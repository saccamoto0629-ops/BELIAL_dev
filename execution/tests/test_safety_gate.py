from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from broker.broker_models import AccountState, OrderSide, OrderType, PositionInfo, PositionSide
from broker.fake_broker import FakeBroker
from execution.execution_models import ExecutionRequest
from execution.safety_gate import (
    ExecutionMode,
    InMemorySafetyDecisionRecorder,
    MarketSafetySnapshot,
    SafetyGate,
    SafetyPolicy,
    SafetyRejectionCode,
    StaticSafetyState,
    submit_after_safety_gate,
)

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
        allowed_symbols=frozenset({"USD_JPY"}),
        allowed_sides=frozenset({OrderSide.BUY, OrderSide.SELL}),
        max_order_quantity=Decimal("10"),
        max_positions_per_symbol=2,
        max_positions_account=5,
        max_reference_age=timedelta(seconds=30),
        max_spread_by_symbol={"USD_JPY": Decimal("0.05")},
        allowed_connection_targets={
            ExecutionMode.PAPER: frozenset({"fake"}),
            ExecutionMode.DEMO: frozenset({"oanda-demo"}),
            ExecutionMode.LIVE: frozenset({"oanda-live"}),
        },
    )
    values.update(overrides)
    return SafetyPolicy(**values)


def gate(*, state=None, selected_policy=None):
    return SafetyGate(
        policy=selected_policy or policy(),
        state_provider=state or StaticSafetyState(),
        clock=lambda: NOW,
    )


def codes(decision):
    return {item.code for item in decision.rejections}


def test_normal_conditions_submit_to_fake_broker_without_external_io():
    broker = FakeBroker(default_fill_price=Decimal("150"))
    recorder = InMemorySafetyDecisionRecorder()

    decision, result = submit_after_safety_gate(
        request(),
        client_order_id="client-1",
        broker=broker,
        gate=gate(),
        recorder=recorder,
        market=market(),
        execution_mode=ExecutionMode.PAPER,
        connection_target="fake",
        created_at_utc=NOW,
    )

    assert decision.allowed is True
    assert result is not None
    assert len(broker.submitted_requests) == 1
    assert recorder.decisions == [decision]


@pytest.mark.parametrize(
    ("req", "snapshot", "selected_policy", "mode", "target", "expected"),
    [
        (request(symbol="XAU_USD"), market(symbol="XAU_USD"), policy(), ExecutionMode.PAPER, "fake", SafetyRejectionCode.SYMBOL_NOT_ALLOWED),
        (request(side=OrderSide.SELL, stop_loss_price=Decimal("151"), take_profit_price=Decimal("149")), market(), policy(allowed_sides=frozenset({OrderSide.BUY})), ExecutionMode.PAPER, "fake", SafetyRejectionCode.SIDE_NOT_ALLOWED),
        (request(quantity=Decimal("11")), market(), policy(), ExecutionMode.PAPER, "fake", SafetyRejectionCode.QUANTITY_LIMIT_EXCEEDED),
        (request(stop_loss_price=None), market(), policy(), ExecutionMode.PAPER, "fake", SafetyRejectionCode.STOP_LOSS_REQUIRED),
        (request(stop_loss_price=Decimal("150.5")), market(), policy(), ExecutionMode.PAPER, "fake", SafetyRejectionCode.STOP_LOSS_INVALID),
        (request(take_profit_price=Decimal("149.5")), market(), policy(), ExecutionMode.PAPER, "fake", SafetyRejectionCode.TAKE_PROFIT_INVALID),
        (request(), market(reference_price=None), policy(), ExecutionMode.PAPER, "fake", SafetyRejectionCode.REFERENCE_PRICE_MISSING),
        (request(), market(observed_at_utc=NOW - timedelta(seconds=31)), policy(), ExecutionMode.PAPER, "fake", SafetyRejectionCode.REFERENCE_PRICE_STALE),
        (request(), market(bid=None), policy(), ExecutionMode.PAPER, "fake", SafetyRejectionCode.SPREAD_MISSING),
        (request(), market(ask=Decimal("150.10")), policy(), ExecutionMode.PAPER, "fake", SafetyRejectionCode.SPREAD_LIMIT_EXCEEDED),
        (request(), market(market_open=False), policy(), ExecutionMode.PAPER, "fake", SafetyRejectionCode.MARKET_CLOSED),
        (request(), market(tradable=False), policy(), ExecutionMode.PAPER, "fake", SafetyRejectionCode.SYMBOL_NOT_TRADABLE),
        (request(), market(), policy(), ExecutionMode.LIVE, "fake", SafetyRejectionCode.EXECUTION_MODE_MISMATCH),
    ],
)
def test_each_static_condition_rejects_before_submit(req, snapshot, selected_policy, mode, target, expected):
    broker = FakeBroker(default_fill_price=Decimal("150"))
    recorder = InMemorySafetyDecisionRecorder()

    decision, result = submit_after_safety_gate(
        req,
        client_order_id="client-1",
        broker=broker,
        gate=gate(selected_policy=selected_policy),
        recorder=recorder,
        market=snapshot,
        execution_mode=mode,
        connection_target=target,
        created_at_utc=NOW,
    )

    assert expected in codes(decision)
    assert result is None
    assert broker.submitted_requests == []
    assert recorder.decisions == [decision]


