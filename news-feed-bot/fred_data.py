"""Fetch interest-rate and macro series from the FRED API."""

import logging
from typing import TypedDict

import requests

logger = logging.getLogger(__name__)

FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
REQUEST_TIMEOUT_SECONDS = 5
OBSERVATION_LIMIT = 10

FRED_SERIES = {
    "US10Y": "DGS10",
    "US2Y": "DGS2",
    "10Y2Y_SPREAD": "T10Y2Y",
    "FED_FUNDS": "DFF",
    "CPI": "CPIAUCSL",
    "PCE": "PCEPI",
    "UNEMPLOYMENT": "UNRATE",
}


class FredSnapshot(TypedDict):
    series_id: str
    date: str
    latest: float
    previous: float | None
    diff: float | None


FredData = dict[str, FredSnapshot | None]


def get_series_snapshot(
    series_id: str,
    api_key: str,
    *,
    limit: int = OBSERVATION_LIMIT,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> FredSnapshot | None:
    """Return latest/previous/diff for one FRED series, or None if unavailable.

    FRED marks holidays/closures with "." instead of omitting the row, so
    those are filtered out and the most recent valid values are used.
    """
    try:
        params: dict[str, str | int] = {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": limit,
        }
        response = requests.get(
            FRED_API_URL,
            params=params,
            timeout=timeout,
        )
        response.raise_for_status()
        observations = response.json().get("observations", [])

        valid_observations = [
            (str(obs.get("date", "")), float(obs["value"]))
            for obs in observations
            if obs.get("value") not in (None, ".", "")
        ]
        if not valid_observations:
            raise ValueError(f"No valid observations for {series_id}")

        latest_date, latest = valid_observations[0]
        previous = valid_observations[1][1] if len(valid_observations) > 1 else None
        diff = (latest - previous) if previous is not None else None

        return {
            "series_id": series_id,
            "date": latest_date,
            "latest": latest,
            "previous": previous,
            "diff": diff,
        }
    except Exception as exc:
        logger.warning("[fred_data] Failed to fetch %s: %s", series_id, exc)
        return None


def get_fred_data(
    api_key: str | None,
    series: dict[str, str] | None = None,
    *,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> FredData | None:
    """Return a dict of label -> snapshot for each configured series.

    Returns None entirely when no API key is configured, so callers can skip
    the whole FRED section without the bot crashing.
    """
    if not api_key:
        logger.warning("[fred_data] FRED_API_KEY is not set; skipping FRED data.")
        return None

    series = series or FRED_SERIES
    return {
        label: get_series_snapshot(series_id, api_key, timeout=timeout)
        for label, series_id in series.items()
    }


if __name__ == "__main__":
    import json
    import os

    print(json.dumps(get_fred_data(os.getenv("FRED_API_KEY", "")), ensure_ascii=False, indent=2))
