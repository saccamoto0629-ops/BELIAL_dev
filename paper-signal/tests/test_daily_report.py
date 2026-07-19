"""Tests for the Issue #47 Paper Signal daily aggregation report.

Covers three layers:
- daily_report.py  -- window resolution, pure aggregation, rendering.
- paper_signal_store.list_signals_in_range -- the read-only, side-effect-free
  windowed query.
- paper_signal_daily_report.py -- the CLI wiring (date/timezone/format/output),
  including that generating a report never writes the signal DB or notifies.
"""

import pathlib
import sys
from datetime import date

import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import daily_report as dr  # noqa: E402
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


def _rows(*specs):
    """Build a list of plain aggregation dicts (not full PaperSignals).

    ``build_daily_report`` only reads the columns it groups by, so a test that
    exercises aggregation can pass minimal dicts. Each spec is a dict of
    overrides on top of a sensible default row.
    """
    out = []
    for spec in specs:
        row = {
            "symbol": "USDJPY",
            "timeframe": "1h",
            "side": "long",
            "state": "Inside band",
            "market_state": "trend_up",
            "strategy_profile": "market_bias_macd_alignment_v1",
            "confidence": 0.5,
        }
        row.update(spec)
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# resolve_report_window
# --------------------------------------------------------------------------- #

def test_window_jst_is_utc_minus_9_hours():
    start, end = dr.resolve_report_window(date(2026, 7, 13), "Asia/Tokyo")
    # JST midnight is the previous UTC day at 15:00Z.
    assert start == "2026-07-12T15:00:00Z"
    assert end == "2026-07-13T15:00:00Z"


def test_window_utc_is_plain_midnight_to_midnight():
    start, end = dr.resolve_report_window(date(2026, 7, 13), "UTC")
    assert start == "2026-07-13T00:00:00Z"
    assert end == "2026-07-14T00:00:00Z"


def test_window_is_half_open_no_overlap_between_consecutive_days():
    _, end_day1 = dr.resolve_report_window(date(2026, 7, 13), "Asia/Tokyo")
    start_day2, _ = dr.resolve_report_window(date(2026, 7, 14), "Asia/Tokyo")
    # Day 1's exclusive end equals day 2's inclusive start: a candle exactly there
    # belongs to day 2 only.
    assert end_day1 == start_day2


def test_unknown_timezone_raises():
    with pytest.raises(dr.UnknownTimezoneError):
        dr.resolve_report_window(date(2026, 7, 13), "Not/AZone")


# --------------------------------------------------------------------------- #
# build_daily_report -- counts, unknown handling, totals, confidence
# --------------------------------------------------------------------------- #

def test_counts_by_every_axis_and_total_equals_sum():
    signals = _rows(
        {"symbol": "USDJPY", "side": "long", "market_state": "trend_up"},
        {"symbol": "USDJPY", "side": "short", "market_state": "range"},
        {"symbol": "XAUUSD", "side": "long", "market_state": "trend_up"},
    )
    report = dr.build_daily_report(signals, report_date=date(2026, 7, 13), timezone_name="UTC")

    assert report["total_signals"] == 3
    assert report["by_symbol"] == {"USDJPY": 2, "XAUUSD": 1}
    assert report["by_direction"] == {"long": 2, "short": 1}
    assert report["by_market_state"] == {"trend_up": 2, "range": 1}
    # Every axis must sum back to total_signals (acceptance: sum == Total Signals).
    for axis_name, _field in dr.COUNT_AXES:
        assert sum(report[axis_name].values()) == report["total_signals"], axis_name


def test_market_state_uses_market_state_not_bollinger_state():
    signals = _rows(
        {"state": "Inside band", "market_state": "trend_up"},
        {"state": "Inside band", "market_state": "range"},
    )
    report = dr.build_daily_report(signals, report_date=date(2026, 7, 13), timezone_name="UTC")

    assert report["by_market_state"] == {"range": 1, "trend_up": 1}
    assert "Inside band" not in report["by_market_state"]


