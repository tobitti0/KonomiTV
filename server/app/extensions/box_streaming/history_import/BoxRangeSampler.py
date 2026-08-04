from __future__ import annotations

from dataclasses import dataclass

from app.extensions.box_streaming.BoxClient import BoxClient, BoxFileMetadata
from app.extensions.box_streaming.history_import.BoxUsageBudget import (
    BoxUsageBudget,
    BoxUsageBudgetError,
)


@dataclass(frozen=True, slots=True)
class BoxRangeSample:
    """TS全体を取得せず、先頭と末尾だけを保持する解析用サンプル。"""

    head: bytes
    tail: bytes
    file_size: int


class BoxRangeSampler:
    """将来のBoxフォルダ由来取り込みで使う、予算管理付きRange取得器。"""

    def __init__(self, client: BoxClient, usage_budget: BoxUsageBudget) -> None:
        """
        Args:
            client: 既存のEnterprise Service Account認証を使うBoxクライアント。
            usage_budget: リクエスト数と受信バイト数を制限する安全予算。
        """

        # BoxClientは認証と許可IDを、usage_budgetは一度きり処理の通信量上限をそれぞれ担当する
        self._client = client
        self._usage_budget = usage_budget

    async def sample(
        self,
        metadata: BoxFileMetadata,
        head_bytes: int,
        tail_bytes: int,
    ) -> BoxRangeSample:
        """
        Box上のTSから重複しない先頭・末尾Rangeを取得する。

        Args:
            metadata: 先に検証済みのBoxファイルメタデータ。
            head_bytes: 先頭から取得する最大バイト数。
            tail_bytes: 末尾から取得する最大バイト数。

        Returns:
            普通の番組メタデータ解析へ渡せる先頭・末尾サンプル。

        Raises:
            BoxUsageBudgetError: サイズ不正、全量応答、または通信量上限到達時。
        """

        if metadata.size <= 0:
            raise BoxUsageBudgetError('Box file size must be greater than zero for Range sampling.')
        if head_bytes <= 0 or tail_bytes < 0:
            raise BoxUsageBudgetError('Box Range sample sizes are invalid.')

        self._client.registerDiscoveredFile(metadata.id)
        head_length = min(head_bytes, metadata.size)
        head = await self._readRange(metadata.id, 0, head_length - 1)

        # 小さいファイルで同じ領域を二重取得しないよう、先頭Rangeの直後から末尾側だけを取得する
        tail_start = max(head_length, metadata.size - tail_bytes)
        if tail_bytes == 0 or tail_start >= metadata.size:
            tail = b''
        else:
            tail = await self._readRange(metadata.id, tail_start, metadata.size - 1)
        return BoxRangeSample(head=head, tail=tail, file_size=metadata.size)

    async def _readRange(self, file_id: str, start: int, end: int) -> bytes:
        """
        1つのRangeを上限より多く読み込まないよう検証しながら取得する。

        Args:
            file_id: Box file ID。
            start: 先頭バイト位置。
            end: 末尾バイト位置（含む）。

        Returns:
            取得したRangeバイト列。

        Raises:
            BoxUsageBudgetError: Boxが部分応答を返さない、またはサイズが一致しない場合。
        """

        expected_bytes = end - start + 1
        if start < 0 or end < start:
            raise BoxUsageBudgetError('Invalid Box byte Range was requested.')
        await self._usage_budget.reserveDownloadBytes(expected_bytes)
        try:
            async with self._client.openFileStream(file_id, f'bytes={start}-{end}') as response:
                # 200応答を許すと巨大TS全体を誤って読み始めるため、部分応答以外は本文を読まずに停止する
                if response.status_code != 206:
                    raise BoxUsageBudgetError('Box did not honor the requested byte Range.')
                received = bytearray()
                async for chunk in response.aiter_bytes():
                    received.extend(chunk)
                    if len(received) > expected_bytes:
                        raise BoxUsageBudgetError('Box returned more data than the requested byte Range.')
            if len(received) != expected_bytes:
                raise BoxUsageBudgetError('Box returned an incomplete byte Range.')
        except Exception:
            await self._usage_budget.cancelDownloadReservation(expected_bytes)
            raise

        await self._usage_budget.completeDownload(expected_bytes, len(received))
        return bytes(received)
