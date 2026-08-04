"""Issue #103: the broker reconciliation common models.

Pure in-memory model tests: no DB, no broker, no network anywhere.
"""

from datetime import datetime, timedelta, timezone

import pytest

from broker.broker_models import BrokerErrorCode, OrderState
from execution.reconciliation_models import (
    DiscrepancyType,
    OrderDiscrepancy,
    ReconciliationMode,
    ReconciliationRun,
    ReconciliationTargetType,
    ResolutionCategory,
)

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def discrepancy(**overrides) -> OrderDiscrepancy:
    kwargs = dict(
        discrepancy_type=DiscrepancyType.IN_FLIGHT_BROKER_FILLED,
        target_type=ReconciliationTargetType.EXECUTION_ORDER,
        target_id="exec-1",
        resolution_category=ResolutionCategory.AUTO_REPAIR_CANDIDATE,
        observed_at_utc=T0,
        detail="broker reports filled",
        internal_state=OrderState.PENDING,
        broker_state=OrderState.FILLED,
        client_order_id="client-1",
        broker_order_id="B-1",
    )
    kwargs.update(overrides)
    return OrderDiscrepancy(**kwargs)


def run(**overrides) -> ReconciliationRun:
    kwargs = dict(
        run_id="run-1",
        mode=ReconciliationMode.DETECTION_ONLY,
        started_at_utc=T0,
        finished_at_utc=T0 + timedelta(seconds=1),
        checked_order_count=1,
        discrepancies=(discrepancy(),),
    )
    kwargs.update(overrides)
    return ReconciliationRun(**kwargs)


# --- the classification vocabularies are pinned -------------------------------


def test_discrepancy_types_cover_the_issue_103_cases():
    assert {t.value for t in DiscrepancyType} == {
        "in_flight_broker_filled",
        "in_flight_broker_cancelled",
        "in_flight_broker_rejected",
        "unknown_broker_settled",
        "broker_only_order",
        "broker_order_missing",
        "state_mismatch",
        "query_failure",
    }


def test_resolution_categories_separate_auto_manual_and_query_failure():
    assert {c.value for c in ResolutionCategory} == {
        "auto_repair_candidate",
        "manual_review",
        "query_failed",
    }


def test_target_types_and_the_single_detection_only_mode():
    assert {t.value for t in ReconciliationTargetType} == {"execution_order", "broker_order"}
    assert [m.value for m in ReconciliationMode] == ["detection_only"]


# --- discrepancy invariants ---------------------------------------------------


def test_a_discrepancy_records_both_observed_states_and_the_reason():
    entry = discrepancy()
    assert entry.internal_state is OrderState.PENDING
    assert entry.broker_state is OrderState.FILLED
    assert entry.target_id == "exec-1"
    assert entry.detail


@pytest.mark.parametrize(
    "overrides",
    [
        {"discrepancy_type": "in_flight_broker_filled"},  # raw string instead of the enum
        {"target_type": "execution_order"},
        {"resolution_category": "manual_review"},
        {"target_id": "  "},
        {"detail": ""},
        {"observed_at_utc": datetime(2026, 7, 20, 12, 0, 0)},  # naive
        {"observed_at_utc": T0.astimezone(timezone(timedelta(hours=9)))},  # not UTC
        {"internal_state": "pending"},
        {"broker_state": "filled"},
        {"client_order_id": ""},
        {"broker_order_id": " "},
    ],
)
def test_malformed_discrepancy_fields_are_rejected(overrides):
    with pytest.raises(ValueError):
        discrepancy(**overrides)


def test_a_broker_only_order_targets_the_broker_id_space_and_has_no_internal_state():
    entry = discrepancy(
        discrepancy_type=DiscrepancyType.BROKER_ONLY_ORDER,
        target_type=ReconciliationTargetType.BROKER_ORDER,
        target_id="client-9",
        resolution_category=ResolutionCategory.MANUAL_REVIEW,
        internal_state=None,
    )
    assert entry.target_type is ReconciliationTargetType.BROKER_ORDER
    with pytest.raises(ValueError, match="internal_state must be None"):
        discrepancy(
            discrepancy_type=DiscrepancyType.BROKER_ONLY_ORDER,
            target_type=ReconciliationTargetType.BROKER_ORDER,
            resolution_category=ResolutionCategory.MANUAL_REVIEW,
            internal_state=OrderState.PENDING,
        )


def test_a_broker_only_order_cannot_claim_the_execution_order_id_space():
    with pytest.raises(ValueError, match="targets 'broker_order'"):
        discrepancy(
            discrepancy_type=DiscrepancyType.BROKER_ONLY_ORDER,
            resolution_category=ResolutionCategory.MANUAL_REVIEW,
            internal_state=None,
        )


