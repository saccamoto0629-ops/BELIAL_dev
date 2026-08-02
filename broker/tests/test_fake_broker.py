"""Issue #73: FakeBroker — deterministic scripted scenarios, no external I/O.

Covers the required scenario matrix: full fill / no fill / partial fill /
reject / timeout / communication failure / cancel success / cancel failure /
price deviation / order-position-account queries / invalid input & invalid
state transitions / no external communication.
"""

import ast
import pathlib
import socket
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from broker import broker_models as m
from broker import fake_broker as fb

PACKAGE_DIR = pathlib.Path(fb.__file__).resolve().parent

UTC_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_request(client_order_id="c-1", *, order_type=m.OrderType.LIMIT,
                 limit_price=Decimal("2400.00"), quantity=Decimal("10")):
    return m.OrderRequest(
        client_order_id=client_order_id,
        symbol="XAUUSD",
        side=m.OrderSide.BUY,
        order_type=order_type,
        quantity=quantity,
        limit_price=limit_price if order_type is m.OrderType.LIMIT else None,
        created_at_utc=UTC_NOW,
    )


# --- 正常約定 (immediate full fill) ------------------------------------------

def test_immediate_full_fill_at_the_limit_price_by_default():
    broker = fb.FakeBroker()
    result = broker.submit_order(make_request())
    assert result.state is m.OrderState.FILLED
    assert result.filled_quantity == Decimal("10")
    assert result.remaining_quantity == Decimal("0")
    assert result.average_fill_price == Decimal("2400.00")
    assert result.broker_order_id == "FAKE-1"
    assert result.client_order_id == "c-1"
    assert len(result.fills) == 1


def test_market_order_fills_at_the_configured_default_price():
    broker = fb.FakeBroker(default_fill_price=Decimal("2405"))
    result = broker.submit_order(make_request(order_type=m.OrderType.MARKET))
    assert result.state is m.OrderState.FILLED
    assert result.average_fill_price == Decimal("2405")
    assert result.requested_price is None  # market: no requested price


def test_fake_broker_is_deterministic_run_to_run():
    def run():
        broker = fb.FakeBroker()
        broker.script_order("c-1", fb.FillImmediately(price=Decimal("2401")))
        return broker.submit_order(make_request())

    assert run() == run()


# --- 未約定 (accepted, nothing filled) ---------------------------------------

def test_stay_pending_leaves_the_order_accepted_and_unfilled():
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.StayPending())
    result = broker.submit_order(make_request())
    assert result.state is m.OrderState.ACCEPTED
    assert result.filled_quantity == Decimal("0")
    assert result.remaining_quantity == Decimal("10")
    assert result.fills == ()
    assert result.average_fill_price is None
    assert result.broker_order_id is not None


# --- 部分約定 (partial fill) --------------------------------------------------

def test_partial_fill_keeps_quantities_and_fill_details():
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.PartiallyFill(fills=(
        (Decimal("2400"), Decimal("3")),
        (Decimal("2401"), Decimal("1")),
    )))
    result = broker.submit_order(make_request())
    assert result.state is m.OrderState.PARTIALLY_FILLED
    assert result.requested_quantity == Decimal("10")
    assert result.filled_quantity == Decimal("4")
    assert result.remaining_quantity == Decimal("6")
    assert [f.quantity for f in result.fills] == [Decimal("3"), Decimal("1")]
    # quantity-weighted average of the fill details
    assert result.average_fill_price == Decimal("2400.25")


def test_partial_fill_script_must_be_strictly_partial():
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.PartiallyFill(fills=((Decimal("2400"), Decimal("10")),)))
    with pytest.raises(m.BrokerInvalidRequestError):
        broker.submit_order(make_request())  # total == requested: not partial


# --- 拒否 (definitive broker rejection) --------------------------------------

def test_rejected_order_is_a_result_state_with_a_reason_not_an_exception():
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.RejectOrder(reason="insufficient margin"))
    result = broker.submit_order(make_request())
    assert result.state is m.OrderState.REJECTED
    assert result.reject_reason == "insufficient margin"
    assert result.filled_quantity == Decimal("0")
    # a definitive rejection is queryable afterwards
    assert broker.get_order("c-1").state is m.OrderState.REJECTED


# --- タイムアウト (outcome unknown, NOT a rejection) --------------------------

