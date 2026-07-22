"""Issue #74 / PR #91 review: SQLite persistence for execution requests and order state.

All DBs live under pytest's tmp_path. No network, no Discord, no S3, no
sleep, no real orders anywhere in this suite.
"""

import json
import logging
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from broker.broker_models import Fill, OrderResult, OrderSide, OrderState, OrderType
from execution import execution_store as store
from execution.execution_models import ExecutionRequest

T0 = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def make_request(request_id="exec-1", **overrides):
    # signal_id derives from the request id: signal_id is UNIQUE DB-wide
    # (Issue #76), so distinct default requests must carry distinct signals.
    kwargs = dict(
        execution_request_id=request_id,
        signal_id=f"sig-{request_id}",
        strategy_id="strat-1",
        symbol="XAUUSD",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("10"),
        requested_at_utc=T0,
        requested_price=Decimal("2400.00"),
        stop_loss_price=Decimal("2390.00"),
        take_profit_price=Decimal("2420.00"),
    )
    kwargs.update(overrides)
    return ExecutionRequest(**kwargs)


def fill(fill_id, quantity, seconds):
    return Fill(
        fill_id=fill_id,
        price=Decimal("2400.00"),
        quantity=Decimal(quantity),
        filled_at_utc=at(seconds),
    )


def make_result(state, request_id="exec-1", *, broker_order_id="B-1", filled="0",
                fills=(), updated_at=None, reject_reason=None, **overrides):
    fills = tuple(fills)
    filled_quantity = Decimal(filled)
    kwargs = dict(
        client_order_id=f"client-{request_id}",
        broker_order_id=broker_order_id,
        state=state,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        requested_quantity=Decimal("10"),
        filled_quantity=filled_quantity,
        remaining_quantity=Decimal("10") - filled_quantity,
        requested_price=Decimal("2400.00"),
        average_fill_price=Decimal("2400.00") if fills else None,
        updated_at_utc=updated_at or at(2),
        fills=fills,
        reject_reason=reject_reason,
    )
    kwargs.update(overrides)
    return OrderResult(**kwargs)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "execution_orders.sqlite3"


def create_order(db_path, request_id="exec-1", client_order_id=None, **overrides):
    request = make_request(request_id, **overrides)
    return store.create_execution_order(
        request,
        client_order_id=client_order_id or f"client-{request_id}",
        created_at_utc=T0,
        db_path=db_path,
    )


def submit(db_path, request_id="exec-1"):
    create_order(db_path, request_id)
    return store.mark_submitting(
        request_id, reason="submit attempt", changed_at_utc=at(1), db_path=db_path
    )


def submit_and_accept(db_path, request_id="exec-1", broker_order_id="B-1"):
    submit(db_path, request_id)
    return store.record_order_result(
        request_id,
        make_result(OrderState.ACCEPTED, request_id, broker_order_id=broker_order_id),
        reason="broker accepted",
        db_path=db_path,
    )


def schema_dump(db_path):
    with closing(sqlite3.connect(db_path)) as connection:
        return sorted(
            (row[0], row[1], row[2])
            for row in connection.execute("SELECT type, name, sql FROM sqlite_master")
        )


# --- schema init / full verification (PR #91 Major 2) ------------------------


def test_init_db_is_idempotent_and_stamps_the_schema_version(db_path):
    store.init_db(db_path)
    store.init_db(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert store.ORDERS_TABLE in tables
    assert store.HISTORY_TABLE in tables


def test_init_db_after_writes_keeps_existing_rows(db_path):
    create_order(db_path)
    store.init_db(db_path)
    assert store.get_execution_order("exec-1", db_path=db_path) is not None


def test_init_db_rejects_a_constraint_less_lookalike_schema_without_touching_it(db_path):
    # same table/column names, but no CHECK / UNIQUE / FK / NOT NULL constraints
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            f"CREATE TABLE {store.ORDERS_TABLE} "
            f"({', '.join(f'{c} TEXT' for c in store._ORDER_COLUMNS)})"
        )
        connection.execute(
            f"CREATE TABLE {store.HISTORY_TABLE} "
            f"({', '.join(f'{c} TEXT' for c in store._HISTORY_COLUMNS)})"
        )
        connection.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION}")
        connection.commit()
    before = schema_dump(db_path)
    with pytest.raises(store.ExecutionSchemaError):
        store.init_db(db_path)
    assert schema_dump(db_path) == before  # schema mismatch never modifies the DB


