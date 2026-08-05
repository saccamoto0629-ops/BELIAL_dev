"""SQLite audit persistence for broker reconciliation runs (Issue #103) and
repair attempts (Issue #104).

*** AUDIT STORE ONLY. It records what a reconciliation run OBSERVED and
what a repair pass DECIDED. It never writes to the execution order store,
never changes an order state and never talks to a broker -- the one
sanctioned repair write path into the order store is
``execution_store.record_order_result`` called by
``broker_reconciliation_repair``, never anything in this module. ***

One DB file (``execution/data/broker_reconciliation.sqlite3`` by default,
``BROKER_RECONCILIATION_DB_PATH`` to override), owned by ``execution/``
alone. It is a SEPARATE file from ``execution_orders.sqlite3`` /
``safety_gate_decisions.sqlite3`` / ``shadow_results.sqlite3`` for the same
reason those are separate from each other: every execution-owned store
verifies its DB as a closed world, so two schemas cannot share a file. The
separation is also a structural guarantee that this store cannot modify
order state -- its own DB has no order tables at all. Repair audit rows
live in this SAME file (not a fifth DB) because observation and repair are
two audit trails of the same broker-reconciliation component, and keeping
them together lets the repair rows carry a real FK to the run they acted
on.

Duplication policy (Issue #103 / #104, decided against the existing audit design)
---------------------------------------------------------------------------------

RUN-SCOPED APPEND. One row in ``broker_reconciliation_runs`` per run, plus
one row per observed discrepancy in
``broker_reconciliation_discrepancies`` (FK -> run). Two runs that observe
the identical situation store two complete, independent sets of rows:
nothing is deduplicated, updated or overwritten. This matches the audit
stance already used by ``safety_gate_decisions`` (every decision appended)
and ``execution_order_state_history`` (every observation appended) -- "what
did we see, and when" must stay reconstructible, and a later run must never
silently rewrite an earlier observation.

``run_id`` is the PRIMARY KEY and is supplied by the caller. Re-recording an
already stored ``run_id`` therefore raises
:class:`ReconciliationRunConflictError` instead of overwriting the stored
run: repeated recording cannot corrupt an existing audit trail, and a
repeated run simply uses a new id.

Repair attempts (Issue #104) follow the same stance: ATTEMPT-SCOPED APPEND
into ``broker_reconciliation_repair_attempts`` (FK -> run). Every attempt
-- applied, already applied, skipped, stale, conflicting, rejected, failed
-- is an independent audit event; re-running a repair appends new attempt
rows (typically ``already_applied``) and never updates or flags the stored
run / discrepancy rows. ``attempt_id`` is the PRIMARY KEY; re-recording one
raises :class:`RepairAttemptConflictError`. Dry-run attempts are never
recorded (:func:`record_repair_attempt` refuses them): dry-run is a
no-side-effect contract, not an audit category.

Schema policy: identical to ``execution_store`` / ``safety_store`` --
:func:`init_reconciliation_db` creates the schema only into a PRISTINE
database (zero ``sqlite_master`` objects AND ``user_version = 0``) and is
idempotent (an already-current DB only passes verification); anything else
fails with :class:`ReconciliationSchemaError` WITHOUT modifying the file.
Every read and write verifies the schema first as a closed world
(``sqlite_master`` object set + token-normalized DDL + PRAGMA structural
signature + version). Reads go through a TRUE read-only connection
(``execution/sqlite_ro.py``): a missing DB is never created, and every
restored row is re-validated strictly through the model invariants, so an
out-of-band edited DB raises :class:`ReconciliationIntegrityError` instead
of being trusted.

Migration (Issue #104): schema v2 adds the repair-attempts table and its
index; the v1 tables are byte-identical between the versions, so the
v1 -> v2 migration is purely ADDITIVE. ``init_reconciliation_db`` migrates
a COMPLETE, verified v1 schema in ONE ``BEGIN IMMEDIATE`` transaction
(create table -> create index -> version bump -> full v2 re-verification ->
COMMIT); existing rows are read back unchanged and a failed migration rolls
back to the untouched v1 file. Read paths NEVER migrate (they refuse a v1
DB with a schema error pointing at ``init_reconciliation_db``), so
dry-run / read-only contracts keep their no-side-effect guarantee.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from broker.broker_models import BrokerErrorCode, OrderState
from execution.execution_store import _ddl_tokens  # shared DDL token normalizer (PR #91 r3)
from execution.reconciliation_models import (
    DiscrepancyType,
    OrderDiscrepancy,
    ReconciliationMode,
    ReconciliationRun,
    ReconciliationTargetType,
    ResolutionCategory,
)
from execution.reconciliation_repair_models import (
    OrderRepairAttempt,
    RepairMode,
    RepairOutcome,
)
from execution.sqlite_ro import ReadOnlyDbMissingError, connect_read_only

_LOGGER = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "broker_reconciliation.sqlite3"
BUSY_TIMEOUT_SECONDS = 30

RUNS_TABLE = "broker_reconciliation_runs"
DISCREPANCIES_TABLE = "broker_reconciliation_discrepancies"
REPAIRS_TABLE = "broker_reconciliation_repair_attempts"

# v1: initial Issue #103 schema (runs + discrepancies).
# v2 (Issue #104): + broker_reconciliation_repair_attempts (repair audit).
#     Purely additive over v1 (the v1 tables are byte-identical); init
#     migrates a verified v1 DB in place (see the module docstring).
SCHEMA_VERSION = 2
_MIGRATABLE_VERSION = 1

_RUN_COLUMNS: tuple[str, ...] = (
    "run_id",
    "mode",
    "started_at_utc",
    "finished_at_utc",
    "checked_order_count",
    "broker_listing_error",
)
_DISCREPANCY_COLUMNS: tuple[str, ...] = (
    "run_id",
    "discrepancy_type",
    "target_type",
    "target_id",
    "resolution_category",
    "internal_state",
    "broker_state",
    "client_order_id",
    "broker_order_id",
    "broker_error_code",
    "detail",
    "observed_at_utc",
)
_REPAIR_COLUMNS: tuple[str, ...] = (
    "attempt_id",
    "run_id",
    "mode",
    "target_type",
    "target_id",
    "execution_request_id",
    "client_order_id",
    "outcome",
    "reconciliation_internal_state",
    "reconciliation_broker_state",
    "internal_state_before",
    "internal_state_after",
    "broker_order_id",
    "broker_error_code",
    "detail",
    "observed_at_utc",
    "attempted_at_utc",
    "applied_at_utc",
)


class ReconciliationStoreError(RuntimeError):
    """Base class for reconciliation store errors."""


class ReconciliationSchemaError(ReconciliationStoreError):
    """The on-disk schema does not match the expected Issue #103 schema."""


