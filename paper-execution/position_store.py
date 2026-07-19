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

Issue #83 adds the ``paper_execution_notifications`` table (schema v2) to the
same DB: a per-event notification history keyed by a deterministic
``event_key`` (``position_id:event_type``, UNIQUE) that gives Discord
notifications duplicate suppression across runner restarts and retryability
for failed deliveries. Each notification write helper is its own transaction
(single statement, committed by the connection context manager) and is always
separate from the position transition it follows -- a notification failure can
never roll back a position state change.
"""

from __future__ import annotations

import math
import os
import sqlite3
import sys
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

DELIVERY_STATUS_PENDING = "pending"   # recorded, not yet attempted
DELIVERY_STATUS_SUCCESS = "success"   # delivered; never re-sent
DELIVERY_STATUS_FAILED = "failed"     # last attempt failed; retryable until the attempt cap
KNOWN_DELIVERY_STATUSES: frozenset[str] = frozenset(
    {DELIVERY_STATUS_PENDING, DELIVERY_STATUS_SUCCESS, DELIVERY_STATUS_FAILED}
)

_NOTIFICATION_COLUMNS: tuple[str, ...] = (
    "event_key",
    "position_id",
    "event_type",
    "delivery_status",
    "attempt_count",
    "last_error",
    "notified_at_utc",
    "created_at_utc",
    "updated_at_utc",
)
REQUIRED_NOTIFICATION_COLUMNS: frozenset[str] = frozenset(_NOTIFICATION_COLUMNS)


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
# v1: constrained virtual_positions. v2 (Issue #83): + paper_execution_notifications.
POSITIONS_SCHEMA_VERSION = 1
CURRENT_SCHEMA_VERSION = 2

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


def _create_notifications_table_sql(table_name: str) -> str:
    """The Issue #83 notification-history schema (event_key UNIQUE via PRIMARY KEY)."""
    events = "', '".join(sorted(KNOWN_NOTIFY_EVENTS))
    statuses = "', '".join(sorted(KNOWN_DELIVERY_STATUSES))
    return f"""
        CREATE TABLE {table_name} (
            event_key TEXT PRIMARY KEY,
            position_id TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK (event_type IN ('{events}')),
            delivery_status TEXT NOT NULL CHECK (delivery_status IN ('{statuses}')),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            notified_at_utc TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        )
    """


def _ensure_notifications_table(connection: sqlite3.Connection) -> None:
    """Create the notifications table, or fail loudly if a same-named table is unusable.

    Never guesses a column-level migration for an existing incompatible table --
    consistent with the positions migration policy (explicit failure over silent
    data rounding). Must run inside the caller's transaction.
    """
    if not _table_exists(connection, NOTIFICATIONS_TABLE):
        connection.execute(_create_notifications_table_sql(NOTIFICATIONS_TABLE))
        return
    missing = REQUIRED_NOTIFICATION_COLUMNS - _table_columns(connection, NOTIFICATIONS_TABLE)
    if missing:
        raise PositionMigrationError(
            f"table {NOTIFICATIONS_TABLE} exists but is missing columns {sorted(missing)}; "
            "refusing to guess a migration"
        )


def _create_indexes(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{POSITIONS_TABLE}_state "
        f"ON {POSITIONS_TABLE} (state, symbol, timeframe)"
    )
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{NOTIFICATIONS_TABLE}_status "
        f"ON {NOTIFICATIONS_TABLE} (delivery_status, created_at_utc)"
    )


def _positions_schema_current(connection: sqlite3.Connection) -> bool:
    """True when ``virtual_positions`` itself needs no rebuild (v1 constraints present).

    ``user_version >= POSITIONS_SCHEMA_VERSION`` distinguishes a properly
    constrained v1 table from a constraint-less legacy table that merely has all
    columns (which reports ``user_version = 0`` and must be rebuilt).
    """
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version < POSITIONS_SCHEMA_VERSION:
        return False
    return not (REQUIRED_COLUMNS - _table_columns(connection, POSITIONS_TABLE))


