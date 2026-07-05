import pathlib
import sys
from datetime import UTC, datetime, timedelta

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import paper_signal_store as store  # noqa: E402
import signal_models as sm  # noqa: E402


def make_signal(*, created_at_utc=None, **overrides):
    candidate = {
        "symbol": "USDJPY",
        "timeframe": "1h",
        "side": "long",
        "market_bias": "Bull",
        "confidence": 0.72,
        "macd_state": "Bullish",
        "trend": "Bull",
        "state": "Inside band",
        "freshness": "Fresh",
        "reason": "test reason",
    }
    candidate.update(overrides)
    return sm.create_paper_signal(candidate, created_at_utc=created_at_utc)


def test_save_and_list_signal_round_trips(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    signal = make_signal()

    store.save_signal(signal, db_path=db_path)
    rows = store.list_signals(db_path=db_path)

    assert len(rows) == 1
    assert rows[0]["signal_id"] == signal["signal_id"]
    assert rows[0]["status"] == "paper_only"


def test_has_recent_signal_true_within_window(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)
    signal = make_signal(created_at_utc=now.replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    store.save_signal(signal, db_path=db_path)

    later = now + timedelta(minutes=30)
    assert store.has_recent_signal(
        "USDJPY", "1h", "long", within_minutes=60, now_utc=later, db_path=db_path
    )


def test_has_recent_signal_false_outside_window(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)
    signal = make_signal(created_at_utc=now.replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    store.save_signal(signal, db_path=db_path)

    much_later = now + timedelta(minutes=61)
    assert not store.has_recent_signal(
        "USDJPY", "1h", "long", within_minutes=60, now_utc=much_later, db_path=db_path
    )


def test_has_recent_signal_false_for_different_side(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)
    signal = make_signal(
        side="long", created_at_utc=now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    store.save_signal(signal, db_path=db_path)

    assert not store.has_recent_signal(
        "USDJPY", "1h", "short", within_minutes=60, now_utc=now, db_path=db_path
    )


def test_has_recent_signal_false_when_db_is_empty(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"

    assert not store.has_recent_signal("USDJPY", "1h", "long", db_path=db_path)


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "paper_signals.sqlite3"

    store.init_db(db_path)
    store.init_db(db_path)

    assert store.list_signals(db_path=db_path) == []
