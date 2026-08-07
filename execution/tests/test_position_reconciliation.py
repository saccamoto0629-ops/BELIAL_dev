"""Issue #105: position / trade / transaction / SL / TP reconciliation.

Everything runs against ``tmp_path`` SQLite files and an in-memory
FakeBroker: no network, no Discord, no S3, no real orders, and the real
OANDA API is never touched (the opt-in demo-API suite lives in
``broker/tests/test_oanda_demo_adapter_demo_api.py``).

Like the Issue #103 suite, the service is driven through poisoned facades
whose ``submit_order`` / ``cancel_order`` raise -- every test here
simultaneously pins that the position scope never writes through the
broker. Broker-side fixtures are seeded on the FakeBroker BEFORE
reconciliation starts, which is why this file is listed in the
submission-boundary allow-list.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from broker.broker_models import (
    BrokerCommunicationError,
    ClosedTradeInfo,
    Fill,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderState,
    OrderType,
    PositionInfo,
    PositionSide,
    ProtectionOrderInfo,
    TradeInfo,
)
from broker.fake_broker import FakeBroker, RejectOrder
from execution import execution_store, reconciliation_store
from execution.broker_reconciliation import BrokerOrderReconciliationService
from execution.broker_reconciliation_repair import BrokerOrderReconciliationRepairService
from execution.execution_models import ExecutionRequest
from execution.reconciliation_models import (
    DiscrepancyType,
    PositionDiscrepancy,
    ReconciliationTargetType,
    ResolutionCategory,
)
from execution.reconciliation_repair_models import RepairMode, RepairOutcome

INTERNAL_T = datetime(2025, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
OBSERVED_AT = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
RETRIEVED_AT = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
FILL_PRICE = Decimal("2400")
QUANTITY = Decimal("10")


def client_id(request_id: str) -> str:
    return f"client-{request_id}"


class QueryOnlyBroker:
    """Query-only facade over a FakeBroker: ``submit_order`` /
    ``cancel_order`` are POISONED, listing capabilities delegate."""

    def __init__(self, inner: FakeBroker):
        self._inner = inner

    def submit_order(self, request):  # pragma: no cover - must never run
        raise AssertionError("reconciliation must never submit an order")

    def cancel_order(self, client_order_id):  # pragma: no cover - must never run
        raise AssertionError("reconciliation must never cancel an order")

    def get_order(self, client_order_id):
        return self._inner.get_order(client_order_id)

    def get_positions(self):
        return self._inner.get_positions()

    def get_account_state(self):
        return self._inner.get_account_state()

    def list_orders(self):
        return self._inner.list_orders()

    def get_open_trades(self):
        return self._inner.get_open_trades()

    def get_closed_trades(self):
        return self._inner.get_closed_trades()


class ListingFailureBroker(QueryOnlyBroker):
    """Scripted failures / misbehaviors for the listing operations."""

    def __init__(self, inner: FakeBroker, *, failures=None, misbehaviors=None):
        super().__init__(inner)
        self._failures = dict(failures or {})
        self._misbehaviors = dict(misbehaviors or {})

    def _dispatch(self, name, default):
        if name in self._failures:
            raise self._failures[name]
        if name in self._misbehaviors:
            return self._misbehaviors[name]
        return default()

    def get_positions(self):
        return self._dispatch("get_positions", self._inner.get_positions)

    def get_open_trades(self):
        return self._dispatch("get_open_trades", self._inner.get_open_trades)

    def get_closed_trades(self):
        return self._dispatch("get_closed_trades", self._inner.get_closed_trades)


class NoTradeCapabilityBroker:
    """The five Issue #73 operations plus order listing -- but NO trade or
    closed-trade capability (like the OANDA adapter before Issue #79's
    ``get_open_trades`` existed)."""

    def __init__(self, inner: FakeBroker):
        self._inner = inner

    def submit_order(self, request):  # pragma: no cover - must never run
        raise AssertionError("reconciliation must never submit an order")

    def cancel_order(self, client_order_id):  # pragma: no cover - must never run
        raise AssertionError("reconciliation must never cancel an order")

    def get_order(self, client_order_id):
        return self._inner.get_order(client_order_id)

    def get_positions(self):
        return self._inner.get_positions()

    def get_account_state(self):
        return self._inner.get_account_state()

    def list_orders(self):
        return self._inner.list_orders()


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "execution_orders.sqlite3", tmp_path / "broker_reconciliation.sqlite3"


def service(broker, paths, *, with_db=True):
    execution_db, reconciliation_db = paths
    return BrokerOrderReconciliationService(
        broker=broker,
        execution_db_path=execution_db,
        reconciliation_db_path=reconciliation_db if with_db else None,
        clock=lambda: OBSERVED_AT,
    )


def detect(broker, paths, *, run_id="run-1"):
    return service(broker, paths, with_db=False).detect(
        run_id=run_id, include_position_scope=True
    )


# --- internal / broker fixtures -----------------------------------------------


def make_request(
    request_id: str,
    *,
    symbol: str = "XAUUSD",
    side: OrderSide = OrderSide.BUY,
    quantity: Decimal = QUANTITY,
    stop_loss_price: Decimal | None = None,
    take_profit_price: Decimal | None = None,
) -> ExecutionRequest:
    return ExecutionRequest(
        execution_request_id=request_id,
        signal_id=f"sig-{request_id}",
        strategy_id="strat-1",
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity,
        requested_at_utc=INTERNAL_T,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
    )


def broker_request(
    request_id: str,
    *,
    symbol: str = "XAUUSD",
    side: OrderSide = OrderSide.BUY,
    quantity: Decimal = QUANTITY,
) -> OrderRequest:
    """Built directly (not via ``to_order_request``) so this fixture stays
    outside the sanctioned submission conversion path."""
    return OrderRequest(
        client_order_id=client_id(request_id),
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity,
        created_at_utc=INTERNAL_T,
    )


def seed_agreeing_order(
    db_path,
    broker: FakeBroker,
    request_id: str,
    *,
    symbol: str = "XAUUSD",
    side: OrderSide = OrderSide.BUY,
    quantity: Decimal = QUANTITY,
    stop_loss_price: Decimal | None = None,
    take_profit_price: Decimal | None = None,
    script=None,
) -> OrderResult:
    """Seed BOTH sides in agreement: submit on the FakeBroker and record the
    broker's own result internally, so the ORDER domain stays quiet and only
    the position domain under test speaks."""
    execution_store.create_execution_order(
        make_request(
            request_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
        ),
        client_order_id=client_id(request_id),
        created_at_utc=INTERNAL_T,
        db_path=db_path,
    )
    execution_store.mark_submitting(
        request_id, reason="submitting", changed_at_utc=INTERNAL_T, db_path=db_path
    )
    if script is not None:
        broker.script_order(client_id(request_id), script)
    result = broker.submit_order(
        broker_request(request_id, symbol=symbol, side=side, quantity=quantity)
    )
    execution_store.record_order_result(
        request_id, result, reason="broker result", db_path=db_path
    )
    return result


def seed_internal_only_cancelled(
    db_path, request_id: str, *, symbol: str = "XAUUSD"
) -> None:
    """An internal CANCELLED-with-zero-fills record whose order the broker
    never saw (the order domain reports it as BROKER_ORDER_MISSING)."""
    execution_store.create_execution_order(
        make_request(request_id, symbol=symbol),
        client_order_id=client_id(request_id),
        created_at_utc=INTERNAL_T,
        db_path=db_path,
    )
    execution_store.mark_submitting(
        request_id, reason="submitting", changed_at_utc=INTERNAL_T, db_path=db_path
    )
    execution_store.record_order_result(
        request_id,
        OrderResult(
            client_order_id=client_id(request_id),
            broker_order_id=None,
            state=OrderState.CANCELLED,
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            requested_quantity=QUANTITY,
            filled_quantity=Decimal("0"),
            remaining_quantity=QUANTITY,
            requested_price=None,
            average_fill_price=None,
            updated_at_utc=INTERNAL_T + timedelta(seconds=1),
        ),
        reason="cancelled before acceptance",
        db_path=db_path,
    )


def seed_internal_unknown(db_path, request_id: str, *, symbol: str = "XAUUSD") -> None:
    execution_store.create_execution_order(
        make_request(request_id, symbol=symbol),
        client_order_id=client_id(request_id),
        created_at_utc=INTERNAL_T,
        db_path=db_path,
    )
    execution_store.mark_submitting(
        request_id, reason="submitting", changed_at_utc=INTERNAL_T, db_path=db_path
    )
    execution_store.record_order_state(
        request_id,
        OrderState.UNKNOWN,
        reason="broker outcome unknown",
        changed_at_utc=INTERNAL_T + timedelta(seconds=1),
        db_path=db_path,
    )


def open_trade(
    trade_id: str,
    *,
    symbol: str = "XAUUSD",
    side: PositionSide = PositionSide.LONG,
    quantity: Decimal = QUANTITY,
    client_order_id: str | None = None,
    stop_loss: ProtectionOrderInfo | None = None,
    take_profit: ProtectionOrderInfo | None = None,
) -> TradeInfo:
    return TradeInfo(
        trade_id=trade_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        open_price=FILL_PRICE,
        opened_at_utc=RETRIEVED_AT,
        retrieved_at_utc=RETRIEVED_AT,
        client_order_id=client_order_id,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )


def closed_trade(
    trade_id: str,
    *,
    symbol: str = "XAUUSD",
    side: PositionSide = PositionSide.LONG,
    quantity: Decimal = QUANTITY,
    closing_transaction_ids: tuple[str, ...] = ("900",),
    client_order_id: str | None = None,
) -> ClosedTradeInfo:
    return ClosedTradeInfo(
        trade_id=trade_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        open_price=FILL_PRICE,
        opened_at_utc=RETRIEVED_AT,
        closed_at_utc=RETRIEVED_AT,
        retrieved_at_utc=RETRIEVED_AT,
        closing_transaction_ids=closing_transaction_ids,
        client_order_id=client_order_id,
    )


def position(
    *,
    symbol: str = "XAUUSD",
    side: PositionSide = PositionSide.LONG,
    quantity: Decimal = QUANTITY,
) -> PositionInfo:
    return PositionInfo(
        symbol=symbol,
        side=side,
        quantity=quantity,
        average_entry_price=FILL_PRICE,
        retrieved_at_utc=RETRIEVED_AT,
    )


def by_type(run, discrepancy_type):
    return [
        d for d in run.position_discrepancies if d.discrepancy_type is discrepancy_type
    ]


def only_types(run):
    return sorted(d.discrepancy_type.value for d in run.position_discrepancies)


# --- acceptance: open position / trade reconciliation -------------------------


def test_internal_position_missing_on_the_broker(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_agreeing_order(execution_db, broker, "req-1")
    # broker side: no position, no trades, no close evidence

    run = detect(QueryOnlyBroker(broker), paths)

    assert run.discrepancies == ()  # the order domain agrees
    (found,) = run.position_discrepancies
    assert found.discrepancy_type is DiscrepancyType.POSITION_MISSING_ON_BROKER
    assert found.target_type is ReconciliationTargetType.POSITION
    assert found.target_id == "XAUUSD:long"
    assert found.resolution_category is ResolutionCategory.MANUAL_REVIEW
    assert found.internal_value == "long 10"
    assert found.broker_value is None
    summary = run.position_summary
    assert summary.checked_position_count == 1
    assert summary.checked_trade_count == 0
    assert not summary.has_failures


def test_broker_only_position_is_manual_review_and_never_adopted(paths):
    execution_db, _ = paths
    broker = FakeBroker()
    broker.set_positions((position(symbol="EUR_USD", side=PositionSide.SHORT),))

    run = detect(QueryOnlyBroker(broker), paths)

    (found,) = run.position_discrepancies
    assert found.discrepancy_type is DiscrepancyType.BROKER_ONLY_POSITION
    assert found.target_id == "EUR_USD:short"
    assert found.resolution_category is ResolutionCategory.MANUAL_REVIEW
    assert found.broker_value == "short 10"
    assert found.internal_value is None
    # nothing was adopted: the execution DB was never even created
    assert not execution_db.exists()


def test_broker_only_trade_is_manual_review_and_never_adopted(paths):
    execution_db, _ = paths
    broker = FakeBroker()
    broker.set_open_trades((open_trade("T-9", symbol="EUR_USD"),))

    run = detect(QueryOnlyBroker(broker), paths)

    (found,) = run.position_discrepancies
    assert found.discrepancy_type is DiscrepancyType.BROKER_ONLY_TRADE
    assert found.target_type is ReconciliationTargetType.BROKER_TRADE
    assert found.target_id == "T-9"
    assert found.broker_trade_id == "T-9"
    assert found.resolution_category is ResolutionCategory.MANUAL_REVIEW
    assert not execution_db.exists()


def test_internal_closed_but_broker_holds_an_open_trade_rejected_order(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_agreeing_order(execution_db, broker, "req-1", script=RejectOrder(reason="margin"))
    broker.set_open_trades((open_trade("T-1", client_order_id=client_id("req-1")),))

    run = detect(QueryOnlyBroker(broker), paths)

    (found,) = by_type(run, DiscrepancyType.INTERNAL_CLOSED_BROKER_OPEN)
    assert found.target_id == "req-1"
    assert found.execution_request_id == "req-1"
    assert found.internal_value == "rejected"
    assert found.broker_value == "open long 10"
    assert found.broker_trade_id == "T-1"
    assert found.resolution_category is ResolutionCategory.MANUAL_REVIEW


def test_internal_cancelled_zero_fill_but_broker_holds_an_open_trade(paths):
    execution_db, _ = paths
    broker = FakeBroker()
    seed_internal_only_cancelled(execution_db, "req-1")
    broker.set_open_trades((open_trade("T-2", client_order_id=client_id("req-1")),))

    run = detect(QueryOnlyBroker(broker), paths)

    (found,) = by_type(run, DiscrepancyType.INTERNAL_CLOSED_BROKER_OPEN)
    assert found.internal_value == "cancelled"
    assert found.broker_trade_id == "T-2"
    # the order domain independently reports the broker-missing order
    assert [d.discrepancy_type for d in run.discrepancies] == [
        DiscrepancyType.BROKER_ORDER_MISSING
    ]


# --- acceptance: closed position / transaction reconciliation -----------------


def test_broker_closed_while_internal_open_is_detected_from_close_evidence(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    result = seed_agreeing_order(execution_db, broker, "req-1")
    fill_id = result.fills[0].fill_id
    broker.set_closed_trades(
        (closed_trade(fill_id, closing_transaction_ids=("900", "901")),)
    )

    run = detect(QueryOnlyBroker(broker), paths)

    (found,) = by_type(run, DiscrepancyType.BROKER_CLOSED_INTERNAL_OPEN)
    assert found.target_type is ReconciliationTargetType.EXECUTION_ORDER
    assert found.target_id == "req-1"
    assert found.broker_trade_id == fill_id
    assert found.broker_transaction_ids == ("900", "901")
    assert found.internal_value == "open long 10"
    assert found.broker_value == "closed long 10"
    assert found.resolution_category is ResolutionCategory.MANUAL_REVIEW
    assert "900" in found.detail and "901" in found.detail
    # the position-level absence is reported independently
    assert by_type(run, DiscrepancyType.POSITION_MISSING_ON_BROKER)


def test_close_evidence_matched_by_broker_reported_client_id(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_agreeing_order(execution_db, broker, "req-1")
    broker.set_closed_trades(
        (closed_trade("T-77", client_order_id=client_id("req-1")),)
    )

    run = detect(QueryOnlyBroker(broker), paths)

    (found,) = by_type(run, DiscrepancyType.BROKER_CLOSED_INTERNAL_OPEN)
    assert found.broker_trade_id == "T-77"


def test_internally_offset_exposure_makes_the_broker_close_expected(paths):
    """BUY 10 then SELL 10 both filled: BELIAL's net is zero, so the broker
    having closed the trade is EXPECTED -- no discrepancy, nothing flagged."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    result = seed_agreeing_order(execution_db, broker, "req-1", side=OrderSide.BUY)
    seed_agreeing_order(execution_db, broker, "req-2", side=OrderSide.SELL)
    broker.set_closed_trades((closed_trade(result.fills[0].fill_id),))

    run = detect(QueryOnlyBroker(broker), paths)

    assert run.position_discrepancies == ()
    assert not run.has_partial_failures


