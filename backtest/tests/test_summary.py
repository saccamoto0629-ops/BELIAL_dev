import json
import math
import pathlib
import sys

import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import summary  # noqa: E402
import report  # noqa: E402


def make_signal(**overrides):
    signal = {
        "id": "sig-1",
        "run_id": "run-1",
        "signal_time": "2026-01-01T00:00:00Z",
        "symbol": "USD_JPY",
        "timeframe": "1h",
        "bias": "Bull",
        "confidence": 0.7,
        "direction": "long",
        "signal_type": "paper_signal",
        "trigger_type": "bar_close",
        "entry_price": 150.0,
        "result_status": "win",
        "pnl": 1.0,
        "closed_at": "2026-01-01T02:00:00Z",
    }
    signal.update(overrides)
    return signal


# =============================================================================
# 1. normal case: wins, losses, and a zero-pnl signal together
# =============================================================================


def test_normal_case_wins_losses_and_zero_pnl():
    signals = [
        make_signal(id="s1", signal_time="2026-01-01T00:00:00Z", result_status="win", pnl=2.0),
        make_signal(id="s2", signal_time="2026-01-01T01:00:00Z", result_status="loss", pnl=-1.0),
        make_signal(id="s3", signal_time="2026-01-01T02:00:00Z", result_status="closed", pnl=0.0),
    ]

    result = summary.build_summary("run-1", signals)

    assert result["run_id"] == "run-1"
    assert result["total_signals"] == 3
    assert result["win_count"] == 1
    assert result["loss_count"] == 1  # pnl==0 counted in neither win nor loss
    assert result["win_rate"] == 0.5  # 1 / (1 + 1)
    assert result["total_pnl"] == 1.0  # 2.0 - 1.0 + 0.0
    assert result["average_pnl"] == 1.0 / 3
    assert result["profit_factor"] == 2.0  # gross_profit=2.0 / abs(gross_loss)=1.0
    assert result["max_drawdown"] == 1.0  # peak=2.0 after s1, drawdown to 1.0 after s2


# =============================================================================
# 2. zero signals
# =============================================================================


def test_zero_signals_matches_issue_23_example():
    result = summary.build_summary("run-empty", [])

    assert result["run_id"] == "run-empty"
    assert result["total_signals"] == 0
    assert result["win_count"] == 0
    assert result["loss_count"] == 0
    assert result["win_rate"] == 0.0
    assert result["total_pnl"] == 0.0
    assert result["average_pnl"] == 0.0
    assert result["profit_factor"] == 0.0
    assert result["max_drawdown"] == 0.0
    assert result["average_holding_bars"] is None


# =============================================================================
# 3. wins only
# =============================================================================


def test_wins_only():
    signals = [
        make_signal(id="s1", signal_time="2026-01-01T00:00:00Z", pnl=1.0),
        make_signal(id="s2", signal_time="2026-01-01T01:00:00Z", pnl=3.0),
    ]

    result = summary.build_summary("run-1", signals)

    assert result["win_count"] == 2
    assert result["loss_count"] == 0
    assert result["win_rate"] == 1.0
    assert result["total_pnl"] == 4.0
    assert result["profit_factor"] is None  # profit but zero loss: mathematically infinite
    assert result["max_drawdown"] == 0.0  # equity only ever rises


# =============================================================================
# 4. losses only
# =============================================================================


def test_losses_only():
    signals = [
        make_signal(id="s1", signal_time="2026-01-01T00:00:00Z", result_status="loss", pnl=-2.0),
        make_signal(id="s2", signal_time="2026-01-01T01:00:00Z", result_status="loss", pnl=-1.0),
    ]

    result = summary.build_summary("run-1", signals)

    assert result["win_count"] == 0
    assert result["loss_count"] == 2
    assert result["win_rate"] == 0.0
    assert result["total_pnl"] == -3.0
    assert result["profit_factor"] == 0.0  # no gross profit at all, so 0.0 (not None)
    assert result["max_drawdown"] == 3.0  # equity: 0 -> -2 -> -3, peak stays at 0


# =============================================================================
# 5. max_drawdown: a specific, hand-computed equity curve
# =============================================================================


