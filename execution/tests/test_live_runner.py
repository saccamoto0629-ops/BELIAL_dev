"""Issue #131: the Live Execution runner core (Fake / Shadow only).

The runner adds a LIFECYCLE around the existing boundaries; these tests pin
that it adds nothing else. They cover, end to end through the production
services (FakeBroker + SQLite under ``tmp_path``, no external I/O):

- the fake path ExecutionRequest -> runner -> Safety Gate ->
  LiveOrderSubmissionService -> Journal / Order Store -> FakeBroker;
- the shadow path signal -> runner -> shadow claim -> Safety Gate ->
  intended order / shadow record, with ZERO broker involvement;
- idempotency: same intent twice in one cycle, twice across cycles, and
  across a "restart" (fresh objects on the same durable stores), including
  the crash windows (order DB save failure after a broker response, an
  unresolved journal entry surviving an order-DB loss);
- fail-closed behaviour: kill switch on, kill switch unreadable, store /
  journal / decision-store failures, gate rejections, invalid intents,
  unsupported modes and unexpected exceptions -- always with a broker
  submit count of zero;
- dry run: no broker call, no write, no file created, bytes unchanged;
- graceful shutdown: no new item after a stop request, and a deterministic
  resident loop (injected clock, injected sleeper, stop event).
"""

import sqlite3
import threading
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from broker.broker_models import AccountState, OrderState
from broker.fake_broker import FakeBroker, RejectOrder, StayPending, TimeoutOrder
from execution import execution_store, shadow_store
from execution.kill_switch_store import (
    PersistentKillSwitchStateProvider,
    SQLiteKillSwitchStore,
)
from execution.live_runner import (
    BrokerContact,
    ExecutionRunner,
    RunnerCycleReport,
    RunnerItemOutcome,
    RunnerItemStatus,
    RunnerLoopReport,
    RunnerModeError,
    RunnerStopReason,
    RUNNER_SUPPORTED_MODES,
    resolve_runner_mode,
)
from execution.runner_composition import (
    FakeSubmissionCycleExecutor,
    ShadowCycleExecutor,
    build_fake_execution_runner,
    build_shadow_execution_runner,
)
from execution.safety_gate import (
    AccountSafetySnapshot,
    ExecutionMode,
    SafetyRejectionCode,
    StaticSafetyState,
)
from execution.safety_store import SQLiteSafetyDecisionStore
from execution.shadow_config import ShadowExecutionConfig
from execution.shadow_providers import (
    PaperSignalDbSignalProvider,
    ShadowInputError,
    StaticAccountSnapshotProvider,
    StaticMarketSnapshotProvider,
)
from execution.submission_journal import OUTCOME_COMPLETED, SubmissionJournal

# Shared builders of the submission suite (tests run per-directory, so
# sibling helpers import flat -- the style test_cancel_submission.py uses).
from test_safety_gate import NOW, connection, make_broker, market, policy, request

ORDERS_DB = "orders.sqlite3"
DECISION_DB = "safety.sqlite3"
JOURNAL_DB = "journal.sqlite3"
SHADOW_DB = "shadow_results.sqlite3"
SIGNAL_DB = "paper_signals.sqlite3"


# --- fake-mode helpers --------------------------------------------------------


def db_paths(tmp_path) -> dict:
    return dict(
        order_db_path=tmp_path / ORDERS_DB,
        decision_db_path=tmp_path / DECISION_DB,
        journal_db_path=tmp_path / JOURNAL_DB,
    )


def market_provider(snapshot=None):
    return StaticMarketSnapshotProvider(
        snapshot=snapshot if snapshot is not None else market(), source="test-market-source"
    )


def build_fake(tmp_path, *, broker=None, state=None, selected_policy=None, **overrides):
    """Compose the fake runner through the production composition root."""
    broker = broker if broker is not None else make_broker()
    options = dict(
        connection=connection(broker),
        policy=selected_policy if selected_policy is not None else policy(),
        state_provider=state if state is not None else StaticSafetyState(),
        market_provider=market_provider(overrides.pop("market_snapshot", None)),
        clock=lambda: NOW,
        **db_paths(tmp_path),
    )
    options.update(overrides)
    return build_fake_execution_runner(**options), broker


def register(tmp_path, req=None, *, client_order_id="client-1"):
    """Registration is NOT the runner's job (a separate use case owns it);
    the tests seed the store through its public API."""
    return execution_store.create_execution_order(
        req if req is not None else request(),
        client_order_id=client_order_id,
        created_at_utc=NOW,
        db_path=tmp_path / ORDERS_DB,
    )


def register_many(tmp_path, count):
    for index in range(1, count + 1):
        register(
            tmp_path,
            request(execution_request_id=f"exec-{index}", signal_id=f"signal-{index}"),
            client_order_id=f"client-{index}",
        )


def stored(tmp_path, execution_request_id="exec-1"):
    return execution_store.get_execution_order(
        execution_request_id, db_path=tmp_path / ORDERS_DB
    )


def decisions(tmp_path, execution_request_id="exec-1"):
    return SQLiteSafetyDecisionStore(tmp_path / DECISION_DB).list_for_request(
        execution_request_id
    )


def journal_entry(tmp_path, execution_request_id="exec-1"):
    return SubmissionJournal(tmp_path / JOURNAL_DB).get_entry(execution_request_id)


def only(report: RunnerCycleReport) -> RunnerItemOutcome:
    assert report.processed == 1, report.outcomes
    return report.outcomes[0]


def codes(outcome: RunnerItemOutcome) -> set[str]:
    return set(outcome.rejection_codes)


# --- shadow-mode helpers ------------------------------------------------------


def seed_signal_db(tmp_path, *, signal_ids=("sig-1",), status="paper_only"):
    db = tmp_path / SIGNAL_DB
    connection_ = sqlite3.connect(db)
    connection_.execute(
        "CREATE TABLE paper_signals (signal_id TEXT PRIMARY KEY, created_at_utc TEXT NOT NULL, "
        "symbol TEXT NOT NULL, timeframe TEXT NOT NULL, side TEXT NOT NULL, "
        "strategy_profile TEXT NOT NULL, status TEXT NOT NULL)"
    )
    for index, signal_id in enumerate(signal_ids):
        created = (NOW - timedelta(minutes=index)).isoformat()
        connection_.execute(
            "INSERT INTO paper_signals VALUES (?, ?, 'USD_JPY', 'H1', 'long', "
            "'strategy-1', ?)",
            (signal_id, created, status),
        )
    connection_.commit()
    connection_.close()
    return db


