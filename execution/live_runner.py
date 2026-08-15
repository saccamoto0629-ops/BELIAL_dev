"""Live Execution runner core (Issue #131) -- the LIFECYCLE around the
existing submission / shadow boundaries. Fake and Shadow only.

*** THIS MODULE NEVER TALKS TO A BROKER. It holds no ``Broker`` /
``BrokerConnection``, imports neither, and calls neither ``submit_order``
nor ``cancel_order`` (the repository-wide AST pins in
``execution/tests/test_submission_boundary.py`` keep it that way). Every
broker-bound action goes through the ONE sanctioned boundary
(``LiveOrderSubmissionService``), reached only via the mode executors in
``execution/runner_composition.py``. ***

What the runner IS
------------------

A deterministic, testable lifecycle: select work, process it item by item,
report, and either return (one cycle) or sleep and repeat (resident loop).
The business decisions -- may this intent be submitted, is it a duplicate,
is the kill switch on, what does the broker outcome mean -- belong to the
services the runner drives and are NOT re-implemented, re-guessed or
"helpfully repaired" here. The runner adds exactly three things of its own:

1. *Work selection*, delegated to a :class:`ExecutionCycleExecutor` that
   reads a store READ-ONLY. A selection failure fails the whole cycle
   CLOSED (zero items processed, nothing sent).
2. *Failure localization*: one item's failure never aborts the cycle and one
   cycle's failure never ends the loop; both are reported, never swallowed.
3. *Graceful shutdown*: once a stop is requested, NO new item is started --
   the runner never begins an intent it may not be able to finish
   observing.

What the runner is NOT
----------------------

- It is not a submission path. It cannot bypass the Safety Gate, the
  Submission Journal, the execution store's ``created -> submitting``
  transition or the durable Kill Switch, because it never performs a
  broker-bound step itself.
- It is not an idempotency mechanism. Whether a given intent may be sent
  is answered by ``LiveOrderSubmissionService`` (execution store phase +
  submission journal) and, for shadow, by the shadow store's claim.
  Restart safety therefore needs NO runner-side memory: every cycle
  re-reads the durable stores, and an intent whose outcome is uncertain is
  refused by the boundary, not re-sent by the runner. The runner keeps no
  cross-cycle in-memory state about work items at all.
- It is not a reconciliation scheduler. Unresolved journal entries /
  UNKNOWN orders are resolved by reconciliation (Issue #103-#105), which
  the runner deliberately does NOT invoke (Issue #131 non-scope).

Modes (fail closed)
-------------------

Only :data:`RUNNER_SUPPORTED_MODES` -- ``fake`` and ``shadow`` -- may be
run. ``demo`` is defined but unimplemented, ``live`` is locked behind the
live activation lock (``execution/live_lock.py``), and ``dry-run`` is a
runner FLAG (``dry_run=True``), not a mode to dispatch on. All four cases
are refused with :class:`RunnerModeError` before anything is selected --
never by falling through to a submission path.

Dry run
-------

``dry_run=True`` calls the executor's ``plan()`` instead of ``execute()``:
it reports which work items WOULD be processed and never performs a
side-effecting step (no broker call, no order-store / journal / shadow-store
write, no DB creation, no migration). "It is only a FakeBroker" is not a
reason to call a broker in dry-run, so the fake plan path stops before the
gate rather than pretending a submission.

Determinism
-----------

The clock is injected (UTC-validated like every other execution service),
the loop's sleep is an injected sleeper (default: the stop event's ``wait``,
so a shutdown interrupts the sleep immediately) and the stop signal is a
plain :class:`threading.Event`. No wall-clock sleep and no ambient time
exist in this module, so the whole lifecycle -- including the resident loop
-- is exercised deterministically by the test suite.
"""

from __future__ import annotations

import enum
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol, Sequence

from execution.safety_gate import (
    ExecutionMode,
    LOCKED_BROKER_MODES,
    UNIMPLEMENTED_BROKER_MODES,
    resolve_execution_mode,
)

