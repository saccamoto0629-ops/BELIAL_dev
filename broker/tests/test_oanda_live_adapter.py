"""Issue #82: the OANDA Live Adapter is live-only, locked and offline here.

Every test in this file runs with an INJECTED transport (or none at all):
no test may reach a real endpoint, no test needs a real credential, and the
default ``requests`` transport is never invoked. The suite pins:

- the live endpoint allowlist (both directions: a demo endpoint under a
  live config is refused, and there is NO default live endpoint);
- the activation grant (``ENABLE_LIVE_TRADING=true`` + ``--confirm-live``);
- demo / live credential separation;
- that the token, the account id and the endpoint never appear in a
  ``repr`` or in any rejection message;
- that the shared v20 translation body is reused unchanged.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from broker import broker_models as m
from broker import oanda_demo_adapter as demo
from broker import oanda_live_adapter as live
from broker.broker_interface import Broker

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
TOKEN = "live-token-SECRET"
ACCOUNT = "101-004-LIVEACCOUNT-001"


def live_env(**overrides):
    env = {
        live.ENV_ENABLE_LIVE_TRADING: live.ENABLE_LIVE_TRADING_TRUE,
        live.ENV_LIVE_API_TOKEN: TOKEN,
        live.ENV_LIVE_ACCOUNT_ID: ACCOUNT,
        live.ENV_LIVE_API_URL: live.OANDA_LIVE_API_URL,
    }
    env.update(overrides)
    return {name: value for name, value in env.items() if value is not None}


def make_config(**overrides):
    values = {
        "api_token": TOKEN,
        "account_id": ACCOUNT,
        "endpoint": live.OANDA_LIVE_API_URL,
    }
    values.update(overrides)
    return live.OandaLiveConfig(**values)


def make_activation():
    return live.LiveActivation.from_environment(
        {live.ENV_ENABLE_LIVE_TRADING: live.ENABLE_LIVE_TRADING_TRUE}, confirm_live=True
    )


class FakeTransport:
    """Scripted (status, body) responses; records every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(
            {"method": method, "url": url, "headers": dict(headers), "body": body}
        )
        if not self.responses:
            raise AssertionError("unexpected extra HTTP call")
        status, payload = self.responses.pop(0)
        return status, json.dumps(payload) if isinstance(payload, dict) else payload


def make_adapter(*responses, config=None, activation=None):
    transport = FakeTransport(*responses)
    adapter = live.OandaLiveAdapter(
        config or make_config(),
        activation or make_activation(),
        transport=transport,
        clock=lambda: NOW,
    )
    return adapter, transport


# --- lock 2 + 3: the activation grant -----------------------------------------


def test_activation_requires_both_the_env_switch_and_the_cli_confirmation():
    assert make_activation().confirm_live is True


@pytest.mark.parametrize(
    ("env_value", "confirm_live", "expected"),
    [
        pytest.param("true", False, "--confirm-live", id="no-confirm-live"),
        pytest.param(None, True, "ENABLE_LIVE_TRADING", id="flag-unset"),
        pytest.param("false", True, "ENABLE_LIVE_TRADING", id="flag-false"),
        pytest.param("TRUE", True, "ENABLE_LIVE_TRADING", id="flag-wrong-case"),
        pytest.param("1", True, "ENABLE_LIVE_TRADING", id="flag-numeric"),
        pytest.param("yes", True, "ENABLE_LIVE_TRADING", id="flag-yes"),
        pytest.param(" true ", True, "ENABLE_LIVE_TRADING", id="flag-padded"),
    ],
)
def test_activation_is_refused_unless_both_locks_are_exact(env_value, confirm_live, expected):
    env = {} if env_value is None else {live.ENV_ENABLE_LIVE_TRADING: env_value}
    with pytest.raises(live.LiveActivationError, match=expected):
        live.LiveActivation.from_environment(env, confirm_live=confirm_live)


@pytest.mark.parametrize("truthy", [1, "true", object()])
def test_confirm_live_must_be_the_boolean_true_not_merely_truthy(truthy):
    with pytest.raises(live.LiveActivationError, match="--confirm-live"):
        live.LiveActivation.from_environment(
            {live.ENV_ENABLE_LIVE_TRADING: "true"}, confirm_live=truthy
        )


