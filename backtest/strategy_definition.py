"""Issue #53: external Strategy definition Loader / Validator (Issue #52 schema).

*** DECLARATIVE CONFIGURATION ONLY. NOT AN EXECUTION LAYER. ***

Implements the contract fixed by ``docs/architecture/strategy_definition.md``:
one YAML file describes one Strategy's *declarative, per-strategy-variable*
settings (symbols/timeframes, entry handler + params, risk/exit numbers).
Indicator math, Market State classification, trigger conditions, and the
entry/exit fill-judgement order all stay in Python (``squeeze_breakout.py``,
``market_state.py``, ...) -- a definition file selects a registered handler by
name and parameterizes it, it can never inject code.

Key properties (all from the Issue #52 design, not re-decided here):

- ``yaml.safe_load()`` only -- YAML tags / arbitrary object construction and
  multi-document streams are rejected. The parsed data is plain dict/list/
  scalar immediately; the input mapping is never mutated.
- Defaults are resolved *here* and stored on the typed, frozen result, so
  downstream logic never depends on "unspecified means some hardcoded
  constant". Every load builds fresh containers (no shared mutable defaults),
  and the result is *deeply* immutable: params / stop_loss / take_profit are
  read-only mappings (list values stored as tuples), so the "validated"
  guarantee cannot be broken by mutating the object after the fact. Only
  ``to_config()`` produces mutable (fresh, JSON-compatible) copies.
- Unknown keys are rejected fail-closed everywhere except inside
  ``entry.params`` structurally -- and even there, the selected handler's
  capability descriptor then rejects params it does not support, so a setting
  that would be silently ignored at runtime ("dead config") cannot pass
  validation.
- Every error is a ``StrategyDefinitionError`` carrying the source file path,
  the strategy id (when already known), the offending field path, the actual
  value/type, and what was expected -- fail-fast, first error only (same
  behaviour as ``exit_policy.validate_exit_policy_config``).

Note on YAML anchors/aliases: the design doc says anchors are "not used";
``yaml.safe_load()`` (the mandated API) resolves aliases into plain data
rather than rejecting them. Because params values are restricted to scalars /
flat scalar lists and every list is copied into the typed result, a resolved
alias cannot introduce shared mutable state here.
"""

from __future__ import annotations

import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
MARKET_WATCH_DIR = REPO_ROOT / "market-watch"

