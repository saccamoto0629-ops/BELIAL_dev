"""SQLite storage for Paper Execution virtual positions (Issue #50).

*** PAPER EXECUTION. NOT A LIVE ORDER / BROKER LAYER. ***

Kept in its own DB file (``paper-execution/data/paper_positions.sqlite3``),
deliberately separate from ``paper-signal/paper_signal_store.py``'s
``paper_signals`` table and from ``backtest/backtest_store.py``'s Backtest DB:

- ``paper_signals``          -- live paper_only *hypotheses* (Paper Signal layer)
- ``backtest_*``             -- historical *replay* results (validation layer)
- ``virtual_positions`` here -- simulated Paper Execution *positions* + PnL

Because it is a separate file, standing up Paper Execution can never require
deleting, migrating, or otherwise touching the existing ``paper_signals`` DB
(Issue #50: "既存DBを削除しなくても起動・migrationできる"). The link to a
Paper Signal is stored as a plain ``paper_signal_id`` TEXT reference (no
cross-file foreign key is possible, and none is needed).

Idempotency is guaranteed at BOTH layers, on purpose:

- Open   -- a UNIQUE index on ``paper_signal_id`` makes "one virtual position
            per Paper Signal" a *database-level* invariant; ``create_position``
            uses a plain INSERT, so a duplicate Open raises
            ``sqlite3.IntegrityError`` rather than creating a second row. The
            runner ALSO probes ``get_position_by_signal`` first (a
            *repository-level* guard) so a re-run is a quiet skip, not an error.
- Close  -- ``close_position`` updates ``WHERE status = 'open'``; a second Close
            matches zero rows and raises ``PositionAlreadyClosedError`` (a
            repository/DB-level guard). A UNIQUE ``(position_id, event_type)``
            index on the history table is the belt-and-braces DB guarantee that
            a duplicate ``closed`` event can never be recorded either.

Every mutating operation writes the position row and its matching history event
inside ONE transaction (one ``sqlite3`` connection ``with`` block), so a failure
part-way through leaves no half-updated state. Read helpers never create the DB
file (mirroring ``paper_signal_store.signal_exists_for_candle``), so a dry-run
that only reads has no persistent side effect.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
DEFAULT_DB_PATH = BASE_DIR / "data" / "paper_positions.sqlite3"

# Tolerate transient SQLite locks under concurrent writers, matching
# paper_signal_store's BUSY_TIMEOUT_SECONDS convention.
BUSY_TIMEOUT_SECONDS = 30

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import position_models as models  # noqa: E402  (canonical status/side/event vocabulary)

POSITIONS_TABLE = "virtual_positions"
EVENTS_TABLE = "position_events"

# Core columns are declared in CREATE TABLE; every NULLable exit / PnL /
# provenance column is (also) added idempotently via _ensure_column(), so a
# ``virtual_positions`` table created by an older/partial schema upgrades in
# place instead of forcing a DB rebuild (Issue #50 backward compatibility).
_MIGRATED_POSITION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("exit_price", "REAL"),
    ("closed_at", "TEXT"),
    ("close_reason", "TEXT"),
    ("gross_pnl", "REAL"),
    ("net_pnl", "REAL"),
    ("spread_cost", "REAL"),
    ("slippage_cost", "REAL"),
    ("fee_cost", "REAL"),
    ("r_multiple", "REAL"),
    ("entry_signal_time", "TEXT"),
)

REQUIRED_POSITION_COLUMNS: frozenset[str] = frozenset(
    {
        "position_id",
        "paper_signal_id",
        "strategy_profile",
        "symbol",
        "timeframe",
        "side",
        "status",
        "entry_price",
        "size",
        "stop_loss",
        "take_profit",
        "opened_at",
        "created_at",
        "updated_at",
    }
    | {name for name, _type in _MIGRATED_POSITION_COLUMNS}
)


class PositionStoreError(RuntimeError):
    """Base class for Paper Execution store errors."""


class PositionSchemaError(PositionStoreError):
    """A pre-existing table exists but is missing required columns."""


class PositionNotFoundError(PositionStoreError):
    """No virtual position exists for the given id."""


class PositionAlreadyClosedError(PositionStoreError):
    """A Close was attempted on a position that is not ``open`` (double Close)."""


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
    raw_path = (
        os.getenv("PAPER_POSITION_DB_PATH") or values.get("PAPER_POSITION_DB_PATH") or str(DEFAULT_DB_PATH)
    ).strip()
    path = Path(raw_path)
    return path if path.is_absolute() else BASE_DIR / path


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_SECONDS)
    # Enforce the events -> positions foreign key (SQLite ignores it otherwise).
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}")
    return connection


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _ensure_column(connection: sqlite3.Connection, table_name: str, column_name: str, column_type: str) -> None:
    """Add a nullable column to an existing table if it isn't there yet.

    Lets a ``virtual_positions`` table created before a column existed upgrade
    in place: existing rows keep working (the new column reads NULL for them)
    and no bespoke migration script is needed. Idempotent.
    """
    if column_name not in _table_columns(connection, table_name):
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def _validate_schema(connection: sqlite3.Connection) -> None:
    missing = REQUIRED_POSITION_COLUMNS - _table_columns(connection, POSITIONS_TABLE)
    if missing:
        raise PositionSchemaError(
            f"{POSITIONS_TABLE} table exists but is missing required columns: {sorted(missing)}. "
            "This looks like an incompatible or incomplete pre-existing schema; migrate or recreate the database."
        )


def init_db(db_path: Path | None = None) -> None:
    """Create the ``virtual_positions`` / ``position_events`` tables + indexes.

    Uses ``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` and
    idempotent ``_ensure_column`` migrations throughout, so calling it
    repeatedly -- on a fresh DB, or on one left by an older/partial schema -- is
    always safe and never destroys data.
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as connection:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {POSITIONS_TABLE} (
                position_id TEXT PRIMARY KEY,
                paper_signal_id TEXT NOT NULL,
                strategy_profile TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                entry_price REAL NOT NULL,
                size REAL NOT NULL,
                stop_loss REAL,
                take_profit REAL,
                opened_at TEXT NOT NULL,
                exit_price REAL,
                closed_at TEXT,
                close_reason TEXT,
                gross_pnl REAL,
                net_pnl REAL,
                spread_cost REAL,
                slippage_cost REAL,
                fee_cost REAL,
                r_multiple REAL,
                entry_signal_time TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {EVENTS_TABLE} (
                event_id TEXT PRIMARY KEY,
                position_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_time TEXT NOT NULL,
                price REAL,
                reason TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (position_id) REFERENCES {POSITIONS_TABLE}(position_id)
            )
            """
        )

        # Upgrade a legacy/partial positions table in place (no-op on a fresh DB).
        for column_name, column_type in _MIGRATED_POSITION_COLUMNS:
            _ensure_column(connection, POSITIONS_TABLE, column_name, column_type)

        _validate_schema(connection)

        # Open idempotency: one virtual position per Paper Signal (DB-level).
        connection.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{POSITIONS_TABLE}_signal_unique "
            f"ON {POSITIONS_TABLE} (paper_signal_id)"
        )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{POSITIONS_TABLE}_status "
            f"ON {POSITIONS_TABLE} (status, symbol, timeframe)"
        )
        # History idempotency: one 'opened' and at most one 'closed' per position.
        connection.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{EVENTS_TABLE}_unique "
            f"ON {EVENTS_TABLE} (position_id, event_type)"
        )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{EVENTS_TABLE}_position ON {EVENTS_TABLE} (position_id)"
        )


_INSERT_POSITION_SQL = f"""
    INSERT INTO {POSITIONS_TABLE}
    (position_id, paper_signal_id, strategy_profile, symbol, timeframe, side, status,
     entry_price, size, stop_loss, take_profit, opened_at,
     exit_price, closed_at, close_reason,
     gross_pnl, net_pnl, spread_cost, slippage_cost, fee_cost, r_multiple,
     entry_signal_time, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_EVENT_SQL = f"""
    INSERT INTO {EVENTS_TABLE}
    (event_id, position_id, event_type, event_time, price, reason, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
"""


def _position_row(record: dict) -> tuple:
    return (
        record["position_id"],
        record["paper_signal_id"],
        record["strategy_profile"],
        record["symbol"],
        record["timeframe"],
        record["side"],
        record["status"],
        record["entry_price"],
        record["size"],
        record.get("stop_loss"),
        record.get("take_profit"),
        record["opened_at"],
        record.get("exit_price"),
        record.get("closed_at"),
        record.get("close_reason"),
        record.get("gross_pnl"),
        record.get("net_pnl"),
        record.get("spread_cost"),
        record.get("slippage_cost"),
        record.get("fee_cost"),
        record.get("r_multiple"),
        record.get("entry_signal_time"),
        record["created_at"],
        record["updated_at"],
    )


def create_position(position: dict, *, db_path: Path | None = None) -> str:
    """Insert one ``open`` virtual position AND its ``opened`` event, atomically.

    Both rows are written in a single transaction, so a failure on either leaves
    neither behind. Raises ``sqlite3.IntegrityError`` when ``position_id`` already
    exists OR when ``paper_signal_id`` is already tied to a position (the UNIQUE
    index that makes Open idempotent). Callers that want a quiet re-run should
    probe :func:`get_position_by_signal` first.
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    init_db(db_path)
    opened_event = _opened_event_row(position)
    with _connect(db_path) as connection:
        connection.execute(_INSERT_POSITION_SQL, _position_row(position))
        connection.execute(_INSERT_EVENT_SQL, opened_event)
    return position["position_id"]


def _opened_event_row(position: dict) -> tuple:
    return (
        models.new_event_id(),
        position["position_id"],
        models.EVENT_OPENED,
        position.get("entry_signal_time") or position["opened_at"],
        position["entry_price"],
        None,
        position["created_at"],
    )


def close_position(
    position_id: str,
    *,
    exit_price: float,
    closed_at: str,
    close_reason: str,
    gross_pnl: float,
    net_pnl: float,
    spread_cost: float | None = None,
    slippage_cost: float | None = None,
    fee_cost: float | None = None,
    r_multiple: float | None = None,
    event_time: str | None = None,
    updated_at: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """Close an ``open`` position, writing all exit + PnL fields at once.

    In one transaction: (1) UPDATE the position's status / exit_price / closed_at
    / close_reason / PnL columns ``WHERE status = 'open'``; (2) INSERT the
    ``closed`` history event. The ``WHERE status = 'open'`` guard is the
    double-Close protection: a second Close matches zero rows and raises
    :class:`PositionAlreadyClosedError` (a missing id raises
    :class:`PositionNotFoundError`). A non-``open`` position is therefore never
    re-updated. Returns the closed position row.
    """
    if close_reason not in models.KNOWN_CLOSE_REASONS:
        raise ValueError(f"close_reason must be one of {sorted(models.KNOWN_CLOSE_REASONS)}, got {close_reason!r}")
    db_path = Path(db_path) if db_path is not None else position_db_path()
    init_db(db_path)
    now = updated_at or models.utc_now_iso()
    with _connect(db_path) as connection:
        cursor = connection.execute(
            f"""
            UPDATE {POSITIONS_TABLE}
            SET status = ?, exit_price = ?, closed_at = ?, close_reason = ?,
                gross_pnl = ?, net_pnl = ?, spread_cost = ?, slippage_cost = ?,
                fee_cost = ?, r_multiple = ?, updated_at = ?
            WHERE position_id = ? AND status = ?
            """,
            (
                models.STATUS_CLOSED,
                float(exit_price),
                closed_at,
                close_reason,
                float(gross_pnl),
                float(net_pnl),
                spread_cost,
                slippage_cost,
                fee_cost,
                r_multiple,
                now,
                position_id,
                models.STATUS_OPEN,
            ),
        )
        if cursor.rowcount == 0:
            # Nothing was updated: either the id is unknown, or it is already
            # closed. Distinguish the two so callers get a precise error. The
            # SELECT runs inside the same (still-open) transaction; raising then
            # triggers a rollback of the no-op UPDATE.
            row = connection.execute(
                f"SELECT status FROM {POSITIONS_TABLE} WHERE position_id = ?", (position_id,)
            ).fetchone()
            if row is None:
                raise PositionNotFoundError(f"no virtual position with id {position_id!r}")
            raise PositionAlreadyClosedError(
                f"position {position_id!r} is already {row[0]!r}; refusing to re-close"
            )
        connection.execute(
            _INSERT_EVENT_SQL,
            (
                models.new_event_id(),
                position_id,
                models.EVENT_CLOSED,
                event_time or closed_at,
                float(exit_price),
                close_reason,
                now,
            ),
        )
    closed = get_position(position_id, db_path=db_path)
    assert closed is not None  # just updated it in this process
    return closed


# --- read helpers (never create the DB file: dry-run / report safe) ----------


def _read_one(db_path: Path, sql: str, params: tuple) -> dict | None:
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(sql, params).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row is not None else None


def _read_many(db_path: Path, sql: str, params: tuple) -> list[dict]:
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]


def get_position(position_id: str, *, db_path: Path | None = None) -> dict | None:
    db_path = Path(db_path) if db_path is not None else position_db_path()
    return _read_one(db_path, f"SELECT * FROM {POSITIONS_TABLE} WHERE position_id = ?", (position_id,))


def get_position_by_signal(paper_signal_id: str, *, db_path: Path | None = None) -> dict | None:
    """Return the virtual position tied to ``paper_signal_id``, or None.

    The repository-level Open idempotency probe: the runner calls this before
    attempting an Open, so a re-run of the same Paper Signal is a quiet skip.
    Read-only -- never creates the DB (dry-run safe).
    """
    db_path = Path(db_path) if db_path is not None else position_db_path()
    return _read_one(
        db_path, f"SELECT * FROM {POSITIONS_TABLE} WHERE paper_signal_id = ?", (paper_signal_id,)
    )


def list_open_positions(
    *, symbol: str | None = None, timeframe: str | None = None, db_path: Path | None = None
) -> list[dict]:
    """Return every ``open`` position (optionally filtered by symbol/timeframe)."""
    db_path = Path(db_path) if db_path is not None else position_db_path()
    clauses = ["status = ?"]
    params: list = [models.STATUS_OPEN]
    if symbol is not None:
        clauses.append("symbol = ?")
        params.append(symbol)
    if timeframe is not None:
        clauses.append("timeframe = ?")
        params.append(timeframe)
    where = " AND ".join(clauses)
    return _read_many(
        db_path, f"SELECT * FROM {POSITIONS_TABLE} WHERE {where} ORDER BY opened_at ASC", tuple(params)
    )


def list_positions(*, db_path: Path | None = None, limit: int = 1000) -> list[dict]:
    db_path = Path(db_path) if db_path is not None else position_db_path()
    return _read_many(
        db_path, f"SELECT * FROM {POSITIONS_TABLE} ORDER BY created_at DESC LIMIT ?", (limit,)
    )


def list_events(position_id: str, *, db_path: Path | None = None) -> list[dict]:
    """Return a position's state-change history, oldest first. Read-only."""
    db_path = Path(db_path) if db_path is not None else position_db_path()
    return _read_many(
        db_path, f"SELECT * FROM {EVENTS_TABLE} WHERE position_id = ? ORDER BY created_at ASC, event_type ASC", (position_id,)
    )
