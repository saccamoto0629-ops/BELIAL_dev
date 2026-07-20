"""Issue #73: broker-agnostic common models — enums, invariants, transitions, errors."""

import pathlib
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

import broker_models as m  # noqa: E402

UTC_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_request(**overrides):
    kwargs = dict(
        client_order_id="c-1",
        symbol="XAUUSD",
        side=m.OrderSide.BUY,
        order_type=m.OrderType.LIMIT,
        quantity=Decimal("10"),
        limit_price=Decimal("2400.00"),
        created_at_utc=UTC_NOW,
    )
    kwargs.update(overrides)
    return m.OrderRequest(**kwargs)


def make_result(**overrides):
    kwargs = dict(
        client_order_id="c-1",
        broker_order_id="B-1",
        state=m.OrderState.FILLED,
        symbol="XAUUSD",
        side=m.OrderSide.BUY,
        order_type=m.OrderType.LIMIT,
        requested_quantity=Decimal("10"),
        filled_quantity=Decimal("10"),
        remaining_quantity=Decimal("0"),
        requested_price=Decimal("2400.00"),
        average_fill_price=Decimal("2400.50"),
        updated_at_utc=UTC_NOW,
        fills=(m.Fill("f-1", Decimal("2400.50"), Decimal("10"), UTC_NOW),),
    )
    kwargs.update(overrides)
    return m.OrderResult(**kwargs)


# --- order states -----------------------------------------------------------

def test_order_state_vocabulary_covers_issue_73_minimum():
    values = {s.value for s in m.OrderState}
    assert {"pending", "accepted", "partially_filled", "filled",
            "cancelled", "rejected", "unknown"} == values


def test_timeout_outcome_is_a_distinct_state_from_rejected():
    assert m.OrderState.UNKNOWN is not m.OrderState.REJECTED
    # UNKNOWN is NOT terminal: the broker may still hold the order.
    assert m.OrderState.UNKNOWN not in m.TERMINAL_ORDER_STATES
    assert m.OrderState.REJECTED in m.TERMINAL_ORDER_STATES


def test_terminal_states_have_no_outgoing_transitions():
    for state in m.TERMINAL_ORDER_STATES:
        assert m.ORDER_STATE_TRANSITIONS[state] == frozenset()


def test_unknown_can_be_resolved_to_any_concrete_state():
    assert m.ORDER_STATE_TRANSITIONS[m.OrderState.UNKNOWN] == {
        m.OrderState.ACCEPTED, m.OrderState.PARTIALLY_FILLED, m.OrderState.FILLED,
        m.OrderState.CANCELLED, m.OrderState.REJECTED,
    }


@pytest.mark.parametrize(
    "old,new",
    [
        (m.OrderState.PENDING, m.OrderState.ACCEPTED),
        (m.OrderState.ACCEPTED, m.OrderState.PARTIALLY_FILLED),
        (m.OrderState.PARTIALLY_FILLED, m.OrderState.PARTIALLY_FILLED),
        (m.OrderState.PARTIALLY_FILLED, m.OrderState.FILLED),
        (m.OrderState.ACCEPTED, m.OrderState.CANCELLED),
        (m.OrderState.PENDING, m.OrderState.UNKNOWN),
        (m.OrderState.UNKNOWN, m.OrderState.FILLED),
    ],
)
def test_valid_transitions_pass(old, new):
    m.validate_order_state_transition(old, new)


@pytest.mark.parametrize(
    "old,new",
    [
        (m.OrderState.FILLED, m.OrderState.ACCEPTED),
        (m.OrderState.FILLED, m.OrderState.CANCELLED),
        (m.OrderState.CANCELLED, m.OrderState.FILLED),
        (m.OrderState.REJECTED, m.OrderState.ACCEPTED),
        (m.OrderState.PARTIALLY_FILLED, m.OrderState.ACCEPTED),
        (m.OrderState.PARTIALLY_FILLED, m.OrderState.REJECTED),
        (m.OrderState.ACCEPTED, m.OrderState.PENDING),
    ],
)
def test_invalid_transitions_raise(old, new):
    with pytest.raises(ValueError):
        m.validate_order_state_transition(old, new)


# --- error taxonomy ---------------------------------------------------------

def test_error_codes_are_distinct_and_machine_checkable():
    codes = {
        m.BrokerTimeoutError("x").code,
        m.BrokerCommunicationError("x").code,
        m.BrokerCancellationError("x").code,
        m.BrokerInvalidRequestError("x").code,
        m.BrokerOrderNotFoundError("x").code,
    }
    assert len(codes) == 5
    assert all(isinstance(c, m.BrokerErrorCode) for c in codes)


def test_all_broker_errors_share_the_base_class_and_carry_client_order_id():
    err = m.BrokerTimeoutError("boom", client_order_id="c-9")
    assert isinstance(err, m.BrokerError)
    assert err.client_order_id == "c-9"
    assert m.BrokerCommunicationError("x").client_order_id is None


def test_timeout_and_communication_failure_are_different_codes():
    assert m.BrokerErrorCode.TIMEOUT is not m.BrokerErrorCode.COMMUNICATION_FAILURE


# --- OrderRequest validation ------------------------------------------------

def test_order_request_accepts_a_valid_limit_order():
    req = make_request()
    assert req.quantity == Decimal("10")
    assert req.limit_price == Decimal("2400.00")


