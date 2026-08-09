
from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, status
from tortoise.expressions import Q
from tortoise.functions import Max
from tortoise.queryset import QuerySet

from app import logging, schemas
from app.constants import JST
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

# シリーズ一覧 API で利用できる並び替え基準
SeriesSort = Literal['broadcasted_at', 'updated_at', 'title']


def BuildFilteredSeriesQuery(
    search_query: str,
    broadcast_start_date: date | None,
    broadcast_end_date: date | None,
) -> QuerySet[Series]:
    """
    検索語と放送日範囲を適用したシリーズ一覧クエリを構築する。
    放送日は録画番組の start_time を基準とし、指定期間内に1本でも放送されたシリーズを対象とする。

    Args:
        search_query (str): シリーズ名・概要に対する検索キーワード。空文字列なら検索条件を適用しない。
        broadcast_start_date (date | None): 放送日の下限 (この日を含む) 。
        broadcast_end_date (date | None): 放送日の上限 (この日を含む) 。

    Returns:
        QuerySet[Series]: 指定された検索条件を適用済みのクエリ。
    """

    # 日付範囲が逆転している場合は、意図しない空結果ではなく入力エラーとして明示する
    if (
        broadcast_start_date is not None and
        broadcast_end_date is not None and
        broadcast_start_date > broadcast_end_date
    ):
        raise HTTPException(
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail = 'broadcast_start_date must be earlier than or equal to broadcast_end_date',
        )

    db_series_query = Series.all()

    # シリーズ名または概要の部分一致検索を適用する
    if search_query != '':
        db_series_query = db_series_query.filter(
            Q(title__icontains=search_query) |
            Q(description__icontains=search_query)
        )

    # 録画番組の放送開始日時で絞り込むことで、長い放送休止を含む放送期間でも正確に判定する
    if broadcast_start_date is not None:
        broadcast_start_at = datetime.combine(broadcast_start_date, time.min, tzinfo=JST)
        db_series_query = db_series_query.filter(
            broadcast_periods__recorded_programs__start_time__gte = broadcast_start_at,
        )
    if broadcast_end_date is not None:
        broadcast_end_at = datetime.combine(broadcast_end_date + timedelta(days=1), time.min, tzinfo=JST)
        db_series_query = db_series_query.filter(
            broadcast_periods__recorded_programs__start_time__lt = broadcast_end_at,
        )

    # 1シリーズに複数の録画番組が一致しても、一覧上では重複させない
    return db_series_query.distinct()


def ApplySeriesSort(
    db_series_query: QuerySet[Series],
    sort: SeriesSort,
    order: Literal['desc', 'asc'],
) -> QuerySet[Series]:
    """
    シリーズ一覧クエリへ指定された並び順を適用する。

    Args:
        db_series_query (QuerySet[Series]): 検索・絞り込み条件を適用済みのクエリ。
        sort (SeriesSort): 並び替え基準。
        order (Literal['desc', 'asc']): 昇順または降順。

    Returns:
        QuerySet[Series]: 並び順を適用済みのクエリ。
    """

    order_prefix = '-' if order == 'desc' else ''

    # 最終放送日時は Series 自体に保持していないため、録画番組の start_time の最大値を集計して並べる
    if sort == 'broadcasted_at':
        return db_series_query.annotate(
            latest_broadcasted_at = Max('broadcast_periods__recorded_programs__start_time'),
        ).order_by(
            f'{order_prefix}latest_broadcasted_at',
            f'{order_prefix}id',
        )

    # タイトル・更新日時は同値時にもページ間の順序が変動しないよう ID を第2ソートキーにする
    return db_series_query.order_by(
        f'{order_prefix}{sort}',
        f'{order_prefix}id',
    )


async def CountFilteredSeries(db_series_query: QuerySet[Series]) -> int:
    """
    絞り込み済みクエリに含まれるシリーズの重複を除いた総件数を取得する。

    Args:
        db_series_query (QuerySet[Series]): 検索・放送日条件を適用済みのクエリ。

    Returns:
        int: 重複を除いたシリーズ総件数。
    """

    # Tortoise ORM の distinct().count() は逆参照 JOIN の行数を数えるため、複数話が条件に一致すると総件数が水増しされる
    ## SELECT DISTINCT したシリーズ ID の件数を数え、ページングの総件数と一覧本体を一致させる
    series_ids = await db_series_query.values_list('id', flat=True)
    return len(series_ids)


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
    sort: Annotated[SeriesSort, Query(description='ソート基準 (broadcasted_at, updated_at, title) 。')] = 'broadcasted_at',
    order: Annotated[Literal['desc', 'asc'], Query(description='ソート順序 (desc or asc) 。')] = 'desc',
    broadcast_start_date: Annotated[date | None, Query(description='放送日の下限 (この日を含む) 。')] = None,
    broadcast_end_date: Annotated[date | None, Query(description='放送日の上限 (この日を含む) 。')] = None,
    page: Annotated[int, Query(description='ページ番号。')] = 1,
):
    """
    すべてのシリーズ番組を一度に 30 件ずつ取得する。<br>
    sort には "broadcasted_at" (最終放送日時)・"updated_at" (更新日時)・"title" (シリーズ名) のいずれかを指定する。<br>
    order には "desc" か "asc" を指定する。<br>
    broadcast_start_date / broadcast_end_date には、その期間内に放送された録画番組を持つシリーズだけを取得する場合に日付を指定する。<br>
    page (ページ番号) には 1 以上の整数を指定する。
    """

    # 絞り込み後の総件数を取得してから、指定された並び順とページングを適用する
    db_series_query = BuildFilteredSeriesQuery('', broadcast_start_date, broadcast_end_date)
    total = await CountFilteredSeries(db_series_query)
    db_series_list = await ApplySeriesSort(db_series_query, sort, order) \
        .offset((page - 1) * PAGE_SIZE) \
        .limit(PAGE_SIZE) \

    return schemas.SeriesList.model_validate({
        'total': total,
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
    sort: Annotated[SeriesSort, Query(description='ソート基準 (broadcasted_at, updated_at, title) 。')] = 'broadcasted_at',
    order: Annotated[Literal['desc', 'asc'], Query(description='ソート順序 (desc or asc) 。')] = 'desc',
    broadcast_start_date: Annotated[date | None, Query(description='放送日の下限 (この日を含む) 。')] = None,
    broadcast_end_date: Annotated[date | None, Query(description='放送日の上限 (この日を含む) 。')] = None,
    page: Annotated[int, Query(description='ページ番号。')] = 1,
):
    """
    指定されたキーワードでシリーズ番組を一度に 30 件ずつ検索する。<br>
    キーワードは title または description のいずれかに部分一致するシリーズ番組を検索する。<br>
    sort には "broadcasted_at" (最終放送日時)・"updated_at" (更新日時)・"title" (シリーズ名) のいずれかを指定する。<br>
    order には "desc" か "asc" を指定する。<br>
    broadcast_start_date / broadcast_end_date には、その期間内に放送された録画番組を持つシリーズだけを取得する場合に日付を指定する。<br>
    page (ページ番号) には 1 以上の整数を指定する。
    """

    # 検索・絞り込み後の総件数を取得してから、指定された並び順とページングを適用する
    db_series_query = BuildFilteredSeriesQuery(query, broadcast_start_date, broadcast_end_date)
    total = await CountFilteredSeries(db_series_query)
    db_series_list = await ApplySeriesSort(db_series_query, sort, order) \
        .offset((page - 1) * PAGE_SIZE) \
        .limit(PAGE_SIZE)

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