_LOGGER = logging.getLogger(__name__)

#: How many work items ONE cycle may process at most. A bound exists so a
#: cycle stays observable and a shutdown stays responsive; the remainder is
#: simply picked up by the next cycle (the durable stores are the queue).
DEFAULT_MAX_ITEMS_PER_CYCLE = 10
MAX_ITEMS_PER_CYCLE_LIMIT = 1000

#: The ONLY execution modes this runner may drive (Issue #131). ``demo``
#: (unimplemented) and ``live`` (locked) are explicitly refused; adding a
#: mode here is a deliberate, reviewed act -- never a side effect of wiring
#: a new adapter.
RUNNER_SUPPORTED_MODES: frozenset[ExecutionMode] = frozenset(
    {ExecutionMode.FAKE, ExecutionMode.SHADOW}
)


class RunnerModeError(ValueError):
    """The requested execution mode may not be driven by this runner."""


def resolve_runner_mode(value) -> ExecutionMode:
    """Resolve a configured mode and refuse everything outside
    :data:`RUNNER_SUPPORTED_MODES` (fail closed).

    Unknown strings are refused by ``resolve_execution_mode`` itself; the
    known-but-forbidden modes each get their own explanation so an operator
    reads WHY, not just "no".
    """
    try:
        mode = resolve_execution_mode(value)
    except ValueError as exc:
        raise RunnerModeError(str(exc)) from None
    if mode in RUNNER_SUPPORTED_MODES:
        return mode
    if mode in UNIMPLEMENTED_BROKER_MODES:
        raise RunnerModeError(
            f"execution mode {mode.value!r} is defined but NOT implemented (no broker "
            "adapter is registered for it); the Live Execution runner refuses it "
            "instead of falling through to another path"
        )
    if mode in LOCKED_BROKER_MODES:
        raise RunnerModeError(
            f"execution mode {mode.value!r} is locked behind the live activation lock "
            "(execution/live_lock.py); the Live Execution runner may not compose a "
            "live connection"
        )
    if mode is ExecutionMode.DRY_RUN:
        raise RunnerModeError(
            "'dry-run' is a runner FLAG (dry_run=True), not a runner mode: a dry run "
            "still runs a concrete mode's selection, it just performs no "
            "side-effecting step"
        )
    raise RunnerModeError(  # pragma: no cover - closed-world guard
        f"execution mode {mode.value!r} is not runnable by the Live Execution runner; "
        f"supported: {sorted(item.value for item in RUNNER_SUPPORTED_MODES)}"
    )


class BrokerContact(enum.Enum):
    """What is PROVABLE about broker contact for one work item.

    ``NOT_SENT``    -- no broker call happened, and that is provable from
                       the boundary's own contract (a typed rejection is
                       always decided before the broker call; shadow and
                       dry-run own no broker at all; a failure raised before
                       ``submit()`` was entered cannot have called anything).
    ``SENT``        -- the boundary reached the broker and returned an
                       outcome.
    ``UNRESOLVED``  -- an exception escaped a submitting boundary: whether
                       the broker holds the order is exactly what is
                       unknown. The runner does NOT guess (and never
                       re-sends): the durable journal / execution store keep
                       the attempt visible for reconciliation.
    """

    NOT_SENT = "not_sent"
    SENT = "sent"
    UNRESOLVED = "unresolved"


class RunnerItemStatus(enum.Enum):
    """How one work item ended inside one cycle."""

    #: fake: the submission boundary submitted and recorded a broker outcome.
    SUBMITTED = "submitted"
    #: shadow: a shadow verification record was completed.
    RECORDED = "recorded"
    #: the boundary refused BEFORE any broker call (gate rejection, duplicate
    #: submission, journal failure, store failure, superseded intent, ...).
    #: The machine-readable reason codes are carried on the outcome.
    REJECTED = "rejected"
    #: a payload-equal record / claim already exists (shadow), so nothing was
    #: evaluated or written by this call.
    DUPLICATE = "duplicate"
    #: the item was not processed at all (vanished between selection and
    #: execution, already handled in this cycle, ...). Never a send.
    SKIPPED = "skipped"
    #: dry run: reported only; nothing was evaluated or persisted.
    PLANNED = "planned"
    #: the item failed. Fail-closed: see ``broker_contact`` for what is
    #: provable about broker contact.
    FAILED = "failed"


