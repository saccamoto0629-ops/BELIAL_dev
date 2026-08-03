"""Issue #78: the shadow result store -- pristine-only initialization,
closed-world schema verification, UNIQUE-claimed atomic INSERT, strict
restore-time validation, and the read-only / missing-DB contracts."""

import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from broker.broker_models import OrderType
from execution import shadow_store
from execution.shadow_models import intended_order_from_request, ShadowResult
from shadow_helpers import NOW, make_account, make_gate, make_market, make_request


def db(tmp_path):
    return tmp_path / "shadow.sqlite3"


def make_result(request=None, *, client_order_id="client-1", market=None, decision=None,
                safety_decision_id=1, timeframe="H1"):
    request = request if request is not None else make_request()
    market = market if market is not None else make_market()
    gate = make_gate()
    if decision is None:
        decision = gate.evaluate_shadow(
            request, market=market, account=make_account(), connection_target="shadow"
        )
    return ShadowResult(
        request=request,
        client_order_id=client_order_id,
        timeframe=timeframe,
        decision=decision,
        intended_order=intended_order_from_request(request, client_order_id=client_order_id),
        market=market,
        policy_snapshot=gate.policy.to_snapshot(),
        safety_decision_id=safety_decision_id,
        created_at_utc=NOW,
    )


def recorded(tmp_path, result=None):
    shadow_store.init_db(db(tmp_path))
    result = result if result is not None else make_result()
    row_id = shadow_store.record_shadow_result(result, db_path=db(tmp_path))
    return replace(result, shadow_result_id=row_id)


# --- create / read round-trip -------------------------------------------------


def test_record_and_read_back_roundtrip(tmp_path):
    stored = recorded(tmp_path)
    by_request = shadow_store.get_shadow_result("exec-1", db_path=db(tmp_path))
    by_signal = shadow_store.get_shadow_result_by_signal_id("signal-1", db_path=db(tmp_path))
    assert by_request == stored
    assert by_signal == stored
    assert by_request.request.quantity == Decimal("1")
    assert by_request.policy_snapshot["policy_id"] == "shadow-test-policy"


def test_limit_order_roundtrip_preserves_decimals(tmp_path):
    request = make_request(
        order_type=OrderType.LIMIT,
        requested_price=Decimal("150.1000"),
        stop_loss_price=Decimal("149.50"),
        take_profit_price=Decimal("151.50"),
    )
    stored = recorded(tmp_path, make_result(request))
    restored = shadow_store.get_shadow_result("exec-1", db_path=db(tmp_path))
    assert restored == stored
    assert str(restored.request.requested_price) == "150.1000"
    assert restored.intended_order.limit_price == Decimal("150.1000")


def test_rejected_result_roundtrips_codes_and_messages(tmp_path):
    market = make_market(market_open=False)
    stored = recorded(tmp_path, make_result(market=market))
    restored = shadow_store.get_shadow_result("exec-1", db_path=db(tmp_path))
    assert restored == stored
    assert restored.allowed is False
    assert "market_closed" in restored.rejection_codes
    assert any("closed" in item.message for item in restored.decision.rejections)


def test_list_filters_by_decision(tmp_path):
    shadow_store.init_db(db(tmp_path))
    allowed = make_result()
    rejected = make_result(
        make_request(execution_request_id="exec-2", signal_id="signal-2"),
        client_order_id="client-2",
        market=make_market(market_open=False),
    )
    shadow_store.record_shadow_result(allowed, db_path=db(tmp_path))
    shadow_store.record_shadow_result(rejected, db_path=db(tmp_path))
    assert len(shadow_store.list_shadow_results(db_path=db(tmp_path))) == 2
    only_rejected = shadow_store.list_shadow_results(allowed=False, db_path=db(tmp_path))
    assert [r.request.execution_request_id for r in only_rejected] == ["exec-2"]
    only_allowed = shadow_store.list_shadow_results(allowed=True, db_path=db(tmp_path))
    assert [r.request.execution_request_id for r in only_allowed] == ["exec-1"]


# --- unique constraints / conflict classification -----------------------------


