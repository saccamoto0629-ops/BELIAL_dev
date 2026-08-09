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

Order Reissue (Issue #102) is scriptable deterministically:
``PartiallyFillThenReissue`` answers the submit with a partial fill whose
remainder was reissued under a NEW broker order id (original id kept in the
``superseded_broker_order_ids`` audit chain); ``ReissueRejected`` answers
with the partial fill kept and the remainder definitively dead (terminal
``CANCELLED`` on the original id); ``ReissueInformationLost`` mirrors the
fail-closed adapters (``BrokerTimeoutError``, order ``UNKNOWN``). Further
chain steps are driven by ``reissue_remainder()`` / ``fill_remaining()``,
each validated through ``validate_order_result_update``.

Unscripted orders use ``default_script`` (immediate fill by default).
Scenario values and configuration-API inputs (``script_order`` /
``script_cancel`` / ``set_positions`` / ``set_account_state``) are validated
eagerly — a mis-scripted test fails at configuration time with
``BrokerInvalidRequestError``, not later inside submit/cancel.
Timeouts are reproduced immediately as ``BrokerTimeoutError`` — never by
waiting — and leave the order in ``OrderState.UNKNOWN`` (the broker may have
accepted it). A communication failure (``FailCommunication``) means the
request never reached the broker: no order is recorded at all.

Timestamps come from a deterministic injected clock (fixed base time plus one
second per event) unless the test supplies its own ``clock`` callable.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Union

from .broker_models import (
    AccountState,
    BrokerCancellationError,
    BrokerCommunicationError,
    BrokerConnectionIdentity,
    BrokerInvalidRequestError,
    BrokerOrderNotFoundError,
    BrokerTimeoutError,
    ClosedTradeInfo,
    Fill,
    OrderRequest,
    OrderResult,
    OrderState,
    OrderType,
    PositionInfo,
    TERMINAL_ORDER_STATES,
    TradeInfo,
    validate_order_result_update,
    weighted_average_price,
)

# --- scripted scenarios ------------------------------------------------------
#
# Scenario dataclasses validate their own values at CONSTRUCTION time
# (PR #87), so a mis-scripted test fails at the script_order()/dataclass
# call, not later inside submit_order()/cancel_order(). All configuration
# mistakes raise BrokerInvalidRequestError so they are machine-checkable.


def _require_valid_price(value, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise BrokerInvalidRequestError(
            f"{field_name} must be a positive finite Decimal, got {value!r}"
        )


def _require_non_empty_reason(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise BrokerInvalidRequestError(
            f"{field_name} must be a non-empty string, got {value!r}"
        )


def _require_id(client_order_id) -> None:
    if not isinstance(client_order_id, str) or not client_order_id.strip():
        raise BrokerInvalidRequestError(
            f"client_order_id must be a non-empty string, got {client_order_id!r}"
        )


def _require_optional_broker_order_id(value, field_name: str) -> None:
    # Issue #77: scripts that acknowledge an order may pin the broker-assigned
    # ID so scenario expectations compare against a fixed value instead of the
    # FAKE-<n> sequence. None keeps the sequential default.
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise BrokerInvalidRequestError(
            f"{field_name} must be a non-empty string or None, got {value!r}"
        )


@dataclass(frozen=True)
class FillImmediately:
    """Fill the whole order at ``price`` (falls back to the request's limit
    price, then the broker's ``default_fill_price``). ``broker_order_id``
    optionally pins the broker-assigned ID (default: next ``FAKE-<n>``)."""

    price: Decimal | None = None
    broker_order_id: str | None = None

    def __post_init__(self) -> None:
        if self.price is not None:
            _require_valid_price(self.price, "FillImmediately.price")
        _require_optional_broker_order_id(self.broker_order_id, "FillImmediately.broker_order_id")


@dataclass(frozen=True)
class StayPending:
    """Broker accepts the order but nothing fills (state ``ACCEPTED``).
    ``broker_order_id`` optionally pins the broker-assigned ID."""

    broker_order_id: str | None = None

    def __post_init__(self) -> None:
        _require_optional_broker_order_id(self.broker_order_id, "StayPending.broker_order_id")


@dataclass(frozen=True)
class PartiallyFill:
    """Fill only ``fills`` — a non-empty tuple of (price, quantity) pairs.

    Each price/quantity must be a positive finite Decimal. The comparison
    against the ORDER quantity happens at submit time: the total must be
    strictly less than the requested quantity (use ``FillImmediately`` for a
    full fill). ``broker_order_id`` optionally pins the broker-assigned ID."""

    fills: tuple[tuple[Decimal, Decimal], ...]
    broker_order_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fills, tuple) or not self.fills:
            raise BrokerInvalidRequestError(
                f"PartiallyFill.fills must be a non-empty tuple, got {self.fills!r}"
            )
        for entry in self.fills:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise BrokerInvalidRequestError(
                    f"each PartiallyFill.fills entry must be a (price, quantity) tuple, got {entry!r}"
                )
            price, quantity = entry
            _require_valid_price(price, "PartiallyFill price")
            _require_valid_price(quantity, "PartiallyFill quantity")
        _require_optional_broker_order_id(self.broker_order_id, "PartiallyFill.broker_order_id")


@dataclass(frozen=True)
class PartiallyFillThenReissue:
    """Partial fill, then the broker REISSUES the remaining quantity as a new
    broker order under a NEW broker order id (Issue #102, OANDA-style
    lifecycle). The submit snapshot is ``PARTIALLY_FILLED`` with
    ``broker_order_id`` set to the reissued (current) id and the original id
    recorded in ``superseded_broker_order_ids`` — mirroring a real submit
    response that carries both the fill and the reissue transaction.

    ``broker_order_id`` pins the ORIGINAL id, ``reissued_broker_order_id``
    pins the NEW current id (each defaults to the next ``FAKE-<n>``). Use
    :meth:`FakeBroker.reissue_remainder` / :meth:`FakeBroker.fill_remaining`
    for further chain steps after the submit."""

    fills: tuple[tuple[Decimal, Decimal], ...]
    broker_order_id: str | None = None
    reissued_broker_order_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fills, tuple) or not self.fills:
            raise BrokerInvalidRequestError(
                f"PartiallyFillThenReissue.fills must be a non-empty tuple, got {self.fills!r}"
            )
        for entry in self.fills:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise BrokerInvalidRequestError(
                    "each PartiallyFillThenReissue.fills entry must be a "
                    f"(price, quantity) tuple, got {entry!r}"
                )
            price, quantity = entry
            _require_valid_price(price, "PartiallyFillThenReissue price")
            _require_valid_price(quantity, "PartiallyFillThenReissue quantity")
        _require_optional_broker_order_id(
            self.broker_order_id, "PartiallyFillThenReissue.broker_order_id"
        )
        _require_optional_broker_order_id(
            self.reissued_broker_order_id, "PartiallyFillThenReissue.reissued_broker_order_id"
        )
        if (
            self.broker_order_id is not None
            and self.broker_order_id == self.reissued_broker_order_id
        ):
            raise BrokerInvalidRequestError(
                "PartiallyFillThenReissue requires distinct original and reissued "
                f"broker order ids, got {self.broker_order_id!r} twice"
            )


@dataclass(frozen=True)
class ReissueRejected:
    """Partial fill, then the broker REJECTS the reissue of the remaining
    quantity (Issue #102, ``orderReissueRejectTransaction`` analogue): the
    partial fills stand, the remainder is definitively dead, so the submit
    snapshot is terminal ``CANCELLED`` with the fills kept and the ORIGINAL
    broker order id (no new order was ever created, so the chain stays
    empty). ``broker_order_id`` optionally pins the original id."""

    fills: tuple[tuple[Decimal, Decimal], ...]
    broker_order_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fills, tuple) or not self.fills:
            raise BrokerInvalidRequestError(
                f"ReissueRejected.fills must be a non-empty tuple, got {self.fills!r}"
            )
        for entry in self.fills:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise BrokerInvalidRequestError(
                    f"each ReissueRejected.fills entry must be a (price, quantity) tuple, got {entry!r}"
                )
            price, quantity = entry
            _require_valid_price(price, "ReissueRejected price")
            _require_valid_price(quantity, "ReissueRejected quantity")
        _require_optional_broker_order_id(self.broker_order_id, "ReissueRejected.broker_order_id")


