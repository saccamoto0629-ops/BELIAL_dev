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

Issue #23 adds a ``summary`` subcommand (``python -m backtest summary
--run-id ...``) alongside the original flat/bare-flags invocation, which
is preserved unchanged as an implicit ``run`` (see _normalize_argv()). It
re-fetches a past run's signals and renders the same win/loss/pnl
performance summary that ``run`` now also prints automatically after
finishing -- see summary.py for the aggregation and report.py for
text/JSON/Markdown rendering.

Issue #24 adds ``run``'s ``--exit-policy`` flag: an explicit choice among
``exit_policy.KNOWN_POLICIES`` (e.g. ``fixed_rr``) that overrides whichever
policy ``--exit-config`` (or the built-in default) would otherwise select.
It is recorded on the saved backtest_runs row (and every backtest_signals
row via apply_exit_policy_result()) exactly like a config-file-selected
policy already was -- this only changes *how* the active policy is chosen,
not how it is stored or evaluated.
"""

from __future__ import annotations

import argparse
import bisect
import logging
import sys
from dataclasses import replace
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
import execution_model as execution  # noqa: E402  (Issue #26: virtual fill spread/slippage model)
import warmup  # noqa: E402
import chart_collector  # noqa: E402  (market-watch)
import exit_policy  # noqa: E402  (paper-signal)
from market_bias_store import (  # noqa: E402  (news-feed-bot)
    get_market_bias_as_of,
    load_market_bias_series,
    market_bias_db_path,
)
import summary as signal_summary  # noqa: E402  (Issue #23: signal-level win/loss/pnl aggregation)
import report  # noqa: E402  (Issue #23: text/JSON/Markdown rendering)

logger = logging.getLogger("backtest.runner")

EXIT_OK = 0
EXIT_USAGE_ERROR = 2
EXIT_NO_DATA = 3

# list_backtest_signals() defaults to limit=1000; re-fetching a past run's
# signals for a summary must never silently truncate, so the "summary"
# subcommand asks for a much larger cap explicitly instead.
SUMMARY_SIGNAL_FETCH_LIMIT = 1_000_000

DEFAULT_STRATEGY_NAME = "paper_signal_generator"
NEUTRAL_BIAS_PLACEHOLDER = {
    "market_bias": "Neutral",
    "confidence": 0.0,
    "scores": {},
    "reasons": [],
    "warnings": [],
    "note": "no persisted Market Bias available at this point in time",
}

# Issue #38: how a run resolves Market Bias across the validation window.
# "static" applies one as-of-``--from`` bias to every bar (pre-#38 behaviour,
# the default); "time_series" resolves each bar's bias point-in-time at its
# own close via --time-series-bias.
BIAS_MODE_STATIC = "static"
BIAS_MODE_TIME_SERIES = "time_series"


def _to_utc_timestamp(value) -> pd.Timestamp:
    """Normalize any bar/close/bias timestamp to a tz-aware UTC pandas Timestamp.

    Issue #38: entry times (validation bar index), Exit Policy ``close_time``
    strings, and Market Bias ``evaluated_at`` strings can arrive naive or
    already tz-aware. Normalizing them all to UTC before any ``<=``/``<``
    comparison keeps re-entry/cooldown suppression and point-in-time bias
    lookups from ever raising on mixed tz-awareness or silently mis-ordering.
    """
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


class TimeSeriesBiasLookup:
    """Point-in-time Market Bias resolver over a preloaded bias series.

    Issue #38: constructed once from the whole (GLOBAL, 1h) bias history
    (loaded through a single SQLite connection -- never re-opened per bar),
    then queried per validation bar. ``__call__(at)`` returns the latest bias
    whose ``evaluated_at <= at`` -- never a future one -- or the Neutral
    placeholder when no bias precedes ``at``. The series is pre-sorted
    ascending and looked up with a binary search, so this already has the
    "preload + time-series pointer" shape a future optimization would want.

    Tie-break: the series is sorted by ``(UTC-normalized evaluated_at,
    market_bias_id)`` ascending, so ``bisect_right`` + ``index - 1`` selects,
    within the latest ``evaluated_at`` group at or before ``at``, the record
    with the lexicographically greatest ``market_bias_id``. That is exactly
    the record ``market_bias_store.get_market_bias_as_of`` (ORDER BY
    evaluated_at DESC, market_bias_id DESC) returns -- so the static and
    time-series paths agree on same-timestamp groups, and neither result
    depends on DB insertion order.
    """

    def __init__(self, series: list[dict], *, neutral: dict) -> None:
        ordered = sorted(
            series,
            key=lambda row: (_to_utc_timestamp(row["evaluated_at"]), row.get("market_bias_id", "")),
        )
        self._times = [_to_utc_timestamp(row["evaluated_at"]) for row in ordered]
        self._biases = ordered
        self._neutral = neutral

    def __call__(self, at) -> dict:
        at_ts = _to_utc_timestamp(at)
        index = bisect.bisect_right(self._times, at_ts) - 1
        if index < 0:
            return self._neutral
        return self._biases[index]

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


def _nonneg_float(value: str) -> float:
    """argparse type for --spread / --slippage: a float >= 0.

    A negative value is rejected here as an argparse usage error (exit code
    2) rather than deep in the execution model, so ``--spread -0.1`` fails
    immediately with a clear message. The execution model itself
    re-validates (>= 0) as a defensive second layer.
    """
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid float value: {value!r}") from exc
    if parsed < 0.0:
        raise argparse.ArgumentTypeError(f"must be >= 0.0, got {parsed}")
    return parsed


def _normalize_argv(argv=None) -> list[str]:
    """Prepend the "run" subcommand when the caller omitted it.

    Issue #33 shipped a flat CLI (``--symbol --timeframe --from --to``,
    no subcommand). Issue #23 adds a second "summary" subcommand, but every
    existing invocation -- including every prior test's bare-flags
    ``parse_args([...])`` call -- must keep working unchanged. Any argv
    that doesn't already start with a known subcommand (or ``-h``/
    ``--help``, so top-level help still shows the "run" options) is treated
    as an implicit ``run``.
    """
    resolved = list(sys.argv[1:] if argv is None else argv)
    if resolved and resolved[0] in ("run", "summary"):
        return resolved
    return ["run", *resolved]


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="backtest",
        description=(
            "Replay stored candles through the Backtest Engine. Paper-only: "
            "never places an order, calls a broker/Execution API, or sends a "
            "Discord notification."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="Run a backtest over a validation window (default when no subcommand is given).",
    )
    run_parser.add_argument("--symbol", required=True, help="e.g. USD_JPY")
    run_parser.add_argument(
        "--timeframe",
        required=True,
        help="Must match how candles were saved in the Chart DB, e.g. 15m, 1h, 4h.",
    )
    run_parser.add_argument(
        "--from",
        dest="from_time",
        required=True,
        help="Validation window start (inclusive), ISO-8601, e.g. 2026-06-01T00:00:00. "
        "Naive timestamps are treated as UTC.",
    )
    run_parser.add_argument(
        "--to",
        dest="to_time",
        required=True,
        help="Validation window end (inclusive), ISO-8601, e.g. 2026-06-30T23:59:59.",
    )
    run_parser.add_argument(
        "--db-path",
        help="Path to the Chart DB (candles). Defaults to CHART_DB_PATH / market-watch/data/chart_data.sqlite3.",
    )
    run_parser.add_argument(
        "--backtest-db-path",
        help="Path to the Backtest results DB. Defaults to BACKTEST_DB_PATH / backtest/data/backtest_results.sqlite3.",
    )
    run_parser.add_argument(
        "--market-bias-db-path",
        help="Path to the Market Bias DB. Defaults to MARKET_BIAS_DB_PATH / news-feed-bot/data/market_bias.sqlite3.",
    )
    run_parser.add_argument(
        "--warmup-bars",
        type=_positive_int,
        default=warmup.DEFAULT_WARMUP_BARS,
        help=f"Candles required before --from to seed indicators (default {warmup.DEFAULT_WARMUP_BARS}). Must be >= 1.",
    )
    run_parser.add_argument(
        "--confidence-threshold",
        type=_unit_float,
        default=None,
        help="Overrides the Paper Signal Generator's default confidence threshold. Must be between 0.0 and 1.0.",
    )
    run_parser.add_argument(
        "--exit-config",
        help="Path to an Exit Policy JSON config (see paper-signal/exit_policy.py). "
        "Defaults to BACKTEST_EXIT_CONFIG_PATH / built-in defaults.",
    )
    run_parser.add_argument(
        "--exit-policy",
        choices=sorted(exit_policy.KNOWN_POLICIES),
        default=None,
        help="Select which Exit Policy is active for this run (overrides --exit-config's own "
        f"exit_policy, if any). Defaults to whatever --exit-config resolves to "
        f"(built-in default: {exit_policy.DEFAULT_EXIT_POLICY}). The chosen policy must be "
        "enabled=true in the resolved config, or this fails with a usage error.",
    )
    run_parser.add_argument(
        "--spread",
        type=_nonneg_float,
        default=0.0,
        help="Issue #26: fixed spread charged against every fill, as an absolute price "
        "difference (not pips). Must be >= 0.0 (default 0.0). Applied always in the "
        "direction unfavorable to the position.",
    )
    run_parser.add_argument(
        "--slippage",
        type=_nonneg_float,
        default=0.0,
        help="Issue #26: fixed slippage charged against every fill, as an absolute price "
        "difference (not pips). Must be >= 0.0 (default 0.0). Applied always in the "
        "direction unfavorable to the position.",
    )
    run_parser.add_argument(
        "--no-reentry",
        action="store_true",
        help="Issue #38: forbid opening a new hypothesis while one is already held. "
        "Enforces a single global position: any candidate whose entry falls within "
        "entry <= T <= close_time of the currently-held position is suppressed. An open "
        "position (no close within the window) is held until --to. Off by default "
        "(overlapping signals allowed, pre-#38 behaviour).",
    )
    run_parser.add_argument(
        "--cooldown-minutes",
        type=_nonneg_float,
        default=0.0,
        help="Issue #38: after any position closes at time C, suppress every candidate "
        "whose entry is < C + N minutes. Global (not per-side / per-signal-type). Must be "
        ">= 0.0 (default 0.0 = disabled, pre-#38 behaviour).",
    )
    run_parser.add_argument(
        "--time-series-bias",
        action="store_true",
        help="Issue #38: resolve Market Bias point-in-time at each bar's close "
        "(evaluated_at <= close, never a future record; Neutral before the first bias) "
        "instead of applying one as-of-'--from' bias to the whole run. Off by default "
        "(static bias, pre-#38 behaviour).",
    )
    run_parser.add_argument("--run-name", help="Defaults to '<symbol>_<timeframe>_<from>_<to>'.")
    run_parser.add_argument(
        "--strategy-name",
        default=DEFAULT_STRATEGY_NAME,
        help=f"A label recorded on the saved backtest_runs row (default: {DEFAULT_STRATEGY_NAME!r}). "
        "Every run replays the same, unmodified Paper Signal rule set (paper-signal/signal_generator.py) "
        "regardless of this value -- it does not select different signal-generation logic.",
    )

    summary_parser = subparsers.add_parser(
        "summary",
        help="Re-display or export a past run's signal-level performance summary.",
    )
    summary_parser.add_argument("--run-id", required=True, help="backtest_runs.id to summarize.")
    summary_parser.add_argument(
        "--backtest-db-path",
        help="Path to the Backtest results DB. Defaults to BACKTEST_DB_PATH / backtest/data/backtest_results.sqlite3.",
    )
    summary_parser.add_argument(
        "--format",
        choices=report.KNOWN_FORMATS,
        default=report.FORMAT_TEXT,
        help=f"Output format (default: {report.FORMAT_TEXT}).",
    )
    summary_parser.add_argument(
        "--output",
        help="Write the rendered summary to this file instead of only printing it. "
        "Parent directories are created automatically; an existing file is overwritten.",
    )

    return parser.parse_args(_normalize_argv(argv))


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
    # Issue #38: a tz-aware UTC copy for held-position / cooldown comparisons.
    validation_end_utc = _to_utc_timestamp(validation_end)
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

    # Issue #38: two Market Bias resolution modes, both reading the same
    # (GLOBAL, 1h) history point-in-time (never a future record).
    #   static      -- one bias as of --from, applied to every bar (default).
    #   time_series -- each bar's bias resolved at its own close via a
    #                  preloaded, binary-searched series (--time-series-bias).
    bias_lookup = None
    if args.time_series_bias:
        bias_mode = BIAS_MODE_TIME_SERIES
        bias_series = load_market_bias_series("GLOBAL", "1h", db_path=bias_db_path)
        if not bias_series:
            logger.warning(
                "No persisted Market Bias series found (symbol=GLOBAL timeframe=1h in %s); "
                "time-series bias will fall back to Neutral for every bar, so no signal "
                "candidates will be generated this run.",
                bias_db_path,
            )
        bias_lookup = TimeSeriesBiasLookup(bias_series, neutral=dict(NEUTRAL_BIAS_PLACEHOLDER))
        # evaluate_backtest_bars still requires a bias_result argument; in
        # time-series mode bias_lookup supersedes it per bar, so pass a neutral
        # placeholder that is never actually consulted.
        bias_result = dict(NEUTRAL_BIAS_PLACEHOLDER)
    else:
        bias_mode = BIAS_MODE_STATIC
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

    # Issue #26: a fixed spread/slippage virtual-fill model. --spread /
    # --slippage default to 0.0, so an unchanged CLI invocation builds a
    # no-op model that leaves every raw entry/close price exactly as before.
    exec_model = execution.ExecutionModel(spread=args.spread, slippage=args.slippage)

    evaluations = warmup.evaluate_backtest_bars(
        candles,
        validation_start,
        validation_end,
        symbol=args.symbol,
        timeframe=args.timeframe,
        bias_result=bias_result,
        warmup_bars=args.warmup_bars,
        confidence_threshold=confidence_threshold,
        bias_lookup=bias_lookup,
    )

    exit_config = exit_policy.load_exit_policy_config(args.exit_config)
    if args.exit_policy is not None:
        # Override --exit-config's own exit_policy selection (if any) with the
        # explicit --exit-policy choice. All policy blocks are already present
        # in the resolved config regardless of which one is active, so this
        # only ever changes *which* one is used -- it never invents a new
        # policies.<name> block. Re-validate immediately so an argparse-valid
        # policy name (choices=KNOWN_POLICIES) that isn't enabled in the
        # resolved config fails here with a clear usage error, not deep
        # inside the first evaluate_exit() call.
        exit_config = dict(exit_config, exit_policy=args.exit_policy)
        try:
            exit_policy.validate_exit_policy_config(exit_config)
        except ValueError as exc:
            print(f"error: --exit-policy {args.exit_policy!r} is not usable: {exc}", file=sys.stderr)
            return EXIT_USAGE_ERROR, None

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
            # Issue #26: record exactly which virtual-fill model produced this
            # run's entry/close prices, alongside the pre-existing keys.
            "execution_model": exec_model.to_config(),
            # Issue #38: record the opt-in realism settings for this run so a
            # saved run is self-describing (all three default to "off").
            "no_reentry": args.no_reentry,
            "cooldown_minutes": args.cooldown_minutes,
            "bias_mode": bias_mode,
        },
    )
    store.save_backtest_run(run_record, db_path=backtest_db_path)

    # Issue #38: opt-in candidate suppression, enforced in strict entry-time
    # order (evaluations are already ascending). Both are global (one position
    # / one cooldown at a time), not per-side or per-signal-type.
    #   --no-reentry     -- while a position is held, drop any candidate whose
    #                       entry falls in [held position entry, close_time].
    #                       An open position (close_time=None) is held until
    #                       validation_end.
    #   --cooldown-minutes N -- after a position closes at C, drop any candidate
    #                       whose entry is < C + N minutes.
    # With both flags off (the defaults) neither branch ever fires and every
    # candidate is adopted exactly as before.
    cooldown_delta = pd.Timedelta(minutes=args.cooldown_minutes)
    held_until: pd.Timestamp | None = None
    cooldown_until: pd.Timestamp | None = None
    suppressed_reentry = 0
    suppressed_cooldown = 0

    signal_records: list[dict] = []
    for evaluation in evaluations:
        if not evaluation.candidates:
            continue
        entry_timestamp = pd.Timestamp(evaluation.timestamp)
        entry_ts_utc = _to_utc_timestamp(entry_timestamp)
        raw_entry_price = float(candles.loc[entry_timestamp, "close"])
        # The hypothesis enters at this bar's close, so it can only ever be
        # exposed to *subsequent* bars' high/low. Exit evaluation must start at
        # the next bar -- passing the entry bar itself here would let its own
        # high/low decide the exit before the paper position could actually
        # have been open against it. (This "enter at bar close" is a Backtest
        # evaluation-timing detail, not the Paper Signal's trigger_type -- see
        # the record below, which never overwrites trigger_type with it.)
        entry_position = candles.index.get_loc(entry_timestamp)
        future_candles = candles.iloc[entry_position + 1 :]

        for candidate in evaluation.candidates:
            # Issue #38: re-entry suppression is checked before cooldown so
            # the two counters never double-count the same candidate. Given a
            # close at C with held_until=C and cooldown_until=C+N: entry <= C
            # counts as re-entry; C < entry < C+N counts as cooldown.
            if args.no_reentry and held_until is not None and entry_ts_utc <= held_until:
                suppressed_reentry += 1
                continue
            if args.cooldown_minutes > 0 and cooldown_until is not None and entry_ts_utc < cooldown_until:
                suppressed_cooldown += 1
                continue

            direction = "long" if candidate.get("side") == "long" else "short"
            # Issue #26 ordering: (1) raw entry = signal bar's close;
            # (2) direction decided above; (3) correct the entry with the
            # virtual-fill model; (4) hand the corrected entry to the Exit
            # Policy so its stop/target and walk are relative to the price
            # the paper position actually "filled" at.
            entry_price = exec_model.adjust_entry_price(raw_entry_price, direction)
            exit_result = exit_policy.evaluate_exit(
                {"direction": direction, "entry_price": entry_price},
                future_candles,
                exit_config,
            )
            # (5) correct the Exit Policy's raw close_price the same way. A
            # None close_price (open / no_close) is left as None -- there is
            # no fill to charge costs against. All other exit fields (mfe,
            # mae, close_time, close_reason, exit_policy, status) are kept
            # exactly as the policy returned them.
            adjusted_close_price = exec_model.adjust_close_price(exit_result.close_price, direction)
            exit_result = replace(exit_result, close_price=adjusted_close_price)
            record = models.create_backtest_signal_record(
                run_record["id"],
                signal_time=evaluation.timestamp,
                symbol=args.symbol,
                timeframe=args.timeframe,
                bias=candidate.get("market_bias"),
                confidence=candidate.get("confidence"),
                direction=direction,
                # Issue #27: carry all four Paper Signal classification fields
                # straight through from the candidate onto the Backtest Signal,
                # unchanged, so they survive to storage/aggregation. Normal
                # processing always saves the candidate's own values (never a
                # fallback constant) -- build_candidates() guarantees all four
                # are present. trigger_type is the signal's real generation
                # trigger ("macd_state_alignment"), never the backtest's
                # bar-close evaluation timing. (The migration-only fallback in
                # backtest_store.py is solely for repairing legacy DB rows.)
                signal_type=candidate["signal_type"],
                trigger_type=candidate["trigger_type"],
                alert_source=candidate["alert_source"],
                strategy_profile=candidate["strategy_profile"],
                reasons=[candidate["reason"]] if candidate.get("reason") else None,
                entry_price=entry_price,
            )
            # (6)/(7) apply_exit_policy_result() maps the corrected exit onto
            # the record and re-derives pnl from the corrected entry/close.
            record = models.apply_exit_policy_result(record, exit_result)
            signal_records.append(record)

            # Issue #38: update the single-position / cooldown state from THIS
            # adopted position's close. close_time is the exit bar's timestamp
            # (isoformat); None means the position never closed within the
            # loaded window. Compared in UTC to stay tz-safe.
            if args.no_reentry or args.cooldown_minutes > 0:
                if exit_result.close_time is None:
                    # Open position: held to the window end; a never-closed
                    # position starts no cooldown.
                    if args.no_reentry:
                        held_until = validation_end_utc
                else:
                    close_ts_utc = _to_utc_timestamp(exit_result.close_time)
                    if args.no_reentry:
                        held_until = close_ts_utc
                    if args.cooldown_minutes > 0:
                        cooldown_until = close_ts_utc + cooldown_delta

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
        # Issue #38: how many candidates the opt-in filters dropped, and which
        # Market Bias mode this run used. Both counts are 0 when the flags are
        # off; bias_mode is "static" unless --time-series-bias was given.
        "suppressed_reentry": suppressed_reentry,
        "suppressed_cooldown": suppressed_cooldown,
        "bias_mode": bias_mode,
        # Issue #26: echo the virtual-fill model settings that produced this
        # run's entry/close prices (mirrors backtest_runs.config_json).
        "execution_model": exec_model.to_config(),
        # Issue #23: win/loss/pnl performance summary for this run, built
        # from the exact signal_records just saved (no extra DB round-trip
        # needed since they're already in memory). Safe when empty.
        "signal_summary": signal_summary.build_summary(run_record["id"], signal_records),
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
    # Issue #38: bias mode + how many candidates the opt-in filters dropped.
    print(f"Bias mode:           {summary.get('bias_mode', BIAS_MODE_STATIC)}")
    print(
        f"Suppressed:          re-entry={summary.get('suppressed_reentry', 0)}, "
        f"cooldown={summary.get('suppressed_cooldown', 0)}"
    )
    execution_model = summary.get("execution_model") or {}
    print(
        f"Execution model:     {execution_model.get('type', 'n/a')} "
        f"(spread={execution_model.get('spread', 0.0)}, "
        f"slippage={execution_model.get('slippage', 0.0)})"
    )
    print("Result breakdown:")
    for status, count in summary["result_counts"].items():
        print(f"  {status:<12} {count}")
    print(f"Run ID:              {summary['run_id']}")
    print(f"Backtest DB:         {summary['backtest_db_path']}")


def run_summary_command(args: argparse.Namespace) -> int:
    """Re-fetch a past run_id's signals and print/export its performance summary.

    Read-only against the Backtest DB except for the optional ``--output``
    file write: never touches backtest_runs/backtest_signals, Discord, or
    any broker/Execution API.
    """
    backtest_db_path = Path(args.backtest_db_path) if args.backtest_db_path else store.backtest_db_path()

    run = store.get_backtest_run(args.run_id, db_path=backtest_db_path)
    if run is None:
        print(
            f"error: no backtest run found for run_id={args.run_id!r} in {backtest_db_path}",
            file=sys.stderr,
        )
        return EXIT_NO_DATA

    signals = store.list_backtest_signals(args.run_id, db_path=backtest_db_path, limit=SUMMARY_SIGNAL_FETCH_LIMIT)
    result = signal_summary.build_summary(args.run_id, signals)

    try:
        content = report.render(result, args.format)
    except ValueError as exc:
        # e.g. render_json()'s allow_nan=False guard tripping on a NaN/Infinity
        # that should never occur -- surfaced as a clean CLI error instead of
        # a traceback, same as any other malformed-input case here.
        print(f"error: could not render summary: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR

    if args.output:
        try:
            saved_path = report.save_summary(content, args.output)
        except OSError as exc:
            # A bad --output path (permission denied, --output pointing at an
            # existing directory, disk full, ...) must surface as a short CLI
            # error, not an uncaught traceback -- this is the only place this
            # subcommand writes a file, so it's the only place this can fail.
            print(f"error: could not write summary to {args.output}: {exc}", file=sys.stderr)
            return EXIT_USAGE_ERROR
        print(f"Saved summary to {saved_path}")
    else:
        print(content)

    return EXIT_OK


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    args = parse_args(argv)
    if args.command == "summary":
        return run_summary_command(args)

    exit_code, run_result = run_backtest(args)
    if run_result is not None:
        print_summary(run_result)
        print()
        print(report.render_text(run_result["signal_summary"]))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
