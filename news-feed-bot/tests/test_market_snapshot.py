import pathlib
import sys

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import market_snapshot as snap  # noqa: E402


def make_snapshot(latest, previous):
    diff = latest - previous
    pct_change = (diff / previous) * 100 if previous else 0.0
    return {"latest": latest, "previous": previous, "diff": diff, "pct_change": pct_change}


def test_compute_risk_label_risk_off_on_high_vix():
    market_data = {
        "VIX": make_snapshot(30.0, 20.0),
        "SP500": make_snapshot(5400.0, 5420.0),
        "NASDAQ": make_snapshot(17000.0, 17200.0),
    }

    assert snap.compute_risk_label(market_data, None) == "Risk Off"


def test_compute_risk_label_risk_off_on_vix_spike_pct():
    market_data = {
        "VIX": make_snapshot(20.0, 17.0),  # ~17.6% jump, below 25 but above +15% pct
        "SP500": make_snapshot(5400.0, 5420.0),
        "NASDAQ": make_snapshot(17000.0, 17200.0),
    }

    assert snap.compute_risk_label(market_data, None) == "Risk Off"


def test_compute_risk_label_risk_off_on_high_vvix():
    market_data = {
        "VIX": make_snapshot(19.0, 18.5),
        "VVIX": make_snapshot(105.0, 95.0),
        "SP500": make_snapshot(5450.0, 5420.0),
        "NASDAQ": make_snapshot(17300.0, 17200.0),
    }

    assert snap.compute_risk_label(market_data, None) == "Risk Off"


def test_compute_risk_label_risk_on_when_vix_low_and_indices_up():
    market_data = {
        "VIX": make_snapshot(17.0, 17.5),
        "SP500": make_snapshot(5450.0, 5420.0),
        "NASDAQ": make_snapshot(17300.0, 17200.0),
    }

    assert snap.compute_risk_label(market_data, None) == "Risk On"


def test_compute_risk_label_watch_when_yields_rise_and_indices_fall():
    market_data = {
        "VIX": make_snapshot(19.0, 18.9),
        "SP500": make_snapshot(5380.0, 5420.0),
        "NASDAQ": make_snapshot(17100.0, 17200.0),
    }
    fred_data = {"US10Y": {"latest": 4.36, "previous": 4.30, "diff": 0.06, "date": "2026-07-03"}}

    assert snap.compute_risk_label(market_data, fred_data) == "Watch"


def test_compute_risk_label_watch_when_dollar_index_rises():
    market_data = {
        "VIX": make_snapshot(19.0, 18.9),
        "SP500": make_snapshot(5420.0, 5420.0),
        "NASDAQ": make_snapshot(17200.0, 17200.0),
        "DOLLAR_INDEX": make_snapshot(106.0, 105.0),
    }

    assert snap.compute_risk_label(market_data, None) == "Watch"


def test_compute_risk_label_neutral_otherwise():
    market_data = {
        "VIX": make_snapshot(19.0, 18.9),
        "SP500": make_snapshot(5420.0, 5420.0),
        "NASDAQ": make_snapshot(17200.0, 17200.0),
    }

    assert snap.compute_risk_label(market_data, None) == "Neutral"


def test_format_snapshot_line_reports_unavailable_when_missing():
    assert snap.format_snapshot_line("S&P500", None) == "- S&P500: unavailable"


def test_format_vix_section_matches_expected_layout():
    vix = make_snapshot(18.20, 17.40)

    section = snap.format_vix_section(vix)

    assert section == "[VIX]\n- latest: 18.20\n- change: +0.80 (+4.60%)"


def test_format_technical_line_reports_unavailable_without_rsi():
    assert snap.format_technical_line("VIX", None) == "- VIX: unavailable"
    assert snap.format_technical_line("VIX", {"rsi": None}) == "- VIX: unavailable"


