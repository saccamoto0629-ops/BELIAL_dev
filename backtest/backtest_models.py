"""Typed models + factories for Backtest run/signal records.

A Backtest run replays historical data through a strategy/exit policy and
records what a hypothetical entry/exit would have produced. These records
are historical simulation results only -- they never represent a real
order, live position, broker call, lot size, or automated trade execution.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TypedDict

# Issue #27: MIGRATION-ONLY fallback values for a Backtest Signal's four
# classification columns.
#
# Normal processing never uses this: the Backtest Runner copies all four fields
# straight off the Paper Signal candidate and saves those exact values (see
# backtest/runner.py). This constant exists *solely* to repair pre-Issue-#27
# databases and legacy/partial rows -- i.e. backtest_store.py's schema migration
# backfills any NULL/empty classification cell with these defaults.
#
# By contract these values must stay identical to paper-signal/
# signal_models.CLASSIFICATION_FIELD_DEFAULTS (a backfilled legacy row should
# read exactly as a freshly generated one would). They are duplicated here --
# not imported -- to keep this low-level backtest module free of a cross-package
# import into paper-signal; a backtest test asserts the two dicts stay equal.
BACKTEST_MIGRATION_CLASSIFICATION_DEFAULTS: dict[str, str] = {
    "signal_type": "trend_follow",
    "trigger_type": "macd_state_alignment",
    "alert_source": "composite",
    "strategy_profile": "market_bias_macd_alignment_v1",
}


class BacktestRunRecord(TypedDict):
    id: str
    run_name: str
    symbol: str
    timeframe: str
    from_time: str
    to_time: str
    strategy_name: str
    exit_policy: str | None
    config_json: str
    created_at: str


class BacktestSignalRecord(TypedDict):
    id: str
    run_id: str
    signal_time: str
    symbol: str
    timeframe: str
    bias: str | None
    confidence: float | None
    direction: str | None
    signal_type: str | None
    trigger_type: str | None
    alert_source: str | None
    strategy_profile: str | None
    reason_json: str
    entry_price: float | None
    invalidation_price: float | None
    take_profit_price: float | None
    close_price: float | None
    result_status: str | None
    pnl: float | None
    mfe: float | None
    mae: float | None
    closed_at: str | None
    close_reason: str | None
    exit_policy: str | None
    # Issue #39: the Market State classified for this signal's bar, and the
    # classifier's confidence for it. Nullable: signals saved before this column
    # existed (or generated when no state was resolvable) read back as None.
    market_state: str | None
    market_state_confidence: float | None
    created_at: str


def new_id() -> str:
    return uuid.uuid4().hex


def utc_now_iso() -> str:
    """Return a UTC ISO-8601 timestamp like ``2026-07-05T12:00:00Z``."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def create_backtest_run_record(
    *,
    run_name: str,
    symbol: str,
    timeframe: str,
    from_time: str,
    to_time: str,
    strategy_name: str,
    exit_policy: str | None = None,
    config: dict | None = None,
    run_id: str | None = None,
    created_at: str | None = None,
) -> BacktestRunRecord:
    """Build a BacktestRunRecord describing one backtest run's conditions.

    Calling this again (even with identical run_name/symbol/timeframe)
    always produces a fresh ``id`` unless ``run_id`` is explicitly passed,
    so re-running a backtest naturally creates a separate, independent run
    instead of overwriting the previous one.
    """
    return {
        "id": run_id or new_id(),
        "run_name": run_name,
        "symbol": symbol,
        "timeframe": timeframe,
        "from_time": from_time,
        "to_time": to_time,
        "strategy_name": strategy_name,
        "exit_policy": exit_policy,
        "config_json": json.dumps(config or {}, ensure_ascii=False),
        "created_at": created_at or utc_now_iso(),
    }