for _dir in (BASE_DIR, MARKET_WATCH_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

from market_state import (  # noqa: E402  (market-watch: the one source of state names + squeeze defaults)
    DEFAULT_SQUEEZE_LOOKBACK,
    DEFAULT_SQUEEZE_PERCENTILE,
    KNOWN_MARKET_STATES,
)

# --- schema contract (docs/architecture/strategy_definition.md) ---------------

SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

ID_PATTERN = re.compile(r"^[a-z0-9_]+$")

REENTRY_OVERLAP = "overlap"
REENTRY_SINGLE_POSITION = "single_position"
REENTRY_NEW_SETUP = "new_setup"
KNOWN_REENTRY_MODES = (REENTRY_OVERLAP, REENTRY_SINGLE_POSITION, REENTRY_NEW_SETUP)
DEFAULT_REENTRY = REENTRY_OVERLAP

STOP_LOSS_TRIGGER_BAR_EXTREME = "trigger_bar_extreme"
STOP_LOSS_FIXED_PCT = "fixed_pct"
KNOWN_STOP_LOSS_TYPES = (STOP_LOSS_TRIGGER_BAR_EXTREME, STOP_LOSS_FIXED_PCT)

TAKE_PROFIT_R_MULTIPLE = "r_multiple"
KNOWN_TAKE_PROFIT_TYPES = (TAKE_PROFIT_R_MULTIPLE,)

# Same vocabulary as exit_policy.py / paper-execution's execution_profile.py so
# the layers stay cross-checkable (pinned by a test against exit_policy).
SAME_CANDLE_PRIORITY_STOP_LOSS = "stop_loss"
SAME_CANDLE_PRIORITY_TAKE_PROFIT = "take_profit"
KNOWN_SAME_CANDLE_PRIORITIES = (SAME_CANDLE_PRIORITY_STOP_LOSS, SAME_CANDLE_PRIORITY_TAKE_PROFIT)
DEFAULT_SAME_CANDLE_PRIORITY = SAME_CANDLE_PRIORITY_STOP_LOSS

KNOWN_TOP_LEVEL_KEYS = (
    "schema_version",
    "id",
    "name",
    "description",
    "symbols",
    "timeframes",
    "allowed_market_states",
    "entry",
    "risk",
    "exit",
)
KNOWN_ENTRY_KEYS = ("handler", "params", "reentry", "cooldown_bars")
KNOWN_RISK_KEYS = ("stop_loss", "take_profit")
KNOWN_STOP_LOSS_KEYS = ("type", "pct")
KNOWN_TAKE_PROFIT_KEYS = ("type", "r")
KNOWN_EXIT_KEYS = ("max_holding_bars", "same_candle_priority")

# --- runtime placement (docs §5.1) --------------------------------------------

STRATEGY_DIR_ENV_VAR = "STRATEGY_DEFINITIONS_DIR"
DEFAULT_STRATEGY_DIR = BASE_DIR / "strategies"
STRATEGY_FILE_SUFFIXES = (".yaml", ".yml")

_MISSING = object()


def strategy_definitions_dir() -> Path:
    """The directory ``--strategy <id>`` scans: env override or backtest/strategies/."""
    override = os.environ.get(STRATEGY_DIR_ENV_VAR)
    if override:
        return Path(override)
    return DEFAULT_STRATEGY_DIR


class StrategyDefinitionError(ValueError):
    """Any invalid Strategy definition: unreadable file, bad YAML, schema violation.

    The message always names the source file (when file-loaded), the strategy
    id (once known), and the offending field path, so the fix location is
    identifiable from the error alone (Issue #53 acceptance criteria).
    """

    def __init__(self, message: str, *, source_path=None, strategy_id=None, field_path=None):
        details = []
        if source_path is not None:
            details.append(f"file: {source_path}")
        if strategy_id is not None:
            details.append(f"strategy id: {strategy_id}")
        if field_path is not None:
            details.append(f"field: {field_path}")
        prefix = f"[{'; '.join(details)}] " if details else ""
        super().__init__(prefix + message)
        self.source_path = str(source_path) if source_path is not None else None
        self.strategy_id = strategy_id
        self.field_path = field_path


# --- typed internal representation (docs §10) ----------------------------------


@dataclass(frozen=True)
class EntrySpec:
    handler: str
    # Resolved params (handler defaults filled). Deeply immutable: a read-only
    # mapping whose list values were converted to tuples at build time, so the
    # "validated" guarantee cannot be broken after the fact -- use
    # StrategyDefinition.to_config() for a mutable JSON-compatible copy.
    params: Mapping
    reentry: str  # one of KNOWN_REENTRY_MODES
    cooldown_bars: int


@dataclass(frozen=True)
class RiskSpec:
    # Both are read-only mappings (scalar values only), immutable after
    # validation just like EntrySpec.params.
    stop_loss: Mapping  # {"type": ...} (+ "pct" for fixed_pct)
    take_profit: Mapping  # {"type": "r_multiple", "r": <float>}


@dataclass(frozen=True)
class ExitSpec:
    max_holding_bars: int
    same_candle_priority: str


@dataclass(frozen=True)
class StrategyDefinition:
    """One validated, default-resolved external Strategy definition."""

    schema_version: int
    id: str
    name: str
    description: str
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...]
    allowed_market_states: frozenset[str] | None
    entry: EntrySpec
    risk: RiskSpec
    exit: ExitSpec
    source_path: str | None

    def to_config(self) -> dict:
        """JSON-serializable normalized form for ``backtest_runs.config_json``.

        Returns a fresh structure on every call (never NaN/Infinity: the
        validator already rejected them, matching report.render_json's
        ``allow_nan=False``), so callers can never mutate the definition
        through the returned dict.
        """
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "symbols": list(self.symbols),
            "timeframes": list(self.timeframes),
            "allowed_market_states": (
                sorted(self.allowed_market_states) if self.allowed_market_states is not None else None
            ),
            "entry": {
                "handler": self.entry.handler,
                "params": _copy_params(self.entry.params),
                "reentry": self.entry.reentry,
                "cooldown_bars": self.entry.cooldown_bars,
            },
            "risk": {
                "stop_loss": dict(self.risk.stop_loss),
                "take_profit": dict(self.risk.take_profit),
            },
            "exit": {
                "max_holding_bars": self.exit.max_holding_bars,
                "same_candle_priority": self.exit.same_candle_priority,
            },
            "source_path": self.source_path,
        }


def _copy_params(params: Mapping) -> dict:
    """Fresh mutable JSON-compatible copy of an immutable params mapping.

    The stored params hold tuples for list-valued params (deep immutability);
    the JSON form converts them back to lists.
    """
    return {key: (list(value) if isinstance(value, tuple) else value) for key, value in params.items()}


