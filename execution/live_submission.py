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
   Every duplicate suppression is WARN-logged with the
   ``execution_request_id``, the source ``signal_id``, the current
   ``submission_phase`` / ``order_state`` and the suppression reason
   (Issue #76); ``register()`` duplicates (same intent id or same source
   signal) are suppressed and logged by the execution store's UNIQUE
   constraints (``signal_id`` is unique DB-wide, Issue #76).
3. The broker outcome is recorded back into the execution store: an
   ``OrderResult`` via ``record_order_result``; ANY exception from
   ``submit_order`` records state UNKNOWN (conservative: even a
   "communication failure" claim never downgrades to "not placed") and then
   re-raises to the caller. A FAILURE TO PERSIST a successfully received
   ``OrderResult`` also falls back to a state-only UNKNOWN record before the
   failure is re-raised (Issue #75 review r3) -- an order that exists at the
   broker must stay a query target, never vanish behind a persistence error.
   Whatever remains stuck (phase ``submitting`` / state ``pending`` after a
   crash or a double failure) is retrievable via
   ``execution_store.list_unresolved_submissions()``.
4. Independently of the execution store, every attempt that survives the
   gate is journaled in the durable SUBMISSION JOURNAL (Issue #93,
   ``execution/submission_journal.py`` -- its own SQLite file, sealed
   inside the service exactly like the audit store): the intent is
   journaled BEFORE the ``created -> submitting`` transition, the
   ``started`` mark is written IMMEDIATELY before ``submit_order``, and
   the broker response / unknown outcome / Execution-DB-save failure /
   completion are journaled around the calls that produce them. ANY
   journal failure BEFORE the broker call is fail-closed
   (``SUBMISSION_JOURNAL_FAILED``, journal conflicts ->
   ``DUPLICATE_SUBMISSION``): the broker is not called. AFTER the broker
   call, journal failures are logged critically but never block recording
   the outcome and are never converted into rejections (the order may
   exist). When the Execution Order DB itself is unavailable (schema
   damage, read-only, corruption, file loss), the journal row -- outcome
   ``reconciliation_required`` -- is the durable record from which
   ``SubmissionJournal.list_unresolved()`` recovers the attempt, across
   process restarts. Unresolved / unknown entries are NEVER re-sent; they
   are resolved by reconciliation (crash-boundary table:
   docs/architecture/submission_journal.md).

Fail-closed conversions (before any broker call): store lookup failures ->
``STATE_QUERY_FAILED``; unregistered intents -> ``INTENT_NOT_REGISTERED``;
decision persistence or read-back failures -> ``DECISION_PERSIST_FAILED``;
unexpected exceptions -> ``GATE_INTERNAL_ERROR``. The audit store is SEALED
inside the service: the constructor accepts only a filesystem path
(``decision_db_path``) and instantiates the concrete
``SQLiteSafetyDecisionStore`` itself -- recorder objects cannot be injected,
so there is no injection point whose ``record()`` / read-back could be
overridden to forge persistence. After the broker was called, failures are
NEVER converted into synthetic rejections -- they surface loudly (the order
may exist).
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
from execution.submission_journal import (
    SubmissionJournal,
    SubmissionJournalConflictError,
)

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
        decision_db_path: str | Path,
        order_db_path: str | Path,
        journal_db_path: str | Path,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(connection, BrokerConnection):
            raise ValueError(f"connection must be a BrokerConnection, got {connection!r}")
        if not isinstance(gate, SafetyGate):
            raise ValueError(f"gate must be a SafetyGate, got {gate!r}")
        if not isinstance(decision_db_path, (str, Path)):
            # The audit trail is SEALED inside the service (Issue #75 review
            # r3): only a filesystem path can be injected, never a recorder
            # object -- so there is no injectable object whose record() /
            # read-back could be overridden to forge persistence.
            raise ValueError(
                "decision_db_path must be a filesystem path (the durable audit store "
                f"is sealed inside the service), got {type(decision_db_path).__name__}"
            )
        if not isinstance(order_db_path, (str, Path)):
            raise ValueError(
                f"order_db_path must be a filesystem path, got {type(order_db_path).__name__}"
            )
        if not isinstance(journal_db_path, (str, Path)):
            # The submission journal is SEALED like the audit store (Issue
            # #93): only a filesystem path can be injected, never a journal
            # object -- no injection point whose writes could be overridden
            # to forge the durable record.
            raise ValueError(
                "journal_db_path must be a filesystem path (the durable submission "
                f"journal is sealed inside the service), got {type(journal_db_path).__name__}"
            )
        if not callable(clock):
            raise ValueError(f"clock must be a callable returning a UTC datetime, got {clock!r}")
        self._connection = connection
        self._gate = gate
        self._decision_store = SQLiteSafetyDecisionStore(Path(decision_db_path))
        self._order_db_path = Path(order_db_path)
        self._journal = SubmissionJournal(Path(journal_db_path))
        self._clock = clock

    # --- intent registration -------------------------------------------------

    def register(self, request: ExecutionRequest, *, client_order_id: str) -> None:
        """Persist the intent (phase ``created``) and bind its FIXED
        ``client_order_id`` (NOT NULL + UNIQUE + immutable, Issue #74).

        Registration is separate from submission on purpose: the broker key
        exists before any submit attempt, and ``submit()`` can only ever use
        this stored key. Duplicate registrations -- same
        ``execution_request_id`` OR same source ``signal_id`` (Issue #76:
        one signal produces at most one execution request) -- raise
        ``ExecutionOrderConflictError`` from the store, which also logs the
        suppression.
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
        # recorded as UNKNOWN and re-raised, never converted to rejections;
        # journal writes are best-effort (critical log) and never block ----
        try:
            result = self._connection.broker.submit_order(order_request)
        except Exception as exc:
            self._journal_write(
                "broker outcome unknown",
                lambda at: self._journal.record_broker_outcome_unknown(
                    execution_request_id, error=f"submit outcome lost: {exc}", at_utc=at
                ),
                execution_request_id,
            )
            self._record_unknown_outcome(execution_request_id, exc, context="submit outcome lost")
            raise
        self._journal_write(
            "broker response",
            lambda at: self._journal.record_broker_response(
                execution_request_id,
                response_state=result.state,
                broker_order_id=result.broker_order_id,
                at_utc=at,
            ),
            execution_request_id,
        )
        try:
            execution_store.record_order_result(
                execution_request_id,
                result,
                reason=_SUBMIT_REASON,
                db_path=self._order_db_path,
            )
        except Exception as exc:
            # The order EXISTS (the broker answered) but its result could not
            # be persisted: it must remain a QUERY TARGET (Issue #75 review
            # r3). The JOURNAL is the durable record when the order DB itself
            # is down (Issue #93): flag reconciliation_required there FIRST,
            # then fall back to a state-only UNKNOWN record, then surface the
            # original failure -- never mask it as a rejection.
            self._journal_write(
                "execution DB save failure",
                lambda at: self._journal.record_execution_db_save_failed(
                    execution_request_id,
                    response_state=result.state,
                    broker_order_id=result.broker_order_id,
                    error=f"broker result persistence failed: {exc}",
                    at_utc=at,
                ),
                execution_request_id,
            )
            self._record_unknown_outcome(
                execution_request_id, exc, context="broker result persistence failed"
            )
            raise
        self._journal_write(
            "completion",
            lambda at: self._journal.mark_completed(
                execution_request_id,
                response_state=result.state,
                broker_order_id=result.broker_order_id,
                at_utc=at,
            ),
            execution_request_id,
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
                    execution_request_id,
                    row["submission_phase"],
                    row["order_state"],
                    now,
                    signal_id=row["signal_id"],
                )
            )

        request = execution_store.load_execution_request(
            execution_request_id, db_path=self._order_db_path
        )
        decision = self._gate.evaluate(request, connection=self._connection, market=market)
        decision = self._recorded(decision)
        if not decision.allowed:
            return decision

        # Durable submission journal, FIRST barrier (Issue #93): the intent
        # must be journaled -- independent of the execution store -- before
        # any transition toward a broker call. The journal's own duplicate
        # guard holds even if the order DB was wiped and re-registered.
        try:
            self._journal.record_submission_intent(
                execution_request_id=execution_request_id,
                client_order_id=row["client_order_id"],
                symbol=request.symbol,
                side=request.side,
                order_type=request.order_type,
                quantity=request.quantity,
                requested_price=request.requested_price,
                execution_mode=self._connection.execution_mode.value,
                connection_target=self._connection.connection_target,
                started_at_utc=self._now(),
            )
        except SubmissionJournalConflictError as exc:
            _LOGGER.warning(
                "duplicate submission suppressed by the submission journal: "
                "execution_request_id=%s reason=%s",
                execution_request_id,
                exc,
            )
            return self._recorded(
                self._decision(
                    execution_request_id,
                    (
                        SafetyRejection(
                            SafetyRejectionCode.DUPLICATE_SUBMISSION,
                            f"the submission journal already holds an attempt for this "
                            f"intent: {exc}; re-sending is banned -- unresolved journal "
                            "entries are handed to reconciliation, never re-submitted",
                        ),
                    ),
                    checked_at_utc=self._now(),
                )
            )
        except Exception as exc:
            return self._recorded(
                self._decision(
                    execution_request_id,
                    (
                        SafetyRejection(
                            SafetyRejectionCode.SUBMISSION_JOURNAL_FAILED,
                            f"submission journal persistence failed: {exc}; the broker "
                            "was NOT called (fail closed)",
                        ),
                    ),
                    checked_at_utc=self._now(),
                )
            )

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

        # Durable submission journal, SECOND barrier (Issue #93): the
        # ``started`` mark is the LAST write before the broker call, so a
        # ``not_started`` journal entry always proves the broker was never
        # called. If this write fails the broker is not called; the order
        # row stays ``submitting``/``pending`` (visible via
        # list_unresolved_submissions()) while the journal still proves
        # no broker call happened.
        try:
            self._journal.mark_broker_call_started(execution_request_id, at_utc=self._now())
        except Exception as exc:
            return self._recorded(
                self._decision(
                    execution_request_id,
                    (
                        SafetyRejection(
                            SafetyRejectionCode.SUBMISSION_JOURNAL_FAILED,
                            f"journal broker-call-start persistence failed: {exc}; the "
                            "broker was NOT called (fail closed) -- the journal entry "
                            "remains 'not_started' as durable proof",
                        ),
                    ),
                    checked_at_utc=self._now(),
                )
            )

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
        signal_id = None
        try:
            row = execution_store.get_execution_order(
                execution_request_id, db_path=self._order_db_path
            )
            if row is not None:
                phase, state, signal_id = (
                    row["submission_phase"],
                    row["order_state"],
                    row["signal_id"],
                )
        except execution_store.ExecutionStoreError:
            pass
        now = self._now()
        if phase is not None and phase != execution_store.PHASE_CREATED:
            return self._duplicate_submission_decision(
                execution_request_id, phase, state, now, signal_id=signal_id
            )
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
        self,
        execution_request_id: str,
        phase: str,
        state: str | None,
        now: datetime,
        *,
        signal_id: str | None,
    ) -> SafetyDecision:
        # Issue #76 suppression log contract: the ids, the current state and
        # the suppression reason must be visible in the log, not only the
        # machine-readable rejection code.
        _LOGGER.warning(
            "duplicate submission suppressed: execution_request_id=%s signal_id=%s "
            "submission_phase=%s order_state=%s reason=%s",
            execution_request_id,
            signal_id,
            phase,
            state,
            "a submit attempt already exists; re-sending is banned while the outcome "
            "is submitted or uncertain (UNKNOWN is resolved by later queries)",
        )
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
        appends ``DECISION_PERSIST_FAILED`` (an allow becomes a rejection).

        The read-back checks MEMBERSHIP, not last position (PR #101 review):
        concurrent losers of the same intent each record their own rejection
        decision, so another caller's decision may land between this record
        and its read-back. What durability requires is that THIS decision
        survives a re-read -- being the newest row is not part of the
        contract and made the check racy."""
        try:
            self._decision_store.record(decision)
            stored = self._decision_store.list_for_request(decision.execution_request_id)
            if decision not in stored:
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

    def _journal_write(self, context: str, write, execution_request_id: str) -> None:
        """Best-effort journal write AFTER the broker call: a journal failure
        here must never block recording the broker outcome elsewhere and is
        never converted into a rejection (the order may exist). The entry
        then stays in its previous state -- still not ``completed``, so it
        remains visible via ``SubmissionJournal.list_unresolved()``. Uses
        the wall clock as a last resort: the injected clock may be exactly
        what is broken, and losing this record over a clock fault would
        defeat the journal's purpose."""
        try:
            try:
                at = self._now()
            except Exception:
                at = datetime.now(timezone.utc)
            write(at)
        except Exception:
            _LOGGER.critical(
                "failed to journal %s for execution_request_id=%s; the journal entry "
                "stays unresolved and remains listed by SubmissionJournal."
                "list_unresolved(); resolve via reconciliation",
                context,
                execution_request_id,
            )

    def _record_unknown_outcome(
        self, execution_request_id: str, exc: Exception, *, context: str
    ) -> None:
        """The broker may hold (or definitely holds) the order but no result
        could be persisted: record UNKNOWN (never "not placed" -- Issue #74)
        on a best-effort basis; the original exception is re-raised by the
        caller either way. If even this fails (e.g. the order DB itself is
        down), the order stays at phase ``submitting`` / state ``pending``
        and is retrievable via
        ``execution_store.list_unresolved_submissions()``."""
        try:
            execution_store.record_order_state(
                execution_request_id,
                OrderState.UNKNOWN,
                reason=f"{context}: {exc}",
                changed_at_utc=self._now(),
                db_path=self._order_db_path,
            )
        except Exception:
            _LOGGER.critical(
                "failed to record UNKNOWN outcome for execution_request_id=%s (%s); "
                "the order remains listed by list_unresolved_submissions(); resolve "
                "via reconciliation",
                execution_request_id,
                context,
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
