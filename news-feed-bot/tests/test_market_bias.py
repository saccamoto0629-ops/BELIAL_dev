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
    assert "Trend: Bear" in section
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
            {"symbol": "SPX", "trend": "Bull", "macd_state": "Bullish", "freshness": "Stale", "bias_weight": 1.0},
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
            {"symbol": "SPX", "trend": "Bull", "macd_state": "Neutral", "freshness": "Weekend Closed", "bias_weight": 0.25},
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
            {"symbol": "SPX", "trend": "Bull", "macd_state": "Neutral", "freshness": "Fresh", "bias_weight": 1.0},
        ],
    )

    assert "Chart/TV mixed" in result["reasons"]


def test_chart_macd_state_adds_lightweight_bull_score():
    result = mb.classify_market_bias(
        {"VIX": make_snapshot(17.0, 18.0)},
        None,
        {},
        chart_data=[
            {"symbol": "USDJPY", "trend": "Neutral", "macd_state": "Bullish", "freshness": "Fresh", "bias_weight": 1.0},
        ],
    )

    assert result["scores"]["Bull"] == 1.5
    assert "Chart USDJPY 1h MACD is Bullish (weight 0.50)" in result["reasons"]


def test_chart_macd_state_adds_lightweight_bear_score_with_market_closed_weight():
    result = mb.classify_market_bias(
        {"VIX": make_snapshot(17.0, 18.0)},
        None,
        {},
        chart_data=[
            {"symbol": "USDJPY", "trend": "Neutral", "macd_state": "Bearish", "freshness": "Weekend Closed", "bias_weight": 0.25},
        ],
    )

    assert result["scores"]["Bear"] == 0.125


# --- Issue #3: Risk / Volatility axes, independent of Trend ---------------


def test_result_keeps_trend_fields_backward_compatible():
    """Existing consumers (e.g. paper-signal) key off market_bias/confidence/scores/reasons.

    These must stay byte-for-byte identical in shape; new axes are additive.
    """
    result = mb.classify_market_bias({}, None, {})

    assert result["market_bias"] == "Neutral"
    assert result["confidence"] == 0.0
    assert result["scores"] == {"Bull": 0, "Bear": 0}
    assert result["reasons"] == ["Insufficient market snapshot data for directional bias."]
    assert result["trend"] == result["market_bias"]


def test_result_exposes_risk_and_volatility_axes_with_allowed_labels():
    result = mb.classify_market_bias({}, None, {})

    assert result["risk"] in {"Risk-On", "Risk-Off", "Neutral"}
    assert result["volatility"] in {"Low", "Normal", "High"}
    assert isinstance(result["risk_reasons"], list) and result["risk_reasons"]
    assert isinstance(result["volatility_reasons"], list) and result["volatility_reasons"]
    assert 0.0 <= result["risk_confidence"] <= 1.0
    assert 0.0 <= result["volatility_confidence"] <= 1.0


def test_risk_off_when_vix_high_and_equities_falling():
    market_data = {
        "VIX": make_snapshot(30.0, 20.0),
        "SP500": make_snapshot(5380.0, 5420.0),
        "NASDAQ": make_snapshot(17100.0, 17200.0),
        "GOLD": make_snapshot(2450.0, 2400.0),
    }

    result = mb.classify_market_bias(market_data, None, {})

    assert result["risk"] == "Risk-Off"
    assert "VIX is above 25 (risk-off)" in result["risk_reasons"]
    assert "S&P500 is falling (risk-off)" in result["risk_reasons"]
    assert "Gold is rising (safe-haven demand, risk-off)" in result["risk_reasons"]


def test_risk_on_when_vix_low_and_equities_rising():
    market_data = {
        "VIX": make_snapshot(15.0, 16.0),
        "SP500": make_snapshot(5450.0, 5420.0),
        "NASDAQ": make_snapshot(17300.0, 17200.0),
    }

    result = mb.classify_market_bias(market_data, None, {})

    assert result["risk"] == "Risk-On"
    assert "VIX is below 18 (risk-on)" in result["risk_reasons"]
    assert "S&P500 is rising (risk-on)" in result["risk_reasons"]


def test_risk_is_neutral_when_signals_are_balanced():
    market_data = {
        "SP500": make_snapshot(5450.0, 5420.0),
        "NASDAQ": make_snapshot(17100.0, 17200.0),
    }

    result = mb.classify_market_bias(market_data, None, {})

    assert result["risk"] == "Neutral"
    assert "Risk-On and Risk-Off signals are balanced, so risk is Neutral." in result["risk_reasons"]


def test_risk_axis_is_independent_of_trend_score():
    """Trend uses VIX/SP500/Nasdaq too, but the two axes must score separately."""
    market_data = {
        "VIX": make_snapshot(30.0, 20.0),
        "SP500": make_snapshot(5380.0, 5420.0),
        "NASDAQ": make_snapshot(17100.0, 17200.0),
    }

    result = mb.classify_market_bias(market_data, None, {})

    assert result["market_bias"] == "Bear"
    assert result["risk"] == "Risk-Off"
    assert result["scores"] != result["risk_scores"]


