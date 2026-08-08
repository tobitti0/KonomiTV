from __future__ import annotations

import asyncio
from datetime import datetime

import anyio

from app import logging, schemas
from app.constants import JST
from app.metadata.CMSectionsDetector import CMSectionsDetector
from app.models.RecordedProgram import RecordedProgram


class CMAnalysisManager:
    """
    API から開始された単体・一括 CM 解析ジョブの対象選択、並列実行、進捗管理を担当するクラス
    """

    def __init__(self) -> None:
        """
        CM 解析ジョブ管理クラスを初期化する

        Args:
            なし
        """

        # 現在のジョブ本体
        # API リクエスト終了後も処理を継続し、キャンセル時はこのタスクを停止する
        self._task: asyncio.Task[None] | None = None
        # ジョブ開始とキャンセルの競合を防ぐロック
        self._lock = asyncio.Lock()
        # GET API から返す現在または直近ジョブの状態
        self._state = schemas.CMAnalysisJob(
            status='Idle',
            total=0,
            processed=0,
            succeeded=0,
            failed=0,
            concurrency=0,
            force=False,
            target=None,
            genre=None,
            started_at=None,
            completed_at=None,
        )

    def getState(self) -> schemas.CMAnalysisJob:
        """
        現在または直近の CM 解析ジョブ状態を取得する

        Returns:
            schemas.CMAnalysisJob: 呼び出し側から変更されないようコピーしたジョブ状態
        """

        return self._state.model_copy(deep=True)

    def isRunning(self) -> bool:
        """
        CM 解析ジョブが実行中か確認する

        Returns:
            bool: 実行中またはキャンセル処理中なら True
        """

        return self._state.status in ['Running', 'Canceling']

    async def startAnalysis(
        self,
        request: schemas.CMAnalysisBatchRequest,
        trigger: schemas.CMAnalysisTrigger,
    ) -> schemas.CMAnalysisJob:
        """
        条件に一致する録画番組の CM 解析をバックグラウンドで開始する

        Args:
            request (schemas.CMAnalysisBatchRequest): 対象条件と並列数
            trigger (schemas.CMAnalysisTrigger): 解析を開始した契機

        Returns:
            schemas.CMAnalysisJob: 開始直後のジョブ状態

        Raises:
            RuntimeError: 別の CM 解析ジョブが既に実行中の場合
        """

        async with self._lock:
            # 同じ録画を複数の一括ジョブから重複実行しないよう、サーバー全体でジョブは1つに限定する
            if self.isRunning() is True:
                raise RuntimeError('CM analysis job is already running')

            targets = await self.__selectTargets(request)
            started_at = datetime.now(tz=JST)
            self._state = schemas.CMAnalysisJob(
                status='Running' if targets else 'Completed',
                total=len(targets),
                processed=0,
                succeeded=0,
                failed=0,
                concurrency=request.concurrency,
                force=request.force,
                target=request.target,
                genre=request.genre,
                started_at=started_at,
                completed_at=None if targets else started_at,
            )

            # 対象が0件の場合も正常な空ジョブとして結果を返す
            if not targets:
                self._task = None
                return self.getState()

            # HTTP コネクションが切断されても解析を継続できるよう、リクエスト処理とは分離したタスクにする
            self._task = asyncio.create_task(self.__run(targets, request.concurrency, request.force, trigger))
            logging.info(
                f'CM analysis job started. targets={len(targets)} concurrency={request.concurrency} '
                f'force={request.force} target={request.target} genre={request.genre!r}'
            )
            return self.getState()

    async def cancelAnalysis(self) -> bool:
        """
        実行中の CM 解析ジョブをキャンセルする

        Returns:
            bool: 実行中のジョブへキャンセルを要求できた場合は True
        """

        async with self._lock:
            if self._task is None or self._task.done() is True or self.isRunning() is False:
                return False
            self._state.status = 'Canceling'
            self._task.cancel()
            task = self._task

        # 外部コマンドの terminate と DB 状態の復元が完了するまで待つ
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    async def __selectTargets(
        self,
        request: schemas.CMAnalysisBatchRequest,
    ) -> list[RecordedProgram]:
        """
        API で指定された録画 ID・ジャンル・解析状態に一致する MPEG-TS 録画を取得する

        Args:
            request (schemas.CMAnalysisBatchRequest): 対象条件

        Returns:
            list[RecordedProgram]: CM 解析対象の録画番組レコード
        """

        query = RecordedProgram.all() \
            .select_related('recorded_video') \
            .select_related('channel') \
            .filter(recorded_video__status='Recorded') \
            .filter(recorded_video__container_format='MPEG-TS')
        if request.recorded_program_ids is not None:
            query = query.filter(id__in=request.recorded_program_ids)
        recorded_programs = await query.order_by('start_time')

        targets: list[RecordedProgram] = []
        normalized_genre = request.genre.casefold() if request.genre is not None else None
        for recorded_program in recorded_programs:
            current_status = recorded_program.recorded_video.cm_analysis_status
            if request.target == 'Unanalyzed' and current_status != 'Unanalyzed':
                continue
            if request.target == 'Failed' and current_status != 'Failed':
                continue
            if request.target == 'UnanalyzedOrFailed' and current_status not in ['Unanalyzed', 'Failed']:
                continue

            # ジャンルは major / middle のどちらかに部分一致すれば対象とする
            # 「アニメ」の指定で「アニメ・特撮」「国内アニメ」のどちらにも一致させられる
            if normalized_genre is not None:
                genre_matches = any(
                    normalized_genre in str(genre.get('major', '')).casefold() or
                    normalized_genre in str(genre.get('middle', '')).casefold()
                    for genre in recorded_program.genres
                )
                if genre_matches is False:
                    continue
            targets.append(recorded_program)
        return targets

    async def __run(
        self,
        targets: list[RecordedProgram],
        concurrency: int,
        force: bool,
        trigger: schemas.CMAnalysisTrigger,
    ) -> None:
        """
        選択済み録画をキューから取り出し、指定並列数で解析する

        Args:
            targets (list[RecordedProgram]): 解析対象
            concurrency (int): 同時実行数
            force (bool): 既存チャプターファイルを使わず解析ツールを再実行するかどうか
            trigger (schemas.CMAnalysisTrigger): 解析を開始した契機

        Returns:
            None
        """

        queue: asyncio.Queue[RecordedProgram] = asyncio.Queue()
        for target in targets:
            queue.put_nowait(target)

        async def Worker() -> None:
            """キューから録画を1件ずつ取り出して CM 解析するワーカー"""

            while queue.empty() is False:
                try:
                    recorded_program = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    result = await CMSectionsDetector(
                        file_path=anyio.Path(recorded_program.recorded_video.file_path),
                        duration_sec=recorded_program.recorded_video.duration,
                        channel_id=recorded_program.channel.id if recorded_program.channel is not None else None,
                        service_id=recorded_program.service_id,
                    ).detectAndSave(trigger=trigger, force=force)
                    self._state.processed += 1
                    if result == 'Completed':
                        self._state.succeeded += 1
                    else:
                        self._state.failed += 1
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(Worker()) for _ in range(min(concurrency, len(targets)))]
        try:
            await asyncio.gather(*workers)
            self._state.status = 'Completed'
            logging.info(
                f'CM analysis job completed. total={self._state.total} '
                f'succeeded={self._state.succeeded} failed={self._state.failed}'
            )
        except asyncio.CancelledError:
            # 実行中の外部コマンドへキャンセルを伝播し、各 Detector が以前の DB 状態を復元するまで待つ
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            self._state.status = 'Canceled'
            logging.info(
                f'CM analysis job canceled. processed={self._state.processed} total={self._state.total}'
            )
            raise
        except Exception as ex:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            self._state.status = 'Failed'
            logging.error('CM analysis job failed:', exc_info=ex)
        finally:
            self._state.completed_at = datetime.now(tz=JST)
            self._task = None


# MaintenanceRouter と VideosRouter のどちらから開始しても同じジョブ・並列数を共有する
CM_ANALYSIS_MANAGER = CMAnalysisManager()
