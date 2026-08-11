"""Issue #93: durable Submission Journal -- independent of the Execution
Order DB -- around the broker call of the sanctioned submission path.

tmp_path SQLite and FakeBroker only -- no network, no Discord, no S3, no real
orders. Execution-DB fault injection uses REAL failure modes wherever the
platform allows a deterministic one (file deletion, byte corruption, schema
tampering) and a genuinely read-only SQLite connection for the read-only
case (chmod is not deterministic across CI users vs. local root)."""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from broker.broker_models import AccountState, OrderSide, OrderState, OrderType
from broker.fake_broker import FakeBroker, TimeoutOrder
from execution import execution_store, sqlite_ro
from execution.execution_models import ExecutionRequest
from execution.live_submission import LiveOrderSubmissionService
from execution.reissue_service import ReissuePolicy
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
    BROKER_CALL_NOT_STARTED,
    BROKER_CALL_RESPONDED,
    BROKER_CALL_STARTED,
    BROKER_CALL_UNKNOWN,
    CANCEL_OUTCOME_BROKER_REFUSED,
    CancellationJournalEntry,
    DB_SAVE_FAILED,
    DB_SAVE_NOT_ATTEMPTED,
    DB_SAVE_SAVED,
    OUTCOME_COMPLETED,
    OUTCOME_OPEN,
    OUTCOME_RECONCILIATION_REQUIRED,
    OUTCOME_SUPERSEDED,
    SubmissionJournal,
    SubmissionJournalConflictError,
    SubmissionJournalEntry,
    SubmissionJournalError,
    SubmissionJournalIntegrityError,
    SubmissionJournalSchemaError,
)

