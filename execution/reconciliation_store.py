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

Repair attempts (Issue #104) follow the same append stance with a TWO-PHASE
lifecycle in ``broker_reconciliation_repair_attempts`` (FK -> run, composite
FK -> the exact discrepancy): every attempt is INSERTed as a ``pending``
RESERVATION (:func:`reserve_repair_attempt`) BEFORE anything is evaluated
or written to the execution order store, and completed exactly once to
``finalized`` (:func:`finalize_repair_attempt`, compare-and-set on the
pending status; double finalize raises
:class:`RepairAttemptConflictError`). Every attempt -- applied, already
applied, skipped, stale, conflicting, rejected, query-failed, failed -- is
an independent audit event; re-running a repair reserves new attempt rows
and never updates or flags the stored run / discrepancy rows or earlier
attempts. ``attempt_id`` is the PRIMARY KEY; reusing one is refused at
RESERVATION time -- before any state update could have happened. A pending
row that never finalized (crash) stays discoverable
(:func:`list_pending_repair_attempts`) and is finalized later under its
original id by the repair service's recovery. Dry-run reserves and records
nothing: dry-run is a no-side-effect contract, not an audit category.

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

Migration (Issue #104): schema v2 adds the repair-attempts table + index
and the ``discrepancies.internal_revision`` column (nullable ``ALTER TABLE
ADD COLUMN`` -- existing rows read back as NULL and are never
auto-repaired); the runs table is byte-identical between the versions, so
the v1 -> v2 migration is purely ADDITIVE. ``init_reconciliation_db``
migrates a COMPLETE, verified v1 schema in ONE ``BEGIN IMMEDIATE``
transaction (alter -> create table -> create index -> version bump -> full
v2 re-verification -> COMMIT); existing rows are read back unchanged and a
failed migration rolls back to the untouched v1 file. Read paths NEVER
migrate (they refuse a v1 DB with a schema error pointing at
``init_reconciliation_db``), so dry-run / read-only contracts keep their
no-side-effect guarantee.
"""

from __future__ import annotations

import json
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
    ORDER_DISCREPANCY_TYPES,
    ORDER_TARGET_TYPES,
    OrderDiscrepancy,
    POSITION_DISCREPANCY_TYPES,
    POSITION_TARGET_TYPES,
    PositionDiscrepancy,
    PositionReconciliationSummary,
    ReconciliationMode,
    ReconciliationRun,
    ReconciliationTargetType,
    ResolutionCategory,
)
from execution.reconciliation_repair_models import (
    ACTIVE_INTENT_STATUSES,
    ALLOWED_INTENT_TRANSITIONS,
    OrderRepairAttempt,
    RepairAttemptReservation,
    RepairFailureCode,
    RepairMode,
    RepairOutcome,
    RepairTakeoverIntent,
    COMPLETABLE_TAKEOVER_OUTCOMES,
    TakeoverBlockedReason,
    TakeoverCancelReason,
    TakeoverIntentStatus,
)
from execution.sqlite_ro import ReadOnlyDbMissingError, connect_read_only

_LOGGER = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "broker_reconciliation.sqlite3"
BUSY_TIMEOUT_SECONDS = 30

RUNS_TABLE = "broker_reconciliation_runs"
DISCREPANCIES_TABLE = "broker_reconciliation_discrepancies"
REPAIRS_TABLE = "broker_reconciliation_repair_attempts"
TAKEOVER_INTENTS_TABLE = "broker_reconciliation_repair_takeover_intents"
POSITION_DISCREPANCIES_TABLE = "broker_reconciliation_position_discrepancies"

# v1: initial Issue #103 schema (runs + discrepancies).
# v2 (Issue #104): + broker_reconciliation_repair_attempts (two-phase repair
#     audit: pending CLAIM -> finalized outcome),
#     + broker_reconciliation_repair_takeover_intents (the DURABLE takeover
#     intents that make a cross-DB takeover restartable under ONE identity)
#     and discrepancies.internal_revision (the internal order's revision at
#     observation time, so repair can prove the order is unchanged). Additive
#     over v1 (ALTER ADD COLUMN + new tables + indexes); init migrates a
#     verified v1 DB in place (see the module docstring). Both v2 tables are
#     introduced by the same unreleased change, so there is no intermediate
#     released version between them to migrate from.
# v3 (Issue #105): + broker_reconciliation_position_discrepancies (the
#     position / trade / transaction / SL / TP detection records; MANUAL
#     REVIEW only by CHECK — no repair path exists for this domain) and the
#     position-domain summary columns on the runs table. Additive over v2
#     (ALTER ADD COLUMN + one new table + index); the Issue #103/#104 table
#     DDL is byte-identical to v2 — the order-domain CHECK constraints are
#     generated from the frozen ORDER_* subsets, not from the grown enums.
SCHEMA_VERSION = 3
# Exactly released prior versions are migratable, newest first; migration is
# applied stepwise (v1 -> v2 -> v3) inside one transaction.
_MIGRATABLE_VERSIONS = (2, 1)

# Repair attempt lifecycle statuses (Issue #104): a row is INSERTed as
# ``pending`` (the reservation) and completed exactly once to ``finalized``.
ATTEMPT_STATUS_PENDING = "pending"
ATTEMPT_STATUS_FINALIZED = "finalized"

_RUN_COLUMNS: tuple[str, ...] = (
    "run_id",
    "mode",
    "started_at_utc",
    "finished_at_utc",
    "checked_order_count",
    "broker_listing_error",
    # v3 (Issue #105): the position-domain summary. All NULL exactly when
    # the run did not reconcile the position domain (order-only runs and
    # rows stored before v3).
    "checked_position_count",
    "checked_trade_count",
    "checked_closed_trade_count",
    "internal_exposure_error",
    "position_listing_error",
    "trade_listing_error",
    "closed_trade_listing_error",
    "excluded_symbols_json",
)
_POSITION_DISCREPANCY_COLUMNS: tuple[str, ...] = (
    "run_id",
    "discrepancy_type",
    "target_type",
    "target_id",
    "resolution_category",
    "symbol",
    "internal_value",
    "broker_value",
    "execution_request_id",
    "client_order_id",
    "broker_trade_id",
    "protection_order_id",
    "broker_transaction_ids_json",
    "detail",
    "observed_at_utc",
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
    "internal_revision",
)
# Columns set by the RESERVATION (INSERT, status pending); the remaining
# outcome columns stay NULL until finalize.
_RESERVATION_COLUMNS: tuple[str, ...] = (
    "attempt_id",
    "run_id",
    "mode",
    "status",
    "target_type",
    "target_id",
    "execution_request_id",
    "client_order_id",
    "reconciliation_internal_state",
    "reconciliation_broker_state",
    "internal_revision",
    "detail",
    "observed_at_utc",
    "reserved_at_utc",
    "owner_id",
    "claim_execution_id",
    "generation",
    "takeover_id",
    "lease_expires_at_utc",
    "last_claimed_at_utc",
)
# The intent row's columns, in DDL order.
_INTENT_COLUMNS: tuple[str, ...] = (
    "takeover_id",
    "repair_attempt_id",
    "source_generation",
    "target_generation",
    "source_owner_id",
    "source_claim_execution_id",
    "target_owner_id",
    "target_claim_execution_id",
    "lease_expires_at_utc",
    "status",
    "created_at_utc",
    "fence_installed_at_utc",
    "claim_promoted_at_utc",
    "completed_at_utc",
    "cancelled_at_utc",
    "blocked_at_utc",
    "blocked_reason",
    "cancelled_reason",
    "observed_claim_generation",
    "observed_fence_generation",
    "observed_fence_identity",
)
# Identity/anchor columns that must be IDENTICAL between the stored pending
# reservation and the finalizing attempt (a finalize may only complete the
# reservation it belongs to). The CLAIM columns (owner / claim execution /
# generation) are deliberately NOT here: they are compare-and-set fencing
# conditions -- a mismatch there is a LOST CLAIM, not corruption.
_ATTEMPT_IDENTITY_COLUMNS: tuple[str, ...] = (
    "run_id",
    "mode",
    "target_type",
    "target_id",
    "execution_request_id",
    "client_order_id",
    "reconciliation_internal_state",
    "reconciliation_broker_state",
    "internal_revision",
    "observed_at_utc",
    "reserved_at_utc",
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


class RepairClaimLostError(ReconciliationStoreError):
    """The caller no longer holds the attempt's claim (Issue #104 fencing).

    Raised when a takeover or finalize compare-and-set finds the pending row
    claimed by a different owner / claim execution / generation -- e.g. the
    lease expired and a recovery worker took over, or another recovery
    worker won the takeover race. The caller's work must be classified as a
    typed lost-claim result, never recorded as a competing outcome."""


class TakeoverIntentConflictError(ReconciliationStoreError):
    """An active takeover intent already exists for this attempt, or the
    intent's preconditions no longer hold (r4 review).

    Raised when a worker tries to CREATE an intent while another one is
    active, when the attempt is already finalized, when the claim's lease is
    still valid, or when the intent's ``source_*`` identity does not match
    the attempt's current claim. The loser must LOAD the winner's active
    intent and continue it under the SAME identity -- never mint a new one.
    """


class TakeoverIntentStateError(ReconciliationStoreError):
    """An intent transition was refused (r4 review).

    Either the transition is not allowed by the closed lifecycle (backwards,
    out of a terminal status) or the caller's identity does not match the
    stored intent. Re-applying the CURRENT status is NOT an error -- it is
    an idempotent no-op, so a restarted worker can always retry its step."""


class TakeoverProtocolInvariantError(ReconciliationStoreError):
    """A takeover status was asked to move ahead of the SUBSTANCE it asserts
    (r5 review Blocker).

    ``claim_promoted`` means "the attempt's claim IS this intent's identity"
    and ``completed`` means "the attempt was FINALIZED under it". Both are
    verified against the real attempt row inside the same transaction, so a
    caller -- including a direct user of this store API -- can never walk an
    intent's status forward while the claim, the finalize or the fence never
    happened. The transaction rolls back and nothing moves."""


class TakeoverClaimNotPromotedError(TakeoverProtocolInvariantError):
    """``claim_promoted`` was requested while the attempt is not a pending
    claim held by this intent."""


class TakeoverFenceUnavailableError(TakeoverProtocolInvariantError):
    """The order store could not be held still long enough to promote a claim
    (Issue #104 re-review r7).

    The promotion never entered its critical section: no fence was read
    under a lock, no claim was moved and both DBs are untouched. Unlike an
    identity mismatch this is a TRANSIENT condition -- another writer simply
    held the order store past the busy timeout -- so the caller backs off
    and the next pass resumes the SAME durable intent."""


class TakeoverIdentityMismatchError(TakeoverProtocolInvariantError):
    """The attempt (or a fence proof) is held by a DIFFERENT identity than
    the intent whose status was to be advanced."""


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


def _iso_optional(value: datetime | None) -> str | None:
    return None if value is None else _iso(value)


def _parse_optional_utc(text: str | None) -> datetime | None:
    return None if text is None else _parse_utc(text)


def _require_utc_argument(value, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ReconciliationStoreError(
            f"{field_name} must be a timezone-aware UTC datetime, got {value!r}"
        )


def _read_path(db_path: Path | None) -> Path:
    return Path(db_path) if db_path is not None else reconciliation_db_path()


def _enum_values_sql(values) -> str:
    return "', '".join(sorted(v.value for v in values))


# Shared between the v3 reference CREATE and the v2 -> v3 ALTERs so a
# migrated DB token-normalizes identically to a freshly created one (SQLite
# appends an added column at the end of the column list). Order matters and
# is part of the schema. Column-level CHECKs only: ALTER TABLE ADD COLUMN
# cannot introduce table-level constraints, so cross-column coherence (all
# eight NULL together or the three counts NOT NULL together) is enforced by
# the model and the restore path instead.
_RUN_POSITION_COLUMN_DDLS: tuple[str, ...] = (
    "checked_position_count INTEGER "
    "CHECK (checked_position_count IS NULL OR checked_position_count >= 0)",
    "checked_trade_count INTEGER "
    "CHECK (checked_trade_count IS NULL OR checked_trade_count >= 0)",
    "checked_closed_trade_count INTEGER "
    "CHECK (checked_closed_trade_count IS NULL OR checked_closed_trade_count >= 0)",
    "internal_exposure_error TEXT "
    "CHECK (internal_exposure_error IS NULL OR trim(internal_exposure_error) <> '')",
    "position_listing_error TEXT "
    "CHECK (position_listing_error IS NULL OR trim(position_listing_error) <> '')",
    "trade_listing_error TEXT "
    "CHECK (trade_listing_error IS NULL OR trim(trade_listing_error) <> '')",
    "closed_trade_listing_error TEXT "
    "CHECK (closed_trade_listing_error IS NULL OR trim(closed_trade_listing_error) <> '')",
    "excluded_symbols_json TEXT "
    "CHECK (excluded_symbols_json IS NULL OR trim(excluded_symbols_json) <> '')",
)


def _create_runs_table_sql(*, version: int = SCHEMA_VERSION) -> str:
    """Full schema for the run rows at ``version``. The CHECKs mirror the
    ``ReconciliationRun`` invariants as a second guard (known mode, UTC
    suffix, non-negative count, non-empty listing error when present). v3
    appends the position-domain summary columns (Issue #105)."""
    modes = _enum_values_sql(ReconciliationMode)
    position_columns = (
        "".join(f",\n            {ddl}" for ddl in _RUN_POSITION_COLUMN_DDLS)
        if version >= 3
        else ""
    )
    return f"""
        CREATE TABLE {RUNS_TABLE} (
            run_id TEXT PRIMARY KEY NOT NULL CHECK (trim(run_id) <> ''),
            mode TEXT NOT NULL CHECK (mode IN ('{modes}')),
            started_at_utc TEXT NOT NULL CHECK (started_at_utc LIKE '%+00:00'),
            finished_at_utc TEXT NOT NULL CHECK (finished_at_utc LIKE '%+00:00'),
            checked_order_count INTEGER NOT NULL CHECK (checked_order_count >= 0),
            broker_listing_error TEXT
                CHECK (broker_listing_error IS NULL OR trim(broker_listing_error) <> ''){position_columns}
        ) WITHOUT ROWID
    """


# Shared between the v2 reference CREATE and the v1 -> v2 ALTER so a migrated
# DB token-normalizes identically to a freshly created one (SQLite inserts an
# added column at the end of the column list, before the table constraints).
_DISCREPANCY_REVISION_COLUMN_DDL = (
    "internal_revision INTEGER CHECK (internal_revision IS NULL OR internal_revision >= 0)"
)


def _create_discrepancies_table_sql(*, version: int = SCHEMA_VERSION) -> str:
    """Full schema for the discrepancy rows at ``version``. The CHECKs
    mirror the ``OrderDiscrepancy`` invariants: known enum members only, and
    the ``query_failed`` category is used exactly for the ``query_failure``
    kind (which is also the only kind carrying a broker error code)."""
    # ORDER-domain subsets, NOT the full enums (Issue #105): the position
    # domain grew DiscrepancyType / ReconciliationTargetType, and this
    # table's DDL must stay byte-identical to the released v2 definition so
    # existing DBs keep verifying without a rebuild.
    types = _enum_values_sql(ORDER_DISCREPANCY_TYPES)
    targets = _enum_values_sql(ORDER_TARGET_TYPES)
    categories = _enum_values_sql(ResolutionCategory)
    states = _enum_values_sql(OrderState)
    codes = _enum_values_sql(BrokerErrorCode)
    query_failure = DiscrepancyType.QUERY_FAILURE.value
    query_failed = ResolutionCategory.QUERY_FAILED.value
    revision_column = (
        f",\n            {_DISCREPANCY_REVISION_COLUMN_DDL}" if version >= 2 else ""
    )
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
            observed_at_utc TEXT NOT NULL CHECK (observed_at_utc LIKE '%+00:00'){revision_column},
            UNIQUE (run_id, target_type, target_id),
            CHECK ((discrepancy_type = '{query_failure}') = (resolution_category = '{query_failed}')),
            CHECK ((discrepancy_type = '{query_failure}') = (broker_error_code IS NOT NULL))
        )
    """


def _create_repairs_table_sql() -> str:
    """Full current schema for the repair-attempt rows (Issue #104).

    Two-phase lifecycle: a row is INSERTed as ``status = 'pending'`` (the
    reservation, outcome columns NULL) and completed exactly once to
    ``status = 'finalized'``. The CHECKs mirror the model invariants as a
    second guard, split by status so a pending row cannot smuggle outcome
    data and a finalized row cannot miss it. ``mode`` admits only ``apply``
    (dry-run persists nothing) and ``would_apply`` is not a storable
    outcome. The composite FK pins every attempt to the exact discrepancy
    it acted on (the discrepancies table's UNIQUE (run_id, target_type,
    target_id) is the parent key)."""
    # ORDER-domain target subset (Issue #105): repair covers order states
    # only, and the released v2 DDL must stay unchanged (see the
    # discrepancies table).
    targets = _enum_values_sql(ORDER_TARGET_TYPES)
    states = _enum_values_sql(OrderState)
    codes = _enum_values_sql(BrokerErrorCode)
    failure_codes = _enum_values_sql(RepairFailureCode)  # closed set, generated
    storable_outcomes = "', '".join(
        sorted(o.value for o in RepairOutcome if o is not RepairOutcome.WOULD_APPLY)
    )
    apply_mode = RepairMode.APPLY.value
    applied = RepairOutcome.APPLIED.value
    already = RepairOutcome.ALREADY_APPLIED.value
    query_failed = RepairOutcome.QUERY_FAILED.value
    execution_target = ReconciliationTargetType.EXECUTION_ORDER.value
    pending = ATTEMPT_STATUS_PENDING
    finalized = ATTEMPT_STATUS_FINALIZED
    return f"""
        CREATE TABLE {REPAIRS_TABLE} (
            attempt_id TEXT PRIMARY KEY NOT NULL CHECK (trim(attempt_id) <> ''),
            run_id TEXT NOT NULL
                REFERENCES {RUNS_TABLE} (run_id)
                CHECK (trim(run_id) <> ''),
            mode TEXT NOT NULL CHECK (mode IN ('{apply_mode}')),
            status TEXT NOT NULL CHECK (status IN ('{pending}', '{finalized}')),
            target_type TEXT NOT NULL CHECK (target_type IN ('{targets}')),
            target_id TEXT NOT NULL CHECK (trim(target_id) <> ''),
            execution_request_id TEXT
                CHECK (execution_request_id IS NULL OR trim(execution_request_id) <> ''),
            client_order_id TEXT CHECK (client_order_id IS NULL OR trim(client_order_id) <> ''),
            reconciliation_internal_state TEXT
                CHECK (reconciliation_internal_state IS NULL
                       OR reconciliation_internal_state IN ('{states}')),
            reconciliation_broker_state TEXT
                CHECK (reconciliation_broker_state IS NULL
                       OR reconciliation_broker_state IN ('{states}')),
            internal_revision INTEGER
                CHECK (internal_revision IS NULL OR internal_revision >= 0),
            owner_id TEXT NOT NULL CHECK (trim(owner_id) <> ''),
            claim_execution_id TEXT NOT NULL CHECK (trim(claim_execution_id) <> ''),
            generation INTEGER NOT NULL CHECK (generation >= 1),
            takeover_id TEXT
                CHECK (takeover_id IS NULL OR trim(takeover_id) <> ''),
            lease_expires_at_utc TEXT NOT NULL CHECK (lease_expires_at_utc LIKE '%+00:00'),
            last_claimed_at_utc TEXT NOT NULL CHECK (last_claimed_at_utc LIKE '%+00:00'),
            internal_state_before TEXT
                CHECK (internal_state_before IS NULL OR internal_state_before IN ('{states}')),
            internal_state_after TEXT
                CHECK (internal_state_after IS NULL OR internal_state_after IN ('{states}')),
            outcome TEXT CHECK (outcome IS NULL OR outcome IN ('{storable_outcomes}')),
            broker_order_id TEXT CHECK (broker_order_id IS NULL OR trim(broker_order_id) <> ''),
            broker_error_code TEXT
                CHECK (broker_error_code IS NULL OR broker_error_code IN ('{codes}')),
            failure_code TEXT CHECK (failure_code IS NULL OR failure_code IN ('{failure_codes}')),
            detail TEXT NOT NULL CHECK (trim(detail) <> ''),
            observed_at_utc TEXT CHECK (observed_at_utc IS NULL OR observed_at_utc LIKE '%+00:00'),
            reserved_at_utc TEXT NOT NULL CHECK (reserved_at_utc LIKE '%+00:00'),
            broker_state_updated_at_utc TEXT
                CHECK (broker_state_updated_at_utc IS NULL
                       OR broker_state_updated_at_utc LIKE '%+00:00'),
            finalized_at_utc TEXT
                CHECK (finalized_at_utc IS NULL OR finalized_at_utc LIKE '%+00:00'),
            FOREIGN KEY (run_id, target_type, target_id)
                REFERENCES {DISCREPANCIES_TABLE} (run_id, target_type, target_id),
            CHECK ((target_type = '{execution_target}') = (execution_request_id IS NOT NULL)),
            CHECK ((status = '{pending}') = (outcome IS NULL)),
            CHECK ((status = '{finalized}') = (finalized_at_utc IS NOT NULL)),
            CHECK (status <> '{pending}'
                   OR (internal_state_before IS NULL AND internal_state_after IS NULL
                       AND broker_order_id IS NULL AND broker_error_code IS NULL
                       AND failure_code IS NULL AND broker_state_updated_at_utc IS NULL)),
            CHECK (status = '{pending}'
                   OR (outcome = '{applied}') = (broker_state_updated_at_utc IS NOT NULL)),
            CHECK (status = '{pending}'
                   OR (outcome IN ('{applied}', '{already}')) = (internal_state_after IS NOT NULL)),
            CHECK (status = '{pending}'
                   OR (outcome = '{query_failed}') = (broker_error_code IS NOT NULL)),
            CHECK (status = '{pending}'
                   OR (outcome IN ('{applied}', '{already}')) = (failure_code IS NULL)),
            CHECK ((generation = 1) = (takeover_id IS NULL)),
            FOREIGN KEY (takeover_id)
                REFERENCES {TAKEOVER_INTENTS_TABLE} (takeover_id)
        ) WITHOUT ROWID
    """


def _create_takeover_intents_table_sql() -> str:
    """DURABLE takeover intents (Issue #104 re-review r4).

    One row per takeover of one repair attempt, committed BEFORE the first
    cross-DB side effect. It is the SOURCE OF TRUTH for the takeover
    identity: every later step (execution-DB fence, claim promotion, attempt
    finalize) must carry exactly this ``takeover_id`` / ``target_owner_id``
    / ``target_claim_execution_id`` / ``target_generation``, and a restarted
    worker resumes THIS row instead of minting a new identity.

    Constraints enforce the protocol: ``target_generation`` is always
    ``source_generation + 1``, at most ONE active intent per attempt
    (partial UNIQUE index), at most one intent per (attempt, source
    generation) so a generation can never be taken over twice, a closed
    status set, a closed blocked-reason set, and status/timestamp coherence
    (a ``fence_installed`` row must carry ``fence_installed_at_utc`` and no
    later stamps, etc.). Rows are NEVER deleted -- completed and blocked
    intents are the audit trail of the identity chain."""
    statuses = _enum_values_sql(TakeoverIntentStatus)
    reasons = _enum_values_sql(TakeoverBlockedReason)
    cancel_reasons = _enum_values_sql(TakeoverCancelReason)
    prepared = TakeoverIntentStatus.PREPARED.value
    fence_installed = TakeoverIntentStatus.FENCE_INSTALLED.value
    claim_promoted = TakeoverIntentStatus.CLAIM_PROMOTED.value
    completed = TakeoverIntentStatus.COMPLETED.value
    cancelled = TakeoverIntentStatus.CANCELLED.value
    blocked = TakeoverIntentStatus.BLOCKED.value
    return f"""
        CREATE TABLE {TAKEOVER_INTENTS_TABLE} (
            takeover_id TEXT PRIMARY KEY NOT NULL CHECK (trim(takeover_id) <> ''),
            repair_attempt_id TEXT NOT NULL
                REFERENCES {REPAIRS_TABLE} (attempt_id)
                CHECK (trim(repair_attempt_id) <> ''),
            source_generation INTEGER NOT NULL CHECK (source_generation >= 1),
            target_generation INTEGER NOT NULL CHECK (target_generation >= 2),
            source_owner_id TEXT NOT NULL CHECK (trim(source_owner_id) <> ''),
            source_claim_execution_id TEXT NOT NULL
                CHECK (trim(source_claim_execution_id) <> ''),
            target_owner_id TEXT NOT NULL CHECK (trim(target_owner_id) <> ''),
            target_claim_execution_id TEXT NOT NULL
                CHECK (trim(target_claim_execution_id) <> ''),
            lease_expires_at_utc TEXT NOT NULL CHECK (lease_expires_at_utc LIKE '%+00:00'),
            status TEXT NOT NULL CHECK (status IN ('{statuses}')),
            created_at_utc TEXT NOT NULL CHECK (created_at_utc LIKE '%+00:00'),
            fence_installed_at_utc TEXT
                CHECK (fence_installed_at_utc IS NULL
                       OR fence_installed_at_utc LIKE '%+00:00'),
            claim_promoted_at_utc TEXT
                CHECK (claim_promoted_at_utc IS NULL OR claim_promoted_at_utc LIKE '%+00:00'),
            completed_at_utc TEXT
                CHECK (completed_at_utc IS NULL OR completed_at_utc LIKE '%+00:00'),
            cancelled_at_utc TEXT
                CHECK (cancelled_at_utc IS NULL OR cancelled_at_utc LIKE '%+00:00'),
            blocked_at_utc TEXT
                CHECK (blocked_at_utc IS NULL OR blocked_at_utc LIKE '%+00:00'),
            blocked_reason TEXT
                CHECK (blocked_reason IS NULL OR blocked_reason IN ('{reasons}')),
            cancelled_reason TEXT
                CHECK (cancelled_reason IS NULL OR cancelled_reason IN ('{cancel_reasons}')),
            observed_claim_generation INTEGER
                CHECK (observed_claim_generation IS NULL OR observed_claim_generation >= 1),
            observed_fence_generation INTEGER
                CHECK (observed_fence_generation IS NULL OR observed_fence_generation >= 1),
            observed_fence_identity TEXT
                CHECK (observed_fence_identity IS NULL OR trim(observed_fence_identity) <> ''),
            CHECK (target_generation = source_generation + 1),
            CHECK (target_claim_execution_id <> source_claim_execution_id),
            CHECK ((status = '{blocked}') = (blocked_at_utc IS NOT NULL)),
            CHECK ((status = '{blocked}') = (blocked_reason IS NOT NULL)),
            CHECK ((status = '{completed}') = (completed_at_utc IS NOT NULL)),
            CHECK ((status = '{cancelled}') = (cancelled_at_utc IS NOT NULL)),
            CHECK ((status = '{cancelled}') = (cancelled_reason IS NOT NULL)),
            CHECK (status = '{blocked}'
                   OR (observed_claim_generation IS NULL
                       AND observed_fence_generation IS NULL
                       AND observed_fence_identity IS NULL)),
            CHECK (status <> '{prepared}'
                   OR (fence_installed_at_utc IS NULL AND claim_promoted_at_utc IS NULL)),
            CHECK (status <> '{fence_installed}'
                   OR (fence_installed_at_utc IS NOT NULL AND claim_promoted_at_utc IS NULL)),
            CHECK (status NOT IN ('{claim_promoted}', '{completed}')
                   OR (fence_installed_at_utc IS NOT NULL
                       AND claim_promoted_at_utc IS NOT NULL))
        ) WITHOUT ROWID
    """


def _create_position_discrepancies_table_sql() -> str:
    """Position-domain discrepancy rows (Issue #105): position / trade /
    transaction-evidenced close / stop loss / take profit detections.

    A SEPARATE table on purpose: the Issue #103 order-discrepancy table is
    OrderState-typed and its DDL is frozen (see ``_create_discrepancies_table_sql``),
    while these records carry rendered observation values. The CHECKs mirror
    the ``PositionDiscrepancy`` invariants; ``resolution_category`` admits
    ONLY ``manual_review`` — the DB itself refuses to store a position-domain
    discrepancy as an auto-repair candidate (Issue #105 fail-safe), and the
    Issue #104 repair service never reads this table. Uniqueness is per
    (run, kind, target): one target may legitimately carry several kinds in
    one run (an SL and a TP finding on the same order)."""
    types = _enum_values_sql(POSITION_DISCREPANCY_TYPES)
    targets = _enum_values_sql(POSITION_TARGET_TYPES)
    manual_review = ResolutionCategory.MANUAL_REVIEW.value
    execution_target = ReconciliationTargetType.EXECUTION_ORDER.value
    return f"""
        CREATE TABLE {POSITION_DISCREPANCIES_TABLE} (
            discrepancy_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL
                REFERENCES {RUNS_TABLE} (run_id)
                CHECK (trim(run_id) <> ''),
            discrepancy_type TEXT NOT NULL CHECK (discrepancy_type IN ('{types}')),
            target_type TEXT NOT NULL CHECK (target_type IN ('{targets}')),
            target_id TEXT NOT NULL CHECK (trim(target_id) <> ''),
            resolution_category TEXT NOT NULL CHECK (resolution_category = '{manual_review}'),
            symbol TEXT NOT NULL CHECK (trim(symbol) <> ''),
            internal_value TEXT CHECK (internal_value IS NULL OR trim(internal_value) <> ''),
            broker_value TEXT CHECK (broker_value IS NULL OR trim(broker_value) <> ''),
            execution_request_id TEXT
                CHECK (execution_request_id IS NULL OR trim(execution_request_id) <> ''),
            client_order_id TEXT CHECK (client_order_id IS NULL OR trim(client_order_id) <> ''),
            broker_trade_id TEXT CHECK (broker_trade_id IS NULL OR trim(broker_trade_id) <> ''),
            protection_order_id TEXT
                CHECK (protection_order_id IS NULL OR trim(protection_order_id) <> ''),
            broker_transaction_ids_json TEXT
                CHECK (broker_transaction_ids_json IS NULL
                       OR trim(broker_transaction_ids_json) <> ''),
            detail TEXT NOT NULL CHECK (trim(detail) <> ''),
            observed_at_utc TEXT NOT NULL CHECK (observed_at_utc LIKE '%+00:00'),
            UNIQUE (run_id, discrepancy_type, target_type, target_id),
            CHECK ((target_type = '{execution_target}') = (execution_request_id IS NOT NULL))
        )
    """


_DISCREPANCIES_RUN_INDEX = f"idx_{DISCREPANCIES_TABLE}_run"
_REPAIRS_RUN_INDEX = f"idx_{REPAIRS_TABLE}_run"
_REPAIRS_RUN_INDEX_SQL = (
    f"CREATE INDEX {_REPAIRS_RUN_INDEX} ON {REPAIRS_TABLE} (run_id, status, reserved_at_utc, attempt_id)"
)
# At most ONE active intent per attempt -- enforced by the DB, not by a
# read-then-write race: two workers preparing a takeover concurrently means
# exactly one INSERT survives and the loser must load the winner's intent.
_INTENT_ACTIVE_INDEX = f"idx_{TAKEOVER_INTENTS_TABLE}_active"
_INTENT_ACTIVE_INDEX_SQL = (
    f"CREATE UNIQUE INDEX {_INTENT_ACTIVE_INDEX} ON {TAKEOVER_INTENTS_TABLE} "
    "(repair_attempt_id) WHERE status IN ("
    + ", ".join(f"'{status.value}'" for status in sorted(ACTIVE_INTENT_STATUSES, key=lambda s: s.value))
    + ")"
)
# A (attempt, source generation) may be taken over AT MOST ONCE: at most one
# non-blocked intent exists per pair. BLOCKED rows are excluded because a
# blocked row is a DIAGNOSIS, not a takeover -- it must be able to coexist
# with the terminal intent it diagnoses (r5 review: a terminal intent plus a
# still-pending attempt has to be recordable).
_INTENT_GENERATION_INDEX = f"idx_{TAKEOVER_INTENTS_TABLE}_generation"
_INTENT_GENERATION_INDEX_SQL = (
    f"CREATE UNIQUE INDEX {_INTENT_GENERATION_INDEX} ON {TAKEOVER_INTENTS_TABLE} "
    f"(repair_attempt_id, source_generation) WHERE status <> "
    f"'{TakeoverIntentStatus.BLOCKED.value}'"
)
# ... and at most ONE blocked diagnosis per pair, so repeated recovery of an
# unresolvable state reports the same row instead of piling up new ones.
_INTENT_BLOCKED_INDEX = f"idx_{TAKEOVER_INTENTS_TABLE}_blocked"
_INTENT_BLOCKED_INDEX_SQL = (
    f"CREATE UNIQUE INDEX {_INTENT_BLOCKED_INDEX} ON {TAKEOVER_INTENTS_TABLE} "
    f"(repair_attempt_id, source_generation) WHERE status = "
    f"'{TakeoverIntentStatus.BLOCKED.value}'"
)
# Ordered lookup of an attempt's full intent history (audit + restart).
_INTENT_ATTEMPT_INDEX = f"idx_{TAKEOVER_INTENTS_TABLE}_attempt"
_INTENT_ATTEMPT_INDEX_SQL = (
    f"CREATE INDEX {_INTENT_ATTEMPT_INDEX} ON {TAKEOVER_INTENTS_TABLE} "
    "(repair_attempt_id, source_generation, takeover_id)"
)


def _create_index_sql() -> str:
    return (
        f"CREATE INDEX {_DISCREPANCIES_RUN_INDEX} ON {DISCREPANCIES_TABLE} "
        "(run_id, discrepancy_id)"
    )


_POSITION_DISCREPANCIES_RUN_INDEX = f"idx_{POSITION_DISCREPANCIES_TABLE}_run"
_POSITION_DISCREPANCIES_RUN_INDEX_SQL = (
    f"CREATE INDEX {_POSITION_DISCREPANCIES_RUN_INDEX} ON {POSITION_DISCREPANCIES_TABLE} "
    "(run_id, discrepancy_id)"
)


def _tables_for(version: int) -> tuple[str, ...]:
    tables = [RUNS_TABLE, DISCREPANCIES_TABLE]
    if version >= 2:
        tables.extend((REPAIRS_TABLE, TAKEOVER_INTENTS_TABLE))
    if version >= 3:
        tables.append(POSITION_DISCREPANCIES_TABLE)
    return tuple(tables)


def _create_schema(connection: sqlite3.Connection, *, version: int = SCHEMA_VERSION) -> None:
    """Create the schema for ``version`` (v1/v2 are used only as in-memory
    migration references and by migration tests; real DBs are created at the
    current version)."""
    connection.execute(_create_runs_table_sql(version=version))
    connection.execute(_create_discrepancies_table_sql(version=version))
    connection.execute(_create_index_sql())
    if version >= 2:
        connection.execute(_create_repairs_table_sql())
        connection.execute(_REPAIRS_RUN_INDEX_SQL)
        connection.execute(_create_takeover_intents_table_sql())
        connection.execute(_INTENT_ACTIVE_INDEX_SQL)
        connection.execute(_INTENT_GENERATION_INDEX_SQL)
        connection.execute(_INTENT_BLOCKED_INDEX_SQL)
        connection.execute(_INTENT_ATTEMPT_INDEX_SQL)
    if version >= 3:
        connection.execute(_create_position_discrepancies_table_sql())
        connection.execute(_POSITION_DISCREPANCIES_RUN_INDEX_SQL)
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
        for migratable in _MIGRATABLE_VERSIONS:
            if not _schema_problems(connection, version=migratable):
                problems.append(
                    f"the DB is a valid v{migratable} schema; run "
                    f"init_reconciliation_db() to migrate it to v{SCHEMA_VERSION} "
                    "(reads never migrate)"
                )
                break
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
    transaction (Issue #104): additive ``ALTER TABLE ... ADD COLUMN
    internal_revision`` on the discrepancies table (SQLite inserts the
    column at the end of the column list, before the table constraints --
    exactly where the v2 reference CREATE declares it, so the migrated DDL
    token-normalizes identically; existing rows read back with
    ``internal_revision = NULL`` and are never auto-repaired), plus the
    repair-attempts table, its index and the version bump. Existing rows
    are untouched; nothing is deleted or rewritten. The caller re-verifies
    the complete v2 schema before COMMIT; any failure rolls back to the
    untouched v1 file."""
    connection.execute(
        f"ALTER TABLE {DISCREPANCIES_TABLE} ADD COLUMN {_DISCREPANCY_REVISION_COLUMN_DDL}"
    )
    connection.execute(_create_repairs_table_sql())
    connection.execute(_REPAIRS_RUN_INDEX_SQL)
    connection.execute(_create_takeover_intents_table_sql())
    connection.execute(_INTENT_ACTIVE_INDEX_SQL)
    connection.execute(_INTENT_GENERATION_INDEX_SQL)
    connection.execute(_INTENT_BLOCKED_INDEX_SQL)
    connection.execute(_INTENT_ATTEMPT_INDEX_SQL)
    connection.execute("PRAGMA user_version = 2")
    _LOGGER.info(
        "migrated broker reconciliation DB %s from schema v1 to v2 (added "
        "discrepancies.internal_revision, repair attempts table %s and its "
        "durable takeover intents)",
        db_path,
        REPAIRS_TABLE,
    )


def _migrate_v2_to_v3(connection: sqlite3.Connection, db_path: Path) -> None:
    """Migrate a fully verified v2 schema to v3 inside the caller's
    transaction (Issue #105): additive ``ALTER TABLE ... ADD COLUMN`` for
    the position-domain summary columns on the runs table (existing rows
    read back all-NULL = "the position domain was not reconciled"), plus the
    position-discrepancies table, its index and the version bump. Existing
    rows are untouched; nothing is deleted or rewritten. The caller
    re-verifies the complete v3 schema before COMMIT; any failure rolls back
    to the untouched v2 file."""
    for column_ddl in _RUN_POSITION_COLUMN_DDLS:
        connection.execute(f"ALTER TABLE {RUNS_TABLE} ADD COLUMN {column_ddl}")
    connection.execute(_create_position_discrepancies_table_sql())
    connection.execute(_POSITION_DISCREPANCIES_RUN_INDEX_SQL)
    connection.execute("PRAGMA user_version = 3")
    _LOGGER.info(
        "migrated broker reconciliation DB %s from schema v2 to v3 (added the "
        "position-domain run summary columns and table %s)",
        db_path,
        POSITION_DISCREPANCIES_TABLE,
    )


def init_reconciliation_db(db_path: Path | None = None) -> None:
    """Create the current schema, verify an existing one, or migrate a valid
    released schema (v1 per Issue #104, v2 per Issue #105) in place;
    idempotent.

    Same pristine rule as ``execution_store.init_db`` /
    ``SQLiteSafetyDecisionStore.initialize``: creation happens only when
    ``sqlite_master`` is empty AND ``user_version`` is 0. A DB that fully
    matches the CURRENT schema passes verification unchanged. A DB that
    fully matches EXACTLY one released prior schema -- and ONLY such a DB --
    is migrated in place, stepwise (:func:`_migrate_v1_to_v2` then
    :func:`_migrate_v2_to_v3`); the migrations, the version bumps and a full
    current-schema re-verification commit in ONE transaction, so a failed
    migration leaves no half-migrated schema behind. Any other content fails
    with :class:`ReconciliationSchemaError` WITHOUT modifying the file (no
    auto-completion, no silent rebuild).
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
                    if not _schema_problems(connection, version=2):
                        _migrate_v2_to_v3(connection, db_path)
                    elif not _schema_problems(connection, version=1):
                        _migrate_v1_to_v2(connection, db_path)
                        _migrate_v2_to_v3(connection, db_path)
                    else:
                        # neither a valid current nor a valid released prior
                        # schema: fail with the full diagnosis, DB untouched
                        _verify_schema(connection, db_path)
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
    summary = run.position_summary
    run_record = (
        run.run_id,
        run.mode.value,
        _iso(run.started_at_utc),
        _iso(run.finished_at_utc),
        run.checked_order_count,
        run.broker_listing_error,
        None if summary is None else summary.checked_position_count,
        None if summary is None else summary.checked_trade_count,
        None if summary is None else summary.checked_closed_trade_count,
        None if summary is None else summary.internal_exposure_error,
        None if summary is None else summary.position_listing_error,
        None if summary is None else summary.trade_listing_error,
        None if summary is None else summary.closed_trade_listing_error,
        None if summary is None else _encode_string_tuple(summary.excluded_symbols),
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
                        discrepancy.internal_revision,
                    ),
                )
            for discrepancy in run.position_discrepancies:
                connection.execute(
                    f"INSERT INTO {POSITION_DISCREPANCIES_TABLE} "
                    f"({', '.join(_POSITION_DISCREPANCY_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _POSITION_DISCREPANCY_COLUMNS)})",
                    (
                        run.run_id,
                        discrepancy.discrepancy_type.value,
                        discrepancy.target_type.value,
                        discrepancy.target_id,
                        discrepancy.resolution_category.value,
                        discrepancy.symbol,
                        discrepancy.internal_value,
                        discrepancy.broker_value,
                        discrepancy.execution_request_id,
                        discrepancy.client_order_id,
                        discrepancy.broker_trade_id,
                        discrepancy.protection_order_id,
                        _encode_string_tuple(discrepancy.broker_transaction_ids),
                        discrepancy.detail,
                        _iso(discrepancy.observed_at_utc),
                    ),
                )
        except sqlite3.IntegrityError as exc:  # UNIQUE / CHECK / FK violation
            raise ReconciliationStoreError(
                f"recording reconciliation run {run.run_id!r} violates a DB constraint: {exc}"
            ) from exc


# --- read paths ---------------------------------------------------------------


def _encode_string_tuple(values: tuple[str, ...]) -> str | None:
    """Compact ASCII JSON array for a tuple of ids; empty -> NULL (the
    columns carry a non-blank CHECK, and NULL keeps "nothing to store"
    distinguishable without an empty-array sentinel)."""
    if not values:
        return None
    return json.dumps(list(values), ensure_ascii=True, separators=(",", ":"))


def _decode_string_tuple(text: str | None, context: str) -> tuple[str, ...]:
    """Inverse of :func:`_encode_string_tuple`; anything but NULL or a JSON
    array of non-empty strings fails restore (fail-closed, like every other
    stored value)."""
    if text is None:
        return ()
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise ReconciliationIntegrityError(f"{context} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) and item.strip() for item in parsed
    ):
        raise ReconciliationIntegrityError(
            f"{context} must be a JSON array of non-empty strings, got {text!r}"
        )
    return tuple(parsed)


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
            internal_revision=row["internal_revision"],
        )
    except (ValueError, TypeError) as exc:
        raise ReconciliationIntegrityError(
            f"discrepancy row {row['discrepancy_id']} of reconciliation run {run_id!r} "
            f"failed restore-time validation: {exc}"
        ) from exc


def _restore_position_discrepancy(row: sqlite3.Row, run_id: str) -> PositionDiscrepancy:
    """Rebuild one position-domain discrepancy (Issue #105); the model
    re-runs every invariant, so an out-of-band edited row fails loudly."""
    try:
        return PositionDiscrepancy(
            discrepancy_type=DiscrepancyType(row["discrepancy_type"]),
            target_type=ReconciliationTargetType(row["target_type"]),
            target_id=row["target_id"],
            resolution_category=ResolutionCategory(row["resolution_category"]),
            observed_at_utc=_parse_utc(row["observed_at_utc"]),
            detail=row["detail"],
            symbol=row["symbol"],
            internal_value=row["internal_value"],
            broker_value=row["broker_value"],
            execution_request_id=row["execution_request_id"],
            client_order_id=row["client_order_id"],
            broker_trade_id=row["broker_trade_id"],
            protection_order_id=row["protection_order_id"],
            broker_transaction_ids=_decode_string_tuple(
                row["broker_transaction_ids_json"],
                f"position discrepancy row {row['discrepancy_id']} broker_transaction_ids",
            ),
        )
    except (ValueError, TypeError) as exc:
        raise ReconciliationIntegrityError(
            f"position discrepancy row {row['discrepancy_id']} of reconciliation run "
            f"{run_id!r} failed restore-time validation: {exc}"
        ) from exc


def _restore_position_summary(
    row: sqlite3.Row, run_id: str
) -> PositionReconciliationSummary | None:
    """Rebuild the run's position-domain summary (Issue #105).

    All summary columns NULL -> None ("the position domain was not
    reconciled": order-only runs and rows stored before schema v3). Any
    other NULL mix is corruption and fails restore -- coherence cannot live
    in a table CHECK because the columns arrive via ``ALTER TABLE`` on
    migrated DBs."""
    values = {
        name: row[name]
        for name in (
            "checked_position_count",
            "checked_trade_count",
            "checked_closed_trade_count",
            "internal_exposure_error",
            "position_listing_error",
            "trade_listing_error",
            "closed_trade_listing_error",
            "excluded_symbols_json",
        )
    }
    if all(value is None for value in values.values()):
        return None
    counts = (
        values["checked_position_count"],
        values["checked_trade_count"],
        values["checked_closed_trade_count"],
    )
    if any(count is None for count in counts):
        raise ReconciliationIntegrityError(
            f"reconciliation run {run_id!r} carries a partially NULL position summary "
            f"({values!r}); a run either reconciled the position domain (all three "
            "checked counts present) or did not (all summary columns NULL)"
        )
    try:
        return PositionReconciliationSummary(
            checked_position_count=values["checked_position_count"],
            checked_trade_count=values["checked_trade_count"],
            checked_closed_trade_count=values["checked_closed_trade_count"],
            internal_exposure_error=values["internal_exposure_error"],
            position_listing_error=values["position_listing_error"],
            trade_listing_error=values["trade_listing_error"],
            closed_trade_listing_error=values["closed_trade_listing_error"],
            excluded_symbols=_decode_string_tuple(
                values["excluded_symbols_json"],
                f"reconciliation run {run_id!r} excluded_symbols",
            ),
        )
    except (ValueError, TypeError) as exc:
        raise ReconciliationIntegrityError(
            f"reconciliation run {run_id!r} position summary failed restore-time "
            f"validation: {exc}"
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
    position_discrepancies = tuple(
        _restore_position_discrepancy(entry, run_id)
        for entry in connection.execute(
            f"SELECT * FROM {POSITION_DISCREPANCIES_TABLE} WHERE run_id = ? "
            "ORDER BY discrepancy_id ASC",
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
            position_discrepancies=position_discrepancies,
            position_summary=_restore_position_summary(row, run_id),
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


# --- repair attempts: two-phase lifecycle (Issue #104) ------------------------


def _require_existing_db(db_path: Path | None) -> Path:
    db_path = Path(db_path) if db_path is not None else reconciliation_db_path()
    if not db_path.exists():
        raise ReconciliationStoreError(
            f"broker reconciliation DB at {db_path} does not exist; call "
            "init_reconciliation_db() first"
        )
    return db_path


def reserve_repair_attempt(
    reservation: RepairAttemptReservation, *, db_path: Path | None = None
) -> None:
    """Persist the PENDING phase of one repair attempt (Issue #104).

    Called BEFORE the attempt is evaluated or anything is written to the
    execution order store, so the attempt id and its run / discrepancy
    identity survive a crash at any later point. A missing DB file is a
    failure, not a trigger for implicit creation. The run AND the exact
    discrepancy the attempt acts on must already be recorded (composite
    FK; also pre-checked for a readable error). A reused ``attempt_id``
    raises :class:`RepairAttemptConflictError` and changes nothing --
    BEFORE any state update could have happened.
    """
    if not isinstance(reservation, RepairAttemptReservation):
        raise ReconciliationStoreError(
            f"reservation must be a RepairAttemptReservation, got {reservation!r}"
        )
    db_path = _require_existing_db(db_path)
    record = (
        reservation.attempt_id,
        reservation.run_id,
        RepairMode.APPLY.value,
        ATTEMPT_STATUS_PENDING,
        reservation.target_type.value,
        reservation.target_id,
        reservation.execution_request_id,
        reservation.client_order_id,
        None
        if reservation.reconciliation_internal_state is None
        else reservation.reconciliation_internal_state.value,
        None
        if reservation.reconciliation_broker_state is None
        else reservation.reconciliation_broker_state.value,
        reservation.internal_revision,
        reservation.detail,
        None if reservation.observed_at_utc is None else _iso(reservation.observed_at_utc),
        _iso(reservation.reserved_at_utc),
        reservation.owner_id,
        reservation.claim_execution_id,
        reservation.generation,
        reservation.takeover_id,
        _iso(reservation.lease_expires_at_utc),
        _iso(reservation.last_claimed_at_utc),
    )
    with _verified_txn(db_path) as connection:
        existing = connection.execute(
            f"SELECT attempt_id FROM {REPAIRS_TABLE} WHERE attempt_id = ?",
            (reservation.attempt_id,),
        ).fetchone()
        if existing is not None:
            raise RepairAttemptConflictError(
                f"repair attempt {reservation.attempt_id!r} is already reserved or "
                "finalized; attempts are appended under a new attempt_id, never reused"
            )
        discrepancy_exists = connection.execute(
            f"SELECT target_id FROM {DISCREPANCIES_TABLE} "
            "WHERE run_id = ? AND target_type = ? AND target_id = ?",
            (reservation.run_id, reservation.target_type.value, reservation.target_id),
        ).fetchone()
        if discrepancy_exists is None:
            raise ReconciliationStoreError(
                f"repair attempt {reservation.attempt_id!r} refers to discrepancy "
                f"({reservation.run_id!r}, {reservation.target_type.value!r}, "
                f"{reservation.target_id!r}), which is not recorded in this DB; repairs "
                "act on recorded observations only"
            )
        try:
            connection.execute(
                f"INSERT INTO {REPAIRS_TABLE} ({', '.join(_RESERVATION_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _RESERVATION_COLUMNS)})",
                record,
            )
        except sqlite3.IntegrityError as exc:  # UNIQUE / CHECK / FK violation
            raise ReconciliationStoreError(
                f"reserving repair attempt {reservation.attempt_id!r} violates a DB "
                f"constraint: {exc}"
            ) from exc


def _restore_intent(row: sqlite3.Row) -> RepairTakeoverIntent:
    """Rebuild an intent, fail-closed on unknown status / blocked reason."""
    try:
        status = TakeoverIntentStatus(row["status"])
    except ValueError as exc:
        raise ReconciliationIntegrityError(
            f"takeover intent {row['takeover_id']!r} carries unknown status "
            f"{row['status']!r}; refusing to restore it"
        ) from exc
    reason = row["blocked_reason"]
    if reason is not None:
        try:
            reason = TakeoverBlockedReason(reason)
        except ValueError as exc:
            raise ReconciliationIntegrityError(
                f"takeover intent {row['takeover_id']!r} carries unknown blocked reason "
                f"{row['blocked_reason']!r}; refusing to restore it"
            ) from exc
    cancel_reason = row["cancelled_reason"]
    if cancel_reason is not None:
        try:
            cancel_reason = TakeoverCancelReason(cancel_reason)
        except ValueError as exc:
            raise ReconciliationIntegrityError(
                f"takeover intent {row['takeover_id']!r} carries unknown cancellation "
                f"reason {row['cancelled_reason']!r}; refusing to restore it"
            ) from exc
    try:
        return RepairTakeoverIntent(
            takeover_id=row["takeover_id"],
            repair_attempt_id=row["repair_attempt_id"],
            source_generation=row["source_generation"],
            target_generation=row["target_generation"],
            source_owner_id=row["source_owner_id"],
            source_claim_execution_id=row["source_claim_execution_id"],
            target_owner_id=row["target_owner_id"],
            target_claim_execution_id=row["target_claim_execution_id"],
            lease_expires_at_utc=_parse_utc(row["lease_expires_at_utc"]),
            status=status,
            created_at_utc=_parse_utc(row["created_at_utc"]),
            fence_installed_at_utc=_parse_optional_utc(row["fence_installed_at_utc"]),
            claim_promoted_at_utc=_parse_optional_utc(row["claim_promoted_at_utc"]),
            completed_at_utc=_parse_optional_utc(row["completed_at_utc"]),
            cancelled_at_utc=_parse_optional_utc(row["cancelled_at_utc"]),
            blocked_at_utc=_parse_optional_utc(row["blocked_at_utc"]),
            blocked_reason=reason,
            cancelled_reason=cancel_reason,
            observed_claim_generation=row["observed_claim_generation"],
            observed_fence_generation=row["observed_fence_generation"],
            observed_fence_identity=row["observed_fence_identity"],
        )
    except ValueError as exc:
        raise ReconciliationIntegrityError(
            f"stored takeover intent {row['takeover_id']!r} fails validation: {exc}"
        ) from exc


def _intent_record(intent: RepairTakeoverIntent) -> tuple:
    return (
        intent.takeover_id,
        intent.repair_attempt_id,
        intent.source_generation,
        intent.target_generation,
        intent.source_owner_id,
        intent.source_claim_execution_id,
        intent.target_owner_id,
        intent.target_claim_execution_id,
        _iso(intent.lease_expires_at_utc),
        intent.status.value,
        _iso(intent.created_at_utc),
        _iso_optional(intent.fence_installed_at_utc),
        _iso_optional(intent.claim_promoted_at_utc),
        _iso_optional(intent.completed_at_utc),
        _iso_optional(intent.cancelled_at_utc),
        _iso_optional(intent.blocked_at_utc),
        None if intent.blocked_reason is None else intent.blocked_reason.value,
        None if intent.cancelled_reason is None else intent.cancelled_reason.value,
        intent.observed_claim_generation,
        intent.observed_fence_generation,
        intent.observed_fence_identity,
    )


def _insert_intent(connection: sqlite3.Connection, intent: RepairTakeoverIntent) -> None:
    try:
        connection.execute(
            f"INSERT INTO {TAKEOVER_INTENTS_TABLE} ({', '.join(_INTENT_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _INTENT_COLUMNS)})",
            _intent_record(intent),
        )
    except sqlite3.IntegrityError as exc:
        # UNIQUE(takeover_id) / UNIQUE(attempt, source_generation) / the
        # partial UNIQUE active index / FK / CHECK -- all mean "this intent
        # may not exist", never "overwrite what is there".
        raise TakeoverIntentConflictError(
            f"takeover intent {intent.takeover_id!r} for repair attempt "
            f"{intent.repair_attempt_id!r} (generation {intent.source_generation} -> "
            f"{intent.target_generation}) violates a DB constraint and was not created: "
            f"{type(exc).__name__}"
        ) from exc


def _active_intent_row(connection: sqlite3.Connection, attempt_id: str):
    """The attempt's single ACTIVE intent row, or None -- fail-closed.

    Every intent row of the attempt is restored (so an unknown status or
    blocked reason raises instead of being filtered away as "not active"),
    and more than one active intent -- which the partial UNIQUE index makes
    impossible in-band -- is refused rather than resolved by guessing."""
    rows = connection.execute(
        f"SELECT * FROM {TAKEOVER_INTENTS_TABLE} WHERE repair_attempt_id = ? "
        "ORDER BY source_generation, takeover_id",
        (attempt_id,),
    ).fetchall()
    active = [row for row in rows if _restore_intent(row).is_active]
    if len(active) > 1:
        raise ReconciliationIntegrityError(
            f"repair attempt {attempt_id!r} has {len(active)} ACTIVE takeover intents "
            f"({', '.join(sorted(row['takeover_id'] for row in active))}); at most one is "
            "permitted and the protocol refuses to guess which is real"
        )
    return active[0] if active else None


def create_takeover_intent(
    intent: RepairTakeoverIntent,
    *,
    observed_at_utc: datetime,
    db_path: Path | None = None,
) -> RepairTakeoverIntent:
    """Durably record the intent to take over an EXPIRED claim (r4 review).

    This is step 1 of the cross-DB takeover protocol and the ONLY place a
    takeover identity is minted. Everything is checked inside one
    transaction, against the CURRENT claim:

    - the attempt exists and is still ``pending`` (a finalized attempt can
      never get an active intent);
    - its lease has EXPIRED relative to ``observed_at_utc`` (the caller's
      clock is the contract; the atomicity is this transaction's);
    - ``source_generation`` / ``source_owner_id`` /
      ``source_claim_execution_id`` match the claim being replaced;
    - no other ACTIVE intent exists (partial UNIQUE index), and this
      (attempt, source generation) was never taken over before.

    A loser of the race gets :class:`TakeoverIntentConflictError` and must
    LOAD the winner's active intent (``get_active_takeover_intent``) and
    continue it -- never mint a second identity.
    """
    if not isinstance(intent, RepairTakeoverIntent):
        raise ReconciliationStoreError(
            f"intent must be a RepairTakeoverIntent, got {intent!r}"
        )
    if intent.status is not TakeoverIntentStatus.PREPARED:
        raise ReconciliationStoreError(
            f"a new takeover intent must be created as "
            f"{TakeoverIntentStatus.PREPARED.value!r}, got {intent.status.value!r}"
        )
    _require_utc_argument(observed_at_utc, "observed_at_utc")
    db_path = _require_existing_db(db_path)
    with _verified_txn(db_path) as connection:
        row = _claim_row_for_intent(connection, intent)
        lease = _parse_utc(row["lease_expires_at_utc"])
        if lease > observed_at_utc:
            raise TakeoverIntentConflictError(
                f"repair attempt {intent.repair_attempt_id!r} is claimed until "
                f"{lease.isoformat()} (observed {observed_at_utc.isoformat()}); a valid "
                "lease may never be taken over"
            )
        active = _active_intent_row(connection, intent.repair_attempt_id)
        if active is not None:
            raise TakeoverIntentConflictError(
                f"repair attempt {intent.repair_attempt_id!r} already has the active "
                f"takeover intent {active['takeover_id']!r} (status {active['status']!r}); "
                "load and continue it instead of creating another"
            )
        blocked = connection.execute(
            f"SELECT takeover_id FROM {TAKEOVER_INTENTS_TABLE} WHERE repair_attempt_id = ? "
            "AND source_generation = ? AND status = ?",
            (
                intent.repair_attempt_id,
                intent.source_generation,
                TakeoverIntentStatus.BLOCKED.value,
            ),
        ).fetchone()
        if blocked is not None:
            # a blocked diagnosis is a HOLD for manual intervention: the same
            # takeover is never started again automatically (r5 review)
            raise TakeoverIntentConflictError(
                f"repair attempt {intent.repair_attempt_id!r} has the BLOCKED takeover "
                f"intent {blocked['takeover_id']!r} for generation "
                f"{intent.source_generation}; it needs manual intervention and is never "
                "retried automatically"
            )
        _insert_intent(connection, intent)
        return intent


def _claim_row_for_intent(connection: sqlite3.Connection, intent: RepairTakeoverIntent):
    """The attempt row an intent acts on, with its source identity verified."""
    row = connection.execute(
        f"SELECT * FROM {REPAIRS_TABLE} WHERE attempt_id = ?", (intent.repair_attempt_id,)
    ).fetchone()
    if row is None:
        raise ReconciliationStoreError(
            f"repair attempt {intent.repair_attempt_id!r} has no reservation; there is "
            "nothing to take over"
        )
    if row["status"] != ATTEMPT_STATUS_PENDING:
        raise RepairAttemptConflictError(
            f"repair attempt {intent.repair_attempt_id!r} is already finalized "
            f"(outcome {row['outcome']!r}); there is no claim to take over"
        )
    if (
        row["generation"] != intent.source_generation
        or row["owner_id"] != intent.source_owner_id
        or row["claim_execution_id"] != intent.source_claim_execution_id
    ):
        raise TakeoverIntentConflictError(
            f"repair attempt {intent.repair_attempt_id!r} is claimed at generation "
            f"{row['generation']} by claim {row['claim_execution_id']!r}, not at the "
            f"intent's source generation {intent.source_generation} / claim "
            f"{intent.source_claim_execution_id!r}; the intent's preconditions no longer hold"
        )
    return row


def insert_blocked_takeover_intent(
    intent: RepairTakeoverIntent, *, db_path: Path | None = None
) -> RepairTakeoverIntent:
    """Durably park an UNRESOLVABLE takeover as a ``blocked`` intent.

    Used when the protocol finds a state it must not resolve automatically
    and there is no active intent to block (e.g. an execution fence ahead of
    every known intent). The row carries the reason code and the observed
    generations / fence identity, so an operator can see exactly what
    stopped it. It occupies the (attempt, source generation) slot, which is
    what prevents a later pass from silently starting the same doomed
    takeover again (and from returning an empty "nothing to do").
    """
    if not isinstance(intent, RepairTakeoverIntent):
        raise ReconciliationStoreError(f"intent must be a RepairTakeoverIntent, got {intent!r}")
    if intent.status is not TakeoverIntentStatus.BLOCKED:
        raise ReconciliationStoreError(
            f"insert_blocked_takeover_intent requires status "
            f"{TakeoverIntentStatus.BLOCKED.value!r}, got {intent.status.value!r}"
        )
    db_path = _require_existing_db(db_path)
    with _verified_txn(db_path) as connection:
        attempt = connection.execute(
            f"SELECT attempt_id FROM {REPAIRS_TABLE} WHERE attempt_id = ?",
            (intent.repair_attempt_id,),
        ).fetchone()
        if attempt is None:
            raise ReconciliationStoreError(
                f"repair attempt {intent.repair_attempt_id!r} has no reservation; a blocked "
                "takeover intent must reference a real attempt"
            )
        _insert_intent(connection, intent)
        return intent


def _attempt_row_for_intent(connection: sqlite3.Connection, intent: RepairTakeoverIntent):
    """The repair attempt an intent acts on, or a typed protocol failure."""
    row = connection.execute(
        f"SELECT * FROM {REPAIRS_TABLE} WHERE attempt_id = ?", (intent.repair_attempt_id,)
    ).fetchone()
    if row is None:
        raise TakeoverProtocolInvariantError(
            f"takeover intent {intent.takeover_id!r} names repair attempt "
            f"{intent.repair_attempt_id!r}, which does not exist"
        )
    return row


def _require_claim_matches_intent(row: sqlite3.Row, intent: RepairTakeoverIntent) -> None:
    """The attempt's claim must BE this intent's target identity.

    This is the substance behind ``claim_promoted`` / ``completed``: the
    status may only move once the CLAIM ITSELF was moved onto the intent's
    identity, verified in the SAME transaction as the status update.

    IDENTITY only -- the lease is liveness, not identity: a resumed intent
    legitimately carries a lease later than the one stamped on the claim
    when it was promoted, and that must not look like a foreign holder."""
    mismatched = sorted(
        column
        for column, expected in (
            ("generation", intent.target_generation),
            ("takeover_id", intent.takeover_id),
            ("owner_id", intent.target_owner_id),
            ("claim_execution_id", intent.target_claim_execution_id),
        )
        if row[column] != expected
    )
    if mismatched:
        raise TakeoverIdentityMismatchError(
            f"repair attempt {intent.repair_attempt_id!r} is not held by takeover intent "
            f"{intent.takeover_id!r} (differing: {', '.join(mismatched)}); the intent's "
            "status may not move ahead of the claim it describes"
        )


def _order_of_attempt(intent: RepairTakeoverIntent, db_path: Path) -> str | None:
    """The execution order this attempt acts on, read WITHOUT holding a lock.

    The guard names an order, so the order must be known before it can be
    taken. This read goes through a TRUE read-only connection and is fully
    finished before the guard is acquired, so **no reconciliation lock is
    ever held while the order store is locked** -- that is what keeps the
    repository-wide lock order one-way.

    It also re-checks the caller's intent identity first, so an unknown or
    foreign intent fails with its usual typed error WITHOUT the order
    store's write lock ever being taken for a doomed step. Nothing here is
    trusted: the guarded transaction reads both rows again and decides.
    """
    with _verified_read(db_path) as connection:
        _verified_intent_row(connection, intent)
        row = connection.execute(
            f"SELECT execution_request_id FROM {REPAIRS_TABLE} WHERE attempt_id = ?",
            (intent.repair_attempt_id,),
        ).fetchone()
    if row is None:
        raise TakeoverProtocolInvariantError(
            f"takeover intent {intent.takeover_id!r} names repair attempt "
            f"{intent.repair_attempt_id!r}, which does not exist"
        )
    return row["execution_request_id"]


@contextmanager
def _fence_held_still(
    intent: RepairTakeoverIntent, execution_request_id: str, execution_db_path: Path
):
    """Hold the order's fence still for a takeover step, or refuse it.

    THE CRITICAL SECTION (r7 review Blocker). Everything inside this block
    runs while this process holds the ORDER STORE's write lock, and the
    fence has already been verified to name exactly this intent under that
    lock. No other connection can replace the fence until the block exits,
    so a reconciliation write committed inside it can never be based on a
    fence that has since moved.

    Every failure mode is typed and leaves BOTH stores untouched:

    - no fence row / no order store -> :class:`TakeoverProtocolInvariantError`
    - the fence names another identity -> :class:`TakeoverIdentityMismatchError`
    - the store stayed write-locked -> :class:`TakeoverFenceUnavailableError`
      (transient; the durable intent survives and the next pass retries)
    - the store is unreadable/corrupt -> :class:`TakeoverProtocolInvariantError`
    """
    from execution import execution_store  # local: keeps the import graph acyclic

    expected = execution_store.RepairFenceToken(
        repair_attempt_id=intent.repair_attempt_id,
        generation=intent.target_generation,
        owner_id=intent.target_owner_id,
        claim_execution_id=intent.target_claim_execution_id,
        takeover_id=intent.takeover_id,
    )
    try:
        with execution_store.repair_fence_promotion_guard(
            execution_request_id, expected=expected, db_path=execution_db_path
        ):
            yield
    except execution_store.ExecutionRepairFenceMissingError as exc:
        raise TakeoverProtocolInvariantError(
            f"execution order {execution_request_id!r} has no committed repair fence; "
            f"takeover intent {intent.takeover_id!r} is not advanced "
            f"({type(exc).__name__})"
        ) from exc
    except execution_store.ExecutionRepairFenceBusyError as exc:
        raise TakeoverFenceUnavailableError(
            f"the execution order store could not be held still for takeover intent "
            f"{intent.takeover_id!r} ({type(exc).__name__}); nothing was written and the "
            "step is retried later"
        ) from exc
    except execution_store.ExecutionRepairFenceConflictError as exc:
        differing = ", ".join(getattr(exc, "mismatched_columns", ()) or ("identity",))
        raise TakeoverIdentityMismatchError(
            f"the committed repair fence of execution order {execution_request_id!r} "
            f"does not carry takeover intent {intent.takeover_id!r} at the moment of "
            f"this step (differing: {differing}); the takeover is not advanced"
        ) from exc
    except (execution_store.ExecutionStoreError, sqlite3.Error) as exc:
        raise TakeoverProtocolInvariantError(
            f"the execution order store could not be read to fence takeover intent "
            f"{intent.takeover_id!r} ({type(exc).__name__}); the takeover is not advanced"
        ) from exc


def mark_takeover_fence_installed(
    intent: RepairTakeoverIntent,
    *,
    execution_db_path: Path,
    at_utc: datetime,
    db_path: Path | None = None,
) -> RepairTakeoverIntent:
    """Record that the EXECUTION fence carries this intent's identity.

    SUBSTANCE CHECK (r6 review Blocker): the fence is read from the ORDER
    STORE's own committed row and compared in FULL --
    ``execution_request_id`` / ``repair_attempt_id`` / ``generation`` /
    ``takeover_id`` / ``owner_id`` / ``claim_execution_id`` -- against this
    intent's target identity AND the attempt's OWN execution order. **No
    caller-supplied value is accepted as evidence** that a fence exists, so
    there is no ``proof`` parameter at all. ``execution_db_path`` is
    CONFIGURATION (which order store this deployment uses), not evidence.

    GUARDED (r7 review): the read and the status write happen while this
    process holds the order store's write lock
    (:func:`execution_store.repair_fence_promotion_guard`), so the fence
    cannot be replaced between them. That also keeps the repository-wide
    LOCK ORDER one-way -- **order-store guard first, reconciliation
    transaction second** -- on every cross-DB path.

    An attempt with no execution order (a broker-only target) has no fence
    to install and is accepted without one and without a guard.

    Marking this status is still only a HISTORICAL FACT about the moment it
    ran. It is never on its own the permission to promote a claim:
    :func:`promote_repair_claim` re-verifies the current fence under its own
    guard.
    """
    _require_utc_argument(at_utc, "at_utc")
    if execution_db_path is None:
        raise ReconciliationStoreError(
            "execution_db_path is required: the fence is verified against the execution "
            "order store's committed row, never against a caller-supplied value"
        )
    execution_db_path = Path(execution_db_path)
    db_path = _require_existing_db(db_path)
    order_id = _order_of_attempt(intent, db_path)
    if order_id is None:
        return _mark_fence_installed_locked(intent, at_utc, db_path, guarded_order=None)
    with _fence_held_still(intent, order_id, execution_db_path):
        return _mark_fence_installed_locked(
            intent, at_utc, db_path, guarded_order=order_id
        )


def _mark_fence_installed_locked(
    intent: RepairTakeoverIntent,
    at_utc: datetime,
    db_path: Path,
    *,
    guarded_order: str | None,
) -> RepairTakeoverIntent:
    """The status write itself -- runs while the fence is held still."""
    with _verified_txn(db_path) as connection:
        stored = _verified_intent_row(connection, intent)
        current = TakeoverIntentStatus(stored["status"])
        attempt = _attempt_row_for_intent(connection, intent)
        if attempt["execution_request_id"] != guarded_order:
            raise TakeoverProtocolInvariantError(
                f"repair attempt {intent.repair_attempt_id!r} names execution order "
                f"{attempt['execution_request_id']!r}, but the fence held still for this "
                f"transition is {guarded_order!r}; the intent is not advanced"
            )
        if current is TakeoverIntentStatus.FENCE_INSTALLED:
            return _restore_intent(stored)  # idempotent retry after a crash
        return _advance_status(
            connection, intent, current, TakeoverIntentStatus.FENCE_INSTALLED, at_utc
        )


def mark_takeover_claim_promoted(
    intent: RepairTakeoverIntent, *, at_utc: datetime, db_path: Path | None = None
) -> RepairTakeoverIntent:
    """Record that the attempt's CLAIM now belongs to this intent.

    SUBSTANCE CHECK (same transaction, same DB): the repair attempt must be
    ``pending`` and held by exactly this intent's target identity --
    generation, takeover id, owner and claim execution id. A status that
    runs ahead of the real claim is refused with a typed error and the
    transaction rolls back (r5 review Blocker)."""
    _require_utc_argument(at_utc, "at_utc")
    db_path = _require_existing_db(db_path)
    with _verified_txn(db_path) as connection:
        stored = _verified_intent_row(connection, intent)
        current = TakeoverIntentStatus(stored["status"])
        attempt = _attempt_row_for_intent(connection, intent)
        if attempt["status"] != ATTEMPT_STATUS_PENDING:
            raise TakeoverClaimNotPromotedError(
                f"repair attempt {intent.repair_attempt_id!r} is {attempt['status']!r}, "
                f"not a pending claim held by takeover intent {intent.takeover_id!r}"
            )
        _require_claim_matches_intent(attempt, intent)
        if current is TakeoverIntentStatus.CLAIM_PROMOTED:
            return _restore_intent(stored)  # idempotent retry after a crash
        return _advance_status(
            connection, intent, current, TakeoverIntentStatus.CLAIM_PROMOTED, at_utc
        )


def complete_takeover_intent(
    intent: RepairTakeoverIntent, *, at_utc: datetime, db_path: Path | None = None
) -> RepairTakeoverIntent:
    """Close a takeover whose attempt was FINALIZED under its identity.

    SUBSTANCE CHECK (same transaction, same DB): the repair attempt must be
    ``finalized``, carry an outcome from
    :data:`COMPLETABLE_TAKEOVER_OUTCOMES` and a ``finalized_at_utc``, and
    still be held by this intent's target identity. A pending attempt, a
    missing finalize timestamp or a foreign identity can never complete an
    intent (r5 review Blocker)."""
    _require_utc_argument(at_utc, "at_utc")
    db_path = _require_existing_db(db_path)
    with _verified_txn(db_path) as connection:
        stored = _verified_intent_row(connection, intent)
        current = TakeoverIntentStatus(stored["status"])
        attempt = _attempt_row_for_intent(connection, intent)
        if attempt["status"] != ATTEMPT_STATUS_FINALIZED:
            raise TakeoverProtocolInvariantError(
                f"repair attempt {intent.repair_attempt_id!r} is {attempt['status']!r}; a "
                "takeover intent completes only once its attempt is finalized"
            )
        if attempt["outcome"] is None or attempt["finalized_at_utc"] is None:
            raise TakeoverProtocolInvariantError(
                f"repair attempt {intent.repair_attempt_id!r} is marked finalized without "
                "an outcome and finalize timestamp; refusing to complete the takeover"
            )
        try:
            outcome = RepairOutcome(attempt["outcome"])
        except ValueError as exc:
            raise ReconciliationIntegrityError(
                f"repair attempt {intent.repair_attempt_id!r} carries unknown outcome "
                f"{attempt['outcome']!r}"
            ) from exc
        if outcome not in COMPLETABLE_TAKEOVER_OUTCOMES:
            raise TakeoverProtocolInvariantError(
                f"repair attempt {intent.repair_attempt_id!r} finalized as "
                f"{outcome.value!r}, which never completes a takeover intent"
            )
        _require_claim_matches_intent(attempt, intent)
        if current is TakeoverIntentStatus.COMPLETED:
            return _restore_intent(stored)  # idempotent retry after a crash
        return _advance_status(
            connection, intent, current, TakeoverIntentStatus.COMPLETED, at_utc
        )


def cancel_takeover_intent(
    intent: RepairTakeoverIntent,
    *,
    reason: TakeoverCancelReason,
    at_utc: datetime,
    db_path: Path | None = None,
) -> RepairTakeoverIntent:
    """Close a takeover that became UNNECESSARY before it took effect.

    SUBSTANCE CHECK: the attempt must be finalized under the claim this
    intent meant to REPLACE (the source identity) -- that is the only state
    in which a takeover legitimately never applies. An attempt still held by
    this intent, or one finalized under a third identity, is not a
    cancellation and is refused."""
    if not isinstance(reason, TakeoverCancelReason):
        raise ReconciliationStoreError(f"reason must be a TakeoverCancelReason, got {reason!r}")
    _require_utc_argument(at_utc, "at_utc")
    db_path = _require_existing_db(db_path)
    with _verified_txn(db_path) as connection:
        stored = _verified_intent_row(connection, intent)
        current = TakeoverIntentStatus(stored["status"])
        attempt = _attempt_row_for_intent(connection, intent)
        if attempt["status"] != ATTEMPT_STATUS_FINALIZED:
            raise TakeoverProtocolInvariantError(
                f"repair attempt {intent.repair_attempt_id!r} is {attempt['status']!r}; a "
                "takeover is cancelled only once its attempt was finalized by the claim "
                "this intent meant to replace"
            )
        if (
            attempt["generation"] != intent.source_generation
            or attempt["owner_id"] != intent.source_owner_id
            or attempt["claim_execution_id"] != intent.source_claim_execution_id
        ):
            raise TakeoverIdentityMismatchError(
                f"repair attempt {intent.repair_attempt_id!r} was not finalized by the "
                f"source claim of takeover intent {intent.takeover_id!r}; refusing to "
                "cancel it"
            )
        if current is TakeoverIntentStatus.CANCELLED:
            return _restore_intent(stored)  # idempotent retry after a crash
        if TakeoverIntentStatus.CANCELLED not in ALLOWED_INTENT_TRANSITIONS[current]:
            raise TakeoverIntentStateError(
                f"takeover intent {intent.takeover_id!r} cannot move from "
                f"{current.value!r} to {TakeoverIntentStatus.CANCELLED.value!r}"
            )
        try:
            connection.execute(
                f"UPDATE {TAKEOVER_INTENTS_TABLE} SET status = ?, cancelled_at_utc = ?, "
                "cancelled_reason = ? WHERE takeover_id = ? AND status = ?",
                (
                    TakeoverIntentStatus.CANCELLED.value,
                    _iso(at_utc),
                    reason.value,
                    intent.takeover_id,
                    current.value,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ReconciliationStoreError(
                f"cancelling takeover intent {intent.takeover_id!r} violates a DB "
                f"constraint: {type(exc).__name__}"
            ) from exc
        return _reload_intent(connection, intent.takeover_id)


def _advance_status(
    connection: sqlite3.Connection,
    intent: RepairTakeoverIntent,
    current: TakeoverIntentStatus,
    to_status: TakeoverIntentStatus,
    at_utc: datetime,
) -> RepairTakeoverIntent:
    """Forward-only status write. Callers have ALREADY verified the substance
    the target status asserts; this only enforces the lifecycle itself."""
    if to_status not in ALLOWED_INTENT_TRANSITIONS[current]:
        raise TakeoverIntentStateError(
            f"takeover intent {intent.takeover_id!r} cannot move from {current.value!r} "
            f"to {to_status.value!r}"
        )
    column = {
        TakeoverIntentStatus.FENCE_INSTALLED: "fence_installed_at_utc",
        TakeoverIntentStatus.CLAIM_PROMOTED: "claim_promoted_at_utc",
        TakeoverIntentStatus.COMPLETED: "completed_at_utc",
    }[to_status]
    try:
        updated = connection.execute(
            f"UPDATE {TAKEOVER_INTENTS_TABLE} SET status = ?, {column} = ? "
            "WHERE takeover_id = ? AND status = ?",
            (to_status.value, _iso(at_utc), intent.takeover_id, current.value),
        )
    except sqlite3.IntegrityError as exc:
        raise ReconciliationStoreError(
            f"advancing takeover intent {intent.takeover_id!r} to {to_status.value!r} "
            f"violates a DB constraint: {type(exc).__name__}"
        ) from exc
    if updated.rowcount != 1:  # pragma: no cover - defence in depth
        raise TakeoverIntentStateError(
            f"takeover intent {intent.takeover_id!r} changed status concurrently; "
            "nothing was updated"
        )
    return _reload_intent(connection, intent.takeover_id)


def _reload_intent(connection: sqlite3.Connection, takeover_id: str) -> RepairTakeoverIntent:
    return _restore_intent(
        connection.execute(
            f"SELECT * FROM {TAKEOVER_INTENTS_TABLE} WHERE takeover_id = ?",
            (takeover_id,),
        ).fetchone()
    )


def renew_takeover_intent_lease(
    intent: RepairTakeoverIntent,
    *,
    new_lease_expires_at_utc: datetime,
    db_path: Path | None = None,
) -> RepairTakeoverIntent:
    """Take over the RESUMPTION of an active intent -- atomic CAS on its lease.

    The intent's lease is the deadline for finishing the takeover; once it
    expires ANY worker may resume the intent, but only ONE may do so at a
    time. The CAS is on the observed lease value, so of several workers that
    find the same expired intent exactly one wins and the losers get
    :class:`TakeoverIntentConflictError` (and change nothing). The IDENTITY
    -- takeover id, target owner, target claim execution id, generations --
    is never touched: resuming means continuing the SAME takeover, never
    minting a new one.
    """
    _require_utc_argument(new_lease_expires_at_utc, "new_lease_expires_at_utc")
    db_path = _require_existing_db(db_path)
    with _verified_txn(db_path) as connection:
        stored = _verified_intent_row(connection, intent)
        status = TakeoverIntentStatus(stored["status"])
        if status not in ACTIVE_INTENT_STATUSES:
            raise TakeoverIntentStateError(
                f"takeover intent {intent.takeover_id!r} is {status.value!r} and is no "
                "longer resumable"
            )
        if new_lease_expires_at_utc <= _parse_utc(stored["lease_expires_at_utc"]):
            # somebody already resumed it with a lease at least this long
            raise TakeoverIntentConflictError(
                f"takeover intent {intent.takeover_id!r} is leased until "
                f"{stored['lease_expires_at_utc']}, at or beyond the requested "
                f"{new_lease_expires_at_utc.isoformat()}; another worker resumed it"
            )
        updated = connection.execute(
            f"UPDATE {TAKEOVER_INTENTS_TABLE} SET lease_expires_at_utc = ? "
            "WHERE takeover_id = ? AND status = ? AND lease_expires_at_utc = ?",
            (
                _iso(new_lease_expires_at_utc),
                intent.takeover_id,
                stored["status"],
                stored["lease_expires_at_utc"],
            ),
        )
        if updated.rowcount != 1:
            raise TakeoverIntentConflictError(
                f"takeover intent {intent.takeover_id!r} was resumed by another worker; "
                "nothing was changed"
            )
        return _restore_intent(
            connection.execute(
                f"SELECT * FROM {TAKEOVER_INTENTS_TABLE} WHERE takeover_id = ?",
                (intent.takeover_id,),
            ).fetchone()
        )


def block_takeover_intent(
    intent: RepairTakeoverIntent,
    *,
    reason: TakeoverBlockedReason,
    at_utc: datetime,
    observed_claim_generation: int | None = None,
    observed_fence_generation: int | None = None,
    observed_fence_identity: str | None = None,
    db_path: Path | None = None,
) -> RepairTakeoverIntent:
    """Park an ACTIVE intent as ``blocked`` with its diagnosis (r4 review).

    Idempotent: an intent already blocked keeps its ORIGINAL reason and
    timestamps (the first diagnosis is the real one) and is returned as
    stored. A completed intent is never blocked.

    The ``observed_*`` arguments are caller-supplied DIAGNOSTICS, not
    evidence: they are written verbatim into columns an operator reads and
    grant no authority whatsoever. Blocking is the fail-closed direction --
    it stops the protocol and leaves the attempt pending -- so an untrue
    observation can only ever make a human look, never move a claim, a
    fence or an order (r6 review cross-check)."""
    if not isinstance(reason, TakeoverBlockedReason):
        raise ReconciliationStoreError(
            f"reason must be a TakeoverBlockedReason, got {reason!r}"
        )
    _require_utc_argument(at_utc, "at_utc")
    db_path = _require_existing_db(db_path)
    with _verified_txn(db_path) as connection:
        stored = _verified_intent_row(connection, intent)
        current = TakeoverIntentStatus(stored["status"])
        if current is TakeoverIntentStatus.BLOCKED:
            return _restore_intent(stored)
        if TakeoverIntentStatus.BLOCKED not in ALLOWED_INTENT_TRANSITIONS[current]:
            raise TakeoverIntentStateError(
                f"takeover intent {intent.takeover_id!r} is {current.value!r} and can no "
                "longer be blocked"
            )
        try:
            connection.execute(
                f"UPDATE {TAKEOVER_INTENTS_TABLE} SET status = ?, blocked_at_utc = ?, "
                "blocked_reason = ?, observed_claim_generation = ?, "
                "observed_fence_generation = ?, observed_fence_identity = ? "
                "WHERE takeover_id = ? AND status = ?",
                (
                    TakeoverIntentStatus.BLOCKED.value,
                    _iso(at_utc),
                    reason.value,
                    observed_claim_generation,
                    observed_fence_generation,
                    observed_fence_identity,
                    intent.takeover_id,
                    current.value,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ReconciliationStoreError(
                f"blocking takeover intent {intent.takeover_id!r} violates a DB "
                f"constraint: {type(exc).__name__}"
            ) from exc
        return _restore_intent(
            connection.execute(
                f"SELECT * FROM {TAKEOVER_INTENTS_TABLE} WHERE takeover_id = ?",
                (intent.takeover_id,),
            ).fetchone()
        )


def _verified_intent_row(connection: sqlite3.Connection, intent: RepairTakeoverIntent):
    """The stored row for ``intent``, with its IDENTITY verified.

    Identity = takeover id + attempt + both generations + target owner +
    target claim execution id + the source claim it replaces. A mismatch is
    never "close enough": it means the caller is acting on a different
    takeover and is refused."""
    if not isinstance(intent, RepairTakeoverIntent):
        raise ReconciliationStoreError(f"intent must be a RepairTakeoverIntent, got {intent!r}")
    row = connection.execute(
        f"SELECT * FROM {TAKEOVER_INTENTS_TABLE} WHERE takeover_id = ?",
        (intent.takeover_id,),
    ).fetchone()
    if row is None:
        raise TakeoverIntentStateError(
            f"takeover intent {intent.takeover_id!r} is not recorded; a takeover step may "
            "only act on a durable intent"
        )
    mismatched = sorted(
        column
        for column, expected in (
            ("repair_attempt_id", intent.repair_attempt_id),
            ("source_generation", intent.source_generation),
            ("target_generation", intent.target_generation),
            ("source_owner_id", intent.source_owner_id),
            ("source_claim_execution_id", intent.source_claim_execution_id),
            ("target_owner_id", intent.target_owner_id),
            ("target_claim_execution_id", intent.target_claim_execution_id),
        )
        if row[column] != expected
    )
    if mismatched:
        raise TakeoverIntentStateError(
            f"takeover intent {intent.takeover_id!r} does not match the stored intent "
            f"(differing: {', '.join(mismatched)}); refusing to act on a foreign identity"
        )
    return row


def get_active_takeover_intent(
    attempt_id: str, *, db_path: Path | None = None
) -> RepairTakeoverIntent | None:
    """The attempt's ACTIVE takeover intent, or None.

    This is what a restarted worker reads FIRST: the intent carries the
    identity to continue with. Several active intents is out-of-band damage
    and fails closed (:class:`ReconciliationIntegrityError`) rather than
    picking one. Missing DB -> None, file never created."""
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ReconciliationStoreError(f"attempt_id must be a non-empty string, got {attempt_id!r}")
    try:
        with _verified_read(_read_path(db_path)) as connection:
            row = _active_intent_row(connection, attempt_id)
    except ReadOnlyDbMissingError:
        return None
    return None if row is None else _restore_intent(row)


def list_takeover_intents(
    *, attempt_id: str | None = None, db_path: Path | None = None
) -> tuple[RepairTakeoverIntent, ...]:
    """Every intent (active, completed and blocked), oldest first.

    Intents are retained forever: they are the audit trail proving which
    identity owned each generation of an attempt."""
    try:
        with _verified_read(_read_path(db_path)) as connection:
            if attempt_id is None:
                rows = connection.execute(
                    f"SELECT * FROM {TAKEOVER_INTENTS_TABLE} "
                    "ORDER BY created_at_utc, repair_attempt_id, source_generation"
                ).fetchall()
            else:
                rows = connection.execute(
                    f"SELECT * FROM {TAKEOVER_INTENTS_TABLE} WHERE repair_attempt_id = ? "
                    "ORDER BY source_generation, created_at_utc",
                    (attempt_id,),
                ).fetchall()
    except ReadOnlyDbMissingError:
        return ()
    return tuple(_restore_intent(row) for row in rows)


def promote_repair_claim(
    intent: RepairTakeoverIntent,
    *,
    execution_db_path: Path,
    promoted_at_utc: datetime,
    db_path: Path | None = None,
) -> RepairAttemptReservation:
    """Move the attempt's claim onto the intent's identity -- step 4.

    Compare-and-set on the SOURCE claim (pending status, source generation,
    source owner, source claim execution id); on success the row carries the
    intent's ``target_*`` identity, its ``takeover_id`` and the intent's
    lease. IDEMPOTENT: if the row already carries exactly this intent's
    identity the stored reservation is returned unchanged, so a worker that
    died after the promotion resumes without a second write.

    A row that moved on to another identity raises
    :class:`RepairClaimLostError`; a finalized attempt raises
    :class:`RepairAttemptConflictError`.

    PROMOTION IS FENCED, NOT MERELY PRECEDED BY A FENCE (r7 review
    Blocker). ``fence_installed`` records that the fence carried this
    identity at SOME EARLIER MOMENT; it is a historical fact, not a standing
    permission. Between that check and this CAS another attempt could
    replace the order's fence, leaving the claim at generation 2 under
    identity X while the order was fenced for Y -- a split identity produced
    by this very API. Re-reading the fence here would only narrow that
    window, so instead the whole critical section runs under the ORDER
    STORE's write lock:

    .. code-block:: text

        execution_store.repair_fence_promotion_guard()   # BEGIN IMMEDIATE
          -> the committed fence still names this intent (6 columns)
          -> reconciliation BEGIN IMMEDIATE -> claim CAS -> COMMIT
        release the guard

    No other connection can take the order store's write lock in between, so
    no fence replacement can land between the check and the committed CAS.
    The guard is read-only, so it never blocks READERS and the lock order
    (order store first, reconciliation second) cannot deadlock against
    :func:`mark_takeover_fence_installed`, which reads the order store from
    inside a reconciliation transaction.

    Typed outcomes: a fence that is missing or names someone else raises
    :class:`TakeoverIdentityMismatchError`; an order store that stays
    write-locked past its busy timeout raises
    :class:`TakeoverFenceUnavailableError` (retryable -- nothing was
    written). An attempt with NO execution order (a broker-only target) has
    no fence to hold and is promoted without a guard.
    """
    _require_utc_argument(promoted_at_utc, "promoted_at_utc")
    if execution_db_path is None:
        raise ReconciliationStoreError(
            "execution_db_path is required: a claim is promoted only while this "
            "process holds the order store's fence still"
        )
    execution_db_path = Path(execution_db_path)
    db_path = _require_existing_db(db_path)
    order_id = _order_of_attempt(intent, db_path)
    if order_id is None:
        return _promote_under_guard(intent, promoted_at_utc, db_path, guarded_order=None)
    with _fence_held_still(intent, order_id, execution_db_path):
        return _promote_under_guard(intent, promoted_at_utc, db_path, guarded_order=order_id)


def _promote_under_guard(
    intent: RepairTakeoverIntent,
    promoted_at_utc: datetime,
    db_path: Path,
    *,
    guarded_order: str | None,
) -> RepairAttemptReservation:
    """The claim CAS itself -- runs while the order's fence is held still."""
    with _verified_txn(db_path) as connection:
        stored_intent = _verified_intent_row(connection, intent)
        status = TakeoverIntentStatus(stored_intent["status"])
        if status not in (
            TakeoverIntentStatus.FENCE_INSTALLED,
            TakeoverIntentStatus.CLAIM_PROMOTED,
        ):
            raise TakeoverIntentStateError(
                f"takeover intent {intent.takeover_id!r} is {status.value!r}; the claim may "
                "only be promoted once the execution fence carries this identity"
            )
        row = connection.execute(
            f"SELECT * FROM {REPAIRS_TABLE} WHERE attempt_id = ?",
            (intent.repair_attempt_id,),
        ).fetchone()
        if row is None:
            raise ReconciliationStoreError(
                f"repair attempt {intent.repair_attempt_id!r} has no reservation"
            )
        if row["status"] != ATTEMPT_STATUS_PENDING:
            raise RepairAttemptConflictError(
                f"repair attempt {intent.repair_attempt_id!r} is already finalized "
                f"(outcome {row['outcome']!r}); its claim cannot be promoted"
            )
        if row["execution_request_id"] != guarded_order:
            # the order named by the guard must still be this attempt's order
            raise TakeoverProtocolInvariantError(
                f"repair attempt {intent.repair_attempt_id!r} names execution order "
                f"{row['execution_request_id']!r}, but the fence held still for this "
                f"promotion is {guarded_order!r}; the claim is not promoted"
            )
        if (
            row["generation"] == intent.target_generation
            and row["takeover_id"] == intent.takeover_id
            and row["owner_id"] == intent.target_owner_id
            and row["claim_execution_id"] == intent.target_claim_execution_id
        ):
            return _restore_reservation(row)  # already promoted by this intent
        try:
            updated = connection.execute(
                f"UPDATE {REPAIRS_TABLE} SET owner_id = ?, claim_execution_id = ?, "
                "generation = ?, takeover_id = ?, lease_expires_at_utc = ?, "
                "last_claimed_at_utc = ? "
                "WHERE attempt_id = ? AND status = ? AND generation = ? AND owner_id = ? "
                "AND claim_execution_id = ?",
                (
                    intent.target_owner_id,
                    intent.target_claim_execution_id,
                    intent.target_generation,
                    intent.takeover_id,
                    _iso(intent.lease_expires_at_utc),
                    _iso(promoted_at_utc),
                    intent.repair_attempt_id,
                    ATTEMPT_STATUS_PENDING,
                    intent.source_generation,
                    intent.source_owner_id,
                    intent.source_claim_execution_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ReconciliationStoreError(
                f"promoting the claim of repair attempt {intent.repair_attempt_id!r} "
                f"violates a DB constraint: {type(exc).__name__}"
            ) from exc
        if updated.rowcount != 1:
            raise RepairClaimLostError(
                f"repair attempt {intent.repair_attempt_id!r} is claimed at generation "
                f"{row['generation']} by claim {row['claim_execution_id']!r}; takeover "
                f"intent {intent.takeover_id!r} expected generation "
                f"{intent.source_generation} / claim {intent.source_claim_execution_id!r}"
            )
        return _restore_reservation(
            connection.execute(
                f"SELECT * FROM {REPAIRS_TABLE} WHERE attempt_id = ?",
                (intent.repair_attempt_id,),
            ).fetchone()
        )


def finalize_repair_attempt(attempt: OrderRepairAttempt, *, db_path: Path | None = None) -> None:
    """Complete the reserved audit row with the attempt's outcome.

    FENCED compare-and-set: only a row that is still ``pending`` AND still
    claimed by THIS attempt's (owner_id, claim_execution_id, generation) can
    be finalized. A worker whose claim was taken over observes
    :class:`RepairClaimLostError` -- its outcome is never stored over the
    new owner's, and an APPLIED fact can never be overwritten by a
    contradicting result. Finalizing an already-finalized attempt raises
    :class:`RepairAttemptConflictError`; an identity/anchor mismatch is
    corruption and raises :class:`ReconciliationIntegrityError`. Only
    APPLY-mode attempts exist in this DB; a ``would_apply`` outcome is
    unrepresentable (dry-run persists nothing). ``attempt.audit_error`` is
    in-memory metadata and is never persisted.
    """
    if not isinstance(attempt, OrderRepairAttempt):
        raise ReconciliationStoreError(f"attempt must be an OrderRepairAttempt, got {attempt!r}")
    if attempt.mode is not RepairMode.APPLY:
        raise ReconciliationStoreError(
            f"only apply-mode repair attempts are audited; got mode {attempt.mode.value!r} "
            "(dry-run is a no-side-effect contract)"
        )
    if attempt.finalized_at_utc is None:
        raise ReconciliationStoreError(
            f"finalizing repair attempt {attempt.attempt_id!r} requires finalized_at_utc"
        )
    if attempt.outcome is RepairOutcome.BLOCKED:
        raise ReconciliationStoreError(
            f"repair attempt {attempt.attempt_id!r} cannot be finalized as "
            f"{RepairOutcome.BLOCKED.value!r}: a blocked takeover leaves the attempt "
            "PENDING for manual intervention and is recorded on the takeover intent row"
        )
    db_path = _require_existing_db(db_path)
    expected_identity = {
        "run_id": attempt.run_id,
        "mode": attempt.mode.value,
        "target_type": attempt.target_type.value,
        "target_id": attempt.target_id,
        "execution_request_id": attempt.execution_request_id,
        "client_order_id": attempt.client_order_id,
        "reconciliation_internal_state": None
        if attempt.reconciliation_internal_state is None
        else attempt.reconciliation_internal_state.value,
        "reconciliation_broker_state": None
        if attempt.reconciliation_broker_state is None
        else attempt.reconciliation_broker_state.value,
        "internal_revision": attempt.internal_revision,
        "observed_at_utc": None
        if attempt.observed_at_utc is None
        else _iso(attempt.observed_at_utc),
        "reserved_at_utc": _iso(attempt.reserved_at_utc),
    }
    with _verified_txn(db_path) as connection:
        row = connection.execute(
            f"SELECT * FROM {REPAIRS_TABLE} WHERE attempt_id = ?", (attempt.attempt_id,)
        ).fetchone()
        if row is None:
            raise ReconciliationStoreError(
                f"repair attempt {attempt.attempt_id!r} has no reservation; attempts are "
                "reserved before they are finalized"
            )
        if row["status"] != ATTEMPT_STATUS_PENDING:
            raise RepairAttemptConflictError(
                f"repair attempt {attempt.attempt_id!r} is already finalized "
                f"(outcome {row['outcome']!r}); an attempt is finalized exactly once"
            )
        mismatched = sorted(
            column
            for column in _ATTEMPT_IDENTITY_COLUMNS
            if row[column] != expected_identity[column]
        )
        if mismatched:
            raise ReconciliationIntegrityError(
                f"finalize of repair attempt {attempt.attempt_id!r} does not match its "
                f"stored reservation (differing: {', '.join(mismatched)}); refusing to "
                "complete a foreign reservation"
            )
        if (
            row["owner_id"] != attempt.owner_id
            or row["claim_execution_id"] != attempt.claim_execution_id
            or row["generation"] != attempt.generation
            or row["takeover_id"] != attempt.takeover_id
        ):
            raise RepairClaimLostError(
                f"repair attempt {attempt.attempt_id!r} is claimed by generation "
                f"{row['generation']} (owner {row['owner_id']!r}, claim "
                f"{row['claim_execution_id']!r}, takeover {row['takeover_id']!r}), not by "
                f"this worker's generation {attempt.generation} / takeover "
                f"{attempt.takeover_id!r}; the lost claim's outcome is not stored"
            )
        try:
            updated = connection.execute(
                f"UPDATE {REPAIRS_TABLE} SET status = ?, outcome = ?, detail = ?, "
                "internal_state_before = ?, internal_state_after = ?, broker_order_id = ?, "
                "broker_error_code = ?, failure_code = ?, broker_state_updated_at_utc = ?, "
                "finalized_at_utc = ? "
                "WHERE attempt_id = ? AND status = ? AND owner_id = ? "
                "AND claim_execution_id = ? AND generation = ? "
                "AND takeover_id IS ?",
                (
                    ATTEMPT_STATUS_FINALIZED,
                    attempt.outcome.value,
                    attempt.detail,
                    None
                    if attempt.internal_state_before is None
                    else attempt.internal_state_before.value,
                    None
                    if attempt.internal_state_after is None
                    else attempt.internal_state_after.value,
                    attempt.broker_order_id,
                    None if attempt.broker_error_code is None else attempt.broker_error_code.value,
                    None if attempt.failure_code is None else attempt.failure_code.value,
                    None
                    if attempt.broker_state_updated_at_utc is None
                    else _iso(attempt.broker_state_updated_at_utc),
                    _iso(attempt.finalized_at_utc),
                    attempt.attempt_id,
                    ATTEMPT_STATUS_PENDING,
                    attempt.owner_id,
                    attempt.claim_execution_id,
                    attempt.generation,
                    attempt.takeover_id,
                ),
            )
        except sqlite3.IntegrityError as exc:  # CHECK violation
            raise ReconciliationStoreError(
                f"finalizing repair attempt {attempt.attempt_id!r} violates a DB "
                f"constraint: {exc}"
            ) from exc
        if updated.rowcount != 1:  # defence in depth under BEGIN IMMEDIATE
            raise RepairClaimLostError(
                f"repair attempt {attempt.attempt_id!r} was claimed or finalized "
                "concurrently; the lost claim's outcome is not stored"
            )


def _restore_repair_attempt(row: sqlite3.Row) -> OrderRepairAttempt:
    """Rebuild one FINALIZED attempt; the model re-runs every invariant, so
    an out-of-band edited row fails loudly instead of being trusted."""

    def state(name):
        return None if row[name] is None else OrderState(row[name])

    try:
        if row["status"] != ATTEMPT_STATUS_FINALIZED:
            raise ValueError(f"attempt row carries status {row['status']!r}, not finalized")
        return OrderRepairAttempt(
            attempt_id=row["attempt_id"],
            run_id=row["run_id"],
            mode=RepairMode(row["mode"]),
            target_type=ReconciliationTargetType(row["target_type"]),
            target_id=row["target_id"],
            outcome=RepairOutcome(row["outcome"]),
            detail=row["detail"],
            reserved_at_utc=_parse_utc(row["reserved_at_utc"]),
            owner_id=row["owner_id"],
            claim_execution_id=row["claim_execution_id"],
            generation=row["generation"],
            takeover_id=row["takeover_id"],
            execution_request_id=row["execution_request_id"],
            client_order_id=row["client_order_id"],
            reconciliation_internal_state=state("reconciliation_internal_state"),
            reconciliation_broker_state=state("reconciliation_broker_state"),
            internal_revision=row["internal_revision"],
            internal_state_before=state("internal_state_before"),
            internal_state_after=state("internal_state_after"),
            broker_order_id=row["broker_order_id"],
            broker_error_code=None
            if row["broker_error_code"] is None
            else BrokerErrorCode(row["broker_error_code"]),
            failure_code=None
            if row["failure_code"] is None
            else RepairFailureCode(row["failure_code"]),
            observed_at_utc=None
            if row["observed_at_utc"] is None
            else _parse_utc(row["observed_at_utc"]),
            broker_state_updated_at_utc=None
            if row["broker_state_updated_at_utc"] is None
            else _parse_utc(row["broker_state_updated_at_utc"]),
            finalized_at_utc=None
            if row["finalized_at_utc"] is None
            else _parse_utc(row["finalized_at_utc"]),
        )
    except (ValueError, TypeError) as exc:
        raise ReconciliationIntegrityError(
            f"repair attempt row {row['attempt_id']!r} failed restore-time validation: {exc}"
        ) from exc


def _restore_reservation(row: sqlite3.Row) -> RepairAttemptReservation:
    """Rebuild one PENDING reservation (claim included); strict, fail-closed."""

    def state(name):
        return None if row[name] is None else OrderState(row[name])

    try:
        if row["status"] != ATTEMPT_STATUS_PENDING:
            raise ValueError(f"attempt row carries status {row['status']!r}, not pending")
        if row["mode"] != RepairMode.APPLY.value:
            raise ValueError(f"reservation carries mode {row['mode']!r}")
        return RepairAttemptReservation(
            attempt_id=row["attempt_id"],
            run_id=row["run_id"],
            target_type=ReconciliationTargetType(row["target_type"]),
            target_id=row["target_id"],
            reserved_at_utc=_parse_utc(row["reserved_at_utc"]),
            detail=row["detail"],
            owner_id=row["owner_id"],
            claim_execution_id=row["claim_execution_id"],
            generation=row["generation"],
            takeover_id=row["takeover_id"],
            lease_expires_at_utc=_parse_utc(row["lease_expires_at_utc"]),
            last_claimed_at_utc=_parse_utc(row["last_claimed_at_utc"]),
            execution_request_id=row["execution_request_id"],
            client_order_id=row["client_order_id"],
            reconciliation_internal_state=state("reconciliation_internal_state"),
            reconciliation_broker_state=state("reconciliation_broker_state"),
            internal_revision=row["internal_revision"],
            observed_at_utc=None
            if row["observed_at_utc"] is None
            else _parse_utc(row["observed_at_utc"]),
        )
    except (ValueError, TypeError) as exc:
        raise ReconciliationIntegrityError(
            f"pending repair attempt row {row['attempt_id']!r} failed restore-time "
            f"validation: {exc}"
        ) from exc


def get_repair_attempt(attempt_id: str, *, db_path: Path | None = None) -> OrderRepairAttempt | None:
    """The stored FINALIZED repair attempt, or None (missing DB, unknown id,
    or a still-pending reservation -- pending rows are listed by
    :func:`list_pending_repair_attempts`). The DB file is never created."""
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ReconciliationStoreError(f"attempt_id must be a non-empty string, got {attempt_id!r}")
    db_path = Path(db_path) if db_path is not None else reconciliation_db_path()
    if not db_path.exists():
        return None
    try:
        with _verified_read(db_path) as connection:
            row = connection.execute(
                f"SELECT * FROM {REPAIRS_TABLE} WHERE attempt_id = ? AND status = ?",
                (attempt_id, ATTEMPT_STATUS_FINALIZED),
            ).fetchone()
            return None if row is None else _restore_repair_attempt(row)
    except ReadOnlyDbMissingError:
        return None


def _list_attempt_rows(status: str, run_id: str | None, db_path: Path | None):
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise ReconciliationStoreError(f"run_id must be a non-empty string or None, got {run_id!r}")
    db_path = Path(db_path) if db_path is not None else reconciliation_db_path()
    if not db_path.exists():
        return None
    where = "WHERE status = ?" + ("" if run_id is None else " AND run_id = ?")
    parameters = (status,) if run_id is None else (status, run_id)
    with _verified_read(db_path) as connection:
        return connection.execute(
            f"SELECT * FROM {REPAIRS_TABLE} {where} "
            "ORDER BY reserved_at_utc ASC, attempt_id ASC",
            parameters,
        ).fetchall()


def list_repair_attempts(
    *, run_id: str | None = None, db_path: Path | None = None
) -> tuple[OrderRepairAttempt, ...]:
    """The FINALIZED repair attempts, oldest first (``reserved_at_utc``,
    ``attempt_id`` as the tie-break so the order is total and stable),
    optionally restricted to one reconciliation run.

    Missing DB -> empty tuple, file never created.
    """
    try:
        rows = _list_attempt_rows(ATTEMPT_STATUS_FINALIZED, run_id, db_path)
    except ReadOnlyDbMissingError:
        return ()
    if rows is None:
        return ()
    return tuple(_restore_repair_attempt(row) for row in rows)


def list_pending_repair_attempts(
    *, run_id: str | None = None, db_path: Path | None = None
) -> tuple[RepairAttemptReservation, ...]:
    """The PENDING reservations (reserved but never finalized -- e.g. a
    crashed repair pass), oldest first. These are the recovery work list:
    the repair service finalizes each one under its ORIGINAL attempt id by
    checking the execution order state history's structured provenance.

    Missing DB -> empty tuple, file never created.
    """
    try:
        rows = _list_attempt_rows(ATTEMPT_STATUS_PENDING, run_id, db_path)
    except ReadOnlyDbMissingError:
        return ()
    if rows is None:
        return ()
    return tuple(_restore_reservation(row) for row in rows)