def test_timeout_raises_immediately_and_leaves_the_order_unknown():
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.TimeoutOrder())
    with pytest.raises(m.BrokerTimeoutError) as exc_info:
        broker.submit_order(make_request())
    assert exc_info.value.code is m.BrokerErrorCode.TIMEOUT
    assert exc_info.value.client_order_id == "c-1"
    # The broker MAY hold the order: state is UNKNOWN — neither "not placed"
    # nor REJECTED.
    after = broker.get_order("c-1")
    assert after.state is m.OrderState.UNKNOWN
    assert after.state is not m.OrderState.REJECTED


def test_timeout_and_rejection_are_distinguishable():
    broker = fb.FakeBroker()
    broker.script_order("t-1", fb.TimeoutOrder())
    broker.script_order("r-1", fb.RejectOrder())
    with pytest.raises(m.BrokerTimeoutError):
        broker.submit_order(make_request("t-1"))
    rejected = broker.submit_order(make_request("r-1"))
    assert broker.get_order("t-1").state is m.OrderState.UNKNOWN
    assert rejected.state is m.OrderState.REJECTED


# --- 通信失敗 (request never reached the broker) ------------------------------

def test_communication_failure_raises_and_records_no_order():
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.FailCommunication())
    with pytest.raises(m.BrokerCommunicationError) as exc_info:
        broker.submit_order(make_request())
    assert exc_info.value.code is m.BrokerErrorCode.COMMUNICATION_FAILURE
    with pytest.raises(m.BrokerOrderNotFoundError):
        broker.get_order("c-1")


def test_communication_failure_code_differs_from_timeout_code():
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.FailCommunication())
    broker.script_order("c-2", fb.TimeoutOrder())
    with pytest.raises(m.BrokerError) as comm:
        broker.submit_order(make_request("c-1"))
    with pytest.raises(m.BrokerError) as tout:
        broker.submit_order(make_request("c-2"))
    assert comm.value.code is not tout.value.code


# --- キャンセル成功 / 失敗 ----------------------------------------------------

def test_cancel_success_on_an_accepted_order():
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.StayPending())
    broker.submit_order(make_request())
    cancelled = broker.cancel_order("c-1")
    assert cancelled.state is m.OrderState.CANCELLED
    assert cancelled.filled_quantity == Decimal("0")
    assert broker.get_order("c-1").state is m.OrderState.CANCELLED


def test_cancel_of_a_partially_filled_order_keeps_the_executed_part():
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.PartiallyFill(fills=((Decimal("2400"), Decimal("4")),)))
    broker.submit_order(make_request())
    cancelled = broker.cancel_order("c-1")
    assert cancelled.state is m.OrderState.CANCELLED
    assert cancelled.filled_quantity == Decimal("4")
    assert cancelled.remaining_quantity == Decimal("6")
    assert len(cancelled.fills) == 1


def test_scripted_cancel_failure_raises_and_leaves_state_unchanged():
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.StayPending())
    broker.script_cancel("c-1", fb.FailCancellation(reason="broker refused"))
    broker.submit_order(make_request())
    with pytest.raises(m.BrokerCancellationError) as exc_info:
        broker.cancel_order("c-1")
    assert exc_info.value.code is m.BrokerErrorCode.CANCELLATION_FAILURE
    assert broker.get_order("c-1").state is m.OrderState.ACCEPTED


def test_cancelling_a_filled_order_fails_with_cancellation_error():
    broker = fb.FakeBroker()
    broker.submit_order(make_request())  # default: immediate full fill
    with pytest.raises(m.BrokerCancellationError):
        broker.cancel_order("c-1")
    assert broker.get_order("c-1").state is m.OrderState.FILLED


def test_cancelling_an_unknown_order_id_raises_not_found():
    broker = fb.FakeBroker()
    with pytest.raises(m.BrokerOrderNotFoundError) as exc_info:
        broker.cancel_order("nope")
    assert exc_info.value.code is m.BrokerErrorCode.NOT_FOUND


# --- 約定価格の乖離 (price deviation) ----------------------------------------

def test_requested_price_and_actual_fill_price_are_held_separately():
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.FillImmediately(price=Decimal("2400.75")))
    result = broker.submit_order(make_request(limit_price=Decimal("2400.00")))
    assert result.requested_price == Decimal("2400.00")
    assert result.average_fill_price == Decimal("2400.75")
    assert result.price_deviation == Decimal("0.75")


