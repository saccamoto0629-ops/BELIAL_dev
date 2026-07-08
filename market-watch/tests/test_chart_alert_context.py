import pathlib
import sqlite3
import sys

import pandas as pd

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import chart_alert_context as context  # noqa: E402


def test_prepare_alert_image_uploads_presigns_and_links_db(monkeypatch, tmp_path):
    db_path = tmp_path / "chart.sqlite3"
    uploaded = {}

    class FakeS3Client:
        def upload_file(self, local_path, bucket, key, ExtraArgs=None):
            uploaded["bucket"] = bucket
            uploaded["key"] = key
            uploaded["content"] = pathlib.Path(local_path).read_bytes()
            uploaded["extra_args"] = ExtraArgs

        def generate_presigned_url(self, operation, Params, ExpiresIn):
            uploaded["presign"] = (operation, Params, ExpiresIn)
            return f"https://presigned.invalid/{Params['Key']}?signature=short-lived"

    monkeypatch.setattr(context, "load_env_file", lambda: {
        "CHART_DB_PATH": str(db_path),
        "AWS_DEFAULT_REGION": "ap-northeast-1",
        "S3_BUCKET_NAME": "belial-chart-snapshots",
        "S3_CHART_PREFIX": "charts",
    })
    monkeypatch.setattr(context, "create_s3_client", lambda config: FakeS3Client())

    snapshot_id, presigned_url = context.prepare_alert_image(
        alert_type="ma_touch",
        symbol="USDJPY",
        timeframe="1h",
        candle_time="2026-07-08T00:00:00Z",
        candles=pd.DataFrame(
            {"close": [100.0, 101.0, 102.0]},
            index=pd.date_range("2026-07-08", periods=3, freq="1h", tz="UTC"),
        ),
        metadata={"detected_at": "2026-07-08T00:01:00Z"},
    )

    assert snapshot_id
    assert uploaded["bucket"] == "belial-chart-snapshots"
    assert uploaded["key"].startswith("charts/USDJPY/1h/")
    assert uploaded["key"].endswith(".png")
    assert uploaded["content"].startswith(b"\x89PNG")
    assert uploaded["extra_args"] == {"ContentType": "image/png"}
    assert presigned_url.endswith("?signature=short-lived")

    with sqlite3.connect(db_path) as connection:
        snapshot_row = connection.execute(
            "SELECT s3_bucket, s3_key FROM chart_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        link_row = connection.execute(
            """
            SELECT alert_chart_snapshots.chart_snapshot_id
            FROM alert_chart_snapshots
            JOIN alerts ON alert_chart_snapshots.alert_id = alerts.id
            WHERE alerts.snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        serialized = "\n".join(
            str(value)
            for value in connection.execute("SELECT * FROM chart_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
            if value is not None
        )

    assert snapshot_row == ("belial-chart-snapshots", uploaded["key"])
    assert link_row == (snapshot_id,)
    assert "presigned.invalid" not in serialized
