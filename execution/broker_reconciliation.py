"""Broker reconciliation -- DIFFERENCE DETECTION ONLY (Issues #103 / #105).

*** READ-ONLY. This service compares BELIAL's internal state against the
broker's and CLASSIFIES what it finds. It never updates an internal order
state, never submits an order and never cancels one -- the broker is used
through query operations exclusively (``get_order`` / ``list_orders`` /
``get_positions`` / ``get_open_trades`` / ``get_closed_trades``). Repairing
what a run finds is Issue #104 and covers ORDER STATES ONLY; the position /
trade / SL / TP scope added by Issue #105 is detection + MANUAL_REVIEW
classification with NO repair path at all. ***

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

from broker.broker_interface import (
    Broker,
    ClosedTradeListingBroker,
    OrderListingBroker,
    TradeListingBroker,
)
from broker.broker_models import (
    BrokerError,
    BrokerErrorCode,
    BrokerInvalidRequestError,
    BrokerOrderNotFoundError,
    ClosedTradeInfo,
    OrderResult,
    OrderSide,
    OrderState,
    PositionInfo,
    PositionSide,
    TERMINAL_ORDER_STATES,
    TradeInfo,
    validate_order_result_update,
    validate_order_state_transition,
)
from execution import execution_store
from execution.execution_models import _require_non_empty_str, _require_utc
from execution.execution_store import ExecutionStoreError
from execution.reconciliation_models import (
    DiscrepancyType,
    OrderDiscrepancy,
    PositionDiscrepancy,
    PositionReconciliationSummary,
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

    def detect(
        self, *, run_id: str, include_position_scope: bool = False
    ) -> ReconciliationRun:
        """Compare both sides and return the classified run. NO persistence.

        Reads the execution order store read-only and asks the broker query
        operations only. Used on its own when a caller wants the comparison
        without an audit row (and by :meth:`run` before recording).

        ``include_position_scope`` (Issue #105) additionally reconciles the
        position domain — positions / open trades / SL / TP / closed-trade
        evidence — recording MANUAL_REVIEW-only :class:`PositionDiscrepancy`
        records and a :class:`PositionReconciliationSummary` on the run. It
        stays False by default so Issue #103 order-only callers keep their
        exact behavior. The position scope is detection-only like everything
        else here: broker enumerations are the only queries it adds, and
        every failure degrades to a summary error, never to a guessed state.
        """
        _require_non_empty_str(run_id, "run_id")
        started_at = self._now()
        discrepancies: list[OrderDiscrepancy] = []

        rows = execution_store.list_reconciliation_orders(db_path=self._execution_db_path)
        for row in rows:
            discrepancies.extend(self._reconcile_internal_order(row))

        position_summary: PositionReconciliationSummary | None = None
        position_discrepancies: tuple[PositionDiscrepancy, ...] = ()
        if include_position_scope:
            position_summary, position_discrepancies = self._reconcile_position_scope(rows)

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
            position_discrepancies=position_discrepancies,
            position_summary=position_summary,
        )
        self._log_run(run)
        return run

    def run(
        self, *, run_id: str, include_position_scope: bool = False
    ) -> ReconciliationRun:
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
        result = self.detect(run_id=run_id, include_position_scope=include_position_scope)
        init_reconciliation_db(self._reconciliation_db_path)
        record_reconciliation_run(result, db_path=self._reconciliation_db_path)
        return result

    # --- internal-order comparison ------------------------------------------

    def _reconcile_internal_order(self, row: dict) -> tuple[OrderDiscrepancy, ...]:
        execution_request_id = row["execution_request_id"]
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

        category, detail = self._classify_resolution(row, internal_state, observed)
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

    def _classify_resolution(
        self, row: dict, internal_state: OrderState, observed: OrderResult
    ) -> tuple[ResolutionCategory, str]:
        """Auto-repairable iff Issue #104 could record this observation
        through ``execution_store.record_order_result`` without loss.

        The checks mirror that function's documented preconditions -- the
        DB-wide ``broker_order_id`` uniqueness included -- and reuse the
        Issue #73 validators; the update itself is NOT performed here.

        Fail-closed: if an execution-store READ needed to prove
        repairability fails, the discrepancy is classified
        ``MANUAL_REVIEW`` with the failure as the reason. Repairability is
        never assumed from a lookup that did not answer.
        """
        blockers: list[str] = []
        execution_request_id = row["execution_request_id"]

        try:
            validate_order_state_transition(internal_state, observed.state)
        except (ValueError, BrokerInvalidRequestError) as exc:
            blockers.append(str(exc))

        identity_problem = self._identity_problem(row, observed)
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
                execution_request_id, db_path=self._execution_db_path
            )
            owner_problem = self._broker_id_owner_problem(row, observed)
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
            f"the internal state {internal_state.value!r}; no repair was performed "
            "(detection-only run)",
        )

    def _broker_id_owner_problem(self, row: dict, observed: OrderResult) -> str | None:
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
            broker_order_id, db_path=self._execution_db_path
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

    @staticmethod
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

    # --- position / trade / SL / TP scope (Issue #105) ------------------------
    #
    # DETECTION ONLY, MANUAL REVIEW ONLY. The position domain has no repair
    # path: every discrepancy built here is ResolutionCategory.MANUAL_REVIEW
    # (the model and the store CHECK both enforce it), broker-only positions
    # and trades are never adopted, internal-only exposure is never closed,
    # and SL / TP orders are never created or changed. Matching is ID-based
    # exclusively -- the broker-reported client_order_id on a trade, or the
    # trade id appearing among an order's recorded broker fill ids -- never
    # by time or price proximity. Anything that cannot be matched UNIQUELY,
    # and any symbol whose internal exposure is indeterminate (an UNKNOWN
    # order outcome, an unreadable snapshot), is surfaced for manual review
    # or excluded fail-safe instead of being guessed at.

    def _reconcile_position_scope(
        self, rows: list[dict]
    ) -> tuple[PositionReconciliationSummary, tuple[PositionDiscrepancy, ...]]:
        """Compare internal exposure expectations against the broker's
        positions / open trades / closed-trade evidence. Read-only."""
        discrepancies: dict[
            tuple[DiscrepancyType, ReconciliationTargetType, str], PositionDiscrepancy
        ] = {}

        def add(discrepancy: PositionDiscrepancy) -> None:
            # One record per (kind, target); a later duplicate observation of
            # the same finding (e.g. per-trade SL checks of an order whose
            # fills opened several trades) does not pile up rows.
            key = (
                discrepancy.discrepancy_type,
                discrepancy.target_type,
                discrepancy.target_id,
            )
            discrepancies.setdefault(key, discrepancy)

        exposure = self._derive_internal_exposure(rows)
        excluded = exposure["excluded_symbols"]

        positions, position_listing_error = self._list_broker_positions()
        checked_position_count = 0
        if position_listing_error is None:
            checked_position_count = self._compare_positions(
                exposure, positions, excluded, add
            )

        open_trades, trade_listing_error = self._list_broker_open_trades()
        checked_trade_count = 0
        open_matched_order_ids: set[str] = set()
        open_trade_ids: set[str] = set()
        if trade_listing_error is None:
            checked_trade_count = len(open_trades)
            open_trade_ids = {trade.trade_id for trade in open_trades}
            open_matched_order_ids = self._compare_open_trades(
                exposure, open_trades, excluded, add
            )

        closed_trades, closed_trade_listing_error = self._list_broker_closed_trades(
            open_trades_available=trade_listing_error is None
        )
        checked_closed_trade_count = 0
        if closed_trade_listing_error is None:
            checked_closed_trade_count = len(closed_trades)
            self._compare_closed_trades(
                exposure,
                closed_trades,
                open_trade_ids,
                open_matched_order_ids,
                excluded,
                add,
            )

        summary = PositionReconciliationSummary(
            checked_position_count=checked_position_count,
            checked_trade_count=checked_trade_count,
            checked_closed_trade_count=checked_closed_trade_count,
            internal_exposure_error=exposure["internal_error"],
            position_listing_error=position_listing_error,
            trade_listing_error=trade_listing_error,
            closed_trade_listing_error=closed_trade_listing_error,
            excluded_symbols=tuple(sorted(excluded)),
        )
        return summary, tuple(discrepancies.values())

    def _derive_internal_exposure(self, rows: list[dict]) -> dict:
        """Derive what BELIAL expects broker-side, from internal records ONLY.

        Per order: the signed filled quantity (BUY positive, SELL negative),
        taken from the stored ``OrderResult`` snapshot. Netting them per
        symbol yields the expected per-symbol exposure -- an internal SELL
        that closed an internal BUY nets to zero, so "closed inside BELIAL"
        needs no separate close record.

        Fail-safe exclusions (never guessed): an order whose outcome is
        UNKNOWN, whose snapshot cannot be read, or whose fill amount cannot
        be established (PARTIALLY_FILLED / CANCELLED without a stored
        snapshot) makes its SYMBOL's expectation indeterminate; the symbol
        is excluded from every exposure comparison and reported in the run
        summary. ID indexes are still built for such orders -- identity
        matching is exact evidence and unaffected by amount indeterminacy.
        """
        net: dict[str, Decimal] = {}
        excluded_symbols: set[str] = set()
        by_client: dict[str, dict] = {}
        by_fill: dict[str, list[dict]] = {}
        infos: dict[str, dict] = {}
        read_failures: list[str] = []

        for row in rows:
            request_id = row["execution_request_id"]
            symbol = row["symbol"]
            state = OrderState(row["order_state"])
            result = None
            result_readable = True
            try:
                result = execution_store.load_order_result(
                    request_id, db_path=self._execution_db_path
                )
            except ExecutionStoreError as exc:
                result_readable = False
                read_failures.append(
                    f"{request_id}: {type(exc).__name__}"
                )

            filled: Decimal | None
            if not result_readable:
                filled = None  # unreadable snapshot: amount unknowable
            elif state is OrderState.UNKNOWN:
                # An unconfirmed outcome may carry unobserved fills; any
                # stored snapshot is not trusted as the final amount.
                filled = None
            elif result is not None:
                filled = result.filled_quantity
            elif state in (OrderState.PENDING, OrderState.ACCEPTED, OrderState.REJECTED):
                filled = Decimal(0)  # these states REQUIRE filled == 0
            elif state is OrderState.FILLED:
                filled = Decimal(row["quantity"])  # FILLED requires filled == requested
            else:
                # PARTIALLY_FILLED / CANCELLED without a snapshot: the fill
                # amount is unknowable from state alone.
                filled = None

            info = {
                "row": row,
                "state": state,
                "result": result,
                "filled": filled,
            }
            infos[request_id] = info
            by_client[row["client_order_id"]] = info
            if result is not None:
                for fill in result.fills:
                    by_fill.setdefault(fill.fill_id, []).append(info)

            if filled is None:
                excluded_symbols.add(symbol)
            elif filled > 0:
                signed = filled if row["side"] == OrderSide.BUY.value else -filled
                net[symbol] = net.get(symbol, Decimal(0)) + signed

        internal_error = None
        if read_failures:
            internal_error = (
                f"{len(read_failures)} execution-store snapshot read(s) failed "
                f"({'; '.join(sorted(read_failures))}); the affected symbols were "
                "excluded from the exposure comparison and nothing was inferred"
            )
        return {
            "net": net,
            "excluded_symbols": excluded_symbols,
            "by_client": by_client,
            "by_fill": by_fill,
            "infos": infos,
            "internal_error": internal_error,
        }

    def _compare_positions(
        self,
        exposure: dict,
        positions: tuple[PositionInfo, ...],
        excluded: set[str],
        add,
    ) -> int:
        """Presence comparison per (symbol, side) aggregate; quantity
        comparison is deliberately out of Issue #105's minimum scope (the
        per-trade checks carry the finer granularity)."""
        observed_at = self._now()
        expected: dict[tuple[str, PositionSide], Decimal] = {}
        for symbol, value in exposure["net"].items():
            if symbol in excluded or value == 0:
                continue
            side = PositionSide.LONG if value > 0 else PositionSide.SHORT
            expected[(symbol, side)] = abs(value)
        broker_keys = {(entry.symbol, entry.side): entry for entry in positions}

        checked = {
            key for key in set(expected) | set(broker_keys) if key[0] not in excluded
        }
        for symbol, side in sorted(expected, key=lambda k: (k[0], k[1].value)):
            if (symbol, side) in broker_keys:
                continue
            add(
                PositionDiscrepancy(
                    discrepancy_type=DiscrepancyType.POSITION_MISSING_ON_BROKER,
                    target_type=ReconciliationTargetType.POSITION,
                    target_id=f"{symbol}:{side.value}",
                    resolution_category=ResolutionCategory.MANUAL_REVIEW,
                    observed_at_utc=observed_at,
                    detail=(
                        f"BELIAL's filled orders expect a {side.value} exposure of "
                        f"{expected[(symbol, side)]} on {symbol}, but the broker reports no "
                        "such position; the internal records are NOT changed and nothing is "
                        "closed or re-opened automatically"
                    ),
                    symbol=symbol,
                    internal_value=f"{side.value} {expected[(symbol, side)]}",
                )
            )
        for symbol, side in sorted(broker_keys, key=lambda k: (k[0], k[1].value)):
            if (symbol, side) in expected or symbol in excluded:
                continue
            entry = broker_keys[(symbol, side)]
            add(
                PositionDiscrepancy(
                    discrepancy_type=DiscrepancyType.BROKER_ONLY_POSITION,
                    target_type=ReconciliationTargetType.POSITION,
                    target_id=f"{symbol}:{side.value}",
                    resolution_category=ResolutionCategory.MANUAL_REVIEW,
                    observed_at_utc=observed_at,
                    detail=(
                        f"the broker holds a {side.value} position of {entry.quantity} on "
                        f"{symbol} that no internal filled order accounts for; adopting a "
                        "broker-side position as an internal one is never automatic"
                    ),
                    symbol=symbol,
                    broker_value=f"{side.value} {entry.quantity}",
                )
            )
        return len(checked)

    def _compare_open_trades(
        self,
        exposure: dict,
        open_trades: tuple[TradeInfo, ...],
        excluded: set[str],
        add,
    ) -> set[str]:
        """Match every broker open trade to at most one internal order by ID
        evidence; classify the unmatched and the ambiguous; run the
        per-trade SL/TP checks for unique matches. Returns the ids of
        internal orders that have at least one open trade."""
        observed_at = self._now()
        matched_order_ids: set[str] = set()
        for trade in sorted(open_trades, key=lambda t: t.trade_id):
            candidates = self._match_order_candidates(
                exposure, trade.client_order_id, trade.trade_id
            )
            if len(candidates) == 1:
                info = candidates[0]
                conflict = self._matched_trade_identity_conflict(
                    info["row"], trade.symbol, trade.side
                )
                if conflict is not None:
                    # ID evidence points at this order, but the trade's own
                    # identity contradicts it (PR #108 re-review): NOT a
                    # unique match. Nothing is derived from it -- no SL/TP
                    # comparison against the wrong instrument, no
                    # internal-closed finding, no broker-only claim.
                    add(
                        PositionDiscrepancy(
                            discrepancy_type=DiscrepancyType.TRADE_MATCH_AMBIGUOUS,
                            target_type=ReconciliationTargetType.BROKER_TRADE,
                            target_id=trade.trade_id,
                            resolution_category=ResolutionCategory.MANUAL_REVIEW,
                            observed_at_utc=observed_at,
                            detail=(
                                f"broker trade {trade.trade_id} matches internal order "
                                f"{info['row']['execution_request_id']} by ID evidence but "
                                f"contradicts its identity ({conflict}); contradictory "
                                "evidence is never resolved by guessing"
                            ),
                            symbol=trade.symbol,
                            broker_value=f"open {trade.side.value} {trade.quantity}",
                            client_order_id=trade.client_order_id,
                            broker_trade_id=trade.trade_id,
                        )
                    )
                    continue
                matched_order_ids.add(info["row"]["execution_request_id"])
                self._check_matched_open_trade(info, trade, observed_at, add)
                continue
            if len(candidates) > 1:
                candidate_ids = sorted(
                    info["row"]["execution_request_id"] for info in candidates
                )
                add(
                    PositionDiscrepancy(
                        discrepancy_type=DiscrepancyType.TRADE_MATCH_AMBIGUOUS,
                        target_type=ReconciliationTargetType.BROKER_TRADE,
                        target_id=trade.trade_id,
                        resolution_category=ResolutionCategory.MANUAL_REVIEW,
                        observed_at_utc=observed_at,
                        detail=(
                            f"broker trade {trade.trade_id} matches more than one internal "
                            f"order by ID evidence ({', '.join(candidate_ids)}); an ambiguous "
                            "match is never resolved by guessing"
                        ),
                        symbol=trade.symbol,
                        broker_value=f"open {trade.side.value} {trade.quantity}",
                        client_order_id=trade.client_order_id,
                        broker_trade_id=trade.trade_id,
                    )
                )
                continue
            if trade.symbol in excluded:
                # The symbol has internal orders whose outcome/fills are
                # unresolved: this trade may belong to one of them, so it is
                # NOT classified broker-only.
                add(
                    PositionDiscrepancy(
                        discrepancy_type=DiscrepancyType.TRADE_MATCH_AMBIGUOUS,
                        target_type=ReconciliationTargetType.BROKER_TRADE,
                        target_id=trade.trade_id,
                        resolution_category=ResolutionCategory.MANUAL_REVIEW,
                        observed_at_utc=observed_at,
                        detail=(
                            f"broker trade {trade.trade_id} matches no internal order by ID "
                            f"evidence, but {trade.symbol} has internal orders with "
                            "unresolved outcomes (UNKNOWN state or unreadable snapshot); "
                            "the trade may belong to one of them, so it is held for manual "
                            "review instead of being classified broker-only"
                        ),
                        symbol=trade.symbol,
                        broker_value=f"open {trade.side.value} {trade.quantity}",
                        client_order_id=trade.client_order_id,
                        broker_trade_id=trade.trade_id,
                    )
                )
                continue
            add(
                PositionDiscrepancy(
                    discrepancy_type=DiscrepancyType.BROKER_ONLY_TRADE,
                    target_type=ReconciliationTargetType.BROKER_TRADE,
                    target_id=trade.trade_id,
                    resolution_category=ResolutionCategory.MANUAL_REVIEW,
                    observed_at_utc=observed_at,
                    detail=(
                        f"the broker holds open trade {trade.trade_id} on {trade.symbol} "
                        "that no internal order accounts for (no client_order_id match, no "
                        "recorded fill id match); adopting a broker-side trade as an "
                        "internal one is never automatic"
                    ),
                    symbol=trade.symbol,
                    broker_value=f"open {trade.side.value} {trade.quantity}",
                    client_order_id=trade.client_order_id,
                    broker_trade_id=trade.trade_id,
                )
            )
        return matched_order_ids

    def _check_matched_open_trade(
        self, info: dict, trade: TradeInfo, observed_at: datetime, add
    ) -> None:
        """Per-trade checks for a UNIQUELY matched internal order: internal
        terminal-without-fill vs. an open trade, then the SL/TP intent
        comparison. Nothing here mutates anything anywhere."""
        row = info["row"]
        state: OrderState = info["state"]
        filled = info["filled"]

        internally_closed = state is OrderState.REJECTED or (
            state is OrderState.CANCELLED and filled is not None and filled == 0
        )
        if internally_closed:
            add(
                PositionDiscrepancy(
                    discrepancy_type=DiscrepancyType.INTERNAL_CLOSED_BROKER_OPEN,
                    target_type=ReconciliationTargetType.EXECUTION_ORDER,
                    target_id=row["execution_request_id"],
                    resolution_category=ResolutionCategory.MANUAL_REVIEW,
                    observed_at_utc=observed_at,
                    detail=(
                        f"BELIAL records this order as {state.value} with no fills, but the "
                        f"broker holds open trade {trade.trade_id} bound to it by ID "
                        "evidence; the broker-side trade is never closed automatically and "
                        "the internal record is not changed"
                    ),
                    symbol=row["symbol"],
                    internal_value=state.value,
                    broker_value=f"open {trade.side.value} {trade.quantity}",
                    execution_request_id=row["execution_request_id"],
                    client_order_id=row["client_order_id"],
                    broker_trade_id=trade.trade_id,
                )
            )
            return

        self._check_protection(
            info,
            trade,
            observed_at,
            add,
            intent_text=row["stop_loss_price"],
            observed=trade.stop_loss,
            missing_type=DiscrepancyType.STOP_LOSS_MISSING,
            mismatch_type=DiscrepancyType.STOP_LOSS_PRICE_MISMATCH,
            reference_type=DiscrepancyType.STOP_LOSS_REFERENCE_MISMATCH,
            unexpected_type=DiscrepancyType.STOP_LOSS_UNEXPECTED,
            label="stop loss",
        )
        self._check_protection(
            info,
            trade,
            observed_at,
            add,
            intent_text=row["take_profit_price"],
            observed=trade.take_profit,
            missing_type=DiscrepancyType.TAKE_PROFIT_MISSING,
            mismatch_type=DiscrepancyType.TAKE_PROFIT_PRICE_MISMATCH,
            reference_type=DiscrepancyType.TAKE_PROFIT_REFERENCE_MISMATCH,
            unexpected_type=DiscrepancyType.TAKE_PROFIT_UNEXPECTED,
            label="take profit",
        )

    def _check_protection(
        self,
        info: dict,
        trade: TradeInfo,
        observed_at: datetime,
        add,
        *,
        intent_text: str | None,
        observed,
        missing_type: DiscrepancyType,
        mismatch_type: DiscrepancyType,
        reference_type: DiscrepancyType,
        unexpected_type: DiscrepancyType,
        label: str,
    ) -> None:
        """Compare one protection intent (the execution order's stored
        SL/TP price) against the broker-reported protection order.

        Prices are compared as exact ``Decimal`` values (the repository's
        existing price convention: canonical Decimal strings, no float, no
        rounding tolerance introduced here). Detection only -- a missing or
        differing protection order is NEVER re-created or modified.
        """
        row = info["row"]
        intent = None if intent_text is None else Decimal(intent_text)
        common = dict(
            target_type=ReconciliationTargetType.EXECUTION_ORDER,
            target_id=row["execution_request_id"],
            resolution_category=ResolutionCategory.MANUAL_REVIEW,
            observed_at_utc=observed_at,
            symbol=row["symbol"],
            execution_request_id=row["execution_request_id"],
            client_order_id=row["client_order_id"],
            broker_trade_id=trade.trade_id,
        )
        if intent is not None and observed is None:
            add(
                PositionDiscrepancy(
                    discrepancy_type=missing_type,
                    detail=(
                        f"the execution request intends a {label} at {intent}, but broker "
                        f"trade {trade.trade_id} carries no {label} order; the protection "
                        "order is never created automatically"
                    ),
                    internal_value=str(intent),
                    **common,
                )
            )
            return
        if intent is None and observed is not None:
            add(
                PositionDiscrepancy(
                    discrepancy_type=unexpected_type,
                    detail=(
                        f"broker trade {trade.trade_id} carries a {label} order at "
                        f"{observed.price} that the execution request never intended; the "
                        "protection order is never cancelled or changed automatically"
                    ),
                    broker_value=str(observed.price),
                    protection_order_id=observed.order_id,
                    **common,
                )
            )
            return
        if intent is None and observed is None:
            return
        if observed.price != intent:
            add(
                PositionDiscrepancy(
                    discrepancy_type=mismatch_type,
                    detail=(
                        f"the execution request intends a {label} at {intent}, but broker "
                        f"trade {trade.trade_id} carries {label} order {observed.order_id} "
                        f"at {observed.price} (exact Decimal comparison); the protection "
                        "order is never modified automatically"
                    ),
                    internal_value=str(intent),
                    broker_value=str(observed.price),
                    protection_order_id=observed.order_id,
                    **common,
                )
            )
        if observed.trade_id is not None and observed.trade_id != trade.trade_id:
            add(
                PositionDiscrepancy(
                    discrepancy_type=reference_type,
                    detail=(
                        f"{label} order {observed.order_id} attached to broker trade "
                        f"{trade.trade_id} references trade {observed.trade_id} instead; "
                        "the reference is never rewired automatically"
                    ),
                    internal_value=trade.trade_id,
                    broker_value=observed.trade_id,
                    protection_order_id=observed.order_id,
                    **common,
                )
            )

    def _compare_closed_trades(
        self,
        exposure: dict,
        closed_trades: tuple[ClosedTradeInfo, ...],
        open_trade_ids: set[str],
        open_matched_order_ids: set[str],
        excluded: set[str],
        add,
    ) -> None:
        """Use broker closed-trade / transaction evidence to detect "closed
        on the broker while BELIAL still expects the exposure".

        Emitted only when ALL of these hold for a uniquely matched order:
        the symbol's expectation is determinate, the order contributed
        exposure, no open trade is bound to the order, and the symbol still
        nets to an expected exposure in the order's direction (an internal
        offsetting order that closed the exposure inside BELIAL nets it to
        zero -- then the broker-side close is EXPECTED and nothing is
        flagged). Absence from the closed listing is never read as "not
        closed", and unmatched closed trades are historical broker facts
        outside Issue #105's scope."""
        observed_at = self._now()
        net = exposure["net"]
        for closed in sorted(closed_trades, key=lambda t: t.trade_id):
            if closed.trade_id in open_trade_ids:
                continue  # still open broker-side; not close evidence
            candidates = self._match_order_candidates(
                exposure, closed.client_order_id, closed.trade_id
            )
            if len(candidates) > 1:
                candidate_ids = sorted(
                    info["row"]["execution_request_id"] for info in candidates
                )
                add(
                    PositionDiscrepancy(
                        discrepancy_type=DiscrepancyType.TRADE_MATCH_AMBIGUOUS,
                        target_type=ReconciliationTargetType.BROKER_TRADE,
                        target_id=closed.trade_id,
                        resolution_category=ResolutionCategory.MANUAL_REVIEW,
                        observed_at_utc=observed_at,
                        detail=(
                            f"broker closed trade {closed.trade_id} matches more than one "
                            f"internal order by ID evidence ({', '.join(candidate_ids)}); "
                            "an ambiguous match is never resolved by guessing"
                        ),
                        symbol=closed.symbol,
                        broker_value=f"closed {closed.side.value} {closed.quantity}",
                        client_order_id=closed.client_order_id,
                        broker_trade_id=closed.trade_id,
                    )
                )
                continue
            if not candidates:
                continue
            info = candidates[0]
            row = info["row"]
            request_id = row["execution_request_id"]
            symbol = row["symbol"]
            filled = info["filled"]
            conflict = self._matched_trade_identity_conflict(
                row, closed.symbol, closed.side
            )
            if conflict is not None:
                # ID evidence points at this order, but the closed trade's own
                # identity contradicts it (PR #108 re-review): never accepted
                # as close evidence -- a cross-symbol or cross-side record
                # must not produce a false close confirmation, and a silent
                # skip would hide the contradiction.
                add(
                    PositionDiscrepancy(
                        discrepancy_type=DiscrepancyType.TRADE_MATCH_AMBIGUOUS,
                        target_type=ReconciliationTargetType.BROKER_TRADE,
                        target_id=closed.trade_id,
                        resolution_category=ResolutionCategory.MANUAL_REVIEW,
                        observed_at_utc=observed_at,
                        detail=(
                            f"broker closed trade {closed.trade_id} matches internal "
                            f"order {request_id} by ID evidence but contradicts its "
                            f"identity ({conflict}); contradictory evidence is never "
                            "accepted as close evidence"
                        ),
                        symbol=closed.symbol,
                        broker_value=f"closed {closed.side.value} {closed.quantity}",
                        client_order_id=closed.client_order_id,
                        broker_trade_id=closed.trade_id,
                    )
                )
                continue
            if symbol in excluded or filled is None or filled == 0:
                continue
            if request_id in open_matched_order_ids:
                continue  # the order still has an open trade broker-side
            order_direction = (
                PositionSide.LONG
                if row["side"] == OrderSide.BUY.value
                else PositionSide.SHORT
            )
            symbol_net = net.get(symbol, Decimal(0))
            expected_side = (
                PositionSide.LONG
                if symbol_net > 0
                else PositionSide.SHORT
                if symbol_net < 0
                else None
            )
            if expected_side is not order_direction:
                continue  # BELIAL already nets this exposure to zero (or opposite)
            transaction_note = (
                f" (closing transaction(s): {', '.join(closed.closing_transaction_ids)})"
                if closed.closing_transaction_ids
                else ""
            )
            add(
                PositionDiscrepancy(
                    discrepancy_type=DiscrepancyType.BROKER_CLOSED_INTERNAL_OPEN,
                    target_type=ReconciliationTargetType.EXECUTION_ORDER,
                    target_id=request_id,
                    resolution_category=ResolutionCategory.MANUAL_REVIEW,
                    observed_at_utc=observed_at,
                    detail=(
                        f"the broker closed trade {closed.trade_id} bound to this order by "
                        f"ID evidence{transaction_note}, but BELIAL still expects a "
                        f"{order_direction.value} exposure of {abs(symbol_net)} on {symbol}; "
                        "the internal records are NOT changed and the broker-side fact is "
                        "surfaced for manual review"
                    ),
                    symbol=symbol,
                    internal_value=f"open {order_direction.value} {abs(symbol_net)}",
                    broker_value=f"closed {closed.side.value} {closed.quantity}",
                    execution_request_id=request_id,
                    client_order_id=row["client_order_id"],
                    broker_trade_id=closed.trade_id,
                    broker_transaction_ids=closed.closing_transaction_ids,
                )
            )

    @staticmethod
    def _matched_trade_identity_conflict(
        row: dict, trade_symbol: str, trade_side: PositionSide
    ) -> str | None:
        """Whether a trade matched by ID evidence CONTRADICTS the matched
        order's own identity (PR #108 re-review).

        A trade opened by an order shares the order's instrument and
        direction (BUY -> long, SELL -> short). A matching client id / fill
        id combined with a different symbol or an opposite side is
        contradictory evidence -- a malformed response or a stale / reused
        identifier -- and must never be treated as a unique match: no close
        confirmation, no SL/TP comparison against the wrong instrument.
        Returns the human-readable contradiction, or None when coherent."""
        problems: list[str] = []
        if trade_symbol != row["symbol"]:
            problems.append(
                f"trade symbol {trade_symbol!r} != order symbol {row['symbol']!r}"
            )
        order_direction = (
            PositionSide.LONG if row["side"] == OrderSide.BUY.value else PositionSide.SHORT
        )
        if trade_side is not order_direction:
            problems.append(
                f"trade side {trade_side.value!r} contradicts the order's "
                f"{order_direction.value!r} direction"
            )
        if not problems:
            return None
        return "; ".join(problems)

    @staticmethod
    def _match_order_candidates(
        exposure: dict, client_order_id: str | None, trade_id: str
    ) -> list[dict]:
        """The internal orders a broker trade matches by ID evidence: the
        trade's broker-reported client_order_id, or the trade id appearing
        among an order's recorded fill ids. Exact-ID only; returns every
        candidate so the caller can distinguish unique / none / ambiguous."""
        candidates: dict[str, dict] = {}
        if client_order_id is not None:
            info = exposure["by_client"].get(client_order_id)
            if info is not None:
                candidates[info["row"]["execution_request_id"]] = info
        for info in exposure["by_fill"].get(trade_id, ()):
            candidates[info["row"]["execution_request_id"]] = info
        return [candidates[key] for key in sorted(candidates)]

    def _list_broker_positions(
        self,
    ) -> tuple[tuple[PositionInfo, ...], str | None]:
        """``(positions, error)``; the error marks the position comparison as
        "not performed" (fail-closed), mirroring ``_list_broker_orders``."""
        try:
            positions = self._broker.get_positions()
            if not isinstance(positions, tuple):
                raise TypeError(
                    "Broker.get_positions() must return a tuple, got "
                    f"{type(positions).__name__}"
                )
            for entry in positions:
                if not isinstance(entry, PositionInfo):
                    raise TypeError(
                        "Broker.get_positions() entries must be PositionInfo, got "
                        f"{type(entry).__name__}"
                    )
            keys = [(entry.symbol, entry.side) for entry in positions]
            duplicates = sorted(
                f"{symbol}:{side.value}"
                for (symbol, side), count in Counter(keys).items()
                if count > 1
            )
            if duplicates:
                return (
                    (),
                    "broker position enumeration is ambiguous: duplicate (symbol, side) "
                    f"entries {duplicates!r}; positions were NOT checked",
                )
        except BrokerError as exc:
            return (
                (),
                f"listing broker positions failed ({type(exc).__name__}: {exc}); "
                "positions were NOT checked",
            )
        except Exception as exc:
            return (
                (),
                "listing broker positions violated the Broker error/return contract "
                f"({type(exc).__name__}); positions were NOT checked",
            )
        return positions, None

    def _list_broker_open_trades(
        self,
    ) -> tuple[tuple[TradeInfo, ...], str | None]:
        """``(open trades, error)``; the optional ``TradeListingBroker``
        capability, handled exactly like ``OrderListingBroker``."""
        if not isinstance(self._broker, TradeListingBroker):
            return (
                (),
                f"{type(self._broker).__name__} does not implement TradeListingBroker; "
                "open trades and SL/TP state were NOT checked",
            )
        try:
            trades = self._broker.get_open_trades()
            if not isinstance(trades, tuple):
                raise TypeError(
                    "TradeListingBroker.get_open_trades() must return a tuple, got "
                    f"{type(trades).__name__}"
                )
            for entry in trades:
                if not isinstance(entry, TradeInfo):
                    raise TypeError(
                        "TradeListingBroker.get_open_trades() entries must be TradeInfo, "
                        f"got {type(entry).__name__}"
                    )
            duplicate_ids = sorted(
                trade_id
                for trade_id, count in Counter(t.trade_id for t in trades).items()
                if count > 1
            )
            if duplicate_ids:
                return (
                    (),
                    "broker open-trade enumeration is ambiguous: duplicate trade ids "
                    f"{duplicate_ids!r}; open trades and SL/TP state were NOT checked",
                )
        except BrokerError as exc:
            return (
                (),
                f"listing broker open trades failed ({type(exc).__name__}: {exc}); open "
                "trades and SL/TP state were NOT checked",
            )
        except Exception as exc:
            return (
                (),
                "listing broker open trades violated the Broker error/return contract "
                f"({type(exc).__name__}); open trades and SL/TP state were NOT checked",
            )
        return trades, None

    def _list_broker_closed_trades(
        self, *, open_trades_available: bool
    ) -> tuple[tuple[ClosedTradeInfo, ...], str | None]:
        """``(closed trades, error)``; the optional
        ``ClosedTradeListingBroker`` capability. Requires the OPEN trade
        listing: without it, "no longer open" cannot be established and any
        close conclusion would be a guess."""
        if not isinstance(self._broker, ClosedTradeListingBroker):
            return (
                (),
                f"{type(self._broker).__name__} does not implement "
                "ClosedTradeListingBroker; closed-trade evidence was NOT checked",
            )
        if not open_trades_available:
            return (
                (),
                "closed-trade evidence was NOT checked: the open-trade listing is "
                "unavailable, so 'no longer open broker-side' cannot be established",
            )
        try:
            trades = self._broker.get_closed_trades()
            if not isinstance(trades, tuple):
                raise TypeError(
                    "ClosedTradeListingBroker.get_closed_trades() must return a tuple, "
                    f"got {type(trades).__name__}"
                )
            for entry in trades:
                if not isinstance(entry, ClosedTradeInfo):
                    raise TypeError(
                        "ClosedTradeListingBroker.get_closed_trades() entries must be "
                        f"ClosedTradeInfo, got {type(entry).__name__}"
                    )
            duplicate_ids = sorted(
                trade_id
                for trade_id, count in Counter(t.trade_id for t in trades).items()
                if count > 1
            )
            if duplicate_ids:
                return (
                    (),
                    "broker closed-trade enumeration is ambiguous: duplicate trade ids "
                    f"{duplicate_ids!r}; closed-trade evidence was NOT checked",
                )
        except BrokerError as exc:
            return (
                (),
                f"listing broker closed trades failed ({type(exc).__name__}: {exc}); "
                "closed-trade evidence was NOT checked",
            )
        except Exception as exc:
            return (
                (),
                "listing broker closed trades violated the Broker error/return contract "
                f"({type(exc).__name__}); closed-trade evidence was NOT checked",
            )
        return trades, None

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
            # the order's revision at observation (Issue #104): repair
            # refuses to act on this observation once the order changed
            internal_revision=row["revision"],
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
        for discrepancy in run.position_discrepancies:
            _LOGGER.warning(
                "reconciliation position-domain discrepancy: run_id=%s type=%s "
                "target_type=%s target_id=%s symbol=%s internal=%s broker=%s "
                "execution_request_id=%s client_order_id=%s broker_trade_id=%s "
                "protection_order_id=%s broker_transaction_ids=%s resolution=%s detail=%s",
                run.run_id,
                discrepancy.discrepancy_type.value,
                discrepancy.target_type.value,
                discrepancy.target_id,
                discrepancy.symbol,
                discrepancy.internal_value,
                discrepancy.broker_value,
                discrepancy.execution_request_id,
                discrepancy.client_order_id,
                discrepancy.broker_trade_id,
                discrepancy.protection_order_id,
                list(discrepancy.broker_transaction_ids),
                discrepancy.resolution_category.value,
                discrepancy.detail,
            )
        if run.broker_listing_error is not None:
            _LOGGER.warning(
                "reconciliation run %s could not enumerate broker orders: %s",
                run.run_id,
                run.broker_listing_error,
            )
        summary = run.position_summary
        if summary is not None:
            for label, error in (
                ("derive internal exposure", summary.internal_exposure_error),
                ("enumerate broker positions", summary.position_listing_error),
                ("enumerate broker open trades", summary.trade_listing_error),
                ("enumerate broker closed trades", summary.closed_trade_listing_error),
            ):
                if error is not None:
                    _LOGGER.warning(
                        "reconciliation run %s could not %s: %s", run.run_id, label, error
                    )
            if summary.excluded_symbols:
                _LOGGER.warning(
                    "reconciliation run %s excluded symbols from the exposure comparison "
                    "(unresolved internal outcomes, nothing inferred): %s",
                    run.run_id,
                    list(summary.excluded_symbols),
                )
            _LOGGER.info(
                "reconciliation run %s position scope: checked_positions=%d "
                "checked_trades=%d checked_closed_trades=%d position_discrepancies=%d "
                "(all manual_review by construction)",
                run.run_id,
                summary.checked_position_count,
                summary.checked_trade_count,
                summary.checked_closed_trade_count,
                len(run.position_discrepancies),
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
