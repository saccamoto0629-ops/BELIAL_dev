"""SQLite persistence for Shadow Mode verification records (Issue #78; claim /
provenance rework PR #100).

*** AUDIT STORE ONLY. No broker calls, no network I/O. A shadow row records
what WOULD have been sent to a broker and how the Safety Gate decided; the
schema deliberately carries NO broker-response vocabulary -- no order state
(ACCEPTED / FILLED / PARTIALLY_FILLED / CANCELLED), no broker reference id, no
fill price / fill quantity. Shadow mode never fabricates broker outcomes. ***

One DB file (``execution/data/shadow_results.sqlite3`` by default,
``SHADOW_RESULT_DB_PATH`` to override), owned by ``execution/`` alone; a
SEPARATE file and a SEPARATE idempotency namespace from
``execution_orders.sqlite3`` / ``safety_gate_decisions.sqlite3`` -- shadow
rows never enter the execution order store, so a shadow run can never make a
later fake / demo / live submission of the same signal look "already
submitted".

Claim-first lifecycle (PR #100 review)
--------------------------------------

Each cycle's identity is CLAIMED **before** the Safety Gate runs, so exactly
one process evaluates the gate and exactly one Safety Decision row is written
per cycle -- losers never evaluate and never record decisions:

    claim_shadow_cycle()                phase = 'claimed'
      ├─ acquired  -> evaluate gate -> record decision ->
      │              complete_shadow_cycle()   phase = 'completed'
      ├─ duplicate -> the stored COMPLETED result, or the live claim of the
      │              current holder (payload-equal; nothing is evaluated)
      └─ conflict  -> ShadowIdentityConflictError (payload differs; nothing
                     is evaluated, nothing is stored)

Claim contract: a claim carries ``claim_owner_id``, ``claimed_at_utc`` and
``lease_expires_at_utc``. The SAME owner may always re-acquire its own claim
immediately (in-process retry after a completion failure); any other owner
may take over only once the lease has EXPIRED (stale claim = the previous
holder crashed between claim and completion). Takeover happens inside the
same ``BEGIN IMMEDIATE`` transaction as the conflict check, so two racers
cannot both take a stale claim. A crash after the decision was recorded but
before completion leaves the claim reclaimable and the decision row an
ORPHAN (visible via ``shadow_report --orphan-decisions``; bounded by the
number of retries, and each successful retry ends the growth). No claim can
stay unprocessable forever: it either completes, or its lease expires and the
next payload-equal run recovers it. SQLite lock / persistence errors raise
:class:`ShadowStoreError` and are NEVER classified as duplicate suppression.

Duplicate vs conflict: a key collision (``execution_request_id`` /
``signal_id`` / ``client_order_id`` -- each UNIQUE) is a DUPLICATE only when
the stored row's canonical payload (``shadow_models.canonical_cycle_payload``)
is identical; ANY difference raises :class:`ShadowIdentityConflictError` with
the machine-readable reason code and the differing field names. The whole
record for one phase transition is written in ONE statement inside the
verified transaction: a failed write leaves no partial row.

Schema policy (same stance as ``execution_store`` / ``safety_store``):
``init_db()`` creates schema version 1 only into a PRISTINE database; every
read and write verifies the closed-world schema first. Reads use TRUE
read-only connections (``execution.sqlite_ro``): a missing DB file is never
created, no journal / WAL / SHM side files appear, and write statements fail.
Stored rows are strictly re-validated on restore (exact JSON key sets, known
rejection codes, UTC timestamps, column/JSON cross-consistency, canonical
payload consistency); an out-of-band edited DB raises
:class:`ShadowIntegrityError` instead of being trusted.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from broker.broker_models import OrderSide, OrderType, PositionInfo, PositionSide, AccountState
from execution.execution_models import ExecutionRequest
from execution.execution_store import _ddl_tokens  # shared DDL token normalizer (PR #91 r3)
from execution.safety_gate import (
    AccountSafetySnapshot,
    ExecutionMode,
    MarketSafetySnapshot,
    SafetyDecision,
    SafetyPolicy,
    SafetyRejection,
    SafetyRejectionCode,
)
from execution.shadow_models import (
    KNOWN_INPUT_ORIGINS,
    PHASE_CLAIMED,
    PHASE_COMPLETED,
    ShadowClaim,
    ShadowIntendedOrder,
    ShadowProvenance,
    ShadowResult,
    canonical_cycle_payload,
    canonical_payload_json,
    differing_payload_fields,
    intended_order_from_request,
)
from execution.sqlite_ro import connect_read_only

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "shadow_results.sqlite3"
BUSY_TIMEOUT_SECONDS = 30

RESULTS_TABLE = "shadow_results"
SCHEMA_VERSION = 1

# Claim outcomes returned by claim_shadow_cycle().
CLAIM_ACQUIRED = "acquired"
CLAIM_DUPLICATE = "duplicate"

# Report-listing bound (PR #100 review): --limit must stay within this.
MAX_LIST_LIMIT = 1000

_CLAIM_COLUMNS: tuple[str, ...] = (
    "execution_request_id",
    "signal_id",
    "client_order_id",
    "phase",
    "claim_owner_id",
    "claim_attempt_id",
    "claim_generation",
    "claimed_at_utc",
    "lease_expires_at_utc",
    "canonical_payload_json",
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
    "execution_mode",
    "input_origin",
    "signal_source",
    "signal_observed_at_utc",
    "config_source",
    "config_version",
    "market_source",
    "account_source",
    "request_json",
    "intended_order_json",
)

_COMPLETION_COLUMNS: tuple[str, ...] = (
    "allowed",
    "rejections_json",
    "policy_id",
    "policy_json",
    "checked_at_utc",
    "connection_target",
    "market_json",
    "market_observed_at_utc",
    "spread",
    "account_snapshot_json",
    "account_id",
    "position_count",
    "safety_decision_id",
    "completed_at_utc",
)

_ALL_COLUMNS: tuple[str, ...] = _CLAIM_COLUMNS + _COMPLETION_COLUMNS

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
_ACCOUNT_SNAPSHOT_JSON_KEYS = frozenset({"account", "positions"})
_ACCOUNT_JSON_KEYS = frozenset(
    {"account_id", "currency", "balance", "equity", "margin_available", "retrieved_at_utc"}
)
_POSITION_JSON_KEYS = frozenset(
    {"symbol", "side", "quantity", "average_entry_price", "retrieved_at_utc"}
)


class ShadowStoreError(RuntimeError):
    """Base class for shadow store errors."""


class ShadowSchemaError(ShadowStoreError):
    """The on-disk schema does not match the expected Issue #78 schema."""


