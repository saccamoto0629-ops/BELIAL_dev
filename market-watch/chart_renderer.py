"""Render local PNG chart snapshots for BELIAL Market Watch.

Rendering is observation-only. It creates visual evidence for snapshots and
alerts, never trading instructions.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


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
    return frame[["open", "high", "low", "close", "volume"]].tail(120)


def _render_with_matplotlib(frame: pd.DataFrame, output_path: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    x_values = mdates.date2num(pd.to_datetime(frame.index).to_pydatetime())
    candle_width = 0.6 * (x_values[1] - x_values[0]) if len(x_values) > 1 else 0.02

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
        price_ax.vlines(x_value, row.low, row.high, color=color, linewidth=1.0)
        body_low = min(row.open, row.close)
        body_height = max(abs(row.close - row.open), 0.00001)
        price_ax.add_patch(
            plt.Rectangle(
                (x_value - candle_width / 2, body_low),
                candle_width,
                body_height,
                facecolor=color,
                edgecolor=color,
                linewidth=0.8,
            )
        )

    close = frame["close"]
    sma20 = close.rolling(20).mean()
    sma75 = close.rolling(75).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    price_ax.plot(x_values, sma20, color="#f1c40f", linewidth=1.2, label="SMA20")
    price_ax.plot(x_values, sma75, color="#3498db", linewidth=1.2, label="SMA75")
    price_ax.plot(x_values, ema20, color="#e67e22", linewidth=1.0, label="EMA20")
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

    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path, format="png", dpi=140)
    plt.close(fig)


def _render_with_pillow(frame: pd.DataFrame, output_path: Path, title: str) -> None:
    from PIL import Image, ImageDraw

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

    def y_for(price: float) -> float:
        return plot_bottom - ((price - min_price) / price_range) * (plot_bottom - plot_top)

    for index, row in enumerate(frame.itertuples()):
        x = plot_left + step * index + step / 2
        color = "#2ecc71" if row.close >= row.open else "#e74c3c"
        draw.line((x, y_for(row.low), x, y_for(row.high)), fill=color, width=2)
        body_top = y_for(max(row.open, row.close))
        body_bottom = y_for(min(row.open, row.close))
        draw.rectangle((x - 3, body_top, x + 3, max(body_bottom, body_top + 1)), fill=color)

    image.save(output_path, format="PNG")


def render_chart_snapshot(
    candles: pd.DataFrame,
    output_path: Path,
    *,
    title: str,
    dry_run: bool = False,
) -> Path:
    """Render candles and indicators to a PNG and return the output path."""
    output_path = Path(output_path)
    if dry_run:
        return output_path

    frame = _prepare_frame(candles)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _render_with_matplotlib(frame, output_path, title)
    except ModuleNotFoundError:
        _render_with_pillow(frame, output_path, title)
    return output_path
