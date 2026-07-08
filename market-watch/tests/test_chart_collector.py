import pathlib
import sqlite3
import sys

import pandas as pd

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import chart_collector as collector  # noqa: E402
from chart_models import ChartTarget  # noqa: E402


def test_load_chart_targets_reads_env_values():
    targets = collector.load_chart_targets(
        {
            "CHART_SYMBOLS": "USDJPY,SPX",
            "CHART_TIMEFRAMES": "15m,1h",
            "CHART_TICKER_USDJPY": "JPY=X",
            "CHART_TICKER_SPX": "^GSPC",
        }
    )

    assert targets == [
        ChartTarget("USDJPY", "JPY=X", "15m", "USD/JPY", "USDJPY", "", "", "fx"),
        ChartTarget("USDJPY", "JPY=X", "1h", "USD/JPY", "USDJPY", "", "", "fx"),
        ChartTarget("SPX", "^GSPC", "15m", "S&P 500", "ES", "SPX", "^GSPC", "index_futures_proxy"),
        ChartTarget("SPX", "^GSPC", "1h", "S&P 500", "ES", "SPX", "^GSPC", "index_futures_proxy"),
    ]


def test_load_chart_targets_defaults_spx_to_es_proxy_with_spx_fallback():
    targets = collector.load_chart_targets({"CHART_SYMBOLS": "SPX", "CHART_TIMEFRAMES": "15m"})

    assert targets == [
        ChartTarget("SPX", "ES=F", "15m", "S&P 500", "ES", "SPX", "^GSPC", "index_futures_proxy")
    ]


def test_load_chart_targets_supports_mes_proxy_override():
    targets = collector.load_chart_targets(
        {
            "CHART_SYMBOLS": "SPX",
            "CHART_TIMEFRAMES": "1h",
            "CHART_SYMBOL_SPX": "MES",
            "CHART_TICKER_SPX": "MES=F",
        }
    )

    assert targets == [
        ChartTarget("SPX", "MES=F", "1h", "S&P 500", "MES", "SPX", "^GSPC", "index_futures_proxy")
    ]


def test_save_and_load_candles_roundtrip(tmp_path):
    target = ChartTarget("USDJPY", "JPY=X", "15m")
    frame = pd.DataFrame(
        {
            "open": [1.0, 2.0],
            "high": [1.5, 2.5],
            "low": [0.5, 1.5],
            "close": [1.2, 2.2],
            "volume": [100, 200],
        },
        index=pd.to_datetime(["2026-07-05T00:00:00Z", "2026-07-05T00:15:00Z"]),
    )
    db_path = tmp_path / "chart.sqlite3"

    count = collector.save_candles(db_path, collector.candles_from_frame(target, frame))
    loaded = collector.load_candles(db_path, "USDJPY", "15m")

    assert count == 2
    assert len(loaded) == 2
    assert loaded.iloc[-1]["close"] == 2.2

    with sqlite3.connect(db_path) as connection:
        row_count = connection.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
    assert row_count == 2


def test_candles_from_frame_stores_timezone_aware_timestamps_as_utc_z():
    target = ChartTarget("USDJPY", "JPY=X", "15m")
    frame = pd.DataFrame(
        {
            "open": [1.0],
            "high": [1.5],
            "low": [0.5],
            "close": [1.2],
            "volume": [100],
        },
        index=pd.to_datetime(["2026-07-04T05:45:00+09:00"]),
    )

    candles = collector.candles_from_frame(target, frame)

    assert candles[0].timestamp == "2026-07-03T20:45:00Z"


def test_load_candles_returns_utc_index(tmp_path):
    target = ChartTarget("USDJPY", "JPY=X", "15m")
    frame = pd.DataFrame(
        {
            "open": [1.0],
            "high": [1.5],
            "low": [0.5],
            "close": [1.2],
            "volume": [100],
        },
        index=pd.to_datetime(["2026-07-04T05:45:00+09:00"]),
    )
    db_path = tmp_path / "chart.sqlite3"

    collector.save_candles(db_path, collector.candles_from_frame(target, frame))
    loaded = collector.load_candles(db_path, "USDJPY", "15m")

    assert loaded.index[0].isoformat() == "2026-07-03T20:45:00+00:00"


