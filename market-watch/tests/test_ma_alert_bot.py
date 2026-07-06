import pathlib
import sys

import pandas as pd

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import ma_alert_bot as bot  # noqa: E402


def _make_candles(values):
    index = pd.date_range("2026-07-01", periods=len(values), freq="15min", tz="UTC")
    return pd.DataFrame({"close": values}, index=index)


def _patch_common(monkeypatch, candles, posted, saved):
    monkeypatch.setattr(bot, "ENABLED", True)
    monkeypatch.setattr(bot, "SYMBOL", "USD_JPY")
    monkeypatch.setattr(bot, "TIMEFRAME", "M15")
    monkeypatch.setattr(bot, "MA_NAMES", ["sma20"])
    monkeypatch.setattr(bot, "THRESHOLD_PCT", 0.05)
    monkeypatch.setattr(bot, "fetch_candles", lambda: candles)
    monkeypatch.setattr(bot, "post_discord_embed", lambda embed, dry_run=False: posted.append((embed, dry_run)))
    monkeypatch.setattr(bot, "save_state", lambda state: saved.append(dict(state)))


def test_detect_ma_touch_when_distance_is_within_threshold():
    result = bot.detect_ma_touch(100.04, 100.0, threshold_pct=0.05)

    assert result is not None
    assert result["distance_pct"] <= 0.05


def test_detect_ma_touch_returns_none_when_distance_exceeds_threshold():
    assert bot.detect_ma_touch(100.06, 100.0, threshold_pct=0.05) is None


def test_detect_ma_touch_reports_above_side():
    result = bot.detect_ma_touch(100.04, 100.0, threshold_pct=0.05)

    assert result["side"] == "above"


def test_detect_ma_touch_reports_below_side():
    result = bot.detect_ma_touch(99.96, 100.0, threshold_pct=0.05)

    assert result["side"] == "below"


def test_run_once_notifies_and_records_state_for_confirmed_ma_touch(monkeypatch):
    candles = _make_candles([100.0] * 80)
    posted = []
    saved = []
    _patch_common(monkeypatch, candles, posted, saved)

    state = {}
    bot.run_once(state, dry_run=False)

    candle_time = candles.index[-2].isoformat()
    key = bot.state_key("USD_JPY", "M15", "sma20", candle_time)
    assert len(posted) == 1
    assert posted[0][0]["title"] == "Berial Market Watch: MA Touch"
    assert "This is a watch alert only, not a trade decision." in posted[0][0]["description"]
    assert key in state
    assert state[key]["candle_time"] == candle_time
    assert state[key]["ma_name"] == "sma20"
    assert state[key]["side"] == "above"
    assert saved


def test_run_once_does_not_duplicate_same_candle_and_ma(monkeypatch):
    candles = _make_candles([100.0] * 80)
    posted = []
    saved = []
    _patch_common(monkeypatch, candles, posted, saved)

    state = {}
    bot.run_once(state, dry_run=False)
    bot.run_once(state, dry_run=False)

    assert len(posted) == 1


def test_run_once_dry_run_does_not_save_state(monkeypatch):
    candles = _make_candles([100.0] * 80)
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
    # The confirmed candle at -2 is not touching SMA20, while the latest
    # possibly-forming candle at -1 is touching. A notification would mean the
    # bot accidentally evaluated the unconfirmed latest candle.
    candles = _make_candles([100.0] * 78 + [101.0, 100.052])
    posted = []
    saved = []
    _patch_common(monkeypatch, candles, posted, saved)

    bot.run_once({}, dry_run=False)

    assert posted == []
    assert saved == []


def test_load_config_reads_ma_alert_section(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[monitor]
symbol = "USD_JPY"
ticker = "JPY=X"
timeframe = "M15"
interval = "15m"
period = "5d"

[ma_alert]
enabled = true
threshold_pct = 0.04
ma_names = ["sma20", "ema20"]

[runtime]
check_interval_seconds = 30

[logging]
log_level = "DEBUG"
""",
        encoding="utf-8",
    )

    config = bot.load_config(config_path)

    assert config.symbol == "USD_JPY"
    assert config.threshold_pct == 0.04
    assert config.ma_names == ["sma20", "ema20"]
    assert config.check_interval_seconds == 30
    assert config.log_level == "DEBUG"
