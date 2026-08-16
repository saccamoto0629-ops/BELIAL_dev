"""Issue #133: static checks on the committed reconciliation systemd templates.

These parse ``scripts/systemd/berial-execution-reconciliation.{service,timer}``
as plain text/ini -- no systemd needed -- and pin the properties the Issue's
acceptance depends on: a ONESHOT service with no restart loop, a periodic
timer that references it, an ExecStart that runs the scheduled reconcile-only
CLI with absolute paths and no shell expansion, and -- the safety-relevant
part -- that the shipped unit stays on the reconcile-only demo path and never
carries a live-trading switch.

Live-systemd checks (``systemd-analyze verify``, actual enable/trigger,
timer accuracy) are documented as manual steps in docs/operations/operations.md
instead of being forced into pytest.
"""

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SYSTEMD_DIR = REPO_ROOT / "scripts" / "systemd"
SERVICE_FILE = SYSTEMD_DIR / "berial-execution-reconciliation.service"
TIMER_FILE = SYSTEMD_DIR / "berial-execution-reconciliation.timer"

CLI_MODULE = "execution.reconciliation_schedule_cli"


def _parse_unit(path: pathlib.Path) -> dict[str, list[tuple[str, str]]]:
    """Parse a systemd unit into {section: [(key, value), ...]}.

    A tiny hand-rolled parser (not configparser) because unit files legally
    repeat keys (e.g. two ``Environment=`` lines) and configparser drops or
    rejects duplicates.
    """
    sections: dict[str, list[tuple[str, str]]] = {}
    current = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
            continue
        key, _, value = line.partition("=")
        sections.setdefault(current, []).append((key.strip(), value.strip()))
    return sections


def _values(sections, section: str, key: str) -> list[str]:
    return [value for k, value in sections.get(section, []) if k == key]


def _single(sections, section: str, key: str) -> str:
    values = _values(sections, section, key)
    assert len(values) == 1, f"expected exactly one {section}/{key}, got {values!r}"
    return values[0]


def test_unit_files_exist():
    assert SERVICE_FILE.is_file()
    assert TIMER_FILE.is_file()


def test_service_is_oneshot_without_restart_loop():
    sections = _parse_unit(SERVICE_FILE)
    assert _single(sections, "Service", "Type") == "oneshot"
    # A failing oneshot must stay failed until the next timer run: a Restart=
    # here would hammer a broker that is already refusing queries.
    assert _values(sections, "Service", "Restart") == []


def test_service_runs_exactly_one_reconciliation_cycle_per_activation():
    """systemd runs EVERY ExecStart of a oneshot service in sequence, so a
    second (e.g. an uncommented --repair variant left next to the default)
    would silently run two cycles per tick."""
    exec_starts = _values(_parse_unit(SERVICE_FILE), "Service", "ExecStart")
    assert len(exec_starts) == 1, exec_starts


def test_service_execstart_runs_the_scheduled_cli_with_absolute_paths():
    sections = _parse_unit(SERVICE_FILE)
    exec_start = _single(sections, "Service", "ExecStart")
    parts = exec_start.split()

    # Interpreter and data dir are absolute; no shell, no $PATH, no expansion.
    assert parts[0].startswith("/") and parts[0].endswith("/python")
    assert ".venv" in parts[0]
    assert "$" not in exec_start
    assert parts[1] == "-m" and parts[2] == CLI_MODULE
    assert "--data-dir" in parts
    assert parts[parts.index("--data-dir") + 1].startswith("/")
    assert "--broker" in parts and parts[parts.index("--broker") + 1] == "oanda-demo"


def test_service_never_ships_a_live_trading_switch():
    """The shipped unit must stay on the reconcile-only demo path: no live
    broker target and no live-trading environment anywhere in the file
    (comments included -- a commented-out live line is a copy/paste away from
    being uncommented)."""
    text = SERVICE_FILE.read_text(encoding="utf-8")
    for forbidden in ("oanda-live", "ENABLE_LIVE_TRADING=true", "--confirm-live"):
        assert forbidden not in text, forbidden


