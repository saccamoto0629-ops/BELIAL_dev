"""Issue #74: SQLite persistence for execution requests and order state.

All DBs live under pytest's tmp_path. No network, no Discord, no S3, no
sleep, no real orders anywhere in this suite.
"""

import sqlite3
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
    kwargs = dict(
        execution_request_id=request_id,
        signal_id="sig-1",
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


def submit_and_accept(db_path, request_id="exec-1", broker_order_id="B-1"):
    create_order(db_path, request_id)
    store.mark_submitting(request_id, reason="submit attempt", changed_at_utc=at(1), db_path=db_path)
    return store.record_order_state(
        request_id,
        OrderState.ACCEPTED,
        reason="broker accepted",
        broker_order_id=broker_order_id,
        changed_at_utc=at(2),
        db_path=db_path,
    )


# --- schema init / idempotency ----------------------------------------------


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


def test_init_db_rejects_an_existing_table_with_missing_columns(db_path):
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(f"CREATE TABLE {store.ORDERS_TABLE} (execution_request_id TEXT)")
        connection.commit()
    with pytest.raises(store.ExecutionSchemaError):
        store.init_db(db_path)


# --- initial record save / restore ------------------------------------------


def test_created_order_round_trips_request_and_metadata(db_path):
    row = create_order(db_path)
    assert row["submission_phase"] == store.PHASE_CREATED
    assert row["order_state"] is None
    assert row["client_order_id"] == "client-exec-1"
    assert row["broker_order_id"] is None
    assert row["last_state_reason"] is None
    assert row["created_at_utc"] == row["updated_at_utc"] == T0.isoformat()

    restored = store.load_execution_request("exec-1", db_path=db_path)
    assert restored == make_request()
    assert isinstance(restored.quantity, Decimal)
    assert restored.requested_at_utc.tzinfo is not None


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
    create_order(db_path)
    row = store.mark_submitting(
        "exec-1", reason="submit attempt", changed_at_utc=at(1), db_path=db_path
    )
    assert row["submission_phase"] == store.PHASE_SUBMITTING
    assert row["order_state"] == OrderState.PENDING.value
    assert row["last_state_reason"] == "submit attempt"
    history = store.get_state_history("exec-1", db_path=db_path)
    assert len(history) == 1
    assert history[0]["previous_state"] is None
    assert history[0]["new_state"] == OrderState.PENDING.value
    assert history[0]["reason"] == "submit attempt"


def test_mark_submitting_is_entered_exactly_once(db_path):
    create_order(db_path)
    store.mark_submitting("exec-1", reason="submit attempt", changed_at_utc=at(1), db_path=db_path)
    with pytest.raises(ValueError):
        store.mark_submitting("exec-1", reason="again", changed_at_utc=at(2), db_path=db_path)


def test_record_order_state_requires_a_submit_attempt_first(db_path):
    create_order(db_path)
    with pytest.raises(ValueError):
        store.record_order_state(
            "exec-1",
            OrderState.ACCEPTED,
            reason="broker accepted",
            broker_order_id="B-1",
            changed_at_utc=at(1),
            db_path=db_path,
        )


# --- broker-side observations -----------------------------------------------


def test_accepted_observation_is_stored_with_broker_order_id(db_path):
    row = submit_and_accept(db_path)
    assert row["order_state"] == OrderState.ACCEPTED.value
    assert row["broker_order_id"] == "B-1"
    assert row["submission_phase"] == store.PHASE_SUBMITTED
    assert row["last_state_reason"] == "broker accepted"


def test_accepted_requires_a_broker_order_id(db_path):
    create_order(db_path)
    store.mark_submitting("exec-1", reason="submit attempt", changed_at_utc=at(1), db_path=db_path)
    with pytest.raises(ValueError):
        store.record_order_state(
            "exec-1",
            OrderState.ACCEPTED,
            reason="broker accepted",
            changed_at_utc=at(2),
            db_path=db_path,
        )


def test_accepted_to_partially_filled_to_filled(db_path):
    submit_and_accept(db_path)
    row = store.record_order_state(
        "exec-1",
        OrderState.PARTIALLY_FILLED,
        reason="partial fill 3/10",
        changed_at_utc=at(3),
        db_path=db_path,
    )
    assert row["order_state"] == OrderState.PARTIALLY_FILLED.value
    row = store.record_order_state(
        "exec-1", OrderState.FILLED, reason="fully filled", changed_at_utc=at(4), db_path=db_path
    )
    assert row["order_state"] == OrderState.FILLED.value
    states = [h["new_state"] for h in store.get_state_history("exec-1", db_path=db_path)]
    assert states == ["pending", "accepted", "partially_filled", "filled"]


def test_accepted_to_rejected(db_path):
    submit_and_accept(db_path)
    row = store.record_order_state(
        "exec-1",
        OrderState.REJECTED,
        reason="rejected by broker",
        changed_at_utc=at(3),
        db_path=db_path,
    )
    assert row["order_state"] == OrderState.REJECTED.value
    assert row["last_state_reason"] == "rejected by broker"


def test_accepted_to_cancelled(db_path):
    submit_and_accept(db_path)
    row = store.record_order_state(
        "exec-1", OrderState.CANCELLED, reason="cancelled", changed_at_utc=at(3), db_path=db_path
    )
    assert row["order_state"] == OrderState.CANCELLED.value


# --- UNKNOWN handling --------------------------------------------------------


def test_unconfirmed_submit_outcome_is_stored_as_unknown(db_path):
    create_order(db_path)
    store.mark_submitting("exec-1", reason="submit attempt", changed_at_utc=at(1), db_path=db_path)
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
    create_order(db_path, "exec-1")
    store.mark_submitting("exec-1", reason="submit attempt", changed_at_utc=at(1), db_path=db_path)
    store.record_order_state(
        "exec-1", OrderState.UNKNOWN, reason="timeout", changed_at_utc=at(2), db_path=db_path
    )
    submit_and_accept(db_path, "exec-2", broker_order_id="B-2")
    create_order(db_path, "exec-3")

    unknown = store.list_unknown_orders(db_path=db_path)
    assert [row["execution_request_id"] for row in unknown] == ["exec-1"]
    # stays a query target until an explicit resolution is recorded
    assert store.list_unknown_orders(db_path=db_path)[0]["order_state"] == "unknown"


@pytest.mark.parametrize(
    "target,broker_order_id",
    [
        (OrderState.ACCEPTED, "B-1"),
        (OrderState.PARTIALLY_FILLED, "B-1"),
        (OrderState.FILLED, "B-1"),
        (OrderState.CANCELLED, None),
        (OrderState.REJECTED, None),
    ],
)
def test_unknown_resolves_to_any_allowed_concrete_state(db_path, target, broker_order_id):
    create_order(db_path)
    store.mark_submitting("exec-1", reason="submit attempt", changed_at_utc=at(1), db_path=db_path)
    store.record_order_state(
        "exec-1", OrderState.UNKNOWN, reason="timeout", changed_at_utc=at(2), db_path=db_path
    )
    row = store.record_order_state(
        "exec-1",
        target,
        reason="resolved by query",
        broker_order_id=broker_order_id,
        changed_at_utc=at(3),
        db_path=db_path,
    )
    assert row["order_state"] == target.value
    assert row["execution_request_id"] not in [
        r["execution_request_id"] for r in store.list_unknown_orders(db_path=db_path)
    ]


# --- invalid transitions / invariants ---------------------------------------


@pytest.mark.parametrize(
    "terminal,extra",
    [
        (OrderState.FILLED, {"broker_order_id": "B-1"}),
        (OrderState.CANCELLED, {}),
        (OrderState.REJECTED, {}),
    ],
)
def test_terminal_states_are_never_left(db_path, terminal, extra):
    submit_and_accept(db_path)
    store.record_order_state(
        "exec-1", terminal, reason="terminal", changed_at_utc=at(3), db_path=db_path, **extra
    )
    for other in OrderState:
        if other is terminal:
            continue
        with pytest.raises(ValueError):
            store.record_order_state(
                "exec-1",
                other,
                reason="illegal",
                broker_order_id="B-1",
                changed_at_utc=at(4),
                db_path=db_path,
            )
    assert store.get_execution_order("exec-1", db_path=db_path)["order_state"] == terminal.value


def test_broker_order_id_cannot_be_replaced(db_path):
    submit_and_accept(db_path, broker_order_id="B-1")
    with pytest.raises(ValueError):
        store.record_order_state(
            "exec-1",
            OrderState.PARTIALLY_FILLED,
            reason="partial",
            broker_order_id="B-OTHER",
            changed_at_utc=at(3),
            db_path=db_path,
        )
    # an observation omitting the broker id keeps the stored value
    row = store.record_order_state(
        "exec-1",
        OrderState.PARTIALLY_FILLED,
        reason="partial",
        changed_at_utc=at(3),
        db_path=db_path,
    )
    assert row["broker_order_id"] == "B-1"


def test_updated_at_utc_must_not_go_backwards(db_path):
    submit_and_accept(db_path)  # updated_at is at(2)
    with pytest.raises(ValueError):
        store.record_order_state(
            "exec-1",
            OrderState.FILLED,
            reason="stale observation",
            changed_at_utc=at(1),
            db_path=db_path,
        )
    create_order(db_path, "exec-2")
    with pytest.raises(ValueError):
        store.mark_submitting(
            "exec-2", reason="submit attempt", changed_at_utc=T0 - timedelta(seconds=1), db_path=db_path
        )


def test_same_state_reobservation_is_allowed_and_recorded(db_path):
    submit_and_accept(db_path)
    before = len(store.get_state_history("exec-1", db_path=db_path))
    row = store.record_order_state(
        "exec-1",
        OrderState.ACCEPTED,
        reason="poll re-observed accepted",
        changed_at_utc=at(3),
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


# --- atomicity of current-state update + history append ----------------------


def test_state_update_and_history_append_are_atomic(db_path):
    submit_and_accept(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(f"ALTER TABLE {store.HISTORY_TABLE} RENAME TO {store.HISTORY_TABLE}_gone")
        connection.commit()
    with pytest.raises(sqlite3.OperationalError):
        store.record_order_state(
            "exec-1", OrderState.FILLED, reason="fill", changed_at_utc=at(3), db_path=db_path
        )
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            f"ALTER TABLE {store.HISTORY_TABLE}_gone RENAME TO {store.HISTORY_TABLE}"
        )
        connection.commit()
    # the failed history append rolled the current-state update back too
    row = store.get_execution_order("exec-1", db_path=db_path)
    assert row["order_state"] == OrderState.ACCEPTED.value
    assert row["updated_at_utc"] == at(2).isoformat()
    states = [h["new_state"] for h in store.get_state_history("exec-1", db_path=db_path)]
    assert states == ["pending", "accepted"]


# --- record_order_result (OrderResult convenience path) ----------------------


def make_result(state, *, filled="0", fills=(), broker_order_id="B-1", updated_at=None, **overrides):
    fills = tuple(fills)
    kwargs = dict(
        client_order_id="client-exec-1",
        broker_order_id=broker_order_id,
        state=state,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        requested_quantity=Decimal("10"),
        filled_quantity=Decimal(filled),
        remaining_quantity=Decimal("10") - Decimal(filled),
        requested_price=Decimal("2400.00"),
        average_fill_price=None if not fills else Decimal("2400.00"),
        updated_at_utc=updated_at or at(3),
        fills=fills,
    )
    kwargs.update(overrides)
    return OrderResult(**kwargs)


def test_record_order_result_persists_state_and_broker_id_from_the_snapshot(db_path):
    create_order(db_path)
    store.mark_submitting("exec-1", reason="submit attempt", changed_at_utc=at(1), db_path=db_path)
    result = make_result(OrderState.ACCEPTED)
    row = store.record_order_result("exec-1", result, reason="submit returned", db_path=db_path)
    assert row["order_state"] == OrderState.ACCEPTED.value
    assert row["broker_order_id"] == "B-1"
    assert row["updated_at_utc"] == at(3).isoformat()


def test_record_order_result_rejects_a_snapshot_for_another_client_order(db_path):
    create_order(db_path)
    store.mark_submitting("exec-1", reason="submit attempt", changed_at_utc=at(1), db_path=db_path)
    result = make_result(OrderState.ACCEPTED, client_order_id="client-someone-else")
    with pytest.raises(ValueError):
        store.record_order_result("exec-1", result, reason="submit returned", db_path=db_path)


def test_record_order_result_enforces_the_full_update_contract_when_given_both_snapshots(db_path):
    create_order(db_path)
    store.mark_submitting("exec-1", reason="submit attempt", changed_at_utc=at(1), db_path=db_path)
    fill = Fill(fill_id="f-1", price=Decimal("2400"), quantity=Decimal("3"), filled_at_utc=at(2))
    previous = make_result(OrderState.PARTIALLY_FILLED, filled="3", fills=(fill,), updated_at=at(2))
    store.record_order_result("exec-1", previous, reason="partial", db_path=db_path)
    # content-level violation: the previously observed fill disappears
    regressed = make_result(OrderState.PARTIALLY_FILLED, filled="3", fills=(
        Fill(fill_id="f-2", price=Decimal("2400"), quantity=Decimal("3"), filled_at_utc=at(3)),
    ), updated_at=at(4))
    with pytest.raises(ValueError):
        store.record_order_result(
            "exec-1", regressed, reason="poll", previous_result=previous, db_path=db_path
        )


# --- restart / restore -------------------------------------------------------


def test_everything_restores_after_the_db_is_reopened(db_path):
    create_order(db_path, "exec-1")
    store.mark_submitting("exec-1", reason="submit attempt", changed_at_utc=at(1), db_path=db_path)
    store.record_order_state(
        "exec-1",
        OrderState.ACCEPTED,
        reason="broker accepted",
        broker_order_id="B-1",
        changed_at_utc=at(2),
        db_path=db_path,
    )
    create_order(db_path, "exec-2")
    store.mark_submitting("exec-2", reason="submit attempt", changed_at_utc=at(1), db_path=db_path)
    store.record_order_state(
        "exec-2", OrderState.UNKNOWN, reason="submit timed out", changed_at_utc=at(2), db_path=db_path
    )

    # every store call opens and closes its own connection, so a fresh call IS
    # a restart; init_db on the reopened file must also change nothing.
    store.init_db(db_path)

    restored = store.load_execution_request("exec-1", db_path=db_path)
    assert restored == make_request("exec-1")

    row = store.get_execution_order("exec-1", db_path=db_path)
    assert row["order_state"] == OrderState.ACCEPTED.value
    assert row["broker_order_id"] == "B-1"
    assert row["last_state_reason"] == "broker accepted"
    assert row["submission_phase"] == store.PHASE_SUBMITTED

    history = store.get_state_history("exec-1", db_path=db_path)
    assert [(h["previous_state"], h["new_state"], h["reason"]) for h in history] == [
        (None, "pending", "submit attempt"),
        ("pending", "accepted", "broker accepted"),
    ]

    unknown = store.list_unknown_orders(db_path=db_path)
    assert [row["execution_request_id"] for row in unknown] == ["exec-2"]


# --- read/update side-effect guarantees --------------------------------------


def test_reads_on_a_missing_db_return_empty_and_create_nothing(db_path):
    assert store.get_execution_order("exec-1", db_path=db_path) is None
    assert store.load_execution_request("exec-1", db_path=db_path) is None
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
    assert not db_path.exists()