class ReconciliationIntegrityError(ReconciliationStoreError):
    """Stored data fails restore-time validation (out-of-band corruption)."""


class ReconciliationRunConflictError(ReconciliationStoreError):
    """A run with this ``run_id`` is already recorded (runs are never
    overwritten -- see the duplication policy in the module docstring)."""


class RepairAttemptConflictError(ReconciliationStoreError):
    """A repair attempt with this ``attempt_id`` is already recorded
    (attempts are appended, never overwritten -- Issue #104)."""


def reconciliation_db_path() -> Path:
    raw = (os.getenv("BROKER_RECONCILIATION_DB_PATH") or str(DEFAULT_DB_PATH)).strip()
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_utc(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError(f"stored timestamp is not UTC: {text!r}")
    return value


# --- schema ------------------------------------------------------------------


def _enum_values_sql(values) -> str:
    return "', '".join(sorted(v.value for v in values))


def _create_runs_table_sql() -> str:
    """Full current schema for the run rows. The CHECKs mirror the
    ``ReconciliationRun`` invariants as a second guard (known mode, UTC
    suffix, non-negative count, non-empty listing error when present)."""
    modes = _enum_values_sql(ReconciliationMode)
    return f"""
        CREATE TABLE {RUNS_TABLE} (
            run_id TEXT PRIMARY KEY NOT NULL CHECK (trim(run_id) <> ''),
            mode TEXT NOT NULL CHECK (mode IN ('{modes}')),
            started_at_utc TEXT NOT NULL CHECK (started_at_utc LIKE '%+00:00'),
            finished_at_utc TEXT NOT NULL CHECK (finished_at_utc LIKE '%+00:00'),
            checked_order_count INTEGER NOT NULL CHECK (checked_order_count >= 0),
            broker_listing_error TEXT
                CHECK (broker_listing_error IS NULL OR trim(broker_listing_error) <> '')
        ) WITHOUT ROWID
    """


def _create_discrepancies_table_sql() -> str:
    """Full current schema for the discrepancy rows. The CHECKs mirror the
    ``OrderDiscrepancy`` invariants: known enum members only, and the
    ``query_failed`` category is used exactly for the ``query_failure`` kind
    (which is also the only kind carrying a broker error code)."""
    types = _enum_values_sql(DiscrepancyType)
    targets = _enum_values_sql(ReconciliationTargetType)
    categories = _enum_values_sql(ResolutionCategory)
    states = _enum_values_sql(OrderState)
    codes = _enum_values_sql(BrokerErrorCode)
    query_failure = DiscrepancyType.QUERY_FAILURE.value
    query_failed = ResolutionCategory.QUERY_FAILED.value
    return f"""
        CREATE TABLE {DISCREPANCIES_TABLE} (
            discrepancy_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL
                REFERENCES {RUNS_TABLE} (run_id)
                CHECK (trim(run_id) <> ''),
            discrepancy_type TEXT NOT NULL CHECK (discrepancy_type IN ('{types}')),
            target_type TEXT NOT NULL CHECK (target_type IN ('{targets}')),
            target_id TEXT NOT NULL CHECK (trim(target_id) <> ''),
            resolution_category TEXT NOT NULL CHECK (resolution_category IN ('{categories}')),
            internal_state TEXT CHECK (internal_state IS NULL OR internal_state IN ('{states}')),
            broker_state TEXT CHECK (broker_state IS NULL OR broker_state IN ('{states}')),
            client_order_id TEXT CHECK (client_order_id IS NULL OR trim(client_order_id) <> ''),
            broker_order_id TEXT CHECK (broker_order_id IS NULL OR trim(broker_order_id) <> ''),
            broker_error_code TEXT
                CHECK (broker_error_code IS NULL OR broker_error_code IN ('{codes}')),
            detail TEXT NOT NULL CHECK (trim(detail) <> ''),
            observed_at_utc TEXT NOT NULL CHECK (observed_at_utc LIKE '%+00:00'),
            UNIQUE (run_id, target_type, target_id),
            CHECK ((discrepancy_type = '{query_failure}') = (resolution_category = '{query_failed}')),
            CHECK ((discrepancy_type = '{query_failure}') = (broker_error_code IS NOT NULL))
        )
    """


def _create_repairs_table_sql() -> str:
    """Full current schema for the repair-attempt rows (Issue #104). The
    CHECKs mirror the ``OrderRepairAttempt`` invariants as a second guard.
    ``mode`` admits only ``apply``: dry-run attempts are never persisted, so
    a stored row claiming one is corruption by construction. Likewise
    ``would_apply`` is not a storable outcome."""
    targets = _enum_values_sql(ReconciliationTargetType)
    states = _enum_values_sql(OrderState)
    codes = _enum_values_sql(BrokerErrorCode)
    storable_outcomes = "', '".join(
        sorted(o.value for o in RepairOutcome if o is not RepairOutcome.WOULD_APPLY)
    )
    apply_mode = RepairMode.APPLY.value
    applied = RepairOutcome.APPLIED.value
    already = RepairOutcome.ALREADY_APPLIED.value
    query_failed = RepairOutcome.QUERY_FAILED.value
    execution_target = ReconciliationTargetType.EXECUTION_ORDER.value
    return f"""
        CREATE TABLE {REPAIRS_TABLE} (
            attempt_id TEXT PRIMARY KEY NOT NULL CHECK (trim(attempt_id) <> ''),
            run_id TEXT NOT NULL
                REFERENCES {RUNS_TABLE} (run_id)
                CHECK (trim(run_id) <> ''),
            mode TEXT NOT NULL CHECK (mode IN ('{apply_mode}')),
            target_type TEXT NOT NULL CHECK (target_type IN ('{targets}')),
            target_id TEXT NOT NULL CHECK (trim(target_id) <> ''),
            execution_request_id TEXT
                CHECK (execution_request_id IS NULL OR trim(execution_request_id) <> ''),
            client_order_id TEXT CHECK (client_order_id IS NULL OR trim(client_order_id) <> ''),
            outcome TEXT NOT NULL CHECK (outcome IN ('{storable_outcomes}')),
            reconciliation_internal_state TEXT
                CHECK (reconciliation_internal_state IS NULL
                       OR reconciliation_internal_state IN ('{states}')),
            reconciliation_broker_state TEXT
                CHECK (reconciliation_broker_state IS NULL
                       OR reconciliation_broker_state IN ('{states}')),
            internal_state_before TEXT
                CHECK (internal_state_before IS NULL OR internal_state_before IN ('{states}')),
            internal_state_after TEXT
                CHECK (internal_state_after IS NULL OR internal_state_after IN ('{states}')),
            broker_order_id TEXT CHECK (broker_order_id IS NULL OR trim(broker_order_id) <> ''),
            broker_error_code TEXT
                CHECK (broker_error_code IS NULL OR broker_error_code IN ('{codes}')),
            detail TEXT NOT NULL CHECK (trim(detail) <> ''),
            observed_at_utc TEXT CHECK (observed_at_utc IS NULL OR observed_at_utc LIKE '%+00:00'),
            attempted_at_utc TEXT NOT NULL CHECK (attempted_at_utc LIKE '%+00:00'),
            applied_at_utc TEXT CHECK (applied_at_utc IS NULL OR applied_at_utc LIKE '%+00:00'),
            CHECK ((target_type = '{execution_target}') = (execution_request_id IS NOT NULL)),
            CHECK ((outcome = '{applied}') = (applied_at_utc IS NOT NULL)),
            CHECK ((outcome IN ('{applied}', '{already}')) = (internal_state_after IS NOT NULL)),
            CHECK ((outcome = '{query_failed}') = (broker_error_code IS NOT NULL))
        ) WITHOUT ROWID
    """


_DISCREPANCIES_RUN_INDEX = f"idx_{DISCREPANCIES_TABLE}_run"
_REPAIRS_RUN_INDEX = f"idx_{REPAIRS_TABLE}_run"
_REPAIRS_RUN_INDEX_SQL = (
    f"CREATE INDEX {_REPAIRS_RUN_INDEX} ON {REPAIRS_TABLE} (run_id, attempted_at_utc, attempt_id)"
)


def _create_index_sql() -> str:
    return (
        f"CREATE INDEX {_DISCREPANCIES_RUN_INDEX} ON {DISCREPANCIES_TABLE} "
        "(run_id, discrepancy_id)"
    )


def _tables_for(version: int) -> tuple[str, ...]:
    tables = [RUNS_TABLE, DISCREPANCIES_TABLE]
    if version >= SCHEMA_VERSION:
        tables.append(REPAIRS_TABLE)
    return tuple(tables)


def _create_schema(connection: sqlite3.Connection, *, version: int = SCHEMA_VERSION) -> None:
    """Create the schema for ``version`` (v1 is used only as the in-memory
    migration reference and by migration tests; real DBs are created at the
    current version)."""
    connection.execute(_create_runs_table_sql())
    connection.execute(_create_discrepancies_table_sql())
    connection.execute(_create_index_sql())
    if version >= SCHEMA_VERSION:
        connection.execute(_create_repairs_table_sql())
        connection.execute(_REPAIRS_RUN_INDEX_SQL)
    connection.execute(f"PRAGMA user_version = {version}")


def _structure_signature(connection: sqlite3.Connection, *, version: int = SCHEMA_VERSION) -> dict:
    signature: dict = {}
    for table in _tables_for(version):
        signature[f"table_info:{table}"] = [
            tuple(row) for row in connection.execute(f"PRAGMA table_info({table})")
        ]
        signature[f"foreign_keys:{table}"] = [
            tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({table})")
        ]
        index_entries = []
        for index_row in connection.execute(f"PRAGMA index_list({table})"):
            columns = [
                tuple(info) for info in connection.execute(f"PRAGMA index_info({index_row[1]})")
            ]
            index_entries.append((index_row[1], index_row[2], index_row[3], index_row[4], columns))
        signature[f"indexes:{table}"] = sorted(index_entries)
    return signature


_REFERENCE_SCHEMA_CACHE: dict[int, tuple[dict, dict]] = {}


def _reference_schema(version: int = SCHEMA_VERSION) -> tuple[dict, dict]:
    """Pristine reference schema for ``version``, built once in memory: the
    complete ``sqlite_master`` object map (SQLite-internal objects included --
    ``sqlite_sequence`` from AUTOINCREMENT, ``sqlite_autoindex_*`` from the
    PRIMARY KEYs) plus the PRAGMA structural signature. The v1 reference
    exists solely so migration can prove a DB is EXACTLY a valid v1 schema
    before touching it."""
    global _REFERENCE_SCHEMA_CACHE
    if not isinstance(_REFERENCE_SCHEMA_CACHE, dict):  # tests reset it to None
        _REFERENCE_SCHEMA_CACHE = {}
    if version not in _REFERENCE_SCHEMA_CACHE:
        with closing(sqlite3.connect(":memory:")) as connection:
            _create_schema(connection, version=version)
            objects = {
                (row[0], row[1]): (row[2], None if row[3] is None else _ddl_tokens(row[3]))
                for row in connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")
            }
            _REFERENCE_SCHEMA_CACHE[version] = (
                objects,
                _structure_signature(connection, version=version),
            )
    return _REFERENCE_SCHEMA_CACHE[version]


def _schema_problems(connection: sqlite3.Connection, *, version: int = SCHEMA_VERSION) -> list[str]:
    """Closed-world structural diff against the ``version`` reference schema;
    returns the list of problems (empty = exact match). Read-only."""
    problems: list[str] = []
    actual_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if actual_version != version:
        problems.append(f"user_version is {actual_version}, expected {version}")

    expected_objects, expected_signature = _reference_schema(version)
    actual_objects = {
        (row[0], row[1]): (row[2], row[3])
        for row in connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")
    }
    for object_type, name in sorted(set(expected_objects) - set(actual_objects)):
        problems.append(f"{object_type} {name!r} is missing")
    for object_type, name in sorted(set(actual_objects) - set(expected_objects)):
        problems.append(f"unexpected {object_type} {name!r} present")
    for key in sorted(set(actual_objects) & set(expected_objects)):
        expected_tbl, expected_tokens = expected_objects[key]
        actual_tbl, actual_sql = actual_objects[key]
        actual_tokens = None if actual_sql is None else _ddl_tokens(actual_sql)
        if actual_tbl != expected_tbl or actual_tokens != expected_tokens:
            problems.append(
                f"{key[0]} {key[1]!r} does not match the expected definition "
                "(constraints missing or altered)"
            )

    if not problems and _structure_signature(connection, version=version) != expected_signature:
        problems.append(
            "live PRAGMA structure (table_info/index_list/index_info/foreign_key_list) "
            "differs from the reference schema"
        )
    return problems


def _verify_schema(connection: sqlite3.Connection, db_path: Path) -> None:
    """Closed-world verification (same stance as ``execution_store`` /
    ``safety_store``): the COMPLETE ``sqlite_master`` object set -- triggers
    and views included -- must exactly equal the pristine reference set, the
    token-normalized DDL of every SQL-bearing object must match, the PRAGMA
    structural signature must match, and the schema version must match.
    Read-only. A DB that is a valid v1 schema is still refused here (only
    :func:`init_reconciliation_db` migrates); the error says so explicitly."""
    problems = _schema_problems(connection)
    if problems:
        if not _schema_problems(connection, version=_MIGRATABLE_VERSION):
            problems.append(
                f"the DB is a valid v{_MIGRATABLE_VERSION} schema; run "
                f"init_reconciliation_db() to migrate it to v{SCHEMA_VERSION} "
                "(reads never migrate)"
            )
        raise ReconciliationSchemaError(
            f"broker reconciliation DB at {db_path} failed schema verification; "
            "refusing to touch it: " + "; ".join(problems)
        )


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.row_factory = sqlite3.Row
    return connection


def _translate_sqlite_error(db_path: Path, operation: str, exc: sqlite3.DatabaseError):
    """Map raw sqlite failures onto the store's public error contract.

    Corrupt/non-SQLite files are schema failures; transient operational
    failures (lock/read-only/open errors) are store availability failures.
    Callers must not need sqlite implementation exceptions to distinguish an
    invalid audit DB from a temporarily unavailable one.
    """
    message = str(exc).lower()
    transient = isinstance(exc, sqlite3.OperationalError) and any(
        marker in message
        for marker in ("locked", "busy", "readonly", "read-only", "unable to open")
    )
    error_type = ReconciliationStoreError if transient else ReconciliationSchemaError
    return error_type(
        f"broker reconciliation DB at {db_path} failed during {operation}: "
        f"{type(exc).__name__}: {exc}"
    )


@contextmanager
def _verified_txn(db_path: Path):
    """Write flow: open -> BEGIN IMMEDIATE -> verify schema -> row work ->
    COMMIT (ROLLBACK on any error, so a failed write leaves the DB
    untouched). Connection always explicitly closed."""
    try:
        with closing(_connect(db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _verify_schema(connection, db_path)
                yield connection
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
    except ReconciliationStoreError:
        raise
    except sqlite3.DatabaseError as exc:
        raise _translate_sqlite_error(db_path, "verified write", exc) from exc


@contextmanager
def _verified_read(db_path: Path):
    """Read flow: TRUE read-only connection (``mode=ro`` + ``query_only`` +
    authorizer: never creates the file, journals or WAL/SHM side files) ->
    BEGIN -> verify schema -> restore. Always rolled back."""
    try:
        with closing(connect_read_only(db_path)) as connection:
            connection.execute("BEGIN")
            try:
                _verify_schema(connection, db_path)
                yield connection
            finally:
                connection.execute("ROLLBACK")
    except ReconciliationStoreError:
        raise
    except sqlite3.DatabaseError as exc:
        raise _translate_sqlite_error(db_path, "verified read", exc) from exc


def _migrate_v1_to_v2(connection: sqlite3.Connection, db_path: Path) -> None:
    """Migrate a fully verified v1 schema to v2 inside the caller's
    transaction (Issue #104): additive ``CREATE TABLE`` for the repair
    attempts + its index + version bump. Existing rows are untouched;
    nothing is deleted or rewritten. The caller re-verifies the complete v2
    schema before COMMIT; any failure rolls back to the untouched v1 file."""
    connection.execute(_create_repairs_table_sql())
    connection.execute(_REPAIRS_RUN_INDEX_SQL)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    _LOGGER.info(
        "migrated broker reconciliation DB %s from schema v%d to v%d (added repair "
        "attempts table %s)",
        db_path,
        _MIGRATABLE_VERSION,
        SCHEMA_VERSION,
        REPAIRS_TABLE,
    )


def init_reconciliation_db(db_path: Path | None = None) -> None:
    """Create the current schema, verify an existing one, or migrate a valid
    v1 schema to v2 (Issue #104); idempotent.

    Same pristine rule as ``execution_store.init_db`` /
    ``SQLiteSafetyDecisionStore.initialize``: creation happens only when
    ``sqlite_master`` is empty AND ``user_version`` is 0. A DB that fully
    matches the CURRENT schema passes verification unchanged. A DB that
    fully matches the released v1 schema -- and ONLY such a DB -- is
    migrated in place (:func:`_migrate_v1_to_v2`); the migration, the
    version bump and a full v2 re-verification commit in ONE transaction, so
    a failed migration leaves no half-migrated schema behind. Any other
    content fails with :class:`ReconciliationSchemaError` WITHOUT modifying
    the file (no auto-completion, no silent rebuild).
    """
    db_path = Path(db_path) if db_path is not None else reconciliation_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(_connect(db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                objects = connection.execute("SELECT name FROM sqlite_master").fetchall()
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if not objects:
                    if version != 0:
                        raise ReconciliationSchemaError(
                            f"DB at {db_path} is empty but carries user_version {version}; "
                            "not a pristine DB -- refusing to initialize it"
                        )
                    _create_schema(connection)
                elif _schema_problems(connection):
                    if _schema_problems(connection, version=_MIGRATABLE_VERSION):
                        # neither a valid current nor a valid v1 schema: fail
                        # with the full diagnosis, DB untouched
                        _verify_schema(connection, db_path)
                    _migrate_v1_to_v2(connection, db_path)
                _verify_schema(connection, db_path)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
    except ReconciliationStoreError:
        raise
    except sqlite3.DatabaseError as exc:
        raise _translate_sqlite_error(db_path, "initialization", exc) from exc


def record_reconciliation_run(run: ReconciliationRun, *, db_path: Path | None = None) -> None:
    """Persist one run and all its discrepancies in ONE transaction.

    A missing DB file is a failure, not a trigger for implicit creation
    (same stance as the safety decision store): the audit DB is initialized
    deliberately via :func:`init_reconciliation_db`. Re-recording a stored
    ``run_id`` raises :class:`ReconciliationRunConflictError` and changes
    nothing -- runs are appended, never overwritten.
    """
    if not isinstance(run, ReconciliationRun):
        raise ReconciliationStoreError(f"run must be a ReconciliationRun, got {run!r}")
    db_path = Path(db_path) if db_path is not None else reconciliation_db_path()
    if not db_path.exists():
        raise ReconciliationStoreError(
            f"broker reconciliation DB at {db_path} does not exist; call "
            "init_reconciliation_db() first"
        )
    run_record = (
        run.run_id,
        run.mode.value,
        _iso(run.started_at_utc),
        _iso(run.finished_at_utc),
        run.checked_order_count,
        run.broker_listing_error,
    )
    with _verified_txn(db_path) as connection:
        existing = connection.execute(
            f"SELECT run_id FROM {RUNS_TABLE} WHERE run_id = ?", (run.run_id,)
        ).fetchone()
        if existing is not None:
            raise ReconciliationRunConflictError(
                f"reconciliation run {run.run_id!r} is already recorded; runs are appended "
                "under a new run_id, never overwritten"
            )
        try:
            connection.execute(
                f"INSERT INTO {RUNS_TABLE} ({', '.join(_RUN_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _RUN_COLUMNS)})",
                run_record,
            )
            for discrepancy in run.discrepancies:
                connection.execute(
                    f"INSERT INTO {DISCREPANCIES_TABLE} ({', '.join(_DISCREPANCY_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _DISCREPANCY_COLUMNS)})",
                    (
                        run.run_id,
                        discrepancy.discrepancy_type.value,
                        discrepancy.target_type.value,
                        discrepancy.target_id,
                        discrepancy.resolution_category.value,
                        None if discrepancy.internal_state is None else discrepancy.internal_state.value,
                        None if discrepancy.broker_state is None else discrepancy.broker_state.value,
                        discrepancy.client_order_id,
                        discrepancy.broker_order_id,
                        None
                        if discrepancy.broker_error_code is None
                        else discrepancy.broker_error_code.value,
                        discrepancy.detail,
                        _iso(discrepancy.observed_at_utc),
                    ),
                )
        except sqlite3.IntegrityError as exc:  # UNIQUE / CHECK / FK violation
            raise ReconciliationStoreError(
                f"recording reconciliation run {run.run_id!r} violates a DB constraint: {exc}"
            ) from exc


# --- read paths ---------------------------------------------------------------


def _restore_discrepancy(row: sqlite3.Row, run_id: str) -> OrderDiscrepancy:
    """Rebuild one discrepancy; the model re-runs every invariant, so an
    out-of-band edited row fails loudly instead of being trusted."""
    try:
        return OrderDiscrepancy(
            discrepancy_type=DiscrepancyType(row["discrepancy_type"]),
            target_type=ReconciliationTargetType(row["target_type"]),
            target_id=row["target_id"],
            resolution_category=ResolutionCategory(row["resolution_category"]),
            observed_at_utc=_parse_utc(row["observed_at_utc"]),
            detail=row["detail"],
            internal_state=None if row["internal_state"] is None else OrderState(row["internal_state"]),
            broker_state=None if row["broker_state"] is None else OrderState(row["broker_state"]),
            client_order_id=row["client_order_id"],
            broker_order_id=row["broker_order_id"],
            broker_error_code=None
            if row["broker_error_code"] is None
            else BrokerErrorCode(row["broker_error_code"]),
        )
    except (ValueError, TypeError) as exc:
        raise ReconciliationIntegrityError(
            f"discrepancy row {row['discrepancy_id']} of reconciliation run {run_id!r} "
            f"failed restore-time validation: {exc}"
        ) from exc


def _restore_run(connection: sqlite3.Connection, row: sqlite3.Row) -> ReconciliationRun:
    run_id = row["run_id"]
    discrepancies = tuple(
        _restore_discrepancy(entry, run_id)
        for entry in connection.execute(
            f"SELECT * FROM {DISCREPANCIES_TABLE} WHERE run_id = ? ORDER BY discrepancy_id ASC",
            (run_id,),
        ).fetchall()
    )
    try:
        return ReconciliationRun(
            run_id=run_id,
            mode=ReconciliationMode(row["mode"]),
            started_at_utc=_parse_utc(row["started_at_utc"]),
            finished_at_utc=_parse_utc(row["finished_at_utc"]),
            checked_order_count=row["checked_order_count"],
            discrepancies=discrepancies,
            broker_listing_error=row["broker_listing_error"],
        )
    except (ValueError, TypeError) as exc:
        raise ReconciliationIntegrityError(
            f"reconciliation run {run_id!r} failed restore-time validation: {exc}"
        ) from exc


def get_reconciliation_run(
    run_id: str, *, db_path: Path | None = None
) -> ReconciliationRun | None:
    """The stored run (with its discrepancies in observation order), or None.

    Missing DB / unknown id -> None; the DB file is never created.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise ReconciliationStoreError(f"run_id must be a non-empty string, got {run_id!r}")
    db_path = Path(db_path) if db_path is not None else reconciliation_db_path()
    if not db_path.exists():
        return None
    try:
        with _verified_read(db_path) as connection:
            row = connection.execute(
                f"SELECT * FROM {RUNS_TABLE} WHERE run_id = ?", (run_id,)
            ).fetchone()
            return None if row is None else _restore_run(connection, row)
    except ReadOnlyDbMissingError:
        return None


def list_reconciliation_runs(
    *, limit: int = 50, db_path: Path | None = None
) -> tuple[ReconciliationRun, ...]:
    """The most recent runs, newest first (by ``started_at_utc``, ``run_id``
    as the tie-break so the order is total and stable).

    Missing DB -> empty tuple, file never created.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ReconciliationStoreError(f"limit must be a positive int, got {limit!r}")
    db_path = Path(db_path) if db_path is not None else reconciliation_db_path()
    if not db_path.exists():
        return ()
    try:
        with _verified_read(db_path) as connection:
            rows = connection.execute(
                f"SELECT * FROM {RUNS_TABLE} ORDER BY started_at_utc DESC, run_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return tuple(_restore_run(connection, row) for row in rows)
    except ReadOnlyDbMissingError:
        return ()


# --- repair attempts (Issue #104) ---------------------------------------------


def record_repair_attempt(attempt: OrderRepairAttempt, *, db_path: Path | None = None) -> None:
    """Append one repair-attempt audit row (Issue #104).

    Only APPLY-mode attempts are recordable: a dry-run pass has a
    no-side-effect contract, so persisting its attempts would itself violate
    it (and a ``would_apply`` outcome is refused for the same reason). A
    missing DB file is a failure, not a trigger for implicit creation. The
    run the attempt refers to must already be recorded (FK -> runs; also
    pre-checked for a readable error). Re-recording a stored ``attempt_id``
    raises :class:`RepairAttemptConflictError` and changes nothing.
    ``attempt.audit_error`` is in-memory metadata and is never persisted.
    """
    if not isinstance(attempt, OrderRepairAttempt):
        raise ReconciliationStoreError(f"attempt must be an OrderRepairAttempt, got {attempt!r}")
    if attempt.mode is not RepairMode.APPLY:
        raise ReconciliationStoreError(
            f"only apply-mode repair attempts are recorded; got mode {attempt.mode.value!r} "
            "(dry-run is a no-side-effect contract)"
        )
    db_path = Path(db_path) if db_path is not None else reconciliation_db_path()
    if not db_path.exists():
        raise ReconciliationStoreError(
            f"broker reconciliation DB at {db_path} does not exist; call "
            "init_reconciliation_db() first"
        )
    record = (
        attempt.attempt_id,
        attempt.run_id,
        attempt.mode.value,
        attempt.target_type.value,
        attempt.target_id,
        attempt.execution_request_id,
        attempt.client_order_id,
        attempt.outcome.value,
        None
        if attempt.reconciliation_internal_state is None
        else attempt.reconciliation_internal_state.value,
        None
        if attempt.reconciliation_broker_state is None
        else attempt.reconciliation_broker_state.value,
        None if attempt.internal_state_before is None else attempt.internal_state_before.value,
        None if attempt.internal_state_after is None else attempt.internal_state_after.value,
        attempt.broker_order_id,
        None if attempt.broker_error_code is None else attempt.broker_error_code.value,
        attempt.detail,
        None if attempt.observed_at_utc is None else _iso(attempt.observed_at_utc),
        _iso(attempt.attempted_at_utc),
        None if attempt.applied_at_utc is None else _iso(attempt.applied_at_utc),
    )
    with _verified_txn(db_path) as connection:
        existing = connection.execute(
            f"SELECT attempt_id FROM {REPAIRS_TABLE} WHERE attempt_id = ?",
            (attempt.attempt_id,),
        ).fetchone()
        if existing is not None:
            raise RepairAttemptConflictError(
                f"repair attempt {attempt.attempt_id!r} is already recorded; attempts are "
                "appended under a new attempt_id, never overwritten"
            )
        run_exists = connection.execute(
            f"SELECT run_id FROM {RUNS_TABLE} WHERE run_id = ?", (attempt.run_id,)
        ).fetchone()
        if run_exists is None:
            raise ReconciliationStoreError(
                f"repair attempt {attempt.attempt_id!r} refers to reconciliation run "
                f"{attempt.run_id!r}, which is not recorded in this DB; repairs act on "
                "recorded runs only"
            )
        try:
            connection.execute(
                f"INSERT INTO {REPAIRS_TABLE} ({', '.join(_REPAIR_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _REPAIR_COLUMNS)})",
                record,
            )
        except sqlite3.IntegrityError as exc:  # UNIQUE / CHECK / FK violation
            raise ReconciliationStoreError(
                f"recording repair attempt {attempt.attempt_id!r} violates a DB "
                f"constraint: {exc}"
            ) from exc


def _restore_repair_attempt(row: sqlite3.Row) -> OrderRepairAttempt:
    """Rebuild one attempt; the model re-runs every invariant, so an
    out-of-band edited row fails loudly instead of being trusted."""

    def state(name):
        return None if row[name] is None else OrderState(row[name])

    try:
        return OrderRepairAttempt(
            attempt_id=row["attempt_id"],
            run_id=row["run_id"],
            mode=RepairMode(row["mode"]),
            target_type=ReconciliationTargetType(row["target_type"]),
            target_id=row["target_id"],
            outcome=RepairOutcome(row["outcome"]),
            detail=row["detail"],
            attempted_at_utc=_parse_utc(row["attempted_at_utc"]),
            execution_request_id=row["execution_request_id"],
            client_order_id=row["client_order_id"],
            reconciliation_internal_state=state("reconciliation_internal_state"),
            reconciliation_broker_state=state("reconciliation_broker_state"),
            internal_state_before=state("internal_state_before"),
            internal_state_after=state("internal_state_after"),
            broker_order_id=row["broker_order_id"],
            broker_error_code=None
            if row["broker_error_code"] is None
            else BrokerErrorCode(row["broker_error_code"]),
            observed_at_utc=None
            if row["observed_at_utc"] is None
            else _parse_utc(row["observed_at_utc"]),
            applied_at_utc=None
            if row["applied_at_utc"] is None
            else _parse_utc(row["applied_at_utc"]),
        )
    except (ValueError, TypeError) as exc:
        raise ReconciliationIntegrityError(
            f"repair attempt row {row['attempt_id']!r} failed restore-time validation: {exc}"
        ) from exc


def get_repair_attempt(attempt_id: str, *, db_path: Path | None = None) -> OrderRepairAttempt | None:
    """The stored repair attempt, or None. Missing DB / unknown id -> None;
    the DB file is never created."""
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ReconciliationStoreError(f"attempt_id must be a non-empty string, got {attempt_id!r}")
    db_path = Path(db_path) if db_path is not None else reconciliation_db_path()
    if not db_path.exists():
        return None
    try:
        with _verified_read(db_path) as connection:
            row = connection.execute(
                f"SELECT * FROM {REPAIRS_TABLE} WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            return None if row is None else _restore_repair_attempt(row)
    except ReadOnlyDbMissingError:
        return None


def list_repair_attempts(
    *, run_id: str | None = None, db_path: Path | None = None
) -> tuple[OrderRepairAttempt, ...]:
    """The stored repair attempts, oldest first (``attempted_at_utc``,
    ``attempt_id`` as the tie-break so the order is total and stable),
    optionally restricted to one reconciliation run.

    Missing DB -> empty tuple, file never created.
    """
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise ReconciliationStoreError(f"run_id must be a non-empty string or None, got {run_id!r}")
    db_path = Path(db_path) if db_path is not None else reconciliation_db_path()
    if not db_path.exists():
        return ()
    where = "" if run_id is None else " WHERE run_id = ?"
    parameters = () if run_id is None else (run_id,)
    try:
        with _verified_read(db_path) as connection:
            rows = connection.execute(
                f"SELECT * FROM {REPAIRS_TABLE}{where} "
                "ORDER BY attempted_at_utc ASC, attempt_id ASC",
                parameters,
            ).fetchall()
            return tuple(_restore_repair_attempt(row) for row in rows)
    except ReadOnlyDbMissingError:
        return ()