def test_service_working_directory_and_env():
    sections = _parse_unit(SERVICE_FILE)
    assert _single(sections, "Service", "WorkingDirectory").startswith("/")
    assert _values(sections, "Service", "User"), "service must run as an explicit user"
    env_lines = _values(sections, "Service", "Environment")
    # ``python -m execution...`` must resolve the package deterministically.
    assert any(line.startswith("PYTHONPATH=/") for line in env_lines)


def test_service_logs_to_journal():
    sections = _parse_unit(SERVICE_FILE)
    assert _single(sections, "Service", "StandardOutput") == "journal"
    assert _single(sections, "Service", "StandardError") == "journal"


def test_service_bounds_one_cycle_so_a_hang_cannot_hold_the_entry_lock_forever():
    timeout = _single(_parse_unit(SERVICE_FILE), "Service", "TimeoutStartSec")
    assert int(timeout) > 0


def test_service_may_write_the_execution_data_directory_only():
    sections = _parse_unit(SERVICE_FILE)
    assert _single(sections, "Service", "ProtectSystem") == "strict"
    writable = _single(sections, "Service", "ReadWritePaths")
    # The audit DB, the order DB and the cycle lock all live there.
    assert writable.startswith("/")
    # NOT "-<path>" (PR #137 Codex review P1): under ProtectSystem=strict, a
    # "-" prefix makes systemd SKIP a not-yet-created path instead of failing,
    # leaving /opt/berial read-only -- every activation would then die creating
    # the data directory, with nothing in the journal pointing at the unit.
    assert not writable.startswith("-"), writable


def test_the_data_dir_the_cli_writes_is_inside_the_writable_path():
    """The one invariant that keeps the sandbox and the command in agreement:
    a --data-dir moved outside ReadWritePaths would be read-only at runtime."""
    sections = _parse_unit(SERVICE_FILE)
    writable = pathlib.PurePosixPath(_single(sections, "Service", "ReadWritePaths"))
    parts = _single(sections, "Service", "ExecStart").split()
    data_dir = pathlib.PurePosixPath(parts[parts.index("--data-dir") + 1])
    assert data_dir == writable or writable in data_dir.parents, (data_dir, writable)


def test_timer_is_periodic_persistent_and_references_the_committed_service():
    sections = _parse_unit(TIMER_FILE)
    calendar = _single(sections, "Timer", "OnCalendar")
    assert re.fullmatch(r"\*:0/\d+:00 UTC", calendar), calendar
    assert _single(sections, "Timer", "Persistent").lower() == "true"
    assert _single(sections, "Timer", "Unit") == SERVICE_FILE.name
    assert _single(sections, "Install", "WantedBy") == "timers.target"


def test_timer_randomized_delay_stays_below_the_period():
    """A jitter as long as the period could push a tick past the next one."""
    sections = _parse_unit(TIMER_FILE)
    period_minutes = int(re.fullmatch(r"\*:0/(\d+):00 UTC", _single(sections, "Timer", "OnCalendar")).group(1))
    delay = _single(sections, "Timer", "RandomizedDelaySec")
    match = re.fullmatch(r"(\d+)m", delay)
    assert match, f"expected a minute-valued RandomizedDelaySec, got {delay!r}"
    assert int(match.group(1)) < period_minutes


def test_the_reconciliation_units_are_independent_of_any_execution_runner():
    """Issue #133 keeps reconciliation a SEPARATE execution unit: the units
    must not order themselves against, or pull in, a submission runner."""
    for path in (SERVICE_FILE, TIMER_FILE):
        text = path.read_text(encoding="utf-8")
        for key in ("Requires=", "BindsTo=", "PartOf="):
            assert key not in text, f"{path.name} declares {key}"
        for section_key in ("After=", "Before=", "Wants="):
            for line in text.splitlines():
                if line.strip().startswith(section_key):
                    assert "runner" not in line, line
