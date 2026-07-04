import pathlib
import sys

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import fred_data  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_get_series_snapshot_skips_missing_values_and_computes_diff(monkeypatch):
    payload = {
        "observations": [
            {"date": "2026-07-04", "value": "."},
            {"date": "2026-07-03", "value": "4.31"},
            {"date": "2026-07-02", "value": "4.25"},
        ]
    }
    monkeypatch.setattr(fred_data.requests, "get", lambda *a, **k: FakeResponse(payload))

    snapshot = fred_data.get_series_snapshot("DGS10", "fake-key")

    assert snapshot["latest"] == 4.31
    assert snapshot["date"] == "2026-07-03"
    assert snapshot["previous"] == 4.25
    assert round(snapshot["diff"], 2) == 0.06


def test_get_series_snapshot_returns_none_when_no_valid_values(monkeypatch):
    payload = {"observations": [{"value": "."}, {"value": "."}]}
    monkeypatch.setattr(fred_data.requests, "get", lambda *a, **k: FakeResponse(payload))

    assert fred_data.get_series_snapshot("DGS10", "fake-key") is None


def test_get_series_snapshot_returns_none_on_request_error(monkeypatch):
    def raise_error(*args, **kwargs):
        raise fred_data.requests.RequestException("network down")

    monkeypatch.setattr(fred_data.requests, "get", raise_error)

    assert fred_data.get_series_snapshot("DGS10", "fake-key") is None


def test_get_fred_data_skips_entirely_without_api_key():
    assert fred_data.get_fred_data("") is None
    assert fred_data.get_fred_data(None) is None


def test_get_fred_data_returns_snapshot_per_series(monkeypatch):
    payload = {"observations": [{"date": "2026-07-01", "value": "5.33"}]}
    monkeypatch.setattr(fred_data.requests, "get", lambda *a, **k: FakeResponse(payload))

    data = fred_data.get_fred_data("fake-key", series={"FED_FUNDS": "DFF"})

    assert data["FED_FUNDS"]["latest"] == 5.33
    assert data["FED_FUNDS"]["date"] == "2026-07-01"
    assert data["FED_FUNDS"]["previous"] is None
    assert data["FED_FUNDS"]["diff"] is None


def test_default_fred_series_include_macro_targets():
    assert fred_data.FRED_SERIES["CPI"] == "CPIAUCSL"
    assert fred_data.FRED_SERIES["PCE"] == "PCEPI"
    assert fred_data.FRED_SERIES["UNEMPLOYMENT"] == "UNRATE"
    assert fred_data.FRED_SERIES["FED_FUNDS"] == "DFF"
    assert "FEDFUNDS" not in fred_data.FRED_SERIES.values()
