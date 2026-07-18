"""Fetch OHLCV chart data and persist it to SQLite.

The collector is observation-only. It never places orders or performs trading
actions.
"""

from __future__ import annotations

import os
import uuid
import sqlite3
from datetime import UTC
from pathlib import Path

import pandas as pd
import yfinance as yf

from chart_models import Candle, ChartTarget
from chart_snapshot_store import ensure_paper_signal_links_schema

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
DEFAULT_DB_PATH = BASE_DIR / "data" / "chart_data.sqlite3"

DEFAULT_PROVIDER_TICKERS = {
    "USDJPY": "JPY=X",
    "XAUUSD": "GC=F",
    "SPX": "^GSPC",
    "ES": "ES=F",
    "MES": "MES=F",
    "VIX": "^VIX",
}
DEFAULT_SYMBOL_TICKERS = {
    "USDJPY": "JPY=X",
    "XAUUSD": "GC=F",
    "SPX": "ES=F",
    "VIX": "^VIX",
}
DEFAULT_DISPLAY_NAMES = {
    "USDJPY": "USD/JPY",
    "XAUUSD": "Gold",
    "SPX": "S&P 500",
    "VIX": "Volatility",
}
DEFAULT_CHART_SYMBOLS = {
    "USDJPY": "USDJPY",
    "XAUUSD": "XAUUSD",
    "SPX": "ES",
    "VIX": "VIX",
}
DEFAULT_FALLBACK_CHART_SYMBOLS = {
    "SPX": "SPX",
}
DEFAULT_ASSET_TYPES = {
    "USDJPY": "fx",
    "XAUUSD": "commodity_futures_proxy",
    "SPX": "index_futures_proxy",
    "VIX": "volatility_index",
}
DEFAULT_SYMBOLS = tuple(DEFAULT_SYMBOL_TICKERS)
DEFAULT_TIMEFRAMES = ("15m", "1h", "4h")
DEFAULT_PERIOD = "60d"

MIGRATION_NOTE = (
    "Chart candle timestamps are stored as UTC ISO strings ending in 'Z'. "
    "If an older SQLite database contains timezone-offset or naive timestamps, "
    "rebuild it by deleting market-watch/data/chart_data.sqlite3 and running "
    "chart_snapshot.py without --skip-fetch, or migrate rows by parsing each "
    "timestamp and rewriting it through to_utc_iso_z()."
)


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


def _env_value(values: dict[str, str], name: str, default: str = "") -> str:
    return (os.getenv(name) or values.get(name) or default).strip()


def parse_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _provider_ticker(chart_symbol: str) -> str:
    normalized = chart_symbol.upper()
    return DEFAULT_PROVIDER_TICKERS.get(normalized, chart_symbol)


def load_chart_targets(env_values: dict[str, str] | None = None) -> list[ChartTarget]:
    values = env_values if env_values is not None else load_env_file()
    symbols = parse_csv(_env_value(values, "CHART_SYMBOLS", ",".join(DEFAULT_SYMBOLS)))
    timeframes = parse_csv(_env_value(values, "CHART_TIMEFRAMES", ",".join(DEFAULT_TIMEFRAMES)))

    targets: list[ChartTarget] = []
    for symbol in symbols:
        logical_symbol = symbol.upper()
        display_name = _env_value(values, f"CHART_DISPLAY_NAME_{logical_symbol}", DEFAULT_DISPLAY_NAMES.get(logical_symbol, logical_symbol))
        chart_symbol = _env_value(values, f"CHART_SYMBOL_{logical_symbol}", DEFAULT_CHART_SYMBOLS.get(logical_symbol, logical_symbol))
        fallback_chart_symbol = _env_value(
            values,
            f"CHART_FALLBACK_CHART_SYMBOL_{logical_symbol}",
            DEFAULT_FALLBACK_CHART_SYMBOLS.get(logical_symbol, ""),
        )
        asset_type = _env_value(values, f"CHART_ASSET_TYPE_{logical_symbol}", DEFAULT_ASSET_TYPES.get(logical_symbol, ""))
        ticker = _env_value(values, f"CHART_TICKER_{logical_symbol}", _provider_ticker(chart_symbol))
        fallback_ticker = ""
        if fallback_chart_symbol:
            fallback_ticker = _env_value(
                values,
                f"CHART_FALLBACK_TICKER_{logical_symbol}",
                _provider_ticker(fallback_chart_symbol),
            )
        for timeframe in timeframes:
            targets.append(
                ChartTarget(
                    symbol=logical_symbol,
                    ticker=ticker,
                    timeframe=timeframe,
                    display_name=display_name,
                    chart_symbol=chart_symbol.upper(),
                    fallback_chart_symbol=fallback_chart_symbol.upper(),
                    fallback_ticker=fallback_ticker,
                    asset_type=asset_type,
                )
            )
    return targets