def test_duplicate_execution_request_id_conflicts_without_second_row(tmp_path):
    recorded(tmp_path)
    duplicate = make_result(
        make_request(signal_id="signal-other"), client_order_id="client-other"
    )
    with pytest.raises(shadow_store.ShadowResultConflictError) as info:
        shadow_store.record_shadow_result(duplicate, db_path=db(tmp_path))
    assert info.value.existing_execution_request_id == "exec-1"
    assert len(shadow_store.list_shadow_results(db_path=db(tmp_path))) == 1


def test_duplicate_signal_id_conflicts(tmp_path):
    recorded(tmp_path)
    duplicate = make_result(
        make_request(execution_request_id="exec-other"), client_order_id="client-other"
    )
    with pytest.raises(shadow_store.ShadowResultConflictError):
        shadow_store.record_shadow_result(duplicate, db_path=db(tmp_path))


def test_duplicate_client_order_id_conflicts(tmp_path):
    recorded(tmp_path)
    duplicate = make_result(
        make_request(execution_request_id="exec-other", signal_id="signal-other"),
        client_order_id="client-1",
    )
    with pytest.raises(shadow_store.ShadowResultConflictError):
        shadow_store.record_shadow_result(duplicate, db_path=db(tmp_path))


def test_out_of_band_duplicate_insert_is_refused_by_the_schema(tmp_path):
    recorded(tmp_path)
    connection = sqlite3.connect(db(tmp_path))
    row = connection.execute(
        f"SELECT {', '.join(shadow_store._RESULT_COLUMNS)} FROM {shadow_store.RESULTS_TABLE}"
    ).fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            f"INSERT INTO {shadow_store.RESULTS_TABLE} "
            f"({', '.join(shadow_store._RESULT_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in shadow_store._RESULT_COLUMNS)})",
            row,
        )
    connection.close()


# --- missing DB / read-only contracts -----------------------------------------


def test_record_refuses_a_missing_db_and_creates_nothing(tmp_path):
    with pytest.raises(shadow_store.ShadowStoreError, match="does not exist"):
        shadow_store.record_shadow_result(make_result(), db_path=db(tmp_path))
    assert not db(tmp_path).exists()


def test_reads_never_create_a_missing_db(tmp_path):
    assert shadow_store.get_shadow_result("exec-1", db_path=db(tmp_path)) is None
    assert shadow_store.get_shadow_result_by_signal_id("signal-1", db_path=db(tmp_path)) is None
    assert shadow_store.list_shadow_results(db_path=db(tmp_path)) == ()
    assert not db(tmp_path).exists()


def test_verify_db_probe_is_read_only(tmp_path):
    with pytest.raises(shadow_store.ShadowStoreError, match="does not exist"):
        shadow_store.verify_db(db(tmp_path))
    assert not db(tmp_path).exists()
    shadow_store.init_db(db(tmp_path))
    before = db(tmp_path).read_bytes()
    shadow_store.verify_db(db(tmp_path))
    assert db(tmp_path).read_bytes() == before


# --- schema management --------------------------------------------------------


def test_init_db_is_idempotent(tmp_path):
    shadow_store.init_db(db(tmp_path))
    stored = recorded(tmp_path)
    shadow_store.init_db(db(tmp_path))  # verify-only second run
    assert shadow_store.get_shadow_result("exec-1", db_path=db(tmp_path)) == stored


def test_init_db_refuses_a_version_only_db(tmp_path):
    connection = sqlite3.connect(db(tmp_path))
    connection.execute("PRAGMA user_version = 7")
    connection.commit()
    connection.close()
    before = db(tmp_path).read_bytes()
    with pytest.raises(shadow_store.ShadowSchemaError, match="pristine"):
        shadow_store.init_db(db(tmp_path))
    assert db(tmp_path).read_bytes() == before


def test_init_db_refuses_a_foreign_schema_without_modification(tmp_path):
    connection = sqlite3.connect(db(tmp_path))
    connection.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    before = db(tmp_path).read_bytes()
    with pytest.raises(shadow_store.ShadowSchemaError):
        shadow_store.init_db(db(tmp_path))
    assert db(tmp_path).read_bytes() == before


