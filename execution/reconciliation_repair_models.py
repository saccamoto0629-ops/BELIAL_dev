"""Broker reconciliation REPAIR common models (Issue #104).

Attempt lifecycle (Issue #104 review redesign)
----------------------------------------------

Every repair attempt in APPLY mode is a TWO-PHASE audit record:

1. :class:`RepairAttemptReservation` -- appended to the audit DB with status
   ``pending`` BEFORE anything is evaluated or written. The attempt id, the
   run / discrepancy identity and the observation anchors (states, revision,
   ``observed_at_utc``) are durable from this point on, so a crash at ANY
   later moment leaves a discoverable pending row instead of losing the
   attempt.
2. :class:`OrderRepairAttempt` -- the FINALIZED view: the pending row is
   completed (compare-and-set on ``status = 'pending'``) with the outcome,
   the before/after states and the timestamps. A pending row that never
   finalized (crash) is recovered by comparing the execution order state
   history's STRUCTURED provenance column (``repair_attempt_id``) -- never
   by parsing reason strings -- and finalized under its ORIGINAL attempt id.

Vocabulary
----------

- :class:`RepairMode` -- what a pass was allowed to do (apply / dry_run).
- :class:`RepairOutcome` -- how one attempt ended, machine-readable.
- :class:`ReconciliationRepairResult` -- the attempts of one repair pass.

Reused, not redefined (Issue #74 / #73 / #103 contract): order states are
``broker.broker_models.OrderState``, query failures carry the existing
``BrokerErrorCode`` and the discrepancy identity reuses
``reconciliation_models.ReconciliationTargetType``. This module introduces
NO order state, NO transition table and NO parallel error taxonomy -- and no
repair is decided here: models only.

Failure recording: ``detail`` is a SANITIZED, self-authored description
(ids, states and allow-listed typed-exception class names only -- never raw
exception text, paths, SQL or adapter free text); ``failure_code`` is the
STABLE machine-readable code every non-success outcome must carry.

Timestamp contract (Issue #104 review)
--------------------------------------

- ``reserved_at_utc``   -- when the attempt was reserved (repair-side clock,
  taken BEFORE any side effect of the attempt).
- ``observed_at_utc``   -- when the reconciliation run observed the
  discrepancy (copied from Issue #103's record).
- ``applied_at_utc``    -- the committed state-history timestamp of the
  applied update. It EQUALS the broker snapshot's ``updated_at_utc`` (that
  is what ``record_order_result`` stamps on the history row), is known
  BEFORE the commit, and lives in the BROKER clock domain -- it is therefore
  deliberately NOT ordered against ``reserved_at_utc``.
- pass ``started_at_utc`` / ``finished_at_utc`` -- repair-side clock around
  the whole pass.

Validation follows the execution layer's style (``ValueError`` on invariant
violations, timezone-aware UTC datetimes only, frozen dataclasses).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime

from broker.broker_models import BrokerErrorCode, OrderState
from execution.execution_models import _require_non_empty_str, _require_utc
from execution.reconciliation_models import ReconciliationTargetType


class RepairMode(enum.Enum):
    """What a repair pass was ALLOWED to do (Issue #104).

    ``APPLY``   -- eligible observations are reflected into the execution
    order store through the existing state machine; every attempt is
    reserved in the audit DB before evaluation and finalized after.
    ``DRY_RUN`` -- detect-only counterpart: the full eligibility evaluation
    runs (execution store reads and broker queries only), but NOTHING is
    written -- no order update, no history row, no reservation, no audit
    row, no DB creation or migration, no state file. The mode is carried on
    every attempt so an audit can tell what a result was permitted to do.
    """

    APPLY = "apply"
    DRY_RUN = "dry_run"


class RepairOutcome(enum.Enum):
    """How one repair attempt ended -- classification, machine-readable.

    ``APPLIED``         -- the broker observation was recorded through the
    existing order state machine (``record_order_result``); the internal
    state changed. APPLY mode only.
    ``WOULD_APPLY``     -- every eligibility check passed but the mode was
    ``DRY_RUN``; nothing was written. DRY_RUN mode only, never persisted.
    ``ALREADY_APPLIED`` -- the internal order already holds the observed
    outcome AND a fresh broker re-query agrees with it in full (three-way
    check; never concluded without asking the broker again). Nothing was
    written. Deliberately NOT a success: re-running a repair must not look
    like it changed something.
    ``SKIPPED``         -- the discrepancy is not an eligible input at all:
    ``MANUAL_REVIEW`` / ``QUERY_FAILED`` categories and broker-only targets
    are never auto-repaired.
    ``STALE``           -- the internal order changed since the
    reconciliation run observed it (revision moved on -- state changes AND
    same-state writes alike); the old observation must not overwrite the
    newer row.
    ``CONFLICT``        -- the repair-time re-query contradicts the stored
    reconciliation observation or the internal record (different broker
    state or content, a broker reference ID mismatch, or a
    ``broker_order_id`` another execution request owns); nothing is
    overwritten -- a fresh reconciliation run re-classifies.
    ``REJECTED``        -- the existing state machine / store contract
    refuses the update (forbidden transition, identity mismatch, snapshot
    update contract violation, backwards timestamp, broken id binding).
    ``QUERY_FAILED``    -- the repair-time broker query failed (typed
    ``BrokerError``, boundary contract violation, or the order was not
    found); no state is inferred and nothing changes.
    ``FAILED``          -- the execution order DB is PROVEN unchanged by
    this attempt: the store refused/failed before commit (its transaction
    rolled back), a required read did not answer, or a recovered pending
    reservation had no committed provenance. Never used for an attempt
    whose state update committed.
    """

    APPLIED = "applied"
    WOULD_APPLY = "would_apply"
    ALREADY_APPLIED = "already_applied"
    SKIPPED = "skipped"
    STALE = "stale"
    CONFLICT = "conflict"
    REJECTED = "rejected"
    QUERY_FAILED = "query_failed"
    FAILED = "failed"


# Outcomes that carry the resulting/planned internal state.
_OUTCOMES_WITH_AFTER_STATE: frozenset[RepairOutcome] = frozenset(
    {RepairOutcome.APPLIED, RepairOutcome.WOULD_APPLY, RepairOutcome.ALREADY_APPLIED}
)

# Outcomes that must NOT carry a failure code (everything else requires one).
_SUCCESS_OUTCOMES: frozenset[RepairOutcome] = _OUTCOMES_WITH_AFTER_STATE


def _require_optional_revision(value, field_name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative int or None, got {value!r}")


@dataclass(frozen=True)
class RepairAttemptReservation:
    """The PENDING phase of one repair attempt (Issue #104).

    Persisted BEFORE any evaluation or state change, so the attempt id and
    its run / discrepancy identity survive a crash at any later point.
    Reservations exist in APPLY mode only -- dry-run reserves nothing.
    """

    attempt_id: str
    run_id: str
    target_type: ReconciliationTargetType
    target_id: str
    reserved_at_utc: datetime
    detail: str
    execution_request_id: str | None = None
    client_order_id: str | None = None
    reconciliation_internal_state: OrderState | None = None
    reconciliation_broker_state: OrderState | None = None
    internal_revision: int | None = None
    observed_at_utc: datetime | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.attempt_id, "attempt_id")
        _require_non_empty_str(self.run_id, "run_id")
        if not isinstance(self.target_type, ReconciliationTargetType):
            raise ValueError(
                f"target_type must be a ReconciliationTargetType, got {self.target_type!r}"
            )
        _require_non_empty_str(self.target_id, "target_id")
        _require_utc(self.reserved_at_utc, "reserved_at_utc")
        _require_non_empty_str(self.detail, "detail")
        for name in ("execution_request_id", "client_order_id"):
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_str(value, name)
        for name in ("reconciliation_internal_state", "reconciliation_broker_state"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, OrderState):
                raise ValueError(f"{name} must be an OrderState or None, got {value!r}")
        _require_optional_revision(self.internal_revision, "internal_revision")
        if self.observed_at_utc is not None:
            _require_utc(self.observed_at_utc, "observed_at_utc")
        is_execution_target = self.target_type is ReconciliationTargetType.EXECUTION_ORDER
        if is_execution_target != (self.execution_request_id is not None):
            raise ValueError(
                "execution_request_id must be present exactly for execution_order targets"
            )
        if is_execution_target and self.reconciliation_internal_state is None:
            raise ValueError(
                "an execution_order reservation requires the reconciliation-observed "
                "internal state"
            )


@dataclass(frozen=True)
class OrderRepairAttempt:
    """The FINALIZED view of one repair attempt (Issue #104).

    ``detail`` is the mandatory sanitized reason for the outcome.
    ``failure_code`` is the stable machine-readable code, required for every
    non-success outcome and forbidden for APPLIED / WOULD_APPLY /
    ALREADY_APPLIED. ``audit_error`` is IN-MEMORY metadata only: it is set
    (via ``dataclasses.replace``) when the finalize of this attempt's
    reserved audit row failed AFTER a committed state update; the
    reservation then stays pending and recovery finalizes it later under
    the same attempt id. It is never persisted.

    Invariants (all enforced): non-empty ids/detail, typed enums,
    timezone-aware UTC timestamps, target/execution-id pairing,
    ``APPLIED`` only in APPLY mode, ``WOULD_APPLY`` only in DRY_RUN,
    ``applied_at_utc`` exactly for APPLIED, the after-state exactly for
    APPLIED / WOULD_APPLY / ALREADY_APPLIED, ``broker_error_code`` exactly
    for QUERY_FAILED, ``failure_code`` exactly for non-success outcomes.
    ``applied_at_utc`` is the committed state-history timestamp (broker
    clock domain) and is deliberately not ordered against
    ``reserved_at_utc`` (see the module docstring's timestamp contract).
    """

    attempt_id: str
    run_id: str
    mode: RepairMode
    target_type: ReconciliationTargetType
    target_id: str
    outcome: RepairOutcome
    detail: str
    reserved_at_utc: datetime
    execution_request_id: str | None = None
    client_order_id: str | None = None
    reconciliation_internal_state: OrderState | None = None
    reconciliation_broker_state: OrderState | None = None
    internal_revision: int | None = None
    internal_state_before: OrderState | None = None
    internal_state_after: OrderState | None = None
    broker_order_id: str | None = None
    broker_error_code: BrokerErrorCode | None = None
    failure_code: str | None = None
    observed_at_utc: datetime | None = None
    applied_at_utc: datetime | None = None
    audit_error: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.attempt_id, "attempt_id")
        _require_non_empty_str(self.run_id, "run_id")
        if not isinstance(self.mode, RepairMode):
            raise ValueError(f"mode must be a RepairMode, got {self.mode!r}")
        if not isinstance(self.target_type, ReconciliationTargetType):
            raise ValueError(
                f"target_type must be a ReconciliationTargetType, got {self.target_type!r}"
            )
        _require_non_empty_str(self.target_id, "target_id")
        if not isinstance(self.outcome, RepairOutcome):
            raise ValueError(f"outcome must be a RepairOutcome, got {self.outcome!r}")
        _require_non_empty_str(self.detail, "detail")
        _require_utc(self.reserved_at_utc, "reserved_at_utc")
        for name in (
            "execution_request_id",
            "client_order_id",
            "broker_order_id",
            "failure_code",
            "audit_error",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_str(value, name)
        for name in (
            "reconciliation_internal_state",
            "reconciliation_broker_state",
            "internal_state_before",
            "internal_state_after",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, OrderState):
                raise ValueError(f"{name} must be an OrderState or None, got {value!r}")
        _require_optional_revision(self.internal_revision, "internal_revision")
        if self.broker_error_code is not None and not isinstance(
            self.broker_error_code, BrokerErrorCode
        ):
            raise ValueError(
                f"broker_error_code must be a BrokerErrorCode or None, got "
                f"{self.broker_error_code!r}"
            )
        if self.observed_at_utc is not None:
            _require_utc(self.observed_at_utc, "observed_at_utc")
        if self.applied_at_utc is not None:
            _require_utc(self.applied_at_utc, "applied_at_utc")

        is_execution_target = self.target_type is ReconciliationTargetType.EXECUTION_ORDER
        if is_execution_target != (self.execution_request_id is not None):
            raise ValueError(
                "execution_request_id must be present exactly for execution_order targets"
            )
        if is_execution_target and self.reconciliation_internal_state is None:
            raise ValueError(
                "an execution_order attempt requires the reconciliation-observed "
                "internal state"
            )

        if self.outcome is RepairOutcome.APPLIED and self.mode is not RepairMode.APPLY:
            raise ValueError("outcome 'applied' is only possible in apply mode")
        if self.outcome is RepairOutcome.WOULD_APPLY and self.mode is not RepairMode.DRY_RUN:
            raise ValueError("outcome 'would_apply' is only possible in dry_run mode")
        if (self.outcome is RepairOutcome.APPLIED) != (self.applied_at_utc is not None):
            raise ValueError("applied_at_utc must be set exactly when the outcome is 'applied'")
        if (self.outcome in _OUTCOMES_WITH_AFTER_STATE) != (self.internal_state_after is not None):
            raise ValueError(
                "internal_state_after must be set exactly for applied / would_apply / "
                "already_applied outcomes"
            )
        if (self.outcome is RepairOutcome.QUERY_FAILED) != (self.broker_error_code is not None):
            raise ValueError(
                "broker_error_code must be set exactly when the outcome is 'query_failed'"
            )
        if (self.outcome in _SUCCESS_OUTCOMES) != (self.failure_code is None):
            raise ValueError(
                "failure_code must be set exactly for non-success outcomes (everything "
                "except applied / would_apply / already_applied)"
            )


@dataclass(frozen=True)
class ReconciliationRepairResult:
    """The result of one repair pass over one stored reconciliation run.

    ``attempts`` holds one entry per discrepancy of the run, in observation
    order -- eligible or not, so the pass is auditable as a whole. The pass
    itself has no identity of its own (each attempt is independently keyed
    and independently durable in the audit DB); the run it acted on is the
    linkage. When a pass aborts mid-way, the caller receives the completed
    attempts on the raised error instead of this object (see the service's
    ``ReconciliationRepairRunError``), and the audit DB holds them too.
    """

    run_id: str
    mode: RepairMode
    started_at_utc: datetime
    finished_at_utc: datetime
    attempts: tuple[OrderRepairAttempt, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_non_empty_str(self.run_id, "run_id")
        if not isinstance(self.mode, RepairMode):
            raise ValueError(f"mode must be a RepairMode, got {self.mode!r}")
        _require_utc(self.started_at_utc, "started_at_utc")
        _require_utc(self.finished_at_utc, "finished_at_utc")
        if self.finished_at_utc < self.started_at_utc:
            raise ValueError(
                f"finished_at_utc must not precede started_at_utc: "
                f"{self.started_at_utc.isoformat()} -> {self.finished_at_utc.isoformat()}"
            )
        if not isinstance(self.attempts, tuple):
            raise ValueError(f"attempts must be a tuple, got {type(self.attempts).__name__}")
        for entry in self.attempts:
            if not isinstance(entry, OrderRepairAttempt):
                raise ValueError(
                    f"every attempts entry must be an OrderRepairAttempt, got {entry!r}"
                )
            if entry.run_id != self.run_id:
                raise ValueError(
                    f"attempt {entry.attempt_id!r} belongs to run {entry.run_id!r}, "
                    f"not {self.run_id!r}"
                )
            if entry.mode is not self.mode:
                raise ValueError(
                    f"attempt {entry.attempt_id!r} carries mode {entry.mode.value!r}, "
                    f"the pass ran as {self.mode.value!r}"
                )
        attempt_ids = [entry.attempt_id for entry in self.attempts]
        if len(set(attempt_ids)) != len(attempt_ids):
            raise ValueError("attempt_id values must be unique within a repair result")

    def by_outcome(self, outcome: RepairOutcome) -> tuple[OrderRepairAttempt, ...]:
        """The attempts with one outcome, in observation order."""
        if not isinstance(outcome, RepairOutcome):
            raise ValueError(f"outcome must be a RepairOutcome, got {outcome!r}")
        return tuple(a for a in self.attempts if a.outcome is outcome)

    def outcome_counts(self) -> dict[RepairOutcome, int]:
        """Attempt count per outcome (every member present)."""
        counts = {outcome: 0 for outcome in RepairOutcome}
        for attempt in self.attempts:
            counts[attempt.outcome] += 1
        return counts

    @property
    def has_audit_failures(self) -> bool:
        """Whether any attempt's reserved audit row could not be finalized.

        The order-state change (if any) is durable regardless -- the state
        history carries the STRUCTURED provenance -- and the reservation is
        still pending in the audit DB; recovery finalizes it under the same
        attempt id. The pass must not be presented as fully audited while
        this is True.
        """
        return any(a.audit_error is not None for a in self.attempts)
