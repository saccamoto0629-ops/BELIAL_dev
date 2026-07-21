"""ExecutionRequest: the BELIAL-internal "what we intend to order" model (Issue #74).

*** NOTHING HERE PLACES A REAL ORDER. This is a model for the FUTURE Live
Execution layer; the Live Execution runner / Broker API adapter / Safety Gate
do not exist yet. ***

ExecutionRequest vs ``broker.OrderRequest``
-------------------------------------------

They are deliberately two different models with one conversion direction:

- ``ExecutionRequest`` (this module) is the PERSISTABLE record of what BELIAL
  decided to order: it carries the provenance (source signal / strategy IDs)
  and the full intent including ``stop_loss_price`` / ``take_profit_price``.
  It lives BEFORE the broker boundary and is what the execution store
  persists and audits.
- ``broker.OrderRequest`` is the broker-agnostic request actually handed to a
  ``Broker`` implementation. It carries no provenance and (deliberately, per
  Issue #73) no SL/TP fields -- the broker boundary models a plain
  market/limit order only.

The conversion responsibility lives HERE: :meth:`ExecutionRequest.to_order_request`
is the only supported way to derive an ``OrderRequest`` from an
``ExecutionRequest``. SL/TP and provenance never cross the broker boundary;
``requested_price`` maps to ``limit_price`` for LIMIT orders.

ID design (``execution_request_id`` vs ``client_order_id``)
-----------------------------------------------------------

Two IDs with different meanings are kept in different fields and are NEVER
merged into one value:

- ``execution_request_id`` identifies the BELIAL-internal order INTENT. It is
  the primary key of the execution store and the key for state history.
- ``client_order_id`` is the caller-side key at the BROKER boundary (submit /
  cancel / query, Issue #73). It identifies one broker-side order for this
  intent.

The execution store binds them 1:1 (``client_order_id`` is NOT NULL + UNIQUE
and immutable once stored): one intent maps to at most one broker order.
After an UNKNOWN outcome the persisted ``client_order_id`` is what a later
query uses -- re-submitting the same intent under a NEW ``client_order_id``
is exactly the double-order hazard Issue #74 exists to prevent, so the model
never generates or rotates client order IDs implicitly. The two IDs MAY hold
equal strings if a caller chooses so, but no code may rely on that.

Validation
----------

Same conventions as the broker boundary (Issue #73): ``Decimal`` for every
price / quantity, timezone-aware UTC datetimes only, frozen dataclass,
non-empty IDs/symbol, positive finite quantity and prices. Violations raise
``ValueError`` (this is a BELIAL-internal model, not the broker boundary, so
the repository's plain model-validation style applies).

SL/TP vs direction: only relations that are unambiguous at this stage are
enforced (see ``__post_init__``). Anything that depends on an unknown fill
price (e.g. SL/TP distance rules for MARKET orders) is deliberately NOT
validated here -- adding such business rules is out of scope for Issue #74.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from broker.broker_models import OrderRequest, OrderSide, OrderType


def _require_non_empty_str(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _require_utc(value, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field_name} must be UTC (utcoffset 0), got {value.tzinfo}")


def _require_positive_decimal(value, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal, got {value!r}")
    if value <= 0:
        raise ValueError(f"{field_name} must be > 0, got {value!r}")


@dataclass(frozen=True)
class ExecutionRequest:
    """One BELIAL-internal order intent, as handed from Strategy / Signal
    toward the (future) Live Execution use case.

    See the module docstring for the ExecutionRequest vs ``broker.OrderRequest``
    split and the ``execution_request_id`` vs ``client_order_id`` ID design.

    ``side`` / ``order_type`` reuse the broker enums (``OrderSide`` /
    ``OrderType``) on purpose: the direction vocabulary is shared with the
    broker boundary, and no parallel enum is introduced. Mapping any
    Signal-side vocabulary (e.g. long/short) to ``OrderSide`` is the caller's
    responsibility and happens before an ExecutionRequest is constructed.

    Cross-field invariants (all unambiguous, enforced):

    - LIMIT requires ``requested_price``; MARKET forbids it (same contract as
      ``OrderRequest.limit_price``).
    - When BOTH stop loss and take profit are set: BUY requires
      ``stop_loss_price < take_profit_price``; SELL requires
      ``take_profit_price < stop_loss_price`` (independent of the fill price).
    - When ``requested_price`` is known (LIMIT): BUY requires
      ``stop_loss_price < requested_price`` and
      ``take_profit_price > requested_price``; SELL the mirror image.

    Deliberately NOT enforced: any SL/TP relation for a MARKET order with only
    one of the two set (no reference price exists yet), and any minimum
    distance / risk-reward business rule.
    """

    execution_request_id: str
    signal_id: str
    strategy_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    requested_at_utc: datetime
    requested_price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.execution_request_id, "execution_request_id")
        _require_non_empty_str(self.signal_id, "signal_id")
        _require_non_empty_str(self.strategy_id, "strategy_id")
        _require_non_empty_str(self.symbol, "symbol")
        if not isinstance(self.side, OrderSide):
            raise ValueError(f"side must be an OrderSide, got {self.side!r}")
        if not isinstance(self.order_type, OrderType):
            raise ValueError(f"order_type must be an OrderType, got {self.order_type!r}")
        _require_positive_decimal(self.quantity, "quantity")
        _require_utc(self.requested_at_utc, "requested_at_utc")
        for name in ("requested_price", "stop_loss_price", "take_profit_price"):
            value = getattr(self, name)
            if value is not None:
                _require_positive_decimal(value, name)

        if self.order_type is OrderType.LIMIT:
            if self.requested_price is None:
                raise ValueError("requested_price is required for LIMIT orders")
        elif self.requested_price is not None:
            raise ValueError("requested_price must be None for MARKET orders")

        sl, tp = self.stop_loss_price, self.take_profit_price
        buying = self.side is OrderSide.BUY
        if sl is not None and tp is not None:
            if buying and not sl < tp:
                raise ValueError(f"BUY requires stop_loss_price < take_profit_price, got {sl} / {tp}")
            if not buying and not tp < sl:
                raise ValueError(f"SELL requires take_profit_price < stop_loss_price, got {tp} / {sl}")
        if self.requested_price is not None:
            price = self.requested_price
            if sl is not None and (sl >= price if buying else sl <= price):
                raise ValueError(
                    f"stop_loss_price {sl} must be on the losing side of requested_price "
                    f"{price} for {self.side.value}"
                )
            if tp is not None and (tp <= price if buying else tp >= price):
                raise ValueError(
                    f"take_profit_price {tp} must be on the winning side of requested_price "
                    f"{price} for {self.side.value}"
                )

    def to_order_request(self, *, client_order_id: str, created_at_utc: datetime) -> OrderRequest:
        """Derive the broker-boundary ``OrderRequest`` for this intent.

        The caller supplies the boundary key (``client_order_id``) and the
        boundary-side creation time; both live at the broker boundary, not in
        the intent. SL/TP and provenance do NOT cross the boundary (the
        Issue #73 order model has no such fields). ``OrderRequest`` performs
        its own construction-time validation.
        """
        return OrderRequest(
            client_order_id=client_order_id,
            symbol=self.symbol,
            side=self.side,
            order_type=self.order_type,
            quantity=self.quantity,
            created_at_utc=created_at_utc,
            limit_price=self.requested_price if self.order_type is OrderType.LIMIT else None,
        )
