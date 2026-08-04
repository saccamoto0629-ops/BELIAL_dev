"""Issue #103: SQLite audit persistence for broker reconciliation runs.

All DBs live under pytest's tmp_path. No network, no Discord, no S3, no
sleep, no real orders anywhere in this suite.
"""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from broker.broker_models import BrokerErrorCode, OrderState
from execution import reconciliation_store as store
from execution.reconciliation_models import (
    DiscrepancyType,
    OrderDiscrepancy,
    ReconciliationMode,
    ReconciliationRun,
    ReconciliationTargetType,
    ResolutionCategory,
)

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def db(tmp_path):
    return tmp_path / "broker_reconciliation.sqlite3"


def sample_discrepancies() -> tuple[OrderDiscrepancy, ...]:
    return (
        OrderDiscrepancy(
            discrepancy_type=DiscrepancyType.IN_FLIGHT_BROKER_FILLED,
            target_type=ReconciliationTargetType.EXECUTION_ORDER,
            target_id="exec-1",
            resolution_category=ResolutionCategory.AUTO_REPAIR_CANDIDATE,
            observed_at_utc=at(1),
            detail="broker reports filled, transition is legal",
            internal_state=OrderState.PENDING,
            broker_state=OrderState.FILLED,
            client_order_id="client-1",
            broker_order_id="B-1",
        ),
        OrderDiscrepancy(
            discrepancy_type=DiscrepancyType.QUERY_FAILURE,
            target_type=ReconciliationTargetType.EXECUTION_ORDER,
            target_id="exec-2",
            resolution_category=ResolutionCategory.QUERY_FAILED,
            observed_at_utc=at(2),
            detail="broker query timed out",
            internal_state=OrderState.UNKNOWN,
            client_order_id="client-2",
            broker_error_code=BrokerErrorCode.TIMEOUT,
        ),
        OrderDiscrepancy(
            discrepancy_type=DiscrepancyType.BROKER_ONLY_ORDER,
            target_type=ReconciliationTargetType.BROKER_ORDER,
            target_id="client-9",
            resolution_category=ResolutionCategory.MANUAL_REVIEW,
            observed_at_utc=at(3),
            detail="the broker holds an order BELIAL has no request for",
            broker_state=OrderState.FILLED,
            client_order_id="client-9",
            broker_order_id="B-9",
        ),
    )


def sample_run(run_id="run-1", **overrides) -> ReconciliationRun:
    kwargs = dict(
        run_id=run_id,
        mode=ReconciliationMode.DETECTION_ONLY,
        started_at_utc=T0,
        finished_at_utc=at(5),
        checked_order_count=4,
        discrepancies=sample_discrepancies(),
    )
    kwargs.update(overrides)
    return ReconciliationRun(**kwargs)


# --- schema / initialization --------------------------------------------------


