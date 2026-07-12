"""Issue #41 review (Major 4): scripts/restart_all.sh regression tests.

Runs the real script through a POSIX ``sh`` with a fake ``systemctl`` placed
FIRST on PATH, and asserts:

* the expected units are restarted in order, including berial-paper-signal.service;
* no existing unit is dropped;
* one unit failing to restart does NOT stop the remaining restarts, and the
  script reports failure (non-zero exit) instead of silently succeeding.

Skipped when no POSIX ``sh`` is available (e.g. a bare Windows shell).
"""

import os
import shutil
import subprocess
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "restart_all.sh"

EXPECTED_ORDER = [
    "berial-news.service",
    "berial-ma-alert.service",
    "berial-macd-alert.service",
    "berial-bollinger-alert.service",
    "berial-paper-signal.service",
    "berial-chart-snapshot.timer",
]

SH = shutil.which("sh") or shutil.which("bash")
pytestmark = pytest.mark.skipif(SH is None, reason="no POSIX sh/bash available")


def _write_fake_systemctl(bin_dir: pathlib.Path, log_path: pathlib.Path) -> None:
    fake = bin_dir / "systemctl"
    fake.write_text(
        "#!/bin/sh\n"
        # restart_all.sh calls: systemctl restart <unit>  ->  $1=restart $2=unit
        'printf "%s\\n" "$2" >> "$SYSTEMCTL_LOG"\n'
        'if [ "$2" = "$FAIL_UNIT" ]; then exit 1; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)


def _run(tmp_path, fail_unit=""):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "calls.log"
    _write_fake_systemctl(bin_dir, log_path)

    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")  # fake FIRST on PATH
    env["SYSTEMCTL_LOG"] = str(log_path)
    env["FAIL_UNIT"] = fail_unit
    env.pop("SYSTEMCTL", None)  # force PATH resolution of the fake

    result = subprocess.run(
        [SH, str(SCRIPT)],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=30,
    )
    calls = log_path.read_text(encoding="utf-8").split() if log_path.exists() else []
    return result, calls


def test_restart_all_restarts_expected_units_in_order(tmp_path):
    result, calls = _run(tmp_path)

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert calls == EXPECTED_ORDER  # exact order, paper-signal included, none dropped


def test_restart_all_includes_paper_signal_and_keeps_existing_units(tmp_path):
    _result, calls = _run(tmp_path)

    assert "berial-paper-signal.service" in calls
    for unit in EXPECTED_ORDER:
        assert unit in calls, f"missing unit: {unit}"


def test_one_failing_unit_does_not_stop_the_rest_and_exit_is_nonzero(tmp_path):
    # Simulate a not-yet-deployed paper-signal unit failing to restart.
    result, calls = _run(tmp_path, fail_unit="berial-paper-signal.service")

    # Units after the failing one are still restarted (no early abort)...
    assert calls == EXPECTED_ORDER
    assert "berial-chart-snapshot.timer" in calls
    # ...and the failure is surfaced as a non-zero exit code.
    assert result.returncode != 0
