"""Rendering + persistence for the Issue #23 Backtest signal summary report.

Pure presentation layer: takes the dict built by
``summary.build_summary()`` and turns it into text/JSON/Markdown, or writes
it to a file. No DB access, no argparse -- see runner.py for how these are
wired into the CLI (``python -m backtest summary --run-id ...``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FORMAT_TEXT = "text"
FORMAT_JSON = "json"
FORMAT_MARKDOWN = "markdown"
KNOWN_FORMATS = (FORMAT_TEXT, FORMAT_JSON, FORMAT_MARKDOWN)


def _format_metric(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _execution_model_text(run_meta: dict[str, Any]) -> str:
    execution_model = run_meta.get("execution_model") or {}
    return (
        f"{execution_model.get('type') or 'n/a'} "
        f"(spread={_format_metric(execution_model.get('spread'))}, "
        f"slippage={_format_metric(execution_model.get('slippage'))})"
    )


def _run_meta_text_lines(run_meta: dict[str, Any] | None) -> list[str]:
    """Text lines identifying the run's strategy + execution conditions.

    Issue #44 review (Major): rendered whenever a summary carries ``run_meta``
    (attached by the runner from the saved run config). Absent for the pre-#44
    signal-only summaries, so those render exactly as before.
    """
    if not run_meta:
        return []
    lines = [f"Strategy profile:    {run_meta.get('strategy_profile') or 'unknown'}"]
    if run_meta.get("symbol") or run_meta.get("timeframe"):
        lines.append(f"Symbol / timeframe:  {run_meta.get('symbol')} / {run_meta.get('timeframe')}")
    if run_meta.get("from_time") or run_meta.get("to_time"):
        lines.append(f"Window:              {run_meta.get('from_time')} .. {run_meta.get('to_time')}")
    if run_meta.get("exit_policy"):
        lines.append(f"Exit policy:         {run_meta.get('exit_policy')}")
    lines.append(f"Execution model:     {_execution_model_text(run_meta)}")
    return lines


def _run_meta_markdown_lines(run_meta: dict[str, Any] | None) -> list[str]:
    if not run_meta:
        return []
    lines = [
        "## Run",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Strategy profile | {run_meta.get('strategy_profile') or 'unknown'} |",
        f"| Symbol / timeframe | {run_meta.get('symbol')} / {run_meta.get('timeframe')} |",
        f"| Window | {run_meta.get('from_time')} .. {run_meta.get('to_time')} |",
        f"| Exit policy | {run_meta.get('exit_policy') or 'n/a'} |",
        f"| Execution model | {_execution_model_text(run_meta)} |",
        "",
    ]
    return lines


def render_text(summary: dict[str, Any]) -> str:
    lines = [
        "=== Backtest Signal Summary ===",
        f"Run ID:              {summary['run_id']}",
        *_run_meta_text_lines(summary.get("run_meta")),
        f"Total signals:       {summary['total_signals']}",
        f"Win / Loss:          {summary['win_count']} / {summary['loss_count']}",
        f"Win rate:            {_format_metric(summary['win_rate'])}",
        f"Total PnL:           {_format_metric(summary['total_pnl'])}",
        f"Average PnL:         {_format_metric(summary['average_pnl'])}",
        f"Profit factor:       {_format_metric(summary['profit_factor'])}",
        f"Max drawdown:        {_format_metric(summary['max_drawdown'])}",
        f"Avg holding bars:    {_format_metric(summary['average_holding_bars'])}",
    ]
    for axis_name, buckets in summary.get("breakdowns", {}).items():
        lines.append(f"{axis_name}:")
        if not buckets:
            lines.append("  (no data)")
            continue
        for key, metrics in buckets.items():
            lines.append(
                f"  {key}: signals={metrics['total_signals']} "
                f"win={metrics['win_count']} loss={metrics['loss_count']} "
                f"win_rate={_format_metric(metrics['win_rate'])} "
                f"profit_factor={_format_metric(metrics['profit_factor'])} "
                f"total_pnl={_format_metric(metrics['total_pnl'])} "
                f"average_pnl={_format_metric(metrics['average_pnl'])} "
                f"max_drawdown={_format_metric(metrics['max_drawdown'])}"
            )
    return "\n".join(lines)


def render_json(summary: dict[str, Any]) -> str:
    """JSON-encode ``summary``.

    ``allow_nan=False`` makes this raise ValueError instead of silently
    emitting the non-standard ``NaN``/``Infinity`` tokens if a NaN/inf ever
    reached this point -- defense in depth on top of
    summary._profit_factor() already never producing one.
    """
    return json.dumps(summary, indent=2, allow_nan=False)


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# Backtest Summary: {summary['run_id']}",
        "",
        *_run_meta_markdown_lines(summary.get("run_meta")),
        "| Metric | Value |",
        "|---|---|",
        f"| Total signals | {summary['total_signals']} |",
        f"| Win count | {summary['win_count']} |",
        f"| Loss count | {summary['loss_count']} |",
        f"| Win rate | {_format_metric(summary['win_rate'])} |",
        f"| Total PnL | {_format_metric(summary['total_pnl'])} |",
        f"| Average PnL | {_format_metric(summary['average_pnl'])} |",
        f"| Profit factor | {_format_metric(summary['profit_factor'])} |",
        f"| Max drawdown | {_format_metric(summary['max_drawdown'])} |",
        f"| Average holding bars | {_format_metric(summary['average_holding_bars'])} |",
    ]
    for axis_name, buckets in summary.get("breakdowns", {}).items():
        lines.append("")
        lines.append(f"## {axis_name}")
        lines.append("")
        if not buckets:
            lines.append("(no data)")
            continue
        lines.append("| Key | Signals | Win | Loss | Win rate | Profit factor | Total PnL | Average PnL | Max drawdown |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for key, metrics in buckets.items():
            lines.append(
                f"| {key} | {metrics['total_signals']} | {metrics['win_count']} | "
                f"{metrics['loss_count']} | {_format_metric(metrics['win_rate'])} | "
                f"{_format_metric(metrics['profit_factor'])} | "
                f"{_format_metric(metrics['total_pnl'])} | "
                f"{_format_metric(metrics['average_pnl'])} | "
                f"{_format_metric(metrics['max_drawdown'])} |"
            )
    return "\n".join(lines) + "\n"


def render(summary: dict[str, Any], fmt: str) -> str:
    if fmt == FORMAT_JSON:
        return render_json(summary)
    if fmt == FORMAT_MARKDOWN:
        return render_markdown(summary)
    if fmt == FORMAT_TEXT:
        return render_text(summary)
    raise ValueError(f"Unknown summary format: {fmt!r}. Known formats: {KNOWN_FORMATS}.")


def save_summary(content: str, output_path: Path | str) -> Path:
    """Write ``content`` to ``output_path``, creating parent directories as needed.

    Always overwrites an existing file at ``output_path`` (no project
    convention here calls for a confirmation prompt or a non-destructive
    default -- e.g. chart_collector.save_candles() already unconditionally
    overwrites rows via ``INSERT OR REPLACE``), so re-running with the same
    ``--output`` path is expected to refresh the file, not fail.
    """
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path