def test_max_drawdown_hand_computed_equity_curve():
    # equity curve: 0 -> 5 -> 2 -> 6 -> 1
    # peaks:              5 -> 5 -> 6 -> 6
    # drawdowns:          0 -> 3 -> 0 -> 5
    signals = [
        make_signal(id="s1", signal_time="2026-01-01T00:00:00Z", pnl=5.0),
        make_signal(id="s2", signal_time="2026-01-01T01:00:00Z", pnl=-3.0),
        make_signal(id="s3", signal_time="2026-01-01T02:00:00Z", pnl=4.0),
        make_signal(id="s4", signal_time="2026-01-01T03:00:00Z", pnl=-5.0),
    ]

    result = summary.build_summary("run-1", signals)

    assert result["max_drawdown"] == 5.0


def test_max_drawdown_never_leaks_future_pnl_regardless_of_db_row_order():
    # Same 4 signals as above, but shuffled as if returned in an
    # arbitrary/incidental DB order -- build_summary() must sort by
    # signal_time itself before computing the equity curve.
    signals = [
        make_signal(id="s4", signal_time="2026-01-01T03:00:00Z", pnl=-5.0),
        make_signal(id="s1", signal_time="2026-01-01T00:00:00Z", pnl=5.0),
        make_signal(id="s3", signal_time="2026-01-01T02:00:00Z", pnl=4.0),
        make_signal(id="s2", signal_time="2026-01-01T01:00:00Z", pnl=-3.0),
    ]

    result = summary.build_summary("run-1", signals)

    assert result["max_drawdown"] == 5.0
    assert result["total_pnl"] == 1.0


# =============================================================================
# open/unresolved signals: never fabricated as 0-pnl trades
# =============================================================================


def test_open_signals_excluded_from_pnl_metrics_but_counted_in_total():
    signals = [
        make_signal(id="s1", signal_time="2026-01-01T00:00:00Z", result_status="win", pnl=2.0),
        make_signal(id="s2", signal_time="2026-01-01T01:00:00Z", result_status="open", pnl=None, closed_at=None),
    ]

    result = summary.build_summary("run-1", signals)

    assert result["total_signals"] == 2  # open signal still counted
    assert result["win_count"] == 1
    assert result["loss_count"] == 0
    assert result["total_pnl"] == 2.0  # open signal's pnl never treated as 0
    assert result["average_pnl"] == 2.0  # averaged over 1 resolved signal, not 2
    assert result["max_drawdown"] == 0.0


def test_all_open_signals_yields_zero_metrics_without_error():
    signals = [
        make_signal(id="s1", signal_time="2026-01-01T00:00:00Z", result_status="open", pnl=None, closed_at=None),
        make_signal(id="s2", signal_time="2026-01-01T01:00:00Z", result_status="open", pnl=None, closed_at=None),
    ]

    result = summary.build_summary("run-1", signals)

    assert result["total_signals"] == 2
    assert result["win_count"] == 0
    assert result["loss_count"] == 0
    assert result["win_rate"] == 0.0
    assert result["total_pnl"] == 0.0
    assert result["average_pnl"] == 0.0
    assert result["profit_factor"] == 0.0
    assert result["max_drawdown"] == 0.0


# =============================================================================
# profit_factor: zero-division policy
# =============================================================================


def test_profit_factor_is_none_for_profit_with_zero_loss():
    assert summary._profit_factor([1.0, 2.0]) is None


def test_profit_factor_is_zero_when_no_profit_and_no_loss():
    assert summary._profit_factor([]) == 0.0
    assert summary._profit_factor([0.0, 0.0]) == 0.0


def test_profit_factor_never_raises_zero_division_error():
    # Regression: gross_loss == 0 must be handled explicitly, never divide.
    try:
        summary._profit_factor([5.0])
    except ZeroDivisionError:
        raise AssertionError("profit_factor must not raise ZeroDivisionError")


# =============================================================================
# average_holding_bars: always None (no such column in backtest_signals)
# =============================================================================


def test_average_holding_bars_is_always_none():
    signals = [make_signal(id="s1")]
    result = summary.build_summary("run-1", signals)
    assert result["average_holding_bars"] is None


# =============================================================================
# total_pnl / gross sums are always float, never int (JSON type consistency)
# =============================================================================