# --- handler registry + capability descriptors (docs §4 / §4.1) ----------------


@dataclass(frozen=True)
class HandlerParamSpec:
    """One handler-owned ``entry.params`` key: its allowed values and default.

    Exactly one contract form applies per param:

    - ``allowed_values`` (a tuple): exhaustively-enumerable fixed-value
      contract (docs §5.2 ③) -- any other value is a validation error.
    - ``allowed_values is None``: a range-validated numeric contract (PR #62
      review: squeeze_lookback / squeeze_percentile become variable execution
      parameters, docs §5.2 ①). ``int_min`` requires a strict int >=
      ``int_min``; otherwise ``number_gt`` / ``number_max`` require a finite
      number with ``number_gt < value <= number_max``.
    """

    default: object
    allowed_values: tuple | None = None
    int_min: int | None = None
    number_gt: float | None = None
    number_max: float | None = None


@dataclass(frozen=True)
class HandlerCapability:
    """What a registered handler supports; validated against every definition.

    A field the handler supports only as a single fixed value must list
    exactly that value -- and (docs §5.2 ③) that value must match the
    handler's actual runtime behaviour, pinned by tests. A definition naming
    an unsupported field/value is a validation error, never silently ignored.
    """

    name: str
    params: Mapping[str, HandlerParamSpec]
    supports_market_state_gate: bool  # False -> allowed_market_states must be null
    allowed_reentry_modes: frozenset[str]
    allowed_cooldown_bars: frozenset[int] | None  # None = any int >= 0
    allowed_stop_loss_types: frozenset[str]
    allowed_take_profit_types: frozenset[str]
    allowed_same_candle_priorities: frozenset[str]


HANDLER_SQUEEZE_BREAKOUT = "squeeze_breakout"

# The registry is owned by Python code: an external file can only *select* one
# of these names; it can never define, replace, or inject a handler. The fixed
# values below are the squeeze_breakout implementation contract from docs §4.1
# / §5.2 (each pinned to the actual squeeze_breakout.py behaviour by
# tests/test_strategy_definition.py, so descriptor and execution cannot drift).
HANDLER_REGISTRY: dict[str, HandlerCapability] = {
    HANDLER_SQUEEZE_BREAKOUT: HandlerCapability(
        name=HANDLER_SQUEEZE_BREAKOUT,
        params={
            # squeeze_breakout.SQUEEZE_STATE: the episode "arming" state label.
            "squeeze_state": HandlerParamSpec(allowed_values=("squeeze",), default="squeeze"),
            # Only the episode-release bar (first confirmed non-squeeze bar) is
            # ever evaluated as a Trigger.
            "require_episode_release": HandlerParamSpec(allowed_values=(True,), default=True),
            # Bollinger *close* break (a wick-only poke is never a Trigger);
            # matches squeeze_breakout.TRIGGER_TYPE ("bollinger_close_break").
            "breakout": HandlerParamSpec(allowed_values=("bollinger_close",), default="bollinger_close"),
            # PR #62 review (Issue #54): the squeeze-episode detection window
            # and percentile become variable execution parameters, passed
            # explicitly to classify_market_state(). Defaults come from
            # market_state so an omitted value is byte-identical to the
            # pre-migration behaviour (and to the live classifier).
            "squeeze_lookback": HandlerParamSpec(default=DEFAULT_SQUEEZE_LOOKBACK, int_min=1),
            "squeeze_percentile": HandlerParamSpec(
                default=DEFAULT_SQUEEZE_PERCENTILE, number_gt=0.0, number_max=1.0
            ),
        },
        # Issue #63: squeeze_breakout consults the market-state gate on the
        # Trigger bar (the episode-release bar whose close fired the breakout);
        # a disallowed state adopts no entry and leaves the episode armed.
        # null (the default) = no gate, byte-identical to the pre-#63 runs.
        supports_market_state_gate=True,
        allowed_reentry_modes=frozenset({REENTRY_NEW_SETUP}),
        allowed_cooldown_bars=frozenset({0}),
        allowed_stop_loss_types=frozenset({STOP_LOSS_TRIGGER_BAR_EXTREME}),
        allowed_take_profit_types=frozenset({TAKE_PROFIT_R_MULTIPLE}),
        allowed_same_candle_priorities=frozenset({SAME_CANDLE_PRIORITY_STOP_LOSS}),
    ),
}


def registered_handler_names() -> tuple[str, ...]:
    return tuple(sorted(HANDLER_REGISTRY))


# --- validation helpers ---------------------------------------------------------


def _type_name(value) -> str:
    return type(value).__name__


