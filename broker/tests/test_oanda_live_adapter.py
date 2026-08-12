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


# The issuers read the REAL process environment (PR #128 Codex review r3):
# neither accepts a mapping, so tests configure the environment exactly the
# way production does. ``monkeypatch`` keeps that per-test.
_LIVE_ENV_NAMES = (
    live.ENV_ENABLE_LIVE_TRADING,
    live.ENV_LIVE_API_TOKEN,
    live.ENV_LIVE_ACCOUNT_ID,
    live.ENV_LIVE_API_URL,
    live.ENV_LIVE_HTTP_TIMEOUT_SECONDS,
    "OANDA_API_TOKEN",
    "OANDA_ACCOUNT_ID",
    "OANDA_API_URL",
)


@pytest.fixture(autouse=True)
def clean_live_environment(monkeypatch):
    """No test inherits a real credential, and none leaks into the next."""
    for name in _LIVE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def set_live_env(monkeypatch, **overrides):
    values = {
        live.ENV_ENABLE_LIVE_TRADING: live.ENABLE_LIVE_TRADING_TRUE,
        live.ENV_LIVE_API_TOKEN: TOKEN,
        live.ENV_LIVE_ACCOUNT_ID: ACCOUNT,
        live.ENV_LIVE_API_URL: live.OANDA_LIVE_API_URL,
    }
    values.update(overrides)
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def make_config(monkeypatch, **overrides):
    set_live_env(monkeypatch, **overrides)
    return live.OandaLiveConfig.from_env()


def make_activation(monkeypatch):
    set_live_env(monkeypatch)
    return live.LiveActivation.from_environment(confirm_live=True)


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


def make_adapter(monkeypatch, *responses):
    transport = FakeTransport(*responses)
    set_live_env(monkeypatch)
    adapter = live.OandaLiveAdapter(
        live.OandaLiveConfig.from_env(),
        live.LiveActivation.from_environment(confirm_live=True),
        transport=transport,
        clock=lambda: NOW,
    )
    return adapter, transport


# --- lock 2 + 3: the activation grant -----------------------------------------


def test_activation_requires_both_the_env_switch_and_the_cli_confirmation(monkeypatch):
    assert make_activation(monkeypatch).confirm_live is True


def test_a_grant_cannot_be_constructed_from_plain_values(monkeypatch):
    """PR #128 Codex review r3 P1: evidence must be ISSUED. Writing the
    proofs out by hand in a process whose environment is empty must not
    produce a grant."""
    for name in _LIVE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    # positionally the first slot is the witness, so the old
    # ``LiveActivation("true", True)`` spelling is refused outright
    with pytest.raises(live.LiveActivationError, match="must be ISSUED"):
        live.LiveActivation("true", True)
    with pytest.raises(live.LiveActivationError, match="must be ISSUED"):
        live.LiveActivation(
            issuance_witness=object(), enable_live_trading_value="true", confirm_live=True
        )


def test_a_config_cannot_be_constructed_from_plain_values(monkeypatch):
    """The same rule for the config: a hand-built one would also skip the
    demo/live credential separation only ``from_env`` can perform."""
    # positional (no witness at all) and an explicitly forged witness
    with pytest.raises(TypeError):
        live.OandaLiveConfig(
            api_token=TOKEN, account_id=ACCOUNT, endpoint=live.OANDA_LIVE_API_URL
        )
    with pytest.raises(live.LiveActivationError, match="must be ISSUED"):
        live.OandaLiveConfig(
            issuance_witness=object(),
            api_token=TOKEN,
            account_id=ACCOUNT,
            endpoint=live.OANDA_LIVE_API_URL,
        )


def test_the_issuers_take_no_caller_supplied_mapping():
    """A mapping parameter would be exactly the freely-constructed evidence
    the issuance path exists to prevent."""
    import inspect

    assert "environ" not in inspect.signature(live.LiveActivation.from_environment).parameters
    assert "environ" not in inspect.signature(live.OandaLiveConfig.from_env).parameters


def test_a_valid_witness_cannot_be_laundered_onto_invalid_values(monkeypatch):
    """``dataclasses.replace`` copies the witness, so it is the obvious way
    to try to keep the evidence while changing what it attests to. Every
    field is re-validated, so it cannot be used that way."""
    import dataclasses

    set_live_env(monkeypatch)
    grant = live.LiveActivation.from_environment(confirm_live=True)
    config = live.OandaLiveConfig.from_env()
    for attempt in (
        lambda: dataclasses.replace(grant, confirm_live=False),
        lambda: dataclasses.replace(grant, enable_live_trading_value="yes"),
        lambda: dataclasses.replace(config, endpoint=demo.OANDA_DEMO_API_URL),
        lambda: dataclasses.replace(config, api_token=""),
    ):
        with pytest.raises(live.LiveActivationError):
            attempt()


