"""Build a Discord-ready market snapshot from independent market providers."""

import concurrent.futures
import logging
import os
import sys
from pathlib import Path
from datetime import datetime
from typing import Callable, TypedDict, cast

from economic_calendar import EconomicEvent, get_economic_calendar
from fedwatch import FedWatchSnapshot, get_fedwatch
from fred_data import get_fred_data
from market_data import get_market_data
from market_bias import classify_market_bias, format_market_bias_section
from tradingview_data import get_tradingview_data

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
MARKET_WATCH_DIR = BASE_DIR.parent / "market-watch"
ENV_FILE = BASE_DIR / ".env"

if MARKET_WATCH_DIR.exists() and str(MARKET_WATCH_DIR) not in sys.path:
    sys.path.insert(0, str(MARKET_WATCH_DIR))

try:
    from chart_collector import chart_db_path, load_chart_targets, load_env_file as load_chart_env_file
    from chart_snapshot import collect_and_build_snapshots
except Exception as exc:  # optional chart layer; keep Market Snapshot alive
    chart_db_path = None
    load_chart_targets = None
    load_chart_env_file = None
    collect_and_build_snapshots = None
    logger.debug("[market_snapshot] Chart Snapshot summary unavailable: %s", exc)

VIX_RISK_OFF_LEVEL = 25
VVIX_RISK_OFF_LEVEL = 100
VIX_RISK_OFF_PCT_CHANGE = 15
VIX_RISK_ON_LEVEL = 18
US10Y_WATCH_DIFF = 0.05
PROVIDER_TIMEOUT_SECONDS = 8
FAST_PROVIDER_TIMEOUT_SECONDS = 3


ProviderResult = object
ProviderCallable = Callable[[], ProviderResult]


class SnapshotSources(TypedDict):
    market: dict
    fred: dict | None
    tradingview: dict
    chart: list
    calendar: list[EconomicEvent]
    fedwatch: FedWatchSnapshot | None


def load_env_file(path=ENV_FILE):
    if not path.exists():
        return {}

    values = {}
    with path.open("r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            if key:
                values[key] = value
    return values


ENV_VALUES = load_env_file()
FRED_API_KEY = (ENV_VALUES.get("FRED_API_KEY") or os.getenv("FRED_API_KEY", "")).strip()


def env_bool(name: str, default: bool = False) -> bool:
    value = (ENV_VALUES.get(name) or os.getenv(name, "")).strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


ENABLE_FEDWATCH = env_bool("ENABLE_FEDWATCH", default=False)
ENABLE_ECONOMIC_CALENDAR = env_bool("ENABLE_ECONOMIC_CALENDAR", default=False)


def format_number(value: float, decimals: int = 2) -> str:
    return f"{value:,.{decimals}f}"


def format_signed(value: float, decimals: int = 2, suffix: str = "") -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.{decimals}f}{suffix}"


def format_snapshot_line(name: str, snapshot: dict | None) -> str:
    if not snapshot:
        return f"- {name}: unavailable"
    return f"- {name}: {format_number(snapshot['latest'])} {format_signed(snapshot['pct_change'], suffix='%')}"


def format_vix_section(vix: dict | None) -> str:
    if not vix:
        return "[VIX]\n- unavailable"
    return (
        "[VIX]\n"
        f"- latest: {format_number(vix['latest'])}\n"
        f"- change: {format_signed(vix['diff'])} ({format_signed(vix['pct_change'], suffix='%')})"
    )


def format_fred_line(name: str, snapshot: dict | None, suffix: str = "%") -> str:
    if not snapshot:
        return f"- {name}: unavailable"
    date = snapshot.get("date")
    date_suffix = f" as of {date}" if date else ""
    return f"- {name}: {format_number(snapshot['latest'])}{suffix}{date_suffix}"


