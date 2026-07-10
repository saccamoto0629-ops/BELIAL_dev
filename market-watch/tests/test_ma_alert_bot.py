import pathlib
import sys

import pandas as pd

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import ma_alert_bot as bot  # noqa: E402


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
    monkeypatch.setattr(bot, "MA_NAMES", ["sma20"])
    monkeypatch.setattr(bot, "THRESHOLD_PCT", 0.05)
    monkeypatch.setattr(bot, "fetch_candles", lambda: candles)
    monkeypatch.setattr(bot, "prepare_alert_image", lambda **kwargs: (None, None))
    monkeypatch.setattr(bot, "post_discord_embed", lambda embed, dry_run=False, image_url=None: posted.append((embed, dry_run)))
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


def test_notify_touch_passes_ohlc_to_chart_image(monkeypatch):
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
    event = bot.build_touch_event("sma20", candles.index[-1].isoformat(), 101.0, 101.0, {"distance_pct": 0.0, "side": "above"})
    captured = {}
    posted = []

    def fake_prepare_alert_image(**kwargs):
        captured["columns"] = list(kwargs["candles"].columns)
        return "snapshot-1", "https://presigned.invalid/chart.png"

    monkeypatch.setattr(bot, "prepare_alert_image", fake_prepare_alert_image)
    monkeypatch.setattr(bot, "post_discord_embed", lambda embed, dry_run=False, image_url=None: posted.append((embed, image_url)))

    bot.notify_touch(event, candles=candles)

    assert captured["columns"][:4] == ["open", "high", "low", "close"]
    assert posted[0][1] == "https://presigned.invalid/chart.png"


def test_debug_render_ma_touch_uses_alert_renderer(monkeypatch, tmp_path):
    candles = _make_candles([100.0] * 80)
    captured = {}

    def fake_render_chart_snapshot(candles_arg, output_path, **kwargs):
        captured["columns"] = list(candles_arg.columns)
        captured["source"] = kwargs["source"]
        captured["event_type"] = kwargs["event_type"]
        pathlib.Path(output_path).write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return pathlib.Path(output_path)

    monkeypatch.setattr(bot, "render_chart_snapshot", fake_render_chart_snapshot)

    rendered = bot.debug_render_ma_touch_image(tmp_path / "ma_touch.png", candles=candles)

    assert rendered == tmp_path / "ma_touch.png"
    assert captured["columns"][:4] == ["open", "high", "low", "close"]
    assert captured["source"] == "alert-debug"
    assert captured["event_type"] == "ma_touch"


