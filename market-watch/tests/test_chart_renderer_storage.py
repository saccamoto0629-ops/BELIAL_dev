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


def test_min_body_height_ignores_a_single_outlier_candle():
    # A window of many quiet, small-bodied candles plus one large news-spike
    # candle. Alert-triggered renders (e.g. an MA touch firing right after
    # price reverts to the mean following a spike) hit this shape often. The
    # floor must be sized off the *typical* candle body, not the spike's
    # window-wide high/low span, or every quiet candle gets force-floored to
    # the exact same height and the whole run reads as one flat bar instead
    # of distinct candles.
    # Varying-but-tiny quiet bodies (~0.0005-0.005), like a real ranging
    # market, plus one dominant news-spike candle that sets the window's
    # high/low span.
    quiet_body_sizes = [0.0005 + 0.0045 * ((index * 37) % 11) / 10 for index in range(40)]
    quiet_open = [1.0000 + 0.0001 * index for index in range(40)]
    quiet_close = [o + size for o, size in zip(quiet_open, quiet_body_sizes)]
    opens = quiet_open[:10] + [1.0000] + quiet_open[10:]
    closes = quiet_close[:10] + [2.0000] + quiet_close[10:]
    candles = pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) + 0.0001 for o, c in zip(opens, closes)],
            "low": [min(o, c) - 0.0001 for o, c in zip(opens, closes)],
            "close": closes,
        },
        index=pd.date_range("2026-07-05", periods=len(opens), freq="15min", tz="UTC"),
    )
    price_range = float(candles["high"].max() - candles["low"].min())

    min_body_height = chart_renderer._min_body_height(candles, price_range)

    quiet_bodies = (candles["open"] - candles["close"]).abs().drop(index=candles.index[10])
    # The quiet candles' real bodies must mostly clear the floor so they keep
    # their own (varying) height instead of collapsing to one shared value.
    assert (quiet_bodies >= min_body_height).mean() > 0.5
    # A naive window-wide floor (0.4% of the spike-dominated range) would
    # have swallowed nearly all of them; confirm the new floor does much
    # better.
    naive_floor = price_range * 0.004
    naive_forced_fraction = (quiet_bodies < naive_floor).mean()
    new_forced_fraction = (quiet_bodies < min_body_height).mean()
    assert naive_forced_fraction > 0.65
    assert new_forced_fraction < naive_forced_fraction / 2


def test_bollinger_event_context_selects_bands_and_highlight():
    context = chart_renderer.RenderContext(event_type="bollinger_lower_break", metadata={})

    assert chart_renderer._show_bollinger(context) is True
    assert chart_renderer._highlight_bollinger_band(context) == "lower"
    assert chart_renderer._calculate_bollinger_bands(make_candles()["close"])["upper"].notna().any()


def test_render_chart_snapshot_draws_candlestick_wicks_and_bodies_from_ohlc(tmp_path, monkeypatch):
    plt = pytest.importorskip("matplotlib.pyplot")

    candles = pd.DataFrame(
        {
            "open": [1.00, 1.02, 1.01],
            "high": [1.05, 1.06, 1.04],
            "low": [0.98, 1.00, 0.99],
            "close": [1.02, 1.01, 1.03],
        },
        index=pd.date_range("2026-07-05", periods=3, freq="15min", tz="UTC"),
    )

    wick_spans = []
    original_vlines = plt.Axes.vlines

    def spy_vlines(self, x, ymin, ymax, *args, **kwargs):
        wick_spans.append((float(ymin), float(ymax)))
        return original_vlines(self, x, ymin, ymax, *args, **kwargs)

    body_heights = []
    original_rectangle = plt.Rectangle

    def spy_rectangle(xy, width, height, **kwargs):
        body_heights.append(height)
        return original_rectangle(xy, width, height, **kwargs)

    monkeypatch.setattr(plt.Axes, "vlines", spy_vlines)
    monkeypatch.setattr(plt, "Rectangle", spy_rectangle)

    chart_renderer.render_chart_snapshot(
        candles,
        tmp_path / "snapshot.png",
        title="candlestick wiring check",
    )

    # One wick + one body per candle, drawn from that candle's own high/low/open/close.
    assert wick_spans == list(zip(candles["low"], candles["high"]))
    assert len(body_heights) == len(candles)
    # Bodies must vary with open/close, not collapse to a single repeated close marker.
    assert len(set(round(height, 6) for height in body_heights)) > 1


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
