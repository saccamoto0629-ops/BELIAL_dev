"""Issue #78: shadow verification-record store.

Persistence round-trips (Decimal / UTC / enums / rejection reasons),
per-mode idempotency (execution_request_id / signal_id UNIQUE inside the
shadow store only), closed-world schema verification, strict restore-time
validation, and the read paths' no-side-effect contract (missing DB is
never created -- dry-run safe). All SQLite lives under tmp_path; no broker,
no network, no Discord, no S3.
"""

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from broker.broker_models import OrderSide, OrderType
from execution.execution_models import ExecutionRequest
from execution.safety_gate import (
    SafetyDecision,
    SafetyRejection,
    SafetyRejectionCode,
)
from execution import shadow_store
from execution.shadow_store import (
    ShadowExecutionConflictError,
    ShadowExecutionRecord,
    ShadowIntegrityError,
    ShadowSchemaError,
    ShadowStoreError,
    SQLiteShadowExecutionStore,
)

NOW = datetime(2026, 7, 22, 1, 0, tzinfo=timezone.utc)


def request(**overrides):
    values = dict(
        execution_request_id="exec-1",
        signal_id="signal-1",
        strategy_id="strategy-1",
        symbol="USD_JPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1.5"),
        requested_at_utc=NOW,
        requested_price=None,
        stop_loss_price=Decimal("149.5"),
        take_profit_price=Decimal("151.5"),
    )
    values.update(overrides)
    return ExecutionRequest(**values)


def decision(request_obj=None, *, allowed=True, rejections=(), **overrides):
    request_obj = request_obj if request_obj is not None else request()
    values = dict(
        execution_request_id=request_obj.execution_request_id,
        allowed=allowed,
        checked_at_utc=NOW,
        policy_id="policy-test-1",
        rejections=tuple(rejections),
        execution_mode="fake",
        connection_target="fake",
        account_id="fake-account",
        position_count=0,
    )
    values.update(overrides)
    return SafetyDecision(**values)


def rejected_decision(request_obj=None, **overrides):
    return decision(
        request_obj,
        allowed=False,
        rejections=(
            SafetyRejection(SafetyRejectionCode.KILL_SWITCH_ENABLED, "kill switch is enabled"),
            SafetyRejection(SafetyRejectionCode.REFERENCE_PRICE_STALE, "price is stale"),
        ),
        account_id=None,
        position_count=None,
        **overrides,
    )


def record(request_obj=None, *, the_decision=None, observed_at=NOW):
    request_obj = request_obj if request_obj is not None else request()
    return ShadowExecutionRecord(
        request=request_obj,
        decision=the_decision if the_decision is not None else decision(request_obj),
        observed_at_utc=observed_at,
    )


def store(tmp_path, *, initialize=True):
    instance = SQLiteShadowExecutionStore(tmp_path / "shadow.sqlite3")
    if initialize:
        instance.initialize()
    return instance


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- model invariants ---------------------------------------------------------


def test_record_requires_matching_request_and_decision_ids():
    other = decision(request(execution_request_id="exec-other", signal_id="signal-other"))
    with pytest.raises(ValueError, match="belongs to"):
        ShadowExecutionRecord(request=request(), decision=other, observed_at_utc=NOW)


def test_record_rejects_naive_observed_at():
    with pytest.raises(ValueError, match="observed_at_utc"):
        ShadowExecutionRecord(
            request=request(), decision=decision(), observed_at_utc=datetime(2026, 7, 22, 1, 0)
        )


def test_record_rejects_wrong_types():
    with pytest.raises(ValueError, match="request must be"):
        ShadowExecutionRecord(request="nope", decision=decision(), observed_at_utc=NOW)
    with pytest.raises(ValueError, match="decision must be"):
        ShadowExecutionRecord(request=request(), decision="nope", observed_at_utc=NOW)