def shadow_policy(**overrides):
    values = dict(
        allowed_connection_targets={ExecutionMode.SHADOW: frozenset({"shadow"})},
        # Stored Paper Signals carry no price levels, so the production
        # shadow request is a MARKET order without protective prices.
        require_stop_loss=False,
    )
    values.update(overrides)
    return policy(**values)


def shadow_config(*, kill_switch=False, selected_policy=None, quantity=Decimal("1")):
    return ShadowExecutionConfig(
        config_id="runner-shadow-config-1",
        source="test-config-source",
        policy=selected_policy if selected_policy is not None else shadow_policy(),
        order_quantities={"USD_JPY": quantity},
        kill_switch=kill_switch,
        connection_target="shadow",
    )


def account_provider():
    return StaticAccountSnapshotProvider(
        snapshot=AccountSafetySnapshot(
            positions=(),
            account=AccountState(
                account_id="shadow-account",
                currency="USD",
                balance=Decimal("100000"),
                equity=Decimal("100000"),
                margin_available=Decimal("100000"),
                retrieved_at_utc=NOW,
            ),
        ),
        source="test-account-source",
    )


def build_shadow(tmp_path, *, signal_ids=("sig-1",), config=None, seed=True, **overrides):
    if seed:
        seed_signal_db(tmp_path, signal_ids=signal_ids)
    provider = PaperSignalDbSignalProvider(tmp_path / SIGNAL_DB)
    options = dict(
        signal_provider=provider,
        signal_lister=provider,
        config=config if config is not None else shadow_config(),
        market_provider=market_provider(),
        account_provider=account_provider(),
        shadow_db_path=tmp_path / SHADOW_DB,
        decision_db_path=tmp_path / DECISION_DB,
        clock=lambda: NOW,
        owner_id="runner-owner-1",
    )
    options.update(overrides)
    return build_shadow_execution_runner(**options)


# --- runner-core stub ---------------------------------------------------------


class StubExecutor:
    """A minimal executor for the runner-core tests (no store, no service)."""

    def __init__(
        self,
        *,
        items=(),
        mode=ExecutionMode.FAKE,
        error_contact=BrokerContact.UNRESOLVED,
        select_error=None,
        behaviour=None,
    ):
        self._items = items
        self.execution_mode = mode
        self.error_broker_contact = error_contact
        self.source_id = "stub-source"
        self._select_error = select_error
        self._behaviour = behaviour or {}
        self.executed = []
        self.planned = []

    def select(self, *, limit):
        if self._select_error is not None:
            raise self._select_error
        return tuple(self._items)[:limit] if isinstance(self._items, (list, tuple)) else self._items

    def execute(self, item_id):
        self.executed.append(item_id)
        action = self._behaviour.get(item_id)
        if isinstance(action, Exception):
            raise action
        if action is not None:
            return action
        return RunnerItemOutcome(
            item_id=item_id,
            status=RunnerItemStatus.RECORDED,
            broker_contact=BrokerContact.NOT_SENT,
            detail="stub outcome",
        )

    def plan(self, item_id):
        self.planned.append(item_id)
        return RunnerItemOutcome(
            item_id=item_id,
            status=RunnerItemStatus.PLANNED,
            broker_contact=BrokerContact.NOT_SENT,
            detail="stub plan",
        )


def stub_runner(**kwargs):
    executor = kwargs.pop("executor", None) or StubExecutor(**kwargs.pop("stub", {}))
    options = dict(executor=executor, clock=lambda: NOW)
    options.update(kwargs)
    return ExecutionRunner(**options), executor


# --- mode dispatch (fail closed) ---------------------------------------------


def test_only_fake_and_shadow_are_runnable():
    assert RUNNER_SUPPORTED_MODES == {ExecutionMode.FAKE, ExecutionMode.SHADOW}
    assert resolve_runner_mode("fake") is ExecutionMode.FAKE
    assert resolve_runner_mode(ExecutionMode.SHADOW) is ExecutionMode.SHADOW


@pytest.mark.parametrize(
    "mode, expected",
    [
        ("demo", "NOT implemented"),
        ("live", "live activation lock"),
        ("dry-run", "runner FLAG"),
        ("paper", "unknown execution mode"),
        ("FAKE", "unknown execution mode"),
    ],
)
def test_unsupported_modes_are_refused_with_a_reason(mode, expected):
    with pytest.raises(RunnerModeError) as excinfo:
        resolve_runner_mode(mode)
    assert expected in str(excinfo.value)


@pytest.mark.parametrize("mode", [ExecutionMode.DEMO, ExecutionMode.LIVE, ExecutionMode.DRY_RUN])
def test_the_runner_refuses_an_executor_for_an_unsupported_mode(mode):
    with pytest.raises(RunnerModeError):
        stub_runner(stub={"mode": mode})


def test_the_runner_refuses_an_executor_without_a_typed_mode():
    with pytest.raises(RunnerModeError):
        stub_runner(stub={"mode": "fake"})


def test_the_fake_executor_refuses_a_non_fake_submission_boundary(tmp_path):
    """The mode gate is checked on the SUBMISSION BOUNDARY itself, so a
    runner can never be wired onto a demo / live connection."""

    class NotFake:
        execution_mode = ExecutionMode.LIVE

    with pytest.raises(ValueError):
        FakeSubmissionCycleExecutor(submission=NotFake(), market_provider=market_provider())


def test_the_fake_composition_root_refuses_a_non_fake_connection(tmp_path):
    class ClaimsShadow:
        execution_mode = ExecutionMode.SHADOW
        connection_target = "shadow"

    with pytest.raises(ValueError):
        build_fake_execution_runner(
            connection=ClaimsShadow(),
            policy=policy(),
            state_provider=StaticSafetyState(),
            market_provider=market_provider(),
            clock=lambda: NOW,
            **db_paths(tmp_path),
        )


