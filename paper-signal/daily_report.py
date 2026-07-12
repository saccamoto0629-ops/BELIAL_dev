"""Daily aggregation of Paper Signals: aggregation + rendering only (Issue #47).

*** OBSERVATION-ONLY. NOT AN EXECUTION LAYER. ***

Paper Signal is an always-on hypothesis generator; this module turns one day's
worth of those hypothesis records into a symbol / timeframe / direction /
market-state / strategy breakdown so the occurrence tendencies can be watched
before Paper Execution is ever introduced. It never reflects a virtual
fill, PnL, win rate, broker call, or real order -- those are explicitly out of
scope for this Issue (see the "By ..." sections below: they are pure counts).

Split of concerns, mirroring backtest/summary.py + backtest/report.py:

- :func:`resolve_report_window` -- turn a calendar date + timezone into the
  half-open ``[start, end)`` UTC window that defines "that day".
- :func:`build_daily_report` -- pure data-in / dict-out aggregation. No DB, no
  argparse, no stdout, no file I/O. Given the day's signal rows (already fetched
  and windowed by the caller), it counts them per axis and summarizes
  confidence. Safe on an empty list.
- :func:`render_text` / :func:`render_markdown` / :func:`render` -- turn that
  dict into text / Markdown. See paper_signal_daily_report.py for the CLI that
  wires the store read + these together.

Day-boundary discipline (Issue #47 acceptance): every ``created_at_utc`` is a
UTC ``...Z`` instant, but "which day" is decided in the *configured timezone*.
The window is computed as local-midnight to next-local-midnight, converted to
UTC -- so a signal at 2026-07-13T15:30:00Z lands on 2026-07-14 in Asia/Tokyo
(00:30 JST) but on 2026-07-13 in UTC, consistently, from one place.

NULL / unknown discipline: a signal whose axis value is NULL or empty is counted
under the explicit ``"unknown"`` bucket (never dropped), exactly like
backtest/summary.py's UNKNOWN_BREAKDOWN_KEY. A literal ``"unknown"`` value merges
into the same bucket. Every axis therefore sums back to ``total_signals``.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Default reporting timezone. The Berial stack reads/renders times in JST
# throughout (freshness.JST, chart_renderer's Asia/Tokyo x-axis, the Paper
# Signal Discord message's JST rendering), so the daily report's day boundary
# defaults to the same civil timezone. Overridable per run (CLI --timezone /
# PAPER_SIGNAL_REPORT_TIMEZONE) so the "configured timezone" is explicit.
DEFAULT_TIMEZONE = "Asia/Tokyo"

# A signal whose axis value is NULL/empty is counted here rather than dropped.
UNKNOWN_KEY = "unknown"

# The five count axes, mapped to the paper_signals column each groups by. Order
# is the report's display order (matches the Issue #47 output example, with
# timeframe added per its scope list).
COUNT_AXES: tuple[tuple[str, str], ...] = (
    ("by_symbol", "symbol"),
    ("by_timeframe", "timeframe"),
    ("by_direction", "side"),
    ("by_market_state", "market_state"),
    ("by_strategy", "strategy_profile"),
)

# Human-readable section titles for rendering, keyed by the axis name above.
AXIS_TITLES: dict[str, str] = {
    "by_symbol": "By Symbol",
    "by_timeframe": "By Timeframe",
    "by_direction": "By Direction",
    "by_market_state": "By Market State",
    "by_strategy": "By Strategy",
}

FORMAT_TEXT = "text"
FORMAT_MARKDOWN = "markdown"
KNOWN_FORMATS = (FORMAT_TEXT, FORMAT_MARKDOWN)


class UnknownTimezoneError(ValueError):
    """Raised when the requested reporting timezone name is not a known zone."""


def _load_timezone(timezone_name: str) -> ZoneInfo:
    """Resolve an IANA timezone name, re-raising a not-found as a clean error.

    zoneinfo raises ``ZoneInfoNotFoundError`` for an unknown/misspelled name (or
    a platform missing the tz database). Surfacing it as our own
    :class:`UnknownTimezoneError` lets the CLI turn it into a short usage error
    instead of an opaque traceback.
    """
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UnknownTimezoneError(f"unknown timezone: {timezone_name!r}") from exc


def _to_utc_z(moment: datetime) -> str:
    """Format an aware datetime as a second-precision UTC ``...Z`` string.

    Matches signal_models.utc_now_iso()'s stored format exactly, so the returned
    window bounds compare correctly against stored ``created_at_utc`` values as
    plain strings (see paper_signal_store.list_signals_in_range).
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_report_window(report_date: datetime | Any, timezone_name: str = DEFAULT_TIMEZONE) -> tuple[str, str]:
    """Return the half-open ``[start, end)`` UTC window for ``report_date``.

    ``report_date`` is a ``datetime.date`` (or anything with year/month/day, e.g.
    a ``datetime``) naming the civil day to report on. The window runs from local
    midnight on that day to local midnight the next day -- computed as two
    ``datetime.combine(date, 00:00, tz)`` instants, not by adding 24h, so it stays
    correct across a DST change in zones that have one. Both bounds are returned
    as UTC ``...Z`` strings; ``start`` is inclusive and ``end`` exclusive, so a
    candle exactly at the next day's midnight belongs to the next day, never both.
    """
    tz = _load_timezone(timezone_name)
    # Accept a full datetime by taking its date part; only the calendar day matters.
    day = report_date.date() if isinstance(report_date, datetime) else report_date
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return _to_utc_z(start_local), _to_utc_z(end_local)


