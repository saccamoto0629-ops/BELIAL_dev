"""Issue #78: the ShadowExecutionService production boundary -- gate pass /
reject recording, the structural zero-broker-calls guarantee, duplicate and
concurrent-duplicate contracts, store-failure behaviour, dry-run
zero-side-effects, mode validation, and the idempotency-namespace separation
from the live submission path."""

import inspect
import threading
from datetime import timedelta
from decimal import Decimal

import pytest

from broker import fake_broker
from execution import execution_store, shadow_store
from execution.live_submission import LiveOrderSubmissionService
from execution.safety_gate import (
    AccountSafetySnapshot,
    BROKER_TYPE_BY_MODE,
    BrokerConnection,
    ExecutionMode,
    UNIMPLEMENTED_BROKER_MODES,
    resolve_execution_mode,
)
from execution.safety_store import SQLiteSafetyDecisionStore
from execution.shadow_execution import (
    ShadowCycleOutcome,
    ShadowCycleStatus,
    ShadowExecutionService,
)
from shadow_helpers import (
    NOW,
    make_account,
    make_gate,
    make_market,
    make_policy,
    make_position,
    make_request,
    make_service,
)


def run_cycle(service, request=None, **overrides):
    values = dict(
        market=make_market(),
        account=make_account(),
        client_order_id="client-1",
        timeframe="H1",
    )
    values.update(overrides)
    return service.run_cycle(request if request is not None else make_request(), **values)


# --- zero broker calls: structural + broker-double guarantees ------------------


def test_service_has_no_broker_parameter_anywhere():
    """The 'no broker call' contract is structural: neither the constructor
    nor any public method accepts a broker/connection."""
    for _, method in inspect.getmembers(ShadowExecutionService, inspect.isfunction):
        for name in inspect.signature(method).parameters:
            assert "broker" not in name and "connection" not in name or name == "connection_target", (
                f"unexpected broker-ish parameter {name!r} on {method.__qualname__}"
            )


def test_full_cycles_never_touch_any_broker_api(tmp_path, monkeypatch):
    """Poison EVERY FakeBroker API (submit / cancel / order / position /
    account query): a full shadow pass, reject, duplicate and dry-run cycle
    must complete without reaching any of them."""
    calls = []

    def poisoned(name):
        def method(self, *args, **kwargs):
            calls.append(name)
            raise AssertionError(f"shadow mode must never call Broker.{name}")

        return method

    for name in ("submit_order", "cancel_order", "get_order", "get_positions", "get_account_state"):
        monkeypatch.setattr(fake_broker.FakeBroker, name, poisoned(name))

    service = make_service(tmp_path)
    assert run_cycle(service).status is ShadowCycleStatus.RECORDED
    assert run_cycle(service).status is ShadowCycleStatus.DUPLICATE
    rejected = run_cycle(
        service,
        make_request(execution_request_id="exec-2", signal_id="signal-2"),
        market=make_market(market_open=False),
        client_order_id="client-2",
    )
    assert rejected.status is ShadowCycleStatus.RECORDED and not rejected.result.allowed
    dry = service.dry_run_cycle(
        make_request(execution_request_id="exec-3", signal_id="signal-3"),
        market=make_market(),
        account=make_account(),
        client_order_id="client-3",
    )
    assert dry.status is ShadowCycleStatus.DRY_RUN
    assert calls == []


# --- gate pass / reject recording ---------------------------------------------


def test_gate_pass_records_shadow_result_and_decision(tmp_path):
    service = make_service(tmp_path)
    outcome = run_cycle(service)
    assert outcome.status is ShadowCycleStatus.RECORDED
    result = outcome.result
    assert result.allowed and result.shadow_result_id == 1 and result.safety_decision_id == 1
    assert result.decision.execution_mode == "shadow"
    assert result.decision.connection_target == "shadow"
    assert result.decision.account_id == "acct-1" and result.decision.position_count == 0
    assert result.intended_order.client_order_id == "client-1"
    stored = shadow_store.get_shadow_result("exec-1", db_path=service.shadow_db_path)
    assert stored == result
    decisions = SQLiteSafetyDecisionStore(service.decision_db_path).list_for_request("exec-1")
    assert len(decisions) == 1 and decisions[0] == result.decision


