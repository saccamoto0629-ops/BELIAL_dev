"""Tests for the Issue #71 scheduled daily report run.

Covers:
- ``--previous-day``: timezone-aware "yesterday" (including just-after-midnight,
  month-start and year-start boundaries), computed from an injectable clock so
  no test depends on the real current time.
- ``paper_signal_store.list_signals_in_range_strict``: a valid DB with zero
  rows is normal; a missing / non-file / non-SQLite / wrong-schema / unreadable
  DB raises ``PaperSignalDbError`` -- and the CLI turns that into a non-zero
  exit without producing a report file.
- ``--output-dir``: auto-named ``paper_signal_YYYY-MM-DD.md`` matching the
  aggregated day, directory auto-creation, safe same-day overwrite (atomic
  replace), and non-zero exit when the file cannot be written.
"""

import os
import pathlib
import sqlite3
import sys
from datetime import UTC, datetime

import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import paper_signal_daily_report as cli  # noqa: E402
import paper_signal_store as store  # noqa: E402
import signal_models as sm  # noqa: E402


def _make_signal(*, created_at_utc, **overrides):
    candidate = {
        "symbol": "USDJPY",
        "timeframe": "1h",
        "side": "long",
        "market_bias": "Bull",
        "confidence": 0.72,
        "macd_state": "Bullish",
        "trend": "Bull",
        "state": "Inside band",
        "market_state": "trend_up",
        "freshness": "Fresh",
        "reason": "test reason",
    }
    candidate.update(overrides)
    return sm.create_paper_signal(candidate, created_at_utc=created_at_utc)


def _freeze_now(monkeypatch, utc_instant: datetime):
    """Pin cli._now so 'the current date in tz' is derived from one UTC instant."""
    assert utc_instant.tzinfo is not None
    monkeypatch.setattr(cli, "_now", lambda tz: utc_instant.astimezone(tz))


def _empty_db(tmp_path) -> pathlib.Path:
    db_path = tmp_path / "paper_signals.sqlite3"
    store.init_db(db_path)
    return db_path


WINDOW = ("2026-07-13T00:00:00Z", "2026-07-14T00:00:00Z")


# --------------------------------------------------------------------------- #
# --previous-day: timezone-aware date resolution
# --------------------------------------------------------------------------- #

