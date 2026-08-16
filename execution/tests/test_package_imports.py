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
    "from execution.safety_gate import BrokerConnection, SafetyGate, SafetyPolicy",
    "from execution.safety_store import SQLiteSafetyDecisionStore",
    "from execution.live_submission import LiveOrderSubmissionService",
    "from execution.scenario_models import load_scenarios, validate_scenario",
    "from execution.scenario_runner import main, run_scenarios",
    "from execution.shadow_models import ShadowResult, intended_order_from_request",
    "from execution import shadow_store",
    "from execution.shadow_execution import ShadowExecutionService",
    "from execution.shadow_providers import PaperSignalDbSignalProvider, StoredSignal",
    "from execution.shadow_config import load_shadow_execution_config",
    "from execution.shadow_composition import run_production_shadow_cycle",
    "from execution.reconciliation_models import OrderDiscrepancy, ReconciliationRun",
    "from execution import reconciliation_store",
    "from execution.broker_reconciliation import BrokerOrderReconciliationService",
    "from execution.reconciliation_repair_models import OrderRepairAttempt, RepairMode, RepairOutcome",
    "from execution.broker_reconciliation_repair import BrokerOrderReconciliationRepairService",
    "from execution.sqlite_ro import connect_read_only",
    "from execution.submission_journal import SubmissionJournal, SubmissionJournalEntry",
    "from execution.strict_json import load_strict_json_file",
    "from execution.shadow_runner import main as shadow_runner_main",
    "from execution.shadow_report import main as shadow_report_main",
    "from execution.reconciliation_models import PositionDiscrepancy, PositionReconciliationSummary",
    "from execution.reconciliation_runner import main as reconciliation_runner_main",
    "from execution.live_lock import build_live_connection, evaluate_live_activation",
    "from execution.live_lock import LiveSafetySummary, load_live_safety_policy",
    "from execution.live_startup_cli import main as live_startup_cli_main",
    "from execution.safety_config import parse_safety_policy, classify_endpoint",
    "from execution.demo_lock import build_demo_connection, evaluate_demo_activation",
    "from execution.demo_lock import DemoRuntimeGrant, load_demo_safety_policy",
    "from execution.demo_startup_cli import main as demo_startup_cli_main",
    "from execution.live_runner import ExecutionRunner, RUNNER_SUPPORTED_MODES",
    "from execution.runner_composition import build_demo_execution_runner",
    "from execution.reconciliation_schedule import run_reconciliation_cycle, cycle_lock",
    "from execution.reconciliation_schedule_cli import main as reconciliation_schedule_cli_main",
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
        "annotations", "argparse", "ast", "collections", "contextlib", "dataclasses",
        "datetime", "decimal", "enum", "errno", "hashlib", "json", "logging", "math",
        "os", "pathlib", "re", "sqlite3", "sys", "tempfile", "threading", "time",
        "traceback", "types", "typing", "uuid", "broker", "execution", "__future__",
        # Issue #134: the resident-service boundary routes SIGTERM/SIGINT
        # into the runner's graceful stop (stdlib).
        "signal",
    }
    for path in (REPO_ROOT / "execution").glob("*.py"):
        names = _imported_top_level_names(path)
        unexpected = names - allowed
        assert unexpected == set(), f"{path.name} imports unexpected modules: {unexpected}"
