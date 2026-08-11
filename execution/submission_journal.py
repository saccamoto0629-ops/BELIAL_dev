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
double-send); reclaiming refreshes the attempt timestamp only and rejects
timestamps behind the stored ``updated_at_utc`` (same monotonicity rule as
every other update). The compared payload covers EVERY broker-bound
``OrderRequest`` field derivable from the intent -- ``client_order_id``,
``symbol``, ``side``, ``order_type``, ``quantity``, ``requested_price``
(the LIMIT price) -- plus the connection identity, so a re-registered
intent whose order CONTENT changed (e.g. MARKET turned LIMIT, or a moved
limit price) can never silently reuse the journaled attempt (PR #113
review P1).

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

from broker.broker_models import OrderSide, OrderState, OrderType
from execution.execution_store import _ddl_tokens  # shared DDL token normalizer (PR #91 r3)
from execution.execution_models import _require_non_empty_str, _require_utc
from execution.sqlite_ro import connect_read_only

_LOGGER = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "submission_journal.sqlite3"
BUSY_TIMEOUT_SECONDS = 30

JOURNAL_TABLE = "submission_journal"
SCHEMA_VERSION = 2

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
# ``superseded`` (Issue #95 / #116, schema v2) is the TERMINAL state of an
# intent whose non-submission became definitive: its parent order was
# retired by a reissue child (gate correction), so this journaled attempt
# will never be sent. It is reachable ONLY from ``not_started`` -- the one
# state that PROVES the broker was never called -- so terminalization can
# never touch an intent the broker might hold, and it never increases
# submission possibility (a superseded entry can neither be reclaimed nor
# progress to a broker call).
OUTCOME_OPEN = "open"
OUTCOME_COMPLETED = "completed"
OUTCOME_RECONCILIATION_REQUIRED = "reconciliation_required"
OUTCOME_SUPERSEDED = "superseded"
OUTCOMES: frozenset[str] = frozenset(
    {OUTCOME_OPEN, OUTCOME_COMPLETED, OUTCOME_RECONCILIATION_REQUIRED, OUTCOME_SUPERSEDED}
)
# The outcomes that end an entry's lifecycle: nothing about them remains
# unresolved, so ``list_unresolved`` excludes them.
TERMINAL_OUTCOMES: frozenset[str] = frozenset({OUTCOME_COMPLETED, OUTCOME_SUPERSEDED})

