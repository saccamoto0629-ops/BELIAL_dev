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
                "type": "ORDER_FILL",
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
                    "orderID": "7001",
                    "clientOrderID": "c-8",
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
                    "type": "ORDER_FILL",
                    "orderID": "10001",
                    "clientOrderID": "c-11",
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
                    "type": "ORDER_FILL",
                    "orderID": "7001",
                    "clientOrderID": "c-8",
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


# --- PR #101 4th review: redaction order & overlapping secrets ---------------------

def test_account_id_straddling_the_truncation_boundary_leaves_no_fragment():
    # 195 filler chars put ACCOUNT across the 200-char cut: redaction must
    # run BEFORE truncation, so neither the full id nor a fragment survives.
    reason = "x" * 195 + ACCOUNT
    adapter, _ = make_adapter(
        (400, {"orderRejectTransaction": {"id": "5001", "rejectReason": reason}})
    )
    result = adapter.submit_order(make_request())
    assert result.state is m.OrderState.REJECTED
    assert ACCOUNT not in result.reject_reason
    assert ACCOUNT[:5] not in result.reject_reason
    assert "***" in result.reject_reason


def test_token_straddling_the_truncation_boundary_leaves_no_fragment():
    message = "y" * 190 + TOKEN
    adapter, _ = make_adapter((400, {"errorMessage": message}))
    with pytest.raises(m.BrokerInvalidRequestError) as exc_info:
        adapter.submit_order(make_request())
    text = str(exc_info.value)
    assert TOKEN not in text
    assert TOKEN[:10] not in text


def test_endpoint_straddling_the_truncation_boundary_leaves_no_fragment():
    message = "z" * 195 + oa.OANDA_DEMO_API_URL
    adapter, _ = make_adapter((400, {"errorMessage": message}))
    with pytest.raises(m.BrokerInvalidRequestError) as exc_info:
        adapter.submit_order(make_request())
    text = str(exc_info.value)
    assert oa.OANDA_DEMO_API_URL not in text
    assert "api-fxpractice" not in text


def test_containing_secrets_are_scrubbed_completely():
    # The token CONTAINS the account id: both must vanish entirely.
    overlap_token = f"tok-{ACCOUNT}-suffix"
    config = oa.OandaDemoConfig(api_token=overlap_token, account_id=ACCOUNT)
    transport = FakeTransport(
        (400, {"errorMessage": f"reflected {overlap_token} and {ACCOUNT} back"})
    )
    adapter = oa.OandaDemoAdapter(config, transport=transport, clock=lambda: NOW)
    with pytest.raises(m.BrokerInvalidRequestError) as exc_info:
        adapter.submit_order(make_request())
    text = str(exc_info.value)
    assert overlap_token not in text
    assert ACCOUNT not in text
    assert "tok-***" not in text  # no partial survivor of the longer secret


def test_partially_overlapping_secrets_leave_no_fragment():
    # PR #101 5th review: token=abcdef and account=defghi overlap (not
    # containment) inside "abcdefghi". Sequential replace would leave "ghi";
    # interval-based redaction must scrub the whole merged span.
    config = oa.OandaDemoConfig(api_token="abcdef", account_id="defghi")
    transport = FakeTransport((400, {"errorMessage": "reflected abcdefghi back"}))
    adapter = oa.OandaDemoAdapter(config, transport=transport, clock=lambda: NOW)
    with pytest.raises(m.BrokerInvalidRequestError) as exc_info:
        adapter.submit_order(make_request())
    text = str(exc_info.value)
    for fragment in ("abcdef", "defghi", "abc", "def", "ghi", "abcdefghi"):
        assert fragment not in text
    assert "reflected *** back" in text


def test_redact_secrets_interval_merging_matrix():
    # direct unit coverage of the pure interval-merger
    redact = oa._redact_secrets
    # multiple occurrences of the same secret
    assert redact("a SEC b SEC c", ("SEC",)) == "a *** b *** c"
    # three match intervals chaining into one merged span
    #   token "abcdef" @0 and @6, account "defabc" @3 -> one interval 0..12
    assert redact("abcdefabcdef!", ("abcdef", "defabc")) == "***!"
    # duplicate registrations of the same secret
    assert redact("x SEC y", ("SEC", "SEC", "SEC")) == "x *** y"
    # empty secrets are ignored (never treated as a match-everything value)
    assert redact("plain text", ("", "")) == "plain text"
    assert redact("a SEC b", ("", "SEC")) == "a *** b"
    # adjacent intervals merge
    assert redact("ABCDEF", ("ABC", "DEF")) == "***"
    # overlapping occurrences of one secret ("aaa" twice inside "aaaa")
    assert redact("aaaa", ("aaa",)) == "***"
    # no matches -> unchanged
    assert redact("nothing here", ("SEC",)) == "nothing here"


def test_overlapping_secrets_across_the_truncation_boundary_leave_no_fragment():
    # redaction happens on the FULL text before the 200-char cut, with the
    # overlapping pair straddling the boundary
    config = oa.OandaDemoConfig(api_token="abcdef", account_id="defghi")
    reason = "x" * 195 + "abcdefghi"
    transport = FakeTransport(
        (400, {"orderRejectTransaction": {"id": "5001", "rejectReason": reason}})
    )
    adapter = oa.OandaDemoAdapter(config, transport=transport, clock=lambda: NOW)
    result = adapter.submit_order(make_request())
    assert result.state is m.OrderState.REJECTED
    for fragment in ("abcdef", "defghi", "ghi"):
        assert fragment not in result.reject_reason
    assert len(result.reject_reason) <= 200


# --- PR #101 4th review: submit-side cancel/create correlation ---------------------

def test_partial_fill_with_foreign_cancel_transaction_fails_closed():
    # A cancel transaction belonging to another order must never turn OUR
    # partial fill into a terminal CANCELLED.
    response = fill_response(units="2", price="155.000")
    response[1]["orderCancelTransaction"] = {
        "id": "1004", "orderID": "9999", "clientOrderID": "c-SOMEONE-ELSE",
    }
    adapter, _ = make_adapter(response)
    with pytest.raises(m.BrokerCommunicationError):
        adapter.submit_order(make_request(quantity=Decimal("5")))


