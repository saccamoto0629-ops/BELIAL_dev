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
import signal
import sys
import threading
import unicodedata
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

import requests

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
NEWS_FEED_BOT_DIR = REPO_ROOT / "news-feed-bot"
MARKET_WATCH_DIR = REPO_ROOT / "market-watch"
ENV_FILE = BASE_DIR / ".env"
LOGS_DIR = REPO_ROOT / "logs"
LOG_FILE = LOGS_DIR / "paper_signal_generator.log"
DB_PATH = BASE_DIR / "data" / "paper_signals.sqlite3"

if str(NEWS_FEED_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(NEWS_FEED_BOT_DIR))
if str(MARKET_WATCH_DIR) not in sys.path:
    sys.path.insert(0, str(MARKET_WATCH_DIR))
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from market_bias import classify_market_bias  # noqa: E402  (news-feed-bot)
from market_bias_models import (  # noqa: E402  (news-feed-bot)
    GLOBAL_SCOPE_SYMBOL,
    effective_bias_scopes,
    normalize_bias_symbol,
)
from market_snapshot import fetch_snapshot_sources  # noqa: E402  (news-feed-bot)

# Issue #41: reuse the single shared confirmed-candle rule (Issue #28) so the
# resident service determines the same "latest closed candle" that Live Chart
# Snapshot and Backtest do -- never re-implementing candle timing here.
from freshness import (  # noqa: E402  (market-watch, dependency-light)
    JST,
    candle_close_time,
    parse_timestamp_utc,
)

# Issue #55: attach the matching EXISTING Chart Snapshot image to the Paper
# Signal notification. Snapshot selection and relation persistence live in the
# dependency-light chart_snapshot_store; S3 presigned URLs reuse the same
# chart_image_uploader path Chart Snapshot notifications already use. Nothing
# here renders charts or uploads to S3.
from chart_collector import chart_db_path, load_env_file as load_chart_env_file  # noqa: E402  (market-watch)
from chart_image_uploader import generate_presigned_chart_url, load_s3_config  # noqa: E402  (market-watch)
from chart_snapshot_store import (  # noqa: E402  (market-watch, dependency-light)
    find_snapshot_for_candle,
    link_paper_signal_chart_snapshot,
)

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


# Issue #41 review (Major 3): a --dry-run must not create logs/ or open the
# rotating logfile. When this is True, setup_logger() attaches a console handler
# only. configure_logging() sets it before the first log use (main() does this);
# it defaults to False so normal runs keep their file log unchanged.
_console_only_logging = False


def configure_logging(*, dry_run: bool) -> None:
    """Select console-only logging for a dry run (no ``logs/`` dir, no logfile).

    Must be called before the first :func:`_get_logger` use. Idempotent before
    initialization; a no-op afterwards (the cached logger's handlers are fixed).
    """
    global _console_only_logging, _logger_instance
    if _logger_instance is not None and _console_only_logging != dry_run:
        for handler in list(_logger_instance.handlers):
            _logger_instance.removeHandler(handler)
            handler.close()
        _logger_instance = None
    _console_only_logging = dry_run


def setup_logger():
    logger = logging.getLogger("paper_signal_generator")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # Dry-run: console handler only -- never create logs/ or open a file handler.
    if not _console_only_logging:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
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


# --- Issue #41: confirmed-candle discipline + input integrity ----------------
#
# Skip-reason codes surfaced to the log so every non-generating cycle is
# explainable (never a silent drop). These are *normal* outcomes, not errors.
SKIP_CHART_SNAPSHOT_MISSING = "chart_snapshot_missing"
SKIP_MARKET_BIAS_MISSING = "market_bias_missing"
SKIP_SYMBOL_TIMEFRAME_MISMATCH = "symbol_timeframe_mismatch"
SKIP_TARGET_CANDLE_MISMATCH = "target_candle_timestamp_mismatch"
SKIP_PROVENANCE_MISMATCH = "provenance_mismatch"
SKIP_UNCONFIRMED_CANDLE = "unconfirmed_candle"
RESULT_NO_SIGNAL = "no_signal"
RESULT_SIGNAL = "signal"


