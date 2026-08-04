from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class HistoricalRecordingCandidate(BaseModel):
    """過去録画の取り込み元に依存しない共通候補レコード。"""

    model_config = ConfigDict(extra='forbid')

    source_kind: Literal['TVDashboardLog', 'BoxInventory']
    source_record_id: Annotated[str, Field(min_length=1)]
    box_file_id: Annotated[str, Field(pattern=r'^\d+$')]
    box_upload_name: Annotated[str, Field(min_length=1)]
    original_file_path: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1)]
    service_name: Annotated[str, Field(min_length=1)]
    network_id: Annotated[int, Field(ge=0)]
    transport_stream_id: Annotated[int, Field(ge=0)]
    service_id: Annotated[int, Field(ge=0)]
    event_id: Annotated[int, Field(ge=0)]
    start_time: datetime
    duration_seconds: Annotated[float, Field(gt=0)]

    def calculateFingerprint(self) -> str:
        """
        候補レコードを検証結果へ安全に結び付けるフィンガープリントを返す。

        Returns:
            候補レコードの SHA-256 フィンガープリント。
        """

        canonical_json = json.dumps(
            self.model_dump(mode='json'),
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        )
        return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()


class HistoricalImportPlanSummary(BaseModel):
    """取り込み計画を生成した際の件数集計。"""

    model_config = ConfigDict(extra='forbid')

    upload_success_count: Annotated[int, Field(ge=0)]
    eligible_count: Annotated[int, Field(ge=0)]
    excluded_existing_count: Annotated[int, Field(ge=0)]
    incomplete_metadata_count: Annotated[int, Field(ge=0)]
    missing_task_count: Annotated[int, Field(ge=0)]
    duplicate_box_file_id_count: Annotated[int, Field(ge=0)]


class HistoricalImportPlan(BaseModel):
    """Box API へアクセスする前に確定する過去録画の取り込み計画。"""

    model_config = ConfigDict(extra='forbid')

    schema_version: Literal[1] = 1
    generated_at: datetime
    source_fingerprint: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
    summary: HistoricalImportPlanSummary
    candidates: list[HistoricalRecordingCandidate]


class HistoricalBoxMetadata(BaseModel):
    """Box API で存在確認済みの過去録画メタデータ。"""

    model_config = ConfigDict(extra='forbid')

    id: Annotated[str, Field(pattern=r'^\d+$')]
    name: Annotated[str, Field(min_length=1)]
    size: Annotated[int, Field(gt=0)]
    sha1: Annotated[str | None, Field(pattern=r'^[0-9A-Fa-f]{40}$')] = None
    created_at: datetime | None = None
    modified_at: datetime | None = None


class HistoricalVerificationResult(BaseModel):
    """1件の候補に対するBoxメタデータ検証結果。"""

    model_config = ConfigDict(extra='forbid')

    schema_version: Literal[1] = 1
    verified_at: datetime
    candidate_fingerprint: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
    box_file_id: Annotated[str, Field(pattern=r'^\d+$')]
    status: Literal['Verified', 'Rejected']
    metadata: HistoricalBoxMetadata | None = None
    error_code: str | None = None


class HistoricalMediaDefaults(BaseModel):
    """Range解析前の録画に使用する保守的な映像・音声初期値。"""

    model_config = ConfigDict(extra='forbid')

    video_codec: Literal['MPEG-2', 'H.264', 'H.265'] = 'MPEG-2'
    video_codec_profile: Literal['High', 'High 10', 'Main', 'Main 10', 'Baseline', 'Constrained Baseline'] = 'Main'
    video_scan_type: Literal['Interlaced', 'Progressive'] = 'Interlaced'
    video_frame_rate: Annotated[float, Field(gt=0)] = 29.97
    video_resolution_width: Annotated[int, Field(gt=0)] = 1440
    video_resolution_height: Annotated[int, Field(gt=0)] = 1080
    primary_audio_channel: Literal['Monaural', 'Stereo', '5.1ch'] = 'Stereo'
    primary_audio_sampling_rate: Annotated[int, Field(gt=0)] = 48000


class BoxUsageLedger(BaseModel):
    """過去録画取り込みが消費したBox通信量の永続台帳。"""

    model_config = ConfigDict(extra='forbid')

    schema_version: Literal[1] = 1
    batch_id: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
    api_request_count: Annotated[int, Field(ge=0)] = 0
    response_bytes: Annotated[int, Field(ge=0)] = 0
    downloaded_bytes: Annotated[int, Field(ge=0)] = 0
    reserved_download_bytes: Annotated[int, Field(ge=0)] = 0
    rate_limit_response_count: Annotated[int, Field(ge=0)] = 0
    updated_at: datetime
