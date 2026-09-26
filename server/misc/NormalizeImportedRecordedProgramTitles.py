#!/usr/bin/env python3

# Usage:
#   poetry run python -m misc.NormalizeImportedRecordedProgramTitles inspect
#   poetry run python -m misc.NormalizeImportedRecordedProgramTitles apply \
#       --server-stopped \
#       --expected-change-set-sha256 HASH_FROM_INSPECT \
#       --confirm NORMALIZE_IMPORTED_RECORDED_PROGRAM_TITLES

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.utils.TSInformation import TSInformation


DEFAULT_DATABASE_PATH = Path('data/database.sqlite')
DEFAULT_BACKUP_DIRECTORY = Path('data/backups/imported-recorded-program-title-normalization')
HISTORY_IMPORT_MATCH_METHOD = 'history-tvdashboard-edcb'
APPLY_CONFIRMATION = 'NORMALIZE_IMPORTED_RECORDED_PROGRAM_TITLES'
MAX_SAMPLE_LIMIT = 1000


class ImportedRecordedProgramTitleNormalizationError(Exception):
    """インポート済みタイトルを安全に確認・更新できない場合の例外。"""


@dataclass(frozen=True, slots=True)
class ImportedRecordedProgramTitleChange:
    """1件の録画番組タイトルに対する正規化前後の値。"""

    recorded_program_id: int
    before: str
    after: str


@dataclass(frozen=True, slots=True)
class ImportedRecordedProgramTitleInspection:
    """読み取り専用で集計した正規化対象。"""

    source_match_method: str
    source_record_count: int
    change_count: int
    unchanged_count: int
    change_set_sha256: str
    samples: list[ImportedRecordedProgramTitleChange]


@dataclass(frozen=True, slots=True)
class ImportedRecordedProgramTitleApplyResult:
    """バックアップを伴う正規化の適用結果。"""

    source_match_method: str
    changed_count: int
    change_set_sha256: str
    backup_path: Path | None
    backup_sha256: str | None


