"""Issue #81: persistent, fail-closed Execution Kill Switch.

The durable kill switch store + provider are exercised through the EXISTING
Safety Gate / LiveOrderSubmissionService boundary (no new gate, no new
submission path): every enabled / unverifiable state must leave FakeBroker
with ZERO submissions, reconciliation and read-only queries must keep
working, and the state (plus its full audit history) must survive process
restarts (simulated by rebuilding every object from the same DB path).
FakeBroker performs no external I/O and all persistence lives under
tmp_path, so none of these tests can reach a network, Discord, S3 or a
real DB.
"""

import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from broker.broker_models import AccountState, OrderSide, OrderState, OrderType
from broker.fake_broker import FakeBroker
from execution import execution_store
from execution.execution_models import ExecutionRequest
from execution.kill_switch_cli import main as kill_switch_cli_main
from execution.kill_switch_store import (
    KillSwitchIntegrityError,
    KillSwitchSchemaError,
    KillSwitchState,
    KillSwitchStoreError,
    KillSwitchTransition,
    PersistentKillSwitchStateProvider,
    SQLiteKillSwitchStore,
    kill_switch_db_path,
)
from execution.live_submission import LiveOrderSubmissionService
from execution.reconciliation_runner import main as reconciliation_main
from execution.reissue_service import ReissuePolicy
from execution.safety_gate import (
    BrokerConnection,
    ExecutionMode,
    MarketSafetySnapshot,
    SafetyGate,
    SafetyPolicy,
    SafetyRejectionCode,
)
from execution.safety_store import SQLiteSafetyDecisionStore
from execution.submission_journal import SubmissionJournal

NOW = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)


# --- builders (mirroring test_safety_gate.py, kept local on purpose) ---------


def request(request_id="exec-ks-1", signal_id="signal-ks-1"):
    return ExecutionRequest(
        execution_request_id=request_id,
        signal_id=signal_id,
        strategy_id="strategy-1",
        symbol="USD_JPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        requested_at_utc=NOW,
        requested_price=None,
        stop_loss_price=Decimal("149.5"),
        take_profit_price=Decimal("151.5"),
    )


def market():
    return MarketSafetySnapshot(
        symbol="USD_JPY",
        reference_price=Decimal("150"),
        bid=Decimal("149.99"),
        ask=Decimal("150.01"),
        observed_at_utc=NOW,
        market_open=True,
        tradable=True,
    )


def policy():
    return SafetyPolicy(
        policy_id="policy-ks-1",
        allowed_symbols=frozenset({"USD_JPY"}),
        allowed_sides=frozenset({OrderSide.BUY, OrderSide.SELL}),
        max_order_quantity=Decimal("10"),
        max_positions_per_symbol=2,
        max_positions_account=5,
        max_reference_age=timedelta(seconds=30),
        max_spread_by_symbol={"USD_JPY": Decimal("0.05")},
        allowed_connection_targets={ExecutionMode.FAKE: frozenset({"fake"})},
    )


def make_broker():
    broker = FakeBroker(default_fill_price=Decimal("150"), clock=lambda: NOW)
    broker.set_account_state(
        AccountState(
            account_id="fake-account",
            currency="USD",
            balance=Decimal("100000"),
            equity=Decimal("100000"),
            margin_available=Decimal("100000"),
            retrieved_at_utc=NOW,
        )
    )
    return broker


def ks_db(tmp_path):
    return tmp_path / "kill_switch.sqlite3"


def initialized_store(tmp_path):
    store = SQLiteKillSwitchStore(ks_db(tmp_path))
    store.initialize()
    return store


def make_service(tmp_path):
    """A FRESH provider + gate + service, all rebuilt from the DB paths under
    tmp_path -- calling this twice simulates a process restart (no object is
    shared between the two services; only the durable files are)."""
    decision_db_path = tmp_path / "safety.sqlite3"
    SQLiteSafetyDecisionStore(decision_db_path).initialize()
    journal_db_path = tmp_path / "journal.sqlite3"
    SubmissionJournal(journal_db_path).initialize()
    broker = make_broker()
    service = LiveOrderSubmissionService(
        connection=BrokerConnection(broker=broker),
        gate=SafetyGate(
            policy=policy(),
            state_provider=PersistentKillSwitchStateProvider(ks_db(tmp_path)),
            clock=lambda: NOW,
        ),
        decision_db_path=decision_db_path,
        order_db_path=tmp_path / "orders.sqlite3",
        journal_db_path=journal_db_path,
        clock=lambda: NOW,
        reissue_policy=ReissuePolicy(),
    )
    return service, broker


def submit_once(tmp_path, request_id="exec-ks-1", signal_id="signal-ks-1", client_order_id="client-ks-1"):
    service, broker = make_service(tmp_path)
    req = request(request_id, signal_id)
    service.register(req, client_order_id=client_order_id)
    decision, result = service.submit(req.execution_request_id, market=market())
    return decision, result, broker


def codes(decision):
    return {item.code for item in decision.rejections}


def at(minute=0):
    return NOW + timedelta(minutes=minute)


# --- store: round-trip, restart persistence, history -------------------------


def test_initialize_is_idempotent_and_creates_pristine_schema(tmp_path):
    store = initialized_store(tmp_path)
    store.initialize()  # verify-only second run
    assert store.current_state() == KillSwitchState(enabled=False, transition=None)
    assert store.list_transitions() == ()


