"""Issue #78: the shadow runner CLI -- mode dispatch (shadow / dry-run run;
fake / demo / live / unknown refused), cycle-input validation, dry-run
zero-side-effects, JSON purity, cp932 console survival and stream
restoration."""

import io
import json
import sys

import pytest

from execution.shadow_runner import (
    EXIT_CYCLE_FAILURE,
    EXIT_OK,
    EXIT_USAGE_ERROR,
    main,
)

FIXED_NOW = "2026-01-01T00:00:00+00:00"


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


@pytest.mark.parametrize("mode", ["demo", "live"])
def test_demo_and_live_are_refused_as_unimplemented(tmp_path, capsys, mode):
    code, data_dir = run(tmp_path, mode=mode)
    assert code == EXIT_USAGE_ERROR
    assert "NOT implemented" in capsys.readouterr().err
    assert not data_dir.exists()


def test_fake_mode_is_refused_with_a_pointer_to_the_sanctioned_path(tmp_path, capsys):
    code, _ = run(tmp_path, mode="fake")
    assert code == EXIT_USAGE_ERROR
    assert "LiveOrderSubmissionService" in capsys.readouterr().err


def test_unknown_mode_is_refused(tmp_path, capsys):
    code, _ = run(tmp_path, mode="paper")
    assert code == EXIT_USAGE_ERROR
    assert "unknown execution mode" in capsys.readouterr().err


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
