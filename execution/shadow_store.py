"""Shadow Mode verification-record model + SQLite persistence (Issue #78).

*** OBSERVATION RECORDS ONLY. No broker calls, no network I/O. This store
persists what the NORMAL decision path (ExecutionRequest + Safety Gate)
produced during a shadow run -- it never submits anything, and it never
stores broker-response order states (there is no broker order to have a
state: nothing was sent). ***

One DB file (``execution/data/shadow_executions.sqlite3`` by default,
``SHADOW_EXECUTION_DB_PATH`` to override), owned by ``execution/`` alone.
It is deliberately a SEPARATE file from ``execution_orders.sqlite3`` and
``safety_gate_decisions.sqlite3``:

- both existing stores verify their DB as a closed world (any foreign table
  fails verification), so the schemas cannot share a file, and
- shadow observations must NEVER pollute the real submission idempotency
  state (Issue #76): the ``signal_id`` UNIQUE constraint of the execution
  order store belongs to real registrations only. Shadow keeps its OWN
  per-mode uniqueness (below), so a shadow-observed signal can still proceed
  to a fake/demo/live submission later, and an already-submitted signal can
  still be shadow-observed -- the deduplication unit is per store (= per
  mode family), never across modes.

What one row holds (Issue #78): the FULL stored intent (every
``ExecutionRequest`` field, so the request can be reconstructed exactly),
the full ``SafetyDecision`` (allow/reject, machine-readable rejection codes
+ human-readable messages, ``policy_id``, the connection identity the gate
evaluated against, account/position info), the derived intended order
content (what WOULD have crossed the broker boundary -- WITHOUT a
``client_order_id``: shadow never mints broker keys), the fixed execution
mode ``shadow`` and the shadow observation time. Deliberately ABSENT: any
broker ``OrderState`` -- a record must never pretend the order was
accepted/filled/rejected by a broker, and a Safety Gate rejection is never
conflated with a broker rejection.

Idempotency (Issue #78 x Issue #76): ``execution_request_id`` and
``signal_id`` are both UNIQUE inside THIS store -- one shadow verification
record per intent and per source signal. Re-running shadow over the same
signal is suppressed by the DB constraint inside a ``BEGIN IMMEDIATE``
transaction (concurrent runners included: the loser gets
:class:`ShadowExecutionConflictError`, never a second row) and WARN-logged
with the ids and the suppression reason, mirroring the execution store's
duplicate suppression contract.

Schema policy (same stance as ``safety_store`` / ``execution_store``,
PR #91): ``initialize()`` creates the schema only into a PRISTINE database
(zero ``sqlite_master`` objects and ``user_version = 0``); anything else
must pass full verification or fails with :class:`ShadowSchemaError`
WITHOUT modifying the file. Every read and write verifies the schema first
-- a closed-world comparison of the complete ``sqlite_master`` object set
(token-normalized DDL) plus the PRAGMA structural signature and the schema
version. Reads never create a missing DB file (dry-run safe) and
re-validate every stored row strictly (exact JSON key sets, known rejection
codes, UTC timestamps, allow/reject consistency, the intended order
matching the stored intent, model re-validation via ``ExecutionRequest`` /
``SafetyDecision`` construction) -- an out-of-band edited DB raises
:class:`ShadowIntegrityError` instead of being trusted. A failed write
rolls back whole (no partial record survives). Connections are always
explicitly closed (Windows file-handle hygiene, Issue #83 r2).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from broker.broker_models import OrderSide, OrderType
from execution.execution_models import ExecutionRequest, _require_utc
from execution.execution_store import _ddl_tokens  # shared DDL token normalizer (PR #91 r3)
from execution.safety_gate import ExecutionMode, SafetyDecision, SafetyRejection, SafetyRejectionCode

_LOGGER = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "shadow_executions.sqlite3"
BUSY_TIMEOUT_SECONDS = 30

SHADOW_TABLE = "shadow_executions"
SCHEMA_VERSION = 1

_SHADOW_COLUMNS: tuple[str, ...] = (
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
    "execution_mode",
    "observed_at_utc",
    "allowed",
    "checked_at_utc",
    "policy_id",
    "connection_mode",
    "connection_target",
    "account_id",
    "position_count",
    "rejections_json",
    "intended_order_json",
)

# Strict JSON shapes: key sets must match EXACTLY on restore.
_REJECTION_JSON_KEYS: frozenset[str] = frozenset({"code", "message"})
_INTENDED_ORDER_KEYS: frozenset[str] = frozenset(
    {"symbol", "side", "order_type", "quantity", "limit_price"}
)


class ShadowStoreError(RuntimeError):
    """Base class for shadow execution store errors."""


class ShadowSchemaError(ShadowStoreError):
    """The on-disk schema does not match the expected Issue #78 schema."""


