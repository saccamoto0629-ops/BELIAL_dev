"""Issue #44: XAUUSD / 1h squeeze-episode Bollinger close-break strategy (Plan A).

*** PAPER-ONLY BACKTEST EVALUATION. NOT AN EXECUTION LAYER. ***

This module implements the single strategy chosen in Issue #43
(``docs/development/reports/issue43_squeeze_breakout_strategy.md``): a
squeeze-episode breakout that is *entirely separate* from the existing
paper-signal ``trend_follow`` candidate path. It never touches
``exit_policy.py`` (so the trend_follow path cannot regress), never places
an order, and reads only already-persisted historical candles.

Flow (see the Issue #43 pseudo-code):

    consecutive Market-State ``squeeze`` bars form one *episode*
      -> the episode ends on the first confirmed non-squeeze bar ``t``
      -> if ``close[t]`` closed beyond the current Bollinger band
         (``close >= bb_upper`` long, ``close <= bb_lower`` short) it is a
         Trigger; a wick-only poke is not
      -> Entry at ``close[t]`` corrected by the Execution Model
      -> Stop = Trigger bar low (long) / high (short); target = 2R from the
         corrected entry
      -> exit walk from ``t+1``: gap Stop -> gap TP -> intrabar SL/TP
         (SL wins a same-bar tie) -> 24-bar time exit at close.

Future-leakage guarantees (Issue #44):

- Each bar's Market State is classified point-in-time over
  ``indicators.iloc[:position+1]`` only -- never a future row.
- ``state[t-1]`` is the previous *confirmed, ready* bar's already-computed
  state; the episode state machine only ever advances on the bars it has
  already seen.
- The Trigger bar's own high/low never decide its own exit: the exit walk
  starts at ``t+1``.
- Warm-up bars seed indicators and may advance the episode state machine,
  but never produce a saved signal -- only Trigger bars inside
  ``[validation_start, validation_end]`` do.
- Bars after ``validation_end`` are dropped before any evaluation, so no
  future candle is ever read.
"""

from __future__ import annotations

import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
MARKET_WATCH_DIR = REPO_ROOT / "market-watch"
PAPER_SIGNAL_DIR = REPO_ROOT / "paper-signal"