# --- fake mode: end-to-end happy path ----------------------------------------


def test_fake_cycle_runs_the_full_production_path(tmp_path):
    register(tmp_path)
    runner, broker = build_fake(tmp_path)

    report = runner.run_cycle()

    outcome = only(report)
    assert outcome.status is RunnerItemStatus.SUBMITTED
    assert outcome.broker_contact is BrokerContact.SENT
    assert report.broker_sends == 1 and report.failed == 0 and report.ok
    assert report.execution_mode == "fake" and report.dry_run is False
    assert report.cycle_index == 1 and report.cycle_id.endswith("-cycle-1")
    # ... through the SINGLE sanctioned boundary: broker, order store,
    # journal and the safety decision all agree.
    assert [item.client_order_id for item in broker.submitted_requests] == ["client-1"]
    row = stored(tmp_path)
    assert (row["submission_phase"], row["order_state"]) == ("submitted", "filled")
    assert journal_entry(tmp_path).outcome == OUTCOME_COMPLETED
    assert [item.allowed for item in decisions(tmp_path)] == [True]


def test_a_cycle_processes_every_pending_intent_up_to_the_limit(tmp_path):
    register_many(tmp_path, 3)
    runner, broker = build_fake(tmp_path, max_items_per_cycle=2)

    first = runner.run_cycle()
    second = runner.run_cycle()

    assert [item.item_id for item in first.outcomes] == ["exec-1", "exec-2"]
    assert [item.item_id for item in second.outcomes] == ["exec-3"]
    assert len(broker.submitted_requests) == 3
    # The store IS the queue: a submitted intent leaves it.
    assert execution_store.list_pending_execution_requests(db_path=tmp_path / ORDERS_DB) == []
    assert runner.run_cycle().processed == 0


def test_a_gate_rejection_is_a_recorded_outcome_not_a_runner_failure(tmp_path):
    register(tmp_path, request(quantity=Decimal("11")))
    runner, broker = build_fake(tmp_path)

    report = runner.run_cycle()

    outcome = only(report)
    assert outcome.status is RunnerItemStatus.REJECTED
    assert outcome.broker_contact is BrokerContact.NOT_SENT
    assert SafetyRejectionCode.QUANTITY_LIMIT_EXCEEDED.value in codes(outcome)
    assert broker.submitted_requests == []
    assert report.ok and report.broker_sends == 0
    # The rejection was durably audited by the boundary, not by the runner.
    assert [item.allowed for item in decisions(tmp_path)] == [False]


def test_a_broker_rejection_is_recorded_and_the_intent_is_not_retried(tmp_path):
    register(tmp_path)
    broker = make_broker()
    broker.script_order("client-1", RejectOrder(reason="insufficient margin"))
    runner, _ = build_fake(tmp_path, broker=broker)

    first = only(runner.run_cycle())
    assert first.status is RunnerItemStatus.SUBMITTED  # the boundary sent it
    assert stored(tmp_path)["order_state"] == "rejected"
    # A rejected order is terminal: it leaves the pending queue and no later
    # cycle re-sends it (re-issuing is a separate, audited use case).
    assert runner.run_cycle().processed == 0
    assert len(broker.submitted_requests) == 1


# --- fake mode: idempotency / restart safety ---------------------------------


def test_the_same_intent_twice_in_one_selection_is_processed_once(tmp_path):
    register(tmp_path)
    runner, broker = build_fake(tmp_path)
    executor = runner._executor
    original = executor.select
    runner._executor.select = lambda *, limit: tuple(original(limit=limit)) * 2

    report = runner.run_cycle()

    assert [(item.item_id, item.status) for item in report.outcomes] == [
        ("exec-1", RunnerItemStatus.SUBMITTED),
        ("exec-1", RunnerItemStatus.SKIPPED),
    ]
    assert len(broker.submitted_requests) == 1


def test_a_second_cycle_never_re_sends_a_submitted_intent(tmp_path):
    """Even if the work source hands the same id back (it does not), the
    boundary refuses: the runner keeps no memory of its own."""
    register(tmp_path)
    runner, broker = build_fake(tmp_path)
    runner.run_cycle()
    runner._executor.select = lambda *, limit: ("exec-1",)

    report = runner.run_cycle()

    outcome = only(report)
    assert outcome.status is RunnerItemStatus.REJECTED
    assert SafetyRejectionCode.DUPLICATE_SUBMISSION.value in codes(outcome)
    assert len(broker.submitted_requests) == 1


def test_a_restart_on_the_same_stores_does_not_re_send(tmp_path):
    """Restart = brand-new runner, executor, service and broker objects on
    the SAME durable stores. Nothing in memory carries over, so only the
    stores can (and do) prevent the second send."""
    register(tmp_path)
    first_runner, first_broker = build_fake(tmp_path)
    first_runner.run_cycle()

    second_runner, second_broker = build_fake(tmp_path)
    second_runner._executor.select = lambda *, limit: ("exec-1",)
    report = second_runner.run_cycle()

    assert SafetyRejectionCode.DUPLICATE_SUBMISSION.value in codes(only(report))
    assert len(first_broker.submitted_requests) == 1
    assert second_broker.submitted_requests == []


def test_a_journal_entry_surviving_an_order_db_loss_blocks_the_resend(tmp_path):
    """Issue #93's crash window, driven by the runner: the order DB is lost
    and the intent re-registered (so it looks pending again), but the
    independent submission journal still holds the attempt."""
    register(tmp_path)
    runner, broker = build_fake(tmp_path)
    runner.run_cycle()
    assert len(broker.submitted_requests) == 1

    (tmp_path / ORDERS_DB).unlink()
    register(tmp_path)
    restarted, second_broker = build_fake(tmp_path)

    outcome = only(restarted.run_cycle())

    assert outcome.status is RunnerItemStatus.REJECTED
    assert SafetyRejectionCode.DUPLICATE_SUBMISSION.value in codes(outcome)
    assert second_broker.submitted_requests == []


