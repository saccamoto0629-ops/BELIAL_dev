r"""Scheduled reconciliation cycle (Issue #133) -- RECONCILE-ONLY, and a
SEPARATE EXECUTION UNIT from the Live Execution runner.

*** NO ORDER IS EVER PLACED, CANCELLED OR MODIFIED FROM THIS MODULE, AND NO
BROKER IS COMPOSED HERE. *** The cycle accepts ONE broker shape -- the
existing :class:`execution.reconciliation_runner.ReconcileOnlyBroker` facade,
whose ``submit_order`` / ``cancel_order`` / ``get_account_state`` raise
unconditionally (Issue #105) -- and refuses anything else at the door. It is
the SAME facade class the manual CLI uses, deliberately imported instead of
re-implemented: a second "almost reconcile-only" wrapper would be a second
thing to keep honest.

What one cycle is
-----------------

One cycle is one oneshot unit of work, meant to be driven by a systemd timer
(``scripts/systemd/berial-execution-reconciliation.{service,timer}``), NOT by
the Execution submission runner:

1. *exclusive entry* -- an OS-level advisory lock (see "Overlap" below) so
   two cycles never run against the same data directory;
2. *takeover / recovery* (repair-enabled cycles only) -- the existing
   ``recover_pending_attempts()`` resumes what a CRASHED earlier cycle left
   pending, under the ORIGINAL claim identity. This runs BEFORE the new
   observation so a leftover attempt is settled before anything is compared.
   If it REFUSES a reservation, reports any ``BLOCKED`` attempt, or leaves
   ANY attempt still pending (still under a valid lease), step 4 is skipped
   for the whole cycle -- a fresh attempt would take an existing claim's
   fence over, and a blocked takeover needs a human (see "An unresolved
   claim" below);
3. *detection* -- ``BrokerOrderReconciliationService.run()``: the Issue #103
   order scope plus, by default, the Issue #105 position / trade /
   transaction / SL / TP scope. The run is recorded to the reconciliation
   audit DB, which is what makes it survivable across a restart;
4. *repair* (opt-in) -- ``BrokerOrderReconciliationRepairService.repair()``
   over the run just recorded.

Why the Execution runner does NOT do this
-----------------------------------------

The submission runner's lifecycle is "select an intent, hand it to the
submission boundary, observe the outcome". Reconciliation's lifecycle is
"observe both sides, classify, and possibly reflect a broker fact into our
own state machine". They fail differently, they are allowed to touch
different things, and they must be able to run, stall and be restarted
independently -- a reconciliation stall must never stop submissions and a
submission stall must never stop the audit. So this is its own process, its
own systemd unit and its own lock. ``execution/live_runner.py`` keeps its
Issue #131 non-scope note: it does not invoke reconciliation.

What this module deliberately CANNOT do
---------------------------------------

- It has no ``submit`` / ``cancel`` capability and no way to acquire one:
  the only broker object it accepts already raises on both, it imports no
  broker implementation, it composes no ``BrokerConnection`` and it never
  constructs an adapter (all four pinned by
  ``execution/tests/test_submission_boundary.py``).
- It does not classify anything itself. Which discrepancy is an
  ``auto_repair_candidate`` and which is ``manual_review`` is decided by the
  Issue #103 / #105 detection service, and whether a candidate may be
  reflected is decided by the Issue #104 repair service -- including its
  claim / lease / generation / takeover / fencing contract, which this
  module neither re-implements nor relaxes. ``manual_review`` (every
  position / trade / transaction / SL / TP discrepancy, and every
  broker-only order) is never auto-repaired, because the repair service
  refuses it and the position domain has no repair path at all.
- It performs no state update of its own: it owns no execution-store write
  call.

An unresolved claim stops the cycle's repair pass
-------------------------------------------------

After recovery, the cycle skips the fresh pass if EITHER anything is still
PENDING in the audit DB, or recovery reported any ``BLOCKED`` attempt.

Both, because neither implies the other. Recovery returning nothing does not
mean nothing is pending, and a BLOCKED attempt is not always still pending:
when the protocol parks an ORPHANED active intent whose attempt was already
finalized, it reports BLOCKED with no pending row behind it. Checking only
one of the two leaves the other's case walking straight through.

Recovery returning nothing does NOT mean nothing is protected. It
deliberately leaves alone every reservation whose lease is still valid
(someone may be driving it right now, and a killed process's claim stays
leased until it expires), and it leaves a ``BLOCKED`` one pending on
purpose -- the Issue #104 protocol parks a state it must not resolve (a
fence ahead of the intent, a foreign identity, several active intents) and
asks for a human, "automatic recovery from blocked" being an explicit
non-scope there.

A fresh pass would walk straight past both boundaries. It mints a NEW
attempt id, and ``execution_store.register_repair_fence`` REPLACES a fence
held by a DIFFERENT attempt by design (a genuinely new attempt is allowed
to take an order over) -- so the very order somebody else has claimed would
be re-observed, re-claimed at generation 1 and committed, while the original
claim stayed pending. Under a timer that is not a one-off: it is every tick.

The entry lock does not cover this. It excludes concurrent CYCLES; a claim
outlives the cycle that made it, so a claim left by a killed cycle (or by a
human calling the repair API) is still there when the next tick takes the
lock.

The deferral is self-healing and bounded: once the lease expires, the next
cycle's recovery takes the attempt over under its ORIGINAL identity and the
pass runs again. Only a BLOCKED attempt stays indefinitely -- which is the
point, and which is why it also makes the cycle report not-clean.

Detection always runs and is always recorded either way: a read-only
observation is exactly what an operator diagnoses the situation with.

Concurrency: what the gate is, and what it is not
--------------------------------------------------

The gate is taken as LATE as possible -- immediately before the pass, after
detection -- so the window between "look" and "act" is as small as this
module can make it. It is still a POLICY gate, not a mutex.

A DIRECT caller of the repair service (an operator invoking the Issue #104
API) does not take the cycle lock and can therefore claim in the remaining
instant. That case is NOT this module's to solve, and it is already
resolved: it is the ordinary "two repair workers, one order" situation the
Issue #104 fencing contract is built for. ``register_repair_fence`` lets the
newer attempt take the order over, and the older worker's commit and
finalize then fail typed (``repair_fence_conflict`` -> outcome ``conflict``)
inside the transaction -- never a wrong write, never two provenance rows,
never a lost update. ``execution/tests/test_reconciliation_schedule.py``
pins exactly that with a real paused thread.

Making the check and the claim atomic ACROSS callers would mean adding a
cross-process admission protocol inside the repair service -- changing the
Issue #104 claim / lease / generation / fencing contract, which Issue #133
is explicitly not allowed to do, to prevent an outcome that contract already
makes safe.

Overlap / concurrent runs
-------------------------

A timer-driven ``Type=oneshot`` unit is already not started twice by systemd,
but a manual run alongside a timer run is not covered by that -- so entry is
guarded by an OS-level advisory lock held for the whole cycle
(:func:`cycle_lock`). The lock is an empty SQLite database used purely as a
portable ``flock``: SQLite's own locking works identically on POSIX and
Windows, is released by the kernel when the process dies (so a crashed cycle
leaves no stale lock to clear by hand), and refuses a second holder in the
SAME process too, which is what makes the exclusion testable. A busy lock is
a SUPPRESSED cycle, not a failure (:class:`CycleAlreadyRunningError`); the
CLI reports it and exits 0, the same stance ``scripts/cleanup_png.py`` takes.

This lock bounds ENTRY. It is not, and must not be mistaken for, the
protection against two workers touching one discrepancy: that is the repair
service's durable claim / lease / generation / fencing contract, which keeps
holding whether or not this lock exists.

Dry run
-------

``dry_run=True`` is fully side-effect free: detection runs through
``detect()`` (reads and broker queries only), the reconciliation DB is not
created, not migrated and not written, a missing execution DB reads as empty
and is not created, the lock file is NOT opened or created, and repair is
structurally skipped -- there is no recorded run to repair, and a dry-run
repair pass would have to read a DB this mode must not require.

Restart continuity
------------------

Everything a cycle produces is durable before the process exits: the run,
its order and position discrepancies (reconciliation audit DB), and every
repair attempt (claim row -> finalized row, plus takeover intents). The
report returned here is a VIEW of that, never the record. After a restart
the next cycle picks the pending attempts up by lease expiry, and an
operator can follow any past cycle by ``run_id`` through
``reconciliation_store.get_reconciliation_run`` /
``list_repair_attempts`` / ``list_pending_repair_attempts``.
"""

