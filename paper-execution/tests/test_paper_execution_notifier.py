"""Issue #83 (+review): transactional outbox, claim/lease, at-least-once, dry-run invariance."""

import hashlib
import json
import pathlib
import sqlite3
import sys

import pandas as pd
import pytest
import requests

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR.parent / "backtest"))
sys.path.insert(0, str(BASE_DIR.parent / "market-watch"))

import execution_profile as profiles  # noqa: E402
import execution_runner as runner  # noqa: E402
import paper_execution_notifier as notifier  # noqa: E402
import position_models as models  # noqa: E402
import position_store as store  # noqa: E402

PROFILE = profiles.ExecutionProfile(max_entry_delay_bars=3, max_hold_bars=24, fallback_stop_pct=0.01, risk_reward=2.0)
WEBHOOK = "https://discord.example.invalid/api/webhooks/123/secret-token"
MAX = notifier.MAX_DELIVERY_ATTEMPTS

T0 = "2026-07-06T18:00:00Z"
T_LATER = "2026-07-07T18:00:00Z"   # far past any lease started at T0


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(runner, "LOG_FILE", tmp_path / "logs" / "paper_execution.log")
    monkeypatch.setattr(runner, "_logger_instance", None, raising=False)
    monkeypatch.setattr(runner, "_console_only_logging", False, raising=False)
    monkeypatch.delenv(notifier.WEBHOOK_ENV_VAR, raising=False)
    runner._STOP_EVENT.clear()
    yield
    runner._STOP_EVENT.clear()


# --- fakes -------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=204):
        self.status_code = status_code


class FakeSession:
    """Records posts; returns queued responses (default 204) or raises `exc`."""

    def __init__(self, responses=None, exc=None):
        self.calls = []
        self.responses = list(responses or [])
        self.exc = exc

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.exc is not None:
            raise self.exc
        return self.responses.pop(0) if self.responses else FakeResponse()

    @property
    def bodies(self):
        return [c["json"]["content"] for c in self.calls]


# --- position fixtures -------------------------------------------------------


def _pending_record(position_id="pos-1", signal_id="sig-1", **over):
    base = dict(
        signal_id=signal_id, symbol="USDJPY", timeframe="1h", side="long", strategy_profile="p",
        profile=PROFILE, signal_candle_utc="2026-07-06T12:00:00Z",
        entry_deadline_utc="2026-07-06T16:00:00Z", position_id=position_id,
    )
    base.update(over)
    return models.create_pending_position(**base)


def _seed_pending(db, position_id="pos-1", signal_id="sig-1", **over):
    pos = _pending_record(position_id=position_id, signal_id=signal_id, **over)
    store.create_position(pos, db_path=db)
    return pos


def _open_atomic(db, position_id="pos-1"):
    return store.transition_to_open_with_notification(
        position_id, entry_price=100.0, entry_time_utc="2026-07-06T13:00:00Z",
        stop_loss_price=99.0, take_profit_price=102.0,
        max_hold_deadline_utc="2026-07-07T13:00:00Z", db_path=db,
    )


def _close_atomic(db, position_id="pos-1"):
    return store.close_open_position_with_notification(
        position_id, expected_watermark=None, new_watermark="2026-07-06T15:00:00Z",
        close_price=102.0, close_time_utc="2026-07-06T15:00:00Z", terminal_reason="take_profit",
        gross_pnl=2.0, net_pnl=1.9, r_multiple=1.9, db_path=db,
    )


def _seed_closed_with_outbox(db, position_id="pos-1", signal_id="sig-1"):
    """pending -> open -> closed via the atomic APIs (open + closed outbox rows exist)."""
    _seed_pending(db, position_id=position_id, signal_id=signal_id)
    _open_atomic(db, position_id)
    return _close_atomic(db, position_id)


def _open_position_dict(**over):
    row = dict(
        position_id="pos-1", signal_id="sig-1", symbol="USDJPY", timeframe="1h", side="long",
        strategy_profile="p", execution_profile="default", state="open", terminal_reason=None,
        entry_time_utc="2026-07-06T13:00:00Z", entry_price=100.0,
        stop_loss_price=99.0, take_profit_price=102.0, max_hold_deadline_utc="2026-07-07T13:00:00Z",
    )
    row.update(over)
    return row


def _closed_position_dict(**over):
    row = _open_position_dict(
        state="closed", terminal_reason="take_profit", close_price=102.0,
        close_time_utc="2026-07-06T15:00:00Z", gross_pnl=2.0, net_pnl=1.9, r_multiple=1.9,
    )
    row.update(over)
    return row


def _deliver(db, *, session, url=WEBHOOK, worker="worker-A", now=None, **kw):
    return notifier.deliver_outbox(
        db_path=db, webhook_url_value=url, session=session, worker_id=worker, now_utc=now, **kw
    )


# --- message bodies (incl. Event ID / at-least-once marker) ------------------


def test_open_message_has_required_fields_and_event_id():
    msg = notifier.format_event_message(notifier.EVENT_OPEN, _open_position_dict())
    assert "Paper Execution OPEN" in msg and "仮想約定" in msg
    assert "Event ID: pos-1:open" in msg
    for expected in (
        "Symbol: USDJPY", "Timeframe: 1h", "Side: long", "Signal ID: sig-1", "Position ID: pos-1",
        "Entry Time (UTC): 2026-07-06T13:00:00Z", "Entry Price: 100.00000",
        "Stop Loss: 99.00000", "Take Profit: 102.00000",
        "Max Hold Deadline (UTC): 2026-07-07T13:00:00Z", "Execution Profile: default",
    ):
        assert expected in msg
    assert notifier.PAPER_EXECUTION_NOTE in msg


def test_closed_message_has_required_fields_and_event_id():
    msg = notifier.format_event_message(notifier.EVENT_CLOSED, _closed_position_dict())
    assert "Paper Execution CLOSED" in msg and "仮想損益" in msg
    assert "Event ID: pos-1:closed" in msg
    for expected in (
        "Terminal Reason: take_profit", "Entry Time (UTC): 2026-07-06T13:00:00Z",
        "Close Time (UTC): 2026-07-06T15:00:00Z", "Entry Price: 100.00000", "Close Price: 102.00000",
        "Gross PnL: 2.00000", "Net PnL: 1.90000", "R Multiple: 1.90",
    ):
        assert expected in msg


@pytest.mark.parametrize("reason", ["stop_loss", "take_profit", "max_holding_time"])
def test_closed_message_shows_each_terminal_reason(reason):
    msg = notifier.format_event_message(notifier.EVENT_CLOSED, _closed_position_dict(terminal_reason=reason))
    assert f"Terminal Reason: {reason}" in msg


