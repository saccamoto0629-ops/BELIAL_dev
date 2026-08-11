"""Issue #116: reissue clearance judgement, issuance, revocation and the
exclusive observation claim.

All DBs live under pytest's tmp_path; the broker is always FakeBroker (or a
minimal query-only wrapper around it). No network, no Discord, no S3, no
sleep, no real orders anywhere in this suite.
"""

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from broker.broker_models import OrderState, RejectionCategory, safe_account_reference
from broker.fake_broker import FakeBroker, StayPending
from execution import execution_store as store
from execution.reissue_clearance import (
    ClearanceClaimLostError,
    ClearanceClaimUnavailableError,
    ClearanceScopeError,
    ReissueClearanceService,
    derive_account_scope,
    evaluate_clearance_run,
)

from test_execution_store import at, create_order, fill, make_result, submit, submit_and_accept
from test_execution_reissue import record_clearance, reject_parent


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "execution_orders.sqlite3"


@pytest.fixture
def reconciliation_db(tmp_path):
    return tmp_path / "reconciliation.sqlite3"


def make_service(db_path, reconciliation_db, *, broker=None, clock=None, lease=None,
                 observer_id="obs-1"):
    kwargs = {}
    if lease is not None:
        kwargs["lease"] = lease
    return ReissueClearanceService(
        broker=broker if broker is not None else FakeBroker(),
        execution_db_path=db_path,
        reconciliation_db_path=reconciliation_db,
        observer_id=observer_id,
        clock=clock or (lambda: at(100)),
        **kwargs,
    )


def expected_scope(broker):
    identity = broker.connection_identity()
    account = broker.get_account_state()
    return f"{identity.mode}:{identity.target}:{safe_account_reference(account.account_id)}"


class QueryOnlyBroker:
    """A Broker-protocol wrapper WITHOUT the optional listing capabilities:
    reconciliation must mark the enumerations as not-performed, and the
    clearance judgement must fail closed on them."""

    def __init__(self, inner):
        self._inner = inner

    def submit_order(self, request):
        return self._inner.submit_order(request)

    def cancel_order(self, client_order_id):
        return self._inner.cancel_order(client_order_id)

    def get_order(self, client_order_id):
        return self._inner.get_order(client_order_id)

    def get_positions(self):
        return self._inner.get_positions()

    def get_account_state(self):
        return self._inner.get_account_state()

    def connection_identity(self):
        return self._inner.connection_identity()


# --- account scope ------------------------------------------------------------


def test_account_scope_is_stable_and_never_contains_the_real_account_id():
    broker = FakeBroker()
    scope = derive_account_scope(broker)
    assert scope == expected_scope(broker)
    assert broker.get_account_state().account_id not in scope
    assert scope == derive_account_scope(broker)  # stable per connection/account


def test_account_scope_derivation_fails_closed_when_the_account_query_fails():
    class BrokenAccountBroker(QueryOnlyBroker):
        def get_account_state(self):
            raise RuntimeError("account endpoint down")

    with pytest.raises(ClearanceScopeError, match="could not be derived"):
        derive_account_scope(BrokenAccountBroker(FakeBroker()))


# --- expected absence vs hazardous divergence ---------------------------------


def test_zero_fill_rejected_parent_missing_on_broker_is_expected_absence(
    db_path, reconciliation_db
):
    """The main-path requirement: a definitively rejected (zero-fill) parent
    that a LATER observation no longer finds on the broker still earns the
    clearance -- the internal record itself asserts no live order remains."""
    reject_parent(db_path)  # exec-1, signal sig-exec-1, REJECTED, zero fills
    # A FRESH broker (e.g. after the broker purged the rejected order): the
    # parent's client_order_id is unknown -> BROKER_ORDER_MISSING.
    service = make_service(db_path, reconciliation_db, broker=FakeBroker())
    observation = service.observe_signal("sig-exec-1")

    assert observation.granted
    assert observation.evaluation.expected_absences == ("exec-1",)
    assert observation.evaluation.hazards == ()
    current = store.get_reissue_clearance("sig-exec-1", db_path=db_path)
    assert current["status"] == store.CLEARANCE_STATUS_VALID
    assert current["account_scope"] == observation.account_scope
    payload = json.loads(current["evidence_json"])
    assert payload["expected_absences"] == ["exec-1"]
    assert payload["run_id"] == observation.run_id


