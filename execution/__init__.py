"""Execution-request model and order-state persistence for the FUTURE Live
Execution layer (Issue #74).

Explicit Python package. Import from the repository root::

    from execution.execution_models import ExecutionRequest
    from execution import execution_store

No Discord and no S3 anywhere in this package, and no module here performs
network I/O of its own: HTTP lives entirely in the broker adapters. Orders
reach a broker through exactly ONE boundary, ``live_submission.py``, and a
LIVE connection is composed only by ``live_lock.py`` with every live lock
satisfied (Issue #82). The only dependency is the ``broker`` boundary
package (Issue #73): the broker
order-state machine (``broker.broker_models.OrderState`` and its validators)
is reused as the single source of truth -- this package defines NO order-state
enum and NO transition table of its own. Paper Signal / Paper Execution stay
broker-free and must not import this package.
"""