from __future__ import annotations

import errno
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from execution import reconciliation_store
from execution.broker_reconciliation import BrokerOrderReconciliationService
from execution.broker_reconciliation_repair import (
    BrokerOrderReconciliationRepairService,
    DEFAULT_LEASE_DURATION_SECONDS,
    ReconciliationRepairError,
    ReconciliationRepairRunError,
)
from execution.reconciliation_models import ReconciliationRun
from execution.reconciliation_repair_models import (
    OrderRepairAttempt,
    RepairMode,
    RepairOutcome,
    TakeoverIntentStatus,
)
from execution.reconciliation_runner import ReconcileOnlyBroker

_LOGGER = logging.getLogger(__name__)

#: The advisory lock guarding ONE data directory's reconciliation cycles.
#: It lives next to the reconciliation audit DB so two deployments pointed at
#: different data directories (e.g. fake and demo) never block each other.
DEFAULT_CYCLE_LOCK_FILENAME = "reconciliation_cycle.lock"

#: SQLITE_BUSY. Used with ``sqlite_errorcode`` (3.11+) and cross-checked
#: against the message, the same "locked / busy" markers the reconciliation
#: store already classifies on.
_SQLITE_BUSY = 5
_BUSY_MARKERS = ("locked", "busy")


class ReconciliationCycleError(RuntimeError):
    """A scheduled reconciliation cycle could not run as specified."""


