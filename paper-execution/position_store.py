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
"""

from __future__ import annotations

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
    "pnl",
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

# Columns added idempotently to an older/partial table (Issue #50 backward compat).
_MIGRATED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("max_hold_deadline_utc", "TEXT"),
    ("close_price", "REAL"),
    ("close_time_utc", "TEXT"),
    ("pnl", "REAL"),
    ("last_evaluated_candle_utc", "TEXT"),
)

REQUIRED_COLUMNS: frozenset[str] = frozenset(_ALL_COLUMNS)


class PositionStoreError(RuntimeError):
    """Base class for Paper Execution store errors."""


class PositionSchemaError(PositionStoreError):
    """A pre-existing table is missing required columns."""


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


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    if column not in _table_columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def _validate_schema(connection: sqlite3.Connection) -> None:
    missing = REQUIRED_COLUMNS - _table_columns(connection, POSITIONS_TABLE)
    if missing:
        raise PositionSchemaError(
            f"{POSITIONS_TABLE} exists but is missing required columns: {sorted(missing)}. "
            "Incompatible/incomplete pre-existing schema; migrate or recreate the database."
        )


def init_db(db_path: Path | None = None) -> None:
    """Create/upgrade the ``virtual_positions`` table (idempotent).

    Enforces, at the DB level: the known ``state`` set, the ``state`` x
    ``terminal_reason`` closed set, the ``same_candle_priority`` vocabulary, and
    ``signal_id`` uniqueness. Safe to call repeatedly.
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as connection:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {POSITIONS_TABLE} (
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
                pnl REAL,
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
        )
        for column_name, column_type in _MIGRATED_COLUMNS:
            _ensure_column(connection, POSITIONS_TABLE, column_name, column_type)
        _validate_schema(connection)
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{POSITIONS_TABLE}_state "
            f"ON {POSITIONS_TABLE} (state, symbol, timeframe)"
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
    pnl: float,
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
    with _connect(db_path) as connection:
        cursor = connection.execute(
            f"""
            UPDATE {POSITIONS_TABLE}
            SET state = ?, close_price = ?, close_time_utc = ?, terminal_reason = ?,
                pnl = ?, last_evaluated_candle_utc = ?, updated_at_utc = ?
            WHERE position_id = ? AND state = ? AND last_evaluated_candle_utc IS ?
            """,
            (
                models.STATE_CLOSED, float(close_price), close_time_utc, terminal_reason,
                float(pnl), new_watermark, now,
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
