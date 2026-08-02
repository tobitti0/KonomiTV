from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError


BOX_API_BASE_URL = 'https://api.box.com/2.0'
BOX_TOKEN_URL = 'https://api.box.com/oauth2/token'


class BoxConfigurationError(Exception):
    """Box 連携設定が不足している、または不正な場合の例外。"""


class BoxAPIError(Exception):
    """Box API がエラーレスポンスを返した場合の例外。"""

    def __init__(self, status_code: int, code: str) -> None:
        """
        Args:
            status_code: Box API が返した HTTP ステータスコード。
            code: Box API が返したエラーコード。
        """

        super().__init__(f'Box API request failed. [status_code: {status_code}][code: {code}]')
        self.status_code = status_code
        self.code = code


class BoxCredentials(BaseModel):
    """Client Credentials Grant で使う Box の資格情報。"""

    model_config = ConfigDict(extra='forbid')

    client_id: str = Field(min_length=1)
    client_secret: SecretStr = Field(min_length=1)
    enterprise_id: str = Field(min_length=1)


class BoxUserSummary(BaseModel):
    """Box のユーザー概要。"""

    id: str
    name: str
    login: str | None = None


class BoxFileMetadata(BaseModel):
    """Box 上の録画ファイルの安全なメタデータ。"""

    id: str
    name: str
    size: int
    sha1: str | None = None
    created_at: str | None = None
    modified_at: str | None = None
    created_by: BoxUserSummary | None = None
    modified_by: BoxUserSummary | None = None


class BoxFolderItem(BaseModel):
    """Box フォルダ直下にあるファイルまたはフォルダの概要。"""

    id: str
    type: str
    name: str
    size: int | None = None


class _BoxTokenResponse(BaseModel):
    """Box OAuth 2.0 トークンレスポンス。"""

    access_token: SecretStr
    expires_in: int = Field(gt=0)


