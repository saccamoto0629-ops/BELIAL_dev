import json
import pathlib
import sqlite3
import sys

import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import backtest_models as models  # noqa: E402
import backtest_store as store  # noqa: E402


def make_run(**overrides):
    kwargs = {
        "run_name": "usdjpy-sma-cross",
        "symbol": "USD_JPY",
        "timeframe": "1h",
        "from_time": "2026-01-01T00:00:00Z",
        "to_time": "2026-06-01T00:00:00Z",
        "strategy_name": "sma_cross",
        "exit_policy": "fixed_rr_1_to_2",
        "config": {"sma_fast": 20, "sma_slow": 75, "threshold_pct": 0.05},
    }
    kwargs.update(overrides)
    return models.create_backtest_run_record(**kwargs)


def make_signal(run_id, **overrides):
    kwargs = {
        "signal_time": "2026-01-05T09:00:00Z",
        "symbol": "USD_JPY",
        "timeframe": "1h",
        "bias": "Bull",
        "confidence": 0.72,
        "direction": "long",
        "signal_type": "ma_touch",
        "trigger_type": "confirmed_candle",
        "reasons": ["Close touched SMA20", "Trend is Bull"],
        "entry_price": 150.0,
        "invalidation_price": 149.0,
        "take_profit_price": 152.0,
        "close_price": 151.5,
        "result_status": "win",
        "pnl": 1.5,
        "mfe": 1.8,
        "mae": -0.3,
        "closed_at": "2026-01-05T15:00:00Z",
        "close_reason": "take_profit",
        "exit_policy": "fixed_rr",
    }
    kwargs.update(overrides)
    return models.create_backtest_signal_record(run_id, **kwargs)


# --- init_db idempotency ----------------------------------------------------


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "backtest.sqlite3"

    store.init_db(db_path)
    store.init_db(db_path)

    assert store.list_backtest_runs(db_path=db_path) == []