def canonical_candle_timestamp(value) -> str | None:
    """Return a candle timestamp as canonical UTC ISO-8601 (``...Z``), or None.

    Normalizes whatever form the Chart Snapshot carried its confirmed-candle
    timestamp in (e.g. ``2026-07-05T12:00:00+00:00``) to the same ``...Z`` form
    the rest of Berial stores (see ``chart_collector.to_utc_iso_z``), so the
    idempotency key and Discord display are byte-stable. Returns None for a
    missing/blank value so the caller can treat it as an unusable snapshot.
    """
    if value in (None, ""):
        return None
    try:
        return parse_timestamp_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None


def is_candle_confirmed(target_candle_utc: str, timeframe: str, evaluation_time: datetime) -> bool:
    """Return True if the candle opening at *target_candle_utc* has fully closed.

    Issue #41: a candle labelled with its open time ``T`` on *timeframe* closes
    at ``T + timeframe``; only then may it drive a signal. Uses the shared
    ``freshness.candle_close_time`` rule so Paper Signal never acts on a still
    forming candle. Unknown timeframe labels cannot be timed, so this check fails
    closed and treats them as unconfirmed.
    """
    try:
        close_time = candle_close_time(target_candle_utc, timeframe)
    except ValueError:
        return False
    return close_time <= parse_timestamp_utc(evaluation_time)


def _snapshot_source_id(snapshot) -> str | None:
    """Best-effort provenance id of a chart snapshot (may be None for live ones)."""
    for key in ("source_snapshot_id", "id", "snapshot_id", "chart_snapshot_id"):
        value = _snapshot_get(snapshot, key)
        if value:
            return str(value)
    return None


