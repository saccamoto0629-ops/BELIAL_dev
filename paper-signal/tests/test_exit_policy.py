import json
import pathlib
import sys

import pandas as pd
import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import exit_policy as ep  # noqa: E402


def make_candles(closes, *, start="2026-01-01T00:00:00Z", freq="1h", high_offset=0.0, low_offset=0.0):
    index = pd.date_range(start, periods=len(closes), freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + high_offset for c in closes],
            "low": [c - low_offset for c in closes],
            "close": closes,
        },
        index=index,
    )


def long_signal(**overrides):
    signal = {"direction": "long", "entry_price": 150.0}
    signal.update(overrides)
    return signal


def short_signal(**overrides):
    signal = {"direction": "short", "entry_price": 150.0}
    signal.update(overrides)
    return signal


def make_config(**overrides):
    """A resolved (already-validated) config, for tests that pass a full config directly."""
    config = ep.default_exit_policy_config()
    for key, value in overrides.items():
        if key == "policies":
            for policy_name, policy_overrides in value.items():
                config["policies"].setdefault(policy_name, {}).update(policy_overrides)
        else:
            config[key] = value
    return config


# =============================================================================
# JSON config: default_exit_policy_config / load_exit_policy_config /
# validate_exit_policy_config / resolve_exit_policy_config
# =============================================================================


def test_default_exit_policy_config_is_itself_valid():
    config = ep.default_exit_policy_config()

    ep.validate_exit_policy_config(config)  # must not raise
    assert config["exit_policy"] == "fixed_rr"
    assert config["same_candle_priority"] == "stop_loss"


def test_load_exit_policy_config_reads_a_temp_json_file(tmp_path):
    config_path = tmp_path / "exit_policy.json"
    config_path.write_text(
        json.dumps(
            {
                "exit_policy": "time_based_close",
                "same_candle_priority": "take_profit",
                "policies": {"time_based_close": {"enabled": True, "max_hold_bars": 10}},
            }
        ),
        encoding="utf-8",
    )

    config = ep.load_exit_policy_config(config_path)

    assert config["exit_policy"] == "time_based_close"
    assert config["same_candle_priority"] == "take_profit"
    assert config["policies"]["time_based_close"]["max_hold_bars"] == 10
    # missing keys/policies are default-filled
    assert config["policies"]["fixed_rr"]["risk_reward"] == 2.0


