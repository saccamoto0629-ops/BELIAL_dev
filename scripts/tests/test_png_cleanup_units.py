"""Issue #67: static checks on the committed PNG-cleanup systemd templates.

Same approach as ``test_systemd_units.py`` (whose parser helpers are reused):
parse ``scripts/systemd/berial-png-cleanup.{service,timer}`` as plain text and
pin the properties the Issue's acceptance depends on -- oneshot service run by
an explicit user, journal logging, sandbox that only allows writing the chart
image directory (and private /tmp), a lock file that is shared with manual
runs (NOT under /tmp, because ``PrivateTmp=true`` would make a /tmp lock
useless for mutual exclusion), and a daily persistent Asia/Tokyo timer.
Live-systemd checks (``systemd-analyze verify``, enable/trigger) stay manual
steps in docs/operations/png_cleanup.md.
"""

import pathlib

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
    write_paths = _single(sections, "Service", "ReadWritePaths").split()
    assert set(write_paths) <= {"/opt/berial/market-watch/data/chart_images", "/tmp"}


def test_service_targets_are_inside_the_writable_sandbox():
    sections = _parse_unit(SERVICE_FILE)
    write_paths = _single(sections, "Service", "ReadWritePaths").split()
    for target in _env_value(sections, "PNG_CLEANUP_TARGETS").split(":"):
        assert target in write_paths, f"target {target} must be in ReadWritePaths"


def test_service_lock_file_is_shared_and_not_under_private_tmp():
    sections = _parse_unit(SERVICE_FILE)
    assert _single(sections, "Service", "PrivateTmp").lower() == "true"
    lock_file = _env_value(sections, "PNG_CLEANUP_LOCK_FILE")
    # With PrivateTmp=true a /tmp lock can never exclude a concurrent manual
    # run, so the lock must live in a shared writable directory -- and it must
    # not look like a cleanup candidate itself.
    assert not lock_file.startswith("/tmp/")
    assert not lock_file.endswith(".png")
    write_paths = _single(sections, "Service", "ReadWritePaths").split()
    assert any(lock_file.startswith(path + "/") for path in write_paths)


def test_timer_fires_daily_0320_tokyo_and_is_persistent():
    sections = _parse_unit(TIMER_FILE)
    assert _single(sections, "Timer", "OnCalendar") == "*-*-* 03:20:00 Asia/Tokyo"
    assert _single(sections, "Timer", "Persistent").lower() == "true"
    assert _single(sections, "Install", "WantedBy") == "timers.target"


def test_timer_references_the_committed_service_unit():
    sections = _parse_unit(TIMER_FILE)
    assert _single(sections, "Timer", "Unit") == SERVICE_FILE.name