class BoxClient:
    """Box API の認証・メタデータ取得・Range ストリーミングを担当するクライアント。"""

    def __init__(
        self,
        credentials: BoxCredentials,
        allowed_file_ids: set[str] | frozenset[str],
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """
        Args:
            credentials: Client Credentials Grant で使う Box の資格情報。
            allowed_file_ids: KonomiTV からアクセスを許可する Box ファイル ID。
            http_client: テストなどで差し替える HTTP クライアント。
        """

        if any(file_id.isdecimal() is False for file_id in allowed_file_ids):
            raise BoxConfigurationError('Box file IDs must contain only decimal digits.')

        self._credentials = credentials
        # 環境変数で明示された ID に加え、許可された同期対象フォルダから見つけた ID を実行中に追加する
        # set 自体への単純な add / contains は CPython 上で不可分なので、ストリーム処理との同時参照でも問題ない
        self._allowed_file_ids = set(allowed_file_ids)
        self._http_client = http_client or httpx.AsyncClient(
            http2 = True,
            follow_redirects = True,
            timeout = httpx.Timeout(connect=20.0, read=None, write=20.0, pool=20.0),
        )
        self._owns_http_client = http_client is None
        self._access_token: SecretStr | None = None
        self._access_token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    @classmethod
    def loadCredentialsFromEnvironment(cls) -> BoxCredentials:
        """
        Docker Secret から Box の資格情報を読み込む。

        Returns:
            検証済みの Box 資格情報。

        Raises:
            BoxConfigurationError: 設定が不足している、または不正な場合。
        """

        credentials_path_text = os.environ.get('KONOMITV_BOX_CREDENTIALS_PATH')
        if credentials_path_text is None or credentials_path_text.strip() == '':
            raise BoxConfigurationError('KONOMITV_BOX_CREDENTIALS_PATH is not configured.')

        credentials_path = Path(credentials_path_text)
        try:
            credentials_json = credentials_path.read_text(encoding='utf-8')
        except OSError:
            # 資格情報そのものやファイル内容は、例外メッセージにも含めない
            raise BoxConfigurationError('Box credentials file could not be read.') from None

        try:
            credentials = BoxCredentials.model_validate_json(credentials_json)
        except ValidationError:
            # Pydantic の ValidationError は入力値を含むことがあるため、そのままログへ流さない
            raise BoxConfigurationError('Box credentials file is invalid.') from None

        return credentials

    @classmethod
    def fromEnvironment(cls) -> BoxClient:
        """
        Docker Secret と環境変数から Box クライアントを生成する。

        Returns:
            Box 連携用クライアント。

        Raises:
            BoxConfigurationError: 設定が不足している、または不正な場合。
        """

        credentials = cls.loadCredentialsFromEnvironment()

        allowed_file_ids_text = os.environ.get('KONOMITV_BOX_ALLOWED_FILE_IDS', '')
        allowed_file_ids = {
            file_id.strip()
            for file_id in allowed_file_ids_text.split(',')
            if file_id.strip() != ''
        }
        return cls(credentials, allowed_file_ids)

    async def close(self) -> None:
        """このクライアントが所有する HTTP クライアントを閉じる。"""

        if self._owns_http_client is True:
            await self._http_client.aclose()

    def assertFileAllowed(self, file_id: str) -> None:
        """
        指定された Box ファイル ID が許可リストに含まれることを確認する。

        Args:
            file_id: 確認する Box ファイル ID。

        Raises:
            PermissionError: ファイル ID が許可リストに含まれない場合。
        """

        if file_id not in self._allowed_file_ids:
            raise PermissionError('Specified Box file ID is not allowed.')

    def registerDiscoveredFile(self, file_id: str) -> None:
        """
        許可された同期対象フォルダ内で発見したファイル ID を許可リストへ追加する。

        Args:
            file_id: Box ファイル ID。

        Raises:
            BoxConfigurationError: ファイル ID の形式が不正な場合。
        """

        if file_id.isdecimal() is False:
            raise BoxConfigurationError('Box file IDs must contain only decimal digits.')
        self._allowed_file_ids.add(file_id)

    def verifyRegistrationToken(self, token: str) -> bool:
        """
        TVDashBoard からのアップロード完了通知用トークンを検証する。

        Box Client Secret そのものは送信せず、用途を固定した HMAC 派生値だけを共有トークンとして使う。

        Args:
            token: X-KonomiTV-Box-Token ヘッダーで受け取った16進トークン。

        Returns:
            正しい派生トークンの場合は True。
        """

        expected_token = hmac.new(
            self._credentials.client_secret.get_secret_value().encode('utf-8'),
            b'KonomiTV Box recording registration v1',
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected_token, token)

    async def listFolderItems(self, folder_id: str) -> list[BoxFolderItem]:
        """
        Box フォルダ直下の項目をページングしながらすべて取得する。

        Args:
            folder_id: Box フォルダ ID。

        Returns:
            フォルダ直下にあるファイルとフォルダの一覧。

        Raises:
            BoxConfigurationError: フォルダ ID の形式が不正な場合。
            BoxAPIError: Box API がエラーを返した場合。
        """

        if folder_id.isdecimal() is False:
            raise BoxConfigurationError('Box folder IDs must contain only decimal digits.')

        items: list[BoxFolderItem] = []
        offset = 0
        while True:
            response = await self._requestWithAuthentication(
                'GET',
                f'{BOX_API_BASE_URL}/folders/{folder_id}/items',
                params = {
                    'fields': 'id,type,name,size',
                    'limit': '1000',
                    'offset': str(offset),
                },
            )
            self._raiseForStatus(response)
            response_data = response.json()
            entries_value = response_data.get('entries') if isinstance(response_data, dict) else None
            if isinstance(entries_value, list) is False:
                raise BoxAPIError(response.status_code, 'invalid_response')
            entries = cast(list[object], entries_value)
            try:
                page_items = [BoxFolderItem.model_validate(entry) for entry in entries]
            except ValidationError:
                raise BoxAPIError(response.status_code, 'invalid_response') from None
            items.extend(page_items)

            offset += len(page_items)
            total_count_value = response_data.get('total_count')
            if isinstance(total_count_value, int) is False:
                break
            total_count = cast(int, total_count_value)
            if len(page_items) == 0 or offset >= total_count:
                break

        return items

    async def getFileMetadata(self, file_id: str) -> BoxFileMetadata:
        """
        Box 上のファイルメタデータを取得する。

        Args:
            file_id: Box ファイル ID。

        Returns:
            Box 上のファイルメタデータ。

        Raises:
            PermissionError: ファイル ID が許可リストに含まれない場合。
            BoxAPIError: Box API がエラーを返した場合。
        """

        self.assertFileAllowed(file_id)
        response = await self._requestWithAuthentication(
            'GET',
            f'{BOX_API_BASE_URL}/files/{file_id}',
            params = {
                'fields': 'id,name,size,sha1,created_at,modified_at,created_by,modified_by',
            },
        )
        self._raiseForStatus(response)
        try:
            return BoxFileMetadata.model_validate(response.json())
        except ValidationError:
            raise BoxAPIError(response.status_code, 'invalid_response') from None

    @asynccontextmanager
    async def openFileStream(
        self,
        file_id: str,
        range_header: str | None = None,
    ) -> AsyncGenerator[httpx.Response]:
        """
        Box 上のファイルをストリーミング可能な状態で開く。

        Args:
            file_id: Box ファイル ID。
            range_header: クライアントから受け取った Range ヘッダー。

        Yields:
            Box のファイルコンテンツレスポンス。

        Raises:
            PermissionError: ファイル ID が許可リストに含まれない場合。
            BoxAPIError: Box API がエラーを返した場合。
        """

        self.assertFileAllowed(file_id)
        for retry_count in range(2):
            access_token = await self._getAccessToken(force_refresh=retry_count > 0)
            request_headers = {
                'Accept': 'application/octet-stream',
                'Authorization': f'Bearer {access_token}',
            }
            if range_header is not None:
                request_headers['Range'] = range_header

            async with self._http_client.stream(
                'GET',
                f'{BOX_API_BASE_URL}/files/{file_id}/content',
                headers = request_headers,
            ) as response:
                if response.status_code == 401 and retry_count == 0:
                    await response.aread()
                    continue
                if response.status_code not in (200, 206):
                    await response.aread()
                    self._raiseForStatus(response)
                    raise BoxAPIError(response.status_code, 'unexpected_response')
                yield response
                return

        raise BoxAPIError(401, 'unauthorized')

    async def _getAccessToken(self, force_refresh: bool = False) -> str:
        """
        Enterprise を認証主体にした Client Credentials Grant のアクセストークンを取得する。

        Args:
            force_refresh: キャッシュの有効期限に関わらず再取得するか。

        Returns:
            Box API のアクセストークン。

        Raises:
            BoxAPIError: Box OAuth 2.0 エンドポイントがエラーを返した場合。
        """

        if (
            force_refresh is False
            and self._access_token is not None
            and time.monotonic() < self._access_token_expires_at
        ):
            return self._access_token.get_secret_value()

        async with self._token_lock:
            if (
                force_refresh is False
                and self._access_token is not None
                and time.monotonic() < self._access_token_expires_at
            ):
                return self._access_token.get_secret_value()

            response = await self._http_client.post(
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
            self._raiseForStatus(response)
            try:
                token_response = _BoxTokenResponse.model_validate(response.json())
            except ValidationError:
                raise BoxAPIError(response.status_code, 'invalid_token_response') from None

            self._access_token = token_response.access_token
            # 有効期限直前のリクエスト失敗を避けるため、最大 60 秒早く更新する
            refresh_margin = min(60, token_response.expires_in / 10)
            self._access_token_expires_at = time.monotonic() + token_response.expires_in - refresh_margin
            return token_response.access_token.get_secret_value()

    async def _requestWithAuthentication(
        self,
        method: str,
        url: str,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """
        Box API に認証付きリクエストを送り、401 の場合だけ一度再認証する。

        Args:
            method: HTTP メソッド。
            url: リクエスト先 URL。
            params: Box API に渡すクエリパラメーター。

        Returns:
            Box API のレスポンス。
        """

        for retry_count in range(2):
            access_token = await self._getAccessToken(force_refresh=retry_count > 0)
            headers = {'Authorization': f'Bearer {access_token}'}
            response = await self._http_client.request(method, url, headers=headers, params=params)
            if response.status_code != 401 or retry_count > 0:
                return response
        raise BoxAPIError(401, 'unauthorized')

    @staticmethod
    def _raiseForStatus(response: httpx.Response) -> None:
        """
        Box API のエラーレスポンスを、秘密値を含まない例外へ変換する。

        Args:
            response: 検査する Box API レスポンス。

        Raises:
            BoxAPIError: HTTP ステータスコードが 400 以上の場合。
        """

        if response.status_code < 400:
            return

        error_code = 'unknown_error'
        try:
            response_data = response.json()
            if isinstance(response_data, dict) and isinstance(response_data.get('code'), str):
                error_code = response_data['code']
        except ValueError:
            pass
        raise BoxAPIError(response.status_code, error_code)
