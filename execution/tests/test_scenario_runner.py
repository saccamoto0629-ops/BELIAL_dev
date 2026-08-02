"""Issue #77: the scenario runner itself -- PASS/FAIL/ERROR classification,
exit codes, scenario filtering, per-scenario DB isolation, fault-injection
cleanup and the no-external-I/O guarantee. The fixed bundled scenarios are
integration-tested separately in ``test_fixed_scenarios.py``."""

import json
import socket

import pytest

from execution import execution_store, scenario_runner
from execution.scenario_models import validate_scenario
from execution.scenario_runner import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_USAGE_ERROR,
    STATUS_ERROR,
    STATUS_FAIL,
    STATUS_PASS,
    main,
    run_scenario,
    run_scenarios,
)
# pytest puts this file's directory on sys.path (no tests/__init__.py, same
# as every other suite in the repository), so sibling helpers import flat.
from test_scenario_models import minimal_scenario, write_scenario


def build(scenario_dict):
    return validate_scenario(scenario_dict)


def run_one(scenario_dict, tmp_path):
    work_dir = tmp_path / scenario_dict["scenario_id"]
    work_dir.mkdir()
    return run_scenario(build(scenario_dict), work_dir)


# --- PASS / FAIL / ERROR classification --------------------------------------


def test_matching_expectations_pass(tmp_path):
    outcome = run_one(minimal_scenario(), tmp_path)
    assert outcome.status == STATUS_PASS
    assert outcome.mismatches == []


def test_expected_mismatch_fails_with_field_level_diff(tmp_path):
    scenario = minimal_scenario()
    scenario["expected"]["final_state"] = "cancelled"  # actual will be filled
    outcome = run_one(scenario, tmp_path)
    assert outcome.status == STATUS_FAIL
    rendered = [mismatch.render() for mismatch in outcome.mismatches]
    assert any("expected.final_state" in line for line in rendered)
    # the runner reports the ACTUAL value; it never rewrites it to match
    assert any("'filled'" in line for line in rendered)


def test_step_outcome_mismatch_fails(tmp_path):
    scenario = minimal_scenario()
    scenario["steps"][1]["expect"] = {"outcome": "rejected", "rejection_codes": ["kill_switch_enabled"]}
    outcome = run_one(scenario, tmp_path)
    assert outcome.status == STATUS_FAIL
    assert any("steps[1](submit).outcome" in m.render() for m in outcome.mismatches)


def test_unhandled_exception_is_an_error_not_a_fail(tmp_path):
    # bid > ask is schema-valid data but the production MarketSafetySnapshot
    # constructor refuses it -> the runner records ERROR with the traceback.
    scenario = minimal_scenario(market={"bid": "150.10", "ask": "149.90"})
    outcome = run_one(scenario, tmp_path)
    assert outcome.status == STATUS_ERROR
    assert outcome.error is not None and "ask" in outcome.error


# --- isolation / determinism --------------------------------------------------


def test_scenarios_reuse_ids_but_stay_isolated_and_order_independent(tmp_path):
    # Same execution_request_id / signal_id / client_order_id in both
    # scenarios: only per-scenario private DBs let both pass, in either order.
    first = minimal_scenario(scenario_id="scenario_a")
    second = minimal_scenario(scenario_id="scenario_b")
    for index, ordering in enumerate((
        (first, second),
        (second, first),
    )):
        report = run_scenarios(
            tuple(build(s) for s in ordering), tmp_path / f"run{index}"
        )
        assert [o.status for o in report.outcomes] == [STATUS_PASS, STATUS_PASS]


