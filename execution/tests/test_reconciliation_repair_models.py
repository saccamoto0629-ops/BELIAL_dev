"""Issue #104: broker reconciliation repair models -- validation only.

No I/O anywhere in this suite: pure model construction and invariants.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from broker.broker_models import BrokerErrorCode, OrderState
from execution.reconciliation_models import ReconciliationTargetType
from execution.reconciliation_repair_models import (
    OrderRepairAttempt,
    ReconciliationRepairResult,
    RepairAttemptReservation,
    RepairMode,
    RepairOutcome,
)

T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def reservation(**overrides) -> RepairAttemptReservation:
    kwargs = dict(
        attempt_id="attempt-1",
        run_id="run-1",
        target_type=ReconciliationTargetType.EXECUTION_ORDER,
        target_id="exec-1",
        reserved_at_utc=at(1),
        detail="reserved: repair evaluation started",
        execution_request_id="exec-1",
        client_order_id="client-1",
        reconciliation_internal_state=OrderState.UNKNOWN,
        reconciliation_broker_state=OrderState.FILLED,
        internal_revision=2,
        observed_at_utc=at(0),
    )
    kwargs.update(overrides)
    return RepairAttemptReservation(**kwargs)


def attempt(**overrides) -> OrderRepairAttempt:
    kwargs = dict(
        attempt_id="attempt-1",
        run_id="run-1",
        mode=RepairMode.APPLY,
        target_type=ReconciliationTargetType.EXECUTION_ORDER,
        target_id="exec-1",
        outcome=RepairOutcome.APPLIED,
        detail="the broker confirmed state 'filled'",
        reserved_at_utc=at(1),
        execution_request_id="exec-1",
        client_order_id="client-1",
        reconciliation_internal_state=OrderState.UNKNOWN,
        reconciliation_broker_state=OrderState.FILLED,
        internal_revision=2,
        internal_state_before=OrderState.UNKNOWN,
        internal_state_after=OrderState.FILLED,
        broker_order_id="B-1",
        observed_at_utc=at(0),
        applied_at_utc=at(2),
    )
    kwargs.update(overrides)
    return OrderRepairAttempt(**kwargs)


def skipped(**overrides) -> OrderRepairAttempt:
    kwargs = dict(
        outcome=RepairOutcome.SKIPPED,
        internal_state_after=None,
        applied_at_utc=None,
        failure_code="not_eligible_manual_review",
        detail="manual review targets are never auto-repaired",
    )
    kwargs.update(overrides)
    return attempt(**kwargs)


# --- reservation invariants ---------------------------------------------------


def test_a_valid_reservation_constructs():
    entry = reservation()
    assert entry.internal_revision == 2
    assert entry.reconciliation_internal_state is OrderState.UNKNOWN


@pytest.mark.parametrize(
    "field, value",
    [
        ("attempt_id", ""),
        ("run_id", " "),
        ("target_id", ""),
        ("detail", ""),
        ("target_type", "execution_order"),
        ("reconciliation_internal_state", "unknown"),
        ("internal_revision", -1),
        ("internal_revision", True),
        ("reserved_at_utc", datetime(2026, 8, 1)),
        ("observed_at_utc", datetime(2026, 8, 1)),
    ],
)
def test_malformed_reservation_fields_are_rejected(field, value):
    with pytest.raises(ValueError):
        reservation(**{field: value})


def test_a_reservation_pairs_execution_target_and_request_id():
    with pytest.raises(ValueError, match="execution_request_id"):
        reservation(execution_request_id=None)
    with pytest.raises(ValueError, match="execution_request_id"):
        reservation(target_type=ReconciliationTargetType.BROKER_ORDER, target_id="client-9")
    broker_only = reservation(
        target_type=ReconciliationTargetType.BROKER_ORDER,
        target_id="client-9",
        execution_request_id=None,
        reconciliation_internal_state=None,
        internal_revision=None,
    )
    assert broker_only.execution_request_id is None


def test_an_execution_reservation_requires_the_observed_internal_state():
    with pytest.raises(ValueError, match="internal state"):
        reservation(reconciliation_internal_state=None)


# --- attempt invariants -------------------------------------------------------


def test_a_valid_applied_attempt_constructs():
    entry = attempt()
    assert entry.outcome is RepairOutcome.APPLIED
    assert entry.applied_at_utc == at(2)
    assert entry.failure_code is None


@pytest.mark.parametrize(
    "field, value",
    [
        ("attempt_id", ""),
        ("run_id", " "),
        ("target_id", ""),
        ("detail", ""),
        ("mode", "apply"),
        ("target_type", "execution_order"),
        ("outcome", "applied"),
        ("reconciliation_internal_state", "unknown"),
        ("internal_state_after", "filled"),
        ("broker_error_code", "timeout"),
        ("internal_revision", -3),
        ("reserved_at_utc", datetime(2026, 8, 1)),
    ],
)
def test_malformed_attempt_fields_are_rejected(field, value):
    with pytest.raises(ValueError):
        attempt(**{field: value})


def test_applied_requires_apply_mode():
    with pytest.raises(ValueError, match="apply mode"):
        attempt(mode=RepairMode.DRY_RUN)


def test_would_apply_requires_dry_run_mode():
    with pytest.raises(ValueError, match="dry_run mode"):
        attempt(outcome=RepairOutcome.WOULD_APPLY, applied_at_utc=None)
    entry = attempt(
        mode=RepairMode.DRY_RUN, outcome=RepairOutcome.WOULD_APPLY, applied_at_utc=None
    )
    assert entry.internal_state_after is OrderState.FILLED


def test_applied_at_exists_exactly_for_applied():
    with pytest.raises(ValueError, match="applied_at_utc"):
        attempt(applied_at_utc=None)
    with pytest.raises(ValueError, match="applied_at_utc"):
        skipped(applied_at_utc=at(2))


def test_applied_at_lives_in_the_broker_clock_domain():
    """applied_at is the committed state-history timestamp (= the broker
    snapshot's updated_at) and is deliberately NOT ordered against the
    repair-side reservation clock."""
    entry = attempt(applied_at_utc=at(1) - timedelta(days=30))
    assert entry.applied_at_utc < entry.reserved_at_utc


def test_the_after_state_exists_exactly_for_applied_like_outcomes():
    with pytest.raises(ValueError, match="internal_state_after"):
        attempt(internal_state_after=None)
    with pytest.raises(ValueError, match="internal_state_after"):
        skipped(internal_state_after=OrderState.FILLED)
    already = attempt(
        outcome=RepairOutcome.ALREADY_APPLIED,
        applied_at_utc=None,
        internal_state_before=OrderState.FILLED,
        internal_state_after=OrderState.FILLED,
    )
    assert already.internal_state_after is OrderState.FILLED


def test_the_broker_error_code_exists_exactly_for_query_failed():
    with pytest.raises(ValueError, match="broker_error_code"):
        skipped(broker_error_code=BrokerErrorCode.TIMEOUT)
    with pytest.raises(ValueError, match="broker_error_code"):
        skipped(outcome=RepairOutcome.QUERY_FAILED, failure_code="broker_query_failed")
    entry = skipped(
        outcome=RepairOutcome.QUERY_FAILED,
        broker_error_code=BrokerErrorCode.TIMEOUT,
        failure_code="broker_query_failed",
    )
    assert entry.broker_error_code is BrokerErrorCode.TIMEOUT


def test_the_failure_code_exists_exactly_for_non_success_outcomes():
    with pytest.raises(ValueError, match="failure_code"):
        attempt(failure_code="some_code")  # applied must not carry one
    with pytest.raises(ValueError, match="failure_code"):
        skipped(failure_code=None)  # skipped must carry one
    with pytest.raises(ValueError):
        skipped(failure_code="  ")
    stale = skipped(outcome=RepairOutcome.STALE, failure_code="internal_order_changed")
    assert stale.failure_code == "internal_order_changed"


def test_execution_request_id_exists_exactly_for_execution_order_targets():
    with pytest.raises(ValueError, match="execution_request_id"):
        attempt(execution_request_id=None)
    with pytest.raises(ValueError, match="execution_request_id"):
        skipped(target_type=ReconciliationTargetType.BROKER_ORDER, target_id="client-9")
    entry = skipped(
        target_type=ReconciliationTargetType.BROKER_ORDER,
        target_id="client-9",
        execution_request_id=None,
        reconciliation_internal_state=None,
        internal_revision=None,
        internal_state_before=None,
    )
    assert entry.execution_request_id is None


def test_an_execution_target_requires_the_observed_internal_state():
    with pytest.raises(ValueError, match="internal state"):
        attempt(reconciliation_internal_state=None)


def test_audit_error_is_optional_metadata():
    entry = replace(attempt(), audit_error="finalize failed; reservation stays pending")
    assert entry.audit_error is not None
    with pytest.raises(ValueError):
        attempt(audit_error="  ")


# --- result invariants --------------------------------------------------------


def result(**overrides) -> ReconciliationRepairResult:
    kwargs = dict(
        run_id="run-1",
        mode=RepairMode.APPLY,
        started_at_utc=T0,
        finished_at_utc=at(10),
        attempts=(attempt(), skipped(attempt_id="attempt-2")),
    )
    kwargs.update(overrides)
    return ReconciliationRepairResult(**kwargs)


def test_a_valid_result_exposes_outcome_helpers():
    summary = result()
    assert summary.by_outcome(RepairOutcome.APPLIED) == (attempt(),)
    counts = summary.outcome_counts()
    assert counts[RepairOutcome.APPLIED] == 1
    assert counts[RepairOutcome.SKIPPED] == 1
    assert set(counts) == set(RepairOutcome)
    assert summary.has_audit_failures is False


def test_has_audit_failures_reflects_attempt_metadata():
    flagged = replace(attempt(), audit_error="finalize failed")
    summary = result(attempts=(flagged,))
    assert summary.has_audit_failures is True


def test_a_result_refuses_foreign_or_inconsistent_attempts():
    with pytest.raises(ValueError, match="belongs to run"):
        result(attempts=(attempt(run_id="run-other"),))
    with pytest.raises(ValueError, match="mode"):
        result(
            mode=RepairMode.DRY_RUN,
            attempts=(
                attempt(
                    mode=RepairMode.DRY_RUN,
                    outcome=RepairOutcome.WOULD_APPLY,
                    applied_at_utc=None,
                ),
                skipped(attempt_id="attempt-2"),
            ),
        )
    with pytest.raises(ValueError, match="unique"):
        result(attempts=(skipped(), skipped()))
    with pytest.raises(ValueError, match="finished_at_utc"):
        result(finished_at_utc=T0 - timedelta(seconds=1))
    with pytest.raises(ValueError):
        result(attempts=[attempt()])
