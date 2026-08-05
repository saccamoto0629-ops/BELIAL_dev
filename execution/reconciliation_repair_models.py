"""Broker reconciliation REPAIR common models (Issue #104).

Attempt lifecycle (Issue #104 re-review: owner + lease + generation)
--------------------------------------------------------------------

Every repair attempt in APPLY mode is a CLAIMED, two-phase audit record:

1. :class:`RepairAttemptReservation` -- appended to the audit DB with status
   ``pending`` BEFORE anything is evaluated or written. The reservation is a
   CLAIM: it carries the claiming worker (``owner_id``), the claim execution
   (``claim_execution_id`` -- unique per acquisition, so two executions of
   the same worker are distinguishable), the fencing ``generation`` (starts
   at 1) and the ``lease_expires_at_utc`` deadline.
2. While the lease is valid, NOBODY else -- recovery included -- may touch
   the pending attempt. After the lease expires, a recovery worker may TAKE
   OVER: an atomic compare-and-set increments the generation, replaces the
   owner / claim execution id and extends the lease. Exactly one of any
   number of concurrent takeover attempts wins.
3. The generation is a FENCING TOKEN: it is mirrored into the execution
   order DB (``execution_repair_fences``) and verified INSIDE the state
   update transaction, so a stale worker that lost its claim can never
   commit -- even if the order's state and revision still match.
4. :class:`OrderRepairAttempt` -- the FINALIZED view. Finalize is
   compare-and-set on (pending status, owner, claim execution id,
   generation): a worker that lost its claim cannot finalize either, and an
   attempt is finalized exactly once.

Vocabulary
----------

- :class:`RepairMode` -- what a pass was allowed to do (apply / dry_run).
- :class:`RepairOutcome` -- how one attempt ended, machine-readable.
- :class:`RepairFailureCode` -- the CLOSED SET of stable failure codes every
  non-success outcome must carry (model validation, store serialization /
  restoration and the SQLite CHECK all share this one enum; an unknown code
  in a stored row fails restore fail-closed).
- :class:`ReconciliationRepairResult` -- the attempts of one repair pass.

Reused, not redefined (Issue #74 / #73 / #103 contract): order states are
``broker.broker_models.OrderState``, query failures carry the existing
``BrokerErrorCode`` and the discrepancy identity reuses
``reconciliation_models.ReconciliationTargetType``. This module introduces
NO order state, NO transition table and NO parallel error taxonomy -- and no
repair is decided here: models only.

Timestamp contract (Issue #104 re-review)
-----------------------------------------

- ``reserved_at_utc``   -- when the attempt was reserved / first claimed
  (repair-side clock, taken BEFORE any side effect of the attempt).
- ``last_claimed_at_utc`` -- when the CURRENT claim was acquired (equals
  ``reserved_at_utc`` until a takeover).
- ``lease_expires_at_utc`` -- when the current claim stops protecting the
  attempt from takeover.
- ``observed_at_utc``   -- when the reconciliation run observed the
  discrepancy (copied from Issue #103's record).
- ``broker_state_updated_at_utc`` -- the BROKER-REPORTED timestamp of the
  applied snapshot (``OrderResult.updated_at_utc``), which is exactly the
  value the execution store stamps on the committed history row. It is
  known BEFORE the commit and lives in the BROKER clock domain -- it is
  neither an internal commit time nor ordered against ``reserved_at_utc``.
- ``finalized_at_utc``  -- when the audit row was finalized (repair-side
  clock, taken immediately BEFORE the finalize write; a clock failure there
  degrades to a recoverable finalize failure, never to a mislabeled
  outcome).
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
    claimed (reserved) in the audit DB before evaluation and finalized
    after.
    ``DRY_RUN`` -- detect-only counterpart: the full eligibility evaluation
    runs (execution store reads and broker queries only), but NOTHING is
    written -- no order update, no history row, no claim / reservation, no
    fence, no audit row, no DB creation or migration, no state file. The
    mode is carried on every attempt so an audit can tell what a result was
    permitted to do.
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
    ``CONFLICT``        -- the repair must not proceed against the CURRENT
    reality: the repair-time re-query contradicts the stored observation or
    the internal record, a broker reference ID would be lost / replaced /
    is owned elsewhere, or this worker LOST ITS CLAIM (a fencing takeover
    or a competing attempt now owns the target -- ``claim_lost`` /
    ``repair_fence_conflict``). Nothing is overwritten.
    ``REJECTED``        -- the existing state machine / store contract
    refuses the update (forbidden transition, identity mismatch, snapshot
    update contract violation, backwards timestamp, broken id binding).
    ``QUERY_FAILED``    -- the repair-time broker query failed (typed
    ``BrokerError``, boundary contract violation, or the order was not
    found); no state is inferred and nothing changes.
    ``FAILED``          -- the execution order DB is PROVEN unchanged by
    this attempt: the store refused/failed before commit (its transaction
    rolled back) or a required read did not answer. Never used for an
    attempt whose state update committed.
    ``BLOCKED``         -- the takeover protocol found a state it must NOT
    resolve automatically (a fence ahead of the intent, a mismatched
    identity, several active intents, a corrupt chain). Nothing is queried
    and nothing is written to the order store; the attempt stays PENDING and
    the durable record is the takeover intent row (status ``blocked`` with a
    reason code and the observed generations / identities). Reported to the
    caller so a human can act -- never a silent skip. APPLY mode only, and
    never stored as a finalized attempt outcome.
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
    BLOCKED = "blocked"


class RepairFailureCode(enum.Enum):
    """The CLOSED SET of stable failure codes (Issue #104 re-review).

    One vocabulary shared by model validation, store serialization /
    restoration, the SQLite CHECK constraint (generated from this enum) and
    the logs. Audit details and logs never carry raw exception text -- the
    code plus at most our own typed exception class names.
    """

    NOT_ELIGIBLE_BROKER_ONLY = "not_eligible_broker_only"
    NOT_ELIGIBLE_QUERY_FAILED = "not_eligible_query_failed"
    NOT_ELIGIBLE_MANUAL_REVIEW = "not_eligible_manual_review"
    CANDIDATE_CONTRACT_VIOLATION = "candidate_contract_violation"
    ORDER_MISSING = "order_missing"
    ORDER_WITHOUT_BROKER_STATE = "order_without_broker_state"
    CLIENT_ORDER_ID_BINDING_MISMATCH = "client_order_id_binding_mismatch"
    EXECUTION_STORE_READ_FAILED = "execution_store_read_failed"
    BROKER_ID_OWNER_LOOKUP_FAILED = "broker_id_owner_lookup_failed"
    MISSING_OBSERVED_REVISION = "missing_observed_revision"
    INTERNAL_ORDER_CHANGED = "internal_order_changed"
    BROKER_ANSWERED_FOREIGN_ORDER = "broker_answered_foreign_order"
    REQUERY_DISAGREES_WITH_OBSERVATION = "requery_disagrees_with_observation"
    BROKER_REFERENCE_CHANGED = "broker_reference_changed"
    BROKER_REFERENCE_OWNED_ELSEWHERE = "broker_reference_owned_elsewhere"
    ALREADY_APPLIED_CONTENT_MISMATCH = "already_applied_content_mismatch"
    SNAPSHOT_IDENTITY_MISMATCH = "snapshot_identity_mismatch"
    FORBIDDEN_TRANSITION = "forbidden_transition"
    SNAPSHOT_CONTRACT_VIOLATION = "snapshot_contract_violation"
    SNAPSHOT_OLDER_THAN_STORED_STATE = "snapshot_older_than_stored_state"
    BROKER_QUERY_FAILED = "broker_query_failed"
    BROKER_ORDER_NOT_FOUND = "broker_order_not_found"
    BROKER_CONTRACT_VIOLATION = "broker_contract_violation"
    UNIQUENESS_CONFLICT = "uniqueness_conflict"
    STORE_REFUSED_UPDATE = "store_refused_update"
    STORE_UPDATE_FAILED = "store_update_failed"
    CAS_CONFLICT_UNRESOLVABLE = "cas_conflict_unresolvable"
    CLAIM_LOST = "claim_lost"
    REPAIR_FENCE_CONFLICT = "repair_fence_conflict"
    # takeover protocol (re-review r4): reported with outcome BLOCKED
    TAKEOVER_INTENT_MISSING = "takeover_intent_missing"
    TAKEOVER_IDENTITY_MISMATCH = "takeover_identity_mismatch"
    FENCE_AHEAD = "fence_ahead"
    MULTIPLE_ACTIVE_INTENTS = "multiple_active_intents"
    CLAIM_INTENT_MISMATCH = "claim_intent_mismatch"
    FENCE_INTENT_MISMATCH = "fence_intent_mismatch"
    TAKEOVER_PROTOCOL_CORRUPT = "takeover_protocol_corrupt"


class TakeoverIntentStatus(enum.Enum):
    """The CLOSED lifecycle of one durable takeover intent (r4 review).

    ``PREPARED``        -- the intent is committed in the reconciliation DB
    but nothing else has moved yet. This is what makes a takeover
    RESTARTABLE: the identity (takeover id, target owner, target claim
    execution id, target generation) is durable BEFORE the first cross-DB
    side effect, so a worker that dies mid-protocol is resumed with the SAME
    identity instead of inventing a new one.
    ``FENCE_INSTALLED`` -- the execution DB fence carries exactly this
    intent's identity.
    ``CLAIM_PROMOTED``  -- the attempt's claim row carries exactly this
    intent's identity (the takeover is now effective).
    ``COMPLETED``       -- the attempt was finalized under this intent.
    ``CANCELLED``       -- the takeover became UNNECESSARY before it took
    effect: the attempt was finalized by the claim this intent meant to
    replace (a slow-but-alive worker finished first). Nothing is wrong, the
    intent simply never applies -- terminal, and never resumed.
    ``BLOCKED``         -- the protocol found a state it must not resolve
    automatically; a reason code and the observed generations / identities
    are stored. Never resumed automatically.
    """

    PREPARED = "prepared"
    FENCE_INSTALLED = "fence_installed"
    CLAIM_PROMOTED = "claim_promoted"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


#: Statuses whose intent is still driving a takeover (at most one per attempt).
ACTIVE_INTENT_STATUSES: frozenset[TakeoverIntentStatus] = frozenset(
    {
        TakeoverIntentStatus.PREPARED,
        TakeoverIntentStatus.FENCE_INSTALLED,
        TakeoverIntentStatus.CLAIM_PROMOTED,
    }
)

#: Forward-only transition table. Re-applying the CURRENT status is an
#: idempotent no-op (handled by the store); anything not listed here is
#: refused, so a completed or blocked intent never moves again.
ALLOWED_INTENT_TRANSITIONS: dict[TakeoverIntentStatus, frozenset[TakeoverIntentStatus]] = {
    TakeoverIntentStatus.PREPARED: frozenset(
        {
            TakeoverIntentStatus.FENCE_INSTALLED,
            TakeoverIntentStatus.CANCELLED,
            TakeoverIntentStatus.BLOCKED,
        }
    ),
    TakeoverIntentStatus.FENCE_INSTALLED: frozenset(
        {
            TakeoverIntentStatus.CLAIM_PROMOTED,
            TakeoverIntentStatus.CANCELLED,
            TakeoverIntentStatus.BLOCKED,
        }
    ),
    TakeoverIntentStatus.CLAIM_PROMOTED: frozenset(
        {TakeoverIntentStatus.COMPLETED, TakeoverIntentStatus.BLOCKED}
    ),
    TakeoverIntentStatus.COMPLETED: frozenset(),
    TakeoverIntentStatus.CANCELLED: frozenset(),
    TakeoverIntentStatus.BLOCKED: frozenset(),
}


class TakeoverBlockedReason(enum.Enum):
    """Why a takeover was parked for a human -- CLOSED set (r4 review).

    Stored on the intent row (DB CHECK generated from this enum) together
    with the observed generations and fence identity, so an operator can see
    exactly which inconsistency stopped the protocol.
    """

    FENCE_AHEAD = "fence_ahead"
    FENCE_IDENTITY_MISMATCH = "fence_identity_mismatch"
    ORPHAN_FENCE_WITHOUT_INTENT = "orphan_fence_without_intent"
    CLAIM_AHEAD_OF_INTENT = "claim_ahead_of_intent"
    CLAIM_IDENTITY_MISMATCH = "claim_identity_mismatch"
    MULTIPLE_ACTIVE_INTENTS = "multiple_active_intents"
    INTENT_SOURCE_MISMATCH = "intent_source_mismatch"
    ATTEMPT_ALREADY_FINALIZED = "attempt_already_finalized"
    PROTOCOL_CORRUPT = "protocol_corrupt"


#: Which repair failure code each blocked reason is reported as.
BLOCKED_REASON_FAILURE_CODES: dict[TakeoverBlockedReason, RepairFailureCode] = {
    TakeoverBlockedReason.FENCE_AHEAD: RepairFailureCode.FENCE_AHEAD,
    TakeoverBlockedReason.FENCE_IDENTITY_MISMATCH: RepairFailureCode.FENCE_INTENT_MISMATCH,
    TakeoverBlockedReason.ORPHAN_FENCE_WITHOUT_INTENT: RepairFailureCode.TAKEOVER_INTENT_MISSING,
    TakeoverBlockedReason.CLAIM_AHEAD_OF_INTENT: RepairFailureCode.CLAIM_INTENT_MISMATCH,
    TakeoverBlockedReason.CLAIM_IDENTITY_MISMATCH: RepairFailureCode.CLAIM_INTENT_MISMATCH,
    TakeoverBlockedReason.MULTIPLE_ACTIVE_INTENTS: RepairFailureCode.MULTIPLE_ACTIVE_INTENTS,
    TakeoverBlockedReason.INTENT_SOURCE_MISMATCH: RepairFailureCode.TAKEOVER_IDENTITY_MISMATCH,
    TakeoverBlockedReason.ATTEMPT_ALREADY_FINALIZED: (
        RepairFailureCode.TAKEOVER_PROTOCOL_CORRUPT
    ),
    TakeoverBlockedReason.PROTOCOL_CORRUPT: RepairFailureCode.TAKEOVER_PROTOCOL_CORRUPT,
}


# Outcomes that carry the resulting/planned internal state.
_OUTCOMES_WITH_AFTER_STATE: frozenset[RepairOutcome] = frozenset(
    {RepairOutcome.APPLIED, RepairOutcome.WOULD_APPLY, RepairOutcome.ALREADY_APPLIED}
)

# Outcomes that must NOT carry a failure code (everything else requires one).
_SUCCESS_OUTCOMES: frozenset[RepairOutcome] = _OUTCOMES_WITH_AFTER_STATE

# Permitted (outcome -> failure code) combinations: a code can never claim a
# different failure family than the outcome it is stored with.
_ALLOWED_FAILURE_CODES: dict[RepairOutcome, frozenset[RepairFailureCode]] = {
    RepairOutcome.SKIPPED: frozenset(
        {
            RepairFailureCode.NOT_ELIGIBLE_BROKER_ONLY,
            RepairFailureCode.NOT_ELIGIBLE_QUERY_FAILED,
            RepairFailureCode.NOT_ELIGIBLE_MANUAL_REVIEW,
        }
    ),
    RepairOutcome.STALE: frozenset(
        {
            RepairFailureCode.MISSING_OBSERVED_REVISION,
            RepairFailureCode.INTERNAL_ORDER_CHANGED,
        }
    ),
    RepairOutcome.CONFLICT: frozenset(
        {
            RepairFailureCode.CANDIDATE_CONTRACT_VIOLATION,
            RepairFailureCode.BROKER_ANSWERED_FOREIGN_ORDER,
            RepairFailureCode.REQUERY_DISAGREES_WITH_OBSERVATION,
            RepairFailureCode.BROKER_REFERENCE_CHANGED,
            RepairFailureCode.BROKER_REFERENCE_OWNED_ELSEWHERE,
            RepairFailureCode.ALREADY_APPLIED_CONTENT_MISMATCH,
            RepairFailureCode.UNIQUENESS_CONFLICT,
            RepairFailureCode.CLAIM_LOST,
            RepairFailureCode.REPAIR_FENCE_CONFLICT,
        }
    ),
    RepairOutcome.REJECTED: frozenset(
        {
            RepairFailureCode.CANDIDATE_CONTRACT_VIOLATION,
            RepairFailureCode.CLIENT_ORDER_ID_BINDING_MISMATCH,
            RepairFailureCode.SNAPSHOT_IDENTITY_MISMATCH,
            RepairFailureCode.FORBIDDEN_TRANSITION,
            RepairFailureCode.SNAPSHOT_CONTRACT_VIOLATION,
            RepairFailureCode.SNAPSHOT_OLDER_THAN_STORED_STATE,
            RepairFailureCode.STORE_REFUSED_UPDATE,
        }
    ),
    RepairOutcome.QUERY_FAILED: frozenset(
        {
            RepairFailureCode.BROKER_QUERY_FAILED,
            RepairFailureCode.BROKER_ORDER_NOT_FOUND,
            RepairFailureCode.BROKER_CONTRACT_VIOLATION,
        }
    ),
    RepairOutcome.FAILED: frozenset(
        {
            RepairFailureCode.ORDER_MISSING,
            RepairFailureCode.ORDER_WITHOUT_BROKER_STATE,
            RepairFailureCode.EXECUTION_STORE_READ_FAILED,
            RepairFailureCode.BROKER_ID_OWNER_LOOKUP_FAILED,
            RepairFailureCode.STORE_UPDATE_FAILED,
            RepairFailureCode.CAS_CONFLICT_UNRESOLVABLE,
        }
    ),
    RepairOutcome.BLOCKED: frozenset(
        {
            RepairFailureCode.TAKEOVER_INTENT_MISSING,
            RepairFailureCode.TAKEOVER_IDENTITY_MISMATCH,
            RepairFailureCode.FENCE_AHEAD,
            RepairFailureCode.MULTIPLE_ACTIVE_INTENTS,
            RepairFailureCode.CLAIM_INTENT_MISMATCH,
            RepairFailureCode.FENCE_INTENT_MISMATCH,
            RepairFailureCode.TAKEOVER_PROTOCOL_CORRUPT,
        }
    ),
}


def _require_optional_revision(value, field_name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative int or None, got {value!r}")


def _require_generation(value, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be an int >= 1, got {value!r}")


def _require_takeover_id_for_generation(takeover_id, generation, *, subject: str) -> None:
    """Generation 1 is the ORIGINAL claim (no takeover); every later
    generation exists only because a durable takeover intent created it, so
    its id must be carried with the claim."""
    if generation == 1:
        if takeover_id is not None:
            raise ValueError(
                f"{subject} at generation 1 is the original claim and carries no takeover_id"
            )
        return
    if takeover_id is None:
        raise ValueError(
            f"{subject} at generation {generation} must name the takeover_id that created it"
        )
    _require_non_empty_str(takeover_id, "takeover_id")


@dataclass(frozen=True)
class RepairTakeoverIntent:
    """A DURABLE takeover intent -- the source of truth for one takeover.

    Committed to the reconciliation DB (``prepared``) BEFORE any cross-DB
    side effect, so every later step -- execution-DB fence install,
    reconciliation claim promotion, attempt finalize -- uses ONE identity
    that survives a process death: ``takeover_id`` + ``target_owner_id`` +
    ``target_claim_execution_id`` + ``target_generation``. A restarted
    worker LOADS the active intent and continues it; it never mints a new
    identity, and no step ever accepts a different one.

    ``source_*`` pins the claim being replaced (the CAS precondition), and
    ``target_generation`` is always ``source_generation + 1``. Rows are kept
    forever (completed / blocked included) as the audit trail of the
    identity chain.
    """

    takeover_id: str
    repair_attempt_id: str
    source_generation: int
    target_generation: int
    source_owner_id: str
    source_claim_execution_id: str
    target_owner_id: str
    target_claim_execution_id: str
    lease_expires_at_utc: datetime
    status: TakeoverIntentStatus
    created_at_utc: datetime
    fence_installed_at_utc: datetime | None = None
    claim_promoted_at_utc: datetime | None = None
    completed_at_utc: datetime | None = None
    cancelled_at_utc: datetime | None = None
    blocked_at_utc: datetime | None = None
    blocked_reason: TakeoverBlockedReason | None = None
    observed_claim_generation: int | None = None
    observed_fence_generation: int | None = None
    observed_fence_identity: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.takeover_id, "takeover_id")
        _require_non_empty_str(self.repair_attempt_id, "repair_attempt_id")
        _require_generation(self.source_generation, "source_generation")
        _require_generation(self.target_generation, "target_generation")
        if self.target_generation != self.source_generation + 1:
            raise ValueError(
                "target_generation must be source_generation + 1, got "
                f"{self.source_generation} -> {self.target_generation}"
            )
        for name in (
            "source_owner_id",
            "source_claim_execution_id",
            "target_owner_id",
            "target_claim_execution_id",
        ):
            _require_non_empty_str(getattr(self, name), name)
        if self.target_claim_execution_id == self.source_claim_execution_id:
            raise ValueError(
                "target_claim_execution_id must differ from the claim it replaces "
                "(a takeover is a NEW execution of the attempt)"
            )
        if not isinstance(self.status, TakeoverIntentStatus):
            raise ValueError(f"status must be a TakeoverIntentStatus, got {self.status!r}")
        _require_utc(self.created_at_utc, "created_at_utc")
        _require_utc(self.lease_expires_at_utc, "lease_expires_at_utc")
        if self.lease_expires_at_utc <= self.created_at_utc:
            raise ValueError(
                "lease_expires_at_utc must lie after created_at_utc: "
                f"{self.created_at_utc.isoformat()} -> {self.lease_expires_at_utc.isoformat()}"
            )
        for name in (
            "fence_installed_at_utc",
            "claim_promoted_at_utc",
            "completed_at_utc",
            "cancelled_at_utc",
            "blocked_at_utc",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_utc(value, name)
                if value < self.created_at_utc:
                    raise ValueError(f"{name} must not precede created_at_utc")
        if self.blocked_reason is not None and not isinstance(
            self.blocked_reason, TakeoverBlockedReason
        ):
            raise ValueError(
                f"blocked_reason must be a TakeoverBlockedReason or None, got "
                f"{self.blocked_reason!r}"
            )
        if self.observed_fence_identity is not None:
            _require_non_empty_str(self.observed_fence_identity, "observed_fence_identity")
        for name in ("observed_claim_generation", "observed_fence_generation"):
            value = getattr(self, name)
            if value is not None:
                _require_generation(value, name)

        # status <-> timestamp consistency (forward-only, no gaps)
        is_blocked = self.status is TakeoverIntentStatus.BLOCKED
        if is_blocked != (self.blocked_at_utc is not None):
            raise ValueError("blocked_at_utc must be set exactly for blocked intents")
        if is_blocked != (self.blocked_reason is not None):
            raise ValueError("blocked_reason must be set exactly for blocked intents")
        if not is_blocked and (
            self.observed_claim_generation is not None
            or self.observed_fence_generation is not None
            or self.observed_fence_identity is not None
        ):
            raise ValueError("observed_* diagnostics belong to blocked intents only")
        if (self.status is TakeoverIntentStatus.COMPLETED) != (self.completed_at_utc is not None):
            raise ValueError("completed_at_utc must be set exactly for completed intents")
        if (self.status is TakeoverIntentStatus.CANCELLED) != (self.cancelled_at_utc is not None):
            raise ValueError("cancelled_at_utc must be set exactly for cancelled intents")
        reached_fence = self.status in (
            TakeoverIntentStatus.FENCE_INSTALLED,
            TakeoverIntentStatus.CLAIM_PROMOTED,
            TakeoverIntentStatus.COMPLETED,
        )
        if reached_fence and self.fence_installed_at_utc is None:
            raise ValueError(f"status {self.status.value!r} requires fence_installed_at_utc")
        reached_claim = self.status in (
            TakeoverIntentStatus.CLAIM_PROMOTED,
            TakeoverIntentStatus.COMPLETED,
        )
        if reached_claim and self.claim_promoted_at_utc is None:
            raise ValueError(f"status {self.status.value!r} requires claim_promoted_at_utc")
        if self.status is TakeoverIntentStatus.PREPARED and (
            self.fence_installed_at_utc is not None or self.claim_promoted_at_utc is not None
        ):
            raise ValueError("a prepared intent has not installed a fence or promoted a claim")
        if (
            self.status is TakeoverIntentStatus.FENCE_INSTALLED
            and self.claim_promoted_at_utc is not None
        ):
            raise ValueError("a fence_installed intent has not promoted a claim yet")
        if (
            self.claim_promoted_at_utc is not None
            and self.fence_installed_at_utc is not None
            and self.claim_promoted_at_utc < self.fence_installed_at_utc
        ):
            raise ValueError("claim_promoted_at_utc must not precede fence_installed_at_utc")
        if (
            self.completed_at_utc is not None
            and self.claim_promoted_at_utc is not None
            and self.completed_at_utc < self.claim_promoted_at_utc
        ):
            raise ValueError("completed_at_utc must not precede claim_promoted_at_utc")

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_INTENT_STATUSES

    def fence_identity(self) -> str:
        """The identity string recorded as ``observed_fence_identity`` when a
        foreign fence blocks a takeover (stable, no free text)."""
        return (
            f"attempt={self.repair_attempt_id} generation={self.target_generation} "
            f"takeover={self.takeover_id} claim={self.target_claim_execution_id} "
            f"owner={self.target_owner_id}"
        )


@dataclass(frozen=True)
class RepairAttemptReservation:
    """The PENDING phase of one repair attempt -- a fenced CLAIM (Issue #104).

    Persisted BEFORE any evaluation or state change. While
    ``lease_expires_at_utc`` lies in the future the claim is untouchable;
    after that a recovery worker takes over by atomically incrementing
    ``generation`` and replacing ``owner_id`` / ``claim_execution_id``.
    Reservations exist in APPLY mode only -- dry-run claims nothing.
    """

    attempt_id: str
    run_id: str
    target_type: ReconciliationTargetType
    target_id: str
    reserved_at_utc: datetime
    detail: str
    owner_id: str
    claim_execution_id: str
    generation: int
    lease_expires_at_utc: datetime
    last_claimed_at_utc: datetime
    takeover_id: str | None = None
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
        _require_non_empty_str(self.owner_id, "owner_id")
        _require_non_empty_str(self.claim_execution_id, "claim_execution_id")
        _require_generation(self.generation, "generation")
        _require_takeover_id_for_generation(
            self.takeover_id, self.generation, subject="a reservation"
        )
        _require_utc(self.lease_expires_at_utc, "lease_expires_at_utc")
        _require_utc(self.last_claimed_at_utc, "last_claimed_at_utc")
        if self.lease_expires_at_utc <= self.last_claimed_at_utc:
            raise ValueError(
                f"lease_expires_at_utc must lie after last_claimed_at_utc: "
                f"{self.last_claimed_at_utc.isoformat()} -> "
                f"{self.lease_expires_at_utc.isoformat()}"
            )
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
    ``failure_code`` is a member of the closed :class:`RepairFailureCode`
    set, required for every non-success outcome, forbidden for APPLIED /
    WOULD_APPLY / ALREADY_APPLIED, and restricted to the outcome's permitted
    family. ``owner_id`` / ``claim_execution_id`` / ``generation`` name the
    claim that produced this outcome (finalize is compare-and-set on them);
    they exist exactly in APPLY mode. ``audit_error`` is IN-MEMORY metadata
    only: set (via ``dataclasses.replace``) when this attempt's reserved
    audit row could not be finalized (store failure or a lost claim); the
    reservation then stays pending -- or is finalized by its new owner --
    and is never overwritten with a contradicting outcome.
    """

    attempt_id: str
    run_id: str
    mode: RepairMode
    target_type: ReconciliationTargetType
    target_id: str
    outcome: RepairOutcome
    detail: str
    reserved_at_utc: datetime
    owner_id: str | None = None
    claim_execution_id: str | None = None
    generation: int | None = None
    takeover_id: str | None = None
    execution_request_id: str | None = None
    client_order_id: str | None = None
    reconciliation_internal_state: OrderState | None = None
    reconciliation_broker_state: OrderState | None = None
    internal_revision: int | None = None
    internal_state_before: OrderState | None = None
    internal_state_after: OrderState | None = None
    broker_order_id: str | None = None
    broker_error_code: BrokerErrorCode | None = None
    failure_code: RepairFailureCode | None = None
    observed_at_utc: datetime | None = None
    broker_state_updated_at_utc: datetime | None = None
    finalized_at_utc: datetime | None = None
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
        if self.failure_code is not None and not isinstance(self.failure_code, RepairFailureCode):
            raise ValueError(
                f"failure_code must be a RepairFailureCode or None, got {self.failure_code!r}"
            )
        for name in ("observed_at_utc", "broker_state_updated_at_utc", "finalized_at_utc"):
            value = getattr(self, name)
            if value is not None:
                _require_utc(value, name)

        # claim fields exist exactly in apply mode (dry-run claims nothing)
        is_apply = self.mode is RepairMode.APPLY
        if is_apply:
            _require_non_empty_str(self.owner_id, "owner_id")
            _require_non_empty_str(self.claim_execution_id, "claim_execution_id")
            _require_generation(self.generation, "generation")
            _require_takeover_id_for_generation(
                self.takeover_id, self.generation, subject="an attempt"
            )
        else:
            if (
                self.owner_id is not None
                or self.claim_execution_id is not None
                or self.generation is not None
                or self.takeover_id is not None
            ):
                raise ValueError(
                    "dry-run attempts carry no claim (owner/claim/generation/takeover)"
                )

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
        if (self.outcome is RepairOutcome.APPLIED) != (
            self.broker_state_updated_at_utc is not None
        ):
            raise ValueError(
                "broker_state_updated_at_utc must be set exactly when the outcome is "
                "'applied'"
            )
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
        if self.failure_code is not None and self.failure_code not in _ALLOWED_FAILURE_CODES.get(
            self.outcome, frozenset()
        ):
            raise ValueError(
                f"failure_code {self.failure_code.value!r} is not permitted for outcome "
                f"{self.outcome.value!r}"
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
        """Whether any attempt's reserved audit row could not be finalized by
        THIS worker (a store failure or a lost claim).

        The order-state change (if any) is durable regardless -- the state
        history carries the STRUCTURED provenance -- and the reservation is
        either still pending (recovered later under the same attempt id) or
        already finalized by its new owner. The pass must not be presented
        as fully audited while this is True.
        """
        return any(a.audit_error is not None for a in self.attempts)