def test_migration_note_documents_old_timestamp_policy():
    assert "deleting market-watch/data/chart_data.sqlite3" in collector.MIGRATION_NOTE
    assert "to_utc_iso_z" in collector.MIGRATION_NOTE


def test_collect_target_fetches_and_saves_with_mock(monkeypatch, tmp_path):
    target = ChartTarget("VIX", "^VIX", "1h")
    frame = pd.DataFrame(
        {
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
            "volume": [0],
        },
        index=pd.to_datetime(["2026-07-05T01:00:00Z"]),
    )

    monkeypatch.setattr(collector, "fetch_ohlcv", lambda target, *, period=collector.DEFAULT_PERIOD: frame)

    assert collector.collect_target(target, tmp_path / "chart.sqlite3") == 1


def test_fetch_ohlcv_falls_back_when_proxy_returns_empty(monkeypatch):
    calls = []
    fallback_history = pd.DataFrame(
        {
            "Open": [5000.0],
            "High": [5010.0],
            "Low": [4990.0],
            "Close": [5005.0],
            "Volume": [100],
        },
        index=pd.to_datetime(["2026-07-08T00:00:00Z"]),
    )

    class FakeTicker:
        def __init__(self, ticker):
            self.ticker = ticker

        def history(self, period, interval):
            calls.append((self.ticker, period, interval))
            if self.ticker == "ES=F":
                return pd.DataFrame()
            return fallback_history

    monkeypatch.setattr(collector.yf, "Ticker", FakeTicker)

    frame = collector.fetch_ohlcv(
        ChartTarget("SPX", "ES=F", "15m", "S&P 500", "ES", "SPX", "^GSPC", "index_futures_proxy"),
        period="5d",
    )

    assert calls == [("ES=F", "5d", "15m"), ("^GSPC", "5d", "15m")]
    assert frame.iloc[0]["close"] == 5005.0


def test_chart_snapshot_schema_persists_private_s3_metadata(tmp_path):
    db_path = tmp_path / "chart.sqlite3"

    collector.init_db(db_path)

    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(chart_snapshots)").fetchall()}
    assert {"snapshot_at", "s3_bucket", "s3_key"}.issubset(columns)


def test_alerts_reference_chart_snapshots_by_snapshot_id(tmp_path):
    db_path = tmp_path / "chart.sqlite3"

    snapshot_id = collector.insert_chart_snapshot(
        db_path,
        symbol="USDJPY",
        timeframe="15m",
        snapshot_at="2026-07-08T00:00:00Z",
        s3_bucket="belial-chart-snapshots",
        s3_key="charts/USDJPY/15m/2026/07/08/snapshot.png",
        captured_at="2026-07-08T00:00:00Z",
        candle_time="2026-07-08T00:00:00Z",
        ohlcv_json="[]",
        indicators_json="{}",
    )
    alert_id = collector.insert_alert(
        db_path,
        alert_type="ma_touch",
        symbol="USDJPY",
        timeframe="15m",
        occurred_at="2026-07-08T00:01:00Z",
        candle_time="2026-07-08T00:00:00Z",
        snapshot_id=snapshot_id,
        metadata_json="{}",
    )
    collector.link_alert_chart_snapshot(db_path, alert_id=alert_id, chart_snapshot_id=snapshot_id)

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT alerts.id, alerts.snapshot_id, chart_snapshots.s3_key
            FROM alerts
            JOIN chart_snapshots ON alerts.snapshot_id = chart_snapshots.id
            WHERE alerts.id = ?
            """,
            (alert_id,),
        ).fetchone()
        link_row = connection.execute(
            """
            SELECT alert_id, chart_snapshot_id
            FROM alert_chart_snapshots
            WHERE alert_id = ? AND chart_snapshot_id = ?
            """,
            (alert_id, snapshot_id),
        ).fetchone()

    assert row == (alert_id, snapshot_id, "charts/USDJPY/15m/2026/07/08/snapshot.png")
    assert link_row == (alert_id, snapshot_id)
