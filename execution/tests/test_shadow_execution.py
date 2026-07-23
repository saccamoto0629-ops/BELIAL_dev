"""Issue #78: Shadow Mode -- observation-only run of the normal decision path.

The central contract under test: the REAL ExecutionRequest + SafetyGate
decision path runs, the outcome is persisted, and the broker submission
boundary is NEVER crossed -- proven with a spy broker whose submit-family
APIs fail the test the instant they are touched. Cross-mode independence
(shadow never pollutes -- and is never suppressed by -- the real
submission idempotency, Issue #76) is exercised through the public
services. All SQLite lives under tmp_path; FakeBroker only, no network.
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from broker.broker_models import AccountState, BrokerConnectionIdentity, OrderSide, OrderType
from broker.fake_broker import FakeBroker
from execution.execution_models import ExecutionRequest
from execution.live_submission import LiveOrderSubmissionService
from execution.safety_gate import (
    BrokerConnection,
    ExecutionMode,
    MarketSafetySnapshot,
    SafetyGate,
    SafetyPolicy,
    SafetyRejectionCode,
    StaticSafetyState,
)
from execution.safety_store import SQLiteSafetyDecisionStore
from execution.shadow_execution import ShadowExecutionService
from execution.shadow_store import (
    ShadowExecutionConflictError,
    ShadowStoreError,
    SQLiteShadowExecutionStore,
)

NOW = datetime(2026, 7, 22, 1, 0, tzinfo=timezone.utc)


class SubmitForbiddenFakeBroker(FakeBroker):
    """Spy: every submit-family call fails the test INSTANTLY and is counted.

    Read-only safety queries (positions / account / identity) keep working --
    they are part of the normal gate evaluation, not the submission boundary.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.submit_calls = 0
        self.cancel_calls = 0

    def submit_order(self, request):
        self.submit_calls += 1
        raise AssertionError("Broker.submit_order must never be reached in shadow mode")

    def cancel_order(self, client_order_id):
        self.cancel_calls += 1
        raise AssertionError("Broker.cancel_order must never be reached in shadow mode")


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


def make_broker(cls=SubmitForbiddenFakeBroker, *, target="fake"):
    broker = cls(default_fill_price=Decimal("150"), clock=lambda: NOW, connection_target=target)
    broker.set_account_state(account_state())
    return broker


def gate(*, state=None, selected_policy=None, clock=None):
    return SafetyGate(
        policy=selected_policy or policy(),
        state_provider=state or StaticSafetyState(),
        clock=clock or (lambda: NOW),
    )


def make_service(tmp_path, *, broker=None, the_gate=None, initialize=True, clock=None):
    broker = broker if broker is not None else make_broker()
    service = ShadowExecutionService(
        connection=BrokerConnection(broker=broker),
        gate=the_gate or gate(),
        shadow_db_path=tmp_path / "shadow.sqlite3",
        clock=clock or (lambda: NOW),
    )
    if initialize:
        service.initialize_store()
    # Reader handle for assertions only (the service's own store is sealed).
    return service, broker, SQLiteShadowExecutionStore(tmp_path / "shadow.sqlite3")


def observe(tmp_path, req=None, *, snapshot=None, **service_kwargs):
    req = req if req is not None else request()
    service, broker, reader = make_service(tmp_path, **service_kwargs)
    record = service.observe(req, market=snapshot if snapshot is not None else market())
    return record, broker, reader, service


def codes(record):
    return {item.code for item in record.decision.rejections}


# --- 1. shadow + Safety Gate allow --------------------------------------------


def test_allowed_shadow_run_persists_the_record_and_never_touches_submit(tmp_path):
    record, broker, reader, _ = observe(tmp_path)

    assert record.decision.allowed is True
    assert record.decision.rejections == ()
    assert record.execution_mode is ExecutionMode.SHADOW
    assert broker.submit_calls == 0 and broker.cancel_calls == 0
    assert broker.submitted_requests == []
    stored = reader.get_by_execution_request_id("exec-1")
    assert stored == record
    assert stored.request == request()
    assert stored.intended_order == {
        "symbol": "USD_JPY",
        "side": "buy",
        "order_type": OrderType.MARKET.value,
        "quantity": "1",
        "limit_price": None,
    }


