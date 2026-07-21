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

UNKNOWN handling (Issue #74): ``unknown`` is stored as-is and keeps the phase
at ``submitting`` -- the broker may or may not hold the order. UNKNOWN rows
are query targets (:func:`list_unknown_orders`); they are resolved ONLY by
recording a later observation through the normal validated update path. This
store never converts UNKNOWN into "not placed" / "rejected" / "retryable",
and re-submission is not modelled at all: ``client_order_id`` is bound 1:1 to
the request and immutable (see ``execution_models`` for the ID design).

Update contract (every state update, one ``BEGIN IMMEDIATE`` transaction):

- transition validated by ``validate_order_state_transition`` (and, when the
  caller has both snapshots, ``validate_order_result_update`` via
  :func:`record_order_result`),
- current-row update and history append commit atomically (both or neither),
- ``broker_order_id`` may go NULL -> value once and is then immutable
  (an observation that omits it keeps the stored value; a DIFFERENT value is
  refused),
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

Schema management: :func:`init_db` is idempotent. This is a brand-new DB
(no legacy data exists anywhere), so there is no legacy-migration path: an
existing table that does not match the expected schema raises
:class:`ExecutionSchemaError` loudly instead of being silently rebuilt or
recreated. Read helpers never create the DB file (dry-run safe) and never
swallow read errors on an existing file.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from broker.broker_models import (
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
# OrderResult construction invariant).
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


class ExecutionStoreError(RuntimeError):
    """Base class for execution store errors."""


class ExecutionSchemaError(ExecutionStoreError):
    """An existing table does not match the expected Issue #74 schema."""


class ExecutionOrderNotFoundError(ExecutionStoreError):
    """No execution order exists for the given execution_request_id."""


class ExecutionOrderConflictError(ExecutionStoreError):
    """An insert collided with an existing execution_request_id / client_order_id."""


def execution_db_path() -> Path:
    raw = (os.getenv("EXECUTION_ORDER_DB_PATH") or str(DEFAULT_DB_PATH)).strip()
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _state_check_sql() -> str:
    states = "', '".join(sorted(s.value for s in OrderState))
    return f"order_state IN ('{states}')"


def _phase_check_sql() -> str:
    phases = "', '".join(sorted(KNOWN_PHASES))
    return f"submission_phase IN ('{phases}')"


def _create_orders_table_sql(table_name: str) -> str:
    """Full current schema. Decimals are stored as canonical TEXT (never REAL:
    live-order arithmetic must not pick up float error, Issue #73/#74)."""
    sides = "', '".join(sorted(s.value for s in OrderSide))
    types = "', '".join(sorted(t.value for t in OrderType))
    return f"""
        CREATE TABLE {table_name} (
            execution_request_id TEXT PRIMARY KEY,
            signal_id TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL CHECK (side IN ('{sides}')),
            order_type TEXT NOT NULL CHECK (order_type IN ('{types}')),
            quantity TEXT NOT NULL,
            requested_price TEXT,
            stop_loss_price TEXT,
            take_profit_price TEXT,
            requested_at_utc TEXT NOT NULL,
            client_order_id TEXT NOT NULL UNIQUE,
            submission_phase TEXT NOT NULL CHECK ({_phase_check_sql()}),
            order_state TEXT CHECK ({_state_check_sql()}),
            broker_order_id TEXT,
            last_state_reason TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            CHECK ((submission_phase = '{PHASE_CREATED}') = (order_state IS NULL))
        )
    """


def _create_history_table_sql(table_name: str) -> str:
    states = "', '".join(sorted(s.value for s in OrderState))
    return f"""
        CREATE TABLE {table_name} (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_request_id TEXT NOT NULL
                REFERENCES {ORDERS_TABLE} (execution_request_id),
            previous_state TEXT CHECK (previous_state IS NULL OR previous_state IN ('{states}')),
            new_state TEXT NOT NULL CHECK (new_state IN ('{states}')),
            reason TEXT NOT NULL,
            broker_order_id TEXT,
            changed_at_utc TEXT NOT NULL
        )
    """


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


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()}


def init_db(db_path: Path | None = None) -> None:
    """Create the Issue #74 schema; idempotent and safe to call repeatedly.

    Brand-new component, brand-new DB: there is no legacy data to migrate, so
    a pre-existing table with missing columns is a hard
    :class:`ExecutionSchemaError` (fail loudly; never silently rebuild or
    recreate someone's data).
    """
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _immediate_txn(db_path) as connection:
        for table, create_sql, required in (
            (ORDERS_TABLE, _create_orders_table_sql(ORDERS_TABLE), REQUIRED_ORDER_COLUMNS),
            (HISTORY_TABLE, _create_history_table_sql(HISTORY_TABLE), REQUIRED_HISTORY_COLUMNS),
        ):
            if not _table_exists(connection, table):
                connection.execute(create_sql)
            else:
                missing = required - _table_columns(connection, table)
                if missing:
                    raise ExecutionSchemaError(
                        f"existing table {table!r} at {db_path} is missing columns "
                        f"{sorted(missing)}; refusing to touch it"
                    )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{ORDERS_TABLE}_state "
            f"ON {ORDERS_TABLE} (order_state, updated_at_utc)"
        )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{HISTORY_TABLE}_request "
            f"ON {HISTORY_TABLE} (execution_request_id, history_id)"
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


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