def _schema_is_current(connection: sqlite3.Connection) -> bool:
    """True only when the DB reports the current version AND has every column
    of every table (positions + notifications).

    Deliberately checks the real schema (``PRAGMA user_version`` + ``PRAGMA
    table_info``), never "the table exists" -- a constraint-less legacy table
    reports ``user_version = 0`` and is rebuilt.
    """
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version < CURRENT_SCHEMA_VERSION:
        return False
    if REQUIRED_COLUMNS - _table_columns(connection, POSITIONS_TABLE):
        return False
    return not (REQUIRED_NOTIFICATION_COLUMNS - _table_columns(connection, NOTIFICATIONS_TABLE))


def init_db(db_path: Path | None = None) -> None:
    """Create, or migrate to, the current constrained ``virtual_positions`` schema.

    - No table -> create it with all CHECK / UNIQUE / NOT NULL constraints and
      stamp ``PRAGMA user_version``.
    - Table present but not current (older ``user_version`` or missing columns) ->
      rebuild it (a real table rebuild that actually ADDS the constraints; see
      :func:`_migrate_rebuild`), inside one transaction with full rollback on any
      failure, and fail loudly on legacy data that cannot be safely converted.
    - Table already current -> no-op (indexes ensured).

    Idempotent and safe to call repeatedly.
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
                connection.execute(_create_table_sql(POSITIONS_TABLE))
                _ensure_notifications_table(connection)
                _create_indexes(connection)
                connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            return
        if _schema_is_current(connection):
            _create_indexes(connection)
            return
        if _positions_schema_current(connection):
            # v1 -> v2 (Issue #83): positions are already fully constrained; only
            # the notifications table / version stamp are missing. Additive-only,
            # one transaction, full rollback on failure -- position rows untouched.
            connection.execute("BEGIN IMMEDIATE")
            try:
                _ensure_notifications_table(connection)
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
        _ensure_notifications_table(connection)
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


def get_schema_status(db_path: Path | None = None) -> str:
    """Return the position DB's schema status WITHOUT writing anything.

    One of ``missing`` / ``current`` / ``legacy`` / ``invalid``. Read-only: never
    creates the DB file, never migrates, never touches ``mtime``/size -- safe on a
    dry run. ``validate_schema_read_only`` is an alias for callers that want the
    intent spelled out.

    Classification:
      * ``missing``  -- no file yet.
      * ``invalid``  -- the file exists but is NOT a usable SQLite database (a
                        non-SQLite byte stream, a corrupt header, an unreadable
                        schema). This is detected by catching ``sqlite3.DatabaseError``
                        -- which ``file is not a database`` raises, and of which
                        ``sqlite3.OperationalError`` (missing table etc.) is a
                        subclass, so both are covered. We deliberately catch this
                        narrow SQLite family (NOT the whole ``sqlite3.Error``, and
                        NOT ``ProgrammingError``/``Exception``), so a genuine
                        programming bug still surfaces rather than being masked as
                        ``invalid``.
      * ``legacy``   -- a valid SQLite DB, but our tables are absent (need creating)
                        or present at an older version / missing columns (needs a
                        migration -- a v1 DB lacking the Issue #83 notifications
                        table classifies here). A normal (non-dry) run resolves this.
      * ``current``  -- all tables present at the current version with every column.
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    if not db_path.exists():
        return SCHEMA_STATUS_MISSING
    try:
        connection = sqlite3.connect(db_path)
    except sqlite3.DatabaseError:
        return SCHEMA_STATUS_INVALID
    try:
        # Reading sqlite_master forces SQLite to validate the file header; a
        # non-SQLite/corrupt file raises sqlite3.DatabaseError here.
        if not _table_exists(connection, POSITIONS_TABLE):
            return SCHEMA_STATUS_LEGACY  # valid SQLite, our table not created yet
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = _table_columns(connection, POSITIONS_TABLE)
        notification_columns = _table_columns(connection, NOTIFICATIONS_TABLE)  # empty when absent
    except sqlite3.DatabaseError:
        return SCHEMA_STATUS_INVALID
    finally:
        connection.close()  # close() never commits -- no write, mtime/size unchanged
    if (
        version >= CURRENT_SCHEMA_VERSION
        and not (REQUIRED_COLUMNS - columns)
        and not (REQUIRED_NOTIFICATION_COLUMNS - notification_columns)
    ):
        return SCHEMA_STATUS_CURRENT
    return SCHEMA_STATUS_LEGACY


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
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def _read_many(db_path: Path, sql: str, params: tuple) -> list[dict]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as connection:
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


