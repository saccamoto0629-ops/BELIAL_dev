"""Issue #104: broker reconciliation repair models -- validation only.

No I/O anywhere in this suite: pure model construction and invariants
(claimed reservations, fenced attempts, the closed failure-code set and the
timestamp contract included).
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from broker.broker_models import BrokerErrorCode, OrderState
from execution.reconciliation_models import ReconciliationTargetType
from execution.reconciliation_repair_models import (
    ACTIVE_INTENT_STATUSES,
    ALLOWED_INTENT_TRANSITIONS,
    BLOCKED_REASON_FAILURE_CODES,
    OrderRepairAttempt,
    ReconciliationRepairResult,
    RepairAttemptReservation,
    RepairFailureCode,
    RepairMode,
    RepairOutcome,
    RepairTakeoverIntent,
    TakeoverBlockedReason,
    TakeoverIntentStatus,
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
        owner_id="worker-1",
        claim_execution_id="claim-1",
        generation=1,
        lease_expires_at_utc=at(601),
        last_claimed_at_utc=at(1),
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
        owner_id="worker-1",
        claim_execution_id="claim-1",
        generation=1,
        execution_request_id="exec-1",
        client_order_id="client-1",
        reconciliation_internal_state=OrderState.UNKNOWN,
        reconciliation_broker_state=OrderState.FILLED,
        internal_revision=2,
        internal_state_before=OrderState.UNKNOWN,
        internal_state_after=OrderState.FILLED,
        broker_order_id="B-1",
        observed_at_utc=at(0),
        broker_state_updated_at_utc=at(2),
        finalized_at_utc=at(3),
    )
    kwargs.update(overrides)
    return OrderRepairAttempt(**kwargs)


def skipped(**overrides) -> OrderRepairAttempt:
    kwargs = dict(
        outcome=RepairOutcome.SKIPPED,
        internal_state_after=None,
        broker_state_updated_at_utc=None,
        failure_code=RepairFailureCode.NOT_ELIGIBLE_MANUAL_REVIEW,
        detail="manual review targets are never auto-repaired",
    )
    kwargs.update(overrides)
    return attempt(**kwargs)


# --- reservation invariants ---------------------------------------------------


def test_a_valid_reservation_constructs():
    entry = reservation()
    assert entry.generation == 1
    assert entry.owner_id == "worker-1"
    assert entry.lease_expires_at_utc > entry.last_claimed_at_utc


@pytest.mark.parametrize(
    "field, value",
    [
        ("attempt_id", ""),
        ("run_id", " "),
        ("target_id", ""),
        ("detail", ""),
        ("owner_id", ""),
        ("claim_execution_id", " "),
        ("generation", 0),
        ("generation", True),
        ("generation", "1"),
        ("target_type", "execution_order"),
        ("reconciliation_internal_state", "unknown"),
        ("internal_revision", -1),
        ("internal_revision", True),
        ("reserved_at_utc", datetime(2026, 8, 1)),
        ("lease_expires_at_utc", datetime(2026, 8, 1)),
        ("last_claimed_at_utc", datetime(2026, 8, 1)),
        ("observed_at_utc", datetime(2026, 8, 1)),
    ],
)
def test_malformed_reservation_fields_are_rejected(field, value):
    with pytest.raises(ValueError):
        reservation(**{field: value})


def test_a_lease_must_extend_beyond_the_claim_time():
    with pytest.raises(ValueError, match="lease_expires_at_utc"):
        reservation(lease_expires_at_utc=at(1))
    with pytest.raises(ValueError, match="lease_expires_at_utc"):
        reservation(lease_expires_at_utc=at(0))


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
    assert entry.broker_state_updated_at_utc == at(2)
    assert entry.failure_code is None
    assert entry.generation == 1


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
        ("failure_code", "claim_lost"),
        ("internal_revision", -3),
        ("generation", 0),
        ("owner_id", " "),
        ("reserved_at_utc", datetime(2026, 8, 1)),
        ("finalized_at_utc", datetime(2026, 8, 1)),
    ],
)
def test_malformed_attempt_fields_are_rejected(field, value):
    with pytest.raises(ValueError):
        attempt(**{field: value})


def test_apply_attempts_require_a_claim_and_dry_run_forbids_one():
    for field in ("owner_id", "claim_execution_id", "generation"):
        with pytest.raises(ValueError, match=field):
            attempt(**{field: None})
    with pytest.raises(ValueError, match="claim"):
        attempt(
            mode=RepairMode.DRY_RUN,
            outcome=RepairOutcome.WOULD_APPLY,
            broker_state_updated_at_utc=None,
            finalized_at_utc=None,
        )
    dry = attempt(
        mode=RepairMode.DRY_RUN,
        outcome=RepairOutcome.WOULD_APPLY,
        owner_id=None,
        claim_execution_id=None,
        generation=None,
        broker_state_updated_at_utc=None,
        finalized_at_utc=None,
    )
    assert dry.generation is None


def test_applied_requires_apply_mode():
    with pytest.raises(ValueError, match="apply mode"):
        attempt(
            mode=RepairMode.DRY_RUN,
            owner_id=None,
            claim_execution_id=None,
            generation=None,
        )


def test_broker_state_updated_at_exists_exactly_for_applied():
    with pytest.raises(ValueError, match="broker_state_updated_at_utc"):
        attempt(broker_state_updated_at_utc=None)
    with pytest.raises(ValueError, match="broker_state_updated_at_utc"):
        skipped(broker_state_updated_at_utc=at(2))


def test_broker_state_updated_at_lives_in_the_broker_clock_domain():
    """It is the broker-reported snapshot timestamp on the committed history
    row -- NOT an internal commit time -- and is deliberately not ordered
    against the repair-side reservation clock."""
    entry = attempt(broker_state_updated_at_utc=at(1) - timedelta(days=30))
    assert entry.broker_state_updated_at_utc < entry.reserved_at_utc


def test_the_after_state_exists_exactly_for_applied_like_outcomes():
    with pytest.raises(ValueError, match="internal_state_after"):
        attempt(internal_state_after=None)
    with pytest.raises(ValueError, match="internal_state_after"):
        skipped(internal_state_after=OrderState.FILLED)
    already = attempt(
        outcome=RepairOutcome.ALREADY_APPLIED,
        broker_state_updated_at_utc=None,
        internal_state_before=OrderState.FILLED,
        internal_state_after=OrderState.FILLED,
    )
    assert already.internal_state_after is OrderState.FILLED


def test_the_broker_error_code_exists_exactly_for_query_failed():
    with pytest.raises(ValueError, match="broker_error_code"):
        skipped(broker_error_code=BrokerErrorCode.TIMEOUT)
    with pytest.raises(ValueError, match="broker_error_code"):
        skipped(
            outcome=RepairOutcome.QUERY_FAILED,
            failure_code=RepairFailureCode.BROKER_QUERY_FAILED,
        )
    entry = skipped(
        outcome=RepairOutcome.QUERY_FAILED,
        broker_error_code=BrokerErrorCode.TIMEOUT,
        failure_code=RepairFailureCode.BROKER_QUERY_FAILED,
    )
    assert entry.broker_error_code is BrokerErrorCode.TIMEOUT


def test_the_failure_code_is_a_closed_set_bound_to_the_outcome():
    # success outcomes carry no code
    with pytest.raises(ValueError, match="failure_code"):
        attempt(failure_code=RepairFailureCode.CLAIM_LOST)
    # non-success outcomes require one
    with pytest.raises(ValueError, match="failure_code"):
        skipped(failure_code=None)
    # raw strings are rejected (Enum only)
    with pytest.raises(ValueError):
        skipped(failure_code="not_eligible_manual_review")
    # and the code must belong to the outcome's permitted family
    with pytest.raises(ValueError, match="not permitted"):
        skipped(failure_code=RepairFailureCode.BROKER_QUERY_FAILED)
    with pytest.raises(ValueError, match="not permitted"):
        attempt(
            outcome=RepairOutcome.STALE,
            broker_state_updated_at_utc=None,
            internal_state_after=None,
            failure_code=RepairFailureCode.CLAIM_LOST,
        )
    conflict = skipped(
        outcome=RepairOutcome.CONFLICT, failure_code=RepairFailureCode.CLAIM_LOST
    )
    assert conflict.failure_code is RepairFailureCode.CLAIM_LOST


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
        failure_code=RepairFailureCode.NOT_ELIGIBLE_BROKER_ONLY,
    )
    assert entry.execution_request_id is None


def test_an_execution_target_requires_the_observed_internal_state():
    with pytest.raises(ValueError, match="internal state"):
        attempt(reconciliation_internal_state=None)


def test_audit_error_is_optional_metadata():
    entry = replace(attempt(), audit_error="claim lost; reservation stays with its new owner")
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
    dry = attempt(
        mode=RepairMode.DRY_RUN,
        outcome=RepairOutcome.WOULD_APPLY,
        owner_id=None,
        claim_execution_id=None,
        generation=None,
        broker_state_updated_at_utc=None,
        finalized_at_utc=None,
    )
    with pytest.raises(ValueError, match="mode"):
        result(mode=RepairMode.DRY_RUN, attempts=(dry, skipped(attempt_id="attempt-2")))
    with pytest.raises(ValueError, match="unique"):
        result(attempts=(skipped(), skipped()))
    with pytest.raises(ValueError, match="finished_at_utc"):
        result(finished_at_utc=T0 - timedelta(seconds=1))
    with pytest.raises(ValueError):
        result(attempts=[attempt()])


# --- durable takeover intents (re-review r4) ----------------------------------


def intent(**overrides) -> RepairTakeoverIntent:
    kwargs = dict(
        takeover_id="tk-1",
        repair_attempt_id="attempt-1",
        source_generation=1,
        target_generation=2,
        source_owner_id="worker-1",
        source_claim_execution_id="claim-1",
        target_owner_id="worker-2",
        target_claim_execution_id="claim-2",
        lease_expires_at_utc=at(700),
        status=TakeoverIntentStatus.PREPARED,
        created_at_utc=at(100),
    )
    kwargs.update(overrides)
    return RepairTakeoverIntent(**kwargs)


def test_a_prepared_intent_carries_the_identity_of_one_takeover():
    prepared = intent()
    assert prepared.is_active is True
    assert prepared.status is TakeoverIntentStatus.PREPARED
    assert "takeover=tk-1" in prepared.fence_identity()
    assert "claim=claim-2" in prepared.fence_identity()


def test_the_target_generation_is_always_one_step_forward():
    for bad in (1, 3):
        with pytest.raises(ValueError, match="source_generation \\+ 1"):
            intent(target_generation=bad)
    with pytest.raises(ValueError, match="target_generation must be an int >= 1"):
        intent(target_generation=0)
    with pytest.raises(ValueError, match="source_generation must be an int >= 1"):
        intent(source_generation=0, target_generation=1)


def test_a_takeover_must_name_a_new_execution():
    with pytest.raises(ValueError, match="must differ"):
        intent(target_claim_execution_id="claim-1")
    for field in (
        "takeover_id",
        "repair_attempt_id",
        "source_owner_id",
        "source_claim_execution_id",
        "target_owner_id",
        "target_claim_execution_id",
    ):
        with pytest.raises(ValueError):
            intent(**{field: "  "})


def test_the_lease_must_outlive_the_intents_creation():
    with pytest.raises(ValueError, match="lease_expires_at_utc"):
        intent(lease_expires_at_utc=at(100))
    with pytest.raises(ValueError, match="timezone-aware"):
        intent(created_at_utc=datetime(2026, 8, 1, 12, 0, 0))


def test_status_and_timestamps_must_agree():
    with pytest.raises(ValueError, match="fence_installed_at_utc"):
        intent(status=TakeoverIntentStatus.FENCE_INSTALLED)
    with pytest.raises(ValueError, match="claim_promoted_at_utc"):
        intent(status=TakeoverIntentStatus.CLAIM_PROMOTED, fence_installed_at_utc=at(110))
    with pytest.raises(ValueError, match="completed_at_utc"):
        intent(
            status=TakeoverIntentStatus.COMPLETED,
            fence_installed_at_utc=at(110),
            claim_promoted_at_utc=at(120),
        )
    with pytest.raises(ValueError, match="cancelled_at_utc"):
        intent(status=TakeoverIntentStatus.CANCELLED)
    with pytest.raises(ValueError, match="blocked_at_utc"):
        intent(status=TakeoverIntentStatus.BLOCKED, blocked_reason=TakeoverBlockedReason.FENCE_AHEAD)
    with pytest.raises(ValueError, match="blocked_reason"):
        intent(status=TakeoverIntentStatus.BLOCKED, blocked_at_utc=at(200))
    with pytest.raises(ValueError, match="prepared intent"):
        intent(fence_installed_at_utc=at(110))
    with pytest.raises(ValueError, match="not precede"):
        intent(
            status=TakeoverIntentStatus.CLAIM_PROMOTED,
            fence_installed_at_utc=at(120),
            claim_promoted_at_utc=at(110),
        )


def test_observed_diagnostics_belong_to_blocked_intents_only():
    with pytest.raises(ValueError, match="observed_"):
        intent(observed_fence_generation=3)
    blocked = intent(
        status=TakeoverIntentStatus.BLOCKED,
        blocked_at_utc=at(200),
        blocked_reason=TakeoverBlockedReason.FENCE_AHEAD,
        observed_claim_generation=1,
        observed_fence_generation=3,
        observed_fence_identity="attempt=attempt-1 generation=3",
    )
    assert blocked.is_active is False
    assert blocked.observed_fence_generation == 3


def test_the_intent_lifecycle_is_closed_and_forward_only():
    assert set(ALLOWED_INTENT_TRANSITIONS) == set(TakeoverIntentStatus)
    assert ACTIVE_INTENT_STATUSES == frozenset(
        {
            TakeoverIntentStatus.PREPARED,
            TakeoverIntentStatus.FENCE_INSTALLED,
            TakeoverIntentStatus.CLAIM_PROMOTED,
        }
    )
    # terminal statuses never move again
    for terminal in (
        TakeoverIntentStatus.COMPLETED,
        TakeoverIntentStatus.CANCELLED,
        TakeoverIntentStatus.BLOCKED,
    ):
        assert ALLOWED_INTENT_TRANSITIONS[terminal] == frozenset()
    # no transition ever goes backwards
    order = [
        TakeoverIntentStatus.PREPARED,
        TakeoverIntentStatus.FENCE_INSTALLED,
        TakeoverIntentStatus.CLAIM_PROMOTED,
        TakeoverIntentStatus.COMPLETED,
    ]
    for index, status in enumerate(order[:-1]):
        for target in ALLOWED_INTENT_TRANSITIONS[status]:
            assert target not in order[: index + 1]


def test_every_blocked_reason_maps_to_a_blocked_failure_code():
    assert set(BLOCKED_REASON_FAILURE_CODES) == set(TakeoverBlockedReason)
    for reason, code in BLOCKED_REASON_FAILURE_CODES.items():
        blocked_attempt = attempt(
            outcome=RepairOutcome.BLOCKED,
            failure_code=code,
            internal_state_after=None,
            broker_state_updated_at_utc=None,
            finalized_at_utc=None,
        )
        assert blocked_attempt.failure_code is code, reason


def test_a_blocked_attempt_rejects_a_foreign_failure_code():
    with pytest.raises(ValueError, match="not permitted"):
        attempt(
            outcome=RepairOutcome.BLOCKED,
            failure_code=RepairFailureCode.BROKER_QUERY_FAILED,
            internal_state_after=None,
            broker_state_updated_at_utc=None,
        )
    with pytest.raises(ValueError, match="failure_code"):
        attempt(
            outcome=RepairOutcome.BLOCKED,
            failure_code=None,
            internal_state_after=None,
            broker_state_updated_at_utc=None,
        )


def test_the_takeover_id_is_bound_to_the_generation():
    assert reservation(generation=1, takeover_id=None).takeover_id is None
    with pytest.raises(ValueError, match="original claim"):
        reservation(generation=1, takeover_id="tk-1")
    with pytest.raises(ValueError, match="must name the takeover_id"):
        reservation(generation=2, takeover_id=None)
    assert reservation(generation=2, takeover_id="tk-1").takeover_id == "tk-1"

    assert attempt(generation=2, takeover_id="tk-1").takeover_id == "tk-1"
    with pytest.raises(ValueError, match="must name the takeover_id"):
        attempt(generation=2)
    with pytest.raises(ValueError, match="dry-run attempts carry no claim"):
        attempt(
            mode=RepairMode.DRY_RUN,
            outcome=RepairOutcome.WOULD_APPLY,
            owner_id=None,
            claim_execution_id=None,
            generation=None,
            takeover_id="tk-1",
            broker_state_updated_at_utc=None,
            finalized_at_utc=None,
        )
