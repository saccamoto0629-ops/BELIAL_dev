"""SQLite persistence for execution requests and their order state (Issue #74).

*** FOUNDATION ONLY. No Live Execution runner, no broker calls, no external
I/O of any kind happens here. This store persists and restores state; it never
submits anything. ***

One DB file (``execution/data/execution_orders.sqlite3`` by default,
``EXECUTION_ORDER_DB_PATH`` to override), owned by ``execution/`` alone --
independent from the Paper Signal / Paper Position / Backtest DBs, per the
one-DB-per-component rule (docs/architecture/architecture.md).

State design (Issue #74): the broker order-state machine
(``broker.broker_models.OrderState`` + ``validate_order_state_transition`` /
``validate_order_result_update``) is the SINGLE source of truth for order
states -- this module defines no state enum and no transition table. The
BELIAL-internal CREATED / SUBMITTING lifecycle is NOT part of the broker
state machine; it is a separate ``submission_phase`` column:

- ``created``    -- intent persisted, no submit attempt yet
                    (``order_state`` is NULL: there is no broker order).
- ``submitting`` -- a submit attempt started. Broker-state tracking begins at
                    the existing ``OrderState.PENDING`` ("submitted by us, no
                    broker acknowledgement yet" -- exactly the broker meaning,
                    so no new enum member is needed).
- ``submitted``  -- at least one broker-CONFIRMED observation exists (state in
                    accepted / partially_filled / filled / cancelled /
                    rejected). Derived, never regresses.

Snapshot persistence, HISTORY-ANCHORED (PR #91 review r1 Major 1 / r2 Major 1):
every broker observation that carries a full ``OrderResult`` is serialized
losslessly to canonical JSON (Decimals as canonical strings, datetimes as UTC
ISO strings, fills included, keys sorted) and written to BOTH

- ``execution_orders.last_order_result_json`` (the current row), and
- ``execution_order_state_history.order_result_json`` on the history row
  recording that observation,

in the SAME transaction. The history side is the ANCHOR: on every restore and
every update the current row's snapshot must be byte-identical to the
snapshot on the LAST snapshot-bearing history row, consecutive history
snapshots must satisfy ``validate_order_result_update`` pairwise, each
history snapshot must agree with its own history row (state, broker id --
compared symmetrically, NULL included -- and timestamp) and with the stored
intent, and ``broker_order_id`` must follow the NULL -> value chain across
ALL history rows, where a later value CHANGE is legal exactly as an AUDITED
Order Reissue (Issue #102): the changing row must carry a full snapshot
whose ``superseded_broker_order_ids`` chain records the prior id, and the
pairwise snapshot validation enforces the exact chain-append rule (old
chain + old id lead the new chain). The ``broker_order_id`` column always
holds the CURRENT actionable id; past ids stay auditable in the history
rows and in the snapshots' superseded chains. Rewriting the current row
alone (state, broker id or snapshot) therefore always fails restore-time
validation -- including while the tracked state is UNKNOWN, which is
deliberately NOT an escape hatch: a state-only UNKNOWN never touches the
snapshot, so the current snapshot must still equal the latest anchored one.

Update contract: the ``old`` side of ``validate_order_result_update`` is the
ANCHORED DB snapshot -- never an in-memory copy -- so known fills /
quantities / broker IDs / timestamps cannot be rolled back by a stale
caller, even right after a process restart. A caller-supplied
``previous_result`` is accepted only when it equals the anchored snapshot.
State-only updates (:func:`record_order_state`) are restricted to the one
state that exists precisely because no full ``OrderResult`` could be
obtained: UNKNOWN (e.g. ``BrokerTimeoutError``); a state-only update may
CONFIRM the stored ``broker_order_id`` but can never introduce or change one
-- broker IDs are only ever learned from full ``OrderResult`` snapshots.

``broker_order_id`` is UNIQUE across the WHOLE table (PR #91 review Major 3):
one broker order can never be attached to two execution requests. The
constraint guards writes; restore additionally scans the whole table --
superseded chain ids included (Issue #102) -- and refuses to restore ANY
row while a duplicate exists (a corrupted / constraint-stripped DB must not
be readable back into use).

Every public API -- reads included -- follows one flow (PR #91 review r2
Major 3): open the DB, VERIFY the schema, start a transaction, then
restore/update rows. A DB whose schema fails verification is never read
from and never written to.

UNKNOWN handling (Issue #74): ``unknown`` is stored as-is and keeps the phase
at ``submitting`` -- the broker may or may not hold the order. UNKNOWN rows
are query targets (:func:`list_unknown_orders`); they are resolved ONLY by
recording a later observation through the normal validated update path.
This store never converts UNKNOWN into "not placed" / "rejected" /
"retryable", and re-submission is not modelled at all: ``client_order_id``
is bound 1:1 to the request and immutable (see ``execution_models`` for the
ID design).

Same-state re-observation (PR #87 contract): allowed for every state,
terminal included. Design decision for persistence: a same-state
re-observation IS recorded -- it refreshes ``updated_at_utc`` /
``last_state_reason`` and appends a history row, so reconciliation
observations leave an audit trail. Callers that do not want history rows for
polling no-ops simply skip calling the store.

Schema management (PR #91 review Major 2): :func:`init_db` creates the
schema only into a PRISTINE database -- zero objects in ``sqlite_master``
AND ``user_version = 0``. Anything else (a version-only DB, a
metadata-only DB, a partial schema, a constraint-less lookalike, a foreign
DB) fails with :class:`ExecutionSchemaError` WITHOUT modifying the file --
brand-new, never-released component, so there is deliberately no
legacy-migration path and nothing is auto-completed or silently rebuilt.

Schema verification (PR #91 review r3 Major 1 + Minor 1) is a closed-world
diff of the COMPLETE ``sqlite_master`` object set -- every type, triggers
and views included -- against a pristine reference schema built in memory:

- the reference object set is exhaustive and explicit; the only
  SQLite-internal objects in it are the ones this schema itself creates
  (``sqlite_sequence`` from AUTOINCREMENT, ``sqlite_autoindex_*`` from the
  UNIQUE constraints). ANY other object -- a trigger, a view, an extra
  table or index, whatever its type -- fails verification, because a
  trigger/view can rewrite or delete rows behind the store's back.
- structure (columns, NOT NULL, PK, UNIQUE, FK, indexes) is authoritative
  from the PRAGMA signature (``table_info`` / ``index_list`` /
  ``index_info`` / ``foreign_key_list``) compared against the reference.
- DDL text is compared TOKEN-normalized, not byte-for-byte: quoted
  identifiers, whitespace / newlines, keyword case, comments and redundant
  parentheses are cosmetic in SQLite and are absorbed, so an equivalent DDL
  (e.g. a quoted table name) is accepted. The token comparison is what
  guards the semantics PRAGMAs cannot see -- CHECK expressions foremost
  (string literals inside them stay case-sensitive), plus WITHOUT ROWID /
  AUTOINCREMENT / COLLATE. Token normalization was chosen over CHECK-only
  extraction precisely because those non-CHECK, DDL-only semantics need
  guarding too; anything the normalizer cannot prove equivalent stays a
  mismatch (fails closed).

``execution_request_id`` is explicitly ``NOT NULL`` (a plain TEXT PRIMARY
KEY on a rowid table would admit NULLs) and the orders table is WITHOUT
ROWID. Key invariants (non-empty IDs / reasons, UTC suffix, MARKET/LIMIT x
``requested_price``, state x ``broker_order_id``) are additionally enforced
by DB CHECK constraints as a second guard under the application-level
validation.

Source-signal idempotency (Issue #76): ``signal_id`` is UNIQUE across the
orders table via a dedicated UNIQUE index (schema v3) -- one source signal
produces AT MOST ONE execution request, ever. The index (not an inline
column constraint) was chosen deliberately so the v2 -> v3 migration is
purely ADDITIVE: the table DDL is identical between v2 and v3. The
constraint -- not any pre-SELECT -- is the last line of defence: concurrent
creators race INSERTs inside ``BEGIN IMMEDIATE`` transactions and the loser
gets :class:`ExecutionOrderConflictError`, never a second row. Every
suppressed duplicate (same ``execution_request_id``, same ``signal_id`` or
same ``client_order_id``) is WARN-logged with the execution_request_id, the
signal_id, the existing row's submission_phase / order_state and the
suppression reason before the conflict error is raised. Together with the
1:1 immutable ``client_order_id`` binding and the enter-once
``mark_submitting`` transition this makes re-processing idempotent across
process restarts and concurrent runners. Note the consequence for re-issue:
a NEW intent for an already-registered signal is refused by the DB itself,
so any future re-issue flow (out of scope) must be an explicit
administrative decision, never an implicit retry.

Migration (Issue #76): ``init_db`` migrates a COMPLETE, verified v2 schema
to v3 in ONE ``BEGIN IMMEDIATE`` transaction: duplicate-``signal_id`` scan
-> ``CREATE UNIQUE INDEX`` -> version bump -> full v3 re-verification ->
COMMIT. Existing rows are read back unchanged; nothing is deleted. If the
v2 data already violates the uniqueness (two rows sharing a signal_id) the
migration FAILS EXPLICITLY without modifying the file -- deduplication is a
human decision, not something a store invents. Anything that is neither a
valid v3 nor a valid v2 schema fails exactly as before. Read paths NEVER
migrate (they refuse a v2 DB with a schema error pointing at ``init_db``),
so dry-run / read-only contracts keep their no-side-effect guarantee.

Intent reissue persistence (Issue #95 / #115, schema v5): a REJECTED (or
never-submitted, gate-corrected) intent can be reissued as an EXPLICIT NEW
ExecutionRequest row that records its provenance -- parent request
(``reissued_from_execution_request_id``), ``signal_attempt`` (1-based,
contiguous per signal), ``reissue_kind`` (``broker_rejected`` /
``gate_correction``), ``reissue_initiated_by`` (``manual`` / ``auto``),
``reissue_reason``, a verifiable reference to the Safety Gate decision that
justified a correction (``gate_correction_ref_json``) and a verifiable
reference to the reissue clearance evidence that justified the registration
(``reissue_clearance_json``). The old whole-table ``signal_id`` uniqueness
becomes ``UNIQUE(signal_id, signal_attempt)`` plus
``UNIQUE(reissued_from_execution_request_id)`` (one parent -> at most one
child: chains are linear, never trees). Registration
(:func:`create_reissue_execution_order`) verifies the parent-state
requirement per kind, the chain arithmetic and the child field invariants
against the parent row INSIDE the same ``BEGIN IMMEDIATE`` transaction that
inserts the child; restore-time validation re-runs THE SAME shared rules
(:func:`_reissue_row_problems` / :func:`_reissue_link_problems`), so a chain
that registered successfully always restores successfully -- quantity
exception included (a quantity INCREASE is legal exactly when the parent is
``broker_rejected`` with ``rejection_category = parameter``).

Reissue clearance evidence (Issue #115): the APPEND-ONLY table
``execution_reissue_clearances`` holds per-signal reconciliation
observations as monotonically numbered generations (``status`` =
``observing`` / ``valid`` / ``revoked``, plus the observed account scope,
observation time and the evidence payload). The CURRENT clearance of a
signal is its highest generation; recording a new observation NEVER
rewrites prior rows, so "the evidence that justified X" stays resolvable
forever and a failed invalidation can never resurrect an older "valid"
(the failure leaves the newest recorded row in place -- fail-closed
ordering discipline on top of that is the service layer's job, enabled by
the ``observing`` status). :func:`mark_submitting` refuses submission
authority to any order that HAS a child (``ExecutionOrderSupersededError``)
and, for a reissue child (attempt >= 2), verifies INSIDE its own
transaction that the signal's CURRENT clearance is ``valid``, matches the
caller's submission scope and freshness window, and persists the accepted
evidence reference onto the very history row that grants the authority
(``clearance_json`` -- same shape as the registration reference, so the
registration-time and submission-time evidence are two separately
traceable facts). All evidence references are content-bound
(sha256 of the stored evidence payload / full decision snapshot for the
cross-DB Safety Gate reference, see ``execution/reissue_evidence.py``);
a reference that cannot be resolved and content-verified is fail-closed:
``ExecutionIntegrityError`` on restore, typed errors on registration.

Pre-commit write-back validation (PR #91 review r3 Major 1): schema
verification runs BEFORE the writes, so every write path additionally
re-reads the target execution bundle INSIDE the same transaction after
mutating it and re-validates it BEFORE COMMIT -- full bundle integrity
(current row, history chain, snapshot anchor, broker-id chain, transitions,
reason, updated_at), prior history preserved row-for-row, and the current
row / appended history row holding exactly the intended values. Any
deviation (e.g. a trigger that somehow slipped past schema verification
firing mid-transaction) raises :class:`ExecutionIntegrityError` and ROLLS
BACK the whole transaction: a failed update API call leaves the DB
byte-for-byte as it was, and corrupted state is never committed. There is
no path that validates only after COMMIT.

Restore-time integrity: every read re-validates what it loads -- enum
membership, UTC parseability, non-empty IDs / reasons, the current/history
chain (first entry is created -> pending, links are gapless, every pair is a
legal transition, the last entry matches the current row), phase
consistency, the broker-id chain, the snapshot anchoring described above,
and the DB-wide broker-id uniqueness scan. Snapshot JSON is parsed
STRICTLY: the top-level key set and each fill's key set must match the
expected sets exactly (unknown fields are rejected). A DB edited
out-of-band into an inconsistent state raises
:class:`ExecutionIntegrityError` instead of being trusted. Read helpers
never create the DB file (dry-run safe) and never swallow read errors on an
existing file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from broker.broker_models import (
    Fill,
    OrderResult,
    OrderSide,
    OrderState,
    OrderType,
    RejectionCategory,
    TERMINAL_ORDER_STATES,
    validate_order_result_update,
    validate_order_state_transition,
)
from execution.execution_models import ExecutionRequest, _require_non_empty_str, _require_utc

_LOGGER = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "execution_orders.sqlite3"
BUSY_TIMEOUT_SECONDS = 30

ORDERS_TABLE = "execution_orders"
HISTORY_TABLE = "execution_order_state_history"
CLEARANCES_TABLE = "execution_reissue_clearances"

# Reissue provenance vocabularies (Issue #95 / #115). Closed sets, mirrored
# by DB CHECK constraints.
REISSUE_KIND_BROKER_REJECTED = "broker_rejected"
REISSUE_KIND_GATE_CORRECTION = "gate_correction"
KNOWN_REISSUE_KINDS: frozenset[str] = frozenset(
    {REISSUE_KIND_BROKER_REJECTED, REISSUE_KIND_GATE_CORRECTION}
)
REISSUE_INITIATED_MANUAL = "manual"
REISSUE_INITIATED_AUTO = "auto"
KNOWN_REISSUE_INITIATORS: frozenset[str] = frozenset(
    {REISSUE_INITIATED_MANUAL, REISSUE_INITIATED_AUTO}
)

# Reissue clearance evidence statuses (Issue #115). ``observing`` exists so
# the reconciliation side can DOWNGRADE a signal's clearance before running a
# comparison whose conclusion write might fail: the newest recorded row wins,
# and anything that is not ``valid`` refuses submission (fail-closed).
CLEARANCE_STATUS_OBSERVING = "observing"
CLEARANCE_STATUS_VALID = "valid"
CLEARANCE_STATUS_REVOKED = "revoked"
KNOWN_CLEARANCE_STATUSES: frozenset[str] = frozenset(
    {CLEARANCE_STATUS_OBSERVING, CLEARANCE_STATUS_VALID, CLEARANCE_STATUS_REVOKED}
)

# BELIAL-internal submission lifecycle (deliberately NOT broker OrderState
# members -- see the module docstring).
PHASE_CREATED = "created"
PHASE_SUBMITTING = "submitting"
PHASE_SUBMITTED = "submitted"
KNOWN_PHASES: frozenset[str] = frozenset({PHASE_CREATED, PHASE_SUBMITTING, PHASE_SUBMITTED})

# Broker-confirmed states: observing one of these proves the broker holds (or
# definitively answered about) the order, so the phase becomes ``submitted``.
# PENDING / UNKNOWN prove nothing and leave the phase alone.
_CONFIRMED_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.ACCEPTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
    }
)

# States whose broker semantics require a broker-assigned ID (mirrors the
# OrderResult construction invariant; also enforced by a DB CHECK).
_STATES_REQUIRING_BROKER_ID: frozenset[OrderState] = frozenset(
    {OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED, OrderState.FILLED}
)

_ORDER_COLUMNS: tuple[str, ...] = (
    "execution_request_id",
    "signal_id",
    "strategy_id",
    "symbol",
    "side",
    "order_type",
    "quantity",
    "requested_price",
    "stop_loss_price",
    "take_profit_price",
    "requested_at_utc",
    "client_order_id",
    "submission_phase",
    "order_state",
    "broker_order_id",
    "last_state_reason",
    "last_order_result_json",
    "created_at_utc",
    "updated_at_utc",
    "reissued_from_execution_request_id",
    "signal_attempt",
    "reissue_kind",
    "reissue_initiated_by",
    "reissue_reason",
    "gate_correction_ref_json",
    "reissue_clearance_json",
)
_HISTORY_COLUMNS: tuple[str, ...] = (
    "history_id",
    "execution_request_id",
    "previous_state",
    "new_state",
    "reason",
    "broker_order_id",
    "order_result_json",
    "changed_at_utc",
    "repair_attempt_id",
    "repair_generation",
    "clearance_json",
)
_CLEARANCE_COLUMNS: tuple[str, ...] = (
    "signal_id",
    "clearance_generation",
    "evidence_id",
    "status",
    "account_scope",
    "observed_at_utc",
    "evidence_json",
    "recorded_at_utc",
)
_FENCE_COLUMNS: tuple[str, ...] = (
    "execution_request_id",
    "repair_attempt_id",
    "generation",
    "takeover_id",
    "owner_id",
    "claim_execution_id",
    "updated_at_utc",
)
#: The identity columns a repair commit must match EXACTLY (r4 review): the
#: attempt id and generation alone are NOT enough -- the takeover id and the
#: claim execution id are what distinguish two executions of the same
#: generation, so they are part of the fencing token.
_FENCE_IDENTITY_COLUMNS: tuple[str, ...] = (
    "repair_attempt_id",
    "generation",
    "takeover_id",
    "owner_id",
    "claim_execution_id",
)
REQUIRED_ORDER_COLUMNS: frozenset[str] = frozenset(_ORDER_COLUMNS)
REQUIRED_HISTORY_COLUMNS: frozenset[str] = frozenset(_HISTORY_COLUMNS)

# v1: initial PR #91 schema (snapshot on the current row only).
# v2 (PR #91 review r2): snapshot anchored on history rows
#     (history.order_result_json), NOT NULL + WITHOUT ROWID on the orders
#     primary key. No migration path from v1 -- pre-release schema; a v1 DB
#     fails verification loudly.
# v3 (Issue #76): UNIQUE index on orders.signal_id (one source signal ->
#     at most one execution request). Purely additive over v2; init_db
#     migrates a verified v2 DB in place (see the module docstring).
# v4 (Issue #104): STRUCTURED repair provenance on the history table --
#     ``repair_attempt_id`` + ``repair_generation`` (nullable) plus a
#     partial UNIQUE index, so a reconciliation-repair application is
#     attributable machine-readably (never by parsing the reason string) and
#     one attempt can commit at most one history row -- and the FENCING
#     table ``execution_repair_fences``: one row per order naming the
#     currently authorized (repair_attempt_id, generation);
#     ``record_order_result`` verifies it INSIDE the update transaction, so
#     a worker whose claim was taken over (lease expiry -> generation
#     increment) can never commit a stale repair. Purely additive over v3
#     (ALTER ADD COLUMN x2 + table + indexes); init_db migrates verified
#     v2 / v3 DBs in place.
# v5 (Issue #95 / #115): intent-reissue chains -- REISSUE PROVENANCE columns
#     on the orders table (parent reference, ``signal_attempt`` NOT NULL
#     DEFAULT 1, kind / initiated_by / reason, the gate-correction decision
#     reference and the registration clearance reference), the submission
#     clearance provenance column ``clearance_json`` on the history table,
#     the APPEND-ONLY clearance evidence table
#     ``execution_reissue_clearances`` (starts EMPTY on migration: existing
#     signals need a fresh observation before any reissue -- fail-closed),
#     and the uniqueness re-shape: the whole-table signal_id UNIQUE index is
#     REPLACED by ``UNIQUE(signal_id, signal_attempt)`` plus
#     ``UNIQUE(reissued_from_execution_request_id)`` (linear chains, one
#     parent -> at most one child). ALTER ADD COLUMN + index swap + new
#     table only -- NO table rebuild, NO row rewrites; init_db migrates
#     verified v2 / v3 / v4 DBs in place (older versions chain).
SCHEMA_VERSION = 5
_MIGRATABLE_VERSIONS = (4, 3, 2)  # newest first; older versions chain up

FENCES_TABLE = "execution_repair_fences"

# Strict snapshot JSON shape (PR #91 review r2 Minor 1): the key sets must
# match EXACTLY -- unknown fields are rejected, missing fields are rejected.
# Two deliberate exceptions ("missing = what the snapshot meant back then",
# backward compatibility; writes always include every key):
#
# - Issue #102: snapshots written BEFORE the reissue support lack
#   "superseded_broker_order_ids"; they parse as an empty chain (no reissue
#   had been representable, so the empty chain is exactly what they meant).
# - Issue #114: snapshots written BEFORE rejection classification lack
#   "rejection_category"; a REJECTED snapshot parses as UNCLASSIFIED
#   (fail-closed: never eligible for automatic reissue) and every other
#   state parses as None (the REJECTED <-> required cross-constraint).
_RESULT_JSON_KEYS: frozenset[str] = frozenset(
    {
        "client_order_id",
        "broker_order_id",
        "state",
        "symbol",
        "side",
        "order_type",
        "requested_quantity",
        "filled_quantity",
        "remaining_quantity",
        "requested_price",
        "average_fill_price",
        "updated_at_utc",
        "reject_reason",
        "rejection_category",
        "fills",
        "superseded_broker_order_ids",
    }
)
_PRE_CLASSIFICATION_RESULT_JSON_KEYS: frozenset[str] = _RESULT_JSON_KEYS - {
    "rejection_category"
}
_LEGACY_RESULT_JSON_KEYS: frozenset[str] = _PRE_CLASSIFICATION_RESULT_JSON_KEYS - {
    "superseded_broker_order_ids"
}
_FILL_JSON_KEYS: frozenset[str] = frozenset({"fill_id", "price", "quantity", "filled_at_utc"})

# Reissue evidence reference shapes (Issue #115). STRICT key sets, same
# stance as the snapshot JSON: unknown or missing keys are rejected.
#
# - A CLEARANCE reference points at one row of the same-DB append-only
#   ``execution_reissue_clearances`` table and is CONTENT-BOUND: it names the
#   generation, the evidence id and the sha256 of the stored evidence
#   payload, so a swapped or edited evidence row can never be mistaken for
#   the referenced one. Used both for the registration reference on the
#   child order row and for the submission-time reference persisted on the
#   ``mark_submitting`` history row.
# - A GATE DECISION reference points at one row of the SEPARATE Safety Gate
#   decision DB (path swappable via SAFETY_GATE_DECISION_DB_PATH, local ids
#   reusable) and therefore embeds the FULL decision content snapshot next
#   to the id -- the same identity-plus-content stance the Shadow store
#   takes. Verification against the live decision DB lives in
#   ``execution/reissue_evidence.py``; this store validates the shape and
#   the cross-constraints.
_CLEARANCE_REF_KEYS: frozenset[str] = frozenset(
    {"clearance_generation", "evidence_id", "evidence_sha256"}
)
_GATE_DECISION_REF_KEYS: frozenset[str] = frozenset({"decision_id", "decision"})
_GATE_DECISION_CONTENT_KEYS: frozenset[str] = frozenset(
    {
        "execution_request_id",
        "allowed",
        "checked_at_utc",
        "policy_id",
        "execution_mode",
        "connection_target",
        "rejections",
        "account_id",
        "position_count",
    }
)
_GATE_DECISION_REJECTION_KEYS: frozenset[str] = frozenset({"code", "message"})


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ExecutionStoreError(RuntimeError):
    """Base class for execution store errors."""


class ExecutionSchemaError(ExecutionStoreError):
    """The on-disk schema does not match the expected Issue #74 schema."""


