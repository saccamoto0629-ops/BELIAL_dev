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
    # Issue #78 shadow-mode scenarios (incl. the PR #100 rework):
    "shadow_payload_conflict",
    "shadow_stale_claim_recovery",
    "shadow_production_signal_path",
    "shadow_gate_pass",
    "shadow_stale_price_rejected",
    "shadow_kill_switch_rejected",
    "shadow_mode_mismatch_rejected",
    "shadow_duplicate_request",
    "shadow_concurrent_single_record",
    "shadow_db_failure_initial_save",
    "shadow_decision_save_failure",
    "shadow_dry_run_no_side_effects",
    "fake_mode_no_shadow_record",
    "normal_market_order",
    "duplicate_signal_suppressed",
    "store_failure_before_submit",
    "communication_failure_unknown",
    "timeout_unknown_no_resend",
    "broker_reject",
    "partial_fill",
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
}

# Cancel scenarios are deliberately NOT part of the fixed suite (PR #96
# review r1): no production cancel Execution use case exists yet, so a fixed
# scenario could only exercise a runner-synthesized path. FakeBroker cancel
# behaviour is covered by broker/tests/test_fake_broker.py and the store's
# cancel transitions by execution/tests/test_execution_store.py
# (test_accepted_to_cancelled and the terminal-state invariants).


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


def test_scenarios_pass_in_reverse_order_too(tmp_path):
    """Order independence (Issue #78): the whole bundled suite passes when
    shuffled -- here deterministically reversed, so a failure reproduces --
    proving no scenario leans on another's leftover state."""
    from execution.scenario_runner import run_scenarios

    report = run_scenarios(tuple(reversed(DEFINITIONS)), tmp_path)
    failing = [o.scenario_id for o in report.outcomes if o.status != STATUS_PASS]
    assert failing == [], f"scenarios fail when run in reverse order: {failing}"
