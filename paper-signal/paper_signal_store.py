"""SQLite storage for Paper Signal records.

This module only stores and reads ``paper_only`` hypothesis records. It
never talks to a broker/order API and never stores order, position, lot, or
leverage data.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "paper_signals.sqlite3"


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_signals (
                signal_id TEXT PRIMARY KEY,
                created_at_utc TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                side TEXT NOT NULL,
                market_bias TEXT NOT NULL,
                confidence REAL NOT NULL,
                macd_state TEXT NOT NULL,
                trend TEXT NOT NULL,
                state TEXT NOT NULL,
                freshness TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_paper_signals_dedup
            ON paper_signals (symbol, timeframe, side, created_at_utc)
            """
        )


def save_signal(signal: dict, *, db_path: Path = DEFAULT_DB_PATH) -> None:
    init_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO paper_signals
            (signal_id, created_at_utc, symbol, timeframe, side, market_bias,
             confidence, macd_state, trend, state, freshness, reason, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal["signal_id"],
                signal["created_at_utc"],
                signal["symbol"],
                signal["timeframe"],
                signal["side"],
                signal["market_bias"],
                signal["confidence"],
                signal["macd_state"],
                signal["trend"],
                signal["state"],
                signal["freshness"],
                signal["reason"],
                signal["status"],
            ),
        )


def has_recent_signal(
    symbol: str,
    timeframe: str,
    side: str,
    *,
    within_minutes: int = 60,
    now_utc: datetime | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> bool:
    """Return True if the same symbol/timeframe/side already fired within the window."""
    init_db(db_path)
    now = now_utc or datetime.now(UTC)
    cutoff = (now - timedelta(minutes=within_minutes)).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT 1 FROM paper_signals
            WHERE symbol = ? AND timeframe = ? AND side = ? AND created_at_utc >= ?
            LIMIT 1
            """,
            (symbol, timeframe, side, cutoff),
        ).fetchone()
    return row is not None


def list_signals(*, db_path: Path = DEFAULT_DB_PATH, limit: int = 100) -> list[dict]:
    init_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM paper_signals ORDER BY created_at_utc DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
