"""Broker reconciliation -- SAFE ORDER-STATE REPAIR (Issue #104).

*** THE ONLY THING THIS SERVICE MAY CHANGE IS BELIAL'S INTERNAL ORDER
STATE, AND ONLY THROUGH THE EXISTING STATE MACHINE. It never submits an
order, never cancels one, never modifies anything broker-side -- the broker
is used through ``get_order`` exclusively (AST-pinned, plus the poisoned
facades in the tests). It never runs a direct SQL UPDATE against the order
store: the single write path is ``execution_store.record_order_result``,
which enforces the Issue #74 transition validator, the snapshot update
contract and the pre-commit write-back validation. ***

Attempt lifecycle (reservation -> evaluation -> finalize)
---------------------------------------------------------

Input is a RECORDED reconciliation run (Issue #103): :meth:`repair` loads
the run by ``run_id`` from the reconciliation audit DB -- an in-memory,
unrecorded run is refused, so every repair is traceable to a stored
observation. ALL attempt ids are generated and uniqueness-checked BEFORE
any side effect. Then, per discrepancy in observation order (APPLY mode):

1. RESERVE: a ``pending`` audit row (attempt id + run / discrepancy
   identity + observation anchors) is committed BEFORE anything else. A
   duplicate attempt id is refused HERE -- before any state update.
2. EVALUATE (the stored result is NOT trusted blindly):
   - eligibility: only ``EXECUTION_ORDER`` targets classified
     ``AUTO_REPAIR_CANDIDATE`` proceed; ``MANUAL_REVIEW`` / ``QUERY_FAILED``
     and broker-only targets finalize as ``skipped``.
   - the current internal row is re-read; the ``client_order_id`` binding
     must be intact.
   - the broker is RE-QUERIED (``get_order``) -- ALWAYS, including before
     an ``already_applied`` verdict. A ``BrokerError``, not-found, or a
     boundary contract violation finalizes as ``query_failed`` (typed
     ``BrokerErrorCode``; nothing is inferred from a failed query).
   - REVISION gate: the discrepancy carries the order's revision at
     observation time (the execution store's history row count -- it
     increments on EVERY write, same-state re-observations and snapshot
     refreshes included). If the revision moved, the observation is
     outdated: either the order already holds the observed outcome and the
     fresh broker snapshot agrees with the anchored one in full (the
     THREE-WAY check: stored discrepancy x current row/anchored snapshot x
     fresh snapshot) -> ``already_applied``, or -> ``stale`` / ``conflict``.
     Nothing is ever overwritten from an outdated observation.
   - broker reference IDs are never lost or replaced; an id owned by
     another execution request is a ``conflict`` (fail-closed on a lookup
     that cannot answer).
   - the existing validators (transition table, intent identity, anchored
     snapshot update contract, no backwards timestamp) must pass ->
     otherwise ``rejected``. The transition table is reused as-is, never
     widened.
3. APPLY: the full APPLIED attempt record -- ``applied_at_utc`` (= the
   broker snapshot's ``updated_at_utc``, which is exactly what the store
   stamps on the history row), the effective broker id, the sanitized
   detail -- is CONSTRUCTED AND VALIDATED BEFORE the store call. The store
   call is a compare-and-set (``expected_current_state`` +
   ``expected_revision``) and records the STRUCTURED provenance
   (``history.repair_attempt_id``). After a successful commit NOTHING is
   computed any more -- the pre-built attempt is returned as-is, so no
   post-commit exception can ever mislabel a committed repair as FAILED.
4. FINALIZE: the pending audit row is completed (CAS on the pending
   status). If THIS store call fails after a committed update, the attempt
   is returned with ``audit_error`` set and the reservation stays pending:
   the repair is durable, attributable (structured provenance, never
   reason-string parsing) and recoverable.

Crash recovery (:meth:`recover_pending_attempts`): pending reservations are
enumerated from the audit DB; each is finalized under its ORIGINAL attempt
id -- ``applied`` when the execution order state history holds a row whose
``repair_attempt_id`` equals the reservation's id (with that row's
committed timestamp), ``failed`` (``reservation_not_applied``) when no such
row exists, i.e. the crash happened before the state update committed.
Recovery reads the execution DB only; it never grows history. A recovery
whose source run / discrepancy has vanished or no longer matches is refused
(fail-closed integrity error).

Cross-DB consistency (state update vs audit row)
------------------------------------------------

The order-state update (execution orders DB) and the repair audit rows
(reconciliation DB) live in different SQLite files and CANNOT share one
transaction. No distributed-transaction machinery is imitated; the design
is reservation (audit) -> commit (orders) -> finalize (audit), an explicit
outbox-style sequence in which every crash window is recoverable:

- crash before the reservation commits: no trace anywhere, nothing changed;
- crash after the reservation, before the state update: a pending row
  remains; recovery finalizes it as ``failed`` (nothing was changed);
- crash after the state update, before finalize: the pending row remains
  AND the history row carries the attempt id; recovery finalizes it as
  ``applied`` under the original id -- the original outcome, attempt id and
  applied timestamp are never lost.

Failure classification (Issue #104 review)
------------------------------------------

Only OPERATIONALLY EXPECTED, typed failures become attempt outcomes:
``BrokerError`` (plus the documented Issue #103 broker-boundary contract
violation), ``ExecutionStoreError``, ``ReconciliationStoreError``, the
store's documented refusal channel (``ValueError`` /
``BrokerInvalidRequestError`` from ``record_order_result``) and the CAS
conflict. Everything else -- ``AssertionError``, ``KeyError``, model
invariant violations, clock contract failures, programmer errors -- is a
RUN-LEVEL INTEGRITY FAILURE: the pass stops with
:class:`ReconciliationRepairRunError`, which carries the attempts completed
so far (also independently durable in the audit DB), and any half-evaluated
attempt stays pending for recovery. Audit details are SANITIZED: stable
``failure_code`` values plus self-authored descriptions naming at most our
own typed exception classes -- never raw exception text, paths, SQL or
adapter free text (full exceptions go to the log, not the audit DB).

Logging is an AID, never a dependency: every log call is isolated so a
broken handler / formatter cannot change a repair outcome, lose a committed
attempt or stop the pass (the audit DB is the authority).

Dry-run (``RepairMode.DRY_RUN``)
--------------------------------

The full evaluation runs -- reads and broker queries only -- but NOTHING is
written: no order update, no history row, no reservation, no audit row, no
DB creation or migration (a v1 reconciliation DB is refused with a pointer
to ``init_reconciliation_db``; reads never migrate), no state file, no
filesystem side effect. Eligible targets report ``would_apply``.

Idempotency
-----------

Re-running the same repair any number of times: the terminal state stays
what the broker confirmed, the order state history gains NO new rows
(``already_applied`` short-circuits before the store; the revision CAS
closes the race window completely -- same-state writes included), and each
pass appends only its own audit attempts. A target that meanwhile changed
in ANY way is never overwritten (``stale`` / ``conflict`` / the
terminal-state transition rules).
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from broker.broker_interface import Broker
from broker.broker_models import (
    BrokerError,
    BrokerErrorCode,
    BrokerInvalidRequestError,
    BrokerOrderNotFoundError,
    OrderResult,
    OrderState,
    validate_order_result_update,
    validate_order_state_transition,
)
from execution import execution_store
from execution.execution_models import _require_non_empty_str, _require_utc
from execution.execution_store import (
    ExecutionOrderConflictError,
    ExecutionOrderStateConflictError,
    ExecutionStoreError,
)
from execution.reconciliation_models import (
    OrderDiscrepancy,
    ReconciliationRun,
    ReconciliationTargetType,
    ResolutionCategory,
)
from execution.reconciliation_repair_models import (
    OrderRepairAttempt,
    ReconciliationRepairResult,
    RepairAttemptReservation,
    RepairMode,
    RepairOutcome,
)
from execution.reconciliation_store import (
    ReconciliationStoreError,
    finalize_repair_attempt,
    get_reconciliation_run,
    init_reconciliation_db,
    list_pending_repair_attempts,
    reserve_repair_attempt,
)

_LOGGER = logging.getLogger(__name__)

# Outcomes that are expected operation, logged at INFO; everything else is a
# WARNING (the repair deliberately did not happen and someone should look).
_INFO_OUTCOMES: frozenset[RepairOutcome] = frozenset(
    {RepairOutcome.APPLIED, RepairOutcome.WOULD_APPLY, RepairOutcome.ALREADY_APPLIED}
)

# Stable failure codes (Issue #104 review): the machine-readable vocabulary
# persisted on every non-success attempt. Audit details never embed raw
# exception text -- at most these codes plus our own typed class names.
FAILURE_NOT_ELIGIBLE_BROKER_ONLY = "not_eligible_broker_only"
FAILURE_NOT_ELIGIBLE_QUERY_FAILED = "not_eligible_query_failed"
FAILURE_NOT_ELIGIBLE_MANUAL_REVIEW = "not_eligible_manual_review"
FAILURE_CANDIDATE_CONTRACT_VIOLATION = "candidate_contract_violation"
FAILURE_ORDER_MISSING = "order_missing"
FAILURE_ORDER_WITHOUT_BROKER_STATE = "order_without_broker_state"
FAILURE_BINDING_MISMATCH = "client_order_id_binding_mismatch"
FAILURE_STORE_READ_FAILED = "execution_store_read_failed"
FAILURE_OWNER_LOOKUP_FAILED = "broker_id_owner_lookup_failed"
FAILURE_MISSING_OBSERVED_REVISION = "missing_observed_revision"
FAILURE_INTERNAL_CHANGED = "internal_order_changed"
FAILURE_BROKER_ANSWERED_FOREIGN_ORDER = "broker_answered_foreign_order"
FAILURE_REQUERY_DISAGREES = "requery_disagrees_with_observation"
FAILURE_BROKER_REFERENCE_CHANGED = "broker_reference_changed"
FAILURE_BROKER_REFERENCE_OWNED_ELSEWHERE = "broker_reference_owned_elsewhere"
FAILURE_ALREADY_APPLIED_MISMATCH = "already_applied_content_mismatch"
FAILURE_IDENTITY_MISMATCH = "snapshot_identity_mismatch"
FAILURE_FORBIDDEN_TRANSITION = "forbidden_transition"
FAILURE_SNAPSHOT_CONTRACT = "snapshot_contract_violation"
FAILURE_SNAPSHOT_OLDER = "snapshot_older_than_stored_state"
FAILURE_BROKER_QUERY_FAILED = "broker_query_failed"
FAILURE_BROKER_NOT_FOUND = "broker_order_not_found"
FAILURE_BROKER_CONTRACT_VIOLATION = "broker_contract_violation"
FAILURE_UNIQUENESS_CONFLICT = "uniqueness_conflict"
FAILURE_STORE_REFUSED_UPDATE = "store_refused_update"
FAILURE_STORE_UPDATE_FAILED = "store_update_failed"
FAILURE_CAS_UNRESOLVABLE = "cas_conflict_unresolvable"
FAILURE_RESERVATION_NOT_APPLIED = "reservation_not_applied"


class ReconciliationRepairError(RuntimeError):
    """A repair pass could not run at all (bad input, unrecorded run,
    missing audit DB, duplicate attempt ids from the factory). Raised BEFORE
    any state change. Per-target problems never raise this -- they become
    typed :class:`RepairOutcome` values on the attempt."""


class ReconciliationRepairRunError(ReconciliationRepairError):
    """The pass stopped on a run-level integrity failure (unexpected
    exception, clock contract failure, result construction failure).

    ``attempts`` carries every attempt COMPLETED before the stop, so a
    caller never loses committed partial results -- and the same attempts
    are independently durable in the audit DB
    (``reconciliation_store.list_repair_attempts``); a half-processed
    target stays pending there for :meth:`recover_pending_attempts`.
    """

    def __init__(self, message: str, *, attempts: tuple[OrderRepairAttempt, ...]):
        super().__init__(message)
        self.attempts = attempts


def _default_attempt_id() -> str:
    return f"repair-{os.urandom(16).hex()}"


class BrokerOrderReconciliationRepairService:
    """Reflect AUTO_REPAIR_CANDIDATE observations into the order store --
    idempotently, through the existing state machine only (Issue #104).

    ``execution_db_path`` is the execution ORDER store (written exclusively
    through ``record_order_result``). ``reconciliation_db_path`` is the
    Issue #103 audit DB the run is loaded from and (in APPLY mode) the
    attempt reservations / finalizations are written to. ``clock`` supplies
    the repair-side timestamps (injected in tests; defaults to
    ``datetime.now(timezone.utc)``) and must return timezone-aware UTC
    datetimes -- a clock that violates the contract stops the pass BEFORE
    the next attempt reserves, never after a commit (post-commit code calls
    no clock). ``attempt_id_factory`` supplies the per-attempt audit ids
    (defaults to a random hex id; injected in tests for determinism); all
    ids for a pass are generated and uniqueness-checked up front.
    """

    def __init__(
        self,
        *,
        broker: Broker,
        execution_db_path: Path | str,
        reconciliation_db_path: Path | str,
        clock: Callable[[], datetime] | None = None,
        attempt_id_factory: Callable[[], str] | None = None,
    ):
        if not isinstance(broker, Broker):
            raise ValueError(
                f"broker must implement the Broker protocol (Issue #73), got {broker!r}"
            )
        if clock is not None and not callable(clock):
            raise ValueError(f"clock must be a callable returning a datetime, got {clock!r}")
        if attempt_id_factory is not None and not callable(attempt_id_factory):
            raise ValueError(
                f"attempt_id_factory must be a callable returning a string, got "
                f"{attempt_id_factory!r}"
            )
        self._broker = broker
        self._execution_db_path = Path(execution_db_path)
        self._reconciliation_db_path = Path(reconciliation_db_path)
        self._clock = clock
        self._attempt_id_factory = attempt_id_factory or _default_attempt_id

    # --- public API ---------------------------------------------------------

    def repair(
        self, *, run_id: str, mode: RepairMode = RepairMode.APPLY
    ) -> ReconciliationRepairResult:
        """Process every discrepancy of the RECORDED run ``run_id``.

        APPLY mode verifies (and, for a valid v1 file, migrates) the
        reconciliation audit DB first -- it must already exist, holding the
        run; a missing DB or an unrecorded run raises
        :class:`ReconciliationRepairError` before anything is touched.
        DRY_RUN mode reads the same DB strictly read-only and refuses a v1
        schema instead of migrating it (reads never migrate).
        """
        _require_non_empty_str(run_id, "run_id")
        if not isinstance(mode, RepairMode):
            raise ReconciliationRepairError(f"mode must be a RepairMode, got {mode!r}")
        started_at = self._now()  # clock contract verified before any side effect
        db_path = self._reconciliation_db_path
        if mode is RepairMode.APPLY:
            if not db_path.exists():
                raise ReconciliationRepairError(
                    f"broker reconciliation DB at {db_path} does not exist; repair acts on "
                    "recorded reconciliation runs only (record one via "
                    "BrokerOrderReconciliationService.run() first)"
                )
            # Idempotent verify; migrates a valid v1 audit DB to v2. The file
            # exists, so this never creates a DB as a side effect.
            init_reconciliation_db(db_path)
        run = get_reconciliation_run(run_id, db_path=db_path)
        if run is None:
            raise ReconciliationRepairError(
                f"reconciliation run {run_id!r} is not recorded in {db_path}; repair acts "
                "on recorded runs only (BrokerOrderReconciliationService.run())"
            )

        # ALL attempt ids up front: a broken factory (exception, empty value,
        # duplicate) fails the pass HERE, before any reservation or state
        # change anywhere.
        attempt_ids = tuple(self._attempt_id_factory() for _ in run.discrepancies)
        for attempt_id in attempt_ids:
            _require_non_empty_str(attempt_id, "attempt_id")
        if len(set(attempt_ids)) != len(attempt_ids):
            raise ReconciliationRepairError(
                "attempt_id_factory produced duplicate ids within one repair pass; "
                "refusing to start (no reservation and no state update was made)"
            )

        attempts: list[OrderRepairAttempt] = []
        try:
            for discrepancy, attempt_id in zip(run.discrepancies, attempt_ids):
                attempt = self._repair_one(run, discrepancy, attempt_id, mode)
                attempts.append(attempt)
                self._safe_log_attempt(attempt)
        except Exception as exc:
            # Run-level integrity failure (unexpected exception, clock
            # contract, store unavailability at reserve time, ...): stop, but
            # never lose what already committed -- the finalized attempts are
            # in the audit DB and returned on the error; a half-processed
            # target stays pending there for recover_pending_attempts().
            raise ReconciliationRepairRunError(
                f"repair pass over run {run_id!r} stopped after "
                f"{len(attempts)}/{len(run.discrepancies)} attempts "
                f"({type(exc).__name__}); completed attempts are recorded in the audit DB "
                "and attached to this error",
                attempts=tuple(attempts),
            ) from exc

        try:
            finished_at = self._now()
            result = ReconciliationRepairResult(
                run_id=run_id,
                mode=mode,
                started_at_utc=started_at,
                finished_at_utc=finished_at,
                attempts=tuple(attempts),
            )
        except Exception as exc:
            # The pass itself is DONE -- every attempt evaluated and audited.
            # A finished-clock failure or result-construction failure must
            # not hide that from the caller.
            raise ReconciliationRepairRunError(
                f"repair pass over run {run_id!r} completed all "
                f"{len(attempts)} attempts but the pass result could not be built "
                f"({type(exc).__name__}); the attempts are recorded in the audit DB and "
                "attached to this error",
                attempts=tuple(attempts),
            ) from exc
        self._safe_log_result(result)
        return result

    def recover_pending_attempts(
        self, *, run_id: str | None = None
    ) -> tuple[OrderRepairAttempt, ...]:
        """Finalize reservations a crashed pass left ``pending``.

        For each pending reservation (optionally restricted to one run), the
        execution order state history is checked for a row whose STRUCTURED
        ``repair_attempt_id`` column equals the reservation's attempt id:

        - found   -> the state update COMMITTED before the crash; the
          attempt is finalized ``applied`` under its ORIGINAL id, with the
          committed history row's state / broker id / timestamp.
        - missing -> nothing ever committed for this attempt; finalized
          ``failed`` (``reservation_not_applied``).

        Recovery reads the execution DB only (never grows history) and is
        fail-closed: a reservation whose source run / discrepancy has been
        deleted or tampered with is refused with
        :class:`ReconciliationRepairError` instead of being guessed about.
        Concurrent recoveries are safe -- finalize is compare-and-set, the
        loser observes ``RepairAttemptConflictError``.
        """
        db_path = self._reconciliation_db_path
        pending = list_pending_repair_attempts(run_id=run_id, db_path=db_path)
        recovered: list[OrderRepairAttempt] = []
        for reservation in pending:
            source_run = get_reconciliation_run(reservation.run_id, db_path=db_path)
            discrepancy = None
            if source_run is not None:
                for entry in source_run.discrepancies:
                    if (
                        entry.target_type is reservation.target_type
                        and entry.target_id == reservation.target_id
                    ):
                        discrepancy = entry
                        break
            if (
                discrepancy is None
                or discrepancy.internal_state is not reservation.reconciliation_internal_state
                or discrepancy.broker_state is not reservation.reconciliation_broker_state
                or discrepancy.internal_revision != reservation.internal_revision
            ):
                raise ReconciliationRepairError(
                    f"pending repair attempt {reservation.attempt_id!r} refers to "
                    f"reconciliation run {reservation.run_id!r} / target "
                    f"{reservation.target_id!r}, whose stored observation is missing or "
                    "no longer matches the reservation; refusing to recover from a "
                    "tampered audit trail"
                )
            attempt = self._recovered_attempt(reservation)
            finalize_repair_attempt(attempt, db_path=db_path)
            self._safe_log_attempt(attempt)
            recovered.append(attempt)
        return tuple(recovered)

    # --- one discrepancy ----------------------------------------------------

    def _repair_one(
        self,
        run: ReconciliationRun,
        discrepancy: OrderDiscrepancy,
        attempt_id: str,
        mode: RepairMode,
    ) -> OrderRepairAttempt:
        reserved_at = self._now()

        def make(
            *,
            outcome: RepairOutcome,
            detail: str,
            failure_code: str | None = None,
            before: OrderState | None = None,
            after: OrderState | None = None,
            broker_order_id: str | None = None,
            broker_error_code: BrokerErrorCode | None = None,
            applied_at: datetime | None = None,
        ) -> OrderRepairAttempt:
            is_execution = discrepancy.target_type is ReconciliationTargetType.EXECUTION_ORDER
            return OrderRepairAttempt(
                attempt_id=attempt_id,
                run_id=run.run_id,
                mode=mode,
                target_type=discrepancy.target_type,
                target_id=discrepancy.target_id,
                outcome=outcome,
                detail=detail,
                reserved_at_utc=reserved_at,
                execution_request_id=discrepancy.target_id if is_execution else None,
                client_order_id=discrepancy.client_order_id,
                reconciliation_internal_state=discrepancy.internal_state,
                reconciliation_broker_state=discrepancy.broker_state,
                internal_revision=discrepancy.internal_revision,
                internal_state_before=before,
                internal_state_after=after,
                broker_order_id=(
                    discrepancy.broker_order_id if broker_order_id is None else broker_order_id
                ),
                broker_error_code=broker_error_code,
                failure_code=failure_code,
                observed_at_utc=discrepancy.observed_at_utc,
                applied_at_utc=applied_at,
            )

        if mode is RepairMode.APPLY:
            # Reservation FIRST: the attempt id is durable (and a duplicate
            # refused) before anything is evaluated or written. A failure
            # here propagates -- nothing was changed yet.
            reserve_repair_attempt(
                self._reservation(run, discrepancy, attempt_id, reserved_at),
                db_path=self._reconciliation_db_path,
            )
        attempt = self._evaluate(run, discrepancy, attempt_id, mode, make)
        if mode is RepairMode.APPLY:
            attempt = self._finalize(attempt)
        return attempt

    @staticmethod
    def _reservation(
        run: ReconciliationRun,
        discrepancy: OrderDiscrepancy,
        attempt_id: str,
        reserved_at: datetime,
    ) -> RepairAttemptReservation:
        is_execution = discrepancy.target_type is ReconciliationTargetType.EXECUTION_ORDER
        return RepairAttemptReservation(
            attempt_id=attempt_id,
            run_id=run.run_id,
            target_type=discrepancy.target_type,
            target_id=discrepancy.target_id,
            reserved_at_utc=reserved_at,
            detail="reserved: repair evaluation started",
            execution_request_id=discrepancy.target_id if is_execution else None,
            client_order_id=discrepancy.client_order_id,
            reconciliation_internal_state=discrepancy.internal_state,
            reconciliation_broker_state=discrepancy.broker_state,
            internal_revision=discrepancy.internal_revision,
            observed_at_utc=discrepancy.observed_at_utc,
        )

    def _evaluate(
        self,
        run: ReconciliationRun,
        discrepancy: OrderDiscrepancy,
        attempt_id: str,
        mode: RepairMode,
        make,
    ) -> OrderRepairAttempt:
        skipped = RepairOutcome.SKIPPED
        if discrepancy.target_type is not ReconciliationTargetType.EXECUTION_ORDER:
            return make(
                outcome=skipped,
                failure_code=FAILURE_NOT_ELIGIBLE_BROKER_ONLY,
                detail=(
                    "broker-only orders are never adopted automatically "
                    f"(resolution {discrepancy.resolution_category.value!r})"
                ),
            )
        if discrepancy.resolution_category is ResolutionCategory.QUERY_FAILED:
            return make(
                outcome=skipped,
                failure_code=FAILURE_NOT_ELIGIBLE_QUERY_FAILED,
                detail=(
                    "the reconciliation query itself failed -- nothing was observed, so "
                    "there is nothing to reflect"
                ),
            )
        if discrepancy.resolution_category is not ResolutionCategory.AUTO_REPAIR_CANDIDATE:
            return make(
                outcome=skipped,
                failure_code=FAILURE_NOT_ELIGIBLE_MANUAL_REVIEW,
                detail=(
                    f"resolution category {discrepancy.resolution_category.value!r} is never "
                    "auto-repaired; a human decides"
                ),
            )
        if discrepancy.broker_state is None or discrepancy.broker_state is OrderState.UNKNOWN:
            # A stored candidate without a confirmable broker outcome violates
            # the Issue #103 classification contract -- fail closed.
            return make(
                outcome=RepairOutcome.REJECTED,
                failure_code=FAILURE_CANDIDATE_CONTRACT_VIOLATION,
                detail=(
                    "the stored auto-repair candidate carries no confirmable broker "
                    "state; refusing to trust it"
                ),
            )

        request_id = discrepancy.target_id

        # --- current internal row (re-validation, never trusted from the run)
        try:
            row = execution_store.get_execution_order(
                request_id, db_path=self._execution_db_path
            )
        except ExecutionStoreError as exc:
            return make(
                outcome=RepairOutcome.FAILED,
                failure_code=FAILURE_STORE_READ_FAILED,
                detail=(
                    f"reading the execution order store failed ({type(exc).__name__}); "
                    "nothing was changed"
                ),
            )
        if row is None:
            return make(
                outcome=RepairOutcome.FAILED,
                failure_code=FAILURE_ORDER_MISSING,
                detail=(
                    f"no execution order {request_id!r} exists in the order store; the "
                    "stored discrepancy cannot be re-validated and nothing was changed"
                ),
            )
        if discrepancy.client_order_id is None or row["client_order_id"] != discrepancy.client_order_id:
            return make(
                outcome=RepairOutcome.REJECTED,
                failure_code=FAILURE_BINDING_MISMATCH,
                detail=(
                    f"the client_order_id binding is broken: the discrepancy observed "
                    f"{discrepancy.client_order_id!r} but the order store binds "
                    f"{row['client_order_id']!r}; refusing to repair across a broken "
                    "identity"
                ),
            )
        if row["order_state"] is None:
            return make(
                outcome=RepairOutcome.FAILED,
                failure_code=FAILURE_ORDER_WITHOUT_BROKER_STATE,
                detail=(
                    "the execution order has no broker state (phase 'created'); the stored "
                    "discrepancy cannot describe this row and nothing was changed"
                ),
            )
        current = OrderState(row["order_state"])

        # --- broker re-query: ALWAYS, before any verdict (already_applied
        # included) -- an outcome is never confirmed without asking again
        try:
            fresh = self._broker.get_order(row["client_order_id"])
            if not isinstance(fresh, OrderResult):
                raise TypeError(
                    "Broker.get_order() must return an OrderResult, got "
                    f"{type(fresh).__name__}"
                )
        except BrokerOrderNotFoundError:
            return make(
                outcome=RepairOutcome.QUERY_FAILED,
                broker_error_code=BrokerErrorCode.NOT_FOUND,
                failure_code=FAILURE_BROKER_NOT_FOUND,
                before=current,
                detail=(
                    "the broker no longer answers about this client_order_id; this "
                    "contradicts the reconciliation observation and proves nothing, so "
                    "the internal state stays untouched"
                ),
            )
        except BrokerError as exc:
            return make(
                outcome=RepairOutcome.QUERY_FAILED,
                broker_error_code=exc.code,
                failure_code=FAILURE_BROKER_QUERY_FAILED,
                before=current,
                detail=(
                    f"repair-time broker query failed ({type(exc).__name__}); a failed "
                    "query never settles an order, so the internal state stays untouched"
                ),
            )
        except Exception as exc:
            # The Issue #73/#103 broker boundary contract: adapters translate
            # transport/malformed-response failures into BrokerError. A leak
            # across that DOCUMENTED boundary is recorded fail-closed as a
            # scoped contract violation (same stance as detection) -- this is
            # a defined boundary conversion, not a general catch-all.
            self._safe_log_exception(
                "broker.get_order violated the Broker error/return contract", exc
            )
            return make(
                outcome=RepairOutcome.QUERY_FAILED,
                broker_error_code=BrokerErrorCode.COMMUNICATION_FAILURE,
                failure_code=FAILURE_BROKER_CONTRACT_VIOLATION,
                before=current,
                detail=(
                    "repair-time broker query violated the Broker error/return contract; "
                    "recorded fail-closed and the internal state stays untouched"
                ),
            )

        if fresh.client_order_id != row["client_order_id"]:
            return make(
                outcome=RepairOutcome.CONFLICT,
                failure_code=FAILURE_BROKER_ANSWERED_FOREIGN_ORDER,
                before=current,
                detail=(
                    f"the broker answered about client_order_id {fresh.client_order_id!r} "
                    f"instead of {row['client_order_id']!r}"
                ),
            )

        # --- revision gate: ANY change since the observation (state changes
        # and same-state writes alike) invalidates the stored classification
        if discrepancy.internal_revision is None:
            return make(
                outcome=RepairOutcome.STALE,
                failure_code=FAILURE_MISSING_OBSERVED_REVISION,
                before=current,
                broker_order_id=row["broker_order_id"],
                detail=(
                    "the stored observation predates revision tracking (schema v1), so "
                    "it cannot be proven current; run a fresh reconciliation"
                ),
            )
        if row["revision"] != discrepancy.internal_revision:
            return self._changed_since_observation(discrepancy, row, current, fresh, make)
        if current is not discrepancy.internal_state:
            # same revision but different state would mean the revision
            # bookkeeping is broken -- fail closed, repair nothing
            return make(
                outcome=RepairOutcome.CONFLICT,
                failure_code=FAILURE_CANDIDATE_CONTRACT_VIOLATION,
                before=current,
                detail=(
                    f"the order is at the observed revision {row['revision']} but in state "
                    f"{current.value!r} instead of the observed "
                    f"{discrepancy.internal_state.value!r}; the stored observation is not "
                    "trustworthy"
                ),
            )

        if fresh.state is not discrepancy.broker_state:
            return make(
                outcome=RepairOutcome.CONFLICT,
                failure_code=FAILURE_REQUERY_DISAGREES,
                before=current,
                detail=(
                    f"the repair-time re-query reports {fresh.state.value!r} but the "
                    f"reconciliation run observed {discrepancy.broker_state.value!r}; the "
                    "stored observation is outdated -- a fresh reconciliation run must "
                    "re-classify before anything is reflected"
                ),
            )
        reference_conflict = self._reference_conflict(discrepancy, row, fresh)
        if reference_conflict is not None:
            return make(
                outcome=RepairOutcome.CONFLICT,
                failure_code=FAILURE_BROKER_REFERENCE_CHANGED,
                before=current,
                detail=reference_conflict,
            )
        owner_failure, owner_conflict = self._broker_id_owner_problem(request_id, fresh)
        if owner_failure is not None:
            return make(
                outcome=RepairOutcome.FAILED,
                failure_code=FAILURE_OWNER_LOOKUP_FAILED,
                before=current,
                detail=owner_failure,
            )
        if owner_conflict is not None:
            return make(
                outcome=RepairOutcome.CONFLICT,
                failure_code=FAILURE_BROKER_REFERENCE_OWNED_ELSEWHERE,
                before=current,
                detail=owner_conflict,
            )
        try:
            rejection = self._contract_rejection(request_id, row, current, fresh)
        except ExecutionStoreError as exc:
            # fail-closed: a store read that cannot answer never proves
            # repairability
            return make(
                outcome=RepairOutcome.FAILED,
                failure_code=FAILURE_STORE_READ_FAILED,
                before=current,
                detail=(
                    f"reading the execution order store failed while proving "
                    f"repairability ({type(exc).__name__}); nothing was changed"
                ),
            )
        if rejection is not None:
            code, detail = rejection
            return make(
                outcome=RepairOutcome.REJECTED,
                failure_code=code,
                before=current,
                detail=detail,
            )

        effective_broker_id = fresh.broker_order_id or row["broker_order_id"]

        if mode is RepairMode.DRY_RUN:
            return make(
                outcome=RepairOutcome.WOULD_APPLY,
                before=current,
                after=fresh.state,
                broker_order_id=effective_broker_id,
                detail=(
                    f"eligible: the broker-confirmed state {fresh.state.value!r} would be "
                    f"recorded over the internal state {current.value!r} through the "
                    "existing state machine; dry-run wrote nothing"
                ),
            )

        # --- apply. EVERYTHING the APPLIED attempt needs is computed and
        # validated BEFORE the store call: applied_at is the broker
        # snapshot's updated_at (exactly the timestamp the store stamps on
        # the committed history row), the broker id is the same effective id
        # the store derives, the attempt object itself is constructed now.
        # After a successful commit nothing is computed any more, so no
        # post-commit failure can mislabel a committed repair.
        reason = (
            f"broker reconciliation repair (run_id={run.run_id}, attempt_id={attempt_id}): "
            f"the broker confirmed state {fresh.state.value!r} for client_order_id "
            f"{row['client_order_id']!r}"
        )
        applied_attempt = make(
            outcome=RepairOutcome.APPLIED,
            before=current,
            after=fresh.state,
            broker_order_id=effective_broker_id,
            applied_at=fresh.updated_at_utc,
            detail=reason,
        )
        try:
            execution_store.record_order_result(
                request_id,
                fresh,
                reason=reason,
                expected_current_state=current,
                expected_revision=discrepancy.internal_revision,
                repair_attempt_id=attempt_id,
                db_path=self._execution_db_path,
            )
        except ExecutionOrderStateConflictError:
            return self._concurrent_loss(discrepancy, current, fresh, make)
        except ExecutionOrderConflictError as exc:
            self._safe_log_exception("conditional repair update hit a uniqueness conflict", exc)
            return make(
                outcome=RepairOutcome.CONFLICT,
                failure_code=FAILURE_UNIQUENESS_CONFLICT,
                before=current,
                detail=(
                    "the update collided with a uniqueness constraint "
                    "(ExecutionOrderConflictError) and was rolled back; nothing was changed"
                ),
            )
        except (ValueError, BrokerInvalidRequestError) as exc:
            self._safe_log_exception("the execution store refused the repair update", exc)
            return make(
                outcome=RepairOutcome.REJECTED,
                failure_code=FAILURE_STORE_REFUSED_UPDATE,
                before=current,
                detail=(
                    f"the execution store refused the update ({type(exc).__name__}); the "
                    "transaction rolled back and nothing was changed"
                ),
            )
        except ExecutionStoreError as exc:
            self._safe_log_exception("the execution store failed during the repair update", exc)
            return make(
                outcome=RepairOutcome.FAILED,
                failure_code=FAILURE_STORE_UPDATE_FAILED,
                before=current,
                detail=(
                    f"the execution store failed during the update ({type(exc).__name__}); "
                    "the transaction rolled back and nothing was changed"
                ),
            )
        return applied_attempt

    # --- helpers ------------------------------------------------------------

    def _changed_since_observation(
        self,
        discrepancy: OrderDiscrepancy,
        row: dict,
        current: OrderState,
        fresh: OrderResult,
        make,
    ) -> OrderRepairAttempt:
        """The order changed after the reconciliation observation. Either it
        already holds the observed outcome -- provable ONLY by the three-way
        agreement of the stored discrepancy, the current row / anchored
        snapshot and the fresh broker snapshot -- or the observation is
        stale."""
        if current is not discrepancy.broker_state:
            return make(
                outcome=RepairOutcome.STALE,
                failure_code=FAILURE_INTERNAL_CHANGED,
                before=current,
                broker_order_id=row["broker_order_id"],
                detail=(
                    f"the order changed since the observation (revision "
                    f"{discrepancy.internal_revision} -> {row['revision']}) and is now "
                    f"{current.value!r}, not the observed outcome "
                    f"{discrepancy.broker_state.value!r}; a stale observation never "
                    "overwrites newer state"
                ),
            )
        mismatch = self._already_applied_mismatch(discrepancy, row, fresh)
        if mismatch is not None:
            return make(
                outcome=RepairOutcome.CONFLICT,
                failure_code=FAILURE_ALREADY_APPLIED_MISMATCH,
                before=current,
                broker_order_id=row["broker_order_id"],
                detail=(
                    f"the internal state already reads {current.value!r} but the "
                    f"three-way verification failed ({mismatch}); a fresh reconciliation "
                    "run must re-classify"
                ),
            )
        return make(
            outcome=RepairOutcome.ALREADY_APPLIED,
            before=current,
            after=current,
            broker_order_id=row["broker_order_id"],
            detail=(
                f"the internal order already holds the broker-observed outcome "
                f"{current.value!r} and the fresh broker snapshot agrees in full; no new "
                "state and no new history row were written"
            ),
        )

    def _already_applied_mismatch(
        self, discrepancy: OrderDiscrepancy, row: dict, fresh: OrderResult
    ) -> str | None:
        """Why ALREADY_APPLIED may NOT be concluded (None = the three-way
        check holds).

        Verified across the stored discrepancy, the current row + anchored
        snapshot and the fresh snapshot: state, client/broker order ids, and
        the COMPLETE snapshot content (identity, quantities, prices, fills,
        reject reason -- every ``OrderResult`` field except the
        re-observation timestamp, which may only move forward)."""
        if fresh.state is not discrepancy.broker_state:
            return (
                f"the fresh broker state {fresh.state.value!r} differs from the observed "
                f"{discrepancy.broker_state.value!r}"
            )
        if fresh.client_order_id != row["client_order_id"]:
            return "the fresh snapshot answers about a different client_order_id"
        if fresh.broker_order_id != row["broker_order_id"]:
            return (
                f"the fresh broker_order_id {fresh.broker_order_id!r} differs from the "
                f"recorded {row['broker_order_id']!r}"
            )
        if (
            discrepancy.broker_order_id is not None
            and discrepancy.broker_order_id != row["broker_order_id"]
        ):
            return (
                f"the recorded broker_order_id {row['broker_order_id']!r} differs from "
                f"the observed {discrepancy.broker_order_id!r}"
            )
        anchored = execution_store.load_order_result(
            discrepancy.target_id, db_path=self._execution_db_path
        )
        if anchored is None:
            return "the internal order carries no anchored broker snapshot to compare"
        if fresh.updated_at_utc < anchored.updated_at_utc:
            return "the fresh snapshot is older than the recorded one"
        try:
            validate_order_result_update(anchored, fresh)
        except (ValueError, BrokerInvalidRequestError) as exc:
            return f"the fresh snapshot violates the update contract ({type(exc).__name__})"
        mismatched = sorted(
            name
            for name in (
                "state",
                "symbol",
                "side",
                "order_type",
                "requested_quantity",
                "filled_quantity",
                "remaining_quantity",
                "requested_price",
                "average_fill_price",
                "fills",
                "reject_reason",
            )
            if getattr(anchored, name) != getattr(fresh, name)
        )
        if mismatched:
            return (
                "the fresh snapshot content differs from the recorded one "
                f"(differing: {', '.join(mismatched)})"
            )
        return None

    def _concurrent_loss(
        self,
        discrepancy: OrderDiscrepancy,
        expected: OrderState,
        fresh: OrderResult,
        make,
    ) -> OrderRepairAttempt:
        """The compare-and-set lost: re-read and classify what happened."""
        try:
            row = execution_store.get_execution_order(
                discrepancy.target_id, db_path=self._execution_db_path
            )
        except ExecutionStoreError as exc:
            return make(
                outcome=RepairOutcome.FAILED,
                failure_code=FAILURE_CAS_UNRESOLVABLE,
                before=expected,
                detail=(
                    "the conditional update was refused (the order changed concurrently) "
                    f"and the re-read failed ({type(exc).__name__}); nothing was changed "
                    "by this attempt"
                ),
            )
        if row is None or row["order_state"] is None:
            return make(
                outcome=RepairOutcome.FAILED,
                failure_code=FAILURE_CAS_UNRESOLVABLE,
                before=expected,
                detail=(
                    "the conditional update was refused (the order changed concurrently) "
                    "and the order could not be re-read into a broker-tracked state; "
                    "nothing was changed by this attempt"
                ),
            )
        current = OrderState(row["order_state"])
        return self._changed_since_observation(discrepancy, row, current, fresh, make)

    @staticmethod
    def _reference_conflict(
        discrepancy: OrderDiscrepancy, row: dict, fresh: OrderResult
    ) -> str | None:
        """Whether the fresh snapshot would lose / replace a known broker
        reference ID or contradicts the observed one."""
        stored_id = row["broker_order_id"]
        if stored_id is not None and fresh.broker_order_id != stored_id:
            return (
                f"the known broker_order_id {stored_id!r} would be "
                + (
                    "dropped"
                    if fresh.broker_order_id is None
                    else f"replaced by {fresh.broker_order_id!r}"
                )
                + "; a known broker reference is never overwritten or lost"
            )
        effective_fresh_id = fresh.broker_order_id or stored_id
        if effective_fresh_id != discrepancy.broker_order_id:
            return (
                f"the broker reference changed between observation and repair "
                f"({discrepancy.broker_order_id!r} -> {effective_fresh_id!r}); a fresh "
                "reconciliation run must re-classify"
            )
        return None

    def _broker_id_owner_problem(
        self, request_id: str, fresh: OrderResult
    ) -> tuple[str | None, str | None]:
        """``(store_failure, conflict)`` for the DB-wide broker-id uniqueness
        check; read-only, fail-closed on a store read failure."""
        if fresh.broker_order_id is None:
            return None, None
        try:
            owner = execution_store.get_execution_order_by_broker_order_id(
                fresh.broker_order_id, db_path=self._execution_db_path
            )
        except ExecutionStoreError as exc:
            return (
                f"the broker_order_id ownership lookup failed ({type(exc).__name__}); "
                "repairability is never assumed from a lookup that did not answer, so "
                "nothing was changed",
                None,
            )
        if owner is None or owner["execution_request_id"] == request_id:
            return None, None
        return None, (
            f"broker_order_id {fresh.broker_order_id!r} is already owned by execution "
            f"request {owner['execution_request_id']!r}, not by {request_id!r}; broker "
            "order IDs are UNIQUE across the execution order store, so this observation "
            "cannot be recorded onto this request"
        )

    def _contract_rejection(
        self, request_id: str, row: dict, current: OrderState, fresh: OrderResult
    ) -> tuple[str, str] | None:
        """``(failure_code, detail)`` when the existing state machine / store
        contract refuses the update -- the same checks
        ``record_order_result`` enforces, run first so the refusal is
        classified (REJECTED) instead of surfacing as a store exception.
        A store read failure propagates as ``ExecutionStoreError``; the
        caller classifies it FAILED (fail-closed), never REJECTED."""
        stored_price = None if row["requested_price"] is None else Decimal(row["requested_price"])
        mismatches = [
            name
            for name, stored, seen in (
                ("symbol", row["symbol"], fresh.symbol),
                ("side", row["side"], fresh.side.value),
                ("order_type", row["order_type"], fresh.order_type.value),
                ("quantity", Decimal(row["quantity"]), fresh.requested_quantity),
                ("requested_price", stored_price, fresh.requested_price),
            )
            if stored != seen
        ]
        if mismatches:
            return (
                FAILURE_IDENTITY_MISMATCH,
                "the broker snapshot does not describe the stored execution request "
                f"(differing: {', '.join(mismatches)})",
            )
        try:
            validate_order_state_transition(current, fresh.state)
        except (ValueError, BrokerInvalidRequestError):
            return (
                FAILURE_FORBIDDEN_TRANSITION,
                f"the order state machine forbids the transition {current.value!r} -> "
                f"{fresh.state.value!r}",
            )
        anchored = execution_store.load_order_result(request_id, db_path=self._execution_db_path)
        if anchored is not None:
            try:
                validate_order_result_update(anchored, fresh)
            except (ValueError, BrokerInvalidRequestError) as exc:
                return (
                    FAILURE_SNAPSHOT_CONTRACT,
                    "the snapshot update contract refuses this observation "
                    f"({type(exc).__name__})",
                )
        stored_updated_at = datetime.fromisoformat(row["updated_at_utc"])
        if fresh.updated_at_utc < stored_updated_at:
            return (
                FAILURE_SNAPSHOT_OLDER,
                f"the broker snapshot is older than the stored state "
                f"({fresh.updated_at_utc.isoformat()} < {row['updated_at_utc']})",
            )
        return None

    def _recovered_attempt(self, reservation: RepairAttemptReservation) -> OrderRepairAttempt:
        """The finalized view of a crashed pending attempt, decided by the
        STRUCTURED history provenance (``repair_attempt_id``)."""
        base = dict(
            attempt_id=reservation.attempt_id,
            run_id=reservation.run_id,
            mode=RepairMode.APPLY,
            target_type=reservation.target_type,
            target_id=reservation.target_id,
            reserved_at_utc=reservation.reserved_at_utc,
            execution_request_id=reservation.execution_request_id,
            client_order_id=reservation.client_order_id,
            reconciliation_internal_state=reservation.reconciliation_internal_state,
            reconciliation_broker_state=reservation.reconciliation_broker_state,
            internal_revision=reservation.internal_revision,
            observed_at_utc=reservation.observed_at_utc,
        )
        committed = None
        if reservation.execution_request_id is not None:
            history = execution_store.get_state_history(
                reservation.execution_request_id, db_path=self._execution_db_path
            )
            for entry in history:
                if entry["repair_attempt_id"] == reservation.attempt_id:
                    committed = entry
                    break
        if committed is None:
            return OrderRepairAttempt(
                outcome=RepairOutcome.FAILED,
                failure_code=FAILURE_RESERVATION_NOT_APPLIED,
                detail=(
                    "recovered pending reservation: no state-history row carries this "
                    "attempt id, so the state update never committed; nothing was changed"
                ),
                **base,
            )
        applied_state = OrderState(committed["new_state"])
        applied_at = datetime.fromisoformat(committed["changed_at_utc"])
        return OrderRepairAttempt(
            outcome=RepairOutcome.APPLIED,
            detail=(
                "recovered pending reservation: the state history carries this attempt id "
                f"(state {applied_state.value!r} committed before the crash); finalized "
                "under the original attempt id"
            ),
            internal_state_before=reservation.reconciliation_internal_state,
            internal_state_after=applied_state,
            broker_order_id=committed["broker_order_id"],
            applied_at_utc=applied_at,
            **base,
        )

    def _finalize(self, attempt: OrderRepairAttempt) -> OrderRepairAttempt:
        """Complete the reserved audit row; on failure attach ``audit_error``
        instead of losing the attempt (the reservation stays pending and the
        structured history provenance lets recovery finalize it under the
        SAME attempt id -- see the module docstring)."""
        try:
            finalize_repair_attempt(attempt, db_path=self._reconciliation_db_path)
        except ReconciliationStoreError as exc:
            attempt = replace(
                attempt,
                audit_error=(
                    f"{type(exc).__name__}: finalizing the reserved audit row failed; the "
                    "reservation stays pending and recover_pending_attempts() completes "
                    "it under the same attempt id"
                ),
            )
            self._safe_log_exception(
                f"repair attempt {attempt.attempt_id} (run {attempt.run_id}, target "
                f"{attempt.target_id}) had outcome {attempt.outcome.value} but its audit "
                "row could not be finalized",
                exc,
            )
        return attempt

    def _now(self) -> datetime:
        value = datetime.now(timezone.utc) if self._clock is None else self._clock()
        _require_utc(value, "clock()")
        return value

    # --- logging (an aid, never a dependency) --------------------------------

    def _safe_log_attempt(self, attempt: OrderRepairAttempt) -> None:
        """One line per attempt. Only order identifiers and states are
        logged -- no credentials, no tokens, no account ids. Wrapped: a
        broken handler/formatter must never change a repair outcome or stop
        the pass (the audit DB is the authority)."""
        try:
            level = logging.INFO if attempt.outcome in _INFO_OUTCOMES else logging.WARNING
            _LOGGER.log(
                level,
                "reconciliation repair attempt: run_id=%s attempt_id=%s mode=%s "
                "target_type=%s target_id=%s client_order_id=%s outcome=%s "
                "failure_code=%s state_before=%s state_after=%s broker_order_id=%s "
                "broker_error_code=%s audit_error=%s detail=%s",
                attempt.run_id,
                attempt.attempt_id,
                attempt.mode.value,
                attempt.target_type.value,
                attempt.target_id,
                attempt.client_order_id,
                attempt.outcome.value,
                attempt.failure_code,
                None
                if attempt.internal_state_before is None
                else attempt.internal_state_before.value,
                None
                if attempt.internal_state_after is None
                else attempt.internal_state_after.value,
                attempt.broker_order_id,
                None if attempt.broker_error_code is None else attempt.broker_error_code.value,
                attempt.audit_error,
                attempt.detail,
            )
        except Exception:  # deliberate: logging is best-effort only
            pass

    def _safe_log_result(self, result: ReconciliationRepairResult) -> None:
        try:
            counts = result.outcome_counts()
            _LOGGER.info(
                "reconciliation repair of run %s (%s) finished: attempts=%d applied=%d "
                "would_apply=%d already_applied=%d skipped=%d stale=%d conflict=%d "
                "rejected=%d query_failed=%d failed=%d audit_failures=%d",
                result.run_id,
                result.mode.value,
                len(result.attempts),
                counts[RepairOutcome.APPLIED],
                counts[RepairOutcome.WOULD_APPLY],
                counts[RepairOutcome.ALREADY_APPLIED],
                counts[RepairOutcome.SKIPPED],
                counts[RepairOutcome.STALE],
                counts[RepairOutcome.CONFLICT],
                counts[RepairOutcome.REJECTED],
                counts[RepairOutcome.QUERY_FAILED],
                counts[RepairOutcome.FAILED],
                sum(1 for a in result.attempts if a.audit_error is not None),
            )
        except Exception:  # deliberate: logging is best-effort only
            pass

    def _safe_log_exception(self, message: str, exc: Exception) -> None:
        try:
            _LOGGER.error("%s: %s", message, exc, exc_info=exc)
        except Exception:  # deliberate: logging is best-effort only
            pass
