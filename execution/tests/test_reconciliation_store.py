"""Issue #103: SQLite audit persistence for broker reconciliation runs.

All DBs live under pytest's tmp_path. No network, no Discord, no S3, no
sleep, no real orders anywhere in this suite.
"""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
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


def test_init_treats_a_zero_byte_sqlite_file_as_pristine(tmp_path):
    path = db(tmp_path)
    path.touch()
    store.init_reconciliation_db(path)
    assert store.list_reconciliation_runs(db_path=path) == ()


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


@pytest.mark.parametrize("operation", ["init", "get", "list", "record"])
def test_a_non_sqlite_file_is_rejected_through_the_store_error_contract(tmp_path, operation):
    path = db(tmp_path)
    path.write_bytes(b"this is not a sqlite database")
    before = path.read_bytes()

    with pytest.raises(store.ReconciliationStoreError) as captured:
        if operation == "init":
            store.init_reconciliation_db(path)
        elif operation == "get":
            store.get_reconciliation_run("run-1", db_path=path)
        elif operation == "list":
            store.list_reconciliation_runs(db_path=path)
        else:
            store.record_reconciliation_run(sample_run(), db_path=path)

    assert isinstance(captured.value, store.ReconciliationSchemaError)
    assert path.read_bytes() == before


def test_a_truncated_sqlite_database_is_rejected_without_further_modification(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    path.write_bytes(path.read_bytes()[:100])
    before = path.read_bytes()

    with pytest.raises(store.ReconciliationSchemaError):
        store.get_reconciliation_run("run-1", db_path=path)

    assert path.read_bytes() == before


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


def test_a_failure_after_partial_discrepancy_inserts_rolls_back_the_whole_run(
    tmp_path, monkeypatch
):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    original_iso = store._iso
    calls = 0

    def fail_on_second_discrepancy(value):
        nonlocal calls
        calls += 1
        # started + finished + first discrepancy are valid; the second
        # discrepancy violates the UTC CHECK after the run/first child INSERT.
        if calls == 4:
            return "not-a-utc-timestamp"
        return original_iso(value)

    monkeypatch.setattr(store, "_iso", fail_on_second_discrepancy)
    with pytest.raises(store.ReconciliationStoreError):
        store.record_reconciliation_run(sample_run(), db_path=path)

    assert store.get_reconciliation_run("run-1", db_path=path) is None
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(f"SELECT COUNT(*) FROM {store.RUNS_TABLE}").fetchone()[0] == 0
        assert (
            connection.execute(f"SELECT COUNT(*) FROM {store.DISCREPANCIES_TABLE}").fetchone()[0]
            == 0
        )


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


def test_concurrent_distinct_runs_are_serialized_without_loss(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    runs = [
        sample_run(
            f"run-{index}",
            started_at_utc=at(index * 10),
            finished_at_utc=at(index * 10 + 5),
        )
        for index in range(8)
    ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda run: store.record_reconciliation_run(run, db_path=path), runs))

    restored = store.list_reconciliation_runs(limit=20, db_path=path)
    assert {run.run_id for run in restored} == {run.run_id for run in runs}
    assert all(len(run.discrepancies) == len(sample_discrepancies()) for run in restored)


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


def test_db_unique_constraint_rejects_a_duplicate_target_within_one_run(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    store.record_reconciliation_run(sample_run(), db_path=path)
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            f"SELECT {', '.join(store._DISCREPANCY_COLUMNS)} "
            f"FROM {store.DISCREPANCIES_TABLE} WHERE target_id = 'exec-1'"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO {store.DISCREPANCIES_TABLE} "
                f"({', '.join(store._DISCREPANCY_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in store._DISCREPANCY_COLUMNS)})",
                tuple(row),
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


# --- repair attempts / schema v2 (Issue #104) ---------------------------------


from execution.reconciliation_repair_models import (  # noqa: E402
    OrderRepairAttempt,
    RepairAttemptReservation,
    RepairFailureCode,
    RepairMode,
    RepairOutcome,
    RepairTakeoverIntent,
    TakeoverBlockedReason,
    TakeoverIntentStatus,
)


def sample_reservation(attempt_id="attempt-1", run_id="run-1", **overrides):
    kwargs = dict(
        attempt_id=attempt_id,
        run_id=run_id,
        target_type=ReconciliationTargetType.EXECUTION_ORDER,
        target_id="exec-1",
        reserved_at_utc=at(10),
        detail="reserved: repair evaluation started",
        owner_id="worker-1",
        claim_execution_id="claim-1",
        generation=1,
        lease_expires_at_utc=at(700),
        last_claimed_at_utc=at(10),
        execution_request_id="exec-1",
        client_order_id="client-1",
        reconciliation_internal_state=OrderState.PENDING,
        reconciliation_broker_state=OrderState.FILLED,
        internal_revision=1,
        observed_at_utc=at(1),
    )
    kwargs.update(overrides)
    return RepairAttemptReservation(**kwargs)


def sample_attempt(attempt_id="attempt-1", run_id="run-1", **overrides) -> OrderRepairAttempt:
    kwargs = dict(
        attempt_id=attempt_id,
        run_id=run_id,
        mode=RepairMode.APPLY,
        target_type=ReconciliationTargetType.EXECUTION_ORDER,
        target_id="exec-1",
        outcome=RepairOutcome.APPLIED,
        detail="the broker confirmed state 'filled'",
        reserved_at_utc=at(10),
        owner_id="worker-1",
        claim_execution_id="claim-1",
        generation=1,
        execution_request_id="exec-1",
        client_order_id="client-1",
        reconciliation_internal_state=OrderState.PENDING,
        reconciliation_broker_state=OrderState.FILLED,
        internal_revision=1,
        internal_state_before=OrderState.PENDING,
        internal_state_after=OrderState.FILLED,
        broker_order_id="B-1",
        observed_at_utc=at(1),
        broker_state_updated_at_utc=at(11),
        finalized_at_utc=at(12),
    )
    kwargs.update(overrides)
    return OrderRepairAttempt(**kwargs)


def recorded_run_db(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    store.record_reconciliation_run(sample_run(), db_path=path)
    return path


def test_a_reserved_then_finalized_attempt_round_trips_with_every_field(tmp_path):
    path = recorded_run_db(tmp_path)
    store.reserve_repair_attempt(sample_reservation(), db_path=path)
    assert store.list_pending_repair_attempts(db_path=path) == (sample_reservation(),)
    assert store.get_repair_attempt("attempt-1", db_path=path) is None  # still pending

    original = sample_attempt()
    store.finalize_repair_attempt(original, db_path=path)

    assert store.get_repair_attempt("attempt-1", db_path=path) == original
    assert store.list_repair_attempts(run_id="run-1", db_path=path) == (original,)
    assert store.list_pending_repair_attempts(db_path=path) == ()


def test_a_skipped_attempt_for_a_broker_only_target_round_trips(tmp_path):
    path = recorded_run_db(tmp_path)
    broker_only_reservation = sample_reservation(
        attempt_id="attempt-skip",
        target_type=ReconciliationTargetType.BROKER_ORDER,
        target_id="client-9",
        execution_request_id=None,
        client_order_id="client-9",
        reconciliation_internal_state=None,
        internal_revision=None,
    )
    store.reserve_repair_attempt(broker_only_reservation, db_path=path)
    finalized = sample_attempt(
        attempt_id="attempt-skip",
        target_type=ReconciliationTargetType.BROKER_ORDER,
        target_id="client-9",
        outcome=RepairOutcome.SKIPPED,
        detail="broker-only orders are never adopted automatically",
        failure_code=RepairFailureCode.NOT_ELIGIBLE_BROKER_ONLY,
        execution_request_id=None,
        client_order_id="client-9",
        reconciliation_internal_state=None,
        internal_revision=None,
        internal_state_before=None,
        internal_state_after=None,
        broker_order_id=None,
        broker_state_updated_at_utc=None,
    )
    store.finalize_repair_attempt(finalized, db_path=path)
    assert store.get_repair_attempt("attempt-skip", db_path=path) == finalized


def test_a_reused_attempt_id_is_refused_at_reservation_time(tmp_path):
    path = recorded_run_db(tmp_path)
    store.reserve_repair_attempt(sample_reservation(), db_path=path)
    with pytest.raises(store.RepairAttemptConflictError):
        store.reserve_repair_attempt(sample_reservation(), db_path=path)
    store.finalize_repair_attempt(sample_attempt(), db_path=path)
    with pytest.raises(store.RepairAttemptConflictError):
        store.reserve_repair_attempt(sample_reservation(), db_path=path)


def test_double_finalize_is_refused(tmp_path):
    path = recorded_run_db(tmp_path)
    store.reserve_repair_attempt(sample_reservation(), db_path=path)
    store.finalize_repair_attempt(sample_attempt(), db_path=path)
    with pytest.raises(store.RepairAttemptConflictError, match="finalized exactly once"):
        store.finalize_repair_attempt(sample_attempt(), db_path=path)
    # the stored outcome is untouched
    assert store.get_repair_attempt("attempt-1", db_path=path) == sample_attempt()


def test_finalize_without_a_reservation_is_refused(tmp_path):
    path = recorded_run_db(tmp_path)
    with pytest.raises(store.ReconciliationStoreError, match="no reservation"):
        store.finalize_repair_attempt(sample_attempt(), db_path=path)
    assert store.list_repair_attempts(db_path=path) == ()


def test_finalize_with_a_mismatched_identity_is_refused_as_corruption(tmp_path):
    path = recorded_run_db(tmp_path)
    store.reserve_repair_attempt(sample_reservation(), db_path=path)
    foreign = sample_attempt(client_order_id="client-somebody-else")
    with pytest.raises(store.ReconciliationIntegrityError, match="client_order_id"):
        store.finalize_repair_attempt(foreign, db_path=path)
    # still pending, recoverable
    assert len(store.list_pending_repair_attempts(db_path=path)) == 1


def test_a_reservation_for_an_unrecorded_discrepancy_is_refused(tmp_path):
    path = recorded_run_db(tmp_path)
    with pytest.raises(store.ReconciliationStoreError, match="not recorded"):
        store.reserve_repair_attempt(
            sample_reservation(target_id="exec-ghost", execution_request_id="exec-ghost"),
            db_path=path,
        )
    with pytest.raises(store.ReconciliationStoreError, match="not recorded"):
        store.reserve_repair_attempt(sample_reservation(run_id="run-ghost"), db_path=path)
    assert store.list_pending_repair_attempts(db_path=path) == ()


def test_a_dry_run_attempt_is_never_persisted(tmp_path):
    path = recorded_run_db(tmp_path)
    dry = sample_attempt(
        mode=RepairMode.DRY_RUN,
        outcome=RepairOutcome.WOULD_APPLY,
        owner_id=None,
        claim_execution_id=None,
        generation=None,
        broker_state_updated_at_utc=None,
        finalized_at_utc=None,
    )
    with pytest.raises(store.ReconciliationStoreError, match="dry-run"):
        store.finalize_repair_attempt(dry, db_path=path)
    assert store.list_repair_attempts(db_path=path) == ()


def test_repair_writes_never_create_a_missing_db(tmp_path):
    path = db(tmp_path)
    with pytest.raises(store.ReconciliationStoreError, match="init_reconciliation_db"):
        store.reserve_repair_attempt(sample_reservation(), db_path=path)
    with pytest.raises(store.ReconciliationStoreError, match="init_reconciliation_db"):
        store.finalize_repair_attempt(sample_attempt(), db_path=path)
    assert not path.exists()


def test_repair_reads_return_the_empty_default_for_a_missing_db(tmp_path):
    path = db(tmp_path)
    assert store.get_repair_attempt("attempt-1", db_path=path) is None
    assert store.list_repair_attempts(db_path=path) == ()
    assert store.list_pending_repair_attempts(db_path=path) == ()
    assert not path.exists()


def test_a_tampered_attempt_row_fails_restore_time_validation(tmp_path):
    path = recorded_run_db(tmp_path)
    store.reserve_repair_attempt(sample_reservation(), db_path=path)
    store.finalize_repair_attempt(sample_attempt(), db_path=path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            f"UPDATE {store.REPAIRS_TABLE} SET reserved_at_utc = 'garbage+00:00'"
        )
        connection.commit()
    with pytest.raises(store.ReconciliationIntegrityError):
        store.get_repair_attempt("attempt-1", db_path=path)


def test_a_malformed_pending_row_fails_closed(tmp_path):
    path = recorded_run_db(tmp_path)
    store.reserve_repair_attempt(sample_reservation(), db_path=path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            f"UPDATE {store.REPAIRS_TABLE} SET reserved_at_utc = 'garbage+00:00'"
        )
        connection.commit()
    with pytest.raises(store.ReconciliationIntegrityError):
        store.list_pending_repair_attempts(db_path=path)


def test_attempts_survive_a_process_restart(tmp_path):
    path = recorded_run_db(tmp_path)
    store.reserve_repair_attempt(sample_reservation(), db_path=path)
    store.finalize_repair_attempt(sample_attempt(), db_path=path)
    store.reserve_repair_attempt(sample_reservation(attempt_id="attempt-2"), db_path=path)
    store._REFERENCE_SCHEMA_CACHE = None  # rebuild the reference like a fresh import
    assert store.get_repair_attempt("attempt-1", db_path=path) == sample_attempt()
    assert store.list_pending_repair_attempts(db_path=path) == (
        sample_reservation(attempt_id="attempt-2"),
    )


def test_internal_revision_round_trips_on_discrepancies(tmp_path):
    path = db(tmp_path)
    store.init_reconciliation_db(path)
    with_revision = sample_run(
        "run-rev",
        discrepancies=(
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
                internal_revision=3,
            ),
        ),
    )
    store.record_reconciliation_run(with_revision, db_path=path)
    restored = store.get_reconciliation_run("run-rev", db_path=path)
    assert restored.discrepancies[0].internal_revision == 3


# --- durable takeover intents (Issue #104 re-review r4) -----------------------


def sample_intent(attempt_id="attempt-1", takeover_id="tk-1", **overrides):
    kwargs = dict(
        takeover_id=takeover_id,
        repair_attempt_id=attempt_id,
        source_generation=1,
        target_generation=2,
        source_owner_id="worker-1",
        source_claim_execution_id="claim-1",
        target_owner_id="worker-2",
        target_claim_execution_id="claim-2",
        lease_expires_at_utc=at(2000),
        status=TakeoverIntentStatus.PREPARED,
        created_at_utc=at(1300),
    )
    kwargs.update(overrides)
    return RepairTakeoverIntent(**kwargs)


def reserved_db(tmp_path, **reservation_overrides):
    path = recorded_run_db(tmp_path)
    store.reserve_repair_attempt(sample_reservation(**reservation_overrides), db_path=path)
    return path


def prepared(path, intent=None, *, observed=None):
    intent = intent or sample_intent()
    return store.create_takeover_intent(
        intent, observed_at_utc=observed or at(1300), db_path=path
    )


def fence_installed(path, intent=None, *, at_utc=None):
    intent = intent or sample_intent()
    return store.advance_takeover_intent(
        intent,
        to_status=TakeoverIntentStatus.FENCE_INSTALLED,
        at_utc=at_utc or at(1301),
        db_path=path,
    )


def test_a_prepared_intent_round_trips_and_is_the_active_one(tmp_path):
    path = reserved_db(tmp_path)
    created = prepared(path)

    assert created == sample_intent()
    assert store.get_active_takeover_intent("attempt-1", db_path=path) == sample_intent()
    assert store.list_takeover_intents(attempt_id="attempt-1", db_path=path) == (sample_intent(),)
    # the claim itself is untouched until the promotion step
    assert only_pending(path).generation == 1


def only_pending(path):
    pending = store.list_pending_repair_attempts(db_path=path)
    assert len(pending) == 1, pending
    return pending[0]


def test_an_intent_cannot_be_created_while_the_lease_is_valid(tmp_path):
    path = reserved_db(tmp_path)
    with pytest.raises(store.TakeoverIntentConflictError, match="claimed until"):
        store.create_takeover_intent(sample_intent(), observed_at_utc=at(11), db_path=path)
    assert store.list_takeover_intents(db_path=path) == ()


def test_a_second_active_intent_is_refused(tmp_path):
    path = reserved_db(tmp_path)
    prepared(path)
    with pytest.raises(store.TakeoverIntentConflictError, match="already has the active"):
        store.create_takeover_intent(
            sample_intent(takeover_id="tk-2", target_claim_execution_id="claim-3"),
            observed_at_utc=at(1300),
            db_path=path,
        )
    assert len(store.list_takeover_intents(db_path=path)) == 1


def test_an_intent_whose_source_identity_moved_on_is_refused(tmp_path):
    path = reserved_db(tmp_path)
    for override in (
        {"source_generation": 2, "target_generation": 3},
        {"source_owner_id": "someone-else"},
        {"source_claim_execution_id": "claim-someone-else"},
    ):
        with pytest.raises(store.TakeoverIntentConflictError, match="preconditions|generation"):
            store.create_takeover_intent(
                sample_intent(takeover_id=f"tk-{len(override)}", **override),
                observed_at_utc=at(1300),
                db_path=path,
            )
    assert store.list_takeover_intents(db_path=path) == ()


def test_an_intent_for_a_finalized_attempt_is_refused(tmp_path):
    path = reserved_db(tmp_path)
    store.finalize_repair_attempt(sample_attempt(), db_path=path)
    with pytest.raises(store.RepairAttemptConflictError, match="already finalized"):
        store.create_takeover_intent(sample_intent(), observed_at_utc=at(1300), db_path=path)
    assert store.list_takeover_intents(db_path=path) == ()


def test_the_same_source_generation_can_never_be_taken_over_twice(tmp_path):
    path = reserved_db(tmp_path)
    intent = prepared(path)
    fence_installed(path)
    store.promote_repair_claim(intent, promoted_at_utc=at(1302), db_path=path)
    store.advance_takeover_intent(
        intent, to_status=TakeoverIntentStatus.CLAIM_PROMOTED, at_utc=at(1303), db_path=path
    )
    store.advance_takeover_intent(
        intent, to_status=TakeoverIntentStatus.COMPLETED, at_utc=at(1304), db_path=path
    )

    # a second takeover of generation 1 (already consumed) is refused by the DB
    with pytest.raises(store.TakeoverIntentConflictError):
        store.create_takeover_intent(
            sample_intent(takeover_id="tk-again", target_claim_execution_id="claim-9"),
            observed_at_utc=at(3000),
            db_path=path,
        )
    # but the NEXT generation may be taken over
    following = store.create_takeover_intent(
        sample_intent(
            takeover_id="tk-2",
            source_generation=2,
            target_generation=3,
            source_owner_id="worker-2",
            source_claim_execution_id="claim-2",
            target_owner_id="worker-3",
            target_claim_execution_id="claim-3",
            created_at_utc=at(3000),
            lease_expires_at_utc=at(3600),
        ),
        observed_at_utc=at(3000),
        db_path=path,
    )
    assert following.target_generation == 3


def test_exactly_one_of_concurrent_intent_creations_wins(tmp_path):
    """Genuinely concurrent: N barrier-aligned workers try to prepare a
    takeover of the SAME expired claim. Exactly one intent exists afterwards
    and the losers can read the winner's identity to join it."""
    path = reserved_db(tmp_path)
    workers = 4
    barrier = threading.Barrier(workers)

    def prepare(index):
        barrier.wait()
        try:
            return store.create_takeover_intent(
                sample_intent(
                    takeover_id=f"tk-{index}",
                    target_owner_id=f"worker-{index}",
                    target_claim_execution_id=f"claim-w{index}",
                ),
                observed_at_utc=at(1300),
                db_path=path,
            )
        except store.TakeoverIntentConflictError:
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(prepare, range(workers)))

    winners = [intent for intent in results if intent is not None]
    assert len(winners) == 1
    stored = store.list_takeover_intents(db_path=path)
    assert stored == (winners[0],)
    # every loser can join the winner by loading the active intent
    assert store.get_active_takeover_intent("attempt-1", db_path=path) == winners[0]


def test_intent_transitions_are_forward_only_and_idempotent(tmp_path):
    path = reserved_db(tmp_path)
    intent = prepared(path)

    first = fence_installed(path, intent)
    assert first.status is TakeoverIntentStatus.FENCE_INSTALLED
    assert first.fence_installed_at_utc == at(1301)
    # re-applying the same step after a crash is a no-op, timestamps unchanged
    again = fence_installed(path, intent, at_utc=at(9999))
    assert again == first

    with pytest.raises(store.TakeoverIntentStateError, match="cannot move"):
        store.advance_takeover_intent(
            intent, to_status=TakeoverIntentStatus.COMPLETED, at_utc=at(1302), db_path=path
        )
    store.promote_repair_claim(intent, promoted_at_utc=at(1302), db_path=path)
    promoted = store.advance_takeover_intent(
        intent, to_status=TakeoverIntentStatus.CLAIM_PROMOTED, at_utc=at(1303), db_path=path
    )
    completed = store.advance_takeover_intent(
        intent, to_status=TakeoverIntentStatus.COMPLETED, at_utc=at(1304), db_path=path
    )
    assert (promoted.status, completed.status) == (
        TakeoverIntentStatus.CLAIM_PROMOTED,
        TakeoverIntentStatus.COMPLETED,
    )
    # a completed intent never moves again -- and is no longer active
    with pytest.raises(store.TakeoverIntentStateError):
        fence_installed(path, intent, at_utc=at(1305))
    assert store.get_active_takeover_intent("attempt-1", db_path=path) is None


def test_a_transition_by_a_foreign_identity_is_refused(tmp_path):
    path = reserved_db(tmp_path)
    intent = prepared(path)
    for override in (
        {"target_owner_id": "impostor"},
        {"target_claim_execution_id": "claim-impostor"},
        {"source_generation": 2, "target_generation": 3},
        {"repair_attempt_id": "attempt-other"},
    ):
        with pytest.raises(store.TakeoverIntentStateError, match="does not match"):
            store.advance_takeover_intent(
                sample_intent(**override),
                to_status=TakeoverIntentStatus.FENCE_INSTALLED,
                at_utc=at(1301),
                db_path=path,
            )
    assert store.get_active_takeover_intent("attempt-1", db_path=path) == intent


def test_a_transition_on_an_unknown_intent_is_refused(tmp_path):
    path = reserved_db(tmp_path)
    with pytest.raises(store.TakeoverIntentStateError, match="not recorded"):
        store.advance_takeover_intent(
            sample_intent(takeover_id="tk-ghost"),
            to_status=TakeoverIntentStatus.FENCE_INSTALLED,
            at_utc=at(1301),
            db_path=path,
        )


def test_claim_promotion_requires_the_fence_step_first(tmp_path):
    path = reserved_db(tmp_path)
    intent = prepared(path)
    with pytest.raises(store.TakeoverIntentStateError, match="execution fence"):
        store.promote_repair_claim(intent, promoted_at_utc=at(1302), db_path=path)
    assert only_pending(path).generation == 1


def test_claim_promotion_moves_the_claim_onto_the_intent_identity_idempotently(tmp_path):
    path = reserved_db(tmp_path)
    intent = prepared(path)
    fence_installed(path, intent)

    promoted = store.promote_repair_claim(intent, promoted_at_utc=at(1302), db_path=path)

    assert (promoted.generation, promoted.owner_id, promoted.claim_execution_id) == (
        2,
        "worker-2",
        "claim-2",
    )
    assert promoted.takeover_id == "tk-1"
    assert promoted.lease_expires_at_utc == at(2000)  # the intent's lease
    assert promoted.last_claimed_at_utc == at(1302)
    assert promoted.attempt_id == "attempt-1" and promoted.reserved_at_utc == at(10)
    # replaying the promotion after a crash returns the same row, writes nothing
    assert store.promote_repair_claim(intent, promoted_at_utc=at(7777), db_path=path) == promoted
    assert only_pending(path) == promoted


def test_claim_promotion_by_a_stale_intent_is_refused(tmp_path):
    path = reserved_db(tmp_path)
    intent = prepared(path)
    fence_installed(path, intent)
    store.promote_repair_claim(intent, promoted_at_utc=at(1302), db_path=path)
    store.advance_takeover_intent(
        intent, to_status=TakeoverIntentStatus.CLAIM_PROMOTED, at_utc=at(1303), db_path=path
    )
    store.advance_takeover_intent(
        intent, to_status=TakeoverIntentStatus.COMPLETED, at_utc=at(1304), db_path=path
    )
    second = store.create_takeover_intent(
        sample_intent(
            takeover_id="tk-2",
            source_generation=2,
            target_generation=3,
            source_owner_id="worker-2",
            source_claim_execution_id="claim-2",
            target_owner_id="worker-3",
            target_claim_execution_id="claim-3",
            created_at_utc=at(3000),
            lease_expires_at_utc=at(3600),
        ),
        observed_at_utc=at(3000),
        db_path=path,
    )
    store.advance_takeover_intent(
        second, to_status=TakeoverIntentStatus.FENCE_INSTALLED, at_utc=at(3001), db_path=path
    )
    store.promote_repair_claim(second, promoted_at_utc=at(3002), db_path=path)

    # the first (completed) intent can never move the claim back
    with pytest.raises(store.TakeoverIntentStateError):
        store.promote_repair_claim(intent, promoted_at_utc=at(3003), db_path=path)
    assert only_pending(path).generation == 3


def test_blocking_records_the_diagnosis_and_is_idempotent(tmp_path):
    path = reserved_db(tmp_path)
    intent = prepared(path)

    blocked = store.block_takeover_intent(
        intent,
        reason=TakeoverBlockedReason.FENCE_AHEAD,
        at_utc=at(1400),
        observed_claim_generation=1,
        observed_fence_generation=3,
        observed_fence_identity="attempt=attempt-1 generation=3 takeover=tk-x",
        db_path=path,
    )

    assert blocked.status is TakeoverIntentStatus.BLOCKED
    assert blocked.blocked_reason is TakeoverBlockedReason.FENCE_AHEAD
    assert (blocked.observed_claim_generation, blocked.observed_fence_generation) == (1, 3)
    assert blocked.blocked_at_utc == at(1400)
    # blocked is terminal: no active intent, no automatic resumption
    assert store.get_active_takeover_intent("attempt-1", db_path=path) is None
    with pytest.raises(store.TakeoverIntentStateError):
        fence_installed(path, intent, at_utc=at(1401))
    # blocking again keeps the ORIGINAL diagnosis
    same = store.block_takeover_intent(
        intent, reason=TakeoverBlockedReason.PROTOCOL_CORRUPT, at_utc=at(1500), db_path=path
    )
    assert same == blocked
    # and the attempt is still pending -- nothing was finalized
    assert only_pending(path).generation == 1


def test_a_blocked_intent_can_be_inserted_without_a_predecessor(tmp_path):
    """An execution fence ahead of every known intent has no active intent to
    block: the diagnosis is inserted directly, and it occupies the (attempt,
    source generation) slot so the doomed takeover is never silently retried."""
    path = reserved_db(tmp_path)
    blocked = store.insert_blocked_takeover_intent(
        sample_intent(
            takeover_id="tk-blocked",
            status=TakeoverIntentStatus.BLOCKED,
            blocked_at_utc=at(1400),
            blocked_reason=TakeoverBlockedReason.ORPHAN_FENCE_WITHOUT_INTENT,
            observed_claim_generation=1,
            observed_fence_generation=2,
            observed_fence_identity="attempt=attempt-1 generation=2 takeover=unknown",
        ),
        db_path=path,
    )
    assert store.list_takeover_intents(attempt_id="attempt-1", db_path=path) == (blocked,)
    assert store.get_active_takeover_intent("attempt-1", db_path=path) is None
    with pytest.raises(store.TakeoverIntentConflictError):
        store.create_takeover_intent(sample_intent(), observed_at_utc=at(1500), db_path=path)


def test_finalize_requires_the_current_claim_including_the_takeover_id(tmp_path):
    path = reserved_db(tmp_path)
    intent = prepared(path)
    fence_installed(path, intent)
    store.promote_repair_claim(intent, promoted_at_utc=at(1302), db_path=path)

    # the ORIGINAL generation-1 worker resurrects and tries to finalize
    with pytest.raises(store.RepairClaimLostError, match="generation 2"):
        store.finalize_repair_attempt(sample_attempt(), db_path=path)
    # so does a worker with the right generation but a forged takeover id
    with pytest.raises(store.RepairClaimLostError):
        store.finalize_repair_attempt(
            sample_attempt(
                owner_id="worker-2",
                claim_execution_id="claim-2",
                generation=2,
                takeover_id="tk-forged",
            ),
            db_path=path,
        )
    assert len(store.list_pending_repair_attempts(db_path=path)) == 1

    completed = sample_attempt(
        owner_id="worker-2",
        claim_execution_id="claim-2",
        generation=2,
        takeover_id="tk-1",
        finalized_at_utc=at(1400),
    )
    store.finalize_repair_attempt(completed, db_path=path)
    assert store.get_repair_attempt("attempt-1", db_path=path) == completed
    assert store.list_pending_repair_attempts(db_path=path) == ()


def test_a_blocked_outcome_is_never_finalized_into_the_attempt_row(tmp_path):
    path = reserved_db(tmp_path)
    with pytest.raises(store.ReconciliationStoreError, match="stays PENDING|BLOCKED|blocked"):
        store.finalize_repair_attempt(
            sample_attempt(
                outcome=RepairOutcome.BLOCKED,
                failure_code=RepairFailureCode.FENCE_AHEAD,
                internal_state_after=None,
                broker_state_updated_at_utc=None,
            ),
            db_path=path,
        )
    assert len(store.list_pending_repair_attempts(db_path=path)) == 1


def test_finalize_requires_the_finalized_timestamp(tmp_path):
    path = reserved_db(tmp_path)
    with pytest.raises(store.ReconciliationStoreError, match="finalized_at_utc"):
        store.finalize_repair_attempt(sample_attempt(finalized_at_utc=None), db_path=path)
    assert len(store.list_pending_repair_attempts(db_path=path)) == 1


def test_unknown_intent_status_or_blocked_reason_fails_closed(tmp_path):
    path = reserved_db(tmp_path)
    prepared(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")  # simulate damage
        connection.execute(f"UPDATE {store.TAKEOVER_INTENTS_TABLE} SET status = 'teleported'")
        connection.commit()
    with pytest.raises(store.ReconciliationIntegrityError, match="unknown status"):
        store.list_takeover_intents(db_path=path)
    with pytest.raises(store.ReconciliationIntegrityError, match="unknown status"):
        store.get_active_takeover_intent("attempt-1", db_path=path)

    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            f"UPDATE {store.TAKEOVER_INTENTS_TABLE} SET status = 'blocked', "
            "blocked_at_utc = ?, blocked_reason = 'because'",
            (store._iso(at(1400)),),
        )
        connection.commit()
    with pytest.raises(store.ReconciliationIntegrityError, match="unknown blocked reason"):
        store.list_takeover_intents(db_path=path)


def test_db_check_constraints_reject_malformed_intents(tmp_path):
    path = reserved_db(tmp_path)
    columns = ", ".join(store._INTENT_COLUMNS)
    placeholders = ", ".join("?" for _ in store._INTENT_COLUMNS)
    base = [
        "tk-x",
        "attempt-1",
        1,
        2,
        "worker-1",
        "claim-1",
        "worker-2",
        "claim-2",
        store._iso(at(2000)),
        "prepared",
        store._iso(at(1300)),
        None,  # fence_installed_at_utc
        None,  # claim_promoted_at_utc
        None,  # completed_at_utc
        None,  # cancelled_at_utc
        None,  # blocked_at_utc
        None,  # blocked_reason
        None,  # observed_claim_generation
        None,  # observed_fence_generation
        None,  # observed_fence_identity
    ]
    mutations = {
        "unknown status": (9, "teleported"),
        "generation gap": (3, 4),
        "generation zero": (2, 0),
        "same claim identity": (7, "claim-1"),
        "non-utc timestamp": (10, "2026-07-20 12:00:00"),
        "unknown blocked reason": (16, "because"),
    }
    for label, (index, value) in mutations.items():
        record = list(base)
        record[index] = value
        with closing(sqlite3.connect(path)) as connection:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"INSERT INTO {store.TAKEOVER_INTENTS_TABLE} ({columns}) "
                    f"VALUES ({placeholders})",
                    record,
                )
        assert store.list_takeover_intents(db_path=path) == (), label


