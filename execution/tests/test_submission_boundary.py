"""Issue #75: the Live Execution submission boundary is enforced STRUCTURALLY.

``LiveOrderSubmissionService`` (execution/live_submission.py) must be the only
path from an ExecutionRequest to ``Broker.submit_order``. These AST scans pin,
repository-wide, that no other module grows a direct ``submit_order`` /
``to_order_request`` call -- so the service boundary cannot be bypassed by
"careful callers"; a bypass fails this suite.
"""

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# The only files that may CALL <something>.submit_order(...):
# - broker/tests exercise the broker boundary itself (Issue #73);
# - execution/live_submission.py holds THE single gated call;
# - execution/tests/test_broker_reconciliation.py and
#   execution/tests/test_broker_reconciliation_repair.py seed the FakeBroker
#   test double with the broker-side state a reconciliation/repair run is
#   supposed to observe (Issue #103 / #104). They are fixtures, not
#   production paths: the reconciliation and repair modules themselves are
#   separately pinned to ZERO submit/cancel calls (their module AST tests
#   plus a poisoned broker facade used by every test there).
# (broker/fake_broker.py only DEFINES submit_order; a def is not a call.)
SUBMIT_ORDER_ALLOWED = {
    "broker/tests",
    "execution/live_submission.py",
    "execution/tests/test_broker_reconciliation.py",
    "execution/tests/test_broker_reconciliation_repair.py",
    # Issue #105: seeds the FakeBroker with the broker-side state a
    # position/trade/SL-TP reconciliation run observes (fixture, not a
    # production path -- the position scope is detection-only and every
    # test there drives the service through a poisoned facade).
    "execution/tests/test_position_reconciliation.py",
    # Issue #105: the CLI suite seeds fixtures the same way AND pins that
    # the ReconcileOnlyBroker facade RAISES on submit/cancel (the assertion
    # itself must call the poisoned method).
    "execution/tests/test_reconciliation_runner_cli.py",
    # Issue #93: FaultAfterResponseBroker is a FakeBroker SUBCLASS test
    # double whose override delegates to super().submit_order() and then
    # injects an Execution-DB fault -- the post-response crash window the
    # submission journal exists for. The production call site remains the
    # sanctioned one in live_submission.py; this is fixture delegation,
    # not a second submission path.
    "execution/tests/test_submission_journal.py",
    # Issue #116: seeds the FakeBroker with the broker-side orders a
    # clearance observation must judge (broker-only order, a ghost order
    # under an unsubmitted parent's key), plus the QueryOnlyBroker facade
    # whose delegation exists so a wrapper WITHOUT the listing
    # capabilities still satisfies the Broker protocol. Fixture
    # provisioning only -- the clearance service itself never submits.
    "execution/tests/test_reissue_clearance.py",
}

# Broker CANCELLATION has exactly ONE production caller (Issue #99):
# ``LiveOrderSubmissionService.cancel()`` in execution/live_submission.py --
# the same single-boundary design as submit_order. Reconciliation (Issue
# #103) stays detection-only and repair (Issue #104) only updates the
# internal DB. Pinning it repository-wide means no other module can quietly
# start cancelling broker orders.
CANCEL_ORDER_ALLOWED = {
    "broker/tests",
    "execution/live_submission.py",
    "execution/tests/test_broker_reconciliation.py",
    "execution/tests/test_broker_reconciliation_repair.py",
    # Issue #105: pins that ReconcileOnlyBroker.cancel_order RAISES.
    "execution/tests/test_reconciliation_runner_cli.py",
    # Issue #116: the QueryOnlyBroker facade's pure delegation (see
    # SUBMIT_ORDER_ALLOWED above); no test there cancels anything.
    "execution/tests/test_reissue_clearance.py",
}

# The only files that may CALL <something>.to_order_request(...):
# - execution/live_submission.py converts AFTER the decision was durably
#   recorded, read back, and the created -> submitting transition was won;
# - execution/tests/test_execution_models.py unit-tests the model itself.
TO_ORDER_REQUEST_ALLOWED = {
    "execution/live_submission.py",
    "execution/tests/test_execution_models.py",
}


