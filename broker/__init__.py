"""Broker boundary for the FUTURE Live Execution layer (Issue #73).

Explicit Python package. Import from the repository root::

    from broker.broker_interface import Broker
    from broker.broker_models import OrderRequest, OrderResult
    from broker.fake_broker import FakeBroker

The boundary modules (``broker_interface`` / ``broker_models``) and
FakeBroker use no network and no DB. The only module that talks HTTP is
``oanda_demo_adapter`` (Issue #79), and it is hard-locked to the OANDA DEMO
endpoint (live endpoints are refused at construction, fail-closed). Paper
Execution (``paper-execution/``) stays broker-free and must not import this
package.
"""
