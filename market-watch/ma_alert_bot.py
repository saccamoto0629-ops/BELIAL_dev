"""MA touch alert bot: posts confirmed-candle moving-average touch events.

This module is observation-only. It does not generate trade decisions,
entries, exits, lot sizes, orders, or automated trading actions.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from chart_alert_context import prepare_alert_image
from chart_renderer import render_chart_snapshot
from indicators import calculate_indicators, detect_ma_touch

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
CONFIG_FILE = BASE_DIR / "config.toml"
LOGS_DIR = BASE_DIR.parent / "logs"
LOG_FILE = LOGS_DIR / "ma_alert_bot.log"
STATE_DIR = BASE_DIR / "state"
STATE_FILE = STATE_DIR / "ma_alert_state.json"

REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_MA_NAMES = ["sma20", "sma75", "ema20"]

DEFAULT_CONFIG = {
    "symbol": "USD_JPY",
    "ticker": "JPY=X",
    "timeframe": "M15",
    "interval": "15m",
    "period": "5d",
    "enabled": True,
    "threshold_pct": 0.05,
    "ma_names": DEFAULT_MA_NAMES,
    "check_interval_seconds": 60,
    "log_level": "INFO",
}


@dataclass(frozen=True)
class BotConfig:
    symbol: str
    ticker: str
    timeframe: str
    interval: str
    period: str
    enabled: bool
    threshold_pct: float
    ma_names: list[str]
    check_interval_seconds: int
    log_level: str


def load_env_file(path=ENV_FILE):
    if not path.exists():
        return {}

    values = {}
    with path.open("r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            if key:
                values[key] = value
    return values


def _config_int(values, key):
    try:
        return int(values[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid config value for {key}: {values.get(key)!r}") from exc


def _config_float(values, key):
    try:
        return float(values[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid config value for {key}: {values.get(key)!r}") from exc


def _config_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _config_ma_names(value):
    if isinstance(value, str):
        return [name.strip() for name in value.split(",") if name.strip()]
    if isinstance(value, list) and all(isinstance(name, str) for name in value):
        return [name.strip() for name in value if name.strip()]
    raise RuntimeError(f"Invalid config value for ma_names: {value!r}")


def load_config(path=CONFIG_FILE):
    values = dict(DEFAULT_CONFIG)
    values["ma_names"] = list(DEFAULT_MA_NAMES)
    if path.exists():
        try:
            with path.open("rb") as config_file:
                raw_config = tomllib.load(config_file)
        except tomllib.TOMLDecodeError as exc:
            raise RuntimeError(f"Failed to parse config file {path}: {exc}") from exc
        except OSError as exc:
            raise RuntimeError(f"Failed to read config file {path}: {exc}") from exc

        monitor = raw_config.get("monitor", {})
        ma_alert = raw_config.get("ma_alert", {})
        runtime_config = raw_config.get("runtime", {})
        logging_config = raw_config.get("logging", {})
        sections = (monitor, ma_alert, runtime_config, logging_config)
        if not all(isinstance(section, dict) for section in sections):
            raise RuntimeError(f"Invalid config file {path}: sections must be TOML tables")

        values.update({key: value for key, value in monitor.items() if key in values})
        values.update({key: value for key, value in ma_alert.items() if key in values})
        values["check_interval_seconds"] = int(
            os.getenv(
                "CHECK_INTERVAL_SECONDS",
                runtime_config.get("check_interval_seconds", DEFAULT_CONFIG["check_interval_seconds"]),
            )
        )
        values.update({key: value for key, value in logging_config.items() if key in values})

    env_overrides = {
        "symbol": os.getenv("SYMBOL") or ENV_VALUES.get("SYMBOL"),
        "timeframe": os.getenv("TIMEFRAME") or ENV_VALUES.get("TIMEFRAME"),
        "threshold_pct": os.getenv("MA_ALERT_THRESHOLD_PCT") or ENV_VALUES.get("MA_ALERT_THRESHOLD_PCT"),
        "ma_names": os.getenv("MA_ALERT_MA_NAMES") or ENV_VALUES.get("MA_ALERT_MA_NAMES"),
        "check_interval_seconds": os.getenv("CHECK_INTERVAL_SECONDS"),
        "log_level": os.getenv("LOG_LEVEL") or ENV_VALUES.get("LOG_LEVEL"),
    }
    values.update({key: value for key, value in env_overrides.items() if value not in (None, "")})

    return BotConfig(
        symbol=str(values["symbol"]).strip(),
        ticker=str(values["ticker"]).strip(),
        timeframe=str(values["timeframe"]).strip(),
        interval=str(values["interval"]).strip(),
        period=str(values["period"]).strip(),
        enabled=_config_bool(values["enabled"]),
        threshold_pct=_config_float(values, "threshold_pct"),
        ma_names=_config_ma_names(values["ma_names"]),
        check_interval_seconds=_config_int(values, "check_interval_seconds"),
        log_level=str(values["log_level"]).strip().upper(),
    )


ENV_VALUES = load_env_file()
CONFIG = load_config()

DISCORD_WEBHOOK_URL = ENV_VALUES.get("BOT_ALERTS_WEBHOOK_URL", ENV_VALUES.get("DISCORD_WEBHOOK_URL", "")).strip()
SYMBOL = CONFIG.symbol
TICKER = CONFIG.ticker
TIMEFRAME = CONFIG.timeframe
INTERVAL = CONFIG.interval
HISTORY_PERIOD = CONFIG.period
ENABLED = CONFIG.enabled
THRESHOLD_PCT = CONFIG.threshold_pct
MA_NAMES = CONFIG.ma_names
CHECK_INTERVAL_SECONDS = CONFIG.check_interval_seconds
LOG_LEVEL = CONFIG.log_level


def setup_logger():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ma_alert_bot")
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


logger = setup_logger()


def validate_config(*, dry_run=False):
    errors = []
    if not dry_run and not DISCORD_WEBHOOK_URL:
        errors.append("BOT_ALERTS_WEBHOOK_URL or DISCORD_WEBHOOK_URL is not set in .env")
    if not SYMBOL:
        errors.append("symbol must not be empty")
    if not TICKER:
        errors.append("ticker must not be empty")
    if not TIMEFRAME:
        errors.append("timeframe must not be empty")
    if not INTERVAL:
        errors.append("interval must not be empty")
    if not HISTORY_PERIOD:
        errors.append("period must not be empty")
    if THRESHOLD_PCT < 0:
        errors.append("threshold_pct must be non-negative")
    if not MA_NAMES:
        errors.append("ma_names must not be empty")
    if CHECK_INTERVAL_SECONDS < 1:
        errors.append("CHECK_INTERVAL_SECONDS must be at least 1")
    if errors:
        raise RuntimeError("; ".join(errors))


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as state_file:
            return json.load(state_file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read state file, starting fresh: %s", exc)
        return {}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = STATE_FILE.with_suffix(".json.tmp")
    with temp_path.open("w", encoding="utf-8") as state_file:
        json.dump(state, state_file, ensure_ascii=False, indent=2)
    os.replace(temp_path, STATE_FILE)


def state_key(symbol, timeframe, ma_name, candle_time):
    return f"{symbol}_{timeframe}_{ma_name}_{candle_time}"


def fetch_candles():
    ticker = yf.Ticker(TICKER)
    history = ticker.history(
        period=HISTORY_PERIOD,
        interval=INTERVAL,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    frame = history.rename(columns=str.lower)
    columns = [column for column in ["open", "high", "low", "close", "volume"] if column in frame.columns]
    frame = frame[columns].dropna(subset=["open", "high", "low", "close"])
    if "volume" not in frame.columns:
        frame["volume"] = 0.0
    logger.info("Fetched candles: symbol=%s timeframe=%s rows=%s", SYMBOL, TIMEFRAME, len(frame))
    return frame


def build_indicators(candles):
    if "close" not in candles.columns:
        raise ValueError("candles must contain a 'close' column")
    return calculate_indicators(candles[["close"]].copy())


def post_discord_embed(embed, *, dry_run=False, image_url=None):
    if image_url:
        embed = dict(embed)
        embed["image"] = {"url": image_url}
    if dry_run:
        logger.info("[DRY_RUN] Would post to Discord: %s", json.dumps(embed, ensure_ascii=False))
        return

    try:
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"embeds": [embed]},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        logger.info("Discord notification sent: title=%s", embed.get("title"))
    except requests.RequestException:
        logger.exception("Discord notification failed: title=%s", embed.get("title"))
        raise


def build_touch_embed(event):
    detected_at = event["detected_at"]
    return {
        "title": "Berial Market Watch: MA Touch",
        "color": 0x3498DB,
        "description": "This is a watch alert only, not a trade decision.",
        "fields": [
            {"name": "Symbol", "value": event["symbol"], "inline": True},
            {"name": "Timeframe", "value": event["timeframe"], "inline": True},
            {"name": "MA name", "value": event["ma_name"], "inline": True},
            {"name": "Candle time", "value": event["candle_time"], "inline": False},
            {"name": "Close", "value": f"{event['close']:.3f}", "inline": True},
            {"name": "MA value", "value": f"{event['ma_value']:.3f}", "inline": True},
            {"name": "Distance pct", "value": f"{event['distance_pct']:.5f}%", "inline": True},
            {"name": "Side", "value": event["side"], "inline": True},
            {"name": "Detected at", "value": detected_at, "inline": False},
        ],
        "timestamp": detected_at,
    }


def notify_touch(event, *, dry_run=False, candles=None):
    image_url = None
    if candles is None:
        logger.warning("Chart image skipped because OHLC candles were not provided for MA alert.")
    else:
        try:
            logger.info(
                "MA alert image route: notify_touch -> %s.%s -> chart_renderer.render_chart_snapshot source=alert alert_type=ma_touch",
                prepare_alert_image.__module__,
                prepare_alert_image.__name__,
            )
            _snapshot_id, image_url = prepare_alert_image(
                alert_type="ma_touch",
                symbol=event["symbol"],
                timeframe=event["timeframe"],
                candle_time=event["candle_time"],
                candles=candles,
                metadata=event,
                dry_run=dry_run,
            )
        except ModuleNotFoundError as exc:
            logger.warning("Chart image presigned URL skipped because dependency is missing: %s", exc)
    embed = build_touch_embed(event)
    if image_url:
        post_discord_embed(embed, dry_run=dry_run, image_url=image_url)
    else:
        post_discord_embed(embed, dry_run=dry_run)
    logger.info(
        "MA touch notified: symbol=%s timeframe=%s ma=%s candle=%s distance_pct=%.5f side=%s",
        event["symbol"],
        event["timeframe"],
        event["ma_name"],
        event["candle_time"],
        event["distance_pct"],
        event["side"],
    )


def build_touch_event(ma_name, candle_time, close_price, ma_value, touch):
    return {
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "ma_name": ma_name,
        "candle_time": candle_time,
        "close": float(close_price),
        "ma_value": float(ma_value),
        "distance_pct": float(touch["distance_pct"]),
        "side": touch["side"],
        "notified_at": datetime.now(timezone.utc).isoformat(),
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }


def debug_render_ma_touch_image(output_path, *, candles=None):
    candles = fetch_candles() if candles is None else candles
    if not isinstance(candles, pd.DataFrame):
        raise TypeError("debug ma_touch render requires an OHLC DataFrame")
    indicators = build_indicators(candles)
    if len(indicators) < 3:
        raise RuntimeError("not enough candles to render ma_touch debug image")

    confirmed_idx = -2
    row = indicators.iloc[confirmed_idx]
    candle_time = indicators.index[confirmed_idx].isoformat()
    close_price = row["close"]
    ma_name = MA_NAMES[0]
    ma_value = row[ma_name]
    touch = detect_ma_touch(close_price, ma_value, threshold_pct=THRESHOLD_PCT) or {
        "distance_pct": abs(close_price - ma_value) / ma_value * 100 if ma_value else 0.0,
        "side": "above" if close_price >= ma_value else "below",
    }
    event = build_touch_event(ma_name, candle_time, close_price, ma_value, touch)
    logger.info(
        "MA debug render route: debug_render_ma_touch_image -> chart_renderer.render_chart_snapshot source=alert-debug alert_type=ma_touch output=%s",
        output_path,
    )
    return render_chart_snapshot(
        candles,
        Path(output_path),
        title=f"{event['symbol']} {event['timeframe']} ma_touch",
        event_type="ma_touch",
        metadata=event,
        source="alert-debug",
    )


def run_once(state, *, dry_run=False):
    if not ENABLED:
        logger.info("MA Alert is disabled; skipping.")
        return

    try:
        candles = fetch_candles()
    except Exception as exc:
        logger.error("Failed to fetch candles: %s", exc)
        return

    if len(candles) < 3:
        logger.warning("Not enough candles yet: got %s, need at least 3", len(candles))
        return

    indicators = build_indicators(candles)
    confirmed_idx = -2
    row = indicators.iloc[confirmed_idx]
    candle_time = indicators.index[confirmed_idx].isoformat()
    close_price = row["close"]

    for ma_name in MA_NAMES:
        if ma_name not in indicators.columns:
            logger.warning("Unknown MA name in config: %s", ma_name)
            continue

        ma_value = row[ma_name]
        touch = detect_ma_touch(close_price, ma_value, threshold_pct=THRESHOLD_PCT)
        if not touch:
            continue

        key = state_key(SYMBOL, TIMEFRAME, ma_name, candle_time)
        if key in state:
            logger.info("MA touch already notified for %s; skipping.", key)
            continue

        event = build_touch_event(ma_name, candle_time, close_price, ma_value, touch)
        notify_touch(event, dry_run=dry_run, candles=candles)
        if not dry_run:
            state[key] = {
                "candle_time": candle_time,
                "ma_name": ma_name,
                "close": event["close"],
                "ma_value": event["ma_value"],
                "distance_pct": event["distance_pct"],
                "side": event["side"],
                "notified_at": event["notified_at"],
            }
            save_state(state)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Post MA touch alerts to Discord.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print alerts instead of sending them to Discord, and do not update state.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check and exit instead of looping forever.",
    )
    parser.add_argument(
        "--debug-render",
        metavar="PNG_PATH",
        help="Render one local ma_touch chart image with the alert renderer and exit without S3, DB, or Discord.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    validate_config(dry_run=args.dry_run)

    logger.info(
        "=== MA Alert Bot v1 === symbol=%s ticker=%s timeframe=%s interval=%s period=%s threshold_pct=%s ma_names=%s check_interval=%ss dry_run=%s",
        SYMBOL,
        TICKER,
        TIMEFRAME,
        INTERVAL,
        HISTORY_PERIOD,
        THRESHOLD_PCT,
        ",".join(MA_NAMES),
        CHECK_INTERVAL_SECONDS,
        args.dry_run,
    )

    if args.debug_render:
        rendered = debug_render_ma_touch_image(args.debug_render)
        logger.info("MA debug render written: %s", rendered)
        print(f"[DEBUG_RENDER] {rendered}")
        return

    state = load_state()

    while True:
        run_once(state, dry_run=args.dry_run)
        if args.once:
            break
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
