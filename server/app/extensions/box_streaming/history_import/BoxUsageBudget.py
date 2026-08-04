from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import ValidationError

from app.extensions.box_streaming.history_import.Models import BoxUsageLedger


class BoxUsageBudgetError(Exception):
    """Box通信量の安全上限到達、台帳破損、または429発生時の例外。"""


class BoxUsageBudget:
    """Box APIリクエストとダウンロード量を計測し、安全上限で停止する。"""

    def __init__(
        self,
        ledger_path: Path,
        batch_id: str,
        max_api_requests: int,
        max_download_bytes: int,
        minimum_request_interval_seconds: float,
    ) -> None:
        """
        Args:
            ledger_path: 再実行時にも引き継ぐ使用量台帳の保存先。
            batch_id: 取り込み計画を識別するSHA-256値。
            max_api_requests: このバッチが発行できるBox APIリクエスト総数。
            max_download_bytes: このバッチが受信できるBoxコンテンツ総量。
            minimum_request_interval_seconds: Box APIリクエスト間の最小待機秒数。

        Raises:
            BoxUsageBudgetError: 上限値または既存台帳が不正な場合。
        """

        if max_api_requests <= 0:
            raise BoxUsageBudgetError('Maximum Box API requests must be greater than zero.')
        if max_download_bytes < 0:
            raise BoxUsageBudgetError('Maximum Box download bytes must not be negative.')
        if minimum_request_interval_seconds < 0:
            raise BoxUsageBudgetError('Minimum Box request interval must not be negative.')

        # 台帳はBox認証情報や番組名を含まず、同じ計画を再開した際の累積使用量だけを保持する
        self._ledger_path = ledger_path
        self._batch_id = batch_id
        self._max_api_requests = max_api_requests
        self._max_download_bytes = max_download_bytes
        self._minimum_request_interval_seconds = minimum_request_interval_seconds
        self._ledger = self._loadLedger()
        self._request_lock = asyncio.Lock()
        self._ledger_lock = asyncio.Lock()
        self._last_api_request_at = 0.0

        if self._ledger.api_request_count > max_api_requests:
            raise BoxUsageBudgetError('Existing Box API request usage already exceeds the configured limit.')
        if self._ledger.downloaded_bytes + self._ledger.reserved_download_bytes > max_download_bytes:
            raise BoxUsageBudgetError('Existing Box download usage already exceeds the configured limit.')
        # 初回作成と、前回異常終了時の予約量を保守的に消費へ振り替えた結果を直ちに永続化する
        self._persistLedger()

    @property
    def ledger(self) -> BoxUsageLedger:
        """現在の使用量台帳を返す。"""

        return self._ledger.model_copy(deep=True)

    async def onRequest(self, request: httpx.Request) -> None:
        """
        httpxの送信直前にAPI回数を予約し、必要な間隔を空ける。

        Args:
            request: 送信予定のHTTPリクエスト。

        Raises:
            BoxUsageBudgetError: 上限到達、またはRangeなしの全量取得を検出した場合。
        """

        if request.url.host != 'api.box.com':
            return

        # 過去録画取り込みからの全量ダウンロードは、通信量上限を迂回しうるため常に拒否する
        if request.method == 'GET' and request.url.path.endswith('/content') and 'Range' not in request.headers:
            raise BoxUsageBudgetError('A Box content request without a Range header was blocked.')

        # 連続リクエストがBoxの一時的なレート制限を誘発しないよう、直列でペースを制御する
        async with self._request_lock:
            elapsed = time.monotonic() - self._last_api_request_at
            wait_seconds = self._minimum_request_interval_seconds - elapsed
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)

            async with self._ledger_lock:
                if self._ledger.api_request_count >= self._max_api_requests:
                    raise BoxUsageBudgetError('The configured Box API request limit was reached.')
                # 送信中にプロセスが終了しても過少計上しないよう、実リクエストより先に台帳へ記録する
                self._ledger.api_request_count += 1
                self._ledger.updated_at = datetime.now(UTC)
                self._persistLedger()
            self._last_api_request_at = time.monotonic()

    async def onResponse(self, response: httpx.Response) -> None:
        """
        httpxのレスポンスヘッダーから通信量と429を記録する。

        Args:
            response: BoxまたはダウンロードCDNから返されたHTTPレスポンス。

        Raises:
            BoxUsageBudgetError: Boxが429を返した場合。
        """

        content_length = self._parseContentLength(response.headers.get('Content-Length'))
        async with self._ledger_lock:
            # バイナリ本体はRangeSamplerが実読込量を記録するため、ここではHTTP応答全体の参考値だけを保持する
            if content_length is not None:
                self._ledger.response_bytes += content_length
            if response.status_code == 429:
                self._ledger.rate_limit_response_count += 1
            self._ledger.updated_at = datetime.now(UTC)
            self._persistLedger()

        # 一度でも429になったら自動再試行せず停止し、同じ台帳からユーザー判断で再開できるようにする
        if response.status_code == 429:
            retry_after = response.headers.get('Retry-After', 'unknown')
            raise BoxUsageBudgetError(
                f'Box returned HTTP 429. Import was stopped. [retry_after_seconds: {retry_after}]'
            )

    async def reserveDownloadBytes(self, requested_bytes: int) -> None:
        """
        Range取得を始める前に最大受信量を予約する。

        Args:
            requested_bytes: Rangeレスポンスとして要求する最大バイト数。

        Raises:
            BoxUsageBudgetError: ダウンロード上限を超える場合。
        """

        if requested_bytes <= 0:
            raise BoxUsageBudgetError('Reserved Box download bytes must be greater than zero.')
        async with self._ledger_lock:
            projected_bytes = (
                self._ledger.downloaded_bytes
                + self._ledger.reserved_download_bytes
                + requested_bytes
            )
            if projected_bytes > self._max_download_bytes:
                raise BoxUsageBudgetError('The configured Box download byte limit would be exceeded.')
            self._ledger.reserved_download_bytes += requested_bytes
            self._ledger.updated_at = datetime.now(UTC)
            self._persistLedger()

    async def completeDownload(self, reserved_bytes: int, actual_bytes: int) -> None:
        """
        完了したRange取得の予約量を実受信量へ振り替える。

        Args:
            reserved_bytes: reserveDownloadBytes()で予約したバイト数。
            actual_bytes: 実際に読み込んだバイト数。

        Raises:
            BoxUsageBudgetError: 予約量より多く受信した場合。
        """

        if actual_bytes < 0 or actual_bytes > reserved_bytes:
            raise BoxUsageBudgetError('Box returned more bytes than the reserved Range size.')
        async with self._ledger_lock:
            self._ledger.reserved_download_bytes = max(
                0,
                self._ledger.reserved_download_bytes - reserved_bytes,
            )
            self._ledger.downloaded_bytes += actual_bytes
            self._ledger.updated_at = datetime.now(UTC)
            self._persistLedger()

    async def cancelDownloadReservation(self, reserved_bytes: int) -> None:
        """
        Range取得に失敗した際、未使用の予約量を解放する。

        Args:
            reserved_bytes: 解放する予約バイト数。
        """

        async with self._ledger_lock:
            self._ledger.reserved_download_bytes = max(
                0,
                self._ledger.reserved_download_bytes - max(0, reserved_bytes),
            )
            self._ledger.updated_at = datetime.now(UTC)
            self._persistLedger()

    def _loadLedger(self) -> BoxUsageLedger:
        """
        既存台帳を検証して読み込み、未作成なら空の台帳を返す。

        Returns:
            検証済み使用量台帳。

        Raises:
            BoxUsageBudgetError: 台帳が壊れている、または別バッチの台帳だった場合。
        """

        if self._ledger_path.exists() is False:
            return BoxUsageLedger(batch_id=self._batch_id, updated_at=datetime.now(UTC))
        try:
            ledger = BoxUsageLedger.model_validate_json(self._ledger_path.read_text(encoding='utf-8'))
        except (OSError, ValidationError):
            raise BoxUsageBudgetError('Box usage ledger could not be read safely.') from None
        if ledger.batch_id != self._batch_id:
            raise BoxUsageBudgetError('Box usage ledger belongs to a different import plan.')
        # 前回の異常終了時に残った予約量は実際には未使用か判定できないため、保守的に消費済みへ振り替える
        if ledger.reserved_download_bytes > 0:
            ledger.downloaded_bytes += ledger.reserved_download_bytes
            ledger.reserved_download_bytes = 0
            ledger.updated_at = datetime.now(UTC)
        return ledger

    def _persistLedger(self) -> None:
        """一時ファイルとfsyncを使い、使用量台帳を原子的に保存する。"""

        self._ledger_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary_path = self._ledger_path.with_name(f'.{self._ledger_path.name}.tmp-{os.getpid()}')
        try:
            descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(descriptor, 'w', encoding='utf-8') as temporary_file:
                temporary_file.write(self._ledger.model_dump_json(indent=2))
                temporary_file.write('\n')
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            temporary_path.replace(self._ledger_path)
            os.chmod(self._ledger_path, 0o600)
            directory_descriptor = os.open(self._ledger_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    @staticmethod
    def _parseContentLength(value: str | None) -> int | None:
        """
        Content-Lengthを安全に整数へ変換する。

        Args:
            value: HTTPレスポンスヘッダー値。

        Returns:
            0以上の値、または不正・未指定ならNone。
        """

        if value is None:
            return None
        try:
            parsed = int(value, 10)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
