r"""Broker interface — the boundary for the FUTURE Live Execution layer (Issue #73).

*** BOUNDARY ONLY. NO LIVE EXECUTION EXISTS. NOTHING HERE PLACES ORDERS. ***

Dependency direction (Issue #73)::

    Strategy / Paper Signal
        |
        v
    (future) Live Execution use case
        |
        v
    Broker interface / common models   <-- this module + broker_models
        ^
        |
    FakeBroker (test double, in-memory)
        ^
        |
    (future) OANDA Adapter

Rules pinned by this boundary:

- Strategy / Paper Signal / Paper Execution never import FakeBroker or any
  future broker adapter. Future execution use-cases depend on this Protocol
  only.
- A future adapter (e.g. OANDA) translates broker-specific wire responses
  into the ``broker_models`` common models INSIDE the adapter. Broker-specific
  fields must not leak through this interface — the Protocol only speaks
  ``OrderRequest`` / ``OrderResult`` / ``PositionInfo`` / ``AccountState``
  and typed ``BrokerError`` exceptions.
- ``client_order_id`` (caller-generated) is the key used at this boundary for
  cancel / query. An adapter maps it to the broker's own order ID internally.

``typing.Protocol`` (structural) was chosen over ABC so implementations —
FakeBroker today, real adapters later — need no inheritance relationship
with this package.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .broker_models import AccountState, OrderRequest, OrderResult, PositionInfo


@runtime_checkable
class Broker(Protocol):
    """Common broker operations (Issue #73).

    Error contract (see ``broker_models``): definitive business outcomes —
    accepted / (partially) filled / rejected — are returned as ``OrderResult``
    states; infrastructure failures raise typed ``BrokerError`` subclasses.
    After ``BrokerTimeoutError`` the order's state is ``UNKNOWN`` — the broker
    may hold the order, and callers must not treat it as "not placed".
    """

    def submit_order(self, request: OrderRequest) -> OrderResult:
        """Submit an order; return its resulting snapshot.

        Raises ``BrokerInvalidRequestError`` (malformed / duplicate request),
        ``BrokerTimeoutError`` (outcome unknown) or
        ``BrokerCommunicationError`` (request never exchanged).
        """
        ...

    def cancel_order(self, client_order_id: str) -> OrderResult:
        """Cancel the order registered under ``client_order_id``.

        Returns the post-cancel snapshot. Raises ``BrokerOrderNotFoundError``
        (unknown ID) or ``BrokerCancellationError`` (broker refused, e.g.
        already filled).
        """
        ...

    def get_order(self, client_order_id: str) -> OrderResult:
        """Return the latest snapshot of the order under ``client_order_id``.

        Raises ``BrokerOrderNotFoundError`` for unknown IDs.
        """
        ...

    def get_positions(self) -> tuple[PositionInfo, ...]:
        """Return the broker-side position snapshots."""
        ...

    def get_account_state(self) -> AccountState:
        """Return the broker account snapshot."""
        ...


@runtime_checkable
class OrderListingBroker(Protocol):
    """OPTIONAL capability: enumerate the orders the BROKER itself holds.

    Deliberately NOT a sixth method on ``Broker`` (Issue #103): the five
    Issue #73 operations stay the boundary every implementation must
    provide, so existing implementations -- FakeBroker, the OANDA Demo
    Adapter (Issue #79) and any future adapter -- keep satisfying ``Broker``
    unchanged. Enumeration is a separate structural Protocol an
    implementation may additionally satisfy.

    Reconciliation (Issue #103) needs it to notice orders that exist ONLY on
    the broker side: without an enumeration there is no way to observe an
    order BELIAL never recorded. An implementation that cannot enumerate
    simply does not satisfy this Protocol, and reconciliation then REPORTS
    that the broker-only check could not run -- it never concludes "no such
    orders exist".

    Read-only by contract: like ``get_order``, listing observes and must
    never place, modify or cancel anything.
    """

    def list_orders(self) -> tuple[OrderResult, ...]:
        """Return a snapshot of every order the broker currently knows about.

        Raises the same typed ``BrokerError`` subclasses as the other query
        operations when the enumeration itself fails.
        """
        ...
