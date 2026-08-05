"""Broker reconciliation REPAIR common models (Issue #104).

Vocabulary
----------

- :class:`OrderRepairAttempt` -- one decision about one stored discrepancy
  of one reconciliation run (Issue #103): what was attempted, against which
  target, what the states were before/after and how it ended
  (:class:`RepairOutcome`). Every attempt -- applied or not -- is an audit
  event, separate from the reconciliation OBSERVATION it acted on: the
  stored ``broker_reconciliation_runs`` / ``..._discrepancies`` rows are
  never updated or flagged by a repair.
- :class:`ReconciliationRepairResult` -- the attempts of one repair pass
  over one reconciliation run, plus the pass metadata.

Reused, not redefined (Issue #74 / #73 / #103 contract): order states are
``broker.broker_models.OrderState``, query failures carry the existing
``BrokerErrorCode`` and the discrepancy identity reuses
``reconciliation_models.ReconciliationTargetType``. This module introduces
NO order state, NO transition table and NO parallel error taxonomy -- and no
repair is decided here: models only.

Outcome design (Issue #104): the outcome is an Enum, never a free string,
so "the repair changed something" (``APPLIED``) is machine-distinguishable
from every way it deliberately did NOT -- already applied, stale
observation, conflicting re-query, ineligible input, refused transition,
failed query, failed store write. A repeated repair of the same target must
therefore never look like a second success.

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
    order store through the existing state machine, and every attempt is
    appended to the repair audit table.
    ``DRY_RUN`` -- detect-only counterpart: the full eligibility evaluation
    runs (execution store reads and broker queries only), but NOTHING is
    written -- no order update, no history row, no audit row, no DB
    creation or migration, no state file. The mode is carried on every
    attempt so an audit can tell what a result was permitted to do.
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
    terminal outcome (this or an earlier/concurrent repair, or the normal
    submission path, got there first); nothing was written. Deliberately NOT
    a success: re-running a repair must not look like it changed something.
    ``SKIPPED``         -- the discrepancy is not an eligible input at all:
    ``MANUAL_REVIEW`` / ``QUERY_FAILED`` categories and broker-only targets
    are never auto-repaired.
    ``STALE``           -- the internal state at repair time differs from
    the state the reconciliation run observed (another process moved the
    order on); the old observation must not overwrite the newer state.
    ``CONFLICT``        -- the repair-time re-query contradicts the stored
    reconciliation observation (different broker state, a broker reference
    ID mismatch, or a ``broker_order_id`` another execution request owns);
    nothing is overwritten -- a fresh reconciliation run re-classifies.
    ``REJECTED``        -- the existing state machine / store contract
    refuses the update (forbidden transition, identity mismatch, snapshot
    update contract violation, backwards timestamp).
    ``QUERY_FAILED``    -- the repair-time broker query failed (typed
    ``BrokerError``, contract violation, or the order was not found); no
    state is inferred and nothing changes.
    ``FAILED``          -- the execution store could not answer or the write
    failed; the store's transaction guarantees the order DB was left
    untouched.
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


@dataclass(frozen=True)
class OrderRepairAttempt:
    """One audited repair decision about one stored discrepancy (Issue #104).

    ``detail`` is the mandatory human-readable reason for the outcome (why
    the attempt applied, or exactly why it did not). ``audit_error`` is
    IN-MEMORY metadata only: it is set (via ``dataclasses.replace``) when
    the attempt's own audit row could not be appended after a successful
    state update, and it is never persisted -- a stored attempt row is by
    definition one whose audit append succeeded. Recovery from a set
    ``audit_error`` is re-running the repair: the state history row written
    by the update carries the run/attempt provenance in its reason, and the
    re-run records an ``ALREADY_APPLIED`` audit row.

    Invariants (all enforced):

    - non-empty ids/detail, typed enums, timezone-aware UTC timestamps,
    - ``execution_request_id`` exists exactly for ``EXECUTION_ORDER``
      targets; ``reconciliation_internal_state`` is then required (the
      Issue #103 record always observed one),
    - ``APPLIED`` happens only in ``APPLY`` mode; ``WOULD_APPLY`` only in
      ``DRY_RUN``,
    - ``applied_at_utc`` exists exactly for ``APPLIED``,
    - the after-state exists exactly for APPLIED / WOULD_APPLY /
      ALREADY_APPLIED,
    - ``broker_error_code`` exists exactly for ``QUERY_FAILED``,
    - ``attempted_at_utc <= applied_at_utc`` when both exist.
    """

    attempt_id: str
    run_id: str
    mode: RepairMode
    target_type: ReconciliationTargetType
    target_id: str
    outcome: RepairOutcome
    detail: str
    attempted_at_utc: datetime
    execution_request_id: str | None = None
    client_order_id: str | None = None
    reconciliation_internal_state: OrderState | None = None
    reconciliation_broker_state: OrderState | None = None
    internal_state_before: OrderState | None = None
    internal_state_after: OrderState | None = None
    broker_order_id: str | None = None
    broker_error_code: BrokerErrorCode | None = None
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
        _require_utc(self.attempted_at_utc, "attempted_at_utc")
        for name in ("execution_request_id", "client_order_id", "broker_order_id", "audit_error"):
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
        if self.applied_at_utc is not None and self.applied_at_utc < self.attempted_at_utc:
            raise ValueError(
                f"applied_at_utc must not precede attempted_at_utc: "
                f"{self.attempted_at_utc.isoformat()} -> {self.applied_at_utc.isoformat()}"
            )


@dataclass(frozen=True)
class ReconciliationRepairResult:
    """The result of one repair pass over one stored reconciliation run.

    ``attempts`` holds one entry per discrepancy of the run, in observation
    order -- eligible or not, so the pass is auditable as a whole. The pass
    itself has no identity of its own (each attempt is independently keyed);
    the run it acted on is the linkage.
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
        """Whether any attempt's own audit row could not be appended.

        The order-state change (if any) is durable regardless -- the state
        history carries the provenance -- but the pass must not be presented
        as fully audited while this is True; re-running the repair records
        the missing rows as ``already_applied``.
        """
        return any(a.audit_error is not None for a in self.attempts)