def _strict_in(value, allowed: tuple) -> bool:
    """Membership that never conflates bool with int (True == 1 in Python)."""
    return any(value == candidate and type(value) is type(candidate) for candidate in allowed)


def _is_strict_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


_PARAM_SCALAR_TYPES = (str, int, float, bool, type(None))


# --- validation / build ---------------------------------------------------------


def build_strategy_definition(data: Mapping, *, source_path=None) -> StrategyDefinition:
    """Validate an already-parsed mapping and build the typed representation.

    ``data`` is read-only here: defaults are resolved into fresh containers and
    the input is never mutated. Raises ``StrategyDefinitionError`` on the first
    violation (fail-fast, deterministic order).
    """
    if not isinstance(data, Mapping):
        raise StrategyDefinitionError(
            f"top-level must be a mapping (key: value pairs), got {_type_name(data)}",
            source_path=source_path,
        )

    raw_id = data.get("id")
    strategy_id = raw_id if isinstance(raw_id, str) and raw_id else None

    def fail(field_path: str | None, message: str):
        raise StrategyDefinitionError(
            message, source_path=source_path, strategy_id=strategy_id, field_path=field_path
        )

    # schema_version first: an unsupported version must be reported as such,
    # not as a cascade of unknown-key/type errors from a future schema.
    schema_version = data.get("schema_version", _MISSING)
    if schema_version is _MISSING:
        fail("schema_version", "required key is missing; expected an int (supported: 1)")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        fail(
            "schema_version",
            f"must be an int, got {schema_version!r} ({_type_name(schema_version)})",
        )
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        fail(
            "schema_version",
            f"unsupported schema_version {schema_version}; "
            f"supported versions: {sorted(SUPPORTED_SCHEMA_VERSIONS)}",
        )

    unknown = sorted(set(data) - set(KNOWN_TOP_LEVEL_KEYS))
    if unknown:
        fail(
            None,
            f"unknown top-level key(s): {', '.join(map(repr, unknown))}. "
            f"Known keys: {', '.join(KNOWN_TOP_LEVEL_KEYS)}",
        )

    definition_id = _required_str(data, "id", fail)
    if not ID_PATTERN.match(definition_id):
        fail("id", f"must match ^[a-z0-9_]+$ (lowercase ascii, digits, underscore), got {definition_id!r}")
    strategy_id = definition_id  # from here on, every error carries the id

    name = _required_str(data, "name", fail)

    description = data.get("description", _MISSING)
    if description is _MISSING:
        description = ""
    elif not isinstance(description, str):
        fail("description", f"must be a str, got {description!r} ({_type_name(description)})")

    symbols = _required_str_list(data, "symbols", fail)
    timeframes = _required_str_list(data, "timeframes", fail)

    allowed_market_states = _validate_allowed_market_states(data, fail)

    entry_raw = _required_mapping(data, "entry", fail, known_keys=KNOWN_ENTRY_KEYS)
    handler_name = _required_str(entry_raw, "handler", fail, field_prefix="entry.")
    if handler_name not in HANDLER_REGISTRY:
        fail(
            "entry.handler",
            f"unknown handler {handler_name!r}; registered handlers: "
            f"{', '.join(registered_handler_names())}",
        )
    capability = HANDLER_REGISTRY[handler_name]

    params = _validate_entry_params(entry_raw, capability, fail)
    reentry = _validate_reentry(entry_raw, capability, fail)
    cooldown_bars = _validate_cooldown_bars(entry_raw, capability, fail)

    risk_raw = _required_mapping(data, "risk", fail, known_keys=KNOWN_RISK_KEYS)
    stop_loss = _validate_stop_loss(risk_raw, capability, fail)
    take_profit = _validate_take_profit(risk_raw, capability, fail)

    exit_raw = _required_mapping(data, "exit", fail, known_keys=KNOWN_EXIT_KEYS)
    max_holding_bars = _validate_max_holding_bars(exit_raw, fail)
    same_candle_priority = _validate_same_candle_priority(exit_raw, capability, fail)

    # Capability check for the market-state gate runs after the generic checks
    # so an unknown state name is reported as such, not as a handler mismatch.
    if allowed_market_states is not None and not capability.supports_market_state_gate:
        fail(
            "allowed_market_states",
            f"handler {handler_name!r} does not support a market-state gate "
            "(it uses its own setup state machine); allowed_market_states must be null or omitted, "
            f"got {sorted(allowed_market_states)!r}",
        )

    return StrategyDefinition(
        schema_version=schema_version,
        id=definition_id,
        name=name,
        description=description,
        symbols=tuple(symbols),
        timeframes=tuple(timeframes),
        allowed_market_states=allowed_market_states,
        entry=EntrySpec(
            handler=handler_name,
            params=params,
            reentry=reentry,
            cooldown_bars=cooldown_bars,
        ),
        risk=RiskSpec(stop_loss=stop_loss, take_profit=take_profit),
        exit=ExitSpec(max_holding_bars=max_holding_bars, same_candle_priority=same_candle_priority),
        source_path=str(source_path) if source_path is not None else None,
    )