class ExecutionIntegrityError(ExecutionStoreError):
    """Stored data fails restore-time validation (out-of-band corruption)."""


class ExecutionOrderNotFoundError(ExecutionStoreError):
    """No execution order exists for the given execution_request_id."""


class ExecutionOrderConflictError(ExecutionStoreError):
    """An insert/update collided with a UNIQUE constraint
    (execution_request_id / signal_id / client_order_id / broker_order_id)."""


class ExecutionOrderSupersededError(ExecutionStoreError):
    """The order has a reissue child, so it can never gain submission
    authority (Issue #95 / #115).

    Raised inside the ``mark_submitting`` transaction when a row exists
    whose ``reissued_from_execution_request_id`` names this order: the
    intent was explicitly superseded by a correction / reissue, and letting
    the parent submit would create two live intents for one signal. The
    transaction rolls back and the DB is untouched (the service layer maps
    this to the ``intent_superseded`` Safety Gate rejection, Issue #116)."""


class ExecutionClearanceError(ExecutionStoreError):
    """A reissue clearance requirement could not be positively verified
    (Issue #95 / #115 -- ALWAYS fail-closed).

    Raised when a reissue child asks for submission authority without a
    clearance to verify against (missing scope / freshness inputs, no
    evidence recorded for the signal), when the signal's CURRENT clearance
    is not ``valid`` (observing / revoked), stale, or scoped to a different
    account than the submission target, and when a clearance reference
    supplied to registration cannot be resolved and content-verified. The
    transaction rolls back and the DB is untouched; absence of evidence is
    never read as absence of divergence."""


class ExecutionEvidenceReferenceError(ExecutionStoreError):
    """An evidence reference cannot be resolved or content-verified
    (Issue #95 / #115 -- fail-closed).

    Raised by the reference verification helpers
    (``execution/reissue_evidence.py``) when the referenced record does not
    exist in the referenced DB, or exists but its content does not match
    the reference's embedded snapshot / digest -- e.g. after the referenced
    DB file was re-created or swapped and a local sequence id now points at
    a different record. An unverifiable reference is treated as no evidence
    at all."""


class ExecutionRepairFenceConflictError(ExecutionStoreError):
    """The caller's repair fencing token is not the currently authorized one
    (Issue #104 re-review).

    Raised inside the ``record_order_result`` transaction when the supplied
    :class:`RepairFenceToken` does not match the order's
    ``execution_repair_fences`` row in FULL -- attempt id, generation,
    takeover id, owner and claim execution id -- i.e. the claim was taken
    over, a different execution of the same generation holds the fence, or a
    newer attempt owns the order. Also raised by
    :func:`register_repair_fence` when a registration may not take the
    fence. The transaction rolls back and the DB is untouched; the caller
    classifies its work as a typed lost-claim result, never as applied."""


class ExecutionRepairFenceMissingError(ExecutionRepairFenceConflictError):
    """The order carries NO committed repair fence at all (Issue #104
    re-review r7).

    Distinguished from a fence that exists and names somebody else so the
    reconciliation side can report "nothing to resume here" separately from
    "another identity owns this order"."""


class ExecutionStoreBusyError(ExecutionStoreError):
    """The order store's WRITE LOCK could not be taken within the busy
    timeout (Issue #104 re-review r7).

    SQLite serialises writers, so a writer that waits out
    ``BUSY_TIMEOUT_SECONDS`` gets a typed, retryable failure instead of a
    raw ``sqlite3.OperationalError``. Nothing was written and the DB is
    untouched: the caller backs off rather than treating a busy store as a
    broken one. This is the ONLY way a lock wait ends -- there is no
    unbounded wait anywhere."""


class ExecutionRepairFenceBusyError(ExecutionStoreBusyError):
    """The order store stayed write-locked while a repair fence was being
    held still for a takeover step (Issue #104 re-review r7).

    Raised by :func:`repair_fence_promotion_guard` when another writer holds
    the order store long enough that the guard's ``BEGIN IMMEDIATE`` times
    out. Nothing was read under a lock and nothing was written: the caller
    retries later rather than promoting a claim it cannot fence. Distinct
    from :class:`ExecutionRepairFenceConflictError`, which means the fence
    WAS read and belongs to somebody else."""


@dataclass(frozen=True)
class RepairFenceToken:
    """The COMPLETE identity of a repair execution (Issue #104 re-review r4).

    One object carries the identity through both stores, so the fence, the
    state update and the audit row can never drift apart: the reconciliation
    DB's durable takeover intent mints it, :func:`register_repair_fence`
    installs it and :func:`record_order_result` re-verifies it inside its
    own transaction.

    ``takeover_id`` is None exactly at ``generation`` 1 (the original claim,
    which no takeover created) and required for every later generation --
    that is what makes two executions of the same attempt and generation
    distinguishable, which an (attempt id, generation) pair alone cannot do.
    """

    repair_attempt_id: str
    generation: int
    owner_id: str
    claim_execution_id: str
    takeover_id: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.repair_attempt_id, "repair_attempt_id")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError(f"generation must be an int >= 1, got {self.generation!r}")
        _require_non_empty_str(self.owner_id, "owner_id")
        _require_non_empty_str(self.claim_execution_id, "claim_execution_id")
        if self.generation == 1:
            if self.takeover_id is not None:
                raise ValueError(
                    "generation 1 is the original claim and carries no takeover_id"
                )
        else:
            if self.takeover_id is None:
                raise ValueError(
                    f"generation {self.generation} exists only because of a takeover and "
                    "must carry its takeover_id"
                )
            _require_non_empty_str(self.takeover_id, "takeover_id")

    def identity(self) -> tuple:
        return (
            self.repair_attempt_id,
            self.generation,
            self.takeover_id,
            self.owner_id,
            self.claim_execution_id,
        )

    def describe(self) -> str:
        """Stable, secret-free rendering for error messages and audits."""
        return (
            f"(attempt {self.repair_attempt_id!r}, generation {self.generation}, "
            f"takeover {self.takeover_id!r}, claim {self.claim_execution_id!r}, "
            f"owner {self.owner_id!r})"
        )


@dataclass(frozen=True)
class InstalledRepairFence:
    """What :func:`register_repair_fence` COMMITTED -- read back inside its
    own transaction (Issue #104 re-review r5).

    Returning the stored row (rather than echoing the caller's arguments)
    is what makes this value TRUE at the moment it is produced. It is still
    only an OBSERVATION for logging and tests: it is never passed on as
    authorization. The reconciliation side proves ``fence_installed`` by
    re-reading this store's committed row itself inside its own transaction
    (r6 review) -- an in-memory object cannot stand in for durable state,
    because whoever holds the object could equally well have fabricated
    it."""

    execution_request_id: str
    token: RepairFenceToken
    updated_at_utc: datetime

    def __post_init__(self) -> None:
        _require_non_empty_str(self.execution_request_id, "execution_request_id")
        if not isinstance(self.token, RepairFenceToken):
            raise ValueError(f"token must be a RepairFenceToken, got {self.token!r}")
        _require_utc(self.updated_at_utc, "updated_at_utc")


@dataclass(frozen=True)
class GateDecisionReference:
    """A verifiable reference to one persisted Safety Gate decision
    (Issue #115 -- the correction basis of a ``gate_correction`` child).

    The decision rows live in a SEPARATE DB file whose path is
    configurable and whose ``decision_id`` is a local AUTOINCREMENT that a
    re-created file re-uses, so the id alone cannot prove the reference
    still points at the intended record. The reference therefore carries
    the FULL decision content next to the id (the Shadow-store precedent);
    ``execution/reissue_evidence.py`` verifies it against the live decision
    DB and fails closed on any mismatch. ``rejections`` are (code, message)
    string pairs -- kept enum-free so this module stays import-independent
    from the Safety Gate. A correction basis is by definition a rejection:
    ``allowed`` must be False and at least one rejection must be present.
    """

    decision_id: int
    execution_request_id: str
    allowed: bool
    checked_at_utc: datetime
    policy_id: str
    rejections: tuple[tuple[str, str], ...]
    execution_mode: str | None = None
    connection_target: str | None = None
    account_id: str | None = None
    position_count: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.decision_id, bool) or not isinstance(self.decision_id, int) \
                or self.decision_id < 1:
            raise ValueError(f"decision_id must be an int >= 1, got {self.decision_id!r}")
        _require_non_empty_str(self.execution_request_id, "execution_request_id")
        if self.allowed is not False:
            raise ValueError(
                "a gate-correction basis is a rejection: allowed must be False, "
                f"got {self.allowed!r}"
            )
        _require_utc(self.checked_at_utc, "checked_at_utc")
        _require_non_empty_str(self.policy_id, "policy_id")
        if not isinstance(self.rejections, tuple) or not self.rejections:
            raise ValueError("rejections must be a non-empty tuple of (code, message) pairs")
        for entry in self.rejections:
            if (
                not isinstance(entry, tuple)
                or len(entry) != 2
                or not all(isinstance(part, str) and part.strip() for part in entry)
            ):
                raise ValueError(
                    f"each rejection must be a (code, message) pair of non-empty "
                    f"strings, got {entry!r}"
                )
        for name in ("execution_mode", "connection_target", "account_id"):
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_str(value, name)
        if self.position_count is not None and (
            isinstance(self.position_count, bool)
            or not isinstance(self.position_count, int)
            or self.position_count < 0
        ):
            raise ValueError(
                f"position_count must be a non-negative int or None, got "
                f"{self.position_count!r}"
            )

    def to_json(self) -> str:
        """Canonical JSON for the ``gate_correction_ref_json`` column."""
        return json.dumps(
            {
                "decision_id": self.decision_id,
                "decision": {
                    "execution_request_id": self.execution_request_id,
                    "allowed": self.allowed,
                    "checked_at_utc": _iso(self.checked_at_utc),
                    "policy_id": self.policy_id,
                    "rejections": [
                        {"code": code, "message": message}
                        for code, message in self.rejections
                    ],
                    "execution_mode": self.execution_mode,
                    "connection_target": self.connection_target,
                    "account_id": self.account_id,
                    "position_count": self.position_count,
                },
            },
            sort_keys=True,
        )


def gate_decision_reference_from_json(text: str) -> GateDecisionReference:
    """Rebuild a :class:`GateDecisionReference` STRICTLY from its stored
    JSON: exact key sets at every level, and the dataclass re-runs every
    invariant. A tampered or truncated reference fails loudly."""
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) != _GATE_DECISION_REF_KEYS:
        raise ValueError(
            f"gate decision reference keys "
            f"{sorted(data) if isinstance(data, dict) else type(data).__name__} do not "
            f"match the expected set {sorted(_GATE_DECISION_REF_KEYS)}"
        )
    decision = data["decision"]
    if not isinstance(decision, dict) or set(decision) != _GATE_DECISION_CONTENT_KEYS:
        raise ValueError(
            f"gate decision content keys "
            f"{sorted(decision) if isinstance(decision, dict) else type(decision).__name__} "
            f"do not match the expected set {sorted(_GATE_DECISION_CONTENT_KEYS)}"
        )
    rejections = decision["rejections"]
    if not isinstance(rejections, list):
        raise ValueError("gate decision reference 'rejections' must be a list")
    pairs = []
    for entry in rejections:
        if not isinstance(entry, dict) or set(entry) != _GATE_DECISION_REJECTION_KEYS:
            raise ValueError(
                f"gate decision rejection keys "
                f"{sorted(entry) if isinstance(entry, dict) else type(entry).__name__} do "
                f"not match the expected set {sorted(_GATE_DECISION_REJECTION_KEYS)}"
            )
        pairs.append((entry["code"], entry["message"]))
    if not isinstance(decision["checked_at_utc"], str):
        raise ValueError("gate decision reference 'checked_at_utc' must be a string")
    return GateDecisionReference(
        decision_id=data["decision_id"],
        execution_request_id=decision["execution_request_id"],
        allowed=decision["allowed"],
        checked_at_utc=_parse_utc(decision["checked_at_utc"]),
        policy_id=decision["policy_id"],
        rejections=tuple(pairs),
        execution_mode=decision["execution_mode"],
        connection_target=decision["connection_target"],
        account_id=decision["account_id"],
        position_count=decision["position_count"],
    )


def _clearance_ref_to_json(
    *, clearance_generation: int, evidence_id: str, evidence_sha256: str
) -> str:
    return json.dumps(
        {
            "clearance_generation": clearance_generation,
            "evidence_id": evidence_id,
            "evidence_sha256": evidence_sha256,
        },
        sort_keys=True,
    )


def _clearance_ref_from_json(text: str) -> dict:
    """Parse a clearance reference STRICTLY (exact key set, typed values)."""
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) != _CLEARANCE_REF_KEYS:
        raise ValueError(
            f"clearance reference keys "
            f"{sorted(data) if isinstance(data, dict) else type(data).__name__} do not "
            f"match the expected set {sorted(_CLEARANCE_REF_KEYS)}"
        )
    generation = data["clearance_generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError(
            f"clearance reference generation must be an int >= 1, got {generation!r}"
        )
    for field in ("evidence_id", "evidence_sha256"):
        if not isinstance(data[field], str) or not data[field].strip():
            raise ValueError(f"clearance reference {field} must be a non-empty string")
    return data


class ExecutionOrderStateConflictError(ExecutionStoreError):
    """The order's CURRENT tracked state or revision differs from what the
    caller conditioned the update on (Issue #104 compare-and-set guard).

    Raised by :func:`record_order_result` when ``expected_current_state`` /
    ``expected_revision`` is supplied and the row read INSIDE the write
    transaction no longer matches -- i.e. another process changed the order
    (a state change OR a same-state write: revision counts every history
    append) between the caller's read and this update. The transaction is
    rolled back and the DB is untouched; the caller re-reads and decides
    (typically: the same outcome was already applied, or the observation is
    stale)."""


