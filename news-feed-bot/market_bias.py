"""Rule-based Bull / Bear / Neutral market-bias classification.

The classifier consumes data that Market Snapshot already fetched. It does not
call external APIs and it never makes trading recommendations.
"""

from typing import Literal, TypedDict, cast

from market_bias_models import normalize_bias_symbol

MarketBiasLabel = Literal["Bull", "Bear", "Neutral"]
RiskLabel = Literal["Risk-On", "Risk-Off", "Neutral"]
VolatilityLabel = Literal["Low", "Normal", "High"]

NOTE = "Observation only. No trade recommendation."
BULLISH_RECOMMENDATIONS = {"BUY", "STRONG_BUY"}
BEARISH_RECOMMENDATIONS = {"SELL", "STRONG_SELL"}

# Risk axis thresholds. Independent of the Trend scoring above -- Risk is a
# market-environment filter (risk appetite), not a directional call.
RISK_ON_VIX_LEVEL = 18.0
RISK_OFF_VIX_LEVEL = 25.0
RISK_GOLD_PCT_THRESHOLD = 0.3
RISK_OIL_PCT_THRESHOLD = 1.5

# Volatility axis thresholds. A minimal classifier: chart Bollinger Band
# width (a stand-in for ATR/range expansion) plus raw day-over-day % moves.
VOLATILITY_HIGH_BB_WIDTH_PCT = 1.2
VOLATILITY_LOW_BB_WIDTH_PCT = 0.4
VOLATILITY_RAPID_CHANGE_PCT = 1.0


class MarketBiasResult(TypedDict):
    market_bias: MarketBiasLabel
    confidence: float
    scores: dict[str, float]
    reasons: list[str]
    warnings: list[str]
    note: str
    trend: MarketBiasLabel
    risk: RiskLabel
    risk_confidence: float
    risk_scores: dict[str, float]
    risk_reasons: list[str]
    volatility: VolatilityLabel
    volatility_confidence: float
    volatility_scores: dict[str, float]
    volatility_reasons: list[str]


def _pct(snapshot: dict | None) -> float | None:
    return snapshot.get("pct_change") if snapshot else None


def _latest(snapshot: dict | None) -> float | None:
    return snapshot.get("latest") if snapshot else None


def _diff(snapshot: dict | None) -> float | None:
    return snapshot.get("diff") if snapshot else None


def _is_up(snapshot: dict | None, threshold: float = 0.0) -> bool:
    pct = _pct(snapshot)
    return pct is not None and pct > threshold


def _is_down(snapshot: dict | None, threshold: float = 0.0) -> bool:
    pct = _pct(snapshot)
    return pct is not None and pct < -threshold


def _above(snapshot: dict | None, level: float) -> bool:
    value = _latest(snapshot)
    return value is not None and value > level


def _below(snapshot: dict | None, level: float) -> bool:
    value = _latest(snapshot)
    return value is not None and value < level


def _recommendation(snapshot: dict | None) -> str | None:
    value = snapshot.get("recommendation") if snapshot else None
    return str(value).upper() if value else None


