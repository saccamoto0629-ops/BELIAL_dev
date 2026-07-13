"""Issue #50: virtual-position repository, migration, transactions, idempotency."""

import pathlib
import sqlite3
import sys

import pytest

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

import position_models as models  # noqa: E402
import position_store as store  # noqa: E402


def _make_position(**overrides):
    base = dict(
        paper_signal_id="sig-1",
        strategy_profile="market_bias_macd_alignment_v1",
        symbol="USDJPY",
        timeframe="1h",
        side="long",
        entry_price=150.0,
        opened_at="2026-07-05T12:00:00Z",
        stop_loss=149.5,
        take_profit=151.0,
    )
    base.update(overrides)
    return models.create_virtual_position(**base)


def test_create_then_get_roundtrip(tmp_path):
    db = tmp_path / "pos.sqlite3"
    pos = _make_position()
    store.create_position(pos, db_path=db)

    fetched = store.get_position(pos["position_id"], db_path=db)
    assert fetched is not None
    assert fetched["paper_signal_id"] == "sig-1"
    assert fetched["status"] == "open"
    assert fetched["entry_price"] == 150.0


def test_get_open_and_list_open_positions(tmp_path):
    db = tmp_path / "pos.sqlite3"
    store.create_position(_make_position(paper_signal_id="a"), db_path=db)
    store.create_position(_make_position(paper_signal_id="b", symbol="XAUUSD"), db_path=db)

    all_open = store.list_open_positions(db_path=db)
    assert len(all_open) == 2
    xau = store.list_open_positions(symbol="XAUUSD", db_path=db)
    assert len(xau) == 1 and xau[0]["symbol"] == "XAUUSD"


def test_paper_signal_relation_and_duplicate_open_blocked(tmp_path):
    db = tmp_path / "pos.sqlite3"
    pos = _make_position(paper_signal_id="sig-42")
    store.create_position(pos, db_path=db)

    by_signal = store.get_position_by_signal("sig-42", db_path=db)
    assert by_signal is not None and by_signal["position_id"] == pos["position_id"]

    # UNIQUE(paper_signal_id): a second position for the same signal is refused.
    with pytest.raises(sqlite3.IntegrityError):
        store.create_position(_make_position(paper_signal_id="sig-42"), db_path=db)
    assert len(store.list_positions(db_path=db)) == 1


def test_close_persists_all_exit_fields(tmp_path):
    db = tmp_path / "pos.sqlite3"
    pos = _make_position()
    store.create_position(pos, db_path=db)

    closed = store.close_position(
        pos["position_id"],
        exit_price=151.0,
        closed_at="2026-07-05T15:00:00Z",
        close_reason="take_profit",
        gross_pnl=1.0,
        net_pnl=0.9,
        spread_cost=0.05,
        slippage_cost=0.0,
        fee_cost=0.1,
        r_multiple=2.0,
        db_path=db,
    )
    assert closed["status"] == "closed"
    assert closed["exit_price"] == 151.0
    assert closed["closed_at"] == "2026-07-05T15:00:00Z"
    assert closed["close_reason"] == "take_profit"
    assert closed["gross_pnl"] == 1.0
    assert closed["net_pnl"] == 0.9
    assert closed["r_multiple"] == 2.0
    # no longer listed as open
    assert store.list_open_positions(db_path=db) == []


def test_double_close_is_refused(tmp_path):
    db = tmp_path / "pos.sqlite3"
    pos = _make_position()
    store.create_position(pos, db_path=db)
    store.close_position(
        pos["position_id"], exit_price=151.0, closed_at="t", close_reason="take_profit",
        gross_pnl=1.0, net_pnl=1.0, db_path=db,
    )
    with pytest.raises(store.PositionAlreadyClosedError):
        store.close_position(
            pos["position_id"], exit_price=140.0, closed_at="t2", close_reason="stop_loss",
            gross_pnl=-10.0, net_pnl=-10.0, db_path=db,
        )
    # the original close is intact -- a refused re-close never mutated the row
    again = store.get_position(pos["position_id"], db_path=db)
    assert again["exit_price"] == 151.0 and again["close_reason"] == "take_profit"


def test_close_unknown_position_raises_not_found(tmp_path):
    db = tmp_path / "pos.sqlite3"
    store.init_db(db)
    with pytest.raises(store.PositionNotFoundError):
        store.close_position(
            "nope", exit_price=1.0, closed_at="t", close_reason="stop_loss",
            gross_pnl=0.0, net_pnl=0.0, db_path=db,
        )