def test_previous_day_is_yesterday_in_the_requested_timezone(tmp_path, capsys, monkeypatch):
    db_path = _empty_db(tmp_path)
    # 16:00Z = 2026-07-19 01:00 JST -> previous day in JST is 2026-07-18.
    _freeze_now(monkeypatch, datetime(2026, 7, 18, 16, 0, tzinfo=UTC))

    rc = cli.main(["--previous-day", "--timezone", "Asia/Tokyo", "--db-path", str(db_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Date: 2026-07-18 (Asia/Tokyo)" in out


def test_previous_day_depends_on_timezone_not_utc(tmp_path, capsys, monkeypatch):
    db_path = _empty_db(tmp_path)
    # Same instant as above, but in UTC "today" is still 07-18 -> previous is 07-17.
    _freeze_now(monkeypatch, datetime(2026, 7, 18, 16, 0, tzinfo=UTC))

    rc = cli.main(["--previous-day", "--timezone", "UTC", "--db-path", str(db_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Date: 2026-07-17 (UTC)" in out


def test_previous_day_just_after_midnight_reports_yesterday_not_today(tmp_path, capsys, monkeypatch):
    db_path = _empty_db(tmp_path)
    # 00:10 JST on 07-14 (the timer's firing time) = 15:10Z on 07-13.
    _freeze_now(monkeypatch, datetime(2026, 7, 13, 15, 10, tzinfo=UTC))

    rc = cli.main(["--previous-day", "--timezone", "Asia/Tokyo", "--db-path", str(db_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Date: 2026-07-13 (Asia/Tokyo)" in out


def test_previous_day_month_start_rolls_back_to_last_day_of_previous_month(tmp_path, capsys, monkeypatch):
    db_path = _empty_db(tmp_path)
    # 00:10 JST on 2026-07-01 -> previous day is 2026-06-30.
    _freeze_now(monkeypatch, datetime(2026, 6, 30, 15, 10, tzinfo=UTC))

    rc = cli.main(["--previous-day", "--timezone", "Asia/Tokyo", "--db-path", str(db_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Date: 2026-06-30 (Asia/Tokyo)" in out


def test_previous_day_year_start_rolls_back_to_december_31(tmp_path, capsys, monkeypatch):
    db_path = _empty_db(tmp_path)
    # 00:10 JST on 2027-01-01 -> previous day is 2026-12-31.
    _freeze_now(monkeypatch, datetime(2026, 12, 31, 15, 10, tzinfo=UTC))

    rc = cli.main(["--previous-day", "--timezone", "Asia/Tokyo", "--db-path", str(db_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Date: 2026-12-31 (Asia/Tokyo)" in out


def test_previous_day_selects_yesterdays_signals_only(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / "paper_signals.sqlite3"
    # 2026-07-18 JST spans 07-17T15:00Z .. 07-18T15:00Z.
    store.save_signal(_make_signal(created_at_utc="2026-07-17T20:00:00Z", symbol="YESTERDAY"), db_path=db_path)
    store.save_signal(_make_signal(created_at_utc="2026-07-18T20:00:00Z", symbol="TODAY"), db_path=db_path)
    _freeze_now(monkeypatch, datetime(2026, 7, 18, 16, 0, tzinfo=UTC))  # 07-19 01:00 JST

    rc = cli.main(["--previous-day", "--timezone", "Asia/Tokyo", "--db-path", str(db_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "YESTERDAY" in out
    assert "TODAY" not in out


def test_date_and_previous_day_together_is_a_usage_error(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--date", "2026-07-13", "--previous-day", "--db-path", str(tmp_path / "x.sqlite3")])
    assert excinfo.value.code == cli.EXIT_USAGE_ERROR


def test_no_date_flags_still_defaults_to_today(tmp_path, capsys, monkeypatch):
    db_path = _empty_db(tmp_path)
    _freeze_now(monkeypatch, datetime(2026, 7, 18, 16, 0, tzinfo=UTC))  # 07-19 01:00 JST

    rc = cli.main(["--timezone", "Asia/Tokyo", "--db-path", str(db_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Date: 2026-07-19 (Asia/Tokyo)" in out  # today, unchanged behavior


# --------------------------------------------------------------------------- #
# list_signals_in_range_strict -- valid-DB cases
# --------------------------------------------------------------------------- #

def test_strict_read_returns_rows_for_a_valid_db(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    store.save_signal(_make_signal(created_at_utc="2026-07-13T10:00:00Z"), db_path=db_path)

    rows = store.list_signals_in_range_strict(*WINDOW, db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "USDJPY"


def test_strict_read_empty_day_on_valid_db_is_normal(tmp_path):
    db_path = _empty_db(tmp_path)
    assert store.list_signals_in_range_strict(*WINDOW, db_path=db_path) == []


def test_strict_read_does_not_modify_the_db(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    store.save_signal(_make_signal(created_at_utc="2026-07-13T10:00:00Z"), db_path=db_path)
    before = db_path.read_bytes()

    store.list_signals_in_range_strict(*WINDOW, db_path=db_path)
    assert db_path.read_bytes() == before


# --------------------------------------------------------------------------- #
# list_signals_in_range_strict -- failure cases
# --------------------------------------------------------------------------- #

def test_strict_read_missing_db_raises_and_does_not_create_it(tmp_path):
    db_path = tmp_path / "does_not_exist.sqlite3"
    with pytest.raises(store.PaperSignalDbError, match="not found"):
        store.list_signals_in_range_strict(*WINDOW, db_path=db_path)
    assert not db_path.exists()


def test_strict_read_directory_path_raises(tmp_path):
    db_dir = tmp_path / "a_directory"
    db_dir.mkdir()
    with pytest.raises(store.PaperSignalDbError, match="not a regular file"):
        store.list_signals_in_range_strict(*WINDOW, db_path=db_dir)


def test_strict_read_non_sqlite_file_raises(tmp_path):
    bogus = tmp_path / "not_a_db.sqlite3"
    bogus.write_text("this is not a sqlite database\n", encoding="utf-8")
    with pytest.raises(store.PaperSignalDbError):
        store.list_signals_in_range_strict(*WINDOW, db_path=bogus)


def test_strict_read_missing_table_raises(tmp_path):
    db_path = tmp_path / "other_schema.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
    with pytest.raises(store.PaperSignalDbError, match="no paper_signals table"):
        store.list_signals_in_range_strict(*WINDOW, db_path=db_path)


def test_strict_read_missing_required_column_raises(tmp_path):
    db_path = tmp_path / "bad_columns.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE paper_signals (signal_id TEXT PRIMARY KEY)")
    with pytest.raises(store.PaperSignalDbError, match="created_at_utc"):
        store.list_signals_in_range_strict(*WINDOW, db_path=db_path)


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="chmod-based denial needs a non-root POSIX user")
def test_strict_read_unreadable_db_raises(tmp_path):
    db_path = _empty_db(tmp_path)
    db_path.chmod(0o000)
    try:
        with pytest.raises(store.PaperSignalDbError):
            store.list_signals_in_range_strict(*WINDOW, db_path=db_path)
    finally:
        db_path.chmod(0o644)


# --------------------------------------------------------------------------- #
# CLI: DB failures exit non-zero and leave no report file
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "make_db",
    [
        pytest.param(lambda p: p / "missing.sqlite3", id="missing-db"),
        pytest.param(lambda p: (lambda d: (d.mkdir(), d)[1])(p / "as_dir.sqlite3"), id="db-path-is-dir"),
        pytest.param(
            lambda p: (lambda f: (f.write_text("nope", encoding="utf-8"), f)[1])(p / "garbage.sqlite3"),
            id="not-sqlite",
        ),
    ],
)
def test_cli_db_failure_exits_nonzero_without_report_file(tmp_path, capsys, make_db):
    db_path = make_db(tmp_path)
    out_dir = tmp_path / "reports"

    rc = cli.main([
        "--date", "2026-07-13", "--timezone", "UTC",
        "--db-path", str(db_path), "--format", "markdown", "--output-dir", str(out_dir),
    ])
    err = capsys.readouterr().err
    assert rc == cli.EXIT_RUNTIME_ERROR
    assert "paper signal DB" in err
    # A DB failure must never leave a (zero-signal) report file behind.
    assert not out_dir.exists() or not list(out_dir.iterdir())


def test_cli_missing_table_exits_nonzero(tmp_path, capsys):
    db_path = tmp_path / "other.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")

    rc = cli.main(["--date", "2026-07-13", "--timezone", "UTC", "--db-path", str(db_path)])
    assert rc == cli.EXIT_RUNTIME_ERROR
    assert "no paper_signals table" in capsys.readouterr().err


def test_cli_zero_signal_day_on_valid_db_is_success_with_report(tmp_path, capsys):
    db_path = _empty_db(tmp_path)
    out_dir = tmp_path / "reports"

    rc = cli.main([
        "--date", "2026-07-13", "--timezone", "Asia/Tokyo",
        "--db-path", str(db_path), "--format", "markdown", "--output-dir", str(out_dir),
    ])
    assert rc == cli.EXIT_OK
    report_path = out_dir / "paper_signal_2026-07-13.md"
    content = report_path.read_text(encoding="utf-8")
    assert content.startswith("# Paper Signal Daily Report")
    assert "**Total Signals:** 0" in content


# --------------------------------------------------------------------------- #
# CLI: --output-dir naming, overwrite, and write failures
# --------------------------------------------------------------------------- #

def test_output_dir_filename_matches_previous_day_date(tmp_path, monkeypatch):
    db_path = _empty_db(tmp_path)
    out_dir = tmp_path / "brand" / "new" / "reports"  # must be created on demand
    _freeze_now(monkeypatch, datetime(2026, 7, 13, 15, 10, tzinfo=UTC))  # 00:10 JST 07-14

    rc = cli.main([
        "--previous-day", "--timezone", "Asia/Tokyo",
        "--db-path", str(db_path), "--format", "markdown", "--output-dir", str(out_dir),
    ])
    assert rc == 0
    assert (out_dir / "paper_signal_2026-07-13.md").exists()


def test_output_dir_same_day_rerun_overwrites_safely(tmp_path, capsys):
    db_path = tmp_path / "paper_signals.sqlite3"
    out_dir = tmp_path / "reports"
    argv = [
        "--date", "2026-07-13", "--timezone", "UTC",
        "--db-path", str(db_path), "--format", "markdown", "--output-dir", str(out_dir),
    ]

    store.init_db(db_path)
    assert cli.main(argv) == 0
    report_path = out_dir / "paper_signal_2026-07-13.md"
    assert "**Total Signals:** 0" in report_path.read_text(encoding="utf-8")

    # A signal arrives; the re-run must replace the file with the new content
    # and leave no temp-file debris next to it.
    store.save_signal(_make_signal(created_at_utc="2026-07-13T10:00:00Z"), db_path=db_path)
    assert cli.main(argv) == 0
    assert "**Total Signals:** 1" in report_path.read_text(encoding="utf-8")
    assert [p.name for p in out_dir.iterdir()] == ["paper_signal_2026-07-13.md"]


def test_output_and_output_dir_together_is_a_usage_error(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        cli.main([
            "--date", "2026-07-13", "--db-path", str(tmp_path / "x.sqlite3"),
            "--output", str(tmp_path / "a.md"), "--output-dir", str(tmp_path),
        ])
    assert excinfo.value.code == cli.EXIT_USAGE_ERROR


def test_unwritable_output_exits_nonzero(tmp_path, capsys):
    db_path = _empty_db(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("a plain file where a directory is needed", encoding="utf-8")

    rc = cli.main([
        "--date", "2026-07-13", "--timezone", "UTC",
        "--db-path", str(db_path), "--format", "markdown",
        "--output-dir", str(blocker),  # mkdir over an existing file fails
    ])
    assert rc == cli.EXIT_RUNTIME_ERROR
    assert "could not write report" in capsys.readouterr().err


def test_output_dir_text_format_uses_txt_extension(tmp_path):
    db_path = _empty_db(tmp_path)
    out_dir = tmp_path / "reports"

    rc = cli.main([
        "--date", "2026-07-13", "--timezone", "UTC",
        "--db-path", str(db_path), "--format", "text", "--output-dir", str(out_dir),
    ])
    assert rc == 0
    assert (out_dir / "paper_signal_2026-07-13.txt").exists()
