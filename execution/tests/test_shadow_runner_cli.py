"""Issue #78: the shadow runner CLI -- the production signal path vs the CLI
fixture path (provenance separation), mode dispatch (shadow / dry-run run;
fake / demo / live / unknown refused), strict cycle-input validation
(duplicate keys / UTF-8 / limits), duplicate-vs-conflict exit codes, dry-run
zero-side-effects, JSON purity, cp932 survival and stream restoration."""

import io
import json
import sqlite3
import sys

import pytest

from execution.shadow_runner import (
    EXIT_CYCLE_FAILURE,
    EXIT_OK,
    EXIT_USAGE_ERROR,
    main,
)

FIXED_NOW = "2026-01-01T00:00:00+00:00"


def seed_signal_db(tmp_path, signal_id="sig-1"):
    db = tmp_path / "paper_signals.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE paper_signals (signal_id TEXT PRIMARY KEY, created_at_utc TEXT NOT NULL, "
        "symbol TEXT NOT NULL, timeframe TEXT NOT NULL, side TEXT NOT NULL, "
        "strategy_profile TEXT NOT NULL, status TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO paper_signals VALUES (?, ?, 'USD_JPY', 'H1', 'long', 'strategy-1', 'paper_only')",
        (signal_id, FIXED_NOW),
    )
    connection.commit()
    connection.close()
    return db


def write_production_inputs(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "config_id": "cli-prod-config-1",
                "policy": {
                    "policy_id": "cli-prod-policy-1",
                    "allowed_symbols": ["USD_JPY"],
                    "allowed_sides": ["buy", "sell"],
                    "max_order_quantity": "10",
                    "max_positions_per_symbol": 2,
                    "max_positions_account": 5,
                    "max_reference_age_seconds": 30,
                    "max_spread_by_symbol": {"USD_JPY": "0.05"},
                    "allowed_shadow_targets": ["shadow"],
                    "require_stop_loss": False,
                },
                "order_quantities": {"USD_JPY": "1"},
                "kill_switch": False,
                "connection_target": "shadow",
            }
        ),
        encoding="utf-8",
    )
    market = tmp_path / "market.json"
    market.write_text(
        json.dumps(
            {
                "symbol": "USD_JPY", "reference_price": "150.00", "bid": "149.99",
                "ask": "150.01", "observed_at_utc": FIXED_NOW, "market_open": True,
                "tradable": True,
            }
        ),
        encoding="utf-8",
    )
    account = tmp_path / "account.json"
    account.write_text(
        json.dumps(
            {
                "account_id": "cli-account", "currency": "JPY", "balance": "1000",
                "equity": "1000", "margin_available": "1000",
                "retrieved_at_utc": FIXED_NOW, "positions": [],
            }
        ),
        encoding="utf-8",
    )
    return config, market, account


def run_production(tmp_path, *extra, signal_id="sig-1", mode="shadow"):
    signal_db = seed_signal_db(tmp_path)
    config, market, account = write_production_inputs(tmp_path)
    data_dir = tmp_path / "data"
    return (
        main(
            ["--mode", mode, "--signal-id", signal_id, "--signal-db", str(signal_db),
             "--execution-config", str(config), "--market-snapshot", str(market),
             "--account-snapshot", str(account), "--data-dir", str(data_dir),
             "--now", FIXED_NOW, *extra]
        ),
        data_dir,
    )


def cycle_payload(**overrides):
    payload = {
        "request": {
            "execution_request_id": "exec-1",
            "signal_id": "signal-1",
            "strategy_id": "strategy-1",
            "symbol": "USD_JPY",
            "side": "buy",
            "order_type": "market",
            "quantity": "1",
            "stop_loss_price": "149.50",
            "take_profit_price": "151.50",
        },
        "client_order_id": "client-1",
        "timeframe": "H1",
        "market": {
            "reference_price": "150.00",
            "bid": "149.99",
            "ask": "150.01",
        },
    }
    payload.update(overrides)
    return payload


