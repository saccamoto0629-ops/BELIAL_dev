"""Issue #78: the shadow record store -- claim-first lifecycle (claim /
duplicate / conflict / takeover / completion), pristine-only initialization,
closed-world schema verification, strict restore-time validation (account
snapshot included), and the true read-only / missing-DB contracts."""

import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest

from broker.broker_models import OrderType
from execution import shadow_store
from shadow_helpers import (
    NOW,
    make_account,
    make_gate,
    make_market,
    make_position,
    make_provenance,
    make_request,
)


def db(tmp_path):
    return tmp_path / "shadow.sqlite3"


def claim(tmp_path, request=None, *, client_order_id="client-1", timeframe="H1",
          provenance=None, owner_id="owner-1", now=NOW, lease_seconds=600, init=True):
    if init:
        shadow_store.init_db(db(tmp_path))
    return shadow_store.claim_shadow_cycle(
        request if request is not None else make_request(),
        client_order_id=client_order_id,
        timeframe=timeframe,
        provenance=provenance if provenance is not None else make_provenance(),
        owner_id=owner_id,
        now=now,
        lease_seconds=lease_seconds,
        db_path=db(tmp_path),
    )


def complete(tmp_path, request=None, *, owner_id="owner-1", market=None, account=None,
             safety_decision_id=1, completed_at=NOW):
    request = request if request is not None else make_request()
    market = market if market is not None else make_market()
    account = account if account is not None else make_account()
    gate = make_gate()
    decision = gate.evaluate_shadow(
        request, market=market, account=account, connection_target="shadow"
    )
    return shadow_store.complete_shadow_cycle(
        request.execution_request_id,
        owner_id=owner_id,
        decision=decision,
        market=market,
        account=account,
        policy_snapshot=gate.policy.to_snapshot(),
        safety_decision_id=safety_decision_id,
        completed_at_utc=completed_at,
        db_path=db(tmp_path),
    )


def recorded(tmp_path, request=None, **kwargs):
    status, held = claim(tmp_path, request, **kwargs)
    assert status == shadow_store.CLAIM_ACQUIRED
    return complete(tmp_path, request, owner_id=kwargs.get("owner_id", "owner-1"))


# --- claim lifecycle ----------------------------------------------------------


def test_claim_then_complete_roundtrips(tmp_path):
    result = recorded(tmp_path)
    assert result.shadow_result_id == 1 and result.allowed
    restored = shadow_store.get_shadow_result("exec-1", db_path=db(tmp_path))
    assert restored == result
    assert restored.claim_owner_id == "owner-1"
    assert restored.provenance == make_provenance()
    assert restored.account == make_account()
    assert shadow_store.get_shadow_claim("exec-1", db_path=db(tmp_path)) is None


def test_claimed_row_is_visible_as_a_claim_not_a_result(tmp_path):
    status, held = claim(tmp_path)
    assert status == shadow_store.CLAIM_ACQUIRED
    assert shadow_store.get_shadow_result("exec-1", db_path=db(tmp_path)) is None
    live = shadow_store.get_shadow_claim("exec-1", db_path=db(tmp_path))
    assert live is not None and live.claim_owner_id == "owner-1"
    assert live.lease_expires_at_utc == NOW + timedelta(seconds=600)
    assert shadow_store.list_shadow_claims(db_path=db(tmp_path)) == (live,)


def test_same_payload_completed_row_is_a_duplicate(tmp_path):
    result = recorded(tmp_path)
    status, record = claim(tmp_path, init=False, owner_id="someone-else")
    assert status == shadow_store.CLAIM_DUPLICATE
    assert record == result


def test_same_owner_reacquires_its_own_claim_immediately(tmp_path):
    claim(tmp_path)
    status, held = claim(tmp_path, init=False, now=NOW + timedelta(seconds=1))
    assert status == shadow_store.CLAIM_ACQUIRED
    assert held.claimed_at_utc == NOW + timedelta(seconds=1)


def test_live_lease_of_another_owner_is_an_in_progress_duplicate(tmp_path):
    claim(tmp_path)
    status, record = claim(
        tmp_path, init=False, owner_id="owner-2", now=NOW + timedelta(seconds=10)
    )
    assert status == shadow_store.CLAIM_DUPLICATE
    assert record.claim_owner_id == "owner-1"