def test_an_unresolved_unknown_outcome_is_never_re_sent(tmp_path):
    """A lost broker outcome (timeout) leaves UNKNOWN; the runner must not
    treat "uncertain" as "retryable"."""
    register(tmp_path)
    broker = make_broker()
    broker.script_order("client-1", TimeoutOrder())
    runner, _ = build_fake(tmp_path, broker=broker)

    report = runner.run_cycle()

    outcome = only(report)
    assert outcome.status is RunnerItemStatus.FAILED
    assert outcome.broker_contact is BrokerContact.UNRESOLVED
    assert report.unresolved == 1 and not report.ok
    assert stored(tmp_path)["order_state"] == "unknown"
    assert execution_store.list_unresolved_submissions(db_path=tmp_path / ORDERS_DB)
    # The intent left the pending queue, and a forced re-selection is refused.
    assert execution_store.list_pending_execution_requests(db_path=tmp_path / ORDERS_DB) == []
    runner._executor.select = lambda *, limit: ("exec-1",)
    assert SafetyRejectionCode.DUPLICATE_SUBMISSION.value in codes(only(runner.run_cycle()))
    assert len(broker.submitted_requests) == 1


def test_a_save_failure_after_the_broker_response_is_unresolved_not_retried(tmp_path, monkeypatch):
    """Partial failure: the broker answered but the outcome could not be
    persisted. The runner reports UNRESOLVED (never "not sent") and the
    intent is not re-sent afterwards."""
    register(tmp_path)
    runner, broker = build_fake(tmp_path)
    real_record = execution_store.record_order_result

    def broken(*args, **kwargs):
        raise execution_store.ExecutionStoreError("disk went away")

    monkeypatch.setattr(execution_store, "record_order_result", broken)
    report = runner.run_cycle()
    monkeypatch.setattr(execution_store, "record_order_result", real_record)

    outcome = only(report)
    assert outcome.status is RunnerItemStatus.FAILED
    assert outcome.broker_contact is BrokerContact.UNRESOLVED
    assert len(broker.submitted_requests) == 1
    assert stored(tmp_path)["order_state"] == "unknown"
    assert journal_entry(tmp_path).outcome == "reconciliation_required"

    runner._executor.select = lambda *, limit: ("exec-1",)
    assert SafetyRejectionCode.DUPLICATE_SUBMISSION.value in codes(only(runner.run_cycle()))
    assert len(broker.submitted_requests) == 1


def test_an_intent_that_vanished_between_selection_and_execution_is_skipped(tmp_path):
    register(tmp_path)
    runner, broker = build_fake(tmp_path)
    runner._executor.select = lambda *, limit: ("exec-missing",)

    outcome = only(runner.run_cycle())

    assert outcome.status is RunnerItemStatus.SKIPPED
    assert outcome.broker_contact is BrokerContact.NOT_SENT
    assert broker.submitted_requests == []


# --- fake mode: kill switch ---------------------------------------------------


def test_kill_switch_enabled_stops_every_new_submission(tmp_path):
    register_many(tmp_path, 2)
    runner, broker = build_fake(tmp_path, state=StaticSafetyState(kill_switch_enabled=True))

    report = runner.run_cycle()

    assert [item.status for item in report.outcomes] == [RunnerItemStatus.REJECTED] * 2
    for outcome in report.outcomes:
        assert SafetyRejectionCode.KILL_SWITCH_ENABLED.value in codes(outcome)
    assert broker.submitted_requests == []
    assert report.broker_sends == 0
    # Nothing moved out of the pending queue: the intents stay submittable
    # once the switch is released.
    assert len(execution_store.list_pending_execution_requests(db_path=tmp_path / ORDERS_DB)) == 2


def test_the_kill_switch_is_evaluated_for_every_item_not_once_per_process(tmp_path):
    """The gate re-reads the provider on EVERY evaluation, so flipping the
    switch between two items of the same cycle stops the second one."""
    register_many(tmp_path, 2)
    state = StaticSafetyState(kill_switch_enabled=False)

    class FlippingMarket:
        source_id = "flipping-market"

        def __init__(self):
            self.calls = 0

        def get_market_snapshot(self, symbol):
            self.calls += 1
            if self.calls > 1:
                state.kill_switch_enabled = True
            return market()

    runner, broker = build_fake(tmp_path, state=state, market_provider=FlippingMarket())

    report = runner.run_cycle()

    assert [item.status for item in report.outcomes] == [
        RunnerItemStatus.SUBMITTED,
        RunnerItemStatus.REJECTED,
    ]
    assert SafetyRejectionCode.KILL_SWITCH_ENABLED.value in codes(report.outcomes[1])
    assert len(broker.submitted_requests) == 1


def test_a_persisted_kill_switch_survives_a_runner_restart(tmp_path):
    register(tmp_path)
    kill_db = tmp_path / "kill_switch.sqlite3"
    SQLiteKillSwitchStore(kill_db).initialize_and_record(
        enabled=True, operator="operator-1", reason="drill", changed_at_utc=NOW
    )
    runner, broker = build_fake(
        tmp_path, state=PersistentKillSwitchStateProvider(kill_db)
    )

    outcome = only(runner.run_cycle())

    assert SafetyRejectionCode.KILL_SWITCH_ENABLED.value in codes(outcome)
    assert broker.submitted_requests == []


def test_an_unreadable_kill_switch_fails_closed(tmp_path):
    """"Cannot read the switch" is never "the switch is off"."""
    register(tmp_path)
    runner, broker = build_fake(
        tmp_path, state=PersistentKillSwitchStateProvider(tmp_path / "missing-kill.sqlite3")
    )

    outcome = only(runner.run_cycle())

    assert outcome.status is RunnerItemStatus.REJECTED
    assert SafetyRejectionCode.STATE_QUERY_FAILED.value in codes(outcome)
    assert broker.submitted_requests == []


def test_a_corrupt_kill_switch_db_fails_closed(tmp_path):
    register(tmp_path)
    kill_db = tmp_path / "kill_switch.sqlite3"
    kill_db.write_bytes(b"this is not a database")
    runner, broker = build_fake(
        tmp_path, state=PersistentKillSwitchStateProvider(kill_db)
    )

    outcome = only(runner.run_cycle())

    assert SafetyRejectionCode.STATE_QUERY_FAILED.value in codes(outcome)
    assert broker.submitted_requests == []


# --- fake mode: store / journal failures --------------------------------------


