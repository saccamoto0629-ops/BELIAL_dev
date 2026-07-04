"""MACD cross alert bot: posts confirmed-candle MACD bullish/bearish crosses to Discord.

Data source: Yahoo Finance via yfinance (no account or API key required).
See README.md for how to swap in a broker feed (e.g. OANDA) later.
"""

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
import yfinance as yf

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
LOGS_DIR = BASE_DIR.parent / "logs"
LOG_FILE = LOGS_DIR / "macd_alert_bot.log"
STATE_FILE = BASE_DIR / "macd_alert_state.json"

REQUEST_TIMEOUT_SECONDS = 20

# SYMBOL -> yfinance ticker. Add more pairs here as they are supported.
SYMBOL_TICKERS = {
    "USD_JPY": "JPY=X",
}

# TIMEFRAME -> yfinance interval / history window.
# 5d of 15m bars is well within Yahoo's ~60 day intraday limit and gives
# enough bars for the MACD(12,26,9) EMAs to be stable.
TIMEFRAME_SETTINGS = {
    "M15": {"interval": "15m", "period": "5d"},
}


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

DISCORD_WEBHOOK_URL = ENV_VALUES.get("DISCORD_WEBHOOK_URL", "").strip()
SYMBOL = ENV_VALUES.get("SYMBOL", "USD_JPY").strip()
TIMEFRAME = ENV_VALUES.get("TIMEFRAME", "M15").strip()
MACD_FAST = int(ENV_VALUES.get("MACD_FAST", "12"))
MACD_SLOW = int(ENV_VALUES.get("MACD_SLOW", "26"))
MACD_SIGNAL = int(ENV_VALUES.get("MACD_SIGNAL", "9"))
CHECK_INTERVAL_SECONDS = int(ENV_VALUES.get("CHECK_INTERVAL_SECONDS", "60"))


def setup_logger():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("macd_alert_bot")
    logger.setLevel(logging.INFO)
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
    if SYMBOL not in SYMBOL_TICKERS:
        errors.append(f"Unsupported SYMBOL: {SYMBOL} (supported: {list(SYMBOL_TICKERS)})")
    if TIMEFRAME not in TIMEFRAME_SETTINGS:
        errors.append(f"Unsupported TIMEFRAME: {TIMEFRAME} (supported: {list(TIMEFRAME_SETTINGS)})")
    if MACD_SLOW <= MACD_FAST:
        errors.append("MACD_SLOW must be greater than MACD_FAST")
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
    temp_path = STATE_FILE.with_suffix(".json.tmp")
    with temp_path.open("w", encoding="utf-8") as state_file:
        json.dump(state, state_file, ensure_ascii=False, indent=2)
    os.replace(temp_path, STATE_FILE)


def state_key():
    return f"{SYMBOL}_{TIMEFRAME}"


def fetch_candles():
    """Return the recent Close-price series for the configured symbol/timeframe."""
    ticker_symbol = SYMBOL_TICKERS[SYMBOL]
    settings = TIMEFRAME_SETTINGS[TIMEFRAME]
    ticker = yf.Ticker(ticker_symbol)
    history = ticker.history(
        period=settings["period"],
        interval=settings["interval"],
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    return history["Close"].dropna()


def compute_macd(close, *, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    macd_line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


def detect_cross(prev_macd, prev_signal, curr_macd, curr_signal):
    prev_diff = prev_macd - prev_signal
    curr_diff = curr_macd - curr_signal
    if prev_diff <= 0 and curr_diff > 0:
        return "bullish"
    if prev_diff >= 0 and curr_diff < 0:
        return "bearish"
    return None


def post_discord_embed(embed, *, dry_run=False):
    if dry_run:
        logger.info("[DRY_RUN] Would post to Discord: %s", json.dumps(embed, ensure_ascii=False))
        return

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={"embeds": [embed]},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def notify_cross(cross, candle_time, close_price, macd_value, signal_value, *, dry_run=False):
    is_bullish = cross == "bullish"
    embed = {
        "title": f"MACD {'Bullish' if is_bullish else 'Bearish'} Cross",
        "color": 0x2ECC71 if is_bullish else 0xE74C3C,
        "fields": [
            {"name": "Symbol", "value": SYMBOL, "inline": True},
            {"name": "Timeframe", "value": TIMEFRAME, "inline": True},
            {"name": "Candle", "value": str(candle_time), "inline": False},
            {"name": "Close", "value": f"{close_price:.3f}", "inline": True},
            {"name": "MACD", "value": f"{macd_value:.5f}", "inline": True},
            {"name": "Signal", "value": f"{signal_value:.5f}", "inline": True},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    post_discord_embed(embed, dry_run=dry_run)
    logger.info("%s cross for %s %s at %s", cross, SYMBOL, TIMEFRAME, candle_time)


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
    close = fetch_candles()
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
    if state.get(state_key()) == candle_key:
        logger.info("Cross already notified for this candle (%s); skipping.", candle_key)
        return

    notify_cross(cross, candle_time, close_price, curr_macd, curr_signal, dry_run=dry_run)
    state[state_key()] = candle_key
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
        "=== MACD Cross Alert Bot === symbol=%s timeframe=%s fast=%s slow=%s signal=%s interval=%ss dry_run=%s",
        SYMBOL, TIMEFRAME, MACD_FAST, MACD_SLOW, MACD_SIGNAL, CHECK_INTERVAL_SECONDS, args.dry_run,
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
