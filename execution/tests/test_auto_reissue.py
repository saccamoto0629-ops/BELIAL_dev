"""Issue #117: policy-gated automatic reissue of transient broker
rejections -- disabled by default, fail-closed on every precondition
(category, kill switch, clearance, parameter identity, cooldown, caps),
persisted-row derived counting and the audit record.

All DBs live under pytest's tmp_path; the broker is always FakeBroker. No
network, no Discord, no S3, no sleep, no real orders anywhere in this suite.
"""

import logging
from datetime import timedelta
from decimal import Decimal

import pytest

from broker.broker_models import OrderState, OrderType, RejectionCategory
from broker.fake_broker import FakeBroker
from execution import execution_store as store
from execution.reissue_service import (
    ReissueEligibilityError,
    ReissuePolicy,
    ReissueService,
)
from execution.safety_gate import BrokerConnection, SafetyGate, StaticSafetyState
from execution.safety_store import SQLiteSafetyDecisionStore
from execution.submission_journal import SubmissionJournal

from test_execution_store import at, make_result, submit
from test_execution_reissue import child_request, record_clearance, reject_parent
from test_reissue_service import make_policy

_UNSET = object()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "execution_orders.sqlite3"


@pytest.fixture
def journal_path(tmp_path):
    return tmp_path / "submission_journal.sqlite3"


@pytest.fixture
def safety_path(tmp_path):
    return tmp_path / "safety_gate_decisions.sqlite3"


def auto_policy(**overrides):
    """An EXPLICITLY opted-in policy: named, cap raised above the 0 default,
    short cooldown so the suite's fixed clocks sit past it."""
    kwargs = dict(
        policy_id="auto-policy-1",
        max_auto_reissues_per_signal=1,
        auto_reissue_cooldown=timedelta(seconds=10),
    )
    kwargs.update(overrides)
    return ReissuePolicy(**kwargs)


def make_service(
    db_path,
    journal_path,
    safety_path,
    *,
    reissue_policy=None,
    clock=None,
    state=_UNSET,
):
    SubmissionJournal(journal_path).initialize()
    SQLiteSafetyDecisionStore(safety_path).initialize()
    return ReissueService(
        execution_db_path=db_path,
        journal_db_path=journal_path,
        decision_db_path=safety_path,
        current_policy=make_policy(),
        reissue_policy=reissue_policy if reissue_policy is not None else auto_policy(),
        clock=clock or (lambda: at(30)),
        state_provider=StaticSafetyState() if state is _UNSET else state,
    )


def register_auto(service, child_id="exec-2", parent_id="exec-1", **overrides):
    # The signal always derives from the chain ROOT ("exec-1"); parent_id
    # names the tail being reissued (attempt >= 2 for longer chains).
    return service.register_auto_reissue(
        child_request("exec-1", child_id, **overrides),
        client_order_id=f"client-{child_id}",
        parent_execution_request_id=parent_id,
    )


def chain_length(db_path, signal_id="sig-exec-1"):
    return len(store.list_execution_order_chain(signal_id, db_path=db_path))


def eligible_parent(db_path):
    """A fully eligible situation: transient-rejected parent + current valid
    clearance. Every refusal test starts from here and flips ONE condition."""
    reject_parent(db_path)  # category defaults to TRANSIENT
    record_clearance(db_path, "sig-exec-1")


def reject_child_and_clear(db_path, child_id="exec-2", *, seconds=60):
    """Drive a registered auto child to a transient REJECTED tail and grant a
    fresh clearance, so the NEXT auto attempt is eligible again."""
    store.mark_submitting(
        child_id,
        reason="submit attempt",
        changed_at_utc=at(seconds - 3),
        clearance_scope="oanda-demo:acct-1",
        clearance_observed_after_utc=at(0),
        db_path=db_path,
    )
    store.record_order_result(
        child_id,
        make_result(
            OrderState.REJECTED,
            child_id,
            broker_order_id=None,
            reject_reason="rejected by broker",
            rejection_category=RejectionCategory.TRANSIENT,
            updated_at=at(seconds),
        ),
        reason="broker rejected",
        db_path=db_path,
    )
    record_clearance(db_path, "sig-exec-1", observed_at=at(seconds + 1))


