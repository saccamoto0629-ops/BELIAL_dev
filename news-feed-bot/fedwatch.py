"""Fetch FedWatch-style rate probability data.

The parser accepts a simple JSON shape first, which keeps unit tests stable.
If the public page changes or the request fails, the provider returns None so
the rest of the bot can continue.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any, TypedDict

import requests

logger = logging.getLogger(__name__)

CME_FEDWATCH_URL = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"
INVESTING_FED_RATE_MONITOR_URL = "https://www.investing.com/central-banks/fed-rate-monitor"
REQUEST_TIMEOUT_SECONDS = 3


class FedWatchSnapshot(TypedDict):
    next_meeting: str
    hold_probability: float | None
    cut_probability: float | None
    hike_probability: float | None
    updated_at: str


def _status_label(response: Any) -> str:
    status_code = getattr(response, "status_code", None)
    if status_code is None:
        return "http_error"
    try:
        return f"http_{int(status_code)}_{HTTPStatus(int(status_code)).phrase.lower().replace(' ', '_')}"
    except ValueError:
        return f"http_{status_code}"


def _log_optional_failure(reason: str) -> None:
    logger.info("[fedwatch] optional provider unavailable: %s", reason)


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        value = value.strip().rstrip("%")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_present(source: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in source and source[key] not in (None, ""):
            return source[key]
    return None


def normalize_fedwatch_payload(payload: Any) -> FedWatchSnapshot | None:
    """Normalize common FedWatch JSON-like shapes into the bot's stable shape."""
    if isinstance(payload, list):
        payload = next((item for item in payload if isinstance(item, dict)), None)
    if not isinstance(payload, dict):
        return None

    next_meeting = _first_present(
        payload,
        ("next_meeting", "nextMeeting", "meeting_date", "meetingDate", "date"),
    )
    hold_probability = _as_float(
        _first_present(payload, ("hold_probability", "holdProbability", "hold", "unchanged"))
    )
    cut_probability = _as_float(
        _first_present(payload, ("cut_probability", "cutProbability", "cut", "ease"))
    )
    hike_probability = _as_float(
        _first_present(payload, ("hike_probability", "hikeProbability", "hike", "raise"))
    )
    updated_at = _first_present(
        payload,
        ("updated_at", "updatedAt", "update_time", "asOf", "timestamp"),
    )

    if next_meeting is None and all(
        value is None
        for value in (hold_probability, cut_probability, hike_probability)
    ):
        return None

    return {
        "next_meeting": str(next_meeting or "unknown"),
        "hold_probability": hold_probability,
        "cut_probability": cut_probability,
        "hike_probability": hike_probability,
        "updated_at": str(updated_at or datetime.now(timezone.utc).isoformat()),
    }


def _extract_json_like_value(text: str, key: str) -> str | None:
    match = re.search(
        rf'"{re.escape(key)}"\s*:\s*"?([^",}}]+)"?',
        text,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else None


def parse_fedwatch_html(text: str) -> FedWatchSnapshot | None:
    """Best-effort fallback for simple embedded JSON values in an HTML page."""
    payload = {
        "next_meeting": _extract_json_like_value(text, "next_meeting")
        or _extract_json_like_value(text, "meetingDate"),
        "hold_probability": _extract_json_like_value(text, "hold_probability")
        or _extract_json_like_value(text, "holdProbability"),
        "cut_probability": _extract_json_like_value(text, "cut_probability")
        or _extract_json_like_value(text, "cutProbability"),
        "hike_probability": _extract_json_like_value(text, "hike_probability")
        or _extract_json_like_value(text, "hikeProbability"),
        "updated_at": _extract_json_like_value(text, "updated_at")
        or _extract_json_like_value(text, "updatedAt"),
    }
    return normalize_fedwatch_payload(payload)


def get_fedwatch(
    *,
    session: Any = requests,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
    url: str = CME_FEDWATCH_URL,
) -> FedWatchSnapshot | None:
    """Return FedWatch probabilities, or None if unavailable."""
    urls = [url]
    if url == CME_FEDWATCH_URL:
        urls.append(INVESTING_FED_RATE_MONITOR_URL)

    for selected_url in urls:
        try:
            response = session.get(selected_url, timeout=timeout)
            try:
                response.raise_for_status()
            except Exception:
                _log_optional_failure(_status_label(response))
                continue

            try:
                snapshot = normalize_fedwatch_payload(response.json())
            except ValueError:
                snapshot = parse_fedwatch_html(response.text)

            if snapshot is None:
                _log_optional_failure("no_probability_data")
                continue
            return snapshot
        except requests.Timeout:
            _log_optional_failure("timeout")
        except requests.RequestException:
            _log_optional_failure("request_error")
        except Exception:
            logger.debug("[fedwatch] optional provider parse failure", exc_info=True)
            _log_optional_failure("parse_error")

    return None


if __name__ == "__main__":
    import json

    print(json.dumps(get_fedwatch(), ensure_ascii=False, indent=2))
