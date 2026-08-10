"""Issue #95 / #115: intent-reissue chains, clearance evidence and schema v5.

All DBs live under pytest's tmp_path. No network, no Discord, no S3, no
sleep, no real orders anywhere in this suite.
"""

import json
import sqlite3
import threading
from contextlib import closing
from datetime import timedelta
from decimal import Decimal

import pytest

from broker.broker_models import OrderState, RejectionCategory
from execution import execution_store as store
from execution import reissue_evidence
from execution.safety_gate import SafetyDecision, SafetyRejection, SafetyRejectionCode
from execution.safety_store import SQLiteSafetyDecisionStore

from test_execution_store import (
    _V5_MIGRATED_ROW_TAIL,
    _downgrade_to_v4,
    at,
    create_order,
    full_db_state,
    make_request,
    make_result,
    submit,
    submit_and_accept,
    T0,
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "execution_orders.sqlite3"


# --- helpers ------------------------------------------------------------------


def reject_parent(db_path, request_id="exec-1", *, category=RejectionCategory.TRANSIENT):
    """A parent that ended in a broker-confirmed REJECTED with ``category``."""
    submit(db_path, request_id)
    return store.record_order_result(
        request_id,
        make_result(
            OrderState.REJECTED,
            request_id,
            broker_order_id=None,
            reject_reason="rejected by broker",
            rejection_category=category,
        ),
        reason="broker rejected",
        db_path=db_path,
    )


_EVIDENCE_COUNTER = {"n": 0}


def record_clearance(
    db_path,
    signal_id,
    *,
    status=store.CLEARANCE_STATUS_VALID,
    account_scope="oanda-demo:acct-1",
    observed_at=None,
    evidence_id=None,
    expected_generation=None,
):
    _EVIDENCE_COUNTER["n"] += 1
    return store.record_reissue_clearance(
        signal_id,
        evidence_id=evidence_id or f"ev-{_EVIDENCE_COUNTER['n']}",
        status=status,
        account_scope=account_scope,
        observed_at_utc=observed_at or at(5),
        evidence_json=json.dumps(
            {"order_scope": "clean", "position_scope": "clean", "seq": _EVIDENCE_COUNTER["n"]}
        ),
        recorded_at_utc=observed_at or at(5),
        expected_generation=expected_generation,
        db_path=db_path,
    )


def child_request(parent_id="exec-1", child_id="exec-2", **overrides):
    kwargs = dict(signal_id=f"sig-{parent_id}", requested_at_utc=at(10))
    kwargs.update(overrides)
    return make_request(child_id, **kwargs)


def register_child(
    db_path,
    parent_id="exec-1",
    child_id="exec-2",
    *,
    kind=store.REISSUE_KIND_BROKER_REJECTED,
    initiated_by=store.REISSUE_INITIATED_MANUAL,
    reason="operator op-1: retry after transient rejection",
    clearance=None,
    gate_correction_reference=None,
    gate_decision_db_path=None,
    **request_overrides,
):
    request = child_request(parent_id, child_id, **request_overrides)
    if clearance is None:
        clearance = record_clearance(db_path, request.signal_id)
    return store.create_reissue_execution_order(
        request,
        client_order_id=f"client-{child_id}",
        created_at_utc=at(11),
        parent_execution_request_id=parent_id,
        reissue_kind=kind,
        reissue_initiated_by=initiated_by,
        reissue_reason=reason,
        clearance_generation=clearance["clearance_generation"],
        clearance_evidence_id=clearance["evidence_id"],
        gate_correction_reference=gate_correction_reference,
        gate_decision_db_path=gate_decision_db_path,
        db_path=db_path,
    )


def submit_child(db_path, child_id="exec-2", *, scope="oanda-demo:acct-1", cutoff=None):
    return store.mark_submitting(
        child_id,
        reason="reissue submit attempt",
        changed_at_utc=at(20),
        clearance_scope=scope,
        clearance_observed_after_utc=cutoff or at(0),
        db_path=db_path,
    )


def gate_reference(db_path_dir, parent_id="exec-1", *, code=SafetyRejectionCode.QUANTITY_LIMIT_EXCEEDED):
    """Record a rejection decision for the parent into a real safety DB and
    build the verifiable reference from it."""
    safety_path = db_path_dir / "safety_gate_decisions.sqlite3"
    decision_store = SQLiteSafetyDecisionStore(safety_path)
    decision_store.initialize()
    decision = SafetyDecision(
        execution_request_id=parent_id,
        allowed=False,
        checked_at_utc=at(3),
        policy_id="policy-1",
        rejections=(SafetyRejection(code, "quantity above policy cap"),),
        execution_mode="demo",
        connection_target="oanda-demo",
        account_id="acct-1",
        position_count=0,
    )
    decision_id = decision_store.record(decision)
    return safety_path, reissue_evidence.build_gate_decision_reference(decision_id, decision)


# --- schema v5 / migration ----------------------------------------------------


def test_fresh_v5_db_has_the_chain_indexes_and_an_empty_clearance_table(db_path):
    store.init_db(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert store.CLEARANCES_TABLE in names
        assert store._SIGNAL_ATTEMPT_UNIQUE_INDEX in names
        assert store._REISSUED_FROM_UNIQUE_INDEX in names
        assert store._SIGNAL_UNIQUE_INDEX not in names
        assert connection.execute(
            f"SELECT COUNT(*) FROM {store.CLEARANCES_TABLE}"
        ).fetchone()[0] == 0


def test_v4_to_v5_migration_keeps_rows_and_starts_clearances_empty(db_path):
    submit_and_accept(db_path)
    create_order(db_path, request_id="exec-2")
    _downgrade_to_v4(db_path)
    rows_before = full_db_state(db_path)[store.ORDERS_TABLE]
    history_before = full_db_state(db_path)[store.HISTORY_TABLE]

    store.init_db(db_path)

    after = full_db_state(db_path)
    assert after["user_version"] == store.SCHEMA_VERSION
    assert after[store.ORDERS_TABLE] == sorted(
        row + _V5_MIGRATED_ROW_TAIL for row in rows_before
    )
    # history rows widen by exactly the NULL clearance provenance column
    assert after[store.HISTORY_TABLE] == sorted(
        row + (None,) for row in history_before
    )
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute(
            f"SELECT COUNT(*) FROM {store.CLEARANCES_TABLE}"
        ).fetchone()[0] == 0  # existing signals hold NO clearance (fail-closed)
    # migrated rows restore through the normal validated read path as attempt 1
    row = store.get_execution_order("exec-1", db_path=db_path)
    assert row["signal_attempt"] == 1
    assert row["reissued_from_execution_request_id"] is None
    store.init_db(db_path)  # idempotent


def test_v4_to_v5_migration_rolls_back_completely_when_verification_fails(
    db_path, monkeypatch
):
    submit_and_accept(db_path)
    _downgrade_to_v4(db_path)
    before = full_db_state(db_path)

    def fail_verification(connection, path):
        raise store.ExecutionSchemaError("injected post-migration verification failure")

    monkeypatch.setattr(store, "_verify_schema", fail_verification)
    with pytest.raises(store.ExecutionSchemaError, match="injected"):
        store.init_db(db_path)

    assert full_db_state(db_path) == before  # columns, indexes, table, version: all back


def test_reads_and_updates_never_migrate_a_v4_db(db_path):
    submit_and_accept(db_path)
    _downgrade_to_v4(db_path)
    before = full_db_state(db_path)
    with pytest.raises(store.ExecutionSchemaError, match="init_db"):
        store.get_execution_order("exec-1", db_path=db_path)
    with pytest.raises(store.ExecutionSchemaError):
        store.record_order_state(
            "exec-1",
            OrderState.UNKNOWN,
            reason="query lost",
            changed_at_utc=at(9),
            db_path=db_path,
        )
    with pytest.raises(store.ExecutionSchemaError):
        store.record_reissue_clearance(
            "sig-exec-1",
            evidence_id="ev-x",
            status=store.CLEARANCE_STATUS_VALID,
            account_scope="scope",
            observed_at_utc=at(1),
            evidence_json='{"k": 1}',
            recorded_at_utc=at(1),
            db_path=db_path,
        )
    assert full_db_state(db_path) == before


def test_clearance_table_check_constraints_fire(db_path):
    store.init_db(db_path)
    good = (
        "sig-1", 1, "ev-1", "valid", "scope", T0.isoformat(), '{"k": 1}', T0.isoformat()
    )
    bad_rows = [
        ("sig-1", 0, "ev-1", "valid", "scope", T0.isoformat(), '{"k": 1}', T0.isoformat()),
        ("sig-1", 1, "ev-1", "cleared", "scope", T0.isoformat(), '{"k": 1}', T0.isoformat()),
        ("sig-1", 1, " ", "valid", "scope", T0.isoformat(), '{"k": 1}', T0.isoformat()),
        ("sig-1", 1, "ev-1", "valid", "scope", "2026-07-20T12:00:00", '{"k": 1}', T0.isoformat()),
    ]
    insert = (
        f"INSERT INTO {store.CLEARANCES_TABLE} ({', '.join(store._CLEARANCE_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in store._CLEARANCE_COLUMNS)})"
    )
    with closing(sqlite3.connect(db_path)) as connection:
        for bad in bad_rows:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(insert, bad)
        connection.execute(insert, good)
        with pytest.raises(sqlite3.IntegrityError):  # (signal, generation) PK
            connection.execute(insert, good)


# --- clearance evidence recording ----------------------------------------------


def test_clearance_generations_append_monotonically_and_latest_wins(db_path):
    store.init_db(db_path)
    first = record_clearance(db_path, "sig-1", status=store.CLEARANCE_STATUS_OBSERVING)
    second = record_clearance(db_path, "sig-1", status=store.CLEARANCE_STATUS_VALID)
    third = record_clearance(db_path, "sig-1", status=store.CLEARANCE_STATUS_REVOKED)
    assert [c["clearance_generation"] for c in (first, second, third)] == [1, 2, 3]
    trail = store.list_reissue_clearances("sig-1", db_path=db_path)
    assert [row["clearance_generation"] for row in trail] == [1, 2, 3]
    assert [row["status"] for row in trail] == ["observing", "valid", "revoked"]
    current = store.get_reissue_clearance("sig-1", db_path=db_path)
    assert current["clearance_generation"] == 3
    assert current["status"] == store.CLEARANCE_STATUS_REVOKED
    assert store.get_reissue_clearance("sig-other", db_path=db_path) is None


def test_clearance_expected_generation_refuses_a_lost_race(db_path):
    store.init_db(db_path)
    record_clearance(db_path, "sig-1", expected_generation=0)
    record_clearance(db_path, "sig-1", expected_generation=1)
    before = full_db_state(db_path)
    with pytest.raises(store.ExecutionClearanceError, match="generation 2"):
        record_clearance(db_path, "sig-1", expected_generation=1)
    assert full_db_state(db_path) == before


def test_clearance_rejects_duplicate_evidence_ids_and_bad_payloads(db_path):
    store.init_db(db_path)
    record_clearance(db_path, "sig-1", evidence_id="ev-dup")
    with pytest.raises(store.ExecutionOrderConflictError):
        record_clearance(db_path, "sig-2", evidence_id="ev-dup")
    with pytest.raises(ValueError, match="status"):
        record_clearance(db_path, "sig-1", status="cleared")
    with pytest.raises(ValueError, match="JSON"):
        store.record_reissue_clearance(
            "sig-1",
            evidence_id="ev-bad",
            status=store.CLEARANCE_STATUS_VALID,
            account_scope="scope",
            observed_at_utc=at(1),
            evidence_json="not json",
            recorded_at_utc=at(1),
            db_path=db_path,
        )


def test_clearance_recording_requires_an_existing_db(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(store.ExecutionStoreError, match="does not exist"):
        store.record_reissue_clearance(
            "sig-1",
            evidence_id="ev-1",
            status=store.CLEARANCE_STATUS_VALID,
            account_scope="scope",
            observed_at_utc=at(1),
            evidence_json='{"k": 1}',
            recorded_at_utc=at(1),
            db_path=missing,
        )
    assert not missing.exists()
    assert store.get_reissue_clearance("sig-1", db_path=missing) is None
    assert not missing.exists()


def test_concurrent_clearance_recorders_produce_gapless_generations(db_path):
    store.init_db(db_path)
    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()

    def observe(worker):
        try:
            barrier.wait(timeout=10)
            row = store.record_reissue_clearance(
                "sig-race",
                evidence_id=f"ev-race-{worker}",
                status=store.CLEARANCE_STATUS_VALID,
                account_scope="scope",
                observed_at_utc=at(1),
                evidence_json=json.dumps({"worker": worker}),
                recorded_at_utc=at(1),
                db_path=db_path,
            )
            outcome = row["clearance_generation"]
        except BaseException as exc:  # transferred to the main thread
            outcome = exc
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=observe, args=(n,)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert sorted(outcomes) == [1, 2]  # serialized, gapless, no failure


# --- registration: parent state matrix ------------------------------------------


def _parent_in_state(db_path, state):
    request_id = "exec-1"
    if state == "created":
        create_order(db_path, request_id)
    elif state == "pending":
        submit(db_path, request_id)
    elif state == "unknown":
        submit(db_path, request_id)
        store.record_order_state(
            request_id, OrderState.UNKNOWN, reason="lost", changed_at_utc=at(2),
            db_path=db_path,
        )
    elif state == "accepted":
        submit_and_accept(db_path, request_id)
    elif state == "partially_filled":
        submit_and_accept(db_path, request_id)
        store.record_order_result(
            request_id,
            make_result(
                OrderState.PARTIALLY_FILLED, request_id, filled="4",
                fills=(fill_at(3),), updated_at=at(3),
            ),
            reason="partial fill",
            db_path=db_path,
        )
    elif state == "filled":
        submit_and_accept(db_path, request_id)
        store.record_order_result(
            request_id,
            make_result(
                OrderState.FILLED, request_id, filled="10",
                fills=(fill_at(3, "10"),), updated_at=at(3),
            ),
            reason="filled",
            db_path=db_path,
        )
    elif state == "cancelled":
        submit_and_accept(db_path, request_id)
        store.record_order_result(
            request_id,
            make_result(OrderState.CANCELLED, request_id, updated_at=at(3)),
            reason="cancelled",
            db_path=db_path,
        )
    elif state == "rejected":
        reject_parent(db_path, request_id)
    else:  # pragma: no cover - test bug guard
        raise AssertionError(state)
    return request_id


def fill_at(seconds, quantity="4"):
    from broker.broker_models import Fill

    return Fill(
        fill_id=f"F-{seconds}",
        price=Decimal("2400.00"),
        quantity=Decimal(quantity),
        filled_at_utc=at(seconds),
    )


ALL_PARENT_STATES = (
    "created", "pending", "unknown", "accepted", "partially_filled",
    "filled", "cancelled", "rejected",
)


@pytest.mark.parametrize("state", ALL_PARENT_STATES)
def test_broker_rejected_reissue_is_allowed_only_from_a_rejected_parent(db_path, state, tmp_path):
    _parent_in_state(db_path, state)
    clearance = record_clearance(db_path, "sig-exec-1")
    before = full_db_state(db_path)
    register = lambda: register_child(db_path, clearance=clearance)  # noqa: E731
    if state == "rejected":
        child = register()
        assert child["signal_attempt"] == 2
        assert child["reissue_kind"] == store.REISSUE_KIND_BROKER_REJECTED
    else:
        with pytest.raises(ValueError, match="requires the parent"):
            register()
        assert full_db_state(db_path) == before


@pytest.mark.parametrize("state", ALL_PARENT_STATES)
def test_gate_correction_is_allowed_only_from_a_never_submitted_parent(db_path, state, tmp_path):
    _parent_in_state(db_path, state)
    clearance = record_clearance(db_path, "sig-exec-1")
    safety_path, reference = gate_reference(tmp_path)
    before = full_db_state(db_path)
    register = lambda: register_child(  # noqa: E731
        db_path,
        kind=store.REISSUE_KIND_GATE_CORRECTION,
        clearance=clearance,
        gate_correction_reference=reference,
        gate_decision_db_path=safety_path,
        quantity=Decimal("5"),
    )
    if state == "created":
        child = register()
        assert child["signal_attempt"] == 2
        assert child["reissue_kind"] == store.REISSUE_KIND_GATE_CORRECTION
        assert child["gate_correction_ref_json"] is not None
    else:
        with pytest.raises(ValueError, match="requires the parent"):
            register()
        assert full_db_state(db_path) == before


# --- registration: cross-constraints and field invariants -----------------------


def test_gate_correction_requires_a_decision_reference_and_manual_initiation(
    db_path, tmp_path
):
    create_order(db_path)
    clearance = record_clearance(db_path, "sig-exec-1")
    safety_path, reference = gate_reference(tmp_path)
    before = full_db_state(db_path)
    with pytest.raises(ValueError, match="gate-correction basis"):
        register_child(
            db_path, kind=store.REISSUE_KIND_GATE_CORRECTION, clearance=clearance
        )
    with pytest.raises(ValueError, match="manual"):
        register_child(
            db_path,
            kind=store.REISSUE_KIND_GATE_CORRECTION,
            initiated_by=store.REISSUE_INITIATED_AUTO,
            clearance=clearance,
            gate_correction_reference=reference,
            gate_decision_db_path=safety_path,
        )
    assert full_db_state(db_path) == before


def test_a_broker_rejected_reissue_must_not_carry_a_correction_basis(db_path, tmp_path):
    reject_parent(db_path)
    clearance = record_clearance(db_path, "sig-exec-1")
    safety_path, reference = gate_reference(tmp_path)
    before = full_db_state(db_path)
    with pytest.raises(ValueError, match="gate-correction basis"):
        register_child(
            db_path,
            clearance=clearance,
            gate_correction_reference=reference,
            gate_decision_db_path=safety_path,
        )
    assert full_db_state(db_path) == before


def test_identity_changes_time_regression_and_free_increases_are_rejected(db_path):
    reject_parent(db_path)  # category transient: no quantity exception
    clearance = record_clearance(db_path, "sig-exec-1")
    cases = [
        (dict(symbol="EURUSD"), "identity field symbol"),
        (dict(strategy_id="strat-2"), "identity field strategy_id"),
        (dict(requested_at_utc=T0 - timedelta(seconds=1)), "precedes the parent"),
        (dict(quantity=Decimal("11")), "exceeds the parent"),
    ]
    before = full_db_state(db_path)
    for overrides, needle in cases:
        with pytest.raises(ValueError, match=needle):
            register_child(db_path, clearance=clearance, **overrides)
        assert full_db_state(db_path) == before
    # side is identity too: build a side-flipped child (SELL needs mirrored
    # SL/TP to construct at all)
    from broker.broker_models import OrderSide

    with pytest.raises(ValueError, match="identity field side"):
        register_child(
            db_path,
            clearance=clearance,
            side=OrderSide.SELL,
            stop_loss_price=Decimal("2420.00"),
            take_profit_price=Decimal("2390.00"),
        )
    assert full_db_state(db_path) == before


def test_signal_id_mismatch_between_child_and_parent_is_rejected(db_path):
    reject_parent(db_path)
    store.init_db(db_path)
    clearance = record_clearance(db_path, "sig-other")
    before = full_db_state(db_path)
    with pytest.raises(ValueError, match="signal_id"):
        register_child(db_path, clearance=clearance, signal_id="sig-other")
    assert full_db_state(db_path) == before


def test_quantity_increase_is_allowed_exactly_for_parameter_rejections(db_path):
    reject_parent(db_path, category=RejectionCategory.PARAMETER)
    child = register_child(db_path, quantity=Decimal("12"))
    assert child["quantity"] == "12"
    # and the registered chain restores through every read path (the
    # bidirectional guarantee, increase included)
    assert store.get_execution_order("exec-2", db_path=db_path)["quantity"] == "12"
    chain = store.list_execution_order_chain("sig-exec-1", db_path=db_path)
    assert [row["execution_request_id"] for row in chain] == ["exec-1", "exec-2"]
    latest = store.get_execution_order_by_signal_id("sig-exec-1", db_path=db_path)
    assert latest["execution_request_id"] == "exec-2"


@pytest.mark.parametrize(
    "category",
    [c for c in RejectionCategory if c is not RejectionCategory.PARAMETER],
)
def test_quantity_increase_is_rejected_for_every_other_category(db_path, category):
    reject_parent(db_path, category=category)
    clearance = record_clearance(db_path, "sig-exec-1")
    before = full_db_state(db_path)
    with pytest.raises(ValueError, match="exceeds the parent"):
        register_child(db_path, clearance=clearance, quantity=Decimal("12"))
    assert full_db_state(db_path) == before


def test_gate_correction_never_allows_a_quantity_increase(db_path, tmp_path):
    create_order(db_path)
    clearance = record_clearance(db_path, "sig-exec-1")
    safety_path, reference = gate_reference(tmp_path)
    before = full_db_state(db_path)
    with pytest.raises(ValueError, match="exceeds the parent"):
        register_child(
            db_path,
            kind=store.REISSUE_KIND_GATE_CORRECTION,
            clearance=clearance,
            gate_correction_reference=reference,
            gate_decision_db_path=safety_path,
            quantity=Decimal("12"),
        )
    assert full_db_state(db_path) == before


def test_an_auto_reissue_must_repeat_the_parent_parameters_exactly(db_path):
    reject_parent(db_path)
    clearance = record_clearance(db_path, "sig-exec-1")
    before = full_db_state(db_path)
    with pytest.raises(ValueError, match="auto reissue"):
        register_child(
            db_path,
            initiated_by=store.REISSUE_INITIATED_AUTO,
            clearance=clearance,
            quantity=Decimal("9"),
        )
    assert full_db_state(db_path) == before
    child = register_child(
        db_path, initiated_by=store.REISSUE_INITIATED_AUTO, clearance=clearance
    )
    assert child["reissue_initiated_by"] == store.REISSUE_INITIATED_AUTO


def test_registration_requires_a_resolvable_clearance_reference(db_path):
    reject_parent(db_path)
    store.init_db(db_path)
    before = full_db_state(db_path)
    request = child_request()
    # no evidence recorded at all
    with pytest.raises(store.ExecutionClearanceError, match="does not resolve"):
        store.create_reissue_execution_order(
            request,
            client_order_id="client-exec-2",
            created_at_utc=at(11),
            parent_execution_request_id="exec-1",
            reissue_kind=store.REISSUE_KIND_BROKER_REJECTED,
            reissue_initiated_by=store.REISSUE_INITIATED_MANUAL,
            reissue_reason="operator op-1",
            clearance_generation=1,
            clearance_evidence_id="ev-none",
            db_path=db_path,
        )
    assert full_db_state(db_path) == before
    # recorded, but the reference names a different evidence id
    clearance = record_clearance(db_path, "sig-exec-1")
    with pytest.raises(store.ExecutionClearanceError, match="does not resolve"):
        store.create_reissue_execution_order(
            request,
            client_order_id="client-exec-2",
            created_at_utc=at(11),
            parent_execution_request_id="exec-1",
            reissue_kind=store.REISSUE_KIND_BROKER_REJECTED,
            reissue_initiated_by=store.REISSUE_INITIATED_MANUAL,
            reissue_reason="operator op-1",
            clearance_generation=clearance["clearance_generation"],
            clearance_evidence_id="ev-wrong",
            db_path=db_path,
        )


def test_a_missing_parent_is_not_found_and_creates_nothing(db_path):
    store.init_db(db_path)
    record_clearance(db_path, "sig-exec-1")
    with pytest.raises(store.ExecutionOrderNotFoundError):
        register_child(db_path, clearance={"clearance_generation": 1, "evidence_id": "ev-1"})
    assert store.get_execution_order("exec-2", db_path=db_path) is None


# --- registration: uniqueness, concurrency, chains ------------------------------


def test_one_parent_can_be_reissued_at_most_once(db_path):
    reject_parent(db_path)
    register_child(db_path)
    clearance = record_clearance(db_path, "sig-exec-1")
    before = full_db_state(db_path)
    with pytest.raises(store.ExecutionOrderConflictError, match="at most one child"):
        register_child(db_path, child_id="exec-3", clearance=clearance)
    assert full_db_state(db_path) == before


def test_concurrent_reissues_of_the_same_parent_yield_exactly_one_child(db_path):
    reject_parent(db_path)
    clearance = record_clearance(db_path, "sig-exec-1")
    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()

    def attempt(child_id):
        try:
            barrier.wait(timeout=10)
            register_child(db_path, child_id=child_id, clearance=clearance)
            outcome = None
        except BaseException as exc:
            outcome = exc
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=attempt, args=(f"exec-c{n}",)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    conflicts = [o for o in outcomes if o is not None]
    assert len(conflicts) == 1  # exactly one loser
    assert isinstance(conflicts[0], store.ExecutionOrderConflictError)
    chain = store.list_execution_order_chain("sig-exec-1", db_path=db_path)
    assert len(chain) == 2  # parent + exactly one child


def test_a_three_attempt_chain_survives_restarts_and_lists_in_order(db_path):
    reject_parent(db_path)
    register_child(db_path)
    # second attempt also gets rejected; third attempt reissues it (state is
    # re-derived from the DB alone -- nothing in-memory survives "restarts")
    store.mark_submitting(
        "exec-2",
        reason="submit",
        changed_at_utc=at(20),
        clearance_scope="oanda-demo:acct-1",
        clearance_observed_after_utc=at(0),
        db_path=db_path,
    )
    store.record_order_result(
        "exec-2",
        make_result(
            OrderState.REJECTED,
            "exec-2",
            broker_order_id=None,
            reject_reason="rejected again",
            rejection_category=RejectionCategory.TRANSIENT,
            updated_at=at(21),
        ),
        reason="broker rejected",
        db_path=db_path,
    )
    clearance = record_clearance(db_path, "sig-exec-1")
    third = store.create_reissue_execution_order(
        child_request(child_id="exec-3", requested_at_utc=at(30)),
        client_order_id="client-exec-3",
        created_at_utc=at(31),
        parent_execution_request_id="exec-2",
        reissue_kind=store.REISSUE_KIND_BROKER_REJECTED,
        reissue_initiated_by=store.REISSUE_INITIATED_MANUAL,
        reissue_reason="operator op-1: second retry",
        clearance_generation=clearance["clearance_generation"],
        clearance_evidence_id=clearance["evidence_id"],
        db_path=db_path,
    )
    assert third["signal_attempt"] == 3

    chain = store.list_execution_order_chain("sig-exec-1", db_path=db_path)
    assert [(r["execution_request_id"], r["signal_attempt"]) for r in chain] == [
        ("exec-1", 1), ("exec-2", 2), ("exec-3", 3),
    ]
    latest = store.get_execution_order_by_signal_id("sig-exec-1", db_path=db_path)
    assert latest["execution_request_id"] == "exec-3"
    # the clearance trail survives too
    assert store.get_reissue_clearance("sig-exec-1", db_path=db_path)[
        "clearance_generation"
    ] == clearance["clearance_generation"]
    # attempts derive from persisted rows: a duplicate attempt number is
    # structurally impossible even after the "restart"
    with pytest.raises(store.ExecutionOrderConflictError):
        store.create_reissue_execution_order(
            child_request(child_id="exec-4", requested_at_utc=at(40)),
            client_order_id="client-exec-4",
            created_at_utc=at(41),
            parent_execution_request_id="exec-2",
            reissue_kind=store.REISSUE_KIND_BROKER_REJECTED,
            reissue_initiated_by=store.REISSUE_INITIATED_MANUAL,
            reissue_reason="operator op-1: dup",
            clearance_generation=clearance["clearance_generation"],
            clearance_evidence_id=clearance["evidence_id"],
            db_path=db_path,
        )


def test_registered_children_restore_identically_after_reopen(db_path, tmp_path):
    # the bidirectional guarantee across every accepted variant: decrease,
    # equal quantity, parameter-exception increase, gate correction
    reject_parent(db_path, category=RejectionCategory.PARAMETER)
    child = register_child(db_path, quantity=Decimal("12"))
    reopened = store.get_execution_order("exec-2", db_path=db_path)
    assert reopened == {**child, "revision": 0}
    chain = store.list_execution_order_chain("sig-exec-1", db_path=db_path)
    assert chain[1] == reopened

    # gate correction variant in a second DB
    db2 = tmp_path / "second.sqlite3"
    create_order(db2)
    clearance = record_clearance(db2, "sig-exec-1")
    safety_path, reference = gate_reference(tmp_path)
    corrected = register_child(
        db2,
        kind=store.REISSUE_KIND_GATE_CORRECTION,
        clearance=clearance,
        gate_correction_reference=reference,
        gate_decision_db_path=safety_path,
        quantity=Decimal("5"),
    )
    assert store.get_execution_order("exec-2", db_path=db2) == {**corrected, "revision": 0}


# --- mark_submitting: supersession + clearance -----------------------------------


def test_a_parent_with_a_child_never_gains_submission_authority(db_path, tmp_path):
    create_order(db_path)
    clearance = record_clearance(db_path, "sig-exec-1")
    safety_path, reference = gate_reference(tmp_path)
    register_child(
        db_path,
        kind=store.REISSUE_KIND_GATE_CORRECTION,
        clearance=clearance,
        gate_correction_reference=reference,
        gate_decision_db_path=safety_path,
        quantity=Decimal("5"),
    )
    before = full_db_state(db_path)
    with pytest.raises(store.ExecutionOrderSupersededError, match="superseded"):
        store.mark_submitting(
            "exec-1", reason="submit", changed_at_utc=at(20), db_path=db_path
        )
    assert full_db_state(db_path) == before
    # the parent is still phase created and still refuses -- restart changes nothing
    with pytest.raises(store.ExecutionOrderSupersededError):
        store.mark_submitting(
            "exec-1", reason="submit again", changed_at_utc=at(21), db_path=db_path
        )


def test_a_reissue_child_cannot_submit_without_clearance_inputs(db_path):
    reject_parent(db_path)
    register_child(db_path)
    before = full_db_state(db_path)
    with pytest.raises(store.ExecutionClearanceError, match="verification inputs"):
        store.mark_submitting(
            "exec-2", reason="submit", changed_at_utc=at(20), db_path=db_path
        )
    assert full_db_state(db_path) == before


@pytest.mark.parametrize(
    "status", [store.CLEARANCE_STATUS_OBSERVING, store.CLEARANCE_STATUS_REVOKED]
)
def test_a_non_valid_current_clearance_refuses_submission(db_path, status):
    reject_parent(db_path)
    register_child(db_path)
    record_clearance(db_path, "sig-exec-1", status=status)  # newest wins
    before = full_db_state(db_path)
    with pytest.raises(store.ExecutionClearanceError, match=status):
        submit_child(db_path)
    assert full_db_state(db_path) == before


def test_scope_mismatch_and_staleness_refuse_submission(db_path):
    reject_parent(db_path)
    register_child(db_path)
    before = full_db_state(db_path)
    with pytest.raises(store.ExecutionClearanceError, match="scope"):
        submit_child(db_path, scope="oanda-demo:acct-OTHER")
    with pytest.raises(store.ExecutionClearanceError, match="freshness cutoff"):
        submit_child(db_path, cutoff=at(6))  # evidence observed at at(5)
    assert full_db_state(db_path) == before


def test_attempt_one_orders_take_no_clearance_inputs(db_path):
    create_order(db_path)
    with pytest.raises(ValueError, match="attempt 1"):
        store.mark_submitting(
            "exec-1",
            reason="submit",
            changed_at_utc=at(1),
            clearance_scope="scope",
            clearance_observed_after_utc=at(0),
            db_path=db_path,
        )


def test_accepted_evidence_is_persisted_atomically_with_the_grant(db_path):
    reject_parent(db_path)
    register_child(db_path)  # registration references generation 1
    fresh = record_clearance(db_path, "sig-exec-1")  # generation 2 authorizes
    updated = submit_child(db_path)
    assert updated["submission_phase"] == "submitting"

    history = store.get_state_history("exec-2", db_path=db_path)
    assert len(history) == 1
    granted = json.loads(history[0]["clearance_json"])
    assert granted["clearance_generation"] == fresh["clearance_generation"]
    assert granted["evidence_id"] == fresh["evidence_id"]
    assert granted["record_sha256"] == fresh["record_sha256"]
    # the registration-time evidence stays separately recorded on the row
    registered = json.loads(
        store.get_execution_order("exec-2", db_path=db_path)["reissue_clearance_json"]
    )
    assert registered["clearance_generation"] == 1
    assert registered != granted


def test_a_failed_grant_persists_nothing(db_path, monkeypatch):
    reject_parent(db_path)
    register_child(db_path)
    before = full_db_state(db_path)

    def failing_append(*args, **kwargs):
        raise sqlite3.OperationalError("injected history failure")

    monkeypatch.setattr(store, "_append_history", failing_append)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        submit_child(db_path)
    assert full_db_state(db_path) == before  # no phase flip without the evidence row


# --- out-of-band damage --------------------------------------------------------


def _raw(db_path, sql, params=()):
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(sql, params)
        connection.commit()


def _chain_with_submitted_child(db_path):
    reject_parent(db_path)
    register_child(db_path)
    record_clearance(db_path, "sig-exec-1")
    submit_child(db_path)


def test_out_of_band_quantity_inflation_is_rejected_on_restore(db_path):
    _chain_with_submitted_child(db_path)
    _raw(
        db_path,
        f"UPDATE {store.ORDERS_TABLE} SET quantity = '99' WHERE execution_request_id = 'exec-2'",
    )
    with pytest.raises(store.ExecutionIntegrityError, match="exceeds the parent"):
        store.get_execution_order("exec-2", db_path=db_path)


def test_out_of_band_attempt_renumbering_is_rejected_on_restore(db_path):
    _chain_with_submitted_child(db_path)
    _raw(
        db_path,
        f"UPDATE {store.ORDERS_TABLE} SET signal_attempt = 3 WHERE execution_request_id = 'exec-2'",
    )
    with pytest.raises(store.ExecutionIntegrityError, match="attempt"):
        store.get_execution_order("exec-2", db_path=db_path)


def test_an_orphaned_child_is_rejected_on_restore(db_path):
    _chain_with_submitted_child(db_path)
    _raw(db_path, f"DELETE FROM {store.HISTORY_TABLE} WHERE execution_request_id = 'exec-1'")
    _raw(db_path, f"DELETE FROM {store.ORDERS_TABLE} WHERE execution_request_id = 'exec-1'")
    with pytest.raises(store.ExecutionIntegrityError, match="does not exist"):
        store.get_execution_order("exec-2", db_path=db_path)


def test_stripped_reissue_audit_columns_are_rejected_on_restore(db_path):
    _chain_with_submitted_child(db_path)
    _raw(
        db_path,
        f"UPDATE {store.ORDERS_TABLE} SET reissue_kind = NULL, reissue_initiated_by = NULL "
        "WHERE execution_request_id = 'exec-2'",
    )
    with pytest.raises(store.ExecutionIntegrityError, match="requires reissue_kind"):
        store.get_execution_order("exec-2", db_path=db_path)


def test_reissue_audit_columns_on_an_attempt_one_row_are_rejected(db_path):
    create_order(db_path)
    _raw(
        db_path,
        f"UPDATE {store.ORDERS_TABLE} SET reissue_reason = 'stray' "
        "WHERE execution_request_id = 'exec-1'",
    )
    with pytest.raises(store.ExecutionIntegrityError, match="attempt 1 must not carry"):
        store.get_execution_order("exec-1", db_path=db_path)


def test_tampered_clearance_evidence_fails_the_referencing_child(db_path):
    _chain_with_submitted_child(db_path)
    _raw(
        db_path,
        f"UPDATE {store.CLEARANCES_TABLE} SET evidence_json = '{{\"swapped\": true}}' "
        "WHERE clearance_generation = 1",
    )
    with pytest.raises(store.ExecutionIntegrityError, match="sha256"):
        store.get_execution_order("exec-2", db_path=db_path)


def test_a_deleted_clearance_row_fails_the_referencing_child(db_path):
    _chain_with_submitted_child(db_path)
    _raw(db_path, f"DELETE FROM {store.CLEARANCES_TABLE} WHERE clearance_generation = 1")
    with pytest.raises(store.ExecutionIntegrityError, match="does not resolve"):
        store.get_execution_order("exec-2", db_path=db_path)


def test_stripping_the_submission_evidence_from_history_is_rejected(db_path):
    _chain_with_submitted_child(db_path)
    _raw(
        db_path,
        f"UPDATE {store.HISTORY_TABLE} SET clearance_json = NULL "
        "WHERE execution_request_id = 'exec-2'",
    )
    with pytest.raises(store.ExecutionIntegrityError, match="carries none"):
        store.get_execution_order("exec-2", db_path=db_path)


def test_clearance_provenance_on_an_attempt_one_history_row_is_rejected(db_path):
    _chain_with_submitted_child(db_path)
    ref = json.dumps(
        {"clearance_generation": 1, "evidence_id": "ev-x", "record_sha256": "0" * 64},
        sort_keys=True,
    )
    _raw(
        db_path,
        f"UPDATE {store.HISTORY_TABLE} SET clearance_json = ? "
        "WHERE execution_request_id = 'exec-1'",
        (ref,),
    )
    with pytest.raises(store.ExecutionIntegrityError, match="belongs only"):
        store.get_execution_order("exec-1", db_path=db_path)


def test_a_gap_in_the_clearance_generations_is_rejected(db_path):
    store.init_db(db_path)
    record_clearance(db_path, "sig-1")
    record_clearance(db_path, "sig-1")
    _raw(db_path, f"DELETE FROM {store.CLEARANCES_TABLE} WHERE clearance_generation = 1")
    with pytest.raises(store.ExecutionIntegrityError, match="contiguous"):
        store.get_reissue_clearance("sig-1", db_path=db_path)


# --- cross-DB gate decision references -------------------------------------------


def test_a_gate_decision_reference_round_trips_and_verifies(db_path, tmp_path):
    create_order(db_path)
    clearance = record_clearance(db_path, "sig-exec-1")
    safety_path, reference = gate_reference(tmp_path)
    child = register_child(
        db_path,
        kind=store.REISSUE_KIND_GATE_CORRECTION,
        clearance=clearance,
        gate_correction_reference=reference,
        gate_decision_db_path=safety_path,
        quantity=Decimal("5"),
    )
    stored_json = child["gate_correction_ref_json"]
    assert store.gate_decision_reference_from_json(stored_json) == reference
    # verification against the live decision DB succeeds (typed or JSON form)
    reissue_evidence.verify_gate_decision_reference(reference, db_path=safety_path)
    reissue_evidence.verify_gate_decision_reference(stored_json, db_path=safety_path)


def test_a_swapped_decision_db_fails_verification_closed(db_path, tmp_path):
    safety_path, reference = gate_reference(tmp_path)
    # simulate the referenced DB being re-created: same decision_id, other content
    safety_path.unlink()
    decision_store = SQLiteSafetyDecisionStore(safety_path)
    decision_store.initialize()
    other = SafetyDecision(
        execution_request_id="exec-1",
        allowed=False,
        checked_at_utc=at(4),
        policy_id="policy-OTHER",
        rejections=(
            SafetyRejection(SafetyRejectionCode.MARKET_CLOSED, "different record"),
        ),
    )
    assert decision_store.record(other) == reference.decision_id  # id reused!
    with pytest.raises(store.ExecutionEvidenceReferenceError, match="not the intended"):
        reissue_evidence.verify_gate_decision_reference(reference, db_path=safety_path)


def test_missing_or_foreign_decision_dbs_fail_verification_closed(tmp_path):
    _, reference = gate_reference(tmp_path)
    missing = tmp_path / "nowhere.sqlite3"
    with pytest.raises(store.ExecutionEvidenceReferenceError, match="no safety decision DB"):
        reissue_evidence.verify_gate_decision_reference(reference, db_path=missing)
    assert not missing.exists()  # verification never creates the file
    foreign = tmp_path / "foreign.sqlite3"
    with closing(sqlite3.connect(foreign)) as connection:
        connection.execute("CREATE TABLE something_else (x)")
        connection.commit()
    with pytest.raises(store.ExecutionEvidenceReferenceError, match="unreadable"):
        reissue_evidence.verify_gate_decision_reference(reference, db_path=foreign)


def test_a_reference_to_a_never_recorded_decision_fails_closed(tmp_path):
    safety_path, reference = gate_reference(tmp_path)
    ghost = store.GateDecisionReference(
        decision_id=reference.decision_id + 7,
        execution_request_id=reference.execution_request_id,
        allowed=False,
        checked_at_utc=reference.checked_at_utc,
        policy_id=reference.policy_id,
        rejections=reference.rejections,
        execution_mode=reference.execution_mode,
        connection_target=reference.connection_target,
        account_id=reference.account_id,
        position_count=reference.position_count,
    )
    with pytest.raises(store.ExecutionEvidenceReferenceError, match="does not resolve"):
        reissue_evidence.verify_gate_decision_reference(ghost, db_path=safety_path)


def test_a_tampered_stored_reference_is_rejected_by_the_strict_parser():
    with pytest.raises(ValueError, match="keys"):
        store.gate_decision_reference_from_json(json.dumps({"decision_id": 1}))
    with pytest.raises(store.ExecutionEvidenceReferenceError, match="unverifiable"):
        reissue_evidence.verify_gate_decision_reference("not json", db_path=None)


def test_a_correction_basis_must_be_a_rejection():
    with pytest.raises(ValueError, match="allowed must be False"):
        store.GateDecisionReference(
            decision_id=1,
            execution_request_id="exec-1",
            allowed=True,
            checked_at_utc=at(1),
            policy_id="p",
            rejections=(("market_closed", "m"),),
        )
    with pytest.raises(ValueError, match="non-empty tuple"):
        store.GateDecisionReference(
            decision_id=1,
            execution_request_id="exec-1",
            allowed=False,
            checked_at_utc=at(1),
            policy_id="p",
            rejections=(),
        )


# --- reads stay dry-run safe ------------------------------------------------------


def test_chain_reads_never_create_a_missing_db(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    assert store.list_execution_order_chain("sig-1", db_path=missing) == []
    assert store.get_execution_order_by_signal_id("sig-1", db_path=missing) is None
    assert store.list_reissue_clearances("sig-1", db_path=missing) == []
    assert not missing.exists()


# --- PR #121 Codex review fixes ---------------------------------------------------


def test_registration_enforces_gate_reference_verification_fail_closed(db_path, tmp_path):
    """P1: a fabricated / unverifiable correction basis can never be
    registered -- verification is enforced on the registration path."""
    create_order(db_path)
    clearance = record_clearance(db_path, "sig-exec-1")
    before = full_db_state(db_path)
    fabricated = store.GateDecisionReference(
        decision_id=1,
        execution_request_id="exec-1",
        allowed=False,
        checked_at_utc=at(3),
        policy_id="policy-forged",
        rejections=(("quantity_limit_exceeded", "forged"),),
    )
    # no decision DB at the given path at all
    with pytest.raises(store.ExecutionEvidenceReferenceError, match="no safety decision DB"):
        register_child(
            db_path,
            kind=store.REISSUE_KIND_GATE_CORRECTION,
            clearance=clearance,
            gate_correction_reference=fabricated,
            gate_decision_db_path=tmp_path / "nowhere.sqlite3",
        )
    assert full_db_state(db_path) == before

    # a real DB whose decision content does not match the reference
    safety_path, _ = gate_reference(tmp_path)  # records decision_id 1, policy-1
    with pytest.raises(store.ExecutionEvidenceReferenceError, match="not the intended"):
        register_child(
            db_path,
            kind=store.REISSUE_KIND_GATE_CORRECTION,
            clearance=clearance,
            gate_correction_reference=fabricated,
            gate_decision_db_path=safety_path,
        )
    assert full_db_state(db_path) == before


@pytest.mark.parametrize(
    "column, value",
    [
        ("status", "revoked"),
        ("account_scope", "oanda-demo:acct-OTHER"),
        ("observed_at_utc", "2026-07-20T13:59:59+00:00"),
        ("recorded_at_utc", "2026-07-20T13:59:59+00:00"),
    ],
)
def test_tampering_any_authorization_field_of_referenced_evidence_is_detected(
    db_path, column, value
):
    """P1: the reference digest binds the COMPLETE clearance record, so an
    out-of-band edit of status / scope / observation time on a referenced
    row fails the referencing child closed -- not just evidence_json."""
    _chain_with_submitted_child(db_path)
    _raw(
        db_path,
        f"UPDATE {store.CLEARANCES_TABLE} SET {column} = ? WHERE clearance_generation = 1",
        (value,),
    )
    with pytest.raises(store.ExecutionIntegrityError, match="sha256"):
        store.get_execution_order("exec-2", db_path=db_path)


def test_a_parent_with_wiped_history_fails_the_child_closed(db_path):
    """P1 (reviewer scenario): corruption inside the parent bundle is
    detected when reading OR submitting the child, not only when reading
    the parent itself."""
    reject_parent(db_path)
    register_child(db_path)
    record_clearance(db_path, "sig-exec-1")
    _raw(db_path, f"DELETE FROM {store.HISTORY_TABLE} WHERE execution_request_id = 'exec-1'")
    with pytest.raises(store.ExecutionIntegrityError):
        store.get_execution_order("exec-1", db_path=db_path)
    with pytest.raises(store.ExecutionIntegrityError):
        store.get_execution_order("exec-2", db_path=db_path)  # via the chain walk
    with pytest.raises(store.ExecutionIntegrityError):
        submit_child(db_path)  # submission authority is refused too


def test_grandparent_corruption_fails_the_whole_chain_closed(db_path):
    """P1: the chain walk validates EVERY ancestor bundle, not just the
    immediate parent."""
    reject_parent(db_path)
    register_child(db_path)
    store.mark_submitting(
        "exec-2",
        reason="submit",
        changed_at_utc=at(20),
        clearance_scope="oanda-demo:acct-1",
        clearance_observed_after_utc=at(0),
        db_path=db_path,
    )
    store.record_order_result(
        "exec-2",
        make_result(
            OrderState.REJECTED,
            "exec-2",
            broker_order_id=None,
            reject_reason="rejected again",
            rejection_category=RejectionCategory.TRANSIENT,
            updated_at=at(21),
        ),
        reason="broker rejected",
        db_path=db_path,
    )
    clearance = record_clearance(db_path, "sig-exec-1")
    store.create_reissue_execution_order(
        child_request(child_id="exec-3", requested_at_utc=at(30)),
        client_order_id="client-exec-3",
        created_at_utc=at(31),
        parent_execution_request_id="exec-2",
        reissue_kind=store.REISSUE_KIND_BROKER_REJECTED,
        reissue_initiated_by=store.REISSUE_INITIATED_MANUAL,
        reissue_reason="operator op-1: second retry",
        clearance_generation=clearance["clearance_generation"],
        clearance_evidence_id=clearance["evidence_id"],
        db_path=db_path,
    )
    # corrupt the ROOT (grandparent of exec-3): swap its stored quantity so
    # its own bundle validation (snapshot identity check) fails
    _raw(
        db_path,
        f"UPDATE {store.ORDERS_TABLE} SET quantity = '7' WHERE execution_request_id = 'exec-1'",
    )
    with pytest.raises(store.ExecutionIntegrityError):
        store.get_execution_order("exec-3", db_path=db_path)


def test_a_generation_gap_blocks_submission_and_registration(db_path):
    """P2: the complete clearance trail is validated inside the
    authorization and registration transactions; a deleted intermediate
    generation fails closed even though the tail row is intact."""
    reject_parent(db_path)
    register_child(db_path)  # references generation 1
    record_clearance(db_path, "sig-exec-1")  # generation 2
    record_clearance(db_path, "sig-exec-1")  # generation 3 (the valid tail)
    _raw(db_path, f"DELETE FROM {store.CLEARANCES_TABLE} WHERE clearance_generation = 2")
    with pytest.raises(store.ExecutionIntegrityError, match="contiguous"):
        submit_child(db_path)
    # still phase created, nothing was granted
    with closing(sqlite3.connect(db_path)) as connection:
        phase = connection.execute(
            f"SELECT submission_phase FROM {store.ORDERS_TABLE} "
            "WHERE execution_request_id = 'exec-2'"
        ).fetchone()[0]
    assert phase == "created"

    # registration over a gapped trail is refused the same way
    db2 = db_path.parent / "second_gap.sqlite3"
    reject_parent(db2)
    record_clearance(db2, "sig-exec-1")  # generation 1
    tail = record_clearance(db2, "sig-exec-1")  # generation 2
    _raw(db2, f"DELETE FROM {store.CLEARANCES_TABLE} WHERE clearance_generation = 1")
    with pytest.raises(store.ExecutionIntegrityError, match="contiguous"):
        register_child(db2, clearance=tail)
    assert store.get_execution_order("exec-2", db_path=db2) is None


# --- PR #121 Codex review round 2 fixes --------------------------------------------


def test_editing_the_unreferenced_tail_is_detected_by_the_head_commitment(db_path):
    """r2 P1 (reviewer repro a): gen 1 valid justified the registration,
    gen 2 revoked was appended later; flipping gen 2 back to valid out of
    band must not grant submission."""
    reject_parent(db_path)
    register_child(db_path)  # references generation 1 (valid)
    record_clearance(db_path, "sig-exec-1", status=store.CLEARANCE_STATUS_REVOKED)
    _raw(
        db_path,
        f"UPDATE {store.CLEARANCES_TABLE} SET status = 'valid' WHERE clearance_generation = 2",
    )
    with pytest.raises(store.ExecutionIntegrityError, match="head commitment"):
        submit_child(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        phase = connection.execute(
            f"SELECT submission_phase FROM {store.ORDERS_TABLE} "
            "WHERE execution_request_id = 'exec-2'"
        ).fetchone()[0]
    assert phase == "created"


def test_deleting_the_revoking_tail_is_detected_by_the_head_commitment(db_path):
    """r2 P1 (reviewer repro b): deleting the newest (revoking) generation
    leaves a contiguous trail ending in the old valid row -- the head
    commitment still names generation 2, so authorization fails closed."""
    reject_parent(db_path)
    register_child(db_path)  # generation 1 valid
    record_clearance(db_path, "sig-exec-1", status=store.CLEARANCE_STATUS_REVOKED)
    _raw(db_path, f"DELETE FROM {store.CLEARANCES_TABLE} WHERE clearance_generation = 2")
    with pytest.raises(store.ExecutionIntegrityError, match="head commitment"):
        submit_child(db_path)
    # reads fail closed the same way
    with pytest.raises(store.ExecutionIntegrityError, match="head commitment"):
        store.get_reissue_clearance("sig-exec-1", db_path=db_path)
    with pytest.raises(store.ExecutionIntegrityError, match="head commitment"):
        store.get_execution_order("exec-2", db_path=db_path)


def test_tampering_the_head_commitment_itself_fails_closed(db_path):
    reject_parent(db_path)
    register_child(db_path)
    _raw(db_path, f"DELETE FROM {store.CLEARANCE_HEADS_TABLE} WHERE signal_id = 'sig-exec-1'")
    with pytest.raises(store.ExecutionIntegrityError, match="without a trail-head"):
        submit_child(db_path)
    # recording refuses to extend a damaged trail too
    with pytest.raises(store.ExecutionIntegrityError, match="without a trail-head"):
        record_clearance(db_path, "sig-exec-1")


def test_the_head_commitment_moves_atomically_with_every_append(db_path):
    store.init_db(db_path)
    first = record_clearance(db_path, "sig-1", status=store.CLEARANCE_STATUS_OBSERVING)
    second = record_clearance(db_path, "sig-1", status=store.CLEARANCE_STATUS_VALID)
    with closing(sqlite3.connect(db_path)) as connection:
        head = dict(
            zip(
                store._CLEARANCE_HEAD_COLUMNS,
                connection.execute(
                    f"SELECT {', '.join(store._CLEARANCE_HEAD_COLUMNS)} FROM "
                    f"{store.CLEARANCE_HEADS_TABLE} WHERE signal_id = 'sig-1'"
                ).fetchone(),
            )
        )
    assert head["clearance_generation"] == 2
    assert head["record_sha256"] == second["record_sha256"]
    assert head["record_sha256"] != first["record_sha256"]


def test_deleting_the_child_row_alone_does_not_resurrect_the_parent(db_path, tmp_path):
    """r2 P1 (reviewer repro): a freshly registered gate-correction child
    has no history and nothing referencing its row; deleting it out of
    band must not let the superseded parent submit."""
    create_order(db_path)
    clearance = record_clearance(db_path, "sig-exec-1")
    safety_path, reference = gate_reference(tmp_path)
    register_child(
        db_path,
        kind=store.REISSUE_KIND_GATE_CORRECTION,
        clearance=clearance,
        gate_correction_reference=reference,
        gate_decision_db_path=safety_path,
        quantity=Decimal("5"),
    )
    _raw(db_path, f"DELETE FROM {store.ORDERS_TABLE} WHERE execution_request_id = 'exec-2'")
    with pytest.raises(store.ExecutionIntegrityError, match="deleted out of band"):
        store.mark_submitting(
            "exec-1", reason="submit", changed_at_utc=at(20), db_path=db_path
        )
    # and a replacement child cannot be registered on top of the damage
    with pytest.raises(store.ExecutionIntegrityError, match="does not exist"):
        register_child(
            db_path,
            child_id="exec-3",
            kind=store.REISSUE_KIND_GATE_CORRECTION,
            clearance=clearance,
            gate_correction_reference=reference,
            gate_decision_db_path=safety_path,
            quantity=Decimal("5"),
        )


def test_deleting_the_supersession_marker_alone_fails_closed(db_path, tmp_path):
    create_order(db_path)
    clearance = record_clearance(db_path, "sig-exec-1")
    safety_path, reference = gate_reference(tmp_path)
    register_child(
        db_path,
        kind=store.REISSUE_KIND_GATE_CORRECTION,
        clearance=clearance,
        gate_correction_reference=reference,
        gate_decision_db_path=safety_path,
        quantity=Decimal("5"),
    )
    _raw(
        db_path,
        f"DELETE FROM {store.SUPERSESSIONS_TABLE} WHERE parent_execution_request_id = 'exec-1'",
    )
    # the parent still cannot submit (child row present, marker missing)
    with pytest.raises(store.ExecutionIntegrityError, match="supersession marker"):
        store.mark_submitting(
            "exec-1", reason="submit", changed_at_utc=at(20), db_path=db_path
        )
    # and the child itself fails restore-time validation
    with pytest.raises(store.ExecutionIntegrityError, match="supersession marker"):
        store.get_execution_order("exec-2", db_path=db_path)


def test_the_supersession_marker_is_written_atomically_with_the_child(db_path):
    reject_parent(db_path)
    child = register_child(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        marker = connection.execute(
            f"SELECT * FROM {store.SUPERSESSIONS_TABLE} WHERE parent_execution_request_id = 'exec-1'"
        ).fetchone()
    assert marker is not None
    assert marker[1] == child["execution_request_id"]
    # a marker naming a different child is out-of-band damage
    _raw(
        db_path,
        f"UPDATE {store.SUPERSESSIONS_TABLE} SET child_execution_request_id = 'exec-x' "
        "WHERE parent_execution_request_id = 'exec-1'",
    )
    with pytest.raises(store.ExecutionIntegrityError, match="different child"):
        store.get_execution_order("exec-2", db_path=db_path)


# --- PR #121 Codex review round 3 fix ----------------------------------------------


def test_a_dangling_marker_fails_parent_reads_closed(db_path, tmp_path):
    """r3 P2 (reviewer repro): with the child row deleted out of band and
    its marker left behind, every read path -- not only mark_submitting --
    must refuse to expose the superseded parent as the live chain tail."""
    create_order(db_path)
    clearance = record_clearance(db_path, "sig-exec-1")
    safety_path, reference = gate_reference(tmp_path)
    register_child(
        db_path,
        kind=store.REISSUE_KIND_GATE_CORRECTION,
        clearance=clearance,
        gate_correction_reference=reference,
        gate_decision_db_path=safety_path,
        quantity=Decimal("5"),
    )
    _raw(db_path, f"DELETE FROM {store.ORDERS_TABLE} WHERE execution_request_id = 'exec-2'")
    with pytest.raises(store.ExecutionIntegrityError, match="deleted out of band"):
        store.get_execution_order("exec-1", db_path=db_path)
    with pytest.raises(store.ExecutionIntegrityError, match="deleted out of band"):
        store.get_execution_order_by_signal_id("sig-exec-1", db_path=db_path)
    with pytest.raises(store.ExecutionIntegrityError, match="deleted out of band"):
        store.list_execution_order_chain("sig-exec-1", db_path=db_path)
    # the mirror damage -- marker deleted, child row kept -- fails the
    # parent's reads too (the child's own reads were already covered)
    db2 = db_path.parent / "second_marker.sqlite3"
    reject_parent(db2)
    register_child(db2)
    _raw(
        db2,
        f"DELETE FROM {store.SUPERSESSIONS_TABLE} WHERE parent_execution_request_id = 'exec-1'",
    )
    with pytest.raises(store.ExecutionIntegrityError, match="supersession marker"):
        store.get_execution_order("exec-1", db_path=db2)
