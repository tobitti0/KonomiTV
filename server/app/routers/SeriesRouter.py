
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status
from tortoise.expressions import Q

from app import logging, schemas
from app.extensions.box_streaming.BoxRecordingCatalog import BoxRecordingCatalog
from app.models.RecordedProgram import RecordedProgram
from app.models.RecordedVideo import RecordedVideo
from app.models.Series import Series
from app.models.SeriesBroadcastPeriod import SeriesBroadcastPeriod


# ルーター
router = APIRouter(
    tags = ['Series'],
    prefix = '/api/series',
)

# ページングで一度に取得するシリーズ番組の数
PAGE_SIZE = 30


async def BuildSeriesSchemas(db_series_list: list[Series]) -> list[schemas.Series]:
    """
    Series DB モデルのリストから、放送期間・録画番組・録画ファイルを含む API レスポンスを構築する。
    Tortoise ORM の逆参照リレーションを Pydantic が QuerySet として誤認しないよう、関連レコードを明示的に取得して組み立てる。

    Args:
        db_series_list (list[Series]): API レスポンスへ変換する Series DB モデルのリスト。

    Returns:
        list[schemas.Series]: 関連する録画番組まで展開済みのシリーズ情報。
    """

    if len(db_series_list) == 0:
        return []

    series_ids = [db_series.id for db_series in db_series_list]

    # SeriesBroadcastPeriod.channel は前方参照なので select_related() で一括取得できる
    db_broadcast_periods = await SeriesBroadcastPeriod.filter(series_id__in=series_ids) \
        .select_related('channel') \
        .order_by('-start_date', 'id')
    broadcast_period_ids = [db_broadcast_period.id for db_broadcast_period in db_broadcast_periods]

    # 録画番組は放送開始順で取得し、一覧・次話判定の基本順序を API 側でも安定させる
    recorded_program_field_names = [
        field_name for field_name in schemas.RecordedProgram.model_fields
        if field_name not in {'recorded_video', 'channel'}
    ]
    db_recorded_programs = await RecordedProgram.filter(
        series_broadcast_period_id__in = broadcast_period_ids,
    ).only(
        *recorded_program_field_names,
        'channel_id',
        'series_broadcast_period_id',
    ).order_by('start_time', 'id') if len(broadcast_period_ids) > 0 else []
    recorded_program_ids = [db_recorded_program.id for db_recorded_program in db_recorded_programs]

    # key_frames / segment_map はシリーズ画面では不要かつ巨大になりうるため、RecordedVideo スキーマのうち DB に実在するフィールドだけを取得する
    ## storage_type などの Box 保存状態は DB カラムではなく、Pydantic モデル構築後に BoxRecordingCatalog から付加する
    recorded_video_virtual_field_names = {'storage_type', 'box_file_id', 'box_availability'}
    recorded_video_field_names = [
        field_name for field_name in schemas.RecordedVideo.model_fields
        if field_name not in recorded_video_virtual_field_names
    ]
    db_recorded_videos = await RecordedVideo.filter(recorded_program_id__in=recorded_program_ids).only(
        *recorded_video_field_names,
        'recorded_program_id',
    ) if len(recorded_program_ids) > 0 else []
    db_recorded_video_by_program_id = {
        db_recorded_video.recorded_program_id: db_recorded_video
        for db_recorded_video in db_recorded_videos
    }

    # 放送期間のチャンネルを番組にも利用できるよう、放送期間 ID ごとに Pydantic モデルへ変換して保持する
    channel_by_broadcast_period_id = {
        db_broadcast_period.id: schemas.Channel.model_validate(db_broadcast_period.channel)
        for db_broadcast_period in db_broadcast_periods
    }
    recorded_programs_by_broadcast_period_id: dict[int, list[schemas.RecordedProgram]] = {
        db_broadcast_period.id: [] for db_broadcast_period in db_broadcast_periods
    }

    for db_recorded_program in db_recorded_programs:
        db_recorded_video = db_recorded_video_by_program_id.get(db_recorded_program.id)
        if db_recorded_video is None or db_recorded_program.series_broadcast_period_id is None:
            # RecordedProgram と RecordedVideo は1対1だが、不整合データがあってもシリーズ API 全体を失敗させない
            logging.warning(
                f'[SeriesRouter][BuildSeriesSchemas] Recorded video or broadcast period was not found. '
                f'[recorded_program_id: {db_recorded_program.id}]'
            )
            continue

        recorded_program_data = {
            field_name: getattr(db_recorded_program, field_name)
            for field_name in recorded_program_field_names
        }
        recorded_program_data['recorded_video'] = schemas.RecordedVideo.model_validate(db_recorded_video)
        recorded_program_data['channel'] = channel_by_broadcast_period_id[db_recorded_program.series_broadcast_period_id]
        recorded_program_schema = schemas.RecordedProgram.model_validate(recorded_program_data)
        recorded_programs_by_broadcast_period_id[db_recorded_program.series_broadcast_period_id].append(
            BoxRecordingCatalog.decorateRecordedProgram(recorded_program_schema)
        )

    broadcast_periods_by_series_id: dict[int, list[schemas.SeriesBroadcastPeriod]] = {
        db_series.id: [] for db_series in db_series_list
    }
    for db_broadcast_period in db_broadcast_periods:
        broadcast_periods_by_series_id[db_broadcast_period.series_id].append(schemas.SeriesBroadcastPeriod(
            channel = channel_by_broadcast_period_id[db_broadcast_period.id],
            start_date = db_broadcast_period.start_date,
            end_date = db_broadcast_period.end_date,
            recorded_programs = recorded_programs_by_broadcast_period_id[db_broadcast_period.id],
        ))

    return [schemas.Series(
        id = db_series.id,
        title = db_series.title,
        description = db_series.description,
        genres = db_series.genres,
        broadcast_periods = broadcast_periods_by_series_id[db_series.id],
        created_at = db_series.created_at,
        updated_at = db_series.updated_at,
    ) for db_series in db_series_list]


