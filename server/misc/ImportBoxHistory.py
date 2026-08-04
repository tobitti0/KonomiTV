from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

from pydantic import ValidationError

from app.extensions.box_streaming.BoxClient import BoxConfigurationError
from app.extensions.box_streaming.history_import.BoxMetadataVerifier import (
    BoxMetadataVerifier,
    BoxMetadataVerifierError,
)
from app.extensions.box_streaming.history_import.BoxUsageBudget import (
    BoxUsageBudget,
    BoxUsageBudgetError,
)
from app.extensions.box_streaming.history_import.DatabaseImporter import (
    HistoricalDatabaseImporter,
    HistoricalDatabaseImporterError,
)
from app.extensions.box_streaming.history_import.Models import HistoricalImportPlan
from app.extensions.box_streaming.history_import.TVDashboardLogSource import (
    TVDashboardLogSource,
    TVDashboardLogSourceError,
)


DEFAULT_DATABASE_PATH = Path('data/database.sqlite')
DEFAULT_STATE_DIRECTORY = Path('data/box-history-import')


def CreateParser() -> argparse.ArgumentParser:
    """
    安全な段階実行だけを公開するコマンドラインパーサーを作成する。

    Returns:
        plan・verify・inspect・applyサブコマンドを持つパーサー。
    """

    parser = argparse.ArgumentParser(
        description='One-time Box historical recording importer for KonomiTV.',
    )
    subparsers = parser.add_subparsers(dest='command', required=True)

    plan_parser = subparsers.add_parser(
        'plan',
        help='Read TVDashboard logs and create an offline import plan.',
    )
    plan_parser.add_argument('--tvdashboard-logs', type=Path, required=True)
    plan_parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE_PATH)
    plan_parser.add_argument('--output', type=Path, default=DEFAULT_STATE_DIRECTORY / 'plan.json')

    verify_parser = subparsers.add_parser(
        'verify',
        help='Verify planned Box file IDs with strict request and byte budgets.',
    )
    verify_parser.add_argument('--plan', type=Path, default=DEFAULT_STATE_DIRECTORY / 'plan.json')
    verify_parser.add_argument(
        '--verification-log',
        type=Path,
        default=DEFAULT_STATE_DIRECTORY / 'verification.jsonl',
    )
    verify_parser.add_argument(
        '--usage-ledger',
        type=Path,
        default=DEFAULT_STATE_DIRECTORY / 'box-usage.json',
    )
    verify_parser.add_argument('--max-box-requests', type=int, default=2000)
    verify_parser.add_argument('--max-download-gib', type=float, default=1.0)
    verify_parser.add_argument('--request-interval-seconds', type=float, default=0.5)

    inspect_parser = subparsers.add_parser(
        'inspect',
        help='Recheck the production database without writing anything.',
    )
    inspect_parser.add_argument('--plan', type=Path, default=DEFAULT_STATE_DIRECTORY / 'plan.json')
    inspect_parser.add_argument(
        '--verification-log',
        type=Path,
        default=DEFAULT_STATE_DIRECTORY / 'verification.jsonl',
    )
    inspect_parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE_PATH)

    apply_parser = subparsers.add_parser(
        'apply',
        help='Back up the database and atomically import verified recordings.',
    )
    apply_parser.add_argument('--plan', type=Path, default=DEFAULT_STATE_DIRECTORY / 'plan.json')
    apply_parser.add_argument(
        '--verification-log',
        type=Path,
        default=DEFAULT_STATE_DIRECTORY / 'verification.jsonl',
    )
    apply_parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE_PATH)
    apply_parser.add_argument(
        '--backup-directory',
        type=Path,
        default=Path('data/backups/box-history-import'),
    )
    apply_parser.add_argument(
        '--report',
        type=Path,
        default=DEFAULT_STATE_DIRECTORY / 'apply-result.json',
    )
    apply_parser.add_argument(
        '--server-stopped',
        action='store_true',
        help='Assert that the KonomiTV server process has been stopped by the operator.',
    )
    apply_parser.add_argument(
        '--confirm',
        help='Must be exactly APPLY_BOX_HISTORY_IMPORT.',
    )
    return parser


def LoadPlan(plan_path: Path) -> HistoricalImportPlan:
    """
    取り込み計画を検証して読み込む。

    Args:
        plan_path: planサブコマンドが生成したJSON。

    Returns:
        検証済み取り込み計画。

    Raises:
        RuntimeError: 計画を安全に読み込めない場合。
    """

    try:
        return HistoricalImportPlan.model_validate_json(plan_path.read_text(encoding='utf-8'))
    except (OSError, ValidationError):
        raise RuntimeError('Import plan could not be read safely.') from None