def write_cycle(tmp_path, payload=None, name="cycle.json"):
    path = tmp_path / name
    path.write_text(
        json.dumps(payload if payload is not None else cycle_payload()), encoding="utf-8"
    )
    return path


def run(tmp_path, *extra, payload=None, mode="shadow"):
    cycle = write_cycle(tmp_path, payload)
    data_dir = tmp_path / "data"
    return (
        main(
            ["--mode", mode, "--cycle", str(cycle), "--data-dir", str(data_dir),
             "--now", FIXED_NOW, *extra]
        ),
        data_dir,
    )


# --- production signal path -----------------------------------------------------


def test_production_cycle_records_with_production_provenance(tmp_path, capsys):
    code, data_dir = run_production(tmp_path)
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "RECORDED sigreq-sig-1 gate=ALLOWED" in out
    assert "origin=production_signal" in out
    assert "paper_signal_db:" in out and "cli-prod-config-1" in out
    assert (data_dir / "shadow_results.sqlite3").exists()


def test_production_json_carries_provenance(tmp_path, capsys):
    code, _ = run_production(tmp_path, "--json")
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    provenance = payload["result"]["provenance"]
    assert provenance["input_origin"] == "production_signal"
    assert provenance["config_version"] == "cli-prod-config-1"
    assert provenance["signal_observed_at_utc"] == FIXED_NOW


def test_production_missing_signal_is_a_cycle_failure(tmp_path, capsys):
    code, _ = run_production(tmp_path, signal_id="missing")
    assert code == EXIT_CYCLE_FAILURE
    assert "does not exist" in capsys.readouterr().err


def test_production_requires_all_provider_flags(tmp_path, capsys):
    signal_db = seed_signal_db(tmp_path)
    code = main(["--signal-id", "sig-1", "--signal-db", str(signal_db), "--now", FIXED_NOW,
                 "--data-dir", str(tmp_path / "data")])
    assert code == EXIT_USAGE_ERROR
    err = capsys.readouterr().err
    assert "--execution-config" in err and "no defaults" in err
    assert not (tmp_path / "data").exists()


def test_production_incomplete_config_is_a_usage_error(tmp_path, capsys):
    signal_db = seed_signal_db(tmp_path)
    config, market, account = write_production_inputs(tmp_path)
    broken = json.loads(config.read_text(encoding="utf-8"))
    del broken["policy"]["require_stop_loss"]
    config.write_text(json.dumps(broken), encoding="utf-8")
    code = main(["--signal-id", "sig-1", "--signal-db", str(signal_db),
                 "--execution-config", str(config), "--market-snapshot", str(market),
                 "--account-snapshot", str(account), "--data-dir", str(tmp_path / "data"),
                 "--now", FIXED_NOW])
    assert code == EXIT_USAGE_ERROR
    assert "require_stop_loss" in capsys.readouterr().err


def test_production_dry_run_creates_nothing(tmp_path):
    code, data_dir = run_production(tmp_path, mode="dry-run")
    assert code == EXIT_OK
    assert not data_dir.exists()


def test_cycle_and_signal_id_are_mutually_exclusive(tmp_path):
    cycle = write_cycle(tmp_path)
    with pytest.raises(SystemExit):
        main(["--cycle", str(cycle), "--signal-id", "sig-1"])


# --- duplicate vs conflict at the CLI boundary ----------------------------------


def test_conflicting_payload_exits_nonzero_with_the_reason_code(tmp_path, capsys):
    cycle = write_cycle(tmp_path)
    data_dir = tmp_path / "data"
    base = ["--cycle", str(cycle), "--data-dir", str(data_dir), "--now", FIXED_NOW]
    assert main(base) == EXIT_OK
    changed = cycle_payload()
    changed["request"]["quantity"] = "2"
    conflicting = write_cycle(tmp_path, changed, name="changed.json")
    code = main(["--cycle", str(conflicting), "--data-dir", str(data_dir), "--now", FIXED_NOW])
    assert code == EXIT_CYCLE_FAILURE
    err = capsys.readouterr().err
    assert "shadow_payload_mismatch" in err and "quantity" in err


