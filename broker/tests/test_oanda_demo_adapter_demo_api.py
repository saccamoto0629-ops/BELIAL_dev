"""Issue #79: OPT-IN tests against the REAL OANDA demo (fxpractice) API.

*** SEPARATED FROM NORMAL CI. ***

This module is skipped entirely unless BOTH hold:

1. ``BELIAL_RUN_OANDA_DEMO_API_TESTS=1`` is set (explicit opt-in), and
2. ``OANDA_API_TOKEN`` / ``OANDA_ACCOUNT_ID`` are set (demo credentials).

Normal CI sets neither, so the seven-suite run never touches the network.
Run explicitly with real demo credentials in the environment:

    BELIAL_RUN_OANDA_DEMO_API_TESTS=1 python -m pytest \
        broker/tests/test_oanda_demo_adapter_demo_api.py -q

Notes:

- These tests PLACE REAL ORDERS on the DEMO account (minimal size, and any
  opened exposure is flattened again). Never point them at anything but a
  practice account — the adapter itself refuses non-demo endpoints.
- Assertions never print or embed the token / account id.
- The LIMIT price used for the cancel lifecycle is far below any realistic
  USD_JPY rate so the order stays pending until it is cancelled.
"""

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from broker import broker_models as m
from broker import oanda_demo_adapter as oa

_OPTED_IN = os.environ.get("BELIAL_RUN_OANDA_DEMO_API_TESTS") == "1"
_HAS_CREDENTIALS = bool(
    os.environ.get(oa.ENV_API_TOKEN, "").strip()
    and os.environ.get(oa.ENV_ACCOUNT_ID, "").strip()
)

pytestmark = pytest.mark.skipif(
    not (_OPTED_IN and _HAS_CREDENTIALS),
    reason=(
        "OANDA demo API tests are opt-in: set BELIAL_RUN_OANDA_DEMO_API_TESTS=1 "
        f"plus {oa.ENV_API_TOKEN} / {oa.ENV_ACCOUNT_ID} (demo credentials) to run"
    ),
)

INSTRUMENT = os.environ.get("OANDA_DEMO_TEST_INSTRUMENT", "USD_JPY")
# Far below any realistic USD_JPY rate: the LIMIT BUY stays pending.
SAFE_LIMIT_PRICE = Decimal(os.environ.get("OANDA_DEMO_TEST_LIMIT_PRICE", "50.000"))


@pytest.fixture(scope="module")
def adapter():
    return oa.OandaDemoAdapter(oa.OandaDemoConfig.from_env())


def _new_client_order_id() -> str:
    return f"belial-79-{uuid.uuid4().hex}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_request(client_order_id, *, side=m.OrderSide.BUY,
                 order_type=m.OrderType.MARKET, limit_price=None):
    return m.OrderRequest(
        client_order_id=client_order_id,
        symbol=INSTRUMENT,
        side=side,
        order_type=order_type,
        quantity=Decimal("1"),
        created_at_utc=_now(),
        limit_price=limit_price,
    )


def test_demo_account_state_can_be_fetched(adapter):
    state = adapter.get_account_state()
    assert isinstance(state, m.AccountState)
    assert state.currency
    assert state.retrieved_at_utc.tzinfo is not None
    # the model keeps the REAL id (identifier semantics) while repr and the
    # display digest conceal it (PR #101 re-review)
    assert state.account_id == os.environ[oa.ENV_ACCOUNT_ID].strip()
    assert state.account_id not in repr(state)
    assert state.account_id not in state.display_account_id


def test_demo_tradable_instruments_include_the_test_instrument(adapter):
    instruments = adapter.get_tradable_instruments()
    assert INSTRUMENT in instruments


def _net_units(adapter):
    """Signed net exposure in INSTRUMENT (long positive), via the broker."""
    net = Decimal("0")
    for position in adapter.get_positions():
        if position.symbol == INSTRUMENT:
            if position.side is m.PositionSide.LONG:
                net += position.quantity
            else:
                net -= position.quantity
    return net


def test_demo_market_order_lifecycle_submit_query_positions_flatten(adapter):
    client_order_id = _new_client_order_id()
    # Cleanup is RECONCILIATION-based (PR #101 re-review): the net exposure
    # is measured before the test and re-measured in the finally block, so
    # even a submit that raises AFTER broker acceptance (e.g. a response
    # correlation failure) still gets flattened. NOTE: the opposite MARKET
    # order nets the exposure on a NETTING demo account; hedging demo
    # accounts are out of scope for this suite (documented limitation).
    net_before = _net_units(adapter)
    try:
        result = adapter.submit_order(make_request(client_order_id))
        # A 1-unit FOK market order on a major pair fills immediately on demo.
        assert result.state is m.OrderState.FILLED
        assert result.client_order_id == client_order_id
        assert result.broker_order_id
        assert result.filled_quantity == Decimal("1")
        assert result.fills and result.average_fill_price is not None

        # Query the order back through the client-side identifier.
        queried = adapter.get_order(client_order_id)
        assert queried.state is m.OrderState.FILLED
        assert queried.broker_order_id == result.broker_order_id

        # The fill must be visible as an open position and as an open trade.
        positions = adapter.get_positions()
        assert any(p.symbol == INSTRUMENT for p in positions)
        trades = adapter.get_open_trades()
        assert any(t.symbol == INSTRUMENT for t in trades)

        # A filled order can no longer be cancelled (common taxonomy, not OANDA).
        with pytest.raises(m.BrokerCancellationError):
            adapter.cancel_order(client_order_id)
    finally:
        delta = _net_units(adapter) - net_before
        if delta != 0:
            flatten = adapter.submit_order(
                m.OrderRequest(
                    client_order_id=_new_client_order_id(),
                    symbol=INSTRUMENT,
                    side=m.OrderSide.SELL if delta > 0 else m.OrderSide.BUY,
                    order_type=m.OrderType.MARKET,
                    quantity=abs(delta),
                    created_at_utc=_now(),
                )
            )
            assert flatten.state is m.OrderState.FILLED


def test_demo_limit_order_can_be_queried_and_cancelled(adapter):
    client_order_id = _new_client_order_id()
    submitted = adapter.submit_order(
        make_request(
            client_order_id,
            order_type=m.OrderType.LIMIT,
            limit_price=SAFE_LIMIT_PRICE,
        )
    )
    try:
        assert submitted.state is m.OrderState.ACCEPTED
        assert submitted.broker_order_id

        queried = adapter.get_order(client_order_id)
        assert queried.state is m.OrderState.ACCEPTED
        assert queried.requested_price == SAFE_LIMIT_PRICE
    finally:
        # cancel runs even when an assertion above fails, so no pending
        # order is left behind on the demo account (PR #101 review)
        cancelled = adapter.cancel_order(client_order_id)
        assert cancelled.state is m.OrderState.CANCELLED
    assert adapter.get_order(client_order_id).state is m.OrderState.CANCELLED


def test_demo_unknown_order_id_raises_not_found(adapter):
    with pytest.raises(m.BrokerOrderNotFoundError):
        adapter.get_order(f"belial-79-missing-{uuid.uuid4().hex}")
