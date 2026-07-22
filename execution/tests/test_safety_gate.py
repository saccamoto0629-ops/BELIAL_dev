"""Issue #75: fail-closed pre-submit Safety Gate + LiveOrderSubmissionService.

Every safety condition is tested independently through the SINGLE sanctioned
submission path; every rejection path asserts that FakeBroker received ZERO
submissions. FakeBroker performs no external I/O and all persistence lives
under tmp_path, so none of these tests can reach a network, Discord, S3 or a
real DB.
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from broker.broker_models import (
    AccountState,
    BrokerCommunicationError,
    BrokerTimeoutError,
    OrderSide,
    OrderState,
    OrderType,
    PositionInfo,
    PositionSide,
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


def make_broker(cls=FakeBroker, *, account_retrieved_at=NOW):
    broker = cls(default_fill_price=Decimal("150"), clock=lambda: NOW)
    broker.set_account_state(account_state(account_retrieved_at))
    return broker


def connection(broker=None, *, target="fake"):
    return BrokerConnection(
        broker=broker if broker is not None else make_broker(),
        execution_mode=ExecutionMode.FAKE,
        connection_target=target,
    )


def gate(*, state=None, selected_policy=None, clock=None):
    return SafetyGate(
        policy=selected_policy or policy(),
        state_provider=state or StaticSafetyState(),
        clock=clock or (lambda: NOW),
    )


def make_service(tmp_path, *, conn=None, the_gate=None, decision_store=None):
    store = decision_store
    if store is None:
        store = SQLiteSafetyDecisionStore(tmp_path / "safety.sqlite3")
        store.initialize()
    conn = conn if conn is not None else connection()
    service = LiveOrderSubmissionService(
        connection=conn,
        gate=the_gate or gate(),
        decision_store=store,
        order_db_path=tmp_path / "orders.sqlite3",
        clock=lambda: NOW,
    )
    return service, conn.broker, store


def run(tmp_path, req=None, *, snapshot=None, register=True, **service_kwargs):
    """Register (unless told not to) and submit through the single service
    boundary; returns (decision, result, broker, decision_store, service)."""
    req = req if req is not None else request()
    service, broker, store = make_service(tmp_path, **service_kwargs)
    if register:
        service.register(req, client_order_id="client-1")
    decision, result = service.submit(
        req.execution_request_id,
        market=snapshot if snapshot is not None else market(),
    )
    return decision, result, broker, store, service


def codes(decision):
    return {item.code for item in decision.rejections}


# --- normal path -------------------------------------------------------------


def test_normal_conditions_submit_to_fake_broker_without_external_io(tmp_path):
    decision, result, broker, store, _ = run(tmp_path)

    assert decision.allowed is True
    assert decision.rejections == ()
    assert result is not None
    assert result.state is OrderState.FILLED
    assert len(broker.submitted_requests) == 1
    assert store.list_for_request("exec-1") == (decision,)


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
    service, broker, store = make_service(tmp_path)
    service.register(req, client_order_id="client-1")

    first, first_result = service.submit(req.execution_request_id, market=market())
    second, second_result = service.submit(req.execution_request_id, market=market())

    assert first.allowed is True and first_result is not None
    assert SafetyRejectionCode.DUPLICATE_SUBMISSION in codes(second)
    assert second_result is None
    assert len(broker.submitted_requests) == 1
    assert store.list_for_request(req.execution_request_id) == (first, second)


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


def test_post_submit_persistence_failure_surfaces_loudly(tmp_path, monkeypatch):
    req = request()
    service, broker, _ = make_service(tmp_path)
    service.register(req, client_order_id="client-1")

    def broken_record_order_result(*args, **kwargs):
        raise RuntimeError("order DB gone after submit")

    monkeypatch.setattr(execution_store, "record_order_result", broken_record_order_result)
    with pytest.raises(RuntimeError, match="order DB gone"):
        service.submit(req.execution_request_id, market=market())
    # The order WAS submitted -- the failure must never be masked as a rejection.
    assert len(broker.submitted_requests) == 1


# --- intent registration / execution store integration -----------------------


def test_unregistered_intent_is_rejected_before_the_broker(tmp_path):
    decision, result, broker, store, _ = run(tmp_path, register=False)
    assert SafetyRejectionCode.INTENT_NOT_REGISTERED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []
    assert store.list_for_request("exec-1") == (decision,)


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


# --- durable decision recording with read-back -------------------------------


class LostCommitStore(SQLiteSafetyDecisionStore):
    """record() appears to work but nothing can be read back."""

    def list_for_request(self, execution_request_id):
        return ()


class NoOpRecordStore(SQLiteSafetyDecisionStore):
    """A silently swallowing recorder -- must never enable a submit."""

    def record(self, decision):
        return None


@pytest.mark.parametrize("store_cls", [LostCommitStore, NoOpRecordStore])
def test_decision_readback_failure_prevents_submit(tmp_path, store_cls):
    store = store_cls(tmp_path / "safety.sqlite3")
    store.initialize()
    decision, result, broker, _, _ = run(tmp_path, decision_store=store)

    assert SafetyRejectionCode.DECISION_PERSIST_FAILED in codes(decision)
    assert decision.allowed is False
    assert result is None
    assert broker.submitted_requests == []


def test_uninitialized_decision_store_prevents_submit(tmp_path):
    store = SQLiteSafetyDecisionStore(tmp_path / "never-initialized.sqlite3")
    decision, result, broker, _, _ = run(tmp_path, decision_store=store)
    assert decision.rejection_codes == (SafetyRejectionCode.DECISION_PERSIST_FAILED.value,)
    assert result is None
    assert broker.submitted_requests == []


def test_persistence_failure_keeps_the_original_rejection_codes(tmp_path):
    store = SQLiteSafetyDecisionStore(tmp_path / "never-initialized.sqlite3")
    decision, result, broker, _, _ = run(
        tmp_path, request(quantity=Decimal("11")), decision_store=store
    )
    assert SafetyRejectionCode.QUANTITY_LIMIT_EXCEEDED in codes(decision)
    assert SafetyRejectionCode.DECISION_PERSIST_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


def test_service_rejects_non_durable_recorders(tmp_path):
    class InMemoryRecorder:
        def record(self, decision): ...
        def list_for_request(self, execution_request_id): return ()

    with pytest.raises(ValueError, match="SQLiteSafetyDecisionStore"):
        LiveOrderSubmissionService(
            connection=connection(),
            gate=gate(),
            decision_store=InMemoryRecorder(),
            order_db_path=tmp_path / "orders.sqlite3",
            clock=lambda: NOW,
        )


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
            SafetyRejectionCode.EXECUTION_MODE_MISMATCH, id="fake-mode-live-target"),
        pytest.param(
            request, market, policy, "oanda-demo",
            SafetyRejectionCode.EXECUTION_MODE_MISMATCH, id="fake-mode-demo-target"),
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
    decision, result, broker, store, _ = run(
        tmp_path,
        req_factory(),
        snapshot=snapshot_factory(),
        conn=connection(target=target),
        the_gate=gate(selected_policy=policy_factory()),
    )
    assert expected in codes(decision)
    assert decision.allowed is False
    assert result is None
    assert broker.submitted_requests == []
    assert store.list_for_request("exec-1") == (decision,)


# --- typed connection identity ----------------------------------------------


class StubBroker:
    """Satisfies the Broker protocol structurally but is not a FakeBroker."""

    def submit_order(self, request): ...
    def cancel_order(self, client_order_id): ...
    def get_order(self, client_order_id): ...
    def get_positions(self): ...
    def get_account_state(self): ...


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        pytest.param(dict(execution_mode=ExecutionMode.LIVE, connection_target="oanda-live"),
                     "no broker implementation is registered", id="fakebroker-cannot-claim-live"),
        pytest.param(dict(execution_mode=ExecutionMode.DEMO, connection_target="oanda-demo"),
                     "no broker implementation is registered", id="fakebroker-cannot-claim-demo"),
        pytest.param(dict(execution_mode="live", connection_target="oanda-live"),
                     "ExecutionMode", id="unknown-mode-string"),
        pytest.param(dict(execution_mode=ExecutionMode.FAKE, connection_target=" fake "),
                     "whitespace", id="whitespace-target"),
        pytest.param(dict(execution_mode=ExecutionMode.FAKE, connection_target=""),
                     "non-empty", id="empty-target"),
    ],
)
def test_broker_connection_rejects_invalid_identity(kwargs, match):
    with pytest.raises(ValueError, match=match):
        BrokerConnection(broker=make_broker(), **kwargs)


def test_broker_connection_rejects_non_fakebroker_claiming_fake_mode():
    with pytest.raises(ValueError, match="requires a FakeBroker instance"):
        BrokerConnection(
            broker=StubBroker(), execution_mode=ExecutionMode.FAKE, connection_target="fake"
        )


def test_evaluate_rechecks_the_typed_identity_fail_closed():
    # Bypass the frozen constructor on purpose to prove the gate re-checks.
    conn = connection()
    object.__setattr__(conn, "execution_mode", ExecutionMode.LIVE)
    decision = gate().evaluate(request(), connection=conn, market=market())
    assert SafetyRejectionCode.EXECUTION_MODE_MISMATCH in codes(decision)
    # Both the identity re-check and the policy target check fire; dedup keeps one.
    assert decision.rejection_codes.count(
        SafetyRejectionCode.EXECUTION_MODE_MISMATCH.value
    ) == 1


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
    decision, result, broker, store, _ = run(tmp_path, snapshot="not-a-snapshot")
    assert SafetyRejectionCode.GATE_INTERNAL_ERROR in codes(decision)
    assert result is None
    assert broker.submitted_requests == []
    assert store.list_for_request("exec-1") == (decision,)


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
        connection=connection(target="unknown-endpoint"),
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