def test_load_exit_policy_config_reads_only_the_path_from_env(tmp_path, monkeypatch):
    config_path = tmp_path / "exit_policy.json"
    config_path.write_text(
        json.dumps(
            {"exit_policy": "fixed_rr", "policies": {"fixed_rr": {"enabled": True, "risk_reward": 3.0}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BACKTEST_EXIT_CONFIG_PATH", str(config_path))

    config = ep.load_exit_policy_config()

    assert config["exit_policy"] == "fixed_rr"
    assert config["policies"]["fixed_rr"]["risk_reward"] == 3.0


def test_load_exit_policy_config_switches_active_policy_via_json():
    partial = {"exit_policy": "ma_break_invalidation", "policies": {"ma_break_invalidation": {"enabled": True}}}

    config = ep.resolve_exit_policy_config(partial)

    assert config["exit_policy"] == "ma_break_invalidation"


def test_load_exit_policy_config_falls_back_to_default_when_no_path_anywhere(monkeypatch):
    monkeypatch.delenv("BACKTEST_EXIT_CONFIG_PATH", raising=False)

    config = ep.load_exit_policy_config(env_values={})

    assert config == ep.default_exit_policy_config()


def test_load_exit_policy_config_raises_file_not_found_for_a_configured_missing_path(tmp_path):
    missing = tmp_path / "does_not_exist.json"

    with pytest.raises(FileNotFoundError):
        ep.load_exit_policy_config(missing)


def test_load_exit_policy_config_raises_clear_error_for_invalid_json(tmp_path):
    config_path = tmp_path / "broken.json"
    config_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ValueError, match="not valid JSON"):
        ep.load_exit_policy_config(config_path)


def test_resolve_exit_policy_config_fills_missing_values_from_defaults():
    partial = {"policies": {"fixed_rr": {"risk_reward": 5.0}}}

    resolved = ep.resolve_exit_policy_config(partial)

    assert resolved["policies"]["fixed_rr"]["risk_reward"] == 5.0
    assert resolved["policies"]["fixed_rr"]["fallback_stop_pct"] == 0.003  # filled from defaults
    assert resolved["exit_policy"] == "fixed_rr"  # top-level default filled in
    assert resolved["policies"]["time_based_close"]["max_hold_bars"] == 48  # whole block defaulted


def test_resolve_exit_policy_config_none_returns_default():
    assert ep.resolve_exit_policy_config(None) == ep.default_exit_policy_config()


# --- validation: unknown exit_policy -----------------------------------------


def test_unknown_exit_policy_raises_value_error():
    with pytest.raises(ValueError, match="Unknown exit_policy"):
        ep.validate_exit_policy_config(make_config(exit_policy="not_a_real_policy"))


def test_evaluate_exit_with_unknown_exit_policy_config_raises_value_error():
    candles = make_candles([150.0], high_offset=0.1, low_offset=0.1)

    with pytest.raises(ValueError, match="Unknown exit_policy"):
        ep.evaluate_exit(long_signal(), candles, {"exit_policy": "not_a_real_policy"})


# --- validation: disabled policy selected ------------------------------------


def test_selecting_a_disabled_policy_raises_value_error():
    config = make_config(exit_policy="ma_break_invalidation")
    config["policies"]["ma_break_invalidation"]["enabled"] = False

    with pytest.raises(ValueError, match="enabled"):
        ep.validate_exit_policy_config(config)


def test_evaluate_exit_with_disabled_policy_raises_value_error():
    candles = make_candles([150.0], high_offset=0.1, low_offset=0.1)
    config = {"exit_policy": "ma_break_invalidation"}  # defaults to enabled: false

    with pytest.raises(ValueError, match="enabled"):
        ep.evaluate_exit(long_signal(), candles, config)


# --- validation: same_candle_priority ----------------------------------------


def test_invalid_same_candle_priority_raises_value_error():
    with pytest.raises(ValueError, match="same_candle_priority"):
        ep.validate_exit_policy_config(make_config(same_candle_priority="whichever"))


# --- validation: ma_type ------------------------------------------------------


def test_invalid_ma_type_raises_value_error():
    config = make_config(policies={"ma_break_invalidation": {"ma_type": "ema"}})

    with pytest.raises(ValueError, match="ma_type"):
        ep.validate_exit_policy_config(config)


# --- validation: time_based_close.close_as -----------------------------------


def test_close_as_expired_is_accepted():
    config = make_config(policies={"time_based_close": {"close_as": "expired"}})

    ep.validate_exit_policy_config(config)  # must not raise


def test_close_as_closed_is_accepted():
    config = make_config(policies={"time_based_close": {"close_as": "closed"}})

    ep.validate_exit_policy_config(config)  # must not raise


def test_close_as_omitted_defaults_to_expired():
    config = ep.resolve_exit_policy_config({"policies": {"time_based_close": {"enabled": True}}})

    assert config["policies"]["time_based_close"]["close_as"] == "expired"


def test_close_as_invalid_value_raises_value_error():
    config = make_config(policies={"time_based_close": {"close_as": "banana"}})

    with pytest.raises(ValueError, match="close_as"):
        ep.validate_exit_policy_config(config)


def test_close_as_invalid_value_message_names_the_bad_value_and_allowed_values():
    config = make_config(policies={"time_based_close": {"close_as": "banana"}})

    with pytest.raises(ValueError) as excinfo:
        ep.validate_exit_policy_config(config)

    message = str(excinfo.value)
    assert "banana" in message
    assert "expired" in message
    assert "closed" in message


def test_close_as_invalid_value_never_reaches_evaluate_exit_result(tmp_path):
    # resolve_exit_policy_config() (called by evaluate_exit() on every
    # invocation) validates close_as before any policy evaluator runs, so
    # a bad close_as can never leak into an ExitPolicyResult.status.
    candles = make_candles([150.0, 150.1, 150.2], high_offset=0.05, low_offset=0.05)
    config = make_config(
        exit_policy="time_based_close",
        policies={"time_based_close": {"max_hold_bars": 2, "close_as": "banana"}},
    )

    with pytest.raises(ValueError, match="close_as"):
        ep.evaluate_exit(long_signal(), candles, config)


def test_close_as_invalid_value_never_reaches_backtest_signal_record(monkeypatch):
    monkeypatch.syspath_prepend(str(BOT_DIR.parent / "backtest"))
    import backtest_models

    config = make_config(policies={"time_based_close": {"close_as": "banana"}})

    with pytest.raises(ValueError, match="close_as"):
        ep.validate_exit_policy_config(config)

    # No ExitPolicyResult was ever produced, so nothing to map -- confirm
    # the mapper itself only ever forwards whatever it is given, and that a
    # valid status (from an allowed close_as) is what actually reaches the
    # persisted BacktestSignalRecord.
    signal_record = backtest_models.create_backtest_signal_record(
        "run-1", signal_time="2026-01-01T00:00:00Z", symbol="USD_JPY", timeframe="1h"
    )
    valid_result = ep.evaluate_exit(
        long_signal(),
        make_candles([150.0, 150.1, 150.2], high_offset=0.05, low_offset=0.05),
        make_config(exit_policy="time_based_close", policies={"time_based_close": {"max_hold_bars": 2}}),
    )
    updated = backtest_models.apply_exit_policy_result(signal_record, valid_result)

    assert updated["result_status"] in ep.ALLOWED_TIME_BASED_CLOSE_STATUSES
    assert updated["result_status"] != "banana"


# --- validation: numeric fields ----------------------------------------------


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"policies": {"fixed_rr": {"risk_reward": 0}}}, "risk_reward"),
        ({"policies": {"fixed_rr": {"risk_reward": -1.0}}}, "risk_reward"),
        ({"policies": {"fixed_rr": {"fallback_stop_pct": 0}}}, "fallback_stop_pct"),
        ({"policies": {"fixed_rr": {"fallback_stop_pct": -0.01}}}, "fallback_stop_pct"),
        ({"policies": {"time_based_close": {"max_hold_bars": 0}}}, "max_hold_bars"),
        ({"policies": {"time_based_close": {"max_hold_bars": -5}}}, "max_hold_bars"),
        ({"policies": {"ma_break_invalidation": {"period": 0}}}, "period"),
        ({"policies": {"ma_break_invalidation": {"period": -20}}}, "period"),
    ],
)
def test_invalid_numeric_values_raise_value_error(overrides, match):
    config = make_config(**overrides)

    with pytest.raises(ValueError, match=match):
        ep.validate_exit_policy_config(config)