# The only PRODUCTION file that may CONSTRUCT LiveOrderSubmissionService /
# ReissueService (Issue #116 / PR #122 review r3): the composition root,
# which builds both from ONE ``ReissuePolicy`` object. Constructing either
# service directly elsewhere re-opens the hazard the root exists to close
# -- a submission freshness window WIDER than the registration one, which
# authorizes clearance evidence the registration policy already treats as
# stale. Tests construct the services directly on purpose (to exercise the
# constructors' own validation), so the whole test tree is allowed.
REISSUE_BOUNDARY_CONSTRUCTION_ALLOWED = {
    "execution/reissue_composition.py",
    "execution/tests",
    "broker/tests",
}


# Issue #82: the LIVE connection has exactly ONE production composer,
# ``execution/live_lock.py``. The broker-layer locks (a live-only endpoint
# allowlist, live-only credentials, the ``LiveActivation`` grant) already
# make a live adapter unbuildable without ``ENABLE_LIVE_TRADING=true`` and
# ``--confirm-live``, but the Kill Switch check, the safety-policy check and
# the startup summary live in the composer -- so a second construction site
# would be a live startup nobody reviewed. Tests construct these classes
# directly on purpose (that is how the constructors' own refusals are
# exercised).
LIVE_CONNECTION_CONSTRUCTION_ALLOWED = {
    "execution/live_lock.py",
    "execution/tests",
    "broker/tests",
}


# Issue #132: the DEMO connection has exactly ONE production composer,
# ``execution/demo_lock.py`` -- the same single-composer stance as live. The
# adapter's own endpoint allowlist already makes a non-practice demo adapter
# unbuildable (Issue #79), but the account pin, the demo/live separation, the
# safety-policy check and the Kill Switch check live in the composer, so a
# second construction site would be a demo startup nobody reviewed.
#
# ``execution/reconciliation_runner.py`` is the ONE documented exception and
# is not a submission path: it builds the adapter for RECONCILE-ONLY queries
# and immediately wraps it in ``ReconcileOnlyBroker``, whose submit / cancel /
# account operations raise unconditionally (Issue #105). It never builds a
# ``BrokerConnection``, so nothing it constructs can reach the submission
# boundary.
DEMO_CONNECTION_CONSTRUCTION_ALLOWED = {
    "execution/demo_lock.py",
    "execution/reconciliation_runner.py",
    "execution/tests",
    "broker/tests",
}

# Issue #132: the demo runtime grant is what authorizes a demo runner, so it
# is issued by the lock alone.
DEMO_GRANT_CONSTRUCTION_ALLOWED = {
    "execution/demo_lock.py",
    "execution/tests",
}


def _python_files():
    for path in sorted(REPO_ROOT.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT)
        # Only scan repository sources (skip VCS internals / any local envs).
        if any(part.startswith(".") or part in {"venv", "node_modules", "__pycache__"}
               for part in relative.parts):
            continue
        yield relative.as_posix(), path


def _method_call_sites(path: pathlib.Path, method_name: str) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method_name
    ]


def _offenders(method_name: str, allowed: set[str]) -> list[str]:
    offenders = []
    for relative, path in _python_files():
        if relative in allowed or any(relative.startswith(prefix + "/") for prefix in allowed):
            continue
        for lineno in _method_call_sites(path, method_name):
            offenders.append(f"{relative}:{lineno}")
    return offenders


def test_submit_order_is_called_only_by_the_safety_gate_and_broker_tests():
    assert _offenders("submit_order", SUBMIT_ORDER_ALLOWED) == []


def test_cancel_order_is_called_only_by_broker_and_reconciliation_fixture_tests():
    assert _offenders("cancel_order", CANCEL_ORDER_ALLOWED) == []


def test_to_order_request_is_called_only_by_the_safety_gate_and_model_tests():
    assert _offenders("to_order_request", TO_ORDER_REQUEST_ALLOWED) == []


def _local_names_for(tree: ast.AST, class_name: str) -> set[str]:
    """Every LOCAL name bound to ``class_name`` in this module: the plain
    name plus any ``from ... import Foo as Alias`` binding (PR #122 Codex
    review r4)."""
    names = {class_name}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == class_name and alias.asname:
                    names.add(alias.asname)
    return names