# --- shadow mode ----------------------------------------------------------------


def test_shadow_cycle_records_and_exits_zero(tmp_path, capsys):
    code, data_dir = run(tmp_path)
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "RECORDED exec-1 gate=ALLOWED" in out
    assert (data_dir / "shadow_results.sqlite3").exists()
    assert (data_dir / "safety_gate_decisions.sqlite3").exists()


def test_rejected_cycle_is_still_a_completed_cycle(tmp_path, capsys):
    payload = cycle_payload()
    payload["kill_switch"] = True
    code, _ = run(tmp_path, payload=payload)
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "gate=REJECTED" in out and "kill_switch_enabled" in out


def test_duplicate_cycle_is_explicit(tmp_path, capsys):
    cycle = write_cycle(tmp_path)
    data_dir = tmp_path / "data"
    args = ["--cycle", str(cycle), "--data-dir", str(data_dir), "--now", FIXED_NOW]
    assert main(args) == EXIT_OK
    assert main(args) == EXIT_OK
    assert "DUPLICATE exec-1" in capsys.readouterr().out


def test_json_output_is_machine_readable_and_pure(tmp_path, capsys):
    code, _ = run(tmp_path, "--json")
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "recorded" and payload["mode"] == "shadow"
    assert payload["result"]["execution_request_id"] == "exec-1"
    assert payload["result"]["allowed"] is True
    assert payload["result"]["intended_order"]["client_order_id"] == "client-1"


# --- dry-run mode ---------------------------------------------------------------


def test_dry_run_creates_no_db_files(tmp_path, capsys):
    code, data_dir = run(tmp_path, mode="dry-run")
    assert code == EXIT_OK
    assert not data_dir.exists() or not any(data_dir.iterdir())
    assert "DRY-RUN" in capsys.readouterr().out


def test_dry_run_with_a_missing_db_path_never_creates_the_file(tmp_path):
    cycle = write_cycle(tmp_path)
    shadow_db = tmp_path / "nowhere" / "shadow_results.sqlite3"
    decision_db = tmp_path / "nowhere" / "safety.sqlite3"
    code = main(
        ["--mode", "dry-run", "--cycle", str(cycle), "--shadow-db", str(shadow_db),
         "--decision-db", str(decision_db), "--now", FIXED_NOW]
    )
    assert code == EXIT_OK
    assert not shadow_db.exists() and not decision_db.exists()
    assert not shadow_db.parent.exists()


def test_dry_run_fails_explicitly_on_an_invalid_existing_db(tmp_path, capsys):
    cycle = write_cycle(tmp_path)
    shadow_db = tmp_path / "shadow_results.sqlite3"
    shadow_db.write_bytes(b"garbage, not sqlite")
    before = shadow_db.read_bytes()
    code = main(
        ["--mode", "dry-run", "--cycle", str(cycle), "--shadow-db", str(shadow_db),
         "--decision-db", str(tmp_path / "safety.sqlite3"), "--now", FIXED_NOW]
    )
    assert code == EXIT_CYCLE_FAILURE
    assert "shadow cycle failed" in capsys.readouterr().err
    assert shadow_db.read_bytes() == before