#: Statuses that are only reachable BEFORE / WITHOUT a broker call.
_NEVER_SENT_STATUSES = frozenset(
    {
        RunnerItemStatus.RECORDED,
        RunnerItemStatus.REJECTED,
        RunnerItemStatus.DUPLICATE,
        RunnerItemStatus.SKIPPED,
        RunnerItemStatus.PLANNED,
    }
)


@dataclass(frozen=True)
class RunnerItemOutcome:
    """One work item's outcome (immutable, validated -- the repository's
    model style).

    Cross-field invariants:

    - ``SUBMITTED``  <-> ``broker_contact`` is ``SENT``
    - every status in :data:`_NEVER_SENT_STATUSES` <-> ``NOT_SENT``
    - ``FAILED``     -> ``NOT_SENT`` (provably before the boundary was
      entered) or ``UNRESOLVED`` (an exception escaped a submitting
      boundary). A failure may never claim ``SENT``.
    """

    item_id: str
    status: RunnerItemStatus
    broker_contact: BrokerContact
    detail: str
    rejection_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty_str(self.item_id, "item_id")
        if not isinstance(self.status, RunnerItemStatus):
            raise ValueError(f"status must be a RunnerItemStatus, got {self.status!r}")
        if not isinstance(self.broker_contact, BrokerContact):
            raise ValueError(f"broker_contact must be a BrokerContact, got {self.broker_contact!r}")
        _require_non_empty_str(self.detail, "detail")
        if not isinstance(self.rejection_codes, tuple) or any(
            not isinstance(code, str) or not code.strip() for code in self.rejection_codes
        ):
            raise ValueError(
                f"rejection_codes must be a tuple of non-empty strings, got "
                f"{self.rejection_codes!r}"
            )
        if self.status is RunnerItemStatus.SUBMITTED:
            if self.broker_contact is not BrokerContact.SENT:
                raise ValueError("a SUBMITTED outcome requires broker_contact=SENT")
        elif self.status in _NEVER_SENT_STATUSES:
            if self.broker_contact is not BrokerContact.NOT_SENT:
                raise ValueError(
                    f"a {self.status.value.upper()} outcome is decided before any broker "
                    "call and requires broker_contact=NOT_SENT"
                )
        elif self.broker_contact is BrokerContact.SENT:
            raise ValueError(
                "a FAILED outcome may never claim a completed broker call; use "
                "UNRESOLVED when an exception escaped a submitting boundary"
            )


class RunnerStopReason(enum.Enum):
    """Why :meth:`ExecutionRunner.run_forever` returned."""

    STOP_REQUESTED = "stop_requested"
    MAX_CYCLES = "max_cycles"


