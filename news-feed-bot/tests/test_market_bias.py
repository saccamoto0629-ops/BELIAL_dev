import pathlib
import re
import sys

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import market_bias as mb  # noqa: E402


PROHIBITED_USER_WORDS = re.compile(
    r"\b(long|short|entry|enter|exit|take[- ]profit|stop[- ]loss|trade now|recommend buying|recommend selling)\b",
    re.IGNORECASE,
)


def make_snapshot(latest, previous):
    diff = latest - previous
    pct_change = (diff / previous) * 100 if previous else 0.0
    return {"latest": latest, "previous": previous, "diff": diff, "pct_change": pct_change}


def make_fred(latest, previous):
    return {"latest": latest, "previous": previous, "diff": latest - previous, "date": "2026-07-04"}


def test_market_bias_is_bull_when_risk_assets_are_strong():
    market_data = {
        "VIX": make_snapshot(15.0, 16.0),
        "SP500": make_snapshot(5450.0, 5420.0),
        "NASDAQ": make_snapshot(17300.0, 17200.0),
    }
    tv_data = {"SPX": {"recommendation": "BUY", "rsi": 61.0}}

    result = mb.classify_market_bias(market_data, None, tv_data)

    assert result["market_bias"] == "Bull"
    assert result["scores"]["Bull"] > result["scores"]["Bear"]
    assert 0.0 <= result["confidence"] <= 1.0


def test_market_bias_is_bear_when_volatility_and_equity_weakness_align():
    market_data = {
        "VIX": make_snapshot(30.0, 20.0),
        "VVIX": make_snapshot(110.0, 95.0),
        "SP500": make_snapshot(5380.0, 5420.0),
        "NASDAQ": make_snapshot(17100.0, 17200.0),
    }

    result = mb.classify_market_bias(market_data, None, {})

    assert result["market_bias"] == "Bear"
    assert result["scores"]["Bear"] > result["scores"]["Bull"]
    assert "VIX is above 25" in result["reasons"]


def test_small_score_difference_becomes_neutral():
    market_data = {
        "SP500": make_snapshot(5450.0, 5420.0),
        "NASDAQ": make_snapshot(17100.0, 17200.0),
    }

    result = mb.classify_market_bias(market_data, None, {})

    assert result["market_bias"] == "Neutral"
    assert "Bull and Bear scores are close, so the bias is Neutral." in result["reasons"]


def test_missing_data_becomes_neutral():
    result = mb.classify_market_bias({}, None, {})

    assert result["market_bias"] == "Neutral"
    assert result["confidence"] == 0.0
    assert result["scores"] == {"Bull": 0, "Bear": 0}
    assert isinstance(result["reasons"], list)
    assert isinstance(result["warnings"], list)


def test_return_shape_and_allowed_labels():
    result = mb.classify_market_bias(
        {"VIX": make_snapshot(17.0, 18.0), "SP500": make_snapshot(5450.0, 5420.0)},
        {"US10Y": make_fred(4.48, 4.40)},
        {"SPX": {"recommendation": "NEUTRAL"}},
    )

    assert result["market_bias"] in {"Bull", "Bear", "Neutral"}
    assert isinstance(result["scores"], dict)
    assert isinstance(result["reasons"], list)
    assert isinstance(result["warnings"], list)
    assert result["note"] == "Observation only. No trade recommendation."
    assert 0.0 <= result["confidence"] <= 1.0


def test_macd_is_auxiliary_and_does_not_dominate_missing_data():
    result = mb.classify_market_bias({}, None, {}, macd_signal="bullish")

    assert result["market_bias"] == "Neutral"
    assert result["scores"] == {"Bull": 1, "Bear": 0}


def test_macd_conflict_warning_for_bullish_cross_during_bear_conditions():
    market_data = {
        "VIX": make_snapshot(30.0, 20.0),
        "SP500": make_snapshot(5380.0, 5420.0),
        "NASDAQ": make_snapshot(17100.0, 17200.0),
    }

    result = mb.classify_market_bias(market_data, None, {}, macd_signal="bullish")

    assert result["market_bias"] == "Bear"
    assert result["warnings"] == [
        "M15 MACD bullish cross conflicts with broader Bear conditions"
    ]


def test_macd_conflict_warning_for_bearish_cross_during_bull_conditions():
    market_data = {
        "VIX": make_snapshot(15.0, 16.0),
        "SP500": make_snapshot(5450.0, 5420.0),
        "NASDAQ": make_snapshot(17300.0, 17200.0),
    }

    result = mb.classify_market_bias(market_data, None, {}, macd_signal="bearish")

    assert result["market_bias"] == "Bull"
    assert result["warnings"] == [
        "M15 MACD bearish cross conflicts with broader Bull conditions"
    ]


def test_format_market_bias_section_is_compact_and_observation_only():
    result = mb.classify_market_bias(
        {
            "VIX": make_snapshot(30.0, 20.0),
            "SP500": make_snapshot(5380.0, 5420.0),
            "NASDAQ": make_snapshot(17100.0, 17200.0),
        },
        None,
        {},
        macd_signal="bullish",
    )

    section = mb.format_market_bias_section(result)

    assert "## Market Bias" in section
    assert "Bias: Bear" in section
    assert "Observation only. No trade recommendation." in section
    assert PROHIBITED_USER_WORDS.search(section) is None


def test_format_market_bias_section_does_not_expose_tv_buy_sell_words():
    result = mb.classify_market_bias(
        {"SP500": make_snapshot(5450.0, 5420.0), "NASDAQ": make_snapshot(17300.0, 17200.0)},
        None,
        {"SPX": {"recommendation": "STRONG_BUY"}},
    )

    section = mb.format_market_bias_section(result)

    assert "BUY" not in section
    assert "SELL" not in section


def test_chart_stale_data_is_excluded_from_market_bias():
    result = mb.classify_market_bias(
        {},
        None,
        {},
        chart_data=[
            {"symbol": "SPX", "trend": "Bull", "freshness": "Stale", "bias_weight": 1.0},
        ],
    )

    assert result["market_bias"] == "Neutral"
    assert result["scores"] == {"Bull": 0, "Bear": 0}


def test_weekend_closed_chart_data_uses_reduced_bias_weight():
    result = mb.classify_market_bias(
        {"VIX": make_snapshot(17.0, 18.0)},
        None,
        {},
        chart_data=[
            {"symbol": "SPX", "trend": "Bull", "freshness": "Weekend Closed", "bias_weight": 0.25},
        ],
    )

    assert result["scores"]["Bull"] == 1.25
    assert any("Chart SPX 1h trend is Bull" in reason for reason in result["reasons"])


def test_chart_tv_mixed_reason_is_added_on_conflict():
    result = mb.classify_market_bias(
        {"VIX": make_snapshot(17.0, 18.0)},
        None,
        {"SPX": {"recommendation": "SELL"}},
        chart_data=[
            {"symbol": "SPX", "trend": "Bull", "freshness": "Fresh", "bias_weight": 1.0},
        ],
    )

    assert "Chart/TV mixed" in result["reasons"]
