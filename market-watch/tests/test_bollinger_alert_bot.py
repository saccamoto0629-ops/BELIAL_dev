import pathlib
import sys

import pandas as pd

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import bollinger_alert_bot as bot  # noqa: E402


def _make_candles(values):
    index = pd.date_range("2026-07-01", periods=len(values), freq="15min", tz="UTC")
    return pd.DataFrame(
        {
            "open": [float(value) for value in values],
            "high": [float(value) + 0.01 for value in values],
            "low": [float(value) - 0.01 for value in values],
            "close": [float(value) for value in values],
        },
        index=index,
    )


def _patch_common(monkeypatch, candles, posted, saved):
    monkeypatch.setattr(bot, "ENABLED", True)
    monkeypatch.setattr(bot, "SYMBOL", "USD_JPY")
    monkeypatch.setattr(bot, "TIMEFRAME", "M15")
    monkeypatch.setattr(bot, "WINDOW", 20)
    monkeypatch.setattr(bot, "STD_MULTIPLIER", 2.0)
    monkeypatch.setattr(bot, "SQUEEZE_LOOKBACK", 3)
    monkeypatch.setattr(bot, "SQUEEZE_PERCENTILE", 0.10)
    monkeypatch.setattr(bot, "EXPANSION_RATIO", 1.30)
    monkeypatch.setattr(
        bot,
        "NOTIFY",
        {
            "upper_break": True,
            "lower_break": True,
            "expansion_start": True,
            "upper_touch": False,
            "lower_touch": False,
            "squeeze": False,
        },
    )
    monkeypatch.setattr(bot, "fetch_candles", lambda: candles)
    monkeypatch.setattr(bot, "prepare_alert_image", lambda **kwargs: (None, None))
    monkeypatch.setattr(bot, "post_discord_embed", lambda embed, dry_run=False, image_url=None: posted.append((embed, dry_run)))
    monkeypatch.setattr(bot, "save_state", lambda state: saved.append(dict(state)))


def test_run_once_notifies_and_records_upper_break(monkeypatch):
    candles = _make_candles([100.0] * 20 + [110.0, 100.0])
    posted = []
    saved = []
    _patch_common(monkeypatch, candles, posted, saved)

    state = {}
    bot.run_once(state, dry_run=False)

    candle_time = candles.index[-2].isoformat()
    key = bot.state_key("USD_JPY", "M15", "upper_break", candle_time)
    assert len(posted) == 1
    assert posted[0][0]["title"] == "Berial Market Watch: Bollinger Bands"
    assert "[BOLLINGER] USD_JPY M15 upper_break" in posted[0][0]["description"]
    assert "エントリー判断は手法側で確認。" in posted[0][0]["description"]
    assert key in state
    assert state[key]["signal_type"] == "upper_break"
    assert saved


def test_run_once_does_not_duplicate_same_candle_signal(monkeypatch):
    candles = _make_candles([100.0] * 20 + [90.0, 100.0])
    posted = []
    saved = []
    _patch_common(monkeypatch, candles, posted, saved)

    state = {}
    bot.run_once(state, dry_run=False)
    bot.run_once(state, dry_run=False)

    assert len(posted) == 1


def test_run_once_dry_run_does_not_save_state(monkeypatch):
    candles = _make_candles([100.0] * 20 + [110.0, 100.0])
    posted = []
    saved = []
    _patch_common(monkeypatch, candles, posted, saved)

    state = {}
    bot.run_once(state, dry_run=True)

    assert len(posted) == 1
    assert posted[0][1] is True
    assert state == {}
    assert saved == []


def test_run_once_uses_confirmed_candle_not_latest_candle(monkeypatch):
    candles = _make_candles([100.0] * 19 + [101.0, 100.0, 110.0])
    posted = []
    saved = []
    _patch_common(monkeypatch, candles, posted, saved)

    bot.run_once({}, dry_run=False)

    assert posted == []
    assert saved == []


def test_run_once_honors_default_touch_notify_off(monkeypatch):
    candles = _make_candles([100.0] * 20 + [110.0, 100.0])
    posted = []
    saved = []
    _patch_common(monkeypatch, candles, posted, saved)
    monkeypatch.setattr(
        bot,
        "NOTIFY",
        {
            "upper_break": False,
            "lower_break": False,
            "expansion_start": False,
            "upper_touch": False,
            "lower_touch": False,
            "squeeze": False,
        },
    )

    bot.run_once({}, dry_run=False)

    assert posted == []
    assert saved == []


def test_notify_bollinger_passes_ohlc_to_chart_image(monkeypatch):
    candles = _make_candles([100.0, 101.0, 102.0])
    candles["open"] = [99.5, 100.5, 101.5]
    candles["volume"] = [100.0, 120.0, 110.0]
    event = bot.build_bollinger_event(
        {
            "signal_type": "upper_break",
            "close": 102.0,
            "upper": 101.5,
            "middle": 100.0,
            "lower": 98.5,
            "bandwidth": 0.03,
            "note": "test",
        },
        candles.index[-1].isoformat(),
    )
    captured = {}
    posted = []

    def fake_prepare_alert_image(**kwargs):
        captured["columns"] = list(kwargs["candles"].columns)
        return "snapshot-1", "https://presigned.invalid/chart.png"

    monkeypatch.setattr(bot, "prepare_alert_image", fake_prepare_alert_image)
    monkeypatch.setattr(bot, "post_discord_embed", lambda embed, dry_run=False, image_url=None: posted.append((embed, image_url)))

    bot.notify_bollinger(event, candles=candles)

    assert {"open", "high", "low", "close"}.issubset(captured["columns"])
    assert posted[0][1] == "https://presigned.invalid/chart.png"


def test_load_config_reads_bollinger_section(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[monitor]
symbol = "USD_JPY"
ticker = "JPY=X"
timeframe = "M15"
interval = "15m"
period = "5d"

[bollinger]
enabled = true
window = 20
std_multiplier = 2.0
squeeze_lookback = 120
squeeze_percentile = 0.10
expansion_ratio = 1.30

[bollinger.notify]
upper_break = true
lower_break = true
expansion_start = true
upper_touch = false
lower_touch = false
squeeze = false

[runtime]
check_interval_seconds = 30

[logging]
log_level = "DEBUG"
""",
        encoding="utf-8",
    )

    config = bot.load_config(config_path)

    assert config.symbol == "USD_JPY"
    assert config.window == 20
    assert config.std_multiplier == 2.0
    assert config.squeeze_lookback == 120
    assert config.squeeze_percentile == 0.10
    assert config.expansion_ratio == 1.30
    assert config.notify["upper_break"] is True
    assert config.notify["upper_touch"] is False
    assert config.check_interval_seconds == 30
    assert config.log_level == "DEBUG"
