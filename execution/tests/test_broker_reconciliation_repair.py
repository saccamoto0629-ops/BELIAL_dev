"""Issue #104: broker reconciliation -- safe order-state REPAIR.

Everything runs against ``tmp_path`` SQLite files and an in-memory
FakeBroker: no network, no Discord, no S3, no sleep-based timing, no real
orders.

The repair service is driven through :class:`QueryOnlyBroker`, a facade
whose ``submit_order`` / ``cancel_order`` are POISONED -- so every test in
this file simultaneously pins that repair never writes through the broker.

Concurrency (Issue #104 re-review): the live-repair-vs-recovery and
recovery-vs-recovery suites REALLY overlap threads -- gates (events) pause a
live worker at a precise point while a recovery worker runs, and barriers
align competing workers -- they are never sequential calls dressed up as
concurrency tests.
"""

import ast
import itertools
import pathlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from broker.broker_models import (
    BrokerCommunicationError,
    BrokerTimeoutError,
    Fill,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderState,
    OrderType,
)
from broker.fake_broker import (
    FakeBroker,
    FillImmediately,
    PartiallyFill,
    RejectOrder,
    StayPending,
)
from execution import execution_store, reconciliation_store
from execution.broker_reconciliation import BrokerOrderReconciliationService
from execution.broker_reconciliation_repair import (
    BrokerOrderReconciliationRepairService,
    ReconciliationRepairError,
    ReconciliationRepairRunError,
)
from execution.execution_models import ExecutionRequest
from execution.reconciliation_models import (
    DiscrepancyType,
    OrderDiscrepancy,
    ReconciliationMode,
    ReconciliationRun,
    ReconciliationTargetType,
    ResolutionCategory,
)
from execution.reconciliation_repair_models import (
    RepairFailureCode,
    RepairMode,
    RepairOutcome,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Internal records predate FakeBroker's clock base (2026-01-01); detection
# observes at OBSERVED_AT, live repair runs at REPAIR_AT, recovery runs at
# RECOVERY_AT (long after every lease expired).
INTERNAL_T = datetime(2025, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
OBSERVED_AT = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
REPAIR_AT = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
RECOVERY_AT = datetime(2026, 3, 2, 0, 0, 0, tzinfo=timezone.utc)  # lease long expired
DURING_LEASE_AT = REPAIR_AT + timedelta(seconds=10)  # lease still valid
LEASE_SECONDS = 600
FILL_PRICE = Decimal("2400")
QUANTITY = Decimal("10")
GATE_TIMEOUT = 15


def client_id(request_id: str) -> str:
    return f"client-{request_id}"


class QueryOnlyBroker:
    """Query-only Broker facade over a FakeBroker (test double, in-memory).

    ``submit_order`` / ``cancel_order`` raise: repair must never reach a
    broker WRITE operation. ``get_order`` can be scripted to fail with a
    typed ``BrokerError`` and counts its calls (used by the recovery-race
    assertions)."""

    def __init__(self, inner: FakeBroker, *, query_failures: dict | None = None):
        self._inner = inner
        self._query_failures = dict(query_failures or {})
        self.query_count = 0

    def submit_order(self, request):  # pragma: no cover - must never run
        raise AssertionError("repair must never submit an order")

    def cancel_order(self, client_order_id):  # pragma: no cover - must never run
        raise AssertionError("repair must never cancel an order")

    def get_order(self, client_order_id):
        self.query_count += 1
        failure = self._query_failures.get(client_order_id)
        if failure is not None:
            raise failure
        return self._inner.get_order(client_order_id)

    def list_orders(self):
        return self._inner.list_orders()

    def get_positions(self):
        return self._inner.get_positions()

    def get_account_state(self):
        return self._inner.get_account_state()


class SnapshotBroker(QueryOnlyBroker):
    """Return selected external snapshots without mutating broker fixtures."""

    def __init__(self, inner: FakeBroker, snapshots: dict[str, OrderResult]):
        super().__init__(inner)
        self._snapshots = snapshots

    def get_order(self, client_order_id):
        self.query_count += 1
        return self._snapshots.get(client_order_id, self._inner.get_order(client_order_id))


class GateBroker(QueryOnlyBroker):
    """Blocks the FIRST get_order call until released (live-vs-recovery)."""

    def __init__(self, inner: FakeBroker):
        super().__init__(inner)
        self.entered = threading.Event()
        self.release = threading.Event()
        self._armed = True

    def get_order(self, client_order_id):
        if self._armed:
            self._armed = False
            self.entered.set()
            assert self.release.wait(timeout=GATE_TIMEOUT)
        return super().get_order(client_order_id)


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "execution_orders.sqlite3", tmp_path / "broker_reconciliation.sqlite3"


def record_run(broker, paths, *, run_id) -> ReconciliationRun:
    """Detect with the Issue #103 service and RECORD the run (repair input)."""
    execution_db, reconciliation_db = paths
    return BrokerOrderReconciliationService(
        broker=broker,
        execution_db_path=execution_db,
        reconciliation_db_path=reconciliation_db,
        clock=lambda: OBSERVED_AT,
    ).run(run_id=run_id)


def repair_service(
    broker, paths, *, prefix="attempt", clock=None, owner="worker-live", lease=LEASE_SECONDS
) -> BrokerOrderReconciliationRepairService:
    execution_db, reconciliation_db = paths
    counter = itertools.count(1)
    return BrokerOrderReconciliationRepairService(
        broker=broker,
        execution_db_path=execution_db,
        reconciliation_db_path=reconciliation_db,
        owner_id=owner,
        lease_duration_seconds=lease,
        clock=clock or (lambda: REPAIR_AT),
        attempt_id_factory=lambda: f"{prefix}-{next(counter)}",
    )


def recovery_service(broker, paths, *, owner="worker-recovery", clock=None, prefix="rec"):
    return repair_service(
        broker, paths, prefix=prefix, owner=owner, clock=clock or (lambda: RECOVERY_AT)
    )


# --- fixtures for both sides --------------------------------------------------


def make_request(request_id: str, *, quantity: Decimal = QUANTITY) -> ExecutionRequest:
    return ExecutionRequest(
        execution_request_id=request_id,
        signal_id=f"sig-{request_id}",
        strategy_id="strat-1",
        symbol="XAUUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=quantity,
        requested_at_utc=INTERNAL_T,
    )


def broker_request(request_id: str, *, quantity: Decimal = QUANTITY) -> OrderRequest:
    """Built directly (not via ``ExecutionRequest.to_order_request``) so the
    fixture does not touch the sanctioned submission conversion path."""
    return OrderRequest(
        client_order_id=client_id(request_id),
        symbol="XAUUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=quantity,
        created_at_utc=INTERNAL_T,
    )


def seed_internal_pending(db_path, request_id: str) -> None:
    execution_store.create_execution_order(
        make_request(request_id),
        client_order_id=client_id(request_id),
        created_at_utc=INTERNAL_T,
        db_path=db_path,
    )
    execution_store.mark_submitting(
        request_id, reason="submitting", changed_at_utc=INTERNAL_T, db_path=db_path
    )


def seed_internal_unknown(db_path, request_id: str) -> None:
    seed_internal_pending(db_path, request_id)
    execution_store.record_order_state(
        request_id,
        OrderState.UNKNOWN,
        reason="broker outcome unknown",
        changed_at_utc=INTERNAL_T + timedelta(seconds=1),
        db_path=db_path,
    )


def order_state(db_path, request_id: str):
    row = execution_store.get_execution_order(request_id, db_path=db_path)
    return row["order_state"], row["broker_order_id"]


def order_snapshot(db_path, request_ids):
    return {
        request_id: (
            execution_store.get_execution_order(request_id, db_path=db_path),
            execution_store.get_state_history(request_id, db_path=db_path),
        )
        for request_id in request_ids
    }


def unknown_snapshot(request_id: str, *, seconds: int = 0) -> OrderResult:
    return OrderResult(
        client_order_id=client_id(request_id),
        broker_order_id=None,
        state=OrderState.UNKNOWN,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        requested_quantity=QUANTITY,
        filled_quantity=Decimal("0"),
        remaining_quantity=QUANTITY,
        requested_price=None,
        average_fill_price=None,
        updated_at_utc=OBSERVED_AT + timedelta(seconds=seconds),
    )


def cancelled_snapshot(request_id: str, *, seconds: int = 0) -> OrderResult:
    return OrderResult(
        client_order_id=client_id(request_id),
        broker_order_id=None,
        state=OrderState.CANCELLED,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        requested_quantity=QUANTITY,
        filled_quantity=Decimal("0"),
        remaining_quantity=QUANTITY,
        requested_price=None,
        average_fill_price=None,
        updated_at_utc=OBSERVED_AT + timedelta(seconds=seconds),
    )


def only(entries):
    assert len(entries) == 1, entries
    return entries[0]


def finalized(reconciliation_db, run_id):
    return reconciliation_store.list_repair_attempts(run_id=run_id, db_path=reconciliation_db)


def pending(reconciliation_db, run_id=None):
    return reconciliation_store.list_pending_repair_attempts(
        run_id=run_id, db_path=reconciliation_db
    )


def seeded_candidate(paths, request_id, run_id):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, request_id)
    broker.submit_order(broker_request(request_id))
    record_run(QueryOnlyBroker(broker), paths, run_id=run_id)
    return broker


# --- safe repairs (issue scenarios 1-6) ---------------------------------------


def test_unknown_to_filled_is_applied_through_the_state_machine(paths):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-1")
    filled = broker.submit_order(broker_request("exec-1"))  # broker-side fixture: FILLED
    run = record_run(QueryOnlyBroker(broker), paths, run_id="run-1")
    observed = only(run.discrepancies)
    assert observed.resolution_category is ResolutionCategory.AUTO_REPAIR_CANDIDATE
    assert observed.internal_revision == 2  # pending + unknown history rows

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-1")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.APPLIED
    assert (attempt.internal_state_before, attempt.internal_state_after) == (
        OrderState.UNKNOWN,
        OrderState.FILLED,
    )
    assert attempt.execution_request_id == "exec-1"
    assert attempt.client_order_id == client_id("exec-1")
    assert attempt.observed_at_utc == OBSERVED_AT
    assert attempt.reserved_at_utc == REPAIR_AT
    assert (attempt.owner_id, attempt.generation) == ("worker-live", 1)
    # broker_state_updated_at is the broker-reported snapshot timestamp that
    # the committed history row carries (broker clock domain, known pre-commit)
    assert attempt.broker_state_updated_at_utc == filled.updated_at_utc
    assert attempt.finalized_at_utc == REPAIR_AT
    assert attempt.broker_order_id == "FAKE-1"
    assert attempt.failure_code is None
    assert order_state(execution_db, "exec-1") == ("filled", "FAKE-1")
    # STRUCTURED provenance on the committed history row (never reason parsing)
    history = execution_store.get_state_history("exec-1", db_path=execution_db)
    assert history[-1]["repair_attempt_id"] == attempt.attempt_id
    assert history[-1]["repair_generation"] == 1
    assert history[-1]["changed_at_utc"] == attempt.broker_state_updated_at_utc.isoformat()
    # the fence names the committing claim
    fence = execution_store.get_repair_fence("exec-1", db_path=execution_db)
    assert (fence["repair_attempt_id"], fence["generation"]) == (attempt.attempt_id, 1)
    # the audit row is finalized and linked to the run
    stored = only(finalized(reconciliation_db, "run-1"))
    assert stored == attempt
    assert pending(reconciliation_db) == ()


def test_unknown_to_cancelled_is_applied(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-2"), StayPending(broker_order_id="B-2"))
    broker.submit_order(broker_request("exec-2"))
    broker.cancel_order(client_id("exec-2"))  # broker-side fixture: CANCELLED
    seed_internal_unknown(execution_db, "exec-2")
    record_run(QueryOnlyBroker(broker), paths, run_id="run-2")

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-2")

    assert only(result.attempts).outcome is RepairOutcome.APPLIED
    assert order_state(execution_db, "exec-2") == ("cancelled", "B-2")


def test_unknown_to_rejected_is_applied(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-3"), RejectOrder(reason="insufficient margin"))
    broker.submit_order(broker_request("exec-3"))
    seed_internal_unknown(execution_db, "exec-3")
    record_run(QueryOnlyBroker(broker), paths, run_id="run-3")

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-3")

    assert only(result.attempts).outcome is RepairOutcome.APPLIED
    assert order_state(execution_db, "exec-3") == ("rejected", None)


def test_pending_submitting_to_filled_is_applied(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-4")
    broker.submit_order(broker_request("exec-4"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-4")

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-4")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.APPLIED
    assert attempt.internal_state_before is OrderState.PENDING
    assert order_state(execution_db, "exec-4")[0] == "filled"


def test_accepted_to_cancelled_is_applied(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-5"), StayPending(broker_order_id="B-5"))
    accepted = broker.submit_order(broker_request("exec-5"))
    seed_internal_pending(execution_db, "exec-5")
    execution_store.record_order_result("exec-5", accepted, reason="accepted", db_path=execution_db)
    broker.cancel_order(client_id("exec-5"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-5")

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-5")

    assert only(result.attempts).outcome is RepairOutcome.APPLIED
    assert order_state(execution_db, "exec-5") == ("cancelled", "B-5")


def test_partially_filled_to_filled_with_a_fill_prefix_is_applied(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(
        client_id("exec-6"),
        PartiallyFill(fills=((FILL_PRICE, Decimal("3")),), broker_order_id="B-6"),
    )
    partial = broker.submit_order(broker_request("exec-6"))
    seed_internal_pending(execution_db, "exec-6")
    execution_store.record_order_result("exec-6", partial, reason="partial", db_path=execution_db)
    remaining_fill = Fill(
        fill_id="F-6-2",
        price=FILL_PRICE,
        quantity=Decimal("7"),
        filled_at_utc=partial.updated_at_utc + timedelta(seconds=1),
    )
    filled = replace(
        partial,
        state=OrderState.FILLED,
        filled_quantity=QUANTITY,
        remaining_quantity=Decimal("0"),
        average_fill_price=FILL_PRICE,
        fills=partial.fills + (remaining_fill,),
        updated_at_utc=partial.updated_at_utc + timedelta(seconds=1),
    )
    facade = SnapshotBroker(broker, {client_id("exec-6"): filled})
    record_run(facade, paths, run_id="run-6")

    result = repair_service(facade, paths).repair(run_id="run-6")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.APPLIED
    assert attempt.internal_state_before is OrderState.PARTIALLY_FILLED
    assert order_state(execution_db, "exec-6") == ("filled", "B-6")
    restored = execution_store.load_order_result("exec-6", db_path=execution_db)
    assert restored.fills == filled.fills  # the known fill prefix survived


# --- ineligible / unsafe inputs -----------------------------------------------


def fabricate_recorded_run(paths, run_id, discrepancies) -> None:
    """Record a hand-built run: repair must re-validate, never trust it."""
    _, reconciliation_db = paths
    reconciliation_store.init_reconciliation_db(reconciliation_db)
    reconciliation_store.record_reconciliation_run(
        ReconciliationRun(
            run_id=run_id,
            mode=ReconciliationMode.DETECTION_ONLY,
            started_at_utc=OBSERVED_AT,
            finished_at_utc=OBSERVED_AT,
            checked_order_count=len(discrepancies),
            discrepancies=tuple(discrepancies),
        ),
        db_path=reconciliation_db,
    )


def test_a_forbidden_transition_in_a_stored_candidate_is_rejected(paths):
    """A stored run claiming a FILLED -> CANCELLED 'candidate' (tampered or
    buggy classification) must be refused by the state machine re-check."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    filled = broker.submit_order(broker_request("exec-7"))
    seed_internal_pending(execution_db, "exec-7")
    execution_store.record_order_result("exec-7", filled, reason="filled", db_path=execution_db)
    cancelled = replace(
        filled,
        state=OrderState.CANCELLED,
        filled_quantity=Decimal("0"),
        remaining_quantity=QUANTITY,
        average_fill_price=None,
        fills=(),
        updated_at_utc=filled.updated_at_utc + timedelta(seconds=1),
    )
    fabricate_recorded_run(
        paths,
        "run-7",
        [
            OrderDiscrepancy(
                discrepancy_type=DiscrepancyType.STATE_MISMATCH,
                target_type=ReconciliationTargetType.EXECUTION_ORDER,
                target_id="exec-7",
                resolution_category=ResolutionCategory.AUTO_REPAIR_CANDIDATE,
                observed_at_utc=OBSERVED_AT,
                detail="tampered: claims a terminal rollback is repairable",
                internal_state=OrderState.FILLED,
                broker_state=OrderState.CANCELLED,
                client_order_id=client_id("exec-7"),
                broker_order_id=filled.broker_order_id,
                internal_revision=2,
            )
        ],
    )
    before = order_snapshot(execution_db, ("exec-7",))

    result = repair_service(
        SnapshotBroker(
            broker,
            {client_id("exec-7"): replace(cancelled, broker_order_id=filled.broker_order_id)},
        ),
        paths,
    ).repair(run_id="run-7")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.REJECTED
    assert attempt.failure_code is RepairFailureCode.FORBIDDEN_TRANSITION
    assert order_snapshot(execution_db, ("exec-7",)) == before


def test_manual_review_targets_are_never_repaired(paths):
    """Internal ACCEPTED vs broker REJECTED without a broker id (a known
    Issue #103 manual-review case) plus a broker-only order. Both attempts
    are claimed and finalized as skipped -- audited, never applied."""
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-9"), StayPending(broker_order_id="B-9"))
    accepted = broker.submit_order(broker_request("exec-9"))
    seed_internal_pending(execution_db, "exec-9")
    execution_store.record_order_result("exec-9", accepted, reason="accepted", db_path=execution_db)
    rejected = OrderResult(
        client_order_id=client_id("exec-9"),
        broker_order_id=None,
        state=OrderState.REJECTED,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        requested_quantity=QUANTITY,
        filled_quantity=Decimal("0"),
        remaining_quantity=QUANTITY,
        requested_price=None,
        average_fill_price=None,
        updated_at_utc=accepted.updated_at_utc + timedelta(seconds=1),
        reject_reason="rejected later",
    )
    broker.submit_order(broker_request("stranger"))  # broker-only order
    facade = SnapshotBroker(broker, {client_id("exec-9"): rejected})
    run = record_run(facade, paths, run_id="run-9")
    assert {d.resolution_category for d in run.discrepancies} == {ResolutionCategory.MANUAL_REVIEW}
    before = order_snapshot(execution_db, ("exec-9",))

    result = repair_service(facade, paths).repair(run_id="run-9")

    assert len(result.attempts) == 2
    assert {a.outcome for a in result.attempts} == {RepairOutcome.SKIPPED}
    broker_only = only(
        [a for a in result.attempts if a.target_type is ReconciliationTargetType.BROKER_ORDER]
    )
    assert broker_only.execution_request_id is None
    assert broker_only.failure_code is RepairFailureCode.NOT_ELIGIBLE_BROKER_ONLY
    assert order_snapshot(execution_db, ("exec-9",)) == before
    assert len(finalized(reconciliation_db, "run-9")) == 2
    assert pending(reconciliation_db) == ()
    # skipped attempts never register an execution fence
    assert execution_store.get_repair_fence("exec-9", db_path=execution_db) is None


def test_query_failed_reconciliation_targets_are_skipped(paths):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-10")
    broker.submit_order(broker_request("exec-10"))
    detect_facade = QueryOnlyBroker(
        broker, query_failures={client_id("exec-10"): BrokerTimeoutError("timeout")}
    )
    run = record_run(detect_facade, paths, run_id="run-10")
    assert only(run.discrepancies).resolution_category is ResolutionCategory.QUERY_FAILED
    before = order_snapshot(execution_db, ("exec-10",))

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-10")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.SKIPPED
    assert attempt.failure_code is RepairFailureCode.NOT_ELIGIBLE_QUERY_FAILED
    assert order_snapshot(execution_db, ("exec-10",)) == before


def test_a_broker_that_now_reports_unknown_changes_nothing(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-11")
    broker.submit_order(broker_request("exec-11"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-11")
    before = order_snapshot(execution_db, ("exec-11",))

    result = repair_service(
        SnapshotBroker(broker, {client_id("exec-11"): unknown_snapshot("exec-11")}), paths
    ).repair(run_id="run-11")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.CONFLICT
    assert attempt.failure_code is RepairFailureCode.REQUERY_DISAGREES_WITH_OBSERVATION
    assert order_snapshot(execution_db, ("exec-11",)) == before


@pytest.mark.parametrize(
    "error, expected_code, expected_failure",
    [
        (BrokerTimeoutError("timed out"), "timeout", RepairFailureCode.BROKER_QUERY_FAILED),
        (
            BrokerCommunicationError("connection refused"),
            "communication_failure",
            RepairFailureCode.BROKER_QUERY_FAILED,
        ),
        (
            RuntimeError("adapter bug"),
            "communication_failure",
            RepairFailureCode.BROKER_CONTRACT_VIOLATION,
        ),
    ],
)
def test_a_repair_time_query_failure_changes_nothing(paths, error, expected_code, expected_failure):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-12")
    broker.submit_order(broker_request("exec-12"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-12")
    before = order_snapshot(execution_db, ("exec-12",))

    result = repair_service(
        QueryOnlyBroker(broker, query_failures={client_id("exec-12"): error}), paths
    ).repair(run_id="run-12")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.QUERY_FAILED
    assert attempt.broker_error_code.value == expected_code
    assert attempt.failure_code is expected_failure
    assert "adapter bug" not in attempt.detail
    assert "connection refused" not in attempt.detail
    assert order_snapshot(execution_db, ("exec-12",)) == before
    assert only(finalized(reconciliation_db, "run-12")).outcome is RepairOutcome.QUERY_FAILED


def test_a_changed_broker_reference_id_is_a_conflict(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-14"), StayPending(broker_order_id="B-ORIG"))
    accepted = broker.submit_order(broker_request("exec-14"))
    seed_internal_pending(execution_db, "exec-14")
    execution_store.record_order_result("exec-14", accepted, reason="accepted", db_path=execution_db)
    cancelled = broker.cancel_order(client_id("exec-14"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-14")
    foreign = replace(
        cancelled,
        broker_order_id="B-FOREIGN",
        updated_at_utc=cancelled.updated_at_utc + timedelta(seconds=1),
    )
    before = order_snapshot(execution_db, ("exec-14",))

    result = repair_service(
        SnapshotBroker(broker, {client_id("exec-14"): foreign}), paths
    ).repair(run_id="run-14")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.CONFLICT
    assert attempt.failure_code is RepairFailureCode.BROKER_REFERENCE_CHANGED
    assert order_snapshot(execution_db, ("exec-14",)) == before


def test_a_broker_id_now_owned_by_another_request_is_a_conflict(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(
        client_id("exec-clash"), FillImmediately(price=FILL_PRICE, broker_order_id="B-1")
    )
    broker.submit_order(broker_request("exec-clash"))
    seed_internal_pending(execution_db, "exec-clash")
    seed_internal_pending(execution_db, "exec-owner")
    run = record_run(QueryOnlyBroker(broker), paths, run_id="run-15")
    clash = only([d for d in run.discrepancies if d.target_id == "exec-clash"])
    assert clash.resolution_category is ResolutionCategory.AUTO_REPAIR_CANDIDATE
    execution_store.record_order_result(
        "exec-owner",
        OrderResult(
            client_order_id=client_id("exec-owner"),
            broker_order_id="B-1",
            state=OrderState.FILLED,
            symbol="XAUUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            requested_quantity=QUANTITY,
            filled_quantity=QUANTITY,
            remaining_quantity=Decimal("0"),
            requested_price=None,
            average_fill_price=FILL_PRICE,
            updated_at_utc=OBSERVED_AT,
            fills=(
                Fill(
                    fill_id="F-OWN",
                    price=FILL_PRICE,
                    quantity=QUANTITY,
                    filled_at_utc=OBSERVED_AT,
                ),
            ),
        ),
        reason="filled elsewhere",
        db_path=execution_db,
    )
    before = order_snapshot(execution_db, ("exec-clash", "exec-owner"))

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-15")

    by_target = {a.target_id: a for a in result.attempts}
    assert by_target["exec-clash"].outcome is RepairOutcome.CONFLICT
    assert by_target["exec-clash"].failure_code is RepairFailureCode.BROKER_REFERENCE_OWNED_ELSEWHERE
    assert by_target["exec-owner"].outcome is RepairOutcome.SKIPPED
    assert order_snapshot(execution_db, ("exec-clash", "exec-owner")) == before


def test_an_ownership_race_after_the_lookup_is_still_a_conflict(paths, monkeypatch):
    """The lookup answers 'free' but another request claims the id before
    the CAS commits -- the store's uniqueness constraint rolls back."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(
        client_id("exec-race"), FillImmediately(price=FILL_PRICE, broker_order_id="B-1")
    )
    broker.submit_order(broker_request("exec-race"))
    seed_internal_pending(execution_db, "exec-race")
    seed_internal_pending(execution_db, "exec-owner")
    record_run(QueryOnlyBroker(broker), paths, run_id="run-16")
    execution_store.record_order_result(
        "exec-owner",
        OrderResult(
            client_order_id=client_id("exec-owner"),
            broker_order_id="B-1",
            state=OrderState.FILLED,
            symbol="XAUUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            requested_quantity=QUANTITY,
            filled_quantity=QUANTITY,
            remaining_quantity=Decimal("0"),
            requested_price=None,
            average_fill_price=FILL_PRICE,
            updated_at_utc=OBSERVED_AT,
            fills=(
                Fill(
                    fill_id="F-OWN",
                    price=FILL_PRICE,
                    quantity=QUANTITY,
                    filled_at_utc=OBSERVED_AT,
                ),
            ),
        ),
        reason="filled elsewhere",
        db_path=execution_db,
    )
    monkeypatch.setattr(
        execution_store, "get_execution_order_by_broker_order_id", lambda *a, **k: None
    )
    before = order_snapshot(execution_db, ("exec-race",))

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-16")

    clash = only([a for a in result.attempts if a.target_id == "exec-race"])
    assert clash.outcome is RepairOutcome.CONFLICT
    assert clash.failure_code is RepairFailureCode.UNIQUENESS_CONFLICT
    assert order_snapshot(execution_db, ("exec-race",)) == before


# --- revision CAS: same-state changes are detected ----------------------------


def same_state_change(execution_db, request_id, kind):
    """Mutate the order AFTER detection without changing its state text."""
    if kind == "unknown_history":
        execution_store.record_order_state(
            request_id,
            OrderState.UNKNOWN,
            reason="another unknown observation",
            changed_at_utc=OBSERVED_AT + timedelta(seconds=5),
            db_path=execution_db,
        )
    elif kind == "accepted_reobservation":
        anchored = execution_store.load_order_result(request_id, db_path=execution_db)
        execution_store.record_order_result(
            request_id,
            replace(anchored, updated_at_utc=anchored.updated_at_utc + timedelta(seconds=5)),
            reason="accepted re-observation (timestamp only)",
            db_path=execution_db,
        )
    elif kind == "partial_fill_growth":
        anchored = execution_store.load_order_result(request_id, db_path=execution_db)
        extra = Fill(
            fill_id="F-EXTRA",
            price=FILL_PRICE,
            quantity=Decimal("2"),
            filled_at_utc=anchored.updated_at_utc + timedelta(seconds=5),
        )
        execution_store.record_order_result(
            request_id,
            replace(
                anchored,
                filled_quantity=anchored.filled_quantity + Decimal("2"),
                remaining_quantity=anchored.remaining_quantity - Decimal("2"),
                fills=anchored.fills + (extra,),
                updated_at_utc=anchored.updated_at_utc + timedelta(seconds=5),
            ),
            reason="further partial fill",
            db_path=execution_db,
        )
    else:  # pragma: no cover - test wiring
        raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["unknown_history", "accepted_reobservation", "partial_fill_growth"])
def test_a_same_state_change_after_the_observation_is_stale(paths, kind):
    """State-only CAS cannot see these; the revision CAS must: an UNKNOWN ->
    UNKNOWN history append, a same-state snapshot/timestamp refresh, and a
    further partial fill all leave the state text unchanged."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    if kind == "unknown_history":
        seed_internal_unknown(execution_db, "exec-17")
        broker.submit_order(broker_request("exec-17"))
    elif kind == "accepted_reobservation":
        broker.script_order(client_id("exec-17"), StayPending(broker_order_id="B-17"))
        accepted = broker.submit_order(broker_request("exec-17"))
        seed_internal_pending(execution_db, "exec-17")
        execution_store.record_order_result(
            "exec-17", accepted, reason="accepted", db_path=execution_db
        )
        broker.cancel_order(client_id("exec-17"))  # broker-side settled -> candidate
    else:
        broker.script_order(
            client_id("exec-17"),
            PartiallyFill(fills=((FILL_PRICE, Decimal("3")),), broker_order_id="B-17"),
        )
        partial = broker.submit_order(broker_request("exec-17"))
        seed_internal_pending(execution_db, "exec-17")
        execution_store.record_order_result(
            "exec-17", partial, reason="partial", db_path=execution_db
        )
        broker.cancel_order(client_id("exec-17"))  # broker-side settled -> candidate
    run = record_run(QueryOnlyBroker(broker), paths, run_id="run-17")
    observed = only(run.discrepancies)
    assert observed.resolution_category is ResolutionCategory.AUTO_REPAIR_CANDIDATE

    same_state_change(execution_db, "exec-17", kind)
    before = order_snapshot(execution_db, ("exec-17",))

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-17")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.STALE
    assert attempt.failure_code is RepairFailureCode.INTERNAL_ORDER_CHANGED
    assert attempt.internal_state_before is observed.internal_state  # state text unchanged
    assert order_snapshot(execution_db, ("exec-17",)) == before


def test_a_stale_reconciliation_result_never_rolls_back_newer_state(paths):
    """The normal submission path settles the order differently after the
    observation -- the repair must not overwrite it."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-18")
    broker.submit_order(broker_request("exec-18"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-18")
    execution_store.record_order_result(
        "exec-18",
        cancelled_snapshot("exec-18", seconds=1),
        reason="cancelled by the submission path",
        db_path=execution_db,
    )
    before = order_snapshot(execution_db, ("exec-18",))

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-18")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.STALE
    assert order_snapshot(execution_db, ("exec-18",)) == before


def test_an_observation_without_a_revision_is_stale(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.submit_order(broker_request("exec-19"))
    seed_internal_unknown(execution_db, "exec-19")
    fabricate_recorded_run(
        paths,
        "run-19",
        [
            OrderDiscrepancy(
                discrepancy_type=DiscrepancyType.UNKNOWN_BROKER_SETTLED,
                target_type=ReconciliationTargetType.EXECUTION_ORDER,
                target_id="exec-19",
                resolution_category=ResolutionCategory.AUTO_REPAIR_CANDIDATE,
                observed_at_utc=OBSERVED_AT,
                detail="legacy record without a revision",
                internal_state=OrderState.UNKNOWN,
                broker_state=OrderState.FILLED,
                client_order_id=client_id("exec-19"),
                broker_order_id="FAKE-1",
                internal_revision=None,
            )
        ],
    )
    before = order_snapshot(execution_db, ("exec-19",))

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-19")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.STALE
    assert attempt.failure_code is RepairFailureCode.MISSING_OBSERVED_REVISION
    assert order_snapshot(execution_db, ("exec-19",)) == before


# --- already_applied: three-way verification ----------------------------------


def applied_elsewhere(paths, request_id, run_id):
    """Candidate observed FILLED; the same outcome is recorded by the normal
    path after detection."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, request_id)
    filled = broker.submit_order(broker_request(request_id))
    record_run(QueryOnlyBroker(broker), paths, run_id=run_id)
    execution_store.record_order_result(request_id, filled, reason="filled", db_path=execution_db)
    return broker, filled


def test_already_applied_requires_a_fresh_matching_broker_snapshot(paths):
    execution_db, reconciliation_db = paths
    broker, _ = applied_elsewhere(paths, "exec-21", "run-21")
    before = order_snapshot(execution_db, ("exec-21",))

    facade = QueryOnlyBroker(broker)
    result = repair_service(facade, paths).repair(run_id="run-21")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.ALREADY_APPLIED
    assert attempt.internal_state_after is OrderState.FILLED
    assert facade.query_count == 1  # the broker WAS asked before the verdict
    assert order_snapshot(execution_db, ("exec-21",)) == before  # no new history row
    assert only(finalized(reconciliation_db, "run-21")).outcome is RepairOutcome.ALREADY_APPLIED


def test_already_applied_is_never_concluded_without_asking_the_broker(paths):
    execution_db, _ = paths
    broker, _ = applied_elsewhere(paths, "exec-22", "run-22")
    before = order_snapshot(execution_db, ("exec-22",))

    result = repair_service(
        QueryOnlyBroker(broker, query_failures={client_id("exec-22"): BrokerTimeoutError("t")}),
        paths,
    ).repair(run_id="run-22")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.QUERY_FAILED
    assert attempt.broker_error_code.value == "timeout"
    assert order_snapshot(execution_db, ("exec-22",)) == before


@pytest.mark.parametrize(
    "mutate, expected_fragment",
    [
        (lambda filled: unknown_snapshot("exec-23", seconds=30), "differs from the observed"),
        (
            lambda filled: replace(
                filled,
                average_fill_price=filled.average_fill_price + Decimal("1"),
                fills=(replace(filled.fills[0], price=filled.fills[0].price + Decimal("1")),),
                updated_at_utc=filled.updated_at_utc + timedelta(seconds=30),
            ),
            "update contract",
        ),
        (
            lambda filled: replace(
                filled,
                broker_order_id="B-OTHER",
                updated_at_utc=filled.updated_at_utc + timedelta(seconds=30),
            ),
            "broker_order_id",
        ),
        (
            lambda filled: replace(
                filled, updated_at_utc=filled.updated_at_utc - timedelta(seconds=30)
            ),
            "older",
        ),
    ],
)
def test_already_applied_rejects_any_three_way_mismatch(paths, mutate, expected_fragment):
    execution_db, _ = paths
    broker, filled = applied_elsewhere(paths, "exec-23", "run-23")
    fresh = mutate(filled)
    before = order_snapshot(execution_db, ("exec-23",))

    result = repair_service(
        SnapshotBroker(broker, {client_id("exec-23"): fresh}), paths
    ).repair(run_id="run-23")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.CONFLICT
    assert attempt.failure_code is RepairFailureCode.ALREADY_APPLIED_CONTENT_MISMATCH
    assert expected_fragment in attempt.detail
    assert order_snapshot(execution_db, ("exec-23",)) == before


# --- dry-run: complete no-side-effect contract --------------------------------


def test_dry_run_evaluates_but_writes_nothing(paths, tmp_path):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-24")
    broker.submit_order(broker_request("exec-24"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-24")
    execution_bytes = execution_db.read_bytes()
    reconciliation_bytes = reconciliation_db.read_bytes()
    mtimes_before = (execution_db.stat().st_mtime_ns, reconciliation_db.stat().st_mtime_ns)
    files_before = sorted(p.name for p in tmp_path.iterdir())
    submits, cancels = len(broker.submitted_requests), len(broker.cancel_requests)

    result = repair_service(QueryOnlyBroker(broker), paths).repair(
        run_id="run-24", mode=RepairMode.DRY_RUN
    )

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.WOULD_APPLY
    assert attempt.internal_state_after is OrderState.FILLED
    assert attempt.broker_state_updated_at_utc is None
    assert (attempt.owner_id, attempt.claim_execution_id, attempt.generation) == (
        None,
        None,
        None,
    )
    # bytes, mtimes, directory entries (journal/WAL/SHM sidecars included)
    assert execution_db.read_bytes() == execution_bytes
    assert reconciliation_db.read_bytes() == reconciliation_bytes
    assert (execution_db.stat().st_mtime_ns, reconciliation_db.stat().st_mtime_ns) == mtimes_before
    assert sorted(p.name for p in tmp_path.iterdir()) == files_before
    # no claim, no fence, no audit row, no broker write
    assert pending(reconciliation_db) == ()
    assert finalized(reconciliation_db, "run-24") == ()
    assert execution_store.get_repair_fence("exec-24", db_path=execution_db) is None
    assert (len(broker.submitted_requests), len(broker.cancel_requests)) == (submits, cancels)
    assert order_state(execution_db, "exec-24")[0] == "unknown"


def test_dry_run_refuses_a_corrupt_reconciliation_db_without_touching_it(paths):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-25")
    broker.submit_order(broker_request("exec-25"))
    reconciliation_db.write_bytes(b"this is not a sqlite database")
    before = reconciliation_db.read_bytes()
    execution_before = execution_db.read_bytes()

    with pytest.raises(reconciliation_store.ReconciliationStoreError):
        repair_service(QueryOnlyBroker(broker), paths).repair(
            run_id="run-25", mode=RepairMode.DRY_RUN
        )

    assert reconciliation_db.read_bytes() == before
    assert execution_db.read_bytes() == execution_before


# --- idempotency --------------------------------------------------------------


def test_repeating_the_same_repair_is_idempotent(paths):
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-26", "run-26")

    first = repair_service(QueryOnlyBroker(broker), paths, prefix="first").repair(run_id="run-26")
    after_first = order_snapshot(execution_db, ("exec-26",))
    second = repair_service(QueryOnlyBroker(broker), paths, prefix="second").repair(run_id="run-26")
    third = repair_service(QueryOnlyBroker(broker), paths, prefix="third").repair(run_id="run-26")

    assert only(first.attempts).outcome is RepairOutcome.APPLIED
    assert only(second.attempts).outcome is RepairOutcome.ALREADY_APPLIED
    assert only(third.attempts).outcome is RepairOutcome.ALREADY_APPLIED
    assert order_snapshot(execution_db, ("exec-26",)) == after_first
    assert order_state(execution_db, "exec-26")[0] == "filled"
    attempts = finalized(reconciliation_db, "run-26")
    assert [a.outcome for a in attempts] == [
        RepairOutcome.APPLIED,
        RepairOutcome.ALREADY_APPLIED,
        RepairOutcome.ALREADY_APPLIED,
    ]


def test_concurrent_live_repairs_never_duplicate_history_or_state(paths):
    """Two LIVE workers, one target, genuinely overlapped via a barrier in
    the attempt-id factory. Exactly one applies; the other observes a typed
    no-op (already_applied) or lost claim (conflict) -- never a second
    history row and never FAILED."""
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-20", "run-20")
    history_before = len(execution_store.get_state_history("exec-20", db_path=execution_db))

    barrier = threading.Barrier(2, timeout=10)

    def worker(prefix):
        counter = itertools.count(1)

        def aligned_attempt_id():
            barrier.wait()  # both workers overlap from the very start
            return f"{prefix}-{next(counter)}"

        service = BrokerOrderReconciliationRepairService(
            broker=QueryOnlyBroker(broker),
            execution_db_path=execution_db,
            reconciliation_db_path=reconciliation_db,
            owner_id=f"owner-{prefix}",
            lease_duration_seconds=LEASE_SECONDS,
            clock=lambda: REPAIR_AT,
            attempt_id_factory=aligned_attempt_id,
        )
        return service.repair(run_id="run-20")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result() for f in [pool.submit(worker, "w1"), pool.submit(worker, "w2")]]

    outcomes = [only(result.attempts).outcome for result in results]
    assert sum(1 for o in outcomes if o is RepairOutcome.APPLIED) == 1
    losers = [o for o in outcomes if o is not RepairOutcome.APPLIED]
    assert len(losers) == 1
    assert losers[0] in (RepairOutcome.ALREADY_APPLIED, RepairOutcome.CONFLICT)
    assert order_state(execution_db, "exec-20")[0] == "filled"
    history = execution_store.get_state_history("exec-20", db_path=execution_db)
    assert len(history) == history_before + 1  # exactly one repair history row


# --- post-commit failures -----------------------------------------------------


class StepClock:
    """A clock that fails from the Nth call on."""

    def __init__(self, *, fail_from: int):
        self.calls = 0
        self._fail_from = fail_from

    def __call__(self):
        self.calls += 1
        if self.calls >= self._fail_from:
            raise RuntimeError("clock provider broke")
        return REPAIR_AT


def test_a_clock_failure_at_finalize_degrades_to_a_pending_claim_not_failed(paths):
    """Clock calls: 1=pass start, 2=claim, 3=finalize, 4=pass finish. A
    clock broken from the finalize call on fails AFTER the commit -- the
    attempt stays APPLIED (audit_error attached, never mislabeled FAILED),
    the claim stays pending, the still-broken finish clock surfaces as a
    run error CARRYING that attempt, and recovery completes the pending
    claim under the original id."""
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-27", "run-27")
    clock = StepClock(fail_from=3)

    with pytest.raises(ReconciliationRepairRunError) as captured:
        repair_service(QueryOnlyBroker(broker), paths, clock=clock, prefix="live").repair(
            run_id="run-27"
        )

    attempt = only(captured.value.attempts)
    assert attempt.outcome is RepairOutcome.APPLIED  # never mislabeled
    assert attempt.audit_error is not None
    assert order_state(execution_db, "exec-27")[0] == "filled"
    assert [r.attempt_id for r in pending(reconciliation_db)] == ["live-1"]

    recovered = recovery_service(QueryOnlyBroker(broker), paths).recover_pending_attempts()
    entry = only(recovered)
    assert (entry.attempt_id, entry.outcome) == ("live-1", RepairOutcome.APPLIED)


def test_a_finished_clock_failure_after_all_attempts_keeps_the_committed_results(paths):
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-28", "run-28")
    clock = StepClock(fail_from=4)  # 1=start, 2=claim, 3=finalize, 4=finish

    with pytest.raises(ReconciliationRepairRunError) as captured:
        repair_service(QueryOnlyBroker(broker), paths, clock=clock).repair(run_id="run-28")

    attempt = only(captured.value.attempts)
    assert attempt.outcome is RepairOutcome.APPLIED
    assert order_state(execution_db, "exec-28")[0] == "filled"
    stored = only(finalized(reconciliation_db, "run-28"))
    assert stored.outcome is RepairOutcome.APPLIED  # audit and reality agree
    assert pending(reconciliation_db) == ()


def test_a_result_construction_failure_after_the_commit_never_yields_failed(paths):
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-29", "run-29")
    times = iter([REPAIR_AT, REPAIR_AT, REPAIR_AT, REPAIR_AT - timedelta(days=1)])
    clock = lambda: next(times)  # noqa: E731  (finish < start)

    with pytest.raises(ReconciliationRepairRunError, match="could not be built") as captured:
        repair_service(QueryOnlyBroker(broker), paths, clock=clock).repair(run_id="run-29")

    attempt = only(captured.value.attempts)
    assert attempt.outcome is RepairOutcome.APPLIED
    assert order_state(execution_db, "exec-29")[0] == "filled"
    assert only(finalized(reconciliation_db, "run-29")).outcome is RepairOutcome.APPLIED


def test_the_applied_attempt_never_depends_on_the_store_return_value(paths, monkeypatch):
    execution_db, _ = paths
    broker = seeded_candidate(paths, "exec-30", "run-30")
    real = execution_store.record_order_result

    def returns_garbage(*args, **kwargs):
        real(*args, **kwargs)
        return None  # a caller reading the returned row would crash

    monkeypatch.setattr(execution_store, "record_order_result", returns_garbage)

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-30")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.APPLIED
    assert attempt.broker_order_id == "FAKE-1"
    assert order_state(execution_db, "exec-30") == ("filled", "FAKE-1")


def test_a_mid_update_store_failure_rolls_back_and_reports_failed(paths, monkeypatch):
    """FAILED is used exactly when the ORDER state is PROVEN unchanged (the
    fence row registered before the update is bookkeeping, not order state)."""
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-31", "run-31")
    history_before = execution_store.get_state_history("exec-31", db_path=execution_db)

    real = execution_store.record_order_result

    def boom(*args, **kwargs):
        raise execution_store.ExecutionIntegrityError("simulated pre-commit failure")

    monkeypatch.setattr(execution_store, "record_order_result", boom)

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-31")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.FAILED
    assert attempt.failure_code is RepairFailureCode.STORE_UPDATE_FAILED
    assert "simulated" not in attempt.detail  # sanitized: no raw exception text
    monkeypatch.setattr(execution_store, "record_order_result", real)
    assert order_state(execution_db, "exec-31")[0] == "unknown"
    assert execution_store.get_state_history("exec-31", db_path=execution_db) == history_before
    assert only(finalized(reconciliation_db, "run-31")).outcome is RepairOutcome.FAILED
    assert pending(reconciliation_db) == ()


# --- live repair vs recovery (genuinely concurrent) ---------------------------


def run_live_and_recovery(paths, broker_for_live, gate_setup, *, recovery_broker=None):
    """Run the live worker in a thread, pause it at the gate, run recovery
    while it is paused, release, and return (live_result, recovered)."""
    execution_db, reconciliation_db = paths
    entered = threading.Event()
    release = threading.Event()
    gate_setup(entered, release)

    live_holder = {}

    def live():
        try:
            live_holder["result"] = repair_service(
                broker_for_live, paths, prefix="live", lease=LEASE_SECONDS
            ).repair(run_id="run-lr")
        except BaseException as exc:  # transferred to the main thread
            live_holder["error"] = exc

    thread = threading.Thread(target=live)
    thread.start()
    assert entered.wait(timeout=GATE_TIMEOUT), "live worker never reached the gate"
    # the lease is long, but recovery runs with a much later clock -> expired
    recovered = recovery_service(
        recovery_broker
        if recovery_broker is not None
        else QueryOnlyBroker(broker_for_live._inner), paths
    ).recover_pending_attempts()
    release.set()
    thread.join(timeout=GATE_TIMEOUT)
    assert not thread.is_alive()
    assert "error" not in live_holder, live_holder.get("error")
    return live_holder["result"], recovered


def assert_consistent_single_apply(paths, request_id, run_id, live_attempt, recovered):
    """The cross-DB consistency invariants every interleaving must satisfy."""
    execution_db, reconciliation_db = paths
    # execution DB: applied exactly once, with the shared attempt id
    state, _ = order_state(execution_db, request_id)
    assert state == "filled"
    history = execution_store.get_state_history(request_id, db_path=execution_db)
    repair_rows = [h for h in history if h["repair_attempt_id"] is not None]
    assert len(repair_rows) == 1
    assert repair_rows[0]["repair_attempt_id"] == "live-1"
    # audit DB: exactly one finalized attempt, same id, outcome APPLIED
    stored = only(finalized(reconciliation_db, run_id))
    assert stored.attempt_id == "live-1"
    assert stored.outcome is RepairOutcome.APPLIED
    assert pending(reconciliation_db) == ()
    # the live caller's view never contradicts reality
    assert live_attempt.attempt_id == "live-1"
    if live_attempt.outcome is RepairOutcome.APPLIED:
        # it really did commit (gen 1 provenance) or was recovered as applied
        assert repair_rows[0]["repair_generation"] == live_attempt.generation
    else:
        # it did not commit: recovery (generation 2) did
        assert live_attempt.outcome in (RepairOutcome.CONFLICT, RepairOutcome.ALREADY_APPLIED)
        assert repair_rows[0]["repair_generation"] == 2
        assert [a.attempt_id for a in recovered] == ["live-1"]
    # fence: the final generation matches the committed provenance
    fence = execution_store.get_repair_fence(request_id, db_path=execution_db)
    assert fence["repair_attempt_id"] == "live-1"
    assert fence["generation"] >= repair_rows[0]["repair_generation"]


def test_recovery_during_the_live_broker_query_takes_over_and_the_old_worker_is_fenced_out(
    paths,
):
    """Gate positions 1-2 (after reservation / inside the broker query): the
    recovery worker takes over (generation 2), re-evaluates and applies; the
    resumed old worker cannot commit gen-1 work."""
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-lr", "run-lr")
    gate = GateBroker(broker)

    def setup(entered, release):
        gate.entered = entered
        gate.release = release

    live_result, recovered = run_live_and_recovery(paths, gate, setup)

    live_attempt = only(live_result.attempts)
    # recovery applied under generation 2; the old worker observed the
    # already-committed outcome (or its lost claim) -- never a second apply
    entry = only(recovered)
    assert (entry.attempt_id, entry.outcome, entry.generation) == (
        "live-1",
        RepairOutcome.APPLIED,
        2,
    )
    assert live_attempt.outcome in (RepairOutcome.ALREADY_APPLIED, RepairOutcome.CONFLICT)
    assert live_attempt.audit_error is not None  # its finalize lost the claim
    assert_consistent_single_apply(paths, "exec-lr", "run-lr", live_attempt, recovered)
    history = execution_store.get_state_history("exec-lr", db_path=execution_db)
    assert only([h for h in history if h["repair_attempt_id"]])["repair_generation"] == 2


def test_recovery_between_evaluation_and_commit_fences_the_old_worker_out(paths, monkeypatch):
    """Gate positions 3-4 (after the broker query, before the execution
    transaction): the old worker's own fence registration is rejected."""
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-lr", "run-lr")
    entered_box = {}
    real_register = execution_store.register_repair_fence
    armed = {"live": True}

    def gated_register(*args, **kwargs):
        if armed.pop("live", None):  # only the live worker's first call blocks
            entered_box["entered"].set()
            assert entered_box["release"].wait(timeout=GATE_TIMEOUT)
        return real_register(*args, **kwargs)

    monkeypatch.setattr(execution_store, "register_repair_fence", gated_register)

    def setup(entered, release):
        entered_box["entered"] = entered
        entered_box["release"] = release

    live_result, recovered = run_live_and_recovery(paths, QueryOnlyBroker(broker), setup)

    live_attempt = only(live_result.attempts)
    entry = only(recovered)
    assert (entry.attempt_id, entry.outcome, entry.generation) == (
        "live-1",
        RepairOutcome.APPLIED,
        2,
    )
    assert live_attempt.outcome is RepairOutcome.CONFLICT
    assert live_attempt.failure_code is RepairFailureCode.CLAIM_LOST
    assert_consistent_single_apply(paths, "exec-lr", "run-lr", live_attempt, recovered)


def test_recovery_during_the_execution_transaction_confirms_the_commit(paths, monkeypatch):
    """Gate positions 5-6 (inside the execution transaction / right after
    the commit): recovery blocks on the DB lock or arrives post-commit, sees
    the structured provenance and finalizes APPLIED under the original id;
    the old worker's finalize loses the claim but its outcome stays APPLIED
    -- audit and execution DB agree (the original review's inconsistency)."""
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-lr", "run-lr")
    entered_box = {}
    import execution.broker_reconciliation_repair as repair_module

    real_finalize = repair_module.finalize_repair_attempt
    armed = {"live": True}

    def gated_finalize(*args, **kwargs):
        if armed.pop("live", None):  # pause the live worker AFTER its commit
            entered_box["entered"].set()
            assert entered_box["release"].wait(timeout=GATE_TIMEOUT)
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(repair_module, "finalize_repair_attempt", gated_finalize)

    def setup(entered, release):
        entered_box["entered"] = entered
        entered_box["release"] = release

    live_result, recovered = run_live_and_recovery(paths, QueryOnlyBroker(broker), setup)

    live_attempt = only(live_result.attempts)
    # THE original blocker scenario: the commit is gen-1 work; recovery must
    # confirm it as APPLIED (original id), never finalize FAILED over it.
    entry = only(recovered)
    assert (entry.attempt_id, entry.outcome) == ("live-1", RepairOutcome.APPLIED)
    assert entry.internal_state_after is OrderState.FILLED
    assert live_attempt.outcome is RepairOutcome.APPLIED  # caller view matches reality
    assert live_attempt.audit_error is not None  # its own finalize lost the claim
    assert_consistent_single_apply(paths, "exec-lr", "run-lr", live_attempt, recovered)
    history = execution_store.get_state_history("exec-lr", db_path=execution_db)
    assert only([h for h in history if h["repair_attempt_id"]])["repair_generation"] == 1


def test_recovery_after_finalize_is_a_no_op(paths):
    broker = seeded_candidate(paths, "exec-lr2", "run-lr2")
    result = repair_service(QueryOnlyBroker(broker), paths, prefix="live").repair(run_id="run-lr2")
    assert only(result.attempts).outcome is RepairOutcome.APPLIED

    recovered = recovery_service(QueryOnlyBroker(broker), paths).recover_pending_attempts()

    assert recovered == ()


def test_recovery_never_touches_a_pending_attempt_with_a_valid_lease(paths, monkeypatch):
    """Lease protection: same pending state, but the recovery clock is
    still inside the lease window -> untouched everywhere."""
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-lv", "run-lv")
    import execution.broker_reconciliation_repair as repair_module

    monkeypatch.setattr(
        repair_module,
        "finalize_repair_attempt",
        lambda *a, **k: (_ for _ in ()).throw(
            reconciliation_store.ReconciliationStoreError("audit down")
        ),
    )
    repair_service(QueryOnlyBroker(broker), paths, prefix="live").repair(run_id="run-lv")
    monkeypatch.undo()
    pending_before = pending(reconciliation_db)
    assert [r.attempt_id for r in pending_before] == ["live-1"]
    execution_bytes = execution_db.read_bytes()
    recovery_broker = QueryOnlyBroker(broker)

    recovered = recovery_service(
        recovery_broker, paths, clock=lambda: DURING_LEASE_AT
    ).recover_pending_attempts()

    assert recovered == ()
    assert pending(reconciliation_db) == pending_before  # claim untouched
    assert recovery_broker.query_count == 0  # no broker query either
    assert execution_db.read_bytes() == execution_bytes  # no fence bump, no write


def test_a_crash_before_the_state_update_is_resumed_by_recovery_not_failed(paths, monkeypatch):
    """The reservation exists but nothing committed: recovery -- as the new
    generation-2 owner -- re-runs the full evaluation (fresh broker query
    included) and APPLIES; it never invents a FAILED verdict."""
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-32", "run-32")

    def crash(*args, **kwargs):
        raise KeyboardInterrupt("process killed")  # crash-equivalent

    monkeypatch.setattr(execution_store, "record_order_result", crash)
    with pytest.raises(KeyboardInterrupt):
        repair_service(QueryOnlyBroker(broker), paths, prefix="crash").repair(run_id="run-32")
    monkeypatch.undo()

    assert order_state(execution_db, "exec-32")[0] == "unknown"
    assert [r.attempt_id for r in pending(reconciliation_db)] == ["crash-1"]
    recovery_broker = QueryOnlyBroker(broker)

    recovered = recovery_service(recovery_broker, paths).recover_pending_attempts()

    entry = only(recovered)
    assert (entry.attempt_id, entry.outcome, entry.generation) == (
        "crash-1",
        RepairOutcome.APPLIED,
        2,
    )
    assert recovery_broker.query_count == 1  # the fresh re-query really happened
    assert order_state(execution_db, "exec-32")[0] == "filled"
    history = execution_store.get_state_history("exec-32", db_path=execution_db)
    assert only([h for h in history if h["repair_attempt_id"]])["repair_generation"] == 2
    assert pending(reconciliation_db) == ()


def test_a_finalize_failure_after_the_commit_is_recovered_under_the_same_id(paths, monkeypatch):
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-33", "run-33")
    import execution.broker_reconciliation_repair as repair_module

    monkeypatch.setattr(
        repair_module,
        "finalize_repair_attempt",
        lambda *a, **k: (_ for _ in ()).throw(
            reconciliation_store.ReconciliationStoreError("audit DB unavailable")
        ),
    )
    first = repair_service(QueryOnlyBroker(broker), paths, prefix="crash").repair(run_id="run-33")
    monkeypatch.undo()

    attempt = only(first.attempts)
    assert attempt.outcome is RepairOutcome.APPLIED  # never FAILED: the commit is real
    assert attempt.audit_error is not None
    assert first.has_audit_failures is True
    assert order_state(execution_db, "exec-33")[0] == "filled"
    assert [r.attempt_id for r in pending(reconciliation_db)] == ["crash-1"]
    history_before = execution_store.get_state_history("exec-33", db_path=execution_db)

    recovered = recovery_service(QueryOnlyBroker(broker), paths).recover_pending_attempts()

    entry = only(recovered)
    assert entry.attempt_id == "crash-1"  # the ORIGINAL id, not a new one
    assert entry.outcome is RepairOutcome.APPLIED
    assert entry.generation == 2  # finalized by the takeover claim
    assert pending(reconciliation_db) == ()
    stored = only(finalized(reconciliation_db, "run-33"))
    assert (stored.attempt_id, stored.outcome) == ("crash-1", RepairOutcome.APPLIED)
    # recovery reads only: history did not grow
    assert execution_store.get_state_history("exec-33", db_path=execution_db) == history_before
    # a later pass converges without new history
    rerun = repair_service(QueryOnlyBroker(broker), paths, prefix="later").repair(run_id="run-33")
    assert only(rerun.attempts).outcome is RepairOutcome.ALREADY_APPLIED
    assert execution_store.get_state_history("exec-33", db_path=execution_db) == history_before


def test_recovery_refuses_a_tampered_or_deleted_source_observation(paths, monkeypatch):
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-34", "run-34")
    import execution.broker_reconciliation_repair as repair_module

    monkeypatch.setattr(
        repair_module,
        "finalize_repair_attempt",
        lambda *a, **k: (_ for _ in ()).throw(
            reconciliation_store.ReconciliationStoreError("audit down")
        ),
    )
    repair_service(QueryOnlyBroker(broker), paths, prefix="crash").repair(run_id="run-34")
    monkeypatch.undo()

    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(reconciliation_db)) as connection:  # FKs off by default
        connection.execute(
            f"DELETE FROM {reconciliation_store.DISCREPANCIES_TABLE} WHERE target_id = 'exec-34'"
        )
        connection.commit()

    with pytest.raises(ReconciliationRepairError, match="tampered"):
        recovery_service(QueryOnlyBroker(broker), paths).recover_pending_attempts()

    assert len(pending(reconciliation_db)) == 1  # still pending, nothing invented


# --- recovery worker races ----------------------------------------------------


def crashed_pending(paths, request_id, run_id, monkeypatch):
    """A pending claim whose worker died before the state update."""
    broker = seeded_candidate(paths, request_id, run_id)
    import execution.broker_reconciliation_repair as repair_module  # noqa: F401

    def crash(*args, **kwargs):
        raise KeyboardInterrupt("process killed")

    monkeypatch.setattr(execution_store, "record_order_result", crash)
    with pytest.raises(KeyboardInterrupt):
        repair_service(QueryOnlyBroker(broker), paths, prefix="crash").repair(run_id=run_id)
    monkeypatch.undo()
    return broker


@pytest.mark.parametrize("workers", [2, 10])
def test_concurrent_recovery_workers_elect_exactly_one_winner(paths, monkeypatch, workers):
    """N recovery workers, one expired pending claim, genuinely overlapped
    via a barrier: exactly one takes over (generation exactly +1), the
    losers change neither fence nor claim nor broker state."""
    execution_db, reconciliation_db = paths
    broker = crashed_pending(paths, "exec-35", "run-35", monkeypatch)
    barrier = threading.Barrier(workers, timeout=15)
    brokers = [QueryOnlyBroker(broker) for _ in range(workers)]

    def recover(index):
        aligned = {"first": True}

        def aligned_clock():
            if aligned.pop("first", None):
                barrier.wait()  # all workers overlap at the expiry check
            return RECOVERY_AT

        service = repair_service(
            brokers[index],
            paths,
            prefix=f"rec{index}",
            owner=f"recovery-{index}",
            clock=aligned_clock,
        )
        return service.recover_pending_attempts()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = [f.result() for f in [pool.submit(recover, i) for i in range(workers)]]

    non_empty = [r for r in results if r]
    assert len(non_empty) == 1  # exactly one winner
    entry = only(non_empty[0])
    assert (entry.attempt_id, entry.outcome, entry.generation) == (
        "crash-1",
        RepairOutcome.APPLIED,
        2,  # incremented exactly once, not once per worker
    )
    fence = execution_store.get_repair_fence("exec-35", db_path=execution_db)
    assert (fence["repair_attempt_id"], fence["generation"]) == ("crash-1", 2)
    assert sum(b.query_count for b in brokers) == 1  # only the winner asked the broker
    assert order_state(execution_db, "exec-35")[0] == "filled"
    history = execution_store.get_state_history("exec-35", db_path=execution_db)
    assert len([h for h in history if h["repair_attempt_id"]]) == 1
    assert pending(reconciliation_db) == ()
    # restart: a later recovery pass is a no-op
    assert recovery_service(QueryOnlyBroker(broker), paths).recover_pending_attempts() == ()


# --- pass-level failures ------------------------------------------------------


def two_candidates(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    for request_id in ("exec-a", "exec-b"):
        seed_internal_unknown(execution_db, request_id)
        broker.submit_order(broker_request(request_id))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-two")
    return broker

def test_a_run_level_failure_on_the_second_target_keeps_the_first_commit(paths, monkeypatch):
    execution_db, reconciliation_db = paths
    broker = two_candidates(paths)
    real = execution_store.get_execution_order

    def breaks_on_second(request_id, **kwargs):
        if request_id == "exec-b":
            raise KeyError("programmer bug")  # NOT an operational error
        return real(request_id, **kwargs)

    monkeypatch.setattr(execution_store, "get_execution_order", breaks_on_second)

    with pytest.raises(ReconciliationRepairRunError, match="KeyError") as captured:
        repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-two")

    partial = captured.value.attempts
    assert [a.outcome for a in partial] == [RepairOutcome.APPLIED]
    monkeypatch.undo()
    assert order_state(execution_db, "exec-a")[0] == "filled"
    assert order_state(execution_db, "exec-b")[0] == "unknown"
    assert only(finalized(reconciliation_db, "run-two")).outcome is RepairOutcome.APPLIED
    assert [r.target_id for r in pending(reconciliation_db)] == ["exec-b"]


class BrokenLogger:
    """A logger whose every emission path raises (handler and formatter
    failures are equivalent from the caller's perspective)."""

    def log(self, *args, **kwargs):
        raise RuntimeError("handler down")

    def info(self, *args, **kwargs):
        raise RuntimeError("handler down")

    def warning(self, *args, **kwargs):
        raise RuntimeError("formatter down")

    def error(self, *args, **kwargs):
        raise RuntimeError("handler down")


def test_a_logging_failure_never_changes_a_repair_outcome(paths, monkeypatch):
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-36", "run-36")
    import execution.broker_reconciliation_repair as repair_module

    monkeypatch.setattr(repair_module, "_LOGGER", BrokenLogger())

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-36")

    assert only(result.attempts).outcome is RepairOutcome.APPLIED
    assert order_state(execution_db, "exec-36")[0] == "filled"
    assert only(finalized(reconciliation_db, "run-36")).outcome is RepairOutcome.APPLIED


def test_duplicate_attempt_ids_from_the_factory_are_refused_before_any_side_effect(paths):
    execution_db, reconciliation_db = paths
    broker = two_candidates(paths)
    before = order_snapshot(execution_db, ("exec-a", "exec-b"))
    service = BrokerOrderReconciliationRepairService(
        broker=QueryOnlyBroker(broker),
        execution_db_path=execution_db,
        reconciliation_db_path=reconciliation_db,
        clock=lambda: REPAIR_AT,
        attempt_id_factory=lambda: "same-id-every-time",
    )

    with pytest.raises(ReconciliationRepairError, match="duplicate"):
        service.repair(run_id="run-two")

    assert order_snapshot(execution_db, ("exec-a", "exec-b")) == before
    assert pending(reconciliation_db) == ()
    assert finalized(reconciliation_db, "run-two") == ()


def test_an_attempt_id_factory_exception_stops_the_pass_before_any_side_effect(paths):
    execution_db, reconciliation_db = paths
    broker = two_candidates(paths)
    before = order_snapshot(execution_db, ("exec-a", "exec-b"))
    calls = itertools.count(1)

    def flaky_factory():
        if next(calls) >= 2:
            raise RuntimeError("factory broke")
        return "attempt-1"

    service = BrokerOrderReconciliationRepairService(
        broker=QueryOnlyBroker(broker),
        execution_db_path=execution_db,
        reconciliation_db_path=reconciliation_db,
        clock=lambda: REPAIR_AT,
        attempt_id_factory=flaky_factory,
    )

    with pytest.raises(RuntimeError, match="factory broke"):
        service.repair(run_id="run-two")

    assert order_snapshot(execution_db, ("exec-a", "exec-b")) == before
    assert pending(reconciliation_db) == ()


def test_a_cross_pass_attempt_id_collision_is_refused_at_reservation_time(paths):
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-37", "run-37")
    repair_service(QueryOnlyBroker(broker), paths, prefix="dup").repair(run_id="run-37")
    state_after_first = order_snapshot(execution_db, ("exec-37",))

    with pytest.raises(ReconciliationRepairRunError) as captured:
        repair_service(QueryOnlyBroker(broker), paths, prefix="dup").repair(run_id="run-37")

    assert isinstance(
        captured.value.__cause__, reconciliation_store.RepairAttemptConflictError
    )
    assert captured.value.attempts == ()
    assert order_snapshot(execution_db, ("exec-37",)) == state_after_first


# --- logging sanitization (secrets never reach logs or audit) -----------------

SECRETS = [
    "OANDA_TOKEN_SUPER_SECRET",
    "oanda_token_super_secret",  # case variant
    "OANDA_TOKEN_SUPER%5FSECRET",  # url-encoded variant
    "api-key-1234567890",
    "hunter2-password",
    "Authorization: Bearer abc.def.ghi",
    "account-001-234-5678-90",
    "https://api.example.com/v3/orders?token=leaky",
    '{"errorMessage":"raw http body"}',
    "SELECT * FROM execution_orders",
    "C:\\Users\\trader\\secrets.db",
    "/home/trader/.env",
]


def test_leaked_adapter_exception_text_never_reaches_logs_or_audit(paths, caplog):
    """A non-BrokerError leak whose message, cause chain and context carry
    secrets: only the class name and stable code may be emitted."""
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-38", "run-38")
    inner = ValueError(" / ".join(SECRETS))  # cause chain carries them too
    leak = RuntimeError("adapter failed: " + " | ".join(SECRETS + SECRETS))
    leak.__cause__ = inner

    with caplog.at_level("DEBUG"):
        result = repair_service(
            QueryOnlyBroker(broker, query_failures={client_id("exec-38"): leak}), paths
        ).repair(run_id="run-38")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.QUERY_FAILED
    assert attempt.failure_code is RepairFailureCode.BROKER_CONTRACT_VIOLATION
    emitted = "\n".join(
        record.getMessage() + repr(record.args) + repr(record.exc_info)
        for record in caplog.records
    )
    stored = only(finalized(reconciliation_db, "run-38"))
    for secret in SECRETS:
        for haystack, where in (
            (emitted, "log output"),
            (attempt.detail, "attempt detail"),
            (stored.detail, "audit detail"),
        ):
            assert secret not in haystack, f"secret {secret!r} leaked into {where}"
        assert secret.lower() not in emitted.lower()
    assert "RuntimeError" in emitted  # the class name IS logged


# --- inputs, configuration, migration, structure ------------------------------


def test_repair_acts_on_recorded_runs_only(paths):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-39")
    broker.submit_order(broker_request("exec-39"))
    BrokerOrderReconciliationService(
        broker=QueryOnlyBroker(broker),
        execution_db_path=execution_db,
        clock=lambda: OBSERVED_AT,
    ).detect(run_id="run-39")

    with pytest.raises(ReconciliationRepairError, match="does not exist"):
        repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-39")
    assert not reconciliation_db.exists()

    with pytest.raises(ReconciliationRepairError, match="not recorded"):
        repair_service(QueryOnlyBroker(broker), paths).repair(
            run_id="run-39", mode=RepairMode.DRY_RUN
        )
    assert not reconciliation_db.exists()

    record_run(QueryOnlyBroker(broker), paths, run_id="run-recorded")
    with pytest.raises(ReconciliationRepairError, match="not recorded"):
        repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-ghost")


def test_the_broker_timeout_must_fit_under_the_lease(paths):
    """The minimal lease contract: no heartbeat, so broker_timeout <
    lease_duration is validated at construction (explicit argument and a
    broker exposing timeout_seconds alike)."""
    execution_db, reconciliation_db = paths
    with pytest.raises(ValueError, match="lease_duration_seconds"):
        BrokerOrderReconciliationRepairService(
            broker=FakeBroker(),
            execution_db_path=execution_db,
            reconciliation_db_path=reconciliation_db,
            lease_duration_seconds=30,
            broker_timeout_seconds=30,
        )

    class TimeoutExposingBroker(QueryOnlyBroker):
        timeout_seconds = 120.0

    with pytest.raises(ValueError, match="broker.timeout_seconds"):
        BrokerOrderReconciliationRepairService(
            broker=TimeoutExposingBroker(FakeBroker()),
            execution_db_path=execution_db,
            reconciliation_db_path=reconciliation_db,
            lease_duration_seconds=60,
        )
    # a compliant configuration constructs
    BrokerOrderReconciliationRepairService(
        broker=TimeoutExposingBroker(FakeBroker()),
        execution_db_path=execution_db,
        reconciliation_db_path=reconciliation_db,
        lease_duration_seconds=600,
        broker_timeout_seconds=30,
    )


def _downgrade_reconciliation_db_to_v1(path):
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"DROP INDEX {reconciliation_store._REPAIRS_RUN_INDEX}")
        connection.execute(f"DROP TABLE {reconciliation_store.REPAIRS_TABLE}")
        connection.execute(
            f"ALTER TABLE {reconciliation_store.DISCREPANCIES_TABLE} "
            "DROP COLUMN internal_revision"
        )
        connection.execute(
            f"PRAGMA user_version = {reconciliation_store._MIGRATABLE_VERSION}"
        )
        connection.commit()


def test_apply_mode_migrates_a_v1_audit_db_and_dry_run_refuses_it(paths):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-40")
    broker.submit_order(broker_request("exec-40"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-40")
    _downgrade_reconciliation_db_to_v1(reconciliation_db)
    v1_bytes = reconciliation_db.read_bytes()

    with pytest.raises(
        reconciliation_store.ReconciliationSchemaError, match="init_reconciliation_db"
    ):
        repair_service(QueryOnlyBroker(broker), paths).repair(
            run_id="run-40", mode=RepairMode.DRY_RUN
        )
    assert reconciliation_db.read_bytes() == v1_bytes

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-40")
    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.STALE
    assert attempt.failure_code is RepairFailureCode.MISSING_OBSERVED_REVISION
    record_run(QueryOnlyBroker(broker), paths, run_id="run-40-fresh")
    fresh = repair_service(QueryOnlyBroker(broker), paths, prefix="fresh").repair(
        run_id="run-40-fresh"
    )
    assert only(fresh.attempts).outcome is RepairOutcome.APPLIED
    assert order_state(execution_db, "exec-40")[0] == "filled"


def test_repair_never_submits_or_cancels_through_the_broker(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-41")
    broker.submit_order(broker_request("exec-41"))
    record_run(broker, paths, run_id="run-41")
    submits, cancels = len(broker.submitted_requests), len(broker.cancel_requests)

    result = repair_service(broker, paths).repair(run_id="run-41")
    recovery_service(broker, paths).recover_pending_attempts()

    assert only(result.attempts).outcome is RepairOutcome.APPLIED
    assert len(broker.submitted_requests) == submits
    assert len(broker.cancel_requests) == cancels


def test_the_service_refuses_bad_construction_and_inputs(paths):
    execution_db, reconciliation_db = paths
    with pytest.raises(ValueError, match="Broker protocol"):
        BrokerOrderReconciliationRepairService(
            broker=object(),
            execution_db_path=execution_db,
            reconciliation_db_path=reconciliation_db,
        )
    with pytest.raises(ValueError, match="clock"):
        BrokerOrderReconciliationRepairService(
            broker=FakeBroker(),
            execution_db_path=execution_db,
            reconciliation_db_path=reconciliation_db,
            clock="not-callable",
        )
    with pytest.raises(ValueError, match="attempt_id_factory"):
        BrokerOrderReconciliationRepairService(
            broker=FakeBroker(),
            execution_db_path=execution_db,
            reconciliation_db_path=reconciliation_db,
            attempt_id_factory="not-callable",
        )
    with pytest.raises(ValueError, match="lease_duration_seconds"):
        BrokerOrderReconciliationRepairService(
            broker=FakeBroker(),
            execution_db_path=execution_db,
            reconciliation_db_path=reconciliation_db,
            lease_duration_seconds=0,
        )
    with pytest.raises(ValueError, match="owner_id"):
        BrokerOrderReconciliationRepairService(
            broker=FakeBroker(),
            execution_db_path=execution_db,
            reconciliation_db_path=reconciliation_db,
            owner_id="  ",
        )
    service = BrokerOrderReconciliationRepairService(
        broker=FakeBroker(),
        execution_db_path=execution_db,
        reconciliation_db_path=reconciliation_db,
    )
    with pytest.raises(ValueError, match="run_id"):
        service.repair(run_id="  ")
    with pytest.raises(ReconciliationRepairError, match="RepairMode"):
        service.repair(run_id="run-x", mode="apply")


def test_repair_attempts_are_logged_with_ids_states_and_outcomes(paths, caplog):
    execution_db, _ = paths
    broker = seeded_candidate(paths, "exec-42", "run-42")

    with caplog.at_level("INFO", logger="execution.broker_reconciliation_repair"):
        repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-42")

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "exec-42" in messages
    assert "outcome=applied" in messages
    assert "generation=1" in messages
    assert "applied=1" in messages


# --- structural guarantees ----------------------------------------------------


def _called_attributes(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_the_repair_module_never_calls_a_broker_write_operation():
    called = _called_attributes(REPO_ROOT / "execution" / "broker_reconciliation_repair.py")
    assert "submit_order" not in called
    assert "cancel_order" not in called


def test_the_repair_module_writes_through_the_state_machine_api_only():
    """The one sanctioned order write is ``record_order_result`` (plus the
    fencing registration); every other execution-store write API and raw
    SQL is forbidden."""
    module_path = REPO_ROOT / "execution" / "broker_reconciliation_repair.py"
    called = _called_attributes(module_path)
    forbidden = {
        "create_execution_order",
        "mark_submitting",
        "record_order_state",
        "init_db",
        "execute",
        "executemany",
        "connect",
    }
    assert called & forbidden == set()
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    record_calls = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "record_order_result"
    ]
    assert len(record_calls) == 1, (
        "broker_reconciliation_repair.py must hold exactly one record_order_result "
        f"call (the guarded one), found lines {record_calls}"
    )