class ShadowIntegrityError(ShadowStoreError):
    """Stored data fails restore-time validation (out-of-band corruption)."""


class ShadowIdentityConflictError(ShadowStoreError):
    """An identity key is already claimed by a DIFFERENT canonical payload.

    This is never silently answered with the stored record: a conflicting
    cycle must fail loudly. ``reason_code`` is the machine-readable record;
    ``differing_fields`` names the payload keys that differ."""

    reason_code = "shadow_payload_mismatch"

    def __init__(
        self,
        message: str,
        *,
        existing_execution_request_id: str,
        differing_fields: tuple[str, ...],
    ):
        super().__init__(message)
        self.existing_execution_request_id = existing_execution_request_id
        self.differing_fields = differing_fields


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
    """Full current schema. The CHECKs mirror the model invariants as a
    second guard: phase/completion-column coupling, allow/reject consistency,
    the shadow-only execution mode, LIMIT <-> requested_price coupling, UTC
    suffixes -- and the broker-response vocabulary is structurally absent."""
    origins = ", ".join(f"'{origin}'" for origin in sorted(KNOWN_INPUT_ORIGINS))
    return f"""
        CREATE TABLE {RESULTS_TABLE} (
            shadow_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_request_id TEXT NOT NULL UNIQUE CHECK (trim(execution_request_id) <> ''),
            signal_id TEXT NOT NULL UNIQUE CHECK (trim(signal_id) <> ''),
            client_order_id TEXT NOT NULL UNIQUE CHECK (trim(client_order_id) <> ''),
            phase TEXT NOT NULL CHECK (phase IN ('{PHASE_CLAIMED}', '{PHASE_COMPLETED}')),
            claim_owner_id TEXT NOT NULL CHECK (trim(claim_owner_id) <> ''),
            claim_attempt_id TEXT NOT NULL CHECK (trim(claim_attempt_id) <> ''),
            claim_generation INTEGER NOT NULL CHECK (claim_generation >= 1),
            claimed_at_utc TEXT NOT NULL CHECK (claimed_at_utc LIKE '%+00:00'),
            lease_expires_at_utc TEXT NOT NULL CHECK (lease_expires_at_utc LIKE '%+00:00'),
            canonical_payload_json TEXT NOT NULL CHECK (trim(canonical_payload_json) <> ''),
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
            execution_mode TEXT NOT NULL CHECK (execution_mode = 'shadow'),
            input_origin TEXT NOT NULL CHECK (input_origin IN ({origins})),
            signal_source TEXT NOT NULL CHECK (trim(signal_source) <> ''),
            signal_observed_at_utc TEXT CHECK (
                signal_observed_at_utc IS NULL OR signal_observed_at_utc LIKE '%+00:00'
            ),
            config_source TEXT NOT NULL CHECK (trim(config_source) <> ''),
            config_version TEXT NOT NULL CHECK (trim(config_version) <> ''),
            market_source TEXT NOT NULL CHECK (trim(market_source) <> ''),
            account_source TEXT NOT NULL CHECK (trim(account_source) <> ''),
            request_json TEXT NOT NULL CHECK (trim(request_json) <> ''),
            intended_order_json TEXT NOT NULL CHECK (trim(intended_order_json) <> ''),
            allowed INTEGER CHECK (allowed IS NULL OR allowed IN (0, 1)),
            rejections_json TEXT CHECK (rejections_json IS NULL OR trim(rejections_json) <> ''),
            policy_id TEXT CHECK (policy_id IS NULL OR trim(policy_id) <> ''),
            policy_json TEXT CHECK (policy_json IS NULL OR trim(policy_json) <> ''),
            checked_at_utc TEXT CHECK (checked_at_utc IS NULL OR checked_at_utc LIKE '%+00:00'),
            connection_target TEXT CHECK (connection_target IS NULL OR trim(connection_target) <> ''),
            market_json TEXT CHECK (market_json IS NULL OR trim(market_json) <> ''),
            market_observed_at_utc TEXT CHECK (
                market_observed_at_utc IS NULL OR market_observed_at_utc LIKE '%+00:00'
            ),
            spread TEXT CHECK (spread IS NULL OR trim(spread) <> ''),
            account_snapshot_json TEXT CHECK (
                account_snapshot_json IS NULL OR trim(account_snapshot_json) <> ''
            ),
            account_id TEXT CHECK (account_id IS NULL OR trim(account_id) <> ''),
            position_count INTEGER CHECK (position_count IS NULL OR position_count >= 0),
            safety_decision_id INTEGER CHECK (safety_decision_id IS NULL OR safety_decision_id >= 1),
            completed_at_utc TEXT CHECK (
                completed_at_utc IS NULL OR completed_at_utc LIKE '%+00:00'
            ),
            CHECK ((phase = '{PHASE_COMPLETED}') = (
                allowed IS NOT NULL AND rejections_json IS NOT NULL
                AND policy_id IS NOT NULL AND policy_json IS NOT NULL
                AND checked_at_utc IS NOT NULL AND connection_target IS NOT NULL
                AND market_json IS NOT NULL AND market_observed_at_utc IS NOT NULL
                AND account_snapshot_json IS NOT NULL AND completed_at_utc IS NOT NULL
            )),
            CHECK ((phase = '{PHASE_CLAIMED}') = (
                allowed IS NULL AND rejections_json IS NULL
                AND policy_id IS NULL AND policy_json IS NULL
                AND checked_at_utc IS NULL AND connection_target IS NULL
                AND market_json IS NULL AND market_observed_at_utc IS NULL
                AND spread IS NULL AND account_snapshot_json IS NULL
                AND account_id IS NULL AND position_count IS NULL
                AND safety_decision_id IS NULL AND completed_at_utc IS NULL
            )),
            CHECK (allowed IS NULL OR ((allowed = 1) = (rejections_json = '[]'))),
            CHECK ((order_type = 'limit') = (requested_price IS NOT NULL))
        )
    """


