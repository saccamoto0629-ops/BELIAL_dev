"""Issue #82: the live activation lock, pinned WITHOUT any live connection.

Nothing in this file talks to a network. Every adapter built here gets a
POISONED transport that raises if it is ever called, so "a live connection
was composed" can be asserted without a single byte leaving the process and
without a real credential.

What is pinned:

- ``execution_mode=live`` ALONE is not enough, and neither is any other
  single lock;
- every missing / mixed / unknown configuration fails CLOSED, with a
  machine-readable code;
- the Kill Switch blocks a live startup when it is ON *and* when its state
  cannot be verified at all;
- a live connection is composed only when EVERY lock holds;
- the startup safety summary shows what an operator must check and none of
  the secrets;
- with the Kill Switch on, a submission through the sanctioned boundary
  never reaches the broker.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from broker.broker_models import OrderSide, OrderType
from broker.oanda_demo_adapter import OANDA_DEMO_API_URL
from broker.oanda_live_adapter import (
    ENABLE_LIVE_TRADING_TRUE,
    ENV_ENABLE_LIVE_TRADING,
    ENV_LIVE_ACCOUNT_ID,
    ENV_LIVE_API_TOKEN,
    ENV_LIVE_API_URL,
    OANDA_LIVE_API_URL,
    OandaLiveAdapter,
)
from broker.oanda_v20_adapter import TransportConnectionError
from execution.execution_models import ExecutionRequest
from execution.kill_switch_store import PersistentKillSwitchStateProvider, SQLiteKillSwitchStore
from execution.live_lock import (
    ENDPOINT_KIND_DEMO,
    ENDPOINT_KIND_LIVE,
    ENDPOINT_KIND_NOT_CONFIGURED,
    ENDPOINT_KIND_UNKNOWN,
    KILL_SWITCH_DISABLED,
    KILL_SWITCH_ENABLED,
    KILL_SWITCH_UNVERIFIABLE,
    LiveLockCode,
    LiveSafetyConfigError,
    build_live_connection,
    classify_endpoint,
    evaluate_live_activation,
    load_live_safety_policy,
)
from execution.live_submission import LiveOrderSubmissionService
from execution.reissue_service import ReissuePolicy
from execution.safety_gate import (
    ExecutionMode,
    MarketSafetySnapshot,
    SafetyGate,
    SafetyPolicy,
    SafetyRejectionCode,
    StaticSafetyState,
)
from execution.safety_store import SQLiteSafetyDecisionStore
from execution.submission_journal import SubmissionJournal

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
TOKEN = "live-token-SECRET"
ACCOUNT = "101-004-LIVEACCOUNT-001"
DEMO_TOKEN = "demo-token"
DEMO_ACCOUNT = "101-001-DEMOACCOUNT-001"


def poisoned_transport(*args, **kwargs):  # pragma: no cover - must never run
    raise AssertionError("no test may perform a live HTTP call")


def live_env(**overrides):
    env = {
        ENV_ENABLE_LIVE_TRADING: ENABLE_LIVE_TRADING_TRUE,
        ENV_LIVE_API_TOKEN: TOKEN,
        ENV_LIVE_ACCOUNT_ID: ACCOUNT,
        ENV_LIVE_API_URL: OANDA_LIVE_API_URL,
    }
    env.update(overrides)
    return {name: value for name, value in env.items() if value is not None}


def policy(**overrides):
    values = dict(
        policy_id="live-policy-1",
        allowed_symbols=frozenset({"USD_JPY"}),
        allowed_sides=frozenset({OrderSide.BUY}),
        max_order_quantity=Decimal("10"),
        max_positions_per_symbol=2,
        max_positions_account=5,
        max_reference_age=timedelta(seconds=30),
        max_spread_by_symbol={"USD_JPY": Decimal("0.05")},
        allowed_connection_targets={ExecutionMode.LIVE: frozenset({"oanda-live"})},
    )
    values.update(overrides)
    return SafetyPolicy(**values)


def evaluate(**overrides):
    values = dict(
        execution_mode="live",
        confirm_live=True,
        environ=live_env(),
        policy=policy(),
        state_provider=StaticSafetyState(kill_switch_enabled=False),
    )
    values.update(overrides)
    return evaluate_live_activation(**values)


def build(**overrides):
    values = dict(
        execution_mode="live",
        confirm_live=True,
        environ=live_env(),
        policy=policy(),
        state_provider=StaticSafetyState(kill_switch_enabled=False),
        transport=poisoned_transport,
    )
    values.update(overrides)
    return build_live_connection(**values)


def codes(decision):
    return set(decision.rejection_codes)


# --- the happy path: every lock, and only then ---------------------------------


def test_a_live_connection_is_composed_only_when_every_lock_holds():
    decision, connection = build()
    assert decision.allowed is True
    assert decision.rejections == ()
    assert connection is not None
    assert connection.execution_mode is ExecutionMode.LIVE
    assert connection.connection_target == "oanda-live"
    assert isinstance(connection.broker, OandaLiveAdapter)


# --- one lock at a time: each one alone refuses --------------------------------


def test_execution_mode_live_alone_is_not_enough():
    """The headline requirement: the mode is ONE of five locks, never the
    switch that turns live trading on."""
    decision, connection = build(
        execution_mode="live",
        confirm_live=False,
        environ={},
    )
    assert decision.allowed is False
    assert connection is None
    assert codes(decision) >= {
        LiveLockCode.LIVE_TRADING_DISABLED.value,
        LiveLockCode.LIVE_CONFIRMATION_MISSING.value,
        LiveLockCode.LIVE_CREDENTIALS_MISSING.value,
        LiveLockCode.LIVE_ENDPOINT_INVALID.value,
    }
    assert LiveLockCode.EXECUTION_MODE_NOT_LIVE.value not in codes(decision)


def test_enable_live_trading_alone_is_not_enough():
    decision, connection = build(
        execution_mode="shadow",
        confirm_live=False,
        environ={ENV_ENABLE_LIVE_TRADING: ENABLE_LIVE_TRADING_TRUE},
    )
    assert decision.allowed is False
    assert connection is None
    assert LiveLockCode.EXECUTION_MODE_NOT_LIVE.value in codes(decision)
    assert LiveLockCode.LIVE_CONFIRMATION_MISSING.value in codes(decision)


def test_missing_confirm_live_refuses_even_with_everything_else_in_place():
    decision, connection = build(confirm_live=False)
    assert decision.allowed is False
    assert connection is None
    assert codes(decision) == {LiveLockCode.LIVE_CONFIRMATION_MISSING.value}


@pytest.mark.parametrize("flag", [None, "false", "TRUE", "1", "yes", " true "])
def test_only_the_exact_enable_flag_value_unlocks_live(flag):
    env = live_env()
    if flag is None:
        del env[ENV_ENABLE_LIVE_TRADING]
    else:
        env[ENV_ENABLE_LIVE_TRADING] = flag
    decision, connection = build(environ=env)
    assert decision.allowed is False
    assert connection is None
    assert LiveLockCode.LIVE_TRADING_DISABLED.value in codes(decision)


@pytest.mark.parametrize("missing", [ENV_LIVE_ACCOUNT_ID, ENV_LIVE_API_TOKEN])
def test_missing_live_credentials_refuse_the_startup(missing):
    env = live_env()
    del env[missing]
    decision, connection = build(environ=env)
    assert decision.allowed is False
    assert connection is None
    assert codes(decision) == {LiveLockCode.LIVE_CREDENTIALS_MISSING.value}
    assert missing in decision.rejections[0].message


def test_missing_live_endpoint_refuses_the_startup():
    env = live_env()
    del env[ENV_LIVE_API_URL]
    decision, connection = build(environ=env)
    assert decision.allowed is False
    assert connection is None
    assert codes(decision) == {LiveLockCode.LIVE_ENDPOINT_INVALID.value}


@pytest.mark.parametrize(
    "endpoint",
    [
        pytest.param("https://api-fxtrade.oanda.com.evil.example", id="lookalike"),
        pytest.param("http://api-fxtrade.oanda.com", id="plain-http"),
        pytest.param("https://example.invalid", id="unrelated"),
        pytest.param("   ", id="blank"),
    ],
)
def test_unknown_live_endpoints_refuse_the_startup(endpoint):
    decision, connection = build(environ=live_env(**{ENV_LIVE_API_URL: endpoint}))
    assert decision.allowed is False
    assert connection is None
    assert LiveLockCode.LIVE_ENDPOINT_INVALID.value in codes(decision)


def test_endpoint_refusal_never_echoes_the_configured_value():
    secret_url = "https://api-fxtrade.oanda.com.attacker.example/?token=leak"
    decision, _ = build(environ=live_env(**{ENV_LIVE_API_URL: secret_url}))
    rendered = " ".join(entry.message for entry in decision.rejections)
    assert secret_url not in rendered
    assert "attacker" not in rendered and "leak" not in rendered


# --- demo / live mixing, in both directions ------------------------------------


def test_live_mode_with_a_demo_endpoint_is_refused():
    decision, connection = build(environ=live_env(**{ENV_LIVE_API_URL: OANDA_DEMO_API_URL}))
    assert decision.allowed is False
    assert connection is None
    assert codes(decision) >= {
        LiveLockCode.LIVE_ENDPOINT_INVALID.value,
        LiveLockCode.DEMO_LIVE_CONFIGURATION_MIXED.value,
    }


def test_live_mode_with_a_shared_demo_account_is_refused():
    decision, connection = build(environ=live_env(OANDA_ACCOUNT_ID=ACCOUNT))
    assert decision.allowed is False
    assert connection is None
    assert LiveLockCode.DEMO_LIVE_CONFIGURATION_MIXED.value in codes(decision)


def test_live_mode_with_a_shared_demo_token_is_refused():
    decision, connection = build(environ=live_env(OANDA_API_TOKEN=TOKEN))
    assert decision.allowed is False
    assert connection is None
    assert LiveLockCode.DEMO_LIVE_CONFIGURATION_MIXED.value in codes(decision)


def test_demo_mode_pointed_at_a_live_endpoint_is_refused():
    decision, connection = build(
        execution_mode="demo",
        confirm_live=False,
        environ={
            "OANDA_API_TOKEN": DEMO_TOKEN,
            "OANDA_ACCOUNT_ID": DEMO_ACCOUNT,
            "OANDA_API_URL": OANDA_LIVE_API_URL,
        },
    )
    assert decision.allowed is False
    assert connection is None
    assert codes(decision) >= {
        LiveLockCode.EXECUTION_MODE_NOT_LIVE.value,
        LiveLockCode.DEMO_LIVE_CONFIGURATION_MIXED.value,
    }


def test_demo_mode_holding_the_live_account_is_refused():
    decision, connection = build(
        execution_mode="demo",
        confirm_live=False,
        environ={
            "OANDA_API_TOKEN": DEMO_TOKEN,
            "OANDA_ACCOUNT_ID": ACCOUNT,
            "OANDA_API_URL": OANDA_DEMO_API_URL,
            ENV_LIVE_ACCOUNT_ID: ACCOUNT,
        },
    )
    assert decision.allowed is False
    assert connection is None
    assert LiveLockCode.DEMO_LIVE_CONFIGURATION_MIXED.value in codes(decision)


def test_demo_mode_started_with_the_live_switch_is_refused():
    decision, connection = build(
        execution_mode="demo",
        confirm_live=False,
        environ={ENV_ENABLE_LIVE_TRADING: ENABLE_LIVE_TRADING_TRUE},
    )
    assert decision.allowed is False
    assert connection is None
    assert LiveLockCode.DEMO_LIVE_CONFIGURATION_MIXED.value in codes(decision)


@pytest.mark.parametrize("mode", ["dry-run", "shadow", "fake", "demo", "bogus", "", None, 7])
def test_no_other_execution_mode_can_produce_a_live_connection(mode):
    decision, connection = build(execution_mode=mode, confirm_live=True)
    assert decision.allowed is False
    assert connection is None
    assert LiveLockCode.EXECUTION_MODE_NOT_LIVE.value in codes(decision)


# --- kill switch ---------------------------------------------------------------


def test_kill_switch_on_refuses_before_a_connection_exists():
    decision, connection = build(state_provider=StaticSafetyState(kill_switch_enabled=True))
    assert decision.allowed is False
    assert connection is None
    assert codes(decision) == {LiveLockCode.KILL_SWITCH_ENABLED.value}
    assert decision.summary.kill_switch == KILL_SWITCH_ENABLED


def test_an_unreadable_kill_switch_is_never_treated_as_off():
    class BrokenProvider:
        def is_kill_switch_enabled(self):
            raise RuntimeError("schema mismatch")

    decision, connection = build(state_provider=BrokenProvider())
    assert decision.allowed is False
    assert connection is None
    assert codes(decision) == {LiveLockCode.KILL_SWITCH_STATE_UNVERIFIABLE.value}
    assert decision.summary.kill_switch == KILL_SWITCH_UNVERIFIABLE


@pytest.mark.parametrize("answer", [None, "false", 0, 1, "off", object()])
def test_a_non_boolean_kill_switch_answer_is_refused(answer):
    class WeirdProvider:
        def is_kill_switch_enabled(self):
            return answer

    decision, connection = build(state_provider=WeirdProvider())
    assert decision.allowed is False
    assert connection is None
    assert codes(decision) == {LiveLockCode.KILL_SWITCH_STATE_UNVERIFIABLE.value}


def test_a_missing_persistent_kill_switch_store_refuses_the_startup(tmp_path):
    """The production provider, against a store that was never created:
    fail-closed, and the store is NOT created as a side effect."""
    db_path = tmp_path / "kill_switch.sqlite3"
    decision, connection = build(state_provider=PersistentKillSwitchStateProvider(db_path))
    assert decision.allowed is False
    assert connection is None
    assert codes(decision) == {LiveLockCode.KILL_SWITCH_STATE_UNVERIFIABLE.value}
    assert not db_path.exists()


def test_a_verified_off_persistent_kill_switch_allows_the_startup(tmp_path):
    db_path = tmp_path / "kill_switch.sqlite3"
    store = SQLiteKillSwitchStore(db_path)
    store.initialize_and_record(
        enabled=True, operator="ops", reason="initial halt", changed_at_utc=NOW
    )
    store.record_transition(
        enabled=False,
        operator="ops",
        reason="ready for live",
        changed_at_utc=NOW + timedelta(minutes=1),
    )
    decision, connection = build(state_provider=PersistentKillSwitchStateProvider(db_path))
    assert decision.allowed is True
    assert connection is not None
    assert decision.summary.kill_switch == KILL_SWITCH_DISABLED


# --- safety configuration ------------------------------------------------------


def test_an_unavailable_safety_policy_refuses_the_startup():
    decision, connection = build(policy=None)
    assert decision.allowed is False
    assert connection is None
    assert codes(decision) == {LiveLockCode.SAFETY_POLICY_UNAVAILABLE.value}


def test_a_non_policy_object_refuses_the_startup():
    decision, connection = build(policy={"policy_id": "forged"})
    assert decision.allowed is False
    assert connection is None
    assert codes(decision) == {LiveLockCode.SAFETY_POLICY_UNAVAILABLE.value}


def test_a_policy_that_does_not_allow_the_live_target_refuses_the_startup():
    decision, connection = build(
        policy=policy(allowed_connection_targets={ExecutionMode.LIVE: frozenset({"somewhere"})})
    )
    assert decision.allowed is False
    assert connection is None
    assert codes(decision) == {LiveLockCode.LIVE_TARGET_NOT_ALLOWED.value}


def write_safety_config(tmp_path, **overrides):
    document = {
        "policy_id": "live-v1",
        "allowed_symbols": ["USD_JPY"],
        "allowed_sides": ["buy"],
        "max_order_quantity": "10",
        "max_positions_per_symbol": 2,
        "max_positions_account": 5,
        "max_reference_age_seconds": 30,
        "max_spread_by_symbol": {"USD_JPY": "0.05"},
        "allowed_live_targets": ["oanda-live"],
        "require_stop_loss": True,
    }
    document.update(overrides)
    path = tmp_path / "live_safety.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_the_safety_configuration_round_trips_into_a_policy(tmp_path):
    loaded = load_live_safety_policy(write_safety_config(tmp_path))
    assert loaded.policy_id == "live-v1"
    assert loaded.allowed_symbols == frozenset({"USD_JPY"})
    assert loaded.max_order_quantity == Decimal("10")
    assert loaded.allowed_connection_targets[ExecutionMode.LIVE] == frozenset({"oanda-live"})


def test_a_missing_safety_configuration_file_fails_closed(tmp_path):
    with pytest.raises(LiveSafetyConfigError):
        load_live_safety_policy(tmp_path / "absent.json")


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"allowed_symbols": []}, id="no-symbols"),
        pytest.param({"max_order_quantity": "0"}, id="zero-quantity"),
        pytest.param({"max_order_quantity": 10}, id="quantity-not-a-string"),
        pytest.param({"allowed_sides": ["sideways"]}, id="unknown-side"),
        pytest.param({"allowed_live_targets": []}, id="no-live-target"),
        pytest.param({"require_stop_loss": "yes"}, id="non-boolean"),
        pytest.param({"max_reference_age_seconds": 0}, id="zero-age"),
        pytest.param({"surprise": 1}, id="unknown-key"),
    ],
)
def test_an_invalid_safety_configuration_fails_closed(tmp_path, overrides):
    with pytest.raises(LiveSafetyConfigError):
        load_live_safety_policy(write_safety_config(tmp_path, **overrides))


def test_a_safety_configuration_missing_a_field_fails_closed(tmp_path):
    document = json.loads(write_safety_config(tmp_path).read_text(encoding="utf-8"))
    for key in list(document):
        partial = dict(document)
        del partial[key]
        path = tmp_path / "partial.json"
        path.write_text(json.dumps(partial), encoding="utf-8")
        with pytest.raises(LiveSafetyConfigError):
            load_live_safety_policy(path)


# --- the startup safety summary -------------------------------------------------


def test_the_summary_shows_every_safety_setting_an_operator_must_check():
    summary = evaluate().summary
    assert summary.execution_mode == "live"
    assert summary.endpoint_kind == ENDPOINT_KIND_LIVE
    assert summary.account_reference is not None
    assert summary.allowed_symbols == ("USD_JPY",)
    assert summary.max_order_quantity == "10"
    assert summary.max_positions_per_symbol == 2
    assert summary.max_positions_account == 5
    assert summary.kill_switch == KILL_SWITCH_DISABLED
    assert summary.live_confirmed is True
    assert dict(summary.locks) == {
        "execution_mode=live": True,
        f"{ENV_ENABLE_LIVE_TRADING}={ENABLE_LIVE_TRADING_TRUE}": True,
        "live credentials configured": True,
        "live endpoint allowlisted": True,
        "--confirm-live": True,
    }
    rendered = summary.render()
    for expected in (
        "execution mode",
        "endpoint kind",
        "account (masked)",
        "allowed symbols",
        "max order quantity",
        "max positions / symbol",
        "max positions / account",
        "kill switch",
        "live confirmation",
    ):
        assert expected in rendered


def test_the_summary_never_shows_a_token_account_id_or_endpoint():
    summary = evaluate().summary
    surfaces = [summary.render(), json.dumps(summary.to_dict()), repr(summary)]
    for surface in surfaces:
        assert TOKEN not in surface
        assert ACCOUNT not in surface
        assert OANDA_LIVE_API_URL not in surface
        assert "api-fxtrade" not in surface
        assert "Bearer" not in surface
        assert "Authorization" not in surface


def test_the_masked_account_reference_is_stable_and_not_reversible():
    first = evaluate().summary.account_reference
    second = evaluate().summary.account_reference
    other = evaluate(environ=live_env(**{ENV_LIVE_ACCOUNT_ID: "999-other"})).summary
    assert first == second
    assert first != other.account_reference
    assert ACCOUNT not in first
    assert first.startswith("account-")


def test_a_refused_summary_reports_the_unsatisfied_locks():
    summary = evaluate(confirm_live=False).summary
    assert summary.live_confirmed is False
    assert dict(summary.locks)["--confirm-live"] is False
    assert LiveLockCode.LIVE_CONFIRMATION_MISSING.value in summary.rejection_codes
    assert "[ ] --confirm-live" in summary.render()


def test_no_rejection_message_anywhere_carries_a_secret():
    """Every refusal path is swept at once: whatever combination produced
    the rejections, none of them may contain a credential."""
    environments = [
        live_env(),
        live_env(**{ENV_LIVE_API_URL: OANDA_DEMO_API_URL}),
        live_env(OANDA_API_TOKEN=TOKEN, OANDA_ACCOUNT_ID=ACCOUNT),
        live_env(**{ENV_ENABLE_LIVE_TRADING: "false"}),
        {},
    ]
    for env in environments:
        for confirm in (True, False):
            decision, _ = build(environ=env, confirm_live=confirm)
            rendered = " ".join(entry.message for entry in decision.rejections)
            rendered += decision.summary.render() + json.dumps(decision.summary.to_dict())
            assert TOKEN not in rendered
            assert ACCOUNT not in rendered
            assert OANDA_LIVE_API_URL not in rendered


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (OANDA_LIVE_API_URL, ENDPOINT_KIND_LIVE),
        (OANDA_LIVE_API_URL + "/", ENDPOINT_KIND_LIVE),
        (OANDA_DEMO_API_URL, ENDPOINT_KIND_DEMO),
        ("https://api-fxtrade.oanda.com.evil.example", ENDPOINT_KIND_LIVE),
        ("https://something-fxpractice.example", ENDPOINT_KIND_DEMO),
        ("https://example.invalid", ENDPOINT_KIND_UNKNOWN),
        ("", ENDPOINT_KIND_NOT_CONFIGURED),
        (None, ENDPOINT_KIND_NOT_CONFIGURED),
    ],
)
def test_endpoint_classification_is_a_label_never_the_value(value, expected):
    assert classify_endpoint(value) == expected


# --- the sanctioned submission boundary is untouched ---------------------------


def test_kill_switch_blocks_a_live_submission_before_any_broker_call(tmp_path):
    """The gate contract (Issue #75 / #81) applied to a REAL live connection:
    with the switch on, ``submit()`` is refused and NO ORDER is sent.

    What is pinned is the order boundary: not one POST to ``/orders`` is
    attempted. The gate's read-only safety queries (positions / account) may
    still run -- the gate deliberately collects EVERY rejection rather than
    short-circuiting on the first, and Issue #82 does not redesign it. In
    the sanctioned startup flow that read never happens either, because the
    live activation lock refuses to compose a connection at all while the
    switch is on (see the tests above); this covers the switch being flipped
    AFTER a process is already live.
    """
    calls = []

    def recording_transport(method, *args, **kwargs):
        calls.append(method)
        if method != "GET":  # pragma: no cover - must never run
            raise AssertionError("no order may be sent while the kill switch is on")
        raise TransportConnectionError("read query refused in this test")

    decision, connection = build(transport=recording_transport)
    assert decision.allowed is True

    gate = SafetyGate(
        policy=policy(),
        state_provider=StaticSafetyState(kill_switch_enabled=True),
        clock=lambda: NOW,
    )
    SQLiteSafetyDecisionStore(tmp_path / "safety.sqlite3").initialize()
    SubmissionJournal(tmp_path / "journal.sqlite3").initialize()
    service = LiveOrderSubmissionService(
        connection=connection,
        gate=gate,
        decision_db_path=tmp_path / "safety.sqlite3",
        order_db_path=tmp_path / "orders.sqlite3",
        journal_db_path=tmp_path / "journal.sqlite3",
        clock=lambda: NOW,
        reissue_policy=ReissuePolicy(),
    )
    request = ExecutionRequest(
        execution_request_id="exec-1",
        signal_id="signal-1",
        strategy_id="strategy-1",
        symbol="USD_JPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        requested_at_utc=NOW,
        stop_loss_price=Decimal("149.5"),
    )
    service.register(request, client_order_id="client-1")
    gate_decision, result = service.submit(
        "exec-1",
        market=MarketSafetySnapshot(
            symbol="USD_JPY",
            reference_price=Decimal("150"),
            bid=Decimal("149.99"),
            ask=Decimal("150.01"),
            observed_at_utc=NOW,
            market_open=True,
            tradable=True,
        ),
    )
    assert result is None
    assert gate_decision.allowed is False
    assert SafetyRejectionCode.KILL_SWITCH_ENABLED.value in gate_decision.rejection_codes
    assert set(calls) <= {"GET"}, "a state-changing broker call was attempted"


def test_an_unreadable_kill_switch_blocks_a_live_submission_too(tmp_path):
    class BrokenProvider:
        def is_kill_switch_enabled(self):
            raise RuntimeError("kill switch DB is corrupt")

    calls = []

    def recording_transport(method, *args, **kwargs):
        calls.append(method)
        if method != "GET":  # pragma: no cover - must never run
            raise AssertionError("no order may be sent on an unverifiable kill switch")
        raise TransportConnectionError("read query refused in this test")

    _, connection = build(transport=recording_transport)
    gate = SafetyGate(
        policy=policy(), state_provider=BrokenProvider(), clock=lambda: NOW
    )
    SQLiteSafetyDecisionStore(tmp_path / "safety.sqlite3").initialize()
    SubmissionJournal(tmp_path / "journal.sqlite3").initialize()
    service = LiveOrderSubmissionService(
        connection=connection,
        gate=gate,
        decision_db_path=tmp_path / "safety.sqlite3",
        order_db_path=tmp_path / "orders.sqlite3",
        journal_db_path=tmp_path / "journal.sqlite3",
        clock=lambda: NOW,
        reissue_policy=ReissuePolicy(),
    )
    request = ExecutionRequest(
        execution_request_id="exec-2",
        signal_id="signal-2",
        strategy_id="strategy-1",
        symbol="USD_JPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        requested_at_utc=NOW,
        stop_loss_price=Decimal("149.5"),
    )
    service.register(request, client_order_id="client-2")
    gate_decision, result = service.submit(
        "exec-2",
        market=MarketSafetySnapshot(
            symbol="USD_JPY",
            reference_price=Decimal("150"),
            bid=Decimal("149.99"),
            ask=Decimal("150.01"),
            observed_at_utc=NOW,
            market_open=True,
            tradable=True,
        ),
    )
    assert result is None
    assert gate_decision.allowed is False
    assert SafetyRejectionCode.STATE_QUERY_FAILED.value in gate_decision.rejection_codes
    assert set(calls) <= {"GET"}, "a state-changing broker call was attempted"
