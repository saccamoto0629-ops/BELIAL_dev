"""Fixed-scenario definitions for the Execution scenario tests (Issue #77).

*** DATA + VALIDATION ONLY. Nothing here submits, persists or decides
anything: scenario files describe INPUTS (request, market, policy, FakeBroker
behaviour, fault injection, steps) and EXPECTED results. All execution logic
stays in the production modules (``execution.live_submission`` /
``execution.execution_store`` / ``execution.safety_gate``) called by
``execution.scenario_runner``. ***

Scenario files live in ``execution/scenarios/`` as one JSON object per file
(JSON via the stdlib keeps the execution package's stdlib+broker-only
dependency rule; ``execution/tests/test_package_imports.py`` pins it).
Loading is ALL-OR-NOTHING, mirroring ``backtest/strategies/``: any invalid
file -- unknown keys, missing required fields, wrong types, unknown broker
behaviours / order states / rejection codes, duplicate scenario ids -- fails
the whole load with :class:`ScenarioDefinitionError`. Invalid scenarios are
never silently skipped.

Values that vary per run (timestamps, DB row ids) are deliberately NOT part
of scenario files: the runner injects a fixed clock and FakeBroker is
deterministic, and expectations reference stable ids only (scenario files pin
``broker_order_id`` via the FakeBroker script where they assert it).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from broker.broker_models import OrderSide, OrderState, OrderType
from execution.execution_store import KNOWN_PHASES
from execution.safety_gate import SafetyRejectionCode

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SCENARIO_DIR = BASE_DIR / "scenarios"

_SCENARIO_ID_PATTERN = re.compile(r"^[a-z0-9_]+$")

_ORDER_STATE_VALUES = frozenset(state.value for state in OrderState)
_REJECTION_CODE_VALUES = frozenset(code.value for code in SafetyRejectionCode)
_SIDE_VALUES = frozenset(side.value for side in OrderSide)
_ORDER_TYPE_VALUES = frozenset(order_type.value for order_type in OrderType)

# Submit-script behaviours the runner can configure on FakeBroker, with the
# extra keys each behaviour accepts. Kept in lockstep with
# ``broker.fake_broker``; an unknown behaviour is a schema error, never a
# silent default. Cancel behaviours are deliberately NOT part of the scenario
# schema (PR #96 review r1): no production cancel Execution use case exists
# yet, so fixed scenarios cannot exercise one -- FakeBroker cancel behaviour
# and the store's cancel transitions are covered by ordinary pytest instead.
_SUBMIT_BEHAVIOUR_KEYS: dict[str, frozenset[str]] = {
    "fill_immediately": frozenset({"price", "broker_order_id"}),
    "stay_pending": frozenset({"broker_order_id"}),
    "partially_fill": frozenset({"fills", "broker_order_id"}),
    "reject": frozenset({"reason"}),
    "timeout": frozenset(),
    "fail_communication": frozenset(),
}

# DB boundaries where a fault can be injected (Issue #77): each maps to one
# store function the runner substitutes for the scenario's duration. Issue #78
# adds the shadow-store save and the safety-decision record boundaries (the
# runner resolves each name onto the owning module/class itself).
FAULT_BOUNDARIES: dict[str, str] = {
    "initial_save": "create_execution_order",
    "mark_submitting": "mark_submitting",
    "record_order_result": "record_order_result",
    "record_order_state": "record_order_state",
    "state_history": "_append_history",
    "shadow_result_save": "record_shadow_result",
    "safety_decision_record": "record",
}

_STEP_ACTIONS = frozenset(
    {
        "register",
        "submit",
        "record_unknown",
        "mark_submitting",
        "submit_concurrent",
        "register_concurrent",
        "shadow",
        "shadow_concurrent",
    }
)

# Expected outcomes per step action (closed world).
_STEP_OUTCOMES: dict[str, frozenset[str]] = {
    "register": frozenset({"registered", "conflict", "error"}),
    "submit": frozenset({"submitted", "rejected", "error"}),
    "record_unknown": frozenset({"recorded", "error"}),
    "mark_submitting": frozenset({"ok", "error"}),
    "shadow": frozenset({"recorded", "duplicate", "dry_run", "error"}),
}

_REQUEST_REQUIRED_KEYS = frozenset(
    {"execution_request_id", "signal_id", "strategy_id", "symbol", "side", "order_type", "quantity"}
)
_REQUEST_OPTIONAL_KEYS = frozenset({"requested_price", "stop_loss_price", "take_profit_price"})

_MARKET_KEYS = frozenset(
    {"symbol", "reference_price", "bid", "ask", "age_seconds", "market_open", "tradable"}
)

_GATE_KEYS = frozenset(
    {
        "kill_switch",
        "allowed_symbols",
        "allowed_sides",
        "max_order_quantity",
        "max_positions_per_symbol",
        "max_positions_account",
        "max_reference_age_seconds",
        "max_spread_by_symbol",
        "allowed_fake_targets",
        "allowed_shadow_targets",
        "require_stop_loss",
    }
)

_BROKER_KEYS = frozenset({"default_fill_price", "connection_target", "scripts"})

_FAULT_KEYS = frozenset({"boundary", "after_calls"})

_SCENARIO_KEYS = frozenset(
    {
        "scenario_id",
        "description",
        "primary_execution_request_id",
        "gate",
        "market",
        "broker",
        "faults",
        "steps",
        "expected",
    }
)

_STEP_COMMON_KEYS = frozenset({"action", "expect"})
_STEP_EXTRA_KEYS: dict[str, frozenset[str]] = {
    "register": frozenset({"request", "client_order_id"}),
    "submit": frozenset({"execution_request_id"}),
    "record_unknown": frozenset({"execution_request_id", "reason"}),
    "mark_submitting": frozenset({"execution_request_id", "reason"}),
    "submit_concurrent": frozenset({"execution_request_id", "workers"}),
    "register_concurrent": frozenset({"registrations"}),
    "shadow": frozenset({"request", "client_order_id", "timeframe", "dry_run"}),
    "shadow_concurrent": frozenset({"request", "client_order_id", "timeframe", "workers"}),
}

# History-entry expectation keys (PR #96 review Minor 1): entries are objects
# so reason / broker reference can be declared; ``changed_at_utc`` presence
# and timestamp monotonicity are always verified by the runner and are not
# part of the declared keys (dynamic values are never string-compared).
_HISTORY_ENTRY_REQUIRED_KEYS = frozenset({"previous_state", "new_state"})
_HISTORY_ENTRY_KEYS = _HISTORY_ENTRY_REQUIRED_KEYS | frozenset({"reason", "broker_order_id"})

# Scenario-level expected keys (closed world). ``decimal`` entries are
# compared as Decimal so "150.0" == "150.00"; DB-assigned values such as
# history_id are deliberately not comparable. Issue #78 adds the shadow-store
# observations: row count, file existence (dry-run must create nothing), the
# stored gate outcome and the stored intended-order content.
EXPECTED_KEYS = frozenset(
    {
        "final_phase",
        "final_state",
        "state_history",
        "broker_submit_calls",
        "submitted_request",
        "order_rows",
        "history_rows",
        "safety_decisions",
        "last_decision_allowed",
        "last_decision_codes",
        "broker_order_id",
        "filled_quantity",
        "average_fill_price",
        "fill_prices",
        "requested_price",
        "reject_reason",
        "listed_unknown",
        "listed_unresolved",
        "shadow_rows",
        "shadow_db_created",
        "decision_db_created",
        "shadow_allowed",
        "shadow_codes",
        "shadow_intended_order",
    }
)

_SUBMITTED_REQUEST_KEYS = frozenset(
    {"client_order_id", "symbol", "side", "order_type", "quantity", "limit_price"}
)

_STEP_EXPECT_KEYS = frozenset({"outcome", "state", "rejection_codes", "error_type", "error_code"})
_SHADOW_STEP_EXPECT_KEYS = frozenset({"outcome", "allowed", "rejection_codes", "error_type"})
_CONCURRENT_SUBMIT_EXPECT_KEYS = frozenset({"submitted", "rejected", "errors", "rejection_codes"})
_CONCURRENT_REGISTER_EXPECT_KEYS = frozenset({"registered", "conflicts", "errors"})
_CONCURRENT_SHADOW_EXPECT_KEYS = frozenset({"recorded", "duplicates", "errors", "rejection_codes"})


class ScenarioDefinitionError(ValueError):
    """A scenario file violates the schema (missing/unknown/invalid fields,
    duplicate ids, unknown behaviours/states/codes). The whole load fails."""


@dataclass(frozen=True)
class ScenarioStep:
    """One declarative step; ``params`` holds the validated action-specific
    keys and ``expect`` the validated expected step outcome."""

    action: str
    params: dict
    expect: dict


@dataclass(frozen=True)
class ScenarioDefinition:
    scenario_id: str
    description: str
    primary_execution_request_id: str | None
    gate: dict
    market: dict
    broker: dict
    faults: tuple[dict, ...]
    steps: tuple[ScenarioStep, ...]
    expected: dict
    source_path: Path | None = field(default=None, compare=False)


def _fail(scenario: str, message: str) -> ScenarioDefinitionError:
    return ScenarioDefinitionError(f"scenario {scenario!r}: {message}")


def _require_mapping(value, name: str, scenario: str) -> dict:
    if not isinstance(value, dict):
        raise _fail(scenario, f"{name} must be an object, got {type(value).__name__}")
    return value


def _require_known_keys(mapping: dict, allowed: frozenset[str], name: str, scenario: str) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        raise _fail(scenario, f"{name} has unknown keys {sorted(unknown)}; allowed: {sorted(allowed)}")


def _require_bool(value, name: str, scenario: str) -> None:
    if not isinstance(value, bool):
        raise _fail(scenario, f"{name} must be a boolean, got {value!r}")


def _require_int(value, name: str, scenario: str, *, minimum: int = 0) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise _fail(scenario, f"{name} must be an integer >= {minimum}, got {value!r}")


def _require_non_empty_str(value, name: str, scenario: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise _fail(scenario, f"{name} must be a non-empty string, got {value!r}")


def _require_decimal_str(value, name: str, scenario: str, *, allow_zero: bool = False) -> None:
    """Prices/quantities are JSON strings (never floats) so scenario data
    round-trips exactly into Decimal. ``allow_zero`` is for EXPECTED result
    fields (e.g. ``filled_quantity`` of a rejected order); inputs stay > 0."""
    if not isinstance(value, str):
        raise _fail(scenario, f"{name} must be a decimal string, got {value!r}")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise _fail(scenario, f"{name} is not a valid decimal string: {value!r}") from None
    if not parsed.is_finite() or parsed < 0 or (parsed == 0 and not allow_zero):
        raise _fail(scenario, f"{name} must be {'>= 0' if allow_zero else '> 0'} and finite, got {value!r}")


def _require_optional_decimal_str(value, name: str, scenario: str, *, allow_zero: bool = False) -> None:
    if value is not None:
        _require_decimal_str(value, name, scenario, allow_zero=allow_zero)


def _validate_request(spec, name: str, scenario: str) -> dict:
    request = _require_mapping(spec, name, scenario)
    _require_known_keys(request, _REQUEST_REQUIRED_KEYS | _REQUEST_OPTIONAL_KEYS, name, scenario)
    missing = _REQUEST_REQUIRED_KEYS - set(request)
    if missing:
        raise _fail(scenario, f"{name} is missing required keys {sorted(missing)}")
    for key in ("execution_request_id", "signal_id", "strategy_id", "symbol"):
        _require_non_empty_str(request[key], f"{name}.{key}", scenario)
    if request["side"] not in _SIDE_VALUES:
        raise _fail(scenario, f"{name}.side must be one of {sorted(_SIDE_VALUES)}, got {request['side']!r}")
    if request["order_type"] not in _ORDER_TYPE_VALUES:
        raise _fail(
            scenario,
            f"{name}.order_type must be one of {sorted(_ORDER_TYPE_VALUES)}, got {request['order_type']!r}",
        )
    _require_decimal_str(request["quantity"], f"{name}.quantity", scenario)
    for key in _REQUEST_OPTIONAL_KEYS:
        _require_optional_decimal_str(request.get(key), f"{name}.{key}", scenario)
    return request


def _validate_script(spec, name: str, scenario: str, *, table: dict[str, frozenset[str]]) -> dict:
    script = _require_mapping(spec, name, scenario)
    behaviour = script.get("behavior")
    if behaviour not in table:
        raise _fail(
            scenario,
            f"{name}.behavior must be one of {sorted(table)}, got {behaviour!r}",
        )
    _require_known_keys(script, frozenset({"behavior"}) | table[behaviour], name, scenario)
    if "price" in script:
        _require_decimal_str(script["price"], f"{name}.price", scenario)
    if "broker_order_id" in script:
        _require_non_empty_str(script["broker_order_id"], f"{name}.broker_order_id", scenario)
    if "reason" in script:
        _require_non_empty_str(script["reason"], f"{name}.reason", scenario)
    if behaviour == "partially_fill":
        fills = script.get("fills")
        if not isinstance(fills, list) or not fills:
            raise _fail(scenario, f"{name}.fills must be a non-empty list of [price, quantity] pairs")
        for index, entry in enumerate(fills):
            if not isinstance(entry, list) or len(entry) != 2:
                raise _fail(scenario, f"{name}.fills[{index}] must be a [price, quantity] pair")
            _require_decimal_str(entry[0], f"{name}.fills[{index}] price", scenario)
            _require_decimal_str(entry[1], f"{name}.fills[{index}] quantity", scenario)
    return script


def _validate_broker(spec, scenario: str) -> dict:
    broker = _require_mapping(spec, "broker", scenario)
    _require_known_keys(broker, _BROKER_KEYS, "broker", scenario)
    _require_optional_decimal_str(broker.get("default_fill_price"), "broker.default_fill_price", scenario)
    if "connection_target" in broker:
        _require_non_empty_str(broker["connection_target"], "broker.connection_target", scenario)
    scripts = _require_mapping(broker.get("scripts", {}), "broker.scripts", scenario)
    for client_order_id, script in scripts.items():
        _require_non_empty_str(client_order_id, "broker.scripts key", scenario)
        _validate_script(
            script, f"broker.scripts[{client_order_id}]", scenario, table=_SUBMIT_BEHAVIOUR_KEYS
        )
    return broker


def _validate_market(spec, scenario: str) -> dict:
    market = _require_mapping(spec, "market", scenario)
    _require_known_keys(market, _MARKET_KEYS, "market", scenario)
    if "symbol" in market:
        _require_non_empty_str(market["symbol"], "market.symbol", scenario)
    for key in ("reference_price", "bid", "ask"):
        if key in market:
            _require_optional_decimal_str(market[key], f"market.{key}", scenario)
    if "age_seconds" in market:
        _require_int(market["age_seconds"], "market.age_seconds", scenario)
    for key in ("market_open", "tradable"):
        if key in market:
            _require_bool(market[key], f"market.{key}", scenario)
    return market


def _validate_gate(spec, scenario: str) -> dict:
    gate = _require_mapping(spec, "gate", scenario)
    _require_known_keys(gate, _GATE_KEYS, "gate", scenario)
    if "kill_switch" in gate:
        _require_bool(gate["kill_switch"], "gate.kill_switch", scenario)
    if "require_stop_loss" in gate:
        _require_bool(gate["require_stop_loss"], "gate.require_stop_loss", scenario)
    for key in ("allowed_symbols", "allowed_fake_targets", "allowed_shadow_targets"):
        if key in gate:
            values = gate[key]
            if not isinstance(values, list) or not values:
                raise _fail(scenario, f"gate.{key} must be a non-empty list of strings")
            for entry in values:
                _require_non_empty_str(entry, f"gate.{key} entry", scenario)
    if "allowed_sides" in gate:
        sides = gate["allowed_sides"]
        if not isinstance(sides, list) or not sides or not set(sides) <= _SIDE_VALUES:
            raise _fail(scenario, f"gate.allowed_sides must be a non-empty subset of {sorted(_SIDE_VALUES)}")
    _require_optional_decimal_str(gate.get("max_order_quantity"), "gate.max_order_quantity", scenario)
    for key in ("max_positions_per_symbol", "max_positions_account", "max_reference_age_seconds"):
        if key in gate:
            _require_int(gate[key], f"gate.{key}", scenario, minimum=0 if "positions" in key else 1)
    if "max_spread_by_symbol" in gate:
        spreads = _require_mapping(gate["max_spread_by_symbol"], "gate.max_spread_by_symbol", scenario)
        for symbol, spread in spreads.items():
            _require_non_empty_str(symbol, "gate.max_spread_by_symbol key", scenario)
            _require_decimal_str(spread, f"gate.max_spread_by_symbol[{symbol}]", scenario)
    return gate


def _validate_faults(spec, scenario: str) -> tuple[dict, ...]:
    if not isinstance(spec, list):
        raise _fail(scenario, f"faults must be a list, got {type(spec).__name__}")
    faults = []
    for index, entry in enumerate(spec):
        fault = _require_mapping(entry, f"faults[{index}]", scenario)
        _require_known_keys(fault, _FAULT_KEYS, f"faults[{index}]", scenario)
        boundary = fault.get("boundary")
        if boundary not in FAULT_BOUNDARIES:
            raise _fail(
                scenario,
                f"faults[{index}].boundary must be one of {sorted(FAULT_BOUNDARIES)}, got {boundary!r}",
            )
        if "after_calls" in fault:
            _require_int(fault["after_calls"], f"faults[{index}].after_calls", scenario)
        faults.append(fault)
    return tuple(faults)


def _validate_rejection_codes(codes, name: str, scenario: str) -> None:
    if not isinstance(codes, list) or not codes:
        raise _fail(scenario, f"{name} must be a non-empty list of rejection codes")
    unknown = set(codes) - _REJECTION_CODE_VALUES
    if unknown:
        raise _fail(scenario, f"{name} contains unknown rejection codes {sorted(unknown)}")


def _validate_step_expect(action: str, expect, name: str, scenario: str) -> dict:
    expect = _require_mapping(expect, name, scenario)
    if action == "shadow":
        _require_known_keys(expect, _SHADOW_STEP_EXPECT_KEYS, name, scenario)
        outcome = expect.get("outcome")
        if outcome not in _STEP_OUTCOMES["shadow"]:
            raise _fail(
                scenario,
                f"{name}.outcome must be one of {sorted(_STEP_OUTCOMES['shadow'])}, got {outcome!r}",
            )
        if "allowed" in expect:
            _require_bool(expect["allowed"], f"{name}.allowed", scenario)
        if "rejection_codes" in expect:
            _validate_rejection_codes(expect["rejection_codes"], f"{name}.rejection_codes", scenario)
        if "error_type" in expect:
            _require_non_empty_str(expect["error_type"], f"{name}.error_type", scenario)
        return expect
    if action == "shadow_concurrent":
        _require_known_keys(expect, _CONCURRENT_SHADOW_EXPECT_KEYS, name, scenario)
        for key in ("recorded", "duplicates", "errors"):
            if key in expect:
                _require_int(expect[key], f"{name}.{key}", scenario)
        if "rejection_codes" in expect:
            _validate_rejection_codes(expect["rejection_codes"], f"{name}.rejection_codes", scenario)
        return expect
    if action == "submit_concurrent":
        _require_known_keys(expect, _CONCURRENT_SUBMIT_EXPECT_KEYS, name, scenario)
        for key in ("submitted", "rejected", "errors"):
            if key in expect:
                _require_int(expect[key], f"{name}.{key}", scenario)
        if "rejection_codes" in expect:
            _validate_rejection_codes(expect["rejection_codes"], f"{name}.rejection_codes", scenario)
        return expect
    if action == "register_concurrent":
        _require_known_keys(expect, _CONCURRENT_REGISTER_EXPECT_KEYS, name, scenario)
        for key in ("registered", "conflicts", "errors"):
            if key in expect:
                _require_int(expect[key], f"{name}.{key}", scenario)
        return expect
    _require_known_keys(expect, _STEP_EXPECT_KEYS, name, scenario)
    outcome = expect.get("outcome")
    if outcome not in _STEP_OUTCOMES[action]:
        raise _fail(
            scenario,
            f"{name}.outcome must be one of {sorted(_STEP_OUTCOMES[action])}, got {outcome!r}",
        )
    if "state" in expect and expect["state"] not in _ORDER_STATE_VALUES:
        raise _fail(scenario, f"{name}.state must be one of {sorted(_ORDER_STATE_VALUES)}")
    if "rejection_codes" in expect:
        _validate_rejection_codes(expect["rejection_codes"], f"{name}.rejection_codes", scenario)
    if "error_type" in expect:
        _require_non_empty_str(expect["error_type"], f"{name}.error_type", scenario)
    if "error_code" in expect:
        _require_non_empty_str(expect["error_code"], f"{name}.error_code", scenario)
    return expect


def _validate_step(spec, index: int, scenario: str) -> ScenarioStep:
    name = f"steps[{index}]"
    step = _require_mapping(spec, name, scenario)
    action = step.get("action")
    if action not in _STEP_ACTIONS:
        raise _fail(scenario, f"{name}.action must be one of {sorted(_STEP_ACTIONS)}, got {action!r}")
    _require_known_keys(step, _STEP_COMMON_KEYS | _STEP_EXTRA_KEYS[action], name, scenario)
    if "expect" not in step:
        raise _fail(scenario, f"{name} must declare an expect block")

    params = {key: value for key, value in step.items() if key not in _STEP_COMMON_KEYS}
    if action == "register":
        params["request"] = _validate_request(step.get("request"), f"{name}.request", scenario)
        _require_non_empty_str(step.get("client_order_id"), f"{name}.client_order_id", scenario)
    elif action in ("shadow", "shadow_concurrent"):
        params["request"] = _validate_request(step.get("request"), f"{name}.request", scenario)
        _require_non_empty_str(step.get("client_order_id"), f"{name}.client_order_id", scenario)
        if "timeframe" in step:
            _require_non_empty_str(step["timeframe"], f"{name}.timeframe", scenario)
        if action == "shadow" and "dry_run" in step:
            _require_bool(step["dry_run"], f"{name}.dry_run", scenario)
        if action == "shadow_concurrent":
            _require_int(step.get("workers"), f"{name}.workers", scenario, minimum=2)
    elif action in ("submit", "record_unknown", "mark_submitting", "submit_concurrent"):
        _require_non_empty_str(step.get("execution_request_id"), f"{name}.execution_request_id", scenario)
        if action == "submit_concurrent":
            _require_int(step.get("workers"), f"{name}.workers", scenario, minimum=2)
        if action in ("record_unknown", "mark_submitting") and "reason" in step:
            _require_non_empty_str(step["reason"], f"{name}.reason", scenario)
    elif action == "register_concurrent":
        registrations = step.get("registrations")
        if not isinstance(registrations, list) or len(registrations) < 2:
            raise _fail(scenario, f"{name}.registrations must be a list of >= 2 entries")
        for reg_index, entry in enumerate(registrations):
            entry = _require_mapping(entry, f"{name}.registrations[{reg_index}]", scenario)
            _require_known_keys(
                entry,
                frozenset({"request", "client_order_id"}),
                f"{name}.registrations[{reg_index}]",
                scenario,
            )
            _validate_request(
                entry.get("request"), f"{name}.registrations[{reg_index}].request", scenario
            )
            _require_non_empty_str(
                entry.get("client_order_id"),
                f"{name}.registrations[{reg_index}].client_order_id",
                scenario,
            )

    expect = _validate_step_expect(action, step.get("expect"), f"{name}.expect", scenario)
    return ScenarioStep(action=action, params=params, expect=expect)


def _validate_expected(spec, scenario: str) -> dict:
    expected = _require_mapping(spec, "expected", scenario)
    _require_known_keys(expected, EXPECTED_KEYS, "expected", scenario)
    if not expected:
        raise _fail(scenario, "expected must declare at least one comparison key")
    if "final_phase" in expected and expected["final_phase"] is not None:
        if expected["final_phase"] not in KNOWN_PHASES:
            raise _fail(scenario, f"expected.final_phase must be one of {sorted(KNOWN_PHASES)} or null")
    if "final_state" in expected and expected["final_state"] is not None:
        if expected["final_state"] not in _ORDER_STATE_VALUES:
            raise _fail(scenario, f"expected.final_state must be one of {sorted(_ORDER_STATE_VALUES)} or null")
    if "state_history" in expected:
        history = expected["state_history"]
        if not isinstance(history, list):
            raise _fail(scenario, "expected.state_history must be a list of history-entry objects")
        for index, entry in enumerate(history):
            name = f"expected.state_history[{index}]"
            entry = _require_mapping(entry, name, scenario)
            _require_known_keys(entry, _HISTORY_ENTRY_KEYS, name, scenario)
            missing = _HISTORY_ENTRY_REQUIRED_KEYS - set(entry)
            if missing:
                raise _fail(scenario, f"{name} is missing required keys {sorted(missing)}")
            if entry["previous_state"] is not None and entry["previous_state"] not in _ORDER_STATE_VALUES:
                raise _fail(scenario, f"{name} has unknown previous_state {entry['previous_state']!r}")
            if entry["new_state"] not in _ORDER_STATE_VALUES:
                raise _fail(scenario, f"{name} has unknown new_state {entry['new_state']!r}")
            # reason / broker_order_id: ABSENT means "not compared"; an
            # explicit null means "must be NULL in the DB" -- the two are
            # deliberately different declarations.
            if "reason" in entry and entry["reason"] is not None:
                _require_non_empty_str(entry["reason"], f"{name}.reason", scenario)
            if "broker_order_id" in entry and entry["broker_order_id"] is not None:
                _require_non_empty_str(entry["broker_order_id"], f"{name}.broker_order_id", scenario)
    for key in ("broker_submit_calls", "order_rows", "history_rows", "safety_decisions", "shadow_rows"):
        if key in expected:
            _require_int(expected[key], f"expected.{key}", scenario)
    for key in ("shadow_db_created", "decision_db_created"):
        if key in expected:
            _require_bool(expected[key], f"expected.{key}", scenario)
    if "shadow_allowed" in expected and expected["shadow_allowed"] is not None:
        _require_bool(expected["shadow_allowed"], "expected.shadow_allowed", scenario)
    if "shadow_codes" in expected:
        _validate_rejection_codes(expected["shadow_codes"], "expected.shadow_codes", scenario)
    if "shadow_intended_order" in expected:
        intended = _require_mapping(
            expected["shadow_intended_order"], "expected.shadow_intended_order", scenario
        )
        _require_known_keys(
            intended, _SUBMITTED_REQUEST_KEYS, "expected.shadow_intended_order", scenario
        )
        if not intended:
            raise _fail(scenario, "expected.shadow_intended_order must declare at least one field")
        if "side" in intended and intended["side"] not in _SIDE_VALUES:
            raise _fail(
                scenario, f"expected.shadow_intended_order.side must be one of {sorted(_SIDE_VALUES)}"
            )
        if "order_type" in intended and intended["order_type"] not in _ORDER_TYPE_VALUES:
            raise _fail(
                scenario,
                f"expected.shadow_intended_order.order_type must be one of {sorted(_ORDER_TYPE_VALUES)}",
            )
        for key in ("quantity", "limit_price"):
            if key in intended:
                _require_optional_decimal_str(
                    intended[key], f"expected.shadow_intended_order.{key}", scenario
                )
    if "submitted_request" in expected:
        submitted = _require_mapping(expected["submitted_request"], "expected.submitted_request", scenario)
        _require_known_keys(submitted, _SUBMITTED_REQUEST_KEYS, "expected.submitted_request", scenario)
        if not submitted:
            raise _fail(scenario, "expected.submitted_request must declare at least one field")
        if "side" in submitted and submitted["side"] not in _SIDE_VALUES:
            raise _fail(scenario, f"expected.submitted_request.side must be one of {sorted(_SIDE_VALUES)}")
        if "order_type" in submitted and submitted["order_type"] not in _ORDER_TYPE_VALUES:
            raise _fail(
                scenario, f"expected.submitted_request.order_type must be one of {sorted(_ORDER_TYPE_VALUES)}"
            )
        for key in ("quantity", "limit_price"):
            if key in submitted:
                _require_optional_decimal_str(submitted[key], f"expected.submitted_request.{key}", scenario)
    for key in ("last_decision_allowed", "listed_unknown", "listed_unresolved"):
        if key in expected:
            _require_bool(expected[key], f"expected.{key}", scenario)
    if "last_decision_codes" in expected:
        _validate_rejection_codes(expected["last_decision_codes"], "expected.last_decision_codes", scenario)
    for key in ("filled_quantity", "average_fill_price", "requested_price"):
        if key in expected:
            _require_optional_decimal_str(
                expected[key], f"expected.{key}", scenario, allow_zero=(key == "filled_quantity")
            )
    if "fill_prices" in expected:
        prices = expected["fill_prices"]
        if not isinstance(prices, list):
            raise _fail(scenario, "expected.fill_prices must be a list of decimal strings")
        for index, price in enumerate(prices):
            _require_decimal_str(price, f"expected.fill_prices[{index}]", scenario)
    if "broker_order_id" in expected and expected["broker_order_id"] is not None:
        _require_non_empty_str(expected["broker_order_id"], "expected.broker_order_id", scenario)
    if "reject_reason" in expected and expected["reject_reason"] is not None:
        _require_non_empty_str(expected["reject_reason"], "expected.reject_reason", scenario)
    return expected


def validate_scenario(raw, *, source_path: Path | None = None) -> ScenarioDefinition:
    """Validate one raw scenario mapping into a :class:`ScenarioDefinition`.

    Closed-world: unknown keys anywhere are schema errors, so a typo in a
    scenario file fails loudly instead of silently not comparing anything.
    """
    label = "<unnamed>" if not isinstance(raw, dict) else str(raw.get("scenario_id", "<unnamed>"))
    scenario = _require_mapping(raw, "scenario", label)
    _require_known_keys(scenario, _SCENARIO_KEYS, "scenario", label)

    scenario_id = scenario.get("scenario_id")
    _require_non_empty_str(scenario_id, "scenario_id", label)
    if not _SCENARIO_ID_PATTERN.match(scenario_id):
        raise _fail(label, f"scenario_id must match {_SCENARIO_ID_PATTERN.pattern}, got {scenario_id!r}")
    _require_non_empty_str(scenario.get("description"), "description", scenario_id)

    if "primary_execution_request_id" in scenario:
        _require_non_empty_str(
            scenario["primary_execution_request_id"], "primary_execution_request_id", scenario_id
        )

    steps_spec = scenario.get("steps")
    if not isinstance(steps_spec, list) or not steps_spec:
        raise _fail(scenario_id, "steps must be a non-empty list")
    steps = tuple(_validate_step(step, index, scenario_id) for index, step in enumerate(steps_spec))

    if "expected" not in scenario:
        raise _fail(scenario_id, "expected block is required")

    definition = ScenarioDefinition(
        scenario_id=scenario_id,
        description=scenario["description"],
        primary_execution_request_id=scenario.get("primary_execution_request_id"),
        gate=_validate_gate(scenario.get("gate", {}), scenario_id),
        market=_validate_market(scenario.get("market", {}), scenario_id),
        broker=_validate_broker(scenario.get("broker", {}), scenario_id),
        faults=_validate_faults(scenario.get("faults", []), scenario_id),
        steps=steps,
        expected=_validate_expected(scenario["expected"], scenario_id),
        source_path=source_path,
    )
    _require_resolvable_primary(definition)
    return definition


def _require_resolvable_primary(definition: ScenarioDefinition) -> None:
    """Row-level expectations need exactly one subject order: the declared
    primary id, or the single id the first ``register`` step introduces."""
    if resolve_primary_id(definition) is not None:
        return
    row_level = EXPECTED_KEYS - {
        "broker_submit_calls",
        "order_rows",
        "history_rows",
        "submitted_request",
        "shadow_rows",
        "shadow_db_created",
        "decision_db_created",
    }
    used = set(definition.expected) & row_level
    if used:
        raise _fail(
            definition.scenario_id,
            f"expected keys {sorted(used)} need a subject order; declare "
            "primary_execution_request_id or start with a register or shadow step",
        )


def resolve_primary_id(definition: ScenarioDefinition) -> str | None:
    """The execution_request_id row-level expectations are checked against."""
    if definition.primary_execution_request_id is not None:
        return definition.primary_execution_request_id
    for step in definition.steps:
        if step.action in ("register", "shadow", "shadow_concurrent"):
            return step.params["request"]["execution_request_id"]
    return None


def load_scenario_file(path: Path) -> ScenarioDefinition:
    """Load and validate ONE scenario file (one JSON object per file)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScenarioDefinitionError(f"cannot read scenario file {path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScenarioDefinitionError(f"scenario file {path} is not valid JSON: {exc}") from exc
    return validate_scenario(raw, source_path=path)


def load_scenarios(paths: list[Path]) -> tuple[ScenarioDefinition, ...]:
    """Load scenario files ALL-OR-NOTHING and refuse duplicate scenario ids."""
    definitions: list[ScenarioDefinition] = []
    seen: dict[str, Path] = {}
    for path in paths:
        definition = load_scenario_file(path)
        previous = seen.get(definition.scenario_id)
        if previous is not None:
            raise ScenarioDefinitionError(
                f"duplicate scenario_id {definition.scenario_id!r} in {path} (already defined in {previous})"
            )
        seen[definition.scenario_id] = path
        definitions.append(definition)
    return tuple(definitions)


def scenario_files(directory: Path) -> list[Path]:
    """All ``*.json`` scenario files in ``directory``, sorted by name. A
    missing or empty directory is an error -- the fixed suite must never
    silently become empty."""
    if not directory.is_dir():
        raise ScenarioDefinitionError(f"scenario directory {directory} does not exist")
    files = sorted(directory.glob("*.json"))
    if not files:
        raise ScenarioDefinitionError(f"scenario directory {directory} contains no *.json files")
    return files