def test_enable_round_trip_records_operator_reason_timestamp(tmp_path):
    store = initialized_store(tmp_path)
    recorded = store.record_transition(
        enabled=True, operator="ops-alice", reason="incident drill", changed_at_utc=at(1)
    )
    assert recorded == KillSwitchTransition(
        transition_id=1,
        enabled=True,
        changed_at_utc=at(1),
        operator="ops-alice",
        reason="incident drill",
    )
    state = store.current_state()
    assert state.enabled is True
    assert state.transition == recorded


def test_state_survives_store_reopen_process_restart_equivalent(tmp_path):
    initialized_store(tmp_path).record_transition(
        enabled=True, operator="ops", reason="halt", changed_at_utc=at(1)
    )
    reopened = SQLiteKillSwitchStore(ks_db(tmp_path))
    assert reopened.current_state().enabled is True
    assert reopened.list_transitions()[0].reason == "halt"


def test_history_round_trip_preserves_every_transition_in_order(tmp_path):
    store = initialized_store(tmp_path)
    store.record_transition(enabled=True, operator="a", reason="halt 1", changed_at_utc=at(1))
    store.record_transition(enabled=False, operator="b", reason="resume 1", changed_at_utc=at(2))
    store.record_transition(enabled=True, operator="c", reason="halt 2", changed_at_utc=at(3))
    history = SQLiteKillSwitchStore(ks_db(tmp_path)).list_transitions()
    assert [entry.transition_id for entry in history] == [1, 2, 3]
    assert [entry.enabled for entry in history] == [True, False, True]
    assert [entry.operator for entry in history] == ["a", "b", "c"]
    assert [entry.reason for entry in history] == ["halt 1", "resume 1", "halt 2"]
    assert [entry.changed_at_utc for entry in history] == [at(1), at(2), at(3)]
    assert SQLiteKillSwitchStore(ks_db(tmp_path)).current_state().enabled is True


def test_repeated_enable_appends_history_instead_of_overwriting(tmp_path):
    store = initialized_store(tmp_path)
    store.record_transition(enabled=True, operator="a", reason="first", changed_at_utc=at(1))
    store.record_transition(enabled=True, operator="b", reason="second", changed_at_utc=at(2))
    history = store.list_transitions()
    assert len(history) == 2
    assert store.current_state().transition.operator == "b"


def test_history_is_append_only_update_and_delete_are_blocked(tmp_path):
    store = initialized_store(tmp_path)
    store.record_transition(enabled=True, operator="a", reason="halt", changed_at_utc=at(1))
    with sqlite3.connect(ks_db(tmp_path)) as connection:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE kill_switch_transitions SET enabled = 0")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM kill_switch_transitions")
    assert store.current_state().enabled is True


def test_record_transition_validates_inputs(tmp_path):
    store = initialized_store(tmp_path)
    naive = NOW.replace(tzinfo=None)
    for kwargs in (
        dict(enabled="yes", operator="a", reason="r", changed_at_utc=at(1)),
        dict(enabled=True, operator="  ", reason="r", changed_at_utc=at(1)),
        dict(enabled=True, operator="a", reason="", changed_at_utc=at(1)),
        dict(enabled=True, operator="a", reason="r", changed_at_utc=naive),
    ):
        with pytest.raises(KillSwitchStoreError):
            store.record_transition(**kwargs)
    assert store.list_transitions() == ()


def test_db_path_env_override_and_relative_resolution(monkeypatch, tmp_path):
    monkeypatch.setenv("KILL_SWITCH_DB_PATH", str(tmp_path / "override.sqlite3"))
    assert kill_switch_db_path() == tmp_path / "override.sqlite3"
    monkeypatch.setenv("KILL_SWITCH_DB_PATH", "data/relative.sqlite3")
    resolved = kill_switch_db_path()
    assert resolved.is_absolute()
    assert resolved.parts[-3:] == ("execution", "data", "relative.sqlite3")


# --- atomic initialize + first transition (PR #126 Codex review P1) ----------


def test_initialize_and_record_creates_schema_and_row_in_one_transaction(tmp_path):
    store = SQLiteKillSwitchStore(ks_db(tmp_path))
    recorded = store.initialize_and_record(
        enabled=True, operator="ops", reason="emergency", changed_at_utc=at(1)
    )
    assert recorded.transition_id == 1
    reopened = SQLiteKillSwitchStore(ks_db(tmp_path))
    assert reopened.current_state().enabled is True
    assert len(reopened.list_transitions()) == 1


def test_initialize_and_record_appends_normally_on_an_existing_store(tmp_path):
    store = initialized_store(tmp_path)
    store.record_transition(enabled=True, operator="a", reason="halt", changed_at_utc=at(1))
    recorded = store.initialize_and_record(
        enabled=False, operator="b", reason="resume", changed_at_utc=at(2)
    )
    assert recorded.transition_id == 2
    assert store.current_state().enabled is False
    assert len(store.list_transitions()) == 2


