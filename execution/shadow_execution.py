"""ShadowExecutionService -- observation-only run of the NORMAL decision path
(Issue #78).

*** NOTHING HERE SUBMITS AN ORDER. This module holds NO call to
``Broker.submit_order`` and NO call to ``ExecutionRequest.to_order_request``;
the repository-wide AST tests (``execution/tests/test_submission_boundary.py``)
pin that structurally -- even a fully configured production adapter inside
the injected ``BrokerConnection`` cannot be reached for submission from a
shadow run, because the call site does not exist. ***

What Shadow Mode is (and is not):

- It RUNS the real decision path: the real ``ExecutionRequest`` (with its
  source-signal / strategy provenance), the real ``SafetyGate`` with the
  real ``SafetyPolicy`` -- the SAME gate object a submission service would
  use, evaluated exactly once per observation, including its normal broker
  safety QUERIES (positions / account -- read-only APIs, never submit-family
  APIs) and its fail-closed contract for kill switch / stale data / broker
  query failures.
- It PERSISTS what happened -- the full intent, the full decision
  (allow/reject + every machine-readable rejection code + human-readable
  detail + ``policy_id`` + connection identity), the intended order content
  and the observation time -- into the shadow store
  (``execution/shadow_store.py``), so the record can be audited later via
  ``python -m execution.shadow_report``.
- It does NOT submit, simulate fills, manage virtual positions or compute
  PnL (Paper Execution / Backtest own those responsibilities), and it never
  records a broker order state: nothing was sent, so there is nothing to
  have ACCEPTED / FILLED / REJECTED. A Safety Gate rejection is a gate
  fact, never a broker response.
- It does NOT touch the execution order store: registering intents /
  winning ``created -> submitting`` belongs to real submissions
  (``LiveOrderSubmissionService``). Shadow keeps its own per-mode
  idempotency (``execution_request_id`` / ``signal_id`` UNIQUE in the
  shadow store only), so a shadow observation never suppresses -- and is
  never suppressed by -- a fake/demo/live submission of the same signal.

Fail-closed contract: an unexpected exception during gate evaluation is
converted into a recorded ``GATE_INTERNAL_ERROR`` rejection (mirroring the
sanctioned submission path); a record that cannot be persisted raises --
an unpersisted observation must never report success. The shadow store is
SEALED inside the service like the submission service's audit store: the
constructor accepts only a filesystem path (``shadow_db_path``), never a
recorder object.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from execution.execution_models import ExecutionRequest
from execution.safety_gate import (
    BrokerConnection,
    MarketSafetySnapshot,
    SafetyDecision,
    SafetyGate,
    SafetyRejection,
    SafetyRejectionCode,
    dedupe_rejections,
)
from execution.shadow_store import ShadowExecutionRecord, SQLiteShadowExecutionStore

_LOGGER = logging.getLogger(__name__)


class ShadowExecutionService:
    """Evaluate one ``ExecutionRequest`` through the normal Safety Gate and
    persist the outcome as a shadow verification record (see the module
    docstring). The broker submission boundary is never crossed."""

    def __init__(
        self,
        *,
        connection: BrokerConnection,
        gate: SafetyGate,
        shadow_db_path: str | Path,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(connection, BrokerConnection):
            raise ValueError(f"connection must be a BrokerConnection, got {connection!r}")
        if not isinstance(gate, SafetyGate):
            raise ValueError(f"gate must be a SafetyGate, got {gate!r}")
        if not isinstance(shadow_db_path, (str, Path)):
            # Same sealing stance as the submission service's audit store
            # (Issue #75 review r3): only a filesystem path can be injected,
            # never a recorder object whose record() could be overridden.
            raise ValueError(
                "shadow_db_path must be a filesystem path (the shadow store is sealed "
                f"inside the service), got {type(shadow_db_path).__name__}"
            )
        if not callable(clock):
            raise ValueError(f"clock must be a callable returning a UTC datetime, got {clock!r}")
        self._connection = connection
        self._gate = gate
        self._store = SQLiteShadowExecutionStore(Path(shadow_db_path))
        self._clock = clock

    def initialize_store(self) -> None:
        """Deliberately create (pristine DB only) or verify the shadow store.

        This is the ONE sanctioned creation entry point: ``observe()`` never
        creates the DB implicitly, and the read paths (store reads /
        ``shadow_report`` CLI) never create it at all.
        """
        self._store.initialize()

    def observe(
        self, request: ExecutionRequest, *, market: MarketSafetySnapshot
    ) -> ShadowExecutionRecord:
        """Run the normal gate evaluation for ``request`` and persist the
        outcome. Returns the record as read back from the store.

        Never calls any submit-family broker API. Gate evaluation failures
        are converted fail-closed into a recorded ``GATE_INTERNAL_ERROR``
        rejection; persistence failures (missing/broken store, duplicate
        intent or source signal) raise ``ShadowStoreError`` subclasses.
        """
        if not isinstance(request, ExecutionRequest):
            raise ValueError(f"request must be an ExecutionRequest, got {request!r}")
        try:
            decision = self._gate.evaluate(request, connection=self._connection, market=market)
        except Exception as exc:
            # Fail closed, mirroring the sanctioned submission path:
            # datetime.now is a last resort -- the injected clock may be
            # exactly what is broken.
            decision = SafetyDecision(
                execution_request_id=request.execution_request_id,
                allowed=False,
                checked_at_utc=datetime.now(timezone.utc),
                policy_id=self._gate.policy_id,
                rejections=dedupe_rejections(
                    [
                        SafetyRejection(
                            SafetyRejectionCode.GATE_INTERNAL_ERROR,
                            f"shadow gate evaluation failed unexpectedly: {exc}",
                        )
                    ]
                ),
                execution_mode=self._connection.execution_mode.value,
                connection_target=self._connection.connection_target,
            )
        record = ShadowExecutionRecord(
            request=request,
            decision=decision,
            observed_at_utc=self._now(),
        )
        stored = self._store.record(record)
        if decision.allowed:
            _LOGGER.info(
                "shadow observation recorded (ALLOWED, nothing submitted): "
                "execution_request_id=%s signal_id=%s policy_id=%s",
                request.execution_request_id,
                request.signal_id,
                decision.policy_id,
            )
        else:
            _LOGGER.warning(
                "shadow observation recorded (REJECTED, nothing submitted): "
                "execution_request_id=%s signal_id=%s policy_id=%s codes=%s",
                request.execution_request_id,
                request.signal_id,
                decision.policy_id,
                decision.rejection_codes,
            )
        return stored

    def _now(self) -> datetime:
        now = self._clock()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
            or now.utcoffset().total_seconds() != 0
        ):
            raise ValueError(f"clock must return a timezone-aware UTC datetime, got {now!r}")
        return now
