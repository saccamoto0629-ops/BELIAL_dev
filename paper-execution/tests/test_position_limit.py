"""Issue #85: concurrent-position cap per (symbol, timeframe, side).

Fixes the acceptance criteria of Issue #85:

- same-scope pending / open blocks a new same-side signal (recorded as a
  terminal ``cancelled`` / ``position_limit`` position, never a silent skip)
- the opposite side has its own independent scope (never blocked by this one)
- cap boundary: below / at / above the limit; ``None`` = unlimited (pre-#85)
- terminal positions (closed / expired / cancelled) never block intake
- idempotency: same-signal re-import, same-cycle re-run, concurrent runners --
  no duplicate positions, no cap overshoot, no duplicate notifications
- dry-run projects the same decisions with zero side effects
- schema v4 migration: the cancelled CHECK gains ``position_limit`` via the
  transactional positions-table rebuild (v3 rows preserved, rollback on failure)
"""

import pathlib
import sqlite3
import sys
import threading

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

PROFILE = profiles.ExecutionProfile(max_entry_delay_bars=3, max_hold_bars=24, fallback_stop_pct=0.01, risk_reward=2.0)
NOW = pd.Timestamp("2026-07-06T18:00:00Z")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(runner, "LOG_FILE", tmp_path / "logs" / "paper_execution.log")
    monkeypatch.setattr(runner, "_logger_instance", None, raising=False)
    monkeypatch.setattr(runner, "_console_only_logging", False, raising=False)
    runner._STOP_EVENT.clear()
    yield
    runner._STOP_EVENT.clear()


def _pending(signal_id="sig-1", symbol="USDJPY", timeframe="1h", side="long"):
    return models.create_pending_position(
        signal_id=signal_id, symbol=symbol, timeframe=timeframe, side=side,
        strategy_profile="p", profile=PROFILE,
        signal_candle_utc="2026-07-06T12:00:00Z", entry_deadline_utc="2026-07-06T16:00:00Z",
    )


def _guarded(db, record, limit=1):
    return store.create_position_capacity_guarded(record, max_active_per_side=limit, db_path=db)


def _open_it(db, pos):
    store.transition_to_open(
        pos["position_id"], entry_price=100.0, entry_time_utc="2026-07-06T13:00:00Z",
        stop_loss_price=99.0, take_profit_price=102.0, max_hold_deadline_utc="2026-07-07T13:00:00Z", db_path=db,
    )


def _states(db):
    return sorted((p["state"], p["terminal_reason"]) for p in store.list_positions(db_path=db))


def _outbox(db):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            f"SELECT event_key FROM {store.NOTIFICATIONS_TABLE} ORDER BY event_key"
        ).fetchall()


# --- store: same side pending / open blocks -----------------------------------

def test_same_scope_pending_blocks_same_side(tmp_path):
    db = tmp_path / "p.sqlite3"
    assert _guarded(db, _pending("a"))[0] == store.CAPACITY_RESULT_PENDING
    outcome, refused = _guarded(db, _pending("b"))
    assert outcome == store.CAPACITY_RESULT_CANCELLED
    assert refused["state"] == "cancelled" and refused["terminal_reason"] == "position_limit"
    stored = store.get_position_by_signal("b", db_path=db)
    assert stored["state"] == "cancelled" and stored["terminal_reason"] == "position_limit"
    # the refusal is recorded WITH its cancelled outbox row (Issue #83 atomicity)
    assert _outbox(db) == [(f"{refused['position_id']}:cancelled",)]


def test_same_scope_open_blocks_same_side(tmp_path):
    db = tmp_path / "p.sqlite3"
    first = _pending("a")
    _guarded(db, first)
    _open_it(db, first)
    outcome, refused = _guarded(db, _pending("b"))
    assert outcome == store.CAPACITY_RESULT_CANCELLED
    assert refused["terminal_reason"] == "position_limit"


# --- store: opposite side is an independent scope -----------------------------

def test_opposite_side_pending_does_not_block(tmp_path):
    db = tmp_path / "p.sqlite3"
    _guarded(db, _pending("a", side="short"))
    assert _guarded(db, _pending("b", side="long"))[0] == store.CAPACITY_RESULT_PENDING


def test_opposite_side_open_does_not_block(tmp_path):
    db = tmp_path / "p.sqlite3"
    first = _pending("a", side="short")
    _guarded(db, first)
    _open_it(db, first)
    assert _guarded(db, _pending("b", side="long"))[0] == store.CAPACITY_RESULT_PENDING


