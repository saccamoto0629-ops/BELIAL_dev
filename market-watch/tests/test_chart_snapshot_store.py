"""Issue #55: Chart Snapshot selection + Paper Signal relation persistence.

Covers chart_snapshot_store.find_snapshot_for_candle (symbol/timeframe exact
match, never-future target-candle selection, deterministic tie-breaking, UTC
normalization, malformed-timestamp safety, read-only guarantee) and the
paper_signal_chart_snapshots link table (idempotent migration + INSERT OR
IGNORE relation saves).
"""

import pathlib
import sqlite3
import sys

import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import chart_collector as collector  # noqa: E402
import chart_snapshot_store as store  # noqa: E402

TARGET = "2026-07-05T12:00:00Z"


def make_db(tmp_path):
    db_path = tmp_path / "chart_data.sqlite3"
    collector.init_db(db_path)
    return db_path


def insert_snapshot(db_path, *, snapshot_id, symbol="USDJPY", timeframe="1h", candle_time=TARGET, captured_at=None, snapshot_at="2026-07-05T12:05:00Z", s3_key=None):
    return collector.insert_chart_snapshot(
        db_path,
        symbol=symbol,
        timeframe=timeframe,
        snapshot_at=snapshot_at,
        s3_bucket="belial-chart-snapshots",
        s3_key=s3_key or f"charts/{symbol}/{timeframe}/{snapshot_id}.png",
        captured_at=captured_at,
        candle_time=candle_time,
        snapshot_id=snapshot_id,
    )


def find(db_path, *, symbol="USDJPY", timeframe="1h", target=TARGET):
    return store.find_snapshot_for_candle(db_path, symbol=symbol, timeframe=timeframe, target_candle_utc=target)


# --- Snapshot selection -------------------------------------------------------


def test_finds_snapshot_matching_symbol_and_timeframe(tmp_path):
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-1")

    found = find(db_path)

    assert found is not None
    assert found["id"] == "snap-1"
    assert found["s3_key"] == "charts/USDJPY/1h/snap-1.png"


def test_does_not_select_other_symbol(tmp_path):
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-xau", symbol="XAUUSD")

    assert find(db_path, symbol="USDJPY") is None


def test_does_not_select_other_timeframe(tmp_path):
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-4h", timeframe="4h")

    assert find(db_path, timeframe="1h") is None


def test_selects_snapshot_at_exact_target_candle(tmp_path):
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-older", candle_time="2026-07-05T11:00:00Z")
    insert_snapshot(db_path, snapshot_id="snap-exact", candle_time=TARGET)

    assert find(db_path)["id"] == "snap-exact"


def test_selects_nearest_snapshot_before_target(tmp_path):
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-old", candle_time="2026-07-05T09:00:00Z")
    insert_snapshot(db_path, snapshot_id="snap-near", candle_time="2026-07-05T11:00:00Z")

    assert find(db_path)["id"] == "snap-near"


def test_never_selects_future_snapshot(tmp_path):
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-past", candle_time="2026-07-05T11:00:00Z")
    insert_snapshot(db_path, snapshot_id="snap-future", candle_time="2026-07-05T13:00:00Z")

    assert find(db_path)["id"] == "snap-past"


def test_only_future_snapshots_yield_none(tmp_path):
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-future", candle_time="2026-07-05T13:00:00Z")

    assert find(db_path) is None


def test_no_snapshots_yield_none(tmp_path):
    db_path = make_db(tmp_path)

    assert find(db_path) is None


def test_missing_db_yields_none_and_is_never_created(tmp_path):
    db_path = tmp_path / "chart_data.sqlite3"

    assert find(db_path) is None
    assert not db_path.exists()  # read-only: the lookup must not create the DB


def test_same_candle_time_picks_newest_captured_at_deterministically(tmp_path):
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-early", captured_at="2026-07-05T12:01:00Z")
    insert_snapshot(db_path, snapshot_id="snap-late", captured_at="2026-07-05T12:09:00Z")

    for _ in range(3):
        assert find(db_path)["id"] == "snap-late"


def test_same_candle_time_falls_back_to_snapshot_at_when_captured_at_missing(tmp_path):
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-early", captured_at=None, snapshot_at="2026-07-05T12:01:00Z")
    insert_snapshot(db_path, snapshot_id="snap-late", captured_at=None, snapshot_at="2026-07-05T12:09:00Z")

    assert find(db_path)["id"] == "snap-late"


