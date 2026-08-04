"""Issue #78: ShadowIntendedOrder / ShadowProvenance / ShadowResult model
invariants and the canonical-payload duplicate/conflict contract (PR #100)."""

from datetime import timedelta
from decimal import Decimal

import pytest

from broker.broker_models import OrderSide, OrderType
from execution.safety_gate import SafetyDecision
from execution.shadow_models import (
    INPUT_ORIGIN_CLI_FIXTURE,
    INPUT_ORIGIN_PRODUCTION_SIGNAL,
    ShadowClaim,
    ShadowIntendedOrder,
    ShadowResult,
    canonical_cycle_payload,
    canonical_payload_json,
    differing_payload_fields,
    intended_order_from_request,
)
from shadow_helpers import (
    NOW,
    make_account,
    make_gate,
    make_market,
    make_provenance,
    make_request,
)


def make_decision(request):
    return make_gate().evaluate_shadow(
        request, market=make_market(), account=make_account(), connection_target="shadow"
    )


def make_result(request=None, decision=None, **overrides) -> ShadowResult:
    request = request if request is not None else make_request()
    decision = decision if decision is not None else make_decision(request)
    values = dict(
        request=request,
        client_order_id="client-1",
        timeframe="H1",
        provenance=make_provenance(),
        decision=decision,
        intended_order=intended_order_from_request(request, client_order_id="client-1"),
        market=make_market(),
        account=make_account(),
        policy_snapshot=make_gate().policy.to_snapshot(),
        safety_decision_id=1,
        claim_owner_id="owner-1",
        claimed_at_utc=NOW,
        completed_at_utc=NOW,
    )
    values.update(overrides)
    return ShadowResult(**values)


# --- intended order projection ------------------------------------------------


def test_market_projection_has_no_limit_price():
    order = intended_order_from_request(make_request(), client_order_id="client-1")
    assert order.limit_price is None
    assert order.symbol == "USD_JPY" and order.quantity == Decimal("1")
    assert order.side is OrderSide.BUY and order.order_type is OrderType.MARKET


def test_limit_projection_maps_requested_price_to_limit_price():
    request = make_request(
        order_type=OrderType.LIMIT,
        requested_price=Decimal("150.10"),
        stop_loss_price=Decimal("149.50"),
        take_profit_price=Decimal("151.50"),
    )
    order = intended_order_from_request(request, client_order_id="client-1")
    assert order.limit_price == Decimal("150.10")


def test_intended_order_rejects_limit_price_mismatch():
    with pytest.raises(ValueError, match="limit_price"):
        ShadowIntendedOrder(
            client_order_id="client-1", symbol="USD_JPY", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=Decimal("1"), limit_price=Decimal("150"),
        )
    with pytest.raises(ValueError, match="limit_price"):
        ShadowIntendedOrder(
            client_order_id="client-1", symbol="USD_JPY", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=Decimal("1"), limit_price=None,
        )


# --- provenance ---------------------------------------------------------------


def test_provenance_rejects_unknown_origin_and_empty_sources():
    with pytest.raises(ValueError, match="input_origin"):
        make_provenance(input_origin="somewhere")
    with pytest.raises(ValueError, match="signal_source"):
        make_provenance(signal_source=" ")


def test_production_provenance_requires_the_signal_timestamp():
    with pytest.raises(ValueError, match="timestamp"):
        make_provenance(input_origin=INPUT_ORIGIN_PRODUCTION_SIGNAL)
    make_provenance(
        input_origin=INPUT_ORIGIN_PRODUCTION_SIGNAL, signal_observed_at_utc=NOW
    )  # valid


# --- canonical payload: duplicate vs conflict contract ------------------------


def payload(**overrides):
    request = make_request(**overrides.pop("request_overrides", {}))
    values = dict(client_order_id="client-1", timeframe="H1",
                  input_origin=INPUT_ORIGIN_CLI_FIXTURE)
    values.update(overrides)
    return canonical_cycle_payload(request, **values)


def test_numerically_equal_decimals_canonicalize_identically():
    base = payload(request_overrides={"quantity": Decimal("1")})
    same = payload(request_overrides={"quantity": Decimal("1.0")})
    exp = payload(request_overrides={"quantity": Decimal("1E+0")})
    assert base == same == exp
    assert canonical_payload_json(base) == canonical_payload_json(same)


