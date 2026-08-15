"""Issue #132: OANDA Demo mode integrated into the Live Execution runner.

NOTHING HERE OPENS A SOCKET. The demo adapter is composed through the demo
activation lock's private seam with a scripted in-process transport, so the
full production chain

    runner
      -> Safety Gate (incl. the DURABLE kill switch, EVERY submission)
      -> LiveOrderSubmissionService (the ONE boundary)
      -> Submission Journal + Execution Order Store
      -> Broker boundary
      -> OANDA Demo adapter (real v20 translation, fake wire)

runs end to end against SQLite under ``tmp_path`` and a fake HTTP layer.

What is pinned:

- the demo runner E2E: an intent becomes a broker-confirmed, journaled,
  state-machine-recorded order;
- every non-demo endpoint is refused before a runner can exist;
- credential / account / mode identity mismatches are refused BEFORE any
  broker call (the broker records zero requests);
- the Kill Switch stops demo submissions -- at composition AND mid-run;
- duplicate execution, restart and partial failure never send twice;
- Journal / Order Store agree with the order state machine, and a demo
  broker response is what drives it;
- Fake / Shadow / Demo stay separate in responsibility and persistence;
- the LIVE endpoint is unreachable from every path used here.
"""

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from broker.broker_models import OrderState, safe_account_reference
from broker.oanda_demo_adapter import (
    ENV_ACCOUNT_ID,
    ENV_API_TOKEN,
    ENV_API_URL,
    OANDA_DEMO_API_URL,
    OANDA_DEMO_CONNECTION_TARGET,
    OandaDemoAdapter,
)
from broker.oanda_live_adapter import (
    ENABLE_LIVE_TRADING_TRUE,
    ENV_ENABLE_LIVE_TRADING,
    ENV_LIVE_ACCOUNT_ID,
    ENV_LIVE_API_TOKEN,
    ENV_LIVE_API_URL,
    OANDA_LIVE_API_URL,
)
from execution import execution_store
from execution.demo_lock import DemoLockCode, DemoRuntimeGrant, _compose_demo_connection
from execution.kill_switch_store import SQLiteKillSwitchStore
from execution.live_runner import BrokerContact, RunnerItemStatus, RunnerModeError
from execution.runner_composition import (
    DemoSubmissionCycleExecutor,
    FakeSubmissionCycleExecutor,
    build_demo_execution_runner,
    build_fake_execution_runner,
)
from execution.safety_gate import (
    ExecutionMode,
    REAL_BROKER_MODES,
    SafetyRejectionCode,
    StaticSafetyState,
)
from execution.safety_store import SQLiteSafetyDecisionStore
from execution.shadow_providers import StaticMarketSnapshotProvider
from execution.submission_journal import (
    BROKER_CALL_NOT_STARTED,
    OUTCOME_COMPLETED,
    OUTCOME_OPEN,
    OUTCOME_RECONCILIATION_REQUIRED,
    SubmissionJournal,
)

# Shared builders of the submission suite (tests run per-directory, so
# sibling helpers import flat -- the style test_live_runner.py uses).
from test_safety_gate import NOW, market, request

ORDERS_DB = "orders.sqlite3"
DECISION_DB = "safety.sqlite3"
JOURNAL_DB = "journal.sqlite3"
KILL_SWITCH_DB = "kill_switch.sqlite3"

TOKEN = "demo-token-not-a-real-secret"
ACCOUNT = "101-000-DEMOACCOUNT-001"
ACCOUNT_REFERENCE = safe_account_reference(ACCOUNT)


# --- the fake OANDA demo wire ---------------------------------------------------


class DemoTransport:
    """A routing in-process stand-in for the OANDA practice endpoint.

    It answers the three requests one gated submission makes (open
    positions, account summary, POST order) and RECORDS every call, so a
    test can assert "the broker was never contacted" as a fact rather than
    an inference. Every URL it receives is asserted to start with the
    practice endpoint: nothing on this path can address the live host.
    """

    def __init__(self, *, order_responses=None):
        self.calls = []
        self.order_responses = list(order_responses or [])
        self.submitted_bodies = []

    @property
    def submit_calls(self) -> list:
        return [call for call in self.calls if call["method"] == "POST"]

    def __call__(self, method, url, headers, body, timeout):
        assert url.startswith(OANDA_DEMO_API_URL), url
        assert OANDA_LIVE_API_URL not in url
        self.calls.append({"method": method, "url": url, "body": body})
        if method == "GET" and url.endswith("/openPositions"):
            return 200, json.dumps({"positions": []})
        if method == "GET" and url.endswith("/summary"):
            return 200, json.dumps(
                {
                    "account": {
                        "id": ACCOUNT,
                        "currency": "JPY",
                        "balance": "3000000.0000",
                        "NAV": "3000000.0000",
                        "marginAvailable": "2999000.0000",
                    }
                }
            )
        if method == "POST" and url.endswith("/orders"):
            self.submitted_bodies.append(json.loads(body))
            if not self.order_responses:
                raise AssertionError("unexpected extra order submission")
            response = self.order_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            status, payload = response
            return status, json.dumps(payload)
        raise AssertionError(f"unexpected demo request {method} {url}")