class CycleLockError(ReconciliationCycleError):
    """The cycle lock could not be opened (bad path, unusable file, ...)."""


class CycleAlreadyRunningError(ReconciliationCycleError):
    """Another cycle holds the lock for this data directory.

    Expected and benign under a timer: the previous cycle is still working.
    The caller SUPPRESSES this cycle -- it does not fail the unit.
    """


def cycle_lock_path_for(reconciliation_db_path: Path | str) -> Path:
    """The default lock path for a reconciliation DB: a sibling file, so the
    lock scope is exactly the data directory whose audit DB is written."""
    return Path(reconciliation_db_path).parent / DEFAULT_CYCLE_LOCK_FILENAME


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code == _SQLITE_BUSY
    message = str(exc).lower()
    return any(marker in message for marker in _BUSY_MARKERS)


@contextmanager
def cycle_lock(lock_path: Path | str, *, wait_seconds: float = 0.0):
    """Hold the exclusive cycle lock for the duration of the block.

    ``wait_seconds`` is how long to wait for a busy lock before giving up
    (0 = refuse immediately, the timer-friendly default: a cycle that is
    already running will finish, and the next timer tick is the retry).

    Raises :class:`CycleAlreadyRunningError` when another holder has it and
    :class:`CycleLockError` when the lock file itself is unusable. The lock
    is an empty SQLite database held under ``BEGIN IMMEDIATE``; nothing is
    ever written into it, so it stays a zero-page file.
    """
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)):
        raise ValueError(f"wait_seconds must be a number, got {wait_seconds!r}")
    if wait_seconds < 0:
        raise ValueError(f"wait_seconds must be >= 0, got {wait_seconds!r}")
    lock_path = Path(lock_path)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # The errno NAME (never the OS message) so a sandboxed deployment is
        # diagnosable from the journal: EROFS / EACCES here means the data
        # directory is not writable by this unit -- see the ReadWritePaths
        # precondition in scripts/systemd/berial-execution-reconciliation.service.
        raise CycleLockError(
            f"cannot create the directory for the cycle lock at {lock_path}: "
            f"{type(exc).__name__} ({errno.errorcode.get(exc.errno, exc.errno)})"
        ) from exc
    try:
        connection = sqlite3.connect(str(lock_path), timeout=float(wait_seconds))
    except sqlite3.Error as exc:
        raise CycleLockError(
            f"cannot open the cycle lock at {lock_path}: {type(exc).__name__}"
        ) from exc
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise CycleAlreadyRunningError(
                    f"another reconciliation cycle holds the lock at {lock_path}; "
                    "this cycle is suppressed (the next scheduled run is the retry)"
                ) from exc
            raise CycleLockError(
                f"cannot acquire the cycle lock at {lock_path}: {type(exc).__name__}"
            ) from exc
        except sqlite3.DatabaseError as exc:
            raise CycleLockError(
                f"the cycle lock at {lock_path} is not a usable lock file: "
                f"{type(exc).__name__}"
            ) from exc
        try:
            yield lock_path
        finally:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - close() releases it anyway
                pass
    finally:
        connection.close()


