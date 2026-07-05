import pathlib
import sys
import json
import time

import pytest


BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

import news_feed_bot as bot  # noqa: E402


SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Sample market feed</title>
    <item>
      <guid>article-1</guid>
      <title>Stocks rise as central bank signals lower interest rates</title>
      <link>https://example.com/markets-1</link>
      <description><![CDATA[<p>Markets gained after inflation cooled.</p>]]></description>
      <pubDate>Sat, 04 Jul 2026 03:00:00 GMT</pubDate>
    </item>
    <item>
      <guid>article-2</guid>
      <title>Sports club wins final</title>
      <link>https://example.com/sports-1</link>
      <description>Non market article.</description>
      <pubDate>Sat, 04 Jul 2026 02:00:00 GMT</pubDate>
    </item>
    <item>
      <guid>article-3</guid>
      <title>Gold and oil prices move before earnings season</title>
      <link>https://example.com/commodities-1</link>
      <description>Commodities traders watched earnings.</description>
      <pubDate>Sat, 04 Jul 2026 01:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

JAPANESE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Japanese market feed</title>
    <item>
      <guid>jp-1</guid>
      <title>日経平均が上昇、円安と株価の堅調さを好感</title>
      <link>https://example.com/jp-market-1</link>
      <description>日銀の政策と金利見通しが市場の注目を集めた。</description>
      <pubDate>Sat, 04 Jul 2026 04:00:00 GMT</pubDate>
    </item>
    <item>
      <guid>jp-2</guid>
      <title>地域イベントに多くの来場者</title>
      <link>https://example.com/local-1</link>
      <description>週末の祭りが各地で開催された。</description>
      <pubDate>Sat, 04 Jul 2026 03:30:00 GMT</pubDate>
    </item>
  </channel>