def _score_core_signals(
    market_data: dict | None,
    fred_data: dict | None,
    tv_data: dict | None,
) -> tuple[dict[str, float], list[str]]:
    market_data = market_data or {}
    fred_data = fred_data or {}
    tv_data = tv_data or {}

    scores = {"Bull": 0, "Bear": 0}
    reasons: list[str] = []

    vix = market_data.get("VIX")
    vvix = market_data.get("VVIX")
    sp500 = market_data.get("SP500")
    nasdaq = market_data.get("NASDAQ")
    dollar_index = market_data.get("DOLLAR_INDEX")
    us10y = fred_data.get("US10Y")
    spx_tv = tv_data.get("SPX")
    vix_tv = tv_data.get("VIX")

    if _below(vix, 18.0):
        scores["Bull"] += 1
        reasons.append("VIX is below 18")
    if _above(vix, 25.0):
        scores["Bear"] += 2
        reasons.append("VIX is above 25")
    vix_pct = _pct(vix)
    if vix_pct is not None and vix_pct >= 15.0:
        scores["Bear"] += 1
        reasons.append("VIX day-over-day change is above +15%")
    if _above(vvix, 100.0):
        scores["Bear"] += 1
        reasons.append("VVIX is above 100")

    if _is_up(sp500):
        scores["Bull"] += 1
        reasons.append("S&P500 is rising")
    if _is_down(sp500):
        scores["Bear"] += 1
        reasons.append("S&P500 is falling")
    if _is_up(nasdaq):
        scores["Bull"] += 1
        reasons.append("Nasdaq is rising")
    if _is_down(nasdaq):
        scores["Bear"] += 1
        reasons.append("Nasdaq is falling")

    if _diff(us10y) is not None and _diff(us10y) > 0:
        scores["Bear"] += 1
        reasons.append("US10Y is rising")
    if _is_up(dollar_index, threshold=0.3):
        scores["Bear"] += 1
        reasons.append("Dollar Index is rising sharply")

    spx_recommendation = _recommendation(spx_tv)
    if spx_recommendation in BULLISH_RECOMMENDATIONS:
        scores["Bull"] += 1
        reasons.append("SPX technical recommendation is positive")
    elif spx_recommendation in BEARISH_RECOMMENDATIONS:
        scores["Bear"] += 1
        reasons.append("SPX technical recommendation is negative")

    vix_recommendation = _recommendation(vix_tv)
    if vix_recommendation in BULLISH_RECOMMENDATIONS:
        scores["Bear"] += 1
        reasons.append("VIX technical recommendation is elevated")

    return scores, reasons


def _snapshot_get(snapshot, key, default=None):
    if isinstance(snapshot, dict):
        return snapshot.get(key, default)
    return getattr(snapshot, key, default)


def _score_chart_signals(chart_data, tv_data: dict | None) -> tuple[dict[str, float], list[str]]:
    scores = {"Bull": 0.0, "Bear": 0.0}
    reasons: list[str] = []
    tv_data = tv_data or {}
    mixed = False

    for snapshot in chart_data or []:
        if _snapshot_get(snapshot, "freshness") == "Stale":
            continue
        trend = _snapshot_get(snapshot, "trend")
        symbol = _snapshot_get(snapshot, "symbol")
        weight = float(_snapshot_get(snapshot, "bias_weight", 1.0) or 0.0)
        if weight <= 0:
            continue
        if trend == "Bull":
            scores["Bull"] += weight
        elif trend == "Bear":
            scores["Bear"] += weight
        if trend in {"Bull", "Bear"}:
            reasons.append(f"Chart {symbol} 1h trend is {trend} (weight {weight:.2f})")

        macd_state = _snapshot_get(snapshot, "macd_state")
        macd_weight = 0.5 * weight
        if macd_state == "Bullish":
            scores["Bull"] += macd_weight
            reasons.append(f"Chart {symbol} 1h MACD is Bullish (weight {macd_weight:.2f})")
        elif macd_state == "Bearish":
            scores["Bear"] += macd_weight
            reasons.append(f"Chart {symbol} 1h MACD is Bearish (weight {macd_weight:.2f})")

        tv_recommendation = _recommendation(tv_data.get(symbol))
        if (
            trend == "Bull" and tv_recommendation in BEARISH_RECOMMENDATIONS
        ) or (
            trend == "Bear" and tv_recommendation in BULLISH_RECOMMENDATIONS
        ):
            mixed = True

    if mixed:
        reasons.append("Chart/TV mixed")
    return scores, reasons


def _macd_adjustment(macd_signal: str | None) -> tuple[dict[str, float], list[str]]:
    if not macd_signal:
        return {"Bull": 0, "Bear": 0}, []

    normalized = macd_signal.strip().lower()
    if normalized == "bullish":
        return {"Bull": 1, "Bear": 0}, ["M15 MACD bullish cross is a near-term support signal"]
    if normalized == "bearish":
        return {"Bull": 0, "Bear": 1}, ["M15 MACD bearish cross is a near-term caution signal"]
    return {"Bull": 0, "Bear": 0}, []


def _choose_bias(scores: dict[str, float], evidence_count: int) -> tuple[MarketBiasLabel, float]:
    if evidence_count < 2:
        return "Neutral", 0.0

    diff = scores["Bull"] - scores["Bear"]
    if abs(diff) <= 1:
        return "Neutral", min(1.0, round(evidence_count / 8, 2))

    label: MarketBiasLabel = "Bull" if diff > 0 else "Bear"
    confidence = min(1.0, round(abs(diff) / max(scores["Bull"] + scores["Bear"], 1), 2))
    return label, confidence


