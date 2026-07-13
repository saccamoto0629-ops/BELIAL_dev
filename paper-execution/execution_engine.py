"""Pure Open / Close judgement for Paper Execution (Issue #49/#51).

*** PAPER EXECUTION. NOT A LIVE ORDER / BROKER LAYER. ***

Pure, deterministic domain logic over already-loaded *confirmed* candles. No
SQLite, no Discord, no candle loading, no CLI, no wall-clock dependence beyond an
explicit ``now_utc`` used only to decide whether an entry deadline has passed.

Entry (Issue #49)
-----------------
- The signal's *root* candle (``signal_candle_utc``) is NEVER the entry candle
  (that would reintroduce the Backtest close-at-signal-bar lookahead).
- Entry candle = the FIRST confirmed candle whose open is at/after the root
  candle's close (``signal_candle_utc + timeframe``). Missing candles are fine --
  the first *existing* confirmed candle in the window is used.
- Entry price = that candle's **open**, shifted unfavourably by spread+slippage
  once (long: ``open + cost``; short: ``open - cost``).
- Entry deadline: if the entry candle's open is later than
  ``signal_close + max_entry_delay`` the signal ``expired`` (``entry_timeout``).
- Weekend carry-over is refused: if a Saturday/Sunday (UTC) lies between the
  signal close and the entry candle's open (e.g. a Friday signal reaching a
  Monday candle), the signal ``expired`` rather than entering.

Close (Issue #49)
-----------------
Confirmed candles from the entry candle onward are walked in time order (the
entry candle's OWN high/low count, since entry is at its open). Per candle, in
order, stopping at the first close:

  1. gap (candle open): open already beyond SL -> fill at the raw **open**
     (stop_loss); open already beyond TP -> fill at the **TP** level
     (take_profit). Ends this candle's evaluation (no intrabar / max-hold).
  2. intrabar (high/low): SL/TP touched within the bar. If BOTH touch, the
     ambiguous bar is resolved by the position's stored ``same_candle_priority``
     (NOT hardcoded) -- default ``stop_loss`` (conservative).
  3. max hold: if the bar's close time has reached ``max_hold_deadline_utc``,
     close at the bar's **close** (max_holding_time). Only checked when 1-2 did
     not close, so an intrabar price hit always wins over the hold deadline.

Cost is applied once per side: once at entry (above) and once at close
(long close: ``price - cost``; short close: ``price + cost``). ``pnl`` is the
effective close minus the effective entry (short: reversed) minus commission --
so spread/slippage are counted exactly once each and never re-corrected.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
BACKTEST_DIR = REPO_ROOT / "backtest"
MARKET_WATCH_DIR = REPO_ROOT / "market-watch"

for _dir in (BASE_DIR, BACKTEST_DIR, MARKET_WATCH_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import execution_model as execution  # noqa: E402  (backtest: fixed spread/slippage fill model)
import position_models as models  # noqa: E402
import execution_profile as profiles  # noqa: E402
from freshness import (  # noqa: E402  (market-watch: shared candle-timing rules)
    candle_close_time,
    parse_timestamp_utc,
    timeframe_to_timedelta,
)

LONG = models.SIDE_LONG
SHORT = models.SIDE_SHORT

ENTRY_ACTION_OPEN = "open"
ENTRY_ACTION_EXPIRED = "expired"
ENTRY_ACTION_PENDING = "pending"

_REQUIRED_CANDLE_COLUMNS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class EntryOutcome:
    """Result of a pending position's entry evaluation."""

    action: str  # ENTRY_ACTION_OPEN / _EXPIRED / _PENDING
    entry_price: float | None = None
    entry_time_utc: str | None = None
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    max_hold_deadline_utc: str | None = None
    terminal_reason: str | None = None


@dataclass(frozen=True)
class CloseWalkResult:
    """Result of walking confirmed candles for an open position's close.

    ``close_price`` is the effective (cost-adjusted) close fill. ``gross_pnl`` is
    pre-cost ``(raw_close - raw_entry) * size`` (short: reversed); ``net_pnl``
    subtracts entry cost, close cost, and commission -- each exactly once.
    ``r_multiple`` is net_pnl / (initial risk * size), or ``None`` when the
    initial risk ``|entry - stop|`` is non-positive/non-finite.
    """

    closed: bool
    new_watermark: str | None
    close_price: float | None = None
    close_time_utc: str | None = None
    terminal_reason: str | None = None
    gross_pnl: float | None = None
    net_pnl: float | None = None
    r_multiple: float | None = None


def _iso_z(value) -> str:
    return parse_timestamp_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_entry_deadline(signal_candle_utc: str, timeframe: str, max_entry_delay_bars: int) -> str:
    """Return the entry deadline (UTC ...Z): signal close + max_entry_delay bars."""
    signal_close = candle_close_time(signal_candle_utc, timeframe)
    deadline = signal_close + max_entry_delay_bars * timeframe_to_timedelta(timeframe)
    return _iso_z(deadline)


