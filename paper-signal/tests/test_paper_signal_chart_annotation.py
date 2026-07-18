"""Issue #65: Paper Signal notifications carry a DERIVED annotated chart image.

The annotated image is regenerated from the stored OHLCV through the existing
chart renderer (paper_signal event context) and uploaded to its own derived S3
key; failure at any step falls back to the Issue #55 original snapshot URL and
ultimately to the text-only notification. Duplicates and --dry-run perform no
image side effects, and the candle window is strictly the signal's own
symbol/timeframe, ending exactly at the target candle.
"""

import logging
import pathlib
import sys
from datetime import UTC, datetime

import pandas as pd
import pytest
import requests

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import paper_signal_store as store  # noqa: E402
import signal_generator as sg  # noqa: E402  (bootstraps market-watch on sys.path)
import chart_collector  # noqa: E402  (market-watch)
from chart_models import Candle  # noqa: E402  (market-watch)

EVAL_TIME = datetime(2026, 7, 5, 18, 0, 0, tzinfo=UTC)
CANDLE_TS = "2026-07-05T12:00:00Z"
SNAPSHOT_S3_KEY = "charts/USDJPY/1h/snap-1.png"
ORIGINAL_URL = f"https://s3.test/{SNAPSHOT_S3_KEY}"


def make_chart_snapshot(**overrides):
    snapshot = {
        "symbol": "USDJPY",
        "timeframe": "1h",
        "timestamp": CANDLE_TS,
        "trend": "Bull",
        "state": "Inside band",
        "freshness": "Fresh",
        "bias_weight": 1.0,
        "macd_state": "Bullish",
    }
    snapshot.update(overrides)
    return snapshot


def make_bias(market_bias="Bull", confidence=0.8):
    return {"market_bias": market_bias, "confidence": confidence, "scores": {}, "reasons": [], "warnings": [], "note": ""}


def seed_stored_snapshot(chart_db, *, snapshot_id="snap-1", candle_time=CANDLE_TS):
    chart_collector.init_db(chart_db)
    return chart_collector.insert_chart_snapshot(
        chart_db,
        symbol="USDJPY",
        timeframe="1h",
        snapshot_at="2026-07-05T12:05:00Z",
        s3_bucket="belial-chart-snapshots",
        s3_key=SNAPSHOT_S3_KEY,
        captured_at="2026-07-05T12:05:00Z",
        candle_time=candle_time,
        snapshot_id=snapshot_id,
    )


def seed_candles(chart_db, *, symbol="USDJPY", timeframe="1h", end=CANDLE_TS, count=30, base=150.0):
    """Store *count* hourly candles ending exactly at *end* via the real writer."""
    chart_collector.init_db(chart_db)
    end_ts = pd.Timestamp(end)
    candles = []
    for offset in range(count):
        ts = end_ts - pd.Timedelta(hours=count - 1 - offset)
        close = base + offset * 0.1
        candles.append(
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                timestamp=chart_collector.to_utc_iso_z(ts),
                open=close - 0.05,
                high=close + 0.2,
                low=close - 0.2,
                close=close,
                volume=100.0,
            )
        )
    chart_collector.save_candles(chart_db, candles)
    return candles


@pytest.fixture
def bull_cycle(monkeypatch, tmp_path):
    """One Bull cycle wired to a tmp Chart DB with the renderer and S3 leaves faked."""
    chart_db = tmp_path / "chart_data.sqlite3"
    monkeypatch.setenv("CHART_DB_PATH", str(chart_db))
    monkeypatch.setattr(
        sg,
        "fetch_snapshot_sources",
        lambda **kw: {"market": {}, "fred": None, "tradingview": {}, "chart": [make_chart_snapshot()]},
    )
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias())
    monkeypatch.setattr(sg, "DISCORD_WEBHOOK_URL", "https://discord.test/webhook")
    monkeypatch.setattr(
        sg, "generate_presigned_chart_url", lambda s3_key, config=None, **kw: f"https://s3.test/{s3_key}"
    )
    return {"chart_db": chart_db, "paper_db": tmp_path / "paper_signals.sqlite3"}


