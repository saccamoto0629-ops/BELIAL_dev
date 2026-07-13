"""Issue #49/#51: runner -- state flows, watermark idempotency, exit codes, dry-run."""

import pathlib
import sqlite3
import sys

import pandas as pd
import pytest

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR.parent / "backtest"))
sys.path.insert(0, str(BASE_DIR.parent / "market-watch"))

import execution_runner as runner  # noqa: E402
import execution_profile as profiles  # noqa: E402
import position_store as store  # noqa: E402

PROFILE = profiles.ExecutionProfile(max_entry_delay_bars=3, max_hold_bars=24, fallback_stop_pct=0.01, risk_reward=2.0)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(runner, "LOG_FILE", tmp_path / "logs" / "paper_execution.log")
    monkeypatch.setattr(runner, "_logger_instance", None, raising=False)
    monkeypatch.setattr(runner, "_console_only_logging", False, raising=False)
    runner._STOP_EVENT.clear()
    yield
    runner._STOP_EVENT.clear()


def _signal(signal_id="sig-1", side="long", status="paper_only", symbol="USDJPY", timeframe="1h",
            target="2026-07-06T12:00:00Z"):
    return {
        "signal_id": signal_id, "symbol": symbol, "timeframe": timeframe, "side": side,
        "strategy_profile": "market_bias_macd_alignment_v1", "target_candle_utc": target, "status": status,
    }


def _candles(rows, start="2026-07-06T12:00:00Z"):
    idx = pd.date_range(start, periods=len(rows), freq="1h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def _loader(df):
    return lambda symbol, timeframe: df


def _run(db, signals, candles, now, **kw):
    return runner.run_once(
        db_path=db, signals=signals, load_candles=_loader(candles), now_utc=now,
        profile=kw.pop("profile", PROFILE), chart_db_path=db.parent / "chart.sqlite3",
        signal_db_path=db.parent / "sig.sqlite3", **kw,
    )


NOW = pd.Timestamp("2026-07-06T18:00:00Z")


# --- state flows -------------------------------------------------------------

def test_pending_open_close_in_one_cycle(tmp_path):
    db = tmp_path / "p.sqlite3"
    candles = _candles([(50, 51, 49, 50), (100.0, 100.2, 99.9, 100.1), (100.5, 102.5, 100.1, 101.0)])
    res = _run(db, [_signal()], candles, NOW)
    assert res.failures == 0
    pos = store.list_positions(db_path=db)[0]
    assert pos["state"] == "closed"
    assert pos["terminal_reason"] == "take_profit"
    assert pos["pnl"] == pytest.approx(2.0)
    assert pos["entry_time_utc"] == "2026-07-06T13:00:00Z"   # next candle, not signal candle


def test_pending_then_expired(tmp_path):
    db = tmp_path / "p.sqlite3"
    # only a candle far past the 3-bar deadline (16:00) exists -> entry timeout.
    idx = pd.to_datetime(["2026-07-06T12:00:00Z", "2026-07-06T20:00:00Z"])
    candles = pd.DataFrame([(50, 51, 49, 50), (100, 101, 99, 100)],
                           columns=["open", "high", "low", "close"], index=idx)
    res = _run(db, [_signal()], candles, pd.Timestamp("2026-07-06T22:00:00Z"))
    assert res.failures == 0
    pos = store.list_positions(db_path=db)[0]
    assert pos["state"] == "expired" and pos["terminal_reason"] == "entry_timeout"


def test_invalid_signal_recorded_as_cancelled(tmp_path):
    db = tmp_path / "p.sqlite3"
    res = _run(db, [_signal(status="closed")], _candles([(50, 51, 49, 50)]), NOW)
    pos = store.list_positions(db_path=db)[0]
    assert pos["state"] == "cancelled" and pos["terminal_reason"] == "invalid_input"
    assert res.cancelled == 1


def test_bad_side_recorded_as_cancelled(tmp_path):
    db = tmp_path / "p.sqlite3"
    _run(db, [_signal(side="Neutral")], _candles([(50, 51, 49, 50)]), NOW)
    assert store.list_positions(db_path=db)[0]["state"] == "cancelled"


# --- watermark idempotency ---------------------------------------------------

def test_rerun_is_idempotent_and_watermark_advances(tmp_path):
    db = tmp_path / "p.sqlite3"
    quiet = _candles([(50, 51, 49, 50), (100.0, 100.2, 99.9, 100.1), (100.1, 100.3, 99.8, 100.0)])
    _run(db, [_signal()], quiet, NOW)
    pos = store.list_positions(db_path=db)[0]
    assert pos["state"] == "open"
    wm = pos["last_evaluated_candle_utc"]
    assert wm == "2026-07-06T14:00:00Z"

    # rerun with the SAME candles: no new confirmed candles, nothing changes.
    res2 = _run(db, [_signal()], quiet, NOW)
    assert res2.failures == 0
    pos2 = store.list_positions(db_path=db)[0]
    assert pos2["state"] == "open" and pos2["last_evaluated_candle_utc"] == wm


def test_past_candle_modification_does_not_reclose(tmp_path):
    db = tmp_path / "p.sqlite3"
    quiet = _candles([(50, 51, 49, 50), (100.0, 100.2, 99.9, 100.1), (100.1, 100.3, 99.8, 100.0)])
    _run(db, [_signal()], quiet, NOW)
    assert store.list_positions(db_path=db)[0]["state"] == "open"

    # a past, already-evaluated candle is retroactively rewritten to hit the stop,
    # with no NEW candle. The watermark must prevent re-evaluation.
    tampered = _candles([(50, 51, 49, 50), (100.0, 100.2, 98.0, 99.0), (100.1, 100.3, 98.0, 99.0)])
    _run(db, [_signal()], tampered, NOW)
    assert store.list_positions(db_path=db)[0]["state"] == "open"  # unchanged


def test_open_then_close_on_later_candle(tmp_path):
    db = tmp_path / "p.sqlite3"
    quiet = _candles([(50, 51, 49, 50), (100.0, 100.2, 99.9, 100.1), (100.1, 100.3, 99.8, 100.0)])
    _run(db, [_signal()], quiet, NOW)
    assert store.list_positions(db_path=db)[0]["state"] == "open"

    extended = _candles(
        [(50, 51, 49, 50), (100.0, 100.2, 99.9, 100.1), (100.1, 100.3, 99.8, 100.0),
         (100.2, 102.5, 100.1, 101.0)]
    )
    _run(db, [_signal()], extended, pd.Timestamp("2026-07-06T20:00:00Z"))
    pos = store.list_positions(db_path=db)[0]
    assert pos["state"] == "closed" and pos["terminal_reason"] == "take_profit"


# --- same_candle_priority via profile ---------------------------------------

@pytest.mark.parametrize("priority,expected", [("stop_loss", "stop_loss"), ("take_profit", "take_profit")])
def test_same_candle_priority_from_profile(tmp_path, priority, expected):
    db = tmp_path / "p.sqlite3"
    profile = profiles.ExecutionProfile(
        max_entry_delay_bars=3, max_hold_bars=24, fallback_stop_pct=0.01, risk_reward=2.0,
        same_candle_priority=priority,
    )
    # entry 100 (SL 99, TP 102); a candle spanning both.
    candles = _candles([(50, 51, 49, 50), (100.0, 100.1, 99.95, 100.0), (100.0, 102.5, 98.5, 101.0)])
    _run(db, [_signal()], candles, NOW, profile=profile)
    pos = store.list_positions(db_path=db)[0]
    assert pos["state"] == "closed" and pos["terminal_reason"] == expected


# --- failures -> non-zero ----------------------------------------------------

def test_signal_read_failure_counts_as_failure(tmp_path, monkeypatch):
    db = tmp_path / "p.sqlite3"
    monkeypatch.setattr(runner, "read_paper_signals", lambda p: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))
    res = runner.run_once(
        db_path=db, signals=None, load_candles=_loader(_candles([(1, 1, 1, 1)])), now_utc=NOW,
        profile=PROFILE, chart_db_path=db.parent / "c.sqlite3", signal_db_path=db.parent / "s.sqlite3",
    )
    assert res.failures >= 1


