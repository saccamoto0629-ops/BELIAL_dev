"""Issue #78: the shadow report CLI -- listing, detail lookup, decision
filtering, JSON purity, missing-DB / unknown-id / empty-store behaviour,
cp932 survival and stream restoration. Strictly read-only."""

import io
import json
import sys

import pytest

from execution import shadow_store
from execution.shadow_models import intended_order_from_request, ShadowResult
from execution.shadow_report import EXIT_FAILURE, EXIT_OK, main
from shadow_helpers import NOW, make_account, make_gate, make_market, make_request


def seed(tmp_path):
    """Two rows: one allowed, one rejected."""
    db = tmp_path / "shadow.sqlite3"
    shadow_store.init_db(db)
    gate = make_gate()
    for request, client_order_id, market in (
        (make_request(), "client-1", make_market()),
        (
            make_request(execution_request_id="exec-2", signal_id="signal-2"),
            "client-2",
            make_market(market_open=False),
        ),
    ):
        decision = gate.evaluate_shadow(
            request, market=market, account=make_account(), connection_target="shadow"
        )
        shadow_store.record_shadow_result(
            ShadowResult(
                request=request,
                client_order_id=client_order_id,
                timeframe="H1",
                decision=decision,
                intended_order=intended_order_from_request(
                    request, client_order_id=client_order_id
                ),
                market=market,
                policy_snapshot=gate.policy.to_snapshot(),
                safety_decision_id=None,
                created_at_utc=NOW,
            ),
            db_path=db,
        )
    return db


def test_list_shows_every_result(tmp_path, capsys):
    db = seed(tmp_path)
    assert main(["--shadow-db", str(db)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "ALLOWED" in out and "REJECTED" in out and "total 2" in out
    assert "exec-1" in out and "exec-2" in out


def test_detail_by_execution_request_id(tmp_path, capsys):
    db = seed(tmp_path)
    assert main(["--shadow-db", str(db), "--execution-request-id", "exec-2"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "REJECTED" in out and "market_closed" in out
    assert "intended order (NOT submitted)" in out


def test_detail_by_source_signal_id(tmp_path, capsys):
    db = seed(tmp_path)
    assert main(["--shadow-db", str(db), "--source-signal-id", "signal-1"]) == EXIT_OK
    assert "exec-1" in capsys.readouterr().out


def test_rejected_filter(tmp_path, capsys):
    db = seed(tmp_path)
    assert main(["--shadow-db", str(db), "--decision", "rejected"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "exec-2" in out and "exec-1" not in out


def test_unknown_id_exits_nonzero(tmp_path, capsys):
    db = seed(tmp_path)
    assert main(["--shadow-db", str(db), "--execution-request-id", "nope"]) == EXIT_FAILURE
    assert "no shadow result" in capsys.readouterr().err


def test_missing_db_is_reported_and_never_created(tmp_path, capsys):
    db = tmp_path / "missing.sqlite3"
    assert main(["--shadow-db", str(db)]) == EXIT_FAILURE
    assert "does not exist" in capsys.readouterr().err
    assert not db.exists()


def test_empty_store_is_a_valid_empty_report(tmp_path, capsys):
    db = tmp_path / "shadow.sqlite3"
    shadow_store.init_db(db)
    assert main(["--shadow-db", str(db)]) == EXIT_OK
    assert "no shadow results" in capsys.readouterr().out


def test_json_report_is_machine_readable(tmp_path, capsys):
    db = seed(tmp_path)
    assert main(["--shadow-db", str(db), "--json", "--decision", "rejected"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    entry = payload["results"][0]
    assert entry["execution_request_id"] == "exec-2"
    assert entry["allowed"] is False and "market_closed" in entry["rejection_codes"]
    assert entry["intended_order"]["client_order_id"] == "client-2"


def test_report_is_read_only(tmp_path):
    db = seed(tmp_path)
    before = db.read_bytes()
    assert main(["--shadow-db", str(db)]) == EXIT_OK
    assert main(["--shadow-db", str(db), "--json"]) == EXIT_OK
    assert db.read_bytes() == before


def test_corrupted_db_is_an_explicit_failure(tmp_path, capsys):
    db = tmp_path / "shadow.sqlite3"
    shadow_store.init_db(db)
    import sqlite3

    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE stray (id INTEGER)")
    connection.commit()
    connection.close()
    assert main(["--shadow-db", str(db)]) == EXIT_FAILURE
    assert "shadow report failed" in capsys.readouterr().err


# --- console encoding / stream restoration --------------------------------------


def _cp932_stdout(monkeypatch):
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp932", errors="strict", newline="")
    monkeypatch.setattr(sys, "stdout", stream)
    return buffer, stream


def test_report_survives_a_cp932_console(tmp_path, monkeypatch):
    db = seed(tmp_path)
    buffer, stream = _cp932_stdout(monkeypatch)
    assert main(["--shadow-db", str(db)]) == EXIT_OK
    assert sys.stdout.errors == "strict"
    stream.flush()
    assert "total 2" in buffer.getvalue().decode("cp932")


def test_json_survives_a_cp932_console(tmp_path, monkeypatch):
    db = seed(tmp_path)
    buffer, stream = _cp932_stdout(monkeypatch)
    assert main(["--shadow-db", str(db), "--json"]) == EXIT_OK
    stream.flush()
    payload = json.loads(buffer.getvalue().decode("ascii"))
    assert payload["count"] == 2


def _stream_settings():
    return (
        getattr(sys.stdout, "errors", None),
        getattr(sys.stderr, "errors", None),
    )


def test_streams_are_restored_on_every_exit_path(tmp_path):
    db = seed(tmp_path)
    before = _stream_settings()
    assert main(["--shadow-db", str(db)]) == EXIT_OK
    assert _stream_settings() == before
    assert main(["--shadow-db", str(tmp_path / 'missing.sqlite3')]) == EXIT_FAILURE
    assert _stream_settings() == before
    with pytest.raises(SystemExit):
        main(["--no-such-flag"])
    assert _stream_settings() == before
