"""Issue #134: the shared resident-service plumbing (execution/runner_service.py).

Pins the process-level concerns BOTH service CLIs share: the exclusive
per-data-directory runner lock (fail-closed refusal, crash-safe release, no
stale lock to clean), the SIGTERM/SIGINT -> graceful-stop routing, the
exit-code contract (a resident stop is clean even after localized cycle
failures; a bounded verification run is judged), and the machine-readable
loop summary a bounded run leaves behind.

No broker, no network, no store: the module under test drives an already
composed runner and nothing else -- these tests compose the runner from a
stub executor.
"""

import signal
import sqlite3
from datetime import datetime, timezone

import pytest

from execution.live_runner import (
    BrokerContact,
    ExecutionRunner,
    RunnerCycleReport,
    RunnerItemOutcome,
    RunnerItemStatus,
    RunnerLoopReport,
    RunnerStopReason,
)
from execution.runner_service import (
    EXECUTION_RUNNER_LOCK_FILENAME,
    SHADOW_RUNNER_LOCK_FILENAME,
    RunnerAlreadyRunningError,
    RunnerServiceLockError,
    install_stop_signal_handlers,
    loop_summary_payload,
    run_service_loop,
    runner_service_lock,
    service_exit_code,
)
from execution.safety_gate import ExecutionMode

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class StubShadowExecutor:
    """A minimal shadow-mode executor: no broker, no store, deterministic."""

    execution_mode = ExecutionMode.SHADOW
    source_id = "stub-source"
    error_broker_contact = BrokerContact.NOT_SENT

    def __init__(self, items=()):
        self.items = list(items)
        self.executed = []

    def select(self, *, limit):
        return tuple(self.items[:limit])

    def execute(self, item_id):
        self.executed.append(item_id)
        return RunnerItemOutcome(
            item_id=item_id,
            status=RunnerItemStatus.RECORDED,
            broker_contact=BrokerContact.NOT_SENT,
            detail="stub recorded",
        )

    def plan(self, item_id):
        return RunnerItemOutcome(
            item_id=item_id,
            status=RunnerItemStatus.PLANNED,
            broker_contact=BrokerContact.NOT_SENT,
            detail="stub planned",
        )


def make_runner(items=()):
    return ExecutionRunner(
        executor=StubShadowExecutor(items), clock=lambda: NOW, runner_id="svc-test"
    )


def cycle_report(index=1, *, ok=True, stopped=False):
    return RunnerCycleReport(
        cycle_id=f"svc-test-cycle-{index}",
        cycle_index=index,
        execution_mode="shadow",
        dry_run=False,
        started_at_utc=NOW,
        finished_at_utc=NOW,
        cycle_error=None if ok else "boom",
        stopped=stopped,
    )


# --- the runner lock ------------------------------------------------------------


def test_the_lock_refuses_a_second_holder_and_releases_on_exit(tmp_path):
    lock = tmp_path / EXECUTION_RUNNER_LOCK_FILENAME
    with runner_service_lock(lock):
        with pytest.raises(RunnerAlreadyRunningError):
            with runner_service_lock(lock):
                pass  # pragma: no cover - never entered
    # Crash-safe release: after the first holder exits, the lock is free.
    with runner_service_lock(lock):
        pass


def test_the_lock_creates_the_data_directory_and_stays_empty(tmp_path):
    lock = tmp_path / "data" / "demo" / EXECUTION_RUNNER_LOCK_FILENAME
    with runner_service_lock(lock):
        assert lock.exists()
    # An empty SQLite file: nothing was ever written into it.
    assert lock.stat().st_size == 0


def test_an_unusable_lock_file_is_a_typed_error_not_a_crash(tmp_path):
    lock = tmp_path / "not-a-lock"
    lock.write_bytes(b"this is not a sqlite database, and never will be")
    with pytest.raises(RunnerServiceLockError):
        with runner_service_lock(lock):
            pass  # pragma: no cover - never entered