_RESULTS_DECISION_INDEX = f"idx_{RESULTS_TABLE}_decision"
_RESULTS_PHASE_INDEX = f"idx_{RESULTS_TABLE}_phase"


def _create_index_sql() -> tuple[str, ...]:
    return (
        f"CREATE INDEX {_RESULTS_DECISION_INDEX} ON {RESULTS_TABLE} (allowed, shadow_result_id)",
        f"CREATE INDEX {_RESULTS_PHASE_INDEX} ON {RESULTS_TABLE} (phase, shadow_result_id)",
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_create_results_table_sql())
    for sql in _create_index_sql():
        connection.execute(sql)
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


def _connect_rw(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}")
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def _verified_txn(db_path: Path):
    """Write flow: open -> BEGIN IMMEDIATE -> verify schema -> row work ->
    COMMIT (ROLLBACK on any error). Connection always explicitly closed."""
    with closing(_connect_rw(db_path)) as connection:
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
    """Read flow: TRUE read-only connection (``mode=ro`` + ``query_only``,
    PR #100 review) -> BEGIN (consistent view) -> verify schema -> restore.
    Never creates the file, journals or WAL/SHM side files."""
    with closing(connect_read_only(db_path)) as connection:
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
    with closing(_connect_rw(path)) as connection:
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
    modifying anything (true ``mode=ro`` connection). A missing file raises
    :class:`ShadowStoreError` -- callers decide whether "missing" is
    acceptable (dry-run treats a missing DB as "nothing to probe" and must
    not call this)."""
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
    data = _strict_object_text(text, _REQUEST_JSON_KEYS, "request_json")
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
    data = _strict_object_text(text, _INTENDED_ORDER_JSON_KEYS, "intended_order_json")
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
    data = _strict_object_text(text, _MARKET_JSON_KEYS, "market_json")
    return MarketSafetySnapshot(
        symbol=data["symbol"],
        reference_price=_optional_decimal(data["reference_price"], "market_json.reference_price"),
        bid=_optional_decimal(data["bid"], "market_json.bid"),
        ask=_optional_decimal(data["ask"], "market_json.ask"),
        observed_at_utc=_parse_utc(data["observed_at_utc"]),
        market_open=_strict_bool(data["market_open"], "market_json.market_open"),
        tradable=_strict_bool(data["tradable"], "market_json.tradable"),
    )


def _account_snapshot_to_json(snapshot: AccountSafetySnapshot) -> str:
    """The FULL injected account/position context the gate judged against
    (PR #100 review) -- every field the Safety Gate consumes, and ONLY model
    fields (the models carry no tokens, credentials or personal data)."""
    account = snapshot.account
    return json.dumps(
        {
            "account": {
                "account_id": account.account_id,
                "currency": account.currency,
                "balance": str(account.balance),
                "equity": str(account.equity),
                "margin_available": str(account.margin_available),
                "retrieved_at_utc": _iso(account.retrieved_at_utc),
            },
            "positions": [
                {
                    "symbol": position.symbol,
                    "side": position.side.value,
                    "quantity": str(position.quantity),
                    "average_entry_price": str(position.average_entry_price),
                    "retrieved_at_utc": _iso(position.retrieved_at_utc),
                }
                for position in snapshot.positions
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _account_snapshot_from_json(text: str) -> AccountSafetySnapshot:
    data = _strict_object_text(text, _ACCOUNT_SNAPSHOT_JSON_KEYS, "account_snapshot_json")
    account_data = data["account"]
    if not isinstance(account_data, dict) or set(account_data) != _ACCOUNT_JSON_KEYS:
        raise ValueError(
            "account_snapshot_json.account keys do not match the expected set "
            f"{sorted(_ACCOUNT_JSON_KEYS)}"
        )
    positions_data = data["positions"]
    if not isinstance(positions_data, list):
        raise ValueError("account_snapshot_json.positions must be a list")
    positions = []
    for index, entry in enumerate(positions_data):
        if not isinstance(entry, dict) or set(entry) != _POSITION_JSON_KEYS:
            raise ValueError(
                f"account_snapshot_json.positions[{index}] keys do not match the "
                f"expected set {sorted(_POSITION_JSON_KEYS)}"
            )
        positions.append(
            PositionInfo(
                symbol=entry["symbol"],
                side=PositionSide(entry["side"]),
                quantity=_strict_decimal(entry["quantity"], "position quantity"),
                average_entry_price=_strict_decimal(
                    entry["average_entry_price"], "position average_entry_price"
                ),
                retrieved_at_utc=_parse_utc(entry["retrieved_at_utc"]),
            )
        )
    return AccountSafetySnapshot(
        positions=tuple(positions),
        account=AccountState(
            account_id=account_data["account_id"],
            currency=account_data["currency"],
            balance=_strict_decimal(account_data["balance"], "account balance"),
            equity=_strict_decimal(account_data["equity"], "account equity"),
            margin_available=_strict_decimal(
                account_data["margin_available"], "account margin_available"
            ),
            retrieved_at_utc=_parse_utc(account_data["retrieved_at_utc"]),
        ),
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


def _strict_object_text(text: str, expected_keys: frozenset[str], name: str) -> dict:
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


# --- claim --------------------------------------------------------------------


def _claim_record(
    request: ExecutionRequest,
    *,
    client_order_id: str,
    timeframe: str | None,
    provenance: ShadowProvenance,
    owner_id: str,
    attempt_id: str,
    now: datetime,
    lease_seconds: int,
) -> dict:
    payload = canonical_cycle_payload(
        request,
        client_order_id=client_order_id,
        timeframe=timeframe,
        input_origin=provenance.input_origin,
    )
    return {
        "execution_request_id": request.execution_request_id,
        "signal_id": request.signal_id,
        "client_order_id": client_order_id,
        "phase": PHASE_CLAIMED,
        "claim_owner_id": owner_id,
        "claim_attempt_id": attempt_id,
        "claim_generation": 1,
        "claimed_at_utc": _iso(now),
        "lease_expires_at_utc": _iso(now + timedelta(seconds=lease_seconds)),
        "canonical_payload_json": canonical_payload_json(payload),
        "strategy_id": request.strategy_id,
        "symbol": request.symbol,
        "timeframe": timeframe,
        "side": request.side.value,
        "order_type": request.order_type.value,
        "quantity": str(request.quantity),
        "requested_price": _decimal_str(request.requested_price),
        "stop_loss_price": _decimal_str(request.stop_loss_price),
        "take_profit_price": _decimal_str(request.take_profit_price),
        "requested_at_utc": _iso(request.requested_at_utc),
        "execution_mode": ExecutionMode.SHADOW.value,
        "input_origin": provenance.input_origin,
        "signal_source": provenance.signal_source,
        "signal_observed_at_utc": None
        if provenance.signal_observed_at_utc is None
        else _iso(provenance.signal_observed_at_utc),
        "config_source": provenance.config_source,
        "config_version": provenance.config_version,
        "market_source": provenance.market_source,
        "account_source": provenance.account_source,
        "request_json": _request_to_json(request),
        "intended_order_json": _intended_order_to_json(
            intended_order_from_request(request, client_order_id=client_order_id)
        ),
    }


def _claim_from_row(row: sqlite3.Row) -> ShadowClaim:
    return ShadowClaim(
        shadow_result_id=int(row["shadow_result_id"]),
        execution_request_id=row["execution_request_id"],
        signal_id=row["signal_id"],
        client_order_id=row["client_order_id"],
        claim_owner_id=row["claim_owner_id"],
        claim_attempt_id=row["claim_attempt_id"],
        claim_generation=int(row["claim_generation"]),
        claimed_at_utc=_parse_utc(row["claimed_at_utc"]),
        lease_expires_at_utc=_parse_utc(row["lease_expires_at_utc"]),
        input_origin=row["input_origin"],
    )


def claim_shadow_cycle(
    request: ExecutionRequest,
    *,
    client_order_id: str,
    timeframe: str | None,
    provenance: ShadowProvenance,
    owner_id: str,
    attempt_id: str,
    now: datetime,
    lease_seconds: int,
    db_path: Path | None = None,
) -> tuple[str, ShadowClaim | ShadowResult]:
    """Atomically claim one shadow cycle identity BEFORE any evaluation.

    ``attempt_id`` identifies ONE ``run_cycle()`` call (PR #100 re-review
    Major 1): an ACTIVE claim is never re-acquired -- not by another attempt
    of the same owner/service either -- so exactly one attempt evaluates the
    gate. Returns ``(CLAIM_ACQUIRED, ShadowClaim)`` when this attempt owns
    the claim (fresh insert, or takeover of an EXPIRED lease -- the fencing
    ``claim_generation`` increments on takeover) or ``(CLAIM_DUPLICATE,
    ShadowResult | ShadowClaim)`` for a payload-equal existing record (the
    completed result, or the live claim of the current holder). A payload
    MISMATCH raises :class:`ShadowIdentityConflictError`; a missing DB, lock
    error or any other store failure raises :class:`ShadowStoreError` (never
    classified as duplicate). See the module docstring for the full
    claim/lease contract.
    """
    if not isinstance(request, ExecutionRequest):
        raise ShadowStoreError(f"request must be an ExecutionRequest, got {request!r}")
    if not isinstance(provenance, ShadowProvenance):
        raise ShadowStoreError(f"provenance must be a ShadowProvenance, got {provenance!r}")
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ShadowStoreError(f"owner_id must be a non-empty string, got {owner_id!r}")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ShadowStoreError(f"attempt_id must be a non-empty string, got {attempt_id!r}")
    if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds < 1:
        raise ShadowStoreError(f"lease_seconds must be a positive int, got {lease_seconds!r}")
    path = Path(db_path) if db_path is not None else shadow_db_path()
    if not path.exists():
        raise ShadowStoreError(
            f"shadow result DB at {path} does not exist; call init_db() first"
        )
    record = _claim_record(
        request,
        client_order_id=client_order_id,
        timeframe=timeframe,
        provenance=provenance,
        owner_id=owner_id,
        attempt_id=attempt_id,
        now=now,
        lease_seconds=lease_seconds,
    )
    insert_sql = (
        f"INSERT INTO {RESULTS_TABLE} ({', '.join(_CLAIM_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _CLAIM_COLUMNS)})"
    )
    try:
        with _verified_txn(path) as connection:
            try:
                cursor = connection.execute(
                    insert_sql, tuple(record[col] for col in _CLAIM_COLUMNS)
                )
            except sqlite3.IntegrityError as exc:
                return _resolve_claim_collision(
                    connection, record, exc, owner_id=owner_id, attempt_id=attempt_id,
                    now=now, lease_seconds=lease_seconds,
                )
            claim_row = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {RESULTS_TABLE} WHERE shadow_result_id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            return CLAIM_ACQUIRED, _claim_from_row(claim_row)
    except sqlite3.Error as exc:
        raise ShadowStoreError(f"shadow claim failed: {exc}") from exc


def _resolve_claim_collision(
    connection: sqlite3.Connection,
    record: dict,
    exc: sqlite3.IntegrityError,
    *,
    owner_id: str,
    attempt_id: str,
    now: datetime,
    lease_seconds: int,
):
    """Classify a claim INSERT collision inside the SAME transaction: an
    existing row must provably own one of the identity keys (otherwise the
    IntegrityError is a store failure), the canonical payload decides
    duplicate vs conflict, and ONLY an expired lease may be taken over
    (fencing generation + 1) -- an active claim is an in-progress duplicate
    for every other attempt, the same owner's attempts included."""
    rows: dict[int, sqlite3.Row] = {}
    for column in ("execution_request_id", "signal_id", "client_order_id"):
        row = connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM {RESULTS_TABLE} WHERE {column} = ?",
            (record[column],),
        ).fetchone()
        if row is not None:
            rows[int(row["shadow_result_id"])] = row
    if not rows:
        # CHECK / NOT NULL or an unknown future constraint: a persistence
        # failure, never duplicate suppression.
        raise ShadowStoreError(
            f"shadow claim INSERT failed with a non-duplicate integrity error: {exc}"
        ) from exc
    if len(rows) > 1:
        ids = ", ".join(str(row_id) for row_id in sorted(rows))
        raise ShadowIdentityConflictError(
            "identity keys (execution_request_id / signal_id / client_order_id) are "
            f"already owned by DIFFERENT shadow rows ({ids}); this cycle mixes "
            "identities and cannot be a duplicate",
            existing_execution_request_id=next(iter(rows.values()))["execution_request_id"],
            differing_fields=("execution_request_id", "signal_id", "client_order_id"),
        ) from exc
    row = next(iter(rows.values()))
    stored_payload = json.loads(row["canonical_payload_json"])
    attempted_payload = json.loads(record["canonical_payload_json"])
    if row["canonical_payload_json"] != record["canonical_payload_json"]:
        fields = differing_payload_fields(stored_payload, attempted_payload)
        raise ShadowIdentityConflictError(
            f"shadow identity for {row['execution_request_id']!r} is already bound to a "
            f"DIFFERENT canonical payload (differing fields: {', '.join(fields)}); "
            "refusing to treat a changed order as a duplicate",
            existing_execution_request_id=row["execution_request_id"],
            differing_fields=fields,
        ) from exc
    if row["phase"] == PHASE_COMPLETED:
        return CLAIM_DUPLICATE, _restore_result(row)
    # phase == claimed: an ACTIVE lease is an in-progress duplicate for EVERY
    # other attempt (owner identity does not grant re-acquisition -- a shared
    # service's second thread must not enter evaluation). Only an EXPIRED
    # lease is taken over, with the fencing generation incremented so the
    # previous holder can never complete over the takeover.
    if _parse_utc(row["lease_expires_at_utc"]) > now:
        return CLAIM_DUPLICATE, _claim_from_row(row)
    connection.execute(
        f"UPDATE {RESULTS_TABLE} SET claim_owner_id = ?, claim_attempt_id = ?, "
        "claim_generation = claim_generation + 1, claimed_at_utc = ?, "
        "lease_expires_at_utc = ? "
        "WHERE shadow_result_id = ? AND phase = ? AND claim_generation = ?",
        (
            owner_id,
            attempt_id,
            _iso(now),
            _iso(now + timedelta(seconds=lease_seconds)),
            row["shadow_result_id"],
            PHASE_CLAIMED,
            row["claim_generation"],
        ),
    )
    refreshed = connection.execute(
        f"SELECT {_SELECT_COLUMNS} FROM {RESULTS_TABLE} WHERE shadow_result_id = ?",
        (row["shadow_result_id"],),
    ).fetchone()
    if (
        refreshed["phase"] != PHASE_CLAIMED
        or refreshed["claim_attempt_id"] != attempt_id
    ):
        # Guarded update did not land (defense in depth inside the same
        # BEGIN IMMEDIATE transaction): treat as an in-progress duplicate.
        return CLAIM_DUPLICATE, _claim_from_row(refreshed)
    return CLAIM_ACQUIRED, _claim_from_row(refreshed)


