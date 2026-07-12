"""Typed model + factory for Paper Signal records.

A Paper Signal is a hypothesis record only. It never represents a real
order, position, lot size, leverage, stop-loss/take-profit level, or trade
execution. Every record's ``status`` is always ``"paper_only"``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import NotRequired, TypedDict

STATUS_PAPER_ONLY = "paper_only"

# --- Issue #27: signal classification / provenance --------------------------
#
# Four descriptive fields distinguish *what kind* of signal this is (Strategy /
# Method layer) from *how it was triggered and where its inputs came from*
# (Alert layer). They are recorded for tracking only and never influence
# entries, exits, sizing, or any execution decision.
#
# The current Paper Signal does NOT detect a live MACD *cross* event; it emits
# a candidate when the Market Bias and the current MACD *state* agree. So the
# trigger is deliberately recorded as ``macd_state_alignment`` -- never
# ``macd_cross``, which would misrepresent the input as a real crossover event.
SIGNAL_TYPE_TREND_FOLLOW = "trend_follow"
TRIGGER_TYPE_MACD_STATE_ALIGNMENT = "macd_state_alignment"
ALERT_SOURCE_COMPOSITE = "composite"
STRATEGY_PROFILE_MARKET_BIAS_MACD_ALIGNMENT_V1 = "market_bias_macd_alignment_v1"

# Canonical field order + default values for the current Paper Signal. Reused
# by signal_generator.build_candidates() (explicit values on every candidate),
# by create_paper_signal() (.get() fallback for legacy candidates), and by
# paper_signal_store (SQLite column defaults / legacy-row backfill), so the
# four strings live in exactly one place.
CLASSIFICATION_FIELD_DEFAULTS: dict[str, str] = {
    "signal_type": SIGNAL_TYPE_TREND_FOLLOW,
    "trigger_type": TRIGGER_TYPE_MACD_STATE_ALIGNMENT,
    "alert_source": ALERT_SOURCE_COMPOSITE,
    "strategy_profile": STRATEGY_PROFILE_MARKET_BIAS_MACD_ALIGNMENT_V1,
}


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
    signal_type: str
    trigger_type: str
    alert_source: str
    strategy_profile: str
    status: str


class SignalContext(TypedDict):
    """Already-fetched inputs for candidate generation.

    Bundles exactly what ``signal_generator.build_candidates()`` needs, so
    Backtest can build one directly from its own historical data slice and
    call ``generate_signal_candidates()`` / ``generate_signal()`` without
    touching any live Provider, DB, or Discord.

    Issue #40: ``bias_by_scope`` optionally carries a per-symbol Market Bias map
    ``{(normalized_symbol, timeframe): bias_result}`` (always including the
    ``(GLOBAL, 1h)`` fallback scope). When present, candidate generation resolves
    each snapshot's bias by its own symbol/timeframe (falling back to GLOBAL);
    when absent, the single ``bias_result`` is applied to every snapshot exactly
    as before. Backtest keeps passing a single pre-resolved ``bias_result`` per
    bar, so its pure, DB-free path is unchanged -- ``bias_by_scope`` is only used
    by live Paper Signal, and it too must already be resolved (no DB lookup
    happens inside the pure candidate functions).
    """

    bias_result: dict
    hourly_snapshots: list
    confidence_threshold: float
    bias_by_scope: NotRequired[dict]


class SignalCandidate(TypedDict):
    """A candidate produced by build_candidates(), before persistence.

    This is not yet a PaperSignal: it has no signal_id, created_at_utc, or
    status. create_paper_signal() converts a SignalCandidate into a
    PaperSignal only when the caller decides to actually persist it.

    Issue #27 classification boundary: the four fields below (signal_type /
    trigger_type / alert_source / strategy_profile) are **required** on every
    candidate that build_candidates() emits today -- they are always populated.
    They are declared as plain required keys here (no ``NotRequired``) on
    purpose: current build output is the contract. The *only* place a candidate
    is allowed to omit them is an externally-supplied, pre-Issue-#27 dict passed
    straight into create_paper_signal(), which tolerates their absence via
    ``.get()`` defaults (see CLASSIFICATION_FIELD_DEFAULTS). Do not relax this
    type to make the fields optional for build output.
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
    signal_type: str
    trigger_type: str
    alert_source: str
    strategy_profile: str


def create_signal_context(
    bias_result: dict,
    hourly_snapshots: list,
    *,
    confidence_threshold: float,
    bias_by_scope: dict | None = None,
) -> SignalContext:
    """Bundle Market Bias + Chart Snapshot data into a SignalContext.

    Issue #40: pass ``bias_by_scope`` to have candidate generation resolve each
    snapshot's bias per symbol/timeframe (GLOBAL fallback). Omit it to keep the
    pre-#40 behaviour of applying the single ``bias_result`` to every snapshot.
    """
    context: SignalContext = {
        "bias_result": bias_result,
        "hourly_snapshots": hourly_snapshots,
        "confidence_threshold": confidence_threshold,
    }
    if bias_by_scope is not None:
        context["bias_by_scope"] = bias_by_scope
    return context


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

    The Issue #27 classification fields (signal_type / trigger_type /
    alert_source / strategy_profile) are read with ``.get()`` and fall back to
    ``CLASSIFICATION_FIELD_DEFAULTS`` so a legacy candidate that predates those
    fields still builds cleanly (with the current Paper Signal's values). The
    normal build_candidates() output already carries all four explicitly.
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
        "signal_type": candidate.get("signal_type", SIGNAL_TYPE_TREND_FOLLOW),
        "trigger_type": candidate.get("trigger_type", TRIGGER_TYPE_MACD_STATE_ALIGNMENT),
        "alert_source": candidate.get("alert_source", ALERT_SOURCE_COMPOSITE),
        "strategy_profile": candidate.get("strategy_profile", STRATEGY_PROFILE_MARKET_BIAS_MACD_ALIGNMENT_V1),
        "status": STATUS_PAPER_ONLY,
    }