def test_zero_deviation_when_filled_at_the_requested_price():
    broker = fb.FakeBroker()
    result = broker.submit_order(make_request())
    assert result.price_deviation == Decimal("0")


# --- 注文照会 -----------------------------------------------------------------

def test_get_order_returns_the_latest_snapshot():
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.StayPending())
    submitted = broker.submit_order(make_request())
    assert broker.get_order("c-1") == submitted
    broker.cancel_order("c-1")
    assert broker.get_order("c-1").state is m.OrderState.CANCELLED


def test_get_order_for_an_unknown_id_raises_not_found():
    broker = fb.FakeBroker()
    with pytest.raises(m.BrokerOrderNotFoundError):
        broker.get_order("missing")


# --- ポジション照会 -----------------------------------------------------------

def test_get_positions_returns_the_injected_snapshots():
    broker = fb.FakeBroker()
    assert broker.get_positions() == ()
    position = m.PositionInfo(
        symbol="XAUUSD", side=m.PositionSide.LONG, quantity=Decimal("2"),
        average_entry_price=Decimal("2400"), retrieved_at_utc=UTC_NOW,
    )
    broker.set_positions([position])
    assert broker.get_positions() == (position,)


# --- 口座状態照会 -------------------------------------------------------------

def test_get_account_state_returns_the_injected_state():
    broker = fb.FakeBroker()
    default_state = broker.get_account_state()
    assert default_state.currency == "USD"
    injected = m.AccountState(
        account_id="acct-9", currency="JPY", balance=Decimal("5000000"),
        equity=Decimal("4990000"), margin_available=Decimal("4000000"),
        retrieved_at_utc=UTC_NOW,
    )
    broker.set_account_state(injected)
    assert broker.get_account_state() == injected


# --- 不正入力 / 不正な状態遷移 ------------------------------------------------

def test_duplicate_client_order_id_is_an_invalid_request():
    broker = fb.FakeBroker()
    broker.submit_order(make_request())
    with pytest.raises(m.BrokerInvalidRequestError) as exc_info:
        broker.submit_order(make_request())
    assert exc_info.value.code is m.BrokerErrorCode.INVALID_REQUEST


def test_submitting_something_that_is_not_an_order_request_is_invalid():
    broker = fb.FakeBroker()
    with pytest.raises(m.BrokerInvalidRequestError):
        broker.submit_order({"symbol": "XAUUSD"})


def test_cancel_after_cancel_is_an_invalid_transition_and_fails():
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.StayPending())
    broker.submit_order(make_request())
    broker.cancel_order("c-1")
    with pytest.raises(m.BrokerCancellationError):
        broker.cancel_order("c-1")


def test_market_fill_without_any_price_source_is_a_scripting_error():
    broker = fb.FakeBroker()  # no default_fill_price
    with pytest.raises(m.BrokerInvalidRequestError):
        broker.submit_order(make_request(order_type=m.OrderType.MARKET))


# --- Scenario 構築時検証 (PR #87) ---------------------------------------------

@pytest.mark.parametrize(
    "price",
    [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), 2400.5],
)
def test_fill_immediately_rejects_invalid_prices_at_construction(price):
    with pytest.raises(m.BrokerInvalidRequestError):
        fb.FillImmediately(price=price)


def test_fill_immediately_accepts_none_and_positive_decimal():
    assert fb.FillImmediately().price is None
    assert fb.FillImmediately(price=Decimal("2400.5")).price == Decimal("2400.5")


@pytest.mark.parametrize(
    "fills",
    [
        (),                                            # empty
        [(Decimal("2400"), Decimal("1"))],             # list, not tuple
        ((Decimal("0"), Decimal("1")),),               # price 0
        ((Decimal("2400"), Decimal("0")),),            # quantity 0
        ((Decimal("2400"), Decimal("-1")),),           # negative quantity
        ((Decimal("2400"),),),                         # not a 2-tuple
        (("2400", Decimal("1")),),                     # non-Decimal price
        ((Decimal("2400"), 1.0),),                     # float quantity
        (object(),),                                   # garbage entry
    ],
)
def test_partially_fill_rejects_invalid_fills_at_construction(fills):
    with pytest.raises(m.BrokerInvalidRequestError):
        fb.PartiallyFill(fills=fills)


