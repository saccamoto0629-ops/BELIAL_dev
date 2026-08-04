"""SQLite audit persistence for Safety Gate decisions (Issue #75).

*** AUDIT STORE ONLY. No broker calls, no network I/O. This store persists
allow/reject decisions so a later audit can reconstruct WHY an order was (not)
sent; it never submits anything. ***

One DB file (``execution/data/safety_gate_decisions.sqlite3`` by default,
``SAFETY_GATE_DECISION_DB_PATH`` to override), owned by ``execution/`` alone.
It is deliberately a SEPARATE file from ``execution_orders.sqlite3``:
``execution_store`` verifies its DB as a closed world (any foreign table
fails verification), so the two schemas cannot share a file.

Fail-closed contract (Issue #75): :meth:`SQLiteSafetyDecisionStore.record`
raises on ANY failure -- missing/uninitialized DB, schema mismatch, insert
failure. ``LiveOrderSubmissionService`` (``execution/live_submission.py``)
treats that exception -- and a failed read-back verification of the recorded
decision -- as ``DECISION_PERSIST_FAILED`` and does not submit, so a broken
audit trail can never be bypassed silently. The service also accepts ONLY
this durable store type: in-memory / no-op recorders cannot enter the
production submission path.

Schema policy (same stance as ``execution_store``, PR #91): ``initialize()``
creates the schema only into a PRISTINE database (zero ``sqlite_master``
objects and ``user_version = 0``); anything else must pass full verification
or fails with :class:`SafetySchemaError` WITHOUT modifying the file. Every
read and write verifies the schema first -- a closed-world comparison of the
complete ``sqlite_master`` object set (token-normalized DDL, so cosmetic
differences are absorbed while missing/altered CHECKs, triggers, views and
extra objects all fail) plus the PRAGMA structural signature and the schema
version. Reads never create a missing DB file (dry-run safe) and re-validate
every stored row strictly (exact JSON key sets, known rejection codes, UTC
timestamps, allow/reject consistency) -- an out-of-band edited DB raises
:class:`SafetyIntegrityError` instead of being trusted. Connections are
always explicitly closed (Windows file-handle hygiene, Issue #83 r2).
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from execution.execution_store import _ddl_tokens  # shared DDL token normalizer (PR #91 r3)
from execution.safety_gate import SafetyDecision, SafetyRejection, SafetyRejectionCode
from execution.sqlite_ro import connect_read_only

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "safety_gate_decisions.sqlite3"
BUSY_TIMEOUT_SECONDS = 30

DECISIONS_TABLE = "safety_gate_decisions"
SCHEMA_VERSION = 1

_DECISION_COLUMNS: tuple[str, ...] = (
    "execution_request_id",
    "allowed",
    "checked_at_utc",
    "policy_id",
    "execution_mode",
    "connection_target",
    "rejections_json",
    "account_id",
    "position_count",
)

# Strict rejection JSON shape: each entry must hold exactly these keys.
_REJECTION_JSON_KEYS: frozenset[str] = frozenset({"code", "message"})


class SafetyStoreError(RuntimeError):
    """Base class for safety decision store errors."""


class SafetySchemaError(SafetyStoreError):
    """The on-disk schema does not match the expected Issue #75 schema."""


class SafetyIntegrityError(SafetyStoreError):
    """Stored data fails restore-time validation (out-of-band corruption)."""


def safety_db_path() -> Path:
    raw = (os.getenv("SAFETY_GATE_DECISION_DB_PATH") or str(DEFAULT_DB_PATH)).strip()
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


