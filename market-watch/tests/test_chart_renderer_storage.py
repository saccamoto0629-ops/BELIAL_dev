import pathlib
import sys

import pandas as pd
import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import chart_image_uploader  # noqa: E402
import chart_renderer  # noqa: E402


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


def test_render_chart_snapshot_requires_real_ohlc(tmp_path):
    close_only = pd.DataFrame(
        {"close": [1.0, 2.0, 3.0]},
        index=pd.date_range("2026-07-05", periods=3, freq="15min", tz="UTC"),
    )

    with pytest.raises(ValueError, match="OHLC"):
        chart_renderer.render_chart_snapshot(
            close_only,
            tmp_path / "snapshot.png",
            title="USDJPY 15m Chart Snapshot",
        )


def test_build_chart_s3_key_uses_required_layout():
    key = chart_image_uploader.build_chart_s3_key(
        "usdjpy",
        "15m",
        pd.Timestamp("2026-07-08T01:02:03Z").to_pydatetime(),
        prefix="charts",
        object_id="snapshot-123",
    )

    assert key == "charts/USDJPY/15m/2026/07/08/snapshot-123.png"


def test_upload_and_presign_use_private_s3_key_without_direct_url(tmp_path):
    local_path = tmp_path / "snapshot.png"
    local_path.write_bytes(b"png-bytes")
    calls = {}

    class FakeS3Client:
        def upload_file(self, local_path_arg, bucket, key, ExtraArgs=None):
            calls["upload"] = (local_path_arg, bucket, key, ExtraArgs)

        def generate_presigned_url(self, operation, Params, ExpiresIn):
            calls["presign"] = (operation, Params, ExpiresIn)
            return f"https://presigned.invalid/{Params['Key']}?expires={ExpiresIn}"

    client = FakeS3Client()
    config = {
        "region": "ap-northeast-1",
        "bucket": "belial-chart-snapshots",
        "prefix": "charts",
    }

    s3_key = chart_image_uploader.upload_chart_image(
        local_path,
        "USDJPY",
        "15m",
        pd.Timestamp("2026-07-08T01:02:03Z").to_pydatetime(),
        config=config,
        s3_client=client,
        s3_key="charts/USDJPY/15m/2026/07/08/snapshot.png",
    )
    presigned_url = chart_image_uploader.generate_presigned_chart_url(
        s3_key,
        expires_in=900,
        config=config,
        s3_client=client,
    )

    assert s3_key == "charts/USDJPY/15m/2026/07/08/snapshot.png"
    assert calls["upload"][1:] == (
        "belial-chart-snapshots",
        s3_key,
        {"ContentType": "image/png"},
    )
    assert calls["presign"] == (
        "get_object",
        {"Bucket": "belial-chart-snapshots", "Key": s3_key},
        900,
    )
    assert presigned_url == f"https://presigned.invalid/{s3_key}?expires=900"
