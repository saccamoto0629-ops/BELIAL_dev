"""Shared builders for the Issue #78 shadow-mode tests (NOT a test module)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from broker.broker_models import AccountState, OrderSide, OrderType, PositionInfo, PositionSide
from execution.execution_models import ExecutionRequest
from execution.safety_gate import (
    AccountSafetySnapshot,
    ExecutionMode,
    MarketSafetySnapshot,
    SafetyGate,
    SafetyPolicy,
    StaticSafetyState,
)
from execution.shadow_execution import ShadowExecutionService

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_policy(**overrides) -> SafetyPolicy:
    values = dict(
        policy_id="shadow-test-policy",
        allowed_symbols=frozenset({"USD_JPY"}),
        allowed_sides=frozenset({OrderSide.BUY, OrderSide.SELL}),
        max_order_quantity=Decimal("10"),
        max_positions_per_symbol=2,
        max_positions_account=5,
        max_reference_age=timedelta(seconds=30),
        max_spread_by_symbol={"USD_JPY": Decimal("0.05")},
        allowed_connection_targets={ExecutionMode.SHADOW: frozenset({"shadow"})},
        require_stop_loss=True,
    )
    values.update(overrides)
    return SafetyPolicy(**values)


def make_gate(policy=None, *, kill_switch=False, clock=None) -> SafetyGate:
    return SafetyGate(
        policy=policy if policy is not None else make_policy(),
        state_provider=StaticSafetyState(kill_switch_enabled=kill_switch),
        clock=clock if clock is not None else (lambda: NOW),
    )


def make_request(**overrides) -> ExecutionRequest:
    values = dict(
        execution_request_id="exec-1",
        signal_id="signal-1",
        strategy_id="strategy-1",
        symbol="USD_JPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        requested_at_utc=NOW,
        requested_price=None,
        stop_loss_price=Decimal("149.50"),
        take_profit_price=Decimal("151.50"),
    )
    values.update(overrides)
    return ExecutionRequest(**values)


def make_market(**overrides) -> MarketSafetySnapshot:
    values = dict(
        symbol="USD_JPY",
        reference_price=Decimal("150"),
        bid=Decimal("149.99"),
        ask=Decimal("150.01"),
        observed_at_utc=NOW,
        market_open=True,
        tradable=True,
    )
    values.update(overrides)
    return MarketSafetySnapshot(**values)


def make_account(*, positions=(), retrieved_at_utc=NOW, account_id="acct-1") -> AccountSafetySnapshot:
    return AccountSafetySnapshot(
        positions=tuple(positions),
        account=AccountState(
            account_id=account_id,
            currency="JPY",
            balance=Decimal("1000"),
            equity=Decimal("1000"),
            margin_available=Decimal("1000"),
            retrieved_at_utc=retrieved_at_utc,
        ),
    )


def make_position(symbol="USD_JPY", retrieved_at_utc=NOW) -> PositionInfo:
    return PositionInfo(
        symbol=symbol,
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        average_entry_price=Decimal("150"),
        retrieved_at_utc=retrieved_at_utc,
    )


def make_service(tmp_path, *, gate=None, initialize=True, clock=None) -> ShadowExecutionService:
    service = ShadowExecutionService(
        gate=gate if gate is not None else make_gate(),
        decision_db_path=tmp_path / "safety.sqlite3",
        shadow_db_path=tmp_path / "shadow.sqlite3",
        clock=clock if clock is not None else (lambda: NOW),
    )
    if initialize:
        service.initialize_stores()
    return service