def _build_warnings(core_scores: dict[str, float], macd_signal: str | None) -> list[str]:
    if not macd_signal:
        return []

    normalized = macd_signal.strip().lower()
    core_diff = core_scores["Bull"] - core_scores["Bear"]
    if normalized == "bullish" and core_diff <= -2:
        return ["M15 MACD bullish cross conflicts with broader Bear conditions"]
    if normalized == "bearish" and core_diff >= 2:
        return ["M15 MACD bearish cross conflicts with broader Bull conditions"]
    return []


def _score_risk_signals(
    market_data: dict | None,
    fred_data: dict | None,
) -> tuple[dict[str, float], list[str]]:
    """Score Risk-On vs Risk-Off. Independent of the Trend (Bull/Bear) axis.

    This is a market-environment filter, not a directional call: equities
    can be risk-off while a currency pair's own trend is still Bull, and
    vice versa.
    """
    market_data = market_data or {}
    fred_data = fred_data or {}

    scores = {"Risk-On": 0.0, "Risk-Off": 0.0}
    reasons: list[str] = []

    vix = market_data.get("VIX")
    sp500 = market_data.get("SP500")
    nasdaq = market_data.get("NASDAQ")
    gold = market_data.get("GOLD")
    oil = market_data.get("OIL")

    if _above(vix, RISK_OFF_VIX_LEVEL):
        scores["Risk-Off"] += 1
        reasons.append(f"VIX is above {RISK_OFF_VIX_LEVEL:g} (risk-off)")
    if _below(vix, RISK_ON_VIX_LEVEL):
        scores["Risk-On"] += 1
        reasons.append(f"VIX is below {RISK_ON_VIX_LEVEL:g} (risk-on)")

    if _is_down(sp500):
        scores["Risk-Off"] += 1
        reasons.append("S&P500 is falling (risk-off)")
    if _is_up(sp500):
        scores["Risk-On"] += 1
        reasons.append("S&P500 is rising (risk-on)")

    if _is_down(nasdaq):
        scores["Risk-Off"] += 1
        reasons.append("Nasdaq is falling (risk-off)")
    if _is_up(nasdaq):
        scores["Risk-On"] += 1
        reasons.append("Nasdaq is rising (risk-on)")

    if _is_up(gold, threshold=RISK_GOLD_PCT_THRESHOLD):
        scores["Risk-Off"] += 1
        reasons.append("Gold is rising (safe-haven demand, risk-off)")
    if _is_down(gold, threshold=RISK_GOLD_PCT_THRESHOLD):
        scores["Risk-On"] += 1
        reasons.append("Gold is soft (risk-on)")

    if _is_up(oil, threshold=RISK_OIL_PCT_THRESHOLD):
        scores["Risk-Off"] += 1
        reasons.append("Oil is spiking (risk-off / geopolitical concern)")

    return scores, reasons


def _choose_risk(scores: dict[str, float], evidence_count: int) -> tuple[RiskLabel, float]:
    if evidence_count == 0:
        return "Neutral", 0.0

    diff = scores["Risk-On"] - scores["Risk-Off"]
    total = scores["Risk-On"] + scores["Risk-Off"]
    if diff == 0:
        return "Neutral", min(1.0, round(evidence_count / 6, 2))

    label: RiskLabel = "Risk-On" if diff > 0 else "Risk-Off"
    confidence = min(1.0, round(abs(diff) / max(total, 1), 2))
    return label, confidence


