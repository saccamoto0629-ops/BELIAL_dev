"""Issue #99: the sanctioned cancel use case (LiveOrderSubmissionService.cancel).

Every branch of the cancel contract is exercised through the SINGLE
sanctioned path: success (CANCELLED persisted via the ordinary
``record_order_result`` state-machine contract), definitive broker refusal
(state untouched), local typed refusals (no broker call at all), lost
outcomes (state-only UNKNOWN + re-raise, never re-sent), the DB-save-failure
fallback and the double-failure degradation. FakeBroker performs no external
I/O and all persistence lives under ``tmp_path``.
"""

import logging

from datetime import timedelta
from decimal import Decimal

import pytest

from broker.broker_models import (
    BrokerCancellationError,
    BrokerCommunicationError,
    BrokerOrderNotFoundError,
    BrokerTimeoutError,
    OrderState,
)
from broker.fake_broker import (
    FailCancellation,
    FakeBroker,
    PartiallyFill,
    RejectOrder,
    StayPending,
    TimeoutCancellation,
    TimeoutOrder,
)
from execution import execution_store
from execution.live_submission import (
    CancelOutcome,
    CancelRefusalCode,
    CancelStatus,
)

# Shared helpers of the submission suite (tests run per-directory, so sibling
# helpers import flat -- same style as test_scenario_runner.py).
from test_safety_gate import (
    NOW,
    account_state,
    connection,
    make_broker,
    make_service,
    market,
    request,
)


def accepted_order(tmp_path, *, broker=None, client_order_id="client-1"):
    """Register + submit one intent that ends broker-ACCEPTED (StayPending);
    returns (service, broker)."""
    broker = broker if broker is not None else make_broker()
    if client_order_id not in broker._submit_scripts:  # keep caller overrides
        broker.script_order(client_order_id, StayPending(broker_order_id="BRK-1"))
    service, _, _ = make_service(tmp_path, conn=connection(broker))
    service.register(request(), client_order_id=client_order_id)
    decision, result = service.submit("exec-1", market=market())
    assert decision.allowed and result.state is OrderState.ACCEPTED
    return service, broker


def stored_state(tmp_path):
    row = execution_store.get_execution_order("exec-1", db_path=tmp_path / "orders.sqlite3")
    return row["submission_phase"], row["order_state"]


def history_rows(tmp_path):
    return execution_store.get_state_history("exec-1", db_path=tmp_path / "orders.sqlite3")


# --- success ------------------------------------------------------------------


def test_cancel_success_persists_cancelled_through_the_state_machine(tmp_path):
    service, broker = accepted_order(tmp_path)
    outcome = service.cancel("exec-1")
    assert outcome.status is CancelStatus.CANCELLED
    assert outcome.broker_called and outcome.refusal_code is None
    assert outcome.result.state is OrderState.CANCELLED
    assert broker.cancel_requests == ["client-1"]
    assert stored_state(tmp_path) == ("submitted", "cancelled")
    history = history_rows(tmp_path)
    assert [entry["new_state"] for entry in history] == ["pending", "accepted", "cancelled"]
    assert history[-1]["reason"] == "live cancellation via sanctioned cancel path"
    assert history[-1]["broker_order_id"] == "BRK-1"
    # The persisted snapshot is the broker's post-cancel OrderResult.
    stored = execution_store.load_order_result("exec-1", db_path=tmp_path / "orders.sqlite3")
    assert stored.state is OrderState.CANCELLED
    assert not execution_store.list_unknown_orders(db_path=tmp_path / "orders.sqlite3")
    assert not execution_store.list_unresolved_submissions(db_path=tmp_path / "orders.sqlite3")