def _construction_sites_in_source(source: str, class_name: str) -> list[int]:
    """Lines constructing ``class_name`` in ``source``, in every static form
    (PR #122 Codex review r4 -- the pin the sharing guarantee relies on must
    not be bypassable by how the class is imported):

    - bare name: ``ReissueService(...)``
    - aliased import: ``from ... import ReissueService as R`` then ``R(...)``
    - module-qualified: ``reissue_service.ReissueService(...)`` and
      ``import ... as rs`` -> ``rs.ReissueService(...)`` (any attribute
      access whose final attribute IS the class name)

    Residual (same limit the ``submit_order`` pin has always had, and the
    same one ``docs/architecture/safety_gate.md`` lists as non-scope):
    fully dynamic construction -- ``getattr(module, "ReissueService")()``
    or a stashed constructor -- is not statically visible to any AST scan.
    """
    tree = ast.parse(source)
    local_names = _local_names_for(tree, class_name)
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in local_names:
            sites.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr == class_name:
            sites.append(node.lineno)
    return sorted(sites)


def _construction_sites(path: pathlib.Path, class_name: str) -> list[int]:
    return _construction_sites_in_source(path.read_text(encoding="utf-8"), class_name)


def _construction_offenders(class_name: str, allowed: set[str]) -> list[str]:
    offenders = []
    for relative, path in _python_files():
        if relative in allowed or any(relative.startswith(prefix + "/") for prefix in allowed):
            continue
        for lineno in _construction_sites(path, class_name):
            offenders.append(f"{relative}:{lineno}")
    return offenders


@pytest.mark.parametrize(
    "class_name", ["LiveOrderSubmissionService", "ReissueService"]
)
def test_reissue_boundary_services_are_built_only_by_the_composition_root(class_name):
    """Issue #116 / PR #122 review r3: one ``ReissuePolicy`` must govern the
    registration AND submission freshness windows. Making the argument
    mandatory removed the "forgot it" failure but still allowed two
    independently configured policies, so production code obtains BOTH
    services from ``execution/reissue_composition.py`` alone."""
    assert (
        _construction_offenders(class_name, REISSUE_BOUNDARY_CONSTRUCTION_ALLOWED) == []
    )


@pytest.mark.parametrize(
    "class_name", ["OandaLiveAdapter", "LiveActivation", "OandaLiveConfig"]
)
def test_the_live_connection_is_built_only_by_the_live_activation_lock(class_name):
    """Issue #82: no production module other than the live activation lock
    may build a live adapter, a live config or an activation grant."""
    assert _construction_offenders(class_name, LIVE_CONNECTION_CONSTRUCTION_ALLOWED) == []


@pytest.mark.parametrize("class_name", ["OandaDemoAdapter", "OandaDemoConfig"])
def test_the_demo_connection_is_built_only_by_the_demo_activation_lock(class_name):
    """Issue #132: no production module other than the demo activation lock
    (and the reconcile-only CLI, which never composes a ``BrokerConnection``)
    may build a demo adapter or a demo config."""
    assert _construction_offenders(class_name, DEMO_CONNECTION_CONSTRUCTION_ALLOWED) == []


def test_the_demo_runtime_grant_is_issued_only_by_the_demo_activation_lock():
    """Issue #132: the grant is what ``build_demo_execution_runner`` accepts,
    so forging one elsewhere in production would bypass every demo lock."""
    assert _construction_offenders("DemoRuntimeGrant", DEMO_GRANT_CONSTRUCTION_ALLOWED) == []


def test_the_demo_lock_holds_exactly_one_demo_adapter_construction():
    sites = _construction_sites(REPO_ROOT / "execution" / "demo_lock.py", "OandaDemoAdapter")
    assert len(sites) == 1, (
        "execution/demo_lock.py must hold exactly one OandaDemoAdapter construction "
        f"(the gated one in _compose_demo_connection), found lines {sites}"
    )


def test_the_live_lock_holds_exactly_one_live_adapter_construction():
    sites = _construction_sites(REPO_ROOT / "execution" / "live_lock.py", "OandaLiveAdapter")
    assert len(sites) == 1, (
        "execution/live_lock.py must hold exactly one OandaLiveAdapter construction "
        f"(the gated one in build_live_connection), found lines {sites}"
    )


def test_the_v20_engine_is_never_constructed_outside_the_broker_layer():
    """The shared OANDA v20 translation body is a BROKER-layer detail: the
    execution layer talks to the concrete adapters (and to the Broker
    protocol), never to the engine. Constants may be imported (endpoint host
    markers are used for the fail-closed classification); the engine CLASS
    may not be constructed outside the broker package -- doing so would be an
    adapter with no endpoint allowlist behind it."""
    offenders = _construction_offenders(
        "OandaV20Adapter", {"broker", "execution/tests", "broker/tests"}
    )
    assert offenders == []