# =============================================================================
# fixed_rr
# =============================================================================


def test_fixed_rr_long_reaches_take_profit():
    candles = make_candles([150.5, 151.0, 152.5], high_offset=0.5, low_offset=0.3)
    config = make_config(policies={"fixed_rr": {"fallback_stop_pct": 1.0 / 150.0, "risk_reward": 2.0}})

    result = ep.evaluate_exit(long_signal(), candles, config)

    assert result.status == "win"
    assert result.close_reason == "take_profit"
    assert result.close_price == pytest.approx(152.0)
    assert result.close_time is not None
    assert result.exit_policy == "fixed_rr"


def test_fixed_rr_short_reaches_take_profit():
    candles = make_candles([149.5, 149.0, 147.5], high_offset=0.3, low_offset=0.5)
    config = make_config(policies={"fixed_rr": {"fallback_stop_pct": 1.0 / 150.0, "risk_reward": 2.0}})

    result = ep.evaluate_exit(short_signal(), candles, config)

    assert result.status == "win"
    assert result.close_reason == "take_profit"
    assert result.close_price == pytest.approx(148.0)


def test_fixed_rr_long_reaches_stop_loss():
    candles = make_candles([149.5, 149.0, 148.5], high_offset=0.2, low_offset=0.6)
    config = make_config(policies={"fixed_rr": {"fallback_stop_pct": 1.0 / 150.0, "risk_reward": 2.0}})

    result = ep.evaluate_exit(long_signal(), candles, config)

    assert result.status == "loss"
    assert result.close_reason == "stop_loss"
    assert result.close_price == pytest.approx(149.0)
    assert result.close_time is not None
    assert result.exit_policy == "fixed_rr"


