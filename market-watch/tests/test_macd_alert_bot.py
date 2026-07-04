import pathlib
import sys

import pandas as pd
import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import macd_alert_bot as bot  # noqa: E402


def test_detect_cross_bullish_when_macd_crosses_above_signal():
    assert bot.detect_cross(prev_macd=-0.1, prev_signal=0.0, curr_macd=0.1, curr_signal=0.0) == "bullish"


def test_detect_cross_bearish_when_macd_crosses_below_signal():
    assert bot.detect_cross(prev_macd=0.1, prev_signal=0.0, curr_macd=-0.1, curr_signal=0.0) == "bearish"


def test_detect_cross_none_when_no_cross():
    assert bot.detect_cross(prev_macd=0.2, prev_signal=0.0, curr_macd=0.1, curr_signal=0.0) is None


def _make_close_series(values):
    index = pd.date_range("2026-07-01", periods=len(values), freq="15min", tz="UTC")
    return pd.Series(values, index=index)


def test_compute_macd_returns_macd_and_signal_lines():
    close = _make_close_series([100 + i * 0.1 for i in range(60)])

    macd_line, signal_line = bot.compute_macd(close, fast=12, slow=26, signal=9)

    assert len(macd_line) == len(close)
    assert len(signal_line) == len(close)


def test_run_once_notifies_and_records_state_on_cross(monkeypatch):
    # 40 flat bars (diff == 0) then one bar that pushes MACD above the signal
    # line, then one more (still-forming, excluded) bar. The cross lands
    # exactly on the "confirmed" candle at index -2.
    values = [100.0] * 40 + [100.5, 100.5]
    close = _make_close_series(values)

    monkeypatch.setattr(bot, "fetch_candles", lambda: close)

    posted = []
    monkeypatch.setattr(bot, "post_discord_embed", lambda embed, dry_run=False: posted.append(embed))

    state = {}
    bot.run_once(state, dry_run=False)

    assert len(posted) == 1
    assert posted[0]["title"] == "MACD Bullish Cross"
    assert bot.state_key() in state


def test_run_once_does_not_duplicate_notification_for_same_candle(monkeypatch):
    values = [100.0] * 40 + [100.5, 100.5]
    close = _make_close_series(values)

    monkeypatch.setattr(bot, "fetch_candles", lambda: close)

    posted = []
    monkeypatch.setattr(bot, "post_discord_embed", lambda embed, dry_run=False: posted.append(embed))

    state = {}
    bot.run_once(state, dry_run=False)
    bot.run_once(state, dry_run=False)

    assert len(posted) == 1


def test_run_once_skips_when_not_enough_candles(monkeypatch, caplog):
    close = _make_close_series([100.0] * 5)
    monkeypatch.setattr(bot, "fetch_candles", lambda: close)

    posted = []
    monkeypatch.setattr(bot, "post_discord_embed", lambda embed, dry_run=False: posted.append(embed))

    bot.run_once({}, dry_run=False)

    assert posted == []


def test_dry_run_does_not_call_discord_http(monkeypatch):
    class FailingSession:
        def post(self, *args, **kwargs):
            raise AssertionError("HTTP post must not be called in dry-run")

    monkeypatch.setattr(bot.requests, "post", FailingSession().post)

    bot.post_discord_embed({"title": "test"}, dry_run=True)


def test_validate_config_requires_webhook_unless_dry_run(monkeypatch):
    monkeypatch.setattr(bot, "DISCORD_WEBHOOK_URL", "")

    with pytest.raises(RuntimeError):
        bot.validate_config(dry_run=False)

    bot.validate_config(dry_run=True)


def test_validate_config_rejects_unsupported_symbol(monkeypatch):
    monkeypatch.setattr(bot, "DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(bot, "SYMBOL", "EUR_USD")

    with pytest.raises(RuntimeError):
        bot.validate_config(dry_run=False)


def test_state_round_trips_through_disk(tmp_path, monkeypatch):
    state_path = tmp_path / "macd_alert_state.json"
    monkeypatch.setattr(bot, "STATE_FILE", state_path)

    bot.save_state({"USD_JPY_M15": "2026-07-01T00:00:00+00:00"})
    loaded = bot.load_state()

    assert loaded == {"USD_JPY_M15": "2026-07-01T00:00:00+00:00"}
