"""Issue #83: Paper Execution Discord notifier -- bodies, dedup, retry, dry-run, migration."""

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


# --- position fixtures -------------------------------------------------------


def _open_position(**over):
    row = dict(
        position_id="pos-1", signal_id="sig-1", symbol="USDJPY", timeframe="1h", side="long",
        strategy_profile="p", execution_profile="default", state="open", terminal_reason=None,
        entry_time_utc="2026-07-06T13:00:00Z", entry_price=100.0,
        stop_loss_price=99.0, take_profit_price=102.0, max_hold_deadline_utc="2026-07-07T13:00:00Z",
    )
    row.update(over)
    return row


def _closed_position(**over):
    row = _open_position(
        state="closed", terminal_reason="take_profit", close_price=102.0,
        close_time_utc="2026-07-06T15:00:00Z", gross_pnl=2.0, net_pnl=1.9, r_multiple=1.9,
    )
    row.update(over)
    return row


def _seed_closed(db, position_id="pos-1", signal_id="sig-1"):
    """Persist a real closed position via the store (full transition chain)."""
    pos = models.create_pending_position(
        signal_id=signal_id, symbol="USDJPY", timeframe="1h", side="long", strategy_profile="p",
        profile=PROFILE, signal_candle_utc="2026-07-06T12:00:00Z",
        entry_deadline_utc="2026-07-06T16:00:00Z", position_id=position_id,
    )
    store.create_position(pos, db_path=db)
    store.transition_to_open(
        position_id, entry_price=100.0, entry_time_utc="2026-07-06T13:00:00Z",
        stop_loss_price=99.0, take_profit_price=102.0,
        max_hold_deadline_utc="2026-07-07T13:00:00Z", db_path=db,
    )
    return store.close_open_position(
        position_id, expected_watermark=None, new_watermark="2026-07-06T15:00:00Z",
        close_price=102.0, close_time_utc="2026-07-06T15:00:00Z", terminal_reason="take_profit",
        gross_pnl=2.0, net_pnl=1.9, r_multiple=1.9, db_path=db,
    )


# --- message bodies ----------------------------------------------------------


def test_open_message_has_required_fields():
    msg = notifier.format_event_message(notifier.EVENT_OPEN, _open_position())
    assert "Paper Execution OPEN" in msg and "仮想約定" in msg
    for expected in (
        "Symbol: USDJPY", "Timeframe: 1h", "Side: long", "Signal ID: sig-1", "Position ID: pos-1",
        "Entry Time (UTC): 2026-07-06T13:00:00Z", "Entry Price: 100.00000",
        "Stop Loss: 99.00000", "Take Profit: 102.00000",
        "Max Hold Deadline (UTC): 2026-07-07T13:00:00Z", "Execution Profile: default",
    ):
        assert expected in msg
    assert notifier.PAPER_EXECUTION_NOTE in msg


def test_closed_message_has_required_fields():
    msg = notifier.format_event_message(notifier.EVENT_CLOSED, _closed_position())
    assert "Paper Execution CLOSED" in msg and "仮想損益" in msg
    for expected in (
        "Symbol: USDJPY", "Timeframe: 1h", "Side: long", "Signal ID: sig-1", "Position ID: pos-1",
        "Terminal Reason: take_profit", "Entry Time (UTC): 2026-07-06T13:00:00Z",
        "Close Time (UTC): 2026-07-06T15:00:00Z", "Entry Price: 100.00000", "Close Price: 102.00000",
        "Gross PnL: 2.00000", "Net PnL: 1.90000", "R Multiple: 1.90",
    ):
        assert expected in msg
    assert notifier.PAPER_EXECUTION_NOTE in msg


@pytest.mark.parametrize("reason", ["stop_loss", "take_profit", "max_holding_time"])
def test_closed_message_shows_each_terminal_reason(reason):
    msg = notifier.format_event_message(notifier.EVENT_CLOSED, _closed_position(terminal_reason=reason))
    assert f"Terminal Reason: {reason}" in msg


def test_short_side_rendered():
    msg = notifier.format_event_message(notifier.EVENT_OPEN, _open_position(side="short"))
    assert "Side: short" in msg


