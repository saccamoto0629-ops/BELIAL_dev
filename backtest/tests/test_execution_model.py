"""Issue #26: unit tests for the fixed spread/slippage virtual-fill model.

These exercise execution_model.ExecutionModel in isolation (no DB, no
candles, no runner): the adjustment direction per side, the zero-cost
no-op, the None-close pass-through, and the validation errors for bad
directions / negative costs.
"""

import pathlib
import sys

import pytest

TESTS_DIR = pathlib.Path(__file__).resolve().parent
BOT_DIR = TESTS_DIR.parent

for _dir in (TESTS_DIR, BOT_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import execution_model as execution  # noqa: E402


RAW = 150.0
SPREAD = 0.01
SLIPPAGE = 0.005
COST = SPREAD + SLIPPAGE  # 0.015


# =============================================================================
# zero-cost no-op
# =============================================================================


def test_zero_spread_and_slippage_leaves_entry_price_unchanged():
    model = execution.ExecutionModel(spread=0.0, slippage=0.0)
    assert model.adjust_entry_price(RAW, "long") == pytest.approx(RAW)
    assert model.adjust_entry_price(RAW, "short") == pytest.approx(RAW)


def test_zero_spread_and_slippage_leaves_close_price_unchanged():
    model = execution.ExecutionModel(spread=0.0, slippage=0.0)
    assert model.adjust_close_price(RAW, "long") == pytest.approx(RAW)
    assert model.adjust_close_price(RAW, "short") == pytest.approx(RAW)


def test_default_model_is_zero_cost():
    model = execution.ExecutionModel()
    assert model.spread == 0.0
    assert model.slippage == 0.0
    assert model.total_cost == 0.0


# =============================================================================
# adjustment direction: always unfavorable to the position
# =============================================================================


def test_long_entry_is_raised_by_spread_plus_slippage():
    model = execution.ExecutionModel(spread=SPREAD, slippage=SLIPPAGE)
    assert model.adjust_entry_price(RAW, "long") == pytest.approx(RAW + COST)


def test_short_entry_is_lowered_by_spread_plus_slippage():
    model = execution.ExecutionModel(spread=SPREAD, slippage=SLIPPAGE)
    assert model.adjust_entry_price(RAW, "short") == pytest.approx(RAW - COST)


def test_long_close_is_lowered_by_spread_plus_slippage():
    model = execution.ExecutionModel(spread=SPREAD, slippage=SLIPPAGE)
    assert model.adjust_close_price(RAW, "long") == pytest.approx(RAW - COST)


def test_short_close_is_raised_by_spread_plus_slippage():
    model = execution.ExecutionModel(spread=SPREAD, slippage=SLIPPAGE)
    assert model.adjust_close_price(RAW, "short") == pytest.approx(RAW + COST)


# =============================================================================
# None close_price passes straight through (open / no_close)
# =============================================================================


def test_none_close_price_stays_none_for_long():
    model = execution.ExecutionModel(spread=SPREAD, slippage=SLIPPAGE)
    assert model.adjust_close_price(None, "long") is None


def test_none_close_price_stays_none_for_short():
    model = execution.ExecutionModel(spread=SPREAD, slippage=SLIPPAGE)
    assert model.adjust_close_price(None, "short") is None


# =============================================================================
# validation errors
# =============================================================================


@pytest.mark.parametrize("direction", ["buy", "sell", "LONG", "", None, "flat"])
def test_invalid_direction_raises_value_error_on_entry(direction):
    model = execution.ExecutionModel(spread=SPREAD, slippage=SLIPPAGE)
    with pytest.raises(ValueError):
        model.adjust_entry_price(RAW, direction)


@pytest.mark.parametrize("direction", ["buy", "sell", "LONG", "", None, "flat"])
def test_invalid_direction_raises_value_error_on_close(direction):
    model = execution.ExecutionModel(spread=SPREAD, slippage=SLIPPAGE)
    with pytest.raises(ValueError):
        model.adjust_close_price(RAW, direction)


@pytest.mark.parametrize("spread", [-0.01, -1, -100.0])
def test_negative_spread_raises_value_error(spread):
    with pytest.raises(ValueError):
        execution.ExecutionModel(spread=spread, slippage=0.0)


@pytest.mark.parametrize("slippage", [-0.01, -1, -100.0])
def test_negative_slippage_raises_value_error(slippage):
    with pytest.raises(ValueError):
        execution.ExecutionModel(spread=0.0, slippage=slippage)


# =============================================================================
# config (de)serialization does not mutate its input, round-trips cleanly
# =============================================================================


def test_to_config_has_the_expected_shape():
    model = execution.ExecutionModel(spread=SPREAD, slippage=SLIPPAGE)
    assert model.to_config() == {
        "type": execution.EXECUTION_MODEL_TYPE,
        "spread": SPREAD,
        "slippage": SLIPPAGE,
    }


def test_to_config_returns_a_fresh_dict_each_call():
    model = execution.ExecutionModel(spread=SPREAD, slippage=SLIPPAGE)
    first = model.to_config()
    first["spread"] = 999.0  # mutating the returned dict must not affect the model
    assert model.to_config()["spread"] == pytest.approx(SPREAD)


def test_from_config_does_not_mutate_the_input_dict():
    config = {"type": execution.EXECUTION_MODEL_TYPE, "spread": SPREAD, "slippage": SLIPPAGE}
    snapshot = dict(config)

    model = execution.ExecutionModel.from_config(config)

    assert config == snapshot  # input dict left completely untouched
    assert model.spread == pytest.approx(SPREAD)
    assert model.slippage == pytest.approx(SLIPPAGE)


def test_from_config_defaults_missing_costs_to_zero():
    model = execution.ExecutionModel.from_config({"type": execution.EXECUTION_MODEL_TYPE})
    assert model.spread == 0.0
    assert model.slippage == 0.0