def fill_response(client_order_id="client-1", units="1", price="155.123"):
    """A 201 create+fill response with the correlation fields OANDA echoes."""
    return (
        201,
        {
            "orderCreateTransaction": {
                "id": "1001",
                "time": "2026-07-22T01:00:00.000000000Z",
                "clientExtensions": {"id": client_order_id},
            },
            "orderFillTransaction": {
                "id": "1002",
                "type": "ORDER_FILL",
                "orderID": "1001",
                "clientOrderID": client_order_id,
                "units": units,
                "price": price,
                "time": "2026-07-22T01:00:00.000000000Z",
            },
        },
    )


def reject_response(client_order_id="client-1"):
    """A definitive broker rejection (no order exists afterwards)."""
    return (
        400,
        {
            "orderRejectTransaction": {
                "id": "2001",
                "rejectReason": "INSUFFICIENT_MARGIN",
                "clientExtensions": {"id": client_order_id},
            }
        },
    )


# --- environment / configuration ------------------------------------------------


def demo_env(**overrides) -> dict:
    env = {ENV_API_TOKEN: TOKEN, ENV_ACCOUNT_ID: ACCOUNT}
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def write_safety_config(tmp_path, **overrides):
    document = {
        "policy_id": "demo-runner-v1",
        "allowed_symbols": ["USD_JPY"],
        "allowed_sides": ["buy", "sell"],
        "max_order_quantity": "10",
        "max_positions_per_symbol": 2,
        "max_positions_account": 5,
        "max_reference_age_seconds": 30,
        "max_spread_by_symbol": {"USD_JPY": "0.05"},
        "allowed_demo_targets": [OANDA_DEMO_CONNECTION_TARGET],
        "expected_account_reference": ACCOUNT_REFERENCE,
        "require_stop_loss": True,
    }
    document.update(overrides)
    path = tmp_path / "demo_safety.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def kill_switch_db(tmp_path, *, enabled=False) -> Path:
    path = tmp_path / KILL_SWITCH_DB
    store = SQLiteKillSwitchStore(path)
    if not path.exists():
        store.initialize_and_record(
            enabled=True, operator="ops", reason="initial halt", changed_at_utc=NOW
        )
        store.record_transition(
            enabled=False,
            operator="ops",
            reason="ready for demo",
            changed_at_utc=NOW + timedelta(minutes=1),
        )
    if enabled:
        store.record_transition(
            enabled=True,
            operator="ops",
            reason="halt",
            changed_at_utc=NOW + timedelta(minutes=2),
        )
    return path


def grant_for(tmp_path, monkeypatch, *, transport, env=None, config_overrides=None,
              kill_switch_enabled=False, execution_mode="demo"):
    """Compose a demo grant through the lock's private (offline) seam."""
    monkeypatch.setattr("os.environ", demo_env() if env is None else env)
    return _compose_demo_connection(
        execution_mode=execution_mode,
        safety_config_path=write_safety_config(tmp_path, **(config_overrides or {})),
        kill_switch_db_path=kill_switch_db(tmp_path, enabled=kill_switch_enabled),
        transport=transport,
        clock=lambda: NOW,
    )


def build_demo(tmp_path, monkeypatch, *, transport=None, order_responses=None, **overrides):
    """Compose the production demo runner (lock -> grant -> runner)."""
    transport = (
        transport
        if transport is not None
        else DemoTransport(order_responses=order_responses or [fill_response()])
    )
    decision, grant = grant_for(tmp_path, monkeypatch, transport=transport)
    assert decision.allowed is True, decision.rejection_codes
    options = dict(
        grant=grant,
        market_provider=StaticMarketSnapshotProvider(snapshot=market(), source="test-market"),
        order_db_path=tmp_path / ORDERS_DB,
        decision_db_path=tmp_path / DECISION_DB,
        journal_db_path=tmp_path / JOURNAL_DB,
        clock=lambda: NOW,
    )
    options.update(overrides)
    return build_demo_execution_runner(**options), transport, grant