def test_a_missing_broker_order_observes_no_broker_state():
    entry = discrepancy(
        discrepancy_type=DiscrepancyType.BROKER_ORDER_MISSING,
        resolution_category=ResolutionCategory.MANUAL_REVIEW,
        broker_state=None,
    )
    assert entry.broker_state is None
    with pytest.raises(ValueError, match="broker_state must be None"):
        discrepancy(
            discrepancy_type=DiscrepancyType.BROKER_ORDER_MISSING,
            resolution_category=ResolutionCategory.MANUAL_REVIEW,
        )


def test_a_state_discrepancy_requires_both_observed_states():
    with pytest.raises(ValueError, match="requires the observed broker_state"):
        discrepancy(broker_state=None)
    with pytest.raises(ValueError, match="requires the observed internal_state"):
        discrepancy(internal_state=None)


def test_query_failed_is_used_exactly_for_query_failures():
    entry = discrepancy(
        discrepancy_type=DiscrepancyType.QUERY_FAILURE,
        resolution_category=ResolutionCategory.QUERY_FAILED,
        broker_state=None,
        broker_error_code=BrokerErrorCode.TIMEOUT,
    )
    assert entry.broker_error_code is BrokerErrorCode.TIMEOUT
    # category without the matching kind ...
    with pytest.raises(ValueError, match="query_failed"):
        discrepancy(resolution_category=ResolutionCategory.QUERY_FAILED)
    # ... and the kind without the matching category
    with pytest.raises(ValueError, match="query_failed"):
        discrepancy(
            discrepancy_type=DiscrepancyType.QUERY_FAILURE,
            resolution_category=ResolutionCategory.MANUAL_REVIEW,
            broker_state=None,
            broker_error_code=BrokerErrorCode.TIMEOUT,
        )


def test_a_query_failure_carries_the_typed_error_code_and_no_broker_state():
    with pytest.raises(ValueError, match="requires the broker_error_code"):
        discrepancy(
            discrepancy_type=DiscrepancyType.QUERY_FAILURE,
            resolution_category=ResolutionCategory.QUERY_FAILED,
            broker_state=None,
        )
    with pytest.raises(ValueError, match="observed no broker state"):
        discrepancy(
            discrepancy_type=DiscrepancyType.QUERY_FAILURE,
            resolution_category=ResolutionCategory.QUERY_FAILED,
            broker_error_code=BrokerErrorCode.TIMEOUT,
        )


def test_only_a_query_failure_carries_an_error_code():
    with pytest.raises(ValueError, match="belongs to a query_failure only"):
        discrepancy(broker_error_code=BrokerErrorCode.COMMUNICATION_FAILURE)


# --- run invariants -----------------------------------------------------------


def test_a_run_reports_its_mode_counts_and_categories():
    manual = discrepancy(
        discrepancy_type=DiscrepancyType.STATE_MISMATCH,
        resolution_category=ResolutionCategory.MANUAL_REVIEW,
    )
    result = run(checked_order_count=3, discrepancies=(discrepancy(), manual))
    assert result.mode is ReconciliationMode.DETECTION_ONLY
    assert result.checked_order_count == 3
    assert result.by_resolution(ResolutionCategory.MANUAL_REVIEW) == (manual,)
    assert result.resolution_counts() == {
        ResolutionCategory.AUTO_REPAIR_CANDIDATE: 1,
        ResolutionCategory.MANUAL_REVIEW: 1,
        ResolutionCategory.QUERY_FAILED: 0,
    }


def test_broker_orders_enumerated_distinguishes_none_found_from_not_checked():
    assert run().broker_orders_enumerated is True
    assert run(broker_listing_error="broker cannot enumerate").broker_orders_enumerated is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_id": ""},
        {"mode": "detection_only"},
        {"started_at_utc": datetime(2026, 7, 20, tzinfo=None)},
        {"finished_at_utc": T0 - timedelta(seconds=1)},  # finished before started
        {"checked_order_count": -1},
        {"checked_order_count": True},  # bool is not a count
        {"checked_order_count": "1"},
        {"discrepancies": [discrepancy()]},  # list instead of tuple
        {"discrepancies": ("not-a-discrepancy",)},
        {"broker_listing_error": "   "},
    ],
)
def test_malformed_run_fields_are_rejected(overrides):
    with pytest.raises(ValueError):
        run(**overrides)


def test_by_resolution_requires_a_category_member():
    with pytest.raises(ValueError, match="ResolutionCategory"):
        run().by_resolution("manual_review")