@pytest.mark.parametrize("reason", ["", " ", "   "])
def test_reject_order_and_fail_cancellation_require_a_reason(reason):
    with pytest.raises(m.BrokerInvalidRequestError):
        fb.RejectOrder(reason=reason)
    with pytest.raises(m.BrokerInvalidRequestError):
        fb.FailCancellation(reason=reason)


# --- 設定APIの検証 (PR #87) ---------------------------------------------------

def test_script_order_rejects_empty_id_and_wrong_script_type():
    broker = fb.FakeBroker()
    with pytest.raises(m.BrokerInvalidRequestError):
        broker.script_order("", fb.StayPending())
    with pytest.raises(m.BrokerInvalidRequestError):
        broker.script_order("c-1", object())
    with pytest.raises(m.BrokerInvalidRequestError):
        broker.script_order("c-1", fb.FailCancellation())  # cancel script on submit side


def test_script_cancel_rejects_empty_id_and_wrong_script_type():
    broker = fb.FakeBroker()
    with pytest.raises(m.BrokerInvalidRequestError):
        broker.script_cancel("", fb.FailCancellation())
    with pytest.raises(m.BrokerInvalidRequestError):
        broker.script_cancel("c-1", object())
    with pytest.raises(m.BrokerInvalidRequestError):
        broker.script_cancel("c-1", fb.StayPending())  # submit script on cancel side


def test_a_rejected_cancel_script_cannot_be_silently_ignored():
    # Registering an invalid cancel script fails eagerly, so it can never be
    # "ignored" at cancel time and let the cancel succeed by accident.
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.StayPending())
    broker.submit_order(make_request())
    with pytest.raises(m.BrokerInvalidRequestError):
        broker.script_cancel("c-1", object())
    # the bad script was NOT registered; a properly scripted failure still works
    broker.script_cancel("c-1", fb.FailCancellation())
    with pytest.raises(m.BrokerCancellationError):
        broker.cancel_order("c-1")


def test_set_positions_rejects_non_position_entries_and_wrong_container():
    broker = fb.FakeBroker()
    with pytest.raises(m.BrokerInvalidRequestError):
        broker.set_positions([object()])
    with pytest.raises(m.BrokerInvalidRequestError):
        broker.set_positions("not-a-collection")
    assert broker.get_positions() == ()  # nothing was applied


def test_set_account_state_rejects_non_account_state():
    broker = fb.FakeBroker()
    with pytest.raises(m.BrokerInvalidRequestError):
        broker.set_account_state(object())
    assert broker.get_account_state().currency == "USD"  # default untouched


def test_constructor_validates_default_script_and_price():
    with pytest.raises(m.BrokerInvalidRequestError):
        fb.FakeBroker(default_script=object())
    with pytest.raises(m.BrokerInvalidRequestError):
        fb.FakeBroker(default_fill_price=Decimal("0"))


def test_constructor_rejects_a_non_callable_clock():
    with pytest.raises(m.BrokerInvalidRequestError):
        fb.FakeBroker(clock=object())
    # a callable clock is accepted and actually used
    broker = fb.FakeBroker(clock=lambda: UTC_NOW)
    result = broker.submit_order(make_request())
    assert result.updated_at_utc == UTC_NOW


# --- cancel と状態遷移の境界 (PR #87) -----------------------------------------

def test_cancel_of_an_unknown_state_order_resolves_it_to_cancelled():
    # UNKNOWN -> CANCELLED is an allowed resolution of the unconfirmed state.
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.TimeoutOrder())
    with pytest.raises(m.BrokerTimeoutError):
        broker.submit_order(make_request())
    cancelled = broker.cancel_order("c-1")
    assert cancelled.state is m.OrderState.CANCELLED


@pytest.mark.parametrize(
    "script",
    [fb.RejectOrder(reason="no"), None],  # None -> default immediate fill
)
def test_cancel_of_any_terminal_state_fails(script):
    broker = fb.FakeBroker()
    if script is not None:
        broker.script_order("c-1", script)
    broker.submit_order(make_request())
    with pytest.raises(m.BrokerCancellationError):
        broker.cancel_order("c-1")


# --- 外部通信・副作用が発生しないこと -----------------------------------------

