"""Issue #55: Paper Signal Discord notifications carry the existing Chart Snapshot image.

Covers the notify pipeline around run_once: a matching stored snapshot yields a
single webhook post with the unchanged text body plus an image embed (presigned
from the already-uploaded S3 object) and a persisted relation in the Chart DB;
every image-side failure degrades to a text-only notification without losing
the saved Paper Signal; duplicates and --dry-run perform no image side effects.
"""

import logging
import pathlib
import sqlite3
import sys
from datetime import UTC, datetime

import pytest
import requests

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import paper_signal_store as store  # noqa: E402
import signal_generator as sg  # noqa: E402  (bootstraps market-watch on sys.path)
import chart_collector  # noqa: E402  (market-watch)
import chart_snapshot_store  # noqa: E402  (market-watch)

EVAL_TIME = datetime(2026, 7, 5, 18, 0, 0, tzinfo=UTC)
CANDLE_TS = "2026-07-05T12:00:00Z"
SNAPSHOT_S3_KEY = "charts/USDJPY/1h/snap-1.png"


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


def seed_stored_snapshot(chart_db, *, snapshot_id="snap-1", candle_time=CANDLE_TS, s3_bucket="belial-chart-snapshots"):
    chart_collector.init_db(chart_db)
    return chart_collector.insert_chart_snapshot(
        chart_db,
        symbol="USDJPY",
        timeframe="1h",
        snapshot_at="2026-07-05T12:05:00Z",
        s3_bucket=s3_bucket,
        s3_key=SNAPSHOT_S3_KEY,
        captured_at="2026-07-05T12:05:00Z",
        candle_time=candle_time,
        snapshot_id=snapshot_id,
    )


@pytest.fixture
def bull_cycle(monkeypatch, tmp_path):
    """One Bull cycle wired to a tmp Chart DB, stubbed presigned URLs, and a
    live-looking webhook -- only the HTTP leaf and S3 leaf are faked."""
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


# --- Discord: snapshot found -> one webhook post with body + image embed ------


def test_snapshot_found_sends_single_post_with_embed_and_saves_link(bull_cycle, posted):
    seed_stored_snapshot(bull_cycle["chart_db"])

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert len(posted) == 1  # body and image share ONE webhook post
    payload = posted[0]["json"]
    # The text body format is unchanged and rides in "content" as before.
    assert payload["content"] == sg.format_discord_message(created[0])[:2000]
    assert payload["embeds"] == [{"image": {"url": f"https://s3.test/{SNAPSHOT_S3_KEY}"}}]
    # The relation was persisted in the Chart DB (alert_chart_snapshots analogue).
    links = chart_snapshot_store.list_paper_signal_links(bull_cycle["chart_db"], created[0]["signal_id"])
    assert [link["chart_snapshot_id"] for link in links] == ["snap-1"]


def test_no_matching_snapshot_sends_text_only(bull_cycle, posted):
    # Only a FUTURE snapshot exists -> it must not be attached.
    seed_stored_snapshot(bull_cycle["chart_db"], candle_time="2026-07-05T13:00:00Z")

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert len(posted) == 1
    payload = posted[0]["json"]
    assert payload["content"] == sg.format_discord_message(created[0])[:2000]
    assert "embeds" not in payload  # text-only, no image embed


def test_missing_chart_db_sends_text_only_and_persists_signal(bull_cycle, posted):
    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert len(posted) == 1
    assert "embeds" not in posted[0]["json"]
    assert not bull_cycle["chart_db"].exists()  # lookup never creates the Chart DB
    assert len(store.list_signals(db_path=bull_cycle["paper_db"])) == 1


def test_presigned_url_failure_still_sends_text_and_keeps_link(bull_cycle, posted, monkeypatch):
    seed_stored_snapshot(bull_cycle["chart_db"])

    def boom(s3_key, config=None, **kw):
        raise RuntimeError("S3 presign failed")

    monkeypatch.setattr(sg, "generate_presigned_chart_url", boom)

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert len(posted) == 1
    assert "embeds" not in posted[0]["json"]  # image dropped, body still delivered
    # The relation save happened before URL generation and is retained.
    links = chart_snapshot_store.list_paper_signal_links(bull_cycle["chart_db"], created[0]["signal_id"])
    assert len(links) == 1