def test_partial_fill_with_empty_or_malformed_cancel_transaction_fails_closed():
    for bad_cancel in ({}, {"id": "1004"}, "CANCELLED", ["x"]):
        response = fill_response(units="2", price="155.000")
        response[1]["orderCancelTransaction"] = bad_cancel
        adapter, _ = make_adapter(response)
        with pytest.raises(m.BrokerCommunicationError):
            adapter.submit_order(make_request(quantity=Decimal("5")))


def test_fill_order_id_fallback_requires_a_correlated_create_transaction():
    # fill tx without orderID: the create-transaction fallback may only
    # donate a broker_order_id when the create tx is proven to be OURS.
    def response(create_tx):
        return (
            201,
            {
                "orderCreateTransaction": create_tx,
                "orderFillTransaction": {
                    "id": "1002",
                    "clientOrderID": "c-1",
                    "units": "1",
                    "price": "155.000",
                    "time": "2026-01-02T03:04:05Z",
                },
            },
        )

    # correlation missing on the create tx
    adapter, _ = make_adapter(response({"id": "1001"}))
    with pytest.raises(m.BrokerCommunicationError):
        adapter.submit_order(make_request())
    # correlation mismatch on the create tx
    adapter, _ = make_adapter(
        response({"id": "1001", "clientExtensions": {"id": "c-OTHER"}})
    )
    with pytest.raises(m.BrokerCommunicationError):
        adapter.submit_order(make_request())
    # correlated create tx works as the fallback source
    adapter, _ = make_adapter(
        response({"id": "1001", "clientExtensions": {"id": "c-1"}})
    )
    assert adapter.submit_order(make_request()).broker_order_id == "1001"


# --- PR #101 4th review: malformed reissue fields fail closed ----------------------

def test_malformed_reissue_values_fail_closed_like_wellformed_ones():
    # Key PRESENCE triggers the Issue #102 refusal: a string, an array, an
    # empty object or a null must not be skipped as "absent".
    for bad_value in ("REISSUED", ["tx"], {}, None):
        response = fill_response(units="2", price="155.000")
        response[1]["orderReissueTransaction"] = bad_value
        adapter, transport = make_adapter(response)
        with pytest.raises(m.BrokerTimeoutError):
            adapter.submit_order(make_request(quantity=Decimal("5")))
        assert len(transport.calls) == 1  # no resend


def test_reissue_key_beats_reject_interpretation_on_400_and_404():
    # PR #101 5th review: the reissue-key check runs BEFORE the business
    # interpretation, so a reject transaction sharing the body with a
    # reissue field is never confirmed as REJECTED — on 400 and 404 alike,
    # for malformed and well-formed reissue values, and for the reissue
    # REJECT transaction key too.
    reject_tx = {"id": "5001", "rejectReason": "INSUFFICIENT_MARGIN"}
    cases = [
        (400, {"orderRejectTransaction": reject_tx,
               "orderReissueTransaction": "malformed"}),
        (400, {"orderRejectTransaction": reject_tx,
               "orderReissueTransaction": {"id": "6001", "orderID": "1"}}),
        (404, {"orderRejectTransaction": reject_tx,
               "orderReissueTransaction": ["x"]}),
        (400, {"orderRejectTransaction": reject_tx,
               "orderReissueRejectTransaction": {}}),
    ]
    for status, payload in cases:
        adapter, transport = make_adapter((status, payload))
        with pytest.raises(m.BrokerTimeoutError) as exc_info:
            adapter.submit_order(make_request())
        assert "reissue" in str(exc_info.value)
        # the reject reason never wins over the reissue fail-closed
        assert "INSUFFICIENT_MARGIN" not in str(exc_info.value)
        assert len(transport.calls) == 1  # no automatic resend


def test_reject_transaction_alone_still_translates_to_rejected():
    # regression guard for the reordered check: a reject WITHOUT any reissue
    # key keeps its normal REJECTED-result translation
    adapter, _ = make_adapter(
        (400, {"orderRejectTransaction": {"id": "5001",
                                          "rejectReason": "INSUFFICIENT_MARGIN"}})
    )
    result = adapter.submit_order(make_request())
    assert result.state is m.OrderState.REJECTED
    assert result.reject_reason == "INSUFFICIENT_MARGIN"


# --- PR #101 4th review: GET-order taxonomy for invalid broker payloads ------------

def test_get_order_with_zero_units_is_a_communication_error():
    adapter, _ = make_adapter(
        (
            200,
            {
                "order": {
                    "id": "6001",
                    "type": "MARKET",
                    "instrument": "USD_JPY",
                    "units": "0",
                    "state": "PENDING",
                    "clientExtensions": {"id": "c-7"},
                }
            },
        )
    )
    with pytest.raises(m.BrokerCommunicationError):
        adapter.get_order("c-7")


def test_get_order_with_zero_or_negative_limit_price_is_a_communication_error():
    for bad_price in ("0", "-1.5"):
        adapter, _ = make_adapter(
            (
                200,
                {
                    "order": {
                        "id": "6001",
                        "type": "LIMIT",
                        "instrument": "USD_JPY",
                        "units": "1",
                        "price": bad_price,
                        "state": "PENDING",
                        "clientExtensions": {"id": "c-7"},
                    }
                },
            )
        )
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


def test_uncorrelated_reissue_transactions_fail_closed_toward_unknown():
    # Issue #102: reissue responses ARE interpreted now, but only when every
    # correlation requirement holds. A bare reissue object without our
    # clientOrderID cannot be bound to this order: the submit fails toward
    # UNKNOWN (no resend), so the recorded state can never misrepresent a
    # reissued order.
    for extra_key in ("orderReissueTransaction", "orderReissueRejectTransaction"):
        response = fill_response(units="2", price="155.000")
        response[1][extra_key] = {"id": "1003"}
        adapter, transport = make_adapter(response)
        with pytest.raises(m.BrokerTimeoutError) as exc_info:
            adapter.submit_order(make_request(quantity=Decimal("5")))
        assert exc_info.value.code is m.BrokerErrorCode.TIMEOUT
        assert "reissue" in str(exc_info.value)
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
                    "type": "ORDER_FILL",
                    "orderID": "7001",
                    "clientOrderID": "c-8",
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


# --- open trades: client id + SL/TP translation (Issue #105) ------------------

def open_trade_entry(**overrides):
    entry = {
        "id": "T-1",
        "instrument": "USD_JPY",
        "currentUnits": "4",
        "price": "155.200",
        "openTime": "2026-01-02T03:00:00Z",
    }
    entry.update(overrides)
    return entry


