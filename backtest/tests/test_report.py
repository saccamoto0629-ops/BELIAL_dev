import json
import pathlib
import sys

import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import report  # noqa: E402
import summary  # noqa: E402


def sample_summary():
    signals = [
        {
            "id": "s1", "run_id": "run-1", "signal_time": "2026-01-01T00:00:00Z",
            "bias": "Bull", "signal_type": "paper_signal", "timeframe": "1h",
            "result_status": "win", "pnl": 2.0, "closed_at": "2026-01-01T01:00:00Z",
        },
        {
            "id": "s2", "run_id": "run-1", "signal_time": "2026-01-01T01:00:00Z",
            "bias": "Bear", "signal_type": "paper_signal", "timeframe": "1h",
            "result_status": "loss", "pnl": -1.0, "closed_at": "2026-01-01T02:00:00Z",
        },
    ]
    return summary.build_summary("run-1", signals)


# =============================================================================
# render_json
# =============================================================================


def test_render_json_is_valid_json_with_expected_fields():
    text = report.render_json(sample_summary())
    parsed = json.loads(text)

    assert parsed["run_id"] == "run-1"
    assert parsed["total_signals"] == 2
    assert parsed["win_count"] == 1
    assert parsed["loss_count"] == 1


def test_render_json_raises_instead_of_emitting_nan_or_infinity():
    broken = {"run_id": "x", "total_signals": 0, "profit_factor": float("inf")}
    with pytest.raises(ValueError):
        report.render_json(broken)


# =============================================================================
# render_markdown
# =============================================================================


def test_render_markdown_contains_run_id_heading_and_metrics_table():
    text = report.render_markdown(sample_summary())

    assert text.startswith("# Backtest Summary: run-1")
    assert "| Total signals | 2 |" in text
    assert "| Win count | 1 |" in text
    assert "| Loss count | 1 |" in text
    assert "## by_bias" in text
    assert "| Bull |" in text
    assert "| Bear |" in text


def test_render_markdown_handles_zero_signals_without_error():
    text = report.render_markdown(summary.build_summary("run-empty", []))

    assert "run-empty" in text
    assert "(no data)" in text  # empty breakdown buckets are shown, not omitted


# =============================================================================
# render_text
# =============================================================================


def test_render_text_contains_key_metrics():
    text = report.render_text(sample_summary())

    assert "Backtest Signal Summary" in text
    assert "Total signals:       2" in text
    assert "Win / Loss:          1 / 1" in text


def test_render_text_shows_na_for_none_values():
    text = report.render_text(summary.build_summary("run-empty", []))

    assert "n/a" in text  # average_holding_bars is None


# =============================================================================
# render(): format dispatch
# =============================================================================


def test_render_dispatches_by_format_name():
    result = sample_summary()

    assert report.render(result, "json") == report.render_json(result)
    assert report.render(result, "markdown") == report.render_markdown(result)
    assert report.render(result, "text") == report.render_text(result)


def test_render_rejects_unknown_format():
    with pytest.raises(ValueError):
        report.render(sample_summary(), "yaml")


# =============================================================================
# save_summary: file output
# =============================================================================


def test_save_summary_creates_parent_directories(tmp_path):
    output_path = tmp_path / "reports" / "nested" / "run_1.json"

    saved = report.save_summary("{}", output_path)

    assert saved == output_path
    assert output_path.exists()
    assert output_path.read_text(encoding="utf-8") == "{}"


def test_save_summary_overwrites_an_existing_file(tmp_path):
    output_path = tmp_path / "run_1.md"
    output_path.write_text("old content", encoding="utf-8")

    report.save_summary("new content", output_path)

    assert output_path.read_text(encoding="utf-8") == "new content"
