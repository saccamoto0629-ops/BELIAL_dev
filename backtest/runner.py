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
import json
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
import squeeze_breakout  # noqa: E402  (Issue #44: XAUUSD 1h squeeze-episode breakout strategy A)
import warmup  # noqa: E402
import chart_collector  # noqa: E402  (market-watch)
from market_state import KNOWN_MARKET_STATES  # noqa: E402  (market-watch, Issue #39)
import exit_policy  # noqa: E402  (paper-signal)
from market_bias_store import (  # noqa: E402  (news-feed-bot)
    get_effective_market_bias_as_of,
    load_effective_market_bias_series,
    market_bias_db_path,
)
from market_bias_models import (  # noqa: E402  (news-feed-bot)
    GLOBAL_SCOPE_SYMBOL,
    effective_bias_scopes,
    normalize_bias_symbol,
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

# Issue #44: which strategy the run replays. The default keeps the existing
# paper-signal (trend_follow / macd alignment) candidate path byte-for-byte;
# selecting the squeeze-breakout profile routes to squeeze_breakout.py instead.
STRATEGY_PROFILE_TREND_FOLLOW = "trend_follow"
KNOWN_STRATEGY_PROFILES = (STRATEGY_PROFILE_TREND_FOLLOW, squeeze_breakout.STRATEGY_PROFILE)
DEFAULT_STRATEGY_PROFILE = STRATEGY_PROFILE_TREND_FOLLOW
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


def _market_state_set(value: str) -> frozenset[str]:
    """argparse type for --allowed-market-states: a comma-separated allow-list.

    Issue #39: parses e.g. ``trend_up,range`` into ``frozenset({"trend_up",
    "range"})``. Whitespace around each name is ignored and duplicates collapse.
    Every name must be one of ``market_state.KNOWN_MARKET_STATES`` -- an empty
    list or any unknown label is rejected here as a usage error (exit code 2)
    with a message listing the valid states, rather than silently filtering
    everything out deep in the run.
    """
    names = [part.strip() for part in value.split(",")]
    if not names or any(not name for name in names):
        raise argparse.ArgumentTypeError(
            "must be a comma-separated list with no empty market states; "
            f"valid states: {', '.join(sorted(KNOWN_MARKET_STATES))}"
        )
    parsed = frozenset(names)
    unknown = sorted(parsed - set(KNOWN_MARKET_STATES))
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown market state(s): {', '.join(unknown)}. "
            f"Valid states: {', '.join(sorted(KNOWN_MARKET_STATES))}"
        )
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
    run_parser.add_argument(
        "--allowed-market-states",
        type=_market_state_set,
        default=None,
        metavar="STATE[,STATE...]",
        help="Issue #39: only adopt a candidate when its bar's Market State is in this "
        f"comma-separated allow-list (valid: {', '.join(sorted(KNOWN_MARKET_STATES))}). "
        "Every bar's state is still classified and recorded regardless; this only gates "
        "which candidates become saved signals. Unspecified (the default) means no state "
        "filter -- pre-#39 behaviour, every candidate is eligible.",
    )
    # Issue #53: opt-in external Strategy definitions (docs/architecture/
    # strategy_definition.md). Mutually exclusive with each other, and with an
    # explicit --strategy-profile (checked after parsing, below). When neither
    # is given, the external-definition machinery is never started: no PyYAML
    # import, no strategy-directory scan -- the --strategy-profile path is
    # preserved byte-for-byte.
    strategy_source = run_parser.add_mutually_exclusive_group()
    strategy_source.add_argument(
        "--strategy-file",
        default=None,
        metavar="PATH",
        help="Issue #53: load ONE external Strategy definition YAML file (see "
        "docs/architecture/strategy_definition.md) and run it. No directory scan. "
        "Mutually exclusive with --strategy and --strategy-profile.",
    )
    strategy_source.add_argument(
        "--strategy",
        default=None,
        metavar="ID",
        help="Issue #53: validate every Strategy definition in backtest/strategies/ "
        "(override with STRATEGY_DEFINITIONS_DIR) and run the definition whose id "
        "matches. Mutually exclusive with --strategy-file and --strategy-profile.",
    )
    run_parser.add_argument(
        "--strategy-profile",
        choices=KNOWN_STRATEGY_PROFILES,
        # None means "not given"; resolved to DEFAULT_STRATEGY_PROFILE after
        # parsing so an explicit value can be detected for the Issue #53
        # exclusivity check without changing the effective default.
        default=None,
        help="Issue #44: which strategy this run replays. "
        f"{STRATEGY_PROFILE_TREND_FOLLOW!r} (the default) keeps the existing paper-signal "
        "candidate path (Market Bias + MACD alignment) unchanged. "
        f"{squeeze_breakout.STRATEGY_PROFILE!r} replays the XAUUSD 1h squeeze-episode "
        "Bollinger close-break strategy instead (its own Trigger-bar stop, 2R target, and "
        "gap -> SL/TP -> 24-bar time-exit composite); Market Bias, MACD, and the "
        "--exit-policy / --allowed-market-states / --no-reentry / --cooldown-minutes flags "
        "do not apply to it.",
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

    args = parser.parse_args(_normalize_argv(argv))
    if args.command == "run":
        # Issue #53: --strategy-file/--strategy select an external definition;
        # combining either with an explicit --strategy-profile would leave it
        # ambiguous which strategy is in effect, so it is a usage error (the
        # --strategy-file vs --strategy pair is already rejected by argparse's
        # mutually exclusive group).
        strategy_profile_given = args.strategy_profile is not None
        if args.strategy_profile is None:
            args.strategy_profile = DEFAULT_STRATEGY_PROFILE
        if strategy_profile_given and (args.strategy_file or args.strategy):
            parser.error(
                "--strategy-file/--strategy cannot be combined with --strategy-profile: "
                "choose either an external Strategy definition or a built-in profile"
            )
    return args


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


def _run_meta_from_run(run: dict) -> dict:
    """Extract a run's strategy identity + execution conditions from its own row.

    Issue #44 review (Major): the ``summary`` subcommand must be able to show a
    saved run's strategy profile and spread/slippage in every format, even when
    the run produced zero signals. Those facts are taken *only* from the run's
    ``config_json`` / columns -- never inferred from its signals -- so a
    zero-signal run is still self-identifying. A legacy run saved before
    ``strategy_profile`` was recorded falls back to its ``strategy_name``.
    """
    try:
        config = json.loads(run.get("config_json") or "{}")
    except (TypeError, ValueError):
        config = {}
    execution_model = config.get("execution_model") or {}
    return {
        "strategy_profile": config.get("strategy_profile") or run.get("strategy_name"),
        "symbol": run.get("symbol"),
        "timeframe": run.get("timeframe"),
        "from_time": run.get("from_time"),
        "to_time": run.get("to_time"),
        "exit_policy": run.get("exit_policy"),
        "execution_model": {
            "type": execution_model.get("type"),
            "spread": execution_model.get("spread"),
            "slippage": execution_model.get("slippage"),
        },
    }


def _summarize(records: list[dict]) -> dict[str, int]:
    counts = {status: 0 for status in RESULT_STATUS_ORDER}
    for record in records:
        status = record.get("result_status") or "open"
        counts[status] = counts.get(status, 0) + 1
    return counts


def _run_squeeze_breakout(
    args: argparse.Namespace,
    *,
    candles: pd.DataFrame,
    chart_db_path: Path,
    backtest_db_path: Path,
    warmup_frame: pd.DataFrame,
    validation_frame: pd.DataFrame,
    warmup_insufficient: bool,
    validation_start: pd.Timestamp,
    validation_end: pd.Timestamp,
    definition=None,
) -> tuple[int, dict | None]:
    """Issue #44: run the XAUUSD 1h squeeze-episode breakout strategy A.

    Entirely separate from the paper-signal (trend_follow) path: no Market
    Bias, no generate_signal_candidates(), no #38/#39 suppression flags. The
    squeeze episode state machine is single-position by construction (one entry
    per episode, a new squeeze episode required before re-entry), so global
    no-reentry is structurally enforced and cooldown is 0. Saves the same
    backtest_runs / backtest_signals shape as the trend_follow path, with the
    squeeze signal_type / trigger_type / strategy_profile on every row so the
    two strategies are never confused in storage or summaries.

    Issue #53: when ``definition`` (a validated strategy_definition.
    StrategyDefinition selecting the squeeze_breakout handler) is given, its
    variable fields drive the run -- ``risk.take_profit.r`` -> risk_reward,
    ``exit.max_holding_bars`` -> time_exit_bars, its id labels every saved
    row, and its full normalized form is recorded in config_json. All of its
    remaining fields are fixed-value contracts already validated to match this
    handler's actual behaviour (docs §5.2), so nothing accepted is ignored.
    When ``definition`` is None, the pre-#53 --strategy-profile behaviour is
    preserved unchanged (module-constant defaults).
    """
    if definition is not None:
        strategy_profile = definition.id
        risk_reward = float(definition.risk.take_profit["r"])
        time_exit_bars = definition.exit.max_holding_bars
        same_candle_priority = definition.exit.same_candle_priority
    else:
        strategy_profile = squeeze_breakout.STRATEGY_PROFILE
        risk_reward = squeeze_breakout.RISK_REWARD
        time_exit_bars = squeeze_breakout.TIME_EXIT_BARS
        same_candle_priority = exit_policy.SAME_CANDLE_PRIORITY_STOP_LOSS

    # Issue #26: the same virtual-fill model the trend_follow path uses. Defaults
    # (spread=slippage=0.0) leave raw prices unchanged.
    exec_model = execution.ExecutionModel(spread=args.spread, slippage=args.slippage)

    config = {
        "warmup_bars": args.warmup_bars,
        "chart_db_path": str(chart_db_path),
        "execution_model": exec_model.to_config(),
        # Issue #44: the fixed, pre-committed strategy parameters (Issue #43:
        # not tuned on Issue #42 PnL). Recorded so a saved run is fully
        # reproducible from its config alone.
        "strategy_profile": strategy_profile,
        "signal_type": squeeze_breakout.SIGNAL_TYPE,
        "trigger_type": squeeze_breakout.TRIGGER_TYPE,
        "risk_reward": risk_reward,
        "time_exit_bars": time_exit_bars,
        "same_candle_priority": same_candle_priority,
        # The episode state machine enforces a single position and a
        # new-episode-before-re-entry rule; there is no time-based cooldown.
        "no_reentry": True,
        "cooldown_minutes": 0.0,
    }
    if definition is not None:
        # Issue #53: the full normalized external definition (defaults
        # resolved, JSON-compatible) so a saved run reproduces the exact
        # strategy configuration -- including its metadata (schema_version /
        # id / name / description) -- from config_json alone.
        config["strategy_definition"] = definition.to_config()

    run_record = models.create_backtest_run_record(
        run_name=args.run_name or _default_run_name(args.symbol, args.timeframe, args.from_time, args.to_time),
        symbol=args.symbol,
        timeframe=args.timeframe,
        from_time=args.from_time,
        to_time=args.to_time,
        strategy_name=args.strategy_name,
        exit_policy=squeeze_breakout.EXIT_POLICY_LABEL,
        config=config,
    )
    store.save_backtest_run(run_record, db_path=backtest_db_path)

    signals = squeeze_breakout.evaluate_squeeze_breakout_signals(
        candles,
        validation_start,
        validation_end,
        symbol=args.symbol,
        timeframe=args.timeframe,
        warmup_bars=args.warmup_bars,
        exec_model=exec_model,
        # Issue #53 (docs §5.2 ①): the two variable squeeze fields. On the
        # non-definition path these equal the module constants, so passing
        # them explicitly changes nothing for --strategy-profile runs.
        risk_reward=risk_reward,
        time_exit_bars=time_exit_bars,
    )

    signal_records: list[dict] = []
    for signal in signals:
        # Issue #44 review (Blocker): a squeeze-breakout take-profit fills
        # exactly at the strategy's 2R target *by design* -- Issue #43 pins its
        # PnL to the defined R multiple and defers execution-cost realism to a
        # future policy. So the close-side Execution Model cost must NOT be
        # re-applied to a TP close, or the saved close_price/pnl would drift off
        # target for any non-zero spread/slippage. Every other exit (stop, time
        # exit, or an unresolved open) still takes the realistic close-side cost.
        # Keyed on the take_profit close_reason so ONLY this strategy's TP leg is
        # exempted -- the trend_follow path (run_backtest) is a separate code
        # path and is untouched, and squeeze stops/time-exits keep their costs.
        exit_result = signal.exit_result
        if exit_result.close_reason == exit_policy.REASON_TAKE_PROFIT:
            costed_close_price = exit_result.close_price  # == target, left exactly
        else:
            costed_close_price = exec_model.adjust_close_price(exit_result.close_price, signal.direction)
        exit_result = replace(exit_result, close_price=costed_close_price)
        record = models.create_backtest_signal_record(
            run_record["id"],
            signal_time=signal.signal_time,
            symbol=args.symbol,
            timeframe=args.timeframe,
            # Bias / confidence are not part of this strategy.
            bias=None,
            confidence=None,
            direction=signal.direction,
            signal_type=squeeze_breakout.SIGNAL_TYPE,
            trigger_type=squeeze_breakout.TRIGGER_TYPE,
            alert_source=squeeze_breakout.ALERT_SOURCE,
            # Issue #53: an external definition stamps its own id; the
            # --strategy-profile path keeps the module constant (same value).
            strategy_profile=strategy_profile,
            reasons=None,
            entry_price=signal.entry_price,
            # Issue #44: unlike the trend_follow path (which falls back to a %
            # stop), squeeze breakout carries an explicit Trigger-bar stop and a
            # 2R target, so both are persisted for reproducibility.
            invalidation_price=signal.stop_price,
            take_profit_price=signal.target_price,
            market_state=signal.market_state,
            market_state_confidence=signal.market_state_confidence,
        )
        record = models.apply_exit_policy_result(record, exit_result)
        signal_records.append(record)

    if signal_records:
        store.save_backtest_signals(signal_records, db_path=backtest_db_path)

    # Issue #44 review (Major): attach the run's strategy profile + execution
    # conditions (from the run's own config, not inferred from signals) so the
    # rendered summary identifies the strategy even with zero signals.
    run_signal_summary = signal_summary.build_summary(run_record["id"], signal_records)
    run_signal_summary["run_meta"] = _run_meta_from_run(run_record)

    summary = {
        "run_id": run_record["id"],
        "symbol": args.symbol,
        "timeframe": args.timeframe,
        "from_time": args.from_time,
        "to_time": args.to_time,
        "strategy_profile": strategy_profile,
        # Issue #53: the normalized external definition (None on the
        # --strategy-profile path), mirroring config_json.
        "strategy_definition": config.get("strategy_definition"),
        "warmup_bars_available": len(warmup_frame),
        "warmup_bars_required": args.warmup_bars,
        "warmup_insufficient": warmup_insufficient,
        "bars_processed": len(validation_frame),
        "signals_generated": len(signal_records),
        "result_counts": _summarize(signal_records),
        "backtest_db_path": str(backtest_db_path),
        # These #38/#39/#40 fields don't apply to this strategy; report neutral
        # values so print_summary stays uniform across both paths.
        "suppressed_reentry": 0,
        "suppressed_cooldown": 0,
        "suppressed_market_state": 0,
        "allowed_market_states": None,
        "bias_mode": "n/a",
        "bias_resolution": {},
        "bias_global_fallback": False,
        "execution_model": exec_model.to_config(),
        "signal_summary": run_signal_summary,
    }
    return EXIT_OK, summary


# Issue #53: execution binding for the handler registry. An external Strategy
# definition can only name a handler that is BOTH declared in
# strategy_definition.HANDLER_REGISTRY (capability validation) and bound here
# to an actual runner (execution). A test pins the two key sets to be equal so
# a handler can never be accepted by validation without an execution path.
# Keys are literals (not strategy_definition constants) so importing this
# module never imports strategy_definition/PyYAML on the default path.
EXTERNAL_STRATEGY_HANDLER_RUNNERS = {
    "squeeze_breakout": _run_squeeze_breakout,
}


def _resolve_external_strategy(args: argparse.Namespace):
    """Issue #53: load + validate the opted-in external Strategy definition.

    Returns ``(exit_code, definition)``: ``(EXIT_OK, StrategyDefinition)`` on
    success, ``(EXIT_USAGE_ERROR, None)`` with a message on stderr otherwise.
    Called before any DB file is opened or created, so a validation failure has
    zero side effects. Never called when neither --strategy-file nor --strategy
    was given (the import below is the only place PyYAML enters the runner).
    """
    import strategy_definition as strategy_definitions  # local: opt-in only

    try:
        if args.strategy_file:
            definition = strategy_definitions.load_strategy_definition(args.strategy_file)
        else:
            registry = strategy_definitions.load_strategy_registry()
            definition = registry.get(args.strategy)
            if definition is None:
                available = ", ".join(sorted(registry)) or "(none)"
                print(
                    f"error: unknown strategy id {args.strategy!r} in "
                    f"{strategy_definitions.strategy_definitions_dir()}; available ids: {available}",
                    file=sys.stderr,
                )
                return EXIT_USAGE_ERROR, None
    except strategy_definitions.StrategyDefinitionError as exc:
        print(f"error: invalid strategy definition: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR, None

    # docs §5.2 ②: the definition declares which symbol/timeframe it applies
    # to; a run requesting anything else is a usage error, not a silent run.
    if args.symbol not in definition.symbols:
        print(
            f"error: --symbol {args.symbol!r} is not declared by strategy {definition.id!r} "
            f"(symbols: {', '.join(definition.symbols)})",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR, None
    if args.timeframe not in definition.timeframes:
        print(
            f"error: --timeframe {args.timeframe!r} is not declared by strategy {definition.id!r} "
            f"(timeframes: {', '.join(definition.timeframes)})",
            file=sys.stderr,
        )
        return EXIT_USAGE_ERROR, None
    return EXIT_OK, definition


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

    # Issue #53: resolve the external Strategy definition (opt-in) BEFORE any
    # DB path is opened/created, so an invalid definition fails with zero side
    # effects. When neither flag is given, definition stays None and nothing
    # external (PyYAML import, strategy-directory scan) ever runs.
    definition = None
    if args.strategy_file or args.strategy:
        exit_code, definition = _resolve_external_strategy(args)
        if definition is None:
            return exit_code, None

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

    # Issue #53: an external definition routes to its handler's registered
    # runner (currently only squeeze_breakout). All of its accepted fields are
    # either passed to the runner, already validated fixed-value contracts, or
    # recorded metadata -- nothing accepted is ignored (docs §5.2).
    if definition is not None:
        handler_runner = EXTERNAL_STRATEGY_HANDLER_RUNNERS[definition.entry.handler]
        return handler_runner(
            args,
            candles=candles,
            chart_db_path=chart_db_path,
            backtest_db_path=backtest_db_path,
            warmup_frame=warmup_frame,
            validation_frame=validation_frame,
            warmup_insufficient=warmup_insufficient,
            validation_start=validation_start,
            validation_end=validation_end,
            definition=definition,
        )

    # Issue #44: the squeeze-breakout profile has its own Trigger generation,
    # stop/target, and composite exit -- it does not use Market Bias, the
    # paper-signal candidate path, or the #38/#39 suppression flags. Route to it
    # here so none of the bias-resolution machinery below runs for it.
    if args.strategy_profile == squeeze_breakout.STRATEGY_PROFILE:
        return _run_squeeze_breakout(
            args,
            candles=candles,
            chart_db_path=chart_db_path,
            backtest_db_path=backtest_db_path,
            warmup_frame=warmup_frame,
            validation_frame=validation_frame,
            warmup_insufficient=warmup_insufficient,
            validation_start=validation_start,
            validation_end=validation_end,
        )

    # Issue #38/#40: two Market Bias resolution modes, both resolving the same
    # per-symbol-with-GLOBAL-fallback priority point-in-time (never a future
    # record). Issue #40: instead of the fixed (GLOBAL, 1h) scope, both modes now
    # resolve against args.symbol/args.timeframe first, falling back to GLOBAL --
    # so a GLOBAL-only DB behaves exactly as before, while a DB that also has a
    # symbol-specific bias prefers it.
    #   static      -- one effective bias as of --from, applied to every bar (default).
    #   time_series -- each bar's effective bias resolved at its own close via a
    #                  preloaded, priority-merged, binary-searched series (--time-series-bias).
    bias_scope_priority = [f"{scope_symbol}/{scope_timeframe}" for scope_symbol, scope_timeframe in effective_bias_scopes(args.symbol, args.timeframe)]
    bias_lookup = None
    if args.time_series_bias:
        bias_mode = BIAS_MODE_TIME_SERIES
        bias_series = load_effective_market_bias_series(args.symbol, args.timeframe, db_path=bias_db_path)
        if not bias_series:
            logger.warning(
                "No persisted Market Bias series found for symbol=%s timeframe=%s (nor a GLOBAL "
                "fallback) in %s; time-series bias will fall back to Neutral for every bar, so no "
                "signal candidates will be generated this run.",
                args.symbol, args.timeframe, bias_db_path,
            )
        bias_lookup = TimeSeriesBiasLookup(bias_series, neutral=dict(NEUTRAL_BIAS_PLACEHOLDER))
        # evaluate_backtest_bars still requires a bias_result argument; in
        # time-series mode bias_lookup supersedes it per bar, so pass a neutral
        # placeholder that is never actually consulted.
        bias_result = dict(NEUTRAL_BIAS_PLACEHOLDER)
        # Issue #40: which scopes actually contributed a record this run, and
        # whether GLOBAL was used as a fallback for at least part of the window.
        scopes_used = sorted({f"{row.get('symbol')}/{row.get('timeframe')}" for row in bias_series})
        bias_global_fallback = any(str(row.get("symbol")).upper() == GLOBAL_SCOPE_SYMBOL for row in bias_series)
        bias_resolution = {
            "mode": bias_mode,
            "requested_symbol": args.symbol,
            "requested_timeframe": args.timeframe,
            "canonical_symbol": normalize_bias_symbol(args.symbol),
            "scope_priority": bias_scope_priority,
            "scopes_used": scopes_used,
            "global_fallback": bias_global_fallback,
        }
    else:
        bias_mode = BIAS_MODE_STATIC
        bias_result = get_effective_market_bias_as_of(
            args.symbol, args.timeframe, validation_start_iso, db_path=bias_db_path
        )
        if bias_result is None:
            logger.warning(
                "No persisted Market Bias found at or before %s for symbol=%s timeframe=%s (nor a "
                "GLOBAL fallback) in %s; using a neutral placeholder, so no signal candidates will "
                "be generated this run.",
                args.from_time, args.symbol, args.timeframe, bias_db_path,
            )
            bias_result = dict(NEUTRAL_BIAS_PLACEHOLDER)
            resolved_scope = None
            bias_global_fallback = False
        else:
            resolved_scope = f"{bias_result.get('symbol')}/{bias_result.get('timeframe')}"
            bias_global_fallback = str(bias_result.get("symbol")).upper() == GLOBAL_SCOPE_SYMBOL
        bias_resolution = {
            "mode": bias_mode,
            "requested_symbol": args.symbol,
            "requested_timeframe": args.timeframe,
            "canonical_symbol": normalize_bias_symbol(args.symbol),
            "scope_priority": bias_scope_priority,
            "resolved_scope": resolved_scope,
            "global_fallback": bias_global_fallback,
        }

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
            # Issue #44: record which strategy this run replayed so a saved run
            # is self-identifying (the default trend_follow path here).
            "strategy_profile": args.strategy_profile,
            # Issue #26: record exactly which virtual-fill model produced this
            # run's entry/close prices, alongside the pre-existing keys.
            "execution_model": exec_model.to_config(),
            # Issue #38: record the opt-in realism settings for this run so a
            # saved run is self-describing (all three default to "off").
            "no_reentry": args.no_reentry,
            "cooldown_minutes": args.cooldown_minutes,
            "bias_mode": bias_mode,
            # Issue #40: record which symbol/timeframe bias this run requested,
            # the scope fallback priority, and whether a GLOBAL fallback was used
            # -- so a saved run is self-describing about per-symbol bias resolution.
            "bias_resolution": bias_resolution,
            # Issue #39: the Market State entry filter for this run, sorted for a
            # stable record, or null when no filter was applied (the default).
            "allowed_market_states": (
                sorted(args.allowed_market_states) if args.allowed_market_states is not None else None
            ),
        },
    )
    store.save_backtest_run(run_record, db_path=backtest_db_path)

    # Issue #38/#39: opt-in candidate suppression, enforced in strict entry-time
    # order (evaluations are already ascending). All three are global (one
    # position / one cooldown at a time), not per-side or per-signal-type.
    #   --allowed-market-states -- drop any candidate whose bar's Market State is
    #                       not in the allow-list (checked first: it is a property
    #                       of the bar itself, upstream of position management).
    #   --no-reentry     -- while a position is held, drop any candidate whose
    #                       entry falls in [held position entry, close_time].
    #                       An open position (close_time=None) is held until
    #                       validation_end.
    #   --cooldown-minutes N -- after a position closes at C, drop any candidate
    #                       whose entry is < C + N minutes.
    # The three suppression counters are mutually exclusive: a candidate is
    # counted under the FIRST reason that drops it (market_state, then re-entry,
    # then cooldown) and never re-tested against the others -- so the counts sum
    # to the number of suppressed candidates, never over-count one. With all
    # flags off (the defaults) no branch ever fires and every candidate is
    # adopted exactly as before.
    allowed_market_states = args.allowed_market_states
    cooldown_delta = pd.Timedelta(minutes=args.cooldown_minutes)
    held_until: pd.Timestamp | None = None
    cooldown_until: pd.Timestamp | None = None
    suppressed_market_state = 0
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
            # Issue #39: Market State gate first -- a candidate on a bar whose
            # state is not allowed is dropped before any position-management
            # check, and never touches held_until / cooldown_until (a
            # never-adopted position neither holds nor starts a cooldown).
            if allowed_market_states is not None and evaluation.market_state not in allowed_market_states:
                suppressed_market_state += 1
                continue
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
                # Issue #39: record this bar's classified Market State (and the
                # classifier's confidence) on the saved signal so by_market_state
                # aggregation has a real value, whether or not a state filter was
                # applied this run.
                market_state=evaluation.market_state,
                market_state_confidence=evaluation.market_state_confidence,
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
        # Issue #44: which strategy produced these signals (the default
        # trend_follow path here; the squeeze path sets its own).
        "strategy_profile": args.strategy_profile,
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
        # Issue #40: per-symbol bias resolution details for this run, and a quick
        # boolean for whether GLOBAL was used as a fallback (mirrors config_json).
        "bias_resolution": bias_resolution,
        "bias_global_fallback": bias_resolution["global_fallback"],
        # Issue #39: how many candidates the Market State filter dropped (0 when
        # --allowed-market-states was not given), and the allow-list itself
        # (sorted, or None when no filter was applied).
        "suppressed_market_state": suppressed_market_state,
        "allowed_market_states": (
            sorted(allowed_market_states) if allowed_market_states is not None else None
        ),
        # Issue #26: echo the virtual-fill model settings that produced this
        # run's entry/close prices (mirrors backtest_runs.config_json).
        "execution_model": exec_model.to_config(),
        # Issue #23: win/loss/pnl performance summary for this run, built
        # from the exact signal_records just saved (no extra DB round-trip
        # needed since they're already in memory). Safe when empty.
        # Issue #44 (Major): carry the run's strategy profile + execution
        # conditions on the summary (from the run config, not inferred from
        # signals) so every render identifies the strategy, even with 0 signals.
        "signal_summary": {
            **signal_summary.build_summary(run_record["id"], signal_records),
            "run_meta": _run_meta_from_run(run_record),
        },
    }
    return EXIT_OK, summary