def _required_str(mapping: Mapping, key: str, fail, *, field_prefix: str = "") -> str:
    field = f"{field_prefix}{key}"
    value = mapping.get(key, _MISSING)
    if value is _MISSING:
        fail(field, "required key is missing; expected a non-empty str")
    if not isinstance(value, str):
        fail(field, f"must be a str, got {value!r} ({_type_name(value)})")
    if not value:
        fail(field, "must be a non-empty str, got ''")
    return value


def _required_str_list(mapping: Mapping, key: str, fail) -> list[str]:
    value = mapping.get(key, _MISSING)
    if value is _MISSING:
        fail(key, "required key is missing; expected a non-empty list of str")
    if not isinstance(value, list):
        fail(key, f"must be a list of str, got {value!r} ({_type_name(value)})")
    if not value:
        fail(key, "must be a non-empty list of str, got []")
    for index, item in enumerate(value):
        if not isinstance(item, str):
            fail(f"{key}[{index}]", f"must be a str, got {item!r} ({_type_name(item)})")
        if not item:
            fail(f"{key}[{index}]", "must be a non-empty str, got ''")
    return list(value)


def _required_mapping(mapping: Mapping, key: str, fail, *, known_keys: tuple) -> Mapping:
    value = mapping.get(key, _MISSING)
    if value is _MISSING:
        fail(key, f"required key is missing; expected a mapping with keys from: {', '.join(known_keys)}")
    if not isinstance(value, Mapping):
        fail(key, f"must be a mapping, got {value!r} ({_type_name(value)})")
    unknown = sorted(set(value) - set(known_keys))
    if unknown:
        fail(
            key,
            f"unknown key(s) under '{key}': {', '.join(map(repr, unknown))}. "
            f"Known keys: {', '.join(known_keys)}",
        )
    return value


def _validate_allowed_market_states(data: Mapping, fail) -> frozenset[str] | None:
    value = data.get("allowed_market_states", _MISSING)
    if value is _MISSING or value is None:
        return None  # null (or omitted) = no filter
    if not isinstance(value, list):
        fail(
            "allowed_market_states",
            f"must be null or a non-empty list of market states, got {value!r} ({_type_name(value)})",
        )
    if not value:
        fail(
            "allowed_market_states",
            "an empty list would allow no entries at all (a dead config); "
            "use null (or omit the key) to mean 'no market-state filter'",
        )
    for index, item in enumerate(value):
        if not isinstance(item, str):
            fail(f"allowed_market_states[{index}]", f"must be a str, got {item!r} ({_type_name(item)})")
        if item not in KNOWN_MARKET_STATES:
            fail(
                f"allowed_market_states[{index}]",
                f"unknown market state {item!r}; valid states: {', '.join(sorted(KNOWN_MARKET_STATES))}",
            )
    return frozenset(value)


def _check_param_scalar(value, field: str, fail) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        fail(field, f"NaN / Infinity are not allowed, got {value!r}")
    if not isinstance(value, _PARAM_SCALAR_TYPES):
        fail(
            field,
            f"entry.params values must be scalars (str/int/float/bool/null) or flat lists of "
            f"scalars, got {value!r} ({_type_name(value)})",
        )


