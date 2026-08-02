from __future__ import annotations

import io
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError

from app.extensions.box_streaming.BoxClient import (
    BOX_API_BASE_URL,
    BOX_TOKEN_URL,
    BoxAPIError,
    BoxClient,
    BoxCredentials,
)
from app.extensions.box_streaming.BoxRecordingCatalog import BoxRecordingCatalog


class _SyncBoxTokenProvider:
    """同期 Range リーダーから共有する Enterprise CCG トークンキャッシュ。"""

    def __init__(self, credentials: BoxCredentials) -> None:
        """
        Args:
            credentials: Enterprise CCG の資格情報。
        """

        self._credentials = credentials
        self._access_token: SecretStr | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def getAccessToken(self, force_refresh: bool = False) -> str:
        """
        有効なアクセストークンを返し、必要な場合だけ同期的に再取得する。

        Args:
            force_refresh: キャッシュを無視して再認証するか。

        Returns:
            Box API アクセストークン。
        """

        if (
            force_refresh is False
            and self._access_token is not None
            and time.monotonic() < self._expires_at
        ):
            return self._access_token.get_secret_value()

        with self._lock:
            if (
                force_refresh is False
                and self._access_token is not None
                and time.monotonic() < self._expires_at
            ):
                return self._access_token.get_secret_value()
            with httpx.Client(timeout=20.0) as client:
                response = client.post(
                    BOX_TOKEN_URL,
                    headers = {'Accept': 'application/json'},
                    data = {
                        'grant_type': 'client_credentials',
                        'client_id': self._credentials.client_id,
                        'client_secret': self._credentials.client_secret.get_secret_value(),
                        'box_subject_type': 'enterprise',
                        'box_subject_id': self._credentials.enterprise_id,
                    },
                )
            if response.status_code >= 400:
                raise BoxAPIError(response.status_code, 'token_request_failed')
            try:
                response_data = response.json()
                access_token = SecretStr(str(response_data['access_token']))
                expires_in = int(response_data['expires_in'])
            except (KeyError, TypeError, ValueError, ValidationError):
                raise BoxAPIError(response.status_code, 'invalid_token_response') from None
            self._access_token = access_token
            self._expires_at = time.monotonic() + expires_in - min(60, expires_in / 10)
            return access_token.get_secret_value()


_TOKEN_PROVIDER: _SyncBoxTokenProvider | None = None
_TOKEN_PROVIDER_LOCK = threading.Lock()


def _GetTokenProvider() -> _SyncBoxTokenProvider:
    """Docker Secret から生成した同期トークンプロバイダーをプロセス内で共有する。"""

    global _TOKEN_PROVIDER
    if _TOKEN_PROVIDER is not None:
        return _TOKEN_PROVIDER
    with _TOKEN_PROVIDER_LOCK:
        if _TOKEN_PROVIDER is None:
            credentials = BoxClient.loadCredentialsFromEnvironment()
            _TOKEN_PROVIDER = _SyncBoxTokenProvider(credentials)
    return _TOKEN_PROVIDER


