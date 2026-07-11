"""Market state classification from already-computed chart indicators.

This is observation-only classification for Paper Signal / Backtest to
consume. It never implies entries, exits, lot sizing, stop-loss,
take-profit, or any other trade decision. Alert bots keep detecting and
notifying individual facts (MA touch, MACD cross, Bollinger break, RSI
zone); this module only summarizes several of those already-computed
indicators into one higher-level state label.
"""

from __future__ import annotations

import math
from typing import Literal, TypedDict, get_args

import pandas as pd

MarketState = Literal["trend_up", "trend_down", "range", "volatile", "squeeze", "unknown"]

# The full set of labels classify_market_state() can ever return, derived
# straight from the MarketState Literal so the two can never drift apart.
# Consumers that let a user pick states to allow/deny (e.g. Backtest's Issue #39
# ``--allowed-market-states`` filter) validate against this instead of
# re-listing the labels.
KNOWN_MARKET_STATES: tuple[MarketState, ...] = get_args(MarketState)

REQUIRED_FIELDS = (
    "close",
    "sma20",
    "sma75",
    "ema20",
    "macd_state",
    "bb_bandwidth",
    "bb_width_pct",
    "bb_position",
)

# bb_position boundaries reuse indicators.classify_bb_state()'s 0.2 / 0.8 split
# between "inside the band" and "near/beyond the band edge".
BAND_EDGE_POSITION = 0.8
RANGE_MACD_STATE = "Neutral"
MA_CONVERGENCE_THRESHOLD = 0.001  # 0.1% relative gap between adjacent MAs

DEFAULT_SQUEEZE_LOOKBACK = 20
DEFAULT_SQUEEZE_PERCENTILE = 0.10
DEFAULT_EXPANSION_LOOKBACK = 20
DEFAULT_EXPANSION_RATIO = 1.30


class MarketStateResult(TypedDict):
    market_state: MarketState
    confidence: float
    reasons: list[str]


def _unknown(reason: str) -> MarketStateResult:
    return {"market_state": "unknown", "confidence": 0.0, "reasons": [reason]}


def _relative_gap(a: float, b: float) -> float:
    if b == 0:
        return float("inf")
    return abs(a - b) / abs(b)


def _bandwidth_in_lower_percentile(
    bandwidth: pd.Series, position: int, *, lookback: int, percentile: float
) -> bool:
    """True when the current bandwidth sits at or below the percentile of its trailing window.

    Mirrors indicators.is_bollinger_squeeze()'s logic, but with a lighter
    default lookback tailored for a single-snapshot classification rather
    than a notification threshold.
    """
    current = bandwidth.iloc[position]
    if pd.isna(current):
        return False
    window = bandwidth.iloc[: position + 1].dropna().tail(lookback)
    if len(window) < lookback:
        return False
    return float(current) <= float(window.quantile(percentile))


def _bandwidth_expanded(bandwidth: pd.Series, position: int, *, lookback: int, ratio: float) -> bool:
    """True when the current bandwidth is at least ``ratio`` times the trailing median."""
    current = bandwidth.iloc[position]
    if pd.isna(current):
        return False
    window = bandwidth.iloc[max(0, position - lookback) : position].dropna()
    if window.empty:
        return False
    baseline = float(window.median())
    if baseline <= 0:
        return False
    return float(current) >= ratio * baseline


