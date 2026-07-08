import pathlib
import sys

import pandas as pd
import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import chart_image_uploader  # noqa: E402
import chart_renderer  # noqa: E402


def make_candles():
    close = [1.0 + (index * 0.01) + (0.04 if index == 24 else 0.0) for index in range(25)]
    return pd.DataFrame(
        {
            "open": [value - 0.003 for value in close],
            "high": [value + 0.02 for value in close],
            "low": [value - 0.02 for value in close],
            "close": close,
            "volume": [100.0 + index for index in range(25)],
        },
        index=pd.date_range("2026-07-05", periods=25, freq="15min", tz="UTC"),
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


def test_render_chart_snapshot_accepts_bollinger_event_context(tmp_path):
    output_path = tmp_path / "snapshot.png"
    candles = make_candles()

    rendered = chart_renderer.render_chart_snapshot(
        candles,
        output_path,
        title="USDJPY 15m bollinger_upper_break",
        event_type="bollinger_upper_break",
        metadata={
            "upper": float(candles["close"].iloc[-1] + 0.01),
            "middle": float(candles["close"].iloc[-1]),
            "lower": float(candles["close"].iloc[-1] - 0.01),
        },
    )

    assert rendered == output_path
    assert output_path.read_bytes().startswith(b"\x89PNG")


def test_visible_body_bounds_keeps_tiny_open_close_visible():
    body_low, body_high = chart_renderer._visible_body_bounds(100.0, 100.000001, 0.02)

    assert body_high - body_low == pytest.approx(0.02)
    assert body_low < 100.0
    assert body_high > 100.000001


def test_bollinger_event_context_selects_bands_and_highlight():
    context = chart_renderer.RenderContext(event_type="bollinger_lower_break", metadata={})

    assert chart_renderer._show_bollinger(context) is True
    assert chart_renderer._highlight_bollinger_band(context) == "lower"
    assert chart_renderer._calculate_bollinger_bands(make_candles()["close"])["upper"].notna().any()


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