def _touch_event(**overrides):
    base = {
        "symbol": "USD_JPY",
        "timeframe": "M15",
        "ma_name": "sma20",
        "ma_type": "sma",
        "ma_period": 20,
        "candle_time": "2026-07-01T00:00:00+00:00",
        "close": 100.0,
        "ma_value": 100.0,
        "distance_pct": 0.0,
        "side": "above",
        "notified_at": "2026-07-01T00:00:00+00:00",
        "detected_at": "2026-07-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


# --- Issue #16: raw detection vs. notification decision separation --------


def test_parse_ma_name_splits_type_and_period():
    assert bot.parse_ma_name("sma20") == ("sma", 20)
    assert bot.parse_ma_name("ema200") == ("ema", 200)


def test_parse_ma_name_falls_back_for_unexpected_format():
    assert bot.parse_ma_name("weird_name") == ("weird_name", None)


def test_zone_state_key_differs_by_symbol_timeframe_ma_type_period():
    keys = {
        bot.zone_state_key("USD_JPY", "M15", "sma", 20),
        bot.zone_state_key("EUR_USD", "M15", "sma", 20),
        bot.zone_state_key("USD_JPY", "H1", "sma", 20),
        bot.zone_state_key("USD_JPY", "M15", "ema", 20),
        bot.zone_state_key("USD_JPY", "M15", "sma", 75),
    }

    assert len(keys) == 5


def test_build_touch_event_includes_ma_type_and_period():
    event = bot.build_touch_event("ema20", "t", 100.0, 100.0, {"distance_pct": 0.0, "side": "above"})

    assert event["ma_type"] == "ema"
    assert event["ma_period"] == 20


def test_decide_ma_touch_notification_notifies_on_first_touch_from_outside():
    decision = bot.decide_ma_touch_notification(_touch_event(), {})

    assert decision.should_notify is True
    assert decision.reason is None
    assert decision.previous_status == "outside"
    assert decision.new_status == "touching"


def test_decide_ma_touch_notification_suppresses_while_still_touching():
    zone_key = bot.zone_state_key("USD_JPY", "M15", "sma", 20)
    state = {"touch_zones": {zone_key: {"status": "touching"}}}

    decision = bot.decide_ma_touch_notification(_touch_event(), state)

    assert decision.should_notify is False
    assert decision.reason == "same_touch_zone"


def test_decide_ma_touch_notification_is_pure_and_does_not_mutate_state():
    """save_event() must not live inside the decision function."""
    state = {"touch_zones": {}}

    bot.decide_ma_touch_notification(_touch_event(), state)

    assert state == {"touch_zones": {}}


def test_decide_ma_touch_notification_is_independent_per_symbol():
    zone_key = bot.zone_state_key("USD_JPY", "M15", "sma", 20)
    state = {"touch_zones": {zone_key: {"status": "touching"}}}
    other_symbol_event = _touch_event(symbol="EUR_USD")

    decision = bot.decide_ma_touch_notification(other_symbol_event, state)

    assert decision.should_notify is True


def test_update_state_if_left_touch_zone_resets_touching_to_outside():
    zone_key = bot.zone_state_key("USD_JPY", "M15", "sma", 20)
    state = {"touch_zones": {zone_key: {"status": "touching"}}}

    changed = bot.update_state_if_left_touch_zone(
        state,
        symbol="USD_JPY",
        timeframe="M15",
        ma_type="sma",
        ma_period=20,
        candle_time="2026-07-01T00:15:00+00:00",
    )

    assert changed is True
    assert state["touch_zones"][zone_key]["status"] == "outside"


def test_update_state_if_left_touch_zone_is_noop_when_already_outside():
    state = {}

    changed = bot.update_state_if_left_touch_zone(
        state,
        symbol="USD_JPY",
        timeframe="M15",
        ma_type="sma",
        ma_period=20,
        candle_time="2026-07-01T00:15:00+00:00",
    )

    assert changed is False
    assert state == {}


def test_update_state_if_left_touch_zone_respects_dry_run():
    zone_key = bot.zone_state_key("USD_JPY", "M15", "sma", 20)
    state = {"touch_zones": {zone_key: {"status": "touching"}}}

    changed = bot.update_state_if_left_touch_zone(
        state,
        symbol="USD_JPY",
        timeframe="M15",
        ma_type="sma",
        ma_period=20,
        candle_time="2026-07-01T00:15:00+00:00",
        dry_run=True,
    )

    assert changed is True
    assert state["touch_zones"][zone_key]["status"] == "touching"


def test_run_once_suppresses_consecutive_touch_in_same_zone(monkeypatch):
    posted = []
    saved = []
    state = {}

    first_candles = _make_candles([100.0] * 80)
    _patch_common(monkeypatch, first_candles, posted, saved)
    bot.run_once(state, dry_run=False)
    assert len(posted) == 1

    second_candles = _make_candles([100.0] * 81)
    monkeypatch.setattr(bot, "fetch_candles", lambda: second_candles)
    bot.run_once(state, dry_run=False)

    # Still touching the same MA zone: no second Discord notification.
    assert len(posted) == 1

    candle_time = second_candles.index[-2].isoformat()
    key = bot.state_key("USD_JPY", "M15", "sma20", candle_time)
    assert key in state
    assert state[key]["suppressed"] is True
    assert state[key]["suppress_reason"] == "same_touch_zone"
    # The raw touch fact is still fully recorded even though suppressed.
    assert state[key]["candle_time"] == candle_time
    assert state[key]["side"] == "above"


def test_run_once_notifies_again_after_leaving_and_retouching_zone(monkeypatch):
    posted = []
    saved = []
    state = {}

    block_a = [100.0] * 80
    block_b = [105.0] * 3  # far enough from SMA20 to clearly exit the zone
    block_c = [100.0] * 25  # flushes the spike out of the 20-period window

    first_candles = _make_candles(block_a)
    _patch_common(monkeypatch, first_candles, posted, saved)
    bot.run_once(state, dry_run=False)
    assert len(posted) == 1  # first touch: outside -> touching

    left_zone_candles = _make_candles(block_a + block_b)
    monkeypatch.setattr(bot, "fetch_candles", lambda: left_zone_candles)
    bot.run_once(state, dry_run=False)
    assert len(posted) == 1  # not touching: zone resets to outside, no notification

    zone_key = bot.zone_state_key("USD_JPY", "M15", "sma", 20)
    assert state["touch_zones"][zone_key]["status"] == "outside"
    assert saved[-1]["touch_zones"][zone_key]["status"] == "outside"

    retouch_candles = _make_candles(block_a + block_b + block_c)
    monkeypatch.setattr(bot, "fetch_candles", lambda: retouch_candles)
    bot.run_once(state, dry_run=False)

    # Re-entering the zone after fully leaving it notifies again.
    assert len(posted) == 2
    assert state["touch_zones"][zone_key]["status"] == "touching"


def test_run_once_treats_different_symbol_as_independent_zone(monkeypatch):
    posted = []
    saved = []
    state = {
        "touch_zones": {
            bot.zone_state_key("EUR_USD", "M15", "sma", 20): {"status": "touching"},
        }
    }

    candles = _make_candles([100.0] * 80)
    _patch_common(monkeypatch, candles, posted, saved)

    bot.run_once(state, dry_run=False)

    # USD_JPY's zone was never "touching", so this is a fresh first touch.
    assert len(posted) == 1


def test_run_once_dry_run_has_no_touch_zone_side_effects(monkeypatch):
    candles = _make_candles([100.0] * 80)
    posted = []
    saved = []
    _patch_common(monkeypatch, candles, posted, saved)

    state = {}
    bot.run_once(state, dry_run=True)
    bot.run_once(state, dry_run=True)

    # dry-run never persists state, so suppression can never kick in either;
    # both calls notify, and no touch_zones (or any other) state is written.
    assert len(posted) == 2
    assert state == {}
    assert saved == []


def test_suppressed_touch_still_logs_raw_event_and_decision(monkeypatch, caplog):
    posted = []
    saved = []
    state = {}

    first_candles = _make_candles([100.0] * 80)
    _patch_common(monkeypatch, first_candles, posted, saved)
    bot.run_once(state, dry_run=False)

    second_candles = _make_candles([100.0] * 81)
    monkeypatch.setattr(bot, "fetch_candles", lambda: second_candles)

    with caplog.at_level("INFO"):
        bot.run_once(state, dry_run=False)

    assert "MA touch raw event" in caplog.text
    assert "same_touch_zone" in caplog.text


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
