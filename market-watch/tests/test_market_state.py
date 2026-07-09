import pathlib
import sys

import pandas as pd
import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import market_state  # noqa: E402
from indicators import calculate_indicators  # noqa: E402


def _row(**overrides):
    base = {
        "close": 100.0,
        "sma20": 100.0,
        "sma75": 100.0,
        "ema20": 100.0,
        "macd_state": "Neutral",
        "bb_bandwidth": 0.01,
        "bb_width_pct": 1.0,
        "bb_position": 0.5,
    }
    base.update(overrides)
    return base


def test_classify_market_state_unknown_for_empty_indicators():
    result = market_state.classify_market_state(pd.DataFrame())

    assert result["market_state"] == "unknown"
    assert result["confidence"] == 0.0
    assert result["reasons"]


def test_classify_market_state_unknown_for_missing_fields():
    frame = pd.DataFrame([_row(sma20=None)])

    result = market_state.classify_market_state(frame)

    assert result["market_state"] == "unknown"
    assert result["confidence"] == 0.0
    assert "sma20" in result["reasons"][0]


def test_classify_market_state_unknown_for_short_candle_history():
    candles = pd.DataFrame(
        {
            "open": [100.0, 100.2, 100.1],
            "high": [100.3, 100.4, 100.2],
            "low": [99.9, 100.0, 99.8],
            "close": [100.1, 100.2, 100.0],
        }
    )
    frame = calculate_indicators(candles)

    result = market_state.classify_market_state(frame)

    assert result["market_state"] == "unknown"
    assert result["confidence"] == 0.0
    assert result["reasons"]


def test_classify_market_state_unknown_for_non_finite_numeric_fields():
    frame = pd.DataFrame([_row(close=float("inf"))])

    result = market_state.classify_market_state(frame)

    assert result["market_state"] == "unknown"
    assert result["confidence"] == 0.0
    assert "close" in result["reasons"][0]


def test_classify_market_state_trend_up_when_ma_stack_and_macd_align():
    frame = pd.DataFrame(
        [_row(sma75=100.0, sma20=101.0, ema20=102.0, close=103.0, macd_state="Bullish", bb_position=0.9)]
    )

    result = market_state.classify_market_state(frame)

    assert result["market_state"] == "trend_up"
    assert result["confidence"] == 1.0
    assert "MACD state is Bullish." in result["reasons"]


def test_classify_market_state_trend_up_has_partial_confidence_without_bb_bonus():
    frame = pd.DataFrame(
        [_row(sma75=100.0, sma20=101.0, ema20=102.0, close=103.0, macd_state="Bullish", bb_position=0.3)]
    )

    result = market_state.classify_market_state(frame)

    assert result["market_state"] == "trend_up"
    assert result["confidence"] == pytest.approx(0.75)


def test_classify_market_state_trend_down_when_ma_stack_and_macd_align():
    frame = pd.DataFrame(
        [_row(sma75=100.0, sma20=99.0, ema20=98.0, close=97.0, macd_state="Bearish", bb_position=0.1)]
    )

    result = market_state.classify_market_state(frame)

    assert result["market_state"] == "trend_down"
    assert result["confidence"] == 1.0
    assert "MACD state is Bearish." in result["reasons"]


def test_classify_market_state_range_when_mas_converge_and_macd_is_neutral():
    frame = pd.DataFrame(
        [_row(sma75=100.0, sma20=100.02, ema20=100.03, close=100.02, macd_state="Neutral", bb_position=0.5)]
    )

    result = market_state.classify_market_state(frame)

    assert result["market_state"] == "range"
    assert result["confidence"] == 1.0
    assert len(result["reasons"]) == 3


def test_classify_market_state_volatile_when_bandwidth_expands():
    steady_rows = [_row(bb_bandwidth=0.01, bb_width_pct=1.0) for _ in range(20)]
    latest_row = _row(bb_bandwidth=0.02, bb_width_pct=2.0, bb_position=0.9)
    frame = pd.DataFrame(steady_rows + [latest_row])

    result = market_state.classify_market_state(frame)

    assert result["market_state"] == "volatile"
    assert 0.0 <= result["confidence"] <= 1.0
    assert result["confidence"] == 1.0
    assert result["reasons"]


def test_classify_market_state_squeeze_when_bandwidth_contracts():
    steady_rows = [_row(bb_bandwidth=0.05, bb_width_pct=5.0) for _ in range(19)]
    latest_row = _row(bb_bandwidth=0.005, bb_width_pct=0.5, bb_position=0.5)
    frame = pd.DataFrame(steady_rows + [latest_row])

    result = market_state.classify_market_state(frame)

    assert result["market_state"] == "squeeze"
    assert 0.0 <= result["confidence"] <= 1.0
    assert result["confidence"] == 1.0
    assert result["reasons"]


def test_classify_market_state_squeeze_takes_priority_over_trend():
    steady_rows = [_row(bb_bandwidth=0.05, bb_width_pct=5.0) for _ in range(19)]
    latest_row = _row(
        bb_bandwidth=0.005,
        bb_width_pct=0.5,
        bb_position=0.5,
        sma75=100.0,
        sma20=101.0,
        ema20=102.0,
        close=103.0,
        macd_state="Bullish",
    )
    frame = pd.DataFrame(steady_rows + [latest_row])

    result = market_state.classify_market_state(frame)

    assert result["market_state"] == "squeeze"
    assert result["confidence"] == 1.0


@pytest.mark.parametrize(
    "frame",
    [
        pd.DataFrame(),
        pd.DataFrame([_row(sma20=None)]),
        pd.DataFrame([_row(sma75=100.0, sma20=101.0, ema20=102.0, close=103.0, macd_state="Bullish")]),
        pd.DataFrame([_row(sma75=100.0, sma20=99.0, ema20=98.0, close=97.0, macd_state="Bearish")]),
        pd.DataFrame([_row()]),
    ],
)
def test_reasons_are_never_empty_and_confidence_is_bounded(frame):
    result = market_state.classify_market_state(frame)

    assert result["reasons"]
    assert 0.0 <= result["confidence"] <= 1.0
    assert result["market_state"] in {"trend_up", "trend_down", "range", "volatile", "squeeze", "unknown"}