def test_link_save_failure_still_sends_body_with_image(bull_cycle, posted, monkeypatch):
    seed_stored_snapshot(bull_cycle["chart_db"])

    def boom(db_path, *, paper_signal_id, chart_snapshot_id):
        raise sqlite3.OperationalError("chart DB locked")

    monkeypatch.setattr(sg, "link_paper_signal_chart_snapshot", boom)

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert len(posted) == 1
    assert posted[0]["json"]["embeds"] == [{"image": {"url": f"https://s3.test/{SNAPSHOT_S3_KEY}"}}]
    assert len(store.list_signals(db_path=bull_cycle["paper_db"])) == 1


def test_discord_failure_keeps_saved_signal_and_link(bull_cycle, monkeypatch):
    seed_stored_snapshot(bull_cycle["chart_db"])

    def failing_post(url, json=None, timeout=None):
        raise requests.RequestException("discord down")

    monkeypatch.setattr(sg.requests, "post", failing_post)

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert len(store.list_signals(db_path=bull_cycle["paper_db"])) == 1
    links = chart_snapshot_store.list_paper_signal_links(bull_cycle["chart_db"], created[0]["signal_id"])
    assert len(links) == 1


def test_presigned_url_signs_against_snapshot_row_bucket(bull_cycle, posted, monkeypatch):
    # The row records where its image actually lives; the environment bucket
    # may have changed since upload and must not win.
    seed_stored_snapshot(bull_cycle["chart_db"], s3_bucket="db-bucket")
    monkeypatch.setattr(sg, "load_chart_env_file", lambda *a, **k: {"S3_BUCKET_NAME": "env-bucket"})
    signed_buckets = []
    monkeypatch.setattr(
        sg,
        "generate_presigned_chart_url",
        lambda s3_key, config=None, **kw: signed_buckets.append(config["bucket"]) or f"https://s3.test/{s3_key}",
    )

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert signed_buckets == ["db-bucket"]
    assert posted[0]["json"]["embeds"] == [{"image": {"url": f"https://s3.test/{SNAPSHOT_S3_KEY}"}}]


def test_presigned_url_falls_back_to_env_bucket_for_legacy_rows(bull_cycle, posted, monkeypatch):
    # A legacy row without a recorded bucket uses the configured one.
    seed_stored_snapshot(bull_cycle["chart_db"], s3_bucket="")
    monkeypatch.setattr(sg, "load_chart_env_file", lambda *a, **k: {"S3_BUCKET_NAME": "env-bucket"})
    signed_buckets = []
    monkeypatch.setattr(
        sg,
        "generate_presigned_chart_url",
        lambda s3_key, config=None, **kw: signed_buckets.append(config["bucket"]) or f"https://s3.test/{s3_key}",
    )

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert signed_buckets == ["env-bucket"]


def test_blank_presigned_url_degrades_to_text_only_post(bull_cycle, posted, monkeypatch):
    # A blank/garbage embed URL would make Discord 400 the whole post; the
    # embed must be dropped so the body still goes out.
    seed_stored_snapshot(bull_cycle["chart_db"])
    monkeypatch.setattr(sg, "generate_presigned_chart_url", lambda s3_key, config=None, **kw: " ")

    created = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert len(posted) == 1
    assert "embeds" not in posted[0]["json"]


