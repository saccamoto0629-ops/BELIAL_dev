"""Issue #116: manual reissue / gate correction eligibility, persisted caps,
policy content identity, journal preconditions and the superseded-intent
terminalization sweep.

All DBs live under pytest's tmp_path. No network, no Discord, no S3, no
sleep, no real orders anywhere in this suite.
"""

import json
import logging
from datetime import timedelta
from decimal import Decimal

import pytest

from broker.broker_models import OrderSide, OrderState, OrderType, RejectionCategory
from execution import execution_store as store
from execution.reissue_service import (
    CORRECTABLE_GATE_REJECTION_CODES,
    ReissueEligibilityError,
    ReissuePolicy,
    ReissueService,
)
from execution.safety_gate import (
    ExecutionMode,
    SafetyDecision,
    SafetyPolicy,
    SafetyRejection,
    SafetyRejectionCode,
)
from execution.safety_store import SQLiteSafetyDecisionStore
from execution.submission_journal import (
    OUTCOME_OPEN,
    OUTCOME_SUPERSEDED,
    SubmissionJournal,
)

from test_execution_store import at, create_order, make_result, submit
from test_execution_reissue import (
    child_request,
    record_clearance,
    register_child,
    reject_parent,
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "execution_orders.sqlite3"


@pytest.fixture
def journal_path(tmp_path):
    return tmp_path / "submission_journal.sqlite3"


@pytest.fixture
def safety_path(tmp_path):
    return tmp_path / "safety_gate_decisions.sqlite3"


def make_policy(policy_id="policy-1", max_order_quantity="50"):
    return SafetyPolicy(
        policy_id=policy_id,
        allowed_symbols=frozenset({"XAUUSD"}),
        allowed_sides=frozenset({OrderSide.BUY, OrderSide.SELL}),
        max_order_quantity=Decimal(max_order_quantity),
        max_positions_per_symbol=5,
        max_positions_account=10,
        max_reference_age=timedelta(seconds=60),
        max_spread_by_symbol={"XAUUSD": Decimal("1")},
        allowed_connection_targets={ExecutionMode.FAKE: frozenset({"fake"})},
    )


def make_service(db_path, journal_path, safety_path, *, policy=None, reissue_policy=None,
                 clock=None):
    SubmissionJournal(journal_path).initialize()
    SQLiteSafetyDecisionStore(safety_path).initialize()
    return ReissueService(
        execution_db_path=db_path,
        journal_db_path=journal_path,
        decision_db_path=safety_path,
        current_policy=policy or make_policy(),
        reissue_policy=reissue_policy or ReissuePolicy(),
        clock=clock or (lambda: at(30)),
    )


def record_decision(
    safety_path,
    *,
    request_id="exec-1",
    allowed=False,
    codes=(SafetyRejectionCode.QUANTITY_LIMIT_EXCEEDED,),
    policy=None,
    policy_id=None,
    snapshot=True,
    seconds=3,
):
    policy = policy or make_policy()
    decision_store = SQLiteSafetyDecisionStore(safety_path)
    decision_store.initialize()
    decision = SafetyDecision(
        execution_request_id=request_id,
        allowed=allowed,
        checked_at_utc=at(seconds),
        policy_id=policy_id or policy.policy_id,
        rejections=(
            ()
            if allowed
            else tuple(SafetyRejection(code, "recorded rejection") for code in codes)
        ),
        execution_mode="fake",
        connection_target="fake",
        account_id="acct-1",
        position_count=0,
    )
    return decision_store.record(
        decision, policy_snapshot=policy.to_snapshot() if snapshot else None
    )


def record_correction_clearance(
    db_path, signal_id="sig-exec-1", *, parent_id="exec-1",
    client_order_id="client-exec-1", confirmed=True, observed_at=None,
):
    """A valid clearance whose payload carries the broker-confirmed absence
    of the unsubmitted parent's client key (what the clearance observation
    records when it probes a created chain tail)."""
    payload = {
        "phase": "conclusion",
        "conclusion": "valid",
        "run_id": "test-run",
    }
    if confirmed is not None:
        payload["parent_absence"] = {
            "client_order_id": client_order_id,
            "execution_request_id": parent_id,
            "confirmed_absent": confirmed,
            "probed_at_utc": at(5).isoformat(),
        }
    return store.record_reissue_clearance(
        signal_id,
        evidence_id=f"ev-corr-{store.CLEARANCES_TABLE}-{parent_id}-{at(5).isoformat()}",
        status=store.CLEARANCE_STATUS_VALID,
        account_scope="oanda-demo:acct-1",
        observed_at_utc=observed_at or at(5),
        evidence_json=json.dumps(payload),
        recorded_at_utc=observed_at or at(5),
        db_path=db_path,
    )


def manual_child(request_id="exec-2", **overrides):
    return child_request("exec-1", request_id, **overrides)


# --- manual reissue -----------------------------------------------------------


def test_manual_reissue_registers_with_operator_audit(
    db_path, journal_path, safety_path
):
    reject_parent(db_path)
    record_clearance(db_path, "sig-exec-1")
    service = make_service(db_path, journal_path, safety_path)
    child = service.register_manual_reissue(
        manual_child(),
        client_order_id="client-exec-2",
        parent_execution_request_id="exec-1",
        operator_id="op-1",
        reason="transient rejection cleared",
    )
    assert child["signal_attempt"] == 2
    assert child["reissue_kind"] == "broker_rejected"
    assert child["reissue_initiated_by"] == "manual"
    assert child["reissue_reason"] == "operator op-1: transient rejection cleared"


@pytest.mark.parametrize("missing", ["operator_id", "reason"])
def test_manual_reissue_requires_operator_identity_and_reason(
    db_path, journal_path, safety_path, missing
):
    reject_parent(db_path)
    record_clearance(db_path, "sig-exec-1")
    service = make_service(db_path, journal_path, safety_path)
    kwargs = dict(operator_id="op-1", reason="why")
    kwargs[missing] = "  "
    with pytest.raises(ValueError, match=missing):
        service.register_manual_reissue(
            manual_child(),
            client_order_id="client-exec-2",
            parent_execution_request_id="exec-1",
            **kwargs,
        )


def test_manual_reissue_without_any_clearance_is_refused(
    db_path, journal_path, safety_path
):
    reject_parent(db_path)
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="no reissue clearance"):
        service.register_manual_reissue(
            manual_child(),
            client_order_id="client-exec-2",
            parent_execution_request_id="exec-1",
            operator_id="op-1",
            reason="retry",
        )


