"""Issue #104: broker reconciliation -- SAFE order-state repair.

Everything runs against ``tmp_path`` SQLite files and an in-memory
FakeBroker: no network, no Discord, no S3, no sleep, no real orders.

The repair service is driven through :class:`QueryOnlyBroker`, a facade
whose ``submit_order`` / ``cancel_order`` are POISONED -- every test in
this file simultaneously pins that repair never writes through the broker.
Broker-side fixtures are seeded on the FakeBroker BEFORE detection/repair
start (that is the state being reconciled against), which is why this file
is listed in the submission-boundary allow-list.
"""

import ast
import pathlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from broker.broker_models import (
    BrokerRateLimitError,
    BrokerTimeoutError,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderState,
    OrderType,
)
from broker.fake_broker import (
    FakeBroker,
    PartiallyFill,
    RejectOrder,
    StayPending,
    TimeoutOrder,
)
from execution import execution_store
from execution.broker_reconciliation import BrokerOrderReconciliationService
from execution.execution_models import ExecutionRequest
from execution.reconciliation_models import (
    DiscrepancyType,
    OrderDiscrepancy,
    ReconciliationMode,
    ReconciliationRun,
    ReconciliationTargetType,
    ResolutionCategory,
)
from execution.reconciliation_repair import (
    BrokerOrderRepairService,
    OrderRepairAction,
    RepairMode,
    RepairOutcome,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Internal records predate FakeBroker's deterministic clock base
# (2026-01-01); detection observes at OBSERVED_AT, repair at REPAIRED_AT.
INTERNAL_T = datetime(2025, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
OBSERVED_AT = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
REPAIRED_AT = datetime(2026, 2, 2, 0, 0, 0, tzinfo=timezone.utc)
FILL_PRICE = Decimal("2400")
QUANTITY = Decimal("10")


def client_id(request_id: str) -> str:
    return f"client-{request_id}"


class QueryOnlyBroker:
    """Query-only Broker facade over a FakeBroker (test double, in-memory).

    ``submit_order`` / ``cancel_order`` raise: neither detection nor repair
    may ever reach a broker WRITE operation. ``get_order`` can be scripted
    to fail with a typed ``BrokerError`` per client_order_id.
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
    """Answer selected client IDs with externally crafted snapshots."""

    def __init__(self, inner: FakeBroker, snapshots: dict[str, OrderResult]):
        super().__init__(inner)
        self._snapshots = snapshots

    def get_order(self, client_order_id):
        return self._snapshots.get(client_order_id, self._inner.get_order(client_order_id))


@pytest.fixture
def execution_db(tmp_path):
    return tmp_path / "execution_orders.sqlite3"


def detect(broker, execution_db, *, run_id="detect-1") -> ReconciliationRun:
    return BrokerOrderReconciliationService(
        broker=broker,
        execution_db_path=execution_db,
        clock=lambda: OBSERVED_AT,
    ).detect(run_id=run_id)


def repairer(broker, execution_db) -> BrokerOrderRepairService:
    return BrokerOrderRepairService(
        broker=broker,
        execution_db_path=execution_db,
        clock=lambda: REPAIRED_AT,
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
        stop_loss_price=Decimal("2390"),
        take_profit_price=Decimal("2420"),
    )


def broker_request(request_id: str, *, quantity: Decimal = QUANTITY) -> OrderRequest:
    """Built directly (not via ``ExecutionRequest.to_order_request``) so the
    fixture stays off the sanctioned submission conversion path."""
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


def only(entries):
    assert len(entries) == 1, entries
    return entries[0]


def order_snapshot(db_path, request_id: str) -> tuple:
    row = execution_store.get_execution_order(request_id, db_path=db_path)
    history = execution_store.get_state_history(request_id, db_path=db_path)
    return row, history


# --- the allowed repairs (Issue #104 minimum set) -----------------------------


def test_unknown_is_repaired_to_broker_filled(execution_db):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-1")
    broker.submit_order(broker_request("exec-1"))  # broker-side fixture: FILLED
    run = detect(QueryOnlyBroker(broker), execution_db)

    result = repairer(QueryOnlyBroker(broker), execution_db).repair(run, mode=RepairMode.APPLY)

    action = only(result.actions)
    assert action.outcome is RepairOutcome.REPAIRED
    assert action.target_id == "exec-1"
    assert (action.state_before, action.state_after) == (OrderState.UNKNOWN, OrderState.FILLED)
    assert action.broker_state is OrderState.FILLED
    assert action.client_order_id == client_id("exec-1")
    assert action.broker_order_id == "FAKE-1"
    assert action.observed_at_utc == REPAIRED_AT
    assert "detect-1" in action.reason

    row = execution_store.get_execution_order("exec-1", db_path=execution_db)
    assert row["order_state"] == "filled"
    assert row["submission_phase"] == "submitted"
    assert row["broker_order_id"] == "FAKE-1"
    # the stored reason names the source run, the broker reference IDs and
    # the repair-time observation timestamp (Issue #104 record contract)
    for token in ("detect-1", "FAKE-1", client_id("exec-1"), REPAIRED_AT.isoformat()):
        assert token in row["last_state_reason"]
    # the state change went through the state machine: a history row was
    # appended with the full broker snapshot anchored on it
    history = execution_store.get_state_history("exec-1", db_path=execution_db)
    assert [(h["previous_state"], h["new_state"]) for h in history] == [
        (None, "pending"),
        ("pending", "unknown"),
        ("unknown", "filled"),
    ]
    assert history[-1]["order_result_json"] is not None
    assert execution_store.load_order_result("exec-1", db_path=execution_db) == broker.get_order(
        client_id("exec-1")
    )


def test_unknown_is_repaired_to_broker_cancelled(execution_db):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-2"), StayPending(broker_order_id="B-2"))
    broker.submit_order(broker_request("exec-2"))
    broker.cancel_order(client_id("exec-2"))  # broker-side fixture: CANCELLED
    seed_internal_unknown(execution_db, "exec-2")
    run = detect(QueryOnlyBroker(broker), execution_db)

    result = repairer(QueryOnlyBroker(broker), execution_db).repair(run, mode=RepairMode.APPLY)

    action = only(result.actions)
    assert action.outcome is RepairOutcome.REPAIRED
    assert (action.state_before, action.state_after) == (
        OrderState.UNKNOWN,
        OrderState.CANCELLED,
    )
    row = execution_store.get_execution_order("exec-2", db_path=execution_db)
    assert row["order_state"] == "cancelled"
    assert row["broker_order_id"] == "B-2"


def test_unknown_is_repaired_to_broker_rejected(execution_db):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-3"), RejectOrder(reason="insufficient margin"))
    broker.submit_order(broker_request("exec-3"))  # broker-side fixture: REJECTED
    seed_internal_unknown(execution_db, "exec-3")
    run = detect(QueryOnlyBroker(broker), execution_db)

    result = repairer(QueryOnlyBroker(broker), execution_db).repair(run, mode=RepairMode.APPLY)

    action = only(result.actions)
    assert action.outcome is RepairOutcome.REPAIRED
    assert (action.state_before, action.state_after) == (
        OrderState.UNKNOWN,
        OrderState.REJECTED,
    )
    row = execution_store.get_execution_order("exec-3", db_path=execution_db)
    assert row["order_state"] == "rejected"
    stored = execution_store.load_order_result("exec-3", db_path=execution_db)
    assert stored.reject_reason == "insufficient margin"


def test_submitting_pending_is_repaired_to_broker_accepted(execution_db):
    """SUBMITTING (= broker state PENDING) -> the broker-confirmed state."""
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-4"), StayPending(broker_order_id="B-4"))
    broker.submit_order(broker_request("exec-4"))  # broker-side fixture: ACCEPTED
    seed_internal_pending(execution_db, "exec-4")
    run = detect(QueryOnlyBroker(broker), execution_db)

    result = repairer(QueryOnlyBroker(broker), execution_db).repair(run, mode=RepairMode.APPLY)

    action = only(result.actions)
    assert action.outcome is RepairOutcome.REPAIRED
    assert (action.state_before, action.state_after) == (
        OrderState.PENDING,
        OrderState.ACCEPTED,
    )
    row = execution_store.get_execution_order("exec-4", db_path=execution_db)
    assert (row["order_state"], row["submission_phase"]) == ("accepted", "submitted")


def test_accepted_is_repaired_to_broker_confirmed_state(execution_db):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-5"), StayPending(broker_order_id="B-5"))
    accepted = broker.submit_order(broker_request("exec-5"))
    seed_internal_pending(execution_db, "exec-5")
    execution_store.record_order_result(
        "exec-5", accepted, reason="broker accepted", db_path=execution_db
    )
    broker.cancel_order(client_id("exec-5"))  # broker-side fixture: CANCELLED
    run = detect(QueryOnlyBroker(broker), execution_db)

    result = repairer(QueryOnlyBroker(broker), execution_db).repair(run, mode=RepairMode.APPLY)

    action = only(result.actions)
    assert action.outcome is RepairOutcome.REPAIRED
    assert (action.state_before, action.state_after) == (
        OrderState.ACCEPTED,
        OrderState.CANCELLED,
    )
    row = execution_store.get_execution_order("exec-5", db_path=execution_db)
    assert row["order_state"] == "cancelled"


def test_partially_filled_is_repaired_to_broker_confirmed_state(execution_db):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(
        client_id("exec-6"),
        PartiallyFill(fills=((FILL_PRICE, Decimal("4")),), broker_order_id="B-6"),
    )
    partial = broker.submit_order(broker_request("exec-6"))
    seed_internal_pending(execution_db, "exec-6")
    execution_store.record_order_result(
        "exec-6", partial, reason="broker partial fill", db_path=execution_db
    )
    broker.cancel_order(client_id("exec-6"))  # broker-side: CANCELLED, fills kept
    run = detect(QueryOnlyBroker(broker), execution_db)

    result = repairer(QueryOnlyBroker(broker), execution_db).repair(run, mode=RepairMode.APPLY)

    action = only(result.actions)
    assert action.outcome is RepairOutcome.REPAIRED
    assert (action.state_before, action.state_after) == (
        OrderState.PARTIALLY_FILLED,
        OrderState.CANCELLED,
    )
    stored = execution_store.load_order_result("exec-6", db_path=execution_db)
    assert stored.state is OrderState.CANCELLED
    assert stored.filled_quantity == Decimal("4")  # known fills survive the repair
    assert len(stored.fills) == 1


# --- forbidden transitions and ineligible targets -----------------------------


def rejected_snapshot(request_id: str, *, seconds: int = 90) -> OrderResult:
    return OrderResult(
        client_order_id=client_id(request_id),
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
        updated_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds),
        reject_reason="rejected later",
    )


def forged_candidate(
    request_id: str, *, internal_state: OrderState, broker_state: OrderState
) -> ReconciliationRun:
    """A HOSTILE / mis-classified input: a run claiming auto-repair candidacy
    for a transition the state machine forbids. The repair service must not
    trust the classification -- it re-proves everything at repair time."""
    discrepancy = OrderDiscrepancy(
        discrepancy_type=DiscrepancyType.STATE_MISMATCH,
        target_type=ReconciliationTargetType.EXECUTION_ORDER,
        target_id=request_id,
        resolution_category=ResolutionCategory.AUTO_REPAIR_CANDIDATE,
        observed_at_utc=OBSERVED_AT,
        detail="forged classification for the test",
        internal_state=internal_state,
        broker_state=broker_state,
        client_order_id=client_id(request_id),
    )
    return ReconciliationRun(
        run_id="forged-run",
        mode=ReconciliationMode.DETECTION_ONLY,
        started_at_utc=OBSERVED_AT,
        finished_at_utc=OBSERVED_AT,
        checked_order_count=1,
        discrepancies=(discrepancy,),
    )


def test_a_transition_the_state_machine_forbids_is_refused(execution_db):
    """PARTIALLY_FILLED -> REJECTED is not a legal transition; even a run
    that (wrongly) claims candidacy must not get it applied."""
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(
        client_id("exec-7"),
        PartiallyFill(fills=((FILL_PRICE, Decimal("4")),), broker_order_id="B-7"),
    )
    partial = broker.submit_order(broker_request("exec-7"))
    seed_internal_pending(execution_db, "exec-7")
    execution_store.record_order_result(
        "exec-7", partial, reason="broker partial fill", db_path=execution_db
    )
    before = order_snapshot(execution_db, "exec-7")
    facade = SnapshotBroker(broker, {client_id("exec-7"): rejected_snapshot("exec-7")})
    run = forged_candidate(
        "exec-7",
        internal_state=OrderState.PARTIALLY_FILLED,
        broker_state=OrderState.REJECTED,
    )

    result = repairer(facade, execution_db).repair(run, mode=RepairMode.APPLY)

    action = only(result.actions)
    assert action.outcome is RepairOutcome.REFUSED
    assert "state machine" in action.reason
    assert (action.state_before, action.state_after) == (
        OrderState.PARTIALLY_FILLED,
        OrderState.PARTIALLY_FILLED,
    )
    assert order_snapshot(execution_db, "exec-7") == before


def test_a_terminal_internal_state_is_never_left(execution_db):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    filled = broker.submit_order(broker_request("exec-8"))
    seed_internal_pending(execution_db, "exec-8")
    execution_store.record_order_result(
        "exec-8", filled, reason="broker filled", db_path=execution_db
    )
    before = order_snapshot(execution_db, "exec-8")
    cancelled = replace(
        rejected_snapshot("exec-8"),
        state=OrderState.CANCELLED,
        reject_reason=None,
        broker_order_id=filled.broker_order_id,
    )
    facade = SnapshotBroker(broker, {client_id("exec-8"): cancelled})
    run = forged_candidate(
        "exec-8", internal_state=OrderState.FILLED, broker_state=OrderState.CANCELLED
    )

    result = repairer(facade, execution_db).repair(run, mode=RepairMode.APPLY)

    action = only(result.actions)
    assert action.outcome is RepairOutcome.REFUSED
    assert "terminal" in action.reason
    assert order_snapshot(execution_db, "exec-8") == before


def test_manual_review_and_query_failed_targets_are_not_eligible(execution_db):
    """要確認対象 (manual review) and query-failed discrepancies are never
    auto-repaired -- the broker is not even queried for them."""
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    # both-sides-UNKNOWN -> manual review
    broker.script_order(client_id("exec-9"), TimeoutOrder())
    with pytest.raises(BrokerTimeoutError):
        broker.submit_order(broker_request("exec-9"))
    seed_internal_unknown(execution_db, "exec-9")
    # broker-only order -> manual review (never adopted)
    broker.submit_order(broker_request("stranger"))
    # query failure at detection time -> query_failed
    seed_internal_unknown(execution_db, "exec-10")
    broker.submit_order(broker_request("exec-10"))
    detection_broker = QueryOnlyBroker(
        broker, query_failures={client_id("exec-10"): BrokerRateLimitError("throttled")}
    )
    run = detect(detection_broker, execution_db)
    assert {d.resolution_category for d in run.discrepancies} == {
        ResolutionCategory.MANUAL_REVIEW,
        ResolutionCategory.QUERY_FAILED,
    }
    before_9 = order_snapshot(execution_db, "exec-9")
    before_10 = order_snapshot(execution_db, "exec-10")
    execution_bytes = execution_db.read_bytes()

    result = repairer(QueryOnlyBroker(broker), execution_db).repair(run, mode=RepairMode.APPLY)

    assert {a.outcome for a in result.actions} == {RepairOutcome.NOT_ELIGIBLE}
    assert len(result.actions) == len(run.discrepancies) == 3
    broker_only = [
        a for a in result.actions
        if a.target_type is ReconciliationTargetType.BROKER_ORDER
    ]
    assert only(broker_only).target_id == client_id("stranger")
    assert order_snapshot(execution_db, "exec-9") == before_9
    assert order_snapshot(execution_db, "exec-10") == before_10
    assert execution_db.read_bytes() == execution_bytes


# --- repair-time query failures and unconfirmed answers -----------------------


def test_a_repair_time_query_failure_changes_nothing(execution_db):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-11")
    broker.submit_order(broker_request("exec-11"))
    run = detect(QueryOnlyBroker(broker), execution_db)
    assert only(run.discrepancies).resolution_category is ResolutionCategory.AUTO_REPAIR_CANDIDATE
    before = order_snapshot(execution_db, "exec-11")
    execution_bytes = execution_db.read_bytes()
    failing = QueryOnlyBroker(
        broker, query_failures={client_id("exec-11"): BrokerTimeoutError("timed out")}
    )

    result = repairer(failing, execution_db).repair(run, mode=RepairMode.APPLY)

    action = only(result.actions)
    assert action.outcome is RepairOutcome.QUERY_FAILED
    assert action.broker_state is None
    assert (action.state_before, action.state_after) == (
        OrderState.UNKNOWN,
        OrderState.UNKNOWN,
    )
    assert order_snapshot(execution_db, "exec-11") == before
    assert execution_db.read_bytes() == execution_bytes


def test_an_unknown_broker_answer_at_repair_time_changes_nothing(execution_db):
    """A repair-time UNKNOWN confirms nothing -- explicit Issue #104
    prohibition, even when detection had seen a settled state."""
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-12")
    broker.submit_order(broker_request("exec-12"))
    run = detect(QueryOnlyBroker(broker), execution_db)
    before = order_snapshot(execution_db, "exec-12")
    unknown_answer = OrderResult(
        client_order_id=client_id("exec-12"),
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
        updated_at_utc=datetime(2026, 1, 1, 0, 2, 0, tzinfo=timezone.utc),
    )
    facade = SnapshotBroker(broker, {client_id("exec-12"): unknown_answer})

    result = repairer(facade, execution_db).repair(run, mode=RepairMode.APPLY)

    action = only(result.actions)
    assert action.outcome is RepairOutcome.BROKER_STATE_UNCONFIRMED
    assert action.broker_state is OrderState.UNKNOWN
    assert order_snapshot(execution_db, "exec-12") == before


def test_an_order_missing_at_repair_time_changes_nothing(execution_db):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-13")
    broker.submit_order(broker_request("exec-13"))
    run = detect(QueryOnlyBroker(broker), execution_db)
    before = order_snapshot(execution_db, "exec-13")
    vanished = FakeBroker(default_fill_price=FILL_PRICE)  # holds no orders at all

    result = repairer(QueryOnlyBroker(vanished), execution_db).repair(
        run, mode=RepairMode.APPLY
    )

    action = only(result.actions)
    assert action.outcome is RepairOutcome.BROKER_STATE_UNCONFIRMED
    assert "never" in action.reason and action.broker_state is None
    assert order_snapshot(execution_db, "exec-13") == before


# --- dry run ------------------------------------------------------------------


def test_dry_run_reports_would_repair_and_writes_nothing(execution_db):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-14")
    broker.submit_order(broker_request("exec-14"))
    run = detect(QueryOnlyBroker(broker), execution_db)
    before = order_snapshot(execution_db, "exec-14")
    execution_bytes = execution_db.read_bytes()

    result = repairer(QueryOnlyBroker(broker), execution_db).repair(
        run, mode=RepairMode.DRY_RUN
    )

    action = only(result.actions)
    assert action.outcome is RepairOutcome.WOULD_REPAIR
    assert (action.state_before, action.state_after) == (
        OrderState.UNKNOWN,
        OrderState.FILLED,
    )
    assert "dry run" in action.reason
    assert result.mode is RepairMode.DRY_RUN
    # completely side-effect free: no state change, no history row, no DB
    # update -- the file is byte-for-byte identical
    assert order_snapshot(execution_db, "exec-14") == before
    assert execution_db.read_bytes() == execution_bytes


def test_dry_run_and_apply_agree_on_the_repair_set(execution_db):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-15")
    broker.submit_order(broker_request("exec-15"))
    broker.script_order(client_id("exec-16"), StayPending(broker_order_id="B-16"))
    broker.submit_order(broker_request("exec-16"))
    seed_internal_pending(execution_db, "exec-16")
    broker.submit_order(broker_request("stranger"))  # never adopted, either mode
    run = detect(QueryOnlyBroker(broker), execution_db)
    service = repairer(QueryOnlyBroker(broker), execution_db)

    dry = service.repair(run, mode=RepairMode.DRY_RUN)
    applied = service.repair(run, mode=RepairMode.APPLY)

    would = {a.target_id for a in dry.by_outcome(RepairOutcome.WOULD_REPAIR)}
    repaired = {a.target_id for a in applied.by_outcome(RepairOutcome.REPAIRED)}
    assert would == repaired == {"exec-15", "exec-16"}
    assert {a.target_id for a in dry.by_outcome(RepairOutcome.NOT_ELIGIBLE)} == {
        client_id("stranger")
    }


# --- idempotency --------------------------------------------------------------


def test_reapplying_the_same_run_writes_nothing_the_second_time(execution_db):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-17")
    broker.submit_order(broker_request("exec-17"))
    run = detect(QueryOnlyBroker(broker), execution_db)
    service = repairer(QueryOnlyBroker(broker), execution_db)

    first = service.repair(run, mode=RepairMode.APPLY)
    assert only(first.actions).outcome is RepairOutcome.REPAIRED
    after_first = order_snapshot(execution_db, "exec-17")
    execution_bytes = execution_db.read_bytes()

    second = service.repair(run, mode=RepairMode.APPLY)
    third = service.repair(run, mode=RepairMode.APPLY)

    for later in (second, third):
        action = only(later.actions)
        assert action.outcome is RepairOutcome.ALREADY_CONSISTENT
        assert (action.state_before, action.state_after) == (
            OrderState.FILLED,
            OrderState.FILLED,
        )
    # state AND history are untouched: no duplicate updates, no duplicate
    # history rows, byte-for-byte identical DB
    assert order_snapshot(execution_db, "exec-17") == after_first
    assert execution_db.read_bytes() == execution_bytes


def test_a_repaired_order_produces_no_discrepancy_on_the_next_detection(execution_db):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-18")
    broker.submit_order(broker_request("exec-18"))
    run = detect(QueryOnlyBroker(broker), execution_db)
    repairer(QueryOnlyBroker(broker), execution_db).repair(run, mode=RepairMode.APPLY)

    fresh = detect(QueryOnlyBroker(broker), execution_db, run_id="detect-2")

    assert fresh.discrepancies == ()
    assert fresh.checked_order_count == 1


def test_a_stale_run_is_not_applied_after_the_internal_state_moved(execution_db):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-19"), StayPending(broker_order_id="B-19"))
    accepted = broker.submit_order(broker_request("exec-19"))
    seed_internal_unknown(execution_db, "exec-19")
    run = detect(QueryOnlyBroker(broker), execution_db)  # UNKNOWN vs ACCEPTED
    # meanwhile another path resolved the order internally to CANCELLED
    cancelled = replace(
        accepted,
        state=OrderState.CANCELLED,
        updated_at_utc=accepted.updated_at_utc + timedelta(seconds=1),
    )
    execution_store.record_order_result(
        "exec-19", cancelled, reason="resolved elsewhere", db_path=execution_db
    )
    before = order_snapshot(execution_db, "exec-19")

    result = repairer(QueryOnlyBroker(broker), execution_db).repair(
        run, mode=RepairMode.APPLY
    )

    action = only(result.actions)
    assert action.outcome is RepairOutcome.INTERNAL_STATE_CHANGED
    assert action.state_before is OrderState.CANCELLED
    assert "stale" in action.reason
    assert order_snapshot(execution_db, "exec-19") == before


# --- inputs and model invariants ----------------------------------------------


def test_the_service_validates_broker_run_mode_and_clock(execution_db):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    with pytest.raises(ValueError, match="Broker protocol"):
        BrokerOrderRepairService(broker=object(), execution_db_path=execution_db)
    with pytest.raises(ValueError, match="clock"):
        BrokerOrderRepairService(
            broker=broker, execution_db_path=execution_db, clock="not-callable"
        )
    service = repairer(QueryOnlyBroker(broker), execution_db)
    empty_run = ReconciliationRun(
        run_id="empty",
        mode=ReconciliationMode.DETECTION_ONLY,
        started_at_utc=OBSERVED_AT,
        finished_at_utc=OBSERVED_AT,
        checked_order_count=0,
    )
    with pytest.raises(ValueError, match="ReconciliationRun"):
        service.repair("not-a-run", mode=RepairMode.APPLY)
    with pytest.raises(ValueError, match="RepairMode"):
        service.repair(empty_run, mode="apply")
    naive = BrokerOrderRepairService(
        broker=broker, execution_db_path=execution_db, clock=lambda: datetime(2026, 2, 2)
    )
    with pytest.raises(ValueError, match="clock"):
        naive.repair(empty_run, mode=RepairMode.DRY_RUN)


def test_an_empty_run_yields_an_empty_result_without_touching_anything(execution_db):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    empty_run = ReconciliationRun(
        run_id="empty",
        mode=ReconciliationMode.DETECTION_ONLY,
        started_at_utc=OBSERVED_AT,
        finished_at_utc=OBSERVED_AT,
        checked_order_count=0,
    )

    result = repairer(QueryOnlyBroker(broker), execution_db).repair(
        empty_run, mode=RepairMode.APPLY
    )

    assert result.actions == ()
    assert not execution_db.exists()  # repair never creates the order DB


def test_action_invariants_reject_inconsistent_records():
    base = dict(
        target_type=ReconciliationTargetType.EXECUTION_ORDER,
        target_id="exec-x",
        outcome=RepairOutcome.REPAIRED,
        reason="r",
        state_before=OrderState.UNKNOWN,
        state_after=OrderState.FILLED,
        broker_state=OrderState.FILLED,
        client_order_id="client-exec-x",
        broker_order_id="B-X",
        observed_at_utc=REPAIRED_AT,
    )
    OrderRepairAction(**base)  # the valid shape
    with pytest.raises(ValueError, match="requires state_before"):
        OrderRepairAction(**{**base, "state_before": None})
    with pytest.raises(ValueError, match="observed broker state"):
        OrderRepairAction(**{**base, "broker_state": OrderState.CANCELLED})
    with pytest.raises(ValueError, match="unsettled internal state"):
        OrderRepairAction(**{**base, "state_before": OrderState.CANCELLED})
    with pytest.raises(ValueError, match="broker-confirmed"):
        OrderRepairAction(
            **{
                **base,
                "state_before": OrderState.PENDING,
                "state_after": OrderState.UNKNOWN,
                "broker_state": OrderState.UNKNOWN,
            }
        )
    with pytest.raises(ValueError, match="no state change"):
        OrderRepairAction(
            **{**base, "outcome": RepairOutcome.REFUSED}
        )
    with pytest.raises(ValueError, match="observes nothing"):
        OrderRepairAction(
            **{
                **base,
                "outcome": RepairOutcome.QUERY_FAILED,
                "state_after": OrderState.UNKNOWN,
                "broker_state": None,
            }
        )
    with pytest.raises(ValueError, match="never adopted"):
        OrderRepairAction(
            target_type=ReconciliationTargetType.BROKER_ORDER,
            target_id="client-stranger",
            outcome=RepairOutcome.REPAIRED,
            reason="r",
            state_before=OrderState.UNKNOWN,
            state_after=OrderState.FILLED,
            broker_state=OrderState.FILLED,
            client_order_id="client-stranger",
            observed_at_utc=REPAIRED_AT,
        )


def test_a_dry_run_result_may_not_contain_repaired_actions(execution_db):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-20")
    broker.submit_order(broker_request("exec-20"))
    run = detect(QueryOnlyBroker(broker), execution_db)
    applied = repairer(QueryOnlyBroker(broker), execution_db).repair(
        run, mode=RepairMode.APPLY
    )
    from execution.reconciliation_repair import OrderRepairRun

    with pytest.raises(ValueError, match="dry_run"):
        OrderRepairRun(
            source_run_id=applied.source_run_id,
            mode=RepairMode.DRY_RUN,
            started_at_utc=applied.started_at_utc,
            finished_at_utc=applied.finished_at_utc,
            actions=applied.actions,
        )


# --- logging ------------------------------------------------------------------


def test_the_repair_run_is_logged_with_ids_states_and_outcomes(execution_db, caplog):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-21")
    broker.submit_order(broker_request("exec-21"))
    run = detect(QueryOnlyBroker(broker), execution_db)

    with caplog.at_level("INFO", logger="execution.reconciliation_repair"):
        repairer(QueryOnlyBroker(broker), execution_db).repair(run, mode=RepairMode.APPLY)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "outcome=repaired" in messages
    assert "exec-21" in messages
    assert "repaired=1" in messages


# --- structural guarantees ----------------------------------------------------


def _attribute_calls(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_the_repair_module_writes_only_through_record_order_result():
    """The one sanctioned write API. No order creation, no phase changes, no
    state-only writes, no DB initialisation, no reconciliation-store writes."""
    called = _attribute_calls(REPO_ROOT / "execution" / "reconciliation_repair.py")
    assert "record_order_result" in called
    forbidden = {
        "create_execution_order",
        "mark_submitting",
        "record_order_state",
        "init_db",
        "init_reconciliation_db",
        "record_reconciliation_run",
        "submit_order",
        "cancel_order",
    }
    assert called & forbidden == set()


def test_the_repair_module_never_touches_sqlite_directly():
    """状態を直接DB更新しない: every state change goes through the store's
    validated API -- the module must not open connections or run SQL."""
    source = (REPO_ROOT / "execution" / "reconciliation_repair.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "sqlite3" not in imported
    called = _attribute_calls(REPO_ROOT / "execution" / "reconciliation_repair.py")
    assert called & {"connect", "execute", "executemany", "executescript"} == set()


def test_repair_never_submits_or_cancels_through_the_broker(execution_db):
    """Driven with the RAW FakeBroker (no poisoned facade) so the recorded
    call lists are the evidence."""
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-22")
    broker.submit_order(broker_request("exec-22"))
    run = detect(broker, execution_db)
    submits, cancels = len(broker.submitted_requests), len(broker.cancel_requests)

    result = repairer(broker, execution_db).repair(run, mode=RepairMode.APPLY)

    assert only(result.actions).outcome is RepairOutcome.REPAIRED
    assert len(broker.submitted_requests) == submits
    assert len(broker.cancel_requests) == cancels
