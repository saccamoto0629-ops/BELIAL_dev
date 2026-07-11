"""Warm-up range and indicator-readiness checks for Backtest.

*** PAPER-ONLY BACKTEST EVALUATION. NOT AN EXECUTION LAYER. ***

This module answers two questions for a Backtest run over a validation
window [validation_start, validation_end]:

1. How many candles *before* validation_start are needed so RSI14 / MACD /
   Bollinger Bands / MA have enough history to be meaningful by the time
   the validation window begins ("warm-up")?
2. For any single bar inside the validation window, are RSI14 / MACD /
   Bollinger Bands / MA all actually usable (not NaN, and past their
   minimum bar count)? If not, Signal generation for that bar is skipped,
   with a reason.

Warm-up candles are used *only* to seed indicator calculations (rolling
windows, EMAs, RSI's running averages, computed via
market-watch/indicators.calculate_indicators() -- this module does not
reimplement any indicator math). They are never included in Backtest
results/signal aggregation: evaluate_backtest_bars() only ever returns one
BarEvaluation per bar at or after validation_start.

This module never places an order, connects to a broker, sizes a
position, or decides an entry/exit/stop-loss/take-profit -- it only
decides whether there is enough historical data to responsibly ask
paper-signal's existing, unmodified generate_signal_candidates() for a
hypothesis at all. Signal generation rules themselves live entirely in
paper-signal/signal_generator.py and are not touched here.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
MARKET_WATCH_DIR = REPO_ROOT / "market-watch"
PAPER_SIGNAL_DIR = REPO_ROOT / "paper-signal"

for _dir in (MARKET_WATCH_DIR, PAPER_SIGNAL_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

from indicators import calculate_indicators, classify_bb_state, classify_trend  # noqa: E402  (market-watch)
# Issue #28: Backtest resolves freshness / bias_weight through the SAME shared
# rule as Live (chart_snapshot uses these too), never a hardcoded "Fresh"/1.0.
# The ``freshness`` module is dependency-light (pandas + datetime only), so this
# does not drag Live's rendering/upload dependencies into the Backtest path.
from freshness import (  # noqa: E402  (market-watch)
    resolve_freshness,
    select_confirmed_candles,
    timeframe_to_timedelta,
)
from signal_generator import DEFAULT_CONFIDENCE_THRESHOLD, generate_signal_candidates  # noqa: E402  (paper-signal)
from signal_models import create_signal_context  # noqa: E402  (paper-signal)

logger = logging.getLogger("backtest.warmup")

# Minimum bar counts each indicator needs before its value is meaningful.
# sma75/sma20/ema20/Bollinger Bands and RSI14 are governed by
# indicators.py's own rolling-window `min_periods` (or, for RSI14, its
# manual Wilder warm-up loop), which already yields NaN before this many
# bars accumulate -- verified directly against calculate_indicators().
# MACD is the one exception: pandas' `ewm()` never produces NaN, so its
# readiness can only be judged by bar count, not by inspecting the value.
RSI_MIN_BARS = 15
MACD_MIN_BARS = 35  # EMA(26) slow leg + signal EMA(9)
BOLLINGER_MIN_BARS = 20
MA_MIN_BARS = 75  # dominated by SMA75, the slowest MA this system uses

DEFAULT_WARMUP_BARS = max(RSI_MIN_BARS, MACD_MIN_BARS, BOLLINGER_MIN_BARS, MA_MIN_BARS)

REASON_RSI_NOT_READY = "rsi14_not_ready"
REASON_MACD_NOT_READY = "macd_not_ready"
REASON_BOLLINGER_NOT_READY = "bollinger_bands_not_ready"
REASON_MA_NOT_READY = "ma_not_ready"
REASON_WARMUP_INSUFFICIENT = "warmup_insufficient"


@dataclass(frozen=True)
class IndicatorReadiness:
    """Whether one bar's RSI14/MACD/Bollinger Bands/MA are all usable.

    ``reasons`` is empty exactly when ``is_ready`` is True.
    """

    is_ready: bool
    reasons: tuple[str, ...] = ()


def check_indicator_readiness(row: pd.Series, position: int) -> IndicatorReadiness:
    """Check one row of indicators.calculate_indicators() output for readiness.

    ``position`` is the 1-indexed bar count since the start of the candle
    series ``row`` came from (i.e. how many candles, including this one,
    have been seen so far) -- used for MACD's bar-count-only check, since
    MACD never goes NaN through pandas' ewm().
    """
    reasons: list[str] = []

    if pd.isna(row.get("rsi14")):
        reasons.append(REASON_RSI_NOT_READY)

    if position < MACD_MIN_BARS or pd.isna(row.get("macd")) or pd.isna(row.get("macd_signal")):
        reasons.append(REASON_MACD_NOT_READY)

    if pd.isna(row.get("bb_upper")) or pd.isna(row.get("bb_lower")) or pd.isna(row.get("bb_position")):
        reasons.append(REASON_BOLLINGER_NOT_READY)

    if pd.isna(row.get("sma20")) or pd.isna(row.get("sma75")) or pd.isna(row.get("ema20")):
        reasons.append(REASON_MA_NOT_READY)

    return IndicatorReadiness(is_ready=not reasons, reasons=tuple(reasons))


@dataclass(frozen=True)
class BarEvaluation:
    """One validation-period bar's readiness and (if ready) generated candidates.

    Warm-up bars never produce a BarEvaluation -- only bars at/after the
    validation window's start do (see evaluate_backtest_bars()), so
    warm-up bars can never appear in Backtest results.
    """

    timestamp: str
    symbol: str
    is_ready: bool
    skip_reasons: tuple[str, ...] = ()
    candidates: tuple[dict, ...] = ()


def _align_timestamp(reference_index: pd.DatetimeIndex, timestamp) -> pd.Timestamp:
    """Match *timestamp*'s tz-awareness to *reference_index* so comparisons don't raise."""
    timestamp = pd.Timestamp(timestamp)
    if reference_index.tz is not None:
        return timestamp.tz_localize(reference_index.tz) if timestamp.tz is None else timestamp.tz_convert(reference_index.tz)
    return timestamp.tz_localize(None) if timestamp.tz is not None else timestamp


def split_warmup_and_validation(
    candles: pd.DataFrame, validation_start, validation_end=None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split *candles* into (warmup_frame, validation_frame) at validation_start.

    ``warmup_frame`` holds only rows strictly before ``validation_start``.
    ``validation_frame`` holds rows with timestamp >= validation_start (and
    <= validation_end if given). Rows after ``validation_end`` belong to
    neither: they must never be counted as warm-up (that would let future
    data mask an actually-insufficient pre-start warm-up allocation) and
    they are not part of this validation window's results either.
    """
    start = _align_timestamp(candles.index, validation_start)
    warmup_mask = candles.index < start
    validation_mask = candles.index >= start
    if validation_end is not None:
        end = _align_timestamp(candles.index, validation_end)
        validation_mask &= candles.index <= end

    return candles[warmup_mask], candles[validation_mask]


