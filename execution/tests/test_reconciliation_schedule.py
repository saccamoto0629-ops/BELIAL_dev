"""Issue #133: the scheduled reconciliation cycle and its oneshot CLI.

Everything runs against ``tmp_path`` SQLite files and an in-memory
FakeBroker: no network, no Discord, no S3, no real orders, no wall-clock
sleep. The real OANDA API is never touched -- the ``oanda-demo`` path is
exercised through environment-driven REFUSALS only.

Like the Issue #103 / #104 / #105 suites, the cycle is driven through the
production ``ReconcileOnlyBroker`` facade, so every test here simultaneously
pins that a scheduled pass cannot reach a broker WRITE operation. Broker-side
fixtures are seeded on the FakeBroker before a cycle starts, which is why this
file is listed in the submission-boundary allow-list.
"""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from broker.broker_models import (
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderState,
    OrderType,
)
from broker.fake_broker import FakeBroker
from execution import execution_store, reconciliation_store
from execution import reconciliation_runner as runner
from execution import reconciliation_schedule as schedule
from execution import reconciliation_schedule_cli as cli
from execution.execution_models import ExecutionRequest
from execution.reconciliation_models import (
    DiscrepancyType,
    ResolutionCategory,
)
from execution.reconciliation_repair_models import RepairFailureCode, RepairOutcome

