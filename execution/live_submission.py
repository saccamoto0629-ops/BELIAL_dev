"""LiveOrderSubmissionService -- THE single sanctioned submission boundary
for the FUTURE Live Execution layer (Issue #75, review r1).

*** THIS SERVICE IS THE ONLY PLACE AN ORDER CROSSES INTO A BROKER. *** The
broker behind it may be a FakeBroker, the OANDA DEMO adapter (Issue #132,
composed only by ``execution/demo_lock.py``) or the OANDA LIVE adapter
(Issue #82, composed only by ``execution/live_lock.py`` and NOT released for
real-account operation); the contract below is identical for all three, and
both real-broker modes additionally require the DURABLE Kill Switch reader.

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

Cancellation contract (Issue #99): ``cancel()`` is THE single sanctioned
path from a registered intent to ``Broker.cancel_order`` -- pinned by the
same repository-wide AST tests. Only a broker-confirmed, still-working
order (phase ``submitted``, state ACCEPTED / PARTIALLY_FILLED) reaches the
broker; everything else is refused BEFORE any broker call with a typed
``CancelRefusalCode`` (already CANCELLED -> idempotent duplicate refusal;
terminal -> nothing to cancel; PENDING / UNKNOWN -> unresolved outcomes are
resolved by queries / reconciliation, never by more broker calls). A
definitive broker refusal (``BrokerCancellationError``) leaves the stored
state untouched and is returned as ``BROKER_REFUSED``; ANY other broker
exception and ANY failure to persist a received post-cancel result record
state-only UNKNOWN (best-effort) and re-raise -- the mirror of the submit
contract above. Every cancel attempt is additionally claimed in the durable
CANCELLATION JOURNAL (PR #124 Codex review P1; same sealed SQLite file as
the submission journal, own table): the claim is journaled BEFORE any
broker call and the ``started`` mark is the last write before
``cancel_order``, so a caller that crashed after the broker call -- or a
concurrent second caller -- can never send a second broker cancel even
while the order row still reads cancellable (journal conflicts ->
``ATTEMPT_UNRESOLVED``; journal failures -> ``JOURNAL_FAILED``, fail
closed). After the broker call, journal writes are best-effort and never
block recording the outcome; unresolved claims are listed by
``SubmissionJournal.list_unresolved_cancellations()`` and are resolved by
reconciliation, never by re-sending.

Reissue policy (Issue #116 / PR #122 review r2): ``reissue_policy`` is a
MANDATORY constructor argument -- the SAME ``ReissuePolicy`` instance the
registration service (``execution/reissue_service.py``) uses. The clearance
freshness window is enforced at both boundaries, and a submission window
wider than the registration one would silently authorize evidence the
registration policy already treats as stale; refusing to default it here
makes that mismatch unconstructable rather than merely discouraged.

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

import enum
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from broker.broker_models import (
    BrokerCancellationError,
    OrderResult,
    OrderState,
    TERMINAL_ORDER_STATES,
)
from execution import execution_store
from execution.execution_models import ExecutionRequest
from execution.reissue_clearance import ClearanceScopeError, derive_account_scope
from execution.reissue_service import ReissuePolicy
from execution.kill_switch_store import PersistentKillSwitchStateProvider
from execution.safety_gate import (
    BrokerConnection,
    ExecutionMode,
    MarketSafetySnapshot,
    REAL_BROKER_MODES,
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
_CANCEL_REASON = "live cancellation via sanctioned cancel path"
_CANCEL_UNKNOWN_CONTEXT_LOST = "cancel outcome lost"
_CANCEL_UNKNOWN_CONTEXT_PERSIST = "cancel result persistence failed"

# The order states a cancel may be sent for: broker-confirmed, still working.
# Everything else is refused BEFORE any broker call (see ``cancel()``).
_CANCELLABLE_STATES: frozenset[OrderState] = frozenset(
    {OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED}
)


class CancelStatus(enum.Enum):
    """Machine-readable outcome classes of one ``cancel()`` call (Issue #99).

    ``CANCELLED``       -- the broker confirmed the cancel and the terminal
                           CANCELLED observation was durably recorded.
    ``REFUSED``         -- refused LOCALLY, before any broker call
                           (``refusal_code`` says why; the broker cancel
                           call count did not change).
    ``BROKER_REFUSED``  -- the broker DEFINITIVELY refused the cancel
                           (``BrokerCancellationError``, e.g. the order
                           settled broker-side). The stored state is
                           unchanged; the order stays in-flight and the
                           divergence is reconciliation's to observe.

    Outcomes that are none of these -- timeout, communication failure,
    unknown order, persistence failure after a received result -- are NOT
    statuses: they RE-RAISE (after conservatively recording UNKNOWN), the
    same way ``submit()`` never converts a post-broker failure into a
    rejection.
    """

    CANCELLED = "cancelled"
    REFUSED = "refused"
    BROKER_REFUSED = "broker_refused"


class CancelRefusalCode(enum.Enum):
    """Why a ``cancel()`` was refused before any broker call (closed set)."""

    #: The execution store could not even be read (fail closed).
    STATE_QUERY_FAILED = "cancel_state_query_failed"
    #: No persisted execution order exists under this id.
    INTENT_NOT_REGISTERED = "cancel_intent_not_registered"
    #: Phase ``created``: no submit attempt was ever made, so no broker
    #: order can exist to cancel.
    NOT_SUBMITTED = "cancel_not_submitted"
    #: Phase ``submitting`` / state ``pending``: a submit attempt started but
    #: no broker observation exists yet (in-flight call, or a crash left the
    #: attempt stuck). Whether the broker holds the order is exactly what is
    #: unresolved -- cancelling blind is banned; reconciliation resolves it.
    SUBMISSION_UNRESOLVED = "cancel_submission_unresolved"
    #: State ``unknown`` (lost submit outcome, lost cancel outcome, or a
    #: result whose persistence failed): re-sending anything while the
    #: outcome is unresolved is banned -- resolved by queries, never by a
    #: second broker call. This is also the duplicate-cancel guard after a
    #: cancel whose outcome was lost.
    OUTCOME_UNRESOLVED = "cancel_outcome_unresolved"
    #: The durable cancellation journal already holds an attempt for this
    #: order that may have reached (or provably reached) the broker -- the
    #: crash-window / concurrent-caller guard (PR #124 Codex review P1):
    #: even when the ORDER row still reads cancellable (the process died
    #: after the broker cancel but before any outcome write, or another
    #: caller is mid-flight), the claim cannot be re-acquired and no second
    #: broker cancel is sent. Resolved by reconciliation
    #: (``SubmissionJournal.list_unresolved_cancellations()``).
    ATTEMPT_UNRESOLVED = "cancel_attempt_unresolved"
    #: The durable cancellation journal could not record the claim (or the
    #: started mark): the broker is NOT called (fail closed) -- without the
    #: durable claim a crash could make the attempt re-sendable.
    JOURNAL_FAILED = "cancel_journal_failed"
    #: State ``cancelled``: the cancel already succeeded. Refusing (instead
    #: of re-sending) makes re-running the same cancel idempotent.
    ALREADY_CANCELLED = "cancel_already_cancelled"
    #: State ``filled`` / ``rejected``: terminal states are never left again
    #: (``TERMINAL_ORDER_STATES``), so there is nothing a cancel could do.
    TERMINAL_STATE = "cancel_terminal_state"


@dataclass(frozen=True)
class CancelOutcome:
    """The typed result of one ``cancel()`` call (Issue #99).

    Cross-field invariants (validated at construction):

    - ``CANCELLED``      <-> ``result`` present, no ``refusal_code``,
                             ``broker_called`` True
    - ``REFUSED``        <-> ``refusal_code`` present, no ``result``,
                             ``broker_called`` False
    - ``BROKER_REFUSED`` <-> no ``result``, no ``refusal_code``,
                             ``broker_called`` True
    - ``detail`` is always a non-empty human-readable explanation.
    """

    execution_request_id: str
    status: CancelStatus
    detail: str
    refusal_code: CancelRefusalCode | None = None
    result: OrderResult | None = None
    broker_called: bool = False

    def __post_init__(self) -> None:
        _require_non_empty_str(self.execution_request_id, "execution_request_id")
        if not isinstance(self.status, CancelStatus):
            raise ValueError(f"status must be a CancelStatus, got {self.status!r}")
        _require_non_empty_str(self.detail, "detail")
        if self.refusal_code is not None and not isinstance(self.refusal_code, CancelRefusalCode):
            raise ValueError(
                f"refusal_code must be a CancelRefusalCode or None, got {self.refusal_code!r}"
            )
        if self.result is not None and not isinstance(self.result, OrderResult):
            raise ValueError(f"result must be an OrderResult or None, got {self.result!r}")
        if not isinstance(self.broker_called, bool):
            raise ValueError(f"broker_called must be a bool, got {self.broker_called!r}")
        if self.status is CancelStatus.CANCELLED:
            if self.result is None or self.refusal_code is not None or not self.broker_called:
                raise ValueError(
                    "a CANCELLED outcome requires a result, no refusal_code and "
                    "broker_called=True"
                )
        elif self.status is CancelStatus.REFUSED:
            if self.result is not None or self.refusal_code is None or self.broker_called:
                raise ValueError(
                    "a REFUSED outcome requires a refusal_code, no result and "
                    "broker_called=False"
                )
        else:  # BROKER_REFUSED
            if self.result is not None or self.refusal_code is not None or not self.broker_called:
                raise ValueError(
                    "a BROKER_REFUSED outcome requires no result, no refusal_code and "
                    "broker_called=True"
                )


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
        reissue_policy: ReissuePolicy,
    ) -> None:
        if not isinstance(connection, BrokerConnection):
            raise ValueError(f"connection must be a BrokerConnection, got {connection!r}")
        if not isinstance(gate, SafetyGate):
            raise ValueError(f"gate must be a SafetyGate, got {gate!r}")
        # A REAL-BROKER submission boundary must read a DURABLE kill switch
        # (Issue #82; PR #128 Codex review r5 P1; extended to ``demo`` by
        # Issue #132). Composition-time evidence cannot be made unforgeable
        # inside one process -- anything that can reach a module-private
        # signer can equally read the credentials out of the environment --
        # so the control that has to hold is here, where it is checkable: an
        # in-memory provider (or any subclass free to override the query)
        # would make "the kill switch is off" true by construction and turn
        # an unreadable switch into a silent OFF, which Issue #82 forbids.
        # ``demo`` is included because an operator pressing the Kill Switch
        # means "stop sending orders", not "stop sending orders unless the
        # account is a practice one" -- a demo runner that could be composed
        # with a static provider would keep submitting. ``fake`` / shadow
        # compositions keep their static providers (no network, no account).
        if connection.execution_mode in REAL_BROKER_MODES and (
            type(gate.state_provider) is not PersistentKillSwitchStateProvider
        ):
            raise ValueError(
                f"a {connection.execution_mode.value} submission boundary requires the "
                "gate to read the persistent kill switch (exactly a "
                "PersistentKillSwitchStateProvider, so the state cannot be answered from "
                "memory and an unreadable switch cannot read as OFF), got "
                f"{type(gate.state_provider).__name__}"
            )
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
        # ONE reissue policy governs both boundaries, and naming it is
        # MANDATORY (PR #122 Codex review r2): a silent default here would
        # let a composer configure a narrow registration window
        # (ReissueService) while submissions kept authorizing evidence
        # under the wider default window -- the mismatch is invisible
        # precisely because nobody wrote it down. Requiring the argument
        # makes "both boundaries share one policy object" a property the
        # code enforces instead of a convention the docs merely ask for.
        # ``reissue_policy`` is exposed as a read-only property so the
        # registration service can be built from the very same instance.
        if not isinstance(reissue_policy, ReissuePolicy):
            raise ValueError(
                f"reissue_policy must be a ReissuePolicy (the SAME instance the "
                f"registration side uses; there is deliberately no default, so the "
                f"two freshness windows cannot silently diverge), got {reissue_policy!r}"
            )
        self._connection = connection
        self._gate = gate
        self._decision_store = SQLiteSafetyDecisionStore(Path(decision_db_path))
        self._order_db_path = Path(order_db_path)
        self._journal = SubmissionJournal(Path(journal_db_path))
        self._clock = clock
        self._reissue_policy = reissue_policy

    @property
    def order_db_path(self) -> Path:
        """The execution order DB this boundary writes (Issue #131).

        A driver has to read the SAME store to find the intents it may
        drive; exposing the path (never a store object -- the audit store
        and the journal stay sealed) removes the possibility of a driver
        being pointed at a different file than the boundary writes. Mirrors
        ``ShadowExecutionService.shadow_db_path``."""
        return self._order_db_path

    @property
    def execution_mode(self) -> ExecutionMode:
        """The composed connection's execution mode (Issue #131).

        Read-only IDENTITY, not a handle: a driver (the Live Execution
        runner) has to refuse an unsupported mode before it processes
        anything, and it must be able to do so WITHOUT being handed the
        broker or the connection. The authoritative check stays where it
        always was -- the gate re-derives the identity from the broker
        instance on every evaluation, so a drifting identity fails closed
        there regardless of what this property reported."""
        return self._connection.execution_mode

    @property
    def connection_target(self) -> str:
        """The composed connection's target (audit / logging identity; see
        :attr:`execution_mode`)."""
        return self._connection.connection_target

    @property
    def reissue_policy(self) -> ReissuePolicy:
        """The reissue policy this submission boundary enforces. Compose the
        registration service from THIS object so both boundaries provably
        share one policy."""
        return self._reissue_policy

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

    # --- cancellation --------------------------------------------------------

    def cancel(self, execution_request_id: str) -> CancelOutcome:
        """Cancel the broker order behind a registered intent -- THE single
        sanctioned path to ``Broker.cancel_order`` (Issue #99), pinned
        repository-wide by the same AST tests that pin ``submit_order``.

        Symmetric with ``submit()`` wherever the contracts overlap:

        - Everything BEFORE the broker call fails closed into a typed
          ``REFUSED`` outcome; a refused cancel provably never reached the
          broker (``broker_called=False``).
        - Only a broker-confirmed, still-working order (phase ``submitted``,
          state ACCEPTED / PARTIALLY_FILLED) may be cancelled. CANCELLED is
          refused as ``ALREADY_CANCELLED`` (duplicate-cancel idempotency:
          re-running a completed cancel never re-calls the broker); other
          terminal states are refused (nothing to cancel); ``pending`` /
          ``unknown`` are refused as unresolved -- an uncertain outcome is
          resolved by later queries / reconciliation, never by sending more
          broker calls (the submit-side UNKNOWN contract, applied to
          cancel).
        - ``BrokerCancellationError`` is the broker's DEFINITIVE refusal:
          the stored state stays untouched (no synthetic transition), the
          failure is WARN-logged with ids/state/reason and returned as
          ``BROKER_REFUSED``. The order remains in-flight, so the
          store/broker divergence that caused the refusal (e.g. filled
          broker-side) stays visible to reconciliation
          (``list_in_flight_orders`` / ``list_reconciliation_orders``).
          A later ``cancel()`` is a NEW attempt, not a duplicate: a
          definitively refused cancel leaves nothing pending at the broker.
        - ANY other exception from ``cancel_order`` (timeout, communication
          failure, unknown order, unexpected) means the cancel outcome --
          and therefore the ORDER outcome -- is lost: state-only UNKNOWN is
          recorded (best-effort, critically logged on failure) and the
          exception re-raises. Never converted into a refusal; never
          re-sent -- the UNKNOWN row is a query target
          (``list_unknown_orders`` / ``list_unresolved_submissions``) and
          any later ``cancel()`` refuses with ``OUTCOME_UNRESOLVED``.
        - A received post-cancel ``OrderResult`` whose persistence fails is
          NEVER lost silently: the response is journaled as
          ``reconciliation_required`` (durable even with the order DB down),
          state-only UNKNOWN is recorded (the order becomes a query target
          whose broker-side truth -- CANCELLED -- a later query re-observes)
          and the original persistence failure re-raises.
        - Every attempt is CLAIMED in the durable cancellation journal
          BEFORE the broker call (PR #124 Codex review P1): concurrent
          callers serialize on the claim and its one-shot ``not_started ->
          started`` transition, and a caller restarted after a possibly-sent
          cancel finds the durable claim and is refused
          (``ATTEMPT_UNRESOLVED``) even while the order row still reads
          cancellable. A claim that provably never reached the broker
          (``not_started``) is reclaimable; a definitively refused attempt
          (``broker_refused``) is reclaimable as a NEW attempt. Journal
          failures before the broker call refuse fail-closed
          (``JOURNAL_FAILED``); after it they are logged critically and
          never block.
        """
        _require_non_empty_str(execution_request_id, "execution_request_id")
        try:
            row = execution_store.get_execution_order(
                execution_request_id, db_path=self._order_db_path
            )
        except Exception as exc:
            # ANY lookup failure -- typed store errors and unexpected ones
            # alike (e.g. a corrupted DB file) -- fails closed before the
            # broker can be reached (submit's GATE_INTERNAL_ERROR catch-all,
            # collapsed into the one query-failure refusal cancel needs).
            return self._refused_cancel(
                execution_request_id,
                CancelRefusalCode.STATE_QUERY_FAILED,
                f"execution store lookup failed: {exc}",
                phase=None,
                state=None,
                signal_id=None,
            )
        if row is None:
            return self._refused_cancel(
                execution_request_id,
                CancelRefusalCode.INTENT_NOT_REGISTERED,
                "no persisted execution order for this id; nothing to cancel",
                phase=None,
                state=None,
                signal_id=None,
            )
        phase = row["submission_phase"]
        state = None if row["order_state"] is None else OrderState(row["order_state"])
        signal_id = row["signal_id"]
        if phase == execution_store.PHASE_CREATED:
            return self._refused_cancel(
                execution_request_id,
                CancelRefusalCode.NOT_SUBMITTED,
                "no submit attempt was ever made (phase 'created'), so no broker "
                "order can exist to cancel",
                phase=phase,
                state=state,
                signal_id=signal_id,
            )
        if state is OrderState.UNKNOWN:
            return self._refused_cancel(
                execution_request_id,
                CancelRefusalCode.OUTCOME_UNRESOLVED,
                "the order's outcome is UNKNOWN; re-sending is banned while the "
                "outcome is uncertain -- it is resolved by later queries / "
                "reconciliation, never by another broker call",
                phase=phase,
                state=state,
                signal_id=signal_id,
            )
        if state is OrderState.PENDING:
            return self._refused_cancel(
                execution_request_id,
                CancelRefusalCode.SUBMISSION_UNRESOLVED,
                "a submit attempt started but no broker observation exists yet "
                "(phase 'submitting'/state 'pending'); whether the broker holds "
                "the order is exactly what is unresolved -- reconciliation "
                "resolves it, a blind cancel does not",
                phase=phase,
                state=state,
                signal_id=signal_id,
            )
        if state is OrderState.CANCELLED:
            return self._refused_cancel(
                execution_request_id,
                CancelRefusalCode.ALREADY_CANCELLED,
                "the order is already CANCELLED; re-running the same cancel is "
                "an idempotent no-op and never re-calls the broker",
                phase=phase,
                state=state,
                signal_id=signal_id,
            )
        if state in TERMINAL_ORDER_STATES:
            return self._refused_cancel(
                execution_request_id,
                CancelRefusalCode.TERMINAL_STATE,
                f"the order is terminal ({state.value}); terminal states are "
                "never left again, so there is nothing a cancel could do",
                phase=phase,
                state=state,
                signal_id=signal_id,
            )
        if state not in _CANCELLABLE_STATES:
            # Defensive closed-world guard: every OrderState is handled above;
            # a state added to the enum without a cancel decision fails closed.
            return self._refused_cancel(
                execution_request_id,
                CancelRefusalCode.OUTCOME_UNRESOLVED,
                f"state {state.value!r} has no defined cancel semantics (fail closed)",
                phase=phase,
                state=state,
                signal_id=signal_id,
            )

        # Durable cancel claim, FIRST barrier (PR #124 Codex review P1): the
        # attempt is journaled -- independent of the order row's state --
        # before any broker call. Concurrent callers serialize on the
        # journal's write transaction, and a restarted caller finds the
        # durable claim: a possibly-sent cancel can never be sent again,
        # even while the order row still reads ACCEPTED (the crash window
        # between the broker call and the outcome writes).
        try:
            self._journal.record_cancellation_intent(
                execution_request_id=execution_request_id,
                client_order_id=row["client_order_id"],
                started_at_utc=self._now(),
            )
        except SubmissionJournalConflictError as exc:
            return self._refused_cancel(
                execution_request_id,
                CancelRefusalCode.ATTEMPT_UNRESOLVED,
                f"the cancellation journal already holds an attempt for this order: "
                f"{exc}; re-sending is banned -- unresolved cancel attempts are "
                "handed to reconciliation, never re-sent",
                phase=phase,
                state=state,
                signal_id=signal_id,
            )
        except Exception as exc:
            return self._refused_cancel(
                execution_request_id,
                CancelRefusalCode.JOURNAL_FAILED,
                f"cancellation journal persistence failed: {exc}; the broker was NOT "
                "called (fail closed)",
                phase=phase,
                state=state,
                signal_id=signal_id,
            )
        # SECOND barrier: the ``started`` mark is the LAST write before the
        # broker call, entered exactly once per claim (``not_started ->
        # started`` serializes in the journal's transaction) -- of two
        # racing callers holding the same claim, only one reaches the
        # broker; a ``not_started`` row remains durable proof the broker
        # cancel was never sent and is reclaimable by a later attempt.
        try:
            self._journal.mark_cancel_broker_call_started(
                execution_request_id, at_utc=self._now()
            )
        except Exception as exc:
            return self._refused_cancel(
                execution_request_id,
                CancelRefusalCode.JOURNAL_FAILED,
                f"journal cancel-call-start persistence failed: {exc}; the broker was "
                "NOT called (fail closed) -- a 'not_started' claim remains as durable "
                "proof and a later cancel may reclaim it",
                phase=phase,
                state=state,
                signal_id=signal_id,
            )

        # ---- past this point the broker is called: failures are recorded
        # conservatively as UNKNOWN and re-raised, never converted into
        # refusals (mirror of the submit contract); journal writes are
        # best-effort (critical log) and never block -----------------------
        try:
            result = self._connection.broker.cancel_order(row["client_order_id"])
        except BrokerCancellationError as exc:
            # The broker's DEFINITIVE refusal: the order was NOT cancelled and
            # the broker answered authoritatively -- nothing is uncertain, so
            # nothing is recorded into the state machine (the stored state
            # stays valid) and nothing re-raises. The failure is recorded on
            # the outcome and in the log (Issue #76 log contract style).
            _LOGGER.warning(
                "broker refused cancellation: execution_request_id=%s signal_id=%s "
                "client_order_id=%s submission_phase=%s order_state=%s reason=%s",
                execution_request_id,
                signal_id,
                row["client_order_id"],
                phase,
                state.value,
                exc,
            )
            self._journal_write(
                "cancel broker refusal",
                lambda at: self._journal.mark_cancel_broker_refused(
                    execution_request_id, error=f"broker refused cancellation: {exc}", at_utc=at
                ),
                execution_request_id,
                recovery_surface="SubmissionJournal.list_unresolved_cancellations()",
            )
            return CancelOutcome(
                execution_request_id=execution_request_id,
                status=CancelStatus.BROKER_REFUSED,
                detail=f"the broker definitively refused the cancellation: {exc}",
                broker_called=True,
            )
        except Exception as exc:
            # Timeout / communication failure / unknown order / anything
            # unexpected: the cancel outcome is LOST, so the order outcome is
            # lost with it (conservative: a "not found" or "communication
            # failure" claim never downgrades what the broker may hold).
            self._journal_write(
                "cancel outcome unknown",
                lambda at: self._journal.record_cancel_broker_outcome_unknown(
                    execution_request_id, error=f"cancel outcome lost: {exc}", at_utc=at
                ),
                execution_request_id,
                recovery_surface="SubmissionJournal.list_unresolved_cancellations()",
            )
            self._record_unknown_outcome(
                execution_request_id, exc, context=_CANCEL_UNKNOWN_CONTEXT_LOST
            )
            raise
        self._journal_write(
            "cancel broker response",
            lambda at: self._journal.record_cancel_broker_response(
                execution_request_id,
                response_state=result.state,
                broker_order_id=result.broker_order_id,
                at_utc=at,
            ),
            execution_request_id,
            recovery_surface="SubmissionJournal.list_unresolved_cancellations()",
        )
        try:
            execution_store.record_order_result(
                execution_request_id,
                result,
                reason=_CANCEL_REASON,
                db_path=self._order_db_path,
            )
        except Exception as exc:
            # The broker ANSWERED (the order is settled broker-side) but the
            # result could not be persisted: the order must remain a QUERY
            # TARGET whose broker-side truth a later query re-observes --
            # never lost behind a persistence error (Issue #75 review r3
            # contract, applied to cancel). The JOURNAL keeps the durable
            # record -- response content included -- even when the order DB
            # itself is down.
            self._journal_write(
                "cancel execution DB save failure",
                lambda at: self._journal.record_cancel_execution_db_save_failed(
                    execution_request_id,
                    response_state=result.state,
                    broker_order_id=result.broker_order_id,
                    error=f"cancel result persistence failed: {exc}",
                    at_utc=at,
                ),
                execution_request_id,
                recovery_surface="SubmissionJournal.list_unresolved_cancellations()",
            )
            self._record_unknown_outcome(
                execution_request_id, exc, context=_CANCEL_UNKNOWN_CONTEXT_PERSIST
            )
            raise
        self._journal_write(
            "cancel completion",
            lambda at: self._journal.mark_cancel_completed(
                execution_request_id,
                response_state=result.state,
                broker_order_id=result.broker_order_id,
                at_utc=at,
            ),
            execution_request_id,
            recovery_surface="SubmissionJournal.list_unresolved_cancellations()",
        )
        if result.state is not OrderState.CANCELLED:
            # Defensive: the Broker contract returns the post-cancel snapshot
            # of a SUCCESSFUL cancel. A conforming observation was recorded
            # above either way (the audit trail keeps the truth), but the
            # outcome must not claim a cancellation that did not happen.
            _LOGGER.warning(
                "cancel returned a non-cancelled snapshot: execution_request_id=%s "
                "signal_id=%s state=%s (recorded as-is; not reported as cancelled)",
                execution_request_id,
                signal_id,
                result.state.value,
            )
            return CancelOutcome(
                execution_request_id=execution_request_id,
                status=CancelStatus.BROKER_REFUSED,
                detail=(
                    f"the broker's post-cancel snapshot is {result.state.value!r}, "
                    "not 'cancelled'; the snapshot was recorded but no "
                    "cancellation is claimed"
                ),
                broker_called=True,
            )
        _LOGGER.info(
            "cancelled execution_request_id=%s signal_id=%s client_order_id=%s "
            "broker_order_id=%s",
            execution_request_id,
            signal_id,
            row["client_order_id"],
            result.broker_order_id,
        )
        return CancelOutcome(
            execution_request_id=execution_request_id,
            status=CancelStatus.CANCELLED,
            detail="the broker confirmed the cancellation and CANCELLED was durably recorded",
            result=result,
            broker_called=True,
        )

    def _refused_cancel(
        self,
        execution_request_id: str,
        code: CancelRefusalCode,
        detail: str,
        *,
        phase: str | None,
        state: OrderState | None,
        signal_id: str | None,
    ) -> CancelOutcome:
        """A cancel refused BEFORE any broker call, WARN-logged with the ids,
        the current phase/state and the refusal reason (the Issue #76
        suppression-log contract, applied to cancels)."""
        _LOGGER.warning(
            "cancellation refused: execution_request_id=%s signal_id=%s "
            "submission_phase=%s order_state=%s code=%s reason=%s",
            execution_request_id,
            signal_id,
            phase,
            None if state is None else state.value,
            code.value,
            detail,
        )
        return CancelOutcome(
            execution_request_id=execution_request_id,
            status=CancelStatus.REFUSED,
            detail=detail,
            refusal_code=code,
        )

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

        # Reissue child (Issue #95 / #116): submission authority requires
        # the signal's CURRENT clearance to be valid, fresh and scoped to
        # THIS connection's broker/account. The scope is derived here from
        # the actual submission connection (identity + account query) and
        # verified by mark_submitting inside its transaction; a scope that
        # cannot be derived is fail-closed BEFORE the journal or any
        # transition is touched.
        clearance_kwargs: dict = {}
        if row["signal_attempt"] >= 2:
            try:
                submission_scope = derive_account_scope(self._connection.broker)
            except ClearanceScopeError as exc:
                return self._recorded(
                    self._decision(
                        execution_request_id,
                        (
                            SafetyRejection(
                                SafetyRejectionCode.REISSUE_CLEARANCE_INVALID,
                                f"the submission connection's broker/account scope "
                                f"could not be derived: {exc}; a reissue child never "
                                "submits under an unverifiable scope (fail closed)",
                            ),
                        ),
                        checked_at_utc=self._now(),
                    )
                )
            clearance_kwargs = dict(
                clearance_scope=submission_scope,
                clearance_observed_after_utc=(
                    self._now() - self._reissue_policy.clearance_max_age
                ),
            )

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
        # For a reissue child the same transaction re-verifies the signal's
        # CURRENT clearance (valid / scope / freshness) and persists the
        # accepted evidence onto the granting history row (Issue #115/#116).
        try:
            execution_store.mark_submitting(
                execution_request_id,
                reason=_SUBMIT_REASON,
                changed_at_utc=self._now(),
                db_path=self._order_db_path,
                **clearance_kwargs,
            )
        except execution_store.ExecutionOrderSupersededError as exc:
            # The intent was retired by a reissue child (possibly registered
            # while this submit was in flight). The journal intent recorded
            # above is provably unsent (not_started) -- terminalize it
            # best-effort so it does not linger as unresolved; the durable
            # sweep (ReissueService.terminalize_superseded_intents) recovers
            # a failed cleanup.
            _LOGGER.warning(
                "submission refused, intent superseded: execution_request_id=%s "
                "signal_id=%s reason=%s",
                execution_request_id,
                row["signal_id"],
                exc,
            )
            self._terminalize_superseded_intent(execution_request_id)
            return self._recorded(
                self._decision(
                    execution_request_id,
                    (
                        SafetyRejection(
                            SafetyRejectionCode.INTENT_SUPERSEDED,
                            f"the intent was superseded by a reissue child and can "
                            f"never gain submission authority: {exc}",
                        ),
                    ),
                    checked_at_utc=self._now(),
                )
            )
        except execution_store.ExecutionClearanceError as exc:
            return self._recorded(
                self._decision(
                    execution_request_id,
                    (
                        SafetyRejection(
                            SafetyRejectionCode.REISSUE_CLEARANCE_INVALID,
                            f"no valid reissue clearance covers this submission: "
                            f"{exc} (fail closed)",
                        ),
                    ),
                    checked_at_utc=self._now(),
                )
            )
        except Exception as exc:
            return self._recorded(self._transition_failure_decision(execution_request_id, exc))

        # FINAL kill switch recheck (Issue #132, PR #136 review P1). The gate
        # read the switch at the TOP of its evaluation -- before the broker
        # safety GETs (open positions, account state), before the clearance
        # scope query, and before the journal / store writes above. On a real
        # account those steps are network round trips, so an operator who
        # pressed the switch during them would still have seen the order go
        # out. "Enabling the kill switch stops orders before the broker call
        # and takes effect immediately" is an operations contract, so the
        # switch is read once more here.
        #
        # PLACEMENT is deliberate: this is the last point before the broker
        # call at which a refusal is still provably clean. The ``started``
        # mark below is the journal's "the broker may hold this order"
        # barrier -- refusing after it would file a reconciliation case for a
        # call that never happened. Refusing here leaves the entry
        # ``not_started``, which is durable proof the broker was never
        # contacted, exactly like the journal-failure path underneath.
        #
        # The intent stays at phase ``submitting`` and is NOT retried: the
        # duplicate guard is untouched, and "stop" fails closed rather than
        # arming a resend. The halted intents are the ones the operator wants
        # to see, and list_unresolved_submissions() lists them.
        #
        # REAL_BROKER_MODES only: fake / shadow own no network-facing account
        # and answer from memory, so a second read there would change nothing
        # except their providers' call counts.
        if self._connection.execution_mode in REAL_BROKER_MODES:
            refusal = self._kill_switch_refusal()
            if refusal is not None:
                return self._recorded(
                    self._decision(
                        execution_request_id, (refusal,), checked_at_utc=self._now()
                    )
                )

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

    def _kill_switch_refusal(self) -> SafetyRejection | None:
        """Read the durable kill switch once more, fail closed.

        Mirrors :meth:`SafetyGate._collect_kill_switch_rejections` exactly:
        an exception or a non-boolean answer is a BROKEN PROVIDER, not an
        "off" -- both refuse, because the whole point of the durable reader
        is that an unreadable switch may never read as OFF. Only a literal
        ``False`` allows the broker call.

        Returns the rejection to record, or ``None`` to proceed.
        """
        try:
            state = self._gate.state_provider.is_kill_switch_enabled()
        except Exception as exc:
            return SafetyRejection(
                SafetyRejectionCode.STATE_QUERY_FAILED,
                f"the final kill switch recheck before the broker call failed: {exc}; "
                "the broker was NOT called (fail closed)",
            )
        if not isinstance(state, bool):
            return SafetyRejection(
                SafetyRejectionCode.STATE_QUERY_FAILED,
                f"the final kill switch recheck returned non-boolean {state!r}; "
                "the broker was NOT called (fail closed)",
            )
        if state:
            return SafetyRejection(
                SafetyRejectionCode.KILL_SWITCH_ENABLED,
                "the kill switch was enabled after the safety evaluation and before "
                "the broker call; the broker was NOT called",
            )
        return None

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

    def _terminalize_superseded_intent(self, execution_request_id: str) -> None:
        """Best-effort terminalization of THIS submit's freshly journaled,
        provably-unsent intent after losing to a supersession (Issue #116).
        Never raises: the durable supersession marker keeps the parent
        unsendable regardless, and the recovery sweep heals a failure."""
        try:
            markers = execution_store.list_intent_supersessions(
                parent_execution_request_id=execution_request_id,
                db_path=self._order_db_path,
            )
            if not markers:
                return
            self._journal.mark_superseded(
                execution_request_id,
                superseded_by_execution_request_id=markers[0][
                    "child_execution_request_id"
                ],
                at_utc=self._now(),
            )
        except Exception as exc:
            _LOGGER.warning(
                "journal terminalization for superseded intent %s failed: %s; the "
                "entry stays visible via list_unresolved() until "
                "ReissueService.terminalize_superseded_intents() recovers it",
                execution_request_id,
                exc,
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
            # The FULL policy content snapshot is persisted next to every
            # decision (Issue #116): "which policy produced this decision"
            # is bound by content, so the gate-correction eligibility check
            # can identify policy identity beyond the mutable id.
            self._decision_store.record(
                decision, policy_snapshot=self._gate.policy.to_snapshot()
            )
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

    def _journal_write(
        self,
        context: str,
        write,
        execution_request_id: str,
        *,
        recovery_surface: str = "SubmissionJournal.list_unresolved()",
    ) -> None:
        """Best-effort journal write AFTER the broker call: a journal failure
        here must never block recording the broker outcome elsewhere and is
        never converted into a rejection (the order may exist). The entry
        then stays in its previous state -- still not terminal, so it
        remains visible via ``recovery_surface`` (the SUBMISSION queue
        ``SubmissionJournal.list_unresolved()`` by default; cancel-side call
        sites name ``list_unresolved_cancellations()`` instead, PR #124
        Codex review r2 P2 -- the recovery message must point at the queue
        that actually lists the entry). Uses the wall clock as a last
        resort: the injected clock may be exactly what is broken, and
        losing this record over a clock fault would defeat the journal's
        purpose."""
        try:
            try:
                at = self._now()
            except Exception:
                at = datetime.now(timezone.utc)
            write(at)
        except Exception:
            _LOGGER.critical(
                "failed to journal %s for execution_request_id=%s; the journal entry "
                "stays unresolved and remains listed by %s; resolve via reconciliation",
                context,
                execution_request_id,
                recovery_surface,
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