def test_total_pnl_is_a_float_even_when_no_signals_resolved():
    signals = [make_signal(id="s1", result_status="open", pnl=None, closed_at=None)]
    result = summary.build_summary("run-1", signals)
    assert isinstance(result["total_pnl"], float)


def test_total_pnl_is_a_float_for_zero_signals():
    result = summary.build_summary("run-empty", [])
    assert isinstance(result["total_pnl"], float)


# =============================================================================
# build_summary(): run_id cross-contamination guard (CodeX review)
# =============================================================================


def test_build_summary_succeeds_when_every_signal_matches_run_id():
    signals = [
        make_signal(id="s1", run_id="run-1", signal_time="2026-01-01T00:00:00Z", pnl=1.0),
        make_signal(id="s2", run_id="run-1", signal_time="2026-01-01T01:00:00Z", pnl=-1.0),
    ]

    result = summary.build_summary("run-1", signals)

    assert result["total_signals"] == 2


def test_build_summary_raises_value_error_when_a_different_run_id_is_mixed_in():
    signals = [
        make_signal(id="s1", run_id="run-1", signal_time="2026-01-01T00:00:00Z", pnl=1.0),
        make_signal(id="s2", run_id="run-2", signal_time="2026-01-01T01:00:00Z", pnl=-1.0),
    ]

    with pytest.raises(ValueError) as exc_info:
        summary.build_summary("run-1", signals)

    message = str(exc_info.value)
    assert "run-1" in message  # expected run_id
    assert "run-2" in message  # the offending mismatched run_id


def test_build_summary_never_mixes_a_mismatched_signals_pnl_into_the_result():
    # Regression: even a single wrongly-included signal must not leak its
    # pnl into total_pnl/win_count/etc -- the whole call must fail loudly
    # rather than aggregate a partially-wrong result.
    signals = [
        make_signal(id="s1", run_id="run-1", signal_time="2026-01-01T00:00:00Z", pnl=1.0),
        make_signal(id="s2", run_id="run-2", signal_time="2026-01-01T01:00:00Z", pnl=999.0),
    ]

    with pytest.raises(ValueError):
        summary.build_summary("run-1", signals)

    # Sanity: build_summary() over only the legitimate signal is unaffected
    # by the rejected call above (no partial/cached state leaks between calls).
    clean_result = summary.build_summary("run-1", [signals[0]])
    assert clean_result["total_pnl"] == 1.0


def test_build_summary_skips_validation_for_signals_missing_a_run_id_key():
    # Internal/test fixtures that never included a run_id field at all must
    # keep working unchanged -- there is nothing to contradict.
    signals = [{"id": "s1", "signal_time": "2026-01-01T00:00:00Z", "pnl": 1.0}]

    result = summary.build_summary("run-1", signals)

    assert result["total_signals"] == 1
    assert result["total_pnl"] == 1.0


# =============================================================================
# breakdowns: by_bias / by_signal_type / by_timeframe
# =============================================================================


def test_breakdown_by_bias_groups_real_column_values():
    signals = [
        make_signal(id="s1", signal_time="2026-01-01T00:00:00Z", bias="Bull", pnl=1.0),
        make_signal(id="s2", signal_time="2026-01-01T01:00:00Z", bias="Bear", pnl=-1.0),
        make_signal(id="s3", signal_time="2026-01-01T02:00:00Z", bias=None, pnl=0.5),
    ]

    result = summary.build_summary("run-1", signals)

    by_bias = result["breakdowns"]["by_bias"]
    assert set(by_bias) == {"Bull", "Bear", summary.UNKNOWN_BREAKDOWN_KEY}
    assert by_bias["Bull"]["total_signals"] == 1
    assert by_bias["Bull"]["total_pnl"] == 1.0
    assert by_bias["Bear"]["total_signals"] == 1
    assert by_bias[summary.UNKNOWN_BREAKDOWN_KEY]["total_signals"] == 1


