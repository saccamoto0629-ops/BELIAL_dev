import pathlib
import re
import sys

import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import signal_generator as sg  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_chart_db(monkeypatch, tmp_path):
    # Issue #55: run_once consults the Chart DB for snapshot attachment; point
    # it at a nonexistent tmp path so no test ever reads or writes a real one.
    monkeypatch.setenv("CHART_DB_PATH", str(tmp_path / "chart_data.sqlite3"))


FORBIDDEN_WORDS = re.compile(
    r"\b(order|position|lot|leverage|stop[_ -]?loss|take[_ -]?profit|entry|execute)\b",
    re.IGNORECASE,
)


def make_chart_snapshot(**overrides):
    snapshot = {
        "symbol": "USDJPY",
        "timeframe": "1h",
        # Issue #41: a confirmed-candle open timestamp well in the past, so the
        # candle is closed at any real evaluation time. build_candidates() ignores
        # it; run_once() uses it as the target candle. Override for candle tests.
        "timestamp": "2026-07-05T12:00:00Z",
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


# --- Issue #27: every candidate carries the four classification fields ---


def test_build_candidates_includes_classification_fields():
    candidates = sg.build_candidates(
        make_bias(market_bias="Bull"),
        [make_chart_snapshot(trend="Bull", macd_state="Bullish", freshness="Fresh", bias_weight=1.0)],
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["signal_type"] == "trend_follow"
    assert candidate["trigger_type"] == "macd_state_alignment"
    assert candidate["alert_source"] == "composite"
    assert candidate["strategy_profile"] == "market_bias_macd_alignment_v1"


def test_build_candidates_never_records_a_macd_cross_trigger():
    # The current logic aligns Market Bias with the MACD *state*; it does not
    # detect a live MACD cross event, so recording macd_cross would be false.
    candidates = sg.build_candidates(
        make_bias(market_bias="Bear"),
        [make_chart_snapshot(trend="Bear", macd_state="Bearish", freshness="Fresh", bias_weight=1.0)],
    )

    assert candidates[0]["trigger_type"] != "macd_cross"
    assert candidates[0]["trigger_type"] == "macd_state_alignment"


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


# --- Issue #20: generate_signal_candidates() / generate_signal() for Backtest reuse ---


def make_context(**overrides):
    from signal_models import create_signal_context

    kwargs = {
        "bias_result": make_bias(market_bias="Bull", confidence=0.8),
        "hourly_snapshots": [make_chart_snapshot(macd_state="Bullish", freshness="Fresh", bias_weight=1.0)],
        "confidence_threshold": 0.40,
    }
    kwargs.update(overrides)
    return create_signal_context(
        kwargs["bias_result"], kwargs["hourly_snapshots"], confidence_threshold=kwargs["confidence_threshold"]
    )


def test_generate_signal_candidates_matches_build_candidates_with_no_side_effects(monkeypatch):
    class FailingSession:
        def post(self, *args, **kwargs):
            raise AssertionError("HTTP must not be called by generate_signal_candidates()")

    monkeypatch.setattr(sg.requests, "post", FailingSession().post)

    class FailingStore:
        def __getattr__(self, name):
            raise AssertionError(f"paper_signal_store.{name} must not be called by generate_signal_candidates()")

    monkeypatch.setattr(sg, "store", FailingStore())

    context = make_context()
    candidates = sg.generate_signal_candidates(context)

    assert candidates == sg.build_candidates(
        context["bias_result"], context["hourly_snapshots"], confidence_threshold=context["confidence_threshold"]
    )
    assert len(candidates) == 1
    assert candidates[0]["side"] == "long"


def test_generate_signal_candidates_is_deterministic_for_the_same_context():
    context = make_context()

    first = sg.generate_signal_candidates(context)
    second = sg.generate_signal_candidates(context)

    assert first == second


def test_generate_signal_returns_first_candidate():
    context = make_context()

    signal = sg.generate_signal(context)

    candidates = sg.generate_signal_candidates(context)
    assert signal == candidates[0]


def test_generate_signal_returns_none_when_no_candidates():
    context = make_context(bias_result=make_bias(market_bias="Neutral"))

    assert sg.generate_signal(context) is None


def test_generate_signal_candidates_never_creates_a_db_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sg, "DB_PATH", tmp_path / "paper_signals.sqlite3")

    context = make_context()
    sg.generate_signal_candidates(context)
    sg.generate_signal(context)

    assert not (tmp_path / "paper_signals.sqlite3").exists()


def test_run_once_uses_build_confirmed_candidate(monkeypatch):
    # Issue #41: run_once() delegates per-snapshot generation to
    # build_confirmed_candidate() (which reuses the unchanged build_candidates
    # strategy) once per snapshot, and yields the confirmed candidate.
    sources = {
        "market": {},
        "fred": None,
        "tradingview": {},
        "chart": [make_chart_snapshot(macd_state="Bullish", freshness="Fresh", bias_weight=1.0)],
    }
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda **kw: sources)
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias(market_bias="Bull", confidence=0.8))
    monkeypatch.setattr(sg, "post_discord_message", lambda message, dry_run=False: None)

    calls = []
    original = sg.build_confirmed_candidate

    def spy(snapshot, bias, provenance, **kwargs):
        calls.append((snapshot, bias, provenance))
        return original(snapshot, bias, provenance, **kwargs)

    monkeypatch.setattr(sg, "build_confirmed_candidate", spy)

    created = sg.run_once(dry_run=True)

    assert len(calls) == 1
    assert len(created) == 1
    assert created[0]["target_candle_utc"] == "2026-07-05T12:00:00Z"


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
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda **kw: sources)
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias(market_bias="Bull", confidence=0.8))

    posted = []
    monkeypatch.setattr(
        sg, "post_discord_message", lambda message, dry_run=False, image_url=None: posted.append(message)
    )

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
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda **kw: sources)
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
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda **kw: sources)
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias(market_bias="Bull", confidence=0.8))
    monkeypatch.setattr(sg, "post_discord_message", lambda message, dry_run=False, image_url=None: None)

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


