"""Issue #78: read-only shadow report CLI.

Deterministic output for allow / reject / empty results, a missing DB is
reported without ever being created (dry-run-safe read path), unknown ids
and tampered records exit non-zero, and the CLI never modifies an existing
DB (byte-for-byte hash comparison). All SQLite lives under tmp_path.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from broker.broker_models import OrderSide, OrderType
from execution.execution_models import ExecutionRequest
from execution.safety_gate import SafetyDecision, SafetyRejection, SafetyRejectionCode
from execution.shadow_report import main
from execution.shadow_store import ShadowExecutionRecord, SQLiteShadowExecutionStore

NOW = datetime(2026, 7, 22, 1, 0, tzinfo=timezone.utc)


def request(**overrides):
    values = dict(
        execution_request_id="exec-1",
        signal_id="signal-1",
        strategy_id="strategy-1",
        symbol="USD_JPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        requested_at_utc=NOW,
        requested_price=None,
        stop_loss_price=Decimal("149.5"),
        take_profit_price=Decimal("151.5"),
    )
    values.update(overrides)
    return ExecutionRequest(**values)


def decision(request_obj, *, allowed=True, rejections=()):
    return SafetyDecision(
        execution_request_id=request_obj.execution_request_id,
        allowed=allowed,
        checked_at_utc=NOW,
        policy_id="policy-test-1",
        rejections=tuple(rejections),
        execution_mode="fake",
        connection_target="fake",
    )


def populate(tmp_path):
    """One ALLOWED and one REJECTED record with distinct observation times."""
    db_path = tmp_path / "shadow.sqlite3"
    store = SQLiteShadowExecutionStore(db_path)
    store.initialize()
    allowed_request = request()
    store.record(
        ShadowExecutionRecord(
            request=allowed_request,
            decision=decision(allowed_request),
            observed_at_utc=NOW,
        )
    )
    rejected_request = request(
        execution_request_id="exec-2", signal_id="signal-2", quantity=Decimal("999")
    )
    store.record(
        ShadowExecutionRecord(
            request=rejected_request,
            decision=decision(
                rejected_request,
                allowed=False,
                rejections=(
                    SafetyRejection(
                        SafetyRejectionCode.QUANTITY_LIMIT_EXCEEDED,
                        "quantity 999 exceeds limit 10",
                    ),
                ),
            ),
            observed_at_utc=NOW + timedelta(seconds=10),
        )
    )
    return db_path


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_list_shows_allow_and_reject_deterministically(tmp_path, capsys):
    db_path = populate(tmp_path)

    assert main(["list", "--db", str(db_path)]) == 0
    first = capsys.readouterr().out
    assert main(["list", "--db", str(db_path)]) == 0
    second = capsys.readouterr().out

    assert first == second  # deterministic
    lines = first.strip().splitlines()
    assert lines[0].startswith("observed_at_utc | signal_id | execution_request_id")
    assert "exec-1" in lines[1] and "ALLOWED" in lines[1]
    assert "exec-2" in lines[2] and "REJECTED" in lines[2]
    assert "quantity_limit_exceeded" in lines[2]


def test_list_respects_the_limit(tmp_path, capsys):
    db_path = populate(tmp_path)
    assert main(["list", "--db", str(db_path), "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "exec-1" in out and "exec-2" not in out


def test_empty_db_lists_no_records(tmp_path, capsys):
    db_path = tmp_path / "shadow.sqlite3"
    SQLiteShadowExecutionStore(db_path).initialize()
    assert main(["list", "--db", str(db_path)]) == 0
    assert "no shadow execution records" in capsys.readouterr().out


def test_missing_db_is_reported_and_never_created(tmp_path, capsys):
    db_path = tmp_path / "missing.sqlite3"
    assert main(["list", "--db", str(db_path)]) == 0
    assert "no shadow execution records" in capsys.readouterr().out
    assert not db_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_show_renders_the_full_allowed_record(tmp_path, capsys):
    db_path = populate(tmp_path)
    assert main(["show", "exec-1", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "execution_mode: shadow" in out
    assert "gate_decision: ALLOWED" in out
    assert "signal_id: signal-1" in out
    assert "policy_id: policy-test-1" in out
    assert "stop_loss_price: 149.5" in out
    assert "intended_order" in out and "client_order_id" not in out.split("intended_order")[0]
    assert "  limit_price: -" in out
    assert "  quantity: 1" in out
    assert "rejections: none" in out


def test_show_renders_rejection_codes_and_messages(tmp_path, capsys):
    db_path = populate(tmp_path)
    assert main(["show", "exec-2", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    assert "gate_decision: REJECTED" in out
    assert "quantity_limit_exceeded: quantity 999 exceeds limit 10" in out


def test_show_unknown_id_exits_non_zero(tmp_path, capsys):
    db_path = populate(tmp_path)
    assert main(["show", "exec-404", "--db", str(db_path)]) == 1
    assert "no shadow execution record" in capsys.readouterr().err


def test_show_on_a_missing_db_exits_non_zero_without_creating_it(tmp_path, capsys):
    db_path = tmp_path / "missing.sqlite3"
    assert main(["show", "exec-1", "--db", str(db_path)]) == 1
    assert not db_path.exists()


def test_tampered_records_fail_loudly_instead_of_being_trusted(tmp_path, capsys):
    db_path = populate(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE shadow_executions SET rejections_json = ? WHERE execution_request_id = 'exec-2'",
            (json.dumps([{"code": "made_up", "message": "x"}]),),
        )
    assert main(["list", "--db", str(db_path)]) == 1
    assert "shadow report failed" in capsys.readouterr().err


def test_a_foreign_db_is_refused_read_only(tmp_path, capsys):
    db_path = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE paper_signals (id INTEGER PRIMARY KEY)")
    before = file_hash(db_path)
    assert main(["list", "--db", str(db_path)]) == 1
    assert "schema verification" in capsys.readouterr().err
    assert file_hash(db_path) == before


def test_the_cli_never_modifies_an_existing_db(tmp_path, capsys):
    db_path = populate(tmp_path)
    before = file_hash(db_path)
    assert main(["list", "--db", str(db_path)]) == 0
    assert main(["show", "exec-1", "--db", str(db_path)]) == 0
    assert main(["show", "exec-404", "--db", str(db_path)]) == 1
    capsys.readouterr()
    assert file_hash(db_path) == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["shadow.sqlite3"]