def test_fixed_rr_short_reaches_stop_loss():
    candles = make_candles([150.5, 151.0, 151.5], high_offset=0.6, low_offset=0.2)
    config = make_config(policies={"fixed_rr": {"fallback_stop_pct": 1.0 / 150.0, "risk_reward": 2.0}})

    result = ep.evaluate_exit(short_signal(), candles, config)

    assert result.status == "loss"
    assert result.close_reason == "stop_loss"
    assert result.close_price == pytest.approx(151.0)


def test_fixed_rr_uses_signal_level_prices_when_present():
    candles = make_candles([150.5, 151.0, 152.5], high_offset=0.5, low_offset=0.3)
    signal = long_signal(invalidation_price=148.0, take_profit_price=152.0)
    config = make_config()  # no fallback_stop_pct needed -- signal already has prices

    result = ep.evaluate_exit(signal, candles, config)

    assert result.status == "win"
    assert result.close_price == pytest.approx(152.0)


def test_fixed_rr_requires_fallback_stop_pct_when_signal_has_no_prices():
    # Explicit None, not a deleted key: evaluate_exit() always merges the
    # given config onto defaults, so a merely-absent key would silently be
    # backfilled with the default fallback_stop_pct. An explicit None
    # survives the merge (dict.update overwrites regardless of value) and
    # is what validate_exit_policy_config() intentionally still allows
    # through -- fallback_stop_pct is only required at evaluation time, and
    # only when the signal itself has no invalidation_price/take_profit_price.
    candles = make_candles([150.0], high_offset=0.1, low_offset=0.1)
    config = make_config(policies={"fixed_rr": {"fallback_stop_pct": None}})

    with pytest.raises(ValueError, match="fallback_stop_pct"):
        ep.evaluate_exit(long_signal(), candles, config)


# --- same_candle_priority ------------------------------------------------------


def test_same_candle_priority_stop_loss_wins_long():
    # One wide candle: low touches stop, high touches take-profit.
    candles = pd.DataFrame(
        {"open": [150.0], "high": [153.0], "low": [148.0], "close": [150.5]},
        index=pd.date_range("2026-01-01T00:00:00Z", periods=1, freq="1h", tz="UTC"),
    )
    config = make_config(
        same_candle_priority="stop_loss",
        policies={"fixed_rr": {"fallback_stop_pct": 1.0 / 150.0, "risk_reward": 2.0}},
    )

    result = ep.evaluate_exit(long_signal(), candles, config)

    assert result.status == "loss"
    assert result.close_reason == "stop_loss"