@pytest.mark.parametrize(
    "status", [store.CLEARANCE_STATUS_OBSERVING, store.CLEARANCE_STATUS_REVOKED]
)
def test_manual_reissue_under_observing_or_revoked_clearance_is_refused(
    db_path, journal_path, safety_path, status
):
    reject_parent(db_path)
    record_clearance(db_path, "sig-exec-1", status=status)
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="not 'valid'"):
        service.register_manual_reissue(
            manual_child(),
            client_order_id="client-exec-2",
            parent_execution_request_id="exec-1",
            operator_id="op-1",
            reason="retry",
        )


def test_manual_reissue_under_a_stale_clearance_is_refused(
    db_path, journal_path, safety_path
):
    reject_parent(db_path)
    record_clearance(db_path, "sig-exec-1", observed_at=at(5))
    service = make_service(
        db_path, journal_path, safety_path, clock=lambda: at(5 + 301)
    )  # default window 5 minutes
    with pytest.raises(ReissueEligibilityError, match="older than the freshness window"):
        service.register_manual_reissue(
            manual_child(),
            client_order_id="client-exec-2",
            parent_execution_request_id="exec-1",
            operator_id="op-1",
            reason="retry",
        )


def test_manual_reissue_of_a_non_tail_parent_is_refused(
    db_path, journal_path, safety_path
):
    reject_parent(db_path)
    register_child(db_path)  # exec-2 becomes the tail
    record_clearance(db_path, "sig-exec-1")
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="not the chain tail"):
        service.register_manual_reissue(
            child_request("exec-1", "exec-3"),
            client_order_id="client-exec-3",
            parent_execution_request_id="exec-1",
            operator_id="op-1",
            reason="retry",
        )


