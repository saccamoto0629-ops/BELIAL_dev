"""SQLite persistence for Shadow Mode verification results (Issue #78).

*** AUDIT STORE ONLY. No broker calls, no network I/O. A shadow row records
what WOULD have been sent to a broker and how the Safety Gate decided; the
schema deliberately carries NO broker-response vocabulary -- no order state
(ACCEPTED / FILLED / PARTIALLY_FILLED / CANCELLED), no broker reference id, no
fill price / fill quantity. Shadow mode never fabricates broker outcomes. ***

One DB file (``execution/data/shadow_results.sqlite3`` by default,
``SHADOW_RESULT_DB_PATH`` to override), owned by ``execution/`` alone. It is a
SEPARATE file from ``execution_orders.sqlite3`` and
``safety_gate_decisions.sqlite3`` (both verify their DBs as closed worlds), and
a SEPARATE idempotency namespace (Issue #78): shadow rows never enter the
execution order store, so a shadow run can never make a later fake / demo /
live submission of the same signal look "already submitted".

Idempotency contract: ``execution_request_id``, ``signal_id`` and
``client_order_id`` are each UNIQUE across the table -- one shadow record per
execution request / per source signal, decided by the DB constraints inside a
``BEGIN IMMEDIATE`` transaction (never by a pre-SELECT). A losing concurrent
writer gets :class:`ShadowResultConflictError` ONLY when an existing row
provably owns one of the keys; CHECK / NOT NULL violations and unknown
constraint failures stay store failures (:class:`ShadowStoreError`) -- a lock
or persistence error is never misreported as successful duplicate suppression.
The whole record is persisted in ONE atomic INSERT: a failed save leaves no
partial row and no dangling idempotency claim. Re-evaluation of the same
request under a changed policy is deliberately NOT modelled (out of scope).

Schema policy (same stance as ``execution_store`` / ``safety_store``):
``init_db()`` creates schema version 1 only into a PRISTINE database (zero
``sqlite_master`` objects and ``user_version = 0``) inside one transaction;
anything else must pass full closed-world verification (complete
``sqlite_master`` object set, token-normalized DDL, PRAGMA structural
signature, schema version) or fails with :class:`ShadowSchemaError` WITHOUT
modifying the file. Every read and write verifies the schema first; reads
never create a missing DB file (dry-run safe) and re-validate every stored row
strictly (exact JSON key sets, known rejection codes, UTC timestamps,
column/JSON cross-consistency) -- an out-of-band edited DB raises
:class:`ShadowIntegrityError` instead of being trusted. Connections are always
explicitly closed (Windows file-handle hygiene, Issue #83 r2).
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from broker.broker_models import OrderSide, OrderType
from execution.execution_models import ExecutionRequest
from execution.execution_store import _ddl_tokens  # shared DDL token normalizer (PR #91 r3)
from execution.safety_gate import (
    ExecutionMode,
    MarketSafetySnapshot,
    SafetyDecision,
    SafetyRejection,
    SafetyRejectionCode,
)
from execution.shadow_models import ShadowIntendedOrder, ShadowResult

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "shadow_results.sqlite3"
BUSY_TIMEOUT_SECONDS = 30

RESULTS_TABLE = "shadow_results"
SCHEMA_VERSION = 1

_RESULT_COLUMNS: tuple[str, ...] = (
    "execution_request_id",
    "signal_id",
    "strategy_id",
    "symbol",
    "timeframe",
    "side",
    "order_type",
    "quantity",
    "requested_price",
    "stop_loss_price",
    "take_profit_price",
    "requested_at_utc",
    "client_order_id",
    "execution_mode",
    "request_json",
    "intended_order_json",
    "allowed",
    "rejections_json",
    "policy_id",
    "policy_json",
    "checked_at_utc",
    "connection_target",
    "market_json",
    "market_observed_at_utc",
    "spread",
    "account_id",
    "position_count",
    "safety_decision_id",
    "created_at_utc",
)

# Strict JSON shapes: each object must hold exactly these keys on restore.
_REJECTION_JSON_KEYS = frozenset({"code", "message"})
_REQUEST_JSON_KEYS = frozenset(
    {
        "execution_request_id",
        "signal_id",
        "strategy_id",
        "symbol",
        "side",
        "order_type",
        "quantity",
        "requested_at_utc",
        "requested_price",
        "stop_loss_price",
        "take_profit_price",
    }
)
_INTENDED_ORDER_JSON_KEYS = frozenset(
    {"client_order_id", "symbol", "side", "order_type", "quantity", "limit_price"}
)
_MARKET_JSON_KEYS = frozenset(
    {"symbol", "reference_price", "bid", "ask", "observed_at_utc", "market_open", "tradable"}
)


class ShadowStoreError(RuntimeError):
    """Base class for shadow store errors."""


class ShadowSchemaError(ShadowStoreError):
    """The on-disk schema does not match the expected Issue #78 schema."""


