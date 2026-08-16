"""OANDA Demo RESIDENT runner CLI (Issue #134) -- the process
``scripts/systemd/berial-execution-demo.service`` keeps running.

*** DEMO IS NOT A LOOSER LIVE, AND THIS CLI ADDS NO ORDER PATH. *** Every
submission goes through the ONE sanctioned boundary
(``LiveOrderSubmissionService``) inside the Issue #132 demo runner: Safety
Gate (with the DURABLE Kill Switch re-read on every evaluation and again at
the broker-call point), Submission Journal, order state machine -- nothing
is relaxed because the account is a practice account. ``live`` cannot be
started from here: this CLI composes only ``build_demo_execution_runner``,
which accepts only a ``DemoRuntimeGrant``, and the grant only exists after
EVERY demo activation lock held.

``python -m execution.demo_service_cli`` is the continuous-operation
counterpart of the Issue #132 preflight
(``python -m execution.demo_startup_cli``):

1. evaluate the demo activation lock via ``build_demo_connection`` -- the
   sealed composer that reads the safety configuration from its path, the
   Kill Switch from its durable store and the credentials from the real
   process environment. ANY refusal (mode, credentials, endpoint, demo/live
   separation, account pin, policy, Kill Switch ON or unreadable) prints the
   same secret-free safety summary the preflight prints and exits 1
   fail-closed -- no runner, no broker call;
2. take the exclusive per-data-directory runner lock (one resident
   submission runner per order-DB directory; systemd already refuses a
   second unit instance -- the lock covers the manual-run-next-to-the-unit
   gap, exit 1 fail-closed);
3. drive the composed runner with ``run_forever``: pending intents (phase
   ``created``) -> the sanctioned boundary, once per item, never re-sent.
   An unresolved outcome stays unresolved -- resolution belongs to
   reconciliation (Issue #133's INDEPENDENT unit), never to this loop;
4. stop gracefully on SIGTERM / SIGINT (``systemctl stop``): the current
   item finishes, no new item starts, the inter-cycle sleep is interrupted.

Work INTAKE is out of scope on purpose: this CLI creates no execution
request. It drains whatever was registered through the sanctioned
registration path into the order store it is pointed at.

``--dry-run`` evaluates with ZERO side effects (``plan()`` only: no broker
call, no DB created or migrated, nothing written, no runner lock file) --
but the demo activation lock is still evaluated for real: a dry run that
would "succeed" without credentials would rehearse a startup that cannot
happen.

Exit codes (shared execution-CLI contract): 0 = the loop ended by a
requested stop, or a bounded ``--max-cycles`` run in which EVERY cycle was
ok; 1 = the demo activation was REFUSED (fail-closed), another resident
runner holds the lock, or a bounded run had a failed cycle; 2 = usage
errors. ``--json`` prints one pure-ASCII machine-readable loop summary on
stdout at the end of the run (the durable evidence of a bounded local
verification run, Issue #134).

Secrets: the token, the account id and the endpoint URL are never printed
-- the startup summary shows the endpoint KIND and the account as a
non-reversible digest (the Issue #132 contract).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from execution.cli_streams import encoding_safe_streams
from execution.demo_lock import build_demo_connection
from execution.execution_store import execution_db_path
from execution.kill_switch_store import kill_switch_db_path
from execution.live_runner import DEFAULT_MAX_ITEMS_PER_CYCLE
from execution.runner_composition import build_demo_execution_runner
from execution.runner_service import (
    DEFAULT_INTERVAL_SECONDS,
    EXECUTION_RUNNER_LOCK_FILENAME,
    RunnerAlreadyRunningError,
    RunnerServiceLockError,
    install_stop_signal_handlers,
    loop_summary_payload,
    run_service_loop,
    runner_service_lock,
    service_exit_code,
)
from execution.safety_store import safety_db_path
from execution.shadow_providers import MarketSnapshotFileProvider
from execution.submission_journal import journal_db_path

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE_ERROR = 2

_LOGGER = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m execution.demo_service_cli",
        description=(
            "Run the OANDA Demo (practice) submission runner as a RESIDENT service "
            "(Issue #134). Starts ONLY when every demo activation lock holds "
            "(fail-closed otherwise), submits pending intents through the one "
            "sanctioned boundary, and never drives 'live'. Stop with SIGTERM/SIGINT "
            "(systemctl stop): the current item finishes, no new item starts."
        ),
    )
    parser.add_argument(
        "--execution-mode",
        required=True,
        help="the configured execution mode; only 'demo' can produce a demo runner "
        "(anything else is refused fail-closed by the activation lock)",
    )
    parser.add_argument(
        "--safety-config",
        type=Path,
        required=True,
        help="path to the demo safety configuration JSON (symbols, quantity ceiling, "
        "position limits, the pinned account digest). It holds NO credentials",
    )
    parser.add_argument(
        "--kill-switch-db",
        type=Path,
        default=None,
        help="kill switch DB path (default: KILL_SWITCH_DB_PATH or the packaged "
        "execution/data/kill_switch.sqlite3). Read fail-closed by the lock AND "
        "re-read by the gate on every submission",
    )
    parser.add_argument(
        "--market-snapshot",
        type=Path,
        required=True,
        help="operator-maintained market snapshot JSON file (re-read every item; "
        "staleness is judged by the Safety Gate). No default",
    )
    parser.add_argument(
        "--order-db",
        type=Path,
        default=None,
        help="execution order DB path (default: EXECUTION_ORDER_DB_PATH or the "
        "packaged default). Keep the demo namespace separate from fake",
    )
    parser.add_argument(
        "--decision-db",
        type=Path,
        default=None,
        help="safety decision DB path (default: SAFETY_GATE_DECISION_DB_PATH or the "
        "packaged default)",
    )
    parser.add_argument(
        "--journal-db",
        type=Path,
        default=None,
        help="submission journal DB path (default: SUBMISSION_JOURNAL_DB_PATH or the "
        "packaged default)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="directory holding the three runner DBs (execution_orders.sqlite3 / "
        "safety_gate_decisions.sqlite3 / submission_journal.sqlite3) and, by "
        "default, the runner lock; overridden by the explicit --*-db options. "
        "Demo and fake must never share one (docs/operations/operations.md)",
    )
    parser.add_argument(
        "--runner-lock",
        type=Path,
        default=None,
        help=f"resident-runner lock file (default: '{EXECUTION_RUNNER_LOCK_FILENAME}' "
        "next to the order DB). One resident submission runner per data directory",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"pause between cycles in seconds (default {DEFAULT_INTERVAL_SECONDS:g}; "
        "a stop request interrupts the pause immediately)",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="run exactly N cycles and exit (bounded verification run: exit 0 only "
        "if every cycle was ok). Default: run until stopped",
    )
    parser.add_argument(
        "--max-items-per-cycle",
        type=int,
        default=DEFAULT_MAX_ITEMS_PER_CYCLE,
        help=f"work items per cycle (default {DEFAULT_MAX_ITEMS_PER_CYCLE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="plan() only: zero side effects -- no broker call, no DB created or "
        "written, no runner lock file. The demo activation lock is still evaluated "
        "for real",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print a machine-readable pure-ASCII loop summary on stdout at the end",
    )
    return parser


def _resolve_db_paths(args) -> tuple[Path, Path, Path]:
    order_db = args.order_db
    decision_db = args.decision_db
    journal_db = args.journal_db
    if args.data_dir is not None:
        order_db = order_db if order_db is not None else args.data_dir / "execution_orders.sqlite3"
        decision_db = (
            decision_db
            if decision_db is not None
            else args.data_dir / "safety_gate_decisions.sqlite3"
        )
        journal_db = (
            journal_db if journal_db is not None else args.data_dir / "submission_journal.sqlite3"
        )
    if order_db is None:
        order_db = execution_db_path()
    if decision_db is None:
        decision_db = safety_db_path()
    if journal_db is None:
        journal_db = journal_db_path()
    return Path(order_db), Path(decision_db), Path(journal_db)


def _run_cli(argv: list[str] | None) -> int:
    args = _build_parser().parse_args(argv)

    if args.max_cycles is not None and args.max_cycles < 1:
        print("--max-cycles must be >= 1", file=sys.stderr)
        return EXIT_USAGE_ERROR

    order_db, decision_db, journal_db = _resolve_db_paths(args)
    lock_path = (
        args.runner_lock
        if args.runner_lock is not None
        else order_db.parent / EXECUTION_RUNNER_LOCK_FILENAME
    )
    kill_switch_db = (
        args.kill_switch_db if args.kill_switch_db is not None else kill_switch_db_path()
    )

    # THE demo activation lock (Issue #132): the sealed composer judges the
    # real environment, the real safety-config file and the real Kill Switch
    # store. Nothing this CLI computes can authorize the connection.
    decision, grant = build_demo_connection(
        execution_mode=args.execution_mode,
        safety_config_path=args.safety_config,
        kill_switch_db_path=kill_switch_db,
    )
    # The startup summary is the operator's audit record in the journal --
    # printed on refusal AND on success, secret-free by construction.
    print(decision.summary.render())
    if not decision.allowed:
        for entry in decision.rejections:
            print(f"  refused [{entry.code.value}]: {entry.message}", file=sys.stderr)
        print(
            "demo startup REFUSED (fail-closed): no runner was composed and no "
            "broker call was made",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    try:
        runner = build_demo_execution_runner(
            grant=grant,
            market_provider=MarketSnapshotFileProvider(args.market_snapshot),
            order_db_path=order_db,
            decision_db_path=decision_db,
            journal_db_path=journal_db,
            clock=lambda: datetime.now(timezone.utc),
            max_items_per_cycle=args.max_items_per_cycle,
            dry_run=args.dry_run,
            runner_id="demo-execution-service",
        )
    except ValueError as exc:
        print(f"invalid runner configuration: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR

    _LOGGER.info(
        "demo service starting: order_db=%s journal_db=%s kill_switch_db=%s "
        "interval=%gs max_cycles=%s dry_run=%s",
        order_db,
        journal_db,
        kill_switch_db,
        args.interval_seconds,
        args.max_cycles,
        args.dry_run,
    )

    install_stop_signal_handlers(runner)

    def _loop():
        return run_service_loop(
            runner,
            interval_seconds=args.interval_seconds,
            max_cycles=args.max_cycles,
        )

    try:
        if args.dry_run:
            # A dry run creates NOTHING -- the lock file included. Two
            # concurrent dry runs are harmless by construction (plan() never
            # reaches a broker or a writable store).
            report = _loop()
        else:
            with runner_service_lock(lock_path):
                report = _loop()
    except RunnerAlreadyRunningError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except RunnerServiceLockError as exc:
        print(f"runner lock unusable: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except ValueError as exc:
        # run_forever's own boundary validation (e.g. a non-positive or
        # over-limit interval) -- refused before any cycle ran.
        print(f"invalid runner configuration: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR

    exit_code = service_exit_code(report, bounded=args.max_cycles is not None)
    _LOGGER.info(
        "demo service stopped: reason=%s cycles=%d broker_sends=%d unresolved "
        "submissions stay unresolved for reconciliation, exit=%d",
        report.stop_reason.value,
        len(report.cycles),
        report.broker_sends,
        exit_code,
    )
    if args.json:
        print(json.dumps(loop_summary_payload(report), ensure_ascii=True, sort_keys=True))
    return exit_code


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    with encoding_safe_streams():
        try:
            return _run_cli(argv)
        except SystemExit as exc:  # argparse usage errors
            return EXIT_USAGE_ERROR if exc.code else EXIT_OK


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