# --- 1. disabled by default ---------------------------------------------------


def test_the_default_policy_refuses_a_fully_eligible_auto_reissue(
    db_path, journal_path, safety_path
):
    """The core Issue #117 contract: with the DEFAULT policy nothing is ever
    auto-reissued, even when every other precondition holds."""
    eligible_parent(db_path)
    service = make_service(
        db_path, journal_path, safety_path, reissue_policy=ReissuePolicy()
    )
    with pytest.raises(ReissueEligibilityError, match="disabled"):
        register_auto(service)
    assert chain_length(db_path) == 1


def test_a_named_policy_with_the_default_cap_still_refuses(
    db_path, journal_path, safety_path
):
    eligible_parent(db_path)
    service = make_service(
        db_path,
        journal_path,
        safety_path,
        reissue_policy=ReissuePolicy(policy_id="auto-policy-1"),
    )
    with pytest.raises(ReissueEligibilityError, match="disabled"):
        register_auto(service)
    assert chain_length(db_path) == 1


def test_a_raised_cap_without_a_policy_id_refuses(db_path, journal_path, safety_path):
    """Opting in means BOTH raising the cap AND naming the policy: the audit
    record must identify the authorizing configuration."""
    eligible_parent(db_path)
    service = make_service(
        db_path,
        journal_path,
        safety_path,
        reissue_policy=ReissuePolicy(max_auto_reissues_per_signal=1),
    )
    with pytest.raises(ReissueEligibilityError, match="policy_id"):
        register_auto(service)
    assert chain_length(db_path) == 1


# --- 2. category gating -------------------------------------------------------


@pytest.mark.parametrize(
    "category",
    [c for c in RejectionCategory if c is not RejectionCategory.TRANSIENT],
    ids=lambda c: c.value,
)
def test_every_non_transient_category_is_refused(
    db_path, journal_path, safety_path, category
):
    reject_parent(db_path, category=category)
    record_clearance(db_path, "sig-exec-1")
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="'transient'"):
        register_auto(service)
    assert chain_length(db_path) == 1


# --- 3. kill switch -----------------------------------------------------------


class _BrokenState:
    def is_kill_switch_enabled(self):
        raise RuntimeError("state store unreachable")


class _NonBooleanState:
    def is_kill_switch_enabled(self):
        return "no"


@pytest.mark.parametrize(
    "state, match",
    [
        (StaticSafetyState(kill_switch_enabled=True), "kill switch is enabled"),
        (None, "state provider"),
        (_BrokenState(), "could not be queried"),
        (_NonBooleanState(), "non-boolean"),
    ],
    ids=["engaged", "no-provider", "query-fails", "non-boolean"],
)
def test_an_unverifiable_or_engaged_kill_switch_refuses(
    db_path, journal_path, safety_path, state, match
):
    eligible_parent(db_path)
    service = make_service(db_path, journal_path, safety_path, state=state)
    with pytest.raises(ReissueEligibilityError, match=match):
        register_auto(service)
    assert chain_length(db_path) == 1