class ShadowIntegrityError(ShadowStoreError):
    """Stored data fails restore-time validation (out-of-band corruption)."""


class ShadowExecutionConflictError(ShadowStoreError):
    """An insert collided with a UNIQUE constraint (execution_request_id /
    signal_id): this intent or source signal already holds a shadow record."""


def shadow_db_path() -> Path:
    raw = (os.getenv("SHADOW_EXECUTION_DB_PATH") or str(DEFAULT_DB_PATH)).strip()
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


def _parse_utc(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError(f"stored timestamp is not UTC: {text!r}")
    return value


@dataclass(frozen=True)
class ShadowExecutionRecord:
    """One shadow verification record: the intent that ran through the
    NORMAL decision path, the Safety Gate decision it produced, and when the
    shadow observation happened.

    The execution mode of every record is fixed to ``shadow``
    (:attr:`execution_mode`); the CONNECTION identity the gate evaluated
    against (e.g. ``fake`` today, a demo/live adapter in the future) is a
    different fact and lives inside ``decision.execution_mode`` /
    ``decision.connection_target``. The two are stored separately so a
    shadow record can never be mistaken for a submission of that mode.
    """

    request: ExecutionRequest
    decision: SafetyDecision
    observed_at_utc: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.request, ExecutionRequest):
            raise ValueError(f"request must be an ExecutionRequest, got {self.request!r}")
        if not isinstance(self.decision, SafetyDecision):
            raise ValueError(f"decision must be a SafetyDecision, got {self.decision!r}")
        if self.decision.execution_request_id != self.request.execution_request_id:
            raise ValueError(
                f"decision belongs to {self.decision.execution_request_id!r}, "
                f"request is {self.request.execution_request_id!r}"
            )
        _require_utc(self.observed_at_utc, "observed_at_utc")

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.SHADOW

    @property
    def intended_order(self) -> dict:
        """What WOULD have crossed the broker boundary, as plain values.

        Deliberately WITHOUT a ``client_order_id`` (shadow never mints broker
        keys -- the fixed key is bound at real registration, Issue #74) and
        WITHOUT SL/TP (they never cross the broker boundary, Issue #73; they
        remain reconstructable from :attr:`request`).
        """
        return {
            "symbol": self.request.symbol,
            "side": self.request.side.value,
            "order_type": self.request.order_type.value,
            "quantity": str(self.request.quantity),
            "limit_price": (
                str(self.request.requested_price)
                if self.request.order_type is OrderType.LIMIT
                else None
            ),
        }


# --- schema -------------------------------------------------------------------


def _enum_values_sql(values) -> str:
    return "', '".join(sorted(v.value for v in values))