FORBIDDEN_IMPORTS = {
    "requests", "urllib", "urllib3", "http", "http.client", "socket",
    "aiohttp", "httpx", "boto3", "botocore",     # network / S3
    "sqlite3", "sqlalchemy", "shelve", "dbm",    # real DB
    "time", "asyncio", "random", "secrets",      # sleep / real waiting / randomness
    "os", "subprocess", "pathlib", "tempfile",   # filesystem / env / processes
}


@pytest.mark.parametrize(
    "module_name", ["__init__", "broker_models", "broker_interface", "fake_broker"]
)
def test_broker_modules_import_no_network_db_or_time_modules(module_name):
    source = (PACKAGE_DIR / f"{module_name}.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    hit = {name for name in imported if name in FORBIDDEN_IMPORTS or name.split(".")[0] in FORBIDDEN_IMPORTS}
    assert not hit, f"{module_name} imports forbidden modules: {sorted(hit)}"


def test_every_scenario_runs_with_sockets_disabled(monkeypatch):
    """Exercise all scripted scenarios with socket creation forced to fail —
    proof that FakeBroker performs no network I/O anywhere."""

    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted during FakeBroker test")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)

    broker = fb.FakeBroker(default_fill_price=Decimal("2400"))
    broker.script_order("fill", fb.FillImmediately(price=Decimal("2400.5")))
    broker.script_order("pend", fb.StayPending())
    broker.script_order("part", fb.PartiallyFill(fills=((Decimal("2400"), Decimal("1")),)))
    broker.script_order("rej", fb.RejectOrder())
    broker.script_order("tout", fb.TimeoutOrder())
    broker.script_order("comm", fb.FailCommunication())
    broker.script_cancel("pend", fb.FailCancellation())

    assert broker.submit_order(make_request("fill")).state is m.OrderState.FILLED
    assert broker.submit_order(make_request("pend")).state is m.OrderState.ACCEPTED
    assert broker.submit_order(make_request("part")).state is m.OrderState.PARTIALLY_FILLED
    assert broker.submit_order(make_request("rej")).state is m.OrderState.REJECTED
    with pytest.raises(m.BrokerTimeoutError):
        broker.submit_order(make_request("tout"))
    with pytest.raises(m.BrokerCommunicationError):
        broker.submit_order(make_request("comm"))
    with pytest.raises(m.BrokerCancellationError):
        broker.cancel_order("pend")
    cancelled = broker.cancel_order("part")
    assert cancelled.state is m.OrderState.CANCELLED
    broker.get_order("fill")
    broker.get_positions()
    broker.get_account_state()


def test_fake_broker_records_submitted_requests_for_assertions():
    broker = fb.FakeBroker()
    request = make_request()
    broker.submit_order(request)
    assert broker.submitted_requests == [request]


# --- connection identity (Issue #75) -----------------------------------------

def test_connection_identity_is_fixed_at_construction():
    broker = fb.FakeBroker()
    identity = broker.connection_identity()
    assert identity == m.BrokerConnectionIdentity(mode="fake", target="fake")
    # constant for the instance lifetime
    assert broker.connection_identity() == identity


def test_connection_identity_target_comes_from_constructor_config():
    broker = fb.FakeBroker(connection_target="fake-endpoint-2")
    assert broker.connection_identity() == m.BrokerConnectionIdentity(
        mode="fake", target="fake-endpoint-2"
    )


@pytest.mark.parametrize("bad_target", ["", "   ", " fake ", None, 42])
def test_invalid_connection_target_is_rejected_at_construction(bad_target):
    with pytest.raises(m.BrokerInvalidRequestError):
        fb.FakeBroker(connection_target=bad_target)


# --- Issue #77 minimal extensions: cancel-call recording / pinned broker id --

def test_fake_broker_records_every_cancel_attempt():
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.StayPending())
    broker.script_cancel("c-1", fb.FailCancellation(reason="refused"))
    broker.submit_order(make_request())
    with pytest.raises(m.BrokerCancellationError):
        broker.cancel_order("c-1")
    with pytest.raises(m.BrokerOrderNotFoundError):
        broker.cancel_order("no-such-order")
    # failed and not-found cancels are attempts too — both are recorded
    assert broker.cancel_requests == ["c-1", "no-such-order"]