def test_manual_reissue_of_a_non_rejected_parent_is_refused(
    db_path, journal_path, safety_path
):
    create_order(db_path)  # created, never submitted
    record_clearance(db_path, "sig-exec-1")
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="order_state 'rejected'"):
        service.register_manual_reissue(
            manual_child(),
            client_order_id="client-exec-2",
            parent_execution_request_id="exec-1",
            operator_id="op-1",
            reason="retry",
        )


def test_reused_client_order_id_is_structurally_refused(
    db_path, journal_path, safety_path
):
    """client_order_id は常に新規発行: reusing the parent's broker key is
    refused by the store's UNIQUE constraint through the service."""
    reject_parent(db_path)
    record_clearance(db_path, "sig-exec-1")
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(store.ExecutionOrderConflictError):
        service.register_manual_reissue(
            manual_child(),
            client_order_id="client-exec-1",  # the parent's key
            parent_execution_request_id="exec-1",
            operator_id="op-1",
            reason="retry",
        )
    assert store.get_execution_order("exec-2", db_path=db_path) is None


# --- quantity increase (parameter rejections only, ratio-capped) -------------


def reject_parent_with(db_path, category):
    return reject_parent(db_path, category=category)


def test_parameter_rejection_allows_a_ratio_capped_increase(
    db_path, journal_path, safety_path
):
    reject_parent_with(db_path, RejectionCategory.PARAMETER)
    record_clearance(db_path, "sig-exec-1")
    service = make_service(db_path, journal_path, safety_path)
    child = service.register_manual_reissue(
        manual_child(quantity=Decimal("15")),  # parent 10, ratio cap 2 -> max 20
        client_order_id="client-exec-2",
        parent_execution_request_id="exec-1",
        operator_id="op-1",
        reason="minimum size correction",
    )
    assert Decimal(child["quantity"]) == Decimal("15")


def test_parameter_increase_above_the_ratio_cap_is_refused(
    db_path, journal_path, safety_path
):
    reject_parent_with(db_path, RejectionCategory.PARAMETER)
    record_clearance(db_path, "sig-exec-1")
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="policy increase cap"):
        service.register_manual_reissue(
            manual_child(quantity=Decimal("25")),
            client_order_id="client-exec-2",
            parent_execution_request_id="exec-1",
            operator_id="op-1",
            reason="too much",
        )


@pytest.mark.parametrize(
    "category",
    [
        RejectionCategory.TRANSIENT,
        RejectionCategory.ACCOUNT,
        RejectionCategory.INSTRUMENT,
        RejectionCategory.BROKER_INTERNAL,
        RejectionCategory.UNCLASSIFIED,
    ],
)
def test_non_parameter_rejections_never_allow_an_increase(
    db_path, journal_path, safety_path, category
):
    reject_parent_with(db_path, category)
    record_clearance(db_path, "sig-exec-1")
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="rejection_category 'parameter'"):
        service.register_manual_reissue(
            manual_child(quantity=Decimal("11")),
            client_order_id="client-exec-2",
            parent_execution_request_id="exec-1",
            operator_id="op-1",
            reason="increase",
        )


def test_gate_correction_never_allows_an_increase(db_path, journal_path, safety_path):
    create_order(db_path)
    record_decision(safety_path)
    record_correction_clearance(db_path)
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ValueError, match="exceeds the parent"):
        service.register_gate_correction(
            manual_child(quantity=Decimal("11")),
            client_order_id="client-exec-2",
            parent_execution_request_id="exec-1",
            operator_id="op-1",
            reason="increase must be refused",
        )


# --- caps from persisted rows -------------------------------------------------