def execution_db_path() -> Path:
    raw = (os.getenv("EXECUTION_ORDER_DB_PATH") or str(DEFAULT_DB_PATH)).strip()
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_utc(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError(f"stored timestamp is not UTC: {text!r}")
    return value


# --- schema (full CREATE SQL + PRAGMA signature are the references) ----------


def _enum_values_sql(values) -> str:
    return "', '".join(sorted(v.value for v in values))


# Shared between the v5 reference CREATE and the v4 -> v5 ALTERs so a
# migrated DB token-normalizes identically to a freshly created one (SQLite
# inserts an added column at the end of the COLUMN list, BEFORE the table
# constraints -- same technique as _HISTORY_REPAIR_COLUMN_DDLS).
def _order_reissue_column_ddls() -> tuple[str, ...]:
    kinds = "', '".join(sorted(KNOWN_REISSUE_KINDS))
    initiators = "', '".join(sorted(KNOWN_REISSUE_INITIATORS))
    return (
        (
            "reissued_from_execution_request_id TEXT "
            f"REFERENCES {ORDERS_TABLE} (execution_request_id) "
            "CHECK (reissued_from_execution_request_id IS NULL "
            "OR trim(reissued_from_execution_request_id) <> '')"
        ),
        "signal_attempt INTEGER NOT NULL DEFAULT 1 CHECK (signal_attempt >= 1)",
        f"reissue_kind TEXT CHECK (reissue_kind IS NULL OR reissue_kind IN ('{kinds}'))",
        (
            "reissue_initiated_by TEXT CHECK (reissue_initiated_by IS NULL "
            f"OR reissue_initiated_by IN ('{initiators}'))"
        ),
        "reissue_reason TEXT CHECK (reissue_reason IS NULL OR trim(reissue_reason) <> '')",
        (
            "gate_correction_ref_json TEXT CHECK (gate_correction_ref_json IS NULL "
            "OR trim(gate_correction_ref_json) <> '')"
        ),
        (
            "reissue_clearance_json TEXT CHECK (reissue_clearance_json IS NULL "
            "OR trim(reissue_clearance_json) <> '')"
        ),
    )


def _create_orders_table_sql(*, version: int = SCHEMA_VERSION) -> str:
    """Full current schema. Decimals are stored as canonical TEXT (never REAL:
    live-order arithmetic must not pick up float error, Issue #73/#74). The
    CHECKs mirror the application-level invariants as a second guard.
    NOT NULL on the PRIMARY KEY is explicit (a rowid-table TEXT PRIMARY KEY
    would admit NULLs) and the table is WITHOUT ROWID. Versions below 5 are
    used only as in-memory migration references and by migration tests."""
    sides = _enum_values_sql(OrderSide)
    types = _enum_values_sql(OrderType)
    states = _enum_values_sql(OrderState)
    phases = "', '".join(sorted(KNOWN_PHASES))
    broker_ack = "', '".join(sorted(s.value for s in _STATES_REQUIRING_BROKER_ID))
    reissue_columns = (
        "".join(f",\n            {ddl}" for ddl in _order_reissue_column_ddls())
        if version >= 5
        else ""
    )
    return f"""
        CREATE TABLE {ORDERS_TABLE} (
            execution_request_id TEXT PRIMARY KEY NOT NULL CHECK (trim(execution_request_id) <> ''),
            signal_id TEXT NOT NULL CHECK (trim(signal_id) <> ''),
            strategy_id TEXT NOT NULL CHECK (trim(strategy_id) <> ''),
            symbol TEXT NOT NULL CHECK (trim(symbol) <> ''),
            side TEXT NOT NULL CHECK (side IN ('{sides}')),
            order_type TEXT NOT NULL CHECK (order_type IN ('{types}')),
            quantity TEXT NOT NULL CHECK (trim(quantity) <> ''),
            requested_price TEXT CHECK (requested_price IS NULL OR trim(requested_price) <> ''),
            stop_loss_price TEXT CHECK (stop_loss_price IS NULL OR trim(stop_loss_price) <> ''),
            take_profit_price TEXT CHECK (take_profit_price IS NULL OR trim(take_profit_price) <> ''),
            requested_at_utc TEXT NOT NULL CHECK (requested_at_utc LIKE '%+00:00'),
            client_order_id TEXT NOT NULL UNIQUE CHECK (trim(client_order_id) <> ''),
            submission_phase TEXT NOT NULL CHECK (submission_phase IN ('{phases}')),
            order_state TEXT CHECK (order_state IS NULL OR order_state IN ('{states}')),
            broker_order_id TEXT UNIQUE CHECK (broker_order_id IS NULL OR trim(broker_order_id) <> ''),
            last_state_reason TEXT CHECK (last_state_reason IS NULL OR trim(last_state_reason) <> ''),
            last_order_result_json TEXT CHECK (last_order_result_json IS NULL OR trim(last_order_result_json) <> ''),
            created_at_utc TEXT NOT NULL CHECK (created_at_utc LIKE '%+00:00'),
            updated_at_utc TEXT NOT NULL CHECK (updated_at_utc LIKE '%+00:00'){reissue_columns},
            CHECK ((submission_phase = '{PHASE_CREATED}') = (order_state IS NULL)),
            CHECK ((order_type = '{OrderType.LIMIT.value}') = (requested_price IS NOT NULL)),
            CHECK (order_state IS NULL OR order_state NOT IN ('{broker_ack}') OR broker_order_id IS NOT NULL)
        ) WITHOUT ROWID
    """


# Shared between the v4 reference CREATE and the v3 -> v4 ALTERs so a migrated
# DB token-normalizes identically to a freshly created one (SQLite appends an
# added column at the end of the column list).
_HISTORY_REPAIR_COLUMN_DDLS = (
    "repair_attempt_id TEXT CHECK (repair_attempt_id IS NULL OR trim(repair_attempt_id) <> '')",
    "repair_generation INTEGER CHECK (repair_generation IS NULL OR repair_generation >= 1)",
)

# v5 (Issue #115): the clearance evidence accepted by mark_submitting is
# persisted on the very history row that grants submission authority --
# the same append-only provenance pattern as the repair columns above.
_HISTORY_CLEARANCE_COLUMN_DDL = (
    "clearance_json TEXT CHECK (clearance_json IS NULL OR trim(clearance_json) <> '')"
)


def _create_history_table_sql(*, version: int = SCHEMA_VERSION) -> str:
    states = _enum_values_sql(OrderState)
    extra_column_ddls: tuple[str, ...] = ()
    if version >= 4:
        extra_column_ddls += _HISTORY_REPAIR_COLUMN_DDLS
    if version >= 5:
        extra_column_ddls += (_HISTORY_CLEARANCE_COLUMN_DDL,)
    extra_columns = "".join(f",\n            {ddl}" for ddl in extra_column_ddls)
    return f"""
        CREATE TABLE {HISTORY_TABLE} (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_request_id TEXT NOT NULL
                REFERENCES {ORDERS_TABLE} (execution_request_id)
                CHECK (trim(execution_request_id) <> ''),
            previous_state TEXT CHECK (previous_state IS NULL OR previous_state IN ('{states}')),
            new_state TEXT NOT NULL CHECK (new_state IN ('{states}')),
            reason TEXT NOT NULL CHECK (trim(reason) <> ''),
            broker_order_id TEXT CHECK (broker_order_id IS NULL OR trim(broker_order_id) <> ''),
            order_result_json TEXT CHECK (order_result_json IS NULL OR trim(order_result_json) <> ''),
            changed_at_utc TEXT NOT NULL CHECK (changed_at_utc LIKE '%+00:00'){extra_columns}
        )
    """


def _create_clearances_table_sql() -> str:
    """The APPEND-ONLY reissue clearance evidence table (Issue #115, v5).

    One row per (signal, generation): the reconciliation side records every
    observation outcome as a NEW generation and never rewrites or deletes a
    prior row, so the evidence a registration or submission referenced stays
    resolvable and content-verifiable forever. The signal's CURRENT
    clearance is its highest generation; ``mark_submitting`` reads it inside
    its own transaction. Starts EMPTY on migration -- an existing signal has
    no clearance until a fresh observation is recorded (fail-closed)."""
    statuses = "', '".join(sorted(KNOWN_CLEARANCE_STATUSES))
    return f"""
        CREATE TABLE {CLEARANCES_TABLE} (
            signal_id TEXT NOT NULL CHECK (trim(signal_id) <> ''),
            clearance_generation INTEGER NOT NULL CHECK (clearance_generation >= 1),
            evidence_id TEXT NOT NULL UNIQUE CHECK (trim(evidence_id) <> ''),
            status TEXT NOT NULL CHECK (status IN ('{statuses}')),
            account_scope TEXT NOT NULL CHECK (trim(account_scope) <> ''),
            observed_at_utc TEXT NOT NULL CHECK (observed_at_utc LIKE '%+00:00'),
            evidence_json TEXT NOT NULL CHECK (trim(evidence_json) <> ''),
            recorded_at_utc TEXT NOT NULL CHECK (recorded_at_utc LIKE '%+00:00'),
            PRIMARY KEY (signal_id, clearance_generation)
        ) WITHOUT ROWID
    """


def _create_fences_table_sql() -> str:
    """The repair FENCING table (Issue #104 re-review, v4).

    One row per order naming the currently authorized repair claim
    (attempt id + fencing generation + claiming execution). Registered /
    taken over by :func:`register_repair_fence`, verified INSIDE the
    ``record_order_result`` transaction. Rows are retained after a repair
    finishes (the last authorized claim is audit-relevant) and are simply
    replaced when a different attempt claims the order -- a stale fence can
    therefore never block future repairs and needs no cleanup."""
    return f"""
        CREATE TABLE {FENCES_TABLE} (
            execution_request_id TEXT PRIMARY KEY NOT NULL
                REFERENCES {ORDERS_TABLE} (execution_request_id)
                CHECK (trim(execution_request_id) <> ''),
            repair_attempt_id TEXT NOT NULL CHECK (trim(repair_attempt_id) <> ''),
            generation INTEGER NOT NULL CHECK (generation >= 1),
            takeover_id TEXT CHECK (takeover_id IS NULL OR trim(takeover_id) <> ''),
            owner_id TEXT NOT NULL CHECK (trim(owner_id) <> ''),
            claim_execution_id TEXT NOT NULL CHECK (trim(claim_execution_id) <> ''),
            updated_at_utc TEXT NOT NULL CHECK (updated_at_utc LIKE '%+00:00'),
            CHECK ((generation = 1) = (takeover_id IS NULL))
        ) WITHOUT ROWID
    """


_ORDERS_STATE_INDEX = f"idx_{ORDERS_TABLE}_state"
_HISTORY_REQUEST_INDEX = f"idx_{HISTORY_TABLE}_request"
# Issue #76: source-signal uniqueness lives in a dedicated UNIQUE index (not
# an inline column constraint) so the v2 -> v3 migration is purely additive:
# the table DDL is byte-identical between the two versions.
_SIGNAL_UNIQUE_INDEX = f"idx_{ORDERS_TABLE}_signal"
_SIGNAL_UNIQUE_INDEX_SQL = (
    f"CREATE UNIQUE INDEX {_SIGNAL_UNIQUE_INDEX} ON {ORDERS_TABLE} (signal_id)"
)
# Issue #104: one repair attempt may commit AT MOST one history row -- the
# partial UNIQUE index is the DB-level guard under the application logic.
_HISTORY_REPAIR_INDEX = f"idx_{HISTORY_TABLE}_repair_attempt"
_HISTORY_REPAIR_INDEX_SQL = (
    f"CREATE UNIQUE INDEX {_HISTORY_REPAIR_INDEX} ON {HISTORY_TABLE} "
    "(repair_attempt_id) WHERE repair_attempt_id IS NOT NULL"
)
# Issue #95 / #115 (v5): the signal-wide UNIQUE index is REPLACED by the
# chain-shaped pair -- one row per (signal, attempt), and one parent may be
# reissued at most once (SQLite UNIQUE indexes treat NULLs as distinct, so
# the many attempt-1 rows with a NULL parent coexist).
_SIGNAL_ATTEMPT_UNIQUE_INDEX = f"idx_{ORDERS_TABLE}_signal_attempt"
_SIGNAL_ATTEMPT_UNIQUE_INDEX_SQL = (
    f"CREATE UNIQUE INDEX {_SIGNAL_ATTEMPT_UNIQUE_INDEX} ON {ORDERS_TABLE} "
    "(signal_id, signal_attempt)"
)
_REISSUED_FROM_UNIQUE_INDEX = f"idx_{ORDERS_TABLE}_reissued_from"
_REISSUED_FROM_UNIQUE_INDEX_SQL = (
    f"CREATE UNIQUE INDEX {_REISSUED_FROM_UNIQUE_INDEX} ON {ORDERS_TABLE} "
    "(reissued_from_execution_request_id)"
)


def _create_index_sql(*, version: int = SCHEMA_VERSION) -> dict[str, str]:
    index_sql = {
        _ORDERS_STATE_INDEX: (
            f"CREATE INDEX {_ORDERS_STATE_INDEX} ON {ORDERS_TABLE} (order_state, updated_at_utc)"
        ),
        _HISTORY_REQUEST_INDEX: (
            f"CREATE INDEX {_HISTORY_REQUEST_INDEX} ON {HISTORY_TABLE} "
            "(execution_request_id, history_id)"
        ),
    }
    if 3 <= version < 5:
        index_sql[_SIGNAL_UNIQUE_INDEX] = _SIGNAL_UNIQUE_INDEX_SQL
    if version >= 4:
        index_sql[_HISTORY_REPAIR_INDEX] = _HISTORY_REPAIR_INDEX_SQL
    if version >= 5:
        index_sql[_SIGNAL_ATTEMPT_UNIQUE_INDEX] = _SIGNAL_ATTEMPT_UNIQUE_INDEX_SQL
        index_sql[_REISSUED_FROM_UNIQUE_INDEX] = _REISSUED_FROM_UNIQUE_INDEX_SQL
    return index_sql


# --- DDL token normalization (PR #91 r3 Minor 1) ------------------------------
# SQLite treats quoted identifiers, whitespace, keyword case, comments and
# redundant parentheses as cosmetic; a byte-for-byte sqlite_master.sql
# comparison would falsely reject an equivalent DDL (e.g. a quoted table
# name). Comparing normalized token sequences absorbs exactly those
# differences while still failing closed on everything else -- a missing or
# altered CHECK expression, a changed string literal (kept case-sensitive),
# a dropped WITHOUT ROWID all change the token sequence.

_DDL_MULTI_CHAR_OPERATORS = ("<>", "<=", ">=", "==", "!=", "||")
_DDL_QUOTE_PAIRS = {'"': '"', "`": "`", "[": "]"}


def _raw_ddl_tokens(sql: str) -> list[str]:
    """Lex DDL into tokens: string literals verbatim (case-sensitive), quoted
    identifiers unquoted + lowercased (identifiers are case-insensitive),
    bare words lowercased, comments dropped, operators split."""
    text = str(sql or "")
    tokens: list[str] = []
    i, length = 0, len(text)
    while i < length:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif text.startswith("--", i):
            newline = text.find("\n", i)
            i = length if newline == -1 else newline + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = length if end == -1 else end + 2
        elif ch == "'":
            j = i + 1
            while j < length:
                if text[j] == "'":
                    if text.startswith("''", j):
                        j += 2
                        continue
                    break
                j += 1
            tokens.append(text[i : j + 1])
            i = j + 1
        elif ch in _DDL_QUOTE_PAIRS:
            close = _DDL_QUOTE_PAIRS[ch]
            j = i + 1
            name: list[str] = []
            while j < length:
                if text[j] == close:
                    if close != "]" and text.startswith(close * 2, j):
                        name.append(close)
                        j += 2
                        continue
                    break
                name.append(text[j])
                j += 1
            tokens.append("".join(name).lower())
            i = j + 1
        elif ch.isalnum() or ch == "_":
            j = i
            while j < length and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(text[i:j].lower())
            i = j
        else:
            for operator in _DDL_MULTI_CHAR_OPERATORS:
                if text.startswith(operator, i):
                    tokens.append(operator)
                    i += len(operator)
                    break
            else:
                tokens.append(ch)
                i += 1
    return tokens


def _ddl_tokens(sql: str) -> tuple[str, ...]:
    """Normalized token sequence with redundant parentheses collapsed.

    Only provably meaning-preserving collapses are applied: a group wrapping
    exactly one atom loses its parentheses, and doubly-nested groups
    ``((...))`` lose one level. An unmatched ``)`` stays a literal token, so
    malformed DDL can never normalize into a prefix of the expected DDL.
    """
    stack: list[list] = [[]]
    for token in _raw_ddl_tokens(sql):
        if token == "(":
            stack.append([])
        elif token == ")" and len(stack) > 1:
            group = stack.pop()
            stack[-1].append(group)
        else:
            stack[-1].append(token)
    while len(stack) > 1:  # unclosed "(" -- close implicitly, keep structure
        group = stack.pop()
        stack[-1].append(group)

    def simplify(node):
        if isinstance(node, str):
            return node
        children = [simplify(child) for child in node]
        while len(children) == 1 and isinstance(children[0], list):
            children = children[0]
        if len(children) == 1 and isinstance(children[0], str):
            return children[0]
        return children

    def flatten(node, out: list[str]) -> None:
        if isinstance(node, str):
            out.append(node)
        else:
            out.append("(")
            for child in node:
                flatten(child, out)
            out.append(")")

    top = simplify(stack[0]) if stack[0] else []
    if isinstance(top, str):
        return (top,)
    flat: list[str] = []
    for child in top:
        flatten(child, flat)
    return tuple(flat)


def _tables_for(version: int) -> tuple[str, ...]:
    tables = [ORDERS_TABLE, HISTORY_TABLE]
    if version >= 4:
        tables.append(FENCES_TABLE)
    if version >= 5:
        tables.append(CLEARANCES_TABLE)
    return tuple(tables)


def _create_schema(connection: sqlite3.Connection, *, version: int = SCHEMA_VERSION) -> None:
    """Create the schema for ``version`` (v2 / v3 / v4 are used only as the
    in-memory migration references and by migration tests; real DBs are
    created at the current version)."""
    connection.execute(_create_orders_table_sql(version=version))
    connection.execute(_create_history_table_sql(version=version))
    if version >= 4:
        connection.execute(_create_fences_table_sql())
    if version >= 5:
        connection.execute(_create_clearances_table_sql())
    for create_sql in _create_index_sql(version=version).values():
        connection.execute(create_sql)
    connection.execute(f"PRAGMA user_version = {version}")


def _structure_signature(connection: sqlite3.Connection, *, version: int = SCHEMA_VERSION) -> dict:
    """Structural signature from the live schema objects (PR #91 r2 Minor 2).

    Built from ``PRAGMA table_info`` / ``index_list`` / ``index_info`` /
    ``foreign_key_list`` -- so verification does not rest on the
    ``sqlite_master`` SQL text alone."""
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
            # (seq, name, unique, origin, partial)
            columns = [
                tuple(info)
                for info in connection.execute(f"PRAGMA index_info({index_row[1]})")
            ]
            index_entries.append((index_row[1], index_row[2], index_row[3], index_row[4], columns))
        signature[f"indexes:{table}"] = sorted(index_entries)
    return signature


_REFERENCE_SCHEMA_CACHE: dict[int, tuple[dict, dict]] = {}


def _reference_schema(version: int = SCHEMA_VERSION) -> tuple[dict, dict]:
    """Pristine reference schema for ``version``, built once in memory.

    Returns ``(objects, signature)``: ``objects`` maps every ``sqlite_master``
    entry -- SQLite-internal ones included (``sqlite_sequence`` from
    AUTOINCREMENT, ``sqlite_autoindex_*`` from the UNIQUE constraints) -- as
    ``(type, name) -> (tbl_name, ddl_tokens | None)``; ``signature`` is the
    PRAGMA structural signature. The v2 reference exists solely so migration
    can prove a DB is EXACTLY a valid v2 schema before touching it."""
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
    """Full structural verification; raises :class:`ExecutionSchemaError`.

    Closed-world check (PR #91 r3 Major 1): the COMPLETE ``sqlite_master``
    object set -- all types, so triggers and views too -- must exactly equal
    the pristine reference set; there is no allow-by-type extraction, only an
    explicit allow-list of the objects this schema itself creates. On top of
    that: the schema version, the token-normalized DDL of every SQL-bearing
    object (PR #91 r3 Minor 1 -- cosmetic differences absorbed, CHECK /
    WITHOUT ROWID / AUTOINCREMENT semantics enforced), and the PRAGMA-derived
    structural signature (authoritative for columns, NOT NULL, PK, UNIQUE,
    FK, indexes). Read-only -- makes no change to the DB. A DB that is a
    valid v2 schema is still refused here (only :func:`init_db` migrates);
    the error says so explicitly.
    """
    problems = _schema_problems(connection)
    if problems:
        for candidate in _MIGRATABLE_VERSIONS:
            if not _schema_problems(connection, version=candidate):
                problems.append(
                    f"the DB is a valid v{candidate} schema; run init_db() to "
                    f"migrate it to v{SCHEMA_VERSION} (reads never migrate)"
                )
                break
        raise ExecutionSchemaError(
            f"execution DB at {db_path} failed schema verification; refusing to touch it: "
            + "; ".join(problems)
        )


def _assert_no_duplicate_broker_ids(connection: sqlite3.Connection) -> None:
    """DB-wide broker_order_id uniqueness scan (PR #91 review Major 3).

    The UNIQUE constraint guards writes; this scan additionally refuses to
    RESTORE anything from a DB that somehow holds duplicates (e.g. a
    constraint-stripped copy). NULLs are not duplicates of each other.

    Order Reissue (Issue #102): a reissued order's CURRENT id lives in the
    ``broker_order_id`` column while its past ids live in the snapshot's
    ``superseded_broker_order_ids`` chain -- the column constraint alone can
    no longer see them. This scan therefore covers EVERY id an order has
    ever lived under (current + superseded, taken from the current row's
    snapshot): one broker order id belonging to two execution requests --
    in either column or chain position -- refuses the whole DB.
    """
    duplicates = [
        row[0]
        for row in connection.execute(
            f"SELECT broker_order_id FROM {ORDERS_TABLE} WHERE broker_order_id IS NOT NULL "
            "GROUP BY broker_order_id HAVING COUNT(*) > 1"
        )
    ]
    if duplicates:
        raise ExecutionIntegrityError(
            f"broker_order_id values assigned to more than one execution order: {duplicates}; "
            "refusing to restore from this DB"
        )
    owners: dict[str, str] = {}
    conflicts: set[str] = set()
    for request_id, broker_id, snapshot_text in connection.execute(
        f"SELECT execution_request_id, broker_order_id, last_order_result_json "
        f"FROM {ORDERS_TABLE} "
        "WHERE broker_order_id IS NOT NULL OR last_order_result_json IS NOT NULL"
    ):
        order_ids = set()
        if broker_id is not None:
            order_ids.add(broker_id)
        if snapshot_text is not None:
            try:
                data = json.loads(snapshot_text)
                chain = data.get("superseded_broker_order_ids", [])
            except (ValueError, AttributeError):
                # unparseable snapshots are rejected in depth by the bundle
                # validation of the row that carries them; this scan only
                # cares about the id sets it CAN read
                chain = []
            if isinstance(chain, list):
                order_ids.update(entry for entry in chain if isinstance(entry, str))
        for order_id in order_ids:
            previous_owner = owners.setdefault(order_id, request_id)
            if previous_owner != request_id:
                conflicts.add(order_id)
    if conflicts:
        raise ExecutionIntegrityError(
            "broker order ids (current or superseded) assigned to more than one "
            f"execution order: {sorted(conflicts)}; refusing to restore from this DB"
        )


def _assert_result_broker_ids_unowned(
    connection: sqlite3.Connection, execution_request_id: str, result: OrderResult
) -> None:
    """Every broker order id the snapshot names — current AND superseded —
    must be free or owned by this very execution order (Issue #102 / PR #112
    review).

    The column UNIQUE constraint only guards the CURRENT id column: a
    superseded chain id colliding with another order's current or superseded
    id (e.g. a multi-hop update learning an intermediate id another request
    already owns, or a new current id sitting in another order's chain)
    would otherwise COMMIT and then brick every later read via
    :func:`_assert_no_duplicate_broker_ids`. Verified INSIDE the update
    transaction, before any row is written, so a refused update rolls back
    with the DB untouched."""
    ids = set(result.all_broker_order_ids)
    if not ids:
        return
    for row in connection.execute(
        f"SELECT execution_request_id, broker_order_id, last_order_result_json "
        f"FROM {ORDERS_TABLE} WHERE execution_request_id != ? "
        "AND (broker_order_id IS NOT NULL OR last_order_result_json IS NOT NULL)",
        (execution_request_id,),
    ):
        owned = set()
        if row["broker_order_id"] is not None:
            owned.add(row["broker_order_id"])
        if row["last_order_result_json"] is not None:
            try:
                chain = json.loads(row["last_order_result_json"]).get(
                    "superseded_broker_order_ids", []
                )
            except (ValueError, AttributeError):
                # rejected in depth by that row's own bundle validation
                chain = []
            if isinstance(chain, list):
                owned.update(entry for entry in chain if isinstance(entry, str))
        conflict = sorted(ids & owned)
        if conflict:
            raise ExecutionOrderConflictError(
                f"broker order ids {conflict} named by the update for execution "
                f"order {execution_request_id!r} are already owned by execution "
                f"order {row['execution_request_id']!r}; broker order ids are "
                "unique across orders (current and superseded alike) and "
                "nothing was changed"
            )


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def _verified_txn(db_path: Path):
    """The single write flow: open -> BEGIN IMMEDIATE -> verify schema ->
    duplicate scan -> row work -> pre-commit write-back validation
    (:func:`_validate_writeback`, called by every writer) -> COMMIT
    (ROLLBACK on any error, so a failed write leaves the DB untouched)."""
    with closing(_connect(db_path)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if not _is_lock_contention(exc):
                raise  # corruption is not contention; never mask it as busy
            raise ExecutionStoreBusyError(
                f"the execution order store at {db_path} stayed write-locked for "
                f"{BUSY_TIMEOUT_SECONDS}s; nothing was written ({type(exc).__name__})"
            ) from exc
        try:
            _verify_schema(connection, db_path)
            _assert_no_duplicate_broker_ids(connection)
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise


@contextmanager
def _verified_read(db_path: Path):
    """The single read flow: open -> BEGIN (consistent view) -> verify schema
    -> duplicate scan -> row restore. Always rolled back (reads write nothing)."""
    with closing(_connect(db_path)) as connection:
        connection.execute("BEGIN")
        try:
            _verify_schema(connection, db_path)
            _assert_no_duplicate_broker_ids(connection)
            yield connection
        finally:
            connection.execute("ROLLBACK")


def _migrate_v2_to_v3(connection: sqlite3.Connection, db_path: Path) -> None:
    """Migrate a fully verified v2 schema to v3 inside the caller's
    transaction (Issue #76): duplicate-``signal_id`` scan -> additive
    ``CREATE UNIQUE INDEX`` -> version bump. Existing rows are untouched;
    nothing is deleted. Pre-existing duplicates make the migration FAIL
    EXPLICITLY (deduplicating released data is a human decision, not
    something a store invents); the caller's rollback then leaves the DB
    byte-for-byte as it was."""
    duplicates = [
        f"{row[0]!r} ({row[1]} rows)"
        for row in connection.execute(
            f"SELECT signal_id, COUNT(*) FROM {ORDERS_TABLE} "
            "GROUP BY signal_id HAVING COUNT(*) > 1 ORDER BY signal_id"
        )
    ]
    if duplicates:
        raise ExecutionSchemaError(
            f"cannot migrate execution DB at {db_path} to schema v{SCHEMA_VERSION}: "
            f"signal_id values held by more than one execution request: "
            f"{', '.join(duplicates)}; the unique-signal constraint cannot be added "
            "safely -- resolve the duplicates explicitly first (nothing was changed)"
        )
    try:
        connection.execute(_SIGNAL_UNIQUE_INDEX_SQL)
    except sqlite3.IntegrityError as exc:  # defence in depth under the scan
        raise ExecutionSchemaError(
            f"cannot migrate execution DB at {db_path} to schema v3: "
            f"unique signal_id index creation failed: {exc} (nothing was changed)"
        ) from exc
    connection.execute("PRAGMA user_version = 3")
    _LOGGER.info(
        "migrated execution DB %s from schema v2 to v3 (added unique signal_id index %s)",
        db_path,
        _SIGNAL_UNIQUE_INDEX,
    )


def _migrate_v3_to_v4(connection: sqlite3.Connection, db_path: Path) -> None:
    """Migrate a fully verified v3 schema to v4 inside the caller's
    transaction (Issue #104): additive ``ALTER TABLE ... ADD COLUMN`` for
    the history provenance columns (SQLite appends each column at the end
    of the column list, exactly where the v4 reference CREATE declares
    them, so the migrated DDL token-normalizes identically), the fencing
    table, the partial UNIQUE index and the version bump. Existing rows are
    untouched and read back with NULL provenance; the fencing table starts
    empty."""
    for column_ddl in _HISTORY_REPAIR_COLUMN_DDLS:
        connection.execute(f"ALTER TABLE {HISTORY_TABLE} ADD COLUMN {column_ddl}")
    connection.execute(_create_fences_table_sql())
    connection.execute(_HISTORY_REPAIR_INDEX_SQL)
    connection.execute("PRAGMA user_version = 4")
    _LOGGER.info(
        "migrated execution DB %s from schema v3 to v4 (added history repair provenance "
        "columns, fencing table %s and index %s)",
        db_path,
        FENCES_TABLE,
        _HISTORY_REPAIR_INDEX,
    )


def _migrate_v4_to_v5(connection: sqlite3.Connection, db_path: Path) -> None:
    """Migrate a fully verified v4 schema to v5 inside the caller's
    transaction (Issue #95 / #115): ``ALTER TABLE ... ADD COLUMN`` for the
    seven orders reissue-provenance columns (``signal_attempt`` is NOT NULL
    DEFAULT 1, so every existing row is backfilled as attempt 1; the other
    six stay NULL = "not a reissue") and the history clearance provenance
    column, creation of the EMPTY clearance evidence table (existing
    signals hold no clearance until a fresh observation is recorded --
    fail-closed), and the uniqueness re-shape: DROP the whole-table signal
    UNIQUE index, CREATE ``UNIQUE(signal_id, signal_attempt)`` and
    ``UNIQUE(reissued_from_execution_request_id)``. SQLite inserts added
    columns at the end of the column list BEFORE the table constraints,
    exactly where the v5 reference CREATE declares them, so the migrated
    DDL token-normalizes identically. No table rebuild, no row rewrites,
    nothing deleted; the index swap cannot fail on released data (the old
    single-column UNIQUE guaranteed no (signal, attempt=1) duplicates)."""
    for column_ddl in _order_reissue_column_ddls():
        connection.execute(f"ALTER TABLE {ORDERS_TABLE} ADD COLUMN {column_ddl}")
    connection.execute(
        f"ALTER TABLE {HISTORY_TABLE} ADD COLUMN {_HISTORY_CLEARANCE_COLUMN_DDL}"
    )
    connection.execute(_create_clearances_table_sql())
    connection.execute(f"DROP INDEX {_SIGNAL_UNIQUE_INDEX}")
    connection.execute(_SIGNAL_ATTEMPT_UNIQUE_INDEX_SQL)
    connection.execute(_REISSUED_FROM_UNIQUE_INDEX_SQL)
    connection.execute("PRAGMA user_version = 5")
    _LOGGER.info(
        "migrated execution DB %s from schema v4 to v5 (added reissue provenance "
        "columns, clearance table %s, replaced index %s with %s + %s)",
        db_path,
        CLEARANCES_TABLE,
        _SIGNAL_UNIQUE_INDEX,
        _SIGNAL_ATTEMPT_UNIQUE_INDEX,
        _REISSUED_FROM_UNIQUE_INDEX,
    )


def init_db(db_path: Path | None = None) -> None:
    """Create the Issue #74 schema, verify an existing one, or migrate a
    valid v2 / v3 / v4 schema to v5 (Issue #76 / #104 / #115); idempotent.

    - PRISTINE DB only (PR #91 review r2 Major 2): the schema is created only
      when ``sqlite_master`` holds ZERO objects AND ``user_version`` is 0. A
      version-only DB, a metadata-only DB (e.g. a stray ``sqlite_sequence``),
      or any other object present means this is NOT a fresh DB.
    - A DB that fully matches the CURRENT schema passes verification.
    - A DB that fully matches a released migratable schema -- and ONLY such
      a DB -- is migrated in place (:func:`_migrate_v2_to_v3` /
      :func:`_migrate_v3_to_v4` / :func:`_migrate_v4_to_v5`; older versions
      chain upward): every step, the version bumps and a full v5
      re-verification commit in ONE transaction, so a failed migration
      leaves no half-migrated schema behind.
    - Anything else: a partial schema, a constraint-less lookalike, an
      unknown version, or a foreign table raises
      :class:`ExecutionSchemaError` WITHOUT modifying the DB (no
      auto-completion, no silent rebuild).
    """
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(db_path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            objects = connection.execute("SELECT name FROM sqlite_master").fetchall()
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if not objects:
                if version != 0:
                    raise ExecutionSchemaError(
                        f"DB at {db_path} is empty but carries user_version {version}; "
                        "not a pristine DB -- refusing to initialize it"
                    )
                _create_schema(connection)
                # pre-commit self-check: the freshly created schema must pass
                # the same closed-world verification before it is committed
                _verify_schema(connection, db_path)
            elif _schema_problems(connection):
                if not _schema_problems(connection, version=4):
                    _migrate_v4_to_v5(connection, db_path)
                elif not _schema_problems(connection, version=3):
                    _migrate_v3_to_v4(connection, db_path)
                    _migrate_v4_to_v5(connection, db_path)
                elif not _schema_problems(connection, version=2):
                    _migrate_v2_to_v3(connection, db_path)
                    _migrate_v3_to_v4(connection, db_path)
                    _migrate_v4_to_v5(connection, db_path)
                else:
                    # neither a valid current nor a valid migratable schema:
                    # fail with the full diagnosis, DB untouched
                    _verify_schema(connection, db_path)
                # the migrated schema must pass full verification pre-COMMIT
                _verify_schema(connection, db_path)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise


# --- OrderResult snapshot serialization --------------------------------------


def _result_to_json(result: OrderResult) -> str:
    """Serialize a full snapshot losslessly and canonically: Decimals as
    canonical strings, datetimes as UTC ISO strings, fills in order, keys
    sorted -- so identical snapshots serialize to identical bytes and the
    history anchor can be compared as a string."""
    def dec(value):
        return None if value is None else str(value)

    return json.dumps(
        {
            "client_order_id": result.client_order_id,
            "broker_order_id": result.broker_order_id,
            "state": result.state.value,
            "symbol": result.symbol,
            "side": result.side.value,
            "order_type": result.order_type.value,
            "requested_quantity": str(result.requested_quantity),
            "filled_quantity": str(result.filled_quantity),
            "remaining_quantity": str(result.remaining_quantity),
            "requested_price": dec(result.requested_price),
            "average_fill_price": dec(result.average_fill_price),
            "updated_at_utc": _iso(result.updated_at_utc),
            "reject_reason": result.reject_reason,
            "rejection_category": (
                None
                if result.rejection_category is None
                else result.rejection_category.value
            ),
            "superseded_broker_order_ids": list(result.superseded_broker_order_ids),
            "fills": [
                {
                    "fill_id": f.fill_id,
                    "price": str(f.price),
                    "quantity": str(f.quantity),
                    "filled_at_utc": _iso(f.filled_at_utc),
                }
                for f in result.fills
            ],
        },
        sort_keys=True,
    )


def _result_from_json(text: str) -> OrderResult:
    """Rebuild the snapshot STRICTLY (PR #91 r2 Minor 1): the top-level key
    set and each fill's key set must match the expected sets exactly, and
    ``OrderResult`` construction re-runs every invariant -- a tampered or
    extended snapshot fails loudly instead of being trusted."""
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) not in (
        _RESULT_JSON_KEYS,
        # pre-Issue #114 snapshots: no classification was representable, so
        # the missing key means "unclassified" for REJECTED (fail-closed)
        # and None for every other state (backward compatibility)
        _PRE_CLASSIFICATION_RESULT_JSON_KEYS,
        # pre-Issue #102 snapshots: additionally no reissue was
        # representable, so the missing chain key means exactly "empty
        # chain" (backward compatibility)
        _LEGACY_RESULT_JSON_KEYS,
    ):
        raise ValueError(
            f"snapshot JSON keys {sorted(data) if isinstance(data, dict) else type(data).__name__} "
            f"do not match the expected set {sorted(_RESULT_JSON_KEYS)}"
        )
    if not isinstance(data["fills"], list):
        raise ValueError("snapshot JSON 'fills' must be a list")
    superseded = data.get("superseded_broker_order_ids", [])
    if not isinstance(superseded, list) or not all(
        isinstance(entry, str) for entry in superseded
    ):
        raise ValueError(
            "snapshot JSON 'superseded_broker_order_ids' must be a list of strings"
        )
    if "rejection_category" in data:
        raw_category = data["rejection_category"]
        if raw_category is None:
            rejection_category = None
        elif isinstance(raw_category, str):
            # an unknown value raises ValueError from the closed enum --
            # a tampered snapshot fails loudly instead of being coerced
            rejection_category = RejectionCategory(raw_category)
        else:
            raise ValueError(
                "snapshot JSON 'rejection_category' must be a string or null"
            )
    else:
        # pre-Issue #114 snapshot (see _RESULT_JSON_KEYS): REJECTED reads
        # back fail-closed as UNCLASSIFIED, every other state as None
        rejection_category = (
            RejectionCategory.UNCLASSIFIED
            if data["state"] == OrderState.REJECTED.value
            else None
        )
    for entry in data["fills"]:
        if not isinstance(entry, dict) or set(entry) != _FILL_JSON_KEYS:
            raise ValueError(
                f"snapshot fill keys {sorted(entry) if isinstance(entry, dict) else type(entry).__name__} "
                f"do not match the expected set {sorted(_FILL_JSON_KEYS)}"
            )

    def dec(value):
        return None if value is None else Decimal(value)

    return OrderResult(
        client_order_id=data["client_order_id"],
        broker_order_id=data["broker_order_id"],
        state=OrderState(data["state"]),
        symbol=data["symbol"],
        side=OrderSide(data["side"]),
        order_type=OrderType(data["order_type"]),
        requested_quantity=Decimal(data["requested_quantity"]),
        filled_quantity=Decimal(data["filled_quantity"]),
        remaining_quantity=Decimal(data["remaining_quantity"]),
        requested_price=dec(data["requested_price"]),
        average_fill_price=dec(data["average_fill_price"]),
        updated_at_utc=_parse_utc(data["updated_at_utc"]),
        fills=tuple(
            Fill(
                fill_id=f["fill_id"],
                price=Decimal(f["price"]),
                quantity=Decimal(f["quantity"]),
                filled_at_utc=_parse_utc(f["filled_at_utc"]),
            )
            for f in data["fills"]
        ),
        reject_reason=data["reject_reason"],
        superseded_broker_order_ids=tuple(superseded),
        rejection_category=rejection_category,
    )


# --- restore-time integrity validation ---------------------------------------


def _integrity(execution_request_id: str, message: str) -> ExecutionIntegrityError:
    return ExecutionIntegrityError(
        f"execution order {execution_request_id!r} failed restore-time validation: {message}"
    )


# --- reissue chain validation (Issue #95 / #115) ------------------------------
# ONE shared implementation for both the registration transaction and the
# restore-time bundle validation: what registration accepts, restore accepts
# -- quantity exception included. Each function returns a list of problem
# strings; the callers wrap them into ValueError (registration) or
# ExecutionIntegrityError (restore).


def _reissue_row_problems(row: dict) -> list[str]:
    """Row-local reissue cross-constraints (no parent needed).

    attempt 1 <=> no parent reference <=> every reissue audit column NULL;
    attempt >= 2 requires ALL of them (and a parseable clearance reference);
    a correction basis is present exactly on ``gate_correction`` children.
    """
    problems: list[str] = []
    attempt = row["signal_attempt"]
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        return [f"signal_attempt must be an int >= 1, got {attempt!r}"]
    parent_id = row["reissued_from_execution_request_id"]
    kind = row["reissue_kind"]
    initiated_by = row["reissue_initiated_by"]
    reason = row["reissue_reason"]
    clearance_ref = row["reissue_clearance_json"]
    correction_ref = row["gate_correction_ref_json"]
    if attempt == 1:
        for field in (
            "reissued_from_execution_request_id",
            "reissue_kind",
            "reissue_initiated_by",
            "reissue_reason",
            "gate_correction_ref_json",
            "reissue_clearance_json",
        ):
            if row[field] is not None:
                problems.append(f"attempt 1 must not carry {field} ({row[field]!r})")
        return problems
    if not isinstance(parent_id, str) or not parent_id.strip():
        problems.append(f"attempt {attempt} requires a parent reference")
    elif parent_id == row["execution_request_id"]:
        problems.append("an order cannot be its own reissue parent")
    if kind not in KNOWN_REISSUE_KINDS:
        problems.append(f"attempt {attempt} requires reissue_kind, got {kind!r}")
    if initiated_by not in KNOWN_REISSUE_INITIATORS:
        problems.append(
            f"attempt {attempt} requires reissue_initiated_by, got {initiated_by!r}"
        )
    elif (
        kind == REISSUE_KIND_GATE_CORRECTION
        and initiated_by != REISSUE_INITIATED_MANUAL
    ):
        problems.append(
            "a gate correction is a manual judgement: reissue_initiated_by must be "
            f"{REISSUE_INITIATED_MANUAL!r}, got {initiated_by!r}"
        )
    if not isinstance(reason, str) or not reason.strip():
        problems.append(f"attempt {attempt} requires a non-empty reissue_reason")
    if clearance_ref is None:
        problems.append(f"attempt {attempt} requires a registration clearance reference")
    else:
        try:
            _clearance_ref_from_json(clearance_ref)
        except (ValueError, TypeError) as exc:
            problems.append(f"registration clearance reference is invalid: {exc}")
    if (correction_ref is not None) != (kind == REISSUE_KIND_GATE_CORRECTION):
        problems.append(
            "a gate-correction basis must be recorded exactly when reissue_kind is "
            f"{REISSUE_KIND_GATE_CORRECTION!r} (kind {kind!r}, basis "
            f"{'present' if correction_ref is not None else 'absent'})"
        )
    elif correction_ref is not None:
        try:
            reference = gate_decision_reference_from_json(correction_ref)
        except (ValueError, TypeError) as exc:
            problems.append(f"gate-correction reference is invalid: {exc}")
        else:
            if reference.execution_request_id != parent_id:
                problems.append(
                    "the gate-correction basis must judge the parent request "
                    f"{parent_id!r}, but judges {reference.execution_request_id!r}"
                )
    return problems


def _parent_rejection_category(parent: dict) -> RejectionCategory | None:
    """The parent's rejection category, from its stored snapshot (None when
    no snapshot is readable -- the quantity exception then never applies:
    fail-closed)."""
    snapshot_text = parent["last_order_result_json"]
    if snapshot_text is None:
        return None
    try:
        return _result_from_json(snapshot_text).rejection_category
    except Exception:
        # an unreadable parent snapshot is rejected in depth when the parent
        # itself is restored; for the child's quantity rule it simply
        # provides no exception
        return None


def _reissue_link_problems(
    parent: dict, child: dict, parent_category: RejectionCategory | None
) -> list[str]:
    """Parent-child invariants, verified against the PARENT ROW -- the one
    shared rule set for registration and restore (Issue #115).

    - the parent-state requirement per ``reissue_kind``,
    - chain arithmetic (same signal, attempt = parent + 1),
    - identity immutability (signal / strategy / symbol / side),
    - time monotonicity (``requested_at_utc`` never goes backwards),
    - the quantity rule: never above the parent, EXCEPT a
      ``broker_rejected`` parent whose ``rejection_category`` is
      ``parameter`` (the one sanctioned increase, Issue #95),
    - ``auto`` reissues must repeat the parent's order parameters exactly.
    """
    problems: list[str] = []
    kind = child["reissue_kind"]
    if child["reissued_from_execution_request_id"] != parent["execution_request_id"]:
        problems.append(
            f"child parent reference {child['reissued_from_execution_request_id']!r} "
            f"does not name the parent row {parent['execution_request_id']!r}"
        )
    if kind == REISSUE_KIND_BROKER_REJECTED:
        if parent["order_state"] != OrderState.REJECTED.value:
            problems.append(
                f"a {REISSUE_KIND_BROKER_REJECTED!r} reissue requires the parent in "
                f"order_state {OrderState.REJECTED.value!r}, but the parent is in "
                f"phase {parent['submission_phase']!r} / state {parent['order_state']!r}"
            )
    elif kind == REISSUE_KIND_GATE_CORRECTION:
        if parent["submission_phase"] != PHASE_CREATED or parent["order_state"] is not None:
            problems.append(
                f"a {REISSUE_KIND_GATE_CORRECTION!r} correction requires the parent in "
                f"phase {PHASE_CREATED!r} with no order state (never submitted), but "
                f"the parent is in phase {parent['submission_phase']!r} / state "
                f"{parent['order_state']!r}"
            )
    if child["signal_id"] != parent["signal_id"]:
        problems.append(
            f"child signal_id {child['signal_id']!r} differs from parent signal_id "
            f"{parent['signal_id']!r}"
        )
    if child["signal_attempt"] != parent["signal_attempt"] + 1:
        problems.append(
            f"child signal_attempt {child['signal_attempt']} must be exactly the "
            f"parent's attempt {parent['signal_attempt']} + 1"
        )
    for field in ("strategy_id", "symbol", "side"):
        if child[field] != parent[field]:
            problems.append(
                f"identity field {field} may not change in a reissue: parent "
                f"{parent[field]!r} -> child {child[field]!r}"
            )
    try:
        parent_requested_at = _parse_utc(parent["requested_at_utc"])
        child_requested_at = _parse_utc(child["requested_at_utc"])
    except ValueError as exc:
        problems.append(f"unparseable requested_at_utc: {exc}")
    else:
        if child_requested_at < parent_requested_at:
            problems.append(
                f"child requested_at_utc {child['requested_at_utc']} precedes the "
                f"parent's {parent['requested_at_utc']} (time never goes backwards)"
            )
    try:
        parent_quantity = Decimal(parent["quantity"])
        child_quantity = Decimal(child["quantity"])
    except (InvalidOperation, TypeError) as exc:
        problems.append(f"unparseable quantity: {exc}")
    else:
        increase_allowed = (
            kind == REISSUE_KIND_BROKER_REJECTED
            and parent_category is RejectionCategory.PARAMETER
        )
        if child_quantity > parent_quantity and not increase_allowed:
            problems.append(
                f"child quantity {child['quantity']} exceeds the parent's "
                f"{parent['quantity']}; an increase is allowed only when the parent "
                f"is {REISSUE_KIND_BROKER_REJECTED!r} with rejection_category "
                f"{RejectionCategory.PARAMETER.value!r} (parent category: "
                f"{'none' if parent_category is None else parent_category.value})"
            )
    if child["reissue_initiated_by"] == REISSUE_INITIATED_AUTO:
        def _decimal_or_none(value):
            return None if value is None else Decimal(value)

        for field in (
            "quantity",
            "requested_price",
            "stop_loss_price",
            "take_profit_price",
        ):
            try:
                same = _decimal_or_none(child[field]) == _decimal_or_none(parent[field])
            except (InvalidOperation, TypeError):
                same = False
            if not same:
                problems.append(
                    f"an auto reissue must repeat the parent's order parameters "
                    f"exactly: {field} parent {parent[field]!r} -> child "
                    f"{child[field]!r}"
                )
        if child["order_type"] != parent["order_type"]:
            problems.append(
                f"an auto reissue must repeat the parent's order parameters exactly: "
                f"order_type parent {parent['order_type']!r} -> child "
                f"{child['order_type']!r}"
            )
    return problems


def _resolve_clearance_ref(
    connection: sqlite3.Connection, signal_id: str, ref: dict
) -> list[str]:
    """Content-verify a clearance reference against the same-DB append-only
    evidence table. Returns problems (empty = the reference resolves to the
    intended record). Fail-closed: missing row, different evidence id or a
    different payload digest all disqualify the reference."""
    row = connection.execute(
        f"SELECT evidence_id, evidence_json FROM {CLEARANCES_TABLE} "
        "WHERE signal_id = ? AND clearance_generation = ?",
        (signal_id, ref["clearance_generation"]),
    ).fetchone()
    if row is None:
        return [
            f"clearance reference (signal {signal_id!r}, generation "
            f"{ref['clearance_generation']}) does not resolve to a recorded evidence row"
        ]
    problems = []
    if row["evidence_id"] != ref["evidence_id"]:
        problems.append(
            f"clearance reference evidence_id {ref['evidence_id']!r} does not match "
            f"the recorded evidence row ({row['evidence_id']!r})"
        )
    if _sha256_text(row["evidence_json"]) != ref["evidence_sha256"]:
        problems.append(
            "clearance reference evidence_sha256 does not match the recorded "
            "evidence payload (the referenced record is not the intended one)"
        )
    return problems


def _validate_reissue_chain(
    connection: sqlite3.Connection, row: dict, history: list[dict]
) -> None:
    """Restore-time chain / evidence validation for one order bundle
    (Issue #115) -- runs inside the caller's transaction, AFTER
    :func:`_validate_bundle`.

    Re-runs the SAME shared rules as registration against the parent row,
    verifies the registration clearance reference resolves and
    content-matches, and pins the submission clearance provenance: a
    reissue child that entered ``submitting`` carries the accepted evidence
    reference on its FIRST history row (and nowhere else); an attempt-1
    order never carries one. Any violation is an out-of-band edit and
    raises :class:`ExecutionIntegrityError`.
    """
    request_id = row["execution_request_id"]
    problems = _reissue_row_problems(row)
    if problems:
        raise _integrity(request_id, "; ".join(problems))
    is_child = row["signal_attempt"] >= 2

    for position, entry in enumerate(history):
        clearance_json = entry["clearance_json"]
        if position == 0 and is_child:
            if clearance_json is None:
                raise _integrity(
                    request_id,
                    "a reissue child's submission-granting history row must record "
                    "the accepted clearance evidence, but carries none",
                )
            try:
                submission_ref = _clearance_ref_from_json(clearance_json)
            except (ValueError, TypeError) as exc:
                raise _integrity(
                    request_id, f"submission clearance reference is invalid: {exc}"
                ) from exc
            ref_problems = _resolve_clearance_ref(
                connection, row["signal_id"], submission_ref
            )
            if ref_problems:
                raise _integrity(request_id, "; ".join(ref_problems))
        elif clearance_json is not None:
            raise _integrity(
                request_id,
                f"history row {entry['history_id']} carries clearance provenance, "
                "which belongs only on the submission-granting first history row "
                "of a reissue child",
            )

    if not is_child:
        return

    registration_ref = _clearance_ref_from_json(row["reissue_clearance_json"])
    ref_problems = _resolve_clearance_ref(connection, row["signal_id"], registration_ref)
    if ref_problems:
        raise _integrity(request_id, "; ".join(ref_problems))

    parent = connection.execute(
        f"SELECT * FROM {ORDERS_TABLE} WHERE execution_request_id = ?",
        (row["reissued_from_execution_request_id"],),
    ).fetchone()
    if parent is None:
        raise _integrity(
            request_id,
            f"parent execution order {row['reissued_from_execution_request_id']!r} "
            "does not exist",
        )
    parent = dict(parent)
    link_problems = _reissue_link_problems(
        parent, row, _parent_rejection_category(parent)
    )
    if link_problems:
        raise _integrity(request_id, "; ".join(link_problems))


def _validate_request_columns(row: dict) -> None:
    """Field-level validation of the stored intent (enums, decimals, UTC)."""
    request_id = row["execution_request_id"]
    for field in ("execution_request_id", "signal_id", "strategy_id", "symbol", "client_order_id"):
        if not isinstance(row[field], str) or not row[field].strip():
            raise _integrity(request_id, f"{field} is empty or not a string")
    try:
        OrderSide(row["side"])
        OrderType(row["order_type"])
    except ValueError as exc:
        raise _integrity(request_id, str(exc)) from exc
    if row["submission_phase"] not in KNOWN_PHASES:
        raise _integrity(request_id, f"unknown submission_phase {row['submission_phase']!r}")
    try:
        for field in ("quantity", "requested_price", "stop_loss_price", "take_profit_price"):
            if row[field] is not None:
                Decimal(row[field])
        for field in ("requested_at_utc", "created_at_utc", "updated_at_utc"):
            _parse_utc(row[field])
    except (InvalidOperation, ValueError) as exc:
        raise _integrity(request_id, f"unparseable stored value: {exc}") from exc


def _validate_history_snapshot(row: dict, entry: dict, snapshot: OrderResult) -> None:
    """One snapshot-bearing history row must agree with itself and the intent.

    broker_order_id is compared SYMMETRICALLY, NULL included: the snapshot's
    broker id and the history row's broker id must be exactly equal.
    """
    request_id = row["execution_request_id"]
    if snapshot.state.value != entry["new_state"]:
        raise _integrity(
            request_id,
            f"history row {entry['history_id']} snapshot state {snapshot.state.value!r} "
            f"does not match its new_state {entry['new_state']!r}",
        )
    if snapshot.broker_order_id != entry["broker_order_id"]:
        raise _integrity(
            request_id,
            f"history row {entry['history_id']} snapshot broker_order_id "
            f"{snapshot.broker_order_id!r} does not match its broker_order_id "
            f"{entry['broker_order_id']!r}",
        )
    if _iso(snapshot.updated_at_utc) != entry["changed_at_utc"]:
        raise _integrity(
            request_id,
            f"history row {entry['history_id']} snapshot timestamp does not match changed_at_utc",
        )
    if snapshot.client_order_id != row["client_order_id"]:
        raise _integrity(request_id, "snapshot client_order_id does not match the order")
    stored_requested_price = (
        None if row["requested_price"] is None else Decimal(row["requested_price"])
    )
    if (
        snapshot.symbol != row["symbol"]
        or snapshot.side.value != row["side"]
        or snapshot.order_type.value != row["order_type"]
        or snapshot.requested_quantity != Decimal(row["quantity"])
        or snapshot.requested_price != stored_requested_price
    ):
        raise _integrity(request_id, "snapshot identity fields do not match the stored request")


def _validate_bundle(row: dict, history: list[dict]) -> None:
    """Cross-validate the current row, its history chain and the snapshot anchor.

    Restore-time guard (PR #91 review): a DB edited out-of-band into an
    inconsistent state must be rejected on load, not trusted. The full
    OrderResult is anchored to the LAST snapshot-bearing HISTORY row, so
    rewriting the current row alone is always detected -- the tracked state
    being UNKNOWN is not an exception.
    """
    request_id = row["execution_request_id"]
    _validate_request_columns(row)

    state_text = row["order_state"]
    phase = row["submission_phase"]
    if (phase == PHASE_CREATED) != (state_text is None):
        raise _integrity(request_id, f"phase {phase!r} inconsistent with order_state {state_text!r}")
    if (phase == PHASE_CREATED) != (not history):
        raise _integrity(request_id, f"phase {phase!r} inconsistent with {len(history)} history rows")
    if phase == PHASE_CREATED:
        if row["last_order_result_json"] is not None:
            raise _integrity(request_id, "created order carries an order-result snapshot")
        if row["broker_order_id"] is not None:
            raise _integrity(request_id, "created order carries a broker_order_id")
        return

    try:
        current_state = OrderState(state_text)
    except ValueError as exc:
        raise _integrity(request_id, str(exc)) from exc

    # history chain: gapless links, legal transitions, broker-id chain,
    # snapshot anchoring, sane content
    previous: dict | None = None
    previous_snapshot: OrderResult | None = None
    last_snapshot_json: str | None = None
    for entry in history:
        try:
            new_state = OrderState(entry["new_state"])
            prev_state = None if entry["previous_state"] is None else OrderState(entry["previous_state"])
            changed_at = _parse_utc(entry["changed_at_utc"])
        except ValueError as exc:
            raise _integrity(request_id, f"history row {entry['history_id']}: {exc}") from exc
        if not isinstance(entry["reason"], str) or not entry["reason"].strip():
            raise _integrity(request_id, f"history row {entry['history_id']} has an empty reason")
        repair_attempt_id = entry["repair_attempt_id"]
        repair_generation = entry["repair_generation"]
        if (repair_attempt_id is None) != (repair_generation is None):
            raise _integrity(
                request_id,
                f"history row {entry['history_id']} carries repair provenance with "
                "attempt id and generation out of lockstep",
            )
        if repair_attempt_id is not None:
            if not isinstance(repair_attempt_id, str) or not repair_attempt_id.strip():
                raise _integrity(
                    request_id,
                    f"history row {entry['history_id']} carries an empty repair_attempt_id",
                )
            if (
                isinstance(repair_generation, bool)
                or not isinstance(repair_generation, int)
                or repair_generation < 1
            ):
                raise _integrity(
                    request_id,
                    f"history row {entry['history_id']} carries an invalid "
                    f"repair_generation {repair_generation!r}",
                )
            if entry["order_result_json"] is None:
                # a repair application is always a full-snapshot observation
                # recorded through record_order_result
                raise _integrity(
                    request_id,
                    f"history row {entry['history_id']} claims repair provenance "
                    f"{repair_attempt_id!r} but carries no order-result snapshot",
                )
        if previous is None:
            if prev_state is not None or new_state is not OrderState.PENDING:
                raise _integrity(
                    request_id,
                    "first history row must be the created -> pending entry, got "
                    f"{entry['previous_state']!r} -> {entry['new_state']!r}",
                )
            if entry["broker_order_id"] is not None:
                raise _integrity(
                    request_id, "the created -> pending entry cannot carry a broker_order_id"
                )
        else:
            if entry["previous_state"] != previous["new_state"]:
                raise _integrity(
                    request_id,
                    f"history chain break at row {entry['history_id']}: previous_state "
                    f"{entry['previous_state']!r} != prior new_state {previous['new_state']!r}",
                )
            try:
                validate_order_state_transition(OrderState(previous["new_state"]), new_state)
            except ValueError as exc:
                raise _integrity(request_id, f"history row {entry['history_id']}: {exc}") from exc
            if changed_at < _parse_utc(previous["changed_at_utc"]):
                raise _integrity(
                    request_id, f"history row {entry['history_id']} goes back in time"
                )
            # broker-id chain: NULL -> value once; afterwards the value may
            # only move as an AUDITED Order Reissue (Issue #102) -- the row
            # must carry a full snapshot whose superseded chain contains the
            # prior id. The exact chain-append legality is enforced by the
            # pairwise validate_order_result_update below; this column-level
            # guard additionally pins that a state-only row can never move
            # the id and that the supersession is recorded, not implicit.
            prior_broker = previous["broker_order_id"]
            if prior_broker is not None and entry["broker_order_id"] != prior_broker:
                snapshot_text = entry["order_result_json"]
                if snapshot_text is None:
                    raise _integrity(
                        request_id,
                        f"history row {entry['history_id']} changes broker_order_id "
                        f"{prior_broker!r} -> {entry['broker_order_id']!r} without an "
                        "order-result snapshot proving the reissue",
                    )
                try:
                    chain = tuple(
                        json.loads(snapshot_text).get("superseded_broker_order_ids", [])
                    )
                except (ValueError, AttributeError):
                    chain = ()
                if prior_broker not in chain:
                    raise _integrity(
                        request_id,
                        f"history row {entry['history_id']} changes broker_order_id "
                        f"{prior_broker!r} -> {entry['broker_order_id']!r} but its "
                        "snapshot does not record the prior id as superseded",
                    )

        if entry["order_result_json"] is None:
            # snapshot-less rows are exactly: the initial pending entry and
            # state-only UNKNOWN records -- nothing else may lack a snapshot
            if previous is not None and new_state is not OrderState.UNKNOWN:
                raise _integrity(
                    request_id,
                    f"history row {entry['history_id']} ({new_state.value!r}) lacks the "
                    "order-result snapshot that produced it",
                )
        else:
            try:
                snapshot = _result_from_json(entry["order_result_json"])
            except Exception as exc:
                raise _integrity(
                    request_id,
                    f"history row {entry['history_id']} snapshot is invalid: {exc}",
                ) from exc
            _validate_history_snapshot(row, entry, snapshot)
            if previous_snapshot is not None:
                try:
                    validate_order_result_update(previous_snapshot, snapshot)
                except ValueError as exc:
                    raise _integrity(
                        request_id,
                        f"history row {entry['history_id']} snapshot violates the "
                        f"update contract against the prior snapshot: {exc}",
                    ) from exc
            previous_snapshot = snapshot
            last_snapshot_json = entry["order_result_json"]
        previous = entry

    last = history[-1]
    if last["new_state"] != current_state.value:
        raise _integrity(
            request_id,
            f"current order_state {current_state.value!r} does not match last history "
            f"new_state {last['new_state']!r}",
        )
    if last["broker_order_id"] != row["broker_order_id"]:
        raise _integrity(
            request_id,
            f"current broker_order_id {row['broker_order_id']!r} does not match last history "
            f"broker_order_id {last['broker_order_id']!r}",
        )
    if last["changed_at_utc"] != row["updated_at_utc"]:
        raise _integrity(
            request_id,
            f"updated_at_utc {row['updated_at_utc']!r} does not match last history "
            f"changed_at_utc {last['changed_at_utc']!r}",
        )

    # THE ANCHOR (PR #91 r2 Major 1): the current row's snapshot must be
    # byte-identical to the last snapshot recorded in history -- UNKNOWN
    # included. Swapping the current row's snapshot for an older one is
    # therefore always detected against the append-validated history.
    if row["last_order_result_json"] != last_snapshot_json:
        raise _integrity(
            request_id,
            "current last_order_result_json does not match the last snapshot anchored "
            "in the state history",
        )

    confirmed_seen = any(h["new_state"] in {s.value for s in _CONFIRMED_STATES} for h in history)
    if (phase == PHASE_SUBMITTED) != confirmed_seen:
        raise _integrity(
            request_id,
            f"phase {phase!r} inconsistent with confirmed observations in history",
        )
    if row["last_state_reason"] is None or not str(row["last_state_reason"]).strip():
        raise _integrity(request_id, "last_state_reason is empty for a submitted-phase order")


def _fetch_and_validate(
    connection: sqlite3.Connection, execution_request_id: str
) -> tuple[dict, list[dict]] | None:
    """Load one order row + its full history in the CALLER's transaction and
    validate the bundle. Returns None only when neither exists."""
    row = connection.execute(
        f"SELECT * FROM {ORDERS_TABLE} WHERE execution_request_id = ?",
        (execution_request_id,),
    ).fetchone()
    history = [
        dict(h)
        for h in connection.execute(
            f"SELECT * FROM {HISTORY_TABLE} WHERE execution_request_id = ? "
            "ORDER BY history_id ASC",
            (execution_request_id,),
        ).fetchall()
    ]
    if row is None:
        if history:
            raise _integrity(execution_request_id, "history rows exist without an order row")
        return None
    row = dict(row)
    _validate_bundle(row, history)
    _validate_reissue_chain(connection, row, history)
    # The order's REVISION (Issue #104): the count of history rows. Every
    # write path appends exactly one history row (enforced by the pre-commit
    # write-back validation), so the revision increments on EVERY change --
    # state changes, same-state re-observations, snapshot/broker-id/timestamp
    # updates alike -- without a schema column. It is derived inside the same
    # transaction as the row, so callers get a consistent (row, revision)
    # pair to condition a later compare-and-set on.
    row["revision"] = len(history)
    return row, history


def _anchored_snapshot(history: list[dict]) -> OrderResult | None:
    """The last snapshot anchored in history (validated by _validate_bundle)."""
    for entry in reversed(history):
        if entry["order_result_json"] is not None:
            return _result_from_json(entry["order_result_json"])
    return None


def _validate_writeback(
    connection: sqlite3.Connection,
    execution_request_id: str,
    *,
    prior_history: list[dict],
    expected_row: dict,
    expected_entry: dict | None,
) -> dict:
    """Pre-commit re-validation of the written bundle (PR #91 r3 Major 1).

    Schema verification runs BEFORE the row work, so the written outcome
    itself is re-read INSIDE the same transaction and checked BEFORE COMMIT:

    - full bundle integrity (:func:`_validate_bundle`: current row, history
      chain, snapshot anchor, broker-id chain, transitions, reason,
      updated_at),
    - the prior history rows preserved row-for-row,
    - the current row holding EXACTLY the intended values,
    - the appended history row (if any) holding EXACTLY the intended values.

    Any deviation -- e.g. a trigger that slipped past schema verification
    firing mid-transaction -- raises :class:`ExecutionIntegrityError`, and
    the caller's transaction ROLLS BACK: corrupted state is never committed.
    Returns the validated current row, so callers can return it without a
    separate post-commit read.
    """
    bundle = _fetch_and_validate(connection, execution_request_id)
    if bundle is None:
        raise _integrity(
            execution_request_id, "row vanished inside the write transaction before commit"
        )
    row, history = bundle
    if row != expected_row:
        changed = sorted(
            field for field in expected_row if row.get(field) != expected_row[field]
        )
        raise _integrity(
            execution_request_id,
            f"pre-commit check: current row does not hold the intended values "
            f"(unexpected side effect on {changed}); rolling back",
        )
    expected_len = len(prior_history) + (0 if expected_entry is None else 1)
    if len(history) != expected_len:
        raise _integrity(
            execution_request_id,
            f"pre-commit check: history holds {len(history)} rows, expected {expected_len} "
            "(unexpected side effect); rolling back",
        )
    if history[: len(prior_history)] != prior_history:
        raise _integrity(
            execution_request_id,
            "pre-commit check: pre-existing history rows were modified inside the write "
            "transaction; rolling back",
        )
    if expected_entry is not None:
        appended = {field: history[-1][field] for field in expected_entry}
        if appended != expected_entry:
            raise _integrity(
                execution_request_id,
                "pre-commit check: the appended history row does not hold the intended "
                "values; rolling back",
            )
    return row


# --- write paths -------------------------------------------------------------


def _classify_create_conflicts(
    connection: sqlite3.Connection, record: dict
) -> list[tuple[str, str, dict]]:
    """Return every expected UNIQUE collision for a failed create INSERT.

    An ``IntegrityError`` is not necessarily a duplicate (CHECK / NOT NULL
    and future constraints use the same exception type).  The caller may
    translate the error to ``ExecutionOrderConflictError`` only when this
    lookup proves that at least one of the three create-time identity keys
    already belongs to a persisted row.  Multiple keys can collide with
    different rows, so retain all matches instead of reporting whichever key
    happens to be checked first.
    """
    conflicts: list[tuple[str, str, dict]] = []
    checks: list[tuple[str, str, str, tuple]] = [
        (
            "execution_request_id",
            "execution_request_id is already persisted",
            "execution_request_id = ?",
            (record["execution_request_id"],),
        ),
        (
            "signal_id",
            f"source signal already produced attempt {record['signal_attempt']}",
            "signal_id = ? AND signal_attempt = ?",
            (record["signal_id"], record["signal_attempt"]),
        ),
        (
            "client_order_id",
            "client_order_id is already bound to an execution request",
            "client_order_id = ?",
            (record["client_order_id"],),
        ),
    ]
    if record["reissued_from_execution_request_id"] is not None:
        checks.append(
            (
                "reissued_from_execution_request_id",
                "the parent execution request already has a reissue child "
                "(one parent -> at most one child)",
                "reissued_from_execution_request_id = ?",
                (record["reissued_from_execution_request_id"],),
            )
        )
    for column, reason, predicate, params in checks:
        row = connection.execute(
            f"SELECT execution_request_id, signal_id, submission_phase, order_state "
            f"FROM {ORDERS_TABLE} WHERE {predicate}",
            params,
        ).fetchone()
        if row is not None:
            conflicts.append((column, reason, dict(row)))
    return conflicts


def create_execution_order(
    request: ExecutionRequest,
    *,
    client_order_id: str,
    created_at_utc: datetime,
    db_path: Path | None = None,
) -> dict:
    """Persist a new intent in phase ``created`` (no broker state yet).

    ``client_order_id`` is bound here, once, 1:1 (UNIQUE) -- see the ID design
    in ``execution_models``. A duplicate ``execution_request_id``,
    ``signal_id`` (Issue #76: one source signal -> at most one execution
    request) or ``client_order_id`` raises
    :class:`ExecutionOrderConflictError` (an intent is inserted exactly once;
    there is no idempotent re-insert semantics at this layer). The UNIQUE
    constraints inside the ``BEGIN IMMEDIATE`` transaction -- not any
    pre-SELECT -- are what make this safe under concurrent creators: the
    loser's INSERT fails and is rolled back, never committed as a second
    row. Every suppressed duplicate is WARN-logged with the ids, the
    existing row's phase/state and the suppression reason. No history row is
    written: history records STATE changes and a created intent has no order
    state.
    """
    if not isinstance(request, ExecutionRequest):
        raise ValueError(f"request must be an ExecutionRequest, got {request!r}")
    _require_non_empty_str(client_order_id, "client_order_id")
    _require_utc(created_at_utc, "created_at_utc")
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    init_db(db_path)
    record = _intent_record(
        request, client_order_id=client_order_id, created_at_utc=created_at_utc
    )
    with _verified_txn(db_path) as connection:
        _insert_order_record(connection, record)
        created = _validate_writeback(
            connection,
            request.execution_request_id,
            prior_history=[],
            expected_row={**record, "revision": 0},
            expected_entry=None,
        )
    return created


def _intent_record(
    request: ExecutionRequest, *, client_order_id: str, created_at_utc: datetime
) -> dict:
    """The full attempt-1 orders row for a new intent (reissue columns at
    their defaults: attempt 1, no provenance)."""
    return {
        "execution_request_id": request.execution_request_id,
        "signal_id": request.signal_id,
        "strategy_id": request.strategy_id,
        "symbol": request.symbol,
        "side": request.side.value,
        "order_type": request.order_type.value,
        "quantity": str(request.quantity),
        "requested_price": None if request.requested_price is None else str(request.requested_price),
        "stop_loss_price": None if request.stop_loss_price is None else str(request.stop_loss_price),
        "take_profit_price": None
        if request.take_profit_price is None
        else str(request.take_profit_price),
        "requested_at_utc": _iso(request.requested_at_utc),
        "client_order_id": client_order_id,
        "submission_phase": PHASE_CREATED,
        "order_state": None,
        "broker_order_id": None,
        "last_state_reason": None,
        "last_order_result_json": None,
        "created_at_utc": _iso(created_at_utc),
        "updated_at_utc": _iso(created_at_utc),
        "reissued_from_execution_request_id": None,
        "signal_attempt": 1,
        "reissue_kind": None,
        "reissue_initiated_by": None,
        "reissue_reason": None,
        "gate_correction_ref_json": None,
        "reissue_clearance_json": None,
    }


def _insert_order_record(connection: sqlite3.Connection, record: dict) -> None:
    """INSERT one orders row; a UNIQUE collision becomes the classified,
    WARN-logged :class:`ExecutionOrderConflictError` (Issue #76 contract,
    shared by the initial-intent and the reissue registration paths)."""
    insert_sql = (
        f"INSERT INTO {ORDERS_TABLE} ({', '.join(_ORDER_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _ORDER_COLUMNS)})"
    )
    try:
        connection.execute(insert_sql, tuple(record[col] for col in _ORDER_COLUMNS))
    except sqlite3.IntegrityError as exc:
        conflicts = _classify_create_conflicts(connection, record)
        if not conflicts:
            # CHECK / NOT NULL / FK or an unknown future constraint is a
            # persistence failure, not successful duplicate suppression.
            raise
        reason = "; ".join(item[1] for item in conflicts)
        existing = conflicts[0][2]
        _LOGGER.warning(
            "duplicate execution order suppressed: execution_request_id=%s "
            "signal_id=%s signal_attempt=%s existing_execution_request_id=%s "
            "existing_submission_phase=%s existing_order_state=%s "
            "conflicting_keys=%s reason=%s",
            record["execution_request_id"],
            record["signal_id"],
            record["signal_attempt"],
            existing["execution_request_id"],
            existing["submission_phase"],
            existing["order_state"],
            ",".join(item[0] for item in conflicts),
            reason,
        )
        raise ExecutionOrderConflictError(
            f"execution order {record['execution_request_id']!r} (signal_id "
            f"{record['signal_id']!r} / signal_attempt {record['signal_attempt']} / "
            f"client_order_id {record['client_order_id']!r}) collides with an "
            f"existing row ({reason}): {exc}"
        ) from exc


def create_reissue_execution_order(
    request: ExecutionRequest,
    *,
    client_order_id: str,
    created_at_utc: datetime,
    parent_execution_request_id: str,
    reissue_kind: str,
    reissue_initiated_by: str,
    reissue_reason: str,
    clearance_generation: int,
    clearance_evidence_id: str,
    gate_correction_reference: GateDecisionReference | None = None,
    db_path: Path | None = None,
) -> dict:
    """Register an EXPLICIT reissue of ``parent_execution_request_id`` as a
    new intent row -- the Issue #95 / #115 registration API.

    Everything happens in ONE ``BEGIN IMMEDIATE`` transaction:

    - the parent bundle is loaded and fully validated, and the parent-state
      requirement for ``reissue_kind`` is verified against it
      (``broker_rejected`` -> parent ``order_state = rejected``;
      ``gate_correction`` -> parent phase ``created`` with no order state;
      EVERY other parent state -- pending / accepted / partially_filled /
      unknown / filled / cancelled -- refuses the registration),
    - the registration clearance reference is resolved against the same-DB
      evidence table and content-bound (generation + evidence id + payload
      sha256); an unresolvable reference is
      :class:`ExecutionClearanceError` (fail-closed -- the store does NOT
      judge the clearance requirements themselves, Issue #116 does, but it
      refuses to record a reference it cannot verify),
    - the child field invariants are verified against the parent row via
      THE SAME shared rules restore-time validation runs
      (:func:`_reissue_link_problems`: identity immutability, attempt
      arithmetic, time monotonicity, the quantity rule with the
      ``broker_rejected`` x ``parameter`` increase exception, auto =
      identical parameters), so a registered chain always restores,
    - the child row is INSERTed; racing registrations for the same parent /
      attempt collide on ``UNIQUE(reissued_from_execution_request_id)`` /
      ``UNIQUE(signal_id, signal_attempt)`` and the loser gets the
      classified, WARN-logged :class:`ExecutionOrderConflictError` -- at
      most one child row ever exists, process restarts and concurrent
      runners included.

    The child starts in phase ``created`` like any other intent and gains
    submission authority only through :func:`mark_submitting`, which
    re-verifies the signal's CURRENT clearance at that moment. Attempt caps
    and eligibility (Gate decision recency, Journal preconditions, broker
    non-existence evidence) are the service layer's preconditions
    (Issue #116); violations of the persistable contracts here raise
    ``ValueError`` with the DB untouched.
    """
    if not isinstance(request, ExecutionRequest):
        raise ValueError(f"request must be an ExecutionRequest, got {request!r}")
    _require_non_empty_str(client_order_id, "client_order_id")
    _require_utc(created_at_utc, "created_at_utc")
    _require_non_empty_str(parent_execution_request_id, "parent_execution_request_id")
    if reissue_kind not in KNOWN_REISSUE_KINDS:
        raise ValueError(
            f"reissue_kind must be one of {sorted(KNOWN_REISSUE_KINDS)}, got {reissue_kind!r}"
        )
    if reissue_initiated_by not in KNOWN_REISSUE_INITIATORS:
        raise ValueError(
            f"reissue_initiated_by must be one of {sorted(KNOWN_REISSUE_INITIATORS)}, "
            f"got {reissue_initiated_by!r}"
        )
    _require_non_empty_str(reissue_reason, "reissue_reason")
    if (
        isinstance(clearance_generation, bool)
        or not isinstance(clearance_generation, int)
        or clearance_generation < 1
    ):
        raise ValueError(
            f"clearance_generation must be an int >= 1, got {clearance_generation!r}"
        )
    _require_non_empty_str(clearance_evidence_id, "clearance_evidence_id")
    if gate_correction_reference is not None and not isinstance(
        gate_correction_reference, GateDecisionReference
    ):
        raise ValueError(
            f"gate_correction_reference must be a GateDecisionReference or None, got "
            f"{gate_correction_reference!r}"
        )
    db_path = _existing_db_path(db_path, parent_execution_request_id)
    with _verified_txn(db_path) as connection:
        parent, _parent_history = _require_bundle_for_update(
            connection, parent_execution_request_id
        )

        # Resolve + content-bind the registration clearance evidence (same
        # DB, same transaction). The reference the child row will carry is
        # built from the COMMITTED evidence row, never from caller input.
        evidence = connection.execute(
            f"SELECT evidence_id, evidence_json FROM {CLEARANCES_TABLE} "
            "WHERE signal_id = ? AND clearance_generation = ?",
            (request.signal_id, clearance_generation),
        ).fetchone()
        if evidence is None or evidence["evidence_id"] != clearance_evidence_id:
            raise ExecutionClearanceError(
                f"clearance evidence (signal {request.signal_id!r}, generation "
                f"{clearance_generation}, evidence_id {clearance_evidence_id!r}) does "
                "not resolve to a recorded evidence row; an unverifiable clearance "
                "reference never justifies a reissue (nothing was changed)"
            )
        clearance_ref_json = _clearance_ref_to_json(
            clearance_generation=clearance_generation,
            evidence_id=evidence["evidence_id"],
            evidence_sha256=_sha256_text(evidence["evidence_json"]),
        )

        record = _intent_record(
            request, client_order_id=client_order_id, created_at_utc=created_at_utc
        )
        record.update(
            reissued_from_execution_request_id=parent_execution_request_id,
            signal_attempt=parent["signal_attempt"] + 1,
            reissue_kind=reissue_kind,
            reissue_initiated_by=reissue_initiated_by,
            reissue_reason=reissue_reason,
            gate_correction_ref_json=(
                None
                if gate_correction_reference is None
                else gate_correction_reference.to_json()
            ),
            reissue_clearance_json=clearance_ref_json,
        )

        problems = _reissue_row_problems(record) + _reissue_link_problems(
            parent, record, _parent_rejection_category(parent)
        )
        if problems:
            raise ValueError(
                f"reissue registration for parent {parent_execution_request_id!r} "
                f"violates the chain contract (nothing was changed): "
                + "; ".join(problems)
            )

        _insert_order_record(connection, record)
        created = _validate_writeback(
            connection,
            request.execution_request_id,
            prior_history=[],
            expected_row={**record, "revision": 0},
            expected_entry=None,
        )
    return created


def _existing_db_path(db_path: Path | None, execution_request_id: str) -> Path:
    """Resolve the DB path for an UPDATE and refuse to create a missing file.

    Update APIs address an already-persisted order; connecting to a missing
    path would create an empty DB file as a side effect, so a missing file is
    reported as the order not existing instead.
    """
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    if not db_path.exists():
        raise ExecutionOrderNotFoundError(
            f"no execution order with id {execution_request_id!r} (no DB at {db_path})"
        )
    return db_path


def _require_bundle_for_update(
    connection: sqlite3.Connection, execution_request_id: str
) -> tuple[dict, list[dict]]:
    bundle = _fetch_and_validate(connection, execution_request_id)
    if bundle is None:
        raise ExecutionOrderNotFoundError(f"no execution order with id {execution_request_id!r}")
    return bundle


def _append_history(
    connection: sqlite3.Connection,
    *,
    execution_request_id: str,
    previous_state: OrderState | None,
    new_state: OrderState,
    reason: str,
    broker_order_id: str | None,
    order_result_json: str | None,
    changed_at_utc: datetime,
    repair_attempt_id: str | None = None,
    repair_generation: int | None = None,
    clearance_json: str | None = None,
) -> None:
    connection.execute(
        f"INSERT INTO {HISTORY_TABLE} "
        "(execution_request_id, previous_state, new_state, reason, broker_order_id, "
        "order_result_json, changed_at_utc, repair_attempt_id, repair_generation, "
        "clearance_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            execution_request_id,
            None if previous_state is None else previous_state.value,
            new_state.value,
            reason,
            broker_order_id,
            order_result_json,
            _iso(changed_at_utc),
            repair_attempt_id,
            repair_generation,
            clearance_json,
        ),
    )


def mark_submitting(
    execution_request_id: str,
    *,
    reason: str,
    changed_at_utc: datetime,
    clearance_scope: str | None = None,
    clearance_observed_after_utc: datetime | None = None,
    db_path: Path | None = None,
) -> dict:
    """Move ``created -> submitting`` and start broker-state tracking at PENDING.

    Entered exactly once: any phase other than ``created`` is refused with
    ``ValueError`` (a lost submit outcome is UNKNOWN territory, resolved by
    query -- never by silently re-entering the submit phase). The phase
    change, the PENDING state, and the first history row (previous_state
    NULL) commit in one transaction. Under two racing connections the
    ``BEGIN IMMEDIATE`` transaction serializes them and exactly one wins.

    Reissue guards (Issue #95 / #115), all INSIDE this same transaction:

    - an order that HAS a reissue child never gains submission authority
      (:class:`ExecutionOrderSupersededError`) -- the supersession commit
      and this transition serialize on the store's write lock, so either
      the child registration sees a non-``created`` parent and fails, or
      this call sees the child and fails: a submitted parent and its child
      can never coexist;
    - a reissue CHILD (``signal_attempt >= 2``) must present the submission
      target scope and the freshness cutoff, and the signal's CURRENT
      clearance (highest generation) must be ``valid``, scoped to exactly
      that target, and observed at/after the cutoff -- anything else
      (missing inputs, no evidence, ``observing`` / ``revoked``, scope
      mismatch, stale) is :class:`ExecutionClearanceError` and nothing
      changes (fail-closed);
    - the ACCEPTED evidence is persisted as a content-bound reference on
      the very history row that grants the authority (``clearance_json``),
      so the evidence that allowed each submission stays independently
      traceable from the evidence that justified the registration.

    An attempt-1 order takes no clearance inputs (passing them is a
    caller error).
    """
    _require_non_empty_str(reason, "reason")
    _require_utc(changed_at_utc, "changed_at_utc")
    if clearance_scope is not None:
        _require_non_empty_str(clearance_scope, "clearance_scope")
    if clearance_observed_after_utc is not None:
        _require_utc(clearance_observed_after_utc, "clearance_observed_after_utc")
    db_path = _existing_db_path(db_path, execution_request_id)
    with _verified_txn(db_path) as connection:
        row, history = _require_bundle_for_update(connection, execution_request_id)
        child = connection.execute(
            f"SELECT execution_request_id FROM {ORDERS_TABLE} "
            "WHERE reissued_from_execution_request_id = ?",
            (execution_request_id,),
        ).fetchone()
        if child is not None:
            raise ExecutionOrderSupersededError(
                f"execution order {execution_request_id!r} was superseded by its "
                f"reissue child {child['execution_request_id']!r} and can never gain "
                "submission authority (nothing was changed)"
            )
        if row["submission_phase"] != PHASE_CREATED:
            raise ValueError(
                f"execution order {execution_request_id!r} is in phase "
                f"{row['submission_phase']!r}, not {PHASE_CREATED!r}; submitting starts exactly once"
            )
        if _parse_utc(row["updated_at_utc"]) > changed_at_utc:
            raise ValueError(
                f"updated_at_utc must not go backwards: {row['updated_at_utc']} -> "
                f"{_iso(changed_at_utc)}"
            )
        clearance_json: str | None = None
        if row["signal_attempt"] >= 2:
            clearance_json = _verify_submission_clearance(
                connection,
                row,
                clearance_scope=clearance_scope,
                clearance_observed_after_utc=clearance_observed_after_utc,
            )
        elif clearance_scope is not None or clearance_observed_after_utc is not None:
            raise ValueError(
                f"execution order {execution_request_id!r} is attempt 1; clearance "
                "verification inputs apply only to reissue children"
            )
        connection.execute(
            f"UPDATE {ORDERS_TABLE} SET submission_phase = ?, order_state = ?, "
            "last_state_reason = ?, updated_at_utc = ? WHERE execution_request_id = ?",
            (
                PHASE_SUBMITTING,
                OrderState.PENDING.value,
                reason,
                _iso(changed_at_utc),
                execution_request_id,
            ),
        )
        _append_history(
            connection,
            execution_request_id=execution_request_id,
            previous_state=None,
            new_state=OrderState.PENDING,
            reason=reason,
            broker_order_id=None,
            order_result_json=None,
            changed_at_utc=changed_at_utc,
            clearance_json=clearance_json,
        )
        expected_row = dict(row)
        expected_row.update(
            submission_phase=PHASE_SUBMITTING,
            order_state=OrderState.PENDING.value,
            last_state_reason=reason,
            updated_at_utc=_iso(changed_at_utc),
            revision=row["revision"] + 1,
        )
        updated = _validate_writeback(
            connection,
            execution_request_id,
            prior_history=history,
            expected_row=expected_row,
            expected_entry={
                "execution_request_id": execution_request_id,
                "previous_state": None,
                "new_state": OrderState.PENDING.value,
                "reason": reason,
                "broker_order_id": None,
                "order_result_json": None,
                "changed_at_utc": _iso(changed_at_utc),
                "repair_attempt_id": None,
                "repair_generation": None,
                "clearance_json": clearance_json,
            },
        )
    return updated


def _verify_submission_clearance(
    connection: sqlite3.Connection,
    row: dict,
    *,
    clearance_scope: str | None,
    clearance_observed_after_utc: datetime | None,
) -> str:
    """Verify the signal's CURRENT clearance for a reissue child inside the
    ``mark_submitting`` transaction; returns the content-bound reference to
    persist on the granting history row. EVERY failure is
    :class:`ExecutionClearanceError` (fail-closed): downgrading information
    always wins, and only a fully verified grant authorizes."""
    request_id = row["execution_request_id"]
    signal_id = row["signal_id"]
    if clearance_scope is None or clearance_observed_after_utc is None:
        raise ExecutionClearanceError(
            f"reissue child {request_id!r} requires clearance verification inputs "
            "(clearance_scope + clearance_observed_after_utc); without them the "
            "clearance cannot be verified and submission authority is refused"
        )
    current = connection.execute(
        f"SELECT * FROM {CLEARANCES_TABLE} WHERE signal_id = ? "
        "ORDER BY clearance_generation DESC LIMIT 1",
        (signal_id,),
    ).fetchone()
    if current is None:
        raise ExecutionClearanceError(
            f"reissue child {request_id!r} has no clearance evidence recorded for "
            f"signal {signal_id!r}; absence of evidence is not absence of divergence"
        )
    current = dict(current)
    if current["status"] != CLEARANCE_STATUS_VALID:
        raise ExecutionClearanceError(
            f"reissue child {request_id!r} cannot submit: the current clearance "
            f"(generation {current['clearance_generation']}) is "
            f"{current['status']!r}, not {CLEARANCE_STATUS_VALID!r}"
        )
    if current["account_scope"] != clearance_scope:
        raise ExecutionClearanceError(
            f"reissue child {request_id!r} cannot submit: the clearance was observed "
            f"for scope {current['account_scope']!r}, but the submission target is "
            f"{clearance_scope!r} (evidence must cover the account it authorizes)"
        )
    try:
        observed_at = _parse_utc(current["observed_at_utc"])
    except ValueError as exc:
        raise ExecutionClearanceError(
            f"reissue child {request_id!r} cannot submit: the clearance carries an "
            f"unreadable observation time ({exc})"
        ) from exc
    if observed_at < clearance_observed_after_utc:
        raise ExecutionClearanceError(
            f"reissue child {request_id!r} cannot submit: the clearance was observed "
            f"at {current['observed_at_utc']}, before the freshness cutoff "
            f"{_iso(clearance_observed_after_utc)}"
        )
    return _clearance_ref_to_json(
        clearance_generation=current["clearance_generation"],
        evidence_id=current["evidence_id"],
        evidence_sha256=_sha256_text(current["evidence_json"]),
    )


def _apply_state_update(
    connection: sqlite3.Connection,
    row: dict,
    history: list[dict],
    *,
    new_state: OrderState,
    reason: str,
    changed_at_utc: datetime,
    effective_broker_id: str | None,
    result_json: str | None,
    repair_attempt_id: str | None = None,
    repair_generation: int | None = None,
) -> dict:
    """Shared tail of both update paths: phase derivation + current-row UPDATE
    + history INSERT (snapshot anchored on the history row too) + pre-commit
    write-back validation (:func:`_validate_writeback`), inside the caller's
    transaction. A UNIQUE violation (DB-wide ``broker_order_id``, PR #91
    Major 3, or a reused ``repair_attempt_id``, Issue #104) surfaces as
    :class:`ExecutionOrderConflictError`; the caller's transaction rolls
    back the whole update, history row included. Returns the validated
    post-update row."""
    execution_request_id = row["execution_request_id"]
    current_state = OrderState(row["order_state"])
    phase = row["submission_phase"]
    if new_state in _CONFIRMED_STATES:
        phase = PHASE_SUBMITTED
    keep_json = row["last_order_result_json"] if result_json is None else result_json
    try:
        connection.execute(
            f"UPDATE {ORDERS_TABLE} SET submission_phase = ?, order_state = ?, "
            "broker_order_id = ?, last_state_reason = ?, last_order_result_json = ?, "
            "updated_at_utc = ? WHERE execution_request_id = ?",
            (
                phase,
                new_state.value,
                effective_broker_id,
                reason,
                keep_json,
                _iso(changed_at_utc),
                execution_request_id,
            ),
        )
        _append_history(
            connection,
            execution_request_id=execution_request_id,
            previous_state=current_state,
            new_state=new_state,
            reason=reason,
            broker_order_id=effective_broker_id,
            order_result_json=result_json,
            changed_at_utc=changed_at_utc,
            repair_attempt_id=repair_attempt_id,
            repair_generation=repair_generation,
        )
    except sqlite3.IntegrityError as exc:
        raise ExecutionOrderConflictError(
            f"state update for execution order {execution_request_id!r} violates a "
            f"uniqueness constraint (broker_order_id {effective_broker_id!r} already "
            f"assigned to another order, or repair_attempt_id {repair_attempt_id!r} "
            f"already applied?): {exc}"
        ) from exc
    expected_row = dict(row)
    expected_row.update(
        submission_phase=phase,
        order_state=new_state.value,
        broker_order_id=effective_broker_id,
        last_state_reason=reason,
        last_order_result_json=keep_json,
        updated_at_utc=_iso(changed_at_utc),
        revision=row["revision"] + 1,
    )
    return _validate_writeback(
        connection,
        execution_request_id,
        prior_history=history,
        expected_row=expected_row,
        expected_entry={
            "execution_request_id": execution_request_id,
            "previous_state": current_state.value,
            "new_state": new_state.value,
            "reason": reason,
            "broker_order_id": effective_broker_id,
            "order_result_json": result_json,
            "changed_at_utc": _iso(changed_at_utc),
            "repair_attempt_id": repair_attempt_id,
            "repair_generation": repair_generation,
            "clearance_json": None,
        },
    )


def _check_transition_and_time(
    row: dict, *, new_state: OrderState, changed_at_utc: datetime
) -> None:
    execution_request_id = row["execution_request_id"]
    if row["submission_phase"] == PHASE_CREATED:
        raise ValueError(
            f"execution order {execution_request_id!r} has no submit attempt yet "
            f"(phase {PHASE_CREATED!r}); call mark_submitting first"
        )
    validate_order_state_transition(OrderState(row["order_state"]), new_state)
    if _parse_utc(row["updated_at_utc"]) > changed_at_utc:
        raise ValueError(
            f"updated_at_utc must not go backwards: {row['updated_at_utc']} -> "
            f"{_iso(changed_at_utc)}"
        )


def record_order_state(
    execution_request_id: str,
    new_state: OrderState,
    *,
    reason: str,
    changed_at_utc: datetime,
    broker_order_id: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """Record a state-only observation -- restricted to ``UNKNOWN``.

    ``UNKNOWN`` is the one state that exists precisely because NO full
    ``OrderResult`` could be obtained (a submit / query whose outcome was
    lost, e.g. ``BrokerTimeoutError``), so it is the only state this
    snapshot-less API accepts (PR #91 review, Major 1). Every other
    observation carries a broker snapshot and must go through
    :func:`record_order_result`, where the full PR #87 content contract is
    enforced against the anchored DB snapshot. A state-only UNKNOWN never
    touches the stored snapshot, so the fills known before the outcome was
    lost keep guarding any later resolution.

    ``broker_order_id`` may only CONFIRM the stored value (or be ``None`` =
    "not part of this observation"): broker IDs are only ever learned from
    full ``OrderResult`` snapshots, so a state-only update can neither
    introduce nor change one. ``changed_at_utc`` must not precede the stored
    ``updated_at_utc``. Same-state re-observations (UNKNOWN -> UNKNOWN) are
    allowed and recorded (see the module docstring).
    """
    if not isinstance(new_state, OrderState):
        raise ValueError(f"new_state must be an OrderState, got {new_state!r}")
    if new_state is not OrderState.UNKNOWN:
        raise ValueError(
            f"record_order_state only records UNKNOWN (no-result outcomes); a "
            f"{new_state.value!r} observation carries a broker snapshot and must go "
            "through record_order_result"
        )
    _require_non_empty_str(reason, "reason")
    _require_utc(changed_at_utc, "changed_at_utc")
    if broker_order_id is not None:
        _require_non_empty_str(broker_order_id, "broker_order_id")
    db_path = _existing_db_path(db_path, execution_request_id)
    with _verified_txn(db_path) as connection:
        row, history = _require_bundle_for_update(connection, execution_request_id)
        if broker_order_id is not None and broker_order_id != row["broker_order_id"]:
            raise ValueError(
                f"a state-only update may only confirm the stored broker_order_id "
                f"{row['broker_order_id']!r}, got {broker_order_id!r}; broker IDs are "
                "only learned from full OrderResult snapshots"
            )
        _check_transition_and_time(row, new_state=new_state, changed_at_utc=changed_at_utc)
        updated = _apply_state_update(
            connection,
            row,
            history,
            new_state=new_state,
            reason=reason,
            changed_at_utc=changed_at_utc,
            effective_broker_id=row["broker_order_id"],
            result_json=None,
        )
    return updated


def record_order_result(
    execution_request_id: str,
    result: OrderResult,
    *,
    reason: str,
    previous_result: OrderResult | None = None,
    expected_current_state: OrderState | None = None,
    expected_revision: int | None = None,
    repair_fence: RepairFenceToken | None = None,
    db_path: Path | None = None,
) -> dict:
    """Record a full broker ``OrderResult`` snapshot for this order.

    The authoritative update path (PR #91 review, Major 1):

    - the whole stored bundle (row + history + anchor) is validated FIRST, so
      a tampered DB is refused before it can influence the update,
    - the snapshot must belong to this order (``client_order_id`` and every
      identity/request field must match the stored intent),
    - the HISTORY-ANCHORED previous snapshot -- never an in-memory copy -- is
      ALWAYS the ``old`` side of ``validate_order_result_update``, so known
      fills / quantities / broker IDs / timestamps can never be rolled back
      by a stale caller, even right after a process restart,
    - a caller-supplied ``previous_result`` is accepted only when it equals
      the anchored snapshot (guards the caller's own staleness assumptions),
    - the transition is additionally validated against the CURRENT tracked
      state (which may be a state-only UNKNOWN recorded after the snapshot),
    - ``broker_order_id`` follows NULL -> value once; afterwards it may only
      change as an AUDITED Order Reissue (Issue #102): the new snapshot must
      record the stored id in its ``superseded_broker_order_ids`` chain and
      pass the chain-append rule against the anchored snapshot. Dropping the
      id to NULL is refused,
    - the snapshot (current row AND history row), the state and the history
      entry commit in ONE transaction; a DB-wide ``broker_order_id``
      uniqueness conflict rolls all of it back
      (:class:`ExecutionOrderConflictError`),
    - ``expected_current_state`` / ``expected_revision`` (Issue #104,
      optional) make the update a COMPARE-AND-SET: the row's current
      ``order_state`` and/or its REVISION (= history row count; increments
      on EVERY write, same-state re-observations, snapshot / broker-id /
      timestamp refreshes included) -- both read inside this same ``BEGIN
      IMMEDIATE`` transaction -- must match exactly, or the update fails
      with :class:`ExecutionOrderStateConflictError` and the DB stays
      untouched. The revision guard closes the read -> decide -> write
      TOCTOU window COMPLETELY: a state-only comparison cannot see a
      concurrent same-state change (an UNKNOWN -> UNKNOWN record, a further
      partial fill, a snapshot refresh), the revision can.
    - ``repair_fence`` (Issue #104, a :class:`RepairFenceToken`) records
      STRUCTURED repair provenance on the appended history row AND makes the
      update FENCED: inside this same transaction the order's
      ``execution_repair_fences`` row must match the token in FULL --
      attempt id, generation, TAKEOVER ID, owner and claim execution id --
      otherwise :class:`ExecutionRepairFenceConflictError` rolls everything
      back. Attempt id and generation ALONE are deliberately not enough: two
      executions of the same generation (a worker that died mid-takeover and
      the worker that resumed it) are distinguished only by the takeover /
      claim execution ids. A worker whose claim was taken over therefore
      cannot commit, even while the order's state and revision still match.
      The partial UNIQUE index additionally lets one attempt commit at most
      one history row DB-wide, and recovery attributes an applied repair by
      the history's provenance columns -- never by parsing the reason string.

      The token is an ASSERTION the caller must satisfy, never evidence it
      supplies: it can only ever narrow what this call may write, since the
      value it is checked against is the committed fence row read here (r6
      review). No caller-supplied object anywhere in the repair protocol is
      accepted as proof that a durable write happened.
    """
    if not isinstance(result, OrderResult):
        raise ValueError(f"result must be an OrderResult, got {result!r}")
    if previous_result is not None and not isinstance(previous_result, OrderResult):
        raise ValueError(f"previous_result must be an OrderResult, got {previous_result!r}")
    if expected_current_state is not None and not isinstance(expected_current_state, OrderState):
        raise ValueError(
            f"expected_current_state must be an OrderState or None, got "
            f"{expected_current_state!r}"
        )
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError(
            f"expected_revision must be a non-negative int or None, got {expected_revision!r}"
        )
    if repair_fence is not None and not isinstance(repair_fence, RepairFenceToken):
        raise ValueError(f"repair_fence must be a RepairFenceToken or None, got {repair_fence!r}")
    repair_attempt_id = None if repair_fence is None else repair_fence.repair_attempt_id
    repair_generation = None if repair_fence is None else repair_fence.generation
    _require_non_empty_str(reason, "reason")
    db_path = _existing_db_path(db_path, execution_request_id)
    with _verified_txn(db_path) as connection:
        row, history = _require_bundle_for_update(connection, execution_request_id)

        if repair_fence is not None:
            # FENCE check (Issue #104 re-review): the currently authorized
            # repair execution for this order -- installed via
            # register_repair_fence from a durable takeover intent -- must
            # match this token in FULL (attempt, generation, takeover id,
            # owner, claim execution id). Verified INSIDE the update
            # transaction, so a takeover between the caller's read and this
            # commit is always detected, state/revision equality
            # notwithstanding.
            fence = connection.execute(
                f"SELECT {', '.join(_FENCE_IDENTITY_COLUMNS)} FROM {FENCES_TABLE} "
                "WHERE execution_request_id = ?",
                (execution_request_id,),
            ).fetchone()
            if fence is None or tuple(fence) != repair_fence.identity():
                raise ExecutionRepairFenceConflictError(
                    f"repair update for execution order {execution_request_id!r} carries "
                    f"the fencing token {repair_fence.describe()} but the order's "
                    "authorized fence is "
                    + (
                        "absent"
                        if fence is None
                        else (
                            f"(attempt {fence['repair_attempt_id']!r}, generation "
                            f"{fence['generation']}, takeover {fence['takeover_id']!r}, "
                            f"claim {fence['claim_execution_id']!r}, owner "
                            f"{fence['owner_id']!r})"
                        )
                    )
                    + "; the claim was lost and nothing was changed"
                )

        if expected_revision is not None and row["revision"] != expected_revision:
            raise ExecutionOrderStateConflictError(
                f"execution order {execution_request_id!r} is at revision "
                f"{row['revision']}, not the expected {expected_revision}; the order "
                "changed since it was observed, so the conditional update is refused "
                "and nothing was changed"
            )
        if expected_current_state is not None:
            current = None if row["order_state"] is None else OrderState(row["order_state"])
            if current is not expected_current_state:
                raise ExecutionOrderStateConflictError(
                    f"execution order {execution_request_id!r} is in state "
                    f"{None if current is None else current.value!r}, not the expected "
                    f"{expected_current_state.value!r}; the conditional update is refused "
                    "and nothing was changed"
                )

        if result.client_order_id != row["client_order_id"]:
            raise ValueError(
                f"result.client_order_id {result.client_order_id!r} does not belong to execution "
                f"order {execution_request_id!r} (client_order_id {row['client_order_id']!r})"
            )
        stored_requested_price = (
            None if row["requested_price"] is None else Decimal(row["requested_price"])
        )
        if (
            result.symbol != row["symbol"]
            or result.side.value != row["side"]
            or result.order_type.value != row["order_type"]
            or result.requested_quantity != Decimal(row["quantity"])
            or result.requested_price != stored_requested_price
        ):
            raise ValueError(
                f"result identity/request fields do not match the stored execution "
                f"request {execution_request_id!r}"
            )

        db_snapshot = _anchored_snapshot(history)
        if previous_result is not None and previous_result != db_snapshot:
            raise ValueError(
                "previous_result does not match the snapshot anchored in the DB; refusing "
                "to validate an update against a stale in-memory snapshot"
            )
        if db_snapshot is not None:
            validate_order_result_update(db_snapshot, result)

        # broker id: NULL -> value once; afterwards it may only move as an
        # AUDITED Order Reissue (Issue #102) -- the new snapshot must record
        # the stored id in its superseded chain, and the exact chain-append
        # legality was already enforced against the ANCHORED snapshot by
        # validate_order_result_update above (so a stored id can never be
        # silently replaced or its history rewritten). A change without an
        # anchored snapshot to validate against is refused outright.
        if row["broker_order_id"] is not None and result.broker_order_id != row["broker_order_id"]:
            if result.broker_order_id is None:
                raise ValueError(
                    f"broker_order_id may never be dropped: "
                    f"{row['broker_order_id']!r} -> None refused"
                )
            if db_snapshot is None or row["broker_order_id"] not in result.superseded_broker_order_ids:
                raise ValueError(
                    f"broker_order_id may only change as an audited reissue: "
                    f"{row['broker_order_id']!r} -> {result.broker_order_id!r} refused "
                    "(the stored id is not recorded as superseded by the new snapshot)"
                )
        effective_broker_id = (
            row["broker_order_id"] if result.broker_order_id is None else result.broker_order_id
        )
        if result.state in _STATES_REQUIRING_BROKER_ID and effective_broker_id is None:
            raise ValueError(f"state {result.state.value!r} requires a broker_order_id")
        # Ownership pre-check (PR #112 review): EVERY id the snapshot names
        # (current + superseded chain) must be free or already this order's
        # own — the column UNIQUE constraint cannot see chain ids, and a
        # colliding chain id that reached COMMIT would brick all later reads
        # via the DB-wide duplicate scan.
        _assert_result_broker_ids_unowned(connection, execution_request_id, result)
        _check_transition_and_time(
            row, new_state=result.state, changed_at_utc=result.updated_at_utc
        )
        updated = _apply_state_update(
            connection,
            row,
            history,
            new_state=result.state,
            reason=reason,
            changed_at_utc=result.updated_at_utc,
            effective_broker_id=effective_broker_id,
            result_json=_result_to_json(result),
            repair_attempt_id=repair_attempt_id,
            repair_generation=repair_generation,
        )
    return updated


# --- repair fencing (Issue #104 re-review) ------------------------------------


def register_repair_fence(
    execution_request_id: str,
    *,
    token: RepairFenceToken,
    registered_at_utc: datetime,
    db_path: Path | None = None,
) -> InstalledRepairFence:
    """Install the order's repair fencing token -- atomically (r4 review).

    Returns the fence as COMMITTED (read back inside this transaction), so
    the caller can prove the installation to the reconciliation side rather
    than asserting it (r5 review).

    The token comes from the reconciliation DB's durable takeover intent (or,
    at generation 1, from the attempt's original claim), so installing it is
    the second step of a restartable cross-DB protocol. All inside one
    ``BEGIN IMMEDIATE`` transaction:

    IDEMPOTENT SUCCESS -- the stored fence already equals the token in FULL
    (attempt id, generation, takeover id, owner, claim execution id):
    nothing is written and the call returns. A worker that died after
    installing its fence can therefore always retry this step.

    CONFLICT (:class:`ExecutionRepairFenceConflictError`, DB untouched) --
    everything else for the SAME attempt:

    - the stored generation is GREATER (a later takeover already owns the
      order -- generation rollback is impossible),
    - the stored generation EQUALS but any identity component differs (a
      DIFFERENT execution holds this generation: never adopted, because
      "same attempt and generation" says nothing about which execution is
      the legitimate one -- only the durable intent does),
    - the token would SKIP a generation (``stored + 1`` is the only legal
      step forward, so a fence can never run ahead of the intent chain).

    A fence held for a DIFFERENT attempt is REPLACED: attempt ids are unique
    per reservation, so a new attempt legitimately takes the order over; the
    previous attempt's later commit / finalize then fails its own fence and
    claim checks as a typed lost claim -- never a wrong write.

    Fence rows are retained after a repair finishes (see
    :func:`_create_fences_table_sql`).
    """
    _require_non_empty_str(execution_request_id, "execution_request_id")
    if not isinstance(token, RepairFenceToken):
        raise ValueError(f"token must be a RepairFenceToken, got {token!r}")
    _require_utc(registered_at_utc, "registered_at_utc")
    db_path = _existing_db_path(db_path, execution_request_id)
    with _verified_txn(db_path) as connection:
        order = connection.execute(
            f"SELECT execution_request_id FROM {ORDERS_TABLE} WHERE execution_request_id = ?",
            (execution_request_id,),
        ).fetchone()
        if order is None:
            raise ExecutionOrderNotFoundError(
                f"no execution order with id {execution_request_id!r}"
            )
        fence = connection.execute(
            f"SELECT * FROM {FENCES_TABLE} WHERE execution_request_id = ?",
            (execution_request_id,),
        ).fetchone()
        if fence is not None and fence["repair_attempt_id"] == token.repair_attempt_id:
            stored_identity = tuple(fence[column] for column in _FENCE_IDENTITY_COLUMNS)
            if stored_identity == token.identity():
                # idempotent: this exact execution already holds it
                return _installed_fence(fence)
            if fence["generation"] != token.generation - 1:
                raise ExecutionRepairFenceConflictError(
                    f"repair fence for execution order {execution_request_id!r} is held at "
                    f"(attempt {fence['repair_attempt_id']!r}, generation "
                    f"{fence['generation']}, takeover {fence['takeover_id']!r}, claim "
                    f"{fence['claim_execution_id']!r}); installing "
                    f"{token.describe()} would "
                    + (
                        "roll the generation back"
                        if fence["generation"] > token.generation
                        else "skip a generation"
                        if fence["generation"] < token.generation - 1
                        else "replace a different execution of the same generation"
                    )
                    + " and changes nothing"
                )
        record = (
            execution_request_id,
            token.repair_attempt_id,
            token.generation,
            token.takeover_id,
            token.owner_id,
            token.claim_execution_id,
            _iso(registered_at_utc),
        )
        if fence is None:
            connection.execute(
                f"INSERT INTO {FENCES_TABLE} ({', '.join(_FENCE_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _FENCE_COLUMNS)})",
                record,
            )
            return _installed_fence(_fence_row(connection, execution_request_id))
        updated = connection.execute(
            f"UPDATE {FENCES_TABLE} SET repair_attempt_id = ?, generation = ?, "
            "takeover_id = ?, owner_id = ?, claim_execution_id = ?, updated_at_utc = ? "
            "WHERE execution_request_id = ?",
            (*record[1:], execution_request_id),
        )
        if updated.rowcount != 1:  # pragma: no cover - defence in depth
            raise ExecutionRepairFenceConflictError(
                f"repair fence for execution order {execution_request_id!r} could not be "
                "updated; the claim was lost and nothing was changed"
            )
        return _installed_fence(_fence_row(connection, execution_request_id))


def _fence_row(connection: sqlite3.Connection, execution_request_id: str):
    return connection.execute(
        f"SELECT * FROM {FENCES_TABLE} WHERE execution_request_id = ?",
        (execution_request_id,),
    ).fetchone()


def _installed_fence(row) -> InstalledRepairFence:
    """The COMMITTED fence row as a typed proof (never the caller's input)."""
    return InstalledRepairFence(
        execution_request_id=row["execution_request_id"],
        token=RepairFenceToken(
            repair_attempt_id=row["repair_attempt_id"],
            generation=row["generation"],
            owner_id=row["owner_id"],
            claim_execution_id=row["claim_execution_id"],
            takeover_id=row["takeover_id"],
        ),
        updated_at_utc=_parse_utc(row["updated_at_utc"]),
    )


@contextmanager
def repair_fence_promotion_guard(
    execution_request_id: str,
    *,
    expected: RepairFenceToken,
    db_path: Path | None = None,
):
    """Hold this order's fence STILL while a repair claim is promoted.

    THE WINDOW THIS CLOSES (Issue #104 re-review r7 Blocker). Verifying the
    committed fence and then promoting the reconciliation claim are writes
    to two different SQLite files, so between them another attempt could
    replace the fence. The claim then reached generation 2 under identity X
    while the order's fence already belonged to Y -- the reconciliation
    store's own API producing a split identity. Re-reading the fence just
    before the promotion does NOT fix that: it only makes the window
    smaller.

    THE GUARD. This context manager opens a WRITE transaction on the order
    store (``BEGIN IMMEDIATE`` -> RESERVED lock) and reads the fence row
    INSIDE it. While the guard is held:

    - no other connection can take the order store's write lock, so
      :func:`register_repair_fence` and :func:`record_order_result` cannot
      replace or move the fence -- they wait for the busy timeout and then
      fail typed;
    - other connections can still READ the order store. The guard sets
      ``PRAGMA query_only`` so it can never write, which means it never
      escalates RESERVED to EXCLUSIVE. That is what keeps the reverse-order
      path (a reconciliation transaction reading this store, e.g.
      :func:`get_repair_fence`) deadlock-free.

    LOCK ORDER, repository-wide: **order-store guard first, reconciliation
    transaction second.** No path takes them the other way round while
    holding a write lock.

    CRASH / TIMEOUT. The lock is an OS-level SQLite file lock held by the
    process; process death releases it -- there is no durable lock row to
    leak and nothing to clean up. A guard that cannot be acquired within
    ``BUSY_TIMEOUT_SECONDS`` raises
    :class:`ExecutionRepairFenceBusyError` instead of waiting forever.

    IDENTITY. Before yielding, the fence row must match ``expected`` in
    FULL -- execution request, attempt, generation, takeover, owner and
    claim execution id. A missing row or any difference raises
    :class:`ExecutionRepairFenceConflictError` and the guard is released
    without the caller ever entering its critical section. The row is read
    from the DB under the lock; no caller-supplied value is accepted as
    evidence that it exists (r6 review).
    """
    _require_non_empty_str(execution_request_id, "execution_request_id")
    if not isinstance(expected, RepairFenceToken):
        raise ValueError(f"expected must be a RepairFenceToken, got {expected!r}")
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    if not db_path.exists():
        raise ExecutionRepairFenceMissingError(
            f"execution order store at {db_path} does not exist, so the order has no "
            f"committed repair fence; {execution_request_id!r} cannot be guarded"
        )
    with closing(_connect(db_path)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if not _is_lock_contention(exc):
                raise  # a corrupt/unreadable store is not "busy"; do not mask it
            raise ExecutionRepairFenceBusyError(
                f"the execution order store at {db_path} stayed write-locked for "
                f"{BUSY_TIMEOUT_SECONDS}s; the repair fence of {execution_request_id!r} "
                f"could not be held still ({type(exc).__name__})"
            ) from exc
        # A guard that cannot write can never escalate its RESERVED lock to
        # EXCLUSIVE, so it blocks writers WITHOUT ever blocking readers.
        connection.execute("PRAGMA query_only = ON")
        try:
            _verify_schema(connection, db_path)
            _assert_no_duplicate_broker_ids(connection)
            row = _fence_row(connection, execution_request_id)
            if row is None:
                raise ExecutionRepairFenceMissingError(
                    f"execution order {execution_request_id!r} has no committed repair "
                    "fence; the takeover protocol never proceeds without one"
                )
            mismatched = _fence_identity_diff(row, execution_request_id, expected)
            if mismatched:
                conflict = ExecutionRepairFenceConflictError(
                    f"execution order {execution_request_id!r} is fenced by "
                    f"{_installed_fence(row).token.describe()}, which differs from the "
                    f"guarding identity in {', '.join(mismatched)}; the takeover step "
                    "does not proceed"
                )
                # the differing column names travel with the error so the
                # reconciliation side can report them without re-reading
                conflict.mismatched_columns = tuple(mismatched)
                raise conflict
            yield _installed_fence(row)
        finally:
            connection.execute("PRAGMA query_only = OFF")
            connection.execute("ROLLBACK")


def _is_lock_contention(exc: sqlite3.Error) -> bool:
    """Is this a WRITE-LOCK contention failure (as opposed to corruption)?"""
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _fence_identity_diff(
    row, execution_request_id: str, expected: RepairFenceToken
) -> list[str]:
    """Which of the fence's six identity columns differ from ``expected``."""
    actual = {
        "execution_request_id": row["execution_request_id"],
        "repair_attempt_id": row["repair_attempt_id"],
        "generation": row["generation"],
        "takeover_id": row["takeover_id"],
        "owner_id": row["owner_id"],
        "claim_execution_id": row["claim_execution_id"],
    }
    wanted = {
        "execution_request_id": execution_request_id,
        "repair_attempt_id": expected.repair_attempt_id,
        "generation": expected.generation,
        "takeover_id": expected.takeover_id,
        "owner_id": expected.owner_id,
        "claim_execution_id": expected.claim_execution_id,
    }
    return sorted(column for column, value in actual.items() if value != wanted[column])


# --- reissue clearance evidence (Issue #115) ----------------------------------


def record_reissue_clearance(
    signal_id: str,
    *,
    evidence_id: str,
    status: str,
    account_scope: str,
    observed_at_utc: datetime,
    evidence_json: str,
    recorded_at_utc: datetime,
    expected_generation: int | None = None,
    db_path: Path | None = None,
) -> dict:
    """APPEND one clearance observation for ``signal_id`` as the next
    generation (Issue #115).

    This is the ONLY write the reconciliation side performs against the
    execution DB: it never touches order rows or state history. Prior
    generations are never rewritten or deleted, so every reference issued
    against them stays resolvable; the signal's CURRENT clearance is simply
    the highest generation. ``BEGIN IMMEDIATE`` serializes concurrent
    observers, so generations are gapless and strictly ordered; a caller
    that must build on a specific predecessor passes
    ``expected_generation`` (the generation it observed; 0 for "none") and
    gets :class:`ExecutionClearanceError` with the DB untouched when a
    newer observation was recorded in between -- the newest information
    always wins and is never silently overridden.

    ``status`` semantics (the ordering discipline is the service layer's,
    Issue #116; the store guarantees only monotonic append + fail-closed
    reads): ``observing`` = a comparison is underway, submission refused;
    ``valid`` = positive no-divergence evidence for ``account_scope`` as of
    ``observed_at_utc``; ``revoked`` = divergence / invalidation.
    ``evidence_json`` is the caller's canonical observation payload (a JSON
    object); references bind to it by sha256. The DB must already exist and
    be at the current schema -- clearance recording never creates or
    migrates a DB (fail-closed, same stance as the safety store).
    """
    _require_non_empty_str(signal_id, "signal_id")
    _require_non_empty_str(evidence_id, "evidence_id")
    if status not in KNOWN_CLEARANCE_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(KNOWN_CLEARANCE_STATUSES)}, got {status!r}"
        )
    _require_non_empty_str(account_scope, "account_scope")
    _require_utc(observed_at_utc, "observed_at_utc")
    _require_utc(recorded_at_utc, "recorded_at_utc")
    if not isinstance(evidence_json, str) or not evidence_json.strip():
        raise ValueError("evidence_json must be a non-empty JSON object string")
    try:
        payload = json.loads(evidence_json)
    except ValueError as exc:
        raise ValueError(f"evidence_json is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise ValueError("evidence_json must encode a non-empty JSON object")
    if expected_generation is not None and (
        isinstance(expected_generation, bool)
        or not isinstance(expected_generation, int)
        or expected_generation < 0
    ):
        raise ValueError(
            f"expected_generation must be a non-negative int or None, got "
            f"{expected_generation!r}"
        )
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    if not db_path.exists():
        raise ExecutionStoreError(
            f"execution DB at {db_path} does not exist; clearance evidence is only "
            "recorded into an initialized store (call init_db first)"
        )
    with _verified_txn(db_path) as connection:
        current = connection.execute(
            f"SELECT MAX(clearance_generation) FROM {CLEARANCES_TABLE} "
            "WHERE signal_id = ?",
            (signal_id,),
        ).fetchone()[0]
        current = 0 if current is None else int(current)
        if expected_generation is not None and current != expected_generation:
            raise ExecutionClearanceError(
                f"clearance for signal {signal_id!r} is at generation {current}, not "
                f"the expected {expected_generation}; a newer observation was recorded "
                "and is never silently overridden (nothing was changed)"
            )
        record = {
            "signal_id": signal_id,
            "clearance_generation": current + 1,
            "evidence_id": evidence_id,
            "status": status,
            "account_scope": account_scope,
            "observed_at_utc": _iso(observed_at_utc),
            "evidence_json": evidence_json,
            "recorded_at_utc": _iso(recorded_at_utc),
        }
        try:
            connection.execute(
                f"INSERT INTO {CLEARANCES_TABLE} ({', '.join(_CLEARANCE_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _CLEARANCE_COLUMNS)})",
                tuple(record[col] for col in _CLEARANCE_COLUMNS),
            )
        except sqlite3.IntegrityError as exc:
            raise ExecutionOrderConflictError(
                f"clearance evidence for signal {signal_id!r} collides with an "
                f"existing row (evidence_id {evidence_id!r} already recorded?): {exc}"
            ) from exc
        stored = connection.execute(
            f"SELECT * FROM {CLEARANCES_TABLE} WHERE signal_id = ? "
            "AND clearance_generation = ?",
            (signal_id, current + 1),
        ).fetchone()
        if stored is None or dict(stored) != record:  # pragma: no cover - defence
            raise _integrity(
                signal_id, "clearance row does not hold the intended values; rolling back"
            )
        stored = dict(stored)
    stored["evidence_sha256"] = _sha256_text(stored["evidence_json"])
    return stored


def _validate_clearance_rows(signal_id: str, rows: list[dict]) -> None:
    """Strict restore-time validation of one signal's clearance history:
    contiguous generations from 1, known statuses, UTC timestamps, JSON
    object payloads, non-empty identity fields. An out-of-band edited
    evidence trail raises :class:`ExecutionIntegrityError`."""
    for position, row in enumerate(rows, start=1):
        if row["clearance_generation"] != position:
            raise ExecutionIntegrityError(
                f"clearance evidence for signal {signal_id!r} failed restore-time "
                f"validation: generations are not contiguous from 1 "
                f"(found {row['clearance_generation']} at position {position})"
            )
        problems = []
        if row["status"] not in KNOWN_CLEARANCE_STATUSES:
            problems.append(f"unknown status {row['status']!r}")
        for field in ("evidence_id", "account_scope"):
            if not isinstance(row[field], str) or not row[field].strip():
                problems.append(f"{field} is empty or not a string")
        for field in ("observed_at_utc", "recorded_at_utc"):
            try:
                _parse_utc(row[field])
            except (ValueError, TypeError) as exc:
                problems.append(f"unparseable {field}: {exc}")
        try:
            payload = json.loads(row["evidence_json"])
            if not isinstance(payload, dict) or not payload:
                problems.append("evidence_json is not a non-empty JSON object")
        except (ValueError, TypeError) as exc:
            problems.append(f"unparseable evidence_json: {exc}")
        if problems:
            raise ExecutionIntegrityError(
                f"clearance evidence for signal {signal_id!r} (generation "
                f"{row['clearance_generation']}) failed restore-time validation: "
                + "; ".join(problems)
            )


def list_reissue_clearances(signal_id: str, *, db_path: Path | None = None) -> list[dict]:
    """The signal's full clearance evidence trail, oldest generation first
    (validated). Missing DB -> empty list, file never created."""
    _require_non_empty_str(signal_id, "signal_id")
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    if not db_path.exists():
        return []
    with _verified_read(db_path) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                f"SELECT * FROM {CLEARANCES_TABLE} WHERE signal_id = ? "
                "ORDER BY clearance_generation ASC",
                (signal_id,),
            ).fetchall()
        ]
    _validate_clearance_rows(signal_id, rows)
    return rows


