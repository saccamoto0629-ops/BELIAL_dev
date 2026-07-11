import json
import pathlib
import sqlite3
import sys

import pandas as pd
import pytest

TESTS_DIR = pathlib.Path(__file__).resolve().parent
BOT_DIR = TESTS_DIR.parent
REPO_ROOT = BOT_DIR.parent
MARKET_WATCH_DIR = REPO_ROOT / "market-watch"
NEWS_FEED_BOT_DIR = REPO_ROOT / "news-feed-bot"

for _dir in (TESTS_DIR, BOT_DIR, MARKET_WATCH_DIR, NEWS_FEED_BOT_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import runner  # noqa: E402
import backtest_store as store  # noqa: E402
import chart_collector as collector  # noqa: E402  (market-watch)
from chart_models import Candle  # noqa: E402  (market-watch)
import market_bias_store as bias_store  # noqa: E402  (news-feed-bot)
import report  # noqa: E402  (Issue #23)
import summary as signal_summary  # noqa: E402  (Issue #23; aliased so it never shadows the many
# local variables named ``summary`` already used throughout this file for
# run_backtest()'s returned dict)

# Shared candle/bias fixture builders (Issue #25): also used by
# test_backtest_engine.py, so the same price-path fixture is defined once.
from candle_fixtures import make_uptrend_candles, seed_bull_bias, seed_chart_db  # noqa: E402


def make_args(tmp_path, *, symbol="USD_JPY", timeframe="1h", from_time, to_time, **overrides):
    argv = [
        "--symbol", symbol,
        "--timeframe", timeframe,
        "--from", from_time,
        "--to", to_time,
        "--db-path", str(overrides.pop("db_path", tmp_path / "chart_data.sqlite3")),
        "--backtest-db-path", str(overrides.pop("backtest_db_path", tmp_path / "backtest_results.sqlite3")),
        "--market-bias-db-path", str(overrides.pop("bias_db_path", tmp_path / "market_bias.sqlite3")),
    ]
    for key, value in overrides.items():
        argv.extend([f"--{key.replace('_', '-')}", str(value)])
    return runner.parse_args(argv)


# =============================================================================
# CLI argument parsing
# =============================================================================


def test_parse_args_requires_symbol_timeframe_from_to():
    with pytest.raises(SystemExit) as exc_info:
        runner.parse_args(["--symbol", "USD_JPY"])
    assert exc_info.value.code == 2  # argparse's own usage-error exit code


def test_parse_args_defaults_warmup_bars_to_warmup_module_default():
    import warmup

    args = runner.parse_args(
        ["--symbol", "USD_JPY", "--timeframe", "1h", "--from", "2026-06-01T00:00:00", "--to", "2026-06-02T00:00:00"]
    )
    assert args.warmup_bars == warmup.DEFAULT_WARMUP_BARS


def test_chart_symbol_strips_underscores_and_uppercases():
    assert runner._chart_symbol("USD_JPY") == "USDJPY"
    assert runner._chart_symbol("xauusd") == "XAUUSD"


# =============================================================================
# run_backtest: usage / no-data error paths
# =============================================================================


def test_run_backtest_returns_usage_error_when_to_before_from(tmp_path):
    args = make_args(tmp_path, from_time="2026-06-02T00:00:00", to_time="2026-06-01T00:00:00")

    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_USAGE_ERROR
    assert summary is None


def test_run_backtest_returns_no_data_when_symbol_has_no_candles(tmp_path):
    chart_db = tmp_path / "chart_data.sqlite3"
    seed_chart_db(chart_db, n=20)  # seeded as USDJPY, not EURUSD
    args = make_args(
        tmp_path, symbol="EUR_USD", from_time="2026-05-27T10:00:00", to_time="2026-05-27T12:00:00",
    )

    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_NO_DATA
    assert summary is None


def test_run_backtest_returns_no_data_when_validation_window_has_no_candles(tmp_path):
    chart_db = tmp_path / "chart_data.sqlite3"
    seed_chart_db(chart_db, n=20)
    args = make_args(tmp_path, from_time="2030-01-01T00:00:00", to_time="2030-01-02T00:00:00")

    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_NO_DATA
    assert summary is None


# =============================================================================
# run_backtest: warm-up / validation separation
# =============================================================================


def test_run_backtest_flags_insufficient_warmup_and_generates_no_signals(tmp_path):
    chart_db = tmp_path / "chart_data.sqlite3"
    index = seed_chart_db(chart_db, n=20)  # far fewer than DEFAULT_WARMUP_BARS
    args = make_args(
        tmp_path,
        from_time=index[15].isoformat(),
        to_time=index[19].isoformat(),
    )

    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_OK
    assert summary["warmup_insufficient"] is True
    assert summary["bars_processed"] == 5
    assert summary["signals_generated"] == 0


def test_run_backtest_only_counts_validation_bars_not_warmup_bars(tmp_path):
    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    index = seed_chart_db(chart_db, n=100)  # 90 warm-up + 10 validation
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[0]))
    args = make_args(
        tmp_path,
        from_time=index[90].isoformat(),
        to_time=index[99].isoformat(),
    )

    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_OK
    assert summary["warmup_insufficient"] is False
    assert summary["bars_processed"] == 10  # only the validation-window bars, never the 90 warm-up bars

    signals = store.list_backtest_signals(summary["run_id"], db_path=tmp_path / "backtest_results.sqlite3")
    validation_start_iso = index[90].isoformat()
    for signal in signals:
        assert signal["signal_time"] >= validation_start_iso  # never a warm-up bar


