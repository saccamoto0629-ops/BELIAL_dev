"""Issue #134: the OANDA Demo resident service CLI
(``python -m execution.demo_service_cli``).

NOTHING HERE OPENS A SOCKET: the demo activation lock is entered through its
private offline seam (a scripted in-process transport), exactly like
execution/tests/test_demo_runner.py -- the CLI's ``build_demo_connection``
call site is monkeypatched onto that seam, and every lock is still evaluated
for real.

What is pinned:

- a bounded run drains a pending intent through the full production chain
  (activation lock -> grant -> runner -> Safety Gate -> the ONE boundary ->
  Journal -> demo adapter over the fake wire) and the broker confirms it;
- every activation-lock refusal (Kill Switch ON, live configuration in the
  environment, a non-demo mode) is fail-closed at STARTUP: exit 1, zero
  broker calls, zero stores created, the secret-free summary printed;
- ``--dry-run`` performs zero side effects (no broker call, no DB write, no
  journal, no runner lock file) while still evaluating the real locks;
- a second resident runner on the same data directory is refused (exit 1);
- a restart never resubmits (the second bounded run selects nothing);
- the graceful-stop contract for the unbounded loop (exit 0);
- 'live' cannot be started from this CLI (refused fail-closed by the lock).
"""

import json
import threading
from datetime import datetime, timezone

from broker.oanda_live_adapter import ENV_ENABLE_LIVE_TRADING
import execution.demo_service_cli as demo_service_cli
from execution import execution_store
from execution.demo_lock import _compose_demo_connection
from execution.runner_service import EXECUTION_RUNNER_LOCK_FILENAME, runner_service_lock
from execution.demo_service_cli import EXIT_FAILURE, EXIT_OK, EXIT_USAGE_ERROR, main
from execution.submission_journal import OUTCOME_COMPLETED, SubmissionJournal

# Shared builders of the submission suite (tests run per-directory, so
# sibling helpers import flat -- the style test_demo_runner.py uses).
from test_demo_runner import (
    DemoTransport,
    demo_env,
    fill_response,
    kill_switch_db,
    write_safety_config,
)
from test_safety_gate import request

ORDERS_DB = "execution_orders.sqlite3"
DECISION_DB = "safety_gate_decisions.sqlite3"
JOURNAL_DB = "submission_journal.sqlite3"


def patch_demo_lock(monkeypatch, *, transport, env=None):
    """Route the CLI's composer call through the lock's offline seam.

    Every lock still runs for real against the given environment, safety
    config and kill-switch store; only the WIRE is in-process. The adapter
    keeps its real clock so account snapshots are fresh for the gate.
    """
    monkeypatch.setattr("os.environ", demo_env() if env is None else env)

    def compose(*, execution_mode, safety_config_path, kill_switch_db_path):
        return _compose_demo_connection(
            execution_mode=execution_mode,
            safety_config_path=safety_config_path,
            kill_switch_db_path=kill_switch_db_path,
            transport=transport,
            clock=None,
        )

    monkeypatch.setattr(demo_service_cli, "build_demo_connection", compose)


def write_market_snapshot(tmp_path):
    path = tmp_path / "market_snapshot.json"
    path.write_text(
        json.dumps(
            {
                "symbol": "USD_JPY", "reference_price": "150.00", "bid": "149.99",
                "ask": "150.01",
                "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                "market_open": True, "tradable": True,
            }
        ),
        encoding="utf-8",
    )
    return path


def service_args(tmp_path, *extra, execution_mode="demo", kill_switch_enabled=False):
    data_dir = tmp_path / "demo-data"
    data_dir.mkdir(exist_ok=True)
    return [
        "--execution-mode", execution_mode,
        "--safety-config", str(write_safety_config(tmp_path)),
        "--kill-switch-db", str(kill_switch_db(tmp_path, enabled=kill_switch_enabled)),
        "--market-snapshot", str(write_market_snapshot(tmp_path)),
        "--data-dir", str(data_dir),
        "--interval-seconds", "0.01",
        *extra,
    ], data_dir


def register_intent(data_dir, *, execution_request_id="exec-1", client_order_id="client-1"):
    now = datetime.now(timezone.utc)
    return execution_store.create_execution_order(
        request(execution_request_id=execution_request_id, requested_at_utc=now),
        client_order_id=client_order_id,
        created_at_utc=now,
        db_path=data_dir / ORDERS_DB,
    )


