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
import position_models as models  # noqa: E402
import position_store as store  # noqa: E402
import chart_collector  # noqa: E402

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
    assert pos["gross_pnl"] == pytest.approx(2.0)
    assert pos["net_pnl"] == pytest.approx(2.0)
    assert pos["pnl"] == pytest.approx(2.0)          # net alias
    assert pos["r_multiple"] == pytest.approx(2.0)
    assert pos["size"] == 1.0
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
    monkeypatch.setattr(
        store, "create_position_capacity_guarded",
        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("x")),
    )
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


# --- Blocker: Chart DB read failure is NOT treated as "no candles" -----------

def _seed_pending(db, signal_id="sig-1"):
    profile = PROFILE
    pos = models.create_pending_position(
        signal_id=signal_id, symbol="USDJPY", timeframe="1h", side="long", strategy_profile="p",
        profile=profile, signal_candle_utc="2026-07-06T12:00:00Z",
        entry_deadline_utc="2026-07-06T13:00:00Z",  # already past at NOW -> would-expire if empty
    )
    store.create_position(pos, db_path=db)
    return store.get_position(pos["position_id"], db_path=db)


def _broken_chart_db(tmp_path):
    """A real SQLite file that exists but has no `candles` table."""
    path = tmp_path / "broken_chart.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
    return path


def test_missing_candles_table_does_not_expire_pending(tmp_path):
    db = tmp_path / "p.sqlite3"
    seeded = _seed_pending(db)
    before_updated = seeded["updated_at_utc"]

    res = runner.run_once(
        db_path=db, signals=[], load_candles=None, now_utc=pd.Timestamp("2026-07-06T20:00:00Z"),
        profile=PROFILE, chart_db_path=_broken_chart_db(tmp_path), signal_db_path=tmp_path / "s.sqlite3",
    )
    assert res.failures >= 1                 # read failure counted, not success
    pos = store.get_position(seeded["position_id"], db_path=db)
    assert pos["state"] == "pending"         # NOT expired by an infra failure
    assert pos["terminal_reason"] is None
    assert pos["last_evaluated_candle_utc"] is None
    assert pos["updated_at_utc"] == before_updated   # untouched