def register(tmp_path, req=None, *, client_order_id="client-1"):
    return execution_store.create_execution_order(
        req if req is not None else request(),
        client_order_id=client_order_id,
        created_at_utc=NOW,
        db_path=tmp_path / ORDERS_DB,
    )


def stored(tmp_path, execution_request_id="exec-1"):
    return execution_store.get_execution_order(
        execution_request_id, db_path=tmp_path / ORDERS_DB
    )


def journal_entry(tmp_path, execution_request_id="exec-1"):
    return SubmissionJournal(tmp_path / JOURNAL_DB).get_entry(execution_request_id)


def only(report):
    assert report.processed == 1, report.outcomes
    return report.outcomes[0]


# --- the demo runner, end to end ------------------------------------------------


def test_a_demo_intent_travels_the_whole_chain(tmp_path, monkeypatch):
    runner, transport, grant = build_demo(tmp_path, monkeypatch)
    register(tmp_path)

    report = runner.run_cycle()
    outcome = only(report)

    # runner
    assert runner.execution_mode is ExecutionMode.DEMO
    assert outcome.status is RunnerItemStatus.SUBMITTED
    assert outcome.broker_contact is BrokerContact.SENT
    assert report.broker_sends == 1 and report.ok

    # broker boundary -> OANDA demo (the practice endpoint, exactly once)
    assert len(transport.submit_calls) == 1
    assert transport.submit_calls[0]["url"] == (
        f"{OANDA_DEMO_API_URL}/v3/accounts/{ACCOUNT}/orders"
    )
    body = transport.submitted_bodies[0]["order"]
    assert body["instrument"] == "USD_JPY"
    assert body["clientExtensions"]["id"] == "client-1"

    # execution order store: the state machine took the broker's answer
    row = stored(tmp_path)
    assert row["submission_phase"] == execution_store.PHASE_SUBMITTED
    assert row["order_state"] == OrderState.FILLED.value
    assert row["broker_order_id"] == "1001"

    # submission journal: one completed attempt
    entry = journal_entry(tmp_path)
    assert entry.outcome == OUTCOME_COMPLETED
    assert SubmissionJournal(tmp_path / JOURNAL_DB).list_unresolved() == ()

    # safety decision: durably recorded under the DEMO identity
    decisions = SQLiteSafetyDecisionStore(tmp_path / DECISION_DB).list_for_request("exec-1")
    assert len(decisions) == 1
    assert decisions[0].execution_mode == ExecutionMode.DEMO.value
    assert decisions[0].connection_target == OANDA_DEMO_CONNECTION_TARGET
    assert grant.connection.connection_target == OANDA_DEMO_CONNECTION_TARGET


def test_a_demo_broker_rejection_reaches_the_state_machine(tmp_path, monkeypatch):
    """The DEMO broker's answer -- not a runner-side guess -- drives the
    stored state."""
    runner, transport, _grant = build_demo(
        tmp_path, monkeypatch, order_responses=[reject_response()]
    )
    register(tmp_path)

    outcome = only(runner.run_cycle())
    assert outcome.status is RunnerItemStatus.SUBMITTED
    assert outcome.broker_contact is BrokerContact.SENT
    row = stored(tmp_path)
    assert row["order_state"] == OrderState.REJECTED.value
    assert row["submission_phase"] == execution_store.PHASE_SUBMITTED
    assert journal_entry(tmp_path).outcome == OUTCOME_COMPLETED
    assert len(transport.submit_calls) == 1


def test_a_gate_rejection_never_reaches_the_demo_broker(tmp_path, monkeypatch):
    runner, transport, _grant = build_demo(tmp_path, monkeypatch)
    # A quantity above the demo safety configuration's ceiling.
    register(tmp_path, request(quantity=Decimal("50")))

    outcome = only(runner.run_cycle())
    assert outcome.status is RunnerItemStatus.REJECTED
    assert outcome.broker_contact is BrokerContact.NOT_SENT
    assert SafetyRejectionCode.QUANTITY_LIMIT_EXCEEDED.value in outcome.rejection_codes
    assert transport.submit_calls == []
    assert stored(tmp_path)["submission_phase"] == execution_store.PHASE_CREATED