def test_manual_cap_is_derived_from_persisted_rows_across_restarts(
    db_path, journal_path, safety_path
):
    reject_parent(db_path)
    register_child(db_path)  # 1 persisted manual reissue (exec-2)
    submitted = store.mark_submitting(
        "exec-2", reason="reissue submit", changed_at_utc=at(20),
        clearance_scope="oanda-demo:acct-1", clearance_observed_after_utc=at(0),
        db_path=db_path,
    )
    assert submitted["submission_phase"] == "submitting"
    store.record_order_result(
        "exec-2",
        make_result(
            OrderState.REJECTED, "exec-2", broker_order_id=None,
            reject_reason="rejected again",
            rejection_category=RejectionCategory.TRANSIENT, updated_at=at(21),
        ),
        reason="broker rejected",
        db_path=db_path,
    )
    record_clearance(db_path, "sig-exec-1")
    policy = ReissuePolicy(max_manual_reissues_per_signal=1)
    # a FRESH service instance (process restart equivalence): the cap is
    # derived from the chain rows, never from in-memory counters
    service = make_service(db_path, journal_path, safety_path, reissue_policy=policy)
    with pytest.raises(ReissueEligibilityError, match="manual reissue cap"):
        service.register_manual_reissue(
            child_request("exec-1", "exec-3", requested_at_utc=at(25)),
            client_order_id="client-exec-3",
            parent_execution_request_id="exec-2",
            operator_id="op-1",
            reason="second retry",
        )


def test_total_attempt_cap_is_enforced(db_path, journal_path, safety_path):
    reject_parent(db_path)
    record_clearance(db_path, "sig-exec-1")
    policy = ReissuePolicy(max_total_attempts_per_signal=1)
    service = make_service(db_path, journal_path, safety_path, reissue_policy=policy)
    with pytest.raises(ReissueEligibilityError, match="total attempt cap"):
        service.register_manual_reissue(
            manual_child(),
            client_order_id="client-exec-2",
            parent_execution_request_id="exec-1",
            operator_id="op-1",
            reason="retry",
        )


# --- gate correction ----------------------------------------------------------


def correction_child(request_id="exec-2", **overrides):
    overrides.setdefault("quantity", Decimal("5"))
    return child_request("exec-1", request_id, **overrides)


def register_correction(service, **overrides):
    kwargs = dict(
        client_order_id="client-exec-2",
        parent_execution_request_id="exec-1",
        operator_id="op-1",
        reason="reduce quantity",
    )
    kwargs.update(overrides)
    return service.register_gate_correction(correction_child(), **kwargs)


def test_gate_correction_succeeds_and_terminalizes_the_parent_intent(
    db_path, journal_path, safety_path
):
    create_order(db_path)
    record_decision(safety_path)
    record_correction_clearance(db_path)
    journal = SubmissionJournal(journal_path)
    journal.initialize()
    # the parent's journaled, provably-unsent intent (e.g. from a submit
    # that failed before the broker call)
    journal.record_submission_intent(
        execution_request_id="exec-1", client_order_id="client-exec-1",
        symbol="XAUUSD", side=OrderSide.BUY, order_type=OrderType.LIMIT,
        quantity=Decimal("10"), requested_price=Decimal("2400.00"),
        execution_mode="fake", connection_target="fake", started_at_utc=at(2),
    )
    service = make_service(db_path, journal_path, safety_path)
    child = service.register_gate_correction(
        correction_child(),
        client_order_id="client-exec-2",
        parent_execution_request_id="exec-1",
        operator_id="op-1",
        reason="reduce quantity 10 -> 5",
    )
    assert child["reissue_kind"] == "gate_correction"
    assert child["gate_correction_ref_json"] is not None
    entry = journal.get_entry("exec-1")
    assert entry.outcome == OUTCOME_SUPERSEDED
    assert entry.superseded_by_execution_request_id == "exec-2"
    assert all(e.execution_request_id != "exec-1" for e in journal.list_unresolved())