@dataclass(frozen=True)
class RunnerCycleReport:
    """Everything ONE cycle did -- the unit of observation and of failure.

    ``selection_error`` and ``cycle_error`` are the two fail-closed cycle
    outcomes: work selection failed (nothing was processed) or the cycle
    itself aborted unexpectedly. Both leave the loop running; neither can
    hide a broker send, because a send is only ever reachable through an
    item outcome.
    """

    cycle_id: str
    cycle_index: int
    execution_mode: str
    dry_run: bool
    started_at_utc: datetime
    finished_at_utc: datetime
    outcomes: tuple[RunnerItemOutcome, ...] = ()
    selection_error: str | None = None
    cycle_error: str | None = None
    stopped: bool = False

    def __post_init__(self) -> None:
        _require_non_empty_str(self.cycle_id, "cycle_id")
        if isinstance(self.cycle_index, bool) or not isinstance(self.cycle_index, int) or self.cycle_index < 1:
            raise ValueError(f"cycle_index must be an int >= 1, got {self.cycle_index!r}")
        _require_non_empty_str(self.execution_mode, "execution_mode")
        if not isinstance(self.dry_run, bool):
            raise ValueError(f"dry_run must be a bool, got {self.dry_run!r}")
        _require_utc(self.started_at_utc, "started_at_utc")
        _require_utc(self.finished_at_utc, "finished_at_utc")
        if not isinstance(self.outcomes, tuple) or any(
            not isinstance(item, RunnerItemOutcome) for item in self.outcomes
        ):
            raise ValueError("outcomes must be a tuple of RunnerItemOutcome")
        for name in ("selection_error", "cycle_error"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or None, got {value!r}")
        if not isinstance(self.stopped, bool):
            raise ValueError(f"stopped must be a bool, got {self.stopped!r}")
        if self.selection_error is not None and self.outcomes:
            raise ValueError(
                "a cycle whose work selection failed processed nothing; it may not "
                "carry item outcomes"
            )

    @property
    def processed(self) -> int:
        return len(self.outcomes)

    @property
    def failed(self) -> int:
        return sum(1 for item in self.outcomes if item.status is RunnerItemStatus.FAILED)

    @property
    def broker_sends(self) -> int:
        """Item outcomes that PROVABLY reached the broker. Unresolved
        outcomes are counted separately on purpose -- "we do not know" must
        never be reported as "we did not send"."""
        return sum(1 for item in self.outcomes if item.broker_contact is BrokerContact.SENT)

    @property
    def unresolved(self) -> int:
        return sum(
            1 for item in self.outcomes if item.broker_contact is BrokerContact.UNRESOLVED
        )

    @property
    def ok(self) -> bool:
        """True when the cycle completed without a cycle-level failure and
        without a failed item (a Safety Gate rejection is a normal, recorded
        outcome -- not a runner failure)."""
        return self.selection_error is None and self.cycle_error is None and self.failed == 0

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.outcomes:
            counts[item.status.value] = counts.get(item.status.value, 0) + 1
        return counts


@dataclass(frozen=True)
class RunnerLoopReport:
    """The resident loop's summary (one entry per executed cycle)."""

    cycles: tuple[RunnerCycleReport, ...]
    stop_reason: RunnerStopReason

    def __post_init__(self) -> None:
        if not isinstance(self.cycles, tuple) or any(
            not isinstance(item, RunnerCycleReport) for item in self.cycles
        ):
            raise ValueError("cycles must be a tuple of RunnerCycleReport")
        if not isinstance(self.stop_reason, RunnerStopReason):
            raise ValueError(f"stop_reason must be a RunnerStopReason, got {self.stop_reason!r}")

    @property
    def broker_sends(self) -> int:
        return sum(cycle.broker_sends for cycle in self.cycles)


class ExecutionCycleExecutor(Protocol):
    """What one execution mode contributes to a cycle.

    The executor owns the mode-specific wiring ONLY: which durable store
    holds the pending work, and which production service processes one
    item. It must never re-implement a decision the service already makes.

    ``error_broker_contact`` declares what an exception escaping
    :meth:`execute` proves about broker contact for THIS mode: a shadow
    executor owns no broker (``NOT_SENT``), a submitting executor cannot
    know (``UNRESOLVED``).
    """

    @property
    def execution_mode(self) -> ExecutionMode: ...

    @property
    def source_id(self) -> str: ...

    @property
    def error_broker_contact(self) -> BrokerContact: ...

    def select(self, *, limit: int) -> Sequence[str]:
        """Work item ids for ONE cycle, oldest/newest-first per mode, at most
        ``limit``. Read-only: selection never mutates a store and never
        creates one. A failure raises (the cycle then fails closed)."""

    def execute(self, item_id: str) -> RunnerItemOutcome:
        """Process ONE item through the production service boundary."""

    def plan(self, item_id: str) -> RunnerItemOutcome:
        """Dry run: report what WOULD happen with zero side effects."""


class ExecutionRunner:
    """The Live Execution runner core (see the module docstring)."""

    def __init__(
        self,
        *,
        executor: ExecutionCycleExecutor,
        clock: Callable[[], datetime],
        max_items_per_cycle: int = DEFAULT_MAX_ITEMS_PER_CYCLE,
        dry_run: bool = False,
        stop_event: threading.Event | None = None,
        runner_id: str = "execution-runner",
    ) -> None:
        mode = getattr(executor, "execution_mode", None)
        if not isinstance(mode, ExecutionMode):
            raise RunnerModeError(
                "executor must expose an ExecutionMode as execution_mode, got "
                f"{mode!r}"
            )
        # The mode gate runs at CONSTRUCTION, before anything can be selected
        # or processed: an executor for an unimplemented / locked mode can
        # never be driven, not even for one cycle.
        if mode not in RUNNER_SUPPORTED_MODES:
            resolve_runner_mode(mode)
            raise RunnerModeError(  # pragma: no cover - resolve_runner_mode raises
                f"execution mode {mode.value!r} is not runnable by this runner"
            )
        for name in ("select", "execute", "plan"):
            if not callable(getattr(executor, name, None)):
                raise ValueError(f"executor must provide a callable {name}()")
        if not isinstance(getattr(executor, "error_broker_contact", None), BrokerContact):
            raise ValueError(
                "executor must declare error_broker_contact (what an escaping "
                "exception proves about broker contact)"
            )
        if not callable(clock):
            raise ValueError(f"clock must be a callable returning a UTC datetime, got {clock!r}")
        if (
            isinstance(max_items_per_cycle, bool)
            or not isinstance(max_items_per_cycle, int)
            or not 1 <= max_items_per_cycle <= MAX_ITEMS_PER_CYCLE_LIMIT
        ):
            raise ValueError(
                f"max_items_per_cycle must be an int between 1 and "
                f"{MAX_ITEMS_PER_CYCLE_LIMIT}, got {max_items_per_cycle!r}"
            )
        if not isinstance(dry_run, bool):
            raise ValueError(f"dry_run must be a bool, got {dry_run!r}")
        if stop_event is not None and not isinstance(stop_event, threading.Event):
            raise ValueError(f"stop_event must be a threading.Event or None, got {stop_event!r}")
        _require_non_empty_str(runner_id, "runner_id")
        self._executor = executor
        self._mode = mode
        self._clock = clock
        self._max_items_per_cycle = max_items_per_cycle
        self._dry_run = dry_run
        self._stop_event = stop_event if stop_event is not None else threading.Event()
        self._runner_id = runner_id
        self._cycle_index = 0

    # --- properties ----------------------------------------------------------

    @property
    def execution_mode(self) -> ExecutionMode:
        return self._mode

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def max_items_per_cycle(self) -> int:
        return self._max_items_per_cycle

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    @property
    def cycles_run(self) -> int:
        return self._cycle_index

    # --- shutdown ------------------------------------------------------------

    def request_stop(self) -> None:
        """Request a graceful shutdown: the current item finishes, no new
        item is started, and the loop's sleep is interrupted.

        Safe to call from a signal handler or another thread (a
        ``threading.Event`` is the whole mechanism). It is deliberately
        one-way: nothing in this module clears the event, so a shutdown
        cannot be un-requested by a later cycle."""
        self._stop_event.set()

    # --- one cycle -----------------------------------------------------------

    def run_cycle(self) -> RunnerCycleReport:
        """Run exactly ONE cycle: select work, process each item, report.

        Never raises for an item failure or a selection failure -- both are
        reported on the cycle. It does raise if the injected clock itself is
        broken (there is then no trustworthy time to report, and refusing is
        the fail-closed answer: nothing was selected or sent).
        """
        self._cycle_index += 1
        index = self._cycle_index
        cycle_id = f"{self._runner_id}-cycle-{index}"
        started = self._now()

        if self.stop_requested:
            # Shutdown observed BEFORE any work was selected: a stopping
            # runner never starts new intents.
            _LOGGER.info(
                "runner cycle %s skipped: shutdown requested before work selection", cycle_id
            )
            return self._report(cycle_id, index, started, stopped=True)

        try:
            # Materializing (and bounding) the selection belongs to the SAME
            # fail-closed step: a work source that cannot even produce a
            # usable list must fail the cycle, not raise out of run_cycle.
            selected = self._bounded(
                self._executor.select(limit=self._max_items_per_cycle), cycle_id
            )
        except Exception as exc:
            # Fail CLOSED: an unreadable / inconsistent work source means we
            # do not know what to process, so we process nothing.
            _LOGGER.warning(
                "runner cycle %s work selection failed (mode=%s source=%s): %s; "
                "nothing was processed and nothing was sent",
                cycle_id,
                self._mode.value,
                self._source_id(),
                exc,
            )
            return self._report(
                cycle_id,
                index,
                started,
                selection_error=f"work selection failed: {type(exc).__name__}: {exc}",
            )

        outcomes: list[RunnerItemOutcome] = []
        stopped = False
        for item_id in selected:
            if self.stop_requested:
                stopped = True
                _LOGGER.info(
                    "runner cycle %s stopping early: shutdown requested; %d item(s) "
                    "processed, no new item started",
                    cycle_id,
                    len(outcomes),
                )
                break
            if not isinstance(item_id, str) or not item_id.strip():
                _LOGGER.warning(
                    "runner cycle %s received an invalid work item id %r from %s; "
                    "refusing to process it",
                    cycle_id,
                    item_id,
                    self._source_id(),
                )
                outcomes.append(
                    RunnerItemOutcome(
                        item_id=f"<invalid:{type(item_id).__name__}>",
                        status=RunnerItemStatus.FAILED,
                        broker_contact=BrokerContact.NOT_SENT,
                        detail=(
                            f"the work source returned an invalid item id {item_id!r}; "
                            "nothing was processed for it"
                        ),
                    )
                )
                continue
            if any(done.item_id == item_id for done in outcomes):
                # Same id twice in ONE selection: process it once. The
                # boundary would refuse the second attempt anyway, but a
                # runner that knowingly re-enters the same intent inside one
                # cycle is exactly what Issue #131 forbids.
                _LOGGER.warning(
                    "runner cycle %s received work item %s twice from %s; the "
                    "duplicate is skipped",
                    cycle_id,
                    item_id,
                    self._source_id(),
                )
                outcomes.append(
                    RunnerItemOutcome(
                        item_id=item_id,
                        status=RunnerItemStatus.SKIPPED,
                        broker_contact=BrokerContact.NOT_SENT,
                        detail=(
                            "the work source returned this item twice in one cycle; it "
                            "was processed once and never re-entered"
                        ),
                    )
                )
                continue
            outcomes.append(self._process(item_id, cycle_id))

        report = self._report(cycle_id, index, started, outcomes=tuple(outcomes), stopped=stopped)
        _LOGGER.info(
            "runner cycle %s finished mode=%s dry_run=%s processed=%d statuses=%s "
            "broker_sends=%d unresolved=%d",
            cycle_id,
            self._mode.value,
            self._dry_run,
            report.processed,
            report.status_counts(),
            report.broker_sends,
            report.unresolved,
        )
        return report

    # --- resident loop -------------------------------------------------------

    def run_forever(
        self,
        *,
        interval_seconds: float,
        max_cycles: int | None = None,
        sleeper: Callable[[float], bool] | None = None,
        on_cycle: Callable[[RunnerCycleReport], None] | None = None,
    ) -> RunnerLoopReport:
        """Run cycles until a stop is requested (or ``max_cycles`` is
        reached), sleeping ``interval_seconds`` between them.

        The loop body holds NO business logic (Issue #131: nothing that has
        to be tested may hide inside an unbounded loop) -- it only paces
        :meth:`run_cycle`, localizes its failures and observes the stop
        signal. ``sleeper`` receives the interval and returns truthy when a
        stop was signalled DURING the sleep; the default is the stop event's
        ``wait``, so a shutdown never waits out the interval. Injecting a
        sleeper (plus the injected clock and stop event) makes the whole
        loop deterministic in tests -- no wall-clock sleep is ever performed
        by this module.
        """
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float)):
            raise ValueError(f"interval_seconds must be a number, got {interval_seconds!r}")
        if not interval_seconds > 0:
            raise ValueError(f"interval_seconds must be > 0, got {interval_seconds!r}")
        if interval_seconds > threading.TIMEOUT_MAX:
            # PR #135 Codex review P2: the DEFAULT sleeper is
            # ``threading.Event.wait``, which raises OverflowError for a
            # timeout above the platform limit (``inf`` included). That
            # exception would escape the loop -- which only translates
            # KeyboardInterrupt -- and kill a resident runner after its
            # first cycle. A misconfigured interval must be refused at the
            # boundary instead, before any cycle runs (an interval beyond
            # the limit is ~49 days: never a real pacing choice).
            raise ValueError(
                f"interval_seconds must not exceed the platform timeout limit "
                f"({threading.TIMEOUT_MAX}), got {interval_seconds!r}"
            )
        if max_cycles is not None and (
            isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 1
        ):
            raise ValueError(f"max_cycles must be an int >= 1 or None, got {max_cycles!r}")
        if sleeper is not None and not callable(sleeper):
            raise ValueError(f"sleeper must be callable, got {sleeper!r}")
        if on_cycle is not None and not callable(on_cycle):
            raise ValueError(f"on_cycle must be callable, got {on_cycle!r}")
        sleep = sleeper if sleeper is not None else self._stop_event.wait

        cycles: list[RunnerCycleReport] = []
        stop_reason = RunnerStopReason.STOP_REQUESTED
        while True:
            if self.stop_requested:
                break
            if max_cycles is not None and len(cycles) >= max_cycles:
                stop_reason = RunnerStopReason.MAX_CYCLES
                break
            try:
                report = self.run_cycle()
            except KeyboardInterrupt:
                # Interactive shutdown: request the stop so the contract is
                # identical to a signalled one, then return what ran.
                self.request_stop()
                break
            except Exception as exc:
                # One cycle's unexpected failure must not end the loop
                # (Issue #131: failures are localized per cycle). Nothing
                # was sent by the runner itself; any in-flight boundary work
                # stays visible through the durable stores.
                report = self._failed_cycle_report(exc)
            cycles.append(report)
            if on_cycle is not None:
                try:
                    on_cycle(report)
                except Exception:
                    _LOGGER.warning(
                        "runner cycle callback failed for %s; the loop continues",
                        report.cycle_id,
                        exc_info=True,
                    )
            if self.stop_requested:
                break
            if max_cycles is not None and len(cycles) >= max_cycles:
                stop_reason = RunnerStopReason.MAX_CYCLES
                break
            try:
                interrupted = sleep(interval_seconds)
            except KeyboardInterrupt:
                self.request_stop()
                break
            if interrupted or self.stop_requested:
                break
        _LOGGER.info(
            "runner loop finished mode=%s cycles=%d reason=%s",
            self._mode.value,
            len(cycles),
            stop_reason.value,
        )
        return RunnerLoopReport(cycles=tuple(cycles), stop_reason=stop_reason)

    # --- internals -----------------------------------------------------------

    def _process(self, item_id: str, cycle_id: str) -> RunnerItemOutcome:
        """Run ONE item through the executor, localizing every failure."""
        try:
            outcome = (
                self._executor.plan(item_id) if self._dry_run else self._executor.execute(item_id)
            )
        except Exception as exc:
            # A dry run never reaches a boundary that could send, so its
            # failures are provably NOT_SENT; otherwise the executor's own
            # declaration decides (shadow: no broker exists at all; fake: an
            # escaping exception means the outcome is unresolved and must
            # NEVER be re-sent -- reconciliation resolves it).
            contact = (
                BrokerContact.NOT_SENT if self._dry_run else self._executor.error_broker_contact
            )
            _LOGGER.warning(
                "runner cycle %s item %s failed (mode=%s broker_contact=%s): %s",
                cycle_id,
                item_id,
                self._mode.value,
                contact.value,
                exc,
            )
            return RunnerItemOutcome(
                item_id=item_id,
                status=RunnerItemStatus.FAILED,
                broker_contact=contact,
                detail=f"{type(exc).__name__}: {exc}",
            )
        if not isinstance(outcome, RunnerItemOutcome) or outcome.item_id != item_id:
            # Defensive: a misbehaving executor must not be able to report an
            # outcome for another item (or no outcome at all).
            _LOGGER.error(
                "runner cycle %s item %s: the executor returned %r instead of a matching "
                "RunnerItemOutcome",
                cycle_id,
                item_id,
                outcome,
            )
            return RunnerItemOutcome(
                item_id=item_id,
                status=RunnerItemStatus.FAILED,
                broker_contact=self._executor.error_broker_contact,
                detail=(
                    "the executor did not return a RunnerItemOutcome for this item; the "
                    "result is treated as unusable (fail closed)"
                ),
            )
        return outcome

    def _bounded(self, selected, cycle_id: str) -> list:
        """The selection, defensively bounded by ``max_items_per_cycle``."""
        try:
            items = list(selected)
        except Exception as exc:
            raise ValueError(f"the work source returned a non-iterable selection: {exc}") from exc
        if len(items) > self._max_items_per_cycle:
            _LOGGER.warning(
                "runner cycle %s work source returned %d items for a limit of %d; the "
                "surplus is left for the next cycle",
                cycle_id,
                len(items),
                self._max_items_per_cycle,
            )
            items = items[: self._max_items_per_cycle]
        return items

    def _source_id(self) -> str:
        try:
            return str(self._executor.source_id)
        except Exception:  # pragma: no cover - diagnostics must never fail a cycle
            return "<unknown>"

    def _report(
        self,
        cycle_id: str,
        index: int,
        started: datetime,
        *,
        outcomes: tuple[RunnerItemOutcome, ...] = (),
        selection_error: str | None = None,
        cycle_error: str | None = None,
        stopped: bool = False,
    ) -> RunnerCycleReport:
        return RunnerCycleReport(
            cycle_id=cycle_id,
            cycle_index=index,
            execution_mode=self._mode.value,
            dry_run=self._dry_run,
            started_at_utc=started,
            finished_at_utc=self._safe_now(started),
            outcomes=outcomes,
            selection_error=selection_error,
            cycle_error=cycle_error,
            stopped=stopped,
        )

    def _failed_cycle_report(self, exc: Exception) -> RunnerCycleReport:
        """A cycle that aborted before it could build its own report (e.g. a
        broken clock). Wall-clock time is the last resort here for exactly
        the reason ``live_submission`` uses it: the injected clock may be
        what is broken, and losing the record of the failure would be worse
        than an approximate timestamp."""
        now = datetime.now(timezone.utc)
        _LOGGER.warning(
            "runner cycle %d failed before completing (mode=%s): %s; the loop continues "
            "with the next cycle",
            self._cycle_index,
            self._mode.value,
            exc,
        )
        return RunnerCycleReport(
            cycle_id=f"{self._runner_id}-cycle-{max(self._cycle_index, 1)}",
            cycle_index=max(self._cycle_index, 1),
            execution_mode=self._mode.value,
            dry_run=self._dry_run,
            started_at_utc=now,
            finished_at_utc=now,
            cycle_error=f"cycle failed: {type(exc).__name__}: {exc}",
        )

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

    def _safe_now(self, fallback: datetime) -> datetime:
        """The cycle's end time; a clock that broke MID-cycle must not throw
        away the report of what already happened."""
        try:
            return self._now()
        except Exception:
            _LOGGER.warning(
                "the runner clock failed while closing a cycle; the cycle start time is "
                "reported as its end time"
            )
            return fallback


def _require_non_empty_str(value, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")


def _require_utc(value, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime, got {value!r}")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field_name} must be UTC (utcoffset 0), got {value.tzinfo}")