def test_failed_initialize_and_record_never_leaves_a_committed_disabled_store(
    tmp_path, monkeypatch
):
    """The Codex P1 crash window: a failure between schema creation and the
    first transition must not leave an empty-but-valid store behind (which
    would read as DISABLED and flip the missing-DB fail-closed state to
    fail-open). The whole call is one transaction: after a mid-transaction
    failure the path is either still missing or unverifiable -- every read
    keeps raising (fail-closed), never returns disabled."""
    import execution.kill_switch_store as module

    real_create_schema = module._create_schema

    def create_schema_then_die(connection):
        real_create_schema(connection)
        raise sqlite3.OperationalError("simulated failure before the first transition")

    monkeypatch.setattr(module, "_create_schema", create_schema_then_die)
    store = SQLiteKillSwitchStore(ks_db(tmp_path))
    with pytest.raises(KillSwitchStoreError):
        store.initialize_and_record(
            enabled=True, operator="ops", reason="emergency", changed_at_utc=at(1)
        )
    monkeypatch.undo()
    with pytest.raises(KillSwitchStoreError):
        store.current_state()
    provider = PersistentKillSwitchStateProvider(ks_db(tmp_path))
    with pytest.raises(KillSwitchStoreError):
        provider.is_kill_switch_enabled()


def test_initialize_and_record_validates_inputs_without_creating_the_file(tmp_path):
    store = SQLiteKillSwitchStore(ks_db(tmp_path))
    with pytest.raises(KillSwitchStoreError):
        store.initialize_and_record(
            enabled=True, operator=" ", reason="r", changed_at_utc=at(1)
        )
    assert not ks_db(tmp_path).exists()


def test_initialize_and_record_rejects_foreign_and_versioned_files(tmp_path):
    path = ks_db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE other_component (id INTEGER)")
    before = path.read_bytes()
    with pytest.raises(KillSwitchSchemaError):
        SQLiteKillSwitchStore(path).initialize_and_record(
            enabled=True, operator="ops", reason="halt", changed_at_utc=at(1)
        )
    assert path.read_bytes() == before


# --- single-snapshot reads (PR #126 Codex review P2) --------------------------


def test_snapshot_returns_state_and_history_from_one_consistent_read(tmp_path):
    store = initialized_store(tmp_path)
    state, history = store.snapshot()
    assert state == KillSwitchState(enabled=False, transition=None)
    assert history == ()
    store.record_transition(enabled=True, operator="a", reason="halt", changed_at_utc=at(1))
    store.record_transition(enabled=False, operator="b", reason="resume", changed_at_utc=at(2))
    state, history = store.snapshot()
    # The state IS the last row of the same result set the history came
    # from -- they cannot diverge under a concurrent writer.
    assert state.transition == history[-1]
    assert state.enabled is history[-1].enabled is False
    assert [entry.transition_id for entry in history] == [1, 2]


# --- WAL-mode refusal (PR #126 Codex review P1) -------------------------------


def to_wal_mode(path):
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    finally:
        connection.close()


def test_wal_mode_db_is_refused_on_every_path_never_stale_off(tmp_path):
    path = ks_db(tmp_path)
    store = SQLiteKillSwitchStore(path)
    store.initialize()
    store.record_transition(enabled=True, operator="ops", reason="halt", changed_at_utc=at(1))
    to_wal_mode(path)
    # A committed enable could now sit in the -wal side file, invisible to an
    # immutable read. The store refuses the file entirely instead of ever
    # answering from a potentially stale view.
    with pytest.raises(KillSwitchSchemaError, match="WAL"):
        store.current_state()
    with pytest.raises(KillSwitchSchemaError, match="WAL"):
        store.list_transitions()
    with pytest.raises(KillSwitchSchemaError, match="WAL"):
        store.record_transition(
            enabled=False, operator="ops", reason="resume", changed_at_utc=at(2)
        )
    with pytest.raises(KillSwitchSchemaError, match="WAL"):
        store.initialize()
    with pytest.raises(KillSwitchSchemaError, match="WAL"):
        store.initialize_and_record(
            enabled=True, operator="ops", reason="halt again", changed_at_utc=at(3)
        )


def test_wal_mode_db_fails_closed_through_the_gate_with_zero_submits(tmp_path):
    path = ks_db(tmp_path)
    store = SQLiteKillSwitchStore(path)
    store.initialize()
    to_wal_mode(path)
    decision, result, broker = submit_once(tmp_path)
    assert decision.allowed is False
    assert SafetyRejectionCode.STATE_QUERY_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


def test_wal_mode_db_recovers_after_conversion_back_to_rollback_journal(tmp_path):
    path = ks_db(tmp_path)
    store = SQLiteKillSwitchStore(path)
    store.initialize()
    store.record_transition(enabled=True, operator="ops", reason="halt", changed_at_utc=at(1))
    to_wal_mode(path)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    finally:
        connection.close()
    assert store.current_state().enabled is True  # nothing was lost


def test_cli_status_on_wal_db_reports_unavailable_exit_1(tmp_path, capsys):
    path = ks_db(tmp_path)
    SQLiteKillSwitchStore(path).initialize()
    to_wal_mode(path)
    assert kill_switch_cli_main(["--db", str(path), "status"]) == 1
    assert "UNAVAILABLE" in capsys.readouterr().err


