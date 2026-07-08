import pathlib
import sys

import pandas as pd

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import rsi_alert_bot as bot  # noqa: E402


def _make_candles(count=20):
    index = pd.date_range("2026-07-01", periods=count, freq="15min", tz="UTC")
    values = [100.0 + index for index in range(count)]
    return pd.DataFrame({"close": values}, index=index)


def _make_indicator_frame(index, previous_rsi, current_rsi):
    values = [50.0 for _ in range(len(index))]
    values[-3] = previous_rsi
    values[-2] = current_rsi
    return pd.DataFrame({"rsi14": values}, index=index)


def _patch_common(monkeypatch, candles, posted, saved, *, previous_rsi=69.0, current_rsi=72.4):
    monkeypatch.setattr(bot, "ENABLED", True)
    monkeypatch.setattr(bot, "SYMBOL", "USD_JPY")
    monkeypatch.setattr(bot, "TIMEFRAME", "M15")
    monkeypatch.setattr(bot, "RSI_PERIOD", 14)
    monkeypatch.setattr(bot, "OVERBOUGHT_THRESHOLD", 70.0)
    monkeypatch.setattr(bot, "OVERSOLD_THRESHOLD", 30.0)
    monkeypatch.setattr(bot, "fetch_candles", lambda: candles)
    monkeypatch.setattr(
        bot,
        "build_indicators",
        lambda candles_arg: _make_indicator_frame(candles_arg.index, previous_rsi, current_rsi),
    )
    monkeypatch.setattr(bot, "post_discord_embed", lambda embed, dry_run=False: posted.append((embed, dry_run)))
    monkeypatch.setattr(bot, "save_state", lambda state: saved.append(dict(state)))


def test_run_once_notifies_and_records_overbought_enter(monkeypatch):
    candles = _make_candles()
    posted = []
    saved = []
    _patch_common(monkeypatch, candles, posted, saved)

    state = {}
    bot.run_once(state, dry_run=False)

    key = bot.state_key("USD_JPY", "M15")
    candle_time = candles.index[-2].isoformat()
    assert len(posted) == 1
    assert posted[0][0]["title"] == "Berial Market Watch: RSI14"
    assert "[RSI Alert]" in posted[0][0]["description"]
    assert "Event: overbought_enter" in posted[0][0]["description"]
    assert "売買判断ではありません" in posted[0][0]["description"]
    assert state[key]["candle_time"] == candle_time
    assert state[key]["last_event_type"] == "overbought_enter"
    assert saved


def test_run_once_does_not_duplicate_recorded_candle(monkeypatch):
    candles = _make_candles()
    posted = []
    saved = []
    _patch_common(monkeypatch, candles, posted, saved)
    candle_time = candles.index[-2].isoformat()
    state = {
        bot.state_key("USD_JPY", "M15"): {
            "candle_time": candle_time,
            "rsi14": 72.4,
            "rsi_state": "Overbought",
            "last_event_type": "overbought_enter",
            "notified_at": "2026-07-01T00:00:00+00:00",
            "updated_at": "2026-07-01T00:00:00+00:00",
        }
    }

    bot.run_once(state, dry_run=False)

    assert posted == []
    assert saved == []


def test_run_once_dry_run_does_not_save_state(monkeypatch):
    candles = _make_candles()
    posted = []
    saved = []
    _patch_common(monkeypatch, candles, posted, saved)

    state = {}
    bot.run_once(state, dry_run=True)

    assert len(posted) == 1
    assert posted[0][1] is True
    assert state == {}
    assert saved == []


def test_run_once_records_neutral_state_without_notification(monkeypatch):
    candles = _make_candles()
    posted = []
    saved = []
    _patch_common(monkeypatch, candles, posted, saved, previous_rsi=45.0, current_rsi=50.0)

    state = {}
    bot.run_once(state, dry_run=False)

    key = bot.state_key("USD_JPY", "M15")
    assert posted == []
    assert state[key]["rsi_state"] == "Neutral"
    assert state[key]["last_event_type"] is None
    assert saved


def test_run_once_uses_confirmed_candle_not_latest_candle(monkeypatch):
    candles = _make_candles()
    posted = []
    saved = []
    values = [50.0 for _ in range(len(candles))]
    values[-3] = 55.0
    values[-2] = 56.0
    values[-1] = 72.0
    frame = pd.DataFrame({"rsi14": values}, index=candles.index)
    monkeypatch.setattr(bot, "ENABLED", True)
    monkeypatch.setattr(bot, "SYMBOL", "USD_JPY")
    monkeypatch.setattr(bot, "TIMEFRAME", "M15")
    monkeypatch.setattr(bot, "fetch_candles", lambda: candles)
    monkeypatch.setattr(bot, "build_indicators", lambda candles_arg: frame)
    monkeypatch.setattr(bot, "post_discord_embed", lambda embed, dry_run=False: posted.append((embed, dry_run)))
    monkeypatch.setattr(bot, "save_state", lambda state: saved.append(dict(state)))

    bot.run_once({}, dry_run=False)

    assert posted == []
    assert saved


def test_load_config_reads_rsi_alert_section(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[monitor]
symbol = "USD_JPY"
ticker = "JPY=X"
timeframe = "M15"
interval = "15m"
period = "5d"

[rsi_alert]
enabled = true
overbought_threshold = 75.0
oversold_threshold = 25.0

[runtime]
check_interval_seconds = 30

[logging]
log_level = "DEBUG"
""",
        encoding="utf-8",
    )

    config = bot.load_config(config_path)

    assert config.symbol == "USD_JPY"
    assert config.overbought_threshold == 75.0
    assert config.oversold_threshold == 25.0
    assert config.check_interval_seconds == 30
    assert config.log_level == "DEBUG"