def print_summary(summary: dict) -> None:
    print("=== Backtest Summary ===")
    print(f"Symbol:              {summary['symbol']}")
    print(f"Timeframe:           {summary['timeframe']}")
    # Issue #44: which strategy this run replayed.
    print(f"Strategy profile:    {summary.get('strategy_profile', STRATEGY_PROFILE_TREND_FOLLOW)}")
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
    # Issue #40: which scope the Market Bias resolved to and whether GLOBAL was
    # used as a fallback (per-symbol bias preferred when present).
    bias_resolution = summary.get("bias_resolution") or {}
    bias_scope = bias_resolution.get("resolved_scope") or (
        ", ".join(bias_resolution.get("scopes_used", [])) or "none"
    )
    print(
        f"Bias scope:          {bias_resolution.get('requested_symbol', summary.get('symbol'))}"
        f"/{bias_resolution.get('requested_timeframe', summary.get('timeframe'))} "
        f"-> {bias_scope}"
        + (" (GLOBAL fallback)" if summary.get("bias_global_fallback") else "")
    )
    # Issue #39: the Market State filter (if any) and its suppression count,
    # shown alongside #38's re-entry/cooldown counts.
    allowed_states = summary.get("allowed_market_states")
    print(f"Allowed states:      {', '.join(allowed_states) if allowed_states else 'all'}")
    print(
        f"Suppressed:          market-state={summary.get('suppressed_market_state', 0)}, "
        f"re-entry={summary.get('suppressed_reentry', 0)}, "
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
    # Issue #44 review (Major): re-render the strategy profile + execution
    # conditions from the saved run row (never inferred from its signals), so a
    # re-displayed run stays self-identifying in text / JSON / Markdown -- even
    # when it has zero signals.
    result["run_meta"] = _run_meta_from_run(run)

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