def test_a_wal_conversion_racing_the_open_can_never_be_read_as_disabled(tmp_path, monkeypatch):
    """PR #126 Codex review P1 (r2): the WAL refusal must not be a SEPARATE
    file-header probe. The race is injected deterministically -- another
    process converts to WAL and commits an ``enable`` exactly between the
    old probe point and the connection open -- by hooking the open itself.
    The racing connection stays OPEN so the enable really does live in the
    ``-wal`` side file (closing it would checkpoint the row into the main
    database and destroy the very condition under test).

    Under the old two-probe design the header still said rollback-journal at
    probe time, so the refusal passed, ``connect_read_only`` then added
    ``immutable=1``, and the enable sitting in ``-wal`` was invisible: the
    store answered DISABLED. Now the journal mode is observed on the open
    connection inside the read transaction, so the same race fails closed.
    """
    import execution.kill_switch_store as module
    from execution.sqlite_ro import connect_read_only as ro_connect

    path = ks_db(tmp_path)
    store = SQLiteKillSwitchStore(path)
    store.initialize()
    store.record_transition(
        enabled=False, operator="ops", reason="baseline disable", changed_at_utc=at(0)
    )
    real_connect = module.connect_read_only
    racing_writer = sqlite3.connect(path)

    def connect_after_the_race(db_path, **kwargs):
        # Lands AFTER any pre-open header probe would have run.
        if racing_writer.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            racing_writer.execute("PRAGMA journal_mode=WAL")
            racing_writer.execute(
                "INSERT INTO kill_switch_transitions "
                "(enabled, changed_at_utc, operator, reason) "
                "VALUES (1, ?, 'ops', 'emergency stop during WAL maintenance')",
                (at(1).isoformat(),),
            )
            racing_writer.commit()
        return real_connect(db_path, **kwargs)

    try:
        monkeypatch.setattr(module, "connect_read_only", connect_after_the_race)
        with pytest.raises(KillSwitchSchemaError, match="WAL"):
            store.current_state()
        with pytest.raises(KillSwitchStoreError):
            PersistentKillSwitchStateProvider(path).is_kill_switch_enabled()
        monkeypatch.undo()

        # The defect being pinned is real, not hypothetical: an immutable=1
        # view of this very file still reports the pre-race state -- exactly
        # the stale OFF the old read path would have returned.
        with closing(ro_connect(path)) as immutable_view:  # default fallback
            stale = immutable_view.execute(
                "SELECT enabled FROM kill_switch_transitions "
                "ORDER BY transition_id DESC LIMIT 1"
            ).fetchone()[0]
        assert stale == 0  # committed enable invisible -> would have read as OFF
        with closing(ro_connect(path, allow_immutable_wal=False)) as wal_aware_view:
            fresh = wal_aware_view.execute(
                "SELECT enabled FROM kill_switch_transitions "
                "ORDER BY transition_id DESC LIMIT 1"
            ).fetchone()[0]
        assert fresh == 1  # the path the store uses does see it (then refuses)
    finally:
        racing_writer.close()


def test_the_read_lock_is_held_before_the_journal_mode_is_trusted(tmp_path, monkeypatch):
    """PR #126 Codex review P1 (r3): asking ``PRAGMA journal_mode`` inside a
    bare ``BEGIN`` locks nothing, so the answer did not bind the reads that
    followed it -- another process could convert to WAL in that window and
    the read would go on to trust a mode that was no longer true.

    The read transaction now takes its SHARED lock with a real page read
    BEFORE the question is asked. This probes that directly: at the exact
    moment the mode is being trusted, a concurrent conversion must FAIL."""
    import execution.kill_switch_store as module

    path = ks_db(tmp_path)
    store = SQLiteKillSwitchStore(path)
    store.initialize()
    store.record_transition(
        enabled=True, operator="ops", reason="halt", changed_at_utc=at(1)
    )
    real_refuse = module._refuse_wal
    conversions = []

    def refuse_while_probing(connection, db_path):
        other = sqlite3.connect(path, timeout=0.5)
        try:
            other.execute("PRAGMA journal_mode=WAL")
            conversions.append("converted")
        except sqlite3.OperationalError as exc:
            conversions.append(f"blocked: {exc}")
        finally:
            other.close()
        return real_refuse(connection, db_path)

    monkeypatch.setattr(module, "_refuse_wal", refuse_while_probing)
    state = store.current_state()

    assert conversions and all(entry.startswith("blocked") for entry in conversions), conversions
    assert state.enabled is True  # the trusted read still reflects the real state


def test_no_conversion_can_land_between_the_mode_check_and_the_trusted_reads(
    tmp_path, monkeypatch
):
    """The exact scenario from the review: the mode is accepted as
    rollback-journal, and the conversion lands AFTER that check but BEFORE
    the reads that trust it (``_verify_schema`` and the row query). The lock
    taken for the snapshot is held for the whole transaction, so that window
    is closed too -- probed at the moment schema verification begins."""
    import execution.kill_switch_store as module

    path = ks_db(tmp_path)
    store = SQLiteKillSwitchStore(path)
    store.initialize()
    store.record_transition(
        enabled=True, operator="ops", reason="halt", changed_at_utc=at(1)
    )
    real_verify = module._verify_schema
    conversions = []

    def verify_after_probing(connection, db_path, *args, **kwargs):
        other = sqlite3.connect(path, timeout=0.5)
        try:
            other.execute("PRAGMA journal_mode=WAL")
            conversions.append("converted")
        except sqlite3.OperationalError as exc:
            conversions.append(f"blocked: {exc}")
        finally:
            other.close()
        return real_verify(connection, db_path, *args, **kwargs)

    monkeypatch.setattr(module, "_verify_schema", verify_after_probing)
    state = store.current_state()

    assert conversions and all(entry.startswith("blocked") for entry in conversions), conversions
    assert state.enabled is True