@pytest.mark.parametrize(
    "label, source",
    [
        (
            "bare name",
            "from execution.reissue_service import ReissueService\n"
            "ReissueService(execution_db_path='x')\n",
        ),
        (
            "aliased import",
            "from execution.reissue_service import ReissueService as _R\n"
            "_R(execution_db_path='x')\n",
        ),
        (
            "module-qualified",
            "from execution import reissue_service\n"
            "reissue_service.ReissueService(execution_db_path='x')\n",
        ),
        (
            "aliased module",
            "import execution.reissue_service as _rs\n"
            "_rs.ReissueService(execution_db_path='x')\n",
        ),
    ],
)
def test_the_boundary_pin_detects_every_static_construction_form(label, source):
    """PR #122 Codex review r4: the sharing guarantee RELIES on this pin, so
    the pin itself is tested. A scan that only matched ``ast.Name`` let a
    module-qualified or aliased construction through -- i.e. the two
    services could still be built with independent policies while the pin
    stayed green."""
    assert _construction_sites_in_source(source, "ReissueService") == [2], label


def test_the_boundary_pin_does_not_flag_unrelated_constructions():
    """The pin must not fire on other classes (including the policy object
    itself, which every composer legitimately constructs)."""
    source = (
        "from execution.reissue_service import ReissuePolicy\n"
        "from execution.reissue_composition import ReissueBoundary\n"
        "ReissuePolicy()\n"
        "ReissueBoundary(submission=None, registration=None, policy=None)\n"
    )
    assert _construction_sites_in_source(source, "ReissueService") == []
    assert _construction_sites_in_source(source, "LiveOrderSubmissionService") == []


def test_the_composition_root_hands_back_one_shared_policy_instance():
    """The invariant the pin protects, checked on the object graph itself."""
    import execution.reissue_composition as composition

    source = (REPO_ROOT / "execution" / "reissue_composition.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    # both services are constructed exactly once, in the one factory
    for class_name in ("LiveOrderSubmissionService", "ReissueService"):
        sites = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == class_name
        ]
        assert len(sites) == 1, f"{class_name} must be constructed once, found {sites}"
    assert hasattr(composition, "build_reissue_boundary")


def test_submission_service_holds_exactly_one_submit_order_call():
    sites = _method_call_sites(REPO_ROOT / "execution" / "live_submission.py", "submit_order")
    assert len(sites) == 1, (
        "execution/live_submission.py must hold exactly one Broker.submit_order call "
        f"(the gated one in LiveOrderSubmissionService.submit), found lines {sites}"
    )


def test_submission_service_holds_exactly_one_cancel_order_call():
    """Issue #99: the sanctioned cancel boundary mirrors the submit one --
    ONE ``Broker.cancel_order`` call inside ``LiveOrderSubmissionService.
    cancel()``, and none anywhere else in production code (the
    repository-wide scan above)."""
    sites = _method_call_sites(REPO_ROOT / "execution" / "live_submission.py", "cancel_order")
    assert len(sites) == 1, (
        "execution/live_submission.py must hold exactly one Broker.cancel_order call "
        f"(the gated one in LiveOrderSubmissionService.cancel), found lines {sites}"
    )


# Issue #78: shadow records are claimed/completed ONLY by the
# ShadowExecutionService -- the scenario runner (and everything else) may read
# shadow records but must never write one directly, so the runner cannot
# synthesize a shadow path.
SHADOW_WRITE_ALLOWED = {
    "execution/shadow_execution.py",
    "execution/tests",
}


def test_shadow_claim_and_completion_are_called_only_by_the_shadow_service():
    assert _offenders("claim_shadow_cycle", SHADOW_WRITE_ALLOWED) == []
    assert _offenders("complete_shadow_cycle", SHADOW_WRITE_ALLOWED) == []


def _name_call_sites(path: pathlib.Path, name: str) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ]


