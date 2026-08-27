from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, NoReturn
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Path, Request, Response, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.extensions.box_streaming.BoxClient import (
    BoxAPIError,
    BoxClient,
    BoxConfigurationError,
    BoxFileMetadata,
)


router = APIRouter(
    tags = ['Streams'],
    prefix = '/api/streams/box',
)

BOX_CLIENT: BoxClient | None = None
BOX_FILE_ID_PATH = Path(pattern=r'^\d+$', description='Box のファイル ID。')


def LogError(message: str) -> None:
    """
    KonomiTV 本体のロガーへエラーメッセージを出力する。

    Box 拡張の import だけでは KonomiTV の設定をロードしないよう、ロガーを遅延 import する。

    Args:
        message: 出力する英語のエラーメッセージ。
    """

    from app import logging

    logging.error(message)


def GetBoxClient() -> BoxClient:
    """
    環境変数と Docker Secret から生成した Box クライアントを取得する。

    Returns:
        プロセス内で共有する Box クライアント。

    Raises:
        HTTPException: Box 連携設定が不足している、または不正な場合。
    """

    global BOX_CLIENT
    if BOX_CLIENT is None:
        try:
            BOX_CLIENT = BoxClient.fromEnvironment()
        except BoxConfigurationError:
            # 設定ファイルの内容や秘密値につながる情報はログへ出力しない
            LogError('[BoxStreaming] Box integration is not configured correctly.')
            raise HTTPException(
                status_code = status.HTTP_503_SERVICE_UNAVAILABLE,
                detail = 'Box integration is not configured correctly',
            ) from None
    return BOX_CLIENT


async def CloseBoxClient() -> None:
    """プロセス内で共有している Box API クライアントを閉じる。"""

    global BOX_CLIENT
    if BOX_CLIENT is not None:
        await BOX_CLIENT.close()
        BOX_CLIENT = None


def AssertBoxFileAllowed(client: BoxClient, file_id: str) -> None:
    """
    Box ファイル ID が明示的な許可リストに含まれることを確認する。

    Args:
        client: Box API クライアント。
        file_id: Box ファイル ID。

    Raises:
        HTTPException: ファイル ID が許可リストに含まれない場合。
    """

    try:
        client.assertFileAllowed(file_id)
    except PermissionError:
        # 許可されていない ID が Box 上に存在するかどうかも外部には開示しない
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail = 'Specified Box file was not found',
        ) from None


def RaiseBoxHTTPException(error: BoxAPIError) -> NoReturn:
    """
    Box API のエラーを KonomiTV の HTTP エラーへ変換する。

    Args:
        error: Box API クライアントが返した例外。

    Raises:
        HTTPException: Box API のステータスに対応する HTTP エラー。
    """

    LogError(
        f'[BoxStreaming] Box API request failed. [status_code: {error.status_code}][code: {error.code}]',
    )
    if error.status_code == 404:
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail = 'Specified Box file was not found',
        ) from None
    if error.status_code == 416:
        raise HTTPException(
            status_code = status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail = 'Requested byte range is not satisfiable',
        ) from None
    if error.status_code == 429:
        raise HTTPException(
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE,
            detail = 'Box API is temporarily rate limited',
        ) from None
    raise HTTPException(
        status_code = status.HTTP_502_BAD_GATEWAY,
        detail = 'Box API request failed',
    ) from None


@router.get(
    '/{file_id}/metadata',
    summary = 'Box 録画ファイルメタデータ取得 API',
    response_model = BoxFileMetadata,
)
async def BoxFileMetadataAPI(
    file_id: Annotated[str, BOX_FILE_ID_PATH],
) -> BoxFileMetadata:
    """
    明示的に許可された Box ファイルのメタデータを取得する。

    Args:
        file_id: Box ファイル ID。

    Returns:
        Box ファイルのメタデータ。
    """

    client = GetBoxClient()
    AssertBoxFileAllowed(client, file_id)
    try:
        return await client.getFileMetadata(file_id)
    except BoxAPIError as error:
        RaiseBoxHTTPException(error)