def test_a_wal_conversion_before_the_lock_is_still_refused(tmp_path, monkeypatch):
    """The other side of the same window: if the conversion lands BEFORE the
    read lock is taken, nothing is blocked -- so the mode check must catch
    it. Injected at the moment the snapshot is being locked."""
    import execution.kill_switch_store as module

    path = ks_db(tmp_path)
    store = SQLiteKillSwitchStore(path)
    store.initialize()
    real_lock = module._lock_snapshot

    def convert_then_lock(connection):
        converter = sqlite3.connect(path)
        try:
            converter.execute("PRAGMA journal_mode=WAL")
        finally:
            converter.close()
        return real_lock(connection)

    monkeypatch.setattr(module, "_lock_snapshot", convert_then_lock)
    with pytest.raises(KillSwitchSchemaError, match="WAL"):
        store.current_state()


def test_the_write_path_holds_its_lock_before_the_journal_mode_is_trusted(tmp_path, monkeypatch):
    """The write paths use ``BEGIN IMMEDIATE``, which already holds the
    write lock when the mode is checked -- pinned the same way as the read
    path so a future refactor cannot quietly reopen the window."""
    import execution.kill_switch_store as module

    path = ks_db(tmp_path)
    store = SQLiteKillSwitchStore(path)
    store.initialize()
    real_refuse = module._refuse_wal
    conversions = []

    def refuse_while_probing(connection, db_path):
        other = sqlite3.connect(path, timeout=0.5)
        try:
            other.execute("PRAGMA journal_mode=WAL")
            conversions.append("converted")
        except sqlite3.OperationalError as exc:
            conversions.append(f"blocked: {exc}")
        finally:
            other.close()
        return real_refuse(connection, db_path)

    monkeypatch.setattr(module, "_refuse_wal", refuse_while_probing)
    store.record_transition(
        enabled=True, operator="ops", reason="halt", changed_at_utc=at(1)
    )
    assert conversions and all(entry.startswith("blocked") for entry in conversions), conversions


def test_the_read_path_never_opens_a_wal_file_immutably(tmp_path, monkeypatch):
    """The refusal works because the read path opted OUT of the
    ``immutable=1`` fallback: an immutable connection reports
    ``journal_mode='delete'`` for a WAL file, so it could neither be
    trusted nor detected. Pin that the kill switch asks for that opt-out."""
    import execution.kill_switch_store as module

    path = ks_db(tmp_path)
    SQLiteKillSwitchStore(path).initialize()
    seen = {}
    real_connect = module.connect_read_only

    def recording_connect(db_path, **kwargs):
        seen.update(kwargs)
        return real_connect(db_path, **kwargs)

    monkeypatch.setattr(module, "connect_read_only", recording_connect)
    SQLiteKillSwitchStore(path).current_state()
    assert seen == {"allow_immutable_wal": False}


def test_default_read_only_connections_keep_the_immutable_wal_fallback(tmp_path):
    """The opt-out is kill-switch-local: every other execution store keeps
    the documented ``immutable=1`` behaviour for foreign WAL files."""
    from execution.sqlite_ro import connect_read_only as ro_connect

    path = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE t (a INTEGER)")
        connection.execute("PRAGMA journal_mode=WAL")
    with closing(ro_connect(path)) as connection:  # default: immutable fallback
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    with closing(ro_connect(path, allow_immutable_wal=False)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


# --- full-history validation before returning OFF (PR #126 review P1 r2) -----


def insert_malformed_row(path, *, enabled=1):
    """An out-of-band row that satisfies the DB CHECKs but is not a valid
    UTC timestamp -- the corruption strict restore validation must catch."""
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO kill_switch_transitions (enabled, changed_at_utc, operator, reason) "
            f"VALUES ({enabled}, 'garbage+00:00', 'x', 'y')"
        )


def test_a_valid_disable_on_top_of_a_malformed_row_still_fails_closed(tmp_path):
    """PR #126 Codex review P1 (r2): a ``LIMIT 1`` state read validated only
    the newest row, so appending a valid disable over a malformed older row
    made the gate read OFF while the CLI rejected the same DB as corrupt.
    Every state read now validates the whole history."""
    path = ks_db(tmp_path)
    store = SQLiteKillSwitchStore(path)
    store.initialize()
    insert_malformed_row(path)
    # Writes verify the schema, not past rows -- so this disable commits.
    store.initialize_and_record(
        enabled=False, operator="ops", reason="resume", changed_at_utc=at(2)
    )
    with pytest.raises(KillSwitchIntegrityError):
        store.current_state()
    with pytest.raises(KillSwitchIntegrityError):
        store.snapshot()
    provider = PersistentKillSwitchStateProvider(path)
    with pytest.raises(KillSwitchIntegrityError):
        provider.is_kill_switch_enabled()


def test_the_gate_and_the_cli_agree_on_a_partially_corrupt_store(tmp_path, capsys):
    """The two answers to "is this store trustworthy?" must not diverge: the
    CLI reporting fail-closed while the gate allowed submissions was the
    defect. Both now refuse, and the broker receives nothing."""
    path = ks_db(tmp_path)
    store = SQLiteKillSwitchStore(path)
    store.initialize()
    insert_malformed_row(path)
    store.record_transition(
        enabled=False, operator="ops", reason="resume", changed_at_utc=at(2)
    )
    assert kill_switch_cli_main(["--db", str(path), "status"]) == 1
    assert "UNAVAILABLE" in capsys.readouterr().err

    decision, result, broker = submit_once(tmp_path)
    assert decision.allowed is False
    assert SafetyRejectionCode.STATE_QUERY_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


# --- store: fail-closed on every unverifiable state --------------------------


