"""Paper Execution runner / CLI (Issue #49/#51 wiring + resident contract).

*** PAPER EXECUTION. NOT A LIVE ORDER / BROKER LAYER. ***

Orchestration only. Each cycle:

  1. read Paper Signals (READ-ONLY, never writes/migrates the Signal DB) and,
     for each not-yet-tracked signal, store a ``pending`` position -- or a
     terminal ``cancelled`` position for an invalid signal (never a silent skip).
  2. evaluate ``pending`` positions against confirmed candles: open (next
     confirmed candle's open), expire (entry timeout / weekend carry-over), or
     keep waiting.
  3. evaluate ``open`` positions against confirmed candles past their watermark:
     close (SL/TP/max-hold) or advance the watermark -- both CAS-guarded.

Failure handling (Issue #49): failures are COUNTED, not swallowed. A read /
write / migration / close error on one item is logged and the cycle continues,
but ``run_once`` returns a non-zero ``failures`` count and ``--once`` exits
non-zero. A ``sqlite3.OperationalError`` is never converted into "no data".

dry-run: computes and logs every decision but performs NO writes -- no schema
creation, no migration, no row insert/update, no Signal-DB write, no log file.
"""

from __future__ import annotations

import argparse
import logging
import signal as signal_module
import sqlite3
import sys
import threading
from dataclasses import dataclass, field
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
import execution_profile as profiles  # noqa: E402
import position_models as models  # noqa: E402
import position_store as store  # noqa: E402
import chart_collector  # noqa: E402  (market-watch, READ-ONLY)
from freshness import parse_timestamp_utc, select_confirmed_candles  # noqa: E402

logger_name = "paper_execution"
DEFAULT_CHECK_INTERVAL_SECONDS = 900
PAPER_ONLY_STATUS = "paper_only"

EXIT_OK = 0
EXIT_FAILURE = 1

# --- logging (console-only on dry run: never creates logs/ or a logfile) -----
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
    logger = logging.getLogger(logger_name)
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
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    return logger


def _get_logger() -> logging.Logger:
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = _setup_logger()
    return _logger_instance


@dataclass
class CycleResult:
    """Outcome of one Paper Execution cycle."""

    failures: int = 0
    pending_created: int = 0
    cancelled: int = 0
    opened: int = 0
    expired: int = 0
    closed: int = 0
    touched: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failures == 0


def _chart_symbol(symbol: str) -> str:
    return symbol.replace("_", "").upper()


def read_paper_signals(signal_db_path: Path) -> list[dict]:
    """READ-ONLY read of the Paper Signal DB (never creates/migrates it).

    Respects the Issue #49 boundary "read the stored records only, never write
    the Signal DB". A missing file yields no signals; an OperationalError on an
    existing file is NOT swallowed -- it propagates so the runner fails loudly.
    """
    path = Path(signal_db_path)
    if not path.exists():
        return []
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM paper_signals ORDER BY created_at_utc ASC").fetchall()
    return [dict(row) for row in rows]


def _profile_from_record(record: dict) -> "profiles.ExecutionProfile":
    """Reconstruct the execution profile from a position's stored snapshot.

    Close / entry evaluation always uses THIS (the frozen snapshot), never the
    live CLI/config, so changing a flag later cannot alter an existing position.
    """
    return profiles.ExecutionProfile(
        name=record["execution_profile"],
        max_entry_delay_bars=record["max_entry_delay_bars"],
        max_hold_bars=record["max_hold_bars"],
        same_candle_priority=record["same_candle_priority"],
        fallback_stop_pct=record["fallback_stop_pct"],
        risk_reward=record["risk_reward"],
        size=record["size"],
        spread=record["spread"],
        slippage=record["slippage"],
        commission=record["commission"],
    )


def _is_valid_signal(signal: dict) -> bool:
    if signal.get("status") != PAPER_ONLY_STATUS:
        return False
    if signal.get("side") not in models.KNOWN_SIDES:
        return False
    if not signal.get("signal_id") or not signal.get("symbol") or not signal.get("timeframe"):
        return False
    target = signal.get("target_candle_utc")
    if not target:
        return False
    try:
        parse_timestamp_utc(target)
    except (ValueError, TypeError):
        return False
    return True


