"""Issue #74: execution/ is a real package importable from the repository root,
and the layer boundaries stay intact (Paper layers never import it)."""

import ast
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

ROOT_LEVEL_IMPORTS = (
    "from execution.execution_models import ExecutionRequest",
    "from execution import execution_store",
    "from execution.execution_store import init_db, create_execution_order",
    "from execution.safety_gate import SafetyGate, SafetyPolicy, submit_after_safety_gate",
    "from execution.safety_store import SQLiteSafetyDecisionStore",
)

# Components that must NEVER depend on the execution layer: Paper Signal /
# Paper Execution stay broker-free virtual layers (their execution_* module
# names are their own files, not this package), and market/news observation
# layers have no business ordering anything.
FORBIDDEN_IMPORTERS = ("paper-signal", "paper-execution", "market-watch", "news-feed-bot")


def test_execution_package_has_an_init_file():
    assert (REPO_ROOT / "execution" / "__init__.py").is_file()


def test_root_level_package_imports_succeed_in_a_clean_subprocess():
    code = "; ".join(ROOT_LEVEL_IMPORTS) + "; print('ok')"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"root-level import failed:\n{result.stderr}"
    assert result.stdout.strip() == "ok"


def _imported_top_level_names(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_paper_and_observation_layers_never_import_the_execution_package():
    offenders = []
    for component in FORBIDDEN_IMPORTERS:
        for path in (REPO_ROOT / component).rglob("*.py"):
            if "execution" in _imported_top_level_names(path):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"execution package imported by: {offenders}"


def test_execution_package_only_depends_on_stdlib_and_broker():
    allowed = {
        "annotations", "ast", "contextlib", "dataclasses", "datetime", "decimal",
        "enum", "json", "logging", "os", "pathlib", "sqlite3", "typing",
        "broker", "execution", "__future__",
    }
    for path in (REPO_ROOT / "execution").glob("*.py"):
        names = _imported_top_level_names(path)
        unexpected = names - allowed
        assert unexpected == set(), f"{path.name} imports unexpected modules: {unexpected}"
