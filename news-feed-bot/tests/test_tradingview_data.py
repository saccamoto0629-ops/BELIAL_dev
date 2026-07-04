import pathlib
import sys

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import tradingview_data  # noqa: E402


class FakeAnalysis:
    def __init__(self, summary, indicators):
        self.summary = summary
        self.indicators = indicators


class FakeHandler:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def get_analysis(self):
        return FakeAnalysis(
            summary={"RECOMMENDATION": "BUY"},
            indicators={
                "RSI": 61.23,
                "MACD.macd": 1.1,
                "MACD.signal": 0.9,
                "EMA20": 150.0,
                "EMA50": 148.0,
                "EMA200": 140.0,
                "ADX": 28.5,
                "Stoch.K": 81.2,
                "Stoch.D": 76.4,
                "close": 151.0,
            },
        )


def test_get_technical_snapshot_returns_indicators(monkeypatch):
    monkeypatch.setattr(tradingview_data, "TA_Handler", FakeHandler)

    snapshot = tradingview_data.get_technical_snapshot(
        "USDJPY", tradingview_data.TRADINGVIEW_SYMBOLS["USDJPY"]
    )

    assert snapshot["recommendation"] == "BUY"
    assert snapshot["rsi"] == 61.23
    assert snapshot["macd"] == 1.1
    assert snapshot["adx"] == 28.5
    assert snapshot["stoch_k"] == 81.2
    assert snapshot["stoch_d"] == 76.4
    assert snapshot["close"] == 151.0


def test_get_technical_snapshot_returns_none_on_error(monkeypatch):
    class FailingHandler:
        def __init__(self, **kwargs):
            pass

        def get_analysis(self):
            raise RuntimeError("rate limited")

    monkeypatch.setattr(tradingview_data, "TA_Handler", FailingHandler)

    snapshot = tradingview_data.get_technical_snapshot(
        "USDJPY", tradingview_data.TRADINGVIEW_SYMBOLS["USDJPY"]
    )

    assert snapshot is None


def test_get_tradingview_data_returns_entry_per_symbol(monkeypatch):
    monkeypatch.setattr(tradingview_data, "TA_Handler", FakeHandler)

    data = tradingview_data.get_tradingview_data(
        symbols={"VIX": tradingview_data.TRADINGVIEW_SYMBOLS["VIX"]}
    )

    assert set(data.keys()) == {"VIX"}
    assert data["VIX"]["recommendation"] == "BUY"