def _bucket_key(value: Any) -> str:
    """Map a raw axis value to its bucket key, folding NULL/empty into UNKNOWN_KEY."""
    if value is None:
        return UNKNOWN_KEY
    text = str(value).strip()
    return text if text else UNKNOWN_KEY


def _count_by(signals: list[dict], field: str) -> dict[str, int]:
    """Count ``signals`` by one column, ordered most-frequent-first then by key.

    A stable, deterministic order (descending count, then ascending key for ties)
    keeps rendered output reproducible regardless of DB row order. NULL/empty
    values are grouped under UNKNOWN_KEY, so the returned counts always sum to
    ``len(signals)``.
    """
    counts: dict[str, int] = {}
    for signal in signals:
        key = _bucket_key(signal.get(field))
        counts[key] = counts.get(key, 0) + 1
    return {key: count for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))}


def _confidence_stats(signals: list[dict]) -> dict[str, Any] | None:
    """Average / min / max confidence over signals that carry one, else None.

    A signal with a NULL confidence is excluded from these stats (never treated
    as 0.0), mirroring how backtest/summary.py excludes an unresolved pnl. When
    no signal carries a confidence value at all, returns None so the report can
    render an explicit "no confidence values" rather than a misleading 0.
    """
    values = [float(signal["confidence"]) for signal in signals if signal.get("confidence") is not None]
    if not values:
        return None
    return {
        "count": len(values),
        "average": sum(values) / len(values),
        "min": min(values),
        "max": max(values),
    }