def get_reissue_clearance(signal_id: str, *, db_path: Path | None = None) -> dict | None:
    """The signal's CURRENT clearance (highest generation), or None while no
    observation was ever recorded. Validates the whole trail it summarizes.
    Read-only: missing DB -> None, file never created."""
    rows = list_reissue_clearances(signal_id, db_path=db_path)
    return rows[-1] if rows else None


def get_repair_fence(execution_request_id: str, *, db_path: Path | None = None) -> dict | None:
    """The order's current repair fencing row, or None. Missing DB -> None;
    the file is never created (read-only contract)."""
    _require_non_empty_str(execution_request_id, "execution_request_id")
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    if not db_path.exists():
        return None
    with _verified_read(db_path) as connection:
        row = connection.execute(
            f"SELECT * FROM {FENCES_TABLE} WHERE execution_request_id = ?",
            (execution_request_id,),
        ).fetchone()
        return None if row is None else dict(row)


# --- read helpers -----------------------------------------------------------
# A missing DB file returns the empty/None default WITHOUT creating the file
# (dry-run safe). An error on an EXISTING file propagates -- never masked as
# "no data". Every read verifies the schema, scans for DB-wide broker-id
# duplicates, and re-validates the bundle it restores.


def get_execution_order(execution_request_id: str, *, db_path: Path | None = None) -> dict | None:
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    if not db_path.exists():
        return None
    with _verified_read(db_path) as connection:
        bundle = _fetch_and_validate(connection, execution_request_id)
    return None if bundle is None else bundle[0]