def test_init_db_raises_clear_error_for_incomplete_existing_schema(tmp_path):
    db_path = tmp_path / "backtest.sqlite3"
    # Simulate a pre-existing, incompatible backtest_runs table (missing
    # config_json/exit_policy) created by something other than init_db().
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE backtest_runs (
                id TEXT PRIMARY KEY,
                run_name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                from_time TEXT NOT NULL,
                to_time TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

    with pytest.raises(store.BacktestSchemaError, match="config_json"):
        store.init_db(db_path)


def test_init_db_migrates_existing_backtest_signals_missing_exit_policy_column(tmp_path):
    db_path = tmp_path / "backtest.sqlite3"
    # Simulate a DB created before exit_policy existed on backtest_signals
    # (i.e. the Issue #19 schema, pre-#21 fix): otherwise-complete tables,
    # possibly with existing rows.
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE backtest_runs (
                id TEXT PRIMARY KEY,
                run_name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                from_time TEXT NOT NULL,
                to_time TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                exit_policy TEXT,
                config_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE backtest_signals (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                signal_time TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                bias TEXT,
                confidence REAL,
                direction TEXT,
                signal_type TEXT,
                trigger_type TEXT,
                reason_json TEXT,
                entry_price REAL,
                invalidation_price REAL,
                take_profit_price REAL,
                close_price REAL,
                result_status TEXT,
                pnl REAL,
                mfe REAL,
                mae REAL,
                closed_at TEXT,
                close_reason TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO backtest_runs (id, run_name, symbol, timeframe, from_time, to_time, "
            "strategy_name, exit_policy, config_json, created_at) VALUES "
            "('run-1', 'old-run', 'USD_JPY', '1h', '2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z', "
            "'sma_cross', 'fixed_rr', '{}', '2026-01-01T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO backtest_signals (id, run_id, signal_time, symbol, timeframe, created_at) "
            "VALUES ('signal-1', 'run-1', '2026-01-05T09:00:00Z', 'USD_JPY', '1h', '2026-01-05T09:00:00Z')"
        )

    # Must not raise: exit_policy is auto-migrated onto the existing table,
    # and pre-existing rows must still be readable afterward.
    store.init_db(db_path)

    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(backtest_signals)").fetchall()}
    assert "exit_policy" in columns

    signals = store.list_backtest_signals("run-1", db_path=db_path)
    assert len(signals) == 1
    assert signals[0]["id"] == "signal-1"
    assert signals[0]["exit_policy"] is None  # pre-existing row: nullable, not backfilled


# --- backtest_runs -----------------------------------------------------------


def test_save_and_get_backtest_run_round_trips(tmp_path):
    db_path = tmp_path / "backtest.sqlite3"
    run = make_run()

    store.save_backtest_run(run, db_path=db_path)
    fetched = store.get_backtest_run(run["id"], db_path=db_path)

    assert fetched is not None
    assert fetched["run_name"] == "usdjpy-sma-cross"
    assert fetched["symbol"] == "USD_JPY"
    assert fetched["strategy_name"] == "sma_cross"
    assert json.loads(fetched["config_json"]) == {"sma_fast": 20, "sma_slow": 75, "threshold_pct": 0.05}


def test_rerunning_creates_a_separate_run_not_an_overwrite(tmp_path):
    db_path = tmp_path / "backtest.sqlite3"
    first = make_run()
    second = make_run()  # same run_name/symbol/timeframe/strategy, fresh id

    store.save_backtest_run(first, db_path=db_path)
    store.save_backtest_run(second, db_path=db_path)

    assert first["id"] != second["id"]
    runs = store.list_backtest_runs(db_path=db_path)
    assert len(runs) == 2
    assert {row["id"] for row in runs} == {first["id"], second["id"]}


def test_get_backtest_run_returns_none_when_db_file_missing(tmp_path):
    db_path = tmp_path / "does_not_exist.sqlite3"

    result = store.get_backtest_run("some-id", db_path=db_path)

    assert result is None
    assert not db_path.exists()


def test_save_backtest_run_with_duplicate_id_raises_integrity_error(tmp_path):
    db_path = tmp_path / "backtest.sqlite3"
    run = make_run()
    store.save_backtest_run(run, db_path=db_path)

    duplicate = make_run(run_id=run["id"])  # reuse the same id
    with pytest.raises(sqlite3.IntegrityError):
        store.save_backtest_run(duplicate, db_path=db_path)

    # the original row must be untouched, not silently replaced
    runs = store.list_backtest_runs(db_path=db_path)
    assert len(runs) == 1
    assert runs[0]["id"] == run["id"]


def test_list_backtest_runs_returns_empty_when_db_file_missing(tmp_path):
    db_path = tmp_path / "does_not_exist.sqlite3"

    assert store.list_backtest_runs(db_path=db_path) == []
    assert not db_path.exists()


# --- backtest_signals ---------------------------------------------------------


def test_save_and_list_backtest_signals_round_trip_by_run_id(tmp_path):
    db_path = tmp_path / "backtest.sqlite3"
    run = make_run()
    store.save_backtest_run(run, db_path=db_path)
    signal = make_signal(run["id"])

    store.save_backtest_signal(signal, db_path=db_path)
    signals = store.list_backtest_signals(run["id"], db_path=db_path)

    assert len(signals) == 1
    assert signals[0]["id"] == signal["id"]
    assert signals[0]["run_id"] == run["id"]
    assert signals[0]["result_status"] == "win"
    assert signals[0]["pnl"] == 1.5
    assert signals[0]["mfe"] == 1.8
    assert signals[0]["mae"] == -0.3
    assert signals[0]["exit_policy"] == "fixed_rr"
    assert json.loads(signals[0]["reason_json"]) == ["Close touched SMA20", "Trend is Bull"]


# --- Issue #27 review Major 1: all four classification fields on Backtest Signal ---


def test_save_and_list_backtest_signals_round_trip_classification_fields(tmp_path):
    db_path = tmp_path / "backtest.sqlite3"
    run = make_run()
    store.save_backtest_run(run, db_path=db_path)
    signal = make_signal(
        run["id"],
        signal_type="trend_follow",
        trigger_type="macd_state_alignment",
        alert_source="composite",
        strategy_profile="market_bias_macd_alignment_v1",
    )

    store.save_backtest_signal(signal, db_path=db_path)
    signals = store.list_backtest_signals(run["id"], db_path=db_path)

    assert len(signals) == 1
    row = signals[0]
    assert row["signal_type"] == "trend_follow"
    assert row["trigger_type"] == "macd_state_alignment"
    assert row["alert_source"] == "composite"
    assert row["strategy_profile"] == "market_bias_macd_alignment_v1"


def _create_pre_issue27_backtest_db(db_path, *, signal_type=None, trigger_type=None):
    """A backtest DB whose backtest_signals table predates Issue #27's two new
    columns (alert_source / strategy_profile), with one pre-existing row.
    Returns the row id."""
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE backtest_runs (
                id TEXT PRIMARY KEY, run_name TEXT NOT NULL, symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL, from_time TEXT NOT NULL, to_time TEXT NOT NULL,
                strategy_name TEXT NOT NULL, exit_policy TEXT, config_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE backtest_signals (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, signal_time TEXT NOT NULL,
                symbol TEXT NOT NULL, timeframe TEXT NOT NULL, bias TEXT, confidence REAL,
                direction TEXT, signal_type TEXT, trigger_type TEXT, reason_json TEXT,
                entry_price REAL, invalidation_price REAL, take_profit_price REAL,
                close_price REAL, result_status TEXT, pnl REAL, mfe REAL, mae REAL,
                closed_at TEXT, close_reason TEXT, exit_policy TEXT, created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO backtest_runs (id, run_name, symbol, timeframe, from_time, to_time, "
            "strategy_name, exit_policy, config_json, created_at) VALUES "
            "('run-1', 'old-run', 'USD_JPY', '1h', '2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z', "
            "'sma_cross', 'fixed_rr', '{}', '2026-01-01T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO backtest_signals (id, run_id, signal_time, symbol, timeframe, "
            "signal_type, trigger_type, created_at) VALUES "
            "('signal-1', 'run-1', '2026-01-05T09:00:00Z', 'USD_JPY', '1h', ?, ?, '2026-01-05T09:00:00Z')",
            (signal_type, trigger_type),
        )
    return "signal-1"


def test_init_db_migrates_backtest_signals_missing_alert_source_and_strategy_profile(tmp_path):
    db_path = tmp_path / "backtest.sqlite3"
    _create_pre_issue27_backtest_db(db_path)  # signal_type/trigger_type NULL too
    before = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(backtest_signals)")}
    assert "alert_source" not in before and "strategy_profile" not in before

    store.init_db(db_path)

    after = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(backtest_signals)")}
    assert "alert_source" in after and "strategy_profile" in after

    # pre-existing row's NULL classification values are backfilled with defaults
    row = store.list_backtest_signals("run-1", db_path=db_path)[0]
    assert row["signal_type"] == "trend_follow"
    assert row["trigger_type"] == "macd_state_alignment"
    assert row["alert_source"] == "composite"
    assert row["strategy_profile"] == "market_bias_macd_alignment_v1"


def test_backtest_classification_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "backtest.sqlite3"
    _create_pre_issue27_backtest_db(db_path)

    store.init_db(db_path)
    store.init_db(db_path)
    store.init_db(db_path)  # must never raise "duplicate column name"

    columns = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(backtest_signals)")}
    for column in ("signal_type", "trigger_type", "alert_source", "strategy_profile"):
        assert column in columns


def test_backtest_backfill_only_touches_null_or_empty_values(tmp_path):
    # A pre-existing row carrying a real signal_type must not be overwritten by
    # the default; only NULL/empty are repaired.
    db_path = tmp_path / "backtest.sqlite3"
    _create_pre_issue27_backtest_db(db_path, signal_type="ma_touch", trigger_type="")

    store.init_db(db_path)

    row = store.list_backtest_signals("run-1", db_path=db_path)[0]
    assert row["signal_type"] == "ma_touch"  # real value preserved
    assert row["trigger_type"] == "macd_state_alignment"  # empty string backfilled
    assert row["alert_source"] == "composite"  # newly-added column backfilled


def test_save_backtest_signal_with_duplicate_id_raises_integrity_error(tmp_path):
    db_path = tmp_path / "backtest.sqlite3"
    run = make_run()
    store.save_backtest_run(run, db_path=db_path)
    signal = make_signal(run["id"])
    store.save_backtest_signal(signal, db_path=db_path)

    duplicate = make_signal(run["id"], signal_id=signal["id"], result_status="loss")
    with pytest.raises(sqlite3.IntegrityError):
        store.save_backtest_signal(duplicate, db_path=db_path)

    # the original row must be untouched, not silently replaced
    signals = store.list_backtest_signals(run["id"], db_path=db_path)
    assert len(signals) == 1
    assert signals[0]["result_status"] == "win"


def test_backtest_signals_are_isolated_by_run_id(tmp_path):
    db_path = tmp_path / "backtest.sqlite3"
    run_a = make_run(run_name="run-a")
    run_b = make_run(run_name="run-b")
    store.save_backtest_run(run_a, db_path=db_path)
    store.save_backtest_run(run_b, db_path=db_path)
    store.save_backtest_signal(make_signal(run_a["id"]), db_path=db_path)
    store.save_backtest_signal(make_signal(run_b["id"]), db_path=db_path)
    store.save_backtest_signal(make_signal(run_b["id"]), db_path=db_path)

    assert len(store.list_backtest_signals(run_a["id"], db_path=db_path)) == 1
    assert len(store.list_backtest_signals(run_b["id"], db_path=db_path)) == 2


def test_save_backtest_signals_bulk_helper(tmp_path):
    db_path = tmp_path / "backtest.sqlite3"
    run = make_run()
    store.save_backtest_run(run, db_path=db_path)
    signals = [make_signal(run["id"], signal_time=f"2026-01-0{i}T09:00:00Z") for i in range(1, 4)]

    ids = store.save_backtest_signals(signals, db_path=db_path)

    assert len(ids) == 3
    assert len(store.list_backtest_signals(run["id"], db_path=db_path)) == 3


def test_save_backtest_signals_rolls_back_entire_batch_on_partial_failure(tmp_path):
    db_path = tmp_path / "backtest.sqlite3"
    run = make_run()
    store.save_backtest_run(run, db_path=db_path)
    duplicate_id = "duplicate-signal-id"
    signals = [
        make_signal(run["id"], signal_id="signal-1", signal_time="2026-01-01T09:00:00Z"),
        make_signal(run["id"], signal_id="signal-2", signal_time="2026-01-02T09:00:00Z"),
        # third row reuses signal-1's id via a fresh id collision to trigger
        # a mid-batch IntegrityError (id already used within this same batch)
        make_signal(run["id"], signal_id="signal-1", signal_time="2026-01-03T09:00:00Z"),
        make_signal(run["id"], signal_id=duplicate_id, signal_time="2026-01-04T09:00:00Z"),
    ]

    with pytest.raises(sqlite3.IntegrityError):
        store.save_backtest_signals(signals, db_path=db_path)

    # none of the batch should have been persisted -- not even the rows
    # that came before the failing one.
    assert store.list_backtest_signals(run["id"], db_path=db_path) == []


def test_list_backtest_signals_returns_empty_when_db_file_missing(tmp_path):
    db_path = tmp_path / "does_not_exist.sqlite3"

    assert store.list_backtest_signals("some-run", db_path=db_path) == []
    assert not db_path.exists()


def test_foreign_key_constraint_rejects_signal_for_unknown_run(tmp_path):
    db_path = tmp_path / "backtest.sqlite3"
    store.init_db(db_path)
    orphan_signal = make_signal("no-such-run-id")

    with pytest.raises(sqlite3.IntegrityError):
        store.save_backtest_signal(orphan_signal, db_path=db_path)


# --- separation from paper_signals -------------------------------------------


def test_backtest_db_is_a_separate_file_from_paper_signals(tmp_path):
    backtest_db = tmp_path / "backtest_results.sqlite3"
    run = make_run()
    store.save_backtest_run(run, db_path=backtest_db)
    store.save_backtest_signal(make_signal(run["id"]), db_path=backtest_db)

    with sqlite3.connect(backtest_db) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

    assert "backtest_runs" in tables
    assert "backtest_signals" in tables
    assert "paper_signals" not in tables


# --- env-configurable default path ------------------------------------------


def test_default_write_path_honors_backtest_db_path_env(tmp_path, monkeypatch):
    env_db_path = tmp_path / "env" / "backtest_results.sqlite3"
    monkeypatch.setenv("BACKTEST_DB_PATH", str(env_db_path))
    run = make_run()

    store.save_backtest_run(run)

    assert env_db_path.exists()
    fetched = store.get_backtest_run(run["id"])
    assert fetched["run_name"] == "usdjpy-sma-cross"


def test_default_read_path_honors_backtest_db_path_env_without_creating_db(tmp_path, monkeypatch):
    env_db_path = tmp_path / "missing" / "backtest_results.sqlite3"
    monkeypatch.setenv("BACKTEST_DB_PATH", str(env_db_path))

    result = store.list_backtest_runs()

    assert result == []
    assert not env_db_path.exists()
