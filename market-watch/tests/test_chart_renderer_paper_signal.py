"""Issue #65: Paper Signal annotations drawn by the ONE existing chart renderer.

The paper_signal event context resolves its target candle by exact UTC match
(never nearest-candle), maps side strictly to PAPER LONG / PAPER SHORT, draws a
vertical marker on both the matplotlib and Pillow backends, and never appears
on scheduled Chart Snapshot or Alert renders.
"""

import pathlib
import sys

import pandas as pd
import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import chart_renderer  # noqa: E402

TARGET_TS = "2026-07-05T12:00:00Z"


def make_candles(periods=30, tz="UTC", end=TARGET_TS):
    end_ts = pd.Timestamp(end)
    if tz is None:
        end_ts = end_ts.tz_localize(None)
    index = pd.date_range(end=end_ts, periods=periods, freq="1h", tz=tz)
    close = [150.0 + offset * 0.1 for offset in range(periods)]
    return pd.DataFrame(
        {
            "open": [value - 0.05 for value in close],
            "high": [value + 0.2 for value in close],
            "low": [value - 0.2 for value in close],
            "close": close,
            "volume": [100.0] * periods,
        },
        index=index,
    )


def paper_context(side="long", target=TARGET_TS):
    return chart_renderer.RenderContext(
        event_type="paper_signal",
        metadata={"side": side, "target_candle_utc": target},
        source="paper_signal",
    )


# --- side labels: strict, no guessing -----------------------------------------


def test_side_label_long_and_short():
    assert chart_renderer.paper_signal_side_label("long") == "PAPER LONG"
    assert chart_renderer.paper_signal_side_label("short") == "PAPER SHORT"
    assert chart_renderer.paper_signal_side_label(" LONG ") == "PAPER LONG"


@pytest.mark.parametrize("bad_side", ["buy", "sell", "l", "", None, 1, "longish"])
def test_side_label_rejects_unknown_sides(bad_side):
    with pytest.raises(ValueError, match="unknown paper signal side"):
        chart_renderer.paper_signal_side_label(bad_side)


# --- annotation resolution: exact target candle match -------------------------


def test_resolve_annotation_long():
    frame = make_candles()
    annotation = chart_renderer.resolve_paper_signal_annotation(frame, paper_context("long"))

    assert annotation.side == "long"
    assert annotation.label == "PAPER LONG"
    assert annotation.position == len(frame) - 1
    assert "2026-07-05T12:00:00Z" in annotation.target_display
    assert "JST" in annotation.target_display


def test_resolve_annotation_short():
    annotation = chart_renderer.resolve_paper_signal_annotation(make_candles(), paper_context("short"))

    assert annotation.side == "short"
    assert annotation.label == "PAPER SHORT"


def test_resolve_annotation_targets_middle_candle():
    frame = make_candles()
    middle = frame.index[10]
    annotation = chart_renderer.resolve_paper_signal_annotation(
        frame, paper_context("long", target=middle.strftime("%Y-%m-%dT%H:%M:%SZ"))
    )

    assert annotation.position == 10


@pytest.mark.parametrize(
    "target",
    [
        "2026-07-05T12:00:00Z",
        "2026-07-05T12:00:00+00:00",
        "2026-07-05T12:00:00",             # naive is treated as UTC
        "2026-07-05T21:00:00+09:00",       # same instant, JST offset
    ],
)
def test_resolve_annotation_normalizes_timezone_forms(target):
    annotation = chart_renderer.resolve_paper_signal_annotation(
        make_candles(), paper_context("long", target=target)
    )

    assert annotation.position == len(make_candles()) - 1


def test_resolve_annotation_matches_against_naive_frame_index():
    # A naive candle index is treated as UTC, same as the rest of the pipeline.
    frame = make_candles(tz=None)
    annotation = chart_renderer.resolve_paper_signal_annotation(frame, paper_context("long"))

    assert annotation.position == len(frame) - 1


def test_resolve_annotation_refuses_missing_target_candle():
    # 12:30 falls between stored hourly candles: no exact match, no nearest fallback.
    with pytest.raises(ValueError, match="not in the rendered candle window"):
        chart_renderer.resolve_paper_signal_annotation(
            make_candles(), paper_context("long", target="2026-07-05T12:30:00Z")
        )


