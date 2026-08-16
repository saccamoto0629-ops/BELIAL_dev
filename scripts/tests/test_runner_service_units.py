"""Issue #134: static checks on the committed Demo / Shadow resident-runner
systemd templates.

These parse ``scripts/systemd/berial-execution-{shadow,demo}.service`` as
plain text/ini -- no systemd needed -- and pin the properties the Issue's
acceptance depends on: resident (Type=simple) services with a restart policy
and a graceful-stop contract (SIGTERM + bounded TimeoutStopSec), ExecStarts
that run the two service CLIs with absolute paths and no shell expansion,
sandboxing that matches the data directories the commands write, and -- the
safety-relevant part -- that the shadow unit carries NO credential source at
all, that the demo unit stays on the demo path with no live-trading switch,
and that the units stay INDEPENDENT of each other and of the reconciliation
units (a runner must never pull in, or be pulled in by, the audit path).

The ExecStart argv <-> CLI parser agreement is pinned separately in
``execution/tests/test_service_cli_systemd_consistency.py`` (it needs the
execution package importable). Live-systemd checks (systemd-analyze verify,
actual enable/stop/restart timing) are documented as manual steps in
docs/operations/operations.md instead of being forced into pytest.
"""

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SYSTEMD_DIR = REPO_ROOT / "scripts" / "systemd"
SHADOW_FILE = SYSTEMD_DIR / "berial-execution-shadow.service"
DEMO_FILE = SYSTEMD_DIR / "berial-execution-demo.service"
RECONCILIATION_FILE = SYSTEMD_DIR / "berial-execution-reconciliation.service"

SHADOW_CLI_MODULE = "execution.shadow_service_cli"
DEMO_CLI_MODULE = "execution.demo_service_cli"


def _parse_unit(path: pathlib.Path) -> dict[str, list[tuple[str, str]]]:
    """Parse a systemd unit into {section: [(key, value), ...]} (unit files
    legally repeat keys, so configparser is unsuitable -- same tiny parser as
    test_reconciliation_units.py)."""
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


def _exec_start_parts(path: pathlib.Path) -> list[str]:
    return _single(_parse_unit(path), "Service", "ExecStart").split()


def _flag_value(parts: list[str], flag: str) -> str:
    assert flag in parts, f"{flag} missing from ExecStart {parts!r}"
    return parts[parts.index(flag) + 1]


def test_unit_files_exist():
    assert SHADOW_FILE.is_file()
    assert DEMO_FILE.is_file()


def test_both_are_resident_services_with_restart_and_graceful_stop():
    """The Issue #134 acceptance is start/stop/restart of RESIDENT runners:
    Type=simple, Restart=always (a crashed runner comes back; a systemctl
    stop does not), SIGTERM as the graceful stop signal and a bounded
    TimeoutStopSec so a hung item cannot stall the stop forever."""
    for path in (SHADOW_FILE, DEMO_FILE):
        sections = _parse_unit(path)
        assert _single(sections, "Service", "Type") == "simple", path.name
        assert _single(sections, "Service", "Restart") == "always", path.name
        assert int(_single(sections, "Service", "RestartSec")) > 0, path.name
        assert _single(sections, "Service", "KillSignal") == "SIGTERM", path.name
        assert int(_single(sections, "Service", "TimeoutStopSec")) > 0, path.name
        assert _single(sections, "Install", "WantedBy") == "multi-user.target", path.name


def test_execstarts_run_the_service_clis_with_absolute_paths():
    for path, module in ((SHADOW_FILE, SHADOW_CLI_MODULE), (DEMO_FILE, DEMO_CLI_MODULE)):
        exec_starts = _values(_parse_unit(path), "Service", "ExecStart")
        assert len(exec_starts) == 1, (path.name, exec_starts)
        parts = exec_starts[0].split()
        # Interpreter and every path are absolute; no shell, no $PATH, no
        # expansion.
        assert parts[0].startswith("/") and parts[0].endswith("/python"), path.name
        assert ".venv" in parts[0], path.name
        assert "$" not in exec_starts[0], path.name
        assert parts[1] == "-m" and parts[2] == module, path.name
        for flag, value in zip(parts, parts[1:]):
            if flag.startswith("--") and not value.startswith("--"):
                if value.lstrip("-").replace(".", "").isdigit():
                    continue
                if flag in ("--execution-mode", "--owner-id"):
                    continue
                assert value.startswith("/"), (path.name, flag, value)


def test_working_directory_env_and_journal():
    for path in (SHADOW_FILE, DEMO_FILE):
        sections = _parse_unit(path)
        assert _single(sections, "Service", "WorkingDirectory").startswith("/"), path.name
        assert _values(sections, "Service", "User"), f"{path.name} must run as an explicit user"
        env_lines = _values(sections, "Service", "Environment")
        # ``python -m execution...`` must resolve the package deterministically.
        assert any(line.startswith("PYTHONPATH=/") for line in env_lines), path.name
        assert _single(sections, "Service", "StandardOutput") == "journal", path.name
        assert _single(sections, "Service", "StandardError") == "journal", path.name