def test_short_side_rendered():
    msg = notifier.format_event_message(notifier.EVENT_OPEN, _open_position_dict(side="short"))
    assert "Side: short" in msg


def test_expired_and_cancelled_messages():
    expired = _open_position_dict(state="expired", terminal_reason="entry_timeout",
                                  entry_price=None, entry_time_utc=None)
    msg = notifier.format_event_message(notifier.EVENT_EXPIRED, expired)
    assert "EXPIRED" in msg and "仮想" in msg and "Event ID: pos-1:expired" in msg
    assert "State: expired" in msg and "Terminal Reason: entry_timeout" in msg

    cancelled = _open_position_dict(state="cancelled", terminal_reason="invalid_input", side="", symbol="")
    msg = notifier.format_event_message(notifier.EVENT_CANCELLED, cancelled)
    assert "CANCELLED" in msg and "Event ID: pos-1:cancelled" in msg
    assert "Symbol: -" in msg and "Side: -" in msg      # blank -> placeholder, no exception


def test_none_values_render_safely_never_raise():
    sparse = {"position_id": "pos-x"}
    for event_type in sorted(store.KNOWN_NOTIFY_EVENTS):
        msg = notifier.format_event_message(event_type, sparse)
        assert "None" not in msg and ": -" in msg


def test_unknown_event_type_raises():
    with pytest.raises(ValueError):
        notifier.format_event_message("promoted", _open_position_dict())


def test_unicode_payload_is_json_serializable_and_capped():
    msg = notifier.format_event_message(notifier.EVENT_OPEN, _open_position_dict(symbol="ドル円🚀"))
    payload = notifier.build_discord_payload(msg)
    assert "ドル円🚀" in payload["content"]
    json.dumps(payload)
    assert len(notifier.build_discord_payload("あ" * 3000)["content"]) == notifier.DISCORD_CONTENT_LIMIT


# --- event_key ---------------------------------------------------------------


def test_event_key_deterministic_and_unique():
    assert store.notification_event_key("p1", "open") == store.notification_event_key("p1", "open")
    keys = {store.notification_event_key(p, e) for p in ("p1", "p2") for e in sorted(store.KNOWN_NOTIFY_EVENTS)}
    assert len(keys) == 2 * len(store.KNOWN_NOTIFY_EVENTS)


def test_event_key_rejects_bad_input():
    with pytest.raises(ValueError):
        store.notification_event_key("", "open")
    with pytest.raises(ValueError):
        store.notification_event_key("p1", "bogus")


# --- 1/2: atomic transition + enqueue, rollback on enqueue failure -----------


def test_each_transition_commits_with_its_outbox_row(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db, "pos-open", "sig-open")
    _open_atomic(db, "pos-open")
    _close_atomic(db, "pos-open")
    _seed_pending(db, "pos-exp", "sig-exp")
    store.transition_pending_terminal_with_notification(
        "pos-exp", state="expired", terminal_reason="entry_timeout", db_path=db)
    cancelled = models.create_cancelled_position(
        signal_id="sig-can", symbol="", timeframe="", side="", strategy_profile="",
        profile=PROFILE, signal_candle_utc="", entry_deadline_utc="", position_id="pos-can")
    assert store.create_cancelled_position_with_notification(cancelled, db_path=db) is True

    keys = {r["event_key"] for r in store.list_undelivered_notifications(db_path=db)}
    assert keys == {"pos-open:open", "pos-open:closed", "pos-exp:expired", "pos-can:cancelled"}
    for row in store.list_undelivered_notifications(db_path=db):
        assert row["delivery_status"] == "pending" and row["attempt_count"] == 0