# --- mode dispatch --------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        # Issue #132: demo is no longer "unimplemented" either -- it is a
        # real submitting mode composed by the demo activation lock. The
        # shadow runner must still refuse it EXPLICITLY (it never submits),
        # and the refusal must not let a shadow cycle run under a label that
        # implies a demo account was contacted.
        pytest.param("demo", "demo_lock", id="demo-submits"),
        # Issue #82: live is no longer "unimplemented" -- it is LOCKED. The
        # shadow runner must still refuse it (it never submits), and the
        # refusal must point at the one command that may compose a live
        # connection.
        pytest.param("live", "live activation lock", id="live-locked"),
    ],
)
def test_demo_and_live_are_refused_by_the_shadow_runner(tmp_path, capsys, mode, expected):
    code, data_dir = run(tmp_path, mode=mode)
    assert code == EXIT_USAGE_ERROR
    assert expected in capsys.readouterr().err
    assert not data_dir.exists()


def test_fake_mode_is_refused_with_a_pointer_to_the_sanctioned_path(tmp_path, capsys):
    code, _ = run(tmp_path, mode="fake")
    assert code == EXIT_USAGE_ERROR
    assert "LiveOrderSubmissionService" in capsys.readouterr().err


def test_unknown_mode_is_refused(tmp_path, capsys):
    code, _ = run(tmp_path, mode="paper")
    assert code == EXIT_USAGE_ERROR
    assert "unknown execution mode" in capsys.readouterr().err


# --- strict JSON loader at the CLI boundary --------------------------------------


def raw_cycle(tmp_path, data: bytes, name="raw.json"):
    path = tmp_path / name
    path.write_bytes(data)
    return main(["--cycle", str(path), "--data-dir", str(tmp_path / "data"), "--now", FIXED_NOW])


def test_duplicate_root_key_is_a_usage_error(tmp_path, capsys):
    payload = json.dumps(cycle_payload())[:-1] + ', "kill_switch": false}'
    assert '"kill_switch"' in payload
    duplicated = payload.replace('{"request"', '{"kill_switch": true, "request"', 1)
    assert raw_cycle(tmp_path, duplicated.encode("utf-8")) == EXIT_USAGE_ERROR
    assert "duplicate object key" in capsys.readouterr().err


def test_duplicate_nested_key_is_a_usage_error(tmp_path, capsys):
    payload = cycle_payload()
    text = json.dumps(payload).replace('"quantity": "1"', '"quantity": "1", "quantity": "1"', 1)
    assert raw_cycle(tmp_path, text.encode("utf-8")) == EXIT_USAGE_ERROR
    assert "duplicate object key" in capsys.readouterr().err


def test_invalid_utf8_is_a_stable_usage_error(tmp_path, capsys):
    assert raw_cycle(tmp_path, b'{"request": "\xff\xfe"}') == EXIT_USAGE_ERROR
    err = capsys.readouterr().err
    assert "not valid UTF-8" in err and "Traceback" not in err


def test_bom_is_rejected(tmp_path, capsys):
    assert raw_cycle(tmp_path, b"\xef\xbb\xbf" + json.dumps(cycle_payload()).encode()) == EXIT_USAGE_ERROR
    assert "BOM" in capsys.readouterr().err


def test_deep_json_is_rejected(tmp_path, capsys):
    deep = b'{"request": ' + b"[" * 64 + b"1" + b"]" * 64 + b"}"
    assert raw_cycle(tmp_path, deep) == EXIT_USAGE_ERROR


def test_oversized_file_is_rejected(tmp_path, capsys):
    blob = b'{"request": "' + b"x" * 300_000 + b'"}'
    assert raw_cycle(tmp_path, blob) == EXIT_USAGE_ERROR
    assert "maximum accepted size" in capsys.readouterr().err


def test_nan_and_infinity_are_rejected(tmp_path, capsys):
    assert raw_cycle(tmp_path, b'{"request": NaN}') == EXIT_USAGE_ERROR
    assert "non-finite" in capsys.readouterr().err
    assert raw_cycle(tmp_path, b'{"request": Infinity}', ) == EXIT_USAGE_ERROR


def test_bool_as_number_is_rejected(tmp_path, capsys):
    payload = cycle_payload()
    payload["request"]["quantity"] = True
    code, _ = run(tmp_path, payload=payload)
    assert code == EXIT_USAGE_ERROR
    assert "decimal string" in capsys.readouterr().err