def run_json(capsys, args):
    code = main(args + ["--json"])
    out = capsys.readouterr().out
    return code, json.loads(out.strip().splitlines()[-1])


# --- the resident demo path, end to end -----------------------------------------


def test_a_pending_intent_is_submitted_end_to_end(tmp_path, capsys, monkeypatch):
    transport = DemoTransport(order_responses=[fill_response()])
    patch_demo_lock(monkeypatch, transport=transport)
    args, data_dir = service_args(tmp_path, "--max-cycles", "1")
    register_intent(data_dir)

    code, payload = run_json(capsys, args)
    assert code == EXIT_OK
    assert payload["all_cycles_ok"] is True
    assert payload["broker_sends"] == 1
    (cycle,) = payload["cycles"]
    assert cycle["execution_mode"] == "demo"
    assert cycle["status_counts"] == {"submitted": 1}
    # The broker really was contacted exactly once, over the practice wire.
    assert len(transport.submit_calls) == 1
    # The durable stores agree: phase submitted, journal completed.
    row = execution_store.get_execution_order("exec-1", db_path=data_dir / ORDERS_DB)
    assert row["submission_phase"] == "submitted"
    entry = SubmissionJournal(data_dir / JOURNAL_DB).get_entry("exec-1")
    assert entry.outcome == OUTCOME_COMPLETED


def test_the_startup_summary_is_printed_and_secret_free(tmp_path, capsys, monkeypatch):
    transport = DemoTransport(order_responses=[fill_response()])
    patch_demo_lock(monkeypatch, transport=transport)
    args, data_dir = service_args(tmp_path, "--max-cycles", "1")
    register_intent(data_dir)
    code = main(args)
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "demo" in out
    from test_demo_runner import ACCOUNT, TOKEN

    assert TOKEN not in out
    assert ACCOUNT not in out


def test_a_restart_never_resubmits(tmp_path, capsys, monkeypatch):
    transport = DemoTransport(order_responses=[fill_response()])
    patch_demo_lock(monkeypatch, transport=transport)
    args, data_dir = service_args(tmp_path, "--max-cycles", "1")
    register_intent(data_dir)
    assert main(args) == EXIT_OK
    capsys.readouterr()

    code, payload = run_json(capsys, args)
    assert code == EXIT_OK
    (cycle,) = payload["cycles"]
    # The submitted intent is no longer pending: nothing is selected, and
    # the boundary is never even asked.
    assert cycle["processed"] == 0
    assert len(transport.submit_calls) == 1


# --- fail-closed startup refusals -----------------------------------------------


def test_a_kill_switch_on_refuses_the_startup_with_zero_broker_calls(
    tmp_path, capsys, monkeypatch
):
    transport = DemoTransport()
    patch_demo_lock(monkeypatch, transport=transport)
    args, data_dir = service_args(tmp_path, "--max-cycles", "1", kill_switch_enabled=True)
    register_intent(data_dir)

    code = main(args)
    captured = capsys.readouterr()
    assert code == EXIT_FAILURE
    assert "kill_switch_enabled" in captured.err
    assert "REFUSED" in captured.err
    assert transport.calls == []
    # No runner was composed: the audit stores were never initialized.
    assert not (data_dir / DECISION_DB).exists()
    assert not (data_dir / JOURNAL_DB).exists()
    # The summary still printed (the operator's journal evidence).
    assert "demo" in captured.out


def test_live_configuration_in_the_environment_refuses_the_startup(
    tmp_path, capsys, monkeypatch
):
    transport = DemoTransport()
    patch_demo_lock(
        monkeypatch, transport=transport, env=demo_env(**{ENV_ENABLE_LIVE_TRADING: "true"})
    )
    args, _ = service_args(tmp_path, "--max-cycles", "1")
    code = main(args)
    assert code == EXIT_FAILURE
    assert "demo_live_configuration_mixed" in capsys.readouterr().err
    assert transport.calls == []


def test_live_mode_cannot_be_started_from_this_cli(tmp_path, capsys, monkeypatch):
    transport = DemoTransport()
    patch_demo_lock(monkeypatch, transport=transport)
    args, _ = service_args(tmp_path, "--max-cycles", "1", execution_mode="live")
    code = main(args)
    assert code == EXIT_FAILURE
    assert "execution_mode_not_demo" in capsys.readouterr().err
    assert transport.calls == []