def test_an_unreadable_work_source_fails_the_cycle_closed(tmp_path):
    register(tmp_path)
    (tmp_path / ORDERS_DB).write_bytes(b"corrupted")
    runner, broker = build_fake(tmp_path)

    report = runner.run_cycle()

    assert report.processed == 0 and report.outcomes == ()
    assert report.selection_error is not None and not report.ok
    assert broker.submitted_requests == []


def test_a_lookup_failure_before_the_boundary_is_provably_not_sent(tmp_path, monkeypatch):
    register(tmp_path)
    runner, broker = build_fake(tmp_path)
    runner._executor.select = lambda *, limit: ("exec-1",)
    monkeypatch.setattr(
        execution_store,
        "get_execution_order",
        lambda *a, **k: (_ for _ in ()).throw(execution_store.ExecutionStoreError("boom")),
    )

    outcome = only(runner.run_cycle())

    assert outcome.status is RunnerItemStatus.FAILED
    assert outcome.broker_contact is BrokerContact.NOT_SENT
    assert broker.submitted_requests == []


def test_an_unavailable_market_snapshot_fails_closed(tmp_path):
    register(tmp_path)

    class BrokenMarket:
        source_id = "broken-market"

        def get_market_snapshot(self, symbol):
            raise ShadowInputError("no snapshot for today")

    runner, broker = build_fake(tmp_path, market_provider=BrokenMarket())

    outcome = only(runner.run_cycle())

    assert outcome.status is RunnerItemStatus.FAILED
    assert outcome.broker_contact is BrokerContact.NOT_SENT
    assert broker.submitted_requests == []
    assert stored(tmp_path)["submission_phase"] == "created"


def test_an_unwritable_decision_store_fails_closed(tmp_path):
    """The decision must be durably recorded and read back BEFORE a send;
    the runner cannot weaken that."""
    register(tmp_path)
    runner, broker = build_fake(tmp_path)
    (tmp_path / DECISION_DB).write_bytes(b"not a database")

    outcome = only(runner.run_cycle())

    assert outcome.status is RunnerItemStatus.REJECTED
    assert SafetyRejectionCode.DECISION_PERSIST_FAILED.value in codes(outcome)
    assert broker.submitted_requests == []


def test_an_unwritable_journal_fails_closed(tmp_path):
    register(tmp_path)
    runner, broker = build_fake(tmp_path)
    (tmp_path / JOURNAL_DB).write_bytes(b"not a database")

    outcome = only(runner.run_cycle())

    assert outcome.status is RunnerItemStatus.REJECTED
    assert SafetyRejectionCode.SUBMISSION_JOURNAL_FAILED.value in codes(outcome)
    assert broker.submitted_requests == []
    assert stored(tmp_path)["submission_phase"] == "created"


# --- fake mode: dry run -------------------------------------------------------