@router.get(
    '',
    summary = 'シリーズ番組一覧 API',
    response_description = 'シリーズ番組のリスト。',
    response_model = schemas.SeriesList,
)
async def SeriesListAPI(
    order: Annotated[Literal['desc', 'asc'], Query(description='ソート順序 (desc or asc) 。')] = 'desc',
    page: Annotated[int, Query(description='ページ番号。')] = 1,
):
    """
    すべてのシリーズ番組を一度に 30 件ずつ取得する。<br>
    order には "desc" か "asc" を指定する。<br>
    page (ページ番号) には 1 以上の整数を指定する。
    """

    db_series_list = await Series.all() \
        .order_by('-updated_at' if order == 'desc' else 'updated_at') \
        .offset((page - 1) * PAGE_SIZE) \
        .limit(PAGE_SIZE) \

    return schemas.SeriesList.model_validate({
        'total': await Series.all().count(),
        'series_list': await BuildSeriesSchemas(db_series_list),
    })


@router.get(
    '/search',
    summary = 'シリーズ番組検索 API',
    response_description = '検索条件に一致するシリーズ番組のリスト。',
    response_model = schemas.SeriesList,
)
async def SeriesSearchAPI(
    query: Annotated[str, Query(description='検索キーワード。title または description のいずれかに部分一致するシリーズ番組を検索する。')] = '',
    order: Annotated[Literal['desc', 'asc'], Query(description='ソート順序 (desc or asc) 。')] = 'desc',
    page: Annotated[int, Query(description='ページ番号。')] = 1,
):
    """
    指定されたキーワードでシリーズ番組を一度に 30 件ずつ検索する。<br>
    キーワードは title または description のいずれかに部分一致するシリーズ番組を検索する。<br>
    order には "desc" か "asc" を指定する。<br>
    page (ページ番号) には 1 以上の整数を指定する。
    """

    # クエリが空の場合は全件取得と同じ挙動にする
    if not query:
        return await SeriesListAPI(order=order, page=page)

    # 検索条件を構築
    # title または description のいずれかに部分一致するレコードを検索
    db_series_list = await Series.all() \
        .filter(
            Q(title__icontains=query) |
            Q(description__icontains=query)
        ) \
        .order_by('-updated_at' if order == 'desc' else 'updated_at') \
        .offset((page - 1) * PAGE_SIZE) \
        .limit(PAGE_SIZE)

    # 検索条件に一致する総件数を取得
    total = await Series.all() \
        .filter(
            Q(title__icontains=query) |
            Q(description__icontains=query)
        ) \
        .count()

    return schemas.SeriesList.model_validate({
        'total': total,
        'series_list': await BuildSeriesSchemas(db_series_list),
    })


@router.get(
    '/{series_id}',
    summary = 'シリーズ番組 API',
    response_description = 'シリーズ番組。',
    response_model = schemas.Series,
)
async def SeriesAPI(
    series_id: Annotated[int, Path(description='シリーズ番組の ID 。')],
):
    """
    指定されたシリーズ番組を取得する。
    """

    db_series = await Series.get_or_none(id=series_id)
    if db_series is None:
        logging.warning(f'[SeriesRouter][SeriesAPI] Specified series_id was not found. [series_id: {series_id}]')
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'Specified series_id was not found',
        )

    series_list = await BuildSeriesSchemas([db_series])
    return series_list[0]