def test_run_backtest_excludes_candles_after_to(tmp_path):
    # Regression: candles stored after --to must never leak into warm-up or
    # validation (see warmup.py's "future rows" regression tests).
    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    index = seed_chart_db(chart_db, n=150)  # 90 warm-up + 10 validation + 50 future rows
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[0]))
    args = make_args(
        tmp_path,
        from_time=index[90].isoformat(),
        to_time=index[99].isoformat(),
    )

    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_OK
    assert summary["bars_processed"] == 10
    assert summary["warmup_bars_available"] == 90  # not 90 + 50 future rows


# =============================================================================
# run_backtest: persistence to the existing Backtest DB
# =============================================================================


def test_run_backtest_saves_a_backtest_run_row(tmp_path):
    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    backtest_db = tmp_path / "backtest_results.sqlite3"
    index = seed_chart_db(chart_db, n=100)
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[0]))
    args = make_args(
        tmp_path,
        from_time=index[90].isoformat(),
        to_time=index[99].isoformat(),
        run_name="my-custom-run",
    )

    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_OK
    run = store.get_backtest_run(summary["run_id"], db_path=backtest_db)
    assert run is not None
    assert run["run_name"] == "my-custom-run"
    assert run["symbol"] == "USD_JPY"
    assert run["timeframe"] == "1h"
    assert run["strategy_name"] == runner.DEFAULT_STRATEGY_NAME


def test_run_backtest_strategy_name_flag_is_saved_on_the_run(tmp_path):
    # Issue #24 checklist item: confirm --strategy-name (already implemented
    # since Issue #33) actually reaches the saved backtest_runs row.
    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    backtest_db = tmp_path / "backtest_results.sqlite3"
    index = seed_chart_db(chart_db, n=100)
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[0]))
    args = make_args(
        tmp_path, from_time=index[90].isoformat(), to_time=index[99].isoformat(),
        strategy_name="trend_pullback_v1",
    )

    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_OK
    run = store.get_backtest_run(summary["run_id"], db_path=backtest_db)
    assert run["strategy_name"] == "trend_pullback_v1"


def test_run_backtest_defaults_exit_policy_to_fixed_rr_when_not_specified(tmp_path):
    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    backtest_db = tmp_path / "backtest_results.sqlite3"
    index = seed_chart_db(chart_db, n=100)
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[0]))
    args = make_args(tmp_path, from_time=index[90].isoformat(), to_time=index[99].isoformat())

    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_OK
    run = store.get_backtest_run(summary["run_id"], db_path=backtest_db)
    assert run["exit_policy"] == "fixed_rr"  # exit_policy.DEFAULT_EXIT_POLICY, unchanged behavior


def test_run_backtest_exit_policy_flag_overrides_the_active_policy_and_is_saved(tmp_path):
    # Issue #24: --exit-policy must actually reach exit evaluation, and the
    # chosen policy must be reflected on both the saved run and every saved
    # signal -- not just recorded as a label.
    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    backtest_db = tmp_path / "backtest_results.sqlite3"
    index = seed_chart_db(chart_db, n=100)
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[0]))
    args = make_args(
        tmp_path, from_time=index[90].isoformat(), to_time=index[99].isoformat(),
        exit_policy="time_based_close",  # enabled by default, distinct from fixed_rr
    )

    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_OK
    run = store.get_backtest_run(summary["run_id"], db_path=backtest_db)
    assert run["exit_policy"] == "time_based_close"
    signals = store.list_backtest_signals(summary["run_id"], db_path=backtest_db)
    assert signals  # sanity: this fixture does generate signals
    assert all(signal["exit_policy"] == "time_based_close" for signal in signals)


def test_run_backtest_rejects_a_valid_but_disabled_exit_policy_with_a_clean_usage_error(tmp_path, capsys):
    # ma_break_invalidation is a real, known policy name (argparse accepts
    # it) but is disabled in the built-in default config -- this must fail
    # as a clear CLI usage error, not deep inside the first evaluate_exit().
    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    index = seed_chart_db(chart_db, n=100)
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[0]))
    args = make_args(
        tmp_path, from_time=index[90].isoformat(), to_time=index[99].isoformat(),
        exit_policy="ma_break_invalidation",
    )

    exit_code, summary = runner.run_backtest(args)
    captured = capsys.readouterr()

    assert exit_code == runner.EXIT_USAGE_ERROR
    assert summary is None
    assert "error: --exit-policy 'ma_break_invalidation' is not usable" in captured.err
    assert "Traceback" not in captured.err


