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
from market_bias_models import (  # noqa: E402  (news-feed-bot)
    GLOBAL_SCOPE_SYMBOL,
    effective_bias_scopes,
    normalize_bias_symbol,
)
from market_snapshot import fetch_snapshot_sources  # noqa: E402  (news-feed-bot)

import paper_signal_store as store  # noqa: E402
from signal_models import (  # noqa: E402
    ALERT_SOURCE_COMPOSITE,
    SIGNAL_TYPE_TREND_FOLLOW,
    STRATEGY_PROFILE_MARKET_BIAS_MACD_ALIGNMENT_V1,
    TRIGGER_TYPE_MACD_STATE_ALIGNMENT,
    create_paper_signal,
    create_signal_context,
)

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


# Issue #31: `import signal_generator` must never create logs/ or initialize a
# file handler as a side effect. setup_logger() is therefore only ever called
# lazily, on first actual log use, via _get_logger() -- never at import time.
_logger_instance: logging.Logger | None = None


def _get_logger() -> logging.Logger:
    """Return the module logger, initializing it on first use only.

    Cached in _logger_instance so repeated calls reuse the same logger/handlers
    instead of re-running setup_logger() (and its handler-dedup check) on every
    log statement.
    """
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = setup_logger()
    return _logger_instance


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
                # Issue #27: this candidate is produced by Market Bias / MACD
                # *state* alignment, not by detecting a live MACD cross event
                # -- so trigger_type is macd_state_alignment, never macd_cross.
                "signal_type": SIGNAL_TYPE_TREND_FOLLOW,
                "trigger_type": TRIGGER_TYPE_MACD_STATE_ALIGNMENT,
                "alert_source": ALERT_SOURCE_COMPOSITE,
                "strategy_profile": STRATEGY_PROFILE_MARKET_BIAS_MACD_ALIGNMENT_V1,
            }
        )
    return candidates


def resolve_scope_bias(bias_by_scope: dict, symbol, timeframe) -> dict | None:
    """Return the Market Bias for one snapshot's ``symbol``/``timeframe``, or None.

    Issue #40: walks the same reference priority as the store's effective lookup
    (symbol+timeframe, symbol+1h, GLOBAL+timeframe, GLOBAL+1h) over a
    ``{(normalized_symbol, timeframe): bias_result}`` map, so a symbol-specific
    bias wins and GLOBAL is the fallback. Pure dict lookups only -- no DB, no
    network. Returns None when no scope in the map applies (the snapshot then
    yields no candidate).
    """
    for scope in effective_bias_scopes(symbol, timeframe):
        bias = bias_by_scope.get(scope)
        if bias is not None:
            return bias
    return None