def test_get_open_trades_translates_client_id_and_protection_orders():
    adapter, _ = make_adapter(
        (
            200,
            {
                "trades": [
                    open_trade_entry(
                        clientExtensions={"id": "c-1"},
                        stopLossOrder={"id": "SL-1", "price": "150.000", "tradeID": "T-1"},
                        takeProfitOrder={"id": "TP-1", "price": "160.000", "tradeID": "T-9"},
                    )
                ]
            },
        )
    )
    (trade,) = adapter.get_open_trades()
    assert trade.client_order_id == "c-1"
    assert trade.stop_loss == m.ProtectionOrderInfo(
        order_id="SL-1", price=Decimal("150.000"), trade_id="T-1"
    )
    assert trade.take_profit == m.ProtectionOrderInfo(
        order_id="TP-1", price=Decimal("160.000"), trade_id="T-9"
    )


def test_get_open_trades_absent_extensions_and_protections_map_to_none():
    adapter, _ = make_adapter((200, {"trades": [open_trade_entry()]}))
    (trade,) = adapter.get_open_trades()
    assert trade.client_order_id is None
    assert trade.stop_loss is None
    assert trade.take_profit is None


def test_get_open_trades_protection_without_trade_reference_maps_to_none_reference():
    adapter, _ = make_adapter(
        (200, {"trades": [open_trade_entry(stopLossOrder={"id": "SL-1", "price": "1.5"})]})
    )
    (trade,) = adapter.get_open_trades()
    assert trade.stop_loss.trade_id is None


def test_get_open_trades_malformed_protection_orders_fail_closed():
    """A PRESENT but uninterpretable SL/TP object must never degrade to
    "no protection order" -- that would fabricate an SL/TP-missing
    observation for reconciliation."""
    for stop_loss in (
        "not-an-object",
        {},  # no id
        {"id": "SL-1"},  # no price
        {"id": "SL-1", "price": "not-a-number"},
        {"id": "SL-1", "price": "0"},
        {"id": "SL-1", "price": "1.5", "tradeID": "  "},
        {"id": "  ", "price": "1.5"},
    ):
        adapter, _ = make_adapter(
            (200, {"trades": [open_trade_entry(stopLossOrder=stop_loss)]})
        )
        with pytest.raises(m.BrokerCommunicationError):
            adapter.get_open_trades()


def test_get_open_trades_malformed_client_extensions_fail_closed():
    for extensions in ("not-an-object", {"id": "  "}, {"id": 7}):
        adapter, _ = make_adapter(
            (200, {"trades": [open_trade_entry(clientExtensions=extensions)]})
        )
        with pytest.raises(m.BrokerCommunicationError):
            adapter.get_open_trades()
    adapter, _ = make_adapter(
        (200, {"trades": [open_trade_entry(clientExtensions={"tag": "x"})]})
    )
    (trade,) = adapter.get_open_trades()
    assert trade.client_order_id is None  # extensions without an id: no id reported


# --- closed trades (Issue #105) -----------------------------------------------

def closed_trade_entry(**overrides):
    entry = {
        "id": "T-7",
        "instrument": "USD_JPY",
        "initialUnits": "-4",
        "price": "155.200",
        "openTime": "2026-01-02T03:00:00Z",
        "closeTime": "2026-01-02T04:00:00Z",
        "closingTransactionIDs": ["900", "901"],
    }
    entry.update(overrides)
    return entry


def test_get_closed_trades_translates_the_closed_listing():
    adapter, transport = make_adapter((200, {"trades": [closed_trade_entry()]}))
    (closed,) = adapter.get_closed_trades()
    url = transport.calls[0]["url"]
    assert f"/v3/accounts/{ACCOUNT}/trades?state=CLOSED" in url
    assert isinstance(closed, m.ClosedTradeInfo)
    assert closed.trade_id == "T-7"
    assert closed.side is m.PositionSide.SHORT
    assert closed.quantity == Decimal("4")
    assert closed.closed_at_utc == datetime(2026, 1, 2, 4, 0, 0, tzinfo=timezone.utc)
    assert closed.closing_transaction_ids == ("900", "901")
    assert closed.client_order_id is None
    assert closed.retrieved_at_utc == NOW


def test_get_closed_trades_missing_transaction_list_maps_to_empty():
    entry = closed_trade_entry()
    del entry["closingTransactionIDs"]
    adapter, _ = make_adapter((200, {"trades": [entry]}))
    (closed,) = adapter.get_closed_trades()
    assert closed.closing_transaction_ids == ()


def test_get_closed_trades_malformed_responses_are_communication_errors():
    for scripted in (
        (200, {}),
        (200, {"trades": ["junk"]}),
        (200, {"trades": [closed_trade_entry(initialUnits="0")]}),
        (200, {"trades": [closed_trade_entry(closingTransactionIDs="900")]}),
        (200, {"trades": [closed_trade_entry(closingTransactionIDs=["900", "  "])]}),
        (200, {"trades": [closed_trade_entry(closingTransactionIDs=["900", 901])]}),
        (200, {"trades": [closed_trade_entry(closeTime=None)]}),
        (500, {}),
    ):
        adapter, _ = make_adapter(scripted)
        with pytest.raises(m.BrokerCommunicationError):
            adapter.get_closed_trades()


def test_trade_listings_never_leak_the_credentials_in_errors():
    for call in ("get_open_trades", "get_closed_trades"):
        adapter, _ = make_adapter((500, {"errorMessage": f"boom {TOKEN} {ACCOUNT}"}))
        with pytest.raises(m.BrokerCommunicationError) as exc_info:
            getattr(adapter, call)()
        text = str(exc_info.value)
        assert TOKEN not in text
        assert ACCOUNT not in text


def test_adapter_satisfies_the_trade_listing_protocols_but_not_order_listing():
    from broker.broker_interface import (
        ClosedTradeListingBroker,
        OrderListingBroker,
        TradeListingBroker,
    )

    adapter, _ = make_adapter()
    assert isinstance(adapter, TradeListingBroker)
    assert isinstance(adapter, ClosedTradeListingBroker)
    assert not isinstance(adapter, OrderListingBroker)


# --- positions: malformed entries fail closed (Issue #105 review) -------------