# --- completion ---------------------------------------------------------------


def complete_shadow_cycle(
    execution_request_id: str,
    *,
    claim: ShadowClaim,
    decision: SafetyDecision,
    market: MarketSafetySnapshot,
    account: AccountSafetySnapshot,
    policy_snapshot: dict,
    safety_decision_id: int | None,
    completed_at_utc: datetime,
    db_path: Path | None = None,
) -> ShadowResult:
    """Turn the held claim into the COMPLETED shadow record in one UPDATE.

    ``claim`` is the exact :class:`ShadowClaim` this attempt ACQUIRED: the
    row must still be phase ``claimed`` with the SAME ``claim_owner_id``,
    ``claim_attempt_id`` AND fencing ``claim_generation`` (PR #100 re-review
    Major 1) -- and the UPDATE itself is additionally guarded on the fencing
    fields. A lost claim (lease expired and taken over; the generation moved
    on) raises :class:`ShadowStoreError` -- the takeover winner owns the
    cycle now and can never be overwritten by the stale holder. A failure
    here leaves the claim row intact and reclaimable (recoverable state; any
    decision row already written becomes a reportable orphan).
    """
    if not isinstance(claim, ShadowClaim):
        raise ShadowStoreError(f"claim must be a ShadowClaim, got {claim!r}")
    if claim.execution_request_id != execution_request_id:
        raise ShadowStoreError(
            f"claim belongs to {claim.execution_request_id!r}, "
            f"completing {execution_request_id!r}"
        )
    if not isinstance(decision, SafetyDecision):
        raise ShadowStoreError(f"decision must be a SafetyDecision, got {decision!r}")
    if decision.execution_request_id != execution_request_id:
        raise ShadowStoreError(
            f"decision belongs to {decision.execution_request_id!r}, "
            f"completing {execution_request_id!r}"
        )
    if not isinstance(market, MarketSafetySnapshot):
        raise ShadowStoreError(f"market must be a MarketSafetySnapshot, got {market!r}")
    if not isinstance(account, AccountSafetySnapshot):
        raise ShadowStoreError(f"account must be an AccountSafetySnapshot, got {account!r}")
    path = Path(db_path) if db_path is not None else shadow_db_path()
    if not path.exists():
        raise ShadowStoreError(f"shadow result DB at {path} does not exist")
    completion = {
        "allowed": int(decision.allowed),
        "rejections_json": _rejections_to_json(decision.rejections),
        "policy_id": decision.policy_id,
        "policy_json": json.dumps(policy_snapshot, sort_keys=True, separators=(",", ":")),
        "checked_at_utc": _iso(decision.checked_at_utc),
        "connection_target": decision.connection_target,
        "market_json": _market_to_json(market),
        "market_observed_at_utc": _iso(market.observed_at_utc),
        "spread": _decimal_str(market.spread),
        "account_snapshot_json": _account_snapshot_to_json(account),
        "account_id": decision.account_id,
        "position_count": decision.position_count,
        "safety_decision_id": safety_decision_id,
        "completed_at_utc": _iso(completed_at_utc),
    }
    try:
        with _verified_txn(path) as connection:
            row = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {RESULTS_TABLE} WHERE execution_request_id = ?",
                (execution_request_id,),
            ).fetchone()
            if row is None:
                raise ShadowStoreError(
                    f"no shadow claim exists for {execution_request_id!r}; "
                    "claim_shadow_cycle() must run first"
                )
            if row["phase"] != PHASE_CLAIMED:
                raise ShadowStoreError(
                    f"shadow cycle {execution_request_id!r} is already {row['phase']!r}"
                )
            if (
                row["claim_owner_id"] != claim.claim_owner_id
                or row["claim_attempt_id"] != claim.claim_attempt_id
                or row["claim_generation"] != claim.claim_generation
            ):
                raise ShadowStoreError(
                    f"claim for {execution_request_id!r} is now held by "
                    f"{row['claim_owner_id']!r} (attempt {row['claim_attempt_id']!r}, "
                    f"generation {row['claim_generation']}); this attempt "
                    f"({claim.claim_attempt_id!r}, generation {claim.claim_generation}) "
                    "lost the cycle and must not overwrite it"
                )
            assignments = ", ".join(f"{column} = ?" for column in _COMPLETION_COLUMNS)
            connection.execute(
                f"UPDATE {RESULTS_TABLE} SET phase = ?, {assignments} "
                "WHERE shadow_result_id = ? AND phase = ? "
                "AND claim_attempt_id = ? AND claim_generation = ?",
                (
                    PHASE_COMPLETED,
                    *(completion[column] for column in _COMPLETION_COLUMNS),
                    row["shadow_result_id"],
                    PHASE_CLAIMED,
                    claim.claim_attempt_id,
                    claim.claim_generation,
                ),
            )
            completed = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {RESULTS_TABLE} WHERE shadow_result_id = ?",
                (row["shadow_result_id"],),
            ).fetchone()
            return _restore_result(completed)
    except sqlite3.Error as exc:
        raise ShadowStoreError(f"shadow completion failed: {exc}") from exc