def _candles_for_close(candles: pd.DataFrame, entry_time_utc: str, watermark: str | None) -> pd.DataFrame:
    """Confirmed candles to evaluate: entry candle inclusive (watermark None),
    else strictly after the watermark."""
    if candles is None or len(candles) == 0:
        return candles
    if watermark is None:
        lower = parse_timestamp_utc(entry_time_utc)
        keep = [parse_timestamp_utc(ts) >= lower for ts in candles.index]
    else:
        lower = parse_timestamp_utc(watermark)
        keep = [parse_timestamp_utc(ts) > lower for ts in candles.index]
    return candles[keep]


# --- per-phase processing ----------------------------------------------------


def _process_signal(signal: dict, *, dry_run: bool, db_path: Path, profile_for) -> str | None:
    """Store one signal as pending (or cancelled). Returns the action, or None if skipped."""
    logger = _get_logger()
    signal_id = signal.get("signal_id") or ""
    # Idempotency: one position per signal (repository probe + UNIQUE index).
    if signal_id and store.get_position_by_signal(signal_id, db_path=db_path) is not None:
        return None

    if not _is_valid_signal(signal):
        symbol = signal.get("symbol") or ""
        timeframe = signal.get("timeframe") or ""
        profile = profile_for(symbol, timeframe)
        if dry_run:
            logger.info("[DRY_RUN] would CANCEL invalid signal %s (invalid_input)", signal_id or "<no id>")
            return "cancelled"
        record = models.create_cancelled_position(
            signal_id=signal_id or models.new_position_id(),
            symbol=symbol, timeframe=timeframe, side=signal.get("side") or "",
            strategy_profile=signal.get("strategy_profile") or "", profile=profile,
            signal_candle_utc=signal.get("target_candle_utc") or "",
            entry_deadline_utc="", terminal_reason=models.REASON_INVALID_INPUT,
        )
        return "cancelled" if store.create_position(record, db_path=db_path) else None

    symbol = signal["symbol"]
    timeframe = signal["timeframe"]
    profile = profile_for(symbol, timeframe)
    entry_deadline = engine.compute_entry_deadline(
        signal["target_candle_utc"], timeframe, profile.max_entry_delay_bars
    )
    if dry_run:
        logger.info(
            "[DRY_RUN] would create PENDING for signal %s (%s %s %s) deadline=%s",
            signal_id, symbol, timeframe, signal["side"], entry_deadline,
        )
        return "pending"
    record = models.create_pending_position(
        signal_id=signal_id, symbol=symbol, timeframe=timeframe, side=signal["side"],
        strategy_profile=signal.get("strategy_profile") or "", profile=profile,
        signal_candle_utc=signal["target_candle_utc"], entry_deadline_utc=entry_deadline,
    )
    return "pending" if store.create_position(record, db_path=db_path) else None


def _process_pending(position: dict, candles: pd.DataFrame, *, dry_run: bool, db_path: Path, now_utc) -> str | None:
    logger = _get_logger()
    profile = _profile_from_record(position)
    outcome = engine.resolve_entry(
        side=position["side"], timeframe=position["timeframe"],
        signal_candle_utc=position["signal_candle_utc"], entry_deadline_utc=position["entry_deadline_utc"],
        candles=candles, profile=profile, now_utc=now_utc,
    )
    if outcome.action == engine.ENTRY_ACTION_PENDING:
        return None
    if outcome.action == engine.ENTRY_ACTION_EXPIRED:
        if dry_run:
            logger.info("[DRY_RUN] would EXPIRE pending %s (entry_timeout)", position["position_id"])
            return "expired"
        store.transition_pending_terminal(
            position["position_id"], state=models.STATE_EXPIRED,
            terminal_reason=models.REASON_ENTRY_TIMEOUT, db_path=db_path,
        )
        return "expired"
    # OPEN
    if dry_run:
        logger.info(
            "[DRY_RUN] would OPEN %s entry=%.5f @%s stop=%.5f target=%.5f",
            position["position_id"], outcome.entry_price, outcome.entry_time_utc,
            outcome.stop_loss_price, outcome.take_profit_price,
        )
        return "opened"
    store.transition_to_open(
        position["position_id"], entry_price=outcome.entry_price, entry_time_utc=outcome.entry_time_utc,
        stop_loss_price=outcome.stop_loss_price, take_profit_price=outcome.take_profit_price,
        max_hold_deadline_utc=outcome.max_hold_deadline_utc, db_path=db_path,
    )
    return "opened"