def test_same_candle_priority_take_profit_wins_long():
    candles = pd.DataFrame(
        {"open": [150.0], "high": [153.0], "low": [148.0], "close": [150.5]},
        index=pd.date_range("2026-01-01T00:00:00Z", periods=1, freq="1h", tz="UTC"),
    )
    config = make_config(
        same_candle_priority="take_profit",
        policies={"fixed_rr": {"fallback_stop_pct": 1.0 / 150.0, "risk_reward": 2.0}},
    )

    result = ep.evaluate_exit(long_signal(), candles, config)

    assert result.status == "win"
    assert result.close_reason == "take_profit"


def test_same_candle_priority_stop_loss_wins_short():
    candles = pd.DataFrame(
        {"open": [150.0], "high": [153.0], "low": [148.0], "close": [149.5]},
        index=pd.date_range("2026-01-01T00:00:00Z", periods=1, freq="1h", tz="UTC"),
    )
    config = make_config(
        same_candle_priority="stop_loss",
        policies={"fixed_rr": {"fallback_stop_pct": 1.0 / 150.0, "risk_reward": 2.0}},
    )

    result = ep.evaluate_exit(short_signal(), candles, config)

    assert result.status == "loss"
    assert result.close_reason == "stop_loss"


def test_same_candle_priority_take_profit_wins_short():
    candles = pd.DataFrame(
        {"open": [150.0], "high": [153.0], "low": [148.0], "close": [149.5]},
        index=pd.date_range("2026-01-01T00:00:00Z", periods=1, freq="1h", tz="UTC"),
    )
    config = make_config(
        same_candle_priority="take_profit",
        policies={"fixed_rr": {"fallback_stop_pct": 1.0 / 150.0, "risk_reward": 2.0}},
    )

    result = ep.evaluate_exit(short_signal(), candles, config)

    assert result.status == "win"
    assert result.close_reason == "take_profit"


# =============================================================================
# time_based_close
# =============================================================================


def test_time_based_close_expires_after_max_hold_bars():
    candles = make_candles([150.0, 150.1, 150.2, 150.3], high_offset=0.05, low_offset=0.05)
    config = make_config(exit_policy="time_based_close", policies={"time_based_close": {"max_hold_bars": 3}})

    result = ep.evaluate_exit(long_signal(), candles, config)

    assert result.status == "expired"
    assert result.close_reason == "time_expired"
    assert result.close_time is not None
    assert result.exit_policy == "time_based_close"


def test_time_based_close_expires_at_the_configured_bar_count():
    candles = make_candles([150.0, 150.1, 150.2, 150.3], high_offset=0.05, low_offset=0.05)
    config = make_config(exit_policy="time_based_close", policies={"time_based_close": {"max_hold_bars": 3}})

    result = ep.evaluate_exit(long_signal(), candles, config)

    assert result.close_price == pytest.approx(150.2)  # close of the 3rd candle


def test_time_based_close_reports_configured_close_as_status():
    candles = make_candles([150.0, 150.1, 150.2], high_offset=0.05, low_offset=0.05)
    config = make_config(
        exit_policy="time_based_close",
        policies={"time_based_close": {"max_hold_bars": 2, "close_as": "closed"}},
    )

    result = ep.evaluate_exit(long_signal(), candles, config)

    assert result.status == "closed"
    assert result.close_reason == "time_expired"


def test_time_based_close_still_open_when_candles_run_out_first():
    candles = make_candles([150.0, 150.1], high_offset=0.05, low_offset=0.05)
    config = make_config(exit_policy="time_based_close", policies={"time_based_close": {"max_hold_bars": 10}})

    result = ep.evaluate_exit(long_signal(), candles, config)

    assert result.status == "open"
    assert result.close_reason == "no_close"
    assert result.close_price is None
    assert result.close_time is None


# =============================================================================
# ma_break_invalidation
# =============================================================================


