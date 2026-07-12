"""SQLite storage for Paper Signal records.

This module only stores and reads ``paper_only`` hypothesis records. It
never talks to a broker/order API and never stores order, position, lot, or
leverage data.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "paper_signals.sqlite3"

# Issue #41 review: tolerate transient SQLite locks under concurrent writers
# (multiple cycles / threads / service instances) instead of erroring out.
BUSY_TIMEOUT_SECONDS = 30

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import signal_models  # noqa: E402  (canonical Issue #27 field names + default values)

# Issue #27: the four classification columns, in canonical order, each with the
# current Paper Signal's value as a SQLite constant DEFAULT. Reusing
# signal_models keeps these strings defined in exactly one place. Values are
# fixed, quote-free identifiers, so embedding them in the DDL below is safe.
CLASSIFICATION_COLUMN_DEFAULTS = signal_models.CLASSIFICATION_FIELD_DEFAULTS


def _classification_columns_ddl() -> str:
    """`name TEXT NOT NULL DEFAULT '...'` fragments for the CREATE TABLE body."""
    return ",\n                ".join(
        f"{name} TEXT NOT NULL DEFAULT '{default}'"
        for name, default in CLASSIFICATION_COLUMN_DEFAULTS.items()
    )


def _migrate_add_classification_columns(connection: sqlite3.Connection) -> None:
    """Idempotently add any missing Issue #27 column to an existing table.

    ``CREATE TABLE IF NOT EXISTS`` never alters an already-existing table, so a
    DB created before Issue #27 keeps its old 13-column schema. This inspects
    ``PRAGMA table_info`` and ``ALTER TABLE ... ADD COLUMN`` only for columns
    that are actually missing -- safe to run on every init_db(), on both new
    and legacy DBs, any number of times. ALTER appends the four columns *after*
    ``status``, so a migrated legacy DB ends with exactly the same column order
    as a freshly created one (see init_db()'s CREATE TABLE). The ``NOT NULL
    DEFAULT '<const>'`` clause backfills every existing row with a safe value in
    the same step.
    """
    existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(paper_signals)")}
    for name, default in CLASSIFICATION_COLUMN_DEFAULTS.items():
        if name not in existing_columns:
            connection.execute(
                f"ALTER TABLE paper_signals ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'"
            )


# Issue #41: the Paper Signal resident service keys idempotency on the target
# *confirmed candle*, not a wall-clock time window. ``target_candle_utc`` records
# which closed candle a signal was generated for, and the UNIQUE index below makes
# "one signal per (strategy_profile, symbol, timeframe, target candle)" a
# database-level guarantee -- so concurrent cycles or multiple service instances
# can never persist or (therefore) re-notify a duplicate. Legacy rows predating
# this column keep ``target_candle_utc = NULL``; SQLite treats NULLs as distinct in
# a UNIQUE index, so existing data is never rejected by the new constraint.
TARGET_CANDLE_COLUMN = "target_candle_utc"


def _migrate_add_target_candle_column(connection: sqlite3.Connection) -> None:
    """Idempotently add the Issue #41 ``target_candle_utc`` column if missing.

    Runs *after* the Issue #27 classification migration so, on both a fresh
    CREATE TABLE and an ALTER-migrated legacy/partial DB, ``target_candle_utc``
    is always the final column -- keeping the physical column order identical
    regardless of how the DB came to be. Safe to run any number of times.
    """
    existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(paper_signals)")}
    if TARGET_CANDLE_COLUMN not in existing_columns:
        connection.execute(f"ALTER TABLE paper_signals ADD COLUMN {TARGET_CANDLE_COLUMN} TEXT")


def _backfill_classification_defaults(connection: sqlite3.Connection) -> None:
    """Fill NULL/empty classification values with the safe defaults.

    ``_migrate_add_classification_columns()`` only backfills rows when it adds a
    brand-new column. A partial/intermediate schema where a classification
    column already exists but holds NULL or empty strings would otherwise keep
    those bad values. This UPDATE closes that gap for all four columns, only
    touching NULL/empty cells (so a real value is never overwritten) and staying
    a no-op on re-run. Column names come from a fixed constant, never user input.
    """
    for name, default in CLASSIFICATION_COLUMN_DEFAULTS.items():
        connection.execute(
            f"UPDATE paper_signals SET {name} = ? WHERE {name} IS NULL OR {name} = ''",
            (default,),
        )


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        # ``status`` is placed *before* the four classification columns so this
        # fresh-DB column order matches a legacy DB migrated by ALTER TABLE
        # (which appends the four columns after the original trailing
        # ``status``). See Issue #27 review Major 2.
        connection.execute(
            f"""
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
                status TEXT NOT NULL,
                {_classification_columns_ddl()},
                {TARGET_CANDLE_COLUMN} TEXT
            )
            """
        )
        # Bring a pre-Issue-#27 DB up to the current schema (no-op on new DBs),
        # then repair any NULL/empty classification values left by a partial
        # schema. Both steps are idempotent. The Issue #41 target-candle column is
        # added last so fresh and ALTER-migrated DBs share one column order.
        _migrate_add_classification_columns(connection)
        _migrate_add_target_candle_column(connection)
        _backfill_classification_defaults(connection)
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_paper_signals_dedup
            ON paper_signals (symbol, timeframe, side, created_at_utc)
            """
        )
        # Issue #41: DB-level idempotency for the resident service. One Paper
        # Signal per (strategy_profile, symbol, timeframe, target candle). NULL
        # target_candle_utc (legacy rows) are treated as distinct by SQLite, so
        # this never conflicts with pre-#41 data.
        connection.execute(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_signals_candle_unique
            ON paper_signals (strategy_profile, symbol, timeframe, {TARGET_CANDLE_COLUMN})
            """
        )


def save_signal(signal: dict, *, db_path: Path = DEFAULT_DB_PATH) -> None:
    init_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO paper_signals
            (signal_id, created_at_utc, symbol, timeframe, side, market_bias,
             confidence, macd_state, trend, state, freshness, reason,
             signal_type, trigger_type, alert_source, strategy_profile, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                # .get() fallback keeps a legacy signal dict (built before the
                # Issue #27 fields existed) insertable with safe defaults.
                signal.get("signal_type", signal_models.SIGNAL_TYPE_TREND_FOLLOW),
                signal.get("trigger_type", signal_models.TRIGGER_TYPE_MACD_STATE_ALIGNMENT),
                signal.get("alert_source", signal_models.ALERT_SOURCE_COMPOSITE),
                signal.get("strategy_profile", signal_models.STRATEGY_PROFILE_MARKET_BIAS_MACD_ALIGNMENT_V1),
                signal["status"],
            ),
        )


def save_signal_for_candle(
    signal: dict,
    *,
    target_candle_utc: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> bool:
    """Persist *signal* for one confirmed candle, idempotently.

    Issue #41: unlike :func:`save_signal` (which upserts on ``signal_id``), this
    relies on the UNIQUE index over
    ``(strategy_profile, symbol, timeframe, target_candle_utc)``. The insert uses
    ``ON CONFLICT DO NOTHING`` so a second attempt for the same target candle --
    from the next cycle, a concurrent cycle, or a second service instance -- is a
    no-op rather than a duplicate row (and, because the caller only notifies on a
    fresh insert, never a duplicate Discord message).

    Returns ``True`` when a new row was inserted, ``False`` when the target candle
    was already stored. Never raises on a duplicate.

    Concurrency (Issue #41 review): a ``busy_timeout`` lets genuinely concurrent
    writers (a second cycle, thread, or process) serialize instead of failing on
    a transient lock. ``ON CONFLICT DO NOTHING`` makes the loser of a race a no-op
    (rowcount 0). As a defensive belt-and-braces, a raised ``IntegrityError`` (e.g.
    if the UNIQUE index were reached without the ON CONFLICT clause taking effect)
    is also reported as a duplicate rather than propagated -- so the invariant
    "one row, one notification per target candle" holds under real contention.
    """
    init_db(db_path)
    with sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_SECONDS) as connection:
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}")
        try:
            cursor = connection.execute(
                f"""
                INSERT INTO paper_signals
                (signal_id, created_at_utc, symbol, timeframe, side, market_bias,
                 confidence, macd_state, trend, state, freshness, reason,
                 signal_type, trigger_type, alert_source, strategy_profile, status,
                 {TARGET_CANDLE_COLUMN})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
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
                    signal.get("signal_type", signal_models.SIGNAL_TYPE_TREND_FOLLOW),
                    signal.get("trigger_type", signal_models.TRIGGER_TYPE_MACD_STATE_ALIGNMENT),
                    signal.get("alert_source", signal_models.ALERT_SOURCE_COMPOSITE),
                    signal.get("strategy_profile", signal_models.STRATEGY_PROFILE_MARKET_BIAS_MACD_ALIGNMENT_V1),
                    signal["status"],
                    target_candle_utc,
                ),
            )
        except sqlite3.IntegrityError:
            return False
    return cursor.rowcount > 0


def signal_exists_for_candle(
    strategy_profile: str,
    symbol: str,
    timeframe: str,
    target_candle_utc: str,
    *,
    db_path: Path = DEFAULT_DB_PATH,
) -> bool:
    """Return True if a Paper Signal is already stored for this target candle.

    Issue #41: read-only duplicate probe used by ``--dry-run`` to report what
    *would* happen without any side effect. Deliberately never creates the DB
    file/table (mirrors ``market_bias_store.get_market_bias_as_of``): returns
    ``False`` when the DB or table does not exist yet, so a dry-run stays free of
    persistent side effects.
    """
    if not db_path.exists():
        return False
    try:
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                f"""
                SELECT 1 FROM paper_signals
                WHERE strategy_profile = ? AND symbol = ? AND timeframe = ?
                  AND {TARGET_CANDLE_COLUMN} = ?
                LIMIT 1
                """,
                (strategy_profile, symbol, timeframe, target_candle_utc),
            ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


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
