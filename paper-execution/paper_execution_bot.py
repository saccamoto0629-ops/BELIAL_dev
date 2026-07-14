#!/usr/bin/env python
"""Entrypoint for the Paper Execution runner (Issue #51).

A thin, self-describing entrypoint name -- consistent with the other Berial
bots (``paper_signal_bot.py``, ``macd_alert_bot.py``, ...). All behaviour lives
in ``execution_runner``; this file adds no logic of its own.

Usage:

    python paper_execution_bot.py --once            # one cycle
    python paper_execution_bot.py --once --dry-run  # one cycle, no side effects
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from execution_runner import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
