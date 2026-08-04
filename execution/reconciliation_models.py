"""Broker reconciliation common models (Issue #103).

*** DETECTION ONLY. Nothing in this module -- or in the service that builds
these records -- changes an internal order state, submits an order or cancels
one. A reconciliation run OBSERVES and CLASSIFIES; repairing what it finds is
Issue #104. ***

Vocabulary
----------

- :class:`ReconciliationRun` -- one comparison pass over BELIAL's in-flight
  orders and the broker's order snapshots.
- :class:`OrderDiscrepancy` -- one classified difference: what kind
  (:class:`DiscrepancyType`), about what (:class:`ReconciliationTargetType` +
  ``target_id``), what each side reported (``internal_state`` /
  ``broker_state``), how it should be handled
  (:class:`ResolutionCategory`), when it was observed and WHY it was
  classified that way (``detail``).

Both classification axes are Enums, never free strings: a discrepancy kind
and a resolution category are compared and persisted machine-readably (the
store's CHECK constraints mirror the members).

Reused, not redefined (Issue #74 / #73 contract): order states are
``broker.broker_models.OrderState`` and query failures carry the existing
``BrokerErrorCode``. This module introduces NO order state, NO transition
table and NO parallel error taxonomy.

Query failures (Issue #103): a failed broker query is recorded as a
``QUERY_FAILURE`` discrepancy with ``ResolutionCategory.QUERY_FAILED``, the
target id and the typed error code. It is deliberately NOT a run-level
abort: one unreachable order must not hide the observations that DID
succeed, and the failing target stays individually auditable. The one
failure that cannot be attributed to a single target -- the broker-side
enumeration -- is recorded on the RUN as ``broker_listing_error``, which
marks the broker-only check as "not performed" instead of "found nothing".

Validation follows the execution layer's style (``ValueError`` on invariant
violations, ``Decimal``-free here, timezone-aware UTC datetimes only).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime

from broker.broker_models import BrokerErrorCode, OrderState
from execution.execution_models import _require_non_empty_str, _require_utc


class ReconciliationMode(enum.Enum):
    """What a run was ALLOWED to do. Issue #103 defines exactly one mode.

    ``DETECTION_ONLY`` -- compare and record; never write to the execution
    order store, never call a broker write operation. The mode is persisted
    with every run so an audit can tell what a stored run was permitted to
    do without inferring it from the code version.
    """

    DETECTION_ONLY = "detection_only"


class ReconciliationTargetType(enum.Enum):
    """What a discrepancy is ABOUT (which id space ``target_id`` lives in).

    ``EXECUTION_ORDER`` -- ``target_id`` is an ``execution_request_id``: a
    BELIAL-internal order intent (Issue #74's primary key).
    ``BROKER_ORDER``    -- ``target_id`` is a ``client_order_id`` observed on
    the broker side with no matching execution request; there is no internal
    id to name.
    """

    EXECUTION_ORDER = "execution_order"
    BROKER_ORDER = "broker_order"


class DiscrepancyType(enum.Enum):
    """The KIND of difference observed between BELIAL and the broker.

    ``IN_FLIGHT_BROKER_FILLED`` / ``_CANCELLED`` / ``_REJECTED`` -- BELIAL
    still tracks the order as working (pending / accepted / partially
    filled) while the broker reports a terminal outcome.
    ``UNKNOWN_BROKER_SETTLED`` -- BELIAL tracks UNKNOWN (an outcome that was
    never confirmed) while the broker reports a settled state.
    ``BROKER_ONLY_ORDER`` -- the broker holds an order BELIAL has no
    execution request for.
    ``BROKER_ORDER_MISSING`` -- BELIAL tracks a submitted order the broker
    does not know (``BrokerOrderNotFoundError``). NOT the same as "never
    placed": the query answered about an id, not about our submission
    history, so this is always a human decision.
    ``STATE_MISMATCH`` -- the two sides disagree in a way none of the above
    describes (e.g. two conflicting terminal states).
    ``QUERY_FAILURE`` -- the broker could not be asked at all (timeout,
    communication failure, auth, rate limit). No state is inferred.
    """

    IN_FLIGHT_BROKER_FILLED = "in_flight_broker_filled"
    IN_FLIGHT_BROKER_CANCELLED = "in_flight_broker_cancelled"
    IN_FLIGHT_BROKER_REJECTED = "in_flight_broker_rejected"
    UNKNOWN_BROKER_SETTLED = "unknown_broker_settled"
    BROKER_ONLY_ORDER = "broker_only_order"
    BROKER_ORDER_MISSING = "broker_order_missing"
    STATE_MISMATCH = "state_mismatch"
    QUERY_FAILURE = "query_failure"


class ResolutionCategory(enum.Enum):
    """How a discrepancy has to be handled -- classification ONLY.

    ``AUTO_REPAIR_CANDIDATE`` -- the broker observation could be reflected
    through the EXISTING order state machine and store contract (Issue #74)
    without losing information. Issue #103 does not repair it; Issue #104
    decides whether and how to.
    ``MANUAL_REVIEW`` -- a human has to look: the observation cannot be
    reflected by the existing state machine / store contract, contradicts a
    settled outcome, or concerns an order only one side knows about.
    ``QUERY_FAILED`` -- the comparison could not be made because the broker
    query failed. NOTHING is concluded about the order's state.
    """

    AUTO_REPAIR_CANDIDATE = "auto_repair_candidate"
    MANUAL_REVIEW = "manual_review"
    QUERY_FAILED = "query_failed"


# Per-kind shape rules: (target type, internal state required, broker state
# required). They keep a record from claiming something it cannot know -- a
# QUERY_FAILURE can never carry a broker state, a BROKER_ONLY_ORDER can never
# carry an internal one.
_TYPE_RULES: dict[DiscrepancyType, tuple[ReconciliationTargetType, bool, bool]] = {
    DiscrepancyType.IN_FLIGHT_BROKER_FILLED: (ReconciliationTargetType.EXECUTION_ORDER, True, True),
    DiscrepancyType.IN_FLIGHT_BROKER_CANCELLED: (
        ReconciliationTargetType.EXECUTION_ORDER,
        True,
        True,
    ),
    DiscrepancyType.IN_FLIGHT_BROKER_REJECTED: (
        ReconciliationTargetType.EXECUTION_ORDER,
        True,
        True,
    ),
    DiscrepancyType.UNKNOWN_BROKER_SETTLED: (ReconciliationTargetType.EXECUTION_ORDER, True, True),
    DiscrepancyType.STATE_MISMATCH: (ReconciliationTargetType.EXECUTION_ORDER, True, True),
    DiscrepancyType.BROKER_ORDER_MISSING: (ReconciliationTargetType.EXECUTION_ORDER, True, False),
    DiscrepancyType.QUERY_FAILURE: (ReconciliationTargetType.EXECUTION_ORDER, True, False),
    DiscrepancyType.BROKER_ONLY_ORDER: (ReconciliationTargetType.BROKER_ORDER, False, True),
}


@dataclass(frozen=True)
class OrderDiscrepancy:
    """One classified difference between BELIAL and the broker (Issue #103).

    ``detail`` is the human-readable REASON for the classification (why a
    discrepancy is auto-repairable or needs review, or which broker error
    occurred). It is mandatory: a stored discrepancy without a reason cannot
    be acted on later.

    Invariants (all enforced):

    - every enum field holds a member of its enum; ``target_id`` /
      ``detail`` are non-empty; ``observed_at_utc`` is timezone-aware UTC,
    - the kind fixes the target type and which observed states must / must
      not be present (see ``_TYPE_RULES``),
    - ``QUERY_FAILED`` is used exactly for ``QUERY_FAILURE`` (and then a
      ``broker_error_code`` is required); every other kind must not carry an
      error code,
    - ``QUERY_FAILURE`` never carries a broker state -- a failed query
      observes nothing.
    """

    discrepancy_type: DiscrepancyType
    target_type: ReconciliationTargetType
    target_id: str
    resolution_category: ResolutionCategory
    observed_at_utc: datetime
    detail: str
    internal_state: OrderState | None = None
    broker_state: OrderState | None = None
    client_order_id: str | None = None
    broker_order_id: str | None = None
    broker_error_code: BrokerErrorCode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.discrepancy_type, DiscrepancyType):
            raise ValueError(
                f"discrepancy_type must be a DiscrepancyType, got {self.discrepancy_type!r}"
            )
        if not isinstance(self.target_type, ReconciliationTargetType):
            raise ValueError(
                f"target_type must be a ReconciliationTargetType, got {self.target_type!r}"
            )
        if not isinstance(self.resolution_category, ResolutionCategory):
            raise ValueError(
                f"resolution_category must be a ResolutionCategory, got "
                f"{self.resolution_category!r}"
            )
        _require_non_empty_str(self.target_id, "target_id")
        _require_non_empty_str(self.detail, "detail")
        _require_utc(self.observed_at_utc, "observed_at_utc")
        for name in ("internal_state", "broker_state"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, OrderState):
                raise ValueError(f"{name} must be an OrderState or None, got {value!r}")
        for name in ("client_order_id", "broker_order_id"):
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_str(value, name)
        if self.broker_error_code is not None and not isinstance(
            self.broker_error_code, BrokerErrorCode
        ):
            raise ValueError(
                f"broker_error_code must be a BrokerErrorCode or None, got "
                f"{self.broker_error_code!r}"
            )

        expected_target, internal_required, broker_required = _TYPE_RULES[self.discrepancy_type]
        if self.target_type is not expected_target:
            raise ValueError(
                f"{self.discrepancy_type.value!r} targets {expected_target.value!r}, got "
                f"{self.target_type.value!r}"
            )
        if internal_required and self.internal_state is None:
            raise ValueError(
                f"{self.discrepancy_type.value!r} requires the observed internal_state"
            )
        if not internal_required and self.internal_state is not None:
            raise ValueError(
                f"{self.discrepancy_type.value!r} has no internal order, so internal_state "
                f"must be None, got {self.internal_state!r}"
            )
        if broker_required and self.broker_state is None:
            raise ValueError(f"{self.discrepancy_type.value!r} requires the observed broker_state")
        if not broker_required and self.broker_state is not None:
            raise ValueError(
                f"{self.discrepancy_type.value!r} observed no broker state, so broker_state "
                f"must be None, got {self.broker_state!r}"
            )

        is_query_failure = self.discrepancy_type is DiscrepancyType.QUERY_FAILURE
        if is_query_failure != (self.resolution_category is ResolutionCategory.QUERY_FAILED):
            raise ValueError(
                "resolution_category 'query_failed' is used exactly for the "
                "'query_failure' discrepancy type"
            )
        if is_query_failure and self.broker_error_code is None:
            raise ValueError("a query_failure discrepancy requires the broker_error_code")
        if not is_query_failure and self.broker_error_code is not None:
            raise ValueError(
                f"broker_error_code belongs to a query_failure only, got "
                f"{self.broker_error_code!r} on {self.discrepancy_type.value!r}"
            )


@dataclass(frozen=True)
class ReconciliationRun:
    """The result of one reconciliation pass (Issue #103).

    ``checked_order_count`` counts the internal in-flight orders the run
    compared (matched ones included), so "0 discrepancies" can be told apart
    from "nothing was looked at".

    ``broker_listing_error`` records why the broker-side order ENUMERATION
    did not happen (query failure, or a broker that does not implement
    ``OrderListingBroker``). While it is set, the absence of
    ``BROKER_ONLY_ORDER`` records means "not checked", NOT "none exist" --
    :attr:`broker_orders_enumerated` is the machine-readable form of that
    distinction.
    """

    run_id: str
    mode: ReconciliationMode
    started_at_utc: datetime
    finished_at_utc: datetime
    checked_order_count: int
    discrepancies: tuple[OrderDiscrepancy, ...] = field(default_factory=tuple)
    broker_listing_error: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.run_id, "run_id")
        if not isinstance(self.mode, ReconciliationMode):
            raise ValueError(f"mode must be a ReconciliationMode, got {self.mode!r}")
        _require_utc(self.started_at_utc, "started_at_utc")
        _require_utc(self.finished_at_utc, "finished_at_utc")
        if self.finished_at_utc < self.started_at_utc:
            raise ValueError(
                f"finished_at_utc must not precede started_at_utc: "
                f"{self.started_at_utc.isoformat()} -> {self.finished_at_utc.isoformat()}"
            )
        if isinstance(self.checked_order_count, bool) or not isinstance(
            self.checked_order_count, int
        ):
            raise ValueError(
                f"checked_order_count must be an int, got {self.checked_order_count!r}"
            )
        if self.checked_order_count < 0:
            raise ValueError(
                f"checked_order_count must be >= 0, got {self.checked_order_count}"
            )
        if not isinstance(self.discrepancies, tuple):
            raise ValueError(
                f"discrepancies must be a tuple, got {type(self.discrepancies).__name__}"
            )
        for entry in self.discrepancies:
            if not isinstance(entry, OrderDiscrepancy):
                raise ValueError(
                    f"every discrepancies entry must be an OrderDiscrepancy, got {entry!r}"
                )
        targets = [(entry.target_type, entry.target_id) for entry in self.discrepancies]
        if len(set(targets)) != len(targets):
            raise ValueError(
                "a reconciliation run may record at most one discrepancy per "
                "(target_type, target_id)"
            )
        if self.broker_listing_error is not None:
            _require_non_empty_str(self.broker_listing_error, "broker_listing_error")

    @property
    def broker_orders_enumerated(self) -> bool:
        """True when the broker-side order listing actually happened -- i.e.
        when "no ``BROKER_ONLY_ORDER`` records" may be read as "none exist"."""
        return self.broker_listing_error is None

    @property
    def has_partial_failures(self) -> bool:
        """Whether any portion of this run could not be checked reliably.

        Per-order query failures and a failed/unsupported broker enumeration
        are both partial failures: successful observations remain valid, but
        callers must not present the run as a complete clean reconciliation.
        """
        return self.broker_listing_error is not None or any(
            discrepancy.discrepancy_type is DiscrepancyType.QUERY_FAILURE
            for discrepancy in self.discrepancies
        )

    def by_resolution(self, category: ResolutionCategory) -> tuple[OrderDiscrepancy, ...]:
        """The discrepancies in one resolution category, in observation order."""
        if not isinstance(category, ResolutionCategory):
            raise ValueError(f"category must be a ResolutionCategory, got {category!r}")
        return tuple(d for d in self.discrepancies if d.resolution_category is category)

    def resolution_counts(self) -> dict[ResolutionCategory, int]:
        """Discrepancy count per resolution category (every member present)."""
        counts = {category: 0 for category in ResolutionCategory}
        for discrepancy in self.discrepancies:
            counts[discrepancy.resolution_category] += 1
        return counts
