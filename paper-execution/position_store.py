"""SQLite storage for Paper Execution virtual positions (Issue #49/#50).

*** PAPER EXECUTION. NOT A LIVE ORDER / BROKER LAYER. ***

Independent DB file (``paper-execution/data/paper_positions.sqlite3``), separate
from the Paper Signal and Backtest DBs, so standing up Paper Execution never
requires touching them.

Guarantees implemented here (Issue #49):

- **State x terminal_reason closed set** is enforced by a DB CHECK constraint
  (mirroring ``position_models.STATE_TERMINAL_REASONS``) AND validated at the
  application layer before every write -- both layers, on purpose.
- **No re-transition from a terminal state**: every transition is a
  compare-and-set (CAS) UPDATE guarded on the *expected source state*
  (``WHERE state = 'pending'`` / ``'open'``). A terminal row matches zero rows,
  so it can never be re-opened or re-closed.
- **Open idempotency**: ``signal_id`` is UNIQUE and ``create_position`` uses
  ``INSERT ... ON CONFLICT(signal_id) DO NOTHING`` -- a second import of the same
  signal is a no-op, never a duplicate position.
- **Watermark CAS**: closing/advancing a position guards on the *expected
  current watermark* (``last_evaluated_candle_utc IS :expected``), so two
  concurrent evaluators of the same open position cannot both transition it --
  the loser matches zero rows.

Read helpers never create the DB file (dry-run safe) and deliberately do NOT
swallow a ``sqlite3.OperationalError`` on an existing file into an empty result
(Issue #49 runner contract: a real read failure must surface, not masquerade as
"no data"). Only a *missing* DB file returns the empty/None default.

Issue #83 adds the ``paper_execution_notifications`` table to the same DB as a
**transactional outbox** for Discord notifications, keyed by a deterministic
``event_key`` (``position_id:event_type``, UNIQUE):

- **Atomic transition + enqueue**: the ``*_with_notification`` APIs commit the
  position state change AND the outbox row in ONE ``BEGIN IMMEDIATE``
  transaction. A position transition can never exist without its outbox row
  (no crash window between them); an enqueue failure rolls the position change
  back too; a CAS loss enqueues nothing.
- **Atomic delivery claim**: :func:`claim_notifications` moves rows to
  ``processing`` with a ``claimed_by`` / ``claimed_until_utc`` lease inside one
  ``BEGIN IMMEDIATE`` transaction, so two concurrent runners can never deliver
  the same row at once; expired leases (crashed worker) become claimable again.
- **Bounded retry / dead-letter**: :func:`finish_notification_delivery` settles
  an attempt; at ``max_attempts`` a failing row becomes ``dead_letter`` (never
  auto-retried, kept visible for monitoring; see :func:`requeue_notification`).
- The HTTP delivery itself happens AFTER the outbox commit, in the notifier --
  a delivery failure never rolls back a position state change.
"""

from __future__ import annotations

import math
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
DEFAULT_DB_PATH = BASE_DIR / "data" / "paper_positions.sqlite3"
BUSY_TIMEOUT_SECONDS = 30

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import position_models as models  # noqa: E402

POSITIONS_TABLE = "virtual_positions"
NOTIFICATIONS_TABLE = "paper_execution_notifications"

# --- notification event vocabulary (Issue #83, closed set) -------------------
# One event per observable transition. `pending` creation is deliberately NOT
# an event (out of the Issue #83 scope).
NOTIFY_EVENT_OPEN = "open"            # pending -> open (virtual fill)
NOTIFY_EVENT_CLOSED = "closed"        # open -> closed (SL / TP / max hold)
NOTIFY_EVENT_EXPIRED = "expired"      # pending -> expired (entry timeout)
NOTIFY_EVENT_CANCELLED = "cancelled"  # invalid signal -> cancelled
KNOWN_NOTIFY_EVENTS: frozenset[str] = frozenset(
    {NOTIFY_EVENT_OPEN, NOTIFY_EVENT_CLOSED, NOTIFY_EVENT_EXPIRED, NOTIFY_EVENT_CANCELLED}
)

DELIVERY_STATUS_PENDING = "pending"          # enqueued, not yet attempted
DELIVERY_STATUS_PROCESSING = "processing"    # claimed by a worker (lease: claimed_until_utc)
DELIVERY_STATUS_SUCCESS = "success"          # outbox row settled; never re-claimed
DELIVERY_STATUS_FAILED = "failed"            # last attempt failed; retryable until the attempt cap
DELIVERY_STATUS_DEAD_LETTER = "dead_letter"  # attempt cap reached; kept visible, never auto-retried
KNOWN_DELIVERY_STATUSES: frozenset[str] = frozenset(
    {
        DELIVERY_STATUS_PENDING,
        DELIVERY_STATUS_PROCESSING,
        DELIVERY_STATUS_SUCCESS,
        DELIVERY_STATUS_FAILED,
        DELIVERY_STATUS_DEAD_LETTER,
    }
)

_NOTIFICATION_COLUMNS: tuple[str, ...] = (
    "event_key",
    "position_id",
    "event_type",
    "delivery_status",
    "attempt_count",
    "last_error",
    "claimed_by",
    "claimed_until_utc",
    "notified_at_utc",
    "created_at_utc",
    "updated_at_utc",
)
REQUIRED_NOTIFICATION_COLUMNS: frozenset[str] = frozenset(_NOTIFICATION_COLUMNS)
# Columns that must be NOT NULL in the physical schema (event_key is covered by
# its PRIMARY KEY). Verified structurally by _notifications_table_object_current.
_NOTIFICATION_NOT_NULL_COLUMNS: frozenset[str] = frozenset(
    {"position_id", "event_type", "delivery_status", "attempt_count", "created_at_utc", "updated_at_utc"}
)


def notification_event_key(position_id: str, event_type: str) -> str:
    """Deterministic dedup key for one notification event: ``position_id:event_type``.

    The state machine allows each transition at most once per position, so this
    key is unique per real-world event; the DB enforces it with a UNIQUE
    constraint (PRIMARY KEY) as the second guard.
    """
    if not position_id:
        raise ValueError("position_id must be non-empty for a notification event key")
    if event_type not in KNOWN_NOTIFY_EVENTS:
        raise ValueError(f"event_type must be one of {sorted(KNOWN_NOTIFY_EVENTS)}, got {event_type!r}")
    return f"{position_id}:{event_type}"

# Column order used by both the INSERT and the row tuple builder.
_ALL_COLUMNS: tuple[str, ...] = (
    "position_id",
    "signal_id",
    "symbol",
    "timeframe",
    "side",
    "strategy_profile",
    "execution_profile",
    "state",
    "terminal_reason",
    "signal_candle_utc",
    "entry_deadline_utc",
    "entry_price",
    "entry_time_utc",
    "stop_loss_price",
    "take_profit_price",
    "max_hold_deadline_utc",
    "close_price",
    "close_time_utc",
    "gross_pnl",
    "net_pnl",
    "pnl",
    "r_multiple",
    "size",
    "max_entry_delay_bars",
    "max_hold_bars",
    "same_candle_priority",
    "fallback_stop_pct",
    "risk_reward",
    "spread",
    "slippage",
    "commission",
    "last_evaluated_candle_utc",
    "created_at_utc",
    "updated_at_utc",
)

# Bumped whenever the physical schema/constraints change. Stored in the DB via
# ``PRAGMA user_version`` and used as the fast-path "already current" marker; the
# migration itself still inspects the real schema (PRAGMA table_info) rather than
# trusting CREATE TABLE IF NOT EXISTS.
# v1: constrained virtual_positions.
# v2 (Issue #83 prototype): + paper_execution_notifications (no claim columns).
# v3 (Issue #83 review): outbox claim/lease columns (claimed_by, claimed_until_utc)
#     and the processing / dead_letter delivery statuses.
# v4 (Issue #85): `position_limit` added to the cancelled terminal_reason closed
#     set. The state x terminal_reason CHECK changes, so the positions table
#     itself must be rebuilt: POSITIONS_SCHEMA_VERSION is raised to v4 so a v3
#     DB (old CHECK) is migrated via the transactional table rebuild.
POSITIONS_SCHEMA_VERSION = 4
CURRENT_SCHEMA_VERSION = 4

REQUIRED_COLUMNS: frozenset[str] = frozenset(_ALL_COLUMNS)