NOW = datetime(2026, 7, 22, 1, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(seconds=1)


# --- shared builders ----------------------------------------------------------


def journal_at(tmp_path):
    journal = SubmissionJournal(tmp_path / "journal.sqlite3")
    journal.initialize()
    return journal


def intent_kwargs(**overrides):
    values = dict(
        execution_request_id="exec-1",
        client_order_id="client-1",
        symbol="USD_JPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        requested_price=None,
        execution_mode="fake",
        connection_target="fake",
        started_at_utc=NOW,
    )
    values.update(overrides)
    return values


def request(**overrides):
    values = dict(
        execution_request_id="exec-1",
        signal_id="signal-1",
        strategy_id="strategy-1",
        symbol="USD_JPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        requested_at_utc=NOW,
        requested_price=None,
        stop_loss_price=Decimal("149.5"),
        take_profit_price=Decimal("151.5"),
    )
    values.update(overrides)
    return ExecutionRequest(**values)


def market(**overrides):
    values = dict(
        symbol="USD_JPY",
        reference_price=Decimal("150"),
        bid=Decimal("149.99"),
        ask=Decimal("150.01"),
        observed_at_utc=NOW,
        market_open=True,
        tradable=True,
    )
    values.update(overrides)
    return MarketSafetySnapshot(**values)


def policy():
    return SafetyPolicy(
        policy_id="policy-test-1",
        allowed_symbols=frozenset({"USD_JPY"}),
        allowed_sides=frozenset({OrderSide.BUY, OrderSide.SELL}),
        max_order_quantity=Decimal("10"),
        max_positions_per_symbol=2,
        max_positions_account=5,
        max_reference_age=timedelta(seconds=30),
        max_spread_by_symbol={"USD_JPY": Decimal("0.05")},
        allowed_connection_targets={ExecutionMode.FAKE: frozenset({"fake"})},
    )


def make_broker(cls=FakeBroker, **kwargs):
    broker = cls(default_fill_price=Decimal("150"), clock=lambda: NOW, **kwargs)
    broker.set_account_state(
        AccountState(
            account_id="fake-account",
            currency="USD",
            balance=Decimal("100000"),
            equity=Decimal("100000"),
            margin_available=Decimal("100000"),
            retrieved_at_utc=NOW,
        )
    )
    return broker


def make_service(
    tmp_path,
    *,
    broker=None,
    initialize_journal=True,
    order_db=None,
    journal_db=None,
):
    decision_db = tmp_path / "safety.sqlite3"
    SQLiteSafetyDecisionStore(decision_db).initialize()
    journal_db = journal_db if journal_db is not None else tmp_path / "journal.sqlite3"
    if initialize_journal:
        SubmissionJournal(journal_db).initialize()
    broker = broker if broker is not None else make_broker()
    service = LiveOrderSubmissionService(
        connection=BrokerConnection(broker=broker),
        gate=SafetyGate(policy=policy(), state_provider=StaticSafetyState(), clock=lambda: NOW),
        decision_db_path=decision_db,
        order_db_path=order_db if order_db is not None else tmp_path / "orders.sqlite3",
        journal_db_path=journal_db,
        clock=lambda: NOW,
        reissue_policy=ReissuePolicy(),
    )
    return service, broker, SubmissionJournal(journal_db)


# --- journal store: lifecycle round trips ------------------------------------


def test_intent_round_trips_and_survives_reopen(tmp_path):
    journal = journal_at(tmp_path)
    journal.record_submission_intent(**intent_kwargs())

    # Simulated restart: a brand-new journal instance over the same file.
    restored = SubmissionJournal(tmp_path / "journal.sqlite3").get_entry("exec-1")
    assert restored is not None
    assert restored.client_order_id == "client-1"
    assert restored.symbol == "USD_JPY"
    assert restored.side is OrderSide.BUY
    assert restored.order_type is OrderType.MARKET
    assert restored.requested_price is None
    assert restored.quantity == Decimal("1")
    assert restored.execution_mode == "fake"
    assert restored.connection_target == "fake"
    assert restored.submission_started_at_utc == NOW
    assert restored.broker_call_state == BROKER_CALL_NOT_STARTED
    assert restored.execution_db_save_state == DB_SAVE_NOT_ATTEMPTED
    assert restored.outcome == OUTCOME_OPEN
    assert restored.broker_response_state is None
    assert restored.last_error is None


def test_full_success_lifecycle(tmp_path):
    journal = journal_at(tmp_path)
    journal.record_submission_intent(**intent_kwargs())
    journal.mark_broker_call_started("exec-1", at_utc=NOW)
    assert journal.get_entry("exec-1").broker_call_state == BROKER_CALL_STARTED
    journal.record_broker_response(
        "exec-1", response_state=OrderState.FILLED, broker_order_id="broker-1", at_utc=NOW
    )
    entry = journal.get_entry("exec-1")
    assert entry.broker_call_state == BROKER_CALL_RESPONDED
    assert entry.broker_response_state is OrderState.FILLED
    assert entry.broker_order_id == "broker-1"
    assert entry.outcome == OUTCOME_OPEN
    journal.mark_completed(
        "exec-1", response_state=OrderState.FILLED, broker_order_id="broker-1", at_utc=LATER
    )
    entry = journal.get_entry("exec-1")
    assert entry.outcome == OUTCOME_COMPLETED
    assert entry.execution_db_save_state == DB_SAVE_SAVED
    assert entry.updated_at_utc == LATER
    assert journal.list_unresolved() == ()


def test_unknown_outcome_lifecycle(tmp_path):
    journal = journal_at(tmp_path)
    journal.record_submission_intent(**intent_kwargs())
    journal.mark_broker_call_started("exec-1", at_utc=NOW)
    journal.record_broker_outcome_unknown("exec-1", error="timeout", at_utc=NOW)
    entry = journal.get_entry("exec-1")
    assert entry.broker_call_state == BROKER_CALL_UNKNOWN
    assert entry.outcome == OUTCOME_RECONCILIATION_REQUIRED
    assert entry.last_error == "timeout"
    assert [e.execution_request_id for e in journal.list_reconciliation_required()] == ["exec-1"]


def test_db_save_failed_lifecycle_records_the_broker_response(tmp_path):
    journal = journal_at(tmp_path)
    journal.record_submission_intent(**intent_kwargs())
    journal.mark_broker_call_started("exec-1", at_utc=NOW)
    journal.record_execution_db_save_failed(
        "exec-1",
        response_state=OrderState.ACCEPTED,
        broker_order_id="broker-1",
        error="order DB gone",
        at_utc=NOW,
    )
    entry = journal.get_entry("exec-1")
    assert entry.broker_call_state == BROKER_CALL_RESPONDED
    assert entry.broker_response_state is OrderState.ACCEPTED
    assert entry.broker_order_id == "broker-1"
    assert entry.execution_db_save_state == DB_SAVE_FAILED
    assert entry.outcome == OUTCOME_RECONCILIATION_REQUIRED
    assert entry.last_error == "order DB gone"


def test_completion_heals_a_lost_response_write(tmp_path):
    # started -> completed directly: the step-3 response write was lost but
    # the Execution DB save succeeded; the completion carries the response.
    journal = journal_at(tmp_path)
    journal.record_submission_intent(**intent_kwargs())
    journal.mark_broker_call_started("exec-1", at_utc=NOW)
    journal.mark_completed(
        "exec-1", response_state=OrderState.FILLED, broker_order_id="broker-1", at_utc=NOW
    )
    entry = journal.get_entry("exec-1")
    assert entry.outcome == OUTCOME_COMPLETED
    assert entry.broker_response_state is OrderState.FILLED


def test_lifecycle_preconditions_are_enforced(tmp_path):
    journal = journal_at(tmp_path)
    journal.record_submission_intent(**intent_kwargs())
    with pytest.raises(SubmissionJournalError, match="requires one of"):
        journal.record_broker_response(
            "exec-1", response_state=OrderState.FILLED, broker_order_id="b", at_utc=NOW
        )
    with pytest.raises(SubmissionJournalError, match="requires one of"):
        journal.mark_completed(
            "exec-1", response_state=OrderState.FILLED, broker_order_id="b", at_utc=NOW
        )
    with pytest.raises(SubmissionJournalError, match="requires one of"):
        journal.record_broker_outcome_unknown("exec-1", error="x", at_utc=NOW)
    journal.mark_broker_call_started("exec-1", at_utc=NOW)
    with pytest.raises(SubmissionJournalError, match="requires one of"):
        journal.mark_broker_call_started("exec-1", at_utc=NOW)
    with pytest.raises(SubmissionJournalError, match="no journal entry"):
        journal.mark_broker_call_started("exec-other", at_utc=NOW)


def test_updates_must_not_precede_the_stored_update_time(tmp_path):
    journal = journal_at(tmp_path)
    journal.record_submission_intent(**intent_kwargs(started_at_utc=LATER))
    with pytest.raises(SubmissionJournalError, match="precedes"):
        journal.mark_broker_call_started("exec-1", at_utc=NOW)


# --- journal store: duplicate-send prevention --------------------------------


def test_identical_not_started_intent_is_reclaimed_not_duplicated(tmp_path):
    journal = journal_at(tmp_path)
    journal.record_submission_intent(**intent_kwargs())
    journal.record_submission_intent(**intent_kwargs(started_at_utc=LATER))
    entry = journal.get_entry("exec-1")
    assert entry.broker_call_state == BROKER_CALL_NOT_STARTED
    assert entry.submission_started_at_utc == LATER


@pytest.mark.parametrize(
    "advance",
    [
        lambda j: j.mark_broker_call_started("exec-1", at_utc=NOW),
        lambda j: (
            j.mark_broker_call_started("exec-1", at_utc=NOW),
            j.record_broker_outcome_unknown("exec-1", error="lost", at_utc=NOW),
        ),
        lambda j: (
            j.mark_broker_call_started("exec-1", at_utc=NOW),
            j.mark_completed(
                "exec-1", response_state=OrderState.FILLED, broker_order_id="b1", at_utc=NOW
            ),
        ),
    ],
)
def test_intent_conflicts_once_a_broker_call_may_have_happened(tmp_path, advance):
    journal = journal_at(tmp_path)
    journal.record_submission_intent(**intent_kwargs())
    advance(journal)
    with pytest.raises(SubmissionJournalConflictError, match="re-sending is banned"):
        journal.record_submission_intent(**intent_kwargs(started_at_utc=LATER))


def test_intent_conflicts_on_payload_mismatch(tmp_path):
    journal = journal_at(tmp_path)
    journal.record_submission_intent(**intent_kwargs())
    for mismatch in (
        {"client_order_id": "client-other"},
        {"quantity": Decimal("2")},
        {"side": OrderSide.SELL},
        {"symbol": "EUR_USD"},
        {"connection_target": "other-target"},
        # PR #113 review P1: order CONTENT changes -- MARKET turned LIMIT --
        # must conflict, never silently reclaim the journaled attempt.
        {"order_type": OrderType.LIMIT, "requested_price": Decimal("150")},
    ):
        with pytest.raises(SubmissionJournalConflictError, match="payload differs"):
            journal.record_submission_intent(**intent_kwargs(**mismatch))


def test_intent_conflicts_on_a_moved_limit_price(tmp_path):
    # PR #113 review P1: for a LIMIT intent the journaled broker-bound
    # payload includes the limit price -- a moved price is a different order.
    journal = journal_at(tmp_path)
    journal.record_submission_intent(
        **intent_kwargs(order_type=OrderType.LIMIT, requested_price=Decimal("150"))
    )
    with pytest.raises(SubmissionJournalConflictError, match="payload differs"):
        journal.record_submission_intent(
            **intent_kwargs(
                order_type=OrderType.LIMIT,
                requested_price=Decimal("151"),
                started_at_utc=LATER,
            )
        )
    # The identical LIMIT payload still reclaims.
    journal.record_submission_intent(
        **intent_kwargs(
            order_type=OrderType.LIMIT, requested_price=Decimal("150"), started_at_utc=LATER
        )
    )
    entry = journal.get_entry("exec-1")
    assert entry.order_type is OrderType.LIMIT
    assert entry.requested_price == Decimal("150")
    assert entry.submission_started_at_utc == LATER


def test_reclaim_refuses_a_backward_timestamp(tmp_path):
    # PR #113 review P2: the reclaim path enforces the same timestamp
    # monotonicity as every other journal update.
    journal = journal_at(tmp_path)
    journal.record_submission_intent(**intent_kwargs(started_at_utc=LATER))
    with pytest.raises(SubmissionJournalError, match="refusing to reclaim backwards"):
        journal.record_submission_intent(**intent_kwargs(started_at_utc=NOW))
    entry = journal.get_entry("exec-1")
    assert entry.submission_started_at_utc == LATER
    assert entry.updated_at_utc == LATER


def test_intent_conflicts_when_client_order_id_belongs_to_another_request(tmp_path):
    journal = journal_at(tmp_path)
    journal.record_submission_intent(**intent_kwargs())
    with pytest.raises(SubmissionJournalConflictError, match="already journaled"):
        journal.record_submission_intent(
            **intent_kwargs(execution_request_id="exec-2", client_order_id="client-1")
        )


# --- journal store: unresolved listing ----------------------------------------


def test_list_unresolved_excludes_completed_and_orders_by_update_time(tmp_path):
    journal = journal_at(tmp_path)
    journal.record_submission_intent(**intent_kwargs())
    journal.record_submission_intent(
        **intent_kwargs(
            execution_request_id="exec-2", client_order_id="client-2", started_at_utc=LATER
        )
    )
    journal.record_submission_intent(
        **intent_kwargs(execution_request_id="exec-3", client_order_id="client-3")
    )
    journal.mark_broker_call_started("exec-3", at_utc=NOW)
    journal.mark_completed(
        "exec-3", response_state=OrderState.FILLED, broker_order_id="b3", at_utc=NOW
    )
    unresolved = SubmissionJournal(tmp_path / "journal.sqlite3").list_unresolved()
    assert [entry.execution_request_id for entry in unresolved] == ["exec-1", "exec-2"]
    assert journal.list_reconciliation_required() == ()


# --- journal store: missing DB / schema / fail-closed -------------------------


def test_writes_on_missing_db_raise_and_create_no_file(tmp_path):
    path = tmp_path / "never-created.sqlite3"
    journal = SubmissionJournal(path)
    with pytest.raises(SubmissionJournalError, match="does not exist"):
        journal.record_submission_intent(**intent_kwargs())
    with pytest.raises(SubmissionJournalError, match="does not exist"):
        journal.mark_broker_call_started("exec-1", at_utc=NOW)
    assert not path.exists()


def test_reads_on_missing_db_are_empty_and_create_no_file(tmp_path):
    path = tmp_path / "never-created.sqlite3"
    journal = SubmissionJournal(path)
    assert journal.get_entry("exec-1") is None
    assert journal.list_unresolved() == ()
    assert journal.list_reconciliation_required() == ()
    assert not path.exists()


def test_initialize_is_idempotent_and_refuses_a_version_only_db(tmp_path):
    journal = journal_at(tmp_path)
    journal.initialize()
    other = tmp_path / "version-only.sqlite3"
    with sqlite3.connect(other) as connection:
        connection.execute("PRAGMA user_version = 7")
    before = other.read_bytes()
    with pytest.raises(SubmissionJournalSchemaError, match="not a pristine DB"):
        SubmissionJournal(other).initialize()
    assert other.read_bytes() == before


def test_foreign_schema_is_rejected_without_modification_by_every_api(tmp_path):
    path = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
    before = path.read_bytes()
    journal = SubmissionJournal(path)
    with pytest.raises(SubmissionJournalSchemaError):
        journal.initialize()
    with pytest.raises(SubmissionJournalSchemaError):
        journal.record_submission_intent(**intent_kwargs())
    with pytest.raises(SubmissionJournalSchemaError):
        journal.list_unresolved()
    assert path.read_bytes() == before


def test_constraint_stripped_lookalike_schema_is_rejected(tmp_path):
    path = tmp_path / "lookalike.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE submission_journal ("
            "execution_request_id TEXT PRIMARY KEY, client_order_id TEXT, symbol TEXT, "
            "side TEXT, quantity TEXT, execution_mode TEXT, connection_target TEXT, "
            "submission_started_at_utc TEXT, broker_call_state TEXT, "
            "broker_response_state TEXT, broker_order_id TEXT, "
            "execution_db_save_state TEXT, outcome TEXT, last_error TEXT, "
            "updated_at_utc TEXT)"
        )
        connection.execute(
            "CREATE INDEX idx_submission_journal_outcome ON "
            "submission_journal (outcome, updated_at_utc)"
        )
        connection.execute("PRAGMA user_version = 1")
    with pytest.raises(SubmissionJournalSchemaError):
        SubmissionJournal(path).record_submission_intent(**intent_kwargs())


