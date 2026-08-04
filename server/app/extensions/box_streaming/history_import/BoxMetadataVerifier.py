from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import ValidationError

from app.extensions.box_streaming.BoxClient import BoxAPIError, BoxClient
from app.extensions.box_streaming.history_import.BoxUsageBudget import BoxUsageBudget
from app.extensions.box_streaming.history_import.Models import (
    HistoricalBoxMetadata,
    HistoricalImportPlan,
    HistoricalVerificationResult,
)


class BoxMetadataVerifierError(Exception):
    """Boxメタデータ検証を安全に継続できない場合の例外。"""


class BoxMetadataVerifier:
    """取り込み候補のBoxメタデータを低速・再開可能な形で検証する。"""

    def __init__(
        self,
        plan: HistoricalImportPlan,
        verification_log_path: Path,
        usage_budget: BoxUsageBudget,
    ) -> None:
        """
        Args:
            plan: オフライン生成済みの取り込み計画。
            verification_log_path: 1件ずつ追記する再開用JSONL。
            usage_budget: Box API回数と通信量の安全予算。
        """

        self._plan = plan
        self._verification_log_path = verification_log_path
        self._usage_budget = usage_budget

    async def run(self) -> dict[str, int]:
        """
        未検証候補だけをBox APIで確認し、結果をJSONLへ永続化する。

        Returns:
            全計画に対する検証済み・拒否・今回API確認した件数。

        Raises:
            BoxMetadataVerifierError: 認証、Box API、または検証ログの問題で継続できない場合。
        """

        results_by_file_id = self.loadResults(self._verification_log_path)
        verified_this_run = 0
        credentials = BoxClient.loadCredentialsFromEnvironment()
        async with httpx.AsyncClient(
            http2 = True,
            follow_redirects = True,
            timeout = httpx.Timeout(connect=20.0, read=120.0, write=20.0, pool=20.0),
            event_hooks = {
                'request': [self._usage_budget.onRequest],
                'response': [self._usage_budget.onResponse],
            },
        ) as http_client:
            client = BoxClient(credentials, set(), http_client)
            for candidate in self._plan.candidates:
                candidate_fingerprint = candidate.calculateFingerprint()
                previous_result = results_by_file_id.get(candidate.box_file_id)
                if (
                    previous_result is not None
                    and previous_result.candidate_fingerprint == candidate_fingerprint
                ):
                    continue

                client.registerDiscoveredFile(candidate.box_file_id)
                try:
                    metadata = await client.getFileMetadata(candidate.box_file_id)
                except BoxAPIError as ex:
                    # 存在しないファイルだけは候補単位の拒否として確定し、認証・権限・一時障害は全体を止める
                    if ex.status_code == 404:
                        result = HistoricalVerificationResult(
                            verified_at = datetime.now(UTC),
                            candidate_fingerprint = candidate_fingerprint,
                            box_file_id = candidate.box_file_id,
                            status = 'Rejected',
                            error_code = ex.code,
                        )
                        self._appendResult(result)
                        results_by_file_id[candidate.box_file_id] = result
                        verified_this_run += 1
                        continue
                    raise BoxMetadataVerifierError(
                        f'Box metadata verification was stopped. '
                        f'[status_code: {ex.status_code}][code: {ex.code}]'
                    ) from None

                if metadata.id != candidate.box_file_id:
                    raise BoxMetadataVerifierError('Box returned a different file ID than requested.')
                if Path(metadata.name).suffix.lower() != '.ts':
                    result = HistoricalVerificationResult(
                        verified_at = datetime.now(UTC),
                        candidate_fingerprint = candidate_fingerprint,
                        box_file_id = candidate.box_file_id,
                        status = 'Rejected',
                        error_code = 'not_mpeg_ts',
                    )
                else:
                    result = HistoricalVerificationResult(
                        verified_at = datetime.now(UTC),
                        candidate_fingerprint = candidate_fingerprint,
                        box_file_id = candidate.box_file_id,
                        status = 'Verified',
                        metadata = HistoricalBoxMetadata(
                            id = metadata.id,
                            name = metadata.name,
                            size = metadata.size,
                            sha1 = metadata.sha1,
                            created_at = self._parseOptionalDateTime(metadata.created_at),
                            modified_at = self._parseOptionalDateTime(metadata.modified_at),
                        ),
                    )
                self._appendResult(result)
                results_by_file_id[candidate.box_file_id] = result
                verified_this_run += 1

        current_results = [
            result
            for candidate in self._plan.candidates
            if (result := results_by_file_id.get(candidate.box_file_id)) is not None
            and result.candidate_fingerprint == candidate.calculateFingerprint()
        ]
        return {
            'planned': len(self._plan.candidates),
            'verified': sum(result.status == 'Verified' for result in current_results),
            'rejected': sum(result.status == 'Rejected' for result in current_results),
            'checked_this_run': verified_this_run,
            'remaining': len(self._plan.candidates) - len(current_results),
        }

    @staticmethod
    def loadResults(verification_log_path: Path) -> dict[str, HistoricalVerificationResult]:
        """
        追記型JSONLからBox file IDごとの最新検証結果を読み込む。

        Args:
            verification_log_path: 検証結果JSONL。

        Returns:
            Box file IDをキーにした最新検証結果。

        Raises:
            BoxMetadataVerifierError: JSONLが破損している場合。
        """

        if verification_log_path.exists() is False:
            return {}
        results: dict[str, HistoricalVerificationResult] = {}
        try:
            with verification_log_path.open('r', encoding='utf-8') as verification_log:
                for line_number, line in enumerate(verification_log, start=1):
                    if line.strip() == '':
                        continue
                    try:
                        result = HistoricalVerificationResult.model_validate_json(line)
                    except ValidationError:
                        raise BoxMetadataVerifierError(
                            f'Box verification log is invalid. [line: {line_number}]'
                        ) from None
                    results[result.box_file_id] = result
        except OSError:
            raise BoxMetadataVerifierError('Box verification log could not be read.') from None
        return results

    def _appendResult(self, result: HistoricalVerificationResult) -> None:
        """
        1件の検証結果をfsync付きで追記し、中断後も再開可能にする。

        Args:
            result: 永続化する検証結果。

        Raises:
            BoxMetadataVerifierError: 結果を保存できない場合。
        """

        self._verification_log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self._verification_log_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            with os.fdopen(descriptor, 'a', encoding='utf-8') as verification_log:
                verification_log.write(result.model_dump_json())
                verification_log.write('\n')
                verification_log.flush()
                os.fsync(verification_log.fileno())
            os.chmod(self._verification_log_path, 0o600)
        except OSError:
            raise BoxMetadataVerifierError('Box verification result could not be saved.') from None

    @staticmethod
    def _parseOptionalDateTime(value: str | None) -> datetime | None:
        """
        BoxのRFC 3339時刻をdatetimeへ変換する。

        Args:
            value: Box APIが返した時刻文字列。

        Returns:
            タイムゾーン付きdatetime、または未指定ならNone。

        Raises:
            BoxMetadataVerifierError: Boxが不正な時刻を返した場合。
        """

        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            raise BoxMetadataVerifierError('Box returned an invalid timestamp.') from None
        if parsed.tzinfo is None:
            raise BoxMetadataVerifierError('Box returned a timestamp without a timezone.')
        return parsed