def test_intended_order_never_carries_a_client_order_id_or_sl_tp():
    market_intent = record().intended_order
    assert set(market_intent) == {"symbol", "side", "order_type", "quantity", "limit_price"}
    assert market_intent["limit_price"] is None

    limit_request = request(
        order_type=OrderType.LIMIT,
        requested_price=Decimal("150"),
    )
    limit_intent = record(
        limit_request, the_decision=decision(limit_request)
    ).intended_order
    assert limit_intent["limit_price"] == "150"
    assert limit_intent["quantity"] == "1.5"


# --- round trip ----------------------------------------------------------------


def test_record_round_trips_after_reopen(tmp_path):
    saved = store(tmp_path).record(record())

    reopened = store(tmp_path, initialize=False)
    restored = reopened.get_by_execution_request_id("exec-1")
    assert restored == saved == record()
    assert restored.request.quantity == Decimal("1.5")
    assert restored.request.stop_loss_price == Decimal("149.5")
    assert restored.observed_at_utc == NOW
    assert restored.decision.allowed is True
    assert reopened.get_by_signal_id("signal-1") == restored
    assert reopened.list_records() == (restored,)


def test_rejected_record_restores_every_rejection_reason(tmp_path):
    the_record = record(the_decision=rejected_decision())
    store(tmp_path).record(the_record)

    restored = store(tmp_path, initialize=False).get_by_execution_request_id("exec-1")
    assert restored.decision.allowed is False
    assert restored.decision.rejection_codes == ("kill_switch_enabled", "reference_price_stale")
    assert [item.message for item in restored.decision.rejections] == [
        "kill switch is enabled",
        "price is stale",
    ]


def test_limit_order_record_round_trips_the_intended_order(tmp_path):
    limit_request = request(order_type=OrderType.LIMIT, requested_price=Decimal("149.75"))
    store(tmp_path).record(record(limit_request, the_decision=decision(limit_request)))

    restored = store(tmp_path, initialize=False).get_by_execution_request_id("exec-1")
    assert restored.intended_order == {
        "symbol": "USD_JPY",
        "side": "buy",
        "order_type": OrderType.LIMIT.value,
        "quantity": "1.5",
        "limit_price": "149.75",
    }


def test_list_records_is_deterministic(tmp_path):
    the_store = store(tmp_path)
    later = request(execution_request_id="exec-2", signal_id="signal-2")
    earlier = request(execution_request_id="exec-3", signal_id="signal-3")
    the_store.record(record(later, the_decision=decision(later), observed_at=NOW + timedelta(seconds=5)))
    the_store.record(record(earlier, the_decision=decision(earlier), observed_at=NOW - timedelta(seconds=5)))
    the_store.record(record())

    listed = the_store.list_records()
    assert [item.request.execution_request_id for item in listed] == ["exec-3", "exec-1", "exec-2"]
    assert the_store.list_records() == listed  # stable across calls
    assert [item.request.execution_request_id for item in the_store.list_records(limit=2)] == [
        "exec-3",
        "exec-1",
    ]


def test_list_records_rejects_invalid_limit(tmp_path):
    the_store = store(tmp_path)
    with pytest.raises(ShadowStoreError, match="limit"):
        the_store.list_records(limit=0)
    with pytest.raises(ShadowStoreError, match="limit"):
        the_store.list_records(limit=True)


# --- idempotency (per-mode uniqueness) ------------------------------------------


def test_duplicate_execution_request_id_is_suppressed_with_a_warn_log(tmp_path, caplog):
    the_store = store(tmp_path)
    the_store.record(record())
    with caplog.at_level(logging.WARNING, logger="execution.shadow_store"):
        with pytest.raises(ShadowExecutionConflictError, match="already holds a shadow record"):
            the_store.record(record())
    assert len(the_store.list_records()) == 1
    joined = " ".join(message for message in caplog.messages)
    assert "exec-1" in joined and "signal-1" in joined and "reason=" in joined


def test_duplicate_signal_id_is_suppressed_even_under_a_new_request_id(tmp_path):
    the_store = store(tmp_path)
    the_store.record(record())
    fresh = request(execution_request_id="exec-2")  # same signal-1
    with pytest.raises(ShadowExecutionConflictError, match="source signal already holds"):
        the_store.record(record(fresh, the_decision=decision(fresh)))
    assert len(the_store.list_records()) == 1