def test_an_uncreatable_lock_directory_is_a_typed_error(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the directory should go", encoding="utf-8")
    with pytest.raises(RunnerServiceLockError):
        with runner_service_lock(blocker / "sub" / "runner.lock"):
            pass  # pragma: no cover - never entered


def test_the_two_service_locks_are_different_files():
    """A shadow runner must never suppress a submission runner (nor the
    reverse), even if both were pointed at one directory by mistake."""
    assert EXECUTION_RUNNER_LOCK_FILENAME != SHADOW_RUNNER_LOCK_FILENAME


def test_the_runner_lock_never_collides_with_the_reconciliation_cycle_lock():
    """The submission runner and the reconciliation cycle are INDEPENDENT
    execution units sharing one demo data directory; their exclusivity
    domains must therefore be separate files."""
    from execution.reconciliation_schedule import DEFAULT_CYCLE_LOCK_FILENAME

    assert EXECUTION_RUNNER_LOCK_FILENAME != DEFAULT_CYCLE_LOCK_FILENAME
    assert SHADOW_RUNNER_LOCK_FILENAME != DEFAULT_CYCLE_LOCK_FILENAME


# --- signal handling ------------------------------------------------------------


def test_sigterm_and_sigint_request_the_runners_graceful_stop():
    runner = make_runner()
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        install_stop_signal_handlers(runner)
        assert runner.stop_requested is False
        signal.raise_signal(signal.SIGTERM)
        assert runner.stop_requested is True
        # A second signal is a no-op, not a second shutdown path.
        signal.raise_signal(signal.SIGINT)
        assert runner.stop_requested is True
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def test_install_refuses_a_non_runner():
    with pytest.raises(ValueError):
        install_stop_signal_handlers(object())


def test_a_stop_requested_before_the_loop_yields_a_clean_resident_exit():
    runner = make_runner(items=["item-1"])
    runner.request_stop()
    report = run_service_loop(runner, interval_seconds=0.01)
    assert report.stop_reason is RunnerStopReason.STOP_REQUESTED
    assert report.cycles == ()
    assert service_exit_code(report, bounded=False) == 0


# --- the loop and the exit-code contract ----------------------------------------


def test_a_bounded_run_executes_exactly_max_cycles_and_is_judged():
    runner = make_runner(items=["item-1"])
    report = run_service_loop(runner, interval_seconds=0.001, max_cycles=2)
    assert report.stop_reason is RunnerStopReason.MAX_CYCLES
    assert len(report.cycles) == 2
    assert all(cycle.ok for cycle in report.cycles)
    assert service_exit_code(report, bounded=True) == 0


def test_a_bounded_run_with_a_failed_cycle_exits_nonzero():
    report = RunnerLoopReport(
        cycles=(cycle_report(1), cycle_report(2, ok=False)),
        stop_reason=RunnerStopReason.MAX_CYCLES,
    )
    assert service_exit_code(report, bounded=True) == 1


def test_an_interrupted_bounded_run_is_not_a_passed_verification():
    """PR #138 Codex review P2: a SIGTERM part-way through --max-cycles N
    leaves only successful cycles behind, but the verification did not run
    to completion -- exit 1, the zero-cycle interruption included."""
    interrupted = RunnerLoopReport(
        cycles=(cycle_report(1),), stop_reason=RunnerStopReason.STOP_REQUESTED
    )
    assert service_exit_code(interrupted, bounded=True) == 1
    nothing_ran = RunnerLoopReport(cycles=(), stop_reason=RunnerStopReason.STOP_REQUESTED)
    assert service_exit_code(nothing_ran, bounded=True) == 1


def test_a_resident_stop_stays_clean_even_after_a_localized_cycle_failure():
    """Per-cycle failures are localized by design (Issue #131), logged and
    durably recorded; a clean operator stop must not convert them into a
    failed unit weeks later."""
    report = RunnerLoopReport(
        cycles=(cycle_report(1, ok=False), cycle_report(2)),
        stop_reason=RunnerStopReason.STOP_REQUESTED,
    )
    assert service_exit_code(report, bounded=False) == 0


def test_a_resident_loop_that_ended_without_a_stop_request_is_not_clean():
    report = RunnerLoopReport(cycles=(cycle_report(1),), stop_reason=RunnerStopReason.MAX_CYCLES)
    assert service_exit_code(report, bounded=False) == 1


# --- the loop summary -----------------------------------------------------------


def test_the_loop_summary_carries_every_cycle_and_every_outcome():
    runner = make_runner(items=["item-1", "item-2"])
    report = run_service_loop(runner, interval_seconds=0.001, max_cycles=1)
    payload = loop_summary_payload(report)
    assert payload["stop_reason"] == "max_cycles"
    assert payload["cycles_run"] == 1
    assert payload["broker_sends"] == 0
    assert payload["all_cycles_ok"] is True
    (cycle,) = payload["cycles"]
    assert cycle["execution_mode"] == "shadow"
    assert cycle["processed"] == 2
    assert {outcome["status"] for outcome in cycle["outcomes"]} == {"recorded"}
    assert {outcome["broker_contact"] for outcome in cycle["outcomes"]} == {"not_sent"}


def test_the_summary_is_json_serializable_pure_ascii():
    import json

    runner = make_runner(items=["item-1"])
    report = run_service_loop(runner, interval_seconds=0.001, max_cycles=1)
    encoded = json.dumps(loop_summary_payload(report), ensure_ascii=True, sort_keys=True)
    assert encoded.isascii()


def test_the_lock_file_is_a_zero_page_sqlite_database(tmp_path):
    """The lock never carries data: a crash leaves nothing to interpret and
    nothing to clean up."""
    lock = tmp_path / SHADOW_RUNNER_LOCK_FILENAME
    with runner_service_lock(lock):
        pass
    connection = sqlite3.connect(lock)
    try:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
    finally:
        connection.close()
