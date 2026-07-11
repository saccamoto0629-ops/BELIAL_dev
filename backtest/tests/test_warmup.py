import pathlib
import sys

import pandas as pd
import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import warmup  # noqa: E402


def make_candles(n, *, start="2026-06-01T00:00:00Z", freq="1h", slope=0.1):
    # Issue #28: Monday 00:00 UTC start so validation bars, now evaluated at
    # their close time through the shared freshness rule, stay on JST weekdays
    # (Fresh) instead of landing on the weekend (Weekend Closed).
    closes = [100.0 + i * slope for i in range(n)]
    index = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"close": closes}, index=index)


def make_bias(market_bias="Bull", confidence=0.8):
    return {"market_bias": market_bias, "confidence": confidence, "scores": {}, "reasons": [], "warnings": [], "note": ""}


def make_row(**overrides):
    row = {
        "close": 150.0,
        "rsi14": 55.0,
        "macd": 0.1,
        "macd_signal": 0.05,
        "macd_hist": 0.05,
        "macd_state": "Bullish",
        "bb_upper": 152.0,
        "bb_middle": 150.0,
        "bb_lower": 148.0,
        "bb_position": 0.5,
        "sma20": 149.5,
        "sma75": 148.0,
        "ema20": 149.8,
    }
    row.update(overrides)
    return pd.Series(row)


# =============================================================================
# check_indicator_readiness (unit-level, no candle fixtures needed)
# =============================================================================


def test_readiness_all_ready_when_every_field_present_and_past_macd_bar_count():
    result = warmup.check_indicator_readiness(make_row(), position=100)

    assert result.is_ready is True
    assert result.reasons == ()


def test_readiness_rsi_not_ready_when_nan():
    result = warmup.check_indicator_readiness(make_row(rsi14=float("nan")), position=100)

    assert result.is_ready is False
    assert warmup.REASON_RSI_NOT_READY in result.reasons


def test_readiness_macd_not_ready_below_min_bars_even_with_a_numeric_value():
    # MACD is never NaN via pandas ewm(); readiness below MACD_MIN_BARS must
    # still be rejected purely on bar count.
    result = warmup.check_indicator_readiness(make_row(macd=0.1, macd_signal=0.05), position=10)

    assert result.is_ready is False
    assert warmup.REASON_MACD_NOT_READY in result.reasons


def test_readiness_macd_ready_at_exactly_min_bars():
    result = warmup.check_indicator_readiness(make_row(), position=warmup.MACD_MIN_BARS)

    assert warmup.REASON_MACD_NOT_READY not in result.reasons


def test_readiness_bollinger_not_ready_when_any_band_is_nan():
    result = warmup.check_indicator_readiness(make_row(bb_upper=float("nan")), position=100)

    assert result.is_ready is False
    assert warmup.REASON_BOLLINGER_NOT_READY in result.reasons


def test_readiness_ma_not_ready_when_sma75_is_nan():
    result = warmup.check_indicator_readiness(make_row(sma75=float("nan")), position=100)

    assert result.is_ready is False
    assert warmup.REASON_MA_NOT_READY in result.reasons


def test_readiness_ma_not_ready_when_ema20_is_nan():
    result = warmup.check_indicator_readiness(make_row(ema20=float("nan")), position=100)

    assert result.is_ready is False
    assert warmup.REASON_MA_NOT_READY in result.reasons


def test_readiness_reports_multiple_reasons_at_once():
    result = warmup.check_indicator_readiness(
        make_row(rsi14=float("nan"), sma20=float("nan")), position=10
    )

    assert warmup.REASON_RSI_NOT_READY in result.reasons
    assert warmup.REASON_MACD_NOT_READY in result.reasons  # position=10 < MACD_MIN_BARS
    assert warmup.REASON_MA_NOT_READY in result.reasons


# =============================================================================
# split_warmup_and_validation
# =============================================================================


