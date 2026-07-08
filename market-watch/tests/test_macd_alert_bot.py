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


def test_compute_macd_returns_macd_and_signal_lines():
    close = _make_close_series([100 + i * 0.1 for i in range(60)])

    macd_line, signal_line = bot.compute_macd(close, fast=12, slow=26, signal=9)

    assert len(macd_line) == len(close)
    assert len(signal_line) == len(close)


def test_compute_macd_uses_shared_indicator_function(monkeypatch):
    close = _make_close_series([100 + i * 0.1 for i in range(60)])
    calls = []

    def fake_calculate_macd(close_series, *, fast=12, slow=26, signal=9):
        calls.append((fast, slow, signal))
        return close_series, close_series + 1, close_series + 2

    monkeypatch.setattr(bot, "calculate_macd", fake_calculate_macd)

    macd_line, signal_line = bot.compute_macd(close, fast=1, slow=2, signal=3)

    assert calls == [(1, 2, 3)]
    assert macd_line.equals(close)
    assert signal_line.equals(close + 1)


def test_run_once_notifies_and_records_state_on_cross(monkeypatch):
    # 40 flat bars (diff == 0) then one bar that pushes MACD above the signal
    # line, then one more (still-forming, excluded) bar. The cross lands
    # exactly on the "confirmed" candle at index -2.
    values = [100.0] * 40 + [100.5, 100.5]
    candles = _make_candles(values)

    monkeypatch.setattr(bot, "fetch_candles", lambda: candles)

    posted = []
    monkeypatch.setattr(bot, "post_discord_embed", lambda embed, dry_run=False, image_url=None: posted.append(embed))
    monkeypatch.setattr(bot, "save_state", lambda state: None)

    state = {}
    bot.run_once(state, dry_run=False)

    assert len(posted) == 1
    assert posted[0]["title"] == "Berial Market Watch: MACD Golden Cross"
    assert "Signal" in [field["name"] for field in posted[0]["fields"]]
    assert bot.state_key() in state
    assert state[bot.state_key()]["signal"] == "bullish"
    assert state[bot.state_key()]["candle_time"] == candles.index[-2].isoformat()


def test_run_once_does_not_duplicate_notification_for_same_candle(monkeypatch):
    values = [100.0] * 40 + [100.5, 100.5]
    candles = _make_candles(values)

    monkeypatch.setattr(bot, "fetch_candles", lambda: candles)

    posted = []
    monkeypatch.setattr(bot, "post_discord_embed", lambda embed, dry_run=False, image_url=None: posted.append(embed))
    monkeypatch.setattr(bot, "save_state", lambda state: None)

    state = {}
    bot.run_once(state, dry_run=False)
    bot.run_once(state, dry_run=False)

    assert len(posted) == 1


def test_run_once_does_not_duplicate_after_restart_style_state(monkeypatch):
    values = [100.0] * 40 + [100.5, 100.5]
    close = _make_close_series(values)

    monkeypatch.setattr(bot, "fetch_candles", lambda: close)

    posted = []
    monkeypatch.setattr(bot, "post_discord_embed", lambda embed, dry_run=False, image_url=None: posted.append(embed))

    state = {
        bot.state_key(): {
            "candle_time": close.index[-2].isoformat(),
            "signal": "bullish",
        }
    }
    bot.run_once(state, dry_run=False)

    assert posted == []


def test_run_once_skips_when_not_enough_candles(monkeypatch, caplog):
    close = _make_close_series([100.0] * 5)
    monkeypatch.setattr(bot, "fetch_candles", lambda: close)

    posted = []
    monkeypatch.setattr(bot, "post_discord_embed", lambda embed, dry_run=False, image_url=None: posted.append(embed))

    bot.run_once({}, dry_run=False)

    assert posted == []


