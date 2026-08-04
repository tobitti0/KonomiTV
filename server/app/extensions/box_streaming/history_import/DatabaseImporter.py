from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from app.extensions.box_streaming.history_import.BoxMetadataVerifier import (
    BoxMetadataVerifier,
)
from app.extensions.box_streaming.history_import.Models import (
    HistoricalBoxMetadata,
    HistoricalImportPlan,
    HistoricalMediaDefaults,
    HistoricalRecordingCandidate,
)


class HistoricalDatabaseImporterError(Exception):
    """バックアップまたは過去録画のDB登録を安全に完了できない場合の例外。"""


@dataclass(frozen=True, slots=True)
class HistoricalDatabaseImportResult:
    """DBバックアップと取り込みの最終集計。"""

    backup_path: Path
    backup_sha256: str
    verified_count: int
    imported_count: int
    skipped_linked_count: int
    skipped_pending_count: int
    skipped_existing_program_count: int


class HistoricalDatabaseImporter:
    """検証済みBox過去録画を、バックアップ必須でKonomiTV DBへ登録する。"""

    REQUIRED_TABLE_COLUMNS: dict[str, frozenset[str]] = {
        'channels': frozenset({'id', 'network_id', 'service_id', 'transport_stream_id'}),
        'recorded_programs': frozenset({
            'id', 'recording_start_margin', 'recording_end_margin', 'is_partially_recorded',
            'channel_id', 'network_id', 'service_id', 'event_id', 'series_id',
            'series_broadcast_period_id', 'title', 'series_title', 'episode_number', 'subtitle',
            'description', 'detail', 'start_time', 'end_time', 'duration', 'is_free', 'genres',
            'primary_audio_type', 'primary_audio_language', 'secondary_audio_type',
            'secondary_audio_language',
        }),
        'recorded_videos': frozenset({
            'id', 'recorded_program_id', 'status', 'file_path', 'file_hash', 'file_size',
            'file_created_at', 'file_modified_at', 'recording_start_time', 'recording_end_time',
            'duration', 'container_format', 'video_codec', 'video_codec_profile', 'video_scan_type',
            'video_frame_rate', 'video_resolution_width', 'video_resolution_height',
            'has_video_stream_changes', 'primary_audio_codec', 'primary_audio_channel',
            'primary_audio_sampling_rate', 'secondary_audio_codec', 'secondary_audio_channel',
            'secondary_audio_sampling_rate', 'key_frames', 'segment_map', 'cm_analysis_status',
            'cm_sections', 'thumbnail_info',
        }),
        'box_recorded_files': frozenset({
            'recorded_program_id', 'box_file_id', 'name', 'size', 'sha1', 'availability',
            'match_method', 'last_synced_at',
        }),
        'box_pending_recordings': frozenset({'box_file_id'}),
    }

    def __init__(
        self,
        database_path: Path,
        backup_directory: Path,
        media_defaults: HistoricalMediaDefaults | None = None,
    ) -> None:
        """
        Args:
            database_path: 本番KonomiTVのSQLiteデータベース。
            backup_directory: 適用前バックアップとハッシュを保存するディレクトリ。
            media_defaults: Range解析前の映像・音声初期値。
        """

        self._database_path = database_path
        self._backup_directory = backup_directory
        self._media_defaults = media_defaults or HistoricalMediaDefaults()
        # 同じ放送サービスの既存録画から得た技術情報を再利用し、候補ごとのDB検索と誤った固定値を減らす
        self._media_defaults_by_service: dict[tuple[int, int], HistoricalMediaDefaults] = {}

    def apply(
        self,
        plan: HistoricalImportPlan,
        verification_log_path: Path,
    ) -> HistoricalDatabaseImportResult:
        """
        検証済み候補を、DBバックアップと単一トランザクションを使って登録する。

        Args:
            plan: オフライン生成済みの取り込み計画。
            verification_log_path: Boxメタデータ検証結果JSONL。

        Returns:
            バックアップ情報を含む取り込み結果。

        Raises:
            HistoricalDatabaseImporterError: 未検証候補、DB不整合、バックアップ失敗時。
        """

        verified_records = self._collectVerifiedRecords(plan, verification_log_path)
        if self._database_path.is_file() is False:
            raise HistoricalDatabaseImporterError('KonomiTV database file was not found.')

        connection = sqlite3.connect(self._database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute('PRAGMA busy_timeout = 5000')
        backup_path: Path | None = None
        backup_sha256 = ''
        imported_count = 0
        skipped_linked_count = 0
        skipped_pending_count = 0
        skipped_existing_program_count = 0
        try:
            self._validateDatabase(connection)

            # BEGIN IMMEDIATEで他プロセスの書き込みを止め、バックアップと登録の間にDBが変化しないようにする
            connection.execute('BEGIN IMMEDIATE')
            backup_path, backup_sha256 = self._createBackupWhileLocked(plan.source_fingerprint)

            linked_box_file_ids = self._loadIDSet(connection, 'box_recorded_files')
            pending_box_file_ids = self._loadIDSet(connection, 'box_pending_recordings')
            for candidate, metadata in verified_records:
                # plan生成後に通常運用で紐付けられたIDも、適用直前のDBを正として必ず除外する
                if candidate.box_file_id in linked_box_file_ids:
                    skipped_linked_count += 1
                    continue
                if candidate.box_file_id in pending_box_file_ids:
                    skipped_pending_count += 1
                    continue
                if self._hasExistingProgram(connection, candidate):
                    skipped_existing_program_count += 1
                    continue

                recorded_program_id = self._insertRecordedProgram(connection, candidate)
                self._insertRecordedVideo(connection, recorded_program_id, candidate, metadata)
                self._insertBoxMapping(connection, recorded_program_id, candidate, metadata)
                imported_count += 1
                linked_box_file_ids.add(candidate.box_file_id)

            # コミット後はロールバックできないため、未コミット状態の整合性を検査してから確定する
            self._assertQuickCheck(connection, 'Imported KonomiTV database')
            connection.commit()
        except Exception as ex:
            connection.rollback()
            if isinstance(ex, HistoricalDatabaseImporterError):
                raise
            backup_hint = str(backup_path) if backup_path is not None else 'not-created'
            raise HistoricalDatabaseImporterError(
                f'Historical import was rolled back. [backup: {backup_hint}]'
            ) from ex
        finally:
            connection.close()

        assert backup_path is not None
        return HistoricalDatabaseImportResult(
            backup_path = backup_path,
            backup_sha256 = backup_sha256,
            verified_count = len(verified_records),
            imported_count = imported_count,
            skipped_linked_count = skipped_linked_count,
            skipped_pending_count = skipped_pending_count,
            skipped_existing_program_count = skipped_existing_program_count,
        )

    def inspect(self, plan: HistoricalImportPlan, verification_log_path: Path) -> dict[str, int]:
        """
        DBへ書き込まず、適用可能件数と現在の除外件数だけを返す。

        Args:
            plan: オフライン生成済みの取り込み計画。
            verification_log_path: Boxメタデータ検証結果JSONL。

        Returns:
            検証済み・既存紐付け・保留中・新規候補の件数。

        Raises:
            HistoricalDatabaseImporterError: DBまたは検証結果が不正な場合。
        """

        verified_records = self._collectVerifiedRecords(plan, verification_log_path)
        if self._database_path.is_file() is False:
            raise HistoricalDatabaseImporterError('KonomiTV database file was not found.')
        connection = sqlite3.connect(f'file:{self._database_path}?mode=ro', uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            self._validateDatabase(connection)
            linked_box_file_ids = self._loadIDSet(connection, 'box_recorded_files')
            pending_box_file_ids = self._loadIDSet(connection, 'box_pending_recordings')
            linked_count = 0
            pending_count = 0
            existing_program_count = 0
            ready_count = 0
            for candidate, _ in verified_records:
                if candidate.box_file_id in linked_box_file_ids:
                    linked_count += 1
                elif candidate.box_file_id in pending_box_file_ids:
                    pending_count += 1
                elif self._hasExistingProgram(connection, candidate):
                    existing_program_count += 1
                else:
                    ready_count += 1
            return {
                'verified': len(verified_records),
                'already_linked': linked_count,
                'pending': pending_count,
                'existing_program_without_mapping': existing_program_count,
                'ready': ready_count,
            }
        finally:
            connection.close()

    def _collectVerifiedRecords(
        self,
        plan: HistoricalImportPlan,
        verification_log_path: Path,
    ) -> list[tuple[HistoricalRecordingCandidate, HistoricalBoxMetadata]]:
        """
        計画と最新検証結果をフィンガープリントで照合する。

        Args:
            plan: 取り込み計画。
            verification_log_path: Box検証結果JSONL。

        Returns:
            Boxで存在確認済みの候補とメタデータ。

        Raises:
            HistoricalDatabaseImporterError: 未検証または計画と不一致の候補がある場合。
        """

        results_by_file_id = BoxMetadataVerifier.loadResults(verification_log_path)
        verified_records: list[tuple[HistoricalRecordingCandidate, HistoricalBoxMetadata]] = []
        missing_result_count = 0
        for candidate in plan.candidates:
            result = results_by_file_id.get(candidate.box_file_id)
            if result is None or result.candidate_fingerprint != candidate.calculateFingerprint():
                missing_result_count += 1
                continue
            if result.status == 'Verified':
                if result.metadata is None or result.metadata.id != candidate.box_file_id:
                    raise HistoricalDatabaseImporterError('Verified Box metadata is inconsistent.')
                verified_records.append((candidate, result.metadata))
        if missing_result_count > 0:
            raise HistoricalDatabaseImporterError(
                f'Box verification is incomplete. [remaining: {missing_result_count}]'
            )
        return verified_records

    def _validateDatabase(self, connection: sqlite3.Connection) -> None:
        """
        現在のKonomiTVとBox拡張が期待するDBスキーマであることを確認する。

        Args:
            connection: 検証するSQLite接続。

        Raises:
            HistoricalDatabaseImporterError: テーブルまたは列が不足している場合。
        """

        self._assertQuickCheck(connection, 'KonomiTV database')
        for table_name, required_columns in self.REQUIRED_TABLE_COLUMNS.items():
            rows = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            actual_columns = {str(row['name']) for row in rows}
            missing_columns = required_columns - actual_columns
            if missing_columns:
                raise HistoricalDatabaseImporterError(
                    f'KonomiTV database schema is incompatible. '
                    f'[table: {table_name}][missing_column_count: {len(missing_columns)}]'
                )

    def _createBackupWhileLocked(self, source_fingerprint: str) -> tuple[Path, str]:
        """
        書き込みロック保持中のDBをSQLite Backup APIで複製し、整合性とハッシュを確認する。

        Args:
            source_fingerprint: バックアップ名へ含める取り込み元ハッシュ。

        Returns:
            バックアップパスとSHA-256。

        Raises:
            HistoricalDatabaseImporterError: バックアップ作成・整合性確認・fsyncに失敗した場合。
        """

        self._backup_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
        backup_path = self._backup_directory / (
            f'database-before-box-history-{timestamp}-{source_fingerprint[:12]}.sqlite'
        )
        if backup_path.exists():
            raise HistoricalDatabaseImporterError('The generated database backup path already exists.')

        source_connection: sqlite3.Connection | None = None
        destination_connection: sqlite3.Connection | None = None
        try:
            source_connection = sqlite3.connect(
                f'file:{self._database_path}?mode=ro',
                uri=True,
                timeout=5.0,
            )
            destination_connection = sqlite3.connect(backup_path)
            source_connection.backup(destination_connection)
            destination_connection.commit()
            self._assertQuickCheck(destination_connection, 'KonomiTV database backup')
        except (OSError, sqlite3.Error) as ex:
            if backup_path.exists():
                backup_path.unlink()
            raise HistoricalDatabaseImporterError('KonomiTV database backup could not be created.') from ex
        finally:
            if destination_connection is not None:
                destination_connection.close()
            if source_connection is not None:
                source_connection.close()

        try:
            os.chmod(backup_path, 0o600)
            with backup_path.open('rb') as backup_file:
                os.fsync(backup_file.fileno())
            backup_sha256 = self._calculateFileSHA256(backup_path)
            hash_path = backup_path.with_suffix(f'{backup_path.suffix}.sha256')
            hash_descriptor = os.open(hash_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(hash_descriptor, 'w', encoding='utf-8') as hash_file:
                hash_file.write(f'{backup_sha256}  {backup_path.name}\n')
                hash_file.flush()
                os.fsync(hash_file.fileno())
            directory_descriptor = os.open(self._backup_directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as ex:
            raise HistoricalDatabaseImporterError('KonomiTV database backup could not be synchronized safely.') from ex
        return (backup_path, backup_sha256)

    def _insertRecordedProgram(
        self,
        connection: sqlite3.Connection,
        candidate: HistoricalRecordingCandidate,
    ) -> int:
        """
        EDCB情報からKonomiTVのRecordedProgramを作成する。

        Args:
            connection: 適用トランザクション中のSQLite接続。
            candidate: EDCB識別情報が完全な取り込み候補。

        Returns:
            新規RecordedProgram ID。
        """

        end_time = candidate.start_time + timedelta(seconds=candidate.duration_seconds)
        channel_id = self._findChannelID(connection, candidate)
        cursor = connection.execute('''
            INSERT INTO recorded_programs (
                recording_start_margin, recording_end_margin, is_partially_recorded,
                channel_id, network_id, service_id, event_id,
                series_id, series_broadcast_period_id,
                title, series_title, episode_number, subtitle,
                description, detail, start_time, end_time, duration, is_free, genres,
                primary_audio_type, primary_audio_language,
                secondary_audio_type, secondary_audio_language
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        ''', [
            0.0,
            0.0,
            0,
            channel_id,
            candidate.network_id,
            candidate.service_id,
            candidate.event_id,
            None,
            None,
            candidate.title,
            None,
            None,
            None,
            '番組概要を取得できませんでした。',
            json.dumps(dict[str, str](), ensure_ascii=False),
            candidate.start_time.isoformat(),
            end_time.isoformat(),
            candidate.duration_seconds,
            1,
            json.dumps(list[object](), ensure_ascii=False),
            '2/0モード(ステレオ)',
            '日本語',
            None,
            None,
        ])
        if cursor.lastrowid is None:
            raise HistoricalDatabaseImporterError('RecordedProgram ID was not generated.')
        return cursor.lastrowid

    def _insertRecordedVideo(
        self,
        connection: sqlite3.Connection,
        recorded_program_id: int,
        candidate: HistoricalRecordingCandidate,
        metadata: HistoricalBoxMetadata,
    ) -> None:
        """
        Boxだけに存在するTSのRecordedVideoを未解析状態で作成する。

        Args:
            connection: 適用トランザクション中のSQLite接続。
            recorded_program_id: 親RecordedProgram ID。
            candidate: EDCB識別情報が完全な取り込み候補。
            metadata: Boxで存在確認済みのファイルメタデータ。
        """

        end_time = candidate.start_time + timedelta(seconds=candidate.duration_seconds)
        file_created_at = metadata.created_at or candidate.start_time
        file_modified_at = metadata.modified_at or end_time
        media_defaults = self._findMediaDefaults(connection, candidate)
        # KonomiTVのサムネイル名などで安全に使える32桁16進値を、Box file IDから決定的に生成する
        file_hash = hashlib.md5(
            f'box-history:{candidate.box_file_id}'.encode(),
            usedforsecurity=False,
        ).hexdigest()
        connection.execute('''
            INSERT INTO recorded_videos (
                recorded_program_id, status, file_path, file_hash, file_size,
                file_created_at, file_modified_at, recording_start_time, recording_end_time,
                duration, container_format,
                video_codec, video_codec_profile, video_scan_type, video_frame_rate,
                video_resolution_width, video_resolution_height, has_video_stream_changes,
                primary_audio_codec, primary_audio_channel, primary_audio_sampling_rate,
                secondary_audio_codec, secondary_audio_channel, secondary_audio_sampling_rate,
                key_frames, segment_map, cm_analysis_status, cm_sections, thumbnail_info
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        ''', [
            recorded_program_id,
            'Recorded',
            candidate.original_file_path,
            file_hash,
            metadata.size,
            file_created_at.isoformat(),
            file_modified_at.isoformat(),
            candidate.start_time.isoformat(),
            end_time.isoformat(),
            candidate.duration_seconds,
            'MPEG-TS',
            media_defaults.video_codec,
            media_defaults.video_codec_profile,
            media_defaults.video_scan_type,
            media_defaults.video_frame_rate,
            media_defaults.video_resolution_width,
            media_defaults.video_resolution_height,
            0,
            'AAC-LC',
            media_defaults.primary_audio_channel,
            media_defaults.primary_audio_sampling_rate,
            None,
            None,
            None,
            json.dumps(list[object](), ensure_ascii=False),
            json.dumps(list[object](), ensure_ascii=False),
            'Unanalyzed',
            None,
            None,
        ])

    def _findMediaDefaults(
        self,
        connection: sqlite3.Connection,
        candidate: HistoricalRecordingCandidate,
    ) -> HistoricalMediaDefaults:
        """
        同じNID・SIDの最新既存録画から映像・音声技術情報を再利用する。

        Args:
            connection: SQLite接続。
            candidate: EDCB識別情報が完全な取り込み候補。

        Returns:
            既存録画由来の技術情報、または見つからなければ保守的な初期値。
        """

        service_key = (candidate.network_id, candidate.service_id)
        cached = self._media_defaults_by_service.get(service_key)
        if cached is not None:
            return cached
        row = connection.execute('''
            SELECT
                rv.video_codec,
                rv.video_codec_profile,
                rv.video_scan_type,
                rv.video_frame_rate,
                rv.video_resolution_width,
                rv.video_resolution_height,
                rv.primary_audio_channel,
                rv.primary_audio_sampling_rate
            FROM recorded_programs rp
            JOIN recorded_videos rv ON rv.recorded_program_id = rp.id
            WHERE rp.network_id = ? AND rp.service_id = ?
            ORDER BY rp.start_time DESC, rp.id DESC
            LIMIT 1
        ''', [
            candidate.network_id,
            candidate.service_id,
        ]).fetchone()
        if row is None:
            media_defaults = self._media_defaults
        else:
            try:
                media_defaults = HistoricalMediaDefaults.model_validate(dict(row))
            except ValidationError:
                media_defaults = self._media_defaults
        self._media_defaults_by_service[service_key] = media_defaults
        return media_defaults

    @staticmethod
    def _insertBoxMapping(
        connection: sqlite3.Connection,
        recorded_program_id: int,
        candidate: HistoricalRecordingCandidate,
        metadata: HistoricalBoxMetadata,
    ) -> None:
        """
        新規番組と検証済みBoxファイルを同じトランザクション内で紐付ける。

        Args:
            connection: 適用トランザクション中のSQLite接続。
            recorded_program_id: 親RecordedProgram ID。
            candidate: 取り込み候補。
            metadata: Boxで存在確認済みのファイルメタデータ。
        """

        connection.execute('''
            INSERT INTO box_recorded_files (
                recorded_program_id, box_file_id, name, size, sha1,
                availability, match_method, last_synced_at
            ) VALUES (?, ?, ?, ?, ?, 'Available', 'history-tvdashboard-edcb', ?)
        ''', [
            recorded_program_id,
            candidate.box_file_id,
            metadata.name,
            metadata.size,
            metadata.sha1,
            datetime.now(UTC).isoformat(),
        ])
        connection.execute(
            'DELETE FROM box_pending_recordings WHERE box_file_id = ?',
            [candidate.box_file_id],
        )

    @staticmethod
    def _findChannelID(
        connection: sqlite3.Connection,
        candidate: HistoricalRecordingCandidate,
    ) -> str | None:
        """
        NID・SID・TSIDに一致する既存チャンネルを一意に選ぶ。

        Args:
            connection: SQLite接続。
            candidate: EDCB識別情報が完全な取り込み候補。

        Returns:
            一意に決まるチャンネルID、または不明ならNone。
        """

        rows = connection.execute('''
            SELECT id, transport_stream_id
            FROM channels
            WHERE network_id = ? AND service_id = ?
            ORDER BY CASE WHEN transport_stream_id = ? THEN 0 ELSE 1 END, id ASC
        ''', [
            candidate.network_id,
            candidate.service_id,
            candidate.transport_stream_id,
        ]).fetchall()
        if len(rows) == 0:
            return None
        exact_rows = [row for row in rows if row['transport_stream_id'] == candidate.transport_stream_id]
        if len(exact_rows) == 1:
            return str(exact_rows[0]['id'])
        if len(rows) == 1:
            return str(rows[0]['id'])
        return None

    @staticmethod
    def _hasExistingProgram(
        connection: sqlite3.Connection,
        candidate: HistoricalRecordingCandidate,
    ) -> bool:
        """
        Box IDが未登録でも同じ放送イベントの番組レコードが存在するか確認する。

        Args:
            connection: SQLite接続。
            candidate: EDCB識別情報が完全な取り込み候補。

        Returns:
            2分以内の同一NID・SID・EID番組が存在する場合はTrue。
        """

        row = connection.execute('''
            SELECT id
            FROM recorded_programs
            WHERE network_id = ?
              AND service_id = ?
              AND event_id = ?
              AND ABS((julianday(start_time) - julianday(?)) * 86400.0) <= 120.0
            LIMIT 1
        ''', [
            candidate.network_id,
            candidate.service_id,
            candidate.event_id,
            candidate.start_time.isoformat(),
        ]).fetchone()
        return row is not None

    @staticmethod
    def _loadIDSet(connection: sqlite3.Connection, table_name: str) -> set[str]:
        """
        Box拡張テーブルからfile ID集合を取得する。

        Args:
            connection: SQLite接続。
            table_name: 検証済みのBox拡張テーブル名。

        Returns:
            Box file ID集合。
        """

        rows = connection.execute(f'SELECT box_file_id FROM "{table_name}"').fetchall()
        return {str(row['box_file_id']) for row in rows}

    @staticmethod
    def _assertQuickCheck(connection: sqlite3.Connection, label: str) -> None:
        """
        SQLite quick_checkがokであることを確認する。

        Args:
            connection: 確認するSQLite接続。
            label: エラーで表示するDB種別。

        Raises:
            HistoricalDatabaseImporterError: 整合性検査が失敗した場合。
        """

        rows = connection.execute('PRAGMA quick_check').fetchall()
        values = [str(row[0]) for row in rows]
        if values != ['ok']:
            raise HistoricalDatabaseImporterError(f'{label} failed SQLite quick_check.')

    @staticmethod
    def _calculateFileSHA256(file_path: Path) -> str:
        """
        ファイルをメモリへ一括展開せずSHA-256を計算する。

        Args:
            file_path: ハッシュ対象ファイル。

        Returns:
            SHA-256ハッシュ。
        """

        digest = hashlib.sha256()
        with file_path.open('rb') as source_file:
            while chunk := source_file.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
