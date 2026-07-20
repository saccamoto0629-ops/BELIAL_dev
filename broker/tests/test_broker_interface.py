"""Issue #73 / PR #87: the Broker Protocol boundary and its dependency direction.

NOTE on the runtime Protocol check: ``@runtime_checkable`` ``isinstance()``
only verifies that the required ATTRIBUTES exist — it does NOT verify
argument or return types. This repository runs no static type checker
(mypy / pyright), so full signature conformance is asserted by the
behavioural tests in ``test_fake_broker.py``, not by ``isinstance``.
"""

import ast
import pathlib

from broker import broker_interface, fake_broker

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _imported_modules(source: str) -> set[str]:
    """All module names imported by ``source`` (AST-based; ignores docstrings
    and comments)."""
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
            # relative "from . import x" style also names the module via alias
            if node.module is None:
                imported.update(alias.name for alias in node.names)
    return imported


def test_fake_broker_has_all_broker_protocol_attributes():
    # Attribute-presence check only (see module docstring): runtime_checkable
    # Protocols do not validate signatures or types.
    assert isinstance(fake_broker.FakeBroker(), broker_interface.Broker)


def test_protocol_defines_exactly_the_five_issue_73_operations():
    expected = {
        "submit_order",       # 注文送信
        "cancel_order",       # 注文キャンセル
        "get_order",          # 注文照会
        "get_positions",      # ポジション照会
        "get_account_state",  # 口座状態照会
    }
    defined = {
        name for name in vars(broker_interface.Broker)
        if not name.startswith("_") and callable(getattr(broker_interface.Broker, name))
    }
    assert expected == defined


def test_an_object_missing_protocol_attributes_is_not_a_broker():
    class NotABroker:
        def submit_order(self, request):
            return None

    assert not isinstance(NotABroker(), broker_interface.Broker)


def test_interface_module_does_not_depend_on_fake_broker_or_adapters():
    # Dependency direction (Issue #73): FakeBroker depends on the interface,
    # never the other way around; no broker-specific (e.g. OANDA) module exists.
    source = pathlib.Path(broker_interface.__file__).read_text(encoding="utf-8")
    imported = _imported_modules(source)
    assert "fake_broker" not in imported
    assert not any("oanda" in name.lower() for name in imported)


# Old flat-module spellings kept forbidden so a stale sys.path-style import
# cannot sneak back in alongside the package form.
FORBIDDEN_BROKER_MODULES = {"broker", "broker_interface", "broker_models", "fake_broker"}


def test_no_upstream_component_imports_the_broker_layer():
    """Strategy / Paper Signal / Paper Execution / Backtest must not import
    the broker package (Issue #73 dependency rule). Recursive (rglob) and
    AST-based, so prose mentions in docstrings/comments do not fail it."""
    components = ("paper-execution", "paper-signal", "backtest")
    checked = 0
    for component in components:
        for path in (REPO_ROOT / component).rglob("*.py"):
            imported = _imported_modules(path.read_text(encoding="utf-8"))
            hits = {
                name for name in imported
                if name in FORBIDDEN_BROKER_MODULES
                or name.split(".")[0] in FORBIDDEN_BROKER_MODULES
            }
            assert not hits, f"{path} imports broker layer: {sorted(hits)}"
            checked += 1
    assert checked > 0, "dependency scan found no files -- component paths wrong?"
