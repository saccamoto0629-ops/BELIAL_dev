"""Issue #53: CLI + execution-integration tests for external Strategy definitions.

Covers the runner-side contract (docs/architecture/strategy_definition.md
§5.1 / §5.2):

- --strategy-file / --strategy CLI wiring, their mutual exclusivity, and the
  exclusivity with the legacy --strategy-profile (all argparse exit code 2).
- Definition validation failures surface as clean pre-run errors with ZERO
  side effects (no Backtest DB created, no chart DB touched).
- The selected definition's variable fields actually drive the run: take
  profit R, max holding bars, symbols/timeframes conformance, and the
  normalized definition + metadata recorded in config_json / summary.
- Full backward compatibility: without --strategy-file/--strategy the
  external machinery (PyYAML import included) never starts, and a broken
  strategies directory cannot affect an ordinary run.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pandas as pd
import pytest
import yaml

TESTS_DIR = pathlib.Path(__file__).resolve().parent
BOT_DIR = TESTS_DIR.parent
REPO_ROOT = BOT_DIR.parent
MARKET_WATCH_DIR = REPO_ROOT / "market-watch"
PAPER_SIGNAL_DIR = REPO_ROOT / "paper-signal"
NEWS_FEED_BOT_DIR = REPO_ROOT / "news-feed-bot"

for _dir in (TESTS_DIR, BOT_DIR, MARKET_WATCH_DIR, PAPER_SIGNAL_DIR, NEWS_FEED_BOT_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import runner  # noqa: E402
import strategy_definition as sd  # noqa: E402
import squeeze_breakout as sb  # noqa: E402
import backtest_store as store  # noqa: E402
import exit_policy  # noqa: E402
import chart_collector as collector  # noqa: E402  (market-watch)
from chart_models import Candle  # noqa: E402  (market-watch)
from candle_fixtures import seed_chart_db, seed_bull_bias  # noqa: E402

# Same synthetic squeeze fixture as test_squeeze_breakout.py: 60 volatile bars,
# 34 flat bars (Market State "squeeze" from bar 74), so bar 94 is the sole
# episode-end / Trigger-evaluation bar.
START = "2026-06-01T00:00:00Z"
VOLATILE = 60
FLAT = 34
TRIGGER_INDEX = VOLATILE + FLAT  # 94


def _prefix(level=2000.0):
    bars = []
    for i in range(VOLATILE):
        c = level + (3 if i % 2 == 0 else -3)
        bars.append((c, c + 0.02, c - 0.02, c))
    for i in range(FLAT):
        c = level + (0.05 if i % 2 == 0 else -0.05)
        bars.append((c, c + 0.02, c - 0.02, c))
    return bars


# A trigger (entry 2010, stop 2005, risk 5) followed by a bar reaching 2021:
# a 2R target (2020) is hit, a 3R+ target is not.
BREAKOUT_BARS = _prefix() + [(2009, 2010, 2005, 2010), (2011, 2021, 2011, 2015)]


def _seed_squeeze_chart_db(db_path, bars):
    index = pd.date_range(START, periods=len(bars), freq="1h", tz="UTC")
    candles = [
        Candle(
            symbol="XAUUSD",
            timeframe="1h",
            timestamp=collector.to_utc_iso_z(timestamp),
            open=o, high=h, low=l, close=c, volume=100.0,
        )
        for timestamp, (o, h, l, c) in zip(index, bars)
    ]
    collector.save_candles(db_path, candles)
    return index


def strategy_dict(**overrides) -> dict:
    data = {
        "schema_version": 1,
        "id": "ext_squeeze_v1",
        "name": "External Squeeze",
        "symbols": ["XAU_USD"],
        "timeframes": ["1h"],
        "entry": {"handler": "squeeze_breakout", "reentry": "new_setup"},
        "risk": {
            "stop_loss": {"type": "trigger_bar_extreme"},
            "take_profit": {"type": "r_multiple", "r": 2.0},
        },
        "exit": {"max_holding_bars": 24},
    }
    data.update(overrides)
    return data


def write_strategy(tmp_path, data=None, name="strategy.yaml", **overrides):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(strategy_dict(**overrides) if data is None else data), encoding="utf-8")
    return path


def make_args(tmp_path, index, **overrides):
    argv = [
        "--symbol", overrides.pop("symbol", "XAU_USD"),
        "--timeframe", overrides.pop("timeframe", "1h"),
        "--from", index[overrides.pop("from_index", TRIGGER_INDEX)].isoformat(),
        "--to", index[-1].isoformat(),
        "--db-path", str(overrides.pop("db_path", tmp_path / "chart_data.sqlite3")),
        "--backtest-db-path", str(overrides.pop("backtest_db_path", tmp_path / "backtest_results.sqlite3")),
        "--market-bias-db-path", str(tmp_path / "market_bias.sqlite3"),
    ]
    for key, value in overrides.items():
        argv.extend([f"--{key.replace('_', '-')}", str(value)])
    return runner.parse_args(argv)


def _run_external(tmp_path, *, bars=None, definition_overrides=None, **arg_overrides):
    bars = BREAKOUT_BARS if bars is None else bars
    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", bars)
    strategy_path = write_strategy(tmp_path, **(definition_overrides or {}))
    args = make_args(tmp_path, index, strategy_file=str(strategy_path), **arg_overrides)
    return index, runner.run_backtest(args)


# =============================================================================
# CLI parsing: new flags + exclusivity (argparse usage errors, exit code 2)
# =============================================================================

BASE_ARGV = ["--symbol", "XAU_USD", "--timeframe", "1h", "--from", "2026-06-01T00:00:00", "--to", "2026-06-02T00:00:00"]


def test_parse_args_defaults_leave_external_strategy_unset():
    args = runner.parse_args(BASE_ARGV)
    assert args.strategy_file is None
    assert args.strategy is None
    assert args.strategy_profile == runner.DEFAULT_STRATEGY_PROFILE  # unchanged default


def test_strategy_file_and_strategy_are_mutually_exclusive():
    with pytest.raises(SystemExit) as exc_info:
        runner.parse_args(BASE_ARGV + ["--strategy-file", "x.yaml", "--strategy", "some_id"])
    assert exc_info.value.code == runner.EXIT_USAGE_ERROR


@pytest.mark.parametrize("external_flag", [["--strategy-file", "x.yaml"], ["--strategy", "some_id"]])
def test_external_strategy_cannot_be_combined_with_strategy_profile(external_flag, capsys):
    with pytest.raises(SystemExit) as exc_info:
        runner.parse_args(BASE_ARGV + external_flag + ["--strategy-profile", "trend_follow"])
    assert exc_info.value.code == runner.EXIT_USAGE_ERROR
    captured = capsys.readouterr()
    assert "--strategy-profile" in captured.err


def test_run_help_documents_the_new_flags(capsys):
    with pytest.raises(SystemExit) as exc_info:
        runner.parse_args(["run", "--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "--strategy-file" in captured.out
    assert "--strategy " in captured.out or "--strategy I" in captured.out
    assert "STRATEGY_DEFINITIONS_DIR" in captured.out


def test_handler_execution_bindings_match_the_capability_registry():
    # Every handler that validation can accept must have an execution runner
    # bound, and vice versa -- a handler must never be accepted and unrunnable.
    assert tuple(sorted(runner.EXTERNAL_STRATEGY_HANDLER_RUNNERS)) == sd.registered_handler_names()


# =============================================================================
# --strategy-file happy path + config/metadata recording
# =============================================================================


def test_strategy_file_runs_and_records_the_normalized_definition(tmp_path):
    _index, (exit_code, summary) = _run_external(tmp_path)
    assert exit_code == runner.EXIT_OK
    assert summary["signals_generated"] == 1
    assert summary["strategy_profile"] == "ext_squeeze_v1"
    assert summary["strategy_definition"]["id"] == "ext_squeeze_v1"

    backtest_db = tmp_path / "backtest_results.sqlite3"
    run = store.get_backtest_run(summary["run_id"], db_path=backtest_db)
    config = json.loads(run["config_json"])
    assert config["strategy_profile"] == "ext_squeeze_v1"
    assert config["risk_reward"] == 2.0
    assert config["time_exit_bars"] == 24
    assert config["same_candle_priority"] == "stop_loss"
    # the full normalized definition (defaults resolved) is recorded
    recorded = config["strategy_definition"]
    assert recorded["schema_version"] == 1
    assert recorded["name"] == "External Squeeze"
    assert recorded["description"] == ""
    assert recorded["entry"]["reentry"] == "new_setup"
    assert recorded["entry"]["cooldown_bars"] == 0
    assert recorded["entry"]["params"] == {
        "squeeze_state": "squeeze",
        "require_episode_release": True,
        "breakout": "bollinger_close",
        "squeeze_lookback": 20,
        "squeeze_percentile": 0.10,
    }
    assert recorded["risk"]["take_profit"] == {"type": "r_multiple", "r": 2.0}
    assert recorded["exit"] == {"max_holding_bars": 24, "same_candle_priority": "stop_loss"}

    signals = store.list_backtest_signals(summary["run_id"], db_path=backtest_db, limit=10)
    assert len(signals) == 1
    assert signals[0]["strategy_profile"] == "ext_squeeze_v1"  # the definition's id, not the module constant
    assert signals[0]["signal_type"] == sb.SIGNAL_TYPE


def test_strategy_file_run_matches_the_equivalent_builtin_profile_run(tmp_path):
    # The sample-equivalent definition (r=2.0, 24 bars) must reproduce the
    # --strategy-profile run's signals exactly -- entry/stop/target/status all
    # identical, inheriting every future-leakage guarantee of that path.
    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", BREAKOUT_BARS)

    profile_db = tmp_path / "profile_results.sqlite3"
    args = make_args(tmp_path, index, backtest_db_path=profile_db, strategy_profile=sb.STRATEGY_PROFILE)
    _exit_code, profile_summary = runner.run_backtest(args)

    external_db = tmp_path / "external_results.sqlite3"
    strategy_path = write_strategy(tmp_path, id=sb.STRATEGY_PROFILE)
    args = make_args(tmp_path, index, backtest_db_path=external_db, strategy_file=str(strategy_path))
    _exit_code, external_summary = runner.run_backtest(args)

    profile_signals = store.list_backtest_signals(profile_summary["run_id"], db_path=profile_db, limit=10)
    external_signals = store.list_backtest_signals(external_summary["run_id"], db_path=external_db, limit=10)
    assert len(profile_signals) == len(external_signals) == 1
    compared_fields = (
        "signal_time", "direction", "entry_price", "invalidation_price", "take_profit_price",
        "close_price", "closed_at", "close_reason", "result_status", "pnl",
        "signal_type", "trigger_type", "alert_source", "strategy_profile", "market_state",
    )
    for field in compared_fields:
        assert external_signals[0][field] == profile_signals[0][field], field


def test_summary_prints_the_external_strategy_profile(tmp_path, capsys):
    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", BREAKOUT_BARS)
    strategy_path = write_strategy(tmp_path)
    argv = [
        "--symbol", "XAU_USD", "--timeframe", "1h",
        "--from", index[TRIGGER_INDEX].isoformat(), "--to", index[-1].isoformat(),
        "--db-path", str(tmp_path / "chart_data.sqlite3"),
        "--backtest-db-path", str(tmp_path / "backtest_results.sqlite3"),
        "--market-bias-db-path", str(tmp_path / "market_bias.sqlite3"),
        "--strategy-file", str(strategy_path),
    ]
    exit_code = runner.main(argv)
    captured = capsys.readouterr()
    assert exit_code == runner.EXIT_OK
    assert "Strategy profile:    ext_squeeze_v1" in captured.out


# =============================================================================
# --strategy <id>: directory registry selection + STRATEGY_DEFINITIONS_DIR
# =============================================================================


def test_strategy_id_selects_from_the_definitions_directory(tmp_path, monkeypatch):
    strategies_dir = tmp_path / "strategies"
    write_strategy(strategies_dir, name="a.yaml", id="squeeze_low_r")
    write_strategy(strategies_dir, name="b.yaml", id="squeeze_high_r",
                   risk={"stop_loss": {"type": "trigger_bar_extreme"},
                         "take_profit": {"type": "r_multiple", "r": 3.0}})
    monkeypatch.setenv(sd.STRATEGY_DIR_ENV_VAR, str(strategies_dir))

    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", BREAKOUT_BARS)
    args = make_args(tmp_path, index, strategy="squeeze_high_r")
    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_OK
    assert summary["strategy_profile"] == "squeeze_high_r"
    run = store.get_backtest_run(summary["run_id"], db_path=tmp_path / "backtest_results.sqlite3")
    assert json.loads(run["config_json"])["risk_reward"] == 3.0


def test_unknown_strategy_id_is_a_usage_error_listing_available_ids(tmp_path, monkeypatch, capsys):
    strategies_dir = tmp_path / "strategies"
    write_strategy(strategies_dir, id="known_id")
    monkeypatch.setenv(sd.STRATEGY_DIR_ENV_VAR, str(strategies_dir))

    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", BREAKOUT_BARS)
    args = make_args(tmp_path, index, strategy="missing_id")
    exit_code, summary = runner.run_backtest(args)
    captured = capsys.readouterr()

    assert exit_code == runner.EXIT_USAGE_ERROR
    assert summary is None
    assert "unknown strategy id 'missing_id'" in captured.err
    assert "known_id" in captured.err
    assert str(strategies_dir) in captured.err


def test_strategy_id_load_is_all_or_nothing_one_broken_file_blocks_valid_ids(tmp_path, monkeypatch, capsys):
    strategies_dir = tmp_path / "strategies"
    write_strategy(strategies_dir, name="valid.yaml", id="valid_id")
    strategies_dir.joinpath("zz_broken.yaml").write_text("schema_version: 99\n", encoding="utf-8")
    monkeypatch.setenv(sd.STRATEGY_DIR_ENV_VAR, str(strategies_dir))

    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", BREAKOUT_BARS)
    args = make_args(tmp_path, index, strategy="valid_id")
    exit_code, summary = runner.run_backtest(args)
    captured = capsys.readouterr()

    assert exit_code == runner.EXIT_USAGE_ERROR
    assert summary is None
    assert "zz_broken.yaml" in captured.err
    assert not (tmp_path / "backtest_results.sqlite3").exists()  # no partial run either


# =============================================================================
# Validation failures: clean pre-run errors, zero side effects
# =============================================================================


def test_invalid_definition_is_a_clean_usage_error_with_field_path(tmp_path, capsys):
    data = strategy_dict()
    data["exit"]["max_holding_bars"] = 0
    strategy_path = write_strategy(tmp_path, data=data)
    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", BREAKOUT_BARS)
    args = make_args(tmp_path, index, strategy_file=str(strategy_path))

    exit_code, summary = runner.run_backtest(args)
    captured = capsys.readouterr()

    assert exit_code == runner.EXIT_USAGE_ERROR
    assert summary is None
    assert "error: invalid strategy definition" in captured.err
    assert "exit.max_holding_bars" in captured.err
    assert str(strategy_path) in captured.err
    assert "Traceback" not in captured.err


def test_validation_failure_creates_no_db_files_at_all(tmp_path, capsys):
    # The strategy is validated BEFORE any DB path is opened: neither the
    # Backtest DB nor even the (missing) chart DB file may be created.
    strategy_path = tmp_path / "broken.yaml"
    strategy_path.write_text("schema_version: 1\n", encoding="utf-8")  # id/name/... missing
    chart_db = tmp_path / "never_created_chart.sqlite3"
    backtest_db = tmp_path / "never_created_results.sqlite3"
    args = runner.parse_args([
        "--symbol", "XAU_USD", "--timeframe", "1h",
        "--from", "2026-06-01T00:00:00", "--to", "2026-06-02T00:00:00",
        "--db-path", str(chart_db),
        "--backtest-db-path", str(backtest_db),
        "--market-bias-db-path", str(tmp_path / "market_bias.sqlite3"),
        "--strategy-file", str(strategy_path),
    ])

    exit_code, summary = runner.run_backtest(args)
    capsys.readouterr()

    assert exit_code == runner.EXIT_USAGE_ERROR
    assert summary is None
    assert not chart_db.exists()
    assert not backtest_db.exists()


def test_symbol_not_declared_by_the_definition_is_a_usage_error(tmp_path, capsys):
    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", BREAKOUT_BARS)
    strategy_path = write_strategy(tmp_path, symbols=["USD_JPY"])
    args = make_args(tmp_path, index, strategy_file=str(strategy_path))
    exit_code, summary = runner.run_backtest(args)
    captured = capsys.readouterr()
    assert exit_code == runner.EXIT_USAGE_ERROR
    assert summary is None
    assert "--symbol 'XAU_USD' is not declared" in captured.err
    assert "USD_JPY" in captured.err


def test_timeframe_not_declared_by_the_definition_is_a_usage_error(tmp_path, capsys):
    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", BREAKOUT_BARS)
    strategy_path = write_strategy(tmp_path, timeframes=["4h"])
    args = make_args(tmp_path, index, strategy_file=str(strategy_path))
    exit_code, summary = runner.run_backtest(args)
    captured = capsys.readouterr()
    assert exit_code == runner.EXIT_USAGE_ERROR
    assert summary is None
    assert "--timeframe '1h' is not declared" in captured.err


def test_capability_violations_from_yaml_are_pre_run_usage_errors(tmp_path, capsys):
    # Nonzero cooldown / overlap reentry / take_profit priority and an unknown
    # market state are accepted nowhere -- each must fail before the run starts
    # (never run-and-ignore). allowed_market_states with KNOWN states is
    # accepted since Issue #63 (see the market-state gate section below).
    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", BREAKOUT_BARS)
    variants = [
        ({"allowed_market_states": ["sideways"]}, "unknown market state 'sideways'"),
        ({"allowed_market_states": []}, "empty list"),
        ({"entry": {"handler": "squeeze_breakout", "reentry": "new_setup", "cooldown_bars": 5}},
         "only supports cooldown_bars in [0]"),
        ({"entry": {"handler": "squeeze_breakout", "reentry": "overlap"}}, "only supports"),
        ({"exit": {"max_holding_bars": 24, "same_candle_priority": "take_profit"}}, "only supports"),
    ]
    for overrides, expected_message in variants:
        strategy_path = write_strategy(tmp_path, **overrides)
        args = make_args(tmp_path, index, strategy_file=str(strategy_path))
        exit_code, summary = runner.run_backtest(args)
        captured = capsys.readouterr()
        assert exit_code == runner.EXIT_USAGE_ERROR, overrides
        assert summary is None
        assert expected_message in captured.err, overrides


# =============================================================================
# Execution integration: definition fields actually drive the run
# =============================================================================


def test_take_profit_r_from_yaml_changes_the_run_result(tmp_path):
    # r=2.0 -> target 2020, hit by the 2021 high (win). r=4.0 -> target 2030,
    # never reached -> the same candles leave the position open. Same bars,
    # different YAML, different outcome: the R is read from the definition.
    _index, (exit_code, summary_r2) = _run_external(tmp_path)
    assert exit_code == runner.EXIT_OK
    backtest_db = tmp_path / "backtest_results.sqlite3"
    win_signal = store.list_backtest_signals(summary_r2["run_id"], db_path=backtest_db, limit=10)[0]
    assert win_signal["take_profit_price"] == 2020.0
    assert win_signal["result_status"] == exit_policy.STATUS_WIN

    strategy_path = write_strategy(
        tmp_path, name="r4.yaml", id="ext_squeeze_r4",
        risk={"stop_loss": {"type": "trigger_bar_extreme"},
              "take_profit": {"type": "r_multiple", "r": 4.0}},
    )
    index = pd.date_range(START, periods=len(BREAKOUT_BARS), freq="1h", tz="UTC")
    args = make_args(tmp_path, index, strategy_file=str(strategy_path))
    exit_code, summary_r4 = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK
    open_signal = store.list_backtest_signals(summary_r4["run_id"], db_path=backtest_db, limit=10)[0]
    assert open_signal["take_profit_price"] == 2030.0  # entry 2010 + 4 * risk 5
    assert open_signal["result_status"] != exit_policy.STATUS_WIN
    assert json.loads(store.get_backtest_run(summary_r4["run_id"], db_path=backtest_db)["config_json"])["risk_reward"] == 4.0


def test_max_holding_bars_from_yaml_changes_the_time_exit(tmp_path):
    # After the trigger, 6 flat bars never reach stop (2005) or target (2020):
    # max_holding_bars=3 must time-exit on the 3rd bar after entry, where the
    # built-in 24 would have left it open.
    flat_after = [(2010.0, 2010.4, 2009.6, 2010.0)] * 6
    bars = _prefix() + [(2009, 2010, 2005, 2010)] + flat_after
    index, (exit_code, summary) = _run_external(
        tmp_path, bars=bars,
        definition_overrides={"exit": {"max_holding_bars": 3}},
    )
    assert exit_code == runner.EXIT_OK
    signal = store.list_backtest_signals(summary["run_id"], db_path=tmp_path / "backtest_results.sqlite3", limit=10)[0]
    assert signal["close_reason"] == exit_policy.REASON_TIME_EXPIRED
    assert signal["closed_at"] == index[TRIGGER_INDEX + 3].isoformat()


def test_external_run_never_reads_candles_after_to(tmp_path):
    # The winning bar (high 2021) lies AFTER --to: capping validation at the
    # trigger bar must leave the position open -- the future winning candle
    # never leaks into the exit walk of an external-definition run.
    index, (exit_code, summary) = _run_external(tmp_path)
    assert exit_code == runner.EXIT_OK  # sanity: with --to at the end it wins

    args = runner.parse_args([
        "--symbol", "XAU_USD", "--timeframe", "1h",
        "--from", index[TRIGGER_INDEX].isoformat(),
        "--to", index[TRIGGER_INDEX].isoformat(),  # ends at the trigger bar
        "--db-path", str(tmp_path / "chart_data.sqlite3"),
        "--backtest-db-path", str(tmp_path / "capped_results.sqlite3"),
        "--market-bias-db-path", str(tmp_path / "market_bias.sqlite3"),
        "--strategy-file", str(write_strategy(tmp_path, name="capped.yaml")),
    ])
    exit_code, capped_summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK
    capped_signal = store.list_backtest_signals(
        capped_summary["run_id"], db_path=tmp_path / "capped_results.sqlite3", limit=10
    )[0]
    assert capped_signal["result_status"] == exit_policy.STATUS_OPEN
    assert capped_signal["close_price"] is None


def test_external_run_reentry_new_setup_yields_one_signal_per_episode(tmp_path):
    # Declared reentry=new_setup matches the state machine: the first episode's
    # single entry stops out, and only a NEW squeeze episode arms a second one.
    tail = [(2011, 2012, 2004, 2005)]
    flat2 = [
        (2005 + (0.05 if i % 2 == 0 else -0.05), 2005 + 0.07, 2005 - 0.07, 2005 + (0.05 if i % 2 == 0 else -0.05))
        for i in range(FLAT)
    ]
    breakout2 = [(2014, 2016, 2010, 2015), (2015, 2026, 2015, 2020)]
    bars = _prefix() + [(2009, 2010, 2005, 2010)] + tail + flat2 + breakout2
    _index, (exit_code, summary) = _run_external(tmp_path, bars=bars)
    assert exit_code == runner.EXIT_OK
    assert summary["signals_generated"] == 2


# =============================================================================
# Issue #63: allowed_market_states from YAML gates the squeeze Trigger bar
# =============================================================================

# BREAKOUT_BARS' Trigger bar (index 94) classifies as Market State "trend_up"
# (verified in exploration), so a gate listing trend_up admits the entry and a
# gate listing only "range" blocks it.


def test_allowed_market_states_from_yaml_blocks_the_entry_and_changes_the_result(tmp_path):
    # Same bars, same code -- a YAML-only change flips the run from 1 trade to
    # 0 trades, so the gate demonstrably reaches execution, not just validation.
    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", BREAKOUT_BARS)

    baseline_path = write_strategy(tmp_path, name="baseline.yaml", id="ext_squeeze_base")
    args = make_args(tmp_path, index, strategy_file=str(baseline_path),
                     backtest_db_path=tmp_path / "baseline.sqlite3")
    exit_code, baseline_summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK
    assert baseline_summary["signals_generated"] == 1
    assert baseline_summary["allowed_market_states"] is None
    assert baseline_summary["suppressed_market_state"] == 0

    gated_path = write_strategy(tmp_path, name="gated.yaml", id="ext_squeeze_gated",
                                allowed_market_states=["range"])
    args = make_args(tmp_path, index, strategy_file=str(gated_path),
                     backtest_db_path=tmp_path / "gated.sqlite3")
    exit_code, gated_summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK
    assert gated_summary["signals_generated"] == 0
    assert gated_summary["allowed_market_states"] == ["range"]
    # Exactly one: the disallowed episode-release trigger (bar 94). Bar 95
    # also breaks the band but the episode is already consumed, so it is
    # neither an entry nor a second suppression (PR #64 review).
    assert gated_summary["suppressed_market_state"] == 1

    # The effective gate is recorded for reproducibility: both in the run's
    # flat config key and inside the normalized definition.
    config = json.loads(
        store.get_backtest_run(gated_summary["run_id"], db_path=tmp_path / "gated.sqlite3")["config_json"]
    )
    assert config["allowed_market_states"] == ["range"]
    assert config["strategy_definition"]["allowed_market_states"] == ["range"]


def test_allowed_market_states_from_yaml_admits_a_listed_state_identically(tmp_path):
    # A gate that includes the Trigger bar's state must reproduce the ungated
    # run exactly -- same signal, same prices, same exit.
    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", BREAKOUT_BARS)

    baseline_path = write_strategy(tmp_path, name="baseline.yaml", id="ext_squeeze_base")
    args = make_args(tmp_path, index, strategy_file=str(baseline_path),
                     backtest_db_path=tmp_path / "baseline.sqlite3")
    _exit_code, baseline_summary = runner.run_backtest(args)

    allowed_path = write_strategy(tmp_path, name="allowed.yaml", id="ext_squeeze_base",
                                  allowed_market_states=["trend_up", "range"])
    args = make_args(tmp_path, index, strategy_file=str(allowed_path),
                     backtest_db_path=tmp_path / "allowed.sqlite3")
    exit_code, allowed_summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK
    assert allowed_summary["signals_generated"] == 1
    assert allowed_summary["allowed_market_states"] == ["range", "trend_up"]  # sorted
    assert allowed_summary["suppressed_market_state"] == 0

    baseline_signal = store.list_backtest_signals(
        baseline_summary["run_id"], db_path=tmp_path / "baseline.sqlite3", limit=10)[0]
    allowed_signal = store.list_backtest_signals(
        allowed_summary["run_id"], db_path=tmp_path / "allowed.sqlite3", limit=10)[0]
    for field in ("signal_time", "direction", "entry_price", "invalidation_price",
                  "take_profit_price", "close_price", "closed_at", "close_reason",
                  "result_status", "pnl", "market_state"):
        assert allowed_signal[field] == baseline_signal[field], field
    assert allowed_signal["market_state"] == "trend_up"


def test_unknown_market_state_in_yaml_fails_before_the_run_with_no_side_effects(tmp_path, capsys):
    strategy_path = write_strategy(tmp_path, allowed_market_states=["trend_up", "sideways"])
    chart_db = tmp_path / "never_created_chart.sqlite3"
    backtest_db = tmp_path / "never_created_results.sqlite3"
    args = runner.parse_args([
        "--symbol", "XAU_USD", "--timeframe", "1h",
        "--from", "2026-06-01T00:00:00", "--to", "2026-06-02T00:00:00",
        "--db-path", str(chart_db),
        "--backtest-db-path", str(backtest_db),
        "--market-bias-db-path", str(tmp_path / "market_bias.sqlite3"),
        "--strategy-file", str(strategy_path),
    ])
    exit_code, summary = runner.run_backtest(args)
    captured = capsys.readouterr()
    assert exit_code == runner.EXIT_USAGE_ERROR
    assert summary is None
    assert "allowed_market_states[1]" in captured.err
    assert "'sideways'" in captured.err
    assert not chart_db.exists()
    assert not backtest_db.exists()


def test_cli_allowed_market_states_flag_never_applies_to_a_squeeze_run(tmp_path):
    # --allowed-market-states stays a trend_follow-path flag (its help says
    # so): an external squeeze definition with a null gate keeps running
    # unrestricted even when the CLI flag names a state the Trigger bar
    # doesn't have. Only the YAML field gates squeeze entries.
    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", BREAKOUT_BARS)
    strategy_path = write_strategy(tmp_path)  # allowed_market_states omitted -> null
    args = make_args(tmp_path, index, strategy_file=str(strategy_path),
                     allowed_market_states="range")
    exit_code, summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK
    assert summary["signals_generated"] == 1
    assert summary["allowed_market_states"] is None
    assert summary["suppressed_market_state"] == 0


def test_yaml_market_state_gate_never_leaks_into_the_builtin_profile_path(tmp_path):
    # An external run whose gate suppressed the entry, then the built-in
    # --strategy-profile path: still ungated, still 1 signal, null in config.
    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", BREAKOUT_BARS)
    gated_path = write_strategy(tmp_path, name="gated.yaml", id="ext_squeeze_gated",
                                allowed_market_states=["range"])
    args = make_args(tmp_path, index, strategy_file=str(gated_path),
                     backtest_db_path=tmp_path / "gated.sqlite3")
    exit_code, gated_summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK
    assert gated_summary["signals_generated"] == 0

    profile_db = tmp_path / "profile.sqlite3"
    args = make_args(tmp_path, index, strategy_profile=sb.STRATEGY_PROFILE,
                     backtest_db_path=profile_db)
    exit_code, profile_summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK
    assert profile_summary["signals_generated"] == 1
    assert profile_summary["allowed_market_states"] is None
    config = json.loads(
        store.get_backtest_run(profile_summary["run_id"], db_path=profile_db)["config_json"]
    )
    assert config["allowed_market_states"] is None


# =============================================================================
# Backward compatibility: the external machinery is strictly opt-in
# =============================================================================


def test_ordinary_run_never_imports_pyyaml_or_the_loader(tmp_path, monkeypatch):
    # Poison both modules: ANY import attempt would raise ImportError. An
    # ordinary trend_follow run (no --strategy-file/--strategy) must succeed.
    monkeypatch.setitem(sys.modules, "yaml", None)
    monkeypatch.setitem(sys.modules, "strategy_definition", None)

    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    index = seed_chart_db(chart_db, n=100)
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[0]))
    args = runner.parse_args([
        "--symbol", "USD_JPY", "--timeframe", "1h",
        "--from", index[90].isoformat(), "--to", index[99].isoformat(),
        "--db-path", str(chart_db),
        "--backtest-db-path", str(tmp_path / "backtest_results.sqlite3"),
        "--market-bias-db-path", str(bias_db),
    ])

    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_OK
    assert summary["signals_generated"] == 10  # the unchanged pre-#53 result


def test_broken_strategies_directory_never_affects_an_ordinary_run(tmp_path, monkeypatch):
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "broken.yaml").write_text("entry: [unclosed", encoding="utf-8")
    monkeypatch.setenv(sd.STRATEGY_DIR_ENV_VAR, str(strategies_dir))

    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    index = seed_chart_db(chart_db, n=100)
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(index[0]))
    args = runner.parse_args([
        "--symbol", "USD_JPY", "--timeframe", "1h",
        "--from", index[90].isoformat(), "--to", index[99].isoformat(),
        "--db-path", str(chart_db),
        "--backtest-db-path", str(tmp_path / "backtest_results.sqlite3"),
        "--market-bias-db-path", str(bias_db),
    ])

    exit_code, summary = runner.run_backtest(args)

    assert exit_code == runner.EXIT_OK
    assert summary["signals_generated"] == 10


def test_builtin_squeeze_profile_run_is_unchanged_by_issue_53(tmp_path):
    # The --strategy-profile path must keep its pre-#53 config_json shape:
    # module-constant values, and no strategy_definition key at all.
    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", BREAKOUT_BARS)
    args = make_args(tmp_path, index, strategy_profile=sb.STRATEGY_PROFILE)
    exit_code, summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK
    assert summary["signals_generated"] == 1
    assert summary["strategy_profile"] == sb.STRATEGY_PROFILE
    assert summary["strategy_definition"] is None

    run = store.get_backtest_run(summary["run_id"], db_path=tmp_path / "backtest_results.sqlite3")
    config = json.loads(run["config_json"])
    assert "strategy_definition" not in config
    assert config["risk_reward"] == sb.RISK_REWARD
    assert config["time_exit_bars"] == sb.TIME_EXIT_BARS
