"""Bollinger Bands alert bot: posts confirmed-candle market-state events.

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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from chart_alert_context import prepare_alert_image
from indicators import detect_bollinger_signals

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
CONFIG_FILE = BASE_DIR / "config.toml"
LOGS_DIR = BASE_DIR.parent / "logs"
LOG_FILE = LOGS_DIR / "bollinger_alert_bot.log"
STATE_DIR = BASE_DIR / "state"
STATE_FILE = STATE_DIR / "bollinger_alert_state.json"

REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_NOTIFY = {
    "upper_break": True,
    "lower_break": True,
    "expansion_start": True,
    "upper_touch": False,
    "lower_touch": False,
    "squeeze": False,
}
DEFAULT_CONFIG = {
    "symbol": "USD_JPY",
    "ticker": "JPY=X",
    "timeframe": "M15",
    "interval": "15m",
    "period": "5d",
    "enabled": True,
    "window": 20,
    "std_multiplier": 2.0,
    "squeeze_lookback": 120,
    "squeeze_percentile": 0.10,
    "expansion_ratio": 1.30,
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
    window: int
    std_multiplier: float
    squeeze_lookback: int
    squeeze_percentile: float
    expansion_ratio: float
    notify: dict[str, bool] = field(default_factory=dict)
    check_interval_seconds: int = 60
    log_level: str = "INFO"


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


def _config_notify(value):
    if not isinstance(value, dict):
        raise RuntimeError(f"Invalid config value for bollinger.notify: {value!r}")
    notify = dict(DEFAULT_NOTIFY)
    for key, enabled in value.items():
        if key in notify:
            notify[key] = _config_bool(enabled)
    return notify


def load_config(path=CONFIG_FILE):
    values = dict(DEFAULT_CONFIG)
    notify = dict(DEFAULT_NOTIFY)
    if path.exists():
        try:
            with path.open("rb") as config_file:
                raw_config = tomllib.load(config_file)
        except tomllib.TOMLDecodeError as exc:
            raise RuntimeError(f"Failed to parse config file {path}: {exc}") from exc
        except OSError as exc:
            raise RuntimeError(f"Failed to read config file {path}: {exc}") from exc

        monitor = raw_config.get("monitor", {})
        bollinger = raw_config.get("bollinger", {})
        runtime_config = raw_config.get("runtime", {})
        logging_config = raw_config.get("logging", {})
        sections = (monitor, bollinger, runtime_config, logging_config)
        if not all(isinstance(section, dict) for section in sections):
            raise RuntimeError(f"Invalid config file {path}: sections must be TOML tables")

        values.update({key: value for key, value in monitor.items() if key in values})
        notify = _config_notify(bollinger.get("notify", notify))
        values.update({key: value for key, value in bollinger.items() if key in values})
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
        window=_config_int(values, "window"),
        std_multiplier=_config_float(values, "std_multiplier"),
        squeeze_lookback=_config_int(values, "squeeze_lookback"),
        squeeze_percentile=_config_float(values, "squeeze_percentile"),
        expansion_ratio=_config_float(values, "expansion_ratio"),
        notify=notify,
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
WINDOW = CONFIG.window
STD_MULTIPLIER = CONFIG.std_multiplier
SQUEEZE_LOOKBACK = CONFIG.squeeze_lookback
SQUEEZE_PERCENTILE = CONFIG.squeeze_percentile
EXPANSION_RATIO = CONFIG.expansion_ratio
NOTIFY = CONFIG.notify
CHECK_INTERVAL_SECONDS = CONFIG.check_interval_seconds
LOG_LEVEL = CONFIG.log_level


def setup_logger():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("bollinger_alert_bot")
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
    if WINDOW < 1:
        errors.append("window must be at least 1")
    if STD_MULTIPLIER <= 0:
        errors.append("std_multiplier must be positive")
    if SQUEEZE_LOOKBACK < 1:
        errors.append("squeeze_lookback must be at least 1")
    if not 0 <= SQUEEZE_PERCENTILE <= 1:
        errors.append("squeeze_percentile must be between 0 and 1")
    if EXPANSION_RATIO <= 0:
        errors.append("expansion_ratio must be positive")
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


def state_key(symbol, timeframe, signal_type, candle_time):
    return f"{symbol}_{timeframe}_{signal_type}_{candle_time}"


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


def build_bollinger_embed(event):
    detected_at = event["detected_at"]
    description = (
        f"[BOLLINGER] {event['symbol']} {event['timeframe']} {event['signal_type']}\n"
        f"close={event['close']:.3f} upper={event['upper']:.3f} "
        f"middle={event['middle']:.3f} lower={event['lower']:.3f}\n"
        f"bandwidth={event['bandwidth'] * 100:.2f}%\n"
        f"note={event['note']} エントリー判断は手法側で確認。"
    )
    return {
        "title": "Berial Market Watch: Bollinger Bands",
        "color": 0x9B59B6,
        "description": description,
        "fields": [
            {"name": "Symbol", "value": event["symbol"], "inline": True},
            {"name": "Timeframe", "value": event["timeframe"], "inline": True},
            {"name": "Signal", "value": event["signal_type"], "inline": True},
            {"name": "Candle time", "value": event["candle_time"], "inline": False},
            {"name": "Close", "value": f"{event['close']:.3f}", "inline": True},
            {"name": "Upper", "value": f"{event['upper']:.3f}", "inline": True},
            {"name": "Middle", "value": f"{event['middle']:.3f}", "inline": True},
            {"name": "Lower", "value": f"{event['lower']:.3f}", "inline": True},
            {"name": "Bandwidth", "value": f"{event['bandwidth'] * 100:.2f}%", "inline": True},
            {"name": "Note", "value": event["note"], "inline": False},
            {"name": "Detected at", "value": detected_at, "inline": False},
        ],
        "timestamp": detected_at,
    }


def notify_bollinger(event, *, dry_run=False, candles=None):
    image_url = None
    if candles is None:
        logger.warning("Chart image skipped because OHLC candles were not provided for Bollinger alert.")
    else:
        try:
            _snapshot_id, image_url = prepare_alert_image(
                alert_type=f"bollinger_{event['signal_type']}",
                symbol=event["symbol"],
                timeframe=event["timeframe"],
                candle_time=event["candle_time"],
                candles=candles,
                metadata=event,
                dry_run=dry_run,
            )
        except ModuleNotFoundError as exc:
            logger.warning("Chart image presigned URL skipped because dependency is missing: %s", exc)
    embed = build_bollinger_embed(event)
    if image_url:
        post_discord_embed(embed, dry_run=dry_run, image_url=image_url)
    else:
        post_discord_embed(embed, dry_run=dry_run)
    logger.info(
        "Bollinger notified: symbol=%s timeframe=%s signal=%s candle=%s close=%.3f bandwidth=%.5f",
        event["symbol"],
        event["timeframe"],
        event["signal_type"],
        event["candle_time"],
        event["close"],
        event["bandwidth"],
    )


def build_bollinger_event(signal, candle_time):
    now = datetime.now(timezone.utc).isoformat()
    return {
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "signal_type": signal["signal_type"],
        "candle_time": candle_time,
        "close": float(signal["close"]),
        "upper": float(signal["upper"]),
        "middle": float(signal["middle"]),
        "lower": float(signal["lower"]),
        "bandwidth": float(signal["bandwidth"]),
        "note": signal["note"],
        "notified_at": now,
        "detected_at": now,
    }


def run_once(state, *, dry_run=False):
    if not ENABLED:
        logger.info("Bollinger Alert is disabled; skipping.")
        return

    try:
        candles = fetch_candles()
    except Exception as exc:
        logger.error("Failed to fetch candles: %s", exc)
        return

    min_bars = WINDOW + 2
    if len(candles) < min_bars:
        logger.warning("Not enough candles yet: got %s, need at least %s", len(candles), min_bars)
        return

    confirmed_position = -2
    candle_time = candles.index[confirmed_position].isoformat()
    signals = detect_bollinger_signals(
        candles,
        current_position=confirmed_position,
        window=WINDOW,
        std_multiplier=STD_MULTIPLIER,
        squeeze_lookback=SQUEEZE_LOOKBACK,
        squeeze_percentile=SQUEEZE_PERCENTILE,
        expansion_ratio=EXPANSION_RATIO,
    )

    for signal in signals:
        signal_type = signal["signal_type"]
        if not NOTIFY.get(signal_type, False):
            logger.info("Bollinger signal disabled by config: %s", signal_type)
            continue

        key = state_key(SYMBOL, TIMEFRAME, signal_type, candle_time)
        if key in state:
            logger.info("Bollinger signal already notified for %s; skipping.", key)
            continue

        event = build_bollinger_event(signal, candle_time)
        notify_bollinger(event, dry_run=dry_run, candles=candles)
        if not dry_run:
            state[key] = {
                "candle_time": candle_time,
                "signal_type": signal_type,
                "close": event["close"],
                "upper": event["upper"],
                "middle": event["middle"],
                "lower": event["lower"],
                "bandwidth": event["bandwidth"],
                "notified_at": event["notified_at"],
            }
            save_state(state)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Post Bollinger Bands alerts to Discord.")
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
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    validate_config(dry_run=args.dry_run)

    logger.info(
        "=== Bollinger Alert Bot v1 === symbol=%s ticker=%s timeframe=%s interval=%s period=%s window=%s std_multiplier=%s squeeze_lookback=%s squeeze_percentile=%s expansion_ratio=%s check_interval=%ss dry_run=%s",
        SYMBOL,
        TICKER,
        TIMEFRAME,
        INTERVAL,
        HISTORY_PERIOD,
        WINDOW,
        STD_MULTIPLIER,
        SQUEEZE_LOOKBACK,
        SQUEEZE_PERCENTILE,
        EXPANSION_RATIO,
        CHECK_INTERVAL_SECONDS,
        args.dry_run,
    )

    state = load_state()

    while True:
        run_once(state, dry_run=args.dry_run)
        if args.once:
            break
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
