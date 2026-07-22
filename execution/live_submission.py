"""LiveOrderSubmissionService -- THE single sanctioned submission boundary
for the FUTURE Live Execution layer (Issue #75, review r1).

*** NOTHING HERE CONNECTS TO A REAL BROKER. The only Broker implementation
that can exist today is FakeBroker (see ``BrokerConnection``); no OANDA
adapter, no live connection, no production runner exists. ***

This service closes the official Live Execution submission path into ONE
boundary. The raw broker adapter is wired INTO the service (via the typed
``BrokerConnection``) at composition time and is never exposed outward;
repository-wide AST tests (``execution/tests/test_submission_boundary.py``)
pin that no other module calls ``Broker.submit_order`` or
``ExecutionRequest.to_order_request``.

Submission contract (docs/architecture/safety_gate.md):

1. ``register()`` persists the intent in the execution store (Issue #74),
   binding the FIXED ``client_order_id`` 1:1 to the ``execution_request_id``.
   ``submit()`` takes no client_order_id -- it always uses the stored one, so
   the same intent can never be re-sent under a fresh broker key (the
   double-order hazard Issue #74 exists to prevent).
2. ``submit()`` evaluates the Safety Gate, DURABLY records the decision and
   verifies it by READ-BACK from the store, and only then attempts the
   ``created -> submitting`` transition in the execution store. That
   transition succeeds exactly once per intent (``BEGIN IMMEDIATE``
   serialization, Issue #74), so only the caller that WINS the transition may
   submit: a second call -- including any call after an UNKNOWN outcome -- is
   rejected with ``DUPLICATE_SUBMISSION`` and never reaches the broker.
   UNKNOWN is resolved by later queries (Issue #74), never by re-sending.
3. The broker outcome is recorded back into the execution store: an
   ``OrderResult`` via ``record_order_result``; ANY exception from
   ``submit_order`` records state UNKNOWN (conservative: even a
   "communication failure" claim never downgrades to "not placed") and then
   re-raises to the caller.

Fail-closed conversions (before any broker call): store lookup failures ->
``STATE_QUERY_FAILED``; unregistered intents -> ``INTENT_NOT_REGISTERED``;
decision persistence or read-back failures -> ``DECISION_PERSIST_FAILED``;
unexpected exceptions -> ``GATE_INTERNAL_ERROR``. The decision store must be
the DURABLE ``SQLiteSafetyDecisionStore`` -- in-memory / no-op recorders are
rejected at construction, so the production path cannot run without an audit
trail. After the broker was called, failures are NEVER converted into
synthetic rejections -- they surface loudly (the order may exist).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from broker.broker_models import OrderResult, OrderState
from execution import execution_store
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
from execution.safety_store import SQLiteSafetyDecisionStore

_LOGGER = logging.getLogger(__name__)

_SUBMIT_REASON = "live submission via safety gate"


class LiveOrderSubmissionService:
    """The single service boundary from ``ExecutionRequest`` to
    ``Broker.submit_order`` (see the module docstring)."""

    def __init__(
        self,
        *,
        connection: BrokerConnection,
        gate: SafetyGate,
        decision_store: SQLiteSafetyDecisionStore,
        order_db_path: str | Path,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(connection, BrokerConnection):
            raise ValueError(f"connection must be a BrokerConnection, got {connection!r}")
        if not isinstance(gate, SafetyGate):
            raise ValueError(f"gate must be a SafetyGate, got {gate!r}")
        if not isinstance(decision_store, SQLiteSafetyDecisionStore):
            # The audit trail must be DURABLE: in-memory / no-op recorders are
            # structurally excluded from the production submission path.
            raise ValueError(
                "decision_store must be a SQLiteSafetyDecisionStore (durable audit "
                f"trail), got {type(decision_store).__name__}"
            )
        if not callable(clock):
            raise ValueError(f"clock must be a callable returning a UTC datetime, got {clock!r}")
        self._connection = connection
        self._gate = gate
        self._decision_store = decision_store
        self._order_db_path = Path(order_db_path)
        self._clock = clock

    # --- intent registration -------------------------------------------------

    def register(self, request: ExecutionRequest, *, client_order_id: str) -> None:
        """Persist the intent (phase ``created``) and bind its FIXED
        ``client_order_id`` (NOT NULL + UNIQUE + immutable, Issue #74).

        Registration is separate from submission on purpose: the broker key
        exists before any submit attempt, and ``submit()`` can only ever use
        this stored key.
        """
        execution_store.create_execution_order(
            request,
            client_order_id=client_order_id,
            created_at_utc=self._now(),
            db_path=self._order_db_path,
        )

    # --- submission ----------------------------------------------------------

    def submit(
        self, execution_request_id: str, *, market: MarketSafetySnapshot
    ) -> tuple[SafetyDecision, OrderResult | None]:
        """Evaluate, persist + read back the decision, win the
        ``created -> submitting`` transition, then submit -- in that order.

        Returns ``(decision, OrderResult)`` on a submitted order and
        ``(decision, None)`` on any rejection. Exceptions AFTER the broker
        call propagate (see the module docstring)."""
        _require_non_empty_str(execution_request_id, "execution_request_id")
        try:
            prepared = self._prepare(execution_request_id, market)
        except Exception as exc:
            # Unexpected failure before any broker call: never fall through
            # toward a submit. datetime.now is a last resort -- the injected
            # clock may be exactly what is broken.
            decision = self._recorded(
                self._decision(
                    execution_request_id,
                    (
                        SafetyRejection(
                            SafetyRejectionCode.GATE_INTERNAL_ERROR,
                            f"submission preparation failed unexpectedly: {exc}",
                        ),
                    ),
                    checked_at_utc=datetime.now(timezone.utc),
                )
            )
            self._log_rejection(decision)
            return decision, None
        if isinstance(prepared, SafetyDecision):
            self._log_rejection(prepared)
            return prepared, None

        decision, order_request = prepared
        # ---- past this point the broker may hold the order: failures are
        # recorded as UNKNOWN and re-raised, never converted to rejections ----
        try:
            result = self._connection.broker.submit_order(order_request)
        except Exception as exc:
            self._record_unknown_outcome(execution_request_id, exc)
            raise
        execution_store.record_order_result(
            execution_request_id,
            result,
            reason=_SUBMIT_REASON,
            db_path=self._order_db_path,
        )
        _LOGGER.info(
            "Safety Gate allowed and submitted execution_request_id=%s policy_id=%s "
            "client_order_id=%s state=%s",
            execution_request_id,
            decision.policy_id,
            order_request.client_order_id,
            result.state.value,
        )
        return decision, result

    # --- internals -----------------------------------------------------------

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

    def _prepare(self, execution_request_id: str, market: MarketSafetySnapshot):
        """Everything BEFORE the broker call. Returns a recorded rejected
        ``SafetyDecision``, or ``(allowed_decision, order_request)`` when the
        ``created -> submitting`` transition was won."""
        now = self._now()

        try:
            row = execution_store.get_execution_order(
                execution_request_id, db_path=self._order_db_path
            )
        except execution_store.ExecutionStoreError as exc:
            return self._recorded(
                self._decision(
                    execution_request_id,
                    (
                        SafetyRejection(
                            SafetyRejectionCode.STATE_QUERY_FAILED,
                            f"execution store lookup failed: {exc}",
                        ),
                    ),
                    checked_at_utc=now,
                )
            )
        if row is None:
            return self._recorded(
                self._decision(
                    execution_request_id,
                    (
                        SafetyRejection(
                            SafetyRejectionCode.INTENT_NOT_REGISTERED,
                            "no persisted execution order for this id; call register() first",
                        ),
                    ),
                    checked_at_utc=now,
                )
            )
        if row["submission_phase"] != execution_store.PHASE_CREATED:
            return self._recorded(
                self._duplicate_submission_decision(
                    execution_request_id, row["submission_phase"], row["order_state"], now
                )
            )

        request = execution_store.load_execution_request(
            execution_request_id, db_path=self._order_db_path
        )
        decision = self._gate.evaluate(request, connection=self._connection, market=market)
        decision = self._recorded(decision)
        if not decision.allowed:
            return decision

        # The double-submission guard: created -> submitting is entered
        # exactly once (BEGIN IMMEDIATE, Issue #74). Only the winner submits.
        try:
            execution_store.mark_submitting(
                execution_request_id,
                reason=_SUBMIT_REASON,
                changed_at_utc=self._now(),
                db_path=self._order_db_path,
            )
        except Exception as exc:
            return self._recorded(self._transition_failure_decision(execution_request_id, exc))

        return decision, request.to_order_request(
            client_order_id=row["client_order_id"],
            created_at_utc=now,
        )

    def _transition_failure_decision(self, execution_request_id: str, exc: Exception) -> SafetyDecision:
        """Classify a failed ``created -> submitting`` transition by the
        CURRENT stored phase, not by exception type/message: any phase other
        than ``created`` means a submit attempt already exists
        (``DUPLICATE_SUBMISSION`` -- UNKNOWN included, re-sending is banned);
        everything else is a store/state failure (fail closed)."""
        phase = None
        state = None
        try:
            row = execution_store.get_execution_order(
                execution_request_id, db_path=self._order_db_path
            )
            if row is not None:
                phase, state = row["submission_phase"], row["order_state"]
        except execution_store.ExecutionStoreError:
            pass
        now = self._now()
        if phase is not None and phase != execution_store.PHASE_CREATED:
            return self._duplicate_submission_decision(execution_request_id, phase, state, now)
        return self._decision(
            execution_request_id,
            (
                SafetyRejection(
                    SafetyRejectionCode.STATE_QUERY_FAILED,
                    f"created -> submitting transition failed: {exc}",
                ),
            ),
            checked_at_utc=now,
        )

    def _duplicate_submission_decision(
        self, execution_request_id: str, phase: str, state: str | None, now: datetime
    ) -> SafetyDecision:
        return self._decision(
            execution_request_id,
            (
                SafetyRejection(
                    SafetyRejectionCode.DUPLICATE_SUBMISSION,
                    f"a submit attempt already exists (phase {phase!r}, order_state "
                    f"{state!r}); re-sending is banned -- UNKNOWN outcomes are resolved "
                    "by later queries, never by re-submitting",
                ),
            ),
            checked_at_utc=now,
        )

    def _decision(
        self,
        execution_request_id: str,
        rejections: tuple[SafetyRejection, ...],
        *,
        checked_at_utc: datetime,
    ) -> SafetyDecision:
        return SafetyDecision(
            execution_request_id=execution_request_id,
            allowed=False,
            checked_at_utc=checked_at_utc,
            policy_id=self._gate.policy_id,
            rejections=dedupe_rejections(list(rejections)),
            execution_mode=self._connection.execution_mode.value,
            connection_target=self._connection.connection_target,
        )

    def _recorded(self, decision: SafetyDecision) -> SafetyDecision:
        """Durably record the decision and VERIFY it by read-back; only a
        decision that survives both may allow a submit (Issue #75 review r1).

        Any failure -- record exception, read-back miss, read-back mismatch --
        appends ``DECISION_PERSIST_FAILED`` (an allow becomes a rejection)."""
        try:
            self._decision_store.record(decision)
            stored = self._decision_store.list_for_request(decision.execution_request_id)
            if not stored or stored[-1] != decision:
                raise RuntimeError(
                    "decision read-back did not return the recorded decision"
                )
        except Exception as exc:
            if SafetyRejectionCode.DECISION_PERSIST_FAILED in {
                entry.code for entry in decision.rejections
            }:
                return decision
            return SafetyDecision(
                execution_request_id=decision.execution_request_id,
                allowed=False,
                checked_at_utc=decision.checked_at_utc,
                policy_id=decision.policy_id,
                rejections=decision.rejections
                + (
                    SafetyRejection(
                        SafetyRejectionCode.DECISION_PERSIST_FAILED,
                        f"safety decision persistence/read-back failed: {exc}",
                    ),
                ),
                execution_mode=decision.execution_mode,
                connection_target=decision.connection_target,
                account_id=decision.account_id,
                position_count=decision.position_count,
            )
        return decision

    def _record_unknown_outcome(self, execution_request_id: str, exc: Exception) -> None:
        """The submit call failed AFTER the broker may have seen the order:
        record UNKNOWN (never "not placed" -- Issue #74) on a best-effort
        basis; the original exception is re-raised by the caller either way."""
        try:
            execution_store.record_order_state(
                execution_request_id,
                OrderState.UNKNOWN,
                reason=f"submit outcome lost: {exc}",
                changed_at_utc=self._now(),
                db_path=self._order_db_path,
            )
        except Exception:
            _LOGGER.critical(
                "failed to record UNKNOWN outcome for execution_request_id=%s after a "
                "submit failure; resolve via manual reconciliation",
                execution_request_id,
            )

    def _log_rejection(self, decision: SafetyDecision) -> None:
        _LOGGER.warning(
            "Safety Gate rejected execution_request_id=%s policy_id=%s codes=%s",
            decision.execution_request_id,
            decision.policy_id,
            decision.rejection_codes,
        )


def _require_non_empty_str(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")