def test_huge_exponent_decimals_are_rejected(tmp_path, capsys):
    payload = cycle_payload()
    payload["request"]["quantity"] = "1E+999"
    code, _ = run(tmp_path, payload=payload)
    assert code == EXIT_USAGE_ERROR
    assert "exponent" in capsys.readouterr().err


def test_valid_japanese_content_survives(tmp_path, capsys):
    payload = cycle_payload()
    payload["request"]["strategy_id"] = "戦略-1"
    code, _ = run(tmp_path, payload=payload)
    assert code == EXIT_OK


# --- cycle input validation -----------------------------------------------------


def test_unknown_cycle_keys_are_refused(tmp_path, capsys):
    payload = cycle_payload()
    payload["surprise"] = 1
    code, _ = run(tmp_path, payload=payload)
    assert code == EXIT_USAGE_ERROR
    assert "unknown keys" in capsys.readouterr().err


def test_float_quantity_is_refused(tmp_path, capsys):
    payload = cycle_payload()
    payload["request"]["quantity"] = 1.0
    code, _ = run(tmp_path, payload=payload)
    assert code == EXIT_USAGE_ERROR
    assert "decimal string" in capsys.readouterr().err


def test_missing_request_key_is_refused(tmp_path, capsys):
    payload = cycle_payload()
    del payload["request"]["signal_id"]
    code, _ = run(tmp_path, payload=payload)
    assert code == EXIT_USAGE_ERROR


def test_unreadable_cycle_file_is_a_usage_error(tmp_path, capsys):
    code = main(["--cycle", str(tmp_path / "missing.json"), "--now", FIXED_NOW,
                 "--data-dir", str(tmp_path / "data")])
    assert code == EXIT_USAGE_ERROR
    assert "cycle input" in capsys.readouterr().err


def test_invalid_now_is_a_usage_error(tmp_path, capsys):
    cycle = write_cycle(tmp_path)
    code = main(["--cycle", str(cycle), "--now", "yesterday",
                 "--data-dir", str(tmp_path / "data")])
    assert code == EXIT_USAGE_ERROR


# --- console encoding / stream restoration --------------------------------------


def _cp932_stdout(monkeypatch):
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp932", errors="strict", newline="")
    monkeypatch.setattr(sys, "stdout", stream)
    return buffer, stream


def test_cli_survives_a_cp932_console(tmp_path, monkeypatch):
    cycle = write_cycle(tmp_path)
    buffer, stream = _cp932_stdout(monkeypatch)
    code = main(["--cycle", str(cycle), "--data-dir", str(tmp_path / "data"), "--now", FIXED_NOW])
    assert code == EXIT_OK
    assert sys.stdout.errors == "strict"
    stream.flush()
    assert "RECORDED exec-1" in buffer.getvalue().decode("cp932")


def test_json_survives_a_cp932_console(tmp_path, monkeypatch):
    cycle = write_cycle(tmp_path)
    buffer, stream = _cp932_stdout(monkeypatch)
    code = main(["--cycle", str(cycle), "--data-dir", str(tmp_path / "data"), "--now", FIXED_NOW,
                 "--json"])
    assert code == EXIT_OK
    stream.flush()
    payload = json.loads(buffer.getvalue().decode("ascii"))
    assert payload["status"] == "recorded"


def _stream_settings():
    return (
        getattr(sys.stdout, "errors", None),
        getattr(sys.stderr, "errors", None),
    )


def test_streams_are_restored_on_every_exit_path(tmp_path):
    before = _stream_settings()
    assert run(tmp_path)[0] == EXIT_OK
    assert _stream_settings() == before
    assert run(tmp_path, mode="live")[0] == EXIT_USAGE_ERROR
    assert _stream_settings() == before
    with pytest.raises(SystemExit):
        main(["--no-such-flag"])
    assert _stream_settings() == before
