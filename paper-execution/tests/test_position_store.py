"""Issue #49/#50: store -- CHECK/UNIQUE, CAS, watermark, PnL, real constraint migration."""

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
        stop_loss_price=99.0, take_profit_price=102.0, max_hold_deadline_utc="2026-07-07T13:00:00Z", db_path=db,
    )


def _close_it(db, pos, **over):
    kw = dict(
        expected_watermark=None, new_watermark="2026-07-06T14:00:00Z", close_price=102.0,
        close_time_utc="2026-07-06T14:00:00Z", terminal_reason="take_profit",
        gross_pnl=2.0, net_pnl=1.9, r_multiple=1.9, db_path=db,
    )
    kw.update(over)
    return store.close_open_position(pos["position_id"], **kw)


# --- create / idempotency / transitions --------------------------------------

def test_create_and_get(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _pending()
    assert store.create_position(pos, db_path=db) is True
    assert store.get_position(pos["position_id"], db_path=db)["state"] == "pending"
    assert store.get_position_by_signal("sig-1", db_path=db)["position_id"] == pos["position_id"]


def test_create_is_idempotent_on_signal_id(tmp_path):
    db = tmp_path / "p.sqlite3"
    assert store.create_position(_pending(signal_id="dup"), db_path=db) is True
    assert store.create_position(_pending(signal_id="dup"), db_path=db) is False
    assert len(store.list_positions(db_path=db)) == 1


def test_pending_to_open(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _pending()
    store.create_position(pos, db_path=db)
    opened = _open_it(db, pos)
    assert opened["state"] == "open" and opened["entry_price"] == 100.0 and opened["size"] == 1.0


def test_pending_to_expired(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _pending()
    store.create_position(pos, db_path=db)
    out = store.transition_pending_terminal(pos["position_id"], state="expired", terminal_reason="entry_timeout", db_path=db)
    assert out["state"] == "expired" and out["terminal_reason"] == "entry_timeout"


def test_pending_to_cancelled(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _pending()
    store.create_position(pos, db_path=db)
    out = store.transition_pending_terminal(pos["position_id"], state="cancelled", terminal_reason="manual", db_path=db)
    assert out["state"] == "cancelled" and out["terminal_reason"] == "manual"


def test_open_to_closed_persists_pnl(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _pending()
    store.create_position(pos, db_path=db)
    _open_it(db, pos)
    closed = _close_it(db, pos, gross_pnl=2.0, net_pnl=1.8, r_multiple=1.8)
    assert closed["state"] == "closed" and closed["close_price"] == 102.0
    assert closed["gross_pnl"] == 2.0 and closed["net_pnl"] == 1.8
    assert closed["pnl"] == 1.8              # pnl is the net_pnl alias
    assert closed["r_multiple"] == 1.8
    assert closed["last_evaluated_candle_utc"] == "2026-07-06T14:00:00Z"


# --- terminal re-transition / double close -----------------------------------

def test_reopen_terminal_rejected(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _pending()
    store.create_position(pos, db_path=db)
    store.transition_pending_terminal(pos["position_id"], state="expired", terminal_reason="entry_timeout", db_path=db)
    with pytest.raises(store.PositionConcurrentModificationError):
        _open_it(db, pos)


def test_double_close_rejected(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _pending()
    store.create_position(pos, db_path=db)
    _open_it(db, pos)
    _close_it(db, pos)
    with pytest.raises(store.PositionConcurrentModificationError):
        _close_it(db, pos, expected_watermark="2026-07-06T14:00:00Z", new_watermark="2026-07-06T15:00:00Z",
                  terminal_reason="stop_loss", gross_pnl=-1.0, net_pnl=-1.0, r_multiple=-1.0)


def test_concurrent_close_only_one_wins(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _pending()
    store.create_position(pos, db_path=db)
    _open_it(db, pos)
    _close_it(db, pos, new_watermark="cA")
    with pytest.raises(store.PositionConcurrentModificationError):
        _close_it(db, pos, new_watermark="cB", terminal_reason="stop_loss", gross_pnl=-1.0, net_pnl=-1.0, r_multiple=-1.0)


def test_advance_watermark_cas(tmp_path):
    db = tmp_path / "p.sqlite3"
    pos = _pending()
    store.create_position(pos, db_path=db)
    _open_it(db, pos)
    assert store.advance_watermark(pos["position_id"], expected_watermark=None, new_watermark="c1", db_path=db) is True
    assert store.advance_watermark(pos["position_id"], expected_watermark=None, new_watermark="c2", db_path=db) is False


# --- DB CHECK constraints (fresh) --------------------------------------------

def _raw_full_row(**over):
    pos = _pending(**{k: v for k, v in over.items() if k == "signal_id"})
    pos.update({k: v for k, v in over.items() if k != "signal_id"})
    return tuple(pos.get(c) for c in store._ALL_COLUMNS)


def test_db_check_rejects_invalid_state_reason(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.init_db(db)
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO {store.POSITIONS_TABLE} ({', '.join(store._ALL_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in store._ALL_COLUMNS)})",
            _raw_full_row(signal_id="x", state="open", terminal_reason="stop_loss"),
        )


def test_db_rejects_unknown_state_and_duplicate_signal(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.create_position(_pending(signal_id="a"), db_path=db)
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO {store.POSITIONS_TABLE} ({', '.join(store._ALL_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in store._ALL_COLUMNS)})",
            _raw_full_row(signal_id="b", state="frozen"),
        )


# --- reads: dry-run safe but never swallow OperationalError -------------------

def test_reads_do_not_create_db(tmp_path):
    db = tmp_path / "absent.sqlite3"
    assert store.get_position("x", db_path=db) is None
    assert store.list_positions_by_state("open", db_path=db) == []
    assert not db.exists()


def test_read_does_not_swallow_operational_error(tmp_path):
    db = tmp_path / "broken.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
    with pytest.raises(sqlite3.OperationalError):
        store.list_positions_by_state("open", db_path=db)


# --- migration: real constraint enforcement via table rebuild ----------------

# text/real/int affinity for building a constraint-less legacy table.
_TEXT = {
    "position_id", "signal_id", "symbol", "timeframe", "side", "strategy_profile", "execution_profile",
    "state", "terminal_reason", "signal_candle_utc", "entry_deadline_utc", "entry_time_utc",
    "max_hold_deadline_utc", "close_time_utc", "same_candle_priority", "last_evaluated_candle_utc",
    "created_at_utc", "updated_at_utc",
}
_INT = {"max_entry_delay_bars", "max_hold_bars"}


def _legacy_type(col):
    return "TEXT" if col in _TEXT else ("INTEGER" if col in _INT else "REAL")


def _make_legacy(db, *, columns, rows, user_version=0):
    """Create a CONSTRAINT-LESS legacy table with `columns`, insert `rows` (dicts)."""
    ddl_cols = ", ".join(f"{c} {_legacy_type(c)}" for c in columns)
    with sqlite3.connect(db) as conn:
        conn.execute(f"CREATE TABLE {store.POSITIONS_TABLE} ({ddl_cols})")
        conn.execute(f"PRAGMA user_version = {user_version}")
        for row in rows:
            cols = [c for c in columns if c in row]
            conn.execute(
                f"INSERT INTO {store.POSITIONS_TABLE} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                tuple(row[c] for c in cols),
            )


def _valid_legacy_row(**over):
    row = dict(
        position_id="old-1", signal_id="s-old", symbol="USDJPY", timeframe="1h", side="long",
        strategy_profile="p", execution_profile="default", state="pending", terminal_reason=None,
        signal_candle_utc="2026-07-06T12:00:00Z", entry_deadline_utc="2026-07-06T16:00:00Z",
        max_entry_delay_bars=3, max_hold_bars=24, same_candle_priority="stop_loss",
        fallback_stop_pct=0.003, risk_reward=2.0, spread=0.0, slippage=0.0, commission=0.0,
        created_at_utc="t", updated_at_utc="t",
    )
    row.update(over)
    return row


# columns of a legacy schema that predates size/gross/net/r (the previous version).
_LEGACY_COLUMNS = [c for c in store._ALL_COLUMNS if c not in ("size", "gross_pnl", "net_pnl", "r_multiple")]


def _user_version(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def _bogus_insert_raises(db):
    with sqlite3.connect(db) as conn:
        try:
            conn.execute(
                f"INSERT INTO {store.POSITIONS_TABLE} ({', '.join(store._ALL_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in store._ALL_COLUMNS)})",
                _raw_full_row(signal_id="z", state="bogus"),
            )
            return False
        except sqlite3.IntegrityError:
            return True


def test_fresh_db_sets_schema_version_and_constraints(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.init_db(db)
    assert _user_version(db) == store.CURRENT_SCHEMA_VERSION
    assert _bogus_insert_raises(db)


def test_migrate_constraintless_legacy_adds_constraints_and_backfills(tmp_path):
    db = tmp_path / "p.sqlite3"
    _make_legacy(db, columns=_LEGACY_COLUMNS, rows=[_valid_legacy_row(pnl=1.5)])
    assert _user_version(db) == 0

    store.init_db(db)  # rebuild

    assert _user_version(db) == store.CURRENT_SCHEMA_VERSION
    migrated = store.get_position("old-1", db_path=db)
    assert migrated["state"] == "pending"
    assert migrated["size"] == 1.0                # backfilled default
    assert migrated["net_pnl"] == 1.5 and migrated["pnl"] == 1.5   # pnl -> net_pnl
    assert migrated["gross_pnl"] is None and migrated["r_multiple"] is None
    assert len(store.list_positions(db_path=db)) == 1   # row count preserved
    # the CHECK / UNIQUE constraints are now actually enforced
    assert _bogus_insert_raises(db)
    assert store.create_position(_pending(signal_id="s-old"), db_path=db) is False  # UNIQUE(signal_id)


def test_migrate_all_columns_but_no_constraints(tmp_path):
    db = tmp_path / "p.sqlite3"
    _make_legacy(db, columns=list(store._ALL_COLUMNS), rows=[_valid_legacy_row(size=2.0)])
    store.init_db(db)
    assert store.get_position("old-1", db_path=db)["size"] == 2.0
    assert _bogus_insert_raises(db)                 # CHECK now enforced


def test_migration_idempotent_rerun(tmp_path):
    db = tmp_path / "p.sqlite3"
    _make_legacy(db, columns=_LEGACY_COLUMNS, rows=[_valid_legacy_row()])
    store.init_db(db)
    store.init_db(db)   # second call is a no-op fast path
    assert _user_version(db) == store.CURRENT_SCHEMA_VERSION
    assert len(store.list_positions(db_path=db)) == 1


@pytest.mark.parametrize("bad", [
    {"state": "bogus"},
    {"state": "open", "terminal_reason": "stop_loss"},     # invalid state x reason
    {"side": "buy"},                                        # invalid side
    {"position_id": None},                                  # NULL identity
])
def test_migration_fails_explicitly_on_invalid_legacy(tmp_path, bad):
    db = tmp_path / "p.sqlite3"
    _make_legacy(db, columns=_LEGACY_COLUMNS, rows=[_valid_legacy_row(**bad)])
    with pytest.raises(store.PositionMigrationError):
        store.init_db(db)
    # rollback: the original legacy table is intact and NOT upgraded.
    assert _user_version(db) == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {store.POSITIONS_TABLE}").fetchone()[0] == 1
        assert "size" not in {r[1] for r in conn.execute(f"PRAGMA table_info({store.POSITIONS_TABLE})")}


def test_migration_fails_on_duplicate_signal(tmp_path):
    db = tmp_path / "p.sqlite3"
    _make_legacy(
        db, columns=_LEGACY_COLUMNS,
        rows=[_valid_legacy_row(position_id="a", signal_id="dup"), _valid_legacy_row(position_id="b", signal_id="dup")],
    )
    with pytest.raises(store.PositionMigrationError):
        store.init_db(db)
    assert _user_version(db) == 0   # rolled back


# --- get_schema_status classification (incl. invalid non-SQLite files) -------

def _corrupt_sqlite_header(db):
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t (x)")
    data = bytearray(db.read_bytes())
    data[0:16] = b"XXXXXXXXXXXXXXXX"   # smash the "SQLite format 3\0" magic
    db.write_bytes(bytes(data))


def test_schema_status_missing(tmp_path):
    assert store.get_schema_status(tmp_path / "nope.sqlite3") == store.SCHEMA_STATUS_MISSING


def test_schema_status_current(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.init_db(db)
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_CURRENT


def test_schema_status_legacy(tmp_path):
    db = tmp_path / "p.sqlite3"
    _make_legacy(db, columns=_LEGACY_COLUMNS, rows=[_valid_legacy_row()])
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_LEGACY


def test_schema_status_valid_sqlite_without_our_table_is_legacy(tmp_path):
    db = tmp_path / "p.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
    # a real SQLite DB missing our table just needs init -> legacy, NOT invalid
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_LEGACY


@pytest.mark.parametrize("make", [
    lambda p: p.write_bytes(bytes(range(256)) * 8),                       # arbitrary bytes
    lambda p: p.write_text("just some text, definitely not a database\n" * 20),  # text file
    _corrupt_sqlite_header,                                               # broken SQLite header
])
def test_schema_status_invalid_for_non_sqlite_files(tmp_path, make):
    db = tmp_path / "bad.sqlite3"
    if make is _corrupt_sqlite_header:
        _corrupt_sqlite_header(db)
    else:
        make(db)
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_INVALID


def test_get_schema_status_does_not_modify_invalid_file(tmp_path):
    db = tmp_path / "bad.sqlite3"
    db.write_bytes(b"not a database \x00\x01\x02" * 16)
    before, mt, sz = db.read_bytes(), db.stat().st_mtime_ns, db.stat().st_size
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_INVALID
    assert db.read_bytes() == before and db.stat().st_mtime_ns == mt and db.stat().st_size == sz


def test_ensure_schema_migrated_refuses_invalid_without_overwrite(tmp_path):
    db = tmp_path / "bad.sqlite3"
    db.write_bytes(b"not a database \x00\x01\x02" * 16)
    before = db.read_bytes()
    with pytest.raises(store.PositionSchemaError):
        store.ensure_schema_migrated(db)
    assert db.read_bytes() == before   # never overwritten / recreated