def _format_timestamp(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _build_snapshot(
    symbol: str,
    timeframe: str,
    row: pd.Series,
    *,
    candle_timestamp,
    evaluation_time,
) -> dict:
    """Build a minimal Chart-Snapshot-shaped dict for one historical bar.

    Mirrors the fields market-watch/chart_snapshot.build_indicator_snapshot()
    would produce for "the latest bar", but for an arbitrary historical
    row -- reusing the same classify_trend()/classify_bb_state() rules AND the
    same freshness/bias_weight rule (freshness.resolve_freshness), not
    reimplementing any of them.

    Issue #28: freshness / bias_weight are NOT hardcoded. They are derived from
    this bar's ``candle_timestamp`` observed at ``evaluation_time`` (the bar's
    replay evaluation time) through exactly the shared rule Live uses -- so the
    same (candle_timestamp, evaluation_time) yields the same freshness on either
    path. When the market-closed / stale rule marks this bar non-Fresh, trend /
    state are reported as "Stale" just like Live does, and the shared
    generate_signal_candidates() then declines to emit a candidate.
    """
    close = float(row["close"])
    sma20 = float(row["sma20"]) if pd.notna(row.get("sma20")) else None
    sma75 = float(row["sma75"]) if pd.notna(row.get("sma75")) else None
    bb_position = float(row["bb_position"]) if pd.notna(row.get("bb_position")) else None
    freshness, _age_seconds, bias_weight = resolve_freshness(candle_timestamp, evaluation_time=evaluation_time)
    is_stale = freshness == "Stale"
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "trend": "Stale" if is_stale else classify_trend(close, sma20, sma75),
        "state": "Stale" if is_stale else classify_bb_state(bb_position),
        "freshness": freshness,
        "bias_weight": bias_weight,
        "macd_state": str(row.get("macd_state") or "Neutral"),
    }


