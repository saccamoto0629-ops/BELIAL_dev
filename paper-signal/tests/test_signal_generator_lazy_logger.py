"""Issue #31 regression: `import signal_generator` must not initialize logging.

`signal_generator.py` used to run `logger = setup_logger()` at module import
time, which unconditionally created `logs/` and attached a RotatingFileHandler
as an import-time side effect. These tests prove the logger is now lazily
created on first actual use (`_get_logger()`), never merely by importing the
module, while `run_once()` still has a working logger and generation
functions never touch logging at all.

The "import alone" checks run in a fresh subprocess: `signal_generator` is
already imported by other test modules in this same pytest session (which
legitimately calls `run_once()` and does initialize the logger), so only a
brand-new interpreter process can observe a truly pristine, not-yet-logged-to
import.
"""

from __future__ import annotations

import logging
import os
import pathlib
import subprocess
import sys

import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import signal_generator as sg  # noqa: E402
from signal_models import create_signal_context  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_chart_db(monkeypatch, tmp_path):
    # Issue #55: run_once consults the Chart DB for snapshot attachment; point
    # it at a nonexistent tmp path so no test ever reads or writes a real one.
    monkeypatch.setenv("CHART_DB_PATH", str(tmp_path / "chart_data.sqlite3"))


def _run_subprocess_script(script: str) -> subprocess.CompletedProcess:
    """Run *script* in a fresh interpreter with only paper-signal on PYTHONPATH.

    signal_generator.py bootstraps its own sys.path for news-feed-bot (and
    market_snapshot.py in turn bootstraps market-watch), so only the real
    paper-signal directory needs to be on PYTHONPATH here. The parent
    process's environment is inherited (not replaced) -- on Windows, missing
    system variables (e.g. SYSTEMROOT) make dependencies that touch
    networking/async I/O (yfinance -> curl_cffi -> asyncio) fail to import.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BOT_DIR)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(BOT_DIR),
        env=env,
        timeout=60,
    )


# --- import alone: no logs/ directory, no setup_logger() call --------------


def test_import_alone_does_not_create_logs_directory():
    # Patch Path.mkdir to fail loudly if anything during import tries to
    # create a directory -- setup_logger()'s very first statement is
    # LOGS_DIR.mkdir(...), so this fails immediately if import still
    # eagerly initializes the logger.
    script = (
        "import pathlib\n"
        "def _fail_mkdir(self, *a, **k):\n"
        "    raise AssertionError(f'Path.mkdir() must not be called during import: {self}')\n"
        "pathlib.Path.mkdir = _fail_mkdir\n"
        "import signal_generator\n"
        "print('IMPORT_OK')\n"
    )

    result = _run_subprocess_script(script)

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "IMPORT_OK" in result.stdout


def test_import_alone_does_not_call_setup_logger_or_attach_handlers():
    script = (
        "import logging\n"
        "import signal_generator\n"
        "assert signal_generator._logger_instance is None, 'logger must not be initialized at import time'\n"
        "handlers = logging.getLogger('paper_signal_generator').handlers\n"
        "assert handlers == [], f'no handlers should be attached at import time, got {handlers!r}'\n"
        "print('NO_LOGGER_OK')\n"
    )

    result = _run_subprocess_script(script)

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "NO_LOGGER_OK" in result.stdout


# --- logger is lazily available exactly when a logging call happens --------


@pytest.fixture
def reset_logger_state():
    """Reset the module logger cache so a test can observe first-use init."""
    original_instance = sg._logger_instance
    original_handlers = list(logging.getLogger("paper_signal_generator").handlers)
    sg._logger_instance = None
    logging.getLogger("paper_signal_generator").handlers = []
    yield
    sg._logger_instance = original_instance
    logging.getLogger("paper_signal_generator").handlers = original_handlers


def test_get_logger_initializes_on_first_call(reset_logger_state):
    assert sg._logger_instance is None

    logger = sg._get_logger()

    assert logger is not None
    assert sg._logger_instance is logger
    assert logger.handlers  # RotatingFileHandler + StreamHandler attached


def test_get_logger_does_not_reinitialize_or_duplicate_handlers(reset_logger_state):
    first = sg._get_logger()
    handler_count = len(first.handlers)

    second = sg._get_logger()

    assert second is first  # cached, not rebuilt
    assert len(second.handlers) == handler_count  # no duplicate handler registration


def make_chart_snapshot(**overrides):
    snapshot = {
        "symbol": "USDJPY",
        "timeframe": "1h",
        # Issue #41: confirmed-candle open timestamp (past -> always closed).
        "timestamp": "2026-07-05T12:00:00Z",
        "trend": "Bull",
        "state": "Inside band",
        "freshness": "Fresh",
        "bias_weight": 1.0,
        "macd_state": "Bullish",
    }
    snapshot.update(overrides)
    return snapshot


def make_bias(market_bias="Bull", confidence=0.72):
    return {"market_bias": market_bias, "confidence": confidence, "scores": {}, "reasons": [], "warnings": [], "note": ""}


def test_run_once_makes_the_logger_available(reset_logger_state, monkeypatch):
    sources = {
        "market": {},
        "fred": None,
        "tradingview": {},
        "chart": [make_chart_snapshot(macd_state="Bullish", freshness="Fresh", bias_weight=1.0)],
    }
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda **kw: sources)
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias(market_bias="Bull", confidence=0.8))

    assert sg._logger_instance is None  # pristine before run_once()

    created = sg.run_once(dry_run=True)

    assert len(created) == 1
    assert sg._logger_instance is not None  # the real run_once() log path initialized it
    assert sg._logger_instance.handlers


def test_generate_signal_candidates_does_not_touch_logger(reset_logger_state):
    context = create_signal_context(
        make_bias("Bull", 0.8),
        [make_chart_snapshot(macd_state="Bullish", freshness="Fresh", bias_weight=1.0)],
        confidence_threshold=0.40,
    )

    candidates = sg.generate_signal_candidates(context)

    assert len(candidates) == 1
    assert sg._logger_instance is None  # generation never calls _get_logger()


def test_generate_signal_does_not_touch_logger(reset_logger_state):
    context = create_signal_context(
        make_bias("Bull", 0.8),
        [make_chart_snapshot(macd_state="Bullish", freshness="Fresh", bias_weight=1.0)],
        confidence_threshold=0.40,
    )

    signal = sg.generate_signal(context)

    assert signal is not None
    assert sg._logger_instance is None


# --- dry-run remains free of persistence/network side effects --------------


def test_run_once_dry_run_still_has_no_db_or_http_side_effects(reset_logger_state, tmp_path, monkeypatch):
    db_path = tmp_path / "paper_signals.sqlite3"
    sources = {
        "market": {},
        "fred": None,
        "tradingview": {},
        "chart": [make_chart_snapshot(macd_state="Bullish", freshness="Fresh", bias_weight=1.0)],
    }
    monkeypatch.setattr(sg, "fetch_snapshot_sources", lambda **kw: sources)
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias(market_bias="Bull", confidence=0.8))

    class FailingSession:
        def post(self, *args, **kwargs):
            raise AssertionError("HTTP post must not be called in dry-run")

    monkeypatch.setattr(sg.requests, "post", FailingSession().post)

    created = sg.run_once(dry_run=True, db_path=db_path)

    assert len(created) == 1
    assert not db_path.exists()