def test_order_request_accepts_a_market_order_without_price():
    req = make_request(order_type=m.OrderType.MARKET, limit_price=None)
    assert req.limit_price is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"client_order_id": ""},
        {"symbol": "  "},
        {"quantity": Decimal("0")},
        {"quantity": Decimal("-1")},
        {"quantity": 10.0},  # float is rejected: Decimal only
        {"quantity": Decimal("NaN")},
        {"limit_price": None},  # LIMIT without a price
        {"limit_price": Decimal("0")},
        {"order_type": m.OrderType.MARKET},  # MARKET with a limit_price
        {"side": "buy"},  # raw string instead of enum
        {"created_at_utc": datetime(2026, 7, 1, 12, 0, 0)},  # naive datetime
        {"created_at_utc": UTC_NOW.astimezone(timezone(timedelta(hours=9)))},  # non-UTC
    ],
)
def test_order_request_rejects_invalid_input(overrides):
    with pytest.raises(m.BrokerInvalidRequestError):
        make_request(**overrides)


# --- OrderResult invariants -------------------------------------------------

def test_order_result_keeps_requested_and_fill_prices_separately():
    result = make_result()
    assert result.requested_price == Decimal("2400.00")
    assert result.average_fill_price == Decimal("2400.50")
    assert result.price_deviation == Decimal("0.50")


def test_price_deviation_is_none_without_a_requested_price():
    result = make_result(
        order_type=m.OrderType.MARKET, requested_price=None,
    )
    assert result.price_deviation is None


def test_partial_fill_tracks_requested_filled_and_remaining():
    fills = (
        m.Fill("f-1", Decimal("2400"), Decimal("3"), UTC_NOW),
        m.Fill("f-2", Decimal("2401"), Decimal("1"), UTC_NOW),
    )
    result = make_result(
        state=m.OrderState.PARTIALLY_FILLED,
        filled_quantity=Decimal("4"),
        remaining_quantity=Decimal("6"),
        average_fill_price=m.weighted_average_price(fills),
        fills=fills,
    )
    assert result.requested_quantity == Decimal("10")
    assert result.filled_quantity == Decimal("4")
    assert result.remaining_quantity == Decimal("6")
    assert result.average_fill_price == Decimal("2400.25")


def test_weighted_average_price_of_no_fills_is_none():
    assert m.weighted_average_price(()) is None


@pytest.mark.parametrize(
    "overrides",
    [
        # remaining must equal requested - filled
        {"remaining_quantity": Decimal("1")},
        # fills must sum to filled_quantity
        {"fills": (m.Fill("f-1", Decimal("2400.5"), Decimal("9"), UTC_NOW),)},
        # FILLED requires filled == requested
        {"filled_quantity": Decimal("9"), "remaining_quantity": Decimal("1"),
         "fills": (m.Fill("f-1", Decimal("2400.5"), Decimal("9"), UTC_NOW),)},
        # ACCEPTED requires filled == 0
        {"state": m.OrderState.ACCEPTED},
        # REJECTED requires a reject_reason and zero fills
        {"state": m.OrderState.REJECTED},
        # reject_reason forbidden outside REJECTED
        {"reject_reason": "nope"},
        # average price without fills
        {"filled_quantity": Decimal("0"), "remaining_quantity": Decimal("10"),
         "fills": (), "state": m.OrderState.ACCEPTED},
        # filled beyond requested
        {"filled_quantity": Decimal("11"), "remaining_quantity": Decimal("-1"),
         "fills": (m.Fill("f-1", Decimal("2400.5"), Decimal("11"), UTC_NOW),)},
        # ACCEPTED requires a broker_order_id
        {"state": m.OrderState.ACCEPTED, "broker_order_id": None,
         "filled_quantity": Decimal("0"), "remaining_quantity": Decimal("10"),
         "fills": (), "average_fill_price": None},
    ],
)
def test_order_result_rejects_inconsistent_snapshots(overrides):
    with pytest.raises(ValueError):
        make_result(**overrides)


def test_unknown_state_result_is_representable_with_zero_fills():
    # After a timeout the outcome is unknown: no fills, no broker_order_id,
    # but the order is NOT rejected and NOT confirmed absent.
    result = make_result(
        state=m.OrderState.UNKNOWN,
        broker_order_id=None,
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal("10"),
        average_fill_price=None,
        fills=(),
    )
    assert result.state is m.OrderState.UNKNOWN
    assert result.state is not m.OrderState.REJECTED


# --- position / account -----------------------------------------------------

def test_position_info_validates_and_holds_decimals():
    pos = m.PositionInfo(
        symbol="XAUUSD", side=m.PositionSide.LONG, quantity=Decimal("2"),
        average_entry_price=Decimal("2400"), retrieved_at_utc=UTC_NOW,
    )
    assert pos.quantity == Decimal("2")
    with pytest.raises(m.BrokerInvalidRequestError):
        m.PositionInfo(
            symbol="XAUUSD", side=m.PositionSide.LONG, quantity=Decimal("0"),
            average_entry_price=Decimal("2400"), retrieved_at_utc=UTC_NOW,
        )


def test_account_state_allows_negative_balance_but_requires_utc():
    state = m.AccountState(
        account_id="a-1", currency="USD", balance=Decimal("-10"),
        equity=Decimal("0"), margin_available=Decimal("0"), retrieved_at_utc=UTC_NOW,
    )
    assert state.balance == Decimal("-10")
    with pytest.raises(m.BrokerInvalidRequestError):
        m.AccountState(
            account_id="a-1", currency="USD", balance=Decimal("0"),
            equity=Decimal("0"), margin_available=Decimal("0"),
            retrieved_at_utc=datetime(2026, 7, 1),
        )


def test_models_are_immutable():
    req = make_request()
    with pytest.raises(Exception):
        req.quantity = Decimal("99")