def snapshot_tree(directory: Path) -> dict:
    return {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def test_a_fake_dry_run_has_no_side_effects_at_all(tmp_path):
    register(tmp_path)
    before = snapshot_tree(tmp_path)
    assert set(before) == {ORDERS_DB}
    runner, broker = build_fake(tmp_path, dry_run=True)

    report = runner.run_cycle()

    outcome = only(report)
    assert outcome.status is RunnerItemStatus.PLANNED
    assert outcome.broker_contact is BrokerContact.NOT_SENT
    assert report.dry_run and report.broker_sends == 0
    # No broker call, no new DB (decision / journal), no byte changed.
    assert broker.submitted_requests == []
    assert snapshot_tree(tmp_path) == before
    assert not (tmp_path / DECISION_DB).exists()
    assert not (tmp_path / JOURNAL_DB).exists()
    assert stored(tmp_path)["submission_phase"] == "created"


def test_a_dry_run_never_calls_execute(tmp_path):
    runner, executor = stub_runner(stub={"items": ("a", "b")}, dry_run=True)

    report = runner.run_cycle()

    assert executor.executed == [] and executor.planned == ["a", "b"]
    assert {item.status for item in report.outcomes} == {RunnerItemStatus.PLANNED}


def test_a_dry_run_on_an_empty_store_creates_nothing(tmp_path):
    runner, broker = build_fake(tmp_path, dry_run=True)

    report = runner.run_cycle()

    assert report.processed == 0 and report.ok
    assert list(tmp_path.iterdir()) == []


# --- shadow mode --------------------------------------------------------------


def test_shadow_cycle_records_the_intended_order_without_any_broker(tmp_path):
    runner = build_shadow(tmp_path)

    report = runner.run_cycle()

    outcome = only(report)
    assert outcome.status is RunnerItemStatus.RECORDED
    assert outcome.broker_contact is BrokerContact.NOT_SENT
    assert report.execution_mode == "shadow" and report.broker_sends == 0 and report.ok
    result = shadow_store.get_shadow_result_by_signal_id(
        "sig-1", db_path=tmp_path / SHADOW_DB
    )
    assert result is not None and result.allowed
    assert result.intended_order.client_order_id == "sigord-sig-1"
    assert result.provenance.input_origin == "production_signal"
    # The shadow namespace stays separate: no execution order was created.
    assert not (tmp_path / ORDERS_DB).exists()
    assert [item.allowed for item in decisions(tmp_path, "sigreq-sig-1")] == [True]


def test_shadow_writes_no_broker_call_because_it_owns_no_broker(tmp_path):
    """The zero-broker-write property is STRUCTURAL: neither the shadow
    executor nor the runner core can reach a broker."""
    import inspect

    import execution.live_runner as live_runner

    signature = inspect.signature(ShadowCycleExecutor.__init__)
    assert not {"broker", "connection"} & set(signature.parameters)
    runner = build_shadow(tmp_path)
    executor = runner._executor
    assert not any(isinstance(value, FakeBroker) for value in vars(executor).values())
    source = Path(live_runner.__file__).read_text(encoding="utf-8")
    assert "broker.fake_broker" not in source and "broker.broker_interface" not in source
    runner.run_cycle()
    assert shadow_store.get_shadow_result_by_signal_id("sig-1", db_path=tmp_path / SHADOW_DB)


def test_a_shadow_gate_rejection_is_still_recorded(tmp_path):
    runner = build_shadow(
        tmp_path, config=shadow_config(selected_policy=shadow_policy(require_stop_loss=True))
    )

    outcome = only(runner.run_cycle())

    assert outcome.status is RunnerItemStatus.RECORDED
    assert SafetyRejectionCode.STOP_LOSS_REQUIRED.value in codes(outcome)
    result = shadow_store.get_shadow_result_by_signal_id("sig-1", db_path=tmp_path / SHADOW_DB)
    assert result is not None and not result.allowed


def test_the_shadow_kill_switch_is_evaluated_through_the_gate(tmp_path):
    runner = build_shadow(tmp_path, config=shadow_config(kill_switch=True))

    outcome = only(runner.run_cycle())

    assert outcome.status is RunnerItemStatus.RECORDED
    assert SafetyRejectionCode.KILL_SWITCH_ENABLED.value in codes(outcome)


def test_an_already_recorded_signal_is_an_explicit_duplicate(tmp_path):
    """Candidates are NOT filtered by "a record already exists" (PR #135
    Codex review P1): the claim classifies them, so a payload-equal re-run
    is an explicit duplicate that evaluates nothing and writes neither a
    second record nor a second decision."""
    runner = build_shadow(tmp_path, signal_ids=("sig-1", "sig-2"))

    first = runner.run_cycle()
    second = runner.run_cycle()

    assert {item.item_id for item in first.outcomes} == {"sig-1", "sig-2"}
    assert {item.status for item in first.outcomes} == {RunnerItemStatus.RECORDED}
    assert {item.status for item in second.outcomes} == {RunnerItemStatus.DUPLICATE}
    assert {item.broker_contact for item in second.outcomes} == {BrokerContact.NOT_SENT}
    assert second.ok
    assert len(shadow_store.list_shadow_results(db_path=tmp_path / SHADOW_DB)) == 2
    assert len(decisions(tmp_path, "sigreq-sig-1")) == 1


def test_a_changed_shadow_payload_is_a_conflict_not_a_silent_skip(tmp_path):
    """The documented fail-loud case (PR #135 Codex review P1): re-running a
    recorded signal under a CHANGED configuration must surface the identity
    conflict instead of reporting "nothing to do". A signal-id-only
    selection filter would have hidden exactly this."""
    build_shadow(tmp_path).run_cycle()
    reconfigured = build_shadow(
        tmp_path, seed=False, config=shadow_config(quantity=Decimal("2"))
    )

    report = reconfigured.run_cycle()

    outcome = only(report)
    assert outcome.status is RunnerItemStatus.FAILED
    assert outcome.broker_contact is BrokerContact.NOT_SENT
    assert "conflict" in outcome.detail.lower() and not report.ok
    # The recorded cycle is untouched: no second record, no second decision.
    results = shadow_store.list_shadow_results(db_path=tmp_path / SHADOW_DB)
    assert len(results) == 1 and results[0].request.quantity == Decimal("1")
    assert len(decisions(tmp_path, "sigreq-sig-1")) == 1


def test_a_shadow_restart_on_the_same_stores_records_once(tmp_path):
    build_shadow(tmp_path).run_cycle()
    restarted = build_shadow(tmp_path, seed=False)

    report = restarted.run_cycle()

    assert [item.status for item in report.outcomes] == [RunnerItemStatus.DUPLICATE]
    assert len(shadow_store.list_shadow_results(db_path=tmp_path / SHADOW_DB)) == 1


def test_a_non_executable_signal_is_never_selected(tmp_path):
    seed_signal_db(tmp_path, signal_ids=("sig-1",), status="archived")
    runner = build_shadow(tmp_path, seed=False)

    report = runner.run_cycle()

    assert report.processed == 0 and report.ok
    assert not (tmp_path / SHADOW_DB).exists()


def test_a_missing_signal_store_fails_the_shadow_cycle_closed(tmp_path):
    runner = build_shadow(tmp_path, seed=False)

    report = runner.run_cycle()

    assert report.selection_error is not None and report.processed == 0


def test_an_unconfigured_symbol_fails_the_item_closed(tmp_path):
    config = ShadowExecutionConfig(
        config_id="runner-shadow-config-2",
        source="test-config-source",
        policy=shadow_policy(),
        order_quantities={"EUR_USD": Decimal("1")},
        kill_switch=False,
        connection_target="shadow",
    )
    runner = build_shadow(tmp_path, config=config)

    outcome = only(runner.run_cycle())

    assert outcome.status is RunnerItemStatus.FAILED
    assert outcome.broker_contact is BrokerContact.NOT_SENT
    assert not (tmp_path / SHADOW_DB).exists()


def test_a_shadow_dry_run_has_no_side_effects_at_all(tmp_path):
    seed_signal_db(tmp_path)
    before = snapshot_tree(tmp_path)
    runner = build_shadow(tmp_path, seed=False, dry_run=True)

    report = runner.run_cycle()

    outcome = only(report)
    assert outcome.status is RunnerItemStatus.PLANNED
    assert snapshot_tree(tmp_path) == before
    assert not (tmp_path / SHADOW_DB).exists()
    assert not (tmp_path / DECISION_DB).exists()


# --- runner core: failure localization ---------------------------------------


def test_one_item_failure_does_not_stop_the_cycle(tmp_path):
    runner, executor = stub_runner(
        stub={"items": ("a", "b", "c"), "behaviour": {"b": RuntimeError("boom")}}
    )

    report = runner.run_cycle()

    assert [item.status for item in report.outcomes] == [
        RunnerItemStatus.RECORDED,
        RunnerItemStatus.FAILED,
        RunnerItemStatus.RECORDED,
    ]
    assert report.outcomes[1].broker_contact is BrokerContact.UNRESOLVED
    assert executor.executed == ["a", "b", "c"]
    assert not report.ok and report.failed == 1


def test_a_shadow_style_executor_failure_is_provably_not_sent():
    runner, _ = stub_runner(
        stub={
            "items": ("a",),
            "mode": ExecutionMode.SHADOW,
            "error_contact": BrokerContact.NOT_SENT,
            "behaviour": {"a": RuntimeError("boom")},
        }
    )

    outcome = only(runner.run_cycle())

    assert outcome.status is RunnerItemStatus.FAILED
    assert outcome.broker_contact is BrokerContact.NOT_SENT


def test_an_invalid_work_item_id_is_refused_without_processing():
    runner, executor = stub_runner(stub={"items": ("a", "", None)})

    report = runner.run_cycle()

    assert executor.executed == ["a"]
    assert [item.status for item in report.outcomes] == [
        RunnerItemStatus.RECORDED,
        RunnerItemStatus.FAILED,
        RunnerItemStatus.FAILED,
    ]


def test_a_work_source_exceeding_the_limit_is_bounded():
    runner, executor = stub_runner(stub={"items": ("a", "b", "c")}, max_items_per_cycle=2)
    runner._executor.select = lambda *, limit: ("a", "b", "c")

    report = runner.run_cycle()

    assert executor.executed == ["a", "b"]
    assert report.processed == 2


def test_an_executor_returning_a_foreign_outcome_fails_closed():
    foreign = RunnerItemOutcome(
        item_id="somebody-else",
        status=RunnerItemStatus.RECORDED,
        broker_contact=BrokerContact.NOT_SENT,
        detail="not mine",
    )
    runner, _ = stub_runner(stub={"items": ("a",), "behaviour": {"a": foreign}})

    outcome = only(runner.run_cycle())

    assert outcome.item_id == "a" and outcome.status is RunnerItemStatus.FAILED


def test_a_selection_failure_reports_zero_items():
    runner, _ = stub_runner(stub={"items": ("a",), "select_error": RuntimeError("store down")})

    report = runner.run_cycle()

    assert report.outcomes == () and "store down" in report.selection_error


# --- runner core: graceful shutdown ------------------------------------------


def test_no_item_is_started_after_a_stop_request():
    runner, executor = stub_runner(stub={"items": ("a", "b")})
    runner.request_stop()

    report = runner.run_cycle()

    assert executor.executed == [] and report.processed == 0
    assert report.stopped and runner.stop_requested


def test_a_stop_during_a_cycle_finishes_the_current_item_only(tmp_path):
    register_many(tmp_path, 3)
    stop_event = threading.Event()

    class StoppingMarket:
        source_id = "stopping-market"

        def get_market_snapshot(self, symbol):
            stop_event.set()
            return market()

    runner, broker = build_fake(
        tmp_path, market_provider=StoppingMarket(), stop_event=stop_event
    )

    report = runner.run_cycle()

    assert report.processed == 1 and report.stopped
    assert report.outcomes[0].status is RunnerItemStatus.SUBMITTED
    assert len(broker.submitted_requests) == 1
    # The untouched intents stay pending for a later runner.
    assert len(execution_store.list_pending_execution_requests(db_path=tmp_path / ORDERS_DB)) == 2


# --- runner core: the resident loop ------------------------------------------


class RecordingSleeper:
    """A deterministic sleeper: no wall clock is ever involved."""

    def __init__(self, *, stop_after=None, runner=None):
        self.calls = []
        self._stop_after = stop_after
        self._runner = runner

    def __call__(self, interval):
        self.calls.append(interval)
        if self._stop_after is not None and len(self.calls) >= self._stop_after:
            self._runner.request_stop()
            return True
        return False


def test_the_loop_runs_the_requested_number_of_cycles():
    runner, executor = stub_runner(stub={"items": ("a",)})
    sleeper = RecordingSleeper()

    report = runner.run_forever(interval_seconds=30, max_cycles=3, sleeper=sleeper)

    assert isinstance(report, RunnerLoopReport)
    assert len(report.cycles) == 3
    assert report.stop_reason is RunnerStopReason.MAX_CYCLES
    assert [cycle.cycle_index for cycle in report.cycles] == [1, 2, 3]
    assert executor.executed == ["a", "a", "a"]
    # The loop sleeps BETWEEN cycles only -- never after the last one.
    assert sleeper.calls == [30, 30]


def test_a_stop_during_the_sleep_ends_the_loop_immediately():
    runner, executor = stub_runner(stub={"items": ("a",)})
    sleeper = RecordingSleeper(stop_after=2, runner=runner)

    report = runner.run_forever(interval_seconds=5, sleeper=sleeper)

    assert report.stop_reason is RunnerStopReason.STOP_REQUESTED
    assert len(report.cycles) == 2 and executor.executed == ["a", "a"]


def test_the_default_sleeper_is_the_stop_event(tmp_path):
    """No wall-clock sleep exists in the runner: the default sleeper is the
    stop event's wait, so a stop interrupts it at once (a pre-set event
    makes this observable without waiting)."""
    stop_event = threading.Event()
    runner, executor = stub_runner(stub={"items": ("a",)}, stop_event=stop_event)
    stop_event.set()

    report = runner.run_forever(interval_seconds=3600)

    assert report.cycles == () and executor.executed == []
    assert report.stop_reason is RunnerStopReason.STOP_REQUESTED


def test_one_failing_cycle_does_not_end_the_loop():
    """A cycle that cannot even build its report (here: a clock that breaks
    for one call) is reported and the loop continues."""
    calls = {"count": 0}

    def flaky_clock():
        calls["count"] += 1
        # call 1 = cycle 1 start, call 2 = cycle 1 end, call 3 = cycle 2 start
        if calls["count"] == 3:
            raise RuntimeError("clock failure")
        return NOW

    executor = StubExecutor(items=("a",))
    runner = ExecutionRunner(executor=executor, clock=flaky_clock)

    report = runner.run_forever(
        interval_seconds=1, max_cycles=3, sleeper=RecordingSleeper()
    )

    assert len(report.cycles) == 3
    assert report.cycles[1].cycle_error is not None
    assert report.cycles[1].processed == 0
    assert [cycle.ok for cycle in report.cycles] == [True, False, True]


def test_the_loop_reports_every_cycle_to_the_callback():
    seen = []
    runner, _ = stub_runner(stub={"items": ("a",)})

    runner.run_forever(
        interval_seconds=1,
        max_cycles=2,
        sleeper=RecordingSleeper(),
        on_cycle=seen.append,
    )

    assert [cycle.cycle_index for cycle in seen] == [1, 2]


def test_a_failing_callback_never_breaks_the_loop():
    runner, executor = stub_runner(stub={"items": ("a",)})

    def boom(_report):
        raise RuntimeError("callback exploded")

    report = runner.run_forever(
        interval_seconds=1, max_cycles=2, sleeper=RecordingSleeper(), on_cycle=boom
    )

    assert len(report.cycles) == 2 and executor.executed == ["a", "a"]


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(interval_seconds=0),
        dict(interval_seconds=-1),
        dict(interval_seconds="30"),
        dict(interval_seconds=1, max_cycles=0),
        dict(interval_seconds=1, sleeper="not callable"),
        dict(interval_seconds=float("inf")),
        dict(interval_seconds=threading.TIMEOUT_MAX + 1000),
    ],
)
def test_the_loop_refuses_invalid_pacing_arguments(kwargs):
    runner, _ = stub_runner(stub={"items": ()})
    with pytest.raises(ValueError):
        runner.run_forever(**kwargs)