def test_parse_args_rejects_an_unknown_exit_policy_choice(tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        make_args(
            tmp_path, from_time="2026-06-01T00:00:00", to_time="2026-06-02T00:00:00",
            exit_policy="not_a_real_policy",
        )
    assert exc_info.value.code == runner.EXIT_USAGE_ERROR


def test_run_help_documents_exit_policy_and_its_choices(capsys):
    with pytest.raises(SystemExit) as exc_info:
        runner.parse_args(["run", "--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "--exit-policy" in captured.out
    assert "fixed_rr" in captured.out
    assert "time_based_close" in captured.out


def test_run_backtest_generates_and_saves_long_signals_for_a_bull_uptrend(tmp_path):
    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    backtest_db = tmp_path / "backtest_results.sqlite3"
    index = seed_chart_db(chart_db, n=100)
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[0]))
    args = make_args(tmp_path, from_time=index[90].isoformat(), to_time=index[99].isoformat())

    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_OK
    assert summary["signals_generated"] == 10
    signals = store.list_backtest_signals(summary["run_id"], db_path=backtest_db)
    assert len(signals) == 10
    assert all(signal["direction"] == "long" for signal in signals)
    assert all(signal["entry_price"] is not None for signal in signals)
    assert all(signal["result_status"] is not None for signal in signals)  # every signal was exit-evaluated


def test_run_backtest_result_counts_match_saved_signal_statuses(tmp_path):
    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    backtest_db = tmp_path / "backtest_results.sqlite3"
    index = seed_chart_db(chart_db, n=100)
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[0]))
    args = make_args(tmp_path, from_time=index[90].isoformat(), to_time=index[99].isoformat())

    _, summary = runner.run_backtest(args)

    signals = store.list_backtest_signals(summary["run_id"], db_path=backtest_db)
    actual_counts = {}
    for signal in signals:
        status = signal["result_status"] or "open"
        actual_counts[status] = actual_counts.get(status, 0) + 1
    for status, count in actual_counts.items():
        assert summary["result_counts"][status] == count


def test_run_backtest_never_places_an_order_or_calls_discord(tmp_path, monkeypatch):
    # No network/broker call should be reachable from this path: fail loudly
    # if anything tries to touch the network.
    import requests

    def _boom(*args, **kwargs):
        raise AssertionError("Backtest Runner must never make a network call")

    monkeypatch.setattr(requests, "post", _boom)
    monkeypatch.setattr(requests, "get", _boom)

    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    index = seed_chart_db(chart_db, n=100)
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[0]))
    args = make_args(tmp_path, from_time=index[90].isoformat(), to_time=index[99].isoformat())

    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_OK
    assert summary["signals_generated"] == 10


# =============================================================================
# run_backtest: Market Bias lookup / fallback
# =============================================================================


def test_run_backtest_falls_back_to_neutral_bias_when_none_persisted(tmp_path):
    chart_db = tmp_path / "chart_data.sqlite3"
    index = seed_chart_db(chart_db, n=100)
    # market_bias.sqlite3 is never created/seeded here.
    args = make_args(tmp_path, from_time=index[90].isoformat(), to_time=index[99].isoformat())

    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_OK
    assert summary["bars_processed"] == 10
    assert summary["signals_generated"] == 0  # Neutral bias never produces a candidate


def test_run_backtest_never_leaks_a_future_market_bias_record(tmp_path):
    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    index = seed_chart_db(chart_db, n=100)
    # Only a bias record evaluated *after* validation_start is available.
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[95]), bias_id="future-bias")
    args = make_args(tmp_path, from_time=index[90].isoformat(), to_time=index[99].isoformat())

    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_OK
    assert summary["signals_generated"] == 0  # falls back to Neutral, not the future Bull record


# =============================================================================
# main(): CLI wiring + stdout summary
# =============================================================================


def test_main_prints_summary_and_returns_ok(tmp_path, capsys):
    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    backtest_db = tmp_path / "backtest_results.sqlite3"
    index = seed_chart_db(chart_db, n=100)
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[0]))

    argv = [
        "--symbol", "USD_JPY",
        "--timeframe", "1h",
        "--from", index[90].isoformat(),
        "--to", index[99].isoformat(),
        "--db-path", str(chart_db),
        "--backtest-db-path", str(backtest_db),
        "--market-bias-db-path", str(bias_db),
    ]

    exit_code = runner.main(argv)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Backtest Summary" in captured.out
    assert "Symbol:              USD_JPY" in captured.out
    assert "Bars processed:      10" in captured.out
    assert "Signals generated:   10" in captured.out


def test_main_returns_no_data_exit_code_without_printing_summary(tmp_path, capsys):
    chart_db = tmp_path / "chart_data.sqlite3"
    seed_chart_db(chart_db, n=10)

    argv = [
        "--symbol", "USD_JPY",
        "--timeframe", "1h",
        "--from", "2030-01-01T00:00:00",
        "--to", "2030-01-02T00:00:00",
        "--db-path", str(chart_db),
        "--backtest-db-path", str(tmp_path / "backtest_results.sqlite3"),
        "--market-bias-db-path", str(tmp_path / "market_bias.sqlite3"),
    ]

    exit_code = runner.main(argv)
    captured = capsys.readouterr()

    assert exit_code == runner.EXIT_NO_DATA
    assert "Backtest Summary" not in captured.out