def _create_shadow_table_sql() -> str:
    """Full current schema. Decimals are stored as canonical TEXT (never
    REAL, Issue #73/#74). The CHECKs mirror the model invariants as a second
    guard -- foremost allow/reject consistency and the FIXED ``shadow``
    execution mode. There is deliberately NO broker order-state column."""
    sides = _enum_values_sql(OrderSide)
    types = _enum_values_sql(OrderType)
    return f"""
        CREATE TABLE {SHADOW_TABLE} (
            shadow_id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_request_id TEXT NOT NULL UNIQUE CHECK (trim(execution_request_id) <> ''),
            signal_id TEXT NOT NULL UNIQUE CHECK (trim(signal_id) <> ''),
            strategy_id TEXT NOT NULL CHECK (trim(strategy_id) <> ''),
            symbol TEXT NOT NULL CHECK (trim(symbol) <> ''),
            side TEXT NOT NULL CHECK (side IN ('{sides}')),
            order_type TEXT NOT NULL CHECK (order_type IN ('{types}')),
            quantity TEXT NOT NULL CHECK (trim(quantity) <> ''),
            requested_price TEXT CHECK (requested_price IS NULL OR trim(requested_price) <> ''),
            stop_loss_price TEXT CHECK (stop_loss_price IS NULL OR trim(stop_loss_price) <> ''),
            take_profit_price TEXT CHECK (take_profit_price IS NULL OR trim(take_profit_price) <> ''),
            requested_at_utc TEXT NOT NULL CHECK (requested_at_utc LIKE '%+00:00'),
            execution_mode TEXT NOT NULL CHECK (execution_mode = '{ExecutionMode.SHADOW.value}'),
            observed_at_utc TEXT NOT NULL CHECK (observed_at_utc LIKE '%+00:00'),
            allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
            checked_at_utc TEXT NOT NULL CHECK (checked_at_utc LIKE '%+00:00'),
            policy_id TEXT NOT NULL CHECK (trim(policy_id) <> ''),
            connection_mode TEXT CHECK (connection_mode IS NULL OR trim(connection_mode) <> ''),
            connection_target TEXT CHECK (connection_target IS NULL OR trim(connection_target) <> ''),
            account_id TEXT CHECK (account_id IS NULL OR trim(account_id) <> ''),
            position_count INTEGER CHECK (position_count IS NULL OR position_count >= 0),
            rejections_json TEXT NOT NULL CHECK (trim(rejections_json) <> ''),
            intended_order_json TEXT NOT NULL CHECK (trim(intended_order_json) <> ''),
            CHECK ((allowed = 1) = (rejections_json = '[]')),
            CHECK ((order_type = '{OrderType.LIMIT.value}') = (requested_price IS NOT NULL))
        )
    """


_SHADOW_OBSERVED_INDEX = f"idx_{SHADOW_TABLE}_observed"


def _create_index_sql() -> str:
    return (
        f"CREATE INDEX {_SHADOW_OBSERVED_INDEX} ON {SHADOW_TABLE} "
        "(observed_at_utc, shadow_id)"
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_create_shadow_table_sql())
    connection.execute(_create_index_sql())
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _structure_signature(connection: sqlite3.Connection) -> dict:
    signature: dict = {
        f"table_info:{SHADOW_TABLE}": [
            tuple(row) for row in connection.execute(f"PRAGMA table_info({SHADOW_TABLE})")
        ],
        f"foreign_keys:{SHADOW_TABLE}": [
            tuple(row) for row in connection.execute(f"PRAGMA foreign_key_list({SHADOW_TABLE})")
        ],
    }
    index_entries = []
    for index_row in connection.execute(f"PRAGMA index_list({SHADOW_TABLE})"):
        columns = [
            tuple(info) for info in connection.execute(f"PRAGMA index_info({index_row[1]})")
        ]
        index_entries.append((index_row[1], index_row[2], index_row[3], index_row[4], columns))
    signature[f"indexes:{SHADOW_TABLE}"] = sorted(index_entries)
    return signature


_REFERENCE_SCHEMA_CACHE: tuple[dict, dict] | None = None


def _reference_schema() -> tuple[dict, dict]:
    """Pristine reference schema built once in memory: the complete
    ``sqlite_master`` object map (``sqlite_sequence`` from AUTOINCREMENT and
    ``sqlite_autoindex_*`` from the UNIQUE constraints included) plus the
    PRAGMA structural signature."""
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
    """Closed-world verification (same stance as ``safety_store``): the
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
        raise ShadowSchemaError(
            f"shadow execution DB at {db_path} failed schema verification; "
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
    COMMIT (ROLLBACK on any error, so a failed write leaves no partial
    record). Connection always explicitly closed."""
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


