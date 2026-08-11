r"""Virtual-position data model + state vocabulary for Paper Execution (Issue #49/#50).

*** PAPER EXECUTION. NOT A LIVE ORDER / BROKER LAYER. ***

Implements the concept model defined in ``docs/architecture/paper_execution.md``
(Issue #49). A virtual position is an observation-only simulation of "if this
Paper Signal had been executed under these fixed rules, what would have
happened". It never places an order, calls a broker, or sizes a real lot.

State machine (Issue #49):

    (none) --receive signal--> pending --entry fill--> open --SL/TP/max hold--> closed
                                 |  \--entry timeout------------------------> expired
                                 \----invalid/manual/position-limit---------> cancelled

An intake refused by the Issue #85 concurrent-position cap is recorded as a
terminal ``cancelled`` position (``terminal_reason = position_limit``) directly,
without ever being ``pending`` -- never a silent skip.

- ``pending`` / ``open`` are non-terminal; ``closed`` / ``expired`` / ``cancelled``
  are terminal and are NEVER left again (no re-open, no re-close). The store
  enforces this with CAS UPDATEs; this module pins the *vocabulary* and its
  ``state`` x ``terminal_reason`` closed set, validated at the application layer
  (and again by DB CHECK constraints in ``position_store``).
- ``terminal_reason`` is NULL for non-terminal states and always set for terminal
  states, restricted to the closed set below. "Close" (``closed``) is reserved for
  a *virtual fill*; a position that never entered ends ``expired``/``cancelled``,
  so ``state`` alone tells you whether a position was ever held.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal, TypedDict

# --- states -----------------------------------------------------------------
STATE_PENDING = "pending"
STATE_OPEN = "open"
STATE_CLOSED = "closed"
STATE_EXPIRED = "expired"
STATE_CANCELLED = "cancelled"

KNOWN_STATES: frozenset[str] = frozenset(
    {STATE_PENDING, STATE_OPEN, STATE_CLOSED, STATE_EXPIRED, STATE_CANCELLED}
)
NON_TERMINAL_STATES: frozenset[str] = frozenset({STATE_PENDING, STATE_OPEN})
TERMINAL_STATES: frozenset[str] = frozenset({STATE_CLOSED, STATE_EXPIRED, STATE_CANCELLED})

# --- terminal reasons (closed set, Issue #49; position_limit added by #85) ---
REASON_TAKE_PROFIT = "take_profit"
REASON_STOP_LOSS = "stop_loss"
REASON_MAX_HOLDING_TIME = "max_holding_time"
REASON_ENTRY_TIMEOUT = "entry_timeout"
REASON_INVALID_INPUT = "invalid_input"
REASON_MANUAL = "manual"
REASON_POSITION_LIMIT = "position_limit"  # Issue #85: intake refused by the concurrent-position cap

# The authoritative state x terminal_reason closed set (Issue #49). The store's
# DB CHECK constraint mirrors this exactly.
STATE_TERMINAL_REASONS: dict[str, frozenset[str]] = {
    STATE_CLOSED: frozenset({REASON_TAKE_PROFIT, REASON_STOP_LOSS, REASON_MAX_HOLDING_TIME}),
    STATE_EXPIRED: frozenset({REASON_ENTRY_TIMEOUT}),
    STATE_CANCELLED: frozenset({REASON_INVALID_INPUT, REASON_MANUAL, REASON_POSITION_LIMIT}),
}

# --- side -------------------------------------------------------------------
SIDE_LONG = "long"
SIDE_SHORT = "short"
KNOWN_SIDES: frozenset[str] = frozenset({SIDE_LONG, SIDE_SHORT})

DEFAULT_SIZE = 1.0

Side = Literal["long", "short"]


def validate_state_reason(state: str, terminal_reason: str | None) -> None:
    """Raise ValueError unless ``(state, terminal_reason)`` is in the Issue #49 closed set."""
    if state not in KNOWN_STATES:
        raise ValueError(f"unknown state {state!r}; known: {sorted(KNOWN_STATES)}")
    if state in NON_TERMINAL_STATES:
        if terminal_reason is not None:
            raise ValueError(f"non-terminal state {state!r} must have terminal_reason=None")
        return
    allowed = STATE_TERMINAL_REASONS[state]
    if terminal_reason not in allowed:
        raise ValueError(
            f"terminal state {state!r} requires terminal_reason in {sorted(allowed)}, "
            f"got {terminal_reason!r}"
        )


class VirtualPosition(TypedDict):
    """One virtual position row (Issue #49 concept model made physical).

    Prices are the *effective* (cost-adjusted) fills; ``spread``/``slippage``/
    ``commission`` snapshot the actual amounts applied so the record is fully
    reproducible even after the live execution profile changes. Entry fields are
    NULL in ``pending``; exit/PnL fields are NULL until ``closed``.
    """

    position_id: str
    signal_id: str
    symbol: str
    timeframe: str
    side: str
    strategy_profile: str        # record-only, from the Paper Signal
    execution_profile: str       # Paper Execution owned (profile name)
    state: str
    terminal_reason: str | None
    signal_candle_utc: str       # the signal's target_candle_utc (root candle open)
    entry_deadline_utc: str      # signal candle close + max_entry_delay
    # --- entry (NULL until open) ---
    entry_price: float | None
    entry_time_utc: str | None
    stop_loss_price: float | None
    take_profit_price: float | None
    max_hold_deadline_utc: str | None
    # --- exit / PnL (NULL until closed) ---
    close_price: float | None      # effective (cost-adjusted) close fill
    close_time_utc: str | None
    gross_pnl: float | None         # (raw_close - raw_entry) * size  [short: reversed], pre-cost
    net_pnl: float | None           # gross_pnl - entry_cost - close_cost - fee (each applied once)
    pnl: float | None               # back-compat alias of net_pnl (kept == net_pnl; see #49)
    r_multiple: float | None        # net_pnl / (|entry - stop| * size); None when initial risk <= 0
    # --- execution profile snapshot (fixed at creation) ---
    size: float
    max_entry_delay_bars: int
    max_hold_bars: int
    same_candle_priority: str
    fallback_stop_pct: float
    risk_reward: float
    spread: float
    slippage: float
    commission: float
    # --- close-evaluation watermark (NULL until first evaluated) ---
    last_evaluated_candle_utc: str | None
    # --- bookkeeping ---
    created_at_utc: str
    updated_at_utc: str