@dataclass(frozen=True)
class ReissueInformationLost:
    """The broker's answer indicates a reissue but the information needed to
    interpret it is missing / malformed / uncorrelated (Issue #102): the
    outcome must not be guessed, so submit raises ``BrokerTimeoutError`` and
    records the order as ``UNKNOWN`` — resolved by a later query, never by
    resending (mirrors the adapters' fail-closed contract)."""


@dataclass(frozen=True)
class RejectOrder:
    """Broker definitively rejects the order (state ``REJECTED``)."""

    reason: str = "rejected by broker"

    def __post_init__(self) -> None:
        _require_non_empty_reason(self.reason, "RejectOrder.reason")


@dataclass(frozen=True)
class TimeoutOrder:
    """Raise ``BrokerTimeoutError`` immediately; the order is recorded with
    state ``UNKNOWN`` because the broker may have accepted it."""


@dataclass(frozen=True)
class FailCommunication:
    """Raise ``BrokerCommunicationError`` immediately; the request never
    reached the broker, so no order is recorded."""


SubmitScript = Union[
    FillImmediately,
    StayPending,
    PartiallyFill,
    PartiallyFillThenReissue,
    ReissueRejected,
    ReissueInformationLost,
    RejectOrder,
    TimeoutOrder,
    FailCommunication,
]