def test_sandbox_writes_are_limited_and_match_the_data_dir():
    """ProtectSystem=strict with exactly the execution data tree writable,
    and the --data-dir each command writes INSIDE that tree -- the invariant
    that keeps the sandbox and the command in agreement. NOT "-<path>"
    (PR #137 Codex review P1): under ProtectSystem=strict a "-" prefix makes
    systemd SKIP a not-yet-created path instead of failing loudly."""
    for path in (SHADOW_FILE, DEMO_FILE):
        sections = _parse_unit(path)
        assert _single(sections, "Service", "ProtectSystem") == "strict", path.name
        assert _single(sections, "Service", "NoNewPrivileges") == "true", path.name
        assert _single(sections, "Service", "ProtectHome") == "true", path.name
        writable = _single(sections, "Service", "ReadWritePaths")
        assert writable.startswith("/"), (path.name, writable)
        assert not writable.startswith("-"), (path.name, writable)
        data_dir = pathlib.PurePosixPath(_flag_value(_exec_start_parts(path), "--data-dir"))
        writable_path = pathlib.PurePosixPath(writable)
        assert data_dir == writable_path or writable_path in data_dir.parents, (
            path.name,
            data_dir,
            writable,
        )


def test_the_shadow_unit_carries_no_credential_source_at_all():
    """The shadow path needs no secret and must not have one available: no
    EnvironmentFile= directive (comments may explain the absence), no broker
    credential variable anywhere in the file, and no live-trading switch."""
    sections = _parse_unit(SHADOW_FILE)
    assert _values(sections, "Service", "EnvironmentFile") == []
    text = SHADOW_FILE.read_text(encoding="utf-8")
    for forbidden in ("OANDA_", "ENABLE_LIVE_TRADING", "oanda-live", "--confirm-live"):
        assert forbidden not in text, forbidden


def test_the_shadow_execstart_stays_on_the_shadow_cli_inputs():
    parts = _exec_start_parts(SHADOW_FILE)
    for flag in ("--signal-db", "--execution-config", "--market-snapshot", "--account-snapshot"):
        assert _flag_value(parts, flag).startswith("/")
    # A stable owner id makes crash-retry claim recovery deterministic.
    assert "--owner-id" in parts


def test_the_demo_unit_never_ships_a_live_trading_switch():
    """The shipped demo unit must stay on the demo path: no live broker
    target, no enabled live-trading environment line and no live
    confirmation flag anywhere in the file (comments included -- a
    commented-out live line is a copy/paste away from being uncommented)."""
    text = DEMO_FILE.read_text(encoding="utf-8")
    for forbidden in ("oanda-live", "ENABLE_LIVE_TRADING=true", "--confirm-live", "OANDA_LIVE_API"):
        assert forbidden not in text, forbidden


def test_the_demo_execstart_declares_the_demo_mode_and_its_inputs():
    parts = _exec_start_parts(DEMO_FILE)
    assert _flag_value(parts, "--execution-mode") == "demo"
    assert _flag_value(parts, "--safety-config").startswith("/")
    assert _flag_value(parts, "--market-snapshot").startswith("/")


def test_the_demo_unit_env_file_is_optional_demo_credentials():
    """The demo unit loads its (uncommitted) credential file with the "-"
    prefix: a missing file must surface as the activation lock's secret-free
    refusal, not as a unit that cannot even start."""
    env_files = _values(_parse_unit(DEMO_FILE), "Service", "EnvironmentFile")
    assert len(env_files) == 1, env_files
    assert env_files[0].startswith("-/"), env_files[0]


def test_the_demo_runner_and_the_reconciliation_unit_audit_the_same_data_dir():
    """Reconciliation must audit the very order store the demo runner
    writes: the two committed templates point at the SAME --data-dir. (Their
    locks are different files inside it, so they never exclude each other.)"""
    demo_dir = _flag_value(_exec_start_parts(DEMO_FILE), "--data-dir")
    recon_dir = _flag_value(_exec_start_parts(RECONCILIATION_FILE), "--data-dir")
    assert demo_dir == recon_dir, (demo_dir, recon_dir)


def test_shadow_and_demo_never_share_a_data_dir():
    """Shadow and demo stores must not share a namespace: a shadow record
    belongs to shadow_results.sqlite3, and the demo order/journal DBs must
    not sit where a second service also writes."""
    shadow_dir = _flag_value(_exec_start_parts(SHADOW_FILE), "--data-dir")
    demo_dir = _flag_value(_exec_start_parts(DEMO_FILE), "--data-dir")
    assert shadow_dir != demo_dir, (shadow_dir, demo_dir)


def test_the_runner_units_are_independent_of_each_other_and_of_reconciliation():
    """Issue #134 keeps the three execution units INDEPENDENT: no unit may
    order itself against, pull in, or propagate its state to another. The
    reconciliation templates are checked from their side in
    test_reconciliation_units.py; this covers the runner side."""
    for path in (SHADOW_FILE, DEMO_FILE):
        text = path.read_text(encoding="utf-8")
        for key in ("Requires=", "BindsTo=", "PartOf=", "Conflicts="):
            assert key not in text, f"{path.name} declares {key}"
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("After=", "Before=", "Wants=")):
                assert "berial-" not in stripped, (path.name, line)


def test_the_demo_unit_refusal_loop_is_documented_not_disabled():
    """Restart=always + a fail-closed exit 1 on refusal means a kill-switched
    deployment flaps by design (and recovers on its own after the switch is
    released). The template must keep the restart policy rather than 'fix'
    the flapping by disabling restarts -- a crashed healthy runner must come
    back without an operator."""
    sections = _parse_unit(DEMO_FILE)
    assert _single(sections, "Service", "Restart") == "always"