INTERNAL_T = datetime(2025, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
OBSERVED_AT = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
LATER_AT = OBSERVED_AT + timedelta(days=1)  # every lease long expired
NOW_ARG = "2026-02-01T00:00:00+00:00"
FILL_PRICE = Decimal("2400")
QUANTITY = Decimal("10")


def client_id(request_id: str) -> str:
    return f"client-{request_id}"


# --- fixtures -----------------------------------------------------------------


@pytest.fixture
def paths(tmp_path):
    """(execution DB, reconciliation DB) inside one data directory, exactly
    the layout ``--data-dir`` resolves to."""
    return tmp_path / "execution_orders.sqlite3", tmp_path / "broker_reconciliation.sqlite3"


def reconcile_only(inner) -> runner.ReconcileOnlyBroker:
    """The PRODUCTION facade, not a test double: a scheduled cycle accepts
    nothing else, so the tests must drive the real thing."""
    return runner.ReconcileOnlyBroker(inner)


def make_request(request_id: str, *, symbol: str = "XAUUSD") -> ExecutionRequest:
    return ExecutionRequest(
        execution_request_id=request_id,
        signal_id=f"sig-{request_id}",
        strategy_id="strat-1",
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=QUANTITY,
        requested_at_utc=INTERNAL_T,
    )


def broker_request(request_id: str, *, symbol: str = "XAUUSD") -> OrderRequest:
    """Built directly (not via ``ExecutionRequest.to_order_request``) so the
    fixture stays outside the sanctioned submission conversion path."""
    return OrderRequest(
        client_order_id=client_id(request_id),
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=QUANTITY,
        created_at_utc=INTERNAL_T,
    )


def seed_internal_unknown(db_path, request_id: str) -> None:
    """An internal order whose broker outcome is UNKNOWN -- the canonical
    ``auto_repair_candidate`` input once the broker side is known."""
    execution_store.create_execution_order(
        make_request(request_id),
        client_order_id=client_id(request_id),
        created_at_utc=INTERNAL_T,
        db_path=db_path,
    )
    execution_store.mark_submitting(
        request_id, reason="submitting", changed_at_utc=INTERNAL_T, db_path=db_path
    )
    execution_store.record_order_state(
        request_id,
        OrderState.UNKNOWN,
        reason="broker outcome unknown",
        changed_at_utc=INTERNAL_T + timedelta(seconds=1),
        db_path=db_path,
    )


def seed_agreeing_filled_order(db_path, broker: FakeBroker, request_id: str) -> OrderResult:
    """Both sides agree on a FILLED order: the ORDER domain stays quiet, and
    the resulting internal exposure is what the POSITION domain compares."""
    execution_store.create_execution_order(
        make_request(request_id),
        client_order_id=client_id(request_id),
        created_at_utc=INTERNAL_T,
        db_path=db_path,
    )
    execution_store.mark_submitting(
        request_id, reason="submitting", changed_at_utc=INTERNAL_T, db_path=db_path
    )
    result = broker.submit_order(broker_request(request_id))
    execution_store.record_order_result(
        request_id, result, reason="broker result", db_path=db_path
    )
    return result


def run_cycle(paths, broker, *, run_id, **overrides):
    execution_db, reconciliation_db = paths
    kwargs = dict(
        broker=broker,
        execution_db_path=execution_db,
        reconciliation_db_path=reconciliation_db,
        run_id=run_id,
        cycle_lock_path=schedule.cycle_lock_path_for(reconciliation_db),
        clock=lambda: OBSERVED_AT,
    )
    kwargs.update(overrides)
    return schedule.run_reconciliation_cycle(**kwargs)


def run_cli(tmp_path, *extra, run_id="cycle-1", now=NOW_ARG):
    argv = ["--broker", "fake", "--data-dir", str(tmp_path), "--run-id", run_id]
    if now is not None:
        argv += ["--now", now]
    return cli.main([*argv, *extra])


# --- the broker a cycle is allowed to hold ------------------------------------


def test_the_cycle_refuses_any_broker_outside_the_reconcile_only_facade(paths):
    """A submit-capable broker must not be reachable from a scheduled path,
    and a duck-typed near-miss is refused just as hard as the real thing."""

    class LooksQueryOnly:
        def get_order(self, client_order_id):  # pragma: no cover - never reached
            raise AssertionError("must not be called")

        def get_positions(self):  # pragma: no cover - never reached
            raise AssertionError("must not be called")

    for candidate in (FakeBroker(), LooksQueryOnly()):
        with pytest.raises(schedule.ReconciliationCycleError, match="ReconcileOnlyBroker"):
            run_cycle(paths, candidate, run_id="run-refused")


def test_the_facade_a_cycle_receives_cannot_submit_cancel_or_read_the_account():
    """The composition entry point the CLI uses hands back the POISONED
    facade -- pinned by calling the poisoned methods, not by reading code."""
    broker, label, timeout_seconds = runner.build_reconcile_only_broker("fake")

    assert isinstance(broker, runner.ReconcileOnlyBroker)
    assert label == "fake / fake"
    assert timeout_seconds is None  # no network, no timeout to enforce a lease against
    with pytest.raises(runner.ReconcileOnlyViolationError):
        broker.submit_order(broker_request("exec-x"))
    with pytest.raises(runner.ReconcileOnlyViolationError):
        broker.cancel_order(client_id("exec-x"))
    with pytest.raises(runner.ReconcileOnlyViolationError):
        broker.get_account_state()


# --- detection: the existing scopes, unchanged --------------------------------


def test_a_cycle_records_the_run_and_the_audit_survives_the_process(paths):
    """Restart continuity: the report is a VIEW, the audit DB is the record.
    A completely fresh read (no service object involved) must find the run,
    its discrepancies and its position summary."""
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-1")
    broker.submit_order(broker_request("exec-1"))  # broker-side fixture: FILLED

    report = run_cycle(paths, reconcile_only(broker), run_id="run-1")

    assert report.run_id == "run-1"
    assert report.recorded_to == reconciliation_db
    assert report.discrepancy_count == 1
    assert not report.is_clean

    stored = reconciliation_store.get_reconciliation_run("run-1", db_path=reconciliation_db)
    assert stored is not None
    (discrepancy,) = stored.discrepancies
    assert discrepancy.resolution_category is ResolutionCategory.AUTO_REPAIR_CANDIDATE
    assert stored.position_summary is not None  # the position scope really ran


def test_orders_only_skips_the_position_scope(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_agreeing_filled_order(execution_db, broker, "exec-2")

    with_positions = run_cycle(paths, reconcile_only(broker), run_id="run-2a")
    orders_only = run_cycle(
        paths, reconcile_only(broker), run_id="run-2b", include_position_scope=False
    )

    assert with_positions.run.position_summary is not None
    assert with_positions.run.position_discrepancies  # exposure the broker does not hold
    assert orders_only.run.position_summary is None
    assert orders_only.run.position_discrepancies == ()


# --- dry run: no side effect at all -------------------------------------------


def test_a_dry_run_cycle_creates_nothing_at_all(tmp_path, paths):
    """No audit DB, no execution DB, no lock file -- the data directory must
    be byte-for-byte absent afterwards."""
    _, reconciliation_db = paths
    broker = FakeBroker()

    report = run_cycle(
        paths,
        reconcile_only(broker),
        run_id="run-dry",
        dry_run=True,
        cycle_lock_path=schedule.cycle_lock_path_for(reconciliation_db),
    )

    assert report.dry_run is True
    assert report.recorded_to is None
    assert report.repair_skipped_reason is not None
    assert list(tmp_path.iterdir()) == []


def test_a_dry_run_cycle_refuses_repair(paths):
    with pytest.raises(schedule.ReconciliationCycleError, match="RECORDED run"):
        run_cycle(
            paths,
            reconcile_only(FakeBroker()),
            run_id="run-dry-repair",
            dry_run=True,
            repair_enabled=True,
        )


def test_a_recording_cycle_without_a_lock_path_is_refused(paths):
    """An unguarded scheduled run is not an option: the caller must name the
    lock, so overlap protection cannot be forgotten into existence."""
    with pytest.raises(schedule.ReconciliationCycleError, match="cycle_lock_path"):
        run_cycle(paths, reconcile_only(FakeBroker()), run_id="run-nolock", cycle_lock_path=None)


# --- overlap / concurrent runs -------------------------------------------------


def test_a_second_cycle_is_suppressed_while_the_lock_is_held(paths):
    """Real mutual exclusion, not a flag: the lock is held by a live holder
    for the duration of the attempt, and the suppressed cycle records
    NOTHING."""
    _, reconciliation_db = paths
    lock_path = schedule.cycle_lock_path_for(reconciliation_db)

    with schedule.cycle_lock(lock_path):
        with pytest.raises(schedule.CycleAlreadyRunningError):
            run_cycle(paths, reconcile_only(FakeBroker()), run_id="run-overlap")

    assert not reconciliation_db.exists()


def test_the_lock_is_released_when_a_cycle_ends(paths):
    broker = reconcile_only(FakeBroker())

    run_cycle(paths, broker, run_id="run-seq-1")
    second = run_cycle(paths, broker, run_id="run-seq-2")

    assert second.is_clean


def test_the_lock_is_released_when_a_cycle_raises(paths):
    """A crashed cycle must not leave the lock behind: the OS releases it
    when the holder goes away, and so must the in-process context manager."""
    execution_db, reconciliation_db = paths
    lock_path = schedule.cycle_lock_path_for(reconciliation_db)
    run_cycle(paths, reconcile_only(FakeBroker()), run_id="run-crash-1")

    with pytest.raises(reconciliation_store.ReconciliationStoreError):
        # recording the same run id twice is refused by the store
        run_cycle(paths, reconcile_only(FakeBroker()), run_id="run-crash-1")

    with schedule.cycle_lock(lock_path) as held:
        assert held == lock_path


def test_the_lock_waits_when_asked_and_still_refuses_a_busy_holder(paths):
    _, reconciliation_db = paths
    lock_path = schedule.cycle_lock_path_for(reconciliation_db)

    with schedule.cycle_lock(lock_path):
        with pytest.raises(schedule.CycleAlreadyRunningError):
            with schedule.cycle_lock(lock_path, wait_seconds=0.05):
                pass  # pragma: no cover - must never be entered


@pytest.mark.parametrize("bad", [-1, "5", True])
def test_the_lock_validates_its_wait(tmp_path, bad):
    with pytest.raises(ValueError):
        with schedule.cycle_lock(tmp_path / "cycle.lock", wait_seconds=bad):
            pass  # pragma: no cover - must never be entered


def test_an_unusable_lock_file_is_a_typed_refusal(tmp_path):
    lock_path = tmp_path / "not-a-lock"
    lock_path.write_text("this is not a SQLite database", encoding="utf-8")

    with pytest.raises(schedule.CycleLockError):
        with schedule.cycle_lock(lock_path):
            pass  # pragma: no cover - must never be entered


# --- repair: only candidates, through the existing path -----------------------


def test_an_auto_repair_candidate_goes_through_the_existing_repair_path(paths):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-3")
    broker.submit_order(broker_request("exec-3"))  # broker-side fixture: FILLED

    report = run_cycle(
        paths, reconcile_only(broker), run_id="run-3", repair_enabled=True, repair_owner_id="cyc"
    )

    (attempt,) = report.repair_attempts
    assert attempt.outcome is RepairOutcome.APPLIED
    assert (attempt.internal_state_before, attempt.internal_state_after) == (
        OrderState.UNKNOWN,
        OrderState.FILLED,
    )
    assert attempt.owner_id == "cyc"
    # the state moved through the existing state machine, with provenance
    row = execution_store.get_execution_order("exec-3", db_path=execution_db)
    assert row["order_state"] == "filled"
    history = execution_store.get_state_history("exec-3", db_path=execution_db)
    assert history[-1]["repair_attempt_id"] == attempt.attempt_id
    # and the audit row is durable, linked to this cycle's run
    stored = reconciliation_store.list_repair_attempts(
        run_id="run-3", db_path=reconciliation_db
    )
    assert [entry.attempt_id for entry in stored] == [attempt.attempt_id]
    assert reconciliation_store.list_pending_repair_attempts(db_path=reconciliation_db) == ()


def test_manual_review_order_discrepancies_are_never_auto_repaired(paths):
    """A broker-only order is ``manual_review``: adopting it would invent an
    internal order nobody asked for. The cycle must record it and stop."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.submit_order(broker_request("ghost"))  # broker-side only

    report = run_cycle(paths, reconcile_only(broker), run_id="run-4", repair_enabled=True)

    (discrepancy,) = report.run.discrepancies
    assert discrepancy.discrepancy_type is DiscrepancyType.BROKER_ONLY_ORDER
    assert discrepancy.resolution_category is ResolutionCategory.MANUAL_REVIEW
    (attempt,) = report.repair_attempts
    assert attempt.outcome is RepairOutcome.SKIPPED
    assert attempt.failure_code is RepairFailureCode.NOT_ELIGIBLE_BROKER_ONLY
    assert attempt.internal_state_after is None
    # nothing was adopted: no internal order was created for the ghost
    assert execution_store.get_execution_order("ghost", db_path=execution_db) is None


def test_position_scope_discrepancies_stay_manual_review_and_get_no_attempt(paths):
    """The position / trade / transaction / SL / TP domain has NO repair path
    at all: a repair-enabled cycle produces no attempt for it and closes,
    re-creates or cancels nothing."""
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_agreeing_filled_order(execution_db, broker, "exec-5")
    before = execution_store.get_execution_order("exec-5", db_path=execution_db)

    report = run_cycle(paths, reconcile_only(broker), run_id="run-5", repair_enabled=True)

    assert report.run.discrepancies == ()  # the order domain agrees
    assert report.run.position_discrepancies
    assert all(
        entry.resolution_category is ResolutionCategory.MANUAL_REVIEW
        for entry in report.run.position_discrepancies
    )
    # a repair pass ran, and had nothing it was allowed to act on
    assert report.repair_attempts == ()
    assert reconciliation_store.list_repair_attempts(
        run_id="run-5", db_path=reconciliation_db
    ) == ()
    assert execution_store.get_execution_order("exec-5", db_path=execution_db) == before


def test_a_detection_only_cycle_says_why_repair_did_not_run(paths):
    report = run_cycle(paths, reconcile_only(FakeBroker()), run_id="run-6")

    assert report.repair_enabled is False
    assert report.repair_attempts == ()
    assert "detection only" in report.repair_skipped_reason


def test_a_lease_shorter_than_the_broker_timeout_is_refused_as_configuration(paths):
    """The Issue #104 no-heartbeat contract is not relaxed for a scheduler:
    a query that could outlive its claim is refused before anything runs."""
    with pytest.raises(schedule.ReconciliationCycleError, match="refused this cycle"):
        run_cycle(
            paths,
            reconcile_only(FakeBroker()),
            run_id="run-lease",
            repair_enabled=True,
            lease_duration_seconds=5,
            broker_timeout_seconds=30,
        )


# --- restart continuity --------------------------------------------------------


def test_a_pending_attempt_from_a_crashed_cycle_is_taken_over_by_the_next_one(
    paths, monkeypatch
):
    """The crashed cycle left a claim, no state change and no finalized audit
    row. The NEXT cycle recovers it -- under the ORIGINAL attempt id and a
    new generation -- before it observes anything new."""
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-7")
    broker.submit_order(broker_request("exec-7"))

    def crash(*args, **kwargs):
        raise KeyboardInterrupt("process killed")  # crash-equivalent

    monkeypatch.setattr(execution_store, "record_order_result", crash)
    with pytest.raises(KeyboardInterrupt):
        run_cycle(paths, reconcile_only(broker), run_id="run-7a", repair_enabled=True)
    monkeypatch.undo()

    pending = reconciliation_store.list_pending_repair_attempts(db_path=reconciliation_db)
    assert len(pending) == 1
    crashed_attempt_id = pending[0].attempt_id
    assert execution_store.get_execution_order("exec-7", db_path=execution_db)[
        "order_state"
    ] == "unknown"

    report = run_cycle(
        paths,
        reconcile_only(broker),
        run_id="run-7b",
        repair_enabled=True,
        clock=lambda: LATER_AT,  # the crashed claim's lease has long expired
    )

    (recovered,) = report.recovered_attempts
    assert recovered.attempt_id == crashed_attempt_id  # same identity, not a new one
    assert recovered.outcome is RepairOutcome.APPLIED
    assert recovered.generation == 2
    assert execution_store.get_execution_order("exec-7", db_path=execution_db)[
        "order_state"
    ] == "filled"
    # the recovered state change happened BEFORE the new observation, so the
    # new run sees a settled order and finds nothing left to repair
    assert report.run.discrepancies == ()
    assert report.repair_attempts == ()
    assert reconciliation_store.list_pending_repair_attempts(db_path=reconciliation_db) == ()


def test_a_first_ever_repair_cycle_does_not_create_the_audit_db_to_ask_for_pending_work(paths):
    """No audit DB means no pending attempt can exist. Asking anyway would
    mean creating the DB before detection does."""
    _, reconciliation_db = paths
    assert not reconciliation_db.exists()

    report = run_cycle(
        paths, reconcile_only(FakeBroker()), run_id="run-8", repair_enabled=True
    )

    assert report.recovered_attempts == ()
    assert reconciliation_db.exists()  # created by the recording detection pass


def test_a_blocked_takeover_keeps_the_cycle_from_reporting_clean(paths):
    """A cycle with zero discrepancies but a BLOCKED attempt still needs a
    human, so it must not exit clean."""
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    report = run_cycle(paths, reconcile_only(broker), run_id="run-9")
    assert report.is_clean

    blocked = report.__class__(
        run_id=report.run_id,
        run=report.run,
        dry_run=False,
        include_position_scope=True,
        repair_enabled=True,
        recorded_to=reconciliation_db,
        recovered_attempts=(_blocked_attempt(),),
    )
    assert blocked.blocked_attempts
    assert not blocked.is_clean


def _blocked_attempt():
    from execution.reconciliation_models import ReconciliationTargetType
    from execution.reconciliation_repair_models import (
        OrderRepairAttempt,
        RepairMode,
    )

    return OrderRepairAttempt(
        attempt_id="blocked-1",
        run_id="run-9",
        mode=RepairMode.APPLY,
        target_type=ReconciliationTargetType.EXECUTION_ORDER,
        target_id="exec-blocked",
        outcome=RepairOutcome.BLOCKED,
        detail="takeover blocked",
        failure_code=RepairFailureCode.FENCE_AHEAD,
        reserved_at_utc=OBSERVED_AT,
        observed_at_utc=OBSERVED_AT,
        execution_request_id="exec-blocked",
        reconciliation_internal_state=OrderState.UNKNOWN,
        owner_id="cyc",
        claim_execution_id="claim-1",
        generation=1,
    )


# --- the oneshot CLI -----------------------------------------------------------


def test_cli_clean_run_exits_zero_and_records(tmp_path, capsys):
    assert run_cli(tmp_path) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "reconciliation cycle cycle-1" in out
    assert "result: CLEAN" in out
    assert (tmp_path / "broker_reconciliation.sqlite3").exists()


def test_cli_json_payload_is_pure_ascii_and_carries_the_run_shape(tmp_path, capsys):
    # the CLI builds its own EMPTY FakeBroker, so this internal UNKNOWN order
    # is what the run reports on
    seed_internal_unknown(tmp_path / "execution_orders.sqlite3", "exec-cli")

    assert run_cli(tmp_path, "--json") == cli.EXIT_ATTENTION

    raw = capsys.readouterr().out.strip()
    assert raw.isascii()
    payload = json.loads(raw)
    assert payload["outcome"] == "attention"
    assert payload["scheduled_cycle"] is True
    assert payload["reconcile_only"] is True
    assert payload["repair_enabled"] is False
    assert payload["reconciliation"]["run_id"] == "cycle-1"
    assert payload["reconciliation"]["broker"] == "fake / fake"
    assert len(payload["reconciliation"]["order_discrepancies"]) == 1


def test_cli_dry_run_creates_nothing(tmp_path, capsys):
    assert run_cli(tmp_path, "--dry-run") == cli.EXIT_OK
    assert "dry-run: nothing was persisted" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_cli_refuses_repair_with_dry_run(tmp_path, capsys):
    assert run_cli(tmp_path, "--dry-run", "--repair") == cli.EXIT_USAGE_ERROR
    assert "--repair cannot be combined with --dry-run" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "extra, marker",
    [
        (("--lock-wait-seconds", "-1"), "--lock-wait-seconds"),
        (("--lease-seconds", "0"), "--lease-seconds"),
        (("--now", "2026-02-01T00:00:00"), "invalid --now"),
        (("--now", "2026-02-01T00:00:00+09:00"), "invalid --now"),
    ],
)
def test_cli_usage_errors_exit_two_without_touching_the_data_dir(
    tmp_path, capsys, extra, marker
):
    assert run_cli(tmp_path, *extra, now=None) == cli.EXIT_USAGE_ERROR
    assert marker in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_cli_empty_run_id_is_a_usage_error(tmp_path, capsys):
    assert run_cli(tmp_path, run_id="   ") == cli.EXIT_USAGE_ERROR
    assert "--run-id must be a non-empty string" in capsys.readouterr().err


def test_cli_suppresses_and_exits_zero_when_another_cycle_holds_the_lock(tmp_path, capsys):
    lock_path = schedule.cycle_lock_path_for(tmp_path / "broker_reconciliation.sqlite3")

    with schedule.cycle_lock(lock_path):
        assert run_cli(tmp_path, "--json") == cli.EXIT_OK

    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["outcome"] == "suppressed_locked"
    assert payload["run_id"] is None
    assert "WARNING" in captured.err
    # nothing was recorded by the suppressed cycle
    assert not (tmp_path / "broker_reconciliation.sqlite3").exists()


def test_cli_repair_applies_only_candidates_and_reports_them(tmp_path, capsys):
    execution_db = tmp_path / "execution_orders.sqlite3"
    reconciliation_db = tmp_path / "broker_reconciliation.sqlite3"
    seed_internal_unknown(execution_db, "exec-cli-2")
    # the CLI's own FakeBroker is empty, so the internal UNKNOWN order is a
    # BROKER_ORDER_MISSING -> manual_review case, never auto-repaired
    assert run_cli(tmp_path, "--repair", "--json", "--repair-owner-id", "cli-owner") == (
        cli.EXIT_ATTENTION
    )

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["repair_enabled"] is True
    (attempt,) = payload["repair_attempts"]
    assert attempt["outcome"] == "skipped"
    assert attempt["owner_id"] == "cli-owner"
    assert execution_store.get_execution_order("exec-cli-2", db_path=execution_db)[
        "order_state"
    ] == "unknown"
    assert reconciliation_store.list_pending_repair_attempts(db_path=reconciliation_db) == ()


def test_cli_orders_only_skips_the_position_scope(tmp_path, capsys):
    assert run_cli(tmp_path, "--orders-only") == cli.EXIT_OK
    assert "position scope: not included" in capsys.readouterr().out


def test_cli_refuses_a_non_demo_oanda_endpoint_without_touching_the_network(
    tmp_path, capsys, monkeypatch
):
    """The adapter's fail-closed allowlist is what refuses; the CLI only
    surfaces the secret-free message and exits 2."""
    monkeypatch.setenv("OANDA_API_TOKEN", "cli-test-token-not-a-real-secret")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "101-000-9999999-001")
    monkeypatch.setenv("OANDA_API_URL", "https://api-fxtrade.oanda.com")

    exit_code = cli.main(["--broker", "oanda-demo", "--data-dir", str(tmp_path)])

    assert exit_code == cli.EXIT_USAGE_ERROR
    err = capsys.readouterr().err
    assert "broker configuration refused" in err
    assert "cli-test-token-not-a-real-secret" not in err
    assert list(tmp_path.iterdir()) == []


def test_cli_has_no_live_broker_option(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--broker", "oanda-live", "--data-dir", str(tmp_path)])
    assert excinfo.value.code == 2
    assert "oanda-live" not in cli.BROKER_CHOICES


def test_a_refused_takeover_recovery_still_records_detection_and_is_not_clean(
    paths, monkeypatch
):
    """Recovery is fail-closed: a pending reservation it must not resolve is
    REFUSED. The cycle then skips the repair pass (a human is needed) but
    still runs and records the read-only observation."""
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-10")
    broker.submit_order(broker_request("exec-10"))

    def crash(*args, **kwargs):
        raise KeyboardInterrupt("process killed")  # crash-equivalent

    monkeypatch.setattr(execution_store, "record_order_result", crash)
    with pytest.raises(KeyboardInterrupt):
        run_cycle(paths, reconcile_only(broker), run_id="run-10a", repair_enabled=True)
    monkeypatch.undo()

    from execution import broker_reconciliation_repair as repair_module

    def refuse(self, reservation, now):
        raise repair_module.ReconciliationRepairError("tampered reservation")

    monkeypatch.setattr(repair_module.BrokerOrderReconciliationRepairService, "_recover_one", refuse)

    report = run_cycle(
        paths,
        reconcile_only(broker),
        run_id="run-10b",
        repair_enabled=True,
        clock=lambda: LATER_AT,
    )

    assert report.recovered_attempts == ()
    assert report.repair_attempts == ()          # no pass stacked on top
    assert "takeover recovery refused" in report.repair_error
    assert not report.is_clean
    # detection still happened and is durable
    assert reconciliation_store.get_reconciliation_run(
        "run-10b", db_path=reconciliation_db
    ) is not None
    # the pending claim is untouched and still awaits a human
    assert [
        entry.attempt_id
        for entry in reconciliation_store.list_pending_repair_attempts(
            db_path=reconciliation_db
        )
    ] != []


def test_an_unwritable_data_directory_is_a_typed_refusal_naming_the_errno(tmp_path):
    """PR #137 Codex review P1: under a sandbox that leaves the parent
    read-only, the cycle must fail with a diagnosable reason -- the errno
    NAME, not a bare 'OSError' -- because the fix is a deployment one
    (ReadWritePaths / directory ownership), not a code one."""
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where a directory should be", encoding="utf-8")

    with pytest.raises(schedule.CycleLockError) as excinfo:
        with schedule.cycle_lock(blocked / "data" / "reconciliation_cycle.lock"):
            pass  # pragma: no cover - must never be entered

    message = str(excinfo.value)
    assert "cannot create the directory for the cycle lock" in message
    assert "ENOTDIR" in message or "EEXIST" in message, message


def force_fence(execution_db, request_id, *, attempt_id, generation, takeover, claim, owner="w"):
    """Install a repair fence directly, BYPASSING the takeover protocol, to
    build the corrupt cross-DB state recovery must refuse rather than
    resolve (the same helper shape the Issue #104 suite uses)."""
    with closing(sqlite3.connect(execution_db)) as connection:
        connection.execute(
            f"INSERT INTO {execution_store.FENCES_TABLE} (execution_request_id, "
            "repair_attempt_id, generation, takeover_id, owner_id, claim_execution_id, "
            "updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (execution_request_id) DO UPDATE SET repair_attempt_id = excluded."
            "repair_attempt_id, generation = excluded.generation, takeover_id = excluded."
            "takeover_id, owner_id = excluded.owner_id, claim_execution_id = excluded."
            "claim_execution_id, updated_at_utc = excluded.updated_at_utc",
            (request_id, attempt_id, generation, takeover, owner, claim, OBSERVED_AT.isoformat()),
        )
        connection.commit()


def test_a_blocked_takeover_stops_this_cycles_fresh_repair_pass(paths, monkeypatch):
    """PR #137 Codex review r2 P1.

    A BLOCKED recovery is the human-intervention boundary. A fresh pass in
    the same cycle would walk past it: its attempt id is new, and
    ``register_repair_fence`` REPLACES a fence held by a different attempt by
    design -- so the very order whose fencing state nobody could account for
    would be re-claimed at generation 1 and committed, every timer tick,
    while the blocked claim stayed pending.
    """
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-11")
    broker.submit_order(broker_request("exec-11"))

    def crash(*args, **kwargs):
        raise KeyboardInterrupt("process killed")  # crash-equivalent

    monkeypatch.setattr(execution_store, "record_order_result", crash)
    with pytest.raises(KeyboardInterrupt):
        run_cycle(paths, reconcile_only(broker), run_id="run-11a", repair_enabled=True)
    monkeypatch.undo()

    (crashed,) = reconciliation_store.list_pending_repair_attempts(db_path=reconciliation_db)
    # an unexplained generation-3 fence: no takeover intent accounts for it,
    # so recovery must BLOCK instead of overwriting it
    force_fence(
        execution_db,
        "exec-11",
        attempt_id=crashed.attempt_id,
        generation=3,
        takeover="tk-unknown",
        claim="claim-unknown",
    )

    report = run_cycle(
        paths,
        reconcile_only(broker),
        run_id="run-11b",
        repair_enabled=True,
        clock=lambda: LATER_AT,
    )

    (blocked,) = report.recovered_attempts
    assert blocked.outcome is RepairOutcome.BLOCKED
    assert blocked.failure_code is RepairFailureCode.FENCE_AHEAD
    # the boundary held: no fresh attempt, no state change, claim still pending
    assert report.repair_attempts == ()
    assert "BLOCKED" in report.repair_skipped_reason
    assert "needing a human" in report.repair_skipped_reason
    assert crashed.attempt_id in report.repair_skipped_reason
    assert execution_store.get_execution_order("exec-11", db_path=execution_db)[
        "order_state"
    ] == "unknown"
    assert [
        entry.attempt_id
        for entry in reconciliation_store.list_pending_repair_attempts(
            db_path=reconciliation_db
        )
    ] == [crashed.attempt_id]
    # the fence nobody could account for is untouched
    fence = execution_store.get_repair_fence("exec-11", db_path=execution_db)
    assert (fence["generation"], fence["takeover_id"]) == (3, "tk-unknown")
    # detection still ran and is durable -- that is what an operator diagnoses with
    assert reconciliation_store.get_reconciliation_run(
        "run-11b", db_path=reconciliation_db
    ) is not None
    assert not report.is_clean


def test_the_cli_says_a_blocked_takeover_skipped_the_repair_pass(tmp_path, capsys):
    """`attempts=0` must never read as "there was nothing to do"."""
    report = schedule.ReconciliationCycleReport(
        run_id="run-x",
        run=schedule.run_reconciliation_cycle(
            broker=reconcile_only(FakeBroker()),
            execution_db_path=tmp_path / "execution_orders.sqlite3",
            reconciliation_db_path=tmp_path / "broker_reconciliation.sqlite3",
            run_id="run-x",
            cycle_lock_path=None,
            dry_run=True,
            clock=lambda: OBSERVED_AT,
        ).run,
        dry_run=False,
        include_position_scope=True,
        repair_enabled=True,
        recorded_to=tmp_path / "broker_reconciliation.sqlite3",
        repair_skipped_reason="takeover recovery reported 1 BLOCKED attempt(s) (a-1)",
    )

    cli._print_human(cli._cycle_payload(report, broker_label="fake / fake"))

    out = capsys.readouterr().out
    assert "repair pass skipped: takeover recovery reported 1 BLOCKED attempt(s)" in out


def test_a_still_leased_claim_stops_this_cycles_fresh_repair_pass(paths, monkeypatch):
    """PR #137 Codex review r3 P1.

    Recovery returning NOTHING does not prove nothing is protected: it leaves
    alone every reservation whose lease is still valid. A killed cycle's
    claim stays leased after the process is gone, so the next tick can land
    inside it -- and the entry lock does not help, because a claim outlives
    the cycle that made it.
    """
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-12")
    broker.submit_order(broker_request("exec-12"))

    def crash(*args, **kwargs):
        raise KeyboardInterrupt("SIGKILL-equivalent")

    monkeypatch.setattr(execution_store, "record_order_result", crash)
    with pytest.raises(KeyboardInterrupt):
        run_cycle(paths, reconcile_only(broker), run_id="run-12a", repair_enabled=True)
    monkeypatch.undo()

    (leased,) = reconciliation_store.list_pending_repair_attempts(db_path=reconciliation_db)
    inside_lease = OBSERVED_AT + timedelta(seconds=60)
    assert inside_lease < leased.lease_expires_at_utc  # the lease really is still valid

    report = run_cycle(
        paths,
        reconcile_only(broker),
        run_id="run-12b",
        repair_enabled=True,
        clock=lambda: inside_lease,
    )

    # recovery correctly leaves it alone -- and the cycle must not step over it
    assert report.recovered_attempts == ()
    assert report.repair_attempts == ()
    assert leased.attempt_id in report.repair_skipped_reason
    assert "leases are simply still valid" in report.repair_skipped_reason
    assert execution_store.get_execution_order("exec-12", db_path=execution_db)[
        "order_state"
    ] == "unknown"
    # the claim and ITS fence are untouched
    fence = execution_store.get_repair_fence("exec-12", db_path=execution_db)
    assert fence["repair_attempt_id"] == leased.attempt_id
    assert [
        entry.attempt_id
        for entry in reconciliation_store.list_pending_repair_attempts(
            db_path=reconciliation_db
        )
    ] == [leased.attempt_id]
    # detection still ran and is durable
    assert reconciliation_store.get_reconciliation_run(
        "run-12b", db_path=reconciliation_db
    ) is not None


def test_the_deferral_is_self_healing_once_the_lease_expires(paths, monkeypatch):
    """Deferring must not mean abandoning: the NEXT cycle after the lease
    expires takes the attempt over under its ORIGINAL identity and applies."""
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-13")
    broker.submit_order(broker_request("exec-13"))

    def crash(*args, **kwargs):
        raise KeyboardInterrupt("SIGKILL-equivalent")

    monkeypatch.setattr(execution_store, "record_order_result", crash)
    with pytest.raises(KeyboardInterrupt):
        run_cycle(paths, reconcile_only(broker), run_id="run-13a", repair_enabled=True)
    monkeypatch.undo()

    (leased,) = reconciliation_store.list_pending_repair_attempts(db_path=reconciliation_db)
    # a tick inside the lease defers ...
    deferred = run_cycle(
        paths,
        reconcile_only(broker),
        run_id="run-13b",
        repair_enabled=True,
        clock=lambda: OBSERVED_AT + timedelta(seconds=60),
    )
    assert deferred.repair_attempts == ()

    # ... and a tick after it expires resolves the ORIGINAL attempt
    healed = run_cycle(
        paths,
        reconcile_only(broker),
        run_id="run-13c",
        repair_enabled=True,
        clock=lambda: LATER_AT,
    )

    (recovered,) = healed.recovered_attempts
    assert (recovered.attempt_id, recovered.outcome, recovered.generation) == (
        leased.attempt_id,
        RepairOutcome.APPLIED,
        2,
    )
    assert execution_store.get_execution_order("exec-13", db_path=execution_db)[
        "order_state"
    ] == "filled"
    assert reconciliation_store.list_pending_repair_attempts(db_path=reconciliation_db) == ()
    assert healed.repair_skipped_reason is None