def test_no_network_is_reachable_during_a_run(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("scenario tests must never open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    outcome = run_one(minimal_scenario(), tmp_path)
    assert outcome.status == STATUS_PASS


def test_fault_injection_restores_the_store_functions(tmp_path):
    originals = {
        name: getattr(execution_store, name)
        for name in ("create_execution_order", "mark_submitting", "record_order_result")
    }
    scenario = minimal_scenario(faults=[{"boundary": "initial_save"}])
    scenario["steps"][0]["expect"] = {"outcome": "error", "error_type": "ExecutionStoreError"}
    scenario["steps"][1]["expect"] = {
        "outcome": "rejected",
        "rejection_codes": ["intent_not_registered"],
    }
    scenario["expected"] = {"order_rows": 0, "broker_submit_calls": 0, "final_phase": None}
    outcome = run_one(scenario, tmp_path)
    assert outcome.status == STATUS_PASS
    for name, original in originals.items():
        assert getattr(execution_store, name) is original


# --- CLI ----------------------------------------------------------------------


def cli_dir(tmp_path, *scenarios):
    directory = tmp_path / "scenarios"
    directory.mkdir()
    for index, scenario in enumerate(scenarios):
        write_scenario(directory / f"{index:02d}_{scenario['scenario_id']}.json", scenario)
    return directory


def test_cli_exit_zero_only_when_every_scenario_passes(tmp_path, capsys):
    directory = cli_dir(tmp_path, minimal_scenario())
    assert main(["--scenario-dir", str(directory)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "PASS minimal_ok" in out
    assert "total 1  pass 1  fail 0  error 0" in out


def test_cli_exit_nonzero_on_any_mismatch(tmp_path, capsys):
    failing = minimal_scenario(scenario_id="failing_one")
    failing["expected"]["broker_submit_calls"] = 7
    directory = cli_dir(tmp_path, minimal_scenario(), failing)
    assert main(["--scenario-dir", str(directory)]) == EXIT_FAILURE
    out = capsys.readouterr().out
    assert "FAIL failing_one" in out
    assert "failed: failing_one" in out


def test_cli_exit_nonzero_on_error(tmp_path):
    erroring = minimal_scenario(scenario_id="error_one", market={"bid": "150.10", "ask": "149.90"})
    directory = cli_dir(tmp_path, erroring)
    assert main(["--scenario-dir", str(directory)]) == EXIT_FAILURE


def test_cli_invalid_scenario_file_is_a_usage_error(tmp_path, capsys):
    directory = cli_dir(tmp_path, minimal_scenario(expected={"nonsense": 1}))
    assert main(["--scenario-dir", str(directory)]) == EXIT_USAGE_ERROR
    assert "scenario definition error" in capsys.readouterr().err


def test_cli_scenario_id_filter_runs_only_the_selected_scenario(tmp_path, capsys):
    directory = cli_dir(
        tmp_path,
        minimal_scenario(scenario_id="first_one"),
        minimal_scenario(scenario_id="second_one"),
    )
    assert main(["--scenario-dir", str(directory), "--scenario-id", "second_one"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "second_one" in out and "first_one" not in out


def test_cli_unknown_scenario_id_is_a_usage_error(tmp_path, capsys):
    directory = cli_dir(tmp_path, minimal_scenario())
    assert main(["--scenario-dir", str(directory), "--scenario-id", "nope"]) == EXIT_USAGE_ERROR
    assert "unknown scenario id" in capsys.readouterr().err


def test_cli_scenario_file_selection(tmp_path, capsys):
    directory = cli_dir(tmp_path, minimal_scenario())
    path = directory / "00_minimal_ok.json"
    assert main(["--scenario-file", str(path)]) == EXIT_OK
    assert "PASS minimal_ok" in capsys.readouterr().out


def test_cli_list_prints_ids_without_running(tmp_path, capsys):
    directory = cli_dir(tmp_path, minimal_scenario())
    assert main(["--scenario-dir", str(directory), "--list"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "minimal_ok" in out and "PASS" not in out


def test_cli_json_report_is_machine_readable(tmp_path, capsys):
    failing = minimal_scenario(scenario_id="failing_one")
    failing["expected"]["final_state"] = "cancelled"
    directory = cli_dir(tmp_path, minimal_scenario(), failing)
    assert main(["--scenario-dir", str(directory), "--json"]) == EXIT_FAILURE
    report = json.loads(capsys.readouterr().out)
    assert report["total"] == 2 and report["passed"] == 1 and report["failed"] == 1
    by_id = {entry["scenario_id"]: entry for entry in report["scenarios"]}
    assert by_id["minimal_ok"]["status"] == STATUS_PASS
    assert by_id["failing_one"]["status"] == STATUS_FAIL
    assert by_id["failing_one"]["mismatches"][0]["field"] == "expected.final_state"


def test_cli_restores_logging_disable_level(tmp_path):
    import logging

    directory = cli_dir(tmp_path, minimal_scenario())
    before = logging.root.manager.disable
    assert main(["--scenario-dir", str(directory)]) == EXIT_OK
    assert logging.root.manager.disable == before


# --- runner never re-decides -------------------------------------------------


def test_runner_defines_no_execution_decision_logic():
    """The runner must not re-implement gate conditions, state transitions or
    idempotency: it may not import the transition table / validators, and the
    production modules it drives stay the only decision-makers."""
    import ast
    import pathlib

    source = pathlib.Path(scenario_runner.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_names.update(alias.name for alias in node.names)
    forbidden = {
        "ORDER_STATE_TRANSITIONS",
        "validate_order_state_transition",
        "validate_order_result_update",
        "SafetyRejectionCode",
        "dedupe_rejections",
    }
    assert imported_names & forbidden == set()