def build_confirmed_candidate(
    chart_snapshot,
    market_bias,
    bias_provenance,
    *,
    evaluation_time: datetime,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> tuple[dict | None, str]:
    """Return ``(candidate, result_code)`` for one Chart Snapshot + Market Bias pair.

    Issue #41 input-integrity + confirmed-candle contract, as a pure function
    (no DB / Discord / HTTP).

    Major 2 (review): the Market Bias's provenance -- the symbol / timeframe /
    target candle / source id of the data it was ACTUALLY classified from -- is
    supplied separately in ``bias_provenance``, captured at bias-generation time
    (see :func:`build_bias_provenance_by_scope`). It is compared against the Chart
    Snapshot's own symbol / timeframe / candle / source id. The chart snapshot's
    timestamp is never copied onto the bias to force agreement: a bias whose
    recorded provenance differs from the chart snapshot is a *skip*, so combining
    a bias and a snapshot from different symbols, timeframes, candles, or source
    datasets can never silently produce a signal.

    Order of checks (each a normal, logged skip -- not an error):

    1. Chart Snapshot missing / no candle timestamp -> ``chart_snapshot_missing``
    2. Market Bias or its provenance missing / no provenance candle -> ``market_bias_missing``
    3. provenance symbol or timeframe disagree -> ``symbol_timeframe_mismatch``
    4. provenance target candle disagrees -> ``target_candle_timestamp_mismatch``
    5. provenance source id disagrees -> ``provenance_mismatch``
    6. the candle has not closed yet at ``evaluation_time`` -> ``unconfirmed_candle``

    Only when all pass are the unchanged strategy rules (:func:`build_candidates`)
    applied. On a generated candidate the resolved ``target_candle_utc`` is
    attached and ``(candidate, "signal")`` returned; when the rules produce
    nothing, ``(None, "no_signal")``.
    """
    if chart_snapshot is None:
        return None, SKIP_CHART_SNAPSHOT_MISSING
    target_candle = canonical_candle_timestamp(_snapshot_get(chart_snapshot, "timestamp"))
    if target_candle is None:
        return None, SKIP_CHART_SNAPSHOT_MISSING
    if market_bias is None or bias_provenance is None:
        return None, SKIP_MARKET_BIAS_MISSING

    symbol = _snapshot_get(chart_snapshot, "symbol")
    timeframe = _snapshot_get(chart_snapshot, "timeframe", REPRESENTATIVE_TIMEFRAME)

    # Provenance recorded when the bias was classified -- NOT copied from this
    # chart snapshot. A bias without a recorded input candle cannot be verified.
    prov_symbol = bias_provenance.get("symbol")
    prov_timeframe = bias_provenance.get("timeframe")
    prov_candle = canonical_candle_timestamp(bias_provenance.get("target_candle_utc"))
    if prov_candle is None:
        return None, SKIP_MARKET_BIAS_MISSING

    if prov_symbol is None or normalize_bias_symbol(prov_symbol) != normalize_bias_symbol(symbol):
        return None, SKIP_SYMBOL_TIMEFRAME_MISMATCH
    if prov_timeframe is None or prov_timeframe != timeframe:
        return None, SKIP_SYMBOL_TIMEFRAME_MISMATCH
    if prov_candle != target_candle:
        return None, SKIP_TARGET_CANDLE_MISMATCH

    # If both sides carry a source-dataset id, they must identify the same input.
    chart_source_id = _snapshot_source_id(chart_snapshot)
    prov_source_id = bias_provenance.get("source_snapshot_id")
    if chart_source_id is not None and prov_source_id is not None and str(prov_source_id) != chart_source_id:
        return None, SKIP_PROVENANCE_MISMATCH

    if not is_candle_confirmed(target_candle, timeframe, evaluation_time):
        return None, SKIP_UNCONFIRMED_CANDLE

    candidates = build_candidates(market_bias, [chart_snapshot], confidence_threshold=confidence_threshold)
    if not candidates:
        return None, RESULT_NO_SIGNAL
    candidate = candidates[0]
    # Descriptive provenance for Issue #47 reporting. It is attached only at
    # the resident-service persistence boundary and never changes eligibility.
    candidate["market_state"] = _snapshot_get(chart_snapshot, "market_state")
    candidate["target_candle_utc"] = target_candle
    return candidate, RESULT_SIGNAL


def format_target_candle_display(target_candle_utc: str) -> str:
    """Render a target candle timestamp as ``<UTC-Z> (<JST> JST)``.

    Issue #41: makes the UTC<->JST relationship explicit and unambiguous in the
    Discord message. The stored/idempotency value is always the UTC ``...Z`` form;
    the JST rendering is display-only and derived from it, never a second source
    of truth.
    """
    utc_ts = parse_timestamp_utc(target_candle_utc)
    jst_ts = utc_ts.tz_convert(JST)
    return f"{utc_ts.strftime('%Y-%m-%dT%H:%M:%SZ')} ({jst_ts.strftime('%Y-%m-%d %H:%M')} JST)"


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
    ]
    # Issue #41: the confirmed candle this signal is for (present on resident
    # service signals; absent on ad-hoc/legacy candidates, which simply omit it).
    target_candle = signal.get("target_candle_utc")
    if target_candle:
        lines.append(f"Target Candle: {format_target_candle_display(target_candle)}")
    lines.extend(
        [
            "",
            f"Reason: {signal['reason']}",
            "",
            PAPER_SIGNAL_NOTE,
        ]
    )
    return "\n".join(lines)


