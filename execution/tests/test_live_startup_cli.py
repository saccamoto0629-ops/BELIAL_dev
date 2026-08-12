"""Issue #82: the ``--confirm-live`` startup CLI, exercised without a network.

The CLI never performs HTTP I/O: composing the live connection is a pure
construction step. To prove that, every test additionally asserts that the
adapter's default ``requests`` transport was never even imported into use --
the module-level ``requests`` import inside the default transport would
raise here if it ran, and no test provides a reachable endpoint anyway.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from broker.oanda_demo_adapter import OANDA_DEMO_API_URL
from broker.oanda_live_adapter import (
    ENABLE_LIVE_TRADING_TRUE,
    ENV_ENABLE_LIVE_TRADING,
    ENV_LIVE_ACCOUNT_ID,
    ENV_LIVE_API_TOKEN,
    ENV_LIVE_API_URL,
    ENV_LIVE_HTTP_TIMEOUT_SECONDS,
    OANDA_LIVE_API_URL,
)
from execution.kill_switch_store import SQLiteKillSwitchStore
from execution.live_startup_cli import EXIT_OK, EXIT_REFUSED, EXIT_USAGE_ERROR, main

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
TOKEN = "live-token-SECRET"
ACCOUNT = "101-004-LIVEACCOUNT-001"

SAFETY_CONFIG = {
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


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """No test inherits a real credential from the developer's shell."""
    for name in (
        ENV_ENABLE_LIVE_TRADING,
        ENV_LIVE_API_TOKEN,
        ENV_LIVE_ACCOUNT_ID,
        ENV_LIVE_API_URL,
        # an inherited timeout would change what the issuer seals, so the
        # suite must not depend on the developer's or CI's shell for it
        # (PR #128 Codex review r4 P2)
        ENV_LIVE_HTTP_TIMEOUT_SECONDS,
        "OANDA_API_TOKEN",
        "OANDA_ACCOUNT_ID",
        "OANDA_API_URL",
        "KILL_SWITCH_DB_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any attempt to open a socket in this suite is a test failure."""
    import socket

    def refuse(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the live startup CLI must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def safety_config(tmp_path, **overrides):
    document = dict(SAFETY_CONFIG)
    document.update(overrides)
    path = tmp_path / "live_safety.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def released_kill_switch(tmp_path):
    """A kill switch store whose VERIFIED state is off (enable then
    disable: the store is never created empty)."""
    db_path = tmp_path / "kill_switch.sqlite3"
    store = SQLiteKillSwitchStore(db_path)
    store.initialize_and_record(
        enabled=True, operator="ops", reason="initial halt", changed_at_utc=NOW
    )
    store.record_transition(
        enabled=False,
        operator="ops",
        reason="live rehearsal complete",
        changed_at_utc=NOW + timedelta(minutes=1),
    )
    return db_path


def set_live_env(monkeypatch, **overrides):
    values = {
        ENV_ENABLE_LIVE_TRADING: ENABLE_LIVE_TRADING_TRUE,
        ENV_LIVE_API_TOKEN: TOKEN,
        ENV_LIVE_ACCOUNT_ID: ACCOUNT,
        ENV_LIVE_API_URL: OANDA_LIVE_API_URL,
    }
    values.update(overrides)
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def run(tmp_path, *extra, config_path=None, kill_switch_db=None, mode="live"):
    argv = [
        "--execution-mode",
        mode,
        "--safety-config",
        str(config_path if config_path is not None else safety_config(tmp_path)),
        "--kill-switch-db",
        str(kill_switch_db if kill_switch_db is not None else released_kill_switch(tmp_path)),
    ]
    argv.extend(extra)
    return main(argv)


# --- exit-code contract ---------------------------------------------------------


def test_every_lock_satisfied_exits_zero(tmp_path, monkeypatch, capsys):
    set_live_env(monkeypatch)
    assert run(tmp_path, "--confirm-live") == EXIT_OK
    out = capsys.readouterr().out
    assert "ALL LOCKS SATISFIED" in out
    assert "live safety summary" in out


def test_missing_confirm_live_is_refused(tmp_path, monkeypatch, capsys):
    set_live_env(monkeypatch)
    assert run(tmp_path) == EXIT_REFUSED
    assert "live_confirmation_missing" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        pytest.param({ENV_ENABLE_LIVE_TRADING: None}, "live_trading_disabled", id="flag-unset"),
        pytest.param({ENV_ENABLE_LIVE_TRADING: "TRUE"}, "live_trading_disabled", id="flag-case"),
        pytest.param({ENV_LIVE_ACCOUNT_ID: None}, "live_credentials_missing", id="no-account"),
        pytest.param({ENV_LIVE_API_TOKEN: None}, "live_credentials_missing", id="no-token"),
        pytest.param({ENV_LIVE_API_URL: None}, "live_endpoint_invalid", id="no-endpoint"),
        pytest.param(
            {ENV_LIVE_API_URL: OANDA_DEMO_API_URL}, "live_endpoint_invalid", id="demo-endpoint"
        ),
        pytest.param(
            {ENV_LIVE_API_URL: "https://example.invalid"},
            "live_endpoint_invalid",
            id="unknown-endpoint",
        ),
        pytest.param(
            {"OANDA_ACCOUNT_ID": ACCOUNT},
            "demo_live_configuration_mixed",
            id="shared-account",
        ),
        pytest.param(
            {"OANDA_API_TOKEN": TOKEN}, "demo_live_configuration_mixed", id="shared-token"
        ),
    ],
)
def test_each_broken_configuration_is_refused(
    tmp_path, monkeypatch, capsys, overrides, expected_code
):
    set_live_env(monkeypatch, **overrides)
    assert run(tmp_path, "--confirm-live") == EXIT_REFUSED
    assert expected_code in capsys.readouterr().err