def chart_db_path(env_values: dict[str, str] | None = None) -> Path:
    values = env_values if env_values is not None else load_env_file()
    raw_path = _env_value(values, "CHART_DB_PATH", str(DEFAULT_DB_PATH))
    path = Path(raw_path)
    return path if path.is_absolute() else BASE_DIR / path


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                PRIMARY KEY (symbol, timeframe, timestamp)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chart_snapshots (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                snapshot_at TEXT NOT NULL,
                s3_bucket TEXT NOT NULL,
                s3_key TEXT NOT NULL,
                captured_at TEXT,
                candle_time TEXT,
                image_url TEXT,
                image_s3_key TEXT,
                ohlcv_json TEXT,
                indicators_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY,
                alert_type TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                candle_time TEXT NOT NULL,
                snapshot_id TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (snapshot_id) REFERENCES chart_snapshots(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_chart_snapshots (
                alert_id TEXT NOT NULL,
                chart_snapshot_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (alert_id, chart_snapshot_id),
                FOREIGN KEY (alert_id) REFERENCES alerts(id),
                FOREIGN KEY (chart_snapshot_id) REFERENCES chart_snapshots(id)
            )
            """
        )
        # Issue #55: the Paper Signal <-> Chart Snapshot link table is part of
        # the regular chart schema (alert_chart_snapshots analogue), owned and
        # migrated here like every other chart table.
        ensure_paper_signal_links_schema(connection)
        _ensure_column(connection, "chart_snapshots", "snapshot_at", "TEXT")
        _ensure_column(connection, "chart_snapshots", "s3_bucket", "TEXT")
        _ensure_column(connection, "chart_snapshots", "s3_key", "TEXT")
        _ensure_column(connection, "chart_snapshots", "image_s3_key", "TEXT")


def _ensure_column(connection: sqlite3.Connection, table_name: str, column_name: str, column_type: str) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()}
    if column_name not in columns:
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def to_utc_iso_z(timestamp) -> str:
    """Return a UTC ISO-8601 timestamp like ``2026-07-03T20:45:00Z``.

    Timezone-aware source timestamps are converted to UTC before saving. Naive
    timestamps are treated as UTC because yfinance normally returns exchange
    timestamps with timezone information, and guessing a local timezone would
    create mixed historical data.
    """
    parsed = pd.Timestamp(timestamp)
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize(UTC)
    else:
        parsed = parsed.tz_convert(UTC)
    parsed = parsed.replace(microsecond=0, nanosecond=0)
    return parsed.isoformat().replace("+00:00", "Z")


def _normalize_history(history: pd.DataFrame) -> pd.DataFrame:
    if history.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    frame = history.rename(columns=str.lower)
    frame = frame[[column for column in ["open", "high", "low", "close", "volume"] if column in frame.columns]]
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    if "volume" not in frame.columns:
        frame["volume"] = 0.0
    return frame


def _resample_4h(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    return (
        frame.resample("4h")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open", "high", "low", "close"])
    )


def fetch_ohlcv(target: ChartTarget, *, period: str = DEFAULT_PERIOD) -> pd.DataFrame:
    """Fetch OHLCV from Yahoo Finance via yfinance for one target."""
    interval = "1h" if target.timeframe == "4h" else target.timeframe
    history = yf.Ticker(target.ticker).history(period=period, interval=interval)
    frame = _normalize_history(history)
    if frame.empty and target.fallback_ticker and target.fallback_ticker != target.ticker:
        history = yf.Ticker(target.fallback_ticker).history(period=period, interval=interval)
        frame = _normalize_history(history)
    if target.timeframe == "4h":
        frame = _resample_4h(frame)
    return frame


def candles_from_frame(target: ChartTarget, frame: pd.DataFrame) -> list[Candle]:
    candles: list[Candle] = []
    for timestamp, row in frame.iterrows():
        candles.append(
            Candle(
                symbol=target.symbol,
                timeframe=target.timeframe,
                timestamp=to_utc_iso_z(timestamp),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0.0)),
            )
        )
    return candles


def save_candles(db_path: Path, candles: list[Candle]) -> int:
    init_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            """
            INSERT OR REPLACE INTO candles
            (symbol, timeframe, timestamp, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    candle.symbol,
                    candle.timeframe,
                    candle.timestamp,
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.volume,
                )
                for candle in candles
            ],
        )
    return len(candles)