def _load_row_for_update(connection: sqlite3.Connection, execution_request_id: str) -> sqlite3.Row:
    row = connection.execute(
        f"SELECT * FROM {ORDERS_TABLE} WHERE execution_request_id = ?", (execution_request_id,)
    ).fetchone()
    if row is None:
        raise ExecutionOrderNotFoundError(f"no execution order with id {execution_request_id!r}")
    return row


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
    NULL) commit in one transaction.
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


def record_order_state(
    execution_request_id: str,
    new_state: OrderState,
    *,
    reason: str,
    changed_at_utc: datetime,
    broker_order_id: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """Record one broker-side observation for a submitted order.

    Validates the transition with the existing broker
    ``validate_order_state_transition`` (the single source of truth; terminal
    states can never be left, UNKNOWN resolves only to its allowed targets),
    then commits the current-row update and the history append atomically.

    ``broker_order_id=None`` means "not part of this observation" and keeps
    the stored value; a different non-NULL value than the stored one is
    refused. States that imply a broker acknowledgement (accepted /
    partially_filled / filled) require a broker_order_id to be known by the
    end of the update. ``changed_at_utc`` must not precede the stored
    ``updated_at_utc``. Same-state re-observations are allowed and recorded
    (see the module docstring).
    """
    if not isinstance(new_state, OrderState):
        raise ValueError(f"new_state must be an OrderState, got {new_state!r}")
    _require_non_empty_str(reason, "reason")
    _require_utc(changed_at_utc, "changed_at_utc")
    if broker_order_id is not None:
        _require_non_empty_str(broker_order_id, "broker_order_id")
    db_path = _existing_db_path(db_path, execution_request_id)
    with _immediate_txn(db_path) as connection:
        row = _load_row_for_update(connection, execution_request_id)
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

        phase = row["submission_phase"]
        if new_state in _CONFIRMED_STATES:
            phase = PHASE_SUBMITTED
        connection.execute(
            f"UPDATE {ORDERS_TABLE} SET submission_phase = ?, order_state = ?, "
            "broker_order_id = ?, last_state_reason = ?, updated_at_utc = ? "
            "WHERE execution_request_id = ?",
            (
                phase,
                new_state.value,
                effective_broker_id,
                reason,
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
    return _require_get(execution_request_id, db_path=db_path)


def record_order_result(
    execution_request_id: str,
    result: OrderResult,
    *,
    reason: str,
    previous_result: OrderResult | None = None,
    db_path: Path | None = None,
) -> dict:
    """Record a full broker ``OrderResult`` snapshot's state for this order.

    Convenience wrapper over :func:`record_order_state` deriving state /
    broker_order_id / timestamp from the snapshot. The snapshot must belong to
    this order (``result.client_order_id`` must equal the stored
    ``client_order_id``). When the caller holds the PREVIOUS snapshot, pass it
    as ``previous_result`` so the full content-level update contract
    (``validate_order_result_update``, PR #87) is enforced too -- the store
    itself persists state-level data only, so content validation across
    restarts requires the caller to supply both snapshots.
    """
    if not isinstance(result, OrderResult):
        raise ValueError(f"result must be an OrderResult, got {result!r}")
    if previous_result is not None:
        validate_order_result_update(previous_result, result)
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    row = get_execution_order(execution_request_id, db_path=db_path)
    if row is None:
        raise ExecutionOrderNotFoundError(f"no execution order with id {execution_request_id!r}")
    if result.client_order_id != row["client_order_id"]:
        raise ValueError(
            f"result.client_order_id {result.client_order_id!r} does not belong to execution "
            f"order {execution_request_id!r} (client_order_id {row['client_order_id']!r})"
        )
    return record_order_state(
        execution_request_id,
        result.state,
        reason=reason,
        changed_at_utc=result.updated_at_utc,
        broker_order_id=result.broker_order_id,
        db_path=db_path,
    )


# --- read helpers -----------------------------------------------------------
# A missing DB file returns the empty/None default WITHOUT creating the file
# (dry-run safe). An error on an EXISTING file propagates -- never masked as
# "no data".


def _read_one(db_path: Path, sql: str, params: tuple) -> dict | None:
    if not db_path.exists():
        return None
    with closing(_connect(db_path)) as connection:
        row = connection.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def _read_many(db_path: Path, sql: str, params: tuple = ()) -> list[dict]:
    if not db_path.exists():
        return []
    with closing(_connect(db_path)) as connection:
        rows = connection.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def get_execution_order(execution_request_id: str, *, db_path: Path | None = None) -> dict | None:
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    return _read_one(
        db_path,
        f"SELECT * FROM {ORDERS_TABLE} WHERE execution_request_id = ?",
        (execution_request_id,),
    )


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


def get_state_history(execution_request_id: str, *, db_path: Path | None = None) -> list[dict]:
    """All state-history rows for one order, oldest first."""
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    return _read_many(
        db_path,
        f"SELECT * FROM {HISTORY_TABLE} WHERE execution_request_id = ? ORDER BY history_id ASC",
        (execution_request_id,),
    )


def list_unknown_orders(*, db_path: Path | None = None) -> list[dict]:
    """Orders whose broker-side outcome is unconfirmed -- the query targets.

    Exactly ``order_state = 'unknown'``: these must be resolved by a later
    broker query, and are never auto-classified as "not placed" / "rejected" /
    "retryable" by this store. (Running such queries -- reconciliation -- is
    out of scope for Issue #74.)
    """
    db_path = Path(db_path) if db_path is not None else execution_db_path()
    return _read_many(
        db_path,
        f"SELECT * FROM {ORDERS_TABLE} WHERE order_state = ? ORDER BY updated_at_utc ASC",
        (OrderState.UNKNOWN.value,),
    )
