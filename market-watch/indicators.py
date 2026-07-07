"""Chart indicators used by Berial's market-watch chart snapshot.

The functions here only describe current chart state. They do not generate
entries, exits, position sizes, orders, or automated trading actions.
"""

from __future__ import annotations

import pandas as pd


def calculate_bollinger_bands(
    close: pd.Series,
    *,
    window: int = 20,
    std_multiplier: float = 2.0,
) -> pd.DataFrame:
    """Return Bollinger Bands and decimal bandwidth for *close*."""
    if window <= 0:
        raise ValueError("window must be positive")
    if std_multiplier <= 0:
        raise ValueError("std_multiplier must be positive")

    close = close.astype(float)
    middle = close.rolling(window=window, min_periods=window).mean()
    std = close.rolling(window=window, min_periods=window).std()
    upper = middle + (std * std_multiplier)
    lower = middle - (std * std_multiplier)
    bandwidth = (upper - lower) / middle
    return pd.DataFrame(
        {
            "bb_middle": middle,
            "bb_upper": upper,
            "bb_lower": lower,
            "bb_bandwidth": bandwidth,
        },
        index=close.index,
    )


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

    bands = calculate_bollinger_bands(close)
    result["bb_middle"] = bands["bb_middle"]
    result["bb_upper"] = bands["bb_upper"]
    result["bb_lower"] = bands["bb_lower"]
    result["bb_bandwidth"] = bands["bb_bandwidth"]

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


def detect_ma_touch(close_price, ma_value, *, threshold_pct: float):
    """Return MA touch details when close is within threshold_pct of ma_value."""
    if close_price is None or ma_value is None or pd.isna(close_price) or pd.isna(ma_value):
        return None
    close = float(close_price)
    ma = float(ma_value)
    if ma == 0:
        return None
    distance_pct = abs(close - ma) / abs(ma) * 100
    if distance_pct > threshold_pct:
        return None
    return {
        "distance_pct": distance_pct,
        "side": "above" if close >= ma else "below",
    }


def is_bollinger_squeeze(
    bandwidth: pd.Series,
    position: int,
    *,
    lookback: int = 120,
    percentile: float = 0.10,
) -> bool:
    """Return True when bandwidth is in the lower percentile of its lookback."""
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    if not 0 <= percentile <= 1:
        raise ValueError("percentile must be between 0 and 1")

    current = bandwidth.iloc[position]
    if pd.isna(current):
        return False
    window = bandwidth.iloc[: position + 1].dropna().tail(lookback)
    if len(window) < lookback:
        return False
    return float(current) <= float(window.quantile(percentile))


def detect_bollinger_signals(
    candles: pd.DataFrame,
    *,
    current_position: int = -1,
    window: int = 20,
    std_multiplier: float = 2.0,
    squeeze_lookback: int = 120,
    squeeze_percentile: float = 0.10,
    expansion_ratio: float = 1.30,
) -> list[dict]:
    """Return Bollinger observation signals for one already-confirmed candle."""
    required = {"high", "low", "close"}
    if not required.issubset(candles.columns):
        missing = ", ".join(sorted(required - set(candles.columns)))
        raise ValueError(f"candles must contain columns: {missing}")
    if expansion_ratio <= 0:
        raise ValueError("expansion_ratio must be positive")

    bands = calculate_bollinger_bands(
        candles["close"],
        window=window,
        std_multiplier=std_multiplier,
    )
    frame = candles.copy()
    frame[["bb_middle", "bb_upper", "bb_lower", "bb_bandwidth"]] = bands
    current_idx = len(frame) + current_position if current_position < 0 else current_position
    row = frame.iloc[current_position]

    values = {
        "close": row["close"],
        "high": row["high"],
        "low": row["low"],
        "middle": row["bb_middle"],
        "upper": row["bb_upper"],
        "lower": row["bb_lower"],
        "bandwidth": row["bb_bandwidth"],
    }
    if any(pd.isna(value) for value in values.values()):
        return []

    close = float(values["close"])
    high = float(values["high"])
    low = float(values["low"])
    upper = float(values["upper"])
    lower = float(values["lower"])
    bandwidth = float(values["bandwidth"])

    events: list[dict] = []

    def append(signal_type: str, note: str) -> None:
        events.append(
            {
                "signal_type": signal_type,
                "close": close,
                "upper": upper,
                "middle": float(values["middle"]),
                "lower": lower,
                "bandwidth": bandwidth,
                "note": note,
            }
        )

    if close >= upper:
        append("upper_break", "終値が+2σ外側。過熱/ブレイク候補。")
    if close <= lower:
        append("lower_break", "終値が-2σ外側。過熱/ブレイク候補。")
    if high >= upper:
        append("upper_touch", "高値が+2σに到達。市場状態の監視対象。")
    if low <= lower:
        append("lower_touch", "安値が-2σに到達。市場状態の監視対象。")

    current_squeeze = is_bollinger_squeeze(
        frame["bb_bandwidth"],
        current_idx,
        lookback=squeeze_lookback,
        percentile=squeeze_percentile,
    )
    if current_squeeze:
        append("squeeze", "バンド幅が過去レンジの下位圏。低ボラティリティ状態。")

    if current_idx > 0:
        previous_bandwidth = frame["bb_bandwidth"].iloc[current_idx - 1]
        previous_squeeze = is_bollinger_squeeze(
            frame["bb_bandwidth"],
            current_idx - 1,
            lookback=squeeze_lookback,
            percentile=squeeze_percentile,
        )
        if (
            previous_squeeze
            and not current_squeeze
            and not pd.isna(previous_bandwidth)
            and float(previous_bandwidth) > 0
            and bandwidth >= float(previous_bandwidth) * expansion_ratio
        ):
            append("expansion_start", "スクイーズを抜けてバンド幅が拡大開始。")

    return events


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