def test_init_db_rejects_a_partial_schema_and_does_not_autocomplete_it(db_path):
    store.init_db(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(f"DROP TABLE {store.HISTORY_TABLE}")
        connection.commit()
    before = schema_dump(db_path)
    with pytest.raises(store.ExecutionSchemaError):
        store.init_db(db_path)
    assert schema_dump(db_path) == before  # the missing table is NOT silently recreated


def test_init_db_rejects_a_missing_columns_table(db_path):
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(f"CREATE TABLE {store.ORDERS_TABLE} (execution_request_id TEXT)")
        connection.commit()
    with pytest.raises(store.ExecutionSchemaError):
        store.init_db(db_path)


def test_init_db_refuses_to_create_the_schema_inside_a_foreign_db(db_path):
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("CREATE TABLE candles (ts TEXT)")
        connection.commit()
    before = schema_dump(db_path)
    with pytest.raises(store.ExecutionSchemaError):
        store.init_db(db_path)
    assert schema_dump(db_path) == before


def test_init_db_rejects_an_unexpected_extra_table_next_to_ours(db_path):
    store.init_db(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("CREATE TABLE stray (x TEXT)")
        connection.commit()
    with pytest.raises(store.ExecutionSchemaError):
        store.init_db(db_path)


def test_init_db_rejects_a_wrong_schema_version(db_path):
    store.init_db(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("PRAGMA user_version = 99")
        connection.commit()
    with pytest.raises(store.ExecutionSchemaError):
        store.init_db(db_path)


def test_db_check_constraints_actually_fire(db_path):
    # second guard under the application-level validation: empty IDs, bad
    # enums, non-UTC timestamps and broker-id-less ACCEPTED are rejected by
    # the DB itself even when inserted out-of-band.
    store.init_db(db_path)
    insert_sql = (
        f"INSERT INTO {store.ORDERS_TABLE} ({', '.join(store._ORDER_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in store._ORDER_COLUMNS)})"
    )

    def row(**overrides):
        record = {
            "execution_request_id": "x-1",
            "signal_id": "sig",
            "strategy_id": "strat",
            "symbol": "XAUUSD",
            "side": "buy",
            "order_type": "limit",
            "quantity": "10",
            "requested_price": "2400",
            "stop_loss_price": None,
            "take_profit_price": None,
            "requested_at_utc": T0.isoformat(),
            "client_order_id": "c-x-1",
            "submission_phase": "created",
            "order_state": None,
            "broker_order_id": None,
            "last_state_reason": None,
            "last_order_result_json": None,
            "created_at_utc": T0.isoformat(),
            "updated_at_utc": T0.isoformat(),
        }
        record.update(overrides)
        return tuple(record[c] for c in store._ORDER_COLUMNS)

    bad_rows = [
        row(execution_request_id="  "),                       # empty ID
        row(side="long"),                                     # bad enum
        row(requested_at_utc="2026-07-20T12:00:00"),          # not UTC-suffixed
        row(order_type="market", requested_price="2400"),     # MARKET x price
        row(order_type="limit", requested_price=None),        # LIMIT without price
        row(submission_phase="created", order_state="pending"),          # phase x state
        row(submission_phase="submitted", order_state="accepted",
            last_state_reason="r"),                           # accepted without broker id
    ]
    with closing(sqlite3.connect(db_path)) as connection:
        for bad in bad_rows:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(insert_sql, bad)


# --- initial record save / restore ------------------------------------------


def test_created_order_round_trips_request_and_metadata(db_path):
    row = create_order(db_path)
    assert row["submission_phase"] == store.PHASE_CREATED
    assert row["order_state"] is None
    assert row["client_order_id"] == "client-exec-1"
    assert row["broker_order_id"] is None
    assert row["last_state_reason"] is None
    assert row["last_order_result_json"] is None
    assert row["created_at_utc"] == row["updated_at_utc"] == T0.isoformat()

    restored = store.load_execution_request("exec-1", db_path=db_path)
    assert restored == make_request()
    assert isinstance(restored.quantity, Decimal)
    assert restored.requested_at_utc.tzinfo is not None
    assert store.load_order_result("exec-1", db_path=db_path) is None


def test_duplicate_execution_request_id_is_a_conflict(db_path):
    create_order(db_path)
    with pytest.raises(store.ExecutionOrderConflictError):
        create_order(db_path, client_order_id="client-other")


def test_duplicate_client_order_id_is_a_conflict(db_path):
    create_order(db_path)
    with pytest.raises(store.ExecutionOrderConflictError):
        create_order(db_path, request_id="exec-2", client_order_id="client-exec-1")


# --- created -> submitting phase --------------------------------------------


def test_mark_submitting_starts_broker_state_tracking_at_pending(db_path):
    row = submit(db_path)
    assert row["submission_phase"] == store.PHASE_SUBMITTING
    assert row["order_state"] == OrderState.PENDING.value
    assert row["last_state_reason"] == "submit attempt"
    history = store.get_state_history("exec-1", db_path=db_path)
    assert len(history) == 1
    assert history[0]["previous_state"] is None
    assert history[0]["new_state"] == OrderState.PENDING.value
    assert history[0]["reason"] == "submit attempt"


def test_mark_submitting_is_entered_exactly_once(db_path):
    submit(db_path)
    with pytest.raises(ValueError):
        store.mark_submitting("exec-1", reason="again", changed_at_utc=at(2), db_path=db_path)


def test_two_racing_connections_yield_exactly_one_successful_update(db_path):
    # PR #91 review: two connections racing for the same created -> submitting
    # transition are serialized by BEGIN IMMEDIATE; exactly one wins.
    create_order(db_path)
    barrier = threading.Barrier(2)
    outcomes = []

    def attempt(worker):
        barrier.wait()
        try:
            store.mark_submitting(
                "exec-1", reason=f"submit by {worker}", changed_at_utc=at(1), db_path=db_path
            )
            outcomes.append(("ok", worker))
        except ValueError:
            outcomes.append(("lost", worker))

    threads = [threading.Thread(target=attempt, args=(w,)) for w in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(o[0] for o in outcomes) == ["lost", "ok"]
    assert len(store.get_state_history("exec-1", db_path=db_path)) == 1


def test_record_apis_require_a_submit_attempt_first(db_path):
    create_order(db_path)
    with pytest.raises(ValueError):
        store.record_order_state(
            "exec-1", OrderState.UNKNOWN, reason="lost", changed_at_utc=at(1), db_path=db_path
        )
    with pytest.raises(ValueError):
        store.record_order_result(
            "exec-1", make_result(OrderState.ACCEPTED), reason="accepted", db_path=db_path
        )


# --- state-only API restriction (PR #91 Major 1) ------------------------------


@pytest.mark.parametrize(
    "state",
    [s for s in OrderState if s is not OrderState.UNKNOWN],
)
def test_record_order_state_is_restricted_to_unknown(db_path, state):
    submit(db_path)
    with pytest.raises(ValueError, match="record_order_result"):
        store.record_order_state(
            "exec-1",
            state,
            reason="state only",
            broker_order_id="B-1",
            changed_at_utc=at(2),
            db_path=db_path,
        )


# --- broker-side observations (full snapshots) --------------------------------


def test_accepted_observation_is_stored_with_broker_order_id_and_snapshot(db_path):
    row = submit_and_accept(db_path)
    assert row["order_state"] == OrderState.ACCEPTED.value
    assert row["broker_order_id"] == "B-1"
    assert row["submission_phase"] == store.PHASE_SUBMITTED
    assert row["last_state_reason"] == "broker accepted"
    snapshot = store.load_order_result("exec-1", db_path=db_path)
    assert snapshot == make_result(OrderState.ACCEPTED)


def test_accepted_to_partially_filled_to_filled(db_path):
    submit_and_accept(db_path)
    partial = make_result(
        OrderState.PARTIALLY_FILLED, filled="3", fills=(fill("f-1", "3", 3),), updated_at=at(3)
    )
    row = store.record_order_result("exec-1", partial, reason="partial fill 3/10", db_path=db_path)
    assert row["order_state"] == OrderState.PARTIALLY_FILLED.value
    full = make_result(
        OrderState.FILLED,
        filled="10",
        fills=(fill("f-1", "3", 3), fill("f-2", "7", 4)),
        updated_at=at(4),
    )
    row = store.record_order_result("exec-1", full, reason="fully filled", db_path=db_path)
    assert row["order_state"] == OrderState.FILLED.value
    states = [h["new_state"] for h in store.get_state_history("exec-1", db_path=db_path)]
    assert states == ["pending", "accepted", "partially_filled", "filled"]
    snapshot = store.load_order_result("exec-1", db_path=db_path)
    assert [f.fill_id for f in snapshot.fills] == ["f-1", "f-2"]


def test_accepted_to_rejected(db_path):
    submit_and_accept(db_path)
    result = make_result(
        OrderState.REJECTED, reject_reason="insufficient margin", updated_at=at(3)
    )
    row = store.record_order_result("exec-1", result, reason="rejected by broker", db_path=db_path)
    assert row["order_state"] == OrderState.REJECTED.value
    assert row["last_state_reason"] == "rejected by broker"
    assert store.load_order_result("exec-1", db_path=db_path).reject_reason == "insufficient margin"


def test_accepted_to_cancelled(db_path):
    submit_and_accept(db_path)
    result = make_result(OrderState.CANCELLED, updated_at=at(3))
    row = store.record_order_result("exec-1", result, reason="cancelled", db_path=db_path)
    assert row["order_state"] == OrderState.CANCELLED.value


def test_result_for_another_client_order_is_rejected(db_path):
    submit(db_path)
    alien = make_result(OrderState.ACCEPTED, client_order_id="client-someone-else")
    with pytest.raises(ValueError):
        store.record_order_result("exec-1", alien, reason="accepted", db_path=db_path)


def test_result_with_mismatching_request_identity_is_rejected(db_path):
    submit(db_path)
    wrong_symbol = make_result(OrderState.ACCEPTED, symbol="EURUSD")
    with pytest.raises(ValueError):
        store.record_order_result("exec-1", wrong_symbol, reason="accepted", db_path=db_path)
    wrong_quantity = make_result(
        OrderState.ACCEPTED, requested_quantity=Decimal("5"), remaining_quantity=Decimal("5")
    )
    with pytest.raises(ValueError):
        store.record_order_result("exec-1", wrong_quantity, reason="accepted", db_path=db_path)


# --- stale-snapshot protection (PR #91 Major 1) -------------------------------


def test_stale_snapshot_cannot_erase_a_known_fill(db_path):
    # ACCEPTED -> PARTIALLY_FILLED(f-1) -> a caller holding the stale ACCEPTED
    # snapshot submits PARTIALLY_FILLED carrying only f-2: the DB snapshot is
    # the old side of validate_order_result_update, so f-1 cannot vanish.
    submit_and_accept(db_path)
    partial = make_result(
        OrderState.PARTIALLY_FILLED, filled="3", fills=(fill("f-1", "3", 3),), updated_at=at(3)
    )
    store.record_order_result("exec-1", partial, reason="partial", db_path=db_path)
    stale_view = make_result(
        OrderState.PARTIALLY_FILLED, filled="3", fills=(fill("f-2", "3", 4),), updated_at=at(4)
    )
    with pytest.raises(ValueError):
        store.record_order_result("exec-1", stale_view, reason="poll", db_path=db_path)
    snapshot = store.load_order_result("exec-1", db_path=db_path)
    assert [f.fill_id for f in snapshot.fills] == ["f-1"]


def test_previous_result_must_match_the_db_snapshot(db_path):
    stale_accepted = make_result(OrderState.ACCEPTED)
    submit_and_accept(db_path)
    partial = make_result(
        OrderState.PARTIALLY_FILLED, filled="3", fills=(fill("f-1", "3", 3),), updated_at=at(3)
    )
    store.record_order_result("exec-1", partial, reason="partial", db_path=db_path)
    # consistent continuation, but validated against a STALE previous_result
    grown = make_result(
        OrderState.PARTIALLY_FILLED,
        filled="6",
        fills=(fill("f-1", "3", 3), fill("f-2", "3", 4)),
        updated_at=at(4),
    )
    with pytest.raises(ValueError, match="previous_result"):
        store.record_order_result(
            "exec-1", grown, reason="poll", previous_result=stale_accepted, db_path=db_path
        )
    # the matching DB snapshot is accepted
    row = store.record_order_result(
        "exec-1", grown, reason="poll", previous_result=partial, db_path=db_path
    )
    assert row["order_state"] == OrderState.PARTIALLY_FILLED.value


def test_update_validation_still_works_after_a_restart(db_path):
    # every store call opens a fresh connection, so this IS the restart case:
    # the old snapshot comes from the DB, not from any in-memory state.
    submit_and_accept(db_path)
    partial = make_result(
        OrderState.PARTIALLY_FILLED, filled="3", fills=(fill("f-1", "3", 3),), updated_at=at(3)
    )
    store.record_order_result("exec-1", partial, reason="partial", db_path=db_path)
    fill_less_filled = make_result(OrderState.FILLED, filled="10", fills=(fill("f-9", "10", 5),),
                                   updated_at=at(5))
    with pytest.raises(ValueError):
        store.record_order_result("exec-1", fill_less_filled, reason="poll", db_path=db_path)
    # the legal continuation still lands
    proper = make_result(
        OrderState.FILLED,
        filled="10",
        fills=(fill("f-1", "3", 3), fill("f-2", "7", 5)),
        updated_at=at(5),
    )
    row = store.record_order_result("exec-1", proper, reason="filled", db_path=db_path)
    assert row["order_state"] == OrderState.FILLED.value


# --- UNKNOWN handling --------------------------------------------------------


def test_unconfirmed_submit_outcome_is_stored_as_unknown(db_path):
    submit(db_path)
    row = store.record_order_state(
        "exec-1",
        OrderState.UNKNOWN,
        reason="submit timed out",
        changed_at_utc=at(2),
        db_path=db_path,
    )
    assert row["order_state"] == OrderState.UNKNOWN.value
    # unknown proves nothing broker-side: the phase must NOT advance to submitted
    assert row["submission_phase"] == store.PHASE_SUBMITTING


def test_list_unknown_orders_returns_exactly_the_query_targets(db_path):
    # exec-1 UNKNOWN, exec-2 ACCEPTED, exec-3 still created
    submit(db_path, "exec-1")
    store.record_order_state(
        "exec-1", OrderState.UNKNOWN, reason="timeout", changed_at_utc=at(2), db_path=db_path
    )
    submit_and_accept(db_path, "exec-2", broker_order_id="B-2")
    create_order(db_path, "exec-3")

    unknown = store.list_unknown_orders(db_path=db_path)
    assert [row["execution_request_id"] for row in unknown] == ["exec-1"]
    # stays a query target until an explicit resolution is recorded
    assert store.list_unknown_orders(db_path=db_path)[0]["order_state"] == "unknown"


def test_list_unresolved_submissions_includes_unknown_and_stuck_pending(db_path):
    # exec-1: UNKNOWN (query target); exec-2: confirmed ACCEPTED (excluded);
    # exec-3: still created, no submit attempt (excluded); exec-4: submitting/
    # pending -- the attempt started but no broker observation was ever
    # recorded (crash before submit, or result + UNKNOWN-fallback both failed;
    # Issue #75 review r3) -- must stay visible to reconciliation.
    submit(db_path, "exec-1")
    store.record_order_state(
        "exec-1", OrderState.UNKNOWN, reason="timeout", changed_at_utc=at(2), db_path=db_path
    )
    submit_and_accept(db_path, "exec-2", broker_order_id="B-2")
    create_order(db_path, "exec-3")
    submit(db_path, "exec-4")

    unresolved = store.list_unresolved_submissions(db_path=db_path)
    assert sorted(row["execution_request_id"] for row in unresolved) == ["exec-1", "exec-4"]
    by_id = {row["execution_request_id"]: row for row in unresolved}
    assert by_id["exec-1"]["order_state"] == OrderState.UNKNOWN.value
    assert by_id["exec-4"]["order_state"] == OrderState.PENDING.value
    assert by_id["exec-4"]["submission_phase"] == store.PHASE_SUBMITTING
    # the stuck-pending order is NOT in the unknown-only listing
    assert [r["execution_request_id"] for r in store.list_unknown_orders(db_path=db_path)] == ["exec-1"]


def test_list_unresolved_submissions_never_creates_a_missing_db(tmp_path):
    missing = tmp_path / "never-created.sqlite3"
    assert store.list_unresolved_submissions(db_path=missing) == []
    assert not missing.exists()


@pytest.mark.parametrize(
    "target,extra",
    [
        (OrderState.ACCEPTED, {}),
        (OrderState.PARTIALLY_FILLED, {"filled": "3", "fills": (fill("f-1", "3", 3),)}),
        (OrderState.FILLED, {"filled": "10", "fills": (fill("f-1", "10", 3),)}),
        (OrderState.CANCELLED, {}),
        (OrderState.REJECTED, {"reject_reason": "rejected"}),
    ],
)
def test_unknown_resolves_to_any_allowed_concrete_state(db_path, target, extra):
    submit(db_path)
    store.record_order_state(
        "exec-1", OrderState.UNKNOWN, reason="timeout", changed_at_utc=at(2), db_path=db_path
    )
    result = make_result(target, updated_at=at(3), **extra)
    row = store.record_order_result("exec-1", result, reason="resolved by query", db_path=db_path)
    assert row["order_state"] == target.value
    assert row["execution_request_id"] not in [
        r["execution_request_id"] for r in store.list_unknown_orders(db_path=db_path)
    ]


def test_unknown_resolution_cannot_lose_known_fills(db_path):
    # fills observed before the outcome was lost keep guarding the resolution:
    # the snapshot survives the state-only UNKNOWN, so a resolution to a
    # fill-less CANCELLED or to REJECTED is refused.
    submit_and_accept(db_path)
    partial = make_result(
        OrderState.PARTIALLY_FILLED, filled="3", fills=(fill("f-1", "3", 3),), updated_at=at(3)
    )
    store.record_order_result("exec-1", partial, reason="partial", db_path=db_path)
    store.record_order_state(
        "exec-1", OrderState.UNKNOWN, reason="query timed out", changed_at_utc=at(4), db_path=db_path
    )
    fill_less_cancel = make_result(OrderState.CANCELLED, updated_at=at(5))
    with pytest.raises(ValueError):
        store.record_order_result("exec-1", fill_less_cancel, reason="resolved", db_path=db_path)
    rejected = make_result(OrderState.REJECTED, reject_reason="never placed", updated_at=at(5))
    with pytest.raises(ValueError):
        store.record_order_result("exec-1", rejected, reason="resolved", db_path=db_path)
    # a resolution that keeps the known fill is accepted
    proper = make_result(
        OrderState.CANCELLED, filled="3", fills=(fill("f-1", "3", 3),), updated_at=at(5)
    )
    row = store.record_order_result("exec-1", proper, reason="resolved", db_path=db_path)
    assert row["order_state"] == OrderState.CANCELLED.value


# --- invalid transitions / invariants ---------------------------------------


@pytest.mark.parametrize(
    "terminal,extra",
    [
        (OrderState.FILLED, {"filled": "10", "fills": (fill("f-1", "10", 3),)}),
        (OrderState.CANCELLED, {}),
        (OrderState.REJECTED, {"reject_reason": "rejected"}),
    ],
)
def test_terminal_states_are_never_left(db_path, terminal, extra):
    submit_and_accept(db_path)
    store.record_order_result(
        "exec-1", make_result(terminal, updated_at=at(3), **extra), reason="terminal",
        db_path=db_path,
    )
    with pytest.raises(ValueError):
        store.record_order_state(
            "exec-1", OrderState.UNKNOWN, reason="illegal", changed_at_utc=at(4), db_path=db_path
        )
    for other, other_extra in [
        (OrderState.ACCEPTED, {}),
        (OrderState.CANCELLED, {}),
        (OrderState.FILLED, {"filled": "10", "fills": (fill("f-1", "10", 3),)}),
    ]:
        if other is terminal:
            continue
        with pytest.raises(ValueError):
            store.record_order_result(
                "exec-1",
                make_result(other, updated_at=at(4), **other_extra),
                reason="illegal",
                db_path=db_path,
            )
    assert store.get_execution_order("exec-1", db_path=db_path)["order_state"] == terminal.value


def test_terminal_same_state_reobservation_is_allowed_and_recorded(db_path):
    submit_and_accept(db_path)
    filled = make_result(
        OrderState.FILLED, filled="10", fills=(fill("f-1", "10", 3),), updated_at=at(3)
    )
    store.record_order_result("exec-1", filled, reason="filled", db_path=db_path)
    before = len(store.get_state_history("exec-1", db_path=db_path))
    reobserved = make_result(
        OrderState.FILLED, filled="10", fills=(fill("f-1", "10", 3),), updated_at=at(4)
    )
    row = store.record_order_result("exec-1", reobserved, reason="poll re-observed", db_path=db_path)
    assert row["updated_at_utc"] == at(4).isoformat()
    assert len(store.get_state_history("exec-1", db_path=db_path)) == before + 1


def test_broker_order_id_cannot_be_replaced_or_dropped(db_path):
    submit_and_accept(db_path, broker_order_id="B-1")
    with pytest.raises(ValueError):
        store.record_order_result(
            "exec-1",
            make_result(OrderState.ACCEPTED, broker_order_id="B-OTHER", updated_at=at(3)),
            reason="poll",
            db_path=db_path,
        )
    # a full snapshot silently dropping the known broker id is refused too
    with pytest.raises(ValueError):
        store.record_order_result(
            "exec-1",
            make_result(OrderState.CANCELLED, broker_order_id=None, updated_at=at(3)),
            reason="poll",
            db_path=db_path,
        )
    # a state-only UNKNOWN omitting the broker id keeps the stored value
    row = store.record_order_state(
        "exec-1", OrderState.UNKNOWN, reason="query lost", changed_at_utc=at(3), db_path=db_path
    )
    assert row["broker_order_id"] == "B-1"
    # ...and may confirm it, but never change it
    row = store.record_order_state(
        "exec-1",
        OrderState.UNKNOWN,
        reason="query lost again",
        broker_order_id="B-1",
        changed_at_utc=at(4),
        db_path=db_path,
    )
    assert row["broker_order_id"] == "B-1"


def test_state_only_update_cannot_introduce_a_broker_order_id(db_path):
    # broker IDs are only learned from full OrderResult snapshots: a
    # state-only UNKNOWN on an order that has none may not invent one.
    submit(db_path)
    with pytest.raises(ValueError, match="only confirm"):
        store.record_order_state(
            "exec-1",
            OrderState.UNKNOWN,
            reason="timeout",
            broker_order_id="B-NEW",
            changed_at_utc=at(2),
            db_path=db_path,
        )


def test_broker_order_id_is_unique_across_orders_and_conflicts_roll_back_fully(db_path):
    # PR #91 Major 3: one broker order can never be attached to two requests;
    # the losing update must leave NO trace (current row, history, snapshot).
    submit_and_accept(db_path, "exec-1", broker_order_id="B-SAME")
    submit(db_path, "exec-2")
    before_row = store.get_execution_order("exec-2", db_path=db_path)
    before_history = store.get_state_history("exec-2", db_path=db_path)
    with pytest.raises(store.ExecutionOrderConflictError):
        store.record_order_result(
            "exec-2",
            make_result(OrderState.ACCEPTED, "exec-2", broker_order_id="B-SAME"),
            reason="accepted",
            db_path=db_path,
        )
    assert store.get_execution_order("exec-2", db_path=db_path) == before_row
    assert store.get_state_history("exec-2", db_path=db_path) == before_history
    assert store.load_order_result("exec-2", db_path=db_path) is None


def test_updated_at_utc_must_not_go_backwards(db_path):
    submit_and_accept(db_path)  # updated_at is at(2)
    stale = make_result(
        OrderState.FILLED, filled="10", fills=(fill("f-1", "10", 1),), updated_at=at(1)
    )
    with pytest.raises(ValueError):
        store.record_order_result("exec-1", stale, reason="stale observation", db_path=db_path)
    create_order(db_path, "exec-2")
    with pytest.raises(ValueError):
        store.mark_submitting(
            "exec-2", reason="submit attempt", changed_at_utc=T0 - timedelta(seconds=1),
            db_path=db_path,
        )


def test_same_state_reobservation_is_allowed_and_recorded(db_path):
    submit_and_accept(db_path)
    before = len(store.get_state_history("exec-1", db_path=db_path))
    row = store.record_order_result(
        "exec-1",
        make_result(OrderState.ACCEPTED, updated_at=at(3)),
        reason="poll re-observed accepted",
        db_path=db_path,
    )
    assert row["order_state"] == OrderState.ACCEPTED.value
    assert row["updated_at_utc"] == at(3).isoformat()
    history = store.get_state_history("exec-1", db_path=db_path)
    assert len(history) == before + 1
    assert history[-1]["previous_state"] == history[-1]["new_state"] == "accepted"


def test_naive_changed_at_is_rejected(db_path):
    create_order(db_path)
    with pytest.raises(ValueError):
        store.mark_submitting(
            "exec-1", reason="submit attempt", changed_at_utc=datetime(2026, 7, 20), db_path=db_path
        )


def test_unknown_order_id_raises_not_found(db_path):
    store.init_db(db_path)
    with pytest.raises(store.ExecutionOrderNotFoundError):
        store.record_order_state(
            "missing", OrderState.UNKNOWN, reason="x", changed_at_utc=at(1), db_path=db_path
        )
    with pytest.raises(store.ExecutionOrderNotFoundError):
        store.record_order_result(
            "missing", make_result(OrderState.ACCEPTED, "missing"), reason="x", db_path=db_path
        )


# --- atomicity of current-state update + history append ----------------------


def test_state_update_and_history_append_are_atomic(db_path, monkeypatch):
    submit_and_accept(db_path)
    real_append = store._append_history

    def failing_append(*args, **kwargs):
        # the current-row UPDATE has already run inside this transaction;
        # failing the history INSERT must roll everything back together
        raise sqlite3.OperationalError("injected history failure")

    monkeypatch.setattr(store, "_append_history", failing_append)
    filled = make_result(
        OrderState.FILLED, filled="10", fills=(fill("f-1", "10", 3),), updated_at=at(3)
    )
    with pytest.raises(sqlite3.OperationalError):
        store.record_order_result("exec-1", filled, reason="fill", db_path=db_path)
    monkeypatch.setattr(store, "_append_history", real_append)
    # the failed history append rolled the current-state + snapshot update back too
    row = store.get_execution_order("exec-1", db_path=db_path)
    assert row["order_state"] == OrderState.ACCEPTED.value
    assert row["updated_at_utc"] == at(2).isoformat()
    assert store.load_order_result("exec-1", db_path=db_path) == make_result(OrderState.ACCEPTED)
    states = [h["new_state"] for h in store.get_state_history("exec-1", db_path=db_path)]
    assert states == ["pending", "accepted"]


# --- schema initial-state gate (PR #91 r2 Major 2) ---------------------------


def test_init_db_rejects_a_version_only_db_without_touching_it(db_path):
    # a DB with user_version set but zero objects is NOT pristine: it must
    # not be silently initialized as a fresh DB.
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    with pytest.raises(store.ExecutionSchemaError):
        store.init_db(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_init_db_rejects_a_metadata_only_db(db_path):
    # only sqlite metadata (sqlite_sequence) left behind -- e.g. a half-wiped
    # DB -- is not pristine either.
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("CREATE TABLE seed (id INTEGER PRIMARY KEY AUTOINCREMENT, x TEXT)")
        connection.execute("INSERT INTO seed (x) VALUES ('y')")
        connection.execute("DROP TABLE seed")
        connection.commit()
        leftovers = [
            row[0] for row in connection.execute("SELECT name FROM sqlite_master").fetchall()
        ]
    assert leftovers == ["sqlite_sequence"]  # precondition for this scenario
    before = schema_dump(db_path)
    with pytest.raises(store.ExecutionSchemaError):
        store.init_db(db_path)
    assert schema_dump(db_path) == before


def test_null_execution_request_id_is_rejected_by_the_schema(db_path):
    store.init_db(db_path)
    insert_sql = (
        f"INSERT INTO {store.ORDERS_TABLE} ({', '.join(store._ORDER_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in store._ORDER_COLUMNS)})"
    )
    values = {
        "execution_request_id": None,
        "signal_id": "sig",
        "strategy_id": "strat",
        "symbol": "XAUUSD",
        "side": "buy",
        "order_type": "limit",
        "quantity": "10",
        "requested_price": "2400",
        "stop_loss_price": None,
        "take_profit_price": None,
        "requested_at_utc": T0.isoformat(),
        "client_order_id": "c-null",
        "submission_phase": "created",
        "order_state": None,
        "broker_order_id": None,
        "last_state_reason": None,
        "last_order_result_json": None,
        "created_at_utc": T0.isoformat(),
        "updated_at_utc": T0.isoformat(),
    }
    with closing(sqlite3.connect(db_path)) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert_sql, tuple(values[c] for c in store._ORDER_COLUMNS))


def test_index_replaced_with_a_different_definition_is_rejected(db_path):
    store.init_db(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(f"DROP INDEX {store._ORDERS_STATE_INDEX}")
        connection.execute(
            f"CREATE INDEX {store._ORDERS_STATE_INDEX} ON {store.ORDERS_TABLE} (symbol)"
        )
        connection.commit()
    with pytest.raises(store.ExecutionSchemaError):
        store.init_db(db_path)


# --- read APIs verify the schema too (PR #91 r2 Major 3) ----------------------


def test_reads_refuse_a_constraint_less_schema(db_path):
    # a lookalike schema without constraints may hold data the real schema
    # forbids (e.g. duplicate broker ids); reads must refuse it up front.
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            f"CREATE TABLE {store.ORDERS_TABLE} "
            f"({', '.join(f'{c} TEXT' for c in store._ORDER_COLUMNS)})"
        )
        connection.execute(
            f"CREATE TABLE {store.HISTORY_TABLE} "
            f"({', '.join(f'{c} TEXT' for c in store._HISTORY_COLUMNS)})"
        )
        for request_id, broker_id in (("exec-1", "B-SAME"), ("exec-2", "B-SAME")):
            connection.execute(
                f"INSERT INTO {store.ORDERS_TABLE} (execution_request_id, broker_order_id) "
                "VALUES (?, ?)",
                (request_id, broker_id),
            )
        connection.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION}")
        connection.commit()
    with pytest.raises(store.ExecutionSchemaError):
        store.get_execution_order("exec-1", db_path=db_path)
    with pytest.raises(store.ExecutionSchemaError):
        store.list_unknown_orders(db_path=db_path)
    with pytest.raises(store.ExecutionSchemaError):
        store.get_state_history("exec-1", db_path=db_path)


def test_duplicate_broker_ids_are_rejected_by_the_restore_scan():
    # unit-level check of the DB-wide duplicate scan itself (a well-formed
    # schema cannot even hold duplicates, so the scan is exercised directly).
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(
            f"CREATE TABLE {store.ORDERS_TABLE} (execution_request_id TEXT, broker_order_id TEXT)"
        )
        for request_id, broker_id in (
            ("exec-1", "B-SAME"),
            ("exec-2", "B-SAME"),
            ("exec-3", None),
            ("exec-4", None),
        ):
            connection.execute(
                f"INSERT INTO {store.ORDERS_TABLE} VALUES (?, ?)", (request_id, broker_id)
            )
        with pytest.raises(store.ExecutionIntegrityError, match="B-SAME"):
            store._assert_no_duplicate_broker_ids(connection)
        # NULLs are not duplicates of each other
        connection.execute(
            f"DELETE FROM {store.ORDERS_TABLE} WHERE execution_request_id = 'exec-2'"
        )
        store._assert_no_duplicate_broker_ids(connection)


# --- history-anchored snapshots (PR #91 r2 Major 1) ---------------------------


def _swap_current_row(db_path, set_sql, params=()):
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            f"UPDATE {store.ORDERS_TABLE} SET {set_sql} WHERE execution_request_id = 'exec-1'",
            params,
        )
        connection.commit()


def accepted_then_partial_then_unknown(db_path):
    """ACCEPTED -> PARTIALLY_FILLED(f-1) -> state-only UNKNOWN; returns the
    stale ACCEPTED snapshot JSON as an attacker would replay it."""
    submit_and_accept(db_path)
    accepted_json = store.get_execution_order("exec-1", db_path=db_path)["last_order_result_json"]
    partial = make_result(
        OrderState.PARTIALLY_FILLED, filled="3", fills=(fill("f-1", "3", 3),), updated_at=at(3)
    )
    store.record_order_result("exec-1", partial, reason="partial", db_path=db_path)
    store.record_order_state(
        "exec-1", OrderState.UNKNOWN, reason="query timed out", changed_at_utc=at(4), db_path=db_path
    )
    return accepted_json


def test_swapping_the_current_snapshot_back_is_detected_even_while_unknown(db_path):
    # THE r2 Major 1 scenario: history holds ...PARTIALLY_FILLED(f-1)->UNKNOWN
    # but the current row's snapshot is swapped back to the stale ACCEPTED.
    # UNKNOWN is not an escape hatch: the anchor comparison must fail.
    accepted_json = accepted_then_partial_then_unknown(db_path)
    _swap_current_row(db_path, "last_order_result_json = ?", (accepted_json,))
    with pytest.raises(store.ExecutionIntegrityError, match="anchored"):
        store.get_execution_order("exec-1", db_path=db_path)
    # and the fill-losing resolution the swap was meant to enable is refused
    # too, because writes validate the bundle before using any snapshot
    rejected = make_result(OrderState.REJECTED, reject_reason="never placed", updated_at=at(5))
    with pytest.raises(store.ExecutionIntegrityError):
        store.record_order_result("exec-1", rejected, reason="resolved", db_path=db_path)


def test_tampering_the_current_broker_id_is_detected(db_path):
    submit_and_accept(db_path)
    _swap_current_row(db_path, "broker_order_id = 'B-EVIL'")
    with pytest.raises(store.ExecutionIntegrityError):
        store.get_execution_order("exec-1", db_path=db_path)


def test_tampering_a_history_snapshot_is_detected(db_path):
    submit_and_accept(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            f"UPDATE {store.HISTORY_TABLE} SET order_result_json = "
            "replace(order_result_json, '\"filled_quantity\": \"0\"', "
            "'\"filled_quantity\": \"5\"') WHERE new_state = 'accepted'"
        )
        connection.commit()
    with pytest.raises(store.ExecutionIntegrityError):
        store.get_state_history("exec-1", db_path=db_path)


def test_stripping_a_snapshot_from_a_confirmed_history_row_is_detected(db_path):
    # only the initial pending entry and state-only UNKNOWN rows may lack a
    # snapshot; a confirmed observation without one is corruption.
    submit_and_accept(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            f"UPDATE {store.HISTORY_TABLE} SET order_result_json = NULL "
            "WHERE new_state = 'accepted'"
        )
        connection.commit()
    with pytest.raises(store.ExecutionIntegrityError):
        store.get_execution_order("exec-1", db_path=db_path)


def test_snapshot_json_with_unknown_fields_is_rejected(db_path):
    # PR #91 r2 Minor 1: strict key sets, top-level and per-fill.
    submit_and_accept(db_path)
    row = store.get_execution_order("exec-1", db_path=db_path)
    tampered = json.loads(row["last_order_result_json"])
    tampered["surprise"] = True
    tampered_json = json.dumps(tampered, sort_keys=True)
    with closing(sqlite3.connect(db_path)) as connection:
        # keep the anchor consistent: tamper BOTH copies identically, so only
        # the strict key validation can catch it
        connection.execute(
            f"UPDATE {store.ORDERS_TABLE} SET last_order_result_json = ? "
            "WHERE execution_request_id = 'exec-1'",
            (tampered_json,),
        )
        connection.execute(
            f"UPDATE {store.HISTORY_TABLE} SET order_result_json = ? "
            "WHERE new_state = 'accepted'",
            (tampered_json,),
        )
        connection.commit()
    with pytest.raises(store.ExecutionIntegrityError, match="snapshot"):
        store.get_execution_order("exec-1", db_path=db_path)


def test_snapshot_fill_with_unknown_fields_is_rejected():
    base = make_result(
        OrderState.PARTIALLY_FILLED, filled="3", fills=(fill("f-1", "3", 3),), updated_at=at(3)
    )
    data = json.loads(store._result_to_json(base))
    data["fills"][0]["note"] = "extra"
    with pytest.raises(ValueError, match="fill keys"):
        store._result_from_json(json.dumps(data, sort_keys=True))
    data = json.loads(store._result_to_json(base))
    del data["fills"][0]["price"]
    with pytest.raises(ValueError, match="fill keys"):
        store._result_from_json(json.dumps(data, sort_keys=True))


# --- restore-time integrity validation (PR #91 Major 2) ----------------------


def test_restore_rejects_a_current_state_that_contradicts_the_history_chain(db_path):
    submit_and_accept(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            f"UPDATE {store.ORDERS_TABLE} SET order_state = 'filled' "
            "WHERE execution_request_id = 'exec-1'"
        )
        connection.commit()
    with pytest.raises(store.ExecutionIntegrityError):
        store.get_execution_order("exec-1", db_path=db_path)


def test_restore_rejects_a_broken_history_chain(db_path):
    submit_and_accept(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            f"DELETE FROM {store.HISTORY_TABLE} WHERE new_state = 'pending'"
        )
        connection.commit()
    with pytest.raises(store.ExecutionIntegrityError):
        store.get_state_history("exec-1", db_path=db_path)


def test_restore_rejects_a_tampered_snapshot(db_path):
    submit_and_accept(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            f"UPDATE {store.ORDERS_TABLE} SET last_order_result_json = "
            "replace(last_order_result_json, '\"filled_quantity\": \"0\"', "
            "'\"filled_quantity\": \"5\"') WHERE execution_request_id = 'exec-1'"
        )
        connection.commit()
    with pytest.raises(store.ExecutionIntegrityError):
        store.load_order_result("exec-1", db_path=db_path)


# --- restart / restore -------------------------------------------------------


def test_everything_restores_after_the_db_is_reopened(db_path):
    submit_and_accept(db_path, "exec-1")
    partial = make_result(
        OrderState.PARTIALLY_FILLED, filled="3", fills=(fill("f-1", "3", 3),), updated_at=at(3)
    )
    store.record_order_result("exec-1", partial, reason="partial fill", db_path=db_path)
    submit(db_path, "exec-2")
    store.record_order_state(
        "exec-2", OrderState.UNKNOWN, reason="submit timed out", changed_at_utc=at(2), db_path=db_path
    )

    # every store call opens and closes its own connection, so a fresh call IS
    # a restart; init_db on the reopened file must also change nothing.
    store.init_db(db_path)

    restored = store.load_execution_request("exec-1", db_path=db_path)
    assert restored == make_request("exec-1")

    row = store.get_execution_order("exec-1", db_path=db_path)
    assert row["order_state"] == OrderState.PARTIALLY_FILLED.value
    assert row["broker_order_id"] == "B-1"
    assert row["last_state_reason"] == "partial fill"
    assert row["submission_phase"] == store.PHASE_SUBMITTED

    snapshot = store.load_order_result("exec-1", db_path=db_path)
    assert snapshot == partial
    assert isinstance(snapshot.filled_quantity, Decimal)
    assert snapshot.fills[0].filled_at_utc.tzinfo is not None

    history = store.get_state_history("exec-1", db_path=db_path)
    assert [(h["previous_state"], h["new_state"], h["reason"]) for h in history] == [
        (None, "pending", "submit attempt"),
        ("pending", "accepted", "broker accepted"),
        ("accepted", "partially_filled", "partial fill"),
    ]

    unknown = store.list_unknown_orders(db_path=db_path)
    assert [row["execution_request_id"] for row in unknown] == ["exec-2"]


# --- read/update side-effect guarantees --------------------------------------


def test_reads_on_a_missing_db_return_empty_and_create_nothing(db_path):
    assert store.get_execution_order("exec-1", db_path=db_path) is None
    assert store.load_execution_request("exec-1", db_path=db_path) is None
    assert store.load_order_result("exec-1", db_path=db_path) is None
    assert store.get_state_history("exec-1", db_path=db_path) == []
    assert store.list_unknown_orders(db_path=db_path) == []
    assert not db_path.exists()


def test_updates_on_a_missing_db_raise_not_found_and_create_nothing(db_path):
    with pytest.raises(store.ExecutionOrderNotFoundError):
        store.mark_submitting("exec-1", reason="x", changed_at_utc=at(1), db_path=db_path)
    with pytest.raises(store.ExecutionOrderNotFoundError):
        store.record_order_state(
            "exec-1", OrderState.UNKNOWN, reason="x", changed_at_utc=at(1), db_path=db_path
        )
    with pytest.raises(store.ExecutionOrderNotFoundError):
        store.record_order_result(
            "exec-1", make_result(OrderState.ACCEPTED), reason="x", db_path=db_path
        )
    assert not db_path.exists()


# --- trigger / view detection (PR #91 r3 Major 1) -----------------------------


def full_db_state(db_path):
    """Complete logical DB content: schema objects, version, and every row of
    both tables -- for asserting a failed API call changed NOTHING."""
    with closing(sqlite3.connect(db_path)) as connection:
        state = {
            "schema": sorted(
                tuple(row)
                for row in connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")
            ),
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
        }
        for table in (store.ORDERS_TABLE, store.HISTORY_TABLE):
            state[table] = sorted(
                tuple(row) for row in connection.execute(f"SELECT * FROM {table}")
            )
    return state


EVIL_SCHEMA_OBJECTS = {
    "after_update_trigger_deletes_history": (
        f"CREATE TRIGGER evil_wipe AFTER UPDATE ON {store.ORDERS_TABLE} BEGIN "
        f"DELETE FROM {store.HISTORY_TABLE} "
        "WHERE execution_request_id = OLD.execution_request_id; END"
    ),
    "before_update_trigger_rewrites_values": (
        f"CREATE TRIGGER evil_rewrite BEFORE UPDATE ON {store.ORDERS_TABLE} BEGIN "
        f"UPDATE {store.HISTORY_TABLE} SET reason = 'tampered'; END"
    ),
    "after_insert_trigger_adds_history_rows": (
        f"CREATE TRIGGER evil_extra AFTER INSERT ON {store.HISTORY_TABLE} BEGIN "
        f"INSERT INTO {store.HISTORY_TABLE} (execution_request_id, previous_state, "
        "new_state, reason, broker_order_id, order_result_json, changed_at_utc) "
        "VALUES (NEW.execution_request_id, NEW.new_state, 'unknown', 'evil', "
        "NULL, NULL, NEW.changed_at_utc); END"
    ),
    "view_over_orders": f"CREATE VIEW evil_view AS SELECT * FROM {store.ORDERS_TABLE}",
}


def _install(db_path, ddl):
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(ddl)
        connection.commit()


@pytest.mark.parametrize("evil_ddl", list(EVIL_SCHEMA_OBJECTS.values()), ids=list(EVIL_SCHEMA_OBJECTS))
def test_unexpected_triggers_and_views_are_rejected_by_every_api_without_any_write(
    db_path, evil_ddl
):
    # PR #91 r3 Major 1: schema verification diffs the COMPLETE sqlite_master
    # object set, so a trigger or view of any kind fails every public API up
    # front -- reads and writes alike -- and the DB file is not touched.
    submit_and_accept(db_path)
    _install(db_path, evil_ddl)
    before_bytes = db_path.read_bytes()
    before_state = full_db_state(db_path)
    cancelled = make_result(OrderState.CANCELLED, updated_at=at(3))
    api_calls = [
        lambda: store.init_db(db_path),
        lambda: store.get_execution_order("exec-1", db_path=db_path),
        lambda: store.load_execution_request("exec-1", db_path=db_path),
        lambda: store.load_order_result("exec-1", db_path=db_path),
        lambda: store.get_state_history("exec-1", db_path=db_path),
        lambda: store.list_unknown_orders(db_path=db_path),
        lambda: store.mark_submitting(
            "exec-1", reason="again", changed_at_utc=at(3), db_path=db_path
        ),
        lambda: store.record_order_state(
            "exec-1", OrderState.UNKNOWN, reason="lost", changed_at_utc=at(3), db_path=db_path
        ),
        lambda: store.record_order_result("exec-1", cancelled, reason="cancel", db_path=db_path),
        lambda: create_order(db_path, "exec-2"),
    ]
    for api_call in api_calls:
        with pytest.raises(store.ExecutionSchemaError):
            api_call()
    assert db_path.read_bytes() == before_bytes
    assert full_db_state(db_path) == before_state


def test_r3_major_repro_history_wiping_trigger_cannot_corrupt_a_cancel(db_path):
    # The exact r3 Major 1 reproduction: ACCEPTED is stored, then a trigger
    # is installed that wipes this request's history on any orders UPDATE.
    # Before this fix record_order_result(CANCELLED) committed the corrupted
    # state and only the post-commit restore raised ExecutionIntegrityError.
    # Now the trigger is detected up front: the call fails with
    # ExecutionSchemaError and current/history/snapshot are untouched.
    submit_and_accept(db_path)
    _install(db_path, EVIL_SCHEMA_OBJECTS["after_update_trigger_deletes_history"])
    before = full_db_state(db_path)
    cancelled = make_result(OrderState.CANCELLED, updated_at=at(3))
    with pytest.raises(store.ExecutionSchemaError):
        store.record_order_result("exec-1", cancelled, reason="cancel", db_path=db_path)
    assert full_db_state(db_path) == before
    # dropping the foreign trigger restores the DB to a valid schema: the
    # very same update then succeeds with the full history intact
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("DROP TRIGGER evil_wipe")
        connection.commit()
    row = store.record_order_result("exec-1", cancelled, reason="cancel", db_path=db_path)
    assert row["order_state"] == OrderState.CANCELLED.value
    states = [h["new_state"] for h in store.get_state_history("exec-1", db_path=db_path)]
    assert states == ["pending", "accepted", "cancelled"]


MID_TXN_SIDE_EFFECTS = {
    "deletes_history": (
        f"DELETE FROM {store.HISTORY_TABLE} "
        "WHERE execution_request_id = OLD.execution_request_id;"
    ),
    "tampers_prior_history": (
        f"UPDATE {store.HISTORY_TABLE} SET reason = 'tampered' WHERE new_state = 'accepted';"
    ),
    "rewrites_current_row": (
        f"UPDATE {store.ORDERS_TABLE} SET last_state_reason = 'tampered' "
        "WHERE execution_request_id = OLD.execution_request_id;"
    ),
}


@pytest.mark.parametrize(
    "side_effect", list(MID_TXN_SIDE_EFFECTS.values()), ids=list(MID_TXN_SIDE_EFFECTS)
)
def test_side_effects_inside_the_write_transaction_roll_back_before_commit(
    db_path, monkeypatch, side_effect
):
    # Defense in depth for r3 Major 1: even a side effect that appears AFTER
    # schema verification (simulated by installing a trigger mid-transaction,
    # right after the verify step) must be caught by the pre-commit bundle
    # re-validation and rolled back -- corrupted state is never committed,
    # and a failed API call leaves current/history/snapshot exactly as-is.
    submit_and_accept(db_path)
    before = full_db_state(db_path)
    real_verify = store._verify_schema

    def verify_then_install_trigger(connection, path):
        real_verify(connection, path)
        connection.execute(
            f"CREATE TRIGGER evil AFTER UPDATE ON {store.ORDERS_TABLE} BEGIN {side_effect} END"
        )

    monkeypatch.setattr(store, "_verify_schema", verify_then_install_trigger)
    cancelled = make_result(OrderState.CANCELLED, updated_at=at(3))
    with pytest.raises(store.ExecutionIntegrityError):
        store.record_order_result("exec-1", cancelled, reason="cancel", db_path=db_path)
    monkeypatch.undo()
    assert full_db_state(db_path) == before
    # with the side effect gone, the very same update commits cleanly
    row = store.record_order_result("exec-1", cancelled, reason="cancel", db_path=db_path)
    assert row["order_state"] == OrderState.CANCELLED.value


# --- equivalent-DDL tolerance (PR #91 r3 Minor 1) -----------------------------


def _write_schema(db_path, orders_sql, history_sql=None, index_sqls=None):
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(orders_sql)
        connection.execute(history_sql if history_sql is not None else store._create_history_table_sql())
        for sql in index_sqls if index_sqls is not None else store._create_index_sql().values():
            connection.execute(sql)
        connection.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION}")
        connection.commit()


def test_equivalent_ddl_with_quoted_identifiers_and_cosmetic_differences_is_accepted(db_path):
    # PR #91 r3 Minor 1: SQLite-equivalent DDL -- quoted table / column
    # names, collapsed whitespace, lowercased keywords, a redundant extra
    # paren pair around a CHECK expression, a trailing comment -- must NOT be
    # rejected: PRAGMA structure and token-normalized DDL both match.
    orders_sql = store._create_orders_table_sql()
    column_def = "signal_id TEXT NOT NULL CHECK (trim(signal_id) <> '')"
    assert column_def in orders_sql
    orders_sql = orders_sql.replace(
        column_def, '"signal_id" TEXT NOT NULL CHECK ((trim("signal_id") <> \'\'))', 1
    )
    orders_sql = orders_sql.replace(
        f"CREATE TABLE {store.ORDERS_TABLE}", f'CREATE TABLE "{store.ORDERS_TABLE}"', 1
    )
    # collapse all whitespace/newlines and lowercase every keyword (string
    # literals in this DDL are lowercase already, so .lower() is equivalence-
    # preserving here)
    orders_sql = " ".join(orders_sql.split()).lower()
    history_sql = store._create_history_table_sql() + " -- audit trail of state changes"
    index_sqls = [
        f'create index "{store._ORDERS_STATE_INDEX}" on "{store.ORDERS_TABLE}" '
        "(order_state, updated_at_utc)",
        store._create_index_sql()[store._HISTORY_REQUEST_INDEX],
        f'create UNIQUE index "{store._SIGNAL_UNIQUE_INDEX}" on {store.ORDERS_TABLE} (signal_id)',
    ]
    _write_schema(db_path, orders_sql, history_sql, index_sqls)
    store.init_db(db_path)  # verification accepts the equivalent schema
    # and the full normal workflow runs against it
    submit_and_accept(db_path)
    row = store.get_execution_order("exec-1", db_path=db_path)
    assert row["order_state"] == OrderState.ACCEPTED.value
    assert [h["new_state"] for h in store.get_state_history("exec-1", db_path=db_path)] == [
        "pending",
        "accepted",
    ]


ORDERS_DDL_DEGRADATIONS = {
    "check_removed": ("CHECK (trim(signal_id) <> '')", ""),
    "check_expression_altered": (
        "requested_at_utc LIKE '%+00:00'",
        "requested_at_utc LIKE '%'",
    ),
    "check_literal_altered": (
        "CHECK (trim(signal_id) <> '')",
        "CHECK (trim(signal_id) <> ' ')",
    ),
    "check_literal_case_changed": ("'created'", "'CREATED'"),
    "not_null_removed": ("signal_id TEXT NOT NULL", "signal_id TEXT"),
    "unique_removed": ("client_order_id TEXT NOT NULL UNIQUE", "client_order_id TEXT NOT NULL"),
    "without_rowid_removed": (") WITHOUT ROWID", ")"),
}


@pytest.mark.parametrize(
    "needle, replacement",
    list(ORDERS_DDL_DEGRADATIONS.values()),
    ids=list(ORDERS_DDL_DEGRADATIONS),
)
def test_degraded_orders_ddl_is_still_rejected(db_path, needle, replacement):
    # the tolerant comparison must stay strict about SEMANTIC differences:
    # missing/altered CHECKs (PRAGMA-invisible -- only the token comparison
    # can catch them), dropped NOT NULL / UNIQUE / WITHOUT ROWID.
    orders_sql = store._create_orders_table_sql()
    assert needle in orders_sql
    _write_schema(db_path, orders_sql.replace(needle, replacement, 1))
    with pytest.raises(store.ExecutionSchemaError):
        store.init_db(db_path)
    with pytest.raises(store.ExecutionSchemaError):
        store.get_execution_order("exec-1", db_path=db_path)


def test_history_foreign_key_removed_is_rejected(db_path):
    history_sql = store._create_history_table_sql()
    needle = f"REFERENCES {store.ORDERS_TABLE} (execution_request_id)"
    assert needle in history_sql
    _write_schema(db_path, store._create_orders_table_sql(), history_sql.replace(needle, "", 1))
    with pytest.raises(store.ExecutionSchemaError):
        store.init_db(db_path)


def test_dropped_index_is_rejected(db_path):
    store.init_db(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(f"DROP INDEX {store._HISTORY_REQUEST_INDEX}")
        connection.commit()
    with pytest.raises(store.ExecutionSchemaError):
        store.get_execution_order("exec-1", db_path=db_path)


# --- Issue #76: source-signal uniqueness / duplicate suppression / migration --


def _raw_created_row(connection, request_id, signal_id):
    """Insert a well-formed `created` row out-of-band (bypassing the store's
    constraints only where the schema itself allows it -- used to seed a v2
    DB with data the v3 constraint would refuse)."""
    values = {
        "execution_request_id": request_id,
        "signal_id": signal_id,
        "strategy_id": "strat-1",
        "symbol": "XAUUSD",
        "side": "buy",
        "order_type": "limit",
        "quantity": "10",
        "requested_price": "2400.00",
        "stop_loss_price": None,
        "take_profit_price": None,
        "requested_at_utc": T0.isoformat(),
        "client_order_id": f"client-{request_id}",
        "submission_phase": "created",
        "order_state": None,
        "broker_order_id": None,
        "last_state_reason": None,
        "last_order_result_json": None,
        "created_at_utc": T0.isoformat(),
        "updated_at_utc": T0.isoformat(),
    }
    connection.execute(
        f"INSERT INTO {store.ORDERS_TABLE} ({', '.join(store._ORDER_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in store._ORDER_COLUMNS)})",
        tuple(values[c] for c in store._ORDER_COLUMNS),
    )


def _downgrade_to_v2(db_path):
    """Turn a freshly created v3 DB into a genuine released v2 DB: drop the
    Issue #76 unique signal index and stamp the v2 version. Rows persist."""
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(f"DROP INDEX {store._SIGNAL_UNIQUE_INDEX}")
        connection.execute(f"PRAGMA user_version = {store._MIGRATABLE_VERSION}")
        connection.commit()


def test_duplicate_signal_id_is_a_conflict_and_creates_no_second_row(db_path):
    create_order(db_path)
    with pytest.raises(store.ExecutionOrderConflictError):
        create_order(db_path, request_id="exec-2", signal_id="sig-exec-1")
    assert store.get_execution_order("exec-2", db_path=db_path) is None
    row = store.get_execution_order_by_signal_id("sig-exec-1", db_path=db_path)
    assert row["execution_request_id"] == "exec-1"


def test_duplicate_signal_suppression_is_logged_with_ids_state_and_reason(db_path, caplog):
    create_order(db_path)
    store.mark_submitting("exec-1", reason="submit", changed_at_utc=at(1), db_path=db_path)
    with caplog.at_level(logging.WARNING, logger="execution.execution_store"):
        with pytest.raises(store.ExecutionOrderConflictError):
            create_order(db_path, request_id="exec-2", signal_id="sig-exec-1")
    message = "\n".join(r.getMessage() for r in caplog.records)
    assert "execution_request_id=exec-2" in message
    assert "signal_id=sig-exec-1" in message
    assert "existing_execution_request_id=exec-1" in message
    assert "existing_submission_phase=submitting" in message
    assert "existing_order_state=pending" in message
    assert "reason=source signal already produced an execution request" in message


def test_duplicate_request_id_suppression_is_logged(db_path, caplog):
    create_order(db_path)
    with caplog.at_level(logging.WARNING, logger="execution.execution_store"):
        with pytest.raises(store.ExecutionOrderConflictError):
            create_order(db_path, client_order_id="client-other", signal_id="sig-other")
    message = "\n".join(r.getMessage() for r in caplog.records)
    assert "execution_request_id=exec-1" in message
    assert "existing_submission_phase=created" in message
    assert "reason=execution_request_id is already persisted" in message


def test_concurrent_creates_from_the_same_signal_yield_exactly_one_row(db_path):
    # Two independent DB connections (each store call opens its own --
    # equivalent to two processes) race to create an intent from the SAME
    # source signal. The UNIQUE constraint inside BEGIN IMMEDIATE -- not any
    # pre-SELECT -- guarantees exactly one row and a clean conflict error
    # for the loser (no crash, DB stays usable).
    store.init_db(db_path)
    barrier = threading.Barrier(2)
    outcomes = []

    def attempt(request_id):
        barrier.wait()
        try:
            create_order(db_path, request_id=request_id, signal_id="sig-shared")
            outcomes.append(("ok", request_id))
        except store.ExecutionOrderConflictError:
            outcomes.append(("suppressed", request_id))

    threads = [threading.Thread(target=attempt, args=(rid,)) for rid in ("exec-a", "exec-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(o[0] for o in outcomes) == ["ok", "suppressed"]
    winner = store.get_execution_order_by_signal_id("sig-shared", db_path=db_path)
    assert winner is not None
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute(
            f"SELECT COUNT(*) FROM {store.ORDERS_TABLE}"
        ).fetchone()[0] == 1
    # the DB remains fully usable after the suppressed conflict
    store.mark_submitting(
        winner["execution_request_id"], reason="submit", changed_at_utc=at(1), db_path=db_path
    )


def test_signal_uniqueness_is_enforced_by_the_db_itself(db_path):
    # second guard: even an out-of-band INSERT cannot create a second row
    # for the same signal on a v3 schema.
    create_order(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _raw_created_row(connection, "exec-oob", "sig-exec-1")


def test_get_execution_order_by_signal_id_round_trips(db_path):
    create_order(db_path)
    row = store.get_execution_order_by_signal_id("sig-exec-1", db_path=db_path)
    assert row["execution_request_id"] == "exec-1"
    assert store.get_execution_order_by_signal_id("sig-none", db_path=db_path) is None


def test_get_execution_order_by_signal_id_never_creates_a_missing_db(db_path):
    assert store.get_execution_order_by_signal_id("sig-1", db_path=db_path) is None
    assert not db_path.exists()


def test_init_db_migrates_a_v2_db_in_place_and_keeps_rows_readable(db_path):
    # a released v2 DB (rows included, one already accepted) migrates
    # additively: unique signal index + version bump, rows byte-identical.
    submit_and_accept(db_path)
    create_order(db_path, request_id="exec-2")
    _downgrade_to_v2(db_path)
    rows_before = full_db_state(db_path)[store.ORDERS_TABLE]

    store.init_db(db_path)

    after = full_db_state(db_path)
    assert after["user_version"] == store.SCHEMA_VERSION
    assert any(name == store._SIGNAL_UNIQUE_INDEX for _, name, _, _ in after["schema"])
    assert after[store.ORDERS_TABLE] == rows_before  # nothing deleted or rewritten
    # existing records restore through the normal validated read path
    assert store.get_execution_order("exec-1", db_path=db_path)["order_state"] == "accepted"
    assert store.load_execution_request("exec-2", db_path=db_path) == make_request("exec-2")
    # the migrated constraint is live
    with pytest.raises(store.ExecutionOrderConflictError):
        create_order(db_path, request_id="exec-3", signal_id="sig-exec-1")
    store.init_db(db_path)  # and migration is idempotent


def test_migration_with_duplicate_signals_fails_explicitly_and_changes_nothing(db_path):
    create_order(db_path)
    _downgrade_to_v2(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        _raw_created_row(connection, "exec-dup", "sig-exec-1")  # legal on v2
        connection.commit()
    before = full_db_state(db_path)

    with pytest.raises(store.ExecutionSchemaError, match="sig-exec-1"):
        store.init_db(db_path)

    assert full_db_state(db_path) == before  # no rows deleted, no index, still v2


def test_reads_never_migrate_a_v2_db(db_path):
    create_order(db_path)
    _downgrade_to_v2(db_path)
    before = full_db_state(db_path)
    with pytest.raises(store.ExecutionSchemaError, match="init_db"):
        store.get_execution_order("exec-1", db_path=db_path)
    with pytest.raises(store.ExecutionSchemaError):
        store.get_execution_order_by_signal_id("sig-exec-1", db_path=db_path)
    assert full_db_state(db_path) == before


def test_updates_refuse_a_v2_db_without_migrating_it(db_path):
    create_order(db_path)
    _downgrade_to_v2(db_path)
    before = full_db_state(db_path)
    with pytest.raises(store.ExecutionSchemaError):
        store.mark_submitting("exec-1", reason="submit", changed_at_utc=at(1), db_path=db_path)
    assert full_db_state(db_path) == before