# --- notification history (Issue #83) ----------------------------------------
# Duplicate suppression + retry bookkeeping for Paper Execution Discord
# notifications. Transaction boundary: each helper is ONE SQL statement in its
# own transaction (committed by the connection context manager), always issued
# AFTER the position transition it refers to has committed -- so a notification
# write failure can never roll back or block a position state change.


class NotificationNotFoundError(PositionStoreError):
    """No notification history row exists for the given event_key."""


def record_notification_event(
    position_id: str,
    event_type: str,
    *,
    created_at: str | None = None,
    db_path: Path | None = None,
) -> bool:
    """Idempotently record one notification event as ``pending``.

    Returns True if a new row was inserted, False when the event was already
    recorded (``ON CONFLICT(event_key) DO NOTHING`` -- the runner-restart /
    re-run duplicate guard). Never modifies an existing row, so an already
    ``success`` event stays delivered and is never re-queued.
    """
    key = notification_event_key(position_id, event_type)
    db_path = Path(db_path) if db_path is not None else position_db_path()
    init_db(db_path)
    now = created_at or models.utc_now_iso()
    with _connect(db_path) as connection:
        cursor = connection.execute(
            f"""
            INSERT INTO {NOTIFICATIONS_TABLE}
                (event_key, position_id, event_type, delivery_status, attempt_count,
                 last_error, notified_at_utc, created_at_utc, updated_at_utc)
            VALUES (?, ?, ?, ?, 0, NULL, NULL, ?, ?)
            ON CONFLICT(event_key) DO NOTHING
            """,
            (key, position_id, event_type, DELIVERY_STATUS_PENDING, now, now),
        )
    return cursor.rowcount > 0


def get_notification(event_key: str, *, db_path: Path | None = None) -> dict | None:
    db_path = Path(db_path) if db_path is not None else position_db_path()
    return _read_one(db_path, f"SELECT * FROM {NOTIFICATIONS_TABLE} WHERE event_key = ?", (event_key,))


def list_undelivered_notifications(
    *, db_path: Path | None = None, max_attempts: int | None = None
) -> list[dict]:
    """Rows still awaiting delivery: ``pending`` plus retryable ``failed``.

    ``max_attempts`` caps retries: rows at/over the cap are excluded, so a
    permanently failing delivery (429 / 5xx / connection error) is never
    retried forever. Successful rows are never returned (no re-send).
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    sql = (
        f"SELECT * FROM {NOTIFICATIONS_TABLE} WHERE delivery_status != ?"
    )
    params: tuple = (DELIVERY_STATUS_SUCCESS,)
    if max_attempts is not None:
        sql += " AND attempt_count < ?"
        params += (int(max_attempts),)
    sql += " ORDER BY created_at_utc ASC"
    return _read_many(db_path, sql, params)


def mark_notification_delivery(
    event_key: str,
    *,
    success: bool,
    error: str | None = None,
    updated_at: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """Record one delivery attempt's outcome (attempt_count += 1, status update).

    Success sets ``notified_at_utc`` and clears ``last_error``; failure stores a
    sanitized ``last_error`` (callers must never pass the webhook URL or other
    secrets in *error*). Raises :class:`NotificationNotFoundError` for an
    unknown event_key.
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    init_db(db_path)
    now = updated_at or models.utc_now_iso()
    status = DELIVERY_STATUS_SUCCESS if success else DELIVERY_STATUS_FAILED
    with _connect(db_path) as connection:
        cursor = connection.execute(
            f"""
            UPDATE {NOTIFICATIONS_TABLE}
            SET delivery_status = ?, attempt_count = attempt_count + 1,
                last_error = ?, notified_at_utc = ?, updated_at_utc = ?
            WHERE event_key = ?
            """,
            (status, None if success else error, now if success else None, now, event_key),
        )
        if cursor.rowcount == 0:
            raise NotificationNotFoundError(f"no notification history row for event_key {event_key!r}")
    row = get_notification(event_key, db_path=db_path)
    if row is None:  # pragma: no cover - row cannot vanish between UPDATE and SELECT here
        raise NotificationNotFoundError(f"no notification history row for event_key {event_key!r}")
    return row
