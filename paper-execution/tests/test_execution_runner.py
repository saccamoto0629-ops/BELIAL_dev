"""Issue #51: runner wiring -- open/close, idempotency, dry-run no side effects."""

import pathlib
import sys

import pandas as pd
import pytest

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR.parent / "backtest"))

import execution_runner as runner  # noqa: E402
import position_store as store  # noqa: E402

CONFIG = runner.ExecutionConfig(stop_loss_pct=0.01, risk_reward=2.0, time_exit_bars=24)


@pytest.fixture(autouse=True)
def _isolate_logging(tmp_path, monkeypatch):
    """Redirect logs into tmp and reset the cached logger so no test writes the repo logs/."""
    monkeypatch.setattr(runner, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(runner, "LOG_FILE", tmp_path / "logs" / "paper_execution.log")
    monkeypatch.setattr(runner, "_logger_instance", None, raising=False)
    monkeypatch.setattr(runner, "_console_only_logging", False, raising=False)
    yield


def _signal(signal_id="sig-1", side="long", symbol="USDJPY", timeframe="1h"):
    return {
        "signal_id": signal_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "side": side,
        "strategy_profile": "market_bias_macd_alignment_v1",
        "target_candle_utc": "2026-07-05T12:00:00Z",
    }


def _candles(rows, start="2026-07-05T12:00:00Z"):
    idx = pd.date_range(start, periods=len(rows), freq="1h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def _loader(df):
    return lambda symbol, timeframe: df


def test_long_open_and_take_profit_close(tmp_path):
    db = tmp_path / "pos.sqlite3"
    # T0 = entry candle (close 100); T1 hits the long target (102).
    candles = _candles([(100.0, 100.2, 99.8, 100.0), (100.5, 102.5, 100.1, 101.2)])
    touched = runner.run_once(
        signals=[_signal()], load_candles=_loader(candles),
        config=CONFIG, db_path=db, chart_db_path=tmp_path / "chart.sqlite3",
    )
    assert len(touched) == 1
    positions = store.list_positions(db_path=db)
    assert len(positions) == 1
    pos = positions[0]
    assert pos["side"] == "long"
    assert pos["status"] == "closed"
    assert pos["close_reason"] == "take_profit"
    assert pos["exit_price"] == pytest.approx(102.0)


def test_short_open_and_take_profit_close(tmp_path):
    db = tmp_path / "pos.sqlite3"
    candles = _candles([(100.0, 100.2, 99.8, 100.0), (99.5, 100.0, 97.5, 98.2)])
    runner.run_once(
        signals=[_signal(side="short")], load_candles=_loader(candles),
        config=CONFIG, db_path=db, chart_db_path=tmp_path / "chart.sqlite3",
    )
    pos = store.list_positions(db_path=db)[0]
    assert pos["side"] == "short"
    assert pos["close_reason"] == "take_profit"
    assert pos["exit_price"] == pytest.approx(98.0)


def test_open_condition_not_met_creates_no_position(tmp_path):
    db = tmp_path / "pos.sqlite3"
    candles = _candles([(100.0, 100.2, 99.8, 100.0)])
    runner.run_once(
        signals=[_signal(side="Neutral")], load_candles=_loader(candles),
        config=CONFIG, db_path=db, chart_db_path=tmp_path / "chart.sqlite3",
    )
    assert store.list_positions(db_path=db) == []


def test_rerun_is_idempotent_no_duplicate_open(tmp_path):
    db = tmp_path / "pos.sqlite3"
    candles = _candles([(100.0, 100.2, 99.8, 100.0), (100.5, 102.5, 100.1, 101.2)])
    kwargs = dict(
        signals=[_signal()], load_candles=_loader(candles),
        config=CONFIG, db_path=db, chart_db_path=tmp_path / "chart.sqlite3",
    )
    first = runner.run_once(**kwargs)
    second = runner.run_once(**kwargs)
    assert len(first) == 1
    assert second == []                       # nothing new on the second pass
    assert len(store.list_positions(db_path=db)) == 1   # still exactly one position
    # exactly one 'opened' + one 'closed' event -- history did not duplicate.
    pos = store.list_positions(db_path=db)[0]
    events = store.list_events(pos["position_id"], db_path=db)
    assert sorted(e["event_type"] for e in events) == ["closed", "opened"]


def test_open_stays_open_then_closes_on_later_candles(tmp_path):
    """A position left open by one cycle closes when a later cycle sees new candles."""
    db = tmp_path / "pos.sqlite3"
    # First cycle: only the entry bar + a quiet bar -> position opens, stays open.
    quiet = _candles([(100.0, 100.2, 99.8, 100.0), (100.1, 100.3, 99.9, 100.1)])
    runner.run_once(
        signals=[_signal()], load_candles=_loader(quiet),
        config=CONFIG, db_path=db, chart_db_path=tmp_path / "chart.sqlite3",
    )
    assert store.list_open_positions(db_path=db)[0]["status"] == "open"

    # Second cycle: same signal (skipped as already-open) but candles now include
    # a target hit -> the open position is re-evaluated and closed.
    extended = _candles(
        [(100.0, 100.2, 99.8, 100.0), (100.1, 100.3, 99.9, 100.1), (100.5, 102.5, 100.2, 101.5)]
    )
    runner.run_once(
        signals=[_signal()], load_candles=_loader(extended),
        config=CONFIG, db_path=db, chart_db_path=tmp_path / "chart.sqlite3",
    )
    pos = store.list_positions(db_path=db)[0]
    assert pos["status"] == "closed"
    assert pos["close_reason"] == "take_profit"


def test_dry_run_creates_no_db_and_no_logs(tmp_path):
    db = tmp_path / "pos.sqlite3"
    candles = _candles([(100.0, 100.2, 99.8, 100.0), (100.5, 102.5, 100.1, 101.2)])
    touched = runner.run_once(
        dry_run=True, signals=[_signal()], load_candles=_loader(candles),
        db_path=db, signal_db_path=tmp_path / "nope_signals.sqlite3",
        chart_db_path=tmp_path / "chart.sqlite3", config=CONFIG,
    )
    assert touched == []
    assert not db.exists()                 # no position DB created
    assert not (tmp_path / "logs").exists()  # console-only logging, no logs/ dir


def test_missing_target_candle_is_skipped(tmp_path):
    db = tmp_path / "pos.sqlite3"
    sig = _signal()
    del sig["target_candle_utc"]
    candles = _candles([(100.0, 100.2, 99.8, 100.0)])
    runner.run_once(
        signals=[sig], load_candles=_loader(candles),
        config=CONFIG, db_path=db, chart_db_path=tmp_path / "chart.sqlite3",
    )
    assert store.list_positions(db_path=db) == []
