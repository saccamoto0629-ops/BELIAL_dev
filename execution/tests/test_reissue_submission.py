"""Issue #116: the sanctioned submission path for reissue children --
clearance verification at the submission instant, account-scope binding,
``intent_superseded`` refusal and submission-evidence traceability.

All DBs live under pytest's tmp_path; the broker is always FakeBroker. No
network, no Discord, no S3, no sleep, no real orders anywhere in this suite.
"""

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from broker.broker_models import (
    AccountState,
    BrokerCommunicationError,
    OrderSide,
    OrderState,
)
from broker.fake_broker import FakeBroker
from execution import execution_store as store
from execution.live_submission import LiveOrderSubmissionService
from execution.reissue_clearance import derive_account_scope
from execution.safety_gate import (
    BrokerConnection,
    ExecutionMode,
    MarketSafetySnapshot,
    SafetyGate,
    SafetyPolicy,
    StaticSafetyState,
)
from execution.safety_store import SQLiteSafetyDecisionStore
from execution.submission_journal import (
    OUTCOME_SUPERSEDED,
    SubmissionJournal,
)

from test_execution_store import at
from test_execution_reissue import (
    gate_reference,
    record_clearance,
    register_child,
    reject_parent,
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "execution_orders.sqlite3"


def make_policy():
    return SafetyPolicy(
        policy_id="policy-1",
        allowed_symbols=frozenset({"XAUUSD"}),
        allowed_sides=frozenset({OrderSide.BUY, OrderSide.SELL}),
        max_order_quantity=Decimal("50"),
        max_positions_per_symbol=5,
        max_positions_account=10,
        max_reference_age=timedelta(seconds=60),
        max_spread_by_symbol={"XAUUSD": Decimal("1")},
        allowed_connection_targets={ExecutionMode.FAKE: frozenset({"fake"})},
    )


def fresh_account_state(seconds=95):
    return AccountState(
        account_id="fake-account",
        currency="USD",
        balance=Decimal("100000"),
        equity=Decimal("100000"),
        margin_available=Decimal("100000"),
        retrieved_at_utc=at(seconds),
    )


def make_broker(cls=FakeBroker, *, seconds=105):
    """A FakeBroker whose clock and account snapshot sit AFTER the store's
    T0-based timestamps (the default FakeBroker base predates them)."""
    broker = cls(clock=lambda: at(seconds))
    broker.set_account_state(fresh_account_state(seconds - 10))
    return broker


def make_market(seconds=95):
    return MarketSafetySnapshot(
        symbol="XAUUSD",
        reference_price=Decimal("2400.00"),
        bid=Decimal("2399.90"),
        ask=Decimal("2400.10"),
        observed_at_utc=at(seconds),
        market_open=True,
        tradable=True,
    )


def make_submission_service(
    tmp_path, db_path, *, broker=None, clock=None, clearance_max_age=None
):
    broker = broker if broker is not None else make_broker()
    decision_db = tmp_path / "safety_gate_decisions.sqlite3"
    journal_db = tmp_path / "submission_journal.sqlite3"
    SQLiteSafetyDecisionStore(decision_db).initialize()
    SubmissionJournal(journal_db).initialize()
    clock = clock or (lambda: at(100))
    kwargs = {}
    if clearance_max_age is not None:
        kwargs["clearance_max_age"] = clearance_max_age
    service = LiveOrderSubmissionService(
        connection=BrokerConnection(broker=broker),
        gate=SafetyGate(policy=make_policy(), state_provider=StaticSafetyState(), clock=clock),
        decision_db_path=decision_db,
        order_db_path=db_path,
        journal_db_path=journal_db,
        clock=clock,
        **kwargs,
    )
    return service, broker, journal_db


def make_reissue_child(db_path, broker):
    """A registered reissue child whose clearance is scoped to ``broker``."""
    reject_parent(db_path)
    clearance = record_clearance(
        db_path, "sig-exec-1", account_scope=derive_account_scope(broker)
    )
    register_child(db_path, clearance=clearance)
    return clearance


# --- clearance at the submission instant --------------------------------------


def test_reissue_child_submits_under_a_matching_scope_and_records_the_evidence(
    tmp_path, db_path
):
    broker = make_broker()
    clearance = make_reissue_child(db_path, broker)
    service, broker, _ = make_submission_service(tmp_path, db_path, broker=broker)
    decision, result = service.submit("exec-2", market=make_market())
    assert decision.allowed and result is not None
    assert result.state is OrderState.FILLED

    # 送信を許可した evidence の一意な追跡 (再起動後の読み直しに相当する
    # 独立読み取りで): the granting history row carries the content-bound
    # reference to the exact evidence generation that authorized it.
    history = store.get_state_history("exec-2", db_path=db_path)
    reference = json.loads(history[0]["clearance_json"])
    assert reference["evidence_id"] == clearance["evidence_id"]
    assert reference["clearance_generation"] == clearance["clearance_generation"]
    trail = store.list_reissue_clearances("sig-exec-1", db_path=db_path)
    referenced = trail[reference["clearance_generation"] - 1]
    assert referenced["evidence_id"] == reference["evidence_id"]


def test_submission_evidence_is_distinguishable_from_the_registration_evidence(
    tmp_path, db_path
):
    """登録時と異なる evidence の下で送信された場合でも、送信を許可した
    evidence を一意に特定できる: a NEWER valid observation lands between the
    registration and the submit; the granting history row references the
    newer generation while the child row keeps the registration reference."""
    broker = make_broker()
    registration_clearance = make_reissue_child(db_path, broker)
    newer = record_clearance(
        db_path, "sig-exec-1", account_scope=derive_account_scope(broker)
    )
    service, _, _ = make_submission_service(tmp_path, db_path, broker=broker)
    decision, result = service.submit("exec-2", market=make_market())
    assert decision.allowed and result is not None

    row = store.get_execution_order("exec-2", db_path=db_path)
    registration_ref = json.loads(row["reissue_clearance_json"])
    history = store.get_state_history("exec-2", db_path=db_path)
    submission_ref = json.loads(history[0]["clearance_json"])
    assert registration_ref["evidence_id"] == registration_clearance["evidence_id"]
    assert submission_ref["evidence_id"] == newer["evidence_id"]
    assert submission_ref["clearance_generation"] != registration_ref["clearance_generation"]


def test_a_different_submission_account_scope_is_refused(tmp_path, db_path):
    """観測口座と送信接続が異なる場合の拒否: the clearance was observed for
    ANOTHER broker/account scope, so this connection gains no authority."""
    reject_parent(db_path)
    clearance = record_clearance(
        db_path, "sig-exec-1", account_scope="fake:fake:account-somewhere-else"
    )
    register_child(db_path, clearance=clearance)
    service, _, _ = make_submission_service(tmp_path, db_path)
    decision, result = service.submit("exec-2", market=make_market())
    assert result is None
    assert decision.rejection_codes == ("reissue_clearance_invalid",)
    row = store.get_execution_order("exec-2", db_path=db_path)
    assert row["submission_phase"] == "created"  # nothing was submitted


def test_an_underivable_submission_scope_is_refused_fail_closed(tmp_path, db_path):
    """スコープを確認できない場合は fail-closed: the account query fails
    AFTER the gate passed, so the scope cannot be derived and the child
    never submits."""

    class FlakyAccountBroker(FakeBroker):
        def __init__(self):
            super().__init__(clock=lambda: at(105))
            self.set_account_state(fresh_account_state())
            self.account_calls = 0

        def get_account_state(self):
            self.account_calls += 1
            if self.account_calls >= 2:  # first call feeds the gate; then fail
                raise BrokerCommunicationError("account endpoint down")
            return super().get_account_state()

    broker = FlakyAccountBroker()
    make_reissue_child(db_path, make_broker())  # scope of a healthy connection
    service, _, _ = make_submission_service(tmp_path, db_path, broker=broker)
    decision, result = service.submit("exec-2", market=make_market())
    assert result is None
    assert decision.rejection_codes == ("reissue_clearance_invalid",)
    assert broker.submitted_requests == []


def test_a_stale_clearance_is_refused_at_the_submission_instant(tmp_path, db_path):
    """鮮度切れ: the clearance was fresh at registration but is older than
    the freshness window at the submission instant."""
    broker = make_broker(seconds=1005)
    make_reissue_child(db_path, broker)  # observed at at(5)
    service, _, _ = make_submission_service(
        tmp_path,
        db_path,
        broker=broker,
        clock=lambda: at(1000),
        clearance_max_age=timedelta(seconds=60),
    )
    decision, result = service.submit("exec-2", market=make_market(seconds=995))
    assert result is None
    assert decision.rejection_codes == ("reissue_clearance_invalid",)


def test_a_revoked_clearance_is_refused_at_the_submission_instant(tmp_path, db_path):
    """観測の反映が送信可否に必ず先行する: a revocation recorded after the
    registration blocks the submit."""
    broker = make_broker()
    make_reissue_child(db_path, broker)
    record_clearance(
        db_path,
        "sig-exec-1",
        status=store.CLEARANCE_STATUS_REVOKED,
        account_scope=derive_account_scope(broker),
    )
    service, _, _ = make_submission_service(tmp_path, db_path, broker=broker)
    decision, result = service.submit("exec-2", market=make_market())
    assert result is None
    assert decision.rejection_codes == ("reissue_clearance_invalid",)
    assert broker.submitted_requests == []


# --- intent_superseded --------------------------------------------------------


def test_a_superseded_parent_submit_is_refused_and_its_intent_terminalized(
    tmp_path, db_path
):
    """差し替え済み親の送信は intent_superseded で拒否され、競合で journal
    に記録された未送信 intent は終端状態へ収束する (the race where the
    parent's submit journals its intent and only then loses
    ``mark_submitting`` to the supersession)."""
    from test_execution_store import create_order

    create_order(db_path)  # parent exec-1, phase created
    gate_db, reference = gate_reference(tmp_path)
    register_child(
        db_path,
        kind=store.REISSUE_KIND_GATE_CORRECTION,
        reason="operator op-1: reduce quantity",
        gate_correction_reference=reference,
        gate_decision_db_path=gate_db,
        quantity=Decimal("5"),
    )
    service, broker, journal_db = make_submission_service(tmp_path, db_path)
    decision, result = service.submit("exec-1", market=make_market())
    assert result is None
    assert decision.rejection_codes == ("intent_superseded",)
    assert broker.submitted_requests == []  # the broker was never called

    journal = SubmissionJournal(journal_db)
    entry = journal.get_entry("exec-1")
    assert entry is not None
    assert entry.broker_call_state == "not_started"
    assert entry.outcome == OUTCOME_SUPERSEDED
    assert entry.superseded_by_execution_request_id == "exec-2"
    # 恒久残存しない: the terminal entry is no longer reported unresolved
    assert all(e.execution_request_id != "exec-1" for e in journal.list_unresolved())


def test_journal_terminalization_failure_keeps_the_refusal_and_the_sweep_recovers(
    tmp_path, db_path, monkeypatch
):
    """cleanup 失敗でも二重送信防止契約は破られない: the refusal stands, the
    entry stays visible as unresolved, and the durable sweep converges it."""
    from test_execution_store import create_order

    create_order(db_path)
    gate_db, reference = gate_reference(tmp_path)
    register_child(
        db_path,
        kind=store.REISSUE_KIND_GATE_CORRECTION,
        reason="operator op-1: reduce quantity",
        gate_correction_reference=reference,
        gate_decision_db_path=gate_db,
        quantity=Decimal("5"),
    )
    service, broker, journal_db = make_submission_service(tmp_path, db_path)
    monkeypatch.setattr(
        SubmissionJournal,
        "mark_superseded",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected")),
    )
    decision, result = service.submit("exec-1", market=make_market())
    assert result is None
    assert decision.rejection_codes == ("intent_superseded",)
    monkeypatch.undo()

    journal = SubmissionJournal(journal_db)
    assert journal.get_entry("exec-1").outcome == "open"  # cleanup failed
    assert [e.execution_request_id for e in journal.list_unresolved()] == ["exec-1"]

    from execution.reissue_service import ReissueService

    sweep = ReissueService(
        execution_db_path=db_path,
        journal_db_path=journal_db,
        decision_db_path=tmp_path / "safety_gate_decisions.sqlite3",
        current_policy=make_policy(),
        clock=lambda: at(200),
    )
    actions = sweep.terminalize_superseded_intents()
    assert actions[0]["action"] == "superseded"
    assert journal.get_entry("exec-1").outcome == OUTCOME_SUPERSEDED
    assert journal.list_unresolved() == ()


def test_attempt_one_orders_still_submit_without_clearance_inputs(tmp_path, db_path):
    """Regression: the reissue wiring must not change attempt-1 behaviour."""
    from test_execution_store import create_order

    create_order(db_path)
    service, _, _ = make_submission_service(tmp_path, db_path)
    decision, result = service.submit("exec-1", market=make_market())
    assert decision.allowed and result is not None
    assert result.state is OrderState.FILLED
