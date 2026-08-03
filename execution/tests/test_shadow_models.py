"""Issue #78: ShadowIntendedOrder / ShadowResult model invariants -- the audit
projection matches the request, non-shadow decisions are unrepresentable, and
policy snapshots detach from the caller's dict."""

from decimal import Decimal

import pytest

from broker.broker_models import OrderSide, OrderType
from execution.safety_gate import SafetyDecision
from execution.shadow_models import (
    ShadowIntendedOrder,
    ShadowResult,
    intended_order_from_request,
)
from shadow_helpers import NOW, make_account, make_gate, make_market, make_request


def make_decision(request, **overrides) -> SafetyDecision:
    gate = make_gate()
    return gate.evaluate_shadow(
        request, market=make_market(), account=make_account(), connection_target="shadow"
    )


def make_result(request=None, decision=None, **overrides) -> ShadowResult:
    request = request if request is not None else make_request()
    decision = decision if decision is not None else make_decision(request)
    values = dict(
        request=request,
        client_order_id="client-1",
        timeframe="H1",
        decision=decision,
        intended_order=intended_order_from_request(request, client_order_id="client-1"),
        market=make_market(),
        policy_snapshot=make_gate().policy.to_snapshot(),
        safety_decision_id=1,
        created_at_utc=NOW,
    )
    values.update(overrides)
    return ShadowResult(**values)


# --- intended order projection ------------------------------------------------


def test_market_projection_has_no_limit_price():
    order = intended_order_from_request(make_request(), client_order_id="client-1")
    assert order.limit_price is None
    assert order.symbol == "USD_JPY" and order.quantity == Decimal("1")
    assert order.side is OrderSide.BUY and order.order_type is OrderType.MARKET


def test_limit_projection_maps_requested_price_to_limit_price():
    request = make_request(
        order_type=OrderType.LIMIT,
        requested_price=Decimal("150.10"),
        stop_loss_price=Decimal("149.50"),
        take_profit_price=Decimal("151.50"),
    )
    order = intended_order_from_request(request, client_order_id="client-1")
    assert order.limit_price == Decimal("150.10")


def test_intended_order_rejects_limit_price_mismatch():
    with pytest.raises(ValueError, match="limit_price"):
        ShadowIntendedOrder(
            client_order_id="client-1",
            symbol="USD_JPY",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
            limit_price=Decimal("150"),
        )
    with pytest.raises(ValueError, match="limit_price"):
        ShadowIntendedOrder(
            client_order_id="client-1",
            symbol="USD_JPY",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("1"),
            limit_price=None,
        )


def test_intended_order_rejects_invalid_fields():
    with pytest.raises(ValueError):
        intended_order_from_request(make_request(), client_order_id=" ")
    with pytest.raises(ValueError):
        intended_order_from_request("not a request", client_order_id="client-1")


# --- ShadowResult invariants --------------------------------------------------


def test_valid_result_roundtrips_properties():
    result = make_result()
    assert result.allowed is True
    assert result.rejection_codes == ()
    assert result.shadow_result_id is None


def test_result_rejects_decision_for_another_request():
    other = make_request(execution_request_id="exec-2", signal_id="signal-2")
    with pytest.raises(ValueError, match="belongs to"):
        make_result(decision=make_decision(other))


def test_result_rejects_non_shadow_decision():
    request = make_request()
    live_like = SafetyDecision(
        execution_request_id=request.execution_request_id,
        allowed=True,
        checked_at_utc=NOW,
        policy_id="shadow-test-policy",
        execution_mode="fake",
        connection_target="fake",
    )
    with pytest.raises(ValueError, match="shadow"):
        make_result(request=request, decision=live_like)


def test_result_rejects_intended_order_drift():
    request = make_request()
    drifted = ShadowIntendedOrder(
        client_order_id="client-1",
        symbol="USD_JPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("2"),  # request says 1
        limit_price=None,
    )
    with pytest.raises(ValueError, match="does not match the audit"):
        make_result(request=request, intended_order=drifted)


def test_result_rejects_policy_snapshot_mismatch_and_bad_types():
    with pytest.raises(ValueError, match="policy_id"):
        make_result(policy_snapshot={"policy_id": "someone-else"})
    with pytest.raises(ValueError, match="dict"):
        make_result(policy_snapshot="not a dict")
    with pytest.raises(ValueError, match="JSON-serializable"):
        make_result(policy_snapshot={"policy_id": "shadow-test-policy", "bad": object()})


def test_policy_snapshot_is_detached_from_the_caller_dict():
    snapshot = make_gate().policy.to_snapshot()
    result = make_result(policy_snapshot=snapshot)
    snapshot["policy_id"] = "mutated"
    snapshot["allowed_symbols"].append("EUR_USD")
    assert result.policy_snapshot["policy_id"] == "shadow-test-policy"
    assert "EUR_USD" not in result.policy_snapshot["allowed_symbols"]


def test_result_rejects_invalid_ids_and_timeframe():
    with pytest.raises(ValueError, match="safety_decision_id"):
        make_result(safety_decision_id=0)
    with pytest.raises(ValueError, match="shadow_result_id"):
        make_result(shadow_result_id=-1)
    with pytest.raises(ValueError, match="timeframe"):
        make_result(timeframe=" ")
    with pytest.raises(ValueError, match="created_at_utc"):
        make_result(created_at_utc=NOW.replace(tzinfo=None))