def _validate_entry_params(entry_raw: Mapping, capability: HandlerCapability, fail) -> Mapping:
    value = entry_raw.get("params", _MISSING)
    if value is _MISSING:
        provided: Mapping = {}
    elif isinstance(value, Mapping):
        provided = value
    else:
        fail("entry.params", f"must be a mapping, got {value!r} ({_type_name(value)})")

    # Structural (generic-loader) rules first: str keys, scalar / flat-list
    # values, no NaN/Infinity, no nesting (docs §7).
    for key, item in provided.items():
        if not isinstance(key, str):
            fail("entry.params", f"param keys must be str, got key {key!r} ({_type_name(key)})")
        field = f"entry.params.{key}"
        if isinstance(item, list):
            for index, element in enumerate(item):
                if isinstance(element, (list, Mapping)):
                    fail(
                        f"{field}[{index}]",
                        f"nested lists/mappings are not allowed in entry.params (schema v1), "
                        f"got {element!r} ({_type_name(element)})",
                    )
                _check_param_scalar(element, f"{field}[{index}]", fail)
        elif isinstance(item, Mapping):
            fail(field, f"nested mappings are not allowed in entry.params (schema v1), got {item!r}")
        else:
            _check_param_scalar(item, field, fail)

    # Handler capability rules: only descriptor-supported keys, only allowed
    # values; omitted (or explicit null = "value absent") keys resolve to the
    # handler's default so the normalized config is self-describing.
    supported = capability.params
    resolved: dict = {}
    for key in sorted(provided):
        if key not in supported:
            fail(
                f"entry.params.{key}",
                f"handler {capability.name!r} does not support this param; "
                f"supported params: {', '.join(sorted(supported)) or '(none)'}",
            )
    for key, spec in supported.items():
        item = provided.get(key, _MISSING)
        if item is _MISSING or item is None:
            item = spec.default
        else:
            _check_param_contract(item, spec, capability, key, fail)
        # Deep immutability: list values become tuples; the read-only proxy
        # below prevents key-level mutation. Only to_config() hands out
        # mutable copies, so a validated definition can never drift.
        resolved[key] = tuple(item) if isinstance(item, list) else item
    return MappingProxyType(resolved)


def _check_param_contract(value, spec: HandlerParamSpec, capability: HandlerCapability, key: str, fail) -> None:
    """Enforce one handler param's contract: enumerable values or a numeric range.

    Structural rules (scalar-only, no NaN/Infinity) already ran; this is the
    handler-capability layer. Bool is never accepted where a number is
    required (True == 1 in Python, same strictness as _strict_in).
    """
    field = f"entry.params.{key}"
    if spec.allowed_values is not None:
        if not _strict_in(value, spec.allowed_values):
            fail(
                field,
                f"handler {capability.name!r} only allows "
                f"{', '.join(map(repr, spec.allowed_values))} for this param, "
                f"got {value!r} ({_type_name(value)})",
            )
        return
    if spec.int_min is not None:
        if not _is_strict_int(value) or value < spec.int_min:
            fail(
                field,
                f"handler {capability.name!r} requires an int >= {spec.int_min} "
                f"for this param, got {value!r} ({_type_name(value)})",
            )
        return
    # number_gt / number_max range contract (finite is already guaranteed by
    # the structural NaN/Infinity rejection above).
    if not _is_number(value) or not (spec.number_gt < float(value) <= spec.number_max):
        fail(
            field,
            f"handler {capability.name!r} requires a number in "
            f"({spec.number_gt}, {spec.number_max}] for this param, "
            f"got {value!r} ({_type_name(value)})",
        )


def _validate_reentry(entry_raw: Mapping, capability: HandlerCapability, fail) -> str:
    value = entry_raw.get("reentry", _MISSING)
    if value is _MISSING:
        # Omission is always allowed (docs §4.1). Resolve to the schema default
        # when the handler supports it; a handler that pins a single fixed mode
        # resolves to that mode instead -- the resolved value must always equal
        # the actual execution behaviour (no dead config).
        if DEFAULT_REENTRY in capability.allowed_reentry_modes:
            return DEFAULT_REENTRY
        if len(capability.allowed_reentry_modes) == 1:
            return next(iter(capability.allowed_reentry_modes))
        fail(
            "entry.reentry",
            f"handler {capability.name!r} requires an explicit reentry mode; "
            f"allowed: {', '.join(sorted(capability.allowed_reentry_modes))}",
        )
    if not isinstance(value, str) or value not in KNOWN_REENTRY_MODES:
        fail(
            "entry.reentry",
            f"must be one of {', '.join(KNOWN_REENTRY_MODES)}, got {value!r} ({_type_name(value)})",
        )
    if value not in capability.allowed_reentry_modes:
        fail(
            "entry.reentry",
            f"handler {capability.name!r} only supports "
            f"{', '.join(sorted(capability.allowed_reentry_modes))}, got {value!r}",
        )
    return value