def evaluate_backtest_bars(
    candles: pd.DataFrame,
    validation_start,
    validation_end=None,
    *,
    symbol: str,
    timeframe: str,
    bias_result: dict,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    evaluation_time=None,
) -> list[BarEvaluation]:
    """Walk the validation-period bars in *candles*, generating candidates only when ready.

    *candles* should already include at least ``warmup_bars`` rows before
    ``validation_start`` (see ``split_warmup_and_validation``); those rows
    seed indicator calculations only -- this function never returns a
    BarEvaluation for a warm-up bar.

    For each validation-period bar: if the warm-up allocation was
    sufficient *and* RSI14/MACD/Bollinger Bands/MA are all ready,
    ``generate_signal_candidates()`` (paper-signal's existing, unmodified
    rule set -- see signal_generator.build_candidates()) is called with
    *bias_result* and a single-symbol snapshot built from that bar.
    Otherwise the bar is skipped and ``skip_reasons`` records why (e.g.
    "warmup_insufficient", "ma_not_ready"), logged at DEBUG level too.

    Issue #28: each replayed bar is evaluated at its own **close time**
    (``candle open + timeframe``) -- the instant that bar becomes the latest
    confirmed candle, i.e. exactly when a Live run at that wall-clock would
    first see it. freshness / bias_weight are then derived from that per-bar
    evaluation time through the same shared rule Live uses.

    Pass an explicit ``evaluation_time`` to instead observe the window as of a
    single fixed "now" (used to reproduce Stale / market-closed cases and to
    mirror a Live run at one wall-clock instant). In that fixed-time mode a
    still-forming validation bar -- one whose close (``candle open + timeframe``)
    is after ``evaluation_time`` -- is dropped BEFORE evaluation via the same
    ``select_confirmed_candles()`` Live uses, so it never yields a
    ``BarEvaluation`` or a Candidate and no future candle is ever read. This
    makes the Backtest confirmed-candle set identical to Live's for the same
    ``evaluation_time``. When ``evaluation_time`` is None (historical replay,
    the runner default) every validation bar is evaluated at its own close, so
    each is confirmed against itself and this filter is a no-op -- the existing
    replay behaviour is unchanged.
    """
    try:
        timeframe_delta = timeframe_to_timedelta(timeframe)
    except ValueError:
        timeframe_delta = pd.Timedelta(0)

    warmup_frame, validation_frame = split_warmup_and_validation(candles, validation_start, validation_end)

    # Fixed-time mode: restrict the evaluatable validation bars to those already
    # CLOSED at evaluation_time, reusing Live's shared confirmed-candle rule
    # (never a backtest-private reimplementation). None -> historical replay,
    # every validation bar stays evaluatable (confirmed against its own close).
    if evaluation_time is not None:
        try:
            evaluatable_index = select_confirmed_candles(
                validation_frame, timeframe=timeframe, evaluation_time=evaluation_time
            ).index
        except ValueError:
            evaluatable_index = validation_frame.index
    else:
        evaluatable_index = validation_frame.index
    warmup_insufficient = len(warmup_frame) < warmup_bars
    if warmup_insufficient:
        logger.debug(
            "Insufficient warm-up for %s %s: have %s bars, need %s.",
            symbol, timeframe, len(warmup_frame), warmup_bars,
        )

    indicators = calculate_indicators(candles)
    results: list[BarEvaluation] = []

    for position, (timestamp, row) in enumerate(indicators.iterrows(), start=1):
        if timestamp not in evaluatable_index:
            # warm-up bar (seeding only), or -- in fixed evaluation_time mode --
            # a still-forming validation bar not yet closed at evaluation_time.
            # Neither ever becomes a BarEvaluation or a Candidate.
            continue

        reasons: list[str] = [REASON_WARMUP_INSUFFICIENT] if warmup_insufficient else []
        readiness = check_indicator_readiness(row, position)
        reasons.extend(readiness.reasons)

        if reasons:
            logger.debug("Skipping %s %s at %s: %s", symbol, timeframe, timestamp, reasons)
            results.append(
                BarEvaluation(
                    timestamp=_format_timestamp(timestamp),
                    symbol=symbol,
                    is_ready=False,
                    skip_reasons=tuple(reasons),
                )
            )
            continue

        bar_evaluation_time = evaluation_time if evaluation_time is not None else timestamp + timeframe_delta
        snapshot = _build_snapshot(
            symbol,
            timeframe,
            row,
            candle_timestamp=timestamp,
            evaluation_time=bar_evaluation_time,
        )
        context = create_signal_context(bias_result, [snapshot], confidence_threshold=confidence_threshold)
        candidates = generate_signal_candidates(context)

        results.append(
            BarEvaluation(
                timestamp=_format_timestamp(timestamp),
                symbol=symbol,
                is_ready=True,
                candidates=tuple(candidates),
            )
        )

    return results