def get_execution_order_by_signal_id(
    signal_id: str, *, db_path: Path | None = None
) -> dict | None:
    """The signal's LATEST attempt (highest ``signal_attempt``), or None.

    Issue #95 / #115 re-defines the old "at most one row per signal"
    contract: a signal may now hold a linear reissue chain, and this lookup
    returns its tail -- the only row that can still be live. Callers that
    need the whole chain use :func:`list_execution_order_chain`. Same read
    contract as every other read helper: schema verification first, full
    bundle validation (chain links included), missing DB -> None without
    creating the file.
    """
    _require_non_empty_str(signal_id, "signal_id")
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    if not db_path.exists():
        return None
    with _verified_read(db_path) as connection:
        row = connection.execute(
            f"SELECT execution_request_id FROM {ORDERS_TABLE} WHERE signal_id = ? "
            "ORDER BY signal_attempt DESC LIMIT 1",
            (signal_id,),
        ).fetchone()
        bundle = None if row is None else _fetch_and_validate(connection, row[0])
    return None if bundle is None else bundle[0]


def list_execution_order_chain(signal_id: str, *, db_path: Path | None = None) -> list[dict]:
    """The signal's FULL reissue chain, attempt 1 first (Issue #95 / #115).

    Every row passes full bundle validation (which re-runs the shared
    parent-child rules per child), and the chain as a whole must be
    CONTIGUOUS from attempt 1 -- a gap means out-of-band damage and raises
    :class:`ExecutionIntegrityError`. Missing DB -> empty list, file never
    created.
    """
    _require_non_empty_str(signal_id, "signal_id")
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    if not db_path.exists():
        return []
    rows: list[dict] = []
    with _verified_read(db_path) as connection:
        ids = [
            row["execution_request_id"]
            for row in connection.execute(
                f"SELECT execution_request_id FROM {ORDERS_TABLE} WHERE signal_id = ? "
                "ORDER BY signal_attempt ASC",
                (signal_id,),
            ).fetchall()
        ]
        for request_id in ids:
            bundle = _fetch_and_validate(connection, request_id)
            if bundle is not None:
                rows.append(bundle[0])
    for position, row in enumerate(rows, start=1):
        if row["signal_attempt"] != position:
            raise ExecutionIntegrityError(
                f"execution chain for signal {signal_id!r} failed restore-time "
                f"validation: attempts are not contiguous from 1 (found attempt "
                f"{row['signal_attempt']} at position {position})"
            )
    return rows


