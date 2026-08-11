"""Durable, append-only Kill Switch state for the Execution layer (Issue #81).

*** STATE STORE ONLY. No broker calls, no network I/O, no order path. This
store persists whether NEW submissions are administratively halted; the
Safety Gate (``execution/safety_gate.py``) is what enforces it, through the
UNCHANGED ``SafetyStateProvider`` contract and the EXISTING rejection codes
(``KILL_SWITCH_ENABLED`` / ``STATE_QUERY_FAILED``). ***

One DB file (``execution/data/kill_switch.sqlite3`` by default,
``KILL_SWITCH_DB_PATH`` to override), owned by ``execution/`` alone. It is
deliberately a SEPARATE file from every other execution store: each store
verifies its DB as a closed world (any foreign table fails verification),
so schemas cannot share a file -- and the kill switch must stay readable
even when the order DB / audit DB are damaged.

State model (append-only, Issue #81: history must never be lost to an
overwrite):

- The ONLY table is ``kill_switch_transitions``: one row per explicit
  operator action, recording ``enabled`` (the state AFTER the action),
  ``changed_at_utc``, ``operator`` and ``reason``. Rows are never updated
  or deleted -- schema triggers ABORT both -- so the full change history
  is the state's own storage.
- The CURRENT state is the row with the highest ``transition_id`` (row ids
  are the ordering authority; timestamps are auxiliary, per the repository
  contract). An INITIALIZED store with zero rows reads as DISABLED: that
  is a deliberately created state ("the switch exists and was never
  engaged"), not a read failure.
- A MISSING or unreadable DB is NEVER "disabled": every read raises a
  :class:`KillSwitchStoreError` subclass. Composed into the gate through
  :class:`PersistentKillSwitchStateProvider`, that exception becomes the
  existing fail-closed ``STATE_QUERY_FAILED`` rejection -- an unverifiable
  kill switch blocks every new submission (Issue #81: unreadable must not
  read as OFF).

Multi-process / restart stance: nothing is cached in process memory. Every
``is_kill_switch_enabled()`` call re-reads the committed DB state through a
fresh connection, so an ``enable`` from another process (the emergency CLI
on the same host) is honored by the very next gate evaluation, and the
state survives process restarts by construction. Writes serialize on
``BEGIN IMMEDIATE`` (same stance as every execution store), so concurrent
CLI invocations append distinct rows instead of corrupting each other.

Schema policy (same stance as ``execution_store`` / ``safety_store``):
``initialize()`` creates the schema only into a PRISTINE database (zero
``sqlite_master`` objects and ``user_version = 0``); anything else must
pass full verification or fails with :class:`KillSwitchSchemaError` WITHOUT
modifying the file. Every read and write verifies the schema first -- the
closed-world comparison of the complete ``sqlite_master`` object set
(token-normalized DDL; the append-only triggers are part of the verified
set), the PRAGMA structural signature and the schema version. Reads go
through TRUE read-only connections (``execution/sqlite_ro.py``: a read can
never create the file or side files) and re-validate every stored row
strictly -- an out-of-band edited DB raises
:class:`KillSwitchIntegrityError` instead of being trusted. Connections are
always explicitly closed (Windows file-handle hygiene).
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from execution.execution_store import _ddl_tokens  # shared DDL token normalizer (PR #91 r3)
from execution.sqlite_ro import connect_read_only

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "kill_switch.sqlite3"
BUSY_TIMEOUT_SECONDS = 30

TRANSITIONS_TABLE = "kill_switch_transitions"
SCHEMA_VERSION = 1

_TRANSITION_COLUMNS: tuple[str, ...] = (
    "enabled",
    "changed_at_utc",
    "operator",
    "reason",
)


class KillSwitchStoreError(RuntimeError):
    """Base class for kill switch store errors (every one is fail-closed:
    the gate converts it into ``STATE_QUERY_FAILED``, never into OFF)."""


class KillSwitchSchemaError(KillSwitchStoreError):
    """The on-disk schema does not match the expected Issue #81 schema."""


class KillSwitchIntegrityError(KillSwitchStoreError):
    """Stored data fails restore-time validation (out-of-band corruption)."""