def _validate_cooldown_bars(entry_raw: Mapping, capability: HandlerCapability, fail) -> int:
    value = entry_raw.get("cooldown_bars", _MISSING)
    if value is _MISSING:
        # Omission resolves to the schema default (0) when the handler allows
        # it; a handler that pins a single other value resolves to that value
        # (same fixed-value rule as reentry -- resolved value == behaviour).
        allowed = capability.allowed_cooldown_bars
        if allowed is None or 0 in allowed:
            return 0
        if len(allowed) == 1:
            return next(iter(allowed))
        fail(
            "entry.cooldown_bars",
            f"handler {capability.name!r} requires an explicit cooldown_bars; "
            f"allowed: {sorted(allowed)}",
        )
    if not _is_strict_int(value):
        fail(
            "entry.cooldown_bars",
            f"must be an int >= 0 (bars, not minutes), got {value!r} ({_type_name(value)})",
        )
    if value < 0:
        fail("entry.cooldown_bars", f"must be >= 0, got {value}")
    if capability.allowed_cooldown_bars is not None and value not in capability.allowed_cooldown_bars:
        fail(
            "entry.cooldown_bars",
            f"handler {capability.name!r} only supports cooldown_bars in "
            f"{sorted(capability.allowed_cooldown_bars)}, got {value}",
        )
    return value


def _positive_finite_number(mapping: Mapping, key: str, field: str, fail) -> float:
    value = mapping.get(key, _MISSING)
    if value is _MISSING:
        fail(field, "required key is missing; expected a finite number > 0")
    if not _is_number(value):
        fail(field, f"must be a finite number > 0, got {value!r} ({_type_name(value)})")
    parsed = float(value)
    if not math.isfinite(parsed):
        fail(field, f"must be finite (NaN / Infinity are not allowed), got {value!r}")
    if parsed <= 0:
        fail(field, f"must be > 0, got {value!r}")
    return parsed


def _validate_stop_loss(risk_raw: Mapping, capability: HandlerCapability, fail) -> Mapping:
    stop_raw = _required_mapping(risk_raw, "stop_loss", fail, known_keys=KNOWN_STOP_LOSS_KEYS)
    type_value = stop_raw.get("type", _MISSING)
    if type_value is _MISSING:
        fail(
            "risk.stop_loss.type",
            f"required key is missing; expected one of {', '.join(KNOWN_STOP_LOSS_TYPES)}",
        )
    if not isinstance(type_value, str) or type_value not in KNOWN_STOP_LOSS_TYPES:
        fail(
            "risk.stop_loss.type",
            f"must be one of {', '.join(KNOWN_STOP_LOSS_TYPES)}, got {type_value!r} ({_type_name(type_value)})",
        )
    stop_loss: dict = {"type": type_value}
    if type_value == STOP_LOSS_FIXED_PCT:
        stop_loss["pct"] = _positive_finite_number(stop_raw, "pct", "risk.stop_loss.pct", fail)
    elif "pct" in stop_raw:
        fail(
            "risk.stop_loss.pct",
            f"'pct' is only valid when type={STOP_LOSS_FIXED_PCT!r} "
            f"(it would be ignored for type={type_value!r})",
        )
    if type_value not in capability.allowed_stop_loss_types:
        fail(
            "risk.stop_loss.type",
            f"handler {capability.name!r} only supports "
            f"{', '.join(sorted(capability.allowed_stop_loss_types))}, got {type_value!r}",
        )
    return MappingProxyType(stop_loss)


def _validate_take_profit(risk_raw: Mapping, capability: HandlerCapability, fail) -> Mapping:
    tp_raw = _required_mapping(risk_raw, "take_profit", fail, known_keys=KNOWN_TAKE_PROFIT_KEYS)
    type_value = tp_raw.get("type", _MISSING)
    if type_value is _MISSING:
        fail(
            "risk.take_profit.type",
            f"required key is missing; expected one of {', '.join(KNOWN_TAKE_PROFIT_TYPES)}",
        )
    if not isinstance(type_value, str) or type_value not in KNOWN_TAKE_PROFIT_TYPES:
        fail(
            "risk.take_profit.type",
            f"must be one of {', '.join(KNOWN_TAKE_PROFIT_TYPES)}, got {type_value!r} ({_type_name(type_value)})",
        )
    take_profit: dict = {"type": type_value}
    if type_value == TAKE_PROFIT_R_MULTIPLE:
        take_profit["r"] = _positive_finite_number(tp_raw, "r", "risk.take_profit.r", fail)
    if type_value not in capability.allowed_take_profit_types:
        fail(
            "risk.take_profit.type",
            f"handler {capability.name!r} only supports "
            f"{', '.join(sorted(capability.allowed_take_profit_types))}, got {type_value!r}",
        )
    return MappingProxyType(take_profit)


def _validate_max_holding_bars(exit_raw: Mapping, fail) -> int:
    value = exit_raw.get("max_holding_bars", _MISSING)
    if value is _MISSING:
        fail("exit.max_holding_bars", "required key is missing; expected an int > 0")
    if not _is_strict_int(value):
        fail(
            "exit.max_holding_bars",
            f"must be an int > 0, got {value!r} ({_type_name(value)})",
        )
    if value <= 0:
        fail("exit.max_holding_bars", f"must be > 0, got {value}")
    return value


