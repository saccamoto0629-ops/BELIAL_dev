import pathlib
import re
import sys

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import signal_generator as sg  # noqa: E402


FORBIDDEN_WORDS = re.compile(
    r"\b(order|position|lot|leverage|stop[_ -]?loss|take[_ -]?profit|entry|execute)\b",
    re.IGNORECASE,
)


def make_chart_snapshot(**overrides):
    snapshot = {
        "symbol": "USDJPY",
        "timeframe": "1h",
        "trend": "Bull",
        "state": "Inside band",
        "freshness": "Fresh",
        "bias_weight": 1.0,
        "macd_state": "Bullish",
    }
    snapshot.update(overrides)
    return snapshot


def make_bias(market_bias="Bull", confidence=0.72):
    return {"market_bias": market_bias, "confidence": confidence, "scores": {}, "reasons": [], "warnings": [], "note": ""}


# --- Rule 1 & 2: Market Bias must be Bull/Bear and confidence over threshold ---


def test_no_candidates_when_bias_is_neutral():
    candidates = sg.build_candidates(make_bias(market_bias="Neutral"), [make_chart_snapshot()])
    assert candidates == []


def test_no_candidates_when_confidence_below_threshold():
    candidates = sg.build_candidates(
        make_bias(confidence=0.39), [make_chart_snapshot()], confidence_threshold=0.40
    )
    assert candidates == []


def test_candidate_generated_at_exact_confidence_threshold():
    candidates = sg.build_candidates(
        make_bias(confidence=0.40), [make_chart_snapshot()], confidence_threshold=0.40
    )
    assert len(candidates) == 1


# --- Rule 4: Bull -> long ---


def test_bull_bias_with_bullish_macd_generates_long_signal():
    candidates = sg.build_candidates(
        make_bias(market_bias="Bull"),
        [make_chart_snapshot(trend="Bull", macd_state="Bullish", freshness="Fresh", bias_weight=1.0)],
    )

    assert len(candidates) == 1
    assert candidates[0]["side"] == "long"
    assert candidates[0]["symbol"] == "USDJPY"
    assert candidates[0]["timeframe"] == "1h"
    assert candidates[0]["market_bias"] == "Bull"


def test_bull_bias_with_non_bullish_macd_generates_no_signal():
    candidates = sg.build_candidates(
        make_bias(market_bias="Bull"),
        [make_chart_snapshot(macd_state="Neutral")],
    )
    assert candidates == []


# --- Rule 5: Bear -> short ---


def test_bear_bias_with_bearish_macd_generates_short_signal():
    candidates = sg.build_candidates(
        make_bias(market_bias="Bear"),
        [make_chart_snapshot(trend="Bear", macd_state="Bearish", freshness="Fresh", bias_weight=1.0)],
    )

    assert len(candidates) == 1
    assert candidates[0]["side"] == "short"


def test_bear_bias_with_bullish_macd_generates_no_signal():
    candidates = sg.build_candidates(
        make_bias(market_bias="Bear"),
        [make_chart_snapshot(macd_state="Bullish")],
    )
    assert candidates == []


# --- Rule 6: Weekend Closed / Market Closed / Stale never produce a signal ---


def test_weekend_closed_freshness_produces_no_signal():
    candidates = sg.build_candidates(
        make_bias(market_bias="Bull"),
        [make_chart_snapshot(macd_state="Bullish", freshness="Weekend Closed")],
    )
    assert candidates == []


def test_market_closed_freshness_produces_no_signal():
    candidates = sg.build_candidates(
        make_bias(market_bias="Bull"),
        [make_chart_snapshot(macd_state="Bullish", freshness="Market Closed")],
    )
    assert candidates == []


def test_stale_freshness_produces_no_signal():
    candidates = sg.build_candidates(
        make_bias(market_bias="Bull"),
        [make_chart_snapshot(macd_state="Bullish", freshness="Stale")],
    )
    assert candidates == []


def test_zero_bias_weight_produces_no_signal():
    candidates = sg.build_candidates(
        make_bias(market_bias="Bull"),
        [make_chart_snapshot(macd_state="Bullish", freshness="Fresh", bias_weight=0.0)],
    )
    assert candidates == []


def test_handles_empty_chart_snapshots_without_crashing():
    candidates = sg.build_candidates(make_bias(market_bias="Bull"), [])
    assert candidates == []


def test_handles_missing_bias_fields_without_crashing():
    candidates = sg.build_candidates({}, [make_chart_snapshot()])
    assert candidates == []