for _dir in (BASE_DIR, MARKET_WATCH_DIR, PAPER_SIGNAL_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

from indicators import calculate_indicators  # noqa: E402  (market-watch)
from market_state import (  # noqa: E402  (market-watch)
    DEFAULT_SQUEEZE_LOOKBACK,
    DEFAULT_SQUEEZE_PERCENTILE,
    classify_market_state,
)
import warmup  # noqa: E402  (backtest: readiness contract + warmup/validation split)
import exit_policy  # noqa: E402  (paper-signal: reuse status/reason vocabulary + result dataclass)

logger = logging.getLogger("backtest.squeeze_breakout")

# --- Strategy identity (Issue #44 signal metadata) ---------------------------
# These three are the values persisted onto every saved backtest_signals row so
# a squeeze-breakout run is never confused with a trend_follow (macd alignment)
# one in storage, summaries, or by_signal_type breakdowns.
STRATEGY_PROFILE = "xauusd_1h_squeeze_breakout_v1"
SIGNAL_TYPE = "squeeze_breakout"
TRIGGER_TYPE = "bollinger_close_break"
# alert_source describes where the setup came from. Unlike the trend_follow
# path's "composite", this setup is derived purely from the Market State
# squeeze episode, so it carries its own label.
ALERT_SOURCE = "market_state_squeeze"

# Named so a saved signal's exit_policy column says which composite produced its
# close fields; it is a free-text label, not a selectable exit_policy.KNOWN_POLICIES.
EXIT_POLICY_LABEL = "squeeze_breakout_2r_time_exit"

# Pre-fixed strategy parameters (Issue #43: not tuned on Issue #42 PnL).
SQUEEZE_STATE = "squeeze"
RISK_REWARD = 2.0
TIME_EXIT_BARS = 24

LONG = exit_policy.LONG
SHORT = exit_policy.SHORT

# Columns the exit walk needs. Unlike fixed_rr (high/low/close) it also reads
# ``open`` to detect an opening gap through the stop/target.
_REQUIRED_EXIT_COLUMNS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class SqueezeBreakoutSignal:
    """One squeeze-episode Trigger that produced a paper entry, plus its exit.

    All prices are the raw levels the strategy decided; the runner applies the
    Execution Model close-side correction to ``exit_result.close_price`` exactly
    as it already does for the trend_follow path. ``entry_price`` is already the
    Execution-Model-corrected entry (the stop/target are computed relative to
    it), matching how the runner hands a corrected entry to the Exit Policy.
    """

    signal_time: str
    direction: str
    entry_price: float
    stop_price: float
    target_price: float
    market_state: str
    market_state_confidence: float
    exit_result: "exit_policy.ExitPolicyResult"


def _track_excursion(
    direction: str, entry_price: float, high: float, low: float, mfe: float, mae: float
) -> tuple[float, float]:
    """Running max-favorable / max-adverse excursion, signed, relative to entry.

    Kept byte-identical to exit_policy._track_excursion() so squeeze-breakout
    MFE/MAE mean exactly what the trend_follow path's do, without importing a
    private helper across modules.
    """
    if direction == LONG:
        favorable = high - entry_price
        adverse = low - entry_price
    else:
        favorable = entry_price - low
        adverse = entry_price - high
    return max(mfe, favorable), min(mae, adverse)


def _format_time(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def evaluate_squeeze_exit(
    direction: str,
    entry_price: float,
    stop_price: float,
    target_price: float,
    candles: pd.DataFrame,
    *,
    time_exit_bars: int = TIME_EXIT_BARS,
) -> "exit_policy.ExitPolicyResult":
    """Walk *candles* (already only the bars strictly after entry) for the exit.

    Per Issue #43/#44, each bar is evaluated in this fixed order:

      1. gap Stop  -- long ``open <= stop`` / short ``open >= stop``: close at
         the raw ``open`` (a gap through the stop fills at the worse open).
      2. gap TP    -- long ``open >= target`` / short ``open <= target``: close
         at the raw ``target`` (conservative: no favorable price improvement,
         PnL is fixed at exactly 2R). This is the Issue #43-authoritative
         resolution of the gap-TP fill price.
      3. intrabar SL/TP via this bar's high/low.
      4. same-bar SL/TP tie -> Stop wins (conservative).
      5. if still open on the ``time_exit_bars``-th bar, close at that bar's
         raw ``close`` (time exit) -- but only *after* steps 1-4 were evaluated
         on that same bar, so a 24th-bar SL/TP still takes precedence.

    Returns an ``ExitPolicyResult`` with the *raw* close price (the runner
    applies the Execution Model close-side correction), or an "open"/no_close
    result when the supplied candles run out before any exit fires. Stop closes
    report ``loss``; TP closes report ``win``; a time exit reports ``closed``
    with reason ``time_expired`` (win/loss is judged from pnl sign downstream).
    """
    if candles is None or candles.empty:
        return exit_policy._no_close(EXIT_POLICY_LABEL)

    missing = [column for column in _REQUIRED_EXIT_COLUMNS if column not in candles.columns]
    if missing:
        raise ValueError(f"candles is missing required column(s) for squeeze breakout exit: {missing}.")

    mfe = 0.0
    mae = 0.0
    for position, (timestamp, row) in enumerate(candles.iterrows(), start=1):
        open_ = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        mfe, mae = _track_excursion(direction, entry_price, high, low, mfe, mae)

        # 1. gap Stop (checked before gap TP: a bar that gaps through the stop
        #    is a loss regardless of where else it reaches).
        gap_stop = open_ <= stop_price if direction == LONG else open_ >= stop_price
        if gap_stop:
            return exit_policy.ExitPolicyResult(
                status=exit_policy.STATUS_LOSS,
                close_price=open_,
                close_time=_format_time(timestamp),
                close_reason=exit_policy.REASON_STOP_LOSS,
                exit_policy=EXIT_POLICY_LABEL,
                mfe=mfe,
                mae=mae,
            )

        # 2. gap TP -- conservative fill at the target (no favorable improvement).
        gap_tp = open_ >= target_price if direction == LONG else open_ <= target_price
        if gap_tp:
            return exit_policy.ExitPolicyResult(
                status=exit_policy.STATUS_WIN,
                close_price=target_price,
                close_time=_format_time(timestamp),
                close_reason=exit_policy.REASON_TAKE_PROFIT,
                exit_policy=EXIT_POLICY_LABEL,
                mfe=mfe,
                mae=mae,
            )

        # 3. intrabar SL/TP.
        if direction == LONG:
            hit_stop = low <= stop_price
            hit_tp = high >= target_price
        else:
            hit_stop = high >= stop_price
            hit_tp = low <= target_price

        # 4. same-bar tie -> Stop wins.
        if hit_stop:
            return exit_policy.ExitPolicyResult(
                status=exit_policy.STATUS_LOSS,
                close_price=stop_price,
                close_time=_format_time(timestamp),
                close_reason=exit_policy.REASON_STOP_LOSS,
                exit_policy=EXIT_POLICY_LABEL,
                mfe=mfe,
                mae=mae,
            )
        if hit_tp:
            return exit_policy.ExitPolicyResult(
                status=exit_policy.STATUS_WIN,
                close_price=target_price,
                close_time=_format_time(timestamp),
                close_reason=exit_policy.REASON_TAKE_PROFIT,
                exit_policy=EXIT_POLICY_LABEL,
                mfe=mfe,
                mae=mae,
            )

        # 5. time exit -- only once steps 1-4 did not fire on this same bar.
        if position >= time_exit_bars:
            return exit_policy.ExitPolicyResult(
                status=exit_policy.STATUS_CLOSED,
                close_price=close,
                close_time=_format_time(timestamp),
                close_reason=exit_policy.REASON_TIME_EXPIRED,
                exit_policy=EXIT_POLICY_LABEL,
                mfe=mfe,
                mae=mae,
            )

    return exit_policy._no_close(EXIT_POLICY_LABEL, mfe=mfe, mae=mae)


def _is_finite(*values: float) -> bool:
    return all(isinstance(value, (int, float)) and math.isfinite(value) for value in values)


def evaluate_squeeze_breakout_signals(
    candles: pd.DataFrame,
    validation_start,
    validation_end,
    *,
    symbol: str,
    timeframe: str,
    warmup_bars: int = warmup.DEFAULT_WARMUP_BARS,
    exec_model=None,
    time_exit_bars: int = TIME_EXIT_BARS,
    risk_reward: float = RISK_REWARD,
    squeeze_lookback: int = DEFAULT_SQUEEZE_LOOKBACK,
    squeeze_percentile: float = DEFAULT_SQUEEZE_PERCENTILE,
    allowed_market_states: frozenset[str] | None = None,
    stats: dict | None = None,
) -> list[SqueezeBreakoutSignal]:
    """Run the squeeze-episode breakout state machine over *candles*.

    *candles* must be indexed by timestamp (ascending) and include enough
    history before ``validation_start`` to warm the indicators (see
    ``warmup.split_warmup_and_validation``). Returns one
    ``SqueezeBreakoutSignal`` per adopted Trigger whose episode-end bar falls
    inside ``[validation_start, validation_end]``, in chronological order.

    The state machine is single-position by construction: after a Trigger opens
    a position, every bar up to and including the exit bar is skipped for
    episode arming, and a *new* squeeze episode must form before another entry
    (Issue #44 no-reentry / new-episode rule). ``exec_model`` (an
    ``execution_model.ExecutionModel``) corrects the entry fill; when None, a
    no-op zero-cost model is used.

    ``squeeze_lookback`` / ``squeeze_percentile`` (Issue #54 / PR #62) are
    forwarded to ``classify_market_state()`` and parameterize only the
    squeeze-episode *detection window*; the episode state transition, trigger
    condition, and fill judgement stay fixed here. Defaults are market_state's
    own, so omitting them is byte-identical to the pre-#54 behaviour.

    ``allowed_market_states`` (Issue #63) is the market-state entry gate: when
    not None, an entry is adopted only if the *Trigger bar's* classified state
    (the same ``classify_market_state()`` label persisted as
    ``SqueezeBreakoutSignal.market_state`` -- ``unknown`` included, exactly the
    Issue #39 ``--allowed-market-states`` vocabulary) is a member of the set.
    The gate is evaluated only on a bar whose close actually fired the
    breakout trigger, never on the squeeze bars forming the episode. A
    disallowed Trigger bar produces no entry, opens no position, and does NOT
    consume the episode: it stays armed, so a later bar of the same episode
    that fires the trigger again in an allowed state can still enter. The
    episode is consumed as before by the first non-squeeze bar that does not
    fire a trigger at all, or by an adopted entry. ``None`` (the default) means
    no gate: byte-identical to the pre-#63 behaviour.

    ``stats``, when given a dict, receives the gate's suppression count under
    ``"suppressed_market_state"`` (how many fired triggers the gate dropped).
    """
    if exec_model is None:
        import execution_model as execution

        exec_model = execution.ExecutionModel()

    # Future-leakage guard: never look at a bar after validation_end, not even
    # for indicator calculation or the exit walk. The runner already loads
    # candles only up to validation_end, so this is a defensive no-op there, but
    # it makes a direct caller (and every unit test) safe too.
    end = warmup._align_timestamp(candles.index, validation_end)
    candles = candles[candles.index <= end]

    warmup_frame, validation_frame = warmup.split_warmup_and_validation(
        candles, validation_start, validation_end
    )
    if validation_frame.empty:
        return []
    # Mirror the runner/warmup contract: an insufficient warm-up allocation
    # means no bar can be responsibly classified, so no signal is produced.
    if len(warmup_frame) < warmup_bars:
        logger.debug(
            "Insufficient warm-up for squeeze breakout %s %s: have %s bars, need %s.",
            symbol, timeframe, len(warmup_frame), warmup_bars,
        )
        return []

    validation_index = validation_frame.index
    # calculate_indicators over the full (warm-up + validation, already
    # <= validation_end) frame once; each bar is then classified point-in-time.
    indicators = calculate_indicators(candles)

    signals: list[SqueezeBreakoutSignal] = []
    episode_active = False
    # Bars at positional index <= resume_after_pos are "inside an open position"
    # and never arm an episode; -1 means no position is currently open.
    resume_after_pos = -1

    for position, (timestamp, row) in enumerate(indicators.iterrows()):
        if position <= resume_after_pos:
            # Held position spans this bar: do not arm a new episode here.
            continue

        # Only fully-ready confirmed bars participate in the state machine; a
        # not-ready bar (still warming an indicator) cannot be classified, so it
        # is skipped without disturbing episode_active.
        readiness = warmup.check_indicator_readiness(row, position + 1)
        if not readiness.is_ready:
            continue

        state_result = classify_market_state(
            indicators.iloc[: position + 1],
            squeeze_lookback=squeeze_lookback,
            squeeze_percentile=squeeze_percentile,
        )
        state = state_result["market_state"]

        if state == SQUEEZE_STATE:
            # Start (or continue) the current episode; a squeeze bar never
            # triggers an entry itself.
            episode_active = True
            continue

        if not episode_active:
            # No episode in progress and this bar is not squeeze: nothing to do.
            continue

        # state != squeeze AND episode_active -> this is the episode-end bar t,
        # the sole Trigger-evaluation bar for this episode. Its predecessor
        # confirmed-ready bar was squeeze (episode_active could only have been
        # set by a squeeze bar and would have been cleared by any earlier
        # non-squeeze ready bar), so state[t-1] == squeeze holds.
        # The episode is consumed here whether or not it triggers -- with one
        # Issue #63 exception below: a fired trigger whose bar state the
        # market-state gate disallows re-arms the episode instead.
        episode_active = False

        in_validation = timestamp in validation_index
        if not in_validation:
            # Episode ended outside the validation window (e.g. during warm-up):
            # consumed, but never a saved signal.
            continue

        close_t = float(row["close"])
        bb_upper = float(row["bb_upper"])
        bb_lower = float(row["bb_lower"])
        long_trigger = close_t >= bb_upper
        short_trigger = close_t <= bb_lower
        # XOR: no break, or (degenerate data) both sides -> no entry.
        if long_trigger == short_trigger:
            continue

        # Issue #63: the market-state gate applies to the bar the trigger just
        # fired on (this bar's own classified state -- the value persisted as
        # signal.market_state). A disallowed state is neither an entry nor a
        # consumed episode: re-arm it, so a later trigger bar of the same
        # episode can still enter in an allowed state. Bars that never fire a
        # trigger are untouched by the gate (the episode was already consumed
        # above, exactly as without a gate).
        if allowed_market_states is not None and state not in allowed_market_states:
            if stats is not None:
                stats["suppressed_market_state"] = stats.get("suppressed_market_state", 0) + 1
            episode_active = True
            continue

        direction = LONG if long_trigger else SHORT
        raw_entry = close_t
        entry_price = exec_model.adjust_entry_price(raw_entry, direction)
        stop_price = float(row["low"]) if direction == LONG else float(row["high"])

        if not _is_finite(entry_price, stop_price):
            continue
        stop_valid = stop_price < entry_price if direction == LONG else stop_price > entry_price
        risk = (entry_price - stop_price) if direction == LONG else (stop_price - entry_price)
        if not stop_valid or risk <= 0:
            continue

        target_price = (
            entry_price + risk_reward * risk if direction == LONG else entry_price - risk_reward * risk
        )

        # Exit walk starts at t+1: the Trigger bar's own high/low never decide
        # its own exit.
        future_candles = candles.iloc[position + 1 :]
        exit_result = evaluate_squeeze_exit(
            direction,
            entry_price,
            stop_price,
            target_price,
            future_candles,
            time_exit_bars=time_exit_bars,
        )

        signals.append(
            SqueezeBreakoutSignal(
                signal_time=_format_time(timestamp),
                direction=direction,
                entry_price=entry_price,
                stop_price=stop_price,
                target_price=target_price,
                market_state=state,
                market_state_confidence=float(state_result["confidence"]),
                exit_result=exit_result,
            )
        )

        # Single-position: block episode arming through the exit bar, then
        # require a brand-new squeeze episode before the next entry.
        if exit_result.close_time is None:
            # Never closed within the loaded window: held to the end, nothing
            # more can arm.
            resume_after_pos = len(indicators) - 1
        else:
            exit_pos = candles.index.get_loc(pd.Timestamp(exit_result.close_time))
            resume_after_pos = exit_pos

    return signals
