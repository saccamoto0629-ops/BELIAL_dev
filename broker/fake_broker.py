r"""FakeBroker — deterministic in-memory test double for the Broker interface (Issue #73).

*** TEST DOUBLE. NO NETWORK, NO SLEEP, NO RANDOMNESS, NO DB, NO DISCORD. ***

FakeBroker exists so future Live Execution code can be tested against the
``broker_interface.Broker`` boundary without any external system. It is NOT
part of Paper Execution (``paper-execution/`` stays broker-free), and its
existence does not mean Live Execution is implemented.

Behaviour is injected per ``client_order_id`` as a *scripted scenario*
(small dataclasses, no generic scenario engine):

    broker = FakeBroker()
    broker.script_order("c-1", FillImmediately(price=Decimal("100.5")))
    broker.script_order("c-2", RejectOrder(reason="insufficient margin"))
    broker.script_order("c-3", TimeoutOrder())
    broker.script_cancel("c-4", FailCancellation(reason="already filled"))

Unscripted orders use ``default_script`` (immediate fill by default).
Timeouts are reproduced immediately as ``BrokerTimeoutError`` — never by
waiting — and leave the order in ``OrderState.UNKNOWN`` (the broker may have
accepted it). A communication failure (``FailCommunication``) means the
request never reached the broker: no order is recorded at all.

Timestamps come from a deterministic injected clock (fixed base time plus one
second per event) unless the test supplies its own ``clock`` callable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Union

from broker_models import (
    AccountState,
    BrokerCancellationError,
    BrokerCommunicationError,
    BrokerInvalidRequestError,
    BrokerOrderNotFoundError,
    BrokerTimeoutError,
    Fill,
    OrderRequest,
    OrderResult,
    OrderState,
    OrderType,
    PositionInfo,
    TERMINAL_ORDER_STATES,
    validate_order_state_transition,
    weighted_average_price,
)

# --- scripted scenarios ------------------------------------------------------


@dataclass(frozen=True)
class FillImmediately:
    """Fill the whole order at ``price`` (falls back to the request's limit
    price, then the broker's ``default_fill_price``)."""

    price: Decimal | None = None


@dataclass(frozen=True)
class StayPending:
    """Broker accepts the order but nothing fills (state ``ACCEPTED``)."""


@dataclass(frozen=True)
class PartiallyFill:
    """Fill only ``fills`` — a tuple of (price, quantity) pairs whose total
    must be strictly less than the requested quantity."""

    fills: tuple[tuple[Decimal, Decimal], ...]


@dataclass(frozen=True)
class RejectOrder:
    """Broker definitively rejects the order (state ``REJECTED``)."""

    reason: str = "rejected by broker"


@dataclass(frozen=True)
class TimeoutOrder:
    """Raise ``BrokerTimeoutError`` immediately; the order is recorded with
    state ``UNKNOWN`` because the broker may have accepted it."""


@dataclass(frozen=True)
class FailCommunication:
    """Raise ``BrokerCommunicationError`` immediately; the request never
    reached the broker, so no order is recorded."""


SubmitScript = Union[
    FillImmediately, StayPending, PartiallyFill, RejectOrder, TimeoutOrder, FailCommunication
]


@dataclass(frozen=True)
class FailCancellation:
    """Broker refuses the cancel (``BrokerCancellationError``); order state
    is left unchanged."""

    reason: str = "cancellation failed"


CancelScript = FailCancellation

_CLOCK_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)

_DEFAULT_ACCOUNT_STATE = AccountState(
    account_id="fake-account",
    currency="USD",
    balance=Decimal("100000"),
    equity=Decimal("100000"),
    margin_available=Decimal("100000"),
    retrieved_at_utc=_CLOCK_BASE,
)


