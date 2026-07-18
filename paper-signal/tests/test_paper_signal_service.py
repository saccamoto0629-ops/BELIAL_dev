"""Issue #41: Paper Signal resident-service behaviour.

Covers the confirmed-candle discipline, Market Bias / Chart Snapshot input
integrity, candle-keyed idempotency, save-then-notify ordering, per-symbol
failure isolation, dry-run side-effect freedom, and the single-cycle (`--once`)
entrypoint -- all without ever starting the resident loop or depending on real
sleeping.
"""

import os
import pathlib
import signal as signal_module
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime

import pytest
import requests

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import paper_signal_store as store  # noqa: E402
import signal_generator as sg  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_chart_db(monkeypatch, tmp_path):
    # Issue #55: run_once consults the Chart DB for snapshot attachment; point
    # it at a nonexistent tmp path so no test ever reads or writes a real one.
    monkeypatch.setenv("CHART_DB_PATH", str(tmp_path / "chart_data.sqlite3"))


# A fixed evaluation time well after the fixture candle closes, so the candle is
# confirmed. The fixture candle opens at 12:00 (1h) -> closes 13:00 < 18:00.
EVAL_TIME = datetime(2026, 7, 5, 18, 0, 0, tzinfo=UTC)
CANDLE_TS = "2026-07-05T12:00:00Z"


def make_chart_snapshot(**overrides):
    snapshot = {
        "symbol": "USDJPY",
        "timeframe": "1h",
        "timestamp": CANDLE_TS,
        "trend": "Bull",
        "state": "Inside band",
        "freshness": "Fresh",
        "bias_weight": 1.0,
        "macd_state": "Bullish",
    }
    snapshot.update(overrides)
    return snapshot


def make_bias(market_bias="Bull", confidence=0.72, **overrides):
    bias = {"market_bias": market_bias, "confidence": confidence, "scores": {}, "reasons": [], "warnings": [], "note": ""}
    bias.update(overrides)
    return bias


def make_provenance(snapshot, **overrides):
    """Market Bias input provenance recorded at generation time, mirroring the
    snapshot's symbol/timeframe/candle/source id. Overridable to force integrity
    mismatches independently of the chart snapshot it is later compared against."""
    provenance = {
        "symbol": snapshot["symbol"],
        "timeframe": snapshot["timeframe"],
        "target_candle_utc": snapshot["timestamp"],
        "source_snapshot_id": snapshot.get("source_snapshot_id"),
    }
    provenance.update(overrides)
    return provenance


# --- Same-candle Market Bias + Chart Snapshot -> a signal --------------------


def test_matching_bias_and_snapshot_generate_signal():
    snapshot = make_chart_snapshot(macd_state="Bullish")
    candidate, result = sg.build_confirmed_candidate(
        snapshot, make_bias(), make_provenance(snapshot), evaluation_time=EVAL_TIME
    )
    assert result == sg.RESULT_SIGNAL
    assert candidate["side"] == "long"
    assert candidate["target_candle_utc"] == CANDLE_TS


def test_confirmed_candidate_carries_market_state_for_daily_reporting():
    snapshot = make_chart_snapshot(market_state="squeeze")
    candidate, result = sg.build_confirmed_candidate(
        snapshot, make_bias(), make_provenance(snapshot), evaluation_time=EVAL_TIME
    )
    assert result == sg.RESULT_SIGNAL
    assert candidate["market_state"] == "squeeze"


# --- Input integrity: missing / mismatched inputs are normal skips -----------


def test_missing_market_bias_is_skipped():
    snapshot = make_chart_snapshot()
    candidate, result = sg.build_confirmed_candidate(
        snapshot, None, make_provenance(snapshot), evaluation_time=EVAL_TIME
    )
    assert candidate is None
    assert result == sg.SKIP_MARKET_BIAS_MISSING


def test_missing_bias_provenance_is_skipped():
    # A bias with no recorded provenance cannot be integrity-checked -> skip.
    candidate, result = sg.build_confirmed_candidate(
        make_chart_snapshot(), make_bias(), None, evaluation_time=EVAL_TIME
    )
    assert candidate is None
    assert result == sg.SKIP_MARKET_BIAS_MISSING