def test_utc_offset_and_z_forms_compare_as_the_same_instant(tmp_path):
    # persist_snapshot_images stores candle_time as "+00:00" isoformat while the
    # Paper Signal target candle is the canonical "...Z" form: the comparison
    # must treat them as equal instants, not unequal strings.
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-offset", candle_time="2026-07-05T12:00:00+00:00")

    assert find(db_path, target=TARGET)["id"] == "snap-offset"


def test_non_utc_offset_is_normalized_before_comparison(tmp_path):
    # 21:00 JST == 12:00 UTC: qualifies; 22:00 JST == 13:00 UTC: future, excluded.
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-jst-ok", candle_time="2026-07-05T21:00:00+09:00")
    insert_snapshot(db_path, snapshot_id="snap-jst-future", candle_time="2026-07-05T22:00:00+09:00")

    assert find(db_path)["id"] == "snap-jst-ok"


def test_malformed_or_null_candle_time_is_never_selected(tmp_path):
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-null", candle_time=None)
    insert_snapshot(db_path, snapshot_id="snap-garbage", candle_time="not-a-timestamp")
    insert_snapshot(db_path, snapshot_id="snap-valid", candle_time="2026-07-05T10:00:00Z")

    assert find(db_path)["id"] == "snap-valid"


def test_only_malformed_candle_times_yield_none(tmp_path):
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-null", candle_time=None)
    insert_snapshot(db_path, snapshot_id="snap-garbage", candle_time="not-a-timestamp")

    assert find(db_path) is None


def test_malformed_target_candle_yields_none(tmp_path):
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-1")

    assert find(db_path, target="not-a-timestamp") is None
    assert find(db_path, target=None) is None


def test_mixed_id_types_still_tie_break_deterministically(tmp_path):
    # A legacy/typeless schema can hold an INTEGER id next to a TEXT id. The
    # tie-break must not raise (str vs int comparison) and must stay
    # deterministic: ids compare as text, so "z" > "2".
    db_path = make_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        for row_id in (2, "z"):
            conn.execute(
                """
                INSERT INTO chart_snapshots
                (id, symbol, timeframe, snapshot_at, s3_bucket, s3_key, captured_at, candle_time, created_at)
                VALUES (?, 'USDJPY', '1h', '2026-07-05T12:05:00Z', 'bucket', 'key.png',
                        '2026-07-05T12:05:00Z', ?, '2026-07-05T12:05:00Z')
                """,
                (row_id, TARGET),
            )

    for _ in range(3):
        assert find(db_path)["id"] == "z"


def test_db_path_with_uri_reserved_characters_is_still_readable(tmp_path):
    # "#" and " " are URI-reserved: an unescaped read-only URI would silently
    # truncate the filename and degrade a perfectly good DB to "not found".
    db_dir = tmp_path / "data dir"
    db_dir.mkdir()
    db_path = db_dir / "chart #1.sqlite3"
    collector.init_db(db_path)
    insert_snapshot(db_path, snapshot_id="snap-1")
    store.link_paper_signal_chart_snapshot(db_path, paper_signal_id="sig-1", chart_snapshot_id="snap-1")

    assert find(db_path)["id"] == "snap-1"
    assert len(store.list_paper_signal_links(db_path, "sig-1")) == 1


# --- Relation persistence (paper_signal_chart_snapshots) ----------------------


def test_link_is_saved(tmp_path):
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-1")

    inserted = store.link_paper_signal_chart_snapshot(db_path, paper_signal_id="sig-1", chart_snapshot_id="snap-1")

    assert inserted is True
    links = store.list_paper_signal_links(db_path, "sig-1")
    assert len(links) == 1
    assert links[0]["chart_snapshot_id"] == "snap-1"
    assert links[0]["created_at"].endswith("Z")


def test_duplicate_link_is_not_stored_twice(tmp_path):
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-1")

    first = store.link_paper_signal_chart_snapshot(db_path, paper_signal_id="sig-1", chart_snapshot_id="snap-1")
    second = store.link_paper_signal_chart_snapshot(db_path, paper_signal_id="sig-1", chart_snapshot_id="snap-1")

    assert first is True
    assert second is False
    assert len(store.list_paper_signal_links(db_path, "sig-1")) == 1


def test_different_snapshot_ids_store_separate_links(tmp_path):
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-1")
    insert_snapshot(db_path, snapshot_id="snap-2", captured_at="2026-07-05T13:05:00Z")

    store.link_paper_signal_chart_snapshot(db_path, paper_signal_id="sig-1", chart_snapshot_id="snap-1")
    store.link_paper_signal_chart_snapshot(db_path, paper_signal_id="sig-1", chart_snapshot_id="snap-2")

    links = store.list_paper_signal_links(db_path, "sig-1")
    assert [link["chart_snapshot_id"] for link in links] == ["snap-1", "snap-2"]