def _process_open(position: dict, candles: pd.DataFrame, *, dry_run: bool, db_path: Path) -> str | None:
    logger = _get_logger()
    profile = _profile_from_record(position)
    watermark = position["last_evaluated_candle_utc"]
    to_eval = _candles_for_close(candles, position["entry_time_utc"], watermark)
    view = engine.OpenPositionView(
        side=position["side"], timeframe=position["timeframe"], entry_price=position["entry_price"],
        stop_loss_price=position["stop_loss_price"], take_profit_price=position["take_profit_price"],
        max_hold_deadline_utc=position["max_hold_deadline_utc"],
        same_candle_priority=profile.same_candle_priority, size=profile.size,
        spread=profile.spread, slippage=profile.slippage, commission=profile.commission,
    )
    result = engine.evaluate_close(view, to_eval)
    if result.new_watermark is None:
        return None  # no new confirmed candles to evaluate
    if dry_run:
        if result.closed:
            logger.info(
                "[DRY_RUN] would CLOSE %s reason=%s exit=%.5f gross=%.5f net=%.5f",
                position["position_id"], result.terminal_reason, result.close_price,
                result.gross_pnl, result.net_pnl,
            )
            return "closed"
        return None
    if result.closed:
        try:
            store.close_open_position(
                position["position_id"], expected_watermark=watermark, new_watermark=result.new_watermark,
                close_price=result.close_price, close_time_utc=result.close_time_utc,
                terminal_reason=result.terminal_reason, gross_pnl=result.gross_pnl,
                net_pnl=result.net_pnl, r_multiple=result.r_multiple, db_path=db_path,
            )
        except store.PositionConcurrentModificationError as exc:
            logger.info("Skip close for %s: %s", position["position_id"], exc)
            return None
        return "closed"
    store.advance_watermark(
        position["position_id"], expected_watermark=watermark, new_watermark=result.new_watermark, db_path=db_path,
    )
    return None


