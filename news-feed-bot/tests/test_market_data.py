import pathlib
import sys

import pandas as pd

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import market_data  # noqa: E402


class FakeTicker:
    def __init__(self, closes):
        self._closes = closes
        self.calls = []

    def history(self, period=None, timeout=None):
        self.calls.append({"period": period, "timeout": timeout})
        return pd.DataFrame({"Close": self._closes})


def test_get_symbol_snapshot_computes_diff_and_pct(monkeypatch):
    monkeypatch.setattr(
        market_data.yf, "Ticker", lambda symbol: FakeTicker([5400.0, 5420.10])
    )

    snapshot = market_data.get_symbol_snapshot("^GSPC")

    assert snapshot["latest"] == 5420.10
    assert snapshot["previous"] == 5400.0
    assert round(snapshot["diff"], 2) == 20.10
    assert round(snapshot["pct_change"], 4) == round(20.10 / 5400.0 * 100, 4)


def test_get_symbol_snapshot_applies_tnx_scale(monkeypatch):
    # ^TNX is quoted at 10x the actual yield.
    fake_ticker = FakeTicker([42.5, 43.1])
    monkeypatch.setattr(market_data.yf, "Ticker", lambda symbol: fake_ticker)

    snapshot = market_data.get_symbol_snapshot("^TNX")

    assert round(snapshot["latest"], 2) == 4.31
    assert round(snapshot["previous"], 2) == 4.25
    assert fake_ticker.calls[0]["period"] == "30d"


def test_get_symbol_snapshot_uses_wider_default_period(monkeypatch):
    fake_ticker = FakeTicker([5400.0, 5420.10])
    monkeypatch.setattr(market_data.yf, "Ticker", lambda symbol: fake_ticker)

    market_data.get_symbol_snapshot("^GSPC")

    assert fake_ticker.calls[0]["period"] == "10d"


def test_get_symbol_snapshot_returns_none_when_not_enough_data(monkeypatch):
    monkeypatch.setattr(market_data.yf, "Ticker", lambda symbol: FakeTicker([5420.10]))

    assert market_data.get_symbol_snapshot("^GSPC") is None


def test_get_symbol_snapshot_returns_none_on_error(monkeypatch):
    def raise_error(symbol):
        raise RuntimeError("network down")

    monkeypatch.setattr(market_data.yf, "Ticker", raise_error)

    assert market_data.get_symbol_snapshot("^GSPC") is None


def test_get_market_data_returns_entry_per_symbol(monkeypatch):
    monkeypatch.setattr(
        market_data.yf, "Ticker", lambda symbol: FakeTicker([100.0, 101.0])
    )

    data = market_data.get_market_data(symbols={"VIX": "^VIX", "GOLD": "GC=F"})

    assert set(data.keys()) == {"VIX", "GOLD"}
    assert data["VIX"]["latest"] == 101.0


def test_default_market_symbols_include_added_yahoo_targets():
    assert market_data.MARKET_SYMBOLS["DOLLAR_INDEX"] == "DX-Y.NYB"
    assert market_data.MARKET_SYMBOLS["OIL"] == "CL=F"
    assert market_data.MARKET_SYMBOLS["VVIX"] == "^VVIX"
