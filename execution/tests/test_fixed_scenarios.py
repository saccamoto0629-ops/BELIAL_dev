"""Issue #77: every bundled fixed scenario (execution/scenarios/*.json) runs
through the scenario runner -- i.e. through the PRODUCTION execution path --
and must PASS. One parametrized test per scenario keeps a failing scenario id
visible directly in the pytest output."""

import pytest

from execution.scenario_models import (
    DEFAULT_SCENARIO_DIR,
    load_scenarios,
    scenario_files,
)
from execution.scenario_runner import STATUS_PASS, run_scenario

DEFINITIONS = load_scenarios(scenario_files(DEFAULT_SCENARIO_DIR))

# The scenarios Issue #77 requires, pinned by id: deleting or renaming one of
# these fixed scenarios must fail loudly here, not silently shrink coverage.
REQUIRED_SCENARIO_IDS = {
    "normal_market_order",
    "duplicate_signal_suppressed",
    "store_failure_before_submit",
    "communication_failure_unknown",
    "timeout_unknown_no_resend",
    "broker_reject",
    "partial_fill",
    "cancel_success",
    "cancel_failure",
    "fill_price_deviation",
    "db_failure_initial_save",
    "db_failure_result_save_after_acceptance",
    "db_failure_history_append_double_failure",
    "stale_price_rejected",
    "kill_switch_rejected",
    "execution_mode_mismatch_rejected",
    "concurrent_submit_single_broker_call",
    "concurrent_register_same_signal",
    "resubmit_after_accepted",
    "resubmit_while_submitting",
    "multiple_gate_violations",
    "invalid_state_transition_rejected_by_store",
    "same_request_id_different_payload_suppressed",
    "cancel_after_filled_fails",
}


def test_all_required_scenarios_are_bundled():
    bundled = {definition.scenario_id for definition in DEFINITIONS}
    assert REQUIRED_SCENARIO_IDS <= bundled, (
        f"missing fixed scenarios: {sorted(REQUIRED_SCENARIO_IDS - bundled)}"
    )


@pytest.mark.parametrize(
    "definition", DEFINITIONS, ids=[definition.scenario_id for definition in DEFINITIONS]
)
def test_fixed_scenario_passes(definition, tmp_path):
    outcome = run_scenario(definition, tmp_path)
    detail = "; ".join(mismatch.render() for mismatch in outcome.mismatches)
    assert outcome.status == STATUS_PASS, (
        f"{definition.scenario_id}: {outcome.status} {detail or outcome.error}"
    )
