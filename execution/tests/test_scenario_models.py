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
        (lambda s: s["expected"].update(state_history=[["pending"]]), "state_history"),
        (lambda s: s["expected"].update(state_history=[["pending", "warp"]]), "state_history"),
        (lambda s: s["expected"].update(broker_submit_calls="1"), "broker_submit_calls"),
        (lambda s: s["expected"].update(last_decision_codes=[]), "last_decision_codes"),
        (lambda s: s["expected"].update(nonsense=True), "unknown keys"),
        (lambda s: s["expected"].update(submitted_request={"colour": "red"}), "unknown keys"),
        (lambda s: s.update(faults=[{"boundary": "everywhere"}]), "boundary"),
        (lambda s: s.update(faults=[{"boundary": "initial_save", "after_calls": -1}]), "after_calls"),
        (lambda s: s.update(broker={"scripts": {"client-1": {"behavior": "explode"}}}), "behavior"),
        (
            lambda s: s.update(broker={"cancel_scripts": {"client-1": {"behavior": "explode"}}}),
            "behavior",
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
