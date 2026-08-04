"""Issue #103: broker reconciliation -- difference DETECTION only.

Everything runs against ``tmp_path`` SQLite files and an in-memory
FakeBroker: no network, no Discord, no S3, no sleep, no real orders.

The reconciliation service is driven through :class:`QueryOnlyBroker`, a
facade whose ``submit_order`` / ``cancel_order`` are POISONED -- so every
test in this file simultaneously pins that reconciliation never writes
through the broker. The broker-side fixtures themselves are seeded on the
FakeBroker BEFORE reconciliation starts (that is the state we reconcile
against), which is why this file is listed in the submission-boundary
allow-list.
"""

import ast
import pathlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from broker.broker_models import (
    BrokerCommunicationError,
    BrokerAuthenticationError,
    BrokerInvalidRequestError,
    BrokerOrderNotFoundError,
    BrokerRateLimitError,
    Fill,
    BrokerTimeoutError,
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
    TimeoutOrder,
)
from execution import execution_store, reconciliation_store
from execution.broker_reconciliation import BrokerOrderReconciliationService
from execution.execution_models import ExecutionRequest
from execution.reconciliation_models import (
    DiscrepancyType,
    ReconciliationMode,
    ReconciliationTargetType,
    ResolutionCategory,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# The internal records are older than FakeBroker's deterministic clock base
# (2026-01-01), so a broker observation never predates the stored state.
INTERNAL_T = datetime(2025, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
OBSERVED_AT = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
FILL_PRICE = Decimal("2400")
QUANTITY = Decimal("10")


def client_id(request_id: str) -> str:
    return f"client-{request_id}"


class QueryOnlyBroker:
    """Query-only Broker facade over a FakeBroker (test double, in-memory).

    ``submit_order`` / ``cancel_order`` raise: reconciliation must never
    reach a broker WRITE operation, and a poisoned facade proves it for
    every test rather than only for a dedicated one. ``get_order`` can be
    scripted to fail with a typed ``BrokerError`` -- FakeBroker has no
    query-failure scripting and Issue #103 deliberately does not widen its
    responsibilities to add one.
    """

    def __init__(self, inner: FakeBroker, *, query_failures: dict | None = None):
        self._inner = inner
        self._query_failures = dict(query_failures or {})

    def submit_order(self, request):  # pragma: no cover - must never run
        raise AssertionError("reconciliation must never submit an order")

    def cancel_order(self, client_order_id):  # pragma: no cover - must never run
        raise AssertionError("reconciliation must never cancel an order")

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


class NonListingBroker:
    """A Broker that does NOT implement ``OrderListingBroker`` -- like the
    OANDA Demo Adapter today: it satisfies the five Issue #73 operations but
    cannot enumerate broker-side orders."""

    def __init__(self, inner: FakeBroker):
        self._inner = inner

    def submit_order(self, request):  # pragma: no cover - must never run
        raise AssertionError("reconciliation must never submit an order")

    def cancel_order(self, client_order_id):  # pragma: no cover - must never run
        raise AssertionError("reconciliation must never cancel an order")

    def get_order(self, client_order_id):
        return self._inner.get_order(client_order_id)

    def get_positions(self):
        return self._inner.get_positions()

    def get_account_state(self):
        return self._inner.get_account_state()


class SnapshotBroker(QueryOnlyBroker):
    """Return selected external snapshots without mutating listing fixtures."""

    def __init__(self, inner: FakeBroker, snapshots: dict[str, OrderResult]):
        super().__init__(inner)
        self._snapshots = snapshots

    def get_order(self, client_order_id):
        return self._snapshots.get(client_order_id, self._inner.get_order(client_order_id))


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "execution_orders.sqlite3", tmp_path / "broker_reconciliation.sqlite3"


def service(broker, paths, *, with_db=True):
    execution_db, reconciliation_db = paths
    return BrokerOrderReconciliationService(
        broker=broker,
        execution_db_path=execution_db,
        reconciliation_db_path=reconciliation_db if with_db else None,
        clock=lambda: OBSERVED_AT,
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
    """The broker-boundary request for a fixture order.

    Built directly (not via ``ExecutionRequest.to_order_request``) so this
    fixture does not touch the sanctioned submission conversion path pinned
    by ``test_submission_boundary.py``.
    """
    return OrderRequest(
        client_order_id=client_id(request_id),
        symbol="XAUUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=quantity,
        created_at_utc=INTERNAL_T,
    )


def seed_internal_pending(db_path, request_id: str, *, quantity: Decimal = QUANTITY) -> None:
    """Internal state after a submit attempt started (phase submitting /
    order state PENDING) -- e.g. the process died before the result was
    persisted."""
    execution_store.create_execution_order(
        make_request(request_id, quantity=quantity),
        client_order_id=client_id(request_id),
        created_at_utc=INTERNAL_T,
        db_path=db_path,
    )
    execution_store.mark_submitting(
        request_id, reason="submitting", changed_at_utc=INTERNAL_T, db_path=db_path
    )


def seed_internal_unknown(db_path, request_id: str) -> None:
    """Internal UNKNOWN: the outcome was never confirmed (e.g. timeout)."""
    seed_internal_pending(db_path, request_id)
    execution_store.record_order_state(
        request_id,
        OrderState.UNKNOWN,
        reason="broker outcome unknown",
        changed_at_utc=INTERNAL_T + timedelta(seconds=1),
        db_path=db_path,
    )


def accepted_result(request_id: str, broker_order_id: str, *, seconds: int = 1) -> OrderResult:
    return OrderResult(
        client_order_id=client_id(request_id),
        broker_order_id=broker_order_id,
        state=OrderState.ACCEPTED,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        requested_quantity=QUANTITY,
        filled_quantity=Decimal("0"),
        remaining_quantity=QUANTITY,
        requested_price=None,
        average_fill_price=None,
        updated_at_utc=INTERNAL_T + timedelta(seconds=seconds),
    )


def seed_internal_accepted(db_path, request_id: str, result: OrderResult) -> None:
    seed_internal_pending(db_path, request_id)
    execution_store.record_order_result(
        request_id, result, reason="broker accepted", db_path=db_path
    )


def filled_result(request_id: str, broker_order_id: str, *, seconds: int = 1) -> OrderResult:
    return OrderResult(
        client_order_id=client_id(request_id),
        broker_order_id=broker_order_id,
        state=OrderState.FILLED,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        requested_quantity=QUANTITY,
        filled_quantity=QUANTITY,
        remaining_quantity=Decimal("0"),
        requested_price=None,
        average_fill_price=FILL_PRICE,
        updated_at_utc=INTERNAL_T + timedelta(seconds=seconds),
        fills=(
            Fill(
                fill_id=f"fill-{request_id}",
                price=FILL_PRICE,
                quantity=QUANTITY,
                filled_at_utc=INTERNAL_T + timedelta(seconds=seconds),
            ),
        ),
    )


def only(discrepancies):
    assert len(discrepancies) == 1, discrepancies
    return discrepancies[0]


# --- order-state comparison ---------------------------------------------------


def test_internal_submitting_but_broker_filled_is_an_auto_repair_candidate(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-1")
    broker.submit_order(broker_request("exec-1"))  # broker-side fixture: FILLED

    run = service(QueryOnlyBroker(broker), paths).detect(run_id="run-1")

    entry = only(run.discrepancies)
    assert entry.discrepancy_type is DiscrepancyType.IN_FLIGHT_BROKER_FILLED
    assert entry.target_type is ReconciliationTargetType.EXECUTION_ORDER
    assert entry.target_id == "exec-1"
    assert entry.client_order_id == client_id("exec-1")
    assert (entry.internal_state, entry.broker_state) == (OrderState.PENDING, OrderState.FILLED)
    assert entry.resolution_category is ResolutionCategory.AUTO_REPAIR_CANDIDATE
    assert run.checked_order_count == 1
    assert run.mode is ReconciliationMode.DETECTION_ONLY


def test_internal_accepted_but_broker_cancelled_is_an_auto_repair_candidate(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-2"), StayPending(broker_order_id="B-2"))
    accepted = broker.submit_order(broker_request("exec-2"))
    seed_internal_accepted(execution_db, "exec-2", accepted)
    broker.cancel_order(client_id("exec-2"))  # broker-side fixture: CANCELLED

    run = service(QueryOnlyBroker(broker), paths).detect(run_id="run-2")

    entry = only(run.discrepancies)
    assert entry.discrepancy_type is DiscrepancyType.IN_FLIGHT_BROKER_CANCELLED
    assert (entry.internal_state, entry.broker_state) == (OrderState.ACCEPTED, OrderState.CANCELLED)
    assert entry.resolution_category is ResolutionCategory.AUTO_REPAIR_CANDIDATE
    assert entry.broker_order_id == "B-2"


def test_internal_accepted_but_broker_filled_is_an_auto_repair_candidate(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-accepted-fill"), StayPending(broker_order_id="B-AF"))
    accepted = broker.submit_order(broker_request("exec-accepted-fill"))
    seed_internal_accepted(execution_db, "exec-accepted-fill", accepted)
    fill_entry = Fill(
        fill_id="F-AF",
        price=FILL_PRICE,
        quantity=QUANTITY,
        filled_at_utc=accepted.updated_at_utc + timedelta(seconds=1),
    )
    filled = replace(
        accepted,
        state=OrderState.FILLED,
        filled_quantity=QUANTITY,
        remaining_quantity=Decimal("0"),
        average_fill_price=FILL_PRICE,
        fills=(fill_entry,),
        updated_at_utc=accepted.updated_at_utc + timedelta(seconds=1),
    )

    run = service(
        SnapshotBroker(broker, {client_id("exec-accepted-fill"): filled}), paths
    ).detect(run_id="run-accepted-fill")

    entry = only(run.discrepancies)
    assert entry.discrepancy_type is DiscrepancyType.IN_FLIGHT_BROKER_FILLED
    assert entry.resolution_category is ResolutionCategory.AUTO_REPAIR_CANDIDATE


def test_internal_accepted_but_broker_rejected_needs_manual_review(paths):
    """The broker's REJECTED snapshot carries no broker_order_id, so
    reflecting it would drop the id we already know -- not something the
    existing store contract can absorb."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-3"), RejectOrder(reason="insufficient margin"))
    broker.submit_order(broker_request("exec-3"))  # broker-side fixture: REJECTED
    seed_internal_accepted(execution_db, "exec-3", accepted_result("exec-3", "B-3"))

    run = service(QueryOnlyBroker(broker), paths).detect(run_id="run-3")

    entry = only(run.discrepancies)
    assert entry.discrepancy_type is DiscrepancyType.IN_FLIGHT_BROKER_REJECTED
    assert (entry.internal_state, entry.broker_state) == (OrderState.ACCEPTED, OrderState.REJECTED)
    assert entry.resolution_category is ResolutionCategory.MANUAL_REVIEW
    assert "broker_order_id" in entry.detail


@pytest.mark.parametrize(
    "request_id, script, expected_state",
    [
        ("exec-u1", None, OrderState.FILLED),
        ("exec-u2", StayPending(broker_order_id="B-U2"), OrderState.CANCELLED),
        ("exec-u3", RejectOrder(reason="rejected by broker"), OrderState.REJECTED),
    ],
)
def test_internal_unknown_but_broker_settled_is_detected(paths, request_id, script, expected_state):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    if script is not None:
        broker.script_order(client_id(request_id), script)
    broker.submit_order(broker_request(request_id))
    if expected_state is OrderState.CANCELLED:
        broker.cancel_order(client_id(request_id))
    seed_internal_unknown(execution_db, request_id)

    run = service(QueryOnlyBroker(broker), paths).detect(run_id="run-unknown")

    entry = only(run.discrepancies)
    assert entry.discrepancy_type is DiscrepancyType.UNKNOWN_BROKER_SETTLED
    assert entry.internal_state is OrderState.UNKNOWN
    assert entry.broker_state is expected_state
    assert entry.resolution_category is ResolutionCategory.AUTO_REPAIR_CANDIDATE


def test_matching_states_are_not_discrepancies(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-4"), StayPending(broker_order_id="B-4"))
    accepted = broker.submit_order(broker_request("exec-4"))
    seed_internal_accepted(execution_db, "exec-4", accepted)

    run = service(QueryOnlyBroker(broker), paths).detect(run_id="run-4")

    assert run.discrepancies == ()
    assert run.checked_order_count == 1
    assert run.broker_orders_enumerated is True


def test_same_state_with_a_different_broker_order_id_is_detected(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-id-mismatch"), StayPending(broker_order_id="B-ORIGINAL"))
    accepted = broker.submit_order(broker_request("exec-id-mismatch"))
    seed_internal_accepted(execution_db, "exec-id-mismatch", accepted)
    conflicting = replace(
        accepted,
        broker_order_id="B-FOREIGN",
        updated_at_utc=accepted.updated_at_utc + timedelta(seconds=1),
    )

    run = service(
        SnapshotBroker(broker, {client_id("exec-id-mismatch"): conflicting}), paths
    ).detect(run_id="run-id-mismatch")

    entry = only(run.discrepancies)
    assert entry.discrepancy_type is DiscrepancyType.STATE_MISMATCH
    assert entry.resolution_category is ResolutionCategory.MANUAL_REVIEW
    assert "broker_order_id" in entry.detail


def test_same_state_with_a_different_order_identity_is_detected(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-shape-mismatch"), StayPending(broker_order_id="B-SHAPE"))
    accepted = broker.submit_order(broker_request("exec-shape-mismatch"))
    seed_internal_accepted(execution_db, "exec-shape-mismatch", accepted)
    conflicting = replace(
        accepted,
        requested_quantity=Decimal("5"),
        remaining_quantity=Decimal("5"),
        updated_at_utc=accepted.updated_at_utc + timedelta(seconds=1),
    )

    run = service(
        SnapshotBroker(broker, {client_id("exec-shape-mismatch"): conflicting}), paths
    ).detect(run_id="run-shape-mismatch")

    entry = only(run.discrepancies)
    assert entry.discrepancy_type is DiscrepancyType.STATE_MISMATCH
    assert entry.resolution_category is ResolutionCategory.MANUAL_REVIEW
    assert "quantity" in entry.detail


def test_conflicting_terminal_states_are_reconciled_for_manual_review(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-5"), StayPending(broker_order_id="B-5"))
    accepted = broker.submit_order(broker_request("exec-5"))
    seed_internal_accepted(execution_db, "exec-5", accepted)
    cancelled = broker.cancel_order(client_id("exec-5"))
    execution_store.record_order_result("exec-5", cancelled, reason="cancelled", db_path=execution_db)
    rejected = replace(
        cancelled,
        state=OrderState.REJECTED,
        reject_reason="broker now reports rejection",
        updated_at_utc=cancelled.updated_at_utc + timedelta(seconds=1),
    )

    run = service(SnapshotBroker(broker, {client_id("exec-5"): rejected}), paths).detect(
        run_id="run-5"
    )

    entry = only(run.discrepancies)
    assert run.checked_order_count == 1
    assert entry.discrepancy_type is DiscrepancyType.STATE_MISMATCH
    assert entry.internal_state is OrderState.CANCELLED
    assert entry.broker_state is OrderState.REJECTED
    assert entry.resolution_category is ResolutionCategory.MANUAL_REVIEW


def test_partially_filled_then_broker_cancelled_is_auto_repair_candidate(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(
        client_id("exec-part-cancel"),
        PartiallyFill(fills=((FILL_PRICE, Decimal("3")),), broker_order_id="B-PC"),
    )
    partial = broker.submit_order(broker_request("exec-part-cancel"))
    seed_internal_pending(execution_db, "exec-part-cancel")
    execution_store.record_order_result(
        "exec-part-cancel", partial, reason="partial", db_path=execution_db
    )
    broker.cancel_order(client_id("exec-part-cancel"))

    run = service(QueryOnlyBroker(broker), paths).detect(run_id="run-part-cancel")

    entry = only(run.discrepancies)
    assert entry.discrepancy_type is DiscrepancyType.IN_FLIGHT_BROKER_CANCELLED
    assert entry.internal_state is OrderState.PARTIALLY_FILLED
    assert entry.resolution_category is ResolutionCategory.AUTO_REPAIR_CANDIDATE


def test_unknown_on_both_sides_remains_visible_as_undetermined(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-unknown-still")
    try:
        broker.script_order(client_id("exec-unknown-still"), TimeoutOrder())
        broker.submit_order(broker_request("exec-unknown-still"))
    except BrokerTimeoutError:
        pass

    run = service(QueryOnlyBroker(broker), paths).detect(run_id="run-unknown-still")

    entry = only(run.discrepancies)
    assert entry.discrepancy_type is DiscrepancyType.STATE_MISMATCH
    assert entry.internal_state is OrderState.UNKNOWN
    assert entry.broker_state is OrderState.UNKNOWN
    assert entry.resolution_category is ResolutionCategory.MANUAL_REVIEW
    assert "still report UNKNOWN" in entry.detail


def test_partially_filled_then_broker_filled_without_fill_prefix_needs_manual_review(paths):
    execution_db, _ = paths
    internal_broker = FakeBroker(default_fill_price=FILL_PRICE)
    internal_broker.script_order(
        client_id("exec-part-fill"),
        PartiallyFill(fills=((FILL_PRICE, Decimal("3")),), broker_order_id="B-PF"),
    )
    partial = internal_broker.submit_order(broker_request("exec-part-fill"))
    seed_internal_pending(execution_db, "exec-part-fill")
    execution_store.record_order_result(
        "exec-part-fill", partial, reason="partial", db_path=execution_db
    )

    external_broker = FakeBroker(default_fill_price=FILL_PRICE)
    external_broker.script_order(
        client_id("exec-part-fill"),
        FillImmediately(price=FILL_PRICE, broker_order_id="B-PF"),
    )
    filled = external_broker.submit_order(broker_request("exec-part-fill"))

    run = service(
        SnapshotBroker(external_broker, {client_id("exec-part-fill"): filled}), paths
    ).detect(run_id="run-part-fill")

    entry = only(run.discrepancies)
    assert entry.discrepancy_type is DiscrepancyType.IN_FLIGHT_BROKER_FILLED
    assert entry.internal_state is OrderState.PARTIALLY_FILLED
    assert entry.broker_state is OrderState.FILLED
    # FakeBroker's independently built full-fill snapshot does not preserve
    # the internally anchored partial fill prefix, so it is correctly manual.
    assert entry.resolution_category is ResolutionCategory.MANUAL_REVIEW


def test_partially_filled_then_broker_filled_with_fill_prefix_is_auto_candidate(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(
        client_id("exec-part-fill-prefix"),
        PartiallyFill(fills=((FILL_PRICE, Decimal("3")),), broker_order_id="B-PFP"),
    )
    partial = broker.submit_order(broker_request("exec-part-fill-prefix"))
    seed_internal_pending(execution_db, "exec-part-fill-prefix")
    execution_store.record_order_result(
        "exec-part-fill-prefix", partial, reason="partial", db_path=execution_db
    )
    remaining_fill = Fill(
        fill_id="F-PFP-2",
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

    run = service(
        SnapshotBroker(broker, {client_id("exec-part-fill-prefix"): filled}), paths
    ).detect(run_id="run-part-fill-prefix")

    entry = only(run.discrepancies)
    assert entry.discrepancy_type is DiscrepancyType.IN_FLIGHT_BROKER_FILLED
    assert entry.resolution_category is ResolutionCategory.AUTO_REPAIR_CANDIDATE


def test_a_broker_snapshot_for_another_order_is_never_adopted(paths):
    """The broker reports a different requested quantity: the snapshot does
    not describe the stored intent, so it must not be reflected onto it."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-6")
    broker.submit_order(broker_request("exec-6", quantity=Decimal("5")))

    run = service(QueryOnlyBroker(broker), paths).detect(run_id="run-6")

    entry = only(run.discrepancies)
    assert entry.discrepancy_type is DiscrepancyType.IN_FLIGHT_BROKER_FILLED
    assert entry.resolution_category is ResolutionCategory.MANUAL_REVIEW
    assert "quantity" in entry.detail


def test_an_order_the_broker_does_not_know_is_flagged_for_review(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-7")  # never reached the broker

    run = service(QueryOnlyBroker(broker), paths).detect(run_id="run-7")

    entry = only(run.discrepancies)
    assert entry.discrepancy_type is DiscrepancyType.BROKER_ORDER_MISSING
    assert entry.resolution_category is ResolutionCategory.MANUAL_REVIEW
    assert entry.broker_state is None
    assert entry.internal_state is OrderState.PENDING


# --- broker_order_id uniqueness (Issue #103 review, Major) --------------------


@pytest.mark.parametrize("internal_state", [OrderState.PENDING, OrderState.UNKNOWN])
def test_a_broker_id_owned_by_another_request_is_never_an_auto_repair_candidate(
    paths, internal_state
):
    """``broker_order_id`` is UNIQUE DB-wide: recording this observation
    would collide with the owning request and roll back, so it must not be
    classified as repairable."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    # A owns B-1 and is settled (not itself a reconciliation target)
    seed_internal_pending(execution_db, "exec-owner")
    execution_store.record_order_result(
        "exec-owner", filled_result("exec-owner", "B-1"), reason="filled",
        db_path=execution_db,
    )
    # B has no broker id yet, and the broker reports B-1 for it
    if internal_state is OrderState.PENDING:
        seed_internal_pending(execution_db, "exec-clash")
    else:
        seed_internal_unknown(execution_db, "exec-clash")
    broker.script_order(
        client_id("exec-clash"), FillImmediately(price=FILL_PRICE, broker_order_id="B-1")
    )
    broker.submit_order(broker_request("exec-clash"))
    before = _order_snapshot(execution_db, ("exec-owner", "exec-clash"))

    run = service(QueryOnlyBroker(broker), paths).detect(run_id="run-clash")

    entry = only([d for d in run.discrepancies if d.target_id == "exec-clash"])
    assert entry.internal_state is internal_state
    assert entry.broker_state is OrderState.FILLED
    assert entry.resolution_category is ResolutionCategory.MANUAL_REVIEW
    # the reason names the id, its current owner and the request being compared
    assert "B-1" in entry.detail
    assert "exec-owner" in entry.detail
    assert "exec-clash" in entry.detail
    # neither side changed
    assert _order_snapshot(execution_db, ("exec-owner", "exec-clash")) == before


def test_an_unassigned_broker_id_stays_an_auto_repair_candidate(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-owner")
    execution_store.record_order_result(
        "exec-owner", filled_result("exec-owner", "B-1"), reason="filled",
        db_path=execution_db,
    )
    seed_internal_pending(execution_db, "exec-free")
    broker.script_order(
        client_id("exec-free"), FillImmediately(price=FILL_PRICE, broker_order_id="B-2")
    )
    broker.submit_order(broker_request("exec-free"))

    run = service(QueryOnlyBroker(broker), paths).detect(run_id="run-free")

    entry = only([d for d in run.discrepancies if d.target_id == "exec-free"])
    assert entry.resolution_category is ResolutionCategory.AUTO_REPAIR_CANDIDATE
    assert entry.broker_order_id == "B-2"


def test_an_order_owning_the_broker_id_itself_is_not_a_conflict(paths):
    """The normal re-observation case: the id the broker reports is the one
    this very request already holds."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-self"), StayPending(broker_order_id="B-SELF"))
    accepted = broker.submit_order(broker_request("exec-self"))
    seed_internal_accepted(execution_db, "exec-self", accepted)
    broker.cancel_order(client_id("exec-self"))

    run = service(QueryOnlyBroker(broker), paths).detect(run_id="run-self")

    entry = only(run.discrepancies)
    assert entry.broker_order_id == "B-SELF"
    assert entry.resolution_category is ResolutionCategory.AUTO_REPAIR_CANDIDATE


def test_an_observation_without_a_broker_id_keeps_its_existing_classification(paths):
    """A REJECTED snapshot carries no broker id at all: nothing can collide,
    and the classification is unchanged by the uniqueness check."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-noid")
    broker.script_order(client_id("exec-noid"), RejectOrder(reason="rejected by broker"))
    broker.submit_order(broker_request("exec-noid"))

    run = service(QueryOnlyBroker(broker), paths).detect(run_id="run-noid")

    entry = only(run.discrepancies)
    assert entry.broker_order_id is None
    assert entry.resolution_category is ResolutionCategory.AUTO_REPAIR_CANDIDATE


def test_a_failed_owner_lookup_falls_back_to_manual_review(paths, monkeypatch):
    """Fail-closed: if the execution store cannot answer who owns the id,
    repairability is not assumed."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-readfail")
    broker.submit_order(broker_request("exec-readfail"))

    def boom(*args, **kwargs):
        raise execution_store.ExecutionIntegrityError("order store unreadable")

    monkeypatch.setattr(execution_store, "get_execution_order_by_broker_order_id", boom)
    before = _order_snapshot(execution_db, ("exec-readfail",))

    run = service(QueryOnlyBroker(broker), paths).detect(run_id="run-readfail")

    entry = only(run.discrepancies)
    assert entry.discrepancy_type is DiscrepancyType.IN_FLIGHT_BROKER_FILLED
    assert entry.resolution_category is ResolutionCategory.MANUAL_REVIEW
    assert "ExecutionIntegrityError" in entry.detail
    assert _order_snapshot(execution_db, ("exec-readfail",)) == before


# --- broker-only orders -------------------------------------------------------


def test_an_order_only_the_broker_holds_is_reported_for_manual_review(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.submit_order(broker_request("stranger"))  # no execution request exists
    seed_internal_pending(execution_db, "exec-8")
    broker.submit_order(broker_request("exec-8"))

    run = service(QueryOnlyBroker(broker), paths).detect(run_id="run-8")

    broker_only = [
        d for d in run.discrepancies if d.discrepancy_type is DiscrepancyType.BROKER_ONLY_ORDER
    ]
    entry = only(broker_only)
    assert entry.target_type is ReconciliationTargetType.BROKER_ORDER
    assert entry.target_id == client_id("stranger")
    assert entry.internal_state is None
    assert entry.broker_state is OrderState.FILLED
    assert entry.resolution_category is ResolutionCategory.MANUAL_REVIEW


def test_a_settled_internal_order_is_not_reported_as_broker_only(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    result = broker.submit_order(broker_request("exec-9"))
    seed_internal_pending(execution_db, "exec-9")
    execution_store.record_order_result("exec-9", result, reason="filled", db_path=execution_db)

    run = service(QueryOnlyBroker(broker), paths).detect(run_id="run-9")

    assert run.discrepancies == ()


def test_a_broker_without_enumeration_reports_the_gap_instead_of_none_found(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-10")
    broker.submit_order(broker_request("exec-10"))

    run = service(NonListingBroker(broker), paths).detect(run_id="run-10")

    assert run.broker_orders_enumerated is False
    assert "OrderListingBroker" in run.broker_listing_error
    assert not [
        d for d in run.discrepancies if d.discrepancy_type is DiscrepancyType.BROKER_ONLY_ORDER
    ]
    # the per-order comparison still happened
    assert run.checked_order_count == 1


def test_a_failing_enumeration_is_recorded_on_the_run(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)

    class ListingFailsBroker(QueryOnlyBroker):
        def list_orders(self):
            raise BrokerCommunicationError("listing unavailable")

    seed_internal_pending(execution_db, "exec-11")
    broker.submit_order(broker_request("exec-11"))

    run = service(ListingFailsBroker(broker), paths).detect(run_id="run-11")

    assert run.broker_orders_enumerated is False
    assert "BrokerCommunicationError" in run.broker_listing_error
    assert only(run.discrepancies).discrepancy_type is DiscrepancyType.IN_FLIGHT_BROKER_FILLED


def test_duplicate_broker_listing_is_ambiguous_not_two_discrepancies(paths):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    observed = broker.submit_order(broker_request("duplicate-listing"))

    class DuplicateListingBroker(QueryOnlyBroker):
        def list_orders(self):
            return (observed, observed)

    run = service(DuplicateListingBroker(broker), paths).detect(run_id="run-duplicate-listing")

    assert run.broker_orders_enumerated is False
    assert "duplicate client_order_id" in run.broker_listing_error
    assert run.discrepancies == ()


# --- query failures -----------------------------------------------------------


@pytest.mark.parametrize(
    "error, expected_code",
    [
        (BrokerTimeoutError("timed out waiting for the broker"), "timeout"),
        (BrokerCommunicationError("connection refused"), "communication_failure"),
        (BrokerAuthenticationError("credentials rejected"), "authentication_failure"),
        (BrokerRateLimitError("slow down"), "rate_limited"),
    ],
)
def test_a_failed_broker_query_is_recorded_without_inferring_a_state(paths, error, expected_code):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-12")
    broker.submit_order(broker_request("exec-12"))
    facade = QueryOnlyBroker(broker, query_failures={client_id("exec-12"): error})

    before = execution_store.get_execution_order("exec-12", db_path=execution_db)
    run = service(facade, paths).detect(run_id="run-12")

    entry = only(run.discrepancies)
    assert entry.discrepancy_type is DiscrepancyType.QUERY_FAILURE
    assert entry.resolution_category is ResolutionCategory.QUERY_FAILED
    assert entry.target_id == "exec-12"
    assert entry.broker_error_code.value == expected_code
    assert entry.broker_state is None  # nothing was observed, nothing is inferred
    assert entry.internal_state is OrderState.UNKNOWN
    assert run.has_partial_failures is True
    assert execution_store.get_execution_order("exec-12", db_path=execution_db) == before


def test_one_failing_query_does_not_hide_the_other_observations(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    for request_id in ("exec-13", "exec-14"):
        seed_internal_pending(execution_db, request_id)
        broker.submit_order(broker_request(request_id))
    facade = QueryOnlyBroker(
        broker, query_failures={client_id("exec-13"): BrokerTimeoutError("timeout")}
    )

    run = service(facade, paths).detect(run_id="run-13")

    kinds = {d.target_id: d.discrepancy_type for d in run.discrepancies}
    assert kinds == {
        "exec-13": DiscrepancyType.QUERY_FAILURE,
        "exec-14": DiscrepancyType.IN_FLIGHT_BROKER_FILLED,
    }
    assert run.checked_order_count == 2


def test_unexpected_adapter_exception_is_recorded_and_other_orders_continue(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    for request_id in ("exec-runtime-error", "exec-still-checked"):
        seed_internal_pending(execution_db, request_id)
        broker.submit_order(broker_request(request_id))
    facade = QueryOnlyBroker(
        broker,
        query_failures={client_id("exec-runtime-error"): RuntimeError("adapter bug")},
    )

    run = service(facade, paths).run(run_id="run-runtime-error")

    by_target = {entry.target_id: entry for entry in run.discrepancies}
    failed = by_target["exec-runtime-error"]
    assert failed.discrepancy_type is DiscrepancyType.QUERY_FAILURE
    assert failed.broker_error_code.value == "communication_failure"
    assert "RuntimeError" in failed.detail
    assert by_target["exec-still-checked"].broker_state is OrderState.FILLED
    assert run.has_partial_failures is True
    assert (
        reconciliation_store.get_reconciliation_run("run-runtime-error", db_path=paths[1])
        == run
    )


def test_malformed_adapter_return_is_recorded_as_a_query_failure(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-malformed")
    broker.submit_order(broker_request("exec-malformed"))

    class MalformedQueryBroker(QueryOnlyBroker):
        def get_order(self, client_order_id):
            return {"state": "filled"}

    run = service(MalformedQueryBroker(broker), paths).detect(run_id="run-malformed")

    entry = only(run.discrepancies)
    assert entry.discrepancy_type is DiscrepancyType.QUERY_FAILURE
    assert entry.broker_error_code.value == "communication_failure"
    assert "TypeError" in entry.detail


# --- read-only / no-side-effect guarantees ------------------------------------


def _order_snapshot(db_path, request_ids):
    return {
        request_id: (
            execution_store.get_execution_order(request_id, db_path=db_path),
            execution_store.get_state_history(request_id, db_path=db_path),
        )
        for request_id in request_ids
    }


def test_repeated_runs_never_change_internal_order_state(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    request_ids = ("exec-15", "exec-16", "exec-17")
    seed_internal_pending(execution_db, "exec-15")
    broker.submit_order(broker_request("exec-15"))  # filled broker-side
    seed_internal_unknown(execution_db, "exec-16")
    broker.script_order(client_id("exec-16"), RejectOrder(reason="no"))
    broker.submit_order(broker_request("exec-16"))
    seed_internal_pending(execution_db, "exec-17")  # unknown to the broker

    reconciler = service(QueryOnlyBroker(broker), paths)
    before = _order_snapshot(execution_db, request_ids)
    execution_bytes = execution_db.read_bytes()

    first = reconciler.run(run_id="run-a")
    second = reconciler.run(run_id="run-b")
    third = reconciler.detect(run_id="run-c")

    assert _order_snapshot(execution_db, request_ids) == before
    assert execution_db.read_bytes() == execution_bytes
    # identical observations every time (the run id is the only difference)
    assert first.discrepancies == second.discrepancies == third.discrepancies
    assert {d.target_id for d in first.discrepancies} == set(request_ids)


def test_reconciliation_never_submits_or_cancels_through_the_broker(paths):
    """Driven with the RAW FakeBroker (not the poisoned facade) so the
    recorded call lists are the evidence."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.script_order(client_id("exec-18"), StayPending(broker_order_id="B-18"))
    accepted = broker.submit_order(broker_request("exec-18"))
    seed_internal_accepted(execution_db, "exec-18", accepted)
    broker.cancel_order(client_id("exec-18"))
    broker.submit_order(broker_request("stranger"))
    submits, cancels = len(broker.submitted_requests), len(broker.cancel_requests)

    run = service(broker, paths).run(run_id="run-18")

    assert len(broker.submitted_requests) == submits
    assert len(broker.cancel_requests) == cancels
    assert run.discrepancies  # the run really did observe something


def test_detect_does_not_create_the_reconciliation_db(paths):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-19")
    broker.submit_order(broker_request("exec-19"))

    service(QueryOnlyBroker(broker), paths).detect(run_id="run-19")

    assert not reconciliation_db.exists()


def test_run_requires_a_reconciliation_db_path(paths):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    reconciler = service(QueryOnlyBroker(broker), paths, with_db=False)
    with pytest.raises(ValueError, match="reconciliation_db_path"):
        reconciler.run(run_id="run-20")


def test_the_service_refuses_a_non_broker_and_a_broken_clock(paths):
    execution_db, reconciliation_db = paths
    with pytest.raises(ValueError, match="Broker protocol"):
        BrokerOrderReconciliationService(
            broker=object(), execution_db_path=execution_db,
        )
    with pytest.raises(ValueError, match="clock"):
        BrokerOrderReconciliationService(
            broker=FakeBroker(), execution_db_path=execution_db, clock="not-callable",
        )
    naive = BrokerOrderReconciliationService(
        broker=FakeBroker(), execution_db_path=execution_db,
        clock=lambda: datetime(2026, 2, 1),
    )
    with pytest.raises(ValueError, match="clock"):
        naive.detect(run_id="run-21")


def test_reconciliation_of_an_empty_or_missing_execution_db_is_a_no_op(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)

    run = service(QueryOnlyBroker(broker), paths).detect(run_id="run-22")

    assert (run.checked_order_count, run.discrepancies) == (0, ())
    assert not execution_db.exists()  # a read path never creates the order DB


# --- recording ----------------------------------------------------------------


def test_the_run_is_recorded_with_type_target_states_and_category(paths):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-23")
    broker.submit_order(broker_request("exec-23"))

    returned = service(QueryOnlyBroker(broker), paths).run(run_id="run-23")

    stored = reconciliation_store.get_reconciliation_run("run-23", db_path=reconciliation_db)
    assert stored == returned
    entry = only(stored.discrepancies)
    assert entry.discrepancy_type is DiscrepancyType.IN_FLIGHT_BROKER_FILLED
    assert entry.target_id == "exec-23"
    assert entry.client_order_id == client_id("exec-23")
    assert (entry.internal_state, entry.broker_state) == (OrderState.PENDING, OrderState.FILLED)
    assert entry.resolution_category is ResolutionCategory.AUTO_REPAIR_CANDIDATE
    assert entry.observed_at_utc == OBSERVED_AT
    assert stored.mode is ReconciliationMode.DETECTION_ONLY


def test_records_are_readable_after_a_restart(paths):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_unknown(execution_db, "exec-24")
    broker.submit_order(broker_request("exec-24"))
    recorded = service(QueryOnlyBroker(broker), paths).run(run_id="run-24")

    # a fresh store module state = a new process reading the same files
    reconciliation_store._REFERENCE_SCHEMA_CACHE = None
    runs = reconciliation_store.list_reconciliation_runs(db_path=reconciliation_db)

    assert runs == (recorded,)
    assert runs[0].discrepancies[0].detail


def test_recording_the_same_run_id_twice_is_refused(paths):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-25")
    broker.submit_order(broker_request("exec-25"))
    reconciler = service(QueryOnlyBroker(broker), paths)
    reconciler.run(run_id="run-25")

    with pytest.raises(reconciliation_store.ReconciliationRunConflictError):
        reconciler.run(run_id="run-25")

    assert len(reconciliation_store.list_reconciliation_runs(db_path=reconciliation_db)) == 1


def test_the_run_is_logged_with_ids_states_and_categories(paths, caplog):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_internal_pending(execution_db, "exec-26")
    broker.submit_order(broker_request("exec-26"))

    with caplog.at_level("INFO", logger="execution.broker_reconciliation"):
        service(QueryOnlyBroker(broker), paths).detect(run_id="run-26")

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "in_flight_broker_filled" in messages
    assert "exec-26" in messages
    assert "auto_repair_candidates=1" in messages
    assert "detection_only" in messages


# --- structural guarantees ----------------------------------------------------


def test_the_reconciliation_module_never_calls_a_broker_write_operation():
    tree = ast.parse(
        (REPO_ROOT / "execution" / "broker_reconciliation.py").read_text(encoding="utf-8")
    )
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "submit_order" not in called
    assert "cancel_order" not in called


def test_the_reconciliation_module_never_calls_an_execution_store_write_api():
    """Detection is read-only: the order store is only ever READ."""
    tree = ast.parse(
        (REPO_ROOT / "execution" / "broker_reconciliation.py").read_text(encoding="utf-8")
    )
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    forbidden = {
        "create_execution_order",
        "mark_submitting",
        "record_order_state",
        "record_order_result",
        "init_db",
    }
    assert called & forbidden == set()


def test_the_fake_broker_rejects_a_duplicate_seed_so_fixtures_stay_honest():
    """Guard for this file's own fixtures: a client_order_id is submitted at
    most once broker-side (a second submit would be a different scenario)."""
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    broker.submit_order(broker_request("exec-27"))
    with pytest.raises(BrokerInvalidRequestError):
        broker.submit_order(broker_request("exec-27"))


def test_get_order_raises_not_found_for_an_unknown_id():
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    with pytest.raises(BrokerOrderNotFoundError):
        broker.get_order("nobody")
