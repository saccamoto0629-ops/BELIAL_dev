"""Issue #78: the shadow report CLI -- listing (newest first / --limit),
detail lookup (request / signal / safety-decision id), decision filtering,
claims and orphan-decision inspection, provenance display, JSON purity,
missing-DB / unknown-id / empty-store behaviour, the true read-only
guarantee, cp932 survival and stream restoration."""

import io
import json
import sqlite3
import sys
from datetime import timedelta

import pytest

from execution import shadow_store
from execution.shadow_report import EXIT_FAILURE, EXIT_OK, EXIT_USAGE_ERROR, main
from shadow_helpers import (
    NOW,
    make_account,
    make_market,
    make_provenance,
    make_request,
    make_service,
)


def seed(tmp_path):
    """Two completed rows (one allowed, one rejected) with real decisions."""
    service = make_service(tmp_path, owner_id="owner-1")
    service.run_cycle(
        make_request(),
        market=make_market(),
        account=make_account(),
        client_order_id="client-1",
        provenance=make_provenance(),
        timeframe="H1",
    )
    service.run_cycle(
        make_request(execution_request_id="exec-2", signal_id="signal-2"),
        market=make_market(market_open=False),
        account=make_account(),
        client_order_id="client-2",
        provenance=make_provenance(),
    )
    return service.shadow_db_path, service.decision_db_path


