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


# =============================================================================
# Issue #40: per-symbol effective bias (symbol-specific preferred, GLOBAL fallback)
# =============================================================================


def _seed(db_path, *, symbol, timeframe, evaluated_at, market_bias, bias_id):
    store.save_market_bias(
        make_record(
            market_bias_id=bias_id,
            symbol=symbol,
            timeframe=timeframe,
            evaluated_at=evaluated_at,
            market_bias=market_bias,
        ),
        db_path=db_path,
    )


def test_effective_bias_prefers_symbol_specific_over_global(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    _seed(db_path, symbol="USDJPY", timeframe="1h", evaluated_at="2026-07-05T10:00:00Z", market_bias="Bear", bias_id="sym")
    _seed(db_path, symbol="GLOBAL", timeframe="1h", evaluated_at="2026-07-05T10:00:00Z", market_bias="Bull", bias_id="glob")

    # Request USD_JPY (underscore) -> normalized to USDJPY -> symbol-specific wins.
    result = store.get_effective_market_bias_as_of("USD_JPY", "1h", "2026-07-05T12:00:00Z", db_path=db_path)

    assert result["market_bias"] == "Bear"
    assert result["symbol"] == "USDJPY"  # returned row identifies the adopted scope
    assert result["timeframe"] == "1h"


def test_effective_bias_falls_back_to_global_when_no_symbol_specific(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    _seed(db_path, symbol="GLOBAL", timeframe="1h", evaluated_at="2026-07-05T10:00:00Z", market_bias="Bull", bias_id="glob")

    result = store.get_effective_market_bias_as_of("USD_JPY", "1h", "2026-07-05T12:00:00Z", db_path=db_path)

    assert result["market_bias"] == "Bull"
    assert result["symbol"] == "GLOBAL"


def test_effective_bias_prefers_symbol_timeframe_exact_match(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    # A 15m request: exact (USDJPY, 15m) must beat the (USDJPY, 1h) fallback.
    _seed(db_path, symbol="USDJPY", timeframe="15m", evaluated_at="2026-07-05T10:00:00Z", market_bias="Bear", bias_id="sym-15m")
    _seed(db_path, symbol="USDJPY", timeframe="1h", evaluated_at="2026-07-05T10:00:00Z", market_bias="Bull", bias_id="sym-1h")

    result = store.get_effective_market_bias_as_of("USD_JPY", "15m", "2026-07-05T12:00:00Z", db_path=db_path)

    assert result["market_bias"] == "Bear"
    assert result["timeframe"] == "15m"


def test_effective_bias_uses_symbol_1h_fallback(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    # No (USDJPY, 15m); (USDJPY, 1h) must win over any GLOBAL scope.
    _seed(db_path, symbol="USDJPY", timeframe="1h", evaluated_at="2026-07-05T10:00:00Z", market_bias="Bear", bias_id="sym-1h")
    _seed(db_path, symbol="GLOBAL", timeframe="15m", evaluated_at="2026-07-05T10:00:00Z", market_bias="Bull", bias_id="glob-15m")

    result = store.get_effective_market_bias_as_of("USD_JPY", "15m", "2026-07-05T12:00:00Z", db_path=db_path)

    assert result["market_bias"] == "Bear"
    assert result["symbol"] == "USDJPY"
    assert result["timeframe"] == "1h"


def test_effective_bias_uses_global_timeframe_fallback(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    # No symbol-specific at all; (GLOBAL, 15m) must beat (GLOBAL, 1h).
    _seed(db_path, symbol="GLOBAL", timeframe="15m", evaluated_at="2026-07-05T10:00:00Z", market_bias="Bear", bias_id="glob-15m")
    _seed(db_path, symbol="GLOBAL", timeframe="1h", evaluated_at="2026-07-05T10:00:00Z", market_bias="Bull", bias_id="glob-1h")

    result = store.get_effective_market_bias_as_of("USD_JPY", "15m", "2026-07-05T12:00:00Z", db_path=db_path)

    assert result["market_bias"] == "Bear"
    assert result["symbol"] == "GLOBAL"
    assert result["timeframe"] == "15m"


def test_effective_bias_uses_global_1h_last_resort(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    _seed(db_path, symbol="GLOBAL", timeframe="1h", evaluated_at="2026-07-05T10:00:00Z", market_bias="Bull", bias_id="glob-1h")

    result = store.get_effective_market_bias_as_of("USD_JPY", "15m", "2026-07-05T12:00:00Z", db_path=db_path)

    assert result["market_bias"] == "Bull"
    assert result["symbol"] == "GLOBAL"
    assert result["timeframe"] == "1h"


def test_effective_bias_does_not_use_a_future_symbol_bias(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    # Symbol-specific bias exists only in the future; GLOBAL exists now.
    _seed(db_path, symbol="GLOBAL", timeframe="1h", evaluated_at="2026-07-05T10:00:00Z", market_bias="Bull", bias_id="glob-now")
    _seed(db_path, symbol="USDJPY", timeframe="1h", evaluated_at="2026-07-05T14:00:00Z", market_bias="Bear", bias_id="sym-future")

    # As of 12:00 the symbol bias has not been evaluated yet -> GLOBAL is used.
    result = store.get_effective_market_bias_as_of("USD_JPY", "1h", "2026-07-05T12:00:00Z", db_path=db_path)

    assert result["market_bias"] == "Bull"
    assert result["symbol"] == "GLOBAL"


def test_effective_series_does_not_revert_to_global_after_symbol_begins(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    # GLOBAL bias exists early and keeps updating; a symbol-specific bias starts
    # at 12:00. A newer GLOBAL at 14:00 must NOT override the effective symbol bias.
    _seed(db_path, symbol="GLOBAL", timeframe="1h", evaluated_at="2026-07-05T09:00:00Z", market_bias="Bull", bias_id="glob-09")
    _seed(db_path, symbol="USDJPY", timeframe="1h", evaluated_at="2026-07-05T12:00:00Z", market_bias="Bear", bias_id="sym-12")
    _seed(db_path, symbol="GLOBAL", timeframe="1h", evaluated_at="2026-07-05T14:00:00Z", market_bias="Neutral", bias_id="glob-14")

    series = store.load_effective_market_bias_series("USD_JPY", "1h", db_path=db_path)

    # Before 12:00 -> GLOBAL Bull; from 12:00 onward -> symbol Bear, and it stays
    # Bear even though a later GLOBAL (Neutral@14:00) was saved.
    def effective_at(at):
        chosen = None
        for row in series:
            if row["evaluated_at"] <= at:
                chosen = row
            else:
                break
        return chosen

    assert effective_at("2026-07-05T10:00:00Z")["market_bias"] == "Bull"
    assert effective_at("2026-07-05T13:00:00Z")["market_bias"] == "Bear"
    at_15 = effective_at("2026-07-05T15:00:00Z")
    assert at_15["market_bias"] == "Bear"
    assert at_15["symbol"] == "USDJPY"
    # The Neutral GLOBAL@14:00 must never appear as an effective record.
    assert all(not (row["symbol"] == "GLOBAL" and row["evaluated_at"] == "2026-07-05T14:00:00Z") for row in series)


def test_effective_lookup_keeps_same_time_market_bias_id_desc_tiebreak(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    # Two symbol-specific records share evaluated_at; the greater market_bias_id wins,
    # matching get_market_bias_as_of's ORDER BY evaluated_at DESC, market_bias_id DESC.
    _seed(db_path, symbol="USDJPY", timeframe="1h", evaluated_at="2026-07-05T10:00:00Z", market_bias="Bull", bias_id="aaa")
    _seed(db_path, symbol="USDJPY", timeframe="1h", evaluated_at="2026-07-05T10:00:00Z", market_bias="Bear", bias_id="zzz")

    result = store.get_effective_market_bias_as_of("USD_JPY", "1h", "2026-07-05T12:00:00Z", db_path=db_path)
    assert result["market_bias_id"] == "zzz"

    series = store.load_effective_market_bias_series("USD_JPY", "1h", db_path=db_path)
    assert series[-1]["market_bias_id"] == "zzz"


def test_effective_lookup_is_side_effect_free_when_db_missing(tmp_path):
    db_path = tmp_path / "does_not_exist.sqlite3"

    assert store.get_effective_market_bias_as_of("USD_JPY", "1h", "2026-07-05T12:00:00Z", db_path=db_path) is None
    assert store.load_effective_market_bias_series("USD_JPY", "1h", db_path=db_path) == []
    assert not db_path.exists()  # read path must never create the DB


def test_effective_series_empty_when_table_absent_but_db_exists(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    # Create an empty DB file with no market_bias table.
    import sqlite3

    sqlite3.connect(db_path).close()

    assert store.load_effective_market_bias_series("USD_JPY", "1h", db_path=db_path) == []
    assert store.get_effective_market_bias_as_of("USD_JPY", "1h", "2026-07-05T12:00:00Z", db_path=db_path) is None


def test_effective_bias_for_global_request_matches_pre_issue40_behaviour(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    _seed(db_path, symbol="GLOBAL", timeframe="1h", evaluated_at="2026-07-05T10:00:00Z", market_bias="Bull", bias_id="glob")

    # A request already scoped to GLOBAL resolves exactly like the plain lookup.
    effective = store.get_effective_market_bias_as_of("GLOBAL", "1h", "2026-07-05T12:00:00Z", db_path=db_path)
    plain = store.get_market_bias_as_of("GLOBAL", "1h", "2026-07-05T12:00:00Z", db_path=db_path)
    assert effective == plain


def test_save_normalizes_symbol_and_timeframe_for_effective_lookup(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    _seed(
        db_path,
        symbol="usd_jpy",
        timeframe="1H",
        evaluated_at="2026-07-05T10:00:00Z",
        market_bias="Bull",
        bias_id="normalized",
    )

    result = store.get_effective_market_bias_as_of(
        "USD_JPY", "1h", "2026-07-05T12:00:00Z", db_path=db_path
    )
    assert result["market_bias_id"] == "normalized"
    assert result["symbol"] == "USDJPY"
    assert result["timeframe"] == "1h"


def test_as_of_compares_real_instants_not_iso_text_order(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    # 08:00-05:00 is 13:00Z and therefore future relative to 12:00Z. A plain
    # text comparison would incorrectly consider "08" <= "12" and leak it.
    _seed(
        db_path,
        symbol="USDJPY",
        timeframe="1h",
        evaluated_at="2026-07-05T08:00:00-05:00",
        market_bias="Bear",
        bias_id="future-offset",
    )
    _seed(
        db_path,
        symbol="GLOBAL",
        timeframe="1h",
        evaluated_at="2026-07-05T10:00:00Z",
        market_bias="Bull",
        bias_id="global-past",
    )

    result = store.get_effective_market_bias_as_of(
        "USDJPY", "1h", "2026-07-05T12:00:00Z", db_path=db_path
    )
    assert result["market_bias_id"] == "global-past"


def test_effective_series_orders_offset_timestamps_by_real_instant(tmp_path):
    db_path = tmp_path / "market_bias.sqlite3"
    _seed(db_path, symbol="GLOBAL", timeframe="1h", evaluated_at="2026-07-05T10:00:00Z", market_bias="Bull", bias_id="global")
    _seed(db_path, symbol="USDJPY", timeframe="1h", evaluated_at="2026-07-05T08:00:00-05:00", market_bias="Bear", bias_id="symbol")

    series = store.load_effective_market_bias_series("USDJPY", "1h", db_path=db_path)

    assert [row["market_bias_id"] for row in series] == ["global", "symbol"]