def test_gate_correction_without_any_decision_is_refused(
    db_path, journal_path, safety_path
):
    create_order(db_path)
    record_correction_clearance(db_path)
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="no persisted Safety Gate decision"):
        register_correction(service)


def test_gate_correction_when_the_latest_decision_allowed_is_refused(
    db_path, journal_path, safety_path
):
    create_order(db_path)
    record_decision(safety_path)  # older rejection
    record_decision(safety_path, allowed=True, seconds=4)  # latest: allowed
    record_correction_clearance(db_path)
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="ALLOWED the order"):
        register_correction(service)


def test_gate_correction_without_a_correctable_code_is_refused(
    db_path, journal_path, safety_path
):
    create_order(db_path)
    record_decision(safety_path, codes=(SafetyRejectionCode.MARKET_CLOSED,))
    record_correction_clearance(db_path)
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="no correctable rejection code"):
        register_correction(service)


def test_gate_correction_under_a_different_policy_id_is_refused(
    db_path, journal_path, safety_path
):
    create_order(db_path)
    record_decision(safety_path, policy_id="policy-OLD")
    record_correction_clearance(db_path)
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="must be current"):
        register_correction(service)


def test_gate_correction_with_same_policy_id_but_changed_content_is_refused(
    db_path, journal_path, safety_path
):
    """policy_id だけでは policy を識別しない: the SAME id with different
    CONTENT must fail closed."""
    create_order(db_path)
    old_content = make_policy(policy_id="policy-1", max_order_quantity="10")
    record_decision(safety_path, policy=old_content)
    record_correction_clearance(db_path)
    current = make_policy(policy_id="policy-1", max_order_quantity="50")
    service = make_service(db_path, journal_path, safety_path, policy=current)
    with pytest.raises(ReissueEligibilityError, match="CONTENT differs"):
        register_correction(service)


def test_gate_correction_without_a_stored_policy_snapshot_is_refused(
    db_path, journal_path, safety_path
):
    create_order(db_path)
    record_decision(safety_path, snapshot=False)
    record_correction_clearance(db_path)
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="no policy content snapshot"):
        register_correction(service)


def test_gate_correction_with_a_journaled_started_intent_is_refused(
    db_path, journal_path, safety_path
):
    """Journal 事前条件は Broker 照会と併用される必要条件: an entry past
    not_started (the broker may hold the order -- e.g. the execution DB was
    re-created around a journaled attempt) refuses the correction."""
    create_order(db_path)
    record_decision(safety_path)
    record_correction_clearance(db_path)
    from broker.broker_models import OrderType

    journal = SubmissionJournal(journal_path)
    journal.initialize()
    journal.record_submission_intent(
        execution_request_id="exec-1", client_order_id="client-exec-1",
        symbol="XAUUSD", side=OrderSide.BUY, order_type=OrderType.LIMIT,
        quantity=Decimal("10"), requested_price=Decimal("2400.00"),
        execution_mode="fake", connection_target="fake", started_at_utc=at(2),
    )
    journal.mark_broker_call_started("exec-1", at_utc=at(3))
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="broker may hold this order"):
        register_correction(service)


def test_gate_correction_without_parent_absence_evidence_is_refused(
    db_path, journal_path, safety_path
):
    """Journal not_started は十分条件ではない: a clearance whose payload
    carries no broker-confirmed absence of the parent's client key refuses
    the correction (positive evidence required)."""
    create_order(db_path)
    record_decision(safety_path)
    record_clearance(db_path, "sig-exec-1")  # valid but no parent_absence
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="broker-confirmed absence"):
        register_correction(service)


def test_gate_correction_with_absence_for_a_different_key_is_refused(
    db_path, journal_path, safety_path
):
    create_order(db_path)
    record_decision(safety_path)
    record_correction_clearance(db_path, client_order_id="client-other")
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="broker-confirmed absence"):
        register_correction(service)