def test_expired_lease_is_taken_over(tmp_path):
    claim(tmp_path, lease_seconds=60)
    status, held = claim(
        tmp_path, init=False, owner_id="owner-2", now=NOW + timedelta(seconds=61)
    )
    assert status == shadow_store.CLAIM_ACQUIRED
    assert held.claim_owner_id == "owner-2"
    # ...and the original owner can no longer complete (it lost the cycle)
    with pytest.raises(shadow_store.ShadowStoreError, match="lost"):
        complete(tmp_path, owner_id="owner-1")


def test_completion_requires_an_existing_claim(tmp_path):
    shadow_store.init_db(db(tmp_path))
    with pytest.raises(shadow_store.ShadowStoreError, match="claim_shadow_cycle"):
        complete(tmp_path)


def test_completion_of_a_completed_cycle_is_refused(tmp_path):
    recorded(tmp_path)
    with pytest.raises(shadow_store.ShadowStoreError, match="already"):
        complete(tmp_path)


# --- duplicate vs conflict ----------------------------------------------------


def test_payload_mismatch_is_a_conflict_not_a_duplicate(tmp_path):
    recorded(tmp_path)
    with pytest.raises(shadow_store.ShadowIdentityConflictError) as info:
        claim(tmp_path, make_request(quantity=Decimal("2")), init=False)
    assert info.value.reason_code == "shadow_payload_mismatch"
    assert info.value.differing_fields == ("quantity",)
    assert len(shadow_store.list_shadow_results(db_path=db(tmp_path))) == 1


def test_conflict_when_a_different_client_order_id_reuses_the_request(tmp_path):
    recorded(tmp_path)
    with pytest.raises(shadow_store.ShadowIdentityConflictError) as info:
        claim(tmp_path, init=False, client_order_id="client-other")
    assert "client_order_id" in info.value.differing_fields


def test_conflict_when_identity_keys_span_different_rows(tmp_path):
    recorded(tmp_path)
    recorded(
        tmp_path,
        make_request(execution_request_id="exec-2", signal_id="signal-2"),
        client_order_id="client-2",
    )
    mixed = make_request(signal_id="signal-2")  # exec-1's id + signal-2's signal
    with pytest.raises(shadow_store.ShadowIdentityConflictError, match="DIFFERENT shadow rows"):
        claim(tmp_path, mixed, init=False, client_order_id="client-3")


def test_numerically_equal_decimal_spellings_are_duplicates(tmp_path):
    result = recorded(tmp_path)
    status, record = claim(
        tmp_path, make_request(quantity=Decimal("1.0")), init=False, owner_id="owner-2"
    )
    assert status == shadow_store.CLAIM_DUPLICATE and record == result


def test_conflicting_origin_is_a_conflict(tmp_path):
    recorded(tmp_path)
    with pytest.raises(shadow_store.ShadowIdentityConflictError) as info:
        claim(
            tmp_path, init=False,
            provenance=make_provenance(input_origin="cli_fixture"),
        )
    assert info.value.differing_fields == ("input_origin",)


# --- read-only / missing-DB contracts ----------------------------------------


def test_claim_refuses_a_missing_db_and_creates_nothing(tmp_path):
    with pytest.raises(shadow_store.ShadowStoreError, match="does not exist"):
        claim(tmp_path, init=False)
    assert not db(tmp_path).exists()


def test_reads_never_create_a_missing_db(tmp_path):
    assert shadow_store.get_shadow_result("exec-1", db_path=db(tmp_path)) is None
    assert shadow_store.get_shadow_result_by_signal_id("signal-1", db_path=db(tmp_path)) is None
    assert shadow_store.get_shadow_result_by_decision_id(1, db_path=db(tmp_path)) is None
    assert shadow_store.get_shadow_claim("exec-1", db_path=db(tmp_path)) is None
    assert shadow_store.list_shadow_results(db_path=db(tmp_path)) == ()
    assert shadow_store.list_shadow_claims(db_path=db(tmp_path)) == ()
    assert not db(tmp_path).exists()


def test_reads_leave_bytes_and_mtime_untouched(tmp_path):
    recorded(tmp_path)
    path = db(tmp_path)
    before_bytes = path.read_bytes()
    before_stat = (path.stat().st_mtime_ns, path.stat().st_size)
    shadow_store.verify_db(path)
    shadow_store.list_shadow_results(db_path=path)
    shadow_store.get_shadow_result("exec-1", db_path=path)
    assert path.read_bytes() == before_bytes
    assert (path.stat().st_mtime_ns, path.stat().st_size) == before_stat
    # no journal / WAL / SHM side files appeared
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["shadow.sqlite3"]