def test_missing_chart_snapshot_is_skipped():
    snapshot = make_chart_snapshot()
    candidate, result = sg.build_confirmed_candidate(
        None, make_bias(), make_provenance(snapshot), evaluation_time=EVAL_TIME
    )
    assert candidate is None
    assert result == sg.SKIP_CHART_SNAPSHOT_MISSING


def test_snapshot_without_candle_timestamp_is_skipped():
    snapshot = make_chart_snapshot()
    provenance = make_provenance(snapshot)
    del snapshot["timestamp"]
    candidate, result = sg.build_confirmed_candidate(
        snapshot, make_bias(), provenance, evaluation_time=EVAL_TIME
    )
    assert candidate is None
    assert result == sg.SKIP_CHART_SNAPSHOT_MISSING


def test_symbol_mismatch_is_skipped():
    snapshot = make_chart_snapshot(symbol="USDJPY")
    provenance = make_provenance(snapshot, symbol="XAUUSD")
    candidate, result = sg.build_confirmed_candidate(snapshot, make_bias(), provenance, evaluation_time=EVAL_TIME)
    assert candidate is None
    assert result == sg.SKIP_SYMBOL_TIMEFRAME_MISMATCH


def test_timeframe_mismatch_is_skipped():
    snapshot = make_chart_snapshot(timeframe="1h")
    provenance = make_provenance(snapshot, timeframe="4h")
    candidate, result = sg.build_confirmed_candidate(snapshot, make_bias(), provenance, evaluation_time=EVAL_TIME)
    assert candidate is None
    assert result == sg.SKIP_SYMBOL_TIMEFRAME_MISMATCH


def test_target_candle_timestamp_mismatch_is_skipped():
    snapshot = make_chart_snapshot(timestamp="2026-07-05T12:00:00Z")
    provenance = make_provenance(snapshot, target_candle_utc="2026-07-05T11:00:00Z")
    candidate, result = sg.build_confirmed_candidate(snapshot, make_bias(), provenance, evaluation_time=EVAL_TIME)
    assert candidate is None
    assert result == sg.SKIP_TARGET_CANDLE_MISMATCH


def test_post_hoc_copying_chart_timestamp_onto_bias_does_not_pass():
    # Major 2: the bias's provenance candle (recorded at generation) is what is
    # compared -- NOT a value copied from the chart snapshot. Here the bias was
    # genuinely generated from candle 11:00 while the chart snapshot is 12:00.
    # Even though the chart says 12:00, integrity must fail (no silent agreement).
    snapshot = make_chart_snapshot(timestamp="2026-07-05T12:00:00Z")
    provenance = make_provenance(snapshot, target_candle_utc="2026-07-05T11:00:00Z")
    candidate, result = sg.build_confirmed_candidate(snapshot, make_bias(), provenance, evaluation_time=EVAL_TIME)
    assert candidate is None
    assert result == sg.SKIP_TARGET_CANDLE_MISMATCH


def test_source_snapshot_id_provenance_mismatch_is_skipped():
    # Both sides carry a source-dataset id but they identify different inputs.
    snapshot = make_chart_snapshot(source_snapshot_id="chart-snap-1")
    provenance = make_provenance(snapshot, source_snapshot_id="bias-snap-2")
    candidate, result = sg.build_confirmed_candidate(snapshot, make_bias(), provenance, evaluation_time=EVAL_TIME)
    assert candidate is None
    assert result == sg.SKIP_PROVENANCE_MISMATCH


# --- Confirmed-candle discipline: never act on a forming candle --------------


def test_unconfirmed_candle_is_skipped():
    # Candle opens 12:00, closes 13:00; evaluate exactly at the open -> forming.
    snapshot = make_chart_snapshot(timestamp="2026-07-05T12:00:00Z")
    forming_eval = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)
    candidate, result = sg.build_confirmed_candidate(
        snapshot, make_bias(), make_provenance(snapshot), evaluation_time=forming_eval
    )
    assert candidate is None
    assert result == sg.SKIP_UNCONFIRMED_CANDLE