@pytest.mark.parametrize(
    "script",
    [
        fb.FillImmediately(price=Decimal("2400"), broker_order_id="BRK-PINNED"),
        fb.StayPending(broker_order_id="BRK-PINNED"),
        fb.PartiallyFill(
            fills=((Decimal("2400"), Decimal("4")),), broker_order_id="BRK-PINNED"
        ),
    ],
)
def test_scripts_can_pin_the_broker_order_id(script):
    broker = fb.FakeBroker()
    broker.script_order("c-1", script)
    result = broker.submit_order(make_request())
    assert result.broker_order_id == "BRK-PINNED"


def test_unpinned_scripts_keep_the_sequential_broker_order_id():
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.StayPending())
    assert broker.submit_order(make_request()).broker_order_id == "FAKE-1"


@pytest.mark.parametrize("bad_id", ["", "   ", 42])
def test_invalid_pinned_broker_order_id_fails_at_construction(bad_id):
    with pytest.raises(m.BrokerInvalidRequestError):
        fb.StayPending(broker_order_id=bad_id)
    with pytest.raises(m.BrokerInvalidRequestError):
        fb.FillImmediately(broker_order_id=bad_id)
    with pytest.raises(m.BrokerInvalidRequestError):
        fb.PartiallyFill(fills=((Decimal("1"), Decimal("1")),), broker_order_id=bad_id)


# --- Issue #77 review r1: broker_order_id uniqueness across orders -----------

def test_same_pinned_broker_order_id_on_two_orders_rejects_the_second():
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.StayPending(broker_order_id="DUP"))
    broker.script_order("c-2", fb.FillImmediately(price=Decimal("2400"), broker_order_id="DUP"))
    broker.submit_order(make_request("c-1"))
    with pytest.raises(m.BrokerInvalidRequestError, match="already assigned"):
        broker.submit_order(make_request("c-2"))


def test_rejected_duplicate_pinned_id_leaves_no_partial_order_behind():
    broker = fb.FakeBroker()
    broker.script_order("c-1", fb.StayPending(broker_order_id="DUP"))
    broker.script_order("c-2", fb.StayPending(broker_order_id="DUP"))
    broker.submit_order(make_request("c-1"))
    with pytest.raises(m.BrokerInvalidRequestError):
        broker.submit_order(make_request("c-2"))
    # no half-registered order: c-2 is unknown to the broker...
    with pytest.raises(m.BrokerOrderNotFoundError):
        broker.get_order("c-2")
    # ...the attempt is still recorded, and c-2 can retry under a free ID
    assert [r.client_order_id for r in broker.submitted_requests] == ["c-1", "c-2"]
    broker.script_order("c-2", fb.StayPending(broker_order_id="FREE"))
    assert broker.submit_order(make_request("c-2")).broker_order_id == "FREE"


def test_sequential_ids_skip_over_a_pinned_collision():
    broker = fb.FakeBroker()
    # pin the value the sequence would produce next
    broker.script_order("c-1", fb.StayPending(broker_order_id="FAKE-1"))
    broker.submit_order(make_request("c-1"))
    result = broker.submit_order(make_request("c-2"))
    # auto-numbering shares the uniqueness set and safely advances past FAKE-1
    assert result.broker_order_id == "FAKE-2"


def test_pinning_an_already_auto_assigned_id_is_rejected():
    broker = fb.FakeBroker()
    first = broker.submit_order(make_request("c-1"))
    assert first.broker_order_id == "FAKE-1"
    broker.script_order("c-2", fb.StayPending(broker_order_id="FAKE-1"))
    with pytest.raises(m.BrokerInvalidRequestError, match="already assigned"):
        broker.submit_order(make_request("c-2"))


def test_unpinned_ids_stay_a_unique_plain_sequence():
    broker = fb.FakeBroker()
    ids = [
        broker.submit_order(make_request(f"c-{n}")).broker_order_id for n in range(1, 4)
    ]
    assert ids == ["FAKE-1", "FAKE-2", "FAKE-3"]


def test_concurrent_submits_never_share_a_broker_order_id():
    import threading

    broker = fb.FakeBroker()
    worker_count = 8
    barrier = threading.Barrier(worker_count)
    results: list[m.OrderResult | None] = [None] * worker_count

    def submit(index: int) -> None:
        request = make_request(f"c-{index}")
        barrier.wait(timeout=30)
        results[index] = broker.submit_order(request)

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assigned = [result.broker_order_id for result in results]
    assert None not in results
    assert len(set(assigned)) == worker_count