def test_allowed_record_carries_the_decision_audit_fields(tmp_path):
    record, _, _, _ = observe(tmp_path)
    assert record.decision.policy_id == "policy-test-1"
    assert record.decision.execution_mode == "fake"  # the CONNECTION the gate ran against
    assert record.decision.connection_target == "fake"
    assert record.decision.account_id == "fake-account"
    assert record.decision.checked_at_utc == NOW
    assert record.observed_at_utc == NOW


def test_shadow_uses_the_real_gate_not_a_copy(tmp_path):
    # The same policy misconfiguration the live gate would reject is
    # rejected identically in shadow: no shadow-only judgment exists.
    record, broker, _, _ = observe(
        tmp_path, request(symbol="EUR_USD", stop_loss_price=None, take_profit_price=None)
    )
    assert record.decision.allowed is False
    assert SafetyRejectionCode.SYMBOL_NOT_ALLOWED in codes(record)
    assert SafetyRejectionCode.STOP_LOSS_REQUIRED in codes(record)
    assert broker.submit_calls == 0


# --- 2. shadow + Safety Gate reject ---------------------------------------------


def test_rejected_shadow_run_persists_codes_and_messages(tmp_path):
    record, broker, reader, _ = observe(tmp_path, request(quantity=Decimal("999")))

    assert record.decision.allowed is False
    assert SafetyRejectionCode.QUANTITY_LIMIT_EXCEEDED in codes(record)
    assert broker.submit_calls == 0
    stored = reader.get_by_execution_request_id("exec-1")
    assert stored.decision.rejection_codes == record.decision.rejection_codes
    assert all(item.message for item in stored.decision.rejections)


# --- 3. shadow + kill switch ------------------------------------------------------


def test_kill_switch_fails_closed_and_the_rejection_is_persisted(tmp_path):
    record, broker, reader, _ = observe(
        tmp_path, the_gate=gate(state=StaticSafetyState(kill_switch_enabled=True))
    )
    assert record.decision.allowed is False
    assert SafetyRejectionCode.KILL_SWITCH_ENABLED in codes(record)
    assert broker.submit_calls == 0
    assert reader.get_by_execution_request_id("exec-1").decision.rejection_codes == (
        record.decision.rejection_codes
    )


# --- 4. shadow + external-state anomalies (fail closed) ---------------------------


def test_stale_reference_price_fails_closed_and_is_persisted(tmp_path):
    record, broker, reader, _ = observe(
        tmp_path, snapshot=market(observed_at_utc=NOW - timedelta(seconds=31))
    )
    assert record.decision.allowed is False
    assert SafetyRejectionCode.REFERENCE_PRICE_STALE in codes(record)
    assert broker.submit_calls == 0
    assert reader.get_by_execution_request_id("exec-1") == record


def test_future_dated_snapshot_fails_closed(tmp_path):
    record, broker, _, _ = observe(
        tmp_path, snapshot=market(observed_at_utc=NOW + timedelta(seconds=5))
    )
    assert SafetyRejectionCode.REFERENCE_TIMESTAMP_FUTURE in codes(record)
    assert broker.submit_calls == 0


def test_broker_query_failure_fails_closed(tmp_path):
    class PositionsBroken(SubmitForbiddenFakeBroker):
        def get_positions(self):
            raise RuntimeError("positions endpoint down")

    record, broker, _, _ = observe(tmp_path, broker=make_broker(PositionsBroken))
    assert record.decision.allowed is False
    assert SafetyRejectionCode.BROKER_QUERY_FAILED in codes(record)
    assert broker.submit_calls == 0


