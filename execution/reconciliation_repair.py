"""Broker reconciliation -- SAFE ORDER-STATE REPAIR (Issue #104).

*** RESTRICTED BY CONSTRUCTION. This service consumes the result of a
detection run (Issue #103, ``broker_reconciliation``) and reflects onto
INTERNAL ORDER STATE ONLY those broker observations that the EXISTING
order state machine and execution-store contract (Issue #74) can absorb
without losing information. Every state change goes through
``execution_store.record_order_result`` -- the validated, transactional
Issue #74 update path -- and through nothing else: this module opens no
SQLite connection, issues no SQL and never calls a broker write operation
(``submit_order`` / ``cancel_order`` are pinned to zero repo-wide). ***

What a repair run does
----------------------

Input is a :class:`ReconciliationRun` (Issue #103). For every discrepancy
one :class:`OrderRepairAction` is produced -- eligible or not, so the
result accounts for every target the detection run surfaced:

1. Only ``AUTO_REPAIR_CANDIDATE`` discrepancies about an
   ``EXECUTION_ORDER`` are eligible. ``MANUAL_REVIEW`` and
   ``QUERY_FAILED`` discrepancies -- broker-only orders, orders the
   broker does not know, contradicted terminal states, both-sides-UNKNOWN,
   failed queries -- are NEVER auto-repaired (``NOT_ELIGIBLE``; the broker
   is not even queried for them).
2. The broker is queried FRESH at repair time (``get_order``). A failed
   query (``QUERY_FAILED``) or an answer that confirms nothing -- state
   UNKNOWN, state PENDING, or the order no longer found
   (``BROKER_STATE_UNCONFIRMED``) -- changes NOTHING: a repair only ever
   reflects a broker-CONFIRMED state (accepted / partially_filled /
   filled / cancelled / rejected).
3. The CURRENT internal state is re-read. If it already equals the
   observed broker state the target is ``ALREADY_CONSISTENT`` and nothing
   is written -- this is what makes re-applying the same run idempotent:
   the second application performs ZERO writes and appends ZERO history
   rows. If it moved to anything else since detection the input is stale
   (``INTERNAL_STATE_CHANGED``) and nothing is written; a fresh detection
   run decides again.
4. The internal state must be one Issue #104 allows to leave
   automatically: UNKNOWN, PENDING (= submission phase ``submitting``),
   ACCEPTED or PARTIALLY_FILLED. Terminal states are never left.
5. Repairability is re-proven at repair time with the SAME shared
   judgement detection used (``classify_order_repairability`` -- the
   Issue #73 validators plus the store's documented preconditions).
   Any blocker refuses the repair (``REFUSED``), fail-closed.
6. ``apply`` mode records the fresh broker snapshot through
   ``record_order_result``; ``dry_run`` mode stops here and reports
   ``WOULD_REPAIR``. The store re-validates everything again inside one
   transaction (state machine, identity, anchored snapshot, broker-id
   chain and DB-wide uniqueness, timestamp monotonicity); a refusal rolls
   the whole update back and is reported as ``REFUSED``.

What is recorded (Issue #104): the store's history row carries the broker
snapshot itself (fills, ``broker_order_id``), ``changed_at_utc`` = the
broker snapshot's ``updated_at_utc``, and a ``reason`` naming the source
reconciliation run, the broker-confirmed state, the repair-time
observation timestamp and the broker reference IDs (``client_order_id`` /
``broker_order_id``). The returned :class:`OrderRepairRun` additionally
holds, per target: the id, the state before, the state after and the
reason for every outcome.

Dry run (照合のみモード): ``RepairMode.DRY_RUN`` performs the same
read-only checks -- broker queries included -- but writes NOTHING: no DB
update, no history row, no state change, no file creation. The result is
returned (and logged) only.

Repair results are NOT persisted anywhere by this service (return value +
log only). The audit trail of what happened to the ORDER lives where it
always lives: the execution order store's state history. The
reconciliation audit DB (Issue #103) stays detection-only and is not
touched here.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from broker.broker_interface import Broker
from broker.broker_models import (
    BrokerError,
    BrokerInvalidRequestError,
    BrokerOrderNotFoundError,
    OrderResult,
    OrderState,
    TERMINAL_ORDER_STATES,
)
from execution import execution_store
from execution.broker_reconciliation import classify_order_repairability
from execution.execution_models import _require_non_empty_str, _require_utc
from execution.execution_store import ExecutionStoreError
from execution.reconciliation_models import (
    OrderDiscrepancy,
    ReconciliationRun,
    ReconciliationTargetType,
    ResolutionCategory,
)

_LOGGER = logging.getLogger(__name__)

# Internal states a repair may leave automatically (Issue #104): the
# unsettled ones. PENDING is the broker state tracked while the submission
# phase is ``submitting``. Terminal states are never left -- also enforced
# by the state machine itself; the explicit set documents the whitelist.
_REPAIRABLE_INTERNAL_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.UNKNOWN,
        OrderState.PENDING,
        OrderState.ACCEPTED,
        OrderState.PARTIALLY_FILLED,
    }
)

# Broker states a repair may reflect: only what the broker CONFIRMED.
# UNKNOWN confirms nothing (never reflected -- an explicit Issue #104
# prohibition) and PENDING means "no broker acknowledgement yet".
_REFLECTABLE_BROKER_STATES: frozenset[OrderState] = TERMINAL_ORDER_STATES | frozenset(
    {OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED}
)


class RepairMode(enum.Enum):
    """What a repair run is ALLOWED to do.

    ``DRY_RUN`` -- evaluate only (照合のみモード): broker queries and store
    reads happen, but no DB update, no history row, no state change.
    ``APPLY``   -- additionally record eligible observations through
    ``execution_store.record_order_result``.
    """

    DRY_RUN = "dry_run"
    APPLY = "apply"


class RepairOutcome(enum.Enum):
    """What happened to one repair target.

    ``REPAIRED``       -- the broker observation was recorded through the
    order state machine (``apply`` mode only).
    ``WOULD_REPAIR``   -- every check passed; ``dry_run`` mode stopped
    before the store call (``dry_run`` mode only).
    ``ALREADY_CONSISTENT`` -- the internal state already equals the
    observed broker state; nothing was written (the idempotent-replay
    outcome).
    ``NOT_ELIGIBLE``   -- the discrepancy is not an auto-repair candidate
    (manual review / query-failed / broker-only); the broker was not even
    queried and nothing was written.
    ``QUERY_FAILED``   -- the repair-time broker query failed; nothing is
    inferred and nothing was written.
    ``BROKER_STATE_UNCONFIRMED`` -- the repair-time answer confirms no
    settled state (UNKNOWN, PENDING, or order not found); nothing was
    written.
    ``INTERNAL_STATE_CHANGED`` -- the internal state moved since the
    detection run and no longer matches the discrepancy; the stale input
    was not applied.
    ``REFUSED``        -- the order state machine / store contract refused
    the update (or a store read needed to prove it failed, fail-closed);
    nothing was committed.
    """

    REPAIRED = "repaired"
    WOULD_REPAIR = "would_repair"
    ALREADY_CONSISTENT = "already_consistent"
    NOT_ELIGIBLE = "not_eligible"
    QUERY_FAILED = "query_failed"
    BROKER_STATE_UNCONFIRMED = "broker_state_unconfirmed"
    INTERNAL_STATE_CHANGED = "internal_state_changed"
    REFUSED = "refused"


@dataclass(frozen=True)
class OrderRepairAction:
    """The result for ONE repair target (Issue #104).

    ``target_id`` is the ``execution_request_id`` (or, for a broker-only
    discrepancy that is never adopted, the broker-side ``client_order_id``
    -- mirroring :class:`ReconciliationTargetType`). ``state_before`` /
    ``state_after`` are the internal order state around the action;
    ``reason`` is the mandatory human-readable WHY. ``broker_state`` is
    the repair-time broker observation (None when none was obtained) and
    ``observed_at_utc`` is when this service observed it.

    Invariants (all enforced):

    - ``REPAIRED`` / ``WOULD_REPAIR`` carry both states, the observed
      broker state (== ``state_after``, a broker-confirmed state), the
      observation time and the ``client_order_id``; the state actually
      CHANGES (equal states are ``ALREADY_CONSISTENT`` by construction)
      and ``state_before`` is a repairable (non-terminal) state.
    - every other outcome asserts NO state change: ``state_after`` equals
      ``state_before`` (both may be None when the row could not be read).
    - ``QUERY_FAILED`` carries no broker state and no observation time --
      a failed query observes nothing.
    - ``ALREADY_CONSISTENT`` requires the observed broker state to equal
      the current internal state.
    - a ``BROKER_ORDER`` target (an order BELIAL has no execution request
      for) can only ever be ``NOT_ELIGIBLE`` and carries no internal
      state: adopting a broker-side order is never automatic.
    """

    target_type: ReconciliationTargetType
    target_id: str
    outcome: RepairOutcome
    reason: str
    state_before: OrderState | None = None
    state_after: OrderState | None = None
    broker_state: OrderState | None = None
    client_order_id: str | None = None
    broker_order_id: str | None = None
    observed_at_utc: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target_type, ReconciliationTargetType):
            raise ValueError(
                f"target_type must be a ReconciliationTargetType, got {self.target_type!r}"
            )
        if not isinstance(self.outcome, RepairOutcome):
            raise ValueError(f"outcome must be a RepairOutcome, got {self.outcome!r}")
        _require_non_empty_str(self.target_id, "target_id")
        _require_non_empty_str(self.reason, "reason")
        for name in ("state_before", "state_after", "broker_state"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, OrderState):
                raise ValueError(f"{name} must be an OrderState or None, got {value!r}")
        for name in ("client_order_id", "broker_order_id"):
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_str(value, name)
        if self.observed_at_utc is not None:
            _require_utc(self.observed_at_utc, "observed_at_utc")

        if self.target_type is ReconciliationTargetType.BROKER_ORDER:
            if self.outcome is not RepairOutcome.NOT_ELIGIBLE:
                raise ValueError(
                    "a broker-only order is never adopted: the only outcome for a "
                    f"broker_order target is 'not_eligible', got {self.outcome.value!r}"
                )
            if self.state_before is not None or self.state_after is not None:
                raise ValueError(
                    "a broker_order target has no internal order, so state_before / "
                    "state_after must be None"
                )

        if self.outcome in (RepairOutcome.REPAIRED, RepairOutcome.WOULD_REPAIR):
            for name in ("state_before", "state_after", "broker_state", "observed_at_utc",
                         "client_order_id"):
                if getattr(self, name) is None:
                    raise ValueError(f"{self.outcome.value!r} requires {name}")
            if self.state_after is not self.broker_state:
                raise ValueError(
                    f"a repair reflects the observed broker state: state_after "
                    f"{self.state_after.value!r} != broker_state {self.broker_state.value!r}"
                )
            if self.state_after not in _REFLECTABLE_BROKER_STATES:
                raise ValueError(
                    f"only a broker-confirmed state can be reflected, got "
                    f"{self.state_after.value!r}"
                )
            if self.state_before not in _REPAIRABLE_INTERNAL_STATES:
                raise ValueError(
                    f"only an unsettled internal state may be repaired, got "
                    f"{self.state_before.value!r}"
                )
            if self.state_before is self.state_after:
                raise ValueError(
                    "equal states are 'already_consistent', never a repair"
                )
        else:
            if self.state_after is not self.state_before:
                raise ValueError(
                    f"outcome {self.outcome.value!r} performs no state change: "
                    f"state_after must equal state_before, got "
                    f"{self.state_before!r} -> {self.state_after!r}"
                )
            if self.outcome is RepairOutcome.QUERY_FAILED and (
                self.broker_state is not None or self.observed_at_utc is not None
            ):
                raise ValueError(
                    "a failed query observes nothing: query_failed must not carry a "
                    "broker_state or an observed_at_utc"
                )
            if self.outcome is RepairOutcome.ALREADY_CONSISTENT and (
                self.state_before is None or self.broker_state is not self.state_before
            ):
                raise ValueError(
                    "already_consistent means the internal state equals the observed "
                    "broker state"
                )


@dataclass(frozen=True)
class OrderRepairRun:
    """The result of one repair pass over a reconciliation run (Issue #104).

    ``source_run_id`` names the detection run the actions were derived
    from. ``mode`` is what this pass was ALLOWED to do; the actions are
    validated against it (a ``dry_run`` pass can never contain a
    ``REPAIRED`` action, an ``apply`` pass never a ``WOULD_REPAIR`` one).
    One action exists per detection discrepancy, so skipped targets stay
    visible.
    """

    source_run_id: str
    mode: RepairMode
    started_at_utc: datetime
    finished_at_utc: datetime
    actions: tuple[OrderRepairAction, ...]

    def __post_init__(self) -> None:
        _require_non_empty_str(self.source_run_id, "source_run_id")
        if not isinstance(self.mode, RepairMode):
            raise ValueError(f"mode must be a RepairMode, got {self.mode!r}")
        _require_utc(self.started_at_utc, "started_at_utc")
        _require_utc(self.finished_at_utc, "finished_at_utc")
        if self.finished_at_utc < self.started_at_utc:
            raise ValueError(
                f"finished_at_utc must not precede started_at_utc: "
                f"{self.started_at_utc.isoformat()} -> {self.finished_at_utc.isoformat()}"
            )
        if not isinstance(self.actions, tuple):
            raise ValueError(f"actions must be a tuple, got {type(self.actions).__name__}")
        for entry in self.actions:
            if not isinstance(entry, OrderRepairAction):
                raise ValueError(
                    f"every actions entry must be an OrderRepairAction, got {entry!r}"
                )
        targets = [(entry.target_type, entry.target_id) for entry in self.actions]
        if len(set(targets)) != len(targets):
            raise ValueError(
                "a repair run holds at most one action per (target_type, target_id)"
            )
        forbidden = (
            RepairOutcome.REPAIRED if self.mode is RepairMode.DRY_RUN
            else RepairOutcome.WOULD_REPAIR
        )
        if any(entry.outcome is forbidden for entry in self.actions):
            raise ValueError(
                f"a {self.mode.value!r} repair run must not contain "
                f"{forbidden.value!r} actions"
            )

    def by_outcome(self, outcome: RepairOutcome) -> tuple[OrderRepairAction, ...]:
        """The actions with one outcome, in input (observation) order."""
        if not isinstance(outcome, RepairOutcome):
            raise ValueError(f"outcome must be a RepairOutcome, got {outcome!r}")
        return tuple(entry for entry in self.actions if entry.outcome is outcome)

    def outcome_counts(self) -> dict[RepairOutcome, int]:
        """Action count per outcome (every member present)."""
        counts = {outcome: 0 for outcome in RepairOutcome}
        for entry in self.actions:
            counts[entry.outcome] += 1
        return counts


class BrokerOrderRepairService:
    """Apply the SAFE portion of a reconciliation run to internal order state.

    ``execution_db_path`` is the execution ORDER store; :class:`RepairMode`
    decides whether it is only read (``DRY_RUN``) or also written through
    ``execution_store.record_order_result`` (``APPLY``). ``clock`` supplies
    the repair-time observation timestamps (injected in tests; defaults to
    ``datetime.now(timezone.utc)``) and must return timezone-aware UTC
    datetimes. The broker is used through ``get_order`` exclusively.
    """

    def __init__(
        self,
        *,
        broker: Broker,
        execution_db_path: Path | str,
        clock: Callable[[], datetime] | None = None,
    ):
        if not isinstance(broker, Broker):
            raise ValueError(
                f"broker must implement the Broker protocol (Issue #73), got {broker!r}"
            )
        if clock is not None and not callable(clock):
            raise ValueError(f"clock must be a callable returning a datetime, got {clock!r}")
        self._broker = broker
        self._execution_db_path = Path(execution_db_path)
        self._clock = clock

    # --- public API ---------------------------------------------------------

    def repair(self, run: ReconciliationRun, *, mode: RepairMode) -> OrderRepairRun:
        """One :class:`OrderRepairAction` per discrepancy of ``run``.

        ``mode`` is mandatory and explicit: ``DRY_RUN`` evaluates without
        any write; ``APPLY`` records eligible observations through the
        order state machine. Either way the result is returned and logged
        -- never persisted by this service.
        """
        if not isinstance(run, ReconciliationRun):
            raise ValueError(f"run must be a ReconciliationRun, got {run!r}")
        if not isinstance(mode, RepairMode):
            raise ValueError(f"mode must be a RepairMode, got {mode!r}")
        started_at = self._now()
        actions = tuple(
            self._repair_one(discrepancy, mode=mode, source_run_id=run.run_id)
            for discrepancy in run.discrepancies
        )
        result = OrderRepairRun(
            source_run_id=run.run_id,
            mode=mode,
            started_at_utc=started_at,
            finished_at_utc=self._now(),
            actions=actions,
        )
        self._log_run(result)
        return result

    # --- one target ---------------------------------------------------------

    def _repair_one(
        self, discrepancy: OrderDiscrepancy, *, mode: RepairMode, source_run_id: str
    ) -> OrderRepairAction:
        if discrepancy.target_type is ReconciliationTargetType.BROKER_ORDER:
            return self._action(
                discrepancy,
                outcome=RepairOutcome.NOT_ELIGIBLE,
                reason=(
                    "the broker holds this order but BELIAL has no execution request "
                    "for it; adopting a broker-side order is never automatic"
                ),
                broker_order_id=discrepancy.broker_order_id,
            )
        internal_at_detection = discrepancy.internal_state
        if discrepancy.resolution_category is not ResolutionCategory.AUTO_REPAIR_CANDIDATE:
            return self._action(
                discrepancy,
                outcome=RepairOutcome.NOT_ELIGIBLE,
                reason=(
                    f"detection classified this discrepancy "
                    f"{discrepancy.resolution_category.value!r} "
                    f"({discrepancy.discrepancy_type.value!r}); only auto-repair "
                    "candidates are ever repaired automatically"
                ),
                state_before=internal_at_detection,
                state_after=internal_at_detection,
                broker_order_id=discrepancy.broker_order_id,
            )

        # Fresh repair-time observation: a repair never trusts a stored
        # state to still hold; it reflects what the broker answers NOW.
        client_order_id = discrepancy.client_order_id
        if client_order_id is None:
            return self._action(
                discrepancy,
                outcome=RepairOutcome.REFUSED,
                reason=(
                    "the discrepancy carries no client_order_id, so the broker cannot "
                    "be re-queried; nothing is repaired"
                ),
                state_before=internal_at_detection,
                state_after=internal_at_detection,
            )
        observed_at = self._now()
        try:
            observed = self._broker.get_order(client_order_id)
            if not isinstance(observed, OrderResult):
                raise TypeError(
                    "Broker.get_order() must return an OrderResult, got "
                    f"{type(observed).__name__}"
                )
        except BrokerOrderNotFoundError as exc:
            return self._action(
                discrepancy,
                outcome=RepairOutcome.BROKER_STATE_UNCONFIRMED,
                reason=(
                    f"the broker no longer answers about this client_order_id "
                    f"({type(exc).__name__}); absence proves nothing and is never "
                    "adopted, so the internal state stays untouched"
                ),
                state_before=internal_at_detection,
                state_after=internal_at_detection,
                observed_at=observed_at,
            )
        except BrokerError as exc:
            return self._action(
                discrepancy,
                outcome=RepairOutcome.QUERY_FAILED,
                reason=(
                    f"the repair-time broker query failed ({type(exc).__name__}: {exc}); "
                    "a failed query never settles an order, so the internal state stays "
                    "untouched"
                ),
                state_before=internal_at_detection,
                state_after=internal_at_detection,
            )
        except Exception as exc:
            # Same fail-closed boundary as detection: an adapter leaking a
            # non-BrokerError (or a malformed return) must neither kill the
            # run nor let a state be inferred.
            return self._action(
                discrepancy,
                outcome=RepairOutcome.QUERY_FAILED,
                reason=(
                    "the repair-time broker query violated the Broker error/return "
                    f"contract ({type(exc).__name__}); recorded fail-closed and the "
                    "internal state stays untouched"
                ),
                state_before=internal_at_detection,
                state_after=internal_at_detection,
            )

        if observed.client_order_id != client_order_id:
            return self._action(
                discrepancy,
                outcome=RepairOutcome.REFUSED,
                reason=(
                    f"the broker answered about client_order_id "
                    f"{observed.client_order_id!r} instead of {client_order_id!r}; a "
                    "foreign snapshot is never reflected"
                ),
                state_before=internal_at_detection,
                state_after=internal_at_detection,
                observed_at=observed_at,
            )
        if observed.state not in _REFLECTABLE_BROKER_STATES:
            return self._action(
                discrepancy,
                outcome=RepairOutcome.BROKER_STATE_UNCONFIRMED,
                reason=(
                    f"the repair-time broker state is {observed.state.value!r}, which "
                    "confirms no settled outcome; an UNKNOWN / unacknowledged answer "
                    "never updates internal state"
                ),
                state_before=internal_at_detection,
                state_after=internal_at_detection,
                broker_state=observed.state,
                broker_order_id=observed.broker_order_id,
                observed_at=observed_at,
            )

        # Current internal state, re-read at repair time (full bundle
        # validation inside the store read).
        try:
            row = execution_store.get_execution_order(
                discrepancy.target_id, db_path=self._execution_db_path
            )
        except ExecutionStoreError as exc:
            return self._action(
                discrepancy,
                outcome=RepairOutcome.REFUSED,
                reason=(
                    f"reading the execution order store failed ({type(exc).__name__}: "
                    f"{exc}); nothing is repaired on an unreadable store (fail-closed)"
                ),
                broker_state=observed.state,
                broker_order_id=observed.broker_order_id,
                observed_at=observed_at,
            )
        if row is None or row["order_state"] is None:
            return self._action(
                discrepancy,
                outcome=RepairOutcome.REFUSED,
                reason=(
                    "the execution order no longer exists (or has no submit attempt) "
                    "in the order store; nothing is repaired"
                ),
                broker_state=observed.state,
                broker_order_id=observed.broker_order_id,
                observed_at=observed_at,
            )
        current_state = OrderState(row["order_state"])

        if current_state is observed.state:
            return self._action(
                discrepancy,
                outcome=RepairOutcome.ALREADY_CONSISTENT,
                reason=(
                    f"the internal state already is {current_state.value!r}; nothing to "
                    "repair and nothing is re-recorded (idempotent replay). A fresh "
                    "detection run compares the full snapshot contract"
                ),
                state_before=current_state,
                state_after=current_state,
                broker_state=observed.state,
                broker_order_id=observed.broker_order_id,
                observed_at=observed_at,
            )
        if current_state is not internal_at_detection:
            return self._action(
                discrepancy,
                outcome=RepairOutcome.INTERNAL_STATE_CHANGED,
                reason=(
                    f"the internal state moved from {internal_at_detection.value!r} "
                    f"(at detection) to {current_state.value!r}; this reconciliation "
                    "result is stale and is not applied -- run a fresh detection"
                ),
                state_before=current_state,
                state_after=current_state,
                broker_state=observed.state,
                broker_order_id=observed.broker_order_id,
                observed_at=observed_at,
            )
        if current_state not in _REPAIRABLE_INTERNAL_STATES:
            return self._action(
                discrepancy,
                outcome=RepairOutcome.REFUSED,
                reason=(
                    f"internal state {current_state.value!r} is not repairable "
                    "automatically (terminal states are never left)"
                ),
                state_before=current_state,
                state_after=current_state,
                broker_state=observed.state,
                broker_order_id=observed.broker_order_id,
                observed_at=observed_at,
            )

        # Re-prove repairability at repair time with the SHARED judgement
        # (state machine transition, stored-intent identity, broker-id
        # chain and DB-wide uniqueness, anchored-snapshot update contract,
        # timestamp monotonicity). Fail-closed on store read failures.
        category, detail = classify_order_repairability(
            row, current_state, observed, execution_db_path=self._execution_db_path
        )
        if category is not ResolutionCategory.AUTO_REPAIR_CANDIDATE:
            return self._action(
                discrepancy,
                outcome=RepairOutcome.REFUSED,
                reason=detail,
                state_before=current_state,
                state_after=current_state,
                broker_state=observed.state,
                broker_order_id=observed.broker_order_id,
                observed_at=observed_at,
            )

        reason = (
            f"broker reconciliation repair (source run {source_run_id!r}): reflecting "
            f"the broker-confirmed state {observed.state.value!r} observed at "
            f"{observed_at.isoformat()} for client_order_id {client_order_id!r} "
            f"(broker_order_id {observed.broker_order_id!r})"
        )
        if mode is RepairMode.DRY_RUN:
            return self._action(
                discrepancy,
                outcome=RepairOutcome.WOULD_REPAIR,
                reason=f"dry run: no update performed; apply mode would record -- {reason}",
                state_before=current_state,
                state_after=observed.state,
                broker_state=observed.state,
                broker_order_id=observed.broker_order_id,
                observed_at=observed_at,
            )

        try:
            # THE state change: the Issue #74 order state machine + store
            # contract, one transaction, rolled back entirely on refusal.
            execution_store.record_order_result(
                discrepancy.target_id,
                observed,
                reason=reason,
                db_path=self._execution_db_path,
            )
        except (ValueError, BrokerInvalidRequestError, ExecutionStoreError) as exc:
            return self._action(
                discrepancy,
                outcome=RepairOutcome.REFUSED,
                reason=(
                    f"the order state machine / store contract refused the update "
                    f"({type(exc).__name__}: {exc}); the transaction rolled back and "
                    "the internal state is unchanged"
                ),
                state_before=current_state,
                state_after=current_state,
                broker_state=observed.state,
                broker_order_id=observed.broker_order_id,
                observed_at=observed_at,
            )
        return self._action(
            discrepancy,
            outcome=RepairOutcome.REPAIRED,
            reason=reason,
            state_before=current_state,
            state_after=observed.state,
            broker_state=observed.state,
            broker_order_id=observed.broker_order_id,
            observed_at=observed_at,
        )

    # --- helpers -------------------------------------------------------------

    def _action(
        self,
        discrepancy: OrderDiscrepancy,
        *,
        outcome: RepairOutcome,
        reason: str,
        state_before: OrderState | None = None,
        state_after: OrderState | None = None,
        broker_state: OrderState | None = None,
        broker_order_id: str | None = None,
        observed_at: datetime | None = None,
    ) -> OrderRepairAction:
        return OrderRepairAction(
            target_type=discrepancy.target_type,
            target_id=discrepancy.target_id,
            outcome=outcome,
            reason=reason,
            state_before=state_before,
            state_after=state_after,
            broker_state=broker_state,
            client_order_id=discrepancy.client_order_id,
            broker_order_id=broker_order_id,
            observed_at_utc=observed_at,
        )

    def _now(self) -> datetime:
        value = datetime.now(timezone.utc) if self._clock is None else self._clock()
        _require_utc(value, "clock()")
        return value

    def _log_run(self, result: OrderRepairRun) -> None:
        """Log the pass: one line per action, then a summary.

        Repairs and no-ops log at INFO; anything that PREVENTED a repair
        (query failure, unconfirmed state, stale input, refusal) logs at
        WARNING. Only order identifiers and states are logged -- no
        credentials, no tokens, no account ids.
        """
        warn_outcomes = {
            RepairOutcome.QUERY_FAILED,
            RepairOutcome.BROKER_STATE_UNCONFIRMED,
            RepairOutcome.INTERNAL_STATE_CHANGED,
            RepairOutcome.REFUSED,
        }
        for action in result.actions:
            log = _LOGGER.warning if action.outcome in warn_outcomes else _LOGGER.info
            log(
                "reconciliation repair: source_run_id=%s mode=%s outcome=%s "
                "target_type=%s target_id=%s client_order_id=%s state_before=%s "
                "state_after=%s broker_state=%s broker_order_id=%s reason=%s",
                result.source_run_id,
                result.mode.value,
                action.outcome.value,
                action.target_type.value,
                action.target_id,
                action.client_order_id,
                None if action.state_before is None else action.state_before.value,
                None if action.state_after is None else action.state_after.value,
                None if action.broker_state is None else action.broker_state.value,
                action.broker_order_id,
                action.reason,
            )
        counts = result.outcome_counts()
        _LOGGER.info(
            "reconciliation repair run over %s (%s) finished: actions=%d repaired=%d "
            "would_repair=%d already_consistent=%d not_eligible=%d query_failed=%d "
            "broker_state_unconfirmed=%d internal_state_changed=%d refused=%d",
            result.source_run_id,
            result.mode.value,
            len(result.actions),
            counts[RepairOutcome.REPAIRED],
            counts[RepairOutcome.WOULD_REPAIR],
            counts[RepairOutcome.ALREADY_CONSISTENT],
            counts[RepairOutcome.NOT_ELIGIBLE],
            counts[RepairOutcome.QUERY_FAILED],
            counts[RepairOutcome.BROKER_STATE_UNCONFIRMED],
            counts[RepairOutcome.INTERNAL_STATE_CHANGED],
            counts[RepairOutcome.REFUSED],
        )
