"""Typed model + factory for Paper Signal records.

A Paper Signal is a hypothesis record only. It never represents a real
order, position, lot size, leverage, stop-loss/take-profit level, or trade
execution. Every record's ``status`` is always ``"paper_only"``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TypedDict

STATUS_PAPER_ONLY = "paper_only"


class PaperSignal(TypedDict):
    signal_id: str
    created_at_utc: str
    symbol: str
    timeframe: str
    side: str
    market_bias: str
    confidence: float
    macd_state: str
    trend: str
    state: str
    freshness: str
    reason: str
    status: str


class SignalContext(TypedDict):
    """Already-fetched inputs for candidate generation.

    Bundles exactly what ``signal_generator.build_candidates()`` needs, so
    Backtest can build one directly from its own historical data slice and
    call ``generate_signal_candidates()`` / ``generate_signal()`` without
    touching any live Provider, DB, or Discord.
    """

    bias_result: dict
    hourly_snapshots: list
    confidence_threshold: float


class SignalCandidate(TypedDict):
    """A candidate produced by build_candidates(), before persistence.

    This is not yet a PaperSignal: it has no signal_id, created_at_utc, or
    status. create_paper_signal() converts a SignalCandidate into a
    PaperSignal only when the caller decides to actually persist it.
    """

    symbol: str
    timeframe: str
    side: str
    market_bias: str
    confidence: float
    macd_state: str
    trend: str
    state: str
    freshness: str
    reason: str


def create_signal_context(
    bias_result: dict,
    hourly_snapshots: list,
    *,
    confidence_threshold: float,
) -> SignalContext:
    """Bundle Market Bias + Chart Snapshot data into a SignalContext."""
    return {
        "bias_result": bias_result,
        "hourly_snapshots": hourly_snapshots,
        "confidence_threshold": confidence_threshold,
    }


def new_signal_id() -> str:
    return uuid.uuid4().hex


def utc_now_iso() -> str:
    """Return a UTC ISO-8601 timestamp like ``2026-07-05T12:00:00Z``."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def create_paper_signal(
    candidate: dict,
    *,
    signal_id: str | None = None,
    created_at_utc: str | None = None,
) -> PaperSignal:
    """Build a PaperSignal record from a candidate dict produced by signal_generator.

    ``candidate`` must already contain: symbol, timeframe, side, market_bias,
    confidence, macd_state, trend, state, freshness, reason.
    """
    return {
        "signal_id": signal_id or new_signal_id(),
        "created_at_utc": created_at_utc or utc_now_iso(),
        "symbol": candidate["symbol"],
        "timeframe": candidate["timeframe"],
        "side": candidate["side"],
        "market_bias": candidate["market_bias"],
        "confidence": candidate["confidence"],
        "macd_state": candidate["macd_state"],
        "trend": candidate["trend"],
        "state": candidate["state"],
        "freshness": candidate["freshness"],
        "reason": candidate["reason"],
        "status": STATUS_PAPER_ONLY,
    }
