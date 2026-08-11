"""Issue #77: scenario-definition schema validation is closed-world and
all-or-nothing -- invalid scenario files must FAIL the load, never be
silently skipped or partially compared."""

import json
from pathlib import Path

import pytest

from execution.scenario_models import (
    DEFAULT_SCENARIO_DIR,
    ScenarioDefinitionError,
    load_scenario_file,
    load_scenarios,
    resolve_primary_id,
    scenario_files,
    validate_scenario,
)


def minimal_scenario(**overrides):
    """A fully valid minimal scenario; tests mutate copies of it."""
    scenario = {
        "scenario_id": "minimal_ok",
        "description": "minimal valid scenario",
        "steps": [
            {
                "action": "register",
                "request": {
                    "execution_request_id": "exec-1",
                    "signal_id": "signal-1",
                    "strategy_id": "strategy-1",
                    "symbol": "USD_JPY",
                    "side": "buy",
                    "order_type": "market",
                    "quantity": "1",
                    "stop_loss_price": "149.50",
                    "take_profit_price": "151.50",
                },
                "client_order_id": "client-1",
                "expect": {"outcome": "registered"},
            },
            {
                "action": "submit",
                "execution_request_id": "exec-1",
                "expect": {"outcome": "submitted", "state": "filled"},
            },
        ],
        "expected": {"final_state": "filled", "broker_submit_calls": 1},
    }
    scenario.update(overrides)
    return scenario


def write_scenario(path: Path, scenario: dict) -> Path:
    path.write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
    return path


# --- valid definitions --------------------------------------------------------


def test_minimal_scenario_validates():
    definition = validate_scenario(minimal_scenario())
    assert definition.scenario_id == "minimal_ok"
    assert len(definition.steps) == 2
    assert resolve_primary_id(definition) == "exec-1"


def test_scenario_file_round_trips(tmp_path):
    path = write_scenario(tmp_path / "a.json", minimal_scenario())
    definition = load_scenario_file(path)
    assert definition.source_path == path


def test_bundled_fixed_scenarios_all_validate():
    definitions = load_scenarios(scenario_files(DEFAULT_SCENARIO_DIR))
    assert len(definitions) >= 14
    assert len({d.scenario_id for d in definitions}) == len(definitions)


def test_primary_id_can_be_declared_explicitly():
    scenario = minimal_scenario(primary_execution_request_id="exec-1")
    assert resolve_primary_id(validate_scenario(scenario)) == "exec-1"


