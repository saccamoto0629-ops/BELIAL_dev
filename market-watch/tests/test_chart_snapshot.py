import pathlib
import sys

import pandas as pd

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import chart_snapshot as snapshot  # noqa: E402
from chart_models import ChartTarget  # noqa: E402


def make_candles():
    return pd.DataFrame(
        {
            "open": [float(i) for i in range(1, 81)],
            "high": [float(i) + 0.5 for i in range(1, 81)],
            "low": [float(i) - 0.5 for i in range(1, 81)],
            "close": [float(i) for i in range(1, 81)],
            "volume": [100.0 for _ in range(80)],
        },
        index=pd.date_range("2026-07-05", periods=80, freq="15min", tz="UTC"),
    )


def make_short_candles():
    return pd.DataFrame(
        {
            "open": [float(i) for i in range(1, 10)],
            "high": [float(i) + 0.5 for i in range(1, 10)],
            "low": [float(i) - 0.5 for i in range(1, 10)],
            "close": [float(i) for i in range(1, 10)],
            "volume": [100.0 for _ in range(9)],
        },
        index=pd.date_range("2026-07-05", periods=9, freq="15min", tz="UTC"),
    )


def test_build_indicator_snapshot_and_format_message():
    target = ChartTarget("USDJPY", "JPY=X", "15m")

    indicator = snapshot.build_indicator_snapshot(
        target,
        make_candles(),
        now_utc=pd.Timestamp("2026-07-06T01:45:00Z"),
    )
    message = snapshot.build_chart_snapshot_message([indicator])

    assert indicator is not None
    assert indicator.symbol == "USDJPY"
    assert indicator.trend == "Bull"
    assert indicator.rsi14 == 100.0
    assert indicator.rsi_state == "Overbought"
    assert "📈 Chart Snapshot" in message
    assert "USDJPY 15m" in message
    assert "Time: 2026-07-06 04:45 JST" in message
    assert "Freshness: Fresh (6h 0m old)" in message
    assert "Bias Weight: 1.00" in message
    assert "SMA20:" in message
    assert "BB Width:" in message
    assert "MACD State:" in message
    assert "RSI14: 100.00" in message
    assert "RSI State: Overbought" in message
    assert "Trend: Bull" in message


def test_build_indicator_snapshot_reports_unavailable_rsi_when_data_is_short():
    target = ChartTarget("USDJPY", "JPY=X", "15m")

    indicator = snapshot.build_indicator_snapshot(
        target,
        make_short_candles(),
        now_utc=pd.Timestamp("2026-07-05T02:00:00Z"),
    )

    assert indicator is not None
    assert indicator.rsi14 is None
    assert indicator.rsi_state == "Unavailable"

    formatted = snapshot.format_snapshot(indicator)
    assert "RSI14: N/A" in formatted
    assert "RSI State: Unavailable" in formatted


def test_post_discord_message_dry_run_does_not_call_session(capsys):
    class FailingSession:
        def post(self, *args, **kwargs):
            raise AssertionError("session.post should not be called")

    snapshot.post_discord_message("", "hello", dry_run=True, session=FailingSession())

    assert "Would send Chart Snapshot" in capsys.readouterr().out


def test_load_webhook_url_uses_chart_snapshot_webhook():
    assert snapshot.load_webhook_url(
        {
            "CHART_SNAPSHOT_WEBHOOK_URL": "https://discord.invalid/chart-snapshot",
            "DISCORD_WEBHOOK_URL": "https://discord.invalid/legacy",
        }
    ) == "https://discord.invalid/chart-snapshot"


def test_format_timestamp_jst_converts_utc_z_to_jst():
    assert snapshot.format_timestamp_jst("2026-07-03T20:45:00Z") == "2026-07-04 05:45 JST"


def test_freshness_from_timestamp_marks_older_than_24h_stale():
    freshness, age_seconds = snapshot.freshness_from_timestamp(
        "2026-07-06T20:45:00Z",
        now_utc=pd.Timestamp("2026-07-07T20:46:00Z"),
    )

    assert freshness == "Stale"
    assert age_seconds == 86460


def test_weekend_closed_is_not_stale_even_when_data_is_old():
    target = ChartTarget("USDJPY", "JPY=X", "15m")

    indicator = snapshot.build_indicator_snapshot(
        target,
        make_candles(),
        now_utc=pd.Timestamp("2026-07-11T03:00:00Z"),
    )

    assert indicator is not None
    assert indicator.freshness == "Weekend Closed"
    assert indicator.trend == "Bull"
    assert indicator.state != "Stale"
    assert indicator.bias_weight == 0.25


def test_monday_morning_market_closed_is_not_stale():
    freshness, age_seconds = snapshot.freshness_from_timestamp(
        "2026-07-03T20:45:00Z",
        now_utc=pd.Timestamp("2026-07-05T21:30:00Z"),
    )

    assert freshness == "Market Closed"
    assert age_seconds > 24 * 60 * 60


