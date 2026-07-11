"""Shared, dependency-light freshness / bias-weight / confirmed-candle rules.

Issue #28: Live (``chart_snapshot.build_indicator_snapshot``) and Backtest
(``backtest/warmup._build_snapshot``) must decide freshness, bias_weight, and
*which candle is the latest confirmed one* through ONE rule, so their Signal
Candidates cannot diverge. That rule lives here as pure functions that take an
explicit ``evaluation_time``:

  * Live passes the current wall-clock time (or a caller-supplied "now").
  * Backtest passes each replayed bar's evaluation time (its close time).

There is no "Backtest is always Fresh" branch: identical
``(candle_timestamp, evaluation_time)`` always yields identical freshness /
bias_weight, whichever caller asked.

This module intentionally imports only ``pandas`` + ``datetime`` (no yfinance,
matplotlib, boto3, or any provider/broker code), so the Backtest signal path
can reuse it without pulling in Live's rendering / upload dependencies.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

import pandas as pd

JST = timezone(timedelta(hours=9), name="JST")
STALE_AFTER = timedelta(hours=24)
MARKET_CLOSED_BIAS_WEIGHT = 0.25

FRESH = "Fresh"
STALE = "Stale"
WEEKEND_CLOSED = "Weekend Closed"
MARKET_CLOSED = "Market Closed"

# Timeframe labels this system uses: "15m", "1h", "4h" (also tolerates "1d").
_TIMEFRAME_RE = re.compile(r"^\s*(\d+)\s*([mhd])\s*$", re.IGNORECASE)
_TIMEFRAME_UNITS = {"m": "min", "h": "h", "d": "D"}


def _to_utc(value) -> pd.Timestamp:
    """Coerce *value* to a UTC-aware ``pd.Timestamp`` (naive is treated as UTC)."""
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize(UTC)
    return timestamp.tz_convert(UTC)


def parse_timestamp_utc(timestamp) -> pd.Timestamp:
    """Parse a candle timestamp into a UTC-aware ``pd.Timestamp``."""
    return _to_utc(timestamp)


def _resolve_now(now_utc=None) -> pd.Timestamp:
    return _to_utc(now_utc if now_utc is not None else datetime.now(UTC))


def is_market_closed_window(now_utc: datetime | pd.Timestamp | None = None) -> tuple[bool, str | None]:
    """Return ``(is_closed, label)`` for the JST FX weekend / Monday-open window."""
    now_jst = _resolve_now(now_utc).tz_convert(JST)
    weekday = now_jst.weekday()
    if weekday in {5, 6}:
        return True, WEEKEND_CLOSED
    if weekday == 0 and now_jst.hour < 7:
        return True, MARKET_CLOSED
    return False, None


def freshness_from_timestamp(
    timestamp: str, *, now_utc: datetime | pd.Timestamp | None = None
) -> tuple[str, int]:
    """Return ``(freshness, age_seconds)`` for a candle at *timestamp* observed at *now_utc*."""
    now = _resolve_now(now_utc)
    age = max(timedelta(0), now.to_pydatetime() - parse_timestamp_utc(timestamp).to_pydatetime())
    is_closed, closed_label = is_market_closed_window(now)
    if is_closed:
        freshness = closed_label or MARKET_CLOSED
    else:
        freshness = STALE if age > STALE_AFTER else FRESH
    return freshness, int(age.total_seconds())


def bias_weight_for_freshness(freshness: str) -> float:
    """Map a freshness label to its bias weight (the single source of this rule)."""
    if freshness in {WEEKEND_CLOSED, MARKET_CLOSED}:
        return MARKET_CLOSED_BIAS_WEIGHT
    if freshness == STALE:
        return 0.0
    return 1.0


def resolve_freshness(candle_timestamp, *, evaluation_time) -> tuple[str, int, float]:
    """Return ``(freshness, age_seconds, bias_weight)`` for a candle observed at *evaluation_time*.

    THE shared entry point for both Live and Backtest. Given the same
    ``candle_timestamp`` and ``evaluation_time`` it always returns the same
    triple, so the two paths can never disagree on freshness or bias weight.
    """
    freshness, age_seconds = freshness_from_timestamp(candle_timestamp, now_utc=evaluation_time)
    return freshness, age_seconds, bias_weight_for_freshness(freshness)


def timeframe_to_timedelta(timeframe: str) -> pd.Timedelta:
    """Parse a timeframe label (e.g. ``"15m"``, ``"1h"``, ``"4h"``) into a ``pd.Timedelta``."""
    match = _TIMEFRAME_RE.match(timeframe or "")
    if not match:
        raise ValueError(f"unrecognized timeframe: {timeframe!r}")
    quantity = int(match.group(1))
    unit = _TIMEFRAME_UNITS[match.group(2).lower()]
    return pd.Timedelta(f"{quantity}{unit}")


def candle_close_time(candle_timestamp, timeframe: str) -> pd.Timestamp:
    """Return the UTC close time of a candle labelled with its open *candle_timestamp*.

    A candle labelled with its open time ``T`` on *timeframe* covers
    ``[T, T + timeframe)`` and closes (becomes confirmed) at ``T + timeframe``.
    """
    return parse_timestamp_utc(candle_timestamp) + timeframe_to_timedelta(timeframe)


def select_confirmed_candles(candles: pd.DataFrame, *, timeframe: str, evaluation_time) -> pd.DataFrame:
    """Return only the candles that have fully CLOSED by *evaluation_time*.

    A candle labelled with its open time ``T`` closes at ``T + timeframe``; it
    is *confirmed* (usable for a signal) once ``evaluation_time >= T + timeframe``
    and is otherwise still forming and must never drive a signal. Live and
    Backtest both call this so they exclude the exact same forming candle.
    """
    if candles.empty:
        return candles
    delta = timeframe_to_timedelta(timeframe)
    now = _resolve_now(evaluation_time)
    index = candles.index
    close_times = (index.tz_localize(UTC) if index.tz is None else index.tz_convert(UTC)) + delta
    return candles[close_times <= now]