def test_the_composition_root_wires_the_gates_kill_switch_source(tmp_path, db_path):
    """build_reissue_boundary hands the SAME SafetyStateProvider the gate
    evaluates with to the registration side: engaging the gate's kill switch
    refuses the auto reissue with no extra wiring."""
    from execution.reissue_composition import build_reissue_boundary

    eligible_parent(db_path)
    decision_db = tmp_path / "safety_gate_decisions.sqlite3"
    journal_db = tmp_path / "submission_journal.sqlite3"
    SQLiteSafetyDecisionStore(decision_db).initialize()
    SubmissionJournal(journal_db).initialize()
    gate_policy = make_policy()
    boundary = build_reissue_boundary(
        connection=BrokerConnection(broker=FakeBroker(clock=lambda: at(30))),
        gate=SafetyGate(
            policy=gate_policy,
            state_provider=StaticSafetyState(kill_switch_enabled=True),
            clock=lambda: at(30),
        ),
        current_policy=gate_policy,
        reissue_policy=auto_policy(),
        decision_db_path=decision_db,
        order_db_path=db_path,
        journal_db_path=journal_db,
        clock=lambda: at(30),
    )
    with pytest.raises(ReissueEligibilityError, match="kill switch is enabled"):
        register_auto(boundary.registration)
    assert chain_length(db_path) == 1


# --- 4. reconciliation clearance ----------------------------------------------


@pytest.mark.parametrize(
    "status, match",
    [
        (store.CLEARANCE_STATUS_REVOKED, "'revoked'"),
        (store.CLEARANCE_STATUS_OBSERVING, "'observing'"),
    ],
)
def test_an_unresolved_discrepancy_or_open_observation_refuses(
    db_path, journal_path, safety_path, status, match
):
    """A hazardous reconciliation divergence on the chain revokes the
    clearance (Issue #116), and an observation still in flight never grants
    one -- either way the auto reissue is refused."""
    reject_parent(db_path)
    record_clearance(db_path, "sig-exec-1", status=status)
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match=match):
        register_auto(service)
    assert chain_length(db_path) == 1


def test_the_absence_of_any_clearance_refuses(db_path, journal_path, safety_path):
    """No observation is NOT 'no divergence': without positive evidence the
    auto reissue fails closed."""
    reject_parent(db_path)
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="no reissue clearance"):
        register_auto(service)
    assert chain_length(db_path) == 1


def test_a_stale_clearance_refuses(db_path, journal_path, safety_path):
    reject_parent(db_path)
    record_clearance(db_path, "sig-exec-1", observed_at=at(5))
    service = make_service(
        db_path,
        journal_path,
        safety_path,
        reissue_policy=auto_policy(clearance_max_age=timedelta(seconds=20)),
    )
    with pytest.raises(ReissueEligibilityError, match="older than the freshness"):
        register_auto(service)
    assert chain_length(db_path) == 1


# --- 5. parameter identity ----------------------------------------------------


@pytest.mark.parametrize(
    "overrides, field",
    [
        (dict(quantity=Decimal("9")), "quantity"),
        (dict(quantity=Decimal("11")), "quantity"),
        (dict(requested_price=Decimal("2401.00")), "requested_price"),
        (dict(stop_loss_price=Decimal("2389.00")), "stop_loss_price"),
        (dict(stop_loss_price=None), "stop_loss_price"),
        (dict(take_profit_price=Decimal("2421.00")), "take_profit_price"),
        (
            dict(order_type=OrderType.MARKET, requested_price=None),
            "order_type",
        ),
    ],
    ids=[
        "quantity-down",
        "quantity-up",
        "price",
        "stop-loss",
        "stop-loss-dropped",
        "take-profit",
        "order-type",
    ],
)
def test_any_order_parameter_drift_is_refused(
    db_path, journal_path, safety_path, overrides, field
):
    """'The same order once more': even a DECREASE (which a manual reissue
    would accept) refuses -- automation never alters content."""
    eligible_parent(db_path)
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match=field):
        register_auto(service, **overrides)
    assert chain_length(db_path) == 1


# --- 6. cooldown --------------------------------------------------------------


