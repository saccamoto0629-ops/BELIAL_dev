"""Shadow Mode result models (Issue #78; claim / provenance rework PR #100).

*** MODELS ONLY. Nothing here submits, persists or decides anything, and
nothing here touches the broker boundary: shadow mode audits what WOULD have
been sent -- it never constructs a ``broker.OrderRequest`` and never fabricates
a broker outcome (no order state, no broker reference id, no fills). ***

Input provenance
----------------

Every shadow record carries WHERE its inputs came from (:class:`ShadowProvenance`):

- ``input_origin`` -- ``production_signal`` (a STORED signal read through the
  production signal provider + production configuration), ``cli_fixture``
  (a manually authored cycle-input file) or ``scenario_fixture`` (the fixed
  scenario suite). Only ``production_signal`` may claim to be "the real
  signal path"; fixtures are clearly labelled as fixtures.
- the provider / store identifiers for the signal, configuration, market
  snapshot and account snapshot, the configuration version, and the source
  signal's own timestamp.

Duplicate vs conflict (canonical payload)
-----------------------------------------

Idempotency identity is the key triple (``execution_request_id`` /
``signal_id`` / ``client_order_id``), but a key match alone never makes a
DUPLICATE: the CANONICAL PAYLOAD (:func:`canonical_cycle_payload` -- request
content, provenance origin, mode, timeframe; Decimals compared by numeric
value, so ``"1"`` == ``"1.0"``) must be byte-identical too. A key match with
ANY payload difference is an identity CONFLICT (a different order trying to
reuse an identity) and is surfaced as an explicit error, never silently
answered with the stored record. Evaluation-time values (market snapshot,
clock timestamps) are deliberately NOT part of the payload: a crash-retry of
the same cycle input must classify as duplicate, not conflict.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from broker.broker_models import OrderSide, OrderType
from execution.execution_models import ExecutionRequest
from execution.safety_gate import (
    AccountSafetySnapshot,
    ExecutionMode,
    MarketSafetySnapshot,
    SafetyDecision,
)

INPUT_ORIGIN_PRODUCTION_SIGNAL = "production_signal"
INPUT_ORIGIN_CLI_FIXTURE = "cli_fixture"
INPUT_ORIGIN_SCENARIO_FIXTURE = "scenario_fixture"
KNOWN_INPUT_ORIGINS: frozenset[str] = frozenset(
    {INPUT_ORIGIN_PRODUCTION_SIGNAL, INPUT_ORIGIN_CLI_FIXTURE, INPUT_ORIGIN_SCENARIO_FIXTURE}
)

PHASE_CLAIMED = "claimed"
PHASE_COMPLETED = "completed"
KNOWN_SHADOW_PHASES: frozenset[str] = frozenset({PHASE_CLAIMED, PHASE_COMPLETED})


def _require_non_empty_str(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _require_utc(value, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got {value!r}")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field_name} must be UTC (utcoffset 0), got {value.tzinfo}")


@dataclass(frozen=True)
class ShadowProvenance:
    """Where one shadow cycle's inputs came from (see the module docstring).

    ``signal_observed_at_utc`` is the SOURCE SIGNAL's own timestamp; the
    production path always carries it, fixtures may leave it None.
    """

    input_origin: str
    signal_source: str
    config_source: str
    config_version: str
    market_source: str
    account_source: str
    signal_observed_at_utc: datetime | None = None

    def __post_init__(self) -> None:
        if self.input_origin not in KNOWN_INPUT_ORIGINS:
            raise ValueError(
                f"input_origin must be one of {sorted(KNOWN_INPUT_ORIGINS)}, "
                f"got {self.input_origin!r}"
            )
        for name in ("signal_source", "config_source", "config_version", "market_source",
                     "account_source"):
            _require_non_empty_str(getattr(self, name), name)
        if self.signal_observed_at_utc is not None:
            _require_utc(self.signal_observed_at_utc, "signal_observed_at_utc")
        if (
            self.input_origin == INPUT_ORIGIN_PRODUCTION_SIGNAL
            and self.signal_observed_at_utc is None
        ):
            raise ValueError(
                "production_signal provenance requires the source signal's timestamp"
            )


@dataclass(frozen=True)
class ShadowIntendedOrder:
    """The order content that WOULD have been handed to a broker.

    Same field vocabulary as ``broker.OrderRequest`` (Issue #73), but a pure
    audit value: it is never submitted, never converted, and carries no
    broker response data.
    """

    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.client_order_id, "client_order_id")
        _require_non_empty_str(self.symbol, "symbol")
        if not isinstance(self.side, OrderSide):
            raise ValueError(f"side must be an OrderSide, got {self.side!r}")
        if not isinstance(self.order_type, OrderType):
            raise ValueError(f"order_type must be an OrderType, got {self.order_type!r}")
        if not isinstance(self.quantity, Decimal) or not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError(f"quantity must be a positive finite Decimal, got {self.quantity!r}")
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None:
                raise ValueError("limit_price is required for LIMIT intended orders")
        elif self.limit_price is not None:
            raise ValueError("limit_price must be None for MARKET intended orders")
        if self.limit_price is not None and (
            not isinstance(self.limit_price, Decimal)
            or not self.limit_price.is_finite()
            or self.limit_price <= 0
        ):
            raise ValueError(f"limit_price must be a positive finite Decimal, got {self.limit_price!r}")


def intended_order_from_request(
    request: ExecutionRequest, *, client_order_id: str
) -> ShadowIntendedOrder:
    """Audit projection of ``request`` onto the broker-boundary field set.

    Field mapping is identical to ``ExecutionRequest.to_order_request()``
    (``requested_price`` -> ``limit_price`` for LIMIT, nothing else crosses),
    but no ``OrderRequest`` is constructed: shadow mode records the content
    without ever touching the submission conversion.
    """
    if not isinstance(request, ExecutionRequest):
        raise ValueError(f"request must be an ExecutionRequest, got {request!r}")
    return ShadowIntendedOrder(
        client_order_id=client_order_id,
        symbol=request.symbol,
        side=request.side,
        order_type=request.order_type,
        quantity=request.quantity,
        limit_price=request.requested_price if request.order_type is OrderType.LIMIT else None,
    )


def _canonical_decimal(value: Decimal | None) -> str | None:
    """Value-canonical Decimal text: numerically equal inputs (``1`` /
    ``1.0`` / ``1E+0``) canonicalize to the SAME string, so payload equality
    follows numeric equality, never surface spelling."""
    return None if value is None else str(value.normalize())


def canonical_cycle_payload(
    request: ExecutionRequest,
    *,
    client_order_id: str,
    timeframe: str | None,
    input_origin: str,
) -> dict:
    """The canonical identity payload one shadow cycle claims (see the module
    docstring). Deterministic across retries of the same input: everything
    here comes from the request / caller identity, nothing from evaluation
    time."""
    if not isinstance(request, ExecutionRequest):
        raise ValueError(f"request must be an ExecutionRequest, got {request!r}")
    _require_non_empty_str(client_order_id, "client_order_id")
    if timeframe is not None:
        _require_non_empty_str(timeframe, "timeframe")
    if input_origin not in KNOWN_INPUT_ORIGINS:
        raise ValueError(
            f"input_origin must be one of {sorted(KNOWN_INPUT_ORIGINS)}, got {input_origin!r}"
        )
    return {
        "execution_request_id": request.execution_request_id,
        "signal_id": request.signal_id,
        "client_order_id": client_order_id,
        "strategy_id": request.strategy_id,
        "symbol": request.symbol,
        "side": request.side.value,
        "order_type": request.order_type.value,
        "quantity": _canonical_decimal(request.quantity),
        "requested_price": _canonical_decimal(request.requested_price),
        "stop_loss_price": _canonical_decimal(request.stop_loss_price),
        "take_profit_price": _canonical_decimal(request.take_profit_price),
        "timeframe": timeframe,
        "execution_mode": ExecutionMode.SHADOW.value,
        "input_origin": input_origin,
    }


def canonical_payload_json(payload: dict) -> str:
    """Key-sorted compact JSON: key ordering can never affect equality."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def differing_payload_fields(stored: dict, attempted: dict) -> tuple[str, ...]:
    """The payload keys whose values differ (for conflict diagnostics)."""
    keys = sorted(set(stored) | set(attempted))
    return tuple(key for key in keys if stored.get(key) != attempted.get(key))


@dataclass(frozen=True)
class ShadowClaim:
    """One claimed-but-not-completed shadow identity row (phase ``claimed``).

    The claim is taken BEFORE the Safety Gate runs: exactly one ATTEMPT may
    hold it, so exactly one evaluation / decision record exists per cycle.
    Ownership is per ``run_cycle()`` ATTEMPT, not per service instance
    (PR #100 re-review Major 1): ``claim_attempt_id`` identifies the one
    call that acquired the claim, and ``claim_generation`` is the fencing
    token -- it increments on every takeover, and completion is accepted only
    when BOTH still match the row, so a stale holder that comes back after a
    takeover can never overwrite the new holder's cycle. An ACTIVE claim is
    never re-acquired by anyone (not even another attempt of the same
    service); a claim whose lease expired is STALE and is taken over by the
    next run of the SAME canonical payload (crash recovery -- no claim can
    stay unprocessable forever).
    """

    shadow_result_id: int
    execution_request_id: str
    signal_id: str
    client_order_id: str
    claim_owner_id: str
    claim_attempt_id: str
    claim_generation: int
    claimed_at_utc: datetime
    lease_expires_at_utc: datetime
    input_origin: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.shadow_result_id, int)
            or isinstance(self.shadow_result_id, bool)
            or self.shadow_result_id < 1
        ):
            raise ValueError(f"shadow_result_id must be a positive int, got {self.shadow_result_id!r}")
        for name in ("execution_request_id", "signal_id", "client_order_id", "claim_owner_id",
                     "claim_attempt_id"):
            _require_non_empty_str(getattr(self, name), name)
        if (
            not isinstance(self.claim_generation, int)
            or isinstance(self.claim_generation, bool)
            or self.claim_generation < 1
        ):
            raise ValueError(f"claim_generation must be an int >= 1, got {self.claim_generation!r}")
        _require_utc(self.claimed_at_utc, "claimed_at_utc")
        _require_utc(self.lease_expires_at_utc, "lease_expires_at_utc")
        if self.lease_expires_at_utc <= self.claimed_at_utc:
            raise ValueError("lease_expires_at_utc must be after claimed_at_utc")
        if self.input_origin not in KNOWN_INPUT_ORIGINS:
            raise ValueError(f"unknown input_origin {self.input_origin!r}")