def test_gate_correction_of_a_submitted_parent_is_refused(
    db_path, journal_path, safety_path
):
    submit(db_path)  # parent gains submission authority -> phase submitting
    record_decision(safety_path)
    record_correction_clearance(db_path)
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="never submitted"):
        register_correction(service)


# --- superseded-intent terminalization sweep ----------------------------------


def test_sweep_terminalizes_a_late_journaled_intent(db_path, journal_path, safety_path):
    """競合で遅れて作成された intent の取りこぼし防止: the parent's submit
    journaled its intent and only then lost to the supersession -- the
    sweep converges the entry to the terminal outcome."""
    create_order(db_path)
    safety_dir = journal_path.parent
    from test_execution_reissue import gate_reference

    gate_db, reference = gate_reference(safety_dir)
    register_child(
        db_path,
        kind=store.REISSUE_KIND_GATE_CORRECTION,
        reason="operator op-1: reduce quantity",
        gate_correction_reference=reference,
        gate_decision_db_path=gate_db,
        quantity=Decimal("5"),
    )
    from broker.broker_models import OrderType

    journal = SubmissionJournal(journal_path)
    journal.initialize()
    # the LATE intent: journaled after the supersession committed
    journal.record_submission_intent(
        execution_request_id="exec-1", client_order_id="client-exec-1",
        symbol="XAUUSD", side=OrderSide.BUY, order_type=OrderType.LIMIT,
        quantity=Decimal("10"), requested_price=Decimal("2400.00"),
        execution_mode="fake", connection_target="fake", started_at_utc=at(15),
    )
    service = make_service(db_path, journal_path, safety_path)
    actions = service.terminalize_superseded_intents()
    assert [(a["execution_request_id"], a["action"]) for a in actions] == [
        ("exec-1", "superseded")
    ]
    entry = journal.get_entry("exec-1")
    assert entry.outcome == OUTCOME_SUPERSEDED
    assert entry.superseded_by_execution_request_id == "exec-2"
    assert journal.list_unresolved() == ()
    # idempotent re-run (crash recovery)
    actions = service.terminalize_superseded_intents()
    assert actions[0]["action"] == "already_superseded"


def test_sweep_never_touches_a_possibly_sent_intent(db_path, journal_path, safety_path):
    """Broker 到達可能性がある intent は終端化しない: a journal entry past
    not_started stays untouched and stays visible as unresolved."""
    reject_parent(db_path)
    register_child(db_path)  # a broker_rejected reissue -> marker exists
    from broker.broker_models import OrderType

    journal = SubmissionJournal(journal_path)
    journal.initialize()
    journal.record_submission_intent(
        execution_request_id="exec-1", client_order_id="client-exec-1",
        symbol="XAUUSD", side=OrderSide.BUY, order_type=OrderType.LIMIT,
        quantity=Decimal("10"), requested_price=Decimal("2400.00"),
        execution_mode="fake", connection_target="fake", started_at_utc=at(2),
    )
    journal.mark_broker_call_started("exec-1", at_utc=at(3))
    service = make_service(db_path, journal_path, safety_path)
    actions = service.terminalize_superseded_intents()
    assert actions[0]["action"] == "not_terminalizable"
    entry = journal.get_entry("exec-1")
    assert entry.outcome == OUTCOME_OPEN
    assert [e.execution_request_id for e in journal.list_unresolved()] == ["exec-1"]


def test_sweep_with_no_journal_entry_reports_and_continues(
    db_path, journal_path, safety_path
):
    reject_parent(db_path)
    register_child(db_path)
    service = make_service(db_path, journal_path, safety_path)
    actions = service.terminalize_superseded_intents()
    assert actions[0]["action"] == "no_journal_entry"


# --- correctable-code closure regression -------------------------------------


