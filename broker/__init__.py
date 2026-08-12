"""Broker boundary for the Live Execution layer (Issue #73).

Explicit Python package. Import from the repository root::

    from broker.broker_interface import Broker
    from broker.broker_models import OrderRequest, OrderResult
    from broker.fake_broker import FakeBroker

The boundary modules (``broker_interface`` / ``broker_models``) and
FakeBroker use no network and no DB. The modules that talk HTTP are the
OANDA adapters, and each is hard-locked to ONE endpoint by a fail-closed
allowlist checked at construction: ``oanda_demo_adapter`` (Issue #79) to
the DEMO endpoint, ``oanda_live_adapter`` (Issue #82) to the LIVE endpoint.
Neither can be pointed at the other's endpoint, and the live adapter
additionally refuses to exist without ``ENABLE_LIVE_TRADING=true`` plus an
explicit ``--confirm-live`` (``LiveActivation``). The shared translation
body they share is ``oanda_v20_adapter``, which declares no endpoint,
credentials or identity of its own. Paper Execution (``paper-execution/``)
stays broker-free and must not import this package.
"""
