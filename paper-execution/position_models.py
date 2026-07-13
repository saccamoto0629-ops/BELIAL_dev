"""Typed model + factory for Paper Execution *virtual positions* (Issue #50).

*** PAPER EXECUTION. NOT A LIVE ORDER / BROKER LAYER. ***

Paper Execution consumes already-decided Paper Signals (paper-only
hypotheses, see ``paper-signal/signal_models.py``) plus historical/updating
candles, and manages a *virtual* position: a simulated Open, a simulated
Close, and the resulting simulated PnL. It NEVER places a real order, calls a
broker/Execution API, sizes a real lot, or connects to any exchange. Every
record here is an observation-only simulation, one layer removed from Paper
Signal and completely separate from a future Live Execution layer.

Responsibility boundary (kept deliberately narrow so Market Watch / Alert /
Paper Signal logic never leaks in here, and Execution logic never flows back
into them):

- This module only *shapes and validates* a virtual-position record. It has
  no SQLite, no candle walking, no Discord, and no entry/exit *decision*
  logic -- those live in ``position_store.py`` (persistence) and
  ``execution_engine.py`` (pure Open/Close judgement) respectively.

Vocabulary (all strings are constrained to the module-level constants below,
never free-typed at call sites -- mirroring how ``signal_models.py`` pins its
status/classification strings in one place):

- ``status``       -- ``open`` while the position is live, ``closed`` once an
                      exit fired. There is no third state in this minimal model.
- ``side``         -- ``long`` / ``short`` (copied from the Paper Signal).
- ``close_reason`` -- why the position closed. The three implemented reasons are
                      kept byte-identical to the Backtest Exit vocabulary
                      (``paper-signal/exit_policy.py``) so a Paper Execution
                      close reads exactly like the equivalent Backtest exit:
                      ``stop_loss`` / ``take_profit`` / ``time_expired``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal, NotRequired, TypedDict

# --- status -----------------------------------------------------------------
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
KNOWN_STATUSES: frozenset[str] = frozenset({STATUS_OPEN, STATUS_CLOSED})

# --- side -------------------------------------------------------------------
SIDE_LONG = "long"
SIDE_SHORT = "short"
KNOWN_SIDES: frozenset[str] = frozenset({SIDE_LONG, SIDE_SHORT})

# --- close_reason -----------------------------------------------------------
# Intentionally string-equal to paper-signal/exit_policy.py's REASON_* values
# so a Paper Execution close and the equivalent Backtest exit share one
# vocabulary. They are duplicated here -- not imported -- to keep this
# dependency-light model free of a cross-package import into paper-signal; a
# unit test asserts the two vocabularies stay in agreement.
CLOSE_REASON_STOP_LOSS = "stop_loss"
CLOSE_REASON_TAKE_PROFIT = "take_profit"
CLOSE_REASON_TIME_EXPIRED = "time_expired"
KNOWN_CLOSE_REASONS: frozenset[str] = frozenset(
    {CLOSE_REASON_STOP_LOSS, CLOSE_REASON_TAKE_PROFIT, CLOSE_REASON_TIME_EXPIRED}
)

DEFAULT_SIZE = 1.0

Side = Literal["long", "short"]


class VirtualPosition(TypedDict):
    """One simulated Paper Execution position.

    ``paper_signal_id`` links the position back to the Paper Signal that
    justified it (Issue #50 traceability); it is also the idempotency key --
    the store enforces one virtual position per Paper Signal via a UNIQUE
    index, so re-processing the same signal can never open a second position.

    Prices are the *executed* (Execution-Model-corrected) fills produced by
    ``execution_engine.py`` -- see that module and ``execution_runner.py`` for
    exactly where spread/slippage is applied (once at entry, once at a
    non-take-profit exit; a take-profit fills at the fixed target and is never
    re-corrected). ``stop_loss`` / ``take_profit`` are the fixed levels the
    Open decision pinned relative to the executed entry.

    All exit / PnL fields are ``None`` while ``status == "open"`` and are filled
    atomically at Close.
    """

    position_id: str
    paper_signal_id: str
    strategy_profile: str
    symbol: str
    timeframe: str
    side: str
    status: str
    # --- entry ---
    entry_price: float
    size: float
    stop_loss: float | None
    take_profit: float | None
    opened_at: str
    # --- exit (None until closed) ---
    exit_price: float | None
    closed_at: str | None
    close_reason: str | None
    # --- PnL / cost decomposition (None until closed) ---
    gross_pnl: float | None
    net_pnl: float | None
    spread_cost: float | None
    slippage_cost: float | None
    fee_cost: float | None
    r_multiple: float | None
    # --- provenance / bookkeeping ---
    entry_signal_time: NotRequired[str | None]
    created_at: str
    updated_at: str


# --- event history ----------------------------------------------------------
EVENT_OPENED = "opened"
EVENT_CLOSED = "closed"
KNOWN_EVENT_TYPES: frozenset[str] = frozenset({EVENT_OPENED, EVENT_CLOSED})


class PositionEvent(TypedDict):
    """One append-only state-change record for a virtual position (Issue #50).

    Written in the SAME transaction as the position create/close it describes,
    so the position row and its history can never diverge. ``(position_id,
    event_type)`` is UNIQUE, giving history-level idempotency (one ``opened``
    and at most one ``closed`` per position).
    """

    event_id: str
    position_id: str
    event_type: str
    event_time: str
    price: float | None
    reason: str | None
    created_at: str


def new_position_id() -> str:
    return uuid.uuid4().hex


def new_event_id() -> str:
    return uuid.uuid4().hex


def utc_now_iso() -> str:
    """Return a UTC ISO-8601 timestamp like ``2026-07-05T12:00:00Z``.

    Byte-identical in shape to ``signal_models.utc_now_iso`` /
    ``backtest_models.utc_now_iso`` so timestamps sort and compare uniformly
    across the three layers.
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def create_virtual_position(
    *,
    paper_signal_id: str,
    strategy_profile: str,
    symbol: str,
    timeframe: str,
    side: str,
    entry_price: float,
    opened_at: str,
    size: float = DEFAULT_SIZE,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    entry_signal_time: str | None = None,
    position_id: str | None = None,
    created_at: str | None = None,
) -> VirtualPosition:
    """Build an ``open`` VirtualPosition record.

    Only shapes/validates the record -- it does not persist anything and makes
    no entry/exit *decision* (that is ``execution_engine.decide_open``'s job).
    ``side`` is validated against ``KNOWN_SIDES`` so a malformed side can never
    reach storage or the price-comparison logic. The record starts with every
    exit / PnL field ``None`` and ``status == "open"``.
    """
    if side not in KNOWN_SIDES:
        raise ValueError(f"side must be one of {sorted(KNOWN_SIDES)}, got {side!r}")
    now = created_at or utc_now_iso()
    return {
        "position_id": position_id or new_position_id(),
        "paper_signal_id": paper_signal_id,
        "strategy_profile": strategy_profile,
        "symbol": symbol,
        "timeframe": timeframe,
        "side": side,
        "status": STATUS_OPEN,
        "entry_price": float(entry_price),
        "size": float(size),
        "stop_loss": None if stop_loss is None else float(stop_loss),
        "take_profit": None if take_profit is None else float(take_profit),
        "opened_at": opened_at,
        "exit_price": None,
        "closed_at": None,
        "close_reason": None,
        "gross_pnl": None,
        "net_pnl": None,
        "spread_cost": None,
        "slippage_cost": None,
        "fee_cost": None,
        "r_multiple": None,
        "entry_signal_time": entry_signal_time,
        "created_at": now,
        "updated_at": now,
    }