def run_once(
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
    signal_db_path: Path | None = None,
    chart_db_path: Path | None = None,
    profile: "profiles.ExecutionProfile | None" = None,
    profile_overrides: dict | None = None,
    now_utc=None,
    signals: list[dict] | None = None,
    load_candles=None,
) -> CycleResult:
    """Run one Paper Execution cycle. Returns a :class:`CycleResult` (failures counted)."""
    configure_logging(dry_run=dry_run)
    logger = _get_logger()

    db_path = Path(db_path) if db_path is not None else store.position_db_path()
    default_profile = profile or profiles.DEFAULT_PROFILE
    now = parse_timestamp_utc(now_utc) if now_utc is not None else parse_timestamp_utc(pd.Timestamp.utcnow())

    if signal_db_path is None:
        import paper_signal_store as signal_store  # noqa: E402  (paper-signal)

        signal_db_path = signal_store.DEFAULT_DB_PATH
    if chart_db_path is None:
        chart_db_path = chart_collector.chart_db_path()

    def profile_for(symbol: str, timeframe: str) -> "profiles.ExecutionProfile":
        return profiles.resolve_execution_profile(
            symbol, timeframe, overrides=profile_overrides, default=default_profile
        )

    if load_candles is None:
        def load_candles(symbol: str, timeframe: str) -> pd.DataFrame:  # noqa: E306
            # STRICT read: a missing DB / candles table / OperationalError raises
            # ChartReadError instead of masquerading as "no candles" (Blocker).
            return chart_collector.load_candles_range_strict(chart_db_path, _chart_symbol(symbol), timeframe)

    candle_cache: dict[tuple[str, str], object] = {}
    chart_failures: set[tuple[str, str]] = set()

    def confirmed_candles(symbol: str, timeframe: str) -> pd.DataFrame:
        key = (symbol, timeframe)
        if key in candle_cache:
            cached = candle_cache[key]
            if isinstance(cached, Exception):
                raise cached
            return cached
        try:
            raw = load_candles(symbol, timeframe)
        except chart_collector.ChartReadError as exc:
            candle_cache[key] = exc  # cache so we do not re-hit the broken DB per position
            raise
        confirmed = select_confirmed_candles(raw, timeframe=timeframe, evaluation_time=now)
        candle_cache[key] = confirmed
        return confirmed

    def candles_or_skip(position: dict):
        """Return (candles, True) or (None, False) if the Chart DB read failed.

        A Chart read failure is counted (once per symbol/timeframe) as a cycle
        failure and the position is SKIPPED -- its state / terminal_reason /
        watermark / updated_at are left untouched (never wrongly expired)."""
        key = (position["symbol"], position["timeframe"])
        try:
            return confirmed_candles(*key), True
        except chart_collector.ChartReadError as exc:
            if key not in chart_failures:
                logger.error(
                    "Paper Execution: Chart DB read failed for %s %s: %s -- affected positions left unchanged.",
                    key[0], key[1], exc,
                )
                chart_failures.add(key)
            return None, False

    result = CycleResult()

    # --- Schema gate: settle migration BEFORE reading any position -----------
    # A legacy DB must never be read as the new model. Normal run: migrate + verify
    # up front (abort the cycle on failure). Dry-run: a READ-ONLY status probe only
    # -- proceed for missing/current, but refuse to read a legacy/invalid DB (and
    # never write to migrate it).
    if dry_run:
        status = store.get_schema_status(db_path)
        if status not in (store.SCHEMA_STATUS_MISSING, store.SCHEMA_STATUS_CURRENT):
            logger.error(
                "Paper Execution [DRY_RUN]: position DB schema is %r -- migration required. "
                "Not reading positions, not modifying the DB.", status,
            )
            result.failures += 1
            return result
    else:
        try:
            store.ensure_schema_migrated(db_path)
        except Exception as exc:
            # Migration failed (or verification did): do NOT read positions, do NOT
            # start evaluation. init_db already rolled back any partial rebuild.
            logger.error(
                "Paper Execution: schema migration failed (%s) -- aborting cycle before any position read.", exc,
            )
            result.failures += 1
            return result

    # Phase 1: signals -> pending / cancelled.
    try:
        signal_rows = signals if signals is not None else read_paper_signals(signal_db_path)
    except Exception as exc:  # a read failure must surface, not be swallowed
        logger.error("Paper Execution: reading Paper Signals failed: %s", exc)
        result.failures += 1
        signal_rows = []
    for signal in signal_rows:
        try:
            action = _process_signal(signal, dry_run=dry_run, db_path=db_path, profile_for=profile_for)
        except Exception as exc:
            logger.error("Paper Execution: signal %s failed: %s", signal.get("signal_id"), exc)
            result.failures += 1
            continue
        if action == "pending":
            result.pending_created += 1
        elif action == "cancelled":
            result.cancelled += 1

    # Phase 2: pending -> open / expired. (Dry-run reads never create the DB.)
    for position in _safe_list(store.list_positions_by_state, models.STATE_PENDING, db_path=db_path, result=result):
        candles, ok = candles_or_skip(position)
        if not ok:
            continue
        try:
            action = _process_pending(position, candles, dry_run=dry_run, db_path=db_path, now_utc=now)
        except Exception as exc:
            logger.error("Paper Execution: pending %s failed: %s", position["position_id"], exc)
            result.failures += 1
            continue
        if action == "opened":
            result.opened += 1
        elif action == "expired":
            result.expired += 1

    # Phase 3: open -> closed / watermark advance.
    for position in _safe_list(store.list_positions_by_state, models.STATE_OPEN, db_path=db_path, result=result):
        candles, ok = candles_or_skip(position)
        if not ok:
            continue
        try:
            action = _process_open(position, candles, dry_run=dry_run, db_path=db_path)
        except Exception as exc:
            logger.error("Paper Execution: open %s failed: %s", position["position_id"], exc)
            result.failures += 1
            continue
        if action == "closed":
            result.closed += 1

    # Chart read failures are cycle failures (one per affected symbol/timeframe),
    # never silently treated as "no candles".
    result.failures += len(chart_failures)

    logger.info(
        "Cycle done: pending=%d cancelled=%d opened=%d expired=%d closed=%d failures=%d",
        result.pending_created, result.cancelled, result.opened, result.expired, result.closed, result.failures,
    )
    return result


def _safe_list(list_fn, state, *, db_path, result: CycleResult) -> list[dict]:
    """Call a list_* read; on failure count it and return [] (never masks the error as success)."""
    try:
        return list_fn(state, db_path=db_path)
    except Exception as exc:
        _get_logger().error("Paper Execution: listing %s positions failed: %s", state, exc)
        result.failures += 1
        return []