# --- invalid definitions are refused loudly ----------------------------------


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda s: s.pop("scenario_id"), "scenario_id"),
        (lambda s: s.update(scenario_id="Bad-ID"), "scenario_id"),
        (lambda s: s.pop("description"), "description"),
        (lambda s: s.pop("steps"), "steps"),
        (lambda s: s.update(steps=[]), "steps"),
        (lambda s: s.pop("expected"), "expected"),
        (lambda s: s.update(expected={}), "at least one comparison key"),
        (lambda s: s.update(unknown_key=1), "unknown keys"),
        (lambda s: s["steps"][0].pop("expect"), "expect"),
        (lambda s: s["steps"][0].update(action="teleport"), "action"),
        (lambda s: s["steps"][0]["request"].pop("symbol"), "missing required keys"),
        (lambda s: s["steps"][0]["request"].update(side="hold"), "side"),
        (lambda s: s["steps"][0]["request"].update(quantity="0"), "quantity"),
        (lambda s: s["steps"][0]["request"].update(quantity=1.5), "quantity"),
        (lambda s: s["steps"][0]["request"].update(surprise=1), "unknown keys"),
        (lambda s: s["steps"][1]["expect"].update(outcome="maybe"), "outcome"),
        (lambda s: s["steps"][1]["expect"].update(state="teleported"), "state"),
        (lambda s: s["steps"][1]["expect"].update(rejection_codes=["not_a_code"]), "rejection codes"),
        (lambda s: s["expected"].update(final_state="teleported"), "final_state"),
        (lambda s: s["expected"].update(final_phase="warp"), "final_phase"),
        # legacy [previous, new] pair form is no longer a valid entry
        (lambda s: s["expected"].update(state_history=[["pending", "filled"]]), "state_history"),
        (lambda s: s["expected"].update(state_history=[{"new_state": "pending"}]), "missing required keys"),
        (
            lambda s: s["expected"].update(
                state_history=[{"previous_state": "pending", "new_state": "warp"}]
            ),
            "new_state",
        ),
        (
            lambda s: s["expected"].update(
                state_history=[{"previous_state": "warp", "new_state": "filled"}]
            ),
            "previous_state",
        ),
        (
            lambda s: s["expected"].update(
                state_history=[{"previous_state": None, "new_state": "pending", "surprise": 1}]
            ),
            "unknown keys",
        ),
        (
            lambda s: s["expected"].update(
                state_history=[{"previous_state": None, "new_state": "pending", "reason": ""}]
            ),
            "reason",
        ),
        (lambda s: s["expected"].update(broker_submit_calls="1"), "broker_submit_calls"),
        (lambda s: s["expected"].update(last_decision_codes=[]), "last_decision_codes"),
        (lambda s: s["expected"].update(nonsense=True), "unknown keys"),
        (lambda s: s["expected"].update(submitted_request={"colour": "red"}), "unknown keys"),
        (lambda s: s.update(faults=[{"boundary": "everywhere"}]), "boundary"),
        (lambda s: s.update(faults=[{"boundary": "initial_save", "after_calls": -1}]), "after_calls"),
        (lambda s: s.update(broker={"scripts": {"client-1": {"behavior": "explode"}}}), "behavior"),
        # Issue #99: cancel is back in the schema, closed-world validated.
        (
            lambda s: s.update(broker={"cancel_scripts": {"client-1": {"behavior": "explode"}}}),
            "behavior",
        ),
        (
            lambda s: s.update(
                broker={"cancel_scripts": {"client-1": {"behavior": "fail_cancellation", "price": "1"}}}
            ),
            "unknown keys",
        ),
        (
            lambda s: s["steps"].append(
                {"action": "cancel", "execution_request_id": "exec-1", "expect": {"outcome": "maybe"}}
            ),
            "outcome",
        ),
        (
            lambda s: s["steps"].append(
                {
                    "action": "cancel",
                    "execution_request_id": "exec-1",
                    "expect": {"outcome": "refused", "refusal_code": "not_a_code"},
                }
            ),
            "refusal_code",
        ),
        (
            lambda s: s["steps"].append(
                {
                    "action": "submit",
                    "execution_request_id": "exec-1",
                    # refusal_code is a CANCEL-step key only
                    "expect": {"outcome": "rejected", "refusal_code": "cancel_terminal_state"},
                }
            ),
            "unknown keys",
        ),
        (
            lambda s: s["steps"].append(
                {
                    "action": "cancel",
                    "execution_request_id": "exec-1",
                    "at_offset_seconds": "60",
                    "expect": {"outcome": "cancelled"},
                }
            ),
            "at_offset_seconds",
        ),
        (
            lambda s: s.update(broker={"scripts": {"client-1": {"behavior": "reject", "price": "1"}}}),
            "unknown keys",
        ),
        (lambda s: s.update(market={"age_seconds": -5}), "age_seconds"),
        (lambda s: s.update(market={"tradable": "yes"}), "tradable"),
        (lambda s: s.update(gate={"allowed_sides": ["hold"]}), "allowed_sides"),
        (lambda s: s.update(gate={"surprise": 1}), "unknown keys"),
    ],
)
def test_invalid_scenarios_are_rejected(mutate, match):
    scenario = minimal_scenario()
    mutate(scenario)
    with pytest.raises(ScenarioDefinitionError, match=match):
        validate_scenario(scenario)


def shadow_scenario(**overrides):
    """A fully valid minimal shadow scenario (Issue #78)."""
    scenario = {
        "scenario_id": "shadow_ok",
        "description": "minimal valid shadow scenario",
        "steps": [
            {
                "action": "shadow",
                "request": {
                    "execution_request_id": "exec-1",
                    "signal_id": "signal-1",
                    "strategy_id": "strategy-1",
                    "symbol": "USD_JPY",
                    "side": "buy",
                    "order_type": "market",
                    "quantity": "1",
                    "stop_loss_price": "149.50",
                    "take_profit_price": "151.50",
                },
                "client_order_id": "client-1",
                "timeframe": "H1",
                "expect": {"outcome": "recorded", "allowed": True},
            }
        ],
        "expected": {"shadow_rows": 1, "broker_submit_calls": 0, "shadow_allowed": True},
    }
    scenario.update(overrides)
    return scenario