def _parse_utc(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError(f"stored timestamp is not UTC: {text!r}")
    return value


def _create_decisions_table_sql() -> str:
    """Full current schema. The CHECKs mirror the ``SafetyDecision`` model
    invariants as a second guard -- foremost allow/reject consistency:
    an allowed row must carry an empty rejections list and vice versa."""
    return f"""
        CREATE TABLE {DECISIONS_TABLE} (
            decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_request_id TEXT NOT NULL CHECK (trim(execution_request_id) <> ''),
            allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
            checked_at_utc TEXT NOT NULL CHECK (checked_at_utc LIKE '%+00:00'),
            policy_id TEXT NOT NULL CHECK (trim(policy_id) <> ''),
            execution_mode TEXT CHECK (execution_mode IS NULL OR trim(execution_mode) <> ''),
            connection_target TEXT CHECK (connection_target IS NULL OR trim(connection_target) <> ''),
            rejections_json TEXT NOT NULL CHECK (trim(rejections_json) <> ''),
            account_id TEXT CHECK (account_id IS NULL OR trim(account_id) <> ''),
            position_count INTEGER CHECK (position_count IS NULL OR position_count >= 0),
            CHECK ((allowed = 1) = (rejections_json = '[]'))
        )
    """


_DECISIONS_REQUEST_INDEX = f"idx_{DECISIONS_TABLE}_request"


def _create_index_sql() -> str:
    return (
        f"CREATE INDEX {_DECISIONS_REQUEST_INDEX} ON {DECISIONS_TABLE} "
        "(execution_request_id, decision_id)"
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_create_decisions_table_sql())
    connection.execute(_create_index_sql())
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _structure_signature(connection: sqlite3.Connection) -> dict:
    signature: dict = {
        f"table_info:{DECISIONS_TABLE}": [
            tuple(row) for row in connection.execute(f"PRAGMA table_info({DECISIONS_TABLE})")
        ],
        f"foreign_keys:{DECISIONS_TABLE}": [
            tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({DECISIONS_TABLE})")
        ],
    }
    index_entries = []
    for index_row in connection.execute(f"PRAGMA index_list({DECISIONS_TABLE})"):
        columns = [
            tuple(info) for info in connection.execute(f"PRAGMA index_info({index_row[1]})")
        ]
        index_entries.append((index_row[1], index_row[2], index_row[3], index_row[4], columns))
    signature[f"indexes:{DECISIONS_TABLE}"] = sorted(index_entries)
    return signature


_REFERENCE_SCHEMA_CACHE: tuple[dict, dict] | None = None


def _reference_schema() -> tuple[dict, dict]:
    """Pristine reference schema built once in memory: the complete
    ``sqlite_master`` object map (``sqlite_sequence`` from AUTOINCREMENT
    included) plus the PRAGMA structural signature."""
    global _REFERENCE_SCHEMA_CACHE
    if _REFERENCE_SCHEMA_CACHE is None:
        with closing(sqlite3.connect(":memory:")) as connection:
            _create_schema(connection)
            objects = {
                (row[0], row[1]): (row[2], None if row[3] is None else _ddl_tokens(row[3]))
                for row in connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")
            }
            _REFERENCE_SCHEMA_CACHE = (objects, _structure_signature(connection))
    return _REFERENCE_SCHEMA_CACHE


def _verify_schema(connection: sqlite3.Connection, db_path: Path) -> None:
    """Closed-world verification (same stance as ``execution_store``):
    the COMPLETE ``sqlite_master`` object set -- triggers and views included --
    must exactly equal the pristine reference set, the token-normalized DDL of
    every SQL-bearing object must match, the PRAGMA structural signature must
    match, and the schema version must match. Read-only."""
    problems: list[str] = []
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        problems.append(f"user_version is {version}, expected {SCHEMA_VERSION}")

    expected_objects, expected_signature = _reference_schema()
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

    if not problems and _structure_signature(connection) != expected_signature:
        problems.append(
            "live PRAGMA structure (table_info/index_list/index_info/foreign_key_list) "
            "differs from the reference schema"
        )

    if problems:
        raise SafetySchemaError(
            f"safety decision DB at {db_path} failed schema verification; "
            "refusing to touch it: " + "; ".join(problems)
        )


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}")
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def _verified_txn(db_path: Path):
    """Write flow: open -> BEGIN IMMEDIATE -> verify schema -> row work ->
    COMMIT (ROLLBACK on any error). Connection always explicitly closed."""
    with closing(_connect(db_path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _verify_schema(connection, db_path)
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise


@contextmanager
def _verified_read(db_path: Path):
    """Read flow: open a TRUE read-only connection (``mode=ro`` +
    ``PRAGMA query_only``, PR #100 review: a read can never create the file,
    journals or WAL/SHM side files, and any write attempt fails) -> BEGIN
    (consistent view) -> verify schema -> restore. Always rolled back."""
    with closing(connect_read_only(db_path)) as connection:
        connection.execute("BEGIN")
        try:
            _verify_schema(connection, db_path)
            yield connection
        finally:
            connection.execute("ROLLBACK")


def _rejections_to_json(rejections: tuple[SafetyRejection, ...]) -> str:
    return json.dumps(
        [{"code": item.code.value, "message": item.message} for item in rejections],
        separators=(",", ":"),
    )


def _rejections_from_json(text: str) -> tuple[SafetyRejection, ...]:
    """Rebuild rejections STRICTLY: a list of objects each holding exactly
    ``code`` (a known ``SafetyRejectionCode`` value) and ``message`` (a
    non-empty string). Anything else raises ``ValueError``."""
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"rejections JSON must be a list, got {type(data).__name__}")
    rejections = []
    for entry in data:
        if not isinstance(entry, dict) or set(entry) != _REJECTION_JSON_KEYS:
            raise ValueError(
                f"rejection entry keys {sorted(entry) if isinstance(entry, dict) else type(entry).__name__} "
                f"do not match the expected set {sorted(_REJECTION_JSON_KEYS)}"
            )
        rejections.append(SafetyRejection(SafetyRejectionCode(entry["code"]), entry["message"]))
    return tuple(rejections)


class SQLiteSafetyDecisionStore:
    """The DURABLE Safety Gate decision store (the audit trail).

    ``record`` raises :class:`SafetyStoreError` subclasses on any failure;
    the sanctioned submission path (``LiveOrderSubmissionService``) converts
    that into a fail-closed ``DECISION_PERSIST_FAILED`` rejection and
    additionally verifies every recorded decision by read-back.
    """

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path is not None else safety_db_path()

    @property
    def path(self) -> Path:
        return self._path

    def initialize(self) -> None:
        """Create the schema (pristine DB only) or verify an existing one.

        Same pristine rule as ``execution_store.init_db``: creation happens
        only when ``sqlite_master`` is empty AND ``user_version`` is 0; any
        other content must pass full verification or fails WITHOUT modifying
        the file.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(_connect(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                objects = connection.execute("SELECT name FROM sqlite_master").fetchall()
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if not objects:
                    if version != 0:
                        raise SafetySchemaError(
                            f"DB at {self._path} is empty but carries user_version {version}; "
                            "not a pristine DB -- refusing to initialize it"
                        )
                    _create_schema(connection)
                    _verify_schema(connection, self._path)
                else:
                    _verify_schema(connection, self._path)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def record(self, decision: SafetyDecision) -> int:
        """Persist one decision; raises on ANY failure (fail closed).
        Returns the assigned ``decision_id`` (Issue #78: shadow results
        reference the decision row they were built from).

        A missing DB file is a failure, not a trigger for implicit creation:
        the audit store must be initialized deliberately, and a submission
        path must never run without one.
        """
        if not isinstance(decision, SafetyDecision):
            raise SafetyStoreError(f"decision must be a SafetyDecision, got {decision!r}")
        if not self._path.exists():
            raise SafetyStoreError(
                f"safety decision DB at {self._path} does not exist; call initialize() first"
            )
        record = (
            decision.execution_request_id,
            int(decision.allowed),
            decision.checked_at_utc.isoformat(),
            decision.policy_id,
            decision.execution_mode,
            decision.connection_target,
            _rejections_to_json(decision.rejections),
            decision.account_id,
            decision.position_count,
        )
        with _verified_txn(self._path) as connection:
            cursor = connection.execute(
                f"INSERT INTO {DECISIONS_TABLE} ({', '.join(_DECISION_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _DECISION_COLUMNS)})",
                record,
            )
            return int(cursor.lastrowid)

    def list_for_request(self, execution_request_id: str) -> tuple[SafetyDecision, ...]:
        """All decisions for one request, in recording order.

        Never creates a missing DB file (missing -> empty tuple, dry-run
        safe). Every row is strictly re-validated; an out-of-band edited DB
        raises :class:`SafetyIntegrityError` instead of being trusted.
        """
        return tuple(decision for _, decision in self.list_for_request_with_ids(execution_request_id))

    def list_for_request_with_ids(
        self, execution_request_id: str
    ) -> tuple[tuple[int, SafetyDecision], ...]:
        """Like :meth:`list_for_request`, additionally exposing each row's
        ``decision_id`` (Issue #78 / PR #100 review: shadow results reference
        their decision row, and the report CLI needs the ids to find ORPHAN
        decisions -- audit rows left by a shadow completion that later failed
        and was retried). Purely additive read API."""
        if not isinstance(execution_request_id, str) or not execution_request_id.strip():
            raise SafetyStoreError(
                f"execution_request_id must be a non-empty string, got {execution_request_id!r}"
            )
        if not self._path.exists():
            return ()
        with _verified_read(self._path) as connection:
            rows = connection.execute(
                f"SELECT decision_id, {', '.join(_DECISION_COLUMNS)} FROM {DECISIONS_TABLE} "
                "WHERE execution_request_id = ? ORDER BY decision_id ASC",
                (execution_request_id,),
            ).fetchall()
        decisions = []
        for row in rows:
            try:
                if row["allowed"] not in (0, 1):
                    raise ValueError(f"allowed must be 0 or 1, got {row['allowed']!r}")
                if row["position_count"] is not None and (
                    not isinstance(row["position_count"], int) or row["position_count"] < 0
                ):
                    raise ValueError(f"position_count is invalid: {row['position_count']!r}")
                decisions.append(
                    (
                        int(row["decision_id"]),
                        SafetyDecision(
                            execution_request_id=row["execution_request_id"],
                            allowed=bool(row["allowed"]),
                            checked_at_utc=_parse_utc(row["checked_at_utc"]),
                            policy_id=row["policy_id"],
                            rejections=_rejections_from_json(row["rejections_json"]),
                            execution_mode=row["execution_mode"],
                            connection_target=row["connection_target"],
                            account_id=row["account_id"],
                            position_count=row["position_count"],
                        ),
                    )
                )
            except (ValueError, TypeError) as exc:
                raise SafetyIntegrityError(
                    f"safety decision row for {execution_request_id!r} failed "
                    f"restore-time validation: {exc}"
                ) from exc
        return tuple(decisions)