def get_execution_order_by_broker_order_id(
    broker_order_id: str, *, db_path: Path | None = None
) -> dict | None:
    """The execution order OWNING this ``broker_order_id``, or None.

    Broker order ids are unique across execution orders (PR #91 review
    Major 3), so at most one row can hold it -- the same lookup shape as
    :func:`get_execution_order_by_signal_id`. Callers use it to find out
    whether a broker-assigned id is already bound to a DIFFERENT execution
    request BEFORE treating a broker observation as safely recordable
    (Issue #103 review: reflecting such an observation would collide with
    the UNIQUE constraint inside ``record_order_result`` and roll back).

    Order Reissue (Issue #102): ownership covers EVERY id an order has ever
    lived under -- the CURRENT ``broker_order_id`` column and the past ids
    recorded in the current snapshot's ``superseded_broker_order_ids``
    chain. A reissued-away id therefore still resolves to its execution
    order (audit lookups keep working across a reissue), and the duplicate
    scan run by every read guarantees at most one owner exists.

    Same read contract as every other read helper: schema verification
    first, full bundle validation of the row it returns, missing DB -> None
    without creating the file. An unassigned id simply yields None.
    """
    _require_non_empty_str(broker_order_id, "broker_order_id")
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    if not db_path.exists():
        return None
    with _verified_read(db_path) as connection:
        row = connection.execute(
            f"SELECT execution_request_id FROM {ORDERS_TABLE} WHERE broker_order_id = ?",
            (broker_order_id,),
        ).fetchone()
        if row is None:
            # not a current id anywhere -- it may be a reissued-away id
            # recorded in a snapshot's superseded chain (Issue #102)
            for request_id, snapshot_text in connection.execute(
                f"SELECT execution_request_id, last_order_result_json "
                f"FROM {ORDERS_TABLE} WHERE last_order_result_json IS NOT NULL"
            ):
                try:
                    chain = json.loads(snapshot_text).get(
                        "superseded_broker_order_ids", []
                    )
                except (ValueError, AttributeError):
                    continue  # rejected in depth by that row's own validation
                if isinstance(chain, list) and broker_order_id in chain:
                    row = (request_id,)
                    break
        bundle = None if row is None else _fetch_and_validate(connection, row[0])
    return None if bundle is None else bundle[0]


