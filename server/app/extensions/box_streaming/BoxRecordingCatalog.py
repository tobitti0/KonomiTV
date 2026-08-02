from __future__ import annotations

import asyncio
import os
import pathlib
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from tortoise import connections

from app import logging, schemas
from app.extensions.box_streaming.BoxClient import (
    BoxAPIError,
    BoxClient,
    BoxFileMetadata,
)


BoxAvailability = Literal['Available', 'Missing', 'Error']


@dataclass(frozen=True, slots=True)
class BoxRecordedFile:
    """録画番組と Box ファイルの永続的な紐付け。"""

    recorded_program_id: int
    box_file_id: str
    name: str
    size: int
    sha1: str | None
    availability: BoxAvailability
    match_method: str
    last_synced_at: str


@dataclass(frozen=True, slots=True)
class PendingBoxRecording:
    """KonomiTV 側の解析完了を待っている Box 録画通知。"""

    box_file_id: str
    name: str
    size: int
    original_file_name: str | None
    original_file_size: int | None
    received_at: str


class BoxRecordingCatalog:
    """Box 録画ファイルの探索・自動紐付け・参照を担当する拡張カタログ。"""

    _mappings_by_program_id: dict[int, BoxRecordedFile] = {}
    _sync_lock = asyncio.Lock()
    _sync_task: asyncio.Task[None] | None = None
    _pending_match_task: asyncio.Task[None] | None = None
    _is_initialized = False

    @classmethod
    async def initialize(cls, client: BoxClient | None) -> None:
        """
        拡張テーブルを作成し、既存の紐付けをメモリへロードする。

        Args:
            client: Box 連携が有効な場合の Box クライアント。未設定時は None。
        """

        connection = connections.get('default')
        await connection.execute_query('''
            CREATE TABLE IF NOT EXISTS box_recorded_files (
                recorded_program_id INTEGER PRIMARY KEY,
                box_file_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                size INTEGER NOT NULL,
                sha1 TEXT NULL,
                availability TEXT NOT NULL DEFAULT 'Available',
                match_method TEXT NOT NULL,
                last_synced_at TEXT NOT NULL,
                FOREIGN KEY (recorded_program_id) REFERENCES recorded_programs(id) ON DELETE CASCADE
            )
        ''')
        await connection.execute_query('''
            CREATE INDEX IF NOT EXISTS idx_box_recorded_files_box_file_id
            ON box_recorded_files(box_file_id)
        ''')
        await connection.execute_query('''
            CREATE TABLE IF NOT EXISTS box_pending_recordings (
                box_file_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                size INTEGER NOT NULL,
                sha1 TEXT NULL,
                original_file_name TEXT NULL,
                original_file_size INTEGER NULL,
                received_at TEXT NOT NULL
            )
        ''')
        await cls._reloadMappings()
        cls._is_initialized = True

        # アップロード通知が KonomiTV のローカルTS解析より先に届いた場合だけ、DB内の保留通知を軽量に再照合する
        if cls._pending_match_task is None:
            cls._pending_match_task = asyncio.create_task(cls._runPendingMatcher())

        if client is None:
            return

        # 永続済みの紐付けは設定変更後も再生できるよう、同期対象フォルダから発見済みの安全な ID として再登録する
        for mapping in cls._mappings_by_program_id.values():
            client.registerDiscoveredFile(mapping.box_file_id)
        # 保留通知も再起動後に管理者が手動紐付けできるよう、通知時に検証済みの ID として復元する
        for pending in await cls.getPending():
            client.registerDiscoveredFile(pending.box_file_id)

        # 初回同期を録画フォルダの削除スキャンより先に完了させることで、
        # ローカルから既に消えた録画番組レコードも Box 側に存在すれば保持する
        await cls.syncConfiguredFiles(client)
        sync_interval = cls._getSyncIntervalSeconds()
        if sync_interval > 0 and cls._sync_task is None:
            cls._sync_task = asyncio.create_task(cls._runPeriodicSync(client, sync_interval))

    @classmethod
    async def shutdown(cls) -> None:
        """定期同期タスクを停止する。"""

        if cls._sync_task is not None:
            cls._sync_task.cancel()
            try:
                await cls._sync_task
            except asyncio.CancelledError:
                pass
            cls._sync_task = None
        if cls._pending_match_task is not None:
            cls._pending_match_task.cancel()
            try:
                await cls._pending_match_task
            except asyncio.CancelledError:
                pass
            cls._pending_match_task = None

    @classmethod
    def get(cls, recorded_program_id: int) -> BoxRecordedFile | None:
        """
        録画番組 ID に対応する Box 紐付けを取得する。

        Args:
            recorded_program_id: KonomiTV の録画番組 ID。

        Returns:
            Box 紐付け。未登録の場合は None。
        """

        return cls._mappings_by_program_id.get(recorded_program_id)

    @classmethod
    def has(cls, recorded_program_id: int) -> bool:
        """
        録画番組が Box ファイルに紐付いているかを返す。

        Args:
            recorded_program_id: KonomiTV の録画番組 ID。

        Returns:
            紐付けが存在する場合は True。
        """

        return recorded_program_id in cls._mappings_by_program_id

    @classmethod
    def getAll(cls) -> list[BoxRecordedFile]:
        """
        現在の Box 紐付けを録画番組 ID 順で返す。

        Returns:
            Box 紐付けの一覧。
        """

        return [cls._mappings_by_program_id[key] for key in sorted(cls._mappings_by_program_id)]

    @classmethod
    def getByBoxFileID(cls, file_id: str) -> BoxRecordedFile | None:
        """
        Box ファイル ID に対応する紐付けを取得する。

        Args:
            file_id: Box ファイル ID。

        Returns:
            対応する紐付け。未登録の場合は None。
        """

        for mapping in cls._mappings_by_program_id.values():
            if mapping.box_file_id == file_id:
                return mapping
        return None

    @classmethod
    async def getPending(cls) -> list[PendingBoxRecording]:
        """
        ローカル録画番組との一致待ちになっている通知を返す。

        Returns:
            保留中の Box 録画通知。
        """

        connection = connections.get('default')
        rows = await connection.execute_query_dict('''
            SELECT box_file_id, name, size, original_file_name, original_file_size, received_at
            FROM box_pending_recordings
            ORDER BY received_at ASC
        ''')
        return [
            PendingBoxRecording(
                box_file_id = str(row['box_file_id']),
                name = str(row['name']),
                size = int(row['size']),
                original_file_name = str(row['original_file_name']) if row['original_file_name'] is not None else None,
                original_file_size = int(row['original_file_size']) if row['original_file_size'] is not None else None,
                received_at = str(row['received_at']),
            )
            for row in rows
        ]

    @classmethod
    def decorateRecordedProgram(cls, recorded_program: schemas.RecordedProgram) -> schemas.RecordedProgram:
        """
        API レスポンスへローカル・Box の保存状態を付加する。

        Args:
            recorded_program: 装飾対象の録画番組。

        Returns:
            保存状態を付加した録画番組。
        """

        mapping = cls.get(recorded_program.id)
        local_exists = pathlib.Path(recorded_program.recorded_video.file_path).is_file()
        if mapping is None:
            recorded_program.recorded_video.storage_type = 'Local'
            recorded_program.recorded_video.box_file_id = None
            recorded_program.recorded_video.box_availability = None
        else:
            recorded_program.recorded_video.storage_type = 'Local+Box' if local_exists is True else 'Box'
            recorded_program.recorded_video.box_file_id = mapping.box_file_id
            recorded_program.recorded_video.box_availability = mapping.availability
        return recorded_program

    @classmethod
    async def link(
        cls,
        recorded_program_id: int,
        metadata: BoxFileMetadata,
        match_method: str,
    ) -> BoxRecordedFile:
        """
        録画番組と Box ファイルを紐付ける。

        Args:
            recorded_program_id: KonomiTV の録画番組 ID。
            metadata: Box ファイルのメタデータ。
            match_method: 自動一致または手動指定を表す識別子。

        Returns:
            保存した Box 紐付け。
        """

        now = datetime.now(UTC).isoformat()
        connection = connections.get('default')
        # 同じ Box ファイルを別番組へ手動で付け替える場合に備え、先に旧紐付けを外す
        await connection.execute_query(
            'DELETE FROM box_recorded_files WHERE box_file_id = ? AND recorded_program_id != ?',
            [metadata.id, recorded_program_id],
        )
        await connection.execute_query('''
            INSERT INTO box_recorded_files (
                recorded_program_id, box_file_id, name, size, sha1,
                availability, match_method, last_synced_at
            ) VALUES (?, ?, ?, ?, ?, 'Available', ?, ?)
            ON CONFLICT(recorded_program_id) DO UPDATE SET
                box_file_id = excluded.box_file_id,
                name = excluded.name,
                size = excluded.size,
                sha1 = excluded.sha1,
                availability = excluded.availability,
                match_method = excluded.match_method,
                last_synced_at = excluded.last_synced_at
        ''', [
            recorded_program_id,
            metadata.id,
            metadata.name,
            metadata.size,
            metadata.sha1,
            match_method,
            now,
        ])
        await connection.execute_query(
            'DELETE FROM box_pending_recordings WHERE box_file_id = ?',
            [metadata.id],
        )
        await cls._reloadMappings()
        mapping = cls.get(recorded_program_id)
        assert mapping is not None
        return mapping

    @classmethod
    async def unlink(cls, recorded_program_id: int) -> None:
        """
        KonomiTV 内の Box 紐付けだけを削除する。Box 上のファイル自体は削除しない。

        Args:
            recorded_program_id: KonomiTV の録画番組 ID。
        """

        connection = connections.get('default')
        await connection.execute_query(
            'DELETE FROM box_recorded_files WHERE recorded_program_id = ?',
            [recorded_program_id],
        )
        cls._mappings_by_program_id.pop(recorded_program_id, None)

    @classmethod
    async def queuePending(
        cls,
        metadata: BoxFileMetadata,
        original_file_name: str | None,
        original_file_size: int | None,
    ) -> None:
        """
        まだ解析済みローカル録画に一致しない Box 通知を永続化する。

        Args:
            metadata: Box ファイルのメタデータ。
            original_file_name: アップロード元ファイル名。
            original_file_size: アップロード元ファイルサイズ。
        """

        connection = connections.get('default')
        await connection.execute_query('''
            INSERT INTO box_pending_recordings (
                box_file_id, name, size, sha1, original_file_name, original_file_size, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(box_file_id) DO UPDATE SET
                name = excluded.name,
                size = excluded.size,
                sha1 = excluded.sha1,
                original_file_name = excluded.original_file_name,
                original_file_size = excluded.original_file_size
        ''', [
            metadata.id,
            metadata.name,
            metadata.size,
            metadata.sha1,
            original_file_name,
            original_file_size,
            datetime.now(UTC).isoformat(),
        ])

    @classmethod
    async def matchPending(cls) -> int:
        """
        保留通知を現在の解析済み録画DBへ再照合する。

        Returns:
            新たに紐付けられた件数。
        """

        connection = connections.get('default')
        rows = await connection.execute_query_dict('''
            SELECT box_file_id, name, size, sha1, original_file_name, original_file_size
            FROM box_pending_recordings
            ORDER BY received_at ASC
        ''')
        linked_count = 0
        for row in rows:
            metadata = BoxFileMetadata(
                id = str(row['box_file_id']),
                name = str(row['name']),
                size = int(row['size']),
                sha1 = str(row['sha1']) if row['sha1'] is not None else None,
            )
            match = await cls.findSafeAutomaticMatch(
                metadata,
                str(row['original_file_name']) if row['original_file_name'] is not None else None,
                int(row['original_file_size']) if row['original_file_size'] is not None else None,
            )
            if match is None:
                continue
            recorded_program_id, match_method = match
            await cls.link(recorded_program_id, metadata, f'pending-{match_method}')
            await connection.execute_query(
                'DELETE FROM box_pending_recordings WHERE box_file_id = ?',
                [metadata.id],
            )
            linked_count += 1
        return linked_count

    @classmethod
    async def syncConfiguredFiles(cls, client: BoxClient) -> dict[str, int]:
        """
        明示 ID と同期対象フォルダから TS を探索し、既存録画番組へ安全に自動紐付けする。

        Args:
            client: Box API クライアント。

        Returns:
            検出・紐付け・未一致件数。
        """

        async with cls._sync_lock:
            metadata_by_id: dict[str, BoxFileMetadata] = {}
            configured_file_ids = cls._parseDecimalIDList('KONOMITV_BOX_ALLOWED_FILE_IDS')
            for file_id in configured_file_ids:
                try:
                    metadata = await client.getFileMetadata(file_id)
                    if pathlib.PurePath(metadata.name).suffix.lower() == '.ts':
                        metadata_by_id[file_id] = metadata
                except BoxAPIError as ex:
                    logging.warning(
                        f'[BoxStreaming] Failed to synchronize configured file. '
                        f'[file_id: {file_id}][status_code: {ex.status_code}][code: {ex.code}]'
                    )
                    await cls._markUnavailable(file_id, 'Missing' if ex.status_code == 404 else 'Error')

            folder_ids = cls._parseDecimalIDList('KONOMITV_BOX_FOLDER_IDS')
            pending_folder_ids = list(folder_ids)
            visited_folder_ids: set[str] = set()
            while pending_folder_ids:
                folder_id = pending_folder_ids.pop(0)
                if folder_id in visited_folder_ids:
                    continue
                visited_folder_ids.add(folder_id)
                try:
                    items = await client.listFolderItems(folder_id)
                except BoxAPIError as ex:
                    logging.warning(
                        f'[BoxStreaming] Failed to scan configured folder. '
                        f'[folder_id: {folder_id}][status_code: {ex.status_code}][code: {ex.code}]'
                    )
                    continue
                for item in items:
                    if item.type == 'folder':
                        pending_folder_ids.append(item.id)
                    elif item.type == 'file' and pathlib.PurePath(item.name).suffix.lower() == '.ts':
                        client.registerDiscoveredFile(item.id)
                        try:
                            metadata_by_id[item.id] = await client.getFileMetadata(item.id)
                        except BoxAPIError as ex:
                            logging.warning(
                                f'[BoxStreaming] Failed to read discovered file metadata. '
                                f'[file_id: {item.id}][status_code: {ex.status_code}][code: {ex.code}]'
                            )

            linked_count = 0
            unmatched_count = 0
            for metadata in metadata_by_id.values():
                existing_program_id = cls._findProgramIDByBoxFileID(metadata.id)
                if existing_program_id is not None:
                    await cls.link(existing_program_id, metadata, 'existing')
                    linked_count += 1
                    continue
                match = await cls.findSafeAutomaticMatch(metadata)
                if match is None:
                    await cls.queuePending(metadata, None, None)
                    unmatched_count += 1
                    continue
                recorded_program_id, match_method = match
                await cls.link(recorded_program_id, metadata, match_method)
                linked_count += 1

            logging.info(
                f'[BoxStreaming] Box recording synchronization completed. '
                f'[found: {len(metadata_by_id)}][linked: {linked_count}][unmatched: {unmatched_count}]'
            )
            return {
                'found': len(metadata_by_id),
                'linked': linked_count,
                'unmatched': unmatched_count,
            }

    @classmethod
    async def findSafeAutomaticMatch(
        cls,
        metadata: BoxFileMetadata,
        original_file_name: str | None = None,
        original_file_size: int | None = None,
    ) -> tuple[int, str] | None:
        """
        Box メタデータまたはアップロード元情報に一意一致する既存録画番組を探す。

        Args:
            metadata: Box ファイルのメタデータ。
            original_file_name: TVDashBoard がアップロードした元ファイル名。
            original_file_size: TVDashBoard がアップロードした元ファイルサイズ。

        Returns:
            一意に一致した録画番組 ID と一致方法。曖昧または未一致なら None。
        """

        connection = connections.get('default')
        rows = await connection.execute_query_dict('''
            SELECT rp.id, rv.file_path, rv.file_size
            FROM recorded_programs rp
            JOIN recorded_videos rv ON rv.recorded_program_id = rp.id
        ''')
        already_linked_program_ids = set(cls._mappings_by_program_id)
        candidates = [row for row in rows if int(row['id']) not in already_linked_program_ids]
        normalized_box_name = cls._normalizeName(metadata.name)
        normalized_original_name = cls._normalizeName(pathlib.PurePath(original_file_name).name) \
            if original_file_name is not None else None

        name_matches = [
            row for row in candidates
            if cls._normalizeName(pathlib.Path(str(row['file_path'])).name)
            in {normalized_box_name, normalized_original_name}
        ]
        if len(name_matches) == 1:
            return (int(name_matches[0]['id']), 'filename')

        target_size = original_file_size if original_file_size is not None else metadata.size
        size_matches = [row for row in candidates if int(row['file_size']) == target_size]
        if len(size_matches) == 1:
            return (int(size_matches[0]['id']), 'file-size')
        return None

    @classmethod
    async def _reloadMappings(cls) -> None:
        """永続テーブルの全紐付けをメモリへロードする。"""

        connection = connections.get('default')
        rows = await connection.execute_query_dict('''
            SELECT recorded_program_id, box_file_id, name, size, sha1,
                   availability, match_method, last_synced_at
            FROM box_recorded_files
        ''')
        cls._mappings_by_program_id = {
            int(row['recorded_program_id']): BoxRecordedFile(
                recorded_program_id = int(row['recorded_program_id']),
                box_file_id = str(row['box_file_id']),
                name = str(row['name']),
                size = int(row['size']),
                sha1 = str(row['sha1']) if row['sha1'] is not None else None,
                availability = str(row['availability']),  # type: ignore[arg-type]
                match_method = str(row['match_method']),
                last_synced_at = str(row['last_synced_at']),
            )
            for row in rows
        }

    @classmethod
    async def _markUnavailable(cls, file_id: str, availability: BoxAvailability) -> None:
        """既存の紐付けを利用不可状態へ更新する。"""

        connection = connections.get('default')
        await connection.execute_query(
            'UPDATE box_recorded_files SET availability = ?, last_synced_at = ? WHERE box_file_id = ?',
            [availability, datetime.now(UTC).isoformat(), file_id],
        )
        await cls._reloadMappings()

    @classmethod
    def _findProgramIDByBoxFileID(cls, file_id: str) -> int | None:
        """メモリ上の紐付けから Box ファイル ID に対応する録画番組 ID を返す。"""

        mapping = cls.getByBoxFileID(file_id)
        return mapping.recorded_program_id if mapping is not None else None

    @staticmethod
    def _parseDecimalIDList(environment_name: str) -> list[str]:
        """カンマ区切りの数値 ID 環境変数を安全にパースする。"""

        values = [value.strip() for value in os.environ.get(environment_name, '').split(',')]
        return [value for value in values if value.isdecimal() is True]

    @staticmethod
    def _normalizeName(name: str) -> str:
        """ファイル名を Unicode 正規化し、大文字小文字を無視できる形式へ変換する。"""

        return unicodedata.normalize('NFKC', name).casefold()

    @staticmethod
    def _getSyncIntervalSeconds() -> float:
        """定期同期間隔を環境変数から取得する。"""

        try:
            return max(0.0, float(os.environ.get('KONOMITV_BOX_SYNC_INTERVAL_SECONDS', '0')))
        except ValueError:
            return 0.0

    @classmethod
    async def _runPeriodicSync(cls, client: BoxClient, interval: float) -> None:
        """設定された間隔で Box 録画カタログを同期する。"""

        while True:
            await asyncio.sleep(interval)
            try:
                await cls.syncConfiguredFiles(client)
            except Exception as ex:
                logging.error('[BoxStreaming] Periodic synchronization failed:', exc_info=ex)

    @classmethod
    async def _runPendingMatcher(cls) -> None:
        """解析完了が後から到着したローカル録画と保留通知を定期的に再照合する。"""

        while True:
            await asyncio.sleep(60.0)
            try:
                linked_count = await cls.matchPending()
                if linked_count > 0:
                    logging.info(f'[BoxStreaming] Pending recordings were linked. [count: {linked_count}]')
            except Exception as ex:
                logging.error('[BoxStreaming] Pending recording matching failed:', exc_info=ex)