def format_technical_line(name: str, snapshot: dict | None) -> str:
    if not snapshot or snapshot.get("rsi") is None:
        return f"- {name}: unavailable"
    recommendation = snapshot.get("recommendation") or "N/A"
    extras = []
    if snapshot.get("adx") is not None:
        extras.append(f"ADX {snapshot['adx']:.1f}")
    if snapshot.get("stoch_k") is not None and snapshot.get("stoch_d") is not None:
        extras.append(f"STOCH {snapshot['stoch_k']:.1f}/{snapshot['stoch_d']:.1f}")
    suffix = f" / {' / '.join(extras)}" if extras else ""
    return f"- {name}: {recommendation} / RSI {snapshot['rsi']:.1f}{suffix}"


def country_flag(country: str) -> str:
    normalized = country.strip().lower()
    if normalized in {"united states", "usa", "us", "u.s.", "u.s.a."}:
        return "🇺🇸"
    if normalized in {"japan", "jp"}:
        return "🇯🇵"
    if normalized in {"euro area", "eurozone", "european union", "eu"}:
        return "🇪🇺"
    if normalized in {"united kingdom", "uk", "great britain"}:
        return "🇬🇧"
    if normalized in {"china", "cn"}:
        return "🇨🇳"
    return "🌐"


def importance_stars(importance: str) -> str:
    normalized = importance.strip().lower()
    if normalized in {"3", "high", "★★★"}:
        return "★★★"
    if normalized in {"2", "medium", "moderate", "★★"}:
        return "★★"
    if normalized in {"1", "low", "★"}:
        return "★"
    return "★" if normalized else ""


def format_event_time(value: str) -> str:
    if not value:
        return "time N/A"
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).strftime("%H:%M")
    except ValueError:
        return value


def format_calendar_section(events: list[EconomicEvent] | None, max_events: int = 5) -> str:
    lines = ["📅 Today's Events"]
    if not events:
        lines.append("- unavailable")
        return "\n".join(lines)

    for event in events[:max_events]:
        flag = country_flag(event.get("country", ""))
        stars = importance_stars(event.get("importance", ""))
        lines.append(f"{flag} {event.get('event') or 'Unknown event'} {stars}".rstrip())
        lines.append(format_event_time(event.get("datetime", "")))
    return "\n".join(lines)


def _format_probability(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.0f}%"


def format_fedwatch_section(snapshot: FedWatchSnapshot | None) -> str:
    lines = ["🏦 FedWatch"]
    if not snapshot:
        lines.append("- unavailable")
        return "\n".join(lines)

    lines.append(f"Next meeting: {snapshot.get('next_meeting', 'unknown')}")
    lines.append("")
    lines.append("Hold")
    lines.append(_format_probability(snapshot.get("hold_probability")))
    lines.append("")
    lines.append("Cut")
    lines.append(_format_probability(snapshot.get("cut_probability")))
    lines.append("")
    lines.append("Hike")
    lines.append(_format_probability(snapshot.get("hike_probability")))
    return "\n".join(lines)


def get_chart_snapshot_summaries() -> list:
    """Return 1h chart snapshots for Market Snapshot summary only.

    Detailed Chart Snapshot Discord notifications remain owned by
    market-watch/chart_snapshot.py and are not sent from here.
    """
    if not all([chart_db_path, load_chart_targets, load_chart_env_file, collect_and_build_snapshots]):
        return []
    env_values = load_chart_env_file()
    targets = [target for target in load_chart_targets(env_values) if target.timeframe == "1h"]
    if not targets:
        return []
    return collect_and_build_snapshots(
        targets,
        chart_db_path(env_values),
        period=env_values.get("CHART_HISTORY_PERIOD", "60d"),
        collect=True,
    )


def _snapshot_get(snapshot, key, default=None):
    if isinstance(snapshot, dict):
        return snapshot.get(key, default)
    return getattr(snapshot, key, default)


def format_chart_summary_section(chart_snapshots: list | None) -> str:
    lines = ["[Chart Snapshot Summary]"]
    if not chart_snapshots:
        lines.append("- unavailable")
        return "\n".join(lines)

    for snapshot in chart_snapshots:
        lines.append(
            "- {symbol} 1h: Trend {trend} / State {state} / Freshness {freshness} "
            "/ Bias Weight {bias_weight:.2f} / BB Position {bb_position} / MACD State {macd_state}".format(
                symbol=_snapshot_get(snapshot, "symbol", "UNKNOWN"),
                trend=_snapshot_get(snapshot, "trend", "N/A"),
                state=_snapshot_get(snapshot, "state", "N/A"),
                freshness=_snapshot_get(snapshot, "freshness", "N/A"),
                bias_weight=float(_snapshot_get(snapshot, "bias_weight", 0.0) or 0.0),
                bb_position=(
                    "N/A"
                    if _snapshot_get(snapshot, "bb_position") is None
                    else f"{float(_snapshot_get(snapshot, 'bb_position')):.2f}"
                ),
                macd_state=_snapshot_get(snapshot, "macd_state", "N/A"),
            )
        )
    return "\n".join(lines)


