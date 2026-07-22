"""Issue #75: SQLite audit store for Safety Gate decisions.

tmp_path SQLite only -- no network, no Discord, no S3, no real DB. Broken or
foreign schemas are rejected loudly and WITHOUT modifying the file, matching
the execution_store stance (PR #91).
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from broker.broker_models import AccountState, OrderSide, OrderType
from broker.fake_broker import FakeBroker
from execution.execution_models import ExecutionRequest
from execution.live_submission import LiveOrderSubmissionService
from execution.safety_gate import (
    BrokerConnection,
    ExecutionMode,
    MarketSafetySnapshot,
    SafetyDecision,
    SafetyGate,
    SafetyPolicy,
    SafetyRejection,
    SafetyRejectionCode,
    StaticSafetyState,
)
from execution.safety_store import (
    SafetyIntegrityError,
    SafetySchemaError,
    SafetyStoreError,
    SQLiteSafetyDecisionStore,
)

NOW = datetime(2026, 7, 22, 1, 0, tzinfo=timezone.utc)


def rejected_decision(**overrides):
    values = dict(
        execution_request_id="exec-1",
        allowed=False,
        checked_at_utc=NOW,
        policy_id="policy-test-1",
        rejections=(
            SafetyRejection(SafetyRejectionCode.KILL_SWITCH_ENABLED, "kill switch is enabled"),
            SafetyRejection(SafetyRejectionCode.MARKET_CLOSED, "market is closed"),
        ),
        execution_mode="fake",
        connection_target="fake",
        account_id="fake-account",
        position_count=2,
    )
    values.update(overrides)
    return SafetyDecision(**values)


def allowed_decision(**overrides):
    values = dict(
        execution_request_id="exec-ok",
        allowed=True,
        checked_at_utc=NOW,
        policy_id="policy-test-1",
        execution_mode="fake",
        connection_target="fake",
        account_id="fake-account",
        position_count=0,
    )
    values.update(overrides)
    return SafetyDecision(**values)


def store_at(tmp_path):
    store = SQLiteSafetyDecisionStore(tmp_path / "safety.sqlite3")
    store.initialize()
    return store


# --- round trips (including a simulated process restart) ---------------------


def test_rejected_decision_round_trips_after_reopen_with_machine_readable_codes(tmp_path):
    path = tmp_path / "safety.sqlite3"
    store_at(tmp_path).record(rejected_decision())

    # Simulated restart: a brand-new store instance over the same file.
    restored = SQLiteSafetyDecisionStore(path).list_for_request("exec-1")

    assert restored == (rejected_decision(),)
    assert restored[0].rejection_codes == ("kill_switch_enabled", "market_closed")
    assert restored[0].policy_id == "policy-test-1"


def test_allowed_decision_round_trips_after_reopen(tmp_path):
    path = tmp_path / "safety.sqlite3"
    store_at(tmp_path).record(allowed_decision())
    assert SQLiteSafetyDecisionStore(path).list_for_request("exec-ok") == (allowed_decision(),)


def test_decisions_for_one_request_are_returned_in_recording_order(tmp_path):
    store = store_at(tmp_path)
    first = rejected_decision()
    second = rejected_decision(
        checked_at_utc=NOW + timedelta(seconds=1),
        rejections=(SafetyRejection(SafetyRejectionCode.MARKET_CLOSED, "market is closed"),),
    )
    store.record(first)
    store.record(second)
    assert store.list_for_request("exec-1") == (first, second)
    assert store.list_for_request("exec-unrelated") == ()


# --- missing DB: fail closed on write, no side effects on read ---------------


def test_record_on_missing_db_raises_and_creates_no_file(tmp_path):
    path = tmp_path / "never-created.sqlite3"
    with pytest.raises(SafetyStoreError, match="does not exist"):
        SQLiteSafetyDecisionStore(path).record(rejected_decision())
    assert not path.exists()


def test_list_on_missing_db_returns_empty_and_creates_no_file(tmp_path):
    path = tmp_path / "never-created.sqlite3"
    assert SQLiteSafetyDecisionStore(path).list_for_request("exec-1") == ()
    assert not path.exists()


def test_record_rejects_non_decision_values(tmp_path):
    store = store_at(tmp_path)
    with pytest.raises(SafetyStoreError, match="SafetyDecision"):
        store.record({"allowed": True})


# --- schema management -------------------------------------------------------


def test_initialize_is_idempotent(tmp_path):
    store = store_at(tmp_path)
    store.initialize()
    store.record(allowed_decision())


def test_initialize_refuses_a_version_only_db_without_modifying_it(tmp_path):
    path = tmp_path / "safety.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 7")
    before = path.read_bytes()
    with pytest.raises(SafetySchemaError, match="not a pristine DB"):
        SQLiteSafetyDecisionStore(path).initialize()
    assert path.read_bytes() == before


def test_foreign_schema_is_rejected_without_modification_by_every_api(tmp_path):
    path = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
    before = path.read_bytes()
    store = SQLiteSafetyDecisionStore(path)
    with pytest.raises(SafetySchemaError):
        store.initialize()
    with pytest.raises(SafetySchemaError):
        store.record(rejected_decision())
    with pytest.raises(SafetySchemaError):
        store.list_for_request("exec-1")
    assert path.read_bytes() == before


def test_constraint_stripped_lookalike_schema_is_rejected(tmp_path):
    path = tmp_path / "lookalike.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE safety_gate_decisions ("
            "decision_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "execution_request_id TEXT, allowed INTEGER, checked_at_utc TEXT, "
            "policy_id TEXT, execution_mode TEXT, connection_target TEXT, "
            "rejections_json TEXT, account_id TEXT, position_count INTEGER)"
        )
        connection.execute(
            "CREATE INDEX idx_safety_gate_decisions_request ON "
            "safety_gate_decisions(execution_request_id, decision_id)"
        )
        connection.execute("PRAGMA user_version = 1")
    store = SQLiteSafetyDecisionStore(path)
    with pytest.raises(SafetySchemaError):
        store.record(rejected_decision())


def test_unexpected_trigger_is_rejected(tmp_path):
    store = store_at(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "CREATE TRIGGER sneak AFTER INSERT ON safety_gate_decisions "
            "BEGIN DELETE FROM safety_gate_decisions; END"
        )
    with pytest.raises(SafetySchemaError, match="unexpected trigger"):
        store.record(rejected_decision())
    with pytest.raises(SafetySchemaError):
        store.list_for_request("exec-1")


def test_wrong_schema_version_is_rejected(tmp_path):
    store = store_at(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(SafetySchemaError, match="user_version"):
        store.record(rejected_decision())


# --- restore-time integrity --------------------------------------------------


def _tamper(store, sql, params=()):
    with sqlite3.connect(store.path) as connection:
        connection.execute(sql, params)


def test_unknown_rejection_code_in_stored_row_is_rejected(tmp_path):
    store = store_at(tmp_path)
    store.record(rejected_decision())
    _tamper(
        store,
        "UPDATE safety_gate_decisions SET rejections_json = ?",
        ('[{"code":"totally_new_code","message":"x"}]',),
    )
    with pytest.raises(SafetyIntegrityError):
        store.list_for_request("exec-1")


def test_rejection_entry_with_unexpected_keys_is_rejected(tmp_path):
    store = store_at(tmp_path)
    store.record(rejected_decision())
    _tamper(
        store,
        "UPDATE safety_gate_decisions SET rejections_json = ?",
        ('[{"code":"market_closed","message":"x","extra":1}]',),
    )
    with pytest.raises(SafetyIntegrityError):
        store.list_for_request("exec-1")


def test_unparseable_stored_timestamp_is_rejected(tmp_path):
    store = store_at(tmp_path)
    store.record(rejected_decision())
    _tamper(store, "UPDATE safety_gate_decisions SET checked_at_utc = 'garbage+00:00'")
    with pytest.raises(SafetyIntegrityError):
        store.list_for_request("exec-1")


def test_db_check_constraints_reject_inconsistent_rows_at_write_time(tmp_path):
    store = store_at(tmp_path)
    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            # allowed row carrying rejections violates the consistency CHECK
            connection.execute(
                "INSERT INTO safety_gate_decisions "
                "(execution_request_id, allowed, checked_at_utc, policy_id, rejections_json) "
                "VALUES ('exec-x', 1, '2026-07-22T01:00:00+00:00', 'p', "
                "'[{\"code\":\"market_closed\",\"message\":\"x\"}]')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            # naive timestamp violates the UTC-suffix CHECK
            connection.execute(
                "INSERT INTO safety_gate_decisions "
                "(execution_request_id, allowed, checked_at_utc, policy_id, rejections_json) "
                "VALUES ('exec-x', 1, '2026-07-22T01:00:00', 'p', '[]')"
            )


# --- integration with the sanctioned submission path -------------------------


def _submit_with_path(tmp_path, *, initialized=True, kill_switch=False):
    decision_db_path = tmp_path / "safety.sqlite3"
    if initialized:
        SQLiteSafetyDecisionStore(decision_db_path).initialize()
    broker = FakeBroker(default_fill_price=Decimal("150"), clock=lambda: NOW)
    broker.set_account_state(
        AccountState(
            account_id="fake-account",
            currency="USD",
            balance=Decimal("100000"),
            equity=Decimal("100000"),
            margin_available=Decimal("100000"),
            retrieved_at_utc=NOW,
        )
    )
    request = ExecutionRequest(
        execution_request_id="exec-flow-1",
        signal_id="signal-1",
        strategy_id="strategy-1",
        symbol="USD_JPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        requested_at_utc=NOW,
        stop_loss_price=Decimal("149.5"),
        take_profit_price=Decimal("151.5"),
    )
    gate = SafetyGate(
        policy=SafetyPolicy(
            policy_id="policy-test-1",
            allowed_symbols=frozenset({"USD_JPY"}),
            allowed_sides=frozenset({OrderSide.BUY, OrderSide.SELL}),
            max_order_quantity=Decimal("10"),
            max_positions_per_symbol=2,
            max_positions_account=5,
            max_reference_age=timedelta(seconds=30),
            max_spread_by_symbol={"USD_JPY": Decimal("0.05")},
            allowed_connection_targets={ExecutionMode.FAKE: frozenset({"fake"})},
        ),
        state_provider=StaticSafetyState(kill_switch_enabled=kill_switch),
        clock=lambda: NOW,
    )
    market = MarketSafetySnapshot(
        symbol="USD_JPY",
        reference_price=Decimal("150"),
        bid=Decimal("149.99"),
        ask=Decimal("150.01"),
        observed_at_utc=NOW,
        market_open=True,
        tradable=True,
    )
    service = LiveOrderSubmissionService(
        connection=BrokerConnection(broker=broker),
        gate=gate,
        decision_db_path=decision_db_path,
        order_db_path=tmp_path / "orders.sqlite3",
        clock=lambda: NOW,
    )
    service.register(request, client_order_id="client-flow-1")
    decision, result = service.submit(request.execution_request_id, market=market)
    return decision, result, broker


def test_rejection_audit_survives_a_store_reopen(tmp_path):
    path = tmp_path / "safety.sqlite3"
    decision, result, broker = _submit_with_path(tmp_path, kill_switch=True)
    assert result is None
    assert broker.submitted_requests == []

    restored = SQLiteSafetyDecisionStore(path).list_for_request("exec-flow-1")
    assert restored == (decision,)
    assert "kill_switch_enabled" in restored[0].rejection_codes


def test_allow_audit_survives_a_store_reopen(tmp_path):
    path = tmp_path / "safety.sqlite3"
    decision, result, broker = _submit_with_path(tmp_path)
    assert decision.allowed is True
    assert result is not None
    assert len(broker.submitted_requests) == 1
    assert SQLiteSafetyDecisionStore(path).list_for_request("exec-flow-1") == (decision,)


def test_uninitialized_store_fails_closed_through_the_submission_path(tmp_path):
    decision, result, broker = _submit_with_path(tmp_path, initialized=False)
    assert decision.allowed is False
    assert decision.rejection_codes == (SafetyRejectionCode.DECISION_PERSIST_FAILED.value,)
    assert result is None
    assert broker.submitted_requests == []