class BoxRandomAccessFile(io.RawIOBase):
    """Box の HTTP Range を seek/read 可能な同期バイナリファイルとして公開する。"""

    DEFAULT_BUFFER_SIZE = 4 * 1024 * 1024

    def __init__(self, file_id: str, file_size: int, buffer_size: int = DEFAULT_BUFFER_SIZE) -> None:
        """
        Args:
            file_id: Box ファイル ID。
            file_size: Box メタデータから得たファイルサイズ。
            buffer_size: 1 回の Range 取得で先読みするバイト数。
        """

        super().__init__()
        self._file_id = file_id
        self._file_size = file_size
        self._buffer_size = max(1024 * 1024, buffer_size)
        self._position = 0
        self._buffer_start = 0
        self._buffer = b''
        self._client = httpx.Client(
            follow_redirects = True,
            timeout = httpx.Timeout(connect=20.0, read=60.0, write=20.0, pool=20.0),
        )
        self._token_provider = _GetTokenProvider()

    def readable(self) -> bool:
        """読み取り可能であることを返す。"""

        return True

    def seekable(self) -> bool:
        """任意位置へシーク可能であることを返す。"""

        return True

    def tell(self) -> int:
        """現在の読み取り位置を返す。"""

        return self._position

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        """
        読み取り位置を移動する。

        Args:
            offset: 基準位置からのバイトオフセット。
            whence: os.SEEK_SET / SEEK_CUR / SEEK_END のいずれか。

        Returns:
            移動後のファイル位置。
        """

        self._checkClosed()
        if whence == os.SEEK_SET:
            new_position = offset
        elif whence == os.SEEK_CUR:
            new_position = self._position + offset
        elif whence == os.SEEK_END:
            new_position = self._file_size + offset
        else:
            raise ValueError(f'Unsupported whence: {whence}')
        if new_position < 0:
            raise ValueError('Negative seek position is not allowed.')
        self._position = min(new_position, self._file_size)
        return self._position

    def read(self, size: int = -1) -> bytes:
        """
        現在位置から指定バイト数を読み取る。

        Args:
            size: 読み取る最大バイト数。負数の場合は末尾まで。

        Returns:
            読み取ったバイト列。
        """

        self._checkClosed()
        if self._position >= self._file_size or size == 0:
            return b''
        if size < 0:
            size = self._file_size - self._position

        remaining = min(size, self._file_size - self._position)
        chunks: list[bytes] = []
        while remaining > 0:
            if not (self._buffer_start <= self._position < self._buffer_start + len(self._buffer)):
                self._fillBuffer(max(self._buffer_size, min(remaining, self._buffer_size * 2)))
            buffer_offset = self._position - self._buffer_start
            available = min(remaining, len(self._buffer) - buffer_offset)
            if available <= 0:
                break
            chunks.append(self._buffer[buffer_offset:buffer_offset + available])
            self._position += available
            remaining -= available
        return b''.join(chunks)

    def close(self) -> None:
        """HTTP クライアントとファイルオブジェクトを閉じる。"""

        if self.closed is False:
            self._client.close()
        super().close()

    def _fillBuffer(self, requested_size: int) -> None:
        """現在位置から Range データを取得し、先読みバッファを更新する。"""

        range_start = self._position
        range_end = min(self._file_size - 1, range_start + requested_size - 1)
        for retry_count in range(2):
            access_token = self._token_provider.getAccessToken(force_refresh=retry_count > 0)
            response = self._client.get(
                f'{BOX_API_BASE_URL}/files/{self._file_id}/content',
                headers = {
                    'Accept': 'application/octet-stream',
                    'Authorization': f'Bearer {access_token}',
                    'Range': f'bytes={range_start}-{range_end}',
                },
            )
            if response.status_code == 401 and retry_count == 0:
                continue
            if response.status_code != 206:
                raise BoxAPIError(response.status_code, 'range_request_failed')
            expected_maximum = range_end - range_start + 1
            if len(response.content) > expected_maximum:
                raise BoxAPIError(response.status_code, 'invalid_range_response')
            self._buffer_start = range_start
            self._buffer = response.content
            return
        raise BoxAPIError(401, 'unauthorized')


@dataclass(frozen=True, slots=True)
class _BoxStatResult:
    """TSKeyFrameSeeker が参照するファイルサイズだけを持つ stat 互換オブジェクト。"""

    st_size: int


@dataclass(frozen=True, slots=True)
class BoxRemotePath:
    """Path と同じ open/stat インターフェースを Box 録画へ提供するアダプター。"""

    file_id: str
    file_name: str
    file_size: int
    buffer_size: int = BoxRandomAccessFile.DEFAULT_BUFFER_SIZE

    def open(self, mode: str = 'rb', *args: Any, **kwargs: Any) -> BoxRandomAccessFile:
        """
        Box 録画を同期 Range リーダーとして開く。

        Args:
            mode: バイナリ読み取りモードのみ対応。
            args: Path.open() 互換の未使用位置引数。
            kwargs: Path.open() 互換の未使用キーワード引数。

        Returns:
            seek/read 可能な Box Range リーダー。
        """

        if mode != 'rb':
            raise ValueError('Box recordings can only be opened in binary read mode.')
        return BoxRandomAccessFile(self.file_id, self.file_size, self.buffer_size)

    def stat(self) -> _BoxStatResult:
        """Box メタデータ由来のファイルサイズを持つ stat 互換値を返す。"""

        return _BoxStatResult(st_size=self.file_size)

    def __str__(self) -> str:
        """ログ表示向けの秘密情報を含まない仮想パスを返す。"""

        return f'box://{self.file_id}/{self.file_name}'


def ResolveRecordedVideoSourcePath(
    recorded_program_id: int,
    local_file_path: str,
    box_buffer_size: int = BoxRandomAccessFile.DEFAULT_BUFFER_SIZE,
) -> Any:
    """
    ローカルファイルがなければ対応する Box Remote Path を返す。

    Args:
        recorded_program_id: KonomiTV の録画番組 ID。
        local_file_path: RecordedVideo に保存された元のローカルパス。
        box_buffer_size: Box Range リーダーの先読みサイズ。

    Returns:
        pathlib.Path または BoxRemotePath。
    """

    from pathlib import Path

    local_path = Path(local_file_path)
    if local_path.is_file() is True:
        return local_path
    mapping = BoxRecordingCatalog.get(recorded_program_id)
    if mapping is None:
        return local_path
    return BoxRemotePath(mapping.box_file_id, mapping.name, mapping.size, box_buffer_size)


def IsBoxOnlyRecording(recorded_program_id: int, local_file_path: str) -> bool:
    """
    録画がローカルにはなく Box にだけ存在するかを返す。

    Args:
        recorded_program_id: KonomiTV の録画番組 ID。
        local_file_path: RecordedVideo に保存された元のローカルパス。

    Returns:
        Box のみから利用可能な場合は True。
    """

    return os.path.isfile(local_file_path) is False and BoxRecordingCatalog.has(recorded_program_id)
