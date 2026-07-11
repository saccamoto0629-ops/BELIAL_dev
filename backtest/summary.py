"""Backtest signal-level performance summary: aggregation logic only.

*** PAPER-ONLY BACKTEST EVALUATION. NOT AN EXECUTION LAYER. ***

Pure data-in/dict-out: no argparse, no stdout, no file I/O, no DB access.
Given a list of ``backtest_signals`` rows (see backtest_store.py's schema)
for one run_id, this computes the win/loss/pnl/profit-factor/drawdown
metrics Issue #23 asks for -- see report.py for turning the resulting dict
into text/JSON/Markdown, and runner.py for how both are wired into the CLI.

Never guesses a value the DB doesn't actually have:

- An unresolved ("open") signal's ``pnl`` is ``None`` (see
  backtest_models.create_backtest_signal_record()) and is excluded from
  every pnl-based metric here -- never treated as a 0-pnl trade. It still
  counts toward ``total_signals``.
- ``average_holding_bars`` is always ``None``: ``backtest_signals`` has no
  holding-bars-equivalent column, only wall-clock ``signal_time`` /
  ``closed_at``. Deriving a bar count from wall-clock duration would
  require assuming each ``timeframe`` string's bar length, which this
  Issue explicitly says not to guess.
"""

from __future__ import annotations

from typing import Any

# Existing backtest_signals columns (see backtest_store.REQUIRED_SIGNAL_COLUMNS)
# usable as a future breakdown axis today, without inventing any new column
# or guessing a value the DB doesn't have. Issue #39 adds ``market_state`` so
# by_market_state win-rate / PF is available in every report format; a signal
# with no state (legacy/NULL row) falls into the UNKNOWN_BREAKDOWN_KEY bucket.
BREAKDOWN_FIELDS = ("bias", "signal_type", "timeframe", "market_state")
UNKNOWN_BREAKDOWN_KEY = "unknown"


def _signal_time_key(signal: dict) -> str:
    return signal.get("signal_time") or ""


def _sorted_by_signal_time(signals: list[dict]) -> list[dict]:
    """Sort signals by signal_time ascending, never relying on DB row order.

    ISO-8601 "...Z" timestamps (see chart_collector.to_utc_iso_z()) sort
    correctly as plain strings, so no datetime parsing is needed here.
    list_backtest_signals() already queries with ``ORDER BY signal_time
    ASC``, but this function re-sorts explicitly anyway so this module's
    correctness never depends on that incidental DB ordering.
    """
    return sorted(signals, key=_signal_time_key)


def _resolved_pnls(ordered_signals: list[dict]) -> list[float]:
    """pnl values for signals that actually closed, in the given order.

    Excludes every signal whose pnl is None (open/unresolved) -- never
    substitutes 0.0 for a trade that hasn't closed yet.
    """
    return [float(signal["pnl"]) for signal in ordered_signals if signal.get("pnl") is not None]


def _max_drawdown(ordered_resolved_pnls: list[float]) -> float:
    """Largest peak-to-trough decline of the cumulative-pnl equity curve.

    Walks ``ordered_resolved_pnls`` in the given order only -- callers must
    have already sorted them causally (see _sorted_by_signal_time()) so no
    later pnl ever leaks into an earlier drawdown. Returned as a
    non-negative magnitude; 0.0 when equity never dips below a prior peak,
    including the 0-signal / 0-resolved-signal case.
    """
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in ordered_resolved_pnls:
        equity += pnl
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > max_dd:
            max_dd = drawdown
    return max_dd


def _profit_factor(resolved_pnls: list[float]) -> float | None:
    """gross_profit / abs(gross_loss), or None when that would be infinite.

    - No resolved trades, or resolved trades that net to zero profit and
      zero loss: 0.0 (matches Issue #23's 0-signal example).
    - Profit but zero loss: None. Mathematically infinite; JSON has no
      Infinity (see report.render_json()), and a sentinel float would
      misrepresent an exact value as approximate.
    - Otherwise: gross_profit / abs(gross_loss), always a finite float.
    """
    # sum(..., 0.0) rather than bare sum(...): Python's sum() of an empty
    # generator otherwise returns int 0, which would make total_pnl/gross_*
    # inconsistently switch between int and float depending on whether any
    # trades resolved -- always a float here so JSON output (and callers)
    # never has to handle a surprise int.
    gross_profit = sum((pnl for pnl in resolved_pnls if pnl > 0), 0.0)
    gross_loss = sum((pnl for pnl in resolved_pnls if pnl < 0), 0.0)
    if gross_loss == 0:
        return None if gross_profit > 0 else 0.0
    return gross_profit / abs(gross_loss)