def test_missing_db_read_raises_and_never_creates_the_file(tmp_path):
    store = SQLiteKillSwitchStore(ks_db(tmp_path))
    with pytest.raises(KillSwitchStoreError, match="does not exist"):
        store.current_state()
    with pytest.raises(KillSwitchStoreError, match="does not exist"):
        store.list_transitions()
    assert not ks_db(tmp_path).exists()


def test_missing_db_write_raises_and_never_creates_the_file(tmp_path):
    store = SQLiteKillSwitchStore(ks_db(tmp_path))
    with pytest.raises(KillSwitchStoreError, match="initialize"):
        store.record_transition(
            enabled=True, operator="a", reason="halt", changed_at_utc=at(1)
        )
    assert not ks_db(tmp_path).exists()


def test_provider_raises_for_missing_db_never_reads_as_disabled(tmp_path):
    provider = PersistentKillSwitchStateProvider(ks_db(tmp_path))
    with pytest.raises(KillSwitchStoreError):
        provider.is_kill_switch_enabled()


def test_foreign_schema_is_rejected_without_modification(tmp_path):
    path = ks_db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE other_component (id INTEGER PRIMARY KEY)")
    before = path.read_bytes()
    store = SQLiteKillSwitchStore(path)
    with pytest.raises(KillSwitchSchemaError):
        store.initialize()
    with pytest.raises(KillSwitchSchemaError):
        store.current_state()
    with pytest.raises(KillSwitchSchemaError):
        store.record_transition(
            enabled=True, operator="a", reason="halt", changed_at_utc=at(1)
        )
    assert path.read_bytes() == before


def test_lookalike_schema_missing_triggers_is_rejected(tmp_path):
    path = ks_db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE kill_switch_transitions ("
            "transition_id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "enabled INTEGER NOT NULL,"
            "changed_at_utc TEXT NOT NULL,"
            "operator TEXT NOT NULL,"
            "reason TEXT NOT NULL)"
        )
        connection.execute("PRAGMA user_version = 1")
    with pytest.raises(KillSwitchSchemaError):
        SQLiteKillSwitchStore(path).current_state()


def test_version_mismatch_is_rejected(tmp_path):
    path = ks_db(tmp_path)
    SQLiteKillSwitchStore(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(KillSwitchSchemaError, match="user_version"):
        SQLiteKillSwitchStore(path).current_state()


def test_empty_but_versioned_file_is_not_pristine(tmp_path):
    path = ks_db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 1")
    with pytest.raises(KillSwitchSchemaError, match="pristine"):
        SQLiteKillSwitchStore(path).initialize()


def test_non_sqlite_file_is_rejected(tmp_path):
    path = ks_db(tmp_path)
    path.write_bytes(b"this is not a sqlite database at all, padded " * 3)
    store = SQLiteKillSwitchStore(path)
    with pytest.raises(KillSwitchStoreError):
        store.current_state()
    with pytest.raises(KillSwitchStoreError):
        store.initialize()


def test_out_of_band_inserted_garbage_row_is_rejected_on_read(tmp_path):
    store = initialized_store(tmp_path)
    with sqlite3.connect(ks_db(tmp_path)) as connection:
        # Passes the CHECK constraints (LIKE '%+00:00') but is not a valid
        # ISO timestamp -- exactly the out-of-band edit strict restore
        # validation exists to catch.
        connection.execute(
            "INSERT INTO kill_switch_transitions (enabled, changed_at_utc, operator, reason) "
            "VALUES (1, 'garbage+00:00', 'x', 'y')"
        )
    with pytest.raises(KillSwitchIntegrityError):
        store.current_state()
    with pytest.raises(KillSwitchIntegrityError):
        store.list_transitions()


def test_state_and_transition_model_invariants():
    with pytest.raises(ValueError):
        KillSwitchState(enabled=True, transition=None)  # no transition -> disabled only
    transition = KillSwitchTransition(
        transition_id=1, enabled=True, changed_at_utc=NOW, operator="a", reason="r"
    )
    with pytest.raises(ValueError):
        KillSwitchState(enabled=False, transition=transition)  # mismatching enabled
    with pytest.raises(ValueError):
        KillSwitchTransition(
            transition_id=0, enabled=True, changed_at_utc=NOW, operator="a", reason="r"
        )
    with pytest.raises(ValueError):
        KillSwitchTransition(
            transition_id=1, enabled=True, changed_at_utc=NOW, operator="a", reason=" "
        )


# --- gate + service integration (the EXISTING boundary, FakeBroker only) -----


def test_disabled_kill_switch_allows_a_normal_submission(tmp_path):
    initialized_store(tmp_path)
    decision, result, broker = submit_once(tmp_path)
    assert decision.allowed is True
    assert result is not None and result.state is OrderState.FILLED
    assert len(broker.submitted_requests) == 1


def test_enabled_kill_switch_rejects_with_the_existing_code_and_zero_submits(tmp_path):
    initialized_store(tmp_path).record_transition(
        enabled=True, operator="ops", reason="emergency", changed_at_utc=at(1)
    )
    decision, result, broker = submit_once(tmp_path)
    assert decision.allowed is False
    assert SafetyRejectionCode.KILL_SWITCH_ENABLED in codes(decision)
    # The Issue #75 rejection-code contract is reused, not re-modelled.
    assert SafetyRejectionCode.KILL_SWITCH_ENABLED.value == "kill_switch_enabled"
    assert result is None
    assert broker.submitted_requests == []


def test_enabled_state_blocks_across_service_and_provider_recreation(tmp_path):
    initialized_store(tmp_path).record_transition(
        enabled=True, operator="ops", reason="emergency", changed_at_utc=at(1)
    )
    # First "process": rejected.
    decision_1, result_1, broker_1 = submit_once(tmp_path, "exec-a", "signal-a", "client-a")
    # Second "process": every object rebuilt from the same paths -- the stop
    # must persist because the DB, not process memory, is the truth.
    decision_2, result_2, broker_2 = submit_once(tmp_path, "exec-b", "signal-b", "client-b")
    for decision, result, broker in (
        (decision_1, result_1, broker_1),
        (decision_2, result_2, broker_2),
    ):
        assert SafetyRejectionCode.KILL_SWITCH_ENABLED in codes(decision)
        assert result is None
        assert broker.submitted_requests == []


def test_explicit_disable_resumes_new_submissions(tmp_path):
    store = initialized_store(tmp_path)
    store.record_transition(enabled=True, operator="ops", reason="halt", changed_at_utc=at(1))
    decision, result, broker = submit_once(tmp_path, "exec-a", "signal-a", "client-a")
    assert result is None and broker.submitted_requests == []
    store.record_transition(enabled=False, operator="ops", reason="resume", changed_at_utc=at(2))
    decision, result, broker = submit_once(tmp_path, "exec-b", "signal-b", "client-b")
    assert decision.allowed is True
    assert result is not None
    assert len(broker.submitted_requests) == 1


def test_missing_kill_switch_db_fails_closed_via_state_query_failed(tmp_path):
    # No initialize(): the provider raises, the gate converts, zero submits.
    decision, result, broker = submit_once(tmp_path)
    assert decision.allowed is False
    assert SafetyRejectionCode.STATE_QUERY_FAILED in codes(decision)
    assert SafetyRejectionCode.KILL_SWITCH_ENABLED not in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


@pytest.mark.parametrize("damage", ["foreign", "corrupt", "malformed_row"])
def test_unverifiable_kill_switch_db_fails_closed_with_zero_submits(tmp_path, damage):
    path = ks_db(tmp_path)
    if damage == "foreign":
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE somebody_elses (id INTEGER)")
    elif damage == "corrupt":
        path.write_bytes(b"garbage bytes, not a database, padded out " * 4)
    else:
        SQLiteKillSwitchStore(path).initialize()
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO kill_switch_transitions (enabled, changed_at_utc, operator, reason) "
                "VALUES (1, 'garbage+00:00', 'x', 'y')"
            )
    decision, result, broker = submit_once(tmp_path)
    assert decision.allowed is False
    assert SafetyRejectionCode.STATE_QUERY_FAILED in codes(decision)
    assert result is None
    assert broker.submitted_requests == []