def test_close_rejects_unknown_reason(tmp_path):
    db = tmp_path / "pos.sqlite3"
    pos = _make_position()
    store.create_position(pos, db_path=db)
    with pytest.raises(ValueError):
        store.close_position(
            pos["position_id"], exit_price=1.0, closed_at="t", close_reason="liquidation",
            gross_pnl=0.0, net_pnl=0.0, db_path=db,
        )


def test_events_history_records_open_and_close(tmp_path):
    db = tmp_path / "pos.sqlite3"
    pos = _make_position()
    store.create_position(pos, db_path=db)
    events_after_open = store.list_events(pos["position_id"], db_path=db)
    assert [e["event_type"] for e in events_after_open] == ["opened"]

    store.close_position(
        pos["position_id"], exit_price=151.0, closed_at="2026-07-05T15:00:00Z",
        close_reason="take_profit", gross_pnl=1.0, net_pnl=1.0, db_path=db,
    )
    events = store.list_events(pos["position_id"], db_path=db)
    assert sorted(e["event_type"] for e in events) == ["closed", "opened"]


def test_init_db_is_idempotent_and_preserves_data(tmp_path):
    db = tmp_path / "pos.sqlite3"
    pos = _make_position()
    store.create_position(pos, db_path=db)
    # Running init_db again must not raise or drop the existing row.
    store.init_db(db)
    store.init_db(db)
    assert store.get_position(pos["position_id"], db_path=db) is not None


def test_legacy_partial_schema_is_migrated_in_place(tmp_path):
    """A positions table created before the cost columns existed upgrades in place."""
    db = tmp_path / "pos.sqlite3"
    # Simulate an older schema: core columns only, none of the migrated
    # exit/PnL/provenance columns, and one pre-existing row.
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"""
            CREATE TABLE {store.POSITIONS_TABLE} (
                position_id TEXT PRIMARY KEY,
                paper_signal_id TEXT NOT NULL,
                strategy_profile TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                entry_price REAL NOT NULL,
                size REAL NOT NULL,
                stop_loss REAL,
                take_profit REAL,
                opened_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            f"INSERT INTO {store.POSITIONS_TABLE} "
            "(position_id, paper_signal_id, strategy_profile, symbol, timeframe, side, status, "
            " entry_price, size, stop_loss, take_profit, opened_at, created_at, updated_at) "
            "VALUES ('old-1','sig-old','p','USDJPY','1h','long','open',150.0,1.0,149.5,151.0,'t','t','t')"
        )

    # init_db must add the missing columns without touching the legacy row.
    store.init_db(db)
    migrated = store.get_position("old-1", db_path=db)
    assert migrated is not None
    assert migrated["net_pnl"] is None          # newly added, NULL for the legacy row
    assert migrated["entry_signal_time"] is None
    assert migrated["entry_price"] == 150.0      # legacy data intact

    # and the migrated DB is fully functional (create + close still work).
    store.close_position(
        "old-1", exit_price=151.0, closed_at="t2", close_reason="take_profit",
        gross_pnl=1.0, net_pnl=1.0, db_path=db,
    )
    assert store.get_position("old-1", db_path=db)["status"] == "closed"


def test_create_position_transaction_rolls_back_on_event_failure(tmp_path, monkeypatch):
    """If the opened-event insert fails, the position insert is rolled back too."""
    db = tmp_path / "pos.sqlite3"
    # Force every opened-event to reuse one event_id, so the SECOND create's
    # event INSERT violates the event PK *after* its position INSERT succeeded.
    monkeypatch.setattr(models, "new_event_id", lambda: "fixed-event-id")

    store.create_position(_make_position(paper_signal_id="a"), db_path=db)  # commits fine

    second = _make_position(paper_signal_id="b")
    with pytest.raises(sqlite3.IntegrityError):
        store.create_position(second, db_path=db)

    # The second position must NOT exist -- the whole transaction rolled back.
    assert store.get_position(second["position_id"], db_path=db) is None
    assert len(store.list_positions(db_path=db)) == 1


def test_reads_never_create_the_db(tmp_path):
    """Dry-run safety: read helpers never create the DB file."""
    db = tmp_path / "does_not_exist.sqlite3"
    assert store.get_position("x", db_path=db) is None
    assert store.get_position_by_signal("x", db_path=db) is None
    assert store.list_open_positions(db_path=db) == []
    assert store.list_positions(db_path=db) == []
    assert store.list_events("x", db_path=db) == []
    assert not db.exists()