def test_a_non_live_execution_mode_is_refused(tmp_path, monkeypatch, capsys):
    set_live_env(monkeypatch)
    assert run(tmp_path, "--confirm-live", mode="shadow") == EXIT_REFUSED
    assert "execution_mode_not_live" in capsys.readouterr().err


def test_an_unknown_execution_mode_is_refused(tmp_path, monkeypatch, capsys):
    set_live_env(monkeypatch)
    assert run(tmp_path, "--confirm-live", mode="produxion") == EXIT_REFUSED
    assert "execution_mode_not_live" in capsys.readouterr().err


def test_an_enabled_kill_switch_refuses_the_startup(tmp_path, monkeypatch, capsys):
    set_live_env(monkeypatch)
    db_path = released_kill_switch(tmp_path)
    SQLiteKillSwitchStore(db_path).record_transition(
        enabled=True,
        operator="ops",
        reason="emergency stop",
        changed_at_utc=NOW + timedelta(minutes=2),
    )
    assert run(tmp_path, "--confirm-live", kill_switch_db=db_path) == EXIT_REFUSED
    assert "kill_switch_enabled" in capsys.readouterr().err


def test_a_missing_kill_switch_store_refuses_and_is_not_created(tmp_path, monkeypatch, capsys):
    set_live_env(monkeypatch)
    db_path = tmp_path / "absent_kill_switch.sqlite3"
    assert run(tmp_path, "--confirm-live", kill_switch_db=db_path) == EXIT_REFUSED
    assert "kill_switch_state_unverifiable" in capsys.readouterr().err
    assert not db_path.exists()


def test_an_unreadable_safety_configuration_refuses_the_startup(tmp_path, monkeypatch, capsys):
    set_live_env(monkeypatch)
    missing = tmp_path / "absent.json"
    assert run(tmp_path, "--confirm-live", config_path=missing) == EXIT_REFUSED
    captured = capsys.readouterr()
    assert "safety_policy_unavailable" in captured.err
    assert "safety configuration error" in captured.out


def test_a_policy_that_does_not_allow_the_live_target_refuses(tmp_path, monkeypatch, capsys):
    set_live_env(monkeypatch)
    path = safety_config(tmp_path, allowed_live_targets=["somewhere-else"])
    assert run(tmp_path, "--confirm-live", config_path=path) == EXIT_REFUSED
    assert "live_target_not_allowed" in capsys.readouterr().err


def test_missing_required_arguments_are_a_usage_error(capsys):
    assert main([]) == EXIT_USAGE_ERROR
    assert main(["--execution-mode", "live"]) == EXIT_USAGE_ERROR


# --- output ---------------------------------------------------------------------