def test_discord_message_includes_classification_labels():
    from signal_models import create_paper_signal

    candidate = sg.build_candidates(
        make_bias(market_bias="Bull", confidence=0.8),
        [make_chart_snapshot(macd_state="Bullish", freshness="Fresh", bias_weight=1.0)],
    )[0]
    signal = create_paper_signal(candidate)

    message = sg.format_discord_message(signal)

    assert "Signal Type: trend_follow" in message
    assert "Trigger Type: macd_state_alignment" in message
    assert "Strategy Profile: market_bias_macd_alignment_v1" in message
    # the paper-only disclaimer must still be present and last
    assert message.rstrip().endswith("Paper signal only. No trade execution.")


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


# =============================================================================
# Issue #40: per-symbol Market Bias (symbol-specific preferred, GLOBAL fallback)
# =============================================================================


def _scope_context(bias_by_scope, snapshots, *, confidence_threshold=0.40):
    from signal_models import create_signal_context

    # bias_result is required by SignalContext but must be ignored when
    # bias_by_scope is present; pass a Neutral one to prove it is unused.
    return create_signal_context(
        make_bias(market_bias="Neutral", confidence=0.0),
        snapshots,
        confidence_threshold=confidence_threshold,
        bias_by_scope=bias_by_scope,
    )


def test_scope_bias_processes_two_symbols_with_opposite_bias_in_one_run():
    # USDJPY is Bull (+Bullish MACD) -> long; XAUUSD is Bear (+Bearish MACD) -> short.
    usdjpy = make_chart_snapshot(symbol="USDJPY", macd_state="Bullish")
    xauusd = make_chart_snapshot(symbol="XAUUSD", macd_state="Bearish", trend="Bear")
    bias_by_scope = {
        ("USDJPY", "1h"): make_bias(market_bias="Bull", confidence=0.8),
        ("XAUUSD", "1h"): make_bias(market_bias="Bear", confidence=0.8),
        ("GLOBAL", "1h"): make_bias(market_bias="Neutral", confidence=0.0),
    }

    candidates = sg.generate_signal_candidates(_scope_context(bias_by_scope, [usdjpy, xauusd]))

    by_symbol = {c["symbol"]: c for c in candidates}
    assert by_symbol["USDJPY"]["side"] == "long"
    assert by_symbol["XAUUSD"]["side"] == "short"


def test_scope_bias_falls_back_to_global_only_for_symbols_without_specific_bias():
    # USDJPY has its own Bull bias; SPX has none, so it uses the GLOBAL Bear bias.
    usdjpy = make_chart_snapshot(symbol="USDJPY", macd_state="Bullish")
    spx = make_chart_snapshot(symbol="SPX", macd_state="Bearish", trend="Bear")
    bias_by_scope = {
        ("USDJPY", "1h"): make_bias(market_bias="Bull", confidence=0.8),
        ("GLOBAL", "1h"): make_bias(market_bias="Bear", confidence=0.8),
    }

    candidates = sg.generate_signal_candidates(_scope_context(bias_by_scope, [usdjpy, spx]))

    by_symbol = {c["symbol"]: c for c in candidates}
    assert by_symbol["USDJPY"]["side"] == "long"  # symbol-specific
    assert by_symbol["SPX"]["side"] == "short"    # GLOBAL fallback