@pytest.fixture
def fake_render(monkeypatch):
    """Record render calls and write a placeholder PNG (no matplotlib work)."""
    calls = []

    def render(candles, output_path, *, title, dry_run=False, event_type=None, metadata=None, source="scheduled"):
        calls.append(
            {
                "candles": candles.copy(),
                "output_path": pathlib.Path(output_path),
                "title": title,
                "event_type": event_type,
                "metadata": metadata,
                "source": source,
            }
        )
        pathlib.Path(output_path).write_bytes(b"\x89PNG fake")
        return pathlib.Path(output_path)

    monkeypatch.setattr(sg, "render_chart_snapshot", render)
    return calls


@pytest.fixture
def fake_upload(monkeypatch):
    calls = []

    def upload(local_path, symbol, timeframe, snapshot_at, *, config=None, s3_client=None, s3_key=None):
        calls.append(
            {
                "local_path": pathlib.Path(local_path),
                "local_bytes": pathlib.Path(local_path).read_bytes(),
                "symbol": symbol,
                "timeframe": timeframe,
                "s3_key": s3_key,
            }
        )
        return s3_key

    monkeypatch.setattr(sg, "upload_chart_image", upload)
    return calls


@pytest.fixture
def posted(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json})
        return FakeResponse()

    monkeypatch.setattr(sg.requests, "post", fake_post)
    return calls


DERIVED_KEY = f"charts/paper-signals/USDJPY/1h/2026/07/05/20260705T120000Z_{{signal_id}}.png"


def expected_derived_key(signal_id):
    return DERIVED_KEY.format(signal_id=signal_id)


# --- derived S3 key: existing layout, idempotent ------------------------------


def test_derived_s3_key_is_deterministic_and_separate_from_snapshots():
    key = sg.build_paper_signal_chart_s3_key("usdjpy", "1h", CANDLE_TS, "sig-1", prefix="charts")

    assert key == "charts/paper-signals/USDJPY/1h/2026/07/05/20260705T120000Z_sig-1.png"
    # Same signal -> same key on every call (idempotent, no unbounded objects).
    assert key == sg.build_paper_signal_chart_s3_key("USDJPY", "1h", "2026-07-05T12:00:00+00:00", "sig-1", prefix="charts")
    assert not key.startswith("charts/USDJPY/")  # never collides with snapshot keys


# --- success: derived image rides the embed -----------------------------------


def test_derived_annotated_image_attached_to_embed(bull_cycle, posted, fake_render, fake_upload):
    seed_stored_snapshot(bull_cycle["chart_db"])
    seed_candles(bull_cycle["chart_db"])

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    signal_id = created[0]["signal_id"]
    assert len(posted) == 1
    payload = posted[0]["json"]
    assert payload["content"] == sg.format_discord_message(created[0])[:2000]
    assert payload["embeds"] == [{"image": {"url": f"https://s3.test/{expected_derived_key(signal_id)}"}}]

    # Rendered through the existing renderer with the paper_signal context.
    assert len(fake_render) == 1
    call = fake_render[0]
    assert call["event_type"] == "paper_signal"
    assert call["source"] == "paper_signal"
    assert call["metadata"] == {"side": "long", "target_candle_utc": CANDLE_TS}
    # The candle window ends exactly at the target candle.
    assert call["candles"].index[-1] == pd.Timestamp(CANDLE_TS)

    # Uploaded once, to the derived key, with the rendered temp file's bytes;
    # the temp PNG is gone afterwards.
    assert len(fake_upload) == 1
    assert fake_upload[0]["s3_key"] == expected_derived_key(signal_id)
    assert fake_upload[0]["local_bytes"].startswith(b"\x89PNG")
    assert not fake_upload[0]["local_path"].exists()

    # The Issue #55 relation to the original snapshot is still persisted.
    import chart_snapshot_store

    links = chart_snapshot_store.list_paper_signal_links(bull_cycle["chart_db"], signal_id)
    assert [link["chart_snapshot_id"] for link in links] == ["snap-1"]


def test_short_signal_renders_short_side_metadata(bull_cycle, posted, fake_render, fake_upload, monkeypatch):
    monkeypatch.setattr(
        sg,
        "fetch_snapshot_sources",
        lambda **kw: {
            "market": {},
            "fred": None,
            "tradingview": {},
            "chart": [make_chart_snapshot(trend="Bear", macd_state="Bearish")],
        },
    )
    monkeypatch.setattr(sg, "classify_market_bias", lambda *a, **k: make_bias(market_bias="Bear"))
    seed_stored_snapshot(bull_cycle["chart_db"])
    seed_candles(bull_cycle["chart_db"])

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert created[0]["side"] == "short"
    assert fake_render[0]["metadata"]["side"] == "short"


