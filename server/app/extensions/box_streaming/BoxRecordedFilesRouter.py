from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field

from app.extensions.box_streaming.BoxClient import BoxAPIError
from app.extensions.box_streaming.BoxRecordingCatalog import (
    BoxRecordedFile,
    BoxRecordingCatalog,
    PendingBoxRecording,
)
from app.extensions.box_streaming.BoxStreamsRouter import (
    GetBoxClient,
    RaiseBoxHTTPException,
)
from app.models.RecordedProgram import RecordedProgram
from app.models.User import User
from app.routers.UsersRouter import GetCurrentAdminUser


router = APIRouter(
    tags = ['Box Recording Extension'],
    prefix = '/api/extensions/box/recordings',
)


class BoxRecordingLinkRequest(BaseModel):
    """Box ファイルを録画番組へ手動で紐付けるリクエスト。"""

    model_config = ConfigDict(extra='forbid')

    file_id: str = Field(pattern=r'^\d+$')


class BoxRecordingRegistrationRequest(BaseModel):
    """TVDashBoard がアップロード完了時に送る1ファイル分の通知。"""

    model_config = ConfigDict(extra='forbid')

    file_id: str = Field(pattern=r'^\d+$')
    original_file_name: str | None = None
    original_file_size: int | None = Field(default=None, ge=1)


class BoxRecordingLinkResponse(BaseModel):
    """録画番組と Box ファイルの紐付け情報。"""

    recorded_program_id: int
    box_file_id: str
    name: str
    size: int
    availability: str
    match_method: str
    last_synced_at: str

    @classmethod
    def fromCatalog(cls, mapping: BoxRecordedFile) -> BoxRecordingLinkResponse:
        """
        カタログ内のデータクラスを API レスポンスへ変換する。

        Args:
            mapping: 変換対象の Box 紐付け。

        Returns:
            API 用の紐付け情報。
        """

        return cls(
            recorded_program_id = mapping.recorded_program_id,
            box_file_id = mapping.box_file_id,
            name = mapping.name,
            size = mapping.size,
            availability = mapping.availability,
            match_method = mapping.match_method,
            last_synced_at = mapping.last_synced_at,
        )


class BoxRecordingSyncResponse(BaseModel):
    """Box 録画同期の集計結果。"""

    found: int
    linked: int
    unmatched: int


class BoxRecordingRegistrationResponse(BaseModel):
    """TVDashBoard から受けた録画通知の受付結果。"""

    status: Literal['Linked', 'Pending']
    box_file_id: str
    recorded_program_id: int | None = None
    match_method: str | None = None

    @classmethod
    def fromCatalog(cls, mapping: BoxRecordedFile) -> BoxRecordingRegistrationResponse:
        """
        保存済み紐付けをアップロード通知レスポンスへ変換する。

        Args:
            mapping: Box 録画紐付け。

        Returns:
            紐付け完了レスポンス。
        """

        return cls(
            status = 'Linked',
            box_file_id = mapping.box_file_id,
            recorded_program_id = mapping.recorded_program_id,
            match_method = mapping.match_method,
        )


class PendingBoxRecordingResponse(BaseModel):
    """解析済みローカル録画との一致を待っている Box 録画通知。"""

    box_file_id: str
    name: str
    size: int
    original_file_name: str | None
    original_file_size: int | None
    received_at: str

    @classmethod
    def fromCatalog(cls, pending: PendingBoxRecording) -> PendingBoxRecordingResponse:
        """
        保留データを API レスポンスへ変換する。

        Args:
            pending: 変換対象の保留通知。

        Returns:
            API 用の保留通知。
        """

        return cls(
            box_file_id = pending.box_file_id,
            name = pending.name,
            size = pending.size,
            original_file_name = pending.original_file_name,
            original_file_size = pending.original_file_size,
            received_at = pending.received_at,
        )


@router.post(
    '/register',
    summary = 'Box 録画アップロード完了通知 API',
    response_model = BoxRecordingRegistrationResponse,
)
async def BoxRecordingRegistrationAPI(
    request: BoxRecordingRegistrationRequest,
    registration_token: Annotated[str, Header(alias='X-KonomiTV-Box-Token')],
) -> BoxRecordingRegistrationResponse:
    """
    TVDashBoard から1件のアップロード完了通知を受け、解析済みの既存録画番組へ紐付ける。

    Args:
        request: Box file ID とアップロード元のファイル情報。
        registration_token: Box Client Secret からHMACで派生した通知専用トークン。

    Returns:
        保存した Box 録画紐付け。
    """

    client = GetBoxClient()
    if client.verifyRegistrationToken(registration_token) is False:
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail = 'Invalid Box recording registration token',
        )

    existing_mapping = BoxRecordingCatalog.getByBoxFileID(request.file_id)
    if existing_mapping is not None:
        return BoxRecordingRegistrationResponse.fromCatalog(existing_mapping)

    # 正しく認証された TVDashBoard が通知した ID だけを、実行中の許可リストへ追加する
    client.registerDiscoveredFile(request.file_id)
    try:
        metadata = await client.getFileMetadata(request.file_id)
    except BoxAPIError as error:
        RaiseBoxHTTPException(error)
    if metadata.name.lower().endswith('.ts') is False:
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Only MPEG-TS recordings can be registered',
        )

    match = await BoxRecordingCatalog.findSafeAutomaticMatch(
        metadata,
        request.original_file_name,
        request.original_file_size,
    )
    if match is None:
        await BoxRecordingCatalog.queuePending(
            metadata,
            request.original_file_name,
            request.original_file_size,
        )
        return BoxRecordingRegistrationResponse(status='Pending', box_file_id=metadata.id)
    recorded_program_id, match_method = match
    mapping = await BoxRecordingCatalog.link(recorded_program_id, metadata, f'tvdashboard-{match_method}')
    return BoxRecordingRegistrationResponse.fromCatalog(mapping)