def _weekend_between(start: pd.Timestamp, end: pd.Timestamp) -> bool:
    """True if any Saturday/Sunday (UTC) overlaps the open interval ``(start, end)``.

    Deterministic weekend/holiday-gap guard: a Friday signal reaching a Monday
    entry candle crosses a weekend and is refused (Issue #49). Uses UTC calendar
    days -- an intentional simplification for v1 (no per-market session table).
    """
    if end <= start:
        return False
    if (end - start) > pd.Timedelta(days=30):
        return True
    day = start.normalize()
    while day <= end:
        if day.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
            day_start = day
            day_end = day + pd.Timedelta(days=1)
            if day_end > start and day_start < end:
                return True
        day += pd.Timedelta(days=1)
    return False


def _confirmed_after(candles: pd.DataFrame, signal_close: pd.Timestamp) -> list[tuple[pd.Timestamp, pd.Series]]:
    """Return (timestamp, row) pairs whose candle open is at/after ``signal_close``, ascending."""
    pairs: list[tuple[pd.Timestamp, pd.Series]] = []
    for timestamp, row in candles.iterrows():
        open_time = parse_timestamp_utc(timestamp)
        if open_time >= signal_close:
            pairs.append((open_time, row))
    pairs.sort(key=lambda item: item[0])
    return pairs


def resolve_entry(
    *,
    side: str,
    timeframe: str,
    signal_candle_utc: str,
    entry_deadline_utc: str,
    candles: pd.DataFrame,
    profile: "profiles.ExecutionProfile",
    now_utc,
) -> EntryOutcome:
    """Decide a pending position's fate: open, expired, or keep waiting.

    ``candles`` must be the CONFIRMED candles for this symbol/timeframe (the
    runner filters by ``now_utc`` before calling). ``profile`` is reconstructed
    from the position's stored snapshot, so the decision is immune to later
    config changes.
    """
    if side not in models.KNOWN_SIDES:
        raise ValueError(f"side must be long/short, got {side!r}")
    _validate_columns(candles)

    signal_close = candle_close_time(signal_candle_utc, timeframe)
    deadline = parse_timestamp_utc(entry_deadline_utc)
    now = parse_timestamp_utc(now_utc)

    candidates = _confirmed_after(candles, signal_close) if candles is not None and len(candles) else []
    if not candidates:
        # No tradable confirmed candle yet. Expire only once the deadline passes.
        if now >= deadline:
            return EntryOutcome(ENTRY_ACTION_EXPIRED, terminal_reason=models.REASON_ENTRY_TIMEOUT)
        return EntryOutcome(ENTRY_ACTION_PENDING)

    entry_time, entry_row = candidates[0]
    if _weekend_between(signal_close, entry_time):
        return EntryOutcome(ENTRY_ACTION_EXPIRED, terminal_reason=models.REASON_ENTRY_TIMEOUT)
    if entry_time > deadline:
        return EntryOutcome(ENTRY_ACTION_EXPIRED, terminal_reason=models.REASON_ENTRY_TIMEOUT)

    exec_model = execution.ExecutionModel(spread=profile.spread, slippage=profile.slippage)
    raw_open = float(entry_row["open"])
    entry_price = exec_model.adjust_entry_price(raw_open, side)
    stop_distance = entry_price * profile.fallback_stop_pct
    tp_distance = stop_distance * profile.risk_reward
    if side == LONG:
        stop_loss_price = entry_price - stop_distance
        take_profit_price = entry_price + tp_distance
    else:
        stop_loss_price = entry_price + stop_distance
        take_profit_price = entry_price - tp_distance

    max_hold_deadline = entry_time + profile.max_hold_bars * timeframe_to_timedelta(timeframe)
    return EntryOutcome(
        action=ENTRY_ACTION_OPEN,
        entry_price=entry_price,
        entry_time_utc=_iso_z(entry_time),
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        max_hold_deadline_utc=_iso_z(max_hold_deadline),
    )


@dataclass(frozen=True)
class OpenPositionView:
    """DB-free view of an open position the Close walk needs (from stored fields)."""

    side: str
    timeframe: str
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    max_hold_deadline_utc: str
    same_candle_priority: str
    size: float
    spread: float
    slippage: float
    commission: float


def _validate_columns(candles: pd.DataFrame) -> None:
    if candles is None or len(candles) == 0:
        return
    missing = [c for c in _REQUIRED_CANDLE_COLUMNS if c not in candles.columns]
    if missing:
        raise ValueError(f"candles is missing required column(s): {missing}.")