def test_list_shows_every_result_newest_first_with_provenance(tmp_path, capsys):
    db, decisions = seed(tmp_path)
    assert main(["--shadow-db", str(db), "--decision-db", str(decisions)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "ALLOWED" in out and "REJECTED" in out and "total 2" in out
    assert out.index("exec-2") < out.index("exec-1")  # newest first
    assert "origin=scenario_fixture" in out


def test_limit_bounds_the_listing(tmp_path, capsys):
    db, decisions = seed(tmp_path)
    assert main(["--shadow-db", str(db), "--decision-db", str(decisions),
                 "--limit", "1"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "exec-2" in out and "exec-1" not in out
    assert main(["--shadow-db", str(db), "--limit", "0"]) == EXIT_USAGE_ERROR
    assert main(["--shadow-db", str(db), "--limit", "1001"]) == EXIT_USAGE_ERROR


def test_detail_by_execution_request_id_shows_provenance_and_snapshot(tmp_path, capsys):
    db, decisions = seed(tmp_path)
    assert main(["--shadow-db", str(db), "--decision-db", str(decisions),
                 "--execution-request-id", "exec-2"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "REJECTED" in out and "market_closed" in out
    assert "intended order (NOT submitted)" in out
    assert "provenance: origin=scenario_fixture" in out
    assert "account snapshot: account_id=acct-1" in out
    assert "claim: owner=owner-1" in out


def test_detail_by_source_signal_and_decision_id(tmp_path, capsys):
    db, decisions = seed(tmp_path)
    assert main(["--shadow-db", str(db), "--decision-db", str(decisions),
                 "--source-signal-id", "signal-1"]) == EXIT_OK
    assert "exec-1" in capsys.readouterr().out
    assert main(["--shadow-db", str(db), "--decision-db", str(decisions),
                 "--safety-decision-id", "2"]) == EXIT_OK
    assert "exec-2" in capsys.readouterr().out
    assert main(["--shadow-db", str(db), "--safety-decision-id", "99"]) == EXIT_FAILURE
    assert "no shadow result references" in capsys.readouterr().err


def test_rejected_filter(tmp_path, capsys):
    db, decisions = seed(tmp_path)
    assert main(["--shadow-db", str(db), "--decision-db", str(decisions),
                 "--decision", "rejected"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "exec-2" in out and "exec-1" not in out


def test_claims_listing_shows_in_flight_cycles(tmp_path, capsys, monkeypatch):
    service = make_service(tmp_path, owner_id="owner-1")

    def explode(*args, **kwargs):
        raise shadow_store.ShadowStoreError("injected completion failure")

    monkeypatch.setattr(shadow_store, "complete_shadow_cycle", explode)
    with pytest.raises(shadow_store.ShadowStoreError):
        service.run_cycle(
            make_request(), market=make_market(), account=make_account(),
            client_order_id="client-1", provenance=make_provenance(),
        )
    monkeypatch.undo()
    assert main(["--shadow-db", str(service.shadow_db_path), "--claims"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "CLAIMED" in out and "owner=owner-1" in out and "lease_expires=" in out


def test_orphan_decisions_are_searchable(tmp_path, capsys, monkeypatch):
    # A completion failure leaves the claim ACTIVE (never re-acquired, PR #100
    # re-review Major 1), so the orphaned decision is produced by the
    # lease-expiry takeover path: crash, wait out the lease, recover.
    service = make_service(tmp_path, owner_id="owner-1", claim_lease_seconds=60)

    def explode(*args, **kwargs):
        raise shadow_store.ShadowStoreError("injected completion failure")

    monkeypatch.setattr(shadow_store, "complete_shadow_cycle", explode)
    with pytest.raises(shadow_store.ShadowStoreError):
        service.run_cycle(
            make_request(), market=make_market(), account=make_account(),
            client_order_id="client-1", provenance=make_provenance(),
        )
    monkeypatch.undo()
    LATER = NOW + timedelta(seconds=61)
    recovery = make_service(
        tmp_path, owner_id="owner-1", claim_lease_seconds=60, clock=lambda: LATER
    )
    retry = recovery.run_cycle(
        make_request(),
        market=make_market(observed_at_utc=LATER),
        account=make_account(retrieved_at_utc=LATER),
        client_order_id="client-1",
        provenance=make_provenance(),
    )
    assert retry.result.safety_decision_id == 2
    assert (
        main(
            ["--shadow-db", str(service.shadow_db_path), "--decision-db",
             str(service.decision_db_path), "--orphan-decisions"]
        )
        == EXIT_OK
    )
    out = capsys.readouterr().out
    assert "decision_id=1" in out and "referenced_decision_id=2" in out and "total 1" in out
    assert (
        main(
            ["--shadow-db", str(service.shadow_db_path), "--decision-db",
             str(service.decision_db_path), "--orphan-decisions", "--json"]
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1 and payload["orphan_decisions"][0]["decision_id"] == 1


# --- restore-time Safety-Decision cross-check (PR #100 re-review Major 4) -------


def tamper_decision(decision_db, sql, params=()):
    connection = sqlite3.connect(decision_db)
    connection.execute(sql, params)
    connection.commit()
    connection.close()


@pytest.mark.parametrize("extra_args", [[], ["--json"]], ids=["text", "json"])
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE safety_gate_decisions SET account_id = 'tampered-acct' WHERE decision_id = 1",
        "UPDATE safety_gate_decisions SET position_count = 42 WHERE decision_id = 1",
        "DELETE FROM safety_gate_decisions WHERE decision_id = 1",
    ],
    ids=["account_id", "position_count", "missing_row"],
)
def test_tampered_safety_decision_fails_the_report_explicitly(
    tmp_path, capsys, sql, extra_args
):
    db, decisions = seed(tmp_path)
    tamper_decision(decisions, sql)
    assert (
        main(["--shadow-db", str(db), "--decision-db", str(decisions), *extra_args])
        == EXIT_FAILURE
    )
    captured = capsys.readouterr()
    assert "shadow report failed" in captured.err
    assert "exec-1" in captured.err  # names the corrupted record
    # the inconsistent record is never displayed -- not even in JSON output
    assert captured.out == ""


# --- unusable decision stores are normalized (PR #100 4th-review Major 1) ------


def break_empty(path):
    path.write_bytes(b"")


def break_non_sqlite(path):
    path.write_bytes(b"this file is not a SQLite database\n" * 20)


def break_table_missing(path):
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE safety_gate_decisions")
    connection.commit()
    connection.close()


def break_column_missing(path):
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE safety_gate_decisions")
    connection.execute(
        "CREATE TABLE safety_gate_decisions ("
        "decision_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "execution_request_id TEXT NOT NULL)"
    )
    connection.commit()
    connection.close()


def break_user_version(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()


def break_malformed_row(path):
    tamper_decision(
        path,
        "UPDATE safety_gate_decisions SET checked_at_utc = 'not-a-timestamp+00:00' "
        "WHERE decision_id = 1",
    )


def break_corrupted_json(path):
    tamper_decision(
        path,
        "UPDATE safety_gate_decisions SET rejections_json = '{not json' WHERE decision_id = 2",
    )


def break_corrupted_image(path):
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])


@pytest.mark.parametrize("extra_args", [[], ["--json"]], ids=["text", "json"])
@pytest.mark.parametrize(
    "corrupt",
    [
        break_empty,
        break_non_sqlite,
        break_table_missing,
        break_column_missing,
        break_user_version,
        break_malformed_row,
        break_corrupted_json,
        break_corrupted_image,
    ],
    ids=[
        "empty", "non_sqlite", "table_missing", "column_missing",
        "wrong_user_version", "malformed_row", "corrupted_json", "corrupted_image",
    ],
)
def test_unusable_decision_store_fails_stably_without_a_traceback(
    tmp_path, capsys, corrupt, extra_args
):
    db, decisions = seed(tmp_path)
    corrupt(decisions)
    before = sorted((entry.name, entry.stat().st_size) for entry in tmp_path.iterdir())
    shadow_bytes = db.read_bytes()
    decision_bytes = decisions.read_bytes()

    assert (
        main(["--shadow-db", str(db), "--decision-db", str(decisions), *extra_args])
        == EXIT_FAILURE
    )
    captured = capsys.readouterr()
    assert captured.err.startswith("shadow report failed: ")
    assert "Traceback" not in captured.err
    # nothing unverifiable reaches stdout, in text or JSON output
    assert captured.out == ""
    # read-only contract holds even on the failing path
    assert sorted((entry.name, entry.stat().st_size) for entry in tmp_path.iterdir()) == before
    assert db.read_bytes() == shadow_bytes
    assert decisions.read_bytes() == decision_bytes


@pytest.mark.parametrize("extra_args", [[], ["--json"]], ids=["text", "json"])
def test_unusable_decision_store_also_fails_the_orphan_search(tmp_path, capsys, extra_args):
    db, decisions = seed(tmp_path)
    break_non_sqlite(decisions)
    assert (
        main(["--shadow-db", str(db), "--decision-db", str(decisions),
              "--orphan-decisions", *extra_args])
        == EXIT_FAILURE
    )
    captured = capsys.readouterr()
    assert captured.err.startswith("shadow report failed: ")
    assert "Traceback" not in captured.err and captured.out == ""


# --- NULL safety_decision_id invariant (PR #100 4th-review Major 2) -------------


def seed_persist_failure(tmp_path, monkeypatch):
    """One completed record whose decision could NOT be persisted: the only
    legitimate NULL ``safety_decision_id``."""
    from execution.safety_store import SQLiteSafetyDecisionStore

    service = make_service(tmp_path, owner_id="owner-1")

    def explode(self, decision):
        raise RuntimeError("injected decision store failure")

    monkeypatch.setattr(SQLiteSafetyDecisionStore, "record", explode)
    service.run_cycle(
        make_request(), market=make_market(), account=make_account(),
        client_order_id="client-1", provenance=make_provenance(),
    )
    monkeypatch.undo()
    return service.shadow_db_path, service.decision_db_path


def test_legitimate_persist_failure_record_reports_normally(tmp_path, capsys, monkeypatch):
    db, decisions = seed_persist_failure(tmp_path, monkeypatch)
    assert main(["--shadow-db", str(db), "--decision-db", str(decisions)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "decision_persist_failed" in out and "total 1" in out


@pytest.mark.parametrize("extra_args", [[], ["--json"]], ids=["text", "json"])
def test_null_decision_reference_without_a_persist_failure_is_refused(
    tmp_path, capsys, extra_args
):
    db, decisions = seed(tmp_path)
    # a normal record loses its decision reference: NULL is NOT a free pass
    tamper_decision(
        db, f"UPDATE {shadow_store.RESULTS_TABLE} SET safety_decision_id = NULL "
            "WHERE execution_request_id = 'exec-1'"
    )
    assert (
        main(["--shadow-db", str(db), "--decision-db", str(decisions), *extra_args])
        == EXIT_FAILURE
    )
    captured = capsys.readouterr()
    assert captured.err.startswith("shadow report failed: ")
    assert "Traceback" not in captured.err and captured.out == ""


@pytest.mark.parametrize("extra_args", [[], ["--json"]], ids=["text", "json"])
def test_persist_failure_record_with_a_reference_is_refused(
    tmp_path, capsys, monkeypatch, extra_args
):
    db, decisions = seed_persist_failure(tmp_path, monkeypatch)
    # the reverse direction: a decision_persist_failed record must reference
    # no decision row at all
    tamper_decision(
        db, f"UPDATE {shadow_store.RESULTS_TABLE} SET safety_decision_id = 1 "
            "WHERE execution_request_id = 'exec-1'"
    )
    assert (
        main(["--shadow-db", str(db), "--decision-db", str(decisions), *extra_args])
        == EXIT_FAILURE
    )
    captured = capsys.readouterr()
    assert captured.err.startswith("shadow report failed: ")
    assert "Traceback" not in captured.err and captured.out == ""


def test_report_boundary_refuses_an_in_memory_invariant_violation(tmp_path):
    """The report verifies the invariant itself, not only via restore: a
    record doctored in memory (bypassing the frozen model's validation) is
    still refused."""
    from execution import shadow_report

    db, decisions = seed(tmp_path)
    results = shadow_store.list_shadow_results(db_path=db)
    doctored = results[0]
    object.__setattr__(doctored, "safety_decision_id", None)
    with pytest.raises(shadow_report.ShadowReportIntegrityError, match="decision_persist_failed"):
        shadow_report._verify_decision_consistency((doctored,), decisions)


def test_detail_view_also_cross_checks_the_referenced_decision(tmp_path, capsys):
    db, decisions = seed(tmp_path)
    tamper_decision(
        decisions,
        "UPDATE safety_gate_decisions SET account_id = 'tampered-acct' WHERE decision_id = 2",
    )
    assert (
        main(["--shadow-db", str(db), "--decision-db", str(decisions),
              "--execution-request-id", "exec-2"])
        == EXIT_FAILURE
    )
    captured = capsys.readouterr()
    assert "shadow report failed" in captured.err and captured.out == ""


@pytest.mark.parametrize("extra_args", [[], ["--json"]], ids=["text", "json"])
def test_missing_decision_db_fails_closed_for_referencing_records(
    tmp_path, capsys, extra_args
):
    # PR #100 3rd-review Major 2: records referencing safety decision ids must
    # not render when the decision store file itself is absent.
    db, _ = seed(tmp_path)
    assert (
        main(["--shadow-db", str(db), "--decision-db",
              str(tmp_path / "absent.sqlite3"), *extra_args])
        == EXIT_FAILURE
    )
    captured = capsys.readouterr()
    assert "shadow report failed" in captured.err and "does not exist" in captured.err
    assert captured.out == ""


def test_missing_decision_db_is_fine_when_no_record_references_a_decision(
    tmp_path, capsys, monkeypatch
):
    # A record whose decision persistence failed carries safety_decision_id
    # NULL -- with no references at all, a standalone shadow DB copy stays
    # inspectable without the decision store.
    from execution.safety_store import SQLiteSafetyDecisionStore

    service = make_service(tmp_path, owner_id="owner-1")

    def explode(self, decision):
        raise RuntimeError("injected decision store failure")

    monkeypatch.setattr(SQLiteSafetyDecisionStore, "record", explode)
    service.run_cycle(
        make_request(), market=make_market(), account=make_account(),
        client_order_id="client-1", provenance=make_provenance(),
    )
    monkeypatch.undo()
    assert main(["--shadow-db", str(service.shadow_db_path), "--decision-db",
                 str(tmp_path / "absent.sqlite3")]) == EXIT_OK
    assert "total 1" in capsys.readouterr().out


@pytest.mark.parametrize("extra_args", [[], ["--json"]], ids=["text", "json"])
def test_infinity_tampered_policy_fails_the_report_stably(tmp_path, capsys, extra_args):
    # PR #100 3rd-review Major 3: an Infinity injected into policy_json must
    # surface as the stable "shadow report failed" error (exit 1), never as
    # an OverflowError traceback.
    db, decisions = seed(tmp_path)
    connection = sqlite3.connect(db)
    policy_json = connection.execute(
        f"SELECT policy_json FROM {shadow_store.RESULTS_TABLE} "
        "WHERE execution_request_id = 'exec-1'"
    ).fetchone()[0]
    tampered = json.loads(policy_json)
    tampered["max_reference_age_seconds"] = float("inf")
    connection.execute(
        f"UPDATE {shadow_store.RESULTS_TABLE} SET policy_json = ? "
        "WHERE execution_request_id = 'exec-1'",
        (json.dumps(tampered, sort_keys=True, separators=(",", ":")),),
    )
    connection.commit()
    connection.close()
    assert (
        main(["--shadow-db", str(db), "--decision-db", str(decisions), *extra_args])
        == EXIT_FAILURE
    )
    captured = capsys.readouterr()
    assert "shadow report failed" in captured.err and "finite" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_no_orphans_for_clean_records(tmp_path, capsys):
    db, decisions = seed(tmp_path)
    assert main(["--shadow-db", str(db), "--decision-db", str(decisions),
                 "--orphan-decisions"]) == EXIT_OK
    assert "no orphan decisions" in capsys.readouterr().out


def test_unknown_id_exits_nonzero(tmp_path, capsys):
    db, _ = seed(tmp_path)
    assert main(["--shadow-db", str(db), "--execution-request-id", "nope"]) == EXIT_FAILURE
    assert "no shadow result" in capsys.readouterr().err


def test_missing_db_is_reported_and_never_created(tmp_path, capsys):
    db = tmp_path / "missing.sqlite3"
    assert main(["--shadow-db", str(db)]) == EXIT_FAILURE
    assert "does not exist" in capsys.readouterr().err
    assert not db.exists()


def test_empty_store_is_a_valid_empty_report(tmp_path, capsys):
    db = tmp_path / "shadow.sqlite3"
    shadow_store.init_db(db)
    assert main(["--shadow-db", str(db)]) == EXIT_OK
    assert "no shadow results" in capsys.readouterr().out


def test_json_report_is_machine_readable_with_provenance(tmp_path, capsys):
    db, decisions = seed(tmp_path)
    assert main(["--shadow-db", str(db), "--decision-db", str(decisions),
                 "--json", "--decision", "rejected"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    entry = payload["results"][0]
    assert entry["execution_request_id"] == "exec-2"
    assert entry["allowed"] is False and "market_closed" in entry["rejection_codes"]
    assert entry["provenance"]["input_origin"] == "scenario_fixture"
    assert entry["account_snapshot"]["account_id"] == "acct-1"
    assert entry["safety_decision_id"] == 2


def test_report_is_truly_read_only(tmp_path):
    db, decisions = seed(tmp_path)
    before = db.read_bytes()
    stat_before = (db.stat().st_mtime_ns, db.stat().st_size)
    files_before = sorted(entry.name for entry in tmp_path.iterdir())
    assert main(["--shadow-db", str(db), "--decision-db", str(decisions)]) == EXIT_OK
    assert main(["--shadow-db", str(db), "--decision-db", str(decisions),
                 "--json"]) == EXIT_OK
    assert main(["--shadow-db", str(db), "--decision-db", str(decisions),
                 "--orphan-decisions"]) == EXIT_OK
    assert db.read_bytes() == before
    assert (db.stat().st_mtime_ns, db.stat().st_size) == stat_before
    assert sorted(entry.name for entry in tmp_path.iterdir()) == files_before


def test_corrupted_db_is_an_explicit_failure(tmp_path, capsys):
    db = tmp_path / "shadow.sqlite3"
    shadow_store.init_db(db)
    import sqlite3

    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE stray (id INTEGER)")
    connection.commit()
    connection.close()
    assert main(["--shadow-db", str(db)]) == EXIT_FAILURE
    assert "shadow report failed" in capsys.readouterr().err


# --- console encoding / stream restoration --------------------------------------


def _cp932_stdout(monkeypatch):
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp932", errors="strict", newline="")
    monkeypatch.setattr(sys, "stdout", stream)
    return buffer, stream


def test_report_survives_a_cp932_console(tmp_path, monkeypatch):
    db, decisions = seed(tmp_path)
    buffer, stream = _cp932_stdout(monkeypatch)
    assert main(["--shadow-db", str(db), "--decision-db", str(decisions)]) == EXIT_OK
    assert sys.stdout.errors == "strict"
    stream.flush()
    assert "total 2" in buffer.getvalue().decode("cp932")


def test_json_survives_a_cp932_console(tmp_path, monkeypatch):
    db, decisions = seed(tmp_path)
    buffer, stream = _cp932_stdout(monkeypatch)
    assert main(["--shadow-db", str(db), "--decision-db", str(decisions),
                 "--json"]) == EXIT_OK
    stream.flush()
    payload = json.loads(buffer.getvalue().decode("ascii"))
    assert payload["count"] == 2


def _stream_settings():
    return (
        getattr(sys.stdout, "errors", None),
        getattr(sys.stderr, "errors", None),
    )


def test_streams_are_restored_on_every_exit_path(tmp_path):
    db, decisions = seed(tmp_path)
    before = _stream_settings()
    assert main(["--shadow-db", str(db), "--decision-db", str(decisions)]) == EXIT_OK
    assert _stream_settings() == before
    assert main(["--shadow-db", str(tmp_path / 'missing.sqlite3')]) == EXIT_FAILURE
    assert _stream_settings() == before
    with pytest.raises(SystemExit):
        main(["--no-such-flag"])
    assert _stream_settings() == before