@dataclass(frozen=True)
class ReconciliationCycleReport:
    """What ONE cycle did -- a VIEW over the durable audit records.

    ``run`` is the detection result (recorded unless ``dry_run``).
    ``recovered_attempts`` are the pending attempts an earlier, crashed cycle
    left behind and this cycle resumed or took over; ``repair_attempts`` are
    this cycle's attempts over the run it just recorded. Both are empty when
    repair is not enabled. ``repair_skipped_reason`` says WHY repair did not
    run (it is never silently absent), and ``repair_error`` carries a
    sanitized description of a repair pass that stopped part-way -- the
    attempts it completed are still in ``repair_attempts`` and in the audit
    DB.
    """

    run_id: str
    run: ReconciliationRun
    dry_run: bool
    include_position_scope: bool
    repair_enabled: bool
    recorded_to: Path | None = None
    recovered_attempts: tuple[OrderRepairAttempt, ...] = field(default_factory=tuple)
    repair_attempts: tuple[OrderRepairAttempt, ...] = field(default_factory=tuple)
    repair_skipped_reason: str | None = None
    repair_error: str | None = None
    #: Takeover ids of BLOCKED intents that were ALREADY in the audit DB when
    #: this cycle started -- durable "a human must look at this" records from
    #: an earlier cycle. They keep every later cycle from reporting clean
    #: until someone deals with them; see ``is_clean``.
    blocked_intent_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def discrepancy_count(self) -> int:
        return len(self.run.discrepancies) + len(self.run.position_discrepancies)

    @property
    def blocked_attempts(self) -> tuple[OrderRepairAttempt, ...]:
        """Attempts the takeover protocol refused to resolve automatically.

        They stay pending in the audit DB with a durable blocked intent, so
        they need a human -- a cycle that produced one is NOT clean even if
        nothing else was found.
        """
        return tuple(
            attempt
            for attempt in self.recovered_attempts + self.repair_attempts
            if attempt.outcome is RepairOutcome.BLOCKED
        )

    @property
    def is_clean(self) -> bool:
        return (
            self.discrepancy_count == 0
            and not self.run.has_partial_failures
            and self.repair_error is None
            and not self.blocked_attempts
            and not self.blocked_intent_ids
        )


def _require_reconcile_only(broker) -> ReconcileOnlyBroker:
    """The cycle accepts the reconcile-only facade and NOTHING else.

    A broker that can submit or cancel must not be reachable from a
    scheduled path, so this refuses by TYPE rather than by inspecting
    methods: a duck-typed "query-only" object would be exactly the kind of
    near-miss the facade exists to prevent.
    """
    if not isinstance(broker, ReconcileOnlyBroker):
        raise ReconciliationCycleError(
            "the scheduled reconciliation cycle accepts only a ReconcileOnlyBroker "
            "(execution/reconciliation_runner.py), whose submit / cancel / account "
            f"operations raise unconditionally; got {type(broker).__name__}"
        )
    return broker


