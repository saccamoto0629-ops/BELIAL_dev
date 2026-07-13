"""Issue #51: pure Open/Close judgement -- long/short, gap, same-bar, PnL, costs."""

import pathlib
import sys

import pandas as pd
import pytest

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR.parent / "backtest"))

import execution_engine as ee  # noqa: E402
import execution_model as em  # noqa: E402


def _candles(rows):
    """rows: list of (open, high, low, close). Indexed by hourly UTC timestamps."""
    idx = pd.date_range("2026-07-05T13:00:00Z", periods=len(rows), freq="1h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def _long_view():
    od = ee.decide_open(side="long", entry_candle_close=100.0, stop_loss_pct=0.01, risk_reward=2.0)
    return od, ee.PositionView(side="long", entry_price=od.entry_price, stop_loss=od.stop_loss, take_profit=od.take_profit)


def _short_view():
    od = ee.decide_open(side="short", entry_candle_close=100.0, stop_loss_pct=0.01, risk_reward=2.0)
    return od, ee.PositionView(side="short", entry_price=od.entry_price, stop_loss=od.stop_loss, take_profit=od.take_profit)


# --- Open ---------------------------------------------------------------------

def test_long_open_levels():
    od = ee.decide_open(side="long", entry_candle_close=100.0, stop_loss_pct=0.01, risk_reward=2.0)
    assert od.entry_price == 100.0
    assert od.stop_loss == pytest.approx(99.0)
    assert od.take_profit == pytest.approx(102.0)


def test_short_open_levels():
    od = ee.decide_open(side="short", entry_candle_close=100.0, stop_loss_pct=0.01, risk_reward=2.0)
    assert od.entry_price == 100.0
    assert od.stop_loss == pytest.approx(101.0)
    assert od.take_profit == pytest.approx(98.0)


def test_open_condition_not_met_returns_none():
    assert ee.decide_open(side="Neutral", entry_candle_close=100.0) is None
    assert ee.decide_open(side=None, entry_candle_close=100.0) is None
    assert ee.decide_open(side="long", entry_candle_close=float("nan")) is None


# --- Close: long --------------------------------------------------------------

def test_long_take_profit():
    _od, pv = _long_view()
    cd = ee.evaluate_close(pv, _candles([(100.5, 102.5, 100.0, 101.0)]))
    assert cd.close_reason == "take_profit"
    assert cd.exit_price == pytest.approx(102.0)
    assert cd.gross_pnl == pytest.approx(2.0)
    assert cd.r_multiple == pytest.approx(2.0)


def test_long_stop_loss():
    _od, pv = _long_view()
    cd = ee.evaluate_close(pv, _candles([(100.0, 100.5, 98.5, 99.2)]))
    assert cd.close_reason == "stop_loss"
    assert cd.exit_price == pytest.approx(99.0)
    assert cd.gross_pnl == pytest.approx(-1.0)
    assert cd.r_multiple == pytest.approx(-1.0)


# --- Close: short -------------------------------------------------------------

def test_short_take_profit():
    _od, pv = _short_view()
    cd = ee.evaluate_close(pv, _candles([(99.5, 100.0, 97.5, 98.0)]))
    assert cd.close_reason == "take_profit"
    assert cd.exit_price == pytest.approx(98.0)
    assert cd.gross_pnl == pytest.approx(2.0)
    assert cd.r_multiple == pytest.approx(2.0)


def test_short_stop_loss():
    _od, pv = _short_view()
    cd = ee.evaluate_close(pv, _candles([(100.0, 101.5, 99.8, 100.6)]))
    assert cd.close_reason == "stop_loss"
    assert cd.exit_price == pytest.approx(101.0)
    assert cd.gross_pnl == pytest.approx(-1.0)
    assert cd.r_multiple == pytest.approx(-1.0)


def test_no_exit_stays_open():
    _od, pv = _long_view()
    cd = ee.evaluate_close(pv, _candles([(100.1, 100.4, 99.6, 100.2), (100.2, 100.5, 99.7, 100.1)]))
    assert cd is None


# --- gap ----------------------------------------------------------------------

def test_gap_stop_fills_at_open():
    _od, pv = _long_view()
    # opens below the stop (98 <= 99): a gap-through-stop fills at the raw open.
    cd = ee.evaluate_close(pv, _candles([(98.0, 98.5, 97.0, 98.2)]))
    assert cd.close_reason == "stop_loss"
    assert cd.exit_price == pytest.approx(98.0)


def test_gap_take_profit_fills_at_fixed_target():
    _od, pv = _long_view()
    # opens above the target (103 >= 102): conservative fill AT the fixed target.
    cd = ee.evaluate_close(pv, _candles([(103.0, 103.5, 102.5, 103.2)]))
    assert cd.close_reason == "take_profit"
    assert cd.exit_price == pytest.approx(102.0)   # not the better 103 open


# --- same-bar SL + TP tie -> Stop wins ---------------------------------------

def test_same_bar_stop_and_target_stop_wins():
    _od, pv = _long_view()
    # one bar spans BOTH stop (low 98.5 <= 99) and target (high 102.5 >= 102),
    # with no opening gap -> conservative: Stop wins.
    cd = ee.evaluate_close(pv, _candles([(100.0, 102.5, 98.5, 101.0)]))
    assert cd.close_reason == "stop_loss"
    assert cd.exit_price == pytest.approx(99.0)


# --- time exit ----------------------------------------------------------------

def test_time_exit_closes_at_bar_close():
    _od, pv = _long_view()
    rows = [(100.1, 100.4, 99.6, 100.2), (100.2, 100.5, 99.7, 100.3)]
    cd = ee.evaluate_close(pv, _candles(rows), time_exit_bars=2)
    assert cd.close_reason == "time_expired"
    assert cd.exit_price == pytest.approx(100.3)   # 2nd bar's close


def test_final_bar_stop_still_beats_time_exit():
    _od, pv = _long_view()
    # On the time-exit bar the stop is also touched -> the stop still wins.
    rows = [(100.1, 100.4, 99.6, 100.2), (100.0, 100.5, 98.5, 99.1)]
    cd = ee.evaluate_close(pv, _candles(rows), time_exit_bars=2)
    assert cd.close_reason == "stop_loss"


# --- missing column -----------------------------------------------------------

def test_missing_column_raises():
    _od, pv = _long_view()
    bad = _candles([(100.0, 101.0, 99.0, 100.0)]).drop(columns=["open"])
    with pytest.raises(ValueError):
        ee.evaluate_close(pv, bad)


def test_empty_candles_returns_none():
    _od, pv = _long_view()
    assert ee.evaluate_close(pv, _candles([])) is None


# --- spread / slippage applied once (no double application) -------------------

def test_spread_applied_once_tp_not_recorrected():
    model = em.ExecutionModel(spread=0.5, slippage=0.0)
    od = ee.decide_open(
        side="long", entry_candle_close=100.0, exec_model=model, stop_loss_pct=0.01, risk_reward=2.0
    )
    # entry corrected once: 100 + 0.5 = 100.5; target fixed at 100.5 + 2*(100.5*0.01)
    assert od.entry_price == pytest.approx(100.5)
    expected_target = 100.5 + 2 * (100.5 * 0.01)
    assert od.take_profit == pytest.approx(expected_target)

    pv = ee.PositionView(side="long", entry_price=od.entry_price, stop_loss=od.stop_loss, take_profit=od.take_profit)
    cd = ee.evaluate_close(
        pv, _candles([(100.6, expected_target + 0.2, 100.4, expected_target)]),
        exec_model=model, fee=0.1,
    )
    assert cd.close_reason == "take_profit"
    # TP fills at the fixed target -- NOT re-corrected by the close-side cost.
    assert cd.exit_price == pytest.approx(expected_target)
    assert cd.gross_pnl == pytest.approx(expected_target - 100.5)
    # fee (and only fee) reduces net; spread/slippage already embedded in prices.
    assert cd.net_pnl == pytest.approx(cd.gross_pnl - 0.1)
    assert cd.fee_cost == pytest.approx(0.1)
    assert cd.spread_cost == pytest.approx(0.5)


def test_spread_applied_once_on_stop_exit():
    model = em.ExecutionModel(spread=0.5, slippage=0.0)
    od = ee.decide_open(
        side="long", entry_candle_close=100.0, exec_model=model, stop_loss_pct=0.01, risk_reward=2.0
    )
    pv = ee.PositionView(side="long", entry_price=od.entry_price, stop_loss=od.stop_loss, take_profit=od.take_profit)
    # stop level = 100.5 - (100.5*0.01) = 99.495; a non-gap intrabar stop fills
    # there, then takes the close-side cost ONCE: 99.495 - 0.5 = 98.995.
    stop_level = od.stop_loss
    cd = ee.evaluate_close(pv, _candles([(100.6, 100.9, stop_level - 0.1, 99.6)]), exec_model=model)
    assert cd.close_reason == "stop_loss"
    assert cd.exit_price == pytest.approx(stop_level - 0.5)
