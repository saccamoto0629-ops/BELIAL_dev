#!/usr/bin/env python
"""CLI entrypoint for the Paper Signal daily aggregation report (Issue #47).

Reads one day's stored Paper Signals and prints (or writes) a symbol / timeframe
/ direction / market-state / strategy breakdown. Read-only and side-effect-free:
it never writes the signal DB, never sends a Discord notification, and never
touches a broker / Execution API -- the only thing it may write is the optional
``--output`` file. All aggregation/rendering lives in ``daily_report``; the store
read is ``paper_signal_store.list_signals_in_range`` (which does not even create
the DB). This module is only argument parsing + wiring, so it stays import-light
(no pandas / requests / signal_generator), suitable for a daily cron/systemd run.

Usage (see docs/operations/operations.md):

    # Today's report (in the configured timezone), text to stdout:
    python paper_signal_daily_report.py

    # A specific day, as Markdown, written to a file:
    python paper_signal_daily_report.py --date 2026-07-13 --format markdown \
        --output reports/paper_signal_2026-07-13.md

    # A different timezone for the day boundary:
    python paper_signal_daily_report.py --date 2026-07-13 --timezone UTC
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import daily_report  # noqa: E402
import paper_signal_store as store  # noqa: E402

ENV_FILE = BASE_DIR / ".env"
# Same env var name a systemd EnvironmentFile / shell export would set. Reading
# the .env file directly (below) mirrors signal_generator.load_env_file(), so a
# manual run without systemd still honors the configured timezone.
TIMEZONE_ENV_VAR = "PAPER_SIGNAL_REPORT_TIMEZONE"

EXIT_OK = 0
EXIT_USAGE_ERROR = 2


def _env_timezone() -> str | None:
    """The configured report timezone from the environment or paper-signal/.env.

    Precedence: a real process env var (set by systemd's EnvironmentFile or a
    shell export) wins; otherwise the same key is read from the uncommitted
    .env file, so a manual ``python paper_signal_daily_report.py`` outside systemd
    still picks it up. Returns None when neither sets it (caller falls back to
    daily_report.DEFAULT_TIMEZONE). A tiny, dependency-free parser deliberately
    mirrors signal_generator.load_env_file() rather than importing it, keeping
    this entrypoint free of the generator's heavy imports.
    """
    from_process = os.environ.get(TIMEZONE_ENV_VAR)
    if from_process not in (None, ""):
        return from_process
    if not ENV_FILE.exists():
        return None
    with ENV_FILE.open("r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == TIMEZONE_ENV_VAR:
                cleaned = value.strip().strip("\"'")
                return cleaned or None
    return None


def _report_date(value: str) -> date:
    """argparse type for --date: a strict ``YYYY-MM-DD`` calendar date."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}: expected YYYY-MM-DD (e.g. 2026-07-13)"
        ) from exc


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="paper_signal_daily_report",
        description=(
            "Aggregate one day's Paper Signals by symbol / timeframe / direction / "
            "market state / strategy. Read-only: never writes the signal DB, sends a "
            "Discord message, or calls a broker/Execution API."
        ),
    )
    parser.add_argument(
        "--date",
        type=_report_date,
        default=None,
        help="Calendar day to report on, YYYY-MM-DD, interpreted in --timezone. "
        "Defaults to today in that timezone.",
    )
    parser.add_argument(
        "--timezone",
        default=None,
        help="IANA timezone for the day boundary (e.g. Asia/Tokyo, UTC). Defaults to "
        f"${TIMEZONE_ENV_VAR} / paper-signal/.env, then {daily_report.DEFAULT_TIMEZONE}.",
    )
    parser.add_argument(
        "--format",
        choices=daily_report.KNOWN_FORMATS,
        default=daily_report.FORMAT_TEXT,
        help=f"Output format (default: {daily_report.FORMAT_TEXT}).",
    )
    parser.add_argument(
        "--output",
        help="Write the rendered report to this file instead of only printing it. "
        "Parent directories are created automatically; an existing file is overwritten.",
    )
    parser.add_argument(
        "--db-path",
        help="Path to the Paper Signal SQLite DB. Defaults to "
        "paper-signal/data/paper_signals.sqlite3.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = parse_args(argv)
    timezone_name = args.timezone or _env_timezone() or daily_report.DEFAULT_TIMEZONE

    # Validate/resolve the timezone once, up front, so a bad name fails as a clean
    # usage error rather than deep inside window computation.
    try:
        tz = daily_report._load_timezone(timezone_name)
    except daily_report.UnknownTimezoneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR

    report_date = args.date or datetime.now(tz).date()
    db_path = Path(args.db_path) if args.db_path else store.DEFAULT_DB_PATH

    start_utc, end_utc = daily_report.resolve_report_window(report_date, timezone_name)
    signals = store.list_signals_in_range(start_utc, end_utc, db_path=db_path)
    report = daily_report.build_daily_report(
        signals, report_date=report_date, timezone_name=timezone_name
    )

    content = daily_report.render(report, args.format)

    if args.output:
        output_path = Path(args.output)
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            # render_markdown already ends with a newline; only text needs one added.
            output_path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"error: could not write report to {args.output}: {exc}", file=sys.stderr)
            return EXIT_USAGE_ERROR
        print(f"Saved daily report to {output_path}")
    else:
        print(content)

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