def test_ma_break_invalidation_ma_type_sma_is_accepted():
    closes = [150.0, 150.5, 151.0, 151.5, 152.0, 152.5, 148.0]
    candles = make_candles(closes, high_offset=0.1, low_offset=0.1)
    config = make_config(
        exit_policy="ma_break_invalidation",
        policies={"ma_break_invalidation": {"enabled": True, "ma_type": "sma", "period": 5}},
    )

    result = ep.evaluate_exit(long_signal(), candles, config)

    assert result.status == "invalidated"
    assert result.close_reason == "ma_break"
    assert result.close_price == pytest.approx(148.0)
    assert result.exit_policy == "ma_break_invalidation"


def test_ma_break_invalidation_ma_type_ema_is_not_yet_supported():
    candles = make_candles([150.0, 150.5, 151.0, 151.5, 152.0, 152.5, 148.0], high_offset=0.1, low_offset=0.1)
    config = make_config(
        exit_policy="ma_break_invalidation",
        policies={"ma_break_invalidation": {"enabled": True, "ma_type": "ema", "period": 5}},
    )

    with pytest.raises(ValueError, match="ma_type"):
        ep.evaluate_exit(long_signal(), candles, config)


def test_ma_break_invalidation_period_is_read_from_config():
    # period=3 has enough history to compute an MA and invalidate; period=20
    # never reaches min_periods with only 6 candles, so it stays "open" --
    # proving the JSON-configured period actually drives the calculation.
    closes = [150.0, 150.2, 149.8, 150.1, 149.9, 148.0]
    candles = make_candles(closes, high_offset=0.1, low_offset=0.1)
    config = make_config(
        exit_policy="ma_break_invalidation",
        policies={"ma_break_invalidation": {"enabled": True, "ma_type": "sma", "period": 3}},
    )

    result = ep.evaluate_exit(long_signal(), candles, config)

    assert result.status == "invalidated"

    config_needs_more_history = make_config(
        exit_policy="ma_break_invalidation",
        policies={"ma_break_invalidation": {"enabled": True, "ma_type": "sma", "period": 20}},
    )
    result_insufficient = ep.evaluate_exit(long_signal(), candles, config_needs_more_history)
    assert result_insufficient.status == "open"  # period=20 never reaches min_periods with only 6 candles


def test_ma_break_invalidation_closes_short_on_close_above_ma():
    closes = [150.0, 149.5, 149.0, 148.5, 148.0, 147.5, 152.0]
    candles = make_candles(closes, high_offset=0.1, low_offset=0.1)
    config = make_config(
        exit_policy="ma_break_invalidation",
        policies={"ma_break_invalidation": {"enabled": True, "ma_type": "sma", "period": 5}},
    )

    result = ep.evaluate_exit(short_signal(), candles, config)

    assert result.status == "invalidated"
    assert result.close_reason == "ma_break"
    assert result.close_price == pytest.approx(152.0)


# =============================================================================
# fields always populated on close / no_close
# =============================================================================


def test_close_price_time_reason_and_exit_policy_are_all_populated_on_close():
    candles = make_candles([151.0, 152.5], high_offset=0.5, low_offset=0.3)
    config = make_config(policies={"fixed_rr": {"fallback_stop_pct": 1.0 / 150.0, "risk_reward": 2.0}})

    result = ep.evaluate_exit(long_signal(), candles, config)

    assert result.status == "win"
    assert result.close_price is not None
    assert result.close_time is not None
    assert isinstance(result.close_time, str)
    assert result.close_reason == "take_profit"
    assert result.exit_policy == "fixed_rr"


def test_no_close_result_has_open_status_and_no_close_reason():
    candles = make_candles([150.0, 150.05], high_offset=0.01, low_offset=0.01)
    config = make_config(policies={"fixed_rr": {"fallback_stop_pct": 5.0 / 150.0, "risk_reward": 2.0}})

    result = ep.evaluate_exit(long_signal(), candles, config)

    assert result.status == "open"
    assert result.close_price is None
    assert result.close_time is None
    assert result.close_reason == "no_close"
    assert result.exit_policy == "fixed_rr"


