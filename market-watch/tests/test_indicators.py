import pathlib
import sys

import pandas as pd
import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import indicators  # noqa: E402


def _make_ohlc(values):
    index = pd.date_range("2026-07-01", periods=len(values), freq="15min", tz="UTC")
    return pd.DataFrame(
        {
            "high": [float(value) + 0.01 for value in values],
            "low": [float(value) - 0.01 for value in values],
            "close": [float(value) for value in values],
        },
        index=index,
    )


def test_calculate_indicators_adds_moving_averages_and_bollinger_bands():
    frame = pd.DataFrame({"close": [float(i) for i in range(1, 81)]})

    result = indicators.calculate_indicators(frame)

    latest = result.iloc[-1]
    assert latest["sma20"] == sum(range(61, 81)) / 20
    assert latest["sma75"] == sum(range(6, 81)) / 75
    assert latest["ema20"] > 0
    assert latest["bb_lower"] < latest["bb_middle"] < latest["bb_upper"]
    assert latest["bb_bandwidth"] == pytest.approx(latest["bb_width_pct"] / 100)
    assert latest["bb_width_pct"] > 0
    assert latest["bb_position"] > 0
    assert latest["macd"] == result["macd"].iloc[-1]
    assert latest["macd_hist"] == latest["macd"] - latest["macd_signal"]
    assert latest["macd_state"] in {"Bullish", "Bearish", "Neutral"}
    assert latest["rsi14"] == 100.0
    assert latest["rsi_state"] == "Overbought"


def test_calculate_bollinger_bands_uses_window_and_std_multiplier():
    close = pd.Series([100.0] * 19 + [110.0])

    bands = indicators.calculate_bollinger_bands(close, window=20, std_multiplier=2.0)

    latest = bands.iloc[-1]
    expected_middle = close.mean()
    expected_std = close.std()
    assert latest["bb_middle"] == pytest.approx(expected_middle)
    assert latest["bb_upper"] == pytest.approx(expected_middle + expected_std * 2.0)
    assert latest["bb_lower"] == pytest.approx(expected_middle - expected_std * 2.0)
    assert latest["bb_bandwidth"] == pytest.approx((latest["bb_upper"] - latest["bb_lower"]) / expected_middle)


def test_calculate_macd_returns_macd_signal_and_histogram():
    close = pd.Series([100 + i * 0.1 for i in range(60)])

    macd, macd_signal, macd_hist = indicators.calculate_macd(close)

    assert len(macd) == len(close)
    assert len(macd_signal) == len(close)
    assert len(macd_hist) == len(close)
    assert macd_hist.iloc[-1] == macd.iloc[-1] - macd_signal.iloc[-1]


def test_calculate_rsi_returns_wilder_rsi():
    close = pd.Series([
        44.34,
        44.09,
        44.15,
        43.61,
        44.33,
        44.83,
        45.10,
        45.42,
        45.84,
        46.08,
        45.89,
        46.03,
        45.61,
        46.28,
        46.28,
        46.00,
        46.03,
    ])

    rsi = indicators.calculate_rsi(close)

    assert len(rsi) == len(close)
    assert pd.isna(rsi.iloc[13])
    assert rsi.iloc[14] == pytest.approx(70.464135)
    assert rsi.iloc[-1] == pytest.approx(66.480942)


def test_calculate_rsi_keeps_series_length_and_waits_for_period():
    close = pd.Series([100.0 + i for i in range(14)])

    rsi = indicators.calculate_rsi(close)

    assert len(rsi) == len(close)
    assert rsi.isna().all()


def test_calculate_rsi_returns_100_for_rising_only_series():
    close = pd.Series([100.0 + i for i in range(20)])

    rsi = indicators.calculate_rsi(close)

    assert rsi.iloc[:14].isna().all()
    assert rsi.iloc[14] == 100.0
    assert rsi.iloc[-1] == 100.0


def test_calculate_rsi_returns_zero_for_falling_only_series():
    close = pd.Series([100.0 - i for i in range(20)])

    rsi = indicators.calculate_rsi(close)

    assert rsi.iloc[:14].isna().all()
    assert rsi.iloc[14] == pytest.approx(0.0)
    assert rsi.iloc[-1] == pytest.approx(0.0)


def test_calculate_rsi_returns_50_for_flat_series():
    close = pd.Series([100.0 for _ in range(20)])

    rsi = indicators.calculate_rsi(close)

    assert rsi.iloc[:14].isna().all()
    assert rsi.iloc[14] == 50.0
    assert rsi.iloc[-1] == 50.0


