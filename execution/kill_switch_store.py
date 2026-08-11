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
through TRUE read-only connections (``execution/sqlite_ro.py``: a read
never creates the database file, and creates no ``-wal`` / ``-shm`` side
files for a non-WAL database -- see the scope note below) and re-validate
every stored row strictly -- an out-of-band edited DB raises
:class:`KillSwitchIntegrityError` instead of being trusted. Connections are
always explicitly closed (Windows file-handle hygiene).

A DB in WAL journal mode is refused on EVERY path (PR #126 Codex review
P1): a point-in-time read of a WAL file can miss a committed ``enable``
still sitting in the ``-wal`` side file and report a stale DISABLED --
exactly the "unreadable reads as OFF" failure Issue #81 forbids. Two checks
work together:

- :func:`_refuse_wal_header` runs BEFORE the file is opened and rejects a
  file that already declares WAL. It is not authoritative (a header probe
  cannot see a conversion racing the open) -- its job is to keep a read
  from RECREATING the ``-wal`` / ``-shm`` sidecars just to refuse the file
  (review r4).
- :func:`_refuse_wal` is authoritative: it asks the connection whose rows
  are about to be trusted, inside its transaction and while its lock is
  already held, so no window exists in which the file could be converted
  between checking and reading (reviews r2 / r3).

The store never creates WAL databases itself; an out-of-band conversion is
fail-closed until reverted (``PRAGMA journal_mode=DELETE``), and nothing
recorded is lost.

Scope of the no-side-file read property (review r4 r2): reads create no
``-wal`` / ``-shm`` for a NON-WAL database -- the normal case, and the only
one the store itself ever produces. If a database is converted to WAL out
of band exactly between the pre-open header probe and the open, the open
does materialize the sidecars before the refusal. That is accepted, not
overlooked: reading WAL content requires the WAL index, so the only
sidecar-free open is one that cannot see committed ``-wal`` rows -- exactly
the stale OFF Issue #81 forbids. Safety wins; the file-system property is
stated with its real boundary instead of being claimed unconditionally.

Every state read validates the COMPLETE history, not merely the newest row
(review r2): otherwise a malformed older row could hide behind a valid
newer ``disable`` and let the gate read OFF from a database the operator
CLI simultaneously reports as corrupt.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from execution.execution_store import _ddl_tokens  # shared DDL token normalizer (PR #91 r3)
from execution.sqlite_ro import _is_wal_header, connect_read_only

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


@dataclass(frozen=True)
class KillSwitchRecordOutcome:
    """What :meth:`SQLiteKillSwitchStore.initialize_and_record` returns (PR
    #126 Codex review P2, r6).

    ``transition`` is ALWAYS the durably committed change -- receiving this
    object at all means the write succeeded. ``state`` / ``history`` are the
    report read inside that same transaction, and carrying them here is the
    point: the caller never runs a second read that could fail on its own
    and leave it unable to describe a change that is already durable.

    ``state`` and ``history`` are ``None`` (with ``integrity_error`` set)
    for the ONE case that survives: the rows were read, but the store's
    history does not validate -- an out-of-band corrupted row. The
    transition still committed (an emergency ``enable`` must never be
    blocked by pre-existing damage), and the caller must report exactly
    that: recorded, but the state is unverifiable and therefore still
    fail-closed.
    """

    transition: KillSwitchTransition
    state: KillSwitchState | None
    history: tuple[KillSwitchTransition, ...] | None
    integrity_error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.transition, KillSwitchTransition):
            raise ValueError(f"transition must be a KillSwitchTransition, got {self.transition!r}")
        if (self.state is None) != (self.history is None):
            raise ValueError("state and history must be present or absent together")
        if (self.state is None) != (self.integrity_error is not None):
            raise ValueError(
                "integrity_error must be set exactly when the state could not be built"
            )
        if self.state is None:
            _require_non_empty_str(self.integrity_error, "integrity_error")
            return
        if not isinstance(self.state, KillSwitchState):
            raise ValueError(f"state must be a KillSwitchState, got {self.state!r}")
        if not isinstance(self.history, tuple):
            raise ValueError(f"history must be a tuple, got {type(self.history).__name__}")
        if not self.history:
            raise ValueError("history must contain at least the recorded transition")
        if self.state.transition != self.history[-1]:
            raise ValueError("state must be derived from the last history entry")
        if self.transition.transition_id != self.history[-1].transition_id:
            raise ValueError(
                "the recorded transition must be the newest entry of the returned history"
            )

    @property
    def state_is_verifiable(self) -> bool:
        """False when the transition committed but the store's history does
        not validate -- the caller must keep reporting fail-closed."""
        return self.state is not None


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


