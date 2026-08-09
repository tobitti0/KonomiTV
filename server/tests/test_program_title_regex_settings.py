import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ruamel.yaml

from app import config as config_module


class ProgramTitleRegexSettingsTest(unittest.TestCase):

    def test_save_program_title_regex_updates_file_and_runtime_only_for_regex(self) -> None:
        runtime_config = config_module.ServerSettings()
        runtime_config.video.program_title_regex = r'(runtime old)'
        new_pattern = r'^(.+?)\s+#(\d+)\s+(.+)$'

        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / 'config.yaml'
            config_path.write_text(
                """# このコメントと、再起動待ちの別設定を保持する
general:
    debug: true
video:
    recorded_folders: ['/recorded/pending']
    program_title_regex: '(saved old)'
custom_section:
    pending_value: 'keep-me'
""",
                encoding = 'utf-8',
            )

            with (
                patch.object(config_module, '_CONFIG_YAML_PATH', config_path),
                patch.object(config_module, '_CONFIG', runtime_config),
            ):
                config_module.SaveProgramTitleRegex(new_pattern)

            saved_text = config_path.read_text(encoding='utf-8')
            saved_config = ruamel.yaml.YAML().load(saved_text)

        self.assertEqual(saved_config['video']['program_title_regex'], new_pattern)
        self.assertEqual(saved_config['video']['recorded_folders'], ['/recorded/pending'])
        self.assertEqual(saved_config['general']['debug'], True)
        self.assertEqual(saved_config['custom_section']['pending_value'], 'keep-me')
        self.assertIn('# このコメントと、再起動待ちの別設定を保持する', saved_text)
        self.assertEqual(runtime_config.video.program_title_regex, new_pattern)


    def test_invalid_program_title_regex_is_not_saved_or_applied(self) -> None:
        runtime_config = config_module.ServerSettings()
        old_pattern = runtime_config.video.program_title_regex

        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / 'config.yaml'
            original_text = "video:\n    program_title_regex: '(saved old)'\n"
            config_path.write_text(original_text, encoding='utf-8')

            with (
                patch.object(config_module, '_CONFIG_YAML_PATH', config_path),
                patch.object(config_module, '_CONFIG', runtime_config),
            ):
                with self.assertRaises(ValueError):
                    config_module.SaveProgramTitleRegex('(')

            self.assertEqual(config_path.read_text(encoding='utf-8'), original_text)

        self.assertEqual(runtime_config.video.program_title_regex, old_pattern)