def test_a_dry_run_reaches_neither_the_broker_nor_a_store(tmp_path, monkeypatch):
    transport = DemoTransport(order_responses=[])
    decision, grant = grant_for(tmp_path, monkeypatch, transport=transport)
    assert decision.allowed
    # Seed the order store BEFORE the dry run so there is work to plan; the
    # audit / journal DBs must stay absent.
    register(tmp_path)
    runner = build_demo_execution_runner(
        grant=grant,
        market_provider=StaticMarketSnapshotProvider(snapshot=market(), source="test-market"),
        order_db_path=tmp_path / ORDERS_DB,
        decision_db_path=tmp_path / DECISION_DB,
        journal_db_path=tmp_path / JOURNAL_DB,
        clock=lambda: NOW,
        dry_run=True,
    )
    before = (tmp_path / ORDERS_DB).read_bytes()

    outcome = only(runner.run_cycle())
    assert outcome.status is RunnerItemStatus.PLANNED
    assert outcome.broker_contact is BrokerContact.NOT_SENT
    assert transport.calls == []
    assert not (tmp_path / DECISION_DB).exists()
    assert not (tmp_path / JOURNAL_DB).exists()
    assert (tmp_path / ORDERS_DB).read_bytes() == before


# --- the Kill Switch ------------------------------------------------------------


def test_the_kill_switch_prevents_a_demo_runner_from_being_composed(tmp_path, monkeypatch):
    transport = DemoTransport()
    decision, grant = grant_for(
        tmp_path, monkeypatch, transport=transport, kill_switch_enabled=True
    )
    assert decision.allowed is False
    assert grant is None
    assert DemoLockCode.KILL_SWITCH_ENABLED.value in decision.rejection_codes
    assert transport.calls == []


def test_a_kill_switch_flipped_mid_run_stops_the_next_demo_submission(tmp_path, monkeypatch):
    """The composition-time check is not the only one: the gate re-reads the
    DURABLE store on every submission, so a halt between two cycles stops the
    second one."""
    runner, transport, _grant = build_demo(tmp_path, monkeypatch)
    register(tmp_path)

    first = runner.run_cycle()
    assert first.broker_sends == 1

    # A second intent arrives, and the operator halts trading in between.
    register(
        tmp_path,
        request(execution_request_id="exec-2", signal_id="signal-2"),
        client_order_id="client-2",
    )
    SQLiteKillSwitchStore(tmp_path / KILL_SWITCH_DB).record_transition(
        enabled=True, operator="ops", reason="halt", changed_at_utc=NOW + timedelta(minutes=5)
    )

    second = runner.run_cycle()
    outcome = only(second)
    assert outcome.status is RunnerItemStatus.REJECTED
    assert outcome.broker_contact is BrokerContact.NOT_SENT
    assert SafetyRejectionCode.KILL_SWITCH_ENABLED.value in outcome.rejection_codes
    assert len(transport.submit_calls) == 1
    assert stored(tmp_path, "exec-2")["submission_phase"] == execution_store.PHASE_CREATED


class HaltingDemoTransport(DemoTransport):
    """A demo transport that runs a callback during the FIRST broker call of
    a submission (PR #136 review P1).

    ``/openPositions`` is the gate's first network round trip, so a callback
    fired there lands exactly in the window between the gate reading the kill
    switch and the order POST -- the window the review reproduced.
    """

    def __init__(self, during_open_positions, **kwargs):
        super().__init__(**kwargs)
        self._during_open_positions = during_open_positions
        self.fired = []

    def __call__(self, method, url, headers, body, timeout):
        if url.endswith("/openPositions") and not self.fired:
            self.fired.append(url)
            self._during_open_positions()
        return super().__call__(method, url, headers, body, timeout)


def test_a_kill_switch_flipped_during_the_safety_gets_stops_the_order_post(
    tmp_path, monkeypatch
):
    """PR #136 review P1, reproduced: the operator presses the switch WHILE
    this submission is being evaluated.

    The gate reads the switch before its broker safety GETs, so between that
    read and the POST there is a window of real network round trips. The
    switch is enabled from inside the ``/openPositions`` callback here, and
    the POST must not happen.
    """

    def halt():
        SQLiteKillSwitchStore(tmp_path / KILL_SWITCH_DB).record_transition(
            enabled=True,
            operator="ops",
            reason="halt mid-submission",
            changed_at_utc=NOW + timedelta(seconds=1),
        )

    transport = HaltingDemoTransport(halt, order_responses=[fill_response()])
    runner, _transport, _grant = build_demo(tmp_path, monkeypatch, transport=transport)
    register(tmp_path)

    outcome = only(runner.run_cycle())

    assert transport.fired, "the window was never entered; the reproduction is not valid"
    assert transport.submit_calls == [], "an order POST was issued after the kill switch went ON"
    assert outcome.status is RunnerItemStatus.REJECTED
    assert outcome.broker_contact is BrokerContact.NOT_SENT
    assert SafetyRejectionCode.KILL_SWITCH_ENABLED.value in outcome.rejection_codes
    # the journal must still prove the broker was never called
    entries = SubmissionJournal(tmp_path / JOURNAL_DB).list_unresolved()
    assert [entry.outcome for entry in entries] == [OUTCOME_OPEN]
    assert entries[0].broker_call_state == BROKER_CALL_NOT_STARTED