def test_pending_creation_enqueues_nothing(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    assert store.list_undelivered_notifications(db_path=db) == []
    assert store.count_notifications_by_status(db_path=db) == {}


@pytest.mark.parametrize("transition", ["open", "expired", "closed", "cancelled"])
def test_enqueue_failure_rolls_back_position_change(tmp_path, monkeypatch, transition):
    db = tmp_path / "p.sqlite3"
    if transition == "closed":
        _seed_pending(db)
        store.transition_to_open(   # plain open (no outbox row): isolates the close enqueue
            "pos-1", entry_price=100.0, entry_time_utc="2026-07-06T13:00:00Z",
            stop_loss_price=99.0, take_profit_price=102.0,
            max_hold_deadline_utc="2026-07-07T13:00:00Z", db_path=db,
        )
        before_state = "open"
    elif transition != "cancelled":
        _seed_pending(db)
        before_state = "pending"

    def boom(connection, position_id, event_type, now):
        raise sqlite3.OperationalError("enqueue failed (injected)")

    monkeypatch.setattr(store, "_enqueue_notification", boom)
    with pytest.raises(sqlite3.OperationalError):
        if transition == "open":
            _open_atomic(db)
        elif transition == "expired":
            store.transition_pending_terminal_with_notification(
                "pos-1", state="expired", terminal_reason="entry_timeout", db_path=db)
        elif transition == "closed":
            _close_atomic(db)
        else:
            record = models.create_cancelled_position(
                signal_id="sig-can", symbol="", timeframe="", side="", strategy_profile="",
                profile=PROFILE, signal_candle_utc="", entry_deadline_utc="", position_id="pos-can")
            store.create_cancelled_position_with_notification(record, db_path=db)
    monkeypatch.undo()

    if transition == "cancelled":
        assert store.get_position("pos-can", db_path=db) is None     # position INSERT rolled back
    else:
        assert store.get_position("pos-1", db_path=db)["state"] == before_state  # transition rolled back
    # and no orphan outbox row either
    with sqlite3.connect(db) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {store.NOTIFICATIONS_TABLE}").fetchone()[0] == 0


def test_cas_conflict_enqueues_nothing(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    store.transition_pending_terminal(  # plain terminal transition, no notification
        "pos-1", state="expired", terminal_reason="entry_timeout", db_path=db)
    with pytest.raises(store.PositionConcurrentModificationError):
        _open_atomic(db)   # CAS loses: position already terminal
    with sqlite3.connect(db) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {store.NOTIFICATIONS_TABLE}").fetchone()[0] == 0


def test_duplicate_cancelled_signal_enqueues_nothing_extra(tmp_path):
    db = tmp_path / "p.sqlite3"
    record = models.create_cancelled_position(
        signal_id="dup", symbol="", timeframe="", side="", strategy_profile="",
        profile=PROFILE, signal_candle_utc="", entry_deadline_utc="", position_id="pos-a")
    assert store.create_cancelled_position_with_notification(record, db_path=db) is True
    second = models.create_cancelled_position(
        signal_id="dup", symbol="", timeframe="", side="", strategy_profile="",
        profile=PROFILE, signal_candle_utc="", entry_deadline_utc="", position_id="pos-b")
    assert store.create_cancelled_position_with_notification(second, db_path=db) is False
    assert len(store.list_undelivered_notifications(db_path=db)) == 1


def test_enqueue_is_idempotent_and_never_clobbers_delivery_state(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_closed_with_outbox(db)
    _deliver(db, session=FakeSession())      # both rows delivered
    assert store.get_notification("pos-1:closed", db_path=db)["delivery_status"] == "success"
    assert store.record_notification_event("pos-1", "closed", db_path=db) is False  # no re-queue
    assert store.get_notification("pos-1:closed", db_path=db)["delivery_status"] == "success"


# --- 3: crash after commit, before HTTP -> delivered on the next cycle -------


def test_crash_between_commit_and_http_is_recovered_next_cycle(tmp_path):
    db = tmp_path / "p.sqlite3"
    # The transition committed atomically with its outbox row; the process then
    # "died" before any HTTP work: this on-disk state IS the crash state.
    _seed_closed_with_outbox(db)
    rows = store.list_undelivered_notifications(db_path=db)
    assert {r["event_key"] for r in rows} == {"pos-1:open", "pos-1:closed"}
    # restart -> a fresh delivery pass finds and sends them
    session = FakeSession()
    res = _deliver(db, session=session)
    assert res.sent == 2 and res.delivery_failures == 0 and res.outbox_failures == 0
    assert store.list_undelivered_notifications(db_path=db) == []


# --- claim / lease (Major 2) -------------------------------------------------


def test_claim_is_single_winner_across_workers(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_closed_with_outbox(db)
    claimed_a = store.claim_notifications(
        worker_id="A", lease_seconds=600, batch_limit=10, max_attempts=MAX, now_utc=T0, db_path=db)
    claimed_b = store.claim_notifications(
        worker_id="B", lease_seconds=600, batch_limit=10, max_attempts=MAX, now_utc=T0, db_path=db)
    assert len(claimed_a) == 2 and claimed_b == []   # B cannot claim A's rows


def test_two_workers_one_http_post(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    _open_atomic(db)
    session_a, session_b = FakeSession(), FakeSession()
    res_a = _deliver(db, session=session_a, worker="A")
    res_b = _deliver(db, session=session_b, worker="B")   # concurrent second runner
    assert res_a.sent == 1 and res_b.sent == 0
    assert len(session_a.calls) + len(session_b.calls) == 1   # exactly one POST


def test_expired_lease_is_reclaimable_and_stale_worker_cannot_settle(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    _open_atomic(db)
    claimed = store.claim_notifications(
        worker_id="A", lease_seconds=60, batch_limit=10, max_attempts=MAX, now_utc=T0, db_path=db)
    assert len(claimed) == 1
    # within the lease: not claimable by B
    assert store.claim_notifications(
        worker_id="B", lease_seconds=60, batch_limit=10, max_attempts=MAX,
        now_utc="2026-07-06T18:00:30Z", db_path=db) == []
    # after the lease: B recovers the row (worker A crashed)
    reclaimed = store.claim_notifications(
        worker_id="B", lease_seconds=60, batch_limit=10, max_attempts=MAX, now_utc=T_LATER, db_path=db)
    assert [r["event_key"] for r in reclaimed] == ["pos-1:open"]
    # the stale worker A must not clobber B's claim
    with pytest.raises(store.NotificationClaimError):
        store.finish_notification_delivery(
            "pos-1:open", worker_id="A", success=True, max_attempts=MAX, db_path=db)
    finished = store.finish_notification_delivery(
        "pos-1:open", worker_id="B", success=True, max_attempts=MAX, db_path=db)
    assert finished["delivery_status"] == "success"


def test_success_and_dead_letter_rows_are_never_claimed(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    _open_atomic(db)
    _seed_pending(db, "pos-2", "sig-2")
    store.transition_pending_terminal_with_notification(
        "pos-2", state="expired", terminal_reason="entry_timeout", db_path=db)
    # settle pos-1 as success; drive pos-2 to dead_letter
    store.claim_notifications(worker_id="A", lease_seconds=600, batch_limit=10, max_attempts=MAX, now_utc=T0, db_path=db)
    store.finish_notification_delivery("pos-1:open", worker_id="A", success=True, max_attempts=MAX, db_path=db)
    store.finish_notification_delivery("pos-2:expired", worker_id="A", success=False, error="x",
                                       max_attempts=1, db_path=db)   # cap 1 -> dead_letter
    assert store.get_notification("pos-2:expired", db_path=db)["delivery_status"] == "dead_letter"
    assert store.claim_notifications(
        worker_id="B", lease_seconds=600, batch_limit=10, max_attempts=MAX, now_utc=T_LATER, db_path=db) == []


def test_claim_respects_batch_limit_oldest_first(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.init_db(db)
    for i in range(5):
        store.record_notification_event(f"pos-{i}", "cancelled", created_at=f"2026-07-06T1{i}:00:00Z", db_path=db)
    claimed = store.claim_notifications(
        worker_id="A", lease_seconds=600, batch_limit=3, max_attempts=MAX, now_utc=T0, db_path=db)
    assert [r["event_key"] for r in claimed] == ["pos-0:cancelled", "pos-1:cancelled", "pos-2:cancelled"]


# --- 4: at-least-once (HTTP success, success record fails) -------------------


def test_success_record_failure_redelivers_with_same_event_id(tmp_path, monkeypatch, caplog):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    _open_atomic(db)
    real_finish = store.finish_notification_delivery
    fail_once = {"armed": True}

    def flaky_finish(event_key, **kw):
        if kw.get("success") and fail_once["armed"]:
            fail_once["armed"] = False
            raise sqlite3.OperationalError("disk I/O error (injected)")
        return real_finish(event_key, **kw)

    monkeypatch.setattr(store, "finish_notification_delivery", flaky_finish)
    monkeypatch.setattr(notifier.store, "finish_notification_delivery", flaky_finish)

    session = FakeSession()
    with caplog.at_level("ERROR", logger="paper_execution"):
        res1 = _deliver(db, session=session, now=T0)
    assert len(session.calls) == 1                      # POST happened
    assert res1.sent == 0 and res1.delivery_failures == 1
    row = store.get_notification("pos-1:open", db_path=db)
    assert row["delivery_status"] == "processing"       # stays claimed until the lease expires
    assert any("DUPLICATE" in r.message and "at-least-once" in r.message for r in caplog.records)

    # after the lease expires, the row is re-claimed and re-POSTed
    res2 = _deliver(db, session=session, now=T_LATER)
    assert res2.sent == 1
    assert len(session.calls) == 2
    id_lines = [line for body in session.bodies for line in body.splitlines() if line.startswith("Event ID:")]
    assert id_lines == ["Event ID: pos-1:open", "Event ID: pos-1:open"]   # duplicates identifiable
    assert store.get_notification("pos-1:open", db_path=db)["delivery_status"] == "success"


# --- delivery error classification (timeout / 429 / 5xx / connection) --------


@pytest.mark.parametrize("exc,expected", [
    (requests.Timeout("boom"), "timeout"),
    (requests.ConnectionError(f"failed to reach {WEBHOOK}"), "connection error"),
])
def test_deliver_classifies_transport_errors_sanitized(exc, expected):
    with pytest.raises(notifier.NotificationDeliveryError) as err:
        notifier.deliver_payload({"content": "x"}, webhook_url=WEBHOOK, session=FakeSession(exc=exc))
    assert expected in str(err.value)
    assert WEBHOOK not in str(err.value) and "secret-token" not in str(err.value)


@pytest.mark.parametrize("status,expected", [(429, "429"), (500, "HTTP 500"), (503, "HTTP 503"), (400, "HTTP 400")])
def test_deliver_classifies_http_errors(status, expected):
    with pytest.raises(notifier.NotificationDeliveryError) as err:
        notifier.deliver_payload({"content": "x"}, webhook_url=WEBHOOK,
                                 session=FakeSession(responses=[FakeResponse(status)]))
    assert expected in str(err.value)


def test_deliver_always_sets_timeout():
    session = FakeSession()
    notifier.deliver_payload({"content": "x"}, webhook_url=WEBHOOK, session=session)
    assert session.calls[0]["timeout"] == notifier.REQUEST_TIMEOUT_SECONDS


@pytest.mark.parametrize("failure", [requests.Timeout("t"), requests.ConnectionError("c"), None])
def test_failures_recorded_sanitized_and_retryable(tmp_path, failure):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    _open_atomic(db)
    session = FakeSession(exc=failure) if failure else FakeSession(responses=[FakeResponse(500)])
    res = _deliver(db, session=session, now=T0)
    assert res.delivery_failures == 1 and res.sent == 0
    row = store.get_notification("pos-1:open", db_path=db)
    assert row["delivery_status"] == "failed" and row["attempt_count"] == 1
    assert row["last_error"] and WEBHOOK not in row["last_error"]
    # retried on a later cycle once the webhook recovers
    ok = FakeSession()
    assert _deliver(db, session=ok, now=T_LATER).sent == 1


# --- 9: attempt cap -> dead_letter, continuously monitored -------------------


def test_dead_letter_after_cap_and_continuous_monitoring(tmp_path, caplog):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    _open_atomic(db)
    failing = FakeSession(exc=requests.ConnectionError("down"))
    now = T0
    for i in range(MAX):
        _deliver(db, session=failing, now=now)
        now = f"2026-07-{8 + i:02d}T18:00:00Z"     # advance past any lease
    row = store.get_notification("pos-1:open", db_path=db)
    assert row["delivery_status"] == "dead_letter" and row["attempt_count"] == MAX
    assert len(failing.calls) == MAX                              # bounded, never infinite

    # later cycles: no more claims, but the dead-letter gauge stays visible
    ok = FakeSession()
    with caplog.at_level("WARNING", logger="paper_execution"):
        res = _deliver(db, session=ok, now=T_LATER)
    assert ok.calls == [] and res.sent == 0
    assert res.dead_letter_total == 1
    assert any("dead-letter" in r.message for r in caplog.records)


def test_requeue_notification_recovers_dead_letter(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    _open_atomic(db)
    store.claim_notifications(worker_id="A", lease_seconds=600, batch_limit=10, max_attempts=1, now_utc=T0, db_path=db)
    store.finish_notification_delivery("pos-1:open", worker_id="A", success=False, error="x",
                                       max_attempts=1, db_path=db)
    assert store.get_notification("pos-1:open", db_path=db)["delivery_status"] == "dead_letter"
    requeued = store.requeue_notification("pos-1:open", db_path=db)
    assert requeued["delivery_status"] == "pending" and requeued["attempt_count"] == 0
    session = FakeSession()
    assert _deliver(db, session=session, now=T_LATER).sent == 1


# --- 10/11: webhook unset -> queued; batch limit -----------------------------


def test_webhook_unset_queues_without_claim_or_attempt_then_drains(tmp_path, caplog):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    _open_atomic(db)
    session = FakeSession()
    with caplog.at_level("WARNING", logger="paper_execution"):
        res = _deliver(db, session=session, url="")
    assert res.sent == 0 and res.delivery_failures == 0 and session.calls == []
    row = store.get_notification("pos-1:open", db_path=db)
    assert row["delivery_status"] == "pending"
    assert row["attempt_count"] == 0 and row["claimed_by"] is None   # nothing claimed/consumed
    assert any("stay queued" in r.message for r in caplog.records)
    # URL configured later -> backlog drains
    assert _deliver(db, session=session, url=WEBHOOK).sent == 1


def test_delivery_batch_limit_per_cycle(tmp_path):
    db = tmp_path / "p.sqlite3"
    for i in range(5):
        _seed_pending(db, f"pos-{i}", f"sig-{i}")
        store.transition_pending_terminal_with_notification(
            f"pos-{i}", state="expired", terminal_reason="entry_timeout", db_path=db)
    session = FakeSession()
    res1 = _deliver(db, session=session, batch_limit=3, now=T0)
    assert res1.sent == 3 and len(session.calls) == 3          # capped this cycle
    res2 = _deliver(db, session=session, batch_limit=3, now=T_LATER)
    assert res2.sent == 2 and len(session.calls) == 5          # backlog drains next cycle


# --- outbox failure classification (data plane vs delivery) ------------------


def test_claim_db_failure_counts_as_outbox_failure(tmp_path, monkeypatch):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    _open_atomic(db)
    monkeypatch.setattr(notifier.store, "claim_notifications",
                        lambda **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    res = _deliver(db, session=FakeSession())
    assert res.outbox_failures == 1 and res.delivery_failures == 0


# --- runner integration ------------------------------------------------------


def _signal(signal_id="sig-1", side="long", status="paper_only"):
    return {
        "signal_id": signal_id, "symbol": "USDJPY", "timeframe": "1h", "side": side,
        "strategy_profile": "market_bias_macd_alignment_v1",
        "target_candle_utc": "2026-07-06T12:00:00Z", "status": status,
    }


def _candles(rows, start="2026-07-06T12:00:00Z"):
    idx = pd.date_range(start, periods=len(rows), freq="1h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


NOW = pd.Timestamp("2026-07-06T18:00:00Z")
CLOSE_CANDLES = [(50, 51, 49, 50), (100.0, 100.2, 99.9, 100.1), (100.5, 102.5, 100.1, 101.0)]


def _run(db, signals, candles, now=NOW, *, session, dry_run=False, webhook=WEBHOOK):
    return runner.run_once(
        dry_run=dry_run, db_path=db, signals=signals, load_candles=lambda s, t: candles,
        now_utc=now, profile=PROFILE, chart_db_path=db.parent / "chart.sqlite3",
        signal_db_path=db.parent / "sig.sqlite3",
        notify_webhook_url=webhook, notify_session=session,
    )


def test_runner_notifies_open_and_closed_with_event_ids(tmp_path):
    db = tmp_path / "p.sqlite3"
    session = FakeSession()
    res = _run(db, [_signal()], _candles(CLOSE_CANDLES), session=session)
    assert res.failures == 0 and res.notification_failures == 0
    assert len(session.bodies) == 2
    assert "OPEN" in session.bodies[0] and "仮想約定" in session.bodies[0] and ":open" in session.bodies[0]
    assert "CLOSED" in session.bodies[1] and "take_profit" in session.bodies[1] and ":closed" in session.bodies[1]


def test_runner_restart_does_not_renotify(tmp_path):
    db = tmp_path / "p.sqlite3"
    session = FakeSession()
    _run(db, [_signal()], _candles(CLOSE_CANDLES), session=session)
    assert len(session.calls) == 2
    res2 = _run(db, [_signal()], _candles(CLOSE_CANDLES), session=session)   # "restart"
    assert res2.notification_failures == 0
    assert len(session.calls) == 2   # nothing re-sent (outbox rows settled)


def test_runner_notifies_expired_and_cancelled(tmp_path):
    db = tmp_path / "p.sqlite3"
    session = FakeSession()
    idx = pd.to_datetime(["2026-07-06T12:00:00Z", "2026-07-06T20:00:00Z"])
    candles = pd.DataFrame([(50, 51, 49, 50), (100, 101, 99, 100)],
                           columns=["open", "high", "low", "close"], index=idx)
    res = _run(db, [_signal(), _signal(signal_id="bad", status="closed")], candles,
               pd.Timestamp("2026-07-06T22:00:00Z"), session=session)
    assert res.expired == 1 and res.cancelled == 1
    joined = "\n".join(session.bodies)
    assert "EXPIRED" in joined and "entry_timeout" in joined
    assert "CANCELLED" in joined and "invalid_input" in joined


def test_runner_delivery_failure_keeps_cycle_ok_and_position_closed(tmp_path):
    db = tmp_path / "p.sqlite3"
    session = FakeSession(exc=requests.ConnectionError("down"))
    res = _run(db, [_signal()], _candles(CLOSE_CANDLES), session=session)
    assert res.failures == 0 and res.ok                     # delivery failure: NOT data-plane
    assert res.notification_failures >= 1
    assert store.list_positions(db_path=db)[0]["state"] == "closed"   # never rolled back


def test_runner_outbox_claim_failure_is_data_plane(tmp_path, monkeypatch):
    db = tmp_path / "p.sqlite3"
    monkeypatch.setattr(notifier.store, "claim_notifications",
                        lambda **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    res = _run(db, [_signal()], _candles(CLOSE_CANDLES), session=FakeSession())
    assert res.failures >= 1 and not res.ok                 # outbox guarantee broken -> failure


def test_runner_enqueue_failure_fails_cycle_then_recovers(tmp_path, monkeypatch):
    db = tmp_path / "p.sqlite3"
    session = FakeSession()
    monkeypatch.setattr(store, "_enqueue_notification",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("enqueue boom")))
    res1 = _run(db, [_signal()], _candles(CLOSE_CANDLES), session=session)
    assert res1.failures >= 1                                # atomic txn failed -> data-plane failure
    assert store.list_positions(db_path=db)[0]["state"] == "pending"   # transition rolled back
    assert session.calls == []
    monkeypatch.undo()
    res2 = _run(db, [_signal()], _candles(CLOSE_CANDLES), session=session)   # next cycle retries
    assert res2.failures == 0 and res2.opened == 1 and res2.closed == 1
    assert len(session.calls) == 2                           # open + closed delivered exactly once
    assert store.list_positions(db_path=db)[0]["state"] == "closed"


def test_runner_reports_dead_letter_gauge(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    _open_atomic(db)
    store.claim_notifications(worker_id="A", lease_seconds=600, batch_limit=10, max_attempts=1, now_utc=T0, db_path=db)
    store.finish_notification_delivery("pos-1:open", worker_id="A", success=False, error="x",
                                       max_attempts=1, db_path=db)
    res = _run(db, [], _candles([(1, 1, 1, 1)]), session=FakeSession())
    assert res.dead_letter_notifications == 1


def test_run_service_once_ok_despite_delivery_failures(monkeypatch):
    monkeypatch.setattr(runner, "run_once", lambda **k: runner.CycleResult(notification_failures=3))
    assert runner.run_service(once=True) == runner.EXIT_OK   # delivery failures never fail --once


# --- 12/13: dry-run leaves the DB byte-identical (WAL and rollback journal) --


def _fingerprint(path):
    if not path.exists():
        return None
    data = path.read_bytes()
    return (hashlib.sha256(data).hexdigest(), len(data), path.stat().st_mtime_ns)


def _db_family_fingerprints(db):
    return {
        suffix: _fingerprint(pathlib.Path(str(db) + suffix))
        for suffix in ("", "-wal", "-shm")
    }


def _dry_run(db, tmp_path):
    return runner.run_once(
        dry_run=True, db_path=db, signals=[], load_candles=lambda s, t: _candles(CLOSE_CANDLES),
        now_utc=NOW, profile=PROFILE, chart_db_path=tmp_path / "c.sqlite3",
        signal_db_path=tmp_path / "s.sqlite3",
        notify_webhook_url=WEBHOOK, notify_session=FakeSession(),
    )


def test_dry_run_wal_db_fully_unchanged(tmp_path, caplog):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    # switch to WAL and leave a live writer so -wal/-shm persist on disk
    holder = sqlite3.connect(db)
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute(f"UPDATE {store.POSITIONS_TABLE} SET updated_at_utc = updated_at_utc")
        holder.commit()
        assert pathlib.Path(str(db) + "-wal").exists() and pathlib.Path(str(db) + "-shm").exists()
        before = _db_family_fingerprints(db)

        with caplog.at_level("INFO", logger="paper_execution"):
            res = _dry_run(db, tmp_path)

        assert res.failures == 0 and res.opened == 1          # decisions still computed
        assert _db_family_fingerprints(db) == before          # db, -wal, -shm: bytes/size/mtime identical
        assert any("would notify" in r.message and "仮想約定" in r.message for r in caplog.records)
    finally:
        holder.close()


def test_dry_run_rollback_journal_db_fully_unchanged(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    before = _db_family_fingerprints(db)
    res = _dry_run(db, tmp_path)
    assert res.failures == 0 and res.opened == 1
    assert _db_family_fingerprints(db) == before
    assert store.list_positions(db_path=db)[0]["state"] == "pending"   # no transition
    with sqlite3.connect(db) as conn:                                  # no outbox rows
        assert conn.execute(f"SELECT COUNT(*) FROM {store.NOTIFICATIONS_TABLE}").fetchone()[0] == 0


def test_dry_run_missing_db_still_not_created(tmp_path):
    db = tmp_path / "absent.sqlite3"
    session = FakeSession()
    res = runner.run_once(
        dry_run=True, db_path=db, signals=[_signal()], load_candles=lambda s, t: _candles(CLOSE_CANDLES),
        now_utc=NOW, profile=PROFILE, chart_db_path=tmp_path / "c.sqlite3",
        signal_db_path=tmp_path / "s.sqlite3", notify_webhook_url=WEBHOOK, notify_session=session,
    )
    assert res.failures == 0
    assert not db.exists() and not (tmp_path / "logs").exists()
    assert session.calls == []


# --- 4 (Major): real constraint validation of the outbox table ---------------


def _user_version(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def _swap_notifications_table(db, ddl, *, rows=(), user_version=store.CURRENT_SCHEMA_VERSION):
    """Replace the outbox table with an arbitrary-DDL variant (constraint experiments)."""
    with sqlite3.connect(db) as conn:
        conn.execute(f"DROP TABLE {store.NOTIFICATIONS_TABLE}")
        conn.execute(ddl)
        for row in rows:
            cols = ", ".join(row)
            conn.execute(
                f"INSERT INTO {store.NOTIFICATIONS_TABLE} ({cols}) VALUES ({', '.join('?' for _ in row)})",
                tuple(row.values()),
            )
        conn.execute(f"PRAGMA user_version = {user_version}")


_ALL_COLS_NO_CONSTRAINTS = (
    f"CREATE TABLE {store.NOTIFICATIONS_TABLE} ("
    "event_key TEXT, position_id TEXT, event_type TEXT, delivery_status TEXT, "
    "attempt_count INTEGER, last_error TEXT, claimed_by TEXT, claimed_until_utc TEXT, "
    "notified_at_utc TEXT, created_at_utc TEXT, updated_at_utc TEXT)"
)


def _notification_row(**over):
    row = dict(
        event_key="pos-9:open", position_id="pos-9", event_type="open", delivery_status="failed",
        attempt_count=2, last_error="HTTP 500", created_at_utc="t", updated_at_utc="t",
    )
    row.update(over)
    return row


@pytest.mark.parametrize("ddl", [
    _ALL_COLS_NO_CONSTRAINTS,   # all columns, but no PK / NOT NULL / CHECKs
    # PK present,但し CHECK なし
    f"CREATE TABLE {store.NOTIFICATIONS_TABLE} (event_key TEXT PRIMARY KEY, position_id TEXT NOT NULL, "
    "event_type TEXT NOT NULL, delivery_status TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0, "
    "last_error TEXT, claimed_by TEXT, claimed_until_utc TEXT, notified_at_utc TEXT, "
    "created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL)",
])
def test_constraintless_outbox_never_current_even_with_forged_version(tmp_path, ddl):
    db = tmp_path / "p.sqlite3"
    store.init_db(db)
    _swap_notifications_table(db, ddl, rows=[_notification_row()])
    # forged user_version=CURRENT must NOT be trusted
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_LEGACY
    store.init_db(db)   # rebuilds transactionally
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_CURRENT
    kept = store.get_notification("pos-9:open", db_path=db)
    assert kept["delivery_status"] == "failed" and kept["attempt_count"] == 2   # rows preserved
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError):    # constraints now real
        conn.execute(
            f"INSERT INTO {store.NOTIFICATIONS_TABLE} (event_key, position_id, event_type, delivery_status, "
            "attempt_count, created_at_utc, updated_at_utc) VALUES ('x', 'p', 'bogus_event', 'pending', 0, 't', 't')"
        )


def test_missing_index_is_not_current_and_gets_recreated(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.init_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(f"DROP INDEX {store._NOTIFICATIONS_STATUS_INDEX}")
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_LEGACY
    store.init_db(db)
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_CURRENT


def test_behavioral_probe_rejects_unenforced_constraints(tmp_path, monkeypatch):
    db = tmp_path / "p.sqlite3"
    store.init_db(db)
    _swap_notifications_table(db, _ALL_COLS_NO_CONSTRAINTS)
    # defense-in-depth: even if the structural check were fooled, the SAVEPOINT
    # behavioral probe must catch constraints that do not actually fire.
    monkeypatch.setattr(store, "_notifications_table_object_current", lambda c: True)
    monkeypatch.setattr(store, "_notifications_index_present", lambda c: True)
    with pytest.raises(store.PositionSchemaError):
        store.ensure_schema_migrated(db)


def test_behavioral_probe_leaves_no_rows_behind(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.ensure_schema_migrated(db)    # runs the probe on a healthy DB
    with sqlite3.connect(db) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {store.NOTIFICATIONS_TABLE}").fetchone()[0] == 0


# --- 14/15/16: migration paths + rollback ------------------------------------


def test_fresh_db_is_current_version(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.init_db(db)
    assert _user_version(db) == store.CURRENT_SCHEMA_VERSION == 4   # v4: Issue #85 position_limit CHECK
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_CURRENT


def test_v1_db_upgrades_additively_preserving_positions(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.init_db(db)
    seeded = _seed_closed_with_outbox(db)
    with sqlite3.connect(db) as conn:      # devolve to v1: no outbox table at all
        conn.execute(f"DROP TABLE {store.NOTIFICATIONS_TABLE}")
        conn.execute("PRAGMA user_version = 1")
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_LEGACY
    store.init_db(db)
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_CURRENT
    assert store.get_position("pos-1", db_path=db) == seeded   # positions byte-identical (no rebuild)


_V2_PROTOTYPE_DDL = (
    f"CREATE TABLE {store.NOTIFICATIONS_TABLE} ("
    "event_key TEXT PRIMARY KEY, position_id TEXT NOT NULL, "
    "event_type TEXT NOT NULL CHECK (event_type IN ('cancelled', 'closed', 'expired', 'open')), "
    "delivery_status TEXT NOT NULL CHECK (delivery_status IN ('failed', 'pending', 'success')), "
    "attempt_count INTEGER NOT NULL DEFAULT 0, last_error TEXT, notified_at_utc TEXT, "
    "created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL)"
)


def _make_v2_prototype(db, rows):
    store.init_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(f"DROP TABLE {store.NOTIFICATIONS_TABLE}")
        conn.execute(_V2_PROTOTYPE_DDL)
        for row in rows:
            cols = ", ".join(row)
            conn.execute(
                f"INSERT INTO {store.NOTIFICATIONS_TABLE} ({cols}) VALUES ({', '.join('?' for _ in row)})",
                tuple(row.values()),
            )
        conn.execute("PRAGMA user_version = 2")


def test_v2_prototype_migrates_to_v3_preserving_history(tmp_path):
    db = tmp_path / "p.sqlite3"
    _make_v2_prototype(db, [
        dict(event_key="a:open", position_id="a", event_type="open", delivery_status="success",
             attempt_count=1, notified_at_utc="t1", created_at_utc="t0", updated_at_utc="t1"),
        dict(event_key="b:closed", position_id="b", event_type="closed", delivery_status="failed",
             attempt_count=3, last_error="HTTP 500", created_at_utc="t0", updated_at_utc="t1"),
    ])
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_LEGACY   # prototype = legacy now
    store.init_db(db)
    assert _user_version(db) == store.CURRENT_SCHEMA_VERSION
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_CURRENT
    done = store.get_notification("a:open", db_path=db)
    assert done["delivery_status"] == "success" and done["attempt_count"] == 1
    assert done["claimed_by"] is None and done["claimed_until_utc"] is None     # new columns NULL
    failed = store.get_notification("b:closed", db_path=db)
    assert failed["delivery_status"] == "failed" and failed["attempt_count"] == 3
    assert failed["last_error"] == "HTTP 500"


def test_v2_prototype_success_rows_not_resent_after_migration(tmp_path):
    db = tmp_path / "p.sqlite3"
    _make_v2_prototype(db, [
        dict(event_key="a:open", position_id="a", event_type="open", delivery_status="success",
             attempt_count=1, created_at_utc="t0", updated_at_utc="t1"),
    ])
    store.init_db(db)
    session = FakeSession()
    assert _deliver(db, session=session).sent == 0 and session.calls == []


def test_migration_rollback_on_unmappable_notification_row(tmp_path):
    db = tmp_path / "p.sqlite3"
    _make_v2_prototype(db, [])
    with sqlite3.connect(db) as conn:   # smuggle an unmappable row past the old CHECKs
        conn.execute(f"DROP TABLE {store.NOTIFICATIONS_TABLE}")
        conn.execute(_ALL_COLS_NO_CONSTRAINTS)
        conn.execute(
            f"INSERT INTO {store.NOTIFICATIONS_TABLE} (event_key, position_id, event_type, delivery_status, "
            "attempt_count, created_at_utc, updated_at_utc) VALUES ('x:bogus', 'x', 'bogus', 'pending', 0, 't', 't')"
        )
        conn.execute("PRAGMA user_version = 2")
    with pytest.raises(store.PositionMigrationError):
        store.init_db(db)
    assert _user_version(db) == 2   # rolled back: stamp unchanged
    with sqlite3.connect(db) as conn:   # legacy table + row intact
        assert conn.execute(f"SELECT COUNT(*) FROM {store.NOTIFICATIONS_TABLE}").fetchone()[0] == 1


# --- review round 2: Windows snapshot cleanup, exhausted-failed reconcile, same-cycle gauge


def _insert_notification_row(db, **over):
    """Insert a raw outbox row directly (bypasses the atomic APIs) for edge-case setup."""
    row = _notification_row(**over)
    store.init_db(db)
    with sqlite3.connect(db) as conn:
        cols = ", ".join(row)
        conn.execute(
            f"INSERT INTO {store.NOTIFICATIONS_TABLE} ({cols}) VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )


# 1. Major: read helpers must close the connection (Windows handle release) ----


def test_read_helpers_close_connection_handle(tmp_path, monkeypatch):
    """A connection left open would keep the file handle -> Windows cleanup fails.

    We can't spawn Windows here, so we assert the invariant that CAUSES WinError
    32: every connection sqlite3.connect returns on the read path must be closed.
    """
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    opened = []
    real_connect = sqlite3.connect

    class TrackedConn:
        """Transparent proxy that records whether close() was called."""

        def __init__(self, conn):
            object.__setattr__(self, "_conn", conn)
            object.__setattr__(self, "closed", False)

        def __getattr__(self, name):
            return getattr(object.__getattribute__(self, "_conn"), name)

        def __setattr__(self, name, value):
            setattr(object.__getattribute__(self, "_conn"), name, value)

        def __enter__(self):
            return object.__getattribute__(self, "_conn").__enter__()

        def __exit__(self, *a):
            return object.__getattribute__(self, "_conn").__exit__(*a)

        def close(self):
            object.__setattr__(self, "closed", True)
            object.__getattribute__(self, "_conn").close()

    def tracking_connect(*a, **k):
        conn = TrackedConn(real_connect(*a, **k))
        opened.append(conn)
        return conn

    monkeypatch.setattr(store.sqlite3, "connect", tracking_connect)
    assert store.get_position("pos-1", db_path=db)["state"] == "pending"   # _read_one
    assert len(store.list_positions_by_state("pending", db_path=db)) == 1  # _read_many
    monkeypatch.undo()
    assert opened and all(object.__getattribute__(c, "closed") for c in opened)   # every read conn closed


def test_dry_run_snapshot_tempdir_is_removed_after_reads(tmp_path, monkeypatch):
    """The dry-run snapshot TemporaryDirectory must be deletable after read helpers run."""
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    created = {}
    import tempfile as _tempfile
    real_tempdir = _tempfile.TemporaryDirectory

    class TrackingTempDir(real_tempdir):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            created["path"] = pathlib.Path(self.name)

    monkeypatch.setattr(runner.tempfile, "TemporaryDirectory", TrackingTempDir)
    before = _db_family_fingerprints(db)
    res = _dry_run(db, tmp_path)
    assert res.failures == 0 and res.opened == 1
    assert "path" in created and not created["path"].exists()   # cleanup() succeeded
    assert _db_family_fingerprints(db) == before                # original untouched


def test_snapshot_dir_removable_after_read_helpers_directly(tmp_path):
    """Directly exercise a read helper against a snapshot, then delete the temp dir."""
    import tempfile as _tempfile
    db = tmp_path / "p.sqlite3"
    _seed_closed_with_outbox(db)
    tmp = _tempfile.TemporaryDirectory(prefix="probe_")
    snap = store.snapshot_db_for_read(db, pathlib.Path(tmp.name))
    assert store.list_positions_by_state("closed", db_path=snap)[0]["position_id"] == "pos-1"
    assert store.get_notification("pos-1:open", db_path=snap) is not None
    tmp.cleanup()                                    # would raise WinError 32 if a handle leaked
    assert not pathlib.Path(tmp.name).exists()


# 2. Major: exhausted (attempt_count >= max) failed rows reconcile to dead_letter


def test_exhausted_failed_row_reconciled_to_dead_letter_at_claim(tmp_path):
    db = tmp_path / "p.sqlite3"
    _insert_notification_row(db, event_key="old:closed", position_id="old", event_type="closed",
                             delivery_status="failed", attempt_count=5)   # v2-style exhausted failed
    # claim (max_attempts=5) reconciles it before selecting candidates
    claimed = store.claim_notifications(
        worker_id="A", lease_seconds=600, batch_limit=10, max_attempts=5, now_utc=T0, db_path=db)
    assert claimed == []                                             # not claimable
    row = store.get_notification("old:closed", db_path=db)
    assert row["delivery_status"] == "dead_letter"                  # normalized
    assert row["claimed_by"] is None and row["claimed_until_utc"] is None
    assert store.count_notifications_by_status(db_path=db).get("dead_letter") == 1
    # and manual requeue still works
    store.requeue_notification("old:closed", db_path=db)
    assert store.get_notification("old:closed", db_path=db)["delivery_status"] == "pending"


def test_retryable_and_terminal_rows_untouched_by_reconcile(tmp_path):
    db = tmp_path / "p.sqlite3"
    _insert_notification_row(db, event_key="r:closed", position_id="r", event_type="closed",
                             delivery_status="failed", attempt_count=2)   # below cap -> retryable
    _insert_notification_row(db, event_key="s:open", position_id="s", event_type="open",
                             delivery_status="success", attempt_count=1)
    _insert_notification_row(db, event_key="p:open", position_id="p", event_type="open",
                             delivery_status="pending", attempt_count=0)
    _insert_notification_row(db, event_key="proc:open", position_id="proc", event_type="open",
                             delivery_status="processing", attempt_count=1,
                             claimed_by="X", claimed_until_utc=T_LATER)
    store.claim_notifications(worker_id="A", lease_seconds=600, batch_limit=10, max_attempts=5,
                              now_utc=T0, db_path=db)
    assert store.get_notification("r:closed", db_path=db)["delivery_status"] in ("failed", "processing")
    assert store.get_notification("s:open", db_path=db)["delivery_status"] == "success"
    # pending got claimed (now processing); success/processing-with-live-lease unchanged in status class
    assert store.get_notification("proc:open", db_path=db)["delivery_status"] == "processing"
    # the retryable failed row was NOT dead-lettered
    assert store.get_notification("r:closed", db_path=db)["delivery_status"] != "dead_letter"


def test_v2_exhausted_failed_migrates_then_reconciles(tmp_path):
    db = tmp_path / "p.sqlite3"
    _make_v2_prototype(db, [
        dict(event_key="b:closed", position_id="b", event_type="closed", delivery_status="failed",
             attempt_count=5, last_error="HTTP 500", created_at_utc="t0", updated_at_utc="t1"),
    ])
    store.init_db(db)   # v2 -> v3 keeps it 'failed' (no dead_letter status existed in v2)
    assert store.get_notification("b:closed", db_path=db)["delivery_status"] == "failed"
    # first delivery cycle reconciles the exhausted row to dead_letter via claim
    res = _deliver(db, session=FakeSession(), now=T0)
    assert store.get_notification("b:closed", db_path=db)["delivery_status"] == "dead_letter"
    assert res.dead_letter_total == 1                              # reflected same cycle (Minor 3)


# 3. Minor: same-cycle dead_letter gauge


def test_same_cycle_dead_letter_gauge(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    _open_atomic(db)
    session = FakeSession(exc=requests.ConnectionError("down"))
    # max_attempts=1: the single failed attempt this cycle -> dead_letter immediately
    res = _deliver(db, session=session, now=T0, max_attempts=1)
    assert res.delivery_failures == 1
    assert store.get_notification("pos-1:open", db_path=db)["delivery_status"] == "dead_letter"
    assert res.dead_letter_total == 1                              # same-cycle, not next-cycle


def test_runner_same_cycle_dead_letter_gauge(tmp_path):
    db = tmp_path / "p.sqlite3"
    # Cycle 1 opens+closes the position (2 outbox rows) but every POST 500s.
    # A failed delivery clears the claim, so each following cycle re-claims and
    # re-fails, incrementing attempt_count until the MAX-th cycle tips both rows
    # to dead_letter -- and THAT cycle's CycleResult must report it immediately.
    res = None
    for _ in range(MAX):
        res = _run(db, [_signal()], _candles(CLOSE_CANDLES),
                   session=FakeSession(exc=requests.ConnectionError("down")))
    assert res.dead_letter_notifications == 2                 # same cycle it tipped over
    assert store.count_notifications_by_status(db_path=db).get("dead_letter") == 2
    assert store.list_positions(db_path=db)[0]["state"] == "closed"


def test_gauge_excludes_success_and_reflects_zero(tmp_path):
    db = tmp_path / "p.sqlite3"
    _seed_pending(db)
    _open_atomic(db)
    res = _deliver(db, session=FakeSession(), now=T0)   # succeeds
    assert res.sent == 1 and res.dead_letter_total == 0