def test_the_cooldown_blocks_until_elapsed_and_then_releases(
    db_path, journal_path, safety_path
):
    """The parent's rejection is recorded at t=2s; with a 60s cooldown the
    registration refuses at t=30s and succeeds at t=120s."""
    eligible_parent(db_path)
    now = {"seconds": 30}
    service = make_service(
        db_path,
        journal_path,
        safety_path,
        reissue_policy=auto_policy(auto_reissue_cooldown=timedelta(seconds=60)),
        clock=lambda: at(now["seconds"]),
    )
    with pytest.raises(ReissueEligibilityError, match="cooldown has not elapsed"):
        register_auto(service)
    assert chain_length(db_path) == 1
    now["seconds"] = 120
    child = register_auto(service)
    assert child["signal_attempt"] == 2
    assert chain_length(db_path) == 2


def test_the_registration_transaction_re_verifies_the_cooldown_cutoff(db_path):
    """PR #123 Codex review P2 (store side): the cooldown cutoff handed to
    ``create_reissue_execution_order`` is verified against the CURRENT
    parent row INSIDE the transaction -- a parent refreshed past the cutoff
    refuses with the DB untouched, like the clearance freshness cutoff."""
    reject_parent(db_path)  # parent updated_at_utc = at(2)
    clearance = record_clearance(db_path, "sig-exec-1")
    with pytest.raises(store.ExecutionOrderStateConflictError, match="cutoff"):
        store.create_reissue_execution_order(
            child_request("exec-1", "exec-2"),
            client_order_id="client-exec-2",
            created_at_utc=at(11),
            parent_execution_request_id="exec-1",
            reissue_kind=store.REISSUE_KIND_BROKER_REJECTED,
            reissue_initiated_by=store.REISSUE_INITIATED_AUTO,
            reissue_reason="auto policy auto-policy-1: parent rejection_category transient",
            clearance_generation=clearance["clearance_generation"],
            clearance_evidence_id=clearance["evidence_id"],
            parent_updated_at_not_after_utc=at(1),
            db_path=db_path,
        )
    assert chain_length(db_path) == 1
    child = store.create_reissue_execution_order(
        child_request("exec-1", "exec-2"),
        client_order_id="client-exec-2",
        created_at_utc=at(11),
        parent_execution_request_id="exec-1",
        reissue_kind=store.REISSUE_KIND_BROKER_REJECTED,
        reissue_initiated_by=store.REISSUE_INITIATED_AUTO,
        reissue_reason="auto policy auto-policy-1: parent rejection_category transient",
        clearance_generation=clearance["clearance_generation"],
        clearance_evidence_id=clearance["evidence_id"],
        parent_updated_at_not_after_utc=at(2),
        db_path=db_path,
    )
    assert child["signal_attempt"] == 2


def test_a_re_observation_in_the_registration_window_restarts_the_cooldown(
    db_path, journal_path, safety_path, monkeypatch
):
    """PR #123 Codex review P2 (service side): an audited same-state
    re-observation landing BETWEEN the service's cooldown pre-check and the
    registration transaction refreshes the parent's ``updated_at_utc`` --
    the transaction detects it via the cutoff and refuses, so the freshly
    restarted cooldown is never slipped past."""
    eligible_parent(db_path)
    service = make_service(db_path, journal_path, safety_path)  # clock at(30)
    real = store.create_reissue_execution_order

    def racing(request, **kwargs):
        # The concurrent writer lands after the pre-check read: a legal
        # terminal re-observation (identical content, newer timestamp).
        store.record_order_result(
            "exec-1",
            make_result(
                OrderState.REJECTED,
                broker_order_id=None,
                reject_reason="rejected by broker",
                rejection_category=RejectionCategory.TRANSIENT,
                updated_at=at(29),
            ),
            reason="audited re-observation",
            db_path=db_path,
        )
        return real(request, **kwargs)

    monkeypatch.setattr(store, "create_reissue_execution_order", racing)
    # cooldown 10s, cutoff = at(20): the refreshed at(29) is inside it.
    with pytest.raises(store.ExecutionOrderStateConflictError, match="cutoff"):
        register_auto(service)
    assert chain_length(db_path) == 1


