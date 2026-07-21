"""Issue #67: static checks on the committed PNG-cleanup systemd templates.

Same approach as ``test_systemd_units.py`` (whose parser helpers are reused):
parse ``scripts/systemd/berial-png-cleanup.{service,timer}`` as plain text and
pin the properties the Issue's acceptance depends on -- oneshot service run by
an explicit user, journal logging, a sandbox that only allows writing the
chart image directory and /tmp (with "-" on the chart directory so a missing
target stays a runtime WARNING instead of a namespace failure), a lock file
shared with manual CLI runs (the script's /tmp default, which requires that
PrivateTmp is NOT enabled -- a private /tmp would defeat the mutual
exclusion), and a daily persistent Asia/Tokyo timer. Live-systemd checks
(``systemd-analyze verify``, enable/trigger) stay manual steps in
docs/operations/png_cleanup.md.
"""

import pathlib
import re

from test_systemd_units import _parse_unit, _single, _values

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SYSTEMD_DIR = REPO_ROOT / "scripts" / "systemd"
SERVICE_FILE = SYSTEMD_DIR / "berial-png-cleanup.service"
TIMER_FILE = SYSTEMD_DIR / "berial-png-cleanup.timer"


def _env_value(sections, name: str) -> str:
    for line in _values(sections, "Service", "Environment"):
        key, _, value = line.strip('"').partition("=")
        if key == name:
            return value
    raise AssertionError(f"missing Environment {name}")


def test_unit_files_exist():
    assert SERVICE_FILE.is_file()
    assert TIMER_FILE.is_file()


def test_service_is_oneshot_without_restart_loop():
    sections = _parse_unit(SERVICE_FILE)
    assert _single(sections, "Service", "Type") == "oneshot"
    # A failing oneshot must stay failed until the next timer run -- no Restart=.
    assert _values(sections, "Service", "Restart") == []


def test_service_runs_as_explicit_user_and_logs_to_journal():
    sections = _parse_unit(SERVICE_FILE)
    assert _values(sections, "Service", "User"), "service must run as an explicit user"
    assert _single(sections, "Service", "StandardOutput") == "journal"
    assert _single(sections, "Service", "StandardError") == "journal"


def test_service_execstart_is_absolute_and_shell_free():
    sections = _parse_unit(SERVICE_FILE)
    exec_start = _single(sections, "Service", "ExecStart")
    parts = exec_start.split()
    # Interpreter and script are absolute (no shell, no $PATH), targets come
    # from the environment, and the timer run is a REAL run, not a dry-run.
    assert parts[0].startswith("/") and parts[0].endswith("/python")
    assert ".venv" in parts[0]
    assert parts[1].startswith("/") and parts[1].endswith("cleanup_png.py")
    assert "--dry-run" not in parts
    assert "$" not in exec_start


def test_service_sandbox_limits_writes_to_chart_images_and_tmp():
    sections = _parse_unit(SERVICE_FILE)
    assert _single(sections, "Service", "ProtectSystem") == "strict"
    assert _single(sections, "Service", "ProtectHome").lower() == "true"
    assert _single(sections, "Service", "NoNewPrivileges").lower() == "true"
    raw_paths = _single(sections, "Service", "ReadWritePaths").split()
    assert {path.lstrip("-") for path in raw_paths} <= {
        "/opt/berial/market-watch/data/chart_images",
        "/tmp",
    }
    # "-" keeps a missing chart_images directory from failing namespace setup,
    # so the script can report the missing target as a WARNING and exit 0
    # (Issue #67) instead of never starting.
    assert "-/opt/berial/market-watch/data/chart_images" in raw_paths


def test_service_targets_are_inside_the_writable_sandbox():
    sections = _parse_unit(SERVICE_FILE)
    write_paths = [path.lstrip("-") for path in _single(sections, "Service", "ReadWritePaths").split()]
    for target in _env_value(sections, "PNG_CLEANUP_TARGETS").split(":"):
        assert target in write_paths, f"target {target} must be in ReadWritePaths"


def test_service_lock_is_the_cli_default_and_tmp_is_not_private():
    sections = _parse_unit(SERVICE_FILE)
    lock_file = _env_value(sections, "PNG_CLEANUP_LOCK_FILE")
    # Manual CLI runs and the timer must contend on the SAME flock, so the
    # service pins the script's default lock path...
    source = (REPO_ROOT / "scripts" / "cleanup_png.py").read_text(encoding="utf-8")
    match = re.search(r'^DEFAULT_LOCK_FILE = "([^"]+)"$', source, re.MULTILINE)
    assert match, "cleanup_png.py must define DEFAULT_LOCK_FILE"
    assert lock_file == match.group(1)
    assert not lock_file.endswith(".png")
    # ...and must not remap /tmp into a private namespace, which would give
    # the service its own lock file and defeat the mutual exclusion.
    for value in _values(sections, "Service", "PrivateTmp"):
        assert value.lower() != "true", "PrivateTmp would namespace the /tmp lock away from manual runs"


def test_timer_fires_daily_0320_tokyo_and_is_persistent():
    sections = _parse_unit(TIMER_FILE)
    assert _single(sections, "Timer", "OnCalendar") == "*-*-* 03:20:00 Asia/Tokyo"
    assert _single(sections, "Timer", "Persistent").lower() == "true"
    assert _single(sections, "Install", "WantedBy") == "timers.target"


def test_timer_references_the_committed_service_unit():
    sections = _parse_unit(TIMER_FILE)
    assert _single(sections, "Timer", "Unit") == SERVICE_FILE.name