def test_candle_confirmed_exactly_at_close_time():
    snapshot = make_chart_snapshot(timestamp="2026-07-05T12:00:00Z")
    at_close = datetime(2026, 7, 5, 13, 0, 0, tzinfo=UTC)  # T + 1h
    candidate, result = sg.build_confirmed_candidate(
        snapshot, make_bias(), make_provenance(snapshot), evaluation_time=at_close
    )
    assert result == sg.RESULT_SIGNAL


def test_unknown_timeframe_is_skipped_as_unconfirmed():
    snapshot = make_chart_snapshot(timeframe="unsupported")
    candidate, result = sg.build_confirmed_candidate(
        snapshot, make_bias(), make_provenance(snapshot), evaluation_time=EVAL_TIME
    )
    assert candidate is None
    assert result == sg.SKIP_UNCONFIRMED_CANDLE


# --- Idempotency: same target candle never stored / notified twice -----------


def test_save_signal_for_candle_is_idempotent(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    from signal_models import create_paper_signal

    candidate = sg.build_candidates(make_bias("Bull", 0.8), [make_chart_snapshot()])[0]
    first = store.save_signal_for_candle(create_paper_signal(candidate), target_candle_utc=CANDLE_TS, db_path=db_path)
    # A different signal_id but the SAME target candle == a concurrent/repeat run.
    second = store.save_signal_for_candle(create_paper_signal(candidate), target_candle_utc=CANDLE_TS, db_path=db_path)

    assert first is True
    assert second is False
    assert len(store.list_signals(db_path=db_path)) == 1


def test_truly_concurrent_duplicate_candles_yield_one_row(tmp_path):
    # Issue #41 review: a REAL race, not sequential inserts. Many threads, each on
    # its own SQLite connection (save_signal_for_candle opens one per call), try to
    # store the SAME (strategy, symbol, timeframe, target candle) with distinct
    # signal_ids at once. Exactly one insert wins; the DB holds exactly one row.
    import threading

    db_path = tmp_path / "paper_signals.sqlite3"
    from signal_models import create_paper_signal

    store.init_db(db_path)  # create schema up front so threads only contend on the write
    n = 12
    barrier = threading.Barrier(n)
    results: list[bool] = []
    results_lock = threading.Lock()

    def worker():
        signal = create_paper_signal(sg.build_candidates(make_bias("Bull", 0.8), [make_chart_snapshot()])[0])
        barrier.wait()  # release all threads as simultaneously as possible
        inserted = store.save_signal_for_candle(signal, target_candle_utc=CANDLE_TS, db_path=db_path)
        with results_lock:
            results.append(inserted)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1          # exactly one writer won
    assert results.count(False) == n - 1     # the rest were duplicate-skipped (no error)
    assert len(store.list_signals(db_path=db_path)) == 1  # one row in the DB


def test_run_once_duplicate_candle_notifies_only_once_under_repeat(tmp_path, monkeypatch):
    # Idempotency also means the notification fires once across repeated cycles.
    db_path = tmp_path / "paper_signals.sqlite3"
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda **kw: _bull_sources(make_chart_snapshot()))
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias("Bull", 0.8))
    posted = []
    monkeypatch.setattr(sg, "post_discord_message", lambda msg, dry_run=False, image_url=None: posted.append(msg))

    for _ in range(4):
        sg.run_once(dry_run=False, db_path=db_path, now_utc=EVAL_TIME)

    assert len(store.list_signals(db_path=db_path)) == 1
    assert len(posted) == 1


def _bull_sources(*snapshots):
    return {"market": {}, "fred": None, "tradingview": {}, "chart": list(snapshots)}


