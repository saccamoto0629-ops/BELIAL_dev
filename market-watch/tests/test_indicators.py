import pathlib
import sys

import pandas as pd

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import indicators  # noqa: E402


def test_calculate_indicators_adds_moving_averages_and_bollinger_bands():
    frame = pd.DataFrame({"close": [float(i) for i in range(1, 81)]})

    result = indicators.calculate_indicators(frame)

    latest = result.iloc[-1]
    assert latest["sma20"] == sum(range(61, 81)) / 20
    assert latest["sma75"] == sum(range(6, 81)) / 75
    assert latest["ema20"] > 0
    assert latest["bb_lower"] < latest["bb_middle"] < latest["bb_upper"]
    assert latest["bb_width_pct"] > 0
    assert latest["bb_position"] > 0


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