def test_a_still_open_trade_is_not_treated_as_close_evidence(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    result = seed_agreeing_order(execution_db, broker, "req-1")
    fill_id = result.fills[0].fill_id
    broker.set_positions((position(),))
    broker.set_open_trades((open_trade(fill_id),))
    # a broker inconsistency: the same id also appears in the closed listing
    broker.set_closed_trades((closed_trade(fill_id),))

    run = detect(QueryOnlyBroker(broker), paths)

    assert by_type(run, DiscrepancyType.BROKER_CLOSED_INTERNAL_OPEN) == []


# --- acceptance: SL / TP reconciliation ---------------------------------------


def seed_protected_order(paths, broker, *, sl=Decimal("2390"), tp=Decimal("2420")):
    execution_db, _ = paths
    result = seed_agreeing_order(
        execution_db,
        broker,
        "req-1",
        stop_loss_price=sl,
        take_profit_price=tp,
    )
    broker.set_positions((position(),))  # silence the position-level check
    return result.fills[0].fill_id


def test_stop_loss_and_take_profit_missing_on_the_broker(paths):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    fill_id = seed_protected_order(paths, broker)
    broker.set_open_trades((open_trade(fill_id),))  # no SL, no TP attached

    run = detect(QueryOnlyBroker(broker), paths)

    assert only_types(run) == ["stop_loss_missing", "take_profit_missing"]
    sl = by_type(run, DiscrepancyType.STOP_LOSS_MISSING)[0]
    assert sl.target_id == "req-1"
    assert sl.internal_value == "2390"
    assert sl.broker_value is None
    assert sl.broker_trade_id == fill_id
    assert sl.protection_order_id is None
    assert sl.resolution_category is ResolutionCategory.MANUAL_REVIEW


def test_stop_loss_price_mismatch_is_detected_with_exact_decimals(paths):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    fill_id = seed_protected_order(paths, broker)
    broker.set_open_trades(
        (
            open_trade(
                fill_id,
                stop_loss=ProtectionOrderInfo(
                    order_id="SL-1", price=Decimal("2389.5"), trade_id=fill_id
                ),
                take_profit=ProtectionOrderInfo(
                    order_id="TP-1", price=Decimal("2420"), trade_id=fill_id
                ),
            ),
        )
    )

    run = detect(QueryOnlyBroker(broker), paths)

    assert only_types(run) == ["stop_loss_price_mismatch"]
    (found,) = run.position_discrepancies
    assert found.internal_value == "2390"
    assert found.broker_value == "2389.5"
    assert found.protection_order_id == "SL-1"


def test_take_profit_price_mismatch_is_detected(paths):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    fill_id = seed_protected_order(paths, broker)
    broker.set_open_trades(
        (
            open_trade(
                fill_id,
                stop_loss=ProtectionOrderInfo(
                    order_id="SL-1", price=Decimal("2390"), trade_id=fill_id
                ),
                take_profit=ProtectionOrderInfo(
                    order_id="TP-1", price=Decimal("2421"), trade_id=fill_id
                ),
            ),
        )
    )

    run = detect(QueryOnlyBroker(broker), paths)

    assert only_types(run) == ["take_profit_price_mismatch"]
    (found,) = run.position_discrepancies
    assert found.protection_order_id == "TP-1"


def test_protection_reference_mismatches_are_detected(paths):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    fill_id = seed_protected_order(paths, broker)
    broker.set_open_trades(
        (
            open_trade(
                fill_id,
                stop_loss=ProtectionOrderInfo(
                    order_id="SL-1", price=Decimal("2390"), trade_id="T-OTHER"
                ),
                take_profit=ProtectionOrderInfo(
                    order_id="TP-1", price=Decimal("2420"), trade_id="T-ELSEWHERE"
                ),
            ),
        )
    )

    run = detect(QueryOnlyBroker(broker), paths)

    assert only_types(run) == [
        "stop_loss_reference_mismatch",
        "take_profit_reference_mismatch",
    ]
    sl = by_type(run, DiscrepancyType.STOP_LOSS_REFERENCE_MISMATCH)[0]
    assert sl.internal_value == fill_id  # the trade the SL should protect
    assert sl.broker_value == "T-OTHER"  # the trade it actually references


def test_price_and_reference_mismatch_coexist_on_one_target(paths):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    fill_id = seed_protected_order(paths, broker)
    broker.set_open_trades(
        (
            open_trade(
                fill_id,
                stop_loss=ProtectionOrderInfo(
                    order_id="SL-1", price=Decimal("1"), trade_id="T-OTHER"
                ),
                take_profit=ProtectionOrderInfo(
                    order_id="TP-1", price=Decimal("2420"), trade_id=fill_id
                ),
            ),
        )
    )

    run = detect(QueryOnlyBroker(broker), paths)

    assert only_types(run) == [
        "stop_loss_price_mismatch",
        "stop_loss_reference_mismatch",
    ]


def test_matching_protection_with_different_decimal_scale_is_no_mismatch(paths):
    """Decimal VALUE equality decides (existing repository convention):
    2390 == 2390.00; no float comparison and no tolerance are introduced."""
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    fill_id = seed_protected_order(paths, broker)
    broker.set_open_trades(
        (
            open_trade(
                fill_id,
                stop_loss=ProtectionOrderInfo(
                    order_id="SL-1", price=Decimal("2390.00"), trade_id=fill_id
                ),
                take_profit=ProtectionOrderInfo(
                    order_id="TP-1", price=Decimal("2420.000"), trade_id=fill_id
                ),
            ),
        )
    )

    run = detect(QueryOnlyBroker(broker), paths)

    assert run.position_discrepancies == ()


def test_unexpected_broker_protection_orders_are_detected(paths):
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    fill_id = seed_protected_order(paths, broker, sl=None, tp=None)
    broker.set_open_trades(
        (
            open_trade(
                fill_id,
                stop_loss=ProtectionOrderInfo(
                    order_id="SL-1", price=Decimal("2390"), trade_id=fill_id
                ),
                take_profit=ProtectionOrderInfo(
                    order_id="TP-1", price=Decimal("2420"), trade_id=fill_id
                ),
            ),
        )
    )

    run = detect(QueryOnlyBroker(broker), paths)

    assert only_types(run) == ["stop_loss_unexpected", "take_profit_unexpected"]
    for found in run.position_discrepancies:
        assert found.internal_value is None
        assert found.resolution_category is ResolutionCategory.MANUAL_REVIEW


def test_duplicate_protection_findings_from_several_trades_collapse_to_one(paths):
    """One order whose fills opened several broker trades: the SL-missing
    finding is recorded once per (kind, target), not once per trade."""
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_protected_order(paths, broker)
    broker.set_open_trades(
        (
            open_trade("T-A", client_order_id=client_id("req-1")),
            open_trade("T-B", client_order_id=client_id("req-1")),
        )
    )

    run = detect(QueryOnlyBroker(broker), paths)

    assert only_types(run) == ["stop_loss_missing", "take_profit_missing"]


# --- matching: ambiguity and unresolved internal outcomes ---------------------


def test_a_trade_matching_two_orders_is_ambiguous_never_guessed(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    result_a = seed_agreeing_order(execution_db, broker, "req-a")
    seed_agreeing_order(execution_db, broker, "req-b")
    broker.set_positions((position(quantity=QUANTITY * 2),))
    # matches req-b by client id AND req-a by fill id
    broker.set_open_trades(
        (
            open_trade(
                result_a.fills[0].fill_id, client_order_id=client_id("req-b")
            ),
        )
    )

    run = detect(QueryOnlyBroker(broker), paths)

    (found,) = by_type(run, DiscrepancyType.TRADE_MATCH_AMBIGUOUS)
    assert found.target_id == result_a.fills[0].fill_id
    assert "req-a" in found.detail and "req-b" in found.detail
    assert found.resolution_category is ResolutionCategory.MANUAL_REVIEW


def test_open_trade_with_cross_symbol_id_match_is_ambiguous_not_reconciled(paths):
    """ID evidence pointing at an order of a DIFFERENT instrument is
    contradictory (PR #108 re-review): no SL/TP comparison runs against the
    wrong symbol, and the trade is neither broker-only nor matched."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    result = seed_agreeing_order(
        execution_db,
        broker,
        "req-1",
        stop_loss_price=Decimal("2390"),
        take_profit_price=Decimal("2420"),
    )
    broker.set_positions((position(),))  # the XAUUSD expectation is satisfied
    broker.set_open_trades(
        (open_trade(result.fills[0].fill_id, symbol="EUR_USD"),)
    )

    run = detect(QueryOnlyBroker(broker), paths)

    assert only_types(run) == ["trade_match_ambiguous"]
    (found,) = run.position_discrepancies
    assert found.target_id == result.fills[0].fill_id
    assert found.symbol == "EUR_USD"  # the broker-observed identity
    assert "req-1" in found.detail and "contradicts" in found.detail
    assert found.resolution_category is ResolutionCategory.MANUAL_REVIEW


def test_open_trade_with_cross_side_id_match_is_ambiguous_not_reconciled(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    result = seed_agreeing_order(
        execution_db, broker, "req-1", stop_loss_price=Decimal("2390")
    )
    broker.set_positions((position(),))
    broker.set_open_trades(
        (open_trade(result.fills[0].fill_id, side=PositionSide.SHORT),)
    )

    run = detect(QueryOnlyBroker(broker), paths)

    # no stop_loss_missing is fabricated from a contradictory match
    assert only_types(run) == ["trade_match_ambiguous"]


def test_closed_trade_with_cross_symbol_id_match_never_confirms_a_close(paths):
    """A closed trade whose ID evidence matches an order but whose symbol
    differs must not produce broker_closed_internal_open (a false close
    confirmation); it is surfaced as ambiguous instead."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    result = seed_agreeing_order(execution_db, broker, "req-1")
    broker.set_positions((position(),))  # exposure still open broker-side
    broker.set_closed_trades(
        (closed_trade(result.fills[0].fill_id, symbol="EUR_USD"),)
    )

    run = detect(QueryOnlyBroker(broker), paths)

    assert by_type(run, DiscrepancyType.BROKER_CLOSED_INTERNAL_OPEN) == []
    (found,) = by_type(run, DiscrepancyType.TRADE_MATCH_AMBIGUOUS)
    assert found.target_id == result.fills[0].fill_id
    assert "close evidence" in found.detail


def test_closed_trade_with_cross_side_id_match_is_ambiguous_not_silently_skipped(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    result = seed_agreeing_order(execution_db, broker, "req-1")
    broker.set_positions((position(),))
    broker.set_closed_trades(
        (closed_trade(result.fills[0].fill_id, side=PositionSide.SHORT),)
    )

    run = detect(QueryOnlyBroker(broker), paths)

    assert by_type(run, DiscrepancyType.BROKER_CLOSED_INTERNAL_OPEN) == []
    assert by_type(run, DiscrepancyType.TRADE_MATCH_AMBIGUOUS)
    assert run.has_partial_failures or run.position_discrepancies


def test_open_trade_with_fill_match_but_foreign_client_id_is_ambiguous(paths):
    """A fill-id hit whose REPORTED client id belongs to no internal order
    is contradictory identifier evidence (PR #108 re-review 2): upstream
    sees only one candidate, so the conflict must be caught here -- no SL/TP
    comparison may run off it."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    result = seed_agreeing_order(
        execution_db,
        broker,
        "req-1",
        stop_loss_price=Decimal("2390"),
        take_profit_price=Decimal("2420"),
    )
    broker.set_positions((position(),))
    broker.set_open_trades(
        (
            open_trade(
                result.fills[0].fill_id,  # matches req-1 by recorded fill id
                client_order_id="client-someone-else",  # ... but names nobody
            ),
        )
    )

    run = detect(QueryOnlyBroker(broker), paths)

    assert only_types(run) == ["trade_match_ambiguous"]
    (found,) = run.position_discrepancies
    assert found.client_order_id == "client-someone-else"
    assert "client_order_id" in found.detail
    assert found.resolution_category is ResolutionCategory.MANUAL_REVIEW


def test_closed_trade_with_fill_match_but_foreign_client_id_confirms_no_close(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    result = seed_agreeing_order(execution_db, broker, "req-1")
    broker.set_positions((position(),))
    broker.set_closed_trades(
        (
            closed_trade(
                result.fills[0].fill_id, client_order_id="client-someone-else"
            ),
        )
    )

    run = detect(QueryOnlyBroker(broker), paths)

    assert by_type(run, DiscrepancyType.BROKER_CLOSED_INTERNAL_OPEN) == []
    (found,) = by_type(run, DiscrepancyType.TRADE_MATCH_AMBIGUOUS)
    assert "client_order_id" in found.detail


def test_coherent_client_id_and_fill_id_evidence_still_matches(paths):
    """The positive control for the identity checks: a trade carrying BOTH
    the order's client id and its fill id, on the same symbol and side, is
    still a match and still produces the SL/TP finding."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    result = seed_agreeing_order(
        execution_db, broker, "req-1", stop_loss_price=Decimal("2390")
    )
    broker.set_positions((position(),))
    broker.set_open_trades(
        (
            open_trade(
                result.fills[0].fill_id, client_order_id=client_id("req-1")
            ),
        )
    )

    run = detect(QueryOnlyBroker(broker), paths)

    assert only_types(run) == ["stop_loss_missing"]
    assert by_type(run, DiscrepancyType.TRADE_MATCH_AMBIGUOUS) == []


def test_identity_conflict_classification_is_stable_across_repeated_runs(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    result = seed_agreeing_order(execution_db, broker, "req-1")
    broker.set_positions((position(),))
    broker.set_open_trades(
        (open_trade(result.fills[0].fill_id, symbol="EUR_USD"),)
    )
    facade = QueryOnlyBroker(broker)
    before = db_bytes(execution_db)

    first = detect(facade, paths, run_id="run-1")
    second = detect(facade, paths, run_id="run-2")

    assert only_types(first) == ["trade_match_ambiguous"]
    assert first.position_discrepancies == second.position_discrepancies
    assert db_bytes(execution_db) == before


def test_unmatched_trade_on_a_symbol_with_unknown_orders_is_not_broker_only(paths):
    execution_db, _ = paths
    broker = FakeBroker()
    seed_internal_unknown(execution_db, "req-u")
    broker.set_open_trades((open_trade("T-5"),))

    run = detect(QueryOnlyBroker(broker), paths)

    (found,) = by_type(run, DiscrepancyType.TRADE_MATCH_AMBIGUOUS)
    assert found.target_id == "T-5"
    assert "unresolved" in found.detail
    assert by_type(run, DiscrepancyType.BROKER_ONLY_TRADE) == []
    # the symbol's exposure comparison was skipped fail-safe
    assert run.position_summary.excluded_symbols == ("XAUUSD",)
    assert by_type(run, DiscrepancyType.POSITION_MISSING_ON_BROKER) == []
    assert run.has_partial_failures


def test_unknown_orders_never_produce_position_discrepancies(paths):
    execution_db, _ = paths
    broker = FakeBroker()
    seed_internal_unknown(execution_db, "req-u")
    broker.set_positions((position(),))

    run = detect(QueryOnlyBroker(broker), paths)

    # neither "missing" nor "broker-only" is concluded for the excluded symbol
    assert run.position_discrepancies == ()
    assert run.position_summary.excluded_symbols == ("XAUUSD",)


# --- fail-closed: broker lookup failures --------------------------------------


def db_bytes(path):
    return path.read_bytes() if path.exists() else None


def test_position_listing_failure_changes_nothing_and_is_reported(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_agreeing_order(execution_db, broker, "req-1")
    before = db_bytes(execution_db)

    run = detect(
        ListingFailureBroker(
            broker, failures={"get_positions": BrokerCommunicationError("down")}
        ),
        paths,
    )

    assert run.position_summary.position_listing_error is not None
    assert run.position_summary.checked_position_count == 0
    assert by_type(run, DiscrepancyType.POSITION_MISSING_ON_BROKER) == []
    assert run.has_partial_failures
    assert db_bytes(execution_db) == before


def test_trade_listing_failure_never_fabricates_sl_tp_findings(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_agreeing_order(
        execution_db,
        broker,
        "req-1",
        stop_loss_price=Decimal("2390"),
        take_profit_price=Decimal("2420"),
    )
    broker.set_positions((position(),))
    before = db_bytes(execution_db)

    run = detect(
        ListingFailureBroker(
            broker, failures={"get_open_trades": BrokerCommunicationError("down")}
        ),
        paths,
    )

    summary = run.position_summary
    assert summary.trade_listing_error is not None
    # closed evidence is skipped too: "no longer open" cannot be established
    assert summary.closed_trade_listing_error is not None
    assert "open-trade listing" in summary.closed_trade_listing_error
    assert run.position_discrepancies == ()
    assert run.has_partial_failures
    assert db_bytes(execution_db) == before


def test_closed_listing_failure_never_fabricates_close_findings(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    result = seed_agreeing_order(execution_db, broker, "req-1")
    broker.set_positions((position(),))
    broker.set_open_trades((open_trade(result.fills[0].fill_id),))

    run = detect(
        ListingFailureBroker(
            broker, failures={"get_closed_trades": BrokerCommunicationError("down")}
        ),
        paths,
    )

    assert run.position_summary.closed_trade_listing_error is not None
    assert by_type(run, DiscrepancyType.BROKER_CLOSED_INTERNAL_OPEN) == []
    assert run.has_partial_failures


def test_broker_contract_violations_fail_closed_per_listing(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_agreeing_order(execution_db, broker, "req-1")
    before = db_bytes(execution_db)

    run = detect(
        ListingFailureBroker(
            broker,
            misbehaviors={
                "get_positions": ["not-a-tuple"],
                "get_open_trades": (object(),),
                "get_closed_trades": "junk",
            },
        ),
        paths,
    )

    summary = run.position_summary
    assert "contract" in summary.position_listing_error
    assert "contract" in summary.trade_listing_error
    assert run.position_discrepancies == ()
    assert db_bytes(execution_db) == before


def test_duplicate_trade_ids_make_the_enumeration_ambiguous_fail_closed(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_agreeing_order(execution_db, broker, "req-1")
    broker.set_positions((position(),))
    broker.set_open_trades((open_trade("T-1"), open_trade("T-1")))

    run = detect(QueryOnlyBroker(broker), paths)

    assert "ambiguous" in run.position_summary.trade_listing_error
    assert run.position_discrepancies == ()
    assert run.has_partial_failures


def test_missing_trade_capability_is_reported_not_silently_skipped(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_agreeing_order(execution_db, broker, "req-1")
    broker.set_positions((position(),))

    run = detect(NoTradeCapabilityBroker(broker), paths)

    summary = run.position_summary
    assert "TradeListingBroker" in summary.trade_listing_error
    assert summary.closed_trade_listing_error is not None
    assert run.has_partial_failures


def test_execution_store_read_failure_excludes_the_symbol_fail_safe(paths, monkeypatch):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_agreeing_order(execution_db, broker, "req-1")
    broker.set_positions((position(),))
    before = db_bytes(execution_db)
    original = execution_store.load_order_result

    def failing_load(request_id, *, db_path=None):
        if request_id == "req-1":
            raise execution_store.ExecutionStoreError("scripted read failure")
        return original(request_id, db_path=db_path)

    monkeypatch.setattr(execution_store, "load_order_result", failing_load)

    run = detect(QueryOnlyBroker(broker), paths)

    summary = run.position_summary
    assert "read(s) failed" in summary.internal_exposure_error
    assert summary.excluded_symbols == ("XAUUSD",)
    # neither direction of the position comparison is concluded
    assert run.position_discrepancies == ()
    assert run.has_partial_failures
    assert db_bytes(execution_db) == before


# --- idempotency and reconcile-only guarantees --------------------------------


def full_fixture(paths):
    """A mixed fixture producing discrepancies in several kinds."""
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_agreeing_order(
        execution_db,
        broker,
        "req-1",
        stop_loss_price=Decimal("2390"),
        take_profit_price=Decimal("2420"),
    )
    result = seed_agreeing_order(execution_db, broker, "req-2", symbol="EUR_USD")
    broker.set_positions((position(),))
    broker.set_open_trades(
        (
            open_trade("T-1", client_order_id=client_id("req-1")),
            open_trade("T-9", symbol="GBP_USD"),
        )
    )
    broker.set_closed_trades(
        (closed_trade(result.fills[0].fill_id, symbol="EUR_USD"),)
    )
    return broker


def test_repeated_reconciliation_is_idempotent(paths):
    execution_db, reconciliation_db = paths
    broker = full_fixture(paths)
    facade = QueryOnlyBroker(broker)

    first = service(facade, paths).run(run_id="run-1", include_position_scope=True)
    execution_after_first = db_bytes(execution_db)
    second = service(facade, paths).run(run_id="run-2", include_position_scope=True)

    # identical observations, appended as independent runs
    assert first.position_discrepancies == second.position_discrepancies
    assert first.position_summary == second.position_summary
    assert first.discrepancies == second.discrepancies
    assert db_bytes(execution_db) == execution_after_first
    stored_first = reconciliation_store.get_reconciliation_run(
        "run-1", db_path=reconciliation_db
    )
    stored_second = reconciliation_store.get_reconciliation_run(
        "run-2", db_path=reconciliation_db
    )
    assert stored_first == first
    assert stored_second == second


def test_recording_the_same_run_id_twice_is_refused(paths):
    _, reconciliation_db = paths
    broker = full_fixture(paths)
    facade = QueryOnlyBroker(broker)
    service(facade, paths).run(run_id="run-1", include_position_scope=True)
    before = db_bytes(reconciliation_db)

    with pytest.raises(reconciliation_store.ReconciliationRunConflictError):
        service(facade, paths).run(run_id="run-1", include_position_scope=True)

    assert db_bytes(reconciliation_db) == before


def test_reconcile_only_run_performs_no_mutation_anywhere(paths):
    execution_db, reconciliation_db = paths
    broker = full_fixture(paths)
    submits, cancels = len(broker.submitted_requests), len(broker.cancel_requests)
    execution_before = db_bytes(execution_db)

    run = detect(QueryOnlyBroker(broker), paths)

    assert run.position_discrepancies  # the fixture does produce findings
    # no broker write was even attempted (poisoned facade would raise)
    assert len(broker.submitted_requests) == submits
    assert len(broker.cancel_requests) == cancels
    # no internal state was touched, no audit DB was created by detect()
    assert db_bytes(execution_db) == execution_before
    assert not reconciliation_db.exists()


def test_every_position_discrepancy_is_manual_review_by_construction(paths):
    broker = full_fixture(paths)
    run = detect(QueryOnlyBroker(broker), paths)
    assert run.position_discrepancies
    assert {
        d.resolution_category for d in run.position_discrepancies
    } == {ResolutionCategory.MANUAL_REVIEW}
    with pytest.raises(ValueError, match="fail-safe"):
        replace(
            run.position_discrepancies[0],
            resolution_category=ResolutionCategory.AUTO_REPAIR_CANDIDATE,
        )


def test_orders_only_run_records_no_position_scope(paths):
    execution_db, _ = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    seed_agreeing_order(execution_db, broker, "req-1")

    run = service(QueryOnlyBroker(broker), paths, with_db=False).detect(run_id="run-1")

    assert run.position_summary is None
    assert run.position_discrepancies == ()


# --- regression: Issue #104 repair never touches the position domain ----------


def test_repair_processes_order_discrepancies_and_ignores_position_records(paths):
    execution_db, reconciliation_db = paths
    broker = FakeBroker(default_fill_price=FILL_PRICE)
    # order domain: an UNKNOWN order the broker settled as FILLED
    # (auto-repair candidate for Issue #104)
    seed_internal_unknown(execution_db, "req-u")
    broker.submit_order(broker_request("req-u"))
    # position domain: a broker-only trade on another symbol
    broker.set_open_trades((open_trade("T-9", symbol="EUR_USD"),))

    facade = QueryOnlyBroker(broker)
    run = service(facade, paths).run(run_id="run-1", include_position_scope=True)
    assert by_type(run, DiscrepancyType.BROKER_ONLY_TRADE)
    assert [d.discrepancy_type for d in run.discrepancies] == [
        DiscrepancyType.UNKNOWN_BROKER_SETTLED
    ]

    repairer = BrokerOrderReconciliationRepairService(
        broker=facade,
        execution_db_path=execution_db,
        reconciliation_db_path=reconciliation_db,
        lease_duration_seconds=600,
    )
    result = repairer.repair(run_id="run-1", mode=RepairMode.APPLY)

    # exactly ONE attempt: the order discrepancy; the position-domain record
    # is structurally invisible to repair (it is not an order discrepancy)
    assert [attempt.outcome for attempt in result.attempts] == [RepairOutcome.APPLIED]
    assert result.attempts[0].target_type is ReconciliationTargetType.EXECUTION_ORDER
    assert result.attempts[0].target_id == "req-u"
    # the position-domain record is still stored, untouched and manual-review
    stored = reconciliation_store.get_reconciliation_run(
        "run-1", db_path=reconciliation_db
    )
    assert stored.position_discrepancies == run.position_discrepancies
    # and the repaired order is now FILLED internally
    row = execution_store.get_execution_order("req-u", db_path=execution_db)
    assert row["order_state"] == OrderState.FILLED.value


# --- model guard: position-domain kinds never enter OrderDiscrepancy ----------


def test_position_domain_kinds_are_rejected_by_order_discrepancy():
    from execution.reconciliation_models import OrderDiscrepancy

    with pytest.raises(ValueError, match="position-domain"):
        OrderDiscrepancy(
            discrepancy_type=DiscrepancyType.BROKER_ONLY_TRADE,
            target_type=ReconciliationTargetType.BROKER_TRADE,
            target_id="T-1",
            resolution_category=ResolutionCategory.MANUAL_REVIEW,
            observed_at_utc=OBSERVED_AT,
            detail="x",
        )


def test_order_domain_kinds_are_rejected_by_position_discrepancy():
    with pytest.raises(ValueError, match="order-domain"):
        PositionDiscrepancy(
            discrepancy_type=DiscrepancyType.BROKER_ONLY_ORDER,
            target_type=ReconciliationTargetType.BROKER_TRADE,
            target_id="T-1",
            resolution_category=ResolutionCategory.MANUAL_REVIEW,
            observed_at_utc=OBSERVED_AT,
            detail="x",
            symbol="XAUUSD",
        )