def test_run_once_persists_then_suppresses_duplicate_candle(tmp_path, monkeypatch):
    db_path = tmp_path / "paper_signals.sqlite3"
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda **kw: _bull_sources(make_chart_snapshot()))
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias("Bull", 0.8))

    posted = []
    monkeypatch.setattr(sg, "post_discord_message", lambda msg, dry_run=False, image_url=None: posted.append(msg))

    first = sg.run_once(dry_run=False, db_path=db_path, now_utc=EVAL_TIME)
    second = sg.run_once(dry_run=False, db_path=db_path, now_utc=EVAL_TIME)

    assert len(first) == 1
    assert second == []            # same target candle -> not re-created
    assert len(posted) == 1        # and therefore never re-notified


# --- Save-then-notify ordering + Discord failure retains the saved signal ----


def test_signal_is_saved_before_discord_is_notified(tmp_path, monkeypatch):
    db_path = tmp_path / "paper_signals.sqlite3"
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda **kw: _bull_sources(make_chart_snapshot()))
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias("Bull", 0.8))

    saved_at_notify_time = {}

    def check_db(msg, dry_run=False, image_url=None):
        # By the time Discord is notified, the signal must already be persisted.
        saved_at_notify_time["rows"] = store.list_signals(db_path=db_path)

    monkeypatch.setattr(sg, "post_discord_message", check_db)

    created = sg.run_once(dry_run=False, db_path=db_path, now_utc=EVAL_TIME)

    assert len(created) == 1
    assert len(saved_at_notify_time["rows"]) == 1  # saved BEFORE notify ran


def test_discord_failure_keeps_saved_signal(tmp_path, monkeypatch):
    db_path = tmp_path / "paper_signals.sqlite3"
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda **kw: _bull_sources(make_chart_snapshot()))
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias("Bull", 0.8))

    def boom(msg, dry_run=False, image_url=None):
        raise requests.RequestException("discord down")

    monkeypatch.setattr(sg, "post_discord_message", boom)

    created = sg.run_once(dry_run=False, db_path=db_path, now_utc=EVAL_TIME)

    # The signal is still created (returned) and still persisted despite the
    # notification failure -- never rolled back.
    assert len(created) == 1
    assert len(store.list_signals(db_path=db_path)) == 1


# --- One symbol failing must not stop the others -----------------------------


def test_one_symbol_failure_does_not_stop_other_symbols(tmp_path, monkeypatch):
    db_path = tmp_path / "paper_signals.sqlite3"
    usdjpy = make_chart_snapshot(symbol="USDJPY", macd_state="Bullish")
    xauusd = make_chart_snapshot(symbol="XAUUSD", macd_state="Bullish")
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda **kw: _bull_sources(usdjpy, xauusd))
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias("Bull", 0.8))
    monkeypatch.setattr(sg, "post_discord_message", lambda msg, dry_run=False, image_url=None: None)

    real_save = store.save_signal_for_candle

    def flaky_save(signal, *, target_candle_utc, db_path):
        if signal["symbol"] == "USDJPY":
            raise RuntimeError("simulated DB failure for USDJPY")
        return real_save(signal, target_candle_utc=target_candle_utc, db_path=db_path)

    monkeypatch.setattr(sg.store, "save_signal_for_candle", flaky_save)

    created = sg.run_once(dry_run=False, db_path=db_path, now_utc=EVAL_TIME)

    # USDJPY blew up, but XAUUSD was still processed and persisted.
    assert {c["symbol"] for c in created} == {"XAUUSD"}
    assert {row["symbol"] for row in store.list_signals(db_path=db_path)} == {"XAUUSD"}


def test_db_save_failure_does_not_notify_discord(tmp_path, monkeypatch):
    # Save-then-notify: if the DB save raises, Discord must NOT be called.
    db_path = tmp_path / "paper_signals.sqlite3"
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda **kw: _bull_sources(make_chart_snapshot()))
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias("Bull", 0.8))

    def failing_save(signal, *, target_candle_utc, db_path):
        raise RuntimeError("simulated DB save failure")

    monkeypatch.setattr(sg.store, "save_signal_for_candle", failing_save)

    posted = []
    monkeypatch.setattr(sg, "post_discord_message", lambda msg, dry_run=False, image_url=None: posted.append(msg))

    created = sg.run_once(dry_run=False, db_path=db_path, now_utc=EVAL_TIME)

    assert created == []       # nothing produced
    assert posted == []        # Discord was never called after a failed save


