"""Composition roots and mode executors for the Live Execution runner
(Issue #131: Fake and Shadow; Issue #132: OANDA Demo).

*** WIRING ONLY. Nothing here decides whether an order may be sent, and
nothing here writes a store directly: each executor resolves ONE work item
and hands it to the production service that owns the decision
(``LiveOrderSubmissionService`` for ``fake`` / ``demo``,
``run_production_shadow_cycle`` for ``shadow``). There is no "simple
submit", no "simple state update" and no "convenient repair" path. ***

    fake   : execution store (phase 'created')
               -> LiveOrderSubmissionService.submit()
                    -> Safety Gate (incl. the kill switch, EVERY call)
                    -> durable decision + read-back
                    -> Submission Journal
                    -> created -> submitting (won exactly once)
                    -> FakeBroker
                    -> Journal + execution store outcome
    demo   : execution store (phase 'created')
               -> LiveOrderSubmissionService.submit()
                    -> ... the IDENTICAL chain ...
                    -> OandaDemoAdapter (practice endpoint, allowlisted)
                    -> Journal + execution store outcome
    shadow : paper signal store (executable signals)
               -> run_production_shadow_cycle()
                    -> shadow claim (idempotency)
                    -> Safety Gate (shadow evaluation)
                    -> durable decision + read-back
                    -> shadow result (intended order, never submitted)

Fake / Shadow / Demo are three different things
-----------------------------------------------

- ``fake``   -- an IN-MEMORY broker. Full submission semantics, no network,
                no account. Persists to the execution store / journal like
                any submission, which is what makes it the state-machine
                rehearsal. Kill switch: any ``SafetyStateProvider``.
- ``shadow`` -- NO broker exists on the path at all. It records what WOULD
                have been ordered into its OWN store (``shadow_results``);
                it never touches the execution order store, the submission
                journal or the order state machine, and it produces no
                fills, positions or PnL (that is Paper Execution's separate
                layer).
- ``demo``   -- a REAL OANDA practice account over HTTPS. Same stores, same
                state machine and same journal as ``fake``; different
                failure modes (timeouts / unknown outcomes are real) and a
                MANDATORY durable Kill Switch reader. It is composed only
                from a ``DemoRuntimeGrant`` the demo activation lock issued.

The three never share persistence: shadow writes ``shadow_results.sqlite3``
and nothing else; fake and demo write the execution order store + submission
journal, and an operator keeps them apart by pointing the demo runner at its
own data directory (docs/operations/operations.md).

Why the executors are this thin
-------------------------------

Every "is this allowed / is this a duplicate / what does the outcome mean"
question is answered inside those services, using the durable stores as the
only memory. An executor therefore holds NO state between items and NO
state between cycles, and it never inspects a state to decide whether to
re-send: a fake item is handed to ``submit()`` and the boundary refuses it
(``duplicate_submission``, ``intent_superseded``, ``reissue_clearance_
invalid``, ``kill_switch_enabled``, ...) whenever it must; a shadow item is
handed to the claim, which is what makes a re-run a duplicate rather than a
second record. Restart safety is a property of those stores, not of this
module.

Broker exposure
---------------

The submission executors receive the SUBMISSION SERVICE, not a broker and
not a ``BrokerConnection``: there is no attribute on them through which a
broker call could be made. The shadow executor has no broker-shaped argument
at all (the shadow service structurally cannot accept one). Each composition
root hands its connection straight to ``build_reissue_boundary`` -- the only
sanctioned builder of the submission service -- and keeps no reference of
its own. Neither module in the runner pair imports a broker implementation
or composes a connection; both facts are pinned by
``execution/tests/test_submission_boundary.py``.

Modes
-----

Each composition root refuses any connection whose DERIVED mode is not its
own (a demo or live adapter can never be wired into the fake runner, and
vice versa), and ``ExecutionRunner`` refuses any executor outside
``RUNNER_SUPPORTED_MODES``. ``build_demo_execution_runner`` goes further: it
accepts no loose connection at all, only a ``DemoRuntimeGrant`` issued by
``execution/demo_lock.py``, and it builds the gate's kill-switch reader
itself from the granted store path -- so a demo runner cannot exist without
the demo locks having held, and cannot be handed a provider that answers
"off" from memory. ``live`` has no composition root here at all.

Dry run
-------

``dry_run=True`` is wired through to the runner, which calls ``plan()``
instead of ``execute()``, AND suppresses store initialization here: no DB
is created, no schema migrated, nothing written -- for either mode.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Callable

from execution import execution_store
from execution.demo_lock import DemoRuntimeGrant
from execution.kill_switch_store import PersistentKillSwitchStateProvider
from execution.live_runner import (
    BrokerContact,
    DEFAULT_MAX_ITEMS_PER_CYCLE,
    ExecutionRunner,
    RunnerItemOutcome,
    RunnerItemStatus,
    RunnerModeError,
)
from execution.live_submission import LiveOrderSubmissionService
from execution.reissue_composition import build_reissue_boundary
from execution.reissue_service import ReissuePolicy
from execution.safety_gate import (
    BrokerConnection,
    ExecutionMode,
    SafetyGate,
    SafetyPolicy,
    SafetyStateProvider,
)
from execution.safety_store import SQLiteSafetyDecisionStore
from execution.shadow_composition import run_production_shadow_cycle
from execution.shadow_config import ShadowExecutionConfig
from execution.shadow_execution import ShadowCycleStatus
from execution.shadow_providers import (
    AccountSnapshotProvider,
    ExecutableSignalLister,
    MarketSnapshotProvider,
    StoredSignalProvider,
)
from execution.submission_journal import SubmissionJournal

_LOGGER = logging.getLogger(__name__)


def _require_provider(provider, method: str, name: str) -> None:
    if not callable(getattr(provider, method, None)):
        raise ValueError(f"{name} must provide a callable {method}()")
    source_id = getattr(provider, "source_id", None)
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError(f"{name} must expose a non-empty source_id, got {source_id!r}")


class SubmissionCycleExecutor:
    """Base executor for every mode that SUBMITS: pending intents -> the ONE
    sanctioned submission boundary.

    Work queue: ``execution_store.list_pending_execution_requests()`` (phase
    ``created``), read strictly read-only from the SAME order DB the
    submission service writes -- the path is taken from the service itself,
    so a driver cannot be pointed at a different file.

    The body is IDENTICAL for every submitting mode on purpose (Issue #132):
    the fake path and the demo path must not be able to drift apart, because
    the whole value of rehearsing on ``fake`` is that ``demo`` then runs the
    same code. The subclasses differ in exactly one declaration --
    :attr:`REQUIRED_MODE`, the mode of the submission boundary they accept --
    and a boundary of any other mode is refused at construction, before
    anything can be selected.
    """

    #: The submission boundary mode this executor exists for. A subclass
    #: without it cannot be instantiated (no default: an executor that
    #: accepts "whatever mode it is handed" is the bug this prevents).
    REQUIRED_MODE: ExecutionMode | None = None

    def __init__(
        self,
        *,
        submission: LiveOrderSubmissionService,
        market_provider: MarketSnapshotProvider,
    ) -> None:
        if not isinstance(self.REQUIRED_MODE, ExecutionMode):
            raise RunnerModeError(  # pragma: no cover - guards a future subclass
                f"{type(self).__name__} must declare REQUIRED_MODE; a submission "
                "executor without a fixed mode would accept any boundary"
            )
        if not isinstance(submission, LiveOrderSubmissionService):
            raise ValueError(
                f"submission must be a LiveOrderSubmissionService, got {submission!r}"
            )
        if submission.execution_mode is not self.REQUIRED_MODE:
            # Fail closed on the mode BEFORE anything can be selected: an
            # executor is bound to ONE mode's boundary, so a runner can never
            # be wired onto a connection of a different mode (Issue #131 /
            # #132). Live operation remains a separate, un-released path with
            # no executor at all.
            raise RunnerModeError(
                f"the {self.REQUIRED_MODE.value} cycle executor requires a "
                f"{self.REQUIRED_MODE.value!r} submission boundary, got "
                f"{submission.execution_mode.value!r}"
            )
        _require_provider(market_provider, "get_market_snapshot", "market_provider")
        self._submission = submission
        self._market_provider = market_provider
        self._order_db_path = Path(submission.order_db_path)

    @property
    def execution_mode(self) -> ExecutionMode:
        return self.REQUIRED_MODE

    @property
    def source_id(self) -> str:
        return f"execution_orders:{self._order_db_path}"

    @property
    def error_broker_contact(self) -> BrokerContact:
        """An exception escaping ``submit()`` means the broker call is the
        thing that is unresolved (the boundary converts everything BEFORE
        the call into a typed rejection). The attempt stays visible through
        the submission journal / ``list_unresolved_submissions()`` and is
        resolved by reconciliation -- never re-sent."""
        return BrokerContact.UNRESOLVED

    def select(self, *, limit: int) -> tuple[str, ...]:
        rows = execution_store.list_pending_execution_requests(
            limit=limit, db_path=self._order_db_path
        )
        return tuple(row["execution_request_id"] for row in rows)

    def execute(self, item_id: str) -> RunnerItemOutcome:
        """Submit ONE registered intent through ``LiveOrderSubmissionService``.

        Everything the boundary refuses (gate rejection, kill switch,
        duplicate submission, journal / store failure, superseded intent,
        missing clearance) comes back as ``(decision, None)`` -- provably
        without a broker call -- and is reported as ``REJECTED`` with the
        machine-readable codes. An exception is left to propagate: the
        runner classifies it as UNRESOLVED, which is the only honest answer
        after the broker may have been reached.
        """
        try:
            row = execution_store.get_execution_order(item_id, db_path=self._order_db_path)
        except Exception as exc:
            # Read failure BEFORE the boundary was entered: provably nothing
            # was sent, and we refuse to guess the intent's symbol.
            return RunnerItemOutcome(
                item_id=item_id,
                status=RunnerItemStatus.FAILED,
                broker_contact=BrokerContact.NOT_SENT,
                detail=f"execution store lookup failed: {type(exc).__name__}: {exc}",
            )
        if row is None:
            return RunnerItemOutcome(
                item_id=item_id,
                status=RunnerItemStatus.SKIPPED,
                broker_contact=BrokerContact.NOT_SENT,
                detail=(
                    "no persisted execution order exists for this id any more; nothing "
                    "was submitted"
                ),
            )
        try:
            market = self._market_provider.get_market_snapshot(row["symbol"])
        except Exception as exc:
            # No market snapshot means the gate cannot judge staleness /
            # spread, so there is nothing to submit. Fail closed, before the
            # boundary is entered.
            return RunnerItemOutcome(
                item_id=item_id,
                status=RunnerItemStatus.FAILED,
                broker_contact=BrokerContact.NOT_SENT,
                detail=(
                    f"market snapshot for {row['symbol']!r} is unavailable "
                    f"({type(exc).__name__}: {exc}); nothing was submitted"
                ),
            )
        decision, result = self._submission.submit(item_id, market=market)
        if result is None:
            return RunnerItemOutcome(
                item_id=item_id,
                status=RunnerItemStatus.REJECTED,
                broker_contact=BrokerContact.NOT_SENT,
                detail=(
                    "the submission boundary refused before any broker call: "
                    + "; ".join(
                        f"{entry.code.value}: {entry.message}" for entry in decision.rejections
                    )
                ),
                rejection_codes=decision.rejection_codes,
            )
        return RunnerItemOutcome(
            item_id=item_id,
            status=RunnerItemStatus.SUBMITTED,
            broker_contact=BrokerContact.SENT,
            detail=(
                f"submitted via the sanctioned boundary; broker state "
                f"{result.state.value!r} broker_order_id={result.broker_order_id!r}"
            ),
        )

    def plan(self, item_id: str) -> RunnerItemOutcome:
        """Dry run: report whether this intent WOULD be submitted.

        Deliberately stops before the Safety Gate. A submission cannot be
        evaluated without durably recording the decision and journaling the
        attempt, so "evaluate but skip the writes" would be a weakened copy
        of the submission path -- exactly the shortcut Issue #131 forbids.
        "It is only a FakeBroker" / "it is only a practice account" is not a
        licence to call a broker in a dry run either. The only I/O here is a
        read-only store probe that never creates a file.
        """
        row = execution_store.get_execution_order(item_id, db_path=self._order_db_path)
        if row is None:
            return RunnerItemOutcome(
                item_id=item_id,
                status=RunnerItemStatus.SKIPPED,
                broker_contact=BrokerContact.NOT_SENT,
                detail="no persisted execution order exists for this id any more",
            )
        if row["submission_phase"] != execution_store.PHASE_CREATED:
            return RunnerItemOutcome(
                item_id=item_id,
                status=RunnerItemStatus.SKIPPED,
                broker_contact=BrokerContact.NOT_SENT,
                detail=(
                    f"the intent is no longer pending (phase "
                    f"{row['submission_phase']!r}); a submit attempt already exists"
                ),
            )
        return RunnerItemOutcome(
            item_id=item_id,
            status=RunnerItemStatus.PLANNED,
            broker_contact=BrokerContact.NOT_SENT,
            detail=(
                f"would submit {row['symbol']} {row['side']}/{row['order_type']} "
                f"qty={row['quantity']} (client_order_id={row['client_order_id']!r}) "
                "through the sanctioned boundary; nothing was evaluated or persisted"
            ),
        )


class FakeSubmissionCycleExecutor(SubmissionCycleExecutor):
    """The ``fake`` mode executor (Issue #131): an IN-MEMORY broker, full
    submission semantics, no network and no account."""

    REQUIRED_MODE = ExecutionMode.FAKE


class DemoSubmissionCycleExecutor(SubmissionCycleExecutor):
    """The ``demo`` mode executor (Issue #132): the SAME body, pointed at a
    real OANDA practice account.

    Nothing is relaxed for demo and nothing is added: the differences that
    matter (a network that can time out, an outcome that can be genuinely
    UNKNOWN, a mandatory durable Kill Switch) live in the submission
    boundary and the demo activation lock, which is exactly where they
    belong. An executor that "handled demo specially" would be a second
    submission path.
    """

    REQUIRED_MODE = ExecutionMode.DEMO


class ShadowCycleExecutor:
    """The ``shadow`` mode executor: stored signals -> the production shadow
    cycle. NO broker exists anywhere on this path.

    Work queue: executable stored signals (newest first), handed to the
    claim UNFILTERED -- the claim is the only component that may decide
    what an already-seen signal means (payload-equal -> ``DUPLICATE``,
    payload-different -> conflict). See :meth:`select`.
    """

    def __init__(
        self,
        *,
        signal_provider: StoredSignalProvider,
        signal_lister: ExecutableSignalLister,
        config: ShadowExecutionConfig,
        market_provider: MarketSnapshotProvider,
        account_provider: AccountSnapshotProvider,
        shadow_db_path: str | Path,
        decision_db_path: str | Path,
        clock: Callable[[], datetime],
        owner_id: str | None = None,
    ) -> None:
        _require_provider(signal_provider, "get_signal", "signal_provider")
        _require_provider(signal_lister, "list_executable_signal_ids", "signal_lister")
        if not isinstance(config, ShadowExecutionConfig):
            raise ValueError(f"config must be a ShadowExecutionConfig, got {config!r}")
        _require_provider(market_provider, "get_market_snapshot", "market_provider")
        _require_provider(account_provider, "get_account_snapshot", "account_provider")
        for name, value in (
            ("shadow_db_path", shadow_db_path),
            ("decision_db_path", decision_db_path),
        ):
            if not isinstance(value, (str, Path)):
                raise ValueError(f"{name} must be a filesystem path, got {type(value).__name__}")
        if not callable(clock):
            raise ValueError(f"clock must be a callable returning a UTC datetime, got {clock!r}")
        if owner_id is not None and (not isinstance(owner_id, str) or not owner_id.strip()):
            raise ValueError(f"owner_id must be a non-empty string or None, got {owner_id!r}")
        self._signal_provider = signal_provider
        self._signal_lister = signal_lister
        self._config = config
        self._market_provider = market_provider
        self._account_provider = account_provider
        self._shadow_db_path = Path(shadow_db_path)
        self._decision_db_path = Path(decision_db_path)
        self._clock = clock
        self._owner_id = owner_id

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.SHADOW

    @property
    def source_id(self) -> str:
        return self._signal_lister.source_id

    @property
    def error_broker_contact(self) -> BrokerContact:
        """Shadow owns no broker AT ALL (the service has no parameter that
        could accept one), so an exception on this path provably sent
        nothing."""
        return BrokerContact.NOT_SENT

    def select(self, *, limit: int) -> tuple[str, ...]:
        """The candidate signals for one cycle -- UNFILTERED (PR #135 Codex
        review P1).

        An earlier revision dropped candidates that already had a completed
        shadow record. That was meant as an efficiency measure, but it
        silently removed a documented fail-loud behaviour: when the shadow
        CONFIGURATION changes (a different order quantity for the same
        signal, say), the canonical payload no longer matches the recorded
        one, and ``claim_shadow_cycle`` must raise
        ``ShadowIdentityConflictError`` rather than let the divergence pass
        (docs/architecture/shadow_execution.md). A signal-id-only filter
        answered "nothing to do" for exactly that case.

        Making the filter payload-aware would mean re-deriving the request,
        the client order id and the canonical payload here -- i.e. copying
        the store's identity rules into the runner, which is what this
        module exists to avoid. So every candidate goes to the claim, which
        is the one component allowed to classify it: payload-equal ->
        ``duplicate`` (nothing evaluated, nothing written), payload-different
        -> conflict, which surfaces as a failed item.
        """
        return tuple(self._signal_lister.list_executable_signal_ids(limit=limit))

    def execute(self, item_id: str) -> RunnerItemOutcome:
        outcome = self._cycle(item_id, dry_run=False)
        if outcome.status is ShadowCycleStatus.RECORDED:
            result = outcome.result
            return RunnerItemOutcome(
                item_id=item_id,
                status=RunnerItemStatus.RECORDED,
                broker_contact=BrokerContact.NOT_SENT,
                detail=(
                    f"shadow record {result.shadow_result_id} completed "
                    f"(gate {'allowed' if result.allowed else 'rejected'}, "
                    f"intended client_order_id="
                    f"{result.intended_order.client_order_id!r}); nothing was sent"
                ),
                rejection_codes=result.decision.rejection_codes,
            )
        if outcome.status is ShadowCycleStatus.DUPLICATE:
            return RunnerItemOutcome(
                item_id=item_id,
                status=RunnerItemStatus.DUPLICATE,
                broker_contact=BrokerContact.NOT_SENT,
                detail=(
                    "a payload-equal shadow cycle already exists (completed record or "
                    "in-flight claim); nothing was evaluated, written or sent"
                ),
            )
        # DRY_RUN out of execute() would mean the side-effecting path silently
        # ran the no-side-effect one: refuse it as a failure rather than
        # reporting a cycle that did not happen.
        return RunnerItemOutcome(  # pragma: no cover - defensive
            item_id=item_id,
            status=RunnerItemStatus.FAILED,
            broker_contact=BrokerContact.NOT_SENT,
            detail=f"unexpected shadow cycle status {outcome.status.value!r} from execute()",
        )

    def plan(self, item_id: str) -> RunnerItemOutcome:
        """Dry run: the SAME production evaluation with zero side effects
        (``ShadowExecutionService.dry_run_cycle`` -- read-only probes only,
        no claim, no record, no DB creation)."""
        outcome = self._cycle(item_id, dry_run=True)
        if outcome.status is not ShadowCycleStatus.DRY_RUN:
            return RunnerItemOutcome(  # pragma: no cover - defensive
                item_id=item_id,
                status=RunnerItemStatus.FAILED,
                broker_contact=BrokerContact.NOT_SENT,
                detail=f"unexpected shadow cycle status {outcome.status.value!r} from plan()",
            )
        result = outcome.result
        return RunnerItemOutcome(
            item_id=item_id,
            status=RunnerItemStatus.PLANNED,
            broker_contact=BrokerContact.NOT_SENT,
            detail=(
                f"would record a shadow cycle for {result.request.symbol} "
                f"{result.request.side.value}/{result.request.order_type.value} "
                f"qty={result.request.quantity} (gate "
                f"{'allowed' if result.allowed else 'rejected'}); nothing was persisted"
            ),
            rejection_codes=result.decision.rejection_codes,
        )

    def _cycle(self, signal_id: str, *, dry_run: bool):
        return run_production_shadow_cycle(
            signal_id,
            signal_provider=self._signal_provider,
            config=self._config,
            market_provider=self._market_provider,
            account_provider=self._account_provider,
            shadow_db_path=self._shadow_db_path,
            decision_db_path=self._decision_db_path,
            clock=self._clock,
            owner_id=self._owner_id,
            dry_run=dry_run,
        )


def build_fake_execution_runner(
    *,
    connection: BrokerConnection,
    policy: SafetyPolicy,
    state_provider: SafetyStateProvider,
    market_provider: MarketSnapshotProvider,
    order_db_path: str | Path,
    decision_db_path: str | Path,
    journal_db_path: str | Path,
    clock: Callable[[], datetime],
    reissue_policy: ReissuePolicy | None = None,
    max_items_per_cycle: int = DEFAULT_MAX_ITEMS_PER_CYCLE,
    dry_run: bool = False,
    stop_event: Event | None = None,
    runner_id: str = "fake-execution-runner",
) -> ExecutionRunner:
    """Compose the ``fake`` mode runner (see the module docstring).

    The connection's mode is DERIVED from the broker instance, so passing a
    non-fake connection is refused here -- a demo or live adapter cannot be
    driven by THIS runner. Demo has its own composition root
    (``build_demo_execution_runner``, which takes a ``DemoRuntimeGrant``
    issued by ``execution/demo_lock.py``), and live operation has no
    composition root here at all: it additionally requires the live
    activation lock and remains un-released.

    ``state_provider`` is the gate's kill-switch source and is consulted on
    EVERY submission (the gate re-reads it per evaluation); pass
    ``PersistentKillSwitchStateProvider`` to make a persisted stop survive
    restarts, or ``StaticSafetyState`` for fixed verification runs.

    Unless ``dry_run`` is set, the sealed audit stores the boundary writes
    (safety decisions, submission journal) are initialized here -- an
    uninitialized store is fail-closed at submit time, which would look like
    a policy rejection instead of a wiring mistake. A DRY RUN initializes
    NOTHING: no file is created and no schema migrated.
    """
    if not isinstance(connection, BrokerConnection):
        raise ValueError(f"connection must be a BrokerConnection, got {connection!r}")
    if connection.execution_mode is not ExecutionMode.FAKE:
        raise RunnerModeError(
            f"the Live Execution runner may only be composed with a 'fake' connection "
            f"here, got {connection.execution_mode.value!r}; a demo connection is "
            "composed by build_demo_execution_runner() from a DemoRuntimeGrant "
            "(execution/demo_lock.py), and 'live' has no composition root at all"
        )
    if not isinstance(policy, SafetyPolicy):
        raise ValueError(f"policy must be a SafetyPolicy, got {policy!r}")
    if not callable(getattr(state_provider, "is_kill_switch_enabled", None)):
        raise ValueError(
            "state_provider must expose a callable is_kill_switch_enabled() (the gate "
            "consults it on every evaluation)"
        )
    gate = SafetyGate(policy=policy, state_provider=state_provider, clock=clock)
    if not dry_run:
        SQLiteSafetyDecisionStore(Path(decision_db_path)).initialize()
        SubmissionJournal(Path(journal_db_path)).initialize()
    boundary = build_reissue_boundary(
        connection=connection,
        gate=gate,
        current_policy=policy,
        reissue_policy=reissue_policy if reissue_policy is not None else ReissuePolicy(),
        decision_db_path=decision_db_path,
        order_db_path=order_db_path,
        journal_db_path=journal_db_path,
        clock=clock,
    )
    executor = FakeSubmissionCycleExecutor(
        submission=boundary.submission, market_provider=market_provider
    )
    _LOGGER.info(
        "composed the fake execution runner: target=%s order_db=%s dry_run=%s",
        connection.connection_target,
        Path(order_db_path),
        dry_run,
    )
    return ExecutionRunner(
        executor=executor,
        clock=clock,
        max_items_per_cycle=max_items_per_cycle,
        dry_run=dry_run,
        stop_event=stop_event,
        runner_id=runner_id,
    )


def build_demo_execution_runner(
    *,
    grant: DemoRuntimeGrant,
    market_provider: MarketSnapshotProvider,
    order_db_path: str | Path,
    decision_db_path: str | Path,
    journal_db_path: str | Path,
    clock: Callable[[], datetime],
    reissue_policy: ReissuePolicy | None = None,
    max_items_per_cycle: int = DEFAULT_MAX_ITEMS_PER_CYCLE,
    dry_run: bool = False,
    stop_event: Event | None = None,
    runner_id: str = "demo-execution-runner",
) -> ExecutionRunner:
    """Compose the ``demo`` mode runner (Issue #132).

    The ONLY input that authorizes anything is ``grant`` -- a
    :class:`~execution.demo_lock.DemoRuntimeGrant`, which
    ``execution/demo_lock.py`` issues only after every demo lock held
    (configured mode, demo credentials, the allowlisted practice endpoint,
    demo/live separation, the pinned account identity, a policy allowing the
    demo target, and a verified-OFF Kill Switch). There is deliberately NO
    ``connection`` / ``policy`` / ``state_provider`` parameter:

    - the connection cannot be swapped for a differently-composed one;
    - the gate enforces the SAME policy whose demo target the lock checked,
      not a second policy the caller happened to build;
    - the kill-switch reader is constructed HERE, over the granted store
      path, as the durable ``PersistentKillSwitchStateProvider`` -- which
      the submission boundary independently REQUIRES for every real-broker
      mode, so an in-memory "off" cannot be wired in even by mistake.

    The gate re-reads that provider on EVERY evaluation, so a Kill Switch
    flipped while the runner is resident stops the very next submission.

    Unless ``dry_run`` is set, the sealed audit stores the boundary writes
    (safety decisions, submission journal) are initialized here. A DRY RUN
    initializes NOTHING: no file is created and no schema migrated, and the
    runner calls ``plan()``, which never reaches a broker.
    """
    if not isinstance(grant, DemoRuntimeGrant):
        raise ValueError(
            "grant must be a DemoRuntimeGrant issued by execution/demo_lock.py "
            f"(a demo runner is never composed from loose parts), got {grant!r}"
        )
    gate = SafetyGate(
        policy=grant.policy,
        state_provider=PersistentKillSwitchStateProvider(grant.kill_switch_db_path),
        clock=clock,
    )
    if not dry_run:
        SQLiteSafetyDecisionStore(Path(decision_db_path)).initialize()
        SubmissionJournal(Path(journal_db_path)).initialize()
    boundary = build_reissue_boundary(
        connection=grant.connection,
        gate=gate,
        current_policy=grant.policy,
        reissue_policy=reissue_policy if reissue_policy is not None else ReissuePolicy(),
        decision_db_path=decision_db_path,
        order_db_path=order_db_path,
        journal_db_path=journal_db_path,
        clock=clock,
    )
    executor = DemoSubmissionCycleExecutor(
        submission=boundary.submission, market_provider=market_provider
    )
    _LOGGER.info(
        "composed the demo execution runner: target=%s order_db=%s kill_switch_db=%s "
        "dry_run=%s",
        grant.connection.connection_target,
        Path(order_db_path),
        grant.kill_switch_db_path,
        dry_run,
    )
    return ExecutionRunner(
        executor=executor,
        clock=clock,
        max_items_per_cycle=max_items_per_cycle,
        dry_run=dry_run,
        stop_event=stop_event,
        runner_id=runner_id,
    )


def build_shadow_execution_runner(
    *,
    signal_provider: StoredSignalProvider,
    signal_lister: ExecutableSignalLister,
    config: ShadowExecutionConfig,
    market_provider: MarketSnapshotProvider,
    account_provider: AccountSnapshotProvider,
    shadow_db_path: str | Path,
    decision_db_path: str | Path,
    clock: Callable[[], datetime],
    owner_id: str | None = None,
    max_items_per_cycle: int = DEFAULT_MAX_ITEMS_PER_CYCLE,
    dry_run: bool = False,
    stop_event: Event | None = None,
    runner_id: str = "shadow-execution-runner",
) -> ExecutionRunner:
    """Compose the ``shadow`` mode runner (see the module docstring).

    No broker argument exists, and the shadow stores are created by the
    production cycle itself (and NOT created at all on the dry-run path), so
    this root has nothing to initialize.
    """
    executor = ShadowCycleExecutor(
        signal_provider=signal_provider,
        signal_lister=signal_lister,
        config=config,
        market_provider=market_provider,
        account_provider=account_provider,
        shadow_db_path=shadow_db_path,
        decision_db_path=decision_db_path,
        clock=clock,
        owner_id=owner_id,
    )
    _LOGGER.info(
        "composed the shadow execution runner: config=%s shadow_db=%s dry_run=%s",
        config.config_id,
        Path(shadow_db_path),
        dry_run,
    )
    return ExecutionRunner(
        executor=executor,
        clock=clock,
        max_items_per_cycle=max_items_per_cycle,
        dry_run=dry_run,
        stop_event=stop_event,
        runner_id=runner_id,
    )
