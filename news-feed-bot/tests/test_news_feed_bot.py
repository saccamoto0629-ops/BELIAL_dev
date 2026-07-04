import pathlib
import sys
import json

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


def test_dry_run_request_helper_honors_cli_argument(monkeypatch):
    monkeypatch.setattr(bot, "DRY_RUN", False)

    assert bot.is_dry_run_requested(["--dry-run"]) is True