def test_adapter_refuses_to_exist_without_a_grant():
    for activation in (None, True, "confirmed", object()):
        with pytest.raises(live.LiveActivationError, match="LiveActivation grant"):
            live.OandaLiveAdapter(make_config(), activation, transport=FakeTransport())


# --- lock 4 + 5: credentials and the endpoint allowlist ------------------------


def test_live_config_accepts_only_the_live_endpoint():
    assert make_config().endpoint == live.OANDA_LIVE_API_URL
    assert make_config(endpoint=live.OANDA_LIVE_API_URL + "/").endpoint == live.OANDA_LIVE_API_URL


@pytest.mark.parametrize(
    "endpoint",
    [
        pytest.param(demo.OANDA_DEMO_API_URL, id="demo-endpoint"),
        pytest.param("https://api-fxpractice.oanda.com/", id="demo-endpoint-slash"),
        pytest.param("https://api-fxtrade.oanda.com.evil.example", id="lookalike-host"),
        pytest.param("http://api-fxtrade.oanda.com", id="plain-http"),
        pytest.param("https://localhost:9999", id="unknown-host"),
        pytest.param("", id="empty"),
        pytest.param(None, id="none"),
    ],
)
def test_live_config_refuses_every_other_endpoint(endpoint):
    with pytest.raises(live.LiveActivationError):
        make_config(endpoint=endpoint)


def test_endpoint_rejection_never_echoes_the_configured_value():
    secret_url = "https://api-fxtrade.oanda.com.attacker.example/leak?token=abc"
    with pytest.raises(live.LiveActivationError) as exc_info:
        make_config(endpoint=secret_url)
    message = str(exc_info.value)
    assert secret_url not in message
    assert "attacker" not in message and "abc" not in message


@pytest.mark.parametrize("missing", [live.ENV_LIVE_API_TOKEN, live.ENV_LIVE_ACCOUNT_ID])
def test_from_env_requires_every_live_credential(missing):
    env = live_env()
    del env[missing]
    with pytest.raises(live.LiveActivationError, match=missing):
        live.OandaLiveConfig.from_env(env)


def test_from_env_requires_an_explicit_live_endpoint():
    """The demo endpoint has a default; the LIVE endpoint deliberately does
    not -- "not configured" must fail closed, never resolve to a live host."""
    env = live_env()
    del env[live.ENV_LIVE_API_URL]
    with pytest.raises(live.LiveActivationError, match=live.ENV_LIVE_API_URL):
        live.OandaLiveConfig.from_env(env)


def test_from_env_reads_live_only_variables():
    """Demo variables are NOT a fallback: an environment that only carries
    demo credentials cannot produce a live config."""
    env = {
        "OANDA_API_TOKEN": "demo-token",
        "OANDA_ACCOUNT_ID": "demo-account",
        "OANDA_API_URL": demo.OANDA_DEMO_API_URL,
        live.ENV_ENABLE_LIVE_TRADING: "true",
    }
    with pytest.raises(live.LiveActivationError, match=live.ENV_LIVE_API_TOKEN):
        live.OandaLiveConfig.from_env(env)


@pytest.mark.parametrize(
    ("demo_name", "live_name", "shared"),
    [
        ("OANDA_API_TOKEN", live.ENV_LIVE_API_TOKEN, TOKEN),
        ("OANDA_ACCOUNT_ID", live.ENV_LIVE_ACCOUNT_ID, ACCOUNT),
    ],
)
def test_shared_demo_and_live_credentials_are_refused(demo_name, live_name, shared):
    env = live_env()
    env[demo_name] = shared
    with pytest.raises(live.LiveActivationError) as exc_info:
        live.OandaLiveConfig.from_env(env)
    message = str(exc_info.value)
    assert demo_name in message and live_name in message
    assert shared not in message  # names only, never the shared value


def test_demo_config_still_refuses_the_live_endpoint():
    """The Issue #82 split must not weaken the demo contract."""
    with pytest.raises(ValueError, match="demo-only"):
        demo.OandaDemoConfig(
            api_token="t", account_id="a", endpoint=live.OANDA_LIVE_API_URL
        )


# --- secrets ------------------------------------------------------------------


def test_config_repr_hides_token_account_and_endpoint():
    rendered = repr(make_config())
    for secret in (TOKEN, ACCOUNT, live.OANDA_LIVE_API_URL):
        assert secret not in rendered


def test_activation_repr_holds_no_environment_value():
    assert "true" not in repr(make_activation())