def test_an_empty_environment_yields_no_grant_however_it_is_asked(monkeypatch):
    for name in _LIVE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(live.LiveActivationError, match="ENABLE_LIVE_TRADING"):
        live.LiveActivation.from_environment(confirm_live=True)
    with pytest.raises(live.LiveActivationError, match=live.ENV_LIVE_API_TOKEN):
        live.OandaLiveConfig.from_env()


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
def test_activation_is_refused_unless_both_locks_are_exact(
    monkeypatch, env_value, confirm_live, expected
):
    if env_value is None:
        monkeypatch.delenv(live.ENV_ENABLE_LIVE_TRADING, raising=False)
    else:
        monkeypatch.setenv(live.ENV_ENABLE_LIVE_TRADING, env_value)
    with pytest.raises(live.LiveActivationError, match=expected):
        live.LiveActivation.from_environment(confirm_live=confirm_live)


@pytest.mark.parametrize("truthy", [1, "true", object()])
def test_confirm_live_must_be_the_boolean_true_not_merely_truthy(monkeypatch, truthy):
    monkeypatch.setenv(live.ENV_ENABLE_LIVE_TRADING, "true")
    with pytest.raises(live.LiveActivationError, match="--confirm-live"):
        live.LiveActivation.from_environment(confirm_live=truthy)


def test_adapter_refuses_to_exist_without_a_grant(monkeypatch):
    for activation in (None, True, "confirmed", object()):
        with pytest.raises(live.LiveActivationError, match="LiveActivation grant"):
            live.OandaLiveAdapter(make_config(monkeypatch), activation, transport=FakeTransport())


def test_a_grant_subclass_that_skips_validation_is_refused(monkeypatch):
    """PR #128 Codex review P1: a grant is only evidence if the validation
    that makes it evidence actually ran. A subclass can override
    ``__post_init__`` away, so only the exact validated class is accepted."""

    class ForgedActivation(live.LiveActivation):
        def __post_init__(self):  # skips BOTH lock checks
            pass

    forged = ForgedActivation(
        issuance_witness=None, enable_live_trading_value=None, confirm_live=False
    )
    assert isinstance(forged, live.LiveActivation)  # the hole isinstance would leave
    with pytest.raises(live.LiveActivationError, match="exact LiveActivation grant"):
        live.OandaLiveAdapter(make_config(monkeypatch), forged, transport=FakeTransport())


def test_a_config_subclass_that_skips_the_allowlist_is_refused(monkeypatch):
    """The same hole on the config side: a subclass could carry empty
    credentials or the demo endpoint straight past the allowlist."""

    class ForgedConfig(live.OandaLiveConfig):
        def __post_init__(self):  # skips the endpoint allowlist entirely
            pass

    forged = ForgedConfig(
        issuance_witness=None, api_token="", account_id="", endpoint=demo.OANDA_DEMO_API_URL
    )
    assert isinstance(forged, live.OandaLiveConfig)
    with pytest.raises(ValueError, match="exactly a OandaLiveConfig"):
        live.OandaLiveAdapter(forged, make_activation(monkeypatch), transport=FakeTransport())


def test_the_demo_adapter_refuses_a_config_subclass_too():
    """The same sealing applies to the demo adapter (strengthened, never
    weakened, by Issue #82)."""

    class ForgedDemoConfig(demo.OandaDemoConfig):
        def __post_init__(self):
            pass

    forged = ForgedDemoConfig(
        api_token="", account_id="", endpoint=live.OANDA_LIVE_API_URL
    )
    with pytest.raises(ValueError, match="exactly a OandaDemoConfig"):
        demo.OandaDemoAdapter(forged, transport=FakeTransport())


# --- lock 4 + 5: credentials and the endpoint allowlist ------------------------


def test_live_config_accepts_only_the_live_endpoint(monkeypatch):
    assert make_config(monkeypatch).endpoint == live.OANDA_LIVE_API_URL
    trailing = make_config(
        monkeypatch, **{live.ENV_LIVE_API_URL: live.OANDA_LIVE_API_URL + "/"}
    )
    assert trailing.endpoint == live.OANDA_LIVE_API_URL


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
def test_live_config_refuses_every_other_endpoint(monkeypatch, endpoint):
    with pytest.raises(live.LiveActivationError):
        make_config(monkeypatch, **{live.ENV_LIVE_API_URL: endpoint})