def test_gate_reject_records_shadow_result_with_codes(tmp_path):
    service = make_service(tmp_path)
    outcome = run_cycle(service, market=make_market(observed_at_utc=NOW - timedelta(seconds=60)))
    result = outcome.result
    assert outcome.status is ShadowCycleStatus.RECORDED and not result.allowed
    assert result.rejection_codes == ("reference_price_stale",)
    stored = shadow_store.get_shadow_result("exec-1", db_path=service.shadow_db_path)
    assert stored.rejection_codes == ("reference_price_stale",)
    assert any("age" in item.message for item in stored.decision.rejections)


def test_kill_switch_rejection_is_recorded(tmp_path):
    service = make_service(tmp_path, gate=make_gate(kill_switch=True))
    outcome = run_cycle(service)
    assert not outcome.result.allowed
    assert outcome.result.rejection_codes == ("kill_switch_enabled",)


def test_shadow_target_mismatch_is_recorded(tmp_path):
    policy = make_policy(
        allowed_connection_targets={ExecutionMode.SHADOW: frozenset({"elsewhere"})}
    )
    service = make_service(tmp_path, gate=make_gate(policy))
    outcome = run_cycle(service)
    assert outcome.result.rejection_codes == ("execution_mode_mismatch",)


def test_stale_account_snapshot_fails_closed(tmp_path):
    service = make_service(tmp_path)
    outcome = run_cycle(
        service, account=make_account(retrieved_at_utc=NOW - timedelta(seconds=120))
    )
    assert outcome.result.rejection_codes == ("broker_query_failed",)


def test_position_limits_use_the_injected_snapshot(tmp_path):
    policy = make_policy(max_positions_per_symbol=1)
    service = make_service(tmp_path, gate=make_gate(policy))
    outcome = run_cycle(service, account=make_account(positions=(make_position(),)))
    assert "symbol_position_limit_exceeded" in outcome.result.rejection_codes


# --- duplicates ----------------------------------------------------------------


def test_duplicate_request_returns_stored_result_without_second_row(tmp_path):
    service = make_service(tmp_path)
    first = run_cycle(service)
    second = run_cycle(service)
    assert second.status is ShadowCycleStatus.DUPLICATE
    assert second.result == first.result
    assert len(shadow_store.list_shadow_results(db_path=service.shadow_db_path)) == 1
    # same signal under a new execution_request_id is also a duplicate
    third = run_cycle(
        service, make_request(execution_request_id="exec-9"), client_order_id="client-9"
    )
    assert third.status is ShadowCycleStatus.DUPLICATE and third.result == first.result