def test_the_scenario_runner_never_constructs_shadow_results():
    """The runner drives the production shadow API only: it may not build a
    ``ShadowResult`` / ``ShadowIntendedOrder`` of its own (which is also
    pinned by it not importing those names)."""
    runner = REPO_ROOT / "execution" / "scenario_runner.py"
    for forbidden in ("ShadowResult", "ShadowIntendedOrder", "intended_order_from_request"):
        assert _name_call_sites(runner, forbidden) == []
    tree = ast.parse(runner.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    assert imported & {"ShadowResult", "ShadowIntendedOrder", "intended_order_from_request"} == set()


def test_shadow_modules_never_import_broker_implementations():
    """The shadow path owns no broker dependency (Issue #78): the shadow
    modules may use broker MODELS only -- importing the Broker protocol or
    FakeBroker would create a call surface the zero-broker-calls contract
    forbids."""
    offenders = []
    for name in ("shadow_execution.py", "shadow_store.py", "shadow_models.py", "shadow_runner.py",
                 "shadow_report.py"):
        path = REPO_ROOT / "execution" / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if module in ("broker.fake_broker", "broker.broker_interface"):
                    offenders.append(f"execution/{name}:{node.lineno} imports {module}")
    assert offenders == []


# Issue #131: the Live Execution runner core drives the existing boundaries;
# it owns no broker and writes no execution state itself.
RUNNER_MODULES = ("live_runner.py", "runner_composition.py")

# Every execution-store function that CHANGES an order's state or creates one.
# The runner must reach all of them only THROUGH the sanctioned services, so
# that no "convenient" runner-side state update or auto-repair can exist.
EXECUTION_STORE_WRITE_FUNCTIONS = (
    "init_db",
    "create_execution_order",
    "create_reissue_execution_order",
    "mark_submitting",
    "record_order_state",
    "record_order_result",
    "register_repair_fence",
    "record_reissue_clearance",
)


def _imported_modules(path: pathlib.Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append((node.lineno, node.module))
    return imported


def test_the_runner_modules_never_import_a_broker_implementation():
    """Issue #131: the runner is a LIFECYCLE, not a submission path. The
    broker connection is composed elsewhere and handed to the sanctioned
    submission service, so neither runner module may import the Broker
    protocol or any concrete adapter -- there is no object through which the
    runner could construct or call a broker (fake, demo or live alike)."""
    offenders = []
    for name in RUNNER_MODULES:
        for lineno, module in _imported_modules(REPO_ROOT / "execution" / name):
            if module.startswith("broker.") and module != "broker.broker_models":
                offenders.append(f"execution/{name}:{lineno} imports {module}")
    assert offenders == []


# Issue #132: ``runner_composition`` imports the demo GRANT type from the
# activation lock, which transitively imports the demo adapter. The import
# above therefore no longer proves the runner cannot reach a broker on its
# own -- so the composers themselves are pinned: a runner module may accept a
# grant, never mint one.
CONNECTION_COMPOSERS = (
    "build_demo_connection",
    "_compose_demo_connection",
    "build_live_connection",
    "_compose_live_connection",
)


def test_the_runner_modules_never_compose_a_broker_connection():
    """Issue #132: a demo runner is composed FROM a ``DemoRuntimeGrant`` the
    activation lock issued. If a runner module could call a composer itself
    it could choose the arguments -- the safety config path, the kill-switch
    DB path -- and so choose which locks it was measured against."""
    offenders = []
    for name in RUNNER_MODULES:
        path = REPO_ROOT / "execution" / name
        for composer in CONNECTION_COMPOSERS + (
            "OandaDemoAdapter",
            "OandaDemoConfig",
            "OandaLiveAdapter",
            "FakeBroker",
        ):
            for lineno in _method_call_sites(path, composer) + _name_call_sites(path, composer):
                offenders.append(f"execution/{name}:{lineno} calls {composer}")
    assert offenders == []


def test_the_runner_modules_never_write_execution_state_directly():
    """Issue #131: every state change belongs to the sanctioned services
    (``LiveOrderSubmissionService`` / the shadow service). A runner that
    could call the store's write API directly could invent a "simple state
    update" or an auto-repair around the state machine -- exactly what the
    single-boundary design forbids."""
    offenders = []
    for name in RUNNER_MODULES:
        path = REPO_ROOT / "execution" / name
        for function in EXECUTION_STORE_WRITE_FUNCTIONS:
            for lineno in _method_call_sites(path, function) + _name_call_sites(path, function):
                offenders.append(f"execution/{name}:{lineno} calls {function}")
    assert offenders == []