def test_correctable_code_set_matches_the_field_invariant_closure():
    """閉包条件: the eligible rejection codes are EXACTLY those resolvable by
    changing only the child-mutable request fields; identity-bound
    (policy-bound) codes are excluded."""
    correctable_field_by_code = {
        SafetyRejectionCode.QUANTITY_LIMIT_EXCEEDED: "quantity",
        SafetyRejectionCode.STOP_LOSS_REQUIRED: "stop_loss_price",
        SafetyRejectionCode.STOP_LOSS_INVALID: "stop_loss_price",
        SafetyRejectionCode.TAKE_PROFIT_INVALID: "take_profit_price",
    }
    assert frozenset(correctable_field_by_code) == CORRECTABLE_GATE_REJECTION_CODES
    child_mutable_fields = {
        "quantity",
        "order_type",
        "requested_price",
        "stop_loss_price",
        "take_profit_price",
    }
    assert set(correctable_field_by_code.values()) <= child_mutable_fields
    assert not CORRECTABLE_GATE_REJECTION_CODES & {
        SafetyRejectionCode.SYMBOL_NOT_ALLOWED,
        SafetyRejectionCode.SIDE_NOT_ALLOWED,
    }


# --- PR #122 Codex review: concurrency between pre-check and registration ----


@pytest.mark.parametrize(
    "status", [store.CLEARANCE_STATUS_OBSERVING, store.CLEARANCE_STATUS_REVOKED]
)
def test_a_concurrent_observation_refuses_the_manual_registration(
    db_path, journal_path, safety_path, monkeypatch, status
):
    """観測前の clearance invalidation が登録にも反映される: an observer
    that claims (``observing``) or revokes the signal AFTER the service's
    pre-check but BEFORE the child INSERT refuses the registration -- the
    parent is not retired on evidence a newer observation invalidated."""
    reject_parent(db_path)
    record_clearance(db_path, "sig-exec-1")
    service = make_service(db_path, journal_path, safety_path)
    real_create = store.create_reissue_execution_order

    def concurrent_observation(*args, **kwargs):
        record_clearance(db_path, "sig-exec-1", status=status)
        return real_create(*args, **kwargs)

    monkeypatch.setattr(
        "execution.reissue_service.execution_store.create_reissue_execution_order",
        concurrent_observation,
    )
    with pytest.raises(store.ExecutionClearanceError):
        service.register_manual_reissue(
            manual_child(),
            client_order_id="client-exec-2",
            parent_execution_request_id="exec-1",
            operator_id="op-1",
            reason="retry",
        )
    assert store.get_execution_order("exec-2", db_path=db_path) is None
    assert store.list_intent_supersessions(db_path=db_path) == []


def test_a_concurrent_observation_refuses_the_gate_correction_and_keeps_the_parent(
    db_path, journal_path, safety_path, monkeypatch
):
    """The same protection on the correction path, where the registration
    would additionally retire the parent and terminalize its intent: a
    concurrent hazard observation leaves the parent live and its journal
    intent untouched."""
    create_order(db_path)
    record_decision(safety_path)
    record_correction_clearance(db_path)
    journal = SubmissionJournal(journal_path)
    journal.initialize()
    journal.record_submission_intent(
        execution_request_id="exec-1", client_order_id="client-exec-1",
        symbol="XAUUSD", side=OrderSide.BUY, order_type=OrderType.LIMIT,
        quantity=Decimal("10"), requested_price=Decimal("2400.00"),
        execution_mode="fake", connection_target="fake", started_at_utc=at(2),
    )
    service = make_service(db_path, journal_path, safety_path)
    real_create = store.create_reissue_execution_order

    def concurrent_hazard(*args, **kwargs):
        store.record_reissue_clearance(
            "sig-exec-1",
            evidence_id="ev-hazard",
            status=store.CLEARANCE_STATUS_REVOKED,
            account_scope="oanda-demo:acct-1",
            observed_at_utc=at(6),
            evidence_json=json.dumps({"hazards": ["broker holds an unexplained order"]}),
            recorded_at_utc=at(6),
            db_path=db_path,
        )
        return real_create(*args, **kwargs)

    monkeypatch.setattr(
        "execution.reissue_service.execution_store.create_reissue_execution_order",
        concurrent_hazard,
    )
    with pytest.raises(store.ExecutionClearanceError):
        register_correction(service)

    assert store.get_execution_order("exec-2", db_path=db_path) is None
    assert store.list_intent_supersessions(db_path=db_path) == []
    parent = store.get_execution_order("exec-1", db_path=db_path)
    assert parent["submission_phase"] == "created"  # still live, not retired
    assert journal.get_entry("exec-1").outcome == OUTCOME_OPEN  # not terminalized