# --- read ---------------------------------------------------------------------

_POLICY_SNAPSHOT_KEYS = frozenset(
    {
        "policy_id",
        "allowed_symbols",
        "allowed_sides",
        "max_order_quantity",
        "max_positions_per_symbol",
        "max_positions_account",
        "max_reference_age_seconds",
        "max_spread_by_symbol",
        "allowed_connection_targets",
        "require_stop_loss",
    }
)


def _snapshot_str_list(value, name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty list of strings")
    for entry in value:
        if not isinstance(entry, str):
            raise ValueError(f"{name} entries must be strings, got {entry!r}")
    return value


def _policy_from_snapshot(data) -> SafetyPolicy:
    """Closed-world reconstruction of the stored policy snapshot into a REAL
    :class:`SafetyPolicy` (PR #100 re-review Major 4): unknown keys, wrong
    types, invalid Decimals / booleans / modes all fail restore -- a policy
    snapshot is never accepted as an arbitrary dict."""
    if not isinstance(data, dict) or set(data) != _POLICY_SNAPSHOT_KEYS:
        raise ValueError(
            f"policy snapshot keys {sorted(data) if isinstance(data, dict) else type(data).__name__} "
            f"do not match the expected set {sorted(_POLICY_SNAPSHOT_KEYS)}"
        )
    sides = []
    for side in _snapshot_str_list(data["allowed_sides"], "policy snapshot allowed_sides"):
        sides.append(OrderSide(side))
    for name in ("max_positions_per_symbol", "max_positions_account"):
        if not isinstance(data[name], int) or isinstance(data[name], bool):
            raise ValueError(f"policy snapshot {name} must be an integer, got {data[name]!r}")
    age = data["max_reference_age_seconds"]
    # math.isfinite: plain json.loads accepts the Infinity/NaN constants, and
    # timedelta(seconds=inf) would raise OverflowError instead of a stable
    # integrity error (PR #100 3rd-review Major 3).
    if (
        isinstance(age, bool)
        or not isinstance(age, (int, float))
        or not math.isfinite(age)
        or not age > 0
    ):
        raise ValueError(
            f"policy snapshot max_reference_age_seconds must be a positive finite "
            f"number, got {age!r}"
        )
    spreads_raw = data["max_spread_by_symbol"]
    if not isinstance(spreads_raw, dict):
        raise ValueError("policy snapshot max_spread_by_symbol must be an object")
    spreads = {
        symbol: _strict_decimal(value, f"policy snapshot max_spread_by_symbol[{symbol}]")
        for symbol, value in spreads_raw.items()
    }
    targets_raw = data["allowed_connection_targets"]
    if not isinstance(targets_raw, dict):
        raise ValueError("policy snapshot allowed_connection_targets must be an object")
    targets = {
        ExecutionMode(mode): frozenset(
            _snapshot_str_list(entries, f"policy snapshot allowed_connection_targets[{mode}]")
        )
        for mode, entries in targets_raw.items()
    }
    if not isinstance(data["require_stop_loss"], bool):
        raise ValueError(
            f"policy snapshot require_stop_loss must be a boolean, got {data['require_stop_loss']!r}"
        )
    policy = SafetyPolicy(
        policy_id=data["policy_id"],
        allowed_symbols=frozenset(
            _snapshot_str_list(data["allowed_symbols"], "policy snapshot allowed_symbols")
        ),
        allowed_sides=frozenset(sides),
        max_order_quantity=_strict_decimal(
            data["max_order_quantity"], "policy snapshot max_order_quantity"
        ),
        max_positions_per_symbol=data["max_positions_per_symbol"],
        max_positions_account=data["max_positions_account"],
        max_reference_age=timedelta(seconds=age),
        max_spread_by_symbol=spreads,
        allowed_connection_targets=targets,
        require_stop_loss=data["require_stop_loss"],
    )
    # Round-trip equality: the snapshot was WRITTEN by SafetyPolicy.to_snapshot,
    # so a snapshot that no longer reproduces itself is tamper evidence.
    if policy.to_snapshot() != data:
        raise ValueError("policy snapshot does not round-trip through SafetyPolicy")
    return policy


def _restore_result(row: sqlite3.Row) -> ShadowResult:
    """Rebuild one COMPLETED ShadowResult STRICTLY: JSON key sets must match
    exactly, the searchable columns must agree with the JSON payloads, the
    canonical payload column must equal the recomputed canonical payload, and
    enums / UTC / allow-reject consistency are re-validated by the model
    constructors."""
    try:
        if row["phase"] != PHASE_COMPLETED:
            raise ValueError(f"expected a completed row, got phase {row['phase']!r}")
        request = _request_from_json(row["request_json"])
        intended = _intended_order_from_json(row["intended_order_json"])
        market = _market_from_json(row["market_json"])
        account = _account_snapshot_from_json(row["account_snapshot_json"])
        rejections = _rejections_from_json(row["rejections_json"])
        if row["allowed"] not in (0, 1):
            raise ValueError(f"allowed must be 0 or 1, got {row['allowed']!r}")
        policy_snapshot = json.loads(row["policy_json"])
        # Deep closed-world validation + SafetyPolicy reconstruction (PR #100
        # re-review Major 4) -- never accepted as an arbitrary dict.
        reconstructed_policy = _policy_from_snapshot(policy_snapshot)
        if reconstructed_policy.policy_id != row["policy_id"]:
            raise ValueError(
                f"policy snapshot policy_id {reconstructed_policy.policy_id!r} does not "
                f"match the policy_id column {row['policy_id']!r}"
            )
        provenance = ShadowProvenance(
            input_origin=row["input_origin"],
            signal_source=row["signal_source"],
            config_source=row["config_source"],
            config_version=row["config_version"],
            market_source=row["market_source"],
            account_source=row["account_source"],
            signal_observed_at_utc=None
            if row["signal_observed_at_utc"] is None
            else _parse_utc(row["signal_observed_at_utc"]),
        )
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
        expected_payload = canonical_payload_json(
            canonical_cycle_payload(
                request,
                client_order_id=row["client_order_id"],
                timeframe=row["timeframe"],
                input_origin=row["input_origin"],
            )
        )
        if row["canonical_payload_json"] != expected_payload:
            raise ValueError("canonical_payload_json does not match the stored request")
        if row["position_count"] is not None and row["position_count"] != len(account.positions):
            raise ValueError(
                f"position_count column ({row['position_count']!r}) does not match the "
                f"stored account snapshot ({len(account.positions)} positions)"
            )
        # account_id must agree across the projection column (= the decision's
        # recorded value) and the stored account snapshot (PR #100 re-review
        # Major 4). A NULL column is the explicit "snapshot deemed unreliable"
        # case and carries no cross-constraint.
        if row["account_id"] is not None and row["account_id"] != account.account.account_id:
            raise ValueError(
                f"account_id column ({row['account_id']!r}) does not match the stored "
                f"account snapshot ({account.account.account_id!r})"
            )
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
            provenance=provenance,
            decision=decision,
            intended_order=intended,
            market=market,
            account=account,
            policy_snapshot=policy_snapshot,
            safety_decision_id=row["safety_decision_id"],
            claim_owner_id=row["claim_owner_id"],
            claimed_at_utc=_parse_utc(row["claimed_at_utc"]),
            completed_at_utc=_parse_utc(row["completed_at_utc"]),
            shadow_result_id=row["shadow_result_id"],
        )
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        # OverflowError: a tampered numeric (e.g. a huge or non-finite value
        # reaching a datetime/timedelta conversion) is integrity evidence, not
        # a crash (PR #100 3rd-review Major 3).
        raise ShadowIntegrityError(
            f"shadow result row {row['shadow_result_id']!r} failed restore-time "
            f"validation: {exc}"
        ) from exc