_SUBMIT_SCRIPT_TYPES = (
    FillImmediately,
    StayPending,
    PartiallyFill,
    PartiallyFillThenReissue,
    ReissueRejected,
    ReissueInformationLost,
    RejectOrder,
    TimeoutOrder,
    FailCommunication,
)


@dataclass(frozen=True)
class FailCancellation:
    """Broker refuses the cancel (``BrokerCancellationError``); order state
    is left unchanged."""

    reason: str = "cancellation failed"

    def __post_init__(self) -> None:
        _require_non_empty_reason(self.reason, "FailCancellation.reason")


CancelScript = FailCancellation

_CANCEL_SCRIPT_TYPES = (FailCancellation,)

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
        connection_target: str = "fake",
    ):
        if default_script is not None and not isinstance(default_script, _SUBMIT_SCRIPT_TYPES):
            raise BrokerInvalidRequestError(f"invalid default_script {default_script!r}")
        if default_fill_price is not None:
            _require_valid_price(default_fill_price, "default_fill_price")
        if account_state is not None and not isinstance(account_state, AccountState):
            raise BrokerInvalidRequestError(f"invalid account_state {account_state!r}")
        if clock is not None and not callable(clock):
            raise BrokerInvalidRequestError(
                f"clock must be a callable returning a datetime, got {clock!r}"
            )
        # Connection identity (Issue #75): fixed at construction, constant for
        # the instance lifetime. FakeBroker's mode is ALWAYS "fake"; the
        # target names the fake endpoint this instance pretends to be
        # (construction-time config, mirroring a real adapter's endpoint URL).
        self._connection_identity = BrokerConnectionIdentity(
            mode="fake", target=connection_target
        )
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
        # Issue #105: pinned open/closed trade snapshots for position /
        # trade / SL / TP reconciliation tests. Like positions, they are
        # static test fixtures — never derived from submitted orders.
        self._open_trades: tuple[TradeInfo, ...] = ()
        self._closed_trades: tuple[ClosedTradeInfo, ...] = ()
        self.submitted_requests: list[OrderRequest] = []
        # Issue #77: every cancel ATTEMPT is recorded (like submitted_requests
        # records every submit attempt), so tests can assert call counts even
        # for cancels that fail or target unknown orders.
        self.cancel_requests: list[str] = []
        # Issue #77 review r1: broker order IDs are unique ACROSS ORDERS,
        # matching the execution store's DB-wide UNIQUE contract. Pinned and
        # sequential IDs share this one assignment set; an ID enters it only
        # together with its stored order, so a rejected submit leaves nothing
        # behind. submit/cancel run under one lock, making sequencing,
        # duplicate checks, order storage and call recording atomic for
        # concurrency tests.
        self._assigned_broker_order_ids: set[str] = set()
        self._lock = threading.Lock()

    # --- test-side configuration -------------------------------------------
    #
    # Configuration mistakes fail HERE, not later at submit/cancel time, and
    # always as BrokerInvalidRequestError (PR #87).

    def script_order(self, client_order_id: str, script: SubmitScript) -> None:
        """Set the scenario applied when ``client_order_id`` is submitted."""
        _require_id(client_order_id)
        if not isinstance(script, _SUBMIT_SCRIPT_TYPES):
            raise BrokerInvalidRequestError(
                f"script must be one of {[t.__name__ for t in _SUBMIT_SCRIPT_TYPES]}, got {script!r}"
            )
        self._submit_scripts[client_order_id] = script

    def script_cancel(self, client_order_id: str, script: CancelScript) -> None:
        """Set the scenario applied when ``client_order_id`` is cancelled."""
        _require_id(client_order_id)
        if not isinstance(script, _CANCEL_SCRIPT_TYPES):
            raise BrokerInvalidRequestError(
                f"script must be one of {[t.__name__ for t in _CANCEL_SCRIPT_TYPES]}, got {script!r}"
            )
        self._cancel_scripts[client_order_id] = script

    def set_positions(self, positions: tuple[PositionInfo, ...] | list[PositionInfo]) -> None:
        if not isinstance(positions, (tuple, list)):
            raise BrokerInvalidRequestError(
                f"positions must be a tuple or list, got {type(positions).__name__}"
            )
        for entry in positions:
            if not isinstance(entry, PositionInfo):
                raise BrokerInvalidRequestError(
                    f"every positions entry must be a PositionInfo, got {entry!r}"
                )
        self._positions = tuple(positions)

    def set_open_trades(self, trades: tuple[TradeInfo, ...] | list[TradeInfo]) -> None:
        """Pin the open-trade snapshot returned by ``get_open_trades``.

        Issue #105: duplicates are deliberately NOT rejected here — the
        reconciliation service must handle an ambiguous broker enumeration
        fail-closed, and tests need to inject one to prove it.
        """
        if not isinstance(trades, (tuple, list)):
            raise BrokerInvalidRequestError(
                f"trades must be a tuple or list, got {type(trades).__name__}"
            )
        for entry in trades:
            if not isinstance(entry, TradeInfo):
                raise BrokerInvalidRequestError(
                    f"every trades entry must be a TradeInfo, got {entry!r}"
                )
        self._open_trades = tuple(trades)

    def set_closed_trades(
        self, trades: tuple[ClosedTradeInfo, ...] | list[ClosedTradeInfo]
    ) -> None:
        """Pin the closed-trade snapshot returned by ``get_closed_trades``
        (Issue #105; duplicate injection allowed, see ``set_open_trades``)."""
        if not isinstance(trades, (tuple, list)):
            raise BrokerInvalidRequestError(
                f"trades must be a tuple or list, got {type(trades).__name__}"
            )
        for entry in trades:
            if not isinstance(entry, ClosedTradeInfo):
                raise BrokerInvalidRequestError(
                    f"every trades entry must be a ClosedTradeInfo, got {entry!r}"
                )
        self._closed_trades = tuple(trades)

    def set_account_state(self, state: AccountState) -> None:
        if not isinstance(state, AccountState):
            raise BrokerInvalidRequestError(
                f"state must be an AccountState, got {state!r}"
            )
        self._account_state = state

    # --- deterministic clock ------------------------------------------------

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        self._tick += 1
        return _CLOCK_BASE + timedelta(seconds=self._tick)

    def _next_broker_order_id(self) -> str:
        # Sequential IDs share the uniqueness set with pinned IDs: when a
        # pinned ID already took FAKE-<n>, the sequence safely skips ahead to
        # the next free number instead of colliding.
        while True:
            self._order_seq += 1
            candidate = f"FAKE-{self._order_seq}"
            if candidate not in self._assigned_broker_order_ids:
                return candidate

    def _next_fill_id(self) -> str:
        self._fill_seq += 1
        return f"FILL-{self._fill_seq}"

    # --- Broker interface ----------------------------------------------------

    def submit_order(self, request: OrderRequest) -> OrderResult:
        if not isinstance(request, OrderRequest):
            raise BrokerInvalidRequestError(f"request must be an OrderRequest, got {request!r}")
        # One lock around duplicate checks, ID assignment, order storage and
        # call recording: concurrent submitters can never interleave between
        # "check the ID is free" and "store the order under it".
        with self._lock:
            return self._submit_order_locked(request)

    def _submit_order_locked(self, request: OrderRequest) -> OrderResult:
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

        if isinstance(script, ReissueInformationLost):
            # Issue #102 fail-closed analogue: the answer indicated a reissue
            # but could not be interpreted — nothing about the outcome may be
            # guessed, so the order is recorded UNKNOWN (the broker may hold
            # it, may have partially filled it) and the caller resolves it by
            # a later query, never by resending.
            self._orders[request.client_order_id] = self._result(
                request,
                state=OrderState.UNKNOWN,
                broker_order_id=None,
                fills=(),
            )
            raise BrokerTimeoutError(
                "broker response carried reissue information that could not be "
                "interpreted; the outcome is unknown and is resolved by a later "
                "query, never by resending",
                client_order_id=request.client_order_id,
            )

        # Rejection atomicity (PR #96 review r2): every branch below runs in
        # the same strict order -- (1) side-effect-free validation, (2)
        # broker_order_id resolution incl. the duplicate check, (3) fill /
        # timestamp generation, (4) result construction, (5) registration.
        # A submit rejected at (1) or (2) therefore consumes NO fill IDs, NO
        # clock ticks and NO sequence numbers -- the failed order is
        # observable only in submitted_requests (the attempt record).
        if isinstance(script, RejectOrder):
            result = self._result(
                request,
                state=OrderState.REJECTED,
                broker_order_id=None,
                fills=(),
                reject_reason=script.reason,
            )
        elif isinstance(script, StayPending):
            broker_order_id = self._resolve_broker_order_id(script, request.client_order_id)
            result = self._result(
                request,
                state=OrderState.ACCEPTED,
                broker_order_id=broker_order_id,
                fills=(),
            )
        elif isinstance(script, PartiallyFill):
            total = sum((quantity for _, quantity in script.fills), Decimal(0))
            if not (0 < total < request.quantity):
                raise BrokerInvalidRequestError(
                    f"PartiallyFill script must fill 0 < total < requested quantity "
                    f"({request.quantity}), got {total}",
                    client_order_id=request.client_order_id,
                )
            broker_order_id = self._resolve_broker_order_id(script, request.client_order_id)
            fills = self._build_fills(script.fills)
            result = self._result(
                request,
                state=OrderState.PARTIALLY_FILLED,
                broker_order_id=broker_order_id,
                fills=fills,
            )
        elif isinstance(script, PartiallyFillThenReissue):
            total = sum((quantity for _, quantity in script.fills), Decimal(0))
            if not (0 < total < request.quantity):
                raise BrokerInvalidRequestError(
                    f"PartiallyFillThenReissue script must fill 0 < total < requested "
                    f"quantity ({request.quantity}), got {total}",
                    client_order_id=request.client_order_id,
                )
            # Both ids are resolved (duplicate check included) BEFORE any
            # fill / timestamp side effect, keeping rejection atomicity.
            original_id, reissued_id = self._resolve_reissue_ids(
                script.broker_order_id,
                script.reissued_broker_order_id,
                request.client_order_id,
            )
            fills = self._build_fills(script.fills)
            # The submit answer already reflects the reissue (mirroring a
            # broker response carrying the fill AND the reissue transaction):
            # the CURRENT actionable id is the reissued one, the original id
            # is auditable in the superseded chain (Issue #102).
            result = self._result(
                request,
                state=OrderState.PARTIALLY_FILLED,
                broker_order_id=reissued_id,
                fills=fills,
                superseded_broker_order_ids=(original_id,),
            )
        elif isinstance(script, ReissueRejected):
            total = sum((quantity for _, quantity in script.fills), Decimal(0))
            if not (0 < total < request.quantity):
                raise BrokerInvalidRequestError(
                    f"ReissueRejected script must fill 0 < total < requested quantity "
                    f"({request.quantity}), got {total}",
                    client_order_id=request.client_order_id,
                )
            broker_order_id = self._resolve_broker_order_id(script, request.client_order_id)
            fills = self._build_fills(script.fills)
            # The reissue of the remainder was definitively rejected: the
            # partial fills stand and the remainder is dead — terminal
            # CANCELLED on the ORIGINAL id, no chain (no new order exists).
            result = self._result(
                request,
                state=OrderState.CANCELLED,
                broker_order_id=broker_order_id,
                fills=fills,
            )
        elif isinstance(script, FillImmediately):
            # Explicit None checks — never truthiness, which would silently
            # skip a (mis-)configured Decimal("0") to a fallback price (PR #87).
            if script.price is not None:
                price = script.price
            elif request.limit_price is not None:
                price = request.limit_price
            else:
                price = self._default_fill_price
            if price is None:
                raise BrokerInvalidRequestError(
                    "FillImmediately needs a price: set script.price, use a LIMIT order, "
                    "or configure FakeBroker(default_fill_price=...)",
                    client_order_id=request.client_order_id,
                )
            broker_order_id = self._resolve_broker_order_id(script, request.client_order_id)
            fills = (Fill(self._next_fill_id(), price, request.quantity, self._now()),)
            result = self._result(
                request,
                state=OrderState.FILLED,
                broker_order_id=broker_order_id,
                fills=fills,
            )
        else:  # unreachable while script_order()/__init__ validate script types
            raise BrokerInvalidRequestError(f"unknown submit script {script!r}")

        # The IDs join the assignment set only together with their stored
        # order, so a submit rejected above leaves neither an order nor a
        # burned ID. Superseded (reissued-away) ids stay in the set too:
        # broker order ids are unique across orders INCLUDING past chain ids
        # (Issue #102 audit contract).
        self._assigned_broker_order_ids.update(result.all_broker_order_ids)
        self._orders[request.client_order_id] = result
        return result

    def cancel_order(self, client_order_id: str) -> OrderResult:
        with self._lock:
            return self._cancel_order_locked(client_order_id)

    def _cancel_order_locked(self, client_order_id: str) -> OrderResult:
        self.cancel_requests.append(client_order_id)
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
        cancelled = OrderResult(
            client_order_id=current.client_order_id,
            # Cancellation targets the CURRENT actionable broker order and
            # keeps the audit chain (Issue #102): a reissued order cancels
            # under its reissued id with the superseded ids preserved.
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
            superseded_broker_order_ids=current.superseded_broker_order_ids,
        )
        # Full update validation (state pair + content invariants, PR #87)
        validate_order_result_update(current, cancelled)
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

    # --- deterministic post-submit lifecycle (Issue #102) --------------------
    #
    # Test-side drivers that advance an order AFTER its submit — no network,
    # no sleep, no randomness. Every step goes through the full
    # validate_order_result_update contract, so FakeBroker can never record a
    # lifecycle the common state machine would reject.

    def reissue_remainder(
        self,
        client_order_id: str,
        *,
        fill_price: Decimal,
        fill_quantity: Decimal,
        reissued_broker_order_id: str | None = None,
    ) -> OrderResult:
        """A further partial fill on the CURRENT broker order, whose still
        remaining quantity the broker reissues under a NEW broker order id
        (Issue #102 chain step: PARTIALLY_FILLED -> PARTIALLY_FILLED with an
        audited id change). Requires a working, broker-acknowledged order and
        ``fill_quantity`` strictly below the remaining quantity."""
        _require_id(client_order_id)
        _require_valid_price(fill_price, "fill_price")
        _require_valid_price(fill_quantity, "fill_quantity")
        _require_optional_broker_order_id(reissued_broker_order_id, "reissued_broker_order_id")
        with self._lock:
            current = self._require_working_order(client_order_id)
            if fill_quantity >= current.remaining_quantity:
                raise BrokerInvalidRequestError(
                    f"reissue_remainder must leave a remainder: fill_quantity "
                    f"{fill_quantity} >= remaining {current.remaining_quantity} "
                    "(use fill_remaining for the final fill)",
                    client_order_id=client_order_id,
                )
            new_id = self._resolve_pinned_or_next(reissued_broker_order_id, client_order_id)
            fill = Fill(self._next_fill_id(), fill_price, fill_quantity, self._now())
            updated = self._advanced(
                current,
                state=OrderState.PARTIALLY_FILLED,
                broker_order_id=new_id,
                superseded=current.superseded_broker_order_ids + (current.broker_order_id,),
                new_fill=fill,
            )
            self._assigned_broker_order_ids.add(new_id)
            self._orders[client_order_id] = updated
            return updated

    def fill_remaining(self, client_order_id: str, *, price: Decimal) -> OrderResult:
        """Fill the whole remaining quantity of a working order at ``price``
        (Issue #102 final-fill step: the CURRENT broker order — reissued or
        not — completes, keeping the audit chain)."""
        _require_id(client_order_id)
        _require_valid_price(price, "price")
        with self._lock:
            current = self._require_working_order(client_order_id)
            fill = Fill(self._next_fill_id(), price, current.remaining_quantity, self._now())
            updated = self._advanced(
                current,
                state=OrderState.FILLED,
                broker_order_id=current.broker_order_id,
                superseded=current.superseded_broker_order_ids,
                new_fill=fill,
            )
            self._orders[client_order_id] = updated
            return updated

    def _require_working_order(self, client_order_id: str) -> OrderResult:
        current = self._orders.get(client_order_id)
        if current is None:
            raise BrokerOrderNotFoundError(
                f"no order under client_order_id {client_order_id!r}",
                client_order_id=client_order_id,
            )
        if current.state not in (OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED):
            raise BrokerInvalidRequestError(
                f"order under client_order_id {client_order_id!r} is not working "
                f"(state {current.state.value!r}); only accepted / partially_filled "
                "orders can take further fills or reissues",
                client_order_id=client_order_id,
            )
        return current

    def _advanced(
        self,
        current: OrderResult,
        *,
        state: OrderState,
        broker_order_id: str,
        superseded: tuple[str, ...],
        new_fill: Fill,
    ) -> OrderResult:
        fills = current.fills + (new_fill,)
        filled = sum((f.quantity for f in fills), Decimal(0))
        updated = OrderResult(
            client_order_id=current.client_order_id,
            broker_order_id=broker_order_id,
            state=state,
            symbol=current.symbol,
            side=current.side,
            order_type=current.order_type,
            requested_quantity=current.requested_quantity,
            filled_quantity=filled,
            remaining_quantity=current.requested_quantity - filled,
            requested_price=current.requested_price,
            average_fill_price=weighted_average_price(fills),
            updated_at_utc=self._now(),
            fills=fills,
            superseded_broker_order_ids=superseded,
        )
        # Full update validation (state pair + content invariants, PR #87 /
        # Issue #102 chain-append rule) — same contract as cancel_order.
        validate_order_result_update(current, updated)
        return updated

    def list_orders(self) -> tuple[OrderResult, ...]:
        """Every order this broker currently holds, ordered by
        ``client_order_id`` (Issue #103, ``OrderListingBroker``).

        Read-only enumeration of the orders ALREADY recorded by
        ``submit_order`` / ``cancel_order`` -- it places nothing, changes
        nothing, and adds no new scripting surface. Reconciliation uses it to
        observe orders that exist only on the broker side; the deterministic
        ordering keeps assertions stable. Orders whose submit raised
        ``FailCommunication`` were never recorded and therefore never appear.
        """
        with self._lock:
            return tuple(self._orders[key] for key in sorted(self._orders))

    def get_positions(self) -> tuple[PositionInfo, ...]:
        return self._positions

    def get_open_trades(self) -> tuple[TradeInfo, ...]:
        """The pinned open-trade snapshot (Issue #105,
        ``TradeListingBroker`` capability). Read-only."""
        return self._open_trades

    def get_closed_trades(self) -> tuple[ClosedTradeInfo, ...]:
        """The pinned closed-trade snapshot (Issue #105,
        ``ClosedTradeListingBroker`` capability). Read-only."""
        return self._closed_trades

    def get_account_state(self) -> AccountState:
        return self._account_state

    def connection_identity(self) -> BrokerConnectionIdentity:
        """The identity this instance was CONSTRUCTED with (Issue #75).

        Not part of the five Issue #73 order operations: it is the
        connection-identity contract the execution layer's typed
        ``BrokerConnection`` requires from every broker implementation.
        """
        return self._connection_identity

    # --- helpers -------------------------------------------------------------

    def _resolve_broker_order_id(self, script, client_order_id: str) -> str:
        """Resolve the ID an accepting branch WILL use, duplicate check
        included, BEFORE any fill / timestamp / sequence side effect: a
        pinned duplicate raises here having consumed nothing (r2 atomicity);
        an unpinned script advances the sequence only after every validation
        that could still reject the order has passed."""
        return self._resolve_pinned_or_next(script.broker_order_id, client_order_id)

    def _resolve_pinned_or_next(
        self, pinned: str | None, client_order_id: str, *, taken: set[str] | None = None
    ) -> str:
        """Resolve one broker order id — a pinned value (duplicate-checked
        against every id ever assigned, chain ids included, plus the ids
        ``taken`` earlier in the same call) or the next free ``FAKE-<n>``."""
        reserved = self._assigned_broker_order_ids if not taken else (
            self._assigned_broker_order_ids | taken
        )
        if pinned is not None:
            if pinned in reserved:
                raise BrokerInvalidRequestError(
                    f"broker_order_id {pinned!r} is already assigned to "
                    "another order; broker order IDs are unique across orders",
                    client_order_id=client_order_id,
                )
            return pinned
        while True:
            candidate = self._next_broker_order_id()
            if candidate not in reserved:
                return candidate

    def _resolve_reissue_ids(
        self, original_pin: str | None, reissued_pin: str | None, client_order_id: str
    ) -> tuple[str, str]:
        """Resolve BOTH ids of a reissue script — every pinned id is
        duplicate-checked BEFORE any sequence number is consumed (PR #112
        review r3): a submit rejected over either pin burns no ``FAKE-<n>``,
        keeping the rejection-atomicity contract. Sequential ids then skip
        the pins, so an unpinned original can never collide with a pinned
        reissued id either."""
        pins = {pin for pin in (original_pin, reissued_pin) if pin is not None}
        for pinned in pins:
            if pinned in self._assigned_broker_order_ids:
                raise BrokerInvalidRequestError(
                    f"broker_order_id {pinned!r} is already assigned to "
                    "another order; broker order IDs are unique across orders",
                    client_order_id=client_order_id,
                )
        if original_pin is not None:
            original_id = original_pin
        else:
            original_id = self._resolve_pinned_or_next(None, client_order_id, taken=pins)
        if reissued_pin is not None:
            reissued_id = reissued_pin
        else:
            reissued_id = self._resolve_pinned_or_next(
                None, client_order_id, taken=pins | {original_id}
            )
        return original_id, reissued_id

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
        superseded_broker_order_ids: tuple[str, ...] = (),
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
            superseded_broker_order_ids=superseded_broker_order_ids,
        )