def test_resolve_annotation_refuses_future_target_candle():
    with pytest.raises(ValueError, match="not in the rendered candle window"):
        chart_renderer.resolve_paper_signal_annotation(
            make_candles(), paper_context("long", target="2026-07-05T13:00:00Z")
        )


def test_resolve_annotation_requires_target_metadata():
    context = chart_renderer.RenderContext(
        event_type="paper_signal", metadata={"side": "long"}, source="paper_signal"
    )
    with pytest.raises(ValueError, match="target_candle_utc"):
        chart_renderer.resolve_paper_signal_annotation(make_candles(), context)


def test_resolve_annotation_rejects_unparseable_target():
    with pytest.raises(ValueError, match="unparseable target_candle_utc"):
        chart_renderer.resolve_paper_signal_annotation(
            make_candles(), paper_context("long", target="not-a-timestamp")
        )


def test_resolve_annotation_rejects_unknown_side():
    with pytest.raises(ValueError, match="unknown paper signal side"):
        chart_renderer.resolve_paper_signal_annotation(make_candles(), paper_context("buy"))


@pytest.mark.parametrize(
    "context",
    [
        chart_renderer.RenderContext(),
        chart_renderer.RenderContext(event_type=None, metadata={"side": "long", "target_candle_utc": TARGET_TS}),
        chart_renderer.RenderContext(
            event_type="ma_touch_lower",
            metadata={"side": "long", "target_candle_utc": TARGET_TS},
            source="alert",
        ),
        chart_renderer.RenderContext(event_type="bollinger_upper_break", metadata={}, source="alert"),
    ],
)
def test_non_paper_signal_contexts_never_resolve_an_annotation(context):
    # Scheduled Chart Snapshots and Alert renders never gain the annotation,
    # even when paper-looking metadata is present.
    assert chart_renderer.resolve_paper_signal_annotation(make_candles(), context) is None


# --- matplotlib backend -------------------------------------------------------


def _spy_axvline_and_text(monkeypatch, plt):
    axvline_calls = []
    original_axvline = plt.Axes.axvline

    def spy_axvline(self, x=0, *args, **kwargs):
        axvline_calls.append({"x": float(x), **kwargs})
        return original_axvline(self, x, *args, **kwargs)

    text_calls = []
    original_text = plt.Axes.text

    def spy_text(self, x, y, s, *args, **kwargs):
        text_calls.append(s)
        return original_text(self, x, y, s, *args, **kwargs)

    monkeypatch.setattr(plt.Axes, "axvline", spy_axvline)
    monkeypatch.setattr(plt.Axes, "text", spy_text)
    return axvline_calls, text_calls


def test_matplotlib_draws_vline_at_target_candle_and_label(tmp_path, monkeypatch):
    plt = pytest.importorskip("matplotlib.pyplot")
    import matplotlib.dates as mdates

    axvline_calls, text_calls = _spy_axvline_and_text(monkeypatch, plt)
    candles = make_candles()
    output_path = tmp_path / "paper_signal.png"

    chart_renderer.render_chart_snapshot(
        candles,
        output_path,
        title="USDJPY 1h Paper Signal",
        event_type="paper_signal",
        metadata={"side": "long", "target_candle_utc": TARGET_TS},
        source="paper_signal",
    )

    assert output_path.read_bytes().startswith(b"\x89PNG")
    # The vline lands exactly on the target candle's plotted (JST wall-clock) x.
    expected_x = mdates.date2num(
        pd.Timestamp(TARGET_TS).tz_convert("Asia/Tokyo").tz_localize(None).to_pydatetime()
    )
    assert len(axvline_calls) == 1
    assert axvline_calls[0]["x"] == pytest.approx(expected_x)
    labels = [text for text in text_calls if "PAPER LONG" in str(text)]
    assert len(labels) == 1
    assert "2026-07-05T12:00:00Z" in labels[0]


def test_matplotlib_short_side_uses_paper_short_label(tmp_path, monkeypatch):
    plt = pytest.importorskip("matplotlib.pyplot")
    _, text_calls = _spy_axvline_and_text(monkeypatch, plt)

    chart_renderer.render_chart_snapshot(
        make_candles(),
        tmp_path / "paper_signal_short.png",
        title="USDJPY 1h Paper Signal",
        event_type="paper_signal",
        metadata={"side": "short", "target_candle_utc": TARGET_TS},
        source="paper_signal",
    )

    assert any("PAPER SHORT" in str(text) for text in text_calls)
    assert not any("PAPER LONG" in str(text) for text in text_calls)


