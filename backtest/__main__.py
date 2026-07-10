"""Enables ``python -m backtest`` as an alias for ``python -m backtest.runner``."""

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from runner import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