def load_candles(db_path: Path, symbol: str, timeframe: str, *, limit: int = 200) -> pd.DataFrame:
    init_db(db_path)
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT timestamp, open, high, low, close, volume
            FROM candles
            WHERE symbol = ? AND timeframe = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (symbol.upper(), timeframe, limit),
        ).fetchall()

    rows.reverse()
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame.set_index("timestamp")


class ChartReadError(RuntimeError):
    """A Chart DB read failed for an infrastructure reason, not "no candles".

    Raised by :func:`load_candles_range_strict` when the DB file is missing, the
    ``candles`` table is absent, the schema is incompatible, or SQLite raises an
    ``OperationalError`` (corruption/lock/etc.). This is deliberately DISTINCT
    from "the table read fine but matched zero rows" (an empty DataFrame), so a
    caller such as Paper Execution can refuse to treat a read failure as "no
    data" (which would wrongly expire pending positions).
    """


def load_candles_range_strict(
    db_path: Path,
    symbol: str,
    timeframe: str,
    *,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """Strict variant of :func:`load_candles_range`: raise on any read failure.

    Returns an empty DataFrame ONLY when the ``candles`` table read succeeds but
    matched no rows. A missing DB file, a missing ``candles`` table, an
    incompatible schema, or any ``sqlite3.OperationalError`` raises
    :class:`ChartReadError` instead of silently returning empty. Read-only:
    never creates the DB file/table.
    """
    if not db_path.exists():
        raise ChartReadError(f"Chart DB file not found: {db_path}")

    clauses = ["symbol = ?", "timeframe = ?"]
    params: list = [symbol.upper(), timeframe]
    if start is not None:
        clauses.append("timestamp >= ?")
        params.append(start)
    if end is not None:
        clauses.append("timestamp <= ?")
        params.append(end)

    query = (
        "SELECT timestamp, open, high, low, close, volume FROM candles "
        f"WHERE {' AND '.join(clauses)} ORDER BY timestamp ASC"
    )
    try:
        with sqlite3.connect(db_path) as connection:
            rows = connection.execute(query, params).fetchall()
    except sqlite3.OperationalError as exc:
        # Missing "candles" table, incompatible schema, corruption, lock, etc.
        raise ChartReadError(f"failed to read candles from {db_path}: {exc}") from exc

    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame.set_index("timestamp")


def load_candles_range(
    db_path: Path,
    symbol: str,
    timeframe: str,
    *,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """Load every stored candle for symbol/timeframe within an optional [start, end] bound.

    Unlike load_candles() (most-recent-N rows), this returns a full
    historical slice, oldest first -- for Backtest, which replays a
    specific date range rather than "the latest N bars". Read-only: never
    creates the DB file/table as a side effect (unlike load_candles(),
    which calls init_db()).

    Lenient by contract: a missing DB file / table or any read error yields an
    empty DataFrame (existing callers -- e.g. Backtest -- rely on this). Callers
    that must distinguish a read failure from a genuine zero-row read should use
    :func:`load_candles_range_strict`, which raises :class:`ChartReadError`.

    ``start``/``end`` are compared as strings against the stored
    ``timestamp`` column, so callers must pass the same canonical
    ``to_utc_iso_z()``-formatted form the candles were saved with (naive
    or differently-formatted bounds can silently mis-compare against the
    zero-padded, "Z"-suffixed stored strings).
    """
    try:
        return load_candles_range_strict(db_path, symbol, timeframe, start=start, end=end)
    except ChartReadError:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def insert_chart_snapshot(
    db_path: Path,
    *,
    symbol: str,
    timeframe: str,
    snapshot_at: str,
    s3_bucket: str,
    s3_key: str,
    captured_at: str | None = None,
    candle_time: str | None = None,
    image_url: str | None = None,
    image_s3_key: str | None = None,
    ohlcv_json: str | None = None,
    indicators_json: str | None = None,
    snapshot_id: str | None = None,
) -> str:
    init_db(db_path)
    row_id = snapshot_id or str(uuid.uuid4())
    now = to_utc_iso_z(pd.Timestamp.now(tz=UTC))
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO chart_snapshots
            (id, symbol, timeframe, snapshot_at, s3_bucket, s3_key, captured_at, candle_time, image_url, image_s3_key, ohlcv_json, indicators_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                symbol.upper(),
                timeframe,
                snapshot_at,
                s3_bucket,
                s3_key,
                captured_at,
                candle_time,
                image_url,
                image_s3_key or s3_key,
                ohlcv_json,
                indicators_json,
                now,
            ),
        )
    return row_id


def update_chart_snapshot_image_s3_key(db_path: Path, snapshot_id: str, image_s3_key: str) -> None:
    init_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE chart_snapshots SET image_s3_key = ? WHERE id = ?",
            (image_s3_key, snapshot_id),
        )