def classify_market_state(
    indicators: pd.DataFrame,
    *,
    squeeze_lookback: int = DEFAULT_SQUEEZE_LOOKBACK,
    squeeze_percentile: float = DEFAULT_SQUEEZE_PERCENTILE,
    expansion_lookback: int = DEFAULT_EXPANSION_LOOKBACK,
    expansion_ratio: float = DEFAULT_EXPANSION_RATIO,
) -> MarketStateResult:
    """Classify the latest row of *indicators* into a MarketState.

    ``indicators`` is expected to already have gone through
    ``indicators.calculate_indicators()`` (or provide the equivalent
    columns). The classification always looks at the last row; callers
    that only want a confirmed candle should pass an already-trimmed frame.

    Confidence is always a matched/total ratio over that state's own
    checklist, so it is bounded to [0.0, 1.0] by construction. ``reasons``
    always contains at least one entry.
    """
    if indicators is None or indicators.empty:
        return _unknown("No indicator data available.")

    position = len(indicators) - 1
    row = indicators.iloc[position]

    missing = [field for field in REQUIRED_FIELDS if field not in row.index or pd.isna(row[field])]
    if missing:
        return _unknown(f"Missing or unavailable indicator fields: {', '.join(missing)}.")

    close = float(row["close"])
    sma20 = float(row["sma20"])
    sma75 = float(row["sma75"])
    ema20 = float(row["ema20"])
    macd_state = str(row["macd_state"])
    bb_width_pct = float(row["bb_width_pct"])
    bb_position = float(row["bb_position"])
    bandwidth = indicators["bb_bandwidth"]

    numeric_values = {
        "close": close,
        "sma20": sma20,
        "sma75": sma75,
        "ema20": ema20,
        "bb_width_pct": bb_width_pct,
        "bb_position": bb_position,
    }
    non_finite = [field for field, value in numeric_values.items() if not math.isfinite(value)]
    if non_finite:
        return _unknown(f"Non-finite indicator fields: {', '.join(non_finite)}.")

    # squeeze: checked first, since a converged/quiet band is the most
    # specific structural signal and would otherwise be miscounted as range.
    if _bandwidth_in_lower_percentile(
        bandwidth, position, lookback=squeeze_lookback, percentile=squeeze_percentile
    ):
        centered = 0.3 <= bb_position <= 0.7
        matched = 1 + (1 if centered else 0)
        reasons = [f"Bollinger Band width ({bb_width_pct:.2f}%) is in the lower {squeeze_percentile:.0%} of its recent range."]
        if centered:
            reasons.append("Price sits near the middle of the band.")
        return {"market_state": "squeeze", "confidence": matched / 2, "reasons": reasons}

    ma_stack_up = ema20 > sma20 > sma75
    ma_stack_down = ema20 < sma20 < sma75

    if ma_stack_up and macd_state == "Bullish" and close > sma20:
        reasons = [
            "Short MA (EMA20) is above mid MA (SMA20), which is above long MA (SMA75).",
            "MACD state is Bullish.",
            "Close is above SMA20.",
        ]
        matched = 3
        if bb_position > 0.5:
            reasons.append("Price is in the upper half of the Bollinger Band.")
            matched += 1
        return {"market_state": "trend_up", "confidence": matched / 4, "reasons": reasons}

    if ma_stack_down and macd_state == "Bearish" and close < sma20:
        reasons = [
            "Short MA (EMA20) is below mid MA (SMA20), which is below long MA (SMA75).",
            "MACD state is Bearish.",
            "Close is below SMA20.",
        ]
        matched = 3
        if bb_position < 0.5:
            reasons.append("Price is in the lower half of the Bollinger Band.")
            matched += 1
        return {"market_state": "trend_down", "confidence": matched / 4, "reasons": reasons}

    # volatile: bandwidth expanded relative to its recent baseline.
    if _bandwidth_expanded(bandwidth, position, lookback=expansion_lookback, ratio=expansion_ratio):
        reasons = [f"Bollinger Band width ({bb_width_pct:.2f}%) has expanded relative to its recent baseline."]
        matched = 1
        near_edge = bb_position >= BAND_EDGE_POSITION or bb_position <= (1 - BAND_EDGE_POSITION)
        if near_edge:
            reasons.append("Price is near or beyond the Bollinger Band edge.")
            matched += 1
        return {"market_state": "volatile", "confidence": matched / 2, "reasons": reasons}

    # range: fallback. Confidence reflects how many of the range-confirming
    # signals actually hold, since falling through by elimination alone is
    # weaker evidence than a clean, converged, inside-the-band reading.
    matched = 0
    reasons: list[str] = []
    if _relative_gap(ema20, sma20) <= MA_CONVERGENCE_THRESHOLD and _relative_gap(sma20, sma75) <= MA_CONVERGENCE_THRESHOLD:
        reasons.append("Short/mid/long MAs are converged.")
        matched += 1
    if macd_state == RANGE_MACD_STATE:
        reasons.append("MACD state is Neutral.")
        matched += 1
    if 0.2 <= bb_position <= 0.8:
        reasons.append("Price is inside the Bollinger Band.")
        matched += 1
    if not reasons:
        reasons.append("No trend, squeeze, or volatility signal matched; defaulting to range.")

    return {"market_state": "range", "confidence": matched / 3, "reasons": reasons}