def test_duplicate_signal_skips_link_url_and_discord(bull_cycle, posted, monkeypatch):
    seed_stored_snapshot(bull_cycle["chart_db"])
    link_calls = []
    real_link = sg.link_paper_signal_chart_snapshot
    monkeypatch.setattr(
        sg,
        "link_paper_signal_chart_snapshot",
        lambda *a, **k: link_calls.append(k) or real_link(*a, **k),
    )
    url_calls = []
    monkeypatch.setattr(
        sg,
        "generate_presigned_chart_url",
        lambda s3_key, config=None, **kw: url_calls.append(s3_key) or f"https://s3.test/{s3_key}",
    )

    first = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)
    second = sg.run_once(dry_run=False, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(first) == 1
    assert second == []            # duplicate suppressed by the existing rule
    assert len(posted) == 1        # no Discord re-send
    assert len(link_calls) == 1    # no repeated relation save attempt
    assert len(url_calls) == 1     # no repeated presigned URL generation
    assert len(chart_snapshot_store.list_paper_signal_links(bull_cycle["chart_db"], first[0]["signal_id"])) == 1


# --- Payload shape ------------------------------------------------------------


def test_build_discord_payload_with_image_matches_issue_shape():
    payload = sg.build_discord_payload("hello", "https://s3.test/chart.png")

    assert payload == {
        "content": "hello",
        "embeds": [{"image": {"url": "https://s3.test/chart.png"}}],
    }


def test_build_discord_payload_without_image_is_content_only():
    assert sg.build_discord_payload("hello") == {"content": "hello"}
    assert sg.build_discord_payload("hello", None) == {"content": "hello"}


def test_build_discord_payload_truncates_content_to_discord_limit():
    payload = sg.build_discord_payload("x" * 3000)
    assert len(payload["content"]) == 2000


def test_build_discord_payload_drops_blank_or_malformed_image_urls():
    # Discord rejects the whole post over a bad embed URL, so anything that is
    # not a well-formed http(s) URL must yield a content-only payload.
    for bad in (" ", "not-a-url", "ftp://host/x.png", "https://", 123):
        assert sg.build_discord_payload("hello", bad) == {"content": "hello"}


def test_build_discord_payload_strips_whitespace_around_valid_url():
    payload = sg.build_discord_payload("hello", "  https://s3.test/x.png  ")
    assert payload["embeds"] == [{"image": {"url": "https://s3.test/x.png"}}]


# --- dry-run: no image side effects ------------------------------------------


def test_dry_run_performs_no_image_side_effects(bull_cycle, monkeypatch, caplog):
    seed_stored_snapshot(bull_cycle["chart_db"])
    schema_before = _schema(bull_cycle["chart_db"])
    mtime_before = bull_cycle["chart_db"].stat().st_mtime_ns

    monkeypatch.setattr(
        sg, "generate_presigned_chart_url",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("presigned URL must not be generated in dry-run")),
    )
    monkeypatch.setattr(
        sg, "link_paper_signal_chart_snapshot",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("relation must not be saved in dry-run")),
    )

    class FailingSession:
        def post(self, *args, **kwargs):
            raise AssertionError("no HTTP in dry-run")

    monkeypatch.setattr(sg.requests, "post", FailingSession().post)

    with caplog.at_level(logging.INFO, logger="paper_signal_generator"):
        created = sg.run_once(dry_run=True, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert not bull_cycle["paper_db"].exists()                       # no Paper Signal DB
    assert _schema(bull_cycle["chart_db"]) == schema_before          # no schema migration
    assert bull_cycle["chart_db"].stat().st_mtime_ns == mtime_before  # Chart DB untouched
    # The attachment candidate is reported on the console log only.
    assert "chart snapshot attachment candidate: snap-1" in caplog.text


def test_dry_run_reports_absence_of_attachment_candidate(bull_cycle, caplog):
    # No Chart DB at all: the dry run must say so without creating one.
    with caplog.at_level(logging.INFO, logger="paper_signal_generator"):
        created = sg.run_once(dry_run=True, db_path=bull_cycle["paper_db"], now_utc=EVAL_TIME)

    assert len(created) == 1
    assert not bull_cycle["chart_db"].exists()
    assert "chart snapshot attachment candidate: none" in caplog.text


def _schema(db_path):
    with sqlite3.connect(db_path) as conn:
        return sorted(conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall())
