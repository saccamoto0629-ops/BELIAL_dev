"""Broker reconciliation -- DIFFERENCE DETECTION ONLY (Issue #103).

*** READ-ONLY. This service compares BELIAL's internal order state against
the broker's and CLASSIFIES what it finds. It never updates an internal
order state, never submits an order and never cancels one -- the broker is
used through query operations exclusively (``get_order`` /
``list_orders``). Repairing what a run finds is Issue #104
(``execution/reconciliation_repair.py`` -- a separate service that consumes
this run's result; detection itself stays read-only); positions / trades /
SL / TP are Issue #105. ***

What a run compares
-------------------

1. Every internal order for which a submit attempt established a broker state
   (``execution_store.list_reconciliation_orders``: pending / accepted /
   partially filled / unknown AND terminal states) is queried on the broker
   by its ``client_order_id``. Created-only intents are excluded:

   - identical states with matching identity and snapshot contract -> NOT a
     discrepancy (nothing is recorded),
   - still working internally but FILLED / CANCELLED / REJECTED at the
     broker -> the matching ``IN_FLIGHT_BROKER_*`` kind,
   - internally UNKNOWN but settled at the broker ->
     ``UNKNOWN_BROKER_SETTLED``,
   - anything else the two sides disagree on -> ``STATE_MISMATCH``,
   - the broker does not know the id -> ``BROKER_ORDER_MISSING``,
   - the query itself failed -> ``QUERY_FAILURE`` (see below).

2. Every order the BROKER holds whose ``client_order_id`` is bound to no
   execution request -> ``BROKER_ONLY_ORDER``. This needs an enumeration,
   which is the optional ``broker_interface.OrderListingBroker`` capability;
   a broker without it (or an enumeration that fails) leaves
   ``ReconciliationRun.broker_listing_error`` set, so "no broker-only
   orders recorded" is never mistaken for "none exist".

Classification of the resolution category
-----------------------------------------

``AUTO_REPAIR_CANDIDATE`` means: the observation could be reflected through
the EXISTING order state machine and execution-store contract (Issue #74)
without losing information. That is decided by REUSING the existing
validators and the store's documented preconditions for
``record_order_result`` -- ``validate_order_state_transition``,
``validate_order_result_update`` against the DB-anchored snapshot, the
identity fields of the stored intent, the ``broker_order_id`` NULL -> value
-> immutable chain, the DB-WIDE ``broker_order_id`` uniqueness (an id
already owned by another execution request could never be recorded onto
this one) and the no-backwards-timestamp rule. Nothing is re-implemented
here, and this service does not perform the update; it only records whether
Issue #104 could.

Everything the existing contract cannot absorb -- a forbidden transition, a
settled outcome contradicted, a snapshot that would drop known fills or a
known ``broker_order_id``, a broker order id another request already owns,
an order only one side knows about -- is ``MANUAL_REVIEW``. So is anything
the store could not ANSWER: if a read needed to prove repairability fails,
the discrepancy falls back to ``MANUAL_REVIEW`` with the failure as its
reason (fail-closed -- repairability is never assumed).

Broker query failures
---------------------

A ``BrokerError`` (timeout, communication failure, authentication, rate
limit, ...) is recorded as a ``QUERY_FAILURE`` discrepancy with the failing
target id, the typed ``BrokerErrorCode`` and the reason, and the run
continues with the other orders. NO state is inferred from a failed query:
an unreachable broker never turns an order into FILLED / CANCELLED /
REJECTED, and it never marks one "not placed". Only the broker-side
ENUMERATION, which belongs to no single target, is recorded on the run
itself. An adapter that leaks an unexpected exception or malformed return
across the Broker boundary is recorded fail-closed as a scoped communication
failure; the remaining orders are still checked.

Repeatability
-------------

Detection reads only; recording appends a new run under a new ``run_id``
(see ``reconciliation_store`` for the duplication policy). Running the same
reconciliation any number of times therefore cannot change internal order
state or history -- an invariant the tests pin explicitly.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from broker.broker_interface import Broker, OrderListingBroker
from broker.broker_models import (
    BrokerError,
    BrokerErrorCode,
    BrokerInvalidRequestError,
    BrokerOrderNotFoundError,
    OrderResult,
    OrderState,
    TERMINAL_ORDER_STATES,
    validate_order_result_update,
    validate_order_state_transition,
)
from execution import execution_store
from execution.execution_models import _require_non_empty_str, _require_utc
from execution.execution_store import ExecutionStoreError
from execution.reconciliation_models import (
    DiscrepancyType,
    OrderDiscrepancy,
    ReconciliationMode,
    ReconciliationRun,
    ReconciliationTargetType,
    ResolutionCategory,
)
from execution.reconciliation_store import (
    init_reconciliation_db,
    record_reconciliation_run,
)

_LOGGER = logging.getLogger(__name__)

# Internal states meaning "the order is still working broker-side".
_IN_FLIGHT_STATES: frozenset[OrderState] = frozenset(
    {OrderState.PENDING, OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED}
)

# Broker states that answer "what happened to the order" for an internally
# UNKNOWN one: the terminal outcomes plus the confirmed working states.
_SETTLED_BROKER_STATES: frozenset[OrderState] = TERMINAL_ORDER_STATES | frozenset(
    {OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED}
)

_TERMINAL_BROKER_DISCREPANCY: dict[OrderState, DiscrepancyType] = {
    OrderState.FILLED: DiscrepancyType.IN_FLIGHT_BROKER_FILLED,
    OrderState.CANCELLED: DiscrepancyType.IN_FLIGHT_BROKER_CANCELLED,
    OrderState.REJECTED: DiscrepancyType.IN_FLIGHT_BROKER_REJECTED,
}


# --- shared repairability classification -------------------------------------
# Module-level so Issue #104's repair service applies EXACTLY the judgement
# Issue #103's detection recorded -- one implementation, two callers, and
# neither re-implements the order state machine or the store contract.


def classify_order_repairability(
    row: dict,
    internal_state: OrderState,
    observed: OrderResult,
    *,
    execution_db_path: Path,
) -> tuple[ResolutionCategory, str]:
    """Auto-repairable iff the observation could be recorded through
    ``execution_store.record_order_result`` without loss.

    The checks mirror that function's documented preconditions -- the
    DB-wide ``broker_order_id`` uniqueness included -- and reuse the
    Issue #73 validators; the update itself is NOT performed here. Used by
    detection (Issue #103) to classify a discrepancy and by the repair
    service (Issue #104) to re-prove repairability at repair time.

    Fail-closed: if an execution-store READ needed to prove repairability
    fails, the observation is classified ``MANUAL_REVIEW`` with the failure
    as the reason. Repairability is never assumed from a lookup that did
    not answer.
    """
    blockers: list[str] = []

    try:
        validate_order_state_transition(internal_state, observed.state)
    except (ValueError, BrokerInvalidRequestError) as exc:
        blockers.append(str(exc))

    identity_problem = _identity_problem(row, observed)
    if identity_problem is not None:
        blockers.append(identity_problem)

    stored_broker_id = row["broker_order_id"]
    if stored_broker_id is not None and observed.broker_order_id != stored_broker_id:
        blockers.append(
            f"broker_order_id is immutable once known: {stored_broker_id!r} -> "
            f"{observed.broker_order_id!r}"
        )

    try:
        anchored = execution_store.load_order_result(
            row["execution_request_id"], db_path=execution_db_path
        )
        owner_problem = _broker_id_owner_problem(row, observed, execution_db_path)
    except ExecutionStoreError as exc:
        # The order store could not answer (schema/integrity failure, a
        # concurrent writer, ...). Nothing is inferred and nothing is
        # changed: a human decides.
        return (
            ResolutionCategory.MANUAL_REVIEW,
            f"the broker state {observed.state.value!r} could not be proven safely "
            f"reflectable onto the internal state {internal_state.value!r}: reading the "
            f"execution order store failed ({type(exc).__name__}: {exc}); no repair "
            "candidacy is assumed",
        )
    if owner_problem is not None:
        blockers.append(owner_problem)
    if anchored is not None:
        try:
            validate_order_result_update(anchored, observed)
        except (ValueError, BrokerInvalidRequestError) as exc:
            blockers.append(f"snapshot update contract: {exc}")

    stored_updated_at = datetime.fromisoformat(row["updated_at_utc"])
    if observed.updated_at_utc < stored_updated_at:
        blockers.append(
            f"the broker snapshot is older than the stored state "
            f"({observed.updated_at_utc.isoformat()} < {row['updated_at_utc']})"
        )

    if blockers:
        return (
            ResolutionCategory.MANUAL_REVIEW,
            f"the broker state {observed.state.value!r} cannot be reflected onto the "
            f"internal state {internal_state.value!r} by the existing order state "
            f"machine / store contract: " + "; ".join(blockers),
        )
    return (
        ResolutionCategory.AUTO_REPAIR_CANDIDATE,
        f"the broker state {observed.state.value!r} is a legal, loss-free resolution of "
        f"the internal state {internal_state.value!r}",
    )


def _broker_id_owner_problem(
    row: dict, observed: OrderResult, execution_db_path: Path
) -> str | None:
    """Whether the observed ``broker_order_id`` already belongs to a
    DIFFERENT execution request (Issue #103 review, Major).

    ``broker_order_id`` is UNIQUE across the whole execution order table
    (PR #91 review Major 3): one broker order can never be attached to
    two execution requests. A PENDING / UNKNOWN order whose broker
    snapshot carries an id another request already owns therefore cannot
    be recorded -- ``record_order_result`` would hit the constraint and
    roll the whole update back -- so it must never be classified as an
    auto-repair candidate. The current order owning the id itself is NOT
    a conflict (that is the normal re-observation case).

    Read-only; a store failure propagates to the caller, which fails
    closed to ``MANUAL_REVIEW``.
    """
    broker_order_id = observed.broker_order_id
    if broker_order_id is None:
        return None  # nothing would be assigned, nothing can collide
    owner = execution_store.get_execution_order_by_broker_order_id(
        broker_order_id, db_path=execution_db_path
    )
    if owner is None:
        return None  # unassigned
    owner_id = owner["execution_request_id"]
    if owner_id == row["execution_request_id"]:
        return None  # this very order already owns it
    return (
        f"broker_order_id {broker_order_id!r} is already owned by execution request "
        f"{owner_id!r}, not by {row['execution_request_id']!r}; broker order IDs are "
        "UNIQUE across the execution order store, so this observation cannot be "
        "recorded onto this request"
    )


def _identity_problem(row: dict, observed: OrderResult) -> str | None:
    """Whether the broker snapshot describes the STORED intent.

    Same comparison ``record_order_result`` performs against the stored
    row: a mismatch means the snapshot belongs to a different order and
    must never be reflected onto this execution request.
    """
    stored_price = None if row["requested_price"] is None else Decimal(row["requested_price"])
    mismatches = [
        name
        for name, stored, seen in (
            ("symbol", row["symbol"], observed.symbol),
            ("side", row["side"], observed.side.value),
            ("order_type", row["order_type"], observed.order_type.value),
            ("quantity", Decimal(row["quantity"]), observed.requested_quantity),
            ("requested_price", stored_price, observed.requested_price),
        )
        if stored != seen
    ]
    if not mismatches:
        return None
    return (
        "the broker snapshot does not describe the stored execution request "
        f"(differing: {', '.join(mismatches)})"
    )


class BrokerOrderReconciliationService:
    """Detect and classify order differences between BELIAL and a broker.

    ``execution_db_path`` is the execution ORDER store to read (never
    written by this service). ``reconciliation_db_path`` is the audit DB the
    run is recorded into by :meth:`run`; :meth:`detect` never touches it.
    ``clock`` supplies the observation timestamps (injected in tests;
    defaults to ``datetime.now(timezone.utc)``) and must return
    timezone-aware UTC datetimes.
    """

    def __init__(
        self,
        *,
        broker: Broker,
        execution_db_path: Path | str,
        reconciliation_db_path: Path | str | None = None,
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
        self._reconciliation_db_path = (
            None if reconciliation_db_path is None else Path(reconciliation_db_path)
        )
        self._clock = clock

    # --- public API ---------------------------------------------------------

    def detect(self, *, run_id: str) -> ReconciliationRun:
        """Compare both sides and return the classified run. NO persistence.

        Reads the execution order store read-only and asks the broker query
        operations only. Used on its own when a caller wants the comparison
        without an audit row (and by :meth:`run` before recording).
        """
        _require_non_empty_str(run_id, "run_id")
        started_at = self._now()
        discrepancies: list[OrderDiscrepancy] = []

        rows = execution_store.list_reconciliation_orders(db_path=self._execution_db_path)
        for row in rows:
            discrepancies.extend(self._reconcile_internal_order(row))

        broker_orders, listing_error = self._list_broker_orders()
        if listing_error is None:
            known = set(execution_store.list_client_order_ids(db_path=self._execution_db_path))
            for observed in broker_orders:
                if observed.client_order_id not in known:
                    discrepancies.append(
                        OrderDiscrepancy(
                            discrepancy_type=DiscrepancyType.BROKER_ONLY_ORDER,
                            target_type=ReconciliationTargetType.BROKER_ORDER,
                            target_id=observed.client_order_id,
                            resolution_category=ResolutionCategory.MANUAL_REVIEW,
                            observed_at_utc=self._now(),
                            detail=(
                                "the broker holds this order but no execution request is bound "
                                "to the client_order_id; adopting a broker-side order as an "
                                "internal one is never automatic"
                            ),
                            broker_state=observed.state,
                            client_order_id=observed.client_order_id,
                            broker_order_id=observed.broker_order_id,
                        )
                    )

        run = ReconciliationRun(
            run_id=run_id,
            mode=ReconciliationMode.DETECTION_ONLY,
            started_at_utc=started_at,
            finished_at_utc=self._now(),
            checked_order_count=len(rows),
            discrepancies=tuple(discrepancies),
            broker_listing_error=listing_error,
        )
        self._log_run(run)
        return run

    def run(self, *, run_id: str) -> ReconciliationRun:
        """:meth:`detect` plus the audit record (return value + log + DB).

        Requires ``reconciliation_db_path``; the audit DB is initialized
        (idempotently) and the run is appended under ``run_id``. Recording a
        ``run_id`` twice is refused by the store -- runs are never
        overwritten.
        """
        if self._reconciliation_db_path is None:
            raise ValueError(
                "run() records the reconciliation result; construct the service with "
                "reconciliation_db_path=... (or call detect() for a non-persisting run)"
            )
        result = self.detect(run_id=run_id)
        init_reconciliation_db(self._reconciliation_db_path)
        record_reconciliation_run(result, db_path=self._reconciliation_db_path)
        return result

    # --- internal-order comparison ------------------------------------------

    def _reconcile_internal_order(self, row: dict) -> tuple[OrderDiscrepancy, ...]:
        client_order_id = row["client_order_id"]
        internal_state = OrderState(row["order_state"])

        try:
            observed = self._broker.get_order(client_order_id)
            if not isinstance(observed, OrderResult):
                raise TypeError(
                    "Broker.get_order() must return an OrderResult, got "
                    f"{type(observed).__name__}"
                )
        except BrokerOrderNotFoundError as exc:
            return (
                self._discrepancy(
                    row,
                    discrepancy_type=DiscrepancyType.BROKER_ORDER_MISSING,
                    resolution_category=ResolutionCategory.MANUAL_REVIEW,
                    internal_state=internal_state,
                    broker_state=None,
                    broker_order_id=row["broker_order_id"],
                    detail=(
                        "the broker does not know this client_order_id "
                        f"({type(exc).__name__}); this does NOT prove the order was never "
                        "placed, so the internal state stays untouched"
                    ),
                ),
            )
        except BrokerError as exc:
            # Query failure: nothing is observed and nothing is inferred.
            return (
                self._discrepancy(
                    row,
                    discrepancy_type=DiscrepancyType.QUERY_FAILURE,
                    resolution_category=ResolutionCategory.QUERY_FAILED,
                    internal_state=internal_state,
                    broker_state=None,
                    broker_order_id=row["broker_order_id"],
                    broker_error_code=exc.code,
                    detail=(
                        f"broker query failed ({type(exc).__name__}: {exc}); the internal "
                        "state is left as-is -- a failed query never settles an order"
                    ),
                ),
            )
        except Exception as exc:
            # A Broker implementation is required to translate transport and
            # malformed-response failures into BrokerError.  If an adapter
            # violates that boundary, reconciliation still records a scoped
            # query failure and continues; it must never lose the whole run or
            # infer an order state from the leaked exception.
            return (
                self._discrepancy(
                    row,
                    discrepancy_type=DiscrepancyType.QUERY_FAILURE,
                    resolution_category=ResolutionCategory.QUERY_FAILED,
                    internal_state=internal_state,
                    broker_state=None,
                    broker_order_id=row["broker_order_id"],
                    broker_error_code=BrokerErrorCode.COMMUNICATION_FAILURE,
                    detail=(
                        "broker query violated the Broker error/return contract "
                        f"({type(exc).__name__}); recorded fail-closed as a "
                        "communication failure and left the internal state unchanged"
                    ),
                ),
            )

        if observed.client_order_id != client_order_id:
            # An adapter answering about a different order would attach a
            # foreign broker order to this execution request (Issue #74's
            # 1:1 binding). Never silently accepted.
            return (
                self._discrepancy(
                    row,
                    discrepancy_type=DiscrepancyType.STATE_MISMATCH,
                    resolution_category=ResolutionCategory.MANUAL_REVIEW,
                    internal_state=internal_state,
                    broker_state=observed.state,
                    broker_order_id=observed.broker_order_id,
                    detail=(
                        f"the broker answered about client_order_id "
                        f"{observed.client_order_id!r} instead of {client_order_id!r}"
                    ),
                ),
            )

        if internal_state is OrderState.UNKNOWN and observed.state is OrderState.UNKNOWN:
            return (
                self._discrepancy(
                    row,
                    discrepancy_type=DiscrepancyType.STATE_MISMATCH,
                    resolution_category=ResolutionCategory.MANUAL_REVIEW,
                    internal_state=internal_state,
                    broker_state=observed.state,
                    broker_order_id=observed.broker_order_id or row["broker_order_id"],
                    detail=(
                        "both BELIAL and the broker still report UNKNOWN; the query did not "
                        "settle the outcome, so this remains visible for manual review"
                    ),
                ),
            )

        category, detail = classify_order_repairability(
            row, internal_state, observed, execution_db_path=self._execution_db_path
        )
        if category is ResolutionCategory.AUTO_REPAIR_CANDIDATE:
            detail += "; no repair was performed (detection-only run)"
        if observed.state is internal_state and category is ResolutionCategory.AUTO_REPAIR_CANDIDATE:
            return ()  # state and the complete stored identity/snapshot contract agree

        discrepancy_type = self._classify_type(internal_state, observed.state)
        return (
            self._discrepancy(
                row,
                discrepancy_type=discrepancy_type,
                resolution_category=category,
                internal_state=internal_state,
                broker_state=observed.state,
                broker_order_id=observed.broker_order_id or row["broker_order_id"],
                detail=detail,
            ),
        )

    @staticmethod
    def _classify_type(internal_state: OrderState, broker_state: OrderState) -> DiscrepancyType:
        if internal_state in _IN_FLIGHT_STATES and broker_state in _TERMINAL_BROKER_DISCREPANCY:
            return _TERMINAL_BROKER_DISCREPANCY[broker_state]
        if internal_state is OrderState.UNKNOWN and broker_state in _SETTLED_BROKER_STATES:
            return DiscrepancyType.UNKNOWN_BROKER_SETTLED
        # Everything else -- an in-flight order the broker reports as UNKNOWN,
        # a working-state disagreement, a terminal internal state contradicted
        # by the broker -- is a plain mismatch for a human to look at.
        return DiscrepancyType.STATE_MISMATCH

    # --- broker-side enumeration --------------------------------------------

    def _list_broker_orders(self) -> tuple[tuple[OrderResult, ...], str | None]:
        """``(orders, listing_error)``; the error explains why the broker-only
        check could not run (capability missing or the query failed)."""
        if not isinstance(self._broker, OrderListingBroker):
            return (
                (),
                f"{type(self._broker).__name__} does not implement OrderListingBroker; "
                "orders existing only on the broker side were NOT checked",
            )
        try:
            orders = self._broker.list_orders()
            if not isinstance(orders, tuple):
                raise TypeError(
                    "OrderListingBroker.list_orders() must return a tuple, got "
                    f"{type(orders).__name__}"
                )
            for order in orders:
                if not isinstance(order, OrderResult):
                    raise TypeError(
                        "OrderListingBroker.list_orders() entries must be OrderResult, got "
                        f"{type(order).__name__}"
                    )
            client_ids = [order.client_order_id for order in orders]
            duplicate_client_ids = sorted(
                client_id for client_id, count in Counter(client_ids).items() if count > 1
            )
            broker_ids = [order.broker_order_id for order in orders if order.broker_order_id is not None]
            duplicate_broker_ids = sorted(
                broker_id for broker_id, count in Counter(broker_ids).items() if count > 1
            )
            if duplicate_client_ids or duplicate_broker_ids:
                return (
                    (),
                    "broker order enumeration is ambiguous: duplicate client_order_id="
                    f"{duplicate_client_ids!r}, duplicate broker_order_id="
                    f"{duplicate_broker_ids!r}; orders existing only on the broker side "
                    "were NOT checked",
                )
        except BrokerError as exc:
            return (
                (),
                f"listing broker orders failed ({type(exc).__name__}: {exc}); orders "
                "existing only on the broker side were NOT checked",
            )
        except Exception as exc:
            return (
                (),
                "listing broker orders violated the Broker error/return contract "
                f"({type(exc).__name__}); orders existing only on the broker "
                "side were NOT checked",
            )
        return orders, None

    # --- helpers -------------------------------------------------------------

    def _discrepancy(
        self,
        row: dict,
        *,
        discrepancy_type: DiscrepancyType,
        resolution_category: ResolutionCategory,
        internal_state: OrderState,
        broker_state: OrderState | None,
        detail: str,
        broker_order_id: str | None = None,
        broker_error_code=None,
    ) -> OrderDiscrepancy:
        return OrderDiscrepancy(
            discrepancy_type=discrepancy_type,
            target_type=ReconciliationTargetType.EXECUTION_ORDER,
            target_id=row["execution_request_id"],
            resolution_category=resolution_category,
            observed_at_utc=self._now(),
            detail=detail,
            internal_state=internal_state,
            broker_state=broker_state,
            client_order_id=row["client_order_id"],
            broker_order_id=broker_order_id,
            broker_error_code=broker_error_code,
        )

    def _now(self) -> datetime:
        value = datetime.now(timezone.utc) if self._clock is None else self._clock()
        _require_utc(value, "clock()")
        return value

    def _log_run(self, run: ReconciliationRun) -> None:
        """Log the run: one WARNING per discrepancy, then a summary.

        Only order identifiers and states are logged -- no credentials, no
        tokens, no account ids (reconciliation never reads account state).
        """
        for discrepancy in run.discrepancies:
            _LOGGER.warning(
                "reconciliation discrepancy: run_id=%s type=%s target_type=%s target_id=%s "
                "client_order_id=%s internal_state=%s broker_state=%s resolution=%s "
                "broker_error_code=%s detail=%s",
                run.run_id,
                discrepancy.discrepancy_type.value,
                discrepancy.target_type.value,
                discrepancy.target_id,
                discrepancy.client_order_id,
                None if discrepancy.internal_state is None else discrepancy.internal_state.value,
                None if discrepancy.broker_state is None else discrepancy.broker_state.value,
                discrepancy.resolution_category.value,
                None
                if discrepancy.broker_error_code is None
                else discrepancy.broker_error_code.value,
                discrepancy.detail,
            )
        if run.broker_listing_error is not None:
            _LOGGER.warning(
                "reconciliation run %s could not enumerate broker orders: %s",
                run.run_id,
                run.broker_listing_error,
            )
        counts = run.resolution_counts()
        _LOGGER.info(
            "reconciliation run %s (%s) finished: checked_orders=%d discrepancies=%d "
            "auto_repair_candidates=%d manual_review=%d query_failed=%d "
            "broker_orders_enumerated=%s",
            run.run_id,
            run.mode.value,
            run.checked_order_count,
            len(run.discrepancies),
            counts[ResolutionCategory.AUTO_REPAIR_CANDIDATE],
            counts[ResolutionCategory.MANUAL_REVIEW],
            counts[ResolutionCategory.QUERY_FAILED],
            run.broker_orders_enumerated,
        )