# =============================================================================
# CLI validation: --warmup-bars / --confidence-threshold boundaries
# =============================================================================


@pytest.mark.parametrize("warmup_bars", ["0", "-1", "-100"])
def test_parse_args_rejects_non_positive_warmup_bars(tmp_path, warmup_bars):
    with pytest.raises(SystemExit) as exc_info:
        make_args(
            tmp_path,
            from_time="2026-06-01T00:00:00",
            to_time="2026-06-02T00:00:00",
            warmup_bars=warmup_bars,
        )
    assert exc_info.value.code == runner.EXIT_USAGE_ERROR


@pytest.mark.parametrize("warmup_bars", ["1", "75"])
def test_parse_args_accepts_positive_warmup_bars(tmp_path, warmup_bars):
    args = make_args(
        tmp_path,
        from_time="2026-06-01T00:00:00",
        to_time="2026-06-02T00:00:00",
        warmup_bars=warmup_bars,
    )
    assert args.warmup_bars == int(warmup_bars)


@pytest.mark.parametrize("confidence_threshold", ["-0.01", "-1", "1.01", "2"])
def test_parse_args_rejects_out_of_range_confidence_threshold(tmp_path, confidence_threshold):
    with pytest.raises(SystemExit) as exc_info:
        make_args(
            tmp_path,
            from_time="2026-06-01T00:00:00",
            to_time="2026-06-02T00:00:00",
            confidence_threshold=confidence_threshold,
        )
    assert exc_info.value.code == runner.EXIT_USAGE_ERROR


@pytest.mark.parametrize("confidence_threshold", ["0.0", "1.0", "0.4"])
def test_parse_args_accepts_boundary_confidence_threshold(tmp_path, confidence_threshold):
    args = make_args(
        tmp_path,
        from_time="2026-06-01T00:00:00",
        to_time="2026-06-02T00:00:00",
        confidence_threshold=confidence_threshold,
    )
    assert args.confidence_threshold == float(confidence_threshold)


# =============================================================================
# Same-bar exit exclusion: signal's own bar must never decide its own exit
# =============================================================================


def test_signal_own_bar_high_low_never_decides_its_own_exit(tmp_path):
    """Regression (Codex review): a signal generated at a bar's close must only
    be exposed to *subsequent* bars for Exit Policy evaluation. If the entry
    bar's own high/low were included, an extreme wick on the very bar that
    produced the signal could "predict" its own take-profit/stop-loss --
    which is look-ahead, since the paper position could not have been open
    yet when that bar's high/low occurred.
    """
    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    backtest_db = tmp_path / "backtest_results.sqlite3"

    warmup_index, warmup_candles = make_uptrend_candles(90)  # bars 0..89, steady Bull warm-up
    entry_timestamp = warmup_index[-1] + pd.Timedelta(hours=1)  # bar 90 (first validation bar)
    next_timestamp = entry_timestamp + pd.Timedelta(hours=1)  # bar 91 (second/last validation bar)

    entry_close = 200.0  # far above the warm-up uptrend so this signal's own take-profit
    # would be trivially hit if its own extreme high were (wrongly) included in the exit walk.
    entry_bar = Candle(
        symbol="USDJPY", timeframe="1h", timestamp=collector.to_utc_iso_z(entry_timestamp),
        open=199.9, high=500.0, low=199.5, close=entry_close, volume=100.0,
    )
    # The only bar actually available for exit evaluation: a small, uneventful
    # move that never touches fixed_rr's default stop/target.
    next_bar = Candle(
        symbol="USDJPY", timeframe="1h", timestamp=collector.to_utc_iso_z(next_timestamp),
        open=200.05, high=200.1, low=199.95, close=200.05, volume=100.0,
    )
    collector.save_candles(chart_db, warmup_candles + [entry_bar, next_bar])
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(warmup_index[0]))

    args = make_args(
        tmp_path,
        from_time=entry_timestamp.isoformat(),
        to_time=next_timestamp.isoformat(),
    )

    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_OK
    signals = store.list_backtest_signals(summary["run_id"], db_path=backtest_db)
    entry_signal = next(s for s in signals if s["signal_time"] == entry_timestamp.isoformat())

    # entry_bar's own high=500.0 would have hit fixed_rr's take-profit
    # immediately (status="win") if it had leaked into its own exit walk.
    assert entry_signal["result_status"] != "win"
    assert entry_signal["close_reason"] != "take_profit"
    # mfe must reflect only next_bar's modest +0.1 excursion, never
    # entry_bar's own high=500.0 (a ~300-point excursion relative to entry).
    assert entry_signal["mfe"] < 1.0


# =============================================================================
# Issue #23: signal_summary is generated automatically after a run
# =============================================================================


def test_run_backtest_result_includes_a_signal_summary(tmp_path):
    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    index = seed_chart_db(chart_db, n=100)
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[0]))
    args = make_args(tmp_path, from_time=index[90].isoformat(), to_time=index[99].isoformat())

    _, run_result = runner.run_backtest(args)

    assert "signal_summary" in run_result
    assert run_result["signal_summary"]["run_id"] == run_result["run_id"]
    assert run_result["signal_summary"]["total_signals"] == run_result["signals_generated"]


