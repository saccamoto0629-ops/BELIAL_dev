"""Issue #49/#50: store -- CHECK constraints, CAS transitions, watermark, migration."""

import pathlib
import sqlite3
import sys

import pytest

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

import position_models as models  # noqa: E402
import position_store as store  # noqa: E402
import execution_profile as profiles  # noqa: E402

PROFILE = profiles.ExecutionProfile(fallback_stop_pct=0.01, risk_reward=2.0)


def _pending(signal_id="sig-1", **over):
    base = dict(
        signal_id=signal_id, symbol="USDJPY", timeframe="1h", side="long",
        strategy_profile="p", profile=PROFILE,
        signal_candle_utc="2026-07-06T12:00:00Z", entry_deadline_utc="2026-07-06T16:00:00Z",
    )
    base.update(over)
    return models.create_pending_position(**base)


def _open_it(db, pos):
    return store.transition_to_open(
        pos["position_id"], entry_price=100.0, entry_time_utc="2026-07-06T13:00:00Z",
        stop_loss_price=99.0, take_profit_price=102.0, max_hold_deadline_utc="2026-07-07T13:00:00Z",
        db_path=db,
    )


# --- create / idempotency ----------------------------------------------------

def test_create_and_get(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _pending()
    assert store.create_position(pos, db_path=db) is True
    got = store.get_position(pos["position_id"], db_path=db)
    assert got["state"] == "pending"
    assert store.get_position_by_signal("sig-1", db_path=db)["position_id"] == pos["position_id"]


def test_create_is_idempotent_on_signal_id(tmp_path):
    db = tmp_path / "p.sqlite3"
    assert store.create_position(_pending(signal_id="dup"), db_path=db) is True
    # second create for the same signal_id is a no-op (ON CONFLICT DO NOTHING)
    assert store.create_position(_pending(signal_id="dup"), db_path=db) is False
    assert len(store.list_positions(db_path=db)) == 1


# --- transitions -------------------------------------------------------------

def test_pending_to_open(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _pending()
    store.create_position(pos, db_path=db)
    opened = _open_it(db, pos)
    assert opened["state"] == "open"
    assert opened["entry_price"] == 100.0
    assert opened["stop_loss_price"] == 99.0


def test_pending_to_expired(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _pending()
    store.create_position(pos, db_path=db)
    out = store.transition_pending_terminal(
        pos["position_id"], state="expired", terminal_reason="entry_timeout", db_path=db
    )
    assert out["state"] == "expired" and out["terminal_reason"] == "entry_timeout"


def test_pending_to_cancelled(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _pending()
    store.create_position(pos, db_path=db)
    out = store.transition_pending_terminal(
        pos["position_id"], state="cancelled", terminal_reason="manual", db_path=db
    )
    assert out["state"] == "cancelled" and out["terminal_reason"] == "manual"


def test_open_to_closed(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _pending()
    store.create_position(pos, db_path=db)
    _open_it(db, pos)
    closed = store.close_open_position(
        pos["position_id"], expected_watermark=None, new_watermark="2026-07-06T14:00:00Z",
        close_price=102.0, close_time_utc="2026-07-06T14:00:00Z", terminal_reason="take_profit",
        pnl=2.0, db_path=db,
    )
    assert closed["state"] == "closed"
    assert closed["close_price"] == 102.0
    assert closed["terminal_reason"] == "take_profit"
    assert closed["last_evaluated_candle_utc"] == "2026-07-06T14:00:00Z"


# --- terminal re-transition rejected -----------------------------------------

def test_reopen_terminal_rejected(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _pending()
    store.create_position(pos, db_path=db)
    store.transition_pending_terminal(pos["position_id"], state="expired", terminal_reason="entry_timeout", db_path=db)
    # an expired position can never be opened
    with pytest.raises(store.PositionConcurrentModificationError):
        _open_it(db, pos)


def test_double_close_rejected(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _pending()
    store.create_position(pos, db_path=db)
    _open_it(db, pos)
    store.close_open_position(
        pos["position_id"], expected_watermark=None, new_watermark="2026-07-06T14:00:00Z",
        close_price=102.0, close_time_utc="t", terminal_reason="take_profit", pnl=2.0, db_path=db,
    )
    with pytest.raises(store.PositionConcurrentModificationError):
        store.close_open_position(
            pos["position_id"], expected_watermark="2026-07-06T14:00:00Z", new_watermark="2026-07-06T15:00:00Z",
            close_price=90.0, close_time_utc="t2", terminal_reason="stop_loss", pnl=-10.0, db_path=db,
        )


# --- DB CHECK constraint (state x terminal_reason) ---------------------------

def _raw_insert(db, **over):
    pos = _pending(**{k: v for k, v in over.items() if k in ("signal_id",)})
    pos.update({k: v for k, v in over.items() if k not in ("signal_id",)})
    store.init_db(db)
    cols = store._ALL_COLUMNS
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"INSERT INTO {store.POSITIONS_TABLE} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            tuple(pos.get(c) for c in cols),
        )


def test_db_check_rejects_invalid_state_reason(tmp_path):
    db = tmp_path / "p.sqlite3"
    # state=open with a terminal_reason violates the CHECK constraint
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(db, signal_id="x", state="open", terminal_reason="stop_loss")


def test_db_check_rejects_unknown_state(tmp_path):
    db = tmp_path / "p.sqlite3"
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(db, signal_id="y", state="frozen", terminal_reason=None)


# --- watermark CAS / concurrency --------------------------------------------

def test_advance_watermark_cas(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _pending()
    store.create_position(pos, db_path=db)
    _open_it(db, pos)
    assert store.advance_watermark(pos["position_id"], expected_watermark=None, new_watermark="c1", db_path=db) is True
    # a stale expected watermark (None again) now matches zero rows
    assert store.advance_watermark(pos["position_id"], expected_watermark=None, new_watermark="c2", db_path=db) is False
    assert store.get_position(pos["position_id"], db_path=db)["last_evaluated_candle_utc"] == "c1"


def test_concurrent_close_only_one_wins(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _pending()
    store.create_position(pos, db_path=db)
    _open_it(db, pos)
    # two evaluators both read watermark=None; the first close wins, the second
    # (stale expected watermark + now-closed state) matches zero rows -> raises.
    store.close_open_position(
        pos["position_id"], expected_watermark=None, new_watermark="cA",
        close_price=102.0, close_time_utc="t", terminal_reason="take_profit", pnl=2.0, db_path=db,
    )
    with pytest.raises(store.PositionConcurrentModificationError):
        store.close_open_position(
            pos["position_id"], expected_watermark=None, new_watermark="cB",
            close_price=99.0, close_time_utc="t", terminal_reason="stop_loss", pnl=-1.0, db_path=db,
        )


# --- migration / backward compat --------------------------------------------

def test_legacy_partial_schema_migrated(tmp_path):
    db = tmp_path / "p.sqlite3"
    with sqlite3.connect(db) as conn:
        # older schema: core columns only, missing the migrated exit/watermark cols
        conn.execute(
            f"""
            CREATE TABLE {store.POSITIONS_TABLE} (
                position_id TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL, timeframe TEXT NOT NULL, side TEXT NOT NULL,
                strategy_profile TEXT NOT NULL, execution_profile TEXT NOT NULL,
                state TEXT NOT NULL, terminal_reason TEXT,
                signal_candle_utc TEXT NOT NULL, entry_deadline_utc TEXT NOT NULL,
                entry_price REAL, entry_time_utc TEXT, stop_loss_price REAL, take_profit_price REAL,
                max_entry_delay_bars INTEGER NOT NULL, max_hold_bars INTEGER NOT NULL,
                same_candle_priority TEXT NOT NULL, fallback_stop_pct REAL NOT NULL, risk_reward REAL NOT NULL,
                spread REAL NOT NULL, slippage REAL NOT NULL, commission REAL NOT NULL,
                created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL
            )
            """
        )
        conn.execute(
            f"INSERT INTO {store.POSITIONS_TABLE} (position_id, signal_id, symbol, timeframe, side, "
            "strategy_profile, execution_profile, state, terminal_reason, signal_candle_utc, entry_deadline_utc, "
            "max_entry_delay_bars, max_hold_bars, same_candle_priority, fallback_stop_pct, risk_reward, "
            "spread, slippage, commission, created_at_utc, updated_at_utc) VALUES "
            "('old-1','s-old','USDJPY','1h','long','p','default','pending',NULL,'2026-07-06T12:00:00Z',"
            "'2026-07-06T16:00:00Z',3,24,'stop_loss',0.003,2.0,0,0,0,'t','t')"
        )
    store.init_db(db)  # must add the missing columns without dropping the row
    got = store.get_position("old-1", db_path=db)
    assert got is not None
    assert got["pnl"] is None and got["last_evaluated_candle_utc"] is None
    assert got["state"] == "pending"


def test_init_db_idempotent(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.create_position(_pending(), db_path=db)
    store.init_db(db)
    store.init_db(db)
    assert len(store.list_positions(db_path=db)) == 1


# --- read helpers: dry-run safe, but do NOT swallow OperationalError ----------

def test_reads_do_not_create_db(tmp_path):
    db = tmp_path / "absent.sqlite3"
    assert store.get_position("x", db_path=db) is None
    assert store.get_position_by_signal("x", db_path=db) is None
    assert store.list_positions_by_state("open", db_path=db) == []
    assert store.list_positions(db_path=db) == []
    assert not db.exists()


def test_read_does_not_swallow_operational_error(tmp_path):
    db = tmp_path / "broken.sqlite3"
    # file exists but the table is missing -> a real OperationalError must surface,
    # not be turned into an empty list (Issue #49 runner contract).
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
    with pytest.raises(sqlite3.OperationalError):
        store.list_positions_by_state("open", db_path=db)
