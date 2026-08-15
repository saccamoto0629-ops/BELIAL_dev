"""Issue #132: the demo activation lock, pinned WITHOUT any network.

Nothing in this file opens a socket. Every adapter built here gets a
POISONED transport that raises if it is ever called, so "a demo connection
was composed" can be asserted without a single byte leaving the process and
without a real credential.

What is pinned:

- ``execution_mode=demo`` ALONE is not enough, and neither is any other
  single lock;
- every missing / mixed / unknown configuration fails CLOSED, with a
  machine-readable code -- endpoint, credentials, demo/live separation and
  the pinned ACCOUNT identity in particular;
- the Kill Switch blocks a demo startup when it is ON *and* when its state
  cannot be verified at all;
- a demo grant is issued only when EVERY lock holds;
- the startup safety summary shows what an operator must check, the account
  digest they have to pin, and none of the secrets;
- nothing on this path can reach the LIVE endpoint or a live connection.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from broker.broker_models import safe_account_reference
from broker.oanda_demo_adapter import (
    ENV_ACCOUNT_ID,
    ENV_API_TOKEN,
    ENV_API_URL,
    OANDA_DEMO_API_URL,
    OANDA_DEMO_CONNECTION_TARGET,
    OandaDemoAdapter,
)
from broker.oanda_live_adapter import (
    ENABLE_LIVE_TRADING_TRUE,
    ENV_ENABLE_LIVE_TRADING,
    ENV_LIVE_ACCOUNT_ID,
    ENV_LIVE_API_TOKEN,
    ENV_LIVE_API_URL,
    ENV_LIVE_HTTP_TIMEOUT_SECONDS,
    OANDA_LIVE_API_URL,
)
from execution.demo_lock import (
    DemoLockCode,
    DemoRuntimeGrant,
    DemoSafetyConfigError,
    DemoSafetySummary,
    _compose_demo_connection,
    build_demo_connection,
    evaluate_demo_activation,
    load_demo_safety_policy,
    parse_expected_account_reference,
)
from execution.demo_startup_cli import main
from execution.kill_switch_store import PersistentKillSwitchStateProvider, SQLiteKillSwitchStore
from execution.safety_gate import ExecutionMode, SafetyPolicy

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
TOKEN = "demo-token-SECRET"
ACCOUNT = "101-000-DEMOACCOUNT-001"
ACCOUNT_REFERENCE = safe_account_reference(ACCOUNT)
LIVE_TOKEN = "live-token-SECRET"
LIVE_ACCOUNT = "001-004-LIVEACCOUNT-001"


def poisoned_transport(*args, **kwargs):  # pragma: no cover - must never run
    raise AssertionError("the demo lock must not perform network I/O")


def demo_env(**overrides) -> dict:
    """A COMPLETE demo environment. Overrides may remove a variable with
    ``None`` or point it somewhere invalid."""
    env = {ENV_API_TOKEN: TOKEN, ENV_ACCOUNT_ID: ACCOUNT}
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def write_safety_config(tmp_path, **overrides):
    document = {
        "policy_id": "demo-v1",
        "allowed_symbols": ["USD_JPY"],
        "allowed_sides": ["buy"],
        "max_order_quantity": "10",
        "max_positions_per_symbol": 2,
        "max_positions_account": 5,
        "max_reference_age_seconds": 30,
        "max_spread_by_symbol": {"USD_JPY": "0.05"},
        "allowed_demo_targets": [OANDA_DEMO_CONNECTION_TARGET],
        "expected_account_reference": ACCOUNT_REFERENCE,
        "require_stop_loss": True,
    }
    document.update(overrides)
    path = tmp_path / "demo_safety.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def kill_switch_db(tmp_path, *, enabled=False):
    path = tmp_path / "kill_switch.sqlite3"
    store = SQLiteKillSwitchStore(path)
    store.initialize_and_record(
        enabled=True, operator="ops", reason="initial halt", changed_at_utc=NOW
    )
    if not enabled:
        store.record_transition(
            enabled=False,
            operator="ops",
            reason="ready for demo",
            changed_at_utc=NOW + timedelta(minutes=1),
        )
    return path


def evaluate(tmp_path, *, execution_mode="demo", env=None, policy=None, pin=..., state=None):
    if policy is ...:  # pragma: no cover - defensive, never used
        policy = None
    if policy is None:
        policy = load_demo_safety_policy(write_safety_config(tmp_path))
    if pin is ...:
        pin = ACCOUNT_REFERENCE
    return evaluate_demo_activation(
        execution_mode=execution_mode,
        environ=demo_env() if env is None else env,
        policy=policy,
        expected_account_reference=pin,
        state_provider=state
        if state is not None
        else PersistentKillSwitchStateProvider(kill_switch_db(tmp_path)),
    )


def codes(decision) -> set[str]:
    return set(decision.rejection_codes)


def compose(tmp_path, monkeypatch, *, execution_mode="demo", env=None, config_path=None,
            kill_switch=None):
    """Compose through the PRIVATE seam so no socket is ever opened."""
    monkeypatch.setattr("os.environ", demo_env() if env is None else env)
    return _compose_demo_connection(
        execution_mode=execution_mode,
        safety_config_path=config_path
        if config_path is not None
        else write_safety_config(tmp_path),
        kill_switch_db_path=kill_switch if kill_switch is not None else kill_switch_db(tmp_path),
        transport=poisoned_transport,
        clock=lambda: NOW,
    )


# --- the locks ------------------------------------------------------------------


def test_a_complete_demo_configuration_satisfies_every_lock(tmp_path):
    decision = evaluate(tmp_path)
    assert decision.allowed is True
    assert decision.rejections == ()
    assert decision.summary.demo_confirmed is True
    assert all(satisfied for _, satisfied in decision.summary.locks)


def test_the_checklist_shows_all_seven_locks(tmp_path):
    """PR #136 Codex review P2: the two locks that most often refuse a
    startup -- the safety policy and the Kill Switch -- were visible only as
    rejection codes, never on the checklist an operator reads."""
    decision = evaluate(tmp_path)
    assert dict(decision.summary.locks) == {
        "execution_mode=demo": True,
        "demo credentials configured": True,
        "demo endpoint allowlisted": True,
        "no live configuration present": True,
        "demo account matches the pinned digest": True,
        "safety policy allows the demo target": True,
        "kill switch verified OFF": True,
    }
    rendered = decision.summary.render()
    assert "[x] safety policy allows the demo target" in rendered
    assert "[x] kill switch verified OFF" in rendered


@pytest.mark.parametrize(
    ("kwargs", "unsatisfied_lock"),
    [
        pytest.param({"execution_mode": "fake"}, "execution_mode=demo", id="mode"),
        pytest.param(
            {"env": {ENV_ACCOUNT_ID: ACCOUNT}}, "demo credentials configured", id="credentials"
        ),
        pytest.param(
            {"env": demo_env(**{ENV_API_URL: OANDA_LIVE_API_URL})},
            "demo endpoint allowlisted",
            id="endpoint",
        ),
        pytest.param(
            {"env": demo_env(**{ENV_LIVE_API_TOKEN: LIVE_TOKEN})},
            "no live configuration present",
            id="live-config",
        ),
        pytest.param(
            {"env": demo_env(**{ENV_ACCOUNT_ID: "101-000-OTHER-002"})},
            "demo account matches the pinned digest",
            id="account",
        ),
        pytest.param({"policy": None, "pin": None}, "safety policy allows the demo target",
                     id="policy-unavailable"),
    ],
)
def test_a_refused_startup_never_reports_demo_confirmed(tmp_path, kwargs, unsatisfied_lock):
    """PR #136 Codex review P2: ``demo_confirmed`` is bound to the FINAL
    decision, so the machine-readable summary can never contradict its own
    ``allowed`` field -- including for refusals the lock checklist does not
    enumerate."""
    decision = evaluate(tmp_path, **kwargs)
    assert decision.allowed is False
    assert decision.summary.demo_confirmed is False
    assert dict(decision.summary.locks)[unsatisfied_lock] is False
    assert decision.summary.to_dict()["demo_confirmed"] is False


@pytest.mark.parametrize(
    "state_kind", ["enabled", "unverifiable", "non-boolean", "missing"]
)
def test_a_kill_switch_refusal_never_reports_demo_confirmed(tmp_path, state_kind):
    """The refusal Codex's finding was about: every other lock holds, so a
    checklist-derived ``demo_confirmed`` answered "true" for a startup that
    was refused because trading is halted."""

    class Broken:
        def is_kill_switch_enabled(self):
            raise RuntimeError("store is gone")

    class Liar:
        def is_kill_switch_enabled(self):
            return "no"

    state = {
        "enabled": lambda: PersistentKillSwitchStateProvider(
            kill_switch_db(tmp_path, enabled=True)
        ),
        "unverifiable": Broken,
        "non-boolean": Liar,
        "missing": lambda: PersistentKillSwitchStateProvider(tmp_path / "absent.sqlite3"),
    }[state_kind]()
    decision = evaluate(tmp_path, state=state)
    assert decision.allowed is False
    assert decision.summary.demo_confirmed is False
    assert dict(decision.summary.locks)["kill switch verified OFF"] is False
    assert decision.summary.to_dict()["demo_confirmed"] is False


@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("fake", DemoLockCode.EXECUTION_MODE_NOT_DEMO),
        ("shadow", DemoLockCode.EXECUTION_MODE_NOT_DEMO),
        ("live", DemoLockCode.EXECUTION_MODE_NOT_DEMO),
        ("dry-run", DemoLockCode.EXECUTION_MODE_NOT_DEMO),
    ],
)
def test_only_the_demo_mode_can_produce_a_demo_connection(tmp_path, mode, expected_code):
    decision = evaluate(tmp_path, execution_mode=mode)
    assert decision.allowed is False
    assert expected_code.value in codes(decision)


def test_an_unknown_execution_mode_value_is_not_echoed(tmp_path):
    decision = evaluate(tmp_path, execution_mode="SECRET-MODE-VALUE")
    assert decision.allowed is False
    assert DemoLockCode.EXECUTION_MODE_NOT_DEMO.value in codes(decision)
    rendered = " ".join(entry.message for entry in decision.rejections)
    assert "SECRET-MODE-VALUE" not in rendered
    assert decision.summary.execution_mode == "invalid"


@pytest.mark.parametrize("missing", [ENV_API_TOKEN, ENV_ACCOUNT_ID])
def test_missing_demo_credentials_fail_closed(tmp_path, missing):
    decision = evaluate(tmp_path, env=demo_env(**{missing: None}))
    assert decision.allowed is False
    assert DemoLockCode.DEMO_CREDENTIALS_MISSING.value in codes(decision)


def test_the_unset_endpoint_variable_is_the_demo_endpoint(tmp_path):
    """``OANDA_API_URL`` defaults to the practice endpoint (Issue #79), so an
    unset variable satisfies the endpoint lock -- it does not bypass it."""
    decision = evaluate(tmp_path, env=demo_env())
    assert decision.allowed is True
    assert decision.summary.endpoint_kind == "demo"


@pytest.mark.parametrize(
    "endpoint",
    [
        OANDA_LIVE_API_URL,
        "https://stream-fxtrade.oanda.com",
        "https://api-fxpractice.oanda.com.evil.example",
        "http://api-fxpractice.oanda.com",
        "https://example.com",
    ],
)
def test_every_non_demo_endpoint_is_refused_fail_closed(tmp_path, endpoint):
    decision = evaluate(tmp_path, env=demo_env(**{ENV_API_URL: endpoint}))
    assert decision.allowed is False
    refused = codes(decision)
    assert (
        DemoLockCode.DEMO_ENDPOINT_INVALID.value in refused
        or DemoLockCode.DEMO_LIVE_CONFIGURATION_MIXED.value in refused
    )
    rendered = " ".join(entry.message for entry in decision.rejections)
    assert endpoint not in rendered


def test_the_live_endpoint_in_the_demo_slot_is_refused_twice(tmp_path):
    """A live URL in the demo endpoint variable trips BOTH the allowlist and
    the demo/live separation check -- the value is never echoed."""
    decision = evaluate(tmp_path, env=demo_env(**{ENV_API_URL: OANDA_LIVE_API_URL}))
    assert codes(decision) >= {
        DemoLockCode.DEMO_ENDPOINT_INVALID.value,
        DemoLockCode.DEMO_LIVE_CONFIGURATION_MIXED.value,
    }


@pytest.mark.parametrize(
    "live_var, value",
    [
        (ENV_ENABLE_LIVE_TRADING, ENABLE_LIVE_TRADING_TRUE),
        (ENV_LIVE_API_TOKEN, LIVE_TOKEN),
        (ENV_LIVE_ACCOUNT_ID, LIVE_ACCOUNT),
        (ENV_LIVE_API_URL, OANDA_LIVE_API_URL),
        # PR #136 Codex review P2: the live TIMEOUT variable carries no
        # secret and cannot reach a live endpoint on its own, but the lock
        # promises "no live configuration present" -- a process holding a
        # leftover live tuning variable had an environment meant for live,
        # and must not pass that lock.
        (ENV_LIVE_HTTP_TIMEOUT_SECONDS, "10"),
    ],
)
def test_a_demo_process_may_not_carry_any_live_configuration(tmp_path, live_var, value):
    decision = evaluate(tmp_path, env=demo_env(**{live_var: value}))
    assert decision.allowed is False
    assert DemoLockCode.DEMO_LIVE_CONFIGURATION_MIXED.value in codes(decision)
    assert dict(decision.summary.locks)["no live configuration present"] is False


def test_the_live_only_variable_list_covers_every_live_adapter_variable():
    """PR #136 Codex review P2: the separation lock is only as complete as
    this list, and the list is hand-maintained. Adding a variable to the live
    adapter without adding it here would silently re-open the gap, so the
    coverage is pinned against the adapter module's own exports rather than
    against a second hand-written list."""
    from broker import oanda_live_adapter as live_adapter
    from execution.demo_lock import _LIVE_ONLY_ENV_VARS

    exported = {
        getattr(live_adapter, name)
        for name in dir(live_adapter)
        if name.startswith("ENV_") and isinstance(getattr(live_adapter, name), str)
    }
    missing = exported - set(_LIVE_ONLY_ENV_VARS)
    assert missing == set(), (
        "every environment variable the live adapter reads must be refused in a demo "
        f"process; missing from _LIVE_ONLY_ENV_VARS: {sorted(missing)}"
    )


@pytest.mark.parametrize(
    "shared", [(ENV_LIVE_API_TOKEN, ENV_API_TOKEN, TOKEN), (ENV_LIVE_ACCOUNT_ID, ENV_ACCOUNT_ID, ACCOUNT)]
)
def test_credentials_shared_between_demo_and_live_are_refused(tmp_path, shared):
    live_var, _demo_var, value = shared
    decision = evaluate(tmp_path, env=demo_env(**{live_var: value}))
    assert decision.allowed is False
    assert DemoLockCode.DEMO_LIVE_CONFIGURATION_MIXED.value in codes(decision)


def test_a_different_demo_account_is_refused_before_any_broker_contact(tmp_path):
    """The ACCOUNT identity lock: the process is configured for a demo
    account the reviewed configuration does not pin."""
    decision = evaluate(tmp_path, env=demo_env(**{ENV_ACCOUNT_ID: "101-000-OTHERACCOUNT-002"}))
    assert decision.allowed is False
    assert DemoLockCode.DEMO_ACCOUNT_MISMATCH.value in codes(decision)
    rendered = " ".join(entry.message for entry in decision.rejections)
    assert "101-000-OTHERACCOUNT-002" not in rendered
    assert ACCOUNT not in rendered


def test_the_summary_shows_the_digest_an_operator_must_pin(tmp_path):
    decision = evaluate(tmp_path, env=demo_env(**{ENV_ACCOUNT_ID: "101-000-OTHERACCOUNT-002"}))
    # Refused, but the digest of the CONFIGURED account is still shown: that
    # is how an operator bootstraps ``expected_account_reference``.
    assert decision.summary.account_reference == safe_account_reference(
        "101-000-OTHERACCOUNT-002"
    )
    assert decision.summary.expected_account_reference == ACCOUNT_REFERENCE


def test_an_unavailable_safety_policy_refuses_the_startup(tmp_path):
    decision = evaluate(tmp_path, policy=None, pin=None)
    assert decision.allowed is False
    assert DemoLockCode.SAFETY_POLICY_UNAVAILABLE.value in codes(decision)


def test_a_policy_that_does_not_allow_the_demo_target_refuses_the_startup(tmp_path):
    policy = load_demo_safety_policy(
        write_safety_config(tmp_path, allowed_demo_targets=["some-other-target"])
    )
    decision = evaluate(tmp_path, policy=policy)
    assert decision.allowed is False
    assert DemoLockCode.DEMO_TARGET_NOT_ALLOWED.value in codes(decision)
    assert decision.summary.demo_confirmed is False
    assert dict(decision.summary.locks)["safety policy allows the demo target"] is False


def test_a_non_policy_object_is_refused(tmp_path):
    decision = evaluate(tmp_path, policy="not-a-policy")
    assert decision.allowed is False
    assert DemoLockCode.SAFETY_POLICY_UNAVAILABLE.value in codes(decision)


def test_the_kill_switch_blocks_a_demo_startup(tmp_path):
    decision = evaluate(
        tmp_path,
        state=PersistentKillSwitchStateProvider(kill_switch_db(tmp_path, enabled=True)),
    )
    assert decision.allowed is False
    assert DemoLockCode.KILL_SWITCH_ENABLED.value in codes(decision)
    assert decision.summary.kill_switch == "enabled"


def test_an_unverifiable_kill_switch_blocks_a_demo_startup(tmp_path):
    class Broken:
        def is_kill_switch_enabled(self):
            raise RuntimeError("store is gone")

    decision = evaluate(tmp_path, state=Broken())
    assert decision.allowed is False
    assert DemoLockCode.KILL_SWITCH_STATE_UNVERIFIABLE.value in codes(decision)
    assert decision.summary.kill_switch == "unverifiable"


def test_a_missing_kill_switch_store_never_reads_as_off(tmp_path):
    decision = evaluate(
        tmp_path,
        state=PersistentKillSwitchStateProvider(tmp_path / "absent.sqlite3"),
    )
    assert decision.allowed is False
    assert DemoLockCode.KILL_SWITCH_STATE_UNVERIFIABLE.value in codes(decision)


def test_a_non_boolean_kill_switch_answer_never_reads_as_off(tmp_path):
    class Liar:
        def is_kill_switch_enabled(self):
            return "no"

    decision = evaluate(tmp_path, state=Liar())
    assert decision.allowed is False
    assert DemoLockCode.KILL_SWITCH_STATE_UNVERIFIABLE.value in codes(decision)


def test_every_violation_is_collected_not_just_the_first(tmp_path):
    decision = evaluate(
        tmp_path,
        execution_mode="fake",
        env={ENV_API_URL: OANDA_LIVE_API_URL, ENV_ENABLE_LIVE_TRADING: ENABLE_LIVE_TRADING_TRUE},
        state=PersistentKillSwitchStateProvider(kill_switch_db(tmp_path, enabled=True)),
    )
    assert codes(decision) >= {
        DemoLockCode.EXECUTION_MODE_NOT_DEMO.value,
        DemoLockCode.DEMO_CREDENTIALS_MISSING.value,
        DemoLockCode.DEMO_ENDPOINT_INVALID.value,
        DemoLockCode.DEMO_LIVE_CONFIGURATION_MIXED.value,
        DemoLockCode.DEMO_ACCOUNT_MISMATCH.value,
        DemoLockCode.KILL_SWITCH_ENABLED.value,
    }


# --- the safety configuration ---------------------------------------------------


def test_the_safety_configuration_round_trips_into_a_policy(tmp_path):
    loaded = load_demo_safety_policy(write_safety_config(tmp_path))
    assert loaded.policy_id == "demo-v1"
    assert loaded.max_order_quantity == Decimal("10")
    assert loaded.allowed_connection_targets[ExecutionMode.DEMO] == frozenset(
        {OANDA_DEMO_CONNECTION_TARGET}
    )
    # The demo document produces a DEMO-only policy: it can never authorize a
    # live target, whatever an operator writes into it.
    assert ExecutionMode.LIVE not in loaded.allowed_connection_targets


def test_a_missing_safety_configuration_file_fails_closed(tmp_path):
    with pytest.raises(DemoSafetyConfigError):
        load_demo_safety_policy(tmp_path / "absent.json")


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"allowed_symbols": []}, id="no-symbols"),
        pytest.param({"max_order_quantity": "0"}, id="zero-quantity"),
        pytest.param({"allowed_sides": ["sideways"]}, id="unknown-side"),
        pytest.param({"allowed_demo_targets": []}, id="no-demo-target"),
        pytest.param({"require_stop_loss": "yes"}, id="non-boolean"),
        pytest.param({"surprise": 1}, id="unknown-key"),
        pytest.param({"allowed_live_targets": ["oanda-live"]}, id="live-targets-are-unknown-here"),
        pytest.param({"expected_account_reference": ACCOUNT}, id="raw-account-id"),
        pytest.param({"expected_account_reference": "account-XYZ"}, id="malformed-digest"),
    ],
)
def test_an_invalid_safety_configuration_fails_closed(tmp_path, overrides):
    with pytest.raises(DemoSafetyConfigError):
        load_demo_safety_policy(write_safety_config(tmp_path, **overrides))


def test_a_safety_configuration_missing_a_field_fails_closed(tmp_path):
    document = json.loads(write_safety_config(tmp_path).read_text(encoding="utf-8"))
    for key in list(document):
        partial = dict(document)
        del partial[key]
        path = tmp_path / "partial.json"
        path.write_text(json.dumps(partial), encoding="utf-8")
        with pytest.raises(DemoSafetyConfigError):
            load_demo_safety_policy(path)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"allowed_sides": ["SECRET-VALUE"]}, id="allowed_sides"),
        pytest.param({"max_order_quantity": "SECRET-VALUE"}, id="max_order_quantity"),
        pytest.param({"allowed_demo_targets": [" "]}, id="allowed_demo_targets"),
        pytest.param({"allowed_symbols": ["SECRET-VALUE"]}, id="allowed_symbols"),
        pytest.param({"expected_account_reference": "SECRET-VALUE"}, id="account-pin"),
        pytest.param({"SECRET-AS-A-KEY": "x"}, id="unknown-key"),
    ],
)
def test_an_invalid_safety_configuration_never_echoes_the_value(tmp_path, overrides):
    path = write_safety_config(tmp_path, **overrides)
    with pytest.raises(DemoSafetyConfigError) as exc_info:
        load_demo_safety_policy(path)
    assert "SECRET" not in str(exc_info.value), str(exc_info.value)


def test_the_account_pin_must_be_a_digest_not_an_account_id():
    with pytest.raises(DemoSafetyConfigError) as exc_info:
        parse_expected_account_reference({"expected_account_reference": ACCOUNT})
    assert ACCOUNT not in str(exc_info.value)
    assert parse_expected_account_reference(
        {"expected_account_reference": f" {ACCOUNT_REFERENCE} "}
    ) == ACCOUNT_REFERENCE


# --- composition ----------------------------------------------------------------


def test_a_complete_configuration_composes_a_demo_connection(tmp_path, monkeypatch):
    decision, grant = compose(tmp_path, monkeypatch)
    assert decision.allowed is True
    assert isinstance(grant, DemoRuntimeGrant)
    assert grant.connection.execution_mode is ExecutionMode.DEMO
    assert grant.connection.connection_target == OANDA_DEMO_CONNECTION_TARGET
    assert type(grant.connection.broker) is OandaDemoAdapter
    assert grant.policy.policy_id == "demo-v1"


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"execution_mode": "live"}, id="wrong-mode"),
        pytest.param({"env": demo_env(**{ENV_API_URL: OANDA_LIVE_API_URL})}, id="live-endpoint"),
        pytest.param({"env": demo_env(**{ENV_API_TOKEN: None})}, id="missing-token"),
        pytest.param(
            {"env": demo_env(**{ENV_ENABLE_LIVE_TRADING: ENABLE_LIVE_TRADING_TRUE})},
            id="live-flag-present",
        ),
        pytest.param({"env": demo_env(**{ENV_ACCOUNT_ID: "101-000-OTHER-002"})}, id="other-account"),
    ],
)
def test_a_refused_startup_composes_no_connection(tmp_path, monkeypatch, kwargs):
    decision, grant = compose(tmp_path, monkeypatch, **kwargs)
    assert decision.allowed is False
    assert grant is None


def test_the_kill_switch_prevents_the_connection_from_being_composed(tmp_path, monkeypatch):
    decision, grant = compose(
        tmp_path, monkeypatch, kill_switch=kill_switch_db(tmp_path, enabled=True)
    )
    assert decision.allowed is False
    assert grant is None
    assert DemoLockCode.KILL_SWITCH_ENABLED.value in codes(decision)


def test_an_unreadable_safety_configuration_composes_no_connection(tmp_path, monkeypatch):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    decision, grant = compose(tmp_path, monkeypatch, config_path=path)
    assert decision.allowed is False
    assert grant is None
    assert DemoLockCode.SAFETY_POLICY_UNAVAILABLE.value in codes(decision)


def test_the_public_composer_never_accepts_a_transport(tmp_path):
    """The endpoint allowlist only means something if the composed
    connection actually talks to the allowlisted endpoint (the rule PR #128
    established for live): a caller-supplied transport would receive the demo
    Authorization header."""
    import inspect

    parameters = inspect.signature(build_demo_connection).parameters
    assert "transport" not in parameters
    assert "clock" not in parameters
    assert "policy" not in parameters
    assert "state_provider" not in parameters


def test_the_grant_refuses_a_policy_that_does_not_allow_its_own_target(tmp_path, monkeypatch):
    _decision, grant = compose(tmp_path, monkeypatch)
    other = SafetyPolicy(
        policy_id="other",
        allowed_symbols=frozenset({"USD_JPY"}),
        allowed_sides=frozenset(grant.policy.allowed_sides),
        max_order_quantity=Decimal("1"),
        max_positions_per_symbol=1,
        max_positions_account=1,
        max_reference_age=grant.policy.max_reference_age,
        max_spread_by_symbol={"USD_JPY": Decimal("0.05")},
        allowed_connection_targets={ExecutionMode.DEMO: frozenset({"somewhere-else"})},
    )
    with pytest.raises(ValueError, match="does not allow"):
        DemoRuntimeGrant(
            connection=grant.connection,
            policy=other,
            kill_switch_db_path=grant.kill_switch_db_path,
        )


def test_the_grant_refuses_a_non_demo_connection(tmp_path):
    from test_safety_gate import connection as fake_connection

    with pytest.raises(ValueError, match="requires a 'demo' connection"):
        DemoRuntimeGrant(
            connection=fake_connection(),
            policy=load_demo_safety_policy(write_safety_config(tmp_path)),
            kill_switch_db_path=tmp_path / "kill_switch.sqlite3",
        )


def test_the_grant_refuses_a_kill_switch_provider_object(tmp_path, monkeypatch):
    _decision, grant = compose(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="addressed by path"):
        DemoRuntimeGrant(
            connection=grant.connection,
            policy=grant.policy,
            kill_switch_db_path=PersistentKillSwitchStateProvider(grant.kill_switch_db_path),
        )


# --- secrets --------------------------------------------------------------------


def test_no_secret_ever_reaches_a_summary_or_a_rejection(tmp_path, monkeypatch):
    env = demo_env(**{ENV_LIVE_API_TOKEN: TOKEN, ENV_API_URL: OANDA_LIVE_API_URL})
    decision, _grant = compose(tmp_path, monkeypatch, env=env)
    blob = " ".join(
        [decision.summary.render(), json.dumps(decision.summary.to_dict()), repr(decision.summary)]
        + [entry.message for entry in decision.rejections]
    )
    for secret in (TOKEN, ACCOUNT, OANDA_DEMO_API_URL, OANDA_LIVE_API_URL):
        assert secret not in blob, secret


def test_the_summary_never_prints_a_symbol_it_cannot_recognise():
    from execution.safety_config import displayable_symbols

    summary = DemoSafetySummary(
        execution_mode="demo",
        endpoint_kind="demo",
        account_reference=None,
        expected_account_reference=None,
        allowed_symbols=displayable_symbols(["USD_JPY", "demo-token-SECRET"]),
        max_order_quantity=None,
        max_positions_per_symbol=None,
        max_positions_account=None,
        kill_switch="disabled",
        demo_confirmed=True,
        locks=(),
        rejection_codes=(),
    )
    assert summary.allowed_symbols == ("USD_JPY", "<unprintable>")
    assert "demo-token-SECRET" not in summary.render()
    assert "demo-token-SECRET" not in json.dumps(summary.to_dict())


# --- the preflight CLI ----------------------------------------------------------


def cli(tmp_path, monkeypatch, *argv, env=None, config_path=None, kill_switch=None):
    monkeypatch.setattr("os.environ", demo_env() if env is None else env)
    return main(
        [
            "--execution-mode",
            "demo",
            "--safety-config",
            str(config_path if config_path is not None else write_safety_config(tmp_path)),
            "--kill-switch-db",
            str(kill_switch if kill_switch is not None else kill_switch_db(tmp_path)),
            *argv,
        ]
    )


def test_the_cli_reports_a_satisfied_demo_startup(tmp_path, monkeypatch, capsys):
    assert cli(tmp_path, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "ALL LOCKS SATISFIED" in out
    assert "real-account LIVE trading is NOT released" in out
    assert TOKEN not in out and ACCOUNT not in out


def test_the_cli_prints_the_account_digest_to_pin(tmp_path, monkeypatch, capsys):
    assert cli(tmp_path, monkeypatch, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["safety_summary"]["account_reference"] == ACCOUNT_REFERENCE
    assert payload["safety_summary"]["expected_account_reference"] == ACCOUNT_REFERENCE


def test_the_cli_fails_closed_on_a_refused_startup(tmp_path, monkeypatch, capsys):
    assert cli(tmp_path, monkeypatch, kill_switch=kill_switch_db(tmp_path, enabled=True)) == 1
    captured = capsys.readouterr()
    assert DemoLockCode.KILL_SWITCH_ENABLED.value in captured.err
    assert "[ ] kill switch verified OFF" in captured.out


def test_the_cli_json_never_reports_a_refused_startup_as_confirmed(
    tmp_path, monkeypatch, capsys
):
    """PR #136 Codex review P2: the machine-readable preflight output must
    not answer ``demo_confirmed: true`` next to ``allowed: false``."""
    assert (
        cli(tmp_path, monkeypatch, "--json", kill_switch=kill_switch_db(tmp_path, enabled=True))
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["allowed"] is False
    assert payload["safety_summary"]["demo_confirmed"] is False
    assert payload["safety_summary"]["locks"]["kill switch verified OFF"] is False


def test_the_cli_refuses_a_live_mode_and_never_composes_anything(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("os.environ", demo_env())
    code = main(
        [
            "--execution-mode",
            "live",
            "--safety-config",
            str(write_safety_config(tmp_path)),
            "--kill-switch-db",
            str(kill_switch_db(tmp_path)),
            "--json",
        ]
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert DemoLockCode.EXECUTION_MODE_NOT_DEMO.value in payload["rejection_codes"]


def test_the_cli_usage_error_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr("os.environ", demo_env())
    assert main(["--execution-mode", "demo"]) == 2


def test_the_cli_never_creates_the_kill_switch_db(tmp_path, monkeypatch, capsys):
    absent = tmp_path / "never_created.sqlite3"
    assert cli(tmp_path, monkeypatch, kill_switch=absent) == 1
    assert not absent.exists()
    assert DemoLockCode.KILL_SWITCH_STATE_UNVERIFIABLE.value in capsys.readouterr().err
