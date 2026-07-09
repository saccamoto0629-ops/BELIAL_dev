"""Paper-only Exit / Invalidation policy evaluation for Backtest.

*** PAPER-ONLY BACKTEST EVALUATION. NOT AN EXECUTION LAYER. ***

This module answers, purely from historical candle data, "what would have
happened to this already-decided paper signal?" It never places a real
order, submits a stop-loss/take-profit order to a broker, closes a real
position, or connects to any broker/order API. Every result here is a
retrospective, simulated mark against past candles for backtest analysis
-- the same "observation, not execution" boundary the rest of BELIAL
keeps (see docs/product/concept.md's layer separation).

Vocabulary used throughout this module, and by callers persisting its
output (see backtest/backtest_models.apply_exit_policy_result() and
backtest/backtest_store.py's ``backtest_signals`` table):

- entry_price: the hypothetical paper entry price. This module never
  decides *when* or *at what price* a signal enters -- that is the
  caller's job (a strategy / Backtest runner upstream of this module).
- invalidation_price: the price level at which the original signal thesis
  is considered wrong. Used by the fixed_rr policy as its stop level.
  This is a paper-only marker, never a real stop-loss order.
- take_profit_price: the price level at which the trade is considered a
  paper win. Used by the fixed_rr policy. Never a real take-profit order.
- close_price / close_time / close_reason: the simulated outcome once a
  policy decides the position is done. These are historical marks against
  candle data, not a real fill from any broker or Execution layer.

Configuration is JSON-only, never read from ``.env`` directly
-----------------------------------------------------------------
``.env`` holds exactly one thing for this module: ``BACKTEST_EXIT_CONFIG_PATH``,
the path to a JSON config file. All Exit Policy *decision* parameters
(which policy is active, risk:reward, hold-bar limits, MA period, which
side wins when TP and SL are touched in the same candle, ...) live in that
JSON file -- never as their own env vars. See ``default_exit_policy_config()``
for the full shape, and ``load_exit_policy_config()`` / ``resolve_exit_policy_config()``
/ ``validate_exit_policy_config()`` for how a config is loaded, defaulted,
and validated.

Exit Policies (selected via config["exit_policy"]; each policy's own
parameters live under config["policies"][<name>]):

- fixed_rr: closes on the first candle whose high/low touches the
  take-profit or invalidation (stop) level, walking candles in order.
  Uses ``signal["invalidation_price"]``/``signal["take_profit_price"]``
  directly when both are present; otherwise both are derived from
  ``fallback_stop_pct`` (stop distance as a fraction of entry price) and
  ``risk_reward`` (take-profit distance = stop distance * risk_reward).
  The config also carries ``stop_loss_mode`` (e.g. "recent_swing") for
  future stop-sizing strategies, but today only the ``fallback_stop_pct``
  fixed-percentage sizing is implemented -- ``stop_loss_mode`` is accepted
  and passed through, not yet used to change behavior.
  If both the stop and target are touched within the same candle, which
  one "wins" is controlled by the top-level config["same_candle_priority"]
  ("stop_loss" or "take_profit") -- this is not hardcoded, so backtest
  runs stay reproducible from the config that produced them.
- time_based_close: closes once ``max_hold_bars`` candles have elapsed
  since entry, regardless of price. Reports the status named by
  ``policies.time_based_close.close_as`` (default "expired"), restricted
  to ``ALLOWED_TIME_BASED_CLOSE_STATUSES`` ("expired" or "closed") --
  this policy does not judge win/loss/invalidated on price, only that the
  hold duration ran out, so any other status value is rejected by
  ``validate_exit_policy_config()`` before evaluation ever runs.
- ma_break_invalidation: closes once price closes on the wrong side of a
  moving average for the signal's direction (long: close below the MA;
  short: close above the MA). ``ma_type`` currently only supports "sma";
  any other value (e.g. "ema") raises ValueError -- the config schema
  reserves the field for future moving-average types, but only SMA is
  implemented today.

If a policy's exit condition is never met within the supplied candles,
the result is status="open", close_reason="no_close" -- the evaluation
simply ran out of historical data to walk, which is not an error.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

POLICY_FIXED_RR = "fixed_rr"
POLICY_TIME_BASED_CLOSE = "time_based_close"
POLICY_MA_BREAK_INVALIDATION = "ma_break_invalidation"
# Reserved for future implementation: recognized by config validation and
# schema, but no evaluator exists yet -- selecting either (even if enabled)
# raises a clear "not yet implemented" error from evaluate_exit().
POLICY_BIAS_REVERSAL_CLOSE = "bias_reversal_close"
POLICY_RECENT_HIGH_LOW_BREAK = "recent_high_low_break"

KNOWN_POLICIES = {
    POLICY_FIXED_RR,
    POLICY_TIME_BASED_CLOSE,
    POLICY_MA_BREAK_INVALIDATION,
    POLICY_BIAS_REVERSAL_CLOSE,
    POLICY_RECENT_HIGH_LOW_BREAK,
}
DEFAULT_EXIT_POLICY = POLICY_FIXED_RR

SAME_CANDLE_PRIORITY_STOP_LOSS = "stop_loss"
SAME_CANDLE_PRIORITY_TAKE_PROFIT = "take_profit"
KNOWN_SAME_CANDLE_PRIORITIES = {SAME_CANDLE_PRIORITY_STOP_LOSS, SAME_CANDLE_PRIORITY_TAKE_PROFIT}
DEFAULT_SAME_CANDLE_PRIORITY = SAME_CANDLE_PRIORITY_STOP_LOSS

# ma_break_invalidation currently only implements a simple moving average.
KNOWN_MA_TYPES = {"sma"}

# status values persisted onto a signal record.
STATUS_OPEN = "open"
STATUS_WIN = "win"
STATUS_LOSS = "loss"
STATUS_EXPIRED = "expired"
STATUS_INVALIDATED = "invalidated"
STATUS_CLOSED = "closed"

KNOWN_RESULT_STATUSES = {
    STATUS_OPEN,
    STATUS_WIN,
    STATUS_LOSS,
    STATUS_EXPIRED,
    STATUS_INVALIDATED,
    STATUS_CLOSED,
}

# time_based_close never judges price, so it may only ever report a
# time-driven outcome -- never win/loss/invalidated/open, which would
# misrepresent what this policy actually decided.
ALLOWED_TIME_BASED_CLOSE_STATUSES = {STATUS_EXPIRED, STATUS_CLOSED}

# close_reason values persisted onto a signal record.
REASON_TAKE_PROFIT = "take_profit"
REASON_STOP_LOSS = "stop_loss"
REASON_TIME_EXPIRED = "time_expired"
REASON_MA_BREAK = "ma_break"
REASON_BIAS_REVERSAL = "bias_reversal"  # reserved: not triggered by any policy below yet
REASON_RECENT_HIGH_LOW_BREAK = "recent_high_low_break"  # reserved: not triggered yet
REASON_NO_CLOSE = "no_close"

LONG = "long"
SHORT = "short"

# Columns each implemented policy needs to walk candles without a raw KeyError.
_REQUIRED_CANDLE_COLUMNS = {
    POLICY_FIXED_RR: {"high", "low", "close"},
    POLICY_TIME_BASED_CLOSE: {"high", "low", "close"},
    POLICY_MA_BREAK_INVALIDATION: {"high", "low", "close"},
}


def _load_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


@dataclass(frozen=True)
class ExitPolicyResult:
    """Paper-only, historical/simulated exit evaluation for one signal.

    Never a real position close, stop-loss order, take-profit order, or
    any broker/Execution-layer action -- see the module docstring.

    ``mfe`` / ``mae`` are the running max-favorable / max-adverse price
    excursion (signed, in price units, relative to entry) observed while
    walking the candles up to (and including) the close, or ``None`` if no
    candles were evaluated at all.
    """

    status: str
    close_price: float | None
    close_time: str | None
    close_reason: str
    exit_policy: str
    mfe: float | None = None
    mae: float | None = None


# --- Exit Policy config: JSON-only, .env holds only the file path -----------


def default_exit_policy_config() -> dict:
    """Return a safe, fully-specified default Exit Policy config.

    Every key ``validate_exit_policy_config()`` checks is present here
    with a valid value, so a caller (or ``resolve_exit_policy_config()``)
    can start from this and override only what it needs.
    """
    return {
        "exit_policy": DEFAULT_EXIT_POLICY,
        "same_candle_priority": DEFAULT_SAME_CANDLE_PRIORITY,
        "policies": {
            POLICY_FIXED_RR: {
                "enabled": True,
                "risk_reward": 2.0,
                "stop_loss_mode": "fixed_pct",
                "fallback_stop_pct": 0.003,
            },
            POLICY_TIME_BASED_CLOSE: {
                "enabled": True,
                "max_hold_bars": 48,
                "close_as": STATUS_EXPIRED,
            },
            POLICY_MA_BREAK_INVALIDATION: {
                "enabled": False,
                "ma_type": "sma",
                "period": 20,
            },
            POLICY_BIAS_REVERSAL_CLOSE: {
                "enabled": False,
            },
            POLICY_RECENT_HIGH_LOW_BREAK: {
                "enabled": False,
                "lookback_bars": 20,
            },
        },
    }


def validate_exit_policy_config(config: dict) -> None:
    """Raise ValueError for any structurally or semantically invalid Exit Policy config.

    Does not mutate ``config`` and does not fill in missing values --
    callers that want defaults filled in first should go through
    ``resolve_exit_policy_config()`` / ``load_exit_policy_config()``, both
    of which always call this before returning.
    """
    exit_policy = config.get("exit_policy")
    if exit_policy not in KNOWN_POLICIES:
        raise ValueError(f"Unknown exit_policy: {exit_policy!r}. Known policies: {sorted(KNOWN_POLICIES)}.")

    same_candle_priority = config.get("same_candle_priority")
    if same_candle_priority not in KNOWN_SAME_CANDLE_PRIORITIES:
        raise ValueError(
            f"Invalid same_candle_priority: {same_candle_priority!r}. "
            f"Must be one of {sorted(KNOWN_SAME_CANDLE_PRIORITIES)}."
        )

    policies = config.get("policies") or {}

    fixed_rr_config = policies.get(POLICY_FIXED_RR) or {}
    risk_reward = fixed_rr_config.get("risk_reward")
    if risk_reward is not None and risk_reward <= 0:
        raise ValueError(f"policies.fixed_rr.risk_reward must be > 0, got {risk_reward!r}.")
    fallback_stop_pct = fixed_rr_config.get("fallback_stop_pct")
    if fallback_stop_pct is not None and fallback_stop_pct <= 0:
        raise ValueError(f"policies.fixed_rr.fallback_stop_pct must be > 0, got {fallback_stop_pct!r}.")

    time_based_close_config = policies.get(POLICY_TIME_BASED_CLOSE) or {}
    max_hold_bars = time_based_close_config.get("max_hold_bars")
    if max_hold_bars is not None and max_hold_bars <= 0:
        raise ValueError(f"policies.time_based_close.max_hold_bars must be > 0, got {max_hold_bars!r}.")
    close_as = time_based_close_config.get("close_as")
    if close_as is not None and close_as not in ALLOWED_TIME_BASED_CLOSE_STATUSES:
        raise ValueError(
            f"policies.time_based_close.close_as={close_as!r} is not allowed. "
            f"Must be one of {sorted(ALLOWED_TIME_BASED_CLOSE_STATUSES)}."
        )

    ma_break_config = policies.get(POLICY_MA_BREAK_INVALIDATION) or {}
    ma_type = ma_break_config.get("ma_type")
    if ma_type is not None and ma_type not in KNOWN_MA_TYPES:
        raise ValueError(
            f"policies.ma_break_invalidation.ma_type={ma_type!r} is not supported yet. "
            f"Supported: {sorted(KNOWN_MA_TYPES)}."
        )
    period = ma_break_config.get("period")
    if period is not None and period <= 0:
        raise ValueError(f"policies.ma_break_invalidation.period must be > 0, got {period!r}.")

    active_policy_config = policies.get(exit_policy) or {}
    if not active_policy_config.get("enabled", False):
        raise ValueError(
            f"exit_policy={exit_policy!r} is selected but policies.{exit_policy}.enabled is not true."
        )


def resolve_exit_policy_config(config: dict | None) -> dict:
    """Merge ``config`` onto safe defaults, then validate.

    Fills any missing top-level keys, and any missing keys within each
    ``policies[...]`` block, from ``default_exit_policy_config()`` -- so a
    caller only needs to specify what differs from the default. Always
    ends with ``validate_exit_policy_config()``.
    """
    defaults = default_exit_policy_config()
    config = config or {}

    merged = dict(defaults)
    merged.update({key: value for key, value in config.items() if key != "policies"})

    default_policies = defaults["policies"]
    user_policies = config.get("policies") or {}
    merged_policies = {}
    for policy_name in set(default_policies) | set(user_policies):
        block = dict(default_policies.get(policy_name, {}))
        block.update(user_policies.get(policy_name, {}))
        merged_policies[policy_name] = block
    merged["policies"] = merged_policies

    validate_exit_policy_config(merged)
    return merged


def load_exit_policy_config(path: str | Path | None = None, *, env_values: dict[str, str] | None = None) -> dict:
    """Load, default-fill, and validate an Exit Policy JSON config.

    Resolution order for the file path:
      1. the explicit ``path`` argument
      2. ``BACKTEST_EXIT_CONFIG_PATH`` (process env, then ``paper-signal/.env``)
      3. no path configured anywhere -> return ``default_exit_policy_config()``

    A configured path that does not exist raises ``FileNotFoundError``
    (the user pointed somewhere specific; silently falling back to
    defaults would hide a real misconfiguration). A configured path whose
    contents are not valid JSON raises ``ValueError`` with a clear message
    (never a bare ``json.JSONDecodeError`` leaking out).
    """
    resolved_path = path
    if resolved_path is None:
        values = env_values if env_values is not None else _load_env_file()
        resolved_path = os.getenv("BACKTEST_EXIT_CONFIG_PATH") or values.get("BACKTEST_EXIT_CONFIG_PATH")

    if not resolved_path:
        return default_exit_policy_config()

    config_path = Path(resolved_path)
    if not config_path.is_absolute():
        config_path = BASE_DIR / config_path
    if not config_path.exists():
        raise FileNotFoundError(f"BACKTEST_EXIT_CONFIG_PATH points to a missing file: {config_path}")

    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Exit Policy config at {config_path} is not valid JSON: {exc}") from exc

    return resolve_exit_policy_config(loaded)


# --- candle walking helpers ---------------------------------------------------


def _format_time(value) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _track_excursion(
    direction: str, entry_price: float, high: float, low: float, mfe: float, mae: float
) -> tuple[float, float]:
    """Return updated (mfe, mae): running max-favorable / max-adverse excursion."""
    if direction == LONG:
        favorable = high - entry_price
        adverse = low - entry_price
    else:
        favorable = entry_price - low
        adverse = entry_price - high
    return max(mfe, favorable), min(mae, adverse)


def _no_close(exit_policy: str, *, mfe: float | None = None, mae: float | None = None) -> ExitPolicyResult:
    return ExitPolicyResult(
        status=STATUS_OPEN,
        close_price=None,
        close_time=None,
        close_reason=REASON_NO_CLOSE,
        exit_policy=exit_policy,
        mfe=mfe,
        mae=mae,
    )


def _validate_candle_columns(policy: str, candles: pd.DataFrame) -> None:
    required = _REQUIRED_CANDLE_COLUMNS.get(policy, set())
    missing = required - set(candles.columns)
    if missing:
        raise ValueError(f"candles is missing required column(s) for {policy!r}: {sorted(missing)}.")


# --- policy evaluators ---------------------------------------------------------


def _evaluate_fixed_rr(
    signal: dict, candles: pd.DataFrame, policy_config: dict, *, same_candle_priority: str
) -> ExitPolicyResult:
    """Fixed risk:reward exit: first candle to touch stop or target wins.

    See the module docstring's fixed_rr entry for the full price-resolution
    and same-candle-priority rules.
    """
    direction = signal["direction"]
    entry_price = float(signal["entry_price"])

    stop_price = signal.get("invalidation_price")
    tp_price = signal.get("take_profit_price")
    if stop_price is None or tp_price is None:
        fallback_stop_pct = policy_config.get("fallback_stop_pct")
        if fallback_stop_pct is None:
            raise ValueError(
                "fixed_rr requires either signal['invalidation_price'] and "
                "signal['take_profit_price'], or policies.fixed_rr.fallback_stop_pct "
                "in the Exit Policy config."
            )
        risk_reward = policy_config.get("risk_reward", 2.0)
        stop_distance = entry_price * fallback_stop_pct
        tp_distance = stop_distance * risk_reward
        if direction == LONG:
            stop_price = entry_price - stop_distance
            tp_price = entry_price + tp_distance
        else:
            stop_price = entry_price + stop_distance
            tp_price = entry_price - tp_distance
    stop_price = float(stop_price)
    tp_price = float(tp_price)

    mfe = 0.0
    mae = 0.0
    for timestamp, row in candles.iterrows():
        high = float(row["high"])
        low = float(row["low"])
        mfe, mae = _track_excursion(direction, entry_price, high, low, mfe, mae)

        if direction == LONG:
            hit_stop = low <= stop_price
            hit_tp = high >= tp_price
        else:
            hit_stop = high >= stop_price
            hit_tp = low <= tp_price

        if hit_stop and hit_tp:
            wins = REASON_TAKE_PROFIT if same_candle_priority == SAME_CANDLE_PRIORITY_TAKE_PROFIT else REASON_STOP_LOSS
        elif hit_stop:
            wins = REASON_STOP_LOSS
        elif hit_tp:
            wins = REASON_TAKE_PROFIT
        else:
            continue

        if wins == REASON_STOP_LOSS:
            return ExitPolicyResult(
                status=STATUS_LOSS,
                close_price=stop_price,
                close_time=_format_time(timestamp),
                close_reason=REASON_STOP_LOSS,
                exit_policy=POLICY_FIXED_RR,
                mfe=mfe,
                mae=mae,
            )
        return ExitPolicyResult(
            status=STATUS_WIN,
            close_price=tp_price,
            close_time=_format_time(timestamp),
            close_reason=REASON_TAKE_PROFIT,
            exit_policy=POLICY_FIXED_RR,
            mfe=mfe,
            mae=mae,
        )

    return _no_close(POLICY_FIXED_RR, mfe=mfe, mae=mae)


def _evaluate_time_based_close(
    signal: dict, candles: pd.DataFrame, policy_config: dict, *, same_candle_priority: str
) -> ExitPolicyResult:
    """Time-based exit: closes at market once ``max_hold_bars`` candles have elapsed.

    Reports ``policies.time_based_close.close_as`` as the status (default
    "expired") -- this policy never judges win/loss, only that the hold
    duration ran out.
    """
    direction = signal["direction"]
    entry_price = float(signal["entry_price"])
    max_hold_bars = policy_config.get("max_hold_bars")
    if max_hold_bars is None or max_hold_bars <= 0:
        raise ValueError("time_based_close requires policies.time_based_close.max_hold_bars > 0.")
    close_as = policy_config.get("close_as", STATUS_EXPIRED)

    mfe = 0.0
    mae = 0.0
    for position, (timestamp, row) in enumerate(candles.iterrows(), start=1):
        high = float(row["high"])
        low = float(row["low"])
        mfe, mae = _track_excursion(direction, entry_price, high, low, mfe, mae)

        if position >= max_hold_bars:
            return ExitPolicyResult(
                status=close_as,
                close_price=float(row["close"]),
                close_time=_format_time(timestamp),
                close_reason=REASON_TIME_EXPIRED,
                exit_policy=POLICY_TIME_BASED_CLOSE,
                mfe=mfe,
                mae=mae,
            )

    return _no_close(POLICY_TIME_BASED_CLOSE, mfe=mfe, mae=mae)


def _evaluate_ma_break_invalidation(
    signal: dict, candles: pd.DataFrame, policy_config: dict, *, same_candle_priority: str
) -> ExitPolicyResult:
    """MA-break invalidation: closes when price closes on the wrong side of an MA.

    Long signals invalidate on a close below the moving average; short
    signals invalidate on a close above it. Only ``ma_type: "sma"`` is
    implemented today (validated in ``validate_exit_policy_config()``).
    """
    direction = signal["direction"]
    entry_price = float(signal["entry_price"])
    ma_type = policy_config.get("ma_type", "sma")
    if ma_type not in KNOWN_MA_TYPES:
        raise ValueError(
            f"ma_break_invalidation.ma_type={ma_type!r} is not supported yet. Supported: {sorted(KNOWN_MA_TYPES)}."
        )
    period = policy_config.get("period")
    if period is None or period <= 0:
        raise ValueError("ma_break_invalidation requires policies.ma_break_invalidation.period > 0.")
    period = int(period)

    moving_average = candles["close"].rolling(window=period, min_periods=period).mean()

    mfe = 0.0
    mae = 0.0
    for timestamp, row in candles.iterrows():
        high = float(row["high"])
        low = float(row["low"])
        mfe, mae = _track_excursion(direction, entry_price, high, low, mfe, mae)

        ma_value = moving_average.loc[timestamp]
        if pd.isna(ma_value):
            continue

        close = float(row["close"])
        broke = (direction == LONG and close < ma_value) or (direction == SHORT and close > ma_value)
        if broke:
            return ExitPolicyResult(
                status=STATUS_INVALIDATED,
                close_price=close,
                close_time=_format_time(timestamp),
                close_reason=REASON_MA_BREAK,
                exit_policy=POLICY_MA_BREAK_INVALIDATION,
                mfe=mfe,
                mae=mae,
            )

    return _no_close(POLICY_MA_BREAK_INVALIDATION, mfe=mfe, mae=mae)


_POLICY_EVALUATORS = {
    POLICY_FIXED_RR: _evaluate_fixed_rr,
    POLICY_TIME_BASED_CLOSE: _evaluate_time_based_close,
    POLICY_MA_BREAK_INVALIDATION: _evaluate_ma_break_invalidation,
}


def evaluate_exit(signal: dict, candles: pd.DataFrame, config: dict | None = None) -> ExitPolicyResult:
    """Evaluate a paper-only, historical exit for *signal* against *candles*.

    PAPER-ONLY BACKTEST EVALUATION -- see the module docstring. Never
    places an order, closes a real position, or calls a broker API.

    ``signal`` must contain ``direction`` ("long"/"short") and
    ``entry_price``; individual policies may read additional fields (see
    each policy function's docstring, and the module docstring's
    Vocabulary section for ``invalidation_price`` / ``take_profit_price``).

    ``candles`` must be a DataFrame indexed by timestamp with at least
    ``high``/``low``/``close`` columns, containing only bars at or after
    the signal's entry -- passing earlier candles would leak look-ahead
    data into the evaluation. An empty/``None`` frame returns an "open"
    (no_close) result, not an error; a non-empty frame missing a required
    column raises ``ValueError`` naming the missing column(s).

    ``config`` is the JSON-shaped Exit Policy config (see
    ``default_exit_policy_config()``); ``None`` or a partial dict is
    resolved via ``resolve_exit_policy_config()`` (defaults filled in,
    then validated) on every call.
    """
    resolved_config = resolve_exit_policy_config(config)
    exit_policy = resolved_config["exit_policy"]
    same_candle_priority = resolved_config["same_candle_priority"]
    active_policy_config = resolved_config["policies"].get(exit_policy, {})

    evaluator = _POLICY_EVALUATORS.get(exit_policy)
    if evaluator is None:
        raise ValueError(
            f"Exit policy {exit_policy!r} is recognized by config validation but not yet implemented."
        )

    if signal.get("direction") not in (LONG, SHORT):
        raise ValueError(f"signal['direction'] must be 'long' or 'short', got {signal.get('direction')!r}.")
    if "entry_price" not in signal:
        raise ValueError("signal['entry_price'] is required.")

    if candles is None or candles.empty:
        return _no_close(exit_policy)

    _validate_candle_columns(exit_policy, candles)

    return evaluator(signal, candles, active_policy_config, same_candle_priority=same_candle_priority)