def build_candidates_by_scope(
    bias_by_scope: dict,
    hourly_snapshots: list,
    *,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> list[dict]:
    """Per-symbol variant of build_candidates(): each snapshot uses its own bias.

    Issue #40: instead of one Market Bias applied to every snapshot, resolve each
    snapshot's bias by its symbol/timeframe (GLOBAL fallback) via
    :func:`resolve_scope_bias`, then reuse the unchanged ``build_candidates``
    rules for that single snapshot. So a Bull bias on USD_JPY never forces a
    candidate on XAU_USD, and a symbol with a Neutral bias yields nothing even
    when another symbol is Bull. Pure and side-effect-free, exactly like
    ``build_candidates`` -- the resolved biases must already be in
    ``bias_by_scope`` (no lookup happens here).
    """
    candidates: list[dict] = []
    for snapshot in hourly_snapshots:
        symbol = _snapshot_get(snapshot, "symbol")
        timeframe = _snapshot_get(snapshot, "timeframe", REPRESENTATIVE_TIMEFRAME)
        bias = resolve_scope_bias(bias_by_scope, symbol, timeframe)
        if bias is None:
            continue
        candidates.extend(
            build_candidates(bias, [snapshot], confidence_threshold=confidence_threshold)
        )
    return candidates


def generate_signal_candidates(context: dict) -> list[dict]:
    """Return paper-signal candidates for *context*, with zero side effects.

    Pure reuse of build_candidates()'s existing rules over already-fetched
    Market Bias + Chart Snapshot data (see ``signal_models.SignalContext``).
    Never touches SQLite, Discord, HTTP, or paper_signal_store -- safe for
    Backtest to call directly, repeatedly, with no persistence.

    Issue #40: when the context carries a ``bias_by_scope`` map, each snapshot is
    matched to its own symbol/timeframe bias (GLOBAL fallback); otherwise the
    single ``bias_result`` is applied to every snapshot, unchanged. Both paths
    stay pure -- the biases are always pre-resolved by the caller.
    """
    confidence_threshold = context.get("confidence_threshold", CONFIDENCE_THRESHOLD)
    bias_by_scope = context.get("bias_by_scope")
    if bias_by_scope:
        return build_candidates_by_scope(
            bias_by_scope,
            context["hourly_snapshots"],
            confidence_threshold=confidence_threshold,
        )
    return build_candidates(
        context["bias_result"],
        context["hourly_snapshots"],
        confidence_threshold=confidence_threshold,
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
        # Issue #27: surface the strategic classification (Strategy/Method
        # layer). alert_source is intentionally omitted here to keep the
        # message compact -- it is still persisted in SQLite. .get() keeps
        # this safe for any legacy signal dict lacking the fields.
        f"Signal Type: {signal.get('signal_type', SIGNAL_TYPE_TREND_FOLLOW)}",
        f"Trigger Type: {signal.get('trigger_type', TRIGGER_TYPE_MACD_STATE_ALIGNMENT)}",
        f"Strategy Profile: {signal.get('strategy_profile', STRATEGY_PROFILE_MARKET_BIAS_MACD_ALIGNMENT_V1)}",
        "",
        f"Reason: {signal['reason']}",
        "",
        PAPER_SIGNAL_NOTE,
    ]
    return "\n".join(lines)


def post_discord_message(message: str, *, dry_run: bool = False, session=requests) -> None:
    if dry_run:
        _get_logger().info("[DRY_RUN] Would post Paper Signal:\n%s", message)
        return
    if not DISCORD_WEBHOOK_URL:
        _get_logger().warning("DISCORD_WEBHOOK_URL is not set; skipping Discord notification.")
        return
    response = session.post(
        DISCORD_WEBHOOK_URL,
        json={"content": message[:2000]},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def build_bias_by_scope(
    sources: dict,
    hourly_snapshots: list,
    global_bias: dict,
) -> dict:
    """Compute the per-symbol Market Bias map for one live Paper Signal run.

    Issue #40: keyed by ``(normalized_symbol, 1h)`` for each charted instrument,
    plus the ``(GLOBAL, 1h)`` fallback scope holding ``global_bias``. Each
    per-symbol bias reuses the shared market/fred/tradingview inputs but sees
    only its own chart trend/MACD (the snapshots are filtered down to that symbol
    before classification), so one symbol's chart never biases another's
    direction. This mirrors what Market Snapshot persists to the DB, but is
    computed in-memory here (no DB lookup) so ``generate_signal_candidates`` stays
    pure. The same module-level ``classify_market_bias`` used for the GLOBAL bias
    is reused per symbol, keeping both scopes consistent.
    """
    market_data = sources.get("market")
    fred_data = sources.get("fred")
    tv_data = sources.get("tradingview")

    bias_by_scope: dict = {(GLOBAL_SCOPE_SYMBOL, REPRESENTATIVE_TIMEFRAME): global_bias}
    seen: set[str] = set()
    for snapshot in hourly_snapshots:
        raw_symbol = _snapshot_get(snapshot, "symbol")
        if not raw_symbol:
            continue
        canonical = normalize_bias_symbol(raw_symbol)
        if canonical in seen:
            continue
        seen.add(canonical)
        own_snapshots = [
            snap for snap in hourly_snapshots if normalize_bias_symbol(_snapshot_get(snap, "symbol")) == canonical
        ]
        bias_by_scope[(canonical, REPRESENTATIVE_TIMEFRAME)] = classify_market_bias(
            market_data,
            fred_data,
            tv_data,
            chart_data=own_snapshots,
        )
    return bias_by_scope


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

    # Issue #40: resolve a per-symbol bias per snapshot (GLOBAL fallback). The DB
    # is never consulted here -- biases are computed up front and handed to the
    # pure candidate functions via the context's bias_by_scope map.
    bias_by_scope = build_bias_by_scope(sources, hourly_snapshots, bias_result)
    context = create_signal_context(
        bias_result,
        hourly_snapshots,
        confidence_threshold=CONFIDENCE_THRESHOLD,
        bias_by_scope=bias_by_scope,
    )
    candidates = generate_signal_candidates(context)
    created: list[dict] = []

    if not candidates:
        _get_logger().info(
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
            _get_logger().info(
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
            _get_logger().error("Failed to post paper signal to Discord: %s", exc)

        _get_logger().info(
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
    _get_logger().info(
        "=== Paper Signal Generator === confidence_threshold=%.2f duplicate_window=%sm dry_run=%s",
        CONFIDENCE_THRESHOLD, DUPLICATE_WINDOW_MINUTES, args.dry_run,
    )

    while True:
        try:
            run_once(dry_run=args.dry_run)
        except Exception as exc:
            _get_logger().error("Run failed: %s", exc)

        if args.once:
            break
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