def test_stale_data_is_not_used_for_trend_or_state():
    target = ChartTarget("USDJPY", "JPY=X", "15m")

    indicator = snapshot.build_indicator_snapshot(
        target,
        make_candles(),
        now_utc=pd.Timestamp("2026-07-07T20:45:00Z"),
    )

    assert indicator is not None
    assert indicator.freshness == "Stale"
    assert indicator.trend == "Stale"
    assert indicator.state == "Stale"
    assert indicator.bias_weight == 0.0


def test_main_once_dry_run_uses_existing_sqlite_without_fetch(monkeypatch, tmp_path):
    target = ChartTarget("USDJPY", "JPY=X", "15m")
    calls = []

    monkeypatch.setattr(snapshot, "load_env_file", lambda: {
        "DRY_RUN": "true",
        "CHART_DB_PATH": str(tmp_path / "chart.sqlite3"),
        "CHART_SNAPSHOT_WEBHOOK_URL": "https://discord.invalid/chart-snapshot",
    })
    monkeypatch.setattr(snapshot, "load_chart_targets", lambda env_values: [target])
    monkeypatch.setattr(snapshot, "chart_db_path", lambda env_values: tmp_path / "chart.sqlite3")
    monkeypatch.setattr(snapshot, "load_candles", lambda db_path, symbol, timeframe, limit=200: make_candles())
    monkeypatch.setattr(
        snapshot,
        "post_discord_message",
        lambda webhook_url, message, dry_run=False, attachments=None: calls.append((webhook_url, dry_run, message, attachments)),
    )

    snapshot.main(["--once", "--dry-run", "--skip-fetch"])

    assert calls
    assert calls[0][0] == "https://discord.invalid/chart-snapshot"
    assert calls[0][1] is True
    assert "Chart Snapshot" in calls[0][2]
    assert calls[0][3] == []


def test_main_dry_run_skip_fetch_does_not_create_missing_sqlite(monkeypatch, tmp_path):
    db_path = tmp_path / "missing.sqlite3"
    calls = []

    monkeypatch.setattr(snapshot, "load_env_file", lambda: {
        "DRY_RUN": "true",
        "CHART_DB_PATH": str(db_path),
        "CHART_SNAPSHOT_WEBHOOK_URL": "https://discord.invalid/chart-snapshot",
    })
    monkeypatch.setattr(snapshot, "load_chart_targets", lambda env_values: [ChartTarget("USDJPY", "JPY=X", "15m")])
    monkeypatch.setattr(snapshot, "chart_db_path", lambda env_values: db_path)
    monkeypatch.setattr(
        snapshot,
        "post_discord_message",
        lambda webhook_url, message, dry_run=False, attachments=None: calls.append((dry_run, message, attachments)),
    )

    snapshot.main(["--once", "--dry-run", "--skip-fetch"])

    assert not db_path.exists()
    assert calls == [(True, "📈 Chart Snapshot\n\n- unavailable", [])]


def test_persist_snapshot_images_uploads_and_saves_s3_key(monkeypatch, tmp_path):
    target = ChartTarget("USDJPY", "JPY=X", "15m")
    db_path = tmp_path / "chart.sqlite3"
    candles = make_candles()

    import chart_collector as collector

    collector.save_candles(db_path, collector.candles_from_frame(target, candles))
    indicator = snapshot.build_indicator_snapshot(
        target,
        candles,
        now_utc=pd.Timestamp("2026-07-06T01:45:00Z"),
    )
    uploaded = {}

    class FakeS3Client:
        def upload_file(self, local_path, bucket, key, ExtraArgs=None):
            uploaded["bucket"] = bucket
            uploaded["key"] = key
            uploaded["content"] = pathlib.Path(local_path).read_bytes()
            uploaded["extra_args"] = ExtraArgs

    monkeypatch.setattr(snapshot, "create_s3_client", lambda config: FakeS3Client())

    attachments = snapshot.persist_snapshot_images(
        [indicator],
        db_path,
        {
            "AWS_REGION": "ap-northeast-1",
            "CHART_S3_BUCKET": "belial-chart-snapshots",
            "CHART_S3_PREFIX": "charts",
        },
    )

    assert uploaded["bucket"] == "belial-chart-snapshots"
    assert uploaded["key"].startswith("charts/USDJPY/15m/")
    assert uploaded["key"].endswith(".png")
    assert uploaded["content"].startswith(b"\x89PNG")
    assert uploaded["extra_args"] == {"ContentType": "image/png"}
    assert attachments[0][1].startswith(b"\x89PNG")

    with collector.sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT image_s3_key FROM chart_snapshots").fetchone()
    assert row[0] == uploaded["key"]
