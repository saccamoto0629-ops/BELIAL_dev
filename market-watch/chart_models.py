"""Typed models for chart collection and snapshot rendering.

This module is observation-only. It intentionally contains no order,
execution, position-sizing, or trading-action model.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChartTarget:
    symbol: str
    ticker: str
    timeframe: str
    display_name: str = ""
    chart_symbol: str = ""
    fallback_chart_symbol: str = ""
    fallback_ticker: str = ""
    asset_type: str = ""

    @property
    def label(self) -> str:
        return self.display_name or self.symbol


@dataclass(frozen=True)
class Candle:
    symbol: str
    timeframe: str
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class IndicatorSnapshot:
    symbol: str
    timeframe: str
    timestamp: str
    close: float
    sma20: float | None
    sma75: float | None
    ema20: float | None
    bb_lower: float | None
    bb_middle: float | None
    bb_upper: float | None
    bb_width_pct: float | None
    bb_position: float | None
    macd: float | None
    macd_signal: float | None
    macd_hist: float | None
    macd_state: str
    rsi14: float | None
    rsi_state: str
    freshness: str
    age_seconds: int
    bias_weight: float
    trend: str
    state: str
    display_name: str = ""
    chart_symbol: str = ""
    asset_type: str = ""

    @property
    def label(self) -> str:
        return self.display_name or self.symbol