# Documented backfill defaults for columns a legacy row may lack (Issue #50/#51
# migration). MISSING columns get these safe defaults; INVALID existing values
# (bad state/side/reason, non-finite numbers, NULL identity) are never silently
# rounded -- they fail the migration explicitly (see _migrate_legacy_row).
_MIGRATION_DEFAULTS: dict[str, object] = {
    "execution_profile": "default",
    "size": 1.0,
    "max_entry_delay_bars": 3,
    "max_hold_bars": 24,
    "same_candle_priority": "stop_loss",
    "fallback_stop_pct": 0.003,
    "risk_reward": 2.0,
    "spread": 0.0,
    "slippage": 0.0,
    "commission": 0.0,
}


class PositionStoreError(RuntimeError):
    """Base class for Paper Execution store errors."""


class PositionSchemaError(PositionStoreError):
    """A pre-existing table is missing required columns."""


class PositionMigrationError(PositionStoreError):
    """A legacy schema could not be safely migrated to the current constrained one.

    Raised (with a full rollback) when a legacy row cannot be converted without
    guessing at invalid data -- an unknown ``state``/``side``, an invalid
    ``state`` x ``terminal_reason`` combination, a non-finite number, a NULL in a
    required identity column, or a duplicate ``signal_id``. The migration fails
    loudly rather than silently rounding bad data to a "valid" value.
    """


class PositionNotFoundError(PositionStoreError):
    """No virtual position exists for the given id."""


class PositionConcurrentModificationError(PositionStoreError):
    """A guarded (CAS) transition matched zero rows: the source state / watermark
    changed under us (already transitioned, or a concurrent evaluator won)."""


def _state_check_sql() -> str:
    states = "', '".join(sorted(models.KNOWN_STATES))
    return f"state IN ('{states}')"


def _terminal_reason_check_sql() -> str:
    """Build the state x terminal_reason CHECK from the model's closed set."""
    non_terminal = "', '".join(sorted(models.NON_TERMINAL_STATES))
    clauses = [f"(state IN ('{non_terminal}') AND terminal_reason IS NULL)"]
    for state in sorted(models.STATE_TERMINAL_REASONS):
        reasons = "', '".join(sorted(models.STATE_TERMINAL_REASONS[state]))
        clauses.append(f"(state = '{state}' AND terminal_reason IN ('{reasons}'))")
    return " OR ".join(clauses)


def load_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def position_db_path(env_values: dict[str, str] | None = None) -> Path:
    values = env_values if env_values is not None else load_env_file()
    raw = (os.getenv("PAPER_POSITION_DB_PATH") or values.get("PAPER_POSITION_DB_PATH") or str(DEFAULT_DB_PATH)).strip()
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_SECONDS)
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}")
    return connection


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).fetchone()
    return row is not None


def _create_table_sql(table_name: str) -> str:
    """The full current schema (constraints included) for ``table_name``."""
    return f"""
        CREATE TABLE {table_name} (
            position_id TEXT PRIMARY KEY,
            signal_id TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            side TEXT NOT NULL,
            strategy_profile TEXT NOT NULL,
            execution_profile TEXT NOT NULL,
            state TEXT NOT NULL CHECK ({_state_check_sql()}),
            terminal_reason TEXT CHECK ({_terminal_reason_check_sql()}),
            signal_candle_utc TEXT NOT NULL,
            entry_deadline_utc TEXT NOT NULL,
            entry_price REAL,
            entry_time_utc TEXT,
            stop_loss_price REAL,
            take_profit_price REAL,
            max_hold_deadline_utc TEXT,
            close_price REAL,
            close_time_utc TEXT,
            gross_pnl REAL,
            net_pnl REAL,
            pnl REAL,
            r_multiple REAL,
            size REAL NOT NULL,
            max_entry_delay_bars INTEGER NOT NULL,
            max_hold_bars INTEGER NOT NULL,
            same_candle_priority TEXT NOT NULL CHECK (same_candle_priority IN ('stop_loss', 'take_profit')),
            fallback_stop_pct REAL NOT NULL,
            risk_reward REAL NOT NULL,
            spread REAL NOT NULL,
            slippage REAL NOT NULL,
            commission REAL NOT NULL,
            last_evaluated_candle_utc TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        )
    """


def _notify_events_check_sql() -> str:
    events = "', '".join(sorted(KNOWN_NOTIFY_EVENTS))
    return f"event_type IN ('{events}')"


def _delivery_statuses_check_sql() -> str:
    statuses = "', '".join(sorted(KNOWN_DELIVERY_STATUSES))
    return f"delivery_status IN ('{statuses}')"


def _create_notifications_table_sql(table_name: str) -> str:
    """The Issue #83 outbox schema (event_key UNIQUE via PRIMARY KEY, claim/lease columns)."""
    return f"""
        CREATE TABLE {table_name} (
            event_key TEXT PRIMARY KEY,
            position_id TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK ({_notify_events_check_sql()}),
            delivery_status TEXT NOT NULL CHECK ({_delivery_statuses_check_sql()}),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            claimed_by TEXT,
            claimed_until_utc TEXT,
            notified_at_utc TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        )
    """


_NOTIFICATIONS_STATUS_INDEX = f"idx_{NOTIFICATIONS_TABLE}_status"


def _normalized_sql(sql) -> str:
    return " ".join(str(sql or "").lower().split())


def _notifications_table_object_current(connection: sqlite3.Connection) -> bool:
    """Structurally verify the outbox table's REAL constraints, not just column names.

    Checks (all read-only): full column set; ``event_key`` is the PRIMARY KEY
    (``PRAGMA table_info`` pk flag -- PK implies UNIQUE + NOT NULL); the NOT NULL
    flags of every required column; and both CHECK constraints (event_type /
    delivery_status closed sets) via the stored ``sqlite_master.sql`` text.
    A table missing any of these is NOT current (it gets a transactional rebuild
    in init_db); a write path additionally behavior-probes the constraints
    (see :func:`_behavioral_probe_notifications`).
    """
    if not _table_exists(connection, NOTIFICATIONS_TABLE):
        return False
    info = {
        row[1]: row  # name -> (cid, name, type, notnull, dflt_value, pk)
        for row in connection.execute(f"PRAGMA table_info({NOTIFICATIONS_TABLE})").fetchall()
    }
    if set(info) != set(_NOTIFICATION_COLUMNS):
        return False
    if info["event_key"][5] != 1:  # not the (single-column) PRIMARY KEY
        return False
    for column in _NOTIFICATION_NOT_NULL_COLUMNS:
        if info[column][3] != 1:
            return False
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (NOTIFICATIONS_TABLE,)
    ).fetchone()
    table_sql = _normalized_sql(row[0] if row else "")
    for check in (_notify_events_check_sql(), _delivery_statuses_check_sql()):
        if f"check ({check.lower()})" not in table_sql:
            return False
    return True


def _notifications_index_present(connection: sqlite3.Connection) -> bool:
    """Verify the delivery-status index exists with the expected columns."""
    for row in connection.execute(f"PRAGMA index_list({NOTIFICATIONS_TABLE})").fetchall():
        if row[1] == _NOTIFICATIONS_STATUS_INDEX:
            columns = [r[2] for r in connection.execute(f"PRAGMA index_info({_NOTIFICATIONS_STATUS_INDEX})")]
            return columns == ["delivery_status", "created_at_utc"]
    return False


def _migrate_legacy_notification_row(legacy: dict, now: str) -> dict:
    """Map one legacy outbox row (e.g. the v2 prototype table) to the v3 shape.

    Preserves delivery history; unknown vocabulary or missing identity fails
    loudly (:class:`PositionMigrationError`) rather than guessing.
    """
    position_id = legacy.get("position_id")
    event_type = legacy.get("event_type")
    if not position_id or event_type not in KNOWN_NOTIFY_EVENTS:
        raise PositionMigrationError(
            f"legacy notification row {legacy.get('event_key')!r} has unusable "
            f"position_id={position_id!r} / event_type={event_type!r}; cannot migrate"
        )
    status = legacy.get("delivery_status") or DELIVERY_STATUS_PENDING
    if status not in KNOWN_DELIVERY_STATUSES:
        raise PositionMigrationError(
            f"legacy notification row {legacy.get('event_key')!r} has unknown delivery_status {status!r}"
        )
    try:
        attempts = int(legacy.get("attempt_count") or 0)
    except (TypeError, ValueError) as exc:
        raise PositionMigrationError(
            f"legacy notification row {legacy.get('event_key')!r} has non-integer attempt_count"
        ) from exc
    return {
        "event_key": legacy.get("event_key") or notification_event_key(position_id, event_type),
        "position_id": position_id,
        "event_type": event_type,
        "delivery_status": status,
        "attempt_count": attempts,
        "last_error": legacy.get("last_error"),
        "claimed_by": legacy.get("claimed_by"),
        "claimed_until_utc": legacy.get("claimed_until_utc"),
        "notified_at_utc": legacy.get("notified_at_utc"),
        "created_at_utc": legacy.get("created_at_utc") or now,
        "updated_at_utc": legacy.get("updated_at_utc") or now,
    }


