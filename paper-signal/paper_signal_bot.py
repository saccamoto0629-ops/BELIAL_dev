#!/usr/bin/env python
"""Resident-service entrypoint for the Paper Signal Generator (Issue #41).

This is a thin entrypoint only. All behaviour -- the strategy rules, the
confirmed-candle discipline, Market Bias / Chart Snapshot input-integrity,
candle-keyed idempotency, save-then-notify ordering, dry-run side-effect
freedom, and SIGTERM/SIGINT graceful shutdown -- lives in ``signal_generator``.

It exists so the systemd unit and operators have a stable, self-describing
entrypoint name (``berial-paper-signal.service`` -> ``paper_signal_bot.py``),
consistent with the other Berial bots (``macd_alert_bot.py``,
``news_feed_bot.py``, ...). It adds no logic of its own.

Usage (see docs/operations/operations.md):

    python paper_signal_bot.py            # resident loop (systemd ExecStart)
    python paper_signal_bot.py --once     # a single cycle, no sleep
    python paper_signal_bot.py --once --dry-run   # single cycle, no side effects
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from signal_generator import main  # noqa: E402


if __name__ == "__main__":
    main()
