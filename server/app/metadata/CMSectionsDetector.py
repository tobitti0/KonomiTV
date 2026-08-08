
from __future__ import annotations

import asyncio
import pathlib
import tempfile
import time
from datetime import datetime
from typing import Literal

import anyio
import typer

from app import logging, schemas
from app.config import LoadConfig
from app.constants import CM_ANALYZER_DIR, CM_ANALYZER_LOGO_DIR, JST
from app.models.CMAnalysisRun import CMAnalysisRun
from app.models.RecordedVideo import RecordedVideo


class CMAnalysisFailure(Exception):
    """
    CM 解析の失敗工程と外部コマンドの診断情報を上位へ伝える例外
    """

    def __init__(
        self,
        stage: Literal['Preparation', 'ChapterFile', 'ChapterEXE', 'LogoFrame', 'JoinLogoSCP', 'DTVIndex', 'Unknown'],
        message: str,
        exit_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        """
        CM 解析失敗例外を初期化する

        Args:
            stage (Literal): 失敗した工程
            message (str): ユーザーへ表示できる簡潔な説明
            exit_code (int | None): 外部コマンドの終了コード
            detail (str | None): 標準エラーなどの診断情報
        """

        super().__init__(message)
        # API と解析履歴へ保存する失敗工程
        self.stage: Literal[
            'Preparation',
            'ChapterFile',
            'ChapterEXE',
            'LogoFrame',
            'JoinLogoSCP',
            'DTVIndex',
            'Unknown',
        ] = stage
        # UI でもそのまま表示できる、ファイルパスを含まない簡潔な説明
        self.message = message
        # 外部コマンド以外の失敗では None になる終了コード
        self.exit_code = exit_code
        # 外部コマンドの標準エラー末尾など、管理者向けの診断情報
        self.detail = detail

    def toError(self) -> schemas.CMAnalysisError:
        """
        DB と API で共通利用するエラー情報へ変換する

        Returns:
            schemas.CMAnalysisError: 解析失敗情報
        """

        return schemas.CMAnalysisError(
            stage=self.stage,
            message=self.message,
            exit_code=self.exit_code,
            detail=self.detail,
        )


class CMSectionsDetector:
    """
    録画 TS ファイルに含まれる CM 区間を検出するクラス
    録画ファイルと同じファイル名で .chapter.txt が保存されていればそこから CM 区間情報を取得し、
    .chapter.txt が存在しない場合は自前で CM 区間を検出する
    """

    # 同じ録画ファイルが録画完了時の自動解析と API から同時に解析されないよう直列化する
    __file_locks: dict[str, asyncio.Lock] = {}
    # 同じチャンネルの logoframe が LGD の世代番号と latest ファイルを同時更新しないよう直列化する
    __logo_locks: dict[str, asyncio.Lock] = {}

    def __init__(
        self,
        file_path: anyio.Path,
        duration_sec: float,
        channel_id: str | None = None,
        service_id: int | None = None,
    ) -> None:
        """
        録画 TS ファイルに含まれる CM 区間を検出するクラスを初期化する

        Args:
            file_path (anyio.Path): 動画ファイルのパス
            duration_sec (float): 動画の再生時間(秒)
            channel_id (str | None): チャンネル別ロゴの検索に利用するチャンネル ID
            service_id (int | None): logoframe の自動 LGD 生成に利用するサービス ID
        """

        # 解析対象の動画ファイルパス
        # .chapter.txt / .lgd サイドカーの検索と各解析ツールの入力に利用する
        self.file_path = file_path
        # 動画全体の長さ
        # 解析結果を動画の範囲内へ丸め、末尾の CM 区間を確定するために利用する
        self.duration_sec = duration_sec
        # 録画番組に紐づくチャンネル ID
        # server/data/cm-analysis/logos/ 以下から logoframe 用 LGD を検索するために利用する
        self.channel_id = channel_id
        # 録画番組に紐づくサービス ID
        # 既存 LGD がない場合に logoframe のチャンネル識別子として渡し、自動生成した LGD の世代管理に利用する
        self.service_id = service_id


    async def detectAndSave(
        self,
        trigger: Literal['Automatic', 'Manual', 'Batch'] = 'Automatic',
        force: bool = False,
    ) -> Literal['Completed', 'Failed']:
        """
        録画ファイルの CM 区間を検出し、データベースに保存する

        Args:
            trigger (Literal): 解析を開始した契機
            force (bool): 既存チャプターファイルを使わず解析ツールを再実行するかどうか

        Returns:
            Literal['Completed', 'Failed']: 解析の終了状態
        """

        # 同一ファイルを複数経路から解析すると、同じサイドカーファイルを同時に更新してしまう
        # ファイル単位のロックは KonomiTV サーバープロセス内の重複実行だけを抑止する
        file_lock = self.__file_locks.setdefault(str(self.file_path), asyncio.Lock())
        async with file_lock:
            return await self.__detectAndSave(trigger, force)


    async def __detectAndSave(
        self,
        trigger: Literal['Automatic', 'Manual', 'Batch'],
        force: bool,
    ) -> Literal['Completed', 'Failed']:
        """
        ファイル単位の排他制御を取得した状態で CM 区間を解析する

        Args:
            trigger (Literal): 解析を開始した契機
            force (bool): 既存チャプターファイルを使わず解析ツールを再実行するかどうか

        Returns:
            Literal['Completed', 'Failed']: 解析の終了状態
        """

        started_at = datetime.now(tz=JST)
        start_time = time.perf_counter()
        logging.info(f'{self.file_path}: Detecting CM sections...')
        db_recorded_video = await RecordedVideo.get_or_none(file_path=str(self.file_path))
        db_analysis_run: CMAnalysisRun | None = None
        stage_results: list[schemas.CMAnalysisStageResult] = []
        previous_status: Literal['Unanalyzed', 'Analyzing', 'Completed', 'Failed'] | None = None
        previous_error: schemas.CMAnalysisError | None = None
        try:
            # 解析開始直後に状態と履歴を保存し、UI と API から実行中であることを判別できるようにする
            if db_recorded_video is not None:
                previous_status = db_recorded_video.cm_analysis_status
                previous_error = db_recorded_video.cm_analysis_error
                db_recorded_video.cm_analysis_status = 'Analyzing'
                db_recorded_video.cm_analysis_error = None
                db_recorded_video.cm_analysis_started_at = started_at
                db_recorded_video.cm_analysis_completed_at = None
                db_recorded_video.cm_analysis_elapsed_time = None
                await db_recorded_video.save(update_fields=[
                    'cm_analysis_status',
                    'cm_analysis_error',
                    'cm_analysis_started_at',
                    'cm_analysis_completed_at',
                    'cm_analysis_elapsed_time',
                ])
                db_analysis_run = await CMAnalysisRun.create(
                    recorded_video=db_recorded_video,
                    status='Analyzing',
                    trigger=trigger,
                    stage_results=[],
                    started_at=started_at,
                )

            # 録画ファイルに対応するチャプターファイル (.chapter.txt) がもしあれば解析し、CM 区間情報を取得する
            ## 自前で解析すると計算コストが高いので、もしチャプターファイルがあればそれを優先的に使う
            ## .chapter.txt は Amatsukaze でエンコードした際に設定次第で自動生成される
            chapter_start_time = time.perf_counter()
            chapter_file_path = self.file_path.with_name(f'{self.file_path.stem}.chapter.txt')
            chapter_file_exists = await chapter_file_path.exists()
            cm_sections = None if force is True else await self.__detectFromChapterFile(chapter_file_path)
            stage_results.append(schemas.CMAnalysisStageResult(
                stage='ChapterFile',
                status=(
                    'Skipped' if force is True else
                    ('Completed' if cm_sections is not None else ('Failed' if chapter_file_exists is True else 'Skipped'))
                ),
                exit_code=None,
                elapsed_time=time.perf_counter() - chapter_start_time,
                detail=(
                    'A full reanalysis was requested.' if force is True else
                    (None if cm_sections is not None or chapter_file_exists is False else 'The chapter file could not be parsed.')
                ),
            ))

            # チャプターファイルが存在しない場合、join_logo_scp を使って自前で解析を試みる
            if cm_sections is None:
                cm_sections = await self.__detectWithJLS(stage_results)

            # 検出結果をログに出力
            for cm_section in cm_sections:
                logging.debug(f'{self.file_path}: CM section detected: {cm_section["start_time"]} - {cm_section["end_time"]}')

            # 検出結果をデータベースに保存
            if db_recorded_video is not None:
                completed_at = datetime.now(tz=JST)
                elapsed_time = time.perf_counter() - start_time
                # CM 区間情報と解析完了状態を同時に更新する
                # 正常に解析して CM がなかった場合だけ [] を設定し、失敗とは明確に区別する
                db_recorded_video.cm_sections = cm_sections
                db_recorded_video.cm_analysis_status = 'Completed'
                db_recorded_video.cm_analysis_error = None
                db_recorded_video.cm_analysis_completed_at = completed_at
                db_recorded_video.cm_analysis_elapsed_time = elapsed_time
                await db_recorded_video.save(update_fields=[
                    'cm_sections',
                    'cm_analysis_status',
                    'cm_analysis_error',
                    'cm_analysis_completed_at',
                    'cm_analysis_elapsed_time',
                ])
                assert db_analysis_run is not None
                db_analysis_run.status = 'Completed'
                db_analysis_run.cm_sections = cm_sections
                db_analysis_run.stage_results = stage_results
                db_analysis_run.completed_at = completed_at
                db_analysis_run.elapsed_time = elapsed_time
                await db_analysis_run.save(update_fields=[
                    'status',
                    'cm_sections',
                    'stage_results',
                    'completed_at',
                    'elapsed_time',
                ])
                if len(cm_sections) > 0:
                    logging.info(f'{self.file_path}: Saved {len(cm_sections)} CM sections. ({elapsed_time:.2f} sec)')
                else:
                    logging.info(f'{self.file_path}: No CM sections detected. ({elapsed_time:.2f} sec)')
            else:
                logging.warning(f'{self.file_path}: RecordedVideo record not found.')
            return 'Completed'

        except asyncio.CancelledError:
            # API からのキャンセルやサーバー終了時は失敗件数へ含めず、以前の表示状態へ戻す
            completed_at = datetime.now(tz=JST)
            elapsed_time = time.perf_counter() - start_time
            canceled_error = schemas.CMAnalysisError(
                stage='Canceled',
                message='CM analysis was canceled.',
                exit_code=None,
                detail=None,
            )
            if db_recorded_video is not None:
                assert previous_status is not None
                db_recorded_video.cm_analysis_status = previous_status
                db_recorded_video.cm_analysis_error = previous_error
                db_recorded_video.cm_analysis_completed_at = completed_at
                db_recorded_video.cm_analysis_elapsed_time = elapsed_time
                await db_recorded_video.save(update_fields=[
                    'cm_analysis_status',
                    'cm_analysis_error',
                    'cm_analysis_completed_at',
                    'cm_analysis_elapsed_time',
                ])
            if db_analysis_run is not None:
                db_analysis_run.status = 'Canceled'
                db_analysis_run.stage_results = stage_results
                db_analysis_run.error = canceled_error
                db_analysis_run.completed_at = completed_at
                db_analysis_run.elapsed_time = elapsed_time
                await db_analysis_run.save(update_fields=[
                    'status',
                    'stage_results',
                    'error',
                    'completed_at',
                    'elapsed_time',
                ])
            raise
        except CMAnalysisFailure as ex:
            return await self.__saveFailure(
                ex.toError(),
                stage_results,
                db_recorded_video,
                db_analysis_run,
                start_time,
            )
        except Exception as ex:
            logging.error(f'{self.file_path}: Error saving CM sections to DB:', exc_info=ex)
            return await self.__saveFailure(
                schemas.CMAnalysisError(
                    stage='Unknown',
                    message='An unexpected error occurred during CM analysis.',
                    exit_code=None,
                    detail=str(ex)[-4000:] or None,
                ),
                stage_results,
                db_recorded_video,
                db_analysis_run,
                start_time,
            )


    async def __saveFailure(
        self,
        error: schemas.CMAnalysisError,
        stage_results: list[schemas.CMAnalysisStageResult],
        db_recorded_video: RecordedVideo | None,
        db_analysis_run: CMAnalysisRun | None,
        start_time: float,
    ) -> Literal['Failed']:
        """
        解析失敗を録画情報と解析履歴へ保存する

        Args:
            error (schemas.CMAnalysisError): 保存する失敗情報
            stage_results (list[schemas.CMAnalysisStageResult]): 失敗までに実行した工程
            db_recorded_video (RecordedVideo | None): 解析対象の録画ファイルレコード
            db_analysis_run (CMAnalysisRun | None): 今回の解析履歴レコード
            start_time (float): perf_counter() で取得した開始時刻

        Returns:
            Literal['Failed']: 解析失敗状態
        """

        completed_at = datetime.now(tz=JST)
        elapsed_time = time.perf_counter() - start_time
        logging.error(
            f'{self.file_path}: CM analysis failed at {error["stage"]}: '
            f'{error["message"]} ({elapsed_time:.2f} sec)'
        )
        if db_recorded_video is not None:
            # 以前の正常な cm_sections は消さず、再生画面で引き続き利用できるようにする
            # 通常ユーザーも取得できる録画 API には、ファイルパスを含み得る診断詳細を公開しない
            db_recorded_video.cm_analysis_status = 'Failed'
            db_recorded_video.cm_analysis_error = schemas.CMAnalysisError(
                stage=error['stage'],
                message=error['message'],
                exit_code=error['exit_code'],
                detail=None,
            )
            db_recorded_video.cm_analysis_completed_at = completed_at
            db_recorded_video.cm_analysis_elapsed_time = elapsed_time
            await db_recorded_video.save(update_fields=[
                'cm_analysis_status',
                'cm_analysis_error',
                'cm_analysis_completed_at',
                'cm_analysis_elapsed_time',
            ])
        if db_analysis_run is not None:
            db_analysis_run.status = 'Failed'
            db_analysis_run.stage_results = stage_results
            db_analysis_run.error = error
            db_analysis_run.completed_at = completed_at
            db_analysis_run.elapsed_time = elapsed_time
            await db_analysis_run.save(update_fields=[
                'status',
                'stage_results',
                'error',
                'completed_at',
                'elapsed_time',
            ])
        return 'Failed'


    async def __detectWithJLS(
        self,
        stage_results: list[schemas.CMAnalysisStageResult],
    ) -> list[schemas.CMSection]:
        """
        録画ファイルの CM 区間を join_logo_scp (with chapter_exe) を使って解析する

        Args:
            stage_results (list[schemas.CMAnalysisStageResult]): 工程別の診断情報を追加するリスト

        Returns:
            list[schemas.CMSection]: 解析に成功した場合は CM 区間のリストを返す
        """

        chapter_exe_path = CM_ANALYZER_DIR / 'chapter_exe'
        dtvindex_path = CM_ANALYZER_DIR / 'dtvindex'
        logoframe_path = CM_ANALYZER_DIR / 'logoframe'
        join_logo_scp_path = CM_ANALYZER_DIR / 'join_logo_scp'
        jls_command_path = CM_ANALYZER_DIR / 'JL_標準.txt'

        # chapter_exe・dtvindex・join_logo_scp・標準解析スクリプトが揃っていなければ解析できない
        # logoframe は LGD がない場合も含めて任意なので、必須ツールには含めない
        required_paths = [chapter_exe_path, dtvindex_path, join_logo_scp_path, jls_command_path]
        missing_paths = [path.name for path in required_paths if path.is_file() is False]
        if missing_paths:
            raise CMAnalysisFailure(
                'Preparation',
                'Required CM analyzer tools are not installed.',
                detail=', '.join(missing_paths),
            )

        # 各ツールの中間ファイルは録画フォルダへ残さず、一時ディレクトリ内だけで管理する
        with tempfile.TemporaryDirectory(prefix='konomitv-cm-analysis-') as temporary_directory:
            temporary_path = pathlib.Path(temporary_directory)
            chapter_output_path = temporary_path / 'chapter.txt'
            logo_output_path = temporary_path / 'logo.txt'
            trim_output_path = temporary_path / 'trim.avs'
            jls_detail_output_path = temporary_path / 'jls-detail.txt'
            index_file_path = pathlib.Path(f'{self.file_path}.dtvi')
            chapter_file_path = self.file_path.with_name(f'{self.file_path.stem}.chapter.txt')

            # まず chapter_exe で無音区間とシーンチェンジを検出する
            chapter_return_code, _, chapter_stderr = await self.__runStage(
                'ChapterEXE',
                [
                    str(chapter_exe_path),
                    '-v', str(self.file_path),
                    '--serial',
                    '-o', str(chapter_output_path),
                ],
                stage_results,
            )
            if chapter_return_code != 0 or chapter_output_path.is_file() is False:
                self.__markLastStageFailed(
                    stage_results,
                    'chapter_exe did not produce its expected output file.',
                )
                raise CMAnalysisFailure(
                    'ChapterEXE',
                    'chapter_exe failed.',
                    exit_code=chapter_return_code,
                    detail=chapter_stderr[-4000:] or None,
                )

            # 録画ファイルのサイドカーまたはチャンネル別ディレクトリに既存 LGD があればそれを優先する
            # 既存 LGD がなくてもサービス ID が分かる場合は、logoframe に検出用 LGD の生成と世代管理を委ねる
            logo_file_path = await self.__findLogoFile()
            logoframe_command: list[str] | None = None
            if logoframe_path.is_file() is True:
                logoframe_command = [
                    str(logoframe_path),
                    str(self.file_path),
                ]
                if logo_file_path is not None:
                    logoframe_command.extend(['-logo', str(logo_file_path)])
                elif self.service_id is not None:
                    # 自動生成した LGD は録画ファイルの一時領域ではなく、サービスごとの永続データとして保存する
                    ## logoframe が作る <service_id>-vXXXX.lgd と <service_id>.latest を次の録画でも再利用できる
                    await anyio.Path(CM_ANALYZER_LOGO_DIR).mkdir(parents=True, exist_ok=True)
                    logoframe_command.extend([
                        '-channel', str(self.service_id),
                        '-logo-dir', str(CM_ANALYZER_LOGO_DIR),
                    ])
                else:
                    # サービス ID も既存 LGD もない録画は、従来どおりロゴなし解析へフォールバックする
                    logoframe_command = None

            if logoframe_command is not None:
                logoframe_command.extend(['-oa', str(logo_output_path)])
                # 同じサービス ID の自動生成 LGD は共通の世代管理ファイルを更新するため、
                # ファイル単位の並列解析中もロゴ工程だけはチャンネルごとに直列化する
                logo_lock_key = str(self.service_id) if self.service_id is not None else str(logo_file_path)
                logo_lock = self.__logo_locks.setdefault(logo_lock_key, asyncio.Lock())
                async with logo_lock:
                    logo_return_code, _, logo_stderr = await self.__runStage(
                        'LogoFrame',
                        logoframe_command,
                        stage_results,
                    )
                if logo_return_code != 0 or logo_output_path.is_file() is False:
                    self.__markLastStageFailed(
                        stage_results,
                        'logoframe did not produce logo interval data; analysis continued without it.',
                    )
                    logging.warning(
                        f'{self.file_path}: logoframe failed with exit code {logo_return_code}; '
                        f'continuing without logo data: {logo_stderr[-4000:]}'
                    )
                    logo_output_path.unlink(missing_ok=True)
            else:
                stage_results.append(schemas.CMAnalysisStageResult(
                    stage='LogoFrame',
                    status='Skipped',
                    exit_code=None,
                    elapsed_time=0.0,
                    detail='No usable logo file or service ID was available.',
                ))

            # join_logo_scp は chapter_exe の結果を必須入力とし、logoframe の結果が得られた場合だけ追加する
            join_logo_scp_command = [
                str(join_logo_scp_path),
                '-inscp', str(chapter_output_path),
                '-incmd', str(jls_command_path),
                '-o', str(trim_output_path),
                '-oscp', str(jls_detail_output_path),
                '-setup', '',
                '-syscode', 'UTF8N',
            ]
            if logo_output_path.is_file() is True:
                join_logo_scp_command[1:1] = ['-inlogo', str(logo_output_path)]

            join_return_code, _, join_stderr = await self.__runStage(
                'JoinLogoSCP',
                join_logo_scp_command,
                stage_results,
            )
            if (
                join_return_code != 0 or
                trim_output_path.is_file() is False or
                jls_detail_output_path.is_file() is False
            ):
                self.__markLastStageFailed(
                    stage_results,
                    'join_logo_scp did not produce its expected output files.',
                )
                raise CMAnalysisFailure(
                    'JoinLogoSCP',
                    'join_logo_scp failed.',
                    exit_code=join_return_code,
                    detail=join_stderr[-4000:] or None,
                )

            # Trim の解釈とフレーム PTS からの時刻変換は dtvindex に集約し、
            # 他のプレイヤーや編集ソフトでも扱える OGM 形式の一般的なチャプターファイルとして保存する
            dtvindex_return_code, _, dtvindex_stderr = await self.__runStage(
                'DTVIndex',
                [
                    str(dtvindex_path),
                    'trim-chapters',
                    str(self.file_path),
                    str(index_file_path),
                    str(trim_output_path),
                    '--jls', str(jls_detail_output_path),
                    '-o', str(chapter_file_path),
                ],
                stage_results,
            )
            if dtvindex_return_code != 0 or await chapter_file_path.is_file() is False:
                self.__markLastStageFailed(
                    stage_results,
                    'dtvindex did not produce a chapter file.',
                )
                raise CMAnalysisFailure(
                    'DTVIndex',
                    'dtvindex trim-chapters failed.',
                    exit_code=dtvindex_return_code,
                    detail=dtvindex_stderr[-4000:] or None,
                )

            cm_sections = await self.__detectFromChapterFile(chapter_file_path)
            if cm_sections is None:
                self.__markLastStageFailed(
                    stage_results,
                    'The generated chapter file could not be parsed.',
                )
                raise CMAnalysisFailure(
                    'DTVIndex',
                    'The generated chapter file is invalid.',
                    exit_code=dtvindex_return_code,
                    detail=str(chapter_file_path),
                )
            return cm_sections


    async def __runStage(
        self,
        stage: Literal['ChapterEXE', 'LogoFrame', 'JoinLogoSCP', 'DTVIndex'],
        command: list[str],
        stage_results: list[schemas.CMAnalysisStageResult],
    ) -> tuple[int, str, str]:
        """
        外部コマンドを実行し、終了コードと所要時間を解析履歴へ追加する

        Args:
            stage (Literal): 実行する解析工程
            command (list[str]): 実行ファイルを先頭にした引数リスト
            stage_results (list[schemas.CMAnalysisStageResult]): 工程別の診断情報を追加するリスト

        Returns:
            tuple[int, str, str]: 終了コード・標準出力・標準エラー
        """

        start_time = time.perf_counter()
        return_code, stdout, stderr = await self.__runCommand(command)
        stage_results.append(schemas.CMAnalysisStageResult(
            stage=stage,
            status='Completed' if return_code == 0 else 'Failed',
            exit_code=return_code,
            elapsed_time=time.perf_counter() - start_time,
            # 成功時もロゴ生成サンプル数などの有用な情報が標準エラーへ出力されるため末尾を保存する
            detail=stderr[-4000:] or None,
        ))
        return return_code, stdout, stderr


    @staticmethod
    def __markLastStageFailed(
        stage_results: list[schemas.CMAnalysisStageResult],
        detail: str,
    ) -> None:
        """
        終了コードは成功でも必要な出力が欠けていた工程を失敗へ修正する

        Args:
            stage_results (list[schemas.CMAnalysisStageResult]): 工程別の診断情報
            detail (str): 出力ファイル不足などの追加説明

        Returns:
            None
        """

        if not stage_results:
            return
        stage_results[-1]['status'] = 'Failed'
        previous_detail = stage_results[-1]['detail']
        stage_results[-1]['detail'] = f'{detail}\n{previous_detail}' if previous_detail else detail


    async def __findLogoFile(self) -> pathlib.Path | None:
        """
        logoframe で利用できる LGD ファイルを規約に基づいて検索する

        Returns:
            pathlib.Path | None: 利用する LGD ファイルのパス。見つからなければ None
        """

        # 録画ごとの LGD は動画と同じ場所に置けるよう、2種類の一般的なサイドカー名を受け付ける
        logo_candidates = [
            pathlib.Path(str(self.file_path.with_suffix('.lgd'))),
            pathlib.Path(str(self.file_path.with_name(f'{self.file_path.stem}.logo.lgd'))),
        ]

        # チャンネル ID が分かる場合は、永続データディレクトリに置かれたチャンネル共通 LGD も利用する
        if self.channel_id is not None:
            logo_candidates.append(CM_ANALYZER_LOGO_DIR / f'{self.channel_id}.lgd')

        # 録画ファイルが NAS 上にある場合もイベントループを塞がないよう、anyio.Path 経由で存在確認する
        for logo_candidate in logo_candidates:
            if await anyio.Path(logo_candidate).is_file():
                logging.info(f'{self.file_path}: Using logoframe logo: {logo_candidate}')
                return logo_candidate
        return None


    @staticmethod
    async def __runCommand(command: list[str]) -> tuple[int, str, str]:
        """
        CM 解析用の外部コマンドを非同期に実行する

        Args:
            command (list[str]): 実行ファイルを先頭にした引数リスト

        Returns:
            tuple[int, str, str]: 終了コード・標準出力・標準エラー
        """

        # 解析には数分掛かることがあるため、パイプを非同期に読みながら完了まで待つ
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(CM_ANALYZER_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await process.communicate()
        except asyncio.CancelledError:
            # サーバー終了時などに親タスクがキャンセルされた場合、重い解析プロセスだけが残らないよう確実に終了する
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except TimeoutError:
                    # SIGTERM を処理しない外部ツールでもキャンセル完了を待ち続けないよう、最後は強制終了する
                    process.kill()
                    await process.wait()
            raise

        stdout = stdout_bytes.decode('utf-8', errors='replace')
        stderr = stderr_bytes.decode('utf-8', errors='replace')
        return_code = process.returncode
        assert return_code is not None
        return return_code, stdout, stderr


    async def __detectFromChapterFile(
        self,
        chapter_file_path: anyio.Path | None = None,
    ) -> list[schemas.CMSection] | None:
        """
        録画ファイルに対応するチャプターファイルがもしあれば解析し、CM 区間情報を取得する

        Returns:
            list[CMSection] | None: チャプターファイルが存在し、解析に成功した場合は CM 区間のリストを返す
        """

        # 明示されていなければ、録画ファイルが hoge.ts のとき hoge.chapter.txt を探す
        resolved_chapter_file_path = chapter_file_path
        if resolved_chapter_file_path is None:
            resolved_chapter_file_path = self.file_path.with_name(f'{self.file_path.stem}.chapter.txt')

        # チャプターファイルが存在しない場合は None を返す
        if not await resolved_chapter_file_path.exists():
            return None

        # チャプターファイルを読み込む
        try:
            async with await resolved_chapter_file_path.open(encoding='utf-8') as f:
                lines = await f.readlines()
        except Exception as ex:
            # チャプターファイルの読み込みに失敗した場合は None を返す
            logging.error(f'{resolved_chapter_file_path}: Failed to read chapter file:', exc_info=ex)
            return None

        # チャプター情報を格納するリスト
        chapters: list[tuple[int, str, float]] = []  # (番号, 名前, 時刻)
        cm_sections: list[schemas.CMSection] = []

        # 2行ずつ処理 (チャプター時刻行とチャプター名行)
        for i in range(0, len(lines), 2):
            if i + 1 >= len(lines):
                break

            time_line = lines[i].strip()
            name_line = lines[i + 1].strip()

            # チャプター行のフォーマットが不正な場合は採用しない
            # 当該行だけ飛ばすこともできるが整合性が崩れる可能性が高いため、自前で CM 区間を検出した方が確実
            if not (time_line.startswith('CHAPTER') and name_line.startswith('CHAPTER') and 'NAME' in name_line):
                return None

            try:
                # CHAPTER100 以降も扱えるよう、固定桁ではなくキー名からチャプター番号を取得する
                time_key, chapter_time_text = time_line.split('=', maxsplit=1)
                name_key, chapter_name = name_line.split('=', maxsplit=1)
                chapter_num = int(time_key.removeprefix('CHAPTER'))
                name_chapter_num = int(name_key.removeprefix('CHAPTER').removesuffix('NAME'))
                if chapter_num != name_chapter_num:
                    return None
                # チャプター時刻を取得
                chapter_time = self.__timeToSeconds(chapter_time_text)

                if chapter_time <= float(self.duration_sec):
                    chapters.append((chapter_num, chapter_name, chapter_time))
                else:
                    # チャプター時刻が動画長を超えている行は無視する
                    logging.warning(
                        f'{resolved_chapter_file_path}: Chapter time {chapter_time} '
                        f'exceeds the video duration {self.duration_sec}. Skipping.'
                    )
            except Exception as ex:
                # パースに失敗した場合は採用しない
                # 当該行だけ飛ばすこともできるが整合性が崩れる可能性が高いため、自前で CM 区間を検出した方が確実
                logging.warning(
                    f'{resolved_chapter_file_path}: Failed to parse chapter data. '
                    f'(line {i}-{i+1}): {time_line}, {name_line}',
                    exc_info=ex,
                )
                return None

        # CM 区間を検出
        current_cm_start: float | None = None

        for i, (_, name, ctime) in enumerate(chapters):
            # 詳細チャプターでは、参考実装と同じくカット対象を X / XCM / X15Sec の形式で表す
            is_cm_chapter = (
                name.startswith('CM') or
                name in ['X', 'XCM'] or
                (name.startswith('X') and name.endswith('Sec') and name[1:-3].isdigit())
            )
            # CM 開始位置を検出
            if is_cm_chapter is True and current_cm_start is None:
                current_cm_start = ctime
            # CM 終了位置を検出
            elif is_cm_chapter is False and current_cm_start is not None:
                cm_sections.append(schemas.CMSection(
                    start_time=current_cm_start,
                    end_time=ctime,
                ))
                current_cm_start = None

        # 最後のチャプターが CM で終わっている場合、動画長を終了時刻とする
        if current_cm_start is not None:
            cm_sections.append(schemas.CMSection(
                start_time=current_cm_start,
                end_time=float(self.duration_sec),
            ))

        return cm_sections


    @staticmethod
    def __timeToSeconds(time_str: str) -> float:
        """
        時刻文字列 (HH:MM:SS.mmm) を秒単位の float に変換する

        Args:
            time_str (str): 時刻文字列 (HH:MM:SS.mmm)

        Returns:
            float: 秒単位の時刻
        """

        # 時、分、秒をそれぞれ分割
        hours, minutes, seconds = time_str.strip().split(':')
        # 時と分は整数に、秒は小数に変換して合計を返す
        return float(hours) * 3600 + float(minutes) * 60 + float(seconds)


if __name__ == "__main__":
    # デバッグ用: 録画ファイルの CM 区間を検出する
    # Usage: poetry run python -m app.metadata.CMSectionsDetector /path/to/recorded_file.ts
    def main(
        file_path: pathlib.Path = typer.Argument(
            ...,
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="録画ファイルのパス",
        ),
    ) -> None:
        """
        録画ファイルの CM 区間を検出する
        """

        # 設定を読み込む (必須)
        LoadConfig(bypass_validation=True)

        # メタデータを解析
        from app.metadata.MetadataAnalyzer import MetadataAnalyzer
        analyzer = MetadataAnalyzer(file_path)
        recorded_program = analyzer.analyze()
        if recorded_program is None:
            print(f'Error: {file_path} is not a valid recorded file.')
            return

        # CMSectionsDetector を初期化
        detector = CMSectionsDetector(
            file_path = anyio.Path(recorded_program.recorded_video.file_path),
            duration_sec = recorded_program.recorded_video.duration,
            channel_id = recorded_program.channel.id if recorded_program.channel is not None else None,
            service_id = recorded_program.service_id,
        )

        # CM 区間を検出
        asyncio.run(detector.detectAndSave())

    typer.run(main)
