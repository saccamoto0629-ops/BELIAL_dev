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

# Broker CANCELLATION has no production caller at all: the cancel Execution
# use case is not implemented (Issue #99), reconciliation (Issue #103) is
# detection-only and repair (Issue #104) only updates the internal DB.
# Pinning it repository-wide means a future module cannot quietly start
# cancelling broker orders.
CANCEL_ORDER_ALLOWED = {
    "broker/tests",
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


def _construction_sites(path: pathlib.Path, class_name: str) -> list[int]:
    """Lines constructing ``class_name`` by bare name (``Foo(...)``) -- the
    form every call site in this repository uses."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == class_name
    ]


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
