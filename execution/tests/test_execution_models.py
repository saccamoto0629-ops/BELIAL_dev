"""Issue #74: ExecutionRequest construction invariants and OrderRequest conversion."""

import dataclasses
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from broker.broker_models import OrderRequest, OrderSide, OrderType
from execution.execution_models import ExecutionRequest

UTC_NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def make_request(**overrides):
    kwargs = dict(
        execution_request_id="exec-1",
        signal_id="sig-1",
        strategy_id="strat-1",
        symbol="XAUUSD",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("10"),
        requested_at_utc=UTC_NOW,
        requested_price=Decimal("2400.00"),
        stop_loss_price=Decimal("2390.00"),
        take_profit_price=Decimal("2420.00"),
    )
    kwargs.update(overrides)
    return ExecutionRequest(**kwargs)


# --- normal construction ----------------------------------------------------


def test_valid_limit_request_constructs_with_all_fields():
    request = make_request()
    assert request.execution_request_id == "exec-1"
    assert request.signal_id == "sig-1"
    assert request.strategy_id == "strat-1"
    assert request.quantity == Decimal("10")
    assert request.requested_price == Decimal("2400.00")
    assert request.stop_loss_price == Decimal("2390.00")
    assert request.take_profit_price == Decimal("2420.00")
    assert request.requested_at_utc == UTC_NOW


def test_valid_market_request_constructs_without_prices():
    request = make_request(
        order_type=OrderType.MARKET,
        requested_price=None,
        stop_loss_price=None,
        take_profit_price=None,
    )
    assert request.order_type is OrderType.MARKET
    assert request.requested_price is None


def test_request_is_immutable():
    request = make_request()
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.quantity = Decimal("1")


# --- required IDs / symbol --------------------------------------------------


@pytest.mark.parametrize(
    "field", ["execution_request_id", "signal_id", "strategy_id", "symbol"]
)
@pytest.mark.parametrize("bad", ["", "   ", None, 7])
def test_empty_or_non_string_identity_fields_are_rejected(field, bad):
    with pytest.raises(ValueError):
        make_request(**{field: bad})


def test_non_enum_side_and_order_type_are_rejected():
    with pytest.raises(ValueError):
        make_request(side="buy")
    with pytest.raises(ValueError):
        make_request(order_type="limit")


# --- quantity / price validation --------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), 10, 10.0, "10"],
)
def test_invalid_quantity_is_rejected(bad):
    with pytest.raises(ValueError):
        make_request(quantity=bad)


@pytest.mark.parametrize("field", ["requested_price", "stop_loss_price", "take_profit_price"])
@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-5"), Decimal("NaN"), 2400.0])
def test_invalid_prices_are_rejected(field, bad):
    overrides = {field: bad}
    # keep the cross-field checks out of the way: only the field under test is set
    for other in ("stop_loss_price", "take_profit_price"):
        if other != field:
            overrides[other] = None
    with pytest.raises(ValueError):
        make_request(**overrides)


# --- datetime validation ----------------------------------------------------


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError):
        make_request(requested_at_utc=datetime(2026, 7, 20, 12, 0, 0))


def test_non_utc_timezone_is_rejected():
    from datetime import timedelta

    jst = timezone(timedelta(hours=9))
    with pytest.raises(ValueError):
        make_request(requested_at_utc=datetime(2026, 7, 20, 21, 0, 0, tzinfo=jst))


# --- MARKET / LIMIT price consistency ---------------------------------------


def test_limit_without_requested_price_is_rejected():
    with pytest.raises(ValueError):
        make_request(requested_price=None, stop_loss_price=None, take_profit_price=None)


def test_market_with_requested_price_is_rejected():
    with pytest.raises(ValueError):
        make_request(order_type=OrderType.MARKET, requested_price=Decimal("2400"))


# --- SL/TP vs direction (only unambiguous relations) ------------------------


def test_buy_requires_stop_loss_below_take_profit():
    with pytest.raises(ValueError):
        make_request(
            order_type=OrderType.MARKET,
            requested_price=None,
            stop_loss_price=Decimal("2420"),
            take_profit_price=Decimal("2390"),
        )


def test_sell_requires_take_profit_below_stop_loss():
    with pytest.raises(ValueError):
        make_request(
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            requested_price=None,
            stop_loss_price=Decimal("2390"),
            take_profit_price=Decimal("2420"),
        )
    # and the mirrored valid layout constructs
    make_request(
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        requested_price=None,
        stop_loss_price=Decimal("2420"),
        take_profit_price=Decimal("2390"),
    )


@pytest.mark.parametrize(
    "side,sl,tp",
    [
        (OrderSide.BUY, Decimal("2405"), None),  # BUY stop above the limit price
        (OrderSide.BUY, None, Decimal("2395")),  # BUY target below the limit price
        (OrderSide.SELL, Decimal("2395"), None),  # SELL stop below the limit price
        (OrderSide.SELL, None, Decimal("2405")),  # SELL target above the limit price
    ],
)
def test_limit_price_side_relations_are_enforced(side, sl, tp):
    with pytest.raises(ValueError):
        make_request(side=side, stop_loss_price=sl, take_profit_price=tp)


def test_market_with_a_single_sl_or_tp_is_not_over_validated():
    # No reference price exists for a MARKET order, so a lone SL or TP has no
    # unambiguous relation to enforce -- construction must succeed.
    make_request(
        order_type=OrderType.MARKET,
        requested_price=None,
        stop_loss_price=Decimal("2500"),
        take_profit_price=None,
    )
    make_request(
        order_type=OrderType.MARKET,
        requested_price=None,
        stop_loss_price=None,
        take_profit_price=Decimal("2300"),
    )


# --- conversion to broker.OrderRequest --------------------------------------


def test_to_order_request_maps_limit_fields_to_the_broker_boundary():
    request = make_request()
    order = request.to_order_request(client_order_id="c-1", created_at_utc=UTC_NOW)
    assert isinstance(order, OrderRequest)
    assert order.client_order_id == "c-1"
    assert order.symbol == "XAUUSD"
    assert order.side is OrderSide.BUY
    assert order.order_type is OrderType.LIMIT
    assert order.quantity == Decimal("10")
    assert order.limit_price == Decimal("2400.00")
    assert order.created_at_utc == UTC_NOW


def test_to_order_request_market_has_no_limit_price_and_no_sl_tp_leak():
    request = make_request(
        order_type=OrderType.MARKET,
        requested_price=None,
        stop_loss_price=Decimal("2390"),
        take_profit_price=Decimal("2420"),
    )
    order = request.to_order_request(client_order_id="c-2", created_at_utc=UTC_NOW)
    assert order.limit_price is None
    # SL/TP stay BELIAL-internal: the broker order model has no such fields.
    assert not hasattr(order, "stop_loss_price")
    assert not hasattr(order, "take_profit_price")


def test_to_order_request_validates_at_the_broker_boundary():
    request = make_request()
    with pytest.raises(Exception):
        request.to_order_request(client_order_id="", created_at_utc=UTC_NOW)