def test_a_kill_switch_that_becomes_unreadable_mid_submission_stops_the_post(
    tmp_path, monkeypatch
):
    """The same window, but the switch becomes UNREADABLE rather than ON. An
    unreadable switch may never read as OFF, so this refuses too."""
    transport = HaltingDemoTransport(
        lambda: (tmp_path / KILL_SWITCH_DB).write_bytes(b"not a database"),
        order_responses=[fill_response()],
    )
    runner, _transport, _grant = build_demo(tmp_path, monkeypatch, transport=transport)
    register(tmp_path)

    outcome = only(runner.run_cycle())

    assert transport.fired
    assert transport.submit_calls == []
    assert outcome.status is RunnerItemStatus.REJECTED
    assert outcome.broker_contact is BrokerContact.NOT_SENT
    assert SafetyRejectionCode.STATE_QUERY_FAILED.value in outcome.rejection_codes


def test_an_unreadable_kill_switch_stops_demo_submission(tmp_path, monkeypatch):
    runner, transport, _grant = build_demo(tmp_path, monkeypatch)
    register(tmp_path)
    (tmp_path / KILL_SWITCH_DB).write_bytes(b"not a database")

    outcome = only(runner.run_cycle())
    assert outcome.status is RunnerItemStatus.REJECTED
    assert SafetyRejectionCode.STATE_QUERY_FAILED.value in outcome.rejection_codes
    assert transport.submit_calls == []


def test_the_durable_kill_switch_requirement_covers_both_real_broker_modes():
    """Issue #132 widened the boundary's durable-reader requirement from
    ``live`` to every mode that reaches a real broker. Pinning the set keeps
    a later edit from quietly narrowing it back to live-only, which would
    let a demo runner keep submitting through an in-memory switch."""
    assert REAL_BROKER_MODES == {ExecutionMode.DEMO, ExecutionMode.LIVE}


def test_a_demo_boundary_refuses_an_in_memory_kill_switch(tmp_path, monkeypatch):
    """The submission boundary itself demands the durable reader for every
    real-broker mode, so a demo runner cannot be wired to a static 'off'."""
    from execution.reissue_composition import build_reissue_boundary
    from execution.reissue_service import ReissuePolicy
    from execution.safety_gate import SafetyGate

    _decision, grant = grant_for(tmp_path, monkeypatch, transport=DemoTransport())
    gate = SafetyGate(
        policy=grant.policy, state_provider=StaticSafetyState(), clock=lambda: NOW
    )
    with pytest.raises(ValueError, match="persistent kill switch"):
        build_reissue_boundary(
            connection=grant.connection,
            gate=gate,
            current_policy=grant.policy,
            reissue_policy=ReissuePolicy(),
            decision_db_path=tmp_path / DECISION_DB,
            order_db_path=tmp_path / ORDERS_DB,
            journal_db_path=tmp_path / JOURNAL_DB,
            clock=lambda: NOW,
        )


# --- identity: endpoint / credential / account / mode ---------------------------


@pytest.mark.parametrize(
    "endpoint",
    [OANDA_LIVE_API_URL, "https://stream-fxtrade.oanda.com", "https://evil.example"],
)
def test_no_runner_exists_for_a_non_demo_endpoint(tmp_path, monkeypatch, endpoint):
    transport = DemoTransport()
    decision, grant = grant_for(
        tmp_path,
        monkeypatch,
        transport=transport,
        env=demo_env(**{ENV_API_URL: endpoint}),
    )
    assert decision.allowed is False
    assert grant is None
    assert transport.calls == []
    with pytest.raises(ValueError, match="DemoRuntimeGrant"):
        build_demo_execution_runner(
            grant=None,
            market_provider=StaticMarketSnapshotProvider(snapshot=market(), source="m"),
            order_db_path=tmp_path / ORDERS_DB,
            decision_db_path=tmp_path / DECISION_DB,
            journal_db_path=tmp_path / JOURNAL_DB,
            clock=lambda: NOW,
        )