def test_mfe_and_mae_are_tracked_for_fixed_rr():
    candles = make_candles([151.0, 152.5], high_offset=0.5, low_offset=0.3)
    config = make_config(policies={"fixed_rr": {"fallback_stop_pct": 1.0 / 150.0, "risk_reward": 2.0}})

    result = ep.evaluate_exit(long_signal(), candles, config)

    assert result.mfe is not None and result.mfe >= 0
    assert result.mae is not None and result.mae <= 0


# =============================================================================
# validation / safety
# =============================================================================


def test_invalid_direction_raises_value_error():
    candles = make_candles([150.0], high_offset=0.1, low_offset=0.1)
    signal = {"direction": "sideways", "entry_price": 150.0}
    config = make_config(policies={"fixed_rr": {"fallback_stop_pct": 1.0 / 150.0}})

    with pytest.raises(ValueError, match="direction"):
        ep.evaluate_exit(signal, candles, config)


def test_missing_entry_price_raises_value_error():
    candles = make_candles([150.0], high_offset=0.1, low_offset=0.1)
    signal = {"direction": "long"}
    config = make_config(policies={"fixed_rr": {"fallback_stop_pct": 1.0 / 150.0}})

    with pytest.raises(ValueError, match="entry_price"):
        ep.evaluate_exit(signal, candles, config)


def test_empty_candles_returns_open_no_close_without_raising():
    empty = pd.DataFrame(columns=["open", "high", "low", "close"])
    config = make_config(policies={"fixed_rr": {"fallback_stop_pct": 1.0 / 150.0}})

    result = ep.evaluate_exit(long_signal(), empty, config)

    assert result.status == "open"
    assert result.close_reason == "no_close"


def test_none_candles_returns_open_no_close_without_raising():
    config = make_config(policies={"fixed_rr": {"fallback_stop_pct": 1.0 / 150.0}})

    result = ep.evaluate_exit(long_signal(), None, config)

    assert result.status == "open"
    assert result.close_reason == "no_close"


def test_candles_missing_required_columns_raise_clear_value_error():
    incomplete = pd.DataFrame(
        {"close": [150.0, 150.5]}, index=pd.date_range("2026-01-01", periods=2, freq="1h", tz="UTC")
    )
    config = make_config(policies={"fixed_rr": {"fallback_stop_pct": 1.0 / 150.0}})

    with pytest.raises(ValueError, match="high"):
        ep.evaluate_exit(long_signal(), incomplete, config)


def test_evaluate_exit_uses_default_config_when_none_given():
    candles = make_candles([151.0, 152.5], high_offset=0.5, low_offset=0.3)

    result = ep.evaluate_exit(long_signal(), candles, None)

    assert result.exit_policy == "fixed_rr"  # DEFAULT_EXIT_POLICY


# =============================================================================
# module never touches broker/execution/db/discord, and .env holds only a path
# =============================================================================


def test_module_source_has_no_broker_or_persistence_side_effects():
    source = pathlib.Path(ep.__file__).read_text(encoding="utf-8")
    import_lines = "\n".join(
        line for line in source.splitlines() if line.strip().startswith(("import ", "from "))
    ).lower()
    for forbidden in ("oanda", "mt5", "metatrader", "broker", "requests", "sqlite3", "paper_signal_store"):
        assert forbidden not in import_lines, f"unexpected dependency: {forbidden!r}"


def test_module_source_does_not_read_a_decision_value_env_var_directly():
    # Only BACKTEST_EXIT_CONFIG_PATH (a file path) may be read from the
    # environment. Policy names/parameters must come from the JSON file.
    source = pathlib.Path(ep.__file__).read_text(encoding="utf-8")
    assert "PAPER_SIGNAL_EXIT_POLICY" not in source
    getenv_lines = [line for line in source.splitlines() if "os.getenv(" in line or "os.environ" in line]
    assert all("BACKTEST_EXIT_CONFIG_PATH" in line for line in getenv_lines)