def test_scope_bias_symbol_with_neutral_bias_produces_no_candidate():
    # XAUUSD is Neutral even though GLOBAL is Bull: no XAUUSD candidate.
    usdjpy = make_chart_snapshot(symbol="USDJPY", macd_state="Bullish")
    xauusd = make_chart_snapshot(symbol="XAUUSD", macd_state="Bullish")
    bias_by_scope = {
        ("USDJPY", "1h"): make_bias(market_bias="Bull", confidence=0.8),
        ("XAUUSD", "1h"): make_bias(market_bias="Neutral", confidence=0.0),
        ("GLOBAL", "1h"): make_bias(market_bias="Bull", confidence=0.8),
    }

    candidates = sg.generate_signal_candidates(_scope_context(bias_by_scope, [usdjpy, xauusd]))

    symbols = {c["symbol"] for c in candidates}
    assert symbols == {"USDJPY"}  # XAUUSD's own Neutral bias wins over GLOBAL Bull


def test_scope_bias_applies_confidence_threshold_per_symbol():
    # Same threshold, but each symbol's own confidence decides eligibility.
    usdjpy = make_chart_snapshot(symbol="USDJPY", macd_state="Bullish")
    xauusd = make_chart_snapshot(symbol="XAUUSD", macd_state="Bullish")
    bias_by_scope = {
        ("USDJPY", "1h"): make_bias(market_bias="Bull", confidence=0.60),
        ("XAUUSD", "1h"): make_bias(market_bias="Bull", confidence=0.30),
        ("GLOBAL", "1h"): make_bias(market_bias="Bull", confidence=0.90),
    }

    candidates = sg.generate_signal_candidates(_scope_context(bias_by_scope, [usdjpy, xauusd], confidence_threshold=0.50))

    symbols = {c["symbol"] for c in candidates}
    assert symbols == {"USDJPY"}  # XAUUSD (0.30) is below threshold, USDJPY (0.60) passes


def test_scope_bias_generation_is_pure_no_db_discord_http(tmp_path, monkeypatch):
    monkeypatch.setattr(sg, "DB_PATH", tmp_path / "paper_signals.sqlite3")

    class FailingSession:
        def post(self, *args, **kwargs):
            raise AssertionError("HTTP must not be called by scope-based candidate generation")

    monkeypatch.setattr(sg.requests, "post", FailingSession().post)

    class FailingStore:
        def __getattr__(self, name):
            raise AssertionError(f"paper_signal_store.{name} must not be called")

    monkeypatch.setattr(sg, "store", FailingStore())

    bias_by_scope = {("USDJPY", "1h"): make_bias(market_bias="Bull", confidence=0.8)}
    context = _scope_context(bias_by_scope, [make_chart_snapshot(symbol="USDJPY", macd_state="Bullish")])
    sg.generate_signal_candidates(context)

    assert not (tmp_path / "paper_signals.sqlite3").exists()


def test_legacy_single_bias_context_still_supported():
    # No bias_by_scope: the single bias_result is applied to every snapshot,
    # exactly as before Issue #40 (Backtest's path).
    from signal_models import create_signal_context

    context = create_signal_context(
        make_bias(market_bias="Bull", confidence=0.8),
        [make_chart_snapshot(symbol="USDJPY", macd_state="Bullish")],
        confidence_threshold=0.40,
    )
    candidates = sg.generate_signal_candidates(context)

    assert len(candidates) == 1
    assert candidates[0]["side"] == "long"


def test_run_once_uses_per_symbol_bias_so_one_symbol_bull_does_not_force_another(monkeypatch):
    # Two symbols share one live run. classify_market_bias returns Bull only when
    # the (filtered) chart data is the USDJPY Bullish snapshot; XAUUSD's own
    # (Bearish) snapshot classifies Neutral -> no XAUUSD candidate from a Bull.
    usdjpy = make_chart_snapshot(symbol="USDJPY", macd_state="Bullish", trend="Bull")
    xauusd = make_chart_snapshot(symbol="XAUUSD", macd_state="Bullish", trend="Bull")
    sources = {"market": {}, "fred": None, "tradingview": {}, "chart": [usdjpy, xauusd]}
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda **kw: sources)

    def fake_classify(*args, **kwargs):
        chart = kwargs.get("chart_data") or []
        symbols = {sg._snapshot_get(s, "symbol") for s in chart}
        # USDJPY-only classification -> Bull; anything else (GLOBAL mix or XAUUSD) -> Neutral.
        if symbols == {"USDJPY"}:
            return make_bias(market_bias="Bull", confidence=0.8)
        return make_bias(market_bias="Neutral", confidence=0.0)

    monkeypatch.setattr(sg, "classify_market_bias", fake_classify)
    monkeypatch.setattr(sg, "post_discord_message", lambda message, dry_run=False: None)

    created = sg.run_once(dry_run=True)

    assert {c["symbol"] for c in created} == {"USDJPY"}
