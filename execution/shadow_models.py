"""Shadow Mode result models (Issue #78).

*** MODELS ONLY. Nothing here submits, persists or decides anything, and
nothing here touches the broker boundary: shadow mode audits what WOULD have
been sent -- it never constructs a ``broker.OrderRequest`` and never fabricates
a broker outcome (no order state, no broker reference id, no fills). ***

:class:`ShadowIntendedOrder` is the audit projection of the order content that
would have crossed the broker boundary if the same request had run in a
submitting mode. It deliberately mirrors the ``broker.OrderRequest`` field set
(``client_order_id`` / symbol / side / order_type / quantity / ``limit_price``)
WITHOUT being one: ``ExecutionRequest.to_order_request()`` stays reserved for
the single sanctioned submission path (``execution/live_submission.py``; the
AST boundary test pins its call sites), so the shadow layer records the same
content as plain audit data instead of calling the conversion.

:class:`ShadowResult` is one complete shadow verification record: the full
``ExecutionRequest``, the Safety Gate decision (with every machine-readable
rejection code and human-readable message), the intended order content, the
market snapshot the gate judged against, and the applied policy snapshot.
Construction validates cross-field consistency, so a shadow record whose parts
disagree (wrong request id, non-shadow mode, intended order that does not match
the request) cannot exist in memory, let alone in the store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from broker.broker_models import OrderSide, OrderType
from execution.execution_models import ExecutionRequest
from execution.safety_gate import ExecutionMode, MarketSafetySnapshot, SafetyDecision


def _require_non_empty_str(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _require_utc(value, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got {value!r}")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field_name} must be UTC (utcoffset 0), got {value.tzinfo}")


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


@dataclass(frozen=True)
class ShadowResult:
    """One complete shadow verification record (Issue #78).

    Cross-field invariants (enforced at construction):

    - ``decision`` belongs to ``request`` (same ``execution_request_id``) and
      was evaluated as execution mode ``shadow``;
    - ``intended_order`` is exactly the audit projection of ``request`` under
      ``client_order_id`` (no drift between the stored intent and the stored
      "would-have-been order");
    - ``policy_snapshot`` is a JSON-able mapping whose ``policy_id`` equals
      the decision's ``policy_id`` (the snapshot is defensively deep-copied
      through JSON, so later caller-side mutation cannot alter the record);
    - ``shadow_result_id`` / ``safety_decision_id`` are DB-assigned positive
      integers or None (``shadow_result_id`` is None until persisted;
      ``safety_decision_id`` is None exactly when decision persistence failed
      and the decision carries ``decision_persist_failed``).

    Deliberately ABSENT: any broker outcome (order state, broker order id,
    fill price/quantity). A shadow record means "this WOULD have been sent /
    was rejected by the gate", never "this happened at a broker".
    """

    request: ExecutionRequest
    client_order_id: str
    timeframe: str | None
    decision: SafetyDecision
    intended_order: ShadowIntendedOrder
    market: MarketSafetySnapshot
    policy_snapshot: dict
    safety_decision_id: int | None
    created_at_utc: datetime
    shadow_result_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, ExecutionRequest):
            raise ValueError(f"request must be an ExecutionRequest, got {self.request!r}")
        _require_non_empty_str(self.client_order_id, "client_order_id")
        if self.timeframe is not None:
            _require_non_empty_str(self.timeframe, "timeframe")
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
        _require_utc(self.created_at_utc, "created_at_utc")

    @property
    def allowed(self) -> bool:
        return self.decision.allowed

    @property
    def rejection_codes(self) -> tuple[str, ...]:
        return self.decision.rejection_codes