# --- Rule 9: duplicate suppression within the run_once pipeline ---


def test_run_once_generates_signal_then_suppresses_duplicate(tmp_path, monkeypatch):
    db_path = tmp_path / "paper_signals.sqlite3"
    monkeypatch.setattr(sg, "DB_PATH", db_path)

    sources = {
        "market": {},
        "fred": None,
        "tradingview": {},
        "chart": [make_chart_snapshot(macd_state="Bullish", freshness="Fresh", bias_weight=1.0)],
    }
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda: sources)
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias(market_bias="Bull", confidence=0.8))

    posted = []
    monkeypatch.setattr(sg, "post_discord_message", lambda message, dry_run=False: posted.append(message))

    first = sg.run_once(dry_run=False, db_path=db_path)
    second = sg.run_once(dry_run=False, db_path=db_path)

    assert len(first) == 1
    assert second == []
    assert len(posted) == 1


def test_run_once_dry_run_does_not_create_db_or_call_discord_http(tmp_path, monkeypatch):
    db_path = tmp_path / "paper_signals.sqlite3"

    sources = {
        "market": {},
        "fred": None,
        "tradingview": {},
        "chart": [make_chart_snapshot(macd_state="Bullish", freshness="Fresh", bias_weight=1.0)],
    }
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda: sources)
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias(market_bias="Bull", confidence=0.8))

    class FailingSession:
        def post(self, *args, **kwargs):
            raise AssertionError("HTTP post must not be called in dry-run")

    monkeypatch.setattr(sg.requests, "post", FailingSession().post)

    created = sg.run_once(dry_run=True, db_path=db_path)

    assert len(created) == 1
    assert not db_path.exists()


def test_run_once_normal_mode_persists_signal(tmp_path, monkeypatch):
    db_path = tmp_path / "paper_signals.sqlite3"

    sources = {
        "market": {},
        "fred": None,
        "tradingview": {},
        "chart": [make_chart_snapshot(macd_state="Bullish", freshness="Fresh", bias_weight=1.0)],
    }
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda: sources)
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias(market_bias="Bull", confidence=0.8))
    monkeypatch.setattr(sg, "post_discord_message", lambda message, dry_run=False: None)

    created = sg.run_once(dry_run=False, db_path=db_path)

    assert len(created) == 1
    assert db_path.exists()
    import paper_signal_store as store

    rows = store.list_signals(db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["signal_id"] == created[0]["signal_id"]


# --- Rule 10: Discord message always carries the paper-only disclaimer ---


def test_discord_message_always_includes_paper_only_disclaimer():
    from signal_models import create_paper_signal

    candidate = sg.build_candidates(
        make_bias(market_bias="Bull", confidence=0.8),
        [make_chart_snapshot(macd_state="Bullish", freshness="Fresh", bias_weight=1.0)],
    )[0]
    signal = create_paper_signal(candidate)

    message = sg.format_discord_message(signal)

    assert "Paper signal only. No trade execution." in message


def test_discord_message_contains_no_trade_execution_language():
    from signal_models import create_paper_signal

    candidate = sg.build_candidates(
        make_bias(market_bias="Bear", confidence=0.8),
        [make_chart_snapshot(trend="Bear", macd_state="Bearish", freshness="Fresh", bias_weight=1.0)],
    )[0]
    signal = create_paper_signal(candidate)

    message = sg.format_discord_message(signal)

    assert FORBIDDEN_WORDS.search(message) is None


# --- Constraint: no broker/order API integration anywhere in this module ---


def test_module_source_has_no_broker_api_integration():
    # The module docstring legitimately *names* OANDA/MT5/XM to disclaim them,
    # so this only checks actual code: import statements and hardcoded broker
    # URLs, neither of which should ever reference a broker/order API.
    source = pathlib.Path(sg.__file__).read_text(encoding="utf-8")
    import_lines = "\n".join(
        line for line in source.splitlines() if line.strip().startswith(("import ", "from "))
    ).lower()
    for forbidden in ("oanda", "mt5", "metatrader", "broker"):
        assert forbidden not in import_lines, f"unexpected broker import: {forbidden!r}"
    for forbidden_call in ("place_order", "submit_order", "broker_api("):
        assert forbidden_call not in source.lower(), f"unexpected broker call: {forbidden_call!r}"
    assert "fxtrade.oanda.com" not in source.lower()
