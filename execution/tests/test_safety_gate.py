"""Issue #75: fail-closed pre-submit Safety Gate.

Every safety condition is tested independently; every rejection path asserts
that FakeBroker received ZERO submissions. FakeBroker performs no external
I/O, so none of these tests can reach a network, Discord, S3 or a real DB.
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from broker.broker_models import (
    AccountState,
    OrderSide,
    OrderType,
    PositionInfo,
    PositionSide,
)
from broker.fake_broker import FakeBroker
from execution.execution_models import ExecutionRequest
from execution.safety_gate import (
    ExecutionMode,
    InMemorySafetyDecisionRecorder,
    MarketSafetySnapshot,
    SafetyDecision,
    SafetyGate,
    SafetyPolicy,
    SafetyRejection,
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


def gate(*, state=None, selected_policy=None, clock=None):
    return SafetyGate(
        policy=selected_policy or policy(),
        state_provider=state or StaticSafetyState(),
        clock=clock or (lambda: NOW),
    )


def codes(decision):
    return {item.code for item in decision.rejections}


def submit(req=None, *, broker=None, the_gate=None, recorder=None, snapshot=None,
           mode=ExecutionMode.FAKE, target="fake"):
    broker = broker if broker is not None else FakeBroker(default_fill_price=Decimal("150"))
    recorder = recorder if recorder is not None else InMemorySafetyDecisionRecorder()
    decision, result = submit_after_safety_gate(
        req if req is not None else request(),
        client_order_id="client-1",
        broker=broker,
        gate=the_gate or gate(),
        recorder=recorder,
        market=snapshot if snapshot is not None else market(),
        execution_mode=mode,
        connection_target=target,
        created_at_utc=NOW,
    )
    return decision, result, broker, recorder


# --- normal path -------------------------------------------------------------


def test_normal_conditions_submit_to_fake_broker_without_external_io():
    decision, result, broker, recorder = submit()

    assert decision.allowed is True
    assert decision.rejections == ()
    assert result is not None
    assert len(broker.submitted_requests) == 1
    assert broker.submitted_requests[0].client_order_id == "client-1"
    assert recorder.decisions == [decision]


def test_allowed_decision_carries_audit_fields():
    decision, _, _, _ = submit()
    assert decision.policy_id == "policy-test-1"
    assert decision.execution_mode == "fake"
    assert decision.connection_target == "fake"
    assert decision.account_id == "fake-account"
    assert decision.position_count == 0
    assert decision.checked_at_utc == NOW


def test_sell_with_valid_sl_tp_is_allowed():
    decision, result, broker, _ = submit(
        request(side=OrderSide.SELL, stop_loss_price=Decimal("150.5"),
                take_profit_price=Decimal("149.5")),
    )
    assert decision.allowed is True
    assert result is not None
    assert len(broker.submitted_requests) == 1


def test_limit_order_sl_tp_are_judged_against_requested_price_not_live_price():
    # BUY LIMIT below the market: TP 149.8 is under the live ask (150.01) but
    # ABOVE the limit price 149 -- must be allowed (the base price for LIMIT
    # SL/TP validation is requested_price, not the live snapshot).
    decision, result, broker, _ = submit(
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


# --- each safety condition rejects independently, before any submit ----------


@pytest.mark.parametrize(
    ("req_factory", "snapshot_factory", "policy_factory", "mode", "target", "expected"),
    [
        pytest.param(
            lambda: request(symbol="XAU_USD"), lambda: market(symbol="XAU_USD"),
            policy, ExecutionMode.FAKE, "fake",
            SafetyRejectionCode.SYMBOL_NOT_ALLOWED, id="symbol-not-allowed"),
        pytest.param(
            lambda: request(side=OrderSide.SELL, stop_loss_price=Decimal("150.5"),
                            take_profit_price=Decimal("149.5")),
            market, lambda: policy(allowed_sides=frozenset({OrderSide.BUY})),
            ExecutionMode.FAKE, "fake",
            SafetyRejectionCode.SIDE_NOT_ALLOWED, id="side-not-allowed"),
        pytest.param(
            lambda: request(quantity=Decimal("11")), market, policy,
            ExecutionMode.FAKE, "fake",
            SafetyRejectionCode.QUANTITY_LIMIT_EXCEEDED, id="quantity-limit"),
        pytest.param(
            lambda: request(stop_loss_price=None), market, policy,
            ExecutionMode.FAKE, "fake",
            SafetyRejectionCode.STOP_LOSS_REQUIRED, id="stop-loss-required"),
        pytest.param(
            lambda: request(stop_loss_price=Decimal("150.5")), market, policy,
            ExecutionMode.FAKE, "fake",
            SafetyRejectionCode.STOP_LOSS_INVALID, id="stop-loss-invalid-buy"),
        pytest.param(
            lambda: request(side=OrderSide.SELL, stop_loss_price=Decimal("149.9"),
                            take_profit_price=Decimal("149.5")),
            market, policy, ExecutionMode.FAKE, "fake",
            SafetyRejectionCode.STOP_LOSS_INVALID, id="stop-loss-invalid-sell"),
        pytest.param(
            lambda: request(take_profit_price=Decimal("150.005")), market, policy,
            ExecutionMode.FAKE, "fake",
            SafetyRejectionCode.TAKE_PROFIT_INVALID, id="take-profit-invalid"),
        pytest.param(
            request, lambda: market(symbol="EUR_USD", reference_price=Decimal("1.1"),
                                    bid=Decimal("1.0999"), ask=Decimal("1.1001")),
            policy, ExecutionMode.FAKE, "fake",
            SafetyRejectionCode.MARKET_SNAPSHOT_MISMATCH, id="snapshot-mismatch"),
        pytest.param(
            request, lambda: market(reference_price=None), policy,
            ExecutionMode.FAKE, "fake",
            SafetyRejectionCode.REFERENCE_PRICE_MISSING, id="reference-missing"),
        pytest.param(
            request, lambda: market(observed_at_utc=NOW - timedelta(seconds=31)), policy,
            ExecutionMode.FAKE, "fake",
            SafetyRejectionCode.REFERENCE_PRICE_STALE, id="reference-stale"),
        pytest.param(
            request, lambda: market(observed_at_utc=NOW + timedelta(seconds=1)), policy,
            ExecutionMode.FAKE, "fake",
            SafetyRejectionCode.REFERENCE_TIMESTAMP_FUTURE, id="reference-future"),
        pytest.param(
            request, lambda: market(bid=None), policy, ExecutionMode.FAKE, "fake",
            SafetyRejectionCode.SPREAD_MISSING, id="spread-missing"),
        pytest.param(
            request, lambda: market(ask=Decimal("150.10")), policy,
            ExecutionMode.FAKE, "fake",
            SafetyRejectionCode.SPREAD_LIMIT_EXCEEDED, id="spread-limit"),
        pytest.param(
            request, lambda: market(market_open=False), policy, ExecutionMode.FAKE, "fake",
            SafetyRejectionCode.MARKET_CLOSED, id="market-closed"),
        pytest.param(
            request, lambda: market(tradable=False), policy, ExecutionMode.FAKE, "fake",
            SafetyRejectionCode.SYMBOL_NOT_TRADABLE, id="not-tradable"),
        pytest.param(
            request, market, policy, ExecutionMode.LIVE, "fake",
            SafetyRejectionCode.EXECUTION_MODE_MISMATCH, id="live-mode-fake-target"),
        pytest.param(
            request, market, policy, ExecutionMode.FAKE, "oanda-live",
            SafetyRejectionCode.EXECUTION_MODE_MISMATCH, id="fake-mode-live-target"),
        pytest.param(
            request, market, policy, ExecutionMode.DEMO, "oanda-live",
            SafetyRejectionCode.EXECUTION_MODE_MISMATCH, id="demo-mode-live-target"),
        pytest.param(
            request, market, policy, ExecutionMode.FAKE, "unknown-endpoint",
            SafetyRejectionCode.EXECUTION_MODE_MISMATCH, id="unknown-target"),
        pytest.param(
            request, market, policy, ExecutionMode.FAKE, "FAKE",
            SafetyRejectionCode.EXECUTION_MODE_MISMATCH, id="case-differs"),
        pytest.param(
            request, market, policy, ExecutionMode.FAKE, " fake ",
            SafetyRejectionCode.EXECUTION_MODE_MISMATCH, id="whitespace-target"),
        pytest.param(
            request, market, policy, ExecutionMode.FAKE, "",
            SafetyRejectionCode.EXECUTION_MODE_MISMATCH, id="empty-target"),
        pytest.param(
            request, market, policy, "live", "oanda-live",
            SafetyRejectionCode.EXECUTION_MODE_MISMATCH, id="unknown-mode-string"),
    ],
)
def test_each_condition_rejects_before_submit(
    req_factory, snapshot_factory, policy_factory, mode, target, expected
):
    decision, result, broker, recorder = submit(
        req_factory(),
        the_gate=gate(selected_policy=policy_factory()),
        snapshot=snapshot_factory(),
        mode=mode,
        target=target,
    )
    assert expected in codes(decision)
    assert decision.allowed is False
    assert result is None
    assert broker.submitted_requests == []
    assert recorder.decisions == [decision]


# --- kill switch -------------------------------------------------------------


def test_kill_switch_rejects_every_new_order_even_when_all_else_passes():
    decision, result, broker, _ = submit(
        the_gate=gate(state=StaticSafetyState(kill_switch_enabled=True)),
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


def test_kill_switch_state_query_failure_is_fail_closed():
    decision, result, broker, _ = submit(the_gate=gate(state=BrokenState()))
    assert SafetyRejectionCode.STATE_QUERY_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


def test_kill_switch_non_boolean_answer_is_fail_closed():
    decision, result, broker, _ = submit(the_gate=gate(state=NonBooleanState()))
    assert SafetyRejectionCode.STATE_QUERY_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


# --- position limits ---------------------------------------------------------


def position(symbol="USD_JPY", side=PositionSide.LONG, retrieved_at=NOW):
    return PositionInfo(symbol, side, Decimal("1"), Decimal("149"), retrieved_at)


def test_symbol_position_limit_counts_long_and_short_together():
    broker = FakeBroker(default_fill_price=Decimal("150"))
    broker.set_positions([position(side=PositionSide.LONG), position(side=PositionSide.SHORT)])

    decision, result, broker, _ = submit(broker=broker)

    assert SafetyRejectionCode.SYMBOL_POSITION_LIMIT_EXCEEDED in codes(decision)
    assert decision.position_count == 2
    assert result is None
    assert broker.submitted_requests == []


def test_account_position_limit_counts_every_symbol():
    broker = FakeBroker(default_fill_price=Decimal("150"))
    broker.set_positions(
        [position(symbol=f"SYM_{i}") for i in range(5)]  # account limit is 5
    )

    decision, result, broker, _ = submit(broker=broker)

    assert SafetyRejectionCode.ACCOUNT_POSITION_LIMIT_EXCEEDED in codes(decision)
    assert SafetyRejectionCode.SYMBOL_POSITION_LIMIT_EXCEEDED not in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


def test_position_count_below_limits_is_allowed():
    broker = FakeBroker(default_fill_price=Decimal("150"))
    broker.set_positions([position()])
    decision, result, _, _ = submit(broker=broker)
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
def test_unreliable_position_snapshots_are_fail_closed(retrieved_at):
    broker = FakeBroker(default_fill_price=Decimal("150"))
    broker.set_positions([position(retrieved_at=retrieved_at)])
    decision, result, broker, _ = submit(broker=broker)
    assert SafetyRejectionCode.BROKER_QUERY_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


# --- broker query failures ---------------------------------------------------


class BrokenPositionsBroker(FakeBroker):
    def get_positions(self):
        raise RuntimeError("positions unavailable")


class BrokenAccountBroker(FakeBroker):
    def get_account_state(self):
        raise RuntimeError("account unavailable")


class GarbagePositionsBroker(FakeBroker):
    def get_positions(self):
        return ({"symbol": "USD_JPY"},)


@pytest.mark.parametrize(
    "broker_cls",
    [
        pytest.param(BrokenPositionsBroker, id="positions-query-fails"),
        pytest.param(BrokenAccountBroker, id="account-query-fails"),
        pytest.param(GarbagePositionsBroker, id="positions-garbage"),
    ],
)
def test_broker_query_failure_is_fail_closed(broker_cls):
    broker = broker_cls(default_fill_price=Decimal("150"))
    decision, result, broker, _ = submit(broker=broker)
    assert SafetyRejectionCode.BROKER_QUERY_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


# --- decision persistence failure --------------------------------------------


class BrokenRecorder:
    def record(self, decision):
        raise RuntimeError("decision DB unavailable")


def test_decision_persistence_failure_prevents_submit_even_when_allowed():
    decision, result, broker, _ = submit(recorder=BrokenRecorder())
    assert decision.allowed is False
    assert decision.rejection_codes == (SafetyRejectionCode.DECISION_PERSIST_FAILED.value,)
    assert result is None
    assert broker.submitted_requests == []


def test_decision_persistence_failure_keeps_the_original_rejection_codes():
    decision, result, broker, _ = submit(
        request(quantity=Decimal("11")), recorder=BrokenRecorder()
    )
    assert SafetyRejectionCode.QUANTITY_LIMIT_EXCEEDED in codes(decision)
    assert SafetyRejectionCode.DECISION_PERSIST_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


# --- unexpected internal failure --------------------------------------------


def test_unexpected_evaluation_failure_is_fail_closed_and_recorded():
    decision, result, broker, recorder = submit(snapshot="not-a-snapshot")
    assert SafetyRejectionCode.GATE_INTERNAL_ERROR in codes(decision)
    assert result is None
    assert broker.submitted_requests == []
    assert recorder.decisions == [decision]


def test_broken_gate_clock_is_fail_closed():
    naive_clock = lambda: datetime(2026, 7, 22, 1, 0)  # noqa: E731 -- naive on purpose
    decision, result, broker, _ = submit(the_gate=gate(clock=naive_clock))
    assert SafetyRejectionCode.GATE_INTERNAL_ERROR in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


# --- multiple violations -----------------------------------------------------


def test_multiple_violations_are_machine_readable_and_collected_together():
    broker = FakeBroker(default_fill_price=Decimal("150"))
    decision = gate(state=StaticSafetyState(kill_switch_enabled=True)).evaluate(
        request(quantity=Decimal("11"), stop_loss_price=None, take_profit_price=None),
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
    assert broker.submitted_requests == []


# --- logging -----------------------------------------------------------------


def test_rejection_codes_appear_in_the_log(caplog):
    with caplog.at_level(logging.WARNING, logger="execution.safety_gate"):
        submit(request(quantity=Decimal("11")))
    assert any(
        "quantity_limit_exceeded" in record.getMessage() and "exec-1" in record.getMessage()
        for record in caplog.records
    )


def test_allow_is_logged_without_secrets(caplog):
    with caplog.at_level(logging.INFO, logger="execution.safety_gate"):
        submit()
    allowed_logs = [r.getMessage() for r in caplog.records if "allowed" in r.getMessage()]
    assert allowed_logs, "the allow path must be observable in logs"


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
