"""Shadow Mode RESIDENT runner CLI (Issue #134) -- the process
``scripts/systemd/berial-execution-shadow.service`` keeps running.

*** NO BROKER EXISTS ANYWHERE ON THIS PATH. Nothing here submits, cancels
or queries an order, and nothing here holds a credential: the shadow runner
records what WOULD have been ordered into its own store and touches nothing
else. ***

``python -m execution.shadow_service_cli`` composes the Issue #131 shadow
runner (``build_shadow_execution_runner`` over the PRODUCTION providers:
stored Paper Signals, the strict execution configuration, operator-maintained
market / account snapshot files) and paces it with ``run_forever`` until a
stop is requested. It adds NO evaluation of its own -- every cycle is the
same production path ``python -m execution.shadow_runner --signal-id ...``
runs once, driven continuously.

Relationship to ``python -m execution.shadow_runner``
-----------------------------------------------------

The Issue #78 CLI stays exactly what it is: ONE cycle for ONE signal,
human-invoked. This CLI adds only what a resident process needs:

- candidate selection (executable stored signals, newest first -- handed to
  the shadow claim UNFILTERED, which is the only component allowed to decide
  what an already-seen signal means);
- an exclusive per-data-directory entry (one resident shadow runner per
  shadow DB; a busy lock is a fail-closed refusal, exit 1);
- graceful shutdown: SIGTERM / SIGINT finish the current item, start no new
  one, and interrupt the inter-cycle sleep (the systemd stop contract).

All inputs are REQUIRED with no permissive defaults (the production-path
stance of PR #100): a missing signal DB, configuration or snapshot file is
an explicit failure, never an empty cycle that looks like "no work".

Modes: there is NO ``--mode`` argument -- this CLI can only compose the
shadow runner, so it cannot be misconfigured into a submitting mode.
``--dry-run`` evaluates with ZERO side effects (``plan()`` only: no DB
created, no claim, no record, and no runner lock file either).

Exit codes (shared execution-CLI contract): 0 = the loop ended by a
requested stop, or a bounded ``--max-cycles`` run that RAN ALL N CYCLES
with every cycle ok; 1 = a bounded run that was interrupted or had a failed
cycle, or a fail-closed startup refusal (another resident runner holds the
lock, unreadable configuration); 2 = usage errors. ``--json`` keeps stdout
to exactly ONE pure-ASCII machine-readable document in every post-parse
outcome: the loop summary on a completed run (the durable evidence a
bounded local verification run leaves behind, Issue #134), or an
``{"error", "message"}`` document on a refusal / configuration failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from execution.cli_streams import encoding_safe_streams
from execution.live_runner import DEFAULT_MAX_ITEMS_PER_CYCLE
from execution.runner_composition import build_shadow_execution_runner
from execution.runner_service import (
    DEFAULT_INTERVAL_SECONDS,
    SHADOW_RUNNER_LOCK_FILENAME,
    RunnerAlreadyRunningError,
    RunnerServiceLockError,
    install_stop_signal_handlers,
    loop_summary_payload,
    run_service_loop,
    runner_service_lock,
    service_exit_code,
)
from execution.safety_store import safety_db_path
from execution.shadow_config import ShadowConfigError, load_shadow_execution_config
from execution.shadow_providers import (
    AccountSnapshotFileProvider,
    MarketSnapshotFileProvider,
    PaperSignalDbSignalProvider,
)
from execution import shadow_store

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE_ERROR = 2

_LOGGER = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m execution.shadow_service_cli",
        description=(
            "Run the Shadow Mode runner as a RESIDENT service (Issue #134): the "
            "production decision path over stored Paper Signals, continuously, with "
            "zero broker calls and zero credentials. Stop with SIGTERM/SIGINT "
            "(systemctl stop): the current item finishes, no new item starts."
        ),
    )
    parser.add_argument(
        "--signal-db",
        type=Path,
        required=True,
        help="paper signal DB path (read-only; the candidate queue of executable "
        "stored signals). No default",
    )
    parser.add_argument(
        "--execution-config",
        type=Path,
        required=True,
        help="production shadow execution configuration JSON (config_id, policy, "
        "order quantities). No default",
    )
    parser.add_argument(
        "--market-snapshot",
        type=Path,
        required=True,
        help="operator-maintained market snapshot JSON file (re-read every item; "
        "staleness is judged by the Safety Gate). No default",
    )
    parser.add_argument(
        "--account-snapshot",
        type=Path,
        required=True,
        help="operator-maintained account/position snapshot JSON file. No default",
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
        help="safety decision DB path (default: SAFETY_GATE_DECISION_DB_PATH or the "
        "packaged default)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="directory holding both DBs (shadow_results.sqlite3 / "
        "safety_gate_decisions.sqlite3) and, by default, the runner lock; "
        "overridden by --shadow-db / --decision-db / --runner-lock",
    )
    parser.add_argument(
        "--runner-lock",
        type=Path,
        default=None,
        help=f"resident-runner lock file (default: '{SHADOW_RUNNER_LOCK_FILENAME}' "
        "next to the shadow DB). One resident shadow runner per data directory",
    )
    parser.add_argument(
        "--owner-id",
        default=None,
        help="shadow claim owner identity (default: a per-process value; give each "
        "deployment a stable, distinct value so crash-retry recovery is deterministic)",
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
        help="plan() only: zero side effects -- no DB created or written, no claim, "
        "no shadow record, and no runner lock file either",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="keep stdout to exactly one machine-readable pure-ASCII JSON document "
        "(the loop summary on a completed run; an error document otherwise)",
    )
    return parser


def _resolve_db_paths(args) -> tuple[Path, Path]:
    shadow_db = args.shadow_db
    decision_db = args.decision_db
    if args.data_dir is not None:
        shadow_db = shadow_db if shadow_db is not None else args.data_dir / "shadow_results.sqlite3"
        decision_db = (
            decision_db
            if decision_db is not None
            else args.data_dir / "safety_gate_decisions.sqlite3"
        )
    if shadow_db is None:
        shadow_db = shadow_store.shadow_db_path()
    if decision_db is None:
        decision_db = safety_db_path()
    return Path(shadow_db), Path(decision_db)


def _fail(args, exit_code: int, error: str, message: str) -> int:
    """One failure, both audiences: the message on stderr for the journal,
    and -- under ``--json`` -- ONE machine-readable error document on stdout,
    so a redirected artifact can always be json.load()ed regardless of the
    outcome (PR #138 Codex review P2)."""
    print(message, file=sys.stderr)
    if args.json:
        print(
            json.dumps({"error": error, "message": message}, ensure_ascii=True, sort_keys=True)
        )
    return exit_code


def _run_cli(argv: list[str] | None) -> int:
    args = _build_parser().parse_args(argv)

    if args.max_cycles is not None and args.max_cycles < 1:
        return _fail(args, EXIT_USAGE_ERROR, "usage_error", "--max-cycles must be >= 1")

    shadow_db, decision_db = _resolve_db_paths(args)
    lock_path = (
        args.runner_lock
        if args.runner_lock is not None
        else shadow_db.parent / SHADOW_RUNNER_LOCK_FILENAME
    )

    try:
        config = load_shadow_execution_config(args.execution_config)
    except ShadowConfigError as exc:
        return _fail(args, EXIT_USAGE_ERROR, "configuration_error", f"configuration error: {exc}")

    # The RESIDENT (non-dry) loop opts out of the immutable-WAL fallback: a
    # point-in-time view silently hides committed-yet-uncheckpointed rows
    # (schema included) from a loop that re-polls the store forever (PR #138
    # Codex review P2). SQLite may then (re)create the signal DB's -wal/-shm
    # sidecars, so the DB directory needs sidecar access (see the unit
    # template's note). A DRY RUN keeps the immutable fallback instead: its
    # contract is ZERO filesystem side effects (recreating a sidecar next to
    # a cleanly closed WAL DB would break that, and must keep working when
    # the input directory is deliberately read-only) -- the price is the
    # point-in-time view, which is acceptable for a plan-only pass (PR #138
    # Codex review P2, second round).
    signal_provider = PaperSignalDbSignalProvider(
        args.signal_db, allow_immutable_wal=args.dry_run
    )
    try:
        runner = build_shadow_execution_runner(
            signal_provider=signal_provider,
            signal_lister=signal_provider,
            config=config,
            market_provider=MarketSnapshotFileProvider(args.market_snapshot),
            account_provider=AccountSnapshotFileProvider(args.account_snapshot),
            shadow_db_path=shadow_db,
            decision_db_path=decision_db,
            clock=lambda: datetime.now(timezone.utc),
            owner_id=args.owner_id,
            max_items_per_cycle=args.max_items_per_cycle,
            dry_run=args.dry_run,
            runner_id="shadow-execution-service",
        )
    except ValueError as exc:
        return _fail(
            args,
            EXIT_USAGE_ERROR,
            "invalid_runner_configuration",
            f"invalid runner configuration: {exc}",
        )

    _LOGGER.info(
        "shadow service starting: signal_db=%s config=%s shadow_db=%s decision_db=%s "
        "interval=%gs max_cycles=%s dry_run=%s (no broker exists on this path)",
        args.signal_db,
        config.config_id,
        shadow_db,
        decision_db,
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
            # concurrent dry runs are harmless by construction (zero side
            # effects on every path).
            report = _loop()
        else:
            with runner_service_lock(lock_path):
                report = _loop()
    except RunnerAlreadyRunningError as exc:
        return _fail(args, EXIT_FAILURE, "runner_already_running", f"refused: {exc}")
    except RunnerServiceLockError as exc:
        return _fail(args, EXIT_FAILURE, "runner_lock_unusable", f"runner lock unusable: {exc}")
    except ValueError as exc:
        # run_forever's own boundary validation (e.g. a non-positive or
        # over-limit interval) -- a configuration mistake, refused before any
        # cycle ran.
        return _fail(
            args,
            EXIT_USAGE_ERROR,
            "invalid_runner_configuration",
            f"invalid runner configuration: {exc}",
        )

    exit_code = service_exit_code(report, bounded=args.max_cycles is not None)
    _LOGGER.info(
        "shadow service stopped: reason=%s cycles=%d exit=%d",
        report.stop_reason.value,
        len(report.cycles),
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