@pytest.mark.parametrize(
    "env",
    [
        pytest.param(demo_env(**{ENV_API_TOKEN: None}), id="missing-token"),
        pytest.param(demo_env(**{ENV_ACCOUNT_ID: None}), id="missing-account"),
        pytest.param(
            demo_env(**{ENV_ACCOUNT_ID: "101-000-SOMEONEELSE-002"}), id="other-account"
        ),
        pytest.param(demo_env(**{ENV_LIVE_API_TOKEN: "live-token"}), id="live-token-present"),
        pytest.param(demo_env(**{ENV_LIVE_ACCOUNT_ID: "001-004-LIVE-001"}), id="live-account"),
        pytest.param(demo_env(**{ENV_LIVE_API_URL: OANDA_LIVE_API_URL}), id="live-endpoint"),
        pytest.param(
            demo_env(**{ENV_ENABLE_LIVE_TRADING: ENABLE_LIVE_TRADING_TRUE}), id="live-flag"
        ),
    ],
)
def test_a_credential_or_account_mismatch_is_refused_before_any_broker_call(
    tmp_path, monkeypatch, env
):
    transport = DemoTransport()
    decision, grant = grant_for(tmp_path, monkeypatch, transport=transport, env=env)
    assert decision.allowed is False
    assert grant is None
    assert transport.calls == [], "the broker must not be contacted by a refused startup"


def test_a_mode_mismatch_is_refused_before_any_broker_call(tmp_path, monkeypatch):
    transport = DemoTransport()
    decision, grant = grant_for(
        tmp_path, monkeypatch, transport=transport, execution_mode="fake"
    )
    assert decision.allowed is False
    assert grant is None
    assert DemoLockCode.EXECUTION_MODE_NOT_DEMO.value in decision.rejection_codes
    assert transport.calls == []


def test_a_policy_that_does_not_allow_the_demo_target_composes_nothing(tmp_path, monkeypatch):
    transport = DemoTransport()
    decision, grant = grant_for(
        tmp_path,
        monkeypatch,
        transport=transport,
        config_overrides={"allowed_demo_targets": ["somewhere-else"]},
    )
    assert decision.allowed is False
    assert grant is None
    assert DemoLockCode.DEMO_TARGET_NOT_ALLOWED.value in decision.rejection_codes


def test_a_gate_identity_mismatch_stops_the_submission(tmp_path, monkeypatch):
    """The gate RE-derives the broker identity per submission: an adapter
    whose identity drifted after composition fails closed."""
    from broker.broker_models import BrokerConnectionIdentity

    runner, transport, grant = build_demo(tmp_path, monkeypatch)
    register(tmp_path)
    object.__setattr__(
        grant.connection.broker,
        "_connection_identity",
        BrokerConnectionIdentity(mode="live", target="oanda-live"),
    )

    outcome = only(runner.run_cycle())
    assert outcome.status is RunnerItemStatus.REJECTED
    assert outcome.broker_contact is BrokerContact.NOT_SENT
    assert SafetyRejectionCode.EXECUTION_MODE_MISMATCH.value in outcome.rejection_codes
    assert transport.submit_calls == []


# --- duplicate / restart / partial failure --------------------------------------


def test_the_same_intent_is_never_submitted_twice_in_one_cycle(tmp_path, monkeypatch):
    runner, transport, _grant = build_demo(tmp_path, monkeypatch)
    register(tmp_path)
    # A work source that hands the SAME intent over twice in one selection.
    runner._executor.select = lambda *, limit: ("exec-1", "exec-1")

    report = runner.run_cycle()
    assert report.processed == 2
    assert report.broker_sends == 1
    assert [item.status for item in report.outcomes] == [
        RunnerItemStatus.SUBMITTED,
        RunnerItemStatus.SKIPPED,
    ]
    assert len(transport.submit_calls) == 1


def test_a_second_cycle_never_resubmits_a_sent_intent(tmp_path, monkeypatch):
    runner, transport, _grant = build_demo(tmp_path, monkeypatch)
    register(tmp_path)

    assert runner.run_cycle().broker_sends == 1
    second = runner.run_cycle()
    # The intent left phase 'created', so it is not even selected again.
    assert second.processed == 0
    assert len(transport.submit_calls) == 1


def test_a_restart_on_the_same_stores_never_resubmits(tmp_path, monkeypatch):
    """Restart safety is a property of the DURABLE stores: a fresh runner,
    fresh adapter and fresh boundary over the same SQLite files must not send
    a second order."""
    runner, transport, _grant = build_demo(tmp_path, monkeypatch)
    register(tmp_path)
    assert runner.run_cycle().broker_sends == 1

    # "Restart": brand new objects, same files, a transport that would raise
    # if a second order were ever posted.
    restarted, second_transport, _grant2 = build_demo(
        tmp_path, monkeypatch, order_responses=[]
    )
    report = restarted.run_cycle()
    assert report.processed == 0
    assert second_transport.submit_calls == []
    assert stored(tmp_path)["order_state"] == OrderState.FILLED.value