# --- resident service --------------------------------------------------------
_STOP_EVENT = threading.Event()


def _handle_shutdown_signal(signum, _frame) -> None:
    _get_logger().info("Received signal %s; Paper Execution will stop after the current cycle.", signum)
    _STOP_EVENT.set()


def _install_signal_handlers() -> None:
    for sig in (signal_module.SIGTERM, signal_module.SIGINT):
        try:
            signal_module.signal(sig, _handle_shutdown_signal)
        except (ValueError, OSError):
            pass


def run_service(
    *,
    dry_run: bool = False,
    once: bool = False,
    interval_seconds: int = DEFAULT_CHECK_INTERVAL_SECONDS,
    db_path: Path | None = None,
    profile: "profiles.ExecutionProfile | None" = None,
) -> int:
    """Run the resident loop (or a single cycle with ``once=True``).

    Returns an exit code: for ``once=True`` it is non-zero if the single cycle had
    any failure. The resident loop logs per-cycle failures and keeps running,
    returning ``EXIT_OK`` on graceful SIGINT/SIGTERM shutdown.
    """
    logger = _get_logger()
    logger.info("=== Paper Execution start === once=%s dry_run=%s interval=%ss", once, dry_run, interval_seconds)
    last_result: CycleResult | None = None
    try:
        while not _STOP_EVENT.is_set():
            try:
                last_result = run_once(dry_run=dry_run, db_path=db_path, profile=profile)
                if last_result.failures:
                    logger.warning("Cycle completed with %d failure(s).", last_result.failures)
            except Exception as exc:  # never let one cycle kill the resident loop
                logger.error("Cycle raised: %s", exc)
                last_result = CycleResult(failures=1)
            if once:
                break
            if _STOP_EVENT.wait(interval_seconds):
                break
    finally:
        logger.info("=== Paper Execution stop ===")
    if once and last_result is not None and last_result.failures:
        return EXIT_FAILURE
    return EXIT_OK


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="paper_execution",
        description=(
            "Manage paper-only virtual positions from Paper Signals + candles. "
            "Never places an order, calls a broker, or sends Discord."
        ),
    )
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit (non-zero on failure).")
    parser.add_argument("--dry-run", action="store_true", help="Log decisions only; no DB/file side effects.")
    parser.add_argument("--interval-seconds", type=int, default=DEFAULT_CHECK_INTERVAL_SECONDS)
    parser.add_argument("--max-entry-delay-bars", type=int, default=profiles.DEFAULT_PROFILE.max_entry_delay_bars)
    parser.add_argument("--max-hold-bars", type=int, default=profiles.DEFAULT_PROFILE.max_hold_bars)
    parser.add_argument(
        "--same-candle-priority",
        choices=sorted(profiles.KNOWN_SAME_CANDLE_PRIORITIES),
        default=profiles.DEFAULT_SAME_CANDLE_PRIORITY,
    )
    parser.add_argument("--fallback-stop-pct", type=float, default=profiles.DEFAULT_PROFILE.fallback_stop_pct)
    parser.add_argument("--risk-reward", type=float, default=profiles.DEFAULT_PROFILE.risk_reward)
    parser.add_argument("--spread", type=float, default=0.0)
    parser.add_argument("--slippage", type=float, default=0.0)
    parser.add_argument("--commission", type=float, default=0.0)
    return parser.parse_args(argv)


def _profile_from_args(args: argparse.Namespace) -> "profiles.ExecutionProfile":
    return profiles.ExecutionProfile(
        max_entry_delay_bars=args.max_entry_delay_bars,
        max_hold_bars=args.max_hold_bars,
        same_candle_priority=args.same_candle_priority,
        fallback_stop_pct=args.fallback_stop_pct,
        risk_reward=args.risk_reward,
        spread=args.spread,
        slippage=args.slippage,
        commission=args.commission,
    )


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    configure_logging(dry_run=args.dry_run)
    _install_signal_handlers()
    return run_service(
        dry_run=args.dry_run, once=args.once, interval_seconds=args.interval_seconds,
        profile=_profile_from_args(args),
    )


if __name__ == "__main__":
    raise SystemExit(main())