def build_daily_report(
    signals: list[dict],
    *,
    report_date: datetime | Any,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    """Build the Issue #47 daily aggregation dict for one day's Paper Signals.

    ``signals`` must be exactly the rows for ``report_date`` in ``timezone_name``
    (typically paper_signal_store.list_signals_in_range() over
    :func:`resolve_report_window`'s bounds); this function does not re-filter by
    time -- it aggregates whatever it is given. Safe to call with an empty list:
    every axis is ``{}`` and confidence is None, and ``total_signals`` is 0.

    The returned dict is JSON-serializable and self-describing (date, timezone,
    and the exact UTC window it covers), so a caller can render it or persist it
    without re-deriving any of those facts.
    """
    day = report_date.date() if isinstance(report_date, datetime) else report_date
    start_utc, end_utc = resolve_report_window(day, timezone_name)
    report: dict[str, Any] = {
        "date": day.isoformat(),
        "timezone": timezone_name,
        "window_utc": {"start": start_utc, "end": end_utc},
        "total_signals": len(signals),
    }
    for axis_name, field in COUNT_AXES:
        report[axis_name] = _count_by(signals, field)
    report["confidence"] = _confidence_stats(signals)
    return report


def _format_confidence(value: float) -> str:
    """Render a confidence value with the codebase's 4-decimal metric convention."""
    return f"{value:.4f}"


def _count_lines(counts: dict[str, int], *, bullet: str = "- ") -> list[str]:
    """One ``- key: count`` line per bucket, or a single "(none)" when empty."""
    if not counts:
        return [f"{bullet}(none)"]
    return [f"{bullet}{key}: {count}" for key, count in counts.items()]


def render_text(report: dict[str, Any]) -> str:
    """Render the daily report as plain text (matches the Issue #47 example)."""
    window = report.get("window_utc") or {}
    lines = [
        "Paper Signal Daily Report",
        f"Date: {report['date']} ({report['timezone']})",
        f"Window (UTC): {window.get('start')} .. {window.get('end')}",
        f"Total Signals: {report['total_signals']}",
    ]
    for axis_name, _field in COUNT_AXES:
        lines.append("")
        lines.append(AXIS_TITLES[axis_name])
        lines.extend(_count_lines(report.get(axis_name) or {}))

    lines.append("")
    lines.append("Confidence")
    confidence = report.get("confidence")
    if confidence is None:
        lines.append("- (no confidence values)")
    else:
        lines.append(f"- count: {confidence['count']}")
        lines.append(f"- average: {_format_confidence(confidence['average'])}")
        lines.append(f"- min: {_format_confidence(confidence['min'])}")
        lines.append(f"- max: {_format_confidence(confidence['max'])}")
    return "\n".join(lines)


def _markdown_count_table(counts: dict[str, int]) -> list[str]:
    if not counts:
        return ["(none)"]
    lines = ["| Key | Count |", "|---|---|"]
    lines.extend(f"| {key} | {count} |" for key, count in counts.items())
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    """Render the daily report as Markdown (headings + count tables)."""
    window = report.get("window_utc") or {}
    lines = [
        "# Paper Signal Daily Report",
        "",
        f"- **Date:** {report['date']} ({report['timezone']})",
        f"- **Window (UTC):** {window.get('start')} .. {window.get('end')}",
        f"- **Total Signals:** {report['total_signals']}",
    ]
    for axis_name, _field in COUNT_AXES:
        lines.append("")
        lines.append(f"## {AXIS_TITLES[axis_name]}")
        lines.append("")
        lines.extend(_markdown_count_table(report.get(axis_name) or {}))

    lines.append("")
    lines.append("## Confidence")
    lines.append("")
    confidence = report.get("confidence")
    if confidence is None:
        lines.append("(no confidence values)")
    else:
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        lines.append(f"| Count | {confidence['count']} |")
        lines.append(f"| Average | {_format_confidence(confidence['average'])} |")
        lines.append(f"| Min | {_format_confidence(confidence['min'])} |")
        lines.append(f"| Max | {_format_confidence(confidence['max'])} |")
    return "\n".join(lines) + "\n"


def render(report: dict[str, Any], fmt: str) -> str:
    """Render ``report`` in ``fmt`` (one of :data:`KNOWN_FORMATS`)."""
    if fmt == FORMAT_TEXT:
        return render_text(report)
    if fmt == FORMAT_MARKDOWN:
        return render_markdown(report)
    raise ValueError(f"Unknown report format: {fmt!r}. Known formats: {KNOWN_FORMATS}.")
