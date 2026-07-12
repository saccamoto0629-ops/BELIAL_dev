"""Issue #41 review Majors 1 & 3: a --dry-run has NO persistent side effects.

Major 1 (chart SQLite): exercised through the REAL chart fetch/build path
(get_chart_snapshot_summaries -> collect_and_build_snapshots /
build_snapshots_in_memory), with only the network leaf (fetch_ohlcv) and the
macro providers stubbed -- not by mocking fetch_snapshot_sources wholesale. A dry
run must neither create nor mutate the chart DB (existence / mtime / row count /
schema all unchanged); a normal run must still create/populate it.

Major 3 (logging + files): a real CLI subprocess dry run in an empty working
directory must leave the directory unchanged (no logs/, no *.sqlite3, no lock).
"""

import os
import pathlib
import sqlite3
import subprocess
import sys

import pandas as pd
import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import signal_generator as sg  # noqa: E402  (bootstraps news-feed-bot + market-watch on sys.path)
import chart_collector  # noqa: E402  (market-watch)
import chart_snapshot  # noqa: E402  (market-watch)
import market_snapshot as ms  # noqa: E402  (news-feed-bot)

EVAL_TIME = pd.Timestamp("2026-07-05T18:00:00Z").to_pydatetime()


def _synthetic_ohlcv():
    idx = pd.date_range("2026-06-01T00:00:00Z", periods=120, freq="1h", tz="UTC")
    base = [1.0 + i * 0.01 for i in range(len(idx))]
    return pd.DataFrame(
        {
            "open": base,
            "high": [b + 0.05 for b in base],
            "low": [b - 0.05 for b in base],
            "close": [b + 0.02 for b in base],
            "volume": [1000.0] * len(idx),
        },
        index=idx,
    )


@pytest.fixture
def offline_chart(monkeypatch, tmp_path):
    """Stub only the network leaves; keep the real chart fetch/build/persist path.

    Points the chart DB at a tmp file and restricts collection to one 1h target so
    the test is fast and hermetic, while still routing through the genuine chart
    provider (persistent vs in-memory) selected by run_once's dry_run flag.
    """
    chart_db = tmp_path / "chart_data.sqlite3"
    monkeypatch.setenv("CHART_DB_PATH", str(chart_db))
    monkeypatch.setenv("CHART_SYMBOLS", "USDJPY")
    monkeypatch.setenv("CHART_TIMEFRAMES", "1h")

    frame = _synthetic_ohlcv()
    # collect_target() (persist path) resolves fetch_ohlcv in chart_collector;
    # build_snapshots_in_memory() (dry-run path) resolves it in chart_snapshot.
    monkeypatch.setattr(chart_collector, "fetch_ohlcv", lambda target, period=chart_collector.DEFAULT_PERIOD: frame)
    monkeypatch.setattr(chart_snapshot, "fetch_ohlcv", lambda target, period=chart_snapshot.DEFAULT_PERIOD: frame)

    # No macro network I/O.
    monkeypatch.setattr(ms, "get_market_data", lambda *a, **k: {})
    monkeypatch.setattr(ms, "get_fred_data", lambda *a, **k: None)
    monkeypatch.setattr(ms, "get_tradingview_data", lambda *a, **k: {})
    if hasattr(ms, "get_economic_calendar"):
        monkeypatch.setattr(ms, "get_economic_calendar", lambda *a, **k: [])
    if hasattr(ms, "get_fedwatch"):
        monkeypatch.setattr(ms, "get_fedwatch", lambda *a, **k: None)
    return chart_db


def _schema(db_path):
    with sqlite3.connect(db_path) as conn:
        return sorted(conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall())


def _candle_rowcount(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0]


def test_dry_run_does_not_create_chart_db(offline_chart, tmp_path):
    chart_db = offline_chart
    paper_db = tmp_path / "paper_signals.sqlite3"

    sg.run_once(dry_run=True, db_path=paper_db, now_utc=EVAL_TIME)

    assert not chart_db.exists()   # in-memory chart path created no SQLite
    assert not paper_db.exists()   # dry-run persists no paper signal


