# Type Hints を指定できるように
# ref: https://stackoverflow.com/a/33533514/17124142
from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Literal, cast

from tortoise import fields
from tortoise.fields import Field as TortoiseField
from tortoise.models import Model as TortoiseModel

from app.schemas import CMAnalysisError, CMAnalysisStageResult, CMSection


if TYPE_CHECKING:
    from app.models.RecordedVideo import RecordedVideo


class CMAnalysisRun(TortoiseModel):
    """
    CM 区間解析を1回実行した結果と各工程の診断情報を保持するモデル
    """

    # データベース上のテーブル名
    class Meta(TortoiseModel.Meta):
        table: str = 'cm_analysis_runs'
        indexes = (('recorded_video_id', 'created_at'),)

    id = fields.IntField(pk=True)
    # 解析対象の録画ファイル
    # 録画ファイルが削除された場合は、参照不能な解析履歴だけが残らないよう cascade で削除する
    recorded_video: fields.ForeignKeyRelation[RecordedVideo] = \
        fields.ForeignKeyField('models.RecordedVideo', related_name='cm_analysis_runs', on_delete=fields.CASCADE)
    recorded_video_id: int
    status = cast(TortoiseField[Literal['Analyzing', 'Completed', 'Failed', 'Canceled']],
        fields.CharField(255, db_index=True))
    trigger = cast(TortoiseField[Literal['Automatic', 'Manual', 'Batch']],
        fields.CharField(255))
    # 正常終了時にその実行で検出した CM 区間を保存する
    # 失敗時は None とし、recorded_videos.cm_sections に保持された以前の正常結果とは明確に区別する
    cm_sections = cast(TortoiseField[list[CMSection] | None],
        fields.JSONField(default=None, encoder=lambda x: json.dumps(x, ensure_ascii=False), null=True))  # type: ignore
    # chapter_exe / logoframe / join_logo_scp / dtvindex の終了状態と所要時間を実行順に保持する
    stage_results = cast(TortoiseField[list[CMAnalysisStageResult]],
        fields.JSONField(default=[], encoder=lambda x: json.dumps(x, ensure_ascii=False)))  # type: ignore
    error = cast(TortoiseField[CMAnalysisError | None],
        fields.JSONField(default=None, encoder=lambda x: json.dumps(x, ensure_ascii=False), null=True))  # type: ignore
    started_at = fields.DatetimeField()
    completed_at = cast(TortoiseField[datetime | None], fields.DatetimeField(null=True))
    elapsed_time = cast(TortoiseField[float | None], fields.FloatField(null=True))
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
