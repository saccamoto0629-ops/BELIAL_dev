import pathlib
import sys
import logging

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import fedwatch  # noqa: E402


class FakeResponse:
    def __init__(self, payload=None, text="", json_error=None, status_code=200, status_error=None):
        self.payload = payload
        self.text = text
        self.json_error = json_error
        self.status_code = status_code
        self.status_error = status_error

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error
        return None

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, *, timeout=None):
        self.calls.append({"url": url, "timeout": timeout})
        return self.response


class FailingSession:
    def get(self, *args, **kwargs):
        raise RuntimeError("fedwatch down")


def test_get_fedwatch_normalizes_json_payload():
    session = FakeSession(
        FakeResponse(
            {
                "next_meeting": "2026-07-29",
                "hold_probability": "78%",
                "cut_probability": 22,
                "hike_probability": None,
                "updated_at": "2026-07-04T00:00:00Z",
            }
        )
    )

    snapshot = fedwatch.get_fedwatch(session=session, timeout=8, url="https://example.test/fedwatch")

    assert snapshot == {
        "next_meeting": "2026-07-29",
        "hold_probability": 78.0,
        "cut_probability": 22.0,
        "hike_probability": None,
        "updated_at": "2026-07-04T00:00:00Z",
    }
    assert session.calls == [{"url": "https://example.test/fedwatch", "timeout": 8}]


def test_get_fedwatch_can_parse_simple_embedded_html_values():
    session = FakeSession(
        FakeResponse(
            text=(
                '{"next_meeting":"2026-07-29",'
                '"hold_probability":78,'
                '"cut_probability":22,'
                '"hike_probability":0,'
                '"updated_at":"2026-07-04T00:00:00Z"}'
            ),
            json_error=ValueError("not json"),
        )
    )

    snapshot = fedwatch.get_fedwatch(session=session)

    assert snapshot is not None
    assert snapshot["next_meeting"] == "2026-07-29"
    assert snapshot["hold_probability"] == 78.0
    assert snapshot["cut_probability"] == 22.0
    assert snapshot["hike_probability"] == 0.0


def test_get_fedwatch_returns_none_on_failure():
    assert fedwatch.get_fedwatch(session=FailingSession()) is None


def test_get_fedwatch_logs_optional_403_without_long_url_or_traceback(caplog):
    session = FakeSession(
        FakeResponse(status_code=403, status_error=RuntimeError("403 Forbidden"))
    )

    with caplog.at_level(logging.INFO):
        assert fedwatch.get_fedwatch(session=session, url="https://example.test/fedwatch") is None

    log_text = caplog.text
    assert "optional provider unavailable: http_403_forbidden" in log_text
    assert "https://example.test/fedwatch" not in log_text
    assert "Traceback" not in log_text
