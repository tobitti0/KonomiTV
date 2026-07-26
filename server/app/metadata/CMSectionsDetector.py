
from __future__ import annotations

import asyncio
import pathlib
import tempfile
import time

import anyio
import typer

from app import logging, schemas
from app.config import LoadConfig
from app.constants import CM_ANALYZER_DIR, CM_ANALYZER_LOGO_DIR
from app.models.RecordedVideo import RecordedVideo


class CMSectionsDetector:
    """
    録画 TS ファイルに含まれる CM 区間を検出するクラス
    録画ファイルと同じファイル名で .chapter.txt が保存されていればそこから CM 区間情報を取得し、
    .chapter.txt が存在しない場合は自前で CM 区間を検出する
    """

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


    async def detectAndSave(self) -> None:
        """
        録画ファイルの CM 区間を検出し、データベースに保存する
        """

        start_time = time.time()
        logging.info(f'{self.file_path}: Detecting CM sections...')
        db_recorded_video: RecordedVideo | None = None
        analysis_completed = False
        try:
            # 解析開始直後に状態を保存し、UI から実行中であることを判別できるようにする
            db_recorded_video = await RecordedVideo.get_or_none(file_path=str(self.file_path))
            if db_recorded_video is not None:
                db_recorded_video.cm_analysis_status = 'Analyzing'
                await db_recorded_video.save(update_fields=['cm_analysis_status'])

            # 録画ファイルに対応するチャプターファイル (.chapter.txt) がもしあれば解析し、CM 区間情報を取得する
            ## 自前で解析すると計算コストが高いので、もしチャプターファイルがあればそれを優先的に使う
            ## .chapter.txt は Amatsukaze でエンコードした際に設定次第で自動生成される
            cm_sections = await self.__detectFromChapterFile()

            # チャプターファイルが存在しない場合、join_logo_scp を使って自前で解析を試みる
            if cm_sections is None:
                cm_sections = await self.__detectWithJLS()

            # 自前でも解析できなかった（解析に失敗した）or CM 区間が1つも検出されなかった場合、
            # バックグラウンド解析処理が再度実行された際の再解析を回避するために [] を設定する
            ## [] は解析したが CM 区間がなかった/検出に失敗したことを表す
            ## CM 区間解析はかなり計算コストが高い処理のため、一度解析に失敗した録画ファイルは再解析しない
            if cm_sections is None:
                cm_sections = []

            # 検出結果をログに出力
            for cm_section in cm_sections:
                logging.debug(f'{self.file_path}: CM section detected: {cm_section["start_time"]} - {cm_section["end_time"]}')

            # 検出結果をデータベースに保存
            if db_recorded_video is not None:
                # CM 区間情報と解析完了状態を同時に更新する
                # 検出できなかった場合も必ず [] を設定する
                db_recorded_video.cm_sections = cm_sections
                db_recorded_video.cm_analysis_status = 'Completed'
                await db_recorded_video.save(update_fields=['cm_sections', 'cm_analysis_status'])
                analysis_completed = True
                if len(cm_sections) > 0:
                    logging.info(f'{self.file_path}: Saved {len(cm_sections)} CM sections. ({time.time() - start_time:.2f} sec)')
                else:
                    logging.info(f'{self.file_path}: No CM sections detected. ({time.time() - start_time:.2f} sec)')
            else:
                logging.warning(f'{self.file_path}: RecordedVideo record not found.')

        except Exception as ex:
            logging.error(f'{self.file_path}: Error saving CM sections to DB:', exc_info=ex)
        finally:
            # 解析失敗やサーバー終了による中断時は、再解析できる未解析状態へ戻す
            if db_recorded_video is not None and analysis_completed is False:
                try:
                    db_recorded_video.cm_analysis_status = 'Unanalyzed'
                    await db_recorded_video.save(update_fields=['cm_analysis_status'])
                except Exception as ex:
                    logging.error(f'{self.file_path}: Error resetting CM analysis status:', exc_info=ex)


    async def __detectWithJLS(self) -> list[schemas.CMSection] | None:
        """
        録画ファイルの CM 区間を join_logo_scp (with chapter_exe) を使って解析する

        Returns:
            list[schemas.CMSection] | None: 解析に成功した場合は CM 区間のリストを返す
        """

        chapter_exe_path = CM_ANALYZER_DIR / 'chapter_exe'
        dtvindex_path = CM_ANALYZER_DIR / 'dtvindex'
        logoframe_path = CM_ANALYZER_DIR / 'logoframe'
        join_logo_scp_path = CM_ANALYZER_DIR / 'join_logo_scp'
        jls_command_path = CM_ANALYZER_DIR / 'JL_標準.txt'

        # chapter_exe・dtvindex・join_logo_scp・標準解析スクリプトが揃っていなければ解析できない
        # logoframe は LGD がない場合も含めて任意なので、必須ツールには含めない
        required_paths = [chapter_exe_path, dtvindex_path, join_logo_scp_path, jls_command_path]
        if any(path.is_file() is False for path in required_paths):
            logging.warning(f'{self.file_path}: CM analyzer tools are not installed. Skipping JLS analysis.')
            return None

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
            chapter_return_code, _, chapter_stderr = await self.__runCommand([
                str(chapter_exe_path),
                '-v', str(self.file_path),
                '--serial',
                '-o', str(chapter_output_path),
            ])
            if chapter_return_code != 0 or chapter_output_path.is_file() is False:
                logging.error(
                    f'{self.file_path}: chapter_exe failed with exit code {chapter_return_code}: '
                    f'{chapter_stderr[-4000:]}'
                )
                return None

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
                logo_return_code, _, logo_stderr = await self.__runCommand(logoframe_command)
                if logo_return_code != 0 or logo_output_path.is_file() is False:
                    logging.warning(
                        f'{self.file_path}: logoframe failed with exit code {logo_return_code}; '
                        f'continuing without logo data: {logo_stderr[-4000:]}'
                    )
                    logo_output_path.unlink(missing_ok=True)

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

            join_return_code, _, join_stderr = await self.__runCommand(join_logo_scp_command)
            if (
                join_return_code != 0 or
                trim_output_path.is_file() is False or
                jls_detail_output_path.is_file() is False
            ):
                logging.error(
                    f'{self.file_path}: join_logo_scp failed with exit code {join_return_code}: '
                    f'{join_stderr[-4000:]}'
                )
                return None

            # Trim の解釈とフレーム PTS からの時刻変換は dtvindex に集約し、
            # 他のプレイヤーや編集ソフトでも扱える OGM 形式の一般的なチャプターファイルとして保存する
            dtvindex_return_code, _, dtvindex_stderr = await self.__runCommand([
                str(dtvindex_path),
                'trim-chapters',
                str(self.file_path),
                str(index_file_path),
                str(trim_output_path),
                '--jls', str(jls_detail_output_path),
                '-o', str(chapter_file_path),
            ])
            if dtvindex_return_code != 0 or await chapter_file_path.is_file() is False:
                logging.error(
                    f'{self.file_path}: dtvindex trim-chapters failed with exit code {dtvindex_return_code}: '
                    f'{dtvindex_stderr[-4000:]}'
                )
                return None

            return await self.__detectFromChapterFile(chapter_file_path)


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