def test_a_forced_duplicate_after_a_restart_is_refused_before_the_broker(tmp_path, monkeypatch):
    """Even when a driver hands the SAME intent to a restarted boundary, the
    ``created -> submitting`` transition is won exactly once."""
    runner, transport, _grant = build_demo(tmp_path, monkeypatch)
    register(tmp_path)
    assert runner.run_cycle().broker_sends == 1

    restarted, second_transport, _grant2 = build_demo(
        tmp_path, monkeypatch, order_responses=[]
    )
    # A driver that re-offers the already-submitted intent after a restart.
    restarted._executor.select = lambda *, limit: ("exec-1",)

    outcome = only(restarted.run_cycle())
    assert outcome.status is RunnerItemStatus.REJECTED
    assert outcome.broker_contact is BrokerContact.NOT_SENT
    assert SafetyRejectionCode.DUPLICATE_SUBMISSION.value in outcome.rejection_codes
    assert second_transport.submit_calls == []


def test_a_partial_failure_before_the_broker_sends_nothing(tmp_path, monkeypatch):
    """Journal failure BEFORE the broker call: fail closed, provably unsent."""
    runner, transport, _grant = build_demo(tmp_path, monkeypatch)
    register(tmp_path)
    # A journal file that cannot be written is a pre-broker failure.
    (tmp_path / JOURNAL_DB).write_bytes(b"not a database")

    outcome = only(runner.run_cycle())
    assert outcome.status is RunnerItemStatus.REJECTED
    assert outcome.broker_contact is BrokerContact.NOT_SENT
    assert SafetyRejectionCode.SUBMISSION_JOURNAL_FAILED.value in outcome.rejection_codes
    assert transport.submit_calls == []
    assert stored(tmp_path)["submission_phase"] == execution_store.PHASE_CREATED


def test_a_timeout_after_the_broker_call_stays_unresolved_and_is_never_resent(
    tmp_path, monkeypatch
):
    """Partial failure AFTER the broker call: the outcome is UNKNOWN, the
    journal keeps the attempt, and no cycle re-sends it."""
    from broker.oanda_v20_adapter import TransportTimeoutError

    runner, transport, _grant = build_demo(
        tmp_path, monkeypatch, order_responses=[TransportTimeoutError("timed out")]
    )
    register(tmp_path)

    outcome = only(runner.run_cycle())
    assert outcome.status is RunnerItemStatus.FAILED
    assert outcome.broker_contact is BrokerContact.UNRESOLVED

    row = stored(tmp_path)
    assert row["order_state"] == OrderState.UNKNOWN.value
    unresolved = execution_store.list_unresolved_submissions(db_path=tmp_path / ORDERS_DB)
    assert [item["execution_request_id"] for item in unresolved] == ["exec-1"]
    assert len(transport.submit_calls) == 1

    # A later cycle -- and a restart -- must not try again.
    assert runner.run_cycle().processed == 0
    restarted, second_transport, _grant2 = build_demo(tmp_path, monkeypatch, order_responses=[])
    assert restarted.run_cycle().processed == 0
    assert second_transport.submit_calls == []


def test_a_save_failure_after_the_demo_broker_responded_is_unresolved_not_retried(
    tmp_path, monkeypatch
):
    """Partial failure on the OTHER side of the broker call: the demo broker
    answered but the outcome could not be persisted. The runner reports
    UNRESOLVED (never "not sent"), the JOURNAL keeps the attempt for
    reconciliation, and the intent is never re-sent."""
    runner, transport, _grant = build_demo(tmp_path, monkeypatch)
    register(tmp_path)
    real_record = execution_store.record_order_result

    def broken(*args, **kwargs):
        raise execution_store.ExecutionStoreError("disk went away")

    monkeypatch.setattr(execution_store, "record_order_result", broken)
    report = runner.run_cycle()
    monkeypatch.setattr(execution_store, "record_order_result", real_record)

    outcome = only(report)
    assert outcome.status is RunnerItemStatus.FAILED
    assert outcome.broker_contact is BrokerContact.UNRESOLVED
    assert report.unresolved == 1 and not report.ok
    assert len(transport.submit_calls) == 1
    assert stored(tmp_path)["order_state"] == OrderState.UNKNOWN.value
    assert journal_entry(tmp_path).outcome == OUTCOME_RECONCILIATION_REQUIRED
    assert SubmissionJournal(tmp_path / JOURNAL_DB).list_unresolved() != ()

    # A forced re-selection is refused BEFORE the broker, on this runner and
    # on a restarted one.
    runner._executor.select = lambda *, limit: ("exec-1",)
    outcome = only(runner.run_cycle())
    assert SafetyRejectionCode.DUPLICATE_SUBMISSION.value in outcome.rejection_codes
    assert len(transport.submit_calls) == 1