@router.get(
    '/pending',
    summary = 'Box 録画紐付け待ち一覧 API',
    response_model = list[PendingBoxRecordingResponse],
)
async def PendingBoxRecordingsAPI(
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> list[PendingBoxRecordingResponse]:
    """
    KonomiTV のローカル解析完了または管理者の手動紐付けを待っている通知を一覧する。

    Args:
        current_user: 認証済み管理者ユーザー。

    Returns:
        保留通知一覧。
    """

    return [PendingBoxRecordingResponse.fromCatalog(pending) for pending in await BoxRecordingCatalog.getPending()]


@router.get(
    '',
    summary = 'Box 録画紐付け一覧 API',
    response_model = list[BoxRecordingLinkResponse],
)
async def BoxRecordingLinksAPI(
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> list[BoxRecordingLinkResponse]:
    """
    KonomiTV 内に保存された Box 録画紐付けを一覧する。

    Args:
        current_user: 認証済み管理者ユーザー。

    Returns:
        Box 録画紐付け一覧。
    """

    return [BoxRecordingLinkResponse.fromCatalog(mapping) for mapping in BoxRecordingCatalog.getAll()]


@router.post(
    '/sync',
    summary = 'Box 録画同期 API',
    response_model = BoxRecordingSyncResponse,
)
async def BoxRecordingSyncAPI(
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> BoxRecordingSyncResponse:
    """
    設定済みファイル ID とフォルダから Box 録画を同期する。

    Args:
        current_user: 認証済み管理者ユーザー。

    Returns:
        同期の集計結果。
    """

    result = await BoxRecordingCatalog.syncConfiguredFiles(GetBoxClient())
    return BoxRecordingSyncResponse.model_validate(result)


@router.get(
    '/{video_id}',
    summary = '録画番組の Box 紐付け取得 API',
    response_model = BoxRecordingLinkResponse | None,
)
async def BoxRecordingLinkDetailAPI(
    video_id: Annotated[int, Path(description='録画番組の ID。')],
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> BoxRecordingLinkResponse | None:
    """
    指定した録画番組の Box 紐付けを KonomiTV 内の DB から取得する。

    Box API への問い合わせやフォルダ探索は行わない。

    Args:
        video_id: KonomiTV の録画番組 ID。
        current_user: 認証済み管理者ユーザー。

    Returns:
        Box 紐付け。未登録の場合は None。
    """

    mapping = BoxRecordingCatalog.get(video_id)
    return BoxRecordingLinkResponse.fromCatalog(mapping) if mapping is not None else None


@router.put(
    '/{video_id}',
    summary = 'Box 録画手動紐付け API',
    response_model = BoxRecordingLinkResponse,
)
async def BoxRecordingLinkAPI(
    video_id: Annotated[int, Path(description='録画番組の ID。')],
    request: BoxRecordingLinkRequest,
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
) -> BoxRecordingLinkResponse:
    """
    管理者が明示した Box ファイルを既存録画番組へ紐付ける。

    Args:
        video_id: KonomiTV の録画番組 ID。
        request: Box ファイル ID。
        current_user: 認証済み管理者ユーザー。

    Returns:
        保存した Box 録画紐付け。
    """

    if await RecordedProgram.filter(id=video_id).exists() is False:
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified video_id was not found',
        )
    client = GetBoxClient()
    # 管理者がUIで明示した数値IDだけを許可し、フォルダ探索なしで対象1件のメタデータを確認する
    client.registerDiscoveredFile(request.file_id)
    try:
        metadata = await client.getFileMetadata(request.file_id)
    except BoxAPIError as error:
        RaiseBoxHTTPException(error)
    if metadata.name.lower().endswith('.ts') is False:
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Only MPEG-TS recordings can be linked',
        )
    mapping = await BoxRecordingCatalog.link(video_id, metadata, 'manual')
    return BoxRecordingLinkResponse.fromCatalog(mapping)


@router.delete(
    '/{video_id}',
    summary = 'Box 録画紐付け解除 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def BoxRecordingUnlinkAPI(
    video_id: Annotated[int, Path(description='録画番組の ID。')],
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """
    KonomiTV 内の紐付けだけを解除し、Box 上のファイルは保持する。

    Args:
        video_id: KonomiTV の録画番組 ID。
        current_user: 認証済み管理者ユーザー。
    """

    await BoxRecordingCatalog.unlink(video_id)