def _valid_http_url(value) -> str | None:
    """Return the stripped URL when *value* is a well-formed http(s) URL, else None.

    A blank or malformed embed URL would make Discord reject the WHOLE webhook
    post with a 400 -- taking the text body down with it -- so anything that is
    not clearly ``http(s)://host/...`` must drop the embed, not the message.
    Never raises: ``urlparse``/``hostname`` themselves reject some malformed
    inputs (e.g. ``http://[``) with ValueError, which counts as invalid here.
    A valid URL is returned exactly as given (minus outer whitespace) -- query
    strings, percent-encoding, and ports are part of a presigned URL's
    signature and must not be altered.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    # Unicode category Cc covers ALL control characters -- C0 (\x00-\x1f, incl.
    # tab/newline), DEL (\x7f), and the C1 range (\x80-\x9f, e.g. U+0085 NEL) that a
    # raw ordinal check below 0x20 would miss.
    if any(unicodedata.category(char) == "Cc" for char in candidate):
        return None
    try:
        parsed = urlparse(candidate)
        hostname = parsed.hostname
    except (ValueError, TypeError):
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    if not hostname:
        return None
    if any(char.isspace() for char in hostname):
        return None
    return candidate


def build_discord_payload(message: str, image_url: str | None = None) -> dict:
    """Return the webhook payload: text always, plus an image embed when given.

    Issue #55: the body format is unchanged; a matching Chart Snapshot image is
    carried in the same single webhook post using the same embed shape as the
    Chart Snapshot notification (``chart_snapshot.post_discord_message``). The
    embed is added only for a well-formed http(s) URL (see
    :func:`_valid_http_url`) so a bad URL can never cost the text body.
    """
    payload: dict = {"content": message[:2000]}
    valid_url = _valid_http_url(image_url)
    if valid_url:
        payload["embeds"] = [{"image": {"url": valid_url}}]
    return payload


def post_discord_message(
    message: str,
    *,
    image_url: str | None = None,
    dry_run: bool = False,
    session=requests,
) -> None:
    if dry_run:
        _get_logger().info("[DRY_RUN] Would post Paper Signal:\n%s", message)
        return
    if not DISCORD_WEBHOOK_URL:
        _get_logger().warning("DISCORD_WEBHOOK_URL is not set; skipping Discord notification.")
        return
    response = session.post(
        DISCORD_WEBHOOK_URL,
        json=build_discord_payload(message, image_url),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


# --- Issue #55: attach the matching existing Chart Snapshot image -------------


def resolve_signal_chart_snapshot(symbol: str, timeframe: str, target_candle_utc: str) -> dict | None:
    """Read-only Chart DB lookup of the snapshot for one target candle.

    Never raises and never creates/migrates the Chart DB: any lookup failure is
    logged and reported as "no snapshot", so the Paper Signal itself is never
    affected. Safe for --dry-run (a missing Chart DB simply yields None).
    """
    try:
        db_path = chart_db_path(load_chart_env_file())
        return find_snapshot_for_candle(
            db_path, symbol=symbol, timeframe=timeframe, target_candle_utc=target_candle_utc
        )
    except Exception as exc:
        _get_logger().warning(
            "Chart snapshot lookup failed for %s %s (target_candle=%s): %s",
            symbol, timeframe, target_candle_utc, exc,
        )
        return None


def resolve_snapshot_image_url(snapshot: dict) -> str | None:
    """Presigned URL for a stored snapshot's S3 image, or None on any failure.

    Reuses the existing chart_image_uploader presigned-URL path against the
    already-uploaded object -- no rendering, no new S3 upload. A failure is
    logged and yields None so the text notification still goes out.
    """
    s3_key = snapshot.get("image_s3_key") or snapshot.get("s3_key")
    if not s3_key:
        _get_logger().warning("Chart snapshot %s has no S3 key; sending text-only.", snapshot.get("id"))
        return None
    try:
        config = load_s3_config(load_chart_env_file())
        # Sign against the bucket this snapshot's image was actually uploaded
        # to (chart_snapshots.s3_bucket): the environment bucket may have
        # changed since, and signing the row's key against it would yield a
        # 404 or the wrong image. The environment bucket remains the fallback
        # for legacy rows without one.
        row_bucket = str(snapshot.get("s3_bucket") or "").strip()
        if row_bucket:
            config["bucket"] = row_bucket
        return generate_presigned_chart_url(s3_key, config=config)
    except Exception as exc:
        _get_logger().warning("Presigned URL generation failed for %s: %s", s3_key, exc)
        return None


def prepare_signal_chart_image(
    signal_id: str, symbol: str, timeframe: str, target_candle_utc: str
) -> str | None:
    """Find, link, and resolve the Chart Snapshot image for one saved signal.

    Issue #55 ordering for a newly persisted (non-duplicate) Paper Signal:
    search the existing snapshot, persist the relation to the Chart DB, then
    generate the presigned URL for the same webhook post. Every step degrades
    to text-only on failure -- a missing snapshot is an info-level non-event, a
    failed link save still allows the image, and a failed URL still allows the
    text. Never raises.
    """
    logger = _get_logger()
    snapshot = resolve_signal_chart_snapshot(symbol, timeframe, target_candle_utc)
    if snapshot is None:
        logger.info(
            "No chart snapshot for %s %s (target_candle=%s); sending text-only notification.",
            symbol, timeframe, target_candle_utc,
        )
        return None
    try:
        db_path = chart_db_path(load_chart_env_file())
        link_paper_signal_chart_snapshot(
            db_path, paper_signal_id=signal_id, chart_snapshot_id=snapshot["id"]
        )
        logger.info("Linked paper signal %s to chart snapshot %s", signal_id, snapshot["id"])
    except Exception as exc:
        logger.warning(
            "Chart snapshot link save failed for %s -> %s: %s", signal_id, snapshot.get("id"), exc
        )
    return resolve_snapshot_image_url(snapshot)


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


def build_bias_provenance_by_scope(hourly_snapshots: list) -> dict:
    """Record each per-symbol Market Bias's input provenance at generation time.

    Issue #41 review (Major 2): mirrors :func:`build_bias_by_scope`'s per-symbol
    grouping. Each per-symbol bias is classified from that symbol's OWN 1h
    snapshot(s); this captures the ``symbol`` / ``timeframe`` / confirmed
    ``target_candle_utc`` / ``source_snapshot_id`` of *that* classification input
    -- fixed at the moment the bias is generated, from the bias's own data, not
    copied from whatever chart snapshot it is later paired with. Keyed identically
    to ``build_bias_by_scope`` so :func:`resolve_scope_bias` resolves the matching
    provenance for a snapshot; the ``(GLOBAL, 1h)`` fallback carries ``None``
    (a GLOBAL bias spans all symbols and has no single input candle).
    """
    provenance_by_scope: dict = {(GLOBAL_SCOPE_SYMBOL, REPRESENTATIVE_TIMEFRAME): None}
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
        source_snapshot = own_snapshots[0]
        provenance_by_scope[(canonical, REPRESENTATIVE_TIMEFRAME)] = {
            "symbol": canonical,
            "timeframe": _snapshot_get(source_snapshot, "timeframe", REPRESENTATIVE_TIMEFRAME),
            "target_candle_utc": canonical_candle_timestamp(_snapshot_get(source_snapshot, "timestamp")),
            "source_snapshot_id": _snapshot_source_id(source_snapshot),
        }
    return provenance_by_scope


def run_once(
    *,
    dry_run: bool = False,
    db_path: Path = DB_PATH,
    now_utc: datetime | None = None,
) -> list[dict]:
    """Run ONE resident-service cycle: read inputs, generate, store, notify.

    Issue #41: a single cycle of the Paper Signal resident service, fully
    decoupled from the loop (:func:`main`). For each symbol it explicitly
    determines the target *confirmed* candle from the 1h Chart Snapshot's own
    candle timestamp (never "the record closest to now"), verifies the Market
    Bias and Chart Snapshot agree on symbol/timeframe/target candle, and only
    then applies the unchanged strategy rules. The processing order is strictly
    generate -> SQLite save -> Discord notify; a notify failure is logged and
    never rolls back the saved signal, and one symbol failing never stops the
    others.

    ``now_utc`` fixes the evaluation time for the whole cycle (defaults to the
    current UTC clock), so the confirmed-candle decision is deterministic and
    identical from the first startup cycle onward.

    Returns the signals produced this cycle: in normal mode only the ones newly
    persisted (duplicates are skipped before notifying); in ``--dry-run`` the
    would-be signals, with no persistent side effect of any kind.
    """
    # Programmatic callers use run_once(dry_run=True) directly in tests and
    # tooling, so enforce the same console-only no-file contract as the CLI.
    configure_logging(dry_run=dry_run)
    evaluation_time = now_utc if now_utc is not None else datetime.now(UTC)
    # Issue #41 review (Major 1): a dry run must not create/update the chart
    # SQLite. persist=False routes the chart provider through the in-memory path.
    sources = fetch_snapshot_sources(persist=not dry_run)
    hourly_snapshots = sources.get("chart") or []

    bias_result = classify_market_bias(
        sources.get("market"),
        sources.get("fred"),
        sources.get("tradingview"),
        chart_data=hourly_snapshots,
    )

    # Issue #40: resolve a per-symbol bias (GLOBAL fallback), unchanged. The DB is
    # never consulted -- biases are computed in-memory from the same fetch as the
    # snapshots. Issue #41 (Major 2): each bias's input provenance is recorded
    # separately at generation time so input integrity is a genuine comparison,
    # not a value copied from the paired chart snapshot.
    bias_by_scope = build_bias_by_scope(sources, hourly_snapshots, bias_result)
    provenance_by_scope = build_bias_provenance_by_scope(hourly_snapshots)
    created: list[dict] = []

    if not hourly_snapshots:
        _get_logger().info("No Chart Snapshot inputs this cycle; nothing to evaluate.")
        return created

    for snapshot in hourly_snapshots:
        symbol = _snapshot_get(snapshot, "symbol")
        timeframe = _snapshot_get(snapshot, "timeframe", REPRESENTATIVE_TIMEFRAME)
        target_candle = canonical_candle_timestamp(_snapshot_get(snapshot, "timestamp"))

        bias = resolve_scope_bias(bias_by_scope, symbol, timeframe)
        provenance = resolve_scope_bias(provenance_by_scope, symbol, timeframe)

        candidate, result = build_confirmed_candidate(
            snapshot,
            bias,
            provenance,
            evaluation_time=evaluation_time,
            confidence_threshold=CONFIDENCE_THRESHOLD,
        )

        if candidate is None:
            _get_logger().info(
                "Skip/no-signal [%s]: symbol=%s timeframe=%s target_candle=%s",
                result, symbol, timeframe, target_candle,
            )
            continue

        try:
            _process_candidate(candidate, dry_run=dry_run, db_path=db_path, created=created)
        except Exception as exc:  # one symbol's failure must not stop the others
            _get_logger().error(
                "Paper signal processing failed for %s %s (target_candle=%s): %s",
                symbol, timeframe, target_candle, exc,
            )

    return created


def _process_candidate(candidate: dict, *, dry_run: bool, db_path: Path, created: list[dict]) -> None:
    """Save-then-notify one confirmed candidate (Issue #41 ordering + idempotency).

    Normal mode: SQLite save first (idempotent on the target candle); only a
    *newly* inserted signal is then notified, so a duplicate is never re-sent and
    a Discord failure -- logged, never fatal -- leaves the saved signal intact.
    Dry-run: no save, no send, no state; just log the intended input, target
    candle, the generated signal, and the duplicate determination.
    """
    logger = _get_logger()
    signal = create_paper_signal(candidate)
    target_candle = candidate["target_candle_utc"]
    # Carry the confirmed-candle key on the record so the Discord message can
    # show it. It is stored in its own DB column via save_signal_for_candle()'s
    # explicit target_candle_utc arg, not from this dict key.
    signal["target_candle_utc"] = target_candle
    strategy_profile = signal["strategy_profile"]

    logger.info(
        "Signal generated: %s %s %s strategy=%s target_candle=%s bias=%s conf=%.2f",
        signal["symbol"], signal["timeframe"], signal["side"], strategy_profile,
        target_candle, signal["market_bias"], signal["confidence"],
    )

    if dry_run:
        would_duplicate = store.signal_exists_for_candle(
            strategy_profile, signal["symbol"], signal["timeframe"], target_candle, db_path=db_path
        )
        logger.info(
            "[DRY_RUN] target_candle=%s would_persist=%s duplicate=%s",
            target_candle, not would_duplicate, would_duplicate,
        )
        # Issue #55: report the attachment candidate only. The lookup is
        # read-only (a missing Chart DB is never created) and no relation is
        # saved, no presigned URL is generated, and no S3/Discord call is made.
        candidate_snapshot = resolve_signal_chart_snapshot(
            signal["symbol"], signal["timeframe"], target_candle
        )
        logger.info(
            "[DRY_RUN] chart snapshot attachment candidate: %s",
            candidate_snapshot["id"] if candidate_snapshot else "none",
        )
        post_discord_message(format_discord_message(signal), dry_run=True)
        created.append(signal)
        return

    try:
        inserted = store.save_signal_for_candle(signal, target_candle_utc=target_candle, db_path=db_path)
    except Exception as exc:
        # DB save failed: Discord is NOT notified (save-then-notify ordering) and
        # the failure is re-raised so run_once's per-symbol isolation logs it and
        # continues with the next symbol.
        logger.error(
            "DB save failure for %s %s (target_candle=%s): %s -- Discord NOT notified.",
            signal["symbol"], signal["timeframe"], target_candle, exc,
        )
        raise

    if not inserted:
        logger.info(
            "Duplicate skipped: %s %s strategy=%s target_candle=%s (already stored)",
            signal["symbol"], signal["timeframe"], strategy_profile, target_candle,
        )
        return

    logger.info("DB save success: %s (target_candle=%s)", signal["signal_id"], target_candle)

    # Issue #55: only a newly persisted signal gets snapshot search / relation
    # save / presigned URL (a duplicate returned above). Any failure here
    # degrades to a text-only notification -- never to a lost Paper Signal.
    try:
        image_url = prepare_signal_chart_image(
            signal["signal_id"], signal["symbol"], signal["timeframe"], target_candle
        )
    except Exception as exc:
        logger.warning(
            "Chart snapshot attachment failed for %s (target_candle=%s): %s -- sending text-only.",
            signal["signal_id"], target_candle, exc,
        )
        image_url = None

    try:
        post_discord_message(format_discord_message(signal), image_url=image_url, dry_run=False)
        logger.info("Discord send success: %s (target_candle=%s)", signal["signal_id"], target_candle)
    except requests.RequestException as exc:
        logger.error(
            "Discord send failure for %s (target_candle=%s): %s -- saved signal retained.",
            signal["signal_id"], target_candle, exc,
        )

    created.append(signal)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Generate paper-only signals from Market Bias + Chart Snapshot.")
    parser.add_argument("--once", action="store_true", help="Run a single check and exit instead of looping.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print/log instead of saving to SQLite or sending to Discord.",
    )
    return parser.parse_args(argv)


# Issue #41: a process-wide stop flag set by SIGTERM/SIGINT. The loop waits on it
# (instead of a bare sleep) so a systemd stop/restart shuts the service down
# promptly and cleanly, without leaving an orphaned process behind.
_STOP_EVENT = threading.Event()


def _handle_shutdown_signal(signum, _frame) -> None:
    _get_logger().info(
        "Received signal %s; Paper Signal service will stop after the current cycle.", signum
    )
    _STOP_EVENT.set()


def _install_signal_handlers() -> None:
    """Install SIGTERM/SIGINT handlers when possible.

    ``signal.signal`` only works on the main thread of the main interpreter; in
    other contexts (some test runners) it raises, which we ignore -- the resident
    service always runs on the main thread, so production shutdown is unaffected.
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_shutdown_signal)
        except (ValueError, OSError):
            pass


