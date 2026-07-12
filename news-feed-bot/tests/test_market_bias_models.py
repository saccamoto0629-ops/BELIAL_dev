import json
import pathlib
import sys

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import market_bias_models as models  # noqa: E402


def test_create_market_bias_record_maps_classify_market_bias_output():
    bias_result = {
        "market_bias": "Bear",
        "confidence": 0.65,
        "trend": "Bear",
        "risk": "Risk-Off",
        "risk_confidence": 0.5,
        "volatility": "High",
        "volatility_confidence": 0.9,
        "reasons": ["VIX is above 25"],
        "risk_reasons": ["VIX is above 25 (risk-off)"],
        "volatility_reasons": ["Chart USDJPY 1h Bollinger Band width is 1.80% (expanded)"],
        "warnings": ["some warning"],
    }

    record = models.create_market_bias_record(bias_result)

    assert record["market_bias"] == "Bear"
    assert record["confidence"] == 0.65
    assert record["trend"] == "Bear"
    assert record["risk"] == "Risk-Off"
    assert record["volatility"] == "High"
    assert json.loads(record["reasons_json"]) == ["VIX is above 25"]
    assert json.loads(record["risk_reasons_json"]) == ["VIX is above 25 (risk-off)"]
    assert json.loads(record["volatility_reasons_json"]) == [
        "Chart USDJPY 1h Bollinger Band width is 1.80% (expanded)"
    ]
    assert json.loads(record["warnings_json"]) == ["some warning"]


def test_create_market_bias_record_defaults_scope_to_global_and_1h():
    record = models.create_market_bias_record({"market_bias": "Neutral", "confidence": 0.0})

    assert record["symbol"] == "GLOBAL"
    assert record["timeframe"] == "1h"
    assert record["source_snapshot_id"] is None


def test_create_market_bias_record_accepts_symbol_timeframe_and_snapshot_id_overrides():
    record = models.create_market_bias_record(
        {"market_bias": "Bull", "confidence": 0.5},
        symbol="USD_JPY",
        timeframe="4h",
        source_snapshot_id="snapshot-123",
    )

    assert record["symbol"] == "USDJPY"
    assert record["timeframe"] == "4h"
    assert record["source_snapshot_id"] == "snapshot-123"


def test_create_market_bias_record_handles_missing_risk_and_volatility_fields():
    """A bias_result without the newer Risk/Volatility axes must not raise."""
    minimal_bias = {"market_bias": "Neutral", "confidence": 0.0, "reasons": [], "warnings": []}

    record = models.create_market_bias_record(minimal_bias)

    assert record["risk"] is None
    assert record["volatility"] is None
    assert record["trend"] == "Neutral"
    assert json.loads(record["reasons_json"]) == []


def test_create_market_bias_record_generates_unique_ids_and_timestamps():
    record_a = models.create_market_bias_record({"market_bias": "Bull", "confidence": 0.1})
    record_b = models.create_market_bias_record({"market_bias": "Bull", "confidence": 0.1})

    assert record_a["market_bias_id"] != record_b["market_bias_id"]
    assert record_a["evaluated_at"].endswith("Z")


# --- Issue #40: symbol normalization + fallback scope ordering ---------------


def test_normalize_bias_symbol_canonicalizes_punctuation_and_case():
    assert models.normalize_bias_symbol("USD_JPY") == "USDJPY"
    assert models.normalize_bias_symbol("usd_jpy") == "USDJPY"
    assert models.normalize_bias_symbol("XAU/USD") == "XAUUSD"
    assert models.normalize_bias_symbol("USDJPY") == "USDJPY"


def test_normalize_bias_symbol_maps_global_and_empty_to_global():
    assert models.normalize_bias_symbol("GLOBAL") == "GLOBAL"
    assert models.normalize_bias_symbol("global") == "GLOBAL"
    assert models.normalize_bias_symbol("") == "GLOBAL"
    assert models.normalize_bias_symbol(None) == "GLOBAL"


def test_create_market_bias_record_normalizes_symbol_and_timeframe():
    record = models.create_market_bias_record(
        {"market_bias": "Bull", "confidence": 0.8},
        symbol=" usd_jpy ",
        timeframe=" 1H ",
    )

    assert record["symbol"] == "USDJPY"
    assert record["timeframe"] == "1h"


def test_invalid_symbol_and_timeframe_are_rejected():
    import pytest

    with pytest.raises(TypeError):
        models.normalize_bias_symbol(123)
    with pytest.raises(ValueError):
        models.effective_bias_scopes("USDJPY", "   ")


def test_effective_bias_scopes_full_priority_for_non_1h_timeframe():
    assert models.effective_bias_scopes("USD_JPY", "15m") == [
        ("USDJPY", "15m"),
        ("USDJPY", "1h"),
        ("GLOBAL", "15m"),
        ("GLOBAL", "1h"),
    ]


def test_effective_bias_scopes_dedupes_when_timeframe_is_1h():
    assert models.effective_bias_scopes("USD_JPY", "1h") == [
        ("USDJPY", "1h"),
        ("GLOBAL", "1h"),
    ]


def test_effective_bias_scopes_for_global_request_is_global_only():
    assert models.effective_bias_scopes("GLOBAL", "1h") == [("GLOBAL", "1h")]
    assert models.effective_bias_scopes("GLOBAL", "15m") == [("GLOBAL", "15m"), ("GLOBAL", "1h")]