# --- candle-window integrity --------------------------------------------------


def test_other_symbol_and_timeframe_candles_are_never_used(bull_cycle, posted, fake_render, fake_upload):
    seed_stored_snapshot(bull_cycle["chart_db"])
    own = seed_candles(bull_cycle["chart_db"], symbol="USDJPY", timeframe="1h", base=150.0)
    seed_candles(bull_cycle["chart_db"], symbol="XAUUSD", timeframe="1h", base=2000.0)
    seed_candles(bull_cycle["chart_db"], symbol="USDJPY", timeframe="4h", base=999.0)

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    frame = fake_render[0]["candles"]
    assert len(frame) == len(own)
    assert list(frame["close"]) == [candle.close for candle in own]  # USDJPY 1h values only


def test_future_candles_are_excluded_and_target_position_stable(bull_cycle, posted, fake_render, fake_upload):
    seed_stored_snapshot(bull_cycle["chart_db"])
    seed_candles(bull_cycle["chart_db"], count=30)
    # A future candle stored after the fact must never enter the window.
    seed_candles(bull_cycle["chart_db"], end="2026-07-05T14:00:00Z", count=2, base=170.0)

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    frame = fake_render[0]["candles"]
    assert frame.index[-1] == pd.Timestamp(CANDLE_TS)
    assert pd.Timestamp("2026-07-05T13:00:00Z") not in frame.index
    assert pd.Timestamp("2026-07-05T14:00:00Z") not in frame.index


def test_missing_target_candle_falls_back_to_original_snapshot_url(bull_cycle, posted, fake_render, fake_upload):
    seed_stored_snapshot(bull_cycle["chart_db"])
    # Candles stop an hour BEFORE the target candle: exact match impossible.
    seed_candles(bull_cycle["chart_db"], end="2026-07-05T11:00:00Z")

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert fake_render == []       # never rendered against the wrong candle
    assert fake_upload == []
    assert posted[0]["json"]["embeds"] == [{"image": {"url": ORIGINAL_URL}}]


def test_no_stored_candles_falls_back_to_original_snapshot_url(bull_cycle, posted, fake_render, fake_upload):
    seed_stored_snapshot(bull_cycle["chart_db"])

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert fake_render == []
    assert posted[0]["json"]["embeds"] == [{"image": {"url": ORIGINAL_URL}}]


# --- failure fallbacks --------------------------------------------------------


def test_renderer_failure_falls_back_to_original_snapshot_url(bull_cycle, posted, fake_upload, monkeypatch):
    seed_stored_snapshot(bull_cycle["chart_db"])
    seed_candles(bull_cycle["chart_db"])

    def failing_render(*args, **kwargs):
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr(sg, "render_chart_snapshot", failing_render)

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert fake_upload == []
    assert len(posted) == 1
    assert posted[0]["json"]["embeds"] == [{"image": {"url": ORIGINAL_URL}}]
    assert len(store.list_signals(db_path=bull_cycle["paper_db"])) == 1


def test_s3_upload_failure_falls_back_to_original_snapshot_url(bull_cycle, posted, fake_render, monkeypatch):
    seed_stored_snapshot(bull_cycle["chart_db"])
    seed_candles(bull_cycle["chart_db"])

    def failing_upload(*args, **kwargs):
        raise RuntimeError("S3 upload failed")

    monkeypatch.setattr(sg, "upload_chart_image", failing_upload)

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert len(posted) == 1
    assert posted[0]["json"]["embeds"] == [{"image": {"url": ORIGINAL_URL}}]


def test_derived_presign_failure_falls_back_to_original_snapshot_url(
    bull_cycle, posted, fake_render, fake_upload, monkeypatch
):
    seed_stored_snapshot(bull_cycle["chart_db"])
    seed_candles(bull_cycle["chart_db"])

    def selective_presign(s3_key, config=None, **kw):
        if "paper-signals" in s3_key:
            raise RuntimeError("presign failed for derived key")
        return f"https://s3.test/{s3_key}"

    monkeypatch.setattr(sg, "generate_presigned_chart_url", selective_presign)

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert posted[0]["json"]["embeds"] == [{"image": {"url": ORIGINAL_URL}}]