class ShadowIntegrityError(ShadowStoreError):
    """Stored data fails restore-time validation (out-of-band corruption)."""


class ShadowResultConflictError(ShadowStoreError):
    """A shadow result for this request / signal / client order id already
    exists (normal duplicate suppression, decided by the UNIQUE constraints)."""

    def __init__(self, message: str, *, existing_execution_request_id: str):
        super().__init__(message)
        self.existing_execution_request_id = existing_execution_request_id


def shadow_db_path() -> Path:
    raw = (os.getenv("SHADOW_RESULT_DB_PATH") or str(DEFAULT_DB_PATH)).strip()
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_utc(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError(f"stored timestamp is not UTC: {text!r}")
    return value


def _create_results_table_sql() -> str:
    """Full current schema. The CHECKs mirror the ``ShadowResult`` model
    invariants as a second guard: allow/reject consistency, the shadow-only
    execution mode, LIMIT <-> requested_price coupling, UTC suffixes -- and
    the broker-response vocabulary is structurally absent."""
    return f"""
        CREATE TABLE {RESULTS_TABLE} (
            shadow_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_request_id TEXT NOT NULL UNIQUE CHECK (trim(execution_request_id) <> ''),
            signal_id TEXT NOT NULL UNIQUE CHECK (trim(signal_id) <> ''),
            strategy_id TEXT NOT NULL CHECK (trim(strategy_id) <> ''),
            symbol TEXT NOT NULL CHECK (trim(symbol) <> ''),
            timeframe TEXT CHECK (timeframe IS NULL OR trim(timeframe) <> ''),
            side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
            order_type TEXT NOT NULL CHECK (order_type IN ('market', 'limit')),
            quantity TEXT NOT NULL CHECK (trim(quantity) <> ''),
            requested_price TEXT CHECK (requested_price IS NULL OR trim(requested_price) <> ''),
            stop_loss_price TEXT CHECK (stop_loss_price IS NULL OR trim(stop_loss_price) <> ''),
            take_profit_price TEXT CHECK (take_profit_price IS NULL OR trim(take_profit_price) <> ''),
            requested_at_utc TEXT NOT NULL CHECK (requested_at_utc LIKE '%+00:00'),
            client_order_id TEXT NOT NULL UNIQUE CHECK (trim(client_order_id) <> ''),
            execution_mode TEXT NOT NULL CHECK (execution_mode = 'shadow'),
            request_json TEXT NOT NULL CHECK (trim(request_json) <> ''),
            intended_order_json TEXT NOT NULL CHECK (trim(intended_order_json) <> ''),
            allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
            rejections_json TEXT NOT NULL CHECK (trim(rejections_json) <> ''),
            policy_id TEXT NOT NULL CHECK (trim(policy_id) <> ''),
            policy_json TEXT NOT NULL CHECK (trim(policy_json) <> ''),
            checked_at_utc TEXT NOT NULL CHECK (checked_at_utc LIKE '%+00:00'),
            connection_target TEXT NOT NULL CHECK (trim(connection_target) <> ''),
            market_json TEXT NOT NULL CHECK (trim(market_json) <> ''),
            market_observed_at_utc TEXT NOT NULL CHECK (market_observed_at_utc LIKE '%+00:00'),
            spread TEXT CHECK (spread IS NULL OR trim(spread) <> ''),
            account_id TEXT CHECK (account_id IS NULL OR trim(account_id) <> ''),
            position_count INTEGER CHECK (position_count IS NULL OR position_count >= 0),
            safety_decision_id INTEGER CHECK (safety_decision_id IS NULL OR safety_decision_id >= 1),
            created_at_utc TEXT NOT NULL CHECK (created_at_utc LIKE '%+00:00'),
            CHECK ((allowed = 1) = (rejections_json = '[]')),
            CHECK ((order_type = 'limit') = (requested_price IS NOT NULL))
        )
    """


_RESULTS_DECISION_INDEX = f"idx_{RESULTS_TABLE}_decision"


def _create_index_sql() -> str:
    return (
        f"CREATE INDEX {_RESULTS_DECISION_INDEX} ON {RESULTS_TABLE} "
        "(allowed, shadow_result_id)"
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_create_results_table_sql())
    connection.execute(_create_index_sql())
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _structure_signature(connection: sqlite3.Connection) -> dict:
    signature: dict = {
        f"table_info:{RESULTS_TABLE}": [
            tuple(row) for row in connection.execute(f"PRAGMA table_info({RESULTS_TABLE})")
        ],
        f"foreign_keys:{RESULTS_TABLE}": [
            tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({RESULTS_TABLE})")
        ],
    }
    index_entries = []
    for index_row in connection.execute(f"PRAGMA index_list({RESULTS_TABLE})"):
        columns = [
            tuple(info) for info in connection.execute(f"PRAGMA index_info({index_row[1]})")
        ]
        index_entries.append((index_row[1], index_row[2], index_row[3], index_row[4], columns))
    signature[f"indexes:{RESULTS_TABLE}"] = sorted(index_entries)
    return signature


_REFERENCE_SCHEMA_CACHE: tuple[dict, dict] | None = None


def _reference_schema() -> tuple[dict, dict]:
    """Pristine reference schema built once in memory: the complete
    ``sqlite_master`` object map (``sqlite_sequence`` from AUTOINCREMENT and
    the UNIQUE autoindexes included) plus the PRAGMA structural signature."""
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
        raise ShadowSchemaError(
            f"shadow result DB at {db_path} failed schema verification; "
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
    """Read flow: open -> BEGIN (consistent view) -> verify schema -> restore.
    Always rolled back (reads write nothing)."""
    with closing(_connect(db_path)) as connection:
        connection.execute("BEGIN")
        try:
            _verify_schema(connection, db_path)
            yield connection
        finally:
            connection.execute("ROLLBACK")


def init_db(db_path: Path | None = None) -> None:
    """Create the schema (pristine DB only) or verify an existing one.

    Same pristine rule as ``execution_store.init_db`` / the safety store:
    creation happens only when ``sqlite_master`` is empty AND ``user_version``
    is 0 -- inside one ``BEGIN IMMEDIATE`` transaction, rolled back entirely
    on any failure; anything else must pass full verification or fails
    WITHOUT modifying the file. Idempotent for a valid DB.
    """
    path = Path(db_path) if db_path is not None else shadow_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            objects = connection.execute("SELECT name FROM sqlite_master").fetchall()
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if not objects:
                if version != 0:
                    raise ShadowSchemaError(
                        f"DB at {path} is empty but carries user_version {version}; "
                        "not a pristine DB -- refusing to initialize it"
                    )
                _create_schema(connection)
                _verify_schema(connection, path)
            else:
                _verify_schema(connection, path)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise


def verify_db(db_path: Path | None = None) -> None:
    """Read-only schema probe (dry-run contract, Issue #78): verify an
    EXISTING file against the closed-world schema without creating or
    modifying anything. A missing file raises :class:`ShadowStoreError` --
    callers decide whether "missing" is acceptable (dry-run treats a missing
    DB as "nothing to probe" and must not call this)."""
    path = Path(db_path) if db_path is not None else shadow_db_path()
    if not path.exists():
        raise ShadowStoreError(f"shadow result DB at {path} does not exist")
    with _verified_read(path):
        pass


# --- serialization ------------------------------------------------------------


def _decimal_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _request_to_json(request: ExecutionRequest) -> str:
    return json.dumps(
        {
            "execution_request_id": request.execution_request_id,
            "signal_id": request.signal_id,
            "strategy_id": request.strategy_id,
            "symbol": request.symbol,
            "side": request.side.value,
            "order_type": request.order_type.value,
            "quantity": str(request.quantity),
            "requested_at_utc": _iso(request.requested_at_utc),
            "requested_price": _decimal_str(request.requested_price),
            "stop_loss_price": _decimal_str(request.stop_loss_price),
            "take_profit_price": _decimal_str(request.take_profit_price),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _request_from_json(text: str) -> ExecutionRequest:
    data = _strict_object(text, _REQUEST_JSON_KEYS, "request_json")
    return ExecutionRequest(
        execution_request_id=data["execution_request_id"],
        signal_id=data["signal_id"],
        strategy_id=data["strategy_id"],
        symbol=data["symbol"],
        side=OrderSide(data["side"]),
        order_type=OrderType(data["order_type"]),
        quantity=_strict_decimal(data["quantity"], "request_json.quantity"),
        requested_at_utc=_parse_utc(data["requested_at_utc"]),
        requested_price=_optional_decimal(data["requested_price"], "request_json.requested_price"),
        stop_loss_price=_optional_decimal(data["stop_loss_price"], "request_json.stop_loss_price"),
        take_profit_price=_optional_decimal(
            data["take_profit_price"], "request_json.take_profit_price"
        ),
    )


def _intended_order_to_json(order: ShadowIntendedOrder) -> str:
    return json.dumps(
        {
            "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "quantity": str(order.quantity),
            "limit_price": _decimal_str(order.limit_price),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _intended_order_from_json(text: str) -> ShadowIntendedOrder:
    data = _strict_object(text, _INTENDED_ORDER_JSON_KEYS, "intended_order_json")
    return ShadowIntendedOrder(
        client_order_id=data["client_order_id"],
        symbol=data["symbol"],
        side=OrderSide(data["side"]),
        order_type=OrderType(data["order_type"]),
        quantity=_strict_decimal(data["quantity"], "intended_order_json.quantity"),
        limit_price=_optional_decimal(data["limit_price"], "intended_order_json.limit_price"),
    )


def _market_to_json(market: MarketSafetySnapshot) -> str:
    return json.dumps(
        {
            "symbol": market.symbol,
            "reference_price": _decimal_str(market.reference_price),
            "bid": _decimal_str(market.bid),
            "ask": _decimal_str(market.ask),
            "observed_at_utc": _iso(market.observed_at_utc),
            "market_open": market.market_open,
            "tradable": market.tradable,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _market_from_json(text: str) -> MarketSafetySnapshot:
    data = _strict_object(text, _MARKET_JSON_KEYS, "market_json")
    return MarketSafetySnapshot(
        symbol=data["symbol"],
        reference_price=_optional_decimal(data["reference_price"], "market_json.reference_price"),
        bid=_optional_decimal(data["bid"], "market_json.bid"),
        ask=_optional_decimal(data["ask"], "market_json.ask"),
        observed_at_utc=_parse_utc(data["observed_at_utc"]),
        market_open=_strict_bool(data["market_open"], "market_json.market_open"),
        tradable=_strict_bool(data["tradable"], "market_json.tradable"),
    )


def _rejections_to_json(rejections: tuple[SafetyRejection, ...]) -> str:
    return json.dumps(
        [{"code": item.code.value, "message": item.message} for item in rejections],
        separators=(",", ":"),
    )


def _rejections_from_json(text: str) -> tuple[SafetyRejection, ...]:
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


def _strict_object(text: str, expected_keys: frozenset[str], name: str) -> dict:
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) != expected_keys:
        raise ValueError(
            f"{name} keys {sorted(data) if isinstance(data, dict) else type(data).__name__} "
            f"do not match the expected set {sorted(expected_keys)}"
        )
    return data


def _strict_decimal(value, name: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a decimal string, got {value!r}")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{name} is not a valid decimal string: {value!r}") from None


def _optional_decimal(value, name: str) -> Decimal | None:
    return None if value is None else _strict_decimal(value, name)


def _strict_bool(value, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean, got {value!r}")
    return value


# --- write --------------------------------------------------------------------


def _classify_conflicts(connection: sqlite3.Connection, record: dict) -> list[tuple[str, str, dict]]:
    """Return every expected UNIQUE collision for a failed INSERT (mirrors
    ``execution_store._classify_create_conflicts``): an ``IntegrityError`` is
    a duplicate ONLY when an existing row provably owns one of the identity
    keys -- CHECK / NOT NULL and unknown constraints stay failures."""
    conflicts: list[tuple[str, str, dict]] = []
    for column, reason in (
        ("execution_request_id", "a shadow result for this execution request already exists"),
        ("signal_id", "this source signal already produced a shadow result"),
        ("client_order_id", "this client_order_id is already bound to a shadow result"),
    ):
        row = connection.execute(
            f"SELECT execution_request_id, signal_id, allowed FROM {RESULTS_TABLE} "
            f"WHERE {column} = ?",
            (record[column],),
        ).fetchone()
        if row is not None:
            conflicts.append((column, reason, dict(row)))
    return conflicts


def record_shadow_result(result: ShadowResult, *, db_path: Path | None = None) -> int:
    """Persist one shadow result in ONE atomic INSERT; returns the assigned
    ``shadow_result_id``.

    A missing DB file is a failure, not a trigger for implicit creation (the
    shadow store must be initialized deliberately via :func:`init_db`).
    Duplicates -- an existing row owning the same ``execution_request_id``,
    ``signal_id`` or ``client_order_id`` -- raise
    :class:`ShadowResultConflictError`; every other failure raises
    :class:`ShadowStoreError` subclasses and leaves the DB unchanged (the
    INSERT is the only write, inside the verified transaction).
    """
    if not isinstance(result, ShadowResult):
        raise ShadowStoreError(f"result must be a ShadowResult, got {result!r}")
    path = Path(db_path) if db_path is not None else shadow_db_path()
    if not path.exists():
        raise ShadowStoreError(
            f"shadow result DB at {path} does not exist; call init_db() first"
        )
    request = result.request
    decision = result.decision
    record = {
        "execution_request_id": request.execution_request_id,
        "signal_id": request.signal_id,
        "strategy_id": request.strategy_id,
        "symbol": request.symbol,
        "timeframe": result.timeframe,
        "side": request.side.value,
        "order_type": request.order_type.value,
        "quantity": str(request.quantity),
        "requested_price": _decimal_str(request.requested_price),
        "stop_loss_price": _decimal_str(request.stop_loss_price),
        "take_profit_price": _decimal_str(request.take_profit_price),
        "requested_at_utc": _iso(request.requested_at_utc),
        "client_order_id": result.client_order_id,
        "execution_mode": decision.execution_mode,
        "request_json": _request_to_json(request),
        "intended_order_json": _intended_order_to_json(result.intended_order),
        "allowed": int(decision.allowed),
        "rejections_json": _rejections_to_json(decision.rejections),
        "policy_id": decision.policy_id,
        "policy_json": json.dumps(result.policy_snapshot, sort_keys=True, separators=(",", ":")),
        "checked_at_utc": _iso(decision.checked_at_utc),
        "connection_target": decision.connection_target,
        "market_json": _market_to_json(result.market),
        "market_observed_at_utc": _iso(result.market.observed_at_utc),
        "spread": _decimal_str(result.market.spread),
        "account_id": decision.account_id,
        "position_count": decision.position_count,
        "safety_decision_id": result.safety_decision_id,
        "created_at_utc": _iso(result.created_at_utc),
    }
    insert_sql = (
        f"INSERT INTO {RESULTS_TABLE} ({', '.join(_RESULT_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _RESULT_COLUMNS)})"
    )
    try:
        with _verified_txn(path) as connection:
            try:
                cursor = connection.execute(
                    insert_sql, tuple(record[col] for col in _RESULT_COLUMNS)
                )
            except sqlite3.IntegrityError as exc:
                conflicts = _classify_conflicts(connection, record)
                if not conflicts:
                    # CHECK / NOT NULL or an unknown future constraint is a
                    # persistence failure, not duplicate suppression.
                    raise ShadowStoreError(
                        f"shadow result INSERT failed with a non-duplicate "
                        f"integrity error: {exc}"
                    ) from exc
                reason = "; ".join(item[1] for item in conflicts)
                raise ShadowResultConflictError(
                    f"shadow result for {request.execution_request_id!r} (signal_id "
                    f"{request.signal_id!r} / client_order_id {result.client_order_id!r}) "
                    f"collides with an existing row ({reason}): {exc}",
                    existing_execution_request_id=conflicts[0][2]["execution_request_id"],
                ) from exc
            return int(cursor.lastrowid)
    except sqlite3.Error as exc:
        raise ShadowStoreError(f"shadow result persistence failed: {exc}") from exc


# --- read ---------------------------------------------------------------------


def _restore_result(row: sqlite3.Row) -> ShadowResult:
    """Rebuild one ShadowResult STRICTLY: JSON key sets must match exactly,
    the searchable columns must agree with the JSON payloads, enums / UTC /
    allow-reject consistency are re-validated by the model constructors."""
    try:
        request = _request_from_json(row["request_json"])
        intended = _intended_order_from_json(row["intended_order_json"])
        market = _market_from_json(row["market_json"])
        rejections = _rejections_from_json(row["rejections_json"])
        if row["allowed"] not in (0, 1):
            raise ValueError(f"allowed must be 0 or 1, got {row['allowed']!r}")
        policy_snapshot = json.loads(row["policy_json"])
        if not isinstance(policy_snapshot, dict):
            raise ValueError("policy_json must hold a JSON object")
        column_view = {
            "execution_request_id": request.execution_request_id,
            "signal_id": request.signal_id,
            "strategy_id": request.strategy_id,
            "symbol": request.symbol,
            "side": request.side.value,
            "order_type": request.order_type.value,
            "quantity": str(request.quantity),
            "requested_price": _decimal_str(request.requested_price),
            "stop_loss_price": _decimal_str(request.stop_loss_price),
            "take_profit_price": _decimal_str(request.take_profit_price),
            "requested_at_utc": _iso(request.requested_at_utc),
            "client_order_id": intended.client_order_id,
            "market_observed_at_utc": _iso(market.observed_at_utc),
            "spread": _decimal_str(market.spread),
        }
        for column, expected in column_view.items():
            if row[column] != expected:
                raise ValueError(
                    f"column {column!r} ({row[column]!r}) does not match the stored "
                    f"JSON payload ({expected!r})"
                )
        if row["execution_mode"] != ExecutionMode.SHADOW.value:
            raise ValueError(f"execution_mode must be 'shadow', got {row['execution_mode']!r}")
        decision = SafetyDecision(
            execution_request_id=request.execution_request_id,
            allowed=bool(row["allowed"]),
            checked_at_utc=_parse_utc(row["checked_at_utc"]),
            policy_id=row["policy_id"],
            rejections=rejections,
            execution_mode=row["execution_mode"],
            connection_target=row["connection_target"],
            account_id=row["account_id"],
            position_count=row["position_count"],
        )
        return ShadowResult(
            request=request,
            client_order_id=row["client_order_id"],
            timeframe=row["timeframe"],
            decision=decision,
            intended_order=intended,
            market=market,
            policy_snapshot=policy_snapshot,
            safety_decision_id=row["safety_decision_id"],
            created_at_utc=_parse_utc(row["created_at_utc"]),
            shadow_result_id=row["shadow_result_id"],
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise ShadowIntegrityError(
            f"shadow result row {row['shadow_result_id']!r} failed restore-time "
            f"validation: {exc}"
        ) from exc


_SELECT_COLUMNS = "shadow_result_id, " + ", ".join(_RESULT_COLUMNS)


def _read_rows(db_path: Path | None, where: str, params: tuple) -> tuple[ShadowResult, ...]:
    path = Path(db_path) if db_path is not None else shadow_db_path()
    if not path.exists():
        return ()
    with _verified_read(path) as connection:
        rows = connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM {RESULTS_TABLE} {where} "
            "ORDER BY shadow_result_id ASC",
            params,
        ).fetchall()
    return tuple(_restore_result(row) for row in rows)


def get_shadow_result(
    execution_request_id: str, *, db_path: Path | None = None
) -> ShadowResult | None:
    """The shadow result for one execution request (at most one row by
    UNIQUE). Missing DB -> None, never created (dry-run safe)."""
    _require_key(execution_request_id, "execution_request_id")
    results = _read_rows(db_path, "WHERE execution_request_id = ?", (execution_request_id,))
    return results[0] if results else None


def get_shadow_result_by_signal_id(
    signal_id: str, *, db_path: Path | None = None
) -> ShadowResult | None:
    """Reverse lookup from the source signal (at most one row by UNIQUE).
    Missing DB -> None, never created."""
    _require_key(signal_id, "signal_id")
    results = _read_rows(db_path, "WHERE signal_id = ?", (signal_id,))
    return results[0] if results else None


def list_shadow_results(
    *, allowed: bool | None = None, db_path: Path | None = None
) -> tuple[ShadowResult, ...]:
    """All shadow results in recording order, optionally filtered by the gate
    decision (``allowed=False`` -> rejected only). Missing DB -> empty tuple,
    never created."""
    if allowed is None:
        return _read_rows(db_path, "", ())
    if not isinstance(allowed, bool):
        raise ShadowStoreError(f"allowed must be a bool or None, got {allowed!r}")
    return _read_rows(db_path, "WHERE allowed = ?", (int(allowed),))


def _require_key(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ShadowStoreError(f"{field_name} must be a non-empty string, got {value!r}")