# --- lookups / listing --------------------------------------------------------


def test_lookup_by_signal_and_decision_id(tmp_path):
    result = recorded(tmp_path)
    assert shadow_store.get_shadow_result_by_signal_id("signal-1", db_path=db(tmp_path)) == result
    assert shadow_store.get_shadow_result_by_decision_id(1, db_path=db(tmp_path)) == result
    assert shadow_store.get_shadow_result_by_decision_id(99, db_path=db(tmp_path)) is None


def test_list_orders_newest_first_and_honours_limit(tmp_path):
    recorded(tmp_path)
    recorded(
        tmp_path,
        make_request(execution_request_id="exec-2", signal_id="signal-2"),
        client_order_id="client-2",
    )
    results = shadow_store.list_shadow_results(db_path=db(tmp_path))
    assert [r.request.execution_request_id for r in results] == ["exec-2", "exec-1"]
    limited = shadow_store.list_shadow_results(limit=1, db_path=db(tmp_path))
    assert [r.request.execution_request_id for r in limited] == ["exec-2"]
    with pytest.raises(shadow_store.ShadowStoreError, match="limit"):
        shadow_store.list_shadow_results(limit=0, db_path=db(tmp_path))
    with pytest.raises(shadow_store.ShadowStoreError, match="limit"):
        shadow_store.list_shadow_results(limit=shadow_store.MAX_LIST_LIMIT + 1, db_path=db(tmp_path))


def test_list_filters_by_decision(tmp_path):
    recorded(tmp_path)
    request = make_request(execution_request_id="exec-2", signal_id="signal-2")
    status, _ = claim(tmp_path, request, init=False, client_order_id="client-2")
    assert status == shadow_store.CLAIM_ACQUIRED
    complete(tmp_path, request, market=make_market(market_open=False))
    rejected = shadow_store.list_shadow_results(allowed=False, db_path=db(tmp_path))
    assert [r.request.execution_request_id for r in rejected] == ["exec-2"]
    assert "market_closed" in rejected[0].rejection_codes


# --- limit-order round-trip / account snapshot --------------------------------


def test_limit_order_roundtrip_preserves_decimals(tmp_path):
    request = make_request(
        order_type=OrderType.LIMIT,
        requested_price=Decimal("150.1000"),
        stop_loss_price=Decimal("149.50"),
        take_profit_price=Decimal("151.50"),
    )
    stored = recorded(tmp_path, request)
    restored = shadow_store.get_shadow_result("exec-1", db_path=db(tmp_path))
    assert restored == stored
    assert str(restored.request.requested_price) == "150.1000"
    assert restored.intended_order.limit_price == Decimal("150.1000")


def test_account_snapshot_with_positions_roundtrips(tmp_path):
    account = make_account(positions=(make_position(), make_position(symbol="EUR_USD")))
    shadow_store.init_db(db(tmp_path))
    claim(tmp_path, init=False)
    complete(tmp_path, account=account)
    restored = shadow_store.get_shadow_result("exec-1", db_path=db(tmp_path))
    assert restored.account == account
    assert len(restored.account.positions) == 2
    assert str(restored.account.positions[0].quantity) == "1"


def test_no_secret_material_is_stored(tmp_path):
    recorded(tmp_path)
    raw = db(tmp_path).read_bytes()
    for needle in (b"token", b"password", b"secret_key", b"credential"):
        assert needle not in raw


# --- schema management --------------------------------------------------------


def test_init_db_is_idempotent(tmp_path):
    stored = recorded(tmp_path)
    shadow_store.init_db(db(tmp_path))  # verify-only second run
    assert shadow_store.get_shadow_result("exec-1", db_path=db(tmp_path)) == stored


def test_init_db_refuses_version_only_and_foreign_schemas(tmp_path):
    connection = sqlite3.connect(db(tmp_path))
    connection.execute("PRAGMA user_version = 7")
    connection.commit()
    connection.close()
    before = db(tmp_path).read_bytes()
    with pytest.raises(shadow_store.ShadowSchemaError, match="pristine"):
        shadow_store.init_db(db(tmp_path))
    assert db(tmp_path).read_bytes() == before

    other = tmp_path / "foreign.sqlite3"
    connection = sqlite3.connect(other)
    connection.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    before = other.read_bytes()
    with pytest.raises(shadow_store.ShadowSchemaError):
        shadow_store.init_db(other)
    assert other.read_bytes() == before