def test_scheduled_snapshot_render_has_no_paper_annotation(tmp_path, monkeypatch):
    plt = pytest.importorskip("matplotlib.pyplot")
    axvline_calls, text_calls = _spy_axvline_and_text(monkeypatch, plt)

    chart_renderer.render_chart_snapshot(
        make_candles(),
        tmp_path / "scheduled.png",
        title="USDJPY 1h Chart Snapshot",
    )

    assert axvline_calls == []
    assert not any("PAPER" in str(text) for text in text_calls)


def test_alert_render_has_no_paper_annotation(tmp_path, monkeypatch):
    plt = pytest.importorskip("matplotlib.pyplot")
    axvline_calls, text_calls = _spy_axvline_and_text(monkeypatch, plt)
    candles = make_candles()

    chart_renderer.render_chart_snapshot(
        candles,
        tmp_path / "alert.png",
        title="USDJPY 1h bollinger_upper_break",
        event_type="bollinger_upper_break",
        metadata={"upper": float(candles["close"].iloc[-1])},
        source="alert",
    )

    assert axvline_calls == []
    assert not any("PAPER" in str(text) for text in text_calls)


def test_unresolvable_target_fails_before_creating_output_dir(tmp_path):
    output_path = tmp_path / "derived" / "paper_signal.png"

    with pytest.raises(ValueError, match="not in the rendered candle window"):
        chart_renderer.render_chart_snapshot(
            make_candles(),
            output_path,
            title="USDJPY 1h Paper Signal",
            event_type="paper_signal",
            metadata={"side": "long", "target_candle_utc": "2026-07-06T00:00:00Z"},
            source="paper_signal",
        )

    assert not output_path.parent.exists()


# --- Pillow fallback ----------------------------------------------------------


@pytest.fixture
def force_pillow(monkeypatch):
    """Route render_chart_snapshot through the real Pillow fallback path."""

    def missing_matplotlib(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'matplotlib'")

    monkeypatch.setattr(chart_renderer, "_render_with_matplotlib", missing_matplotlib)


@pytest.mark.parametrize("side,label", [("long", "PAPER LONG"), ("short", "PAPER SHORT")])
def test_pillow_fallback_renders_annotated_png(tmp_path, monkeypatch, force_pillow, side, label):
    pytest.importorskip("PIL")
    seen = {}
    original = chart_renderer._render_with_pillow

    def spy_pillow(frame, output_path, title, context, annotation=None):
        seen["annotation"] = annotation
        return original(frame, output_path, title, context, annotation)

    monkeypatch.setattr(chart_renderer, "_render_with_pillow", spy_pillow)
    output_path = tmp_path / f"paper_signal_{side}.png"

    chart_renderer.render_chart_snapshot(
        make_candles(),
        output_path,
        title="USDJPY 1h Paper Signal",
        event_type="paper_signal",
        metadata={"side": side, "target_candle_utc": TARGET_TS},
        source="paper_signal",
    )

    assert output_path.read_bytes().startswith(b"\x89PNG")
    assert seen["annotation"].label == label
    assert seen["annotation"].position == len(make_candles()) - 1


def test_pillow_fallback_scheduled_render_gets_no_annotation(tmp_path, monkeypatch, force_pillow):
    pytest.importorskip("PIL")
    seen = {}
    original = chart_renderer._render_with_pillow

    def spy_pillow(frame, output_path, title, context, annotation=None):
        seen["annotation"] = annotation
        return original(frame, output_path, title, context, annotation)

    monkeypatch.setattr(chart_renderer, "_render_with_pillow", spy_pillow)

    chart_renderer.render_chart_snapshot(
        make_candles(),
        tmp_path / "scheduled.png",
        title="USDJPY 1h Chart Snapshot",
    )

    assert seen["annotation"] is None


def test_pillow_fallback_still_refuses_missing_target(tmp_path, force_pillow):
    with pytest.raises(ValueError, match="not in the rendered candle window"):
        chart_renderer.render_chart_snapshot(
            make_candles(),
            tmp_path / "paper_signal.png",
            title="USDJPY 1h Paper Signal",
            event_type="paper_signal",
            metadata={"side": "long", "target_candle_utc": "2026-07-06T00:00:00Z"},
            source="paper_signal",
        )