def _score_volatility_signals(
    market_data: dict | None,
    chart_data,
) -> tuple[dict[str, float], list[str]]:
    """Score Low / Normal / High volatility. Minimal implementation.

    Uses chart Bollinger Band width (a stand-in for ATR/range expansion)
    plus raw day-over-day percent moves as a proxy for "sudden change".
    """
    market_data = market_data or {}
    scores = {"High": 0.0, "Normal": 0.0, "Low": 0.0}
    reasons: list[str] = []

    for snapshot in chart_data or []:
        if _snapshot_get(snapshot, "freshness") == "Stale":
            continue
        symbol = _snapshot_get(snapshot, "symbol")
        bb_width_pct = _snapshot_get(snapshot, "bb_width_pct")
        if bb_width_pct is None:
            continue
        bb_width_pct = float(bb_width_pct)
        if bb_width_pct >= VOLATILITY_HIGH_BB_WIDTH_PCT:
            scores["High"] += 1
            reasons.append(f"Chart {symbol} 1h Bollinger Band width is {bb_width_pct:.2f}% (expanded)")
        elif bb_width_pct <= VOLATILITY_LOW_BB_WIDTH_PCT:
            scores["Low"] += 1
            reasons.append(f"Chart {symbol} 1h Bollinger Band width is {bb_width_pct:.2f}% (contracted)")
        else:
            scores["Normal"] += 1
            reasons.append(f"Chart {symbol} 1h Bollinger Band width is {bb_width_pct:.2f}% (normal range)")

    for name, key in (("S&P500", "SP500"), ("Nasdaq", "NASDAQ"), ("Gold", "GOLD"), ("Oil", "OIL")):
        pct = _pct(market_data.get(key))
        if pct is not None and abs(pct) >= VOLATILITY_RAPID_CHANGE_PCT:
            scores["High"] += 1
            reasons.append(f"{name} moved {pct:+.2f}% (rapid change)")

    return scores, reasons


def _choose_volatility(scores: dict[str, float], evidence_count: int) -> tuple[VolatilityLabel, float]:
    if evidence_count == 0:
        return "Normal", 0.0

    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top_label, top_score = ordered[0]
    runner_up_score = ordered[1][1] if len(ordered) > 1 else 0.0
    total = sum(scores.values())

    if top_score == 0 or top_score == runner_up_score:
        return "Normal", min(1.0, round(evidence_count / 6, 2))

    label: VolatilityLabel = cast(VolatilityLabel, top_label)
    confidence = min(1.0, round(top_score / max(total, 1), 2))
    return label, confidence


def classify_market_bias(
    market_data: dict | None = None,
    fred_data: dict | None = None,
    tv_data: dict | None = None,
    *,
    macd_signal: str | None = None,
    chart_data=None,
) -> MarketBiasResult:
    """Classify market bias along three independent axes.

    - Trend (Bull/Bear/Neutral): the original directional classification,
      unchanged from before this axis was named explicitly.
    - Risk (Risk-On/Risk-Off/Neutral): a risk-appetite / market-environment
      filter, scored independently of Trend.
    - Volatility (Low/Normal/High): a minimal range/expansion classifier.

    This is a market-environment filter, not a trading decision. MACD is
    treated as a near-term auxiliary Trend signal only: it can slightly
    adjust the Trend score and add warnings, but it cannot dominate when
    broader data is missing or mixed, and it has no effect on Risk or
    Volatility.
    """
    core_scores, reasons = _score_core_signals(market_data, fred_data, tv_data)
    chart_scores, chart_reasons = _score_chart_signals(chart_data, tv_data)
    macd_scores, macd_reasons = _macd_adjustment(macd_signal)
    scores = {
        "Bull": core_scores["Bull"] + chart_scores["Bull"] + macd_scores["Bull"],
        "Bear": core_scores["Bear"] + chart_scores["Bear"] + macd_scores["Bear"],
    }
    reasons = [*reasons, *chart_reasons]
    evidence_count = len(reasons)
    bias, confidence = _choose_bias(scores, evidence_count)
    warnings = _build_warnings(core_scores, macd_signal)

    if macd_reasons and evidence_count >= 2:
        reasons = [*reasons, *macd_reasons]
    if not reasons:
        reasons = ["Insufficient market snapshot data for directional bias."]
    elif bias == "Neutral" and abs(scores["Bull"] - scores["Bear"]) <= 1:
        reasons.append("Bull and Bear scores are close, so the bias is Neutral.")

    risk_scores, risk_reasons = _score_risk_signals(market_data, fred_data)
    risk_evidence_count = len(risk_reasons)
    risk, risk_confidence = _choose_risk(risk_scores, risk_evidence_count)
    if not risk_reasons:
        risk_reasons = ["Insufficient market snapshot data for risk classification."]
    elif risk == "Neutral":
        risk_reasons.append("Risk-On and Risk-Off signals are balanced, so risk is Neutral.")

    volatility_scores, volatility_reasons = _score_volatility_signals(market_data, chart_data)
    volatility_evidence_count = len(volatility_reasons)
    volatility, volatility_confidence = _choose_volatility(volatility_scores, volatility_evidence_count)
    if not volatility_reasons:
        volatility_reasons = [
            "Insufficient chart/market data for volatility classification; defaulting to Normal."
        ]

    return {
        "market_bias": bias,
        "confidence": max(0.0, min(1.0, confidence)),
        "scores": scores,
        "reasons": reasons,
        "warnings": warnings,
        "note": NOTE,
        "trend": bias,
        "risk": risk,
        "risk_confidence": max(0.0, min(1.0, risk_confidence)),
        "risk_scores": risk_scores,
        "risk_reasons": risk_reasons,
        "volatility": volatility,
        "volatility_confidence": max(0.0, min(1.0, volatility_confidence)),
        "volatility_scores": volatility_scores,
        "volatility_reasons": volatility_reasons,
    }


