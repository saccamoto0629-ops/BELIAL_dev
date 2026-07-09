import pathlib
import sys

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import market_bias_models as models  # noqa: E402
import market_bias_store as store  # noqa: E402


def make_record(**overrides):
    bias_result = {
        "market_bias": "Bull",
        "confidence": 0.72,
        "trend": "Bull",
        "risk": "Risk-On",
        "risk_confidence": 0.6,
        "volatility": "Normal",
        "volatility_confidence": 0.3,
        "reasons": ["S&P500 is rising"],
        "risk_reasons": ["VIX is below 18 (risk-on)"],
        "volatility_reasons": [],
        "warnings": [],
    }
    record = models.create_market_bias_record(bias_result)
    record.update(overrides)
    return record


def test_save_and_get_market_bias_round_trips(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    record = make_record(evaluated_at="2026-07-05T12:00:00Z")

    store.save_market_bias(record, db_path=db_path)
    fetched = store.get_market_bias_as_of("GLOBAL", "1h", "2026-07-05T12:00:00Z", db_path=db_path)

    assert fetched is not None
    assert fetched["market_bias_id"] == record["market_bias_id"]
    assert fetched["market_bias"] == "Bull"
    assert fetched["risk"] == "Risk-On"
    assert fetched["confidence"] == 0.72


def test_get_market_bias_as_of_returns_none_when_db_file_missing(tmp_path):
    db_path = tmp_path / "does_not_exist.sqlite3"

    result = store.get_market_bias_as_of("GLOBAL", "1h", "2026-07-05T12:00:00Z", db_path=db_path)

    assert result is None
    assert not db_path.exists()  # read path must never create the DB as a side effect


def test_default_write_path_honors_market_bias_db_path_env(tmp_path, monkeypatch):
    env_db_path = tmp_path / "env" / "market_bias.sqlite3"
    monkeypatch.setenv("MARKET_BIAS_DB_PATH", str(env_db_path))

    store.save_market_bias(make_record(evaluated_at="2026-07-05T12:00:00Z"))

    assert env_db_path.exists()
    result = store.get_market_bias_as_of("GLOBAL", "1h", "2026-07-05T12:00:00Z")
    assert result["market_bias"] == "Bull"


def test_default_read_path_honors_market_bias_db_path_env_without_creating_db(tmp_path, monkeypatch):
    env_db_path = tmp_path / "missing" / "market_bias.sqlite3"
    monkeypatch.setenv("MARKET_BIAS_DB_PATH", str(env_db_path))

    result = store.get_market_bias_as_of("GLOBAL", "1h", "2026-07-05T12:00:00Z")

    assert result is None
    assert not env_db_path.exists()


def test_get_market_bias_as_of_returns_none_for_unknown_symbol_or_timeframe(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    store.save_market_bias(make_record(evaluated_at="2026-07-05T12:00:00Z"), db_path=db_path)

    assert store.get_market_bias_as_of("EUR_USD", "1h", "2026-07-05T12:00:00Z", db_path=db_path) is None
    assert store.get_market_bias_as_of("GLOBAL", "4h", "2026-07-05T12:00:00Z", db_path=db_path) is None


def test_get_market_bias_as_of_never_leaks_a_future_record(tmp_path):
    """Backtest correctness: a lookup 'at' T must never see a bias evaluated after T."""
    db_path = tmp_path / "market_bias.sqlite3"
    earlier = make_record(
        market_bias_id="earlier",
        evaluated_at="2026-07-05T10:00:00Z",
        market_bias="Neutral",
    )
    later = make_record(
        market_bias_id="later",
        evaluated_at="2026-07-05T14:00:00Z",
        market_bias="Bear",
    )
    store.save_market_bias(earlier, db_path=db_path)
    store.save_market_bias(later, db_path=db_path)

    # Requested "as of" time sits strictly between the two records.
    result = store.get_market_bias_as_of("GLOBAL", "1h", "2026-07-05T12:00:00Z", db_path=db_path)

    assert result["market_bias_id"] == "earlier"
    assert result["market_bias"] == "Neutral"


def test_get_market_bias_as_of_picks_the_latest_record_at_or_before_the_time(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    store.save_market_bias(
        make_record(market_bias_id="a", evaluated_at="2026-07-05T09:00:00Z", market_bias="Neutral"),
        db_path=db_path,
    )
    store.save_market_bias(
        make_record(market_bias_id="b", evaluated_at="2026-07-05T11:00:00Z", market_bias="Bull"),
        db_path=db_path,
    )

    result = store.get_market_bias_as_of("GLOBAL", "1h", "2026-07-05T11:00:00Z", db_path=db_path)

    assert result["market_bias_id"] == "b"


def test_get_market_bias_as_of_is_independent_per_symbol_and_timeframe(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    store.save_market_bias(
        make_record(
            market_bias_id="usdjpy-1h",
            symbol="USD_JPY",
            timeframe="1h",
            evaluated_at="2026-07-05T10:00:00Z",
            market_bias="Bear",
        ),
        db_path=db_path,
    )
    store.save_market_bias(
        make_record(
            market_bias_id="global-1h",
            symbol="GLOBAL",
            timeframe="1h",
            evaluated_at="2026-07-05T10:00:00Z",
            market_bias="Bull",
        ),
        db_path=db_path,
    )

    usdjpy_result = store.get_market_bias_as_of("USD_JPY", "1h", "2026-07-05T12:00:00Z", db_path=db_path)
    global_result = store.get_market_bias_as_of("GLOBAL", "1h", "2026-07-05T12:00:00Z", db_path=db_path)

    assert usdjpy_result["market_bias"] == "Bear"
    assert global_result["market_bias"] == "Bull"


def test_list_market_bias_orders_newest_first(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    store.save_market_bias(
        make_record(market_bias_id="a", evaluated_at="2026-07-05T09:00:00Z"), db_path=db_path
    )
    store.save_market_bias(
        make_record(market_bias_id="b", evaluated_at="2026-07-05T11:00:00Z"), db_path=db_path
    )

    rows = store.list_market_bias(db_path=db_path)

    assert [row["market_bias_id"] for row in rows] == ["b", "a"]


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"

    store.init_db(db_path)
    store.init_db(db_path)

    assert store.list_market_bias(db_path=db_path) == []
