"""Shared resident-service plumbing for the Execution runner CLIs
(Issue #134: the continuous-operation conduit for ``shadow`` and ``demo``).

*** NOTHING HERE DECIDES, SUBMITS OR WRITES: this module paces an already
composed :class:`~execution.live_runner.ExecutionRunner` and nothing else. ***

Issue #131 deliberately shipped the runner core as a library (``run_cycle`` /
``run_forever`` / graceful shutdown) with NO process boundary, and Issues
#132 / #133 kept it that way. This module is the one place the process-level
concerns of a RESIDENT runner live, shared by
``python -m execution.shadow_service_cli`` and
``python -m execution.demo_service_cli`` so the two services cannot drift
apart:

- **exclusive entry** (:func:`runner_service_lock`): one resident runner per
  data namespace. systemd already refuses to start a unit twice; the lock
  covers what systemd cannot see -- a manual run started next to the unit
  (the same gap the reconciliation cycle lock closes for its oneshot). The
  mechanism deliberately MIRRORS ``reconciliation_schedule.cycle_lock``
  (an empty SQLite file held under ``BEGIN IMMEDIATE``: portable, crash-safe,
  nothing to clean up) without importing that module -- the submission runner
  and the reconciliation cycle are INDEPENDENT execution units, and a shared
  import graph is how independent units start failing together. The two locks
  are separate files on purpose: a runner must never suppress a
  reconciliation cycle, nor the other way round.
- **graceful shutdown** (:func:`install_stop_signal_handlers`): SIGTERM /
  SIGINT request the runner's own stop (``request_stop()``) -- the current
  item finishes, no new item starts, the inter-cycle sleep is interrupted.
  This is exactly the systemd stop contract (``KillSignal=SIGTERM`` +
  ``TimeoutStopSec``); the handler adds no second shutdown path.
- **the loop with observable cycles** (:func:`run_service_loop`): every cycle
  is logged as one line (the journal is the operational record), and the
  exceptions ``run_forever`` already localizes stay localized.
- **the exit-code contract** (:func:`service_exit_code`): a RESIDENT run
  (no ``--max-cycles``) exits 0 when it was STOPPED -- per-cycle failures are
  localized by design (Issue #131), stay visible in the journal and the
  durable stores, and must not turn a weeks-long service into "failed" at
  the moment of a clean operator stop (the same reasoning that makes a
  suppressed reconciliation cycle exit 0). A BOUNDED verification run
  (``--max-cycles N``) is judged: it exits 1 unless every cycle was ok.

Nothing in this module knows what a cycle does: modes, stores, brokers and
gates are entirely the composed runner's affair. It imports no broker
module, composes no connection and calls no store write API (pinned by
``execution/tests/test_submission_boundary.py``).
"""

from __future__ import annotations

import errno
import logging
import signal
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from execution.live_runner import (
    ExecutionRunner,
    RunnerCycleReport,
    RunnerLoopReport,
    RunnerStopReason,
)

_LOGGER = logging.getLogger(__name__)

#: One resident SUBMISSION runner per data namespace (the fake/demo order-DB
#: directory). Sibling of the order DB by default, like the reconciliation
#: lock is a sibling of the audit DB -- and a DIFFERENT file name, because
#: the two execution units must never exclude each other.
EXECUTION_RUNNER_LOCK_FILENAME = "execution_runner.lock"

#: One resident SHADOW runner per shadow-DB directory. Shadow owns no broker
#: and its store's claim already makes concurrent cycles safe; the lock is
#: operational hygiene (one journal, one pacing, no surprise second process).
SHADOW_RUNNER_LOCK_FILENAME = "shadow_runner.lock"

#: Default pacing between cycles. Deliberately conservative: work arrives
#: through durable stores, so a missed minute delays nothing that matters.
DEFAULT_INTERVAL_SECONDS = 60.0

_SQLITE_BUSY = 5
_BUSY_MARKERS = ("locked", "busy")


class RunnerServiceLockError(RuntimeError):
    """The runner service lock could not be opened (bad path, unusable file)."""


class RunnerAlreadyRunningError(RunnerServiceLockError):
    """Another resident runner holds the lock for this data namespace.

    Fail-closed refusal, not a queue: the second process must exit and the
    operator must decide which one was intended.
    """


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code == _SQLITE_BUSY
    message = str(exc).lower()
    return any(marker in message for marker in _BUSY_MARKERS)


@contextmanager
def runner_service_lock(lock_path: Path | str):
    """Hold the exclusive resident-runner lock for the duration of the block.

    No waiting, ever: a busy lock means a runner for this namespace is
    already resident, and the honest outcome is an immediate
    :class:`RunnerAlreadyRunningError` -- there is no "next tick" that would
    make queueing meaningful for a resident process.
    :class:`RunnerServiceLockError` is raised when the lock file itself is
    unusable. The lock is an empty SQLite database held under ``BEGIN
    IMMEDIATE``; nothing is written into it, it stays a zero-page file, and
    the OS releases it however the holder dies.
    """
    lock_path = Path(lock_path)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # The errno NAME (never the OS message): EROFS / EACCES here means
        # the data directory is not writable by this unit -- see the
        # ReadWritePaths precondition in the berial-execution-* templates.
        raise RunnerServiceLockError(
            f"cannot create the directory for the runner lock at {lock_path}: "
            f"{type(exc).__name__} ({errno.errorcode.get(exc.errno, exc.errno)})"
        ) from exc
    try:
        connection = sqlite3.connect(str(lock_path), timeout=0.0)
    except sqlite3.Error as exc:
        raise RunnerServiceLockError(
            f"cannot open the runner lock at {lock_path}: {type(exc).__name__}"
        ) from exc
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise RunnerAlreadyRunningError(
                    f"another resident runner holds the lock at {lock_path}; "
                    "refusing to start a second one for the same data directory"
                ) from exc
            raise RunnerServiceLockError(
                f"cannot acquire the runner lock at {lock_path}: {type(exc).__name__}"
            ) from exc
        except sqlite3.DatabaseError as exc:
            raise RunnerServiceLockError(
                f"the runner lock at {lock_path} is not a usable lock file: "
                f"{type(exc).__name__}"
            ) from exc
        try:
            yield lock_path
        finally:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - close() releases it anyway
                pass
    finally:
        connection.close()


