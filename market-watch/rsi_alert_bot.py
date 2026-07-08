"""RSI14 alert bot: posts observation-only threshold transition events.

This module reports what happened. It does not generate trade decisions,
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

from indicators import calculate_indicators, classify_rsi_state, detect_rsi_zone_event

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
CONFIG_FILE = BASE_DIR / "config.toml"
LOGS_DIR = BASE_DIR.parent / "logs"
LOG_FILE = LOGS_DIR / "rsi_alert_bot.log"
STATE_DIR = BASE_DIR / "state"
STATE_FILE = STATE_DIR / "rsi_alert_state.json"

REQUEST_TIMEOUT_SECONDS = 20

DEFAULT_CONFIG = {
    "symbol": "USD_JPY",
    "ticker": "JPY=X",
    "timeframe": "M15",
    "interval": "15m",
    "period": "5d",
    "enabled": True,
    "overbought_threshold": 70.0,
    "oversold_threshold": 30.0,
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
    overbought_threshold: float
    oversold_threshold: float
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


def load_config(path=CONFIG_FILE):
    values = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            with path.open("rb") as config_file:
                raw_config = tomllib.load(config_file)
        except tomllib.TOMLDecodeError as exc:
            raise RuntimeError(f"Failed to parse config file {path}: {exc}") from exc
        except OSError as exc:
            raise RuntimeError(f"Failed to read config file {path}: {exc}") from exc

        monitor = raw_config.get("monitor", {})
        rsi_alert = raw_config.get("rsi_alert", {})
        runtime_config = raw_config.get("runtime", {})
        logging_config = raw_config.get("logging", {})
        sections = (monitor, rsi_alert, runtime_config, logging_config)
        if not all(isinstance(section, dict) for section in sections):
            raise RuntimeError(f"Invalid config file {path}: sections must be TOML tables")

        values.update({key: value for key, value in monitor.items() if key in values})
        values.update({key: value for key, value in rsi_alert.items() if key in values})
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
        "overbought_threshold": os.getenv("RSI_OVERBOUGHT_THRESHOLD")
        or ENV_VALUES.get("RSI_OVERBOUGHT_THRESHOLD"),
        "oversold_threshold": os.getenv("RSI_OVERSOLD_THRESHOLD") or ENV_VALUES.get("RSI_OVERSOLD_THRESHOLD"),
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
        overbought_threshold=_config_float(values, "overbought_threshold"),
        oversold_threshold=_config_float(values, "oversold_threshold"),
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
RSI_PERIOD = 14
OVERBOUGHT_THRESHOLD = CONFIG.overbought_threshold
OVERSOLD_THRESHOLD = CONFIG.oversold_threshold
CHECK_INTERVAL_SECONDS = CONFIG.check_interval_seconds
LOG_LEVEL = CONFIG.log_level


def setup_logger():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("rsi_alert_bot")
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


def disable_yfinance_cache_for_dry_run():
    """Avoid yfinance cache DB reads/writes during zero-side-effect dry-runs."""
    try:
        import yfinance.cache as yf_cache

        yf_cache._TzCacheManager._tz_cache = yf_cache._TzCacheDummy()
        yf_cache._CookieCacheManager._Cookie_cache = yf_cache._CookieCacheDummy()
        yf_cache._ISINCacheManager._isin_cache = yf_cache._ISINCacheDummy()
    except Exception as exc:
        logger.debug("Could not disable yfinance cache for dry-run: %s", exc)


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
    if RSI_PERIOD < 1:
        errors.append("period_length must be at least 1")
    if OVERBOUGHT_THRESHOLD <= OVERSOLD_THRESHOLD:
        errors.append("overbought_threshold must be greater than oversold_threshold")
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


def state_key(symbol, timeframe):
    return f"{symbol}_{timeframe}"


def fetch_candles():
    ticker = yf.Ticker(TICKER)
    history = ticker.history(
        period=HISTORY_PERIOD,
        interval=INTERVAL,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    frame = history.rename(columns=str.lower)
    columns = [column for column in ["open", "high", "low", "close", "volume"] if column in frame.columns]
    frame = frame[columns].dropna(subset=["close"])
    if "volume" not in frame.columns:
        frame["volume"] = 0.0
    logger.info("Fetched candles: symbol=%s timeframe=%s rows=%s", SYMBOL, TIMEFRAME, len(frame))
    return frame


def build_indicators(candles):
    if "close" not in candles.columns:
        raise ValueError("candles must contain a 'close' column")
    return calculate_indicators(candles[["close"]].copy())


def post_discord_embed(embed, *, dry_run=False):
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


def build_rsi_embed(event):
    detected_at = event["detected_at"]
    description = (
        "[RSI Alert]\n"
        f"{event['symbol']} {event['timeframe']}\n"
        f"RSI14: {event['rsi14']:.1f}\n"
        f"Event: {event['event_type']}\n\n"
        "Note: RSIが過熱/冷却圏に入りました、または戻りました。売買判断ではありません。"
    )
    return {
        "title": "Berial Market Watch: RSI14",
        "color": 0xE67E22,
        "description": description,
        "fields": [
            {"name": "Symbol", "value": event["symbol"], "inline": True},
            {"name": "Timeframe", "value": event["timeframe"], "inline": True},
            {"name": "Event", "value": event["event_type"], "inline": True},
            {"name": "Candle time", "value": event["candle_time"], "inline": False},
            {"name": "Previous RSI14", "value": f"{event['previous_rsi14']:.1f}", "inline": True},
            {"name": "RSI14", "value": f"{event['rsi14']:.1f}", "inline": True},
            {"name": "State", "value": event["rsi_state"], "inline": True},
            {"name": "Note", "value": "Observation only. Not a trade decision.", "inline": False},
            {"name": "Detected at", "value": detected_at, "inline": False},
        ],
        "timestamp": detected_at,
    }


def notify_rsi(event, *, dry_run=False):
    embed = build_rsi_embed(event)
    post_discord_embed(embed, dry_run=dry_run)
    logger.info(
        "RSI notified: symbol=%s timeframe=%s event=%s candle=%s rsi=%.2f",
        event["symbol"],
        event["timeframe"],
        event["event_type"],
        event["candle_time"],
        event["rsi14"],
    )


def build_rsi_event(event_type, candle_time, previous_rsi14, rsi14):
    now = datetime.now(timezone.utc).isoformat()
    return {
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "event_type": event_type,
        "candle_time": candle_time,
        "previous_rsi14": float(previous_rsi14),
        "rsi14": float(rsi14),
        "rsi_state": classify_rsi_state(rsi14),
        "notified_at": now,
        "detected_at": now,
    }


def build_state_entry(*, candle_time, rsi14, event_type=None, notified_at=None):
    return {
        "candle_time": candle_time,
        "rsi14": float(rsi14),
        "rsi_state": classify_rsi_state(rsi14),
        "last_event_type": event_type,
        "notified_at": notified_at,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def run_once(state, *, dry_run=False):
    if not ENABLED:
        logger.info("RSI Alert is disabled; skipping.")
        return

    if dry_run:
        disable_yfinance_cache_for_dry_run()

    try:
        candles = fetch_candles()
    except Exception as exc:
        logger.error("Failed to fetch candles: %s", exc)
        return

    min_bars = RSI_PERIOD + 3
    if len(candles) < min_bars:
        logger.warning("Not enough candles yet: got %s, need at least %s", len(candles), min_bars)
        return

    indicators = build_indicators(candles)
    previous_row = indicators.iloc[-3]
    current_row = indicators.iloc[-2]
    previous_rsi = previous_row["rsi14"]
    current_rsi = current_row["rsi14"]
    if pd.isna(previous_rsi) or pd.isna(current_rsi):
        logger.warning("RSI14 is not available yet for confirmed candles.")
        return

    candle_time = indicators.index[-2].isoformat()
    key = state_key(SYMBOL, TIMEFRAME)
    existing = state.get(key, {})
    if existing.get("candle_time") == candle_time:
        logger.info("RSI state already recorded for %s candle=%s; skipping.", key, candle_time)
        return

    event_type = detect_rsi_zone_event(
        previous_rsi,
        current_rsi,
        overbought_threshold=OVERBOUGHT_THRESHOLD,
        oversold_threshold=OVERSOLD_THRESHOLD,
    )
    event = None
    if event_type:
        event = build_rsi_event(event_type, candle_time, previous_rsi, current_rsi)
        notify_rsi(event, dry_run=dry_run)

    if not dry_run:
        state[key] = build_state_entry(
            candle_time=candle_time,
            rsi14=current_rsi,
            event_type=event_type,
            notified_at=event["notified_at"] if event else existing.get("notified_at"),
        )
        save_state(state)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Post RSI14 observation alerts to Discord.")
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
        "=== RSI Alert Bot v1 === symbol=%s ticker=%s timeframe=%s interval=%s period=%s rsi_period=%s overbought=%s oversold=%s check_interval=%ss dry_run=%s",
        SYMBOL,
        TICKER,
        TIMEFRAME,
        INTERVAL,
        HISTORY_PERIOD,
        RSI_PERIOD,
        OVERBOUGHT_THRESHOLD,
        OVERSOLD_THRESHOLD,
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