def test_breakdown_by_market_state_groups_real_column_values():
    # Issue #39: by_market_state groups on the recorded market_state column, and
    # a signal with no state (legacy/NULL row) falls into the unknown bucket --
    # never guessed.
    signals = [
        make_signal(id="s1", signal_time="2026-01-01T00:00:00Z", market_state="trend_up", pnl=2.0),
        make_signal(id="s2", signal_time="2026-01-01T01:00:00Z", market_state="trend_up", pnl=-1.0),
        make_signal(id="s3", signal_time="2026-01-01T02:00:00Z", market_state="range", pnl=0.5),
        make_signal(id="s4", signal_time="2026-01-01T03:00:00Z", market_state=None, pnl=0.3),
    ]

    result = summary.build_summary("run-1", signals)

    by_market_state = result["breakdowns"]["by_market_state"]
    assert set(by_market_state) == {"trend_up", "range", summary.UNKNOWN_BREAKDOWN_KEY}
    assert by_market_state["trend_up"]["total_signals"] == 2
    assert by_market_state["trend_up"]["win_count"] == 1
    assert by_market_state["trend_up"]["loss_count"] == 1
    assert by_market_state["trend_up"]["win_rate"] == pytest.approx(0.5)
    assert by_market_state["trend_up"]["profit_factor"] == pytest.approx(2.0)
    assert by_market_state["trend_up"]["total_pnl"] == pytest.approx(1.0)
    assert by_market_state["trend_up"]["average_pnl"] == pytest.approx(0.5)
    assert by_market_state["range"]["total_signals"] == 1
    assert by_market_state[summary.UNKNOWN_BREAKDOWN_KEY]["total_signals"] == 1


def test_market_state_metrics_render_in_text_json_and_markdown():
    signals = [
        make_signal(id="s1", signal_time="2026-01-01T00:00:00Z", market_state="trend_up", pnl=2.0),
        make_signal(id="s2", signal_time="2026-01-01T01:00:00Z", market_state="trend_up", pnl=-1.0),
        make_signal(id="s3", signal_time="2026-01-01T02:00:00Z", market_state=None, pnl=0.5),
        make_signal(id="s4", signal_time="2026-01-01T03:00:00Z", market_state="unknown", pnl=-0.25),
    ]
    result = summary.build_summary("run-1", signals)

    unknown = result["breakdowns"]["by_market_state"]["unknown"]
    assert unknown["total_signals"] == 2
    assert unknown["profit_factor"] == pytest.approx(2.0)

    text_output = report.render_text(result)
    assert "by_market_state:" in text_output
    assert "trend_up:" in text_output
    assert "profit_factor=2.0000" in text_output

    parsed = json.loads(report.render_json(result))
    assert parsed["breakdowns"]["by_market_state"]["trend_up"]["profit_factor"] == pytest.approx(2.0)

    markdown = report.render_markdown(result)
    assert "## by_market_state" in markdown
    assert "| Profit factor |" in markdown
    assert "| trend_up | 2 | 1 | 1 | 0.5000 | 2.0000 |" in markdown


def test_breakdowns_present_but_empty_for_zero_signals():
    result = summary.build_summary("run-empty", [])

    for axis in ("by_bias", "by_signal_type", "by_timeframe", "by_market_state"):
        assert result["breakdowns"][axis] == {}


# =============================================================================
# JSON: never emits NaN/Infinity
# =============================================================================


def test_json_never_contains_nan_or_infinity_for_wins_only():
    signals = [make_signal(id="s1", pnl=1.0)]
    result = summary.build_summary("run-1", signals)

    text = report.render_json(result)  # must not raise (allow_nan=False)

    assert "NaN" not in text
    assert "Infinity" not in text
    parsed = json.loads(text)
    assert parsed["profit_factor"] is None


def test_json_round_trips_all_required_fields_for_zero_signals():
    result = summary.build_summary("run-empty", [])
    text = report.render_json(result)
    parsed = json.loads(text)

    assert parsed == {
        "run_id": "run-empty",
        "total_signals": 0,
        "win_count": 0,
        "loss_count": 0,
        "win_rate": 0.0,
        "total_pnl": 0.0,
        "average_pnl": 0.0,
        "profit_factor": 0.0,
        "max_drawdown": 0.0,
        "average_holding_bars": None,
        "breakdowns": {"by_bias": {}, "by_signal_type": {}, "by_timeframe": {}, "by_market_state": {}},
    }
    for value in parsed.values():
        if isinstance(value, float):
            assert math.isfinite(value)