def test_concurrent_style_double_record_keeps_exactly_one_row(tmp_path):
    first = store(tmp_path)
    second = SQLiteShadowExecutionStore(first.path)  # independent connection path
    first.record(record())
    with pytest.raises(ShadowExecutionConflictError):
        second.record(record())
    assert len(second.list_records()) == 1


def test_uniqueness_is_enforced_at_the_db_level_too(tmp_path):
    the_store = store(tmp_path)
    the_store.record(record())
    with sqlite3.connect(the_store.path) as connection:
        row = connection.execute(
            "SELECT * FROM shadow_executions WHERE execution_request_id = 'exec-1'"
        ).fetchone()
        assert row is not None
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO shadow_executions (execution_request_id, signal_id, strategy_id, "
                "symbol, side, order_type, quantity, requested_price, stop_loss_price, "
                "take_profit_price, requested_at_utc, execution_mode, observed_at_utc, allowed, "
                "checked_at_utc, policy_id, connection_mode, connection_target, account_id, "
                "position_count, rejections_json, intended_order_json) "
                "SELECT 'exec-9', signal_id, strategy_id, symbol, side, order_type, quantity, "
                "requested_price, stop_loss_price, take_profit_price, requested_at_utc, "
                "execution_mode, observed_at_utc, allowed, checked_at_utc, policy_id, "
                "connection_mode, connection_target, account_id, position_count, "
                "rejections_json, intended_order_json FROM shadow_executions",
            )


# --- persistence failure / rollback ---------------------------------------------


def test_record_on_missing_db_fails_without_creating_the_file(tmp_path):
    the_store = store(tmp_path, initialize=False)
    with pytest.raises(ShadowStoreError, match="initialize"):
        the_store.record(record())
    assert not the_store.path.exists()


def test_failed_write_back_validation_rolls_the_whole_insert_back(tmp_path, monkeypatch):
    the_store = store(tmp_path)

    def broken_restore(row):
        raise ValueError("simulated mid-transaction corruption")

    monkeypatch.setattr(shadow_store, "_record_from_row", broken_restore)
    with pytest.raises(ShadowIntegrityError, match="read-back"):
        the_store.record(record())
    monkeypatch.undo()
    assert the_store.list_records() == ()  # nothing half-written survived


def test_conflicting_insert_leaves_the_existing_record_untouched(tmp_path):
    the_store = store(tmp_path)
    saved = the_store.record(record())
    with pytest.raises(ShadowExecutionConflictError):
        the_store.record(record())
    assert the_store.get_by_execution_request_id("exec-1") == saved


# --- read paths never create files (dry-run safe) --------------------------------


def test_reads_on_missing_db_return_empty_without_creating_the_file(tmp_path):
    the_store = store(tmp_path, initialize=False)
    assert the_store.list_records() == ()
    assert the_store.get_by_execution_request_id("exec-1") is None
    assert the_store.get_by_signal_id("signal-1") is None
    assert not the_store.path.exists()
    assert list(tmp_path.iterdir()) == []


def test_reads_reject_empty_keys(tmp_path):
    the_store = store(tmp_path)
    with pytest.raises(ShadowStoreError, match="execution_request_id"):
        the_store.get_by_execution_request_id("  ")
    with pytest.raises(ShadowStoreError, match="signal_id"):
        the_store.get_by_signal_id("")


def test_reads_do_not_modify_an_existing_db(tmp_path):
    the_store = store(tmp_path)
    the_store.record(record())
    before = file_hash(the_store.path)
    the_store.list_records()
    the_store.get_by_execution_request_id("exec-1")
    the_store.get_by_signal_id("signal-1")
    assert file_hash(the_store.path) == before


# --- schema policy ----------------------------------------------------------------


def test_initialize_is_idempotent(tmp_path):
    the_store = store(tmp_path)
    the_store.record(record())
    the_store.initialize()  # verifies, does not recreate
    assert len(the_store.list_records()) == 1


