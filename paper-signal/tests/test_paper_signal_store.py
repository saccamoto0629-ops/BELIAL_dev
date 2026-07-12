import pathlib
import sqlite3
import sys
from datetime import UTC, datetime, timedelta

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import paper_signal_store as store  # noqa: E402
import signal_models as sm  # noqa: E402

CLASSIFICATION_COLUMNS = ("signal_type", "trigger_type", "alert_source", "strategy_profile")


def _column_names(db_path):
    with sqlite3.connect(db_path) as connection:
        return {row[1] for row in connection.execute("PRAGMA table_info(paper_signals)")}


def _ordered_column_names(db_path):
    """Column names of paper_signals in physical (cid) order."""
    with sqlite3.connect(db_path) as connection:
        return [row[1] for row in connection.execute("PRAGMA table_info(paper_signals)")]


# The exact pre-Issue-#27 schema (13 columns, no classification columns).
_LEGACY_SCHEMA_SQL = """
CREATE TABLE paper_signals (
    signal_id TEXT PRIMARY KEY,
    created_at_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    side TEXT NOT NULL,
    market_bias TEXT NOT NULL,
    confidence REAL NOT NULL,
    macd_state TEXT NOT NULL,
    trend TEXT NOT NULL,
    state TEXT NOT NULL,
    freshness TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL
)
"""


