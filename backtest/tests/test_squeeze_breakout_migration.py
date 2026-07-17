"""Issue #54: squeeze_breakout migration to the shipped external Strategy definition.

Covers what Issue #53's tests (test_strategy_runner.py) do not: the actual
migration artifact ``backtest/strategies/xauusd_1h_squeeze_breakout_v1.yaml``.

- The shipped file exists, validates, loads from the DEFAULT registry
  directory (no env override), and is value-identical to the built-in
  defaults (STRATEGY_PROFILE / RISK_REWARD / TIME_EXIT_BARS / new_setup / ...).
- Full old-path vs new-path equivalence on a multi-trade fixture: trade
  count, signal_time, direction, entry/stop/target, exit time/price/reason,
  per-trade pnl, aggregate PnL / profit factor / max drawdown, market state,
  and every persisted strategy-identity metadata field.
- ``--strategy <id>`` picks the shipped definition up from the default
  directory end-to-end through the CLI.
- --warmup-bars (a run-level setting, deliberately NOT part of the Issue #52
  schema) still gates external-definition runs through the existing route.
- Settings given to an external-definition run never leak into the built-in
  --strategy-profile path or the trend_follow strategy afterwards.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

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
import warmup  # noqa: E402
import chart_collector as collector  # noqa: E402  (market-watch)
from candle_fixtures import seed_chart_db, seed_bull_bias  # noqa: E402

# Reuse Issue #53's synthetic squeeze fixture helpers (not tests) so both
# suites agree on what a Trigger bar looks like.
from test_strategy_runner import (  # noqa: E402
    BREAKOUT_BARS,
    FLAT,
    TRIGGER_INDEX,
    _prefix,
    _seed_squeeze_chart_db,
    make_args,
    write_strategy,
)

SHIPPED_DEFINITION_PATH = BOT_DIR / "strategies" / "xauusd_1h_squeeze_breakout_v1.yaml"

# Two-trade fixture: the first episode's Trigger (entry 2010, stop 2005) stops
# out on the next bar (low 2004 -> loss -5), a NEW squeeze episode forms, and
# its Trigger (entry 2015, stop 2010, 2R target 2025) wins on the next bar
# (high 2026 -> pnl +10). Loss-then-win makes aggregate PnL (+5), profit
# factor (10/5 = 2.0) and max drawdown (5) all non-trivial, so the old/new
# path comparison pins every Issue #54 metric, not just per-signal fields.
_STOP_OUT = [(2011, 2012, 2004, 2005)]
_FLAT_2 = [
    (2005 + (0.05 if i % 2 == 0 else -0.05), 2005 + 0.07, 2005 - 0.07, 2005 + (0.05 if i % 2 == 0 else -0.05))
    for i in range(FLAT)
]
_BREAKOUT_2 = [(2014, 2016, 2010, 2015), (2015, 2026, 2015, 2020)]
TWO_TRADE_BARS = _prefix() + [(2009, 2010, 2005, 2010)] + _STOP_OUT + _FLAT_2 + _BREAKOUT_2


# =============================================================================
# The shipped definition file: existence, default-registry load, default parity
# =============================================================================


def test_shipped_definition_validates_and_equals_the_builtin_defaults():
    definition = sd.load_strategy_definition(SHIPPED_DEFINITION_PATH)

    # Identity: the definition's id IS the legacy STRATEGY_PROFILE, so every
    # persisted strategy_profile value stays byte-identical across paths.
    assert definition.id == sb.STRATEGY_PROFILE
    assert definition.schema_version == 1
    assert definition.symbols == ("XAU_USD",)
    assert definition.timeframes == ("1h",)
    assert definition.allowed_market_states is None

    assert definition.entry.handler == sd.HANDLER_SQUEEZE_BREAKOUT
    assert definition.entry.reentry == sd.REENTRY_NEW_SETUP
    assert definition.entry.cooldown_bars == 0
    assert dict(definition.entry.params) == {
        "squeeze_state": sb.SQUEEZE_STATE,
        "require_episode_release": True,
        "breakout": "bollinger_close",
    }

    assert dict(definition.risk.stop_loss) == {"type": sd.STOP_LOSS_TRIGGER_BAR_EXTREME}
    assert dict(definition.risk.take_profit) == {"type": sd.TAKE_PROFIT_R_MULTIPLE, "r": sb.RISK_REWARD}
    assert definition.exit.max_holding_bars == sb.TIME_EXIT_BARS
    assert definition.exit.same_candle_priority == sd.SAME_CANDLE_PRIORITY_STOP_LOSS


def test_shipped_definition_loads_from_the_default_registry_directory(monkeypatch):
    # No STRATEGY_DEFINITIONS_DIR override: this scans the real
    # backtest/strategies/ directory. Because the registry is all-or-nothing,
    # this also proves every file shipped there is valid.
    monkeypatch.delenv(sd.STRATEGY_DIR_ENV_VAR, raising=False)
    registry = sd.load_strategy_registry()
    assert sb.STRATEGY_PROFILE in registry
    definition = registry[sb.STRATEGY_PROFILE]
    assert definition.source_path == str(SHIPPED_DEFINITION_PATH)
    assert float(definition.risk.take_profit["r"]) == sb.RISK_REWARD
    assert definition.exit.max_holding_bars == sb.TIME_EXIT_BARS


# =============================================================================
# Backward compatibility: old path vs new path, full-field equivalence
# =============================================================================

# Every persisted per-signal field Issue #54 requires to match exactly:
# signal_time, direction, entry, stop, target, exit time/price/reason,
# per-trade pnl, result status, market state, and the strategy metadata.
_COMPARED_SIGNAL_FIELDS = (
    "signal_time",
    "direction",
    "entry_price",
    "invalidation_price",   # stop_price
    "take_profit_price",    # target_price
    "closed_at",            # exit time
    "close_price",          # exit price
    "close_reason",         # exit reason
    "result_status",
    "pnl",                  # per-trade PnL
    "market_state",
    "market_state_confidence",
    "signal_type",
    "trigger_type",
    "alert_source",
    "strategy_profile",
    "exit_policy",
    "symbol",
    "timeframe",
)

_COMPARED_SUMMARY_METRICS = (
    "total_signals",
    "win_count",
    "loss_count",
    "win_rate",
    "total_pnl",            # aggregate PnL
    "average_pnl",
    "profit_factor",        # PF
    "max_drawdown",
)


def _run_both_paths(tmp_path, bars, **arg_overrides):
    """Run the same data/window/Execution Model through the old
    --strategy-profile path and the new shipped-definition path."""
    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", bars)

    profile_db = tmp_path / "profile_results.sqlite3"
    args = make_args(tmp_path, index, backtest_db_path=profile_db,
                     strategy_profile=sb.STRATEGY_PROFILE, **arg_overrides)
    exit_code, profile_summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK

    external_db = tmp_path / "external_results.sqlite3"
    args = make_args(tmp_path, index, backtest_db_path=external_db,
                     strategy_file=str(SHIPPED_DEFINITION_PATH), **arg_overrides)
    exit_code, external_summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK

    profile_signals = store.list_backtest_signals(profile_summary["run_id"], db_path=profile_db, limit=100)
    external_signals = store.list_backtest_signals(external_summary["run_id"], db_path=external_db, limit=100)
    return profile_summary, external_summary, profile_signals, external_signals


def _assert_equivalent(profile_summary, external_summary, profile_signals, external_signals):
    # trade count
    assert len(external_signals) == len(profile_signals)
    assert external_summary["signals_generated"] == profile_summary["signals_generated"]
    assert external_summary["result_counts"] == profile_summary["result_counts"]

    # every persisted per-trade field, in chronological order
    for position, (external, profile) in enumerate(zip(external_signals, profile_signals)):
        for field in _COMPARED_SIGNAL_FIELDS:
            assert external[field] == profile[field], f"signal[{position}].{field}"

    # aggregate metrics: PnL / PF / max drawdown / win rate
    external_aggregate = external_summary["signal_summary"]
    profile_aggregate = profile_summary["signal_summary"]
    for metric in _COMPARED_SUMMARY_METRICS:
        assert external_aggregate[metric] == profile_aggregate[metric], metric

    # strategy identity in the summary itself
    assert external_summary["strategy_profile"] == profile_summary["strategy_profile"] == sb.STRATEGY_PROFILE
    meta_fields = ("strategy_profile", "symbol", "timeframe", "exit_policy", "execution_model")
    for field in meta_fields:
        assert external_aggregate["run_meta"][field] == profile_aggregate["run_meta"][field], field


def test_shipped_definition_reproduces_the_old_default_path_exactly(tmp_path):
    results = _run_both_paths(tmp_path, TWO_TRADE_BARS)
    _assert_equivalent(*results)
    # Sanity: the fixture really exercises loss AND win, so PF / drawdown are
    # non-trivial (a 1-signal comparison could pass with broken aggregation).
    _profile_summary, external_summary, _p, external_signals = results
    assert len(external_signals) == 2
    aggregate = external_summary["signal_summary"]
    assert aggregate["win_count"] == 1 and aggregate["loss_count"] == 1
    assert aggregate["total_pnl"] == 5.0
    assert aggregate["profit_factor"] == 2.0
    assert aggregate["max_drawdown"] == 5.0


def test_shipped_definition_matches_the_old_path_under_execution_costs(tmp_path):
    # Same equivalence with a non-zero Execution Model: entry-side costs and
    # the TP-fills-at-target rule must behave identically on both paths.
    results = _run_both_paths(tmp_path, TWO_TRADE_BARS, spread=0.3, slippage=0.1)
    _assert_equivalent(*results)
    _profile_summary, external_summary, _p, _e = results
    assert external_summary["execution_model"]["spread"] == 0.3
    assert external_summary["execution_model"]["slippage"] == 0.1


def test_run_config_records_the_same_effective_settings_on_both_paths(tmp_path):
    profile_summary, external_summary, _p, _e = _run_both_paths(tmp_path, TWO_TRADE_BARS)
    profile_config = json.loads(
        store.get_backtest_run(profile_summary["run_id"], db_path=tmp_path / "profile_results.sqlite3")["config_json"]
    )
    external_config = json.loads(
        store.get_backtest_run(external_summary["run_id"], db_path=tmp_path / "external_results.sqlite3")["config_json"]
    )
    for key in ("strategy_profile", "signal_type", "trigger_type", "risk_reward",
                "time_exit_bars", "same_candle_priority", "no_reentry", "cooldown_minutes",
                "warmup_bars", "execution_model"):
        assert external_config[key] == profile_config[key], key
    # Only the new path additionally records the normalized definition.
    assert "strategy_definition" not in profile_config
    assert external_config["strategy_definition"]["id"] == sb.STRATEGY_PROFILE
    assert external_config["strategy_definition"]["source_path"] == str(SHIPPED_DEFINITION_PATH)


# =============================================================================
# CLI: --strategy <id> resolves the shipped file from the default directory
# =============================================================================


def test_cli_strategy_id_runs_the_shipped_definition_from_the_default_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(sd.STRATEGY_DIR_ENV_VAR, raising=False)
    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", BREAKOUT_BARS)
    exit_code = runner.main([
        "run",
        "--symbol", "XAU_USD", "--timeframe", "1h",
        "--from", index[TRIGGER_INDEX].isoformat(), "--to", index[-1].isoformat(),
        "--db-path", str(tmp_path / "chart_data.sqlite3"),
        "--backtest-db-path", str(tmp_path / "backtest_results.sqlite3"),
        "--market-bias-db-path", str(tmp_path / "market_bias.sqlite3"),
        "--strategy", sb.STRATEGY_PROFILE,
    ])
    captured = capsys.readouterr()
    assert exit_code == runner.EXIT_OK
    assert f"Strategy profile:    {sb.STRATEGY_PROFILE}" in captured.out


# =============================================================================
# warmup_bars: a run-level CLI setting (NOT in the Issue #52 schema) that must
# keep gating external-definition runs through the existing route
# =============================================================================


def test_warmup_bars_cli_still_gates_the_external_definition_run(tmp_path):
    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", BREAKOUT_BARS)

    # Default warmup (75): 94 warm-up bars are enough -> 1 signal.
    args = make_args(tmp_path, index, strategy_file=str(SHIPPED_DEFINITION_PATH),
                     backtest_db_path=tmp_path / "ok.sqlite3")
    exit_code, summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK
    assert args.warmup_bars == warmup.DEFAULT_WARMUP_BARS  # unchanged default
    assert summary["signals_generated"] == 1
    assert summary["warmup_insufficient"] is False

    # Raising --warmup-bars above the available history moves the evaluation
    # start condition: the same window now produces no signal at all.
    args = make_args(tmp_path, index, strategy_file=str(SHIPPED_DEFINITION_PATH),
                     backtest_db_path=tmp_path / "insufficient.sqlite3", warmup_bars=500)
    exit_code, summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK
    assert summary["signals_generated"] == 0
    assert summary["warmup_insufficient"] is True


# =============================================================================
# Isolation: external-definition settings never leak into other strategies
# =============================================================================


def test_external_definition_settings_never_leak_into_the_builtin_profile_path(tmp_path):
    index = _seed_squeeze_chart_db(tmp_path / "chart_data.sqlite3", BREAKOUT_BARS)

    # 1) An external run with deliberately different settings (r=5, 3 bars).
    strategy_path = write_strategy(
        tmp_path, id="ext_squeeze_r5",
        risk={"stop_loss": {"type": "trigger_bar_extreme"},
              "take_profit": {"type": "r_multiple", "r": 5.0}},
        exit={"max_holding_bars": 3},
    )
    args = make_args(tmp_path, index, strategy_file=str(strategy_path),
                     backtest_db_path=tmp_path / "external.sqlite3")
    exit_code, _summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK

    # 2) The module constants are untouched...
    assert sb.RISK_REWARD == 2.0
    assert sb.TIME_EXIT_BARS == 24

    # 3) ...and a subsequent built-in --strategy-profile run still uses them:
    # 2R target (2020) hit -> win, config records the default values.
    profile_db = tmp_path / "profile.sqlite3"
    args = make_args(tmp_path, index, strategy_profile=sb.STRATEGY_PROFILE, backtest_db_path=profile_db)
    exit_code, profile_summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK
    signal = store.list_backtest_signals(profile_summary["run_id"], db_path=profile_db, limit=10)[0]
    assert signal["take_profit_price"] == 2020.0
    assert signal["result_status"] == exit_policy.STATUS_WIN
    assert signal["strategy_profile"] == sb.STRATEGY_PROFILE
    config = json.loads(store.get_backtest_run(profile_summary["run_id"], db_path=profile_db)["config_json"])
    assert config["risk_reward"] == sb.RISK_REWARD
    assert config["time_exit_bars"] == sb.TIME_EXIT_BARS
    assert "strategy_definition" not in config


def test_external_definition_settings_never_leak_into_a_trend_follow_run(tmp_path):
    # An external squeeze run first...
    squeeze_dir = tmp_path / "squeeze"
    squeeze_dir.mkdir()
    index = _seed_squeeze_chart_db(squeeze_dir / "chart_data.sqlite3", BREAKOUT_BARS)
    strategy_path = write_strategy(
        squeeze_dir, id="ext_squeeze_r5",
        risk={"stop_loss": {"type": "trigger_bar_extreme"},
              "take_profit": {"type": "r_multiple", "r": 5.0}},
    )
    args = make_args(squeeze_dir, index, strategy_file=str(strategy_path))
    exit_code, _summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK

    # ...then the unchanged trend_follow default path: the same 10 signals the
    # pre-#53 suite pins (see test_strategy_runner.py), all trend_follow-typed.
    chart_db = tmp_path / "chart_data.sqlite3"
    bias_db = tmp_path / "market_bias.sqlite3"
    tf_index = seed_chart_db(chart_db, n=100)
    seed_bull_bias(bias_db, evaluated_at=collector.to_utc_iso_z(tf_index[0]))
    args = runner.parse_args([
        "--symbol", "USD_JPY", "--timeframe", "1h",
        "--from", tf_index[90].isoformat(), "--to", tf_index[99].isoformat(),
        "--db-path", str(chart_db),
        "--backtest-db-path", str(tmp_path / "trend_results.sqlite3"),
        "--market-bias-db-path", str(bias_db),
    ])
    exit_code, summary = runner.run_backtest(args)
    assert exit_code == runner.EXIT_OK
    assert summary["signals_generated"] == 10
    assert summary["strategy_profile"] == runner.STRATEGY_PROFILE_TREND_FOLLOW
    signals = store.list_backtest_signals(summary["run_id"], db_path=tmp_path / "trend_results.sqlite3", limit=100)
    assert all(signal["signal_type"] != sb.SIGNAL_TYPE for signal in signals)
    assert all(signal["strategy_profile"] != "ext_squeeze_r5" for signal in signals)