def load_execution_request(
    execution_request_id: str, *, db_path: Path | None = None
) -> ExecutionRequest | None:
    """Rebuild the typed :class:`ExecutionRequest` from its stored row.

    Construction re-runs the model's own validation, so a row this store wrote
    always round-trips; a row edited out-of-band to an invalid combination
    fails loudly here instead of leaking onward.
    """
    row = get_execution_order(execution_request_id, db_path=db_path)
    if row is None:
        return None
    return ExecutionRequest(
        execution_request_id=row["execution_request_id"],
        signal_id=row["signal_id"],
        strategy_id=row["strategy_id"],
        symbol=row["symbol"],
        side=OrderSide(row["side"]),
        order_type=OrderType(row["order_type"]),
        quantity=Decimal(row["quantity"]),
        requested_at_utc=_parse_utc(row["requested_at_utc"]),
        requested_price=None if row["requested_price"] is None else Decimal(row["requested_price"]),
        stop_loss_price=None if row["stop_loss_price"] is None else Decimal(row["stop_loss_price"]),
        take_profit_price=None
        if row["take_profit_price"] is None
        else Decimal(row["take_profit_price"]),
    )


def load_order_result(
    execution_request_id: str, *, db_path: Path | None = None
) -> OrderResult | None:
    """Rebuild the latest full broker snapshot (None while none was recorded,
    e.g. before the first broker response or after only state-only records).
    The bundle validation guarantees it equals the history-anchored snapshot."""
    row = get_execution_order(execution_request_id, db_path=db_path)
    if row is None or row["last_order_result_json"] is None:
        return None
    return _result_from_json(row["last_order_result_json"])


