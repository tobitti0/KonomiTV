import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ruamel.yaml

from app import config as config_module
from app.metadata.ProgramTitleParser import ProgramTitleRegexRule


class ProgramTitleRegexSettingsTest(unittest.TestCase):

    def test_save_program_title_regexes_updates_file_and_runtime_only_for_regexes(self) -> None:
        runtime_config = config_module.ServerSettings()
        runtime_config.video.program_title_regexes = [
            ProgramTitleRegexRule(name='実行中ルール', pattern=r'(runtime old)'),
        ]
        new_rules = [
            ProgramTitleRegexRule(name='話数あり', pattern=r'^(.+?)\s+#(\d+)\s+(.+)$'),
            ProgramTitleRegexRule(name='一時停止中', pattern=r'^(.+?)「(.+)」$', enabled=False),
        ]

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
                config_module.SaveProgramTitleRegexes(new_rules)

            saved_text = config_path.read_text(encoding='utf-8')
            saved_config = ruamel.yaml.YAML().load(saved_text)

        self.assertNotIn('program_title_regex', saved_config['video'])
        self.assertEqual(saved_config['video']['program_title_regexes'][0]['name'], '話数あり')
        self.assertEqual(saved_config['video']['program_title_regexes'][0]['pattern'], new_rules[0].pattern)
        self.assertEqual(saved_config['video']['program_title_regexes'][0]['enabled'], True)
        self.assertEqual(saved_config['video']['program_title_regexes'][1]['enabled'], False)
        self.assertEqual(saved_config['video']['recorded_folders'], ['/recorded/pending'])
        self.assertEqual(saved_config['general']['debug'], True)
        self.assertEqual(saved_config['custom_section']['pending_value'], 'keep-me')
        self.assertIn('# このコメントと、再起動待ちの別設定を保持する', saved_text)
        self.assertEqual(runtime_config.video.program_title_regexes, new_rules)


    def test_invalid_program_title_regexes_are_not_saved_or_applied(self) -> None:
        runtime_config = config_module.ServerSettings()
        old_rules = [rule.model_copy(deep=True) for rule in runtime_config.video.program_title_regexes]
        invalid_rules = [ProgramTitleRegexRule.model_construct(name='不正', pattern='(', enabled=True)]

        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / 'config.yaml'
            original_text = "video:\n    program_title_regex: '(saved old)'\n"
            config_path.write_text(original_text, encoding='utf-8')

            with (
                patch.object(config_module, '_CONFIG_YAML_PATH', config_path),
                patch.object(config_module, '_CONFIG', runtime_config),
            ):
                with self.assertRaises(ValueError):
                    config_module.SaveProgramTitleRegexes(invalid_rules)

            self.assertEqual(config_path.read_text(encoding='utf-8'), original_text)

        self.assertEqual(runtime_config.video.program_title_regexes, old_rules)


    def test_legacy_program_title_regex_is_migrated_to_rule_list(self) -> None:
        legacy_pattern = r'^(.+?)\s+#(\d+)$'
        settings = config_module.ServerSettings.model_validate({
            'video': {
                'program_title_regex': legacy_pattern,
            },
        })

        self.assertEqual(len(settings.video.program_title_regexes), 2)
        self.assertEqual(settings.video.program_title_regexes[0].name, '従来の正規表現')
        self.assertEqual(settings.video.program_title_regexes[0].pattern, legacy_pattern)
        self.assertEqual(settings.video.program_title_regexes[1].name, '話数なしのサブタイトル付き番組')
