"""Issue #104: broker reconciliation -- safe order-state REPAIR.

Everything runs against ``tmp_path`` SQLite files and an in-memory
FakeBroker: no network, no Discord, no S3, no sleep, no real orders.

The repair service is driven through :class:`QueryOnlyBroker`, a facade
whose ``submit_order`` / ``cancel_order`` are POISONED -- so every test in
this file simultaneously pins that repair never writes through the broker.
The broker-side fixtures themselves are seeded on the FakeBroker BEFORE
detection/repair start (that is the state being reconciled against), which
is why this file is listed in the submission-boundary allow-list.
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
from execution.reconciliation_repair_models import RepairMode, RepairOutcome

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Internal records predate FakeBroker's clock base (2026-01-01); detection
# observes at OBSERVED_AT and repair runs later at REPAIR_AT.
INTERNAL_T = datetime(2025, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
OBSERVED_AT = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
REPAIR_AT = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
FILL_PRICE = Decimal("2400")
QUANTITY = Decimal("10")


def client_id(request_id: str) -> str:
    return f"client-{request_id}"


class QueryOnlyBroker:
    """Query-only Broker facade over a FakeBroker (test double, in-memory).

    ``submit_order`` / ``cancel_order`` raise: repair must never reach a
    broker WRITE operation, and a poisoned facade proves it for every test
    rather than only for a dedicated one. ``get_order`` can be scripted to
    fail with a typed ``BrokerError`` (FakeBroker has no query-failure
    scripting, same stance as Issue #103).
    """

    def __init__(self, inner: FakeBroker, *, query_failures: dict | None = None):
        self._inner = inner
        self._query_failures = dict(query_failures or {})

    def submit_order(self, request):  # pragma: no cover - must never run
        raise AssertionError("repair must never submit an order")

    def cancel_order(self, client_order_id):  # pragma: no cover - must never run
        raise AssertionError("repair must never cancel an order")

    def get_order(self, client_order_id):
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
        return self._snapshots.get(client_order_id, self._inner.get_order(client_order_id))


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


def repair_service(broker, paths, *, prefix="attempt") -> BrokerOrderReconciliationRepairService:
    execution_db, reconciliation_db = paths
    counter = itertools.count(1)
    return BrokerOrderReconciliationRepairService(
        broker=broker,
        execution_db_path=execution_db,
        reconciliation_db_path=reconciliation_db,
        clock=lambda: REPAIR_AT,
        attempt_id_factory=lambda: f"{prefix}-{next(counter)}",
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


def only(entries):
    assert len(entries) == 1, entries
    return entries[0]


# --- safe repairs (issue scenarios 1-6) ---------------------------------------


def test_unknown_to_filled_is_applied_through_the_state_machine(paths):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-1")
    broker.submit_order(broker_request("exec-1"))  # broker-side fixture: FILLED
    run = record_run(QueryOnlyBroker(broker), paths, run_id="run-1")
    assert only(run.discrepancies).resolution_category is ResolutionCategory.AUTO_REPAIR_CANDIDATE

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
    assert attempt.attempted_at_utc == REPAIR_AT
    assert attempt.applied_at_utc == REPAIR_AT
    assert attempt.broker_order_id == "FAKE-1"
    assert order_state(execution_db, "exec-1") == ("filled", "FAKE-1")
    # the state history row carries the run/attempt provenance (audit anchor)
    history = execution_store.get_state_history("exec-1", db_path=execution_db)
    assert "run_id=run-1" in history[-1]["reason"]
    assert f"attempt_id={attempt.attempt_id}" in history[-1]["reason"]
    # the audit row is persisted and linked to the run
    stored = only(reconciliation_store.list_repair_attempts(run_id="run-1", db_path=reconciliation_db))
    assert stored == attempt


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


# --- ineligible / unsafe inputs (scenarios 7-13) ------------------------------


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
            )
        ],
    )
    before = order_snapshot(execution_db, ("exec-7",))

    result = repair_service(
        SnapshotBroker(broker, {client_id("exec-7"): cancelled}), paths
    ).repair(run_id="run-7")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.REJECTED
    assert "state machine" in attempt.detail
    assert order_snapshot(execution_db, ("exec-7",)) == before


def test_a_broken_client_order_id_binding_is_rejected(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.submit_order(broker_request("exec-8"))
    seed_internal_unknown(execution_db, "exec-8")
    fabricate_recorded_run(
        paths,
        "run-8",
        [
            OrderDiscrepancy(
                discrepancy_type=DiscrepancyType.UNKNOWN_BROKER_SETTLED,
                target_type=ReconciliationTargetType.EXECUTION_ORDER,
                target_id="exec-8",
                resolution_category=ResolutionCategory.AUTO_REPAIR_CANDIDATE,
                observed_at_utc=OBSERVED_AT,
                detail="tampered: observed under a foreign client_order_id",
                internal_state=OrderState.UNKNOWN,
                broker_state=OrderState.FILLED,
                client_order_id="client-somebody-else",
                broker_order_id="FAKE-1",
            )
        ],
    )
    before = order_snapshot(execution_db, ("exec-8",))

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-8")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.REJECTED
    assert "client_order_id" in attempt.detail
    assert order_snapshot(execution_db, ("exec-8",)) == before


def test_manual_review_targets_are_never_repaired(paths):
    """Scenario 8: internal ACCEPTED vs broker REJECTED without a broker id
    (a known Issue #103 manual-review case) plus a broker-only order."""
    execution_db, _ = paths
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
    assert order_snapshot(execution_db, ("exec-9",)) == before


def test_query_failed_reconciliation_targets_are_skipped(paths):
    execution_db, _ = paths
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
    assert "nothing was observed" in attempt.detail
    assert order_snapshot(execution_db, ("exec-10",)) == before


def test_a_broker_that_now_reports_unknown_changes_nothing(paths):
    """Scenario 10: the candidate observed FILLED, but the repair-time
    re-query reports UNKNOWN -- the stored observation is not confirmable."""
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
    assert "'unknown'" in attempt.detail
    assert order_snapshot(execution_db, ("exec-11",)) == before


@pytest.mark.parametrize(
    "error, expected_code",
    [
        (BrokerTimeoutError("timed out"), "timeout"),
        (BrokerCommunicationError("connection refused"), "communication_failure"),
        (RuntimeError("adapter bug"), "communication_failure"),
    ],
)
def test_a_repair_time_query_failure_changes_nothing(paths, error, expected_code):
    execution_db, _ = paths
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
    assert order_snapshot(execution_db, ("exec-12",)) == before


def test_an_order_the_broker_no_longer_knows_is_a_query_failure(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-13")
    broker.submit_order(broker_request("exec-13"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-13")
    before = order_snapshot(execution_db, ("exec-13",))

    # a broker that lost the order between detection and repair
    result = repair_service(
        QueryOnlyBroker(FakeBroker(default_fill_price=FILL_PRICE)), paths
    ).repair(run_id="run-13")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.QUERY_FAILED
    assert attempt.broker_error_code.value == "not_found"
    assert order_snapshot(execution_db, ("exec-13",)) == before


def test_a_changed_broker_reference_id_is_a_conflict(paths):
    """Scenario 12a: the fresh snapshot carries a different broker_order_id
    than the one already known -- never overwritten, never lost."""
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
    assert "B-ORIG" in attempt.detail and "B-FOREIGN" in attempt.detail
    assert order_snapshot(execution_db, ("exec-14",)) == before


def test_a_broker_id_now_owned_by_another_request_is_a_conflict(paths):
    """Scenario 12b: the id was unassigned at detection but another request
    recorded it before the repair ran -- reflecting it would collide with
    the DB-wide uniqueness."""
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
    # after detection, another request takes ownership of B-1 internally
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
    assert "exec-owner" in by_target["exec-clash"].detail
    # exec-owner itself was a broker_order_missing manual-review target
    assert by_target["exec-owner"].outcome is RepairOutcome.SKIPPED
    assert order_snapshot(execution_db, ("exec-clash", "exec-owner")) == before


def test_a_stale_reconciliation_result_never_rolls_back_newer_state(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-16")
    broker.submit_order(broker_request("exec-16"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-16")
    # another process settles the order differently after the observation
    execution_store.record_order_result(
        "exec-16",
        OrderResult(
            client_order_id=client_id("exec-16"),
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
            updated_at_utc=OBSERVED_AT + timedelta(seconds=1),
        ),
        reason="cancelled by another process",
        db_path=execution_db,
    )
    before = order_snapshot(execution_db, ("exec-16",))

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-16")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.STALE
    assert "'cancelled'" in attempt.detail
    assert order_snapshot(execution_db, ("exec-16",)) == before


def test_a_target_already_at_the_observed_outcome_is_already_applied(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-17")
    filled = broker.submit_order(broker_request("exec-17"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-17")
    # the normal path records the same outcome before the repair runs
    execution_store.record_order_result("exec-17", filled, reason="filled", db_path=execution_db)
    before = order_snapshot(execution_db, ("exec-17",))

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-17")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.ALREADY_APPLIED
    assert attempt.internal_state_after is OrderState.FILLED
    assert order_snapshot(execution_db, ("exec-17",)) == before  # no new history row


# --- dry-run (scenario 14) ----------------------------------------------------


def test_dry_run_evaluates_but_writes_nothing(paths, tmp_path):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-18")
    broker.submit_order(broker_request("exec-18"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-18")
    execution_bytes = execution_db.read_bytes()
    reconciliation_bytes = reconciliation_db.read_bytes()
    files_before = sorted(p.name for p in tmp_path.iterdir())

    result = repair_service(QueryOnlyBroker(broker), paths).repair(
        run_id="run-18", mode=RepairMode.DRY_RUN
    )

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.WOULD_APPLY
    assert attempt.internal_state_after is OrderState.FILLED
    assert attempt.applied_at_utc is None
    assert execution_db.read_bytes() == execution_bytes
    assert reconciliation_db.read_bytes() == reconciliation_bytes
    assert sorted(p.name for p in tmp_path.iterdir()) == files_before
    assert reconciliation_store.list_repair_attempts(db_path=reconciliation_db) == ()
    assert order_state(execution_db, "exec-18")[0] == "unknown"


# --- idempotency and concurrency (scenarios 15-16) ----------------------------


def test_repeating_the_same_repair_is_idempotent(paths):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-19")
    broker.submit_order(broker_request("exec-19"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-19")

    first = repair_service(QueryOnlyBroker(broker), paths, prefix="first").repair(run_id="run-19")
    after_first = order_snapshot(execution_db, ("exec-19",))
    second = repair_service(QueryOnlyBroker(broker), paths, prefix="second").repair(run_id="run-19")
    third = repair_service(QueryOnlyBroker(broker), paths, prefix="third").repair(run_id="run-19")

    assert only(first.attempts).outcome is RepairOutcome.APPLIED
    assert only(second.attempts).outcome is RepairOutcome.ALREADY_APPLIED
    assert only(third.attempts).outcome is RepairOutcome.ALREADY_APPLIED
    # the final state holds and the history did NOT grow after the first pass
    assert order_snapshot(execution_db, ("exec-19",)) == after_first
    assert order_state(execution_db, "exec-19")[0] == "filled"
    # every pass appended exactly its own audit attempt (append-only audit)
    attempts = reconciliation_store.list_repair_attempts(run_id="run-19", db_path=reconciliation_db)
    assert [a.outcome for a in attempts] == [
        RepairOutcome.APPLIED,
        RepairOutcome.ALREADY_APPLIED,
        RepairOutcome.ALREADY_APPLIED,
    ]


def test_concurrent_repairs_never_duplicate_history_or_state(paths):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-20")
    broker.submit_order(broker_request("exec-20"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-20")
    history_before = len(execution_store.get_state_history("exec-20", db_path=execution_db))

    barrier = threading.Barrier(2, timeout=10)

    def worker(prefix):
        counter = itertools.count(1)

        def aligned_attempt_id():
            barrier.wait()  # both workers evaluate the same starting state
            return f"{prefix}-{next(counter)}"

        service = BrokerOrderReconciliationRepairService(
            broker=QueryOnlyBroker(broker),
            execution_db_path=execution_db,
            reconciliation_db_path=reconciliation_db,
            clock=lambda: REPAIR_AT,
            attempt_id_factory=aligned_attempt_id,
        )
        return service.repair(run_id="run-20")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(worker, "w1"), pool.submit(worker, "w2")]]

    outcomes = sorted(only(result.attempts).outcome.value for result in results)
    assert outcomes == ["already_applied", "applied"]
    assert order_state(execution_db, "exec-20")[0] == "filled"
    history = execution_store.get_state_history("exec-20", db_path=execution_db)
    assert len(history) == history_before + 1  # exactly one repair history row
    audit = reconciliation_store.list_repair_attempts(run_id="run-20", db_path=reconciliation_db)
    assert sorted(a.outcome.value for a in audit) == ["already_applied", "applied"]


# --- failure atomicity (scenarios 17-18) --------------------------------------


def test_a_mid_update_store_failure_rolls_back_and_reports_failed(paths, monkeypatch):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-21")
    broker.submit_order(broker_request("exec-21"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-21")
    before_bytes = execution_db.read_bytes()

    def boom(*args, **kwargs):
        raise execution_store.ExecutionIntegrityError("simulated pre-commit failure")

    monkeypatch.setattr(execution_store, "_validate_writeback", boom)

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-21")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.FAILED
    assert "rolled back" in attempt.detail
    monkeypatch.undo()
    assert execution_db.read_bytes() == before_bytes  # the whole txn rolled back
    assert order_state(execution_db, "exec-21")[0] == "unknown"
    # the failed attempt itself is still audited
    stored = only(reconciliation_store.list_repair_attempts(run_id="run-21", db_path=reconciliation_db))
    assert stored.outcome is RepairOutcome.FAILED


def test_an_audit_append_failure_after_the_update_is_recoverable(paths, monkeypatch):
    """Scenario 18: the state update committed but the audit row could not
    be appended. The attempt reports the gap, the state history carries the
    provenance, and re-running the repair records already_applied."""
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-22")
    broker.submit_order(broker_request("exec-22"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-22")

    import execution.broker_reconciliation_repair as repair_module

    def audit_down(*args, **kwargs):
        raise reconciliation_store.ReconciliationStoreError("audit DB unavailable")

    monkeypatch.setattr(repair_module, "record_repair_attempt", audit_down)
    first = repair_service(QueryOnlyBroker(broker), paths, prefix="first").repair(run_id="run-22")
    monkeypatch.undo()

    attempt = only(first.attempts)
    assert attempt.outcome is RepairOutcome.APPLIED
    assert attempt.audit_error is not None and "audit DB unavailable" in attempt.audit_error
    assert first.has_audit_failures is True
    # the state change is durable and correct despite the missing audit row
    assert order_state(execution_db, "exec-22")[0] == "filled"
    assert reconciliation_store.list_repair_attempts(run_id="run-22", db_path=reconciliation_db) == ()
    history = execution_store.get_state_history("exec-22", db_path=execution_db)
    assert "run_id=run-22" in history[-1]["reason"]  # provenance survives in history

    # recovery: re-running converges and records the missing audit trail
    second = repair_service(QueryOnlyBroker(broker), paths, prefix="second").repair(run_id="run-22")
    assert only(second.attempts).outcome is RepairOutcome.ALREADY_APPLIED
    assert second.has_audit_failures is False
    stored = only(reconciliation_store.list_repair_attempts(run_id="run-22", db_path=reconciliation_db))
    assert stored.outcome is RepairOutcome.ALREADY_APPLIED
    assert len(execution_store.get_state_history("exec-22", db_path=execution_db)) == len(history)


# --- inputs, migration, structure ---------------------------------------------


def test_repair_acts_on_recorded_runs_only(paths):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-23")
    broker.submit_order(broker_request("exec-23"))
    # detect() alone records nothing
    BrokerOrderReconciliationService(
        broker=QueryOnlyBroker(broker),
        execution_db_path=execution_db,
        clock=lambda: OBSERVED_AT,
    ).detect(run_id="run-23")

    with pytest.raises(ReconciliationRepairError, match="does not exist"):
        repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-23")
    assert not reconciliation_db.exists()  # apply mode never creates the DB

    with pytest.raises(ReconciliationRepairError, match="not recorded"):
        repair_service(QueryOnlyBroker(broker), paths).repair(
            run_id="run-23", mode=RepairMode.DRY_RUN
        )
    assert not reconciliation_db.exists()

    record_run(QueryOnlyBroker(broker), paths, run_id="run-recorded")
    with pytest.raises(ReconciliationRepairError, match="not recorded"):
        repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-ghost")


def _downgrade_reconciliation_db_to_v1(path):
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"DROP INDEX {reconciliation_store._REPAIRS_RUN_INDEX}")
        connection.execute(f"DROP TABLE {reconciliation_store.REPAIRS_TABLE}")
        connection.execute(f"PRAGMA user_version = {reconciliation_store._MIGRATABLE_VERSION}")
        connection.commit()


def test_apply_mode_migrates_a_v1_audit_db_and_dry_run_refuses_it(paths):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-24")
    broker.submit_order(broker_request("exec-24"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-24")
    _downgrade_reconciliation_db_to_v1(reconciliation_db)
    v1_bytes = reconciliation_db.read_bytes()

    # dry-run never migrates: it refuses the v1 file and changes nothing
    with pytest.raises(
        reconciliation_store.ReconciliationSchemaError, match="init_reconciliation_db"
    ):
        repair_service(QueryOnlyBroker(broker), paths).repair(
            run_id="run-24", mode=RepairMode.DRY_RUN
        )
    assert reconciliation_db.read_bytes() == v1_bytes

    # apply mode migrates the existing file in place, then repairs
    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-24")
    assert only(result.attempts).outcome is RepairOutcome.APPLIED
    assert order_state(execution_db, "exec-24")[0] == "filled"


def test_repair_never_submits_or_cancels_through_the_broker(paths):
    """Driven with the RAW FakeBroker (not the poisoned facade) so the
    recorded call lists are the evidence."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-25")
    broker.submit_order(broker_request("exec-25"))
    record_run(broker, paths, run_id="run-25")
    submits, cancels = len(broker.submitted_requests), len(broker.cancel_requests)

    result = repair_service(broker, paths).repair(run_id="run-25")

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
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-26")
    broker.submit_order(broker_request("exec-26"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-26")

    with caplog.at_level("INFO", logger="execution.broker_reconciliation_repair"):
        repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-26")

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "exec-26" in messages
    assert "outcome=applied" in messages
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
    """The one sanctioned write is ``record_order_result``; every other
    execution-store write API (and raw SQL) is forbidden."""
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
