"""Fetch equity index, FX, and commodity snapshots from Yahoo Finance."""

import logging
import concurrent.futures
from typing import TypedDict

import yfinance as yf  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 5
HISTORY_PERIOD = "10d"
TNX_HISTORY_PERIOD = "30d"
MAX_SYMBOL_WORKERS = 6

MARKET_SYMBOLS = {
    "VIX": "^VIX",
    "SP500": "^GSPC",
    "NASDAQ": "^IXIC",
    "USDJPY": "JPY=X",
    "GOLD": "GC=F",
    "DOLLAR_INDEX": "DX-Y.NYB",
    "OIL": "CL=F",
    "VVIX": "^VVIX",
    # Quick 10Y proxy. Quoted at 10x the actual yield (see SYMBOL_SCALE below).
    "US10Y_QUICK": "^TNX",
}

SYMBOL_SCALE = {
    "^TNX": 10.0,
}

SYMBOL_PERIOD = {
    # ^TNX sometimes has sparse Yahoo rows. Keep it as a quick fallback only;
    # the primary rates source remains FRED when FRED_API_KEY is configured.
    "^TNX": TNX_HISTORY_PERIOD,
}


class MarketSnapshot(TypedDict):
    symbol: str
    latest: float
    previous: float
    diff: float
    pct_change: float


MarketData = dict[str, MarketSnapshot | None]


def fetch_recent_closes(
    symbol: str,
    *,
    period: str = HISTORY_PERIOD,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> list[float]:
    """Return recent valid closes for a symbol, newest first.

    Holidays and market closures simply produce fewer rows rather than gaps,
    so the first two entries are always the two most recent trading days.
    """
    ticker = yf.Ticker(symbol)
    history = ticker.history(period=period, timeout=timeout)
    closes = history["Close"].dropna()
    return [float(value) for value in closes.iloc[::-1]]


def get_symbol_snapshot(
    symbol: str,
    *,
    period: str = HISTORY_PERIOD,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> MarketSnapshot | None:
    """Return latest/previous/diff/pct_change for one symbol, or None if unavailable."""
    try:
        selected_period = SYMBOL_PERIOD.get(symbol, period)
        closes = fetch_recent_closes(symbol, period=selected_period, timeout=timeout)
        if len(closes) < 2:
            raise ValueError(f"Not enough close prices for {symbol}")

        scale = SYMBOL_SCALE.get(symbol, 1.0)
        latest = closes[0] / scale
        previous = closes[1] / scale
        diff = latest - previous
        pct_change = (diff / previous) * 100 if previous else 0.0

        return {
            "symbol": symbol,
            "latest": latest,
            "previous": previous,
            "diff": diff,
            "pct_change": pct_change,
        }
    except Exception as exc:
        logger.warning("[market_data] Failed to fetch %s: %s", symbol, exc)
        return None


def get_market_data(
    symbols: dict[str, str] | None = None,
    *,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
    period: str = HISTORY_PERIOD,
) -> MarketData:
    """Return a dict of label -> snapshot (or None) for each configured symbol."""
    symbols = symbols or MARKET_SYMBOLS
    results: MarketData = {label: None for label in symbols}
    if not symbols:
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_SYMBOL_WORKERS, len(symbols))) as executor:
        futures = {
            executor.submit(get_symbol_snapshot, symbol, period=period, timeout=timeout): label
            for label, symbol in symbols.items()
        }
        for future in concurrent.futures.as_completed(futures):
            label = futures[future]
            try:
                results[label] = future.result()
            except Exception as exc:
                logger.warning("[market_data] Failed to fetch %s: %s", label, exc)

    return results


if __name__ == "__main__":
    import json

    print(json.dumps(get_market_data(), ensure_ascii=False, indent=2))
