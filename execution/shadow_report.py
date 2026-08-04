"""Shadow result report CLI (Issue #78; provenance / claim / orphan rework
PR #100): inspect persisted Shadow Mode verification records.

*** READ-ONLY. No broker calls, no network, no writes: every DB access goes
through TRUE read-only connections (``execution.sqlite_ro`` -- ``mode=ro`` +
``PRAGMA query_only``), so a report can never create a missing file, journal
or WAL/SHM side files, and never changes content or mtime. An invalid schema
or corrupted row is an explicit error, never silently skipped. ***

Usage::

    python -m execution.shadow_report                        # newest first
    python -m execution.shadow_report --limit 20             # bounded listing
    python -m execution.shadow_report --execution-request-id exec-1
    python -m execution.shadow_report --source-signal-id signal-1
    python -m execution.shadow_report --safety-decision-id 7
    python -m execution.shadow_report --decision rejected    # or: allowed
    python -m execution.shadow_report --claims               # in-flight/stale claims
    python -m execution.shadow_report --orphan-decisions     # audit consistency
    python -m execution.shadow_report --json                 # machine-readable

Listing order is ``shadow_result_id DESC`` (recording order, newest first);
``--limit`` accepts 1..1000. Every record shows its input provenance (origin
/ signal / configuration / snapshot sources) and phase. ``--orphan-decisions``
cross-checks the Safety Decision store (``--decision-db``): decision rows for
a shadow request that are NOT the one referenced by the completed record are
orphans -- audit leftovers of completion failures that were later retried
(see ``execution/shadow_execution.py``); bounded by the number of retries.

Before anything is printed, every displayed record is cross-checked against
the Safety Decision store it references; ANY failure of that check --
including a decision store that is unusable as a database -- is normalized
into :class:`ShadowReportIntegrityError` and reported as a stable
``shadow report failed: ...`` line, never a traceback and never a partially
displayed record.

Exit codes: 0 = report produced (an initialized-but-empty store IS a valid
report); 1 = missing DB file, unknown id, schema/integrity failure; 2 = usage
error. ``--json`` output is pure ASCII and carries no log lines (production
logs go to stderr), so it parses on any console encoding (cp932 included).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

from execution import shadow_store
from execution.cli_streams import encoding_safe_streams
from execution.safety_gate import SafetyRejectionCode
from execution.safety_store import SafetyStoreError, SQLiteSafetyDecisionStore, safety_db_path
from execution.shadow_models import ShadowClaim, ShadowResult

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE_ERROR = 2


class ShadowReportIntegrityError(RuntimeError):
    """The report cannot vouch for what it would display.

    Every failure of the Safety Decision cross-check funnels through this
    type (PR #100 4th-review Major 1): a decision store that is empty, not a
    SQLite file, schema-broken, version-mismatched, physically corrupted or
    holds an unrestorable row is an integrity failure of the REPORT, not a
    crash. The CLI turns it into the same stable ``shadow report failed:
    ...`` line + exit 1 as every other integrity failure, so no traceback is
    ever shown and no unverifiable record reaches stdout.
    """


# Anything the decision-store read path can raise: the store's own errors
# (SafetySchemaError / SafetyIntegrityError share SafetyStoreError), raw
# SQLite failures for files that are not usable databases at all, and the
# value/type/key errors a JSON or column restore can produce.
_DECISION_STORE_FAILURES = (
    SafetyStoreError,
    sqlite3.Error,
    ValueError,  # includes json.JSONDecodeError
    TypeError,
    KeyError,
)


def result_payload(result: ShadowResult) -> dict:
    """JSON-able projection of one shadow result (also reused by the shadow
    runner's ``--json`` output). Decimals become canonical strings; no broker
    outcome fields exist to project (shadow records fabricate none)."""
    request = result.request
    decision = result.decision
    market = result.market
    account = result.account.account
    return {
        "shadow_result_id": result.shadow_result_id,
        "phase": "completed",
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
        "provenance": {
            "input_origin": result.provenance.input_origin,
            "signal_source": result.provenance.signal_source,
            "signal_observed_at_utc": None
            if result.provenance.signal_observed_at_utc is None
            else result.provenance.signal_observed_at_utc.isoformat(),
            "config_source": result.provenance.config_source,
            "config_version": result.provenance.config_version,
            "market_source": result.provenance.market_source,
            "account_source": result.provenance.account_source,
        },
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
        "account_snapshot": {
            "account_id": account.account_id,
            "currency": account.currency,
            "balance": str(account.balance),
            "equity": str(account.equity),
            "margin_available": str(account.margin_available),
            "retrieved_at_utc": account.retrieved_at_utc.isoformat(),
            "positions": [
                {
                    "symbol": position.symbol,
                    "side": position.side.value,
                    "quantity": str(position.quantity),
                    "average_entry_price": str(position.average_entry_price),
                    "retrieved_at_utc": position.retrieved_at_utc.isoformat(),
                }
                for position in result.account.positions
            ],
        },
        "account_id": decision.account_id,
        "position_count": decision.position_count,
        "safety_decision_id": result.safety_decision_id,
        "policy_snapshot": result.policy_snapshot,
        "claim_owner_id": result.claim_owner_id,
        "claimed_at_utc": result.claimed_at_utc.isoformat(),
        "completed_at_utc": result.completed_at_utc.isoformat(),
    }


def claim_payload(claim: ShadowClaim) -> dict:
    return {
        "shadow_result_id": claim.shadow_result_id,
        "phase": "claimed",
        "execution_request_id": claim.execution_request_id,
        "signal_id": claim.signal_id,
        "client_order_id": claim.client_order_id,
        "claim_owner_id": claim.claim_owner_id,
        "claim_attempt_id": claim.claim_attempt_id,
        "claim_generation": claim.claim_generation,
        "claimed_at_utc": claim.claimed_at_utc.isoformat(),
        "lease_expires_at_utc": claim.lease_expires_at_utc.isoformat(),
        "input_origin": claim.input_origin,
    }


def _render_summary_line(result: ShadowResult, out) -> None:
    verdict = "ALLOWED " if result.allowed else "REJECTED"
    codes = "" if result.allowed else " codes=" + ",".join(result.rejection_codes)
    print(
        f"{result.shadow_result_id:>5} {verdict} {result.request.execution_request_id} "
        f"origin={result.provenance.input_origin} signal={result.request.signal_id} "
        f"{result.request.symbol} {result.request.side.value}/{result.request.order_type.value} "
        f"qty={result.request.quantity} decision_id={result.safety_decision_id} "
        f"completed={result.completed_at_utc.isoformat()}{codes}",
        file=out,
    )


def _render_detail(result: ShadowResult, out) -> None:
    payload = result_payload(result)
    print(f"shadow_result_id: {payload['shadow_result_id']} (phase completed)", file=out)
    print(
        f"decision: {'ALLOWED' if payload['allowed'] else 'REJECTED'} "
        f"(execution_mode={payload['execution_mode']}, policy_id={payload['policy_id']}, "
        f"connection_target={payload['connection_target']}, "
        f"safety_decision_id={payload['safety_decision_id']})",
        file=out,
    )
    for entry in payload["rejections"]:
        print(f"  - {entry['code']}: {entry['message']}", file=out)
    provenance = payload["provenance"]
    print(
        f"provenance: origin={provenance['input_origin']} signal={provenance['signal_source']} "
        f"observed_at={provenance['signal_observed_at_utc']}",
        file=out,
    )
    print(
        f"  config={provenance['config_source']} (version {provenance['config_version']}) "
        f"market={provenance['market_source']} account={provenance['account_source']}",
        file=out,
    )
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
    snapshot = payload["account_snapshot"]
    print(
        f"account snapshot: account_id={snapshot['account_id']} currency={snapshot['currency']} "
        f"balance={snapshot['balance']} equity={snapshot['equity']} "
        f"margin_available={snapshot['margin_available']} "
        f"retrieved_at={snapshot['retrieved_at_utc']} positions={len(snapshot['positions'])}",
        file=out,
    )
    for position in snapshot["positions"]:
        print(
            f"  - {position['symbol']} {position['side']} qty={position['quantity']} "
            f"avg={position['average_entry_price']} retrieved_at={position['retrieved_at_utc']}",
            file=out,
        )
    print(
        f"claim: owner={payload['claim_owner_id']} claimed_at={payload['claimed_at_utc']} "
        f"completed_at={payload['completed_at_utc']}",
        file=out,
    )


def _render_claim_line(claim: ShadowClaim, out) -> None:
    print(
        f"{claim.shadow_result_id:>5} CLAIMED  {claim.execution_request_id} "
        f"origin={claim.input_origin} signal={claim.signal_id} owner={claim.claim_owner_id} "
        f"attempt={claim.claim_attempt_id} generation={claim.claim_generation} "
        f"claimed={claim.claimed_at_utc.isoformat()} "
        f"lease_expires={claim.lease_expires_at_utc.isoformat()}",
        file=out,
    )


def _decision_persistence_failed(result: ShadowResult) -> bool:
    """Whether this record's decision carries ``decision_persist_failed`` --
    the ONLY state in which ``safety_decision_id`` may be NULL."""
    return SafetyRejectionCode.DECISION_PERSIST_FAILED in {
        entry.code for entry in result.decision.rejections
    }


def _verify_decision_reference_invariant(result: ShadowResult) -> None:
    """``safety_decision_id IS NULL`` <=> decision persistence failed, BOTH
    directions (PR #100 4th-review Major 2).

    A NULL reference is legitimate ONLY for the explicit recoverable state
    the service records when the decision could not be persisted / read back
    (``decision_persist_failed``); a NULL on any other record means the
    reference was lost, and a decision-persistence failure that still carries
    a reference means the record points at a row it never wrote. Neither may
    be skipped silently -- both are integrity failures. (The same invariant
    is enforced when a :class:`ShadowResult` is constructed, so a stored row
    fails at restore time too; this is the report-boundary guarantee. The
    report only ever displays PERSISTED records, which is the scope of the
    NULL direction -- the unpersisted dry-run projection is not reported.)"""
    persist_failed = _decision_persistence_failed(result)
    if result.safety_decision_id is None and not persist_failed:
        raise ShadowReportIntegrityError(
            f"shadow record {result.request.execution_request_id!r} has no "
            "safety_decision_id but does not carry the 'decision_persist_failed' "
            "rejection; a missing decision reference is only valid for a recorded "
            "decision-persistence failure"
        )
    if result.safety_decision_id is not None and persist_failed:
        raise ShadowReportIntegrityError(
            f"shadow record {result.request.execution_request_id!r} carries the "
            "'decision_persist_failed' rejection but still references "
            f"safety_decision_id {result.safety_decision_id}"
        )


def _verify_decision_consistency(results, decision_db: Path) -> None:
    """Cross-check every completed record against its REFERENCED Safety
    Decision row (PR #100 re-review Major 4): the stored decision must exist
    and be FIELD-IDENTICAL to the decision embedded in the shadow record
    (account_id, position_count, execution request id, policy id, mode,
    codes, timestamps). Any mismatch means a corrupted / inconsistent record
    and the report fails EXPLICITLY -- never silently displaying it, in text
    or JSON output alike.

    A NULL ``safety_decision_id`` is not a free pass: it is verified against
    the decision-persistence-failure invariant first (PR #100 4th-review
    Major 2). A MISSING decision store file is itself an integrity failure
    whenever any displayed record references a decision id (PR #100
    3rd-review Major 2): the report fails closed instead of silently showing
    unverifiable records. Only records with no references at all render
    without the store.

    Raises :class:`ShadowReportIntegrityError` -- every failure of the store
    read path is normalized into it (PR #100 4th-review Major 1)."""
    for result in results:
        _verify_decision_reference_invariant(result)

    referencing = [
        result.request.execution_request_id
        for result in results
        if result.safety_decision_id is not None
    ]
    if not decision_db.exists():
        if referencing:
            raise ShadowReportIntegrityError(
                f"the decision store at {decision_db} does not exist, but "
                f"{len(referencing)} shadow record(s) (e.g. {referencing[0]!r}) "
                "reference safety decision ids that cannot be verified"
            )
        return
    if not referencing:
        return

    with _decision_store_boundary(decision_db):
        store = SQLiteSafetyDecisionStore(decision_db)
        for result in results:
            if result.safety_decision_id is None:
                continue
            stored = dict(
                store.list_for_request_with_ids(result.request.execution_request_id)
            ).get(result.safety_decision_id)
            if stored is None:
                raise ShadowReportIntegrityError(
                    f"shadow record {result.request.execution_request_id!r} references "
                    f"safety_decision_id {result.safety_decision_id}, which does not exist "
                    f"in the decision store at {decision_db}"
                )
            if stored != result.decision:
                raise ShadowReportIntegrityError(
                    f"shadow record {result.request.execution_request_id!r} does not match "
                    f"its referenced Safety Decision row {result.safety_decision_id} "
                    "(account_id / position_count / codes / policy or another field "
                    "disagrees); refusing to report a corrupted or inconsistent record"
                )


@contextmanager
def _decision_store_boundary(decision_db: Path):
    """Normalize EVERY decision-store read failure into
    :class:`ShadowReportIntegrityError` (PR #100 4th-review Major 1).

    An empty file, a file that is not SQLite at all, a missing table or
    column, a mismatched ``user_version``, a physically corrupted image and
    an unrestorable row all raise different exception types from the store /
    sqlite3 layers. Letting any of them escape would print a traceback and
    break the CLI's stable failure contract, so they are converted here --
    reading NEVER creates or modifies the file either way."""
    try:
        yield
    except ShadowReportIntegrityError:
        raise
    except _DECISION_STORE_FAILURES as exc:
        raise ShadowReportIntegrityError(
            f"the decision store at {decision_db} could not be read for "
            f"verification ({type(exc).__name__}: {exc})"
        ) from exc


def _find_orphan_decisions(shadow_db: Path, decision_db: Path) -> list[dict]:
    """Decision rows for shadow requests that are NOT referenced by the
    completed record (module docstring). Read-only on both stores; decision
    store failures are normalized like every other read (Major 1)."""
    orphans: list[dict] = []
    records: list[tuple[str, int | None]] = [
        (result.request.execution_request_id, result.safety_decision_id)
        for result in shadow_store.list_shadow_results(db_path=shadow_db)
    ]
    records.extend(
        (claim.execution_request_id, None)
        for claim in shadow_store.list_shadow_claims(db_path=shadow_db)
    )
    with _decision_store_boundary(decision_db):
        store = SQLiteSafetyDecisionStore(decision_db)
        for execution_request_id, referenced_id in records:
            for decision_id, decision in store.list_for_request_with_ids(execution_request_id):
                if decision_id != referenced_id:
                    orphans.append(
                        {
                            "decision_id": decision_id,
                            "execution_request_id": execution_request_id,
                            "allowed": decision.allowed,
                            "checked_at_utc": decision.checked_at_utc.isoformat(),
                            "referenced_decision_id": referenced_id,
                        }
                    )
    return orphans


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m execution.shadow_report",
        description=(
            "Inspect persisted Shadow Mode verification records (Issue #78). "
            "Strictly read-only: never creates or modifies any DB file."
        ),
    )
    parser.add_argument(
        "--shadow-db",
        type=Path,
        default=None,
        help="shadow result DB path (default: SHADOW_RESULT_DB_PATH or the packaged default)",
    )
    parser.add_argument(
        "--decision-db",
        type=Path,
        default=None,
        help="safety decision DB path (used by --orphan-decisions; default: "
        "SAFETY_GATE_DECISION_DB_PATH or the packaged default)",
    )
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument(
        "--execution-request-id", default=None, help="show the record for one execution request"
    )
    selector.add_argument(
        "--source-signal-id", default=None, help="show the record for one source signal"
    )
    selector.add_argument(
        "--safety-decision-id",
        type=int,
        default=None,
        help="show the record referencing one Safety Decision row",
    )
    selector.add_argument(
        "--claims", action="store_true", help="list live/stale claims instead of completed records"
    )
    selector.add_argument(
        "--orphan-decisions",
        action="store_true",
        help="list decision rows not referenced by any completed shadow record",
    )
    parser.add_argument(
        "--decision",
        choices=("allowed", "rejected"),
        default=None,
        help="list only gate-allowed or gate-rejected records",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=f"maximum records to list (1..{shadow_store.MAX_LIST_LIMIT}; "
        "listing order is shadow_result_id DESC, newest first)",
    )
    parser.add_argument(
        "--json", action="store_true", help="print a machine-readable JSON report to stdout"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    with encoding_safe_streams():
        return _run_cli(argv)


def _run_cli(argv: list[str] | None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and not 1 <= args.limit <= shadow_store.MAX_LIST_LIMIT:
        print(
            f"--limit must be between 1 and {shadow_store.MAX_LIST_LIMIT}, got {args.limit}",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR
    if args.safety_decision_id is not None and args.safety_decision_id < 1:
        print("--safety-decision-id must be a positive integer", file=sys.stderr)
        return EXIT_USAGE_ERROR
    db_path = Path(args.shadow_db) if args.shadow_db is not None else shadow_store.shadow_db_path()
    if not db_path.exists():
        # Read-only contract: a missing store is reported, never created.
        print(f"shadow result DB does not exist: {db_path}", file=sys.stderr)
        return EXIT_FAILURE
    decision_db = Path(args.decision_db) if args.decision_db is not None else safety_db_path()

    try:
        if args.orphan_decisions:
            orphans = _find_orphan_decisions(db_path, decision_db)
            if args.json:
                print(
                    json.dumps(
                        {"shadow_db": str(db_path), "decision_db": str(decision_db),
                         "count": len(orphans), "orphan_decisions": orphans},
                        ensure_ascii=True,
                        indent=2,
                    )
                )
                return EXIT_OK
            if not orphans:
                print("no orphan decisions")
                return EXIT_OK
            for orphan in orphans:
                print(
                    f"decision_id={orphan['decision_id']} "
                    f"execution_request_id={orphan['execution_request_id']} "
                    f"allowed={orphan['allowed']} checked_at={orphan['checked_at_utc']} "
                    f"referenced_decision_id={orphan['referenced_decision_id']}"
                )
            print(f"total {len(orphans)}")
            return EXIT_OK

        if args.claims:
            claims = shadow_store.list_shadow_claims(limit=args.limit, db_path=db_path)
            if args.json:
                print(
                    json.dumps(
                        {"shadow_db": str(db_path), "count": len(claims),
                         "claims": [claim_payload(claim) for claim in claims]},
                        ensure_ascii=True,
                        indent=2,
                    )
                )
                return EXIT_OK
            if not claims:
                print("no shadow claims")
                return EXIT_OK
            for claim in claims:
                _render_claim_line(claim, sys.stdout)
            print(f"total {len(claims)}")
            return EXIT_OK

        detail = False
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
        elif args.safety_decision_id is not None:
            result = shadow_store.get_shadow_result_by_decision_id(
                args.safety_decision_id, db_path=db_path
            )
            if result is None:
                print(
                    f"no shadow result references safety_decision_id {args.safety_decision_id}",
                    file=sys.stderr,
                )
                return EXIT_FAILURE
            results = (result,)
            detail = True
        else:
            allowed = None if args.decision is None else (args.decision == "allowed")
            results = shadow_store.list_shadow_results(
                allowed=allowed, limit=args.limit, db_path=db_path
            )
    except (shadow_store.ShadowStoreError, ShadowReportIntegrityError) as exc:
        print(f"shadow report failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    try:
        _verify_decision_consistency(results, decision_db)
    except (shadow_store.ShadowStoreError, ShadowReportIntegrityError) as exc:
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