def test_partially_filled_order_cancels_and_keeps_known_fills(tmp_path):
    broker = make_broker()
    broker.script_order(
        "client-1",
        PartiallyFill(fills=((Decimal("150.25"), Decimal("0.4")),), broker_order_id="BRK-1"),
    )
    service, _, _ = make_service(tmp_path, conn=connection(broker))
    service.register(request(), client_order_id="client-1")
    decision, result = service.submit("exec-1", market=market())
    assert result.state is OrderState.PARTIALLY_FILLED
    outcome = service.cancel("exec-1")
    assert outcome.status is CancelStatus.CANCELLED
    stored = execution_store.load_order_result("exec-1", db_path=tmp_path / "orders.sqlite3")
    assert stored.state is OrderState.CANCELLED
    assert stored.filled_quantity == Decimal("0.4")
    assert len(stored.fills) == 1


# --- duplicate cancel (idempotency) ------------------------------------------


def test_duplicate_cancel_is_refused_without_a_second_broker_call(tmp_path):
    service, broker = accepted_order(tmp_path)
    assert service.cancel("exec-1").status is CancelStatus.CANCELLED
    second = service.cancel("exec-1")
    assert second.status is CancelStatus.REFUSED
    assert second.refusal_code is CancelRefusalCode.ALREADY_CANCELLED
    assert not second.broker_called
    assert broker.cancel_requests == ["client-1"]  # exactly one attempt ever
    assert stored_state(tmp_path) == ("submitted", "cancelled")
    assert len(history_rows(tmp_path)) == 3  # the refusal wrote nothing


def test_duplicate_cancel_survives_service_recreation(tmp_path):
    """Process-restart equivalence: the guard is the DURABLE stored state,
    not in-memory service state."""
    service, broker = accepted_order(tmp_path)
    assert service.cancel("exec-1").status is CancelStatus.CANCELLED
    fresh_service, _, _ = make_service(
        tmp_path, conn=connection(broker), initialize_decisions=False, initialize_journal=False
    )
    second = fresh_service.cancel("exec-1")
    assert second.refusal_code is CancelRefusalCode.ALREADY_CANCELLED
    assert broker.cancel_requests == ["client-1"]


# --- definitive broker refusal ------------------------------------------------


def test_broker_refused_cancel_leaves_state_unchanged_and_records_failure(tmp_path, caplog):
    broker = make_broker()
    broker.script_order("client-1", StayPending(broker_order_id="BRK-1"))
    broker.script_cancel("client-1", FailCancellation(reason="broker-side hold"))
    service, broker = accepted_order(tmp_path, broker=broker)
    with caplog.at_level(logging.WARNING):
        outcome = service.cancel("exec-1")
    assert outcome.status is CancelStatus.BROKER_REFUSED
    assert outcome.broker_called and outcome.refusal_code is None and outcome.result is None
    assert "broker-side hold" in outcome.detail
    assert broker.cancel_requests == ["client-1"]
    # State machine untouched: no synthetic transition, no history row.
    assert stored_state(tmp_path) == ("submitted", "accepted")
    assert len(history_rows(tmp_path)) == 2
    # The failure is recorded with ids and reason (Issue #76 log contract).
    record = next(r for r in caplog.records if "broker refused cancellation" in r.getMessage())
    assert "exec-1" in record.getMessage() and "broker-side hold" in record.getMessage()
    # The order stays in-flight -- reconciliation keeps seeing it.
    in_flight = execution_store.list_in_flight_orders(db_path=tmp_path / "orders.sqlite3")
    assert [row["execution_request_id"] for row in in_flight] == ["exec-1"]


def test_cancel_after_definitive_refusal_is_a_new_attempt(tmp_path):
    """A definitively refused cancel leaves nothing pending at the broker,
    so a later cancel MAY try again (it is not a duplicate)."""
    broker = make_broker()
    broker.script_order("client-1", StayPending(broker_order_id="BRK-1"))
    broker.script_cancel("client-1", FailCancellation(reason="hold"))
    service, broker = accepted_order(tmp_path, broker=broker)
    assert service.cancel("exec-1").status is CancelStatus.BROKER_REFUSED
    assert service.cancel("exec-1").status is CancelStatus.BROKER_REFUSED
    assert broker.cancel_requests == ["client-1", "client-1"]
    assert stored_state(tmp_path) == ("submitted", "accepted")