def test_other_symbol_or_timeframe_does_not_block(tmp_path):
    db = tmp_path / "p.sqlite3"
    _guarded(db, _pending("a"))
    assert _guarded(db, _pending("b", symbol="EURUSD"))[0] == store.CAPACITY_RESULT_PENDING
    assert _guarded(db, _pending("c", timeframe="4h"))[0] == store.CAPACITY_RESULT_PENDING


# --- store: cap boundary / unlimited ------------------------------------------

def test_cap_boundary_below_at_above(tmp_path):
    db = tmp_path / "p.sqlite3"
    assert _guarded(db, _pending("a"), limit=2)[0] == store.CAPACITY_RESULT_PENDING   # 0 -> 1 (below)
    assert _guarded(db, _pending("b"), limit=2)[0] == store.CAPACITY_RESULT_PENDING   # 1 -> 2 (boundary)
    assert _guarded(db, _pending("c"), limit=2)[0] == store.CAPACITY_RESULT_CANCELLED  # at cap
    assert _guarded(db, _pending("d"), limit=2)[0] == store.CAPACITY_RESULT_CANCELLED  # still at cap
    assert _states(db) == [
        ("cancelled", "position_limit"), ("cancelled", "position_limit"), ("pending", None), ("pending", None),
    ]


def test_unlimited_none_preserves_per_signal_behaviour(tmp_path):
    db = tmp_path / "p.sqlite3"
    for signal_id in ("a", "b", "c", "d"):
        assert _guarded(db, _pending(signal_id), limit=None)[0] == store.CAPACITY_RESULT_PENDING
    assert len(store.list_positions(db_path=db)) == 4


def test_guard_rejects_non_pending_record_and_bad_limit(tmp_path):
    db = tmp_path / "p.sqlite3"
    cancelled = dict(_pending("x"), state="cancelled", terminal_reason="manual")
    with pytest.raises(ValueError):
        store.create_position_capacity_guarded(cancelled, max_active_per_side=1, db_path=db)
    with pytest.raises(ValueError):
        _guarded(db, _pending("y"), limit=0)


# --- store: terminal positions never block ------------------------------------

def test_terminal_positions_do_not_block(tmp_path):
    db = tmp_path / "p.sqlite3"
    # closed
    first = _pending("a")
    _guarded(db, first)
    _open_it(db, first)
    store.close_open_position(
        first["position_id"], expected_watermark=None, new_watermark="2026-07-06T14:00:00Z",
        close_price=102.0, close_time_utc="2026-07-06T14:00:00Z", terminal_reason="take_profit",
        gross_pnl=2.0, net_pnl=2.0, r_multiple=2.0, db_path=db,
    )
    # expired
    second = _pending("b")
    _guarded(db, second)
    store.transition_pending_terminal(second["position_id"], state="expired", terminal_reason="entry_timeout", db_path=db)
    # cancelled
    third = _pending("c")
    _guarded(db, third)
    store.transition_pending_terminal(third["position_id"], state="cancelled", terminal_reason="manual", db_path=db)
    # all three are terminal -> a new same-scope signal is admitted at limit 1
    assert _guarded(db, _pending("d"))[0] == store.CAPACITY_RESULT_PENDING


def test_count_active_positions_scope_and_missing_db(tmp_path):
    db = tmp_path / "p.sqlite3"
    assert store.count_active_positions("USDJPY", "1h", "long", db_path=db) == 0   # missing DB, not created
    assert not db.exists()
    first = _pending("a")
    _guarded(db, first)
    _guarded(db, _pending("b", side="short"))
    _open_it(db, first)
    third = _pending("c", symbol="EURUSD")
    _guarded(db, third)
    store.transition_pending_terminal(third["position_id"], state="expired", terminal_reason="entry_timeout", db_path=db)
    assert store.count_active_positions("USDJPY", "1h", "long", db_path=db) == 1   # the open one
    assert store.count_active_positions("USDJPY", "1h", "short", db_path=db) == 1
    assert store.count_active_positions("EURUSD", "1h", "long", db_path=db) == 0   # terminal


# --- store: idempotency / duplicate signal ------------------------------------

def test_duplicate_signal_writes_nothing_and_no_second_notification(tmp_path):
    db = tmp_path / "p.sqlite3"
    _guarded(db, _pending("a"))
    outcome, refused = _guarded(db, _pending("b"))     # refused -> cancelled + 1 outbox row
    assert outcome == store.CAPACITY_RESULT_CANCELLED
    # re-import of both signals: duplicates, nothing written, outbox unchanged
    assert _guarded(db, _pending("a")) == (store.CAPACITY_RESULT_DUPLICATE, None)
    assert _guarded(db, _pending("b")) == (store.CAPACITY_RESULT_DUPLICATE, None)
    assert len(store.list_positions(db_path=db)) == 2
    assert _outbox(db) == [(f"{refused['position_id']}:cancelled",)]


