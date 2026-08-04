from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from app.extensions.box_streaming.history_import.Models import (
    HistoricalImportPlan,
    HistoricalImportPlanSummary,
    HistoricalRecordingCandidate,
)


JST = timezone(timedelta(hours=9))
CURRENT_UPLOAD_PATTERN = re.compile(r'^✅ Uploaded \(([^)]+)\): id=(\d+), name=(.+)$')
LEGACY_UPLOAD_PATTERN = re.compile(r'^✅ Uploaded: id=(\d+), name=(.+)$')


class TVDashboardLogSourceError(Exception):
    """TVDashboardログが不正または読み取れない場合の例外。"""


@dataclass(frozen=True, slots=True)
class _UploadEvent:
    """JSONLから抽出した元TSアップロード成功イベント。"""

    task_id: str
    box_file_id: str
    box_upload_name: str


class TVDashboardLogSource:
    """TVDashboardのtasks.jsonとtask-logから取り込み計画を生成する。"""

    def __init__(self, source_directory: Path) -> None:
        """
        Args:
            source_directory: tasks.json と task-log を含むTVDashboardログディレクトリ。
        """

        # 取り込み元は本番ログを読み取り専用でマウントする前提なので、パスだけを保持して内容は変更しない
        self._source_directory = source_directory
        self._tasks_path = source_directory / 'tasks.json'
        self._task_log_directory = source_directory / 'task-log'

    def createPlan(self, existing_box_file_ids: set[str] | frozenset[str]) -> HistoricalImportPlan:
        """
        既存Box紐付けを除外し、EDCB情報が完全な候補だけを計画へ含める。

        Args:
            existing_box_file_ids: KonomiTVですでに紐付けられているBox file ID。

        Returns:
            Box APIへアクセスする前の取り込み計画。

        Raises:
            TVDashboardLogSourceError: ログ構造または必須値が不正な場合。
        """

        tasks_by_id = self._loadTasks()
        upload_events = self._loadOriginalUploadEvents()
        source_fingerprint = self._calculateSourceFingerprint()

        # 同じBox file IDが複数ログに現れた場合は、どちらを正とするか推測せず全候補から除外する
        events_by_box_file_id: dict[str, list[_UploadEvent]] = {}
        for event in upload_events:
            events_by_box_file_id.setdefault(event.box_file_id, []).append(event)
        duplicate_box_file_ids = {
            box_file_id
            for box_file_id, events in events_by_box_file_id.items()
            if len(events) > 1
        }

        candidates: list[HistoricalRecordingCandidate] = []
        excluded_existing_count = 0
        incomplete_metadata_count = 0
        missing_task_count = 0
        for event in upload_events:
            if event.box_file_id in duplicate_box_file_ids:
                continue
            if event.box_file_id in existing_box_file_ids:
                excluded_existing_count += 1
                continue

            task = tasks_by_id.get(event.task_id)
            if task is None:
                missing_task_count += 1
                continue

            candidate = self._createCandidate(task, event)
            if candidate is None:
                incomplete_metadata_count += 1
                continue
            candidates.append(candidate)

        # 実行順が毎回変わると検証ログの再開判定が難しくなるため、放送時刻とBox IDで決定的に並べる
        candidates.sort(key=lambda candidate: (candidate.start_time, candidate.box_file_id))
        return HistoricalImportPlan(
            generated_at = datetime.now(UTC),
            source_fingerprint = source_fingerprint,
            summary = HistoricalImportPlanSummary(
                upload_success_count = len(upload_events),
                eligible_count = len(candidates),
                excluded_existing_count = excluded_existing_count,
                incomplete_metadata_count = incomplete_metadata_count,
                missing_task_count = missing_task_count,
                duplicate_box_file_id_count = len(duplicate_box_file_ids),
            ),
            candidates = candidates,
        )

    def _loadTasks(self) -> dict[str, dict[str, Any]]:
        """
        tasks.jsonを読み込み、タスクIDで参照できる形へ変換する。

        Returns:
            タスクIDをキーにしたタスクレコード。

        Raises:
            TVDashboardLogSourceError: ファイルが存在しない、またはJSON構造が不正な場合。
        """

        try:
            raw_tasks = json.loads(self._tasks_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            raise TVDashboardLogSourceError('TVDashboard tasks.json could not be read.') from None

        tasks_value = raw_tasks if isinstance(raw_tasks, list) else raw_tasks.get('tasks') \
            if isinstance(raw_tasks, dict) else None
        if isinstance(tasks_value, list) is False:
            raise TVDashboardLogSourceError('TVDashboard tasks.json has an invalid structure.')

        tasks_by_id: dict[str, dict[str, Any]] = {}
        for task_value in cast(list[object], tasks_value):
            if isinstance(task_value, dict) is False:
                continue
            task = cast(dict[str, Any], task_value)
            task_id = task.get('id')
            if isinstance(task_id, str) and task_id != '':
                tasks_by_id[task_id] = task
        return tasks_by_id

    def _loadOriginalUploadEvents(self) -> list[_UploadEvent]:
        """
        新旧両方のログ形式から元TSのアップロード成功イベントを抽出する。

        Returns:
            元TSアップロード成功イベント。

        Raises:
            TVDashboardLogSourceError: task-logを読み取れない、またはJSONLが壊れている場合。
        """

        if self._task_log_directory.is_dir() is False:
            raise TVDashboardLogSourceError('TVDashboard task-log directory was not found.')

        events: list[_UploadEvent] = []
        try:
            log_paths = sorted(self._task_log_directory.glob('*.jsonl'))
            for log_path in log_paths:
                task_id = log_path.stem
                with log_path.open('r', encoding='utf-8') as log_file:
                    for line_number, line in enumerate(log_file, start=1):
                        if line.strip() == '':
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            raise TVDashboardLogSourceError(
                                f'TVDashboard task-log contains invalid JSON. '
                                f'[file: {log_path.name}][line: {line_number}]'
                            ) from None
                        if isinstance(row, dict) is False:
                            continue
                        message = row.get('message')
                        if isinstance(message, str) is False:
                            continue

                        current_match = CURRENT_UPLOAD_PATTERN.fullmatch(message)
                        if current_match is not None:
                            # current形式ではoriginal以外にエンコード成果物が含まれうるため、元TSだけを採用する
                            if current_match.group(1) == 'original':
                                events.append(_UploadEvent(task_id, current_match.group(2), current_match.group(3)))
                            continue

                        legacy_match = LEGACY_UPLOAD_PATTERN.fullmatch(message)
                        if legacy_match is not None:
                            # 旧実装は元TSだけをアップロードしており、ラベルを付けていなかった
                            events.append(_UploadEvent(task_id, legacy_match.group(1), legacy_match.group(2)))
        except OSError:
            raise TVDashboardLogSourceError('TVDashboard task-log could not be read.') from None
        return events

    def _createCandidate(
        self,
        task: dict[str, Any],
        event: _UploadEvent,
    ) -> HistoricalRecordingCandidate | None:
        """
        1件のタスクとアップロード成功イベントを共通候補へ変換する。

        Args:
            task: tasks.json内のタスクレコード。
            event: 対応する元TSアップロード成功イベント。

        Returns:
            必須情報が完全なら候補レコード、不足または不整合ならNone。
        """

        payload_value = task.get('payload')
        if isinstance(payload_value, dict) is False:
            return None
        payload = cast(dict[str, Any], payload_value)
        env_value = payload.get('env')
        if isinstance(env_value, dict) is False:
            return None
        env = cast(dict[str, Any], env_value)

        try:
            network_id = self._parseConsistentID(env, 'ONID10', 'ONID16')
            transport_stream_id = self._parseConsistentID(env, 'TSID10', 'TSID16')
            service_id = self._parseConsistentID(env, 'SID10', 'SID16')
            event_id = self._parseConsistentID(env, 'EID10', 'EID16')
            start_time = self._parseDateTime(env.get('StartTime'))
            duration_seconds = float(env.get('DurationSecond', ''))
            if duration_seconds <= 0:
                return None
        except (TypeError, ValueError):
            return None

        title_value = env.get('Title') or payload.get('title') or task.get('title')
        service_name_value = env.get('ServiceName')
        original_file_path_value = payload.get('filePath') or env.get('FilePath')
        if not isinstance(title_value, str) or title_value.strip() == '':
            return None
        if not isinstance(service_name_value, str) or service_name_value.strip() == '':
            return None
        if not isinstance(original_file_path_value, str) or original_file_path_value.strip() == '':
            return None

        try:
            return HistoricalRecordingCandidate(
                source_kind = 'TVDashboardLog',
                source_record_id = event.task_id,
                box_file_id = event.box_file_id,
                box_upload_name = event.box_upload_name,
                original_file_path = original_file_path_value,
                title = title_value,
                service_name = service_name_value,
                network_id = network_id,
                transport_stream_id = transport_stream_id,
                service_id = service_id,
                event_id = event_id,
                start_time = start_time,
                duration_seconds = duration_seconds,
            )
        except ValidationError:
            return None

    @staticmethod
    def _parseConsistentID(env: dict[str, Any], decimal_key: str, hexadecimal_key: str) -> int:
        """
        EDCBが出力した10進値と16進値が同じIDを表すことを検証する。

        Args:
            env: EDCB環境変数。
            decimal_key: 10進値のキー。
            hexadecimal_key: 16進値のキー。

        Returns:
            検証済みID。

        Raises:
            ValueError: 値が欠けている、数値でない、または相互に不一致の場合。
        """

        decimal_text = env.get(decimal_key)
        hexadecimal_text = env.get(hexadecimal_key)
        if not isinstance(decimal_text, str) or not isinstance(hexadecimal_text, str):
            raise ValueError('EDCB identity is missing.')
        decimal_value = int(decimal_text, 10)
        hexadecimal_value = int(hexadecimal_text, 16)
        if decimal_value != hexadecimal_value:
            raise ValueError('EDCB decimal and hexadecimal identities do not match.')
        return decimal_value

    @staticmethod
    def _parseDateTime(value: object) -> datetime:
        """
        EDCB開始時刻をタイムゾーン付きdatetimeへ変換する。

        Args:
            value: tasks.json内のStartTime値。

        Returns:
            タイムゾーン付き開始時刻。

        Raises:
            ValueError: ISO 8601形式でない場合。
        """

        if not isinstance(value, str) or value.strip() == '':
            raise ValueError('EDCB start time is missing.')
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        # 古いTVDashboardがタイムゾーンなしで保存した場合は、EDCB実行環境のJSTとして扱う
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=JST)
        return parsed

    def _calculateSourceFingerprint(self) -> str:
        """
        読み取ったログ一式が後から差し替わっていないことを確認できるハッシュを計算する。

        Returns:
            tasks.jsonとJSONL一式のSHA-256フィンガープリント。

        Raises:
            TVDashboardLogSourceError: ファイルを読み取れない場合。
        """

        digest = hashlib.sha256()
        try:
            source_paths = [self._tasks_path, *sorted(self._task_log_directory.glob('*.jsonl'))]
            for source_path in source_paths:
                relative_path = source_path.relative_to(self._source_directory).as_posix()
                digest.update(relative_path.encode('utf-8'))
                digest.update(b'\0')
                with source_path.open('rb') as source_file:
                    while chunk := source_file.read(1024 * 1024):
                        digest.update(chunk)
                digest.update(b'\0')
        except OSError:
            raise TVDashboardLogSourceError('TVDashboard source fingerprint could not be calculated.') from None
        return digest.hexdigest()