def test_persisted_market_state_round_trips_into_report(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    store.save_signal(
        _make_signal(created_at_utc="2026-07-13T01:00:00Z", market_state="squeeze"),
        db_path=db_path,
    )

    rows = store.list_signals_in_range(
        "2026-07-13T00:00:00Z", "2026-07-14T00:00:00Z", db_path=db_path
    )
    report = dr.build_daily_report(rows, report_date=date(2026, 7, 13), timezone_name="UTC")
    assert report["by_market_state"] == {"squeeze": 1}


def test_null_empty_and_literal_unknown_all_fold_into_unknown_bucket():
    signals = _rows(
        {"market_state": None},
        {"market_state": ""},
        {"market_state": "   "},
        {"market_state": "unknown"},
        {"market_state": "trend_up"},
    )
    report = dr.build_daily_report(signals, report_date=date(2026, 7, 13), timezone_name="UTC")
    # NULL, empty, whitespace, and the literal string all count under "unknown";
    # none are dropped, so the axis still sums to the total.
    assert report["by_market_state"] == {"unknown": 4, "trend_up": 1}
    assert sum(report["by_market_state"].values()) == 5


def test_missing_axis_key_on_legacy_dict_counts_as_unknown():
    # A dict that predates a column (key entirely absent) must not raise and must
    # land in the unknown bucket rather than being dropped.
    signals = [{"symbol": "USDJPY", "side": "long", "confidence": 0.4}]
    report = dr.build_daily_report(signals, report_date=date(2026, 7, 13), timezone_name="UTC")
    assert report["by_timeframe"] == {"unknown": 1}
    assert report["by_market_state"] == {"unknown": 1}
    assert report["by_strategy"] == {"unknown": 1}


def test_counts_ordered_by_descending_count_then_key():
    signals = _rows(
        {"symbol": "BBB"},
        {"symbol": "AAA"},
        {"symbol": "AAA"},
        {"symbol": "CCC"},
    )
    report = dr.build_daily_report(signals, report_date=date(2026, 7, 13), timezone_name="UTC")
    # AAA first (count 2); BBB before CCC (tie broken by ascending key).
    assert list(report["by_symbol"].items()) == [("AAA", 2), ("BBB", 1), ("CCC", 1)]


def test_empty_day_is_all_empty_not_an_error():
    report = dr.build_daily_report([], report_date=date(2026, 7, 13), timezone_name="Asia/Tokyo")
    assert report["total_signals"] == 0
    for axis_name, _field in dr.COUNT_AXES:
        assert report[axis_name] == {}
    assert report["confidence"] is None
    # Still self-describing about the day/window it covers.
    assert report["date"] == "2026-07-13"
    assert report["timezone"] == "Asia/Tokyo"
    assert report["window_utc"]["start"] == "2026-07-12T15:00:00Z"


def test_confidence_stats_average_min_max():
    signals = _rows({"confidence": 0.4}, {"confidence": 0.6}, {"confidence": 0.8})
    report = dr.build_daily_report(signals, report_date=date(2026, 7, 13), timezone_name="UTC")
    assert report["confidence"]["count"] == 3
    assert report["confidence"]["average"] == pytest.approx(0.6)
    assert report["confidence"]["min"] == 0.4
    assert report["confidence"]["max"] == 0.8


def test_confidence_excludes_null_values_but_keeps_signal_in_totals():
    signals = _rows({"confidence": 0.4}, {"confidence": None})
    report = dr.build_daily_report(signals, report_date=date(2026, 7, 13), timezone_name="UTC")
    # The None-confidence signal still counts toward the total and every axis,
    # but is excluded from the confidence stats (never treated as 0.0).
    assert report["total_signals"] == 2
    assert report["confidence"]["count"] == 1
    assert report["confidence"]["average"] == 0.4


def test_confidence_is_none_when_no_signal_has_one():
    signals = _rows({"confidence": None}, {"confidence": None})
    report = dr.build_daily_report(signals, report_date=date(2026, 7, 13), timezone_name="UTC")
    assert report["confidence"] is None


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

def test_render_text_contains_headers_and_counts():
    signals = _rows(
        {"symbol": "USDJPY", "side": "long"},
        {"symbol": "XAUUSD", "side": "short"},
    )
    report = dr.build_daily_report(signals, report_date=date(2026, 7, 13), timezone_name="Asia/Tokyo")
    text = dr.render_text(report)
    assert "Paper Signal Daily Report" in text
    assert "Date: 2026-07-13 (Asia/Tokyo)" in text
    assert "Total Signals: 2" in text
    assert "By Symbol" in text
    assert "- USDJPY: 1" in text
    assert "By Direction" in text


def test_render_text_empty_day_shows_none_and_no_confidence():
    report = dr.build_daily_report([], report_date=date(2026, 7, 13), timezone_name="UTC")
    text = dr.render_text(report)
    assert "Total Signals: 0" in text
    assert "- (none)" in text
    assert "- (no confidence values)" in text


def test_render_markdown_has_tables_and_trailing_newline():
    signals = _rows({"symbol": "USDJPY"})
    report = dr.build_daily_report(signals, report_date=date(2026, 7, 13), timezone_name="UTC")
    md = dr.render_markdown(report)
    assert md.startswith("# Paper Signal Daily Report")
    assert "## By Symbol" in md
    assert "| Key | Count |" in md
    assert "| USDJPY | 1 |" in md
    assert md.endswith("\n")


@pytest.mark.parametrize("renderer", [dr.render_text, dr.render_markdown])
def test_both_formats_show_every_required_axis_and_confidence(renderer):
    report = dr.build_daily_report(
        _rows(
            {
                "symbol": "XAUUSD",
                "timeframe": "15m",
                "side": "short",
                "market_state": "squeeze",
                "strategy_profile": "squeeze_breakout",
                "confidence": 0.75,
            }
        ),
        report_date=date(2026, 7, 13),
        timezone_name="Asia/Tokyo",
    )
    rendered = renderer(report)

    assert "Total Signals" in rendered and "1" in rendered
    for title in dr.AXIS_TITLES.values():
        assert title in rendered
    for value in ("XAUUSD", "15m", "short", "squeeze", "squeeze_breakout"):
        assert value in rendered
    for label in ("Average", "Min", "Max"):
        assert label.lower() in rendered.lower()
    assert "0.7500" in rendered


def test_render_rejects_unknown_format():
    report = dr.build_daily_report([], report_date=date(2026, 7, 13), timezone_name="UTC")
    with pytest.raises(ValueError):
        dr.render(report, "json")


# --------------------------------------------------------------------------- #
# paper_signal_store.list_signals_in_range -- windowed, read-only
# --------------------------------------------------------------------------- #

def test_range_query_is_half_open_start_inclusive_end_exclusive(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    for ts, sym in (
        ("2026-07-13T00:00:00Z", "AT_START"),      # == start: included
        ("2026-07-13T12:00:00Z", "MIDDLE"),        # inside: included
        ("2026-07-14T00:00:00Z", "AT_END"),        # == end: excluded
    ):
        store.save_signal(_make_signal(created_at_utc=ts, symbol=sym), db_path=db_path)

    rows = store.list_signals_in_range("2026-07-13T00:00:00Z", "2026-07-14T00:00:00Z", db_path=db_path)
    symbols = {row["symbol"] for row in rows}
    assert symbols == {"AT_START", "MIDDLE"}
    assert "AT_END" not in symbols


def test_range_query_missing_db_returns_empty_without_creating_file(tmp_path):
    db_path = tmp_path / "does_not_exist.sqlite3"
    rows = store.list_signals_in_range("2026-07-13T00:00:00Z", "2026-07-14T00:00:00Z", db_path=db_path)
    assert rows == []
    # Read-only: a report for a day before any signal existed must not create the DB.
    assert not db_path.exists()


def test_range_query_orders_newest_first(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    store.save_signal(_make_signal(created_at_utc="2026-07-13T01:00:00Z", symbol="EARLY"), db_path=db_path)
    store.save_signal(_make_signal(created_at_utc="2026-07-13T09:00:00Z", symbol="LATE"), db_path=db_path)
    rows = store.list_signals_in_range("2026-07-13T00:00:00Z", "2026-07-14T00:00:00Z", db_path=db_path)
    assert [row["symbol"] for row in rows] == ["LATE", "EARLY"]


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #

def _seed_boundary_db(db_path):
    # Two signals straddling the 15:00Z JST day boundary, plus one in the next day.
    store.save_signal(_make_signal(created_at_utc="2026-07-12T15:30:00Z", symbol="USDJPY"), db_path=db_path)  # 07-13 JST
    store.save_signal(_make_signal(created_at_utc="2026-07-13T14:59:59Z", symbol="XAUUSD"), db_path=db_path)  # 07-13 JST
    store.save_signal(_make_signal(created_at_utc="2026-07-13T15:00:00Z", symbol="SPX"), db_path=db_path)     # 07-14 JST


def test_cli_selects_only_the_requested_jst_day(tmp_path, capsys):
    db_path = tmp_path / "paper_signals.sqlite3"
    _seed_boundary_db(db_path)

    rc = cli.main(["--date", "2026-07-13", "--timezone", "Asia/Tokyo", "--db-path", str(db_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Total Signals: 2" in out
    assert "USDJPY" in out and "XAUUSD" in out
    assert "SPX" not in out  # SPX is 2026-07-14 in JST


def test_cli_timezone_changes_the_day_a_signal_belongs_to(tmp_path, capsys):
    db_path = tmp_path / "paper_signals.sqlite3"
    _seed_boundary_db(db_path)

    # In UTC, the 2026-07-13T15:00:00Z SPX signal falls on 2026-07-13.
    rc = cli.main(["--date", "2026-07-13", "--timezone", "UTC", "--db-path", str(db_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "SPX" in out
    assert "USDJPY" not in out  # 2026-07-12T15:30Z is 2026-07-12 in UTC


def test_cli_writes_output_file_and_does_not_write_signal_db(tmp_path, capsys):
    # A valid-but-empty DB: the CLI must read it without mutating it. (Issue #71
    # made a *missing* DB a runtime error; see test_daily_report_scheduled.py.)
    db_path = tmp_path / "paper_signals.sqlite3"
    store.init_db(db_path)
    before = db_path.read_bytes()
    out_path = tmp_path / "nested" / "report.md"

    rc = cli.main([
        "--date", "2026-07-13", "--timezone", "UTC",
        "--db-path", str(db_path), "--format", "markdown", "--output", str(out_path),
    ])
    assert rc == 0
    assert out_path.exists()
    assert out_path.read_text(encoding="utf-8").startswith("# Paper Signal Daily Report")
    # Generating the report must not have modified the signal DB (read-only).
    assert db_path.read_bytes() == before
    assert f"Saved daily report to {out_path}" in capsys.readouterr().out


def test_cli_bad_timezone_is_a_usage_error(tmp_path, capsys):
    db_path = tmp_path / "paper_signals.sqlite3"
    rc = cli.main(["--date", "2026-07-13", "--timezone", "Not/AZone", "--db-path", str(db_path)])
    assert rc == cli.EXIT_USAGE_ERROR
    assert "unknown timezone" in capsys.readouterr().err


def test_cli_bad_date_rejected_by_argparse(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--date", "2026/07/13", "--db-path", str(tmp_path / "x.sqlite3")])
    assert excinfo.value.code == 2


def test_cli_env_timezone_used_when_flag_absent(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / "paper_signals.sqlite3"
    _seed_boundary_db(db_path)
    monkeypatch.setenv(cli.TIMEZONE_ENV_VAR, "UTC")

    # No --timezone: the env var supplies UTC, so SPX (15:00Z) is on 2026-07-13.
    rc = cli.main(["--date", "2026-07-13", "--db-path", str(db_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "(UTC)" in out
    assert "SPX" in out