def test_registration_enforces_the_policy_freshness_window_in_the_transaction(
    db_path, journal_path, safety_path, monkeypatch
):
    """The cutoff handed to the store is the POLICY window, so evidence that
    ages past it between the pre-check and the INSERT is refused there too."""
    reject_parent(db_path)
    record_clearance(db_path, "sig-exec-1", observed_at=at(5))
    policy = ReissuePolicy(clearance_max_age=timedelta(seconds=30))
    captured: dict = {}
    real_create = store.create_reissue_execution_order

    def capture(*args, **kwargs):
        captured.update(kwargs)
        return real_create(*args, **kwargs)

    monkeypatch.setattr(
        "execution.reissue_service.execution_store.create_reissue_execution_order", capture
    )
    service = make_service(
        db_path, journal_path, safety_path, reissue_policy=policy, clock=lambda: at(20)
    )
    service.register_manual_reissue(
        manual_child(),
        client_order_id="client-exec-2",
        parent_execution_request_id="exec-1",
        operator_id="op-1",
        reason="retry",
    )
    assert captured["clearance_observed_after_utc"] == at(20) - timedelta(seconds=30)


# --- PR #122 Codex review: Issue #95 decision-read narrowing -----------------


def test_a_concurrently_recorded_newer_decision_is_warned_not_silently_accepted(
    db_path, journal_path, safety_path, monkeypatch, caplog
):
    """Issue #95 deliberately does NOT fence the decision store (it would
    hold this store's write lock across another DB file while submit()
    performs broker queries). Its sanctioned closure is the window
    narrowing plus a post-commit re-read that makes the residual
    machine-detectable: the registration stands (the child is re-evaluated
    by the Gate on its own submit and the parent can never submit), and the
    skew is WARN-logged."""
    create_order(db_path)
    basis_id = record_decision(safety_path)
    record_correction_clearance(db_path)
    service = make_service(db_path, journal_path, safety_path)
    real_create = store.create_reissue_execution_order

    def concurrent_allow(*args, **kwargs):
        # a concurrent submit() persists a NEWER (allowed) decision AFTER
        # the basis was read but BEFORE the child INSERT commits
        record_decision(safety_path, allowed=True, seconds=4)
        return real_create(*args, **kwargs)

    monkeypatch.setattr(
        "execution.reissue_service.execution_store.create_reissue_execution_order",
        concurrent_allow,
    )
    with caplog.at_level(logging.WARNING, logger="execution.reissue_service"):
        child = register_correction(service)
    monkeypatch.undo()
    assert basis_id is not None

    assert child["signal_attempt"] == 2  # the registration stands
    assert any(
        "no longer the parent's latest Safety Gate decision" in record.getMessage()
        for record in caplog.records
    )
    # the safety argument holds: the parent can never gain submission authority
    with pytest.raises(store.ExecutionOrderSupersededError):
        store.mark_submitting(
            "exec-1", reason="must fail", changed_at_utc=at(40), db_path=db_path
        )


def test_no_warning_when_the_correction_basis_is_still_the_latest(
    db_path, journal_path, safety_path, caplog
):
    create_order(db_path)
    record_decision(safety_path)
    record_correction_clearance(db_path)
    service = make_service(db_path, journal_path, safety_path)
    with caplog.at_level(logging.WARNING, logger="execution.reissue_service"):
        register_correction(service)
    assert not [
        record
        for record in caplog.records
        if "no longer the parent's latest" in record.getMessage()
    ]
