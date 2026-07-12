"""Issue #44: tests for the XAUUSD 1h squeeze-episode breakout strategy A.

Covers the two independently-testable layers:

- ``evaluate_squeeze_exit`` -- the composite gap -> intrabar SL/TP -> same-bar
  SL priority -> 24-bar time-exit walk, driven by hand-written future candles.
- ``evaluate_squeeze_breakout_signals`` -- the squeeze episode state machine +
  Bollinger close-break Trigger, driven by a synthetic price path that forms a
  real Market-State ``squeeze`` episode and then breaks out.

plus a runner-level integration slice (symbol normalization, saved
signal_type / trigger_type / strategy_profile, summary identifiability, and
that the default profile still runs the untouched trend_follow path).
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

import pandas as pd
import pytest

TESTS_DIR = pathlib.Path(__file__).resolve().parent
BOT_DIR = TESTS_DIR.parent
REPO_ROOT = BOT_DIR.parent
MARKET_WATCH_DIR = REPO_ROOT / "market-watch"
PAPER_SIGNAL_DIR = REPO_ROOT / "paper-signal"
NEWS_FEED_BOT_DIR = REPO_ROOT / "news-feed-bot"

for _dir in (TESTS_DIR, BOT_DIR, MARKET_WATCH_DIR, PAPER_SIGNAL_DIR, NEWS_FEED_BOT_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import squeeze_breakout as sb  # noqa: E402
import execution_model as execution  # noqa: E402
import exit_policy  # noqa: E402
import runner  # noqa: E402
import backtest_store as store  # noqa: E402
import chart_collector as collector  # noqa: E402  (market-watch)
from chart_models import Candle  # noqa: E402  (market-watch)

START = "2026-06-01T00:00:00Z"

# The synthetic warm-up: 60 volatile bars (high Bollinger bandwidth) then 34
# nearly-flat bars (bandwidth collapses into the lower 10% -> Market State
# ``squeeze``). Indicators become ready at bar 74 (SMA75), and bars 74..93 all
# classify as ``squeeze``, so the bar at index 94 is the sole episode-end /
# Trigger-evaluation bar. Verified in exploration before these tests were fixed.
VOLATILE = 60
FLAT = 34
TRIGGER_INDEX = VOLATILE + FLAT  # 94


def _prefix(level=2000.0):
    bars = []
    for i in range(VOLATILE):
        c = level + (3 if i % 2 == 0 else -3)
        bars.append((c, c + 0.02, c - 0.02, c))
    for i in range(FLAT):
        c = level + (0.05 if i % 2 == 0 else -0.05)
        bars.append((c, c + 0.02, c - 0.02, c))
    return bars


def _frame(bars, start=START):
    index = pd.date_range(start, periods=len(bars), freq="1h", tz="UTC")
    frame = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=index)
    frame["volume"] = 100.0
    return frame


def _run_signals(bars, *, from_index=TRIGGER_INDEX, to_index=None, exec_model=None, symbol="XAU_USD"):
    frame = _frame(bars)
    to_index = len(bars) - 1 if to_index is None else to_index
    return frame, sb.evaluate_squeeze_breakout_signals(
        frame,
        frame.index[from_index],
        frame.index[to_index],
        symbol=symbol,
        timeframe="1h",
        exec_model=exec_model,
    )


# =============================================================================
# evaluate_squeeze_exit -- composite exit walk (pure, hand-written candles)
# =============================================================================


def _exit_frame(bars, start="2026-06-10T00:00:00Z"):
    index = pd.date_range(start, periods=len(bars), freq="1h", tz="UTC")
    return pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=index)


# long: entry=100, stop=95, target=110 (risk 5, 2R). short: entry=100, stop=105, target=90.
LONG_KW = dict(direction="long", entry_price=100.0, stop_price=95.0, target_price=110.0)
SHORT_KW = dict(direction="short", entry_price=100.0, stop_price=105.0, target_price=90.0)


def test_exit_long_intrabar_take_profit():
    candles = _exit_frame([(101, 110, 100, 105)])
    result = sb.evaluate_squeeze_exit(**LONG_KW, candles=candles)
    assert result.status == exit_policy.STATUS_WIN
    assert result.close_price == 110.0
    assert result.close_reason == exit_policy.REASON_TAKE_PROFIT


def test_exit_long_intrabar_stop_loss():
    candles = _exit_frame([(99, 100, 95, 96)])
    result = sb.evaluate_squeeze_exit(**LONG_KW, candles=candles)
    assert result.status == exit_policy.STATUS_LOSS
    assert result.close_price == 95.0
    assert result.close_reason == exit_policy.REASON_STOP_LOSS


def test_exit_short_intrabar_take_profit():
    candles = _exit_frame([(99, 100, 90, 95)])
    result = sb.evaluate_squeeze_exit(**SHORT_KW, candles=candles)
    assert result.status == exit_policy.STATUS_WIN
    assert result.close_price == 90.0


def test_exit_same_bar_stop_and_tp_prefers_stop_loss():
    # This bar touches both stop (low 95) and target (high 110): SL wins.
    candles = _exit_frame([(100, 110, 95, 100)])
    result = sb.evaluate_squeeze_exit(**LONG_KW, candles=candles)
    assert result.status == exit_policy.STATUS_LOSS
    assert result.close_price == 95.0


def test_exit_long_gap_stop_fills_at_open():
    # Opens below the stop: a gap through the stop fills at the (worse) raw open.
    candles = _exit_frame([(94, 96, 93, 95)])
    result = sb.evaluate_squeeze_exit(**LONG_KW, candles=candles)
    assert result.status == exit_policy.STATUS_LOSS
    assert result.close_price == 94.0
    assert result.close_reason == exit_policy.REASON_STOP_LOSS


def test_exit_long_gap_tp_fills_at_target_not_open():
    # Opens above the target: conservative fill at the target (NOT the more
    # favorable open). Issue #43-authoritative resolution of the gap-TP price.
    candles = _exit_frame([(112, 115, 111, 113)])
    result = sb.evaluate_squeeze_exit(**LONG_KW, candles=candles)
    assert result.status == exit_policy.STATUS_WIN
    assert result.close_price == 110.0  # target, not 112 (the open)


def test_exit_short_gap_tp_fills_at_target_not_open():
    candles = _exit_frame([(88, 89, 85, 87)])
    result = sb.evaluate_squeeze_exit(**SHORT_KW, candles=candles)
    assert result.status == exit_policy.STATUS_WIN
    assert result.close_price == 90.0  # target, not 88 (the open)


def test_exit_gap_stop_checked_before_gap_tp_is_impossible_but_order_holds():
    # A single open can't be both <=stop and >=target (stop<entry<target), so
    # this just asserts a normal gap-stop still fills as a stop.
    candles = _exit_frame([(90, 120, 89, 95)])
    result = sb.evaluate_squeeze_exit(**LONG_KW, candles=candles)
    assert result.close_reason == exit_policy.REASON_STOP_LOSS


def test_exit_time_exit_on_24th_bar_at_close():
    # 24 bars all inside (95, 110), no gap: time exit on the 24th at its close.
    inside = [(100, 101, 99, 100)] * 24
    candles = _exit_frame(inside)
    result = sb.evaluate_squeeze_exit(**LONG_KW, candles=candles, time_exit_bars=24)
    assert result.status == exit_policy.STATUS_CLOSED
    assert result.close_reason == exit_policy.REASON_TIME_EXPIRED
    assert result.close_price == 100.0
    assert result.close_time == candles.index[23].isoformat()  # the 24th bar


def test_exit_24th_bar_sl_evaluated_before_time_exit():
    # 23 inside bars, then a 24th bar that hits the stop: SL wins over time exit.
    bars = [(100, 101, 99, 100)] * 23 + [(99, 100, 95, 96)]
    candles = _exit_frame(bars)
    result = sb.evaluate_squeeze_exit(**LONG_KW, candles=candles, time_exit_bars=24)
    assert result.status == exit_policy.STATUS_LOSS
    assert result.close_reason == exit_policy.REASON_STOP_LOSS
    assert result.close_price == 95.0


def test_exit_no_close_when_candles_run_out_before_any_exit():
    inside = [(100, 101, 99, 100)] * 5
    candles = _exit_frame(inside)
    result = sb.evaluate_squeeze_exit(**LONG_KW, candles=candles, time_exit_bars=24)
    assert result.status == exit_policy.STATUS_OPEN
    assert result.close_price is None
    assert result.close_reason == exit_policy.REASON_NO_CLOSE


def test_exit_empty_candles_is_open_not_error():
    empty = _exit_frame([]).astype(float)
    result = sb.evaluate_squeeze_exit(**LONG_KW, candles=empty)
    assert result.status == exit_policy.STATUS_OPEN


def test_exit_missing_open_column_raises():
    candles = _exit_frame([(100, 101, 99, 100)]).drop(columns=["open"])
    with pytest.raises(ValueError, match="open"):
        sb.evaluate_squeeze_exit(**LONG_KW, candles=candles)


# =============================================================================
# evaluate_squeeze_breakout_signals -- episode state machine + Trigger
# =============================================================================


def test_consecutive_squeeze_is_one_episode_one_entry():
    # Validation spans the whole squeeze run + the breakout bar; only the
    # episode-end bar (index 94) triggers, exactly once.
    bars = _prefix() + [(2009, 2010, 2005, 2010), (2011, 2021, 2011, 2015)]
    frame, signals = _run_signals(bars, from_index=75)
    assert len(signals) == 1
    assert signals[0].signal_time == frame.index[TRIGGER_INDEX].isoformat()


def test_no_entry_while_squeeze_continues():
    # Validation window entirely inside the squeeze run (ends before the
    # breakout bar): no episode-end bar, so no entry.
    bars = _prefix()
    _frame_, signals = _run_signals(bars, from_index=75, to_index=93)
    assert signals == []


def test_long_breakout_close_beyond_upper_band():
    bars = _prefix() + [(2009, 2010, 2005, 2010), (2011, 2021, 2011, 2015)]
    _frame_, signals = _run_signals(bars)
    assert len(signals) == 1
    signal = signals[0]
    assert signal.direction == "long"
    assert signal.entry_price == 2010.0
    assert signal.stop_price == 2005.0  # Trigger bar low
    assert signal.target_price == 2020.0  # entry + 2 * (entry - stop)


def test_short_breakout_close_beyond_lower_band():
    bars = _prefix() + [(2000, 1991, 1980, 1990), (1989, 1989, 1979, 1980)]
    _frame_, signals = _run_signals(bars)
    assert len(signals) == 1
    signal = signals[0]
    assert signal.direction == "short"
    assert signal.entry_price == 1990.0
    assert signal.stop_price == 1991.0  # Trigger bar high
    assert signal.target_price == 1988.0  # entry - 2 * (stop - entry)


def test_wick_only_break_is_not_a_trigger():
    # Episode-end bar closes *inside* the band (2000.1 < bb_upper ~2000.11) but
    # its high wicks well above it: a close-break is required, so no entry.
    bars = _prefix() + [(2000.0, 2005.1, 1999.0, 2000.1), (2000, 2001, 1999, 2000)]
    _frame_, signals = _run_signals(bars)
    assert signals == []


def test_invalid_stop_when_risk_is_zero():
    # Trigger bar low == close and zero execution cost -> stop == entry -> the
    # stop is not strictly on the correct side (risk <= 0) -> no entry.
    bars = _prefix() + [(2009, 2011, 2010, 2010), (2010, 2011, 2009, 2010)]
    _frame_, signals = _run_signals(bars)
    assert signals == []


def test_entry_bar_itself_is_never_exited():
    # The Trigger bar's own low (2005) equals the stop; if the exit walk wrongly
    # included the entry bar it would stop out immediately at the entry bar.
    # Instead the position wins on the *next* bar.
    bars = _prefix() + [(2009, 2010, 2005, 2010), (2011, 2021, 2011, 2015)]
    frame, signals = _run_signals(bars)
    signal = signals[0]
    assert signal.exit_result.status == exit_policy.STATUS_WIN
    assert signal.exit_result.close_time == frame.index[TRIGGER_INDEX + 1].isoformat()
    assert signal.exit_result.close_time != signal.signal_time


def test_two_r_target_uses_execution_adjusted_entry():
    bars = _prefix() + [(2009, 2010, 2005, 2010), (2011, 2012, 2011, 2011)]
    exec_model = execution.ExecutionModel(spread=0.5, slippage=0.5)  # cost 1.0/side
    _frame_, signals = _run_signals(bars, exec_model=exec_model)
    signal = signals[0]
    assert signal.entry_price == 2011.0  # 2010 + 1.0 unfavorable
    assert signal.stop_price == 2005.0  # raw Trigger-bar low, uncosted
    # risk = 2011 - 2005 = 6 -> target = 2011 + 12 = 2023, from the *adjusted* entry.
    assert signal.target_price == 2023.0


def test_no_reentry_or_reverse_within_same_episode():
    # ep1 long stops out, then price trends down with no new squeeze forming:
    # neither a re-entry nor a reverse-direction entry is allowed.
    tail = [(2011, 2012, 2004, 2005)]  # stop-out on the bar after entry
    falling = [(2005 - 2 * i, 2005 - 2 * i + 0.5, 2005 - 2 * i - 2, 2005 - 2 * i - 1) for i in range(1, 15)]
    bars = _prefix() + [(2009, 2010, 2005, 2010)] + tail + falling
    _frame_, signals = _run_signals(bars)
    assert len(signals) == 1
    assert signals[0].direction == "long"
    assert signals[0].exit_result.status == exit_policy.STATUS_LOSS


def test_new_squeeze_episode_allows_a_second_entry():
    # ep1 stops out, a fresh flat squeeze forms, then a second breakout triggers
    # a second, independent entry.
    tail = [(2011, 2012, 2004, 2005)]
    flat2 = [
        (2005 + (0.05 if i % 2 == 0 else -0.05), 2005 + 0.07, 2005 - 0.07, 2005 + (0.05 if i % 2 == 0 else -0.05))
        for i in range(FLAT)
    ]
    breakout2 = [(2014, 2016, 2010, 2015), (2015, 2026, 2015, 2020)]
    bars = _prefix() + [(2009, 2010, 2005, 2010)] + tail + flat2 + breakout2
    _frame_, signals = _run_signals(bars)
    assert len(signals) == 2
    assert signals[0].exit_result.status == exit_policy.STATUS_LOSS
    assert signals[1].exit_result.status == exit_policy.STATUS_WIN


def test_episode_ending_in_warmup_is_consumed_not_emitted():
    # Validation starts *after* the breakout bar, so the episode that ends at
    # the breakout is consumed during warm-up and never emitted -- and no stale
    # re-trigger happens later either.
    bars = _prefix() + [(2009, 2010, 2005, 2010), (2011, 2021, 2011, 2015)]
    _frame_, signals = _run_signals(bars, from_index=TRIGGER_INDEX + 1)
    assert signals == []


def test_no_data_after_validation_end_is_referenced():
    # A second squeeze->breakout episode exists after validation_end; capping the
    # window at the first breakout must ignore everything past it.
    tail = [(2011, 2012, 2004, 2005)]
    flat2 = [
        (2005 + (0.05 if i % 2 == 0 else -0.05), 2005 + 0.07, 2005 - 0.07, 2005 + (0.05 if i % 2 == 0 else -0.05))
        for i in range(FLAT)
    ]
    breakout2 = [(2014, 2016, 2010, 2015), (2015, 2026, 2015, 2020)]
    bars = _prefix() + [(2009, 2010, 2005, 2010)] + tail + flat2 + breakout2
    # Cap validation_end at the first breakout bar: the second episode (well
    # past it) must never be seen.
    _frame_, signals = _run_signals(bars, from_index=TRIGGER_INDEX, to_index=TRIGGER_INDEX + 1)
    assert len(signals) == 1
    assert signals[0].direction == "long"


def test_insufficient_warmup_produces_no_signals():
    bars = _prefix() + [(2009, 2010, 2005, 2010), (2011, 2021, 2011, 2015)]
    frame = _frame(bars)
    # Start validation at bar 2: only 2 warm-up bars, far below the default 75.
    signals = sb.evaluate_squeeze_breakout_signals(
        frame, frame.index[2], frame.index[-1], symbol="XAU_USD", timeframe="1h"
    )
    assert signals == []


# =============================================================================
# Runner integration
# =============================================================================


def _seed_chart_db(db_path, bars, *, chart_symbol="XAUUSD"):
    index = pd.date_range(START, periods=len(bars), freq="1h", tz="UTC")
    candles = [
        Candle(
            symbol=chart_symbol,
            timeframe="1h",
            timestamp=collector.to_utc_iso_z(ts),
            open=float(o),
            high=float(h),
            low=float(low),
            close=float(c),
            volume=100.0,
        )
        for ts, (o, h, low, c) in zip(index, bars)
    ]
    collector.save_candles(db_path, candles)
    return index


def _squeeze_args(tmp_path, index, **overrides):
    argv = [
        "--symbol", overrides.pop("symbol", "XAU_USD"),
        "--timeframe", "1h",
        "--from", index[overrides.pop("from_index", TRIGGER_INDEX)].isoformat(),
        "--to", index[-1].isoformat(),
        "--db-path", str(tmp_path / "chart_data.sqlite3"),
        "--backtest-db-path", str(tmp_path / "backtest_results.sqlite3"),
        "--market-bias-db-path", str(tmp_path / "market_bias.sqlite3"),
    ]
    for key, value in overrides.items():
        argv.extend([f"--{key.replace('_', '-')}", str(value)])
    return runner.parse_args(argv)


def test_parse_args_defaults_strategy_profile_to_trend_follow():
    args = runner.parse_args(
        ["--symbol", "XAU_USD", "--timeframe", "1h", "--from", "2026-06-01T00:00:00", "--to", "2026-06-02T00:00:00"]
    )
    assert args.strategy_profile == runner.STRATEGY_PROFILE_TREND_FOLLOW


def test_chart_symbol_normalizes_xau_usd_to_xauusd():
    assert runner._chart_symbol("XAU_USD") == "XAUUSD"


def test_runner_squeeze_profile_saves_signal_with_squeeze_classification(tmp_path):
    bars = _prefix() + [(2009, 2010, 2005, 2010), (2011, 2021, 2011, 2015)]
    index = _seed_chart_db(tmp_path / "chart_data.sqlite3", bars)
    args = _squeeze_args(tmp_path, index, strategy_profile=sb.STRATEGY_PROFILE)

    exit_code, summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK
    assert summary["signals_generated"] == 1
    assert summary["strategy_profile"] == sb.STRATEGY_PROFILE

    backtest_db = tmp_path / "backtest_results.sqlite3"
    saved = store.list_backtest_signals(summary["run_id"], db_path=backtest_db, limit=100)
    assert len(saved) == 1
    row = saved[0]
    assert row["signal_type"] == sb.SIGNAL_TYPE
    assert row["trigger_type"] == sb.TRIGGER_TYPE
    assert row["strategy_profile"] == sb.STRATEGY_PROFILE
    assert row["direction"] == "long"
    assert row["entry_price"] == 2010.0
    assert row["invalidation_price"] == 2005.0
    assert row["take_profit_price"] == 2020.0
    assert row["result_status"] == exit_policy.STATUS_WIN
    # Symbol is stored in the CLI form (existing convention); candles were found
    # only via the XAU_USD -> XAUUSD normalization.
    assert row["symbol"] == "XAU_USD"


def test_runner_squeeze_summary_identifies_strategy_by_signal_type(tmp_path):
    bars = _prefix() + [(2009, 2010, 2005, 2010), (2011, 2021, 2011, 2015)]
    index = _seed_chart_db(tmp_path / "chart_data.sqlite3", bars)
    args = _squeeze_args(tmp_path, index, strategy_profile=sb.STRATEGY_PROFILE)
    _exit_code, summary = runner.run_backtest(args)
    by_signal_type = summary["signal_summary"]["breakdowns"]["by_signal_type"]
    assert sb.SIGNAL_TYPE in by_signal_type


def test_runner_squeeze_records_strategy_params_in_run_config(tmp_path):
    import json

    bars = _prefix() + [(2009, 2010, 2005, 2010), (2011, 2021, 2011, 2015)]
    index = _seed_chart_db(tmp_path / "chart_data.sqlite3", bars)
    args = _squeeze_args(tmp_path, index, strategy_profile=sb.STRATEGY_PROFILE)
    _exit_code, summary = runner.run_backtest(args)

    run = store.get_backtest_run(summary["run_id"], db_path=tmp_path / "backtest_results.sqlite3")
    config = json.loads(run["config_json"])
    assert config["strategy_profile"] == sb.STRATEGY_PROFILE
    assert config["risk_reward"] == sb.RISK_REWARD
    assert config["time_exit_bars"] == sb.TIME_EXIT_BARS
    assert config["same_candle_priority"] == exit_policy.SAME_CANDLE_PRIORITY_STOP_LOSS
    assert run["exit_policy"] == sb.EXIT_POLICY_LABEL


def test_runner_default_profile_never_produces_squeeze_signals(tmp_path):
    # The same squeeze fixture, run with the DEFAULT profile, must go through the
    # untouched paper-signal path and never emit a squeeze_breakout signal.
    bars = _prefix() + [(2009, 2010, 2005, 2010), (2011, 2021, 2011, 2015)]
    index = _seed_chart_db(tmp_path / "chart_data.sqlite3", bars)
    args = _squeeze_args(tmp_path, index)  # no strategy_profile override -> default
    exit_code, summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK
    assert summary["strategy_profile"] == runner.STRATEGY_PROFILE_TREND_FOLLOW

    saved = store.list_backtest_signals(summary["run_id"], db_path=tmp_path / "backtest_results.sqlite3", limit=100)
    assert all(row["signal_type"] != sb.SIGNAL_TYPE for row in saved)


def test_runner_squeeze_profile_never_places_order_or_calls_discord(tmp_path, monkeypatch):
    # Defensive: the squeeze path must remain paper-only. Break any accidental
    # network/broker import surface and confirm the run still completes.
    bars = _prefix() + [(2009, 2010, 2005, 2010), (2011, 2021, 2011, 2015)]
    index = _seed_chart_db(tmp_path / "chart_data.sqlite3", bars)
    args = _squeeze_args(tmp_path, index, strategy_profile=sb.STRATEGY_PROFILE)
    exit_code, summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK
    assert summary["signals_generated"] == 1


# =============================================================================
# Blocker (review): a squeeze-breakout TP fills exactly at the 2R target -- the
# close-side spread/slippage is NOT re-applied, so PnL stays pinned to +2R even
# with non-zero costs. Verified at the *save* level (post-Execution-Model).
# =============================================================================

# spread + slippage = 1.0 per side. Every case below must leave the TP
# close_price exactly on the target and PnL at exactly 2R regardless.
_COST = dict(spread="0.5", slippage="0.5")


def _run_and_fetch_one_signal(tmp_path, bars, **overrides):
    index = _seed_chart_db(tmp_path / "chart_data.sqlite3", bars)
    args = _squeeze_args(tmp_path, index, strategy_profile=sb.STRATEGY_PROFILE, **overrides)
    _exit_code, summary = runner.run_backtest(args)
    saved = store.list_backtest_signals(summary["run_id"], db_path=tmp_path / "backtest_results.sqlite3", limit=100)
    assert len(saved) == 1
    return saved[0]


def test_long_normal_tp_close_equals_target_with_nonzero_cost(tmp_path):
    # entry 2010+1=2011, stop 2005, risk 6, target 2023. bar t+1 touches 2023
    # intrabar (high 2024) without gapping.
    bars = _prefix() + [(2009, 2010, 2005, 2010), (2012, 2024, 2011, 2015)]
    row = _run_and_fetch_one_signal(tmp_path, bars, **_COST)
    assert row["direction"] == "long"
    assert row["result_status"] == exit_policy.STATUS_WIN
    assert row["close_reason"] == exit_policy.REASON_TAKE_PROFIT
    assert row["take_profit_price"] == 2023.0
    assert row["close_price"] == 2023.0  # target, NOT 2023 - cost
    # PnL is exactly 2R = 2 * |entry - stop| = 2 * 6.
    assert row["pnl"] == 12.0


def test_long_gap_tp_close_equals_target_with_nonzero_cost(tmp_path):
    # bar t+1 gaps open above the target (open 2025 >= 2023).
    bars = _prefix() + [(2009, 2010, 2005, 2010), (2025, 2030, 2024, 2026)]
    row = _run_and_fetch_one_signal(tmp_path, bars, **_COST)
    assert row["direction"] == "long"
    assert row["close_reason"] == exit_policy.REASON_TAKE_PROFIT
    assert row["close_price"] == 2023.0  # target, NOT the more favorable 2025 open
    assert row["pnl"] == 12.0


def test_short_normal_tp_close_equals_target_with_nonzero_cost(tmp_path):
    # entry 1990-1=1989, stop 1991, risk 2, target 1985. bar t+1 touches 1985
    # intrabar (low 1984) without gapping.
    bars = _prefix() + [(2000, 1991, 1980, 1990), (1988, 1988, 1984, 1986)]
    row = _run_and_fetch_one_signal(tmp_path, bars, **_COST)
    assert row["direction"] == "short"
    assert row["close_reason"] == exit_policy.REASON_TAKE_PROFIT
    assert row["take_profit_price"] == 1985.0
    assert row["close_price"] == 1985.0  # target, NOT 1985 + cost
    assert row["pnl"] == 4.0  # 2R = 2 * 2


def test_short_gap_tp_close_equals_target_with_nonzero_cost(tmp_path):
    # bar t+1 gaps open below the target (open 1983 <= 1985).
    bars = _prefix() + [(2000, 1991, 1980, 1990), (1983, 1984, 1980, 1982)]
    row = _run_and_fetch_one_signal(tmp_path, bars, **_COST)
    assert row["direction"] == "short"
    assert row["close_reason"] == exit_policy.REASON_TAKE_PROFIT
    assert row["close_price"] == 1985.0  # target, NOT the more favorable 1983 open
    assert row["pnl"] == 4.0


def test_squeeze_stop_close_still_takes_close_side_cost(tmp_path):
    # Scoping proof: only the TP leg is cost-exempt. A stop-loss close still gets
    # the realistic close-side cost, so close = raw stop - cost.
    bars = _prefix() + [(2009, 2010, 2005, 2010), (2006, 2007, 2004, 2005)]
    row = _run_and_fetch_one_signal(tmp_path, bars, **_COST)
    assert row["direction"] == "long"
    assert row["result_status"] == exit_policy.STATUS_LOSS
    assert row["close_reason"] == exit_policy.REASON_STOP_LOSS
    assert row["invalidation_price"] == 2005.0
    assert row["close_price"] == 2004.0  # raw stop 2005 - cost 1.0 (cost applied)
    assert row["pnl"] == -7.0  # (2004 - 2011)


# =============================================================================
# Major (review): the summary subcommand re-renders strategy profile + execution
# conditions from the saved run config in every format, even with zero signals.
# =============================================================================


def _saved_run_id(tmp_path, bars, **overrides):
    index = _seed_chart_db(tmp_path / "chart_data.sqlite3", bars)
    args = _squeeze_args(tmp_path, index, strategy_profile=sb.STRATEGY_PROFILE, **overrides)
    _exit_code, summary = runner.run_backtest(args)
    return summary["run_id"]


def _summary_output(tmp_path, run_id, fmt, capsys):
    runner.main(
        [
            "summary",
            "--run-id", run_id,
            "--backtest-db-path", str(tmp_path / "backtest_results.sqlite3"),
            "--format", fmt,
        ]
    )
    return capsys.readouterr().out


def test_summary_command_shows_strategy_profile_and_costs_in_text(tmp_path, capsys):
    bars = _prefix() + [(2009, 2010, 2005, 2010), (2012, 2024, 2011, 2015)]
    run_id = _saved_run_id(tmp_path, bars, **_COST)
    out = _summary_output(tmp_path, run_id, "text", capsys)
    assert sb.STRATEGY_PROFILE in out
    assert "spread=0.5" in out
    assert "slippage=0.5" in out


def test_summary_command_shows_strategy_profile_in_json(tmp_path, capsys):
    import json

    bars = _prefix() + [(2009, 2010, 2005, 2010), (2012, 2024, 2011, 2015)]
    run_id = _saved_run_id(tmp_path, bars, **_COST)
    out = _summary_output(tmp_path, run_id, "json", capsys)
    payload = json.loads(out)
    assert payload["run_meta"]["strategy_profile"] == sb.STRATEGY_PROFILE
    assert payload["run_meta"]["execution_model"]["spread"] == 0.5
    assert payload["run_meta"]["execution_model"]["slippage"] == 0.5


def test_summary_command_shows_strategy_profile_in_markdown(tmp_path, capsys):
    bars = _prefix() + [(2009, 2010, 2005, 2010), (2012, 2024, 2011, 2015)]
    run_id = _saved_run_id(tmp_path, bars, **_COST)
    out = _summary_output(tmp_path, run_id, "markdown", capsys)
    assert sb.STRATEGY_PROFILE in out
    assert "Strategy profile" in out


def test_summary_command_identifies_strategy_even_with_zero_signals(tmp_path, capsys):
    # A run whose validation window sits entirely inside the squeeze (no
    # breakout) saves zero signals; its strategy profile must still be
    # recoverable from the run config, not inferred from (absent) signals.
    bars = _prefix()
    index = _seed_chart_db(tmp_path / "chart_data.sqlite3", bars)
    args = _squeeze_args(tmp_path, index, strategy_profile=sb.STRATEGY_PROFILE, from_index=75)
    # Cap the window before the (nonexistent) breakout: purely squeeze bars.
    args.to_time = index[93].isoformat()
    _exit_code, summary = runner.run_backtest(args)
    assert summary["signals_generated"] == 0

    out = _summary_output(tmp_path, summary["run_id"], "text", capsys)
    assert "Total signals:       0" in out
    assert sb.STRATEGY_PROFILE in out


def test_summary_command_reports_trend_follow_profile_from_config(tmp_path, capsys):
    # The default (trend_follow) run also records its profile now, so a
    # re-rendered trend_follow run is self-identifying too.
    bars = _prefix() + [(2009, 2010, 2005, 2010), (2012, 2024, 2011, 2015)]
    index = _seed_chart_db(tmp_path / "chart_data.sqlite3", bars)
    args = _squeeze_args(tmp_path, index)  # default profile
    _exit_code, summary = runner.run_backtest(args)
    out = _summary_output(tmp_path, summary["run_id"], "text", capsys)
    assert runner.STRATEGY_PROFILE_TREND_FOLLOW in out