def kill_switch_db_path() -> Path:
    raw = (os.getenv("KILL_SWITCH_DB_PATH") or str(DEFAULT_DB_PATH)).strip()
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


def _parse_utc(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError(f"stored timestamp is not UTC: {text!r}")
    return value


def _require_utc(value, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got {value!r}")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field_name} must be UTC (utcoffset 0), got {value.tzinfo}")


def _require_non_empty_str(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


@dataclass(frozen=True)
class KillSwitchTransition:
    """One recorded state change: the state AFTER the action plus the full
    operator attribution (Issue #81: who / when / why must be auditable)."""

    transition_id: int
    enabled: bool
    changed_at_utc: datetime
    operator: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.transition_id, int) or isinstance(self.transition_id, bool):
            raise ValueError(f"transition_id must be an int, got {self.transition_id!r}")
        if self.transition_id < 1:
            raise ValueError(f"transition_id must be >= 1, got {self.transition_id!r}")
        if not isinstance(self.enabled, bool):
            raise ValueError(f"enabled must be a bool, got {self.enabled!r}")
        _require_utc(self.changed_at_utc, "changed_at_utc")
        _require_non_empty_str(self.operator, "operator")
        _require_non_empty_str(self.reason, "reason")


@dataclass(frozen=True)
class KillSwitchState:
    """The current state as read from the store. ``transition`` is the row
    the state comes from, or ``None`` for an initialized store that never
    recorded a transition (which is always DISABLED)."""

    enabled: bool
    transition: KillSwitchTransition | None

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError(f"enabled must be a bool, got {self.enabled!r}")
        if self.transition is None:
            if self.enabled:
                raise ValueError("a state without any recorded transition must be disabled")
        elif not isinstance(self.transition, KillSwitchTransition):
            raise ValueError(
                f"transition must be a KillSwitchTransition or None, got {self.transition!r}"
            )
        elif self.transition.enabled is not self.enabled:
            raise ValueError(
                "state.enabled must equal the latest transition's enabled value"
            )


def _create_transitions_table_sql() -> str:
    """Append-only history table. The CHECKs mirror the
    :class:`KillSwitchTransition` invariants as a second guard."""
    return f"""
        CREATE TABLE {TRANSITIONS_TABLE} (
            transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            changed_at_utc TEXT NOT NULL CHECK (changed_at_utc LIKE '%+00:00'),
            operator TEXT NOT NULL CHECK (trim(operator) <> ''),
            reason TEXT NOT NULL CHECK (trim(reason) <> '')
        )
    """


# Append-only enforcement inside the schema itself (Issue #81: the change
# history must never be lost to an overwrite). The triggers are part of the
# closed-world verified object set, so a DB with them missing -- or altered
# -- fails verification and is never trusted.
def _create_no_update_trigger_sql() -> str:
    return f"""
        CREATE TRIGGER trg_{TRANSITIONS_TABLE}_no_update
        BEFORE UPDATE ON {TRANSITIONS_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'kill switch history is append-only: UPDATE is forbidden');
        END
    """


def _create_no_delete_trigger_sql() -> str:
    return f"""
        CREATE TRIGGER trg_{TRANSITIONS_TABLE}_no_delete
        BEFORE DELETE ON {TRANSITIONS_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'kill switch history is append-only: DELETE is forbidden');
        END
    """


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_create_transitions_table_sql())
    connection.execute(_create_no_update_trigger_sql())
    connection.execute(_create_no_delete_trigger_sql())
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _structure_signature(connection: sqlite3.Connection) -> dict:
    signature: dict = {
        f"table_info:{TRANSITIONS_TABLE}": [
            tuple(row) for row in connection.execute(f"PRAGMA table_info({TRANSITIONS_TABLE})")
        ],
        f"foreign_keys:{TRANSITIONS_TABLE}": [
            tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({TRANSITIONS_TABLE})")
        ],
    }
    index_entries = []
    for index_row in connection.execute(f"PRAGMA index_list({TRANSITIONS_TABLE})"):
        columns = [
            tuple(info) for info in connection.execute(f"PRAGMA index_info({index_row[1]})")
        ]
        index_entries.append((index_row[1], index_row[2], index_row[3], index_row[4], columns))
    signature[f"indexes:{TRANSITIONS_TABLE}"] = sorted(index_entries)
    return signature