_ENTRY_COLUMNS: tuple[str, ...] = (
    "execution_request_id",
    "client_order_id",
    "symbol",
    "side",
    "order_type",
    "quantity",
    "requested_price",
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
    "superseded_by_execution_request_id",
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


def _create_journal_table_sql(*, version: int = SCHEMA_VERSION) -> str:
    """Full schema for ``version`` (1 or 2). The CHECKs mirror the
    :class:`SubmissionJournalEntry` invariants as a second guard, foremost
    the lifecycle consistency: ``completed`` requires a saved Execution DB
    result, ``unknown`` / ``failed`` require ``reconciliation_required`` and
    a recorded failure reason, and nothing before ``responded`` may carry a
    broker response. Schema v2 (Issue #116) adds the terminal ``superseded``
    outcome: reachable only from ``not_started`` (the broker was provably
    never called), and it must name the superseding execution request."""
    sides = _enum_values_sql(OrderSide)
    types = _enum_values_sql(OrderType)
    states = _enum_values_sql(OrderState)
    call_states = "', '".join(sorted(BROKER_CALL_STATES))
    save_states = "', '".join(sorted(DB_SAVE_STATES))
    if version < 2:
        outcomes = "', '".join(sorted(OUTCOMES - {OUTCOME_SUPERSEDED}))
        superseded_column = ""
        lifecycle_checks = f"""CHECK (broker_call_state NOT IN ('{BROKER_CALL_NOT_STARTED}', '{BROKER_CALL_STARTED}') OR outcome = '{OUTCOME_OPEN}'),"""
    else:
        outcomes = "', '".join(sorted(OUTCOMES))
        superseded_column = """superseded_by_execution_request_id TEXT CHECK (superseded_by_execution_request_id IS NULL OR trim(superseded_by_execution_request_id) <> ''),
            """
        lifecycle_checks = f"""CHECK (broker_call_state <> '{BROKER_CALL_STARTED}' OR outcome = '{OUTCOME_OPEN}'),
            CHECK (broker_call_state <> '{BROKER_CALL_NOT_STARTED}' OR outcome IN ('{OUTCOME_OPEN}', '{OUTCOME_SUPERSEDED}')),
            CHECK (outcome <> '{OUTCOME_SUPERSEDED}' OR broker_call_state = '{BROKER_CALL_NOT_STARTED}'),
            CHECK ((outcome = '{OUTCOME_SUPERSEDED}') = (superseded_by_execution_request_id IS NOT NULL)),
            CHECK (superseded_by_execution_request_id IS NULL OR superseded_by_execution_request_id <> execution_request_id),"""
    return f"""
        CREATE TABLE {JOURNAL_TABLE} (
            execution_request_id TEXT PRIMARY KEY NOT NULL CHECK (trim(execution_request_id) <> ''),
            client_order_id TEXT NOT NULL UNIQUE CHECK (trim(client_order_id) <> ''),
            symbol TEXT NOT NULL CHECK (trim(symbol) <> ''),
            side TEXT NOT NULL CHECK (side IN ('{sides}')),
            order_type TEXT NOT NULL CHECK (order_type IN ('{types}')),
            quantity TEXT NOT NULL CHECK (trim(quantity) <> ''),
            requested_price TEXT CHECK (requested_price IS NULL OR trim(requested_price) <> ''),
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
            {superseded_column}CHECK ((order_type = '{OrderType.LIMIT.value}') = (requested_price IS NOT NULL)),
            CHECK ((broker_call_state = '{BROKER_CALL_RESPONDED}') = (broker_response_state IS NOT NULL)),
            CHECK (broker_order_id IS NULL OR broker_call_state = '{BROKER_CALL_RESPONDED}'),
            CHECK (execution_db_save_state = '{DB_SAVE_NOT_ATTEMPTED}' OR broker_call_state = '{BROKER_CALL_RESPONDED}'),
            CHECK ((outcome = '{OUTCOME_COMPLETED}') = (execution_db_save_state = '{DB_SAVE_SAVED}')),
            {lifecycle_checks}
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


def _create_schema(connection: sqlite3.Connection, *, version: int = SCHEMA_VERSION) -> None:
    connection.execute(_create_journal_table_sql(version=version))
    connection.execute(_create_index_sql())
    connection.execute(f"PRAGMA user_version = {version}")


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


_REFERENCE_SCHEMA_CACHE: dict[int, tuple[dict, dict]] = {}


def _reference_schema(version: int = SCHEMA_VERSION) -> tuple[dict, dict]:
    """Pristine reference schema for ``version`` built once in memory: the
    complete ``sqlite_master`` object map (``sqlite_autoindex_*`` from the
    UNIQUE constraint included) plus the PRAGMA structural signature."""
    if version not in _REFERENCE_SCHEMA_CACHE:
        with closing(sqlite3.connect(":memory:")) as connection:
            _create_schema(connection, version=version)
            objects = {
                (row[0], row[1]): (row[2], None if row[3] is None else _ddl_tokens(row[3]))
                for row in connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")
            }
            _REFERENCE_SCHEMA_CACHE[version] = (objects, _structure_signature(connection))
    return _REFERENCE_SCHEMA_CACHE[version]


def _verify_schema(
    connection: sqlite3.Connection, db_path: Path, *, version: int = SCHEMA_VERSION
) -> None:
    """Closed-world verification (same stance as ``execution_store``): the
    COMPLETE ``sqlite_master`` object set -- triggers and views included --
    must exactly equal the pristine reference set, the token-normalized DDL
    of every SQL-bearing object must match, the PRAGMA structural signature
    must match, and the schema version must match. Read-only."""
    problems: list[str] = []
    stored_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if stored_version != version:
        problems.append(f"user_version is {stored_version}, expected {version}")

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
    order_type: OrderType
    quantity: Decimal
    execution_mode: str
    connection_target: str
    submission_started_at_utc: datetime
    broker_call_state: str
    execution_db_save_state: str
    outcome: str
    updated_at_utc: datetime
    requested_price: Decimal | None = None
    broker_response_state: OrderState | None = None
    broker_order_id: str | None = None
    last_error: str | None = None
    superseded_by_execution_request_id: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.execution_request_id, "execution_request_id")
        _require_non_empty_str(self.client_order_id, "client_order_id")
        _require_non_empty_str(self.symbol, "symbol")
        if not isinstance(self.side, OrderSide):
            raise ValueError(f"side must be an OrderSide, got {self.side!r}")
        if not isinstance(self.order_type, OrderType):
            raise ValueError(f"order_type must be an OrderType, got {self.order_type!r}")
        if not isinstance(self.quantity, Decimal) or not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError(f"quantity must be a positive finite Decimal, got {self.quantity!r}")
        # Mirrors the OrderRequest / ExecutionRequest invariant: LIMIT carries
        # the price that goes to the broker; MARKET must not carry one.
        if (self.order_type is OrderType.LIMIT) != (self.requested_price is not None):
            raise ValueError(
                "requested_price must be present exactly for LIMIT orders (got "
                f"order_type {self.order_type!r}, requested_price {self.requested_price!r})"
            )
        if self.requested_price is not None and (
            not isinstance(self.requested_price, Decimal)
            or not self.requested_price.is_finite()
            or self.requested_price <= 0
        ):
            raise ValueError(
                f"requested_price must be a positive finite Decimal, got {self.requested_price!r}"
            )
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
        if self.broker_call_state == BROKER_CALL_STARTED and self.outcome != OUTCOME_OPEN:
            raise ValueError(
                f"broker_call_state {BROKER_CALL_STARTED!r} requires outcome "
                f"{OUTCOME_OPEN!r}, got {self.outcome!r}"
            )
        if self.broker_call_state == BROKER_CALL_NOT_STARTED and self.outcome not in (
            OUTCOME_OPEN,
            OUTCOME_SUPERSEDED,
        ):
            raise ValueError(
                f"broker_call_state {BROKER_CALL_NOT_STARTED!r} requires outcome "
                f"{OUTCOME_OPEN!r} or {OUTCOME_SUPERSEDED!r}, got {self.outcome!r}"
            )
        # Terminal supersession (Issue #116): only a provably-never-sent
        # intent may be superseded, and the superseding request is named for
        # audit -- exactly when the outcome is ``superseded``.
        if self.superseded_by_execution_request_id is not None:
            _require_non_empty_str(
                self.superseded_by_execution_request_id, "superseded_by_execution_request_id"
            )
            if self.superseded_by_execution_request_id == self.execution_request_id:
                raise ValueError("an intent cannot be superseded by itself")
        if (self.outcome == OUTCOME_SUPERSEDED) != (
            self.superseded_by_execution_request_id is not None
        ):
            raise ValueError(
                f"outcome {OUTCOME_SUPERSEDED!r} and superseded_by_execution_request_id "
                f"must coincide (got outcome {self.outcome!r}, superseded_by "
                f"{self.superseded_by_execution_request_id!r})"
            )
        if self.outcome == OUTCOME_SUPERSEDED and self.broker_call_state != BROKER_CALL_NOT_STARTED:
            raise ValueError(
                f"outcome {OUTCOME_SUPERSEDED!r} requires broker_call_state "
                f"{BROKER_CALL_NOT_STARTED!r} (a possibly-sent intent is never "
                f"terminalized), got {self.broker_call_state!r}"
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
            self.order_type,
            self.quantity,
            self.requested_price,
            self.execution_mode,
            self.connection_target,
        )


def _entry_from_row(row, db_path: Path) -> SubmissionJournalEntry:
    try:
        quantity = Decimal(row["quantity"])
        if not quantity.is_finite():
            raise ValueError(f"stored quantity is not finite: {row['quantity']!r}")
        requested_price = (
            None if row["requested_price"] is None else Decimal(row["requested_price"])
        )
        return SubmissionJournalEntry(
            execution_request_id=row["execution_request_id"],
            client_order_id=row["client_order_id"],
            symbol=row["symbol"],
            side=OrderSide(row["side"]),
            order_type=OrderType(row["order_type"]),
            quantity=quantity,
            requested_price=requested_price,
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
            superseded_by_execution_request_id=row["superseded_by_execution_request_id"],
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
                elif version == 1:
                    self._migrate_v1_to_v2(connection)
                else:
                    _verify_schema(connection, self._path)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _migrate_v1_to_v2(self, connection: sqlite3.Connection) -> None:
        """v1 -> v2 migration (Issue #116), inside the caller's ``BEGIN
        IMMEDIATE`` transaction. The v2 lifecycle CHECKs replace a v1 inline
        table CHECK, which SQLite's ``ALTER TABLE`` cannot rewrite, so the
        journal table is REBUILT: verify the DB matches the released v1
        schema COMPLETELY, create the v2 table, copy every row unchanged
        (``superseded_by_execution_request_id`` NULL -- v2 is a strict
        relaxation, so every valid v1 row satisfies the v2 constraints),
        verify the copy row-for-row, drop the v1 table, recreate the index,
        bump the version and fully re-verify as v2. Any failure rolls the
        whole transaction back and leaves the v1 file untouched."""
        _verify_schema(connection, self._path, version=1)
        v1_columns = tuple(
            column for column in _ENTRY_COLUMNS
            if column != "superseded_by_execution_request_id"
        )
        migration_table = f"{JOURNAL_TABLE}_v1_migration"
        connection.execute(f"DROP INDEX {_JOURNAL_OUTCOME_INDEX}")
        connection.execute(f"ALTER TABLE {JOURNAL_TABLE} RENAME TO {migration_table}")
        connection.execute(_create_journal_table_sql())
        connection.execute(
            f"INSERT INTO {JOURNAL_TABLE} ({', '.join(v1_columns)}) "
            f"SELECT {', '.join(v1_columns)} FROM {migration_table}"
        )
        before = connection.execute(f"SELECT COUNT(*) FROM {migration_table}").fetchone()[0]
        after = connection.execute(f"SELECT COUNT(*) FROM {JOURNAL_TABLE}").fetchone()[0]
        if before != after:
            raise SubmissionJournalIntegrityError(
                f"v1 -> v2 migration of {self._path} copied {after} of {before} rows; "
                "rolling back"
            )
        connection.execute(f"DROP TABLE {migration_table}")
        connection.execute(_create_index_sql())
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _verify_schema(connection, self._path)
        # every migrated row must restore as a valid v2 entry
        for row in connection.execute(
            f"SELECT {', '.join(_ENTRY_COLUMNS)} FROM {JOURNAL_TABLE}"
        ):
            _entry_from_row(row, self._path)

    # --- writes (all fail closed) --------------------------------------------

    def record_submission_intent(
        self,
        *,
        execution_request_id: str,
        client_order_id: str,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: Decimal,
        requested_price: Decimal | None,
        execution_mode: str,
        connection_target: str,
        started_at_utc: datetime,
    ) -> None:
        """Durably journal a submission attempt BEFORE any broker call.

        Inserts the ``not_started`` row, or RECLAIMS an existing
        ``not_started`` row whose payload is IDENTICAL (a prior attempt that
        provably never reached the broker -- reclaiming refreshes the
        attempt timestamp only, and refuses a timestamp behind the stored
        ``updated_at_utc``, PR #113 review P2). The compared payload covers
        every broker-bound ``OrderRequest`` field -- ``client_order_id``,
        ``symbol``, ``side``, ``order_type``, ``quantity``,
        ``requested_price`` -- plus the connection identity (PR #113 review
        P1). Everything else conflicts
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
        if not isinstance(order_type, OrderType):
            raise SubmissionJournalError(f"order_type must be an OrderType, got {order_type!r}")
        if not isinstance(quantity, Decimal) or not quantity.is_finite() or quantity <= 0:
            raise SubmissionJournalError(
                f"quantity must be a positive finite Decimal, got {quantity!r}"
            )
        if requested_price is not None and (
            not isinstance(requested_price, Decimal)
            or not requested_price.is_finite()
            or requested_price <= 0
        ):
            raise SubmissionJournalError(
                f"requested_price must be a positive finite Decimal or None, got "
                f"{requested_price!r}"
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
            order_type=order_type,
            quantity=quantity,
            requested_price=requested_price,
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
                        candidate.order_type.value,
                        str(candidate.quantity),
                        (
                            None
                            if candidate.requested_price is None
                            else str(candidate.requested_price)
                        ),
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
                        None,
                    ),
                )
            else:
                existing = _entry_from_row(existing_row, self._path)
                if existing.outcome == OUTCOME_SUPERSEDED:
                    # Terminal (Issue #116): the intent's non-submission is
                    # definitive; reclaiming it would increase submission
                    # possibility, which terminalization must never do.
                    self._conflict(
                        execution_request_id,
                        f"the journaled intent was terminally superseded by "
                        f"{existing.superseded_by_execution_request_id!r}; a superseded "
                        "intent is never reclaimed or re-submitted",
                    )
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
                        f"order_type {existing.order_type.value!r}, "
                        f"quantity {existing.quantity!r}, requested_price "
                        f"{existing.requested_price!r}, mode "
                        f"{existing.execution_mode!r}, target "
                        f"{existing.connection_target!r})",
                    )
                if started_at_utc < existing.updated_at_utc:
                    # Same monotonicity rule as _transition() (PR #113 review
                    # P2): a reclaim must never move the durable timestamps
                    # backwards, or unresolved-entry ordering and crash
                    # chronology become inaccurate.
                    raise SubmissionJournalError(
                        f"started_at_utc {started_at_utc.isoformat()} precedes the "
                        f"stored updated_at_utc {existing.updated_at_utc.isoformat()} "
                        f"for {execution_request_id!r}; refusing to reclaim backwards"
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

    def mark_superseded(
        self,
        execution_request_id: str,
        *,
        superseded_by_execution_request_id: str,
        at_utc: datetime,
    ) -> bool:
        """Terminalize a provably-never-sent intent whose parent order was
        retired by a reissue child (Issue #95 / #116): ``not_started`` +
        ``open`` becomes the terminal ``superseded`` outcome, naming the
        superseding request for audit.

        Fail-closed toward broker reachability: ONLY a ``not_started`` entry
        may be terminalized -- ``started`` / ``responded`` / ``unknown``
        entries (the broker may hold the order) raise
        :class:`SubmissionJournalError` and stay untouched, exactly like any
        other refused transition. An entry already superseded BY THE SAME
        request is an idempotent no-op returning ``False`` (recovery sweeps
        re-run safely); superseded by a DIFFERENT request is a conflict.
        Returns ``True`` when this call performed the terminalization."""
        _require_non_empty_str(execution_request_id, "execution_request_id")
        _require_non_empty_str(
            superseded_by_execution_request_id, "superseded_by_execution_request_id"
        )
        if superseded_by_execution_request_id == execution_request_id:
            raise SubmissionJournalError("an intent cannot be superseded by itself")
        _require_utc(at_utc, "at_utc")
        self._require_db_file()
        with _verified_txn(self._path) as connection:
            row = self._fetch_row(connection, execution_request_id)
            if row is None:
                raise SubmissionJournalError(
                    f"no journal entry exists for execution_request_id "
                    f"{execution_request_id!r}; nothing to supersede"
                )
            entry = _entry_from_row(row, self._path)
            if entry.outcome == OUTCOME_SUPERSEDED:
                if (
                    entry.superseded_by_execution_request_id
                    == superseded_by_execution_request_id
                ):
                    return False  # already terminal, same provenance: no-op
                self._conflict(
                    execution_request_id,
                    f"the entry is already superseded by "
                    f"{entry.superseded_by_execution_request_id!r}, not by "
                    f"{superseded_by_execution_request_id!r}",
                )
            if entry.broker_call_state != BROKER_CALL_NOT_STARTED:
                raise SubmissionJournalError(
                    f"journal entry for {execution_request_id!r} is in "
                    f"broker_call_state {entry.broker_call_state!r}; only a "
                    f"{BROKER_CALL_NOT_STARTED!r} entry (the broker was provably never "
                    "called) may be terminalized as superseded"
                )
            if at_utc < entry.updated_at_utc:
                raise SubmissionJournalError(
                    f"at_utc {at_utc.isoformat()} precedes the stored updated_at_utc "
                    f"{entry.updated_at_utc.isoformat()} for {execution_request_id!r}"
                )
            connection.execute(
                f"UPDATE {JOURNAL_TABLE} SET outcome = ?, "
                "superseded_by_execution_request_id = ?, updated_at_utc = ? "
                "WHERE execution_request_id = ?",
                (
                    OUTCOME_SUPERSEDED,
                    superseded_by_execution_request_id,
                    at_utc.isoformat(),
                    execution_request_id,
                ),
            )
            self._read_back(connection, execution_request_id)
        return True

    # --- reads (never create the DB file) ------------------------------------

    def get_entry(self, execution_request_id: str) -> SubmissionJournalEntry | None:
        _require_non_empty_str(execution_request_id, "execution_request_id")
        if not self._path.exists():
            return None
        with _verified_read(self._path) as connection:
            row = self._fetch_row(connection, execution_request_id)
        return None if row is None else _entry_from_row(row, self._path)

    def list_unresolved(self) -> tuple[SubmissionJournalEntry, ...]:
        """Every entry whose outcome is not terminal (``completed`` /
        ``superseded``) -- oldest update first: attempts that never reached
        the broker call, possibly-sent attempts, unknown outcomes, and
        ``reconciliation_required`` rows. Terminally superseded intents
        (Issue #116) are resolved facts and deliberately NOT listed -- an
        intent whose non-submission is definitive must not be reported as
        unresolved forever. This is the hand-off surface for reconciliation
        and works with the Execution Order DB completely unavailable.
        Missing DB -> empty tuple (the write side fails closed, so nothing
        was ever submitted without a journal row)."""
        terminal = "', '".join(sorted(TERMINAL_OUTCOMES))
        return self._list_where(f"outcome NOT IN ('{terminal}')")

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
            if entry.outcome == OUTCOME_SUPERSEDED:
                # Terminal (Issue #116): a superseded entry never progresses
                # -- foremost not toward a broker call.
                raise SubmissionJournalError(
                    f"journal entry for {execution_request_id!r} was terminally "
                    f"superseded by {entry.superseded_by_execution_request_id!r}; no "
                    "further lifecycle update is accepted"
                )
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
