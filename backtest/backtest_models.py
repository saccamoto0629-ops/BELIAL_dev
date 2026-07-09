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
    signal_id: str | None = None,
    created_at: str | None = None,
) -> BacktestSignalRecord:
    """Build a BacktestSignalRecord belonging to ``run_id``.

    ``entry_price`` / ``invalidation_price`` / ``take_profit_price`` /
    ``close_price`` / ``pnl`` / ``mfe`` / ``mae`` describe a simulated,
    historical outcome computed by a (separate) backtest runner replaying
    past data -- this factory only shapes the record for storage and does
    not place orders, size positions, or make any live trading decision.
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
        "created_at": created_at or utc_now_iso(),
    }