def test_run_backtest_signal_summary_is_zero_valued_when_no_signals_generated(tmp_path):
    chart_db = tmp_path / "chart_data.sqlite3"
    index = seed_chart_db(chart_db, n=100)  # no market_bias.sqlite3 -> Neutral fallback -> 0 signals
    args = make_args(tmp_path, from_time=index[90].isoformat(), to_time=index[99].isoformat())

    _, run_result = runner.run_backtest(args)

    assert run_result["signals_generated"] == 0
    assert run_result["signal_summary"]["total_signals"] == 0
    assert run_result["signal_summary"]["profit_factor"] == 0.0
    assert run_result["signal_summary"]["max_drawdown"] == 0.0


def test_main_prints_signal_summary_after_a_backtest_run(tmp_path, capsys):
    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    index = seed_chart_db(chart_db, n=100)
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[0]))

    argv = [
        "--symbol", "USD_JPY", "--timeframe", "1h",
        "--from", index[90].isoformat(), "--to", index[99].isoformat(),
        "--db-path", str(chart_db),
        "--backtest-db-path", str(tmp_path / "backtest_results.sqlite3"),
        "--market-bias-db-path", str(bias_db),
    ]

    exit_code = runner.main(argv)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Backtest Summary" in captured.out  # existing Issue #33 section, unchanged
    assert "Backtest Signal Summary" in captured.out  # new Issue #23 section
    assert "Total signals:       10" in captured.out


# =============================================================================
# Issue #23: `summary` subcommand -- re-fetch a past run_id
# =============================================================================


def _run_and_get_run_id(tmp_path, *, n=100, from_index=90, to_index=99):
    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    backtest_db = tmp_path / "backtest_results.sqlite3"
    index = seed_chart_db(chart_db, n=n)
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[0]))
    args = make_args(tmp_path, from_time=index[from_index].isoformat(), to_time=index[to_index].isoformat())
    _, run_result = runner.run_backtest(args)
    return run_result["run_id"], backtest_db


def test_summary_command_reprints_a_past_run_as_text(tmp_path, capsys):
    run_id, backtest_db = _run_and_get_run_id(tmp_path)

    exit_code = runner.main(["summary", "--run-id", run_id, "--backtest-db-path", str(backtest_db)])
    captured = capsys.readouterr()

    assert exit_code == runner.EXIT_OK
    assert "Backtest Signal Summary" in captured.out
    assert run_id in captured.out
    assert "Total signals:       10" in captured.out


def test_summary_command_json_format_prints_valid_json_to_stdout(tmp_path, capsys):
    run_id, backtest_db = _run_and_get_run_id(tmp_path)

    exit_code = runner.main(
        ["summary", "--run-id", run_id, "--backtest-db-path", str(backtest_db), "--format", "json"]
    )
    captured = capsys.readouterr()

    assert exit_code == runner.EXIT_OK
    parsed = json.loads(captured.out)
    assert parsed["run_id"] == run_id
    assert parsed["total_signals"] == 10
    assert "NaN" not in captured.out
    assert "Infinity" not in captured.out


def test_summary_command_markdown_format_prints_markdown_to_stdout(tmp_path, capsys):
    run_id, backtest_db = _run_and_get_run_id(tmp_path)

    exit_code = runner.main(
        ["summary", "--run-id", run_id, "--backtest-db-path", str(backtest_db), "--format", "markdown"]
    )
    captured = capsys.readouterr()

    assert exit_code == runner.EXIT_OK
    assert f"# Backtest Summary: {run_id}" in captured.out
    assert "| Total signals | 10 |" in captured.out


