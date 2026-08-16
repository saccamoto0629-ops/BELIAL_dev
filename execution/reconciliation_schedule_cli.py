r"""Scheduled reconciliation ONESHOT CLI (Issue #133) -- RECONCILE-ONLY.

*** NO ORDER IS EVER PLACED, CANCELLED OR MODIFIED FROM THIS CLI. ***

``python -m execution.reconciliation_schedule_cli`` runs exactly ONE
scheduled reconciliation cycle (:mod:`execution.reconciliation_schedule`) and
exits. It is the process a **systemd timer + oneshot service** starts --
``scripts/systemd/berial-execution-reconciliation.{service,timer}`` -- and it
is deliberately NOT part of the Live Execution runner: reconciliation runs as
its own execution unit, with its own schedule, its own failure surface and
its own lock, so neither side can stall the other.

Relationship to ``python -m execution.reconciliation_runner``
-------------------------------------------------------------

The manual CLI stays exactly what it is: a detection-only, human-invoked
pass that changes no state whatsoever. This CLI adds the three things a
PERIODIC boundary needs and nothing else:

- exclusive entry, so a timer tick that lands on a still-running cycle is
  suppressed instead of overlapping it;
- takeover recovery of repair attempts a crashed cycle left pending;
- an opt-in ``--repair`` pass over the run it just recorded.

Both build their broker through the SAME
``reconciliation_runner.build_reconcile_only_broker`` factory, so the broker
this CLI holds is already the reconcile-only facade whose ``submit_order`` /
``cancel_order`` / ``get_account_state`` raise unconditionally, and no second
OANDA-adapter construction site exists.

``--repair`` (default OFF)
--------------------------

Repair is opt-in on purpose. When it is on, ONLY ``auto_repair_candidate``
ORDER discrepancies reach the existing Issue #104 repair path, through its
existing claim / lease / generation / takeover / fencing contract; the only
thing that can change is BELIAL's own order state, and only through the
Issue #74 state machine. ``manual_review`` is never repaired -- neither the
order-scope cases (broker-only orders, contradicted outcomes) nor ANY
position / trade / transaction / SL / TP discrepancy, whose whole domain has
no repair path at all. Nothing is ever closed, re-created, cancelled or
re-sent broker-side.

Modes
-----

- default: the run is recorded to the reconciliation audit DB, and (with
  ``--repair``) attempts are claimed and finalized there.
- ``--dry-run``: compare and report ONLY -- no reconciliation DB is created,
  migrated or written, no execution DB is created, no lock file is opened or
  created, and ``--repair`` is refused (there is no recorded run to repair).

Exit codes (shared execution-CLI contract): 0 = the cycle ran clean, or was
SUPPRESSED because another cycle held the lock; 1 = discrepancies, a partial
failure, a blocked takeover, or a repair pass that stopped part-way; 2 =
usage / configuration error (including every non-demo OANDA endpoint
refusal). ``--json`` prints one pure-ASCII JSON document to stdout.

A suppressed cycle exits 0 by design: under a timer, "the previous cycle is
still working" is normal, and turning it into a failed unit would train
operators to ignore the unit's state. It is still visible -- ``outcome`` is
``suppressed_locked`` in JSON and a WARNING goes to stderr -- so a cycle that
is stuck holding the lock shows up as a run that never reports a run_id.

Secrets: the OANDA token / account id / endpoint are read from the
environment by the adapter and NEVER printed -- output identifies the
connection as ``demo / oanda-demo`` (constant identity labels) only.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from execution import execution_store, reconciliation_store
from execution.broker_reconciliation_repair import DEFAULT_LEASE_DURATION_SECONDS
from execution.cli_streams import encoding_safe_streams
from execution.reconciliation_runner import (
    BROKER_CHOICES,
    build_reconcile_only_broker,
    parse_utc_argument,
    resolve_db_paths,
    run_payload,
)
from execution.reconciliation_schedule import (
    CycleAlreadyRunningError,
    CycleLockError,
    ReconciliationCycleError,
    ReconciliationCycleReport,
    cycle_lock_path_for,
    run_reconciliation_cycle,
)

EXIT_OK = 0
EXIT_ATTENTION = 1
EXIT_USAGE_ERROR = 2

_OUTCOME_SUPPRESSED = "suppressed_locked"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m execution.reconciliation_schedule_cli",
        description=(
            "Run ONE scheduled reconcile-only reconciliation cycle (Issue #133): "
            "orders + positions / trades / SL / TP, with an exclusive entry lock and "
            "an opt-in repair pass over auto_repair_candidate order discrepancies. "
            "No order, cancel, close or SL/TP change is ever sent."
        ),
    )
    parser.add_argument(
        "--broker",
        required=True,
        choices=BROKER_CHOICES,
        help=(
            "'oanda-demo': the OANDA DEMO account from OANDA_API_TOKEN / "
            "OANDA_ACCOUNT_ID (non-demo endpoints are refused, fail-closed); "
            "'fake': an empty in-memory FakeBroker (plumbing / diagnostics, no network)"
        ),
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="reconciliation run id (default: a fresh 'recon-cycle-<utc>-<suffix>' "
        "value; recording the same id twice is refused by the store)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compare and report only: no DB is created, migrated or written, no lock "
        "file is opened, and --repair is refused",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="additionally recover pending repair attempts and run the existing "
        "Issue #104 repair pass over this cycle's run (auto_repair_candidate ORDER "
        "discrepancies only; manual_review is never repaired)",
    )
    parser.add_argument(
        "--execution-db",
        type=Path,
        default=None,
        help="execution order DB path (default: EXECUTION_ORDER_DB_PATH or the "
        "packaged default; read-only for detection, written only through the "
        "repair service's state machine)",
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
        "broker_reconciliation.sqlite3) and, by default, the cycle lock; "
        "overridden by --execution-db / --reconciliation-db / --cycle-lock",
    )
    parser.add_argument(
        "--cycle-lock",
        type=Path,
        default=None,
        help="cycle lock file (default: 'reconciliation_cycle.lock' next to the "
        "reconciliation DB). Two cycles sharing a data directory must share this file",
    )
    parser.add_argument(
        "--lock-wait-seconds",
        type=float,
        default=0.0,
        help="how long to wait for a busy cycle lock before suppressing this cycle "
        "(default 0: refuse immediately and let the next timer tick retry)",
    )
    parser.add_argument(
        "--orders-only",
        action="store_true",
        help="restrict the pass to the Issue #103 order scope (skip positions / "
        "trades / SL / TP)",
    )
    parser.add_argument(
        "--repair-owner-id",
        default=None,
        help="owner id recorded on this cycle's repair claims (default: a random "
        "per-process id). Give each deployment a stable, distinct value",
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=DEFAULT_LEASE_DURATION_SECONDS,
        help=f"repair claim lease duration in seconds (default "
        f"{DEFAULT_LEASE_DURATION_SECONDS}). It must comfortably exceed one attempt's "
        "work, and the broker query timeout must stay strictly below it",
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


def _default_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"recon-cycle-{stamp}-{uuid.uuid4().hex[:8]}"


def _attempt_payload(attempt) -> dict:
    """One repair attempt, audit-shaped and secret-free.

    Only classification and identity: the outcome, the closed-set failure
    code, the claim identity and the states around it. Never a raw exception
    text -- the repair service's sanitization contract applies to everything
    that leaves it.
    """
    return {
        "attempt_id": attempt.attempt_id,
        "run_id": attempt.run_id,
        "mode": attempt.mode.value,
        "outcome": attempt.outcome.value,
        "target_type": attempt.target_type.value,
        "target_id": attempt.target_id,
        "execution_request_id": attempt.execution_request_id,
        "client_order_id": attempt.client_order_id,
        "owner_id": attempt.owner_id,
        "generation": attempt.generation,
        "takeover_id": attempt.takeover_id,
        "internal_state_before": None
        if attempt.internal_state_before is None
        else attempt.internal_state_before.value,
        "internal_state_after": None
        if attempt.internal_state_after is None
        else attempt.internal_state_after.value,
        "failure_code": None if attempt.failure_code is None else attempt.failure_code.value,
        "detail": attempt.detail,
    }


def _cycle_payload(report: ReconciliationCycleReport, *, broker_label: str) -> dict:
    reconciliation = run_payload(
        report.run,
        broker_label=broker_label,
        dry_run=report.dry_run,
        recorded_to=report.recorded_to,
    )
    return {
        "outcome": "clean" if report.is_clean else "attention",
        "run_id": report.run_id,
        "dry_run": report.dry_run,
        "reconcile_only": True,
        "scheduled_cycle": True,
        "repair_enabled": report.repair_enabled,
        "repair_skipped_reason": report.repair_skipped_reason,
        "repair_error": report.repair_error,
        "recovered_attempts": [
            _attempt_payload(attempt) for attempt in report.recovered_attempts
        ],
        "repair_attempts": [_attempt_payload(attempt) for attempt in report.repair_attempts],
        "blocked_attempt_count": len(report.blocked_attempts),
        # Durable "a human must look" records from EARLIER cycles. They keep
        # this cycle from reporting clean until someone deals with them, so a
        # blocked takeover cannot scroll out of the journal and be forgotten.
        "unresolved_blocked_intents": list(report.blocked_intent_ids),
        "reconciliation": reconciliation,
    }


def _print_human(payload: dict) -> None:
    reconciliation = payload["reconciliation"]
    print(
        f"reconciliation cycle {payload['run_id']} ({reconciliation['mode']}, "
        f"reconcile-only) broker={reconciliation['broker']}"
    )
    if payload["dry_run"]:
        print("dry-run: nothing was persisted (no DB created, migrated or written)")
    else:
        print(f"recorded to: {reconciliation['recorded_to']}")
    print(f"checked orders: {reconciliation['checked_order_count']}")
    scope = reconciliation["position_scope"]
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
    print(
        "discrepancies: orders={0} position_scope={1} partial_failures={2}".format(
            len(reconciliation["order_discrepancies"]),
            len(reconciliation["position_discrepancies"]),
            reconciliation["has_partial_failures"],
        )
    )
    if payload["repair_enabled"]:
        print(
            "repair: recovered={0} attempts={1} blocked={2}".format(
                len(payload["recovered_attempts"]),
                len(payload["repair_attempts"]),
                payload["blocked_attempt_count"],
            )
        )
        for entry in payload["recovered_attempts"] + payload["repair_attempts"]:
            print(
                "  attempt {0}: target={1}:{2} outcome={3} failure_code={4}".format(
                    entry["attempt_id"],
                    entry["target_type"],
                    entry["target_id"],
                    entry["outcome"],
                    entry["failure_code"],
                )
            )
        # An ENABLED repair can still be skipped -- a blocked takeover stops
        # the pass. Say so here, or the operator reads "attempts=0" as "there
        # was nothing to do".
        if payload["repair_skipped_reason"] is not None:
            print(f"  repair pass skipped: {payload['repair_skipped_reason']}")
    else:
        print(f"repair: not run ({payload['repair_skipped_reason']})")
    if payload["unresolved_blocked_intents"]:
        print(
            "UNRESOLVED blocked takeover intent(s) from an earlier cycle -- a human "
            "must resolve these: {0}".format(
                ", ".join(payload["unresolved_blocked_intents"])
            )
        )
    if payload["repair_error"] is not None:
        print(f"repair error: {payload['repair_error']}")
    print(
        "result: {0} (discrepancies={1}, partial_failures={2}, blocked={3})".format(
            "CLEAN" if payload["outcome"] == "clean" else "ATTENTION",
            reconciliation["discrepancy_count"],
            reconciliation["has_partial_failures"],
            payload["blocked_attempt_count"],
        )
    )


def _print_suppressed(payload: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    else:
        print(f"suppressed: {payload['reason']}")


def _run_cli(argv: list[str] | None) -> int:
    args = _build_parser().parse_args(argv)

    if args.dry_run and args.repair:
        print(
            "--repair cannot be combined with --dry-run: repair acts on a RECORDED "
            "run, and a dry run records nothing",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR
    if args.lock_wait_seconds < 0:
        print("--lock-wait-seconds must be >= 0", file=sys.stderr)
        return EXIT_USAGE_ERROR
    if args.lease_seconds < 1:
        print("--lease-seconds must be >= 1", file=sys.stderr)
        return EXIT_USAGE_ERROR

    fixed_now = None
    if args.now is not None:
        try:
            fixed_now = parse_utc_argument(args.now)
        except ValueError as exc:
            print(f"invalid --now value: {exc}", file=sys.stderr)
            return EXIT_USAGE_ERROR

    run_id = args.run_id if args.run_id is not None else _default_run_id()
    if not run_id.strip():
        print("--run-id must be a non-empty string", file=sys.stderr)
        return EXIT_USAGE_ERROR

    execution_db, reconciliation_db = resolve_db_paths(
        execution_db=args.execution_db,
        reconciliation_db=args.reconciliation_db,
        data_dir=args.data_dir,
    )
    lock_path = args.cycle_lock or cycle_lock_path_for(reconciliation_db)

    try:
        broker, broker_label, query_timeout = build_reconcile_only_broker(args.broker)
    except ValueError as exc:
        # The adapter's construction-time refusals (missing credentials, any
        # non-demo endpoint) arrive here with fixed, secret-free messages.
        print(f"broker configuration refused: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR

    try:
        report = run_reconciliation_cycle(
            broker=broker,
            execution_db_path=execution_db,
            reconciliation_db_path=reconciliation_db,
            run_id=run_id,
            cycle_lock_path=None if args.dry_run else lock_path,
            include_position_scope=not args.orders_only,
            dry_run=args.dry_run,
            repair_enabled=args.repair,
            repair_owner_id=args.repair_owner_id,
            lease_duration_seconds=args.lease_seconds,
            # The adapter's own configured HTTP timeout, so the repair
            # service really enforces "broker query timeout < claim lease"
            # instead of skipping the check for want of a value.
            broker_timeout_seconds=query_timeout,
            lock_wait_seconds=args.lock_wait_seconds,
            clock=None if fixed_now is None else (lambda: fixed_now),
        )
    except CycleAlreadyRunningError as exc:
        print(f"WARNING {exc}", file=sys.stderr)
        _print_suppressed(
            {
                "outcome": _OUTCOME_SUPPRESSED,
                "run_id": None,
                "reconcile_only": True,
                "scheduled_cycle": True,
                "reason": str(exc),
            },
            as_json=args.json,
        )
        return EXIT_OK
    except CycleLockError as exc:
        print(f"cycle lock unusable: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR
    except ReconciliationCycleError as exc:
        print(f"cycle refused: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR
    except (
        execution_store.ExecutionStoreError,
        reconciliation_store.ReconciliationStoreError,
    ) as exc:
        print(f"reconciliation cycle failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ATTENTION

    payload = _cycle_payload(report, broker_label=broker_label)
    if args.json:
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    else:
        _print_human(payload)
    return EXIT_OK if report.is_clean else EXIT_ATTENTION


def main(argv: list[str] | None = None) -> int:
    with encoding_safe_streams():
        return _run_cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
