"""SQLite storage for persisted Market Bias records.

Market Bias is an observation/classification log only (see
docs/architecture/market_bias.md). This module stores that log so a
Backtest layer can later ask "what was the market state at time T" -- it
never places orders, sizes positions, or generates a trade recommendation.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
DEFAULT_DB_PATH = BASE_DIR / "data" / "market_bias.sqlite3"


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


def market_bias_db_path(env_values: dict[str, str] | None = None) -> Path:
    values = env_values if env_values is not None else load_env_file()
    raw_path = (os.getenv("MARKET_BIAS_DB_PATH") or values.get("MARKET_BIAS_DB_PATH") or str(DEFAULT_DB_PATH)).strip()
    path = Path(raw_path)
    return path if path.is_absolute() else BASE_DIR / path


def init_db(db_path: Path | None = None) -> None:
    db_path = Path(db_path) if db_path is not None else market_bias_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS market_bias (
                market_bias_id TEXT PRIMARY KEY,
                evaluated_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                market_bias TEXT NOT NULL,
                confidence REAL NOT NULL,
                trend TEXT NOT NULL,
                risk TEXT,
                risk_confidence REAL,
                volatility TEXT,
                volatility_confidence REAL,
                reasons_json TEXT,
                risk_reasons_json TEXT,
                volatility_reasons_json TEXT,
                warnings_json TEXT,
                source_snapshot_id TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_market_bias_lookup
            ON market_bias (symbol, timeframe, evaluated_at)
            """
        )


def save_market_bias(record: dict, *, db_path: Path | None = None) -> str:
    db_path = Path(db_path) if db_path is not None else market_bias_db_path()
    init_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO market_bias
            (market_bias_id, evaluated_at, symbol, timeframe, market_bias, confidence,
             trend, risk, risk_confidence, volatility, volatility_confidence,
             reasons_json, risk_reasons_json, volatility_reasons_json, warnings_json,
             source_snapshot_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["market_bias_id"],
                record["evaluated_at"],
                record["symbol"],
                record["timeframe"],
                record["market_bias"],
                record["confidence"],
                record["trend"],
                record.get("risk"),
                record.get("risk_confidence"),
                record.get("volatility"),
                record.get("volatility_confidence"),
                record.get("reasons_json"),
                record.get("risk_reasons_json"),
                record.get("volatility_reasons_json"),
                record.get("warnings_json"),
                record.get("source_snapshot_id"),
            ),
        )
    return record["market_bias_id"]


def get_market_bias_as_of(
    symbol: str,
    timeframe: str,
    at: str,
    *,
    db_path: Path | None = None,
) -> dict | None:
    """Return the latest Market Bias known at or before timestamp ``at``.

    This is a point-in-time ("as of") lookup, not literal nearest-neighbor
    in either time direction: a Backtest replaying the past must never see
    a bias evaluated *after* the moment it is currently at, or it would leak
    future information into a past decision.

    Ties are broken deterministically: when several records share the latest
    ``evaluated_at`` at or before ``at``, the one with the lexicographically
    greatest ``market_bias_id`` wins (``ORDER BY evaluated_at DESC,
    market_bias_id DESC``). This matches ``load_market_bias_series`` +
    ``runner.TimeSeriesBiasLookup`` (whose ascending sort + ``bisect_right``
    selects that same max-id record), so the static and time-series paths
    never disagree on a same-timestamp group -- and neither depends on DB
    insertion order.

    Deliberately side-effect-free on read: unlike the rest of this module,
    this never creates the DB file/table. Returns None (never raises) when
    the DB doesn't exist yet, the table doesn't exist yet, or no record for
    this symbol/timeframe precedes ``at`` -- callers (Paper Signal, Backtest)
    must treat that as "bias unknown for this period", not an error.
    """
    db_path = Path(db_path) if db_path is not None else market_bias_db_path()
    if not db_path.exists():
        return None

    try:
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT * FROM market_bias
                WHERE symbol = ? AND timeframe = ? AND evaluated_at <= ?
                ORDER BY evaluated_at DESC, market_bias_id DESC
                LIMIT 1
                """,
                (symbol, timeframe, at),
            ).fetchone()
    except sqlite3.OperationalError:
        return None

    return dict(row) if row is not None else None


def load_market_bias_series(
    symbol: str,
    timeframe: str,
    *,
    db_path: Path | None = None,
) -> list[dict]:
    """Return every persisted Market Bias for symbol/timeframe, oldest first.

    Point-in-time consumers (Issue #38's time-series Backtest bias) load the
    whole series once, up front, and then walk it in memory -- so this opens
    a single SQLite connection for the entire run instead of re-querying the
    DB per bar.

    Ordered ``evaluated_at ASC, market_bias_id ASC`` so the result is fully
    deterministic even when several records share an ``evaluated_at`` (never
    dependent on DB insertion order). Consumers that binary-search this series
    and take the last record at or before a time therefore pick the
    lexicographically greatest ``market_bias_id`` within that timestamp group
    -- the same record ``get_market_bias_as_of`` returns.

    Deliberately side-effect-free on read, exactly like
    ``get_market_bias_as_of``: it never creates the DB file/table and returns
    an empty list (never raises) when the DB doesn't exist yet, the table
    doesn't exist yet, or no record matches -- callers must treat that as "no
    bias history", not an error.
    """
    db_path = Path(db_path) if db_path is not None else market_bias_db_path()
    if not db_path.exists():
        return []

    try:
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM market_bias
                WHERE symbol = ? AND timeframe = ?
                ORDER BY evaluated_at ASC, market_bias_id ASC
                """,
                (symbol, timeframe),
            ).fetchall()
    except sqlite3.OperationalError:
        return []

    return [dict(row) for row in rows]


def list_market_bias(*, db_path: Path | None = None, limit: int = 100) -> list[dict]:
    db_path = Path(db_path) if db_path is not None else market_bias_db_path()
    init_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM market_bias ORDER BY evaluated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
