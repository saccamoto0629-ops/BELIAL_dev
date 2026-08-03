"""ShadowExecutionService -- THE official Production boundary for Shadow Mode
(Issue #78).

*** NOTHING HERE TOUCHES A BROKER. The service does not accept, hold or
construct any ``Broker`` / ``BrokerConnection``: no constructor or method
parameter for a broker exists, so no broker API (submit / cancel / order
query / position query / account query) is reachable from the shadow path --
the "zero broker calls" contract is structural, not a runtime check. ***

Shadow mode runs the REAL decision path -- an ``ExecutionRequest`` built from
a real signal, the configured ``SafetyPolicy``, the full Safety Gate -- and
persists the outcome as an audit record instead of submitting:

    ExecutionRequest (real signal; signal-vocabulary mapping is the caller's
                      responsibility, exactly as for live submission)
      -> duplicate pre-check against the shadow store (explicit DUPLICATE)
      -> SafetyGate.evaluate_shadow()   ... same shared checks as live
      -> durable SafetyDecision record + READ-BACK verification
         (same sealed audit store as live submission; failure appends
          ``decision_persist_failed`` and turns an allow into a rejection)
      -> ShadowResult persisted in ONE atomic INSERT (UNIQUE-claimed)

Mode dispatch (Issue #78): ``shadow`` runs through THIS service; ``fake``
submission stays exclusively ``LiveOrderSubmissionService`` (which cannot be
built without a ``BrokerConnection``); ``demo`` / ``live`` remain
unconstructible until an adapter type is registered (``BROKER_TYPE_BY_MODE``)
and must be refused explicitly by any runner; ``dry-run`` uses
:meth:`ShadowExecutionService.dry_run_cycle`, which persists NOTHING.

Idempotency contract (Issue #78):

- NAMESPACE SEPARATION: shadow writes only the shadow store
  (``shadow_results.sqlite3``). The execution order store
  (``execution_orders.sqlite3``) is never touched, so a shadow run can never
  make a later fake / demo / live submission of the same signal look
  "already submitted" -- and a live registration never blocks shadow.
- ONE RECORD per execution request AND per source signal AND per client
  order id: the shadow store's UNIQUE constraints decide the winner inside
  ``BEGIN IMMEDIATE``; a losing writer (sequential re-run or concurrent
  race) gets an explicit ``DUPLICATE`` outcome carrying the STORED result --
  never a second row, and never a lock/persistence error dressed up as
  duplicate suppression.
- Re-evaluation of the same request under a changed policy/configuration is
  deliberately NOT modelled (out of scope): the first recorded evaluation
  wins; the applied policy snapshot is stored with it.

Dry-run vs shadow (both never call a broker; only shadow persists):
``dry_run_cycle()`` evaluates the same production checks, creates NO file,
performs NO insert/update and NO store initialization; existing DB files are
probed strictly READ-ONLY (an invalid schema is an explicit error, never
"success"), missing files are left missing. Decision persistence and shadow
persistence are both skipped, so the returned outcome carries
``safety_decision_id=None`` and ``shadow_result_id=None``.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, replace
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
from execution.shadow_models import ShadowResult, intended_order_from_request

_LOGGER = logging.getLogger(__name__)

DEFAULT_SHADOW_CONNECTION_TARGET = "shadow"


class ShadowCycleStatus(enum.Enum):
    """How one shadow cycle ended (never a broker outcome)."""

    RECORDED = "recorded"
    DUPLICATE = "duplicate"
    DRY_RUN = "dry-run"


@dataclass(frozen=True)
class ShadowCycleOutcome:
    """One shadow cycle's outcome: ``result`` is the freshly RECORDED record,
    the pre-existing STORED record (``DUPLICATE``), or the evaluated-but-not-
    persisted record (``DRY_RUN``, ids all None)."""

    status: ShadowCycleStatus
    result: ShadowResult

    def __post_init__(self) -> None:
        if not isinstance(self.status, ShadowCycleStatus):
            raise ValueError(f"status must be a ShadowCycleStatus, got {self.status!r}")
        if not isinstance(self.result, ShadowResult):
            raise ValueError(f"result must be a ShadowResult, got {self.result!r}")
        if self.status is ShadowCycleStatus.DRY_RUN and (
            self.result.shadow_result_id is not None
            or self.result.safety_decision_id is not None
        ):
            raise ValueError("a dry-run outcome must not carry persisted ids")
        if self.status is ShadowCycleStatus.RECORDED and self.result.shadow_result_id is None:
            raise ValueError("a recorded outcome must carry the assigned shadow_result_id")


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
        self._gate = gate
        self._decision_store = SQLiteSafetyDecisionStore(Path(decision_db_path))
        self._shadow_db_path = Path(shadow_db_path)
        self._clock = clock
        self._connection_target = connection_target

    @property
    def shadow_db_path(self) -> Path:
        return self._shadow_db_path

    @property
    def decision_db_path(self) -> Path:
        return self._decision_store.path

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
        timeframe: str | None = None,
    ) -> ShadowCycleOutcome:
        """Evaluate the Safety Gate for ``request`` and persist the shadow
        verification record; returns RECORDED or DUPLICATE (see the module
        docstring). Store failures raise -- a failed save never leaves a
        partial row or a dangling idempotency claim (single atomic INSERT).
        """
        self._validate_cycle_args(request, market, account, client_order_id, timeframe)

        existing = self._find_existing(request)
        if existing is not None:
            self._log_duplicate(request, existing, "pre-check found an existing shadow result")
            return ShadowCycleOutcome(status=ShadowCycleStatus.DUPLICATE, result=existing)

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

        result = ShadowResult(
            request=request,
            client_order_id=client_order_id,
            timeframe=timeframe,
            decision=decision,
            intended_order=intended_order_from_request(request, client_order_id=client_order_id),
            market=market,
            policy_snapshot=self._gate.policy.to_snapshot(),
            safety_decision_id=decision_id,
            created_at_utc=self._now(),
        )
        try:
            shadow_result_id = shadow_store.record_shadow_result(
                result, db_path=self._shadow_db_path
            )
        except shadow_store.ShadowResultConflictError:
            # A concurrent writer won the UNIQUE claim between the pre-check
            # and this INSERT: surface the STORED record as an explicit
            # duplicate. (A conflict whose row cannot be found again is a
            # store failure and propagates from _find_existing's re-read.)
            stored = self._find_existing(request)
            if stored is None:
                raise
            self._log_duplicate(request, stored, "lost the concurrent UNIQUE claim")
            return ShadowCycleOutcome(status=ShadowCycleStatus.DUPLICATE, result=stored)
        _LOGGER.info(
            "shadow result recorded execution_request_id=%s signal_id=%s allowed=%s "
            "shadow_result_id=%s policy_id=%s",
            request.execution_request_id,
            request.signal_id,
            decision.allowed,
            shadow_result_id,
            decision.policy_id,
        )
        return ShadowCycleOutcome(
            status=ShadowCycleStatus.RECORDED,
            result=replace(result, shadow_result_id=shadow_result_id),
        )

    def dry_run_cycle(
        self,
        request: ExecutionRequest,
        *,
        market: MarketSafetySnapshot,
        account: AccountSafetySnapshot,
        client_order_id: str,
        timeframe: str | None = None,
    ) -> ShadowCycleOutcome:
        """The dry-run mode cycle: the same production evaluation with ZERO
        side effects -- no DB creation, no migration, no INSERT/UPDATE, no
        state update. Existing store files are probed strictly read-only
        first (an invalid schema is an explicit failure, never success); a
        missing file stays missing."""
        self._validate_cycle_args(request, market, account, client_order_id, timeframe)

        if self._shadow_db_path.exists():
            shadow_store.verify_db(self._shadow_db_path)  # read-only probe
        if self._decision_store.path.exists():
            # Read-only probe: list_for_request verifies the schema without
            # creating or writing anything (missing file would return empty).
            self._decision_store.list_for_request(request.execution_request_id)

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
            decision=decision,
            intended_order=intended_order_from_request(request, client_order_id=client_order_id),
            market=market,
            policy_snapshot=self._gate.policy.to_snapshot(),
            safety_decision_id=None,
            created_at_utc=self._now(),
        )
        return ShadowCycleOutcome(status=ShadowCycleStatus.DRY_RUN, result=result)

    # --- internals -----------------------------------------------------------

    @staticmethod
    def _validate_cycle_args(request, market, account, client_order_id, timeframe) -> None:
        if not isinstance(request, ExecutionRequest):
            raise ValueError(f"request must be an ExecutionRequest, got {request!r}")
        if not isinstance(market, MarketSafetySnapshot):
            raise ValueError(f"market must be a MarketSafetySnapshot, got {market!r}")
        if not isinstance(account, AccountSafetySnapshot):
            raise ValueError(f"account must be an AccountSafetySnapshot, got {account!r}")
        if not isinstance(client_order_id, str) or not client_order_id.strip():
            raise ValueError(f"client_order_id must be a non-empty string, got {client_order_id!r}")
        if timeframe is not None and (not isinstance(timeframe, str) or not timeframe.strip()):
            raise ValueError(f"timeframe must be a non-empty string or None, got {timeframe!r}")

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

    def _find_existing(self, request: ExecutionRequest) -> ShadowResult | None:
        existing = shadow_store.get_shadow_result(
            request.execution_request_id, db_path=self._shadow_db_path
        )
        if existing is not None:
            return existing
        return shadow_store.get_shadow_result_by_signal_id(
            request.signal_id, db_path=self._shadow_db_path
        )

    def _log_duplicate(
        self, request: ExecutionRequest, existing: ShadowResult, reason: str
    ) -> None:
        # Issue #76-style suppression log contract, shadow namespace: the
        # ids, the stored outcome and the suppression reason stay visible.
        _LOGGER.warning(
            "duplicate shadow cycle suppressed: execution_request_id=%s signal_id=%s "
            "existing_shadow_result_id=%s existing_allowed=%s reason=%s",
            request.execution_request_id,
            request.signal_id,
            existing.shadow_result_id,
            existing.allowed,
            reason,
        )

    def _recorded(self, decision: SafetyDecision) -> tuple[SafetyDecision, int | None]:
        """Durably record the decision and VERIFY it by read-back (same
        contract as live submission, Issue #75 review r1). Any failure --
        record exception, read-back miss, read-back mismatch -- appends
        ``decision_persist_failed`` (an allow becomes a rejection) and the
        shadow record is stored WITHOUT a decision reference
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
