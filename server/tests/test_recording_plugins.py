import unittest
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import schemas
from app.routers.RecordingPresetsRouter import ParsePreset, RecordingPresetsAPI
from app.routers.ReservationsRouter import (
    DecodeEDCBRecSettingData,
    EncodeEDCBRecSettingData,
    GetCtrlCmdUtil,
    GetReserveData,
    router,
)
from app.utils.edcb import RecFileSetInfoRequired
from app.utils.edcb.EDCBUtil import EDCBUtil


class RecordingPluginsTest(unittest.TestCase):
    """EDCB のプラグイン名とオプションを予約・プリセット間で保持する回帰テスト。"""

    def test_reservation_round_trip_preserves_plugins_and_options(self) -> None:
        """OS・独自名・空設定・空オプションを、通常録画とワンセグの両方で保持する。"""

        for write_plugin, name_plugin in (
            ('Write_Default.so', 'RecName_Macro.so?$title$.ts'),
            ('Write_Default.dll', 'RecName_Macro.dll?$title$.ts'),
            ('Write_Custom.so', 'RecName_Custom.so?prefix?option=%Title%'),
            ('Write_Custom.dll', 'RecName_Custom.dll'),
            ('Write_Default.so', 'RecName_Macro.so?'),
            ('Write_Default.so', ''),
            ('', ''),
        ):
            with self.subTest(write_plugin=write_plugin, name_plugin=name_plugin):
                data = EncodeEDCBRecSettingData(schemas.RecordSettings())
                for key in ('rec_folder_list', 'partial_rec_folder'):
                    data[key] = [RecFileSetInfoRequired(
                        rec_folder = f'/recordings/{key}',
                        write_plug_in = write_plugin,
                        rec_name_plug_in = name_plugin,
                    )]
                # JSON を挟んで、API から返した値をそのまま送り返す編集経路も検証する。
                settings = schemas.RecordSettings.model_validate_json(
                    DecodeEDCBRecSettingData(data).model_dump_json(),
                )
                result = EncodeEDCBRecSettingData(settings)
                self.assertEqual(result['rec_folder_list'], data['rec_folder_list'])
                self.assertEqual(result['partial_rec_folder'], data['partial_rec_folder'])

    def test_empty_folders_remain_empty(self) -> None:
        """フォルダ未指定時は EDCB のデフォルトを使用し、プラグインを生成しない。"""

        data = EncodeEDCBRecSettingData(schemas.RecordSettings())
        result = EncodeEDCBRecSettingData(DecodeEDCBRecSettingData(data))
        self.assertEqual(result['rec_folder_list'], [])
        self.assertEqual(result['partial_rec_folder'], [])

    def test_legacy_payload_without_plugin_fields_is_rejected(self) -> None:
        """旧クライアントの省略値から Windows プラグインを補完しない。"""

        for fields in (
            {},
            {'write_plugin': 'Write_Default.so'},
            {'recording_file_name_plugin': None},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                schemas.RecordSettings.model_validate({
                    'recording_folders': [{'recording_folder_path': '/recordings', **fields}],
                })

    def test_explicit_null_name_plugin_is_preserved(self) -> None:
        """明示的な null は未指定として保存し、マクロプラグインへ置き換えない。"""

        settings = schemas.RecordSettings(recording_folders=[schemas.RecordingFolder(
            recording_folder_path = '/recordings',
            write_plugin = '',
            recording_file_name_plugin = None,
        )])
        self.assertEqual(EncodeEDCBRecSettingData(settings)['rec_folder_list'], [RecFileSetInfoRequired(
            rec_folder = '/recordings', write_plug_in = '', rec_name_plug_in = '',
        )])

    def test_api_rejects_legacy_folders_before_writing_to_edcb(self) -> None:
        """追加・更新 API が旧形式を 422 で拒否し、EDCB へ書き込まないことを確認する。"""

        app = FastAPI()
        app.include_router(router)
        edcb = AsyncMock()
        app.dependency_overrides[GetCtrlCmdUtil] = lambda: edcb
        app.dependency_overrides[GetReserveData] = lambda: dict()
        body = {
            'program_id': 'NID1-SID1-EID1',
            'record_settings': {'recording_folders': [{'recording_folder_path': '/recordings'}]},
        }
        with TestClient(app) as client:
            for method, path in (
                ('POST', '/api/recording/reservations'),
                ('PUT', '/api/recording/reservations/1'),
            ):
                with self.subTest(method=method):
                    response = client.request(method, path, json=body)
                    self.assertEqual(response.status_code, 422)
                    missing_fields = {error['loc'][-1] for error in response.json()['detail']}
                    self.assertEqual(missing_fields, {'write_plugin', 'recording_file_name_plugin'})
        edcb.sendAddReserve.assert_not_awaited()
        edcb.sendChgReserve.assert_not_awaited()

    def test_presets_preserve_plugins_for_normal_and_oneseg_folders(self) -> None:
        """デフォルト・独自プリセットを新規予約に変換してもプラグイン設定を保持する。"""

        for preset_id in (0, 2):
            suffix = '' if preset_id == 0 else str(preset_id)
            for write_plugin, name_plugin in (
                ('Write_Default.so', 'RecName_Macro.so?$title$.ts'),
                ('Write_Default.dll', 'RecName_Macro.dll?$title$.ts'),
                ('Write_Custom.so', 'RecName_Custom.so?value?%Title%'),
                ('Write_Default.so', 'RecName_Macro.so?'),
                ('', ''),
            ):
                with self.subTest(preset_id=preset_id, name_plugin=name_plugin):
                    config = EDCBUtil.parseEDCBIni(
                        f'[REC_DEF{suffix}]\nSetName=Test\n'
                        f'[REC_DEF_FOLDER{suffix}]\nCount=1\n0=/recordings\n'
                        f'WritePlugIn0={write_plugin}\nRecNamePlugIn0={name_plugin}\n'
                        f'[REC_DEF_FOLDER_1SEG{suffix}]\nCount=1\n0=/oneseg\n'
                        'WritePlugIn0=Write_OneService.so\nRecNamePlugIn0=RecName_Other.so?one\n',
                        is_preserve_case=True,
                    )
                    data = EncodeEDCBRecSettingData(ParsePreset(config, preset_id).record_settings)
                    self.assertEqual(data['rec_folder_list'], [RecFileSetInfoRequired(
                        rec_folder = '/recordings', write_plug_in = write_plugin, rec_name_plug_in = name_plugin,
                    )])
                    self.assertEqual(data['partial_rec_folder'], [RecFileSetInfoRequired(
                        rec_folder = '/oneseg', write_plug_in = 'Write_OneService.so',
                        rec_name_plug_in = 'RecName_Other.so?one',
                    )])

    def test_preset_missing_plugin_keys_does_not_invent_plugins(self) -> None:
        """INI のキー欠落時も EDCB の未指定値を維持する。"""

        config = EDCBUtil.parseEDCBIni('[REC_DEF_FOLDER]\nCount=1\n0=/recordings\n', is_preserve_case=True)
        folder = ParsePreset(config, 0).record_settings.recording_folders[0]
        self.assertEqual(folder.write_plugin, '')
        self.assertIsNone(folder.recording_file_name_plugin)
        self.assertIsNone(folder.recording_file_name_template)


class RecordingPresetsFailureTest(unittest.IsolatedAsyncioTestCase):
    """EDCB から設定を取得できない場合に架空のプリセットを返さない。"""

    async def test_unavailable_ini_is_an_error(self) -> None:
        """通信失敗・空の応答では新規予約用のデフォルト取得を失敗させる。"""

        for response in (None, [], [{'name': 'EpgTimerSrv.ini', 'data': b''}]):
            with self.subTest(response=response):
                edcb = AsyncMock()
                edcb.sendFileCopy2.return_value = response
                with self.assertRaises(HTTPException) as error:
                    await RecordingPresetsAPI(edcb=edcb)
                self.assertEqual(error.exception.status_code, 500)


if __name__ == '__main__':
    unittest.main()