def new_position_id() -> str:
    return uuid.uuid4().hex


def utc_now_iso() -> str:
    """Return a UTC ISO-8601 timestamp like ``2026-07-05T12:00:00Z``."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _snapshot_fields(profile) -> dict:
    """Snapshot the execution-profile params that make a position self-contained."""
    return {
        "execution_profile": profile.name,
        "size": float(profile.size),
        "max_entry_delay_bars": int(profile.max_entry_delay_bars),
        "max_hold_bars": int(profile.max_hold_bars),
        "same_candle_priority": profile.same_candle_priority,
        "fallback_stop_pct": float(profile.fallback_stop_pct),
        "risk_reward": float(profile.risk_reward),
        "spread": float(profile.spread),
        "slippage": float(profile.slippage),
        "commission": float(profile.commission),
    }


def _base_record(
    *,
    signal_id: str,
    symbol: str,
    timeframe: str,
    side: str,
    strategy_profile: str,
    profile,
    signal_candle_utc: str,
    entry_deadline_utc: str,
    position_id: str | None,
    created_at: str | None,
) -> VirtualPosition:
    now = created_at or utc_now_iso()
    record: VirtualPosition = {
        "position_id": position_id or new_position_id(),
        "signal_id": signal_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "side": side,
        "strategy_profile": strategy_profile,
        "state": STATE_PENDING,
        "terminal_reason": None,
        "signal_candle_utc": signal_candle_utc,
        "entry_deadline_utc": entry_deadline_utc,
        "entry_price": None,
        "entry_time_utc": None,
        "stop_loss_price": None,
        "take_profit_price": None,
        "max_hold_deadline_utc": None,
        "close_price": None,
        "close_time_utc": None,
        "gross_pnl": None,
        "net_pnl": None,
        "pnl": None,
        "r_multiple": None,
        "last_evaluated_candle_utc": None,
        "created_at_utc": now,
        "updated_at_utc": now,
    }
    record.update(_snapshot_fields(profile))  # type: ignore[typeddict-item]
    return record


def create_pending_position(
    *,
    signal_id: str,
    symbol: str,
    timeframe: str,
    side: str,
    strategy_profile: str,
    profile,
    signal_candle_utc: str,
    entry_deadline_utc: str,
    position_id: str | None = None,
    created_at: str | None = None,
) -> VirtualPosition:
    """Build a ``pending`` virtual position from an accepted Paper Signal.

    Issue #49: a received signal is stored as ``pending`` (NOT opened) with the
    entry deadline and execution-profile snapshot recorded up front. ``side`` is
    validated here so a malformed side never reaches the price logic.
    """
    if side not in KNOWN_SIDES:
        raise ValueError(f"side must be one of {sorted(KNOWN_SIDES)}, got {side!r}")
    return _base_record(
        signal_id=signal_id, symbol=symbol, timeframe=timeframe, side=side,
        strategy_profile=strategy_profile, profile=profile,
        signal_candle_utc=signal_candle_utc, entry_deadline_utc=entry_deadline_utc,
        position_id=position_id, created_at=created_at,
    )


def create_cancelled_position(
    *,
    signal_id: str,
    symbol: str,
    timeframe: str,
    side: str,
    strategy_profile: str,
    profile,
    signal_candle_utc: str,
    entry_deadline_utc: str,
    terminal_reason: str = REASON_INVALID_INPUT,
    position_id: str | None = None,
    created_at: str | None = None,
) -> VirtualPosition:
    """Build a terminal ``cancelled`` position for an invalid/withdrawn/refused signal.

    Issue #49: an invalid signal is not silently skipped -- it is RECORDED as
    ``cancelled`` (``invalid_input`` or ``manual``; Issue #85 adds
    ``position_limit`` for a signal refused by the concurrent-position cap).
    ``side``/``symbol``/
    ``timeframe`` may be blank when the input was malformed; they are coerced to
    ``""`` rather than validated, since the whole point is to record the reject.
    """
    validate_state_reason(STATE_CANCELLED, terminal_reason)
    record = _base_record(
        signal_id=signal_id, symbol=symbol or "", timeframe=timeframe or "", side=side or "",
        strategy_profile=strategy_profile or "", profile=profile,
        signal_candle_utc=signal_candle_utc or "", entry_deadline_utc=entry_deadline_utc or "",
        position_id=position_id, created_at=created_at,
    )
    record["state"] = STATE_CANCELLED
    record["terminal_reason"] = terminal_reason
    return record
