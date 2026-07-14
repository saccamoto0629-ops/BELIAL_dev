"""Issue #53: tests for the external Strategy definition Loader / Validator.

Covers the four independently-testable layers:

- YAML file loading (safe_load only: empty / syntax error / non-mapping top
  level / missing file / unsafe tags / multi-document all rejected with the
  source path in the error).
- Schema validation (required keys, types, defaults, enums, ranges, unknown
  keys fail-closed, bool-vs-int strictness, NaN/Infinity rejection) per
  docs/architecture/strategy_definition.md.
- Handler registry + capability descriptors (unregistered handler, params /
  reentry / cooldown / stop-loss / take-profit / same-candle-priority /
  market-state-gate contracts of squeeze_breakout).
- Directory registry loading (deterministic order, all-or-nothing, duplicate
  id detection with both file paths, STRATEGY_DEFINITIONS_DIR override).

plus the docs §5.2 "descriptor allowed values == actual handler behaviour"
consistency tests that pin the squeeze_breakout capability descriptor to
squeeze_breakout.py's real execution.
"""

from __future__ import annotations

import copy
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

for _dir in (TESTS_DIR, BOT_DIR, MARKET_WATCH_DIR, PAPER_SIGNAL_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import strategy_definition as sd  # noqa: E402
import squeeze_breakout as sb  # noqa: E402
import execution_model as execution  # noqa: E402
import exit_policy  # noqa: E402
from market_state import KNOWN_MARKET_STATES  # noqa: E402  (market-watch)

SAMPLE_YAML_PATH = REPO_ROOT / "docs" / "architecture" / "examples" / "strategy_squeeze_breakout_v1.yaml"


def base_definition() -> dict:
    """A minimal valid schema-v1 squeeze_breakout definition (fresh each call)."""
    return {
        "schema_version": 1,
        "id": "test_squeeze_v1",
        "name": "Test Squeeze",
        "symbols": ["XAU_USD"],
        "timeframes": ["1h"],
        "entry": {"handler": "squeeze_breakout", "reentry": "new_setup"},
        "risk": {
            "stop_loss": {"type": "trigger_bar_extreme"},
            "take_profit": {"type": "r_multiple", "r": 2.0},
        },
        "exit": {"max_holding_bars": 24},
    }


def build(data=None, **top_level_overrides):
    data = base_definition() if data is None else data
    data.update(top_level_overrides)
    return sd.build_strategy_definition(data, source_path="inline.yaml")


def build_error(data=None, **top_level_overrides) -> sd.StrategyDefinitionError:
    with pytest.raises(sd.StrategyDefinitionError) as exc_info:
        build(data, **top_level_overrides)
    return exc_info.value


def write_yaml(tmp_path, data, name="strategy.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


# =============================================================================
# YAML file loading
# =============================================================================


def test_loads_the_repo_sample_definition():
    definition = sd.load_strategy_definition(SAMPLE_YAML_PATH)
    assert definition.schema_version == 1
    assert definition.id == "xauusd_1h_squeeze_breakout_v1"
    assert definition.symbols == ("XAU_USD",)
    assert definition.timeframes == ("1h",)
    assert definition.allowed_market_states is None
    assert definition.entry.handler == "squeeze_breakout"
    assert definition.entry.reentry == "new_setup"
    assert definition.entry.cooldown_bars == 0
    assert definition.risk.stop_loss == {"type": "trigger_bar_extreme"}
    assert definition.risk.take_profit == {"type": "r_multiple", "r": 2.0}
    assert definition.exit.max_holding_bars == 24
    assert definition.exit.same_candle_priority == "stop_loss"
    assert definition.source_path == str(SAMPLE_YAML_PATH)


def test_loading_a_valid_file_resolves_all_defaults(tmp_path):
    data = base_definition()
    del data["entry"]["reentry"]  # omitted -> squeeze's fixed new_setup
    path = write_yaml(tmp_path, data)
    definition = sd.load_strategy_definition(path)
    assert definition.description == ""
    assert definition.allowed_market_states is None
    assert definition.entry.reentry == "new_setup"
    assert definition.entry.cooldown_bars == 0
    assert definition.exit.same_candle_priority == "stop_loss"
    # handler param defaults are filled so the normalized config is complete
    assert definition.entry.params == {
        "squeeze_state": "squeeze",
        "require_episode_release": True,
        "breakout": "bollinger_close",
    }


@pytest.mark.parametrize("content", ["", "   \n\n# comment only\n"])
def test_empty_file_is_a_clear_validation_error(tmp_path, content):
    path = tmp_path / "empty.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(sd.StrategyDefinitionError, match="empty"):
        sd.load_strategy_definition(path)


def test_yaml_syntax_error_names_the_file(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("entry: [unclosed\n  bad: {", encoding="utf-8")
    with pytest.raises(sd.StrategyDefinitionError, match="invalid YAML") as exc_info:
        sd.load_strategy_definition(path)
    assert str(path) in str(exc_info.value)


def test_top_level_list_is_rejected(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- schema_version: 1\n- id: x\n", encoding="utf-8")
    with pytest.raises(sd.StrategyDefinitionError, match="top-level must be a mapping"):
        sd.load_strategy_definition(path)


def test_missing_file_is_a_clear_error_with_the_path(tmp_path):
    path = tmp_path / "does_not_exist.yaml"
    with pytest.raises(sd.StrategyDefinitionError, match="does not exist") as exc_info:
        sd.load_strategy_definition(path)
    assert str(path) in str(exc_info.value)


def test_unsafe_yaml_tag_is_rejected_never_constructed(tmp_path):
    path = tmp_path / "unsafe.yaml"
    path.write_text(
        'exploit: !!python/object/apply:os.system ["echo pwned"]\n', encoding="utf-8"
    )
    with pytest.raises(sd.StrategyDefinitionError, match="invalid YAML"):
        sd.load_strategy_definition(path)


def test_multi_document_yaml_is_rejected(tmp_path):
    doc = yaml.safe_dump(base_definition())
    path = tmp_path / "multi.yaml"
    path.write_text(f"---\n{doc}---\n{doc}", encoding="utf-8")
    with pytest.raises(sd.StrategyDefinitionError, match="multiple YAML documents"):
        sd.load_strategy_definition(path)


def test_validator_never_mutates_the_input_mapping():
    data = base_definition()
    data["entry"]["params"] = {"squeeze_state": "squeeze"}
    snapshot = copy.deepcopy(data)
    build(copy.deepcopy(data))  # sanity: valid
    sd.build_strategy_definition(data, source_path="inline.yaml")
    assert data == snapshot


def test_two_loads_share_no_mutable_state(tmp_path):
    path = write_yaml(tmp_path, base_definition())
    first = sd.load_strategy_definition(path)
    second = sd.load_strategy_definition(path)
    first.entry.params["squeeze_state"] = "tampered"
    assert second.entry.params["squeeze_state"] == "squeeze"


def test_to_config_is_json_serializable_and_returns_fresh_dicts():
    definition = build()
    config = definition.to_config()
    parsed = json.loads(json.dumps(config, allow_nan=False))
    assert parsed["id"] == "test_squeeze_v1"
    assert parsed["risk"]["take_profit"] == {"type": "r_multiple", "r": 2.0}
    assert parsed["exit"] == {"max_holding_bars": 24, "same_candle_priority": "stop_loss"}
    # mutating the returned config never reaches the frozen definition
    config["entry"]["params"]["squeeze_state"] = "tampered"
    assert definition.entry.params["squeeze_state"] == "squeeze"
    assert definition.to_config()["entry"]["params"]["squeeze_state"] == "squeeze"


# =============================================================================
# Schema validation: schema_version / id / name / description
# =============================================================================


@pytest.mark.parametrize("value", [True, False])
def test_schema_version_bool_is_not_an_int(value):
    error = build_error(schema_version=value)
    assert "must be an int" in str(error)
    assert "schema_version" in str(error)


def test_schema_version_str_is_rejected():
    error = build_error(schema_version="1")
    assert "must be an int" in str(error)


def test_schema_version_unsupported_value_lists_supported_versions():
    error = build_error(schema_version=2)
    assert "unsupported schema_version 2" in str(error)
    assert "[1]" in str(error)


def test_schema_version_missing_is_an_error():
    data = base_definition()
    del data["schema_version"]
    with pytest.raises(sd.StrategyDefinitionError, match="schema_version"):
        build(data)


@pytest.mark.parametrize("value", ["", "Bad-Id", "UPPER", "id with space", "id.dot", 123])
def test_invalid_id_is_rejected(value):
    error = build_error(id=value)
    assert error.field_path == "id"


def test_id_missing_is_an_error():
    data = base_definition()
    del data["id"]
    error = build_error(data)
    assert error.field_path == "id"


@pytest.mark.parametrize("value", ["", 5, None])
def test_invalid_name_is_rejected(value):
    error = build_error(name=value)
    assert error.field_path == "name"
    assert error.strategy_id == "test_squeeze_v1"  # id is already known here


def test_description_defaults_to_empty_string_and_must_be_str():
    assert build().description == ""
    error = build_error(description=123)
    assert error.field_path == "description"


# =============================================================================
# Schema validation: symbols / timeframes / allowed_market_states
# =============================================================================


@pytest.mark.parametrize("key", ["symbols", "timeframes"])
@pytest.mark.parametrize("value", [[], "XAU_USD", None])
def test_symbols_and_timeframes_must_be_non_empty_lists(key, value):
    error = build_error(**{key: value})
    assert key in str(error)


@pytest.mark.parametrize("key", ["symbols", "timeframes"])
@pytest.mark.parametrize("bad_item", [123, None, "", True])
def test_symbols_and_timeframes_items_must_be_non_empty_str(key, bad_item):
    error = build_error(**{key: ["ok_value", bad_item]})
    assert f"{key}[1]" in str(error)


def test_allowed_market_states_null_means_no_filter():
    assert build(allowed_market_states=None).allowed_market_states is None


def test_allowed_market_states_empty_list_is_not_null():
    # [] would allow no entries at all -- a dead config, distinct from null.
    error = build_error(allowed_market_states=[])
    assert "empty list" in str(error)
    assert "null" in str(error)


def test_allowed_market_states_unknown_state_lists_valid_states():
    error = build_error(allowed_market_states=["sideways"])
    assert "'sideways'" in str(error)
    for state in KNOWN_MARKET_STATES:
        assert state in str(error)


def test_allowed_market_states_non_list_is_rejected():
    error = build_error(allowed_market_states="trend_up")
    assert error.field_path == "allowed_market_states"


def test_allowed_market_states_known_state_is_still_rejected_by_squeeze_capability():
    # trend_up is a valid state, but squeeze_breakout does not support the
    # market-state gate at all -- accepting it would be a dead setting.
    error = build_error(allowed_market_states=["trend_up"])
    assert "does not support a market-state gate" in str(error)
    assert "squeeze_breakout" in str(error)


# =============================================================================
# Schema validation: unknown keys are rejected fail-closed
# =============================================================================


def test_unknown_top_level_key_is_rejected():
    error = build_error(extra_setting=1)
    assert "'extra_setting'" in str(error)


@pytest.mark.parametrize(
    ("section", "bad"),
    [
        ("entry", {"handler": "squeeze_breakout", "cooldown": 0}),  # typo of cooldown_bars
        ("risk", {"stop_loss": {"type": "trigger_bar_extreme"}, "take_profit": {"type": "r_multiple", "r": 2.0}, "fee": 0.1}),
        ("exit", {"max_holding_bars": 24, "priority": "stop_loss"}),
    ],
)
def test_unknown_keys_under_known_objects_are_rejected(section, bad):
    error = build_error(**{section: bad})
    assert "unknown key" in str(error).lower()
    assert error.field_path == section


def test_unknown_key_under_stop_loss_and_take_profit_is_rejected():
    data = base_definition()
    data["risk"]["stop_loss"]["r"] = 1.0
    error = build_error(data)
    assert error.field_path == "stop_loss"

    data = base_definition()
    data["risk"]["take_profit"]["pct"] = 0.1
    error = build_error(data)
    assert error.field_path == "take_profit"


# =============================================================================
# Schema validation: entry.params structural rules (docs §7)
# =============================================================================


def _params_error(params):
    data = base_definition()
    data["entry"]["params"] = params
    return build_error(data)


def test_params_nested_mapping_is_rejected():
    error = _params_error({"breakout": {"kind": "bollinger_close"}})
    assert "nested mappings are not allowed" in str(error)
    assert error.field_path == "entry.params.breakout"


def test_params_nested_list_is_rejected():
    error = _params_error({"breakout": [["bollinger_close"]]})
    assert "nested lists/mappings are not allowed" in str(error)


def test_params_list_of_mappings_is_rejected():
    error = _params_error({"breakout": [{"kind": "bollinger_close"}]})
    assert "nested lists/mappings are not allowed" in str(error)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_params_nan_and_infinity_are_rejected(value):
    error = _params_error({"squeeze_state": value})
    assert "NaN / Infinity" in str(error)


def test_params_yaml_dot_nan_and_dot_inf_are_rejected(tmp_path):
    data = base_definition()
    path = tmp_path / "nan.yaml"
    text = yaml.safe_dump(data)
    path.write_text(text.replace("handler: squeeze_breakout", "handler: squeeze_breakout\n  params: {squeeze_state: .nan}"), encoding="utf-8")
    with pytest.raises(sd.StrategyDefinitionError, match="NaN / Infinity"):
        sd.load_strategy_definition(path)


def test_params_non_mapping_is_rejected():
    error = _params_error(["squeeze"])
    assert error.field_path == "entry.params"


def test_params_non_str_key_is_rejected():
    error = _params_error({1: "squeeze"})
    assert "param keys must be str" in str(error)


def test_params_unsupported_key_for_the_handler_is_rejected():
    error = _params_error({"bb_period": 20})
    assert "does not support this param" in str(error)
    assert "squeeze_state" in str(error)  # lists the supported params


def test_params_value_outside_the_handler_contract_is_rejected():
    error = _params_error({"squeeze_state": "trend_up"})
    assert "only allows" in str(error)
    assert error.field_path == "entry.params.squeeze_state"


def test_params_bool_contract_is_type_strict_never_accepts_int_one():
    # require_episode_release must be the bool True; the int 1 (== True in
    # Python) must not slip through the membership check.
    error = _params_error({"require_episode_release": 1})
    assert error.field_path == "entry.params.require_episode_release"
    error = _params_error({"require_episode_release": False})
    assert error.field_path == "entry.params.require_episode_release"


def test_params_explicit_null_resolves_to_the_handler_default():
    data = base_definition()
    data["entry"]["params"] = {"squeeze_state": None}
    definition = build(data)
    assert definition.entry.params["squeeze_state"] == "squeeze"


# =============================================================================
# Schema validation: entry.handler / reentry / cooldown_bars
# =============================================================================


def test_unregistered_handler_lists_registered_handlers():
    data = base_definition()
    data["entry"]["handler"] = "trend_follow"
    error = build_error(data)
    assert "'trend_follow'" in str(error)
    assert "squeeze_breakout" in str(error)
    assert error.field_path == "entry.handler"


def test_entry_handler_missing_is_an_error():
    data = base_definition()
    del data["entry"]["handler"]
    error = build_error(data)
    assert error.field_path == "entry.handler"


@pytest.mark.parametrize("value", ["bogus", 1, None])
def test_reentry_outside_the_enum_is_rejected(value):
    data = base_definition()
    data["entry"]["reentry"] = value
    error = build_error(data)
    assert error.field_path == "entry.reentry"
    assert "overlap" in str(error)


@pytest.mark.parametrize("value", ["overlap", "single_position"])
def test_reentry_valid_enum_value_not_allowed_by_squeeze_is_rejected(value):
    data = base_definition()
    data["entry"]["reentry"] = value
    error = build_error(data)
    assert "only supports" in str(error)
    assert "new_setup" in str(error)


def test_reentry_omitted_resolves_to_squeezes_fixed_new_setup():
    data = base_definition()
    del data["entry"]["reentry"]
    assert build(data).entry.reentry == "new_setup"


@pytest.mark.parametrize("value", [-1, 1.5, True, "0"])
def test_cooldown_bars_must_be_a_non_negative_int(value):
    data = base_definition()
    data["entry"]["cooldown_bars"] = value
    error = build_error(data)
    assert error.field_path == "entry.cooldown_bars"


def test_cooldown_bars_nonzero_is_rejected_by_squeeze_capability():
    data = base_definition()
    data["entry"]["cooldown_bars"] = 5
    error = build_error(data)
    assert "only supports cooldown_bars in [0]" in str(error)


def test_cooldown_bars_zero_and_omitted_both_resolve_to_zero():
    data = base_definition()
    data["entry"]["cooldown_bars"] = 0
    assert build(data).entry.cooldown_bars == 0
    data = base_definition()
    assert build(data).entry.cooldown_bars == 0


# =============================================================================
# Schema validation: risk (stop_loss / take_profit)
# =============================================================================


def test_stop_loss_type_missing_or_unknown_is_rejected():
    data = base_definition()
    data["risk"]["stop_loss"] = {}
    error = build_error(data)
    assert error.field_path == "risk.stop_loss.type"

    data = base_definition()
    data["risk"]["stop_loss"] = {"type": "atr"}
    error = build_error(data)
    assert "trigger_bar_extreme" in str(error)
    assert "fixed_pct" in str(error)


def test_fixed_pct_requires_pct():
    data = base_definition()
    data["risk"]["stop_loss"] = {"type": "fixed_pct"}
    error = build_error(data)
    assert error.field_path == "risk.stop_loss.pct"
    assert "missing" in str(error)


@pytest.mark.parametrize("value", [0, -0.5, float("nan"), float("inf"), True, "0.003"])
def test_fixed_pct_pct_must_be_a_finite_positive_number(value):
    data = base_definition()
    data["risk"]["stop_loss"] = {"type": "fixed_pct", "pct": value}
    error = build_error(data)
    assert error.field_path == "risk.stop_loss.pct"


def test_pct_with_trigger_bar_extreme_is_a_dead_setting_and_rejected():
    data = base_definition()
    data["risk"]["stop_loss"] = {"type": "trigger_bar_extreme", "pct": 0.003}
    error = build_error(data)
    assert "only valid when type='fixed_pct'" in str(error)


def test_fixed_pct_is_rejected_by_squeeze_capability_after_generic_checks():
    data = base_definition()
    data["risk"]["stop_loss"] = {"type": "fixed_pct", "pct": 0.003}
    error = build_error(data)
    assert "only supports" in str(error)
    assert "trigger_bar_extreme" in str(error)


def test_take_profit_type_missing_or_unknown_is_rejected():
    data = base_definition()
    data["risk"]["take_profit"] = {}
    error = build_error(data)
    assert error.field_path == "risk.take_profit.type"

    data = base_definition()
    data["risk"]["take_profit"] = {"type": "fixed_price", "r": 2.0}
    error = build_error(data)
    assert "r_multiple" in str(error)


def test_r_multiple_requires_r():
    data = base_definition()
    data["risk"]["take_profit"] = {"type": "r_multiple"}
    error = build_error(data)
    assert error.field_path == "risk.take_profit.r"
    assert "missing" in str(error)


@pytest.mark.parametrize("value", [0, -2.0, float("nan"), float("inf"), float("-inf"), True, "2.0"])
def test_r_must_be_a_finite_positive_number(value):
    data = base_definition()
    data["risk"]["take_profit"] = {"type": "r_multiple", "r": value}
    error = build_error(data)
    assert error.field_path == "risk.take_profit.r"


def test_r_as_int_is_normalized_to_float():
    data = base_definition()
    data["risk"]["take_profit"] = {"type": "r_multiple", "r": 3}
    definition = build(data)
    assert definition.risk.take_profit["r"] == 3.0
    assert isinstance(definition.risk.take_profit["r"], float)


# =============================================================================
# Schema validation: exit
# =============================================================================


@pytest.mark.parametrize("value", [0, -3, 1.5, True, "24", None])
def test_max_holding_bars_must_be_a_positive_int(value):
    data = base_definition()
    data["exit"]["max_holding_bars"] = value
    error = build_error(data)
    assert error.field_path == "exit.max_holding_bars"


def test_max_holding_bars_missing_is_an_error():
    data = base_definition()
    data["exit"] = {}
    error = build_error(data)
    assert error.field_path == "exit.max_holding_bars"


def test_same_candle_priority_unknown_value_is_rejected():
    data = base_definition()
    data["exit"]["same_candle_priority"] = "coin_flip"
    error = build_error(data)
    assert error.field_path == "exit.same_candle_priority"
    assert "stop_loss" in str(error)


def test_same_candle_priority_take_profit_is_rejected_by_squeeze_capability():
    data = base_definition()
    data["exit"]["same_candle_priority"] = "take_profit"
    error = build_error(data)
    assert "only supports" in str(error)
    assert "'take_profit'" in str(error)


def test_same_candle_priority_defaults_to_stop_loss():
    assert build().exit.same_candle_priority == "stop_loss"


# =============================================================================
# Directory registry loading (docs §5.1)
# =============================================================================


def _definition_with(id_, r=2.0):
    data = base_definition()
    data["id"] = id_
    data["risk"]["take_profit"]["r"] = r
    return data


def test_registry_loads_multiple_files_same_handler_different_ids(tmp_path):
    write_yaml(tmp_path, _definition_with("squeeze_a", r=2.0), name="a.yaml")
    write_yaml(tmp_path, _definition_with("squeeze_b", r=3.0), name="b.yml")
    registry = sd.load_strategy_registry(tmp_path)
    assert set(registry) == {"squeeze_a", "squeeze_b"}
    assert registry["squeeze_a"].risk.take_profit["r"] == 2.0
    assert registry["squeeze_b"].risk.take_profit["r"] == 3.0
    assert registry["squeeze_a"].entry.handler == registry["squeeze_b"].entry.handler


def test_registry_scan_order_is_deterministic_sorted_by_path(tmp_path):
    write_yaml(tmp_path, _definition_with("id_from_z"), name="z.yaml")
    write_yaml(tmp_path, _definition_with("id_from_a"), name="a.yaml")
    registry = sd.load_strategy_registry(tmp_path)
    assert list(registry) == ["id_from_a", "id_from_z"]  # insertion follows sorted filenames


def test_registry_ignores_non_yaml_files(tmp_path):
    write_yaml(tmp_path, _definition_with("only_one"))
    (tmp_path / "README.md").write_text("not a strategy", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("also not", encoding="utf-8")
    registry = sd.load_strategy_registry(tmp_path)
    assert list(registry) == ["only_one"]


def test_registry_duplicate_id_fails_the_whole_load_with_both_paths(tmp_path):
    write_yaml(tmp_path, _definition_with("dup_id", r=2.0), name="first.yaml")
    write_yaml(tmp_path, _definition_with("dup_id", r=3.0), name="second.yaml")
    with pytest.raises(sd.StrategyDefinitionError) as exc_info:
        sd.load_strategy_registry(tmp_path)
    message = str(exc_info.value)
    assert "duplicate strategy id 'dup_id'" in message
    assert "first.yaml" in message
    assert "second.yaml" in message


def test_registry_is_all_or_nothing_one_invalid_file_fails_everything(tmp_path):
    write_yaml(tmp_path, _definition_with("valid_one"), name="a_valid.yaml")
    (tmp_path / "b_broken.yaml").write_text("schema_version: 1\nid: [not, a, str]\n", encoding="utf-8")
    with pytest.raises(sd.StrategyDefinitionError) as exc_info:
        sd.load_strategy_registry(tmp_path)
    assert "b_broken.yaml" in str(exc_info.value)


def test_registry_error_order_is_deterministic_first_sorted_invalid_file_wins(tmp_path):
    (tmp_path / "a_broken.yaml").write_text("", encoding="utf-8")
    (tmp_path / "z_broken.yaml").write_text("", encoding="utf-8")
    with pytest.raises(sd.StrategyDefinitionError) as exc_info:
        sd.load_strategy_registry(tmp_path)
    assert "a_broken.yaml" in str(exc_info.value)


def test_registry_missing_directory_is_a_clear_error(tmp_path):
    with pytest.raises(sd.StrategyDefinitionError, match="does not exist"):
        sd.load_strategy_registry(tmp_path / "no_such_dir")


def test_registry_empty_directory_is_an_empty_registry(tmp_path):
    assert sd.load_strategy_registry(tmp_path) == {}


def test_default_directory_is_backtest_strategies(monkeypatch):
    monkeypatch.delenv(sd.STRATEGY_DIR_ENV_VAR, raising=False)
    assert sd.strategy_definitions_dir() == REPO_ROOT / "backtest" / "strategies"


def test_env_var_overrides_the_definitions_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(sd.STRATEGY_DIR_ENV_VAR, str(tmp_path))
    assert sd.strategy_definitions_dir() == tmp_path
    write_yaml(tmp_path, _definition_with("from_env_dir"))
    registry = sd.load_strategy_registry()  # no explicit directory
    assert list(registry) == ["from_env_dir"]


# =============================================================================
# docs §5.2: capability descriptor values == actual squeeze_breakout behaviour
# =============================================================================

SQUEEZE_CAP = sd.HANDLER_REGISTRY[sd.HANDLER_SQUEEZE_BREAKOUT]


def _exit_frame(bars, start="2026-06-10T00:00:00Z"):
    index = pd.date_range(start, periods=len(bars), freq="1h", tz="UTC")
    return pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=index)


# Same synthetic squeeze fixture as test_squeeze_breakout.py: 60 volatile bars
# then 34 flat bars -> bars 74..93 classify as Market State "squeeze", bar 94
# is the episode-end / Trigger-evaluation bar.
_VOLATILE, _FLAT = 60, 34
_TRIGGER_INDEX = _VOLATILE + _FLAT


def _squeeze_prefix(level=2000.0):
    bars = []
    for i in range(_VOLATILE):
        c = level + (3 if i % 2 == 0 else -3)
        bars.append((c, c + 0.02, c - 0.02, c))
    for i in range(_FLAT):
        c = level + (0.05 if i % 2 == 0 else -0.05)
        bars.append((c, c + 0.02, c - 0.02, c))
    return bars


def _squeeze_signals(bars, **kwargs):
    index = pd.date_range("2026-06-01T00:00:00Z", periods=len(bars), freq="1h", tz="UTC")
    frame = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=index)
    frame["volume"] = 100.0
    return frame, sb.evaluate_squeeze_breakout_signals(
        frame, frame.index[_TRIGGER_INDEX], frame.index[-1], symbol="XAU_USD", timeframe="1h", **kwargs
    )


def test_descriptor_same_candle_priority_matches_evaluate_squeeze_exit_tie_break():
    # Descriptor: stop_loss is the only allowed priority...
    assert SQUEEZE_CAP.allowed_same_candle_priorities == frozenset(
        {exit_policy.SAME_CANDLE_PRIORITY_STOP_LOSS}
    )
    # ...and the implementation resolves a same-bar SL+TP tie as stop_loss.
    candles = _exit_frame([(100.0, 111.0, 94.0, 100.0)])  # hits stop 95 AND target 110
    result = sb.evaluate_squeeze_exit(
        "long", entry_price=100.0, stop_price=95.0, target_price=110.0, candles=candles
    )
    assert result.status == exit_policy.STATUS_LOSS
    assert result.close_reason == exit_policy.REASON_STOP_LOSS


def test_descriptor_take_profit_r_multiple_matches_target_price_math():
    assert SQUEEZE_CAP.allowed_take_profit_types == frozenset({sd.TAKE_PROFIT_R_MULTIPLE})
    bars = _squeeze_prefix() + [(2009, 2010, 2005, 2010), (2011, 2012, 2011, 2011)]
    _frame, signals_r2 = _squeeze_signals(bars, risk_reward=2.0)
    _frame, signals_r3 = _squeeze_signals(bars, risk_reward=3.0)
    # entry 2010, stop 2005 (risk 5) -> target = entry + r * risk, from the r arg
    assert signals_r2[0].target_price == 2020.0
    assert signals_r3[0].target_price == 2025.0


def test_descriptor_stop_loss_trigger_bar_extreme_matches_stop_price():
    assert SQUEEZE_CAP.allowed_stop_loss_types == frozenset({sd.STOP_LOSS_TRIGGER_BAR_EXTREME})
    bars = _squeeze_prefix() + [(2009, 2010, 2005, 2010), (2011, 2012, 2011, 2011)]
    _frame, signals = _squeeze_signals(bars)
    assert signals[0].stop_price == 2005.0  # the Trigger bar's own low


def test_descriptor_max_holding_bars_matches_time_exit_bars_walk():
    flat_bar = (100.0, 100.4, 99.6, 100.0)  # never reaches stop 95 / target 110
    candles = _exit_frame([flat_bar] * 5)
    result = sb.evaluate_squeeze_exit(
        "long", entry_price=100.0, stop_price=95.0, target_price=110.0, candles=candles,
        time_exit_bars=2,
    )
    assert result.close_reason == exit_policy.REASON_TIME_EXPIRED
    assert result.close_time == candles.index[1].isoformat()  # the 2nd bar after entry


def test_descriptor_reentry_new_setup_and_cooldown_zero_match_the_state_machine():
    assert SQUEEZE_CAP.allowed_reentry_modes == frozenset({sd.REENTRY_NEW_SETUP})
    assert SQUEEZE_CAP.allowed_cooldown_bars == frozenset({0})
    # new_setup: one entry per episode; a NEW squeeze episode re-arms with no
    # time-based cooldown suppressing the second entry.
    tail = [(2011, 2012, 2004, 2005)]  # first entry stops out on the next bar
    flat2 = [
        (2005 + (0.05 if i % 2 == 0 else -0.05), 2005 + 0.07, 2005 - 0.07, 2005 + (0.05 if i % 2 == 0 else -0.05))
        for i in range(_FLAT)
    ]
    breakout2 = [(2014, 2016, 2010, 2015), (2015, 2026, 2015, 2020)]
    bars = _squeeze_prefix() + [(2009, 2010, 2005, 2010)] + tail + flat2 + breakout2
    _frame, signals = _squeeze_signals(bars)
    assert len(signals) == 2  # exactly one per episode, none suppressed by cooldown


def test_descriptor_params_fixed_values_match_the_implementation_constants():
    assert SQUEEZE_CAP.params["squeeze_state"].allowed_values == (sb.SQUEEZE_STATE,)
    assert SQUEEZE_CAP.params["squeeze_state"].default == sb.SQUEEZE_STATE
    assert SQUEEZE_CAP.params["require_episode_release"].allowed_values == (True,)
    # "bollinger_close" (a close break, wick pokes never trigger) is what
    # squeeze_breakout.TRIGGER_TYPE records on every saved signal.
    assert SQUEEZE_CAP.params["breakout"].allowed_values == ("bollinger_close",)
    assert sb.TRIGGER_TYPE == "bollinger_close_break"


def test_descriptor_market_state_gate_unsupported_matches_the_implementation():
    # squeeze_breakout never consults a market-state allow-list: its signal
    # evaluation has no such parameter at all, so the descriptor must say
    # "unsupported" (docs §6) -- otherwise a gate would be accepted and ignored.
    import inspect

    assert SQUEEZE_CAP.supports_market_state_gate is False
    parameters = inspect.signature(sb.evaluate_squeeze_breakout_signals).parameters
    assert "allowed_market_states" not in parameters


def test_schema_enums_match_the_exit_policy_vocabulary():
    # docs §8: same_candle_priority uses the exact exit_policy strings so the
    # layers stay cross-checkable.
    assert set(sd.KNOWN_SAME_CANDLE_PRIORITIES) == exit_policy.KNOWN_SAME_CANDLE_PRIORITIES
    assert sd.DEFAULT_SAME_CANDLE_PRIORITY == exit_policy.DEFAULT_SAME_CANDLE_PRIORITY


def test_default_risk_reward_and_time_exit_defaults_match_module_constants():
    # The sample definition's r=2.0 / max_holding_bars=24 pin the same values
    # as the module constants the non-external path still uses.
    definition = sd.load_strategy_definition(SAMPLE_YAML_PATH)
    assert definition.risk.take_profit["r"] == sb.RISK_REWARD
    assert definition.exit.max_holding_bars == sb.TIME_EXIT_BARS
