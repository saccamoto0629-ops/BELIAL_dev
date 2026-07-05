import pathlib
import sys

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import signal_models as sm  # noqa: E402


def make_candidate(**overrides):
    candidate = {
        "symbol": "USDJPY",
        "timeframe": "1h",
        "side": "long",
        "market_bias": "Bull",
        "confidence": 0.72,
        "macd_state": "Bullish",
        "trend": "Bull",
        "state": "Inside band",
        "freshness": "Fresh",
        "reason": "Market Bias=Bull (72%); USDJPY 1h MACD=Bullish, Trend=Bull, State=Inside band, Freshness=Fresh.",
    }
    candidate.update(overrides)
    return candidate


def test_create_paper_signal_has_all_required_fields():
    signal = sm.create_paper_signal(make_candidate())

    assert set(signal.keys()) == {
        "signal_id",
        "created_at_utc",
        "symbol",
        "timeframe",
        "side",
        "market_bias",
        "confidence",
        "macd_state",
        "trend",
        "state",
        "freshness",
        "reason",
        "status",
    }


def test_create_paper_signal_status_is_always_paper_only():
    signal = sm.create_paper_signal(make_candidate())

    assert signal["status"] == "paper_only"


def test_create_paper_signal_generates_unique_ids():
    first = sm.create_paper_signal(make_candidate())
    second = sm.create_paper_signal(make_candidate())

    assert first["signal_id"] != second["signal_id"]


def test_create_paper_signal_uses_supplied_id_and_timestamp():
    signal = sm.create_paper_signal(
        make_candidate(), signal_id="fixed-id", created_at_utc="2026-07-05T00:00:00Z"
    )

    assert signal["signal_id"] == "fixed-id"
    assert signal["created_at_utc"] == "2026-07-05T00:00:00Z"


def test_utc_now_iso_ends_with_z_suffix():
    assert sm.utc_now_iso().endswith("Z")


def test_generated_signal_field_values_contain_no_trade_execution_language():
    # The module docstring legitimately *names* order/lot/leverage to disclaim
    # them, so this checks the generated record's field values, not source
    # text: nothing produced by create_paper_signal() should ever look like
    # an order, lot size, or stop-loss/take-profit instruction.
    signal = sm.create_paper_signal(make_candidate())
    forbidden = ("order", "lot", "leverage", "stop_loss", "stop-loss", "take_profit", "take-profit")
    for key, value in signal.items():
        if key == "status":
            continue
        text = str(value).lower()
        for word in forbidden:
            assert word not in text, f"field {key!r} unexpectedly contains {word!r}: {value!r}"