def SaveJSON(output_path: Path, value: object) -> None:
    """
    秘密ではないがBox IDを含む実行データを、所有者だけ読める権限で原子的に保存する。

    Args:
        output_path: 保存先。
        value: JSONへ変換可能な値。

    Raises:
        RuntimeError: 保存できない場合。
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f'.{output_path.name}.tmp-{os.getpid()}')
    try:
        descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output_file:
            json.dump(value, output_file, ensure_ascii=False, indent=2)
            output_file.write('\n')
            output_file.flush()
            os.fsync(output_file.fileno())
        temporary_path.replace(output_path)
        os.chmod(output_path, 0o600)
    except OSError:
        raise RuntimeError('Output JSON could not be saved safely.') from None
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def LoadExistingBoxFileIDs(database_path: Path) -> set[str]:
    """
    計画作成時点の紐付け済み・保留中Box file IDを読み取り専用DBから取得する。

    Args:
        database_path: KonomiTV SQLite DB。

    Returns:
        自動取り込みから除外するBox file ID。

    Raises:
        RuntimeError: DBまたはBox拡張テーブルを読み取れない場合。
    """

    if database_path.is_file() is False:
        raise RuntimeError('KonomiTV database file was not found.')
    try:
        connection = sqlite3.connect(f'file:{database_path}?mode=ro', uri=True, timeout=5.0)
        try:
            linked_rows = connection.execute('SELECT box_file_id FROM box_recorded_files').fetchall()
            pending_rows = connection.execute('SELECT box_file_id FROM box_pending_recordings').fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        raise RuntimeError('Box recording tables could not be read from KonomiTV database.') from None
    return {str(row[0]) for row in [*linked_rows, *pending_rows]}


def RunPlan(args: argparse.Namespace) -> dict[str, object]:
    """
    TVDashboardログからオフライン計画を生成する。

    Args:
        args: planサブコマンド引数。

    Returns:
        画面へ表示する集計。
    """

    existing_box_file_ids = LoadExistingBoxFileIDs(args.database)
    plan = TVDashboardLogSource(args.tvdashboard_logs).createPlan(existing_box_file_ids)
    SaveJSON(args.output, plan.model_dump(mode='json'))
    return {
        'plan_path': str(args.output),
        'source_fingerprint': plan.source_fingerprint,
        **plan.summary.model_dump(),
    }


async def RunVerify(args: argparse.Namespace) -> dict[str, object]:
    """
    Box通信予算を適用しながら計画内のfile IDを検証する。

    Args:
        args: verifyサブコマンド引数。

    Returns:
        検証件数と累積Box使用量。
    """

    plan = LoadPlan(args.plan)
    max_download_bytes = int(args.max_download_gib * 1024 * 1024 * 1024)
    usage_budget = BoxUsageBudget(
        ledger_path = args.usage_ledger,
        batch_id = plan.source_fingerprint,
        max_api_requests = args.max_box_requests,
        max_download_bytes = max_download_bytes,
        minimum_request_interval_seconds = args.request_interval_seconds,
    )
    verifier = BoxMetadataVerifier(plan, args.verification_log, usage_budget)
    verification_summary = await verifier.run()
    ledger = usage_budget.ledger
    return {
        **verification_summary,
        'api_request_count': ledger.api_request_count,
        'downloaded_bytes': ledger.downloaded_bytes,
        'response_bytes': ledger.response_bytes,
        'rate_limit_response_count': ledger.rate_limit_response_count,
    }


def RunInspect(args: argparse.Namespace) -> dict[str, object]:
    """
    最新DBに対する適用予定を読み取り専用で再集計する。

    Args:
        args: inspectサブコマンド引数。

    Returns:
        適用可能・既存・保留件数。
    """

    plan = LoadPlan(args.plan)
    importer = HistoricalDatabaseImporter(args.database, Path('data/backups/box-history-import'))
    return importer.inspect(plan, args.verification_log)


def RunApply(args: argparse.Namespace) -> dict[str, object]:
    """
    明示確認後にDBバックアップと取り込みを実行する。

    Args:
        args: applyサブコマンド引数。

    Returns:
        バックアップと取り込みの最終結果。

    Raises:
        RuntimeError: サーバー停止または確認文字列が不足している場合。
    """

    if args.server_stopped is False:
        raise RuntimeError('Apply requires --server-stopped after the operator stops KonomiTV.')
    if args.confirm != 'APPLY_BOX_HISTORY_IMPORT':
        raise RuntimeError('Apply requires --confirm APPLY_BOX_HISTORY_IMPORT.')

    plan = LoadPlan(args.plan)
    importer = HistoricalDatabaseImporter(args.database, args.backup_directory)
    result = importer.apply(plan, args.verification_log)
    report = {
        'backup_path': str(result.backup_path),
        'backup_sha256': result.backup_sha256,
        'verified_count': result.verified_count,
        'imported_count': result.imported_count,
        'skipped_linked_count': result.skipped_linked_count,
        'skipped_pending_count': result.skipped_pending_count,
        'skipped_existing_program_count': result.skipped_existing_program_count,
    }
    SaveJSON(args.report, report)
    return {'report_path': str(args.report), **report}


def Main() -> None:
    """サブコマンドを実行し、番組名やBox IDを含まない集計だけを標準出力へ出す。"""

    parser = CreateParser()
    args = parser.parse_args()
    try:
        if args.command == 'plan':
            result = RunPlan(args)
        elif args.command == 'verify':
            result = asyncio.run(RunVerify(args))
        elif args.command == 'inspect':
            result = RunInspect(args)
        elif args.command == 'apply':
            result = RunApply(args)
        else:
            parser.error('Unknown command.')
            return
    except (
        BoxConfigurationError,
        BoxMetadataVerifierError,
        BoxUsageBudgetError,
        HistoricalDatabaseImporterError,
        RuntimeError,
        TVDashboardLogSourceError,
    ) as ex:
        print(json.dumps({'status': 'error', 'message': str(ex)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps({'status': 'ok', **result}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    Main()
