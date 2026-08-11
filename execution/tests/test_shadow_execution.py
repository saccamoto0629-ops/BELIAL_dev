"""Issue #78: the ShadowExecutionService production boundary -- claim-first
evaluation (decision 1:1), gate pass / reject recording, the structural
zero-broker-calls guarantee, duplicate / conflict contracts, crash and stale
claim recovery, store-failure behaviour, dry-run zero-side-effects, mode
validation, and the idempotency-namespace separation from live submission."""

import inspect
import sqlite3
import threading
from datetime import timedelta
from decimal import Decimal

import pytest

from broker import fake_broker
from execution import execution_store, shadow_store
from execution.live_submission import LiveOrderSubmissionService
from execution.reissue_service import ReissuePolicy
from execution.safety_gate import (
    AccountSafetySnapshot,
    BROKER_TYPE_BY_MODE,
    BrokerConnection,
    ExecutionMode,
    UNIMPLEMENTED_BROKER_MODES,
    resolve_execution_mode,
)
from execution.safety_store import SQLiteSafetyDecisionStore
from execution.submission_journal import SubmissionJournal
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
    make_provenance,
    make_request,
    make_service,
)


def run_cycle(service, request=None, **overrides):
    values = dict(
        market=make_market(),
        account=make_account(),
        client_order_id="client-1",
        provenance=make_provenance(),
        timeframe="H1",
    )
    values.update(overrides)
    return service.run_cycle(request if request is not None else make_request(), **values)


def decisions(service, execution_request_id="exec-1"):
    return SQLiteSafetyDecisionStore(service.decision_db_path).list_for_request(
        execution_request_id
    )


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
        provenance=make_provenance(),
    )
    assert dry.status is ShadowCycleStatus.DRY_RUN
    assert calls == []


# --- gate pass / reject recording ---------------------------------------------


def test_gate_pass_records_shadow_result_and_exactly_one_decision(tmp_path):
    service = make_service(tmp_path, owner_id="owner-1")
    outcome = run_cycle(service)
    assert outcome.status is ShadowCycleStatus.RECORDED
    result = outcome.result
    assert result.allowed and result.shadow_result_id == 1 and result.safety_decision_id == 1
    assert result.decision.execution_mode == "shadow"
    assert result.claim_owner_id == "owner-1"
    assert result.provenance == make_provenance()
    stored = shadow_store.get_shadow_result("exec-1", db_path=service.shadow_db_path)
    assert stored == result
    assert len(decisions(service)) == 1


def test_gate_reject_records_shadow_result_with_codes(tmp_path):
    service = make_service(tmp_path)
    outcome = run_cycle(service, market=make_market(observed_at_utc=NOW - timedelta(seconds=60)))
    result = outcome.result
    assert outcome.status is ShadowCycleStatus.RECORDED and not result.allowed
    assert result.rejection_codes == ("reference_price_stale",)


def test_position_limit_rejection_is_replayable_from_the_stored_snapshot(tmp_path):
    policy = make_policy(max_positions_per_symbol=1)
    service = make_service(tmp_path, gate=make_gate(policy))
    account = make_account(positions=(make_position(),))
    outcome = run_cycle(service, account=account)
    assert "symbol_position_limit_exceeded" in outcome.result.rejection_codes
    stored = shadow_store.get_shadow_result("exec-1", db_path=service.shadow_db_path)
    # Audit replay: the persisted snapshot re-produces the same decision.
    replayed = make_gate(policy).evaluate_shadow(
        stored.request, market=stored.market, account=stored.account, connection_target="shadow"
    )
    assert replayed.rejection_codes == stored.decision.rejection_codes


def test_account_limit_rejection_is_replayable_from_the_stored_snapshot(tmp_path):
    policy = make_policy(max_positions_account=1)
    service = make_service(tmp_path, gate=make_gate(policy))
    outcome = run_cycle(service, account=make_account(positions=(make_position("EUR_USD"),)))
    assert "account_position_limit_exceeded" in outcome.result.rejection_codes
    stored = shadow_store.get_shadow_result("exec-1", db_path=service.shadow_db_path)
    replayed = make_gate(policy).evaluate_shadow(
        stored.request, market=stored.market, account=stored.account, connection_target="shadow"
    )
    assert replayed.rejection_codes == stored.decision.rejection_codes