def _repair_service(
    *,
    broker: ReconcileOnlyBroker,
    execution_db_path: Path,
    reconciliation_db_path: Path,
    owner_id: str | None,
    lease_duration_seconds: int,
    broker_timeout_seconds: float | None,
    clock: Callable[[], datetime] | None,
) -> BrokerOrderReconciliationRepairService:
    """Build the Issue #104 repair service for this cycle.

    Its constructor validation -- notably ``broker query timeout < claim
    lease``, the contract that stands in for a heartbeat -- is a
    CONFIGURATION refusal, so it is surfaced as a cycle error rather than an
    unhandled ``ValueError`` from inside a scheduled process.
    """
    try:
        return BrokerOrderReconciliationRepairService(
            broker=broker,
            execution_db_path=execution_db_path,
            reconciliation_db_path=reconciliation_db_path,
            owner_id=owner_id,
            lease_duration_seconds=lease_duration_seconds,
            broker_timeout_seconds=broker_timeout_seconds,
            clock=clock,
        )
    except ValueError as exc:
        raise ReconciliationCycleError(
            f"the repair service refused this cycle's configuration: {exc}"
        ) from exc


def run_reconciliation_cycle(
    *,
    broker,
    execution_db_path: Path | str,
    reconciliation_db_path: Path | str,
    run_id: str,
    cycle_lock_path: Path | str | None,
    include_position_scope: bool = True,
    dry_run: bool = False,
    repair_enabled: bool = False,
    repair_owner_id: str | None = None,
    lease_duration_seconds: int = DEFAULT_LEASE_DURATION_SECONDS,
    broker_timeout_seconds: float | None = None,
    lock_wait_seconds: float = 0.0,
    clock: Callable[[], datetime] | None = None,
) -> ReconciliationCycleReport:
    """Run ONE scheduled reconciliation cycle and return its report.

    ``broker`` MUST be a :class:`ReconcileOnlyBroker`. ``cycle_lock_path`` is
    required for a real cycle and is IGNORED (never opened, never created)
    when ``dry_run`` is set -- a dry run has nothing to exclude anybody from.

    ``repair_enabled`` opts the cycle into the existing Issue #104 repair
    path: recovery of pending attempts first, then a repair pass over the run
    just recorded. Only ``auto_repair_candidate`` order discrepancies are
    acted on -- that gate lives in the repair service, is not duplicated here
    and is not weakened here.

    Raises :class:`CycleAlreadyRunningError` when another cycle holds the
    lock (a suppression, not a failure) and :class:`ReconciliationCycleError`
    for a misuse of this API. Detection failures propagate as the store /
    reconciliation errors they already are; a repair pass that stops part-way
    is REPORTED on the report (``repair_error``) rather than raised, because
    the detection result is durable and must still be reported.
    """
    _require_reconcile_only(broker)
    if not isinstance(run_id, str) or not run_id.strip():
        raise ReconciliationCycleError("run_id must be a non-empty string")
    execution_db_path = Path(execution_db_path)
    reconciliation_db_path = Path(reconciliation_db_path)

    if dry_run:
        if repair_enabled:
            raise ReconciliationCycleError(
                "a dry-run cycle cannot repair: repair acts on a RECORDED run, and a "
                "dry run records nothing (use --dry-run without --repair)"
            )
        return _detect_only_cycle(
            broker=broker,
            execution_db_path=execution_db_path,
            reconciliation_db_path=reconciliation_db_path,
            run_id=run_id,
            include_position_scope=include_position_scope,
            clock=clock,
        )

    if cycle_lock_path is None:
        raise ReconciliationCycleError(
            "cycle_lock_path is required for a recording cycle: overlapping cycles are "
            "excluded at entry, and an unguarded scheduled run is not an option"
        )

    with cycle_lock(cycle_lock_path, wait_seconds=lock_wait_seconds):
        return _guarded_cycle(
            broker=broker,
            execution_db_path=execution_db_path,
            reconciliation_db_path=reconciliation_db_path,
            run_id=run_id,
            include_position_scope=include_position_scope,
            repair_enabled=repair_enabled,
            repair_owner_id=repair_owner_id,
            lease_duration_seconds=lease_duration_seconds,
            broker_timeout_seconds=broker_timeout_seconds,
            clock=clock,
        )


