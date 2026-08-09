"""Durable Submission Journal for the sanctioned live submission path
(Issue #93).

*** JOURNAL ONLY. No broker calls, no network I/O. This store records the
around-the-broker-call lifecycle of every submission attempt; it never
submits anything. ***

Why this exists (Issue #93): ``LiveOrderSubmissionService`` records broker
outcomes into the Execution Order DB (Issue #74) and falls back to a
state-only UNKNOWN when that persistence fails (Issue #75 review r3). But if
the Execution Order DB ITSELF is unavailable -- schema mismatch, read-only
filesystem, corruption, file loss -- the result save, the UNKNOWN fallback
AND ``list_unresolved_submissions()`` all fail together, and an order the
broker may hold becomes untrackable. This journal is the INDEPENDENT durable
medium: one SQLite file of its own
(``execution/data/submission_journal.sqlite3`` by default,
``SUBMISSION_JOURNAL_DB_PATH`` to override), owned by ``execution/`` alone
and deliberately SEPARATE from every other store file (the closed-world
schema verification of each store refuses cohabiting tables).

Journal lifecycle (one row per submission attempt, keyed by the
``execution_request_id``; ``client_order_id`` is UNIQUE table-wide):

- ``broker_call_state``: ``not_started`` -> ``started`` -> ``responded`` |
  ``unknown``. ``started`` is written IMMEDIATELY before
  ``Broker.submit_order``; ``not_started`` therefore PROVES the broker was
  never called for this intent, while ``started`` means the call may have
  happened (conservative).
- ``execution_db_save_state``: ``not_attempted`` -> ``saved`` | ``failed``
  (the Execution Order DB persistence of the received ``OrderResult``).
- ``outcome`` (machine-readable summary): ``open`` -> ``completed``
  (result durably saved to the Execution DB) or ``reconciliation_required``
  (broker outcome unknown, or the broker answered but the Execution DB save
  failed). ``reconciliation_required`` and stuck-``open`` rows are what
  :meth:`SubmissionJournal.list_unresolved` hands to future reconciliation.

Duplicate-send prevention (second, independent guard under Issue #74/#76):
:meth:`record_submission_intent` inserts the row BEFORE the broker call and
refuses -- ``SubmissionJournalConflictError`` -- whenever an entry for the
same ``execution_request_id`` already progressed past ``not_started``
(a broker call may have happened), whenever the payload differs, or whenever
the ``client_order_id`` is already journaled for a DIFFERENT request. Even
if the Execution Order DB is wiped and the same intent re-registered from
scratch, the journal still blocks a second broker call. A ``not_started``
entry with an IDENTICAL payload is reclaimed instead (the broker was
provably never called, so a retry after an infrastructure failure is not a
double-send); reclaiming refreshes the attempt timestamp only.

Fail-closed contract (Issue #93): every write raises on ANY failure --
missing/uninitialized DB, schema mismatch, precondition violation, insert
failure. The sanctioned submission path treats any journal failure BEFORE
the broker call as a rejection (``submission_journal_failed`` /
``duplicate_submission``) and does not submit. AFTER the broker call,
journal failures are logged critically but never block recording the broker
outcome elsewhere and are never converted into rejections (the order may
exist). UNKNOWN / unresolved entries are NEVER auto-resubmitted; they are
query targets for reconciliation.

Schema policy (same stance as ``execution_store`` / ``safety_store``):
``initialize()`` creates the schema only into a PRISTINE database (zero
``sqlite_master`` objects and ``user_version`` 0); anything else must pass
full verification or fails with :class:`SubmissionJournalSchemaError`
WITHOUT modifying the file. Every read and write verifies the schema first
(closed-world ``sqlite_master`` object set, token-normalized DDL, PRAGMA
structural signature, schema version). Writes additionally re-read the
mutated row INSIDE the same transaction and re-validate the full entry
invariants before COMMIT. Reads use a true read-only connection
(``execution/sqlite_ro.py``), never create a missing DB file, and strictly
re-validate every row (enums, UTC, quantity, cross-field state invariants)
-- an out-of-band edited DB raises
:class:`SubmissionJournalIntegrityError` instead of being trusted.
Connections are always explicitly closed (Windows file-handle hygiene).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from broker.broker_models import OrderSide, OrderState
from execution.execution_store import _ddl_tokens  # shared DDL token normalizer (PR #91 r3)
from execution.execution_models import _require_non_empty_str, _require_utc
from execution.sqlite_ro import connect_read_only

_LOGGER = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "submission_journal.sqlite3"
BUSY_TIMEOUT_SECONDS = 30

JOURNAL_TABLE = "submission_journal"
SCHEMA_VERSION = 1

# broker_call_state values (see the module docstring for the semantics).
BROKER_CALL_NOT_STARTED = "not_started"
BROKER_CALL_STARTED = "started"
BROKER_CALL_RESPONDED = "responded"
BROKER_CALL_UNKNOWN = "unknown"
BROKER_CALL_STATES: frozenset[str] = frozenset(
    {BROKER_CALL_NOT_STARTED, BROKER_CALL_STARTED, BROKER_CALL_RESPONDED, BROKER_CALL_UNKNOWN}
)

# execution_db_save_state values.
DB_SAVE_NOT_ATTEMPTED = "not_attempted"
DB_SAVE_SAVED = "saved"
DB_SAVE_FAILED = "failed"
DB_SAVE_STATES: frozenset[str] = frozenset({DB_SAVE_NOT_ATTEMPTED, DB_SAVE_SAVED, DB_SAVE_FAILED})

# outcome values (the machine-readable journal/Execution-DB divergence flag).
OUTCOME_OPEN = "open"
OUTCOME_COMPLETED = "completed"
OUTCOME_RECONCILIATION_REQUIRED = "reconciliation_required"
OUTCOMES: frozenset[str] = frozenset(
    {OUTCOME_OPEN, OUTCOME_COMPLETED, OUTCOME_RECONCILIATION_REQUIRED}
)

_ENTRY_COLUMNS: tuple[str, ...] = (
    "execution_request_id",
    "client_order_id",
    "symbol",
    "side",
    "quantity",
    "execution_mode",
    "connection_target",
    "submission_started_at_utc",
    "broker_call_state",
    "broker_response_state",
    "broker_order_id",
    "execution_db_save_state",
    "outcome",
    "last_error",
    "updated_at_utc",
)


class SubmissionJournalError(RuntimeError):
    """Base class for submission journal errors."""


class SubmissionJournalSchemaError(SubmissionJournalError):
    """The on-disk schema does not match the expected Issue #93 schema."""