def run_service(
    *,
    dry_run: bool = False,
    once: bool = False,
    interval_seconds: int = CHECK_INTERVAL_SECONDS,
    db_path: Path = DB_PATH,
) -> None:
    """Run the resident loop: one :func:`run_once` cycle per interval until stopped.

    Issue #41: the loop is deliberately thin and separate from the per-cycle work
    (:func:`run_once`), and ``once=True`` runs exactly one cycle with no sleep --
    so tests exercise a single cycle without ever starting the loop or depending
    on real time. Between cycles it waits on the shutdown event rather than
    sleeping, so SIGTERM/SIGINT ends the wait immediately.
    """
    logger = _get_logger()
    logger.info(
        "=== Paper Signal service start === interval=%ss once=%s dry_run=%s confidence_threshold=%.2f",
        interval_seconds, once, dry_run, CONFIDENCE_THRESHOLD,
    )
    try:
        while not _STOP_EVENT.is_set():
            try:
                run_once(dry_run=dry_run, db_path=db_path)
            except Exception as exc:  # never let one cycle kill the resident loop
                logger.error("Cycle failed: %s", exc)
            if once:
                break
            if _STOP_EVENT.wait(interval_seconds):
                break
    finally:
        logger.info("=== Paper Signal service stop ===")


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    # Issue #41 (Major 3): choose console-only logging BEFORE any log call so a
    # --dry-run never creates logs/ or opens the rotating logfile.
    configure_logging(dry_run=args.dry_run)
    _install_signal_handlers()
    run_service(dry_run=args.dry_run, once=args.once)


if __name__ == "__main__":
    main()