def _create_legacy_db(db_path):
    """Create a pre-Issue-#27 DB with one legacy row and return that row's id."""
    with sqlite3.connect(db_path) as connection:
        connection.execute(_LEGACY_SCHEMA_SQL)
        connection.execute(
            """
            INSERT INTO paper_signals
            (signal_id, created_at_utc, symbol, timeframe, side, market_bias,
             confidence, macd_state, trend, state, freshness, reason, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-1", "2026-07-01T00:00:00Z", "USDJPY", "1h", "long", "Bull",
                0.72, "Bullish", "Bull", "Inside band", "Fresh", "legacy reason", "paper_only",
            ),
        )
    return "legacy-1"


def make_signal(*, created_at_utc=None, **overrides):
    candidate = {
        "symbol": "USDJPY",
        "timeframe": "1h",
        "side": "long",
        "market_bias": "Bull",
        "confidence": 0.72,
        "macd_state": "Bullish",
        "trend": "Bull",
        "state": "Inside band",
        "freshness": "Fresh",
        "reason": "test reason",
    }
    candidate.update(overrides)
    return sm.create_paper_signal(candidate, created_at_utc=created_at_utc)


def test_save_and_list_signal_round_trips(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    signal = make_signal()

    store.save_signal(signal, db_path=db_path)
    rows = store.list_signals(db_path=db_path)

    assert len(rows) == 1
    assert rows[0]["signal_id"] == signal["signal_id"]
    assert rows[0]["status"] == "paper_only"


def test_has_recent_signal_true_within_window(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)
    signal = make_signal(created_at_utc=now.replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    store.save_signal(signal, db_path=db_path)

    later = now + timedelta(minutes=30)
    assert store.has_recent_signal(
        "USDJPY", "1h", "long", within_minutes=60, now_utc=later, db_path=db_path
    )


def test_has_recent_signal_false_outside_window(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)
    signal = make_signal(created_at_utc=now.replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    store.save_signal(signal, db_path=db_path)

    much_later = now + timedelta(minutes=61)
    assert not store.has_recent_signal(
        "USDJPY", "1h", "long", within_minutes=60, now_utc=much_later, db_path=db_path
    )


def test_has_recent_signal_false_for_different_side(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)
    signal = make_signal(
        side="long", created_at_utc=now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    store.save_signal(signal, db_path=db_path)

    assert not store.has_recent_signal(
        "USDJPY", "1h", "short", within_minutes=60, now_utc=now, db_path=db_path
    )


def test_has_recent_signal_false_when_db_is_empty(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"

    assert not store.has_recent_signal("USDJPY", "1h", "long", db_path=db_path)


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"

    store.init_db(db_path)
    store.init_db(db_path)

    assert store.list_signals(db_path=db_path) == []


# --- Issue #27: classification columns + idempotent migration ---------------


def test_new_db_has_classification_columns(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"

    store.init_db(db_path)

    columns = _column_names(db_path)
    for column in CLASSIFICATION_COLUMNS:
        assert column in columns


def test_init_db_migrates_a_legacy_schema_by_adding_missing_columns(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    legacy_id = _create_legacy_db(db_path)
    assert not (CLASSIFICATION_COLUMNS[0] in _column_names(db_path))  # sanity: legacy really lacks them

    store.init_db(db_path)

    columns = _column_names(db_path)
    for column in CLASSIFICATION_COLUMNS:
        assert column in columns

    # the pre-existing legacy row is backfilled with safe default values
    rows = store.list_signals(db_path=db_path)
    legacy_row = next(row for row in rows if row["signal_id"] == legacy_id)
    assert legacy_row["signal_type"] == "trend_follow"
    assert legacy_row["trigger_type"] == "macd_state_alignment"
    assert legacy_row["alert_source"] == "composite"
    assert legacy_row["strategy_profile"] == "market_bias_macd_alignment_v1"


def test_migration_is_safe_to_run_multiple_times(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    _create_legacy_db(db_path)

    # Running the migration repeatedly must never raise (e.g. "duplicate
    # column name") regardless of whether it starts from legacy or current.
    store.init_db(db_path)
    store.init_db(db_path)
    store.init_db(db_path)

    columns = _column_names(db_path)
    for column in CLASSIFICATION_COLUMNS:
        assert column in columns


def test_save_and_list_round_trips_classification_fields(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    signal = make_signal(
        signal_type="trend_follow",
        trigger_type="macd_state_alignment",
        alert_source="composite",
        strategy_profile="market_bias_macd_alignment_v1",
    )

    store.save_signal(signal, db_path=db_path)
    rows = store.list_signals(db_path=db_path)

    assert len(rows) == 1
    row = rows[0]
    assert row["signal_type"] == "trend_follow"
    assert row["trigger_type"] == "macd_state_alignment"
    assert row["alert_source"] == "composite"
    assert row["strategy_profile"] == "market_bias_macd_alignment_v1"


def test_save_signal_on_a_legacy_db_migrates_then_persists(tmp_path):
    # save_signal() calls init_db(), so writing a new signal into a legacy DB
    # must transparently migrate the schema first, then store all four fields.
    db_path = tmp_path / "paper_signals.sqlite3"
    _create_legacy_db(db_path)

    signal = make_signal(side="short")
    store.save_signal(signal, db_path=db_path)

    rows = store.list_signals(db_path=db_path)
    new_row = next(row for row in rows if row["signal_id"] == signal["signal_id"])
    assert new_row["signal_type"] == "trend_follow"
    assert new_row["strategy_profile"] == "market_bias_macd_alignment_v1"


# --- Issue #27 review Major 2: new vs migrated schema column order matches ----


def test_new_and_migrated_db_have_identical_column_order(tmp_path):
    # A fresh DB (CREATE TABLE) and a legacy DB brought up to date via ALTER
    # TABLE must end with exactly the same ordered column-name list, so the
    # physical row layout never depends on how a given DB came to be.
    new_db = tmp_path / "new.sqlite3"
    migrated_db = tmp_path / "migrated.sqlite3"

    store.init_db(new_db)  # fresh CREATE TABLE path
    _create_legacy_db(migrated_db)  # pre-Issue-#27 schema...
    store.init_db(migrated_db)  # ...then ALTER-migrated

    new_order = _ordered_column_names(new_db)
    migrated_order = _ordered_column_names(migrated_db)

    assert new_order == migrated_order
    # status, then the four Issue #27 classification columns, then the Issue #41
    # target_candle_utc column, are the trailing columns in the documented final
    # order -- identical whether the DB was freshly created or ALTER-migrated.
    assert new_order[-6:] == [
        "status",
        "signal_type",
        "trigger_type",
        "alert_source",
        "strategy_profile",
        "target_candle_utc",
    ]


# --- Issue #27 review Minor 1: NULL/empty backfill on a partial schema --------


def _create_partial_schema_db_with_bad_values(db_path):
    """A schema that already has the four columns but as nullable TEXT holding
    NULL / empty strings (an intermediate state some other process could leave).
    Returns (null_row_id, empty_row_id)."""
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE paper_signals (
                signal_id TEXT PRIMARY KEY,
                created_at_utc TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                side TEXT NOT NULL,
                market_bias TEXT NOT NULL,
                confidence REAL NOT NULL,
                macd_state TEXT NOT NULL,
                trend TEXT NOT NULL,
                state TEXT NOT NULL,
                freshness TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                signal_type TEXT,
                trigger_type TEXT,
                alert_source TEXT,
                strategy_profile TEXT
            )
            """
        )
        base = ("2026-07-01T00:00:00Z", "USDJPY", "1h", "long", "Bull", 0.72,
                "Bullish", "Bull", "Inside band", "Fresh", "reason", "paper_only")
        # one row with NULLs, one row with empty strings
        connection.execute(
            "INSERT INTO paper_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("null-row", *base, None, None, None, None),
        )
        connection.execute(
            "INSERT INTO paper_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("empty-row", *base, "", "", "", ""),
        )
    return "null-row", "empty-row"


