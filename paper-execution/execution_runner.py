"""Paper Execution runner / CLI (Issue #51 wiring).

*** PAPER EXECUTION. NOT A LIVE ORDER / BROKER LAYER. ***

Orchestration only -- the thin layer that connects three side-effect-free /
single-responsibility pieces:

  read Paper Signals (paper-signal/paper_signal_store, READ-ONLY)
    -> load candles (market-watch/chart_collector, READ-ONLY)
      -> pure Open/Close judgement (execution_engine)
        -> persist virtual positions (position_store)

It never generates a Paper Signal, never writes to the Paper Signal DB, and
never re-classifies Market Bias / Market State -- Execution logic is kept out of
Market Watch / Alert / Paper Signal, and those layers' logic is kept out of
here. It also never calls a broker, places an order, or sends a Discord message.

Idempotency (Issue #51): re-running is a no-op on already-processed work.
- Open  is skipped when ``get_position_by_signal`` already returns a position
  (repository guard), and is a hard DB error otherwise blocked by the UNIQUE
  ``paper_signal_id`` index (DB guard) -- so re-processing the same signal, or
  the same decision candle, never opens a second position.
- Close re-evaluates each still-open position against newly available candles;
  a position that already closed is protected by ``close_position``'s
  ``status = 'open'`` guard, so re-processing never double-closes.

dry-run (Issue #51 / BELIAL policy): with ``--dry-run`` NOTHING is persisted --
no DB schema creation, no position rows, no updates, no history events, no log
files, no Discord. Every Open/Close *decision* is computed and logged, but only
the read side (which never creates a DB) is exercised.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
MARKET_WATCH_DIR = REPO_ROOT / "market-watch"
PAPER_SIGNAL_DIR = REPO_ROOT / "paper-signal"
BACKTEST_DIR = REPO_ROOT / "backtest"
LOGS_DIR = REPO_ROOT / "logs"
LOG_FILE = LOGS_DIR / "paper_execution.log"

for _dir in (BASE_DIR, MARKET_WATCH_DIR, PAPER_SIGNAL_DIR, BACKTEST_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import execution_engine as engine  # noqa: E402
import execution_model as execution  # noqa: E402  (backtest: fixed spread/slippage fill model)
import position_models as models  # noqa: E402
import position_store as store  # noqa: E402
import chart_collector  # noqa: E402  (market-watch, READ-ONLY load_candles_range)
import paper_signal_store as signal_store  # noqa: E402  (paper-signal, READ-ONLY)

# --- logging: console-only on a dry run (never create logs/, never open a file) ---
_console_only_logging = False
_logger_instance: logging.Logger | None = None


def configure_logging(*, dry_run: bool) -> None:
    global _console_only_logging, _logger_instance
    if _logger_instance is not None and _console_only_logging != dry_run:
        for handler in list(_logger_instance.handlers):
            _logger_instance.removeHandler(handler)
            handler.close()
        _logger_instance = None
    _console_only_logging = dry_run


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("paper_execution")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    if not _console_only_logging:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        from logging.handlers import RotatingFileHandler

        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger


def _get_logger() -> logging.Logger:
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = _setup_logger()
    return _logger_instance


@dataclass(frozen=True)
class ExecutionConfig:
    """Fixed, deterministic Open/Close parameters for one runner invocation.

    Defaults mirror the Backtest vocabulary (see execution_engine defaults). The
    Execution Model (spread/slippage) is built from ``spread``/``slippage`` and
    reused for both entry and exit corrections; ``fee`` is a flat commission
    subtracted only into ``net_pnl``.
    """

    stop_loss_pct: float = engine.DEFAULT_STOP_LOSS_PCT
    risk_reward: float = engine.DEFAULT_RISK_REWARD
    time_exit_bars: int = engine.DEFAULT_TIME_EXIT_BARS
    spread: float = 0.0
    slippage: float = 0.0
    fee: float = 0.0

    def exec_model(self) -> "execution.ExecutionModel":
        return execution.ExecutionModel(spread=self.spread, slippage=self.slippage)


def _chart_symbol(symbol: str) -> str:
    """Map a signal symbol (e.g. 'USD_JPY') to the Chart DB candle key ('USDJPY').

    Mirrors ``backtest/runner._chart_symbol`` so Paper Execution resolves candles
    with exactly the same naming convention as the Backtest Engine.
    """
    return symbol.replace("_", "").upper()


def _to_utc_timestamp(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _default_load_candles(chart_db_path: Path, symbol: str, timeframe: str) -> pd.DataFrame:
    """Read-only candle load for one symbol/timeframe (never creates the DB)."""
    return chart_collector.load_candles_range(chart_db_path, _chart_symbol(symbol), timeframe)


def _entry_and_future(candles: pd.DataFrame, target_candle_utc: str) -> tuple[float, pd.DataFrame] | None:
    """Return (entry_close, bars-strictly-after-entry) for the confirmed candle.

    The entry candle is the Paper Signal's own confirmed ``target_candle_utc``
    (the same bar the signal was generated for); the position enters at that
    bar's close and can only be exposed to *subsequent* bars -- exactly the
    "enter at bar close, exit walk starts at t+1" timing the Backtest uses.
    Returns ``None`` when that candle is not present in the loaded data.
    """
    if candles is None or len(candles) == 0:
        return None
    target_ts = _to_utc_timestamp(target_candle_utc)
    index_utc = [_to_utc_timestamp(ts) for ts in candles.index]
    try:
        position = index_utc.index(target_ts)
    except ValueError:
        return None
    entry_close = float(candles.iloc[position]["close"])
    future_candles = candles.iloc[position + 1 :]
    return entry_close, future_candles


def process_signal(
    signal: dict,
    candles: pd.DataFrame,
    *,
    config: ExecutionConfig,
    dry_run: bool,
    db_path: Path,
) -> dict | None:
    """Open (and, if the candles allow, immediately Close) one Paper Signal.

    Returns the resulting position record (open or closed), or ``None`` when the
    signal could not be processed (already opened, missing entry candle, or the
    Open condition was not met). Idempotent: an already-opened signal is skipped
    via the repository probe.
    """
    logger = _get_logger()
    paper_signal_id = signal.get("signal_id")
    symbol = signal.get("symbol")
    timeframe = signal.get("timeframe")
    side = signal.get("side")
    strategy_profile = signal.get("strategy_profile", "")
    target_candle = signal.get("target_candle_utc")

    if not paper_signal_id or not target_candle:
        logger.info("Skip signal: missing signal_id/target_candle (symbol=%s tf=%s)", symbol, timeframe)
        return None

    # Repository-level Open idempotency: never re-open a signal already tied to a
    # position. Read-only -- in dry-run this touches only the read path (which
    # never creates the DB).
    existing = store.get_position_by_signal(paper_signal_id, db_path=db_path)
    if existing is not None:
        logger.info("Skip signal %s: already has position %s", paper_signal_id, existing["position_id"])
        return None

    resolved = _entry_and_future(candles, target_candle)
    if resolved is None:
        logger.info("Skip signal %s: entry candle %s not in loaded data", paper_signal_id, target_candle)
        return None
    entry_close, future_candles = resolved

    exec_model = config.exec_model()
    open_decision = engine.decide_open(
        side=side,
        entry_candle_close=entry_close,
        exec_model=exec_model,
        stop_loss_pct=config.stop_loss_pct,
        risk_reward=config.risk_reward,
    )
    if open_decision is None:
        logger.info("Open condition not met for signal %s (side=%s)", paper_signal_id, side)
        return None

    close_decision = engine.evaluate_close(
        engine.PositionView(
            side=open_decision.side,
            entry_price=open_decision.entry_price,
            stop_loss=open_decision.stop_loss,
            take_profit=open_decision.take_profit,
            size=open_decision.size,
        ),
        future_candles,
        exec_model=exec_model,
        time_exit_bars=config.time_exit_bars,
        fee=config.fee,
    )

    if dry_run:
        logger.info(
            "[DRY_RUN] would OPEN %s %s %s entry=%.5f stop=%.5f target=%.5f (signal=%s); "
            "would %s",
            symbol, timeframe, open_decision.side, open_decision.entry_price,
            open_decision.stop_loss, open_decision.take_profit, paper_signal_id,
            (
                f"CLOSE reason={close_decision.close_reason} exit={close_decision.exit_price:.5f} "
                f"net_pnl={close_decision.net_pnl:.5f}"
                if close_decision is not None
                else "stay OPEN (no exit within loaded candles)"
            ),
        )
        return None

    position = models.create_virtual_position(
        paper_signal_id=paper_signal_id,
        strategy_profile=strategy_profile,
        symbol=symbol,
        timeframe=timeframe,
        side=open_decision.side,
        entry_price=open_decision.entry_price,
        opened_at=target_candle,
        size=open_decision.size,
        stop_loss=open_decision.stop_loss,
        take_profit=open_decision.take_profit,
        entry_signal_time=target_candle,
    )
    store.create_position(position, db_path=db_path)
    logger.info(
        "OPEN %s %s %s entry=%.5f (position=%s signal=%s)",
        symbol, timeframe, open_decision.side, open_decision.entry_price,
        position["position_id"], paper_signal_id,
    )

    if close_decision is not None:
        return _close(position["position_id"], close_decision, db_path=db_path)
    return store.get_position(position["position_id"], db_path=db_path)


def evaluate_open_position(
    position: dict,
    candles: pd.DataFrame,
    *,
    config: ExecutionConfig,
    dry_run: bool,
    db_path: Path,
) -> dict | None:
    """Re-evaluate one already-open position against (possibly newer) candles.

    This is the "candle更新ごとにopen positionを評価する" path: an Open position
    from a previous run is walked again over the bars after its entry, and closed
    if an exit now fires. Double-close is impossible (``close_position`` guards on
    ``status = 'open'``). Returns the closed position, or ``None`` when it stays
    open / cannot be evaluated.
    """
    logger = _get_logger()
    resolved = _entry_and_future(candles, position["opened_at"])
    if resolved is None:
        return None
    _entry_close, future_candles = resolved

    exec_model = config.exec_model()
    close_decision = engine.evaluate_close(
        engine.PositionView(
            side=position["side"],
            entry_price=position["entry_price"],
            stop_loss=position["stop_loss"],
            take_profit=position["take_profit"],
            size=position["size"],
        ),
        future_candles,
        exec_model=exec_model,
        time_exit_bars=config.time_exit_bars,
        fee=config.fee,
    )
    if close_decision is None:
        return None

    if dry_run:
        logger.info(
            "[DRY_RUN] would CLOSE position %s reason=%s exit=%.5f net_pnl=%.5f",
            position["position_id"], close_decision.close_reason,
            close_decision.exit_price, close_decision.net_pnl,
        )
        return None
    return _close(position["position_id"], close_decision, db_path=db_path)


def _close(position_id: str, close_decision: engine.CloseDecision, *, db_path: Path) -> dict:
    logger = _get_logger()
    closed = store.close_position(
        position_id,
        exit_price=close_decision.exit_price,
        closed_at=close_decision.closed_at,
        close_reason=close_decision.close_reason,
        gross_pnl=close_decision.gross_pnl,
        net_pnl=close_decision.net_pnl,
        spread_cost=close_decision.spread_cost,
        slippage_cost=close_decision.slippage_cost,
        fee_cost=close_decision.fee_cost,
        r_multiple=close_decision.r_multiple,
        db_path=db_path,
    )
    logger.info(
        "CLOSE %s reason=%s exit=%.5f gross=%.5f net=%.5f r=%s",
        position_id, close_decision.close_reason, close_decision.exit_price,
        close_decision.gross_pnl, close_decision.net_pnl, close_decision.r_multiple,
    )
    return closed


def run_once(
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
    signal_db_path: Path | None = None,
    chart_db_path: Path | None = None,
    config: ExecutionConfig | None = None,
    signals: list[dict] | None = None,
    load_candles=None,
) -> list[dict]:
    """Run one Paper Execution cycle: open new signals, then re-evaluate open ones.

    ``signals`` / ``load_candles`` are injectable so tests can drive the runner
    with synthetic inputs; by default signals are read from the Paper Signal DB
    (READ-ONLY) and candles from the Chart DB (READ-ONLY). Returns the position
    records touched this cycle (created and/or closed). In ``dry_run`` no DB is
    ever written and the returned list is empty (decisions are logged only).
    """
    configure_logging(dry_run=dry_run)
    logger = _get_logger()

    db_path = Path(db_path) if db_path is not None else store.position_db_path()
    signal_db_path = Path(signal_db_path) if signal_db_path is not None else signal_store.DEFAULT_DB_PATH
    chart_db_path = Path(chart_db_path) if chart_db_path is not None else chart_collector.chart_db_path()
    config = config or ExecutionConfig()
    if load_candles is None:
        def load_candles(symbol: str, timeframe: str) -> pd.DataFrame:  # noqa: E306
            return _default_load_candles(chart_db_path, symbol, timeframe)

    if signals is None:
        # READ-ONLY: never writes to the Paper Signal DB. list_signals() would
        # call init_db on that DB, so in dry-run only read a pre-existing file.
        if dry_run and not Path(signal_db_path).exists():
            signals = []
        else:
            signals = signal_store.list_signals(db_path=signal_db_path)

    candle_cache: dict[tuple[str, str], pd.DataFrame] = {}

    def candles_for(symbol: str, timeframe: str) -> pd.DataFrame:
        key = (symbol, timeframe)
        if key not in candle_cache:
            candle_cache[key] = load_candles(symbol, timeframe)
        return candle_cache[key]

    touched: list[dict] = []

    # 1. Open new signals (and close them immediately if an exit is already
    #    present in the loaded candles).
    for signal in signals:
        symbol = signal.get("symbol")
        timeframe = signal.get("timeframe")
        if not symbol or not timeframe:
            continue
        try:
            result = process_signal(
                signal, candles_for(symbol, timeframe), config=config, dry_run=dry_run, db_path=db_path
            )
        except Exception as exc:  # one signal's failure must not stop the others
            logger.error("Paper Execution open failed for signal %s: %s", signal.get("signal_id"), exc)
            continue
        if result is not None:
            touched.append(result)

    # 2. Re-evaluate positions still open (from this or a previous run) against
    #    the latest candles. Skipped in dry-run reads that found no DB file.
    if not (dry_run and not Path(db_path).exists()):
        for position in store.list_open_positions(db_path=db_path):
            try:
                closed = evaluate_open_position(
                    position, candles_for(position["symbol"], position["timeframe"]),
                    config=config, dry_run=dry_run, db_path=db_path,
                )
            except Exception as exc:
                logger.error("Paper Execution close failed for position %s: %s", position["position_id"], exc)
                continue
            if closed is not None:
                touched.append(closed)

    return touched


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="paper_execution",
        description=(
            "Manage paper-only virtual positions from Paper Signals + candles. "
            "Never places an order, calls a broker, or sends Discord."
        ),
    )
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit (default).")
    parser.add_argument(
        "--dry-run", action="store_true", help="Log decisions only; create/update no DB, no files."
    )
    parser.add_argument("--stop-loss-pct", type=float, default=engine.DEFAULT_STOP_LOSS_PCT)
    parser.add_argument("--risk-reward", type=float, default=engine.DEFAULT_RISK_REWARD)
    parser.add_argument("--time-exit-bars", type=int, default=engine.DEFAULT_TIME_EXIT_BARS)
    parser.add_argument("--spread", type=float, default=0.0)
    parser.add_argument("--slippage", type=float, default=0.0)
    parser.add_argument("--fee", type=float, default=0.0)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    configure_logging(dry_run=args.dry_run)
    config = ExecutionConfig(
        stop_loss_pct=args.stop_loss_pct,
        risk_reward=args.risk_reward,
        time_exit_bars=args.time_exit_bars,
        spread=args.spread,
        slippage=args.slippage,
        fee=args.fee,
    )
    touched = run_once(dry_run=args.dry_run, config=config)
    _get_logger().info("Paper Execution cycle done: %d position(s) touched.", len(touched))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