def _detect_only_cycle(
    *,
    broker: ReconcileOnlyBroker,
    execution_db_path: Path,
    reconciliation_db_path: Path,
    run_id: str,
    include_position_scope: bool,
    clock: Callable[[], datetime] | None,
) -> ReconciliationCycleReport:
    """The dry-run path: compare and report, touch nothing.

    ``reconciliation_db_path=None`` is handed to the service so the audit DB
    cannot be created or migrated even by accident.
    """
    service = BrokerOrderReconciliationService(
        broker=broker,
        execution_db_path=execution_db_path,
        reconciliation_db_path=None,
        clock=clock,
    )
    run = service.detect(run_id=run_id, include_position_scope=include_position_scope)
    return ReconciliationCycleReport(
        run_id=run_id,
        run=run,
        dry_run=True,
        include_position_scope=include_position_scope,
        repair_enabled=False,
        recorded_to=None,
        repair_skipped_reason="dry_run: nothing was recorded, so there is nothing to repair",
    )


def _blocked_in(recovered) -> tuple[OrderRepairAttempt, ...]:
    return tuple(
        attempt for attempt in recovered if attempt.outcome is RepairOutcome.BLOCKED
    )


def _persisted_blocked_intent_ids(reconciliation_db_path: Path) -> tuple[str, ...]:
    """Takeover ids of BLOCKED intents already durable in the audit DB.

    These are the human-intervention records an EARLIER cycle produced. They
    make every later cycle report not-clean (exit 1) until someone deals with
    them, so the state cannot quietly disappear from an operator's view once
    the cycle that found it has scrolled out of the journal.

    Read by EVERY non-dry cycle, including detection-only ones -- the shipped
    systemd unit runs without ``--repair``, and that is exactly the
    deployment that must not report clean over a parked block.

    They deliberately do NOT gate the repair pass. ``BLOCKED`` is a TERMINAL
    intent status (``ALLOWED_INTENT_TRANSITIONS[BLOCKED]`` is empty) and
    intents are retained forever as the audit trail, so there is no in-band
    way for an operator to clear one. Gating on them would therefore not be
    "stop until resolved" -- it would be "stop forever, on a historical audit
    row", disabling reconciliation repair for every unrelated order with no
    supported way back. Visibility is the part that belongs here; the live
    protections (pending claims, this cycle's BLOCKED results) are what gate.
    """
    return tuple(
        intent.takeover_id
        for intent in reconciliation_store.list_takeover_intents(
            db_path=reconciliation_db_path
        )
        if intent.status is TakeoverIntentStatus.BLOCKED
    )


def _skip_reason(unresolved, blocked) -> str:
    """Why a repair pass was skipped, in the terms an operator must act on.

    A BLOCKED attempt needs a human; a still-leased one only needs time (the
    next cycle takes it over once the lease expires). Saying which is which
    is the difference between "investigate now" and "it will clear itself" --
    and the two are reported independently because they do not imply each
    other (see the gate).
    """
    parts = []
    if blocked:
        parts.append(
            f"{len(blocked)} attempt(s) BLOCKED by the takeover protocol "
            f"({', '.join(sorted(entry.attempt_id for entry in blocked))}) and needing "
            "a human"
        )
    if unresolved:
        tail = (
            ""
            if blocked
            else " -- none of them BLOCKED, so their claim leases are simply still "
            "valid and a later cycle takes them over once the leases expire"
        )
        parts.append(
            f"{len(unresolved)} repair attempt(s) still pending after takeover recovery "
            f"({', '.join(sorted(entry.attempt_id for entry in unresolved))}){tail}"
        )
    return (
        "; ".join(parts) + ". No fresh repair pass was run this cycle -- a new attempt "
        "would take an existing claim's fence over, and a blocked takeover needs a human"
    )


