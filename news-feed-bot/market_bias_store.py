"""SQLite storage for persisted Market Bias records.

Market Bias is an observation/classification log only (see
docs/architecture/market_bias.md). This module stores that log so a
Backtest layer can later ask "what was the market state at time T" -- it
never places orders, sizes positions, or generates a trade recommendation.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from market_bias_models import effective_bias_scopes, normalize_bias_symbol, normalize_bias_timeframe

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
DEFAULT_DB_PATH = BASE_DIR / "data" / "market_bias.sqlite3"


def _timestamp_instant(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Market Bias timestamp must be a non-empty ISO-8601 string")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _canonical_timestamp(value: str) -> str:
    return _timestamp_instant(value).isoformat().replace("+00:00", "Z")


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
                _canonical_timestamp(record["evaluated_at"]),
                normalize_bias_symbol(record["symbol"]),
                normalize_bias_timeframe(record["timeframe"]),
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

    symbol = normalize_bias_symbol(symbol)
    timeframe = normalize_bias_timeframe(timeframe)
    at = _canonical_timestamp(at)

    try:
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT * FROM market_bias
                WHERE symbol = ? AND timeframe = ?
                  AND julianday(evaluated_at) <= julianday(?)
                ORDER BY julianday(evaluated_at) DESC, market_bias_id DESC
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

    symbol = normalize_bias_symbol(symbol)
    timeframe = normalize_bias_timeframe(timeframe)

    try:
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM market_bias
                WHERE symbol = ? AND timeframe = ?
                  AND julianday(evaluated_at) IS NOT NULL
                ORDER BY julianday(evaluated_at) ASC, market_bias_id ASC
                """,
                (symbol, timeframe),
            ).fetchall()
    except sqlite3.OperationalError:
        return []

    return [dict(row) for row in rows]


def get_effective_market_bias_as_of(
    symbol: str,
    timeframe: str,
    at: str,
    *,
    db_path: Path | None = None,
) -> dict | None:
    """Point-in-time Market Bias for ``symbol``/``timeframe`` with GLOBAL fallback.

    Issue #40: resolves the reference priority (see
    ``market_bias_models.effective_bias_scopes``) at a single instant ``at``:

    1. requested ``symbol`` + ``timeframe``
    2. requested ``symbol`` + ``1h``
    3. ``GLOBAL`` + ``timeframe``
    4. ``GLOBAL`` + ``1h``

    The first scope with a record whose ``evaluated_at <= at`` wins; duplicate
    scopes are never queried twice. Each scope delegates to
    :func:`get_market_bias_as_of`, so this inherits its exact
    ``evaluated_at <= at`` (never-future) filter and same-timestamp
    ``market_bias_id DESC`` tie-break. The returned row keeps its own ``symbol``
    / ``timeframe``, so the caller can tell which scope was adopted (e.g. whether
    GLOBAL was used as a fallback).

    Side-effect-free on read, exactly like :func:`get_market_bias_as_of`: never
    creates the DB file/table and returns ``None`` (never raises) when the DB or
    table does not exist yet, or no scope has a record at or before ``at`` --
    callers must treat that as "bias unknown for this period", not an error.
    """
    db_path = Path(db_path) if db_path is not None else market_bias_db_path()
    if not db_path.exists():
        return None

    for scope_symbol, scope_timeframe in effective_bias_scopes(symbol, timeframe):
        record = get_market_bias_as_of(scope_symbol, scope_timeframe, at, db_path=db_path)
        if record is not None:
            return record
    return None


def _latest_record_at_or_before(series: list[dict], at: str | datetime) -> dict | None:
    """Return the last record in ``series`` with ``evaluated_at <= at``, else None.

    ``series`` must already be ordered ``evaluated_at ASC, market_bias_id ASC``
    (the order :func:`load_market_bias_series` guarantees). Walking to the last
    row that satisfies ``evaluated_at <= at`` therefore selects, within the
    latest matching ``evaluated_at`` group, the greatest ``market_bias_id`` --
    byte-for-byte the record :func:`get_market_bias_as_of` would return for the
    same instant. ``evaluated_at`` is compared as an ISO-8601 string, identical
    to the SQL ``evaluated_at <= ?`` comparison used everywhere else here.
    """
    at_instant = _timestamp_instant(at) if isinstance(at, str) else at
    chosen: dict | None = None
    for row in series:
        if _timestamp_instant(row["evaluated_at"]) <= at_instant:
            chosen = row
        else:
            break
    return chosen


def load_effective_market_bias_series(
    symbol: str,
    timeframe: str,
    *,
    db_path: Path | None = None,
) -> list[dict]:
    """Return a merged, priority-resolved Market Bias series for time-series use.

    Issue #40: a time-series Backtest must apply the same per-instant fallback
    priority as :func:`get_effective_market_bias_as_of` -- *at every point in
    time*, not once. Naively concatenating the symbol-specific and GLOBAL series
    and sorting by ``evaluated_at`` is wrong: a GLOBAL record saved *after* a
    symbol-specific bias became effective would incorrectly override it.

    Instead, each scope's full history is preloaded once (single connection per
    scope, never re-opened per bar), and the effective bias is resolved at every
    distinct ``evaluated_at`` breakpoint across all scopes. The result is a
    minimal list -- one record per change of effective bias, carrying that
    record's own ``symbol`` / ``timeframe`` / ``evaluated_at`` / ``market_bias_id``
    -- with strictly increasing ``evaluated_at``. That means:

    * before any symbol-specific bias exists, GLOBAL is used;
    * once a symbol-specific bias exists at/before the instant, it is used and
      is carried forward between symbol-specific records;
    * a newer GLOBAL record does NOT override an already-effective symbol bias;
    * no future record is ever used.

    A plain point-in-time consumer (e.g. ``runner.TimeSeriesBiasLookup``) can
    binary-search this merged series unchanged and get the correct effective
    bias for any bar close.

    Side-effect-free on read, like the other read helpers here: returns ``[]``
    (never raises, never creates the DB) when the DB/table is missing or no
    scope has any record.
    """
    db_path = Path(db_path) if db_path is not None else market_bias_db_path()
    if not db_path.exists():
        return []

    # Preload each scope's history once, keeping them in strict priority order.
    scope_series: list[list[dict]] = []
    for scope_symbol, scope_timeframe in effective_bias_scopes(symbol, timeframe):
        series = load_market_bias_series(scope_symbol, scope_timeframe, db_path=db_path)
        if series:
            scope_series.append(series)
    if not scope_series:
        return []

    # Every record's evaluated_at is a candidate "the effective bias might change
    # here" breakpoint. Resolve the effective bias at each and keep it only when
    # it differs from the previous breakpoint's, yielding a minimal merged series.
    breakpoints = sorted({_timestamp_instant(row["evaluated_at"]) for series in scope_series for row in series})
    merged: list[dict] = []
    for at in breakpoints:
        effective: dict | None = None
        for series in scope_series:  # priority order: symbol scopes before GLOBAL
            effective = _latest_record_at_or_before(series, at)
            if effective is not None:
                break
        if effective is None:
            continue
        if merged and merged[-1] is effective:
            continue
        merged.append(effective)
    return merged


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