def test_build_market_snapshot_message_includes_all_sections():
    market_data = {
        "VIX": make_snapshot(18.20, 17.40),
        "SP500": make_snapshot(5420.10, 5439.10),
        "NASDAQ": make_snapshot(17200.50, 17338.10),
        "USDJPY": make_snapshot(161.20, 160.80),
        "GOLD": make_snapshot(2360.00, 2350.60),
        "DOLLAR_INDEX": make_snapshot(105.20, 105.00),
        "OIL": make_snapshot(81.10, 80.00),
        "VVIX": make_snapshot(92.0, 90.0),
    }
    fred_data = {
        "US10Y": {"latest": 4.48, "previous": 4.42, "diff": 0.06, "date": "2026-07-03"},
        "US2Y": {"latest": 4.17, "previous": 4.12, "diff": 0.05, "date": "2026-07-03"},
        "10Y2Y_SPREAD": {"latest": 0.35, "previous": 0.34, "diff": 0.01, "date": "2026-07-03"},
        "FED_FUNDS": {"latest": 3.63, "previous": 3.63, "diff": 0.0, "date": "2026-07-01"},
        "CPI": {"latest": 315.2, "previous": 314.9, "diff": 0.3, "date": "2026-06-01"},
        "PCE": {"latest": 124.1, "previous": 123.9, "diff": 0.2, "date": "2026-06-01"},
        "UNEMPLOYMENT": {"latest": 4.1, "previous": 4.0, "diff": 0.1, "date": "2026-06-01"},
    }
    tv_data = {
        "USDJPY": {"recommendation": "BUY", "rsi": 61.2, "adx": 24.0, "stoch_k": 70.0, "stoch_d": 65.0},
        "XAUUSD": {"recommendation": "STRONG_BUY", "rsi": 68.4},
        "SPX": {"recommendation": "NEUTRAL", "rsi": 52.1},
        "VIX": {"recommendation": "SELL", "rsi": 44.0},
    }

    calendar_events = [
        {
            "datetime": "2026-07-04T21:30:00",
            "country": "United States",
            "event": "CPI",
            "importance": "3",
            "forecast": "3.1%",
            "previous": "3.0%",
            "actual": None,
        }
    ]
    fedwatch = {
        "next_meeting": "2026-07-29",
        "hold_probability": 78.0,
        "cut_probability": 22.0,
        "hike_probability": 0.0,
        "updated_at": "2026-07-04T00:00:00Z",
    }

    message = snap.build_market_snapshot_message(
        market_data,
        fred_data,
        tv_data,
        calendar_events,
        fedwatch,
        chart_snapshots=[
            {
                "symbol": "SPX",
                "timeframe": "1h",
                "trend": "Bull",
                "state": "Inside band",
                "freshness": "Fresh",
                "bias_weight": 1.0,
                "bb_position": 0.67,
            }
        ],
    )

    assert "[VIX]" in message
    assert "- S&P500: 5,420.10" in message
    assert "- Dollar Index: 105.20" in message
    assert "- USDJPY: 161.20" in message
    assert "- Oil: 81.10" in message
    assert "- VVIX: 92.00" in message
    assert "- US10Y: 4.48% as of 2026-07-03" in message
    assert "- US2Y: 4.17% as of 2026-07-03" in message
    assert "- 10Y-2Y Spread: 0.35% as of 2026-07-03" in message
    assert "- EFFR / DFF: 3.63% as of 2026-07-01" in message
    assert "Effective Fed Funds" not in message
    assert "- CPI: 315.20 as of 2026-06-01" in message
    assert "- PCE: 124.10 as of 2026-06-01" in message
    assert "- Unemployment: 4.10% as of 2026-06-01" in message
    assert "- USDJPY: BUY / RSI 61.2 / ADX 24.0 / STOCH 70.0/65.0" in message
    assert "[Chart Snapshot Summary]" in message
    assert "- SPX 1h: Trend Bull / State Inside band / Freshness Fresh / Bias Weight 1.00 / BB Position 0.67" in message
    assert "📅 Today's Events" in message
    assert "🇺🇸 CPI ★★★" in message
    assert "21:30" in message
    assert "🏦 FedWatch" in message
    assert "Hold\n78%" in message
    assert "Cut\n22%" in message
    assert "[Risk Label]" in message


def test_build_market_snapshot_message_shows_unavailable_without_fred():
    market_data = {"VIX": make_snapshot(18.20, 17.40)}
    tv_data = {}

    message = snap.build_market_snapshot_message(market_data, None, tv_data)

    assert "[Rates / FRED]\n- unavailable" in message


def test_format_chart_summary_section_reports_unavailable_when_missing():
    assert snap.format_chart_summary_section([]) == "[Chart Snapshot Summary]\n- unavailable"


def test_get_market_snapshot_message_uses_all_three_sources(monkeypatch):
    monkeypatch.setattr(snap, "get_market_data", lambda **kwargs: {"VIX": make_snapshot(18.2, 17.4)})
    monkeypatch.setattr(snap, "get_fred_data", lambda api_key, **kwargs: None)
    monkeypatch.setattr(snap, "get_tradingview_data", lambda **kwargs: {})
    monkeypatch.setattr(snap, "get_chart_snapshot_summaries", lambda: [])
    monkeypatch.setattr(snap, "get_economic_calendar", lambda: [])
    monkeypatch.setattr(snap, "get_fedwatch", lambda: None)

    message = snap.get_market_snapshot_message()

    assert "Market Snapshot" in message
    assert "[Risk Label]" in message