def position_entry(**overrides):
    entry = {
        "instrument": "USD_JPY",
        "long": {"units": "7", "averagePrice": "155.100"},
        "short": {"units": "0"},
    }
    entry.update(overrides)
    return entry


def test_get_positions_malformed_side_structures_fail_closed():
    """Reconciliation consumes this listing: a side this adapter cannot
    interpret must reject the WHOLE listing, never degrade to 'flat side' --
    that would fabricate a position-missing observation from broken data."""
    missing_long = position_entry()
    del missing_long["long"]
    missing_units = position_entry(long={"averagePrice": "155.100"})
    for entry in (
        missing_long,                                # side key absent
        position_entry(short="junk"),                # side is not an object
        position_entry(long=None),                   # side is null
        missing_units,                               # units field absent
        position_entry(long={"units": None}),        # units is null
        position_entry(long={"units": 7}),           # units is not a string
        position_entry(long={"units": "not-a-number"}),
    ):
        adapter, _ = make_adapter((200, {"positions": [entry]}))
        with pytest.raises(m.BrokerCommunicationError):
            adapter.get_positions()


def test_get_positions_wrong_sign_units_fail_closed():
    for entry in (
        position_entry(long={"units": "-3", "averagePrice": "155.100"}),
        position_entry(short={"units": "2", "averagePrice": "155.100"}),
    ):
        adapter, _ = make_adapter((200, {"positions": [entry]}))
        with pytest.raises(m.BrokerCommunicationError):
            adapter.get_positions()


def test_get_positions_flat_sides_are_still_no_positions():
    adapter, _ = make_adapter(
        (200, {"positions": [position_entry(long={"units": "0"}, short={"units": "0"})]})
    )
    assert adapter.get_positions() == ()


# --- Issue #102: Order Reissue interpretation --------------------------------
#
# Partial fill + reissue of the remainder under a NEW broker order id. The
# OANDA transaction shapes stay inside the adapter; what crosses the boundary
# is the common model's current broker_order_id + superseded chain. Anything
# the interpretation cannot prove fails closed toward UNKNOWN.


def reissue_submit_response(
    client_order_id="c-1",
    filled_units="2",
    price="155.000",
    reissue_overrides=None,
    drop_reissue_keys=(),
):
    """A 201 create+fill+reissue response (original order 1001 partially
    filled, remainder reissued as order 2001)."""
    status, payload = fill_response(client_order_id, units=filled_units, price=price)
    # the fields an OANDA reissue transaction actually carries (PR #112
    # review): client correlation via clientExtensions.id, the replaced
    # order via replacesOrderID, the transaction id doubling as the NEW
    # order id, and the reissued units
    reissue = {
        "id": "2001",
        "type": "ORDER_REISSUE",
        "replacesOrderID": "1001",
        "clientExtensions": {"id": client_order_id},
        "units": "3",
        "time": "2026-01-02T03:04:05.123456789Z",
    }
    reissue.update(reissue_overrides or {})
    for key in drop_reissue_keys:
        reissue.pop(key, None)
    payload["orderReissueTransaction"] = reissue
    return status, payload


def test_partial_fill_with_successful_reissue_translates_to_the_common_chain():
    adapter, transport = make_adapter(reissue_submit_response())
    result = adapter.submit_order(make_request(quantity=Decimal("5")))
    assert result.state is m.OrderState.PARTIALLY_FILLED
    # the CURRENT actionable id is the reissued order; the original id is
    # auditable in the superseded chain — nothing is overwritten silently
    assert result.broker_order_id == "2001"
    assert result.superseded_broker_order_ids == ("1001",)
    assert result.original_broker_order_id == "1001"
    assert result.all_broker_order_ids == ("1001", "2001")
    assert result.client_order_id == "c-1"
    assert result.filled_quantity == Decimal("2")
    assert result.remaining_quantity == Decimal("3")
    assert result.fills[0].price == Decimal("155.000")
    assert len(transport.calls) == 1  # one attempt, no automatic resend


def test_reissue_reject_translates_to_cancelled_on_the_original_id():
    status, payload = fill_response(units="2", price="155.000")
    payload["orderReissueRejectTransaction"] = {
        "id": "2002",
        "type": "ORDER_REISSUE_REJECT",
        # the reject transaction names the order it ATTEMPTED to replace
        # (intendedReplacesOrderID) — replacesOrderID belongs to the
        # successful OrderReissueTransaction (PR #112 re-review) — and
        # carries the attempted replacement's units (the exact remainder)
        "intendedReplacesOrderID": "1001",
        "clientExtensions": {"id": "c-1"},
        "units": "3",
        "rejectReason": "MARKET_HALTED",
    }
    adapter, transport = make_adapter((status, payload))
    result = adapter.submit_order(make_request(quantity=Decimal("5")))
    # the remainder is definitively dead: terminal CANCELLED, partial fill
    # kept, NO new order id and therefore no chain
    assert result.state is m.OrderState.CANCELLED
    assert result.broker_order_id == "1001"
    assert result.superseded_broker_order_ids == ()
    assert result.filled_quantity == Decimal("2")
    assert result.remaining_quantity == Decimal("3")
    assert len(transport.calls) == 1


def test_reissue_with_uncorrelated_or_inconsistent_fields_fails_closed():
    # every correlation / consistency failure is refused toward UNKNOWN with
    # exactly one attempt — never a guessed PARTIALLY_FILLED / CANCELLED
    cases = (
        # client correlation missing / mismatched on the reissue tx
        {"reissue_overrides": {"clientExtensions": {"id": "someone-else"}}},
        {"reissue_overrides": {"clientExtensions": {}}},
        {"drop_reissue_keys": ("clientExtensions",)},
        # replaced-order correlation: reissue references a different order
        {"reissue_overrides": {"replacesOrderID": "9999"}},
        {"drop_reissue_keys": ("replacesOrderID",)},
        # transaction identity: id (= the NEW order id) missing / equal to
        # the original, type missing / wrong
        {"drop_reissue_keys": ("id",)},
        {"reissue_overrides": {"id": "1001"}},
        {"drop_reissue_keys": ("type",)},
        {"reissue_overrides": {"type": "ORDER_FILL"}},
        # legacy/wrong correlation fields alone must not satisfy the schema
        # (fill-transaction style clientOrderID/orderID are NOT the reissue
        # transaction's fields — PR #112 review)
        {"reissue_overrides": {"clientOrderID": "c-1", "orderID": "1001"},
         "drop_reissue_keys": ("clientExtensions", "replacesOrderID")},
        # the REJECT transaction's intendedReplacesOrderID must not satisfy
        # the success branch (PR #112 re-review)
        {"reissue_overrides": {"intendedReplacesOrderID": "1001"},
         "drop_reissue_keys": ("replacesOrderID",)},
        # quantity consistency: reissued units must equal the exact remainder
        {"reissue_overrides": {"units": "4"}},
        {"drop_reissue_keys": ("units",)},
        {"reissue_overrides": {"units": "garbage"}},
    )
    for case in cases:
        adapter, transport = make_adapter(reissue_submit_response(**case))
        with pytest.raises(m.BrokerTimeoutError) as exc_info:
            adapter.submit_order(make_request(quantity=Decimal("5")))
        assert "reissue" in str(exc_info.value)
        assert len(transport.calls) == 1