def test_unexpected_trigger_is_rejected_by_reads_and_writes(tmp_path):
    journal = journal_at(tmp_path)
    with sqlite3.connect(journal.path) as connection:
        connection.execute(
            "CREATE TRIGGER sneak AFTER INSERT ON submission_journal "
            "BEGIN DELETE FROM submission_journal; END"
        )
    with pytest.raises(SubmissionJournalSchemaError, match="unexpected trigger"):
        journal.record_submission_intent(**intent_kwargs())
    with pytest.raises(SubmissionJournalSchemaError):
        journal.list_unresolved()


def test_wrong_schema_version_is_rejected(tmp_path):
    journal = journal_at(tmp_path)
    with sqlite3.connect(journal.path) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(SubmissionJournalSchemaError, match="user_version"):
        journal.record_submission_intent(**intent_kwargs())


def test_out_of_band_tampering_fails_restore(tmp_path):
    journal = journal_at(tmp_path)
    journal.record_submission_intent(**intent_kwargs())
    with sqlite3.connect(journal.path) as connection:
        connection.execute("UPDATE submission_journal SET quantity = 'not-a-number'")
    with pytest.raises(SubmissionJournalIntegrityError, match="restore-time validation"):
        journal.get_entry("exec-1")
    with pytest.raises(SubmissionJournalIntegrityError):
        journal.list_unresolved()


