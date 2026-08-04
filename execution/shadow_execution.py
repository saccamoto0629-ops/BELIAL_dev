"""ShadowExecutionService -- THE official Production boundary for Shadow Mode
(Issue #78; claim-first / provenance rework PR #100).

*** NOTHING HERE TOUCHES A BROKER. The service does not accept, hold or
construct any ``Broker`` / ``BrokerConnection``: no constructor or method
parameter for a broker exists, so no broker API (submit / cancel / order
query / position query / account query) is reachable from the shadow path --
the "zero broker calls" contract is structural, not a runtime check. ***

Cycle order (claim BEFORE evaluation -- PR #100 review):

    ExecutionRequest (from the production signal path or a labelled fixture)
      -> shadow_store.claim_shadow_cycle()      ... DB-owned identity claim
           ├─ duplicate -> return the stored record; the gate is NOT
           │              evaluated and NO Safety Decision row is written
           ├─ conflict  -> ShadowIdentityConflictError (payload differs);
           │              nothing evaluated, nothing written
           └─ acquired:
                -> SafetyGate.evaluate_shadow()  ... same shared checks as live
                -> durable SafetyDecision record + READ-BACK verification
                -> shadow_store.complete_shadow_cycle()  phase -> completed

Because losers never evaluate, ONE completed shadow record corresponds to ONE
Safety Decision row (the referenced ``safety_decision_id``) in the normal
case. The exceptions are explicit and auditable: decision persistence failure
completes the record with ``safety_decision_id=None`` plus the
``decision_persist_failed`` rejection; a crash AFTER the decision was
recorded but BEFORE completion leaves a reclaimable claim whose retry writes
a fresh decision -- the earlier row becomes an ORPHAN, searchable via
``shadow_report --orphan-decisions`` and bounded by the number of retries.

Claim recovery contract (see ``shadow_store`` for the mechanics): the same
owner re-acquires its claim immediately; other owners take over only after
the lease expires; every crash point (before evaluation, after decision,
before completion) therefore converges back to a completed record on retry,
and no claim can stay unprocessable forever.

Mode dispatch (Issue #78): ``shadow`` runs through THIS service; ``fake``
submission stays exclusively ``LiveOrderSubmissionService``; ``demo`` /
``live`` remain unconstructible (``BROKER_TYPE_BY_MODE``) and must be refused
explicitly by any runner; ``dry-run`` uses :meth:`dry_run_cycle`, which
persists NOTHING and probes existing stores strictly read-only.

Idempotency namespaces: shadow writes only the shadow store; the execution
order store is never touched, so a shadow run can never make a later fake /
demo / live submission of the same signal look "already submitted" -- and a
live registration never blocks shadow.
"""

from __future__ import annotations

import enum
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from execution import shadow_store
from execution.execution_models import ExecutionRequest
from execution.safety_gate import (
    AccountSafetySnapshot,
    MarketSafetySnapshot,
    SafetyDecision,
    SafetyGate,
    SafetyRejection,
    SafetyRejectionCode,
)
from execution.safety_store import SQLiteSafetyDecisionStore
from execution.shadow_models import (
    ShadowClaim,
    ShadowProvenance,
    ShadowResult,
    intended_order_from_request,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_SHADOW_CONNECTION_TARGET = "shadow"
DEFAULT_CLAIM_LEASE_SECONDS = 600


class ShadowCycleStatus(enum.Enum):
    """How one shadow cycle ended (never a broker outcome)."""

    RECORDED = "recorded"
    DUPLICATE = "duplicate"
    DRY_RUN = "dry-run"


@dataclass(frozen=True)
class ShadowCycleOutcome:
    """One shadow cycle's outcome.

    - ``RECORDED``: THIS call completed the record (``result`` set).
    - ``DUPLICATE``: a payload-equal record already exists -- ``result`` when
      it is completed, ``claim`` when another holder is still in flight.
      Nothing was evaluated and no decision row was written by this call.
    - ``DRY_RUN``: evaluated but never persisted (``result`` with all ids
      None).
    """

    status: ShadowCycleStatus
    result: ShadowResult | None = None
    claim: ShadowClaim | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ShadowCycleStatus):
            raise ValueError(f"status must be a ShadowCycleStatus, got {self.status!r}")
        if self.result is not None and not isinstance(self.result, ShadowResult):
            raise ValueError(f"result must be a ShadowResult or None, got {self.result!r}")
        if self.claim is not None and not isinstance(self.claim, ShadowClaim):
            raise ValueError(f"claim must be a ShadowClaim or None, got {self.claim!r}")
        if self.status is ShadowCycleStatus.RECORDED:
            if self.result is None or self.result.shadow_result_id is None or self.claim is not None:
                raise ValueError("a recorded outcome carries exactly the persisted result")
        elif self.status is ShadowCycleStatus.DUPLICATE:
            if (self.result is None) == (self.claim is None):
                raise ValueError("a duplicate outcome carries exactly one of result / claim")
        else:  # DRY_RUN
            if self.result is None or self.claim is not None:
                raise ValueError("a dry-run outcome carries exactly the evaluated result")
            if self.result.shadow_result_id is not None or self.result.safety_decision_id is not None:
                raise ValueError("a dry-run outcome must not carry persisted ids")


