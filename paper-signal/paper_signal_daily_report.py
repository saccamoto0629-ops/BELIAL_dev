#!/usr/bin/env python
"""CLI entrypoint for the Paper Signal daily aggregation report (Issue #47).

Reads one day's stored Paper Signals and prints (or writes) a symbol / timeframe
/ direction / market-state / strategy breakdown. Read-only and side-effect-free:
it never writes the signal DB, never sends a Discord notification, and never
touches a broker / Execution API -- the only thing it may write is the optional
``--output`` / ``--output-dir`` file. All aggregation/rendering lives in
``daily_report``; the store read is
``paper_signal_store.list_signals_in_range_strict`` (read-only URI connection --
it can never create or write the DB). This module is only argument parsing +
wiring, so it stays import-light
(no pandas / requests / signal_generator), suitable for a daily cron/systemd run.

Issue #71 (scheduled run): ``--previous-day`` reports on yesterday *in the
resolved timezone* (computed here, not by shell ``date`` tricks in the systemd
unit), ``--output-dir`` derives the ``paper_signal_YYYY-MM-DD.md`` filename from
the actual report date, and DB problems (missing file, not SQLite, missing
table/column, unreadable) exit non-zero via the strict store read instead of
being silently rendered as a zero-signal day -- so the systemd oneshot service
fails visibly. Only a valid DB whose day is genuinely empty produces the normal
zero-signal report.

Usage (see docs/operations/operations.md):

    # Today's report (in the configured timezone), text to stdout:
    python paper_signal_daily_report.py

    # A specific day, as Markdown, written to a file:
    python paper_signal_daily_report.py --date 2026-07-13 --format markdown \
        --output reports/paper_signal_2026-07-13.md

    # Yesterday (in Asia/Tokyo), auto-named file -- the systemd timer's form:
    python paper_signal_daily_report.py --previous-day --timezone Asia/Tokyo \
        --format markdown --output-dir reports

    # A different timezone for the day boundary:
    python paper_signal_daily_report.py --date 2026-07-13 --timezone UTC
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from contextlib import suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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
# Issue #71 exit-code contract (systemd relies on non-zero == service failure):
#   0 -- normal completion, including a valid DB with zero signals for the day.
#   1 -- runtime error: DB missing/unreadable/not SQLite/bad schema, or the
#        report file could not be written.
#   2 -- usage/configuration error: bad arguments, bad date, unknown timezone
#        (argparse itself also exits 2 on invalid CLI input).
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

# --output-dir derives the filename from the actual report date, so the name can
# never disagree with the aggregated day (unlike a shell $(date ...) expansion).
REPORT_FILENAME_PREFIX = "paper_signal_"
REPORT_FILENAME_SUFFIXES = {
    daily_report.FORMAT_MARKDOWN: ".md",
    daily_report.FORMAT_TEXT: ".txt",
}


def _now(tz: ZoneInfo) -> datetime:
    """Current moment in ``tz``. A seam so tests can pin "now" deterministically."""
    return datetime.now(tz)


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
    day_group = parser.add_mutually_exclusive_group()
    day_group.add_argument(
        "--date",
        type=_report_date,
        default=None,
        help="Calendar day to report on, YYYY-MM-DD, interpreted in --timezone. "
        "Defaults to today in that timezone.",
    )
    day_group.add_argument(
        "--previous-day",
        action="store_true",
        help="Report on the day before the current date in --timezone (the "
        "timezone is resolved first, then 'yesterday' is computed from it). "
        "Intended for the daily systemd timer; mutually exclusive with --date.",
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
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--output",
        help="Write the rendered report to this file instead of only printing it. "
        "Parent directories are created automatically; an existing file is overwritten.",
    )
    output_group.add_argument(
        "--output-dir",
        help="Write the report into this directory as "
        f"{REPORT_FILENAME_PREFIX}YYYY-MM-DD{REPORT_FILENAME_SUFFIXES[daily_report.FORMAT_MARKDOWN]} "
        "(date = the actual report day; extension follows --format). The directory "
        "is created if missing; an existing same-day file is atomically replaced. "
        "Mutually exclusive with --output.",
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

    # The target day is computed only after the timezone is resolved: "yesterday"
    # is timezone-dependent (01:00 JST on Jan 1 must report Dec 31 JST, even
    # though it is still Dec 31 in UTC). timedelta handles month/year rollovers.
    if args.previous_day:
        report_date = _now(tz).date() - timedelta(days=1)
    else:
        report_date = args.date or _now(tz).date()
    db_path = Path(args.db_path) if args.db_path else store.DEFAULT_DB_PATH

    start_utc, end_utc = daily_report.resolve_report_window(report_date, timezone_name)
    # Strict read (Issue #71): a missing/unreadable/non-SQLite DB or a bad schema
    # is a runtime failure, never a silent zero-signal day. Happens before any
    # output-file work, so a DB failure leaves no report file behind.
    try:
        signals = store.list_signals_in_range_strict(start_utc, end_utc, db_path=db_path)
    except store.PaperSignalDbError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_ERROR
    report = daily_report.build_daily_report(
        signals, report_date=report_date, timezone_name=timezone_name
    )

    content = daily_report.render(report, args.format)

    if args.output:
        output_path = Path(args.output)
    elif args.output_dir:
        suffix = REPORT_FILENAME_SUFFIXES[args.format]
        output_path = Path(args.output_dir) / f"{REPORT_FILENAME_PREFIX}{report_date.isoformat()}{suffix}"
    else:
        output_path = None

    if output_path is not None:
        try:
            # render_markdown already ends with a newline; only text needs one added.
            _write_report_file(output_path, content if content.endswith("\n") else content + "\n")
        except OSError as exc:
            print(f"error: could not write report to {output_path}: {exc}", file=sys.stderr)
            return EXIT_RUNTIME_ERROR
        print(f"Saved daily report to {output_path}")
    else:
        print(content)

    return EXIT_OK


def _write_report_file(output_path: Path, content: str) -> None:
    """Write ``content`` to ``output_path`` atomically, creating parent dirs.

    The report is written to a temp file in the destination directory and then
    ``os.replace``d over the target, so a crash or full disk mid-write can never
    leave a truncated ``paper_signal_YYYY-MM-DD.md`` -- the previous complete
    file (or no file) remains. Same-day re-runs therefore overwrite safely.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=output_path.parent, prefix=output_path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(content)
        os.replace(tmp_name, output_path)
    except OSError:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