def test_reissue_without_a_partial_fill_transaction_fails_closed():
    # a reissue exists only because a partial fill happened; a reissue key
    # without an interpretable partial fill cannot be interpreted
    status, payload = reissue_submit_response()
    del payload["orderFillTransaction"]
    adapter, _ = make_adapter((status, payload))
    with pytest.raises(m.BrokerTimeoutError) as exc_info:
        adapter.submit_order(make_request(quantity=Decimal("5")))
    assert "reissue" in str(exc_info.value)


def test_reissue_with_a_full_fill_is_inconsistent_and_fails_closed():
    adapter, _ = make_adapter(reissue_submit_response(filled_units="5"))
    with pytest.raises(m.BrokerTimeoutError) as exc_info:
        adapter.submit_order(make_request(quantity=Decimal("5")))
    assert "reissue" in str(exc_info.value)


def test_reissue_with_an_uncorrelated_fill_transaction_fails_closed():
    status, payload = reissue_submit_response()
    payload["orderFillTransaction"]["clientOrderID"] = "someone-else"
    adapter, _ = make_adapter((status, payload))
    with pytest.raises(m.BrokerTimeoutError) as exc_info:
        adapter.submit_order(make_request(quantity=Decimal("5")))
    assert "reissue" in str(exc_info.value)


def test_reissue_and_reissue_reject_together_are_contradictory():
    status, payload = reissue_submit_response()
    payload["orderReissueRejectTransaction"] = {
        "id": "2002", "orderID": "1001", "clientOrderID": "c-1",
    }
    adapter, _ = make_adapter((status, payload))
    with pytest.raises(m.BrokerTimeoutError) as exc_info:
        adapter.submit_order(make_request(quantity=Decimal("5")))
    assert "reissue" in str(exc_info.value)


def test_reissue_coexisting_with_cancel_or_reject_transactions_fails_closed():
    # Issue #102: a reissue sharing the body with a cancel / reject
    # transaction is ambiguous — never confirmed as any definite state
    for extra_key, extra in (
        ("orderCancelTransaction", {"id": "3001", "orderID": "2001", "clientOrderID": "c-1"}),
        ("orderRejectTransaction", {"id": "3001", "rejectReason": "X"}),
    ):
        status, payload = reissue_submit_response()
        payload[extra_key] = extra
        adapter, _ = make_adapter((status, payload))
        with pytest.raises(m.BrokerTimeoutError) as exc_info:
            adapter.submit_order(make_request(quantity=Decimal("5")))
        assert "reissue" in str(exc_info.value)


def test_reissue_reject_with_broken_correlation_or_schema_fails_closed():
    # PR #112 review: a transaction that may confirm a terminal CANCELLED
    # must prove its identity (id + type) and its correlation
    # (clientExtensions.id + replacesOrderID); anything less is refused
    # toward UNKNOWN, never confirmed.
    complete = {
        "id": "2002",
        "type": "ORDER_REISSUE_REJECT",
        "intendedReplacesOrderID": "1001",
        "clientExtensions": {"id": "c-1"},
        "units": "3",
    }
    cases = (
        {**complete, "clientExtensions": {"id": "someone-else"}},
        {**complete, "clientExtensions": {}},
        {key: value for key, value in complete.items() if key != "clientExtensions"},
        {**complete, "intendedReplacesOrderID": "9999"},
        {key: value for key, value in complete.items()
         if key != "intendedReplacesOrderID"},
        # the SUCCESS transaction's replacesOrderID must not satisfy the
        # reject branch (the reject schema names the attempted replacement
        # as intendedReplacesOrderID — PR #112 re-review)
        {**{key: value for key, value in complete.items()
            if key != "intendedReplacesOrderID"},
         "replacesOrderID": "1001"},
        # missing transaction id / missing or wrong type
        {key: value for key, value in complete.items() if key != "id"},
        {**complete, "id": ""},
        {key: value for key, value in complete.items() if key != "type"},
        {**complete, "type": "ORDER_CANCEL"},
        # units of the attempted replacement: missing / inconsistent /
        # malformed / opposite-signed (PR #112 re-review r3)
        {key: value for key, value in complete.items() if key != "units"},
        {**complete, "units": "4"},
        {**complete, "units": "garbage"},
        {**complete, "units": "-3"},
        # the Codex example: two loosely matching fields alone must never
        # confirm a terminal CANCELLED
        {"orderID": "1001", "clientOrderID": "c-1"},
    )
    for reject_tx in cases:
        status, payload = fill_response(units="2", price="155.000")
        payload["orderReissueRejectTransaction"] = reject_tx
        adapter, transport = make_adapter((status, payload))
        with pytest.raises(m.BrokerTimeoutError) as exc_info:
            adapter.submit_order(make_request(quantity=Decimal("5")))
        assert "reissue" in str(exc_info.value)
        assert len(transport.calls) == 1


# --- Issue #102: get_order resolves the reissue chain ------------------------


def chain_order(
    order_id,
    *,
    state,
    units,
    client_order_id="c-9",
    replaces=None,
    replaced_by=None,
    filling_tx=None,
    order_type="LIMIT",
    price="150.000",
    client_extensions=True,
):
    order = {
        "id": order_id,
        "type": order_type,
        "instrument": "USD_JPY",
        "units": units,
        "state": state,
    }
    if order_type == "LIMIT":
        order["price"] = price
    if client_extensions:
        order["clientExtensions"] = {"id": client_order_id}
    if replaces is not None:
        order["replacesOrderID"] = replaces
    if replaced_by is not None:
        order["replacedByOrderID"] = replaced_by
    if filling_tx is not None:
        order["fillingTransactionID"] = filling_tx
    return order