def _refuse_wal_header(db_path: Path) -> None:
    """EARLY, NON-AUTHORITATIVE WAL refusal, before the file is opened
    (PR #126 Codex review P2, r4).

    :func:`_refuse_wal` is the authoritative check, but it can only speak
    after a connection exists -- and opening an ALREADY-WAL database with
    ``allow_immutable_wal=False`` makes SQLite recreate the ``-wal`` /
    ``-shm`` sidecars (measured: the directory held only the DB before
    ``current_state()`` and both sidecars after). Read paths -- the operator
    ``status`` and every gate evaluation -- must not mutate the filesystem,
    least of all on a failure path, so a file whose header ALREADY declares
    WAL is refused here, before any open.

    This is deliberately not a replacement for the in-transaction check: a
    header probe is a separate observation and cannot see a conversion that
    races the open (review r1/r3). It only removes the side effect for the
    case that is decidable without opening anything; correctness still
    rests on :func:`_refuse_wal` under a held lock.

    RESIDUAL, and deliberately not closed (review r4 r2): a conversion that
    lands BETWEEN this probe and the open still materializes the sidecars,
    because the two properties cannot be had at once. Measured on one
    WAL-header file:

    ==========================  ========================  ==============
    open                        sees WAL rows / reports   sidecars
    ==========================  ========================  ==============
    ``mode=ro``                 yes / ``'wal'``           CREATED
    ``mode=ro&immutable=1``     stale / ``'delete'``      none
    ==========================  ========================  ==============

    Reading WAL content requires the WAL index (``-shm``), so the only
    sidecar-free open is the one that cannot see committed ``-wal`` rows and
    cannot even report that the file is WAL -- i.e. the one that can answer
    a stale DISABLED. Between "never touch the filesystem" and "never read a
    stale OFF", Issue #81 makes the second non-negotiable. So in that narrow
    race the sidecars appear and the store still fails closed; the no-side-
    file property is documented as holding for non-WAL databases rather than
    silently assumed (docs/architecture/safety_gate.md).
    """
    if _is_wal_header(db_path):
        raise KillSwitchSchemaError(
            f"kill switch DB at {db_path} is in WAL journal mode, which the store "
            "refuses: a read could then answer from a snapshot that predates a "
            "committed enable. Convert it back with 'PRAGMA journal_mode=DELETE' "
            "(fail-closed until then; no recorded state or history is lost)"
        )


def _lock_snapshot(connection: sqlite3.Connection) -> None:
    """Turn a just-opened DEFERRED transaction into a REAL read transaction
    (PR #126 Codex review P1, r3).

    ``BEGIN`` alone acquires nothing, and ``PRAGMA journal_mode`` does not
    take or keep a read lock either -- so a journal-mode answer obtained
    before any table read describes a file that another process may still
    convert. (Measured: with only ``BEGIN`` + the pragma held, a concurrent
    ``PRAGMA journal_mode=WAL`` succeeds AND the reader goes on to see rows
    committed after it.) One real page read takes the SHARED lock the rest
    of the transaction rides on; while it is held, a concurrent conversion
    fails with ``database is locked``.

    Reading ``sqlite_master`` is deliberate: it is a real database page, it
    is what the schema verification reads anyway, and it is permitted by the
    read-only authorizer.
    """
    connection.execute("SELECT count(*) FROM sqlite_master").fetchone()


