"""Issue #49/#51: pure Open/Close judgement -- entry timing, deadline, weekend, gap, PnL."""

import pathlib
import sys

import pandas as pd
import pytest

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR.parent / "backtest"))
sys.path.insert(0, str(BASE_DIR.parent / "market-watch"))

import execution_engine as ee  # noqa: E402
import execution_profile as profiles  # noqa: E402

PROFILE = profiles.ExecutionProfile(max_entry_delay_bars=3, max_hold_bars=24, fallback_stop_pct=0.01, risk_reward=2.0)
NOW = pd.Timestamp("2026-07-06T23:00:00Z")  # a Monday evening, well after the candles below


def _candles(rows, start="2026-07-06T12:00:00Z"):
    idx = pd.date_range(start, periods=len(rows), freq="1h", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def _resolve(candles, *, side="long", signal="2026-07-06T12:00:00Z", deadline=None, profile=PROFILE, now=NOW):
    if deadline is None:
        deadline = ee.compute_entry_deadline(signal, "1h", profile.max_entry_delay_bars)
    return ee.resolve_entry(
        side=side, timeframe="1h", signal_candle_utc=signal, entry_deadline_utc=deadline,
        candles=candles, profile=profile, now_utc=now,
    )


# --- Entry: root candle is NOT the entry; next confirmed candle open is -------

def test_entry_is_next_confirmed_candle_open_not_signal_candle():
    # signal candle 12:00 (close 13:00); candles include the signal candle + next.
    candles = _candles([(50.0, 99, 1, 50), (100.0, 100.5, 99.9, 100.1), (101, 101, 100, 100.5)])
    out = _resolve(candles)
    assert out.action == "open"
    assert out.entry_time_utc == "2026-07-06T13:00:00Z"   # the NEXT candle, not 12:00
    assert out.entry_price == pytest.approx(100.0)        # its OPEN (not the signal candle close)


def test_entry_uses_first_confirmed_candle_after_missing_bars():
    # no 13:00 or 14:00 candle; the first existing confirmed candle is 15:00.
    idx = pd.to_datetime(["2026-07-06T12:00:00Z", "2026-07-06T15:00:00Z", "2026-07-06T16:00:00Z"])
    candles = pd.DataFrame(
        [(50, 51, 49, 50), (103.0, 103.2, 102.9, 103.1), (103, 103, 102, 102.5)],
        columns=["open", "high", "low", "close"], index=idx,
    )
    out = _resolve(candles, profile=profiles.ExecutionProfile(max_entry_delay_bars=4, fallback_stop_pct=0.01))
    assert out.action == "open"
    assert out.entry_time_utc == "2026-07-06T15:00:00Z"
    assert out.entry_price == pytest.approx(103.0)


# --- Entry deadline boundary --------------------------------------------------

def test_entry_deadline_exactly_on_boundary_opens():
    candles = _candles([(50, 51, 49, 50), (100.0, 100.1, 99.9, 100.0)])
    # deadline set exactly to the entry candle's open time -> still opens (inclusive).
    out = _resolve(candles, deadline="2026-07-06T13:00:00Z")
    assert out.action == "open"


def test_entry_deadline_one_second_over_expires():
    candles = _candles([(50, 51, 49, 50), (100.0, 100.1, 99.9, 100.0)])
    # deadline 1 second before the entry candle open -> expired (entry_timeout).
    out = _resolve(candles, deadline="2026-07-06T12:59:59Z")
    assert out.action == "expired"
    assert out.terminal_reason == "entry_timeout"


def test_no_candle_before_deadline_stays_pending():
    # only the signal candle exists; deadline not yet passed -> keep waiting.
    candles = _candles([(50, 51, 49, 50)])
    out = _resolve(candles, deadline="2026-07-06T20:00:00Z", now=pd.Timestamp("2026-07-06T13:30:00Z"))
    assert out.action == "pending"


def test_no_candle_after_deadline_expires():
    candles = _candles([(50, 51, 49, 50)])
    out = _resolve(candles, deadline="2026-07-06T13:00:00Z", now=pd.Timestamp("2026-07-06T20:00:00Z"))
    assert out.action == "expired"


# --- weekend carry-over refused ----------------------------------------------

def test_friday_signal_to_monday_entry_expires():
    # Friday 2026-07-10 20:00 signal (close 21:00); next candle is Monday 13:00.
    idx = pd.to_datetime(["2026-07-13T13:00:00Z", "2026-07-13T14:00:00Z"])
    candles = pd.DataFrame(
        [(100, 101, 99, 100), (100, 101, 99, 100)], columns=["open", "high", "low", "close"], index=idx
    )
    out = ee.resolve_entry(
        side="long", timeframe="1h", signal_candle_utc="2026-07-10T20:00:00Z",
        entry_deadline_utc="2026-07-20T00:00:00Z",  # deliberately generous
        candles=candles, profile=PROFILE, now_utc=pd.Timestamp("2026-07-13T20:00:00Z"),
    )
    assert out.action == "expired"
    assert out.terminal_reason == "entry_timeout"


def test_max_hold_deadline_computed():
    candles = _candles([(50, 51, 49, 50), (100.0, 100.1, 99.9, 100.0)])
    out = _resolve(candles)
    # entry 13:00 + 24 bars(1h) = next day 13:00
    assert out.max_hold_deadline_utc == "2026-07-07T13:00:00Z"


# --- Close walk ---------------------------------------------------------------

def _open_long():
    out = _resolve(_candles([(50, 51, 49, 50), (100.0, 100.1, 99.9, 100.0)]))
    return ee.OpenPositionView(
        side="long", timeframe="1h", entry_price=out.entry_price, stop_loss_price=out.stop_loss_price,
        take_profit_price=out.take_profit_price, max_hold_deadline_utc=out.max_hold_deadline_utc,
        same_candle_priority="stop_loss", spread=0.0, slippage=0.0, commission=0.0,
    )


def _close(view, rows, start="2026-07-06T13:00:00Z"):
    return ee.evaluate_close(view, _candles(rows, start=start))


def test_entry_candle_own_high_low_can_close_same_cycle():
    # entry 100 (SL 99, TP 102). The entry candle itself spikes to TP.
    view = _open_long()
    res = _close(view, [(100.0, 102.5, 99.9, 101.0)])
    assert res.closed and res.terminal_reason == "take_profit"
    assert res.close_price == pytest.approx(102.0)
    assert res.new_watermark == "2026-07-06T13:00:00Z"   # entry candle itself


def test_long_stop_loss():
    view = _open_long()
    res = _close(view, [(100.0, 100.2, 98.5, 99.2)])
    assert res.terminal_reason == "stop_loss"
    assert res.close_price == pytest.approx(99.0)
    assert res.pnl == pytest.approx(-1.0)


def test_long_take_profit():
    view = _open_long()
    res = _close(view, [(100.0, 100.1, 99.9, 100.0), (100.5, 102.5, 100.1, 101.0)])
    assert res.terminal_reason == "take_profit"
    assert res.close_price == pytest.approx(102.0)
    assert res.pnl == pytest.approx(2.0)


def test_gap_stop_fills_at_open():
    view = _open_long()
    res = _close(view, [(98.0, 98.5, 97.0, 98.2)])   # opens below the stop
    assert res.terminal_reason == "stop_loss"
    assert res.close_price == pytest.approx(98.0)     # raw open, not the SL level


def test_gap_take_profit_fills_at_target():
    view = _open_long()
    res = _close(view, [(103.0, 103.5, 102.5, 103.2)])  # opens above the target
    assert res.terminal_reason == "take_profit"
    assert res.close_price == pytest.approx(102.0)       # the fixed TP, not 103


def test_same_bar_priority_stop_loss_default():
    view = _open_long()
    res = _close(view, [(100.0, 102.5, 98.5, 101.0)])   # both SL and TP touched
    assert res.terminal_reason == "stop_loss"


def test_same_bar_priority_take_profit_when_configured():
    out = _resolve(_candles([(50, 51, 49, 50), (100.0, 100.1, 99.9, 100.0)]))
    view = ee.OpenPositionView(
        side="long", timeframe="1h", entry_price=out.entry_price, stop_loss_price=out.stop_loss_price,
        take_profit_price=out.take_profit_price, max_hold_deadline_utc=out.max_hold_deadline_utc,
        same_candle_priority="take_profit", spread=0.0, slippage=0.0, commission=0.0,
    )
    res = _close(view, [(100.0, 102.5, 98.5, 101.0)])
    assert res.terminal_reason == "take_profit"


def test_short_stop_and_take_profit():
    out = _resolve(_candles([(50, 51, 49, 50), (100.0, 100.1, 99.9, 100.0)]), side="short")
    view = ee.OpenPositionView(
        side="short", timeframe="1h", entry_price=out.entry_price, stop_loss_price=out.stop_loss_price,
        take_profit_price=out.take_profit_price, max_hold_deadline_utc=out.max_hold_deadline_utc,
        same_candle_priority="stop_loss", spread=0.0, slippage=0.0, commission=0.0,
    )
    # short: entry 100, SL 101, TP 98
    sl = _close(view, [(100.0, 101.5, 99.8, 100.6)])
    assert sl.terminal_reason == "stop_loss" and sl.close_price == pytest.approx(101.0)
    tp = _close(view, [(100.0, 100.2, 97.5, 98.0)])
    assert tp.terminal_reason == "take_profit" and tp.close_price == pytest.approx(98.0)


def test_max_hold_close_with_missing_bars():
    # max hold at entry+2h; a gap in the series still closes at the deadline bar's close.
    out = _resolve(
        _candles([(50, 51, 49, 50), (100.0, 100.1, 99.9, 100.0)]),
        profile=profiles.ExecutionProfile(max_hold_bars=2, fallback_stop_pct=0.05, risk_reward=2.0),
    )
    view = ee.OpenPositionView(
        side="long", timeframe="1h", entry_price=out.entry_price, stop_loss_price=out.stop_loss_price,
        take_profit_price=out.take_profit_price, max_hold_deadline_utc=out.max_hold_deadline_utc,
        same_candle_priority="stop_loss", spread=0.0, slippage=0.0, commission=0.0,
    )
    # deadline = 13:00 + 2h = 15:00. candle at 15:00 (open) closes at 16:00 >= deadline.
    idx = pd.to_datetime(["2026-07-06T13:00:00Z", "2026-07-06T15:00:00Z"])  # 14:00 missing
    candles = pd.DataFrame(
        [(100.0, 100.4, 99.7, 100.1), (100.2, 100.3, 99.8, 100.25)],
        columns=["open", "high", "low", "close"], index=idx,
    )
    res = ee.evaluate_close(view, candles)
    assert res.terminal_reason == "max_holding_time"
    assert res.close_price == pytest.approx(100.25)


def test_no_close_returns_watermark():
    view = _open_long()
    res = _close(view, [(100.0, 100.1, 99.9, 100.0), (100.0, 100.2, 99.8, 100.1)])
    assert res.closed is False
    assert res.new_watermark == "2026-07-06T14:00:00Z"   # last evaluated candle


def test_cost_applied_once_each_side():
    out = _resolve(
        _candles([(50, 51, 49, 50), (100.0, 100.1, 99.9, 100.0)]),
        profile=profiles.ExecutionProfile(spread=0.5, slippage=0.0, fallback_stop_pct=0.01, risk_reward=2.0),
    )
    # entry corrected once: 100 + 0.5 = 100.5; SL = 100.5 - 1.005 = 99.495
    assert out.entry_price == pytest.approx(100.5)
    view = ee.OpenPositionView(
        side="long", timeframe="1h", entry_price=out.entry_price, stop_loss_price=out.stop_loss_price,
        take_profit_price=out.take_profit_price, max_hold_deadline_utc=out.max_hold_deadline_utc,
        same_candle_priority="stop_loss", spread=0.5, slippage=0.0, commission=0.1,
    )
    res = _close(view, [(100.6, 100.9, out.stop_loss_price - 0.1, 99.6)])  # intrabar stop
    # close corrected once more: SL - 0.5; pnl = (close - entry) - commission
    assert res.close_price == pytest.approx(out.stop_loss_price - 0.5)
    assert res.pnl == pytest.approx((out.stop_loss_price - 0.5) - 100.5 - 0.1)