def fill_tx_response(tx_id, units, *, order_id, client_order_id="c-9",
                     price="150.000", time="2026-01-02T03:04:07.000000000Z",
                     tx_overrides=None):
    tx = {
        "id": tx_id,
        "type": "ORDER_FILL",
        "orderID": order_id,
        "clientOrderID": client_order_id,
        "units": units,
        "price": price,
        "time": time,
    }
    tx.update(tx_overrides or {})
    return (200, {"transaction": tx})


def test_get_order_after_reissue_rebuilds_the_cumulative_snapshot():
    # original order 1001 (5 units) partially filled (2) and reissued as
    # 2001 (3 units, still working): the snapshot must describe the WHOLE
    # request — requested 5, filled 2, remaining 3 — under the current id
    adapter, transport = make_adapter(
        (200, {"order": chain_order("2001", state="PENDING", units="3", replaces="1001")}),
        (200, {"order": chain_order(
            "1001", state="REISSUED", units="5", replaced_by="2001", filling_tx="9001"
        )}),
        fill_tx_response("9001", "2", order_id="1001"),
    )
    result = adapter.get_order("c-9")
    assert transport.calls[0]["url"].endswith(f"/v3/accounts/{ACCOUNT}/orders/@c-9")
    assert transport.calls[1]["url"].endswith(f"/v3/accounts/{ACCOUNT}/orders/1001")
    assert transport.calls[2]["url"].endswith(f"/v3/accounts/{ACCOUNT}/transactions/9001")
    assert result.state is m.OrderState.PARTIALLY_FILLED
    assert result.broker_order_id == "2001"
    assert result.superseded_broker_order_ids == ("1001",)
    assert result.requested_quantity == Decimal("5")
    assert result.filled_quantity == Decimal("2")
    assert result.remaining_quantity == Decimal("3")
    assert result.requested_price == Decimal("150.000")


def test_get_order_after_reissue_final_fill_is_filled_with_all_fills():
    adapter, _ = make_adapter(
        (200, {"order": chain_order(
            "2001", state="FILLED", units="3", replaces="1001", filling_tx="9002"
        )}),
        (200, {"order": chain_order(
            "1001", state="REISSUED", units="5", replaced_by="2001", filling_tx="9001"
        )}),
        fill_tx_response("9001", "2", order_id="1001"),
        fill_tx_response("9002", "3", order_id="2001", price="150.000"),
    )
    result = adapter.get_order("c-9")
    assert result.state is m.OrderState.FILLED
    assert result.broker_order_id == "2001"
    assert result.superseded_broker_order_ids == ("1001",)
    assert result.filled_quantity == Decimal("5")
    assert [f.quantity for f in result.fills] == [Decimal("2"), Decimal("3")]


def test_get_order_after_reissue_cancel_keeps_the_partial_fills():
    adapter, _ = make_adapter(
        (200, {"order": chain_order("2001", state="CANCELLED", units="3", replaces="1001")}),
        (200, {"order": chain_order(
            "1001", state="REISSUED", units="5", replaced_by="2001", filling_tx="9001"
        )}),
        fill_tx_response("9001", "2", order_id="1001"),
    )
    result = adapter.get_order("c-9")
    assert result.state is m.OrderState.CANCELLED
    assert result.broker_order_id == "2001"
    assert result.superseded_broker_order_ids == ("1001",)
    assert result.filled_quantity == Decimal("2")
    assert result.remaining_quantity == Decimal("3")


def test_get_order_resolves_a_repeated_reissue_chain_oldest_first():
    adapter, _ = make_adapter(
        (200, {"order": chain_order("3001", state="PENDING", units="2", replaces="2001")}),
        (200, {"order": chain_order(
            "2001", state="REISSUED", units="3", replaces="1001",
            replaced_by="3001", filling_tx="9002",
        )}),
        (200, {"order": chain_order(
            "1001", state="REISSUED", units="5", replaced_by="2001", filling_tx="9001"
        )}),
        fill_tx_response("9001", "2", order_id="1001"),
        fill_tx_response("9002", "1", order_id="2001"),
    )
    result = adapter.get_order("c-9")
    assert result.state is m.OrderState.PARTIALLY_FILLED
    assert result.broker_order_id == "3001"
    assert result.superseded_broker_order_ids == ("1001", "2001")
    assert result.requested_quantity == Decimal("5")
    assert result.filled_quantity == Decimal("3")
    assert result.remaining_quantity == Decimal("2")


def test_get_order_reissue_chain_correlation_failures_fail_closed():
    # every unprovable link refuses the whole snapshot (never a partial
    # guess): back-reference mismatch, wrong state, foreign client id,
    # missing client extensions, id mismatch against the reference
    predecessors = (
        chain_order("1001", state="REISSUED", units="5", replaced_by="9999",
                    filling_tx="9001"),
        chain_order("1001", state="FILLED", units="5", replaced_by="2001",
                    filling_tx="9001"),
        chain_order("1001", state="REISSUED", units="5", replaced_by="2001",
                    filling_tx="9001", client_order_id="someone-else"),
        chain_order("1001", state="REISSUED", units="5", replaced_by="2001",
                    filling_tx="9001", client_extensions=False),
        chain_order("1002", state="REISSUED", units="5", replaced_by="2001",
                    filling_tx="9001"),
    )
    for predecessor in predecessors:
        adapter, _ = make_adapter(
            (200, {"order": chain_order("2001", state="PENDING", units="3",
                                        replaces="1001")}),
            (200, {"order": predecessor}),
        )
        with pytest.raises(m.BrokerCommunicationError):
            adapter.get_order("c-9")


def test_get_order_reissue_chain_quantity_inconsistency_fails_closed():
    # 5 units reissued away with a 2-unit fill must leave exactly 3: a
    # 4-unit successor cannot belong to the same request
    adapter, _ = make_adapter(
        (200, {"order": chain_order("2001", state="PENDING", units="4", replaces="1001")}),
        (200, {"order": chain_order(
            "1001", state="REISSUED", units="5", replaced_by="2001", filling_tx="9001"
        )}),
        fill_tx_response("9001", "2", order_id="1001"),
    )
    with pytest.raises(m.BrokerCommunicationError) as exc_info:
        adapter.get_order("c-9")
    assert "quantity" in str(exc_info.value)


