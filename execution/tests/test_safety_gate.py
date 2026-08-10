"""Issue #75: fail-closed pre-submit Safety Gate + LiveOrderSubmissionService.

Every safety condition is tested independently through the SINGLE sanctioned
submission path; every rejection path asserts that FakeBroker received ZERO
submissions. FakeBroker performs no external I/O and all persistence lives
under tmp_path, so none of these tests can reach a network, Discord, S3 or a
real DB.
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from broker.broker_models import (
    AccountState,
    BrokerCommunicationError,
    BrokerConnectionIdentity,
    BrokerTimeoutError,
    Fill,
    OrderResult,
    OrderSide,
    OrderState,
    OrderType,
    PositionInfo,
    PositionSide,
    RejectionCategory,
)
from broker.fake_broker import FailCommunication, FakeBroker, RejectOrder, TimeoutOrder
from execution import execution_store
from execution.execution_models import ExecutionRequest
from execution.live_submission import LiveOrderSubmissionService
from execution.safety_gate import (
    BrokerConnection,
    ExecutionMode,
    MarketSafetySnapshot,
    SafetyDecision,
    SafetyGate,
    SafetyPolicy,
    SafetyRejection,
    SafetyRejectionCode,
    StaticSafetyState,
    dedupe_rejections,
)
from execution.safety_store import SQLiteSafetyDecisionStore
from execution.submission_journal import SubmissionJournal

NOW = datetime(2026, 7, 22, 1, 0, tzinfo=timezone.utc)


def request(**overrides):
    values = dict(
        execution_request_id="exec-1",
        signal_id="signal-1",
        strategy_id="strategy-1",
        symbol="USD_JPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        requested_at_utc=NOW,
        requested_price=None,
        stop_loss_price=Decimal("149.5"),
        take_profit_price=Decimal("151.5"),
    )
    values.update(overrides)
    return ExecutionRequest(**values)


def market(**overrides):
    values = dict(
        symbol="USD_JPY",
        reference_price=Decimal("150"),
        bid=Decimal("149.99"),
        ask=Decimal("150.01"),
        observed_at_utc=NOW,
        market_open=True,
        tradable=True,
    )
    values.update(overrides)
    return MarketSafetySnapshot(**values)


def policy(**overrides):
    values = dict(
        policy_id="policy-test-1",
        allowed_symbols=frozenset({"USD_JPY"}),
        allowed_sides=frozenset({OrderSide.BUY, OrderSide.SELL}),
        max_order_quantity=Decimal("10"),
        max_positions_per_symbol=2,
        max_positions_account=5,
        max_reference_age=timedelta(seconds=30),
        max_spread_by_symbol={"USD_JPY": Decimal("0.05")},
        allowed_connection_targets={
            ExecutionMode.FAKE: frozenset({"fake"}),
            ExecutionMode.DEMO: frozenset({"oanda-demo"}),
            ExecutionMode.LIVE: frozenset({"oanda-live"}),
        },
    )
    values.update(overrides)
    return SafetyPolicy(**values)


def account_state(retrieved_at=NOW):
    return AccountState(
        account_id="fake-account",
        currency="USD",
        balance=Decimal("100000"),
        equity=Decimal("100000"),
        margin_available=Decimal("100000"),
        retrieved_at_utc=retrieved_at,
    )


def make_broker(cls=FakeBroker, *, account_retrieved_at=NOW, target="fake"):
    broker = cls(default_fill_price=Decimal("150"), clock=lambda: NOW, connection_target=target)
    broker.set_account_state(account_state(account_retrieved_at))
    return broker


def connection(broker=None):
    return BrokerConnection(broker=broker if broker is not None else make_broker())


def gate(*, state=None, selected_policy=None, clock=None):
    return SafetyGate(
        policy=selected_policy or policy(),
        state_provider=state or StaticSafetyState(),
        clock=clock or (lambda: NOW),
    )


def make_service(
    tmp_path, *, conn=None, the_gate=None, initialize_decisions=True, initialize_journal=True
):
    decision_db_path = tmp_path / "safety.sqlite3"
    if initialize_decisions:
        SQLiteSafetyDecisionStore(decision_db_path).initialize()
    journal_db_path = tmp_path / "journal.sqlite3"
    if initialize_journal:
        SubmissionJournal(journal_db_path).initialize()
    conn = conn if conn is not None else connection()
    service = LiveOrderSubmissionService(
        connection=conn,
        gate=the_gate or gate(),
        decision_db_path=decision_db_path,
        order_db_path=tmp_path / "orders.sqlite3",
        journal_db_path=journal_db_path,
        clock=lambda: NOW,
    )
    # Reader handle for assertions only (the service's own store is sealed).
    return service, conn.broker, SQLiteSafetyDecisionStore(decision_db_path)


def run(tmp_path, req=None, *, snapshot=None, register=True, **service_kwargs):
    """Register (unless told not to) and submit through the single service
    boundary; returns (decision, result, broker, decision_reader, service)."""
    req = req if req is not None else request()
    service, broker, reader = make_service(tmp_path, **service_kwargs)
    if register:
        service.register(req, client_order_id="client-1")
    decision, result = service.submit(
        req.execution_request_id,
        market=snapshot if snapshot is not None else market(),
    )
    return decision, result, broker, reader, service


def codes(decision):
    return {item.code for item in decision.rejections}


# --- normal path -------------------------------------------------------------


def test_normal_conditions_submit_to_fake_broker_without_external_io(tmp_path):
    decision, result, broker, reader, _ = run(tmp_path)

    assert decision.allowed is True
    assert decision.rejections == ()
    assert result is not None
    assert result.state is OrderState.FILLED
    assert len(broker.submitted_requests) == 1
    assert reader.list_for_request("exec-1") == (decision,)


def test_submit_uses_the_registered_fixed_client_order_id(tmp_path):
    _, result, broker, _, _ = run(tmp_path)
    assert broker.submitted_requests[0].client_order_id == "client-1"
    assert result.client_order_id == "client-1"


def test_allowed_submission_is_recorded_in_the_execution_store(tmp_path):
    decision, result, _, _, _ = run(tmp_path)
    row = execution_store.get_execution_order("exec-1", db_path=tmp_path / "orders.sqlite3")
    assert row["submission_phase"] == execution_store.PHASE_SUBMITTED
    assert row["order_state"] == OrderState.FILLED.value
    assert row["client_order_id"] == "client-1"
    assert decision.allowed and result is not None


def test_allowed_decision_carries_audit_fields(tmp_path):
    decision, _, _, _, _ = run(tmp_path)
    assert decision.policy_id == "policy-test-1"
    assert decision.execution_mode == "fake"
    assert decision.connection_target == "fake"
    assert decision.account_id == "fake-account"
    # display boundary (PR #101): the identifier survives internally, but
    # repr() never renders the raw account id
    assert "fake-account" not in repr(decision)
    assert decision.position_count == 0
    assert decision.checked_at_utc == NOW


def test_sell_with_valid_sl_tp_is_allowed(tmp_path):
    decision, result, broker, _, _ = run(
        tmp_path,
        request(side=OrderSide.SELL, stop_loss_price=Decimal("150.5"),
                take_profit_price=Decimal("149.5")),
    )
    assert decision.allowed is True
    assert result is not None
    assert len(broker.submitted_requests) == 1


def test_limit_order_sl_tp_are_judged_against_requested_price_not_live_price(tmp_path):
    # BUY LIMIT below the market: TP 149.8 is under the live ask (150.01) but
    # ABOVE the limit price 149 -- must be allowed (the base price for LIMIT
    # SL/TP validation is requested_price, not the live snapshot).
    decision, result, broker, _, _ = run(
        tmp_path,
        request(
            order_type=OrderType.LIMIT,
            requested_price=Decimal("149"),
            stop_loss_price=Decimal("148.5"),
            take_profit_price=Decimal("149.8"),
        ),
    )
    assert decision.allowed is True
    assert result is not None
    assert len(broker.submitted_requests) == 1


# --- double submission / UNKNOWN re-send bans --------------------------------


def test_second_submit_of_the_same_intent_is_rejected_before_the_broker(tmp_path):
    req = request()
    service, broker, reader = make_service(tmp_path)
    service.register(req, client_order_id="client-1")

    first, first_result = service.submit(req.execution_request_id, market=market())
    second, second_result = service.submit(req.execution_request_id, market=market())

    assert first.allowed is True and first_result is not None
    assert SafetyRejectionCode.DUPLICATE_SUBMISSION in codes(second)
    assert second_result is None
    assert len(broker.submitted_requests) == 1
    assert reader.list_for_request(req.execution_request_id) == (first, second)


def test_unknown_outcome_bans_resubmission(tmp_path):
    req = request()
    service, broker, _ = make_service(tmp_path)
    service.register(req, client_order_id="client-1")
    broker.script_order("client-1", TimeoutOrder())

    with pytest.raises(BrokerTimeoutError):
        service.submit(req.execution_request_id, market=market())

    row = execution_store.get_execution_order(
        req.execution_request_id, db_path=tmp_path / "orders.sqlite3"
    )
    assert row["order_state"] == OrderState.UNKNOWN.value

    decision, result = service.submit(req.execution_request_id, market=market())
    assert SafetyRejectionCode.DUPLICATE_SUBMISSION in codes(decision)
    assert result is None
    assert len(broker.submitted_requests) == 1  # the broker never saw a second request


def test_communication_failure_records_unknown_and_bans_resubmission(tmp_path):
    req = request()
    service, broker, _ = make_service(tmp_path)
    service.register(req, client_order_id="client-1")
    broker.script_order("client-1", FailCommunication())

    with pytest.raises(BrokerCommunicationError):
        service.submit(req.execution_request_id, market=market())

    row = execution_store.get_execution_order(
        req.execution_request_id, db_path=tmp_path / "orders.sqlite3"
    )
    assert row["order_state"] == OrderState.UNKNOWN.value  # never "not placed"

    decision, result = service.submit(req.execution_request_id, market=market())
    assert SafetyRejectionCode.DUPLICATE_SUBMISSION in codes(decision)
    assert result is None


def test_only_the_winner_of_created_to_submitting_may_send(tmp_path):
    # Another caller won the created -> submitting transition first: this
    # service call must NOT reach the broker.
    req = request()
    service, broker, _ = make_service(tmp_path)
    service.register(req, client_order_id="client-1")
    execution_store.mark_submitting(
        req.execution_request_id,
        reason="another caller won the race",
        changed_at_utc=NOW,
        db_path=tmp_path / "orders.sqlite3",
    )

    decision, result = service.submit(req.execution_request_id, market=market())

    assert SafetyRejectionCode.DUPLICATE_SUBMISSION in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


def test_mark_submitting_infrastructure_failure_is_fail_closed(tmp_path, monkeypatch):
    req = request()
    service, broker, _ = make_service(tmp_path)
    service.register(req, client_order_id="client-1")

    def broken_mark_submitting(*args, **kwargs):
        raise RuntimeError("order DB unavailable")

    monkeypatch.setattr(execution_store, "mark_submitting", broken_mark_submitting)
    decision, result = service.submit(req.execution_request_id, market=market())

    assert SafetyRejectionCode.STATE_QUERY_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


def test_broker_rejection_is_a_result_not_an_exception(tmp_path):
    req = request()
    service, broker, _ = make_service(tmp_path)
    service.register(req, client_order_id="client-1")
    broker.script_order("client-1", RejectOrder(reason="insufficient margin"))

    decision, result = service.submit(req.execution_request_id, market=market())

    assert decision.allowed is True  # the GATE allowed; the broker said no
    assert result.state is OrderState.REJECTED
    row = execution_store.get_execution_order(
        req.execution_request_id, db_path=tmp_path / "orders.sqlite3"
    )
    assert row["order_state"] == OrderState.REJECTED.value


# --- broker answered but the result could not be persisted -------------------


def test_result_persistence_failure_falls_back_to_unknown(tmp_path, monkeypatch):
    req = request()
    service, broker, _ = make_service(tmp_path)
    service.register(req, client_order_id="client-1")

    def broken_record_order_result(*args, **kwargs):
        raise RuntimeError("order DB failed while persisting the result")

    monkeypatch.setattr(execution_store, "record_order_result", broken_record_order_result)
    with pytest.raises(RuntimeError, match="persisting the result"):
        service.submit(req.execution_request_id, market=market())

    # The order EXISTS at the broker; it must remain a query target.
    assert len(broker.submitted_requests) == 1
    order_db = tmp_path / "orders.sqlite3"
    row = execution_store.get_execution_order(req.execution_request_id, db_path=order_db)
    assert row["order_state"] == OrderState.UNKNOWN.value
    unresolved = execution_store.list_unresolved_submissions(db_path=order_db)
    assert [r["execution_request_id"] for r in unresolved] == [req.execution_request_id]
    assert [
        r["execution_request_id"]
        for r in execution_store.list_unknown_orders(db_path=order_db)
    ] == [req.execution_request_id]


def test_double_persistence_failure_keeps_the_order_visible_to_reconciliation(
    tmp_path, monkeypatch
):
    req = request()
    service, broker, _ = make_service(tmp_path)
    service.register(req, client_order_id="client-1")

    def broken(*args, **kwargs):
        raise RuntimeError("order DB completely down")

    monkeypatch.setattr(execution_store, "record_order_result", broken)
    monkeypatch.setattr(execution_store, "record_order_state", broken)
    with pytest.raises(RuntimeError, match="completely down"):
        service.submit(req.execution_request_id, market=market())

    assert len(broker.submitted_requests) == 1
    order_db = tmp_path / "orders.sqlite3"
    # Even with BOTH recordings failed, the submitting/pending order is
    # retrievable as an unresolved submission -- it never disappears from
    # the reconciliation view (Issue #75 review r3).
    row = execution_store.get_execution_order(req.execution_request_id, db_path=order_db)
    assert row["submission_phase"] == execution_store.PHASE_SUBMITTING
    assert row["order_state"] == OrderState.PENDING.value
    assert execution_store.list_unknown_orders(db_path=order_db) == []
    unresolved = execution_store.list_unresolved_submissions(db_path=order_db)
    assert [r["execution_request_id"] for r in unresolved] == [req.execution_request_id]


def test_stuck_pending_submission_is_listed_as_unresolved(tmp_path):
    # Simulated crash: the transition was won but the process died before the
    # broker call. The order must be visible to reconciliation.
    req = request()
    service, _, _ = make_service(tmp_path)
    service.register(req, client_order_id="client-1")
    order_db = tmp_path / "orders.sqlite3"
    execution_store.mark_submitting(
        req.execution_request_id, reason="crashed before submit", changed_at_utc=NOW,
        db_path=order_db,
    )

    unresolved = execution_store.list_unresolved_submissions(db_path=order_db)
    assert [r["execution_request_id"] for r in unresolved] == [req.execution_request_id]
    assert execution_store.list_unknown_orders(db_path=order_db) == []


# --- intent registration / execution store integration -----------------------


def test_unregistered_intent_is_rejected_before_the_broker(tmp_path):
    decision, result, broker, reader, _ = run(tmp_path, register=False)
    assert SafetyRejectionCode.INTENT_NOT_REGISTERED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []
    assert reader.list_for_request("exec-1") == (decision,)


def test_corrupted_order_store_is_fail_closed(tmp_path):
    import sqlite3

    req = request()
    service, broker, _ = make_service(tmp_path)
    service.register(req, client_order_id="client-1")
    with sqlite3.connect(tmp_path / "orders.sqlite3") as raw:
        raw.execute(
            "CREATE TRIGGER sneak AFTER UPDATE ON execution_orders BEGIN "
            "DELETE FROM execution_order_state_history; END"
        )

    decision, result = service.submit(req.execution_request_id, market=market())

    assert SafetyRejectionCode.STATE_QUERY_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


# --- durable decision recording: sealed store + read-back --------------------


def test_decision_store_cannot_be_injected_as_an_object(tmp_path):
    class InMemoryRecorder:
        def record(self, decision): ...
        def list_for_request(self, execution_request_id): return ()

    for not_a_path in (
        InMemoryRecorder(),
        SQLiteSafetyDecisionStore(tmp_path / "safety.sqlite3"),
    ):
        with pytest.raises(ValueError, match="filesystem path"):
            LiveOrderSubmissionService(
                connection=connection(),
                gate=gate(),
                decision_db_path=not_a_path,
                order_db_path=tmp_path / "orders.sqlite3",
                journal_db_path=tmp_path / "journal.sqlite3",
                clock=lambda: NOW,
            )


def test_silently_lost_decision_write_is_caught_by_readback(tmp_path, monkeypatch):
    # Simulate an infrastructure write loss on the SEALED store: record()
    # appears to succeed but nothing lands in the DB. The read-back must
    # catch it and block the submit.
    monkeypatch.setattr(SQLiteSafetyDecisionStore, "record", lambda self, decision: None)
    decision, result, broker, _, _ = run(tmp_path)

    assert SafetyRejectionCode.DECISION_PERSIST_FAILED in codes(decision)
    assert decision.allowed is False
    assert result is None
    assert broker.submitted_requests == []


def test_readback_tolerates_a_concurrent_decision_landing_after_ours(tmp_path, monkeypatch):
    # PR #101 review (Linux CI flake): concurrent losers of the same intent
    # each record their own rejection decision, so ANOTHER caller's decision
    # can land between this caller's record() and its read-back. Durability
    # requires OUR decision to survive a re-read -- being the newest row is
    # not part of the contract. Simulate the interleaving deterministically
    # by appending an interloper decision to every read-back result; the
    # submit must still proceed without DECISION_PERSIST_FAILED.
    from dataclasses import replace

    original = SQLiteSafetyDecisionStore.list_for_request

    def racy_list_for_request(self, execution_request_id):
        stored = original(self, execution_request_id)
        if not stored:
            return stored
        interloper = replace(
            stored[-1],
            checked_at_utc=stored[-1].checked_at_utc + timedelta(seconds=1),
        )
        return tuple(stored) + (interloper,)

    monkeypatch.setattr(SQLiteSafetyDecisionStore, "list_for_request", racy_list_for_request)
    decision, result, broker, _, _ = run(tmp_path)

    assert SafetyRejectionCode.DECISION_PERSIST_FAILED not in codes(decision)
    assert decision.allowed is True
    assert result is not None
    assert len(broker.submitted_requests) == 1


def test_uninitialized_decision_store_prevents_submit(tmp_path):
    decision, result, broker, _, _ = run(tmp_path, initialize_decisions=False)
    assert decision.rejection_codes == (SafetyRejectionCode.DECISION_PERSIST_FAILED.value,)
    assert result is None
    assert broker.submitted_requests == []


def test_persistence_failure_keeps_the_original_rejection_codes(tmp_path):
    decision, result, broker, _, _ = run(
        tmp_path, request(quantity=Decimal("11")), initialize_decisions=False
    )
    assert SafetyRejectionCode.QUANTITY_LIMIT_EXCEEDED in codes(decision)
    assert SafetyRejectionCode.DECISION_PERSIST_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


# --- each safety condition rejects independently, before any submit ----------


@pytest.mark.parametrize(
    ("req_factory", "snapshot_factory", "policy_factory", "target", "expected"),
    [
        pytest.param(
            lambda: request(symbol="XAU_USD"), lambda: market(symbol="XAU_USD"),
            policy, "fake",
            SafetyRejectionCode.SYMBOL_NOT_ALLOWED, id="symbol-not-allowed"),
        pytest.param(
            lambda: request(side=OrderSide.SELL, stop_loss_price=Decimal("150.5"),
                            take_profit_price=Decimal("149.5")),
            market, lambda: policy(allowed_sides=frozenset({OrderSide.BUY})),
            "fake",
            SafetyRejectionCode.SIDE_NOT_ALLOWED, id="side-not-allowed"),
        pytest.param(
            lambda: request(quantity=Decimal("11")), market, policy, "fake",
            SafetyRejectionCode.QUANTITY_LIMIT_EXCEEDED, id="quantity-limit"),
        pytest.param(
            lambda: request(stop_loss_price=None), market, policy, "fake",
            SafetyRejectionCode.STOP_LOSS_REQUIRED, id="stop-loss-required"),
        pytest.param(
            lambda: request(stop_loss_price=Decimal("150.5")), market, policy, "fake",
            SafetyRejectionCode.STOP_LOSS_INVALID, id="stop-loss-invalid-buy"),
        pytest.param(
            lambda: request(side=OrderSide.SELL, stop_loss_price=Decimal("149.9"),
                            take_profit_price=Decimal("149.5")),
            market, policy, "fake",
            SafetyRejectionCode.STOP_LOSS_INVALID, id="stop-loss-invalid-sell"),
        pytest.param(
            lambda: request(take_profit_price=Decimal("150.005")), market, policy, "fake",
            SafetyRejectionCode.TAKE_PROFIT_INVALID, id="take-profit-invalid"),
        pytest.param(
            request, lambda: market(symbol="EUR_USD", reference_price=Decimal("1.1"),
                                    bid=Decimal("1.0999"), ask=Decimal("1.1001")),
            policy, "fake",
            SafetyRejectionCode.MARKET_SNAPSHOT_MISMATCH, id="snapshot-mismatch"),
        pytest.param(
            request, lambda: market(reference_price=None), policy, "fake",
            SafetyRejectionCode.REFERENCE_PRICE_MISSING, id="reference-missing"),
        pytest.param(
            request, lambda: market(observed_at_utc=NOW - timedelta(seconds=31)), policy,
            "fake",
            SafetyRejectionCode.REFERENCE_PRICE_STALE, id="reference-stale"),
        pytest.param(
            request, lambda: market(observed_at_utc=NOW + timedelta(seconds=1)), policy,
            "fake",
            SafetyRejectionCode.REFERENCE_TIMESTAMP_FUTURE, id="reference-future"),
        pytest.param(
            request, lambda: market(bid=None), policy, "fake",
            SafetyRejectionCode.SPREAD_MISSING, id="spread-missing"),
        pytest.param(
            request, lambda: market(ask=Decimal("150.10")), policy, "fake",
            SafetyRejectionCode.SPREAD_LIMIT_EXCEEDED, id="spread-limit"),
        pytest.param(
            request, lambda: market(market_open=False), policy, "fake",
            SafetyRejectionCode.MARKET_CLOSED, id="market-closed"),
        pytest.param(
            request, lambda: market(tradable=False), policy, "fake",
            SafetyRejectionCode.SYMBOL_NOT_TRADABLE, id="not-tradable"),
        pytest.param(
            request, market, policy, "oanda-live",
            SafetyRejectionCode.EXECUTION_MODE_MISMATCH, id="fake-broker-live-target"),
        pytest.param(
            request, market, policy, "oanda-demo",
            SafetyRejectionCode.EXECUTION_MODE_MISMATCH, id="fake-broker-demo-target"),
        pytest.param(
            request, market, policy, "unknown-endpoint",
            SafetyRejectionCode.EXECUTION_MODE_MISMATCH, id="unknown-target"),
        pytest.param(
            request, market, policy, "FAKE",
            SafetyRejectionCode.EXECUTION_MODE_MISMATCH, id="target-case-differs"),
    ],
)
def test_each_condition_rejects_before_submit(
    tmp_path, req_factory, snapshot_factory, policy_factory, target, expected
):
    decision, result, broker, reader, _ = run(
        tmp_path,
        req_factory(),
        snapshot=snapshot_factory(),
        conn=connection(make_broker(target=target)),
        the_gate=gate(selected_policy=policy_factory()),
    )
    assert expected in codes(decision)
    assert decision.allowed is False
    assert result is None
    assert broker.submitted_requests == []
    assert reader.list_for_request("exec-1") == (decision,)


# --- typed connection identity (derived from the broker instance) ------------


class StubBroker:
    """Satisfies the Broker protocol structurally but is not a FakeBroker."""

    def submit_order(self, request): ...
    def cancel_order(self, client_order_id): ...
    def get_order(self, client_order_id): ...
    def get_positions(self): ...
    def get_account_state(self): ...


class StubBrokerWithIdentity(StubBroker):
    def connection_identity(self):
        return BrokerConnectionIdentity(mode="fake", target="fake")


def identity_broker(mode, target):
    class LyingIdentityBroker(FakeBroker):
        def connection_identity(self):
            return BrokerConnectionIdentity(mode=mode, target=target)

    return LyingIdentityBroker(default_fill_price=Decimal("150"), clock=lambda: NOW)


def test_connection_identity_is_derived_from_the_broker_instance():
    conn = BrokerConnection(broker=make_broker(target="fake-endpoint-2"))
    assert conn.execution_mode is ExecutionMode.FAKE
    assert conn.connection_target == "fake-endpoint-2"


def test_connection_mode_and_target_cannot_be_declared_by_the_caller():
    with pytest.raises(TypeError):
        BrokerConnection(
            broker=make_broker(),
            execution_mode=ExecutionMode.LIVE,
            connection_target="oanda-live",
        )


@pytest.mark.parametrize(
    ("mode", "target", "match"),
    [
        pytest.param("live", "oanda-live", "no broker implementation is registered",
                     id="fakebroker-cannot-claim-live"),
        pytest.param("demo", "oanda-demo", "no broker implementation is registered",
                     id="fakebroker-cannot-claim-demo"),
        pytest.param("mystery", "somewhere", "unknown execution mode",
                     id="unknown-mode"),
    ],
)
def test_broker_connection_rejects_invalid_self_reported_identity(mode, target, match):
    with pytest.raises(ValueError, match=match):
        BrokerConnection(broker=identity_broker(mode, target))


def test_broker_without_connection_identity_is_rejected():
    with pytest.raises(ValueError, match="connection_identity"):
        BrokerConnection(broker=StubBroker())


def test_non_fakebroker_reporting_fake_mode_is_rejected():
    with pytest.raises(ValueError, match="requires a FakeBroker instance"):
        BrokerConnection(broker=StubBrokerWithIdentity())


def test_malformed_identity_object_is_rejected():
    class BadIdentityBroker(FakeBroker):
        def connection_identity(self):
            return ("fake", "fake")

    with pytest.raises(ValueError, match="BrokerConnectionIdentity"):
        BrokerConnection(
            broker=BadIdentityBroker(default_fill_price=Decimal("150"), clock=lambda: NOW)
        )


def test_evaluate_rederives_the_identity_fail_closed():
    # Bypass the frozen constructor on purpose to prove the gate re-derives.
    conn = connection()
    object.__setattr__(conn, "execution_mode", ExecutionMode.LIVE)
    decision = gate().evaluate(request(), connection=conn, market=market())
    assert SafetyRejectionCode.EXECUTION_MODE_MISMATCH in codes(decision)
    # Several identity checks fire; dedup keeps exactly one code.
    assert decision.rejection_codes.count(
        SafetyRejectionCode.EXECUTION_MODE_MISMATCH.value
    ) == 1


def test_identity_drift_after_composition_is_fail_closed(tmp_path):
    class ShiftyIdentityBroker(FakeBroker):
        current_target = "fake"

        def connection_identity(self):
            return BrokerConnectionIdentity(mode="fake", target=self.current_target)

    broker = ShiftyIdentityBroker(default_fill_price=Decimal("150"), clock=lambda: NOW)
    broker.set_account_state(account_state())
    conn = BrokerConnection(broker=broker)
    assert conn.connection_target == "fake"

    broker.current_target = "oanda-demo"  # identity drifts after composition
    decision, result, broker, _, _ = run(tmp_path, conn=conn)

    assert SafetyRejectionCode.EXECUTION_MODE_MISMATCH in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


# --- kill switch -------------------------------------------------------------


def test_kill_switch_rejects_every_new_order_even_when_all_else_passes(tmp_path):
    decision, result, broker, _, _ = run(
        tmp_path, the_gate=gate(state=StaticSafetyState(kill_switch_enabled=True))
    )
    assert SafetyRejectionCode.KILL_SWITCH_ENABLED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


class BrokenState:
    def is_kill_switch_enabled(self):
        raise RuntimeError("state DB unavailable")


class NonBooleanState:
    def is_kill_switch_enabled(self):
        return None  # falsy garbage must NOT read as "disabled"


@pytest.mark.parametrize("state_cls", [BrokenState, NonBooleanState])
def test_kill_switch_query_failures_are_fail_closed(tmp_path, state_cls):
    decision, result, broker, _, _ = run(tmp_path, the_gate=gate(state=state_cls()))
    assert SafetyRejectionCode.STATE_QUERY_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


# --- position limits ---------------------------------------------------------


def position(symbol="USD_JPY", side=PositionSide.LONG, retrieved_at=NOW):
    return PositionInfo(symbol, side, Decimal("1"), Decimal("149"), retrieved_at)


def test_symbol_position_limit_counts_long_and_short_together(tmp_path):
    broker = make_broker()
    broker.set_positions([position(side=PositionSide.LONG), position(side=PositionSide.SHORT)])

    decision, result, broker, _, _ = run(tmp_path, conn=connection(broker))

    assert SafetyRejectionCode.SYMBOL_POSITION_LIMIT_EXCEEDED in codes(decision)
    assert decision.position_count == 2
    assert result is None
    assert broker.submitted_requests == []


def test_account_position_limit_counts_every_symbol(tmp_path):
    broker = make_broker()
    broker.set_positions([position(symbol=f"SYM_{i}") for i in range(5)])  # account limit 5

    decision, result, broker, _, _ = run(tmp_path, conn=connection(broker))

    assert SafetyRejectionCode.ACCOUNT_POSITION_LIMIT_EXCEEDED in codes(decision)
    assert SafetyRejectionCode.SYMBOL_POSITION_LIMIT_EXCEEDED not in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


def test_position_count_below_limits_is_allowed(tmp_path):
    broker = make_broker()
    broker.set_positions([position()])
    decision, result, _, _, _ = run(tmp_path, conn=connection(broker))
    assert decision.allowed is True
    assert decision.position_count == 1
    assert result is not None


@pytest.mark.parametrize(
    "retrieved_at",
    [
        pytest.param(NOW - timedelta(seconds=31), id="stale-position-snapshot"),
        pytest.param(NOW + timedelta(seconds=1), id="future-position-snapshot"),
    ],
)
def test_unreliable_position_snapshots_are_fail_closed(tmp_path, retrieved_at):
    broker = make_broker()
    broker.set_positions([position(retrieved_at=retrieved_at)])
    decision, result, broker, _, _ = run(tmp_path, conn=connection(broker))
    assert SafetyRejectionCode.BROKER_QUERY_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


# --- broker query failures (positions / account) -----------------------------


class BrokenPositionsBroker(FakeBroker):
    def get_positions(self):
        raise RuntimeError("positions unavailable")


class BrokenAccountBroker(FakeBroker):
    def get_account_state(self):
        raise RuntimeError("account unavailable")


class GarbagePositionsBroker(FakeBroker):
    def get_positions(self):
        return ({"symbol": "USD_JPY"},)


class GarbageAccountBroker(FakeBroker):
    def get_account_state(self):
        return {"account_id": "fake-account"}


@pytest.mark.parametrize(
    "broker_cls",
    [
        pytest.param(BrokenPositionsBroker, id="positions-query-fails"),
        pytest.param(BrokenAccountBroker, id="account-query-fails"),
        pytest.param(GarbagePositionsBroker, id="positions-garbage"),
        pytest.param(GarbageAccountBroker, id="account-garbage"),
    ],
)
def test_broker_query_failure_is_fail_closed(tmp_path, broker_cls):
    broker = make_broker(broker_cls)
    decision, result, broker, _, _ = run(tmp_path, conn=connection(broker))
    assert SafetyRejectionCode.BROKER_QUERY_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


@pytest.mark.parametrize(
    "retrieved_at",
    [
        pytest.param(NOW - timedelta(seconds=31), id="stale-account-snapshot"),
        pytest.param(NOW + timedelta(seconds=1), id="future-account-snapshot"),
    ],
)
def test_unreliable_account_snapshots_are_fail_closed(tmp_path, retrieved_at):
    broker = make_broker(account_retrieved_at=retrieved_at)
    decision, result, broker, _, _ = run(tmp_path, conn=connection(broker))
    assert SafetyRejectionCode.BROKER_QUERY_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


# --- unexpected internal failure --------------------------------------------


def test_unexpected_evaluation_failure_is_fail_closed_and_recorded(tmp_path):
    decision, result, broker, reader, _ = run(tmp_path, snapshot="not-a-snapshot")
    assert SafetyRejectionCode.GATE_INTERNAL_ERROR in codes(decision)
    assert result is None
    assert broker.submitted_requests == []
    assert reader.list_for_request("exec-1") == (decision,)


def test_broken_gate_clock_is_fail_closed(tmp_path):
    naive_clock = lambda: datetime(2026, 7, 22, 1, 0)  # noqa: E731 -- naive on purpose
    decision, result, broker, _, _ = run(tmp_path, the_gate=gate(clock=naive_clock))
    assert SafetyRejectionCode.GATE_INTERNAL_ERROR in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


# --- multiple violations, order-preserving dedup -----------------------------


def test_multiple_violations_are_machine_readable_and_collected_together():
    decision = gate(state=StaticSafetyState(kill_switch_enabled=True)).evaluate(
        request(quantity=Decimal("11"), stop_loss_price=None, take_profit_price=None),
        connection=connection(make_broker(target="unknown-endpoint")),
        market=market(market_open=False),
    )
    assert decision.allowed is False
    assert {
        SafetyRejectionCode.QUANTITY_LIMIT_EXCEEDED,
        SafetyRejectionCode.STOP_LOSS_REQUIRED,
        SafetyRejectionCode.MARKET_CLOSED,
        SafetyRejectionCode.KILL_SWITCH_ENABLED,
        SafetyRejectionCode.EXECUTION_MODE_MISMATCH,
    }.issubset(codes(decision))


def test_dedupe_rejections_preserves_first_occurrence_order():
    first = SafetyRejection(SafetyRejectionCode.MARKET_CLOSED, "first")
    duplicate = SafetyRejection(SafetyRejectionCode.MARKET_CLOSED, "second")
    other = SafetyRejection(SafetyRejectionCode.KILL_SWITCH_ENABLED, "kill switch")
    assert dedupe_rejections([first, other, duplicate]) == (first, other)


def test_decision_rejects_duplicate_codes():
    entry = SafetyRejection(SafetyRejectionCode.MARKET_CLOSED, "market is closed")
    with pytest.raises(ValueError, match="unique"):
        SafetyDecision(
            execution_request_id="exec-1", allowed=False, checked_at_utc=NOW,
            policy_id="p-1", rejections=(entry, entry),
        )


# --- logging -----------------------------------------------------------------


def test_rejection_codes_appear_in_the_log(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="execution.live_submission"):
        run(tmp_path, request(quantity=Decimal("11")))
    assert any(
        "quantity_limit_exceeded" in record.getMessage() and "exec-1" in record.getMessage()
        for record in caplog.records
    )


def test_allow_and_submit_are_logged(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="execution.live_submission"):
        run(tmp_path)
    assert any("allowed and submitted" in r.getMessage() for r in caplog.records)


# --- model validation --------------------------------------------------------


def test_policy_content_is_immune_to_caller_side_dict_mutation():
    spreads = {"USD_JPY": Decimal("0.05")}
    targets = {
        ExecutionMode.FAKE: frozenset({"fake"}),
        ExecutionMode.DEMO: frozenset({"oanda-demo"}),
        ExecutionMode.LIVE: frozenset({"oanda-live"}),
    }
    frozen_policy = policy(max_spread_by_symbol=spreads, allowed_connection_targets=targets)

    # Mutate the caller-held originals AFTER construction (Issue #75 review r3
    # Major): the validated policy content must not move under its policy_id.
    spreads["USD_JPY"] = Decimal("2")
    targets[ExecutionMode.FAKE] = frozenset({"unknown-endpoint"})

    assert frozen_policy.max_spread_by_symbol["USD_JPY"] == Decimal("0.05")
    assert frozen_policy.allowed_connection_targets[ExecutionMode.FAKE] == frozenset({"fake"})
    decision = gate(selected_policy=frozen_policy).evaluate(
        request(), connection=connection(), market=market(ask=Decimal("150.10"))
    )
    assert SafetyRejectionCode.SPREAD_LIMIT_EXCEEDED in codes(decision)


def test_policy_mappings_are_read_only():
    frozen_policy = policy()
    with pytest.raises(TypeError):
        frozen_policy.max_spread_by_symbol["USD_JPY"] = Decimal("2")
    with pytest.raises(TypeError):
        frozen_policy.allowed_connection_targets[ExecutionMode.FAKE] = frozenset({"x"})


def test_policy_requires_spread_limit_coverage_for_every_allowed_symbol():
    with pytest.raises(ValueError, match="max_spread_by_symbol"):
        policy(allowed_symbols=frozenset({"USD_JPY", "EUR_USD"}))


def test_policy_rejects_spread_limit_for_disallowed_symbol():
    with pytest.raises(ValueError, match="max_spread_by_symbol"):
        policy(max_spread_by_symbol={"USD_JPY": Decimal("0.05"), "XAU_USD": Decimal("1")})


def test_policy_rejects_one_target_shared_by_two_modes():
    with pytest.raises(ValueError, match="cannot be valid for two execution modes"):
        policy(
            allowed_connection_targets={
                ExecutionMode.FAKE: frozenset({"fake"}),
                ExecutionMode.LIVE: frozenset({"fake"}),
            }
        )


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(dict(policy_id="  "), id="blank-policy-id"),
        pytest.param(dict(allowed_symbols=frozenset()), id="no-symbols"),
        pytest.param(dict(allowed_symbols=frozenset({" USD_JPY"}),
                          max_spread_by_symbol={" USD_JPY": Decimal("0.05")}),
                     id="whitespace-symbol"),
        pytest.param(dict(allowed_sides=frozenset()), id="no-sides"),
        pytest.param(dict(max_order_quantity=Decimal("0")), id="zero-quantity-limit"),
        pytest.param(dict(max_order_quantity=Decimal("NaN")), id="nan-quantity-limit"),
        pytest.param(dict(max_positions_per_symbol=-1), id="negative-symbol-limit"),
        pytest.param(dict(max_positions_account=-1), id="negative-account-limit"),
        pytest.param(dict(max_reference_age=timedelta(0)), id="zero-reference-age"),
        pytest.param(dict(allowed_connection_targets={ExecutionMode.FAKE: frozenset({" fake"})}),
                     id="whitespace-target"),
    ],
)
def test_policy_rejects_invalid_configuration(overrides):
    with pytest.raises(ValueError):
        policy(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(dict(reference_price=Decimal("0")), id="zero-price"),
        pytest.param(dict(reference_price=Decimal("-1")), id="negative-price"),
        pytest.param(dict(reference_price=Decimal("NaN")), id="nan-price"),
        pytest.param(dict(reference_price=Decimal("Infinity")), id="infinite-price"),
        pytest.param(dict(bid=Decimal("150.02")), id="ask-below-bid"),
        pytest.param(dict(observed_at_utc=datetime(2026, 7, 22, 1, 0)), id="naive-timestamp"),
        pytest.param(dict(market_open="yes"), id="non-bool-open"),
        pytest.param(dict(symbol="  "), id="blank-symbol"),
    ],
)
def test_market_snapshot_rejects_invalid_values(overrides):
    with pytest.raises(ValueError):
        market(**overrides)


def test_decision_invariant_allowed_xor_rejections():
    rejection = SafetyRejection(SafetyRejectionCode.MARKET_CLOSED, "market is closed")
    with pytest.raises(ValueError):
        SafetyDecision(
            execution_request_id="exec-1", allowed=True, checked_at_utc=NOW,
            policy_id="p-1", rejections=(rejection,),
        )
    with pytest.raises(ValueError):
        SafetyDecision(
            execution_request_id="exec-1", allowed=False, checked_at_utc=NOW,
            policy_id="p-1", rejections=(),
        )
    with pytest.raises(ValueError):
        SafetyDecision(
            execution_request_id="exec-1", allowed=False,
            checked_at_utc=datetime(2026, 7, 22, 1, 0),  # naive
            policy_id="p-1", rejections=(rejection,),
        )


# --- Issue #76: idempotency / duplicate-order suppression ---------------------


def accepted_result(client_order_id="client-1"):
    """A broker-ACCEPTED snapshot matching the default request()."""
    return OrderResult(
        client_order_id=client_order_id,
        broker_order_id="B-1",
        state=OrderState.ACCEPTED,
        symbol="USD_JPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        requested_quantity=Decimal("1"),
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal("1"),
        requested_price=None,
        average_fill_price=None,
        updated_at_utc=NOW,
        fills=(),
        reject_reason=None,
    )


def result_for_suppressed_state(state):
    filled = Decimal("0")
    fills = ()
    average = None
    broker_order_id = "B-1" if state in {
        OrderState.ACCEPTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
    } else None
    reject_reason = None
    rejection_category = None
    if state is OrderState.PARTIALLY_FILLED:
        filled = Decimal("0.5")
        fills = (Fill("fill-1", Decimal("150"), filled, NOW),)
        average = Decimal("150")
    elif state is OrderState.FILLED:
        filled = Decimal("1")
        fills = (Fill("fill-1", Decimal("150"), filled, NOW),)
        average = Decimal("150")
    elif state is OrderState.REJECTED:
        reject_reason = "broker rejected"
        rejection_category = RejectionCategory.UNCLASSIFIED
    return OrderResult(
        client_order_id="client-1",
        broker_order_id=broker_order_id,
        state=state,
        symbol="USD_JPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        requested_quantity=Decimal("1"),
        filled_quantity=filled,
        remaining_quantity=Decimal("1") - filled,
        requested_price=None,
        average_fill_price=average,
        updated_at_utc=NOW,
        fills=fills,
        reject_reason=reject_reason,
        rejection_category=rejection_category,
    )


def test_same_signal_cannot_register_two_execution_requests(tmp_path):
    # Issue #76: one source signal -> at most one execution request. The
    # second intent (fresh execution_request_id, same signal) is refused by
    # the store's UNIQUE constraint and nothing reaches the broker.
    service, broker, _ = make_service(tmp_path)
    service.register(request(), client_order_id="client-1")
    with pytest.raises(execution_store.ExecutionOrderConflictError):
        service.register(request(execution_request_id="exec-2"), client_order_id="client-2")
    stored = execution_store.get_execution_order_by_signal_id(
        "signal-1", db_path=tmp_path / "orders.sqlite3"
    )
    assert stored["execution_request_id"] == "exec-1"
    assert broker.submitted_requests == []


def test_accepted_request_is_not_resent(tmp_path):
    # reprocessing an intent whose broker state is ACCEPTED must not submit.
    req = request()
    service, broker, _ = make_service(tmp_path)
    service.register(req, client_order_id="client-1")
    order_db = tmp_path / "orders.sqlite3"
    execution_store.mark_submitting(
        req.execution_request_id, reason="submitted elsewhere", changed_at_utc=NOW, db_path=order_db
    )
    execution_store.record_order_result(
        req.execution_request_id, accepted_result(), reason="broker accepted", db_path=order_db
    )

    decision, result = service.submit(req.execution_request_id, market=market())

    assert SafetyRejectionCode.DUPLICATE_SUBMISSION in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


def test_database_locked_during_mark_submitting_never_reaches_broker(tmp_path, monkeypatch):
    import sqlite3

    req = request()
    service, broker, _ = make_service(tmp_path)
    service.register(req, client_order_id="client-1")
    order_db = tmp_path / "orders.sqlite3"
    monkeypatch.setattr(execution_store, "BUSY_TIMEOUT_SECONDS", 0.05)

    blocker = sqlite3.connect(order_db, timeout=0.1, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        decision, result = service.submit(req.execution_request_id, market=market())
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert SafetyRejectionCode.STATE_QUERY_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []
    row = execution_store.get_execution_order(req.execution_request_id, db_path=order_db)
    assert row["submission_phase"] == execution_store.PHASE_CREATED

    # Once the unrelated lock is gone, the same still-created intent can win
    # the atomic transition; no earlier broker attempt occurred.
    retry_decision, retry_result = service.submit(req.execution_request_id, market=market())
    assert retry_decision.allowed is True
    assert retry_result is not None
    assert len(broker.submitted_requests) == 1


@pytest.mark.parametrize(
    "state",
    [
        OrderState.ACCEPTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.UNKNOWN,
        OrderState.CANCELLED,
        OrderState.REJECTED,
    ],
)
def test_every_attempted_or_final_state_is_not_resent(tmp_path, state):
    req = request()
    service, broker, _ = make_service(tmp_path)
    service.register(req, client_order_id="client-1")
    order_db = tmp_path / "orders.sqlite3"
    execution_store.mark_submitting(
        req.execution_request_id, reason="attempt started", changed_at_utc=NOW, db_path=order_db
    )
    if state is OrderState.UNKNOWN:
        execution_store.record_order_state(
            req.execution_request_id,
            state,
            reason="outcome unknown",
            changed_at_utc=NOW,
            db_path=order_db,
        )
    else:
        execution_store.record_order_result(
            req.execution_request_id,
            result_for_suppressed_state(state),
            reason="broker observation",
            db_path=order_db,
        )

    decision, result = service.submit(req.execution_request_id, market=market())

    assert SafetyRejectionCode.DUPLICATE_SUBMISSION in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


def test_restart_immediately_after_mark_submitting_never_resends(tmp_path):
    req = request()
    first_service, _, _ = make_service(tmp_path)
    first_service.register(req, client_order_id="client-1")
    execution_store.mark_submitting(
        req.execution_request_id,
        reason="process stopped before broker call",
        changed_at_utc=NOW,
        db_path=tmp_path / "orders.sqlite3",
    )

    restarted_service, restarted_broker, _ = make_service(tmp_path)
    decision, result = restarted_service.submit(req.execution_request_id, market=market())

    assert SafetyRejectionCode.DUPLICATE_SUBMISSION in codes(decision)
    assert result is None
    assert restarted_broker.submitted_requests == []
    unresolved = execution_store.list_unresolved_submissions(
        db_path=tmp_path / "orders.sqlite3"
    )
    assert [row["execution_request_id"] for row in unresolved] == [req.execution_request_id]


def test_submitted_request_is_not_resent_after_a_service_restart(tmp_path):
    # a brand-new service over the SAME persisted stores (process restart
    # equivalent) must still suppress the duplicate: the judgement lives in
    # the DB, not in process memory.
    req = request()
    first_service, first_broker, _ = make_service(tmp_path)
    first_service.register(req, client_order_id="client-1")
    first_decision, first_result = first_service.submit(req.execution_request_id, market=market())
    assert first_decision.allowed is True and first_result is not None
    assert len(first_broker.submitted_requests) == 1

    restarted_service, restarted_broker, _ = make_service(tmp_path)
    decision, result = restarted_service.submit(req.execution_request_id, market=market())

    assert SafetyRejectionCode.DUPLICATE_SUBMISSION in codes(decision)
    assert result is None
    assert restarted_broker.submitted_requests == []  # nothing re-sent after restart


def test_unknown_request_is_not_resent_after_a_service_restart(tmp_path):
    req = request()
    first_service, first_broker, _ = make_service(tmp_path)
    first_service.register(req, client_order_id="client-1")
    first_broker.script_order("client-1", TimeoutOrder())
    with pytest.raises(BrokerTimeoutError):
        first_service.submit(req.execution_request_id, market=market())
    row = execution_store.get_execution_order(
        req.execution_request_id, db_path=tmp_path / "orders.sqlite3"
    )
    assert row["order_state"] == OrderState.UNKNOWN.value  # timeout persisted as UNKNOWN

    restarted_service, restarted_broker, _ = make_service(tmp_path)
    decision, result = restarted_service.submit(req.execution_request_id, market=market())

    assert SafetyRejectionCode.DUPLICATE_SUBMISSION in codes(decision)
    assert result is None
    assert restarted_broker.submitted_requests == []


def test_two_concurrent_services_reach_the_broker_exactly_once(tmp_path, monkeypatch):
    # Two independent services (separate decision stores, independent SQLite
    # connections -- multi-process equivalent) race to submit the same
    # intent against ONE broker: the created -> submitting transition is won
    # exactly once, so the broker sees exactly one request.
    class ThreadSafeRequestLog(list):
        def __init__(self):
            super().__init__()
            self.lock = threading.Lock()

        def append(self, item):
            with self.lock:
                super().append(item)

        def __len__(self):
            with self.lock:
                return super().__len__()

    shared_broker = make_broker()
    shared_broker.submitted_requests = ThreadSafeRequestLog()
    req = request()
    order_db = tmp_path / "orders.sqlite3"
    journal_db = tmp_path / "journal.sqlite3"
    SubmissionJournal(journal_db).initialize()
    services = []
    for name in ("a", "b"):
        decision_db = tmp_path / f"safety-{name}.sqlite3"
        SQLiteSafetyDecisionStore(decision_db).initialize()
        services.append(
            LiveOrderSubmissionService(
                connection=BrokerConnection(broker=shared_broker),
                gate=gate(),
                decision_db_path=decision_db,
                order_db_path=order_db,
                journal_db_path=journal_db,
                clock=lambda: NOW,
            )
        )
    services[0].register(req, client_order_id="client-1")

    barrier = threading.Barrier(2)
    original_evaluate = SafetyGate.evaluate

    def synchronized_evaluate(self, *args, **kwargs):
        # Both services have already read phase=created and loaded the request.
        # Release them together immediately before decision persistence and
        # the atomic mark_submitting race.
        barrier.wait(timeout=10)
        return original_evaluate(self, *args, **kwargs)

    monkeypatch.setattr(SafetyGate, "evaluate", synchronized_evaluate)
    outcomes = []
    errors = []
    outcome_lock = threading.Lock()

    def attempt(service):
        try:
            outcome = service.submit(req.execution_request_id, market=market())
            with outcome_lock:
                outcomes.append(outcome)
        except BaseException as exc:
            with outcome_lock:
                errors.append(exc)

    threads = [threading.Thread(target=attempt, args=(s,)) for s in services]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert all(not t.is_alive() for t in threads)
    assert errors == []
    submitted = [result for _, result in outcomes if result is not None]
    suppressed = [decision for decision, result in outcomes if result is None]
    assert len(submitted) == 1
    assert len(suppressed) == 1
    assert SafetyRejectionCode.DUPLICATE_SUBMISSION in codes(suppressed[0])
    assert len(shared_broker.submitted_requests) == 1


def test_broker_call_holds_no_sqlite_write_transaction(tmp_path):
    order_db = tmp_path / "orders.sqlite3"

    class LockProbingRequestLog(list):
        def append(self, item):
            import sqlite3

            with sqlite3.connect(order_db, timeout=0.1, isolation_level=None) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("ROLLBACK")
            super().append(item)

    broker = make_broker()
    broker.submitted_requests = LockProbingRequestLog()
    service, _, _ = make_service(tmp_path, conn=BrokerConnection(broker=broker))
    service.register(request(), client_order_id="client-1")

    decision, result = service.submit("exec-1", market=market())

    assert decision.allowed is True
    assert result is not None
    assert len(broker.submitted_requests) == 1


def test_duplicate_suppression_log_carries_ids_state_and_reason(tmp_path, caplog):
    # Issue #76 log contract: execution_request_id, source signal id, the
    # current state and the suppression reason are all in the log.
    req = request()
    service, broker, _ = make_service(tmp_path)
    service.register(req, client_order_id="client-1")
    service.submit(req.execution_request_id, market=market())
    with caplog.at_level(logging.WARNING, logger="execution.live_submission"):
        service.submit(req.execution_request_id, market=market())
    message = "\n".join(r.getMessage() for r in caplog.records)
    assert "duplicate submission suppressed" in message
    assert "execution_request_id=exec-1" in message
    assert "signal_id=signal-1" in message
    assert "submission_phase=" in message
    assert "order_state=" in message
    assert "reason=" in message
    assert len(broker.submitted_requests) == 1
