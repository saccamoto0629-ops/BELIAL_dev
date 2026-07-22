"""Issue #75: the Live Execution submission boundary is enforced STRUCTURALLY.

``LiveOrderSubmissionService`` (execution/live_submission.py) must be the only
path from an ExecutionRequest to ``Broker.submit_order``. These AST scans pin,
repository-wide, that no other module grows a direct ``submit_order`` /
``to_order_request`` call -- so the service boundary cannot be bypassed by
"careful callers"; a bypass fails this suite.
"""

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# The only files that may CALL <something>.submit_order(...):
# - broker/tests exercise the broker boundary itself (Issue #73);
# - execution/live_submission.py holds THE single gated call.
# (broker/fake_broker.py only DEFINES submit_order; a def is not a call.)
SUBMIT_ORDER_ALLOWED = {
    "broker/tests",
    "execution/live_submission.py",
}

# The only files that may CALL <something>.to_order_request(...):
# - execution/live_submission.py converts AFTER the decision was durably
#   recorded, read back, and the created -> submitting transition was won;
# - execution/tests/test_execution_models.py unit-tests the model itself.
TO_ORDER_REQUEST_ALLOWED = {
    "execution/live_submission.py",
    "execution/tests/test_execution_models.py",
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


def test_to_order_request_is_called_only_by_the_safety_gate_and_model_tests():
    assert _offenders("to_order_request", TO_ORDER_REQUEST_ALLOWED) == []


def test_submission_service_holds_exactly_one_submit_order_call():
    sites = _method_call_sites(REPO_ROOT / "execution" / "live_submission.py", "submit_order")
    assert len(sites) == 1, (
        "execution/live_submission.py must hold exactly one Broker.submit_order call "
        f"(the gated one in LiveOrderSubmissionService.submit), found lines {sites}"
    )
