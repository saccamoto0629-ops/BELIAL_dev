"""Issue #73: broker-agnostic common models — enums, invariants, transitions, errors."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from broker import broker_models as m

UTC_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
UTC_LATER = datetime(2026, 7, 1, 12, 5, 0, tzinfo=timezone.utc)


def make_request(**overrides):
    kwargs = dict(
        client_order_id="c-1",
        symbol="XAUUSD",
        side=m.OrderSide.BUY,
        order_type=m.OrderType.LIMIT,
        quantity=Decimal("10"),
        limit_price=Decimal("2400.00"),
        created_at_utc=UTC_NOW,
    )
    kwargs.update(overrides)
    return m.OrderRequest(**kwargs)


def make_result(**overrides):
    kwargs = dict(
        client_order_id="c-1",
        broker_order_id="B-1",
        state=m.OrderState.FILLED,
        symbol="XAUUSD",
        side=m.OrderSide.BUY,
        order_type=m.OrderType.LIMIT,
        requested_quantity=Decimal("10"),
        filled_quantity=Decimal("10"),
        remaining_quantity=Decimal("0"),
        requested_price=Decimal("2400.00"),
        average_fill_price=Decimal("2400.50"),
        updated_at_utc=UTC_NOW,
        fills=(m.Fill("f-1", Decimal("2400.50"), Decimal("10"), UTC_NOW),),
    )
    kwargs.update(overrides)
    return m.OrderResult(**kwargs)


# --- order states -----------------------------------------------------------

def test_order_state_vocabulary_covers_issue_73_minimum():
    values = {s.value for s in m.OrderState}
    assert {"pending", "accepted", "partially_filled", "filled",
            "cancelled", "rejected", "unknown"} == values


def test_timeout_outcome_is_a_distinct_state_from_rejected():
    assert m.OrderState.UNKNOWN is not m.OrderState.REJECTED
    # UNKNOWN is NOT terminal: the broker may still hold the order.
    assert m.OrderState.UNKNOWN not in m.TERMINAL_ORDER_STATES
    assert m.OrderState.REJECTED in m.TERMINAL_ORDER_STATES


def test_terminal_states_allow_no_state_change():
    # The map records state CHANGES only; same-state re-observation is
    # handled by the validator (see below).
    for state in m.TERMINAL_ORDER_STATES:
        assert m.ORDER_STATE_TRANSITIONS[state] == frozenset()


@pytest.mark.parametrize("state", list(m.OrderState))
def test_same_state_update_is_always_allowed(state):
    # PR #87 contract: broker polling re-observes the same snapshot, so
    # old == new passes for EVERY state, terminal ones included.
    m.validate_order_state_transition(state, state)


@pytest.mark.parametrize(
    "terminal", sorted(m.TERMINAL_ORDER_STATES, key=lambda s: s.value)
)
def test_terminal_states_still_reject_any_different_state(terminal):
    for new in m.OrderState:
        if new is terminal:
            continue
        with pytest.raises(ValueError):
            m.validate_order_state_transition(terminal, new)


def test_transition_validator_rejects_non_enum_inputs():
    with pytest.raises(m.BrokerInvalidRequestError):
        m.validate_order_state_transition("filled", m.OrderState.FILLED)
    with pytest.raises(m.BrokerInvalidRequestError):
        m.validate_order_state_transition(m.OrderState.FILLED, "filled")


def test_unknown_can_be_resolved_to_any_concrete_state():
    assert m.ORDER_STATE_TRANSITIONS[m.OrderState.UNKNOWN] == {
        m.OrderState.ACCEPTED, m.OrderState.PARTIALLY_FILLED, m.OrderState.FILLED,
        m.OrderState.CANCELLED, m.OrderState.REJECTED,
    }


@pytest.mark.parametrize(
    "old,new",
    [
        (m.OrderState.PENDING, m.OrderState.ACCEPTED),
        (m.OrderState.ACCEPTED, m.OrderState.PARTIALLY_FILLED),
        (m.OrderState.PARTIALLY_FILLED, m.OrderState.PARTIALLY_FILLED),
        (m.OrderState.PARTIALLY_FILLED, m.OrderState.FILLED),
        (m.OrderState.ACCEPTED, m.OrderState.CANCELLED),
        (m.OrderState.PENDING, m.OrderState.UNKNOWN),
        (m.OrderState.UNKNOWN, m.OrderState.FILLED),
    ],
)
def test_valid_transitions_pass(old, new):
    m.validate_order_state_transition(old, new)


@pytest.mark.parametrize(
    "old,new",
    [
        (m.OrderState.FILLED, m.OrderState.ACCEPTED),
        (m.OrderState.FILLED, m.OrderState.CANCELLED),
        (m.OrderState.CANCELLED, m.OrderState.FILLED),
        (m.OrderState.REJECTED, m.OrderState.ACCEPTED),
        (m.OrderState.PARTIALLY_FILLED, m.OrderState.ACCEPTED),
        (m.OrderState.PARTIALLY_FILLED, m.OrderState.REJECTED),
        (m.OrderState.ACCEPTED, m.OrderState.PENDING),
    ],
)
def test_invalid_transitions_raise(old, new):
    with pytest.raises(ValueError):
        m.validate_order_state_transition(old, new)


# --- error taxonomy ---------------------------------------------------------

def test_error_codes_are_distinct_and_machine_checkable():
    codes = {
        m.BrokerTimeoutError("x").code,
        m.BrokerCommunicationError("x").code,
        m.BrokerCancellationError("x").code,
        m.BrokerInvalidRequestError("x").code,
        m.BrokerOrderNotFoundError("x").code,
    }
    assert len(codes) == 5
    assert all(isinstance(c, m.BrokerErrorCode) for c in codes)


def test_all_broker_errors_share_the_base_class_and_carry_client_order_id():
    err = m.BrokerTimeoutError("boom", client_order_id="c-9")
    assert isinstance(err, m.BrokerError)
    assert err.client_order_id == "c-9"
    assert m.BrokerCommunicationError("x").client_order_id is None


def test_timeout_and_communication_failure_are_different_codes():
    assert m.BrokerErrorCode.TIMEOUT is not m.BrokerErrorCode.COMMUNICATION_FAILURE


def test_account_state_keeps_the_real_id_and_conceals_it_in_repr():
    # Issue #79 / PR #101 re-review: account_id is an IDENTIFIER (Safety
    # Decision / audit / shadow report rely on it) — the field keeps the
    # real value; only repr and display_account_id conceal it.
    state = m.AccountState(
        account_id="101-004-1234567-001",
        currency="JPY",
        balance=Decimal("1"),
        equity=Decimal("1"),
        margin_available=Decimal("1"),
        retrieved_at_utc=UTC_NOW,
    )
    assert state.account_id == "101-004-1234567-001"
    assert "101-004-1234567-001" not in repr(state)
    display = state.display_account_id
    assert "101-004-1234567-001" not in display
    # non-reversible digest, stable per id, distinct across accounts
    other = m.AccountState(
        account_id="101-004-7654321-001",
        currency="JPY",
        balance=Decimal("1"),
        equity=Decimal("1"),
        margin_available=Decimal("1"),
        retrieved_at_utc=UTC_NOW,
    )
    assert state.display_account_id == display
    assert other.display_account_id != display


def test_authentication_and_rate_limit_errors_join_the_taxonomy():
    # Issue #79: real-endpoint failure categories, still broker-agnostic.
    codes = {
        m.BrokerTimeoutError("x").code,
        m.BrokerCommunicationError("x").code,
        m.BrokerCancellationError("x").code,
        m.BrokerInvalidRequestError("x").code,
        m.BrokerOrderNotFoundError("x").code,
        m.BrokerAuthenticationError("x").code,
        m.BrokerRateLimitError("x").code,
    }
    assert len(codes) == 7
    assert m.BrokerAuthenticationError("x").code is m.BrokerErrorCode.AUTHENTICATION_FAILURE
    assert m.BrokerRateLimitError("x").code is m.BrokerErrorCode.RATE_LIMITED
    assert isinstance(m.BrokerAuthenticationError("x", client_order_id="c-1"), m.BrokerError)
    assert m.BrokerRateLimitError("x").client_order_id is None


# --- OrderRequest validation ------------------------------------------------

def test_order_request_accepts_a_valid_limit_order():
    req = make_request()
    assert req.quantity == Decimal("10")
    assert req.limit_price == Decimal("2400.00")


def test_order_request_accepts_a_market_order_without_price():
    req = make_request(order_type=m.OrderType.MARKET, limit_price=None)
    assert req.limit_price is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"client_order_id": ""},
        {"symbol": "  "},
        {"quantity": Decimal("0")},
        {"quantity": Decimal("-1")},
        {"quantity": 10.0},  # float is rejected: Decimal only
        {"quantity": Decimal("NaN")},
        {"limit_price": None},  # LIMIT without a price
        {"limit_price": Decimal("0")},
        {"order_type": m.OrderType.MARKET},  # MARKET with a limit_price
        {"side": "buy"},  # raw string instead of enum
        {"created_at_utc": datetime(2026, 7, 1, 12, 0, 0)},  # naive datetime
        {"created_at_utc": UTC_NOW.astimezone(timezone(timedelta(hours=9)))},  # non-UTC
    ],
)
def test_order_request_rejects_invalid_input(overrides):
    with pytest.raises(m.BrokerInvalidRequestError):
        make_request(**overrides)


# --- OrderResult invariants -------------------------------------------------

def test_order_result_keeps_requested_and_fill_prices_separately():
    result = make_result()
    assert result.requested_price == Decimal("2400.00")
    assert result.average_fill_price == Decimal("2400.50")
    assert result.price_deviation == Decimal("0.50")


def test_price_deviation_is_none_without_a_requested_price():
    result = make_result(
        order_type=m.OrderType.MARKET, requested_price=None,
    )
    assert result.price_deviation is None


def test_partial_fill_tracks_requested_filled_and_remaining():
    fills = (
        m.Fill("f-1", Decimal("2400"), Decimal("3"), UTC_NOW),
        m.Fill("f-2", Decimal("2401"), Decimal("1"), UTC_NOW),
    )
    result = make_result(
        state=m.OrderState.PARTIALLY_FILLED,
        filled_quantity=Decimal("4"),
        remaining_quantity=Decimal("6"),
        average_fill_price=m.weighted_average_price(fills),
        fills=fills,
    )
    assert result.requested_quantity == Decimal("10")
    assert result.filled_quantity == Decimal("4")
    assert result.remaining_quantity == Decimal("6")
    assert result.average_fill_price == Decimal("2400.25")


def test_weighted_average_price_of_no_fills_is_none():
    assert m.weighted_average_price(()) is None


@pytest.mark.parametrize(
    "overrides",
    [
        # remaining must equal requested - filled
        {"remaining_quantity": Decimal("1")},
        # fills must sum to filled_quantity
        {"fills": (m.Fill("f-1", Decimal("2400.5"), Decimal("9"), UTC_NOW),)},
        # FILLED requires filled == requested
        {"filled_quantity": Decimal("9"), "remaining_quantity": Decimal("1"),
         "fills": (m.Fill("f-1", Decimal("2400.5"), Decimal("9"), UTC_NOW),)},
        # ACCEPTED requires filled == 0
        {"state": m.OrderState.ACCEPTED},
        # REJECTED requires a reject_reason and zero fills
        {"state": m.OrderState.REJECTED},
        # reject_reason forbidden outside REJECTED
        {"reject_reason": "nope"},
        # average price without fills
        {"filled_quantity": Decimal("0"), "remaining_quantity": Decimal("10"),
         "fills": (), "state": m.OrderState.ACCEPTED},
        # filled beyond requested
        {"filled_quantity": Decimal("11"), "remaining_quantity": Decimal("-1"),
         "fills": (m.Fill("f-1", Decimal("2400.5"), Decimal("11"), UTC_NOW),)},
        # ACCEPTED requires a broker_order_id
        {"state": m.OrderState.ACCEPTED, "broker_order_id": None,
         "filled_quantity": Decimal("0"), "remaining_quantity": Decimal("10"),
         "fills": (), "average_fill_price": None},
    ],
)
def test_order_result_rejects_inconsistent_snapshots(overrides):
    with pytest.raises(ValueError):
        make_result(**overrides)


def test_order_result_rejects_wrong_field_types_as_invalid_request():
    # PR #87: type-malformed fields from a caller/adapter raise the typed
    # BrokerInvalidRequestError (cross-field inconsistencies stay ValueError).
    cases = [
        {"state": "filled"},
        {"side": "buy"},
        {"order_type": "market"},
        {"fills": []},                      # list instead of tuple
        {"fills": (object(),)},             # non-Fill entry
        {"reject_reason": ""},
        {"reject_reason": "   "},
    ]
    for overrides in cases:
        with pytest.raises(m.BrokerInvalidRequestError):
            make_result(**overrides)


# --- UNKNOWN contract (PR #87) ----------------------------------------------
# "unknown" qualifies the broker CONFIRMATION, not the fill amount: an
# UNKNOWN snapshot may hold no fills, partial fills, or even the full
# requested quantity (observed before confirmation was lost), with or
# without a broker_order_id. It only refuses to assert a final outcome.

def test_unknown_state_result_is_representable_with_zero_fills():
    # After a timeout the outcome is unknown: no fills, no broker_order_id,
    # but the order is NOT rejected and NOT confirmed absent.
    result = make_result(
        state=m.OrderState.UNKNOWN,
        broker_order_id=None,
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal("10"),
        average_fill_price=None,
        fills=(),
    )
    assert result.state is m.OrderState.UNKNOWN
    assert result.state is not m.OrderState.REJECTED


def test_unknown_state_may_carry_known_partial_fills():
    fills = (m.Fill("f-1", Decimal("2400"), Decimal("4"), UTC_NOW),)
    result = make_result(
        state=m.OrderState.UNKNOWN,
        filled_quantity=Decimal("4"),
        remaining_quantity=Decimal("6"),
        average_fill_price=Decimal("2400"),
        fills=fills,
    )
    assert result.filled_quantity == Decimal("4")


def test_unknown_state_may_carry_a_full_quantity_fill():
    # Fully-filled-as-far-as-we-saw, but broker confirmation was lost:
    # still UNKNOWN, not FILLED, until a later query resolves it.
    result = make_result(state=m.OrderState.UNKNOWN)
    assert result.filled_quantity == result.requested_quantity
    assert result.state is m.OrderState.UNKNOWN


# --- validate_order_result_update (PR #87 Major 2) ---------------------------
# Snapshot-to-snapshot update invariants: identity/request fields frozen,
# monotonic fill progress, old fills preserved as a prefix, broker_order_id
# only None -> value. Applies to terminal same-state re-observations and to
# every resolution of UNKNOWN.

PARTIAL_FILLS = (m.Fill("f-1", Decimal("2400"), Decimal("4"), UTC_NOW),)


def unknown_zero_fill(**overrides):
    kwargs = dict(
        state=m.OrderState.UNKNOWN, broker_order_id=None,
        filled_quantity=Decimal("0"), remaining_quantity=Decimal("10"),
        average_fill_price=None, fills=(),
    )
    kwargs.update(overrides)
    return make_result(**kwargs)


def unknown_partial_fill(**overrides):
    kwargs = dict(
        state=m.OrderState.UNKNOWN, broker_order_id=None,
        filled_quantity=Decimal("4"), remaining_quantity=Decimal("6"),
        average_fill_price=Decimal("2400"), fills=PARTIAL_FILLS,
    )
    kwargs.update(overrides)
    return make_result(**kwargs)


def unknown_full_fill(**overrides):
    return make_result(state=m.OrderState.UNKNOWN, **overrides)


def accepted_zero_fill(**overrides):
    kwargs = dict(
        state=m.OrderState.ACCEPTED, broker_order_id="B-1",
        filled_quantity=Decimal("0"), remaining_quantity=Decimal("10"),
        average_fill_price=None, fills=(),
    )
    kwargs.update(overrides)
    return make_result(**kwargs)


def rejected_zero_fill(**overrides):
    kwargs = dict(
        state=m.OrderState.REJECTED, broker_order_id=None,
        filled_quantity=Decimal("0"), remaining_quantity=Decimal("10"),
        average_fill_price=None, fills=(), reject_reason="rejected",
    )
    kwargs.update(overrides)
    return make_result(**kwargs)


def test_update_validator_rejects_non_order_result_inputs():
    with pytest.raises(m.BrokerInvalidRequestError):
        m.validate_order_result_update(object(), make_result())
    with pytest.raises(m.BrokerInvalidRequestError):
        m.validate_order_result_update(make_result(), "snapshot")


def test_unknown_with_zero_fills_can_resolve_to_accepted_or_rejected():
    m.validate_order_result_update(unknown_zero_fill(), accepted_zero_fill())
    m.validate_order_result_update(unknown_zero_fill(), rejected_zero_fill())


def test_unknown_with_partial_fills_can_resolve_to_fill_progress_states():
    old = unknown_partial_fill()
    m.validate_order_result_update(
        old, unknown_partial_fill(state=m.OrderState.PARTIALLY_FILLED, broker_order_id="B-1"),
    )
    full_fills = PARTIAL_FILLS + (m.Fill("f-2", Decimal("2401"), Decimal("6"), UTC_NOW),)
    m.validate_order_result_update(
        old,
        make_result(
            state=m.OrderState.FILLED,
            average_fill_price=m.weighted_average_price(full_fills),
            fills=full_fills,
        ),
    )
    m.validate_order_result_update(
        old, unknown_partial_fill(state=m.OrderState.CANCELLED),
    )


def test_unknown_with_partial_fills_cannot_resolve_to_accepted_or_rejected():
    # ACCEPTED / REJECTED require zero fills by construction, so any legal
    # candidate snapshot violates filled-quantity monotonicity here.
    old = unknown_partial_fill()
    with pytest.raises(ValueError):
        m.validate_order_result_update(old, accepted_zero_fill())
    with pytest.raises(ValueError):
        m.validate_order_result_update(old, rejected_zero_fill())


def test_unknown_with_a_full_fill_resolves_only_to_filled():
    old = unknown_full_fill()
    m.validate_order_result_update(old, make_result())  # FILLED: allowed
    for candidate in (
        accepted_zero_fill(),
        rejected_zero_fill(),
        unknown_partial_fill(state=m.OrderState.PARTIALLY_FILLED, broker_order_id="B-1"),
        unknown_partial_fill(state=m.OrderState.CANCELLED),
    ):
        with pytest.raises(ValueError):
            m.validate_order_result_update(old, candidate)


def test_snapshot_construction_rejects_duplicate_fill_ids():
    # Two fills sharing one fill_id sum to the right quantity but would
    # double-count the same execution — rejected at construction.
    duplicated = (
        m.Fill("same-fill", Decimal("2400"), Decimal("5"), UTC_NOW),
        m.Fill("same-fill", Decimal("2400"), Decimal("5"), UTC_NOW),
    )
    with pytest.raises(ValueError):
        make_result(
            average_fill_price=Decimal("2400"), fills=duplicated,
        )


def test_unknown_resolution_cannot_smuggle_in_duplicate_fill_ids():
    # The reviewer's repro: UNKNOWN with no fills resolving to FILLED whose
    # two fills share a fill_id. The FILLED snapshot itself is now
    # unconstructible, so the double-counted update can never be validated.
    old = unknown_zero_fill()
    with pytest.raises(ValueError):
        make_result(
            state=m.OrderState.FILLED,
            average_fill_price=Decimal("2400"),
            fills=(
                m.Fill("same-fill", Decimal("2400"), Decimal("5"), UTC_NOW),
                m.Fill("same-fill", Decimal("2400"), Decimal("5"), UTC_NOW),
            ),
        )
    # a well-formed resolution with distinct IDs still passes
    distinct = (
        m.Fill("f-a", Decimal("2400"), Decimal("5"), UTC_NOW),
        m.Fill("f-b", Decimal("2400"), Decimal("5"), UTC_NOW),
    )
    m.validate_order_result_update(
        old,
        make_result(average_fill_price=Decimal("2400"), fills=distinct),
    )


def test_update_rejects_fill_regression_removal_and_alteration():
    old = unknown_partial_fill()
    # fill 数量後退 (same fill_id, smaller quantity)
    with pytest.raises(ValueError):
        m.validate_order_result_update(
            old,
            unknown_partial_fill(
                filled_quantity=Decimal("2"), remaining_quantity=Decimal("8"),
                fills=(m.Fill("f-1", Decimal("2400"), Decimal("2"), UTC_NOW),),
            ),
        )
    # fill 削除
    with pytest.raises(ValueError):
        m.validate_order_result_update(old, unknown_zero_fill())
    # fill 改変 (same fill_id, different price, same total quantity)
    with pytest.raises(ValueError):
        m.validate_order_result_update(
            old,
            unknown_partial_fill(
                average_fill_price=Decimal("2500"),
                fills=(m.Fill("f-1", Decimal("2500"), Decimal("4"), UTC_NOW),),
            ),
        )
    # appended fill must not reuse an existing fill_id
    reused = PARTIAL_FILLS + (m.Fill("f-1", Decimal("2401"), Decimal("2"), UTC_NOW),)
    with pytest.raises(ValueError):
        m.validate_order_result_update(
            old,
            unknown_partial_fill(
                filled_quantity=Decimal("6"), remaining_quantity=Decimal("4"),
                average_fill_price=m.weighted_average_price(reused), fills=reused,
            ),
        )


def test_update_rejects_identity_and_request_field_changes():
    old = unknown_partial_fill()
    for overrides in (
        {"client_order_id": "c-2"},
        {"symbol": "EURUSD"},
        {"side": m.OrderSide.SELL},
        {"order_type": m.OrderType.MARKET, "requested_price": None},
        {"requested_price": Decimal("2500")},
    ):
        with pytest.raises(ValueError):
            m.validate_order_result_update(old, unknown_partial_fill(**overrides))
    with pytest.raises(ValueError):  # requested_quantity change
        m.validate_order_result_update(
            old,
            unknown_partial_fill(
                requested_quantity=Decimal("20"), remaining_quantity=Decimal("16"),
            ),
        )


def test_broker_order_id_may_only_go_from_none_to_a_value():
    m.validate_order_result_update(
        unknown_zero_fill(), accepted_zero_fill(),  # None -> "B-1"
    )
    accepted = accepted_zero_fill()
    with pytest.raises(ValueError):  # value -> None
        m.validate_order_result_update(
            accepted, unknown_zero_fill(),
        )
    with pytest.raises(ValueError):  # value A -> value B
        m.validate_order_result_update(
            accepted, accepted_zero_fill(broker_order_id="B-2"),
        )


def test_result_construction_rejects_order_type_price_mismatch():
    # LIMIT results must carry the requested price; MARKET results must not.
    with pytest.raises(ValueError):
        make_result(requested_price=None)  # LIMIT without requested_price
    with pytest.raises(ValueError):
        make_result(
            order_type=m.OrderType.MARKET, requested_price=Decimal("10"),
        )


def test_result_construction_rejects_pending_with_a_broker_order_id():
    def pending(broker_order_id):
        return make_result(
            state=m.OrderState.PENDING, broker_order_id=broker_order_id,
            filled_quantity=Decimal("0"), remaining_quantity=Decimal("10"),
            average_fill_price=None, fills=(),
        )

    assert pending(None).broker_order_id is None
    with pytest.raises(ValueError):
        pending("B-1")  # PENDING means no broker acknowledgement yet


def test_updated_at_must_not_go_backwards_and_may_advance():
    old = accepted_zero_fill(updated_at_utc=UTC_NOW)
    m.validate_order_result_update(
        old, accepted_zero_fill(updated_at_utc=UTC_NOW + timedelta(seconds=1))
    )
    m.validate_order_result_update(old, accepted_zero_fill(updated_at_utc=UTC_NOW))
    with pytest.raises(ValueError):
        m.validate_order_result_update(
            old, accepted_zero_fill(updated_at_utc=UTC_NOW - timedelta(seconds=1))
        )


def test_terminal_reobservation_rejects_a_lone_average_price_change():
    # Same fills, same everything — ONLY average_fill_price rewritten:
    # a settled fill price must never change after the fact.
    filled = make_result()
    with pytest.raises(ValueError):
        m.validate_order_result_update(
            filled, make_result(average_fill_price=Decimal("2400.75"))
        )


def test_terminal_reobservation_rejects_a_lone_reject_reason_change():
    old = rejected_zero_fill()
    with pytest.raises(ValueError):
        m.validate_order_result_update(
            old, rejected_zero_fill(reject_reason="different story")
        )


def test_terminal_reobservation_allows_only_a_fresh_timestamp():
    filled = make_result()
    m.validate_order_result_update(
        filled, make_result(updated_at_utc=UTC_NOW + timedelta(seconds=5))
    )


def test_terminal_same_state_reobservation_must_not_change_content():
    filled = make_result()
    # identical re-observation (fresh timestamp) is fine
    m.validate_order_result_update(filled, make_result(updated_at_utc=UTC_NOW))
    # content alteration under the same terminal state is rejected
    altered_fills = (m.Fill("f-1", Decimal("2500"), Decimal("10"), UTC_NOW),)
    with pytest.raises(ValueError):
        m.validate_order_result_update(
            filled,
            make_result(average_fill_price=Decimal("2500"), fills=altered_fills),
        )
    with pytest.raises(ValueError):
        m.validate_order_result_update(filled, make_result(symbol="EURUSD"))
    # and a terminal state still never moves to a different state
    with pytest.raises(ValueError):
        m.validate_order_result_update(
            filled, unknown_partial_fill(state=m.OrderState.CANCELLED)
        )


def test_unknown_state_broker_order_id_is_optional_either_way():
    with_id = make_result(state=m.OrderState.UNKNOWN)
    without_id = make_result(
        state=m.OrderState.UNKNOWN,
        broker_order_id=None,
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal("10"),
        average_fill_price=None,
        fills=(),
    )
    assert with_id.broker_order_id == "B-1"
    assert without_id.broker_order_id is None


# --- position / account -----------------------------------------------------

def test_position_info_validates_and_holds_decimals():
    pos = m.PositionInfo(
        symbol="XAUUSD", side=m.PositionSide.LONG, quantity=Decimal("2"),
        average_entry_price=Decimal("2400"), retrieved_at_utc=UTC_NOW,
    )
    assert pos.quantity == Decimal("2")
    with pytest.raises(m.BrokerInvalidRequestError):
        m.PositionInfo(
            symbol="XAUUSD", side=m.PositionSide.LONG, quantity=Decimal("0"),
            average_entry_price=Decimal("2400"), retrieved_at_utc=UTC_NOW,
        )


def test_account_state_allows_negative_balance_but_requires_utc():
    state = m.AccountState(
        account_id="a-1", currency="USD", balance=Decimal("-10"),
        equity=Decimal("0"), margin_available=Decimal("0"), retrieved_at_utc=UTC_NOW,
    )
    assert state.balance == Decimal("-10")
    with pytest.raises(m.BrokerInvalidRequestError):
        m.AccountState(
            account_id="a-1", currency="USD", balance=Decimal("0"),
            equity=Decimal("0"), margin_available=Decimal("0"),
            retrieved_at_utc=datetime(2026, 7, 1),
        )


def test_models_are_immutable():
    req = make_request()
    with pytest.raises(Exception):
        req.quantity = Decimal("99")


# --- BrokerConnectionIdentity (Issue #75) ------------------------------------

def test_connection_identity_requires_non_empty_stripped_strings():
    assert m.BrokerConnectionIdentity(mode="fake", target="fake").mode == "fake"
    for bad in ("", "  ", None, 7):
        with pytest.raises(m.BrokerInvalidRequestError):
            m.BrokerConnectionIdentity(mode=bad, target="fake")
        with pytest.raises(m.BrokerInvalidRequestError):
            m.BrokerConnectionIdentity(mode="fake", target=bad)
    with pytest.raises(m.BrokerInvalidRequestError):
        m.BrokerConnectionIdentity(mode=" fake", target="fake")
    with pytest.raises(m.BrokerInvalidRequestError):
        m.BrokerConnectionIdentity(mode="fake", target="fake ")


# --- ProtectionOrderInfo / TradeInfo SL-TP / ClosedTradeInfo (Issue #105) -----

def make_trade(**overrides):
    kwargs = dict(
        trade_id="T-1",
        symbol="USD_JPY",
        side=m.PositionSide.LONG,
        quantity=Decimal("10"),
        open_price=Decimal("150.5"),
        opened_at_utc=UTC_NOW,
        retrieved_at_utc=UTC_NOW,
    )
    kwargs.update(overrides)
    return m.TradeInfo(**kwargs)


def make_closed_trade(**overrides):
    kwargs = dict(
        trade_id="T-1",
        symbol="USD_JPY",
        side=m.PositionSide.LONG,
        quantity=Decimal("10"),
        open_price=Decimal("150.5"),
        opened_at_utc=UTC_NOW,
        closed_at_utc=UTC_NOW + timedelta(minutes=1),
        retrieved_at_utc=UTC_NOW + timedelta(minutes=2),
    )
    kwargs.update(overrides)
    return m.ClosedTradeInfo(**kwargs)


def test_protection_order_info_validates_id_price_and_optional_trade_reference():
    info = m.ProtectionOrderInfo(order_id="SL-1", price=Decimal("149.5"), trade_id="T-1")
    assert info.trade_id == "T-1"
    assert m.ProtectionOrderInfo(order_id="SL-1", price=Decimal("1")).trade_id is None
    for bad in ("", "  ", None, 7):
        with pytest.raises(m.BrokerInvalidRequestError):
            m.ProtectionOrderInfo(order_id=bad, price=Decimal("1"))
    for bad_price in (Decimal("0"), Decimal("-1"), Decimal("NaN"), 1.5, "1"):
        with pytest.raises(m.BrokerInvalidRequestError):
            m.ProtectionOrderInfo(order_id="SL-1", price=bad_price)
    with pytest.raises(m.BrokerInvalidRequestError):
        m.ProtectionOrderInfo(order_id="SL-1", price=Decimal("1"), trade_id="  ")


def test_trade_info_optional_issue_105_fields_default_to_absent():
    trade = make_trade()
    assert trade.client_order_id is None
    assert trade.stop_loss is None
    assert trade.take_profit is None


def test_trade_info_validates_the_issue_105_fields():
    protection = m.ProtectionOrderInfo(order_id="SL-1", price=Decimal("149.5"))
    trade = make_trade(client_order_id="c-1", stop_loss=protection)
    assert trade.stop_loss.order_id == "SL-1"
    with pytest.raises(m.BrokerInvalidRequestError):
        make_trade(client_order_id="  ")
    for field_name in ("stop_loss", "take_profit"):
        with pytest.raises(m.BrokerInvalidRequestError):
            make_trade(**{field_name: "not-a-protection-order"})


def test_closed_trade_info_validates_shape_and_transaction_ids():
    closed = make_closed_trade(closing_transaction_ids=("900", "901"), client_order_id="c-1")
    assert closed.closing_transaction_ids == ("900", "901")
    assert make_closed_trade().closing_transaction_ids == ()
    with pytest.raises(m.BrokerInvalidRequestError):
        make_closed_trade(closing_transaction_ids=["900"])  # list, not tuple
    with pytest.raises(m.BrokerInvalidRequestError):
        make_closed_trade(closing_transaction_ids=("900", "900"))  # duplicates
    with pytest.raises(m.BrokerInvalidRequestError):
        make_closed_trade(closing_transaction_ids=("900", " "))
    with pytest.raises(m.BrokerInvalidRequestError):
        make_closed_trade(quantity=Decimal("0"))
    with pytest.raises(m.BrokerInvalidRequestError):
        make_closed_trade(side="long")
    with pytest.raises(m.BrokerInvalidRequestError):
        make_closed_trade(closed_at_utc=UTC_NOW.replace(tzinfo=None))
    with pytest.raises(m.BrokerInvalidRequestError):
        make_closed_trade(client_order_id="")


# --- Issue #102: broker order reissue chain ----------------------------------
#
# The remaining quantity of a partially filled order may be reissued by the
# broker as a NEW order under a NEW broker order id. broker_order_id always
# names the CURRENT actionable order; superseded_broker_order_ids is the
# append-only, oldest-first audit history of the ids the order lived under.


def partial_result(**overrides):
    kwargs = dict(
        state=m.OrderState.PARTIALLY_FILLED,
        filled_quantity=Decimal("4"),
        remaining_quantity=Decimal("6"),
        average_fill_price=Decimal("2400.50"),
        fills=(m.Fill("f-1", Decimal("2400.50"), Decimal("4"), UTC_NOW),),
    )
    kwargs.update(overrides)
    return make_result(**kwargs)


def test_reissue_chain_construction_and_audit_properties():
    result = partial_result(
        broker_order_id="B-2", superseded_broker_order_ids=("B-1",)
    )
    assert result.broker_order_id == "B-2"
    assert result.original_broker_order_id == "B-1"
    assert result.all_broker_order_ids == ("B-1", "B-2")


def test_without_a_chain_the_current_id_is_also_the_original():
    result = make_result()
    assert result.original_broker_order_id == "B-1"
    assert result.all_broker_order_ids == ("B-1",)
    result = make_result(
        state=m.OrderState.UNKNOWN, broker_order_id=None,
        filled_quantity=Decimal("0"), remaining_quantity=Decimal("10"),
        average_fill_price=None, fills=(),
    )
    assert result.original_broker_order_id is None
    assert result.all_broker_order_ids == ()


def test_reissue_chain_field_shape_is_validated():
    for bad in (["B-1"], "B-1", ("",), ("  ",), (1,)):
        with pytest.raises(m.BrokerInvalidRequestError):
            partial_result(broker_order_id="B-2", superseded_broker_order_ids=bad)
    with pytest.raises(m.BrokerInvalidRequestError):
        partial_result(
            broker_order_id="B-3", superseded_broker_order_ids=("B-1", "B-1")
        )


def test_reissue_chain_must_not_contain_the_current_id():
    with pytest.raises(ValueError):
        partial_result(
            broker_order_id="B-2", superseded_broker_order_ids=("B-1", "B-2")
        )


def test_reissue_chain_requires_a_current_id_and_a_partial_fill():
    # a chain without a current id cannot exist
    with pytest.raises(ValueError):
        make_result(
            state=m.OrderState.UNKNOWN, broker_order_id=None,
            filled_quantity=Decimal("0"), remaining_quantity=Decimal("10"),
            average_fill_price=None, fills=(),
            superseded_broker_order_ids=("B-1",),
        )
    # a chain without any fill would fabricate a reissue that cannot have
    # happened (reissues are caused by partial fills)
    with pytest.raises(ValueError):
        make_result(
            state=m.OrderState.ACCEPTED, broker_order_id="B-2",
            filled_quantity=Decimal("0"), remaining_quantity=Decimal("10"),
            average_fill_price=None, fills=(),
            superseded_broker_order_ids=("B-1",),
        )


def test_reissue_chain_is_allowed_for_every_fill_bearing_state():
    for state, filled in (
        (m.OrderState.PARTIALLY_FILLED, Decimal("4")),
        (m.OrderState.FILLED, Decimal("10")),
        (m.OrderState.CANCELLED, Decimal("4")),
        (m.OrderState.UNKNOWN, Decimal("4")),
    ):
        result = make_result(
            state=state,
            broker_order_id="B-2",
            filled_quantity=filled,
            remaining_quantity=Decimal("10") - filled,
            average_fill_price=Decimal("2400.50"),
            fills=(m.Fill("f-1", Decimal("2400.50"), filled, UTC_NOW),),
            superseded_broker_order_ids=("B-1",),
        )
        assert result.superseded_broker_order_ids == ("B-1",)


def test_update_allows_an_audited_reissue_id_change():
    old = partial_result()  # B-1, partially filled 4/10
    new = partial_result(
        broker_order_id="B-2",
        superseded_broker_order_ids=("B-1",),
        updated_at_utc=UTC_LATER,
    )
    m.validate_order_result_update(old, new)


def test_update_refuses_an_id_change_without_the_audited_chain():
    old = partial_result()
    for chain in ((), ("B-9",), ("B-0",)):
        new = partial_result(
            broker_order_id="B-2",
            superseded_broker_order_ids=chain,
            updated_at_utc=UTC_LATER,
        )
        with pytest.raises(ValueError) as exc_info:
            m.validate_order_result_update(old, new)
        assert "chain" in str(exc_info.value)


def test_update_refuses_dropping_the_id_even_with_a_chain():
    old = partial_result()
    new = partial_result(
        state=m.OrderState.UNKNOWN, broker_order_id=None,
        updated_at_utc=UTC_LATER,
    )
    with pytest.raises(ValueError):
        m.validate_order_result_update(old, new)


def test_update_refuses_chain_changes_while_the_id_is_unchanged():
    old = partial_result()
    new = partial_result(
        superseded_broker_order_ids=("B-0",), updated_at_utc=UTC_LATER
    )
    # constructing B-1 current with B-0 superseded is legal in isolation,
    # but as an UPDATE of a chainless B-1 snapshot it rewrites history
    with pytest.raises(ValueError):
        m.validate_order_result_update(old, new)


def test_update_allows_a_multi_hop_reissue_learned_in_one_observation():
    # between two polls the order went B-1 -> B-2 -> B-3: the new snapshot
    # extends the chain by the old current id FIRST, then the intermediate
    old = partial_result()
    new = partial_result(
        broker_order_id="B-3",
        superseded_broker_order_ids=("B-1", "B-2"),
        updated_at_utc=UTC_LATER,
    )
    m.validate_order_result_update(old, new)
    # but a chain that does not start with the known history is refused
    wrong = partial_result(
        broker_order_id="B-3",
        superseded_broker_order_ids=("B-2", "B-1"),
        updated_at_utc=UTC_LATER,
    )
    with pytest.raises(ValueError):
        m.validate_order_result_update(old, wrong)


def test_update_reissue_chain_progresses_across_repeated_reissues():
    first = partial_result(
        broker_order_id="B-2", superseded_broker_order_ids=("B-1",)
    )
    second = partial_result(
        broker_order_id="B-3",
        superseded_broker_order_ids=("B-1", "B-2"),
        filled_quantity=Decimal("6"),
        remaining_quantity=Decimal("4"),
        fills=first.fills + (m.Fill("f-2", Decimal("2401"), Decimal("2"), UTC_LATER),),
        average_fill_price=Decimal("2400.67"),
        updated_at_utc=UTC_LATER,
    )
    m.validate_order_result_update(first, second)
    # losing an intermediate id from the chain is refused
    truncated = partial_result(
        broker_order_id="B-3",
        superseded_broker_order_ids=("B-1",),
        filled_quantity=Decimal("6"),
        remaining_quantity=Decimal("4"),
        fills=second.fills,
        average_fill_price=Decimal("2400.67"),
        updated_at_utc=UTC_LATER,
    )
    with pytest.raises(ValueError):
        m.validate_order_result_update(first, truncated)


def test_update_reissued_order_can_reach_every_partial_fill_successor():
    old = partial_result(broker_order_id="B-2", superseded_broker_order_ids=("B-1",))
    fills = old.fills + (m.Fill("f-2", Decimal("2401"), Decimal("6"), UTC_LATER),)
    filled = make_result(
        state=m.OrderState.FILLED,
        broker_order_id="B-2",
        superseded_broker_order_ids=("B-1",),
        fills=fills,
        average_fill_price=Decimal("2400.80"),
        updated_at_utc=UTC_LATER,
    )
    m.validate_order_result_update(old, filled)
    cancelled = partial_result(
        state=m.OrderState.CANCELLED,
        broker_order_id="B-2",
        superseded_broker_order_ids=("B-1",),
        updated_at_utc=UTC_LATER,
    )
    m.validate_order_result_update(old, cancelled)
    unknown = partial_result(
        state=m.OrderState.UNKNOWN,
        broker_order_id="B-2",
        superseded_broker_order_ids=("B-1",),
        updated_at_utc=UTC_LATER,
    )
    m.validate_order_result_update(old, unknown)


def test_update_unknown_resolution_may_reveal_a_reissue_chain():
    # an UNKNOWN snapshot without any broker id can resolve into a queried
    # snapshot that reconstructed the whole chain
    old = make_result(
        state=m.OrderState.UNKNOWN, broker_order_id=None,
        filled_quantity=Decimal("0"), remaining_quantity=Decimal("10"),
        average_fill_price=None, fills=(),
    )
    new = partial_result(
        broker_order_id="B-2",
        superseded_broker_order_ids=("B-1",),
        updated_at_utc=UTC_LATER,
    )
    m.validate_order_result_update(old, new)


def test_terminal_same_state_reobservation_freezes_the_chain():
    done = make_result(
        state=m.OrderState.CANCELLED,
        broker_order_id="B-2",
        superseded_broker_order_ids=("B-1",),
        filled_quantity=Decimal("4"),
        remaining_quantity=Decimal("6"),
        average_fill_price=Decimal("2400.50"),
        fills=(m.Fill("f-1", Decimal("2400.50"), Decimal("4"), UTC_NOW),),
    )
    rewritten = make_result(
        state=m.OrderState.CANCELLED,
        broker_order_id="B-2",
        superseded_broker_order_ids=("B-0", "B-1"),
        filled_quantity=Decimal("4"),
        remaining_quantity=Decimal("6"),
        average_fill_price=Decimal("2400.50"),
        fills=done.fills,
        updated_at_utc=UTC_LATER,
    )
    with pytest.raises(ValueError):
        m.validate_order_result_update(done, rewritten)