def _migrate_notifications_table(connection: sqlite3.Connection) -> None:
    """Create, keep, or transactionally rebuild the outbox table to the canonical shape.

    - absent -> create with all constraints.
    - structurally current -> no-op.
    - anything else (v2 prototype without claim columns, constraint-less table,
      wrong NOT NULL/PK/CHECK) -> a true rebuild (CREATE new -> validated copy ->
      DROP -> RENAME) that PRESERVES existing rows; unmappable rows fail the
      migration loudly. Must run inside the caller's transaction (rolled back
      as one unit on failure).
    """
    if not _table_exists(connection, NOTIFICATIONS_TABLE):
        connection.execute(_create_notifications_table_sql(NOTIFICATIONS_TABLE))
        return
    if _notifications_table_object_current(connection):
        return
    temp_table = f"{NOTIFICATIONS_TABLE}__migrate_new"
    legacy_columns = [row[1] for row in connection.execute(f"PRAGMA table_info({NOTIFICATIONS_TABLE})")]
    legacy_rows = connection.execute(f"SELECT * FROM {NOTIFICATIONS_TABLE}").fetchall()
    connection.execute(f"DROP TABLE IF EXISTS {temp_table}")
    connection.execute(_create_notifications_table_sql(temp_table))
    insert_sql = (
        f"INSERT INTO {temp_table} ({', '.join(_NOTIFICATION_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _NOTIFICATION_COLUMNS)})"
    )
    now = models.utc_now_iso()
    for raw in legacy_rows:
        migrated = _migrate_legacy_notification_row(dict(zip(legacy_columns, raw)), now)
        try:
            connection.execute(insert_sql, tuple(migrated[col] for col in _NOTIFICATION_COLUMNS))
        except sqlite3.IntegrityError as exc:
            raise PositionMigrationError(
                f"legacy notification row {migrated.get('event_key')!r} violates the outbox schema: {exc}"
            ) from exc
    migrated_count = connection.execute(f"SELECT COUNT(*) FROM {temp_table}").fetchone()[0]
    if migrated_count != len(legacy_rows):
        raise PositionMigrationError(
            f"notification row-count mismatch during migration: legacy={len(legacy_rows)} migrated={migrated_count}"
        )
    connection.execute(f"DROP TABLE {NOTIFICATIONS_TABLE}")
    connection.execute(f"ALTER TABLE {temp_table} RENAME TO {NOTIFICATIONS_TABLE}")


def _create_indexes(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{POSITIONS_TABLE}_state "
        f"ON {POSITIONS_TABLE} (state, symbol, timeframe)"
    )
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS {_NOTIFICATIONS_STATUS_INDEX} "
        f"ON {NOTIFICATIONS_TABLE} (delivery_status, created_at_utc)"
    )


def _positions_schema_current(connection: sqlite3.Connection) -> bool:
    """True when ``virtual_positions`` itself needs no rebuild (v4 constraints present).

    ``user_version >= POSITIONS_SCHEMA_VERSION`` distinguishes a properly
    constrained current table from (a) a constraint-less legacy table that
    merely has all columns (``user_version = 0``) and (b) a v1..v3 table whose
    terminal_reason CHECK predates ``position_limit`` (Issue #85) -- both must
    be rebuilt.
    """
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version < POSITIONS_SCHEMA_VERSION:
        return False
    return not (REQUIRED_COLUMNS - _table_columns(connection, POSITIONS_TABLE))


def _schema_is_current(connection: sqlite3.Connection) -> bool:
    """True only when the DB reports the current version AND every table's REAL
    schema is current (positions columns; notifications constraints + index).

    Deliberately checks the real schema (``PRAGMA user_version`` + ``PRAGMA
    table_info`` + ``sqlite_master.sql`` + ``PRAGMA index_list``), never "the
    table exists" -- a constraint-less table is never accepted as current, even
    with a forged ``user_version``.
    """
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version < CURRENT_SCHEMA_VERSION:
        return False
    if REQUIRED_COLUMNS - _table_columns(connection, POSITIONS_TABLE):
        return False
    return _notifications_table_object_current(connection) and _notifications_index_present(connection)