@pytest.mark.parametrize(
    "prepare, internal_state",
    [
        (lambda db: submit(db, "exec-1"), "pending"),
        (lambda db: submit_and_accept(db, "exec-1"), "accepted"),
        (
            lambda db: (
                submit(db, "exec-1"),
                store.record_order_state(
                    "exec-1", OrderState.UNKNOWN, reason="submit outcome lost",
                    changed_at_utc=at(2), db_path=db,
                ),
            ),
            "unknown",
        ),
        (
            lambda db: (
                submit(db, "exec-1"),
                store.record_order_result(
                    "exec-1",
                    make_result(
                        OrderState.PARTIALLY_FILLED, "exec-1", filled="4",
                        fills=(fill("f-1", "4", 3),),
                    ),
                    reason="partial fill",
                    db_path=db,
                ),
            ),
            "partially_filled",
        ),
    ],
)
def test_same_absence_under_non_asserting_internal_states_is_hazardous(
    db_path, reconciliation_db, prepare, internal_state
):
    """The SAME broker observation (order missing) under an internal state
    that does not assert the absence of exposure must revoke, not clear:
    absence there means "we do not know" (fail-closed)."""
    prepare(db_path)
    service = make_service(db_path, reconciliation_db, broker=FakeBroker())
    observation = service.observe_signal("sig-exec-1")

    assert not observation.granted
    assert observation.evaluation.hazards, internal_state
    current = store.get_reissue_clearance("sig-exec-1", db_path=db_path)
    assert current["status"] == store.CLEARANCE_STATUS_REVOKED


def test_broker_only_order_is_hazardous_for_every_chain(db_path, reconciliation_db):
    """An order the internal records cannot attribute means the broker state
    is not fully explained: unclassifiable -> hazardous (fail-closed)."""
    reject_parent(db_path)
    broker = FakeBroker()
    broker.script_order("foreign-1", StayPending(broker_order_id="B-FOREIGN"))
    from broker.broker_models import OrderRequest, OrderSide, OrderType

    broker.submit_order(
        OrderRequest(
            client_order_id="foreign-1",
            symbol="EURUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
            created_at_utc=at(50),
        )
    )
    observation = make_service(db_path, reconciliation_db, broker=broker).observe_signal(
        "sig-exec-1"
    )
    assert not observation.granted
    assert any("no internal record accounts" in hazard for hazard in observation.evaluation.hazards)
    assert (
        store.get_reissue_clearance("sig-exec-1", db_path=db_path)["status"]
        == store.CLEARANCE_STATUS_REVOKED
    )


def test_missing_listing_capabilities_block_the_grant_without_revoking(
    db_path, reconciliation_db
):
    """Enumerations that did not run are NOT observations: no grant, but no
    revocation either -- the observing claim stays newest (blocked)."""
    reject_parent(db_path)
    observation = make_service(
        db_path, reconciliation_db, broker=QueryOnlyBroker(FakeBroker())
    ).observe_signal("sig-exec-1")

    assert not observation.granted
    assert observation.clearance is None
    assert observation.evaluation.hazards == ()
    assert any("enumeration did not succeed" in f for f in observation.evaluation.failures)
    current = store.get_reissue_clearance("sig-exec-1", db_path=db_path)
    assert current["status"] == store.CLEARANCE_STATUS_OBSERVING  # still blocked


def test_excluded_chain_symbol_blocks_the_grant(db_path, reconciliation_db):
    """A fail-safe excluded symbol means the position domain was NOT
    observed for this chain -> no grant."""
    reject_parent(db_path)  # chain symbol XAUUSD
    # another signal's order on the SAME symbol with an unresolved outcome
    submit(db_path, "exec-9")
    store.record_order_state(
        "exec-9", OrderState.UNKNOWN, reason="submit outcome lost",
        changed_at_utc=at(2), db_path=db_path,
    )
    broker = FakeBroker()
    observation = make_service(db_path, reconciliation_db, broker=broker).observe_signal(
        "sig-exec-1"
    )
    assert not observation.granted
    assert any("fail-safe excluded" in failure for failure in observation.evaluation.failures)


def test_order_only_run_never_satisfies_the_requirements(db_path, reconciliation_db):
    """Pure-function regression: a run without the position domain
    (include_position_scope False) is judged not-observed."""
    reject_parent(db_path)
    from execution.broker_reconciliation import BrokerOrderReconciliationService

    chain = store.list_execution_order_chain("sig-exec-1", db_path=db_path)
    run = BrokerOrderReconciliationService(
        broker=FakeBroker(), execution_db_path=db_path, clock=lambda: at(60)
    ).detect(run_id="order-only", include_position_scope=False)
    evaluation = evaluate_clearance_run(
        run, chain_rows_before=chain, chain_rows_after=chain
    )
    assert not evaluation.satisfied
    assert any("position domain" in failure for failure in evaluation.failures)