def test_gate_internal_error_is_recorded_fail_closed(tmp_path):
    record, broker, reader, _ = observe(tmp_path, the_gate=gate(clock=lambda: "broken"))
    assert record.decision.allowed is False
    assert codes(record) == {SafetyRejectionCode.GATE_INTERNAL_ERROR}
    assert broker.submit_calls == 0
    assert reader.get_by_execution_request_id("exec-1").decision.allowed is False


# --- persistence is fail-closed ------------------------------------------------


def test_uninitialized_shadow_store_fails_the_observation_without_creating_files(tmp_path):
    service, broker, reader = make_service(tmp_path, initialize=False)
    with pytest.raises(ShadowStoreError, match="initialize"):
        service.observe(request(), market=market())
    assert broker.submit_calls == 0
    assert not (tmp_path / "shadow.sqlite3").exists()
    assert reader.list_records() == ()


# --- idempotency: within shadow and across modes -----------------------------------


def test_re_running_shadow_for_the_same_intent_is_suppressed(tmp_path, caplog):
    record, broker, reader, service = observe(tmp_path)
    with caplog.at_level(logging.WARNING, logger="execution.shadow_store"):
        with pytest.raises(ShadowExecutionConflictError):
            service.observe(request(), market=market())
    assert len(reader.list_records()) == 1
    assert reader.get_by_execution_request_id("exec-1") == record
    assert broker.submit_calls == 0
    assert any("duplicate shadow execution suppressed" in message for message in caplog.messages)


def test_re_running_shadow_for_the_same_signal_is_suppressed(tmp_path):
    _, _, reader, service = observe(tmp_path)
    with pytest.raises(ShadowExecutionConflictError):
        service.observe(request(execution_request_id="exec-2"), market=market())
    assert len(reader.list_records()) == 1


def test_two_services_on_the_same_db_keep_exactly_one_record(tmp_path):
    _, _, reader, _ = observe(tmp_path)
    second, broker2, _ = make_service(tmp_path, initialize=False)
    with pytest.raises(ShadowExecutionConflictError):
        second.observe(request(), market=market())
    assert len(reader.list_records()) == 1
    assert broker2.submit_calls == 0


def _live_service(tmp_path, req):
    """A REAL (fake-mode) submission service on its own stores."""
    decision_db = tmp_path / "safety.sqlite3"
    SQLiteSafetyDecisionStore(decision_db).initialize()
    live_broker = FakeBroker(
        default_fill_price=Decimal("150"), clock=lambda: NOW, connection_target="fake"
    )
    live_broker.set_account_state(account_state())
    service = LiveOrderSubmissionService(
        connection=BrokerConnection(broker=live_broker),
        gate=gate(),
        decision_db_path=decision_db,
        order_db_path=tmp_path / "orders.sqlite3",
        clock=lambda: NOW,
    )
    service.register(req, client_order_id="client-1")
    return service, live_broker


def test_a_shadow_observed_signal_can_still_be_submitted_in_fake_mode(tmp_path):
    _, shadow_broker, _, _ = observe(tmp_path)

    live, live_broker = _live_service(tmp_path, request())
    decision, result = live.submit("exec-1", market=market())
    assert decision.allowed is True
    assert result is not None
    assert len(live_broker.submitted_requests) == 1  # shadow did not suppress it
    assert shadow_broker.submit_calls == 0


def test_an_already_submitted_signal_can_still_be_shadow_observed(tmp_path):
    live, live_broker = _live_service(tmp_path, request())
    decision, result = live.submit("exec-1", market=market())
    assert decision.allowed and result is not None

    record, shadow_broker, reader, _ = observe(tmp_path)
    assert record.decision.allowed is True  # no cross-mode duplicate suppression
    assert len(reader.list_records()) == 1
    assert shadow_broker.submit_calls == 0
    assert len(live_broker.submitted_requests) == 1


