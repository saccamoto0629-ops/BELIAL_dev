"""Rule-based Bull / Bear / Neutral market-bias classification.

The classifier consumes data that Market Snapshot already fetched. It does not
call external APIs and it never makes trading recommendations.
"""

from typing import Literal, TypedDict

MarketBiasLabel = Literal["Bull", "Bear", "Neutral"]

NOTE = "Observation only. No trade recommendation."
BULLISH_RECOMMENDATIONS = {"BUY", "STRONG_BUY"}
BEARISH_RECOMMENDATIONS = {"SELL", "STRONG_SELL"}


class MarketBiasResult(TypedDict):
    market_bias: MarketBiasLabel
    confidence: float
    scores: dict[str, int]
    reasons: list[str]
    warnings: list[str]
    note: str


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
) -> tuple[dict[str, int], list[str]]:
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


def _macd_adjustment(macd_signal: str | None) -> tuple[dict[str, int], list[str]]:
    if not macd_signal:
        return {"Bull": 0, "Bear": 0}, []

    normalized = macd_signal.strip().lower()
    if normalized == "bullish":
        return {"Bull": 1, "Bear": 0}, ["M15 MACD bullish cross is a near-term support signal"]
    if normalized == "bearish":
        return {"Bull": 0, "Bear": 1}, ["M15 MACD bearish cross is a near-term caution signal"]
    return {"Bull": 0, "Bear": 0}, []


def _choose_bias(scores: dict[str, int], evidence_count: int) -> tuple[MarketBiasLabel, float]:
    if evidence_count < 2:
        return "Neutral", 0.0

    diff = scores["Bull"] - scores["Bear"]
    if abs(diff) <= 1:
        return "Neutral", min(1.0, round(evidence_count / 8, 2))

    label: MarketBiasLabel = "Bull" if diff > 0 else "Bear"
    confidence = min(1.0, round(abs(diff) / max(scores["Bull"] + scores["Bear"], 1), 2))
    return label, confidence


def _build_warnings(core_scores: dict[str, int], macd_signal: str | None) -> list[str]:
    if not macd_signal:
        return []

    normalized = macd_signal.strip().lower()
    core_diff = core_scores["Bull"] - core_scores["Bear"]
    if normalized == "bullish" and core_diff <= -2:
        return ["M15 MACD bullish cross conflicts with broader Bear conditions"]
    if normalized == "bearish" and core_diff >= 2:
        return ["M15 MACD bearish cross conflicts with broader Bull conditions"]
    return []


def classify_market_bias(
    market_data: dict | None = None,
    fred_data: dict | None = None,
    tv_data: dict | None = None,
    *,
    macd_signal: str | None = None,
) -> MarketBiasResult:
    """Classify market bias as Bull, Bear, or Neutral.

    MACD is treated as a near-term auxiliary signal. It can slightly adjust
    the score and add warnings, but it cannot dominate when broader data is
    missing or mixed.
    """
    core_scores, reasons = _score_core_signals(market_data, fred_data, tv_data)
    macd_scores, macd_reasons = _macd_adjustment(macd_signal)
    scores = {
        "Bull": core_scores["Bull"] + macd_scores["Bull"],
        "Bear": core_scores["Bear"] + macd_scores["Bear"],
    }
    evidence_count = len(reasons)
    bias, confidence = _choose_bias(scores, evidence_count)
    warnings = _build_warnings(core_scores, macd_signal)

    if macd_reasons and evidence_count >= 2:
        reasons = [*reasons, *macd_reasons]
    if not reasons:
        reasons = ["Insufficient market snapshot data for directional bias."]
    elif bias == "Neutral" and abs(scores["Bull"] - scores["Bear"]) <= 1:
        reasons.append("Bull and Bear scores are close, so the bias is Neutral.")

    return {
        "market_bias": bias,
        "confidence": max(0.0, min(1.0, confidence)),
        "scores": scores,
        "reasons": reasons,
        "warnings": warnings,
        "note": NOTE,
    }


def format_market_bias_section(result: MarketBiasResult) -> str:
    """Render a compact Discord Markdown section for Market Snapshot."""
    lines = ["## Market Bias", ""]
    lines.append(f"Bias: {result['market_bias']}")
    lines.append(f"Confidence: {round(result['confidence'] * 100)}%")
    lines.append(f"Scores: Bull {result['scores'].get('Bull', 0)} / Bear {result['scores'].get('Bear', 0)}")
    lines.append("")
    lines.append("Reasons:")
    lines.extend(f"- {reason}" for reason in result["reasons"][:5])

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
