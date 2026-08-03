"""Shadow result report CLI (Issue #78): inspect persisted Shadow Mode
verification records.

*** READ-ONLY. No broker calls, no network, no writes: a missing DB file is
reported, never created; an invalid schema or corrupted row is an explicit
error, never silently skipped. ***

Usage::

    python -m execution.shadow_report                       # list all results
    python -m execution.shadow_report --execution-request-id exec-1
    python -m execution.shadow_report --source-signal-id signal-1
    python -m execution.shadow_report --decision rejected   # or: allowed
    python -m execution.shadow_report --json                # machine-readable

Exit codes: 0 = report produced (an initialized-but-empty store IS a valid
report); 1 = missing DB file, unknown id, schema/integrity failure; 2 = usage
error. ``--json`` output is pure ASCII and carries no log lines (production
logs go to stderr), so it parses on any console encoding (cp932 included).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from execution import shadow_store
from execution.cli_streams import encoding_safe_streams
from execution.shadow_models import ShadowResult

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE_ERROR = 2


def result_payload(result: ShadowResult) -> dict:
    """JSON-able projection of one shadow result (also reused by the shadow
    runner's ``--json`` output). Decimals become canonical strings; no broker
    outcome fields exist to project (shadow records fabricate none)."""
    request = result.request
    decision = result.decision
    market = result.market
    return {
        "shadow_result_id": result.shadow_result_id,
        "execution_request_id": request.execution_request_id,
        "signal_id": request.signal_id,
        "strategy_id": request.strategy_id,
        "symbol": request.symbol,
        "timeframe": result.timeframe,
        "side": request.side.value,
        "order_type": request.order_type.value,
        "quantity": str(request.quantity),
        "requested_price": None if request.requested_price is None else str(request.requested_price),
        "stop_loss_price": None if request.stop_loss_price is None else str(request.stop_loss_price),
        "take_profit_price": None
        if request.take_profit_price is None
        else str(request.take_profit_price),
        "requested_at_utc": request.requested_at_utc.isoformat(),
        "client_order_id": result.client_order_id,
        "execution_mode": decision.execution_mode,
        "allowed": decision.allowed,
        "rejection_codes": list(decision.rejection_codes),
        "rejections": [
            {"code": entry.code.value, "message": entry.message} for entry in decision.rejections
        ],
        "policy_id": decision.policy_id,
        "connection_target": decision.connection_target,
        "checked_at_utc": decision.checked_at_utc.isoformat(),
        "intended_order": {
            "client_order_id": result.intended_order.client_order_id,
            "symbol": result.intended_order.symbol,
            "side": result.intended_order.side.value,
            "order_type": result.intended_order.order_type.value,
            "quantity": str(result.intended_order.quantity),
            "limit_price": None
            if result.intended_order.limit_price is None
            else str(result.intended_order.limit_price),
        },
        "market": {
            "symbol": market.symbol,
            "reference_price": None
            if market.reference_price is None
            else str(market.reference_price),
            "bid": None if market.bid is None else str(market.bid),
            "ask": None if market.ask is None else str(market.ask),
            "spread": None if market.spread is None else str(market.spread),
            "observed_at_utc": market.observed_at_utc.isoformat(),
            "market_open": market.market_open,
            "tradable": market.tradable,
        },
        "account_id": decision.account_id,
        "position_count": decision.position_count,
        "safety_decision_id": result.safety_decision_id,
        "policy_snapshot": result.policy_snapshot,
        "created_at_utc": result.created_at_utc.isoformat(),
    }


def _render_summary_line(result: ShadowResult, out) -> None:
    verdict = "ALLOWED " if result.allowed else "REJECTED"
    codes = "" if result.allowed else " codes=" + ",".join(result.rejection_codes)
    print(
        f"{result.shadow_result_id:>5} {verdict} {result.request.execution_request_id} "
        f"signal={result.request.signal_id} {result.request.symbol} "
        f"{result.request.side.value}/{result.request.order_type.value} "
        f"qty={result.request.quantity} created={result.created_at_utc.isoformat()}{codes}",
        file=out,
    )


def _render_detail(result: ShadowResult, out) -> None:
    payload = result_payload(result)
    print(f"shadow_result_id: {payload['shadow_result_id']}", file=out)
    print(
        f"decision: {'ALLOWED' if payload['allowed'] else 'REJECTED'} "
        f"(execution_mode={payload['execution_mode']}, policy_id={payload['policy_id']}, "
        f"connection_target={payload['connection_target']})",
        file=out,
    )
    for entry in payload["rejections"]:
        print(f"  - {entry['code']}: {entry['message']}", file=out)
    print(
        f"request: execution_request_id={payload['execution_request_id']} "
        f"signal_id={payload['signal_id']} strategy_id={payload['strategy_id']}",
        file=out,
    )
    print(
        f"  {payload['symbol']} {payload['side']}/{payload['order_type']} "
        f"qty={payload['quantity']} requested_price={payload['requested_price']} "
        f"sl={payload['stop_loss_price']} tp={payload['take_profit_price']} "
        f"timeframe={payload['timeframe']}",
        file=out,
    )
    intended = payload["intended_order"]
    print(
        f"intended order (NOT submitted): client_order_id={intended['client_order_id']} "
        f"{intended['symbol']} {intended['side']}/{intended['order_type']} "
        f"qty={intended['quantity']} limit_price={intended['limit_price']}",
        file=out,
    )
    market = payload["market"]
    print(
        f"market: ref={market['reference_price']} bid={market['bid']} ask={market['ask']} "
        f"spread={market['spread']} observed_at={market['observed_at_utc']} "
        f"open={market['market_open']} tradable={market['tradable']}",
        file=out,
    )
    print(
        f"account_id={payload['account_id']} position_count={payload['position_count']} "
        f"safety_decision_id={payload['safety_decision_id']}",
        file=out,
    )
    print(
        f"checked_at={payload['checked_at_utc']} requested_at={payload['requested_at_utc']} "
        f"created_at={payload['created_at_utc']}",
        file=out,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m execution.shadow_report",
        description=(
            "Inspect persisted Shadow Mode verification records (Issue #78). "
            "Read-only: never creates or modifies the DB."
        ),
    )
    parser.add_argument(
        "--shadow-db",
        type=Path,
        default=None,
        help="shadow result DB path (default: SHADOW_RESULT_DB_PATH or the packaged default)",
    )
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument(
        "--execution-request-id", default=None, help="show the record for one execution request"
    )
    selector.add_argument(
        "--source-signal-id", default=None, help="show the record for one source signal"
    )
    parser.add_argument(
        "--decision",
        choices=("allowed", "rejected"),
        default=None,
        help="list only gate-allowed or gate-rejected records",
    )
    parser.add_argument(
        "--json", action="store_true", help="print a machine-readable JSON report to stdout"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    with encoding_safe_streams():
        return _run_cli(argv)


def _run_cli(argv: list[str] | None) -> int:
    args = _build_parser().parse_args(argv)
    db_path = Path(args.shadow_db) if args.shadow_db is not None else shadow_store.shadow_db_path()
    if not db_path.exists():
        # Read-only contract: a missing store is reported, never created.
        print(f"shadow result DB does not exist: {db_path}", file=sys.stderr)
        return EXIT_FAILURE

    try:
        if args.execution_request_id is not None:
            result = shadow_store.get_shadow_result(args.execution_request_id, db_path=db_path)
            if result is None:
                print(
                    f"no shadow result for execution_request_id {args.execution_request_id!r}",
                    file=sys.stderr,
                )
                return EXIT_FAILURE
            results = (result,)
            detail = True
        elif args.source_signal_id is not None:
            result = shadow_store.get_shadow_result_by_signal_id(
                args.source_signal_id, db_path=db_path
            )
            if result is None:
                print(
                    f"no shadow result for source signal id {args.source_signal_id!r}",
                    file=sys.stderr,
                )
                return EXIT_FAILURE
            results = (result,)
            detail = True
        else:
            allowed = None if args.decision is None else (args.decision == "allowed")
            results = shadow_store.list_shadow_results(allowed=allowed, db_path=db_path)
            detail = False
    except shadow_store.ShadowStoreError as exc:
        print(f"shadow report failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    if args.json:
        # Pure-ASCII JSON: machine-readable output must never depend on the
        # console encoding, and stdout carries no log lines.
        payload = {
            "shadow_db": str(db_path),
            "count": len(results),
            "results": [result_payload(result) for result in results],
        }
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return EXIT_OK

    if not results:
        print("no shadow results")
        return EXIT_OK
    if detail:
        _render_detail(results[0], sys.stdout)
    else:
        for result in results:
            _render_summary_line(result, sys.stdout)
        print(f"total {len(results)}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