def test_endpoint_rejection_never_echoes_the_configured_value(monkeypatch):
    secret_url = "https://api-fxtrade.oanda.com.attacker.example/leak?token=abc"
    with pytest.raises(live.LiveActivationError) as exc_info:
        make_config(monkeypatch, **{live.ENV_LIVE_API_URL: secret_url})
    message = str(exc_info.value)
    assert secret_url not in message
    assert "attacker" not in message and "abc" not in message


@pytest.mark.parametrize("missing", [live.ENV_LIVE_API_TOKEN, live.ENV_LIVE_ACCOUNT_ID])
def test_from_env_requires_every_live_credential(monkeypatch, missing):
    set_live_env(monkeypatch, **{missing: None})
    with pytest.raises(live.LiveActivationError, match=missing):
        live.OandaLiveConfig.from_env()


def test_from_env_requires_an_explicit_live_endpoint(monkeypatch):
    """The demo endpoint has a default; the LIVE endpoint deliberately does
    not -- "not configured" must fail closed, never resolve to a live host."""
    set_live_env(monkeypatch, **{live.ENV_LIVE_API_URL: None})
    with pytest.raises(live.LiveActivationError, match=live.ENV_LIVE_API_URL):
        live.OandaLiveConfig.from_env()


def test_from_env_reads_live_only_variables(monkeypatch):
    """Demo variables are NOT a fallback: an environment that only carries
    demo credentials cannot produce a live config."""
    monkeypatch.setenv("OANDA_API_TOKEN", "demo-token")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "demo-account")
    monkeypatch.setenv("OANDA_API_URL", demo.OANDA_DEMO_API_URL)
    monkeypatch.setenv(live.ENV_ENABLE_LIVE_TRADING, "true")
    with pytest.raises(live.LiveActivationError, match=live.ENV_LIVE_API_TOKEN):
        live.OandaLiveConfig.from_env()


@pytest.mark.parametrize(
    ("demo_name", "live_name", "shared"),
    [
        ("OANDA_API_TOKEN", live.ENV_LIVE_API_TOKEN, TOKEN),
        ("OANDA_ACCOUNT_ID", live.ENV_LIVE_ACCOUNT_ID, ACCOUNT),
    ],
)
def test_shared_demo_and_live_credentials_are_refused(monkeypatch, demo_name, live_name, shared):
    set_live_env(monkeypatch, **{demo_name: shared})
    with pytest.raises(live.LiveActivationError) as exc_info:
        live.OandaLiveConfig.from_env()
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


def test_config_repr_hides_token_account_and_endpoint(monkeypatch):
    rendered = repr(make_config(monkeypatch))
    for secret in (TOKEN, ACCOUNT, live.OANDA_LIVE_API_URL):
        assert secret not in rendered


def test_activation_repr_holds_no_environment_value(monkeypatch):
    assert "true" not in repr(make_activation(monkeypatch))


def test_broker_error_text_scrubs_the_live_secrets(monkeypatch):
    """The shared engine's redaction applies to the live adapter unchanged:
    a broker that echoes our token / account / endpoint back cannot get them
    into an exception or a reject_reason."""
    adapter, _ = make_adapter(monkeypatch,
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


def test_connection_identity_is_the_constant_live_label(monkeypatch):
    adapter, _ = make_adapter(monkeypatch)
    identity = adapter.connection_identity()
    assert identity.mode == "live"
    assert identity.target == live.OANDA_LIVE_CONNECTION_TARGET
    assert live.OANDA_LIVE_API_URL not in identity.target
    assert ACCOUNT not in identity.target


def test_the_live_adapter_satisfies_the_broker_protocol(monkeypatch):
    adapter, _ = make_adapter(monkeypatch)
    assert isinstance(adapter, Broker)


def test_the_live_adapter_is_not_a_demo_adapter_and_vice_versa():
    """Neither class is a subclass of the other, so the Safety Gate's
    concrete-type cross-check can bind each mode to exactly one type."""
    assert not issubclass(live.OandaLiveAdapter, demo.OandaDemoAdapter)
    assert not issubclass(demo.OandaDemoAdapter, live.OandaLiveAdapter)


def test_submit_uses_the_shared_v20_translation_against_the_live_host(monkeypatch):
    adapter, transport = make_adapter(monkeypatch,
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


def test_transport_failures_stay_unknown_and_are_never_resent(monkeypatch):
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
        make_config(monkeypatch), make_activation(monkeypatch), transport=transport, clock=lambda: NOW
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


def test_the_engine_alone_cannot_be_instantiated(monkeypatch):
    """``OandaV20Adapter`` declares no endpoint, label or identity, so it
    cannot become an adapter that points somewhere by default."""
    from broker.oanda_v20_adapter import OandaV20Adapter

    with pytest.raises(ValueError, match="abstract"):
        OandaV20Adapter(make_config(monkeypatch))
