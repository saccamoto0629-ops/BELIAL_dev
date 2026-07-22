"""Fail-closed pre-submit Safety Gate for future Live Execution (Issue #75).

Nothing in this module connects to a real broker adapter.  It validates an
``ExecutionRequest`` against static policy, current market data, and broker
snapshots before the request may cross the ``Broker.submit_order`` boundary.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Protocol

from broker.broker_interface import Broker
from broker.broker_models import AccountState, BrokerError, OrderResult, OrderSide, PositionInfo
from execution.execution_models import ExecutionRequest

_LOGGER = logging.getLogger(__name__)


class ExecutionMode(enum.Enum):
    PAPER = "paper"
    DEMO = "demo"
    LIVE = "live"


class SafetyRejectionCode(enum.Enum):
    SYMBOL_NOT_ALLOWED = "symbol_not_allowed"
    SIDE_NOT_ALLOWED = "side_not_allowed"
    QUANTITY_LIMIT_EXCEEDED = "quantity_limit_exceeded"
    SYMBOL_POSITION_LIMIT_EXCEEDED = "symbol_position_limit_exceeded"
    ACCOUNT_POSITION_LIMIT_EXCEEDED = "account_position_limit_exceeded"
    STOP_LOSS_REQUIRED = "stop_loss_required"
    STOP_LOSS_INVALID = "stop_loss_invalid"
    TAKE_PROFIT_INVALID = "take_profit_invalid"
    REFERENCE_PRICE_MISSING = "reference_price_missing"
    REFERENCE_PRICE_STALE = "reference_price_stale"
    SPREAD_MISSING = "spread_missing"
    SPREAD_LIMIT_EXCEEDED = "spread_limit_exceeded"
    MARKET_CLOSED = "market_closed"
    SYMBOL_NOT_TRADABLE = "symbol_not_tradable"
    KILL_SWITCH_ENABLED = "kill_switch_enabled"
    BROKER_QUERY_FAILED = "broker_query_failed"
    STATE_QUERY_FAILED = "state_query_failed"
    DECISION_PERSIST_FAILED = "decision_persist_failed"
    EXECUTION_MODE_MISMATCH = "execution_mode_mismatch"


@dataclass(frozen=True)
class MarketSafetySnapshot:
    symbol: str
    reference_price: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    observed_at_utc: datetime
    market_open: bool
    tradable: bool

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        if self.observed_at_utc.tzinfo is None or self.observed_at_utc.utcoffset() is None:
            raise ValueError("observed_at_utc must be timezone-aware")
        if self.observed_at_utc.utcoffset().total_seconds() != 0:
            raise ValueError("observed_at_utc must be UTC")
        for name in ("reference_price", "bid", "ask"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value <= 0):
                raise ValueError(f"{name} must be a positive finite Decimal or None")
        if self.bid is not None and self.ask is not None and self.ask < self.bid:
            raise ValueError("ask must be >= bid")

    @property
    def spread(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


@dataclass(frozen=True)
class SafetyPolicy:
    allowed_symbols: frozenset[str]
    allowed_sides: frozenset[OrderSide]
    max_order_quantity: Decimal
    max_positions_per_symbol: int
    max_positions_account: int
    max_reference_age: timedelta
    max_spread_by_symbol: dict[str, Decimal]
    allowed_connection_targets: dict[ExecutionMode, frozenset[str]]
    require_stop_loss: bool = True

    def __post_init__(self) -> None:
        if not self.allowed_symbols:
            raise ValueError("allowed_symbols must not be empty")
        if not self.allowed_sides:
            raise ValueError("allowed_sides must not be empty")
        if not isinstance(self.max_order_quantity, Decimal) or not self.max_order_quantity.is_finite():
            raise ValueError("max_order_quantity must be a finite Decimal")
        if self.max_order_quantity <= 0:
            raise ValueError("max_order_quantity must be > 0")
        if self.max_positions_per_symbol < 0 or self.max_positions_account < 0:
            raise ValueError("position limits must be >= 0")
        if self.max_reference_age.total_seconds() < 0:
            raise ValueError("max_reference_age must be >= 0")
        for symbol, spread in self.max_spread_by_symbol.items():
            if symbol not in self.allowed_symbols:
                raise ValueError(f"spread limit configured for disallowed symbol {symbol!r}")
            if not isinstance(spread, Decimal) or not spread.is_finite() or spread < 0:
                raise ValueError(f"max spread for {symbol!r} must be a non-negative finite Decimal")


@dataclass(frozen=True)
class SafetyRejection:
    code: SafetyRejectionCode
    message: str


@dataclass(frozen=True)
class SafetyDecision:
    execution_request_id: str
    allowed: bool
    checked_at_utc: datetime
    rejections: tuple[SafetyRejection, ...] = ()
    account_id: str | None = None
    position_count: int | None = None

    def __post_init__(self) -> None:
        if self.allowed == bool(self.rejections):
            raise ValueError("allowed decisions must have no rejections; rejected decisions must have at least one")

    @property
    def rejection_codes(self) -> tuple[str, ...]:
        return tuple(item.code.value for item in self.rejections)


class SafetyStateProvider(Protocol):
    def is_kill_switch_enabled(self) -> bool: ...


class SafetyDecisionRecorder(Protocol):
    def record(self, decision: SafetyDecision) -> None: ...


@dataclass
class StaticSafetyState:
    kill_switch_enabled: bool = False

    def is_kill_switch_enabled(self) -> bool:
        return self.kill_switch_enabled


@dataclass
class InMemorySafetyDecisionRecorder:
    decisions: list[SafetyDecision] = field(default_factory=list)

    def record(self, decision: SafetyDecision) -> None:
        self.decisions.append(decision)


class SafetyGate:
    """Evaluate every pre-submit condition and return all rejection reasons."""

    def __init__(
        self,
        *,
        policy: SafetyPolicy,
        state_provider: SafetyStateProvider,
        clock: Callable[[], datetime],
    ) -> None:
        self._policy = policy
        self._state_provider = state_provider
        self._clock = clock

    def evaluate(
        self,
        request: ExecutionRequest,
        *,
        broker: Broker,
        market: MarketSafetySnapshot,
        execution_mode: ExecutionMode,
        connection_target: str,
    ) -> SafetyDecision:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None or now.utcoffset().total_seconds() != 0:
            raise ValueError("SafetyGate clock must return timezone-aware UTC")

        rejections: list[SafetyRejection] = []

        def reject(code: SafetyRejectionCode, message: str) -> None:
            rejections.append(SafetyRejection(code=code, message=message))

        if request.symbol not in self._policy.allowed_symbols:
            reject(SafetyRejectionCode.SYMBOL_NOT_ALLOWED, f"symbol {request.symbol!r} is not allowed")
        if request.side not in self._policy.allowed_sides:
            reject(SafetyRejectionCode.SIDE_NOT_ALLOWED, f"side {request.side.value!r} is not allowed")
        if request.quantity > self._policy.max_order_quantity:
            reject(
                SafetyRejectionCode.QUANTITY_LIMIT_EXCEEDED,
                f"quantity {request.quantity} exceeds {self._policy.max_order_quantity}",
            )

        if self._policy.require_stop_loss and request.stop_loss_price is None:
            reject(SafetyRejectionCode.STOP_LOSS_REQUIRED, "stop loss is required")
        self._validate_protective_prices(request, market, reject)

        if market.symbol != request.symbol:
            reject(
                SafetyRejectionCode.REFERENCE_PRICE_MISSING,
                f"market snapshot symbol {market.symbol!r} does not match request {request.symbol!r}",
            )
        if market.reference_price is None:
            reject(SafetyRejectionCode.REFERENCE_PRICE_MISSING, "reference price is unavailable")
        if now - market.observed_at_utc > self._policy.max_reference_age:
            reject(
                SafetyRejectionCode.REFERENCE_PRICE_STALE,
                f"reference price age {now - market.observed_at_utc} exceeds {self._policy.max_reference_age}",
            )
        if not market.market_open:
            reject(SafetyRejectionCode.MARKET_CLOSED, "market is closed")
        if not market.tradable:
            reject(SafetyRejectionCode.SYMBOL_NOT_TRADABLE, "symbol is not tradable")

        spread_limit = self._policy.max_spread_by_symbol.get(request.symbol)
        if spread_limit is None or market.spread is None:
            reject(SafetyRejectionCode.SPREAD_MISSING, "spread or spread limit is unavailable")
        elif market.spread > spread_limit:
            reject(
                SafetyRejectionCode.SPREAD_LIMIT_EXCEEDED,
                f"spread {market.spread} exceeds {spread_limit}",
            )

        allowed_targets = self._policy.allowed_connection_targets.get(execution_mode, frozenset())
        if connection_target not in allowed_targets:
            reject(
                SafetyRejectionCode.EXECUTION_MODE_MISMATCH,
                f"connection target {connection_target!r} is not allowed for {execution_mode.value!r}",
            )

        try:
            if self._state_provider.is_kill_switch_enabled():
                reject(SafetyRejectionCode.KILL_SWITCH_ENABLED, "kill switch is enabled")
        except Exception as exc:  # provider may be DB/config backed; fail closed
            reject(SafetyRejectionCode.STATE_QUERY_FAILED, f"safety state query failed: {exc}")

        positions: tuple[PositionInfo, ...] | None = None
        account: AccountState | None = None
        try:
            positions = broker.get_positions()
            account = broker.get_account_state()
        except (BrokerError, Exception) as exc:
            reject(SafetyRejectionCode.BROKER_QUERY_FAILED, f"broker safety query failed: {exc}")

        if positions is not None:
            symbol_count = sum(1 for position in positions if position.symbol == request.symbol)
            if symbol_count >= self._policy.max_positions_per_symbol:
                reject(
                    SafetyRejectionCode.SYMBOL_POSITION_LIMIT_EXCEEDED,
                    f"symbol position count {symbol_count} reached limit {self._policy.max_positions_per_symbol}",
                )
            if len(positions) >= self._policy.max_positions_account:
                reject(
                    SafetyRejectionCode.ACCOUNT_POSITION_LIMIT_EXCEEDED,
                    f"account position count {len(positions)} reached limit {self._policy.max_positions_account}",
                )

        return SafetyDecision(
            execution_request_id=request.execution_request_id,
            allowed=not rejections,
            checked_at_utc=now,
            rejections=tuple(rejections),
            account_id=account.account_id if account is not None else None,
            position_count=len(positions) if positions is not None else None,
        )

    @staticmethod
    def _validate_protective_prices(request, market, reject) -> None:
        price = market.reference_price
        if price is None:
            return
        if request.stop_loss_price is not None:
            invalid_sl = (
                request.stop_loss_price >= price
                if request.side is OrderSide.BUY
                else request.stop_loss_price <= price
            )
            if invalid_sl:
                reject(
                    SafetyRejectionCode.STOP_LOSS_INVALID,
                    f"stop loss {request.stop_loss_price} is not on the losing side of {price}",
                )
        if request.take_profit_price is not None:
            invalid_tp = (
                request.take_profit_price <= price
                if request.side is OrderSide.BUY
                else request.take_profit_price >= price
            )
            if invalid_tp:
                reject(
                    SafetyRejectionCode.TAKE_PROFIT_INVALID,
                    f"take profit {request.take_profit_price} is not on the winning side of {price}",
                )


def submit_after_safety_gate(
    request: ExecutionRequest,
    *,
    client_order_id: str,
    broker: Broker,
    gate: SafetyGate,
    recorder: SafetyDecisionRecorder,
    market: MarketSafetySnapshot,
    execution_mode: ExecutionMode,
    connection_target: str,
    created_at_utc: datetime,
) -> tuple[SafetyDecision, OrderResult | None]:
    """Record a decision and submit only after a positive recorded decision.

    A rejected decision, a state/broker lookup failure, or a decision persistence
    failure returns without calling ``Broker.submit_order``.
    """
    decision = gate.evaluate(
        request,
        broker=broker,
        market=market,
        execution_mode=execution_mode,
        connection_target=connection_target,
    )
    try:
        recorder.record(decision)
    except Exception as exc:
        failed = SafetyDecision(
            execution_request_id=request.execution_request_id,
            allowed=False,
            checked_at_utc=decision.checked_at_utc,
            rejections=(
                SafetyRejection(
                    SafetyRejectionCode.DECISION_PERSIST_FAILED,
                    f"safety decision persistence failed: {exc}",
                ),
            ),
            account_id=decision.account_id,
            position_count=decision.position_count,
        )
        _LOGGER.error(
            "Safety Gate rejected execution_request_id=%s codes=%s",
            request.execution_request_id,
            failed.rejection_codes,
        )
        return failed, None

    if not decision.allowed:
        _LOGGER.warning(
            "Safety Gate rejected execution_request_id=%s codes=%s",
            request.execution_request_id,
            decision.rejection_codes,
        )
        return decision, None

    order_request = request.to_order_request(
        client_order_id=client_order_id,
        created_at_utc=created_at_utc,
    )
    return decision, broker.submit_order(order_request)
