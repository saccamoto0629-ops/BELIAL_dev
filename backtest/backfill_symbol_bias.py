"""Backfill a per-symbol, historical Market Bias series from stored candles.

*** PAPER-ONLY OFFLINE TOOL. NOT AN EXECUTION LAYER. ***

Issue #40 lets a Backtest/Paper Signal prefer a symbol-scoped Market Bias over
the shared GLOBAL bias, but only if such per-symbol records actually exist in
the Market Bias DB. Live ``market_snapshot`` records the bias for "now" and
never backfills history, so a time-series Backtest over a past year sees only
GLOBAL rows and #40 silently falls back to GLOBAL (``global_fallback=True``).

This tool reconstructs a per-symbol bias *series* for each requested instrument
by replaying its own candles: at each 1h bar it rebuilds the exact per-symbol
chart snapshot Live would have produced (same ``build_indicator_snapshot`` /
``classify_market_bias_for_symbol`` code, same confirmed-candle + freshness
rules) and persists the resulting Bull/Bear/Neutral under the normalized symbol
at the representative 1h timeframe, timestamped at that bar's close.

Design decisions (deliberate, documented so results are interpretable):

* **Chart-only directional score.** The cross-asset core signals Live also
  feeds ``classify_market_bias`` (VIX, S&P500, Nasdaq, US10Y, Dollar, Oil, TV
  recommendations...) are NOT stored historically, so we cannot replay them.
  Rather than fabricate approximate macro history, we pass ``market_data =
  fred_data = tv_data = None`` and let each symbol's directional bias come purely
  from its OWN chart trend + MACD -- which is exactly the per-symbol axis Issue
  #40 isolates (``classify_market_bias_for_symbol`` filters ``chart_data`` down
  to the symbol's own snapshots anyway). Consequence of the classifier's
  evidence rules: a directional Bull needs chart trend ``Bull`` AND MACD
  ``Bullish`` (Bear needs both bearish); otherwise the bar is ``Neutral``. That
  is a sensible "confirmed trend" gate and means Neutral bars generate no
  signal, the safe default.

* **Point-in-time, no look-ahead.** Each bar is evaluated with candles only up
  to and including that bar (a bounded 200-bar tail, matching Live's
  ``load_candles(limit=200)`` so indicators are byte-identical), at an
  ``evaluation_time`` equal to that bar's close. Future bars never influence a
  past record.

* **On-change de-duplication.** Consecutive bars with the same bias label are
  collapsed to a single record at the first bar of the run (the time-series
  lookup carries the last bias forward, so this is equivalent to writing every
  bar but keeps the DB small and the ``evaluated_at`` breakpoints meaningful).

* **Idempotent re-runs.** Records use a deterministic id
  (``backfill:<symbol>:<tf>:<evaluated_at>``) and, before writing, existing rows
  for exactly the target canonical symbols are deleted (GLOBAL is never
  touched), so re-running produces a clean series instead of duplicates.

It never sends Discord, calls a broker, or writes anything except per-symbol
rows into the Market Bias DB (and nothing at all in ``--dry-run``).
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
MARKET_WATCH_DIR = REPO_ROOT / "market-watch"
NEWS_FEED_BOT_DIR = REPO_ROOT / "news-feed-bot"

for _dir in (BASE_DIR, MARKET_WATCH_DIR, NEWS_FEED_BOT_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import chart_collector  # noqa: E402  (market-watch)
from chart_models import ChartTarget  # noqa: E402  (market-watch)
from chart_snapshot import build_indicator_snapshot  # noqa: E402  (market-watch)
from freshness import candle_close_time  # noqa: E402  (market-watch)
from market_bias import classify_market_bias_for_symbol  # noqa: E402  (news-feed-bot)
from market_bias_models import (  # noqa: E402  (news-feed-bot)
    GLOBAL_SCOPE_SYMBOL,
    REPRESENTATIVE_TIMEFRAME,
    create_market_bias_record,
    normalize_bias_symbol,
    normalize_bias_timeframe,
)
from market_bias_store import (  # noqa: E402  (news-feed-bot)
    init_db,
    market_bias_db_path,
)

logger = logging.getLogger("backtest.backfill_symbol_bias")

EXIT_OK = 0
EXIT_USAGE_ERROR = 2
EXIT_NO_DATA = 3

# Match Live's per-snapshot candle fetch (chart_collector.load_candles' default
# limit) so indicator values for the latest row are identical to what Live saw.
DEFAULT_WINDOW = 200

# How long to wait for a competing writer's lock before giving up. The live
# news-feed-bot writes GLOBAL/per-symbol bias to this same DB on a schedule, so
# a bulk backfill must tolerate a transient write lock rather than fail.
BUSY_TIMEOUT_MS = 30_000

# Columns mirror market_bias_store.save_market_bias's INSERT exactly. We insert
# through a single long-lived connection here (rather than calling
# save_market_bias per record, which re-opens the DB + re-runs init_db every
# time) so a year-long backfill does thousands of inserts over ONE file handle
# in one transaction per symbol -- far fewer opens, and no per-record
# reconnect storm that can exhaust file descriptors ("unable to open database
# file").
_INSERT_SQL = """
    INSERT OR REPLACE INTO market_bias
    (market_bias_id, evaluated_at, symbol, timeframe, market_bias, confidence,
     trend, risk, risk_confidence, volatility, volatility_confidence,
     reasons_json, risk_reasons_json, volatility_reasons_json, warnings_json,
     source_snapshot_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _insert_record(connection: sqlite3.Connection, record: dict) -> None:
    connection.execute(
        _INSERT_SQL,
        (
            record["market_bias_id"],
            record["evaluated_at"],
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


def _to_z(timestamp: pd.Timestamp) -> str:
    """Canonical ``...Z`` UTC ISO-8601 string for an ``evaluated_at`` value."""
    ts = timestamp.tz_convert("UTC") if timestamp.tzinfo else timestamp.tz_localize("UTC")
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_symbols(raw: str) -> list[str]:
    symbols = [normalize_bias_symbol(part) for part in raw.split(",") if part.strip()]
    deduped: list[str] = []
    for symbol in symbols:
        if symbol == GLOBAL_SCOPE_SYMBOL:
            raise argparse.ArgumentTypeError(
                "refusing to backfill the GLOBAL scope; pass real instrument symbols "
                "(e.g. USD_JPY,XAU_USD,SPX)"
            )
        if symbol not in deduped:
            deduped.append(symbol)
    if not deduped:
        raise argparse.ArgumentTypeError("no symbols given")
    return deduped


def _delete_existing(db_path: Path, symbols: list[str], timeframe: str) -> int:
    """Delete existing (non-GLOBAL) rows for the target symbols/timeframe.

    Guarded so the GLOBAL market-wide series is never removed.
    """
    if not db_path.exists():
        return 0
    placeholders = ",".join("?" for _ in symbols)
    deleted = 0
    with sqlite3.connect(db_path) as connection:
        cursor = connection.execute(
            f"DELETE FROM market_bias WHERE timeframe = ? AND symbol != ? "
            f"AND symbol IN ({placeholders})",
            (timeframe, GLOBAL_SCOPE_SYMBOL, *symbols),
        )
        deleted = cursor.rowcount or 0
    return deleted


def backfill_symbol(
    symbol: str,
    *,
    chart_db_path: Path,
    connection: sqlite3.Connection | None,
    timeframe: str,
    window: int,
    from_bound: pd.Timestamp | None,
    to_bound: pd.Timestamp | None,
) -> dict:
    """Backfill one symbol; return a small stats dict for reporting.

    ``connection`` is a single open Market Bias DB connection (shared across all
    symbols in the run) or ``None`` for a dry run that writes nothing.
    """
    end_str = _to_z(to_bound) if to_bound is not None else None
    candles = chart_collector.load_candles_range(
        chart_db_path, symbol, timeframe, start=None, end=end_str
    )
    stats = {"symbol": symbol, "candles": len(candles), "written": 0,
             "Bull": 0, "Bear": 0, "Neutral": 0, "skipped_none": 0}
    if candles.empty:
        logger.warning("[backfill] no candles for %s %s in %s", symbol, timeframe, chart_db_path)
        return stats

    target = ChartTarget(symbol=symbol, ticker="", timeframe=timeframe)
    last_label: str | None = None

    for i in range(len(candles)):
        bar_open = candles.index[i]
        close_time = candle_close_time(bar_open, timeframe)
        if from_bound is not None and close_time < from_bound:
            continue
        if to_bound is not None and close_time > to_bound:
            break

        tail = candles.iloc[max(0, i - window + 1): i + 1]
        snapshot = build_indicator_snapshot(target, tail, now_utc=close_time)
        if snapshot is None:
            stats["skipped_none"] += 1
            continue

        bias = classify_market_bias_for_symbol(
            symbol, None, None, None, chart_data=[snapshot]
        )
        label = bias.get("market_bias", "Neutral")
        stats[label] = stats.get(label, 0) + 1

        # On-change de-dup: only persist when the effective label changes.
        if label == last_label:
            continue
        last_label = label

        evaluated_at = _to_z(close_time)
        record = create_market_bias_record(
            bias,
            symbol=symbol,
            timeframe=timeframe,
            source_snapshot_id=None,
            market_bias_id=f"backfill:{symbol}:{normalize_bias_timeframe(timeframe)}:{evaluated_at}",
            evaluated_at=evaluated_at,
        )
        stats["written"] += 1
        if connection is not None:
            _insert_record(connection, record)

    return stats


def _parse_bound(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backtest.backfill_symbol_bias",
        description="Backfill a per-symbol historical Market Bias series from stored candles (Issue #40).",
    )
    parser.add_argument(
        "--symbols", required=True, type=_parse_symbols,
        help="Comma-separated instrument symbols, e.g. USD_JPY,XAU_USD,SPX (GLOBAL is rejected).",
    )
    parser.add_argument(
        "--timeframe", default=REPRESENTATIVE_TIMEFRAME,
        help=f"Candle timeframe to replay and the scope timeframe to store (default: {REPRESENTATIVE_TIMEFRAME}).",
    )
    parser.add_argument("--from", dest="from_time", default=None,
                        help="Only emit records whose bar close is at/after this ISO-8601 time (UTC if naive).")
    parser.add_argument("--to", dest="to_time", default=None,
                        help="Only replay candles whose close is at/before this ISO-8601 time (UTC if naive).")
    parser.add_argument("--db-path", default=None,
                        help="Chart DB (candles). Default: CHART_DB_PATH / market-watch/data/chart_data.sqlite3.")
    parser.add_argument("--market-bias-db-path", default=None,
                        help="Market Bias DB to write into. Default: MARKET_BIAS_DB_PATH / news-feed-bot/data/market_bias.sqlite3.")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                        help=f"Trailing candle window per evaluation (default: {DEFAULT_WINDOW}, matching Live).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify and report the distribution but write nothing to the DB.")
    parser.add_argument("--keep-existing", action="store_true",
                        help="Do NOT delete existing per-symbol rows first (default deletes them for a clean re-run).")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.window < 1:
        parser.error("--window must be >= 1")

    timeframe = normalize_bias_timeframe(args.timeframe)
    symbols: list[str] = args.symbols

    chart_db_path = (
        Path(args.db_path) if args.db_path
        else chart_collector.chart_db_path() if hasattr(chart_collector, "chart_db_path")
        else MARKET_WATCH_DIR / "data" / "chart_data.sqlite3"
    )
    if not chart_db_path.exists():
        logger.error("Chart DB not found: %s", chart_db_path)
        return EXIT_NO_DATA

    bias_db_path = (
        Path(args.market_bias_db_path) if args.market_bias_db_path else market_bias_db_path()
    )

    try:
        from_bound = _parse_bound(args.from_time)
        to_bound = _parse_bound(args.to_time)
    except ValueError as exc:
        parser.error(f"could not parse --from/--to: {exc}")
    if from_bound is not None and to_bound is not None and to_bound < from_bound:
        parser.error("--to must not be earlier than --from")

    connection: sqlite3.Connection | None = None
    if not args.dry_run:
        init_db(bias_db_path)
        if not args.keep_existing:
            deleted = _delete_existing(bias_db_path, symbols, timeframe)
            logger.info("Deleted %d existing per-symbol row(s) for %s @ %s", deleted, ",".join(symbols), timeframe)
        # One shared connection for the whole run (see _INSERT_SQL). busy_timeout
        # lets us wait out the live writer's lock instead of failing outright.
        connection = sqlite3.connect(bias_db_path, timeout=BUSY_TIMEOUT_MS / 1000)
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")

    grand_written = 0
    try:
        for symbol in symbols:
            stats = backfill_symbol(
                symbol,
                chart_db_path=chart_db_path,
                connection=connection,
                timeframe=timeframe,
                window=args.window,
                from_bound=from_bound,
                to_bound=to_bound,
            )
            if connection is not None:
                connection.commit()  # persist this symbol before moving on
            grand_written += stats["written"]
            logger.info(
                "%-8s candles=%-6d evaluated: Bull=%d Bear=%d Neutral=%d (skipped_none=%d) -> %d record(s)%s",
                stats["symbol"], stats["candles"], stats["Bull"], stats["Bear"], stats["Neutral"],
                stats["skipped_none"], stats["written"], " [DRY_RUN]" if args.dry_run else "",
            )
    finally:
        if connection is not None:
            connection.close()

    logger.info("%sTotal records %s: %d",
                "[DRY_RUN] " if args.dry_run else "",
                "that would be written" if args.dry_run else "written", grand_written)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
