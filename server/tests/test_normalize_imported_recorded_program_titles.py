import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from misc.NormalizeImportedRecordedProgramTitles import (
    HISTORY_IMPORT_MATCH_METHOD,
    ImportedRecordedProgramTitleNormalizationError,
    ImportedRecordedProgramTitleNormalizer,
)


class ImportedRecordedProgramTitleNormalizerTest(unittest.TestCase):

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base_directory = Path(self.temporary_directory.name)
        self.database_path = self.base_directory / 'database.sqlite'
        self.backup_directory = self.base_directory / 'backups'
        connection = sqlite3.connect(self.database_path)
        try:
            connection.executescript('''
                CREATE TABLE recorded_programs (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    series_id INTEGER NULL,
                    series_title TEXT NULL,
                    episode_number TEXT NULL,
                    subtitle TEXT NULL
                );
                CREATE TABLE box_recorded_files (
                    recorded_program_id INTEGER PRIMARY KEY,
                    match_method TEXT NOT NULL
                );
                CREATE TABLE recorded_videos (
                    id INTEGER PRIMARY KEY,
                    recorded_program_id INTEGER NOT NULL,
                    cm_analysis_status TEXT NOT NULL,
                    cm_sections TEXT NULL,
                    key_frames TEXT NOT NULL
                );
            ''')
            connection.executemany('''
                INSERT INTO recorded_programs (
                    id, title, series_id, series_title, episode_number, subtitle
                ) VALUES (?, ?, ?, ?, ?, ?)
            ''', [
                (
                    1,
                    '新プロジェクトＸ～挑戦者たち～　７月ラインナップＰＲ',
                    101,
                    '変更してはいけないシリーズＸ',
                    '７',
                    '変更してはいけない字幕Ｘ',
                ),
                (2, 'すでに正規化済み #2', 102, '正規化済みシリーズ', '2', None),
                (3, '通常録画Ｘ　＃３', 103, '通常録画シリーズＸ', '３', None),
            ])
            connection.executemany('''
                INSERT INTO box_recorded_files (recorded_program_id, match_method)
                VALUES (?, ?)
            ''', [
                (1, HISTORY_IMPORT_MATCH_METHOD),
                (2, HISTORY_IMPORT_MATCH_METHOD),
                (3, 'filename'),
            ])
            connection.execute('''
                INSERT INTO recorded_videos (
                    id, recorded_program_id, cm_analysis_status, cm_sections, key_frames
                ) VALUES (?, ?, ?, ?, ?)
            ''', [
                1,
                1,
                'Completed',
                '[{"start_time": 10.0, "end_time": 40.0}]',
                '[{"offset": 1234, "dts": 90000}]',
            ])
            connection.commit()
        finally:
            connection.close()

        self.normalizer = ImportedRecordedProgramTitleNormalizer(
            database_path = self.database_path,
            backup_directory = self.backup_directory,
        )


    def tearDown(self) -> None:
        self.temporary_directory.cleanup()


    def test_inspect_only_returns_history_import_title_changes(self) -> None:
        inspection = self.normalizer.inspect(sample_limit=10)

        self.assertEqual(inspection.source_record_count, 2)
        self.assertEqual(inspection.change_count, 1)
        self.assertEqual(inspection.unchanged_count, 1)
        self.assertEqual(len(inspection.samples), 1)
        self.assertEqual(inspection.samples[0].recorded_program_id, 1)
        self.assertEqual(
            inspection.samples[0].after,
            '新プロジェクトX～挑戦者たち～ 7月ラインナップPR',
        )


    def test_apply_only_updates_title_and_creates_verified_backup(self) -> None:
        inspection = self.normalizer.inspect(sample_limit=10)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            recorded_video_before = dict(connection.execute(
                'SELECT * FROM recorded_videos WHERE id = 1'
            ).fetchone())
            series_metadata_before = tuple(connection.execute('''
                SELECT series_id, series_title, episode_number, subtitle
                FROM recorded_programs WHERE id = 1
            ''').fetchone())
        finally:
            connection.close()

        result = self.normalizer.apply(inspection.change_set_sha256)

        self.assertEqual(result.changed_count, 1)
        self.assertIsNotNone(result.backup_path)
        self.assertIsNotNone(result.backup_sha256)
        assert result.backup_path is not None
        assert result.backup_sha256 is not None
        self.assertTrue(result.backup_path.is_file())
        hash_path = result.backup_path.with_suffix(f'{result.backup_path.suffix}.sha256')
        self.assertEqual(
            hash_path.read_text(encoding='utf-8'),
            f'{result.backup_sha256}  {result.backup_path.name}\n',
        )
        self.assertEqual(
            hashlib.sha256(result.backup_path.read_bytes()).hexdigest(),
            result.backup_sha256,
        )

        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            self.assertEqual(
                connection.execute('SELECT title FROM recorded_programs WHERE id = 1').fetchone()[0],
                '新プロジェクトX～挑戦者たち～ 7月ラインナップPR',
            )
            # 同じ DB 内の通常録画は、formatString() で変化するタイトルでも対象外
            self.assertEqual(
                connection.execute('SELECT title FROM recorded_programs WHERE id = 3').fetchone()[0],
                '通常録画Ｘ　＃３',
            )
            self.assertEqual(
                tuple(connection.execute('''
                    SELECT series_id, series_title, episode_number, subtitle
                    FROM recorded_programs WHERE id = 1
                ''').fetchone()),
                series_metadata_before,
            )
            self.assertEqual(
                dict(connection.execute('SELECT * FROM recorded_videos WHERE id = 1').fetchone()),
                recorded_video_before,
            )
        finally:
            connection.close()

        # バックアップには更新前タイトルが残っている
        backup_connection = sqlite3.connect(result.backup_path)
        try:
            self.assertEqual(
                backup_connection.execute(
                    'SELECT title FROM recorded_programs WHERE id = 1'
                ).fetchone()[0],
                '新プロジェクトＸ～挑戦者たち～　７月ラインナップＰＲ',
            )
        finally:
            backup_connection.close()

        after_inspection = self.normalizer.inspect(sample_limit=10)
        self.assertEqual(after_inspection.change_count, 0)


    def test_apply_rejects_change_set_that_was_not_inspected(self) -> None:
        with self.assertRaisesRegex(
            ImportedRecordedProgramTitleNormalizationError,
            'differs from the inspected result',
        ):
            self.normalizer.apply('0' * 64)

        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute('SELECT title FROM recorded_programs WHERE id = 1').fetchone()[0],
                '新プロジェクトＸ～挑戦者たち～　７月ラインナップＰＲ',
            )
        finally:
            connection.close()
        self.assertFalse(self.backup_directory.exists())


    def test_apply_rolls_back_every_title_when_one_update_fails(self) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute('''
                INSERT INTO recorded_programs (id, title, series_id, series_title, episode_number, subtitle)
                VALUES (4, '更新失敗テストＸ', NULL, NULL, NULL, NULL)
            ''')
            connection.execute('''
                INSERT INTO box_recorded_files (recorded_program_id, match_method)
                VALUES (4, ?)
            ''', [HISTORY_IMPORT_MATCH_METHOD])
            connection.execute('''
                CREATE TRIGGER reject_test_title_update
                BEFORE UPDATE OF title ON recorded_programs
                WHEN OLD.id = 4
                BEGIN
                    SELECT RAISE(ABORT, 'intentional test failure');
                END;
            ''')
            connection.commit()
        finally:
            connection.close()

        inspection = self.normalizer.inspect(sample_limit=10)
        with self.assertRaisesRegex(
            ImportedRecordedProgramTitleNormalizationError,
            'was rolled back',
        ):
            self.normalizer.apply(inspection.change_set_sha256)

        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute('SELECT title FROM recorded_programs WHERE id = 1').fetchone()[0],
                '新プロジェクトＸ～挑戦者たち～　７月ラインナップＰＲ',
            )
            self.assertEqual(
                connection.execute('SELECT title FROM recorded_programs WHERE id = 4').fetchone()[0],
                '更新失敗テストＸ',
            )
        finally:
            connection.close()
        # 更新はロールバックされても、復旧に使える更新前バックアップは保持される
        self.assertEqual(len(list(self.backup_directory.glob('*.sqlite'))), 1)


if __name__ == '__main__':
    unittest.main()
