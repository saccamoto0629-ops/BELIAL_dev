"""Intent reissue use cases: manual reissue and gate correction
(Issue #95 / #116).

*** REGISTRATION ONLY. Nothing here submits anything. A registered reissue
child is a normal phase-``created`` intent that gains submission authority
exclusively through the existing sanctioned path
(``LiveOrderSubmissionService.submit()`` -> Safety Gate -> Submission
Journal -> ``mark_submitting``); this module creates no submission path, no
gate bypass and no broker write. Its only broker-free reads are the
execution store, the Safety Gate decision store and the Submission
Journal; its only writes are the reissue registration
(``create_reissue_execution_order``) and the Issue #116 journal
terminalization (``SubmissionJournal.mark_superseded``). ***

Eligibility (service-layer preconditions on top of the Issue #115 store
contracts, all fail-closed):

- the parent must be the signal chain's TAIL (a parent that already has a
  child is never reissued again -- linear chains);
- caps are derived from PERSISTED rows only (the chain itself): per-kind
  caps plus a total-attempt cap, so restarts and concurrent runners cannot
  reset a counter;
- a CURRENT ``valid`` clearance within the policy freshness window must
  exist (the registration reference the store persists is built from it);
  the final judgement -- validity, account scope match, freshness -- is
  re-run by ``mark_submitting`` at the submission instant;
- ``gate_correction`` additionally requires: the parent's LATEST Safety
  Gate decision is a rejection carrying at least one correctable code AND
  was produced under a policy whose CONTENT equals the currently applied
  policy (``policy_id`` alone never suffices -- the id does not bind the
  content; the comparison uses the decision row's stored
  ``policy_snapshot_json`` against ``SafetyPolicy.to_snapshot()``, and a
  decision without a stored snapshot fails closed); the parent's Journal
  entry is absent or ``not_started``; and the CURRENT clearance's evidence
  payload carries the broker-confirmed ABSENCE of the parent's
  ``client_order_id`` (the positive evidence
  ``ReissueClearanceService`` records when it probes an unsubmitted chain
  tail -- a mere "we never sent it" inference is not accepted);
- the quantity increase a ``parameter``-rejected parent permits is bounded
  by ``ReissuePolicy.max_parameter_quantity_increase_ratio`` (a RATIO cap
  against the parent quantity -- decided in Issue #116); its market-risk
  bound stays the Safety Gate's ``max_order_quantity`` on the child's own
  submit.

Superseded-intent terminalization (Issue #116): once a gate correction
retires a parent, the parent's Submission Journal intent -- provably never
sent (``not_started``) -- must converge to the TERMINAL ``superseded``
outcome instead of lingering as unresolved forever. The registration path
terminalizes best-effort right after the child commits, and
:meth:`ReissueService.terminalize_superseded_intents` is the durable,
idempotent recovery sweep that also catches intents journaled LATE by a
losing ``submit()`` (the race where the parent's submit records its intent
and only then loses ``mark_submitting`` to the supersession). Both paths
refuse to touch any entry that is not ``not_started`` -- terminalization
never applies to an intent the broker might hold and never increases
submission possibility.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from execution import execution_store
from execution.execution_models import ExecutionRequest, _require_non_empty_str
from execution.reissue_evidence import build_gate_decision_reference
from execution.safety_gate import SafetyPolicy, SafetyRejectionCode
from execution.safety_store import SQLiteSafetyDecisionStore
from execution.submission_journal import (
    BROKER_CALL_NOT_STARTED,
    OUTCOME_OPEN,
    OUTCOME_SUPERSEDED,
    SubmissionJournal,
    SubmissionJournalError,
)

_LOGGER = logging.getLogger(__name__)

#: Default freshness window for a reissue clearance (Issue #116): the
#: submission cutoff handed to ``mark_submitting`` is "now - this window".
DEFAULT_CLEARANCE_MAX_AGE = timedelta(minutes=5)

#: The CLOSED set of Safety Gate rejection codes a ``gate_correction`` may
#: correct (Issue #95 / #116). Closure condition: exactly the codes whose
#: violation is resolvable by changing ONLY the child-mutable request
#: fields (quantity / order_type / requested_price / stop_loss_price /
#: take_profit_price -- the field invariants of
#: ``docs/architecture/execution_reissue.md``). ``symbol_not_allowed`` /
#: ``side_not_allowed`` are policy-bound (identity fields are immutable in
#: a reissue) and deliberately excluded; a regression test pins this
#: closure.
CORRECTABLE_GATE_REJECTION_CODES: frozenset[SafetyRejectionCode] = frozenset(
    {
        SafetyRejectionCode.QUANTITY_LIMIT_EXCEEDED,
        SafetyRejectionCode.STOP_LOSS_REQUIRED,
        SafetyRejectionCode.STOP_LOSS_INVALID,
        SafetyRejectionCode.TAKE_PROFIT_INVALID,
    }
)


class ReissueServiceError(RuntimeError):
    """Base class for reissue service errors."""


class ReissueEligibilityError(ReissueServiceError):
    """A reissue registration precondition is not met (fail-closed): wrong
    parent, cap reached, missing/stale/invalid clearance, gate-decision or
    journal precondition violated, or policy identity unverifiable. Nothing
    was registered."""


@dataclass(frozen=True)
class ReissuePolicy:
    """The Issue #116 reissue policy. Caps are judged against PERSISTED
    chain rows only; ``max_auto_reissues_per_signal`` defaults to 0
    (automatic retries are Issue #117 and disabled by default).

    ``max_parameter_quantity_increase_ratio`` bounds the ONE sanctioned
    quantity increase (a ``broker_rejected`` parent with
    ``rejection_category = parameter``): child quantity <= parent quantity
    x ratio. The ratio form was chosen over an absolute cap because the
    increase exists to fix minimum-size / increment rejections, which scale
    with the attempted size; the absolute market-risk bound remains the
    Safety Gate's ``max_order_quantity``."""

    max_manual_reissues_per_signal: int = 2
    max_gate_corrections_per_signal: int = 2
    max_auto_reissues_per_signal: int = 0
    max_total_attempts_per_signal: int = 4
    clearance_max_age: timedelta = DEFAULT_CLEARANCE_MAX_AGE
    max_parameter_quantity_increase_ratio: Decimal = Decimal("2")

    def __post_init__(self) -> None:
        for name in (
            "max_manual_reissues_per_signal",
            "max_gate_corrections_per_signal",
            "max_auto_reissues_per_signal",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be an int >= 0, got {value!r}")
        if (
            isinstance(self.max_total_attempts_per_signal, bool)
            or not isinstance(self.max_total_attempts_per_signal, int)
            or self.max_total_attempts_per_signal < 1
        ):
            raise ValueError(
                f"max_total_attempts_per_signal must be an int >= 1, got "
                f"{self.max_total_attempts_per_signal!r}"
            )
        if (
            not isinstance(self.clearance_max_age, timedelta)
            or self.clearance_max_age <= timedelta(0)
        ):
            raise ValueError(
                f"clearance_max_age must be a positive timedelta, got "
                f"{self.clearance_max_age!r}"
            )
        ratio = self.max_parameter_quantity_increase_ratio
        if not isinstance(ratio, Decimal) or not ratio.is_finite() or ratio < 1:
            raise ValueError(
                f"max_parameter_quantity_increase_ratio must be a finite Decimal >= 1, "
                f"got {ratio!r}"
            )


class ReissueService:
    """The sanctioned registration entry points for manual reissue and gate
    correction (see the module docstring)."""

    def __init__(
        self,
        *,
        execution_db_path: Path | str,
        journal_db_path: Path | str,
        decision_db_path: Path | str,
        current_policy: SafetyPolicy,
        reissue_policy: ReissuePolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(current_policy, SafetyPolicy):
            raise ValueError(f"current_policy must be a SafetyPolicy, got {current_policy!r}")
        if reissue_policy is not None and not isinstance(reissue_policy, ReissuePolicy):
            raise ValueError(
                f"reissue_policy must be a ReissuePolicy or None, got {reissue_policy!r}"
            )
        if clock is not None and not callable(clock):
            raise ValueError(f"clock must be a callable returning a datetime, got {clock!r}")
        self._execution_db_path = Path(execution_db_path)
        self._journal = SubmissionJournal(Path(journal_db_path))
        self._decision_db_path = Path(decision_db_path)
        self._current_policy = current_policy
        self._reissue_policy = reissue_policy or ReissuePolicy()
        self._clock = clock

    @property
    def reissue_policy(self) -> ReissuePolicy:
        return self._reissue_policy

    # --- registration use cases ---------------------------------------------

    def register_manual_reissue(
        self,
        request: ExecutionRequest,
        *,
        client_order_id: str,
        parent_execution_request_id: str,
        operator_id: str,
        reason: str,
    ) -> dict:
        """Register an operator-initiated reissue of a broker-REJECTED
        parent (``reissue_kind = broker_rejected``). Allowed for every
        ``rejection_category`` -- the manual basis is the operator's
        explicit judgement, which is mandatory and recorded (operator id +
        reason). Returns the created child row; every violated precondition
        raises :class:`ReissueEligibilityError` with nothing registered."""
        parent, chain, clearance = self._common_preconditions(
            request,
            parent_execution_request_id=parent_execution_request_id,
            kind=execution_store.REISSUE_KIND_BROKER_REJECTED,
            operator_id=operator_id,
            reason=reason,
        )
        if parent["order_state"] != "rejected":
            raise ReissueEligibilityError(
                f"a manual reissue requires the parent in order_state 'rejected'; "
                f"parent {parent_execution_request_id!r} is in phase "
                f"{parent['submission_phase']!r} / state {parent['order_state']!r}"
            )
        self._check_quantity_cap(request, parent)
        child = execution_store.create_reissue_execution_order(
            request,
            client_order_id=client_order_id,
            created_at_utc=self._now(),
            parent_execution_request_id=parent_execution_request_id,
            reissue_kind=execution_store.REISSUE_KIND_BROKER_REJECTED,
            reissue_initiated_by=execution_store.REISSUE_INITIATED_MANUAL,
            reissue_reason=f"operator {operator_id}: {reason}",
            clearance_generation=clearance["clearance_generation"],
            clearance_evidence_id=clearance["evidence_id"],
            clearance_observed_after_utc=self._clearance_cutoff(),
            db_path=self._execution_db_path,
        )
        _LOGGER.info(
            "manual reissue registered: signal_id=%s parent=%s child=%s attempt=%s "
            "operator=%s clearance_generation=%s",
            request.signal_id,
            parent_execution_request_id,
            request.execution_request_id,
            child["signal_attempt"],
            operator_id,
            clearance["clearance_generation"],
        )
        return child

    def register_gate_correction(
        self,
        request: ExecutionRequest,
        *,
        client_order_id: str,
        parent_execution_request_id: str,
        operator_id: str,
        reason: str,
    ) -> dict:
        """Register an operator-initiated CORRECTION of a never-submitted,
        gate-rejected parent (``reissue_kind = gate_correction``). See the
        module docstring for the full precondition list. Returns the created
        child row; after the child commits, the parent's ``not_started``
        Journal intent is terminalized best-effort (the recovery sweep
        :meth:`terminalize_superseded_intents` heals a failed cleanup)."""
        parent, chain, clearance = self._common_preconditions(
            request,
            parent_execution_request_id=parent_execution_request_id,
            kind=execution_store.REISSUE_KIND_GATE_CORRECTION,
            operator_id=operator_id,
            reason=reason,
        )
        if (
            parent["submission_phase"] != execution_store.PHASE_CREATED
            or parent["order_state"] is not None
        ):
            raise ReissueEligibilityError(
                f"a gate correction requires the parent in phase 'created' with no "
                f"order state (never submitted); parent "
                f"{parent_execution_request_id!r} is in phase "
                f"{parent['submission_phase']!r} / state {parent['order_state']!r}"
            )
        self._require_parent_journal_unsent(parent_execution_request_id)
        self._require_parent_absence_evidence(parent, clearance)
        # The Gate decision is read LAST, immediately before the
        # registration transaction (Issue #95's window narrowing): a
        # concurrent submit() may persist a newer decision at any moment,
        # and Issue #95 deliberately does NOT adopt a strict decision-store
        # fence -- that would require holding this store's write lock
        # across the other DB file while submit() performs broker queries,
        # which the Execution Request contract forbids. The narrowing plus
        # the post-commit re-read below is the sanctioned closure; the
        # SAFETY argument is unchanged: of the decisions that could
        # invalidate this precondition, only an ALLOW can reach a
        # submission, and an allowed submit must win created -> submitting,
        # which serializes against this child INSERT -- so either the
        # parent submits (and the registration fails on the phase check) or
        # the child registers (and the submit is refused with
        # intent_superseded). Never both.
        gate_reference = self._require_correctable_latest_decision(
            parent_execution_request_id
        )
        child = execution_store.create_reissue_execution_order(
            request,
            client_order_id=client_order_id,
            created_at_utc=self._now(),
            parent_execution_request_id=parent_execution_request_id,
            reissue_kind=execution_store.REISSUE_KIND_GATE_CORRECTION,
            reissue_initiated_by=execution_store.REISSUE_INITIATED_MANUAL,
            reissue_reason=f"operator {operator_id}: {reason}",
            clearance_generation=clearance["clearance_generation"],
            clearance_evidence_id=clearance["evidence_id"],
            clearance_observed_after_utc=self._clearance_cutoff(),
            gate_correction_reference=gate_reference,
            gate_decision_db_path=self._decision_db_path,
            db_path=self._execution_db_path,
        )
        self._warn_if_correction_basis_went_stale(
            parent_execution_request_id,
            gate_reference.decision_id,
            request.execution_request_id,
        )
        _LOGGER.info(
            "gate correction registered: signal_id=%s parent=%s child=%s attempt=%s "
            "operator=%s decision_id=%s clearance_generation=%s",
            request.signal_id,
            parent_execution_request_id,
            request.execution_request_id,
            child["signal_attempt"],
            operator_id,
            gate_reference.decision_id,
            clearance["clearance_generation"],
        )
        # Best-effort journal terminalization; a failure here never unwinds
        # the committed registration (the supersession marker keeps the
        # parent unsendable regardless) and the durable sweep recovers it.
        try:
            self._terminalize_intent(
                parent_execution_request_id, request.execution_request_id
            )
        except Exception as exc:
            _LOGGER.warning(
                "superseded-intent terminalization failed for parent %s (child %s): "
                "%s; terminalize_superseded_intents() recovers it",
                parent_execution_request_id,
                request.execution_request_id,
                exc,
            )
        return child

    # --- superseded-intent recovery -----------------------------------------

    def terminalize_superseded_intents(self) -> tuple[dict, ...]:
        """Idempotent recovery sweep (Issue #116): converge every journaled
        intent of a superseded parent to the terminal ``superseded``
        outcome -- including intents journaled LATE by a losing submit.

        Safety: only ``not_started`` + ``open`` entries are touched (the
        journal proves the broker was never called for them; the durable
        supersession marker proves the parent can never gain submission
        authority). Entries in any other state -- possibly sent, completed,
        already superseded -- are reported and left alone. Per-marker
        failures are collected, never raised, so one broken entry cannot
        stall the sweep."""
        actions: list[dict] = []
        markers = execution_store.list_intent_supersessions(
            db_path=self._execution_db_path
        )
        for marker in markers:
            parent_id = marker["parent_execution_request_id"]
            child_id = marker["child_execution_request_id"]
            outcome = {"execution_request_id": parent_id, "superseded_by": child_id}
            try:
                action = self._terminalize_intent(parent_id, child_id)
                outcome["action"] = action
            except Exception as exc:
                outcome["action"] = "failed"
                outcome["detail"] = f"{type(exc).__name__}: {exc}"
                _LOGGER.warning(
                    "superseded-intent sweep failed for parent %s: %s", parent_id, exc
                )
            actions.append(outcome)
        return tuple(actions)

    def _terminalize_intent(self, parent_id: str, child_id: str) -> str:
        """Terminalize ONE superseded parent's journal intent when -- and
        only when -- the journal proves the broker was never called.
        Returns the action taken."""
        entry = self._journal.get_entry(parent_id)
        if entry is None:
            return "no_journal_entry"
        if entry.outcome == OUTCOME_SUPERSEDED:
            return "already_superseded"
        if entry.broker_call_state != BROKER_CALL_NOT_STARTED or entry.outcome != OUTCOME_OPEN:
            # The broker may have been called (started/responded/unknown) or
            # the entry already resolved (completed): terminalization never
            # touches these -- broker reachability wins.
            return "not_terminalizable"
        changed = self._journal.mark_superseded(
            parent_id,
            superseded_by_execution_request_id=child_id,
            at_utc=self._now(),
        )
        return "superseded" if changed else "already_superseded"

    # --- preconditions ------------------------------------------------------

    def _common_preconditions(
        self,
        request: ExecutionRequest,
        *,
        parent_execution_request_id: str,
        kind: str,
        operator_id: str,
        reason: str,
    ) -> tuple[dict, list[dict], dict]:
        if not isinstance(request, ExecutionRequest):
            raise ValueError(f"request must be an ExecutionRequest, got {request!r}")
        _require_non_empty_str(parent_execution_request_id, "parent_execution_request_id")
        _require_non_empty_str(operator_id, "operator_id")
        _require_non_empty_str(reason, "reason")
        chain = execution_store.list_execution_order_chain(
            request.signal_id, db_path=self._execution_db_path
        )
        if not chain:
            raise ReissueEligibilityError(
                f"signal {request.signal_id!r} has no execution order chain; a reissue "
                "extends an existing chain"
            )
        tail = chain[-1]
        if tail["execution_request_id"] != parent_execution_request_id:
            raise ReissueEligibilityError(
                f"parent {parent_execution_request_id!r} is not the chain tail of "
                f"signal {request.signal_id!r} (tail: "
                f"{tail['execution_request_id']!r}, attempt {tail['signal_attempt']}); "
                "only the latest attempt can be reissued"
            )
        self._check_caps(chain, kind)
        clearance = self._require_fresh_valid_clearance(request.signal_id)
        return tail, chain, clearance

    def _check_caps(self, chain: list[dict], kind: str) -> None:
        """Caps derived from PERSISTED rows only (restart/multi-process
        safe): the chain rows themselves are the counters."""
        policy = self._reissue_policy
        if len(chain) + 1 > policy.max_total_attempts_per_signal:
            raise ReissueEligibilityError(
                f"the total attempt cap is reached: the chain already holds "
                f"{len(chain)} attempt(s) and max_total_attempts_per_signal is "
                f"{policy.max_total_attempts_per_signal}"
            )
        manual_reissues = sum(
            1
            for row in chain
            if row["reissue_kind"] == execution_store.REISSUE_KIND_BROKER_REJECTED
            and row["reissue_initiated_by"] == execution_store.REISSUE_INITIATED_MANUAL
        )
        gate_corrections = sum(
            1
            for row in chain
            if row["reissue_kind"] == execution_store.REISSUE_KIND_GATE_CORRECTION
        )
        if (
            kind == execution_store.REISSUE_KIND_BROKER_REJECTED
            and manual_reissues + 1 > policy.max_manual_reissues_per_signal
        ):
            raise ReissueEligibilityError(
                f"the manual reissue cap is reached: {manual_reissues} manual "
                f"reissue(s) are persisted and max_manual_reissues_per_signal is "
                f"{policy.max_manual_reissues_per_signal}"
            )
        if (
            kind == execution_store.REISSUE_KIND_GATE_CORRECTION
            and gate_corrections + 1 > policy.max_gate_corrections_per_signal
        ):
            raise ReissueEligibilityError(
                f"the gate correction cap is reached: {gate_corrections} "
                f"correction(s) are persisted and max_gate_corrections_per_signal is "
                f"{policy.max_gate_corrections_per_signal}"
            )

    def _clearance_cutoff(self) -> datetime:
        """The freshness cutoff the REGISTRATION transaction enforces (the
        same window the pre-check used). Passing it into the store binds
        currency, status and freshness to the child INSERT atomically
        (PR #122 Codex review P2)."""
        return self._now() - self._reissue_policy.clearance_max_age

    def _warn_if_correction_basis_went_stale(
        self, parent_id: str, decision_id: int, child_id: str
    ) -> None:
        """Issue #95's residual detection: re-read the parent's decisions
        after the registration committed and WARN when the referenced
        rejection is no longer the latest.

        This is deliberately NOT a refusal. Per Issue #95 the residual is an
        AUDIT-ONLY skew (the child is re-evaluated by the Gate under the
        current policy on its own submit, and a concurrent ALLOW cannot both
        submit the parent and let this child exist), so turning it into a
        rollback would buy no safety while making a routine race fail. The
        WARN makes the skew machine-detectable, which is what the contract
        asks for. Never raises: the registration is already committed."""
        try:
            records = SQLiteSafetyDecisionStore(
                self._decision_db_path
            ).list_for_request_records(parent_id)
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.warning(
                "could not re-read the Safety Gate decisions of parent %s after "
                "registering correction child %s: %s; the recorded correction basis "
                "(decision_id %s) could not be confirmed as the latest",
                parent_id,
                child_id,
                exc,
                decision_id,
            )
            return
        if records and records[-1][0] != decision_id:
            latest_id, latest_decision, _snapshot = records[-1]
            _LOGGER.warning(
                "the correction basis of child %s (parent %s, decision_id %s) is no "
                "longer the parent's latest Safety Gate decision (now decision_id %s, "
                "allowed=%s, codes=%s): a decision was recorded concurrently with the "
                "registration. The child is still re-evaluated by the Gate under the "
                "current policy on its own submit, and the parent can never submit "
                "(supersession marker), so this is an audit-metadata skew only",
                child_id,
                parent_id,
                decision_id,
                latest_id,
                latest_decision.allowed,
                latest_decision.rejection_codes,
            )

    def _require_fresh_valid_clearance(self, signal_id: str) -> dict:
        clearance = execution_store.get_reissue_clearance(
            signal_id, db_path=self._execution_db_path
        )
        if clearance is None:
            raise ReissueEligibilityError(
                f"signal {signal_id!r} holds no reissue clearance; the absence of "
                "evidence is not the absence of divergence (run a clearance "
                "observation first)"
            )
        if clearance["status"] != execution_store.CLEARANCE_STATUS_VALID:
            raise ReissueEligibilityError(
                f"the current clearance of signal {signal_id!r} (generation "
                f"{clearance['clearance_generation']}) is "
                f"{clearance['status']!r}, not 'valid'"
            )
        observed_at = datetime.fromisoformat(clearance["observed_at_utc"])
        cutoff = self._now() - self._reissue_policy.clearance_max_age
        if observed_at < cutoff:
            raise ReissueEligibilityError(
                f"the current clearance of signal {signal_id!r} was observed at "
                f"{clearance['observed_at_utc']}, older than the freshness window "
                f"{self._reissue_policy.clearance_max_age} (cutoff "
                f"{cutoff.isoformat()})"
            )
        return clearance

    def _check_quantity_cap(self, request: ExecutionRequest, parent: dict) -> None:
        parent_quantity = Decimal(parent["quantity"])
        if request.quantity <= parent_quantity:
            return
        category = execution_store._parent_rejection_category(parent)
        if category is None or category.value != "parameter":
            raise ReissueEligibilityError(
                f"child quantity {request.quantity} exceeds the parent's "
                f"{parent_quantity}; an increase is allowed only for a parent "
                f"rejected with rejection_category 'parameter' (parent category: "
                f"{'none' if category is None else category.value})"
            )
        cap = parent_quantity * self._reissue_policy.max_parameter_quantity_increase_ratio
        if request.quantity > cap:
            raise ReissueEligibilityError(
                f"child quantity {request.quantity} exceeds the policy increase cap "
                f"{cap} (parent {parent_quantity} x ratio "
                f"{self._reissue_policy.max_parameter_quantity_increase_ratio})"
            )

    def _require_correctable_latest_decision(self, parent_id: str):
        """Precondition (a)-(c) of the gate correction: the parent's LATEST
        decision is a rejection with a correctable code, produced under a
        policy content-identical to the currently applied one."""
        decision_store = SQLiteSafetyDecisionStore(self._decision_db_path)
        try:
            records = decision_store.list_for_request_records(parent_id)
        except Exception as exc:
            raise ReissueEligibilityError(
                f"the Safety Gate decisions for parent {parent_id!r} could not be "
                f"read ({type(exc).__name__}: {exc}); an unverifiable correction "
                "basis never justifies a correction"
            ) from exc
        if not records:
            raise ReissueEligibilityError(
                f"parent {parent_id!r} holds no persisted Safety Gate decision; "
                "there is nothing to correct"
            )
        decision_id, decision, policy_snapshot = records[-1]
        if decision.allowed:
            raise ReissueEligibilityError(
                f"the latest Safety Gate decision for parent {parent_id!r} "
                f"(decision_id {decision_id}) ALLOWED the order; a correction "
                "requires a current rejection -- re-submit the parent instead"
            )
        codes = {rejection.code for rejection in decision.rejections}
        if not codes & CORRECTABLE_GATE_REJECTION_CODES:
            raise ReissueEligibilityError(
                f"the latest Safety Gate decision for parent {parent_id!r} "
                f"(decision_id {decision_id}) carries no correctable rejection code "
                f"(codes: {sorted(code.value for code in codes)}); only "
                f"{sorted(code.value for code in CORRECTABLE_GATE_REJECTION_CODES)} "
                "are correctable by changing the mutable request fields"
            )
        if decision.policy_id != self._current_policy.policy_id:
            raise ReissueEligibilityError(
                f"the latest rejection of parent {parent_id!r} was judged under "
                f"policy id {decision.policy_id!r}, not the currently applied "
                f"{self._current_policy.policy_id!r}; the correction basis must be "
                "current"
            )
        if policy_snapshot is None:
            raise ReissueEligibilityError(
                f"the latest rejection of parent {parent_id!r} (decision_id "
                f"{decision_id}) carries no policy content snapshot; policy identity "
                "cannot be verified by id alone (the content behind an id can "
                "change), so the correction is refused fail-closed"
            )
        if policy_snapshot != self._current_policy.to_snapshot():
            raise ReissueEligibilityError(
                f"the latest rejection of parent {parent_id!r} was judged under a "
                f"policy whose CONTENT differs from the currently applied policy "
                f"(same id {decision.policy_id!r} or not); the recorded rejection "
                "does not prove a correction is needed under the current policy"
            )
        return build_gate_decision_reference(decision_id, decision)

    def _require_parent_journal_unsent(self, parent_id: str) -> None:
        """The Journal precondition: the parent's entry is absent or
        ``not_started``. Necessary but NOT sufficient (the broker absence
        evidence is required on top); a journal that cannot be read is
        fail-closed."""
        try:
            entry = self._journal.get_entry(parent_id)
        except SubmissionJournalError as exc:
            raise ReissueEligibilityError(
                f"the Submission Journal entry for parent {parent_id!r} could not be "
                f"read ({exc}); an unverifiable journal precondition refuses the "
                "correction"
            ) from exc
        if entry is not None and entry.broker_call_state != BROKER_CALL_NOT_STARTED:
            raise ReissueEligibilityError(
                f"the Submission Journal records broker_call_state "
                f"{entry.broker_call_state!r} for parent {parent_id!r}; the broker "
                "may hold this order, so a correction (which retires the parent) is "
                "refused"
            )

    def _require_parent_absence_evidence(self, parent: dict, clearance: dict) -> None:
        """The positive broker-absence evidence for the parent's client key
        must be part of the CURRENT clearance's payload (recorded by the
        clearance observation when it probed the unsubmitted chain tail)."""
        parent_id = parent["execution_request_id"]
        try:
            payload = json.loads(clearance["evidence_json"])
            absence = payload.get("parent_absence")
        except (ValueError, TypeError) as exc:
            raise ReissueEligibilityError(
                f"the current clearance evidence for parent {parent_id!r} is "
                f"unreadable ({exc}); positive broker absence cannot be verified"
            ) from exc
        if (
            not isinstance(absence, dict)
            or absence.get("confirmed_absent") is not True
            or absence.get("client_order_id") != parent["client_order_id"]
            or absence.get("execution_request_id") != parent_id
        ):
            raise ReissueEligibilityError(
                f"the current clearance for signal {parent['signal_id']!r} carries no "
                f"broker-confirmed absence of the parent's client_order_id "
                f"{parent['client_order_id']!r}; 'we never sent it' is not positive "
                "evidence -- a clearance observation must probe the broker "
                "explicitly"
            )

    def _now(self) -> datetime:
        value = datetime.now(timezone.utc) if self._clock is None else self._clock()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
            or value.utcoffset().total_seconds() != 0
        ):
            raise ValueError(f"clock must return a timezone-aware UTC datetime, got {value!r}")
        return value
