"""Chart indicators used by Berial's market-watch chart snapshot.

The functions here only describe current chart state. They do not generate
entries, exits, position sizes, orders, or automated trading actions.
"""

from __future__ import annotations

import pandas as pd


def calculate_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of *frame* with moving averages and Bollinger Bands.

    Expected input columns are at least ``close``. The returned frame includes:
    ``sma20``, ``sma75``, ``ema20``, ``bb_middle``, ``bb_upper``, ``bb_lower``,
    ``bb_width_pct``, and ``bb_position``.
    """
    if "close" not in frame.columns:
        raise ValueError("frame must contain a 'close' column")

    result = frame.copy()
    close = result["close"].astype(float)

    result["sma20"] = close.rolling(window=20, min_periods=20).mean()
    result["sma75"] = close.rolling(window=75, min_periods=75).mean()
    result["ema20"] = close.ewm(span=20, adjust=False, min_periods=20).mean()

    result["bb_middle"] = result["sma20"]
    bb_std = close.rolling(window=20, min_periods=20).std()
    result["bb_upper"] = result["bb_middle"] + (bb_std * 2)
    result["bb_lower"] = result["bb_middle"] - (bb_std * 2)

    band_range = result["bb_upper"] - result["bb_lower"]
    result["bb_width_pct"] = (band_range / result["bb_middle"]) * 100
    result["bb_position"] = (close - result["bb_lower"]) / band_range
    return result


def classify_trend(close: float, sma20: float | None, sma75: float | None) -> str:
    if sma20 is None or sma75 is None or pd.isna(sma20) or pd.isna(sma75):
        return "Neutral"
    if close > sma20 > sma75:
        return "Bull"
    if close < sma20 < sma75:
        return "Bear"
    return "Neutral"


def classify_bb_state(position: float | None) -> str:
    if position is None or pd.isna(position):
        return "Unavailable"
    if position > 1.0:
        return "Upper breakout"
    if position >= 0.8:
        return "Near upper band"
    if position >= 0.2:
        return "Inside band"
    if position >= 0.0:
        return "Near lower band"
    return "Lower breakout"