def test_kill_switch_rejects_every_new_order():
    broker = FakeBroker(default_fill_price=Decimal("150"))
    recorder = InMemorySafetyDecisionRecorder()

    decision, result = submit_after_safety_gate(
        request(),
        client_order_id="client-1",
        broker=broker,
        gate=gate(state=StaticSafetyState(kill_switch_enabled=True)),
        recorder=recorder,
        market=market(),
        execution_mode=ExecutionMode.PAPER,
        connection_target="fake",
        created_at_utc=NOW,
    )

    assert SafetyRejectionCode.KILL_SWITCH_ENABLED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


def test_position_limits_use_broker_snapshot_and_reject_at_limit():
    broker = FakeBroker(default_fill_price=Decimal("150"))
    broker.set_positions(
        [
            PositionInfo("USD_JPY", PositionSide.LONG, Decimal("1"), Decimal("149"), NOW),
            PositionInfo("USD_JPY", PositionSide.SHORT, Decimal("1"), Decimal("151"), NOW),
        ]
    )

    decision = gate().evaluate(
        request(),
        broker=broker,
        market=market(),
        execution_mode=ExecutionMode.PAPER,
        connection_target="fake",
    )

    assert SafetyRejectionCode.SYMBOL_POSITION_LIMIT_EXCEEDED in codes(decision)
    assert decision.position_count == 2


class BrokenBroker(FakeBroker):
    def get_positions(self):
        raise RuntimeError("positions unavailable")


class BrokenState:
    def is_kill_switch_enabled(self):
        raise RuntimeError("state DB unavailable")


class BrokenRecorder:
    def record(self, decision):
        raise RuntimeError("decision DB unavailable")


def test_broker_query_failure_is_fail_closed():
    broker = BrokenBroker(default_fill_price=Decimal("150"))
    decision, result = submit_after_safety_gate(
        request(),
        client_order_id="client-1",
        broker=broker,
        gate=gate(),
        recorder=InMemorySafetyDecisionRecorder(),
        market=market(),
        execution_mode=ExecutionMode.PAPER,
        connection_target="fake",
        created_at_utc=NOW,
    )
    assert SafetyRejectionCode.BROKER_QUERY_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


def test_state_query_failure_is_fail_closed():
    broker = FakeBroker(default_fill_price=Decimal("150"))
    decision, result = submit_after_safety_gate(
        request(),
        client_order_id="client-1",
        broker=broker,
        gate=gate(state=BrokenState()),
        recorder=InMemorySafetyDecisionRecorder(),
        market=market(),
        execution_mode=ExecutionMode.PAPER,
        connection_target="fake",
        created_at_utc=NOW,
    )
    assert SafetyRejectionCode.STATE_QUERY_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


def test_decision_persistence_failure_prevents_submit():
    broker = FakeBroker(default_fill_price=Decimal("150"))
    decision, result = submit_after_safety_gate(
        request(),
        client_order_id="client-1",
        broker=broker,
        gate=gate(),
        recorder=BrokenRecorder(),
        market=market(),
        execution_mode=ExecutionMode.PAPER,
        connection_target="fake",
        created_at_utc=NOW,
    )
    assert decision.rejection_codes == (SafetyRejectionCode.DECISION_PERSIST_FAILED.value,)
    assert result is None
    assert broker.submitted_requests == []


def test_multiple_violations_are_machine_readable_and_collected_together():
    broker = FakeBroker(default_fill_price=Decimal("150"))
    decision = gate(state=StaticSafetyState(kill_switch_enabled=True)).evaluate(
        request(quantity=Decimal("11"), stop_loss_price=None),
        broker=broker,
        market=market(market_open=False),
        execution_mode=ExecutionMode.LIVE,
        connection_target="fake",
    )
    assert decision.allowed is False
    assert {
        SafetyRejectionCode.QUANTITY_LIMIT_EXCEEDED,
        SafetyRejectionCode.STOP_LOSS_REQUIRED,
        SafetyRejectionCode.MARKET_CLOSED,
        SafetyRejectionCode.KILL_SWITCH_ENABLED,
        SafetyRejectionCode.EXECUTION_MODE_MISMATCH,
    }.issubset(codes(decision))