def test_init_db_backfills_null_and_empty_classification_values(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    null_id, empty_id = _create_partial_schema_db_with_bad_values(db_path)

    store.init_db(db_path)

    rows = {row["signal_id"]: row for row in store.list_signals(db_path=db_path)}
    for row_id in (null_id, empty_id):
        row = rows[row_id]
        assert row["signal_type"] == "trend_follow"
        assert row["trigger_type"] == "macd_state_alignment"
        assert row["alert_source"] == "composite"
        assert row["strategy_profile"] == "market_bias_macd_alignment_v1"


def test_backfill_is_idempotent_and_preserves_real_values(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    _create_partial_schema_db_with_bad_values(db_path)
    # a row carrying a real (non-default) classification value must survive
    with sqlite3.connect(db_path) as connection:
        base = ("2026-07-02T00:00:00Z", "USDJPY", "1h", "long", "Bull", 0.72,
                "Bullish", "Bull", "Inside band", "Fresh", "reason", "paper_only")
        connection.execute(
            "INSERT INTO paper_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("real-row", *base, "mean_reversion", "macd_state_alignment", "composite", "custom_profile_v2"),
        )

    store.init_db(db_path)
    store.init_db(db_path)  # idempotent: second run must not change anything

    rows = {row["signal_id"]: row for row in store.list_signals(db_path=db_path)}
    real = rows["real-row"]
    assert real["signal_type"] == "mean_reversion"  # never overwritten by the default
    assert real["strategy_profile"] == "custom_profile_v2"


# --- Issue #41: target-candle column, migration, candle-keyed idempotency -----


def test_new_db_has_target_candle_column(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    store.init_db(db_path)
    assert "target_candle_utc" in _column_names(db_path)


def test_legacy_db_migrates_to_add_target_candle_column(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    _create_legacy_db(db_path)
    assert "target_candle_utc" not in _column_names(db_path)  # sanity

    store.init_db(db_path)

    assert "target_candle_utc" in _column_names(db_path)


def test_save_signal_for_candle_inserts_once_then_dedupes(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    candle = "2026-07-05T12:00:00Z"

    first = store.save_signal_for_candle(make_signal(), target_candle_utc=candle, db_path=db_path)
    # A brand-new signal_id but the same (strategy_profile, symbol, timeframe,
    # target candle) must be rejected by the UNIQUE index.
    second = store.save_signal_for_candle(make_signal(), target_candle_utc=candle, db_path=db_path)

    assert first is True
    assert second is False
    rows = store.list_signals(db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["target_candle_utc"] == candle


def test_save_signal_for_candle_allows_different_candles(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    assert store.save_signal_for_candle(make_signal(), target_candle_utc="2026-07-05T12:00:00Z", db_path=db_path)
    assert store.save_signal_for_candle(make_signal(), target_candle_utc="2026-07-05T13:00:00Z", db_path=db_path)
    assert len(store.list_signals(db_path=db_path)) == 2


def test_signal_exists_for_candle_is_read_only(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    # No DB yet: the probe must return False WITHOUT creating the file.
    assert not store.signal_exists_for_candle(
        "market_bias_macd_alignment_v1", "USDJPY", "1h", "2026-07-05T12:00:00Z", db_path=db_path
    )
    assert not db_path.exists()

    store.save_signal_for_candle(make_signal(), target_candle_utc="2026-07-05T12:00:00Z", db_path=db_path)
    assert store.signal_exists_for_candle(
        "market_bias_macd_alignment_v1", "USDJPY", "1h", "2026-07-05T12:00:00Z", db_path=db_path
    )
    assert not store.signal_exists_for_candle(
        "market_bias_macd_alignment_v1", "USDJPY", "1h", "2026-07-05T99:00:00Z", db_path=db_path
    )
