"""Fixed-scenario runner for the Execution layer (Issue #77).

*** NOTHING HERE PLACES A REAL ORDER AND NOTHING HERE RE-IMPLEMENTS AN
EXECUTION DECISION. The runner only wires fixed scenario data
(``execution/scenarios/*.json``, validated by ``execution.scenario_models``)
into the PRODUCTION code path -- ``LiveOrderSubmissionService`` /
``SafetyGate`` / ``execution_store`` / ``FakeBroker`` -- then collects the
actual outcome from the store, the broker and the return values and compares
it against the scenario's ``expected`` block. Safety-gate conditions, state
transitions and idempotency are judged exclusively by the production
modules; a runner that disagreed with them could only ever FAIL a scenario,
never re-decide it. ***

Isolation: every scenario runs against its own temporary directory holding a
fresh ``orders.sqlite3`` + ``safety.sqlite3`` (paths are injected explicitly;
the ``EXECUTION_ORDER_DB_PATH`` / ``SAFETY_GATE_DECISION_DB_PATH`` defaults
are never consulted), and an in-memory FakeBroker. No network, no Discord,
no S3, no ``.env``, no state files, no production DB. Scenarios are
independent of each other and of execution order.

Determinism: the runner injects a fixed clock (``NOW`` -- equal to
FakeBroker's deterministic clock base, whose event ticks strictly follow it),
so timestamps never vary per run and expectations never compare "current
time". DB-assigned values (history row ids) are not comparable at all.

Fault injection (Issue #77): faults are armed by substituting the named
``execution_store`` function for the duration of ONE scenario (dependency
substitution -- the recommended monkeypatch style; no hidden env-var failure
modes in production code). ``after_calls`` lets a fault pass through the
first N calls, so e.g. the history-append boundary can fail at the
result-recording write while the earlier ``mark_submitting`` write succeeds.

Concurrency scenarios start real worker threads that block on a
``threading.Barrier`` immediately before hitting the production API, so both
workers cross the contested boundary together -- the race is decided by the
store's ``BEGIN IMMEDIATE`` serialization / UNIQUE constraints exactly as in
a multi-process deployment, never by sleep timing.

CLI (``python -m execution.scenario_runner``): exit code 0 only when every
scenario PASSes; any FAIL (expected/actual mismatch) or ERROR (unexpected
exception) exits 1; usage or scenario-schema problems exit 2.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sqlite3
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from broker.broker_models import AccountState, BrokerError, OrderSide, OrderState, OrderType
from broker.fake_broker import (
    FailCancellation,
    FailCommunication,
    FakeBroker,
    FillImmediately,
    PartiallyFill,
    PartiallyFillThenReissue,
    RejectOrder,
    StayPending,
    TimeoutCancellation,
    TimeoutOrder,
)
from execution import execution_store, shadow_store
from execution.cli_streams import encoding_safe_streams
from execution.execution_models import ExecutionRequest
from execution.live_submission import LiveOrderSubmissionService
from execution.safety_gate import (
    AccountSafetySnapshot,
    BrokerConnection,
    ExecutionMode,
    MarketSafetySnapshot,
    SafetyGate,
    SafetyPolicy,
    StaticSafetyState,
)
from execution.safety_store import SafetyStoreError, SQLiteSafetyDecisionStore
from execution.reissue_clearance import ReissueClearanceService, ReissueClearanceError
from execution.reissue_composition import build_reissue_boundary
from execution.reissue_service import ReissueEligibilityError, ReissuePolicy
from execution.shadow_composition import run_production_shadow_cycle
from execution.shadow_config import parse_shadow_execution_config
from execution.shadow_execution import ShadowCycleStatus, ShadowExecutionService
from execution.shadow_models import INPUT_ORIGIN_SCENARIO_FIXTURE, ShadowProvenance
from execution.submission_journal import SubmissionJournal
from execution.shadow_providers import (
    PaperSignalDbSignalProvider,
    StaticAccountSnapshotProvider,
    StaticMarketSnapshotProvider,
)
from execution.scenario_models import (
    DEFAULT_SCENARIO_DIR,
    FAULT_BOUNDARIES,
    ScenarioDefinition,
    ScenarioDefinitionError,
    ScenarioStep,
    load_scenarios,
    resolve_primary_id,
    scenario_files,
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE_ERROR = 2

# Fixed evaluation clock: equal to FakeBroker's deterministic clock BASE, so
# broker-side event timestamps (base + n seconds) never precede the stored
# ``updated_at_utc`` the store's monotonicity checks compare against.
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

_UNKNOWN_REASON = "scenario reconciliation observation"
_MARK_SUBMITTING_REASON = "scenario: another worker won the transition"

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_ERROR = "ERROR"


@dataclass(frozen=True)
class Mismatch:
    field_name: str
    expected: object
    actual: object

    def render(self) -> str:
        return f"{self.field_name}: expected {self.expected!r}, actual {self.actual!r}"


@dataclass
class ScenarioOutcome:
    scenario_id: str
    description: str
    status: str
    mismatches: list[Mismatch] = field(default_factory=list)
    error: str | None = None
    expected_summary: dict = field(default_factory=dict)
    actual_summary: dict = field(default_factory=dict)
    duration_seconds: float = 0.0

    def as_json(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "description": self.description,
            "status": self.status,
            "mismatches": [
                {"field": m.field_name, "expected": _jsonable(m.expected), "actual": _jsonable(m.actual)}
                for m in self.mismatches
            ],
            "error": self.error,
            "expected": _jsonable(self.expected_summary),
            "actual": _jsonable(self.actual_summary),
            "duration_seconds": round(self.duration_seconds, 6),
        }


@dataclass
class RunReport:
    outcomes: list[ScenarioOutcome]
    duration_seconds: float

    @property
    def passed(self) -> list[ScenarioOutcome]:
        return [o for o in self.outcomes if o.status == STATUS_PASS]

    @property
    def failed(self) -> list[ScenarioOutcome]:
        return [o for o in self.outcomes if o.status == STATUS_FAIL]

    @property
    def errored(self) -> list[ScenarioOutcome]:
        return [o for o in self.outcomes if o.status == STATUS_ERROR]

    @property
    def all_passed(self) -> bool:
        return len(self.passed) == len(self.outcomes)


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


# --- construction from scenario data -----------------------------------------


def _build_request(spec: dict) -> ExecutionRequest:
    return ExecutionRequest(
        execution_request_id=spec["execution_request_id"],
        signal_id=spec["signal_id"],
        strategy_id=spec["strategy_id"],
        symbol=spec["symbol"],
        side=OrderSide(spec["side"]),
        order_type=OrderType(spec["order_type"]),
        quantity=Decimal(spec["quantity"]),
        requested_at_utc=NOW,
        requested_price=_decimal_or_none(spec.get("requested_price")),
        stop_loss_price=_decimal_or_none(spec.get("stop_loss_price")),
        take_profit_price=_decimal_or_none(spec.get("take_profit_price")),
    )


def _decimal_or_none(value):
    return None if value is None else Decimal(value)


def _build_submit_script(spec: dict):
    behaviour = spec["behavior"]
    if behaviour == "fill_immediately":
        return FillImmediately(
            price=_decimal_or_none(spec.get("price")),
            broker_order_id=spec.get("broker_order_id"),
        )
    if behaviour == "stay_pending":
        return StayPending(broker_order_id=spec.get("broker_order_id"))
    if behaviour == "partially_fill":
        return PartiallyFill(
            fills=tuple((Decimal(price), Decimal(quantity)) for price, quantity in spec["fills"]),
            broker_order_id=spec.get("broker_order_id"),
        )
    if behaviour == "partially_fill_then_reissue":
        return PartiallyFillThenReissue(
            fills=tuple((Decimal(price), Decimal(quantity)) for price, quantity in spec["fills"]),
            broker_order_id=spec.get("broker_order_id"),
            reissued_broker_order_id=spec.get("reissued_broker_order_id"),
        )
    if behaviour == "reject":
        kwargs = {}
        if "reason" in spec:
            kwargs["reason"] = spec["reason"]
        if "category" in spec:
            from broker.broker_models import RejectionCategory

            kwargs["category"] = RejectionCategory(spec["category"])
        return RejectOrder(**kwargs)
    if behaviour == "timeout":
        return TimeoutOrder()
    if behaviour == "fail_communication":
        return FailCommunication()
    raise ScenarioDefinitionError(f"unknown submit behaviour {behaviour!r}")  # guarded by validation


def _build_cancel_script(spec: dict):
    behaviour = spec["behavior"]
    kwargs = {}
    if "reason" in spec:
        kwargs["reason"] = spec["reason"]
    if behaviour == "fail_cancellation":
        return FailCancellation(**kwargs)
    if behaviour == "timeout":
        return TimeoutCancellation(**kwargs)
    raise ScenarioDefinitionError(f"unknown cancel behaviour {behaviour!r}")  # guarded by validation


def _build_broker(definition: ScenarioDefinition) -> FakeBroker:
    spec = definition.broker
    broker = FakeBroker(
        default_fill_price=_decimal_or_none(spec.get("default_fill_price", "150")),
        connection_target=spec.get("connection_target", "fake"),
    )
    for client_order_id, script in spec.get("scripts", {}).items():
        broker.script_order(client_order_id, _build_submit_script(script))
    for client_order_id, script in spec.get("cancel_scripts", {}).items():
        broker.script_cancel(client_order_id, _build_cancel_script(script))
    return broker


def _scenario_symbols(definition: ScenarioDefinition) -> set[str]:
    symbols = set()
    for step in definition.steps:
        if step.action in ("register", "manual_reissue", "gate_correction"):
            symbols.add(step.params["request"]["symbol"])
        elif step.action == "register_concurrent":
            symbols.update(entry["request"]["symbol"] for entry in step.params["registrations"])
    return symbols or {"USD_JPY"}


def _build_policy(definition: ScenarioDefinition) -> SafetyPolicy:
    gate = definition.gate
    allowed_symbols = frozenset(gate.get("allowed_symbols", sorted(_scenario_symbols(definition))))
    default_spread = {symbol: Decimal("0.05") for symbol in allowed_symbols}
    spread_overrides = {
        symbol: Decimal(spread) for symbol, spread in gate.get("max_spread_by_symbol", {}).items()
    }
    return SafetyPolicy(
        policy_id="scenario-policy-1",
        allowed_symbols=allowed_symbols,
        allowed_sides=frozenset(
            OrderSide(side) for side in gate.get("allowed_sides", ["buy", "sell"])
        ),
        max_order_quantity=Decimal(gate.get("max_order_quantity", "10")),
        max_positions_per_symbol=gate.get("max_positions_per_symbol", 2),
        max_positions_account=gate.get("max_positions_account", 5),
        max_reference_age=timedelta(seconds=gate.get("max_reference_age_seconds", 30)),
        max_spread_by_symbol={**default_spread, **spread_overrides},
        allowed_connection_targets={
            ExecutionMode.FAKE: frozenset(gate.get("allowed_fake_targets", ["fake"])),
            ExecutionMode.SHADOW: frozenset(gate.get("allowed_shadow_targets", ["shadow"])),
        },
        require_stop_loss=gate.get("require_stop_loss", True),
    )


def _build_market(definition: ScenarioDefinition) -> MarketSafetySnapshot:
    spec = definition.market
    symbols = sorted(_scenario_symbols(definition))
    return MarketSafetySnapshot(
        symbol=spec.get("symbol", symbols[0]),
        reference_price=_decimal_or_none(spec.get("reference_price", "150")),
        bid=_decimal_or_none(spec.get("bid", "149.99")),
        ask=_decimal_or_none(spec.get("ask", "150.01")),
        observed_at_utc=NOW - timedelta(seconds=spec.get("age_seconds", 0)),
        market_open=spec.get("market_open", True),
        tradable=spec.get("tradable", True),
    )


# --- fault injection ----------------------------------------------------------

# Boundary name -> (owner object, error type the wrapper raises). The
# attribute substituted on the owner comes from ``FAULT_BOUNDARIES``
# (scenario_models validates the names). Issue #78 adds the shadow-store save
# (module function) and the safety-decision record (class attribute on the
# sealed store type -- the only substitution point, since instances cannot be
# injected into the services).
_FAULT_TARGETS: dict[str, tuple[object, type[Exception]]] = {
    "initial_save": (execution_store, execution_store.ExecutionStoreError),
    "mark_submitting": (execution_store, execution_store.ExecutionStoreError),
    "record_order_result": (execution_store, execution_store.ExecutionStoreError),
    "record_order_state": (execution_store, execution_store.ExecutionStoreError),
    "state_history": (execution_store, execution_store.ExecutionStoreError),
    "shadow_claim": (shadow_store, shadow_store.ShadowStoreError),
    "shadow_result_save": (shadow_store, shadow_store.ShadowStoreError),
    "safety_decision_record": (SQLiteSafetyDecisionStore, SafetyStoreError),
}


@contextlib.contextmanager
def _armed_faults(faults: tuple[dict, ...]):
    """Substitute the targeted store functions with raising stubs for the
    duration of one scenario (restored in reverse order even on error). This
    is the explicit, test-only dependency substitution Issue #77 asks for --
    production modules keep no hidden failure switches."""
    originals: list[tuple[object, str, object]] = []
    try:
        for fault in faults:
            boundary = fault["boundary"]
            attribute = FAULT_BOUNDARIES[boundary]
            owner, error_type = _FAULT_TARGETS[boundary]
            original = getattr(owner, attribute)
            originals.append((owner, attribute, original))
            setattr(
                owner,
                attribute,
                _fault_wrapper(
                    original,
                    boundary,
                    error_type,
                    after_calls=fault.get("after_calls"),
                    fail_calls=fault.get("fail_calls"),
                ),
            )
        yield
    finally:
        for owner, attribute, original in reversed(originals):
            setattr(owner, attribute, original)


def _fault_wrapper(
    original,
    boundary: str,
    error_type: type[Exception],
    *,
    after_calls: int | None,
    fail_calls: int | None,
):
    """``after_calls=N``: pass the first N calls, fail the rest.
    ``fail_calls=N`` (Issue #78 stale-claim recovery): fail the first N
    calls, pass the rest. Default (neither): fail every call."""
    lock = threading.Lock()
    calls = [0]

    def wrapper(*args, **kwargs):
        with lock:
            calls[0] += 1
            call_number = calls[0]
        if fail_calls is not None:
            should_fail = call_number <= fail_calls
        else:
            should_fail = call_number > (after_calls or 0)
        if should_fail:
            raise error_type(f"injected fault at boundary {boundary!r} (call {call_number})")
        return original(*args, **kwargs)

    return wrapper


# --- step execution -----------------------------------------------------------


class _ScenarioContext:
    """Everything one scenario run needs; built fresh per scenario."""

    def __init__(self, definition: ScenarioDefinition, work_dir: Path):
        self.definition = definition
        self.orders_db = work_dir / "orders.sqlite3"
        self.decisions_db = work_dir / "safety.sqlite3"
        self.shadow_db = work_dir / "shadow.sqlite3"
        self.journal_db = work_dir / "journal.sqlite3"
        self.reconciliation_db = work_dir / "reconciliation.sqlite3"
        # No store is initialized eagerly (Issue #78): dry-run shadow
        # scenarios observe that NO store file is ever created, so store
        # initialization happens lazily (and idempotently) in the service
        # factories the non-dry-run steps use.
        self.broker = _build_broker(definition)
        self.connection = BrokerConnection(broker=self.broker)
        self.policy = _build_policy(definition)
        # ONE reissue policy for BOTH boundaries (PR #122 Codex review P2):
        # the same object governs the registration freshness window and the
        # submission cutoff, so the scenarios exercise the wiring a composer
        # must use -- never two independently defaulted windows.
        self.reissue_policy = ReissuePolicy()
        self.market = _build_market(definition)
        self.state = StaticSafetyState(
            kill_switch_enabled=definition.gate.get("kill_switch", False)
        )
        # Injected broker-state context for shadow evaluations: an empty,
        # fresh snapshot (the required shadow scenarios exercise gate
        # conditions other than position limits).
        self.account = AccountSafetySnapshot(
            positions=(),
            account=AccountState(
                account_id="scenario-account",
                currency="XXX",
                balance=Decimal("0"),
                equity=Decimal("0"),
                margin_available=Decimal("0"),
                retrieved_at_utc=NOW,
            ),
        )
        # Fixture provenance for shadow steps: clearly labelled as the
        # scenario suite, never as the production signal path.
        self.provenance = ShadowProvenance(
            input_origin=INPUT_ORIGIN_SCENARIO_FIXTURE,
            signal_source=f"scenario:{definition.scenario_id}",
            config_source=f"scenario:{definition.scenario_id}",
            config_version="scenario-policy-1",
            market_source=f"scenario:{definition.scenario_id}",
            account_source=f"scenario:{definition.scenario_id}",
        )
        # Stored-signal fixtures for shadow_production steps: seeded ONCE per
        # scenario into a private temporary paper-signal DB that the
        # PRODUCTION PaperSignalDbSignalProvider then reads (fixture
        # provisioning only -- the decision path stays production code).
        self.signal_db = work_dir / "paper_signals.sqlite3"
        if definition.signals:
            _seed_signal_fixture(self.signal_db, definition.signals)
        self.client_order_ids: dict[str, str] = {}
        self.step_results: list[dict] = []

    def _boundary(self, clock=None):
        """Both production services from the PRODUCTION composition root
        (Issue #116 / PR #122 review r3): one ReissuePolicy governs the
        registration and submission freshness windows, and the scenarios
        exercise the only sanctioned way to wire them. ``clock`` overrides
        the SERVICE clock only (cancel steps move it past the broker's
        deterministic event ticks via ``at_offset_seconds``)."""
        SQLiteSafetyDecisionStore(self.decisions_db).initialize()
        SubmissionJournal(self.journal_db).initialize()
        return build_reissue_boundary(
            connection=self.connection,
            gate=SafetyGate(policy=self.policy, state_provider=self.state, clock=lambda: NOW),
            current_policy=self.policy,
            reissue_policy=self.reissue_policy,
            decision_db_path=self.decisions_db,
            order_db_path=self.orders_db,
            journal_db_path=self.journal_db,
            clock=clock if clock is not None else (lambda: NOW),
        )

    def make_service(self, clock=None) -> LiveOrderSubmissionService:
        """One service per call: concurrency steps give each worker its own
        instance (multi-process equivalence); sequential steps just use a
        fresh one, which is stateless beyond its injected paths."""
        return self._boundary(clock).submission

    def make_reissue_service(self):
        """One PRODUCTION reissue registration service per call (Issue #116),
        from the same composition root as the submission service; the
        current policy is the scenario's gate policy, so the policy-identity
        precondition compares against the same content the submitted
        decisions were produced under."""
        return self._boundary().registration

    def make_clearance_service(self) -> ReissueClearanceService:
        """One PRODUCTION clearance observation service per call (Issue
        #116), running against the scenario's FakeBroker and temporary DBs."""
        return ReissueClearanceService(
            broker=self.broker,
            execution_db_path=self.orders_db,
            reconciliation_db_path=self.reconciliation_db,
            observer_id=f"scenario-{self.definition.scenario_id}",
            clock=lambda: NOW,
        )

    def step_clock(self, step: ScenarioStep):
        """The step's evaluation clock: NOW plus the declared offset (used by
        the stale-claim recovery scenarios to move past a lease expiry)."""
        offset = step.params.get("at_offset_seconds", 0)
        moment = NOW + timedelta(seconds=offset)
        return lambda: moment

    def make_shadow_service(
        self,
        *,
        initialize: bool,
        clock=None,
        owner_id: str | None = None,
        claim_lease_seconds: int | None = None,
    ) -> ShadowExecutionService:
        """One shadow service per call (same multi-process equivalence as
        ``make_service``). ``initialize=False`` is the dry-run path: the
        service must not create any store file."""
        clock = clock if clock is not None else (lambda: NOW)
        kwargs = {}
        if claim_lease_seconds is not None:
            kwargs["claim_lease_seconds"] = claim_lease_seconds
        service = ShadowExecutionService(
            gate=SafetyGate(policy=self.policy, state_provider=self.state, clock=clock),
            decision_db_path=self.decisions_db,
            shadow_db_path=self.shadow_db,
            clock=clock,
            owner_id=owner_id,
            **kwargs,
        )
        if initialize:
            service.initialize_stores()
        return service


def _seed_signal_fixture(db_path: Path, signals: tuple[dict, ...]) -> None:
    """Write the scenario's stored-signal FIXTURES into a private temporary
    DB carrying the paper-signal columns the production adapter reads. Pure
    input provisioning: nothing here decides or evaluates anything."""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE paper_signals ("
            "signal_id TEXT PRIMARY KEY, created_at_utc TEXT NOT NULL, "
            "symbol TEXT NOT NULL, timeframe TEXT NOT NULL, side TEXT NOT NULL, "
            "strategy_profile TEXT NOT NULL, status TEXT NOT NULL)"
        )
        for signal in signals:
            connection.execute(
                "INSERT INTO paper_signals (signal_id, created_at_utc, symbol, timeframe, "
                "side, strategy_profile, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    signal["signal_id"],
                    NOW.isoformat(),
                    signal["symbol"],
                    signal["timeframe"],
                    signal["side"],
                    signal.get("strategy_id", "scenario-strategy"),
                    signal.get("status", "paper_only"),
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _classify_error(exc: Exception) -> dict:
    classified = {"error_type": type(exc).__name__}
    if isinstance(exc, BrokerError):
        classified["error_code"] = exc.code.value
    return classified


def _execute_register(context: _ScenarioContext, step: ScenarioStep) -> dict:
    request = _build_request(step.params["request"])
    client_order_id = step.params["client_order_id"]
    try:
        context.make_service().register(request, client_order_id=client_order_id)
    except execution_store.ExecutionOrderConflictError:
        return {"outcome": "conflict"}
    except Exception as exc:
        return {"outcome": "error", **_classify_error(exc)}
    context.client_order_ids[request.execution_request_id] = client_order_id
    return {"outcome": "registered"}


def _execute_submit(context: _ScenarioContext, step: ScenarioStep) -> dict:
    try:
        decision, result = context.make_service().submit(
            step.params["execution_request_id"], market=context.market
        )
    except Exception as exc:
        return {"outcome": "error", **_classify_error(exc)}
    if result is not None:
        return {"outcome": "submitted", "state": result.state.value}
    return {"outcome": "rejected", "rejection_codes": sorted(decision.rejection_codes)}


def _execute_cancel(context: _ScenarioContext, step: ScenarioStep) -> dict:
    """Issue #99: the runner calls the PRODUCTION public cancel API only.
    Outcome classification (cancelled / refused / broker_refused) is the
    service's own ``CancelOutcome``; a re-raised exception (timeout,
    persistence failure -- UNKNOWN territory) is reported as ``error``."""
    try:
        outcome = context.make_service(clock=context.step_clock(step)).cancel(
            step.params["execution_request_id"]
        )
    except Exception as exc:
        return {"outcome": "error", **_classify_error(exc)}
    payload = {"outcome": outcome.status.value}
    if outcome.refusal_code is not None:
        payload["refusal_code"] = outcome.refusal_code.value
    if outcome.result is not None:
        payload["state"] = outcome.result.state.value
    return payload


def _execute_record_unknown(context: _ScenarioContext, step: ScenarioStep) -> dict:
    try:
        execution_store.record_order_state(
            step.params["execution_request_id"],
            OrderState.UNKNOWN,
            reason=step.params.get("reason", _UNKNOWN_REASON),
            changed_at_utc=NOW + timedelta(seconds=60),
            db_path=context.orders_db,
        )
    except Exception as exc:
        return {"outcome": "error", **_classify_error(exc)}
    return {"outcome": "recorded"}


def _execute_mark_submitting(context: _ScenarioContext, step: ScenarioStep) -> dict:
    try:
        execution_store.mark_submitting(
            step.params["execution_request_id"],
            reason=step.params.get("reason", _MARK_SUBMITTING_REASON),
            changed_at_utc=NOW,
            db_path=context.orders_db,
        )
    except Exception as exc:
        return {"outcome": "error", **_classify_error(exc)}
    return {"outcome": "ok"}


def _run_workers(worker_count: int, work) -> list[dict]:
    """Start ``worker_count`` threads that all block on one barrier directly
    before the contested production call, then join them. The barrier -- not
    sleeps -- aligns the race window."""
    barrier = threading.Barrier(worker_count)
    results: list[dict | None] = [None] * worker_count
    threads = []

    def runner(index: int) -> None:
        results[index] = work(index, barrier)

    for index in range(worker_count):
        thread = threading.Thread(target=runner, args=(index,), name=f"scenario-worker-{index}")
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError("concurrent scenario workers did not finish within 60s")
    return [result if result is not None else {"outcome": "error", "error_type": "missing"} for result in results]


def _execute_submit_concurrent(context: _ScenarioContext, step: ScenarioStep) -> dict:
    execution_request_id = step.params["execution_request_id"]

    def work(index: int, barrier: threading.Barrier) -> dict:
        service = context.make_service()
        barrier.wait(timeout=60)
        try:
            decision, result = service.submit(execution_request_id, market=context.market)
        except Exception as exc:
            return {"outcome": "error", **_classify_error(exc)}
        if result is not None:
            return {"outcome": "submitted", "state": result.state.value}
        return {"outcome": "rejected", "rejection_codes": sorted(decision.rejection_codes)}

    worker_results = _run_workers(step.params["workers"], work)
    rejected = [entry for entry in worker_results if entry["outcome"] == "rejected"]
    loser_codes = sorted({code for entry in rejected for code in entry["rejection_codes"]})
    return {
        "submitted": sum(1 for entry in worker_results if entry["outcome"] == "submitted"),
        "rejected": len(rejected),
        "errors": sum(1 for entry in worker_results if entry["outcome"] == "error"),
        "rejection_codes": loser_codes,
        "workers": worker_results,
    }


def _shadow_step_payload(outcome) -> dict:
    result = outcome.result
    payload = {
        # Scenario vocabulary uses identifier-style outcome names.
        "outcome": "dry_run" if outcome.status is ShadowCycleStatus.DRY_RUN else outcome.status.value,
        # A duplicate against a still-in-flight claim carries no result:
        # ``allowed`` is None there (the gate never ran for this call).
        "allowed": None if result is None else result.allowed,
        "rejection_codes": [] if result is None else sorted(result.rejection_codes),
    }
    return payload


def _classify_shadow_error(exc: Exception) -> dict:
    if isinstance(exc, shadow_store.ShadowIdentityConflictError):
        return {
            "outcome": "conflict",
            "error_type": type(exc).__name__,
            "conflict_fields": sorted(exc.differing_fields),
        }
    return {"outcome": "error", **_classify_error(exc)}


def _execute_shadow(context: _ScenarioContext, step: ScenarioStep) -> dict:
    request = _build_request(step.params["request"])
    dry_run = step.params.get("dry_run", False)
    try:
        service = context.make_shadow_service(
            initialize=not dry_run,
            clock=context.step_clock(step),
            owner_id=step.params.get("owner_id"),
            claim_lease_seconds=step.params.get("claim_lease_seconds"),
        )
        run = service.dry_run_cycle if dry_run else service.run_cycle
        outcome = run(
            request,
            market=context.market,
            account=context.account,
            client_order_id=step.params["client_order_id"],
            provenance=context.provenance,
            timeframe=step.params.get("timeframe"),
        )
    except Exception as exc:
        return _classify_shadow_error(exc)
    return _shadow_step_payload(outcome)


def _execute_shadow_production(context: _ScenarioContext, step: ScenarioStep) -> dict:
    """Drive the PRODUCTION composition root over the scenario's seeded
    signal store and validated production configuration -- the runner only
    provisions inputs and observes."""
    config = parse_shadow_execution_config(
        context.definition.production_config,
        source=f"scenario:{context.definition.scenario_id}",
    )
    clock = context.step_clock(step)
    try:
        outcome = run_production_shadow_cycle(
            step.params["signal_id"],
            signal_provider=PaperSignalDbSignalProvider(context.signal_db),
            config=config,
            market_provider=StaticMarketSnapshotProvider(
                context.market, f"scenario:{context.definition.scenario_id}"
            ),
            account_provider=StaticAccountSnapshotProvider(
                context.account, f"scenario:{context.definition.scenario_id}"
            ),
            shadow_db_path=context.shadow_db,
            decision_db_path=context.decisions_db,
            clock=clock,
            owner_id=step.params.get("owner_id"),
            dry_run=step.params.get("dry_run", False),
        )
    except Exception as exc:
        return _classify_shadow_error(exc)
    return _shadow_step_payload(outcome)


def _execute_shadow_concurrent(context: _ScenarioContext, step: ScenarioStep) -> dict:
    request = _build_request(step.params["request"])
    worker_count = step.params["workers"]
    # Stores are initialized once, sequentially, before the race: the
    # contested boundary is the shadow store's claim, not init.
    services = [
        context.make_shadow_service(initialize=True, owner_id=f"scenario-worker-{index}")
        for index in range(worker_count)
    ]

    def work(index: int, barrier: threading.Barrier) -> dict:
        barrier.wait(timeout=60)
        try:
            outcome = services[index].run_cycle(
                request,
                market=context.market,
                account=context.account,
                client_order_id=step.params["client_order_id"],
                provenance=context.provenance,
                timeframe=step.params.get("timeframe"),
            )
        except Exception as exc:
            # A locked-DB / store failure is an ``error`` -- it must never be
            # counted as a recorded result or as duplicate suppression.
            return _classify_shadow_error(exc)
        return _shadow_step_payload(outcome)

    worker_results = _run_workers(worker_count, work)
    codes = sorted(
        {code for entry in worker_results for code in entry.get("rejection_codes", [])}
    )
    return {
        "recorded": sum(1 for entry in worker_results if entry["outcome"] == "recorded"),
        "duplicates": sum(1 for entry in worker_results if entry["outcome"] == "duplicate"),
        "errors": sum(1 for entry in worker_results if entry["outcome"] == "error"),
        "rejection_codes": codes,
        "workers": worker_results,
    }


def _execute_register_concurrent(context: _ScenarioContext, step: ScenarioStep) -> dict:
    registrations = step.params["registrations"]
    requests = [_build_request(entry["request"]) for entry in registrations]

    def work(index: int, barrier: threading.Barrier) -> dict:
        service = context.make_service()
        request = requests[index]
        barrier.wait(timeout=60)
        try:
            service.register(request, client_order_id=registrations[index]["client_order_id"])
        except execution_store.ExecutionOrderConflictError:
            # The UNIQUE-constraint loser. A locked-DB / store failure takes
            # the ``error`` branch below instead -- a lock error is NOT
            # duplicate suppression and must never count as either winner or
            # suppressed duplicate.
            return {"outcome": "conflict"}
        except Exception as exc:
            return {"outcome": "error", **_classify_error(exc)}
        context.client_order_ids[request.execution_request_id] = registrations[index]["client_order_id"]
        return {"outcome": "registered"}

    worker_results = _run_workers(len(registrations), work)
    return {
        "registered": sum(1 for entry in worker_results if entry["outcome"] == "registered"),
        "conflicts": sum(1 for entry in worker_results if entry["outcome"] == "conflict"),
        "errors": sum(1 for entry in worker_results if entry["outcome"] == "error"),
        "workers": worker_results,
    }


def _execute_grant_clearance(context: _ScenarioContext, step: ScenarioStep) -> dict:
    """Drive the PRODUCTION clearance observation (claim -> reconcile both
    domains -> judge -> conclude) for the scenario's signal."""
    try:
        observation = context.make_clearance_service().observe_signal(
            step.params["signal_id"]
        )
    except ReissueClearanceError as exc:
        return {"outcome": "error", **_classify_error(exc)}
    except Exception as exc:
        return {"outcome": "error", **_classify_error(exc)}
    if observation.granted:
        return {"outcome": "granted"}
    return {"outcome": "not_granted"}


def _execute_reissue(context: _ScenarioContext, step: ScenarioStep, register) -> dict:
    request = _build_request(step.params["request"])
    try:
        register(
            request,
            client_order_id=step.params["client_order_id"],
            parent_execution_request_id=step.params["parent_execution_request_id"],
            operator_id=step.params["operator_id"],
            reason=step.params["reason"],
        )
    except (ReissueEligibilityError, execution_store.ExecutionStoreError, ValueError) as exc:
        return {"outcome": "refused", "error_type": type(exc).__name__}
    except Exception as exc:
        return {"outcome": "error", **_classify_error(exc)}
    context.client_order_ids[request.execution_request_id] = step.params["client_order_id"]
    return {"outcome": "registered"}


def _execute_manual_reissue(context: _ScenarioContext, step: ScenarioStep) -> dict:
    return _execute_reissue(context, step, context.make_reissue_service().register_manual_reissue)


def _execute_gate_correction(context: _ScenarioContext, step: ScenarioStep) -> dict:
    return _execute_reissue(context, step, context.make_reissue_service().register_gate_correction)


# The ``cancel`` step (Issue #99) drives the PRODUCTION cancel use case --
# ``LiveOrderSubmissionService.cancel()`` -- and nothing else: the runner
# holds no cancel orchestration of its own (the PR #96 review r1 ban on a
# synthesized cancel path stands; what changed is that the production path
# now exists, so the fixed cancel scenarios return via the public API).
_STEP_EXECUTORS = {
    "register": _execute_register,
    "submit": _execute_submit,
    "cancel": _execute_cancel,
    "record_unknown": _execute_record_unknown,
    "mark_submitting": _execute_mark_submitting,
    "submit_concurrent": _execute_submit_concurrent,
    "register_concurrent": _execute_register_concurrent,
    "shadow": _execute_shadow,
    "shadow_concurrent": _execute_shadow_concurrent,
    "shadow_production": _execute_shadow_production,
    "grant_clearance": _execute_grant_clearance,
    "manual_reissue": _execute_manual_reissue,
    "gate_correction": _execute_gate_correction,
}


# --- actual collection / comparison ------------------------------------------


def _row_counts(db_path: Path) -> tuple[int, int]:
    """Row counts straight from the scenario's temporary DB, read-only. A
    missing file (e.g. the initial save was fault-injected) counts as zero
    rows and is never created here."""
    if not db_path.exists():
        return 0, 0
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        orders = connection.execute(
            f"SELECT COUNT(*) FROM {execution_store.ORDERS_TABLE}"
        ).fetchone()[0]
        history = connection.execute(
            f"SELECT COUNT(*) FROM {execution_store.HISTORY_TABLE}"
        ).fetchone()[0]
    finally:
        connection.close()
    return orders, history


def _shadow_row_counts(db_path: Path) -> tuple[int, int]:
    """(completed, claimed) row counts straight from the scenario's temporary
    DB, read-only; a missing file counts as zero rows and is never created."""
    if not db_path.exists():
        return 0, 0
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        completed = connection.execute(
            f"SELECT COUNT(*) FROM {shadow_store.RESULTS_TABLE} WHERE phase = 'completed'"
        ).fetchone()[0]
        claimed = connection.execute(
            f"SELECT COUNT(*) FROM {shadow_store.RESULTS_TABLE} WHERE phase = 'claimed'"
        ).fetchone()[0]
    finally:
        connection.close()
    return completed, claimed


def _collect_actual(context: _ScenarioContext) -> dict:
    definition = context.definition
    primary_id = resolve_primary_id(definition)
    orders_db = context.orders_db

    row = None
    history: list[dict] = []
    result = None
    if primary_id is not None:
        row = execution_store.get_execution_order(primary_id, db_path=orders_db)
        history = execution_store.get_state_history(primary_id, db_path=orders_db)
        result = execution_store.load_order_result(primary_id, db_path=orders_db)

    order_rows, history_rows = _row_counts(orders_db)
    unknown_ids = {
        entry["execution_request_id"]
        for entry in execution_store.list_unknown_orders(db_path=orders_db)
    }
    unresolved_ids = {
        entry["execution_request_id"]
        for entry in execution_store.list_unresolved_submissions(db_path=orders_db)
    }
    decisions = (
        SQLiteSafetyDecisionStore(context.decisions_db).list_for_request(primary_id)
        if primary_id is not None
        else ()
    )
    last_decision = decisions[-1] if decisions else None

    submitted = context.broker.submitted_requests
    first_submitted = submitted[0] if submitted else None

    actual = {
        "final_phase": None if row is None else row["submission_phase"],
        "final_state": None if row is None else row["order_state"],
        "state_history": [
            {
                "previous_state": entry["previous_state"],
                "new_state": entry["new_state"],
                "reason": entry["reason"],
                "broker_order_id": entry["broker_order_id"],
                "changed_at_utc": entry["changed_at_utc"],
            }
            for entry in history
        ],
        "broker_submit_calls": len(submitted),
        # Issue #99: every cancel ATTEMPT recorded by FakeBroker (successful,
        # refused and timed-out alike), so scenarios can pin that refusals
        # and duplicate cancels never reach the broker.
        "broker_cancel_calls": len(context.broker.cancel_requests),
        "submitted_request": None
        if first_submitted is None
        else {
            "client_order_id": first_submitted.client_order_id,
            "symbol": first_submitted.symbol,
            "side": first_submitted.side.value,
            "order_type": first_submitted.order_type.value,
            "quantity": first_submitted.quantity,
            "limit_price": first_submitted.limit_price,
        },
        "order_rows": order_rows,
        "history_rows": history_rows,
        "safety_decisions": len(decisions),
        "last_decision_allowed": None if last_decision is None else last_decision.allowed,
        "last_decision_codes": None
        if last_decision is None
        else sorted(last_decision.rejection_codes),
        "broker_order_id": None if row is None else row["broker_order_id"],
        "filled_quantity": None if result is None else result.filled_quantity,
        "average_fill_price": None if result is None else result.average_fill_price,
        "fill_prices": None if result is None else [fill.price for fill in result.fills],
        "requested_price": None
        if row is None
        else (None if row["requested_price"] is None else Decimal(row["requested_price"])),
        "reject_reason": None if result is None else result.reject_reason,
        "listed_unknown": primary_id in unknown_ids,
        "listed_unresolved": primary_id in unresolved_ids,
    }

    # --- intent-reissue observations (Issue #116) ------------------------
    chain_actual = None
    if row is not None:
        chain_actual = [
            {
                "execution_request_id": entry["execution_request_id"],
                "signal_attempt": entry["signal_attempt"],
                "submission_phase": entry["submission_phase"],
                "order_state": entry["order_state"],
            }
            for entry in execution_store.list_execution_order_chain(
                row["signal_id"], db_path=orders_db
            )
        ]
    journal_expected = definition.expected.get("journal_outcomes")
    journal_actual = None
    if journal_expected is not None:
        journal = SubmissionJournal(context.journal_db)
        journal_actual = {}
        for request_id in journal_expected:
            entry = journal.get_entry(request_id)
            journal_actual[request_id] = None if entry is None else entry.outcome
    actual.update({"chain": chain_actual, "journal_outcomes": journal_actual})

    # --- shadow-store observations (Issue #78) ---------------------------
    shadow_result = None
    shadow_claim = None
    if primary_id is not None and context.shadow_db.exists():
        shadow_result = shadow_store.get_shadow_result(primary_id, db_path=context.shadow_db)
        shadow_claim = shadow_store.get_shadow_claim(primary_id, db_path=context.shadow_db)
    intended = None if shadow_result is None else shadow_result.intended_order
    completed_rows, claimed_rows = _shadow_row_counts(context.shadow_db)
    if shadow_result is not None:
        phase = "completed"
    elif shadow_claim is not None:
        phase = "claimed"
    else:
        phase = None
    origin = (
        shadow_result.provenance.input_origin
        if shadow_result is not None
        else (shadow_claim.input_origin if shadow_claim is not None else None)
    )
    actual.update(
        {
            "shadow_rows": completed_rows,
            "shadow_claim_rows": claimed_rows,
            "shadow_db_created": context.shadow_db.exists(),
            "decision_db_created": context.decisions_db.exists(),
            "shadow_allowed": None if shadow_result is None else shadow_result.allowed,
            "shadow_codes": None
            if shadow_result is None
            else sorted(shadow_result.rejection_codes),
            "shadow_origin": origin,
            "shadow_phase": phase,
            "shadow_account_id": None
            if shadow_result is None
            else shadow_result.account.account.account_id,
            "shadow_safety_decision_id": None
            if shadow_result is None
            else shadow_result.safety_decision_id,
            "shadow_intended_order": None
            if intended is None
            else {
                "client_order_id": intended.client_order_id,
                "symbol": intended.symbol,
                "side": intended.side.value,
                "order_type": intended.order_type.value,
                "quantity": intended.quantity,
                "limit_price": intended.limit_price,
            },
        }
    )
    return actual


_DECIMAL_EXPECTED_KEYS = frozenset({"filled_quantity", "average_fill_price", "requested_price"})


def _values_match(key: str, expected, actual) -> bool:
    if key in _DECIMAL_EXPECTED_KEYS:
        if (expected is None) != (actual is None):
            return False
        return expected is None or Decimal(expected) == actual
    if key == "fill_prices":
        return actual is not None and [Decimal(price) for price in expected] == list(actual)
    if key in ("last_decision_codes", "shadow_codes"):
        return actual is not None and sorted(expected) == actual
    if key in ("submitted_request", "shadow_intended_order"):
        if actual is None:
            return False
        for sub_key, sub_expected in expected.items():
            sub_actual = actual[sub_key]
            if sub_key in ("quantity", "limit_price"):
                if (sub_expected is None) != (sub_actual is None):
                    return False
                if sub_expected is not None and Decimal(sub_expected) != sub_actual:
                    return False
            elif sub_expected != sub_actual:
                return False
        return True
    return expected == actual


def _history_mismatches(expected_entries: list, actual_entries: list) -> list[Mismatch]:
    """Full-history comparison: entry COUNT and ORDER always match exactly.

    Each expected entry is a subset comparison over ``previous_state`` /
    ``new_state`` / ``reason`` / ``broker_order_id``: an ABSENT key is not
    compared, while an explicit null requires the stored value to be NULL --
    the two declarations stay distinct. Independent of what a scenario
    declares, every actual entry must carry a ``changed_at_utc`` and the
    timestamps must be monotonically non-decreasing; the exact timestamp
    strings are dynamic and are never compared.
    """
    mismatches = []
    if len(expected_entries) != len(actual_entries):
        mismatches.append(
            Mismatch(
                "expected.state_history length",
                len(expected_entries),
                [f"{e['previous_state']}->{e['new_state']}" for e in actual_entries],
            )
        )
    previous_changed_at = None
    for index, actual_entry in enumerate(actual_entries):
        if index < len(expected_entries):
            for key in ("previous_state", "new_state", "reason", "broker_order_id"):
                expected_entry = expected_entries[index]
                if key in expected_entry and expected_entry[key] != actual_entry[key]:
                    mismatches.append(
                        Mismatch(
                            f"expected.state_history[{index}].{key}",
                            expected_entry[key],
                            actual_entry[key],
                        )
                    )
        changed_at = actual_entry["changed_at_utc"]
        if not isinstance(changed_at, str) or not changed_at.strip():
            mismatches.append(
                Mismatch(f"state_history[{index}].changed_at_utc", "<present>", changed_at)
            )
            continue
        parsed = datetime.fromisoformat(changed_at)
        if previous_changed_at is not None and parsed < previous_changed_at:
            mismatches.append(
                Mismatch(
                    f"state_history[{index}].changed_at_utc monotonicity",
                    f">= {previous_changed_at.isoformat()}",
                    changed_at,
                )
            )
        previous_changed_at = parsed
    return mismatches


def _compare_expected(definition: ScenarioDefinition, actual: dict) -> list[Mismatch]:
    mismatches = []
    for key, expected_value in definition.expected.items():
        if key == "state_history":
            mismatches.extend(_history_mismatches(expected_value, actual[key]))
        elif not _values_match(key, expected_value, actual[key]):
            mismatches.append(Mismatch(f"expected.{key}", expected_value, actual[key]))
    return mismatches


def _compare_step(index: int, step: ScenarioStep, actual: dict) -> list[Mismatch]:
    mismatches = []
    for key, expected_value in step.expect.items():
        actual_value = actual.get(key)
        if key in ("rejection_codes", "conflict_fields"):
            matched = actual_value is not None and sorted(expected_value) == sorted(actual_value)
        else:
            matched = expected_value == actual_value
        if not matched:
            mismatches.append(
                Mismatch(f"steps[{index}]({step.action}).{key}", expected_value, actual_value)
            )
    return mismatches


# --- scenario execution -------------------------------------------------------


def run_scenario(definition: ScenarioDefinition, work_dir: Path) -> ScenarioOutcome:
    """Run ONE scenario in ``work_dir`` (must be empty / scenario-private)."""
    started = time.monotonic()
    outcome = ScenarioOutcome(
        scenario_id=definition.scenario_id,
        description=definition.description,
        status=STATUS_ERROR,
        expected_summary=dict(definition.expected),
    )
    try:
        context = _ScenarioContext(definition, work_dir)
        with _armed_faults(definition.faults):
            for step in definition.steps:
                context.step_results.append(_STEP_EXECUTORS[step.action](context, step))
        actual = _collect_actual(context)
        outcome.actual_summary = {
            key: actual[key] for key in definition.expected
        }
        outcome.actual_summary["steps"] = context.step_results
        mismatches: list[Mismatch] = []
        for index, (step, step_actual) in enumerate(zip(definition.steps, context.step_results)):
            mismatches.extend(_compare_step(index, step, step_actual))
        mismatches.extend(_compare_expected(definition, actual))
        outcome.mismatches = mismatches
        outcome.status = STATUS_PASS if not mismatches else STATUS_FAIL
    except Exception:
        outcome.status = STATUS_ERROR
        outcome.error = traceback.format_exc()
    outcome.duration_seconds = time.monotonic() - started
    return outcome


def run_scenarios(
    definitions: tuple[ScenarioDefinition, ...], base_dir: Path
) -> RunReport:
    """Run scenarios sequentially, each in its own fresh subdirectory of
    ``base_dir``."""
    started = time.monotonic()
    outcomes = []
    for index, definition in enumerate(definitions):
        work_dir = base_dir / f"{index:03d}_{definition.scenario_id}"
        work_dir.mkdir(parents=True, exist_ok=False)
        outcomes.append(run_scenario(definition, work_dir))
    return RunReport(outcomes=outcomes, duration_seconds=time.monotonic() - started)


# --- reporting / CLI ----------------------------------------------------------


def _render_report(report: RunReport, *, verbose: bool, out) -> None:
    # ASCII separators only (PR #96 review r1): the default Windows console
    # (cp932) cannot encode characters like an em dash, and the report
    # structure must survive any console encoding.
    for outcome in report.outcomes:
        print(f"{outcome.status} {outcome.scenario_id} - {outcome.description}", file=out)
        if verbose or outcome.status == STATUS_FAIL:
            if verbose:
                print(f"  expected: {_jsonable(outcome.expected_summary)}", file=out)
                print(f"  actual:   {_jsonable(outcome.actual_summary)}", file=out)
            for mismatch in outcome.mismatches:
                print(f"  - {mismatch.render()}", file=out)
        if outcome.status == STATUS_ERROR and outcome.error:
            for line in outcome.error.rstrip().splitlines():
                print(f"  ! {line}", file=out)
    print("-" * 60, file=out)
    print(
        f"total {len(report.outcomes)}  pass {len(report.passed)}  "
        f"fail {len(report.failed)}  error {len(report.errored)}  "
        f"({report.duration_seconds:.2f}s)",
        file=out,
    )
    if report.failed:
        print("failed: " + ", ".join(o.scenario_id for o in report.failed), file=out)
    if report.errored:
        print("errors: " + ", ".join(o.scenario_id for o in report.errored), file=out)


def _report_as_json(report: RunReport) -> dict:
    return {
        "total": len(report.outcomes),
        "passed": len(report.passed),
        "failed": len(report.failed),
        "errored": len(report.errored),
        "duration_seconds": round(report.duration_seconds, 6),
        "scenarios": [outcome.as_json() for outcome in report.outcomes],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m execution.scenario_runner",
        description=(
            "Run the fixed Execution scenarios (Issue #77) against the production "
            "execution path with FakeBroker and per-scenario temporary DBs. "
            "Exit 0 only when every scenario passes."
        ),
    )
    parser.add_argument(
        "--scenario-dir",
        type=Path,
        default=DEFAULT_SCENARIO_DIR,
        help=f"directory holding *.json scenario files (default: {DEFAULT_SCENARIO_DIR})",
    )
    parser.add_argument(
        "--scenario-file",
        action="append",
        type=Path,
        default=None,
        help="run specific scenario file(s) instead of --scenario-dir (repeatable)",
    )
    parser.add_argument(
        "--scenario-id",
        action="append",
        default=None,
        help="run only the given scenario id(s) (repeatable; unknown ids are a usage error)",
    )
    parser.add_argument("--list", action="store_true", help="list scenario ids and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="print expected/actual per scenario")
    parser.add_argument(
        "--json", action="store_true", help="print a machine-readable JSON report to stdout"
    )
    return parser


# Console-encoding safety (PR #96 review r1) lives in execution.cli_streams
# since Issue #78 -- the same contract now covers the shadow CLIs too.
_encoding_safe_streams = encoding_safe_streams


def main(argv: list[str] | None = None) -> int:
    with _encoding_safe_streams():
        return _run_cli(argv)


def _run_cli(argv: list[str] | None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.scenario_file:
            paths = [Path(path) for path in args.scenario_file]
        else:
            paths = scenario_files(args.scenario_dir)
        definitions = load_scenarios(paths)
    except ScenarioDefinitionError as exc:
        print(f"scenario definition error: {exc}", file=sys.stderr)
        return EXIT_USAGE_ERROR

    if args.scenario_id:
        by_id = {definition.scenario_id: definition for definition in definitions}
        unknown = [scenario_id for scenario_id in args.scenario_id if scenario_id not in by_id]
        if unknown:
            print(
                f"unknown scenario id(s): {', '.join(unknown)}; known: "
                f"{', '.join(sorted(by_id))}",
                file=sys.stderr,
            )
            return EXIT_USAGE_ERROR
        definitions = tuple(by_id[scenario_id] for scenario_id in args.scenario_id)

    if args.list:
        for definition in definitions:
            print(f"{definition.scenario_id} - {definition.description}")
        return EXIT_OK

    # CLI readability only: production WARN/CRITICAL logs (duplicate
    # suppression etc.) are part of the exercised behaviour and stay visible
    # with --verbose; the previous disable level is always restored so
    # embedding callers (tests) keep their logging intact.
    previous_disable = logging.root.manager.disable
    if not args.verbose:
        logging.disable(logging.CRITICAL)
    try:
        with TemporaryDirectory(prefix="execution-scenarios-") as tmp:
            report = run_scenarios(definitions, Path(tmp))
    finally:
        logging.disable(previous_disable)

    if args.json:
        # Pure-ASCII JSON: the machine-readable output must never depend on
        # the console encoding (PR #96 review r1) -- non-ASCII scenario text
        # is escaped, so parsing round-trips losslessly on any console.
        print(json.dumps(_report_as_json(report), ensure_ascii=True, indent=2))
    else:
        _render_report(report, verbose=args.verbose, out=sys.stdout)
    return EXIT_OK if report.all_passed else EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
