"""Issue #79: OANDA Demo Adapter — translation-only, demo-only, no network.

Everything here runs against an injected fake transport: no socket is ever
opened, no credential is real, and no test output may contain the fake
secrets (asserted explicitly). The real demo API is exercised ONLY by the
opt-in suite in ``test_oanda_demo_adapter_demo_api.py``.
"""

import json
import socket
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from broker import broker_models as m
from broker import oanda_demo_adapter as oa
from broker.broker_interface import Broker

TOKEN = "unit-test-token-not-a-real-secret"
ACCOUNT = "101-000-0000000-001"
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def make_config(**overrides):
    values = dict(api_token=TOKEN, account_id=ACCOUNT)
    values.update(overrides)
    return oa.OandaDemoConfig(**values)


class FakeTransport:
    """Scripted (status, body) responses; records every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(
            {"method": method, "url": url, "headers": dict(headers), "body": body,
             "timeout": timeout}
        )
        if not self.responses:
            raise AssertionError("unexpected extra HTTP call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        status, payload = response
        return status, json.dumps(payload) if isinstance(payload, dict) else payload


def make_adapter(*responses, config=None):
    transport = FakeTransport(*responses)
    adapter = oa.OandaDemoAdapter(
        config or make_config(), transport=transport, clock=lambda: NOW
    )
    return adapter, transport


def make_request(
    client_order_id="c-1",
    side=m.OrderSide.BUY,
    order_type=m.OrderType.MARKET,
    quantity=Decimal("1"),
    limit_price=None,
):
    return m.OrderRequest(
        client_order_id=client_order_id,
        symbol="USD_JPY",
        side=side,
        order_type=order_type,
        quantity=quantity,
        created_at_utc=NOW,
        limit_price=limit_price,
    )


# --- configuration: demo-only fail-closed ------------------------------------

def test_config_defaults_to_the_demo_endpoint():
    config = make_config()
    assert config.endpoint == oa.OANDA_DEMO_API_URL


def test_config_normalizes_a_trailing_slash():
    config = make_config(endpoint=oa.OANDA_DEMO_API_URL + "/")
    assert config.endpoint == oa.OANDA_DEMO_API_URL


def test_config_refuses_the_live_endpoint_fail_closed():
    with pytest.raises(ValueError) as exc_info:
        make_config(endpoint="https://api-fxtrade.oanda.com")
    assert "live endpoint" in str(exc_info.value)
    assert "demo-only" in str(exc_info.value)


def test_endpoint_rejection_messages_are_fixed_text_without_any_url():
    # PR #101 re-review: neither the configured value nor any endpoint URL
    # (allowed one included) may be echoed by the rejection.
    for bad in ("https://api-fxtrade.oanda.com", "https://evil.example/path?x=1"):
        with pytest.raises(ValueError) as exc_info:
            make_config(endpoint=bad)
        text = str(exc_info.value)
        assert bad not in text
        assert "https://" not in text
        assert "oanda.com" not in text


def test_config_refuses_the_live_stream_endpoint_and_case_variants():
    for live in (
        "https://stream-fxtrade.oanda.com",
        "https://API-FXTRADE.oanda.com",
        "https://api-fxtrade.oanda.com/",
    ):
        with pytest.raises(ValueError):
            make_config(endpoint=live)


def test_config_refuses_any_unknown_endpoint_allowlist_not_blocklist():
    for endpoint in (
        "https://example.com",
        "http://api-fxpractice.oanda.com",   # https only
        "https://api-fxpractice.oanda.com.evil.example",
    ):
        with pytest.raises(ValueError):
            make_config(endpoint=endpoint)


def test_config_requires_token_and_account_id():
    with pytest.raises(ValueError):
        make_config(api_token="  ")
    with pytest.raises(ValueError):
        make_config(account_id="")


def test_config_rejects_a_non_positive_or_non_finite_timeout():
    for timeout in (0, -1, "10", True, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            make_config(timeout_seconds=timeout)


def test_from_env_reads_only_environment_variables():
    env = {
        oa.ENV_API_TOKEN: TOKEN,
        oa.ENV_ACCOUNT_ID: ACCOUNT,
        oa.ENV_API_URL: oa.OANDA_DEMO_API_URL,
        oa.ENV_HTTP_TIMEOUT_SECONDS: "5.5",
    }
    config = oa.OandaDemoConfig.from_env(env)
    assert config.api_token == TOKEN
    assert config.account_id == ACCOUNT
    assert config.endpoint == oa.OANDA_DEMO_API_URL
    assert config.timeout_seconds == 5.5


def test_from_env_defaults_endpoint_and_timeout_when_unset():
    config = oa.OandaDemoConfig.from_env({oa.ENV_API_TOKEN: TOKEN, oa.ENV_ACCOUNT_ID: ACCOUNT})
    assert config.endpoint == oa.OANDA_DEMO_API_URL
    assert config.timeout_seconds > 0


def test_from_env_fails_closed_on_missing_or_live_configuration():
    with pytest.raises(ValueError) as missing_token:
        oa.OandaDemoConfig.from_env({oa.ENV_ACCOUNT_ID: ACCOUNT})
    assert oa.ENV_API_TOKEN in str(missing_token.value)
    with pytest.raises(ValueError) as missing_account:
        oa.OandaDemoConfig.from_env({oa.ENV_API_TOKEN: TOKEN})
    assert oa.ENV_ACCOUNT_ID in str(missing_account.value)
    with pytest.raises(ValueError):
        oa.OandaDemoConfig.from_env(
            {
                oa.ENV_API_TOKEN: TOKEN,
                oa.ENV_ACCOUNT_ID: ACCOUNT,
                oa.ENV_API_URL: "https://api-fxtrade.oanda.com",
            }
        )
    with pytest.raises(ValueError) as bad_timeout:
        oa.OandaDemoConfig.from_env(
            {
                oa.ENV_API_TOKEN: TOKEN,
                oa.ENV_ACCOUNT_ID: ACCOUNT,
                oa.ENV_HTTP_TIMEOUT_SECONDS: "fast",
            }
        )
    assert oa.ENV_HTTP_TIMEOUT_SECONDS in str(bad_timeout.value)


def test_config_error_messages_never_echo_the_secret_values():
    try:
        make_config(endpoint="https://api-fxtrade.oanda.com")
    except ValueError as exc:
        text = str(exc)
        assert TOKEN not in text
        assert ACCOUNT not in text


def test_config_repr_hides_token_account_id_and_endpoint():
    # PR #101 review: the endpoint is environment-managed like the
    # credentials and must not surface in repr either.
    text = repr(make_config())
    assert TOKEN not in text
    assert ACCOUNT not in text
    assert oa.OANDA_DEMO_API_URL not in text


def test_transport_mapped_errors_carry_no_cause_chain():
    # `raise ... from None`: no library exception (which may embed the URL /
    # headers) may survive into the traceback chain.
    adapter, _ = make_adapter(oa.TransportTimeoutError("t"))
    with pytest.raises(m.BrokerTimeoutError) as exc_info:
        adapter.submit_order(make_request())
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


# --- protocol conformance / connection identity --------------------------------

def test_adapter_satisfies_the_broker_protocol():
    adapter, _ = make_adapter()
    assert isinstance(adapter, Broker)


def test_connection_identity_is_demo_with_a_non_secret_target_label():
    # PR #101 review: the target is the constant "oanda-demo" label, never
    # the environment-managed endpoint URL.
    adapter, _ = make_adapter()
    identity = adapter.connection_identity()
    assert isinstance(identity, m.BrokerConnectionIdentity)
    assert identity.mode == "demo"
    assert identity.target == oa.OANDA_DEMO_CONNECTION_TARGET
    assert oa.OANDA_DEMO_API_URL not in identity.target
    # constant for the instance lifetime (Issue #75)
    assert adapter.connection_identity() == identity


def test_adapter_rejects_non_config_and_broken_injectables():
    with pytest.raises(ValueError):
        oa.OandaDemoAdapter("not-a-config")
    with pytest.raises(ValueError):
        oa.OandaDemoAdapter(make_config(), transport="not-callable")
    with pytest.raises(ValueError):
        oa.OandaDemoAdapter(make_config(), clock="not-callable")


# --- submit_order: request translation -----------------------------------------

def fill_response(client_order_id="c-1", units="1", price="155.123"):
    """A 201 create+fill response with the correlation fields OANDA echoes
    (clientExtensions on the create tx, clientOrderID on the fill tx)."""
    return (
        201,
        {
            "orderCreateTransaction": {
                "id": "1001",
                "time": "2026-01-02T03:04:05.123456789Z",
                "clientExtensions": {"id": client_order_id},
            },
            "orderFillTransaction": {
                "id": "1002",
                "orderID": "1001",
                "clientOrderID": client_order_id,
                "units": units,
                "price": price,
                "time": "2026-01-02T03:04:05.123456789Z",
            },
        },
    )


FILL_RESPONSE = fill_response()


def test_submit_market_buy_sends_the_oanda_order_payload():
    adapter, transport = make_adapter(FILL_RESPONSE)
    adapter.submit_order(make_request())
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{oa.OANDA_DEMO_API_URL}/v3/accounts/{ACCOUNT}/orders"
    body = json.loads(call["body"])
    assert body["order"]["type"] == "MARKET"
    assert body["order"]["instrument"] == "USD_JPY"
    assert body["order"]["units"] == "1"
    assert body["order"]["timeInForce"] == "FOK"
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_submit_sends_the_client_order_id_as_client_extension():
    # client_order_id is the execution store's 1:1 key to the
    # execution_request_id (Issue #74); OANDA must persist it.
    adapter, transport = make_adapter(fill_response(client_order_id="c-linked-42"))
    adapter.submit_order(make_request(client_order_id="c-linked-42"))
    body = json.loads(transport.calls[0]["body"])
    assert body["order"]["clientExtensions"]["id"] == "c-linked-42"


def test_submit_market_sell_uses_negative_units():
    adapter, transport = make_adapter(
        (
            201,
            {
                "orderCreateTransaction": {"id": "2001", "clientExtensions": {"id": "c-1"}},
                "orderFillTransaction": {
                    "id": "2002",
                    "orderID": "2001",
                    "clientOrderID": "c-1",
                    "units": "-3",
                    "price": "155.001",
                    "time": "2026-01-02T03:04:06Z",
                },
            },
        )
    )
    result = adapter.submit_order(make_request(side=m.OrderSide.SELL, quantity=Decimal("3")))
    assert json.loads(transport.calls[0]["body"])["order"]["units"] == "-3"
    assert result.side is m.OrderSide.SELL
    assert result.filled_quantity == Decimal("3")


def test_submit_limit_sends_price_and_gtc():
    adapter, transport = make_adapter(
        (201, {"orderCreateTransaction": {"id": "3001", "clientExtensions": {"id": "c-1"}}})
    )
    result = adapter.submit_order(
        make_request(order_type=m.OrderType.LIMIT, limit_price=Decimal("50.000"))
    )
    body = json.loads(transport.calls[0]["body"])
    assert body["order"]["type"] == "LIMIT"
    assert body["order"]["price"] == "50.000"
    assert body["order"]["timeInForce"] == "GTC"
    assert result.state is m.OrderState.ACCEPTED
    assert result.broker_order_id == "3001"
    assert result.requested_price == Decimal("50.000")


def test_submit_rejects_a_non_order_request():
    adapter, _ = make_adapter()
    with pytest.raises(m.BrokerInvalidRequestError):
        adapter.submit_order({"symbol": "USD_JPY"})


# --- submit_order: response translation -----------------------------------------

def test_submit_fill_translates_to_a_filled_order_result():
    adapter, _ = make_adapter(FILL_RESPONSE)
    result = adapter.submit_order(make_request())
    assert result.state is m.OrderState.FILLED
    assert result.client_order_id == "c-1"
    assert result.broker_order_id == "1001"
    assert result.filled_quantity == Decimal("1")
    assert result.remaining_quantity == Decimal("0")
    assert result.average_fill_price == Decimal("155.123")
    assert result.fills[0].fill_id == "1002"
    assert result.fills[0].filled_at_utc == datetime(
        2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc
    )
    assert result.requested_price is None  # MARKET


def test_submit_fok_cancel_translates_to_cancelled():
    adapter, _ = make_adapter(
        (
            201,
            {
                "orderCreateTransaction": {"id": "4001", "clientExtensions": {"id": "c-1"}},
                "orderCancelTransaction": {"id": "4002", "orderID": "4001",
                                           "clientOrderID": "c-1",
                                           "reason": "INSUFFICIENT_LIQUIDITY"},
            },
        )
    )
    result = adapter.submit_order(make_request())
    assert result.state is m.OrderState.CANCELLED
    assert result.broker_order_id == "4001"
    assert result.filled_quantity == Decimal("0")


def test_submit_broker_rejection_is_a_result_not_an_exception():
    adapter, _ = make_adapter(
        (
            400,
            {
                "orderRejectTransaction": {"id": "5001", "rejectReason": "INSUFFICIENT_MARGIN"},
                "errorMessage": "insufficient margin",
            },
        )
    )
    result = adapter.submit_order(make_request())
    assert result.state is m.OrderState.REJECTED
    assert result.reject_reason == "INSUFFICIENT_MARGIN"
    assert result.broker_order_id is None
    assert result.filled_quantity == Decimal("0")


def test_submit_invalid_request_without_reject_transaction_raises():
    adapter, _ = make_adapter((400, {"errorMessage": "Invalid value specified for 'units'"}))
    with pytest.raises(m.BrokerInvalidRequestError) as exc_info:
        adapter.submit_order(make_request())
    assert exc_info.value.code is m.BrokerErrorCode.INVALID_REQUEST
    assert "units" in str(exc_info.value)


# --- error taxonomy translation -------------------------------------------------

def test_transport_timeout_becomes_broker_timeout_with_exactly_one_attempt():
    adapter, transport = make_adapter(oa.TransportTimeoutError("HTTP request timed out"))
    with pytest.raises(m.BrokerTimeoutError) as exc_info:
        adapter.submit_order(make_request())
    assert exc_info.value.code is m.BrokerErrorCode.TIMEOUT
    assert exc_info.value.client_order_id == "c-1"
    # NO automatic resend on timeout (Issue #79 acceptance criterion).
    assert len(transport.calls) == 1


def test_connection_failure_becomes_communication_error_without_resend():
    adapter, transport = make_adapter(oa.TransportConnectionError("no route"))
    with pytest.raises(m.BrokerCommunicationError):
        adapter.submit_order(make_request())
    assert len(transport.calls) == 1


def test_http_401_and_403_become_authentication_errors():
    for status in (401, 403):
        adapter, _ = make_adapter((status, {"errorMessage": "Insufficient authorization"}))
        with pytest.raises(m.BrokerAuthenticationError) as exc_info:
            adapter.get_account_state()
        assert exc_info.value.code is m.BrokerErrorCode.AUTHENTICATION_FAILURE


def test_http_429_becomes_rate_limit_error_without_retry():
    adapter, transport = make_adapter((429, {"errorMessage": "Rate limit exceeded"}))
    with pytest.raises(m.BrokerRateLimitError) as exc_info:
        adapter.submit_order(make_request())
    assert exc_info.value.code is m.BrokerErrorCode.RATE_LIMITED
    assert len(transport.calls) == 1


def test_http_5xx_after_submit_is_unknown_outcome_not_communication_failure():
    # The server saw the request: the order may exist, so the failure must
    # never claim "not placed" (Issue #74 UNKNOWN contract).
    adapter, _ = make_adapter((500, {}))
    with pytest.raises(m.BrokerTimeoutError):
        adapter.submit_order(make_request())


def test_http_5xx_on_a_read_is_a_communication_failure():
    adapter, _ = make_adapter((503, {}))
    with pytest.raises(m.BrokerCommunicationError):
        adapter.get_positions()


def test_malformed_body_after_submit_is_unknown_outcome():
    adapter, _ = make_adapter((201, "this is not json"))
    with pytest.raises(m.BrokerTimeoutError):
        adapter.submit_order(make_request())


def test_error_messages_never_leak_token_account_or_url():
    failures = []
    for scripted in (
        oa.TransportTimeoutError("t"),
        oa.TransportConnectionError("c"),
        (401, {"errorMessage": "unauthorized"}),
        (429, {}),
        (500, {}),
    ):
        adapter, _ = make_adapter(scripted)
        try:
            adapter.submit_order(make_request())
        except m.BrokerError as exc:
            failures.append(str(exc))
    assert failures
    for text in failures:
        assert TOKEN not in text
        assert ACCOUNT not in text
        assert "https://" not in text


# --- get_order -------------------------------------------------------------------

def test_get_order_pending_limit_translates_to_accepted():
    adapter, transport = make_adapter(
        (
            200,
            {
                "order": {
                    "id": "6001",
                    "type": "LIMIT",
                    "instrument": "USD_JPY",
                    "units": "2",
                    "price": "50.000",
                    "state": "PENDING",
                    "clientExtensions": {"id": "c-7"},
                }
            },
        )
    )
    result = adapter.get_order("c-7")
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["url"].endswith(f"/v3/accounts/{ACCOUNT}/orders/@c-7")
    assert result.state is m.OrderState.ACCEPTED
    assert result.client_order_id == "c-7"
    assert result.broker_order_id == "6001"
    assert result.side is m.OrderSide.BUY
    assert result.requested_quantity == Decimal("2")
    assert result.requested_price == Decimal("50.000")


def test_get_order_filled_market_fetches_the_filling_transaction():
    adapter, transport = make_adapter(
        (
            200,
            {
                "order": {
                    "id": "7001",
                    "type": "MARKET",
                    "instrument": "USD_JPY",
                    "units": "-5",
                    "state": "FILLED",
                    "fillingTransactionID": "7002",
                    "clientExtensions": {"id": "c-8"},
                }
            },
        ),
        (
            200,
            {
                "transaction": {
                    "id": "7002",
                    "type": "ORDER_FILL",
                    "units": "-5",
                    "price": "154.500",
                    "time": "2026-01-02T03:04:07.000000000Z",
                }
            },
        ),
    )
    result = adapter.get_order("c-8")
    assert transport.calls[1]["url"].endswith(f"/v3/accounts/{ACCOUNT}/transactions/7002")
    assert result.state is m.OrderState.FILLED
    assert result.side is m.OrderSide.SELL
    assert result.filled_quantity == Decimal("5")
    assert result.fills[0].price == Decimal("154.500")


def test_get_order_cancelled_translates_to_cancelled():
    adapter, _ = make_adapter(
        (
            200,
            {
                "order": {
                    "id": "8001",
                    "type": "LIMIT",
                    "instrument": "USD_JPY",
                    "units": "1",
                    "price": "50.000",
                    "state": "CANCELLED",
                    "clientExtensions": {"id": "c-9"},
                }
            },
        )
    )
    assert adapter.get_order("c-9").state is m.OrderState.CANCELLED


def test_get_order_unknown_id_raises_not_found():
    adapter, _ = make_adapter((404, {"errorMessage": "The order specifier is invalid"}))
    with pytest.raises(m.BrokerOrderNotFoundError) as exc_info:
        adapter.get_order("c-none")
    assert exc_info.value.code is m.BrokerErrorCode.NOT_FOUND
    assert exc_info.value.client_order_id == "c-none"


def test_get_order_rejects_a_blank_client_order_id():
    adapter, _ = make_adapter()
    with pytest.raises(m.BrokerInvalidRequestError):
        adapter.get_order("   ")


# --- cancel_order ------------------------------------------------------------------

def test_cancel_order_returns_the_post_cancel_snapshot():
    adapter, transport = make_adapter(
        (200, {"orderCancelTransaction": {"id": "9002", "orderID": "9001",
                                          "clientOrderID": "c-10"}}),
        (
            200,
            {
                "order": {
                    "id": "9001",
                    "type": "LIMIT",
                    "instrument": "USD_JPY",
                    "units": "1",
                    "price": "50.000",
                    "state": "CANCELLED",
                    "clientExtensions": {"id": "c-10"},
                }
            },
        ),
    )
    result = adapter.cancel_order("c-10")
    assert transport.calls[0]["method"] == "PUT"
    assert transport.calls[0]["url"].endswith(f"/orders/@c-10/cancel")
    assert result.state is m.OrderState.CANCELLED
    assert result.broker_order_id == "9001"


def test_cancel_of_a_terminal_order_is_a_cancellation_failure():
    adapter, _ = make_adapter(
        (404, {"errorMessage": "The Order specified does not exist"}),
        (
            200,
            {
                "order": {
                    "id": "10001",
                    "type": "MARKET",
                    "instrument": "USD_JPY",
                    "units": "1",
                    "state": "FILLED",
                    "fillingTransactionID": "10002",
                    "clientExtensions": {"id": "c-11"},
                }
            },
        ),
        (
            200,
            {
                "transaction": {
                    "id": "10002",
                    "units": "1",
                    "price": "155.000",
                    "time": "2026-01-02T03:04:08Z",
                }
            },
        ),
    )
    with pytest.raises(m.BrokerCancellationError) as exc_info:
        adapter.cancel_order("c-11")
    assert exc_info.value.code is m.BrokerErrorCode.CANCELLATION_FAILURE


def test_cancel_without_a_cancel_transaction_is_refused():
    # PR #101 re-review: a 200 PUT answer without orderCancelTransaction is
    # never treated as a confirmed cancel — no follow-up GET can repair it.
    adapter, transport = make_adapter((200, {}))
    with pytest.raises(m.BrokerCommunicationError) as exc_info:
        adapter.cancel_order("c-10")
    assert "orderCancelTransaction" in str(exc_info.value)
    assert len(transport.calls) == 1  # the GET is never reached


def test_cancel_with_missing_client_order_id_echo_is_refused():
    adapter, transport = make_adapter(
        (200, {"orderCancelTransaction": {"id": "9002", "orderID": "9001"}})
    )
    with pytest.raises(m.BrokerCommunicationError):
        adapter.cancel_order("c-10")
    assert len(transport.calls) == 1


def test_cancel_with_mismatched_client_order_id_echo_is_refused():
    adapter, transport = make_adapter(
        (200, {"orderCancelTransaction": {"id": "9002", "orderID": "9001",
                                          "clientOrderID": "c-SOMEONE-ELSE"}})
    )
    with pytest.raises(m.BrokerCommunicationError):
        adapter.cancel_order("c-10")
    assert len(transport.calls) == 1


def test_cancel_with_malformed_response_body_is_refused():
    adapter, _ = make_adapter((200, "this is not json"))
    with pytest.raises(m.BrokerCommunicationError):
        adapter.cancel_order("c-10")


def test_cancel_whose_post_cancel_snapshot_is_not_cancelled_is_refused():
    # The cancel transaction correlates, but the follow-up GET reports a
    # non-cancelled state: inconsistent broker responses fail closed.
    adapter, _ = make_adapter(
        (200, {"orderCancelTransaction": {"id": "9002", "orderID": "9001",
                                          "clientOrderID": "c-10"}}),
        (
            200,
            {
                "order": {
                    "id": "9001",
                    "type": "LIMIT",
                    "instrument": "USD_JPY",
                    "units": "1",
                    "price": "50.000",
                    "state": "PENDING",
                    "clientExtensions": {"id": "c-10"},
                }
            },
        ),
    )
    with pytest.raises(m.BrokerCommunicationError) as exc_info:
        adapter.cancel_order("c-10")
    assert "post-cancel" in str(exc_info.value)


def test_cancel_of_an_unknown_order_raises_not_found():
    adapter, _ = make_adapter(
        (404, {"errorMessage": "The Order specified does not exist"}),
        (404, {"errorMessage": "The Order specified does not exist"}),
    )
    with pytest.raises(m.BrokerOrderNotFoundError):
        adapter.cancel_order("c-none")


# --- get_positions / get_account_state / instruments -------------------------------

def test_get_positions_translates_long_and_short_sides():
    adapter, _ = make_adapter(
        (
            200,
            {
                "positions": [
                    {
                        "instrument": "USD_JPY",
                        "long": {"units": "7", "averagePrice": "155.100"},
                        "short": {"units": "0"},
                    },
                    {
                        "instrument": "EUR_USD",
                        "long": {"units": "0"},
                        "short": {"units": "-2", "averagePrice": "1.0850"},
                    },
                ]
            },
        )
    )
    positions = adapter.get_positions()
    assert len(positions) == 2
    long_pos, short_pos = positions
    assert long_pos.symbol == "USD_JPY"
    assert long_pos.side is m.PositionSide.LONG
    assert long_pos.quantity == Decimal("7")
    assert long_pos.average_entry_price == Decimal("155.100")
    assert long_pos.retrieved_at_utc == NOW
    assert short_pos.side is m.PositionSide.SHORT
    assert short_pos.quantity == Decimal("2")


def test_get_positions_empty_account_returns_an_empty_tuple():
    adapter, _ = make_adapter((200, {"positions": []}))
    assert adapter.get_positions() == ()


def test_get_account_state_translates_the_summary():
    adapter, _ = make_adapter(
        (
            200,
            {
                "account": {
                    "id": ACCOUNT,
                    "currency": "JPY",
                    "balance": "3000000.0000",
                    "NAV": "3000123.4567",
                    "marginAvailable": "2999876.0000",
                }
            },
        )
    )
    state = adapter.get_account_state()
    # identifier semantics preserved; concealment is repr-level only
    assert state.account_id == ACCOUNT
    assert ACCOUNT not in repr(state)
    assert state.currency == "JPY"
    assert state.balance == Decimal("3000000.0000")
    assert state.equity == Decimal("3000123.4567")
    assert state.margin_available == Decimal("2999876.0000")
    assert state.retrieved_at_utc == NOW


def test_get_tradable_instruments_returns_sorted_names():
    adapter, transport = make_adapter(
        (200, {"instruments": [{"name": "USD_JPY"}, {"name": "EUR_USD"}]})
    )
    assert adapter.get_tradable_instruments() == ("EUR_USD", "USD_JPY")
    assert transport.calls[0]["url"].endswith(f"/v3/accounts/{ACCOUNT}/instruments")


def test_malformed_query_responses_fail_as_communication_errors():
    for scripted, call in (
        ((200, {"order": "not-an-object"}), lambda a: a.get_order("c-1")),
        ((200, {}), lambda a: a.get_positions()),
        ((200, {"positions": [{"long": {"units": "1"}}]}), lambda a: a.get_positions()),
        ((200, {"account": {"id": ACCOUNT, "currency": "JPY", "balance": "x",
                            "NAV": "1", "marginAvailable": "1"}}),
         lambda a: a.get_account_state()),
        ((200, {}), lambda a: a.get_tradable_instruments()),
    ):
        adapter, _ = make_adapter(scripted)
        with pytest.raises(m.BrokerCommunicationError):
            call(adapter)


# --- PR #101 review: redirects are never followed --------------------------------

def test_redirect_on_submit_is_refused_with_a_single_connection():
    # A 30x answer must not trigger a second exchange (allow_redirects=False
    # in the default transport; the adapter converts the 3xx itself).
    for status in (301, 302, 307, 308):
        adapter, transport = make_adapter((status, ""))
        with pytest.raises(m.BrokerTimeoutError):
            adapter.submit_order(make_request())
        assert len(transport.calls) == 1


def test_redirect_on_a_read_is_a_communication_failure_without_follow_up():
    adapter, transport = make_adapter((302, ""))
    with pytest.raises(m.BrokerCommunicationError) as exc_info:
        adapter.get_account_state()
    assert len(transport.calls) == 1
    assert "redirect" in str(exc_info.value)


def test_default_transport_disables_redirect_following():
    import inspect

    source = inspect.getsource(oa._requests_transport)
    assert "allow_redirects=False" in source


# --- PR #101 review: secret redaction / correlation --------------------------------

def test_broker_echoed_error_messages_are_redacted():
    # The broker may reflect request values (account id, token, endpoint)
    # back in errorMessage / rejectReason; all must be scrubbed.
    adapter, _ = make_adapter(
        (
            400,
            {
                "errorMessage": (
                    f"account {ACCOUNT} rejected token {TOKEN} "
                    f"at {oa.OANDA_DEMO_API_URL}/v3"
                )
            },
        )
    )
    with pytest.raises(m.BrokerInvalidRequestError) as exc_info:
        adapter.submit_order(make_request())
    text = str(exc_info.value)
    assert ACCOUNT not in text
    assert TOKEN not in text
    assert oa.OANDA_DEMO_API_URL not in text
    assert "***" in text


def test_broker_echoed_reject_reason_is_redacted_in_the_result():
    adapter, _ = make_adapter(
        (
            400,
            {
                "orderRejectTransaction": {
                    "id": "5001",
                    "rejectReason": (
                        f"MARGIN for {ACCOUNT} via {oa.OANDA_DEMO_API_URL} token {TOKEN}"
                    ),
                }
            },
        )
    )
    result = adapter.submit_order(make_request())
    assert result.state is m.OrderState.REJECTED
    assert ACCOUNT not in result.reject_reason
    assert TOKEN not in result.reject_reason
    assert oa.OANDA_DEMO_API_URL not in result.reject_reason


def test_zero_or_negative_fill_price_is_a_communication_error():
    # A malformed RESPONSE, never a caller-side BrokerInvalidRequestError
    # (PR #101 re-review) — via submit...
    for bad_price in ("0", "-1.5"):
        adapter, _ = make_adapter(fill_response(price=bad_price))
        with pytest.raises(m.BrokerCommunicationError):
            adapter.submit_order(make_request())
    # ...and via the filled-order query's filling transaction.
    adapter, _ = make_adapter(
        (
            200,
            {
                "order": {
                    "id": "7001",
                    "type": "MARKET",
                    "instrument": "USD_JPY",
                    "units": "1",
                    "state": "FILLED",
                    "fillingTransactionID": "7002",
                    "clientExtensions": {"id": "c-8"},
                }
            },
        ),
        (
            200,
            {
                "transaction": {
                    "id": "7002",
                    "units": "1",
                    "price": "0",
                    "time": "2026-01-02T03:04:07Z",
                }
            },
        ),
    )
    with pytest.raises(m.BrokerCommunicationError):
        adapter.get_order("c-8")


def test_zero_or_negative_open_trade_price_is_a_communication_error():
    for bad_price in ("0", "-155.0"):
        adapter, _ = make_adapter(
            (
                200,
                {
                    "trades": [
                        {
                            "id": "T-1",
                            "instrument": "USD_JPY",
                            "currentUnits": "1",
                            "price": bad_price,
                            "openTime": "2026-01-02T03:00:00Z",
                        }
                    ]
                },
            )
        )
        with pytest.raises(m.BrokerCommunicationError):
            adapter.get_open_trades()


def test_account_state_keeps_the_real_id_but_conceals_it_at_display_boundaries():
    # PR #101 re-review: the REAL id is the identifier downstream layers
    # (Safety Decision / audit / shadow report) rely on — only repr and the
    # display digest conceal it.
    adapter, _ = make_adapter(
        (
            200,
            {
                "account": {
                    "id": ACCOUNT,
                    "currency": "JPY",
                    "balance": "1",
                    "NAV": "1",
                    "marginAvailable": "1",
                }
            },
        )
    )
    state = adapter.get_account_state()
    assert state.account_id == ACCOUNT
    assert ACCOUNT not in repr(state)
    assert ACCOUNT not in state.display_account_id


def test_account_state_for_a_different_account_id_fails_closed():
    adapter, _ = make_adapter(
        (
            200,
            {
                "account": {
                    "id": "999-000-9999999-999",
                    "currency": "JPY",
                    "balance": "1",
                    "NAV": "1",
                    "marginAvailable": "1",
                }
            },
        )
    )
    with pytest.raises(m.BrokerCommunicationError) as exc_info:
        adapter.get_account_state()
    text = str(exc_info.value)
    assert ACCOUNT not in text
    assert "999-000-9999999-999" not in text


def test_submit_fill_without_client_order_id_echo_is_refused():
    response = fill_response()
    del response[1]["orderFillTransaction"]["clientOrderID"]
    adapter, _ = make_adapter(response)
    with pytest.raises(m.BrokerCommunicationError):
        adapter.submit_order(make_request())


def test_submit_fill_with_a_different_client_order_id_echo_is_refused():
    adapter, _ = make_adapter(fill_response(client_order_id="c-SOMEONE-ELSE"))
    with pytest.raises(m.BrokerCommunicationError):
        adapter.submit_order(make_request())


def test_submit_create_without_client_extension_echo_is_refused():
    adapter, _ = make_adapter((201, {"orderCreateTransaction": {"id": "3001"}}))
    with pytest.raises(m.BrokerCommunicationError):
        adapter.submit_order(
            make_request(order_type=m.OrderType.LIMIT, limit_price=Decimal("50.000"))
        )


def test_get_order_with_missing_or_mismatched_client_extension_is_refused():
    for extensions in ({}, {"clientExtensions": {"id": "c-OTHER"}}):
        order = {
            "id": "6001",
            "type": "LIMIT",
            "instrument": "USD_JPY",
            "units": "2",
            "price": "50.000",
            "state": "PENDING",
        }
        order.update(extensions)
        adapter, _ = make_adapter((200, {"order": order}))
        with pytest.raises(m.BrokerCommunicationError):
            adapter.get_order("c-7")


# --- PR #101 review: client_order_id URL safety -------------------------------------

def test_url_unsafe_or_oversized_client_order_ids_are_rejected():
    adapter, transport = make_adapter()
    for bad in ("a/b", "a?b", "a#b", "a%b", "a@b", "a b", "日本語", ".leading-dot",
                "x" * 129):
        with pytest.raises(m.BrokerInvalidRequestError):
            adapter.get_order(bad)
        with pytest.raises(m.BrokerInvalidRequestError):
            adapter.cancel_order(bad)
    # nothing ever reached the transport
    assert transport.calls == []


def test_url_unsafe_client_order_id_is_rejected_before_submit():
    adapter, transport = make_adapter()
    request = m.OrderRequest(
        client_order_id="bad/id",
        symbol="USD_JPY",
        side=m.OrderSide.BUY,
        order_type=m.OrderType.MARKET,
        quantity=Decimal("1"),
        created_at_utc=NOW,
    )
    with pytest.raises(m.BrokerInvalidRequestError):
        adapter.submit_order(request)
    assert transport.calls == []


def test_max_length_url_safe_client_order_id_is_accepted():
    cid = "x" * 128
    adapter, _ = make_adapter(fill_response(client_order_id=cid))
    assert adapter.submit_order(make_request(client_order_id=cid)).state is m.OrderState.FILLED


# --- PR #101 review: duplicate contract / partial fills ------------------------------

def test_duplicate_client_order_id_rejection_maps_to_invalid_request():
    # Broker-boundary contract (Issue #73 / FakeBroker parity): a duplicate
    # client_order_id is BrokerInvalidRequestError, not an ordinary REJECTED.
    adapter, _ = make_adapter(
        (
            400,
            {
                "orderRejectTransaction": {
                    "id": "5002",
                    "rejectReason": "CLIENT_ORDER_ID_ALREADY_EXISTS",
                }
            },
        )
    )
    with pytest.raises(m.BrokerInvalidRequestError) as exc_info:
        adapter.submit_order(make_request())
    assert exc_info.value.code is m.BrokerErrorCode.INVALID_REQUEST
    assert "duplicate" in str(exc_info.value)


def test_partial_fill_with_working_remainder_is_partially_filled():
    adapter, _ = make_adapter(fill_response(units="2", price="155.000"))
    result = adapter.submit_order(make_request(quantity=Decimal("5")))
    assert result.state is m.OrderState.PARTIALLY_FILLED
    assert result.filled_quantity == Decimal("2")
    assert result.remaining_quantity == Decimal("3")
    assert result.broker_order_id == "1001"


def test_reissue_transactions_are_deliberately_unsupported_and_fail_closed():
    # Issue #102 owns reissue handling across the state machine / audit /
    # query / cancel paths. Until then a reissue-carrying response is NOT
    # interpreted: the submit fails toward UNKNOWN (no resend), so the
    # recorded state can never misrepresent a reissued order.
    for extra_key in ("orderReissueTransaction", "orderReissueRejectTransaction"):
        response = fill_response(units="2", price="155.000")
        response[1][extra_key] = {"id": "1003"}
        adapter, transport = make_adapter(response)
        with pytest.raises(m.BrokerTimeoutError) as exc_info:
            adapter.submit_order(make_request(quantity=Decimal("5")))
        assert exc_info.value.code is m.BrokerErrorCode.TIMEOUT
        assert "reissue" in str(exc_info.value)
        assert "unsupported" in str(exc_info.value)
        # one attempt, no automatic resend
        assert len(transport.calls) == 1


def test_partial_fill_with_cancelled_remainder_is_cancelled():
    response = fill_response(units="2", price="155.000")
    response[1]["orderCancelTransaction"] = {
        "id": "1004", "orderID": "1001", "clientOrderID": "c-1",
    }
    adapter, _ = make_adapter(response)
    result = adapter.submit_order(make_request(quantity=Decimal("5")))
    assert result.state is m.OrderState.CANCELLED


def test_overfill_or_zero_fill_responses_are_communication_errors_not_value_errors():
    for units in ("9", "0"):
        adapter, _ = make_adapter(fill_response(units=units))
        with pytest.raises(m.BrokerCommunicationError):
            adapter.submit_order(make_request(quantity=Decimal("5")))


def test_inconsistent_filled_order_query_raises_a_common_error_not_value_error():
    # order says 5 units but the filling transaction reports 3: the common
    # model's FILLED invariant would raise ValueError; the adapter must wrap
    # it (PR #101 review).
    adapter, _ = make_adapter(
        (
            200,
            {
                "order": {
                    "id": "7001",
                    "type": "MARKET",
                    "instrument": "USD_JPY",
                    "units": "5",
                    "state": "FILLED",
                    "fillingTransactionID": "7002",
                    "clientExtensions": {"id": "c-8"},
                }
            },
        ),
        (
            200,
            {
                "transaction": {
                    "id": "7002",
                    "units": "3",
                    "price": "154.500",
                    "time": "2026-01-02T03:04:07Z",
                }
            },
        ),
    )
    with pytest.raises(m.BrokerCommunicationError):
        adapter.get_order("c-8")


def test_unknown_broker_order_type_is_a_communication_error():
    adapter, _ = make_adapter(
        (
            200,
            {
                "order": {
                    "id": "6001",
                    "type": "STOP",
                    "instrument": "USD_JPY",
                    "units": "1",
                    "state": "PENDING",
                    "clientExtensions": {"id": "c-7"},
                }
            },
        )
    )
    with pytest.raises(m.BrokerCommunicationError):
        adapter.get_order("c-7")


def test_missing_instrument_in_order_is_a_communication_error():
    adapter, _ = make_adapter(
        (
            200,
            {
                "order": {
                    "id": "6001",
                    "type": "MARKET",
                    "units": "1",
                    "state": "PENDING",
                    "clientExtensions": {"id": "c-7"},
                }
            },
        )
    )
    with pytest.raises(m.BrokerCommunicationError):
        adapter.get_order("c-7")


# --- PR #101 review: open trades ------------------------------------------------------

def test_get_open_trades_translates_long_and_short_trades():
    adapter, transport = make_adapter(
        (
            200,
            {
                "trades": [
                    {
                        "id": "T-1",
                        "instrument": "USD_JPY",
                        "currentUnits": "4",
                        "price": "155.200",
                        "openTime": "2026-01-02T03:00:00.000000000Z",
                    },
                    {
                        "id": "T-2",
                        "instrument": "EUR_USD",
                        "currentUnits": "-1",
                        "price": "1.0900",
                        "openTime": "2026-01-02T03:01:00Z",
                    },
                ]
            },
        )
    )
    trades = adapter.get_open_trades()
    assert transport.calls[0]["url"].endswith(f"/v3/accounts/{ACCOUNT}/openTrades")
    assert len(trades) == 2
    long_trade, short_trade = trades
    assert isinstance(long_trade, m.TradeInfo)
    assert long_trade.trade_id == "T-1"
    assert long_trade.side is m.PositionSide.LONG
    assert long_trade.quantity == Decimal("4")
    assert long_trade.open_price == Decimal("155.200")
    assert long_trade.opened_at_utc == datetime(2026, 1, 2, 3, 0, 0, tzinfo=timezone.utc)
    assert long_trade.retrieved_at_utc == NOW
    assert short_trade.side is m.PositionSide.SHORT
    assert short_trade.quantity == Decimal("1")


def test_get_open_trades_empty_account_returns_an_empty_tuple():
    adapter, _ = make_adapter((200, {"trades": []}))
    assert adapter.get_open_trades() == ()


def test_get_open_trades_malformed_responses_are_communication_errors():
    for scripted in (
        (200, {}),
        (200, {"trades": ["not-an-object"]}),
        (200, {"trades": [{"instrument": "USD_JPY", "currentUnits": "1"}]}),
        (200, {"trades": [{"id": "T-1", "instrument": "USD_JPY", "currentUnits": "0"}]}),
        (500, {}),
    ):
        adapter, _ = make_adapter(scripted)
        with pytest.raises(m.BrokerCommunicationError):
            adapter.get_open_trades()


def test_get_open_trades_auth_and_rate_limit_translate_like_other_reads():
    adapter, _ = make_adapter((401, {}))
    with pytest.raises(m.BrokerAuthenticationError):
        adapter.get_open_trades()
    adapter, _ = make_adapter((429, {}))
    with pytest.raises(m.BrokerRateLimitError):
        adapter.get_open_trades()
    adapter, _ = make_adapter((404, {"errorMessage": "account not found"}))
    with pytest.raises(m.BrokerCommunicationError):
        adapter.get_open_trades()


# --- timestamp parsing ----------------------------------------------------------

def test_nanosecond_timestamps_are_truncated_to_microseconds():
    parsed = oa._parse_utc_time("2026-01-02T03:04:05.987654321Z", "t")
    assert parsed == datetime(2026, 1, 2, 3, 4, 5, 987654, tzinfo=timezone.utc)


def test_offset_timestamps_are_normalized_to_utc():
    parsed = oa._parse_utc_time("2026-01-02T12:04:05.5+09:00", "t")
    assert parsed == datetime(2026, 1, 2, 3, 4, 5, 500000, tzinfo=timezone.utc)


def test_naive_or_malformed_timestamps_are_rejected():
    for value in ("2026-01-02T03:04:05", "not-a-time", "", None):
        with pytest.raises(m.BrokerCommunicationError):
            oa._parse_utc_time(value, "t")


# --- no network in this suite -----------------------------------------------------

def test_the_whole_translation_path_runs_with_sockets_disabled(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted during unit test")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)

    adapter, _ = make_adapter(
        FILL_RESPONSE,
        (200, {"positions": []}),
        (200, {"account": {"id": ACCOUNT, "currency": "JPY", "balance": "1",
                           "NAV": "1", "marginAvailable": "1"}}),
        (200, {"instruments": [{"name": "USD_JPY"}]}),
    )
    adapter.submit_order(make_request())
    adapter.get_positions()
    adapter.get_account_state()
    adapter.get_tradable_instruments()
    adapter.connection_identity()