def get_state_history(execution_request_id: str, *, db_path: Path | None = None) -> list[dict]:
    """All state-history rows for one order, oldest first (validated)."""
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    if not db_path.exists():
        return []
    with _verified_read(db_path) as connection:
        bundle = _fetch_and_validate(connection, execution_request_id)
    return [] if bundle is None else bundle[1]


def list_unknown_orders(*, db_path: Path | None = None) -> list[dict]:
    """Orders whose broker-side outcome is unconfirmed -- the query targets.

    Exactly ``order_state = 'unknown'``: these must be resolved by a later
    broker query, and are never auto-classified as "not placed" / "rejected" /
    "retryable" by this store. (Running such queries -- reconciliation -- is
    out of scope for Issue #74.)
    """
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    if not db_path.exists():
        return []
    rows = []
    with _verified_read(db_path) as connection:
        ids = [
            row["execution_request_id"]
            for row in connection.execute(
                f"SELECT execution_request_id FROM {ORDERS_TABLE} WHERE order_state = ? "
                "ORDER BY updated_at_utc ASC",
                (OrderState.UNKNOWN.value,),
            ).fetchall()
        ]
        for request_id in ids:
            bundle = _fetch_and_validate(connection, request_id)
            if bundle is not None:
                rows.append(bundle[0])
    return rows


def list_in_flight_orders(*, db_path: Path | None = None) -> list[dict]:
    """Every order whose broker-side outcome is not settled yet (Issue #103).

    Exactly ``order_state IN (pending, accepted, partially_filled,
    unknown)`` -- the orders a reconciliation run has to compare against the
    broker: a still-working order may have been filled / cancelled /
    rejected broker-side without us observing it, and an UNKNOWN order may
    have been settled all along. Terminal orders (filled / cancelled /
    rejected) are deliberately excluded: their outcome can never change
    (``TERMINAL_ORDER_STATES``), and ``created`` intents were never sent.

    Read-only and additive: same contract as the other read helpers (missing
    DB -> empty list without creating the file, schema verified first, every
    returned row passed full bundle validation). Oldest ``updated_at_utc``
    first.
    """
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    if not db_path.exists():
        return []
    in_flight = sorted(s.value for s in OrderState if s not in TERMINAL_ORDER_STATES)
    placeholders = ", ".join("?" for _ in in_flight)
    rows = []
    with _verified_read(db_path) as connection:
        ids = [
            row["execution_request_id"]
            for row in connection.execute(
                f"SELECT execution_request_id FROM {ORDERS_TABLE} "
                f"WHERE order_state IN ({placeholders}) "
                "ORDER BY updated_at_utc ASC, execution_request_id ASC",
                tuple(in_flight),
            ).fetchall()
        ]
        for request_id in ids:
            bundle = _fetch_and_validate(connection, request_id)
            if bundle is not None:
                rows.append(bundle[0])
    return rows


def list_reconciliation_orders(*, db_path: Path | None = None) -> list[dict]:
    """Every execution order for which a broker state exists (Issue #103).

    This is the complete comparison set for reconciliation: all in-flight
    rows plus terminal rows.  A terminal state is immutable *inside BELIAL*,
    but a later broker query can still contradict the stored terminal fact;
    that contradiction must be detected for audit/manual review rather than
    silently skipped.  ``created`` intents are excluded because no submit
    attempt has occurred and therefore ``order_state`` is NULL.

    Read-only, never creates or migrates a missing DB, and validates every
    returned order/history bundle.  Ordering is total and deterministic.
    """
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    if not db_path.exists():
        return []
    rows = []
    with _verified_read(db_path) as connection:
        ids = [
            row["execution_request_id"]
            for row in connection.execute(
                f"SELECT execution_request_id FROM {ORDERS_TABLE} "
                "WHERE order_state IS NOT NULL "
                "ORDER BY updated_at_utc ASC, execution_request_id ASC"
            ).fetchall()
        ]
        for request_id in ids:
            bundle = _fetch_and_validate(connection, request_id)
            if bundle is not None:
                rows.append(bundle[0])
    return rows


def list_client_order_ids(*, db_path: Path | None = None) -> list[str]:
    """Every ``client_order_id`` bound to an execution request (Issue #103).

    Reconciliation needs the COMPLETE set -- terminal and ``created`` rows
    included -- to decide whether a broker-side order is unknown to BELIAL:
    matching only against in-flight orders would report a settled order as
    "broker-only". Only the binding column is read (no bundle restore), so
    this stays a cheap membership lookup; every state-bearing read path
    keeps its full validation. Read-only, never creates a missing DB.
    """
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    if not db_path.exists():
        return []
    with _verified_read(db_path) as connection:
        return [
            row["client_order_id"]
            for row in connection.execute(
                f"SELECT client_order_id FROM {ORDERS_TABLE} ORDER BY client_order_id ASC"
            ).fetchall()
        ]


def list_unresolved_submissions(*, db_path: Path | None = None) -> list[dict]:
    """Every submission whose broker-side outcome is not yet recorded as a
    confirmed observation (Issue #75 review r3) -- read-only, additive.

    Two shapes are returned, oldest first:

    - ``order_state = 'unknown'`` -- the classic query targets (same set as
      :func:`list_unknown_orders`), and
    - ``submission_phase = 'submitting'`` with ``order_state = 'pending'`` --
      a submit attempt STARTED but no broker observation was ever recorded:
      the process died between the transition and the submit call, or the
      broker answered and both the result persistence and the UNKNOWN
      fallback failed. The broker may or may not hold such an order, so it
      must stay visible to reconciliation instead of vanishing behind a
      persistence error.

    ``created`` intents (no submit attempt) and confirmed observations
    (phase ``submitted``) are deliberately NOT included. Missing DB -> empty
    list, file never created; every returned row passed full bundle
    validation.
    """
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    if not db_path.exists():
        return []
    rows = []
    with _verified_read(db_path) as connection:
        ids = [
            row["execution_request_id"]
            for row in connection.execute(
                f"SELECT execution_request_id FROM {ORDERS_TABLE} "
                "WHERE order_state = ? OR (submission_phase = ? AND order_state = ?) "
                "ORDER BY updated_at_utc ASC",
                (OrderState.UNKNOWN.value, PHASE_SUBMITTING, OrderState.PENDING.value),
            ).fetchall()
        ]
        for request_id in ids:
            bundle = _fetch_and_validate(connection, request_id)
            if bundle is not None:
                rows.append(bundle[0])
    return rows
