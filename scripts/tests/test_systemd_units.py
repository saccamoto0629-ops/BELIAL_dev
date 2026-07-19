"""Issue #71: static checks on the committed daily-report systemd templates.

These parse ``scripts/systemd/berial-paper-signal-report.{service,timer}`` as
plain text/ini -- no systemd needed -- and pin the properties the Issue's
acceptance depends on: oneshot service, daily 00:10 Asia/Tokyo timer with
``Persistent=true``, ``--previous-day`` + Markdown + absolute paths in
ExecStart, matching service/timer unit references, and a timer timezone that
agrees with the CLI ``--timezone``. Live-systemd checks (``systemd-analyze
verify``, actual enable/trigger) are documented as manual steps in
docs/operations/operations.md instead of being forced into pytest.
"""

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SYSTEMD_DIR = REPO_ROOT / "scripts" / "systemd"
SERVICE_FILE = SYSTEMD_DIR / "berial-paper-signal-report.service"
TIMER_FILE = SYSTEMD_DIR / "berial-paper-signal-report.timer"


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
    # A failing oneshot must stay failed until the next timer run -- no Restart=.
    assert _values(sections, "Service", "Restart") == []


def test_service_execstart_previous_day_markdown_absolute_paths():
    sections = _parse_unit(SERVICE_FILE)
    exec_start = _single(sections, "Service", "ExecStart")
    parts = exec_start.split()

    assert "--previous-day" in parts
    assert "--format" in parts and parts[parts.index("--format") + 1] == "markdown"
    # Interpreter, script, and output dir are all absolute (no shell, no $PATH).
    assert parts[0].startswith("/") and parts[0].endswith("/python")
    assert ".venv" in parts[0]
    assert parts[1].startswith("/") and parts[1].endswith("paper_signal_daily_report.py")
    assert "--output-dir" in parts
    assert parts[parts.index("--output-dir") + 1].startswith("/")
    # No shell $(date ...) style expansion anywhere in the command.
    assert "$" not in exec_start


def test_service_working_directory_and_env():
    sections = _parse_unit(SERVICE_FILE)
    assert _single(sections, "Service", "WorkingDirectory").startswith("/")
    assert _values(sections, "Service", "User"), "service must run as an explicit user"
    env_lines = _values(sections, "Service", "Environment")
    assert any("PAPER_SIGNAL_REPORT_TIMEZONE=Asia/Tokyo" in line for line in env_lines)


def test_service_logs_to_journal():
    sections = _parse_unit(SERVICE_FILE)
    assert _single(sections, "Service", "StandardOutput") == "journal"
    assert _single(sections, "Service", "StandardError") == "journal"


def test_timer_fires_daily_0010_tokyo_and_is_persistent():
    sections = _parse_unit(TIMER_FILE)
    assert _single(sections, "Timer", "OnCalendar") == "*-*-* 00:10:00 Asia/Tokyo"
    assert _single(sections, "Timer", "Persistent").lower() == "true"
    assert _single(sections, "Install", "WantedBy") == "timers.target"


def test_timer_references_the_committed_service_unit():
    sections = _parse_unit(TIMER_FILE)
    assert _single(sections, "Timer", "Unit") == SERVICE_FILE.name


def test_timer_timezone_matches_cli_timezone():
    timer_calendar = _single(_parse_unit(TIMER_FILE), "Timer", "OnCalendar")
    exec_start = _single(_parse_unit(SERVICE_FILE), "Service", "ExecStart")
    match = re.search(r"--timezone\s+(\S+)", exec_start)
    assert match, "ExecStart must pass an explicit --timezone"
    assert match.group(1) in timer_calendar, "timer and CLI must use the same timezone"