def test_get_order_top_level_reissued_state_is_inconsistent():
    # the @client specifier must resolve to the CURRENT order of the chain
    adapter, _ = make_adapter(
        (200, {"order": chain_order("1001", state="REISSUED", units="5",
                                    replaced_by="2001", filling_tx="9001")}),
    )
    with pytest.raises(m.BrokerCommunicationError) as exc_info:
        adapter.get_order("c-9")
    assert "reissued-away" in str(exc_info.value)


def test_get_order_malformed_replaces_order_id_is_refused_before_any_fetch():
    # a broker-supplied order id is embedded into a request path, so it must
    # stay inside the URL-safe alphabet — no fetch may happen otherwise
    for bad in ("", "  ", "../evil", "a/b", "@x", "x" * 129):
        adapter, transport = make_adapter(
            (200, {"order": chain_order("2001", state="PENDING", units="3",
                                        replaces=bad)}),
        )
        with pytest.raises(m.BrokerCommunicationError):
            adapter.get_order("c-9")
        assert len(transport.calls) == 1  # only the initial @client lookup


def test_get_order_reissue_chain_depth_is_bounded():
    # a reference cycle (or a runaway chain) must terminate in a refusal,
    # never an unbounded fetch loop
    responses = [
        (200, {"order": chain_order("2001", state="PENDING", units="3", replaces="1001")}),
    ]
    # 1001 <-> 2001 reference cycle: every fetched predecessor points back
    # into the chain, so only the depth bound can stop the walk
    for _ in range(oa._MAX_REISSUE_CHAIN + 1):
        responses.append(
            (200, {"order": chain_order(
                "1001", state="REISSUED", units="5", replaces="2001",
                replaced_by="2001", filling_tx="9001",
            )}),
        )
        responses.append(
            (200, {"order": chain_order(
                "2001", state="REISSUED", units="5", replaces="1001",
                replaced_by="1001", filling_tx="9002",
            )}),
        )
    adapter, transport = make_adapter(*responses)
    with pytest.raises(m.BrokerCommunicationError) as exc_info:
        adapter.get_order("c-9")
    assert "depth" in str(exc_info.value)
    assert len(transport.calls) <= oa._MAX_REISSUE_CHAIN + 1


def test_get_order_reissue_chain_lookup_error_fails_closed():
    for chain_response in ((404, {}), (200, {"order": "not-an-object"}), (200, {})):
        adapter, _ = make_adapter(
            (200, {"order": chain_order("2001", state="PENDING", units="3",
                                        replaces="1001")}),
            chain_response,
        )
        with pytest.raises(m.BrokerCommunicationError):
            adapter.get_order("c-9")


def test_submit_reissue_then_get_order_satisfies_the_update_contract():
    # the lifecycle the execution store will drive: the submit snapshot and
    # a later query snapshot must chain through validate_order_result_update
    adapter, _ = make_adapter(
        reissue_submit_response(client_order_id="c-9"),
        (200, {"order": chain_order(
            "2001", state="FILLED", units="3", replaces="1001",
            order_type="MARKET", filling_tx="9002",
        )}),
        (200, {"order": chain_order(
            "1001", state="REISSUED", units="5", replaced_by="2001",
            order_type="MARKET", filling_tx="1002",
        )}),
        # the SAME fill the submit already observed must come back unchanged
        # (fills are an append-only prefix across snapshots)
        fill_tx_response("1002", "2", order_id="1001", price="155.000",
                         time="2026-01-02T03:04:05.123456789Z"),
        fill_tx_response("9002", "3", order_id="2001", price="155.500"),
    )
    submitted = adapter.submit_order(
        make_request(client_order_id="c-9", quantity=Decimal("5"))
    )
    queried = adapter.get_order("c-9")
    m.validate_order_result_update(submitted, queried)
    assert queried.state is m.OrderState.FILLED
    assert queried.superseded_broker_order_ids == ("1001",)
    assert queried.broker_order_id == "2001"


def test_chain_fill_transactions_must_correlate_to_their_chain_member():
    # PR #112 review: a predecessor referencing a transaction that answers
    # under a different id, is not an ORDER_FILL, filled a DIFFERENT order,
    # or carries a foreign client correlation must never contribute a fill
    # to this order's audit snapshot — even when the quantities happen to
    # satisfy the chain arithmetic.
    cases = (
        {"orderID": "9999"},                # fill of a different order
        {"orderID": None},                  # no order reference at all
        {"clientOrderID": "someone-else"},  # foreign client correlation
        {"clientOrderID": None},
        {"type": "MARKET_ORDER"},           # not an ORDER_FILL transaction
        {"type": None},
        {"id": "8888"},                     # answers under a different tx id
    )
    for overrides in cases:
        tx_overrides = {k: v for k, v in overrides.items() if v is not None}
        drop = [k for k, v in overrides.items() if v is None]
        response = fill_tx_response("9001", "2", order_id="1001",
                                    tx_overrides=tx_overrides)
        for key in drop:
            del response[1]["transaction"][key]
        adapter, _ = make_adapter(
            (200, {"order": chain_order("2001", state="PENDING", units="3",
                                        replaces="1001")}),
            (200, {"order": chain_order(
                "1001", state="REISSUED", units="5", replaced_by="2001",
                filling_tx="9001"
            )}),
            response,
        )
        with pytest.raises(m.BrokerCommunicationError):
            adapter.get_order("c-9")


def test_filled_order_query_refuses_a_foreign_filling_transaction():
    # the same correlation contract guards the ordinary (chain-less) FILLED
    # query path: a transaction filled for another order / client is refused
    for tx_overrides in (
        {"orderID": "6000"},
        {"clientOrderID": "someone-else"},
        {"type": "TRADE_CLOSE"},
    ):
        adapter, _ = make_adapter(
            (200, {"order": {
                "id": "7001", "type": "MARKET", "instrument": "USD_JPY",
                "units": "5", "state": "FILLED", "fillingTransactionID": "7002",
                "clientExtensions": {"id": "c-8"},
            }}),
            fill_tx_response("7002", "5", order_id="7001",
                             client_order_id="c-8", tx_overrides=tx_overrides),
        )
        with pytest.raises(m.BrokerCommunicationError):
            adapter.get_order("c-8")