def test_dry_run_does_not_call_discord_http(monkeypatch):
    class FailingSession:
        def post(self, *args, **kwargs):
            raise AssertionError("HTTP post must not be called in dry-run")

    monkeypatch.setattr(bot.requests, "post", FailingSession().post)

    bot.post_discord_embed({"title": "test"}, dry_run=True)


def test_post_discord_embed_sets_presigned_image_url(monkeypatch):
    posted = {}

    class Response:
        def raise_for_status(self):
            return None

    def fake_post(url, *, json=None, timeout=None):
        posted["url"] = url
        posted["json"] = json
        posted["timeout"] = timeout
        return Response()

    monkeypatch.setattr(bot, "DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(bot.requests, "post", fake_post)

    bot.post_discord_embed(
        {"title": "test"},
        image_url="https://presigned.invalid/charts/USDJPY/1h/snapshot.png?X-Amz-Signature=test",
    )

    assert posted["json"]["embeds"][0]["image"]["url"].startswith("https://presigned.invalid/")


def test_notify_cross_passes_ohlc_to_chart_image(monkeypatch):
    candles = pd.DataFrame(
        {
            "open": [99.5, 100.5],
            "high": [100.5, 101.5],
            "low": [99.0, 100.0],
            "close": [100.0, 101.0],
            "volume": [100.0, 120.0],
        },
        index=pd.date_range("2026-07-08", periods=2, freq="15min", tz="UTC"),
    )
    captured = {}

    def fake_prepare_alert_image(**kwargs):
        captured["columns"] = list(kwargs["candles"].columns)
        return "snapshot-1", "https://presigned.invalid/chart.png"

    posted = []
    monkeypatch.setattr(bot, "prepare_alert_image", fake_prepare_alert_image)
    monkeypatch.setattr(bot, "post_discord_embed", lambda embed, dry_run=False, image_url=None: posted.append((embed, image_url)))

    bot.notify_cross("bullish", candles.index[-1], 101.0, 0.1, 0.05, candles=candles)

    assert captured["columns"][:4] == ["open", "high", "low", "close"]
    assert posted[0][1] == "https://presigned.invalid/chart.png"


def test_validate_config_requires_webhook_unless_dry_run(monkeypatch):
    monkeypatch.setattr(bot, "DISCORD_WEBHOOK_URL", "")

    with pytest.raises(RuntimeError):
        bot.validate_config(dry_run=False)

    bot.validate_config(dry_run=True)


def test_validate_config_rejects_unsupported_symbol(monkeypatch):
    monkeypatch.setattr(bot, "DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
    monkeypatch.setattr(bot, "SYMBOL", "")

    with pytest.raises(RuntimeError):
        bot.validate_config(dry_run=False)


def test_state_round_trips_through_disk(tmp_path, monkeypatch):
    state_path = tmp_path / "macd_alert_state.json"
    monkeypatch.setattr(bot, "STATE_DIR", tmp_path)
    monkeypatch.setattr(bot, "STATE_FILE", state_path)

    bot.save_state({"USD_JPY_M15": {"candle_time": "2026-07-01T00:00:00+00:00", "signal": "bullish"}})
    loaded = bot.load_state()

    assert loaded == {"USD_JPY_M15": {"candle_time": "2026-07-01T00:00:00+00:00", "signal": "bullish"}}


def test_load_config_reads_public_config_file(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[monitor]
symbol = "USD_JPY"
ticker = "JPY=X"
timeframe = "M15"
interval = "15m"
period = "5d"

[macd]
macd_fast = 12
macd_slow = 26
macd_signal = 9

[runtime]
check_interval_seconds = 30

[logging]
log_level = "DEBUG"
""",
        encoding="utf-8",
    )

    config = bot.load_config(config_path)

    assert config.symbol == "USD_JPY"
    assert config.ticker == "JPY=X"
    assert config.check_interval_seconds == 30
    assert config.log_level == "DEBUG"


def test_load_config_reports_invalid_toml(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("[monitor\nsymbol = 'USD_JPY'", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Failed to parse config file"):
        bot.load_config(config_path)