def test_calculate_rsi_handles_nan_by_waiting_for_a_new_seed_window():
    close = pd.Series(
        [100.0 + i for i in range(10)]
        + [float("nan")]
        + [110.0 + i for i in range(16)]
    )

    rsi = indicators.calculate_rsi(close)

    assert len(rsi) == len(close)
    assert pd.isna(rsi.iloc[10])
    assert pd.isna(rsi.iloc[24])
    assert rsi.iloc[-1] == 100.0


def test_detect_macd_cross_rules():
    assert indicators.detect_macd_cross(-0.1, 0.0, 0.1, 0.0) == "bullish"
    assert indicators.detect_macd_cross(0.1, 0.0, -0.1, 0.0) == "bearish"
    assert indicators.detect_macd_cross(0.2, 0.0, 0.1, 0.0) is None


def test_detect_bollinger_signals_upper_and_lower_breaks():
    upper = _make_ohlc([100.0] * 20 + [110.0, 100.0])
    lower = _make_ohlc([100.0] * 20 + [90.0, 100.0])

    upper_signals = indicators.detect_bollinger_signals(upper, current_position=-2, squeeze_lookback=3)
    lower_signals = indicators.detect_bollinger_signals(lower, current_position=-2, squeeze_lookback=3)

    assert {signal["signal_type"] for signal in upper_signals} >= {"upper_break", "upper_touch"}
    assert {signal["signal_type"] for signal in lower_signals} >= {"lower_break", "lower_touch"}
    assert upper_signals[0]["note"] == "終値が+2σ外側。過熱/ブレイク候補。"
    assert lower_signals[0]["note"] == "終値が-2σ外側。過熱/ブレイク候補。"


def test_detect_bollinger_signals_squeeze_and_expansion_start():
    squeeze_candles = _make_ohlc([100.0, 102.0, 100.0, 102.0, 100.0, 100.1, 100.0])
    squeeze_signals = indicators.detect_bollinger_signals(
        squeeze_candles,
        current_position=-2,
        window=2,
        squeeze_lookback=3,
        squeeze_percentile=0.10,
    )

    expansion_candles = _make_ohlc([100.0, 102.0, 100.0, 102.0, 100.0, 100.1, 100.0, 101.0, 101.1])
    expansion_signals = indicators.detect_bollinger_signals(
        expansion_candles,
        current_position=-2,
        window=2,
        squeeze_lookback=3,
        squeeze_percentile=0.10,
        expansion_ratio=1.30,
    )

    assert "squeeze" in {signal["signal_type"] for signal in squeeze_signals}
    assert "expansion_start" in {signal["signal_type"] for signal in expansion_signals}


def test_classify_macd_state_rules():
    assert indicators.classify_macd_state(1.0, 0.5, 0.5) == "Bullish"
    assert indicators.classify_macd_state(0.5, 1.0, -0.5) == "Bearish"
    assert indicators.classify_macd_state(1.0, 1.0, 0.0) == "Neutral"
    assert indicators.classify_macd_state(None, 1.0, 0.0) == "Neutral"


def test_classify_rsi_state_rules():
    assert indicators.classify_rsi_state(70.0) == "Overbought"
    assert indicators.classify_rsi_state(69.9) == "Neutral"
    assert indicators.classify_rsi_state(30.0) == "Oversold"
    assert indicators.classify_rsi_state(30.1) == "Neutral"
    assert indicators.classify_rsi_state(50.0) == "Neutral"
    assert indicators.classify_rsi_state(None) == "Unavailable"
    assert indicators.classify_rsi_state(float("nan")) == "Unavailable"


def test_detect_rsi_zone_event_rules():
    assert indicators.detect_rsi_zone_event(69.9, 70.0) == "overbought_enter"
    assert indicators.detect_rsi_zone_event(70.0, 69.9) == "overbought_exit"
    assert indicators.detect_rsi_zone_event(30.1, 30.0) == "oversold_enter"
    assert indicators.detect_rsi_zone_event(30.0, 30.1) == "oversold_exit"
    assert indicators.detect_rsi_zone_event(50.0, 55.0) is None
    assert indicators.detect_rsi_zone_event(float("nan"), 72.0) is None


def test_classify_trend_rules():
    assert indicators.classify_trend(105.0, 100.0, 90.0) == "Bull"
    assert indicators.classify_trend(85.0, 90.0, 100.0) == "Bear"
    assert indicators.classify_trend(95.0, 100.0, 90.0) == "Neutral"
    assert indicators.classify_trend(95.0, None, 90.0) == "Neutral"


def test_classify_bb_state_rules():
    assert indicators.classify_bb_state(1.1) == "Upper breakout"
    assert indicators.classify_bb_state(0.8) == "Near upper band"
    assert indicators.classify_bb_state(0.5) == "Inside band"
    assert indicators.classify_bb_state(0.1) == "Near lower band"
    assert indicators.classify_bb_state(-0.1) == "Lower breakout"
    assert indicators.classify_bb_state(None) == "Unavailable"
