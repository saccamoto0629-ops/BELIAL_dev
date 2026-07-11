"""Issue #28 regression: Live Paper Signal == Backtest Signal for identical input.

This file guarantees the Issue #28 property end to end:

    same candles
    + same candle timestamp
    + same evaluation timestamp
    + same indicators
    + same bias
    + same config
            -> Live run_once() and Backtest run_backtest()
            -> the same Signal Candidate fields

It drives the REAL production paths -- Live's chart_snapshot.build_indicator_snapshot()
and signal_generator.run_once(), and Backtest's warmup.evaluate_backtest_bars() and
runner.run_backtest() -- never hand-written snapshots for the things under test. Both
paths converge on the single shared pure rule generate_signal_candidates() ->
build_candidates(), and on the single shared freshness/confirmed-candle rule in
market-watch/freshness.py. There is no second copy of the Bull/Bear, confidence,
MACD-alignment, freshness, bias_weight, side, or classification logic on the Backtest
side, and ``mode`` is never a branch condition.

All comments here are plain ASCII/UTF-8 (Issue #28 review point 6: no mojibake).

Section map (review Major points):
  1. same candle timestamp / timezone, and which bar produced the candidate
  2. freshness / bias_weight derived from the one shared rule (not hardcoded)
  3. confirmed-candle selection: forming candle excluded, timeframe boundary
  4. real entrypoints run_once() vs run_backtest()
  5. Stale through the real Backtest snapshot path (freshness not faked Fresh)
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile

import pandas as pd
import pytest

TESTS_DIR = pathlib.Path(__file__).resolve().parent
BACKTEST_DIR = TESTS_DIR.parent
REPO_ROOT = BACKTEST_DIR.parent
MARKET_WATCH_DIR = REPO_ROOT / "market-watch"
PAPER_SIGNAL_DIR = REPO_ROOT / "paper-signal"
NEWS_FEED_BOT_DIR = REPO_ROOT / "news-feed-bot"

for _dir in (TESTS_DIR, BACKTEST_DIR, MARKET_WATCH_DIR, PAPER_SIGNAL_DIR, NEWS_FEED_BOT_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import warmup  # noqa: E402  (backtest)
import runner  # noqa: E402  (backtest; also puts paper-signal/news-feed-bot on sys.path)
import backtest_store as bt_store  # noqa: E402  (backtest)
import chart_collector as collector  # noqa: E402  (market-watch)
import chart_snapshot as cs  # noqa: E402  (market-watch)
from chart_models import ChartTarget  # noqa: E402  (market-watch)
from freshness import resolve_freshness  # noqa: E402  (market-watch)

import signal_generator as sg  # noqa: E402  (paper-signal)
from signal_models import create_paper_signal, create_signal_context  # noqa: E402  (paper-signal)

from candle_fixtures import make_uptrend_candles, seed_bull_bias  # noqa: E402

DEFAULT_THRESHOLD = sg.DEFAULT_CONFIDENCE_THRESHOLD  # 0.40, shared by both paths
TIMEFRAME = "1h"
HOUR = pd.Timedelta(hours=1)

# Candidate keys Issue #28 requires the two paths to agree on. signal_id /
# created_at_utc / status are persistence-only metadata and never compared here.
CANDIDATE_FIELDS = (
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
    "signal_type",
    "trigger_type",
    "alert_source",
    "strategy_profile",
)


def make_bias(market_bias="Bull", confidence=0.8):
    return {
        "market_bias": market_bias,
        "confidence": confidence,
        "scores": {},
        "reasons": [],
        "warnings": [],
        "note": "",
    }


def _ohlcv_frame(candles) -> pd.DataFrame:
    """A UTC-aware OHLCV DataFrame from candle_fixtures Candle objects."""
    return pd.DataFrame(
        [(c.open, c.high, c.low, c.close, c.volume) for c in candles],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.to_datetime([c.timestamp for c in candles], utc=True),
    )


# ---------------------------------------------------------------------------
# Real Live path: build_indicator_snapshot() -> generate_signal_candidates()
# ---------------------------------------------------------------------------
def live_snapshot(frame, *, symbol, evaluation_time, timeframe=TIMEFRAME):
    target = ChartTarget(symbol=symbol, ticker="X", timeframe=timeframe, chart_symbol=symbol)
    return cs.build_indicator_snapshot(target, frame, now_utc=evaluation_time)


def live_candidates(frame, *, symbol, bias_result, evaluation_time, threshold=DEFAULT_THRESHOLD, timeframe=TIMEFRAME):
    snapshot = live_snapshot(frame, symbol=symbol, evaluation_time=evaluation_time, timeframe=timeframe)
    context = create_signal_context(bias_result, [snapshot], confidence_threshold=threshold)
    return sg.generate_signal_candidates(context), snapshot


# ---------------------------------------------------------------------------
# Real Backtest path: evaluate_backtest_bars() -> generate_signal_candidates()
# ---------------------------------------------------------------------------
def backtest_bar(frame, *, symbol, bias_result, bar_timestamp, threshold=DEFAULT_THRESHOLD,
                 timeframe=TIMEFRAME, evaluation_time=None):
    """Evaluate exactly one confirmed bar (validation window == [bar, bar])."""
    close_frame = frame[["close"]] if "close" in frame else frame
    evaluations = warmup.evaluate_backtest_bars(
        close_frame,
        bar_timestamp,
        bar_timestamp,
        symbol=symbol,
        timeframe=timeframe,
        bias_result=bias_result,
        confidence_threshold=threshold,
        evaluation_time=evaluation_time,
    )
    last = evaluations[-1]
    return list(last.candidates), last


def backtest_range(frame, *, symbol, bias_result, from_ts, to_ts, threshold=DEFAULT_THRESHOLD,
                   timeframe=TIMEFRAME, evaluation_time=None):
    """Evaluate a whole validation window [from_ts, to_ts] and return its BarEvaluations.

    Used for the forming-candle tests: the window deliberately contains BOTH a
    confirmed and a still-forming bar so we can prove Backtest itself drops the
    forming one (no BarEvaluation) under a fixed evaluation_time.
    """
    close_frame = frame[["close"]] if "close" in frame else frame
    return warmup.evaluate_backtest_bars(
        close_frame,
        from_ts,
        to_ts,
        symbol=symbol,
        timeframe=timeframe,
        bias_result=bias_result,
        confidence_threshold=threshold,
        evaluation_time=evaluation_time,
    )


def assert_candidates_equal(live_candidate, backtest_candidate):
    for field in CANDIDATE_FIELDS:
        assert live_candidate[field] == backtest_candidate[field], (
            f"{field}: live={live_candidate[field]!r} != backtest={backtest_candidate[field]!r}"
        )
    assert live_candidate == backtest_candidate


# ===========================================================================
# 1. Same candle timestamp / timezone, and provenance of the candidate
# ===========================================================================


def test_same_candle_and_evaluation_timestamp_produce_identical_candidate():
    # ONE candle set, ONE candle timestamp, ONE UTC-aware evaluation timestamp
    # fed to both paths -- the previous version used different fixtures per path.
    index, candles = make_uptrend_candles(91)
    frame = _ohlcv_frame(candles)
    bar_ts = index[90]
    eval_ts = bar_ts + HOUR  # this bar's close time (UTC-aware)
    bias = make_bias("Bull", 0.72)

    live, snap = live_candidates(frame, symbol="USDJPY", bias_result=bias, evaluation_time=eval_ts)
    backtest, bar_eval = backtest_bar(frame, symbol="USDJPY", bias_result=bias, bar_timestamp=bar_ts)

    assert len(live) == 1 and len(backtest) == 1
    assert_candidates_equal(live[0], backtest[0])
    # Provenance: both candidates were generated from the SAME confirmed candle.
    assert snap.timestamp == bar_ts.isoformat()
    assert bar_eval.timestamp == bar_ts.isoformat()
    assert snap.timestamp == bar_eval.timestamp


def test_naive_and_utc_aware_evaluation_select_the_same_bar_and_candidate():
    # A naive vs UTC-aware evaluation timestamp for the same wall-clock instant
    # must never select a different candle (Issue #28 review point 1).
    index, candles = make_uptrend_candles(91)
    frame = _ohlcv_frame(candles)
    bar_ts = index[90]
    aware_eval = bar_ts + HOUR
    naive_eval = aware_eval.tz_convert(None)  # same instant, tz-naive
    bias = make_bias("Bull", 0.8)

    aware, aware_snap = live_candidates(frame, symbol="USDJPY", bias_result=bias, evaluation_time=aware_eval)
    naive, naive_snap = live_candidates(frame, symbol="USDJPY", bias_result=bias, evaluation_time=naive_eval)

    assert aware_snap.timestamp == naive_snap.timestamp == bar_ts.isoformat()
    assert aware == naive

    # And the Backtest split accepts a naive validation bound against the
    # tz-aware candle index without shifting which bar is chosen.
    backtest, bar_eval = backtest_bar(
        frame, symbol="USDJPY", bias_result=bias, bar_timestamp=bar_ts.tz_convert(None)
    )
    assert bar_eval.timestamp == bar_ts.isoformat()
    assert_candidates_equal(aware[0], backtest[0])


# ===========================================================================
# 2. freshness / bias_weight come from the one shared rule (not hardcoded)
# ===========================================================================


def test_shared_freshness_rule_is_symmetric_for_same_candle_and_evaluation_time():
    # The property both paths rely on: identical (candle_timestamp,
    # evaluation_time) -> identical (freshness, age, bias_weight).
    index, candles = make_uptrend_candles(91)
    frame = _ohlcv_frame(candles)
    bar_ts = index[90]
    eval_ts = bar_ts + HOUR

    freshness, _age, bias_weight = resolve_freshness(bar_ts, evaluation_time=eval_ts)

    live, snap = live_candidates(frame, symbol="USDJPY", bias_result=make_bias(), evaluation_time=eval_ts)
    backtest, _ = backtest_bar(frame, symbol="USDJPY", bias_result=make_bias(), bar_timestamp=bar_ts)

    # Live snapshot, Backtest candidate, and the shared rule all agree.
    assert snap.freshness == freshness
    assert snap.bias_weight == bias_weight
    assert live[0]["freshness"] == freshness == backtest[0]["freshness"]


def test_backtest_freshness_is_derived_not_a_fixed_fresh():
    # Proof the hardcoded "Fresh"/1.0 is gone: observing the SAME bar as of a
    # much later weekday time flips Backtest's own snapshot to Stale/0.0
    # through its real _build_snapshot path -- impossible if it were hardcoded.
    index, candles = make_uptrend_candles(91)
    frame = _ohlcv_frame(candles)
    # A JST-weekday bar so a +25h observation is genuinely "Stale" (> 24h old)
    # rather than "Weekend Closed" -- both are non-Fresh, but Stale is the
    # unambiguous "derived, not hardcoded" proof.
    bar_ts = index[80]
    stale_eval = bar_ts + pd.Timedelta(hours=25)

    fresh_freshness, _a, fresh_weight = resolve_freshness(bar_ts, evaluation_time=bar_ts + HOUR)
    stale_freshness, _b, stale_weight = resolve_freshness(bar_ts, evaluation_time=stale_eval)

    assert (fresh_freshness, fresh_weight) == ("Fresh", 1.0)
    assert (stale_freshness, stale_weight) == ("Stale", 0.0)

    row = warmup.calculate_indicators(frame[["close"]]).loc[bar_ts]
    snap = warmup._build_snapshot(
        "USDJPY", TIMEFRAME, row, candle_timestamp=bar_ts, evaluation_time=stale_eval,
    )
    assert snap["freshness"] == "Stale"
    assert snap["bias_weight"] == 0.0


# ===========================================================================
# 3. Confirmed-candle selection: forming candle excluded; timeframe boundary
# ===========================================================================


def test_forming_candle_is_excluded_by_backtest_itself_under_a_fixed_evaluation_time():
    # Review Major (残存): the Backtest validation window contains BOTH a
    # confirmed bar and a still-forming bar, and a single fixed evaluation_time
    # is handed to both paths. Backtest must drop the forming bar ITSELF (no
    # BarEvaluation, no Candidate, no future-candle read) -- not rely on the
    # caller pre-trimming the window.
    #
    # Mirrors the reproduction: 1h bars, 18:00 confirmed (closes 19:00), 19:00
    # forming (closes 20:00), evaluation_time 19:30.
    index, candles = make_uptrend_candles(92)
    frame = _ohlcv_frame(candles)
    confirmed = index[90]   # "18:00" -- closes at confirmed + 1h ("19:00")
    forming = index[91]     # "19:00" -- closes at forming + 1h ("20:00")
    eval_1930 = confirmed + HOUR + pd.Timedelta(minutes=30)  # "19:30"
    assert confirmed + HOUR <= eval_1930 < forming + HOUR    # confirmed closed, forming not yet
    bias = make_bias("Bull", 0.8)

    # Live at 19:30: uses the confirmed bar only, never the forming one.
    live, snap = live_candidates(frame, symbol="USDJPY", bias_result=bias, evaluation_time=eval_1930)
    assert snap.timestamp == confirmed.isoformat()
    assert snap.timestamp != forming.isoformat()

    # Backtest at 19:30 over [confirmed, forming]: the forming bar produces NO
    # BarEvaluation at all, and only the confirmed bar yields a candidate.
    evaluations = backtest_range(
        frame, symbol="USDJPY", bias_result=bias,
        from_ts=confirmed, to_ts=forming, evaluation_time=eval_1930,
    )
    evaluated_timestamps = [e.timestamp for e in evaluations]
    assert evaluated_timestamps == [confirmed.isoformat()]           # only the confirmed bar
    assert forming.isoformat() not in evaluated_timestamps           # forming bar excluded entirely
    assert all(forming.isoformat() != e.timestamp for e in evaluations)
    backtest_candidates = [c for e in evaluations for c in e.candidates]
    assert len(backtest_candidates) == 1                             # no forming-bar candidate

    # Same confirmed candle -> identical candidate fields on both paths.
    assert len(live) == 1
    assert_candidates_equal(live[0], backtest_candidates[0])


@pytest.mark.parametrize("tz_aware", [True, False])
def test_timeframe_boundary_one_second_before_at_and_after_close(tz_aware):
    # Review boundary matrix for a 1h bar that closes at "11:00":
    #   close - 1s (10:59:59) -> still forming -> excluded
    #   close      (11:00:00) -> confirmed     -> evaluated
    #   close + 1s (11:00:01) -> confirmed     -> evaluated
    # Identical outcome whether evaluation_time is tz-aware or naive.
    index, candles = make_uptrend_candles(92)
    frame = _ohlcv_frame(candles)
    the_bar = index[91]              # the "10:00" candle
    close_time = the_bar + HOUR      # "11:00"
    bias = make_bias("Bull", 0.8)

    def eval_at(delta_seconds):
        instant = close_time + pd.Timedelta(seconds=delta_seconds)
        return instant.tz_convert(None) if not tz_aware else instant

    cases = {
        "before": (eval_at(-1), 0),  # forming -> no BarEvaluation
        "at": (eval_at(0), 1),       # confirmed -> evaluated
        "after": (eval_at(+1), 1),   # confirmed -> evaluated
    }
    for label, (evaluation_time, expected_barevals) in cases.items():
        evaluations = backtest_range(
            frame, symbol="USDJPY", bias_result=bias,
            from_ts=the_bar, to_ts=the_bar, evaluation_time=evaluation_time,
        )
        assert len(evaluations) == expected_barevals, f"backtest[{label}]"

        # Live confirmed-candle selection must agree at each boundary point.
        snap = live_snapshot(frame, symbol="USDJPY", evaluation_time=evaluation_time)
        if expected_barevals == 0:
            # forming: Live falls back to the prior confirmed bar (index[90]).
            assert snap.timestamp == index[90].isoformat(), f"live[{label}]"
        else:
            assert snap.timestamp == the_bar.isoformat(), f"live[{label}]"


# ===========================================================================
# 4. Real entrypoints: run_once() vs run_backtest()
# ===========================================================================


def _live_fields_from_paper_signal(signal):
    return {
        "symbol": signal["symbol"].replace("_", ""),
        "timeframe": signal["timeframe"],
        "side": signal["side"],
        "market_bias": signal["market_bias"],
        "confidence": signal["confidence"],
        "signal_type": signal["signal_type"],
        "trigger_type": signal["trigger_type"],
        "alert_source": signal["alert_source"],
        "strategy_profile": signal["strategy_profile"],
        "reason": signal["reason"],
    }


def _backtest_fields_from_signal_row(row):
    reasons = json.loads(row["reason_json"])
    return {
        # side<->direction, market_bias<->bias, reason<->reasons[0] normalized.
        "symbol": row["symbol"].replace("_", ""),
        "timeframe": row["timeframe"],
        "side": row["direction"],
        "market_bias": row["bias"],
        "confidence": row["confidence"],
        "signal_type": row["signal_type"],
        "trigger_type": row["trigger_type"],
        "alert_source": row["alert_source"],
        "strategy_profile": row["strategy_profile"],
        "reason": reasons[0] if reasons else None,
    }


def test_run_once_and_run_backtest_emit_the_same_candidate_fields(tmp_path, monkeypatch):
    # ONE candle set + ONE bias + ONE config drive BOTH real entrypoints.
    symbol = "USDJPY"
    index, candles = make_uptrend_candles(91)
    frame = _ohlcv_frame(candles)
    bar_ts = index[90]
    eval_ts = bar_ts + HOUR
    bias = make_bias("Bull", 0.8)

    # --- Live run_once(): mock the fetchers + all side effects, dry-run so no
    # DB write / duplicate check / Discord / HTTP happens. signal_id/created_at
    # are auto-generated but excluded from the comparison below.
    live_snap = live_snapshot(frame, symbol=symbol, evaluation_time=eval_ts)
    monkeypatch.setattr(
        sg, "fetch_snapshot_sources",
        lambda: {"market": {}, "fred": None, "tradingview": {}, "chart": [live_snap]},
    )
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: bias)

    class _FailingSession:
        def post(self, *args, **kwargs):
            raise AssertionError("run_once() must not make an HTTP call in this parity test")

    monkeypatch.setattr(sg.requests, "post", _FailingSession().post)
    created = sg.run_once(dry_run=True, db_path=tmp_path / "unused_paper_signals.sqlite3")
    assert len(created) == 1
    assert not (tmp_path / "unused_paper_signals.sqlite3").exists()  # no side effect

    # --- Backtest run_backtest(): same candles in the Chart DB, matching bias
    # in the Market Bias DB, spread/slippage 0, results persisted then re-read.
    chart_db = tmp_path / "chart.sqlite3"
    bias_db = tmp_path / "bias.sqlite3"
    backtest_db = tmp_path / "backtest.sqlite3"
    collector.save_candles(chart_db, candles)
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[0]), confidence=0.8)
    args = runner.parse_args([
        "run", "--symbol", symbol, "--timeframe", TIMEFRAME,
        "--from", bar_ts.isoformat(), "--to", bar_ts.isoformat(),
        "--db-path", str(chart_db),
        "--backtest-db-path", str(backtest_db),
        "--market-bias-db-path", str(bias_db),
        "--spread", "0.0", "--slippage", "0.0",
    ])
    exit_code, summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK
    assert summary["signals_generated"] == 1
    rows = bt_store.list_backtest_signals(summary["run_id"], db_path=backtest_db)
    assert len(rows) == 1

    # Same candidate-equivalent fields via both real entrypoints (post-run,
    # post-persistence for the Backtest side). ID/time/entry/exit/run_id excluded.
    assert _live_fields_from_paper_signal(created[0]) == _backtest_fields_from_signal_row(rows[0])


# ===========================================================================
# 5. Stale through the real Backtest snapshot path (bias_weight <= 0 too)
# ===========================================================================


def test_stale_candle_yields_no_candidate_through_both_real_paths():
    # Set candle time + evaluation time so the bar is genuinely Stale (a JST
    # weekday, > 24h old). Both real snapshot paths must produce no candidate.
    index, candles = make_uptrend_candles(81)
    frame = _ohlcv_frame(candles)
    bar_ts = index[80]
    stale_eval = bar_ts + pd.Timedelta(hours=25)  # weekday, > STALE_AFTER
    bias = make_bias("Bull", 0.8)

    # Confirm this really is Stale / zero-weight via the shared rule.
    freshness, _age, bias_weight = resolve_freshness(bar_ts, evaluation_time=stale_eval)
    assert freshness == "Stale"
    assert bias_weight == 0.0  # bias_weight <= 0 arises from the shared freshness rule

    # Live real path: build_indicator_snapshot() marks it Stale -> no candidate.
    live, snap = live_candidates(frame, symbol="USDJPY", bias_result=bias, evaluation_time=stale_eval)
    assert snap.freshness == "Stale"
    assert snap.bias_weight == 0.0
    assert live == []

    # Backtest real path: evaluate_backtest_bars() builds the snapshot from the
    # historical bar (NOT a fixture with freshness="Fresh" written in) and, as
    # of the same Stale evaluation time, also produces no candidate.
    backtest, bar_eval = backtest_bar(
        frame, symbol="USDJPY", bias_result=bias, bar_timestamp=bar_ts, evaluation_time=stale_eval
    )
    assert bar_eval.is_ready is True  # indicator-ready...
    assert backtest == []             # ...but Stale, so no candidate


# ===========================================================================
# Core input-parity matrix (Bull / Bear / threshold / no-signal), trimmed.
# Kept because it still exercises value parity across both real builders on
# multiple symbols and directions -- not for line count.
# ===========================================================================


@pytest.mark.parametrize(
    "symbol, market_bias, uptrend, expected_side",
    [
        ("USDJPY", "Bull", True, "long"),
        ("EURUSD", "Bear", False, "short"),
        ("XAUUSD", "Bull", True, "long"),
    ],
)
def test_bull_and_bear_stay_in_lockstep(symbol, market_bias, uptrend, expected_side):
    step = 0.1 if uptrend else -0.1
    index, candles = make_uptrend_candles(91, chart_symbol=symbol, step=step)
    frame = _ohlcv_frame(candles)
    bar_ts = index[90]
    bias = make_bias(market_bias, 0.72)

    live, _ = live_candidates(frame, symbol=symbol, bias_result=bias, evaluation_time=bar_ts + HOUR)
    backtest, _ = backtest_bar(frame, symbol=symbol, bias_result=bias, bar_timestamp=bar_ts)

    assert len(live) == 1 and len(backtest) == 1
    assert_candidates_equal(live[0], backtest[0])
    assert live[0]["side"] == expected_side
    assert live[0]["symbol"] == symbol


def test_confidence_threshold_boundary_matches_on_both_paths():
    index, candles = make_uptrend_candles(91)
    frame = _ohlcv_frame(candles)
    bar_ts = index[90]

    at = make_bias("Bull", 0.40)
    live_at, _ = live_candidates(frame, symbol="USDJPY", bias_result=at, evaluation_time=bar_ts + HOUR, threshold=0.40)
    bt_at, _ = backtest_bar(frame, symbol="USDJPY", bias_result=at, bar_timestamp=bar_ts, threshold=0.40)
    assert len(live_at) == 1 and len(bt_at) == 1
    assert_candidates_equal(live_at[0], bt_at[0])

    below = make_bias("Bull", 0.39)
    live_below, _ = live_candidates(frame, symbol="USDJPY", bias_result=below, evaluation_time=bar_ts + HOUR, threshold=0.40)
    bt_below, _ = backtest_bar(frame, symbol="USDJPY", bias_result=below, bar_timestamp=bar_ts, threshold=0.40)
    assert live_below == [] and bt_below == []


def test_neutral_bias_yields_no_candidate_on_either_path():
    index, candles = make_uptrend_candles(91)
    frame = _ohlcv_frame(candles)
    bar_ts = index[90]
    bias = make_bias("Neutral", 0.9)

    live, _ = live_candidates(frame, symbol="USDJPY", bias_result=bias, evaluation_time=bar_ts + HOUR)
    backtest, _ = backtest_bar(frame, symbol="USDJPY", bias_result=bias, bar_timestamp=bar_ts)

    assert live == [] and backtest == []


# ===========================================================================
# ID / time handling: signal_id + created_at_utc are persistence-only metadata
# ===========================================================================


def test_paper_signals_match_when_id_and_timestamp_are_injected_fixed():
    index, candles = make_uptrend_candles(91)
    frame = _ohlcv_frame(candles)
    bar_ts = index[90]
    bias = make_bias("Bull", 0.8)

    live, _ = live_candidates(frame, symbol="USDJPY", bias_result=bias, evaluation_time=bar_ts + HOUR)
    backtest, _ = backtest_bar(frame, symbol="USDJPY", bias_result=bias, bar_timestamp=bar_ts)

    for candidate in (live[0], backtest[0]):
        assert "signal_id" not in candidate and "created_at_utc" not in candidate

    fixed = dict(signal_id="fixed-id", created_at_utc="2026-07-11T00:00:00Z")
    assert create_paper_signal(live[0], **fixed) == create_paper_signal(backtest[0], **fixed)
