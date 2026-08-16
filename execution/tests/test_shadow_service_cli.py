"""Issue #134: the Shadow Mode resident service CLI
(``python -m execution.shadow_service_cli``).

What is pinned:

- a bounded run drives the PRODUCTION shadow cycle over stored signals and
  records into the shadow store -- with zero broker anywhere (the composed
  runner has no broker-shaped part at all);
- re-running over the same signals yields explicit DUPLICATE outcomes, not
  second records (restart safety is the store's claim, not runner memory);
- ``--dry-run`` creates NOTHING: no DB, no record, no runner lock file;
- a second resident runner on the same data directory is refused
  fail-closed (exit 1), and a held lock is released afterwards;
- the graceful-stop contract: a stop request ends an UNBOUNDED loop with
  exit 0;
- the exit-code contract for bounded verification runs, and the
  machine-readable ``--json`` summary a bounded run leaves behind;
- there is no ``--mode`` argument to misconfigure: this CLI composes the
  shadow runner and nothing else.
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone

import execution.shadow_service_cli as shadow_service_cli
from execution import shadow_store
from execution.runner_service import SHADOW_RUNNER_LOCK_FILENAME, runner_service_lock
from execution.shadow_service_cli import EXIT_FAILURE, EXIT_OK, EXIT_USAGE_ERROR, main


def seed_signal_db(tmp_path, signal_ids=("sig-1",)):
    db = tmp_path / "paper_signals.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE paper_signals (signal_id TEXT PRIMARY KEY, created_at_utc TEXT NOT NULL, "
        "symbol TEXT NOT NULL, timeframe TEXT NOT NULL, side TEXT NOT NULL, "
        "strategy_profile TEXT NOT NULL, status TEXT NOT NULL)"
    )
    now = datetime.now(timezone.utc).isoformat()
    for signal_id in signal_ids:
        connection.execute(
            "INSERT INTO paper_signals VALUES (?, ?, 'USD_JPY', 'H1', 'long', "
            "'strategy-1', 'paper_only')",
            (signal_id, now),
        )
    connection.commit()
    connection.close()
    return db


def write_inputs(tmp_path):
    """Production configuration + FRESH snapshots (the service uses the real
    clock, so snapshot timestamps must be current for the gate to see them
    as fresh; a stale snapshot would still be a recorded rejection)."""
    now = datetime.now(timezone.utc).isoformat()
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "config_id": "svc-config-1",
                "policy": {
                    "policy_id": "svc-policy-1",
                    "allowed_symbols": ["USD_JPY"],
                    "allowed_sides": ["buy", "sell"],
                    "max_order_quantity": "10",
                    "max_positions_per_symbol": 2,
                    "max_positions_account": 5,
                    "max_reference_age_seconds": 300,
                    "max_spread_by_symbol": {"USD_JPY": "0.05"},
                    "allowed_shadow_targets": ["shadow"],
                    "require_stop_loss": False,
                },
                "order_quantities": {"USD_JPY": "1"},
                "kill_switch": False,
                "connection_target": "shadow",
            }
        ),
        encoding="utf-8",
    )
    market = tmp_path / "market.json"
    market.write_text(
        json.dumps(
            {
                "symbol": "USD_JPY", "reference_price": "150.00", "bid": "149.99",
                "ask": "150.01", "observed_at_utc": now, "market_open": True,
                "tradable": True,
            }
        ),
        encoding="utf-8",
    )
    account = tmp_path / "account.json"
    account.write_text(
        json.dumps(
            {
                "account_id": "svc-account", "currency": "JPY", "balance": "1000",
                "equity": "1000", "margin_available": "1000",
                "retrieved_at_utc": now, "positions": [],
            }
        ),
        encoding="utf-8",
    )
    return config, market, account


def service_args(tmp_path, *extra, signal_ids=("sig-1",)):
    signal_db = seed_signal_db(tmp_path, signal_ids)
    config, market, account = write_inputs(tmp_path)
    data_dir = tmp_path / "shadow-data"
    return [
        "--signal-db", str(signal_db),
        "--execution-config", str(config),
        "--market-snapshot", str(market),
        "--account-snapshot", str(account),
        "--data-dir", str(data_dir),
        "--owner-id", "svc-test-owner",
        "--interval-seconds", "0.01",
        *extra,
    ], data_dir


def run_json(capsys, args):
    code = main(args + ["--json"])
    out = capsys.readouterr().out
    # The WHOLE of stdout must be one machine-readable JSON document (PR
    # #138 Codex review P2 on the demo CLI; same artifact contract here).
    return code, json.loads(out)


# --- the resident production path -----------------------------------------------


def test_a_bounded_run_records_shadow_results_with_zero_broker(tmp_path, capsys):
    args, data_dir = service_args(tmp_path, "--max-cycles", "1")
    code, payload = run_json(capsys, args)
    assert code == EXIT_OK
    assert payload["all_cycles_ok"] is True
    assert payload["broker_sends"] == 0
    (cycle,) = payload["cycles"]
    assert cycle["execution_mode"] == "shadow"
    assert cycle["status_counts"] == {"recorded": 1}
    assert {o["broker_contact"] for o in cycle["outcomes"]} == {"not_sent"}
    results = shadow_store.list_shadow_results(db_path=data_dir / "shadow_results.sqlite3")
    assert len(results) == 1
    assert results[0].request.signal_id == "sig-1"


def test_a_second_bounded_run_reports_duplicates_not_second_records(tmp_path, capsys):
    args, data_dir = service_args(tmp_path, "--max-cycles", "1")
    code, _ = run_json(capsys, args)
    assert code == EXIT_OK
    code, payload = run_json(capsys, args)
    assert code == EXIT_OK
    (cycle,) = payload["cycles"]
    assert cycle["status_counts"] == {"duplicate": 1}
    results = shadow_store.list_shadow_results(db_path=data_dir / "shadow_results.sqlite3")
    assert len(results) == 1


def test_multiple_signals_are_processed_in_one_cycle(tmp_path, capsys):
    args, data_dir = service_args(
        tmp_path, "--max-cycles", "1", signal_ids=("sig-1", "sig-2", "sig-3")
    )
    code, payload = run_json(capsys, args)
    assert code == EXIT_OK
    (cycle,) = payload["cycles"]
    assert cycle["status_counts"] == {"recorded": 3}
    assert len(shadow_store.list_shadow_results(db_path=data_dir / "shadow_results.sqlite3")) == 3


def test_a_missing_signal_db_is_a_loud_cycle_failure_not_an_empty_cycle(tmp_path, capsys):
    args, _ = service_args(tmp_path, "--max-cycles", "1")
    index = args.index("--signal-db") + 1
    args[index] = str(tmp_path / "no-such.sqlite3")
    code, payload = run_json(capsys, args)
    assert code == EXIT_FAILURE
    (cycle,) = payload["cycles"]
    assert cycle["selection_error"] is not None
    assert cycle["processed"] == 0


# --- dry run --------------------------------------------------------------------


def test_dry_run_creates_nothing_at_all(tmp_path, capsys):
    args, data_dir = service_args(tmp_path, "--max-cycles", "1", "--dry-run")
    code, payload = run_json(capsys, args)
    assert code == EXIT_OK
    (cycle,) = payload["cycles"]
    assert cycle["status_counts"] == {"planned": 1}
    # No shadow DB, no decision DB, no runner lock file -- the whole data
    # directory was never created.
    assert not data_dir.exists()


# --- exclusivity ----------------------------------------------------------------


def test_a_second_resident_runner_is_refused_fail_closed(tmp_path, capsys):
    args, data_dir = service_args(tmp_path, "--max-cycles", "1")
    with runner_service_lock(data_dir / SHADOW_RUNNER_LOCK_FILENAME):
        code = main(args)
        err = capsys.readouterr().err
    assert code == EXIT_FAILURE
    assert "refused" in err and "another resident runner" in err
    # Nothing ran and nothing was recorded.
    assert not (data_dir / "shadow_results.sqlite3").exists()
    # After the holder releases, the service starts normally.
    assert main(args) == EXIT_OK


# --- graceful stop --------------------------------------------------------------


def test_an_unbounded_loop_stops_cleanly_on_a_stop_request(tmp_path, capsys, monkeypatch):
    """The systemd stop contract at CLI level: an UNBOUNDED loop, a stop
    request from outside the loop (what the SIGTERM handler performs), exit
    0. The handler installation itself is pinned in test_runner_service.py
    (signals cannot be exercised from a worker thread here)."""
    captured = {}
    real_install = shadow_service_cli.install_stop_signal_handlers

    def capture_and_install(runner, **kwargs):
        captured["runner"] = runner
        real_install(runner, **kwargs)
        threading.Timer(0.05, runner.request_stop).start()

    monkeypatch.setattr(shadow_service_cli, "install_stop_signal_handlers", capture_and_install)
    args, _ = service_args(tmp_path)  # no --max-cycles: resident
    code, payload = run_json(capsys, args)
    assert code == EXIT_OK
    assert payload["stop_reason"] == "stop_requested"
    assert captured["runner"].stop_requested is True


# --- argument contract ----------------------------------------------------------


def test_every_production_input_is_required(tmp_path, capsys):
    code = main(["--signal-db", str(tmp_path / "db.sqlite3")])
    assert code == EXIT_USAGE_ERROR


def test_an_unreadable_execution_config_is_a_usage_error(tmp_path, capsys):
    args, _ = service_args(tmp_path, "--max-cycles", "1")
    index = args.index("--execution-config") + 1
    args[index] = str(tmp_path / "missing-config.json")
    code = main(args)
    assert code == EXIT_USAGE_ERROR
    assert "configuration error" in capsys.readouterr().err


def test_a_non_positive_interval_is_a_usage_error(tmp_path, capsys):
    args, _ = service_args(tmp_path, "--max-cycles", "1")
    index = args.index("--interval-seconds") + 1
    args[index] = "0"
    code = main(args)
    assert code == EXIT_USAGE_ERROR


def test_max_cycles_must_be_positive(tmp_path, capsys):
    args, _ = service_args(tmp_path, "--max-cycles", "0")
    assert main(args) == EXIT_USAGE_ERROR


def test_there_is_no_mode_argument_to_misconfigure(tmp_path, capsys):
    """The CLI can only compose the shadow runner: a submitting mode cannot
    be requested, not even as an unknown argument."""
    args, _ = service_args(tmp_path, "--mode", "demo", "--max-cycles", "1")
    assert main(args) == EXIT_USAGE_ERROR