def test_shadow_never_writes_the_execution_order_store(tmp_path):
    from execution import execution_store

    observe(tmp_path)
    order_db = tmp_path / "orders.sqlite3"
    assert not order_db.exists()
    assert execution_store.get_execution_order_by_signal_id("signal-1", db_path=order_db) is None


# --- mode validation ---------------------------------------------------------------


def test_execution_modes_are_distinct_and_unknown_modes_fail_fast():
    assert ExecutionMode("shadow") is ExecutionMode.SHADOW
    assert {mode.value for mode in ExecutionMode} == {"shadow", "fake", "demo", "live"}
    with pytest.raises(ValueError):
        ExecutionMode("paper")
    with pytest.raises(ValueError):
        ExecutionMode("SHADOW")


def test_no_broker_may_claim_the_shadow_mode():
    class ShadowClaimingBroker(FakeBroker):
        def connection_identity(self):
            return BrokerConnectionIdentity(mode="shadow", target="shadow-target")

    with pytest.raises(ValueError, match="no broker implementation is registered"):
        BrokerConnection(broker=make_broker(ShadowClaimingBroker))


def test_identity_drift_toward_shadow_is_rejected_by_the_gate(tmp_path):
    broker = make_broker()
    connection = BrokerConnection(broker=broker)
    broker._connection_identity = BrokerConnectionIdentity(mode="shadow", target="fake")

    service = ShadowExecutionService(
        connection=connection,
        gate=gate(),
        shadow_db_path=tmp_path / "shadow.sqlite3",
        clock=lambda: NOW,
    )
    service.initialize_store()
    record = service.observe(request(), market=market())
    assert record.decision.allowed is False
    assert SafetyRejectionCode.EXECUTION_MODE_MISMATCH in codes(record)
    assert broker.submit_calls == 0


def test_policy_refuses_connection_targets_for_the_shadow_mode():
    with pytest.raises(ValueError, match="must not contain ExecutionMode.SHADOW"):
        policy(
            allowed_connection_targets={
                ExecutionMode.FAKE: frozenset({"fake"}),
                ExecutionMode.SHADOW: frozenset({"anywhere"}),
            }
        )


# --- service construction ------------------------------------------------------------


def test_service_constructor_validates_its_arguments(tmp_path):
    connection = BrokerConnection(broker=make_broker())
    with pytest.raises(ValueError, match="connection must be"):
        ShadowExecutionService(
            connection="nope", gate=gate(), shadow_db_path=tmp_path / "s.sqlite3", clock=lambda: NOW
        )
    with pytest.raises(ValueError, match="gate must be"):
        ShadowExecutionService(
            connection=connection, gate="nope", shadow_db_path=tmp_path / "s.sqlite3", clock=lambda: NOW
        )
    with pytest.raises(ValueError, match="clock must be"):
        ShadowExecutionService(
            connection=connection, gate=gate(), shadow_db_path=tmp_path / "s.sqlite3", clock="nope"
        )


def test_the_shadow_store_is_sealed_inside_the_service(tmp_path):
    connection = BrokerConnection(broker=make_broker())
    recorder = SQLiteShadowExecutionStore(tmp_path / "s.sqlite3")
    with pytest.raises(ValueError, match="filesystem path"):
        ShadowExecutionService(
            connection=connection, gate=gate(), shadow_db_path=recorder, clock=lambda: NOW
        )


def test_observe_validates_the_request_type(tmp_path):
    service, _, _ = make_service(tmp_path)
    with pytest.raises(ValueError, match="request must be"):
        service.observe("nope", market=market())


def test_a_broken_service_clock_fails_the_observation_without_saving(tmp_path):
    service, broker, reader = make_service(tmp_path, clock=lambda: "broken")
    with pytest.raises(ValueError, match="clock must return"):
        service.observe(request(), market=market())
    assert broker.submit_calls == 0
    assert reader.list_records() == ()
