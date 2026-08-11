"""Reissue clearance: requirement judgement, issuance and revocation
(Issue #95 / #116).

*** OBSERVATION + EVIDENCE ONLY. Nothing here submits, cancels or repairs
anything. The broker is used through QUERY operations exclusively (the
Issue #103/#105 reconciliation service plus one explicit ``get_order``
probe), and the only execution-DB write is the append-only clearance
evidence table (``execution_store.record_reissue_clearance``). ***

An intent reissue (Issue #95) requires POSITIVE evidence that the signal's
chain left no live broker order or position -- "no discrepancy recorded" is
not evidence, and "a discrepancy was recorded" is not automatically
disqualifying either. This module implements the service-layer half the
Issue #115 store deliberately left open:

Requirement judgement (fail-closed, per ``docs/architecture/execution_reissue.md``)
-----------------------------------------------------------------------------------

A ``valid`` clearance for a signal is granted only when ALL of these hold
for one reconciliation run:

1. the run covered every chain order that has a broker state -- proven by
   reading the chain immediately after claiming the observation and again
   after the run, and requiring the comparable rows (id, revision, state)
   unchanged: the run enumerates ALL broker-stated orders of the same
   execution DB, so an unchanged chain was necessarily part of it;
2. BOTH domains ran (``include_position_scope=True``) and BOTH enumerations
   succeeded (``broker_listing_error`` and every position-summary ``*_error``
   NULL, the chain's symbol not fail-safe excluded);
3. no HAZARDOUS DIVERGENCE was observed. Divergence is judged from the
   combination INTERNAL STATE x BROKER OBSERVATION, never from the
   discrepancy count:

   - ``BROKER_ORDER_MISSING`` about a chain order whose internal state is
     the zero-fill-guaranteed ``REJECTED`` is an EXPECTED ABSENCE -- the
     internal record itself asserts no live broker order remains, so the
     broker not returning it is consistent, not hazardous. This is the ONE
     non-hazardous divergence class; the classification is closed and
     everything unclassifiable is hazardous (fail-closed);
   - the same observation about any other internal state (UNKNOWN /
     ACCEPTED / PARTIALLY_FILLED / ...) is HAZARDOUS: those states do not
     assert the absence of exposure, so absence means "we do not know";
   - every other order-domain discrepancy about a chain order is hazardous;
     a ``QUERY_FAILURE`` about a chain order means NOT OBSERVED (no grant,
     but also no revocation -- nothing was seen);
   - a ``BROKER_ONLY_ORDER`` anywhere is hazardous: an order the internal
     records cannot attribute means the broker state is not fully
     explained, which is exactly the situation a reissue must not proceed
     under;
   - any position-domain discrepancy touching the chain's symbol or a chain
     order is hazardous;
4. for a chain whose tail was never submitted (phase ``created`` -- the
   ``gate_correction`` parent), the tail's ``client_order_id`` is probed
   EXPLICITLY via ``Broker.get_order``: ``BrokerOrderNotFoundError`` is the
   positive absence evidence (recorded content-bound in the granted
   evidence payload), an answered order is hazardous, and a failed probe is
   NOT OBSERVED (no grant).

Ordering and concurrency (write-ahead invalidation + exclusive claim)
---------------------------------------------------------------------

- BEFORE any broker query, the signal's clearance is downgraded by
  appending an ``observing`` generation -- the write-ahead invalidation.
  The same row IS the exclusive observation claim: its payload carries the
  observer id and a lease expiry, and the append uses the store's
  ``expected_generation`` CAS, so exactly one observer can claim a signal.
  While an unexpired ``observing`` claim exists, other observers refuse to
  observe (``ClearanceClaimUnavailableError``); only an EXPIRED lease may
  be taken over (by appending a fresh ``observing`` generation, again under
  the CAS).
- The conclusion is committed AFTER the observation: a revocation (hazard
  observed) is UNCONDITIONAL -- downgrading information always wins, claim
  or no claim -- while a grant re-verifies the claim is still held and
  appends ``valid`` under a generation CAS (``expected_generation`` = the
  claimed ``observing`` generation). A lost claim or CAS failure leaves the
  signal blocked (the newest row stays ``observing``/``revoked``), never
  submittable: failures always fall toward "cannot submit".
- Freshness is enforced at the submission boundary: ``mark_submitting``
  compares the granted evidence's ``observed_at_utc`` against the caller's
  cutoff (``ReissuePolicy.clearance_max_age``, Issue #116).

Account scope
-------------

Positive no-divergence evidence is inseparable from WHERE it was observed.
:func:`derive_account_scope` builds the stable identity
``<mode>:<target>:<safe_account_reference(account_id)>`` from the broker
instance's own ``connection_identity()`` plus the account id it reports --
the account id enters only as the existing non-reversible display digest
(``broker_models.safe_account_reference``), so the persisted scope never
contains the real account id. The SAME derivation runs at grant time here
and at submission time in ``LiveOrderSubmissionService``, and
``mark_submitting`` refuses submission authority when the two differ or the
submission-side derivation fails (fail-closed).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from broker.broker_models import (
    BrokerError,
    BrokerOrderNotFoundError,
    OrderResult,
    OrderState,
    safe_account_reference,
)
from execution import execution_store
from execution.broker_reconciliation import BrokerOrderReconciliationService
from execution.execution_models import _require_non_empty_str, _require_utc
from execution.reconciliation_models import (
    DiscrepancyType,
    OrderDiscrepancy,
    PositionDiscrepancy,
    ReconciliationRun,
)
from execution.safety_gate import derive_connection_identity

_LOGGER = logging.getLogger(__name__)

#: Default exclusive-observation lease (Issue #116). While an ``observing``
#: claim is younger than this, no other observer may take the signal over.
DEFAULT_OBSERVATION_LEASE = timedelta(minutes=5)

#: The scope recorded on an unconditional revocation whose broker scope
#: could not be derived. Revocations must never be blocked by a scope
#: failure (downgrading information always wins); the scope of a revocation
#: is informational -- anything that is not ``valid`` refuses submission
#: regardless of scope.
REVOCATION_SCOPE_UNKNOWN = "unknown"


class ReissueClearanceError(RuntimeError):
    """Base class for clearance service errors."""


class ClearanceScopeError(ReissueClearanceError):
    """The submission/observation account scope could not be derived from
    the broker (identity or account query failed). Fail-closed: without a
    scope no observation is claimed and no clearance can be granted."""


class ClearanceClaimUnavailableError(ReissueClearanceError):
    """Another observer holds an unexpired ``observing`` claim (or won the
    append race) for this signal. The caller must NOT observe."""


class ClearanceClaimLostError(ReissueClearanceError):
    """The claim was lost between the observation and its conclusion commit
    (a newer generation was appended). The conclusion was NOT committed;
    the signal stays blocked."""


def derive_account_scope(broker) -> str:
    """The stable broker/account identity a clearance observation or a
    submission runs against (see the module docstring). Raises
    :class:`ClearanceScopeError` on ANY failure -- an unidentifiable scope
    never yields evidence and never receives submission authority."""
    try:
        mode, target = derive_connection_identity(broker)
        account_state = broker.get_account_state()
        account_id = account_state.account_id
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError(f"broker reported an invalid account id ({account_id!r})")
    except Exception as exc:
        raise ClearanceScopeError(
            f"the broker/account scope could not be derived: {exc}"
        ) from exc
    return f"{mode.value}:{target}:{safe_account_reference(account_id)}"


@dataclass(frozen=True)
class ClearanceEvaluation:
    """The judgement of one reconciliation run against one signal's chain.

    ``hazards`` are observations contradicting "the chain left no live
    broker order or position" (each entry names the observation);
    ``failures`` are portions that were NOT observed (enumeration errors,
    fail-safe exclusions, probe failures, coverage loss) -- they block a
    grant but, having observed nothing, do not by themselves revoke;
    ``expected_absences`` are the chain orders whose ``BROKER_ORDER_MISSING``
    was classified as consistent with a zero-fill definitive rejection.
    """

    hazards: tuple[str, ...]
    failures: tuple[str, ...]
    expected_absences: tuple[str, ...]

    @property
    def satisfied(self) -> bool:
        return not self.hazards and not self.failures


def _classify_order_discrepancy(
    discrepancy: OrderDiscrepancy,
    *,
    chain_request_ids: frozenset[str],
    chain_client_ids: frozenset[str],
    rejected_chain_ids: frozenset[str],
) -> tuple[str | None, str | None, str | None]:
    """Classify one order-domain discrepancy for this chain: returns
    ``(hazard, failure, expected_absence_target)`` -- at most one non-None.
    The classification is CLOSED: the only non-hazardous divergence is the
    expected absence below; everything else about the chain is hazardous,
    and an unattributable broker-side order is hazardous for every chain
    (fail-closed on anything unclassifiable)."""
    about_chain = (
        discrepancy.target_id in chain_request_ids
        or (
            discrepancy.client_order_id is not None
            and discrepancy.client_order_id in chain_client_ids
        )
    )
    if discrepancy.discrepancy_type is DiscrepancyType.BROKER_ONLY_ORDER:
        return (
            "the broker holds an order no internal record accounts for "
            f"(client_order_id {discrepancy.target_id!r}); the broker state is not "
            "fully explained by the internal records",
            None,
            None,
        )
    if not about_chain:
        return None, None, None
    if discrepancy.discrepancy_type is DiscrepancyType.QUERY_FAILURE:
        return (
            None,
            f"chain order {discrepancy.target_id!r} could not be observed "
            f"(broker query failed: {discrepancy.detail})",
            None,
        )
    if discrepancy.discrepancy_type is DiscrepancyType.BROKER_ORDER_MISSING:
        # Internal state x observation: absence is EXPECTED exactly when the
        # internal record itself asserts "no live broker order remains" --
        # the definitively rejected state, whose zero-fill is an OrderResult
        # construction invariant. Absence under any other internal state
        # (UNKNOWN / ACCEPTED / PARTIALLY_FILLED / ...) means "we do not
        # know" and is hazardous.
        if (
            discrepancy.internal_state is OrderState.REJECTED
            and discrepancy.target_id in rejected_chain_ids
        ):
            return None, None, discrepancy.target_id
        internal = (
            None
            if discrepancy.internal_state is None
            else discrepancy.internal_state.value
        )
        return (
            f"chain order {discrepancy.target_id!r} is missing on the broker while "
            f"its internal state {internal!r} does not assert the absence of live "
            "exposure",
            None,
            None,
        )
    return (
        f"chain order {discrepancy.target_id!r} diverges from the broker "
        f"({discrepancy.discrepancy_type.value}: internal "
        f"{None if discrepancy.internal_state is None else discrepancy.internal_state.value!r} "
        f"vs broker "
        f"{None if discrepancy.broker_state is None else discrepancy.broker_state.value!r})",
        None,
        None,
    )


def evaluate_clearance_run(
    run: ReconciliationRun,
    *,
    chain_rows_before: list[dict],
    chain_rows_after: list[dict],
) -> ClearanceEvaluation:
    """Judge one reconciliation run against one signal's chain (pure
    function; see the module docstring for the requirement list).

    ``chain_rows_before`` / ``chain_rows_after`` are the chain read
    immediately after claiming the observation and immediately after the
    run: unchanged comparable rows prove the run's enumeration covered the
    chain. Every requirement that cannot be POSITIVELY confirmed lands in
    ``failures`` (fail-closed)."""
    if not isinstance(run, ReconciliationRun):
        raise ValueError(f"run must be a ReconciliationRun, got {run!r}")
    hazards: list[str] = []
    failures: list[str] = []
    expected_absences: list[str] = []

    if not chain_rows_after:
        return ClearanceEvaluation((), ("the signal has no execution order chain",), ())
    symbol = chain_rows_after[0]["symbol"]

    def comparable(rows):
        return [
            (row["execution_request_id"], row["revision"], row["order_state"])
            for row in rows
            if row["order_state"] is not None
        ]

    if comparable(chain_rows_before) != comparable(chain_rows_after):
        failures.append(
            "the chain's broker-stated orders changed while the observation ran; "
            "the run cannot be proven to cover them"
        )

    chain_request_ids = frozenset(row["execution_request_id"] for row in chain_rows_after)
    chain_client_ids = frozenset(row["client_order_id"] for row in chain_rows_after)
    rejected_chain_ids = frozenset(
        row["execution_request_id"]
        for row in chain_rows_after
        if row["order_state"] == OrderState.REJECTED.value
    )

    # requirement 2 + 3: both domains ran and both enumerations succeeded
    if run.position_summary is None:
        failures.append(
            "the run did not reconcile the position domain "
            "(include_position_scope was not set)"
        )
    else:
        summary = run.position_summary
        for label, error in (
            ("broker order enumeration", run.broker_listing_error),
            ("internal exposure derivation", summary.internal_exposure_error),
            ("broker position enumeration", summary.position_listing_error),
            ("broker open-trade enumeration", summary.trade_listing_error),
            ("broker closed-trade enumeration", summary.closed_trade_listing_error),
        ):
            if error is not None:
                failures.append(f"{label} did not succeed: {error}")
        if symbol in summary.excluded_symbols:
            failures.append(
                f"symbol {symbol!r} was fail-safe excluded from the exposure "
                "comparison (an internal outcome is unresolved); the position "
                "domain was not observed for this chain"
            )
    if run.position_summary is None and run.broker_listing_error is not None:
        failures.append(
            f"broker order enumeration did not succeed: {run.broker_listing_error}"
        )

    for discrepancy in run.discrepancies:
        hazard, failure, absence = _classify_order_discrepancy(
            discrepancy,
            chain_request_ids=chain_request_ids,
            chain_client_ids=chain_client_ids,
            rejected_chain_ids=rejected_chain_ids,
        )
        if hazard is not None:
            hazards.append(hazard)
        if failure is not None:
            failures.append(failure)
        if absence is not None:
            expected_absences.append(absence)

    for discrepancy in run.position_discrepancies:
        if not isinstance(discrepancy, PositionDiscrepancy):  # pragma: no cover
            hazards.append(f"unclassifiable position observation {discrepancy!r}")
            continue
        touches_chain = discrepancy.symbol == symbol or (
            discrepancy.execution_request_id is not None
            and discrepancy.execution_request_id in chain_request_ids
        )
        if touches_chain:
            hazards.append(
                f"position-domain divergence on the chain's scope "
                f"({discrepancy.discrepancy_type.value} on {discrepancy.target_id!r}): "
                f"{discrepancy.detail}"
            )

    return ClearanceEvaluation(tuple(hazards), tuple(failures), tuple(expected_absences))


@dataclass(frozen=True)
class ClearanceObservation:
    """The outcome of one :meth:`ReissueClearanceService.observe_signal`."""

    signal_id: str
    granted: bool
    evaluation: ClearanceEvaluation
    clearance: dict | None  # the appended conclusion row (valid/revoked), if any
    run_id: str
    account_scope: str


class ReissueClearanceService:
    """Claim, observe, judge and conclude reissue clearances (Issue #116).

    ``broker`` must be the SAME broker connection later used for
    submission: the derived account scope binds the evidence to it.
    ``observer_id`` identifies this observer in the claim lease (e.g. a
    hostname/pid label); ``lease`` bounds how long a crashed observation
    blocks the signal before takeover."""

    def __init__(
        self,
        *,
        broker,
        execution_db_path: Path | str,
        reconciliation_db_path: Path | str,
        observer_id: str,
        clock: Callable[[], datetime] | None = None,
        lease: timedelta = DEFAULT_OBSERVATION_LEASE,
    ) -> None:
        _require_non_empty_str(observer_id, "observer_id")
        if not isinstance(lease, timedelta) or lease <= timedelta(0):
            raise ValueError(f"lease must be a positive timedelta, got {lease!r}")
        if clock is not None and not callable(clock):
            raise ValueError(f"clock must be a callable returning a datetime, got {clock!r}")
        self._broker = broker
        self._execution_db_path = Path(execution_db_path)
        self._reconciliation_db_path = Path(reconciliation_db_path)
        self._observer_id = observer_id
        self._clock = clock
        self._lease = lease

    # --- public API ---------------------------------------------------------

    def observe_signal(self, signal_id: str) -> ClearanceObservation:
        """One full clearance observation for ``signal_id``: claim (with
        write-ahead invalidation), reconcile both domains, judge, conclude.

        Raises :class:`ClearanceClaimUnavailableError` when another
        observer's lease is unexpired (nothing is observed),
        :class:`ClearanceScopeError` when the broker/account scope cannot
        be derived, and :class:`ClearanceClaimLostError` when a satisfied
        observation cannot be granted because the claim was lost (the
        signal stays blocked). A hazardous observation returns
        ``granted=False`` with the appended ``revoked`` row; an observation
        with unobserved portions returns ``granted=False`` with no appended
        conclusion (the ``observing`` claim row stays newest -- blocked)."""
        _require_non_empty_str(signal_id, "signal_id")
        chain_before = execution_store.list_execution_order_chain(
            signal_id, db_path=self._execution_db_path
        )
        if not chain_before:
            raise ValueError(
                f"signal {signal_id!r} has no execution order chain; there is nothing "
                "to observe"
            )
        account_scope = derive_account_scope(self._broker)

        claim = self._claim(signal_id, account_scope)
        observing_generation = claim["clearance_generation"]
        run_id = f"reissue-clearance-{signal_id}-g{observing_generation}"

        service = BrokerOrderReconciliationService(
            broker=self._broker,
            execution_db_path=self._execution_db_path,
            reconciliation_db_path=self._reconciliation_db_path,
            clock=self._clock,
        )
        run = service.run(run_id=run_id, include_position_scope=True)

        chain_after = execution_store.list_execution_order_chain(
            signal_id, db_path=self._execution_db_path
        )
        evaluation = evaluate_clearance_run(
            run, chain_rows_before=chain_before, chain_rows_after=chain_after
        )

        # Requirement 4 (gate_correction parents): a never-submitted chain
        # tail needs POSITIVE broker absence evidence for its client key.
        parent_absence = None
        tail = chain_after[-1] if chain_after else None
        if (
            tail is not None
            and tail["submission_phase"] == execution_store.PHASE_CREATED
            and tail["order_state"] is None
        ):
            parent_absence, probe_hazard, probe_failure = self._probe_parent_absence(tail)
            if probe_hazard is not None or probe_failure is not None:
                evaluation = ClearanceEvaluation(
                    evaluation.hazards + ((probe_hazard,) if probe_hazard else ()),
                    evaluation.failures + ((probe_failure,) if probe_failure else ()),
                    evaluation.expected_absences,
                )

        observed_at = run.finished_at_utc
        if evaluation.hazards:
            revoked = self._append_conclusion(
                signal_id,
                status=execution_store.CLEARANCE_STATUS_REVOKED,
                account_scope=account_scope,
                observed_at=observed_at,
                payload={
                    "phase": "conclusion",
                    "conclusion": "revoked",
                    "run_id": run_id,
                    "observer_id": self._observer_id,
                    "hazards": list(evaluation.hazards),
                    "failures": list(evaluation.failures),
                },
                expected_generation=None,  # downgrades are unconditional
            )
            _LOGGER.warning(
                "reissue clearance REVOKED: signal_id=%s run_id=%s hazards=%s",
                signal_id,
                run_id,
                list(evaluation.hazards),
            )
            return ClearanceObservation(
                signal_id, False, evaluation, revoked, run_id, account_scope
            )
        if evaluation.failures:
            # Nothing hazardous was OBSERVED, but the requirements are not
            # positively confirmed either: no conclusion is appended, the
            # ``observing`` claim stays newest, and the signal stays
            # blocked until a later observation succeeds or takes over.
            _LOGGER.warning(
                "reissue clearance NOT granted (unobserved portions): signal_id=%s "
                "run_id=%s failures=%s",
                signal_id,
                run_id,
                list(evaluation.failures),
            )
            return ClearanceObservation(
                signal_id, False, evaluation, None, run_id, account_scope
            )

        # Re-verify the claim is still held IMMEDIATELY before committing
        # the grant, then commit under the generation CAS: only the claim
        # holder may grant, and only onto its own observing generation.
        current = execution_store.get_reissue_clearance(
            signal_id, db_path=self._execution_db_path
        )
        if (
            current is None
            or current["clearance_generation"] != observing_generation
            or current["evidence_id"] != claim["evidence_id"]
        ):
            raise ClearanceClaimLostError(
                f"the observation claim for signal {signal_id!r} (generation "
                f"{observing_generation}) was lost before the conclusion could be "
                "committed; the grant was NOT recorded and the signal stays blocked"
            )
        payload = {
            "phase": "conclusion",
            "conclusion": "valid",
            "run_id": run_id,
            "observer_id": self._observer_id,
            "checked_order_count": run.checked_order_count,
            "chain_orders": sorted(
                row["execution_request_id"] for row in chain_after
            ),
            "expected_absences": sorted(evaluation.expected_absences),
        }
        if parent_absence is not None:
            payload["parent_absence"] = parent_absence
        try:
            granted = self._append_conclusion(
                signal_id,
                status=execution_store.CLEARANCE_STATUS_VALID,
                account_scope=account_scope,
                observed_at=observed_at,
                payload=payload,
                expected_generation=observing_generation,
            )
        except execution_store.ExecutionClearanceError as exc:
            raise ClearanceClaimLostError(
                f"the grant for signal {signal_id!r} lost the generation CAS "
                f"(claimed generation {observing_generation}): {exc}; the signal "
                "stays blocked"
            ) from exc
        _LOGGER.info(
            "reissue clearance GRANTED: signal_id=%s run_id=%s generation=%s scope=%s "
            "expected_absences=%s",
            signal_id,
            run_id,
            granted["clearance_generation"],
            account_scope,
            sorted(evaluation.expected_absences),
        )
        return ClearanceObservation(
            signal_id, True, evaluation, granted, run_id, account_scope
        )

    def revoke_signal(self, signal_id: str, *, reason: str) -> dict:
        """Unconditionally revoke a signal's clearance (an explicit
        downgrade -- e.g. an operator invalidation). Never requires a claim:
        information that lowers authority always wins."""
        _require_non_empty_str(signal_id, "signal_id")
        _require_non_empty_str(reason, "reason")
        try:
            account_scope = derive_account_scope(self._broker)
        except ClearanceScopeError:
            account_scope = REVOCATION_SCOPE_UNKNOWN
        now = self._now()
        return self._append_conclusion(
            signal_id,
            status=execution_store.CLEARANCE_STATUS_REVOKED,
            account_scope=account_scope,
            observed_at=now,
            payload={
                "phase": "conclusion",
                "conclusion": "revoked",
                "observer_id": self._observer_id,
                "reason": reason,
            },
            expected_generation=None,
        )

    # --- internals ----------------------------------------------------------

    def _now(self) -> datetime:
        value = datetime.now(timezone.utc) if self._clock is None else self._clock()
        _require_utc(value, "clock()")
        return value

    def _claim(self, signal_id: str, account_scope: str) -> dict:
        """Write-ahead invalidation + exclusive claim: append the
        ``observing`` generation under the store's generation CAS. Refuses
        (nothing appended, nothing observed) while another observer's lease
        is unexpired -- an ``observing`` row whose lease cannot be read
        counts as UNEXPIRED (fail-closed: exclusivity is never assumed from
        unreadable data; the block clears via lease-less takeover only after
        an explicit revocation)."""
        now = self._now()
        current = execution_store.get_reissue_clearance(
            signal_id, db_path=self._execution_db_path
        )
        current_generation = 0 if current is None else current["clearance_generation"]
        if (
            current is not None
            and current["status"] == execution_store.CLEARANCE_STATUS_OBSERVING
        ):
            expires_at = self._lease_expiry(current)
            if expires_at is None or now < expires_at:
                raise ClearanceClaimUnavailableError(
                    f"signal {signal_id!r} is already being observed (generation "
                    f"{current_generation}); the claim lease "
                    f"{'cannot be read' if expires_at is None else f'expires at {expires_at.isoformat()}'} "
                    "-- refusing to observe concurrently"
                )
        try:
            return execution_store.record_reissue_clearance(
                signal_id,
                evidence_id=f"clr-{uuid.uuid4().hex}",
                status=execution_store.CLEARANCE_STATUS_OBSERVING,
                account_scope=account_scope,
                observed_at_utc=now,
                evidence_json=json.dumps(
                    {
                        "phase": "observing",
                        "observer_id": self._observer_id,
                        "claimed_at_utc": now.isoformat(),
                        "lease_expires_at_utc": (now + self._lease).isoformat(),
                        "takeover_of_generation": (
                            current_generation
                            if current is not None
                            and current["status"]
                            == execution_store.CLEARANCE_STATUS_OBSERVING
                            else None
                        ),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                recorded_at_utc=now,
                expected_generation=current_generation,
                db_path=self._execution_db_path,
            )
        except execution_store.ExecutionClearanceError as exc:
            raise ClearanceClaimUnavailableError(
                f"another observer claimed signal {signal_id!r} first: {exc}"
            ) from exc

    @staticmethod
    def _lease_expiry(observing_row: dict) -> datetime | None:
        try:
            payload = json.loads(observing_row["evidence_json"])
            raw = payload.get("lease_expires_at_utc")
            value = datetime.fromisoformat(raw)
            if value.tzinfo is None or value.utcoffset() is None:
                return None
            return value
        except (ValueError, TypeError, KeyError):
            return None

    def _probe_parent_absence(
        self, tail: dict
    ) -> tuple[dict | None, str | None, str | None]:
        """Explicit broker probe for a never-submitted chain tail's client
        key. Returns ``(absence_evidence, hazard, failure)``."""
        client_order_id = tail["client_order_id"]
        probed_at = self._now()
        try:
            observed = self._broker.get_order(client_order_id)
        except BrokerOrderNotFoundError:
            return (
                {
                    "client_order_id": client_order_id,
                    "execution_request_id": tail["execution_request_id"],
                    "confirmed_absent": True,
                    "probed_at_utc": probed_at.isoformat(),
                },
                None,
                None,
            )
        except BrokerError as exc:
            return (
                None,
                None,
                f"the broker probe for the unsubmitted parent "
                f"{tail['execution_request_id']!r} (client_order_id "
                f"{client_order_id!r}) failed ({type(exc).__name__}: {exc}); absence "
                "was not confirmed",
            )
        except Exception as exc:
            return (
                None,
                None,
                f"the broker probe for the unsubmitted parent "
                f"{tail['execution_request_id']!r} violated the Broker error/return "
                f"contract ({type(exc).__name__}); absence was not confirmed",
            )
        state = observed.state.value if isinstance(observed, OrderResult) else "malformed"
        return (
            None,
            f"the broker HOLDS an order for the internally never-submitted parent "
            f"{tail['execution_request_id']!r} (client_order_id {client_order_id!r}, "
            f"broker state {state!r}); an unsubmitted intent must have no broker "
            "order",
            None,
        )

    def _append_conclusion(
        self,
        signal_id: str,
        *,
        status: str,
        account_scope: str,
        observed_at: datetime,
        payload: dict,
        expected_generation: int | None,
    ) -> dict:
        return execution_store.record_reissue_clearance(
            signal_id,
            evidence_id=f"clr-{uuid.uuid4().hex}",
            status=status,
            account_scope=account_scope,
            observed_at_utc=observed_at,
            evidence_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            recorded_at_utc=self._now(),
            expected_generation=expected_generation,
            db_path=self._execution_db_path,
        )