# --- Provenance is fixed at bias-generation, per symbol (no cross-mixing) -----


def test_bias_provenance_by_scope_records_each_symbols_own_candle():
    usdjpy = make_chart_snapshot(symbol="USDJPY", timestamp="2026-07-05T12:00:00Z")
    xauusd = make_chart_snapshot(symbol="XAUUSD", timestamp="2026-07-05T13:00:00Z")

    provenance = sg.build_bias_provenance_by_scope([usdjpy, xauusd])

    assert provenance[("USDJPY", "1h")]["target_candle_utc"] == "2026-07-05T12:00:00Z"
    assert provenance[("XAUUSD", "1h")]["target_candle_utc"] == "2026-07-05T13:00:00Z"
    # GLOBAL fallback carries no single input candle.
    assert provenance[("GLOBAL", "1h")] is None


def test_run_once_two_symbols_do_not_mix_candles(tmp_path, monkeypatch):
    # Two symbols on different confirmed candles: each signal is stored against its
    # OWN target candle -- a near-boundary difference never leaks across symbols.
    db_path = tmp_path / "paper_signals.sqlite3"
    usdjpy = make_chart_snapshot(symbol="USDJPY", timestamp="2026-07-05T12:00:00Z", macd_state="Bullish")
    xauusd = make_chart_snapshot(symbol="XAUUSD", timestamp="2026-07-05T13:00:00Z", macd_state="Bullish")
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda **kw: _bull_sources(usdjpy, xauusd))
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias("Bull", 0.8))
    monkeypatch.setattr(sg, "post_discord_message", lambda msg, dry_run=False, image_url=None: None)

    created = sg.run_once(dry_run=False, db_path=db_path, now_utc=datetime(2026, 7, 5, 18, tzinfo=UTC))

    by_symbol = {c["symbol"]: c["target_candle_utc"] for c in created}
    assert by_symbol["USDJPY"] == "2026-07-05T12:00:00Z"
    assert by_symbol["XAUUSD"] == "2026-07-05T13:00:00Z"
    rows = {r["symbol"]: r["target_candle_utc"] for r in store.list_signals(db_path=db_path)}
    assert rows == {"USDJPY": "2026-07-05T12:00:00Z", "XAUUSD": "2026-07-05T13:00:00Z"}


# --- dry-run: no DB, no Discord, no state ------------------------------------


def test_dry_run_has_no_persistent_side_effects(tmp_path, monkeypatch):
    db_path = tmp_path / "paper_signals.sqlite3"
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda **kw: _bull_sources(make_chart_snapshot()))
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias("Bull", 0.8))

    class FailingSession:
        def post(self, *args, **kwargs):
            raise AssertionError("no HTTP in dry-run")

    monkeypatch.setattr(sg.requests, "post", FailingSession().post)

    created = sg.run_once(dry_run=True, db_path=db_path, now_utc=EVAL_TIME)

    assert len(created) == 1          # the would-be signal is reported
    assert not db_path.exists()       # but nothing was persisted


# --- Time convention: UTC stored, UTC+JST displayed, derived from one source --


def test_discord_message_shows_target_candle_in_utc_and_jst():
    from signal_models import create_paper_signal

    candidate = sg.build_candidates(make_bias("Bull", 0.8), [make_chart_snapshot()])[0]
    candidate["target_candle_utc"] = "2026-07-05T12:00:00Z"
    signal = create_paper_signal(candidate)
    signal["target_candle_utc"] = candidate["target_candle_utc"]

    message = sg.format_discord_message(signal)

    # UTC is the canonical stored form; JST (UTC+9) is a derived display only.
    assert "Target Candle: 2026-07-05T12:00:00Z (2026-07-05 21:00 JST)" in message
    # The paper-only disclaimer is still last.
    assert message.rstrip().endswith("Paper signal only. No trade execution.")