class ImportedRecordedProgramTitleNormalizer:
    """
    TVDashboard/Box 履歴インポート由来の RecordedProgram.title だけを一度正規化する。

    Series 関連列や RecordedVideo は一切更新しない。apply() は更新前の SQLite バックアップを
    必須とし、全タイトルを単一トランザクションで更新する。
    """

    REQUIRED_TABLE_COLUMNS: dict[str, frozenset[str]] = {
        'recorded_programs': frozenset({'id', 'title'}),
        'box_recorded_files': frozenset({'recorded_program_id', 'match_method'}),
    }

    def __init__(self, database_path: Path, backup_directory: Path) -> None:
        self._database_path = database_path
        self._backup_directory = backup_directory


    def inspect(self, sample_limit: int = 50) -> ImportedRecordedProgramTitleInspection:
        """DB を変更せず、正規化対象件数・変更集合ハッシュ・サンプルを返す。"""

        if sample_limit < 0 or sample_limit > MAX_SAMPLE_LIMIT:
            raise ImportedRecordedProgramTitleNormalizationError(
                f'sample_limit must be between 0 and {MAX_SAMPLE_LIMIT}.'
            )
        if self._database_path.is_file() is False:
            raise ImportedRecordedProgramTitleNormalizationError('KonomiTV database file was not found.')

        try:
            connection = sqlite3.connect(
                f'file:{self._database_path}?mode=ro',
                uri=True,
                timeout=5.0,
            )
            connection.row_factory = sqlite3.Row
            try:
                self._validateDatabase(connection)
                source_record_count, changes = self._loadChanges(connection)
            finally:
                connection.close()
        except sqlite3.Error as ex:
            raise ImportedRecordedProgramTitleNormalizationError(
                'KonomiTV database could not be inspected safely.'
            ) from ex

        return ImportedRecordedProgramTitleInspection(
            source_match_method = HISTORY_IMPORT_MATCH_METHOD,
            source_record_count = source_record_count,
            change_count = len(changes),
            unchanged_count = source_record_count - len(changes),
            change_set_sha256 = self._calculateChangeSetSHA256(changes),
            samples = changes[:sample_limit],
        )


    def apply(self, expected_change_set_sha256: str) -> ImportedRecordedProgramTitleApplyResult:
        """
        事前確認した変更集合だけを、バックアップ作成後に単一トランザクションで適用する。

        Series 関連列・RecordedVideo・CM 解析結果は更新しない。
        """

        if self._database_path.is_file() is False:
            raise ImportedRecordedProgramTitleNormalizationError('KonomiTV database file was not found.')
        if len(expected_change_set_sha256) != 64 or any(
            character not in '0123456789abcdef' for character in expected_change_set_sha256
        ):
            raise ImportedRecordedProgramTitleNormalizationError(
                'Expected change-set SHA-256 must be a 64-character lowercase hexadecimal string.'
            )

        connection = sqlite3.connect(self._database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute('PRAGMA busy_timeout = 5000')
        backup_path: Path | None = None
        backup_sha256: str | None = None
        try:
            self._validateDatabase(connection)

            # サーバー停止を CLI で明示確認したうえで、他プロセスの書き込みが入り込まないようロックする
            connection.execute('BEGIN IMMEDIATE')
            _, changes = self._loadChanges(connection)
            actual_change_set_sha256 = self._calculateChangeSetSHA256(changes)
            if actual_change_set_sha256 != expected_change_set_sha256:
                raise ImportedRecordedProgramTitleNormalizationError(
                    'The title change set differs from the inspected result. Run inspect again.'
                )

            # 対象が 0 件なら DB ファイルを作らず、何も更新せずに終了する
            if len(changes) == 0:
                connection.rollback()
                return ImportedRecordedProgramTitleApplyResult(
                    source_match_method = HISTORY_IMPORT_MATCH_METHOD,
                    changed_count = 0,
                    change_set_sha256 = actual_change_set_sha256,
                    backup_path = None,
                    backup_sha256 = None,
                )

            backup_path, backup_sha256 = self._createBackupWhileLocked()
            for change in changes:
                cursor = connection.execute(
                    'UPDATE recorded_programs SET title = ? WHERE id = ? AND title = ?',
                    [change.after, change.recorded_program_id, change.before],
                )
                if cursor.rowcount != 1:
                    raise ImportedRecordedProgramTitleNormalizationError(
                        'A recorded program changed while title normalization was running.'
                    )

            # 同一処理をもう一度実行しても変更が発生しないことをコミット前に確認する
            _, remaining_changes = self._loadChanges(connection)
            if len(remaining_changes) != 0:
                raise ImportedRecordedProgramTitleNormalizationError(
                    'Some imported recorded program titles could not be normalized.'
                )
            self._assertQuickCheck(connection, 'Normalized KonomiTV database')
            connection.commit()
        except Exception as ex:
            connection.rollback()
            if isinstance(ex, ImportedRecordedProgramTitleNormalizationError):
                raise
            backup_hint = str(backup_path) if backup_path is not None else 'not-created'
            raise ImportedRecordedProgramTitleNormalizationError(
                f'Title normalization was rolled back. [backup: {backup_hint}]'
            ) from ex
        finally:
            connection.close()

        return ImportedRecordedProgramTitleApplyResult(
            source_match_method = HISTORY_IMPORT_MATCH_METHOD,
            changed_count = len(changes),
            change_set_sha256 = expected_change_set_sha256,
            backup_path = backup_path,
            backup_sha256 = backup_sha256,
        )


    def _validateDatabase(self, connection: sqlite3.Connection) -> None:
        """対象 DB の整合性と最低限必要なスキーマを検証する。"""

        self._assertQuickCheck(connection, 'KonomiTV database')
        for table_name, required_columns in self.REQUIRED_TABLE_COLUMNS.items():
            rows = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            actual_columns = {str(row['name']) for row in rows}
            missing_columns = required_columns - actual_columns
            if missing_columns:
                raise ImportedRecordedProgramTitleNormalizationError(
                    f'KonomiTV database schema is incompatible. '
                    f'[table: {table_name}][missing_column_count: {len(missing_columns)}]'
                )


    @staticmethod
    def _loadChanges(
        connection: sqlite3.Connection,
    ) -> tuple[int, list[ImportedRecordedProgramTitleChange]]:
        """履歴インポート由来のレコードから formatString() で変化するタイトルを抽出する。"""

        rows = connection.execute('''
            SELECT rp.id, rp.title
            FROM recorded_programs rp
            INNER JOIN box_recorded_files brf ON brf.recorded_program_id = rp.id
            WHERE brf.match_method = ?
            ORDER BY rp.id ASC
        ''', [HISTORY_IMPORT_MATCH_METHOD]).fetchall()
        changes: list[ImportedRecordedProgramTitleChange] = []
        for row in rows:
            before = str(row['title'])
            after = TSInformation.formatString(before)
            if before != after:
                changes.append(ImportedRecordedProgramTitleChange(
                    recorded_program_id = int(row['id']),
                    before = before,
                    after = after,
                ))
        return (len(rows), changes)


    def _createBackupWhileLocked(self) -> tuple[Path, str]:
        """SQLite Backup API で更新直前の DB を複製し、quick_check と SHA-256 を保存する。"""

        self._backup_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
        backup_path = self._backup_directory / f'database-before-title-normalization-{timestamp}.sqlite'
        hash_path = backup_path.with_suffix(f'{backup_path.suffix}.sha256')
        if backup_path.exists() or hash_path.exists():
            raise ImportedRecordedProgramTitleNormalizationError(
                'The generated database backup path already exists.'
            )

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
            raise ImportedRecordedProgramTitleNormalizationError(
                'KonomiTV database backup could not be created.'
            ) from ex
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
            raise ImportedRecordedProgramTitleNormalizationError(
                'KonomiTV database backup could not be synchronized safely.'
            ) from ex
        return (backup_path, backup_sha256)


    @staticmethod
    def _calculateChangeSetSHA256(changes: list[ImportedRecordedProgramTitleChange]) -> str:
        """ID・変換前・変換後タイトルを順序固定 JSON にして SHA-256 を計算する。"""

        canonical_json = json.dumps(
            [asdict(change) for change in changes],
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        )
        return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()


    @staticmethod
    def _calculateFileSHA256(file_path: Path) -> str:
        """ファイルを分割読み込みして SHA-256 を計算する。"""

        digest = hashlib.sha256()
        with file_path.open('rb') as source_file:
            while chunk := source_file.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()


    @staticmethod
    def _assertQuickCheck(connection: sqlite3.Connection, label: str) -> None:
        """SQLite quick_check が ok 以外なら処理を中止する。"""

        rows = connection.execute('PRAGMA quick_check').fetchall()
        values = [str(row[0]) for row in rows]
        if values != ['ok']:
            raise ImportedRecordedProgramTitleNormalizationError(
                f'{label} failed SQLite quick_check.'
            )


def CreateParser() -> argparse.ArgumentParser:
    """inspect と apply だけを公開する CLI パーサーを作成する。"""

    parser = argparse.ArgumentParser(
        description=(
            'One-time title normalizer for TVDashboard/Box historical imports. '
            'Only recorded_programs.title is updated.'
        ),
    )
    subparsers = parser.add_subparsers(dest='command', required=True)

    inspect_parser = subparsers.add_parser('inspect', help='Preview changes without writing anything.')
    inspect_parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE_PATH)
    inspect_parser.add_argument('--sample-limit', type=int, default=50)

    apply_parser = subparsers.add_parser(
        'apply',
        help='Back up the database and atomically normalize imported titles.',
    )
    apply_parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE_PATH)
    apply_parser.add_argument('--backup-directory', type=Path, default=DEFAULT_BACKUP_DIRECTORY)
    apply_parser.add_argument(
        '--server-stopped',
        action='store_true',
        help='Assert that the KonomiTV server process has been stopped by the operator.',
    )
    apply_parser.add_argument(
        '--expected-change-set-sha256',
        required=True,
        help='Must match change_set_sha256 from the latest inspect command.',
    )
    apply_parser.add_argument('--confirm', help=f'Must be exactly {APPLY_CONFIRMATION}.')
    return parser


def Main() -> int:
    """CLI を実行し、結果を JSON で標準出力へ表示する。"""

    args = CreateParser().parse_args()
    try:
        if args.command == 'inspect':
            normalizer = ImportedRecordedProgramTitleNormalizer(
                database_path = args.database,
                backup_directory = DEFAULT_BACKUP_DIRECTORY,
            )
            result: object = normalizer.inspect(sample_limit=args.sample_limit)
        else:
            if args.server_stopped is False:
                raise ImportedRecordedProgramTitleNormalizationError(
                    'Apply requires --server-stopped after the operator stops KonomiTV.'
                )
            if args.confirm != APPLY_CONFIRMATION:
                raise ImportedRecordedProgramTitleNormalizationError(
                    f'Apply requires --confirm {APPLY_CONFIRMATION}.'
                )
            normalizer = ImportedRecordedProgramTitleNormalizer(
                database_path = args.database,
                backup_directory = args.backup_directory,
            )
            result = normalizer.apply(args.expected_change_set_sha256)
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))
        return 0
    except ImportedRecordedProgramTitleNormalizationError as ex:
        print(f'ERROR: {ex}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(Main())