def test_intent_reads_return_defaults_for_a_missing_db(tmp_path):
    missing = tmp_path / "nothing.sqlite3"
    assert store.get_active_takeover_intent("attempt-1", db_path=missing) is None
    assert store.list_takeover_intents(db_path=missing) == ()
    assert not missing.exists()


# --- v1 -> v2 migration (Issue #104) ------------------------------------------


def downgrade_to_v1(path):
    """Rebuild the exact released v1 file: drop the additive v2 objects and
    the discrepancies revision column (asserted against the pristine v1
    reference)."""
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"DROP INDEX {store._INTENT_ACTIVE_INDEX}")
        connection.execute(f"DROP INDEX {store._INTENT_ATTEMPT_INDEX}")
        connection.execute(f"DROP TABLE {store.TAKEOVER_INTENTS_TABLE}")
        connection.execute(f"DROP INDEX {store._REPAIRS_RUN_INDEX}")
        connection.execute(f"DROP TABLE {store.REPAIRS_TABLE}")
        connection.execute(
            f"ALTER TABLE {store.DISCREPANCIES_TABLE} DROP COLUMN internal_revision"
        )
        connection.execute(f"PRAGMA user_version = {store._MIGRATABLE_VERSION}")
        connection.commit()
    with closing(sqlite3.connect(path)) as connection:
        assert store._schema_problems(connection, version=store._MIGRATABLE_VERSION) == []