def test_target_candle_timestamp_is_stored_in_utc(tmp_path, monkeypatch):
    db_path = tmp_path / "paper_signals.sqlite3"
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda **kw: _bull_sources(make_chart_snapshot()))
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias("Bull", 0.8))
    monkeypatch.setattr(sg, "post_discord_message", lambda msg, dry_run=False, image_url=None: None)

    sg.run_once(dry_run=False, db_path=db_path, now_utc=EVAL_TIME)

    rows = store.list_signals(db_path=db_path)
    assert rows[0]["target_candle_utc"] == CANDLE_TS
    assert rows[0]["created_at_utc"].endswith("Z")  # DB save time is UTC too


# --- --once / single-cycle entrypoint, and graceful stop flag ----------------


def test_run_service_once_runs_exactly_one_cycle(monkeypatch):
    calls = []
    monkeypatch.setattr(sg, "run_once", lambda **kwargs: calls.append(kwargs) or [])

    sg.run_service(once=True, dry_run=True)

    assert len(calls) == 1
    assert calls[0]["dry_run"] is True


def test_stop_event_prevents_further_cycles(monkeypatch):
    # A preset stop flag means the resident loop exits without running a cycle --
    # the mechanism SIGTERM/SIGINT use to shut the service down cleanly.
    calls = []
    monkeypatch.setattr(sg, "run_once", lambda **kwargs: calls.append(kwargs) or [])
    sg._STOP_EVENT.set()
    try:
        sg.run_service(once=False, interval_seconds=0)
    finally:
        sg._STOP_EVENT.clear()

    assert calls == []


# --- Signal handlers: really registered, and they interrupt the loop wait -----


def test_install_signal_handlers_registers_sigterm_and_sigint():
    original = {s: signal_module.getsignal(s) for s in (signal_module.SIGTERM, signal_module.SIGINT)}
    try:
        sg._install_signal_handlers()
        assert signal_module.getsignal(signal_module.SIGTERM) is sg._handle_shutdown_signal
        assert signal_module.getsignal(signal_module.SIGINT) is sg._handle_shutdown_signal
    finally:
        for s, handler in original.items():
            signal_module.signal(s, handler)


def test_shutdown_signal_interrupts_the_loop_wait(monkeypatch):
    # The loop waits on _STOP_EVENT (not a bare sleep), so delivering the shutdown
    # signal wakes it immediately instead of blocking for the full interval.
    sg._STOP_EVENT.clear()
    cycles = []
    monkeypatch.setattr(sg, "run_once", lambda **kw: cycles.append(1) or [])

    # A deliberately long interval: the test must finish only because the wait is
    # interrupted, never because the interval elapsed.
    thread = threading.Thread(target=sg.run_service, kwargs={"once": False, "interval_seconds": 3600})
    thread.start()
    try:
        deadline = time.time() + 5
        while not cycles and time.time() < deadline:
            time.sleep(0.01)
        # Simulate signal delivery on the main thread.
        sg._handle_shutdown_signal(signal_module.SIGTERM, None)
        thread.join(timeout=5)
        assert not thread.is_alive()  # woke from the 3600s wait at once
    finally:
        sg._STOP_EVENT.set()
        thread.join(timeout=5)
        sg._STOP_EVENT.clear()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery to a child process")
def test_sigterm_terminates_the_resident_subprocess(tmp_path):
    # End-to-end: a real child running the resident loop must exit on SIGTERM
    # (not be force-killed) and leave no orphan. run_once is stubbed to a no-op so
    # the child does no network I/O and reaches the interruptible wait quickly.
    script = (
        "import signal_generator as sg\n"
        "sg.run_once = lambda **k: []\n"
        "print('READY', flush=True)\n"
        "sg.main([])\n"  # resident loop (no --once)
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BOT_DIR)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(tmp_path),
        env=env,
    )
    try:
        # Wait for the child to be up and (about to be) inside the loop wait.
        deadline = time.time() + 15
        while time.time() < deadline and (proc.stdout.readline().strip() != "READY"):
            pass
        time.sleep(0.5)
        proc.send_signal(signal_module.SIGTERM)
        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    assert proc.returncode == 0  # graceful shutdown, not SIGKILL