def test_enable_from_a_second_store_instance_is_seen_without_recomposition(tmp_path):
    """Multi-process equivalent: the service's provider and an 'other
    process' store share only the DB file; an enable through the latter
    stops the NEXT submission through the former (nothing is cached)."""
    initialized_store(tmp_path)
    service, broker = make_service(tmp_path)
    first = request("exec-a", "signal-a")
    service.register(first, client_order_id="client-a")
    decision, result = service.submit(first.execution_request_id, market=market())
    assert result is not None and len(broker.submitted_requests) == 1

    SQLiteKillSwitchStore(ks_db(tmp_path)).record_transition(
        enabled=True, operator="other-process", reason="halt", changed_at_utc=at(1)
    )
    second = request("exec-b", "signal-b")
    service.register(second, client_order_id="client-b")
    decision, result = service.submit(second.execution_request_id, market=market())
    assert SafetyRejectionCode.KILL_SWITCH_ENABLED in codes(decision)
    assert result is None
    assert len(broker.submitted_requests) == 1  # unchanged


def test_concurrent_multi_instance_appends_neither_lose_nor_corrupt_history(tmp_path):
    initialized_store(tmp_path)
    barrier = threading.Barrier(6)
    errors = []

    def append(index):
        try:
            barrier.wait(timeout=10)
            SQLiteKillSwitchStore(ks_db(tmp_path)).record_transition(
                enabled=index % 2 == 0,
                operator=f"op-{index}",
                reason=f"concurrent append {index}",
                changed_at_utc=at(index),
            )
        except Exception as exc:  # pragma: no cover - failure detail
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    history = SQLiteKillSwitchStore(ks_db(tmp_path)).list_transitions()
    assert [entry.transition_id for entry in history] == [1, 2, 3, 4, 5, 6]
    assert {entry.operator for entry in history} == {f"op-{i}" for i in range(6)}
    state = SQLiteKillSwitchStore(ks_db(tmp_path)).current_state()
    assert state.transition == history[-1]


# --- non-submission paths keep working while the switch is engaged -----------


def test_reconciliation_cli_runs_while_kill_switch_is_enabled(tmp_path, monkeypatch):
    initialized_store(tmp_path).record_transition(
        enabled=True, operator="ops", reason="halt", changed_at_utc=at(1)
    )
    monkeypatch.setenv("KILL_SWITCH_DB_PATH", str(ks_db(tmp_path)))
    execution_store.init_db(db_path=tmp_path / "execution_orders.sqlite3")
    exit_code = reconciliation_main(
        [
            "--broker",
            "fake",
            "--data-dir",
            str(tmp_path),
            "--run-id",
            "run-ks-1",
            "--now",
            NOW.isoformat(),
        ]
    )
    assert exit_code == 0


