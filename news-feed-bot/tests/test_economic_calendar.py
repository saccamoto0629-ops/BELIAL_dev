import datetime as dt
import logging
import pathlib
import sys

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import economic_calendar as calendar  # noqa: E402


class FakeResponse:
    def __init__(self, payload=None, status_code=200, status_error=None):
        self.payload = payload
        self.status_code = status_code
        self.status_error = status_error

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, *, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        return FakeResponse(self.payload)


class FailingSession:
    def post(self, *args, **kwargs):
        raise RuntimeError("calendar down")


def test_get_economic_calendar_normalizes_events():
    session = FakeSession(
        [
            {
                "Date": "2026-07-04T12:30:00",
                "Country": "United States",
                "Event": "CPI",
                "Importance": "3",
                "Forecast": "3.1%",
                "Previous": "3.0%",
                "Actual": "",
            }
        ]
    )

    events = calendar.get_economic_calendar(
        session=session,
        today=dt.date(2026, 7, 4),
        timeout=7,
        client="guest:guest",
    )

    assert events == [
        {
            "datetime": "2026-07-04T12:30:00",
            "country": "United States",
            "event": "CPI",
            "importance": "3",
            "forecast": "3.1%",
            "previous": "3.0%",
            "actual": None,
        }
    ]
    assert session.calls[0]["data"]["dateFrom"] == "2026-07-04"
    assert session.calls[0]["data"]["dateTo"] == "2026-07-04"
    assert session.calls[0]["timeout"] == 7


def test_get_economic_calendar_returns_empty_list_on_failure():
    assert calendar.get_economic_calendar(session=FailingSession()) == []


def test_get_economic_calendar_logs_optional_403_without_long_url_or_traceback(caplog):
    session = FakeSession([])
    session.payload = None

    def post(url, *, data=None, headers=None, timeout=None):
        session.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        return FakeResponse(status_code=403, status_error=RuntimeError("403 Forbidden"))

    session.post = post

    with caplog.at_level(logging.INFO):
        assert calendar.get_economic_calendar(session=session, url="https://example.test/calendar") == []

    log_text = caplog.text
    assert "optional provider unavailable: http_403_forbidden" in log_text
    assert "https://example.test/calendar" not in log_text
    assert "Traceback" not in log_text
