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


def test_legacy_chart_snapshots_without_optional_columns_is_searchable(tmp_path):
    # A schema predating s3_bucket / image_s3_key / captured_at / snapshot_at
    # must still be searchable; the absent columns come back as None so the
    # caller can fall back (e.g. to the environment bucket).
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE chart_snapshots (id TEXT, symbol TEXT, timeframe TEXT, candle_time TEXT, s3_key TEXT)")
        conn.execute(
            "INSERT INTO chart_snapshots VALUES ('snap-1', 'USDJPY', '1h', ?, 'key.png')", (TARGET,)
        )

    found = find(db_path)

    assert found["id"] == "snap-1"
    assert found["s3_key"] == "key.png"
    assert found["s3_bucket"] is None
    assert found["image_s3_key"] is None


def test_missing_required_column_raises_schema_error(tmp_path):
    # No s3_key: this table cannot serve an attachment. That is a schema error,
    # not an empty search result.
    db_path = tmp_path / "broken.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE chart_snapshots (id TEXT, symbol TEXT, timeframe TEXT, candle_time TEXT)")
        conn.execute("INSERT INTO chart_snapshots VALUES ('snap-1', 'USDJPY', '1h', ?)", (TARGET,))

    with pytest.raises(store.ChartSchemaError):
        find(db_path)


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


# Canonical link-table shape: {column: (type, notnull, pk position)}.
CANONICAL_LINKS_INFO = {
    "paper_signal_id": ("TEXT", 1, 1),
    "chart_snapshot_id": ("TEXT", 1, 2),
    "created_at": ("TEXT", 1, 0),
}


def _links_table_info(db_path):
    with sqlite3.connect(db_path) as conn:
        return {
            row[1]: (row[2].upper(), row[3], row[5])
            for row in conn.execute("PRAGMA table_info(paper_signal_chart_snapshots)")
        }


def _links_foreign_keys(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute("PRAGMA foreign_key_list(paper_signal_chart_snapshots)").fetchall()


def _replace_with_constraintless_legacy_table(db_path, rows):
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE paper_signal_chart_snapshots")
        conn.execute(
            "CREATE TABLE paper_signal_chart_snapshots (paper_signal_id TEXT, chart_snapshot_id TEXT, created_at TEXT)"
        )
        conn.executemany("INSERT INTO paper_signal_chart_snapshots VALUES (?, ?, ?)", rows)


def test_incomplete_legacy_link_table_is_rebuilt_not_left_broken(tmp_path):
    # A same-name table missing columns (partial legacy schema) must be
    # rebuilt by the migration; otherwise every relation insert fails forever.
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
    assert store.link_paper_signal_chart_snapshot(db_path, paper_signal_id="sig-1", chart_snapshot_id="snap-1") is False
    assert len(store.list_paper_signal_links(db_path, "sig-1")) == 1
    assert _links_table_info(db_path) == CANONICAL_LINKS_INFO


def test_constraintless_legacy_table_is_rebuilt_to_canonical_schema(tmp_path):
    # No PK, no NOT NULL, no FK, plus accumulated garbage: duplicates, an
    # orphan, a NULL-keyed row, and an empty created_at. The rebuild keeps the
    # valid relations only, on the canonical constraints.
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-1")
    insert_snapshot(db_path, snapshot_id="snap-2")
    _replace_with_constraintless_legacy_table(
        db_path,
        [
            ("sig-1", "snap-1", "2026-07-01T00:00:00Z"),
            ("sig-1", "snap-1", "2026-07-02T00:00:00Z"),  # duplicate relation
            ("sig-2", "ghost", "2026-07-01T00:00:00Z"),   # orphan (no such snapshot)
            (None, "snap-2", "2026-07-01T00:00:00Z"),     # NULL key
            ("sig-3", "snap-2", ""),                       # created_at to backfill
        ],
    )

    collector.init_db(db_path)  # regular schema management triggers the rebuild

    # Canonical constraints restored: columns/NOT NULL/composite PK and one FK.
    assert _links_table_info(db_path) == CANONICAL_LINKS_INFO
    fks = _links_foreign_keys(db_path)
    assert len(fks) == 1
    assert (fks[0][2], fks[0][3], fks[0][4]) == ("chart_snapshots", "chart_snapshot_id", "id")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA foreign_key_check(paper_signal_chart_snapshots)").fetchall() == []
        total = conn.execute("SELECT COUNT(*) FROM paper_signal_chart_snapshots").fetchone()[0]
    # Existing valid data survives; duplicates collapse to one; orphan/NULL gone.
    assert total == 2
    assert [link["chart_snapshot_id"] for link in store.list_paper_signal_links(db_path, "sig-1")] == ["snap-1"]
    assert store.list_paper_signal_links(db_path, "sig-2") == []
    sig3 = store.list_paper_signal_links(db_path, "sig-3")
    assert [link["chart_snapshot_id"] for link in sig3] == ["snap-2"]
    assert sig3[0]["created_at"]  # empty created_at was backfilled


def test_rebuild_migration_is_idempotent(tmp_path):
    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-1")
    _replace_with_constraintless_legacy_table(db_path, [("sig-1", "snap-1", "2026-07-01T00:00:00Z")])

    collector.init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        schema_after_first = sorted(conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall())
        rows_after_first = sorted(conn.execute("SELECT * FROM paper_signal_chart_snapshots").fetchall())

    collector.init_db(db_path)
    collector.init_db(db_path)

    with sqlite3.connect(db_path) as conn:
        schema_after_more = sorted(conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall())
        rows_after_more = sorted(conn.execute("SELECT * FROM paper_signal_chart_snapshots").fetchall())
    assert schema_after_more == schema_after_first
    assert rows_after_more == rows_after_first


def test_concurrent_links_after_rebuild_store_one_row(tmp_path):
    # After migrating a constraintless legacy table, the restored composite PK
    # keeps the relation unique even under genuinely concurrent writers.
    import threading

    db_path = make_db(tmp_path)
    insert_snapshot(db_path, snapshot_id="snap-1")
    _replace_with_constraintless_legacy_table(db_path, [])
    collector.init_db(db_path)

    n = 8
    barrier = threading.Barrier(n)
    results, lock = [], threading.Lock()

    def worker():
        barrier.wait()
        inserted = store.link_paper_signal_chart_snapshot(
            db_path, paper_signal_id="sig-1", chart_snapshot_id="snap-1"
        )
        with lock:
            results.append(inserted)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 1
    assert results.count(False) == n - 1
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