# --- models -------------------------------------------------------------------


def test_a_submitted_outcome_must_admit_the_broker_call():
    with pytest.raises(ValueError):
        RunnerItemOutcome(
            item_id="a",
            status=RunnerItemStatus.SUBMITTED,
            broker_contact=BrokerContact.NOT_SENT,
            detail="lying about the send",
        )


@pytest.mark.parametrize(
    "status",
    [
        RunnerItemStatus.RECORDED,
        RunnerItemStatus.REJECTED,
        RunnerItemStatus.DUPLICATE,
        RunnerItemStatus.SKIPPED,
        RunnerItemStatus.PLANNED,
    ],
)
def test_pre_broker_statuses_must_report_not_sent(status):
    with pytest.raises(ValueError):
        RunnerItemOutcome(
            item_id="a",
            status=status,
            broker_contact=BrokerContact.SENT,
            detail="cannot have been sent",
        )


def test_a_failed_outcome_may_never_claim_a_completed_send():
    with pytest.raises(ValueError):
        RunnerItemOutcome(
            item_id="a",
            status=RunnerItemStatus.FAILED,
            broker_contact=BrokerContact.SENT,
            detail="not a valid claim",
        )
    assert RunnerItemOutcome(
        item_id="a",
        status=RunnerItemStatus.FAILED,
        broker_contact=BrokerContact.UNRESOLVED,
        detail="honest",
    ).broker_contact is BrokerContact.UNRESOLVED