def test_refused_signal_stays_cancelled_after_capacity_frees(tmp_path):
    # Deterministic contract: a position_limit refusal is terminal. When the
    # blocking position later closes, the refused signal is NOT retroactively
    # re-admitted (its signal_id row already exists); only NEW signals are.
    db = tmp_path / "p.sqlite3"
    first = _pending("a")
    _guarded(db, first)
    _guarded(db, _pending("b"))                                     # refused
    _open_it(db, first)
    store.close_open_position(
        first["position_id"], expected_watermark=None, new_watermark="2026-07-06T14:00:00Z",
        close_price=102.0, close_time_utc="2026-07-06T14:00:00Z", terminal_reason="take_profit",
        gross_pnl=2.0, net_pnl=2.0, r_multiple=2.0, db_path=db,
    )
    assert _guarded(db, _pending("b")) == (store.CAPACITY_RESULT_DUPLICATE, None)
    assert store.get_position_by_signal("b", db_path=db)["state"] == "cancelled"
    assert _guarded(db, _pending("c"))[0] == store.CAPACITY_RESULT_PENDING


# --- store: concurrent runners cannot overshoot the cap (TOCTOU) --------------

def test_concurrent_intake_respects_cap_exactly(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.init_db(db)
    results, errors = [], []
    barrier = threading.Barrier(6)

    def intake(signal_id):
        try:
            barrier.wait(timeout=30)
            results.append(_guarded(db, _pending(signal_id))[0])
        except Exception as exc:  # pragma: no cover - failure detail for the assert below
            errors.append(exc)

    threads = [threading.Thread(target=intake, args=(f"sig-{i}",)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(results) == ["cancelled"] * 5 + ["pending"]   # exactly ONE admitted
    rows = store.list_positions(db_path=db)
    assert len(rows) == 6
    assert sum(1 for r in rows if r["state"] == "pending") == 1
    assert sum(1 for r in rows if r["terminal_reason"] == "position_limit") == 5
    assert len(_outbox(db)) == 5   # one cancelled notification per refusal, no duplicates


# --- models / DB CHECK ---------------------------------------------------------

def test_position_limit_reason_only_valid_for_cancelled():
    models.validate_state_reason("cancelled", "position_limit")
    for state in ("closed", "expired", "pending", "open"):
        with pytest.raises(ValueError):
            models.validate_state_reason(state, "position_limit")


def test_db_check_accepts_position_limit_only_for_cancelled(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.init_db(db)
    good = dict(_pending("ok"), state="cancelled", terminal_reason="position_limit")
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"INSERT INTO {store.POSITIONS_TABLE} ({', '.join(store._ALL_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in store._ALL_COLUMNS)})",
            tuple(good.get(c) for c in store._ALL_COLUMNS),
        )
    bad = dict(_pending("ng"), state="closed", terminal_reason="position_limit")
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO {store.POSITIONS_TABLE} ({', '.join(store._ALL_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in store._ALL_COLUMNS)})",
            tuple(bad.get(c) for c in store._ALL_COLUMNS),
        )


# --- migration: v3 -> v4 (positions rebuild adds position_limit to the CHECK) --

def _make_v3_db(db, rows=()):
    """Build a faithful released-v3 DB: constrained positions table WITHOUT
    position_limit in the cancelled CHECK, current outbox table, user_version 3."""
    old_positions_sql = store._create_table_sql(store.POSITIONS_TABLE).replace(
        "'invalid_input', 'manual', 'position_limit'", "'invalid_input', 'manual'"
    )
    assert "'position_limit'" not in old_positions_sql   # guard the replace against schema drift
    with sqlite3.connect(db) as conn:
        conn.execute(old_positions_sql)
        conn.execute(store._create_notifications_table_sql(store.NOTIFICATIONS_TABLE))
        conn.execute(
            f"CREATE INDEX {store._NOTIFICATIONS_STATUS_INDEX} "
            f"ON {store.NOTIFICATIONS_TABLE} (delivery_status, created_at_utc)"
        )
        conn.execute("PRAGMA user_version = 3")
        for row in rows:
            conn.execute(
                f"INSERT INTO {store.POSITIONS_TABLE} ({', '.join(store._ALL_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in store._ALL_COLUMNS)})",
                tuple(row.get(c) for c in store._ALL_COLUMNS),
            )


def _user_version(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def test_v3_db_is_legacy_and_migrates_to_v4_preserving_rows(tmp_path):
    db = tmp_path / "p.sqlite3"
    kept = dict(_pending("keep"), state="cancelled", terminal_reason="manual")
    _make_v3_db(db, rows=[_pending("stay"), kept])
    assert _user_version(db) == 3
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_LEGACY   # v3 CHECK predates position_limit

    store.ensure_schema_migrated(db)

    assert _user_version(db) == store.CURRENT_SCHEMA_VERSION == 4
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_CURRENT
    assert store.get_position_by_signal("stay", db_path=db)["state"] == "pending"      # rows preserved
    assert store.get_position_by_signal("keep", db_path=db)["terminal_reason"] == "manual"
    # the rebuilt CHECK now accepts position_limit refusals
    assert _guarded(db, _pending("new"))[0] == store.CAPACITY_RESULT_CANCELLED  # "stay" occupies the cap


def test_v3_migration_failure_rolls_back_everything(tmp_path, monkeypatch):
    db = tmp_path / "p.sqlite3"
    _make_v3_db(db, rows=[_pending("stay")])
    monkeypatch.setattr(
        store, "_create_indexes",
        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")),
    )
    with pytest.raises(sqlite3.OperationalError):
        store.init_db(db)
    # full rollback: still v3, old CHECK still rejects position_limit, row intact
    assert _user_version(db) == 3
    with sqlite3.connect(db) as conn:
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (store.POSITIONS_TABLE,)
        ).fetchone()[0]
        assert "position_limit" not in table_sql
        assert conn.execute(f"SELECT COUNT(*) FROM {store.POSITIONS_TABLE}").fetchone()[0] == 1


# --- runner: cycle-level behaviour --------------------------------------------

def _signal(signal_id="sig-1", side="long", symbol="USDJPY", timeframe="1h", target="2026-07-06T12:00:00Z"):
    return {
        "signal_id": signal_id, "symbol": symbol, "timeframe": timeframe, "side": side,
        "strategy_profile": "p", "target_candle_utc": target, "status": "paper_only",
    }


def _candles(rows, start="2026-07-06T12:00:00Z"):
    idx = pd.date_range(start, periods=len(rows), freq="1h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


QUIET = [(50, 51, 49, 50), (100.0, 100.2, 99.9, 100.1), (100.1, 100.3, 99.8, 100.0)]


def _run(db, signals, *, dry_run=False, profile=PROFILE, candles=None, now=NOW):
    df = _candles(candles if candles is not None else QUIET)
    return runner.run_once(
        dry_run=dry_run, db_path=db, signals=signals, load_candles=lambda s, t: df, now_utc=now,
        profile=profile, chart_db_path=db.parent / "chart.sqlite3", signal_db_path=db.parent / "sig.sqlite3",
    )


def test_cycle_admits_one_and_cancels_rest_by_default(tmp_path):
    db = tmp_path / "p.sqlite3"
    signals = [_signal(f"sig-{i}") for i in range(3)]
    res = _run(db, signals)
    assert res.failures == 0
    assert res.pending_created == 1 and res.cancelled == 2
    assert _states(db) == [("cancelled", "position_limit"), ("cancelled", "position_limit"), ("open", None)]
    assert len(_outbox(db)) == 3   # 1 open + 2 cancelled events, one row each


def test_cycle_rerun_is_idempotent_no_new_rows_or_notifications(tmp_path):
    db = tmp_path / "p.sqlite3"
    signals = [_signal("sig-0"), _signal("sig-1")]
    _run(db, signals)
    before_positions = _states(db)
    before_outbox = _outbox(db)
    res2 = _run(db, signals)   # same cycle re-run / same signals re-imported
    assert res2.failures == 0
    assert res2.pending_created == 0 and res2.cancelled == 0
    assert _states(db) == before_positions
    assert _outbox(db) == before_outbox


def test_existing_open_blocks_same_side_but_not_opposite(tmp_path):
    db = tmp_path / "p.sqlite3"
    _run(db, [_signal("sig-long")])                       # becomes open on QUIET candles
    assert store.get_position_by_signal("sig-long", db_path=db)["state"] == "open"
    res = _run(db, [_signal("same", side="long"), _signal("other", side="short")])
    assert res.failures == 0
    assert store.get_position_by_signal("same", db_path=db)["terminal_reason"] == "position_limit"
    other = store.get_position_by_signal("other", db_path=db)
    assert other["state"] in ("pending", "open")          # opposite side admitted


def test_profile_cap_of_two_admits_two(tmp_path):
    db = tmp_path / "p.sqlite3"
    profile = profiles.ExecutionProfile(
        max_entry_delay_bars=3, max_hold_bars=24, fallback_stop_pct=0.01, risk_reward=2.0,
        max_active_positions_per_side=2,
    )
    res = _run(db, [_signal(f"sig-{i}") for i in range(3)], profile=profile)
    assert res.failures == 0
    assert res.pending_created == 2 and res.cancelled == 1


def test_unlimited_profile_restores_previous_behaviour(tmp_path):
    db = tmp_path / "p.sqlite3"
    profile = profiles.ExecutionProfile(
        max_entry_delay_bars=3, max_hold_bars=24, fallback_stop_pct=0.01, risk_reward=2.0,
        max_active_positions_per_side=None,
    )
    res = _run(db, [_signal(f"sig-{i}") for i in range(4)], profile=profile)
    assert res.failures == 0 and res.pending_created == 4 and res.cancelled == 0


def test_terminal_positions_do_not_block_new_cycle_signals(tmp_path):
    db = tmp_path / "p.sqlite3"
    # take_profit close within the first cycle -> terminal
    closing = [(50, 51, 49, 50), (100.0, 100.2, 99.9, 100.1), (100.5, 102.5, 100.1, 101.0)]
    _run(db, [_signal("sig-old")], candles=closing)
    assert store.get_position_by_signal("sig-old", db_path=db)["state"] == "closed"
    res = _run(db, [_signal("sig-new", target="2026-07-06T13:00:00Z")], candles=closing)
    assert res.failures == 0 and res.cancelled == 0
    assert store.get_position_by_signal("sig-new", db_path=db)["state"] in ("pending", "open", "closed")


def test_concurrent_runners_no_duplicates_no_overshoot(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.init_db(db)
    signals = [_signal("sig-0"), _signal("sig-1")]
    errors = []
    barrier = threading.Barrier(2)

    def cycle():
        try:
            barrier.wait(timeout=30)
            # only the signal candle exists and the deadline (16:00) is not
            # reached at 13:30 -> the admitted position stays pending.
            res = _run(db, signals, candles=[(50, 51, 49, 50)], now=pd.Timestamp("2026-07-06T13:30:00Z"))
            assert res.failures == 0
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=cycle) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    rows = store.list_positions(db_path=db)
    assert len(rows) == 2                                          # one position per signal, no duplicates
    assert sum(1 for r in rows if r["state"] == "pending") == 1    # cap never overshot
    assert sum(1 for r in rows if r["terminal_reason"] == "position_limit") == 1
    assert len(_outbox(db)) == 1                                   # single cancelled event, no duplicates


# --- runner: dry-run is fully side-effect free --------------------------------

def test_dry_run_projects_cap_decisions_without_writing(tmp_path):
    db = tmp_path / "p.sqlite3"
    res = _run(db, [_signal("sig-0"), _signal("sig-1")], dry_run=True)
    assert res.failures == 0
    assert res.pending_created == 1 and res.cancelled == 1   # same decisions as a real run
    assert not db.exists()                                   # nothing persisted
    assert not (tmp_path / "logs").exists()


def test_dry_run_counts_existing_active_positions_and_leaves_db_untouched(tmp_path):
    db = tmp_path / "p.sqlite3"
    _guarded(db, _pending("occupied"))          # existing pending occupies the cap
    before_bytes = db.read_bytes()
    before_mtime = db.stat().st_mtime_ns
    res = _run(db, [_signal("sig-new")], dry_run=True)
    assert res.failures == 0
    assert res.pending_created == 0 and res.cancelled == 1   # would be refused
    assert db.read_bytes() == before_bytes and db.stat().st_mtime_ns == before_mtime
    assert len(store.list_positions(db_path=db)) == 1        # no new rows, no outbox rows
    assert _outbox(db) == []


# --- CLI mapping ---------------------------------------------------------------

def test_cli_default_and_zero_means_unlimited():
    args = runner.parse_args([])
    assert runner._profile_from_args(args).max_active_positions_per_side == 1
    args = runner.parse_args(["--max-active-positions-per-side", "0"])
    assert runner._profile_from_args(args).max_active_positions_per_side is None
    args = runner.parse_args(["--max-active-positions-per-side", "3"])
    assert runner._profile_from_args(args).max_active_positions_per_side == 3


def test_profile_rejects_negative_cap():
    with pytest.raises(ValueError):
        profiles.ExecutionProfile(max_active_positions_per_side=-1)
    with pytest.raises(ValueError):
        profiles.ExecutionProfile(max_active_positions_per_side=0)