def init_db(db_path: Path | None = None) -> None:
    """Create, or migrate to, the current constrained ``virtual_positions`` schema.

    - No table -> create it with all CHECK / UNIQUE / NOT NULL constraints and
      stamp ``PRAGMA user_version``.
    - Table present but not current (older ``user_version`` or missing columns) ->
      rebuild it (a real table rebuild that actually ADDS the constraints; see
      :func:`_migrate_rebuild`), inside one transaction with full rollback on any
      failure, and fail loudly on legacy data that cannot be safely converted.
    - Table already current -> no-op (indexes ensured).

    Idempotent and safe to call repeatedly -- including CONCURRENTLY (Issue #85
    review): the "table absent" decision is re-checked INSIDE the
    ``BEGIN IMMEDIATE`` transaction, because two workers starting against a
    missing DB can both observe "absent" before either creates it; the loser
    would then wait on the write lock and fail its CREATE TABLE with "table
    already exists". Re-checking under the write lock makes the loser fall
    through to the normal already-current path instead of erroring.
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Autocommit mode (isolation_level=None) so DDL participates in the explicit
    # BEGIN..COMMIT below -- otherwise sqlite3 can implicitly commit around DDL and
    # break the rebuild's atomicity.
    connection = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    try:
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}")
        if not _table_exists(connection, POSITIONS_TABLE):
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not _table_exists(connection, POSITIONS_TABLE):  # re-check under the write lock
                    connection.execute(_create_table_sql(POSITIONS_TABLE))
                    _migrate_notifications_table(connection)
                    _create_indexes(connection)
                    connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
                    connection.execute("COMMIT")
                    return
                # a concurrent worker created the schema while we waited: nothing
                # to create -- fall through to the normal current/migration paths.
                connection.execute("ROLLBACK")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if _schema_is_current(connection):
            _create_indexes(connection)
            return
        if _positions_schema_current(connection):
            # Notifications-only migration (Issue #83): positions are already fully
            # constrained; only the outbox table needs creating (v1), rebuilding
            # (v2 prototype / constraint-less table -- rows preserved), or the
            # index / version stamp is missing. One transaction, full rollback on
            # failure -- position rows are never rebuilt or touched here.
            connection.execute("BEGIN IMMEDIATE")
            try:
                _migrate_notifications_table(connection)
                _create_indexes(connection)
                connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            return
        _migrate_rebuild(connection)
    finally:
        connection.close()


_MISSING = object()


def _migrate_legacy_row(legacy: dict) -> dict:
    """Convert one legacy row to the current schema, or fail explicitly.

    MISSING columns are backfilled with documented defaults (see
    ``_MIGRATION_DEFAULTS``); INVALID existing data is never silently rounded --
    an unknown state/side, an invalid state x terminal_reason combination, or a
    non-finite number raises :class:`PositionMigrationError`.
    """
    position_id = legacy.get("position_id")
    signal_id = legacy.get("signal_id")
    if not position_id or not signal_id:
        raise PositionMigrationError("legacy row missing position_id/signal_id (cannot migrate)")

    state = legacy.get("state")
    if state not in models.KNOWN_STATES:
        raise PositionMigrationError(f"legacy row {position_id!r} has unknown state {state!r}")
    terminal_reason = legacy.get("terminal_reason")
    try:
        models.validate_state_reason(state, terminal_reason)
    except ValueError as exc:
        raise PositionMigrationError(f"legacy row {position_id!r}: {exc}") from exc

    side = legacy.get("side")
    if side not in models.KNOWN_SIDES and not (
        state == models.STATE_CANCELLED and (side is None or side == "")
    ):
        raise PositionMigrationError(f"legacy row {position_id!r} has invalid side {side!r}")

    def value(col):
        if col in legacy and legacy[col] is not None:
            return legacy[col]
        return _MIGRATION_DEFAULTS.get(col)

    record = {col: value(col) for col in _ALL_COLUMNS}
    record["position_id"] = position_id
    record["signal_id"] = signal_id
    record["state"] = state
    record["terminal_reason"] = terminal_reason
    record["side"] = side if side is not None else ""
    for text_col in ("symbol", "timeframe", "strategy_profile", "signal_candle_utc", "entry_deadline_utc"):
        if record[text_col] is None:
            record[text_col] = ""
    now = models.utc_now_iso()
    record["created_at_utc"] = record["created_at_utc"] or now
    record["updated_at_utc"] = record["updated_at_utc"] or now
    # Preserve the pnl == net_pnl invariant across the migration.
    if record["net_pnl"] is None and record["pnl"] is not None:
        record["net_pnl"] = record["pnl"]
    record["pnl"] = record["net_pnl"]

    for numeric_col in (
        "entry_price", "stop_loss_price", "take_profit_price", "close_price",
        "gross_pnl", "net_pnl", "pnl", "r_multiple", "size", "max_entry_delay_bars",
        "max_hold_bars", "fallback_stop_pct", "risk_reward", "spread", "slippage", "commission",
    ):
        raw = record.get(numeric_col)
        if raw is None:
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise PositionMigrationError(f"legacy row {position_id!r} has non-numeric {numeric_col}={raw!r}") from exc
        if not math.isfinite(number):
            raise PositionMigrationError(f"legacy row {position_id!r} has non-finite {numeric_col}={raw!r}")
    return record


def _migrate_rebuild(connection: sqlite3.Connection) -> None:
    """Rebuild ``virtual_positions`` into the fully-constrained current schema.

    A true table rebuild (CREATE new -> validated copy -> DROP old -> RENAME),
    because ``CREATE TABLE IF NOT EXISTS`` / ``ALTER TABLE ADD COLUMN`` cannot add
    CHECK / UNIQUE / NOT NULL to an existing table. All in one transaction; any
    failure rolls the whole thing back, leaving the legacy table untouched.
    """
    temp_table = f"{POSITIONS_TABLE}__migrate_new"
    connection.execute("BEGIN IMMEDIATE")
    try:
        legacy_columns = [row[1] for row in connection.execute(f"PRAGMA table_info({POSITIONS_TABLE})")]
        legacy_rows = connection.execute(f"SELECT * FROM {POSITIONS_TABLE}").fetchall()
        legacy_dicts = [dict(zip(legacy_columns, row)) for row in legacy_rows]

        connection.execute(f"DROP TABLE IF EXISTS {temp_table}")
        connection.execute(_create_table_sql(temp_table))
        insert_sql = (
            f"INSERT INTO {temp_table} ({', '.join(_ALL_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _ALL_COLUMNS)})"
        )
        for legacy in legacy_dicts:
            migrated = _migrate_legacy_row(legacy)
            try:
                connection.execute(insert_sql, tuple(migrated.get(col) for col in _ALL_COLUMNS))
            except sqlite3.IntegrityError as exc:
                # e.g. duplicate signal_id, a CHECK the app-level validation didn't
                # pre-catch, or a NULL in a NOT NULL column -- fail, don't guess.
                raise PositionMigrationError(
                    f"legacy row {legacy.get('position_id')!r} violates the new schema: {exc}"
                ) from exc

        migrated_count = connection.execute(f"SELECT COUNT(*) FROM {temp_table}").fetchone()[0]
        if migrated_count != len(legacy_dicts):
            raise PositionMigrationError(
                f"row-count mismatch during migration: legacy={len(legacy_dicts)} migrated={migrated_count}"
            )

        connection.execute(f"DROP TABLE {POSITIONS_TABLE}")
        connection.execute(f"ALTER TABLE {temp_table} RENAME TO {POSITIONS_TABLE}")
        _migrate_notifications_table(connection)
        _create_indexes(connection)
        connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


# --- schema status / startup gate (Issue #49 runner contract) ----------------
#
# The runner MUST settle migration up front, before reading any position, so a
# legacy DB is never read as the new model. These give it an explicit,
# non-implicit way to do that: a READ-ONLY status probe (for dry-run) and a
# WRITE migrate-and-verify (for a normal run).
SCHEMA_STATUS_MISSING = "missing"    # no DB file yet
SCHEMA_STATUS_CURRENT = "current"    # table present, current version + all columns
SCHEMA_STATUS_LEGACY = "legacy"      # table present but older/incomplete -> needs migration
SCHEMA_STATUS_INVALID = "invalid"    # file present but not a usable positions DB


def _classify_schema(db_path: Path) -> str:
    """Classify one on-disk DB file (see :func:`get_schema_status` for the vocabulary).

    Only ever called on a private SNAPSHOT COPY of the real DB, so a plain
    read-write connection here is safe -- checkpoints or sidecar churn hit the
    throwaway copy, never the original.
    """
    try:
        connection = sqlite3.connect(db_path)
    except sqlite3.DatabaseError:
        return SCHEMA_STATUS_INVALID
    try:
        # Reading sqlite_master forces SQLite to validate the file header; a
        # non-SQLite/corrupt file raises sqlite3.DatabaseError here.
        if not _table_exists(connection, POSITIONS_TABLE):
            return SCHEMA_STATUS_LEGACY  # valid SQLite, our table not created yet
        current = _schema_is_current(connection)
    except sqlite3.DatabaseError:
        return SCHEMA_STATUS_INVALID
    finally:
        connection.close()
    return SCHEMA_STATUS_CURRENT if current else SCHEMA_STATUS_LEGACY


def snapshot_db_for_read(db_path: Path, directory: Path) -> Path:
    """Copy the DB file plus any ``-wal`` / ``-shm`` sidecars into *directory*.

    Returns the snapshot's path. Reading the snapshot instead of the real file
    is what keeps a dry run byte-invariant even for WAL DBs: SQLite readers
    create/modify the ``-shm`` sidecar and can checkpoint the WAL, so the real
    file must never be opened at all on a dry run.
    """
    snapshot = Path(directory) / (db_path.name or "positions.sqlite3")
    shutil.copy2(db_path, snapshot)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, Path(str(snapshot) + suffix))
    return snapshot


def get_schema_status(db_path: Path | None = None) -> str:
    """Return the position DB's schema status WITHOUT touching the file AT ALL.

    One of ``missing`` / ``current`` / ``legacy`` / ``invalid``.
    ``validate_schema_read_only`` is an alias for callers that want the intent
    spelled out.

    **Snapshot probe (Issue #83 review):** SQLite is NEVER pointed at the real
    file here -- not even read-only, because a plain connection can checkpoint a
    WAL DB (rewriting the main file and deleting ``-wal``), and even a
    ``mode=ro`` URI connection can create/modify the ``-shm`` sidecar of a WAL
    DB. Instead the DB file plus any ``-wal`` / ``-shm`` sidecars are copied to
    a private temp directory and the COPY is probed. Guarantee: the real DB,
    ``-wal`` and ``-shm`` keep their exact bytes, size and mtime across a probe
    (dry-run contract). A copy torn by a concurrent writer can classify as
    ``invalid`` for that probe -- a diagnostic false negative, never a write.

    Classification:
      * ``missing``  -- no file yet.
      * ``invalid``  -- the file exists but is NOT a usable SQLite database
                        (detected via ``sqlite3.DatabaseError`` on the snapshot;
                        the narrow SQLite family only, so genuine programming
                        bugs still surface).
      * ``legacy``   -- a valid SQLite DB needing migration: tables absent, an
                        older version (v1 / the v2 outbox prototype), missing
                        columns, or missing REAL constraints (PK / NOT NULL /
                        CHECK / index) on the notifications table. A normal
                        (non-dry) run resolves this.
      * ``current``  -- every table present at the current version with its full
                        physical schema (constraints verified, not assumed).
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    if not db_path.exists():
        return SCHEMA_STATUS_MISSING
    # OSError while snapshotting (permissions, disk full) propagates: it is an
    # environment failure, not a schema state -- callers count it as a failure.
    with tempfile.TemporaryDirectory(prefix="paper_exec_probe_") as tmp_dir:
        return _classify_schema(snapshot_db_for_read(db_path, Path(tmp_dir)))


# Explicit alias: dry-run schema validation is a READ-ONLY status probe.
validate_schema_read_only = get_schema_status


def ensure_schema_migrated(db_path: Path | None = None) -> None:
    """Fresh-create or migrate the DB, then VERIFY the result is current.

    The write path the runner calls once at cycle start (non-dry-run) before any
    position read. Runs :func:`init_db` (create/rebuild) and then re-checks the
    real schema; if it is not ``current`` afterwards, raises
    :class:`PositionSchemaError` so the caller can abort the cycle rather than
    read a half-migrated table. ``init_db`` itself rolls back a failed rebuild,
    so this never leaves a partial migration behind.
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    # Refuse an invalid (non-SQLite / corrupt) file BEFORE init_db so we never
    # overwrite it or recreate it as a fresh DB -- "not a SQLite file" is a hard
    # error, not a fresh-DB case.
    if get_schema_status(db_path) == SCHEMA_STATUS_INVALID:
        raise PositionSchemaError(
            f"position DB at {db_path} is not a valid SQLite positions DB (corrupt / not a database); "
            "refusing to overwrite or recreate it"
        )
    init_db(db_path)
    status = get_schema_status(db_path)
    if status != SCHEMA_STATUS_CURRENT:
        raise PositionSchemaError(
            f"position DB schema is {status!r} after migration at {db_path}; refusing to proceed"
        )
    # Belt and braces (Issue #83 review): after the structural verification,
    # prove the outbox constraints actually FIRE with a SAVEPOINT-rolled-back
    # behavioral probe. Write path only -- never runs on a dry run.
    _behavioral_probe_notifications(db_path)


def _behavioral_probe_notifications(db_path: Path) -> None:
    """Behaviorally verify the outbox constraints inside a rolled-back SAVEPOINT.

    Probes that (a) an unknown ``event_type`` and (b) an unknown
    ``delivery_status`` are rejected by their CHECKs, (c) a NULL ``position_id``
    is rejected by NOT NULL, and (d) a duplicate ``event_key`` is rejected by
    the PRIMARY KEY. Everything happens inside ``SAVEPOINT`` + ``ROLLBACK TO``
    within a transaction that is itself rolled back -- no probe row can ever be
    committed. Raises :class:`PositionSchemaError` when a constraint fails to fire.
    """
    now = models.utc_now_iso()
    probe_key = "__schema_probe__:open"
    base = (probe_key, "__schema_probe__", NOTIFY_EVENT_OPEN, DELIVERY_STATUS_PENDING, 0, None, None, None, None, now, now)
    insert_sql = (
        f"INSERT INTO {NOTIFICATIONS_TABLE} ({', '.join(_NOTIFICATION_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _NOTIFICATION_COLUMNS)})"
    )

    connection = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    not_enforced: list[str] = []
    try:
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("SAVEPOINT schema_probe")

            def expect_rejected(params: tuple, label: str) -> None:
                try:
                    connection.execute(insert_sql, params)
                except sqlite3.IntegrityError:
                    return
                not_enforced.append(label)

            expect_rejected(base[:2] + ("__invalid_event__",) + base[3:], "event_type CHECK")
            expect_rejected(base[:3] + ("__invalid_status__",) + base[4:], "delivery_status CHECK")
            expect_rejected((probe_key, None) + base[2:], "position_id NOT NULL")
            try:
                connection.execute(insert_sql, base)  # valid probe row must insert...
                expect_rejected(base, "event_key PRIMARY KEY")  # ...and its duplicate must not
            except sqlite3.IntegrityError:
                not_enforced.append("valid probe row rejected (schema unusable)")
            connection.execute("ROLLBACK TO schema_probe")
            connection.execute("RELEASE SAVEPOINT schema_probe")
        finally:
            connection.execute("ROLLBACK")  # discard the probe transaction entirely
    finally:
        connection.close()
    if not_enforced:
        raise PositionSchemaError(
            f"notifications table constraints not actually enforced at {db_path}: "
            f"{', '.join(not_enforced)}; refusing to proceed"
        )


def _row_tuple(record: dict) -> tuple:
    return tuple(record.get(column) for column in _ALL_COLUMNS)


_INSERT_SQL = (
    f"INSERT INTO {POSITIONS_TABLE} ({', '.join(_ALL_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _ALL_COLUMNS)}) "
    "ON CONFLICT(signal_id) DO NOTHING"
)


def create_position(record: dict, *, db_path: Path | None = None) -> bool:
    """Insert a ``pending`` (or terminal ``cancelled``) position, idempotently.

    Returns True if a row was inserted, False if a position for this ``signal_id``
    already existed (``ON CONFLICT DO NOTHING`` -- the Issue #49 "1 signal = 1
    position" rule). Validates the ``state`` x ``terminal_reason`` combination at
    the application layer before writing (the DB CHECK is the second guard).
    """
    models.validate_state_reason(record["state"], record.get("terminal_reason"))
    db_path = Path(db_path) if db_path is not None else position_db_path()
    init_db(db_path)
    with _connect(db_path) as connection:
        cursor = connection.execute(_INSERT_SQL, _row_tuple(record))
    return cursor.rowcount > 0


# --- concurrent-position cap (Issue #85) --------------------------------------
# Capacity scope: (symbol, timeframe, side). Non-terminal (pending / open)
# positions count; terminal ones (closed / expired / cancelled) never do.

ACTIVE_STATES: tuple[str, str] = (models.STATE_PENDING, models.STATE_OPEN)

CAPACITY_RESULT_PENDING = "pending"        # intake accepted: pending row inserted
CAPACITY_RESULT_CANCELLED = "cancelled"    # cap reached: recorded as cancelled/position_limit
CAPACITY_RESULT_DUPLICATE = "duplicate"    # signal_id already tracked: nothing written

_COUNT_ACTIVE_SQL = (
    f"SELECT COUNT(*) FROM {POSITIONS_TABLE} "
    "WHERE symbol = ? AND timeframe = ? AND side = ? AND state IN (?, ?)"
)


def count_active_positions(symbol: str, timeframe: str, side: str, *, db_path: Path | None = None) -> int:
    """Count non-terminal (pending / open) positions in one ``(symbol, timeframe, side)`` scope.

    Read helper for the Issue #85 capacity decision preview (dry-run) and
    monitoring. Missing DB -> 0 (dry-run safe); an OperationalError on an
    existing file propagates (never masqueraded as "no data"). The
    authoritative, race-free check lives in
    :func:`create_position_capacity_guarded` -- never enforce the cap from this
    read alone.
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    if not db_path.exists():
        return 0
    with closing(sqlite3.connect(db_path)) as connection:  # see _read_one re: closing()
        row = connection.execute(_COUNT_ACTIVE_SQL, (symbol, timeframe, side, *ACTIVE_STATES)).fetchone()
    return int(row[0])


def create_position_capacity_guarded(
    record: dict, *, max_active_per_side: int | None, db_path: Path | None = None
) -> tuple[str, dict | None]:
    """Insert one intake record under the Issue #85 concurrent-position cap, atomically.

    ``record`` must be a ``pending`` position (the would-be intake). Everything
    happens inside ONE ``BEGIN IMMEDIATE`` transaction -- SQLite's single-writer
    lock strictly serializes concurrent runners, so the capacity check and the
    INSERT commit together and a check-then-insert (TOCTOU) race cannot
    overshoot the cap:

    1. ``signal_id`` already tracked (any state) -> ``("duplicate", None)``;
       nothing is written, no outbox row (idempotent re-import / re-run).
    2. the scope ``(symbol, timeframe, side)`` already holds
       ``max_active_per_side`` non-terminal (pending / open) positions -> the
       record is stored as a terminal ``cancelled`` position
       (``terminal_reason = position_limit``) TOGETHER with its ``cancelled``
       outbox row (Issue #83 atomicity) -> ``("cancelled", stored_record)``.
       A refused signal is recorded, never silently skipped, and its
       ``signal_id`` row makes the refusal itself idempotent.
    3. otherwise the ``pending`` row is inserted -> ``("pending", record)``
       (pending creation is deliberately NOT a notification event).

    ``max_active_per_side=None`` disables the cap (case 2 never happens);
    terminal positions never count toward it. Opposite-side positions live in
    their own scope and are never consulted here.
    """
    if record.get("state") != models.STATE_PENDING:
        raise ValueError(
            f"create_position_capacity_guarded requires state='pending', got {record.get('state')!r}"
        )
    models.validate_state_reason(record["state"], record.get("terminal_reason"))
    if max_active_per_side is not None and int(max_active_per_side) < 1:
        raise ValueError(f"max_active_per_side must be >= 1 or None, got {max_active_per_side!r}")
    db_path = Path(db_path) if db_path is not None else position_db_path()
    init_db(db_path)
    now = models.utc_now_iso()
    with _immediate_txn(db_path) as connection:
        existing = connection.execute(
            f"SELECT 1 FROM {POSITIONS_TABLE} WHERE signal_id = ?", (record["signal_id"],)
        ).fetchone()
        if existing is not None:
            return CAPACITY_RESULT_DUPLICATE, None
        if max_active_per_side is not None:
            active = connection.execute(
                _COUNT_ACTIVE_SQL,
                (record["symbol"], record["timeframe"], record["side"], *ACTIVE_STATES),
            ).fetchone()[0]
            if active >= int(max_active_per_side):
                refused = {
                    **record,
                    "state": models.STATE_CANCELLED,
                    "terminal_reason": models.REASON_POSITION_LIMIT,
                }
                connection.execute(_INSERT_SQL, _row_tuple(refused))
                _enqueue_notification(connection, refused["position_id"], NOTIFY_EVENT_CANCELLED, now)
                return CAPACITY_RESULT_CANCELLED, refused
        connection.execute(_INSERT_SQL, _row_tuple(record))
        return CAPACITY_RESULT_PENDING, record


def transition_to_open(
    position_id: str,
    *,
    entry_price: float,
    entry_time_utc: str,
    stop_loss_price: float,
    take_profit_price: float,
    max_hold_deadline_utc: str,
    updated_at: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """Atomically move ``pending -> open``, pinning the entry fields (CAS on state).

    Guarded by ``WHERE state = 'pending'`` so a position that already opened,
    expired, or was cancelled matches zero rows and raises
    :class:`PositionConcurrentModificationError` -- never a second Open.
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    init_db(db_path)
    now = updated_at or models.utc_now_iso()
    with _connect(db_path) as connection:
        cursor = connection.execute(
            f"""
            UPDATE {POSITIONS_TABLE}
            SET state = ?, entry_price = ?, entry_time_utc = ?, stop_loss_price = ?,
                take_profit_price = ?, max_hold_deadline_utc = ?, updated_at_utc = ?
            WHERE position_id = ? AND state = ?
            """,
            (
                models.STATE_OPEN, float(entry_price), entry_time_utc, float(stop_loss_price),
                float(take_profit_price), max_hold_deadline_utc, now,
                position_id, models.STATE_PENDING,
            ),
        )
        _require_cas(connection, cursor, position_id, expected_state=models.STATE_PENDING)
    return _require_get(position_id, db_path=db_path)


def transition_pending_terminal(
    position_id: str,
    *,
    state: str,
    terminal_reason: str,
    updated_at: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """Atomically move ``pending -> expired|cancelled`` (CAS on state).

    Used for entry timeout (``expired``/``entry_timeout``) and manual/late
    cancellation (``cancelled``). Validated against the closed set first.
    """
    if state not in (models.STATE_EXPIRED, models.STATE_CANCELLED):
        raise ValueError(f"transition_pending_terminal only handles expired/cancelled, got {state!r}")
    models.validate_state_reason(state, terminal_reason)
    db_path = Path(db_path) if db_path is not None else position_db_path()
    init_db(db_path)
    now = updated_at or models.utc_now_iso()
    with _connect(db_path) as connection:
        cursor = connection.execute(
            f"""
            UPDATE {POSITIONS_TABLE}
            SET state = ?, terminal_reason = ?, updated_at_utc = ?
            WHERE position_id = ? AND state = ?
            """,
            (state, terminal_reason, now, position_id, models.STATE_PENDING),
        )
        _require_cas(connection, cursor, position_id, expected_state=models.STATE_PENDING)
    return _require_get(position_id, db_path=db_path)


def close_open_position(
    position_id: str,
    *,
    expected_watermark: str | None,
    new_watermark: str,
    close_price: float,
    close_time_utc: str,
    terminal_reason: str,
    gross_pnl: float,
    net_pnl: float,
    r_multiple: float | None,
    updated_at: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """Atomically move ``open -> closed`` AND advance the watermark, in one UPDATE.

    CAS-guarded on BOTH ``state = 'open'`` and ``last_evaluated_candle_utc IS
    :expected_watermark``: if the position already closed, or a concurrent
    evaluator advanced the watermark first, zero rows match and this raises
    :class:`PositionConcurrentModificationError`. The state change and the
    watermark advance commit together (never a half-applied close).
    """
    if terminal_reason not in models.STATE_TERMINAL_REASONS[models.STATE_CLOSED]:
        raise ValueError(
            f"close terminal_reason must be in {sorted(models.STATE_TERMINAL_REASONS[models.STATE_CLOSED])}, "
            f"got {terminal_reason!r}"
        )
    db_path = Path(db_path) if db_path is not None else position_db_path()
    init_db(db_path)
    now = updated_at or models.utc_now_iso()
    # pnl is the back-compat alias of net_pnl (kept identical on purpose).
    with _connect(db_path) as connection:
        cursor = connection.execute(
            f"""
            UPDATE {POSITIONS_TABLE}
            SET state = ?, close_price = ?, close_time_utc = ?, terminal_reason = ?,
                gross_pnl = ?, net_pnl = ?, pnl = ?, r_multiple = ?,
                last_evaluated_candle_utc = ?, updated_at_utc = ?
            WHERE position_id = ? AND state = ? AND last_evaluated_candle_utc IS ?
            """,
            (
                models.STATE_CLOSED, float(close_price), close_time_utc, terminal_reason,
                float(gross_pnl), float(net_pnl), float(net_pnl),
                None if r_multiple is None else float(r_multiple),
                new_watermark, now,
                position_id, models.STATE_OPEN, expected_watermark,
            ),
        )
        _require_cas(connection, cursor, position_id, expected_state=models.STATE_OPEN)
    return _require_get(position_id, db_path=db_path)


def advance_watermark(
    position_id: str,
    *,
    expected_watermark: str | None,
    new_watermark: str,
    updated_at: str | None = None,
    db_path: Path | None = None,
) -> bool:
    """Advance an open position's evaluated-candle watermark (CAS, no close).

    Guarded on ``state = 'open'`` and the expected current watermark. Returns
    True on success, False when the CAS matched zero rows (another evaluator
    advanced/closed it first) -- a benign no-op the caller can ignore.
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    init_db(db_path)
    now = updated_at or models.utc_now_iso()
    with _connect(db_path) as connection:
        cursor = connection.execute(
            f"""
            UPDATE {POSITIONS_TABLE}
            SET last_evaluated_candle_utc = ?, updated_at_utc = ?
            WHERE position_id = ? AND state = ? AND last_evaluated_candle_utc IS ?
            """,
            (new_watermark, now, position_id, models.STATE_OPEN, expected_watermark),
        )
    return cursor.rowcount > 0


def _require_cas(connection, cursor, position_id: str, *, expected_state: str) -> None:
    if cursor.rowcount != 0:
        return
    row = connection.execute(
        f"SELECT state FROM {POSITIONS_TABLE} WHERE position_id = ?", (position_id,)
    ).fetchone()
    if row is None:
        raise PositionNotFoundError(f"no virtual position with id {position_id!r}")
    raise PositionConcurrentModificationError(
        f"position {position_id!r} is {row[0]!r}, not {expected_state!r} (or watermark changed); "
        "refusing to re-transition"
    )


# --- read helpers -----------------------------------------------------------
# Missing DB file -> empty/None (dry-run safe). An OperationalError on an
# EXISTING file is NOT swallowed -- it propagates so the runner can fail loudly.


def _read_one(db_path: Path, sql: str, params: tuple) -> dict | None:
    if not db_path.exists():
        return None
    # closing(): the sqlite3 connection CONTEXT MANAGER only ends the
    # transaction, it does NOT close the connection. On Windows an unclosed
    # connection keeps the (snapshot) DB file handle open, so a dry-run's
    # TemporaryDirectory.cleanup() fails with WinError 32. closing() guarantees
    # the handle is released on every path, including exceptions.
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def _read_many(db_path: Path, sql: str, params: tuple) -> list[dict]:
    if not db_path.exists():
        return []
    with closing(sqlite3.connect(db_path)) as connection:  # see _read_one re: closing()
        connection.row_factory = sqlite3.Row
        rows = connection.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def get_position(position_id: str, *, db_path: Path | None = None) -> dict | None:
    db_path = Path(db_path) if db_path is not None else position_db_path()
    return _read_one(db_path, f"SELECT * FROM {POSITIONS_TABLE} WHERE position_id = ?", (position_id,))


def _require_get(position_id: str, *, db_path: Path) -> dict:
    row = get_position(position_id, db_path=db_path)
    if row is None:
        raise PositionNotFoundError(f"no virtual position with id {position_id!r}")
    return row


def get_position_by_signal(signal_id: str, *, db_path: Path | None = None) -> dict | None:
    db_path = Path(db_path) if db_path is not None else position_db_path()
    return _read_one(db_path, f"SELECT * FROM {POSITIONS_TABLE} WHERE signal_id = ?", (signal_id,))


def list_positions_by_state(state: str, *, db_path: Path | None = None) -> list[dict]:
    db_path = Path(db_path) if db_path is not None else position_db_path()
    return _read_many(
        db_path,
        f"SELECT * FROM {POSITIONS_TABLE} WHERE state = ? ORDER BY created_at_utc ASC",
        (state,),
    )


def list_positions(*, db_path: Path | None = None, limit: int = 1000) -> list[dict]:
    db_path = Path(db_path) if db_path is not None else position_db_path()
    return _read_many(
        db_path, f"SELECT * FROM {POSITIONS_TABLE} ORDER BY created_at_utc DESC LIMIT ?", (limit,)
    )


# --- transactional outbox (Issue #83) ----------------------------------------
# Position transition + notification enqueue commit as ONE transaction (the
# ``*_with_notification`` APIs); delivery claims rows atomically with a lease.
# Transaction boundaries:
#   * transition + enqueue      -> one BEGIN IMMEDIATE (atomic; both or neither)
#   * claim (SELECT + UPDATEs)  -> one BEGIN IMMEDIATE (single winner per row)
#   * finish / requeue          -> one BEGIN IMMEDIATE each
# The Discord HTTP call itself happens OUTSIDE any transaction, after the
# enqueue commit -- a delivery failure never rolls back a position change.


class NotificationNotFoundError(PositionStoreError):
    """No notification outbox row exists for the given event_key."""


class NotificationClaimError(PositionStoreError):
    """The delivery claim was lost: the row is no longer ``processing`` under this
    worker (its lease expired and another worker re-claimed it, or it was
    requeued). The caller must not update the new owner's state."""


@contextmanager
def _immediate_txn(db_path: Path):
    """One explicit ``BEGIN IMMEDIATE ... COMMIT`` transaction (ROLLBACK on error)."""
    connection = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    try:
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}")
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()


def _iso_plus_seconds(iso_utc: str, seconds: float) -> str:
    base = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    result = (base + timedelta(seconds=seconds)).astimezone(timezone.utc)
    return result.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _enqueue_notification(connection: sqlite3.Connection, position_id: str, event_type: str, now: str) -> None:
    """INSERT one ``pending`` outbox row inside the CALLER's transaction.

    ``ON CONFLICT(event_key) DO NOTHING``: a replayed transition (which the CAS
    guards already prevent) or a leftover row from an earlier schema can never
    produce a duplicate outbox row or clobber delivery state.
    """
    key = notification_event_key(position_id, event_type)
    connection.execute(
        f"""
        INSERT INTO {NOTIFICATIONS_TABLE}
            (event_key, position_id, event_type, delivery_status, attempt_count,
             last_error, claimed_by, claimed_until_utc, notified_at_utc, created_at_utc, updated_at_utc)
        VALUES (?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, ?, ?)
        ON CONFLICT(event_key) DO NOTHING
        """,
        (key, position_id, event_type, DELIVERY_STATUS_PENDING, now, now),
    )


# --- atomic transition + enqueue APIs -----------------------------------------
# The runner MUST use these for every notifiable transition (Issue #83 review
# Blocker): a position transition commits together with its outbox row, so a
# crash/SIGKILL at ANY point leaves either "no transition" or "transition +
# outbox row" -- never a transition whose notification is lost forever.
# Plain create_position / transition_* remain for non-notifiable writes
# (pending creation) and callers that explicitly do not want notifications.


def create_cancelled_position_with_notification(record: dict, *, db_path: Path | None = None) -> bool:
    """Insert a terminal ``cancelled`` position AND its outbox row atomically.

    Returns True when the position was inserted (and the event enqueued), False
    when a position for this ``signal_id`` already existed (idempotent skip --
    no outbox row is created for a duplicate). Only accepts ``cancelled``
    records: pending creation is deliberately NOT a notification event.
    """
    if record.get("state") != models.STATE_CANCELLED:
        raise ValueError(
            f"create_cancelled_position_with_notification requires state='cancelled', got {record.get('state')!r}"
        )
    models.validate_state_reason(record["state"], record.get("terminal_reason"))
    db_path = Path(db_path) if db_path is not None else position_db_path()
    init_db(db_path)
    now = models.utc_now_iso()
    with _immediate_txn(db_path) as connection:
        cursor = connection.execute(_INSERT_SQL, _row_tuple(record))
        if cursor.rowcount == 0:
            return False  # duplicate signal_id: no position, no outbox row
        _enqueue_notification(connection, record["position_id"], NOTIFY_EVENT_CANCELLED, now)
    return True


def transition_to_open_with_notification(
    position_id: str,
    *,
    entry_price: float,
    entry_time_utc: str,
    stop_loss_price: float,
    take_profit_price: float,
    max_hold_deadline_utc: str,
    updated_at: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """``pending -> open`` AND the ``open`` outbox row, in ONE transaction.

    Same CAS contract as :func:`transition_to_open`; a CAS loss raises and
    enqueues nothing (rolled back). An enqueue failure rolls the position
    transition back too -- never a transition without its outbox row.
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    init_db(db_path)
    now = updated_at or models.utc_now_iso()
    with _immediate_txn(db_path) as connection:
        cursor = connection.execute(
            f"""
            UPDATE {POSITIONS_TABLE}
            SET state = ?, entry_price = ?, entry_time_utc = ?, stop_loss_price = ?,
                take_profit_price = ?, max_hold_deadline_utc = ?, updated_at_utc = ?
            WHERE position_id = ? AND state = ?
            """,
            (
                models.STATE_OPEN, float(entry_price), entry_time_utc, float(stop_loss_price),
                float(take_profit_price), max_hold_deadline_utc, now,
                position_id, models.STATE_PENDING,
            ),
        )
        _require_cas(connection, cursor, position_id, expected_state=models.STATE_PENDING)
        _enqueue_notification(connection, position_id, NOTIFY_EVENT_OPEN, now)
    return _require_get(position_id, db_path=db_path)


def transition_pending_terminal_with_notification(
    position_id: str,
    *,
    state: str,
    terminal_reason: str,
    updated_at: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """``pending -> expired|cancelled`` AND the matching outbox row, in ONE transaction."""
    if state not in (models.STATE_EXPIRED, models.STATE_CANCELLED):
        raise ValueError(f"transition_pending_terminal only handles expired/cancelled, got {state!r}")
    models.validate_state_reason(state, terminal_reason)
    event_type = NOTIFY_EVENT_EXPIRED if state == models.STATE_EXPIRED else NOTIFY_EVENT_CANCELLED
    db_path = Path(db_path) if db_path is not None else position_db_path()
    init_db(db_path)
    now = updated_at or models.utc_now_iso()
    with _immediate_txn(db_path) as connection:
        cursor = connection.execute(
            f"""
            UPDATE {POSITIONS_TABLE}
            SET state = ?, terminal_reason = ?, updated_at_utc = ?
            WHERE position_id = ? AND state = ?
            """,
            (state, terminal_reason, now, position_id, models.STATE_PENDING),
        )
        _require_cas(connection, cursor, position_id, expected_state=models.STATE_PENDING)
        _enqueue_notification(connection, position_id, event_type, now)
    return _require_get(position_id, db_path=db_path)


def close_open_position_with_notification(
    position_id: str,
    *,
    expected_watermark: str | None,
    new_watermark: str,
    close_price: float,
    close_time_utc: str,
    terminal_reason: str,
    gross_pnl: float,
    net_pnl: float,
    r_multiple: float | None,
    updated_at: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """``open -> closed`` (+ watermark) AND the ``closed`` outbox row, in ONE transaction."""
    if terminal_reason not in models.STATE_TERMINAL_REASONS[models.STATE_CLOSED]:
        raise ValueError(
            f"close terminal_reason must be in {sorted(models.STATE_TERMINAL_REASONS[models.STATE_CLOSED])}, "
            f"got {terminal_reason!r}"
        )
    db_path = Path(db_path) if db_path is not None else position_db_path()
    init_db(db_path)
    now = updated_at or models.utc_now_iso()
    with _immediate_txn(db_path) as connection:
        cursor = connection.execute(
            f"""
            UPDATE {POSITIONS_TABLE}
            SET state = ?, close_price = ?, close_time_utc = ?, terminal_reason = ?,
                gross_pnl = ?, net_pnl = ?, pnl = ?, r_multiple = ?,
                last_evaluated_candle_utc = ?, updated_at_utc = ?
            WHERE position_id = ? AND state = ? AND last_evaluated_candle_utc IS ?
            """,
            (
                models.STATE_CLOSED, float(close_price), close_time_utc, terminal_reason,
                float(gross_pnl), float(net_pnl), float(net_pnl),
                None if r_multiple is None else float(r_multiple),
                new_watermark, now,
                position_id, models.STATE_OPEN, expected_watermark,
            ),
        )
        _require_cas(connection, cursor, position_id, expected_state=models.STATE_OPEN)
        _enqueue_notification(connection, position_id, NOTIFY_EVENT_CLOSED, now)
    return _require_get(position_id, db_path=db_path)


# --- outbox reads / delivery claim / settle -----------------------------------


def record_notification_event(
    position_id: str,
    event_type: str,
    *,
    created_at: str | None = None,
    db_path: Path | None = None,
) -> bool:
    """Enqueue-only helper (tests / manual ops): one ``pending`` outbox row.

    The runner never calls this directly -- transitions enqueue atomically via
    the ``*_with_notification`` APIs. Returns True if a new row was inserted,
    False when the event was already recorded (never clobbers delivery state).
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    init_db(db_path)
    now = created_at or models.utc_now_iso()
    with _immediate_txn(db_path) as connection:
        before = connection.total_changes
        _enqueue_notification(connection, position_id, event_type, now)
        inserted = connection.total_changes > before
    return inserted


def get_notification(event_key: str, *, db_path: Path | None = None) -> dict | None:
    db_path = Path(db_path) if db_path is not None else position_db_path()
    return _read_one(db_path, f"SELECT * FROM {NOTIFICATIONS_TABLE} WHERE event_key = ?", (event_key,))


def list_undelivered_notifications(*, db_path: Path | None = None) -> list[dict]:
    """Monitoring/test view of unsettled rows (``pending`` / ``processing`` / ``failed``).

    NOT the delivery path: delivery must go through :func:`claim_notifications`
    (atomic claim + lease) so concurrent workers cannot double-send.
    ``success`` and ``dead_letter`` rows are excluded.
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    return _read_many(
        db_path,
        f"SELECT * FROM {NOTIFICATIONS_TABLE} WHERE delivery_status NOT IN (?, ?) ORDER BY created_at_utc ASC",
        (DELIVERY_STATUS_SUCCESS, DELIVERY_STATUS_DEAD_LETTER),
    )


def count_notifications_by_status(*, db_path: Path | None = None) -> dict[str, int]:
    """``{delivery_status: row count}`` -- dead-letter monitoring etc. Missing DB -> {}."""
    db_path = Path(db_path) if db_path is not None else position_db_path()
    rows = _read_many(
        db_path,
        f"SELECT delivery_status, COUNT(*) AS n FROM {NOTIFICATIONS_TABLE} GROUP BY delivery_status",
        (),
    )
    return {row["delivery_status"]: row["n"] for row in rows}


def claim_notifications(
    *,
    worker_id: str,
    lease_seconds: float,
    batch_limit: int,
    max_attempts: int,
    now_utc: str | None = None,
    db_path: Path | None = None,
) -> list[dict]:
    """Atomically claim up to ``batch_limit`` deliverable outbox rows for one worker.

    One ``BEGIN IMMEDIATE`` transaction (SQLite single-writer): the candidate
    SELECT and every per-row UPDATE to ``processing`` commit together, so two
    concurrent workers can never claim -- and therefore never deliver -- the
    same row at once. Claimable rows are ``pending`` / ``failed`` under the
    attempt cap, plus ``processing`` rows whose lease (``claimed_until_utc``)
    has expired (a crashed worker's rows become deliverable again after the
    lease). ``success`` and ``dead_letter`` rows are never claimed.
    Returns the claimed rows (post-claim state), oldest first.

    Reconciliation (Issue #83 review): each claim transaction first normalizes
    any ``failed`` row already at/over ``max_attempts`` to ``dead_letter``. Such
    rows are otherwise unclaimable (claim requires ``attempt_count <
    max_attempts``) AND excluded from the dead-letter gauge, so without this
    they would be stuck forever -- not retried, not monitored. This runs against
    the LIVE ``max_attempts`` setting, so it reconciles v2-prototype rows that
    migrated as ``failed`` (no dead_letter status existed there) and any stale
    v3 rows, without baking a fixed cap into the migration. Lowering
    ``max_attempts`` later also demotes now-exhausted rows on the next cycle.
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    init_db(db_path)
    now = now_utc or models.utc_now_iso()
    lease_until = _iso_plus_seconds(now, lease_seconds)
    claimed: list[dict] = []
    with _immediate_txn(db_path) as connection:
        connection.execute(
            f"""
            UPDATE {NOTIFICATIONS_TABLE}
            SET delivery_status = ?, claimed_by = NULL, claimed_until_utc = NULL, updated_at_utc = ?
            WHERE delivery_status = ? AND attempt_count >= ?
            """,
            (DELIVERY_STATUS_DEAD_LETTER, now, DELIVERY_STATUS_FAILED, int(max_attempts)),
        )
        candidates = connection.execute(
            f"""
            SELECT * FROM {NOTIFICATIONS_TABLE}
            WHERE attempt_count < ?
              AND (
                delivery_status IN (?, ?)
                OR (delivery_status = ? AND (claimed_until_utc IS NULL OR claimed_until_utc <= ?))
              )
            ORDER BY created_at_utc ASC
            LIMIT ?
            """,
            (
                int(max_attempts),
                DELIVERY_STATUS_PENDING, DELIVERY_STATUS_FAILED,
                DELIVERY_STATUS_PROCESSING, now,
                int(batch_limit),
            ),
        ).fetchall()
        for row in candidates:
            connection.execute(
                f"""
                UPDATE {NOTIFICATIONS_TABLE}
                SET delivery_status = ?, claimed_by = ?, claimed_until_utc = ?, updated_at_utc = ?
                WHERE event_key = ?
                """,
                (DELIVERY_STATUS_PROCESSING, worker_id, lease_until, now, row["event_key"]),
            )
            claimed.append(
                {
                    **dict(row),
                    "delivery_status": DELIVERY_STATUS_PROCESSING,
                    "claimed_by": worker_id,
                    "claimed_until_utc": lease_until,
                    "updated_at_utc": now,
                }
            )
    return claimed


def finish_notification_delivery(
    event_key: str,
    *,
    worker_id: str,
    success: bool,
    max_attempts: int,
    error: str | None = None,
    updated_at: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """Settle one CLAIMED delivery attempt (attempt_count += 1) and release the claim.

    Guarded on ``(delivery_status = 'processing', claimed_by = worker_id)``: if
    the lease expired and another worker re-claimed the row (or it was
    requeued), this raises :class:`NotificationClaimError` instead of
    clobbering the new owner's state. Outcomes: ``success`` (never re-claimed);
    failure -> ``failed`` (retryable), or ``dead_letter`` once ``attempt_count``
    reaches ``max_attempts`` (kept visible, never auto-retried -- see
    :func:`requeue_notification`). ``error`` must already be sanitized (never a
    webhook URL/secret). One transaction.
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    now = updated_at or models.utc_now_iso()
    with _immediate_txn(db_path) as connection:
        row = connection.execute(
            f"SELECT attempt_count FROM {NOTIFICATIONS_TABLE} "
            "WHERE event_key = ? AND delivery_status = ? AND claimed_by = ?",
            (event_key, DELIVERY_STATUS_PROCESSING, worker_id),
        ).fetchone()
        if row is None:
            raise NotificationClaimError(
                f"claim lost for {event_key!r} (worker {worker_id!r}): row is not processing under this worker"
            )
        attempts = row["attempt_count"] + 1
        if success:
            status = DELIVERY_STATUS_SUCCESS
        elif attempts >= int(max_attempts):
            status = DELIVERY_STATUS_DEAD_LETTER
        else:
            status = DELIVERY_STATUS_FAILED
        connection.execute(
            f"""
            UPDATE {NOTIFICATIONS_TABLE}
            SET delivery_status = ?, attempt_count = ?, last_error = ?,
                claimed_by = NULL, claimed_until_utc = NULL,
                notified_at_utc = ?, updated_at_utc = ?
            WHERE event_key = ?
            """,
            (status, attempts, None if success else error, now if success else None, now, event_key),
        )
    row = get_notification(event_key, db_path=db_path)
    if row is None:  # pragma: no cover - row cannot vanish behind the settled UPDATE
        raise NotificationNotFoundError(f"no notification outbox row for event_key {event_key!r}")
    return row


def requeue_notification(event_key: str, *, db_path: Path | None = None) -> dict:
    """Manual ops recovery: reset one row (incl. ``dead_letter``) to a fresh ``pending``.

    Clears the claim and ``attempt_count`` so the next cycle re-delivers it.
    Raises :class:`NotificationNotFoundError` for an unknown event_key.
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    now = models.utc_now_iso()
    with _immediate_txn(db_path) as connection:
        cursor = connection.execute(
            f"""
            UPDATE {NOTIFICATIONS_TABLE}
            SET delivery_status = ?, attempt_count = 0, last_error = NULL,
                claimed_by = NULL, claimed_until_utc = NULL, updated_at_utc = ?
            WHERE event_key = ?
            """,
            (DELIVERY_STATUS_PENDING, now, event_key),
        )
        if cursor.rowcount == 0:
            raise NotificationNotFoundError(f"no notification outbox row for event_key {event_key!r}")
    row = get_notification(event_key, db_path=db_path)
    if row is None:  # pragma: no cover
        raise NotificationNotFoundError(f"no notification outbox row for event_key {event_key!r}")
    return row