def test_every_image_path_failing_still_sends_text_only(bull_cycle, posted, monkeypatch):
    seed_stored_snapshot(bull_cycle["chart_db"])
    seed_candles(bull_cycle["chart_db"])

    monkeypatch.setattr(
        sg, "render_chart_snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("render down"))
    )
    monkeypatch.setattr(
        sg, "generate_presigned_chart_url", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("presign down"))
    )

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert len(posted) == 1
    assert "embeds" not in posted[0]["json"]
    assert len(store.list_signals(db_path=bull_cycle["paper_db"])) == 1


def test_discord_failure_keeps_signal_after_derived_image(bull_cycle, fake_render, fake_upload, monkeypatch):
    seed_stored_snapshot(bull_cycle["chart_db"])
    seed_candles(bull_cycle["chart_db"])

    def failing_post(url, json=None, timeout=None):
        raise requests.RequestException("discord down")

    monkeypatch.setattr(sg.requests, "post", failing_post)

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert len(store.list_signals(db_path=bull_cycle["paper_db"])) == 1


def test_unknown_side_is_never_guessed_into_a_direction(bull_cycle, fake_render, fake_upload):
    seed_stored_snapshot(bull_cycle["chart_db"])
    seed_candles(bull_cycle["chart_db"])

    url = sg.render_and_upload_paper_signal_chart("sig-x", "USDJPY", "1h", CANDLE_TS, "mystery")

    assert url is None
    assert fake_render == []   # renderer never asked to draw a guessed side
    assert fake_upload == []


# --- duplicates ---------------------------------------------------------------


def test_duplicate_signal_does_not_regenerate_derived_image(bull_cycle, posted, fake_render, fake_upload):
    seed_stored_snapshot(bull_cycle["chart_db"])
    seed_candles(bull_cycle["chart_db"])

    first = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)
    second = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(first) == 1
    assert second == []
    assert len(posted) == 1
    assert len(fake_render) == 1   # no re-render for the duplicate
    assert len(fake_upload) == 1   # no re-upload for the duplicate


# --- dry-run: no renderer, no temp files, no S3, no Discord, no DB writes -----


def test_dry_run_never_touches_renderer_temp_s3_or_discord(bull_cycle, monkeypatch, caplog):
    seed_stored_snapshot(bull_cycle["chart_db"])
    seed_candles(bull_cycle["chart_db"])
    mtime_before = bull_cycle["chart_db"].stat().st_mtime_ns

    def forbid(name):
        def _fail(*args, **kwargs):
            raise AssertionError(f"{name} must not run in dry-run")

        return _fail

    monkeypatch.setattr(sg, "render_chart_snapshot", forbid("renderer"))
    monkeypatch.setattr(sg, "upload_chart_image", forbid("S3 upload"))
    monkeypatch.setattr(sg, "generate_presigned_chart_url", forbid("presigned URL"))
    monkeypatch.setattr(sg, "link_paper_signal_chart_snapshot", forbid("relation save"))
    monkeypatch.setattr(sg.tempfile, "TemporaryDirectory", forbid("temp dir"))
    monkeypatch.setattr(sg.requests, "post", forbid("Discord"))

    with caplog.at_level(logging.INFO, logger="paper_signal_generator"):
        created = sg.run_once(dry_run=True, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert not bull_cycle["paper_db"].exists()
    assert bull_cycle["chart_db"].stat().st_mtime_ns == mtime_before
    # Console diagnostics name the plan: snapshot candidate + derived image intent.
    assert "chart snapshot attachment candidate: snap-1" in caplog.text
    assert "paper signal annotated chart plan" in caplog.text
    assert "symbol=USDJPY" in caplog.text
    assert "target_candle=2026-07-05T12:00:00Z" in caplog.text
    assert "side=long" in caplog.text
    assert "would_generate=True" in caplog.text


def test_dry_run_reports_no_derived_image_without_snapshot_candidate(bull_cycle, caplog):
    with caplog.at_level(logging.INFO, logger="paper_signal_generator"):
        created = sg.run_once(dry_run=True, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert not bull_cycle["chart_db"].exists()
    assert "chart snapshot attachment candidate: none" in caplog.text
    assert "would_generate=False" in caplog.text
