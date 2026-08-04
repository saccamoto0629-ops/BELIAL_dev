"""Production composition root for Shadow Mode (Issue #78 / PR #100 review).

THIS is the official "real signal path": a STORED signal (resolved through a
:class:`~execution.shadow_providers.StoredSignalProvider`), the production
execution configuration (:class:`~execution.shadow_config.ShadowExecutionConfig`)
and provider-supplied market / account snapshots are composed into ONE shadow
cycle through the production services:

    signal_id
      -> StoredSignalProvider.get_signal()          (missing -> explicit error)
      -> ShadowExecutionConfig.order_quantity_for() (unconfigured -> error)
      -> build_execution_request_from_signal()      (deterministic identity)
      -> ShadowExecutionService.run_cycle()         (claim -> snapshots -> gate -> record)
           where the MarketSnapshotProvider / AccountSnapshotProvider lookups
           run only AFTER the claim is won (missing -> error): a duplicate
           loser performs no snapshot-provider I/O at all.

No permissive defaults exist anywhere on this path: every input either
resolves explicitly or fails with :class:`ShadowInputError` /
:class:`ShadowConfigError`. The resulting record carries
``input_origin="production_signal"`` plus the provider identifiers, the
configuration version and the signal's own timestamp -- fixtures
(``cli_fixture`` / ``scenario_fixture``) are labelled differently and can
never masquerade as the production path.

Deterministic request identity (idempotency across retries): the
``execution_request_id`` / ``client_order_id`` derive from the signal id and
``requested_at_utc`` is the SIGNAL's timestamp, so re-running the same stored
signal claims the same canonical payload -- a crash-retry classifies as
duplicate/recovery, never as a fresh cycle or a conflict.

Stored Paper Signals carry NO price levels (no entry / stop-loss /
take-profit columns), so production shadow requests are MARKET orders without
protective prices; whether that passes the gate is the configuration's
explicit ``require_stop_loss`` decision. Deriving protective prices or order
sizing beyond the configured per-symbol quantity is Live-Execution-scope work
and is deliberately absent here.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable

from broker.broker_models import OrderSide, OrderType
from execution.execution_models import ExecutionRequest
from execution.safety_gate import SafetyGate, StaticSafetyState
from execution.shadow_config import ShadowExecutionConfig
from execution.shadow_execution import ShadowCycleOutcome, ShadowExecutionService
from execution.shadow_models import INPUT_ORIGIN_PRODUCTION_SIGNAL, ShadowProvenance
from execution.shadow_providers import (
    AccountSnapshotProvider,
    MarketSnapshotProvider,
    ShadowInputError,
    SIGNAL_SIDE_LONG,
    SIGNAL_SIDE_SHORT,
    StoredSignal,
    StoredSignalProvider,
    require_executable_signal_status,
)

# Deterministic identity derivation (see the module docstring). The prefixes
# keep the production-shadow namespace visibly distinct from hand-written
# fixture ids.
EXECUTION_REQUEST_ID_PREFIX = "sigreq-"
CLIENT_ORDER_ID_PREFIX = "sigord-"

_SIDE_BY_SIGNAL_SIDE = {
    SIGNAL_SIDE_LONG: OrderSide.BUY,
    SIGNAL_SIDE_SHORT: OrderSide.SELL,
}


def build_execution_request_from_signal(
    signal: StoredSignal, *, quantity: Decimal
) -> tuple[ExecutionRequest, str]:
    """The production Signal -> ExecutionRequest service: maps the stored
    signal's vocabulary onto the execution intent, deterministically.

    Returns ``(request, client_order_id)``. The quantity comes from the
    production configuration (`ShadowExecutionConfig.order_quantity_for`),
    never from a shadow-side sizing calculation.
    """
    if not isinstance(signal, StoredSignal):
        raise ShadowInputError(f"signal must be a StoredSignal, got {signal!r}")
    # Belt and braces on top of the provider-side gate (PR #100 re-review
    # Major 3): the production request builder itself refuses non-executable
    # statuses, so a custom provider cannot smuggle one past the closed set.
    require_executable_signal_status(signal.signal_id, signal.status)
    side = _SIDE_BY_SIGNAL_SIDE.get(signal.side)
    if side is None:
        raise ShadowInputError(
            f"stored signal {signal.signal_id!r} carries unmappable side {signal.side!r}"
        )
    try:
        request = ExecutionRequest(
            execution_request_id=f"{EXECUTION_REQUEST_ID_PREFIX}{signal.signal_id}",
            signal_id=signal.signal_id,
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            side=side,
            order_type=OrderType.MARKET,
            quantity=quantity,
            requested_at_utc=signal.created_at_utc,
            # Stored Paper Signals carry no price levels (module docstring):
            # no requested price, no protective prices -- nothing is invented.
            requested_price=None,
            stop_loss_price=None,
            take_profit_price=None,
        )
    except ValueError as exc:
        raise ShadowInputError(
            f"stored signal {signal.signal_id!r} could not form a valid execution "
            f"request: {exc}"
        ) from exc
    return request, f"{CLIENT_ORDER_ID_PREFIX}{signal.signal_id}"


def run_production_shadow_cycle(
    signal_id: str,
    *,
    signal_provider: StoredSignalProvider,
    config: ShadowExecutionConfig,
    market_provider: MarketSnapshotProvider,
    account_provider: AccountSnapshotProvider,
    shadow_db_path: str | Path,
    decision_db_path: str | Path,
    clock: Callable[[], datetime],
    owner_id: str | None = None,
    dry_run: bool = False,
) -> ShadowCycleOutcome:
    """Run ONE production shadow cycle for a STORED signal (module docstring).

    ``dry_run=True`` evaluates the same inputs with zero side effects (no
    store initialization, nothing persisted, read-only probes only).
    """
    if not isinstance(config, ShadowExecutionConfig):
        raise ShadowInputError(f"config must be a ShadowExecutionConfig, got {config!r}")

    signal = signal_provider.get_signal(signal_id)
    quantity = config.order_quantity_for(signal.symbol)
    request, client_order_id = build_execution_request_from_signal(signal, quantity=quantity)

    provenance = ShadowProvenance(
        input_origin=INPUT_ORIGIN_PRODUCTION_SIGNAL,
        signal_source=signal_provider.source_id,
        config_source=config.source,
        config_version=config.config_id,
        market_source=market_provider.source_id,
        account_source=account_provider.source_id,
        signal_observed_at_utc=signal.created_at_utc,
    )
    gate = SafetyGate(
        policy=config.policy,
        state_provider=StaticSafetyState(kill_switch_enabled=config.kill_switch),
        clock=clock,
    )
    service = ShadowExecutionService(
        gate=gate,
        decision_db_path=decision_db_path,
        shadow_db_path=shadow_db_path,
        clock=clock,
        connection_target=config.connection_target,
        owner_id=owner_id,
    )
    if dry_run:
        # Dry run never claims (zero side effects), so the snapshots are
        # simply resolved for this one evaluation.
        return service.dry_run_cycle(
            request,
            market=market_provider.get_market_snapshot(signal.symbol),
            account=account_provider.get_account_snapshot(),
            client_order_id=client_order_id,
            provenance=provenance,
            timeframe=signal.timeframe,
        )
    service.initialize_stores()
    # Suppliers, not values (PR #100 3rd-review Major 1): only the claim
    # WINNER performs the market/account provider I/O -- a duplicate loser
    # returns before either provider is contacted. A provider failure after
    # the claim leaves the claim held for ordinary lease-expiry recovery.
    return service.run_cycle(
        request,
        market_supplier=lambda: market_provider.get_market_snapshot(signal.symbol),
        account_supplier=account_provider.get_account_snapshot,
        client_order_id=client_order_id,
        provenance=provenance,
        timeframe=signal.timeframe,
    )