def test_broker_side_terminal_divergence_is_refused_by_the_broker(tmp_path):
    """Store says ACCEPTED, broker settled the order meanwhile: the broker's
    terminal refusal surfaces as BROKER_REFUSED, the stored state stays
    unchanged, and the divergence remains reconciliation's to observe."""
    service, broker = accepted_order(tmp_path)
    broker.fill_remaining("client-1", price=Decimal("150.30"))  # broker-side only
    outcome = service.cancel("exec-1")
    assert outcome.status is CancelStatus.BROKER_REFUSED
    assert "terminal" in outcome.detail
    assert broker.cancel_requests == ["client-1"]
    assert stored_state(tmp_path) == ("submitted", "accepted")
    in_flight = execution_store.list_in_flight_orders(db_path=tmp_path / "orders.sqlite3")
    assert [row["execution_request_id"] for row in in_flight] == ["exec-1"]


# --- local typed refusals (no broker call) ------------------------------------


def test_cancel_unregistered_intent_is_refused(tmp_path):
    service, broker, _ = make_service(tmp_path)
    outcome = service.cancel("exec-1")
    assert outcome.status is CancelStatus.REFUSED
    assert outcome.refusal_code is CancelRefusalCode.INTENT_NOT_REGISTERED
    assert broker.cancel_requests == []


def test_cancel_never_submitted_intent_is_refused(tmp_path):
    service, broker, _ = make_service(tmp_path)
    service.register(request(), client_order_id="client-1")
    outcome = service.cancel("exec-1")
    assert outcome.refusal_code is CancelRefusalCode.NOT_SUBMITTED
    assert broker.cancel_requests == []
    assert stored_state(tmp_path) == ("created", None)


def test_cancel_while_submitting_pending_is_refused(tmp_path):
    """Phase submitting / state pending -- a submit whose outcome was never
    observed (in-flight or crashed): cancelling blind is banned."""
    service, broker, _ = make_service(tmp_path)
    service.register(request(), client_order_id="client-1")
    execution_store.mark_submitting(
        "exec-1", reason="test", changed_at_utc=NOW, db_path=tmp_path / "orders.sqlite3"
    )
    outcome = service.cancel("exec-1")
    assert outcome.refusal_code is CancelRefusalCode.SUBMISSION_UNRESOLVED
    assert broker.cancel_requests == []
    assert stored_state(tmp_path) == ("submitting", "pending")


def test_cancel_after_submit_unknown_is_refused(tmp_path):
    """A lost SUBMIT outcome (state UNKNOWN) refuses cancels too: no broker
    call of any kind while the outcome is unresolved."""
    broker = make_broker()
    broker.script_order("client-1", TimeoutOrder())
    service, _, _ = make_service(tmp_path, conn=connection(broker))
    service.register(request(), client_order_id="client-1")
    with pytest.raises(BrokerTimeoutError):
        service.submit("exec-1", market=market())
    outcome = service.cancel("exec-1")
    assert outcome.refusal_code is CancelRefusalCode.OUTCOME_UNRESOLVED
    assert broker.cancel_requests == []


@pytest.mark.parametrize("state", [OrderState.FILLED, OrderState.REJECTED])
def test_cancel_terminal_order_is_refused_locally(tmp_path, state):
    broker = make_broker()
    if state is OrderState.REJECTED:
        broker.script_order("client-1", RejectOrder(reason="no"))
    service, _, _ = make_service(tmp_path, conn=connection(broker))
    service.register(request(), client_order_id="client-1")
    decision, result = service.submit("exec-1", market=market())
    assert result.state is state
    outcome = service.cancel("exec-1")
    assert outcome.status is CancelStatus.REFUSED
    assert outcome.refusal_code is (
        CancelRefusalCode.ALREADY_CANCELLED
        if state is OrderState.CANCELLED
        else CancelRefusalCode.TERMINAL_STATE
    )
    assert broker.cancel_requests == []
    assert stored_state(tmp_path) == ("submitted", state.value)