# --- duplicates / conflicts ----------------------------------------------------


def test_duplicate_request_returns_stored_result_without_second_decision(tmp_path):
    service = make_service(tmp_path)
    first = run_cycle(service)
    second = run_cycle(service)
    assert second.status is ShadowCycleStatus.DUPLICATE
    assert second.result == first.result
    assert len(shadow_store.list_shadow_results(db_path=service.shadow_db_path)) == 1
    assert len(decisions(service)) == 1  # the duplicate wrote NO new decision


def test_payload_conflict_raises_and_writes_no_decision(tmp_path):
    service = make_service(tmp_path)
    run_cycle(service)
    with pytest.raises(shadow_store.ShadowIdentityConflictError) as info:
        run_cycle(service, make_request(quantity=Decimal("2")))
    assert info.value.differing_fields == ("quantity",)
    assert len(decisions(service)) == 1
    assert len(shadow_store.list_shadow_results(db_path=service.shadow_db_path)) == 1


def test_concurrent_payload_mismatch_yields_one_winner_and_conflicts(tmp_path):
    services = [make_service(tmp_path, owner_id=f"o{i}") for i in range(4)]
    barrier = threading.Barrier(4)
    outcomes: list = [None] * 4

    def work(index):
        request = make_request(quantity=Decimal(str(index + 1)))
        barrier.wait(timeout=60)
        try:
            outcomes[index] = run_cycle(services[index], request)
        except shadow_store.ShadowIdentityConflictError as exc:
            outcomes[index] = exc

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    winners = [o for o in outcomes if isinstance(o, ShadowCycleOutcome)]
    conflicts = [o for o in outcomes if isinstance(o, shadow_store.ShadowIdentityConflictError)]
    assert len(winners) == 1 and winners[0].status is ShadowCycleStatus.RECORDED
    assert len(conflicts) == 3
    assert len(decisions(services[0])) == 1


@pytest.mark.parametrize("iteration", range(20))
def test_concurrent_duplicates_yield_one_row_one_decision(tmp_path, iteration):
    """Repeated (20x) race: 8 workers, exactly one RECORDED, the rest explicit
    DUPLICATEs, ONE decision row, never an error and never a second row."""
    worker_count = 8
    services = [make_service(tmp_path, owner_id=f"o{i}") for i in range(worker_count)]
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
    assert len(shadow_store.list_shadow_results(db_path=services[0].shadow_db_path)) == 1
    assert len(decisions(services[0])) == 1