@dataclass(frozen=True)
class ShadowResult:
    """One COMPLETED shadow verification record (Issue #78).

    Cross-field invariants (enforced at construction):

    - ``decision`` belongs to ``request`` (same ``execution_request_id``) and
      was evaluated as execution mode ``shadow``;
    - ``intended_order`` is exactly the audit projection of ``request`` under
      ``client_order_id``;
    - ``account`` is the full injected :class:`AccountSafetySnapshot` the
      gate judged against (persisted for audit replay; no secrets exist in
      the model);
    - ``policy_snapshot`` is a JSON-able mapping whose ``policy_id`` equals
      the decision's ``policy_id`` (defensively deep-copied through JSON);
    - ``shadow_result_id`` / ``safety_decision_id`` are DB-assigned positive
      integers or None (``safety_decision_id`` is None exactly when decision
      persistence failed and the decision carries ``decision_persist_failed``).

    Deliberately ABSENT: any broker outcome (order state, broker order id,
    fill price/quantity). A shadow record means "this WOULD have been sent /
    was rejected by the gate", never "this happened at a broker".
    """

    request: ExecutionRequest
    client_order_id: str
    timeframe: str | None
    provenance: ShadowProvenance
    decision: SafetyDecision
    intended_order: ShadowIntendedOrder
    market: MarketSafetySnapshot
    account: AccountSafetySnapshot
    policy_snapshot: dict
    safety_decision_id: int | None
    claim_owner_id: str
    claimed_at_utc: datetime
    completed_at_utc: datetime
    shadow_result_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, ExecutionRequest):
            raise ValueError(f"request must be an ExecutionRequest, got {self.request!r}")
        _require_non_empty_str(self.client_order_id, "client_order_id")
        if self.timeframe is not None:
            _require_non_empty_str(self.timeframe, "timeframe")
        if not isinstance(self.provenance, ShadowProvenance):
            raise ValueError(f"provenance must be a ShadowProvenance, got {self.provenance!r}")
        if not isinstance(self.decision, SafetyDecision):
            raise ValueError(f"decision must be a SafetyDecision, got {self.decision!r}")
        if self.decision.execution_request_id != self.request.execution_request_id:
            raise ValueError(
                f"decision belongs to {self.decision.execution_request_id!r}, "
                f"request is {self.request.execution_request_id!r}"
            )
        if self.decision.execution_mode != ExecutionMode.SHADOW.value:
            raise ValueError(
                f"shadow results require execution_mode {ExecutionMode.SHADOW.value!r}, "
                f"decision carries {self.decision.execution_mode!r}"
            )
        if not isinstance(self.intended_order, ShadowIntendedOrder):
            raise ValueError(
                f"intended_order must be a ShadowIntendedOrder, got {self.intended_order!r}"
            )
        expected = intended_order_from_request(self.request, client_order_id=self.client_order_id)
        if self.intended_order != expected:
            raise ValueError(
                f"intended_order {self.intended_order!r} does not match the audit "
                f"projection of the request ({expected!r})"
            )
        if not isinstance(self.market, MarketSafetySnapshot):
            raise ValueError(f"market must be a MarketSafetySnapshot, got {self.market!r}")
        if not isinstance(self.account, AccountSafetySnapshot):
            raise ValueError(f"account must be an AccountSafetySnapshot, got {self.account!r}")
        if not isinstance(self.policy_snapshot, dict):
            raise ValueError(
                f"policy_snapshot must be a dict, got {type(self.policy_snapshot).__name__}"
            )
        try:
            canonical = json.loads(json.dumps(self.policy_snapshot))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"policy_snapshot must be JSON-serializable: {exc}") from exc
        if canonical.get("policy_id") != self.decision.policy_id:
            raise ValueError(
                f"policy_snapshot policy_id {canonical.get('policy_id')!r} does not match "
                f"the decision's policy_id {self.decision.policy_id!r}"
            )
        # Detach from the caller's dict AFTER validation (deep immutability in
        # spirit -- the record can no longer change under the caller's hands).
        object.__setattr__(self, "policy_snapshot", canonical)
        for name in ("safety_decision_id", "shadow_result_id"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 1
            ):
                raise ValueError(f"{name} must be a positive int or None, got {value!r}")
        _require_non_empty_str(self.claim_owner_id, "claim_owner_id")
        _require_utc(self.claimed_at_utc, "claimed_at_utc")
        _require_utc(self.completed_at_utc, "completed_at_utc")

    @property
    def allowed(self) -> bool:
        return self.decision.allowed

    @property
    def rejection_codes(self) -> tuple[str, ...]:
        return self.decision.rejection_codes

    @property
    def canonical_payload(self) -> dict:
        return canonical_cycle_payload(
            self.request,
            client_order_id=self.client_order_id,
            timeframe=self.timeframe,
            input_origin=self.provenance.input_origin,
        )
