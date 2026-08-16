"""Issue #134: the committed systemd templates and the service CLIs must
agree on the command line.

The static shape of the units is pinned in
``scripts/tests/test_runner_service_units.py`` (no execution import there);
THIS suite closes the remaining gap -- an ExecStart whose argv the CLI
parser refuses, or whose values contradict the CLI's contract, would only
surface on the deployed host. Each unit's ExecStart is parsed exactly the
way ``python -m <module>`` would parse it.
"""

import pathlib

import pytest

import execution.demo_service_cli as demo_service_cli
import execution.shadow_service_cli as shadow_service_cli
from execution.reconciliation_schedule import DEFAULT_CYCLE_LOCK_FILENAME
from execution.runner_service import (
    EXECUTION_RUNNER_LOCK_FILENAME,
    SHADOW_RUNNER_LOCK_FILENAME,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SYSTEMD_DIR = REPO_ROOT / "scripts" / "systemd"

UNITS = {
    "berial-execution-shadow.service": ("execution.shadow_service_cli", shadow_service_cli),
    "berial-execution-demo.service": ("execution.demo_service_cli", demo_service_cli),
}


def exec_start_argv(unit_name: str) -> list[str]:
    for raw_line in (SYSTEMD_DIR / unit_name).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("ExecStart="):
            return line[len("ExecStart="):].split()
    raise AssertionError(f"{unit_name} has no ExecStart")


def parsed_unit_args(unit_name: str):
    module_name, module = UNITS[unit_name]
    argv = exec_start_argv(unit_name)
    assert argv[1] == "-m" and argv[2] == module_name, argv[:3]
    # Exactly what ``python -m <module>`` hands to main(): everything after
    # the module name. parse_args raises SystemExit(2) on anything unknown
    # or malformed, which fails the test loudly.
    return module._build_parser().parse_args(argv[3:])


@pytest.mark.parametrize("unit_name", sorted(UNITS))
def test_the_execstart_argv_is_accepted_by_the_cli_parser(unit_name):
    args = parsed_unit_args(unit_name)
    assert args.interval_seconds > 0
    # The committed templates are RESIDENT services: no bounded run, no dry
    # run, no JSON summary mode baked into the unit.
    assert args.max_cycles is None
    assert args.dry_run is False
    assert args.json is False


def test_the_shadow_unit_matches_the_shadow_cli_contract():
    args = parsed_unit_args("berial-execution-shadow.service")
    assert args.data_dir is not None
    assert args.owner_id, "a stable owner id makes claim recovery deterministic"
    for path in (args.signal_db, args.execution_config, args.market_snapshot, args.account_snapshot):
        # .as_posix(): argparse type=Path yields WindowsPath on the Windows
        # CI job, whose str() spells the POSIX unit path with backslashes.
        assert path.as_posix().startswith("/"), path


def test_the_demo_unit_matches_the_demo_cli_contract():
    args = parsed_unit_args("berial-execution-demo.service")
    assert args.execution_mode == "demo"
    assert args.data_dir is not None
    assert args.safety_config.as_posix().startswith("/")
    assert args.market_snapshot.as_posix().startswith("/")
    # The Kill Switch DB stays at its global default: ONE switch stops
    # everything, so the unit must not point the runner at a private copy.
    assert args.kill_switch_db is None


def test_the_default_lock_files_keep_the_execution_units_independent():
    """Demo runner and reconciliation share one data directory on purpose
    (the audit must see the runner's stores); their default lock FILES must
    therefore differ, or one would suppress the other. The shadow lock is a
    third name for the same reason."""
    assert len(
        {
            EXECUTION_RUNNER_LOCK_FILENAME,
            SHADOW_RUNNER_LOCK_FILENAME,
            DEFAULT_CYCLE_LOCK_FILENAME,
        }
    ) == 3


def test_the_demo_unit_and_the_reconciliation_unit_share_the_data_dir():
    """Cross-checked here with the CLIs' own parsers on the demo side: the
    reconciliation oneshot audits exactly the data directory the demo
    runner writes."""
    demo_args = parsed_unit_args("berial-execution-demo.service")
    recon_argv = exec_start_argv("berial-execution-reconciliation.service")
    recon_data_dir = recon_argv[recon_argv.index("--data-dir") + 1]
    # .as_posix() for the Windows CI job (see above).
    assert demo_args.data_dir.as_posix() == recon_data_dir