def test_shadow_scenario_validates_and_resolves_its_primary_id():
    definition = validate_scenario(shadow_scenario())
    assert resolve_primary_id(definition) == "exec-1"


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda s: s["steps"][0].update(surprise=1), "unknown keys"),
        (lambda s: s["steps"][0].update(dry_run="yes"), "dry_run"),
        (lambda s: s["steps"][0].update(timeframe=" "), "timeframe"),
        (lambda s: s["steps"][0]["expect"].update(outcome="submitted"), "outcome"),
        (lambda s: s["steps"][0]["expect"].update(allowed="yes"), "allowed"),
        (lambda s: s["steps"][0]["expect"].update(state="filled"), "unknown keys"),
        (
            lambda s: s["steps"][0]["expect"].update(rejection_codes=["not_a_code"]),
            "rejection codes",
        ),
        (lambda s: s["expected"].update(shadow_rows="1"), "shadow_rows"),
        (lambda s: s["expected"].update(shadow_db_created="yes"), "shadow_db_created"),
        (lambda s: s["expected"].update(shadow_allowed="yes"), "shadow_allowed"),
        (lambda s: s["expected"].update(shadow_codes=[]), "shadow_codes"),
        (lambda s: s["expected"].update(shadow_codes=["fabricated"]), "shadow_codes"),
        (
            lambda s: s["expected"].update(shadow_intended_order={"colour": "red"}),
            "unknown keys",
        ),
        (
            lambda s: s["expected"].update(shadow_intended_order={"side": "hold"}),
            "side",
        ),
        (lambda s: s.update(gate={"allowed_shadow_targets": []}), "allowed_shadow_targets"),
        (
            lambda s: s["steps"].__setitem__(
                0,
                {
                    "action": "shadow_concurrent",
                    "request": s["steps"][0]["request"],
                    "client_order_id": "client-1",
                    "workers": 1,
                    "expect": {"recorded": 1},
                },
            ),
            "workers",
        ),
        (
            lambda s: s["steps"].__setitem__(
                0,
                {
                    "action": "shadow_concurrent",
                    "request": s["steps"][0]["request"],
                    "client_order_id": "client-1",
                    "workers": 2,
                    "expect": {"conflicts": 1},
                },
            ),
            "unknown keys",
        ),
    ],
)
def test_invalid_shadow_scenarios_are_rejected(mutate, match):
    scenario = shadow_scenario()
    mutate(scenario)
    with pytest.raises(ScenarioDefinitionError, match=match):
        validate_scenario(scenario)


def test_shadow_scenario_null_shadow_allowed_is_valid():
    scenario = shadow_scenario()
    scenario["expected"]["shadow_allowed"] = None
    validate_scenario(scenario)


def production_scenario(**overrides):
    scenario = shadow_scenario(scenario_id="shadow_prod_ok")
    scenario["signals"] = [
        {"signal_id": "sig-1", "symbol": "USD_JPY", "timeframe": "H1", "side": "long"}
    ]
    scenario["production_config"] = {
        "config_id": "c1",
        "policy": {
            "policy_id": "p1",
            "allowed_symbols": ["USD_JPY"],
            "allowed_sides": ["buy", "sell"],
            "max_order_quantity": "10",
            "max_positions_per_symbol": 2,
            "max_positions_account": 5,
            "max_reference_age_seconds": 30,
            "max_spread_by_symbol": {"USD_JPY": "0.05"},
            "allowed_shadow_targets": ["shadow"],
            "require_stop_loss": False,
        },
        "order_quantities": {"USD_JPY": "1"},
        "kill_switch": False,
        "connection_target": "shadow",
    }
    scenario["steps"] = [
        {
            "action": "shadow_production",
            "signal_id": "sig-1",
            "expect": {"outcome": "recorded", "allowed": True},
        }
    ]
    scenario.update(overrides)
    return scenario