def test_init_db_owns_the_link_table_alongside_the_chart_schema(tmp_path):
    # The link table is part of the regular Chart DB schema management
    # (chart_collector.init_db), next to alert_chart_snapshots.
    db_path = make_db(tmp_path)

    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "alert_chart_snapshots" in tables
    assert "paper_signal_chart_snapshots" in tables


def test_link_table_migration_is_idempotent_and_preserves_data(tmp_path):
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-1")
    store.link_paper_signal_chart_snapshot(db_path, paper_signal_id="sig-1", chart_snapshot_id="snap-1")

    with sqlite3.connect(db_path) as conn:
        schema_before = sorted(conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall())

    # Re-running the migration on an existing, populated DB changes nothing.
    collector.init_db(db_path)
    collector.init_db(db_path)

    with sqlite3.connect(db_path) as conn:
        schema_after = sorted(conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall())
        snapshots = conn.execute("SELECT COUNT(*) FROM chart_snapshots").fetchone()[0]
    assert schema_after == schema_before
    assert snapshots == 1
    assert len(store.list_paper_signal_links(db_path, "sig-1")) == 1


def test_incomplete_legacy_link_table_is_repaired_not_left_broken(tmp_path):
    # A same-name table missing columns (partial legacy schema) must be
    # repaired by the migration; otherwise every relation insert fails forever.
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-1")
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE paper_signal_chart_snapshots")
        conn.execute(
            "CREATE TABLE paper_signal_chart_snapshots (paper_signal_id TEXT, chart_snapshot_id TEXT)"
        )

    inserted = store.link_paper_signal_chart_snapshot(db_path, paper_signal_id="sig-1", chart_snapshot_id="snap-1")

    assert inserted is True
    links = store.list_paper_signal_links(db_path, "sig-1")
    assert [link["chart_snapshot_id"] for link in links] == ["snap-1"]
    # Idempotency holds even though the legacy table has no primary key.
    assert store.link_paper_signal_chart_snapshot(db_path, paper_signal_id="sig-1", chart_snapshot_id="snap-1") is False
    assert len(store.list_paper_signal_links(db_path, "sig-1")) == 1


def test_link_missing_db_raises_without_creating_it(tmp_path):
    db_path = tmp_path / "chart_data.sqlite3"

    with pytest.raises(store.ChartLinkError):
        store.link_paper_signal_chart_snapshot(db_path, paper_signal_id="sig-1", chart_snapshot_id="snap-1")
    assert not db_path.exists()


def test_link_uninitialized_db_raises_and_gains_no_link_table(tmp_path):
    # A DB without chart_snapshots is not a Chart DB: refuse instead of
    # conjuring a partial DB that holds only the link table.
    db_path = tmp_path / "not_a_chart_db.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE unrelated (x TEXT)")

    with pytest.raises(store.ChartLinkError):
        store.link_paper_signal_chart_snapshot(db_path, paper_signal_id="sig-1", chart_snapshot_id="snap-1")

    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "paper_signal_chart_snapshots" not in tables


def test_link_nonexistent_snapshot_raises_and_stores_no_orphan(tmp_path):
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-1")

    with pytest.raises(store.ChartLinkError):
        store.link_paper_signal_chart_snapshot(db_path, paper_signal_id="sig-1", chart_snapshot_id="no-such-snap")

    assert store.list_paper_signal_links(db_path, "sig-1") == []
    with sqlite3.connect(db_path) as conn:
        orphans = conn.execute("PRAGMA foreign_key_check(paper_signal_chart_snapshots)").fetchall()
    assert orphans == []


def test_link_write_leaves_foreign_keys_consistent(tmp_path):
    # The FK to chart_snapshots(id) is enforced on the write connection
    # (PRAGMA foreign_keys=ON): after real links, a full FK check is clean.
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-1")
    store.link_paper_signal_chart_snapshot(db_path, paper_signal_id="sig-1", chart_snapshot_id="snap-1")

    with sqlite3.connect(db_path) as conn:
        orphans = conn.execute("PRAGMA foreign_key_check(paper_signal_chart_snapshots)").fetchall()
    assert orphans == []


def test_list_links_on_missing_db_returns_empty_without_creating_it(tmp_path):
    db_path = tmp_path / "chart_data.sqlite3"

    assert store.list_paper_signal_links(db_path, "sig-1") == []
    assert not db_path.exists()