def _skipped_report(
    *,
    run_id: str,
    run: ReconciliationRun,
    include_position_scope: bool,
    reconciliation_db_path: Path,
    recovered,
    skipped_reason: str,
    blocked_intent_ids: tuple[str, ...],
) -> ReconciliationCycleReport:
    """A repair-enabled cycle whose pass was skipped, detection recorded."""
    return ReconciliationCycleReport(
        run_id=run_id,
        run=run,
        dry_run=False,
        include_position_scope=include_position_scope,
        repair_enabled=True,
        recorded_to=reconciliation_db_path,
        recovered_attempts=recovered,
        repair_skipped_reason=skipped_reason,
        blocked_intent_ids=blocked_intent_ids,
    )


def _guarded_cycle(
    *,
    broker: ReconcileOnlyBroker,
    execution_db_path: Path,
    reconciliation_db_path: Path,
    run_id: str,
    include_position_scope: bool,
    repair_enabled: bool,
    repair_owner_id: str | None,
    lease_duration_seconds: int,
    broker_timeout_seconds: float | None,
    clock: Callable[[], datetime] | None,
) -> ReconciliationCycleReport:
    """Recovery -> detection -> repair, under the entry lock."""
    recovered: tuple[OrderRepairAttempt, ...] = ()
    skipped_reason: str | None = None
    repair_error: str | None = None
    attempts: tuple[OrderRepairAttempt, ...] = ()
    blocked_intent_ids: tuple[str, ...] = ()

    # EVERY non-dry cycle reports the blocked intents already in the audit DB,
    # whether or not repair is enabled (PR #137 review r7). The shipped systemd
    # unit runs WITHOUT --repair, so nesting this under ``repair_enabled``
    # would mean the default deployment reports clean forever while a
    # human-intervention record sits in the DB. It is a read of an existing
    # file: no Issue #104 state transition, and nothing is created.
    if reconciliation_db_path.exists():
        blocked_intent_ids = _persisted_blocked_intent_ids(reconciliation_db_path)

    service = None
    if repair_enabled:
        service = _repair_service(
            broker=broker,
            execution_db_path=execution_db_path,
            reconciliation_db_path=reconciliation_db_path,
            owner_id=repair_owner_id,
            lease_duration_seconds=lease_duration_seconds,
            broker_timeout_seconds=broker_timeout_seconds,
            clock=clock,
        )
        # Finish what a crashed cycle left pending BEFORE observing again:
        # a leftover attempt that commits after the new observation would
        # only make that observation stale. A first-ever cycle has no audit
        # DB yet, so there is provably nothing pending -- and asking would
        # mean creating the DB before detection does.
        if reconciliation_db_path.exists():
            try:
                recovered = service.recover_pending_attempts()
            except ReconciliationRepairError as exc:
                # Recovery is fail-closed by contract: a pending reservation
                # whose source run / discrepancy is gone or tampered with is
                # REFUSED, never guessed about. That needs a human, so this
                # cycle does not stack a fresh repair pass on top of it --
                # but detection still runs and is still recorded, because a
                # read-only observation is exactly what the operator needs.
                service = None
                repair_error = (
                    f"takeover recovery refused to resolve a pending attempt "
                    f"({type(exc).__name__}); no repair pass was run this cycle -- the "
                    "pending claim and its takeover intent stay in the audit DB for "
                    "manual review"
                )
        else:
            _LOGGER.info(
                "no reconciliation audit DB at %s yet: no pending repair attempt can "
                "exist, skipping takeover recovery",
                reconciliation_db_path,
            )
    else:
        skipped_reason = "repair not enabled for this cycle (detection only)"

    run = BrokerOrderReconciliationService(
        broker=broker,
        execution_db_path=execution_db_path,
        reconciliation_db_path=reconciliation_db_path,
        clock=clock,
    ).run(run_id=run_id, include_position_scope=include_position_scope)

    if service is not None:
        # THE PENDING GATE, taken as LATE as possible -- immediately before
        # the pass, not before detection (PR #137 review r4: the detection
        # pass was a substantial window between the check and the pass).
        #
        # RECOVERY RETURNING NOTHING DOES NOT MEAN NOTHING IS PROTECTED. It
        # deliberately leaves alone every reservation whose lease is still
        # valid (someone may be driving it right now), and it leaves a
        # BLOCKED one pending on purpose. So the decision is made on what is
        # STILL PENDING, not on what came back.
        #
        # A fresh pass would otherwise walk straight past that boundary: its
        # attempt id is new, and ``register_repair_fence`` REPLACES a fence
        # held by a DIFFERENT attempt by design -- so the very order somebody
        # else has claimed would be re-observed, re-claimed at generation 1
        # and committed. Under a timer that is not a one-off; it is every
        # tick.
        #
        # This is a POLICY gate, not a mutex: a direct repair-service caller
        # that claims in the remaining instant is still possible, and is
        # resolved -- as it always has been -- by the store's fencing, which
        # fails the loser typed (``repair_fence_conflict``) and never lets a
        # wrong write land. See "Concurrency" in the module docstring.
        # BOTH conditions, because neither implies the other (PR #137 review
        # r5). A BLOCKED attempt is USUALLY still pending -- but not when the
        # protocol parked an ORPHANED active intent whose attempt was already
        # finalized (``_report_orphaned_active_intents``): recovery reports
        # BLOCKED and the audit DB has no pending row at all.
        unresolved = reconciliation_store.list_pending_repair_attempts(
            db_path=reconciliation_db_path
        )
        blocked = _blocked_in(recovered)
        if unresolved or blocked:
            return _skipped_report(
                run_id=run_id,
                run=run,
                include_position_scope=include_position_scope,
                reconciliation_db_path=reconciliation_db_path,
                recovered=recovered,
                skipped_reason=_skip_reason(unresolved, blocked),
                blocked_intent_ids=blocked_intent_ids,
            )
        try:
            attempts = service.repair(run_id=run_id, mode=RepairMode.APPLY).attempts
        except ReconciliationRepairRunError as exc:
            # The pass stopped part-way. Its completed attempts are durable
            # in the audit DB and are carried on the error; the detection
            # result is durable too, so the cycle REPORTS instead of losing
            # both. Sanitized like every other repair-side log: the typed
            # class name, never raw exception text.
            attempts = exc.attempts
            repair_error = (
                f"the repair pass over run {run_id!r} stopped part-way "
                f"({type(exc).__name__}); {len(attempts)} attempt(s) completed and are "
                "recorded in the audit DB"
            )
        except ReconciliationRepairError as exc:
            repair_error = (
                f"the repair pass over run {run_id!r} could not start "
                f"({type(exc).__name__})"
            )

    return ReconciliationCycleReport(
        run_id=run_id,
        run=run,
        dry_run=False,
        include_position_scope=include_position_scope,
        repair_enabled=repair_enabled,
        recorded_to=reconciliation_db_path,
        recovered_attempts=recovered,
        repair_attempts=attempts,
        repair_skipped_reason=skipped_reason,
        repair_error=repair_error,
        blocked_intent_ids=blocked_intent_ids,
    )


__all__ = [
    "CycleAlreadyRunningError",
    "CycleLockError",
    "DEFAULT_CYCLE_LOCK_FILENAME",
    "ReconciliationCycleError",
    "ReconciliationCycleReport",
    "cycle_lock",
    "cycle_lock_path_for",
    "run_reconciliation_cycle",
]