def test_normal_run_creates_chart_db(offline_chart, tmp_path):
    # Contrast: the persistent path really does create/populate the chart DB, so
    # the dry-run test above is not vacuously passing.
    chart_db = offline_chart
    paper_db = tmp_path / "paper_signals.sqlite3"

    sg.run_once(dry_run=False, db_path=paper_db, now_utc=EVAL_TIME)

    assert chart_db.exists()
    assert _candle_rowcount(chart_db) > 0


def test_dry_run_does_not_mutate_existing_chart_db(offline_chart, tmp_path):
    chart_db = offline_chart
    paper_db = tmp_path / "paper_signals.sqlite3"

    # Establish a real chart DB via the persistent path.
    sg.run_once(dry_run=False, db_path=paper_db, now_utc=EVAL_TIME)
    assert chart_db.exists()
    before_mtime = chart_db.stat().st_mtime_ns
    before_rows = _candle_rowcount(chart_db)
    before_schema = _schema(chart_db)

    # A dry run must not touch it: existence, mtime, row count, schema unchanged.
    sg.run_once(dry_run=True, db_path=paper_db, now_utc=EVAL_TIME)

    assert chart_db.exists()
    assert chart_db.stat().st_mtime_ns == before_mtime
    assert _candle_rowcount(chart_db) == before_rows
    assert _schema(chart_db) == before_schema


# --- Major 3: CLI subprocess dry-run creates no files ------------------------


def _cli_subprocess(sandbox: pathlib.Path, args: str) -> subprocess.CompletedProcess:
    """Run paper_signal_bot's main() with all persistent outputs redirected into
    *sandbox* and synthetic in-memory sources, so the ONLY thing that could create
    a file in the sandbox is a persistence side effect we are testing for."""
    script = (
        "import sys, pathlib\n"
        "sandbox = pathlib.Path(sys.argv[1])\n"
        "import signal_generator as sg\n"
        "sg.LOGS_DIR = sandbox / 'logs'\n"
        "sg.LOG_FILE = sg.LOGS_DIR / 'paper_signal_generator.log'\n"
        "sg.DB_PATH = sandbox / 'paper_signals.sqlite3'\n"
        "snap = {'symbol':'USDJPY','timeframe':'1h','timestamp':'2026-06-05T12:00:00Z',"
        "'trend':'Bull','state':'Inside band','freshness':'Fresh','bias_weight':1.0,'macd_state':'Bullish'}\n"
        "sg.fetch_snapshot_sources = lambda **k: {'market':{}, 'fred':None, 'tradingview':{}, 'chart':[snap]}\n"
        "sg.classify_market_bias = lambda *a, **k: "
        "{'market_bias':'Bull','confidence':0.8,'scores':{},'reasons':[],'warnings':[],'note':''}\n"
        f"sg.main([{args}])\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BOT_DIR)
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.run(
        [sys.executable, "-c", script, str(sandbox)],
        capture_output=True, text=True, cwd=str(sandbox), env=env, timeout=60,
    )


def test_cli_dry_run_creates_no_files(tmp_path):
    sandbox = tmp_path / "run"
    sandbox.mkdir()
    before = sorted(os.listdir(sandbox))

    result = _cli_subprocess(sandbox, "'--once','--dry-run'")

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    after = sorted(os.listdir(sandbox))
    assert before == after == []  # no logs/, no *.sqlite3, no lock -- nothing


def test_cli_normal_run_writes_log_files(tmp_path):
    # Contrast: a normal (non-dry) --once run DOES create the log directory, so the
    # dry-run test above is meaningful, not vacuous.
    sandbox = tmp_path / "run"
    sandbox.mkdir()

    result = _cli_subprocess(sandbox, "'--once'")

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert (sandbox / "logs").exists()  # file logging created the redirected logs/ dir


def test_direct_run_once_dry_run_creates_no_log_file(tmp_path, monkeypatch):
    logs_dir = tmp_path / "logs"
    monkeypatch.setattr(sg, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(sg, "LOG_FILE", logs_dir / "paper_signal_generator.log")
    monkeypatch.setattr(
        sg,
        "fetch_snapshot_sources",
        lambda **_kwargs: {"market": {}, "fred": None, "tradingview": {}, "chart": []},
    )

    sg.run_once(dry_run=True, db_path=tmp_path / "paper.sqlite3", now_utc=EVAL_TIME)

    assert not logs_dir.exists()
