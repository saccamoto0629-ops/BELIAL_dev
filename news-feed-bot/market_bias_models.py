"""Typed model + factory for persisted Market Bias records.

A Market Bias record is an observation/classification log entry only. It
never represents an order, position, entry, exit, stop-loss, take-profit, or
trade recommendation. It exists so a later Backtest layer can look up "what
was the market state at time T" for a symbol/timeframe.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TypedDict

# Market Bias (news-feed-bot/market_bias.py) classifies the overall market
# regime from cross-asset data (VIX, S&P500, Nasdaq, ...). "GLOBAL" marks that
# market-wide scope; Issue #40 additionally persists per-instrument Market Bias
# under a real symbol so a Backtest/Paper Signal for that instrument can prefer
# it over the shared GLOBAL bias. 1h matches the representative chart timeframe
# already used for Market Bias's chart inputs (see
# paper-signal/signal_generator.py's REPRESENTATIVE_TIMEFRAME).
DEFAULT_SCOPE_SYMBOL = "GLOBAL"
# Alias used at the call sites (runner / signal_generator) that talk about the
# "GLOBAL fallback" scope explicitly rather than "the default scope symbol".
GLOBAL_SCOPE_SYMBOL = DEFAULT_SCOPE_SYMBOL
REPRESENTATIVE_TIMEFRAME = "1h"


def normalize_bias_symbol(symbol: str | None) -> str:
    """Canonicalize a symbol for Market Bias scope storage and lookup.

    Issue #40: Chart Snapshots, the Chart DB's ``candles`` table, and a
    Backtest ``--symbol`` disagree on punctuation (``USD_JPY`` vs ``USDJPY``,
    ``XAU/USD`` vs ``XAUUSD``). Per-symbol Market Bias is persisted and looked
    up under one canonical form -- the underscore/slash-free upper-case key the
    Chart DB + chart snapshots already use (``USD_JPY`` -> ``USDJPY``) -- so the
    three layers agree on a single row. ``GLOBAL`` (no punctuation) maps to
    itself, keeping the pre-#40 market-wide scope unchanged. An empty/None
    symbol collapses to ``GLOBAL`` rather than an empty key.
    """
    if symbol is None:
        return DEFAULT_SCOPE_SYMBOL
    if not isinstance(symbol, str):
        raise TypeError("Market Bias symbol must be a string or None")
    normalized = symbol.strip().replace("_", "").replace("/", "").upper()
    return normalized or DEFAULT_SCOPE_SYMBOL


def normalize_bias_timeframe(timeframe: str) -> str:
    """Return the canonical lower-case Market Bias timeframe key."""
    if not isinstance(timeframe, str):
        raise TypeError("Market Bias timeframe must be a string")
    normalized = timeframe.strip().lower()
    if not normalized:
        raise ValueError("Market Bias timeframe must not be empty")
    return normalized


def effective_bias_scopes(symbol: str | None, timeframe: str) -> list[tuple[str, str]]:
    """Return the ordered, de-duplicated ``(symbol, timeframe)`` fallback scopes.

    Issue #40 reference priority for resolving a Market Bias:

    1. requested ``symbol`` + ``timeframe``
    2. requested ``symbol`` + ``1h`` (the representative timeframe)
    3. ``GLOBAL`` + ``timeframe``
    4. ``GLOBAL`` + ``1h``

    The requested symbol is canonicalized via :func:`normalize_bias_symbol`, and
    duplicate scopes are dropped while preserving priority order -- so a
    ``timeframe == "1h"`` request collapses to ``[(symbol, 1h), (GLOBAL, 1h)]``
    and a request already scoped to ``GLOBAL`` collapses to just the two GLOBAL
    scopes (identical to pre-#40 behaviour, hence backward compatible).
    """
    canonical = normalize_bias_symbol(symbol)
    timeframe = normalize_bias_timeframe(timeframe)
    scopes: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for scope in (
        (canonical, timeframe),
        (canonical, REPRESENTATIVE_TIMEFRAME),
        (DEFAULT_SCOPE_SYMBOL, timeframe),
        (DEFAULT_SCOPE_SYMBOL, REPRESENTATIVE_TIMEFRAME),
    ):
        if scope in seen:
            continue
        seen.add(scope)
        scopes.append(scope)
    return scopes


class MarketBiasRecord(TypedDict):
    market_bias_id: str
    evaluated_at: str
    symbol: str
    timeframe: str
    market_bias: str
    confidence: float
    trend: str
    risk: str | None
    risk_confidence: float | None
    volatility: str | None
    volatility_confidence: float | None
    reasons_json: str
    risk_reasons_json: str
    volatility_reasons_json: str
    warnings_json: str
    source_snapshot_id: str | None


def new_market_bias_id() -> str:
    return uuid.uuid4().hex


def utc_now_iso() -> str:
    """Return a UTC ISO-8601 timestamp like ``2026-07-05T12:00:00Z``."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def create_market_bias_record(
    bias_result: dict,
    *,
    symbol: str = DEFAULT_SCOPE_SYMBOL,
    timeframe: str = REPRESENTATIVE_TIMEFRAME,
    source_snapshot_id: str | None = None,
    market_bias_id: str | None = None,
    evaluated_at: str | None = None,
) -> MarketBiasRecord:
    """Build a persisted MarketBiasRecord from classify_market_bias()'s output.

    Reads defensively via ``.get()`` so an older or partial bias_result (e.g.
    missing the Risk/Volatility axes) still produces a valid record instead
    of raising.
    """
    return {
        "market_bias_id": market_bias_id or new_market_bias_id(),
        "evaluated_at": evaluated_at or utc_now_iso(),
        "symbol": normalize_bias_symbol(symbol),
        "timeframe": normalize_bias_timeframe(timeframe),
        "market_bias": bias_result.get("market_bias", "Neutral"),
        "confidence": float(bias_result.get("confidence", 0.0) or 0.0),
        "trend": bias_result.get("trend", bias_result.get("market_bias", "Neutral")),
        "risk": bias_result.get("risk"),
        "risk_confidence": bias_result.get("risk_confidence"),
        "volatility": bias_result.get("volatility"),
        "volatility_confidence": bias_result.get("volatility_confidence"),
        "reasons_json": json.dumps(bias_result.get("reasons", []), ensure_ascii=False),
        "risk_reasons_json": json.dumps(bias_result.get("risk_reasons", []), ensure_ascii=False),
        "volatility_reasons_json": json.dumps(bias_result.get("volatility_reasons", []), ensure_ascii=False),
        "warnings_json": json.dumps(bias_result.get("warnings", []), ensure_ascii=False),
        "source_snapshot_id": source_snapshot_id,
    }
