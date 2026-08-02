from __future__ import annotations

import os

from fastapi import HTTPException

from app import logging
from app.extensions.box_streaming.BoxRecordingCatalog import BoxRecordingCatalog
from app.extensions.box_streaming.BoxStreamsRouter import CloseBoxClient, GetBoxClient


async def InitializeBoxStreamingExtension() -> None:
    """Box 録画拡張の永続テーブル・初回同期・定期同期を開始する。"""

    is_configured = bool(os.environ.get('KONOMITV_BOX_CREDENTIALS_PATH', '').strip())
    if is_configured is False:
        await BoxRecordingCatalog.initialize(None)
        logging.info('[BoxStreaming] Box recording extension is disabled because credentials are not configured.')
        return

    try:
        client = GetBoxClient()
    except HTTPException:
        await BoxRecordingCatalog.initialize(None)
        logging.error('[BoxStreaming] Box recording extension configuration is invalid.')
        return
    await BoxRecordingCatalog.initialize(client)


async def ShutdownBoxStreamingExtension() -> None:
    """Box 録画拡張の定期同期と HTTP クライアントを停止する。"""

    await BoxRecordingCatalog.shutdown()
    await CloseBoxClient()
