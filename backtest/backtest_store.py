"""SQLite storage for Backtest run/signal results.

Kept in its own DB file, separate from paper-signal/paper_signal_store.py:
``paper_signals`` are live paper_only hypothesis records, while
``backtest_runs`` / ``backtest_signals`` are historical simulation results
used to compare strategies after the fact. Neither this module nor its
callers place real orders, connect to a broker, or compute a live lot
size / stop-loss / take-profit -- entry/close/pnl columns here describe
simulated, historical outcomes only.

Save functions always use plain ``INSERT`` statements: a run or signal id
is only ever meant to be written once. Re-running a backtest must produce
a new run with a fresh id (see backtest_models.py), never silently
overwrite a previous one. Reusing an id raises
``sqlite3.IntegrityError``. There are no update_* functions yet -- none of
today's callers need to mutate a saved row after the fact.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
DEFAULT_DB_PATH = BASE_DIR / "data" / "backtest_results.sqlite3"

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backtest_models import BACKTEST_MIGRATION_CLASSIFICATION_DEFAULTS  # noqa: E402  (Issue #27 migration fallback)

REQUIRED_RUN_COLUMNS = {
    "id",
    "run_name",
    "symbol",
    "timeframe",
    "from_time",
    "to_time",
    "strategy_name",
    "exit_policy",
    "config_json",
    "created_at",
}
REQUIRED_SIGNAL_COLUMNS = {
    "id",
    "run_id",
    "signal_time",
    "symbol",
    "timeframe",
    "bias",
    "confidence",
    "direction",
    "signal_type",
    "trigger_type",
    "alert_source",
    "strategy_profile",
    "reason_json",
    "entry_price",
    "invalidation_price",
    "take_profit_price",
    "close_price",
    "result_status",
    "pnl",
    "mfe",
    "mae",
    "closed_at",
    "close_reason",
    "exit_policy",
    "market_state",
    "market_state_confidence",
    "created_at",
}

_INSERT_SIGNAL_SQL = """
    INSERT INTO backtest_signals
    (id, run_id, signal_time, symbol, timeframe, bias, confidence, direction,
     signal_type, trigger_type, alert_source, strategy_profile, reason_json,
     entry_price, invalidation_price, take_profit_price, close_price,
     result_status, pnl, mfe, mae, closed_at, close_reason, exit_policy,
     market_state, market_state_confidence, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class BacktestSchemaError(RuntimeError):
    """Raised when a pre-existing backtest table is missing required columns.

    This means a table with this name already exists but was not created
    by this module's ``init_db()`` (e.g. an incomplete/incompatible schema
    left over from something else) -- surfaced clearly here instead of
    letting a later ``CREATE INDEX`` fail with a confusing "no such column".
    """


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


def backtest_db_path(env_values: dict[str, str] | None = None) -> Path:
    values = env_values if env_values is not None else load_env_file()
    raw_path = (os.getenv("BACKTEST_DB_PATH") or values.get("BACKTEST_DB_PATH") or str(DEFAULT_DB_PATH)).strip()
    path = Path(raw_path)
    return path if path.is_absolute() else BASE_DIR / path


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    # Required for run_id -> backtest_runs.id to actually be enforced;
    # SQLite parses FOREIGN KEY clauses but ignores them unless this is set
    # on the connection.
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _ensure_column(connection: sqlite3.Connection, table_name: str, column_name: str, column_type: str) -> None:
    """Add ``column_name`` to an existing table if it isn't there yet.

    Lets a backtest_signals table created before ``exit_policy`` existed
    upgrade in place: existing rows keep working (the new column reads as
    NULL for them) and the process never needs a bespoke migration script.
    """
    if column_name not in _table_columns(connection, table_name):
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def _backfill_classification_defaults(connection: sqlite3.Connection) -> None:
    """Fill NULL/empty Issue #27 classification values with the safe defaults.

    Applies to all four columns (signal_type / trigger_type / alert_source /
    strategy_profile), so a row from before these columns existed -- or a
    partial/intermediate schema that left them NULL or empty -- ends up with a
    valid value instead of NULL. Only touches NULL/empty cells, so a row that
    already carries a real value (e.g. a candidate's "trend_follow") is never
    overwritten, and re-running is a no-op. Column names come from a fixed
    constant, never user input. This is the *only* consumer of
    ``BACKTEST_MIGRATION_CLASSIFICATION_DEFAULTS`` -- normal signal saves store
    the candidate's own values (see backtest/runner.py), never these fallbacks.
    """
    for column, default in BACKTEST_MIGRATION_CLASSIFICATION_DEFAULTS.items():
        connection.execute(
            f"UPDATE backtest_signals SET {column} = ? WHERE {column} IS NULL OR {column} = ''",
            (default,),
        )


def _validate_schema(connection: sqlite3.Connection) -> None:
    """Raise BacktestSchemaError if either table exists but lacks required columns.

    Not a full migration system (no schema-version table) -- just a clear
    failure instead of a cryptic sqlite3.OperationalError from a later
    CREATE INDEX / INSERT against a column that doesn't exist.
    """
    missing_run = REQUIRED_RUN_COLUMNS - _table_columns(connection, "backtest_runs")
    if missing_run:
        raise BacktestSchemaError(
            f"backtest_runs table exists but is missing required columns: {sorted(missing_run)}. "
            "This looks like an incompatible or incomplete pre-existing schema; "
            "migrate or recreate the database."
        )

    missing_signal = REQUIRED_SIGNAL_COLUMNS - _table_columns(connection, "backtest_signals")
    if missing_signal:
        raise BacktestSchemaError(
            f"backtest_signals table exists but is missing required columns: {sorted(missing_signal)}. "
            "This looks like an incompatible or incomplete pre-existing schema; "
            "migrate or recreate the database."
        )


def init_db(db_path: Path | None = None) -> None:
    """Create backtest_runs / backtest_signals (and indexes) if missing.

    Uses ``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS``
    throughout, so calling this repeatedly is always safe. Validates the
    existing schema (see ``_validate_schema``) before creating indexes, so
    an incompatible pre-existing table fails clearly here rather than
    inside a later CREATE INDEX / INSERT.
    """
    db_path = Path(db_path) if db_path is not None else backtest_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id TEXT PRIMARY KEY,
                run_name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                from_time TEXT NOT NULL,
                to_time TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                exit_policy TEXT,
                config_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS backtest_signals (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                signal_time TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                bias TEXT,
                confidence REAL,
                direction TEXT,
                signal_type TEXT,
                trigger_type TEXT,
                alert_source TEXT,
                strategy_profile TEXT,
                reason_json TEXT,
                entry_price REAL,
                invalidation_price REAL,
                take_profit_price REAL,
                close_price REAL,
                result_status TEXT,
                pnl REAL,
                mfe REAL,
                mae REAL,
                closed_at TEXT,
                close_reason TEXT,
                exit_policy TEXT,
                market_state TEXT,
                market_state_confidence REAL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES backtest_runs(id)
            )
            """
        )

        # Migrate a backtest_signals table created before exit_policy existed.
        _ensure_column(connection, "backtest_signals", "exit_policy", "TEXT")

        # Issue #27: add the two new classification columns to a
        # backtest_signals table created before they existed, then backfill any
        # NULL/empty classification value (including the older signal_type /
        # trigger_type columns) with the current Paper Signal defaults. Both
        # steps are idempotent, so this is safe to run on every init_db().
        _ensure_column(connection, "backtest_signals", "alert_source", "TEXT")
        _ensure_column(connection, "backtest_signals", "strategy_profile", "TEXT")
        _backfill_classification_defaults(connection)

        # Issue #39: add the Market State columns to a backtest_signals table
        # created before they existed. Unlike the Issue #27 classification
        # columns, these are NOT backfilled: a legacy row's true Market State is
        # unknowable after the fact, so NULL (== "unknown" in the by_market_state
        # breakdown) is the honest value rather than a fabricated default.
        _ensure_column(connection, "backtest_signals", "market_state", "TEXT")
        _ensure_column(connection, "backtest_signals", "market_state_confidence", "REAL")

        _validate_schema(connection)

        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_backtest_signals_run_id ON backtest_signals (run_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_backtest_signals_symbol_timeframe "
            "ON backtest_signals (symbol, timeframe, signal_time)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_backtest_runs_symbol_timeframe "
            "ON backtest_runs (symbol, timeframe, created_at)"
        )


def save_backtest_run(record: dict, *, db_path: Path | None = None) -> str:
    """Insert one backtest_runs row.

    Raises ``sqlite3.IntegrityError`` if ``record["id"]`` already exists.
    """
    db_path = Path(db_path) if db_path is not None else backtest_db_path()
    init_db(db_path)
    with _connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO backtest_runs
            (id, run_name, symbol, timeframe, from_time, to_time, strategy_name,
             exit_policy, config_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["id"],
                record["run_name"],
                record["symbol"],
                record["timeframe"],
                record["from_time"],
                record["to_time"],
                record["strategy_name"],
                record.get("exit_policy"),
                record.get("config_json"),
                record["created_at"],
            ),
        )
    return record["id"]


def _signal_row(record: dict) -> tuple:
    return (
        record["id"],
        record["run_id"],
        record["signal_time"],
        record["symbol"],
        record["timeframe"],
        record.get("bias"),
        record.get("confidence"),
        record.get("direction"),
        record.get("signal_type"),
        record.get("trigger_type"),
        record.get("alert_source"),
        record.get("strategy_profile"),
        record.get("reason_json"),
        record.get("entry_price"),
        record.get("invalidation_price"),
        record.get("take_profit_price"),
        record.get("close_price"),
        record.get("result_status"),
        record.get("pnl"),
        record.get("mfe"),
        record.get("mae"),
        record.get("closed_at"),
        record.get("close_reason"),
        record.get("exit_policy"),
        record.get("market_state"),
        record.get("market_state_confidence"),
        record["created_at"],
    )


def save_backtest_signal(record: dict, *, db_path: Path | None = None) -> str:
    """Insert one backtest_signals row.

    Raises ``sqlite3.IntegrityError`` if ``record["id"]`` already exists or
    ``record["run_id"]`` does not exist in ``backtest_runs`` (foreign key
    enforcement is on).
    """
    db_path = Path(db_path) if db_path is not None else backtest_db_path()
    init_db(db_path)
    with _connect(db_path) as connection:
        connection.execute(_INSERT_SIGNAL_SQL, _signal_row(record))
    return record["id"]


def save_backtest_signals(records: list[dict], *, db_path: Path | None = None) -> list[str]:
    """Bulk-save signals belonging to the same run in a single transaction.

    Uses one ``executemany()`` call inside one transaction: if any row
    fails (duplicate id, FK violation, etc.), the whole batch is rolled
    back and none of the rows are persisted.
    """
    if not records:
        return []
    db_path = Path(db_path) if db_path is not None else backtest_db_path()
    init_db(db_path)
    with _connect(db_path) as connection:
        connection.executemany(_INSERT_SIGNAL_SQL, (_signal_row(record) for record in records))
    return [record["id"] for record in records]


def get_backtest_run(run_id: str, *, db_path: Path | None = None) -> dict | None:
    """Return one backtest_runs row, or None if the DB/table/row don't exist.

    Read-only: never creates the DB file as a side effect.
    """
    db_path = Path(db_path) if db_path is not None else backtest_db_path()
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row is not None else None


def list_backtest_runs(*, db_path: Path | None = None, limit: int = 100) -> list[dict]:
    """Return backtest_runs, most recent first. Read-only, never creates the DB."""
    db_path = Path(db_path) if db_path is not None else backtest_db_path()
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]


def list_backtest_signals(run_id: str, *, db_path: Path | None = None, limit: int = 1000) -> list[dict]:
    """Return backtest_signals for one run, oldest first. Read-only, never creates the DB."""
    db_path = Path(db_path) if db_path is not None else backtest_db_path()
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM backtest_signals WHERE run_id = ? ORDER BY signal_time ASC LIMIT ?",
                (run_id, limit),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]