def test_split_warmup_and_validation_splits_at_the_given_start():
    candles = make_candles(10)
    validation_start = candles.index[6]

    warmup_frame, validation_frame = warmup.split_warmup_and_validation(candles, validation_start)

    assert len(warmup_frame) == 6
    assert len(validation_frame) == 4
    assert warmup_frame.index[-1] < validation_start
    assert validation_frame.index[0] == validation_start


def test_split_warmup_and_validation_respects_validation_end():
    candles = make_candles(10)
    validation_start = candles.index[3]
    validation_end = candles.index[6]

    _, validation_frame = warmup.split_warmup_and_validation(candles, validation_start, validation_end)

    assert len(validation_frame) == 4
    assert validation_frame.index[-1] == validation_end


def test_split_warmup_and_validation_excludes_future_rows_from_both_frames():
    # Regression: rows after validation_end must never land in warmup_frame
    # (nor validation_frame) -- see the "future rows leak into warm-up"
    # CodeX review finding.
    candles = make_candles(20)
    validation_start = candles.index[5]
    validation_end = candles.index[8]

    warmup_frame, validation_frame = warmup.split_warmup_and_validation(
        candles, validation_start, validation_end
    )

    future_rows = candles.index[9:]  # strictly after validation_end
    assert len(future_rows) > 0
    assert warmup_frame.index.max() < validation_start
    assert validation_frame.index.min() >= validation_start
    assert validation_frame.index.max() <= validation_end
    assert not any(ts in warmup_frame.index for ts in future_rows)
    assert not any(ts in validation_frame.index for ts in future_rows)
    assert len(warmup_frame) == 5  # only the 5 rows strictly before validation_start
    assert len(validation_frame) == 4  # validation_start..validation_end inclusive


def test_split_warmup_and_validation_future_rows_do_not_inflate_warmup_frame_size():
    # Regression: with the old `~mask` implementation, a large number of
    # rows *after* validation_end would count toward warmup_frame's length,
    # potentially masking an actually-insufficient pre-start warm-up.
    tiny_warmup = 5
    validation_len = 3
    lots_of_future_rows = 100
    candles = make_candles(tiny_warmup + validation_len + lots_of_future_rows)
    validation_start = candles.index[tiny_warmup]
    validation_end = candles.index[tiny_warmup + validation_len - 1]

    warmup_frame, validation_frame = warmup.split_warmup_and_validation(
        candles, validation_start, validation_end
    )

    assert len(warmup_frame) == tiny_warmup  # not tiny_warmup + lots_of_future_rows
    assert len(validation_frame) == validation_len


def test_evaluate_backtest_bars_future_rows_do_not_hide_warmup_insufficient():
    # End-to-end regression: abundant data *after* validation_end must not
    # mask an insufficient pre-start warm-up allocation.
    tiny_warmup = 5  # well below DEFAULT_WARMUP_BARS (75)
    validation_len = 3
    lots_of_future_rows = 100
    candles = make_candles(tiny_warmup + validation_len + lots_of_future_rows)
    validation_start = candles.index[tiny_warmup]
    validation_end = candles.index[tiny_warmup + validation_len - 1]

    results = warmup.evaluate_backtest_bars(
        candles, validation_start, validation_end,
        symbol="USDJPY", timeframe="1h", bias_result=make_bias(),
    )

    assert len(results) == validation_len  # future rows never become results either
    assert all(not r.is_ready for r in results)
    assert all(warmup.REASON_WARMUP_INSUFFICIENT in r.skip_reasons for r in results)


def test_split_warmup_and_validation_accepts_naive_timestamp_against_tz_aware_index():
    candles = make_candles(10)
    naive_start = candles.index[5].tz_localize(None)

    warmup_frame, validation_frame = warmup.split_warmup_and_validation(candles, naive_start)

    assert len(warmup_frame) == 5
    assert len(validation_frame) == 5


# =============================================================================
# evaluate_backtest_bars: warm-up insufficiency
# =============================================================================


