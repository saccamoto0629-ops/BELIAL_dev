"""Pure Open / Close judgement for Paper Execution virtual positions (Issue #51).

*** PAPER EXECUTION. NOT A LIVE ORDER / BROKER LAYER. ***

This module answers two narrow, deterministic questions from candle data alone:

  1. Open  -- given a Paper Signal's side and the entry candle, at what price
              does a virtual position fill, and where are its stop / target?
  2. Close -- walking the candles after entry, which exit (stop-loss,
              take-profit, or time exit) fires first, at what price, and what
              is the resulting simulated PnL?

It is deliberately a *pure* domain layer: no SQLite, no Paper Signal / Backtest
DB, no Discord, no candle *loading*, no CLI. It receives already-fetched inputs
and returns dataclasses. Persistence lives in ``position_store.py`` and the
wiring (reading signals, loading candles, logging) lives in
``execution_runner.py`` -- so Open/Close judgement can be unit-tested in
isolation and never reaches out to any side-effecting system.

Consistency with the Backtest Engine
-------------------------------------
The gap / intrabar / same-bar-tie / time-exit ordering here is intentionally
byte-for-byte the same decision procedure as the Backtest squeeze-breakout exit
walk (``backtest/squeeze_breakout.evaluate_squeeze_exit``), and the fill-price
correction reuses the same ``backtest/execution_model.ExecutionModel``. So a
Paper Execution close never contradicts how the Backtest Engine would have
resolved the identical candles:

  1. gap Stop  -- long ``open <= stop`` / short ``open >= stop``: fills at the
                  raw ``open`` (a gap through the stop fills at the worse open),
                  then corrected once by the Execution Model.
  2. gap TP    -- long ``open >= target`` / short ``open <= target``: fills at
                  the FIXED ``target`` (conservative: no favourable price
                  improvement). The target is a fixed level and is NEVER
                  re-corrected by the Execution Model (that would drift PnL off
                  the intended R multiple -- the exact "target固定・stop固定"
                  double-application hazard Issue #51 warns about).
  3. intrabar SL/TP via this bar's high/low.
  4. same-bar SL+TP tie -> Stop wins (conservative, deterministic). There is no
     configuration knob that would let a tie optimistically pick the profit
     side; the pessimistic rule is fixed here and asserted by a test.
  5. time exit -- if still open on the ``time_exit_bars``-th bar, close at that
     bar's raw ``close``, corrected once by the Execution Model -- but only
     AFTER steps 1-4 were evaluated on that same bar, so a stop/target on the
     final bar still wins.

Spread / slippage / fee -- applied exactly once (no double counting)
--------------------------------------------------------------------
- Entry: corrected ONCE by ``ExecutionModel.adjust_entry_price``. The stop and
  target are then derived relative to that corrected entry.
- Exit:  a stop / time-exit / gap-stop fill is corrected ONCE by
  ``ExecutionModel.adjust_close_price``. A take-profit fill stays at the fixed
  target (never corrected -- see rule 2).
- ``gross_pnl`` is computed from those already-corrected executed prices, so
  spread/slippage are reflected in PnL exactly once (embedded in the fills).
  ``spread_cost`` / ``slippage_cost`` are recorded PURELY for transparency;
  they are NOT subtracted again (doing so would double-apply the cost).
- ``fee_cost`` is a flat, price-independent commission (default 0.0), the only
  cost NOT embedded in a price. ``net_pnl = gross_pnl - fee_cost``.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
BACKTEST_DIR = REPO_ROOT / "backtest"

for _dir in (BASE_DIR, BACKTEST_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import execution_model as execution  # noqa: E402  (backtest: reused fixed spread/slippage fill model)
import position_models as models  # noqa: E402

LONG = models.SIDE_LONG
SHORT = models.SIDE_SHORT

# Defaults kept aligned with the Backtest vocabulary: the fixed-percentage stop
# distance matches exit_policy's ``fallback_stop_pct`` default, the 2R target
# matches the squeeze-breakout ``RISK_REWARD``, and the 24-bar time exit matches
# the squeeze-breakout ``TIME_EXIT_BARS``.
DEFAULT_STOP_LOSS_PCT = 0.003
DEFAULT_RISK_REWARD = 2.0
DEFAULT_TIME_EXIT_BARS = 24

# Columns the Close walk needs -- ``open`` is required to detect an opening gap
# through the stop/target, exactly like the squeeze-breakout exit walk.
_REQUIRED_EXIT_COLUMNS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class OpenDecision:
    """The fill an Open would produce: executed entry + fixed stop/target."""

    side: str
    entry_price: float
    stop_loss: float
    take_profit: float
    size: float
    raw_entry_price: float


@dataclass(frozen=True)
class PositionView:
    """The minimal, DB-free view of an open position the Close walk needs."""

    side: str
    entry_price: float
    stop_loss: float
    take_profit: float
    size: float = models.DEFAULT_SIZE


@dataclass(frozen=True)
class CloseDecision:
    """A resolved exit: reason, executed exit price, time, and simulated PnL.

    ``exit_price`` is the FINAL executed fill (already Execution-Model-corrected
    where applicable); the store persists it verbatim, with no further common
    re-correction step -- so a fixed target can never be re-shifted on the way
    to storage.
    """

    close_reason: str
    exit_price: float
    closed_at: str
    gross_pnl: float
    net_pnl: float
    spread_cost: float
    slippage_cost: float
    fee_cost: float
    r_multiple: float | None


def _is_finite(*values: float) -> bool:
    return all(isinstance(value, (int, float)) and math.isfinite(value) for value in values)


def _format_time(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def decide_open(
    *,
    side: str,
    entry_candle_close: float,
    exec_model: "execution.ExecutionModel | None" = None,
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
    risk_reward: float = DEFAULT_RISK_REWARD,
    size: float = models.DEFAULT_SIZE,
) -> OpenDecision | None:
    """Decide the Open fill for a Paper Signal, or ``None`` if it cannot open.

    Returns ``None`` (an "Open condition not met" outcome, never an exception)
    when ``side`` is not long/short, the inputs are non-finite, or the derived
    risk is non-positive -- so a Neutral/degenerate signal simply produces no
    position. Otherwise:

      raw entry  = ``entry_candle_close`` (the confirmed entry candle's close)
      entry      = Execution-Model-corrected raw entry (cost applied ONCE)
      stop dist  = entry * ``stop_loss_pct``          target dist = stop * ``risk_reward``
      long : stop = entry - dist,  target = entry + rr*dist
      short: stop = entry + dist,  target = entry - rr*dist

    The stop and target are pinned relative to the *corrected* entry, so at
    Close they are fixed levels and never need re-correcting.
    """
    if side not in models.KNOWN_SIDES:
        return None
    if exec_model is None:
        exec_model = execution.ExecutionModel()
    if not _is_finite(entry_candle_close) or stop_loss_pct <= 0 or risk_reward <= 0 or size <= 0:
        return None

    raw_entry = float(entry_candle_close)
    entry_price = exec_model.adjust_entry_price(raw_entry, side)
    stop_distance = entry_price * stop_loss_pct
    tp_distance = stop_distance * risk_reward
    if side == LONG:
        stop_loss = entry_price - stop_distance
        take_profit = entry_price + tp_distance
    else:
        stop_loss = entry_price + stop_distance
        take_profit = entry_price - tp_distance

    if not _is_finite(entry_price, stop_loss, take_profit) or stop_distance <= 0:
        return None

    return OpenDecision(
        side=side,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        size=float(size),
        raw_entry_price=raw_entry,
    )


def _compute_pnl(
    position: PositionView,
    exit_price: float,
    *,
    exec_model: "execution.ExecutionModel",
    fee: float,
) -> tuple[float, float, float, float, float, float | None]:
    """Return (gross_pnl, net_pnl, spread_cost, slippage_cost, fee_cost, r_multiple).

    ``gross_pnl`` uses the executed entry/exit prices (spread/slippage already
    embedded in the fills, counted once). ``fee`` is the only cost subtracted to
    reach ``net_pnl``. ``spread_cost`` / ``slippage_cost`` are transparency-only
    records of the per-unit cost already embedded in the fills -- never
    re-subtracted. ``r_multiple`` is the signed price move in units of the
    initial risk ``|entry - stop|`` (a +2R target -> ~+2.0, a stop -> ~-1.0).
    """
    size = position.size
    if position.side == LONG:
        move_per_unit = exit_price - position.entry_price
    else:
        move_per_unit = position.entry_price - exit_price
    gross_pnl = move_per_unit * size
    fee_cost = float(fee)
    net_pnl = gross_pnl - fee_cost
    spread_cost = exec_model.spread * size
    slippage_cost = exec_model.slippage * size

    initial_risk = abs(position.entry_price - position.stop_loss)
    r_multiple = (move_per_unit / initial_risk) if initial_risk > 0 else None

    return gross_pnl, net_pnl, spread_cost, slippage_cost, fee_cost, r_multiple


def evaluate_close(
    position: PositionView,
    candles: pd.DataFrame,
    *,
    exec_model: "execution.ExecutionModel | None" = None,
    time_exit_bars: int = DEFAULT_TIME_EXIT_BARS,
    fee: float = 0.0,
) -> CloseDecision | None:
    """Walk *candles* (already only the bars strictly AFTER entry) for the exit.

    Returns a :class:`CloseDecision` for the first exit that fires (following the
    gap -> intrabar -> same-bar-tie(Stop wins) -> time-exit order documented in
    the module docstring), or ``None`` when the supplied candles run out with the
    position still open (an "Open, not yet closed" outcome, not an error).

    Raises ``ValueError`` if a non-empty frame is missing a required column, so a
    malformed candle frame fails loudly instead of silently mis-judging. An
    empty / ``None`` frame returns ``None``. Missing bars in the sequence do not
    break the walk -- ``time_exit_bars`` counts the *available* bars after entry.
    """
    if position.side not in models.KNOWN_SIDES:
        raise ValueError(f"position.side must be long/short, got {position.side!r}")
    if exec_model is None:
        exec_model = execution.ExecutionModel()
    if candles is None or len(candles) == 0:
        return None

    missing = [column for column in _REQUIRED_EXIT_COLUMNS if column not in candles.columns]
    if missing:
        raise ValueError(f"candles is missing required column(s) for the Close walk: {missing}.")

    side = position.side
    stop_price = position.stop_loss
    target_price = position.take_profit

    def _build(close_reason: str, raw_exit: float, timestamp) -> CloseDecision:
        # A take-profit fills at the FIXED target and is never re-corrected; every
        # other exit takes the Execution Model close-side correction exactly once.
        if close_reason == models.CLOSE_REASON_TAKE_PROFIT:
            exit_price = float(raw_exit)
        else:
            exit_price = float(exec_model.adjust_close_price(raw_exit, side))
        gross_pnl, net_pnl, spread_cost, slippage_cost, fee_cost, r_multiple = _compute_pnl(
            position, exit_price, exec_model=exec_model, fee=fee
        )
        return CloseDecision(
            close_reason=close_reason,
            exit_price=exit_price,
            closed_at=_format_time(timestamp),
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            spread_cost=spread_cost,
            slippage_cost=slippage_cost,
            fee_cost=fee_cost,
            r_multiple=r_multiple,
        )

    for bar_position, (timestamp, row) in enumerate(candles.iterrows(), start=1):
        open_ = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])

        # 1. gap Stop (checked before gap TP: a bar that gaps through the stop is
        #    a loss regardless of where else it reaches). Fills at the raw open.
        gap_stop = open_ <= stop_price if side == LONG else open_ >= stop_price
        if gap_stop:
            return _build(models.CLOSE_REASON_STOP_LOSS, open_, timestamp)

        # 2. gap TP -- conservative fill at the fixed target (no improvement).
        gap_tp = open_ >= target_price if side == LONG else open_ <= target_price
        if gap_tp:
            return _build(models.CLOSE_REASON_TAKE_PROFIT, target_price, timestamp)

        # 3. intrabar SL/TP via this bar's high/low.
        if side == LONG:
            hit_stop = low <= stop_price
            hit_tp = high >= target_price
        else:
            hit_stop = high >= stop_price
            hit_tp = low <= target_price

        # 4. same-bar SL+TP tie -> Stop wins (conservative, deterministic).
        if hit_stop:
            return _build(models.CLOSE_REASON_STOP_LOSS, stop_price, timestamp)
        if hit_tp:
            return _build(models.CLOSE_REASON_TAKE_PROFIT, target_price, timestamp)

        # 5. time exit -- only once steps 1-4 did not fire on this same bar.
        if bar_position >= time_exit_bars:
            return _build(models.CLOSE_REASON_TIME_EXPIRED, close, timestamp)

    return None
