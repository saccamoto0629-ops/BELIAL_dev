"""Fetch economic calendar events.

The provider intentionally returns an empty list on any failure so the bot can
continue to publish the rest of the market snapshot.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from http import HTTPStatus
from typing import Any, TypedDict

import requests

logger = logging.getLogger(__name__)

INVESTING_CALENDAR_URL = "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData"
REQUEST_TIMEOUT_SECONDS = 3


class EconomicEvent(TypedDict):
    datetime: str
    country: str
    event: str
    importance: str
    forecast: str | None
    previous: str | None
    actual: str | None


def _status_label(response: Any) -> str:
    status_code = getattr(response, "status_code", None)
    if status_code is None:
        return "http_error"
    try:
        return f"http_{int(status_code)}_{HTTPStatus(int(status_code)).phrase.lower().replace(' ', '_')}"
    except ValueError:
        return f"http_{status_code}"


def _log_optional_failure(reason: str) -> None:
    logger.info("[economic_calendar] optional provider unavailable: %s", reason)


def _as_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", text).strip()


def normalize_event(raw_event: dict[str, Any]) -> EconomicEvent:
    """Normalize a TradingEconomics calendar row to the bot's stable shape."""
    return {
        "datetime": _as_text(
            raw_event.get("Date")
            or raw_event.get("datetime")
            or raw_event.get("date")
            or raw_event.get("timestamp")
        ) or "",
        "country": _as_text(raw_event.get("Country") or raw_event.get("country")) or "",
        "event": _as_text(raw_event.get("Event") or raw_event.get("event") or raw_event.get("name")) or "",
        "importance": _as_text(raw_event.get("Importance") or raw_event.get("importance") or raw_event.get("impact")) or "",
        "forecast": _as_text(raw_event.get("Forecast") or raw_event.get("forecast")),
        "previous": _as_text(raw_event.get("Previous") or raw_event.get("previous")),
        "actual": _as_text(raw_event.get("Actual") or raw_event.get("actual")),
    }


def parse_investing_calendar_html(html: str) -> list[EconomicEvent]:
    """Best-effort parser for Investing.com calendar row HTML."""
    events: list[EconomicEvent] = []
    for row in re.findall(r"<tr\b[^>]*?event_timestamp=\"([^\"]+)\"[^>]*>(.*?)</tr>", html, re.I | re.S):
        timestamp, row_html = row
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row_html, re.I | re.S)
        clean_cells = [_strip_html(cell) for cell in cells]
        event_name = next((cell for cell in clean_cells if cell and cell.lower() not in {"actual", "forecast", "previous"}), "")
        country_match = re.search(r"title=\"([^\"]+)\"", row_html)
        importance = str(len(re.findall(r"grayFullBullishIcon|bull\d", row_html, re.I)) or "")
        if event_name:
            events.append(
                {
                    "datetime": timestamp,
                    "country": country_match.group(1) if country_match else "",
                    "event": event_name,
                    "importance": importance,
                    "forecast": None,
                    "previous": None,
                    "actual": None,
                }
            )
    return events


def extract_events(payload: Any) -> list[EconomicEvent]:
    if isinstance(payload, list):
        return [normalize_event(event) for event in payload if isinstance(event, dict)]
    if isinstance(payload, dict):
        for key in ("events", "calendar", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return [normalize_event(event) for event in value if isinstance(event, dict)]
        data = payload.get("data")
        if isinstance(data, list):
            return [normalize_event(event) for event in data if isinstance(event, dict)]
        if isinstance(data, str):
            return parse_investing_calendar_html(data)
    return []


def get_economic_calendar(
    *,
    session: Any = requests,
    today: date | None = None,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
    url: str = INVESTING_CALENDAR_URL,
    client: str | None = None,
) -> list[EconomicEvent]:
    """Return today's economic calendar events, or [] if unavailable."""
    target_date = today or date.today()
    data = {
        "dateFrom": target_date.strftime("%Y-%m-%d"),
        "dateTo": target_date.strftime("%Y-%m-%d"),
        "timeZone": "8",
        "timeFilter": "timeRemain",
        "currentTab": "today",
        "limit_from": "0",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.investing.com/economic-calendar/",
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        response = session.post(
            url,
            data=data,
            headers=headers,
            timeout=timeout,
        )
        try:
            response.raise_for_status()
        except Exception:
            _log_optional_failure(_status_label(response))
            return []
        payload = response.json()
        return extract_events(payload)
    except requests.Timeout:
        _log_optional_failure("timeout")
    except requests.RequestException:
        _log_optional_failure("request_error")
    except Exception:
        logger.debug("[economic_calendar] optional provider parse failure", exc_info=True)
        _log_optional_failure("parse_error")
    return []


if __name__ == "__main__":
    import json

    print(json.dumps(get_economic_calendar(), ensure_ascii=False, indent=2))