def test_broker_error_text_scrubs_the_live_secrets():
    """The shared engine's redaction applies to the live adapter unchanged:
    a broker that echoes our token / account / endpoint back cannot get them
    into an exception or a reject_reason."""
    adapter, _ = make_adapter(
        (400, {"errorMessage": f"bad request for {ACCOUNT} at {live.OANDA_LIVE_API_URL} with {TOKEN}"})
    )
    with pytest.raises(m.BrokerInvalidRequestError) as exc_info:
        adapter.submit_order(
            m.OrderRequest(
                client_order_id="c-1",
                symbol="USD_JPY",
                side=m.OrderSide.BUY,
                order_type=m.OrderType.MARKET,
                quantity=Decimal("1"),
                created_at_utc=NOW,
            )
        )
    message = str(exc_info.value)
    for secret in (TOKEN, ACCOUNT, live.OANDA_LIVE_API_URL):
        assert secret not in message


# --- identity and the reused translation body ----------------------------------


def test_connection_identity_is_the_constant_live_label():
    adapter, _ = make_adapter()
    identity = adapter.connection_identity()
    assert identity.mode == "live"
    assert identity.target == live.OANDA_LIVE_CONNECTION_TARGET
    assert live.OANDA_LIVE_API_URL not in identity.target
    assert ACCOUNT not in identity.target


def test_the_live_adapter_satisfies_the_broker_protocol():
    adapter, _ = make_adapter()
    assert isinstance(adapter, Broker)


def test_the_live_adapter_is_not_a_demo_adapter_and_vice_versa():
    """Neither class is a subclass of the other, so the Safety Gate's
    concrete-type cross-check can bind each mode to exactly one type."""
    assert not issubclass(live.OandaLiveAdapter, demo.OandaDemoAdapter)
    assert not issubclass(demo.OandaDemoAdapter, live.OandaLiveAdapter)


def test_submit_uses_the_shared_v20_translation_against_the_live_host():
    adapter, transport = make_adapter(
        (
            201,
            {
                "orderCreateTransaction": {
                    "id": "1001",
                    "time": "2026-01-02T03:04:05.123456789Z",
                    "clientExtensions": {"id": "c-1"},
                },
                "orderFillTransaction": {
                    "id": "1002",
                    "type": "ORDER_FILL",
                    "orderID": "1001",
                    "clientOrderID": "c-1",
                    "units": "1",
                    "price": "155.123",
                    "time": "2026-01-02T03:04:05.123456789Z",
                },
            },
        )
    )
    result = adapter.submit_order(
        m.OrderRequest(
            client_order_id="c-1",
            symbol="USD_JPY",
            side=m.OrderSide.BUY,
            order_type=m.OrderType.MARKET,
            quantity=Decimal("1"),
            created_at_utc=NOW,
        )
    )
    assert result.state is m.OrderState.FILLED
    call = transport.calls[0]
    assert call["url"] == f"{live.OANDA_LIVE_API_URL}/v3/accounts/{ACCOUNT}/orders"
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_transport_failures_stay_unknown_and_are_never_resent():
    """The "one attempt, UNKNOWN outcome" contract is inherited unchanged --
    the live path must not grow a retry."""
    from broker.oanda_v20_adapter import TransportTimeoutError

    class TimingOutTransport:
        def __init__(self):
            self.calls = 0

        def __call__(self, *args):
            self.calls += 1
            raise TransportTimeoutError("timed out")

    transport = TimingOutTransport()
    adapter = live.OandaLiveAdapter(
        make_config(), make_activation(), transport=transport, clock=lambda: NOW
    )
    with pytest.raises(m.BrokerTimeoutError) as exc_info:
        adapter.submit_order(
            m.OrderRequest(
                client_order_id="c-1",
                symbol="USD_JPY",
                side=m.OrderSide.BUY,
                order_type=m.OrderType.MARKET,
                quantity=Decimal("1"),
                created_at_utc=NOW,
            )
        )
    assert transport.calls == 1
    assert "OANDA live" in str(exc_info.value)


def test_the_engine_alone_cannot_be_instantiated():
    """``OandaV20Adapter`` declares no endpoint, label or identity, so it
    cannot become an adapter that points somewhere by default."""
    from broker.oanda_v20_adapter import OandaV20Adapter

    with pytest.raises(ValueError, match="abstract"):
        OandaV20Adapter(make_config())
