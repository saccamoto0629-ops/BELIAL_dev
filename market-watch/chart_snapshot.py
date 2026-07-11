"""Build and send Berial chart snapshots.

This module is independent from news_feed_bot.py and is observation-only. It
does not implement entries, exits, lot sizing, orders, or automated trading.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import requests

from chart_collector import (
    BASE_DIR,
    DEFAULT_PERIOD,
    chart_db_path,
    collect_all,
    insert_chart_snapshot,
    load_candles,
    load_chart_targets,
    load_env_file,
)
from chart_models import ChartTarget, IndicatorSnapshot
from chart_renderer import render_chart_snapshot
from chart_image_uploader import create_s3_client, generate_presigned_chart_url, load_s3_config, upload_chart_image
from indicators import calculate_indicators, classify_bb_state, classify_trend
from market_state import classify_market_state

# Issue #28: freshness / bias_weight / confirmed-candle rules are shared with
# Backtest via the dependency-light ``freshness`` module. Re-exported here so
# existing callers of ``chart_snapshot.freshness_from_timestamp`` /
# ``is_market_closed_window`` keep working unchanged.
from freshness import (  # noqa: F401  (re-exported for backward compatibility)
    JST,
    MARKET_CLOSED_BIAS_WEIGHT,
    STALE_AFTER,
    freshness_from_timestamp,
    is_market_closed_window,
    parse_timestamp_utc,
    resolve_freshness,
    select_confirmed_candles,
)

REQUEST_TIMEOUT_SECONDS = 20


def _float_or_none(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def build_indicator_snapshot(
    target: ChartTarget,
    candles: pd.DataFrame,
    *,
    now_utc: datetime | pd.Timestamp | None = None,
) -> IndicatorSnapshot | None:
    if candles.empty:
        return None

    # Issue #28: evaluate against an explicit time and drop the still-forming
    # candle, so Live picks the same latest *confirmed* bar Backtest would for
    # the same (candles, evaluation_time). Unknown timeframe labels fall back
    # to using every candle, preserving the previous behaviour.
    evaluation_time = now_utc if now_utc is not None else datetime.now(UTC)
    try:
        candles = select_confirmed_candles(candles, timeframe=target.timeframe, evaluation_time=evaluation_time)
    except ValueError:
        pass
    if candles.empty:
        return None

    indicators = calculate_indicators(candles)
    latest = indicators.iloc[-1]
    close = float(latest["close"])
    sma20 = _float_or_none(latest.get("sma20"))
    sma75 = _float_or_none(latest.get("sma75"))
    bb_position = _float_or_none(latest.get("bb_position"))
    macd = _float_or_none(latest.get("macd"))
    macd_signal = _float_or_none(latest.get("macd_signal"))
    macd_hist = _float_or_none(latest.get("macd_hist"))
    rsi14 = _float_or_none(latest.get("rsi14"))
    timestamp = indicators.index[-1].isoformat()
    # Issue #28: freshness / age / bias_weight all come from the one shared
    # rule, evaluated at the same explicit time used for confirmed-candle
    # selection above.
    freshness, age_seconds, bias_weight = resolve_freshness(timestamp, evaluation_time=evaluation_time)
    is_stale = freshness == "Stale"

    market_state_result = (
        {"market_state": "unknown", "confidence": 0.0, "reasons": ["Data is stale."]}
        if is_stale
        else classify_market_state(indicators)
    )

    return IndicatorSnapshot(
        symbol=target.symbol,
        timeframe=target.timeframe,
        timestamp=timestamp,
        close=close,
        sma20=sma20,
        sma75=sma75,
        ema20=_float_or_none(latest.get("ema20")),
        bb_lower=_float_or_none(latest.get("bb_lower")),
        bb_middle=_float_or_none(latest.get("bb_middle")),
        bb_upper=_float_or_none(latest.get("bb_upper")),
        bb_width_pct=_float_or_none(latest.get("bb_width_pct")),
        bb_position=bb_position,
        macd=macd,
        macd_signal=macd_signal,
        macd_hist=macd_hist,
        macd_state=str(latest.get("macd_state") or "Neutral"),
        rsi14=rsi14,
        rsi_state=str(latest.get("rsi_state") or "Unavailable"),
        freshness=freshness,
        age_seconds=age_seconds,
        bias_weight=bias_weight,
        trend="Stale" if is_stale else classify_trend(close, sma20, sma75),
        state="Stale" if is_stale else classify_bb_state(bb_position),
        display_name=target.display_name,
        chart_symbol=target.chart_symbol,
        asset_type=target.asset_type,
        market_state=market_state_result["market_state"],
        market_state_confidence=market_state_result["confidence"],
        market_state_reasons=tuple(market_state_result["reasons"]),
    )


def _fmt(value: float | None, digits: int = 2) -> str:
    return "N/A" if value is None else f"{value:.{digits}f}"


def format_timestamp_jst(timestamp: str) -> str:
    return parse_timestamp_utc(timestamp).tz_convert(JST).strftime("%Y-%m-%d %H:%M JST")


def format_age(age_seconds: int) -> str:
    hours, remainder = divmod(max(age_seconds, 0), 3600)
    minutes = remainder // 60
    if hours >= 24:
        days, day_hours = divmod(hours, 24)
        return f"{days}d {day_hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_snapshot(snapshot: IndicatorSnapshot) -> str:
    return "\n".join(
        [
            f"{snapshot.symbol} {snapshot.timeframe}",
            f"Display: {snapshot.label}",
            f"Chart Symbol: {snapshot.chart_symbol or snapshot.symbol}",
            f"Time: {format_timestamp_jst(snapshot.timestamp)}",
            f"Freshness: {snapshot.freshness} ({format_age(snapshot.age_seconds)} old)",
            f"Bias Weight: {_fmt(snapshot.bias_weight)}",
            f"Close: {_fmt(snapshot.close)}",
            f"SMA20: {_fmt(snapshot.sma20)}",
            f"SMA75: {_fmt(snapshot.sma75)}",
            f"EMA20: {_fmt(snapshot.ema20)}",
            f"BB: {_fmt(snapshot.bb_lower)} / {_fmt(snapshot.bb_middle)} / {_fmt(snapshot.bb_upper)}",
            f"BB Width: {_fmt(snapshot.bb_width_pct)}%",
            f"BB Position: {_fmt(snapshot.bb_position)}",
            f"MACD: {_fmt(snapshot.macd, 5)} / Signal: {_fmt(snapshot.macd_signal, 5)} / Hist: {_fmt(snapshot.macd_hist, 5)}",
            f"MACD State: {snapshot.macd_state}",
            f"RSI14: {_fmt(snapshot.rsi14)}",
            f"RSI State: {snapshot.rsi_state}",
            f"Trend: {snapshot.trend}",
            f"State: {snapshot.state}",
            f"Market State: {snapshot.market_state} ({round(snapshot.market_state_confidence * 100)}%)",
        ]
    )


def build_chart_snapshot_message(snapshots: list[IndicatorSnapshot]) -> str:
    if not snapshots:
        return "📈 Chart Snapshot\n\n- unavailable"
    sections = ["📈 Chart Snapshot"]
    sections.extend(format_snapshot(snapshot) for snapshot in snapshots)
    return "\n\n".join(sections)


def _json_default(value):
    if isinstance(value, (datetime, pd.Timestamp)):
        return pd.Timestamp(value).isoformat()
    return str(value)


def post_discord_message(
    webhook_url: str,
    message: str,
    *,
    dry_run: bool = False,
    image_urls: list[str] | None = None,
    session=requests,
) -> None:
    if dry_run:
        print(f"[DRY_RUN] Would send Chart Snapshot:\n{message}")
        if image_urls:
            print(f"[DRY_RUN] Would include {len(image_urls)} presigned chart image URL(s)")
        return
    payload = {"content": message[:2000]}
    if image_urls:
        payload["embeds"] = [{"image": {"url": url}} for url in image_urls[:10]]
    response = session.post(
        webhook_url,
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def load_webhook_url(env_values: dict[str, str]) -> str:
    return env_values.get("CHART_SNAPSHOT_WEBHOOK_URL", "").strip()


def collect_and_build_snapshots(
    targets: list[ChartTarget],
    db_path: Path,
    *,
    period: str = DEFAULT_PERIOD,
    collect: bool = True,
) -> list[IndicatorSnapshot]:
    if collect:
        collect_all(targets, db_path, period=period)

    snapshots: list[IndicatorSnapshot] = []
    for target in targets:
        candles = load_candles(db_path, target.symbol, target.timeframe, limit=200)
        snapshot = build_indicator_snapshot(target, candles)
        if snapshot is not None:
            snapshots.append(snapshot)
    return snapshots


def persist_snapshot_images(
    snapshots: list[IndicatorSnapshot],
    db_path: Path,
    env_values: dict[str, str],
    *,
    dry_run: bool = False,
) -> list[str]:
    if dry_run or not snapshots:
        return []

    config = load_s3_config(env_values)
    client = create_s3_client(config)
    image_urls: list[str] = []
    image_dir = BASE_DIR / "data" / "chart_images"
    snapshot_at = datetime.now(UTC)

    for snapshot in snapshots:
        candles = load_candles(db_path, snapshot.symbol, snapshot.timeframe, limit=200)
        local_path = image_dir / f"{snapshot.symbol}_{snapshot.timeframe}_{snapshot_at:%Y%m%d%H%M%S}.png"
        render_chart_snapshot(
            candles,
            local_path,
            title=f"{snapshot.label} {snapshot.timeframe} Chart Snapshot",
            source="scheduled",
        )
        s3_key = upload_chart_image(
            local_path,
            symbol=snapshot.symbol,
            timeframe=snapshot.timeframe,
            snapshot_at=snapshot_at,
            config=config,
            s3_client=client,
        )
        snapshot_id = insert_chart_snapshot(
            db_path,
            symbol=snapshot.symbol,
            timeframe=snapshot.timeframe,
            snapshot_at=snapshot_at.isoformat(),
            s3_bucket=config["bucket"],
            s3_key=s3_key,
            captured_at=snapshot_at.isoformat(),
            candle_time=snapshot.timestamp,
            ohlcv_json=candles.tail(120).reset_index().to_json(orient="records", date_format="iso"),
            indicators_json=json.dumps(snapshot.__dict__, ensure_ascii=False, default=_json_default),
        )
        image_urls.append(generate_presigned_chart_url(s3_key, config=config, s3_client=client))
    return image_urls


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Send Berial chart snapshot to Discord.")
    parser.add_argument("--once", action="store_true", help="Collect charts, send one snapshot, and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Print payload instead of sending to Discord.")
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Use existing SQLite candles without fetching from Yahoo Finance.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    env_values = load_env_file()
    dry_run = args.dry_run or env_values.get("DRY_RUN", "").lower() in {"1", "true", "yes", "on"}
    webhook_url = load_webhook_url(env_values)
    if not dry_run and not webhook_url:
        raise RuntimeError("CHART_SNAPSHOT_WEBHOOK_URL is not set in market-watch/.env")

    targets = load_chart_targets(env_values)
    db_path = chart_db_path(env_values)
    period = env_values.get("CHART_HISTORY_PERIOD", DEFAULT_PERIOD)
    if dry_run and args.skip_fetch and not db_path.exists():
        snapshots = []
    else:
        snapshots = collect_and_build_snapshots(targets, db_path, period=period, collect=not args.skip_fetch)
    message = build_chart_snapshot_message(snapshots)
    image_urls = persist_snapshot_images(snapshots, db_path, env_values, dry_run=dry_run)
    post_discord_message(webhook_url, message, dry_run=dry_run, image_urls=image_urls)

    if not args.once:
        print("[INFO] Chart snapshot sent once. Add scheduling later if continuous mode is needed.")


if __name__ == "__main__":
    main()