def _snapshot_symbol_matches(snapshot, symbol: str) -> bool:
    snap_symbol = _snapshot_get(snapshot, "symbol")
    return snap_symbol is not None and normalize_bias_symbol(snap_symbol) == normalize_bias_symbol(symbol)


def classify_market_bias_for_symbol(
    symbol: str,
    market_data: dict | None = None,
    fred_data: dict | None = None,
    tv_data: dict | None = None,
    *,
    macd_signal: str | None = None,
    chart_data=None,
) -> MarketBiasResult:
    """Classify a single-instrument Market Bias for ``symbol``.

    Issue #40: a per-symbol bias must reuse shared market-environment inputs
    (the cross-asset core signals -- VIX, S&P500, Nasdaq, ... -- plus the Risk
    and Volatility axes) but must NOT let another instrument's chart trend/MACD
    leak into this instrument's directional score. So ``chart_data`` is filtered
    down to only the snapshots whose symbol matches ``symbol`` (normalized the
    same way on both sides) before delegating to :func:`classify_market_bias`.

    ``macd_signal`` (the M15 near-term auxiliary Trend nudge) is per-caller and
    not attributed to any particular instrument here; callers building a
    per-symbol bias should leave it None to avoid mixing it across symbols.

    This is still observation-only classification: it never places an order,
    sizes a position, or emits a trade recommendation.
    """
    own_chart = [snapshot for snapshot in (chart_data or []) if _snapshot_symbol_matches(snapshot, symbol)]
    return classify_market_bias(
        market_data,
        fred_data,
        tv_data,
        macd_signal=macd_signal,
        chart_data=own_chart,
    )


def format_market_bias_section(result: MarketBiasResult) -> str:
    """Render a compact Discord Markdown section for Market Snapshot."""
    lines = ["## Market Bias", ""]
    lines.append(f"Trend: {result['market_bias']}")
    lines.append(f"Risk: {result.get('risk', 'Neutral')}")
    lines.append(f"Volatility: {result.get('volatility', 'Normal')}")
    lines.append(
        "Confidence: "
        f"Trend {round(result['confidence'] * 100)}% / "
        f"Risk {round(result.get('risk_confidence', 0.0) * 100)}% / "
        f"Volatility {round(result.get('volatility_confidence', 0.0) * 100)}%"
    )
    lines.append(f"Scores: Bull {result['scores'].get('Bull', 0)} / Bear {result['scores'].get('Bear', 0)}")
    lines.append("")
    lines.append("Reasons [Trend]:")
    lines.extend(f"- {reason}" for reason in result["reasons"][:5])
    lines.append("")
    lines.append("Reasons [Risk]:")
    lines.extend(f"- {reason}" for reason in result.get("risk_reasons", [])[:5])
    lines.append("")
    lines.append("Reasons [Volatility]:")
    lines.extend(f"- {reason}" for reason in result.get("volatility_reasons", [])[:5])

    if result["warnings"]:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in result["warnings"][:3])

    lines.append("")
    lines.append("Note:")
    lines.append(result["note"])
    return "\n".join(lines)


if __name__ == "__main__":
    import json

    print(json.dumps(classify_market_bias(), ensure_ascii=False, indent=2))
