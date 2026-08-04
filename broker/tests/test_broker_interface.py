"""Issue #73 / PR #87: the Broker Protocol boundary and its dependency direction.

NOTE on the runtime Protocol check: ``@runtime_checkable`` ``isinstance()``
only verifies that the required ATTRIBUTES exist — it does NOT verify
argument or return types. This repository runs no static type checker
(mypy / pyright), so nothing here GUARANTEES signature conformance for any
implementation. The behavioural tests in ``test_fake_broker.py`` exercise
FakeBroker's concrete behaviour only; a future adapter gets no conformance
check from this suite and needs its own tests (and/or a type checker).
"""

import ast
import pathlib

from broker import broker_interface, fake_broker

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _dynamic_import_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Names that actually refer to importlib machinery in this module.

    Returns ``(function_names, module_names)``: local names bound to
    ``importlib.import_module`` via ``from importlib import import_module
    [as alias]``, and local names bound to the ``importlib`` module itself
    via ``import importlib [as alias]`` / ``import importlib.<sub>``. An
    unrelated user-defined ``import_module`` function or a
    ``loader.import_module(...)`` call therefore does NOT match."""
    function_names: set[str] = {"__import__"}
    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    function_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib" or alias.name.startswith("importlib."):
                    module_names.add(alias.asname or alias.name.split(".")[0])
    return function_names, module_names


def _is_dynamic_import_call(
    node: ast.Call, function_names: set[str], module_names: set[str]
) -> bool:
    """True for ``__import__(...)``, ``importlib.import_module(...)`` and
    calls through names genuinely bound to importlib's import_module."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in function_names
    if isinstance(func, ast.Attribute):
        return (
            func.attr == "import_module"
            and isinstance(func.value, ast.Name)
            and func.value.id in module_names
        )
    return False


def _imported_modules(source: str) -> set[str]:
    """All module names imported by ``source`` (AST-based; ignores docstrings
    and comments).

    Covers static ``import`` / ``from ... import`` AND the dynamic forms
    ``importlib.import_module("x")`` / ``__import__("x")``. A dynamic import
    whose target is not a string literal cannot be resolved statically, so it
    is reported conservatively as ``<dynamic-import:non-literal>`` (no scanned
    component uses dynamic imports today, so this fails loudly instead of
    silently missing a broker import)."""
    imported = set()
    tree = ast.parse(source)
    function_names, module_names = _dynamic_import_bindings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
            # relative "from . import x" style also names the module via alias
            if node.module is None:
                imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call) and _is_dynamic_import_call(
            node, function_names, module_names
        ):
            target = node.args[0] if node.args else None
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                imported.add(target.value)
            else:
                imported.add("<dynamic-import:non-literal>")
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
# cannot sneak back in alongside the package form. The non-literal marker
# makes an unresolvable dynamic import fail the scan conservatively.
FORBIDDEN_BROKER_MODULES = {
    "broker", "broker_interface", "broker_models", "fake_broker",
    "<dynamic-import:non-literal>",
}


def test_imported_modules_helper_detects_dynamic_imports():
    source = (
        "import importlib\n"
        "importlib.import_module('broker.fake_broker')\n"
        "__import__('broker_models')\n"
        "from importlib import import_module as im\n"
        "im('broker')\n"
        "importlib.import_module(some_variable)\n"
    )
    imported = _imported_modules(source)
    assert "broker.fake_broker" in imported
    assert "broker_models" in imported
    assert "broker" in imported
    assert "<dynamic-import:non-literal>" in imported


def test_imported_modules_helper_ignores_unrelated_import_module_names():
    # A user-defined function or an attribute on a non-importlib object that
    # happens to be called "import_module" is NOT a dynamic import and must
    # not trip the conservative non-literal marker.
    source = (
        "def import_module(name):\n"
        "    return name\n"
        "import_module(variable)\n"
        "loader.import_module(variable)\n"
    )
    imported = _imported_modules(source)
    assert "<dynamic-import:non-literal>" not in imported
    assert imported == set()


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


def test_order_listing_is_a_separate_optional_protocol():
    """Issue #103: enumeration is an ADDITIVE capability. ``Broker`` keeps
    exactly the five Issue #73 operations (pinned above), and an
    implementation that only provides those still satisfies ``Broker``."""
    defined = {
        name for name in vars(broker_interface.OrderListingBroker)
        if not name.startswith("_")
        and callable(getattr(broker_interface.OrderListingBroker, name))
    }
    assert defined == {"list_orders"}
    assert "list_orders" not in vars(broker_interface.Broker)

    class FiveOperationBroker:
        def submit_order(self, request): ...
        def cancel_order(self, client_order_id): ...
        def get_order(self, client_order_id): ...
        def get_positions(self): ...
        def get_account_state(self): ...

    assert isinstance(FiveOperationBroker(), broker_interface.Broker)
    assert not isinstance(FiveOperationBroker(), broker_interface.OrderListingBroker)


def test_fake_broker_additionally_satisfies_the_order_listing_protocol():
    assert isinstance(fake_broker.FakeBroker(), broker_interface.OrderListingBroker)