class FakeBroker:
    """In-memory, deterministic implementation of ``broker_interface.Broker``."""

    def __init__(
        self,
        *,
        default_script: SubmitScript | None = None,
        default_fill_price: Decimal | None = None,
        account_state: AccountState | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._default_script: SubmitScript = default_script or FillImmediately()
        self._default_fill_price = default_fill_price
        self._account_state = account_state or _DEFAULT_ACCOUNT_STATE
        self._clock = clock
        self._tick = 0
        self._order_seq = 0
        self._fill_seq = 0
        self._submit_scripts: dict[str, SubmitScript] = {}
        self._cancel_scripts: dict[str, CancelScript] = {}
        self._orders: dict[str, OrderResult] = {}
        self._positions: tuple[PositionInfo, ...] = ()
        self.submitted_requests: list[OrderRequest] = []

    # --- test-side configuration -------------------------------------------

    def script_order(self, client_order_id: str, script: SubmitScript) -> None:
        """Set the scenario applied when ``client_order_id`` is submitted."""
        self._submit_scripts[client_order_id] = script

    def script_cancel(self, client_order_id: str, script: CancelScript) -> None:
        """Set the scenario applied when ``client_order_id`` is cancelled."""
        self._cancel_scripts[client_order_id] = script

    def set_positions(self, positions: tuple[PositionInfo, ...] | list[PositionInfo]) -> None:
        self._positions = tuple(positions)

    def set_account_state(self, state: AccountState) -> None:
        self._account_state = state

    # --- deterministic clock ------------------------------------------------

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        self._tick += 1
        return _CLOCK_BASE + timedelta(seconds=self._tick)

    def _next_broker_order_id(self) -> str:
        self._order_seq += 1
        return f"FAKE-{self._order_seq}"

    def _next_fill_id(self) -> str:
        self._fill_seq += 1
        return f"FILL-{self._fill_seq}"

    # --- Broker interface ----------------------------------------------------

    def submit_order(self, request: OrderRequest) -> OrderResult:
        if not isinstance(request, OrderRequest):
            raise BrokerInvalidRequestError(f"request must be an OrderRequest, got {request!r}")
        if request.client_order_id in self._orders:
            raise BrokerInvalidRequestError(
                f"duplicate client_order_id {request.client_order_id!r}",
                client_order_id=request.client_order_id,
            )
        self.submitted_requests.append(request)
        script = self._submit_scripts.get(request.client_order_id, self._default_script)

        if isinstance(script, FailCommunication):
            raise BrokerCommunicationError(
                "communication failure before the request reached the broker",
                client_order_id=request.client_order_id,
            )

        if isinstance(script, TimeoutOrder):
            self._orders[request.client_order_id] = self._result(
                request,
                state=OrderState.UNKNOWN,
                broker_order_id=None,
                fills=(),
            )
            raise BrokerTimeoutError(
                "timed out waiting for the broker; the order may have been accepted",
                client_order_id=request.client_order_id,
            )

        if isinstance(script, RejectOrder):
            result = self._result(
                request,
                state=OrderState.REJECTED,
                broker_order_id=None,
                fills=(),
                reject_reason=script.reason,
            )
        elif isinstance(script, StayPending):
            result = self._result(
                request,
                state=OrderState.ACCEPTED,
                broker_order_id=self._next_broker_order_id(),
                fills=(),
            )
        elif isinstance(script, PartiallyFill):
            fills = self._build_fills(script.fills)
            total = sum((f.quantity for f in fills), Decimal(0))
            if not fills or total >= request.quantity:
                raise ValueError(
                    f"PartiallyFill script must fill 0 < total < requested quantity "
                    f"({request.quantity}), got {total}"
                )
            result = self._result(
                request,
                state=OrderState.PARTIALLY_FILLED,
                broker_order_id=self._next_broker_order_id(),
                fills=fills,
            )
        elif isinstance(script, FillImmediately):
            price = script.price or request.limit_price or self._default_fill_price
            if price is None:
                raise ValueError(
                    "FillImmediately needs a price: set script.price, use a LIMIT order, "
                    "or configure FakeBroker(default_fill_price=...)"
                )
            fills = (Fill(self._next_fill_id(), price, request.quantity, self._now()),)
            result = self._result(
                request,
                state=OrderState.FILLED,
                broker_order_id=self._next_broker_order_id(),
                fills=fills,
            )
        else:
            raise ValueError(f"unknown submit script {script!r}")

        self._orders[request.client_order_id] = result
        return result

    def cancel_order(self, client_order_id: str) -> OrderResult:
        current = self._orders.get(client_order_id)
        if current is None:
            raise BrokerOrderNotFoundError(
                f"no order under client_order_id {client_order_id!r}",
                client_order_id=client_order_id,
            )
        script = self._cancel_scripts.get(client_order_id)
        if isinstance(script, FailCancellation):
            raise BrokerCancellationError(script.reason, client_order_id=client_order_id)
        if current.state in TERMINAL_ORDER_STATES:
            raise BrokerCancellationError(
                f"cannot cancel order in terminal state {current.state.value!r}",
                client_order_id=client_order_id,
            )
        validate_order_state_transition(current.state, OrderState.CANCELLED)
        cancelled = OrderResult(
            client_order_id=current.client_order_id,
            broker_order_id=current.broker_order_id,
            state=OrderState.CANCELLED,
            symbol=current.symbol,
            side=current.side,
            order_type=current.order_type,
            requested_quantity=current.requested_quantity,
            filled_quantity=current.filled_quantity,
            remaining_quantity=current.remaining_quantity,
            requested_price=current.requested_price,
            average_fill_price=current.average_fill_price,
            updated_at_utc=self._now(),
            fills=current.fills,
        )
        self._orders[client_order_id] = cancelled
        return cancelled

    def get_order(self, client_order_id: str) -> OrderResult:
        result = self._orders.get(client_order_id)
        if result is None:
            raise BrokerOrderNotFoundError(
                f"no order under client_order_id {client_order_id!r}",
                client_order_id=client_order_id,
            )
        return result

    def get_positions(self) -> tuple[PositionInfo, ...]:
        return self._positions

    def get_account_state(self) -> AccountState:
        return self._account_state

    # --- helpers -------------------------------------------------------------

    def _build_fills(self, price_qty_pairs: tuple[tuple[Decimal, Decimal], ...]) -> tuple[Fill, ...]:
        return tuple(
            Fill(self._next_fill_id(), price, quantity, self._now())
            for price, quantity in price_qty_pairs
        )

    def _result(
        self,
        request: OrderRequest,
        *,
        state: OrderState,
        broker_order_id: str | None,
        fills: tuple[Fill, ...],
        reject_reason: str | None = None,
    ) -> OrderResult:
        filled = sum((f.quantity for f in fills), Decimal(0))
        return OrderResult(
            client_order_id=request.client_order_id,
            broker_order_id=broker_order_id,
            state=state,
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            requested_quantity=request.quantity,
            filled_quantity=filled,
            remaining_quantity=request.quantity - filled,
            requested_price=request.limit_price if request.order_type is OrderType.LIMIT else None,
            average_fill_price=weighted_average_price(fills),
            updated_at_utc=self._now(),
            fills=fills,
            reject_reason=reject_reason,
        )