_REFERENCE_SCHEMA_CACHE: tuple[dict, dict] | None = None


def _reference_schema() -> tuple[dict, dict]:
    """Pristine reference schema built once in memory: the complete
    ``sqlite_master`` object map (``sqlite_sequence`` from AUTOINCREMENT and
    the append-only triggers included) plus the PRAGMA structural
    signature."""
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
    """Closed-world verification (same stance as ``execution_store`` /
    ``safety_store``): the COMPLETE ``sqlite_master`` object set -- the
    append-only triggers included -- must exactly equal the pristine
    reference set, the token-normalized DDL of every SQL-bearing object must
    match, the PRAGMA structural signature must match, and the schema
    version must match. Read-only."""
    problems: list[str] = []
    stored_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if stored_version != SCHEMA_VERSION:
        problems.append(f"user_version is {stored_version}, expected {SCHEMA_VERSION}")

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
        raise KillSwitchSchemaError(
            f"kill switch DB at {db_path} failed schema verification; "
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
    """Read flow: open a TRUE read-only connection (``mode=ro`` + query_only
    + write-denying authorizer -- a read can never create the file or side
    files) -> BEGIN (consistent view) -> verify schema -> read. Always
    rolled back."""
    with closing(connect_read_only(db_path)) as connection:
        connection.execute("BEGIN")
        try:
            _verify_schema(connection, db_path)
            yield connection
        finally:
            connection.execute("ROLLBACK")


def _transition_from_row(row) -> KillSwitchTransition:
    """Rebuild one transition STRICTLY; the caller converts ``ValueError``
    into :class:`KillSwitchIntegrityError` (out-of-band edits are rejected,
    never trusted)."""
    if row["enabled"] not in (0, 1):
        raise ValueError(f"enabled must be 0 or 1, got {row['enabled']!r}")
    return KillSwitchTransition(
        transition_id=int(row["transition_id"]),
        enabled=bool(row["enabled"]),
        changed_at_utc=_parse_utc(row["changed_at_utc"]),
        operator=row["operator"],
        reason=row["reason"],
    )


class SQLiteKillSwitchStore:
    """The DURABLE kill switch store (state = its own append-only history).

    Every method raises a :class:`KillSwitchStoreError` subclass on any
    failure. A MISSING DB file is a failure on every path -- including
    reads: an uninitialized kill switch is an unverifiable one, and the
    Issue #81 contract forbids reading "unverifiable" as "disabled".
    """

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path is not None else kill_switch_db_path()

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
        try:
            with closing(_connect(self._path)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    objects = connection.execute("SELECT name FROM sqlite_master").fetchall()
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                    if not objects:
                        if version != 0:
                            raise KillSwitchSchemaError(
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
        except KillSwitchStoreError:
            raise
        except sqlite3.Error as exc:
            # e.g. the path holds a non-SQLite file: surface it as the same
            # fail-closed store error family every caller already handles.
            raise KillSwitchStoreError(
                f"kill switch DB at {self._path} could not be initialized: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def record_transition(
        self, *, enabled: bool, operator: str, reason: str, changed_at_utc: datetime
    ) -> KillSwitchTransition:
        """Append one explicit state change and return the recorded row.

        Appending is deliberately unconditional (no "already in that state"
        suppression): every explicit operator action is part of the audit
        history, and an emergency ``enable`` must never be refused because
        someone else enabled first. Raises on ANY failure (fail closed); a
        missing DB file is a failure, not a trigger for implicit creation --
        the store must be initialized deliberately (the CLI does).
        """
        if not isinstance(enabled, bool):
            raise KillSwitchStoreError(f"enabled must be a bool, got {enabled!r}")
        try:
            _require_non_empty_str(operator, "operator")
            _require_non_empty_str(reason, "reason")
            _require_utc(changed_at_utc, "changed_at_utc")
        except ValueError as exc:
            raise KillSwitchStoreError(str(exc)) from exc
        if not self._path.exists():
            raise KillSwitchStoreError(
                f"kill switch DB at {self._path} does not exist; call initialize() first"
            )
        record = (int(enabled), changed_at_utc.isoformat(), operator, reason)
        try:
            with _verified_txn(self._path) as connection:
                cursor = connection.execute(
                    f"INSERT INTO {TRANSITIONS_TABLE} ({', '.join(_TRANSITION_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _TRANSITION_COLUMNS)})",
                    record,
                )
                transition_id = int(cursor.lastrowid)
        except KillSwitchStoreError:
            raise
        except sqlite3.Error as exc:
            raise KillSwitchStoreError(
                f"kill switch DB at {self._path} could not record the transition: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return KillSwitchTransition(
            transition_id=transition_id,
            enabled=enabled,
            changed_at_utc=changed_at_utc,
            operator=operator,
            reason=reason,
        )

    def current_state(self) -> KillSwitchState:
        """The current state = the highest-id transition (ids are the
        ordering authority; timestamps are auxiliary). An initialized store
        with zero rows is DISABLED. Raises on a missing, foreign, corrupt or
        out-of-band-edited DB -- never "disabled by default"."""
        rows = self._read_rows(
            f"SELECT transition_id, {', '.join(_TRANSITION_COLUMNS)} FROM {TRANSITIONS_TABLE} "
            "ORDER BY transition_id DESC LIMIT 1"
        )
        if not rows:
            return KillSwitchState(enabled=False, transition=None)
        transition = self._validated(rows[0])
        return KillSwitchState(enabled=transition.enabled, transition=transition)

    def list_transitions(self) -> tuple[KillSwitchTransition, ...]:
        """The full change history in recording order (Issue #81: enable /
        disable, time, operator and reason are all auditable). Every row is
        strictly re-validated. Raises on any failure, like every read."""
        rows = self._read_rows(
            f"SELECT transition_id, {', '.join(_TRANSITION_COLUMNS)} FROM {TRANSITIONS_TABLE} "
            "ORDER BY transition_id ASC"
        )
        return tuple(self._validated(row) for row in rows)

    def _read_rows(self, query: str) -> list:
        if not self._path.exists():
            raise KillSwitchStoreError(
                f"kill switch DB at {self._path} does not exist; the kill switch state "
                "is unverifiable (fail closed) -- initialize the store deliberately"
            )
        try:
            with _verified_read(self._path) as connection:
                return connection.execute(query).fetchall()
        except KillSwitchStoreError:
            raise
        except OSError as exc:
            # connect_read_only raises ReadOnlyDbMissingError (an OSError)
            # when the file vanished between the exists() probe and the open.
            raise KillSwitchStoreError(
                f"kill switch DB at {self._path} could not be opened read-only: {exc}"
            ) from exc
        except sqlite3.Error as exc:
            raise KillSwitchStoreError(
                f"kill switch DB at {self._path} could not be read: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def _validated(self, row) -> KillSwitchTransition:
        try:
            return _transition_from_row(row)
        except (ValueError, TypeError) as exc:
            raise KillSwitchIntegrityError(
                f"kill switch transition row failed restore-time validation: {exc}"
            ) from exc


class PersistentKillSwitchStateProvider:
    """The DURABLE ``SafetyStateProvider`` (Issue #81): the production
    kill-switch source for every Safety Gate composition.

    Satisfies the existing ``execution.safety_gate.SafetyStateProvider``
    protocol structurally -- the gate, its ``KILL_SWITCH_ENABLED`` /
    ``STATE_QUERY_FAILED`` contract and the single sanctioned submission
    path (``LiveOrderSubmissionService``) are all UNCHANGED. Every call
    re-reads the committed SQLite state through a fresh connection: nothing
    is cached, so the answer survives process restarts, is shared across
    processes on the host, and honors another process's ``enable``
    immediately. Any store failure (missing / foreign / corrupt DB)
    propagates as an exception, which the gate already converts into the
    fail-closed ``STATE_QUERY_FAILED`` rejection -- an unverifiable kill
    switch never reads as OFF.

    ``StaticSafetyState`` remains the in-memory provider for tests, fixed
    scenarios and shadow-cycle fixtures; this provider is the one a live
    submission composition must wire (docs/architecture/safety_gate.md).
    """

    def __init__(self, db_path: str | Path | None = None):
        self._store = SQLiteKillSwitchStore(db_path)

    @property
    def db_path(self) -> Path:
        return self._store.path

    def is_kill_switch_enabled(self) -> bool:
        return self._store.current_state().enabled
