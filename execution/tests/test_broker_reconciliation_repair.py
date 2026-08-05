"""Issue #104: broker reconciliation -- safe order-state REPAIR.

Everything runs against ``tmp_path`` SQLite files and an in-memory
FakeBroker: no network, no Discord, no S3, no sleep, no real orders.

The repair service is driven through :class:`QueryOnlyBroker`, a facade
whose ``submit_order`` / ``cancel_order`` are POISONED -- so every test in
this file simultaneously pins that repair never writes through the broker.
The broker-side fixtures themselves are seeded on the FakeBroker BEFORE
detection/repair start (that is the state being reconciled against), which
is why this file is listed in the submission-boundary allow-list.

Fault injection (Issue #104 review): post-commit failures, pass-level
failures, revision-CAS races, three-way already-applied verification,
reservation / finalize / crash recovery and dry-run no-side-effect are all
exercised here.
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


def repair_service(
    broker, paths, *, prefix="attempt", clock=None
) -> BrokerOrderReconciliationRepairService:
    execution_db, reconciliation_db = paths
    counter = itertools.count(1)
    return BrokerOrderReconciliationRepairService(
        broker=broker,
        execution_db_path=execution_db,
        reconciliation_db_path=reconciliation_db,
        clock=clock or (lambda: REPAIR_AT),
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
    # applied_at is the committed history timestamp = the broker snapshot's
    # updated_at (known BEFORE the commit; broker clock domain)
    assert attempt.applied_at_utc == filled.updated_at_utc
    assert attempt.broker_order_id == "FAKE-1"
    assert attempt.failure_code is None
    assert order_state(execution_db, "exec-1") == ("filled", "FAKE-1")
    # STRUCTURED provenance on the committed history row (never reason parsing)
    history = execution_store.get_state_history("exec-1", db_path=execution_db)
    assert history[-1]["repair_attempt_id"] == attempt.attempt_id
    assert history[-1]["changed_at_utc"] == attempt.applied_at_utc.isoformat()
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
        SnapshotBroker(broker, {client_id("exec-7"): replace(cancelled, broker_order_id=filled.broker_order_id)}),
        paths,
    ).repair(run_id="run-7")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.REJECTED
    assert attempt.failure_code == "forbidden_transition"
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
                internal_revision=2,
            )
        ],
    )
    before = order_snapshot(execution_db, ("exec-8",))

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-8")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.REJECTED
    assert attempt.failure_code == "client_order_id_binding_mismatch"
    assert order_snapshot(execution_db, ("exec-8",)) == before


def test_manual_review_targets_are_never_repaired(paths):
    """Internal ACCEPTED vs broker REJECTED without a broker id (a known
    Issue #103 manual-review case) plus a broker-only order. Both attempts
    are reserved and finalized as skipped -- audited, never applied."""
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
    assert broker_only.failure_code == "not_eligible_broker_only"
    assert order_snapshot(execution_db, ("exec-9",)) == before
    # both skips are finalized in the audit DB (reservation -> skipped)
    assert len(finalized(reconciliation_db, "run-9")) == 2
    assert pending(reconciliation_db) == ()


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
    assert attempt.failure_code == "not_eligible_query_failed"
    assert order_snapshot(execution_db, ("exec-10",)) == before
    assert only(finalized(reconciliation_db, "run-10")).outcome is RepairOutcome.SKIPPED


def test_a_broker_that_now_reports_unknown_changes_nothing(paths):
    """The candidate observed FILLED, but the repair-time re-query reports
    UNKNOWN -- the stored observation is not confirmable."""
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
    assert attempt.failure_code == "requery_disagrees_with_observation"
    assert order_snapshot(execution_db, ("exec-11",)) == before


@pytest.mark.parametrize(
    "error, expected_code, expected_failure",
    [
        (BrokerTimeoutError("timed out"), "timeout", "broker_query_failed"),
        (
            BrokerCommunicationError("connection refused"),
            "communication_failure",
            "broker_query_failed",
        ),
        (RuntimeError("adapter bug"), "communication_failure", "broker_contract_violation"),
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
    assert attempt.failure_code == expected_failure
    # sanitized audit: the raw exception text never reaches the audit row
    assert "adapter bug" not in attempt.detail
    assert "connection refused" not in attempt.detail
    assert order_snapshot(execution_db, ("exec-12",)) == before
    assert only(finalized(reconciliation_db, "run-12")).outcome is RepairOutcome.QUERY_FAILED


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
    assert attempt.failure_code == "broker_reference_changed"
    assert "B-ORIG" in attempt.detail and "B-FOREIGN" in attempt.detail
    assert order_snapshot(execution_db, ("exec-14",)) == before


def test_a_broker_id_now_owned_by_another_request_is_a_conflict(paths):
    """The id was unassigned at detection but another request recorded it
    before the repair ran -- reflecting it would collide with the DB-wide
    uniqueness."""
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
    assert by_target["exec-clash"].failure_code == "broker_reference_owned_elsewhere"
    assert "exec-owner" in by_target["exec-clash"].detail
    # exec-owner itself was a broker_order_missing manual-review target
    assert by_target["exec-owner"].outcome is RepairOutcome.SKIPPED
    assert order_snapshot(execution_db, ("exec-clash", "exec-owner")) == before


def test_an_ownership_race_after_the_lookup_is_still_a_conflict(paths, monkeypatch):
    """Fault: the ownership lookup answers 'free' but another request claims
    the id before the CAS commits -- the store's uniqueness constraint rolls
    the update back and the attempt reports conflict."""
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
    # simulate the race: the lookup misses the concurrent owner
    monkeypatch.setattr(
        execution_store, "get_execution_order_by_broker_order_id", lambda *a, **k: None
    )
    before = order_snapshot(execution_db, ("exec-race",))

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-16")

    clash = only([a for a in result.attempts if a.target_id == "exec-race"])
    assert clash.outcome is RepairOutcome.CONFLICT
    assert clash.failure_code == "uniqueness_conflict"
    assert order_snapshot(execution_db, ("exec-race",)) == before


# --- revision CAS: same-state changes are detected (review 17-23) -------------


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
    """State-only CAS cannot see these; the revision CAS must (review 17-21):
    an UNKNOWN -> UNKNOWN history append, a same-state snapshot/timestamp
    refresh, and a further partial fill all leave the state text unchanged."""
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
    assert attempt.failure_code == "internal_order_changed"
    assert attempt.internal_state_before is observed.internal_state  # state text unchanged
    assert order_snapshot(execution_db, ("exec-17",)) == before


def test_a_stale_reconciliation_result_never_rolls_back_newer_state(paths):
    """Review 23: the normal submission path settles the order differently
    after the observation -- the repair must not overwrite it."""
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
    assert "'cancelled'" in attempt.detail
    assert order_snapshot(execution_db, ("exec-18",)) == before


def test_an_observation_without_a_revision_is_stale(paths):
    """Records stored before revision tracking (schema v1) cannot be proven
    current and are never auto-repaired."""
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
    assert attempt.failure_code == "missing_observed_revision"
    assert order_snapshot(execution_db, ("exec-19",)) == before


def test_concurrent_repairs_never_duplicate_history_or_state(paths):
    """Review 22: two workers, one target. Exactly one applies; the loser's
    CAS is refused and the three-way check reports already_applied."""
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
    audit = finalized(reconciliation_db, "run-20")
    assert sorted(a.outcome.value for a in audit) == ["already_applied", "applied"]
    assert pending(reconciliation_db) == ()


# --- already_applied: three-way verification (review 25-31) -------------------


def applied_elsewhere(paths, request_id, run_id):
    """Candidate observed FILLED; the same outcome is recorded by the normal
    path after detection. Returns the broker fixture and the fresh snapshot."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, request_id)
    filled = broker.submit_order(broker_request(request_id))
    record_run(QueryOnlyBroker(broker), paths, run_id=run_id)
    execution_store.record_order_result(request_id, filled, reason="filled", db_path=execution_db)
    return broker, filled


def test_already_applied_requires_a_fresh_matching_broker_snapshot(paths):
    """Review 31: only the full three-way agreement (stored discrepancy x
    current row/anchored snapshot x fresh re-query) yields already_applied."""
    execution_db, reconciliation_db = paths
    broker, filled = applied_elsewhere(paths, "exec-21", "run-21")
    before = order_snapshot(execution_db, ("exec-21",))

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-21")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.ALREADY_APPLIED
    assert attempt.internal_state_after is OrderState.FILLED
    assert order_snapshot(execution_db, ("exec-21",)) == before  # no new history row
    assert only(finalized(reconciliation_db, "run-21")).outcome is RepairOutcome.ALREADY_APPLIED


def test_already_applied_is_never_concluded_without_asking_the_broker(paths):
    """Review 25: the internal state matches the observed outcome but the
    repair-time query fails -> query_failed, NOT already_applied."""
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
        (  # review 26: the broker no longer reports the observed state
            lambda filled: unknown_snapshot("exec-23", seconds=30),
            "differs from the observed",
        ),
        (  # review 27/28: fill content / average price differ
            lambda filled: replace(
                filled,
                average_fill_price=filled.average_fill_price + Decimal("1"),
                fills=(replace(filled.fills[0], price=filled.fills[0].price + Decimal("1")),),
                updated_at_utc=filled.updated_at_utc + timedelta(seconds=30),
            ),
            "update contract",
        ),
        (  # review 29: broker reference differs
            lambda filled: replace(
                filled,
                broker_order_id="B-OTHER",
                updated_at_utc=filled.updated_at_utc + timedelta(seconds=30),
            ),
            "broker_order_id",
        ),
        (  # review 30: the fresh snapshot is older than the recorded one
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
    assert attempt.failure_code == "already_applied_content_mismatch"
    assert expected_fragment in attempt.detail
    assert order_snapshot(execution_db, ("exec-23",)) == before


# --- dry-run: complete no-side-effect contract (review 41-47) -----------------


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
    assert attempt.applied_at_utc is None
    # bytes, mtimes, directory entries (journal/WAL/SHM sidecars included)
    assert execution_db.read_bytes() == execution_bytes
    assert reconciliation_db.read_bytes() == reconciliation_bytes
    assert (execution_db.stat().st_mtime_ns, reconciliation_db.stat().st_mtime_ns) == mtimes_before
    assert sorted(p.name for p in tmp_path.iterdir()) == files_before
    # no reservation, no audit row, no broker write
    assert pending(reconciliation_db) == ()
    assert finalized(reconciliation_db, "run-24") == ()
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
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-26")
    broker.submit_order(broker_request("exec-26"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-26")

    first = repair_service(QueryOnlyBroker(broker), paths, prefix="first").repair(run_id="run-26")
    after_first = order_snapshot(execution_db, ("exec-26",))
    second = repair_service(QueryOnlyBroker(broker), paths, prefix="second").repair(run_id="run-26")
    third = repair_service(QueryOnlyBroker(broker), paths, prefix="third").repair(run_id="run-26")

    assert only(first.attempts).outcome is RepairOutcome.APPLIED
    assert only(second.attempts).outcome is RepairOutcome.ALREADY_APPLIED
    assert only(third.attempts).outcome is RepairOutcome.ALREADY_APPLIED
    # the final state holds and the history did NOT grow after the first pass
    assert order_snapshot(execution_db, ("exec-26",)) == after_first
    assert order_state(execution_db, "exec-26")[0] == "filled"
    # every pass appended exactly its own audit attempt (append-only audit)
    attempts = finalized(reconciliation_db, "run-26")
    assert [a.outcome for a in attempts] == [
        RepairOutcome.APPLIED,
        RepairOutcome.ALREADY_APPLIED,
        RepairOutcome.ALREADY_APPLIED,
    ]


# --- post-commit failures (review 1-3, 10, 11) --------------------------------


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


def seeded_candidate(paths, request_id, run_id):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, request_id)
    broker.submit_order(broker_request(request_id))
    record_run(QueryOnlyBroker(broker), paths, run_id=run_id)
    return broker


def test_a_clock_failure_after_the_commit_never_yields_failed(paths):
    """Review 1/10: clock calls are pass-start, per-attempt reservation and
    pass-finish. The finish call runs AFTER the commit -- its failure must
    not relabel the committed repair; the attempt is finalized APPLIED in
    the audit DB and returned on the run-level error."""
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-27", "run-27")
    clock = StepClock(fail_from=3)  # 1=start, 2=reserve, 3=finish

    with pytest.raises(ReconciliationRepairRunError) as captured:
        repair_service(QueryOnlyBroker(broker), paths, clock=clock).repair(run_id="run-27")

    attempt = only(captured.value.attempts)
    assert attempt.outcome is RepairOutcome.APPLIED
    assert order_state(execution_db, "exec-27")[0] == "filled"
    stored = only(finalized(reconciliation_db, "run-27"))
    assert stored.outcome is RepairOutcome.APPLIED  # audit and reality agree
    assert pending(reconciliation_db) == ()


def test_a_result_construction_failure_after_the_commit_never_yields_failed(paths):
    """Review 2/11: a finish clock that goes backwards makes the pass result
    unconstructible -- the committed, finalized attempt still reaches the
    caller machine-readably."""
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-28", "run-28")
    times = iter(
        [REPAIR_AT, REPAIR_AT, REPAIR_AT - timedelta(days=1)]  # finish < start
    )
    clock = lambda: next(times)  # noqa: E731

    with pytest.raises(ReconciliationRepairRunError, match="could not be built") as captured:
        repair_service(QueryOnlyBroker(broker), paths, clock=clock).repair(run_id="run-28")

    attempt = only(captured.value.attempts)
    assert attempt.outcome is RepairOutcome.APPLIED
    assert order_state(execution_db, "exec-28")[0] == "filled"
    assert only(finalized(reconciliation_db, "run-28")).outcome is RepairOutcome.APPLIED


def test_the_applied_attempt_never_depends_on_the_store_return_value(paths, monkeypatch):
    """Review 3: everything the APPLIED attempt needs is computed BEFORE the
    commit; a store returning garbage changes nothing."""
    execution_db, _ = paths
    broker = seeded_candidate(paths, "exec-29", "run-29")
    real = execution_store.record_order_result

    def returns_garbage(*args, **kwargs):
        real(*args, **kwargs)
        return None  # a caller reading the returned row would crash

    monkeypatch.setattr(execution_store, "record_order_result", returns_garbage)

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-29")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.APPLIED
    assert attempt.broker_order_id == "FAKE-1"
    assert order_state(execution_db, "exec-29") == ("filled", "FAKE-1")


# --- reservation / finalize / crash recovery (review 4-8, 32-40) --------------


def test_a_mid_update_store_failure_rolls_back_and_reports_failed(paths, monkeypatch):
    """Review 32: reservation succeeded, the state update failed pre-commit
    -- FAILED is accurate (the order DB is proven unchanged) and finalized."""
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-30", "run-30")
    before_bytes = execution_db.read_bytes()

    def boom(*args, **kwargs):
        raise execution_store.ExecutionIntegrityError("simulated pre-commit failure")

    monkeypatch.setattr(execution_store, "_validate_writeback", boom)

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-30")

    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.FAILED
    assert attempt.failure_code == "store_update_failed"
    assert "simulated" not in attempt.detail  # sanitized: no raw exception text
    monkeypatch.undo()
    assert execution_db.read_bytes() == before_bytes  # the whole txn rolled back
    assert order_state(execution_db, "exec-30")[0] == "unknown"
    assert only(finalized(reconciliation_db, "run-30")).outcome is RepairOutcome.FAILED
    assert pending(reconciliation_db) == ()


def test_a_finalize_failure_after_the_commit_is_recoverable_under_the_same_id(
    paths, monkeypatch
):
    """Review 4-8 / 35: the state update committed but the audit finalize
    failed. The attempt reports APPLIED + audit_error, the reservation stays
    pending, and recovery finalizes it -- APPLIED, under the ORIGINAL
    attempt id, from the STRUCTURED history provenance, without growing
    history."""
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-31", "run-31")

    import execution.broker_reconciliation_repair as repair_module

    def audit_down(*args, **kwargs):
        raise reconciliation_store.ReconciliationStoreError("audit DB unavailable")

    monkeypatch.setattr(repair_module, "finalize_repair_attempt", audit_down)
    first = repair_service(QueryOnlyBroker(broker), paths, prefix="crash").repair(run_id="run-31")
    monkeypatch.undo()

    attempt = only(first.attempts)
    assert attempt.outcome is RepairOutcome.APPLIED  # never FAILED: the commit is real
    assert attempt.audit_error is not None
    assert first.has_audit_failures is True
    assert order_state(execution_db, "exec-31")[0] == "filled"
    # the reservation is still pending (crash-equivalent state)
    assert [r.attempt_id for r in pending(reconciliation_db)] == ["crash-1"]
    assert finalized(reconciliation_db, "run-31") == ()
    history_before = execution_store.get_state_history("exec-31", db_path=execution_db)

    # restart: a NEW service instance recovers the pending attempt
    recovered = repair_service(QueryOnlyBroker(broker), paths, prefix="rec").recover_pending_attempts()

    entry = only(recovered)
    assert entry.attempt_id == "crash-1"  # the ORIGINAL id, not a new one
    assert entry.outcome is RepairOutcome.APPLIED
    assert entry.internal_state_after is OrderState.FILLED
    assert entry.applied_at_utc is not None
    assert pending(reconciliation_db) == ()
    stored = only(finalized(reconciliation_db, "run-31"))
    assert stored.attempt_id == "crash-1"
    assert stored.outcome is RepairOutcome.APPLIED
    # recovery reads only: history did not grow
    assert execution_store.get_state_history("exec-31", db_path=execution_db) == history_before
    # and a later repair pass converges without new history
    rerun = repair_service(QueryOnlyBroker(broker), paths, prefix="later").repair(run_id="run-31")
    assert only(rerun.attempts).outcome is RepairOutcome.ALREADY_APPLIED
    assert execution_store.get_state_history("exec-31", db_path=execution_db) == history_before


def test_a_crash_before_the_state_update_recovers_as_failed(paths, monkeypatch):
    """Review 36: the reservation exists but nothing ever committed -- the
    recovery finalizes it FAILED (reservation_not_applied); FAILED is
    accurate because the order DB is proven unchanged."""
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

    recovered = repair_service(QueryOnlyBroker(broker), paths).recover_pending_attempts()

    entry = only(recovered)
    assert entry.attempt_id == "crash-1"
    assert entry.outcome is RepairOutcome.FAILED
    assert entry.failure_code == "reservation_not_applied"
    assert pending(reconciliation_db) == ()
    assert order_state(execution_db, "exec-32")[0] == "unknown"


def test_concurrent_recovery_finalizes_each_pending_attempt_exactly_once(paths, monkeypatch):
    """Review 37/38: finalize is compare-and-set; a second recovery of the
    same pending attempt finds nothing left to do."""
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-33", "run-33")
    import execution.broker_reconciliation_repair as repair_module

    monkeypatch.setattr(
        repair_module,
        "finalize_repair_attempt",
        lambda *a, **k: (_ for _ in ()).throw(
            reconciliation_store.ReconciliationStoreError("audit down")
        ),
    )
    repair_service(QueryOnlyBroker(broker), paths, prefix="crash").repair(run_id="run-33")
    monkeypatch.undo()
    assert len(pending(reconciliation_db)) == 1

    service = repair_service(QueryOnlyBroker(broker), paths)
    first = service.recover_pending_attempts()
    second = service.recover_pending_attempts()

    assert [a.attempt_id for a in first] == ["crash-1"]
    assert second == ()  # nothing pending any more
    assert len(finalized(reconciliation_db, "run-33")) == 1


def test_recovery_refuses_a_tampered_or_deleted_source_observation(paths, monkeypatch):
    """Review 40: a pending reservation whose run/discrepancy was deleted or
    edited out-of-band is refused fail-closed, never guessed about."""
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
        repair_service(QueryOnlyBroker(broker), paths).recover_pending_attempts()

    assert len(pending(reconciliation_db)) == 1  # still pending, nothing invented


# --- pass-level failures (review 9, 12-16) ------------------------------------


def two_candidates(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    for request_id in ("exec-a", "exec-b"):
        seed_internal_unknown(execution_db, request_id)
        broker.submit_order(broker_request(request_id))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-two")
    return broker


def test_a_run_level_failure_on_the_second_target_keeps_the_first_commit(paths, monkeypatch):
    """Review 9/16: target one applies and finalizes; an unexpected
    (programmer-level) exception on target two stops the pass with the
    partial result attached AND retrievable from the audit DB; target two's
    reservation stays pending for recovery."""
    execution_db, reconciliation_db = paths
    broker = two_candidates(paths)
    real = execution_store.get_execution_order
    calls = {"n": 0}

    def breaks_on_second(request_id, **kwargs):
        calls["n"] += 1
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
    # the committed attempt is independently durable in the audit DB
    assert only(finalized(reconciliation_db, "run-two")).outcome is RepairOutcome.APPLIED
    # the interrupted attempt stays pending for recovery
    assert [r.target_id for r in pending(reconciliation_db)] == ["exec-b"]


class BrokenLogger:
    """A logger whose every emission path raises (review 12/13: handler and
    formatter failures are equivalent from the caller's perspective)."""

    def log(self, *args, **kwargs):
        raise RuntimeError("handler down")

    def info(self, *args, **kwargs):
        raise RuntimeError("handler down")

    def warning(self, *args, **kwargs):
        raise RuntimeError("formatter down")

    def error(self, *args, **kwargs):
        raise RuntimeError("handler down")


def test_a_logging_failure_never_changes_a_repair_outcome(paths, monkeypatch):
    """Review 12/13: logging is an aid; the audit DB is the authority."""
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-35", "run-35")
    import execution.broker_reconciliation_repair as repair_module

    monkeypatch.setattr(repair_module, "_LOGGER", BrokenLogger())

    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-35")

    assert only(result.attempts).outcome is RepairOutcome.APPLIED
    assert order_state(execution_db, "exec-35")[0] == "filled"
    assert only(finalized(reconciliation_db, "run-35")).outcome is RepairOutcome.APPLIED


def test_duplicate_attempt_ids_from_the_factory_are_refused_before_any_side_effect(paths):
    """Review 14: ids are generated and checked up front."""
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
    """Review 15."""
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
    """A second pass reusing an already persisted attempt id stops at the
    RESERVATION (before any state update)."""
    execution_db, reconciliation_db = paths
    broker = seeded_candidate(paths, "exec-36", "run-36")
    repair_service(QueryOnlyBroker(broker), paths, prefix="dup").repair(run_id="run-36")
    state_after_first = order_snapshot(execution_db, ("exec-36",))

    with pytest.raises(ReconciliationRepairRunError) as captured:
        repair_service(QueryOnlyBroker(broker), paths, prefix="dup").repair(run_id="run-36")

    assert isinstance(
        captured.value.__cause__, reconciliation_store.RepairAttemptConflictError
    )
    assert captured.value.attempts == ()
    assert order_snapshot(execution_db, ("exec-36",)) == state_after_first


# --- inputs, migration, structure ---------------------------------------------


def test_repair_acts_on_recorded_runs_only(paths):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-37")
    broker.submit_order(broker_request("exec-37"))
    # detect() alone records nothing
    BrokerOrderReconciliationService(
        broker=QueryOnlyBroker(broker),
        execution_db_path=execution_db,
        clock=lambda: OBSERVED_AT,
    ).detect(run_id="run-37")

    with pytest.raises(ReconciliationRepairError, match="does not exist"):
        repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-37")
    assert not reconciliation_db.exists()  # apply mode never creates the DB

    with pytest.raises(ReconciliationRepairError, match="not recorded"):
        repair_service(QueryOnlyBroker(broker), paths).repair(
            run_id="run-37", mode=RepairMode.DRY_RUN
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
        connection.execute(
            f"ALTER TABLE {reconciliation_store.DISCREPANCIES_TABLE} "
            "DROP COLUMN internal_revision"
        )
        connection.execute(
            f"PRAGMA user_version = {reconciliation_store._MIGRATABLE_VERSION}"
        )
        connection.commit()


def test_apply_mode_migrates_a_v1_audit_db_and_dry_run_refuses_it(paths):
    """Review 45: dry-run never migrates. A v1 observation migrated to v2
    has no revision, so the repair conservatively reports it stale."""
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-38")
    broker.submit_order(broker_request("exec-38"))
    record_run(QueryOnlyBroker(broker), paths, run_id="run-38")
    _downgrade_reconciliation_db_to_v1(reconciliation_db)
    v1_bytes = reconciliation_db.read_bytes()

    # dry-run never migrates: it refuses the v1 file and changes nothing
    with pytest.raises(
        reconciliation_store.ReconciliationSchemaError, match="init_reconciliation_db"
    ):
        repair_service(QueryOnlyBroker(broker), paths).repair(
            run_id="run-38", mode=RepairMode.DRY_RUN
        )
    assert reconciliation_db.read_bytes() == v1_bytes

    # apply mode migrates the existing file in place; the legacy record has
    # no revision -> conservative stale, and a FRESH run then applies
    result = repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-38")
    attempt = only(result.attempts)
    assert attempt.outcome is RepairOutcome.STALE
    assert attempt.failure_code == "missing_observed_revision"
    record_run(QueryOnlyBroker(broker), paths, run_id="run-38-fresh")
    fresh = repair_service(QueryOnlyBroker(broker), paths, prefix="fresh").repair(
        run_id="run-38-fresh"
    )
    assert only(fresh.attempts).outcome is RepairOutcome.APPLIED
    assert order_state(execution_db, "exec-38")[0] == "filled"


def test_repair_never_submits_or_cancels_through_the_broker(paths):
    """Review 47: driven with the RAW FakeBroker (not the poisoned facade)
    so the recorded call lists are the evidence."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-39")
    broker.submit_order(broker_request("exec-39"))
    record_run(broker, paths, run_id="run-39")
    submits, cancels = len(broker.submitted_requests), len(broker.cancel_requests)

    result = repair_service(broker, paths).repair(run_id="run-39")
    repair_service(broker, paths).recover_pending_attempts()

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
    broker = seeded_candidate(paths, "exec-40", "run-40")

    with caplog.at_level("INFO", logger="execution.broker_reconciliation_repair"):
        repair_service(QueryOnlyBroker(broker), paths).repair(run_id="run-40")

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "exec-40" in messages
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