def test_cancel_with_unreadable_store_fails_closed(tmp_path):
    service, broker, _ = make_service(tmp_path)
    (tmp_path / "orders.sqlite3").write_bytes(b"not a database")
    outcome = service.cancel("exec-1")
    assert outcome.refusal_code is CancelRefusalCode.STATE_QUERY_FAILED
    assert broker.cancel_requests == []


def test_local_refusals_are_warn_logged_with_ids(tmp_path, caplog):
    service, _, _ = make_service(tmp_path)
    with caplog.at_level(logging.WARNING):
        service.cancel("exec-1")
    record = next(r for r in caplog.records if "cancellation refused" in r.getMessage())
    message = record.getMessage()
    assert "exec-1" in message and "cancel_intent_not_registered" in message


# --- lost outcomes (UNKNOWN + re-raise, never re-sent) ------------------------


def test_cancel_timeout_records_unknown_and_bans_resend(tmp_path):
    broker = make_broker()
    broker.script_order("client-1", StayPending(broker_order_id="BRK-1"))
    broker.script_cancel("client-1", TimeoutCancellation())
    service, broker = accepted_order(tmp_path, broker=broker)
    with pytest.raises(BrokerTimeoutError):
        service.cancel("exec-1")
    assert broker.cancel_requests == ["client-1"]
    assert stored_state(tmp_path) == ("submitted", "unknown")
    unknown = execution_store.list_unknown_orders(db_path=tmp_path / "orders.sqlite3")
    assert [row["execution_request_id"] for row in unknown] == ["exec-1"]
    unresolved = execution_store.list_unresolved_submissions(db_path=tmp_path / "orders.sqlite3")
    assert [row["execution_request_id"] for row in unresolved] == ["exec-1"]
    history = history_rows(tmp_path)
    assert history[-1]["new_state"] == "unknown"
    assert "cancel outcome lost" in history[-1]["reason"]
    # Re-running the same cancel NEVER re-sends: typed refusal, one attempt.
    retry = service.cancel("exec-1")
    assert retry.status is CancelStatus.REFUSED
    assert retry.refusal_code is CancelRefusalCode.OUTCOME_UNRESOLVED
    assert broker.cancel_requests == ["client-1"]


class _RaisingCancelBroker(FakeBroker):
    """Cancel raises the injected infrastructure error; the attempt is still
    counted (mirrors FakeBroker's attempt-recording contract)."""

    def __init__(self, exc, **kwargs):
        super().__init__(**kwargs)
        self._cancel_exc = exc
        self.cancel_attempts = 0

    def cancel_order(self, client_order_id: str):
        self.cancel_attempts += 1
        raise self._cancel_exc


@pytest.mark.parametrize(
    "exc",
    [
        BrokerCommunicationError("connection lost"),
        BrokerOrderNotFoundError("no such order", client_order_id="client-1"),
        RuntimeError("unexpected adapter fault"),
    ],
)
def test_any_other_cancel_exception_is_conservative_unknown(tmp_path, exc):
    """Communication failure, an order the broker claims not to know, or an
    unexpected fault: never downgraded to 'not cancelled' -- UNKNOWN is
    recorded, the exception re-raises and the retry is refused."""
    broker = _RaisingCancelBroker(exc, default_fill_price=Decimal("150"), clock=lambda: NOW)
    broker.set_account_state(account_state())
    broker.script_order("client-1", StayPending(broker_order_id="BRK-1"))
    service, broker = accepted_order(tmp_path, broker=broker)
    with pytest.raises(type(exc)):
        service.cancel("exec-1")
    assert broker.cancel_attempts == 1
    assert stored_state(tmp_path) == ("submitted", "unknown")
    retry = service.cancel("exec-1")
    assert retry.refusal_code is CancelRefusalCode.OUTCOME_UNRESOLVED
    assert broker.cancel_attempts == 1


# --- DB save failure after a received result ----------------------------------