# --- 7 / 8. cap and restart persistence ---------------------------------------


def test_the_auto_cap_is_enforced_from_persisted_rows(
    db_path, journal_path, safety_path
):
    eligible_parent(db_path)
    service = make_service(
        db_path,
        journal_path,
        safety_path,
        reissue_policy=auto_policy(max_auto_reissues_per_signal=1),
        clock=lambda: at(50),
    )
    register_auto(service)
    reject_child_and_clear(db_path)
    with pytest.raises(ReissueEligibilityError, match="auto reissue cap is reached"):
        register_auto(service, "exec-3", "exec-2")
    assert chain_length(db_path) == 2


def test_the_auto_count_survives_a_process_restart(
    db_path, journal_path, safety_path
):
    """Restart equivalence: a FRESH service instance (no shared in-memory
    state) still refuses, because the count is derived from persisted rows."""
    eligible_parent(db_path)
    first = make_service(
        db_path,
        journal_path,
        safety_path,
        reissue_policy=auto_policy(max_auto_reissues_per_signal=1),
        clock=lambda: at(50),
    )
    register_auto(first)
    reject_child_and_clear(db_path)
    reborn = make_service(
        db_path,
        journal_path,
        safety_path,
        reissue_policy=auto_policy(max_auto_reissues_per_signal=1),
        clock=lambda: at(50),
    )
    with pytest.raises(ReissueEligibilityError, match="auto reissue cap is reached"):
        register_auto(reborn, "exec-3", "exec-2")
    assert chain_length(db_path) == 2


# --- 9. independent budgets ---------------------------------------------------


def test_the_exhausted_auto_cap_leaves_the_manual_budget_untouched(
    db_path, journal_path, safety_path
):
    """One chain: auto reaches its cap, yet the SAME tail still accepts a
    manual reissue -- the auto rows never consume the operator's budget."""
    eligible_parent(db_path)
    service = make_service(
        db_path,
        journal_path,
        safety_path,
        reissue_policy=auto_policy(
            max_auto_reissues_per_signal=1,
            max_manual_reissues_per_signal=1,
            max_total_attempts_per_signal=6,
        ),
        clock=lambda: at(50),
    )
    register_auto(service)
    reject_child_and_clear(db_path)
    with pytest.raises(ReissueEligibilityError, match="auto reissue cap is reached"):
        register_auto(service, "exec-3", "exec-2")
    child = service.register_manual_reissue(
        child_request("exec-1", "exec-3"),
        client_order_id="client-exec-3",
        parent_execution_request_id="exec-2",
        operator_id="op-1",
        reason="operator takes over after the auto budget",
    )
    assert child["reissue_initiated_by"] == "manual"
    assert chain_length(db_path) == 3


def test_exhausted_manual_and_correction_caps_leave_the_auto_budget_untouched(
    db_path, journal_path, safety_path
):
    """The converse independence: manual and correction caps of 0 do not
    block an auto reissue within its own cap."""
    eligible_parent(db_path)
    service = make_service(
        db_path,
        journal_path,
        safety_path,
        reissue_policy=auto_policy(
            max_manual_reissues_per_signal=0,
            max_gate_corrections_per_signal=0,
        ),
    )
    child = register_auto(service)
    assert child["reissue_initiated_by"] == "auto"
    assert chain_length(db_path) == 2


def test_the_total_attempt_cap_still_bounds_auto_reissues(
    db_path, journal_path, safety_path
):
    eligible_parent(db_path)
    service = make_service(
        db_path,
        journal_path,
        safety_path,
        reissue_policy=auto_policy(
            max_auto_reissues_per_signal=5, max_total_attempts_per_signal=2
        ),
        clock=lambda: at(50),
    )
    register_auto(service)
    reject_child_and_clear(db_path)
    with pytest.raises(ReissueEligibilityError, match="total attempt cap"):
        register_auto(service, "exec-3", "exec-2")
    assert chain_length(db_path) == 2


