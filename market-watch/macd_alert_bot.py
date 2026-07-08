"""MACD cross alert bot: posts confirmed-candle MACD bullish/bearish crosses to Discord.

Data source: Yahoo Finance via yfinance (no account or API key required).
See README.md for how to swap in a broker feed (e.g. OANDA) later.
"""

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

import requests
import pandas as pd
import yfinance as yf

from chart_alert_context import prepare_alert_image
from indicators import calculate_macd, detect_macd_cross

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
CONFIG_FILE = BASE_DIR / "config.toml"
LOGS_DIR = BASE_DIR.parent / "logs"
LOG_FILE = LOGS_DIR / "macd_alert_bot.log"
STATE_DIR = BASE_DIR / "state"
STATE_FILE = STATE_DIR / "macd_alert_state.json"
LEGACY_STATE_FILE = BASE_DIR / "macd_alert_state.json"

REQUEST_TIMEOUT_SECONDS = 20

DEFAULT_CONFIG = {
    "symbol": "USD_JPY",
    "ticker": "JPY=X",
    "timeframe": "M15",
    "interval": "15m",
    "period": "5d",
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
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
    macd_fast: int
    macd_slow: int
    macd_signal: int
    check_interval_seconds: int
    log_level: str


def _config_int(values, key):
    try:
        return int(values[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid config value for {key}: {values.get(key)!r}") from exc


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
        macd = raw_config.get("macd", {})
        runtime_config = raw_config.get("runtime", {})
        logging_config = raw_config.get("logging", {})
        if not all(isinstance(section, dict) for section in (monitor, macd, runtime_config, logging_config)):
            raise RuntimeError(f"Invalid config file {path}: sections must be TOML tables")

        values.update({key: value for key, value in monitor.items() if key in values})
        values.update({key: value for key, value in macd.items() if key in values})
        values["check_interval_seconds"] = int(
            os.getenv(
                "CHECK_INTERVAL_SECONDS",
                runtime_config.get("check_interval_seconds", DEFAULT_CONFIG["check_interval_seconds"]),
            )
        )
        values.update({key: value for key, value in logging_config.items() if key in values})

    # Backward-compatible environment overrides for old .env files. Secrets
    # still belong in .env; public monitoring settings should live in config.toml.
    env_overrides = {
        "symbol": os.getenv("SYMBOL") or ENV_VALUES.get("SYMBOL"),
        "timeframe": os.getenv("TIMEFRAME") or ENV_VALUES.get("TIMEFRAME"),
        "macd_fast": os.getenv("MACD_FAST") or ENV_VALUES.get("MACD_FAST"),
        "macd_slow": os.getenv("MACD_SLOW") or ENV_VALUES.get("MACD_SLOW"),
        "macd_signal": os.getenv("MACD_SIGNAL") or ENV_VALUES.get("MACD_SIGNAL"),
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
        macd_fast=_config_int(values, "macd_fast"),
        macd_slow=_config_int(values, "macd_slow"),
        macd_signal=_config_int(values, "macd_signal"),
        check_interval_seconds=_config_int(values, "check_interval_seconds"),
        log_level=str(values["log_level"]).strip().upper(),
    )


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


ENV_VALUES = load_env_file()
CONFIG = load_config()

DISCORD_WEBHOOK_URL = ENV_VALUES.get("BOT_ALERTS_WEBHOOK_URL", ENV_VALUES.get("DISCORD_WEBHOOK_URL", "")).strip()
SYMBOL = CONFIG.symbol
TICKER = CONFIG.ticker
TIMEFRAME = CONFIG.timeframe
INTERVAL = CONFIG.interval
HISTORY_PERIOD = CONFIG.period
MACD_FAST = CONFIG.macd_fast
MACD_SLOW = CONFIG.macd_slow
MACD_SIGNAL = CONFIG.macd_signal
CHECK_INTERVAL_SECONDS = CONFIG.check_interval_seconds
LOG_LEVEL = CONFIG.log_level


def setup_logger():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("macd_alert_bot")
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
        errors.append("DISCORD_WEBHOOK_URL is not set in .env")
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
    if MACD_SLOW <= MACD_FAST:
        errors.append("MACD_SLOW must be greater than MACD_FAST")
    if CHECK_INTERVAL_SECONDS < 1:
        errors.append("CHECK_INTERVAL_SECONDS must be at least 1")
    if errors:
        raise RuntimeError("; ".join(errors))


def load_state():
    path = STATE_FILE if STATE_FILE.exists() else LEGACY_STATE_FILE
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as state_file:
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


def state_key():
    return f"{SYMBOL}_{TIMEFRAME}"


def fetch_candles():
    """Return the recent Close-price series for the configured symbol/timeframe."""
    ticker = yf.Ticker(TICKER)
    history = ticker.history(
        period=HISTORY_PERIOD,
        interval=INTERVAL,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    close = history["Close"].dropna()
    logger.info("Fetched candles: symbol=%s timeframe=%s rows=%s", SYMBOL, TIMEFRAME, len(close))
    return close


def compute_macd(close, *, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    macd_line, signal_line, _hist = calculate_macd(close, fast=fast, slow=slow, signal=signal)
    return macd_line, signal_line


def detect_cross(prev_macd, prev_signal, curr_macd, curr_signal):
    return detect_macd_cross(prev_macd, prev_signal, curr_macd, curr_signal)


def post_discord_embed(embed, *, dry_run=False, image_bytes=None, image_filename=None):
    if dry_run:
        logger.info("[DRY_RUN] Would post to Discord: %s", json.dumps(embed, ensure_ascii=False))
        if image_filename:
            logger.info("[DRY_RUN] Would attach chart image: %s", image_filename)
        return

    try:
        if image_bytes and image_filename:
            response = requests.post(
                DISCORD_WEBHOOK_URL,
                data={"payload_json": json.dumps({"embeds": [embed]}, ensure_ascii=False)},
                files={"files[0]": (image_filename, image_bytes, "image/png")},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        else:
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


def notify_cross(cross, candle_time, close_price, macd_value, signal_value, *, dry_run=False, candles=None):
    is_bullish = cross == "bullish"
    signal_name = "golden cross" if is_bullish else "dead cross"
    detected_at = datetime.now(timezone.utc).isoformat()
    embed = {
        "title": f"Berial Market Watch: MACD {signal_name.title()}",
        "color": 0x2ECC71 if is_bullish else 0xE74C3C,
        "description": (
            f"The watchtower saw a {signal_name} on the confirmed {TIMEFRAME} candle. "
            "This is a signal alert only, not a trade decision."
        ),
        "fields": [
            {"name": "Symbol", "value": SYMBOL, "inline": True},
            {"name": "Timeframe", "value": TIMEFRAME, "inline": True},
            {"name": "Signal", "value": f"{cross} / {signal_name}", "inline": True},
            {"name": "Candle", "value": str(candle_time), "inline": False},
            {"name": "Close", "value": f"{close_price:.3f}", "inline": True},
            {"name": "MACD", "value": f"{macd_value:.5f}", "inline": True},
            {"name": "Signal line", "value": f"{signal_value:.5f}", "inline": True},
            {"name": "Detected at", "value": detected_at, "inline": False},
        ],
        "timestamp": detected_at,
    }
    image_bytes = None
    image_filename = None
    try:
        _snapshot_id, image_bytes, image_filename = prepare_alert_image(
            alert_type=f"macd_{cross}",
            symbol=SYMBOL,
            timeframe=TIMEFRAME,
            candle_time=str(candle_time),
            candles=candles if candles is not None else pd.Series([close_price]),
            metadata={
                "cross": cross,
                "close": float(close_price),
                "macd": float(macd_value),
                "signal_line": float(signal_value),
                "detected_at": detected_at,
            },
            dry_run=dry_run,
        )
    except ModuleNotFoundError as exc:
        logger.warning("Chart image attachment skipped because dependency is missing: %s", exc)
    if image_bytes and image_filename:
        post_discord_embed(embed, dry_run=dry_run, image_bytes=image_bytes, image_filename=image_filename)
    else:
        post_discord_embed(embed, dry_run=dry_run)
    logger.info("%s MACD %s for %s %s at %s", cross, signal_name, SYMBOL, TIMEFRAME, candle_time)


def notify_error(exc, *, dry_run=False):
    if not DISCORD_WEBHOOK_URL and not dry_run:
        return
    try:
        embed = {
            "title": "MACD Alert Bot Error",
            "color": 0xE74C3C,
            "description": str(exc)[:4000],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        post_discord_embed(embed, dry_run=dry_run)
    except requests.RequestException as post_exc:
        logger.error("Failed to post error notification to Discord: %s", post_exc)


def run_once(state, *, dry_run=False):
    try:
        close = fetch_candles()
    except Exception as exc:
        logger.error("Failed to fetch candles: %s", exc)
        return
    min_bars = MACD_SLOW + MACD_SIGNAL + 2
    if len(close) < min_bars:
        logger.warning("Not enough candles yet: got %s, need %s", len(close), min_bars)
        return

    macd_line, signal_line = compute_macd(close)

    # The most recently returned bar may still be forming (unconfirmed), so
    # crosses are only evaluated between the two most recent CLOSED candles.
    confirmed_idx = -2
    previous_idx = -3

    prev_macd = macd_line.iloc[previous_idx]
    prev_signal = signal_line.iloc[previous_idx]
    curr_macd = macd_line.iloc[confirmed_idx]
    curr_signal = signal_line.iloc[confirmed_idx]
    candle_time = close.index[confirmed_idx]
    close_price = close.iloc[confirmed_idx]

    cross = detect_cross(prev_macd, prev_signal, curr_macd, curr_signal)
    if not cross:
        return

    candle_key = candle_time.isoformat()
    existing_state = state.get(state_key())
    if isinstance(existing_state, dict):
        already_notified = (
            existing_state.get("candle_time") == candle_key
            and existing_state.get("signal") == cross
        )
    else:
        already_notified = existing_state == candle_key

    if already_notified:
        logger.info("Cross already notified for this candle (%s, %s); skipping.", candle_key, cross)
        return

    notify_cross(cross, candle_time, close_price, curr_macd, curr_signal, dry_run=dry_run, candles=close)
    state[state_key()] = {
        "candle_time": candle_key,
        "signal": cross,
        "price": float(close_price),
        "macd": float(curr_macd),
        "signal_line": float(curr_signal),
        "notified_at": datetime.now(timezone.utc).isoformat(),
    }
    if not dry_run:
        save_state(state)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Post MACD cross alerts to Discord.")
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
        "=== MACD Cross Alert Bot v1.2.2 === symbol=%s ticker=%s timeframe=%s interval=%s period=%s fast=%s slow=%s signal=%s check_interval=%ss dry_run=%s",
        SYMBOL, TICKER, TIMEFRAME, INTERVAL, HISTORY_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIGNAL, CHECK_INTERVAL_SECONDS, args.dry_run,
    )

    state = load_state()

    while True:
        try:
            run_once(state, dry_run=args.dry_run)
        except Exception as exc:
            logger.error("Run failed: %s", exc)
            notify_error(exc, dry_run=args.dry_run)

        if args.once:
            break
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