def _is_up(snapshot: dict | None) -> bool:
    return bool(snapshot and snapshot.get("pct_change", 0) > 0)


def _is_down(snapshot: dict | None) -> bool:
    return bool(snapshot and snapshot.get("pct_change", 0) < 0)


def compute_risk_label(market_data: dict, fred_data: dict | None) -> str:
    """Apply the simple Risk On / Risk Off / Watch / Neutral rules."""
    vix = market_data.get("VIX")
    vvix = market_data.get("VVIX")
    sp500 = market_data.get("SP500")
    nasdaq = market_data.get("NASDAQ")
    dollar_index = market_data.get("DOLLAR_INDEX")
    us10y = (fred_data or {}).get("US10Y")

    if (
        (vix and (vix["latest"] > VIX_RISK_OFF_LEVEL or vix["pct_change"] >= VIX_RISK_OFF_PCT_CHANGE))
        or (vvix and vvix["latest"] > VVIX_RISK_OFF_LEVEL)
    ):
        return "Risk Off"

    if (
        vix and vix["latest"] < VIX_RISK_ON_LEVEL
        and _is_up(sp500)
        and _is_up(nasdaq)
    ):
        return "Risk On"

    if (
        (us10y and us10y.get("diff") is not None and us10y["diff"] >= US10Y_WATCH_DIFF)
        or _is_up(dollar_index)
        or _is_down(sp500)
        or _is_down(nasdaq)
    ):
        return "Watch"

    return "Neutral"


def build_market_snapshot_message(
    market_data: dict,
    fred_data: dict | None,
    tv_data: dict,
    calendar_events: list[EconomicEvent] | None = None,
    fedwatch: FedWatchSnapshot | None = None,
    *,
    macd_signal: str | None = None,
    chart_snapshots: list | None = None,
) -> str:
    """Build the Discord message body (plain Markdown) from already-fetched data."""
    lines = ["\U0001f4ca Market Snapshot", ""]

    lines.append(format_vix_section(market_data.get("VIX")))
    lines.append("")

    lines.append("[US Index]")
    lines.append(format_snapshot_line("S&P500", market_data.get("SP500")))
    lines.append(format_snapshot_line("Nasdaq", market_data.get("NASDAQ")))
    lines.append("")

    lines.append("[FX / Commodity]")
    lines.append(format_snapshot_line("USDJPY", market_data.get("USDJPY")))
    lines.append(format_snapshot_line("Dollar Index", market_data.get("DOLLAR_INDEX")))
    lines.append(format_snapshot_line("Gold", market_data.get("GOLD")))
    lines.append(format_snapshot_line("Oil", market_data.get("OIL")))
    lines.append(format_snapshot_line("VVIX", market_data.get("VVIX")))
    lines.append("")

    lines.append("[Rates / FRED]")
    if fred_data is None:
        lines.append("- unavailable")
    else:
        lines.append(format_fred_line("US10Y", fred_data.get("US10Y")))
        lines.append(format_fred_line("US2Y", fred_data.get("US2Y")))
        lines.append(format_fred_line("10Y-2Y Spread", fred_data.get("10Y2Y_SPREAD")))
        lines.append(format_fred_line("EFFR / DFF", fred_data.get("FED_FUNDS")))
        lines.append(format_fred_line("CPI", fred_data.get("CPI"), suffix=""))
        lines.append(format_fred_line("PCE", fred_data.get("PCE"), suffix=""))
        lines.append(format_fred_line("Unemployment", fred_data.get("UNEMPLOYMENT")))
    lines.append("")

    lines.append("[Technical / TradingView]")
    lines.append(format_technical_line("USDJPY", tv_data.get("USDJPY")))
    lines.append(format_technical_line("XAUUSD", tv_data.get("XAUUSD")))
    lines.append(format_technical_line("SPX", tv_data.get("SPX")))
    lines.append(format_technical_line("VIX", tv_data.get("VIX")))
    lines.append("")

    lines.append(format_chart_summary_section(chart_snapshots))
    lines.append("")

    lines.append(format_calendar_section(calendar_events))
    lines.append("")

    lines.append(format_fedwatch_section(fedwatch))
    lines.append("")

    lines.append("[Risk Label]")
    lines.append(compute_risk_label(market_data, fred_data))
    lines.append("")

    try:
        bias = classify_market_bias(
            market_data,
            fred_data,
            tv_data,
            macd_signal=macd_signal,
            chart_data=chart_snapshots,
        )
        lines.append(format_market_bias_section(bias))
    except Exception as exc:
        logger.warning("[market_snapshot] Market Bias failed: %s", exc)

    return "\n".join(lines)