def test_expired_message_has_state_reason_and_virtual_note():
    pos = _open_position(state="expired", terminal_reason="entry_timeout",
                         entry_price=None, entry_time_utc=None)
    msg = notifier.format_event_message(notifier.EVENT_EXPIRED, pos)
    assert "Paper Execution EXPIRED" in msg and "仮想" in msg
    assert "State: expired" in msg and "Terminal Reason: entry_timeout" in msg
    for expected in ("Symbol: USDJPY", "Timeframe: 1h", "Side: long", "Signal ID: sig-1", "Position ID: pos-1"):
        assert expected in msg


def test_cancelled_message_has_state_reason_and_virtual_note():
    pos = _open_position(state="cancelled", terminal_reason="invalid_input", side="", symbol="")
    msg = notifier.format_event_message(notifier.EVENT_CANCELLED, pos)
    assert "Paper Execution CANCELLED" in msg and "仮想" in msg
    assert "State: cancelled" in msg and "Terminal Reason: invalid_input" in msg
    assert "Symbol: -" in msg and "Side: -" in msg   # blank fields -> placeholder, no exception


def test_none_values_render_safely_never_raise():
    sparse = {"position_id": "pos-x"}   # everything else missing
    for event_type in sorted(store.KNOWN_NOTIFY_EVENTS):
        msg = notifier.format_event_message(event_type, sparse)
        assert "None" not in msg
        assert ": -" in msg


def test_unknown_event_type_raises():
    with pytest.raises(ValueError):
        notifier.format_event_message("promoted", _open_position())


def test_unicode_payload_is_json_serializable_and_capped():
    msg = notifier.format_event_message(notifier.EVENT_OPEN, _open_position(symbol="ドル円🚀"))
    payload = notifier.build_discord_payload(msg)
    assert "ドル円🚀" in payload["content"]
    json.dumps(payload)                    # serializable as sent by requests
    long_payload = notifier.build_discord_payload("あ" * 3000)
    assert len(long_payload["content"]) == notifier.DISCORD_CONTENT_LIMIT


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


# --- notification history store ----------------------------------------------


def test_record_event_idempotent(tmp_path):
    db = tmp_path / "p.sqlite3"
    assert store.record_notification_event("pos-1", "open", db_path=db) is True
    assert store.record_notification_event("pos-1", "open", db_path=db) is False
    row = store.get_notification("pos-1:open", db_path=db)
    assert row["delivery_status"] == "pending" and row["attempt_count"] == 0