def test_chain_change_during_the_observation_blocks_the_grant(db_path, reconciliation_db):
    """Coverage proof: if the chain's broker-stated rows changed while the
    run executed, the run cannot be proven to cover them."""
    reject_parent(db_path)
    from execution.broker_reconciliation import BrokerOrderReconciliationService

    before = store.list_execution_order_chain("sig-exec-1", db_path=db_path)
    run = BrokerOrderReconciliationService(
        broker=FakeBroker(), execution_db_path=db_path,
        reconciliation_db_path=reconciliation_db, clock=lambda: at(60),
    ).run(run_id="covered", include_position_scope=True)
    # simulate a row that changed after the run started: re-observe REJECTED
    store.record_order_result(
        "exec-1",
        make_result(
            OrderState.REJECTED, "exec-1", broker_order_id=None,
            reject_reason="rejected by broker",
            rejection_category=RejectionCategory.TRANSIENT, updated_at=at(90),
        ),
        reason="re-observation",
        db_path=db_path,
    )
    after = store.list_execution_order_chain("sig-exec-1", db_path=db_path)
    evaluation = evaluate_clearance_run(run, chain_rows_before=before, chain_rows_after=after)
    assert not evaluation.satisfied
    assert any("cannot be proven to cover" in failure for failure in evaluation.failures)


# --- gate-correction parents: positive absence evidence -----------------------


def test_unsubmitted_tail_probe_records_positive_absence_evidence(
    db_path, reconciliation_db
):
    create_order(db_path)  # exec-1 stays created / never submitted
    observation = make_service(db_path, reconciliation_db).observe_signal("sig-exec-1")
    assert observation.granted
    payload = json.loads(observation.clearance["evidence_json"])
    assert payload["parent_absence"] == {
        "client_order_id": "client-exec-1",
        "execution_request_id": "exec-1",
        "confirmed_absent": True,
        "probed_at_utc": at(100).isoformat(),
    }


def test_a_broker_held_order_for_an_unsubmitted_parent_is_hazardous(
    db_path, reconciliation_db
):
    """If the broker HOLDS an order under the never-submitted parent's
    client key, the internal record is wrong somewhere: revoke."""
    create_order(db_path)
    broker = FakeBroker()
    broker.script_order("client-exec-1", StayPending(broker_order_id="B-GHOST"))
    from broker.broker_models import OrderRequest, OrderSide, OrderType

    broker.submit_order(
        OrderRequest(
            client_order_id="client-exec-1",
            symbol="XAUUSD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("10"),
            created_at_utc=at(50),
        )
    )
    observation = make_service(db_path, reconciliation_db, broker=broker).observe_signal(
        "sig-exec-1"
    )
    assert not observation.granted
    assert any("broker HOLDS an order" in hazard for hazard in observation.evaluation.hazards)
    assert (
        store.get_reissue_clearance("sig-exec-1", db_path=db_path)["status"]
        == store.CLEARANCE_STATUS_REVOKED
    )


def test_a_failed_absence_probe_blocks_the_grant(db_path, reconciliation_db):
    create_order(db_path)

    class ProbeFailingBroker(QueryOnlyBroker):
        """Listing capabilities intact (delegated via inner FakeBroker
        attributes), but the direct order probe fails."""

        def __init__(self, inner):
            super().__init__(inner)

        def get_order(self, client_order_id):
            from broker.broker_models import BrokerCommunicationError

            raise BrokerCommunicationError("probe endpoint down")

        # keep the optional listing capabilities so ONLY the probe fails
        def list_orders(self):
            return self._inner.list_orders()

        def get_open_trades(self):
            return self._inner.get_open_trades()

        def get_closed_trades(self):
            return self._inner.get_closed_trades()

    observation = make_service(
        db_path, reconciliation_db, broker=ProbeFailingBroker(FakeBroker())
    ).observe_signal("sig-exec-1")
    assert not observation.granted
    assert observation.clearance is None  # no conclusion: blocked, not revoked
    assert any("absence was not confirmed" in f for f in observation.evaluation.failures)


# --- claim / lease / takeover / conclusion ordering ---------------------------


def test_an_unexpired_claim_refuses_a_second_observer(db_path, reconciliation_db):
    reject_parent(db_path)
    service = make_service(db_path, reconciliation_db)
    service._claim("sig-exec-1", "fake:fake:scope")  # hold the claim
    with pytest.raises(ClearanceClaimUnavailableError, match="already being observed"):
        make_service(db_path, reconciliation_db, observer_id="obs-2").observe_signal(
            "sig-exec-1"
        )