def test_initialize_refuses_an_empty_db_with_a_version(tmp_path):
    path = tmp_path / "shadow.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 7")
    before = file_hash(path)
    with pytest.raises(ShadowSchemaError, match="not a pristine DB"):
        SQLiteShadowExecutionStore(path).initialize()
    assert file_hash(path) == before


def test_foreign_schema_is_refused_unchanged_on_read_write_and_initialize(tmp_path):
    path = tmp_path / "shadow.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE paper_signals (id INTEGER PRIMARY KEY)")
    before = file_hash(path)
    the_store = SQLiteShadowExecutionStore(path)
    with pytest.raises(ShadowSchemaError):
        the_store.initialize()
    with pytest.raises(ShadowSchemaError):
        the_store.record(record())
    with pytest.raises(ShadowSchemaError):
        the_store.list_records()
    assert file_hash(path) == before


def test_an_unexpected_trigger_fails_verification(tmp_path):
    the_store = store(tmp_path)
    with sqlite3.connect(the_store.path) as connection:
        connection.execute(
            "CREATE TRIGGER sneaky AFTER INSERT ON shadow_executions BEGIN "
            "DELETE FROM shadow_executions; END"
        )
    with pytest.raises(ShadowSchemaError, match="unexpected trigger"):
        the_store.list_records()


def test_a_version_mismatch_fails_verification(tmp_path):
    the_store = store(tmp_path)
    with sqlite3.connect(the_store.path) as connection:
        connection.execute("PRAGMA user_version = 9")
    with pytest.raises(ShadowSchemaError, match="user_version"):
        the_store.list_records()


# --- restore-time strictness -------------------------------------------------------


def _tamper(path, sql, parameters=()):
    with sqlite3.connect(path) as connection:
        connection.execute(sql, parameters)


def test_unknown_rejection_code_is_refused_on_restore(tmp_path):
    the_store = store(tmp_path)
    the_store.record(record(the_decision=rejected_decision()))
    _tamper(
        the_store.path,
        "UPDATE shadow_executions SET rejections_json = ?",
        (json.dumps([{"code": "made_up_code", "message": "x"}]),),
    )
    with pytest.raises(ShadowIntegrityError):
        the_store.get_by_execution_request_id("exec-1")


def test_extra_rejection_json_keys_are_refused_on_restore(tmp_path):
    the_store = store(tmp_path)
    the_store.record(record(the_decision=rejected_decision()))
    _tamper(
        the_store.path,
        "UPDATE shadow_executions SET rejections_json = ?",
        (json.dumps([{"code": "kill_switch_enabled", "message": "x", "extra": 1}]),),
    )
    with pytest.raises(ShadowIntegrityError):
        the_store.get_by_execution_request_id("exec-1")


def test_broken_timestamp_is_refused_on_restore(tmp_path):
    the_store = store(tmp_path)
    the_store.record(record())
    _tamper(
        the_store.path,
        "UPDATE shadow_executions SET checked_at_utc = 'not-a-time+00:00'",
    )
    with pytest.raises(ShadowIntegrityError):
        the_store.get_by_execution_request_id("exec-1")


def test_a_tampered_intended_order_is_refused_on_restore(tmp_path):
    the_store = store(tmp_path)
    the_store.record(record())
    forged = json.dumps(
        {
            "symbol": "USD_JPY",
            "side": "buy",
            "order_type": "market",
            "quantity": "9999",
            "limit_price": None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    _tamper(
        the_store.path,
        "UPDATE shadow_executions SET intended_order_json = ?",
        (forged,),
    )
    with pytest.raises(ShadowIntegrityError, match="intended order"):
        the_store.get_by_execution_request_id("exec-1")


def test_db_check_constraints_fire_on_forged_rows(tmp_path):
    the_store = store(tmp_path)
    the_store.record(record())
    with sqlite3.connect(the_store.path) as connection:
        # A broker-style execution mode is unrepresentable in this table.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE shadow_executions SET execution_mode = 'fake'")
        # allow/reject consistency is a DB-level invariant too.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE shadow_executions SET allowed = 0")