def create_backtest_signal_record(
    run_id: str,
    *,
    signal_time: str,
    symbol: str,
    timeframe: str,
    bias: str | None = None,
    confidence: float | None = None,
    direction: str | None = None,
    signal_type: str | None = None,
    trigger_type: str | None = None,
    alert_source: str | None = None,
    strategy_profile: str | None = None,
    reasons: list[str] | None = None,
    entry_price: float | None = None,
    invalidation_price: float | None = None,
    take_profit_price: float | None = None,
    close_price: float | None = None,
    result_status: str | None = None,
    pnl: float | None = None,
    mfe: float | None = None,
    mae: float | None = None,
    closed_at: str | None = None,
    close_reason: str | None = None,
    exit_policy: str | None = None,
    market_state: str | None = None,
    market_state_confidence: float | None = None,
    signal_id: str | None = None,
    created_at: str | None = None,
) -> BacktestSignalRecord:
    """Build a BacktestSignalRecord belonging to ``run_id``.

    ``entry_price`` / ``invalidation_price`` / ``take_profit_price`` /
    ``close_price`` / ``pnl`` / ``mfe`` / ``mae`` describe a simulated,
    historical outcome computed by a (separate) backtest runner replaying
    past data -- this factory only shapes the record for storage and does
    not place orders, size positions, or make any live trading decision.

    ``exit_policy`` names which paper-only Exit Policy (see
    ``paper-signal/exit_policy.py``) produced the close fields on this
    signal. It is nullable: a signal can be saved before its exit is known
    (still "open"), and older rows saved before this column existed read
    back as ``None``. See ``apply_exit_policy_result()`` below for the
    usual way to fill it in once an ``ExitPolicyResult`` is available.
    """
    return {
        "id": signal_id or new_id(),
        "run_id": run_id,
        "signal_time": signal_time,
        "symbol": symbol,
        "timeframe": timeframe,
        "bias": bias,
        "confidence": confidence,
        "direction": direction,
        "signal_type": signal_type,
        "trigger_type": trigger_type,
        "alert_source": alert_source,
        "strategy_profile": strategy_profile,
        "reason_json": json.dumps(reasons or [], ensure_ascii=False),
        "entry_price": entry_price,
        "invalidation_price": invalidation_price,
        "take_profit_price": take_profit_price,
        "close_price": close_price,
        "result_status": result_status,
        "pnl": pnl,
        "mfe": mfe,
        "mae": mae,
        "closed_at": closed_at,
        "close_reason": close_reason,
        "exit_policy": exit_policy,
        "market_state": market_state,
        "market_state_confidence": market_state_confidence,
        "created_at": created_at or utc_now_iso(),
    }


def apply_exit_policy_result(signal_record: dict, exit_result) -> dict:
    """Merge a paper-signal ExitPolicyResult onto an existing BacktestSignalRecord dict.

    This is the bridge between paper-only Exit Policy evaluation
    (``paper-signal/exit_policy.evaluate_exit()``) and Backtest storage: it
    is how "where did this hypothesis die" ends up reliably in
    ``backtest_signals`` instead of being computed and then discarded.
    Never places an order or touches a broker -- purely a field mapping.

    ``exit_result`` is duck-typed: it accepts either an
    ``exit_policy.ExitPolicyResult`` instance or an equivalent plain dict,
    so this module does not need to import paper-signal/exit_policy.py.

    Field mapping (ExitPolicyResult -> BacktestSignalRecord):
      status       -> result_status
      close_price  -> close_price
      close_time   -> closed_at
      close_reason -> close_reason
      exit_policy  -> exit_policy
      mfe          -> mfe
      mae          -> mae

    ``pnl`` (price-units, ``close_price - entry_price`` for "long",
    ``entry_price - close_price`` for "short") is computed here too, once
    ``close_price``/``entry_price``/``direction`` are all available --
    this is the only place a signal's pnl is ever derived, so Backtest
    reporting (see backtest/summary.py) has a real value to aggregate
    instead of every signal reading back with ``pnl=None``. When there is
    no ``close_price`` yet (still "open"/"no_close"), ``pnl`` is left
    exactly as it was on ``signal_record`` (``None`` unless the caller
    already set one) -- never fabricated for an unresolved signal.

    Returns a new dict (``signal_record`` is not mutated); all other
    fields of ``signal_record`` (id, run_id, entry_price, ...) pass through
    unchanged.
    """

    def _field(name, default=None):
        if isinstance(exit_result, dict):
            return exit_result.get(name, default)
        return getattr(exit_result, name, default)

    updated = dict(signal_record)
    updated["result_status"] = _field("status")
    updated["close_price"] = _field("close_price")
    updated["closed_at"] = _field("close_time")
    updated["close_reason"] = _field("close_reason")
    updated["exit_policy"] = _field("exit_policy")
    updated["mfe"] = _field("mfe")
    updated["mae"] = _field("mae")

    close_price = updated["close_price"]
    entry_price = signal_record.get("entry_price")
    direction = signal_record.get("direction")
    if close_price is not None and entry_price is not None and direction in ("long", "short"):
        updated["pnl"] = (close_price - entry_price) if direction == "long" else (entry_price - close_price)

    return updated
