import json
import pathlib
import sqlite3
import sys

import pandas as pd
import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = BOT_DIR.parent
MARKET_WATCH_DIR = REPO_ROOT / "market-watch"
NEWS_FEED_BOT_DIR = REPO_ROOT / "news-feed-bot"

for _dir in (BOT_DIR, MARKET_WATCH_DIR, NEWS_FEED_BOT_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import runner  # noqa: E402
import backtest_store as store  # noqa: E402
import chart_collector as collector  # noqa: E402  (market-watch)
from chart_models import Candle  # noqa: E402  (market-watch)
import market_bias_store as bias_store  # noqa: E402  (news-feed-bot)


def make_uptrend_candles(n, *, start="2026-05-27T00:00:00Z", freq="1h", chart_symbol="USDJPY", timeframe="1h", step=0.05):
    """A steady uptrend: after warm-up, this is Bull/Bullish MACD -- matches
    backtest/tests/test_warmup.py's fixture shape, so it reliably produces
    'long' candidates once ready.
    """
    index = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    candles = []
    price = 150.0
    for timestamp in index:
        price += step
        candles.append(
            Candle(
                symbol=chart_symbol,
                timeframe=timeframe,
                timestamp=collector.to_utc_iso_z(timestamp),
                open=price - 0.02,
                high=price + 0.05,
                low=price - 0.05,
                close=price,
                volume=100.0,
            )
        )
    return index, candles


def seed_chart_db(db_path, *, n=100, chart_symbol="USDJPY", timeframe="1h", step=0.05):
    index, candles = make_uptrend_candles(n, chart_symbol=chart_symbol, timeframe=timeframe, step=step)
    collector.save_candles(db_path, candles)
    return index


def seed_bull_bias(db_path, *, evaluated_at, market_bias="Bull", confidence=0.8, bias_id="mb-1"):
    bias_store.save_market_bias(
        {
            "market_bias_id": bias_id,
            "evaluated_at": evaluated_at,
            "symbol": "GLOBAL",
            "timeframe": "1h",
            "market_bias": market_bias,
            "confidence": confidence,
            "trend": market_bias,
        },
        db_path=db_path,
    )


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