def test_json_output_is_pure_ascii_and_machine_readable(tmp_path, monkeypatch, capsys):
    set_live_env(monkeypatch)
    assert run(tmp_path, "--confirm-live", "--json") == EXIT_OK
    out = capsys.readouterr().out
    out.encode("ascii")  # raises if a non-ASCII character slipped in
    payload = json.loads(out)
    assert payload["allowed"] is True
    assert payload["rejection_codes"] == []
    summary = payload["safety_summary"]
    assert summary["execution_mode"] == "live"
    assert summary["endpoint_kind"] == "live"
    assert summary["kill_switch"] == "disabled"
    assert summary["allowed_symbols"] == ["USD_JPY"]
    assert summary["max_order_quantity"] == "10"
    assert summary["max_positions_per_symbol"] == 2
    assert summary["max_positions_account"] == 5
    assert summary["live_confirmed"] is True
    assert all(summary["locks"].values())


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_no_output_stream_ever_carries_a_secret(tmp_path, monkeypatch, capsys, json_flag):
    """Both the allowed and the refused paths, both output formats."""
    set_live_env(monkeypatch)
    for extra in (["--confirm-live", *json_flag], [*json_flag]):
        run(tmp_path, *extra)
        captured = capsys.readouterr()
        for stream in (captured.out, captured.err):
            assert TOKEN not in stream
            assert ACCOUNT not in stream
            assert OANDA_LIVE_API_URL not in stream
            assert "api-fxtrade" not in stream
            assert "Bearer" not in stream
            assert "Authorization" not in stream


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"policy_id": ""}, id="policy_id"),
        pytest.param({"allowed_sides": ["PASTED-CREDENTIAL"]}, id="allowed_sides"),
        pytest.param({"max_order_quantity": "PASTED-CREDENTIAL"}, id="max_order_quantity"),
        pytest.param(
            {"max_spread_by_symbol": {"USD_JPY": "PASTED-CREDENTIAL"}}, id="spread-value"
        ),
        pytest.param({"require_stop_loss": "PASTED-CREDENTIAL"}, id="require_stop_loss"),
        pytest.param({"allowed_symbols": ["PASTED-CREDENTIAL", " "]}, id="allowed_symbols"),
    ],
)
@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_a_credential_pasted_into_the_safety_config_is_never_printed(
    tmp_path, monkeypatch, capsys, overrides, json_flag
):
    """PR #128 Codex review P1: an invalid field must be reported by NAME and
    expected shape only -- the value never reaches stdout, stderr or the
    machine-readable output."""
    set_live_env(monkeypatch)
    path = safety_config(tmp_path, **overrides)
    assert run(tmp_path, "--confirm-live", *json_flag, config_path=path) == EXIT_REFUSED
    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert "PASTED-CREDENTIAL" not in stream


def test_a_credential_pasted_as_a_config_key_is_never_printed(tmp_path, monkeypatch, capsys):
    """PR #128 Codex review r2 P1: the unknown-field message reaches stdout
    and the JSON output, so the key NAMES must not be in it either."""
    set_live_env(monkeypatch)
    path = safety_config(tmp_path, **{"PASTED-CREDENTIAL": "x"})
    assert run(tmp_path, "--confirm-live", "--json", config_path=path) == EXIT_REFUSED
    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert "PASTED-CREDENTIAL" not in stream


def test_the_cli_cannot_hand_the_composer_a_policy_object(tmp_path, monkeypatch, capsys):
    """PR #128 Codex review P1: the CLI loads the config only to render an
    operator message; authorization always re-loads it inside the sealed
    composer. A policy that the CLI could read but the composer could not is
    still a refusal."""
    import execution.live_startup_cli as cli

    set_live_env(monkeypatch)
    path = safety_config(tmp_path)
    # The CLI's own read succeeds; the composer's read is made to fail.
    def refusing_loader(config_path):
        raise cli.LiveSafetyConfigError("configuration unreadable at composition time")

    monkeypatch.setattr("execution.live_lock.load_live_safety_policy", refusing_loader)
    assert run(tmp_path, "--confirm-live", config_path=path) == EXIT_REFUSED
    assert "safety_policy_unavailable" in capsys.readouterr().err


def test_the_masked_account_is_shown_instead_of_the_real_one(tmp_path, monkeypatch, capsys):
    set_live_env(monkeypatch)
    run(tmp_path, "--confirm-live", "--json")
    payload = json.loads(capsys.readouterr().out)
    reference = payload["safety_summary"]["account_reference"]
    assert reference.startswith("account-")
    assert ACCOUNT not in reference