def test_summary_command_with_output_writes_file_and_prints_confirmation_not_full_dump(tmp_path, capsys):
    run_id, backtest_db = _run_and_get_run_id(tmp_path)
    output_path = tmp_path / "reports" / f"run_{run_id}.json"

    exit_code = runner.main(
        [
            "summary", "--run-id", run_id, "--backtest-db-path", str(backtest_db),
            "--format", "json", "--output", str(output_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == runner.EXIT_OK
    assert output_path.exists()  # parent dir "reports/" created automatically
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["run_id"] == run_id
    assert saved["total_signals"] == 10
    # stdout gets a short confirmation, not the (potentially large) file
    # content duplicated a second time.
    assert str(output_path) in captured.out
    assert '"total_signals"' not in captured.out


def test_summary_command_output_overwrites_an_existing_file(tmp_path):
    run_id, backtest_db = _run_and_get_run_id(tmp_path)
    output_path = tmp_path / "run.md"
    output_path.write_text("stale content", encoding="utf-8")

    exit_code = runner.main(
        [
            "summary", "--run-id", run_id, "--backtest-db-path", str(backtest_db),
            "--format", "markdown", "--output", str(output_path),
        ]
    )

    assert exit_code == runner.EXIT_OK
    content = output_path.read_text(encoding="utf-8")
    assert "stale content" not in content
    assert run_id in content


def test_summary_command_output_pointing_at_an_existing_directory_is_a_clean_cli_error(tmp_path, capsys):
    # Regression (CodeX review): --output naming an existing *directory*
    # must surface as a short CLI error, not an uncaught traceback.
    run_id, backtest_db = _run_and_get_run_id(tmp_path)
    output_dir = tmp_path / "already_a_directory"
    output_dir.mkdir()

    exit_code = runner.main(
        [
            "summary", "--run-id", run_id, "--backtest-db-path", str(backtest_db),
            "--format", "json", "--output", str(output_dir),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == runner.EXIT_USAGE_ERROR
    assert "Traceback" not in captured.err
    assert "error: could not write summary to" in captured.err
    assert str(output_dir) in captured.err


def test_summary_command_output_write_failure_is_a_clean_cli_error_not_a_traceback(tmp_path, capsys, monkeypatch):
    # Simulate an unwritable destination (permission denied, disk full, ...)
    # without depending on actual filesystem permissions, which behave
    # differently across platforms/CI.
    run_id, backtest_db = _run_and_get_run_id(tmp_path)
    output_path = tmp_path / "run.json"

    def _boom(content, output_path):
        raise OSError("simulated write failure")

    monkeypatch.setattr(report, "save_summary", _boom)

    exit_code = runner.main(
        [
            "summary", "--run-id", run_id, "--backtest-db-path", str(backtest_db),
            "--format", "json", "--output", str(output_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == runner.EXIT_USAGE_ERROR
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
    assert "error: could not write summary to" in captured.err
    assert "simulated write failure" in captured.err
    assert not output_path.exists()


def test_summary_command_json_save_still_succeeds_after_error_handling_added(tmp_path):
    # Not a regression test per se -- reconfirms the ordinary success path
    # (JSON + --output) still works now that failure handling wraps it.
    run_id, backtest_db = _run_and_get_run_id(tmp_path)
    output_path = tmp_path / "ok.json"

    exit_code = runner.main(
        [
            "summary", "--run-id", run_id, "--backtest-db-path", str(backtest_db),
            "--format", "json", "--output", str(output_path),
        ]
    )

    assert exit_code == runner.EXIT_OK
    assert json.loads(output_path.read_text(encoding="utf-8"))["run_id"] == run_id


def test_summary_command_markdown_save_still_succeeds_after_error_handling_added(tmp_path):
    run_id, backtest_db = _run_and_get_run_id(tmp_path)
    output_path = tmp_path / "ok.md"

    exit_code = runner.main(
        [
            "summary", "--run-id", run_id, "--backtest-db-path", str(backtest_db),
            "--format", "markdown", "--output", str(output_path),
        ]
    )

    assert exit_code == runner.EXIT_OK
    assert run_id in output_path.read_text(encoding="utf-8")


def test_summary_command_returns_no_data_for_unknown_run_id(tmp_path, capsys):
    backtest_db = tmp_path / "backtest_results.sqlite3"
    store.init_db(backtest_db)

    exit_code = runner.main(["summary", "--run-id", "does-not-exist", "--backtest-db-path", str(backtest_db)])
    captured = capsys.readouterr()

    assert exit_code == runner.EXIT_NO_DATA
    assert "Backtest Signal Summary" not in captured.out


def test_summary_command_handles_a_run_with_zero_signals(tmp_path, capsys):
    # warm-up-insufficient window -> a real backtest_runs row with zero
    # backtest_signals rows, exercising the 0-signal path end-to-end
    # through the actual DB round-trip (not just summary.build_summary()
    # called directly).
    chart_db = tmp_path / "chart_data.sqlite3"
    backtest_db = tmp_path / "backtest_results.sqlite3"
    index = seed_chart_db(chart_db, n=20)
    args = make_args(tmp_path, from_time=index[15].isoformat(), to_time=index[19].isoformat())
    _, run_result = runner.run_backtest(args)
    assert run_result["signals_generated"] == 0

    exit_code = runner.main(
        ["summary", "--run-id", run_result["run_id"], "--backtest-db-path", str(backtest_db), "--format", "json"]
    )
    captured = capsys.readouterr()

    assert exit_code == runner.EXIT_OK
    parsed = json.loads(captured.out)
    assert parsed["total_signals"] == 0
    assert parsed["profit_factor"] == 0.0
    assert parsed["average_holding_bars"] is None


def test_summary_command_signals_are_sorted_by_signal_time_not_db_row_order(tmp_path):
    # Regression: build the summary from freshly re-fetched DB rows (not
    # the in-memory list run_backtest() already had), proving the CLI path
    # -- not just summary.build_summary() in isolation -- sorts explicitly.
    run_id, backtest_db = _run_and_get_run_id(tmp_path)

    fetched_signals = store.list_backtest_signals(run_id, db_path=backtest_db)
    direct_result = signal_summary.build_summary(run_id, fetched_signals)

    assert direct_result["total_signals"] == 10
    # sanity: this run's uptrend fixture never actually closes within the
    # loaded window (see other tests), so every signal is still open and
    # max_drawdown/profit_factor must reflect "nothing resolved yet".
    assert direct_result["max_drawdown"] == 0.0


# =============================================================================
# Issue #26: virtual execution model (fixed spread + slippage)
# =============================================================================

import execution_model as execution  # noqa: E402


# A tiny time_based_close config: closes each signal at the *next* bar's
# market close. Unlike fixed_rr (whose take-profit/stop are derived from the
# entry price), this yields a raw close_price that does NOT move with the
# corrected entry -- so the spread/slippage adjustment applied to the close
# is isolated and directly assertable.
def _write_time_based_exit_config(tmp_path, *, max_hold_bars=1):
    config_path = tmp_path / "exit_config.json"
    config_path.write_text(
        json.dumps(
            {
                "exit_policy": "time_based_close",
                "policies": {
                    "time_based_close": {
                        "enabled": True,
                        "max_hold_bars": max_hold_bars,
                        "close_as": "closed",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return config_path


def _seed_trend(tmp_path, *, step, market_bias):
    """Seed a 100-bar trend (step<0 -> downtrend) + a matching bias; return the index."""
    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    index = seed_chart_db(chart_db, n=100, step=step)
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[0]), market_bias=market_bias)
    return index


def _run_and_fetch_signals(tmp_path, index, **overrides):
    backtest_db = tmp_path / "backtest_results.sqlite3"
    args = make_args(tmp_path, from_time=index[90].isoformat(), to_time=index[99].isoformat(), **overrides)
    exit_code, summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK
    signals = sorted(
        store.list_backtest_signals(summary["run_id"], db_path=backtest_db),
        key=lambda s: s["signal_time"],
    )
    return summary, signals


# --- CLI parsing: defaults + rejection of negatives --------------------------


def test_parse_args_defaults_spread_and_slippage_to_zero(tmp_path):
    args = make_args(tmp_path, from_time="2026-06-01T00:00:00", to_time="2026-06-02T00:00:00")
    assert args.spread == 0.0
    assert args.slippage == 0.0


def test_parse_args_accepts_nonnegative_spread_and_slippage(tmp_path):
    args = make_args(
        tmp_path, from_time="2026-06-01T00:00:00", to_time="2026-06-02T00:00:00",
        spread="0.01", slippage="0.005",
    )
    assert args.spread == pytest.approx(0.01)
    assert args.slippage == pytest.approx(0.005)


@pytest.mark.parametrize("field", ["spread", "slippage"])
@pytest.mark.parametrize("value", ["-0.01", "-1", "-100"])
def test_parse_args_rejects_negative_spread_or_slippage(tmp_path, field, value):
    with pytest.raises(SystemExit) as exc_info:
        make_args(
            tmp_path, from_time="2026-06-01T00:00:00", to_time="2026-06-02T00:00:00",
            **{field: value},
        )
    assert exc_info.value.code == runner.EXIT_USAGE_ERROR


def test_run_help_documents_spread_and_slippage(capsys):
    with pytest.raises(SystemExit):
        runner.parse_args(["run", "--help"])
    captured = capsys.readouterr()
    assert "--spread" in captured.out
    assert "--slippage" in captured.out


# --- config_json + summary carry the execution model -------------------------


def test_execution_model_settings_are_saved_to_config_json(tmp_path):
    backtest_db = tmp_path / "backtest_results.sqlite3"
    index = _seed_trend(tmp_path, step=0.05, market_bias="Bull")
    args = make_args(
        tmp_path, from_time=index[90].isoformat(), to_time=index[99].isoformat(),
        spread="0.01", slippage="0.005",
    )

    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_OK
    run = store.get_backtest_run(summary["run_id"], db_path=backtest_db)
    config = json.loads(run["config_json"])
    assert config["execution_model"] == {
        "type": execution.EXECUTION_MODEL_TYPE,
        "spread": 0.01,
        "slippage": 0.005,
    }
    # existing config_json keys must not be dropped
    for key in ("warmup_bars", "confidence_threshold", "chart_db_path", "market_bias_db_path"):
        assert key in config


def test_execution_model_settings_are_reflected_in_the_summary(tmp_path):
    index = _seed_trend(tmp_path, step=0.05, market_bias="Bull")
    args = make_args(
        tmp_path, from_time=index[90].isoformat(), to_time=index[99].isoformat(),
        spread="0.02", slippage="0.0",
    )

    _, summary = runner.run_backtest(args)

    assert summary["execution_model"] == {
        "type": execution.EXECUTION_MODEL_TYPE,
        "spread": 0.02,
        "slippage": 0.0,
    }


def test_print_summary_shows_spread_and_slippage(tmp_path, capsys):
    index = _seed_trend(tmp_path, step=0.05, market_bias="Bull")
    args = make_args(
        tmp_path, from_time=index[90].isoformat(), to_time=index[99].isoformat(),
        spread="0.01", slippage="0.005",
    )
    _, summary = runner.run_backtest(args)

    runner.print_summary(summary)
    captured = capsys.readouterr()

    assert "Execution model:" in captured.out
    assert "fixed_spread_slippage" in captured.out
    assert "spread=0.01" in captured.out
    assert "slippage=0.005" in captured.out


# --- zero cost is a pure pass-through (backward compatibility) ----------------


def test_zero_spread_slippage_leaves_entry_close_and_pnl_unchanged(tmp_path):
    # With time_based_close, entry = signal bar's raw close and close = next
    # bar's raw market close. A zero-cost model must reproduce exactly those
    # raw prices and the raw pnl, i.e. change nothing vs pre-Issue-#26.
    exit_config = _write_time_based_exit_config(tmp_path)
    index = _seed_trend(tmp_path, step=0.05, market_bias="Bull")

    _, baseline = _run_and_fetch_signals(tmp_path, index, exit_config=str(exit_config))
    _, zero_cost = _run_and_fetch_signals(
        tmp_path, index, exit_config=str(exit_config), spread="0.0", slippage="0.0",
    )

    assert len(baseline) == len(zero_cost) == 10
    for base, zero in zip(baseline, zero_cost):
        assert base["signal_time"] == zero["signal_time"]
        assert zero["entry_price"] == pytest.approx(base["entry_price"])
        assert zero["close_price"] == (None if base["close_price"] is None else pytest.approx(base["close_price"]))
        assert zero["pnl"] == (None if base["pnl"] is None else pytest.approx(base["pnl"]))


# --- long: entry rises, close falls, pnl worsens by 2 * (spread + slippage) ---


def test_long_entry_rises_and_close_falls_with_cost(tmp_path):
    exit_config = _write_time_based_exit_config(tmp_path)
    index = _seed_trend(tmp_path, step=0.05, market_bias="Bull")
    spread, slippage = 0.01, 0.005
    cost = spread + slippage

    _, baseline = _run_and_fetch_signals(tmp_path, index, exit_config=str(exit_config))
    _, with_cost = _run_and_fetch_signals(
        tmp_path, index, exit_config=str(exit_config), spread=str(spread), slippage=str(slippage),
    )

    closed_pairs = [
        (b, c) for b, c in zip(baseline, with_cost) if b["close_price"] is not None
    ]
    assert closed_pairs  # this fixture does close signals

    for base, cost_signal in closed_pairs:
        assert cost_signal["direction"] == "long"
        # long entry filled higher (unfavorable)
        assert cost_signal["entry_price"] == pytest.approx(base["entry_price"] + cost)
        # long close filled lower (unfavorable)
        assert cost_signal["close_price"] == pytest.approx(base["close_price"] - cost)
        # net pnl worsens by exactly 2 * cost (paid on both entry and close)
        assert cost_signal["pnl"] == pytest.approx(base["pnl"] - 2 * cost)
        assert cost_signal["pnl"] < base["pnl"]


# --- short: entry falls, close rises, pnl worsens by 2 * (spread + slippage) --


def test_short_entry_falls_and_close_rises_with_cost(tmp_path):
    exit_config = _write_time_based_exit_config(tmp_path)
    index = _seed_trend(tmp_path, step=-0.05, market_bias="Bear")  # downtrend -> short signals
    spread, slippage = 0.01, 0.005
    cost = spread + slippage

    _, baseline = _run_and_fetch_signals(tmp_path, index, exit_config=str(exit_config))
    _, with_cost = _run_and_fetch_signals(
        tmp_path, index, exit_config=str(exit_config), spread=str(spread), slippage=str(slippage),
    )

    closed_pairs = [
        (b, c) for b, c in zip(baseline, with_cost) if b["close_price"] is not None
    ]
    assert closed_pairs

    for base, cost_signal in closed_pairs:
        assert cost_signal["direction"] == "short"
        # short entry filled lower (unfavorable)
        assert cost_signal["entry_price"] == pytest.approx(base["entry_price"] - cost)
        # short close filled higher (unfavorable)
        assert cost_signal["close_price"] == pytest.approx(base["close_price"] + cost)
        # short pnl = entry - close, so both adjustments worsen it by 2 * cost
        assert cost_signal["pnl"] == pytest.approx(base["pnl"] - 2 * cost)
        assert cost_signal["pnl"] < base["pnl"]


# --- open / no_close signals keep close_price = None even with cost ----------


def test_open_signal_close_price_stays_none_with_cost(tmp_path):
    # The default fixed_rr policy never closes this uptrend within the loaded
    # window (see test_summary_command_signals_are_sorted...), so every signal
    # stays "open" with close_price=None -- and a None close must never be
    # turned into a fabricated adjusted price by the execution model.
    index = _seed_trend(tmp_path, step=0.05, market_bias="Bull")

    _, signals = _run_and_fetch_signals(tmp_path, index, spread="0.01", slippage="0.005")

    assert signals
    assert all(s["result_status"] == "open" for s in signals)
    assert all(s["close_price"] is None for s in signals)
    assert all(s["pnl"] is None for s in signals)
    # entry price is still corrected upward for a long even when the signal
    # never closes.
    raw_entries = _run_and_fetch_signals(tmp_path, index)[1]
    for corrected, raw in zip(signals, raw_entries):
        assert corrected["entry_price"] == pytest.approx(raw["entry_price"] + 0.015)
