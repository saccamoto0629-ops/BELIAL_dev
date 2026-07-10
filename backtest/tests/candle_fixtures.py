"""Shared, deliberately small OHLCV fixture builders for the Backtest test suite.

Issue #25: fixtures stay minimal -- hourly bars, constant volume, and a
price path a reviewer can read straight off the numbers. The warm-up block
is a one-line-describable steady uptrend (make_uptrend_candles); the part
that carries each test's intent is a short list of hand-written
(open, high, low, close) tuples (make_ohlcv_candles).

Every builder only ever writes to the db_path the caller passes in -- tests
must always point these at tmp_path, never at a real data/ directory.
"""

from __future__ import annotations

import pathlib
import sys

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = BOT_DIR.parent
MARKET_WATCH_DIR = REPO_ROOT / "market-watch"
NEWS_FEED_BOT_DIR = REPO_ROOT / "news-feed-bot"

for _dir in (BOT_DIR, MARKET_WATCH_DIR, NEWS_FEED_BOT_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import pandas as pd  # noqa: E402
import chart_collector as collector  # noqa: E402  (market-watch)
from chart_models import Candle  # noqa: E402  (market-watch)
import market_bias_store as bias_store  # noqa: E402  (news-feed-bot)

DEFAULT_START = "2026-05-27T00:00:00Z"


def make_uptrend_candles(n, *, start=DEFAULT_START, freq="1h", chart_symbol="USDJPY", timeframe="1h", step=0.05):
    """A steady uptrend from 150.0: bar i closes at 150 + (i+1)*step.

    After ~90 bars this is reliably Bull trend / Bullish MACD with every
    indicator warm (sma75 included) -- matches test_warmup.py's fixture
    shape, so it reliably produces 'long' candidates once ready, and it
    doubles as the standard warm-up block placed in front of hand-written
    validation bars.
    """
    index = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    candles = []
    price = 150.0
    for timestamp in index:
        price += step
        candles.append(
            Candle(
                symbol=chart_symbol,
                timeframe=timeframe,
                timestamp=collector.to_utc_iso_z(timestamp),
                open=price - 0.02,
                high=price + 0.05,
                low=price - 0.05,
                close=price,
                volume=100.0,
            )
        )
    return index, candles


def make_ohlcv_candles(bars, *, start, freq="1h", chart_symbol="USDJPY", timeframe="1h"):
    """Hand-written bars: each entry of *bars* is an (open, high, low, close) tuple.

    This is the reviewable part of a fixture -- a test's price story should
    be readable directly from these tuples.
    """
    index = pd.date_range(start, periods=len(bars), freq=freq, tz="UTC")
    candles = []
    for timestamp, (open_, high, low, close) in zip(index, bars):
        candles.append(
            Candle(
                symbol=chart_symbol,
                timeframe=timeframe,
                timestamp=collector.to_utc_iso_z(timestamp),
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=100.0,
            )
        )
    return index, candles


def save_candles(db_path, candles):
    """Save candles into db_path's candles table (INSERT OR REPLACE: re-seeding is safe)."""
    return collector.save_candles(db_path, candles)


def seed_chart_db(db_path, *, n=100, chart_symbol="USDJPY", timeframe="1h", step=0.05):
    """Save an n-bar uptrend into db_path and return its DatetimeIndex."""
    index, candles = make_uptrend_candles(n, chart_symbol=chart_symbol, timeframe=timeframe, step=step)
    collector.save_candles(db_path, candles)
    return index


def seed_bull_bias(db_path, *, evaluated_at, market_bias="Bull", confidence=0.8, bias_id="mb-1"):
    bias_store.save_market_bias(
        {
            "market_bias_id": bias_id,
            "evaluated_at": evaluated_at,
            "symbol": "GLOBAL",
            "timeframe": "1h",
            "market_bias": market_bias,
            "confidence": confidence,
            "trend": market_bias,
        },
        db_path=db_path,
    )