def test_fetch_snapshot_sources_runs_providers_and_keeps_partial_failures():
    def market_provider():
        return {"VIX": make_snapshot(18.2, 17.4)}

    def fred_provider():
        raise RuntimeError("fred down")

    def tradingview_provider():
        return {"USDJPY": {"recommendation": "BUY", "rsi": 61.2}}

    def chart_provider():
        return [{"symbol": "USDJPY", "trend": "Bull", "freshness": "Fresh", "bias_weight": 1.0, "bb_position": 0.5}]

    def calendar_provider():
        return [
            {
                "datetime": "2026-07-04T21:30:00",
                "country": "United States",
                "event": "CPI",
                "importance": "3",
                "forecast": None,
                "previous": None,
                "actual": None,
            }
        ]

    def fedwatch_provider():
        return {
            "next_meeting": "2026-07-29",
            "hold_probability": 78.0,
            "cut_probability": 22.0,
            "hike_probability": 0.0,
            "updated_at": "2026-07-04T00:00:00Z",
        }

    sources = snap.fetch_snapshot_sources(
        {
            "market": market_provider,
            "fred": fred_provider,
            "tradingview": tradingview_provider,
            "chart": chart_provider,
            "calendar": calendar_provider,
            "fedwatch": fedwatch_provider,
        }
    )

    assert sources["market"]["VIX"]["latest"] == 18.2
    assert sources["fred"] is None
    assert sources["tradingview"]["USDJPY"]["recommendation"] == "BUY"
    assert sources["chart"][0]["symbol"] == "USDJPY"
    assert sources["calendar"][0]["event"] == "CPI"
    assert sources["fedwatch"]["hold_probability"] == 78.0


def test_calendar_and_fedwatch_formatters_report_unavailable():
    assert snap.format_calendar_section([]) == "📅 Today's Events\n- unavailable"
    assert snap.format_fedwatch_section(None) == "🏦 FedWatch\n- unavailable"


def test_optional_calendar_and_fedwatch_are_disabled_by_default(monkeypatch):
    calls = {"calendar": 0, "fedwatch": 0}

    monkeypatch.setattr(snap, "ENABLE_ECONOMIC_CALENDAR", False)
    monkeypatch.setattr(snap, "ENABLE_FEDWATCH", False)
    monkeypatch.setattr(snap, "get_market_data", lambda **kwargs: {})
    monkeypatch.setattr(snap, "get_fred_data", lambda api_key, **kwargs: None)
    monkeypatch.setattr(snap, "get_tradingview_data", lambda **kwargs: {})
    monkeypatch.setattr(snap, "get_chart_snapshot_summaries", lambda: [])

    def calendar_provider(**kwargs):
        calls["calendar"] += 1
        return []

    def fedwatch_provider(**kwargs):
        calls["fedwatch"] += 1
        return None

    monkeypatch.setattr(snap, "get_economic_calendar", calendar_provider)
    monkeypatch.setattr(snap, "get_fedwatch", fedwatch_provider)

    sources = snap.fetch_snapshot_sources()

    assert sources["calendar"] == []
    assert sources["fedwatch"] is None
    assert calls == {"calendar": 0, "fedwatch": 0}


def test_optional_calendar_and_fedwatch_run_only_when_enabled(monkeypatch):
    calls = {"calendar": 0, "fedwatch": 0}

    monkeypatch.setattr(snap, "ENABLE_ECONOMIC_CALENDAR", True)
    monkeypatch.setattr(snap, "ENABLE_FEDWATCH", True)
    monkeypatch.setattr(snap, "get_market_data", lambda **kwargs: {})
    monkeypatch.setattr(snap, "get_fred_data", lambda api_key, **kwargs: None)
    monkeypatch.setattr(snap, "get_tradingview_data", lambda **kwargs: {})
    monkeypatch.setattr(snap, "get_chart_snapshot_summaries", lambda: [])

    def calendar_provider(**kwargs):
        calls["calendar"] += 1
        return [
            {
                "datetime": "2026-07-04T21:30:00",
                "country": "United States",
                "event": "CPI",
                "importance": "3",
                "forecast": None,
                "previous": None,
                "actual": None,
            }
        ]

    def fedwatch_provider(**kwargs):
        calls["fedwatch"] += 1
        return {
            "next_meeting": "2026-07-29",
            "hold_probability": 78.0,
            "cut_probability": 22.0,
            "hike_probability": 0.0,
            "updated_at": "2026-07-04T00:00:00Z",
        }

    monkeypatch.setattr(snap, "get_economic_calendar", calendar_provider)
    monkeypatch.setattr(snap, "get_fedwatch", fedwatch_provider)

    sources = snap.fetch_snapshot_sources()

    assert sources["calendar"][0]["event"] == "CPI"
    assert sources["fedwatch"]["hold_probability"] == 78.0
    assert calls == {"calendar": 1, "fedwatch": 1}
