"""Paper Signal generator: hypothesis-only signals from Market Bias + Chart Snapshot.

This module NEVER connects to a broker/order API (no OANDA, XM, MT5, or any
other broker integration) and NEVER implements entries, exits, lot sizing,
leverage, stop-loss, or take-profit. Every generated record is stored with
``status = "paper_only"`` and is for observation only.

See docs/architecture/berial_spec.md chapter 29 for the full v1.4.0 Paper Signal spec.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
NEWS_FEED_BOT_DIR = REPO_ROOT / "news-feed-bot"
ENV_FILE = BASE_DIR / ".env"
LOGS_DIR = REPO_ROOT / "logs"
LOG_FILE = LOGS_DIR / "paper_signal_generator.log"
DB_PATH = BASE_DIR / "data" / "paper_signals.sqlite3"

if str(NEWS_FEED_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(NEWS_FEED_BOT_DIR))
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from market_bias import classify_market_bias  # noqa: E402  (news-feed-bot)
from market_snapshot import fetch_snapshot_sources  # noqa: E402  (news-feed-bot)

import paper_signal_store as store  # noqa: E402
from signal_models import create_paper_signal, create_signal_context  # noqa: E402

REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_CONFIDENCE_THRESHOLD = 0.40
DEFAULT_DUPLICATE_WINDOW_MINUTES = 60
DEFAULT_CHECK_INTERVAL_SECONDS = 900
PAPER_SIGNAL_NOTE = "Paper signal only. No trade execution."
REPRESENTATIVE_TIMEFRAME = "1h"
FRESH = "Fresh"
BULLISH_MACD_STATE = "Bullish"
BEARISH_MACD_STATE = "Bearish"


def load_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


ENV_VALUES = load_env_file()


def _env_float(name: str, default: float) -> float:
    raw = ENV_VALUES.get(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = ENV_VALUES.get(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


DISCORD_WEBHOOK_URL = ENV_VALUES.get("DISCORD_WEBHOOK_URL", "").strip()
CONFIDENCE_THRESHOLD = _env_float("PAPER_SIGNAL_CONFIDENCE_THRESHOLD", DEFAULT_CONFIDENCE_THRESHOLD)
DUPLICATE_WINDOW_MINUTES = _env_int("PAPER_SIGNAL_DUPLICATE_WINDOW_MINUTES", DEFAULT_DUPLICATE_WINDOW_MINUTES)
CHECK_INTERVAL_SECONDS = _env_int("PAPER_SIGNAL_CHECK_INTERVAL_SECONDS", DEFAULT_CHECK_INTERVAL_SECONDS)


def setup_logger():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("paper_signal_generator")
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


def _snapshot_get(snapshot, key, default=None):
    if isinstance(snapshot, dict):
        return snapshot.get(key, default)
    return getattr(snapshot, key, default)


def build_candidates(
    bias_result: dict,
    hourly_snapshots: list,
    *,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> list[dict]:
    """Return paper-signal candidates from Market Bias + 1h Chart Snapshot data.

    Rules (docs/architecture/berial_spec.md chapter 29):
      - market_bias must be Bull or Bear (Neutral never produces a candidate)
      - confidence must be >= confidence_threshold
      - Bull requires macd_state == Bullish, freshness == Fresh, bias_weight > 0 -> side=long
      - Bear requires macd_state == Bearish, freshness == Fresh, bias_weight > 0 -> side=short
      - Weekend Closed / Market Closed / Stale freshness never produces a candidate
    """
    market_bias = bias_result.get("market_bias")
    confidence = bias_result.get("confidence", 0.0)
    candidates: list[dict] = []

    if market_bias not in {"Bull", "Bear"}:
        return candidates
    if confidence < confidence_threshold:
        return candidates

    for snapshot in hourly_snapshots:
        if _snapshot_get(snapshot, "freshness") != FRESH:
            continue
        bias_weight = _snapshot_get(snapshot, "bias_weight", 0.0) or 0.0
        if bias_weight <= 0:
            continue

        macd_state = _snapshot_get(snapshot, "macd_state")
        if market_bias == "Bull" and macd_state == BULLISH_MACD_STATE:
            side = "long"
        elif market_bias == "Bear" and macd_state == BEARISH_MACD_STATE:
            side = "short"
        else:
            continue

        symbol = _snapshot_get(snapshot, "symbol")
        trend = _snapshot_get(snapshot, "trend")
        state = _snapshot_get(snapshot, "state")
        freshness = _snapshot_get(snapshot, "freshness")
        reason = (
            f"Market Bias={market_bias} ({round(confidence * 100)}%); "
            f"{symbol} {REPRESENTATIVE_TIMEFRAME} MACD={macd_state}, Trend={trend}, "
            f"State={state}, Freshness={freshness}."
        )
        candidates.append(
            {
                "symbol": symbol,
                "timeframe": REPRESENTATIVE_TIMEFRAME,
                "side": side,
                "market_bias": market_bias,
                "confidence": confidence,
                "macd_state": macd_state,
                "trend": trend,
                "state": state,
                "freshness": freshness,
                "reason": reason,
            }
        )
    return candidates


def generate_signal_candidates(context: dict) -> list[dict]:
    """Return paper-signal candidates for *context*, with zero side effects.

    Pure reuse of build_candidates()'s existing rules over already-fetched
    Market Bias + Chart Snapshot data (see ``signal_models.SignalContext``).
    Never touches SQLite, Discord, HTTP, or paper_signal_store -- safe for
    Backtest to call directly, repeatedly, with no persistence.
    """
    return build_candidates(
        context["bias_result"],
        context["hourly_snapshots"],
        confidence_threshold=context.get("confidence_threshold", CONFIDENCE_THRESHOLD),
    )


def generate_signal(context: dict) -> dict | None:
    """Return the first candidate from generate_signal_candidates(), or None.

    Same zero-side-effect guarantee as generate_signal_candidates(): no DB,
    no Discord, no HTTP, no store calls.
    """
    candidates = generate_signal_candidates(context)
    return candidates[0] if candidates else None


def format_discord_message(signal: dict) -> str:
    lines = [
        "\U0001f4dd Paper Signal",
        "",
        f"Symbol: {signal['symbol']}",
        f"Timeframe: {signal['timeframe']}",
        f"Side: {signal['side']}",
        f"Market Bias: {signal['market_bias']} ({round(signal['confidence'] * 100)}%)",
        f"MACD State: {signal['macd_state']}",
        f"Trend: {signal['trend']}",
        f"State: {signal['state']}",
        f"Freshness: {signal['freshness']}",
        "",
        f"Reason: {signal['reason']}",
        "",
        PAPER_SIGNAL_NOTE,
    ]
    return "\n".join(lines)


def post_discord_message(message: str, *, dry_run: bool = False, session=requests) -> None:
    if dry_run:
        logger.info("[DRY_RUN] Would post Paper Signal:\n%s", message)
        return
    if not DISCORD_WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK_URL is not set; skipping Discord notification.")
        return
    response = session.post(
        DISCORD_WEBHOOK_URL,
        json={"content": message[:2000]},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def run_once(*, dry_run: bool = False, db_path: Path = DB_PATH) -> list[dict]:
    """Fetch Market Bias + 1h Chart Snapshot, then generate/store/notify new paper signals.

    Returns the list of newly created signals (empty if nothing qualified or
    every candidate was a duplicate within the dedup window).
    """
    sources = fetch_snapshot_sources()
    hourly_snapshots = sources.get("chart") or []

    bias_result = classify_market_bias(
        sources.get("market"),
        sources.get("fred"),
        sources.get("tradingview"),
        chart_data=hourly_snapshots,
    )

    context = create_signal_context(bias_result, hourly_snapshots, confidence_threshold=CONFIDENCE_THRESHOLD)
    candidates = generate_signal_candidates(context)
    created: list[dict] = []

    if not candidates:
        logger.info(
            "No paper signal candidates this run (bias=%s, confidence=%.2f).",
            bias_result.get("market_bias"),
            bias_result.get("confidence", 0.0),
        )
        return created

    for candidate in candidates:
        if not dry_run and store.has_recent_signal(
            candidate["symbol"],
            candidate["timeframe"],
            candidate["side"],
            within_minutes=DUPLICATE_WINDOW_MINUTES,
            db_path=db_path,
        ):
            logger.info(
                "Skipping duplicate paper signal: %s %s %s (within %sm)",
                candidate["symbol"], candidate["timeframe"], candidate["side"], DUPLICATE_WINDOW_MINUTES,
            )
            continue

        signal = create_paper_signal(candidate)
        if not dry_run:
            store.save_signal(signal, db_path=db_path)

        try:
            post_discord_message(format_discord_message(signal), dry_run=dry_run)
        except requests.RequestException as exc:
            logger.error("Failed to post paper signal to Discord: %s", exc)

        logger.info(
            "Paper signal generated: %s %s %s (%s)",
            signal["symbol"], signal["timeframe"], signal["side"], signal["signal_id"],
        )
        created.append(signal)

    return created


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Generate paper-only signals from Market Bias + Chart Snapshot.")
    parser.add_argument("--once", action="store_true", help="Run a single check and exit instead of looping.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print/log instead of saving to SQLite or sending to Discord.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    logger.info(
        "=== Paper Signal Generator === confidence_threshold=%.2f duplicate_window=%sm dry_run=%s",
        CONFIDENCE_THRESHOLD, DUPLICATE_WINDOW_MINUTES, args.dry_run,
    )

    while True:
        try:
            run_once(dry_run=args.dry_run)
        except Exception as exc:
            logger.error("Run failed: %s", exc)

        if args.once:
            break
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