def test_an_expired_lease_may_be_taken_over(db_path, reconciliation_db):
    reject_parent(db_path)
    early = make_service(db_path, reconciliation_db, clock=lambda: at(10))
    early._claim("sig-exec-1", "fake:fake:scope")  # lease expires at ~at(310)
    late = make_service(
        db_path, reconciliation_db, observer_id="obs-2", clock=lambda: at(1000)
    )
    observation = late.observe_signal("sig-exec-1")
    assert observation.granted
    trail = store.list_reissue_clearances("sig-exec-1", db_path=db_path)
    assert [row["status"] for row in trail] == [
        store.CLEARANCE_STATUS_OBSERVING,  # the crashed observer's claim
        store.CLEARANCE_STATUS_OBSERVING,  # the takeover claim
        store.CLEARANCE_STATUS_VALID,
    ]
    assert json.loads(trail[1]["evidence_json"])["takeover_of_generation"] == 1


def test_an_unreadable_lease_never_grants_takeover(db_path, reconciliation_db):
    """An observing row whose lease cannot be read counts as UNEXPIRED:
    exclusivity is never assumed from unreadable data (fail-closed)."""
    reject_parent(db_path)
    store.record_reissue_clearance(
        "sig-exec-1",
        evidence_id="ev-opaque",
        status=store.CLEARANCE_STATUS_OBSERVING,
        account_scope="fake:fake:scope",
        observed_at_utc=at(1),
        evidence_json=json.dumps({"phase": "observing"}),  # no lease keys
        recorded_at_utc=at(1),
        db_path=db_path,
    )
    with pytest.raises(ClearanceClaimUnavailableError, match="cannot be read"):
        make_service(db_path, reconciliation_db, clock=lambda: at(10_000)).observe_signal(
            "sig-exec-1"
        )


def test_a_lost_claim_never_commits_the_grant(db_path, reconciliation_db):
    """A newer write between the observation and its conclusion means the
    claim was lost: the grant is NOT committed and the newer (downgrading)
    information stays newest -- an older observation can never override a
    newer revocation."""
    reject_parent(db_path)
    service = make_service(db_path, reconciliation_db)
    original = service._claim

    def claim_then_lose(signal_id, account_scope):
        claim = original(signal_id, account_scope)
        # a concurrent unconditional revocation lands AFTER the claim
        store.record_reissue_clearance(
            signal_id,
            evidence_id="ev-concurrent-revoke",
            status=store.CLEARANCE_STATUS_REVOKED,
            account_scope="fake:fake:scope",
            observed_at_utc=at(50),
            evidence_json=json.dumps({"reason": "operator revocation"}),
            recorded_at_utc=at(50),
            db_path=db_path,
        )
        return claim

    service._claim = claim_then_lose
    with pytest.raises(ClearanceClaimLostError):
        service.observe_signal("sig-exec-1")
    current = store.get_reissue_clearance("sig-exec-1", db_path=db_path)
    assert current["status"] == store.CLEARANCE_STATUS_REVOKED  # downgrade wins


def test_a_failed_grant_write_leaves_the_signal_blocked(
    db_path, reconciliation_db, monkeypatch
):
    """確定失敗時のブロック継続: when committing the VALID conclusion fails,
    the observing claim stays newest and submission remains refused."""
    reject_parent(db_path)
    service = make_service(db_path, reconciliation_db)
    real_record = store.record_reissue_clearance

    def fail_the_grant(signal_id, **kwargs):
        if kwargs.get("status") == store.CLEARANCE_STATUS_VALID:
            raise store.ExecutionStoreError("injected grant write failure")
        return real_record(signal_id, **kwargs)

    monkeypatch.setattr(store, "record_reissue_clearance", fail_the_grant)
    monkeypatch.setattr(
        "execution.reissue_clearance.execution_store.record_reissue_clearance",
        fail_the_grant,
    )
    with pytest.raises(store.ExecutionStoreError, match="injected grant write failure"):
        service.observe_signal("sig-exec-1")
    current = store.get_reissue_clearance("sig-exec-1", db_path=db_path)
    assert current["status"] == store.CLEARANCE_STATUS_OBSERVING


def test_revoke_signal_is_unconditional(db_path, reconciliation_db):
    reject_parent(db_path)
    record_clearance(db_path, "sig-exec-1")  # a valid clearance exists
    service = make_service(db_path, reconciliation_db)
    revoked = service.revoke_signal("sig-exec-1", reason="operator invalidation")
    assert revoked["status"] == store.CLEARANCE_STATUS_REVOKED
    assert (
        store.get_reissue_clearance("sig-exec-1", db_path=db_path)["status"]
        == store.CLEARANCE_STATUS_REVOKED
    )


def test_observing_a_signal_without_a_chain_is_refused(db_path, reconciliation_db):
    store.init_db(db_path)
    with pytest.raises(ValueError, match="no execution order chain"):
        make_service(db_path, reconciliation_db).observe_signal("sig-none")
