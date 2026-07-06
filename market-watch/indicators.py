"""Chart indicators used by Berial's market-watch chart snapshot.

The functions here only describe current chart state. They do not generate
entries, exits, position sizes, orders, or automated trading actions.
"""

from __future__ import annotations

import pandas as pd


def calculate_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of *frame* with moving averages and technical indicators.

    Expected input columns are at least ``close``. The returned frame includes:
    ``sma20``, ``sma75``, ``ema20``, ``bb_middle``, ``bb_upper``, ``bb_lower``,
    ``bb_width_pct``, ``bb_position``, ``macd``, ``macd_signal``,
    ``macd_hist``, ``macd_state``, ``rsi14``, and ``rsi_state``.
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

    macd, macd_signal, macd_hist = calculate_macd(close)
    result["macd"] = macd
    result["macd_signal"] = macd_signal
    result["macd_hist"] = macd_hist
    result["macd_state"] = [
        classify_macd_state(macd_value, signal_value, hist_value)
        for macd_value, signal_value, hist_value in zip(macd, macd_signal, macd_hist)
    ]

    result["rsi14"] = calculate_rsi(close)
    result["rsi_state"] = [classify_rsi_state(value) for value in result["rsi14"]]
    return result


def calculate_macd(
    close: pd.Series,
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return MACD, signal, and histogram series.

    MACD = EMA12 - EMA26 by default. The signal line is EMA9 of MACD, and the
    histogram is MACD - signal.
    """
    close = close.astype(float)
    macd = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    macd_signal = macd.ewm(span=signal, adjust=False).mean()
    macd_hist = macd - macd_signal
    return macd, macd_signal, macd_hist


def calculate_rsi(close: pd.Series, *, period: int = 14) -> pd.Series:
    """Return Wilder-style RSI for *close*.

    The default period is 14. Values before enough candles are available remain
    ``NaN`` so callers can classify them as unavailable.
    """
    if period <= 0:
        raise ValueError("period must be positive")

    close = close.astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    rsi = pd.Series(float("nan"), index=close.index, dtype="float64")
    window_gains: list[float] = []
    window_losses: list[float] = []
    avg_gain: float | None = None
    avg_loss: float | None = None

    for position in range(1, len(close)):
        current_gain = gain.iloc[position]
        current_loss = loss.iloc[position]

        if pd.isna(current_gain) or pd.isna(current_loss):
            window_gains.clear()
            window_losses.clear()
            avg_gain = None
            avg_loss = None
            continue

        if avg_gain is None or avg_loss is None:
            window_gains.append(float(current_gain))
            window_losses.append(float(current_loss))
            if len(window_gains) < period:
                continue

            avg_gain = sum(window_gains[-period:]) / period
            avg_loss = sum(window_losses[-period:]) / period
        else:
            avg_gain = ((avg_gain * (period - 1)) + float(current_gain)) / period
            avg_loss = ((avg_loss * (period - 1)) + float(current_loss)) / period

        if avg_loss == 0 and avg_gain > 0:
            rsi.iloc[position] = 100.0
        elif avg_loss == 0 and avg_gain == 0:
            rsi.iloc[position] = 50.0
        else:
            rs = avg_gain / avg_loss
            rsi.iloc[position] = 100 - (100 / (1 + rs))

    return rsi


def detect_macd_cross(prev_macd, prev_signal, curr_macd, curr_signal) -> str | None:
    """Return bullish/bearish when MACD crosses the signal line."""
    prev_diff = prev_macd - prev_signal
    curr_diff = curr_macd - curr_signal
    if prev_diff <= 0 and curr_diff > 0:
        return "bullish"
    if prev_diff >= 0 and curr_diff < 0:
        return "bearish"
    return None


def classify_macd_state(macd: float | None, macd_signal: float | None, macd_hist: float | None) -> str:
    if (
        macd is None
        or macd_signal is None
        or macd_hist is None
        or pd.isna(macd)
        or pd.isna(macd_signal)
        or pd.isna(macd_hist)
    ):
        return "Neutral"
    if macd > macd_signal and macd_hist > 0:
        return "Bullish"
    if macd < macd_signal and macd_hist < 0:
        return "Bearish"
    return "Neutral"


def classify_rsi_state(rsi: float | None) -> str:
    if rsi is None or pd.isna(rsi):
        return "Unavailable"
    if rsi >= 70:
        return "Overbought"
    if rsi <= 30:
        return "Oversold"
    return "Neutral"


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