def _aggregate(ordered_signals: list[dict]) -> dict[str, Any]:
    """Core win/loss/pnl metrics, shared by the top-level summary and every breakdown bucket.

    ``ordered_signals`` must already be sorted by signal_time (see
    _sorted_by_signal_time()) -- this function does not re-sort, since
    every caller here already has.
    """
    resolved_pnls = _resolved_pnls(ordered_signals)
    win_count = sum(1 for pnl in resolved_pnls if pnl > 0)
    loss_count = sum(1 for pnl in resolved_pnls if pnl < 0)
    decided = win_count + loss_count
    total_pnl = sum(resolved_pnls, 0.0)  # always float, even when resolved_pnls is empty
    resolved_count = len(resolved_pnls)

    return {
        "total_signals": len(ordered_signals),
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate": (win_count / decided) if decided > 0 else 0.0,
        "total_pnl": total_pnl,
        "average_pnl": (total_pnl / resolved_count) if resolved_count > 0 else 0.0,
        "profit_factor": _profit_factor(resolved_pnls),
        "max_drawdown": _max_drawdown(resolved_pnls),
    }


def _validate_run_id(run_id: str, signals: list[dict]) -> None:
    """Raise ValueError if any signal belongs to a different run_id.

    Only checks signals that actually carry a ``run_id`` key -- test/internal
    fixtures that omit it entirely (e.g. a hand-built dict with no ``run_id``
    field at all) are left alone, since there is nothing to contradict. Any
    signal that *does* carry a ``run_id`` but disagrees with the caller's
    ``run_id`` is a real cross-run mix-up and must never be silently
    aggregated in (or silently dropped) -- callers need to know their input
    was wrong, not get a quietly-incomplete or quietly-contaminated summary.
    """
    mismatched = sorted({signal["run_id"] for signal in signals if "run_id" in signal and signal["run_id"] != run_id})
    if mismatched:
        raise ValueError(
            f"build_summary() was called with run_id={run_id!r}, but received signal(s) "
            f"belonging to a different run_id: {mismatched!r}. Refusing to mix signals "
            "across runs -- pass only signals whose run_id matches the run_id argument."
        )


def _breakdown(ordered_signals: list[dict], field: str) -> dict[str, dict[str, Any]]:
    """Group ``ordered_signals`` by an existing column's actual value.

    A signal with no value for ``field`` (e.g. ``bias`` is nullable) is
    grouped under UNKNOWN_BREAKDOWN_KEY rather than dropped or guessed --
    this is a real "value is absent" bucket, not a fabricated one.
    """
    buckets: dict[str, list[dict]] = {}
    for signal in ordered_signals:
        key = signal.get(field) or UNKNOWN_BREAKDOWN_KEY
        buckets.setdefault(key, []).append(signal)
    return {key: _aggregate(bucket_signals) for key, bucket_signals in buckets.items()}


def build_summary(run_id: str, signals: list[dict]) -> dict[str, Any]:
    """Build the full Issue #23 summary report dict for one run_id's signals.

    ``signals`` should be every backtest_signals row for ``run_id``
    (typically backtest_store.list_backtest_signals(run_id, ...)); this
    function sorts them internally and never assumes any particular
    incoming order (see _sorted_by_signal_time()). Safe to call with an
    empty list -- raises nothing, and every metric resolves to its
    documented 0/None default.

    Raises ValueError if any signal's own ``run_id`` field disagrees with
    ``run_id`` (see _validate_run_id()) -- a caller must never end up
    silently aggregating another run's signals into this one.
    """
    _validate_run_id(run_id, signals)
    ordered = _sorted_by_signal_time(signals)
    result: dict[str, Any] = {"run_id": run_id, **_aggregate(ordered)}
    result["average_holding_bars"] = None
    result["breakdowns"] = {f"by_{field}": _breakdown(ordered, field) for field in BREAKDOWN_FIELDS}
    return result