def test_write_failure_counts_as_failure(tmp_path, monkeypatch):
    db = tmp_path / "p.sqlite3"
    monkeypatch.setattr(store, "create_position", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("x")))
    res = _run(db, [_signal()], _candles([(50, 51, 49, 50), (100, 100, 100, 100)]), NOW)
    assert res.failures >= 1


def test_list_read_failure_counts_as_failure(tmp_path, monkeypatch):
    db = tmp_path / "p.sqlite3"
    monkeypatch.setattr(
        store, "list_positions_by_state",
        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("bad")),
    )
    res = _run(db, [], _candles([(1, 1, 1, 1)]), NOW)
    assert res.failures >= 1


# --- resident contract / exit codes -----------------------------------------

def test_run_service_once_returns_ok_on_clean_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "run_once", lambda **k: runner.CycleResult(failures=0))
    assert runner.run_service(once=True) == runner.EXIT_OK


def test_run_service_once_returns_failure_when_cycle_failed(monkeypatch):
    monkeypatch.setattr(runner, "run_once", lambda **k: runner.CycleResult(failures=2))
    assert runner.run_service(once=True) == runner.EXIT_FAILURE


def test_run_service_loop_stops_on_shutdown_event(monkeypatch):
    # pre-set the stop event: the loop body never runs, returns immediately.
    called = {"n": 0}
    monkeypatch.setattr(runner, "run_once", lambda **k: called.__setitem__("n", called["n"] + 1) or runner.CycleResult())
    runner._STOP_EVENT.set()
    assert runner.run_service(once=False, interval_seconds=0) == runner.EXIT_OK
    assert called["n"] == 0


# --- dry-run: no side effects ------------------------------------------------

def test_dry_run_creates_no_db_or_logs(tmp_path):
    db = tmp_path / "p.sqlite3"
    candles = _candles([(50, 51, 49, 50), (100.0, 100.2, 99.9, 100.1), (100.5, 102.5, 100.1, 101.0)])
    res = runner.run_once(
        dry_run=True, db_path=db, signals=[_signal()], load_candles=_loader(candles), now_utc=NOW,
        profile=PROFILE, chart_db_path=tmp_path / "c.sqlite3", signal_db_path=tmp_path / "s.sqlite3",
    )
    assert res.failures == 0
    assert res.pending_created == 1          # decision computed
    assert not db.exists()                   # but nothing persisted
    assert not (tmp_path / "logs").exists()  # console-only logging