def _compute_pnl(
    position: "OpenPositionView", raw_close: float, effective_close: float
) -> tuple[float, float, float | None]:
    """Return (gross_pnl, net_pnl, r_multiple) with each cost applied exactly once.

    ``position.entry_price`` is the *effective* (cost-adjusted) entry fill; the
    *raw* entry is recovered from it by removing the one entry-side cost, so the
    raw prices drive ``gross_pnl`` and the effective prices drive ``net_pnl`` --
    spread/slippage are never double-counted (they are embedded in the effective
    fills, not additionally subtracted from ``net_pnl``). ``commission`` is the
    only cost subtracted explicitly. ``r_multiple`` is net-based per unit of
    initial risk; ``None`` when that risk is non-positive/non-finite.
    """
    side = position.side
    size = position.size
    cost = position.spread + position.slippage
    effective_entry = position.entry_price
    raw_entry = effective_entry - cost if side == LONG else effective_entry + cost

    if side == LONG:
        gross_pnl = (raw_close - raw_entry) * size
        net_pnl = (effective_close - effective_entry) * size - position.commission
    else:
        gross_pnl = (raw_entry - raw_close) * size
        net_pnl = (effective_entry - effective_close) * size - position.commission

    initial_risk = abs(effective_entry - position.stop_loss_price)
    r_multiple = None
    if initial_risk > 0 and size > 0:
        r_multiple = net_pnl / (initial_risk * size)
    return gross_pnl, net_pnl, r_multiple


def evaluate_close(position: OpenPositionView, candles: pd.DataFrame) -> CloseWalkResult:
    """Walk ``candles`` (entry candle inclusive on first pass, else post-watermark).

    Returns a :class:`CloseWalkResult`. On a close, ``new_watermark`` is the close
    candle's open timestamp and the effective (cost-adjusted) close price / reason
    / pnl are filled. With no close, ``closed=False`` and ``new_watermark`` is the
    last evaluated candle (or ``None`` if no candles were supplied).
    """
    if position.side not in models.KNOWN_SIDES:
        raise ValueError(f"position.side must be long/short, got {position.side!r}")
    if position.same_candle_priority not in profiles.KNOWN_SAME_CANDLE_PRIORITIES:
        raise ValueError(f"unknown same_candle_priority {position.same_candle_priority!r}")
    _validate_columns(candles)
    if candles is None or len(candles) == 0:
        return CloseWalkResult(closed=False, new_watermark=None)

    exec_model = execution.ExecutionModel(spread=position.spread, slippage=position.slippage)
    side = position.side
    stop_price = position.stop_loss_price
    target_price = position.take_profit_price
    max_hold_deadline = parse_timestamp_utc(position.max_hold_deadline_utc)

    def _close(raw_price: float, reason: str, timestamp) -> CloseWalkResult:
        effective = float(exec_model.adjust_close_price(raw_price, side))
        gross_pnl, net_pnl, r_multiple = _compute_pnl(position, float(raw_price), effective)
        return CloseWalkResult(
            closed=True,
            new_watermark=_iso_z(timestamp),
            close_price=effective,
            close_time_utc=_iso_z(timestamp),
            terminal_reason=reason,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            r_multiple=r_multiple,
        )

    last_ts = None
    for timestamp, row in candles.iterrows():
        last_ts = timestamp
        open_ = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        candle_close = candle_close_time(timestamp, position.timeframe)

        # 1. gap (candle open). open is a single price: it cannot cross SL and TP
        #    at once while SL < entry < TP holds.
        if side == LONG:
            if open_ <= stop_price:
                return _close(open_, models.REASON_STOP_LOSS, timestamp)
            if open_ >= target_price:
                return _close(target_price, models.REASON_TAKE_PROFIT, timestamp)
        else:
            if open_ >= stop_price:
                return _close(open_, models.REASON_STOP_LOSS, timestamp)
            if open_ <= target_price:
                return _close(target_price, models.REASON_TAKE_PROFIT, timestamp)

        # 2. intrabar (high/low), only when gap did not close.
        if side == LONG:
            hit_stop = low <= stop_price
            hit_tp = high >= target_price
        else:
            hit_stop = high >= stop_price
            hit_tp = low <= target_price

        if hit_stop and hit_tp:
            if position.same_candle_priority == profiles.SAME_CANDLE_PRIORITY_TAKE_PROFIT:
                return _close(target_price, models.REASON_TAKE_PROFIT, timestamp)
            return _close(stop_price, models.REASON_STOP_LOSS, timestamp)
        if hit_stop:
            return _close(stop_price, models.REASON_STOP_LOSS, timestamp)
        if hit_tp:
            return _close(target_price, models.REASON_TAKE_PROFIT, timestamp)

        # 3. max hold, only when 1-2 did not close (a price hit always wins).
        if candle_close >= max_hold_deadline:
            return _close(close, models.REASON_MAX_HOLDING_TIME, timestamp)

    return CloseWalkResult(closed=False, new_watermark=_iso_z(last_ts))