def test_volatility_is_high_when_chart_bollinger_band_is_wide():
    result = mb.classify_market_bias(
        {},
        None,
        {},
        chart_data=[
            {"symbol": "USDJPY", "trend": "Neutral", "freshness": "Fresh", "bias_weight": 1.0, "bb_width_pct": 1.8},
        ],
    )

    assert result["volatility"] == "High"
    assert "Chart USDJPY 1h Bollinger Band width is 1.80% (expanded)" in result["volatility_reasons"]


def test_volatility_is_low_when_chart_bollinger_band_is_narrow():
    result = mb.classify_market_bias(
        {},
        None,
        {},
        chart_data=[
            {"symbol": "USDJPY", "trend": "Neutral", "freshness": "Fresh", "bias_weight": 1.0, "bb_width_pct": 0.2},
        ],
    )

    assert result["volatility"] == "Low"
    assert "Chart USDJPY 1h Bollinger Band width is 0.20% (contracted)" in result["volatility_reasons"]


def test_volatility_is_normal_when_chart_bollinger_band_is_mid_range():
    result = mb.classify_market_bias(
        {},
        None,
        {},
        chart_data=[
            {"symbol": "USDJPY", "trend": "Neutral", "freshness": "Fresh", "bias_weight": 1.0, "bb_width_pct": 0.8},
        ],
    )

    assert result["volatility"] == "Normal"
    assert "Chart USDJPY 1h Bollinger Band width is 0.80% (normal range)" in result["volatility_reasons"]
    assert "Insufficient chart/market data" not in result["volatility_reasons"][0]


def test_volatility_defaults_to_normal_when_no_data():
    result = mb.classify_market_bias({}, None, {})

    assert result["volatility"] == "Normal"
    assert result["volatility_confidence"] == 0.0
    assert "defaulting to Normal" in result["volatility_reasons"][0]


def test_volatility_stale_chart_data_is_excluded():
    result = mb.classify_market_bias(
        {},
        None,
        {},
        chart_data=[
            {"symbol": "USDJPY", "trend": "Neutral", "freshness": "Stale", "bias_weight": 1.0, "bb_width_pct": 5.0},
        ],
    )

    assert result["volatility"] == "Normal"


def test_volatility_rapid_market_move_is_flagged_high():
    market_data = {"SP500": make_snapshot(5600.0, 5500.0)}

    result = mb.classify_market_bias(market_data, None, {})

    assert result["volatility"] == "High"
    assert any("S&P500 moved" in reason for reason in result["volatility_reasons"])


def test_conflicting_usdjpy_style_market_shows_distinct_trend_risk_volatility():
    """A mixed-factor market must not collapse everything into a single Neutral.

    SP500 up / Nasdaq down cancel out for Trend (Neutral), while Gold and Oil
    spiking (not part of Trend scoring at all) make Risk clearly Risk-Off,
    and the combination of a wide chart Bollinger Band plus fast Gold/Oil
    moves makes Volatility clearly High. Trend, Risk, and Volatility must
    each surface their own independent read instead of one flat Neutral.
    """
    market_data = {
        "SP500": make_snapshot(5450.0, 5420.0),
        "NASDAQ": make_snapshot(17100.0, 17200.0),
        "GOLD": make_snapshot(2500.0, 2400.0),
        "OIL": make_snapshot(90.0, 80.0),
    }
    chart_data = [
        {"symbol": "USDJPY", "trend": "Neutral", "freshness": "Fresh", "bias_weight": 1.0, "bb_width_pct": 1.6},
    ]

    result = mb.classify_market_bias(market_data, None, {}, chart_data=chart_data)

    assert result["market_bias"] == "Neutral"
    assert result["risk"] == "Risk-Off"
    assert result["volatility"] == "High"
    assert result["risk"] != result["market_bias"]
    assert result["volatility"] != result["market_bias"]


def test_format_market_bias_section_renders_all_three_axes_separately():
    result = mb.classify_market_bias(
        {
            "VIX": make_snapshot(30.0, 20.0),
            "SP500": make_snapshot(5380.0, 5420.0),
            "NASDAQ": make_snapshot(17100.0, 17200.0),
        },
        None,
        {},
        chart_data=[
            {"symbol": "USDJPY", "trend": "Neutral", "freshness": "Fresh", "bias_weight": 1.0, "bb_width_pct": 1.8},
        ],
    )

    section = mb.format_market_bias_section(result)

    assert "Trend: Bear" in section
    assert "Risk: Risk-Off" in section
    assert "Volatility: High" in section
    assert "Reasons [Trend]:" in section
    assert "Reasons [Risk]:" in section
    assert "Reasons [Volatility]:" in section
    assert "VIX is above 25 (risk-off)" in section
    assert "Chart USDJPY 1h Bollinger Band width is 1.80% (expanded)" in section
    assert PROHIBITED_USER_WORDS.search(section) is None