def test_read_only_order_queries_run_while_kill_switch_is_enabled(tmp_path):
    initialized_store(tmp_path).record_transition(
        enabled=True, operator="ops", reason="halt", changed_at_utc=at(1)
    )
    # A rejected submit still registers the intent; the read-only queries
    # over the order store stay available during the stop.
    decision, result, broker = submit_once(tmp_path)
    assert result is None
    row = execution_store.get_execution_order("exec-ks-1", db_path=tmp_path / "orders.sqlite3")
    assert row is not None
    assert row["submission_phase"] == execution_store.PHASE_CREATED
    assert (
        list(execution_store.list_unresolved_submissions(db_path=tmp_path / "orders.sqlite3"))
        == []
    )


def test_rejection_is_recorded_in_the_audit_store_and_logged(tmp_path, caplog):
    initialized_store(tmp_path).record_transition(
        enabled=True, operator="ops", reason="halt", changed_at_utc=at(1)
    )
    with caplog.at_level("WARNING", logger="execution.live_submission"):
        decision, result, broker = submit_once(tmp_path)
    assert result is None
    # The stop is observable in logs (Issue #81: the halt must be visible)...
    assert any("kill_switch_enabled" in message for message in caplog.messages)
    # ...and in the durable decision audit trail.
    reader = SQLiteSafetyDecisionStore(tmp_path / "safety.sqlite3")
    stored = reader.list_for_request("exec-ks-1")
    assert len(stored) == 1
    assert "kill_switch_enabled" in stored[0].rejection_codes


# --- CLI ---------------------------------------------------------------------


def run_cli(tmp_path, *argv, json_output=False):
    args = ["--db", str(ks_db(tmp_path))]
    if json_output:
        args.append("--json")
    args.extend(argv)
    return kill_switch_cli_main(args)


def test_cli_enable_status_disable_happy_path(tmp_path, capsys):
    assert run_cli(tmp_path, "enable", "--operator", "ops", "--reason", "drill") == 0
    assert "ENABLED" in capsys.readouterr().out
    assert run_cli(tmp_path, "status") == 0
    out = capsys.readouterr().out
    assert "ENABLED" in out and "operator=ops" in out and "reason=drill" in out
    assert (
        run_cli(
            tmp_path,
            "disable",
            "--operator",
            "ops",
            "--reason",
            "resolved",
            "--confirm-resume",
        )
        == 0
    )
    assert "DISABLED" in capsys.readouterr().out
    assert SQLiteKillSwitchStore(ks_db(tmp_path)).current_state().enabled is False


def test_cli_json_output_round_trips_state_and_history(tmp_path, capsys):
    assert run_cli(tmp_path, "enable", "--operator", "ops", "--reason", "drill", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kill_switch_enabled"] is True
    assert payload["recorded_transition"]["operator"] == "ops"
    assert run_cli(tmp_path, "status", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "status"
    assert payload["kill_switch_enabled"] is True
    assert payload["transition_count"] == 1
    assert payload["history"][0]["reason"] == "drill"
    assert payload["last_transition"]["changed_at_utc"].endswith("+00:00")


def test_cli_status_on_missing_db_is_fail_closed_exit_1_and_creates_nothing(tmp_path, capsys):
    assert run_cli(tmp_path, "status") == 1
    err = capsys.readouterr().err
    assert "UNAVAILABLE" in err and "fail-closed" in err
    assert not ks_db(tmp_path).exists()


def test_cli_disable_without_confirm_flag_is_refused_and_changes_nothing(tmp_path, capsys):
    assert run_cli(tmp_path, "enable", "--operator", "ops", "--reason", "halt") == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "disable", "--operator", "ops", "--reason", "resume") == 2
    assert "--confirm-resume" in capsys.readouterr().err
    assert SQLiteKillSwitchStore(ks_db(tmp_path)).current_state().enabled is True


def test_cli_blank_operator_or_reason_is_a_usage_error(tmp_path, capsys):
    assert run_cli(tmp_path, "enable", "--operator", "  ", "--reason", "x") == 2
    assert run_cli(tmp_path, "enable", "--operator", "x", "--reason", "") == 2
    capsys.readouterr()
    assert not ks_db(tmp_path).exists()


def test_cli_missing_required_arguments_exit_2(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        run_cli(tmp_path, "enable", "--reason", "x")
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        kill_switch_cli_main(["--db", str(ks_db(tmp_path))])  # no subcommand
    assert excinfo.value.code == 2


def test_cli_enable_on_foreign_db_is_fail_closed_exit_1(tmp_path, capsys):
    with sqlite3.connect(ks_db(tmp_path)) as connection:
        connection.execute("CREATE TABLE other_component (id INTEGER)")
    assert run_cli(tmp_path, "enable", "--operator", "ops", "--reason", "halt") == 1
    assert "UNAVAILABLE" in capsys.readouterr().err


def test_cli_disable_on_missing_db_initializes_an_explicit_disabled_state(tmp_path, capsys):
    """An explicit, confirmed disable may initialize the store: the resulting
    state ('exists, disabled, with an attributed transition') is exactly the
    deliberate resume declaration the flag confirms."""
    assert (
        run_cli(
            tmp_path,
            "disable",
            "--operator",
            "ops",
            "--reason",
            "declare resumable",
            "--confirm-resume",
        )
        == 0
    )
    store = SQLiteKillSwitchStore(ks_db(tmp_path))
    assert store.current_state().enabled is False
    assert store.list_transitions()[0].reason == "declare resumable"