def test_init_creates_the_schema_and_is_idempotent(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    store.init_reconciliation_db(path)  # verification only, no rebuild
    with closing(sqlite3.connect(path)) as connection:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert {store.RUNS_TABLE, store.DISCREPANCIES_TABLE} <= tables
    assert version == store.SCHEMA_VERSION


def test_re_initializing_keeps_every_stored_row(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    store.record_reconciliation_run(sample_run(), db_path=path)
    store.init_reconciliation_db(path)
    store.init_reconciliation_db(path)
    restored = store.get_reconciliation_run("run-1", db_path=path)
    assert restored == sample_run()


def test_a_foreign_or_partial_schema_is_refused_without_modification(tmp_path):
    path = db(tmp_path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
        connection.commit()
    before = path.read_bytes()
    with pytest.raises(store.ReconciliationSchemaError):
        store.init_reconciliation_db(path)
    assert path.read_bytes() == before


def test_an_empty_db_carrying_a_version_is_not_pristine(tmp_path):
    path = db(tmp_path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version = 7")
        connection.commit()
    with pytest.raises(store.ReconciliationSchemaError, match="not a pristine DB"):
        store.init_reconciliation_db(path)


def test_a_constraint_stripped_lookalike_fails_verification(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = ?",
            (
                f"CREATE TABLE {store.RUNS_TABLE} (run_id TEXT PRIMARY KEY NOT NULL, "
                "mode TEXT NOT NULL, started_at_utc TEXT NOT NULL, "
                "finished_at_utc TEXT NOT NULL, checked_order_count INTEGER NOT NULL, "
                "broker_listing_error TEXT) WITHOUT ROWID",
                store.RUNS_TABLE,
            ),
        )
        connection.execute("PRAGMA writable_schema = OFF")
        connection.commit()
    with pytest.raises(store.ReconciliationSchemaError):
        store.get_reconciliation_run("run-1", db_path=path)


def test_a_trigger_is_rejected_by_the_closed_world_verification(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            f"CREATE TRIGGER t AFTER INSERT ON {store.RUNS_TABLE} BEGIN "
            f"DELETE FROM {store.RUNS_TABLE}; END"
        )
        connection.commit()
    with pytest.raises(store.ReconciliationSchemaError, match="unexpected"):
        store.record_reconciliation_run(sample_run(), db_path=path)


# --- recording ----------------------------------------------------------------


def test_a_run_round_trips_with_every_field(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    original = sample_run(broker_listing_error=None)
    store.record_reconciliation_run(original, db_path=path)
    restored = store.get_reconciliation_run("run-1", db_path=path)
    assert restored == original
    # every classification field survived the round trip
    filled, failure, broker_only = restored.discrepancies
    assert filled.discrepancy_type is DiscrepancyType.IN_FLIGHT_BROKER_FILLED
    assert filled.target_id == "exec-1"
    assert (filled.internal_state, filled.broker_state) == (OrderState.PENDING, OrderState.FILLED)
    assert filled.resolution_category is ResolutionCategory.AUTO_REPAIR_CANDIDATE
    assert failure.broker_error_code is BrokerErrorCode.TIMEOUT
    assert broker_only.target_type is ReconciliationTargetType.BROKER_ORDER


def test_records_survive_a_process_restart(tmp_path):
    """A fresh connection (new process equivalent) reads the run back."""
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    store.record_reconciliation_run(sample_run(), db_path=path)
    store._REFERENCE_SCHEMA_CACHE = None  # rebuild the reference like a fresh import
    restored = store.get_reconciliation_run("run-1", db_path=path)
    assert restored == sample_run()
    assert store.list_reconciliation_runs(db_path=path) == (sample_run(),)


def test_a_run_without_discrepancies_is_recorded_too(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    empty = sample_run("run-empty", checked_order_count=2, discrepancies=())
    store.record_reconciliation_run(empty, db_path=path)
    assert store.get_reconciliation_run("run-empty", db_path=path) == empty


def test_the_broker_listing_error_is_persisted(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    run = sample_run("run-nolist", broker_listing_error="broker cannot enumerate orders")
    store.record_reconciliation_run(run, db_path=path)
    restored = store.get_reconciliation_run("run-nolist", db_path=path)
    assert restored.broker_listing_error == "broker cannot enumerate orders"
    assert restored.broker_orders_enumerated is False


def test_repeating_a_reconciliation_appends_a_new_run(tmp_path):
    """Duplication policy: identical observations are recorded per run, and
    an earlier run is never rewritten."""
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    store.record_reconciliation_run(sample_run("run-1"), db_path=path)
    store.record_reconciliation_run(sample_run("run-2", started_at_utc=at(60),
                                               finished_at_utc=at(65)), db_path=path)
    runs = store.list_reconciliation_runs(db_path=path)
    assert [r.run_id for r in runs] == ["run-2", "run-1"]  # newest first
    assert runs[0].discrepancies == runs[1].discrepancies
    assert store.get_reconciliation_run("run-1", db_path=path) == sample_run("run-1")


def test_recording_the_same_run_id_twice_is_refused_and_changes_nothing(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    store.record_reconciliation_run(sample_run(), db_path=path)
    before = path.read_bytes()
    with pytest.raises(store.ReconciliationRunConflictError):
        store.record_reconciliation_run(sample_run(checked_order_count=99), db_path=path)
    assert path.read_bytes() == before
    assert store.get_reconciliation_run("run-1", db_path=path).checked_order_count == 4


def test_recording_into_a_missing_db_fails_without_creating_the_file(tmp_path):
    path = db(tmp_path)
    with pytest.raises(store.ReconciliationStoreError, match="does not exist"):
        store.record_reconciliation_run(sample_run(), db_path=path)
    assert not path.exists()


def test_recording_requires_a_run_model(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    with pytest.raises(store.ReconciliationStoreError):
        store.record_reconciliation_run({"run_id": "run-1"}, db_path=path)


# --- reads --------------------------------------------------------------------


def test_reads_never_create_the_db_file(tmp_path):
    path = db(tmp_path)
    assert store.get_reconciliation_run("run-1", db_path=path) is None
    assert store.list_reconciliation_runs(db_path=path) == ()
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_reading_an_unknown_run_returns_none(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    assert store.get_reconciliation_run("nope", db_path=path) is None


def test_a_read_leaves_the_file_untouched(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    store.record_reconciliation_run(sample_run(), db_path=path)
    before = (path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in tmp_path.iterdir()))
    store.get_reconciliation_run("run-1", db_path=path)
    store.list_reconciliation_runs(db_path=path)
    assert (path.read_bytes(), path.stat().st_mtime_ns,
            sorted(p.name for p in tmp_path.iterdir())) == before


def test_list_respects_the_limit_and_validates_it(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    for index in range(3):
        store.record_reconciliation_run(
            sample_run(f"run-{index}", started_at_utc=at(index * 10),
                       finished_at_utc=at(index * 10 + 1)),
            db_path=path,
        )
    assert [r.run_id for r in store.list_reconciliation_runs(limit=2, db_path=path)] == [
        "run-2",
        "run-1",
    ]
    for bad in (0, -1, True, "5"):
        with pytest.raises(store.ReconciliationStoreError):
            store.list_reconciliation_runs(limit=bad, db_path=path)


def test_an_empty_run_id_is_refused_on_read(tmp_path):
    with pytest.raises(store.ReconciliationStoreError):
        store.get_reconciliation_run("  ", db_path=db(tmp_path))


# --- restore-time integrity ---------------------------------------------------


def test_an_out_of_band_edited_discrepancy_row_is_refused(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    store.record_reconciliation_run(sample_run(), db_path=path)
    with closing(sqlite3.connect(path)) as connection:
        # a state discrepancy without the observed internal state: the column
        # is nullable, the MODEL invariant is what rejects it
        connection.execute(
            f"UPDATE {store.DISCREPANCIES_TABLE} SET internal_state = NULL "
            "WHERE target_id = 'exec-1'"
        )
        connection.commit()
    with pytest.raises(store.ReconciliationIntegrityError, match="failed restore-time validation"):
        store.get_reconciliation_run("run-1", db_path=path)


def test_db_check_constraints_reject_unknown_enum_values(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    store.record_reconciliation_run(sample_run(), db_path=path)
    with closing(sqlite3.connect(path)) as connection:
        for column, value in (
            ("discrepancy_type", "invented_kind"),
            ("resolution_category", "auto_fix_everything"),
            ("target_type", "position"),
            ("internal_state", "half_filled"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"UPDATE {store.DISCREPANCIES_TABLE} SET {column} = ? "
                    "WHERE target_id = 'exec-1'",
                    (value,),
                )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"UPDATE {store.RUNS_TABLE} SET mode = 'repair' WHERE run_id = 'run-1'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"UPDATE {store.RUNS_TABLE} SET checked_order_count = -1 WHERE run_id = 'run-1'"
            )


def test_db_check_constraint_pins_query_failed_to_query_failure(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    store.record_reconciliation_run(sample_run(), db_path=path)
    with closing(sqlite3.connect(path)) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"UPDATE {store.DISCREPANCIES_TABLE} SET resolution_category = 'query_failed' "
                "WHERE target_id = 'exec-1'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"UPDATE {store.DISCREPANCIES_TABLE} SET broker_error_code = 'timeout' "
                "WHERE target_id = 'exec-1'"
            )


def test_a_discrepancy_cannot_reference_an_unknown_run(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    store.record_reconciliation_run(sample_run(), db_path=path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"UPDATE {store.DISCREPANCIES_TABLE} SET run_id = 'ghost-run' "
                "WHERE target_id = 'exec-1'"
            )


def test_the_default_db_path_is_execution_owned_and_env_overridable(tmp_path, monkeypatch):
    monkeypatch.delenv("BROKER_RECONCILIATION_DB_PATH", raising=False)
    assert store.reconciliation_db_path() == store.DEFAULT_DB_PATH
    assert store.DEFAULT_DB_PATH.parent.name == "data"
    monkeypatch.setenv("BROKER_RECONCILIATION_DB_PATH", str(tmp_path / "custom.sqlite3"))
    assert store.reconciliation_db_path() == tmp_path / "custom.sqlite3"
    monkeypatch.setenv("BROKER_RECONCILIATION_DB_PATH", "relative.sqlite3")
    assert store.reconciliation_db_path() == store.BASE_DIR / "relative.sqlite3"