def fetch_snapshot_sources(
    providers: dict[str, ProviderCallable] | None = None,
    *,
    timeout: int = PROVIDER_TIMEOUT_SECONDS,
) -> SnapshotSources:
    """Fetch all providers in parallel without allowing one provider to stop the bot."""
    if providers is None:
        providers = {
            "market": lambda: get_market_data(timeout=5),
            "fred": lambda: get_fred_data(FRED_API_KEY, timeout=5),
            "tradingview": lambda: get_tradingview_data(timeout=5),
            "chart": get_chart_snapshot_summaries,
        }
        if ENABLE_ECONOMIC_CALENDAR:
            providers["calendar"] = lambda: get_economic_calendar(timeout=FAST_PROVIDER_TIMEOUT_SECONDS)
        else:
            logger.debug("[market_snapshot] Economic Calendar optional provider disabled.")
        if ENABLE_FEDWATCH:
            providers["fedwatch"] = lambda: get_fedwatch(timeout=FAST_PROVIDER_TIMEOUT_SECONDS)
        else:
            logger.debug("[market_snapshot] FedWatch optional provider disabled.")
    defaults: SnapshotSources = {
        "market": {},
        "fred": None,
        "tradingview": {},
        "chart": [],
        "calendar": [],
        "fedwatch": None,
    }

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(providers))
    futures: dict[concurrent.futures.Future[ProviderResult], str] = {
        executor.submit(provider): name
        for name, provider in providers.items()
    }
    try:
        for future in concurrent.futures.as_completed(futures, timeout=timeout):
            name = futures[future]
            try:
                result = future.result(timeout=timeout)
                if name == "market":
                    defaults["market"] = result if isinstance(result, dict) else {}
                elif name == "fred":
                    defaults["fred"] = result if isinstance(result, dict) else None
                elif name == "tradingview":
                    defaults["tradingview"] = result if isinstance(result, dict) else {}
                elif name == "chart":
                    defaults["chart"] = result if isinstance(result, list) else []
                elif name == "calendar":
                    defaults["calendar"] = result if isinstance(result, list) else []
                elif name == "fedwatch":
                    defaults["fedwatch"] = cast(FedWatchSnapshot, result) if isinstance(result, dict) else None
            except Exception as exc:
                if name in {"calendar", "fedwatch"}:
                    logger.debug("[market_snapshot] optional %s provider failed", name, exc_info=True)
                else:
                    logger.warning("[market_snapshot] %s provider failed: %s", name, exc)
    except concurrent.futures.TimeoutError:
        unfinished = [name for future, name in futures.items() if not future.done()]
        logger.warning("[market_snapshot] Provider timeout: %s", ", ".join(unfinished))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return defaults


def get_market_snapshot_message() -> str:
    """Fetch all sources and return the finished Discord message body."""
    sources = fetch_snapshot_sources()
    return build_market_snapshot_message(
        sources["market"],
        sources["fred"],
        sources["tradingview"],
        sources["calendar"],
        sources["fedwatch"],
        chart_snapshots=sources["chart"],
    )


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(get_market_snapshot_message())