def test_init_migrates_a_valid_v1_db_and_keeps_every_row(tmp_path):
    path = recorded_run_db(tmp_path)
    downgrade_to_v1(path)

    store.init_reconciliation_db(path)

    with closing(sqlite3.connect(path)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert version == store.SCHEMA_VERSION
    assert store.REPAIRS_TABLE in tables
    restored = store.get_reconciliation_run("run-1", db_path=path)
    # pre-migration rows read back with internal_revision = NULL
    assert {d.internal_revision for d in restored.discrepancies} == {None}
    assert [d.target_id for d in restored.discrepancies] == [
        d.target_id for d in sample_run().discrepancies
    ]
    assert store.list_repair_attempts(db_path=path) == ()
    # idempotent: a second init only verifies
    store.init_reconciliation_db(path)


def test_reads_and_writes_never_migrate_a_v1_db(tmp_path):
    path = recorded_run_db(tmp_path)
    downgrade_to_v1(path)
    before = path.read_bytes()

    with pytest.raises(store.ReconciliationSchemaError, match="init_reconciliation_db"):
        store.get_reconciliation_run("run-1", db_path=path)
    with pytest.raises(store.ReconciliationSchemaError):
        store.record_reconciliation_run(sample_run("run-2"), db_path=path)
    with pytest.raises(store.ReconciliationSchemaError):
        store.reserve_repair_attempt(sample_reservation(), db_path=path)

    assert path.read_bytes() == before


def test_a_failed_migration_rolls_back_to_the_untouched_v1_file(tmp_path, monkeypatch):
    path = recorded_run_db(tmp_path)
    downgrade_to_v1(path)
    before = path.read_bytes()
    monkeypatch.setattr(
        store, "_REPAIRS_RUN_INDEX_SQL", "CREATE INDEX broken ON missing_table (x)"
    )

    with pytest.raises(store.ReconciliationStoreError):
        store.init_reconciliation_db(path)

    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == store._MIGRATABLE_VERSION


def test_a_v1_lookalike_with_extra_objects_is_not_migrated(tmp_path):
    path = recorded_run_db(tmp_path)
    downgrade_to_v1(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE stray (id INTEGER PRIMARY KEY)")
        connection.commit()
    before = path.read_bytes()
    with pytest.raises(store.ReconciliationSchemaError, match="unexpected"):
        store.init_reconciliation_db(path)
    assert path.read_bytes() == before