def test_success_not_listed_for_redelivery(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.record_notification_event("pos-1", "open", db_path=db)
    row = store.mark_notification_delivery("pos-1:open", success=True, db_path=db)
    assert row["delivery_status"] == "success" and row["attempt_count"] == 1
    assert row["notified_at_utc"] and row["last_error"] is None
    assert store.list_undelivered_notifications(db_path=db) == []


def test_failed_row_is_retryable_until_attempt_cap(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.record_notification_event("pos-1", "closed", db_path=db)
    for attempt in range(1, notifier.MAX_DELIVERY_ATTEMPTS + 1):
        rows = store.list_undelivered_notifications(db_path=db, max_attempts=notifier.MAX_DELIVERY_ATTEMPTS)
        assert [r["event_key"] for r in rows] == ["pos-1:closed"]
        row = store.mark_notification_delivery("pos-1:closed", success=False, error="HTTP 500", db_path=db)
        assert row["delivery_status"] == "failed" and row["attempt_count"] == attempt
        assert row["last_error"] == "HTTP 500"
    # attempt cap reached: no longer retried (never an infinite retry)
    assert store.list_undelivered_notifications(db_path=db, max_attempts=notifier.MAX_DELIVERY_ATTEMPTS) == []


def test_mark_unknown_event_key_raises(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.init_db(db)
    with pytest.raises(store.NotificationNotFoundError):
        store.mark_notification_delivery("nope:open", success=True, db_path=db)


# --- schema migration (v1 -> v2) ---------------------------------------------


def _user_version(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def _make_v1_db(db):
    """A v1 DB: fully constrained positions table, user_version=1, no notifications table."""
    store.init_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(f"DROP TABLE {store.NOTIFICATIONS_TABLE}")
        conn.execute("PRAGMA user_version = 1")


def test_fresh_db_has_notifications_table_and_v2(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.init_db(db)
    assert _user_version(db) == store.CURRENT_SCHEMA_VERSION == 2
    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({store.NOTIFICATIONS_TABLE})")}
    assert store.REQUIRED_NOTIFICATION_COLUMNS <= cols
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_CURRENT


def test_v1_db_classified_legacy_then_upgraded_by_store_write(tmp_path):
    db = tmp_path / "p.sqlite3"
    _make_v1_db(db)
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_LEGACY
    _seed_closed(db)   # any store write path runs init_db -> additive v1 -> v2 upgrade
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_CURRENT
    assert _user_version(db) == 2
    assert store.get_position("pos-1", db_path=db)["state"] == "closed"


def test_v1_upgrade_preserves_position_rows_without_rebuild(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.init_db(db)
    seeded = _seed_closed(db)
    with sqlite3.connect(db) as conn:
        conn.execute(f"DROP TABLE {store.NOTIFICATIONS_TABLE}")
        conn.execute("PRAGMA user_version = 1")
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_LEGACY   # v1 = legacy now
    store.init_db(db)
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_CURRENT
    after = store.get_position("pos-1", db_path=db)
    assert after == seeded   # rows byte-identical: additive upgrade, no rebuild


def test_incompatible_notifications_table_fails_with_rollback(tmp_path):
    db = tmp_path / "p.sqlite3"
    store.init_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(f"DROP TABLE {store.NOTIFICATIONS_TABLE}")
        conn.execute(f"CREATE TABLE {store.NOTIFICATIONS_TABLE} (wrong_column TEXT)")
        conn.execute("PRAGMA user_version = 1")
    with pytest.raises(store.PositionMigrationError):
        store.init_db(db)
    # rollback: version stamp unchanged, incompatible table untouched
    assert _user_version(db) == 1
    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({store.NOTIFICATIONS_TABLE})")}
    assert cols == {"wrong_column"}


def test_legacy_rebuild_also_creates_notifications_table(tmp_path):
    db = tmp_path / "p.sqlite3"
    legacy_cols = [c for c in store._ALL_COLUMNS if c not in ("size", "gross_pnl", "net_pnl", "r_multiple")]
    text_cols = {
        "position_id", "signal_id", "symbol", "timeframe", "side", "strategy_profile", "execution_profile",
        "state", "terminal_reason", "signal_candle_utc", "entry_deadline_utc", "entry_time_utc",
        "max_hold_deadline_utc", "close_time_utc", "same_candle_priority", "last_evaluated_candle_utc",
        "created_at_utc", "updated_at_utc",
    }
    with sqlite3.connect(db) as conn:
        ddl = ", ".join(f"{c} {'TEXT' if c in text_cols else 'REAL'}" for c in legacy_cols)
        conn.execute(f"CREATE TABLE {store.POSITIONS_TABLE} ({ddl})")
        conn.execute("PRAGMA user_version = 0")
    store.init_db(db)
    assert _user_version(db) == store.CURRENT_SCHEMA_VERSION
    assert store.get_schema_status(db) == store.SCHEMA_STATUS_CURRENT


# --- delivery error classification -------------------------------------------


@pytest.mark.parametrize("exc,expected", [
    (requests.Timeout("boom"), "timeout"),
    (requests.ConnectionError("refused"), "connection error"),
])
def test_deliver_classifies_transport_errors(exc, expected):
    with pytest.raises(notifier.NotificationDeliveryError) as err:
        notifier.deliver_payload({"content": "x"}, webhook_url=WEBHOOK, session=FakeSession(exc=exc))
    assert expected in str(err.value)
    assert WEBHOOK not in str(err.value)   # sanitized: never the webhook URL


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


def test_transport_error_message_never_contains_url():
    exc = requests.ConnectionError(f"failed to reach {WEBHOOK}")
    with pytest.raises(notifier.NotificationDeliveryError) as err:
        notifier.deliver_payload({"content": "x"}, webhook_url=WEBHOOK, session=FakeSession(exc=exc))
    assert WEBHOOK not in str(err.value) and "secret-token" not in str(err.value)


# --- process_events: dedup / retry / failure contract ------------------------


def _event_for(row, event_type=None):
    return notifier.ExecutionEvent(event_type or notifier.EVENT_CLOSED, row)


def test_process_events_success_records_and_sends(tmp_path):
    db = tmp_path / "p.sqlite3"
    closed = _seed_closed(db)
    session = FakeSession()
    res = notifier.process_events([_event_for(closed)], db_path=db, webhook_url_value=WEBHOOK, session=session)
    assert res.sent == 1 and res.failed == 0
    assert len(session.calls) == 1
    assert "仮想損益" in session.calls[0]["json"]["content"]
    assert store.get_notification("pos-1:closed", db_path=db)["delivery_status"] == "success"


def test_success_is_not_resent_on_rerun_or_restart(tmp_path):
    db = tmp_path / "p.sqlite3"
    closed = _seed_closed(db)
    session = FakeSession()
    notifier.process_events([_event_for(closed)], db_path=db, webhook_url_value=WEBHOOK, session=session)
    # same event again (re-run / runner restart replays the transition record)
    res2 = notifier.process_events([_event_for(closed)], db_path=db, webhook_url_value=WEBHOOK, session=session)
    assert res2.sent == 0 and res2.skipped_duplicates == 1
    assert len(session.calls) == 1   # exactly one Discord post ever
    assert store.get_notification("pos-1:closed", db_path=db)["attempt_count"] == 1


def test_failed_delivery_is_retried_on_next_cycle(tmp_path):
    db = tmp_path / "p.sqlite3"
    closed = _seed_closed(db)
    failing = FakeSession(responses=[FakeResponse(500)])
    res1 = notifier.process_events([_event_for(closed)], db_path=db, webhook_url_value=WEBHOOK, session=failing)
    assert res1.failed == 1 and res1.sent == 0
    row = store.get_notification("pos-1:closed", db_path=db)
    assert row["delivery_status"] == "failed" and row["attempt_count"] == 1

    # next cycle: NO new events, the queued failure is picked up and delivered
    ok = FakeSession()
    res2 = notifier.process_events([], db_path=db, webhook_url_value=WEBHOOK, session=ok)
    assert res2.sent == 1
    assert len(ok.calls) == 1
    assert store.get_notification("pos-1:closed", db_path=db)["delivery_status"] == "success"


@pytest.mark.parametrize("failure", [
    requests.Timeout("t"), requests.ConnectionError("c"),
])
def test_transport_failures_are_recorded_and_do_not_raise(tmp_path, failure):
    db = tmp_path / "p.sqlite3"
    closed = _seed_closed(db)
    res = notifier.process_events(
        [_event_for(closed)], db_path=db, webhook_url_value=WEBHOOK, session=FakeSession(exc=failure)
    )
    assert res.failed == 1
    row = store.get_notification("pos-1:closed", db_path=db)
    assert row["delivery_status"] == "failed" and row["last_error"]
    assert WEBHOOK not in (row["last_error"] or "")


@pytest.mark.parametrize("status", [429, 500])
def test_http_failures_are_bounded_not_infinite(tmp_path, status):
    db = tmp_path / "p.sqlite3"
    closed = _seed_closed(db)
    session = FakeSession(responses=[FakeResponse(status)] * (notifier.MAX_DELIVERY_ATTEMPTS + 3))
    notifier.process_events([_event_for(closed)], db_path=db, webhook_url_value=WEBHOOK, session=session)
    for _ in range(notifier.MAX_DELIVERY_ATTEMPTS + 2):   # later cycles, still failing
        notifier.process_events([], db_path=db, webhook_url_value=WEBHOOK, session=session)
    # one attempt per cycle, capped at MAX_DELIVERY_ATTEMPTS total
    assert len(session.calls) == notifier.MAX_DELIVERY_ATTEMPTS
    assert store.get_notification("pos-1:closed", db_path=db)["attempt_count"] == notifier.MAX_DELIVERY_ATTEMPTS


def test_position_state_survives_delivery_failure(tmp_path):
    db = tmp_path / "p.sqlite3"
    closed = _seed_closed(db)
    before = store.get_position("pos-1", db_path=db)
    res = notifier.process_events(
        [_event_for(closed)], db_path=db, webhook_url_value=WEBHOOK,
        session=FakeSession(exc=requests.ConnectionError("down")),
    )
    assert res.failed == 1
    after = store.get_position("pos-1", db_path=db)
    assert after == before                       # transition NOT rolled back
    assert after["state"] == "closed"


def test_missing_webhook_queues_without_sending(tmp_path):
    db = tmp_path / "p.sqlite3"
    closed = _seed_closed(db)
    session = FakeSession()
    res = notifier.process_events([_event_for(closed)], db_path=db, webhook_url_value="", session=session)
    assert res.sent == 0 and res.failed == 0
    assert session.calls == []
    assert store.get_notification("pos-1:closed", db_path=db)["delivery_status"] == "pending"


def test_dry_run_no_db_no_http_logs_body(tmp_path, caplog):
    db = tmp_path / "absent.sqlite3"
    session = FakeSession()
    with caplog.at_level("INFO", logger="paper_execution"):
        res = notifier.process_events(
            [_event_for(_closed_position())], db_path=db, dry_run=True,
            webhook_url_value=WEBHOOK, session=session,
        )
    assert res.sent == 0 and res.failed == 0
    assert not db.exists()                       # no DB, no file
    assert session.calls == []                   # no HTTP
    assert any("仮想損益" in r.message for r in caplog.records)   # body inspectable in logs


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


def _run(db, signals, candles, now=NOW, *, session, dry_run=False, webhook=WEBHOOK):
    return runner.run_once(
        dry_run=dry_run, db_path=db, signals=signals, load_candles=lambda s, t: candles,
        now_utc=now, profile=PROFILE, chart_db_path=db.parent / "chart.sqlite3",
        signal_db_path=db.parent / "sig.sqlite3",
        notify_webhook_url=webhook, notify_session=session,
    )


def test_runner_notifies_open_and_closed(tmp_path):
    db = tmp_path / "p.sqlite3"
    session = FakeSession()
    candles = _candles([(50, 51, 49, 50), (100.0, 100.2, 99.9, 100.1), (100.5, 102.5, 100.1, 101.0)])
    res = _run(db, [_signal()], candles, session=session)
    assert res.failures == 0 and res.notification_failures == 0
    bodies = [c["json"]["content"] for c in session.calls]
    assert len(bodies) == 2
    assert "OPEN" in bodies[0] and "仮想約定" in bodies[0]
    assert "CLOSED" in bodies[1] and "仮想損益" in bodies[1] and "take_profit" in bodies[1]


def test_runner_restart_does_not_renotify(tmp_path):
    db = tmp_path / "p.sqlite3"
    session = FakeSession()
    candles = _candles([(50, 51, 49, 50), (100.0, 100.2, 99.9, 100.1), (100.5, 102.5, 100.1, 101.0)])
    _run(db, [_signal()], candles, session=session)
    assert len(session.calls) == 2
    # "restart": a fresh cycle over the same inputs -- no transition re-fires,
    # the history table suppresses everything already delivered.
    res2 = _run(db, [_signal()], candles, session=session)
    assert res2.notification_failures == 0
    assert len(session.calls) == 2   # nothing re-sent


def test_runner_notifies_expired(tmp_path):
    db = tmp_path / "p.sqlite3"
    session = FakeSession()
    idx = pd.to_datetime(["2026-07-06T12:00:00Z", "2026-07-06T20:00:00Z"])
    candles = pd.DataFrame([(50, 51, 49, 50), (100, 101, 99, 100)],
                           columns=["open", "high", "low", "close"], index=idx)
    res = _run(db, [_signal()], candles, pd.Timestamp("2026-07-06T22:00:00Z"), session=session)
    assert res.expired == 1
    assert any("EXPIRED" in c["json"]["content"] for c in session.calls)
    assert any("entry_timeout" in c["json"]["content"] for c in session.calls)


def test_runner_notifies_cancelled_for_invalid_signal(tmp_path):
    db = tmp_path / "p.sqlite3"
    session = FakeSession()
    res = _run(db, [_signal(status="closed")], _candles([(50, 51, 49, 50)]), session=session)
    assert res.cancelled == 1
    assert len(session.calls) == 1
    body = session.calls[0]["json"]["content"]
    assert "CANCELLED" in body and "invalid_input" in body


def test_runner_delivery_failure_does_not_fail_cycle_or_position(tmp_path):
    db = tmp_path / "p.sqlite3"
    session = FakeSession(exc=requests.ConnectionError("down"))
    candles = _candles([(50, 51, 49, 50), (100.0, 100.2, 99.9, 100.1), (100.5, 102.5, 100.1, 101.0)])
    res = _run(db, [_signal()], candles, session=session)
    assert res.failures == 0                      # data plane untouched by webhook outage
    assert res.notification_failures >= 1
    assert res.ok                                 # exit-code contract: notify failures don't fail --once
    pos = store.list_positions(db_path=db)[0]
    assert pos["state"] == "closed"               # transition committed before/despite notify failure
    # and the failed rows are queued for retry
    assert all(r["delivery_status"] == "failed"
               for r in store.list_undelivered_notifications(db_path=db))


def test_runner_retries_failed_notification_next_cycle(tmp_path):
    db = tmp_path / "p.sqlite3"
    candles = _candles([(50, 51, 49, 50), (100.0, 100.2, 99.9, 100.1), (100.5, 102.5, 100.1, 101.0)])
    _run(db, [_signal()], candles, session=FakeSession(exc=requests.Timeout("t")))
    ok = FakeSession()
    res2 = _run(db, [_signal()], candles, session=ok)   # next cycle, webhook recovered
    assert res2.notification_failures == 0
    assert len(ok.calls) == 2                     # open + closed delivered exactly once each
    assert store.list_undelivered_notifications(db_path=db) == []


def test_runner_dry_run_no_db_creation_no_http(tmp_path):
    db = tmp_path / "p.sqlite3"
    session = FakeSession()
    candles = _candles([(50, 51, 49, 50), (100.0, 100.2, 99.9, 100.1), (100.5, 102.5, 100.1, 101.0)])
    res = _run(db, [_signal()], candles, session=session, dry_run=True)
    assert res.failures == 0
    assert not db.exists()                        # no position DB, no notifications table
    assert not (tmp_path / "logs").exists()       # no log file
    assert session.calls == []                    # no Discord


def test_runner_dry_run_existing_db_unchanged_and_bodies_logged(tmp_path, caplog):
    db = tmp_path / "p.sqlite3"
    # seed a pending position (normal run side effect) so the dry run has a
    # would-OPEN / would-CLOSE decision to notify about.
    pos = models.create_pending_position(
        signal_id="sig-1", symbol="USDJPY", timeframe="1h", side="long", strategy_profile="p",
        profile=PROFILE, signal_candle_utc="2026-07-06T12:00:00Z",
        entry_deadline_utc="2026-07-06T16:00:00Z",
    )
    store.create_position(pos, db_path=db)
    before_mtime = db.stat().st_mtime_ns
    session = FakeSession()
    candles = _candles([(50, 51, 49, 50), (100.0, 100.2, 99.9, 100.1), (100.5, 102.5, 100.1, 101.0)])
    with caplog.at_level("INFO", logger="paper_execution"):
        res = _run(db, [], candles, session=session, dry_run=True)
    assert res.failures == 0 and res.opened == 1
    assert session.calls == []                                        # no Discord
    assert db.stat().st_mtime_ns == before_mtime                      # DB untouched
    assert store.list_positions(db_path=db)[0]["state"] == "pending"  # no transition
    with sqlite3.connect(db) as conn:                                 # no notification rows
        assert conn.execute(f"SELECT COUNT(*) FROM {store.NOTIFICATIONS_TABLE}").fetchone()[0] == 0
    assert any("would notify" in r.message and "仮想約定" in r.message for r in caplog.records)


def test_run_service_once_ok_despite_notification_failures(monkeypatch):
    monkeypatch.setattr(runner, "run_once", lambda **k: runner.CycleResult(notification_failures=3))
    assert runner.run_service(once=True) == runner.EXIT_OK   # documented contract
