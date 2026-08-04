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
intent, and ``broker_order_id`` must follow the NULL -> value -> same-value
chain across ALL history rows. Rewriting the current row alone (state,
broker id or snapshot) therefore always fails restore-time validation --
including while the tracked state is UNKNOWN, which is deliberately NOT an
escape hatch: a state-only UNKNOWN never touches the snapshot, so the
current snapshot must still equal the latest anchored one.

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
constraint guards writes; restore additionally scans the whole table and
refuses to restore ANY row while a duplicate exists (a corrupted /
constraint-stripped DB must not be readable back into use).

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

import json
import logging
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from broker.broker_models import (
    Fill,
    OrderResult,
    OrderSide,
    OrderState,
    OrderType,
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
SCHEMA_VERSION = 3
_MIGRATABLE_VERSION = 2

# Strict snapshot JSON shape (PR #91 review r2 Minor 1): the key sets must
# match EXACTLY -- unknown fields are rejected, missing fields are rejected.
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
        "fills",
    }
)
_FILL_JSON_KEYS: frozenset[str] = frozenset({"fill_id", "price", "quantity", "filled_at_utc"})


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


def _create_orders_table_sql() -> str:
    """Full current schema. Decimals are stored as canonical TEXT (never REAL:
    live-order arithmetic must not pick up float error, Issue #73/#74). The
    CHECKs mirror the application-level invariants as a second guard.
    NOT NULL on the PRIMARY KEY is explicit (a rowid-table TEXT PRIMARY KEY
    would admit NULLs) and the table is WITHOUT ROWID."""
    sides = _enum_values_sql(OrderSide)
    types = _enum_values_sql(OrderType)
    states = _enum_values_sql(OrderState)
    phases = "', '".join(sorted(KNOWN_PHASES))
    broker_ack = "', '".join(sorted(s.value for s in _STATES_REQUIRING_BROKER_ID))
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
            updated_at_utc TEXT NOT NULL CHECK (updated_at_utc LIKE '%+00:00'),
            CHECK ((submission_phase = '{PHASE_CREATED}') = (order_state IS NULL)),
            CHECK ((order_type = '{OrderType.LIMIT.value}') = (requested_price IS NOT NULL)),
            CHECK (order_state IS NULL OR order_state NOT IN ('{broker_ack}') OR broker_order_id IS NOT NULL)
        ) WITHOUT ROWID
    """


def _create_history_table_sql() -> str:
    states = _enum_values_sql(OrderState)
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
            changed_at_utc TEXT NOT NULL CHECK (changed_at_utc LIKE '%+00:00')
        )
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
    if version >= SCHEMA_VERSION:
        index_sql[_SIGNAL_UNIQUE_INDEX] = _SIGNAL_UNIQUE_INDEX_SQL
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


def _create_schema(connection: sqlite3.Connection, *, version: int = SCHEMA_VERSION) -> None:
    """Create the schema for ``version`` (v2 is used only as the in-memory
    migration reference and by migration tests; real DBs are created at the
    current version)."""
    connection.execute(_create_orders_table_sql())
    connection.execute(_create_history_table_sql())
    for create_sql in _create_index_sql(version=version).values():
        connection.execute(create_sql)
    connection.execute(f"PRAGMA user_version = {version}")


def _structure_signature(connection: sqlite3.Connection) -> dict:
    """Structural signature from the live schema objects (PR #91 r2 Minor 2).

    Built from ``PRAGMA table_info`` / ``index_list`` / ``index_info`` /
    ``foreign_key_list`` -- so verification does not rest on the
    ``sqlite_master`` SQL text alone."""
    signature: dict = {}
    for table in (ORDERS_TABLE, HISTORY_TABLE):
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
            _REFERENCE_SCHEMA_CACHE[version] = (objects, _structure_signature(connection))
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

    if not problems and _structure_signature(connection) != expected_signature:
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
        if not _schema_problems(connection, version=_MIGRATABLE_VERSION):
            problems.append(
                f"the DB is a valid v{_MIGRATABLE_VERSION} schema; run init_db() to "
                f"migrate it to v{SCHEMA_VERSION} (reads never migrate)"
            )
        raise ExecutionSchemaError(
            f"execution DB at {db_path} failed schema verification; refusing to touch it: "
            + "; ".join(problems)
        )


def _assert_no_duplicate_broker_ids(connection: sqlite3.Connection) -> None:
    """DB-wide broker_order_id uniqueness scan (PR #91 review Major 3).

    The UNIQUE constraint guards writes; this scan additionally refuses to
    RESTORE anything from a DB that somehow holds duplicates (e.g. a
    constraint-stripped copy). NULLs are not duplicates of each other.
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
        connection.execute("BEGIN IMMEDIATE")
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
            f"cannot migrate execution DB at {db_path} to schema v{SCHEMA_VERSION}: "
            f"unique signal_id index creation failed: {exc} (nothing was changed)"
        ) from exc
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    _LOGGER.info(
        "migrated execution DB %s from schema v%d to v%d (added unique signal_id index %s)",
        db_path,
        _MIGRATABLE_VERSION,
        SCHEMA_VERSION,
        _SIGNAL_UNIQUE_INDEX,
    )


def init_db(db_path: Path | None = None) -> None:
    """Create the Issue #74 schema, verify an existing one, or migrate a
    valid v2 schema to v3 (Issue #76); idempotent.

    - PRISTINE DB only (PR #91 review r2 Major 2): the schema is created only
      when ``sqlite_master`` holds ZERO objects AND ``user_version`` is 0. A
      version-only DB, a metadata-only DB (e.g. a stray ``sqlite_sequence``),
      or any other object present means this is NOT a fresh DB.
    - A DB that fully matches the CURRENT schema passes verification.
    - A DB that fully matches the released v2 schema -- and ONLY such a DB --
      is migrated in place (:func:`_migrate_v2_to_v3`): the migration, the
      version bump and a full v3 re-verification commit in ONE transaction,
      so a failed migration leaves no half-migrated schema behind.
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
                if _schema_problems(connection, version=_MIGRATABLE_VERSION):
                    # neither a valid current nor a valid v2 schema: fail
                    # with the full diagnosis, DB untouched
                    _verify_schema(connection, db_path)
                _migrate_v2_to_v3(connection, db_path)
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
    if not isinstance(data, dict) or set(data) != _RESULT_JSON_KEYS:
        raise ValueError(
            f"snapshot JSON keys {sorted(data) if isinstance(data, dict) else type(data).__name__} "
            f"do not match the expected set {sorted(_RESULT_JSON_KEYS)}"
        )
    if not isinstance(data["fills"], list):
        raise ValueError("snapshot JSON 'fills' must be a list")
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
    )


# --- restore-time integrity validation ---------------------------------------


def _integrity(execution_request_id: str, message: str) -> ExecutionIntegrityError:
    return ExecutionIntegrityError(
        f"execution order {execution_request_id!r} failed restore-time validation: {message}"
    )


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
            # broker-id chain: NULL -> value once, then the same value forever
            prior_broker = previous["broker_order_id"]
            if prior_broker is not None and entry["broker_order_id"] != prior_broker:
                raise _integrity(
                    request_id,
                    f"history row {entry['history_id']} changes broker_order_id "
                    f"{prior_broker!r} -> {entry['broker_order_id']!r}",
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
    for column, reason in (
        ("execution_request_id", "execution_request_id is already persisted"),
        ("signal_id", "source signal already produced an execution request"),
        ("client_order_id", "client_order_id is already bound to an execution request"),
    ):
        row = connection.execute(
            f"SELECT execution_request_id, signal_id, submission_phase, order_state "
            f"FROM {ORDERS_TABLE} WHERE {column} = ?",
            (record[column],),
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
    record = {
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
    }
    insert_sql = (
        f"INSERT INTO {ORDERS_TABLE} ({', '.join(_ORDER_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _ORDER_COLUMNS)})"
    )
    with _verified_txn(db_path) as connection:
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
                "signal_id=%s existing_execution_request_id=%s "
                "existing_submission_phase=%s existing_order_state=%s "
                "conflicting_keys=%s reason=%s",
                request.execution_request_id,
                request.signal_id,
                existing["execution_request_id"],
                existing["submission_phase"],
                existing["order_state"],
                ",".join(item[0] for item in conflicts),
                reason,
            )
            raise ExecutionOrderConflictError(
                f"execution order {request.execution_request_id!r} (signal_id "
                f"{request.signal_id!r} / client_order_id {client_order_id!r}) collides "
                f"with an existing row ({reason}): {exc}"
            ) from exc
        created = _validate_writeback(
            connection,
            request.execution_request_id,
            prior_history=[],
            expected_row=record,
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
) -> None:
    connection.execute(
        f"INSERT INTO {HISTORY_TABLE} "
        "(execution_request_id, previous_state, new_state, reason, broker_order_id, "
        "order_result_json, changed_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            execution_request_id,
            None if previous_state is None else previous_state.value,
            new_state.value,
            reason,
            broker_order_id,
            order_result_json,
            _iso(changed_at_utc),
        ),
    )


def mark_submitting(
    execution_request_id: str,
    *,
    reason: str,
    changed_at_utc: datetime,
    db_path: Path | None = None,
) -> dict:
    """Move ``created -> submitting`` and start broker-state tracking at PENDING.

    Entered exactly once: any phase other than ``created`` is refused with
    ``ValueError`` (a lost submit outcome is UNKNOWN territory, resolved by
    query -- never by silently re-entering the submit phase). The phase
    change, the PENDING state, and the first history row (previous_state
    NULL) commit in one transaction. Under two racing connections the
    ``BEGIN IMMEDIATE`` transaction serializes them and exactly one wins.
    """
    _require_non_empty_str(reason, "reason")
    _require_utc(changed_at_utc, "changed_at_utc")
    db_path = _existing_db_path(db_path, execution_request_id)
    with _verified_txn(db_path) as connection:
        row, history = _require_bundle_for_update(connection, execution_request_id)
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
        )
        expected_row = dict(row)
        expected_row.update(
            submission_phase=PHASE_SUBMITTING,
            order_state=OrderState.PENDING.value,
            last_state_reason=reason,
            updated_at_utc=_iso(changed_at_utc),
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
            },
        )
    return updated


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
) -> dict:
    """Shared tail of both update paths: phase derivation + current-row UPDATE
    + history INSERT (snapshot anchored on the history row too) + pre-commit
    write-back validation (:func:`_validate_writeback`), inside the caller's
    transaction. A UNIQUE violation (DB-wide ``broker_order_id``, PR #91
    Major 3) surfaces as :class:`ExecutionOrderConflictError`; the caller's
    transaction rolls back the whole update, history row included. Returns
    the validated post-update row."""
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
        )
    except sqlite3.IntegrityError as exc:
        raise ExecutionOrderConflictError(
            f"state update for execution order {execution_request_id!r} violates a "
            f"uniqueness constraint (broker_order_id {effective_broker_id!r} already "
            f"assigned to another order?): {exc}"
        ) from exc
    expected_row = dict(row)
    expected_row.update(
        submission_phase=phase,
        order_state=new_state.value,
        broker_order_id=effective_broker_id,
        last_state_reason=reason,
        last_order_result_json=keep_json,
        updated_at_utc=_iso(changed_at_utc),
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
    - ``broker_order_id`` follows NULL -> value once, then immutable; a full
      snapshot must carry the known id itself (dropping it to NULL is
      refused),
    - the snapshot (current row AND history row), the state and the history
      entry commit in ONE transaction; a DB-wide ``broker_order_id``
      uniqueness conflict rolls all of it back
      (:class:`ExecutionOrderConflictError`).
    """
    if not isinstance(result, OrderResult):
        raise ValueError(f"result must be an OrderResult, got {result!r}")
    if previous_result is not None and not isinstance(previous_result, OrderResult):
        raise ValueError(f"previous_result must be an OrderResult, got {previous_result!r}")
    _require_non_empty_str(reason, "reason")
    db_path = _existing_db_path(db_path, execution_request_id)
    with _verified_txn(db_path) as connection:
        row, history = _require_bundle_for_update(connection, execution_request_id)

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

        # broker id: NULL -> value once, then immutable; a full snapshot must
        # carry the known id itself (row and snapshot must stay in lockstep).
        if row["broker_order_id"] is not None and result.broker_order_id != row["broker_order_id"]:
            raise ValueError(
                f"broker_order_id is immutable once set: "
                f"{row['broker_order_id']!r} -> {result.broker_order_id!r} refused"
            )
        effective_broker_id = (
            row["broker_order_id"] if result.broker_order_id is None else result.broker_order_id
        )
        if result.state in _STATES_REQUIRING_BROKER_ID and effective_broker_id is None:
            raise ValueError(f"state {result.state.value!r} requires a broker_order_id")
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
        )
    return updated


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
    """The execution order created from this source signal, or None.

    At most one can exist (Issue #76: ``signal_id`` is UNIQUE), so callers
    can look up what a signal already produced -- e.g. to report why a new
    intent was suppressed. Same read contract as every other read helper:
    schema verification first, full bundle validation, missing DB -> None
    without creating the file.
    """
    _require_non_empty_str(signal_id, "signal_id")
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    if not db_path.exists():
        return None
    with _verified_read(db_path) as connection:
        row = connection.execute(
            f"SELECT execution_request_id FROM {ORDERS_TABLE} WHERE signal_id = ?",
            (signal_id,),
        ).fetchone()
        bundle = None if row is None else _fetch_and_validate(connection, row[0])
    return None if bundle is None else bundle[0]


def get_execution_order_by_broker_order_id(
    broker_order_id: str, *, db_path: Path | None = None
) -> dict | None:
    """The execution order currently OWNING this ``broker_order_id``, or None.

    ``broker_order_id`` is UNIQUE across the whole table (PR #91 review
    Major 3), so at most one row can hold it -- the same lookup shape as
    :func:`get_execution_order_by_signal_id`. Callers use it to find out
    whether a broker-assigned id is already bound to a DIFFERENT execution
    request BEFORE treating a broker observation as safely recordable
    (Issue #103 review: reflecting such an observation would collide with
    the UNIQUE constraint inside ``record_order_result`` and roll back).

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