class SubmissionJournalIntegrityError(SubmissionJournalError):
    """Stored data fails restore-time validation (out-of-band corruption)."""


class SubmissionJournalConflictError(SubmissionJournalError):
    """A submission intent collided with an existing journal entry: the same
    ``execution_request_id`` already progressed past ``not_started`` (a
    broker call may have happened -- re-sending is banned), the payload
    differs from the journaled one, or the ``client_order_id`` is already
    journaled for a different request. The sanctioned path converts this
    into a fail-closed ``duplicate_submission`` rejection."""


def journal_db_path() -> Path:
    raw = (os.getenv("SUBMISSION_JOURNAL_DB_PATH") or str(DEFAULT_DB_PATH)).strip()
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


def _parse_utc(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError(f"stored timestamp is not UTC: {text!r}")
    return value


def _enum_values_sql(values) -> str:
    return "', '".join(sorted(v.value for v in values))


def _create_journal_table_sql() -> str:
    """Full current schema. The CHECKs mirror the
    :class:`SubmissionJournalEntry` invariants as a second guard, foremost
    the lifecycle consistency: ``completed`` requires a saved Execution DB
    result, ``unknown`` / ``failed`` require ``reconciliation_required`` and
    a recorded failure reason, and nothing before ``responded`` may carry a
    broker response."""
    sides = _enum_values_sql(OrderSide)
    states = _enum_values_sql(OrderState)
    call_states = "', '".join(sorted(BROKER_CALL_STATES))
    save_states = "', '".join(sorted(DB_SAVE_STATES))
    outcomes = "', '".join(sorted(OUTCOMES))
    return f"""
        CREATE TABLE {JOURNAL_TABLE} (
            execution_request_id TEXT PRIMARY KEY NOT NULL CHECK (trim(execution_request_id) <> ''),
            client_order_id TEXT NOT NULL UNIQUE CHECK (trim(client_order_id) <> ''),
            symbol TEXT NOT NULL CHECK (trim(symbol) <> ''),
            side TEXT NOT NULL CHECK (side IN ('{sides}')),
            quantity TEXT NOT NULL CHECK (trim(quantity) <> ''),
            execution_mode TEXT NOT NULL CHECK (trim(execution_mode) <> ''),
            connection_target TEXT NOT NULL CHECK (trim(connection_target) <> ''),
            submission_started_at_utc TEXT NOT NULL CHECK (submission_started_at_utc LIKE '%+00:00'),
            broker_call_state TEXT NOT NULL CHECK (broker_call_state IN ('{call_states}')),
            broker_response_state TEXT CHECK (broker_response_state IS NULL OR broker_response_state IN ('{states}')),
            broker_order_id TEXT CHECK (broker_order_id IS NULL OR trim(broker_order_id) <> ''),
            execution_db_save_state TEXT NOT NULL CHECK (execution_db_save_state IN ('{save_states}')),
            outcome TEXT NOT NULL CHECK (outcome IN ('{outcomes}')),
            last_error TEXT CHECK (last_error IS NULL OR trim(last_error) <> ''),
            updated_at_utc TEXT NOT NULL CHECK (updated_at_utc LIKE '%+00:00'),
            CHECK ((broker_call_state = '{BROKER_CALL_RESPONDED}') = (broker_response_state IS NOT NULL)),
            CHECK (broker_order_id IS NULL OR broker_call_state = '{BROKER_CALL_RESPONDED}'),
            CHECK (execution_db_save_state = '{DB_SAVE_NOT_ATTEMPTED}' OR broker_call_state = '{BROKER_CALL_RESPONDED}'),
            CHECK ((outcome = '{OUTCOME_COMPLETED}') = (execution_db_save_state = '{DB_SAVE_SAVED}')),
            CHECK (broker_call_state NOT IN ('{BROKER_CALL_NOT_STARTED}', '{BROKER_CALL_STARTED}') OR outcome = '{OUTCOME_OPEN}'),
            CHECK (broker_call_state <> '{BROKER_CALL_UNKNOWN}' OR (outcome = '{OUTCOME_RECONCILIATION_REQUIRED}' AND last_error IS NOT NULL)),
            CHECK (execution_db_save_state <> '{DB_SAVE_FAILED}' OR (outcome = '{OUTCOME_RECONCILIATION_REQUIRED}' AND last_error IS NOT NULL))
        ) WITHOUT ROWID
    """


_JOURNAL_OUTCOME_INDEX = f"idx_{JOURNAL_TABLE}_outcome"


def _create_index_sql() -> str:
    return (
        f"CREATE INDEX {_JOURNAL_OUTCOME_INDEX} ON {JOURNAL_TABLE} "
        "(outcome, updated_at_utc)"
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_create_journal_table_sql())
    connection.execute(_create_index_sql())
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _structure_signature(connection: sqlite3.Connection) -> dict:
    signature: dict = {
        f"table_info:{JOURNAL_TABLE}": [
            tuple(row) for row in connection.execute(f"PRAGMA table_info({JOURNAL_TABLE})")
        ],
        f"foreign_keys:{JOURNAL_TABLE}": [
            tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({JOURNAL_TABLE})")
        ],
    }
    index_entries = []
    for index_row in connection.execute(f"PRAGMA index_list({JOURNAL_TABLE})"):
        columns = [
            tuple(info) for info in connection.execute(f"PRAGMA index_info({index_row[1]})")
        ]
        index_entries.append((index_row[1], index_row[2], index_row[3], index_row[4], columns))
    signature[f"indexes:{JOURNAL_TABLE}"] = sorted(index_entries)
    return signature


_REFERENCE_SCHEMA_CACHE: tuple[dict, dict] | None = None


def _reference_schema() -> tuple[dict, dict]:
    """Pristine reference schema built once in memory: the complete
    ``sqlite_master`` object map (``sqlite_autoindex_*`` from the UNIQUE
    constraint included) plus the PRAGMA structural signature."""
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
    """Closed-world verification (same stance as ``execution_store``): the
    COMPLETE ``sqlite_master`` object set -- triggers and views included --
    must exactly equal the pristine reference set, the token-normalized DDL
    of every SQL-bearing object must match, the PRAGMA structural signature
    must match, and the schema version must match. Read-only."""
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
        raise SubmissionJournalSchemaError(
            f"submission journal at {db_path} failed schema verification; "
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
    ``PRAGMA query_only``: a read can never create the file, journals or
    WAL/SHM side files, and any write attempt fails) -> BEGIN (consistent
    view) -> verify schema -> restore. Always rolled back."""
    with closing(connect_read_only(db_path)) as connection:
        connection.execute("BEGIN")
        try:
            _verify_schema(connection, db_path)
            yield connection
        finally:
            connection.execute("ROLLBACK")


@dataclass(frozen=True)
class SubmissionJournalEntry:
    """One journaled submission attempt, with the full lifecycle invariants
    enforced at construction (restore-time validation reuses this)."""

    execution_request_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    execution_mode: str
    connection_target: str
    submission_started_at_utc: datetime
    broker_call_state: str
    execution_db_save_state: str
    outcome: str
    updated_at_utc: datetime
    broker_response_state: OrderState | None = None
    broker_order_id: str | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.execution_request_id, "execution_request_id")
        _require_non_empty_str(self.client_order_id, "client_order_id")
        _require_non_empty_str(self.symbol, "symbol")
        if not isinstance(self.side, OrderSide):
            raise ValueError(f"side must be an OrderSide, got {self.side!r}")
        if not isinstance(self.quantity, Decimal) or not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError(f"quantity must be a positive finite Decimal, got {self.quantity!r}")
        _require_non_empty_str(self.execution_mode, "execution_mode")
        _require_non_empty_str(self.connection_target, "connection_target")
        _require_utc(self.submission_started_at_utc, "submission_started_at_utc")
        _require_utc(self.updated_at_utc, "updated_at_utc")
        if self.broker_call_state not in BROKER_CALL_STATES:
            raise ValueError(f"unknown broker_call_state {self.broker_call_state!r}")
        if self.execution_db_save_state not in DB_SAVE_STATES:
            raise ValueError(f"unknown execution_db_save_state {self.execution_db_save_state!r}")
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {self.outcome!r}")
        if self.broker_response_state is not None and not isinstance(
            self.broker_response_state, OrderState
        ):
            raise ValueError(
                f"broker_response_state must be an OrderState or None, got "
                f"{self.broker_response_state!r}"
            )
        if self.broker_order_id is not None:
            _require_non_empty_str(self.broker_order_id, "broker_order_id")
        if self.last_error is not None:
            _require_non_empty_str(self.last_error, "last_error")
        # Lifecycle cross-field invariants (mirrored by the DB CHECKs).
        responded = self.broker_call_state == BROKER_CALL_RESPONDED
        if responded != (self.broker_response_state is not None):
            raise ValueError(
                "broker_response_state must be present exactly when broker_call_state "
                f"is {BROKER_CALL_RESPONDED!r} (got state {self.broker_call_state!r}, "
                f"response {self.broker_response_state!r})"
            )
        if self.broker_order_id is not None and not responded:
            raise ValueError(
                f"broker_order_id requires broker_call_state {BROKER_CALL_RESPONDED!r}, "
                f"got {self.broker_call_state!r}"
            )
        if self.execution_db_save_state != DB_SAVE_NOT_ATTEMPTED and not responded:
            raise ValueError(
                "an Execution DB save can only be recorded for a responded broker call "
                f"(got call state {self.broker_call_state!r}, save state "
                f"{self.execution_db_save_state!r})"
            )
        if (self.outcome == OUTCOME_COMPLETED) != (self.execution_db_save_state == DB_SAVE_SAVED):
            raise ValueError(
                f"outcome {OUTCOME_COMPLETED!r} and execution_db_save_state "
                f"{DB_SAVE_SAVED!r} must coincide (got outcome {self.outcome!r}, save "
                f"state {self.execution_db_save_state!r})"
            )
        if (
            self.broker_call_state in (BROKER_CALL_NOT_STARTED, BROKER_CALL_STARTED)
            and self.outcome != OUTCOME_OPEN
        ):
            raise ValueError(
                f"broker_call_state {self.broker_call_state!r} requires outcome "
                f"{OUTCOME_OPEN!r}, got {self.outcome!r}"
            )
        if self.broker_call_state == BROKER_CALL_UNKNOWN and (
            self.outcome != OUTCOME_RECONCILIATION_REQUIRED or self.last_error is None
        ):
            raise ValueError(
                f"broker_call_state {BROKER_CALL_UNKNOWN!r} requires outcome "
                f"{OUTCOME_RECONCILIATION_REQUIRED!r} and a recorded last_error"
            )
        if self.execution_db_save_state == DB_SAVE_FAILED and (
            self.outcome != OUTCOME_RECONCILIATION_REQUIRED or self.last_error is None
        ):
            raise ValueError(
                f"execution_db_save_state {DB_SAVE_FAILED!r} requires outcome "
                f"{OUTCOME_RECONCILIATION_REQUIRED!r} and a recorded last_error"
            )

    def payload_identity(self) -> tuple:
        """The fields a reclaim must match EXACTLY (see
        :meth:`SubmissionJournal.record_submission_intent`)."""
        return (
            self.client_order_id,
            self.symbol,
            self.side,
            self.quantity,
            self.execution_mode,
            self.connection_target,
        )


def _entry_from_row(row, db_path: Path) -> SubmissionJournalEntry:
    try:
        quantity = Decimal(row["quantity"])
        if not quantity.is_finite():
            raise ValueError(f"stored quantity is not finite: {row['quantity']!r}")
        return SubmissionJournalEntry(
            execution_request_id=row["execution_request_id"],
            client_order_id=row["client_order_id"],
            symbol=row["symbol"],
            side=OrderSide(row["side"]),
            quantity=quantity,
            execution_mode=row["execution_mode"],
            connection_target=row["connection_target"],
            submission_started_at_utc=_parse_utc(row["submission_started_at_utc"]),
            broker_call_state=row["broker_call_state"],
            broker_response_state=(
                None
                if row["broker_response_state"] is None
                else OrderState(row["broker_response_state"])
            ),
            broker_order_id=row["broker_order_id"],
            execution_db_save_state=row["execution_db_save_state"],
            outcome=row["outcome"],
            last_error=row["last_error"],
            updated_at_utc=_parse_utc(row["updated_at_utc"]),
        )
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise SubmissionJournalIntegrityError(
            f"submission journal row in {db_path} failed restore-time validation: {exc}"
        ) from exc


class SubmissionJournal:
    """The durable Submission Journal store (see the module docstring).

    Every write raises a :class:`SubmissionJournalError` subclass on any
    failure; the sanctioned submission path converts pre-broker failures
    into fail-closed rejections and never calls the broker."""

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path is not None else journal_db_path()

    @property
    def path(self) -> Path:
        return self._path

    def initialize(self) -> None:
        """Create the schema (pristine DB only) or verify an existing one.

        Same pristine rule as ``execution_store.init_db``: creation happens
        only when ``sqlite_master`` is empty AND ``user_version`` is 0; any
        other content must pass full verification or fails WITHOUT
        modifying the file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(_connect(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                objects = connection.execute("SELECT name FROM sqlite_master").fetchall()
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if not objects:
                    if version != 0:
                        raise SubmissionJournalSchemaError(
                            f"DB at {self._path} is empty but carries user_version "
                            f"{version}; not a pristine DB -- refusing to initialize it"
                        )
                    _create_schema(connection)
                    _verify_schema(connection, self._path)
                else:
                    _verify_schema(connection, self._path)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    # --- writes (all fail closed) --------------------------------------------

    def record_submission_intent(
        self,
        *,
        execution_request_id: str,
        client_order_id: str,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        execution_mode: str,
        connection_target: str,
        started_at_utc: datetime,
    ) -> None:
        """Durably journal a submission attempt BEFORE any broker call.

        Inserts the ``not_started`` row, or RECLAIMS an existing
        ``not_started`` row whose payload is IDENTICAL (a prior attempt that
        provably never reached the broker -- reclaiming refreshes the
        attempt timestamp only). Everything else conflicts
        (:class:`SubmissionJournalConflictError`): an entry past
        ``not_started`` (a broker call may have happened; re-sending is
        banned), a payload mismatch, or a ``client_order_id`` journaled for
        a different request. A missing DB file is a failure, not a trigger
        for implicit creation."""
        _require_non_empty_str(execution_request_id, "execution_request_id")
        _require_non_empty_str(client_order_id, "client_order_id")
        _require_non_empty_str(symbol, "symbol")
        if not isinstance(side, OrderSide):
            raise SubmissionJournalError(f"side must be an OrderSide, got {side!r}")
        if not isinstance(quantity, Decimal) or not quantity.is_finite() or quantity <= 0:
            raise SubmissionJournalError(
                f"quantity must be a positive finite Decimal, got {quantity!r}"
            )
        _require_non_empty_str(execution_mode, "execution_mode")
        _require_non_empty_str(connection_target, "connection_target")
        _require_utc(started_at_utc, "started_at_utc")
        self._require_db_file()
        candidate = SubmissionJournalEntry(
            execution_request_id=execution_request_id,
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            execution_mode=execution_mode,
            connection_target=connection_target,
            submission_started_at_utc=started_at_utc,
            broker_call_state=BROKER_CALL_NOT_STARTED,
            execution_db_save_state=DB_SAVE_NOT_ATTEMPTED,
            outcome=OUTCOME_OPEN,
            updated_at_utc=started_at_utc,
        )
        with _verified_txn(self._path) as connection:
            existing_row = self._fetch_row(connection, execution_request_id)
            if existing_row is None:
                owner = connection.execute(
                    f"SELECT execution_request_id FROM {JOURNAL_TABLE} "
                    "WHERE client_order_id = ?",
                    (client_order_id,),
                ).fetchone()
                if owner is not None:
                    self._conflict(
                        execution_request_id,
                        f"client_order_id {client_order_id!r} is already journaled for "
                        f"execution_request_id {owner['execution_request_id']!r}",
                    )
                connection.execute(
                    f"INSERT INTO {JOURNAL_TABLE} ({', '.join(_ENTRY_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _ENTRY_COLUMNS)})",
                    (
                        candidate.execution_request_id,
                        candidate.client_order_id,
                        candidate.symbol,
                        candidate.side.value,
                        str(candidate.quantity),
                        candidate.execution_mode,
                        candidate.connection_target,
                        candidate.submission_started_at_utc.isoformat(),
                        candidate.broker_call_state,
                        None,
                        None,
                        candidate.execution_db_save_state,
                        candidate.outcome,
                        None,
                        candidate.updated_at_utc.isoformat(),
                    ),
                )
            else:
                existing = _entry_from_row(existing_row, self._path)
                if existing.broker_call_state != BROKER_CALL_NOT_STARTED:
                    self._conflict(
                        execution_request_id,
                        f"a journaled attempt already progressed to broker_call_state "
                        f"{existing.broker_call_state!r} (outcome {existing.outcome!r}); "
                        "the broker may have been called -- re-sending is banned and "
                        "the entry is resolved by reconciliation, never by re-submitting",
                    )
                if existing.payload_identity() != candidate.payload_identity():
                    self._conflict(
                        execution_request_id,
                        "the journaled payload differs from this attempt "
                        f"(journaled client_order_id {existing.client_order_id!r}, "
                        f"symbol {existing.symbol!r}, side {existing.side.value!r}, "
                        f"quantity {existing.quantity!r}, mode "
                        f"{existing.execution_mode!r}, target "
                        f"{existing.connection_target!r})",
                    )
                connection.execute(
                    f"UPDATE {JOURNAL_TABLE} SET submission_started_at_utc = ?, "
                    "updated_at_utc = ? WHERE execution_request_id = ?",
                    (
                        started_at_utc.isoformat(),
                        started_at_utc.isoformat(),
                        execution_request_id,
                    ),
                )
            self._read_back(connection, execution_request_id)

    def mark_broker_call_started(self, execution_request_id: str, *, at_utc: datetime) -> None:
        """Record -- IMMEDIATELY before ``Broker.submit_order`` -- that the
        broker call is about to happen. From here on the journal treats the
        outcome as possibly-sent until a response or completion is
        recorded. Requires the entry to be ``not_started``."""
        self._transition(
            execution_request_id,
            at_utc=at_utc,
            allowed_states=(BROKER_CALL_NOT_STARTED,),
            updates={"broker_call_state": BROKER_CALL_STARTED},
        )

    def record_broker_response(
        self,
        execution_request_id: str,
        *,
        response_state: OrderState,
        broker_order_id: str | None,
        at_utc: datetime,
    ) -> None:
        """Record the received broker response (before the Execution DB
        save). Requires the entry to be ``started``."""
        if not isinstance(response_state, OrderState):
            raise SubmissionJournalError(
                f"response_state must be an OrderState, got {response_state!r}"
            )
        if broker_order_id is not None:
            _require_non_empty_str(broker_order_id, "broker_order_id")
        self._transition(
            execution_request_id,
            at_utc=at_utc,
            allowed_states=(BROKER_CALL_STARTED,),
            updates={
                "broker_call_state": BROKER_CALL_RESPONDED,
                "broker_response_state": response_state.value,
                "broker_order_id": broker_order_id,
            },
        )

    def record_broker_outcome_unknown(
        self, execution_request_id: str, *, error: str, at_utc: datetime
    ) -> None:
        """The broker call raised: the outcome is UNKNOWN (the broker may
        hold the order). Marks the entry ``reconciliation_required``; it is
        resolved by later queries, never by re-sending."""
        _require_non_empty_str(error, "error")
        self._transition(
            execution_request_id,
            at_utc=at_utc,
            allowed_states=(BROKER_CALL_STARTED,),
            updates={
                "broker_call_state": BROKER_CALL_UNKNOWN,
                "outcome": OUTCOME_RECONCILIATION_REQUIRED,
                "last_error": error,
            },
        )

    def record_execution_db_save_failed(
        self,
        execution_request_id: str,
        *,
        response_state: OrderState,
        broker_order_id: str | None,
        error: str,
        at_utc: datetime,
    ) -> None:
        """The broker ANSWERED but persisting the result into the Execution
        Order DB failed: journal the response itself plus the failure, and
        mark the entry ``reconciliation_required`` -- this row is the
        durable record Issue #93 exists for. Accepted from ``started``
        (a lost step-3 response write is healed here) or ``responded``."""
        if not isinstance(response_state, OrderState):
            raise SubmissionJournalError(
                f"response_state must be an OrderState, got {response_state!r}"
            )
        if broker_order_id is not None:
            _require_non_empty_str(broker_order_id, "broker_order_id")
        _require_non_empty_str(error, "error")
        self._transition(
            execution_request_id,
            at_utc=at_utc,
            allowed_states=(BROKER_CALL_STARTED, BROKER_CALL_RESPONDED),
            updates={
                "broker_call_state": BROKER_CALL_RESPONDED,
                "broker_response_state": response_state.value,
                "broker_order_id": broker_order_id,
                "execution_db_save_state": DB_SAVE_FAILED,
                "outcome": OUTCOME_RECONCILIATION_REQUIRED,
                "last_error": error,
            },
        )

    def mark_completed(
        self,
        execution_request_id: str,
        *,
        response_state: OrderState,
        broker_order_id: str | None,
        at_utc: datetime,
    ) -> None:
        """The broker response was durably saved into the Execution Order
        DB: close the journal entry (``completed``). Accepted from
        ``started`` (a lost step-3 response write is healed here) or
        ``responded``."""
        if not isinstance(response_state, OrderState):
            raise SubmissionJournalError(
                f"response_state must be an OrderState, got {response_state!r}"
            )
        if broker_order_id is not None:
            _require_non_empty_str(broker_order_id, "broker_order_id")
        self._transition(
            execution_request_id,
            at_utc=at_utc,
            allowed_states=(BROKER_CALL_STARTED, BROKER_CALL_RESPONDED),
            updates={
                "broker_call_state": BROKER_CALL_RESPONDED,
                "broker_response_state": response_state.value,
                "broker_order_id": broker_order_id,
                "execution_db_save_state": DB_SAVE_SAVED,
                "outcome": OUTCOME_COMPLETED,
                "last_error": None,
            },
        )

    # --- reads (never create the DB file) ------------------------------------

    def get_entry(self, execution_request_id: str) -> SubmissionJournalEntry | None:
        _require_non_empty_str(execution_request_id, "execution_request_id")
        if not self._path.exists():
            return None
        with _verified_read(self._path) as connection:
            row = self._fetch_row(connection, execution_request_id)
        return None if row is None else _entry_from_row(row, self._path)

    def list_unresolved(self) -> tuple[SubmissionJournalEntry, ...]:
        """Every entry whose outcome is not ``completed`` -- oldest update
        first: attempts that never reached the broker call, possibly-sent
        attempts, unknown outcomes, and ``reconciliation_required`` rows.
        This is the hand-off surface for reconciliation and works with the
        Execution Order DB completely unavailable. Missing DB -> empty
        tuple (the write side fails closed, so nothing was ever submitted
        without a journal row)."""
        return self._list_where(f"outcome <> '{OUTCOME_COMPLETED}'")

    def list_reconciliation_required(self) -> tuple[SubmissionJournalEntry, ...]:
        """Entries explicitly flagged ``reconciliation_required`` (broker
        outcome unknown, or broker answered but the Execution DB save
        failed), oldest update first."""
        return self._list_where(f"outcome = '{OUTCOME_RECONCILIATION_REQUIRED}'")

    # --- internals -----------------------------------------------------------

    def _list_where(self, predicate: str) -> tuple[SubmissionJournalEntry, ...]:
        if not self._path.exists():
            return ()
        with _verified_read(self._path) as connection:
            rows = connection.execute(
                f"SELECT {', '.join(_ENTRY_COLUMNS)} FROM {JOURNAL_TABLE} "
                f"WHERE {predicate} ORDER BY updated_at_utc ASC, execution_request_id ASC"
            ).fetchall()
        return tuple(_entry_from_row(row, self._path) for row in rows)

    def _require_db_file(self) -> None:
        if not self._path.exists():
            raise SubmissionJournalError(
                f"submission journal at {self._path} does not exist; call "
                "initialize() first (a submission path must never run without "
                "its journal)"
            )

    def _fetch_row(self, connection: sqlite3.Connection, execution_request_id: str):
        return connection.execute(
            f"SELECT {', '.join(_ENTRY_COLUMNS)} FROM {JOURNAL_TABLE} "
            "WHERE execution_request_id = ?",
            (execution_request_id,),
        ).fetchone()

    def _conflict(self, execution_request_id: str, reason: str) -> None:
        _LOGGER.warning(
            "submission journal conflict: execution_request_id=%s reason=%s",
            execution_request_id,
            reason,
        )
        raise SubmissionJournalConflictError(
            f"submission journal conflict for execution_request_id "
            f"{execution_request_id!r}: {reason}"
        )

    def _transition(
        self,
        execution_request_id: str,
        *,
        at_utc: datetime,
        allowed_states: tuple[str, ...],
        updates: dict,
    ) -> None:
        _require_non_empty_str(execution_request_id, "execution_request_id")
        _require_utc(at_utc, "at_utc")
        self._require_db_file()
        with _verified_txn(self._path) as connection:
            row = self._fetch_row(connection, execution_request_id)
            if row is None:
                raise SubmissionJournalError(
                    f"no journal entry exists for execution_request_id "
                    f"{execution_request_id!r}; record_submission_intent() must run "
                    "before any lifecycle update"
                )
            entry = _entry_from_row(row, self._path)
            if entry.broker_call_state not in allowed_states:
                raise SubmissionJournalError(
                    f"journal entry for {execution_request_id!r} is in "
                    f"broker_call_state {entry.broker_call_state!r} (outcome "
                    f"{entry.outcome!r}); this update requires one of "
                    f"{sorted(allowed_states)}"
                )
            if at_utc < entry.updated_at_utc:
                raise SubmissionJournalError(
                    f"at_utc {at_utc.isoformat()} precedes the stored updated_at_utc "
                    f"{entry.updated_at_utc.isoformat()} for {execution_request_id!r}"
                )
            assignments = dict(updates)
            assignments["updated_at_utc"] = at_utc.isoformat()
            connection.execute(
                f"UPDATE {JOURNAL_TABLE} SET "
                + ", ".join(f"{column} = ?" for column in assignments)
                + " WHERE execution_request_id = ?",
                (*assignments.values(), execution_request_id),
            )
            self._read_back(connection, execution_request_id)

    def _read_back(self, connection: sqlite3.Connection, execution_request_id: str) -> None:
        """Pre-commit write-back validation: the mutated row must re-read as
        a fully valid entry INSIDE the same transaction, or the whole write
        rolls back."""
        row = self._fetch_row(connection, execution_request_id)
        if row is None:
            raise SubmissionJournalIntegrityError(
                f"journal write for {execution_request_id!r} did not read back"
            )
        _entry_from_row(row, self._path)
