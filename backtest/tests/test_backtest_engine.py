"""Issue #25: fixture-driven regression tests for the Backtest Engine's main paths.

Every test drives the public entry point (runner.run_backtest()) end-to-end
against a tiny OHLCV fixture: a one-line-describable 90-bar uptrend warm-up
(candle_fixtures.make_uptrend_candles) followed by a few hand-written
(open, high, low, close) validation bars whose price story carries the
test's intent. All SQLite files live under tmp_path -- no real data/
directory is ever read or written -- and the autouse guard below fails any
test that tries to reach the network (Discord/webhooks/HTTP).

Paths pinned here:
- a validation bar that generates a signal (and 0-future-bars stays "open")
- a run that generates no signal (Bull bias below the confidence threshold;
  a normal outcome, not an error)
- an exit reached at the take-profit (fixed_rr -> "win") and at the stop
  (fixed_rr -> "loss")
- an invalidation reached (ma_break_invalidation -> "invalidated")

Two Issue #25 paths already have dedicated regression tests and are
deliberately not duplicated here:
- warm-up-insufficient skip:
  test_warmup.py::test_warmup_insufficient_skips_signal_generation_for_every_validation_bar,
  test_warmup.py::test_warmup_insufficient_flag_persists_even_after_indicators_naturally_warm_up,
  test_runner.py::test_run_backtest_flags_insufficient_warmup_and_generates_no_signals
- bars after validation_end never counting as warm-up (or results):
  test_warmup.py::test_split_warmup_and_validation_excludes_future_rows_from_both_frames,
  test_warmup.py::test_split_warmup_and_validation_future_rows_do_not_inflate_warmup_frame_size,
  test_warmup.py::test_evaluate_backtest_bars_future_rows_do_not_hide_warmup_insufficient,
  test_runner.py::test_run_backtest_excludes_candles_after_to
"""

import json
import pathlib
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
from candle_fixtures import (  # noqa: E402
    make_ohlcv_candles,
    make_uptrend_candles,
    save_candles,
    seed_bull_bias,
)

WARMUP_BARS = 90  # comfortably above DEFAULT_WARMUP_BARS (75); keeps sma75 warm

# The 90-bar warm-up uptrend ends with a close of 154.50. Every validation
# fixture below hangs off that price.
#
# ENTRY_BAR continues the trend and closes at 154.55 -> Bull bias + Bullish
# MACD -> one long paper signal entering at 154.55. With fixed_rr's default
# config (fallback_stop_pct=0.003, risk_reward=2.0) that entry implies:
#   stop        = 154.55 * 0.997 = 154.08635
#   take-profit = 154.55 * 1.006 = 155.4773
ENTRY_PRICE = 154.55
ENTRY_BAR = (154.50, 154.60, 154.45, ENTRY_PRICE)
TP_HIT_BAR = (154.60, 155.60, 154.40, 155.50)  # high pierces 155.477; low stays above the stop
STOP_HIT_BAR = (154.40, 154.50, 153.90, 154.00)  # low pierces 154.086; high stays below the target
FLAT_BAR = (154.55, 154.60, 154.50, 154.55)  # goes nowhere; touches neither stop nor target
MA_BREAK_BAR = (154.40, 154.60, 153.90, 154.00)  # closes well below the 5-bar MA (~154.44)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if anything in this file tries to reach the network."""
    import requests

    def _boom(*args, **kwargs):
        raise AssertionError("Backtest engine tests must never make a network call")

    monkeypatch.setattr(requests, "post", _boom)
    monkeypatch.setattr(requests, "get", _boom)
    monkeypatch.setattr(requests, "request", _boom)
    monkeypatch.setattr(requests.Session, "request", _boom)


def seed_engine_fixture(tmp_path, validation_bars, *, confidence=0.8):
    """Warm-up uptrend + hand-written validation bars, all under tmp_path.

    Returns (chart_db, bias_db, validation_index).
    """
    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    warmup_index, warmup_candles = make_uptrend_candles(WARMUP_BARS)
    validation_start = warmup_index[-1] + pd.Timedelta(hours=1)
    validation_index, validation_candles = make_ohlcv_candles(validation_bars, start=validation_start)
    save_candles(chart_db, warmup_candles + validation_candles)
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(warmup_index[0]), confidence=confidence)
    return chart_db, bias_db, validation_index


def run_engine(tmp_path, chart_db, bias_db, validation_index, *, extra_args=()):
    """Run the engine over exactly the hand-written validation window."""
    backtest_db = tmp_path / "backtest_results.sqlite3"
    args = runner.parse_args(
        [
            "run",
            "--symbol", "USD_JPY",
            "--timeframe", "1h",
            "--from", validation_index[0].isoformat(),
            "--to", validation_index[-1].isoformat(),
            "--db-path", str(chart_db),
            "--backtest-db-path", str(backtest_db),
            "--market-bias-db-path", str(bias_db),
            *extra_args,
        ]
    )
    exit_code, result = runner.run_backtest(args)
    return exit_code, result, backtest_db


# =============================================================================
# signal generated
# =============================================================================


