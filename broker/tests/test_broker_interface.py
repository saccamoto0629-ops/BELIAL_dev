"""Issue #73: the Broker Protocol boundary and its dependency direction."""

import pathlib
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

import broker_interface  # noqa: E402
import fake_broker  # noqa: E402


def test_fake_broker_satisfies_the_broker_protocol():
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


def test_an_incomplete_implementation_is_not_a_broker():
    class NotABroker:
        def submit_order(self, request):
            return None

    assert not isinstance(NotABroker(), broker_interface.Broker)


def test_interface_module_does_not_depend_on_fake_broker_or_adapters():
    # Dependency direction (Issue #73): FakeBroker depends on the interface,
    # never the other way around; no broker-specific (e.g. OANDA) module exists.
    import ast

    source = pathlib.Path(broker_interface.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "fake_broker" not in imported
    assert not any("oanda" in name.lower() for name in imported)


def test_paper_execution_and_paper_signal_do_not_import_the_broker_layer():
    # Strategy / Paper Signal / Paper Execution must not import FakeBroker or
    # broker models (Issue #73 dependency rule).
    repo_root = BASE_DIR.parent
    for component in ("paper-execution", "paper-signal"):
        for path in (repo_root / component).glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for forbidden in ("fake_broker", "broker_interface", "broker_models"):
                assert forbidden not in source, f"{path} imports {forbidden}"