def test_a_failed_selection_may_not_carry_outcomes():
    with pytest.raises(ValueError):
        RunnerCycleReport(
            cycle_id="c-1",
            cycle_index=1,
            execution_mode="fake",
            dry_run=False,
            started_at_utc=NOW,
            finished_at_utc=NOW,
            outcomes=(
                RunnerItemOutcome(
                    item_id="a",
                    status=RunnerItemStatus.SKIPPED,
                    broker_contact=BrokerContact.NOT_SENT,
                    detail="x",
                ),
            ),
            selection_error="store down",
        )


def test_the_runner_refuses_a_broken_clock():
    executor = StubExecutor(items=("a",))
    runner = ExecutionRunner(executor=executor, clock=lambda: datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        runner.run_cycle()
    assert executor.executed == []


@pytest.mark.parametrize("limit", [0, -1, 1001, True, "10"])
def test_the_runner_refuses_an_invalid_cycle_limit(limit):
    with pytest.raises(ValueError):
        stub_runner(max_items_per_cycle=limit)


def test_pending_selection_is_ordered_and_bounded(tmp_path):
    register_many(tmp_path, 3)
    rows = execution_store.list_pending_execution_requests(
        limit=2, db_path=tmp_path / ORDERS_DB
    )
    assert [row["execution_request_id"] for row in rows] == ["exec-1", "exec-2"]
    with pytest.raises(execution_store.ExecutionStoreError):
        execution_store.list_pending_execution_requests(limit=0, db_path=tmp_path / ORDERS_DB)
    # A missing DB is empty work, never a created file (dry-run safe).
    missing = tmp_path / "no-such.sqlite3"
    assert execution_store.list_pending_execution_requests(db_path=missing) == []
    assert not missing.exists()


def test_a_submitted_intent_leaves_the_pending_queue(tmp_path):
    register(tmp_path)
    runner, _ = build_fake(tmp_path)
    runner.run_cycle()
    assert execution_store.list_pending_execution_requests(db_path=tmp_path / ORDERS_DB) == []
    row = stored(tmp_path)
    assert row["submission_phase"] == "submitted"
    assert OrderState(row["order_state"]) is OrderState.FILLED


def test_an_accepted_order_stays_out_of_the_queue(tmp_path):
    register(tmp_path)
    broker = make_broker()
    broker.script_order("client-1", StayPending(broker_order_id="BRK-1"))
    runner, _ = build_fake(tmp_path, broker=broker)

    runner.run_cycle()

    assert stored(tmp_path)["order_state"] == "accepted"
    assert execution_store.list_pending_execution_requests(db_path=tmp_path / ORDERS_DB) == []
    assert runner.run_cycle().processed == 0


def test_a_non_iterable_selection_fails_the_cycle_instead_of_raising():
    """A work source that cannot even produce a list must fail the cycle
    closed -- run_cycle() itself never raises for a bad work source."""
    runner, executor = stub_runner(stub={"items": ("a",)})
    runner._executor.select = lambda *, limit: 42

    report = runner.run_cycle()

    assert report.selection_error is not None and report.outcomes == ()
    assert executor.executed == []


def test_an_interval_beyond_the_platform_limit_is_refused_before_any_cycle():
    """PR #135 Codex review P2: the default sleeper is
    ``threading.Event.wait``, which raises OverflowError above the platform
    timeout limit. Such an interval must be refused at the boundary -- not
    kill a resident runner after its first cycle."""
    runner, executor = stub_runner(stub={"items": ("a",)})

    with pytest.raises(ValueError) as excinfo:
        runner.run_forever(interval_seconds=float("inf"))

    assert "platform timeout limit" in str(excinfo.value)
    assert executor.executed == [] and runner.cycles_run == 0
    # The accepted boundary value still runs (and the default sleeper is
    # what a stop event interrupts).
    runner.request_stop()
    assert runner.run_forever(interval_seconds=threading.TIMEOUT_MAX).cycles == ()