def _refuse_wal(connection: sqlite3.Connection, db_path: Path) -> None:
    """Refuse a kill switch DB that is in WAL journal mode (PR #126 Codex
    review P1, r2/r3).

    Every execution-owned store uses the default rollback journal; a WAL
    file can only come from out-of-band maintenance (``PRAGMA
    journal_mode=WAL``). WAL is refused rather than supported because the
    read path must never be able to answer from a snapshot that predates a
    committed ``enable``.

    Two properties make the answer trustworthy, and BOTH are required:

    1. The check runs INSIDE the caller's transaction, on the very
       connection whose rows will be trusted -- not as a separate
       file-header probe. A header probe is two observations of a file that
       another process can convert between them (the r1 residual TOCTOU):
       the switch to WAL could land after the probe and before the open, and
       ``connect_read_only`` would then add ``immutable=1`` and silently
       ignore the ``-wal`` rows. It is also why the read path opens with
       ``allow_immutable_wal=False`` -- an ``immutable=1`` connection
       reports ``journal_mode = 'delete'`` for a WAL file, so it could
       neither be trusted nor even detected here.
    2. The transaction already HOLDS its read (or write) lock when the
       question is asked -- :func:`_lock_snapshot` for readers, ``BEGIN
       IMMEDIATE`` for writers (the r3 residual TOCTOU: inside a bare
       ``BEGIN`` the pragma locks nothing, so the mode could still flip
       before the first trusted read).
    """
    mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() == "wal":
        raise KillSwitchSchemaError(
            f"kill switch DB at {db_path} is in WAL journal mode, which the store "
            "refuses: a read could then answer from a snapshot that predates a "
            "committed enable. Convert it back with 'PRAGMA journal_mode=DELETE' "
            "(fail-closed until then; no recorded state or history is lost)"
        )


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}")
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def _verified_txn(db_path: Path):
    """Write flow: refuse an already-WAL file -> open -> BEGIN IMMEDIATE ->
    refuse WAL (authoritative, under the write lock) -> verify schema -> row
    work -> COMMIT (ROLLBACK on any error). Connection always explicitly
    closed."""
    _refuse_wal_header(db_path)
    with closing(_connect(db_path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            _refuse_wal(connection, db_path)
            _verify_schema(connection, db_path)
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise


@contextmanager
def _verified_read(db_path: Path):
    """Read flow: open a TRUE read-only connection (``mode=ro`` + query_only
    + write-denying authorizer -- a read can never create the file) -> BEGIN
    -> take the read lock -> refuse WAL and verify the schema INSIDE that
    locked transaction -> read. Always rolled back.

    ``allow_immutable_wal=False`` (PR #126 Codex review P1, r2): this store
    must never answer from a point-in-time view that ignores committed
    ``-wal`` rows, so the ``immutable=1`` fallback is disabled here. A WAL
    file is then opened as an ordinary read-only connection, which both
    SEES the WAL content and REPORTS ``journal_mode = 'wal'`` -- so
    :func:`_refuse_wal`, running on this same connection and snapshot, fails
    closed with no window in which the file could change identity between
    the check and the read.
    """
    # Refuse an already-WAL file BEFORE opening it (review r4): opening one
    # would recreate its -wal / -shm sidecars, and a read must not touch the
    # filesystem. Not authoritative on its own -- see _refuse_wal_header.
    _refuse_wal_header(db_path)
    with closing(connect_read_only(db_path, allow_immutable_wal=False)) as connection:
        connection.execute("BEGIN")
        try:
            # Order matters (review r3): take the SHARED lock FIRST, then
            # ask about the journal mode. A pragma asked inside a bare
            # BEGIN locks nothing, so its answer would not bind the reads
            # that follow it.
            _lock_snapshot(connection)
            _refuse_wal(connection, db_path)
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
        self._initialize_txn(row_work=None)

    def initialize_and_record(
        self, *, enabled: bool, operator: str, reason: str, changed_at_utc: datetime
    ) -> KillSwitchRecordOutcome:
        """Create-or-verify the schema, append the transition AND read back
        the resulting state + history -- all in ONE transaction (PR #126
        Codex review P1 / P2 r6).

        The CLI's emergency path previously ran ``initialize()`` and
        ``record_transition()`` as two separate transactions; a process kill
        (or a full disk) between them would leave a COMMITTED empty store --
        which reads as DISABLED -- turning the prior fail-closed missing-DB
        state into fail-open without any audit row. Here the schema creation
        (pristine DB only, same rule as ``initialize``) and the first row
        share one ``BEGIN IMMEDIATE`` transaction: any failure rolls both
        back, so the file stays missing / schema-less (= unverifiable =
        fail-closed), never "initialized but empty". On an EXISTING store
        this degrades to verify + append, exactly like
        :meth:`record_transition`.

        The state and history are returned FROM THAT SAME TRANSACTION
        (review r6) rather than left to a second, independent read. A
        separate read can fail on its own -- another process holding an
        exclusive lock, an out-of-band WAL conversion -- and the caller
        would then have a DURABLY COMMITTED transition it cannot describe.
        For ``disable`` that mismatch is dangerous in the wrong direction:
        submissions resume while the operator is told the store is
        fail-closed. Returning the report with the mutation removes that
        window instead of asking the CLI to reason about it.
        """
        if not isinstance(enabled, bool):
            raise KillSwitchStoreError(f"enabled must be a bool, got {enabled!r}")
        try:
            _require_non_empty_str(operator, "operator")
            _require_non_empty_str(reason, "reason")
            _require_utc(changed_at_utc, "changed_at_utc")
        except ValueError as exc:
            raise KillSwitchStoreError(str(exc)) from exc
        record = (int(enabled), changed_at_utc.isoformat(), operator, reason)

        def append_and_read(connection: sqlite3.Connection):
            cursor = connection.execute(
                f"INSERT INTO {TRANSITIONS_TABLE} ({', '.join(_TRANSITION_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _TRANSITION_COLUMNS)})",
                record,
            )
            rows = connection.execute(
                f"SELECT transition_id, {', '.join(_TRANSITION_COLUMNS)} "
                f"FROM {TRANSITIONS_TABLE} ORDER BY transition_id ASC"
            ).fetchall()
            return int(cursor.lastrowid), rows

        transition_id, rows = self._initialize_txn(row_work=append_and_read)
        # ---- past this point the transition is COMMITTED and durable ----
        recorded = KillSwitchTransition(
            transition_id=transition_id,
            enabled=enabled,
            changed_at_utc=changed_at_utc,
            operator=operator,
            reason=reason,
        )
        try:
            history = tuple(self._validated(row) for row in rows)
        except KillSwitchIntegrityError as exc:
            # Pre-existing out-of-band damage elsewhere in the history. The
            # write is deliberately NOT rolled back: an emergency enable must
            # never be blocked by a corrupted older row (and rolling back
            # would also discard an operator action that already succeeded).
            # The state stays unverifiable, so the caller reports fail-closed
            # while making clear the transition WAS recorded.
            return KillSwitchRecordOutcome(
                transition=recorded, state=None, history=None, integrity_error=str(exc)
            )
        latest = history[-1]
        return KillSwitchRecordOutcome(
            transition=recorded,
            state=KillSwitchState(enabled=latest.enabled, transition=latest),
            history=history,
        )

    def _initialize_txn(self, *, row_work):
        """One ``BEGIN IMMEDIATE`` transaction: create the schema into a
        pristine DB (or fully verify an existing one), then run ``row_work``
        (if any) inside the SAME transaction. Any failure rolls everything
        back -- a fresh file is left schema-less (unverifiable, fail-closed),
        never committed empty."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        _refuse_wal_header(self._path)
        try:
            with closing(_connect(self._path)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _refuse_wal(connection, self._path)
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
                    result = None if row_work is None else row_work(connection)
                    connection.execute("COMMIT")
                    return result
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
        out-of-band-edited DB -- never "disabled by default".

        The WHOLE history is validated before a state is returned (PR #126
        Codex review P1, r2), not just the latest row. A ``LIMIT 1`` read
        would let a malformed OLDER row stay invisible: appending a valid
        ``disable`` on top of it (writes verify the schema, not past rows)
        would make the gate read OFF and allow submissions, while
        ``snapshot()`` / the CLI rejected the very same DB as corrupt --
        two answers to one question, with the permissive one enforcing.
        ``docs/architecture/safety_gate.md`` requires corruption and
        out-of-band edits to stay errors, so every state read carries the
        same integrity commitment.
        """
        return self.snapshot()[0]

    def list_transitions(self) -> tuple[KillSwitchTransition, ...]:
        """The full change history in recording order (Issue #81: enable /
        disable, time, operator and reason are all auditable). Every row is
        strictly re-validated. Raises on any failure, like every read."""
        return self.snapshot()[1]

    def snapshot(self) -> tuple[KillSwitchState, tuple[KillSwitchTransition, ...]]:
        """Current state AND full history from ONE verified read transaction
        (PR #126 Codex review P2): the state is derived from the last row of
        the SAME result set the history comes from, so a concurrent writer
        can never make a status report say "disabled" while its own history
        listing already shows a newer enable. Strict validation and
        fail-closed behaviour are identical to the other reads.

        This is also the single implementation behind ``current_state()``
        and ``list_transitions()`` (review r2): one read shape means one
        integrity commitment, so the gate and the operator CLI can never
        disagree about whether the store is trustworthy."""
        rows = self._read_rows(
            f"SELECT transition_id, {', '.join(_TRANSITION_COLUMNS)} FROM {TRANSITIONS_TABLE} "
            "ORDER BY transition_id ASC"
        )
        transitions = tuple(self._validated(row) for row in rows)
        if not transitions:
            return KillSwitchState(enabled=False, transition=None), ()
        latest = transitions[-1]
        return KillSwitchState(enabled=latest.enabled, transition=latest), transitions

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