_SELECT_COLUMNS = "shadow_result_id, " + ", ".join(_ALL_COLUMNS)


def _read_rows(db_path: Path | None, where: str, params: tuple, *, order: str, limit: int | None):
    path = Path(db_path) if db_path is not None else shadow_db_path()
    if not path.exists():
        return ()
    limit_sql = ""
    if limit is not None:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIST_LIMIT:
            raise ShadowStoreError(
                f"limit must be an int between 1 and {MAX_LIST_LIMIT}, got {limit!r}"
            )
        limit_sql = f" LIMIT {limit}"
    with _verified_read(path) as connection:
        return connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM {RESULTS_TABLE} {where} ORDER BY {order}{limit_sql}",
            params,
        ).fetchall()


def get_shadow_result(
    execution_request_id: str, *, db_path: Path | None = None
) -> ShadowResult | None:
    """The COMPLETED shadow result for one execution request (at most one row
    by UNIQUE). Missing DB -> None, never created (dry-run safe)."""
    _require_key(execution_request_id, "execution_request_id")
    rows = _read_rows(
        db_path,
        "WHERE execution_request_id = ? AND phase = ?",
        (execution_request_id, PHASE_COMPLETED),
        order="shadow_result_id ASC",
        limit=None,
    )
    return _restore_result(rows[0]) if rows else None