def install_stop_signal_handlers(
    runner: ExecutionRunner,
    *,
    signals: tuple[int, ...] = (signal.SIGTERM, signal.SIGINT),
) -> None:
    """Route SIGTERM / SIGINT into the runner's OWN graceful stop.

    ``request_stop()`` is signal-safe by design (a ``threading.Event``), so
    the handler does nothing else: the current item finishes, no new item is
    started, and the inter-cycle wait returns immediately. A second signal
    changes nothing (the stop is one-way) -- the hard bound on shutdown time
    is systemd's ``TimeoutStopSec``, not a second code path here.

    Must be called from the main thread (CPython restricts ``signal.signal``
    to it); the CLIs call it once, right before entering the loop.
    """
    if not isinstance(runner, ExecutionRunner):
        raise ValueError(f"runner must be an ExecutionRunner, got {runner!r}")

    def _request_stop(signum, frame):
        try:
            name = signal.Signals(signum).name
        except ValueError:  # pragma: no cover - unknown signal number
            name = str(signum)
        _LOGGER.info(
            "received signal %s: requesting graceful stop (the current item "
            "finishes; no new item starts)",
            name,
        )
        runner.request_stop()

    for signum in signals:
        signal.signal(signum, _request_stop)


def _log_cycle(report: RunnerCycleReport) -> None:
    """One journal line per cycle -- the resident service's heartbeat."""
    if report.selection_error is not None:
        _LOGGER.warning(
            "cycle %s: work selection failed (%s); nothing was processed",
            report.cycle_id,
            report.selection_error,
        )
        return
    if report.cycle_error is not None:
        _LOGGER.warning(
            "cycle %s: failed (%s); the loop continues", report.cycle_id, report.cycle_error
        )
        return
    _LOGGER.info(
        "cycle %s: processed=%d statuses=%s broker_sends=%d unresolved=%d failed=%d%s",
        report.cycle_id,
        report.processed,
        report.status_counts(),
        report.broker_sends,
        report.unresolved,
        report.failed,
        " (stop requested)" if report.stopped else "",
    )


def run_service_loop(
    runner: ExecutionRunner,
    *,
    interval_seconds: float,
    max_cycles: int | None = None,
) -> RunnerLoopReport:
    """Drive ``run_forever`` with per-cycle journal logging. Nothing else."""
    return runner.run_forever(
        interval_seconds=interval_seconds,
        max_cycles=max_cycles,
        on_cycle=_log_cycle,
    )


def service_exit_code(report: RunnerLoopReport, *, bounded: bool) -> int:
    """The shared exit-code rule (see the module docstring).

    - bounded verification run (``--max-cycles``): 0 only when EVERY cycle
      was ok -- the whole point of a bounded run is a judged result;
    - resident run: 0 when the loop ended by a requested stop. Per-cycle
      failures were already localized, logged and durably recorded; a clean
      operator stop is not the moment to convert them into a failed unit.
    """
    if bounded:
        return 0 if all(cycle.ok for cycle in report.cycles) else 1
    return 0 if report.stop_reason is RunnerStopReason.STOP_REQUESTED else 1


def loop_summary_payload(report: RunnerLoopReport) -> dict:
    """A machine-readable end-of-run summary (pure data, ASCII-safe values).

    This is how a bounded local verification run leaves a durable result
    behind (Issue #134: local test evidence under CI-quota constraints):
    ``--json`` prints exactly this document.
    """
    return {
        "stop_reason": report.stop_reason.value,
        "cycles_run": len(report.cycles),
        "broker_sends": report.broker_sends,
        "all_cycles_ok": all(cycle.ok for cycle in report.cycles),
        "cycles": [
            {
                "cycle_id": cycle.cycle_id,
                "cycle_index": cycle.cycle_index,
                "execution_mode": cycle.execution_mode,
                "dry_run": cycle.dry_run,
                "started_at_utc": cycle.started_at_utc.isoformat(),
                "finished_at_utc": cycle.finished_at_utc.isoformat(),
                "ok": cycle.ok,
                "processed": cycle.processed,
                "failed": cycle.failed,
                "broker_sends": cycle.broker_sends,
                "unresolved": cycle.unresolved,
                "status_counts": cycle.status_counts(),
                "selection_error": cycle.selection_error,
                "cycle_error": cycle.cycle_error,
                "stopped": cycle.stopped,
                "outcomes": [
                    {
                        "item_id": outcome.item_id,
                        "status": outcome.status.value,
                        "broker_contact": outcome.broker_contact.value,
                        "rejection_codes": list(outcome.rejection_codes),
                        "detail": outcome.detail,
                    }
                    for outcome in cycle.outcomes
                ],
            }
            for cycle in report.cycles
        ],
    }
