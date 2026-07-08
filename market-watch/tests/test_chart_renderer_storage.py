import io
import pathlib
import sys

import pandas as pd

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import chart_renderer  # noqa: E402
import chart_storage  # noqa: E402


def make_candles():
    return pd.DataFrame(
        {
            "open": [1.0, 2.0, 1.5, 2.5],
            "high": [1.5, 2.3, 1.8, 2.8],
            "low": [0.8, 1.7, 1.2, 2.1],
            "close": [1.2, 1.9, 1.7, 2.6],
            "volume": [100.0, 120.0, 90.0, 130.0],
        },
        index=pd.date_range("2026-07-05", periods=4, freq="15min", tz="UTC"),
    )


def test_render_chart_snapshot_creates_local_png(tmp_path):
    output_path = tmp_path / "snapshot.png"

    rendered = chart_renderer.render_chart_snapshot(
        make_candles(),
        output_path,
        title="USDJPY 15m Chart Snapshot",
    )

    assert rendered == output_path
    assert output_path.read_bytes().startswith(b"\x89PNG")


def test_build_s3_key_uses_required_layout():
    key = chart_storage.build_s3_key(
        symbol="usdjpy",
        timeframe="15m",
        snapshot_id="snapshot-123",
        captured_at=pd.Timestamp("2026-07-08T01:02:03Z").to_pydatetime(),
        prefix="charts",
    )

    assert key == "charts/USDJPY/15m/2026/07/08/snapshot-123.png"


def test_upload_and_download_use_private_s3_key_without_url(tmp_path):
    local_path = tmp_path / "snapshot.png"
    local_path.write_bytes(b"png-bytes")
    calls = {}

    class Body:
        def read(self):
            return b"downloaded-png"

    class FakeS3Client:
        def upload_file(self, local_path_arg, bucket, key, ExtraArgs=None):
            calls["upload"] = (local_path_arg, bucket, key, ExtraArgs)

        def get_object(self, Bucket, Key):
            calls["download"] = (Bucket, Key)
            return {"Body": Body()}

    client = FakeS3Client()

    s3_key = chart_storage.upload_chart_image(
        local_path,
        bucket="belial-chart-snapshots",
        s3_key="charts/USDJPY/15m/2026/07/08/snapshot.png",
        s3_client=client,
    )
    data = chart_storage.download_chart_image(
        bucket="belial-chart-snapshots",
        s3_key=s3_key,
        s3_client=client,
    )

    assert s3_key == "charts/USDJPY/15m/2026/07/08/snapshot.png"
    assert calls["upload"][1:] == (
        "belial-chart-snapshots",
        s3_key,
        {"ContentType": "image/png"},
    )
    assert calls["download"] == ("belial-chart-snapshots", s3_key)
    assert data == b"downloaded-png"