def test_reissue_path_fill_transaction_must_be_an_order_fill():
    # PR #112 re-review: a mapping under orderFillTransaction with matching
    # ids but a missing or foreign type must not drive a reissue result —
    # neither the successful branch (audit chain) nor the reject branch
    # (terminal CANCELLED).
    for tx_type in (None, "MARKET_ORDER", "TRADE_CLOSE"):
        # successful-reissue branch
        status, payload = reissue_submit_response()
        if tx_type is None:
            del payload["orderFillTransaction"]["type"]
        else:
            payload["orderFillTransaction"]["type"] = tx_type
        adapter, transport = make_adapter((status, payload))
        with pytest.raises(m.BrokerTimeoutError) as exc_info:
            adapter.submit_order(make_request(quantity=Decimal("5")))
        assert "reissue" in str(exc_info.value)
        assert len(transport.calls) == 1

        # reissue-reject branch
        status, payload = fill_response(units="2", price="155.000")
        if tx_type is None:
            del payload["orderFillTransaction"]["type"]
        else:
            payload["orderFillTransaction"]["type"] = tx_type
        payload["orderReissueRejectTransaction"] = {
            "id": "2002",
            "type": "ORDER_REISSUE_REJECT",
            "intendedReplacesOrderID": "1001",
            "clientExtensions": {"id": "c-1"},
            "units": "3",
        }
        adapter, transport = make_adapter((status, payload))
        with pytest.raises(m.BrokerTimeoutError) as exc_info:
            adapter.submit_order(make_request(quantity=Decimal("5")))
        assert "reissue" in str(exc_info.value)
        assert len(transport.calls) == 1


def test_reissue_quantities_must_be_signed_on_the_requests_side():
    # PR #112 re-review r3: a BUY request answered with SELL-signed fill /
    # reissue units is contradictory and must never be normalized away by
    # abs() — neither branch may produce a result from it.
    for overrides in (
        {"fill_units": "-2"},                              # fill on the wrong side
        {"reissue_overrides": {"units": "-3"}},            # reissue on the wrong side
        {"fill_units": "-2", "reissue_overrides": {"units": "-3"}},  # both flipped
    ):
        status, payload = reissue_submit_response(
            reissue_overrides=overrides.get("reissue_overrides"),
        )
        if "fill_units" in overrides:
            payload["orderFillTransaction"]["units"] = overrides["fill_units"]
        adapter, transport = make_adapter((status, payload))
        with pytest.raises(m.BrokerTimeoutError) as exc_info:
            adapter.submit_order(make_request(quantity=Decimal("5")))
        assert "reissue" in str(exc_info.value)
        assert len(transport.calls) == 1

    # the reject branch refuses an opposite-side fill the same way
    status, payload = fill_response(units="-2", price="155.000")
    payload["orderReissueRejectTransaction"] = {
        "id": "2002",
        "type": "ORDER_REISSUE_REJECT",
        "intendedReplacesOrderID": "1001",
        "clientExtensions": {"id": "c-1"},
        "units": "3",
    }
    adapter, _ = make_adapter((status, payload))
    with pytest.raises(m.BrokerTimeoutError) as exc_info:
        adapter.submit_order(make_request(quantity=Decimal("5")))
    assert "reissue" in str(exc_info.value)


def test_sell_reissue_with_consistently_negative_units_is_interpreted():
    # SELL requests carry negative units throughout — the sign validation
    # must accept the consistent case, not just refuse the flipped one
    status, payload = reissue_submit_response(
        filled_units="-2", reissue_overrides={"units": "-3"}
    )
    adapter, _ = make_adapter((status, payload))
    result = adapter.submit_order(
        make_request(side=m.OrderSide.SELL, quantity=Decimal("5"))
    )
    assert result.state is m.OrderState.PARTIALLY_FILLED
    assert result.side is m.OrderSide.SELL
    assert result.broker_order_id == "2001"
    assert result.superseded_broker_order_ids == ("1001",)
    assert result.filled_quantity == Decimal("2")
    assert result.remaining_quantity == Decimal("3")


def test_chain_fill_with_opposite_signed_units_is_refused_despite_arithmetic():
    # PR #112 re-review r4: a BUY chain (positive units throughout) whose
    # predecessor references a SELL-signed fill transaction satisfies the
    # 5 == |-2| + 3 arithmetic after abs() — the sign must be validated
    # against the order's side BEFORE normalization, so the contradictory
    # response fails closed instead of entering the audit snapshot.
    adapter, _ = make_adapter(
        (200, {"order": chain_order("2001", state="PENDING", units="3",
                                    replaces="1001")}),
        (200, {"order": chain_order(
            "1001", state="REISSUED", units="5", replaced_by="2001",
            filling_tx="9001"
        )}),
        fill_tx_response("9001", "-2", order_id="1001"),
    )
    with pytest.raises(m.BrokerCommunicationError) as exc_info:
        adapter.get_order("c-9")
    assert "opposite side" in str(exc_info.value)


def test_final_fill_with_opposite_signed_units_is_refused():
    # the current (final-fill) order's transaction is validated the same way
    adapter, _ = make_adapter(
        (200, {"order": chain_order(
            "2001", state="FILLED", units="3", replaces="1001",
            filling_tx="9002"
        )}),
        (200, {"order": chain_order(
            "1001", state="REISSUED", units="5", replaced_by="2001",
            filling_tx="9001"
        )}),
        fill_tx_response("9001", "2", order_id="1001"),
        fill_tx_response("9002", "-3", order_id="2001"),
    )
    with pytest.raises(m.BrokerCommunicationError) as exc_info:
        adapter.get_order("c-9")
    assert "opposite side" in str(exc_info.value)


def test_plain_filled_query_refuses_an_opposite_signed_filling_transaction():
    # the chain-less FILLED query path shares the sign contract: a SELL
    # order whose filling transaction reports BUY-signed units is refused
    adapter, _ = make_adapter(
        (200, {"order": {
            "id": "7001", "type": "MARKET", "instrument": "USD_JPY",
            "units": "-5", "state": "FILLED", "fillingTransactionID": "7002",
            "clientExtensions": {"id": "c-8"},
        }}),
        fill_tx_response("7002", "5", order_id="7001", client_order_id="c-8"),
    )
    with pytest.raises(m.BrokerCommunicationError) as exc_info:
        adapter.get_order("c-8")
    assert "opposite side" in str(exc_info.value)
