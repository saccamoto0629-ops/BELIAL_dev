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
# regime from cross-asset data (VIX, S&P500, Nasdaq, ...), not a single
# instrument. "GLOBAL" marks that market-wide scope; a future per-instrument
# Market Bias could pass a real symbol instead. 1h matches the representative
# chart timeframe already used for Market Bias's chart inputs (see
# paper-signal/signal_generator.py's REPRESENTATIVE_TIMEFRAME).
DEFAULT_SCOPE_SYMBOL = "GLOBAL"
REPRESENTATIVE_TIMEFRAME = "1h"


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
        "symbol": symbol,
        "timeframe": timeframe,
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