def test_twenty_workers_single_recorded(tmp_path):
    worker_count = 20
    services = [make_service(tmp_path, owner_id=f"o{i}") for i in range(worker_count)]
    barrier = threading.Barrier(worker_count)
    outcomes: list = [None] * worker_count

    def work(index):
        barrier.wait(timeout=60)
        outcomes[index] = run_cycle(services[index])

    threads = [threading.Thread(target=work, args=(i,)) for i in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    statuses = [outcome.status for outcome in outcomes]
    assert statuses.count(ShadowCycleStatus.RECORDED) == 1
    assert statuses.count(ShadowCycleStatus.DUPLICATE) == worker_count - 1
    assert len(decisions(services[0])) == 1


# --- ONE service instance shared by many threads (PR #100 re-review Major 1) ---


def _run_shared_service_race(service, worker_count):
    barrier = threading.Barrier(worker_count)
    outcomes: list = [None] * worker_count

    def work(index):
        barrier.wait(timeout=60)
        try:
            outcomes[index] = run_cycle(service)
        except Exception as exc:  # pragma: no cover - would fail the test
            outcomes[index] = exc

    threads = [threading.Thread(target=work, args=(i,)) for i in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    return outcomes


def test_shared_service_twenty_threads_single_winner_single_decision(tmp_path, monkeypatch):
    """20 threads share ONE ShadowExecutionService: attempt-scoped claims must
    yield exactly one winner, one gate evaluation, one decision row, one
    completed record, zero live claims, zero errors -- and no broker call."""
    from execution.safety_gate import SafetyGate

    evaluations = []
    original_evaluate = SafetyGate.evaluate_shadow

    def counting_evaluate(self, *args, **kwargs):
        evaluations.append(1)
        return original_evaluate(self, *args, **kwargs)

    monkeypatch.setattr(SafetyGate, "evaluate_shadow", counting_evaluate)

    calls = []
    for name in ("submit_order", "cancel_order", "get_order", "get_positions", "get_account_state"):
        monkeypatch.setattr(
            fake_broker.FakeBroker, name,
            lambda self, *a, __n=name, **k: calls.append(__n) or (_ for _ in ()).throw(
                AssertionError(f"broker API {__n} must never be reached")
            ),
        )

    service = make_service(tmp_path, owner_id="shared-owner")
    outcomes = _run_shared_service_race(service, 20)
    assert all(isinstance(outcome, ShadowCycleOutcome) for outcome in outcomes), outcomes
    statuses = [outcome.status for outcome in outcomes]
    assert statuses.count(ShadowCycleStatus.RECORDED) == 1
    assert statuses.count(ShadowCycleStatus.DUPLICATE) == 19
    assert len(evaluations) == 1  # the gate ran exactly once
    assert calls == []
    assert len(decisions(service)) == 1
    assert len(shadow_store.list_shadow_results(db_path=service.shadow_db_path)) == 1
    assert shadow_store.list_shadow_claims(db_path=service.shadow_db_path) == ()


@pytest.mark.parametrize("iteration", range(20))
def test_shared_service_race_repeats(tmp_path, iteration):
    """The shared-service race, repeated 20x with 8 threads: never a second
    evaluation path, never an error, never a lock error dressed as anything."""
    service = make_service(tmp_path, owner_id="shared-owner")
    outcomes = _run_shared_service_race(service, 8)
    assert all(isinstance(outcome, ShadowCycleOutcome) for outcome in outcomes), outcomes
    statuses = [outcome.status for outcome in outcomes]
    assert statuses.count(ShadowCycleStatus.RECORDED) == 1
    assert statuses.count(ShadowCycleStatus.DUPLICATE) == 7
    assert len(decisions(service)) == 1
    assert len(shadow_store.list_shadow_results(db_path=service.shadow_db_path)) == 1
    assert shadow_store.list_shadow_claims(db_path=service.shadow_db_path) == ()


def test_stale_holder_cannot_complete_over_a_takeover(tmp_path):
    """Fencing: after a lease-expiry takeover, the ORIGINAL attempt's claim
    (older attempt id / generation) is refused at completion."""
    from shadow_helpers import make_gate as build_gate

    shadow_store.init_db(tmp_path / "shadow.sqlite3")
    status, original = shadow_store.claim_shadow_cycle(
        make_request(),
        client_order_id="client-1",
        timeframe="H1",
        provenance=make_provenance(),
        owner_id="owner-a",
        attempt_id="owner-a-attempt-1",
        now=NOW,
        lease_seconds=60,
        db_path=tmp_path / "shadow.sqlite3",
    )
    assert status == shadow_store.CLAIM_ACQUIRED
    status, takeover = shadow_store.claim_shadow_cycle(
        make_request(),
        client_order_id="client-1",
        timeframe="H1",
        provenance=make_provenance(),
        owner_id="owner-b",
        attempt_id="owner-b-attempt-1",
        now=NOW + timedelta(seconds=61),
        lease_seconds=60,
        db_path=tmp_path / "shadow.sqlite3",
    )
    assert status == shadow_store.CLAIM_ACQUIRED and takeover.claim_generation == 2
    gate = build_gate()
    decision = gate.evaluate_shadow(
        make_request(), market=make_market(), account=make_account(), connection_target="shadow"
    )
    with pytest.raises(shadow_store.ShadowStoreError, match="lost"):
        shadow_store.complete_shadow_cycle(
            "exec-1",
            claim=original,  # stale attempt/generation
            decision=decision,
            market=make_market(),
            account=make_account(),
            policy_snapshot=gate.policy.to_snapshot(),
            safety_decision_id=None,
            completed_at_utc=NOW,
            db_path=tmp_path / "shadow.sqlite3",
        )


def test_lock_errors_are_store_failures_not_duplicates(tmp_path, monkeypatch):
    service = make_service(tmp_path)

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(shadow_store, "_connect_rw", locked)
    with pytest.raises(shadow_store.ShadowStoreError, match="locked"):
        run_cycle(service)


# --- crash / stale claim recovery ----------------------------------------------


def _fail_completion_once(monkeypatch):
    original = shadow_store.complete_shadow_cycle
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise shadow_store.ShadowStoreError("injected completion failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(shadow_store, "complete_shadow_cycle", flaky)


def test_active_claim_blocks_retries_until_the_lease_expires(tmp_path, monkeypatch):
    """PR #100 re-review Major 1: after a completion failure the claim stays
    ACTIVE until its lease expires -- a retry (same service included) is an
    in-progress duplicate, and recovery happens via lease-expiry takeover."""
    service = make_service(tmp_path, owner_id="owner-1", claim_lease_seconds=60)
    _fail_completion_once(monkeypatch)
    with pytest.raises(shadow_store.ShadowStoreError, match="injected"):
        run_cycle(service)
    monkeypatch.undo()
    claim = shadow_store.get_shadow_claim("exec-1", db_path=service.shadow_db_path)
    assert claim is not None and claim.claim_owner_id == "owner-1"
    # a retry on the SAME service before the lease expires does not evaluate
    blocked = run_cycle(service)
    assert blocked.status is ShadowCycleStatus.DUPLICATE and blocked.claim is not None
    assert len(decisions(service)) == 1  # only the crashed attempt evaluated
    # after the lease expires, a takeover attempt recovers and completes
    LATER = NOW + timedelta(seconds=61)
    recovery = make_service(
        tmp_path, owner_id="owner-1", claim_lease_seconds=60, clock=lambda: LATER
    )
    retry = run_cycle(
        recovery,
        market=make_market(observed_at_utc=LATER),
        account=make_account(retrieved_at_utc=LATER),
    )
    assert retry.status is ShadowCycleStatus.RECORDED
    assert shadow_store.get_shadow_claim("exec-1", db_path=service.shadow_db_path) is None
    # the failed attempt's decision remains as a REPORTABLE orphan
    assert len(decisions(service)) == 2
    assert retry.result.safety_decision_id == 2


def test_stale_claim_is_recovered_by_another_owner_after_the_lease(tmp_path, monkeypatch):
    crashed = make_service(tmp_path, owner_id="owner-a", claim_lease_seconds=60)
    _fail_completion_once(monkeypatch)
    with pytest.raises(shadow_store.ShadowStoreError):
        run_cycle(crashed)
    monkeypatch.undo()

    LATER = NOW + timedelta(seconds=61)
    other = make_service(
        tmp_path, owner_id="owner-b", claim_lease_seconds=60, clock=lambda: LATER
    )
    # before the lease expires a third owner only sees an in-progress duplicate
    early = make_service(
        tmp_path, owner_id="owner-c", claim_lease_seconds=60,
        clock=lambda: NOW + timedelta(seconds=30),
    )
    blocked = run_cycle(
        early,
        market=make_market(observed_at_utc=NOW + timedelta(seconds=30)),
        account=make_account(retrieved_at_utc=NOW + timedelta(seconds=30)),
    )
    assert blocked.status is ShadowCycleStatus.DUPLICATE and blocked.claim is not None
    recovered = run_cycle(
        other,
        market=make_market(observed_at_utc=LATER),
        account=make_account(retrieved_at_utc=LATER),
    )
    assert recovered.status is ShadowCycleStatus.RECORDED
    assert recovered.result.claim_owner_id == "owner-b"


def test_process_restart_equivalent_still_deduplicates(tmp_path):
    first = make_service(tmp_path, owner_id="owner-1")
    stored = run_cycle(first).result
    # a NEW service instance with a NEW owner (fresh process) after completion
    restarted = make_service(tmp_path, owner_id="owner-2")
    outcome = run_cycle(restarted)
    assert outcome.status is ShadowCycleStatus.DUPLICATE and outcome.result == stored
    assert len(decisions(first)) == 1


# --- decision persistence failure ----------------------------------------------


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
    assert decisions(service) == ()


# --- dry-run -------------------------------------------------------------------


def test_dry_run_creates_no_files_and_persists_nothing(tmp_path):
    service = make_service(tmp_path, initialize=False)
    outcome = service.dry_run_cycle(
        make_request(), market=make_market(), account=make_account(),
        client_order_id="client-1", provenance=make_provenance(),
    )
    assert outcome.status is ShadowCycleStatus.DRY_RUN
    assert outcome.result.allowed
    assert outcome.result.shadow_result_id is None and outcome.result.safety_decision_id is None
    assert not service.shadow_db_path.exists()
    assert not service.decision_db_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_dry_run_probes_existing_dbs_without_touching_bytes_or_mtime(tmp_path):
    service = make_service(tmp_path)
    run_cycle(service)
    shadow_stat = (service.shadow_db_path.stat().st_mtime_ns, service.shadow_db_path.stat().st_size)
    decision_stat = (
        service.decision_db_path.stat().st_mtime_ns, service.decision_db_path.stat().st_size,
    )
    files_before = sorted(entry.name for entry in tmp_path.iterdir())
    outcome = service.dry_run_cycle(
        make_request(execution_request_id="exec-2", signal_id="signal-2"),
        market=make_market(), account=make_account(),
        client_order_id="client-2", provenance=make_provenance(),
    )
    assert outcome.status is ShadowCycleStatus.DRY_RUN
    assert (service.shadow_db_path.stat().st_mtime_ns, service.shadow_db_path.stat().st_size) == shadow_stat
    assert (
        service.decision_db_path.stat().st_mtime_ns, service.decision_db_path.stat().st_size,
    ) == decision_stat
    assert sorted(entry.name for entry in tmp_path.iterdir()) == files_before


def test_dry_run_fails_explicitly_on_a_corrupted_shadow_db(tmp_path):
    service = make_service(tmp_path, initialize=False)
    service.shadow_db_path.write_bytes(b"not a sqlite file at all")
    with pytest.raises(Exception):
        service.dry_run_cycle(
            make_request(), market=make_market(), account=make_account(),
            client_order_id="client-1", provenance=make_provenance(),
        )


# --- execution mode vocabulary -------------------------------------------------


def test_resolve_execution_mode_accepts_exactly_the_five_modes():
    assert resolve_execution_mode("dry-run") is ExecutionMode.DRY_RUN
    assert resolve_execution_mode("shadow") is ExecutionMode.SHADOW
    assert resolve_execution_mode("fake") is ExecutionMode.FAKE
    assert resolve_execution_mode("demo") is ExecutionMode.DEMO
    assert resolve_execution_mode("live") is ExecutionMode.LIVE
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
    with pytest.raises(ValueError, match="owner_id"):
        ShadowExecutionService(
            gate=gate, decision_db_path=tmp_path / "d", shadow_db_path=tmp_path / "s",
            clock=lambda: NOW, owner_id=" ",
        )
    with pytest.raises(ValueError, match="claim_lease_seconds"):
        ShadowExecutionService(
            gate=gate, decision_db_path=tmp_path / "d", shadow_db_path=tmp_path / "s",
            clock=lambda: NOW, claim_lease_seconds=0,
        )


# --- namespace separation from the live submission path ------------------------


def _live_service(tmp_path, broker):
    policy = make_policy(
        allowed_connection_targets={
            ExecutionMode.FAKE: frozenset({"fake"}),
            ExecutionMode.SHADOW: frozenset({"shadow"}),
        }
    )
    SQLiteSafetyDecisionStore(tmp_path / "safety.sqlite3").initialize()
    SubmissionJournal(tmp_path / "journal.sqlite3").initialize()
    return LiveOrderSubmissionService(
        connection=BrokerConnection(broker=broker),
        gate=make_gate(policy),
        decision_db_path=tmp_path / "safety.sqlite3",
        order_db_path=tmp_path / "orders.sqlite3",
        journal_db_path=tmp_path / "journal.sqlite3",
        clock=lambda: NOW,
            reissue_policy=ReissuePolicy(),
    )


def test_shadow_run_does_not_block_a_later_fake_submission(tmp_path):
    service = make_service(tmp_path)
    assert run_cycle(service).status is ShadowCycleStatus.RECORDED
    broker = fake_broker.FakeBroker(default_fill_price=Decimal("150"))
    live = _live_service(tmp_path, broker)
    request = make_request()  # SAME signal and SAME execution_request_id
    live.register(request, client_order_id="client-1")
    decision, result = live.submit(request.execution_request_id, market=make_market())
    assert decision.allowed and result is not None
    assert len(broker.submitted_requests) == 1


def test_live_registration_does_not_block_a_shadow_run(tmp_path):
    broker = fake_broker.FakeBroker(default_fill_price=Decimal("150"))
    live = _live_service(tmp_path, broker)
    request = make_request()
    live.register(request, client_order_id="client-1")
    service = make_service(tmp_path)
    assert run_cycle(service).status is ShadowCycleStatus.RECORDED
    row = execution_store.get_execution_order(
        request.execution_request_id, db_path=tmp_path / "orders.sqlite3"
    )
    assert row["submission_phase"] == execution_store.PHASE_CREATED


# --- AccountSafetySnapshot / evaluate_shadow validation -------------------------


def test_account_snapshot_validates_container_types():
    with pytest.raises(ValueError, match="positions"):
        AccountSafetySnapshot(positions=[make_position()], account=make_account().account)
    with pytest.raises(ValueError, match="AccountState"):
        AccountSafetySnapshot(positions=(), account="nope")


def test_stale_account_snapshot_fails_closed(tmp_path):
    service = make_service(tmp_path)
    outcome = run_cycle(
        service, account=make_account(retrieved_at_utc=NOW - timedelta(seconds=120))
    )
    assert outcome.result.rejection_codes == ("broker_query_failed",)


# --- snapshot suppliers: winner-only provider I/O (PR #100 3rd review) ----------


def test_snapshot_suppliers_run_only_for_the_claim_winner(tmp_path):
    service = make_service(tmp_path, owner_id="owner-1", claim_lease_seconds=60)
    calls = {"market": 0, "account": 0}

    def market_supplier():
        calls["market"] += 1
        return make_market()

    def account_supplier():
        calls["account"] += 1
        return make_account()

    outcome = service.run_cycle(
        make_request(),
        market_supplier=market_supplier,
        account_supplier=account_supplier,
        client_order_id="client-1",
        provenance=make_provenance(),
    )
    assert outcome.status is ShadowCycleStatus.RECORDED
    assert calls == {"market": 1, "account": 1}

    def poisoned():
        raise AssertionError("a duplicate loser must not resolve snapshots")

    blocked = service.run_cycle(
        make_request(),
        market_supplier=poisoned,
        account_supplier=poisoned,
        client_order_id="client-1",
        provenance=make_provenance(),
    )
    assert blocked.status is ShadowCycleStatus.DUPLICATE
    assert calls == {"market": 1, "account": 1}
    assert len(decisions(service)) == 1


def test_supplier_failure_leaves_the_claim_for_lease_recovery(tmp_path):
    service = make_service(tmp_path, owner_id="owner-1", claim_lease_seconds=60)

    def failing_market():
        raise RuntimeError("injected provider outage")

    with pytest.raises(RuntimeError, match="injected provider outage"):
        service.run_cycle(
            make_request(),
            market_supplier=failing_market,
            account_supplier=make_account,
            client_order_id="client-1",
            provenance=make_provenance(),
            timeframe="H1",
        )
    # the claim stays held (gate never evaluated, no decision written) ...
    assert shadow_store.get_shadow_claim("exec-1", db_path=service.shadow_db_path) is not None
    assert len(decisions(service)) == 0

    def poisoned():
        raise AssertionError("an in-progress duplicate must not resolve snapshots")

    blocked = service.run_cycle(
        make_request(),
        market_supplier=poisoned,
        account_supplier=poisoned,
        client_order_id="client-1",
        provenance=make_provenance(),
        timeframe="H1",
    )
    assert blocked.status is ShadowCycleStatus.DUPLICATE and blocked.claim is not None
    # ... and the ordinary lease-expiry recovery completes the cycle.
    LATER = NOW + timedelta(seconds=61)
    recovery = make_service(
        tmp_path, owner_id="owner-1", claim_lease_seconds=60, clock=lambda: LATER
    )
    retry = run_cycle(
        recovery,
        market=make_market(observed_at_utc=LATER),
        account=make_account(retrieved_at_utc=LATER),
    )
    assert retry.status is ShadowCycleStatus.RECORDED


def test_exactly_one_snapshot_source_is_required(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(ValueError, match="exactly one of market"):
        service.run_cycle(
            make_request(),
            account=make_account(),
            client_order_id="client-1",
            provenance=make_provenance(),
        )
    with pytest.raises(ValueError, match="exactly one of market"):
        service.run_cycle(
            make_request(),
            market=make_market(),
            market_supplier=lambda: make_market(),
            account=make_account(),
            client_order_id="client-1",
            provenance=make_provenance(),
        )
    # nothing was claimed by the rejected calls
    assert shadow_store.list_shadow_claims(db_path=service.shadow_db_path) == ()
