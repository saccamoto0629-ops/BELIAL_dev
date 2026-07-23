"""Read-only CLI for Shadow Mode verification records (Issue #78).

Usage (from the repository root)::

    python -m execution.shadow_report list [--limit N] [--db PATH]
    python -m execution.shadow_report show EXECUTION_REQUEST_ID [--db PATH]

STRICTLY READ-ONLY: this CLI only calls the shadow store's read helpers,
which never create a missing DB file, never migrate and never write -- a
missing DB simply reports "no records" (exit 0). Broken schemas or
tampered records are reported as errors (exit 1) instead of being trusted.
Output order is deterministic (observed_at_utc, then insertion order).

Exit codes: 0 = success (including an empty listing), 1 = record/store
error or unknown execution_request_id, 2 = usage error (argparse).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from execution.shadow_store import (
    ShadowExecutionRecord,
    ShadowStoreError,
    SQLiteShadowExecutionStore,
    shadow_db_path,
)


def _store(args: argparse.Namespace) -> SQLiteShadowExecutionStore:
    return SQLiteShadowExecutionStore(Path(args.db) if args.db else shadow_db_path())


def _decision_word(record: ShadowExecutionRecord) -> str:
    return "ALLOWED" if record.decision.allowed else "REJECTED"


def _format_list_line(record: ShadowExecutionRecord) -> str:
    request, decision = record.request, record.decision
    codes = ",".join(decision.rejection_codes) if decision.rejections else "-"
    return " | ".join(
        (
            record.observed_at_utc.isoformat(),
            request.signal_id,
            request.execution_request_id,
            request.symbol,
            request.side.value,
            str(request.quantity),
            _decision_word(record),
            codes,
        )
    )


def _print_detail(record: ShadowExecutionRecord) -> None:
    request, decision = record.request, record.decision
    intended = record.intended_order

    def line(name: str, value) -> None:
        print(f"{name}: {'-' if value is None else value}")

    line("execution_mode", record.execution_mode.value)
    line("observed_at_utc", record.observed_at_utc.isoformat())
    line("execution_request_id", request.execution_request_id)
    line("signal_id", request.signal_id)
    line("strategy_id", request.strategy_id)
    line("symbol", request.symbol)
    line("side", request.side.value)
    line("order_type", request.order_type.value)
    line("quantity", request.quantity)
    line("requested_price", request.requested_price)
    line("stop_loss_price", request.stop_loss_price)
    line("take_profit_price", request.take_profit_price)
    line("requested_at_utc", request.requested_at_utc.isoformat())
    line("gate_decision", _decision_word(record))
    line("checked_at_utc", decision.checked_at_utc.isoformat())
    line("policy_id", decision.policy_id)
    line("connection_mode", decision.execution_mode)
    line("connection_target", decision.connection_target)
    line("account_id", decision.account_id)
    line("position_count", decision.position_count)
    print("intended_order (never submitted; no client_order_id exists in shadow):")
    for key in sorted(intended):
        print(f"  {key}: {'-' if intended[key] is None else intended[key]}")
    if decision.rejections:
        print("rejections:")
        for rejection in decision.rejections:
            print(f"  {rejection.code.value}: {rejection.message}")
    else:
        print("rejections: none")


def _run_list(args: argparse.Namespace) -> int:
    records = _store(args).list_records(limit=args.limit)
    if not records:
        print("no shadow execution records")
        return 0
    print(
        "observed_at_utc | signal_id | execution_request_id | symbol | side | "
        "quantity | gate | rejection_codes"
    )
    for record in records:
        print(_format_list_line(record))
    return 0


def _run_show(args: argparse.Namespace) -> int:
    record = _store(args).get_by_execution_request_id(args.execution_request_id)
    if record is None:
        print(
            f"no shadow execution record for execution_request_id "
            f"{args.execution_request_id!r}",
            file=sys.stderr,
        )
        return 1
    _print_detail(record)
    return 0


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m execution.shadow_report",
        description=(
            "Read-only report of Shadow Mode verification records "
            "(Issue #78). Never creates, migrates or writes the DB."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_db_argument(subparser):
        subparser.add_argument(
            "--db",
            default=None,
            help="Shadow execution DB path (default: SHADOW_EXECUTION_DB_PATH or "
            "execution/data/shadow_executions.sqlite3).",
        )

    list_parser = subparsers.add_parser("list", help="List shadow records (deterministic order).")
    add_db_argument(list_parser)
    list_parser.add_argument("--limit", type=int, default=None, help="Maximum records to list.")
    list_parser.set_defaults(handler=_run_list)

    show_parser = subparsers.add_parser("show", help="Show one shadow record in full detail.")
    add_db_argument(show_parser)
    show_parser.add_argument("execution_request_id")
    show_parser.set_defaults(handler=_run_show)

    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return args.handler(args)
    except ShadowStoreError as exc:
        print(f"shadow report failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