def test_payload_json_is_key_order_independent():
    base = payload()
    reordered = dict(reversed(list(base.items())))
    assert canonical_payload_json(base) == canonical_payload_json(reordered)


def test_differing_fields_are_reported():
    base = payload()
    changed = payload(request_overrides={"quantity": Decimal("2")})
    assert differing_payload_fields(base, changed) == ("quantity",)
    changed_origin = payload(input_origin="scenario_fixture")
    assert differing_payload_fields(base, changed_origin) == ("input_origin",)


def test_payload_excludes_evaluation_time_values():
    later = payload(request_overrides={"requested_at_utc": NOW + timedelta(seconds=5)})
    assert later == payload()  # timestamps are not identity


# --- ShadowClaim --------------------------------------------------------------


def test_claim_validates_lease_ordering():
    with pytest.raises(ValueError, match="lease_expires_at_utc"):
        ShadowClaim(
            shadow_result_id=1, execution_request_id="exec-1", signal_id="signal-1",
            client_order_id="client-1", claim_owner_id="owner-1",
            claim_attempt_id="owner-1-attempt-1", claim_generation=1,
            claimed_at_utc=NOW, lease_expires_at_utc=NOW, input_origin="cli_fixture",
        )


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"claim_attempt_id": ""}, "claim_attempt_id"),
        ({"claim_generation": 0}, "claim_generation"),
    ],
)
def test_claim_validates_attempt_identity(overrides, match):
    values = dict(
        shadow_result_id=1, execution_request_id="exec-1", signal_id="signal-1",
        client_order_id="client-1", claim_owner_id="owner-1",
        claim_attempt_id="owner-1-attempt-1", claim_generation=1,
        claimed_at_utc=NOW, lease_expires_at_utc=NOW + timedelta(seconds=60),
        input_origin="cli_fixture",
    )
    values.update(overrides)
    with pytest.raises(ValueError, match=match):
        ShadowClaim(**values)


# --- ShadowResult invariants --------------------------------------------------


def test_valid_result_roundtrips_properties():
    result = make_result()
    assert result.allowed is True
    assert result.rejection_codes == ()
    assert result.shadow_result_id is None
    assert result.canonical_payload["quantity"] == "1"
    assert result.provenance.input_origin == "scenario_fixture"


def test_result_rejects_decision_for_another_request():
    other = make_request(execution_request_id="exec-2", signal_id="signal-2")
    with pytest.raises(ValueError, match="belongs to"):
        make_result(decision=make_decision(other))


def test_result_rejects_non_shadow_decision():
    request = make_request()
    live_like = SafetyDecision(
        execution_request_id=request.execution_request_id,
        allowed=True,
        checked_at_utc=NOW,
        policy_id="shadow-test-policy",
        execution_mode="fake",
        connection_target="fake",
    )
    with pytest.raises(ValueError, match="shadow"):
        make_result(request=request, decision=live_like)


def test_result_rejects_intended_order_drift():
    drifted = ShadowIntendedOrder(
        client_order_id="client-1", symbol="USD_JPY", side=OrderSide.BUY,
        order_type=OrderType.MARKET, quantity=Decimal("2"), limit_price=None,
    )
    with pytest.raises(ValueError, match="does not match the audit"):
        make_result(intended_order=drifted)


def test_result_requires_account_snapshot_and_provenance():
    with pytest.raises(ValueError, match="account"):
        make_result(account="nope")
    with pytest.raises(ValueError, match="provenance"):
        make_result(provenance="nope")


def test_result_rejects_policy_snapshot_mismatch():
    with pytest.raises(ValueError, match="policy_id"):
        make_result(policy_snapshot={"policy_id": "someone-else"})


def test_policy_snapshot_is_detached_from_the_caller_dict():
    snapshot = make_gate().policy.to_snapshot()
    result = make_result(policy_snapshot=snapshot)
    snapshot["policy_id"] = "mutated"
    assert result.policy_snapshot["policy_id"] == "shadow-test-policy"


def test_result_rejects_invalid_ids_claim_fields_and_timeframe():
    with pytest.raises(ValueError, match="safety_decision_id"):
        make_result(safety_decision_id=0)
    with pytest.raises(ValueError, match="claim_owner_id"):
        make_result(claim_owner_id=" ")
    with pytest.raises(ValueError, match="timeframe"):
        make_result(timeframe=" ")
    with pytest.raises(ValueError, match="completed_at_utc"):
        make_result(completed_at_utc=NOW.replace(tzinfo=None))