class ShadowExecutionService:
    """The single service boundary from ``ExecutionRequest`` to a persisted
    shadow verification record (see the module docstring)."""

    def __init__(
        self,
        *,
        gate: SafetyGate,
        decision_db_path: str | Path,
        shadow_db_path: str | Path,
        clock: Callable[[], datetime],
        connection_target: str = DEFAULT_SHADOW_CONNECTION_TARGET,
        owner_id: str | None = None,
        claim_lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
    ) -> None:
        if not isinstance(gate, SafetyGate):
            raise ValueError(f"gate must be a SafetyGate, got {gate!r}")
        if not isinstance(decision_db_path, (str, Path)):
            # Same sealing as LiveOrderSubmissionService (Issue #75 review
            # r3): only a filesystem path can be injected, never a recorder
            # object whose record()/read-back could forge persistence.
            raise ValueError(
                "decision_db_path must be a filesystem path (the durable audit store "
                f"is sealed inside the service), got {type(decision_db_path).__name__}"
            )
        if not isinstance(shadow_db_path, (str, Path)):
            raise ValueError(
                f"shadow_db_path must be a filesystem path, got {type(shadow_db_path).__name__}"
            )
        if not callable(clock):
            raise ValueError(f"clock must be a callable returning a UTC datetime, got {clock!r}")
        if not isinstance(connection_target, str) or not connection_target.strip():
            raise ValueError(
                f"connection_target must be a non-empty string, got {connection_target!r}"
            )
        if owner_id is not None and (not isinstance(owner_id, str) or not owner_id.strip()):
            raise ValueError(f"owner_id must be a non-empty string or None, got {owner_id!r}")
        if (
            not isinstance(claim_lease_seconds, int)
            or isinstance(claim_lease_seconds, bool)
            or claim_lease_seconds < 1
        ):
            raise ValueError(
                f"claim_lease_seconds must be a positive int, got {claim_lease_seconds!r}"
            )
        self._gate = gate
        self._decision_store = SQLiteSafetyDecisionStore(Path(decision_db_path))
        self._shadow_db_path = Path(shadow_db_path)
        self._clock = clock
        self._connection_target = connection_target
        # The claim owner identity (diagnostics; unique per process+service
        # instance by default, tests inject fixed ids). Ownership itself is
        # per ATTEMPT: every run_cycle() call claims under its own unique
        # attempt id, so owner equality never grants claim re-acquisition
        # (PR #100 re-review Major 1 -- a shared service's threads race the
        # DB claim exactly like separate processes do).
        self._owner_id = owner_id if owner_id is not None else f"pid{os.getpid()}-svc{id(self):x}"
        self._claim_lease_seconds = claim_lease_seconds
        self._attempt_lock = threading.Lock()
        self._attempt_counter = 0

    @property
    def shadow_db_path(self) -> Path:
        return self._shadow_db_path

    @property
    def decision_db_path(self) -> Path:
        return self._decision_store.path

    @property
    def owner_id(self) -> str:
        return self._owner_id

    # --- store lifecycle -----------------------------------------------------

    def initialize_stores(self) -> None:
        """Idempotently initialize (or verify) BOTH durable stores for shadow
        mode. NEVER called on the dry-run path -- dry-run must not create or
        migrate anything."""
        shadow_store.init_db(self._shadow_db_path)
        self._decision_store.initialize()

    # --- shadow cycle --------------------------------------------------------

    def run_cycle(
        self,
        request: ExecutionRequest,
        *,
        market: MarketSafetySnapshot,
        account: AccountSafetySnapshot,
        client_order_id: str,
        provenance: ShadowProvenance,
        timeframe: str | None = None,
    ) -> ShadowCycleOutcome:
        """Claim, evaluate, persist -- in that order (module docstring).

        Returns RECORDED or an explicit DUPLICATE; a payload mismatch raises
        ``ShadowIdentityConflictError`` and store failures raise
        ``ShadowStoreError`` (the claim, if held, stays reclaimable)."""
        self._validate_cycle_args(request, market, account, client_order_id, provenance, timeframe)

        status, record = shadow_store.claim_shadow_cycle(
            request,
            client_order_id=client_order_id,
            timeframe=timeframe,
            provenance=provenance,
            owner_id=self._owner_id,
            attempt_id=self._next_attempt_id(),
            now=self._now(),
            lease_seconds=self._claim_lease_seconds,
            db_path=self._shadow_db_path,
        )
        if status == shadow_store.CLAIM_DUPLICATE:
            if isinstance(record, ShadowResult):
                self._log_duplicate(request, "a completed shadow record already exists")
                return ShadowCycleOutcome(status=ShadowCycleStatus.DUPLICATE, result=record)
            self._log_duplicate(
                request,
                f"claim is held by {record.claim_owner_id!r} (attempt "
                f"{record.claim_attempt_id!r}) until "
                f"{record.lease_expires_at_utc.isoformat()}",
            )
            return ShadowCycleOutcome(status=ShadowCycleStatus.DUPLICATE, claim=record)

        # --- claim acquired: this caller alone evaluates ---------------------
        decision = self._gate.evaluate_shadow(
            request,
            market=market,
            account=account,
            connection_target=self._connection_target,
        )
        decision, decision_id = self._recorded(decision)
        if not decision.allowed:
            _LOGGER.warning(
                "Safety Gate rejected shadow execution_request_id=%s policy_id=%s codes=%s",
                decision.execution_request_id,
                decision.policy_id,
                decision.rejection_codes,
            )

        result = shadow_store.complete_shadow_cycle(
            request.execution_request_id,
            claim=record,
            decision=decision,
            market=market,
            account=account,
            policy_snapshot=self._gate.policy.to_snapshot(),
            safety_decision_id=decision_id,
            completed_at_utc=self._now(),
            db_path=self._shadow_db_path,
        )
        _LOGGER.info(
            "shadow result recorded execution_request_id=%s signal_id=%s allowed=%s "
            "shadow_result_id=%s safety_decision_id=%s policy_id=%s input_origin=%s",
            request.execution_request_id,
            request.signal_id,
            decision.allowed,
            result.shadow_result_id,
            decision_id,
            decision.policy_id,
            provenance.input_origin,
        )
        return ShadowCycleOutcome(status=ShadowCycleStatus.RECORDED, result=result)

    def dry_run_cycle(
        self,
        request: ExecutionRequest,
        *,
        market: MarketSafetySnapshot,
        account: AccountSafetySnapshot,
        client_order_id: str,
        provenance: ShadowProvenance,
        timeframe: str | None = None,
    ) -> ShadowCycleOutcome:
        """The dry-run mode cycle: the same production evaluation with ZERO
        side effects -- no DB creation, no migration, no INSERT/UPDATE, no
        claim. Existing store files are probed strictly READ-ONLY first (an
        invalid schema is an explicit failure, never success; probing changes
        neither content nor mtime); a missing file stays missing."""
        self._validate_cycle_args(request, market, account, client_order_id, provenance, timeframe)

        if self._shadow_db_path.exists():
            shadow_store.verify_db(self._shadow_db_path)  # true read-only probe
        if self._decision_store.path.exists():
            # Read-only probe: list_for_request verifies the schema over a
            # mode=ro connection without creating or writing anything.
            self._decision_store.list_for_request(request.execution_request_id)

        now = self._now()
        decision = self._gate.evaluate_shadow(
            request,
            market=market,
            account=account,
            connection_target=self._connection_target,
        )
        result = ShadowResult(
            request=request,
            client_order_id=client_order_id,
            timeframe=timeframe,
            provenance=provenance,
            decision=decision,
            intended_order=intended_order_from_request(request, client_order_id=client_order_id),
            market=market,
            account=account,
            policy_snapshot=self._gate.policy.to_snapshot(),
            safety_decision_id=None,
            claim_owner_id=self._owner_id,
            claimed_at_utc=now,
            completed_at_utc=now,
        )
        return ShadowCycleOutcome(status=ShadowCycleStatus.DRY_RUN, result=result)

    # --- internals -----------------------------------------------------------

    @staticmethod
    def _validate_cycle_args(request, market, account, client_order_id, provenance, timeframe) -> None:
        if not isinstance(request, ExecutionRequest):
            raise ValueError(f"request must be an ExecutionRequest, got {request!r}")
        if not isinstance(market, MarketSafetySnapshot):
            raise ValueError(f"market must be a MarketSafetySnapshot, got {market!r}")
        if not isinstance(account, AccountSafetySnapshot):
            raise ValueError(f"account must be an AccountSafetySnapshot, got {account!r}")
        if not isinstance(client_order_id, str) or not client_order_id.strip():
            raise ValueError(f"client_order_id must be a non-empty string, got {client_order_id!r}")
        if not isinstance(provenance, ShadowProvenance):
            raise ValueError(f"provenance must be a ShadowProvenance, got {provenance!r}")
        if timeframe is not None and (not isinstance(timeframe, str) or not timeframe.strip()):
            raise ValueError(f"timeframe must be a non-empty string or None, got {timeframe!r}")

    def _next_attempt_id(self) -> str:
        """A unique id per ``run_cycle()`` call: claim ownership and the
        completion fencing check are keyed on THIS, never on the service."""
        with self._attempt_lock:
            self._attempt_counter += 1
            return f"{self._owner_id}-attempt-{self._attempt_counter}"

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

    def _log_duplicate(self, request: ExecutionRequest, reason: str) -> None:
        # Issue #76-style suppression log contract, shadow namespace: the
        # ids and the suppression reason stay visible.
        _LOGGER.warning(
            "duplicate shadow cycle suppressed: execution_request_id=%s signal_id=%s reason=%s",
            request.execution_request_id,
            request.signal_id,
            reason,
        )

    def _recorded(self, decision: SafetyDecision) -> tuple[SafetyDecision, int | None]:
        """Durably record the decision and VERIFY it by read-back (same
        contract as live submission, Issue #75 review r1). Any failure --
        record exception, read-back miss, read-back mismatch -- appends
        ``decision_persist_failed`` (an allow becomes a rejection) and the
        shadow record is completed WITHOUT a decision reference
        (``safety_decision_id=None``): no dangling reference, explicitly
        recoverable state (Issue #78)."""
        try:
            decision_id = self._decision_store.record(decision)
            stored = self._decision_store.list_for_request(decision.execution_request_id)
            if not stored or stored[-1] != decision:
                raise RuntimeError("decision read-back did not return the recorded decision")
        except Exception as exc:
            if SafetyRejectionCode.DECISION_PERSIST_FAILED in {
                entry.code for entry in decision.rejections
            }:
                return decision, None
            return (
                SafetyDecision(
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
                ),
                None,
            )
        return decision, decision_id