def test_missing_chart_db_file_counts_as_failure(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    res = runner.run_once(
        db_path=db, signals=[], load_candles=None, now_utc=pd.Timestamp("2026-07-06T20:00:00Z"),
        profile=PROFILE, chart_db_path=tmp_path / "does_not_exist.sqlite3", signal_db_path=tmp_path / "s.sqlite3",
    )
    assert res.failures >= 1


def test_chart_read_failure_does_not_advance_open_watermark(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _seed_pending(db)
    # move it to open with a known watermark
    store.transition_to_open(
        pos["position_id"], entry_price=100.0, entry_time_utc="2026-07-06T13:00:00Z",
        stop_loss_price=99.0, take_profit_price=102.0, max_hold_deadline_utc="2026-07-07T13:00:00Z", db_path=db,
    )
    store.advance_watermark(pos["position_id"], expected_watermark=None, new_watermark="2026-07-06T14:00:00Z", db_path=db)
    before = store.get_position(pos["position_id"], db_path=db)

    res = runner.run_once(
        db_path=db, signals=[], load_candles=None, now_utc=pd.Timestamp("2026-07-06T20:00:00Z"),
        profile=PROFILE, chart_db_path=_broken_chart_db(tmp_path), signal_db_path=tmp_path / "s.sqlite3",
    )
    assert res.failures >= 1
    after = store.get_position(pos["position_id"], db_path=db)
    assert after["state"] == "open"
    assert after["last_evaluated_candle_utc"] == before["last_evaluated_candle_utc"]
    assert after["updated_at_utc"] == before["updated_at_utc"]


def test_dry_run_chart_read_failure_not_success(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    res = runner.run_once(
        dry_run=True, db_path=db, signals=[], load_candles=None,
        now_utc=pd.Timestamp("2026-07-06T20:00:00Z"), profile=PROFILE,
        chart_db_path=_broken_chart_db(tmp_path), signal_db_path=tmp_path / "s.sqlite3",
    )
    assert res.failures >= 1   # a read error is never a dry-run "success"


# --- strict chart read helper (real SQLite) ----------------------------------

def test_strict_read_empty_on_valid_zero_rows(tmp_path):
    chart = tmp_path / "chart.sqlite3"
    with sqlite3.connect(chart) as conn:
        conn.execute(
            "CREATE TABLE candles (symbol TEXT, timeframe TEXT, timestamp TEXT, "
            "open REAL, high REAL, low REAL, close REAL, volume REAL)"
        )
    df = chart_collector.load_candles_range_strict(chart, "USDJPY", "1h")
    assert df.empty                         # valid table, no rows -> empty (success)


def test_strict_read_raises_on_missing_file(tmp_path):
    with pytest.raises(chart_collector.ChartReadError):
        chart_collector.load_candles_range_strict(tmp_path / "nope.sqlite3", "USDJPY", "1h")


def test_strict_read_raises_on_missing_table(tmp_path):
    with pytest.raises(chart_collector.ChartReadError):
        chart_collector.load_candles_range_strict(_broken_chart_db(tmp_path), "USDJPY", "1h")


def test_lenient_load_candles_range_still_empty_on_error(tmp_path):
    # the existing lenient contract is preserved for other callers (Backtest).
    assert chart_collector.load_candles_range(tmp_path / "nope.sqlite3", "USDJPY", "1h").empty
    assert chart_collector.load_candles_range(_broken_chart_db(tmp_path), "USDJPY", "1h").empty


# --- Major: runner settles migration BEFORE reading any position -------------

_LEGACY_COLUMNS = [c for c in store._ALL_COLUMNS if c not in ("size", "gross_pnl", "net_pnl", "r_multiple")]
_TEXT_COLS = {
    "position_id", "signal_id", "symbol", "timeframe", "side", "strategy_profile", "execution_profile",
    "state", "terminal_reason", "signal_candle_utc", "entry_deadline_utc", "entry_time_utc",
    "max_hold_deadline_utc", "close_time_utc", "same_candle_priority", "last_evaluated_candle_utc",
    "created_at_utc", "updated_at_utc",
}


def _seed_legacy_db(db, row):
    """Create a constraint-less legacy positions table (no size/gross/net/r, user_version 0)."""
    def typ(c):
        return "TEXT" if c in _TEXT_COLS else ("INTEGER" if c in ("max_entry_delay_bars", "max_hold_bars") else "REAL")
    with sqlite3.connect(db) as conn:
        conn.execute(f"CREATE TABLE {store.POSITIONS_TABLE} ({', '.join(f'{c} {typ(c)}' for c in _LEGACY_COLUMNS)})")
        conn.execute("PRAGMA user_version = 0")
        cols = [c for c in _LEGACY_COLUMNS if c in row]
        conn.execute(
            f"INSERT INTO {store.POSITIONS_TABLE} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            tuple(row[c] for c in cols),
        )


def _legacy_row(**over):
    row = dict(
        position_id="old-1", signal_id="s-old", symbol="USDJPY", timeframe="1h", side="long",
        strategy_profile="p", execution_profile="default", state="pending", terminal_reason=None,
        signal_candle_utc="2026-07-06T12:00:00Z", entry_deadline_utc="2026-07-06T18:00:00Z",
        max_entry_delay_bars=3, max_hold_bars=24, same_candle_priority="stop_loss",
        fallback_stop_pct=0.01, risk_reward=2.0, spread=0.0, slippage=0.0, commission=0.0,
        created_at_utc="2026-07-06T12:00:00Z", updated_at_utc="2026-07-06T12:00:00Z",
    )
    row.update(over)
    return row


def _uv(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def _has_size(db):
    with sqlite3.connect(db) as conn:
        return "size" in {r[1] for r in conn.execute(f"PRAGMA table_info({store.POSITIONS_TABLE})")}


# ----- non-dry-run -----

def test_fresh_db_absent_then_runner_creates_and_runs(tmp_path):
    db = tmp_path / "p.sqlite3"
    res = _run(db, [], _candles([(1, 1, 1, 1)]), NOW)
    assert res.failures == 0
    assert db.exists() and _uv(db) == store.CURRENT_SCHEMA_VERSION


def test_legacy_db_no_signal_migrates_then_evaluates(tmp_path):
    # THE reported bug: legacy DB + existing pending + no new signal -> must migrate
    # up front, then read the pending as the new model (size present) and evaluate.
    db = tmp_path / "p.sqlite3"
    _seed_legacy_db(db, _legacy_row())
    assert _uv(db) == 0 and not _has_size(db)

    candles = _candles([(50, 51, 49, 50), (100.0, 100.2, 99.9, 100.1), (100.5, 102.5, 100.1, 101.0)])
    res = _run(db, [], candles, NOW)   # no new signals

    assert res.failures == 0
    assert _uv(db) == store.CURRENT_SCHEMA_VERSION and _has_size(db)
    pos = store.get_position("old-1", db_path=db)
    assert pos["size"] == 1.0
    assert pos["state"] == "closed"          # migrated pending was opened+closed on the candles
    assert pos["gross_pnl"] == pytest.approx(2.0)


def test_legacy_db_open_position_migrates_and_closes(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_legacy_db(db, _legacy_row(
        state="open", entry_price=100.0, entry_time_utc="2026-07-06T13:00:00Z",
        stop_loss_price=99.0, take_profit_price=102.0, max_hold_deadline_utc="2026-07-07T13:00:00Z",
    ))
    candles = _candles([(50, 51, 49, 50), (100.0, 100.1, 99.9, 100.0), (100.5, 102.5, 100.1, 101.0)])
    res = _run(db, [], candles, NOW)
    assert res.failures == 0
    pos = store.get_position("old-1", db_path=db)
    assert pos["state"] == "closed" and pos["size"] == 1.0
    assert pos["net_pnl"] == pytest.approx(2.0)


def test_migration_failure_aborts_before_position_read(tmp_path, monkeypatch):
    db = tmp_path / "p.sqlite3"
    calls = []
    monkeypatch.setattr(store, "ensure_schema_migrated",
                        lambda *a, **k: (_ for _ in ()).throw(store.PositionMigrationError("boom")))
    monkeypatch.setattr(store, "list_positions_by_state",
                        lambda *a, **k: calls.append(a) or [])
    res = _run(db, [], _candles([(1, 1, 1, 1)]), NOW)
    assert res.failures >= 1
    assert calls == []   # positions were never listed after a failed migration


def test_migration_failure_leaves_legacy_db_unchanged(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_legacy_db(db, _legacy_row(state="bogus"))   # invalid legacy row -> migration fails
    res = _run(db, [], _candles([(1, 1, 1, 1)]), NOW)
    assert res.failures >= 1
    assert _uv(db) == 0 and not _has_size(db)   # rolled back / untouched


# ----- dry-run -----

def test_dry_run_current_schema_no_write(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)                       # creates a CURRENT-schema DB
    before_mtime = db.stat().st_mtime_ns
    before_uv = _uv(db)
    candles = _candles([(50, 51, 49, 50), (100.0, 100.2, 99.9, 100.1), (100.5, 102.5, 100.1, 101.0)])
    res = runner.run_once(
        dry_run=True, db_path=db, signals=[], load_candles=_loader(candles), now_utc=NOW,
        profile=PROFILE, chart_db_path=tmp_path / "c.sqlite3", signal_db_path=tmp_path / "s.sqlite3",
    )
    assert res.failures == 0
    assert db.stat().st_mtime_ns == before_mtime and _uv(db) == before_uv
    assert store.list_positions(db_path=db)[0]["state"] == "pending"   # no transition


def test_dry_run_legacy_schema_reports_migration_required_and_does_not_touch_db(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_legacy_db(db, _legacy_row())
    before_mtime = db.stat().st_mtime_ns
    candles = _candles([(50, 51, 49, 50), (100.0, 100.2, 99.9, 100.1), (100.5, 102.5, 100.1, 101.0)])
    res = runner.run_once(
        dry_run=True, db_path=db, signals=[], load_candles=_loader(candles), now_utc=NOW,
        profile=PROFILE, chart_db_path=tmp_path / "c.sqlite3", signal_db_path=tmp_path / "s.sqlite3",
    )
    assert res.failures >= 1                          # "migration required"
    assert _uv(db) == 0 and not _has_size(db)         # no migration
    assert db.stat().st_mtime_ns == before_mtime      # mtime unchanged
    with sqlite3.connect(db) as conn:                 # legacy row untouched
        row = conn.execute(f"SELECT state, updated_at_utc FROM {store.POSITIONS_TABLE} WHERE position_id='old-1'").fetchone()
    assert row == ("pending", "2026-07-06T12:00:00Z")


def test_dry_run_missing_db_not_created(tmp_path):
    db = tmp_path / "absent.sqlite3"
    res = runner.run_once(
        dry_run=True, db_path=db, signals=[_signal()], load_candles=_loader(_candles([(1, 1, 1, 1)])),
        now_utc=NOW, profile=PROFILE, chart_db_path=tmp_path / "c.sqlite3", signal_db_path=tmp_path / "s.sqlite3",
    )
    assert res.failures == 0
    assert not db.exists()


# --- Major: invalid (non-SQLite) position DB is handled, never leaked ---------

def _write_invalid(db, kind):
    if kind == "bytes":
        db.write_bytes(bytes(range(256)) * 8)
    elif kind == "text":
        db.write_text("this is a text file, not a database\n" * 20)
    elif kind == "corrupt_header":
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE t (x)")
        data = bytearray(db.read_bytes())
        data[0:16] = b"XXXXXXXXXXXXXXXX"
        db.write_bytes(bytes(data))


def _run_invalid(db, tmp_path, *, dry_run):
    return runner.run_once(
        dry_run=dry_run, db_path=db, signals=[], load_candles=_loader(_candles([(1, 1, 1, 1)])),
        now_utc=NOW, profile=PROFILE, chart_db_path=tmp_path / "c.sqlite3", signal_db_path=tmp_path / "s.sqlite3",
    )


@pytest.mark.parametrize("kind", ["bytes", "text", "corrupt_header"])
def test_dry_run_invalid_db_fails_without_touching_file(tmp_path, kind, monkeypatch):
    db = tmp_path / "bad.sqlite3"
    _write_invalid(db, kind)
    before, mt, sz = db.read_bytes(), db.stat().st_mtime_ns, db.stat().st_size

    # spy: position read + migration helpers must NOT be called in dry-run.
    read_calls, init_calls = [], []
    monkeypatch.setattr(store, "list_positions_by_state", lambda *a, **k: read_calls.append(a) or [])
    monkeypatch.setattr(store, "init_db", lambda *a, **k: init_calls.append(a))
    monkeypatch.setattr(store, "ensure_schema_migrated", lambda *a, **k: init_calls.append(a))

    res = _run_invalid(db, tmp_path, dry_run=True)   # must not raise
    assert res.failures == 1
    assert read_calls == [] and init_calls == []
    assert db.read_bytes() == before and db.stat().st_mtime_ns == mt and db.stat().st_size == sz


@pytest.mark.parametrize("kind", ["bytes", "text", "corrupt_header"])
def test_non_dry_run_invalid_db_fails_without_overwrite(tmp_path, kind):
    db = tmp_path / "bad.sqlite3"
    _write_invalid(db, kind)
    before = db.read_bytes()
    res = _run_invalid(db, tmp_path, dry_run=False)   # must not raise
    assert res.failures == 1
    assert db.read_bytes() == before   # not overwritten / not recreated as fresh DB


def test_invalid_db_cli_exit_non_zero(tmp_path):
    db = tmp_path / "bad.sqlite3"
    _write_invalid(db, "bytes")
    assert runner.run_service(once=True, dry_run=True, db_path=db, profile=PROFILE) == runner.EXIT_FAILURE
    assert runner.run_service(once=True, dry_run=False, db_path=db, profile=PROFILE) == runner.EXIT_FAILURE
