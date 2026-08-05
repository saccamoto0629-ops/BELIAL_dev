"""Broker reconciliation -- SAFE ORDER-STATE REPAIR (Issue #104).

*** THE ONLY THING THIS SERVICE MAY CHANGE IS BELIAL'S INTERNAL ORDER
STATE, AND ONLY THROUGH THE EXISTING STATE MACHINE. It never submits an
order, never cancels one, never modifies anything broker-side -- the broker
is used through ``get_order`` exclusively (AST-pinned, plus the poisoned
facades in the tests). It never runs a direct SQL UPDATE against the order
store: the single write path is ``execution_store.record_order_result``,
which enforces the Issue #74 transition validator, the snapshot update
contract and the pre-commit write-back validation. ***

What a repair pass does
-----------------------

Input is a RECORDED reconciliation run (Issue #103): :meth:`repair` loads
the run by ``run_id`` from the reconciliation audit DB -- an in-memory,
unrecorded run is refused, so every repair is traceable to a stored
observation. For each stored discrepancy, in observation order:

1. ELIGIBILITY: only ``EXECUTION_ORDER`` targets classified
   ``AUTO_REPAIR_CANDIDATE`` proceed. ``MANUAL_REVIEW`` and ``QUERY_FAILED``
   discrepancies and broker-only orders are recorded as ``SKIPPED`` --
   they are never auto-repaired.
2. RE-VALIDATION (the stored result is NOT trusted blindly): the current
   internal row is re-read; the ``client_order_id`` binding must be intact,
   and the current internal state must still be the state the run observed.
   A state that moved on is ``STALE`` -- unless it already equals the
   observed broker outcome (with a consistent broker reference), which is
   ``ALREADY_APPLIED``: an idempotent no-op, not a second success.
3. RE-QUERY: the broker is asked again (``get_order``). A ``BrokerError``,
   a not-found answer, or an adapter that violates the Broker contract is
   ``QUERY_FAILED`` (typed ``BrokerErrorCode``, #103 policy: scoped
   fail-closed, the remaining targets are still processed) -- no state is
   inferred from a failed query. A fresh snapshot that DISAGREES with the
   stored observation (different state, a changed broker reference ID, or a
   ``broker_order_id`` another execution request owns) is ``CONFLICT``:
   nothing is overwritten, a fresh reconciliation run re-classifies.
   A known broker reference ID is never dropped or replaced.
4. STATE MACHINE: the existing validators
   (``validate_order_state_transition``, identity against the stored
   intent, ``validate_order_result_update`` against the history-anchored
   snapshot, no backwards timestamp) must all pass -- otherwise
   ``REJECTED``. The transition table is reused as-is, never widened:
   UNKNOWN -> FILLED / CANCELLED / REJECTED / ACCEPTED / PARTIALLY_FILLED
   and the in-flight (PENDING / ACCEPTED / PARTIALLY_FILLED) -> confirmed
   transitions pass exactly when Issue #74 allows them.
5. APPLY (mode ``APPLY`` only): ``record_order_result`` is called with
   ``expected_current_state`` -- an atomic compare-and-set INSIDE the
   store's ``BEGIN IMMEDIATE`` transaction, so two workers repairing the
   same target cannot both append a history row: the loser observes
   ``ExecutionOrderStateConflictError``, re-reads and reports
   ``ALREADY_APPLIED`` (or ``STALE``). The history reason embeds the
   ``run_id`` and ``attempt_id``, making the order state history itself the
   durable record that a repair was applied.
6. AUDIT: every attempt (eligible or not) is appended to
   ``broker_reconciliation_repair_attempts`` in the reconciliation DB.

Dry-run (``RepairMode.DRY_RUN``)
--------------------------------

The full evaluation above runs -- reads and broker queries only -- but
NOTHING is written: no order update, no history row, no audit row, no DB
creation or migration (a v1 reconciliation DB is refused with a pointer to
``init_reconciliation_db``; reads never migrate), no state file, no
filesystem side effect. Eligible targets report ``WOULD_APPLY``.

Cross-DB consistency (state update vs audit row)
------------------------------------------------

The order-state update (execution orders DB) and the repair audit row
(reconciliation DB) live in different SQLite files and CANNOT share one
transaction. No distributed-transaction machinery is imitated. Instead the
ORDER STATE HISTORY IS THE SOURCE OF TRUTH for applied repairs: the update
commits first with the run/attempt provenance in its reason; the audit row
is appended second. If the audit append fails after a successful update,
the attempt is returned with ``audit_error`` set (and ERROR-logged) -- the
state change is durable and correct, only its audit row is missing, and
RE-RUNNING the repair converges: the re-run finds the target already at the
observed outcome, reports ``ALREADY_APPLIED`` and appends that audit row.
The failure mode "updated but unaudited" is therefore detectable
(``ReconciliationRepairResult.has_audit_failures``) and recoverable by
repetition, never a half-applied internal state.

Idempotency
-----------

Re-running the same repair any number of times: the terminal state stays
what the broker confirmed, the order state history gains NO new rows
(``ALREADY_APPLIED`` short-circuits before the store, and the CAS guard
closes the race window), and each pass appends only its own audit attempts
(append-only audit, per the Issue #103/#104 duplication policy). A target
that meanwhile progressed to a DIFFERENT outcome is never overwritten
(``STALE`` / ``CONFLICT`` / the terminal-state transition rules).
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
    RepairMode,
    RepairOutcome,
)
from execution.reconciliation_store import (
    ReconciliationStoreError,
    get_reconciliation_run,
    init_reconciliation_db,
    record_repair_attempt,
)

_LOGGER = logging.getLogger(__name__)

# Outcomes that are expected operation, logged at INFO; everything else is a
# WARNING (the repair deliberately did not happen and someone should look).
_INFO_OUTCOMES: frozenset[RepairOutcome] = frozenset(
    {RepairOutcome.APPLIED, RepairOutcome.WOULD_APPLY, RepairOutcome.ALREADY_APPLIED}
)


class ReconciliationRepairError(RuntimeError):
    """A repair pass could not run at all (bad input, unrecorded run,
    missing audit DB). Per-target problems never raise this -- they become
    typed :class:`RepairOutcome` values on the attempt."""


def _default_attempt_id() -> str:
    return f"repair-{os.urandom(16).hex()}"


class BrokerOrderReconciliationRepairService:
    """Reflect AUTO_REPAIR_CANDIDATE observations into the order store --
    idempotently, through the existing state machine only (Issue #104).

    ``execution_db_path`` is the execution ORDER store (written exclusively
    through ``record_order_result``). ``reconciliation_db_path`` is the
    Issue #103 audit DB the run is loaded from and (in APPLY mode) the
    repair attempts are appended to. ``clock`` supplies the timestamps
    (injected in tests; defaults to ``datetime.now(timezone.utc)``) and must
    return timezone-aware UTC datetimes. ``attempt_id_factory`` supplies the
    per-attempt audit ids (defaults to a random hex id; injected in tests
    for determinism).
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
        started_at = self._now()
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

        attempts: list[OrderRepairAttempt] = []
        for discrepancy in run.discrepancies:
            attempt = self._repair_one(run, discrepancy, mode)
            if mode is RepairMode.APPLY:
                attempt = self._record_attempt(attempt)
            self._log_attempt(attempt)
            attempts.append(attempt)

        result = ReconciliationRepairResult(
            run_id=run_id,
            mode=mode,
            started_at_utc=started_at,
            finished_at_utc=self._now(),
            attempts=tuple(attempts),
        )
        self._log_result(result)
        return result

    # --- one discrepancy ----------------------------------------------------

    def _repair_one(
        self, run: ReconciliationRun, discrepancy: OrderDiscrepancy, mode: RepairMode
    ) -> OrderRepairAttempt:
        attempt_id = self._attempt_id_factory()
        attempted_at = self._now()

        def make(
            *,
            outcome: RepairOutcome,
            detail: str,
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
                attempted_at_utc=attempted_at,
                execution_request_id=discrepancy.target_id if is_execution else None,
                client_order_id=discrepancy.client_order_id,
                reconciliation_internal_state=discrepancy.internal_state,
                reconciliation_broker_state=discrepancy.broker_state,
                internal_state_before=before,
                internal_state_after=after,
                broker_order_id=(
                    discrepancy.broker_order_id if broker_order_id is None else broker_order_id
                ),
                broker_error_code=broker_error_code,
                observed_at_utc=discrepancy.observed_at_utc,
                applied_at_utc=applied_at,
            )

        try:
            return self._evaluate(run, discrepancy, mode, attempt_id, make)
        except Exception as exc:
            # Unexpected failure (adapter bug leaking past the handled broker
            # branches, a store invariant nobody anticipated, ...): recorded
            # fail-closed as FAILED. No partial internal state can remain --
            # the one write path is transactional and either committed
            # (handled above) or rolled back by the store.
            return make(
                outcome=RepairOutcome.FAILED,
                detail=(
                    f"unexpected failure while repairing ({type(exc).__name__}: {exc}); "
                    "the execution order store commits atomically, so no partial state "
                    "was left behind"
                ),
            )

    def _evaluate(
        self,
        run: ReconciliationRun,
        discrepancy: OrderDiscrepancy,
        mode: RepairMode,
        attempt_id: str,
        make,
    ) -> OrderRepairAttempt:
        skipped = RepairOutcome.SKIPPED
        if discrepancy.target_type is not ReconciliationTargetType.EXECUTION_ORDER:
            return make(
                outcome=skipped,
                detail=(
                    "broker-only orders are never adopted automatically "
                    f"(resolution {discrepancy.resolution_category.value!r})"
                ),
            )
        if discrepancy.resolution_category is ResolutionCategory.QUERY_FAILED:
            return make(
                outcome=skipped,
                detail=(
                    "the reconciliation query itself failed -- nothing was observed, so "
                    "there is nothing to reflect"
                ),
            )
        if discrepancy.resolution_category is not ResolutionCategory.AUTO_REPAIR_CANDIDATE:
            return make(
                outcome=skipped,
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
                detail=(
                    "the stored auto-repair candidate carries no confirmable broker state "
                    f"({None if discrepancy.broker_state is None else discrepancy.broker_state.value!r}); "
                    "refusing to trust it"
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
                detail=(
                    f"reading the execution order store failed ({type(exc).__name__}: "
                    f"{exc}); nothing was changed"
                ),
            )
        if row is None:
            return make(
                outcome=RepairOutcome.FAILED,
                detail=(
                    f"no execution order {request_id!r} exists in the order store; the "
                    "stored discrepancy cannot be re-validated and nothing was changed"
                ),
            )
        if discrepancy.client_order_id is None or row["client_order_id"] != discrepancy.client_order_id:
            return make(
                outcome=RepairOutcome.REJECTED,
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
                detail=(
                    "the execution order has no broker state (phase 'created'); the stored "
                    "discrepancy cannot describe this row and nothing was changed"
                ),
            )
        current = OrderState(row["order_state"])

        if current is not discrepancy.internal_state:
            return self._stale_or_already_applied(discrepancy, row, current, make)

        # --- broker re-query (the stored observation is confirmed, not trusted)
        try:
            fresh = self._broker.get_order(row["client_order_id"])
            if not isinstance(fresh, OrderResult):
                raise TypeError(
                    "Broker.get_order() must return an OrderResult, got "
                    f"{type(fresh).__name__}"
                )
        except BrokerOrderNotFoundError as exc:
            return make(
                outcome=RepairOutcome.QUERY_FAILED,
                broker_error_code=BrokerErrorCode.NOT_FOUND,
                before=current,
                detail=(
                    f"the broker no longer answers about this client_order_id "
                    f"({type(exc).__name__}); this contradicts the reconciliation "
                    "observation and proves nothing, so the internal state stays untouched"
                ),
            )
        except BrokerError as exc:
            return make(
                outcome=RepairOutcome.QUERY_FAILED,
                broker_error_code=exc.code,
                before=current,
                detail=(
                    f"repair-time broker query failed ({type(exc).__name__}: {exc}); a "
                    "failed query never settles an order, so the internal state stays "
                    "untouched"
                ),
            )
        except Exception as exc:
            # Same stance as Issue #103: an adapter leaking a non-BrokerError
            # across the boundary is recorded fail-closed as a scoped
            # communication failure; the remaining targets are still processed.
            return make(
                outcome=RepairOutcome.QUERY_FAILED,
                broker_error_code=BrokerErrorCode.COMMUNICATION_FAILURE,
                before=current,
                detail=(
                    "repair-time broker query violated the Broker error/return contract "
                    f"({type(exc).__name__}); recorded fail-closed and the internal state "
                    "stays untouched"
                ),
            )

        conflict = self._requery_conflict(discrepancy, row, fresh)
        if conflict is not None:
            return make(outcome=RepairOutcome.CONFLICT, before=current, detail=conflict)

        owner_failure, owner_conflict = self._broker_id_owner_problem(request_id, fresh)
        if owner_failure is not None:
            return make(outcome=RepairOutcome.FAILED, before=current, detail=owner_failure)
        if owner_conflict is not None:
            return make(outcome=RepairOutcome.CONFLICT, before=current, detail=owner_conflict)

        try:
            rejection = self._contract_rejection(request_id, row, current, fresh)
        except ExecutionStoreError as exc:
            # fail-closed: a store read that cannot answer never proves
            # repairability
            return make(
                outcome=RepairOutcome.FAILED,
                before=current,
                detail=(
                    f"reading the execution order store failed while proving "
                    f"repairability ({type(exc).__name__}: {exc}); nothing was changed"
                ),
            )
        if rejection is not None:
            return make(outcome=RepairOutcome.REJECTED, before=current, detail=rejection)

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

        # --- apply through the state machine, compare-and-set on the state
        reason = (
            f"broker reconciliation repair (run_id={run.run_id}, attempt_id={attempt_id}): "
            f"the broker confirmed state {fresh.state.value!r} for client_order_id "
            f"{row['client_order_id']!r}"
        )
        try:
            updated = execution_store.record_order_result(
                request_id,
                fresh,
                reason=reason,
                expected_current_state=current,
                db_path=self._execution_db_path,
            )
        except ExecutionOrderStateConflictError as exc:
            return self._concurrent_loss(discrepancy, current, make, exc)
        except ExecutionOrderConflictError as exc:
            return make(
                outcome=RepairOutcome.CONFLICT,
                before=current,
                detail=(
                    f"the update collided with a uniqueness constraint and was rolled back "
                    f"({exc}); nothing was changed"
                ),
            )
        except (ValueError, BrokerInvalidRequestError) as exc:
            return make(
                outcome=RepairOutcome.REJECTED,
                before=current,
                detail=(
                    f"the execution store refused the update ({exc}); the transaction "
                    "rolled back and nothing was changed"
                ),
            )
        except ExecutionStoreError as exc:
            return make(
                outcome=RepairOutcome.FAILED,
                before=current,
                detail=(
                    f"the execution store failed during the update ({type(exc).__name__}: "
                    f"{exc}); the transaction rolled back and nothing was changed"
                ),
            )
        return make(
            outcome=RepairOutcome.APPLIED,
            before=current,
            after=fresh.state,
            broker_order_id=updated["broker_order_id"],
            applied_at=self._now(),
            detail=reason,
        )

    # --- helpers ------------------------------------------------------------

    def _stale_or_already_applied(
        self, discrepancy: OrderDiscrepancy, row: dict, current: OrderState, make
    ) -> OrderRepairAttempt:
        """The internal state moved on since the reconciliation observation."""
        reference_consistent = (
            discrepancy.broker_order_id is None
            or row["broker_order_id"] == discrepancy.broker_order_id
        )
        if current is discrepancy.broker_state and reference_consistent:
            return make(
                outcome=RepairOutcome.ALREADY_APPLIED,
                before=current,
                after=current,
                broker_order_id=row["broker_order_id"],
                detail=(
                    f"the internal state already holds the broker-observed outcome "
                    f"{current.value!r} (recorded by an earlier repair or the normal "
                    "path); no new state and no new history row were written"
                ),
            )
        return make(
            outcome=RepairOutcome.STALE,
            before=current,
            broker_order_id=row["broker_order_id"],
            detail=(
                f"the reconciliation run observed internal state "
                f"{discrepancy.internal_state.value!r} but the order is now "
                f"{current.value!r}"
                + (
                    ""
                    if reference_consistent
                    else (
                        f" with broker_order_id {row['broker_order_id']!r} instead of the "
                        f"observed {discrepancy.broker_order_id!r}"
                    )
                )
                + "; a stale observation never overwrites newer state"
            ),
        )

    def _concurrent_loss(
        self, discrepancy: OrderDiscrepancy, expected: OrderState, make, exc: Exception
    ) -> OrderRepairAttempt:
        """The compare-and-set lost: re-read and classify what happened."""
        try:
            row = execution_store.get_execution_order(
                discrepancy.target_id, db_path=self._execution_db_path
            )
        except ExecutionStoreError as read_exc:
            return make(
                outcome=RepairOutcome.FAILED,
                before=expected,
                detail=(
                    f"the conditional update was refused ({exc}) and the re-read failed "
                    f"({type(read_exc).__name__}: {read_exc}); nothing was changed by "
                    "this attempt"
                ),
            )
        if row is None or row["order_state"] is None:
            return make(
                outcome=RepairOutcome.FAILED,
                before=expected,
                detail=(
                    f"the conditional update was refused ({exc}) and the order could not "
                    "be re-read into a broker-tracked state; nothing was changed by this "
                    "attempt"
                ),
            )
        current = OrderState(row["order_state"])
        return self._stale_or_already_applied(discrepancy, row, current, make)

    @staticmethod
    def _requery_conflict(
        discrepancy: OrderDiscrepancy, row: dict, fresh: OrderResult
    ) -> str | None:
        """Whether the fresh snapshot contradicts the stored observation or
        would lose / replace a known broker reference ID."""
        if fresh.client_order_id != row["client_order_id"]:
            return (
                f"the broker answered about client_order_id {fresh.client_order_id!r} "
                f"instead of {row['client_order_id']!r}"
            )
        if fresh.state is not discrepancy.broker_state:
            return (
                f"the repair-time re-query reports {fresh.state.value!r} but the "
                f"reconciliation run observed {discrepancy.broker_state.value!r}; the "
                "stored observation is outdated -- a fresh reconciliation run must "
                "re-classify before anything is reflected"
            )
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
                f"the broker_order_id ownership lookup failed ({type(exc).__name__}: "
                f"{exc}); repairability is never assumed from a lookup that did not "
                "answer, so nothing was changed",
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
    ) -> str | None:
        """Whether the existing state machine / store contract refuses the
        update -- the same checks ``record_order_result`` enforces, run first
        so the refusal is classified (REJECTED) instead of surfacing as a
        store exception."""
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
                "the broker snapshot does not describe the stored execution request "
                f"(differing: {', '.join(mismatches)})"
            )
        try:
            validate_order_state_transition(current, fresh.state)
        except (ValueError, BrokerInvalidRequestError) as exc:
            return f"the order state machine forbids this transition: {exc}"
        # a store read failure here propagates as ExecutionStoreError; the
        # caller classifies it FAILED (fail-closed), never REJECTED
        anchored = execution_store.load_order_result(request_id, db_path=self._execution_db_path)
        if anchored is not None:
            try:
                validate_order_result_update(anchored, fresh)
            except (ValueError, BrokerInvalidRequestError) as exc:
                return f"the snapshot update contract refuses this observation: {exc}"
        stored_updated_at = datetime.fromisoformat(row["updated_at_utc"])
        if fresh.updated_at_utc < stored_updated_at:
            return (
                f"the broker snapshot is older than the stored state "
                f"({fresh.updated_at_utc.isoformat()} < {row['updated_at_utc']})"
            )
        return None

    def _record_attempt(self, attempt: OrderRepairAttempt) -> OrderRepairAttempt:
        """Append the audit row; on failure attach ``audit_error`` instead of
        losing the attempt (see the module docstring: the state history is
        the source of truth, re-running converges)."""
        try:
            record_repair_attempt(attempt, db_path=self._reconciliation_db_path)
        except ReconciliationStoreError as exc:
            attempt = replace(attempt, audit_error=f"{type(exc).__name__}: {exc}")
            _LOGGER.error(
                "repair attempt %s (run %s, target %s) had outcome %s but its audit row "
                "could not be recorded: %s; the order state history carries the "
                "provenance, and re-running the repair records it as already_applied",
                attempt.attempt_id,
                attempt.run_id,
                attempt.target_id,
                attempt.outcome.value,
                attempt.audit_error,
            )
        return attempt

    def _now(self) -> datetime:
        value = datetime.now(timezone.utc) if self._clock is None else self._clock()
        _require_utc(value, "clock()")
        return value

    def _log_attempt(self, attempt: OrderRepairAttempt) -> None:
        """One line per attempt. Only order identifiers and states are
        logged -- no credentials, no tokens, no account ids."""
        level = logging.INFO if attempt.outcome in _INFO_OUTCOMES else logging.WARNING
        _LOGGER.log(
            level,
            "reconciliation repair attempt: run_id=%s attempt_id=%s mode=%s target_type=%s "
            "target_id=%s client_order_id=%s outcome=%s state_before=%s state_after=%s "
            "broker_order_id=%s broker_error_code=%s detail=%s",
            attempt.run_id,
            attempt.attempt_id,
            attempt.mode.value,
            attempt.target_type.value,
            attempt.target_id,
            attempt.client_order_id,
            attempt.outcome.value,
            None if attempt.internal_state_before is None else attempt.internal_state_before.value,
            None if attempt.internal_state_after is None else attempt.internal_state_after.value,
            attempt.broker_order_id,
            None if attempt.broker_error_code is None else attempt.broker_error_code.value,
            attempt.detail,
        )

    def _log_result(self, result: ReconciliationRepairResult) -> None:
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