def get_shadow_result_by_signal_id(
    signal_id: str, *, db_path: Path | None = None
) -> ShadowResult | None:
    """Reverse lookup from the source signal (completed rows only)."""
    _require_key(signal_id, "signal_id")
    rows = _read_rows(
        db_path,
        "WHERE signal_id = ? AND phase = ?",
        (signal_id, PHASE_COMPLETED),
        order="shadow_result_id ASC",
        limit=None,
    )
    return _restore_result(rows[0]) if rows else None


def get_shadow_result_by_decision_id(
    safety_decision_id: int, *, db_path: Path | None = None
) -> ShadowResult | None:
    """Lookup by the referenced Safety Decision row (PR #100 review:
    ``shadow_report --safety-decision-id``)."""
    if (
        not isinstance(safety_decision_id, int)
        or isinstance(safety_decision_id, bool)
        or safety_decision_id < 1
    ):
        raise ShadowStoreError(
            f"safety_decision_id must be a positive int, got {safety_decision_id!r}"
        )
    rows = _read_rows(
        db_path,
        "WHERE safety_decision_id = ? AND phase = ?",
        (safety_decision_id, PHASE_COMPLETED),
        order="shadow_result_id ASC",
        limit=None,
    )
    return _restore_result(rows[0]) if rows else None