# --- JSON (de)serialization ----------------------------------------------------


def _rejections_to_json(rejections: tuple[SafetyRejection, ...]) -> str:
    return json.dumps(
        [{"code": item.code.value, "message": item.message} for item in rejections],
        separators=(",", ":"),
    )


def _rejections_from_json(text: str) -> tuple[SafetyRejection, ...]:
    """Rebuild rejections STRICTLY: a list of objects each holding exactly
    ``code`` (a known ``SafetyRejectionCode`` value) and ``message`` (a
    non-empty string). Anything else raises ``ValueError``."""
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


def _intended_order_to_json(record: ShadowExecutionRecord) -> str:
    return json.dumps(record.intended_order, sort_keys=True, separators=(",", ":"))


# --- restore -------------------------------------------------------------------


def _record_from_row(row: sqlite3.Row) -> ShadowExecutionRecord:
    """Rebuild the typed record STRICTLY. ``ExecutionRequest`` /
    ``SafetyDecision`` construction re-runs every model invariant, and the
    stored intended order must equal its re-derivation from the stored
    intent -- a tampered row fails loudly instead of being trusted."""
    if row["execution_mode"] != ExecutionMode.SHADOW.value:
        raise ValueError(
            f"execution_mode must be {ExecutionMode.SHADOW.value!r}, got {row['execution_mode']!r}"
        )
    if row["allowed"] not in (0, 1):
        raise ValueError(f"allowed must be 0 or 1, got {row['allowed']!r}")
    if row["position_count"] is not None and (
        not isinstance(row["position_count"], int) or row["position_count"] < 0
    ):
        raise ValueError(f"position_count is invalid: {row['position_count']!r}")

    def dec(value):
        return None if value is None else Decimal(value)

    request = ExecutionRequest(
        execution_request_id=row["execution_request_id"],
        signal_id=row["signal_id"],
        strategy_id=row["strategy_id"],
        symbol=row["symbol"],
        side=OrderSide(row["side"]),
        order_type=OrderType(row["order_type"]),
        quantity=Decimal(row["quantity"]),
        requested_at_utc=_parse_utc(row["requested_at_utc"]),
        requested_price=dec(row["requested_price"]),
        stop_loss_price=dec(row["stop_loss_price"]),
        take_profit_price=dec(row["take_profit_price"]),
    )
    decision = SafetyDecision(
        execution_request_id=row["execution_request_id"],
        allowed=bool(row["allowed"]),
        checked_at_utc=_parse_utc(row["checked_at_utc"]),
        policy_id=row["policy_id"],
        rejections=_rejections_from_json(row["rejections_json"]),
        execution_mode=row["connection_mode"],
        connection_target=row["connection_target"],
        account_id=row["account_id"],
        position_count=row["position_count"],
    )
    record = ShadowExecutionRecord(
        request=request,
        decision=decision,
        observed_at_utc=_parse_utc(row["observed_at_utc"]),
    )
    stored_intended = json.loads(row["intended_order_json"])
    if not isinstance(stored_intended, dict) or set(stored_intended) != _INTENDED_ORDER_KEYS:
        raise ValueError(
            f"intended order keys "
            f"{sorted(stored_intended) if isinstance(stored_intended, dict) else type(stored_intended).__name__} "
            f"do not match the expected set {sorted(_INTENDED_ORDER_KEYS)}"
        )
    if stored_intended != record.intended_order:
        raise ValueError(
            "stored intended order does not match its re-derivation from the stored intent"
        )
    return record


