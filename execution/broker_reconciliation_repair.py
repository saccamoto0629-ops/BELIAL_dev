"""Broker reconciliation -- SAFE ORDER-STATE REPAIR (Issue #104).

*** THE ONLY THING THIS SERVICE MAY CHANGE IS BELIAL'S INTERNAL ORDER
STATE, AND ONLY THROUGH THE EXISTING STATE MACHINE. It never submits an
order, never cancels one, never modifies anything broker-side -- the broker
is used through ``get_order`` exclusively (AST-pinned, plus the poisoned
facades in the tests). It never runs a direct SQL UPDATE against the order
store: the single write path is ``execution_store.record_order_result``,
which enforces the Issue #74 transition validator, the snapshot update
contract and the pre-commit write-back validation. ***

Claimed attempt lifecycle (owner + lease + generation fencing)
--------------------------------------------------------------

Input is a RECORDED reconciliation run (Issue #103): :meth:`repair` loads
the run by ``run_id`` from the reconciliation audit DB. ALL attempt ids are
generated and uniqueness-checked BEFORE any side effect. Then, per
discrepancy in observation order (APPLY mode):

1. CLAIM / RESERVE: a ``pending`` audit row -- attempt id, run /
   discrepancy identity, observation anchors AND the claim (``owner_id``,
   a fresh ``claim_execution_id``, ``generation = 1``,
   ``lease_expires_at_utc = reserved_at + lease_duration``) -- is committed
   BEFORE anything else. While the lease is valid, nobody else (recovery
   included) may touch the attempt; a duplicate attempt id is refused HERE,
   before any state update.
2. EVALUATE (the stored result is NOT trusted blindly): the current
   internal row is re-read (``client_order_id`` binding intact), the broker
   is RE-QUERIED -- always, including before an ``already_applied`` verdict
   -- and the REVISION gate compares the discrepancy's observed revision
   against the current one, so ANY change since the observation (same-state
   writes included) is detected. Broker reference IDs are never lost,
   replaced or stolen from another execution request; the existing
   validators (transition table, intent identity, anchored snapshot update
   contract, no backwards timestamp) must all pass.
3. FENCE + COMMIT: right before the commit the claim's fencing token is
   registered in the EXECUTION DB (``execution_repair_fences``:
   ``register_repair_fence``), and ``record_order_result`` verifies that
   token INSIDE its transaction on top of the state/revision compare-and-
   set. A worker whose claim was taken over (lease expiry -> generation
   increment) therefore CANNOT commit -- fence registration and commit both
   fail typed (``ExecutionRepairFenceConflictError`` -> outcome
   ``conflict`` / ``claim_lost``), even while state and revision still
   match. The APPLIED attempt record is fully constructed and validated
   BEFORE the store call; after a successful commit nothing is computed any
   more, so no post-commit exception can mislabel a committed repair.
4. FINALIZE: the pending audit row is completed by a compare-and-set on
   (pending status, owner, claim execution id, generation). A lost claim
   (``RepairClaimLostError``) attaches ``audit_error`` to the returned
   attempt and NEVER stores an outcome over the new owner's -- an APPLIED
   fact in the execution DB can never be contradicted by a stale worker's
   audit row.

Lease contract: there is NO heartbeat by design (minimal Issue #104 scope);
instead the lease must comfortably exceed one attempt's work, and the
constructor VALIDATES ``broker timeout < lease duration`` (against the
explicit ``broker_timeout_seconds`` argument and/or a broker exposing
``timeout_seconds``, e.g. the OANDA adapter config). Adapters must keep
their query timeout below the lease duration -- see the docs.

Crash / takeover recovery (:meth:`recover_pending_attempts`)
------------------------------------------------------------

Pending reservations are enumerated from the audit DB:

- LEASE STILL VALID -> untouched. No takeover, no finalize, no broker
  query, no execution-DB change: a live worker owns it.
- LEASE EXPIRED -> fenced takeover: (a) the EXECUTION-DB fence is bumped to
  ``generation + 1`` first (this is the takeover mutex -- of any number of
  concurrent recovery workers exactly one wins; losers change neither
  fence nor claim), then (b) the reconciliation-DB claim is taken over by
  atomic CAS (owner / claim execution id replaced, generation incremented,
  lease extended). The new generation is now the only token the execution
  DB accepts; a resurrected old worker can neither commit nor finalize.
  Then, under the new claim:
  - the execution history's STRUCTURED provenance (``repair_attempt_id``)
    already names this attempt -> the state update committed before the
    crash; finalize ``applied`` under the ORIGINAL attempt id.
  - no provenance -> the update never committed; the recovery worker
    CONTINUES the attempt as its legitimate new owner: fresh broker
    re-query plus the full eligibility evaluation, then commit/finalize
    under the new generation. Recovery never invents a ``failed`` verdict
    without that re-evaluation.

Cross-DB consistency: reservation (audit) -> fence + commit (orders) ->
finalize (audit) is an explicit outbox-style sequence; every crash window
is recoverable and the fence closes the "old worker resurrects after
takeover" window inside the execution DB transaction itself.

Failure classification and logging (Issue #104 re-review)
---------------------------------------------------------

Only OPERATIONALLY EXPECTED, typed failures become attempt outcomes
(``BrokerError``, the documented broker-boundary contract violation,
``ExecutionStoreError``, ``ReconciliationStoreError``, the store's
refusal channel, CAS / fence / claim conflicts). Everything else stops the
pass with :class:`ReconciliationRepairRunError` carrying the completed
attempts. Audit details AND LOGS are sanitized: stable
:class:`RepairFailureCode` members plus our own typed exception class
names -- never raw exception text, ``repr``, tracebacks, secrets, paths or
SQL (an unexpected exception is logged as its class name only). Logging is
an AID, never a dependency: every log call is isolated so a broken
handler / formatter cannot change a repair outcome or stop the pass.

Dry-run (``RepairMode.DRY_RUN``)
--------------------------------

The full evaluation runs -- reads and broker queries only -- but NOTHING is
written: no order update, no history row, no claim / reservation, no
fence, no audit row, no DB creation or migration (a v1 reconciliation DB is
refused; reads never migrate), no state file, no filesystem side effect.
Eligible targets report ``would_apply``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
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
    ExecutionRepairFenceConflictError,
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
    RepairFailureCode,
    RepairMode,
    RepairOutcome,
)
from execution.reconciliation_store import (
    ReconciliationStoreError,
    RepairAttemptConflictError,
    RepairClaimLostError,
    claim_repair_attempt,
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

DEFAULT_LEASE_DURATION_SECONDS = 600


class ReconciliationRepairError(RuntimeError):
    """A repair pass could not run at all (bad input, unrecorded run,
    missing audit DB, duplicate attempt ids from the factory, a broker
    timeout that does not fit under the lease). Raised BEFORE any state
    change. Per-target problems never raise this -- they become typed
    :class:`RepairOutcome` values on the attempt."""


class ReconciliationRepairRunError(ReconciliationRepairError):
    """The pass stopped on a run-level integrity failure (unexpected
    exception, clock contract failure, result construction failure).

    ``attempts`` carries every attempt COMPLETED before the stop, so a
    caller never loses committed partial results -- and the same attempts
    are independently durable in the audit DB
    (``reconciliation_store.list_repair_attempts``); a half-processed
    target stays pending there (its lease eventually expires) for
    :meth:`recover_pending_attempts`.
    """

    def __init__(self, message: str, *, attempts: tuple[OrderRepairAttempt, ...]):
        super().__init__(message)
        self.attempts = attempts


def _default_attempt_id() -> str:
    return f"repair-{os.urandom(16).hex()}"


def _new_claim_execution_id() -> str:
    return f"claim-{os.urandom(8).hex()}"


class _Claim:
    """The claim under which one attempt is being executed (in-memory)."""

    __slots__ = ("owner_id", "claim_execution_id", "generation")

    def __init__(self, owner_id: str, claim_execution_id: str, generation: int):
        self.owner_id = owner_id
        self.claim_execution_id = claim_execution_id
        self.generation = generation


class BrokerOrderReconciliationRepairService:
    """Reflect AUTO_REPAIR_CANDIDATE observations into the order store --
    idempotently, through the existing state machine only (Issue #104).

    ``execution_db_path`` is the execution ORDER store (written exclusively
    through ``record_order_result``; the fencing table is maintained via
    ``register_repair_fence``). ``reconciliation_db_path`` is the Issue #103
    audit DB the run is loaded from and (in APPLY mode) the claims are
    written to. ``owner_id`` names this worker in claims (defaults to a
    random per-instance id). ``lease_duration_seconds`` is the claim lease;
    ``broker_timeout_seconds`` (explicit, and/or a broker exposing
    ``timeout_seconds``) must stay BELOW it -- validated at construction.
    ``clock`` supplies the repair-side timestamps (injected in tests) and
    must return timezone-aware UTC datetimes; it is called at pass
    start/finish, at claim/reservation time and at finalize time -- never
    between a state commit and the return of the attempt.
    ``attempt_id_factory`` supplies the per-attempt audit ids; all ids for a
    pass are generated and uniqueness-checked up front.
    """

    def __init__(
        self,
        *,
        broker: Broker,
        execution_db_path: Path | str,
        reconciliation_db_path: Path | str,
        owner_id: str | None = None,
        lease_duration_seconds: int = DEFAULT_LEASE_DURATION_SECONDS,
        broker_timeout_seconds: float | None = None,
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
        if owner_id is not None:
            _require_non_empty_str(owner_id, "owner_id")
        if (
            isinstance(lease_duration_seconds, bool)
            or not isinstance(lease_duration_seconds, int)
            or lease_duration_seconds < 1
        ):
            raise ValueError(
                f"lease_duration_seconds must be an int >= 1, got {lease_duration_seconds!r}"
            )
        # Lease contract (Issue #104 re-review): no heartbeat by design, so
        # every broker query must fit comfortably under the lease. Validated
        # against the explicit argument AND a broker that exposes its
        # timeout (e.g. an adapter's ``timeout_seconds``).
        for source, timeout in (
            ("broker_timeout_seconds", broker_timeout_seconds),
            ("broker.timeout_seconds", getattr(broker, "timeout_seconds", None)),
        ):
            if timeout is None:
                continue
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not timeout > 0
            ):
                raise ValueError(f"{source} must be a positive number, got {timeout!r}")
            if timeout >= lease_duration_seconds:
                raise ValueError(
                    f"{source} ({timeout}) must be strictly below "
                    f"lease_duration_seconds ({lease_duration_seconds}); a broker query "
                    "must never outlive the claim lease"
                )
        self._broker = broker
        self._execution_db_path = Path(execution_db_path)
        self._reconciliation_db_path = Path(reconciliation_db_path)
        self._owner_id = owner_id or f"repair-worker-{os.urandom(4).hex()}"
        self._lease_duration = timedelta(seconds=lease_duration_seconds)
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
        # duplicate) fails the pass HERE, before any claim or state change.
        attempt_ids = tuple(self._attempt_id_factory() for _ in run.discrepancies)
        for attempt_id in attempt_ids:
            _require_non_empty_str(attempt_id, "attempt_id")
        if len(set(attempt_ids)) != len(attempt_ids):
            raise ReconciliationRepairError(
                "attempt_id_factory produced duplicate ids within one repair pass; "
                "refusing to start (no claim and no state update was made)"
            )

        attempts: list[OrderRepairAttempt] = []
        try:
            for discrepancy, attempt_id in zip(run.discrepancies, attempt_ids):
                attempt = self._repair_one(run, discrepancy, attempt_id, mode)
                attempts.append(attempt)
                self._safe_log_attempt(attempt)
        except Exception as exc:
            # Run-level integrity failure (unexpected exception, clock
            # contract, store unavailability at claim time, ...): stop, but
            # never lose what already committed -- the finalized attempts are
            # in the audit DB and returned on the error; a half-processed
            # target stays pending there (lease expiry hands it to recovery).
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
        """Take over and complete pending attempts whose lease EXPIRED.

        For each pending reservation (optionally restricted to one run):

        - lease still valid -> UNTOUCHED (no takeover, no finalize, no
          broker query, no execution-DB change): a live worker owns it.
        - lease expired -> fenced takeover (execution-DB fence bumped to
          ``generation + 1`` FIRST -- the takeover mutex; exactly one of any
          number of concurrent recovery workers wins, losers change
          nothing), then the reconciliation-DB claim CAS. Under the new
          claim: a history row whose STRUCTURED ``repair_attempt_id``
          equals the reservation's id proves the state update committed ->
          finalize ``applied`` under the ORIGINAL attempt id; otherwise the
          attempt is RE-EVALUATED in full (fresh broker query, eligibility,
          fenced commit) as the new legitimate owner -- recovery never
          invents a ``failed`` verdict without that re-evaluation.

        Recovery is fail-closed: a reservation whose source run /
        discrepancy has been deleted or tampered with is refused with
        :class:`ReconciliationRepairError` instead of being guessed about.
        """
        db_path = self._reconciliation_db_path
        pending = list_pending_repair_attempts(run_id=run_id, db_path=db_path)
        recovered: list[OrderRepairAttempt] = []
        for reservation in pending:
            now = self._now()
            if reservation.lease_expires_at_utc > now:
                continue  # the lease protects a (possibly live) worker
            run, discrepancy = self._verified_source(reservation)
            new_claim = _Claim(
                self._owner_id, _new_claim_execution_id(), reservation.generation + 1
            )
            if reservation.execution_request_id is not None:
                # Fence FIRST (the takeover mutex): after this, the old
                # generation can no longer commit -- and of concurrent
                # recovery workers only the fence winner proceeds.
                try:
                    execution_store.register_repair_fence(
                        reservation.execution_request_id,
                        repair_attempt_id=reservation.attempt_id,
                        generation=new_claim.generation,
                        owner_id=new_claim.owner_id,
                        claim_execution_id=new_claim.claim_execution_id,
                        registered_at_utc=now,
                        db_path=self._execution_db_path,
                    )
                except ExecutionRepairFenceConflictError:
                    continue  # another worker holds a newer fence; not ours
                except execution_store.ExecutionOrderNotFoundError:
                    pass  # no order -> nothing to fence; the claim CAS decides
            try:
                claimed = claim_repair_attempt(
                    reservation.attempt_id,
                    expected_generation=reservation.generation,
                    new_owner_id=new_claim.owner_id,
                    new_claim_execution_id=new_claim.claim_execution_id,
                    new_lease_expires_at_utc=now + self._lease_duration,
                    claimed_at_utc=now,
                    db_path=db_path,
                )
            except (RepairClaimLostError, RepairAttemptConflictError):
                continue  # someone else took it over or finalized it first
            attempt = self._resume_claimed_attempt(run, discrepancy, claimed, new_claim)
            attempt = self._finalize(attempt)
            self._safe_log_attempt(attempt)
            recovered.append(attempt)
        return tuple(recovered)

    # --- recovery helpers ---------------------------------------------------

    def _verified_source(
        self, reservation: RepairAttemptReservation
    ) -> tuple[ReconciliationRun, OrderDiscrepancy]:
        run = get_reconciliation_run(reservation.run_id, db_path=self._reconciliation_db_path)
        discrepancy = None
        if run is not None:
            for entry in run.discrepancies:
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
        return run, discrepancy

    def _resume_claimed_attempt(
        self,
        run: ReconciliationRun,
        discrepancy: OrderDiscrepancy,
        reservation: RepairAttemptReservation,
        claim: _Claim,
    ) -> OrderRepairAttempt:
        """Complete a taken-over attempt: confirm from provenance, or
        re-evaluate in full as the new owner."""
        make = self._attempt_builder(
            run, discrepancy, reservation.attempt_id, RepairMode.APPLY,
            reservation.reserved_at_utc, claim,
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
        if committed is not None:
            return make(
                outcome=RepairOutcome.APPLIED,
                before=reservation.reconciliation_internal_state,
                after=OrderState(committed["new_state"]),
                broker_order_id=committed["broker_order_id"],
                broker_state_updated_at=datetime.fromisoformat(committed["changed_at_utc"]),
                detail=(
                    "recovered pending claim: the state history carries this attempt id "
                    f"(state {committed['new_state']!r} committed before the takeover); "
                    "finalized under the original attempt id"
                ),
            )
        # nothing committed: continue the attempt as its legitimate new owner
        return self._evaluate(run, discrepancy, reservation.attempt_id, RepairMode.APPLY, make, claim)

    # --- one discrepancy ----------------------------------------------------

    def _repair_one(
        self,
        run: ReconciliationRun,
        discrepancy: OrderDiscrepancy,
        attempt_id: str,
        mode: RepairMode,
    ) -> OrderRepairAttempt:
        reserved_at = self._now()
        claim = (
            _Claim(self._owner_id, _new_claim_execution_id(), 1)
            if mode is RepairMode.APPLY
            else None
        )
        make = self._attempt_builder(run, discrepancy, attempt_id, mode, reserved_at, claim)

        if mode is RepairMode.APPLY:
            # Claim FIRST: the attempt id and its lease are durable (and a
            # duplicate refused) before anything is evaluated or written. A
            # failure here propagates -- nothing was changed yet.
            reserve_repair_attempt(
                self._reservation(run, discrepancy, attempt_id, reserved_at, claim),
                db_path=self._reconciliation_db_path,
            )
        attempt = self._evaluate(run, discrepancy, attempt_id, mode, make, claim)
        if mode is RepairMode.APPLY:
            attempt = self._finalize(attempt)
        return attempt

    def _attempt_builder(
        self,
        run: ReconciliationRun,
        discrepancy: OrderDiscrepancy,
        attempt_id: str,
        mode: RepairMode,
        reserved_at: datetime,
        claim: _Claim | None,
    ):
        def make(
            *,
            outcome: RepairOutcome,
            detail: str,
            failure_code: RepairFailureCode | None = None,
            before: OrderState | None = None,
            after: OrderState | None = None,
            broker_order_id: str | None = None,
            broker_error_code: BrokerErrorCode | None = None,
            broker_state_updated_at: datetime | None = None,
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
                owner_id=None if claim is None else claim.owner_id,
                claim_execution_id=None if claim is None else claim.claim_execution_id,
                generation=None if claim is None else claim.generation,
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
                broker_state_updated_at_utc=broker_state_updated_at,
            )

        return make

    def _reservation(
        self,
        run: ReconciliationRun,
        discrepancy: OrderDiscrepancy,
        attempt_id: str,
        reserved_at: datetime,
        claim: _Claim,
    ) -> RepairAttemptReservation:
        is_execution = discrepancy.target_type is ReconciliationTargetType.EXECUTION_ORDER
        return RepairAttemptReservation(
            attempt_id=attempt_id,
            run_id=run.run_id,
            target_type=discrepancy.target_type,
            target_id=discrepancy.target_id,
            reserved_at_utc=reserved_at,
            detail="reserved: repair evaluation started",
            owner_id=claim.owner_id,
            claim_execution_id=claim.claim_execution_id,
            generation=claim.generation,
            lease_expires_at_utc=reserved_at + self._lease_duration,
            last_claimed_at_utc=reserved_at,
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
        claim: _Claim | None,
    ) -> OrderRepairAttempt:
        skipped = RepairOutcome.SKIPPED
        if discrepancy.target_type is not ReconciliationTargetType.EXECUTION_ORDER:
            return make(
                outcome=skipped,
                failure_code=RepairFailureCode.NOT_ELIGIBLE_BROKER_ONLY,
                detail=(
                    "broker-only orders are never adopted automatically "
                    f"(resolution {discrepancy.resolution_category.value!r})"
                ),
            )
        if discrepancy.resolution_category is ResolutionCategory.QUERY_FAILED:
            return make(
                outcome=skipped,
                failure_code=RepairFailureCode.NOT_ELIGIBLE_QUERY_FAILED,
                detail=(
                    "the reconciliation query itself failed -- nothing was observed, so "
                    "there is nothing to reflect"
                ),
            )
        if discrepancy.resolution_category is not ResolutionCategory.AUTO_REPAIR_CANDIDATE:
            return make(
                outcome=skipped,
                failure_code=RepairFailureCode.NOT_ELIGIBLE_MANUAL_REVIEW,
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
                failure_code=RepairFailureCode.CANDIDATE_CONTRACT_VIOLATION,
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
                failure_code=RepairFailureCode.EXECUTION_STORE_READ_FAILED,
                detail=(
                    f"reading the execution order store failed ({type(exc).__name__}); "
                    "nothing was changed"
                ),
            )
        if row is None:
            return make(
                outcome=RepairOutcome.FAILED,
                failure_code=RepairFailureCode.ORDER_MISSING,
                detail=(
                    f"no execution order {request_id!r} exists in the order store; the "
                    "stored discrepancy cannot be re-validated and nothing was changed"
                ),
            )
        if discrepancy.client_order_id is None or row["client_order_id"] != discrepancy.client_order_id:
            return make(
                outcome=RepairOutcome.REJECTED,
                failure_code=RepairFailureCode.CLIENT_ORDER_ID_BINDING_MISMATCH,
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
                failure_code=RepairFailureCode.ORDER_WITHOUT_BROKER_STATE,
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
                failure_code=RepairFailureCode.BROKER_ORDER_NOT_FOUND,
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
                failure_code=RepairFailureCode.BROKER_QUERY_FAILED,
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
            # a defined boundary conversion, not a general catch-all. The
            # leaked exception is logged as its CLASS NAME only: adapter free
            # text can carry secrets and never reaches logs or audit rows.
            self._safe_log_sanitized_exception(
                "broker.get_order violated the Broker error/return contract",
                exc,
                failure_code=RepairFailureCode.BROKER_CONTRACT_VIOLATION,
                attempt_id=attempt_id,
                run_id=run.run_id,
                target_id=discrepancy.target_id,
            )
            return make(
                outcome=RepairOutcome.QUERY_FAILED,
                broker_error_code=BrokerErrorCode.COMMUNICATION_FAILURE,
                failure_code=RepairFailureCode.BROKER_CONTRACT_VIOLATION,
                before=current,
                detail=(
                    "repair-time broker query violated the Broker error/return contract; "
                    "recorded fail-closed and the internal state stays untouched"
                ),
            )

        if fresh.client_order_id != row["client_order_id"]:
            return make(
                outcome=RepairOutcome.CONFLICT,
                failure_code=RepairFailureCode.BROKER_ANSWERED_FOREIGN_ORDER,
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
                failure_code=RepairFailureCode.MISSING_OBSERVED_REVISION,
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
                failure_code=RepairFailureCode.CANDIDATE_CONTRACT_VIOLATION,
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
                failure_code=RepairFailureCode.REQUERY_DISAGREES_WITH_OBSERVATION,
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
                failure_code=RepairFailureCode.BROKER_REFERENCE_CHANGED,
                before=current,
                detail=reference_conflict,
            )
        owner_failure, owner_conflict = self._broker_id_owner_problem(request_id, fresh)
        if owner_failure is not None:
            return make(
                outcome=RepairOutcome.FAILED,
                failure_code=RepairFailureCode.BROKER_ID_OWNER_LOOKUP_FAILED,
                before=current,
                detail=owner_failure,
            )
        if owner_conflict is not None:
            return make(
                outcome=RepairOutcome.CONFLICT,
                failure_code=RepairFailureCode.BROKER_REFERENCE_OWNED_ELSEWHERE,
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
                failure_code=RepairFailureCode.EXECUTION_STORE_READ_FAILED,
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

        # --- fence + apply. EVERYTHING the APPLIED attempt needs is computed
        # and validated BEFORE the store call: broker_state_updated_at is the
        # broker snapshot's updated_at (exactly the timestamp the store
        # stamps on the history row), the broker id is the same effective id
        # the store derives, the attempt object itself is constructed now.
        # After a successful commit nothing is computed any more, so no
        # post-commit failure can mislabel a committed repair.
        reason = (
            f"broker reconciliation repair (run_id={run.run_id}, attempt_id={attempt_id}, "
            f"generation={claim.generation}): the broker confirmed state "
            f"{fresh.state.value!r} for client_order_id {row['client_order_id']!r}"
        )
        applied_attempt = make(
            outcome=RepairOutcome.APPLIED,
            before=current,
            after=fresh.state,
            broker_order_id=effective_broker_id,
            broker_state_updated_at=fresh.updated_at_utc,
            detail=reason,
        )
        try:
            execution_store.register_repair_fence(
                request_id,
                repair_attempt_id=attempt_id,
                generation=claim.generation,
                owner_id=claim.owner_id,
                claim_execution_id=claim.claim_execution_id,
                registered_at_utc=applied_attempt.reserved_at_utc,
                db_path=self._execution_db_path,
            )
            execution_store.record_order_result(
                request_id,
                fresh,
                reason=reason,
                expected_current_state=current,
                expected_revision=discrepancy.internal_revision,
                repair_attempt_id=attempt_id,
                repair_generation=claim.generation,
                db_path=self._execution_db_path,
            )
        except ExecutionRepairFenceConflictError:
            # the claim was fenced out (a takeover after lease expiry, or a
            # newer attempt now owns the order): typed lost claim, nothing
            # was changed by this worker
            return make(
                outcome=RepairOutcome.CONFLICT,
                failure_code=RepairFailureCode.CLAIM_LOST,
                before=current,
                detail=(
                    "the repair fencing token was superseded before the commit "
                    "(the claim was taken over or a newer attempt owns the order); "
                    "nothing was changed by this worker"
                ),
            )
        except ExecutionOrderStateConflictError:
            return self._concurrent_loss(discrepancy, current, fresh, make)
        except ExecutionOrderConflictError as exc:
            self._safe_log_sanitized_exception(
                "conditional repair update hit a uniqueness conflict",
                exc,
                failure_code=RepairFailureCode.UNIQUENESS_CONFLICT,
                attempt_id=attempt_id,
                run_id=run.run_id,
                target_id=discrepancy.target_id,
            )
            return make(
                outcome=RepairOutcome.CONFLICT,
                failure_code=RepairFailureCode.UNIQUENESS_CONFLICT,
                before=current,
                detail=(
                    "the update collided with a uniqueness constraint "
                    "(ExecutionOrderConflictError) and was rolled back; nothing was changed"
                ),
            )
        except (ValueError, BrokerInvalidRequestError) as exc:
            self._safe_log_sanitized_exception(
                "the execution store refused the repair update",
                exc,
                failure_code=RepairFailureCode.STORE_REFUSED_UPDATE,
                attempt_id=attempt_id,
                run_id=run.run_id,
                target_id=discrepancy.target_id,
            )
            return make(
                outcome=RepairOutcome.REJECTED,
                failure_code=RepairFailureCode.STORE_REFUSED_UPDATE,
                before=current,
                detail=(
                    f"the execution store refused the update ({type(exc).__name__}); the "
                    "transaction rolled back and nothing was changed"
                ),
            )
        except ExecutionStoreError as exc:
            self._safe_log_sanitized_exception(
                "the execution store failed during the repair update",
                exc,
                failure_code=RepairFailureCode.STORE_UPDATE_FAILED,
                attempt_id=attempt_id,
                run_id=run.run_id,
                target_id=discrepancy.target_id,
            )
            return make(
                outcome=RepairOutcome.FAILED,
                failure_code=RepairFailureCode.STORE_UPDATE_FAILED,
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
                failure_code=RepairFailureCode.INTERNAL_ORDER_CHANGED,
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
                failure_code=RepairFailureCode.ALREADY_APPLIED_CONTENT_MISMATCH,
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
        """The state/revision compare-and-set lost: re-read and classify."""
        try:
            row = execution_store.get_execution_order(
                discrepancy.target_id, db_path=self._execution_db_path
            )
        except ExecutionStoreError as exc:
            return make(
                outcome=RepairOutcome.FAILED,
                failure_code=RepairFailureCode.CAS_CONFLICT_UNRESOLVABLE,
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
                failure_code=RepairFailureCode.CAS_CONFLICT_UNRESOLVABLE,
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
    ) -> tuple[RepairFailureCode, str] | None:
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
                RepairFailureCode.SNAPSHOT_IDENTITY_MISMATCH,
                "the broker snapshot does not describe the stored execution request "
                f"(differing: {', '.join(mismatches)})",
            )
        try:
            validate_order_state_transition(current, fresh.state)
        except (ValueError, BrokerInvalidRequestError):
            return (
                RepairFailureCode.FORBIDDEN_TRANSITION,
                f"the order state machine forbids the transition {current.value!r} -> "
                f"{fresh.state.value!r}",
            )
        anchored = execution_store.load_order_result(request_id, db_path=self._execution_db_path)
        if anchored is not None:
            try:
                validate_order_result_update(anchored, fresh)
            except (ValueError, BrokerInvalidRequestError) as exc:
                return (
                    RepairFailureCode.SNAPSHOT_CONTRACT_VIOLATION,
                    "the snapshot update contract refuses this observation "
                    f"({type(exc).__name__})",
                )
        stored_updated_at = datetime.fromisoformat(row["updated_at_utc"])
        if fresh.updated_at_utc < stored_updated_at:
            return (
                RepairFailureCode.SNAPSHOT_OLDER_THAN_STORED_STATE,
                f"the broker snapshot is older than the stored state "
                f"({fresh.updated_at_utc.isoformat()} < {row['updated_at_utc']})",
            )
        return None

    def _finalize(self, attempt: OrderRepairAttempt) -> OrderRepairAttempt:
        """Complete the reserved audit row under this attempt's claim.

        The finalize timestamp is taken HERE (immediately before the write);
        any failure -- store, clock, or a LOST CLAIM (the reservation was
        taken over after lease expiry) -- attaches ``audit_error`` instead
        of changing the outcome: the state history's structured provenance
        keeps an APPLIED fact attributable, and the pending row is completed
        by its current owner (recovery), never overwritten by this worker.
        """
        try:
            finalized = replace(attempt, finalized_at_utc=self._now())
            finalize_repair_attempt(finalized, db_path=self._reconciliation_db_path)
            return finalized
        except RepairClaimLostError as exc:
            attempt = replace(
                attempt,
                audit_error=(
                    f"{type(exc).__name__}: the claim was taken over before finalize; "
                    "the new owner completes the audit row (this worker's outcome is "
                    "not stored)"
                ),
            )
            self._safe_log_lost_claim(attempt)
            return attempt
        except Exception as exc:  # store failure or clock contract failure
            attempt = replace(
                attempt,
                audit_error=(
                    f"{type(exc).__name__}: finalizing the reserved audit row failed; the "
                    "reservation stays pending and recover_pending_attempts() completes "
                    "it under the same attempt id"
                ),
            )
            self._safe_log_sanitized_exception(
                "finalizing a repair attempt failed",
                exc,
                failure_code=None,
                attempt_id=attempt.attempt_id,
                run_id=attempt.run_id,
                target_id=attempt.target_id,
            )
            return attempt

    def _now(self) -> datetime:
        value = datetime.now(timezone.utc) if self._clock is None else self._clock()
        _require_utc(value, "clock()")
        return value

    # --- logging (an aid, never a dependency; sanitized) ----------------------

    def _safe_log_attempt(self, attempt: OrderRepairAttempt) -> None:
        """One line per attempt. Only order identifiers, states, stable codes
        and our own typed class names are logged -- no credentials, no
        tokens, no account ids, no raw exception text. Wrapped: a broken
        handler/formatter must never change a repair outcome or stop the
        pass (the audit DB is the authority)."""
        try:
            level = logging.INFO if attempt.outcome in _INFO_OUTCOMES else logging.WARNING
            _LOGGER.log(
                level,
                "reconciliation repair attempt: run_id=%s attempt_id=%s mode=%s "
                "generation=%s target_type=%s target_id=%s client_order_id=%s outcome=%s "
                "failure_code=%s state_before=%s state_after=%s broker_order_id=%s "
                "broker_error_code=%s audit_error=%s detail=%s",
                attempt.run_id,
                attempt.attempt_id,
                attempt.mode.value,
                attempt.generation,
                attempt.target_type.value,
                attempt.target_id,
                attempt.client_order_id,
                attempt.outcome.value,
                None if attempt.failure_code is None else attempt.failure_code.value,
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

    def _safe_log_lost_claim(self, attempt: OrderRepairAttempt) -> None:
        try:
            _LOGGER.warning(
                "repair attempt %s (run %s, target %s) lost its claim before finalize "
                "(generation %s); the new owner completes the audit row",
                attempt.attempt_id,
                attempt.run_id,
                attempt.target_id,
                attempt.generation,
            )
        except Exception:  # deliberate: logging is best-effort only
            pass

    def _safe_log_sanitized_exception(
        self,
        context: str,
        exc: Exception,
        *,
        failure_code: RepairFailureCode | None,
        attempt_id: str | None = None,
        run_id: str | None = None,
        target_id: str | None = None,
    ) -> None:
        """SANITIZED failure logging (Issue #104 re-review): only the stable
        failure code, the exception CLASS NAME and our own identifiers are
        emitted. Never ``str(exc)``, ``repr(exc)`` or a traceback -- adapter
        free text, tokens, credentials, URLs, SQL and paths must not reach
        any log stream, and the deliberately traceback-free form keeps
        ``__cause__`` / ``__context__`` chains out too."""
        try:
            _LOGGER.error(
                "%s: exception_class=%s failure_code=%s attempt_id=%s run_id=%s "
                "target_id=%s (details withheld from logs; see the typed audit record)",
                context,
                type(exc).__name__,
                None if failure_code is None else failure_code.value,
                attempt_id,
                run_id,
                target_id,
            )
        except Exception:  # deliberate: logging is best-effort only
            pass