# --- 10. UNKNOWN is never auto-resent -----------------------------------------


def test_an_unknown_parent_is_never_auto_reissued(db_path, journal_path, safety_path):
    """The Issue #95 contract holds: UNKNOWN is resolved by reconciliation
    queries only. An UNKNOWN tail is not broker-rejected, so the auto path
    structurally refuses it."""
    submit(db_path)
    store.record_order_result(
        "exec-1",
        make_result(OrderState.UNKNOWN, broker_order_id=None),
        reason="broker timeout",
        db_path=db_path,
    )
    record_clearance(db_path, "sig-exec-1")
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="order_state 'rejected'"):
        register_auto(service)
    assert chain_length(db_path) == 1


def test_a_never_submitted_parent_is_never_auto_reissued(
    db_path, journal_path, safety_path
):
    """A gate-rejected, never-submitted parent is gate_correction territory
    (manual only) -- the auto path refuses it on the parent-state rule."""
    from test_execution_store import create_order

    create_order(db_path)
    record_clearance(db_path, "sig-exec-1")
    service = make_service(db_path, journal_path, safety_path)
    with pytest.raises(ReissueEligibilityError, match="order_state 'rejected'"):
        register_auto(service)
    assert chain_length(db_path) == 1


# --- audit record and the sanctioned path -------------------------------------


def test_the_audit_record_names_the_policy_and_the_category(
    db_path, journal_path, safety_path
):
    eligible_parent(db_path)
    service = make_service(db_path, journal_path, safety_path)
    child = register_auto(service)
    assert child["signal_attempt"] == 2
    assert child["reissue_kind"] == "broker_rejected"
    assert child["reissue_initiated_by"] == "auto"
    assert (
        child["reissue_reason"]
        == "auto policy auto-policy-1: parent rejection_category transient"
    )


def test_the_registered_child_submits_through_the_existing_path_only(
    db_path, journal_path, safety_path
):
    """The child is a normal phase-``created`` intent: it gains submission
    authority through the EXISTING ``mark_submitting`` contract (clearance
    scope + freshness verified at the submission instant), no new path."""
    eligible_parent(db_path)
    service = make_service(db_path, journal_path, safety_path)
    child = register_auto(service)
    assert child["submission_phase"] == store.PHASE_CREATED
    row = store.mark_submitting(
        "exec-2",
        reason="auto reissue submit attempt",
        changed_at_utc=at(40),
        clearance_scope="oanda-demo:acct-1",
        clearance_observed_after_utc=at(0),
        db_path=db_path,
    )
    assert row["submission_phase"] == store.PHASE_SUBMITTING


def test_registration_logs_the_policy_identifier(
    db_path, journal_path, safety_path, caplog
):
    eligible_parent(db_path)
    service = make_service(db_path, journal_path, safety_path)
    with caplog.at_level(logging.INFO, logger="execution.reissue_service"):
        register_auto(service)
    assert any(
        "auto reissue registered" in record.message
        and "auto-policy-1" in record.getMessage()
        for record in caplog.records
    )


# --- policy / constructor validation ------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(policy_id="   "),
        dict(policy_id=123),
        dict(auto_reissue_cooldown=timedelta(0)),
        dict(auto_reissue_cooldown=timedelta(seconds=-1)),
        dict(auto_reissue_cooldown=300),
    ],
    ids=["blank-id", "non-str-id", "zero-cooldown", "negative-cooldown", "int-cooldown"],
)
def test_invalid_auto_policy_fields_are_rejected(kwargs):
    with pytest.raises((ValueError, TypeError)):
        ReissuePolicy(**kwargs)


def test_a_state_provider_without_the_protocol_is_rejected(
    db_path, journal_path, safety_path
):
    with pytest.raises(ValueError, match="is_kill_switch_enabled"):
        make_service(db_path, journal_path, safety_path, state=object())
