"""Backtest Runner / CLI: replay stored candles through the Backtest Engine.

*** PAPER-ONLY BACKTEST EVALUATION. NOT AN EXECUTION LAYER. ***

This is the entry point Issue #33 asks for: given ``--symbol --timeframe
--from --to``, it loads previously-collected candles from
market-watch's Chart DB, splits them into a warm-up range and a validation
range (see warmup.py), replays each validation bar through the existing,
unmodified Paper Signal rule set (paper-signal/signal_generator.py) and
Exit Policy (paper-signal/exit_policy.py), saves the results to the
existing Backtest DB (backtest_store.py / backtest_models.py), and prints a
minimal summary to stdout.

It never sends a Discord notification, calls a broker/Execution API, or
places a real order -- it only reads already-persisted historical data
(candles, Market Bias) and writes to the Backtest results DB.

Only candles at or after ``--from`` (and at or before ``--to``) are ever
returned as a BarEvaluation (see warmup.split_warmup_and_validation() /
evaluate_backtest_bars()); warm-up candles seed indicator calculations only
and never appear in the result summary or in saved backtest_signals rows.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
MARKET_WATCH_DIR = REPO_ROOT / "market-watch"
PAPER_SIGNAL_DIR = REPO_ROOT / "paper-signal"
NEWS_FEED_BOT_DIR = REPO_ROOT / "news-feed-bot"

for _dir in (BASE_DIR, MARKET_WATCH_DIR, PAPER_SIGNAL_DIR, NEWS_FEED_BOT_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import backtest_models as models  # noqa: E402
import backtest_store as store  # noqa: E402
import warmup  # noqa: E402
import chart_collector  # noqa: E402  (market-watch)
import exit_policy  # noqa: E402  (paper-signal)
from market_bias_store import get_market_bias_as_of, market_bias_db_path  # noqa: E402  (news-feed-bot)

logger = logging.getLogger("backtest.runner")

EXIT_OK = 0
EXIT_USAGE_ERROR = 2
EXIT_NO_DATA = 3

DEFAULT_STRATEGY_NAME = "paper_signal_generator"
NEUTRAL_BIAS_PLACEHOLDER = {
    "market_bias": "Neutral",
    "confidence": 0.0,
    "scores": {},
    "reasons": [],
    "warnings": [],
    "note": "no persisted Market Bias available at this point in time",
}

# Terminal states an evaluated signal can end up in; "open" also covers
# signals for which the loaded candle window ran out before any policy
# closed them (see exit_policy._no_close()).
RESULT_STATUS_ORDER = ("open", "win", "loss", "expired", "invalidated", "closed")


def _default_run_name(symbol: str, timeframe: str, from_time: str, to_time: str) -> str:
    def _clean(value: str) -> str:
        return value.replace(":", "").replace("-", "")

    return f"{symbol}_{timeframe}_{_clean(from_time)}_{_clean(to_time)}"


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"--warmup-bars must be >= 1, got {parsed}")
    return parsed


def _unit_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid float value: {value!r}") from exc
    if not (0.0 <= parsed <= 1.0):
        raise argparse.ArgumentTypeError(f"--confidence-threshold must be between 0.0 and 1.0, got {parsed}")
    return parsed


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="backtest",
        description=(
            "Replay stored candles through the Backtest Engine. Paper-only: "
            "never places an order, calls a broker/Execution API, or sends a "
            "Discord notification."
        ),
    )
    parser.add_argument("--symbol", required=True, help="e.g. USD_JPY")
    parser.add_argument(
        "--timeframe",
        required=True,
        help="Must match how candles were saved in the Chart DB, e.g. 15m, 1h, 4h.",
    )
    parser.add_argument(
        "--from",
        dest="from_time",
        required=True,
        help="Validation window start (inclusive), ISO-8601, e.g. 2026-06-01T00:00:00. "
        "Naive timestamps are treated as UTC.",
    )
    parser.add_argument(
        "--to",
        dest="to_time",
        required=True,
        help="Validation window end (inclusive), ISO-8601, e.g. 2026-06-30T23:59:59.",
    )
    parser.add_argument(
        "--db-path",
        help="Path to the Chart DB (candles). Defaults to CHART_DB_PATH / market-watch/data/chart_data.sqlite3.",
    )
    parser.add_argument(
        "--backtest-db-path",
        help="Path to the Backtest results DB. Defaults to BACKTEST_DB_PATH / backtest/data/backtest_results.sqlite3.",
    )
    parser.add_argument(
        "--market-bias-db-path",
        help="Path to the Market Bias DB. Defaults to MARKET_BIAS_DB_PATH / news-feed-bot/data/market_bias.sqlite3.",
    )
    parser.add_argument(
        "--warmup-bars",
        type=_positive_int,
        default=warmup.DEFAULT_WARMUP_BARS,
        help=f"Candles required before --from to seed indicators (default {warmup.DEFAULT_WARMUP_BARS}). Must be >= 1.",
    )
    parser.add_argument(
        "--confidence-threshold",
        type=_unit_float,
        default=None,
        help="Overrides the Paper Signal Generator's default confidence threshold. Must be between 0.0 and 1.0.",
    )
    parser.add_argument(
        "--exit-config",
        help="Path to an Exit Policy JSON config (see paper-signal/exit_policy.py). "
        "Defaults to BACKTEST_EXIT_CONFIG_PATH / built-in defaults.",
    )
    parser.add_argument("--run-name", help="Defaults to '<symbol>_<timeframe>_<from>_<to>'.")
    parser.add_argument("--strategy-name", default=DEFAULT_STRATEGY_NAME)
    return parser.parse_args(argv)


def _chart_symbol(symbol: str) -> str:
    """Map a backtest --symbol (e.g. 'USD_JPY') to the Chart DB's candle key (e.g. 'USDJPY').

    backtest_models/backtest_store keep the underscore form (see their
    tests' "USD_JPY" fixtures) while chart_collector's ``candles`` table is
    keyed without it (see DEFAULT_SYMBOLS: "USDJPY", "XAUUSD", ...) -- this
    bridges the two naming conventions without changing either module's
    stored format.
    """
    return symbol.replace("_", "").upper()


def _load_candles(chart_db_path: Path, symbol: str, timeframe: str, *, end_iso: str) -> pd.DataFrame:
    return chart_collector.load_candles_range(
        chart_db_path,
        _chart_symbol(symbol),
        timeframe,
        end=end_iso,
    )


def _summarize(records: list[dict]) -> dict[str, int]:
    counts = {status: 0 for status in RESULT_STATUS_ORDER}
    for record in records:
        status = record.get("result_status") or "open"
        counts[status] = counts.get(status, 0) + 1
    return counts


def run_backtest(args: argparse.Namespace) -> tuple[int, dict | None]:
    """Run one backtest per CLI args. Returns (exit_code, summary_dict_or_None)."""
    try:
        validation_start_iso = chart_collector.to_utc_iso_z(args.from_time)
        validation_end_iso = chart_collector.to_utc_iso_z(args.to_time)
    except (ValueError, TypeError) as exc:
        print(f"error: could not parse --from/--to as ISO-8601 timestamps: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR, None

    validation_start = pd.Timestamp(validation_start_iso)
    validation_end = pd.Timestamp(validation_end_iso)
    if validation_end < validation_start:
        print("error: --to must not be before --from", file=sys.stderr)
        return EXIT_USAGE_ERROR, None

    chart_db_path = Path(args.db_path) if args.db_path else chart_collector.chart_db_path()
    backtest_db_path = Path(args.backtest_db_path) if args.backtest_db_path else store.backtest_db_path()
    bias_db_path = Path(args.market_bias_db_path) if args.market_bias_db_path else market_bias_db_path()

    candles = _load_candles(chart_db_path, args.symbol, args.timeframe, end_iso=validation_end_iso)
    if candles.empty:
        print(
            f"error: no candles found for symbol={args.symbol!r} "
            f"(looked up as {_chart_symbol(args.symbol)!r}) timeframe={args.timeframe!r} in {chart_db_path}",
            file=sys.stderr,
        )
        return EXIT_NO_DATA, None

    warmup_frame, validation_frame = warmup.split_warmup_and_validation(candles, validation_start, validation_end)
    if validation_frame.empty:
        print(
            f"error: no candles fall within the validation window {args.from_time}..{args.to_time}",
            file=sys.stderr,
        )
        return EXIT_NO_DATA, None

    warmup_insufficient = len(warmup_frame) < args.warmup_bars
    if warmup_insufficient:
        logger.warning(
            "Warm-up allocation is insufficient: have %s candles before %s, need %s. "
            "Every validation bar will be skipped with reason=%s.",
            len(warmup_frame), args.from_time, args.warmup_bars, warmup.REASON_WARMUP_INSUFFICIENT,
        )

    bias_result = get_market_bias_as_of("GLOBAL", "1h", validation_start_iso, db_path=bias_db_path)
    if bias_result is None:
        logger.warning(
            "No persisted Market Bias found at or before %s (symbol=GLOBAL timeframe=1h in %s); "
            "using a neutral placeholder, so no signal candidates will be generated this run.",
            args.from_time, bias_db_path,
        )
        bias_result = dict(NEUTRAL_BIAS_PLACEHOLDER)

    confidence_threshold = (
        args.confidence_threshold if args.confidence_threshold is not None else warmup.DEFAULT_CONFIDENCE_THRESHOLD
    )

    evaluations = warmup.evaluate_backtest_bars(
        candles,
        validation_start,
        validation_end,
        symbol=args.symbol,
        timeframe=args.timeframe,
        bias_result=bias_result,
        warmup_bars=args.warmup_bars,
        confidence_threshold=confidence_threshold,
    )

    exit_config = exit_policy.load_exit_policy_config(args.exit_config)

    run_record = models.create_backtest_run_record(
        run_name=args.run_name or _default_run_name(args.symbol, args.timeframe, args.from_time, args.to_time),
        symbol=args.symbol,
        timeframe=args.timeframe,
        from_time=args.from_time,
        to_time=args.to_time,
        strategy_name=args.strategy_name,
        exit_policy=exit_config.get("exit_policy"),
        config={
            "warmup_bars": args.warmup_bars,
            "confidence_threshold": confidence_threshold,
            "chart_db_path": str(chart_db_path),
            "market_bias_db_path": str(bias_db_path),
        },
    )
    store.save_backtest_run(run_record, db_path=backtest_db_path)

    signal_records: list[dict] = []
    for evaluation in evaluations:
        if not evaluation.candidates:
            continue
        entry_timestamp = pd.Timestamp(evaluation.timestamp)
        entry_price = float(candles.loc[entry_timestamp, "close"])
        # signal_type="bar_close": the hypothesis enters at this bar's close,
        # so it can only ever be exposed to *subsequent* bars' high/low.
        # Exit evaluation must start at the next bar -- passing the entry
        # bar itself here would let its own high/low decide the exit before
        # the paper position could actually have been open against it.
        entry_position = candles.index.get_loc(entry_timestamp)
        future_candles = candles.iloc[entry_position + 1 :]

        for candidate in evaluation.candidates:
            direction = "long" if candidate.get("side") == "long" else "short"
            exit_result = exit_policy.evaluate_exit(
                {"direction": direction, "entry_price": entry_price},
                future_candles,
                exit_config,
            )
            record = models.create_backtest_signal_record(
                run_record["id"],
                signal_time=evaluation.timestamp,
                symbol=args.symbol,
                timeframe=args.timeframe,
                bias=candidate.get("market_bias"),
                confidence=candidate.get("confidence"),
                direction=direction,
                signal_type="paper_signal",
                trigger_type="bar_close",
                reasons=[candidate["reason"]] if candidate.get("reason") else None,
                entry_price=entry_price,
            )
            record = models.apply_exit_policy_result(record, exit_result)
            signal_records.append(record)

    if signal_records:
        store.save_backtest_signals(signal_records, db_path=backtest_db_path)

    summary = {
        "run_id": run_record["id"],
        "symbol": args.symbol,
        "timeframe": args.timeframe,
        "from_time": args.from_time,
        "to_time": args.to_time,
        "warmup_bars_available": len(warmup_frame),
        "warmup_bars_required": args.warmup_bars,
        "warmup_insufficient": warmup_insufficient,
        "bars_processed": len(evaluations),
        "signals_generated": len(signal_records),
        "result_counts": _summarize(signal_records),
        "backtest_db_path": str(backtest_db_path),
    }
    return EXIT_OK, summary


def print_summary(summary: dict) -> None:
    print("=== Backtest Summary ===")
    print(f"Symbol:              {summary['symbol']}")
    print(f"Timeframe:           {summary['timeframe']}")
    print(f"Validation window:   {summary['from_time']} .. {summary['to_time']}")
    print(
        f"Warm-up candles:     {summary['warmup_bars_available']} "
        f"(required >= {summary['warmup_bars_required']}"
        + (", INSUFFICIENT" if summary["warmup_insufficient"] else "")
        + ")"
    )
    print(f"Bars processed:      {summary['bars_processed']}")
    print(f"Signals generated:   {summary['signals_generated']}")
    print("Result breakdown:")
    for status, count in summary["result_counts"].items():
        print(f"  {status:<12} {count}")
    print(f"Run ID:              {summary['run_id']}")
    print(f"Backtest DB:         {summary['backtest_db_path']}")


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    args = parse_args(argv)
    exit_code, summary = run_backtest(args)
    if summary is not None:
        print_summary(summary)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