def test_corrupted_schema_is_detected_on_read_and_write(tmp_path):
    shadow_store.init_db(db(tmp_path))
    connection = sqlite3.connect(db(tmp_path))
    connection.execute(f"CREATE TRIGGER sneaky AFTER INSERT ON {shadow_store.RESULTS_TABLE} "
                       f"BEGIN DELETE FROM {shadow_store.RESULTS_TABLE}; END")
    connection.commit()
    connection.close()
    before = db(tmp_path).read_bytes()
    with pytest.raises(shadow_store.ShadowSchemaError, match="unexpected"):
        shadow_store.list_shadow_results(db_path=db(tmp_path))
    with pytest.raises(shadow_store.ShadowSchemaError, match="unexpected"):
        shadow_store.record_shadow_result(make_result(), db_path=db(tmp_path))
    assert db(tmp_path).read_bytes() == before


def test_missing_check_constraint_lookalike_is_refused(tmp_path):
    shadow_store.init_db(db(tmp_path))
    connection = sqlite3.connect(db(tmp_path))
    ddl = _weakened_ddl()
    connection.executescript(
        f"DROP TABLE {shadow_store.RESULTS_TABLE}; {ddl}"
    )
    connection.commit()
    connection.close()
    with pytest.raises(shadow_store.ShadowSchemaError):
        shadow_store.list_shadow_results(db_path=db(tmp_path))


def _weakened_ddl() -> str:
    # Same columns, but the allow/reject consistency CHECK removed.
    ddl = shadow_store._create_results_table_sql()
    weakened = ddl.replace("CHECK ((allowed = 1) = (rejections_json = '[]')),", "")
    assert weakened != ddl
    return weakened


def test_failed_insert_rolls_back_completely(tmp_path):
    shadow_store.init_db(db(tmp_path))
    # Violates the DB CHECK (execution_mode must be 'shadow') only at the SQL
    # layer: build a valid result, then corrupt the record via monkeypatching
    # is impossible for a frozen model -- instead check rollback via the
    # duplicate path: after a conflict, the row count is unchanged.
    stored = recorded(tmp_path)
    duplicate = make_result(
        make_request(signal_id="signal-other"), client_order_id="client-other"
    )
    with pytest.raises(shadow_store.ShadowResultConflictError):
        shadow_store.record_shadow_result(duplicate, db_path=db(tmp_path))
    results = shadow_store.list_shadow_results(db_path=db(tmp_path))
    assert results == (stored,)


# --- restore-time validation --------------------------------------------------


def _update_row(tmp_path, sql, params=()):
    connection = sqlite3.connect(db(tmp_path))
    connection.execute(sql, params)
    connection.commit()
    connection.close()


def test_out_of_band_column_edit_is_detected(tmp_path):
    recorded(tmp_path)
    _update_row(
        tmp_path,
        f"UPDATE {shadow_store.RESULTS_TABLE} SET quantity = '999'",
    )
    with pytest.raises(shadow_store.ShadowIntegrityError, match="quantity"):
        shadow_store.get_shadow_result("exec-1", db_path=db(tmp_path))


def test_unknown_rejection_code_is_refused_on_restore(tmp_path):
    market = make_market(market_open=False)
    recorded(tmp_path, make_result(market=market))
    _update_row(
        tmp_path,
        f"UPDATE {shadow_store.RESULTS_TABLE} SET rejections_json = ?",
        ('[{"code":"not_a_real_code","message":"x"}]',),
    )
    with pytest.raises(shadow_store.ShadowIntegrityError):
        shadow_store.get_shadow_result("exec-1", db_path=db(tmp_path))


def test_invalid_json_payload_is_refused_on_restore(tmp_path):
    recorded(tmp_path)
    _update_row(
        tmp_path,
        f"UPDATE {shadow_store.RESULTS_TABLE} SET request_json = ?",
        ('{"only":"partial"}',),
    )
    with pytest.raises(shadow_store.ShadowIntegrityError, match="request_json"):
        shadow_store.get_shadow_result("exec-1", db_path=db(tmp_path))


def test_db_check_constraints_fire_for_broken_rows(tmp_path):
    recorded(tmp_path)
    connection = sqlite3.connect(db(tmp_path))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            f"UPDATE {shadow_store.RESULTS_TABLE} SET execution_mode = 'live'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            f"UPDATE {shadow_store.RESULTS_TABLE} SET allowed = 0"
        )  # allowed row with empty rejections_json
    connection.close()
