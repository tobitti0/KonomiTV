import asyncio
import unittest
from datetime import datetime

from tortoise import Tortoise

from app import schemas
from app.constants import JST
from app.metadata.RecordedScanTask import RecordedScanTask
from app.models.Channel import Channel
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedVideo import RecordedVideo
from app.models.Series import Series
from app.models.SeriesBroadcastPeriod import SeriesBroadcastPeriod
from app.routers.SeriesRouter import SeriesAPI, SeriesListAPI, SeriesSearchAPI


class SeriesMetadataTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        await Tortoise.init(
            db_url = 'sqlite://:memory:',
            modules = {
                'models': [
                    'app.models.Channel',
                    'app.models.RecordedProgram',
                    'app.models.RecordedVideo',
                    'app.models.Series',
                    'app.models.SeriesBroadcastPeriod',
                ],
            },
        )
        await Tortoise.generate_schemas()

        self.channel = Channel(
            id = 'NID32736-SID1024',
            display_channel_id = 'gr011',
            network_id = 32736,
            service_id = 1024,
            transport_stream_id = 32736,
            remocon_id = 1,
            channel_number = '011',
            type = 'GR',
            name = 'テストチャンネル',
            jikkyo_force = None,
            is_subchannel = False,
            is_radiochannel = False,
            is_watchable = False,
        )
        await self.channel.save()

        # RecordedScanTask.__init__() は実際のサーバー設定を必要とするため、シリーズ解決に必要なロックだけを持つテスト用インスタンスを作る
        self.scan_task = object.__new__(RecordedScanTask)
        self.scan_task._series_metadata_lock = asyncio.Lock()
        self.scan_task._series_reclassification_lock = asyncio.Lock()


    async def asyncTearDown(self) -> None:
        await Tortoise.close_connections()


    async def resolveSeriesMetadata(self, series_title: str, start_time: datetime) -> tuple[int | None, int | None]:
        """
        テスト用の番組情報からシリーズと放送期間を解決する。

        Args:
            series_title (str): シリーズ名。
            start_time (datetime): 番組開始日時。

        Returns:
            tuple[int | None, int | None]: シリーズ ID と放送期間 ID。
        """

        return await self.scan_task._RecordedScanTask__resolveSeriesMetadata(
            series_title = series_title,
            description = 'テスト番組の概要',
            genres = [{'major': 'アニメ・特撮', 'middle': '国内アニメ'}],
            start_time = start_time,
            channel_id = self.channel.id,
        )


    async def test_same_title_is_grouped_into_one_series(self) -> None:
        first_series_id, first_period_id = await self.resolveSeriesMetadata(
            'テストアニメ',
            datetime(2026, 1, 1, tzinfo=JST),
        )
        second_series_id, second_period_id = await self.resolveSeriesMetadata(
            'テストアニメ',
            datetime(2026, 1, 8, tzinfo=JST),
        )

        self.assertEqual(first_series_id, second_series_id)
        self.assertEqual(first_period_id, second_period_id)
        self.assertEqual(await Series.all().count(), 1)
        db_period = await SeriesBroadcastPeriod.get(id=first_period_id)
        self.assertEqual(db_period.start_date.isoformat(), '2026-01-01')
        self.assertEqual(db_period.end_date.isoformat(), '2026-01-08')


    async def test_periods_are_merged_when_an_intermediate_recording_bridges_them(self) -> None:
        await self.resolveSeriesMetadata('順不同テストアニメ', datetime(2026, 1, 1, tzinfo=JST))
        await self.resolveSeriesMetadata('順不同テストアニメ', datetime(2026, 6, 30, tzinfo=JST))
        self.assertEqual(await SeriesBroadcastPeriod.all().count(), 2)

        _, merged_period_id = await self.resolveSeriesMetadata(
            '順不同テストアニメ',
            datetime(2026, 4, 1, tzinfo=JST),
        )

        self.assertEqual(await SeriesBroadcastPeriod.all().count(), 1)
        db_period = await SeriesBroadcastPeriod.get(id=merged_period_id)
        self.assertEqual(db_period.start_date.isoformat(), '2026-01-01')
        self.assertEqual(db_period.end_date.isoformat(), '2026-06-30')


    async def test_reclassification_does_not_modify_recorded_video_metadata(self) -> None:
        start_time = datetime(2026, 7, 1, 0, 0, tzinfo=JST)
        db_program = RecordedProgram(
            recording_start_margin = 5.0,
            recording_end_margin = 10.0,
            is_partially_recorded = False,
            channel = self.channel,
            network_id = self.channel.network_id,
            service_id = self.channel.service_id,
            event_id = 100,
            title = '再判定テストアニメ #01「はじまり」',
            series_title = '誤ったシリーズ名',
            episode_number = '99',
            subtitle = '誤ったサブタイトル',
            description = '再判定テスト番組の概要',
            detail = {'出演者': 'テスト出演者'},
            start_time = start_time,
            end_time = datetime(2026, 7, 1, 0, 30, tzinfo=JST),
            duration = 1800.0,
            is_free = True,
            genres = [{'major': 'アニメ・特撮', 'middle': '国内アニメ'}],
            primary_audio_type = '2/0モード(ステレオ)',
            primary_audio_language = '日本語',
            secondary_audio_type = None,
            secondary_audio_language = None,
        )
        await db_program.save()

        cm_sections: list[schemas.CMSection] = [
            {'start_time': 30.0, 'end_time': 60.0},
            {'start_time': 900.0, 'end_time': 930.0},
        ]
        thumbnail_info = schemas.ThumbnailInfo(
            version = 1,
            representative = schemas.ThumbnailImageInfo(
                format = 'WebP',
                width = 480,
                height = 270,
            ),
            tile = schemas.ThumbnailTileInfo(
                format = 'WebP',
                image_width = 480,
                image_height = 270,
                tile_width = 160,
                tile_height = 90,
                total_tiles = 120,
                column_count = 10,
                row_count = 12,
                interval_sec = 15.0,
            ),
        )
        db_video = RecordedVideo(
            recorded_program = db_program,
            status = 'Recorded',
            file_path = '/recorded/reclassification-test.ts',
            file_hash = '0123456789abcdef0123456789abcdef',
            file_size = 1024,
            file_created_at = start_time,
            file_modified_at = start_time,
            recording_start_time = start_time,
            recording_end_time = datetime(2026, 7, 1, 0, 30, tzinfo=JST),
            duration = 1800.0,
            container_format = 'MPEG-TS',
            video_codec = 'MPEG-2',
            video_codec_profile = 'Main',
            video_scan_type = 'Interlaced',
            video_frame_rate = 29.97,
            video_resolution_width = 1920,
            video_resolution_height = 1080,
            primary_audio_codec = 'AAC-LC',
            primary_audio_channel = 'Stereo',
            primary_audio_sampling_rate = 48000,
            secondary_audio_codec = None,
            secondary_audio_channel = None,
            secondary_audio_sampling_rate = None,
            key_frames = [{'offset': 1880, 'dts': 90000}],
            segment_map = [{
                'sequence_index': 0,
                'source_file_position': 1880,
                'source_start_dts': 90000,
            }],
            cm_sections = cm_sections,
            thumbnail_info = thumbnail_info,
        )
        await db_video.save()
        video_updated_at_before = db_video.updated_at

        result = await self.scan_task.reclassifySeries(r'^(.+?)\s+#(\d+)「(.+)」$')

        self.assertEqual(result.total_count, 1)
        self.assertEqual(result.matched_count, 1)
        self.assertEqual(result.unmatched_count, 0)
        self.assertEqual(result.changed_count, 1)

        await db_program.refresh_from_db()
        self.assertEqual(db_program.series_title, '再判定テストアニメ')
        self.assertEqual(db_program.episode_number, '01')
        self.assertEqual(db_program.subtitle, 'はじまり')
        self.assertIsNotNone(db_program.series_id)
        self.assertIsNotNone(db_program.series_broadcast_period_id)

        # シリーズ画面が利用する一覧・検索・詳細 API で、放送期間から録画番組まで正しく取得できる
        series_list_response = schemas.SeriesList.model_validate(await SeriesListAPI(order='desc', page=1))
        self.assertEqual(series_list_response.total, 1)
        self.assertEqual(series_list_response.series_list[0].title, '再判定テストアニメ')
        self.assertEqual(series_list_response.series_list[0].broadcast_periods[0].recorded_programs[0].id, db_program.id)
        self.assertEqual(
            series_list_response.series_list[0].broadcast_periods[0].recorded_programs[0].recorded_video.storage_type,
            'Local',
        )

        series_search_response = schemas.SeriesList.model_validate(
            await SeriesSearchAPI(query='再判定', order='desc', page=1)
        )
        self.assertEqual(series_search_response.total, 1)

        series_response = await SeriesAPI(db_program.series_id)  # type: ignore[arg-type]
        self.assertEqual(series_response.title, '再判定テストアニメ')
        self.assertEqual(series_response.broadcast_periods[0].recorded_programs[0].id, db_program.id)

        # シリーズ再判定は RecordedVideo を一切更新せず、CM・サムネイル・再生用キャッシュをそのまま保持する
        await db_video.refresh_from_db()
        self.assertEqual(db_video.cm_sections, cm_sections)
        self.assertEqual(db_video.thumbnail_info, thumbnail_info)
        self.assertEqual(db_video.key_frames, [{'offset': 1880, 'dts': 90000}])
        self.assertEqual(db_video.segment_map, [{
            'sequence_index': 0,
            'source_file_position': 1880,
            'source_start_dts': 90000,
        }])
        self.assertEqual(db_video.updated_at, video_updated_at_before)


if __name__ == '__main__':
    unittest.main()
