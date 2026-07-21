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

Snapshot persistence (PR #91 review, Major 1): every broker observation that
carries a full ``OrderResult`` is persisted COMPLETELY -- the latest snapshot
is serialized to JSON (Decimals as canonical strings, datetimes as UTC ISO
strings, fills included) in ``last_order_result_json``, committed in the SAME
transaction as the current-row update and the history row. On every
result-bearing update the store deserializes the DB snapshot and runs
``validate_order_result_update(db_snapshot, new)`` -- so a stale in-memory
snapshot can never erase known fills, roll back ``filled_quantity`` /
``remaining_quantity``, drop ``broker_order_id``, or rewind ``updated_at_utc``,
even across a process restart. A caller-supplied ``previous_result`` is only
accepted when it EQUALS the DB snapshot. State-only updates
(:func:`record_order_state`) are restricted to the one case where no full
``OrderResult`` exists by construction: recording UNKNOWN after a submit /
query whose outcome was lost (e.g. ``BrokerTimeoutError``).

``broker_order_id`` is UNIQUE across the WHOLE table (PR #91 review,
Major 3): one broker order can never be attached to two execution requests;
a conflicting update rolls back the current-row update and the history append
together and raises :class:`ExecutionOrderConflictError`.

UNKNOWN handling (Issue #74): ``unknown`` is stored as-is and keeps the phase
at ``submitting`` -- the broker may or may not hold the order. UNKNOWN rows
are query targets (:func:`list_unknown_orders`); they are resolved ONLY by
recording a later observation through the normal validated update path. A
state-only UNKNOWN does NOT touch the last full snapshot, so a later
resolution is still validated against the fills known before the outcome was
lost. This store never converts UNKNOWN into "not placed" / "rejected" /
"retryable", and re-submission is not modelled at all: ``client_order_id``
is bound 1:1 to the request and immutable (see ``execution_models`` for the
ID design).

Update contract (every state update, one ``BEGIN IMMEDIATE`` transaction):

- transition validated by ``validate_order_state_transition``; content
  validated by ``validate_order_result_update`` against the DB snapshot
  whenever one exists,
- current-row update, snapshot update and history append commit atomically
  (all or nothing; a UNIQUE ``broker_order_id`` conflict rolls back all),
- ``broker_order_id`` may go NULL -> value once and is then immutable
  (a state-only observation that omits it keeps the stored value; a
  DIFFERENT value -- or dropping it from a full snapshot -- is refused),
- ``updated_at_utc`` is monotonically non-decreasing,
- terminal states are never left (enforced by the broker transition table).

Same-state re-observation (PR #87 contract): allowed for every state,
terminal included. Design decision for persistence: a same-state
re-observation IS recorded -- it refreshes ``updated_at_utc`` /
``last_state_reason`` and appends a history row, so reconciliation
observations leave an audit trail. Callers that do not want history rows for
polling no-ops simply skip calling the store.

History rows record ``previous_state`` (NULL for the first entry, i.e.
created -> pending), ``new_state``, ``reason``, ``broker_order_id`` as of the
change, and ``changed_at_utc``.

Schema management (PR #91 review, Major 2): :func:`init_db` is idempotent,
but "current" is decided by FULL structural verification, never by column
names alone -- ``PRAGMA user_version``, the complete normalized CREATE SQL of
both tables (all NOT NULL / UNIQUE / CHECK / FK constraints), the required
indexes, and the absence of unexpected tables are all checked. A partial
schema (one table missing), a constraint-less lookalike, or a foreign DB
fails with :class:`ExecutionSchemaError` WITHOUT modifying the file -- this
is a brand-new, never-released component, so there is deliberately no
legacy-migration path and nothing is auto-completed or silently rebuilt.
Key invariants (non-empty IDs / reasons, UTC suffix, MARKET/LIMIT x
requested_price, state x broker_order_id) are additionally enforced by DB
CHECK constraints as a second guard under the application-level validation.

Restore-time integrity (PR #91 review, Major 2): every read that returns an
order re-validates what it loads -- enum membership, UTC parseability,
non-empty IDs / reasons, the current/history chain (first entry is
created -> pending, links are gapless, every pair is a legal transition, the
last entry matches the current row), phase consistency, and the snapshot
JSON (round-trips into a valid ``OrderResult`` that belongs to this order).
A DB edited out-of-band into an inconsistent state raises
:class:`ExecutionIntegrityError` instead of being trusted. Read helpers
never create the DB file (dry-run safe) and never swallow read errors on an
existing file.
"""

from __future__ import annotations

import json
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
    validate_order_result_update,
    validate_order_state_transition,
)
from execution.execution_models import ExecutionRequest, _require_non_empty_str, _require_utc

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
    "changed_at_utc",
)
REQUIRED_ORDER_COLUMNS: frozenset[str] = frozenset(_ORDER_COLUMNS)
REQUIRED_HISTORY_COLUMNS: frozenset[str] = frozenset(_HISTORY_COLUMNS)

SCHEMA_VERSION = 1

# AUTOINCREMENT bookkeeping table SQLite creates itself.
_ALLOWED_EXTRA_TABLES: frozenset[str] = frozenset({"sqlite_sequence"})


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
    (execution_request_id / client_order_id / broker_order_id)."""


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


# --- schema (full CREATE SQL is the verification reference) ------------------


def _enum_values_sql(values) -> str:
    return "', '".join(sorted(v.value for v in values))


def _create_orders_table_sql() -> str:
    """Full current schema. Decimals are stored as canonical TEXT (never REAL:
    live-order arithmetic must not pick up float error, Issue #73/#74). The
    CHECKs mirror the application-level invariants as a second guard."""
    sides = _enum_values_sql(OrderSide)
    types = _enum_values_sql(OrderType)
    states = _enum_values_sql(OrderState)
    phases = "', '".join(sorted(KNOWN_PHASES))
    broker_ack = "', '".join(sorted(s.value for s in _STATES_REQUIRING_BROKER_ID))
    return f"""
        CREATE TABLE {ORDERS_TABLE} (
            execution_request_id TEXT PRIMARY KEY CHECK (trim(execution_request_id) <> ''),
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
        )
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
            changed_at_utc TEXT NOT NULL CHECK (changed_at_utc LIKE '%+00:00')
        )
    """


_ORDERS_STATE_INDEX = f"idx_{ORDERS_TABLE}_state"
_HISTORY_REQUEST_INDEX = f"idx_{HISTORY_TABLE}_request"


def _create_index_sql() -> dict[str, str]:
    return {
        _ORDERS_STATE_INDEX: (
            f"CREATE INDEX {_ORDERS_STATE_INDEX} ON {ORDERS_TABLE} (order_state, updated_at_utc)"
        ),
        _HISTORY_REQUEST_INDEX: (
            f"CREATE INDEX {_HISTORY_REQUEST_INDEX} ON {HISTORY_TABLE} "
            "(execution_request_id, history_id)"
        ),
    }


def _expected_tables() -> dict[str, str]:
    return {ORDERS_TABLE: _create_orders_table_sql(), HISTORY_TABLE: _create_history_table_sql()}


def _normalized_sql(sql) -> str:
    return " ".join(str(sql or "").lower().split())


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def _immediate_txn(db_path: Path):
    """One explicit ``BEGIN IMMEDIATE ... COMMIT`` transaction (ROLLBACK on error)."""
    with closing(_connect(db_path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).fetchone()
    return row is not None


def _verify_schema(connection: sqlite3.Connection, db_path: Path) -> None:
    """Full structural verification; raises :class:`ExecutionSchemaError`.

    Never trusts column names alone: the schema version, the COMPLETE
    normalized CREATE SQL of both tables (so every NOT NULL / UNIQUE / CHECK /
    FK constraint is verified), the required indexes, and the absence of
    unexpected tables are all checked. Read-only -- makes no change to the DB.
    """
    problems: list[str] = []
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        problems.append(f"user_version is {version}, expected {SCHEMA_VERSION}")

    tables = {
        row["name"]: row["sql"]
        for row in connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
    }
    expected = _expected_tables()
    for name in sorted(set(expected) - set(tables)):
        problems.append(f"table {name!r} is missing")
    for name in sorted(set(tables) - set(expected) - _ALLOWED_EXTRA_TABLES):
        problems.append(f"unexpected table {name!r} present")
    for name, create_sql in expected.items():
        if name in tables and _normalized_sql(tables[name]) != _normalized_sql(create_sql):
            problems.append(
                f"table {name!r} does not match the expected schema "
                "(constraints missing or altered)"
            )

    indexes = {
        row["name"]: row["sql"]
        for row in connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'index'")
        if not row["name"].startswith("sqlite_autoindex_")
    }
    for name, create_sql in _create_index_sql().items():
        if name not in indexes:
            problems.append(f"index {name!r} is missing")
        elif _normalized_sql(indexes[name]) != _normalized_sql(create_sql):
            problems.append(f"index {name!r} does not match the expected definition")
    for name in sorted(set(indexes) - set(_create_index_sql())):
        problems.append(f"unexpected index {name!r} present")

    if problems:
        raise ExecutionSchemaError(
            f"execution DB at {db_path} failed schema verification; refusing to touch it: "
            + "; ".join(problems)
        )


def init_db(db_path: Path | None = None) -> None:
    """Create the Issue #74 schema, or verify an existing one; idempotent.

    - Fresh DB (neither of our tables present, no foreign tables): create both
      tables, both indexes, and stamp ``PRAGMA user_version`` in one
      transaction.
    - Anything already present: FULL structural verification
      (:func:`_verify_schema`). A partial schema, a constraint-less lookalike,
      a wrong version, or a foreign table raises
      :class:`ExecutionSchemaError` WITHOUT modifying the DB -- brand-new
      component, brand-new DB, so there is no legacy-migration or
      auto-completion path.
    """
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _immediate_txn(db_path) as connection:
        ours_present = [t for t in _expected_tables() if _table_exists(connection, t)]
        if not ours_present:
            foreign = [
                row["name"]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                if row["name"] not in _ALLOWED_EXTRA_TABLES
            ]
            if foreign:
                raise ExecutionSchemaError(
                    f"DB at {db_path} contains foreign tables {sorted(foreign)}; "
                    "refusing to create the execution schema in it"
                )
            connection.execute(_create_orders_table_sql())
            connection.execute(_create_history_table_sql())
            for create_sql in _create_index_sql().values():
                connection.execute(create_sql)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        else:
            _verify_schema(connection, db_path)


# --- OrderResult snapshot serialization --------------------------------------


def _result_to_json(result: OrderResult) -> str:
    """Serialize a full snapshot losslessly: Decimals as canonical strings,
    datetimes as UTC ISO strings, fills in order."""
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
    """Rebuild the snapshot; ``OrderResult`` construction re-runs every
    invariant, so a tampered snapshot fails loudly instead of being trusted."""
    data = json.loads(text)

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


def _validate_bundle(row: dict, history: list[dict]) -> None:
    """Cross-validate the current row, its history chain and its snapshot.

    Restore-time guard (PR #91 review): a DB edited out-of-band into an
    inconsistent state must be rejected on load, not trusted.
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
        return

    try:
        current_state = OrderState(state_text)
    except ValueError as exc:
        raise _integrity(request_id, str(exc)) from exc

    # history chain: gapless links, legal transitions, sane content
    previous: dict | None = None
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

    confirmed_seen = any(h["new_state"] in {s.value for s in _CONFIRMED_STATES} for h in history)
    if (phase == PHASE_SUBMITTED) != confirmed_seen:
        raise _integrity(
            request_id,
            f"phase {phase!r} inconsistent with confirmed observations in history",
        )
    if row["last_state_reason"] is None or not str(row["last_state_reason"]).strip():
        raise _integrity(request_id, "last_state_reason is empty for a submitted-phase order")

    snapshot_text = row["last_order_result_json"]
    if snapshot_text is None:
        return
    try:
        snapshot = _result_from_json(snapshot_text)
    except Exception as exc:  # malformed JSON or a snapshot violating OrderResult invariants
        raise _integrity(request_id, f"stored order-result snapshot is invalid: {exc}") from exc
    if snapshot.client_order_id != row["client_order_id"]:
        raise _integrity(request_id, "snapshot client_order_id does not match the order")
    if (
        snapshot.symbol != row["symbol"]
        or snapshot.side.value != row["side"]
        or snapshot.order_type.value != row["order_type"]
        or snapshot.requested_quantity != Decimal(row["quantity"])
        or (snapshot.requested_price is None) != (row["requested_price"] is None)
        or (
            snapshot.requested_price is not None
            and snapshot.requested_price != Decimal(row["requested_price"])
        )
    ):
        raise _integrity(request_id, "snapshot identity fields do not match the stored request")
    # the only legal way for the tracked state to diverge from the snapshot's
    # state is a later state-only UNKNOWN (which never touches the snapshot)
    if snapshot.state is not current_state and current_state is not OrderState.UNKNOWN:
        raise _integrity(
            request_id,
            f"snapshot state {snapshot.state.value!r} inconsistent with current "
            f"order_state {current_state.value!r}",
        )
    if snapshot.broker_order_id is not None and snapshot.broker_order_id != row["broker_order_id"]:
        raise _integrity(request_id, "snapshot broker_order_id does not match the order")
    if _parse_utc(row["updated_at_utc"]) < snapshot.updated_at_utc:
        raise _integrity(request_id, "updated_at_utc is older than the stored snapshot")


def _read_bundle(db_path: Path, execution_request_id: str) -> tuple[dict, list[dict]] | None:
    """Load one order row + its full history in ONE connection and validate."""
    if not db_path.exists():
        return None
    with closing(_connect(db_path)) as connection:
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


# --- write paths -------------------------------------------------------------


def create_execution_order(
    request: ExecutionRequest,
    *,
    client_order_id: str,
    created_at_utc: datetime,
    db_path: Path | None = None,
) -> dict:
    """Persist a new intent in phase ``created`` (no broker state yet).

    ``client_order_id`` is bound here, once, 1:1 (UNIQUE) -- see the ID design
    in ``execution_models``. A duplicate ``execution_request_id`` or
    ``client_order_id`` raises :class:`ExecutionOrderConflictError` (an intent
    is inserted exactly once; there is no idempotent re-insert semantics at
    this layer). No history row is written: history records STATE changes and
    a created intent has no order state.
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
    with _immediate_txn(db_path) as connection:
        try:
            connection.execute(insert_sql, tuple(record[col] for col in _ORDER_COLUMNS))
        except sqlite3.IntegrityError as exc:
            raise ExecutionOrderConflictError(
                f"execution order {request.execution_request_id!r} / client_order_id "
                f"{client_order_id!r} collides with an existing row: {exc}"
            ) from exc
    return _require_get(request.execution_request_id, db_path=db_path)


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


def _load_row_for_update(connection: sqlite3.Connection, execution_request_id: str) -> dict:
    row = connection.execute(
        f"SELECT * FROM {ORDERS_TABLE} WHERE execution_request_id = ?", (execution_request_id,)
    ).fetchone()
    if row is None:
        raise ExecutionOrderNotFoundError(f"no execution order with id {execution_request_id!r}")
    return dict(row)


def _append_history(
    connection: sqlite3.Connection,
    *,
    execution_request_id: str,
    previous_state: OrderState | None,
    new_state: OrderState,
    reason: str,
    broker_order_id: str | None,
    changed_at_utc: datetime,
) -> None:
    connection.execute(
        f"INSERT INTO {HISTORY_TABLE} "
        "(execution_request_id, previous_state, new_state, reason, broker_order_id, changed_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            execution_request_id,
            None if previous_state is None else previous_state.value,
            new_state.value,
            reason,
            broker_order_id,
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
    with _immediate_txn(db_path) as connection:
        row = _load_row_for_update(connection, execution_request_id)
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
            changed_at_utc=changed_at_utc,
        )
    return _require_get(execution_request_id, db_path=db_path)


def _apply_state_update(
    connection: sqlite3.Connection,
    row: dict,
    *,
    new_state: OrderState,
    reason: str,
    changed_at_utc: datetime,
    effective_broker_id: str | None,
    result_json: str | None,
) -> None:
    """Shared tail of both update paths: phase derivation + current-row UPDATE
    + history INSERT, inside the caller's transaction. A UNIQUE violation
    (DB-wide ``broker_order_id``, PR #91 Major 3) surfaces as
    :class:`ExecutionOrderConflictError`; the caller's transaction rolls back
    the whole update, history row included."""
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
            changed_at_utc=changed_at_utc,
        )
    except sqlite3.IntegrityError as exc:
        raise ExecutionOrderConflictError(
            f"state update for execution order {execution_request_id!r} violates a "
            f"uniqueness constraint (broker_order_id {effective_broker_id!r} already "
            f"assigned to another order?): {exc}"
        ) from exc


def _common_update_checks(
    row: dict,
    *,
    new_state: OrderState,
    changed_at_utc: datetime,
    broker_order_id: str | None,
) -> str | None:
    """Transition + broker-id + timestamp invariants shared by both update
    paths. Returns the effective broker_order_id to store."""
    execution_request_id = row["execution_request_id"]
    if row["submission_phase"] == PHASE_CREATED:
        raise ValueError(
            f"execution order {execution_request_id!r} has no submit attempt yet "
            f"(phase {PHASE_CREATED!r}); call mark_submitting first"
        )
    current_state = OrderState(row["order_state"])
    validate_order_state_transition(current_state, new_state)

    stored_broker_id = row["broker_order_id"]
    if broker_order_id is None:
        effective_broker_id = stored_broker_id
    elif stored_broker_id is not None and broker_order_id != stored_broker_id:
        raise ValueError(
            f"broker_order_id is immutable once set: "
            f"{stored_broker_id!r} -> {broker_order_id!r} refused"
        )
    else:
        effective_broker_id = broker_order_id
    if new_state in _STATES_REQUIRING_BROKER_ID and effective_broker_id is None:
        raise ValueError(f"state {new_state.value!r} requires a broker_order_id")

    if _parse_utc(row["updated_at_utc"]) > changed_at_utc:
        raise ValueError(
            f"updated_at_utc must not go backwards: {row['updated_at_utc']} -> "
            f"{_iso(changed_at_utc)}"
        )
    return effective_broker_id


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
    enforced against the DB snapshot. A state-only UNKNOWN never touches the
    stored snapshot, so the fills known before the outcome was lost keep
    guarding any later resolution.

    The transition is validated with ``validate_order_state_transition``;
    the current-row update and the history append commit atomically.
    ``broker_order_id=None`` keeps the stored value; a different non-NULL
    value than the stored one is refused. ``changed_at_utc`` must not precede
    the stored ``updated_at_utc``. Same-state re-observations (UNKNOWN ->
    UNKNOWN) are allowed and recorded (see the module docstring).
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
    with _immediate_txn(db_path) as connection:
        row = _load_row_for_update(connection, execution_request_id)
        effective_broker_id = _common_update_checks(
            row, new_state=new_state, changed_at_utc=changed_at_utc, broker_order_id=broker_order_id
        )
        _apply_state_update(
            connection,
            row,
            new_state=new_state,
            reason=reason,
            changed_at_utc=changed_at_utc,
            effective_broker_id=effective_broker_id,
            result_json=None,
        )
    return _require_get(execution_request_id, db_path=db_path)


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

    - the snapshot must belong to this order (``client_order_id`` and every
      identity/request field must match the stored intent),
    - the DB-stored previous snapshot -- not any in-memory copy -- is ALWAYS
      used as the ``old`` side of ``validate_order_result_update``, so known
      fills / quantities / broker IDs / timestamps can never be rolled back
      by a stale caller, even right after a process restart,
    - a caller-supplied ``previous_result`` is accepted only when it equals
      the DB snapshot (guards the caller's own staleness assumptions),
    - the transition is additionally validated against the CURRENT tracked
      state (which may be a state-only UNKNOWN recorded after the snapshot),
    - the snapshot JSON, the current row and the history row commit in ONE
      transaction; a DB-wide ``broker_order_id`` uniqueness conflict rolls
      all of it back (:class:`ExecutionOrderConflictError`).
    """
    if not isinstance(result, OrderResult):
        raise ValueError(f"result must be an OrderResult, got {result!r}")
    if previous_result is not None and not isinstance(previous_result, OrderResult):
        raise ValueError(f"previous_result must be an OrderResult, got {previous_result!r}")
    _require_non_empty_str(reason, "reason")
    db_path = _existing_db_path(db_path, execution_request_id)
    with _immediate_txn(db_path) as connection:
        row = _load_row_for_update(connection, execution_request_id)

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

        db_snapshot = (
            None
            if row["last_order_result_json"] is None
            else _result_from_json(row["last_order_result_json"])
        )
        if previous_result is not None and previous_result != db_snapshot:
            raise ValueError(
                "previous_result does not match the snapshot stored in the DB; refusing to "
                "validate an update against a stale in-memory snapshot"
            )
        if db_snapshot is not None:
            validate_order_result_update(db_snapshot, result)

        # A full snapshot must carry the known broker id itself: silently
        # keeping the stored value would desynchronize row and snapshot.
        if row["broker_order_id"] is not None and result.broker_order_id != row["broker_order_id"]:
            raise ValueError(
                f"broker_order_id is immutable once set: "
                f"{row['broker_order_id']!r} -> {result.broker_order_id!r} refused"
            )
        effective_broker_id = _common_update_checks(
            row,
            new_state=result.state,
            changed_at_utc=result.updated_at_utc,
            broker_order_id=result.broker_order_id,
        )
        _apply_state_update(
            connection,
            row,
            new_state=result.state,
            reason=reason,
            changed_at_utc=result.updated_at_utc,
            effective_broker_id=effective_broker_id,
            result_json=_result_to_json(result),
        )
    return _require_get(execution_request_id, db_path=db_path)


# --- read helpers -----------------------------------------------------------
# A missing DB file returns the empty/None default WITHOUT creating the file
# (dry-run safe). An error on an EXISTING file propagates -- never masked as
# "no data". Every returned order is re-validated (restore-time integrity).


def get_execution_order(execution_request_id: str, *, db_path: Path | None = None) -> dict | None:
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    bundle = _read_bundle(db_path, execution_request_id)
    return None if bundle is None else bundle[0]


def _require_get(execution_request_id: str, *, db_path: Path) -> dict:
    row = get_execution_order(execution_request_id, db_path=db_path)
    if row is None:
        raise ExecutionOrderNotFoundError(f"no execution order with id {execution_request_id!r}")
    return row


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
    e.g. before the first broker response or after only state-only records)."""
    row = get_execution_order(execution_request_id, db_path=db_path)
    if row is None or row["last_order_result_json"] is None:
        return None
    return _result_from_json(row["last_order_result_json"])


def get_state_history(execution_request_id: str, *, db_path: Path | None = None) -> list[dict]:
    """All state-history rows for one order, oldest first (validated)."""
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    bundle = _read_bundle(db_path, execution_request_id)
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
    with closing(_connect(db_path)) as connection:
        ids = [
            row["execution_request_id"]
            for row in connection.execute(
                f"SELECT execution_request_id FROM {ORDERS_TABLE} WHERE order_state = ? "
                "ORDER BY updated_at_utc ASC",
                (OrderState.UNKNOWN.value,),
            ).fetchall()
        ]
    rows = []
    for request_id in ids:
        bundle = _read_bundle(db_path, request_id)
        if bundle is not None:
            rows.append(bundle[0])
    return rows