def test_warmup_insufficient_skips_signal_generation_for_every_validation_bar():
    candles = make_candles(20)  # far fewer than DEFAULT_WARMUP_BARS (75)
    validation_start = candles.index[15]

    results = warmup.evaluate_backtest_bars(
        candles, validation_start, symbol="USDJPY", timeframe="1h", bias_result=make_bias(),
    )

    assert len(results) == 5  # only the validation-period bars
    assert all(not r.is_ready for r in results)
    assert all(r.candidates == () for r in results)
    assert all(warmup.REASON_WARMUP_INSUFFICIENT in r.skip_reasons for r in results)


def test_warmup_insufficient_flag_persists_even_after_indicators_naturally_warm_up():
    # Only 10 bars of warm-up (well below the 75-bar MA requirement),
    # followed by 80 validation bars. By the later validation bars, total
    # accumulated bars exceed every indicator's minimum -- but
    # warmup_insufficient must still be flagged, since the warm-up
    # *allocation* itself never had enough bars.
    candles = make_candles(90)
    validation_start = candles.index[10]

    results = warmup.evaluate_backtest_bars(
        candles, validation_start, symbol="USDJPY", timeframe="1h", bias_result=make_bias(),
    )

    last = results[-1]
    assert warmup.REASON_WARMUP_INSUFFICIENT in last.skip_reasons
    assert last.is_ready is False
    # but the per-indicator NaN reasons should have cleared by the end,
    # since 90 total bars comfortably exceeds every indicator's minimum
    for reason in (warmup.REASON_RSI_NOT_READY, warmup.REASON_MA_NOT_READY, warmup.REASON_BOLLINGER_NOT_READY):
        assert reason not in last.skip_reasons


# =============================================================================
# evaluate_backtest_bars: sufficient warm-up
# =============================================================================


def test_sufficient_warmup_only_validation_bars_appear_in_results():
    candles = make_candles(85)  # 80 warm-up + 5 validation, warm-up >= 75
    validation_start = candles.index[80]

    results = warmup.evaluate_backtest_bars(
        candles, validation_start, symbol="USDJPY", timeframe="1h", bias_result=make_bias(),
    )

    assert len(results) == 5  # not 85 -- warm-up bars never appear
    assert all(r.is_ready for r in results)
    assert all(not r.skip_reasons for r in results)


def test_warmup_bars_never_appear_in_results_timestamps():
    candles = make_candles(85)
    validation_start = candles.index[80]

    results = warmup.evaluate_backtest_bars(
        candles, validation_start, symbol="USDJPY", timeframe="1h", bias_result=make_bias(),
    )

    validation_start_iso = validation_start.isoformat()
    for result in results:
        assert result.timestamp >= validation_start_iso


def test_sufficient_warmup_generates_candidates_matching_build_candidates_directly():
    # Prove the pre-filter wiring calls paper-signal's existing,
    # unmodified rule set rather than reimplementing it: the candidate
    # produced for a ready bar must equal what build_candidates() itself
    # produces for the same (independently reconstructed) snapshot.
    import signal_generator as sg
    from indicators import calculate_indicators

    candles = make_candles(85)
    validation_start = candles.index[84]  # exactly one validation bar
    bias_result = make_bias(market_bias="Bull", confidence=0.8)

    results = warmup.evaluate_backtest_bars(
        candles, validation_start, symbol="USDJPY", timeframe="1h", bias_result=bias_result,
    )

    assert len(results) == 1
    assert results[0].is_ready is True
    assert len(results[0].candidates) == 1
    candidate = results[0].candidates[0]

    # Independently rebuild the same bar's snapshot (not via warmup's
    # private helper) and call build_candidates() directly on it.
    last_row = calculate_indicators(candles).iloc[-1]
    snapshot = {
        "symbol": "USDJPY",
        "timeframe": "1h",
        "trend": "Bull",  # steady uptrend: close > sma20 > sma75
        "state": "Near upper band",  # steady uptrend keeps bb_position just under breakout
        "freshness": "Fresh",
        "bias_weight": 1.0,
        "macd_state": str(last_row["macd_state"]),
    }
    expected = sg.build_candidates(bias_result, [snapshot])

    assert len(expected) == 1
    assert candidate == expected[0]
    assert candidate["side"] == "long"  # steady uptrend -> Bull/Bullish MACD -> long
    assert candidate["symbol"] == "USDJPY"