def test_db_check_constraints_reject_inconsistent_lifecycle_rows(tmp_path):
    journal = journal_at(tmp_path)
    with sqlite3.connect(journal.path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            # completed without a saved Execution DB result
            connection.execute(
                "INSERT INTO submission_journal VALUES ("
                "'exec-x', 'client-x', 'USD_JPY', 'buy', 'market', '1', NULL, "
                "'fake', 'fake', "
                "'2026-07-22T01:00:00+00:00', 'not_started', NULL, NULL, "
                "'not_attempted', 'completed', NULL, '2026-07-22T01:00:00+00:00', NULL)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            # unknown without a recorded failure reason
            connection.execute(
                "INSERT INTO submission_journal VALUES ("
                "'exec-x', 'client-x', 'USD_JPY', 'buy', 'market', '1', NULL, "
                "'fake', 'fake', "
                "'2026-07-22T01:00:00+00:00', 'unknown', NULL, NULL, "
                "'not_attempted', 'reconciliation_required', NULL, "
                "'2026-07-22T01:00:00+00:00', NULL)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            # a MARKET row carrying a limit price (PR #113 review P1 guard)
            connection.execute(
                "INSERT INTO submission_journal VALUES ("
                "'exec-x', 'client-x', 'USD_JPY', 'buy', 'market', '1', '150', "
                "'fake', 'fake', "
                "'2026-07-22T01:00:00+00:00', 'not_started', NULL, NULL, "
                "'not_attempted', 'open', NULL, '2026-07-22T01:00:00+00:00', NULL)"
            )


def test_entry_model_requires_price_exactly_for_limit():
    kwargs = dict(
        execution_request_id="exec-1",
        client_order_id="client-1",
        symbol="USD_JPY",
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        execution_mode="fake",
        connection_target="fake",
        submission_started_at_utc=NOW,
        broker_call_state=BROKER_CALL_NOT_STARTED,
        execution_db_save_state=DB_SAVE_NOT_ATTEMPTED,
        outcome=OUTCOME_OPEN,
        updated_at_utc=NOW,
    )
    with pytest.raises(ValueError, match="requested_price"):
        SubmissionJournalEntry(
            order_type=OrderType.MARKET, requested_price=Decimal("150"), **kwargs
        )
    with pytest.raises(ValueError, match="requested_price"):
        SubmissionJournalEntry(order_type=OrderType.LIMIT, requested_price=None, **kwargs)


def test_entry_model_enforces_lifecycle_invariants():
    with pytest.raises(ValueError, match="must coincide"):
        SubmissionJournalEntry(
            execution_request_id="exec-1",
            client_order_id="client-1",
            symbol="USD_JPY",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
            execution_mode="fake",
            connection_target="fake",
            submission_started_at_utc=NOW,
            broker_call_state=BROKER_CALL_RESPONDED,
            broker_response_state=OrderState.FILLED,
            execution_db_save_state=DB_SAVE_NOT_ATTEMPTED,
            outcome=OUTCOME_COMPLETED,
            updated_at_utc=NOW,
        )
    with pytest.raises(ValueError, match="broker_response_state"):
        SubmissionJournalEntry(
            execution_request_id="exec-1",
            client_order_id="client-1",
            symbol="USD_JPY",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
            execution_mode="fake",
            connection_target="fake",
            submission_started_at_utc=NOW,
            broker_call_state=BROKER_CALL_NOT_STARTED,
            broker_response_state=OrderState.FILLED,
            execution_db_save_state=DB_SAVE_NOT_ATTEMPTED,
            outcome=OUTCOME_OPEN,
            updated_at_utc=NOW,
        )


# --- service integration: the durable record around the broker call -----------


def register_and_submit(service, req=None, *, client_order_id="client-1"):
    req = req if req is not None else request()
    service.register(req, client_order_id=client_order_id)
    return service.submit(req.execution_request_id, market=market())


def test_successful_submission_journals_a_completed_entry(tmp_path):
    service, broker, journal = make_service(tmp_path)
    decision, result = register_and_submit(service)
    assert decision.allowed and result is not None
    assert len(broker.submitted_requests) == 1
    entry = journal.get_entry("exec-1")
    assert entry.outcome == OUTCOME_COMPLETED
    assert entry.broker_call_state == BROKER_CALL_RESPONDED
    assert entry.broker_response_state is result.state
    assert entry.broker_order_id == result.broker_order_id
    assert entry.execution_db_save_state == DB_SAVE_SAVED
    assert journal.list_unresolved() == ()


def test_missing_journal_is_fail_closed_before_any_broker_call(tmp_path):
    service, broker, journal = make_service(tmp_path, initialize_journal=False)
    decision, result = register_and_submit(service)
    assert result is None
    assert decision.rejection_codes == ("submission_journal_failed",)
    assert broker.submitted_requests == []
    assert not journal.path.exists()


def test_broken_journal_schema_is_fail_closed_before_any_broker_call(tmp_path):
    journal_db = tmp_path / "journal.sqlite3"
    with sqlite3.connect(journal_db) as connection:
        connection.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
    service, broker, _ = make_service(
        tmp_path, initialize_journal=False, journal_db=journal_db
    )
    decision, result = register_and_submit(service)
    assert result is None
    assert decision.rejection_codes == ("submission_journal_failed",)
    assert broker.submitted_requests == []


def test_failed_broker_call_start_mark_blocks_the_broker_call(tmp_path, monkeypatch):
    service, broker, journal = make_service(tmp_path)

    def broken(self, execution_request_id, *, at_utc):
        raise RuntimeError("journal write lost")

    monkeypatch.setattr(SubmissionJournal, "mark_broker_call_started", broken)
    decision, result = register_and_submit(service)
    assert result is None
    assert decision.rejection_codes == ("submission_journal_failed",)
    assert broker.submitted_requests == []
    # The journal entry proves the broker was never called ...
    monkeypatch.undo()
    entry = journal.get_entry("exec-1")
    assert entry.broker_call_state == BROKER_CALL_NOT_STARTED
    # ... while the order row (already past created -> submitting) stays
    # visible to reconciliation as an unresolved submission.
    unresolved = execution_store.list_unresolved_submissions(db_path=tmp_path / "orders.sqlite3")
    assert [row["execution_request_id"] for row in unresolved] == ["exec-1"]


# --- service integration: Execution DB unavailability after the broker call ---


class FaultAfterResponseBroker(FakeBroker):
    """A FakeBroker whose submit_order triggers an Execution-DB fault AFTER
    producing the (successful) broker response -- the Issue #93 window."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fault = lambda: None

    def submit_order(self, order_request):
        result = super().submit_order(order_request)
        self.fault()
        return result


def corrupting_service(tmp_path, fault_builder):
    broker = make_broker(cls=FaultAfterResponseBroker, connection_target="fake")
    service, _, journal = make_service(tmp_path, broker=broker)
    order_db = tmp_path / "orders.sqlite3"
    broker.fault = fault_builder(order_db)
    return service, broker, journal, order_db


def _delete_file(order_db):
    return lambda: order_db.unlink()


def _corrupt_bytes(order_db):
    return lambda: order_db.write_bytes(b"this is no longer a sqlite database")


def _tamper_schema(order_db):
    def fault():
        with sqlite3.connect(order_db) as connection:
            connection.execute(
                "CREATE TRIGGER sneak AFTER UPDATE ON execution_orders "
                "BEGIN DELETE FROM execution_order_state_history; END"
            )

    return fault


@pytest.mark.parametrize(
    "fault_builder, expected_error",
    [
        (_delete_file, "file loss"),
        (_corrupt_bytes, "corruption"),
        (_tamper_schema, "schema damage"),
    ],
    ids=["file-loss", "byte-corruption", "schema-tamper"],
)
def test_execution_db_unavailable_after_acceptance_leaves_a_durable_journal_record(
    tmp_path, fault_builder, expected_error
):
    service, broker, journal, order_db = corrupting_service(tmp_path, fault_builder)
    with pytest.raises(Exception):
        register_and_submit(service)
    assert len(broker.submitted_requests) == 1

    # The journal -- independent of the broken order DB -- durably holds the
    # broker response and flags reconciliation_required, machine-readably.
    entry = journal.get_entry("exec-1")
    assert entry.broker_call_state == BROKER_CALL_RESPONDED
    assert entry.broker_response_state is OrderState.FILLED
    assert entry.broker_order_id is not None
    assert entry.execution_db_save_state == DB_SAVE_FAILED
    assert entry.outcome == OUTCOME_RECONCILIATION_REQUIRED
    assert entry.last_error is not None

    # Restart equivalent: a fresh journal instance still lists the entry
    # while the Execution DB stays unusable.
    reopened = SubmissionJournal(journal.path)
    listed = reopened.list_reconciliation_required()
    assert [e.execution_request_id for e in listed] == ["exec-1"]
    assert [e.execution_request_id for e in reopened.list_unresolved()] == ["exec-1"]


def test_execution_db_read_only_after_acceptance_leaves_a_durable_journal_record(
    tmp_path, monkeypatch
):
    """Read-only Execution DB: from the fault point on, every order-store
    connection is a TRUE read-only SQLite connection (deterministic across
    root/non-root environments, unlike chmod)."""
    read_only = {"active": False}
    real_connect = execution_store._connect

    def maybe_read_only(db_path):
        if read_only["active"]:
            connection = sqlite_ro.connect_read_only(db_path)
            connection.row_factory = sqlite3.Row
            return connection
        return real_connect(db_path)

    monkeypatch.setattr(execution_store, "_connect", maybe_read_only)

    def fault_builder(order_db):
        return lambda: read_only.update(active=True)

    service, broker, journal, _ = corrupting_service(tmp_path, fault_builder)
    with pytest.raises(Exception):
        register_and_submit(service)
    assert len(broker.submitted_requests) == 1
    entry = journal.get_entry("exec-1")
    assert entry.execution_db_save_state == DB_SAVE_FAILED
    assert entry.outcome == OUTCOME_RECONCILIATION_REQUIRED


def test_no_resubmission_after_reconciliation_required_even_with_a_fresh_order_db(tmp_path):
    """The journal's own duplicate guard: even when the Execution DB is
    wiped and the same intent re-registered from scratch (the execution
    store's phase guard is gone), the second submit never reaches the
    broker."""
    service, broker, journal, order_db = corrupting_service(tmp_path, _delete_file)
    with pytest.raises(Exception):
        register_and_submit(service)
    assert len(broker.submitted_requests) == 1

    # Process restart with a WIPED order DB: re-register the same intent.
    broker.fault = lambda: None
    restarted, _, _ = make_service(tmp_path, broker=broker)
    decision, result = register_and_submit(restarted)
    assert result is None
    assert decision.rejection_codes == ("duplicate_submission",)
    assert len(broker.submitted_requests) == 1


def test_unknown_broker_outcome_is_journaled_and_never_resent(tmp_path):
    service, broker, journal = make_service(tmp_path)
    broker.script_order("client-1", TimeoutOrder())
    with pytest.raises(Exception):
        register_and_submit(service)
    entry = journal.get_entry("exec-1")
    assert entry.broker_call_state == BROKER_CALL_UNKNOWN
    assert entry.outcome == OUTCOME_RECONCILIATION_REQUIRED
    assert len(broker.submitted_requests) == 1

    # UNKNOWN is resolved by queries, never by re-sending: a restarted
    # service refuses the retry and the broker sees no second call.
    restarted, _, _ = make_service(tmp_path, broker=broker)
    decision, result = restarted.submit("exec-1", market=market())
    assert result is None
    assert decision.rejection_codes == ("duplicate_submission",)
    assert len(broker.submitted_requests) == 1


def test_post_broker_journal_failures_never_block_the_outcome(tmp_path, monkeypatch, caplog):
    """After the broker call, journal failures are logged critically but the
    submission outcome is still recorded and returned."""
    service, broker, journal = make_service(tmp_path)

    def broken(self, *args, **kwargs):
        raise RuntimeError("journal down")

    monkeypatch.setattr(SubmissionJournal, "record_broker_response", broken)
    monkeypatch.setattr(SubmissionJournal, "mark_completed", broken)
    with caplog.at_level(logging.CRITICAL, logger="execution.live_submission"):
        decision, result = register_and_submit(service)
    assert decision.allowed and result is not None
    assert len(broker.submitted_requests) == 1
    assert "failed to journal" in caplog.text
    # The order result IS in the Execution DB ...
    row = execution_store.get_execution_order("exec-1", db_path=tmp_path / "orders.sqlite3")
    assert row["order_state"] == "filled"
    # ... and the stuck-open journal entry stays visible for reconciliation.
    monkeypatch.undo()
    assert [e.execution_request_id for e in journal.list_unresolved()] == ["exec-1"]
    assert journal.get_entry("exec-1").broker_call_state == BROKER_CALL_STARTED


def test_lost_response_write_is_healed_by_the_completion_write(tmp_path, monkeypatch):
    service, broker, journal = make_service(tmp_path)

    def broken(self, *args, **kwargs):
        raise RuntimeError("journal write lost")

    monkeypatch.setattr(SubmissionJournal, "record_broker_response", broken)
    decision, result = register_and_submit(service)
    assert decision.allowed and result is not None
    monkeypatch.undo()
    entry = journal.get_entry("exec-1")
    assert entry.outcome == OUTCOME_COMPLETED
    assert entry.broker_response_state is result.state


def test_retry_after_a_pre_broker_transition_failure_reclaims_the_intent(
    tmp_path, monkeypatch
):
    """A store failure AFTER the intent was journaled but BEFORE any broker
    call leaves a not_started entry; the retry reclaims it (the broker was
    provably never called) and submits exactly once."""
    service, broker, journal = make_service(tmp_path)
    real_mark_submitting = execution_store.mark_submitting
    calls = {"n": 0}

    def failing_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise execution_store.ExecutionStoreError("transient store failure")
        return real_mark_submitting(*args, **kwargs)

    monkeypatch.setattr(execution_store, "mark_submitting", failing_once)
    decision, result = register_and_submit(service)
    assert result is None
    assert decision.rejection_codes == ("state_query_failed",)
    assert broker.submitted_requests == []
    assert journal.get_entry("exec-1").broker_call_state == BROKER_CALL_NOT_STARTED

    decision, result = service.submit("exec-1", market=market())
    assert decision.allowed and result is not None
    assert len(broker.submitted_requests) == 1
    assert journal.get_entry("exec-1").outcome == OUTCOME_COMPLETED


def test_unresolved_entries_survive_a_process_restart(tmp_path):
    service, broker, journal, _ = corrupting_service(tmp_path, _delete_file)
    with pytest.raises(Exception):
        register_and_submit(service)
    # Everything re-created from the files alone (restart equivalent).
    reopened = SubmissionJournal(tmp_path / "journal.sqlite3")
    entries = reopened.list_unresolved()
    assert [e.execution_request_id for e in entries] == ["exec-1"]
    assert entries[0].outcome == OUTCOME_RECONCILIATION_REQUIRED
    assert entries[0].client_order_id == "client-1"


# --- Issue #116: the terminal ``superseded`` outcome --------------------------


def _record_not_started(journal, request_id="exec-1", client_order_id="client-1"):
    journal.record_submission_intent(
        **intent_kwargs(execution_request_id=request_id, client_order_id=client_order_id)
    )


def test_mark_superseded_terminalizes_a_not_started_intent(tmp_path):
    journal = journal_at(tmp_path)
    _record_not_started(journal)
    assert journal.mark_superseded(
        "exec-1", superseded_by_execution_request_id="exec-2", at_utc=LATER
    ) is True
    entry = journal.get_entry("exec-1")
    assert entry.outcome == OUTCOME_SUPERSEDED
    assert entry.broker_call_state == BROKER_CALL_NOT_STARTED
    assert entry.superseded_by_execution_request_id == "exec-2"
    # 恒久残存しない: terminal entries leave the unresolved report
    assert journal.list_unresolved() == ()


def test_mark_superseded_is_idempotent_for_the_same_provenance(tmp_path):
    journal = journal_at(tmp_path)
    _record_not_started(journal)
    journal.mark_superseded("exec-1", superseded_by_execution_request_id="exec-2", at_utc=LATER)
    assert journal.mark_superseded(
        "exec-1", superseded_by_execution_request_id="exec-2", at_utc=LATER
    ) is False  # no-op: recovery sweeps re-run safely


def test_mark_superseded_conflicts_on_a_different_provenance(tmp_path):
    journal = journal_at(tmp_path)
    _record_not_started(journal)
    journal.mark_superseded("exec-1", superseded_by_execution_request_id="exec-2", at_utc=LATER)
    with pytest.raises(SubmissionJournalConflictError, match="already superseded by"):
        journal.mark_superseded(
            "exec-1", superseded_by_execution_request_id="exec-3", at_utc=LATER
        )


@pytest.mark.parametrize("progress", ["started", "responded", "unknown", "completed"])
def test_mark_superseded_never_touches_a_possibly_sent_intent(tmp_path, progress):
    """Broker 到達可能性がある intent は終端化できない (fail-closed)."""
    journal = journal_at(tmp_path)
    _record_not_started(journal)
    journal.mark_broker_call_started("exec-1", at_utc=LATER)
    if progress == "responded":
        journal.record_broker_response(
            "exec-1", response_state=OrderState.ACCEPTED, broker_order_id="B-1", at_utc=LATER
        )
    elif progress == "unknown":
        journal.record_broker_outcome_unknown("exec-1", error="lost", at_utc=LATER)
    elif progress == "completed":
        journal.mark_completed(
            "exec-1", response_state=OrderState.FILLED, broker_order_id="B-1", at_utc=LATER
        )
    with pytest.raises(SubmissionJournalError, match="only a 'not_started' entry"):
        journal.mark_superseded(
            "exec-1", superseded_by_execution_request_id="exec-2", at_utc=LATER
        )


def test_mark_superseded_requires_an_existing_entry_and_monotonic_time(tmp_path):
    journal = journal_at(tmp_path)
    with pytest.raises(SubmissionJournalError, match="nothing to supersede"):
        journal.mark_superseded(
            "exec-1", superseded_by_execution_request_id="exec-2", at_utc=NOW
        )
    _record_not_started(journal)
    with pytest.raises(SubmissionJournalError, match="precedes the stored"):
        journal.mark_superseded(
            "exec-1",
            superseded_by_execution_request_id="exec-2",
            at_utc=NOW - timedelta(seconds=1),
        )
    with pytest.raises(SubmissionJournalError, match="cannot be superseded by itself"):
        journal.mark_superseded(
            "exec-1", superseded_by_execution_request_id="exec-1", at_utc=LATER
        )


def test_a_superseded_entry_is_never_reclaimed_and_never_progresses(tmp_path):
    """終端化は送信可能性を増やさない: neither the reclaim path nor any
    lifecycle transition accepts a superseded entry."""
    journal = journal_at(tmp_path)
    _record_not_started(journal)
    journal.mark_superseded("exec-1", superseded_by_execution_request_id="exec-2", at_utc=LATER)
    with pytest.raises(SubmissionJournalConflictError, match="never reclaimed"):
        _record_not_started(journal)
    with pytest.raises(SubmissionJournalError, match="terminally"):
        journal.mark_broker_call_started("exec-1", at_utc=LATER + timedelta(seconds=1))


def test_v2_db_checks_reject_inconsistent_superseded_rows(tmp_path):
    journal = journal_at(tmp_path)
    with sqlite3.connect(journal.path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            # superseded after the broker call may have happened
            connection.execute(
                "INSERT INTO submission_journal VALUES ("
                "'exec-x', 'client-x', 'USD_JPY', 'buy', 'market', '1', NULL, "
                "'fake', 'fake', "
                "'2026-07-22T01:00:00+00:00', 'started', NULL, NULL, "
                "'not_attempted', 'superseded', NULL, '2026-07-22T01:00:00+00:00', "
                "'exec-y')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            # superseded without naming the superseding request
            connection.execute(
                "INSERT INTO submission_journal VALUES ("
                "'exec-x', 'client-x', 'USD_JPY', 'buy', 'market', '1', NULL, "
                "'fake', 'fake', "
                "'2026-07-22T01:00:00+00:00', 'not_started', NULL, NULL, "
                "'not_attempted', 'superseded', NULL, '2026-07-22T01:00:00+00:00', NULL)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            # a superseding reference on a non-superseded outcome
            connection.execute(
                "INSERT INTO submission_journal VALUES ("
                "'exec-x', 'client-x', 'USD_JPY', 'buy', 'market', '1', NULL, "
                "'fake', 'fake', "
                "'2026-07-22T01:00:00+00:00', 'not_started', NULL, NULL, "
                "'not_attempted', 'open', NULL, '2026-07-22T01:00:00+00:00', 'exec-y')"
            )


# --- Issue #116: v1 -> v2 migration ------------------------------------------


def _build_v1_journal(path):
    """A released-v1 journal DB holding one not_started and one completed
    row (written through the v1 schema directly)."""
    from execution import submission_journal as sj

    connection = sqlite3.connect(path)
    try:
        sj._create_schema(connection, version=1)
        connection.execute(
            "INSERT INTO submission_journal VALUES ("
            "'exec-1', 'client-1', 'USD_JPY', 'buy', 'market', '1', NULL, "
            "'fake', 'fake', "
            "'2026-07-22T01:00:00+00:00', 'not_started', NULL, NULL, "
            "'not_attempted', 'open', NULL, '2026-07-22T01:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO submission_journal VALUES ("
            "'exec-2', 'client-2', 'USD_JPY', 'buy', 'market', '1', NULL, "
            "'fake', 'fake', "
            "'2026-07-22T01:00:00+00:00', 'responded', 'filled', 'B-1', "
            "'saved', 'completed', NULL, '2026-07-22T01:00:01+00:00')"
        )
        connection.commit()
    finally:
        connection.close()


def test_v1_to_v2_migration_rebuilds_and_keeps_every_row(tmp_path):
    path = tmp_path / "journal.sqlite3"
    _build_v1_journal(path)
    journal = SubmissionJournal(path)
    journal.initialize()

    with sqlite3.connect(path) as connection:
        # v1 chains through v2 up to the current version (v3, Issue #99)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        rows = connection.execute(
            "SELECT execution_request_id, outcome, superseded_by_execution_request_id "
            "FROM submission_journal ORDER BY execution_request_id"
        ).fetchall()
    assert rows == [("exec-1", "open", None), ("exec-2", "completed", None)]
    # migrated rows restore through the normal validated read path
    assert journal.get_entry("exec-1").broker_call_state == BROKER_CALL_NOT_STARTED
    assert journal.get_entry("exec-2").outcome == OUTCOME_COMPLETED
    journal.initialize()  # idempotent
    # and the v2 lifecycle works on the migrated DB
    journal.mark_superseded("exec-1", superseded_by_execution_request_id="exec-9", at_utc=LATER)
    assert journal.get_entry("exec-1").outcome == OUTCOME_SUPERSEDED


def test_v1_to_v2_migration_rolls_back_completely_on_failure(tmp_path, monkeypatch):
    from execution import submission_journal as sj

    path = tmp_path / "journal.sqlite3"
    _build_v1_journal(path)
    with sqlite3.connect(path) as connection:
        before = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        rows_before = connection.execute(
            "SELECT * FROM submission_journal ORDER BY execution_request_id"
        ).fetchall()

    real_verify = sj._verify_schema

    def fail_v2_verification(connection, db_path, *, version=sj.SCHEMA_VERSION):
        if version == sj.SCHEMA_VERSION:
            raise sj.SubmissionJournalSchemaError("injected post-migration failure")
        return real_verify(connection, db_path, version=version)

    monkeypatch.setattr(sj, "_verify_schema", fail_v2_verification)
    with pytest.raises(SubmissionJournalSchemaError, match="injected"):
        SubmissionJournal(path).initialize()
    monkeypatch.undo()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY name"
        ).fetchall() == before
        assert connection.execute(
            "SELECT * FROM submission_journal ORDER BY execution_request_id"
        ).fetchall() == rows_before
    # and a clean retry succeeds
    SubmissionJournal(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3


# --- cancellation journal (Issue #99 / PR #124 review P1) ---------------------


def make_cancel_journal(tmp_path) -> SubmissionJournal:
    journal = SubmissionJournal(tmp_path / "journal.sqlite3")
    journal.initialize()
    return journal


def claim(journal, *, at=NOW, execution_request_id="exec-1", client_order_id="client-1"):
    journal.record_cancellation_intent(
        execution_request_id=execution_request_id,
        client_order_id=client_order_id,
        started_at_utc=at,
    )


def test_cancel_claim_round_trip_to_completed(tmp_path):
    journal = make_cancel_journal(tmp_path)
    claim(journal)
    entry = journal.get_cancellation_entry("exec-1")
    assert entry.broker_call_state == BROKER_CALL_NOT_STARTED
    assert entry.outcome == OUTCOME_OPEN and entry.attempt_count == 1
    journal.mark_cancel_broker_call_started("exec-1", at_utc=LATER)
    journal.record_cancel_broker_response(
        "exec-1", response_state=OrderState.CANCELLED, broker_order_id="B-1", at_utc=LATER
    )
    journal.mark_cancel_completed(
        "exec-1", response_state=OrderState.CANCELLED, broker_order_id="B-1", at_utc=LATER
    )
    entry = journal.get_cancellation_entry("exec-1")
    assert entry.outcome == OUTCOME_COMPLETED
    assert entry.execution_db_save_state == DB_SAVE_SAVED
    assert entry.broker_response_state is OrderState.CANCELLED
    assert journal.list_unresolved_cancellations() == ()
    # survives reopen (process-restart equivalence)
    assert SubmissionJournal(journal.path).get_cancellation_entry("exec-1").outcome == (
        OUTCOME_COMPLETED
    )


def test_cancel_claim_is_not_reacquirable_once_started(tmp_path):
    """The P1 crash window: a caller that died after the broker cancel left a
    ``started`` claim; no restarted caller may re-acquire it."""
    journal = make_cancel_journal(tmp_path)
    claim(journal)
    journal.mark_cancel_broker_call_started("exec-1", at_utc=LATER)
    with pytest.raises(SubmissionJournalConflictError, match="progressed"):
        claim(journal, at=LATER + timedelta(seconds=1))
    # started claims stay visible for reconciliation
    unresolved = journal.list_unresolved_cancellations()
    assert [entry.execution_request_id for entry in unresolved] == ["exec-1"]


def test_cancel_claim_conflicts_after_unknown_and_save_failed(tmp_path):
    journal = make_cancel_journal(tmp_path)
    claim(journal)
    journal.mark_cancel_broker_call_started("exec-1", at_utc=LATER)
    journal.record_cancel_broker_outcome_unknown("exec-1", error="timeout", at_utc=LATER)
    entry = journal.get_cancellation_entry("exec-1")
    assert entry.outcome == OUTCOME_RECONCILIATION_REQUIRED
    assert entry.broker_call_state == BROKER_CALL_UNKNOWN
    with pytest.raises(SubmissionJournalConflictError):
        claim(journal, at=LATER)

    claim(journal, execution_request_id="exec-2", client_order_id="client-2")
    journal.mark_cancel_broker_call_started("exec-2", at_utc=LATER)
    journal.record_cancel_execution_db_save_failed(
        "exec-2",
        response_state=OrderState.CANCELLED,
        broker_order_id="B-2",
        error="disk full",
        at_utc=LATER,
    )
    entry = journal.get_cancellation_entry("exec-2")
    assert entry.outcome == OUTCOME_RECONCILIATION_REQUIRED
    assert entry.execution_db_save_state == DB_SAVE_FAILED
    assert entry.broker_response_state is OrderState.CANCELLED
    with pytest.raises(SubmissionJournalConflictError):
        claim(journal, execution_request_id="exec-2", client_order_id="client-2", at=LATER)
    assert [e.execution_request_id for e in journal.list_unresolved_cancellations()] == [
        "exec-1",
        "exec-2",
    ]


def test_cancel_not_started_claim_is_reclaimable_but_never_backwards(tmp_path):
    journal = make_cancel_journal(tmp_path)
    claim(journal)
    claim(journal, at=LATER)  # provably never sent: reclaim refreshes timestamps
    entry = journal.get_cancellation_entry("exec-1")
    assert entry.attempt_count == 1 and entry.updated_at_utc == LATER
    with pytest.raises(SubmissionJournalError, match="backwards"):
        claim(journal, at=NOW)
    with pytest.raises(SubmissionJournalConflictError, match="client_order_id"):
        claim(journal, at=LATER, client_order_id="client-other")


def test_cancel_broker_refused_terminalizes_and_reclaims_as_new_attempt(tmp_path):
    journal = make_cancel_journal(tmp_path)
    claim(journal)
    journal.mark_cancel_broker_call_started("exec-1", at_utc=LATER)
    journal.mark_cancel_broker_refused("exec-1", error="already filled", at_utc=LATER)
    entry = journal.get_cancellation_entry("exec-1")
    assert entry.outcome == CANCEL_OUTCOME_BROKER_REFUSED
    assert entry.broker_call_state == BROKER_CALL_RESPONDED
    assert entry.broker_response_state is None
    assert entry.last_error == "already filled"
    # terminal: excluded from the unresolved hand-off surface
    assert journal.list_unresolved_cancellations() == ()
    # a definitively refused cancel left nothing pending: NEW attempt
    claim(journal, at=LATER + timedelta(seconds=1))
    entry = journal.get_cancellation_entry("exec-1")
    assert entry.attempt_count == 2
    assert entry.broker_call_state == BROKER_CALL_NOT_STARTED
    assert entry.outcome == OUTCOME_OPEN and entry.last_error is None


def test_cancel_started_transition_is_one_shot(tmp_path):
    journal = make_cancel_journal(tmp_path)
    claim(journal)
    journal.mark_cancel_broker_call_started("exec-1", at_utc=LATER)
    with pytest.raises(SubmissionJournalError, match="requires one of"):
        journal.mark_cancel_broker_call_started("exec-1", at_utc=LATER)


def test_cancel_claim_refuses_foreign_client_order_id(tmp_path):
    journal = make_cancel_journal(tmp_path)
    claim(journal)
    with pytest.raises(SubmissionJournalConflictError, match="already has a journaled"):
        claim(journal, execution_request_id="exec-2", client_order_id="client-1")


def test_cancel_lifecycle_updates_require_an_existing_claim(tmp_path):
    journal = make_cancel_journal(tmp_path)
    with pytest.raises(SubmissionJournalError, match="record_cancellation_intent"):
        journal.mark_cancel_broker_call_started("exec-1", at_utc=NOW)


def test_cancel_claim_requires_the_db_file(tmp_path):
    journal = SubmissionJournal(tmp_path / "missing.sqlite3")
    with pytest.raises(SubmissionJournalError, match="initialize"):
        claim(journal)
    assert not (tmp_path / "missing.sqlite3").exists()


def test_cancellation_entry_invariants():
    def entry(**overrides):
        values = dict(
            execution_request_id="exec-1",
            client_order_id="client-1",
            attempt_count=1,
            cancellation_started_at_utc=NOW,
            broker_call_state=BROKER_CALL_NOT_STARTED,
            execution_db_save_state=DB_SAVE_NOT_ATTEMPTED,
            outcome=OUTCOME_OPEN,
            updated_at_utc=NOW,
        )
        values.update(overrides)
        return CancellationJournalEntry(**values)

    entry()  # the baseline claim is valid
    with pytest.raises(ValueError):
        entry(attempt_count=0)
    with pytest.raises(ValueError):
        entry(outcome=OUTCOME_SUPERSEDED)  # not a cancel outcome
    with pytest.raises(ValueError):
        entry(outcome=OUTCOME_COMPLETED)  # completed requires saved
    with pytest.raises(ValueError):
        entry(broker_call_state=BROKER_CALL_STARTED, outcome=OUTCOME_RECONCILIATION_REQUIRED)
    with pytest.raises(ValueError):
        # broker_refused must be responded + no snapshot + reason
        entry(outcome=CANCEL_OUTCOME_BROKER_REFUSED)
    with pytest.raises(ValueError):
        # a responded (non-refused) entry must carry the response snapshot
        entry(broker_call_state=BROKER_CALL_RESPONDED)
    with pytest.raises(ValueError):
        entry(broker_call_state=BROKER_CALL_UNKNOWN, outcome=OUTCOME_OPEN)


def _build_v2_journal(path):
    """A released-v2 journal DB with one completed submission row, written
    through the v2 schema directly."""
    from execution import submission_journal as sj

    connection = sqlite3.connect(path)
    try:
        sj._create_schema(connection, version=2)
        connection.execute(
            "INSERT INTO submission_journal VALUES ("
            "'exec-2', 'client-2', 'USD_JPY', 'buy', 'market', '1', NULL, "
            "'fake', 'fake', "
            "'2026-07-22T01:00:00+00:00', 'responded', 'filled', 'B-1', "
            "'saved', 'completed', NULL, '2026-07-22T01:00:01+00:00', NULL)"
        )
        connection.commit()
    finally:
        connection.close()


def test_v2_to_v3_migration_adds_the_cancellation_table_additively(tmp_path):
    path = tmp_path / "journal.sqlite3"
    _build_v2_journal(path)
    with sqlite3.connect(path) as connection:
        row_before = connection.execute("SELECT * FROM submission_journal").fetchone()
    journal = SubmissionJournal(path)
    journal.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        # existing submission rows untouched
        assert connection.execute("SELECT * FROM submission_journal").fetchone() == row_before
        # the cancellation table starts empty
        assert connection.execute("SELECT COUNT(*) FROM cancellation_journal").fetchone()[0] == 0
    journal.initialize()  # idempotent
    # and the cancel lifecycle works on the migrated DB
    claim(journal)
    assert journal.get_cancellation_entry("exec-1").outcome == OUTCOME_OPEN


def test_v2_to_v3_migration_rolls_back_completely_on_failure(tmp_path, monkeypatch):
    from execution import submission_journal as sj

    path = tmp_path / "journal.sqlite3"
    _build_v2_journal(path)
    with sqlite3.connect(path) as connection:
        before = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()

    real_verify = sj._verify_schema

    def fail_v3_verification(connection, db_path, *, version=sj.SCHEMA_VERSION):
        if version == sj.SCHEMA_VERSION:
            raise sj.SubmissionJournalSchemaError("injected post-migration failure")
        return real_verify(connection, db_path, version=version)

    monkeypatch.setattr(sj, "_verify_schema", fail_v3_verification)
    with pytest.raises(SubmissionJournalSchemaError, match="injected"):
        SubmissionJournal(path).initialize()
    monkeypatch.undo()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY name"
        ).fetchall() == before
    # and a clean retry succeeds
    SubmissionJournal(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