def test_missing_demo_credentials_refuse_the_startup(tmp_path, capsys, monkeypatch):
    transport = DemoTransport()
    patch_demo_lock(monkeypatch, transport=transport, env={})
    args, _ = service_args(tmp_path, "--max-cycles", "1")
    code = main(args)
    assert code == EXIT_FAILURE
    assert "demo_credentials_missing" in capsys.readouterr().err
    assert transport.calls == []


# --- dry run --------------------------------------------------------------------


def test_dry_run_reaches_neither_the_broker_nor_a_store(tmp_path, capsys, monkeypatch):
    transport = DemoTransport()
    patch_demo_lock(monkeypatch, transport=transport)
    args, data_dir = service_args(tmp_path, "--max-cycles", "1", "--dry-run")
    register_intent(data_dir)
    before = sorted(p.name for p in data_dir.iterdir())

    code, payload = run_json(capsys, args)
    assert code == EXIT_OK
    (cycle,) = payload["cycles"]
    assert cycle["status_counts"] == {"planned": 1}
    assert cycle["dry_run"] is True
    assert transport.calls == []
    # Nothing new in the data directory: no decision DB, no journal, no
    # runner lock file. The intent is untouched.
    assert sorted(p.name for p in data_dir.iterdir()) == before
    row = execution_store.get_execution_order("exec-1", db_path=data_dir / ORDERS_DB)
    assert row["submission_phase"] == "created"


# --- exclusivity ----------------------------------------------------------------


def test_a_second_resident_runner_is_refused_fail_closed(tmp_path, capsys, monkeypatch):
    transport = DemoTransport(order_responses=[fill_response()])
    patch_demo_lock(monkeypatch, transport=transport)
    args, data_dir = service_args(tmp_path, "--max-cycles", "1")
    register_intent(data_dir)

    with runner_service_lock(data_dir / EXECUTION_RUNNER_LOCK_FILENAME):
        code = main(args)
        err = capsys.readouterr().err
    assert code == EXIT_FAILURE
    assert "another resident runner" in err
    # Refused BEFORE any cycle: the intent stayed pending, nothing was sent.
    assert transport.submit_calls == []
    row = execution_store.get_execution_order("exec-1", db_path=data_dir / ORDERS_DB)
    assert row["submission_phase"] == "created"
    # After the holder releases, the service runs normally.
    assert main(args) == EXIT_OK
    assert len(transport.submit_calls) == 1


# --- graceful stop --------------------------------------------------------------


def test_an_unbounded_loop_stops_cleanly_on_a_stop_request(tmp_path, capsys, monkeypatch):
    """The systemd stop contract at CLI level (the SIGTERM handler itself is
    pinned in test_runner_service.py; signals cannot be raised from the
    worker timer thread here)."""
    transport = DemoTransport(order_responses=[fill_response()])
    patch_demo_lock(monkeypatch, transport=transport)
    real_install = demo_service_cli.install_stop_signal_handlers

    def capture_and_install(runner, **kwargs):
        real_install(runner, **kwargs)
        threading.Timer(0.05, runner.request_stop).start()

    monkeypatch.setattr(demo_service_cli, "install_stop_signal_handlers", capture_and_install)
    args, data_dir = service_args(tmp_path)  # no --max-cycles: resident
    register_intent(data_dir)
    code, payload = run_json(capsys, args)
    assert code == EXIT_OK
    assert payload["stop_reason"] == "stop_requested"


# --- argument contract ----------------------------------------------------------


def test_required_arguments_are_required(capsys):
    assert main([]) == EXIT_USAGE_ERROR


def test_max_cycles_must_be_positive(tmp_path, capsys, monkeypatch):
    patch_demo_lock(monkeypatch, transport=DemoTransport())
    args, _ = service_args(tmp_path, "--max-cycles", "0")
    assert main(args) == EXIT_USAGE_ERROR


def test_a_non_positive_interval_is_a_usage_error(tmp_path, capsys, monkeypatch):
    patch_demo_lock(monkeypatch, transport=DemoTransport())
    args, _ = service_args(tmp_path, "--max-cycles", "1")
    index = args.index("--interval-seconds") + 1
    args[index] = "-1"
    assert main(args) == EXIT_USAGE_ERROR