def get_shadow_claim(
    execution_request_id: str, *, db_path: Path | None = None
) -> ShadowClaim | None:
    """The live (phase ``claimed``) claim for one execution request, if any."""
    _require_key(execution_request_id, "execution_request_id")
    rows = _read_rows(
        db_path,
        "WHERE execution_request_id = ? AND phase = ?",
        (execution_request_id, PHASE_CLAIMED),
        order="shadow_result_id ASC",
        limit=None,
    )
    return _claim_from_row(rows[0]) if rows else None


def list_shadow_results(
    *, allowed: bool | None = None, limit: int | None = None, db_path: Path | None = None
) -> tuple[ShadowResult, ...]:
    """COMPLETED shadow results, NEWEST FIRST (``shadow_result_id DESC`` --
    the documented report ordering), optionally filtered by the gate decision
    and bounded by ``limit`` (1..{MAX_LIST_LIMIT}). Missing DB -> empty tuple,
    never created."""
    where = f"WHERE phase = '{PHASE_COMPLETED}'"
    params: tuple = ()
    if allowed is not None:
        if not isinstance(allowed, bool):
            raise ShadowStoreError(f"allowed must be a bool or None, got {allowed!r}")
        where += " AND allowed = ?"
        params = (int(allowed),)
    rows = _read_rows(db_path, where, params, order="shadow_result_id DESC", limit=limit)
    return tuple(_restore_result(row) for row in rows)


def list_shadow_claims(
    *, limit: int | None = None, db_path: Path | None = None
) -> tuple[ShadowClaim, ...]:
    """Live claims (phase ``claimed``), newest first: in-flight cycles plus
    stale claims from crashed runs awaiting lease-expiry recovery."""
    rows = _read_rows(
        db_path,
        f"WHERE phase = '{PHASE_CLAIMED}'",
        (),
        order="shadow_result_id DESC",
        limit=limit,
    )
    return tuple(_claim_from_row(row) for row in rows)


def _require_key(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ShadowStoreError(f"{field_name} must be a non-empty string, got {value!r}")