@router.head(
    '/{file_id}/mpegts',
    summary = 'Box 録画ファイルストリーム情報取得 API',
)
async def BoxFileStreamHeadAPI(
    file_id: Annotated[str, BOX_FILE_ID_PATH],
) -> Response:
    """
    明示的に許可された Box ファイルのサイズと Range 対応状況を返す。

    Args:
        file_id: Box ファイル ID。

    Returns:
        MPEG-TS ストリームのヘッダー情報。
    """

    client = GetBoxClient()
    AssertBoxFileAllowed(client, file_id)
    try:
        metadata = await client.getFileMetadata(file_id)
    except BoxAPIError as error:
        RaiseBoxHTTPException(error)

    headers = {
        'Accept-Ranges': 'bytes',
        'Content-Length': str(metadata.size),
        'Content-Type': 'video/mp2t',
    }
    if metadata.sha1 is not None:
        headers['ETag'] = f'"{metadata.sha1}"'
    return Response(status_code=status.HTTP_200_OK, headers=headers)


@router.get(
    '/{file_id}/mpegts',
    summary = 'Box 録画ファイルストリーミング API',
)
async def BoxFileStreamAPI(
    request: Request,
    file_id: Annotated[str, BOX_FILE_ID_PATH],
) -> StreamingResponse:
    """
    明示的に許可された Box ファイルを Range 対応の MPEG-TS として中継する。

    Args:
        request: クライアントからの HTTP リクエスト。
        file_id: Box ファイル ID。

    Returns:
        Box API から逐次転送する MPEG-TS ストリーム。
    """

    return await CreateBoxFileStreamingResponse(request, file_id)


async def CreateBoxFileStreamingResponse(
    request: Request,
    file_id: str,
    download_filename: str | None = None,
) -> StreamingResponse:
    """
    Box ファイルを Range 対応で中継する共通レスポンスを生成する。

    Args:
        request: クライアントからの HTTP リクエスト。
        file_id: Box ファイル ID。
        download_filename: ダウンロードとして扱う場合のファイル名。

    Returns:
        Box API から逐次転送する MPEG-TS ストリーム。
    """

    client = GetBoxClient()
    AssertBoxFileAllowed(client, file_id)
    stream_context = client.openFileStream(file_id, request.headers.get('range'))
    try:
        upstream_response = await stream_context.__aenter__()
    except BoxAPIError as error:
        RaiseBoxHTTPException(error)

    response_headers = {'Accept-Ranges': 'bytes'}
    for header_name in ('Content-Range', 'Content-Length', 'ETag', 'Last-Modified'):
        header_value = upstream_response.headers.get(header_name)
        if header_value is not None:
            response_headers[header_name] = header_value
    if download_filename is not None:
        response_headers['Content-Disposition'] = (
            f"attachment; filename*=UTF-8''{quote(download_filename, safe='')}"
        )

    is_stream_closed = False

    async def CloseBoxStream() -> None:
        """Box API のレスポンスストリームを最初の1回だけ閉じる。"""
        nonlocal is_stream_closed
        # 応答本文の finally と StreamingResponse の終了処理から重複して呼ばれるため、最初の1回だけ閉じる
        if is_stream_closed is True:
            return
        is_stream_closed = True
        await stream_context.__aexit__(None, None, None)

    async def StreamBoxFile() -> AsyncIterator[bytes]:
        """
        Box API のレスポンスをチャンク単位で転送する。

        Yields:
            Box API から受信した MPEG-TS のバイト列。
        """

        try:
            async for chunk in upstream_response.aiter_bytes(chunk_size=1024 * 1024):
                yield chunk
        finally:
            await CloseBoxStream()

    return StreamingResponse(
        StreamBoxFile(),
        status_code = upstream_response.status_code,
        headers = response_headers,
        media_type = 'video/mp2t',
        # クライアントが応答開始直後に切断し、ジェネレーター本体が一度も実行されない場合も必ず閉じる
        background = BackgroundTask(CloseBoxStream),
    )