def test_production_scenario_validates_and_derives_its_primary_id():
    definition = validate_scenario(production_scenario())
    assert resolve_primary_id(definition) == "sigreq-sig-1"


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda s: s.pop("signals"), "signals fixture"),
        (lambda s: s.pop("production_config"), "production_config"),
        (lambda s: s["signals"][0].update(side="sideways"), "side"),
        (lambda s: s["signals"][0].update(surprise=1), "unknown keys"),
        (lambda s: s["production_config"]["policy"].pop("require_stop_loss"), "require_stop_loss"),
        (lambda s: s["production_config"].update(surprise=1), "unknown keys"),
        (lambda s: s["steps"][0].update(signal_id="unknown"), "unknown signal_id"),
        (lambda s: s.update(faults=[{"boundary": "shadow_claim", "after_calls": 1, "fail_calls": 1}]),
         "must not declare both"),
        (lambda s: s.update(faults=[{"boundary": "shadow_claim", "fail_calls": 0}]), "fail_calls"),
    ],
)
def test_invalid_production_scenarios_are_rejected(mutate, match):
    scenario = production_scenario()
    mutate(scenario)
    with pytest.raises(ScenarioDefinitionError, match=match):
        validate_scenario(scenario)


def test_duplicate_json_keys_in_a_scenario_file_fail_the_load(tmp_path):
    path = tmp_path / "dup.json"
    body = json.dumps(minimal_scenario())
    duplicated = body.replace('{"scenario_id"', '{"description": "dup", "scenario_id"', 1)
    path.write_text(duplicated, encoding="utf-8")
    with pytest.raises(ScenarioDefinitionError, match="duplicate object key"):
        load_scenario_file(path)


def test_expected_filled_quantity_zero_is_allowed_but_request_quantity_zero_is_not():
    scenario = minimal_scenario()
    scenario["expected"]["filled_quantity"] = "0"
    validate_scenario(scenario)  # expected zero fill is a legitimate outcome


def test_row_level_expectations_need_a_subject_order():
    scenario = minimal_scenario()
    scenario["steps"] = [
        {
            "action": "register_concurrent",
            "registrations": [
                {"request": minimal_scenario()["steps"][0]["request"], "client_order_id": "client-1"},
                {
                    "request": {
                        **minimal_scenario()["steps"][0]["request"],
                        "execution_request_id": "exec-2",
                    },
                    "client_order_id": "client-2",
                },
            ],
            "expect": {"registered": 1, "conflicts": 1},
        }
    ]
    with pytest.raises(ScenarioDefinitionError, match="subject order"):
        validate_scenario(scenario)


def test_concurrent_step_requires_at_least_two_workers():
    scenario = minimal_scenario()
    scenario["steps"][1] = {
        "action": "submit_concurrent",
        "execution_request_id": "exec-1",
        "workers": 1,
        "expect": {"submitted": 1},
    }
    with pytest.raises(ScenarioDefinitionError, match="workers"):
        validate_scenario(scenario)


def test_non_json_file_is_rejected(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ScenarioDefinitionError, match="not valid JSON"):
        load_scenario_file(path)


def test_duplicate_scenario_ids_across_files_are_rejected(tmp_path):
    first = write_scenario(tmp_path / "a.json", minimal_scenario())
    second = write_scenario(tmp_path / "b.json", minimal_scenario())
    with pytest.raises(ScenarioDefinitionError, match="duplicate scenario_id"):
        load_scenarios([first, second])


def test_one_invalid_file_fails_the_whole_load(tmp_path):
    good = write_scenario(tmp_path / "a.json", minimal_scenario())
    bad = write_scenario(
        tmp_path / "b.json", minimal_scenario(scenario_id="minimal_bad", expected={"nonsense": 1})
    )
    with pytest.raises(ScenarioDefinitionError):
        load_scenarios([good, bad])


def test_missing_and_empty_scenario_directories_are_errors(tmp_path):
    with pytest.raises(ScenarioDefinitionError, match="does not exist"):
        scenario_files(tmp_path / "nope")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ScenarioDefinitionError, match="no \\*\\.json files"):
        scenario_files(empty)