def update_chart_snapshot_s3_metadata(db_path: Path, snapshot_id: str, *, s3_bucket: str, s3_key: str) -> None:
    init_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE chart_snapshots
            SET s3_bucket = ?, s3_key = ?, image_s3_key = ?
            WHERE id = ?
            """,
            (s3_bucket, s3_key, s3_key, snapshot_id),
        )


def get_chart_snapshot_image_s3_key(db_path: Path, snapshot_id: str) -> str | None:
    init_db(db_path)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT image_s3_key FROM chart_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
    return None if row is None else row[0]


def insert_alert(
    db_path: Path,
    *,
    alert_type: str,
    symbol: str,
    timeframe: str,
    occurred_at: str,
    candle_time: str,
    snapshot_id: str,
    metadata_json: str | None = None,
    alert_id: str | None = None,
) -> str:
    init_db(db_path)
    row_id = alert_id or str(uuid.uuid4())
    now = to_utc_iso_z(pd.Timestamp.now(tz=UTC))
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO alerts
            (id, alert_type, symbol, timeframe, occurred_at, candle_time, snapshot_id, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                alert_type,
                symbol.upper(),
                timeframe,
                occurred_at,
                candle_time,
                snapshot_id,
                metadata_json,
                now,
            ),
        )
    return row_id


def link_alert_chart_snapshot(db_path: Path, *, alert_id: str, chart_snapshot_id: str) -> None:
    init_db(db_path)
    now = to_utc_iso_z(pd.Timestamp.now(tz=UTC))
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO alert_chart_snapshots
            (alert_id, chart_snapshot_id, created_at)
            VALUES (?, ?, ?)
            """,
            (alert_id, chart_snapshot_id, now),
        )


def collect_target(target: ChartTarget, db_path: Path, *, period: str = DEFAULT_PERIOD) -> int:
    frame = fetch_ohlcv(target, period=period)
    return save_candles(db_path, candles_from_frame(target, frame))


def collect_all(targets: list[ChartTarget], db_path: Path, *, period: str = DEFAULT_PERIOD) -> dict[str, int]:
    return {
        f"{target.symbol}_{target.timeframe}": collect_target(target, db_path, period=period)
        for target in targets
    }
