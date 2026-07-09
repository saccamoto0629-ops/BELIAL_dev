"""Render local PNG chart snapshots for BELIAL Market Watch.

Rendering is observation-only. It creates visual evidence for snapshots and
alerts, never trading instructions.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import pandas as pd

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOGS_DIR / "chart_renderer.log"


@dataclass(frozen=True)
class RenderContext:
    event_type: str | None = None
    metadata: dict[str, Any] | None = None
    source: str = "scheduled"


def _logger() -> logging.Logger:
    logger = logging.getLogger("chart_renderer")
    if logger.handlers:
        return logger
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.propagate = True
    return logger


def _event_name(context: RenderContext) -> str:
    return (context.event_type or "").lower()


def _show_bollinger(context: RenderContext) -> bool:
    return True


def _highlight_bollinger_band(context: RenderContext) -> str | None:
    event_name = _event_name(context)
    if "upper" in event_name:
        return "upper"
    if "lower" in event_name:
        return "lower"
    if "middle" in event_name:
        return "middle"
    return None


def _min_body_height(frame: pd.DataFrame, price_range: float, *, source: str = "scheduled") -> float:
    """Return the minimum candle-body height that keeps bodies visible.

    Scaled off the *typical* per-candle body size (median), not the full
    window's high/low span. A single outlier candle (e.g. a news spike)
    would otherwise dominate the window-wide range and force every other,
    genuinely small-bodied candle to the exact same forced height, making a
    run of quiet candles collapse into one uniform flat bar instead of
    showing their real (if small) variation.
    """
    body_sizes = (frame["open"] - frame["close"]).abs()
    positive_bodies = body_sizes[body_sizes > 0]
    typical_body = float(positive_bodies.median()) if not positive_bodies.empty else 0.0
    if source.startswith("alert"):
        return max(typical_body * 2.0, price_range * 0.006, 0.0002)
    return max(typical_body * 0.25, price_range * 0.0006, 0.00002)


def _to_jst_naive_index(index: pd.Index) -> pd.DatetimeIndex:
    """Convert a UTC-based candle index to JST wall-clock time for display.

    Storage and provider timestamps stay UTC; only this rendering step
    shifts to Asia/Tokyo so the x-axis matches how the chart is read
    (e.g. on Discord). The tz is dropped after conversion so mdates treats
    the values as plain JST wall-clock instants instead of re-localizing.
    """
    utc_index = pd.DatetimeIndex(pd.to_datetime(index))
    if utc_index.tz is None:
        utc_index = utc_index.tz_localize("UTC")
    else:
        utc_index = utc_index.tz_convert("UTC")
    return utc_index.tz_convert("Asia/Tokyo").tz_localize(None)


def _visible_body_bounds(open_price: float, close_price: float, min_body_height: float) -> tuple[float, float]:
    body_low = min(open_price, close_price)
    body_high = max(open_price, close_price)
    if body_high - body_low >= min_body_height:
        return body_low, body_high

    midpoint = (open_price + close_price) / 2
    half_height = min_body_height / 2
    return midpoint - half_height, midpoint + half_height


def _prepare_frame(candles: pd.DataFrame) -> pd.DataFrame:
    frame = candles.copy()
    if frame.empty:
        raise ValueError("candles must not be empty")
    required = {"open", "high", "low", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"candles must contain OHLC columns; missing: {', '.join(sorted(missing))}")
    if "volume" not in frame.columns:
        frame["volume"] = 0.0
    frame = frame[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    if frame.empty:
        raise ValueError("candles must contain numeric OHLC values")
    return frame.tail(120)


def _log_render_input(frame: pd.DataFrame, title: str, context: RenderContext) -> None:
    body_sizes = (frame["open"] - frame["close"]).abs()
    all_open_equals_close = bool((body_sizes == 0).all())
    logger = _logger()
    logger.info(
        "Chart render input: source=%s event_type=%s title=%r rows=%s columns=%s all_open_equals_close=%s body_min=%.8f body_median=%.8f body_max=%.8f",
        context.source,
        context.event_type,
        title,
        len(frame),
        list(frame.columns),
        all_open_equals_close,
        float(body_sizes.min()),
        float(body_sizes.median()),
        float(body_sizes.max()),
    )
    if all_open_equals_close:
        logger.warning(
            "Chart render input has open == close for every candle: source=%s event_type=%s title=%r",
            context.source,
            context.event_type,
            title,
        )
    logger.info(
        "Chart render OHLC tail(10): source=%s event_type=%s\n%s",
        context.source,
        context.event_type,
        frame[["open", "high", "low", "close"]].tail(10).to_string(),
    )


def _calculate_bollinger_bands(close: pd.Series) -> pd.DataFrame:
    middle = close.rolling(window=20, min_periods=20).mean()
    std = close.rolling(window=20, min_periods=20).std()
    return pd.DataFrame(
        {
            "middle": middle,
            "upper": middle + (std * 2),
            "lower": middle - (std * 2),
        },
        index=close.index,
    )


def _bollinger_band_styles(context: RenderContext) -> dict[str, dict[str, Any]]:
    highlight = _highlight_bollinger_band(context)
    return {
        "upper": {
            "color": "#d2a8ff",
            "label": "BB Upper",
            "linewidth": 2.2 if highlight == "upper" else 1.25,
            "linestyle": "-" if highlight == "upper" else "--",
            "highlight": highlight == "upper",
        },
        "middle": {
            "color": "#8b949e",
            "label": "BB Middle",
            "linewidth": 1.55 if highlight == "middle" else 1.0,
            "linestyle": "-" if highlight == "middle" else ":",
            "highlight": highlight == "middle",
        },
        "lower": {
            "color": "#7ee787",
            "label": "BB Lower",
            "linewidth": 2.2 if highlight == "lower" else 1.25,
            "linestyle": "-" if highlight == "lower" else "--",
            "highlight": highlight == "lower",
        },
    }


def _render_with_matplotlib(frame: pd.DataFrame, output_path: Path, title: str, context: RenderContext) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    _logger().info(
        "Chart render backend=matplotlib candlestick=manual_vlines_rectangles no_hlines_scatter_for_price source=%s event_type=%s title=%r",
        context.source,
        context.event_type,
        title,
    )

    x_values = mdates.date2num(_to_jst_naive_index(frame.index).to_pydatetime())
    if len(x_values) > 1:
        candle_steps = pd.Series(x_values).diff().dropna()
        positive_steps = candle_steps[candle_steps > 0]
        candle_step = float(positive_steps.median()) if not positive_steps.empty else 0.02
    else:
        candle_step = 0.02
    candle_width = min(max(0.68 * candle_step, 0.003), candle_step * 0.9)
    price_range = max(float(frame["high"].max() - frame["low"].min()), 0.00001)
    min_body_height = _min_body_height(frame, price_range, source=context.source)

    fig, (price_ax, macd_ax) = plt.subplots(
        2,
        1,
        figsize=(12, 7),
        sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )
    fig.patch.set_facecolor("#101418")
    for axis in (price_ax, macd_ax):
        axis.set_facecolor("#101418")
        axis.grid(True, color="#2d333b", linewidth=0.5)
        axis.tick_params(colors="#d0d7de")
        for spine in axis.spines.values():
            spine.set_color("#3d444d")

    for x_value, row in zip(x_values, frame.itertuples(), strict=False):
        color = "#2ecc71" if row.close >= row.open else "#e74c3c"
        price_ax.vlines(x_value, row.low, row.high, color=color, linewidth=1.4)
        body_low, body_high = _visible_body_bounds(float(row.open), float(row.close), min_body_height)
        price_ax.add_patch(
            plt.Rectangle(
                (x_value - candle_width / 2, body_low),
                candle_width,
                body_high - body_low,
                facecolor=color,
                edgecolor=color,
                linewidth=1.1,
            )
        )

    close = frame["close"]
    sma20 = close.rolling(20).mean()
    sma75 = close.rolling(75).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    price_ax.plot(x_values, sma20, color="#f1c40f", linewidth=1.2, label="SMA20")
    price_ax.plot(x_values, sma75, color="#3498db", linewidth=1.2, label="SMA75")
    price_ax.plot(x_values, ema20, color="#e67e22", linewidth=1.0, label="EMA20")
    if _show_bollinger(context):
        bands = _calculate_bollinger_bands(close)
        band_styles = _bollinger_band_styles(context)
        for band_name, style in band_styles.items():
            price_ax.plot(
                x_values,
                bands[band_name],
                color=style["color"],
                linewidth=style["linewidth"],
                linestyle=style["linestyle"],
                label=style["label"],
            )
        metadata = context.metadata or {}
        highlight = _highlight_bollinger_band(context)
        if highlight and metadata.get(highlight) is not None:
            price_ax.axhline(
                float(metadata[highlight]),
                color=band_styles[highlight]["color"],
                linewidth=1.0,
                linestyle=":",
            )
    price_ax.set_title(title, color="#f0f6fc", fontsize=14)
    price_ax.legend(facecolor="#161b22", edgecolor="#3d444d", labelcolor="#d0d7de")

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    macd_ax.bar(x_values, hist, color=["#2ecc71" if value >= 0 else "#e74c3c" for value in hist], width=candle_width)
    macd_ax.plot(x_values, macd, color="#58a6ff", linewidth=1.0, label="MACD")
    macd_ax.plot(x_values, signal, color="#f78166", linewidth=1.0, label="Signal")
    macd_ax.legend(facecolor="#161b22", edgecolor="#3d444d", labelcolor="#d0d7de")
    macd_ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    macd_ax.set_xlabel("Time (JST)", color="#d0d7de")

    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path, format="png", dpi=140)
    plt.close(fig)


def _render_with_pillow(frame: pd.DataFrame, output_path: Path, title: str, context: RenderContext) -> None:
    from PIL import Image, ImageDraw

    _logger().info(
        "Chart render backend=pillow candlestick=manual_lines_rectangles no_hlines_scatter_for_price source=%s event_type=%s title=%r",
        context.source,
        context.event_type,
        title,
    )

    image = Image.new("RGB", (1200, 700), "#101418")
    draw = ImageDraw.Draw(image)
    draw.text((24, 18), title, fill="#f0f6fc")
    plot_left, plot_top, plot_right, plot_bottom = 60, 70, 1140, 620
    draw.rectangle((plot_left, plot_top, plot_right, plot_bottom), outline="#3d444d")

    lows = frame["low"].astype(float)
    highs = frame["high"].astype(float)
    min_price = float(lows.min())
    max_price = float(highs.max())
    price_range = max(max_price - min_price, 0.00001)
    step = (plot_right - plot_left) / max(len(frame), 1)
    pixels_per_price = (plot_bottom - plot_top) / price_range
    min_body_pixels = max(_min_body_height(frame, price_range, source=context.source) * pixels_per_price, 2)

    def y_for(price: float) -> float:
        return plot_bottom - ((price - min_price) / price_range) * (plot_bottom - plot_top)

    def draw_series(values: pd.Series, color: str, width: int = 2) -> None:
        points = []
        for index, value in enumerate(values):
            if pd.isna(value):
                continue
            x = plot_left + step * index + step / 2
            points.append((x, y_for(float(value))))
        if len(points) > 1:
            draw.line(points, fill=color, width=width)

    for index, row in enumerate(frame.itertuples()):
        x = plot_left + step * index + step / 2
        color = "#2ecc71" if row.close >= row.open else "#e74c3c"
        draw.line((x, y_for(row.low), x, y_for(row.high)), fill=color, width=3)
        body_top = y_for(max(row.open, row.close))
        body_bottom = y_for(min(row.open, row.close))
        if body_bottom - body_top < min_body_pixels:
            midpoint = (body_top + body_bottom) / 2
            body_top = midpoint - (min_body_pixels / 2)
            body_bottom = midpoint + (min_body_pixels / 2)
        half_width = max(4, int(step * 0.32))
        draw.rectangle((x - half_width, body_top, x + half_width, body_bottom), fill=color)

    close = frame["close"]
    draw_series(close.rolling(20).mean(), "#f1c40f", width=2)
    draw_series(close.rolling(75).mean(), "#3498db", width=2)
    draw_series(close.ewm(span=20, adjust=False).mean(), "#e67e22", width=2)

    if _show_bollinger(context):
        bands = _calculate_bollinger_bands(close)
        for band_name, style in _bollinger_band_styles(context).items():
            draw_series(bands[band_name], style["color"], width=3 if style["highlight"] else 2)

    image.save(output_path, format="PNG")


def render_chart_snapshot(
    candles: pd.DataFrame,
    output_path: Path,
    *,
    title: str,
    dry_run: bool = False,
    event_type: str | None = None,
    metadata: dict[str, Any] | None = None,
    source: str = "scheduled",
) -> Path:
    """Render candles and indicators to a PNG and return the output path."""
    output_path = Path(output_path)
    if dry_run:
        return output_path

    frame = _prepare_frame(candles)
    context = RenderContext(event_type=event_type, metadata=metadata, source=source)
    _log_render_input(frame, title, context)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _render_with_matplotlib(frame, output_path, title, context)
    except ModuleNotFoundError:
        _render_with_pillow(frame, output_path, title, context)
    return output_path
