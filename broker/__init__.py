"""Broker boundary for the FUTURE Live Execution layer (Issue #73).

Explicit Python package. Import from the repository root::

    from broker.broker_interface import Broker
    from broker.broker_models import OrderRequest, OrderResult
    from broker.fake_broker import FakeBroker

No real orders, no network, no DB anywhere in this package. Paper Execution
(``paper-execution/``) stays broker-free and must not import this package.
"""