class SQLiteShadowExecutionStore:
    """The DURABLE shadow verification-record store (Issue #78).

    ``record`` raises :class:`ShadowStoreError` subclasses on any failure
    (fail closed: an observation that could not be persisted is a failed
    observation -- nothing was sent either way, but the run must not report
    success). Duplicate intents / source signals raise
    :class:`ShadowExecutionConflictError` (see the module docstring).
    """

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path is not None else shadow_db_path()

    @property
    def path(self) -> Path:
        return self._path

    def initialize(self) -> None:
        """Create the schema (pristine DB only) or verify an existing one.

        Same pristine rule as ``safety_store`` / ``execution_store``:
        creation happens only when ``sqlite_master`` is empty AND
        ``user_version`` is 0; any other content must pass full verification
        or fails WITHOUT modifying the file.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(_connect(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                objects = connection.execute("SELECT name FROM sqlite_master").fetchall()
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if not objects:
                    if version != 0:
                        raise ShadowSchemaError(
                            f"DB at {self._path} is empty but carries user_version {version}; "
                            "not a pristine DB -- refusing to initialize it"
                        )
                    _create_schema(connection)
                    _verify_schema(connection, self._path)
                else:
                    _verify_schema(connection, self._path)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def record(self, record: ShadowExecutionRecord) -> ShadowExecutionRecord:
        """Persist one shadow verification record and return it as READ BACK
        from the same transaction (so a successful return proves the record
        is restorable). Raises on ANY failure (fail closed).

        A missing DB file is a failure, not a trigger for implicit creation:
        the store must be initialized deliberately. A duplicate
        ``execution_request_id`` or ``signal_id`` raises
        :class:`ShadowExecutionConflictError` -- the UNIQUE constraints
        inside the ``BEGIN IMMEDIATE`` transaction (not any pre-SELECT) make
        this safe under concurrent shadow runners; every suppressed
        duplicate is WARN-logged with the ids and the reason (Issue #76
        suppression-log contract).
        """
        if not isinstance(record, ShadowExecutionRecord):
            raise ShadowStoreError(f"record must be a ShadowExecutionRecord, got {record!r}")
        if not self._path.exists():
            raise ShadowStoreError(
                f"shadow execution DB at {self._path} does not exist; call initialize() first"
            )
        request, decision = record.request, record.decision

        def dec(value):
            return None if value is None else str(value)

        values = {
            "execution_request_id": request.execution_request_id,
            "signal_id": request.signal_id,
            "strategy_id": request.strategy_id,
            "symbol": request.symbol,
            "side": request.side.value,
            "order_type": request.order_type.value,
            "quantity": str(request.quantity),
            "requested_price": dec(request.requested_price),
            "stop_loss_price": dec(request.stop_loss_price),
            "take_profit_price": dec(request.take_profit_price),
            "requested_at_utc": request.requested_at_utc.isoformat(),
            "execution_mode": ExecutionMode.SHADOW.value,
            "observed_at_utc": record.observed_at_utc.isoformat(),
            "allowed": int(decision.allowed),
            "checked_at_utc": decision.checked_at_utc.isoformat(),
            "policy_id": decision.policy_id,
            "connection_mode": decision.execution_mode,
            "connection_target": decision.connection_target,
            "account_id": decision.account_id,
            "position_count": decision.position_count,
            "rejections_json": _rejections_to_json(decision.rejections),
            "intended_order_json": _intended_order_to_json(record),
        }
        insert_sql = (
            f"INSERT INTO {SHADOW_TABLE} ({', '.join(_SHADOW_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _SHADOW_COLUMNS)})"
        )
        with _verified_txn(self._path) as connection:
            try:
                cursor = connection.execute(
                    insert_sql, tuple(values[col] for col in _SHADOW_COLUMNS)
                )
            except sqlite3.IntegrityError as exc:
                conflict = self._classify_conflict(connection, values)
                if conflict is None:
                    # CHECK / NOT NULL or an unknown future constraint is a
                    # persistence failure, not duplicate suppression.
                    raise ShadowStoreError(
                        f"shadow record for {request.execution_request_id!r} could not "
                        f"be persisted: {exc}"
                    ) from exc
                raise conflict from exc
            stored = connection.execute(
                f"SELECT * FROM {SHADOW_TABLE} WHERE shadow_id = ?", (cursor.lastrowid,)
            ).fetchone()
            try:
                restored = _record_from_row(stored)
            except (ValueError, TypeError) as exc:
                raise ShadowIntegrityError(
                    f"shadow record for {request.execution_request_id!r} failed its "
                    f"write-time read-back validation: {exc}"
                ) from exc
            if restored != record:
                raise ShadowIntegrityError(
                    f"shadow record for {request.execution_request_id!r} did not round-trip; "
                    "rolling back"
                )
        return restored

    def _classify_conflict(
        self, connection: sqlite3.Connection, values: dict
    ) -> ShadowExecutionConflictError | None:
        conflicts = []
        for column, reason in (
            ("execution_request_id", "execution_request_id already holds a shadow record"),
            ("signal_id", "source signal already holds a shadow record"),
        ):
            row = connection.execute(
                f"SELECT execution_request_id, signal_id, allowed, observed_at_utc "
                f"FROM {SHADOW_TABLE} WHERE {column} = ?",
                (values[column],),
            ).fetchone()
            if row is not None:
                conflicts.append((column, reason, row))
        if not conflicts:
            return None
        reason = "; ".join(item[1] for item in conflicts)
        existing = conflicts[0][2]
        _LOGGER.warning(
            "duplicate shadow execution suppressed: execution_request_id=%s signal_id=%s "
            "existing_execution_request_id=%s existing_allowed=%s existing_observed_at=%s "
            "conflicting_keys=%s reason=%s",
            values["execution_request_id"],
            values["signal_id"],
            existing["execution_request_id"],
            existing["allowed"],
            existing["observed_at_utc"],
            ",".join(item[0] for item in conflicts),
            reason,
        )
        return ShadowExecutionConflictError(
            f"shadow record for execution_request_id {values['execution_request_id']!r} "
            f"(signal_id {values['signal_id']!r}) collides with an existing record ({reason})"
        )

    # --- reads (never create a missing DB file: dry-run safe) -----------------

    def list_records(self, *, limit: int | None = None) -> tuple[ShadowExecutionRecord, ...]:
        """All shadow records in DETERMINISTIC order (observed_at_utc, then
        insertion order). Missing DB -> empty tuple, file never created."""
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
            raise ShadowStoreError(f"limit must be a positive int or None, got {limit!r}")
        if not self._path.exists():
            return ()
        sql = f"SELECT * FROM {SHADOW_TABLE} ORDER BY observed_at_utc ASC, shadow_id ASC"
        parameters: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (limit,)
        with _verified_read(self._path) as connection:
            rows = connection.execute(sql, parameters).fetchall()
            return tuple(self._restore(row) for row in rows)

    def get_by_execution_request_id(
        self, execution_request_id: str
    ) -> ShadowExecutionRecord | None:
        """The shadow record for one intent, or None. Missing DB -> None."""
        self._require_key(execution_request_id, "execution_request_id")
        return self._get_by("execution_request_id", execution_request_id)

    def get_by_signal_id(self, signal_id: str) -> ShadowExecutionRecord | None:
        """The shadow record for one source signal, or None. Missing DB -> None."""
        self._require_key(signal_id, "signal_id")
        return self._get_by("signal_id", signal_id)

    def _get_by(self, column: str, value: str) -> ShadowExecutionRecord | None:
        if not self._path.exists():
            return None
        with _verified_read(self._path) as connection:
            row = connection.execute(
                f"SELECT * FROM {SHADOW_TABLE} WHERE {column} = ?", (value,)
            ).fetchone()
            return None if row is None else self._restore(row)

    def _restore(self, row: sqlite3.Row) -> ShadowExecutionRecord:
        try:
            return _record_from_row(row)
        except (ValueError, TypeError) as exc:
            raise ShadowIntegrityError(
                f"shadow record {row['execution_request_id']!r} failed restore-time "
                f"validation: {exc}"
            ) from exc

    @staticmethod
    def _require_key(value, field_name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ShadowStoreError(
                f"{field_name} must be a non-empty string, got {value!r}"
            )
