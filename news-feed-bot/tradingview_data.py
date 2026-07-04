"""Fetch technical-analysis indicators from TradingView via tradingview_ta."""

import logging
from typing import Any, TypedDict

from tradingview_ta import Interval, TA_Handler  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 5

class TradingViewSymbolConfig(TypedDict):
    symbol: str
    exchange: str
    screener: str


class TechnicalSnapshot(TypedDict, total=False):
    recommendation: str | None
    rsi: float | None
    macd: float | None
    macd_signal: float | None
    ema20: float | None
    ema50: float | None
    ema200: float | None
    adx: float | None
    stoch_k: float | None
    stoch_d: float | None
    close: float | None


TradingViewData = dict[str, TechnicalSnapshot | None]


TRADINGVIEW_SYMBOLS: dict[str, TradingViewSymbolConfig] = {
    "USDJPY": {"symbol": "USDJPY", "exchange": "FX_IDC", "screener": "forex"},
    "XAUUSD": {"symbol": "XAUUSD", "exchange": "OANDA", "screener": "cfd"},
    "SPX": {"symbol": "SPX", "exchange": "SP", "screener": "america"},
    "VIX": {"symbol": "VIX", "exchange": "CBOE", "screener": "america"},
}


def _as_optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def get_technical_snapshot(
    label: str,
    config: TradingViewSymbolConfig,
    *,
    interval: str = Interval.INTERVAL_1_DAY,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> TechnicalSnapshot | None:
    """Return recommendation + key indicators for one symbol, or None if unavailable."""
    try:
        handler = TA_Handler(
            symbol=config["symbol"],
            exchange=config["exchange"],
            screener=config["screener"],
            interval=interval,
            timeout=timeout,
        )
        analysis = handler.get_analysis()
        indicators = analysis.indicators
        return {
            "recommendation": (analysis.summary or {}).get("RECOMMENDATION"),
            "rsi": _as_optional_float(indicators.get("RSI")),
            "macd": _as_optional_float(indicators.get("MACD.macd")),
            "macd_signal": _as_optional_float(indicators.get("MACD.signal")),
            "ema20": _as_optional_float(indicators.get("EMA20")),
            "ema50": _as_optional_float(indicators.get("EMA50")),
            "ema200": _as_optional_float(indicators.get("EMA200")),
            "adx": _as_optional_float(indicators.get("ADX")),
            "stoch_k": _as_optional_float(indicators.get("Stoch.K")),
            "stoch_d": _as_optional_float(indicators.get("Stoch.D")),
            "close": _as_optional_float(indicators.get("close")),
        }
    except Exception as exc:
        logger.warning("[tradingview_data] Failed to fetch %s: %s", label, exc)
        return None


def get_tradingview_data(
    symbols: dict[str, TradingViewSymbolConfig] | None = None,
    *,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> TradingViewData:
    """Return a dict of label -> snapshot (or None) for each configured symbol."""
    selected_symbols = symbols or TRADINGVIEW_SYMBOLS
    return {
        label: get_technical_snapshot(label, config, timeout=timeout)
        for label, config in selected_symbols.items()
    }


if __name__ == "__main__":
    import json

    print(json.dumps(get_tradingview_data(), ensure_ascii=False, indent=2))