def test_corrupted_schema_is_detected_on_read_and_write(tmp_path):
    shadow_store.init_db(db(tmp_path))
    connection = sqlite3.connect(db(tmp_path))
    connection.execute(
        f"CREATE TRIGGER sneaky AFTER INSERT ON {shadow_store.RESULTS_TABLE} "
        f"BEGIN DELETE FROM {shadow_store.RESULTS_TABLE}; END"
    )
    connection.commit()
    connection.close()
    before = db(tmp_path).read_bytes()
    with pytest.raises(shadow_store.ShadowSchemaError, match="unexpected"):
        shadow_store.list_shadow_results(db_path=db(tmp_path))
    with pytest.raises(shadow_store.ShadowSchemaError, match="unexpected"):
        claim(tmp_path, init=False)
    assert db(tmp_path).read_bytes() == before


def test_missing_check_constraint_lookalike_is_refused(tmp_path):
    shadow_store.init_db(db(tmp_path))
    ddl = shadow_store._create_results_table_sql()
    weakened = ddl.replace("CHECK (allowed IS NULL OR ((allowed = 1) = (rejections_json = '[]'))),", "")
    assert weakened != ddl
    connection = sqlite3.connect(db(tmp_path))
    connection.executescript(f"DROP TABLE {shadow_store.RESULTS_TABLE}; {weakened}")
    connection.commit()
    connection.close()
    with pytest.raises(shadow_store.ShadowSchemaError):
        shadow_store.list_shadow_results(db_path=db(tmp_path))


# --- restore-time validation --------------------------------------------------


def _update_row(tmp_path, sql, params=()):
    connection = sqlite3.connect(db(tmp_path))
    connection.execute(sql, params)
    connection.commit()
    connection.close()


def test_out_of_band_column_edit_is_detected(tmp_path):
    recorded(tmp_path)
    _update_row(tmp_path, f"UPDATE {shadow_store.RESULTS_TABLE} SET quantity = '999'")
    with pytest.raises(shadow_store.ShadowIntegrityError, match="quantity|canonical"):
        shadow_store.get_shadow_result("exec-1", db_path=db(tmp_path))


def test_account_snapshot_projection_mismatch_is_detected(tmp_path):
    shadow_store.init_db(db(tmp_path))
    claim(tmp_path, init=False)
    complete(tmp_path, account=make_account(positions=(make_position(),)))
    _update_row(tmp_path, f"UPDATE {shadow_store.RESULTS_TABLE} SET position_count = 5")
    with pytest.raises(shadow_store.ShadowIntegrityError, match="position_count"):
        shadow_store.get_shadow_result("exec-1", db_path=db(tmp_path))


def test_tampered_account_snapshot_json_is_detected(tmp_path):
    recorded(tmp_path)
    _update_row(
        tmp_path,
        f"UPDATE {shadow_store.RESULTS_TABLE} SET account_snapshot_json = ?",
        ('{"only":"partial"}',),
    )
    with pytest.raises(shadow_store.ShadowIntegrityError, match="account_snapshot_json"):
        shadow_store.get_shadow_result("exec-1", db_path=db(tmp_path))


def test_unknown_rejection_code_is_refused_on_restore(tmp_path):
    shadow_store.init_db(db(tmp_path))
    claim(tmp_path, init=False)
    complete(tmp_path, market=make_market(market_open=False))
    _update_row(
        tmp_path,
        f"UPDATE {shadow_store.RESULTS_TABLE} SET rejections_json = ?",
        ('[{"code":"not_a_real_code","message":"x"}]',),
    )
    with pytest.raises(shadow_store.ShadowIntegrityError):
        shadow_store.get_shadow_result("exec-1", db_path=db(tmp_path))


def test_db_check_constraints_fire_for_broken_rows(tmp_path):
    recorded(tmp_path)
    connection = sqlite3.connect(db(tmp_path))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(f"UPDATE {shadow_store.RESULTS_TABLE} SET execution_mode = 'live'")
    with pytest.raises(sqlite3.IntegrityError):
        # completed rows must keep their completion columns
        connection.execute(f"UPDATE {shadow_store.RESULTS_TABLE} SET allowed = NULL")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(f"UPDATE {shadow_store.RESULTS_TABLE} SET input_origin = 'nowhere'")
    connection.close()