def test_cancel_result_persistence_failure_falls_back_to_unknown(tmp_path, monkeypatch):
    service, broker = accepted_order(tmp_path)
    boom = execution_store.ExecutionStoreError("disk full")
    original = execution_store.record_order_result

    def failing_record(*args, **kwargs):
        raise boom

    monkeypatch.setattr(execution_store, "record_order_result", failing_record)
    with pytest.raises(execution_store.ExecutionStoreError) as excinfo:
        service.cancel("exec-1")
    assert excinfo.value is boom  # the original failure surfaces, unmasked
    monkeypatch.setattr(execution_store, "record_order_result", original)
    # The broker DID cancel; the received result could not be saved -- the
    # order became a query target whose broker-side truth a later query
    # re-observes (never lost behind the persistence error).
    assert broker.get_order("client-1").state is OrderState.CANCELLED
    assert stored_state(tmp_path) == ("submitted", "unknown")
    assert "cancel result persistence failed" in history_rows(tmp_path)[-1]["reason"]
    unresolved = execution_store.list_unresolved_submissions(db_path=tmp_path / "orders.sqlite3")
    assert [row["execution_request_id"] for row in unresolved] == ["exec-1"]
    # Idempotency holds across the failure: the retry is refused, one attempt.
    retry = service.cancel("exec-1")
    assert retry.refusal_code is CancelRefusalCode.OUTCOME_UNRESOLVED
    assert broker.cancel_requests == ["client-1"]


def test_cancel_double_failure_keeps_the_order_visible_in_flight(tmp_path, monkeypatch, caplog):
    """Result save AND the UNKNOWN fallback both fail (order DB down): the
    original failure still surfaces, the fact is critically logged, and the
    order -- still ACCEPTED on its durable row -- remains a reconciliation
    target via list_in_flight_orders()."""
    service, broker = accepted_order(tmp_path)

    def failing(*args, **kwargs):
        raise execution_store.ExecutionStoreError("order DB down")

    monkeypatch.setattr(execution_store, "record_order_result", failing)
    monkeypatch.setattr(execution_store, "record_order_state", failing)
    with caplog.at_level(logging.CRITICAL):
        with pytest.raises(execution_store.ExecutionStoreError):
            service.cancel("exec-1")
    assert any("failed to record UNKNOWN outcome" in r.getMessage() for r in caplog.records)
    monkeypatch.undo()
    assert stored_state(tmp_path) == ("submitted", "accepted")
    in_flight = execution_store.list_in_flight_orders(db_path=tmp_path / "orders.sqlite3")
    assert [row["execution_request_id"] for row in in_flight] == ["exec-1"]


# --- CancelOutcome invariants -------------------------------------------------


def test_cancel_outcome_cross_field_invariants():
    with pytest.raises(ValueError):
        CancelOutcome(
            execution_request_id="exec-1",
            status=CancelStatus.CANCELLED,
            detail="missing result",
            broker_called=True,
        )
    with pytest.raises(ValueError):
        CancelOutcome(
            execution_request_id="exec-1",
            status=CancelStatus.REFUSED,
            detail="refusal without code",
        )
    with pytest.raises(ValueError):
        CancelOutcome(
            execution_request_id="exec-1",
            status=CancelStatus.REFUSED,
            detail="refusal claiming a broker call",
            refusal_code=CancelRefusalCode.TERMINAL_STATE,
            broker_called=True,
        )
    with pytest.raises(ValueError):
        CancelOutcome(
            execution_request_id="exec-1",
            status=CancelStatus.BROKER_REFUSED,
            detail="broker refusal without a call",
            broker_called=False,
        )
    with pytest.raises(ValueError):
        CancelOutcome(
            execution_request_id="", status=CancelStatus.REFUSED, detail="empty id",
            refusal_code=CancelRefusalCode.TERMINAL_STATE,
        )
    with pytest.raises(ValueError):
        CancelOutcome(
            execution_request_id="exec-1", status="cancelled", detail="not an enum"
        )


def test_cancel_requires_a_non_empty_id(tmp_path):
    service, _, _ = make_service(tmp_path)
    with pytest.raises(ValueError):
        service.cancel("")
    with pytest.raises(ValueError):
        service.cancel(None)
