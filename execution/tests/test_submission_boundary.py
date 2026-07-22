"""Issue #75: the Safety Gate submission boundary is enforced STRUCTURALLY.

``submit_after_safety_gate`` must be the only path from an ExecutionRequest to
``Broker.submit_order``. These AST scans pin, repository-wide, that no other
module grows a direct ``submit_order`` / ``to_order_request`` call -- so the
gate cannot be bypassed by "careful callers"; a bypass fails this suite.
"""

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# The only files that may CALL <something>.submit_order(...):
# - broker/fake_broker.py defines the test double (its submit_order is the
#   implementation, and it never calls itself);
# - broker/tests exercise the broker boundary itself (Issue #73);
# - execution/safety_gate.py holds THE single gated call.
SUBMIT_ORDER_ALLOWED = {
    "broker/tests",
    "execution/safety_gate.py",
}

# The only files that may CALL <something>.to_order_request(...):
# - execution/safety_gate.py converts AFTER a recorded allow decision;
# - execution/tests/test_execution_models.py unit-tests the model itself.
TO_ORDER_REQUEST_ALLOWED = {
    "execution/safety_gate.py",
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


def test_safety_gate_module_holds_exactly_one_submit_order_call():
    sites = _method_call_sites(REPO_ROOT / "execution" / "safety_gate.py", "submit_order")
    assert len(sites) == 1, (
        "execution/safety_gate.py must hold exactly one Broker.submit_order call "
        f"(the gated one in submit_after_safety_gate), found lines {sites}"
    )