</rss>
""".encode("utf-8")


def test_rss_entries_can_build_discord_messages():
    messages, selected, skipped = bot.build_messages_from_rss(
        "Sample RSS",
        SAMPLE_RSS,
        posted_ids=[],
        max_articles=5,
    )

    assert len(selected) == 2
    assert skipped == 1
    assert len(messages) == 2
    first_embed = messages[0]["embeds"][0]
    assert first_embed["title"] == "Stocks rise as central bank signals lower interest rates"
    assert first_embed["url"] == "https://example.com/markets-1"
    assert first_embed["description"] == "Markets gained after inflation cooled."


def test_articles_in_posted_ids_are_not_selected():
    entries = bot.parse_rss_entries("Sample RSS", SAMPLE_RSS)

    selected, skipped = bot.select_new_articles(
        entries,
        posted_ids=["article-1", "article-3"],
        max_articles=5,
    )

    assert selected == []
    assert skipped == 3


def test_only_articles_missing_from_posted_ids_are_send_targets():
    entries = bot.parse_rss_entries("Sample RSS", SAMPLE_RSS)

    selected, skipped = bot.select_new_articles(
        entries,
        posted_ids=["article-1"],
        max_articles=5,
    )

    assert [bot.article_id(article) for article in selected] == ["article-3"]
    assert skipped == 2


def test_japanese_finance_keywords_are_selected():
    entries = bot.parse_rss_entries("Japanese RSS", JAPANESE_RSS)

    selected, skipped = bot.select_new_articles(entries, posted_ids=[], max_articles=5)

    assert [bot.article_id(article) for article in selected] == ["jp-1"]
    assert skipped == 1


def test_general_japanese_article_without_finance_keywords_is_excluded():
    article = {
        "id": "general-1",
        "title": "地域イベントに多くの来場者",
        "summary": "週末の祭りが各地で開催された。",
        "link": "https://example.com/local-1",
    }

    selected, skipped = bot.select_new_articles([article], posted_ids=[], max_articles=5)

    assert selected == []
    assert skipped == 1


def test_importance_score_adds_points_for_high_importance_keywords():
    article = {
        "id": "important-1",
        "title": "Breaking FRB interest rates shock lifts stocks",
        "summary": "Inflation and earnings remain in focus for markets.",
    }

    score = bot.importance_score(article)

    assert score >= 20


def test_importance_score_subtracts_points_for_low_value_keywords():
    article = {
        "id": "low-value-1",
        "title": "Stocks ranking for individual investors PR",
        "summary": "Sponsored column for beginners and promotion.",
    }

    score = bot.importance_score(article)

    assert score < 0


def test_discord_payload_includes_importance_score_and_hit_keywords():
    article = {
        "id": "payload-score-1",
        "title": "Breaking central bank inflation news lifts stocks",
        "summary": "<p>Markets watched interest rates.</p>",
        "link": "https://example.com/scored",
        "_feed_name": "Fixture",
    }

    payload = bot.build_discord_message(article)
    footer = payload["embeds"][0]["footer"]["text"]

    assert "Score:" in footer
    assert "Hits:" in footer


def test_rank_articles_puts_higher_score_before_lower_score():
    high_score_article = {
        "id": "high-score",
        "title": "Breaking FRB inflation shock hits stocks and bonds",
        "summary": "Central bank interest rates and earnings in focus.",
        "published_parsed": time.strptime("Sat, 04 Jul 2026 01:00:00 GMT", "%a, %d %b %Y %H:%M:%S %Z"),
    }
    low_score_article = {
        "id": "low-score",
        "title": "Stocks edge higher",
        "summary": "Markets update.",
        "published_parsed": time.strptime("Sat, 04 Jul 2026 04:00:00 GMT", "%a, %d %b %Y %H:%M:%S %Z"),
    }

    ranked = bot.rank_articles([low_score_article, high_score_article], max_articles=2)

    assert [article["id"] for article in ranked] == ["high-score", "low-score"]


def test_rank_articles_limits_total_articles():
    articles = [
        {"id": "a1", "title": "Breaking FRB stocks", "summary": "markets"},
        {"id": "a2", "title": "Inflation stocks", "summary": "markets"},
        {"id": "a3", "title": "Stocks", "summary": "markets"},
    ]

    ranked = bot.rank_articles(articles, max_articles=2)

    assert len(ranked) == 2
    assert [article["id"] for article in ranked] == ["a1", "a2"]


def test_rank_articles_uses_newer_published_date_when_scores_tie():
    older = {
        "id": "older",
        "title": "Stocks rise",
        "summary": "Markets update.",
        "published_parsed": time.strptime("Sat, 04 Jul 2026 01:00:00 GMT", "%a, %d %b %Y %H:%M:%S %Z"),
    }
    newer = {
        "id": "newer",
        "title": "Stocks rise",
        "summary": "Markets update.",
        "published_parsed": time.strptime("Sat, 04 Jul 2026 04:00:00 GMT", "%a, %d %b %Y %H:%M:%S %Z"),
    }

    ranked = bot.rank_articles([older, newer], max_articles=2)

    assert [article["id"] for article in ranked] == ["newer", "older"]


def test_select_new_articles_returns_reason_counts():
    entries = [
        {"id": "posted", "title": "Stocks", "summary": "markets"},
        {"id": "dup", "title": "Stocks", "summary": "markets"},
        {"id": "dup", "title": "Stocks", "summary": "markets"},
        {"id": "non-finance", "title": "Sports club wins", "summary": "final"},
        {"id": "extra", "title": "Oil and gold markets", "summary": "commodities"},
    ]

    selected, skipped, reason_stats = bot.select_new_articles(
        entries,
        posted_ids=["posted"],
        max_articles=1,
        return_stats=True,
    )

    assert [article["id"] for article in selected] == ["dup"]
    assert skipped == 4
    assert reason_stats == {
        "posted": 1,
        "duplicate": 1,
        "non_finance": 1,
        "low_score": 0,
        "per_feed_limit": 1,
        "total_limit": 0,
        "selected": 1,
    }


def test_fetch_and_post_applies_max_total_articles_per_run(tmp_path, monkeypatch):
    posted_ids_path = tmp_path / "posted_ids.json"
    posted_ids_path.write_text("[]", encoding="utf-8")
    entries = [
        {"id": "high", "title": "Breaking FRB inflation stocks", "summary": "markets"},
        {"id": "middle", "title": "Oil and gold markets", "summary": "commodities"},
        {"id": "low", "title": "Stocks", "summary": "markets"},
    ]
    sent_articles = []

    monkeypatch.setattr(bot, "POSTED_IDS_FILE", posted_ids_path)
    monkeypatch.setattr(bot, "RSS_FEEDS", [("Fixture RSS", "https://example.invalid/rss")])
    monkeypatch.setattr(bot, "MAX_ARTICLES_PER_RUN", 10)
    monkeypatch.setattr(bot, "MAX_TOTAL_ARTICLES_PER_RUN", 2)
    monkeypatch.setattr(bot, "fetch_feed", lambda feed_name, rss_url: entries)
    monkeypatch.setattr(
        bot,
        "post_article",
        lambda article, webhook_url=None, dry_run=False: sent_articles.append(bot.article_id(article)),
    )
    monkeypatch.setattr(bot, "send_dev_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot.time, "sleep", lambda seconds: None)

    bot.fetch_and_post(dry_run=False)

    assert sent_articles == ["high", "middle"]
    assert json.loads(posted_ids_path.read_text(encoding="utf-8")) == ["high", "middle"]


def test_debug_selection_true_prints_reason_counts(tmp_path, monkeypatch, capsys):
    posted_ids_path = tmp_path / "posted_ids.json"
    posted_ids_path.write_text(json.dumps(["posted"]), encoding="utf-8")
    entries = [
        {"id": "posted", "title": "Stocks", "summary": "markets"},
        {"id": "dup", "title": "Stocks", "summary": "markets"},
        {"id": "dup", "title": "Stocks", "summary": "markets"},
        {"id": "non-finance", "title": "Sports club wins", "summary": "final"},
        {"id": "top", "title": "Breaking FRB inflation stocks", "summary": "markets"},
        {"id": "extra", "title": "Oil and gold markets", "summary": "commodities"},
    ]

    monkeypatch.setattr(bot, "DEBUG_SELECTION", True)
    monkeypatch.setattr(bot, "POSTED_IDS_FILE", posted_ids_path)
    monkeypatch.setattr(bot, "RSS_FEEDS", [("Fixture RSS", "https://example.invalid/rss")])
    monkeypatch.setattr(bot, "MAX_ARTICLES_PER_RUN", 2)
    monkeypatch.setattr(bot, "MAX_TOTAL_ARTICLES_PER_RUN", 1)
    monkeypatch.setattr(bot, "fetch_feed", lambda feed_name, rss_url: entries)
    monkeypatch.setattr(bot, "post_article", lambda article, webhook_url=None, dry_run=False: None)
    monkeypatch.setattr(bot, "send_dev_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot.time, "sleep", lambda seconds: None)

    bot.fetch_and_post(dry_run=True)

    output = capsys.readouterr().out
    assert "[DEBUG] Fixture RSS:" in output
    assert "posted=1 duplicate=1 non_finance=1 low_score=0 per_feed_limit=1 total_limit=0 selected=2" in output
    assert "[DEBUG] Selection stats:" in output
    assert "posted=1 duplicate=1 non_finance=1 low_score=0 per_feed_limit=1 total_limit=1 selected=1" in output


def test_debug_selection_false_suppresses_reason_counts(tmp_path, monkeypatch, capsys):
    posted_ids_path = tmp_path / "posted_ids.json"
    posted_ids_path.write_text("[]", encoding="utf-8")
    entries = [{"id": "one", "title": "Stocks", "summary": "markets"}]

    monkeypatch.setattr(bot, "DEBUG_SELECTION", False)
    monkeypatch.setattr(bot, "POSTED_IDS_FILE", posted_ids_path)
    monkeypatch.setattr(bot, "RSS_FEEDS", [("Fixture RSS", "https://example.invalid/rss")])
    monkeypatch.setattr(bot, "fetch_feed", lambda feed_name, rss_url: entries)
    monkeypatch.setattr(bot, "post_article", lambda article, webhook_url=None, dry_run=False: None)
    monkeypatch.setattr(bot, "send_dev_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot.time, "sleep", lambda seconds: None)

    bot.fetch_and_post(dry_run=True)

    assert "[DEBUG]" not in capsys.readouterr().out


def test_dry_run_prints_without_sending_to_discord(capsys):
    class FailingSession:
        def post(self, *args, **kwargs):
            raise AssertionError("HTTP post must not be called in DRY_RUN")

    payload = {"embeds": [{"title": "Dry run market story"}]}

    result = bot.send_discord_webhook(
        "https://discord.invalid/webhook",
        payload,
        dry_run=True,
        session=FailingSession(),
    )

    captured = capsys.readouterr()
    assert result is None
    assert "[DRY_RUN] Would send Discord message: Dry run market story" in captured.out


def test_non_dry_run_uses_injected_session_without_real_webhook():
    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeSession:
        def __init__(self):
            self.calls = []

        def post(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return FakeResponse()

    session = FakeSession()
    payload = {"embeds": [{"title": "Market story"}]}

    bot.send_discord_webhook(
        "https://discord.invalid/webhook",
        payload,
        dry_run=False,
        session=session,
    )

    assert len(session.calls) == 1
    args, kwargs = session.calls[0]
    assert args == ("https://discord.invalid/webhook",)
    assert kwargs["json"] == payload


def test_dry_run_does_not_update_posted_ids_json(tmp_path, monkeypatch):
    posted_ids_path = tmp_path / "posted_ids.json"
    original_ids = ["article-1"]
    posted_ids_path.write_text(json.dumps(original_ids), encoding="utf-8")

    sent_articles = []

    def fake_fetch_feed(feed_name, rss_url):
        return bot.parse_rss_entries(feed_name, SAMPLE_RSS)

    def fake_post_article(article, *, webhook_url=None, dry_run=False):
        assert dry_run is True
        sent_articles.append(bot.article_id(article))

    monkeypatch.setattr(bot, "POSTED_IDS_FILE", posted_ids_path)
    monkeypatch.setattr(bot, "RSS_FEEDS", [("Sample RSS", "https://example.invalid/rss")])
    monkeypatch.setattr(bot, "fetch_feed", fake_fetch_feed)
    monkeypatch.setattr(bot, "post_article", fake_post_article)
    monkeypatch.setattr(bot, "send_dev_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot.time, "sleep", lambda seconds: None)

    bot.fetch_and_post(dry_run=True)

    assert json.loads(posted_ids_path.read_text(encoding="utf-8")) == original_ids
    assert sent_articles == ["article-3"]


def test_cli_dry_run_argument_matches_dry_run_mode():
    args = bot.parse_args(["--dry-run"])

    assert args.dry_run is True


def test_cli_send_market_snapshot_now_argument():
    args = bot.parse_args(["--send-market-snapshot-now"])

    assert args.send_market_snapshot_now is True


def test_dry_run_request_helper_honors_cli_argument(monkeypatch):
    monkeypatch.setattr(bot, "DRY_RUN", False)

    assert bot.is_dry_run_requested(["--dry-run"]) is True


def test_send_market_snapshot_now_exits_without_news_loop(monkeypatch):
    calls = []

    monkeypatch.setattr(bot, "DRY_RUN", False)
    monkeypatch.setattr(bot, "validate_config", lambda *, dry_run=False: calls.append(("validate", dry_run)))
    monkeypatch.setattr(bot, "post_market_snapshot", lambda *, dry_run=False: calls.append(("snapshot", dry_run)))
    monkeypatch.setattr(
        bot,
        "fetch_and_post",
        lambda *, dry_run=False: (_ for _ in ()).throw(AssertionError("news loop should not run")),
    )

    bot.main(["--send-market-snapshot-now", "--dry-run"])

    assert calls == [("validate", True), ("snapshot", True)]
