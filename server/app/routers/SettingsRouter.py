
import asyncio
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, status

from app import logging, schemas
from app.config import (
    ApplyProgramTitleRegex,
    ClientSettings,
    Config,
    SaveConfig,
    SaveProgramTitleRegex,
    ServerSettings,
)
from app.metadata.ProgramTitleParser import ProgramTitleParser
from app.metadata.RecordedScanTask import RecordedScanTask
from app.models.User import User
from app.routers.UsersRouter import GetCurrentAdminUser, GetCurrentUser


# ルーター
router = APIRouter(
    tags = ['Settings'],
    prefix = '/api/settings',
)

# 保存済み録画番組のシリーズ一括再判定タスク
program_title_reclassification_task: asyncio.Task[schemas.ProgramTitleReclassificationResult] | None = None


@router.get(
    '/client',
    summary = 'クライアント設定取得 API',
    response_description = 'ログイン中のユーザーアカウントのクライアント設定。',
    response_model = ClientSettings,
)
async def ClientSettingsAPI(
    current_user: Annotated[User, Depends(GetCurrentUser)],
):
    """
    現在ログイン中のユーザーアカウントのクライアント設定を取得する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。
    """
    return current_user.client_settings


@router.put(
    '/client',
    summary = 'クライアント設定更新 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def ClientSettingsUpdateAPI(
    client_settings: Annotated[ClientSettings, Body(description='更新するクライアント設定のデータ。')],
    current_user: Annotated[User, Depends(GetCurrentUser)],
):
    """
    現在ログイン中のユーザーアカウントのクライアント設定を更新する。<br>
    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていないとアクセスできない。
    """

    # 現在サーバーに保存されているクライアント設定の最終同期時刻よりも古いクライアント設定が送られてきた場合、エラーを返す
    current_client_settings = ClientSettings.model_validate(current_user.client_settings)
    if client_settings.last_synced_at < current_client_settings.last_synced_at:
        logging.error(f'[ClientSettingsUpdateAPI] Client settings are outdated! [{client_settings.last_synced_at} < {current_client_settings.last_synced_at}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'The client settings are outdated. Please update the client settings from the server.',
        )

    # dict に変換してから入れる
    ## Pydantic モデルのままだと JSON にシリアライズできないので怒られる
    current_user.client_settings = dict(client_settings)

    # レコードを保存する
    await current_user.save()


@router.get(
    '/server',
    summary = 'サーバー設定取得 API',
    response_description = '現在稼働中の KonomiTV サーバーのサーバー設定。',
    response_model = ServerSettings,
)
async def ServerSettingsAPI():
    """
    現在稼働中の KonomiTV サーバーのサーバー設定を取得する。<br>
    Docker 環境では、パス指定の項目は Docker 環境向けの Prefix (/host-rootfs) が付与された状態で返される。<br>
    """

    return Config()


@router.put(
    '/server',
    summary = 'サーバー設定更新 API',
    status_code = status.HTTP_204_NO_CONTENT,
)
async def ServerSettingsUpdateAPI(
    server_settings: Annotated[ServerSettings, Body(description='更新するサーバー設定のデータ。')],
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """
    現在稼働中の KonomiTV サーバーのサーバー設定を更新する。<br>
    Docker 環境では、パス指定の項目には Docker 環境向けの Prefix (/host-rootfs) を付与した状態でリクエストする必要がある。<br>
    Pydantic のカスタムバリデーターの実装の都合上、バリデーション処理中はメインスレッドが数秒間ブロッキングされることがあるので注意。<br>

    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    # バリデーションが完了したサーバー設定を config.yaml に保存する
    SaveConfig(server_settings)

    # 番組タイトルのシリーズ判定用正規表現は、サーバーを再起動せず今後の解析へ反映する
    # ほかの設定は従来どおりサーバー再起動後に反映される
    ApplyProgramTitleRegex(server_settings.video.program_title_regex)


@router.post(
    '/server/program-title-regex/test',
    summary = '番組タイトルのシリーズ判定用正規表現テスト API',
    response_description = '各番組タイトルの正規表現による解析結果。',
    response_model = schemas.ProgramTitleRegexTestResponse,
)
async def ProgramTitleRegexTestAPI(
    request: Annotated[schemas.ProgramTitleRegexTestRequest, Body(description='テストする正規表現と番組タイトルのリスト。')],
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """
    未保存の番組タイトル解析用正規表現を、複数の番組タイトルに対してテストする。<br>
    第1キャプチャをシリーズ名、第2キャプチャを話数、第3キャプチャをサブタイトルとして返す。<br>

    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    results: list[schemas.ProgramTitleRegexTestResult] = []
    for title in request.titles:
        parsed_title = ProgramTitleParser.parse(title=title, pattern=request.pattern)
        results.append(schemas.ProgramTitleRegexTestResult(
            title = title,
            matched = parsed_title is not None,
            series_title = parsed_title.series_title if parsed_title is not None else None,
            episode_number = parsed_title.episode_number if parsed_title is not None else None,
            subtitle = parsed_title.subtitle if parsed_title is not None else None,
        ))

    return schemas.ProgramTitleRegexTestResponse(results=results)


@router.post(
    '/server/program-title-regex/reclassify',
    summary = '保存済み録画番組のシリーズ一括再判定 API',
    response_description = '保存済み録画番組のシリーズ再判定結果。',
    response_model = schemas.ProgramTitleReclassificationResult,
)
async def ProgramTitleReclassificationAPI(
    request: Annotated[
        schemas.ProgramTitleReclassificationRequest,
        Body(description='シリーズ再判定に利用する番組タイトル解析用正規表現。'),
    ],
    current_user: Annotated[User, Depends(GetCurrentAdminUser)],
):
    """
    指定された正規表現を config.yaml へ保存・即時反映した上で、DB に保存済みの録画番組タイトルを再解析し、シリーズへの紐付けを一括更新する。<br>
    録画ファイルの再解析は行わないため、CM 区間情報・サムネイル・動画メタデータは変更されない。<br>

    JWT エンコードされたアクセストークンがリクエストの Authorization: Bearer に設定されていて、かつ管理者アカウントでないとアクセスできない。
    """

    global program_title_reclassification_task

    async def ProgramTitleReclassification() -> schemas.ProgramTitleReclassificationResult:
        global program_title_reclassification_task
        try:
            return await RecordedScanTask().reclassifySeries(request.pattern)
        finally:
            # 成否を問わず、次回の再判定を開始できる状態へ戻す
            program_title_reclassification_task = None

    # 同じ全件更新処理が重複して走ると結果件数やシリーズ紐付けが競合するため、同時実行は許可しない
    if program_title_reclassification_task is not None:
        logging.warning('[SettingsRouter][ProgramTitleReclassificationAPI] Series reclassification is already running.')
        raise HTTPException(
            status_code = status.HTTP_429_TOO_MANY_REQUESTS,
            detail = 'Series reclassification is already running',
        )

    # 再判定に使う正規表現を保存し、今後新しく解析される録画にも即時反映する
    # 保存に失敗した場合はシリーズ再判定を開始しない
    SaveProgramTitleRegex(request.pattern)

    # HTTP コネクションが途中で切断されても DB 更新を最後まで完了またはロールバックできるよう独立タスクで実行する
    program_title_reclassification_task = asyncio.create_task(ProgramTitleReclassification())
    return await asyncio.shield(program_title_reclassification_task)