def _validate_same_candle_priority(exit_raw: Mapping, capability: HandlerCapability, fail) -> str:
    value = exit_raw.get("same_candle_priority", _MISSING)
    if value is _MISSING:
        # Same omission rule as reentry: schema default when allowed, else the
        # handler's single fixed value.
        allowed = capability.allowed_same_candle_priorities
        if DEFAULT_SAME_CANDLE_PRIORITY in allowed:
            return DEFAULT_SAME_CANDLE_PRIORITY
        if len(allowed) == 1:
            return next(iter(allowed))
        fail(
            "exit.same_candle_priority",
            f"handler {capability.name!r} requires an explicit same_candle_priority; "
            f"allowed: {', '.join(sorted(allowed))}",
        )
    if not isinstance(value, str) or value not in KNOWN_SAME_CANDLE_PRIORITIES:
        fail(
            "exit.same_candle_priority",
            f"must be one of {', '.join(KNOWN_SAME_CANDLE_PRIORITIES)}, "
            f"got {value!r} ({_type_name(value)})",
        )
    if value not in capability.allowed_same_candle_priorities:
        fail(
            "exit.same_candle_priority",
            f"handler {capability.name!r} only supports "
            f"{', '.join(sorted(capability.allowed_same_candle_priorities))}, got {value!r}",
        )
    return value


# --- YAML file loading ----------------------------------------------------------


def load_strategy_definition(path) -> StrategyDefinition:
    """Load + validate one Strategy definition YAML file (1 file = 1 Strategy)."""
    source_path = Path(path)
    try:
        text = source_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise StrategyDefinitionError(
            "strategy definition file does not exist", source_path=source_path
        ) from None
    except IsADirectoryError:
        raise StrategyDefinitionError(
            "strategy definition path is a directory, expected a YAML file", source_path=source_path
        ) from None
    except OSError as exc:
        raise StrategyDefinitionError(
            f"strategy definition file could not be read: {exc}", source_path=source_path
        ) from exc

    try:
        # safe_load only (docs §1): no arbitrary object construction via tags,
        # and a multi-document stream raises ComposerError.
        raw = yaml.safe_load(text)
    except yaml.composer.ComposerError as exc:
        raise StrategyDefinitionError(
            f"multiple YAML documents are not allowed (1 Strategy = 1 file): {exc}",
            source_path=source_path,
        ) from exc
    except yaml.YAMLError as exc:
        raise StrategyDefinitionError(f"invalid YAML: {exc}", source_path=source_path) from exc

    if raw is None:
        raise StrategyDefinitionError(
            "file is empty; expected a YAML mapping with the strategy definition",
            source_path=source_path,
        )
    if not isinstance(raw, dict):
        raise StrategyDefinitionError(
            f"top-level must be a mapping (key: value pairs), got {_type_name(raw)}",
            source_path=source_path,
        )
    return build_strategy_definition(raw, source_path=source_path)


def load_strategy_registry(directory=None) -> dict[str, StrategyDefinition]:
    """Load every ``*.yaml`` / ``*.yml`` in *directory* into an id-keyed registry.

    All-or-nothing (docs §5.1): the first invalid file or duplicate id fails the
    whole load -- a partial registry is never returned, and neither first-wins
    nor last-wins ever applies. Files are scanned non-recursively in sorted
    path order, so load order and error order are deterministic.
    """
    resolved_dir = Path(directory) if directory is not None else strategy_definitions_dir()
    if not resolved_dir.exists():
        raise StrategyDefinitionError(
            f"strategy definitions directory does not exist: {resolved_dir} "
            f"(create it, set {STRATEGY_DIR_ENV_VAR}, or use --strategy-file)"
        )
    if not resolved_dir.is_dir():
        raise StrategyDefinitionError(
            f"strategy definitions path is not a directory: {resolved_dir}"
        )

    paths = sorted(
        entry
        for entry in resolved_dir.iterdir()
        if entry.is_file() and entry.suffix.lower() in STRATEGY_FILE_SUFFIXES
    )

    registry: dict[str, StrategyDefinition] = {}
    for path in paths:
        definition = load_strategy_definition(path)
        existing = registry.get(definition.id)
        if existing is not None:
            raise StrategyDefinitionError(
                f"duplicate strategy id {definition.id!r}: defined in both "
                f"{existing.source_path} and {path}",
                source_path=path,
                strategy_id=definition.id,
                field_path="id",
            )
        registry[definition.id] = definition
    return registry