def test_neutral_bias_produces_no_candidates_even_when_fully_ready():
    candles = make_candles(85)
    validation_start = candles.index[80]

    results = warmup.evaluate_backtest_bars(
        candles, validation_start, symbol="USDJPY", timeframe="1h",
        bias_result=make_bias(market_bias="Neutral"),
    )

    assert all(r.is_ready for r in results)  # readiness is independent of bias
    assert all(r.candidates == () for r in results)  # but Neutral bias yields no candidates


# =============================================================================
# Issue #39: per-bar Market State classification (point-in-time)
# =============================================================================

from market_state import KNOWN_MARKET_STATES  # noqa: E402  (market-watch; on sys.path via `import warmup`)


def _ready_states(candles, validation_start, validation_end=None):
    """Map each ready bar's timestamp -> (market_state, confidence)."""
    results = warmup.evaluate_backtest_bars(
        candles, validation_start, validation_end,
        symbol="USDJPY", timeframe="1h", bias_result=make_bias(),
    )
    return {r.timestamp: (r.market_state, r.market_state_confidence) for r in results if r.is_ready}


def test_ready_bars_carry_a_market_state_and_skipped_bars_do_not():
    candles = make_candles(85)  # 80 warm-up + 5 validation, every validation bar ready
    validation_start = candles.index[80]

    results = warmup.evaluate_backtest_bars(
        candles, validation_start, symbol="USDJPY", timeframe="1h", bias_result=make_bias(),
    )

    assert all(r.is_ready for r in results)
    for r in results:
        assert r.market_state in KNOWN_MARKET_STATES
        assert 0.0 <= r.market_state_confidence <= 1.0


def test_skipped_bars_leave_market_state_none():
    candles = make_candles(20)  # far fewer than DEFAULT_WARMUP_BARS -> every bar skipped
    validation_start = candles.index[15]

    results = warmup.evaluate_backtest_bars(
        candles, validation_start, symbol="USDJPY", timeframe="1h", bias_result=make_bias(),
    )

    assert all(not r.is_ready for r in results)
    assert all(r.market_state is None for r in results)
    assert all(r.market_state_confidence is None for r in results)


def test_market_state_never_depends_on_a_future_bar():
    # A past bar's state is classified from indicators up to and including
    # itself only. Mutating any bar *after* the evaluated window must not change
    # a single already-classified state -- the slice excludes future bars and the
    # indicators at each row are causal.
    base = make_candles(120, slope=0.1)
    start, end = base.index[110], base.index[116]  # bars 117..119 are "future" here
    before = _ready_states(base, start, end)

    mutated = base.copy()
    mutated.iloc[119, mutated.columns.get_loc("close")] = 9_999.0  # a wild future outlier
    after = _ready_states(mutated, start, end)

    assert before == after  # no past state (or confidence) moved


def test_market_state_reflects_a_change_to_the_current_bar_itself():
    # Off-by-one guard: the classification slice is inclusive of the current bar,
    # so mutating the last in-window bar's own close must change ITS OWN state (or
    # at least its confidence). If the slice were exclusive of the current bar,
    # this mutation would be invisible and the tuple would be unchanged.
    base = make_candles(120, slope=0.1)
    start, end = base.index[110], base.index[116]
    target = base.index[116]  # the last evaluated bar
    before = _ready_states(base, start, end)

    mutated = base.copy()
    mutated.loc[target, "close"] = float(base.loc[target, "close"]) + 50.0
    after = _ready_states(mutated, start, end)

    # Only the mutated bar's own reading changes; earlier bars stay put.
    assert after[target.isoformat()] != before[target.isoformat()]
    for ts in before:
        if ts != target.isoformat():
            assert after[ts] == before[ts]


# =============================================================================
# existing test suites must still pass (see verification command in the report)
# =============================================================================