# --- Fake / Shadow / Demo separation --------------------------------------------


def test_the_demo_executor_refuses_a_fake_boundary_and_vice_versa(tmp_path, monkeypatch):
    runner, _transport, _grant = build_demo(tmp_path, monkeypatch)
    demo_submission = runner._executor._submission
    provider = StaticMarketSnapshotProvider(snapshot=market(), source="m")

    with pytest.raises(RunnerModeError, match="'fake' submission boundary"):
        FakeSubmissionCycleExecutor(submission=demo_submission, market_provider=provider)

    class ClaimsFake:
        execution_mode = ExecutionMode.FAKE

    with pytest.raises(ValueError):
        DemoSubmissionCycleExecutor(submission=ClaimsFake(), market_provider=provider)


def test_the_fake_composition_root_refuses_a_demo_connection(tmp_path, monkeypatch):
    from execution.safety_gate import SafetyPolicy

    _decision, grant = grant_for(tmp_path, monkeypatch, transport=DemoTransport())
    with pytest.raises(RunnerModeError):
        build_fake_execution_runner(
            connection=grant.connection,
            policy=grant.policy,
            state_provider=StaticSafetyState(),
            market_provider=StaticMarketSnapshotProvider(snapshot=market(), source="m"),
            order_db_path=tmp_path / ORDERS_DB,
            decision_db_path=tmp_path / DECISION_DB,
            journal_db_path=tmp_path / JOURNAL_DB,
            clock=lambda: NOW,
        )
    assert isinstance(grant.policy, SafetyPolicy)


def test_demo_and_fake_never_share_persistence_by_accident(tmp_path, monkeypatch):
    """Fake and Demo use the SAME store shapes, so an operator separates them
    by data directory. This pins that the runner writes exactly where it was
    pointed -- nothing is implicitly shared, and the shadow store is never
    touched by either."""
    demo_dir = tmp_path / "demo"
    demo_dir.mkdir()
    runner, _transport, _grant = build_demo(
        tmp_path,
        monkeypatch,
        order_db_path=demo_dir / ORDERS_DB,
        decision_db_path=demo_dir / DECISION_DB,
        journal_db_path=demo_dir / JOURNAL_DB,
    )
    execution_store.create_execution_order(
        request(),
        client_order_id="client-1",
        created_at_utc=NOW,
        db_path=demo_dir / ORDERS_DB,
    )
    assert runner.run_cycle().broker_sends == 1

    assert (demo_dir / ORDERS_DB).exists()
    assert not (tmp_path / ORDERS_DB).exists()
    assert not (tmp_path / "shadow_results.sqlite3").exists()
    assert not (demo_dir / "shadow_results.sqlite3").exists()


def test_the_demo_runner_source_id_names_the_execution_order_store(tmp_path, monkeypatch):
    runner, _transport, _grant = build_demo(tmp_path, monkeypatch)
    assert runner._executor.source_id.startswith("execution_orders:")
    assert runner._executor.execution_mode is ExecutionMode.DEMO


# --- live stays locked ----------------------------------------------------------


def test_nothing_on_the_demo_path_can_address_the_live_endpoint(tmp_path, monkeypatch):
    """The adapter behind a demo grant is pinned to the practice endpoint by
    its own construction-time allowlist, and the transport asserts it on
    every request."""
    runner, transport, grant = build_demo(tmp_path, monkeypatch)
    register(tmp_path)
    runner.run_cycle()

    assert type(grant.connection.broker) is OandaDemoAdapter
    assert grant.connection.execution_mode is ExecutionMode.DEMO
    assert all(call["url"].startswith(OANDA_DEMO_API_URL) for call in transport.calls)
    assert transport.calls, "the demo path was actually exercised"


def test_a_demo_grant_can_never_carry_a_live_connection(tmp_path, monkeypatch):
    _decision, grant = grant_for(tmp_path, monkeypatch, transport=DemoTransport())
    assert ExecutionMode.LIVE not in grant.policy.allowed_connection_targets
    with pytest.raises(ValueError):
        DemoRuntimeGrant(
            connection=grant.connection,
            policy=grant.policy,
            kill_switch_db_path="not-a-path",
        )
