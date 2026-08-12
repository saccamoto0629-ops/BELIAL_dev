r"""Manual broker reconciliation CLI (Issue #105) -- RECONCILE-ONLY.

*** NO ORDER IS EVER PLACED, CANCELLED OR MODIFIED FROM THIS CLI. ***

``python -m execution.reconciliation_runner`` runs ONE detection-only
reconciliation pass (Issues #103 / #105 scope: orders + positions / trades /
SL / TP / closed-trade evidence) against either

- ``--broker oanda-demo`` -- the OANDA DEMO account configured via
  ``OANDA_API_TOKEN`` / ``OANDA_ACCOUNT_ID`` (Issue #79 adapter). The
  adapter itself refuses every non-demo endpoint at construction
  (fail-closed allowlist); this CLI adds nothing that could weaken that,
  and it does NOT register the adapter with the Safety Gate's submit path
  (``demo`` stays absent from ``BROKER_TYPE_BY_MODE`` -- reconciliation
  talks to the broker directly, query operations only). It never touches
  the live path either: this CLI has no ``--broker oanda-live`` option.
- ``--broker fake`` -- an empty in-memory FakeBroker. A plumbing /
  diagnostics path: it proves the CLI wiring (DB resolution, dry-run
  no-effect, output shape) without any network. It seeds no data, so a run
  compares the execution store against an empty broker.

Reconcile-only guarantee, enforced structurally: the broker handed to the
reconciliation service is wrapped in :class:`ReconcileOnlyBroker`, whose
``submit_order`` / ``cancel_order`` / ``get_account_state`` RAISE
unconditionally. Only query operations pass through (``get_order``,
``get_positions`` and the optional listing capabilities). The service itself
is detection-only (Issue #103 contract) and the position domain has no
repair path at all (Issue #105) -- the wrapper is defense in depth, not the
only line.

Modes (aligned with the repository's dry-run vocabulary):

- default: the run is recorded to the reconciliation audit DB
  (``init_reconciliation_db`` + append; both idempotent per Issue #103).
- ``--dry-run``: compare and report ONLY -- the reconciliation DB is not
  created, not migrated and not written; a missing execution DB reads as
  empty and is not created either.

Exit codes (shared execution-CLI contract): 0 = ran, no discrepancies and
no partial failures; 1 = discrepancies found or a partial failure (some
comparison could not run); 2 = usage / configuration error (including every
non-demo OANDA endpoint refusal). ``--json`` prints one pure-ASCII JSON
document to stdout.

Secrets: the OANDA token / account id / endpoint are read from the
environment by the adapter and NEVER printed by this CLI -- output and logs
identify the connection as ``demo / oanda-demo`` (the adapter's constant
identity labels) only.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from broker.fake_broker import FakeBroker
from execution import execution_store, reconciliation_store
from execution.broker_reconciliation import BrokerOrderReconciliationService
from execution.cli_streams import encoding_safe_streams
from execution.reconciliation_models import ReconciliationRun

EXIT_OK = 0
EXIT_DISCREPANCIES = 1
EXIT_USAGE_ERROR = 2

_BROKER_FAKE = "fake"
_BROKER_OANDA_DEMO = "oanda-demo"


class ReconcileOnlyViolationError(RuntimeError):
    """A mutating (or account-reading) broker operation was reached from the
    reconcile-only CLI. This must never happen; raising is the guarantee."""


class ReconcileOnlyBroker:
    """Query-only facade over a broker (Issue #105 defense in depth).

    ``submit_order`` / ``cancel_order`` raise: a reconcile-only run must be
    UNABLE to place or cancel anything, independent of the service's own
    detection-only contract. ``get_account_state`` raises too -- reconciliation
    never needs account data, so none is fetched (and none can leak into
    output). The optional listing capabilities (``list_orders`` /
    ``get_open_trades`` / ``get_closed_trades``) are mirrored as INSTANCE
    attributes exactly when the wrapped broker provides them, so the facade
    satisfies the same optional Protocols as the broker it wraps and no
    capability is fabricated. Instance attributes (not ``__getattr__``
    delegation) on purpose: since Python 3.12 a ``runtime_checkable``
    Protocol ``isinstance`` check uses ``inspect.getattr_static``, which
    deliberately ignores ``__getattr__`` -- statically bound methods are
    seen by both the 3.11 and 3.12+ semantics.
    """

    def __init__(self, inner):
        self._inner = inner
        from broker.broker_interface import (
            ClosedTradeListingBroker,
            OrderListingBroker,
            TradeListingBroker,
        )

        if isinstance(inner, OrderListingBroker):
            self.list_orders = inner.list_orders
        if isinstance(inner, TradeListingBroker):
            self.get_open_trades = inner.get_open_trades
        if isinstance(inner, ClosedTradeListingBroker):
            self.get_closed_trades = inner.get_closed_trades

    def submit_order(self, request):
        raise ReconcileOnlyViolationError(
            "reconcile-only: submit_order must never be called"
        )

    def cancel_order(self, client_order_id):
        raise ReconcileOnlyViolationError(
            "reconcile-only: cancel_order must never be called"
        )

    def get_account_state(self):
        raise ReconcileOnlyViolationError(
            "reconcile-only: reconciliation never reads account state"
        )

    def get_order(self, client_order_id):
        return self._inner.get_order(client_order_id)

    def get_positions(self):
        return self._inner.get_positions()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m execution.reconciliation_runner",
        description=(
            "Run ONE reconcile-only broker reconciliation pass (Issue #105): "
            "orders + positions / trades / SL / TP. Detection and classification "
            "only -- no order, cancel, close or SL/TP change is ever sent."
        ),
    )
    parser.add_argument(
        "--broker",
        required=True,
        choices=(_BROKER_OANDA_DEMO, _BROKER_FAKE),
        help=(
            "'oanda-demo': the OANDA DEMO account from OANDA_API_TOKEN / "
            "OANDA_ACCOUNT_ID (non-demo endpoints are refused, fail-closed); "
            "'fake': an empty in-memory FakeBroker (plumbing / diagnostics, no network)"
        ),
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="reconciliation run id (default: a fresh 'recon-<utc>-<suffix>' value; "
        "recording the same id twice is refused by the store)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compare and report only: the reconciliation DB is not created, not "
        "migrated and not written",
    )
    parser.add_argument(
        "--execution-db",
        type=Path,
        default=None,
        help="execution order DB path (default: EXECUTION_ORDER_DB_PATH or the "
        "packaged default; read-only here, never created)",
    )
    parser.add_argument(
        "--reconciliation-db",
        type=Path,
        default=None,
        help="reconciliation audit DB path (default: BROKER_RECONCILIATION_DB_PATH "
        "or the packaged default)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="directory holding both DBs (execution_orders.sqlite3 / "
        "broker_reconciliation.sqlite3); overridden by --execution-db / "
        "--reconciliation-db",
    )
    parser.add_argument(
        "--orders-only",
        action="store_true",
        help="restrict the pass to the Issue #103 order scope (skip positions / "
        "trades / SL / TP)",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="fixed ISO-8601 UTC observation clock for deterministic runs "
        "(default: the current UTC time)",
    )
    parser.add_argument(
        "--json", action="store_true", help="print a machine-readable pure-ASCII JSON outcome"
    )
    return parser


def _resolve_db_paths(args) -> tuple[Path, Path]:
    execution_db = args.execution_db
    reconciliation_db = args.reconciliation_db
    if args.data_dir is not None:
        if execution_db is None:
            execution_db = args.data_dir / "execution_orders.sqlite3"
        if reconciliation_db is None:
            reconciliation_db = args.data_dir / "broker_reconciliation.sqlite3"
    if execution_db is None:
        execution_db = execution_store.execution_db_path()
    if reconciliation_db is None:
        reconciliation_db = reconciliation_store.reconciliation_db_path()
    return Path(execution_db), Path(reconciliation_db)


def _build_broker(kind: str):
    """Construct the (unwrapped) broker for ``kind``.

    ``oanda-demo`` raises ``ValueError`` with the adapter's fixed, secret-free
    messages when the environment is missing or names a non-demo endpoint --
    the CLI surfaces them verbatim as usage errors (fail-closed startup
    refusal). The adapter import is lazy so the fake path never touches the
    HTTP stack."""
    if kind == _BROKER_FAKE:
        return FakeBroker(), "fake / fake"
    from broker.oanda_demo_adapter import OandaDemoAdapter, OandaDemoConfig

    adapter = OandaDemoAdapter(OandaDemoConfig.from_env())
    identity = adapter.connection_identity()
    return adapter, f"{identity.mode} / {identity.target}"


def _parse_now(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("--now must be timezone-aware")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError("--now must be UTC (offset 0)")
    return value


def _default_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"recon-{stamp}-{uuid.uuid4().hex[:8]}"


def _order_discrepancy_payload(discrepancy) -> dict:
    return {
        "discrepancy_type": discrepancy.discrepancy_type.value,
        "target_type": discrepancy.target_type.value,
        "target_id": discrepancy.target_id,
        "resolution_category": discrepancy.resolution_category.value,
        "internal_state": None
        if discrepancy.internal_state is None
        else discrepancy.internal_state.value,
        "broker_state": None
        if discrepancy.broker_state is None
        else discrepancy.broker_state.value,
        "client_order_id": discrepancy.client_order_id,
        "broker_order_id": discrepancy.broker_order_id,
        "broker_error_code": None
        if discrepancy.broker_error_code is None
        else discrepancy.broker_error_code.value,
        "detail": discrepancy.detail,
    }


def _position_discrepancy_payload(discrepancy) -> dict:
    return {
        "discrepancy_type": discrepancy.discrepancy_type.value,
        "target_type": discrepancy.target_type.value,
        "target_id": discrepancy.target_id,
        "resolution_category": discrepancy.resolution_category.value,
        "symbol": discrepancy.symbol,
        "internal_value": discrepancy.internal_value,
        "broker_value": discrepancy.broker_value,
        "execution_request_id": discrepancy.execution_request_id,
        "client_order_id": discrepancy.client_order_id,
        "broker_trade_id": discrepancy.broker_trade_id,
        "protection_order_id": discrepancy.protection_order_id,
        "broker_transaction_ids": list(discrepancy.broker_transaction_ids),
        "detail": discrepancy.detail,
    }


def _run_payload(run: ReconciliationRun, *, broker_label: str, dry_run: bool, recorded_to) -> dict:
    counts = run.resolution_counts()
    summary = run.position_summary
    return {
        "run_id": run.run_id,
        "mode": run.mode.value,
        "reconcile_only": True,
        "dry_run": dry_run,
        "broker": broker_label,
        "recorded_to": None if recorded_to is None else str(recorded_to),
        "checked_order_count": run.checked_order_count,
        "broker_listing_error": run.broker_listing_error,
        "order_discrepancies": [
            _order_discrepancy_payload(entry) for entry in run.discrepancies
        ],
        "order_resolution_counts": {
            category.value: count for category, count in counts.items()
        },
        "position_scope": None
        if summary is None
        else {
            "checked_position_count": summary.checked_position_count,
            "checked_trade_count": summary.checked_trade_count,
            "checked_closed_trade_count": summary.checked_closed_trade_count,
            "internal_exposure_error": summary.internal_exposure_error,
            "position_listing_error": summary.position_listing_error,
            "trade_listing_error": summary.trade_listing_error,
            "closed_trade_listing_error": summary.closed_trade_listing_error,
            "excluded_symbols": list(summary.excluded_symbols),
        },
        "position_discrepancies": [
            _position_discrepancy_payload(entry) for entry in run.position_discrepancies
        ],
        "has_partial_failures": run.has_partial_failures,
        "discrepancy_count": len(run.discrepancies) + len(run.position_discrepancies),
    }


def _print_human(payload: dict) -> None:
    print(
        f"reconciliation run {payload['run_id']} ({payload['mode']}, reconcile-only) "
        f"broker={payload['broker']}"
    )
    if payload["dry_run"]:
        print("dry-run: nothing was persisted (no DB created, migrated or written)")
    else:
        print(f"recorded to: {payload['recorded_to']}")
    print(f"checked orders: {payload['checked_order_count']}")
    scope = payload["position_scope"]
    if scope is None:
        print("position scope: not included (--orders-only)")
    else:
        print(
            "position scope: positions={0} open_trades={1} closed_trades={2}".format(
                scope["checked_position_count"],
                scope["checked_trade_count"],
                scope["checked_closed_trade_count"],
            )
        )
        for key in (
            "internal_exposure_error",
            "position_listing_error",
            "trade_listing_error",
            "closed_trade_listing_error",
        ):
            if scope[key] is not None:
                print(f"  {key}: {scope[key]}")
        if scope["excluded_symbols"]:
            print(f"  excluded_symbols: {', '.join(scope['excluded_symbols'])}")
    if payload["broker_listing_error"] is not None:
        print(f"broker order listing: {payload['broker_listing_error']}")
    for entry in payload["order_discrepancies"]:
        print(
            "order discrepancy: type={0} target={1}:{2} resolution={3} detail={4}".format(
                entry["discrepancy_type"],
                entry["target_type"],
                entry["target_id"],
                entry["resolution_category"],
                entry["detail"],
            )
        )
    for entry in payload["position_discrepancies"]:
        print(
            "position discrepancy: type={0} target={1}:{2} symbol={3} internal={4} "
            "broker={5} detail={6}".format(
                entry["discrepancy_type"],
                entry["target_type"],
                entry["target_id"],
                entry["symbol"],
                entry["internal_value"],
                entry["broker_value"],
                entry["detail"],
            )
        )
    verdict = (
        "CLEAN"
        if payload["discrepancy_count"] == 0 and not payload["has_partial_failures"]
        else "DISCREPANCIES / PARTIAL FAILURES"
    )
    print(
        f"result: {verdict} (discrepancies={payload['discrepancy_count']}, "
        f"partial_failures={payload['has_partial_failures']})"
    )


def _run_cli(argv: list[str] | None) -> int:
    args = _build_parser().parse_args(argv)

    fixed_now = None
    if args.now is not None:
        try:
            fixed_now = _parse_now(args.now)
        except ValueError as exc:
            print(f"invalid --now value: {exc}", file=sys.stderr)
            return EXIT_USAGE_ERROR

    try:
        broker, broker_label = _build_broker(args.broker)
    except ValueError as exc:
        # The adapter's construction-time refusals (missing credentials, any
        # non-demo endpoint) arrive here with fixed, secret-free messages.
        print(f"broker configuration refused: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR

    execution_db, reconciliation_db = _resolve_db_paths(args)
    run_id = args.run_id if args.run_id is not None else _default_run_id()
    if not run_id.strip():
        print("--run-id must be a non-empty string", file=sys.stderr)
        return EXIT_USAGE_ERROR

    service = BrokerOrderReconciliationService(
        broker=ReconcileOnlyBroker(broker),
        execution_db_path=execution_db,
        reconciliation_db_path=None if args.dry_run else reconciliation_db,
        clock=None if fixed_now is None else (lambda: fixed_now),
    )
    include_position_scope = not args.orders_only
    try:
        if args.dry_run:
            run = service.detect(run_id=run_id, include_position_scope=include_position_scope)
        else:
            run = service.run(run_id=run_id, include_position_scope=include_position_scope)
    except (
        execution_store.ExecutionStoreError,
        reconciliation_store.ReconciliationStoreError,
    ) as exc:
        print(f"reconciliation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_DISCREPANCIES

    payload = _run_payload(
        run,
        broker_label=broker_label,
        dry_run=args.dry_run,
        recorded_to=None if args.dry_run else reconciliation_db,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    else:
        _print_human(payload)

    if payload["discrepancy_count"] > 0 or payload["has_partial_failures"]:
        return EXIT_DISCREPANCIES
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    with encoding_safe_streams():
        return _run_cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