def test_concurrent_duplicates_yield_exactly_one_row(tmp_path):
    """20+ workers race the same request: exactly one RECORDED, the rest are
    explicit DUPLICATEs carrying the stored row -- never an error, never a
    second row."""
    worker_count = 20
    services = [make_service(tmp_path) for _ in range(worker_count)]
    barrier = threading.Barrier(worker_count)
    outcomes: list = [None] * worker_count

    def work(index):
        barrier.wait(timeout=60)
        try:
            outcomes[index] = run_cycle(services[index])
        except Exception as exc:  # pragma: no cover - would fail the test
            outcomes[index] = exc

    threads = [threading.Thread(target=work, args=(i,)) for i in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert all(isinstance(outcome, ShadowCycleOutcome) for outcome in outcomes), outcomes
    statuses = [outcome.status for outcome in outcomes]
    assert statuses.count(ShadowCycleStatus.RECORDED) == 1
    assert statuses.count(ShadowCycleStatus.DUPLICATE) == worker_count - 1
    rows = shadow_store.list_shadow_results(db_path=services[0].shadow_db_path)
    assert len(rows) == 1
    assert all(outcome.result.shadow_result_id == rows[0].shadow_result_id for outcome in outcomes)


# --- store failures ------------------------------------------------------------


def test_shadow_save_failure_raises_and_leaves_no_partial_state(tmp_path, monkeypatch):
    service = make_service(tmp_path)

    def explode(*args, **kwargs):
        raise shadow_store.ShadowStoreError("injected shadow save failure")

    monkeypatch.setattr(shadow_store, "record_shadow_result", explode)
    with pytest.raises(shadow_store.ShadowStoreError, match="injected"):
        run_cycle(service)
    monkeypatch.undo()
    assert shadow_store.list_shadow_results(db_path=service.shadow_db_path) == ()
    # no dangling idempotency claim: the same cycle succeeds afterwards
    assert run_cycle(service).status is ShadowCycleStatus.RECORDED


def test_decision_persist_failure_records_explicit_recoverable_state(tmp_path, monkeypatch):
    service = make_service(tmp_path)

    def explode(self, decision):
        raise RuntimeError("injected decision store failure")

    monkeypatch.setattr(SQLiteSafetyDecisionStore, "record", explode)
    outcome = run_cycle(service)
    monkeypatch.undo()
    result = outcome.result
    assert outcome.status is ShadowCycleStatus.RECORDED
    assert not result.allowed
    assert result.rejection_codes == ("decision_persist_failed",)
    assert result.safety_decision_id is None
    assert SQLiteSafetyDecisionStore(service.decision_db_path).list_for_request("exec-1") == ()


# --- dry-run -------------------------------------------------------------------


def test_dry_run_creates_no_files_and_persists_nothing(tmp_path):
    service = make_service(tmp_path, initialize=False)
    outcome = service.dry_run_cycle(
        make_request(), market=make_market(), account=make_account(), client_order_id="client-1"
    )
    assert outcome.status is ShadowCycleStatus.DRY_RUN
    assert outcome.result.allowed
    assert outcome.result.shadow_result_id is None and outcome.result.safety_decision_id is None
    assert not service.shadow_db_path.exists()
    assert not service.decision_db_path.exists()


def test_dry_run_probes_existing_dbs_read_only(tmp_path):
    service = make_service(tmp_path)  # initialized
    run_cycle(service)
    before_shadow = service.shadow_db_path.read_bytes()
    before_decisions = service.decision_db_path.read_bytes()
    outcome = service.dry_run_cycle(
        make_request(execution_request_id="exec-2", signal_id="signal-2"),
        market=make_market(),
        account=make_account(),
        client_order_id="client-2",
    )
    assert outcome.status is ShadowCycleStatus.DRY_RUN
    assert service.shadow_db_path.read_bytes() == before_shadow
    assert service.decision_db_path.read_bytes() == before_decisions


def test_dry_run_fails_explicitly_on_a_corrupted_shadow_db(tmp_path):
    service = make_service(tmp_path, initialize=False)
    service.shadow_db_path.write_bytes(b"not a sqlite file at all")
    with pytest.raises(Exception):
        service.dry_run_cycle(
            make_request(), market=make_market(), account=make_account(), client_order_id="client-1"
        )


# --- execution mode vocabulary -------------------------------------------------


def test_resolve_execution_mode_accepts_exactly_the_five_modes():
    assert resolve_execution_mode("dry-run") is ExecutionMode.DRY_RUN
    assert resolve_execution_mode("shadow") is ExecutionMode.SHADOW
    assert resolve_execution_mode("fake") is ExecutionMode.FAKE
    assert resolve_execution_mode("demo") is ExecutionMode.DEMO
    assert resolve_execution_mode("live") is ExecutionMode.LIVE
    assert resolve_execution_mode(ExecutionMode.SHADOW) is ExecutionMode.SHADOW
    for bogus in ("SHADOW", " shadow", "paper", "", None, 7):
        with pytest.raises(ValueError):
            resolve_execution_mode(bogus)


def test_demo_and_live_stay_unimplemented_and_unconnectable():
    assert UNIMPLEMENTED_BROKER_MODES == {ExecutionMode.DEMO, ExecutionMode.LIVE}
    for mode in (ExecutionMode.DEMO, ExecutionMode.LIVE, ExecutionMode.SHADOW, ExecutionMode.DRY_RUN):
        assert mode not in BROKER_TYPE_BY_MODE


def test_shadow_service_constructor_validation(tmp_path):
    gate = make_gate()
    with pytest.raises(ValueError, match="gate"):
        ShadowExecutionService(
            gate="nope", decision_db_path=tmp_path / "d", shadow_db_path=tmp_path / "s",
            clock=lambda: NOW,
        )
    with pytest.raises(ValueError, match="sealed"):
        ShadowExecutionService(
            gate=gate, decision_db_path=object(), shadow_db_path=tmp_path / "s",
            clock=lambda: NOW,
        )
    with pytest.raises(ValueError, match="shadow_db_path"):
        ShadowExecutionService(
            gate=gate, decision_db_path=tmp_path / "d", shadow_db_path=object(),
            clock=lambda: NOW,
        )
    with pytest.raises(ValueError, match="connection_target"):
        ShadowExecutionService(
            gate=gate, decision_db_path=tmp_path / "d", shadow_db_path=tmp_path / "s",
            clock=lambda: NOW, connection_target=" ",
        )


# --- namespace separation from the live submission path ------------------------


def test_shadow_run_does_not_block_a_later_fake_submission(tmp_path):
    """Issue #78: running shadow for a signal must NOT make a later fake /
    demo / live submission of the same signal look 'already submitted'."""
    service = make_service(tmp_path)
    assert run_cycle(service).status is ShadowCycleStatus.RECORDED

    broker = fake_broker.FakeBroker(default_fill_price=Decimal("150"))
    policy = make_policy(
        allowed_connection_targets={
            ExecutionMode.FAKE: frozenset({"fake"}),
            ExecutionMode.SHADOW: frozenset({"shadow"}),
        }
    )
    live = LiveOrderSubmissionService(
        connection=BrokerConnection(broker=broker),
        gate=make_gate(policy),
        decision_db_path=service.decision_db_path,
        order_db_path=tmp_path / "orders.sqlite3",
        clock=lambda: NOW,
    )
    request = make_request()  # SAME signal and SAME execution_request_id
    live.register(request, client_order_id="client-1")
    decision, result = live.submit(request.execution_request_id, market=make_market())
    assert decision.allowed and result is not None
    assert len(broker.submitted_requests) == 1


def test_live_registration_does_not_block_a_shadow_run(tmp_path):
    broker = fake_broker.FakeBroker(default_fill_price=Decimal("150"))
    policy = make_policy(
        allowed_connection_targets={
            ExecutionMode.FAKE: frozenset({"fake"}),
            ExecutionMode.SHADOW: frozenset({"shadow"}),
        }
    )
    live = LiveOrderSubmissionService(
        connection=BrokerConnection(broker=broker),
        gate=make_gate(policy),
        decision_db_path=tmp_path / "safety.sqlite3",
        order_db_path=tmp_path / "orders.sqlite3",
        clock=lambda: NOW,
    )
    SQLiteSafetyDecisionStore(tmp_path / "safety.sqlite3").initialize()
    request = make_request()
    live.register(request, client_order_id="client-1")
    service = make_service(tmp_path)
    assert run_cycle(service).status is ShadowCycleStatus.RECORDED
    # the execution order store was never touched by the shadow run
    row = execution_store.get_execution_order(
        request.execution_request_id, db_path=tmp_path / "orders.sqlite3"
    )
    assert row["submission_phase"] == execution_store.PHASE_CREATED


# --- AccountSafetySnapshot validation -------------------------------------------


def test_account_snapshot_validates_container_types():
    with pytest.raises(ValueError, match="positions"):
        AccountSafetySnapshot(positions=[make_position()], account=make_account().account)
    with pytest.raises(ValueError, match="PositionInfo"):
        AccountSafetySnapshot(positions=("x",), account=make_account().account)
    with pytest.raises(ValueError, match="AccountState"):
        AccountSafetySnapshot(positions=(), account="nope")


def test_evaluate_shadow_argument_validation(tmp_path):
    gate = make_gate()
    with pytest.raises(ValueError, match="request"):
        gate.evaluate_shadow(
            "nope", market=make_market(), account=make_account(), connection_target="shadow"
        )
    with pytest.raises(ValueError, match="market"):
        gate.evaluate_shadow(
            make_request(), market="nope", account=make_account(), connection_target="shadow"
        )
    with pytest.raises(ValueError, match="account"):
        gate.evaluate_shadow(
            make_request(), market=make_market(), account="nope", connection_target="shadow"
        )
    with pytest.raises(ValueError, match="connection_target"):
        gate.evaluate_shadow(
            make_request(), market=make_market(), account=make_account(), connection_target=" "
        )