def test_a_trend_continuation_bar_generates_one_long_signal(tmp_path):
    chart_db, bias_db, validation_index = seed_engine_fixture(tmp_path, [ENTRY_BAR])

    exit_code, result, backtest_db = run_engine(tmp_path, chart_db, bias_db, validation_index)

    assert exit_code == runner.EXIT_OK
    assert result["bars_processed"] == 1
    assert result["signals_generated"] == 1
    signals = store.list_backtest_signals(result["run_id"], db_path=backtest_db)
    assert len(signals) == 1
    signal = signals[0]
    assert signal["direction"] == "long"
    assert signal["signal_time"] == validation_index[0].isoformat()
    assert signal["entry_price"] == pytest.approx(ENTRY_PRICE)
    # the only bar in the window: nothing after it to walk, so it stays open
    # with no fabricated pnl
    assert signal["result_status"] == "open"
    assert signal["close_reason"] == "no_close"
    assert signal["pnl"] is None


# =============================================================================
# no signal generated (a normal outcome, not an error)
# =============================================================================


def test_bull_bias_below_the_confidence_threshold_generates_no_signals(tmp_path):
    # Identical candles to the signal case -- only the persisted bias
    # confidence differs (0.2 < the 0.40 default threshold), so zero signals
    # is the expected outcome and the run still completes normally.
    chart_db, bias_db, validation_index = seed_engine_fixture(tmp_path, [ENTRY_BAR], confidence=0.2)

    exit_code, result, backtest_db = run_engine(tmp_path, chart_db, bias_db, validation_index)

    assert exit_code == runner.EXIT_OK
    assert result["bars_processed"] == 1
    assert result["signals_generated"] == 0
    assert store.get_backtest_run(result["run_id"], db_path=backtest_db) is not None  # run row still saved
    assert store.list_backtest_signals(result["run_id"], db_path=backtest_db) == []
    assert result["signal_summary"]["total_signals"] == 0


# =============================================================================
# exit reached: fixed_rr take-profit (win) and stop (loss)
# =============================================================================


def test_exit_reached_at_the_take_profit_records_a_win(tmp_path):
    chart_db, bias_db, validation_index = seed_engine_fixture(tmp_path, [ENTRY_BAR, TP_HIT_BAR])

    exit_code, result, backtest_db = run_engine(tmp_path, chart_db, bias_db, validation_index)

    assert exit_code == runner.EXIT_OK
    signals = store.list_backtest_signals(result["run_id"], db_path=backtest_db)
    entry_signal = signals[0]  # ordered by signal_time ASC
    assert entry_signal["signal_time"] == validation_index[0].isoformat()
    assert entry_signal["result_status"] == "win"
    assert entry_signal["close_reason"] == "take_profit"
    assert entry_signal["close_price"] == pytest.approx(ENTRY_PRICE * 1.006)
    assert entry_signal["pnl"] == pytest.approx(ENTRY_PRICE * 0.006)
    assert entry_signal["closed_at"] == validation_index[1].isoformat()


def test_exit_reached_at_the_stop_records_a_loss(tmp_path):
    chart_db, bias_db, validation_index = seed_engine_fixture(tmp_path, [ENTRY_BAR, STOP_HIT_BAR])

    exit_code, result, backtest_db = run_engine(tmp_path, chart_db, bias_db, validation_index)

    assert exit_code == runner.EXIT_OK
    signals = store.list_backtest_signals(result["run_id"], db_path=backtest_db)
    entry_signal = signals[0]
    assert entry_signal["signal_time"] == validation_index[0].isoformat()
    assert entry_signal["result_status"] == "loss"
    assert entry_signal["close_reason"] == "stop_loss"
    assert entry_signal["close_price"] == pytest.approx(ENTRY_PRICE * 0.997)
    assert entry_signal["pnl"] == pytest.approx(-ENTRY_PRICE * 0.003)
    assert entry_signal["closed_at"] == validation_index[1].isoformat()


# =============================================================================
# invalidation reached: ma_break_invalidation
# =============================================================================


def test_invalidation_reached_when_price_closes_below_the_ma(tmp_path):
    # Price story: entry at 154.55, five flat bars hold the 5-bar MA at
    # ~154.55, then MA_BREAK_BAR closes at 154.00 -- below the MA over the
    # last 5 closes ((154.55 * 4 + 154.00) / 5 = 154.44) -- so the long
    # signal's thesis is invalidated there, at that bar's close.
    exit_config_path = tmp_path / "exit_config.json"
    exit_config_path.write_text(
        json.dumps(
            {
                "exit_policy": "ma_break_invalidation",
                "policies": {"ma_break_invalidation": {"enabled": True, "ma_type": "sma", "period": 5}},
            }
        ),
        encoding="utf-8",
    )
    bars = [ENTRY_BAR] + [FLAT_BAR] * 5 + [MA_BREAK_BAR]
    chart_db, bias_db, validation_index = seed_engine_fixture(tmp_path, bars)

    exit_code, result, backtest_db = run_engine(
        tmp_path, chart_db, bias_db, validation_index,
        extra_args=("--exit-config", str(exit_config_path)),
    )

    assert exit_code == runner.EXIT_OK
    run = store.get_backtest_run(result["run_id"], db_path=backtest_db)
    assert run["exit_policy"] == "ma_break_invalidation"
    signals = store.list_backtest_signals(result["run_id"], db_path=backtest_db)
    entry_signal = signals[0]
    assert entry_signal["signal_time"] == validation_index[0].isoformat()
    assert entry_signal["result_status"] == "invalidated"
    assert entry_signal["close_reason"] == "ma_break"
    assert entry_signal["exit_policy"] == "ma_break_invalidation"
    assert entry_signal["close_price"] == pytest.approx(154.00)
    assert entry_signal["closed_at"] == validation_index[-1].isoformat()
