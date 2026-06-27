import html
import json
import os
import re
import shutil
import signal
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import requests
import schedule


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
POSTED_IDS_FILE = BASE_DIR / "posted_ids.json"
MAX_SAVED_IDS = 500
REQUEST_TIMEOUT_SECONDS = 20

DEFAULT_NEWS_QUERY = (
    'site:reuters.com '
    '(markets OR stocks OR bonds OR forex OR commodities OR oil OR gold '
    'OR "central bank" OR inflation OR "interest rates" OR earnings) '
    "when:1d"
)

STOP_REQUESTED = False


def load_env_file(path):
    """Return KEY=VALUE entries read only from the specified .env file."""
    if not path.exists():
        return {}

    values = {}
    with path.open("r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            if key:
                values[key] = value
    return values


ENV_VALUES = load_env_file(ENV_FILE)

NEWS_WEBHOOK_URL = ENV_VALUES.get("NEWS_WEBHOOK_URL", "").strip()
DEV_LOG_WEBHOOK_URL = ENV_VALUES.get("DEV_LOG_WEBHOOK_URL", "").strip()
GOOGLE_NEWS_QUERY = ENV_VALUES.get("GOOGLE_NEWS_QUERY", DEFAULT_NEWS_QUERY).strip()
MAX_ARTICLES_PER_RUN = int(ENV_VALUES.get("MAX_ARTICLES_PER_RUN", "5"))
FETCH_INTERVAL_MINUTES = int(ENV_VALUES.get("FETCH_INTERVAL_MINUTES", "60"))
GOOGLE_NEWS_RSS_URL = (
    f"https://news.google.com/rss/search?q={quote_plus(GOOGLE_NEWS_QUERY)}"
    "&hl=en-US&gl=US&ceid=US:en"
)
RSS_FEEDS = [
    ("Google News / Reuters", GOOGLE_NEWS_RSS_URL),
    ("Yahoo!ニュース / 経済", "https://news.yahoo.co.jp/rss/categories/business.xml"),
    ("Yahoo!ニュース / IT", "https://news.yahoo.co.jp/rss/categories/it.xml"),
    ("Yahoo!ニュース / 国内", "https://news.yahoo.co.jp/rss/categories/domestic.xml"),
    ("Yahoo!ニュース / 国際", "https://news.yahoo.co.jp/rss/categories/world.xml"),
]


def strip_html(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def article_id(article):
    return article.get("id") or article.get("link", "")


def load_posted_ids():
    if not POSTED_IDS_FILE.exists():
        return []

    try:
        with POSTED_IDS_FILE.open("r", encoding="utf-8") as posted_file:
            data = json.load(posted_file)
        if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
            raise ValueError("posted_ids.json must contain a list of strings")
        return data
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = POSTED_IDS_FILE.with_name(
            f"{POSTED_IDS_FILE.stem}.corrupt-{timestamp}{POSTED_IDS_FILE.suffix}"
        )
        try:
            shutil.move(str(POSTED_IDS_FILE), str(backup_path))
            print(f"[WARN] Invalid posted ID file moved to: {backup_path.name}")
        except OSError as backup_exc:
            print(f"[WARN] Could not back up invalid posted ID file: {backup_exc}")
        print(f"[WARN] Starting with an empty posted ID list: {exc}")
        return []


def save_posted_ids(ids):
    unique_ids = list(dict.fromkeys(ids))[-MAX_SAVED_IDS:]
    temp_path = POSTED_IDS_FILE.with_suffix(".json.tmp")
    with temp_path.open("w", encoding="utf-8") as posted_file:
        json.dump(unique_ids, posted_file, ensure_ascii=False, indent=2)
        posted_file.flush()
        os.fsync(posted_file.fileno())
    os.replace(temp_path, POSTED_IDS_FILE)


def post_webhook(webhook_url, payload):
    response = requests.post(
        webhook_url,
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def post_article(article):
    summary = strip_html(article.get("summary", ""))
    description = summary[:300] + "..." if len(summary) > 300 else summary
    embed = {
        "title": article.get("title", "No title")[:256],
        "url": article.get("link", ""),
        "description": description,
        "color": 0xE8312A,
        "footer": {
            "text": (
                f"{article.get('_feed_name', 'RSS')} • "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )
        },
    }
    post_webhook(NEWS_WEBHOOK_URL, {"embeds": [embed]})


def fetch_feed(feed_name, rss_url):
    response = requests.get(
        rss_url,
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": "DiscordMarketNewsFeedBot/1.0"},
    )
    response.raise_for_status()
    feed = feedparser.parse(response.content)

    if getattr(feed, "bozo", False) and not feed.entries:
        raise RuntimeError(f"RSS parse failed: {feed.bozo_exception}")

    entries = list(feed.entries)
    for entry in entries:
        entry["_feed_name"] = feed_name
    return entries


def published_sort_key(article):
    published = article.get("published_parsed") or article.get("updated_parsed")
    return time.mktime(published) if published else 0


def send_dev_log(stats, status="OK", detail=""):
    if not DEV_LOG_WEBHOOK_URL:
        print("[WARN] DEV_LOG_WEBHOOK_URL is not set; skipping dev-log post.")
        return

    fields = [
        {"name": "fetch", "value": str(stats["fetch"]), "inline": True},
        {"name": "post", "value": str(stats["post"]), "inline": True},
        {"name": "skip", "value": str(stats["skip"]), "inline": True},
        {"name": "error", "value": str(stats["error"]), "inline": True},
    ]
    embed = {
        "title": f"News Feed Bot Run: {status}",
        "color": 0x2ECC71 if status == "OK" else 0xE74C3C,
        "fields": fields,
        "timestamp": datetime.now().astimezone().isoformat(),
    }
    if detail:
        embed["description"] = detail[:4000]

    try:
        post_webhook(DEV_LOG_WEBHOOK_URL, {"embeds": [embed]})
    except requests.RequestException as exc:
        print(f"[ERROR] Failed to post dev log: {exc}")


def fetch_and_post():
    stats = {"fetch": 0, "post": 0, "skip": 0, "error": 0}
    details = []
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Fetching RSS feed...")

    try:
        posted_ids = load_posted_ids()
        posted_id_set = set(posted_ids)
        queued_id_set = set()
        selected = []
        for feed_name, rss_url in RSS_FEEDS:
            if STOP_REQUESTED:
                details.append("Stop requested; remaining RSS feeds were skipped.")
                break
            try:
                feed_entries = fetch_feed(feed_name, rss_url)
                feed_entries.sort(key=published_sort_key, reverse=True)
                stats["fetch"] += len(feed_entries)
                print(f"[OK] Fetched {len(feed_entries)} article(s): {feed_name}")

                feed_selected = []
                for entry in feed_entries:
                    entry_id = article_id(entry)
                    if (
                        not entry_id
                        or entry_id in posted_id_set
                        or entry_id in queued_id_set
                    ):
                        stats["skip"] += 1
                        continue
                    if len(feed_selected) >= MAX_ARTICLES_PER_RUN:
                        stats["skip"] += 1
                        continue
                    feed_selected.append(entry)
                    queued_id_set.add(entry_id)

                selected.extend(feed_selected)
                print(
                    f"[INFO] Selected {len(feed_selected)} article(s): {feed_name}"
                )
            except Exception as exc:
                stats["error"] += 1
                details.append(f"Fetch failed: {feed_name} ({exc})")
                print(f"[ERROR] Failed to fetch {feed_name}: {exc}")

        selected.sort(key=published_sort_key, reverse=True)

        for index, article in enumerate(selected):
            if STOP_REQUESTED:
                stats["skip"] += len(selected) - index
                details.append("Stop requested; remaining articles were skipped.")
                break

            try:
                post_article(article)
                entry_id = article_id(article)
                posted_ids.append(entry_id)
                posted_id_set.add(entry_id)
                stats["post"] += 1
                print(f"[OK] Posted: {article.get('title', '')[:60]}")
            except requests.RequestException as exc:
                stats["error"] += 1
                details.append(f"Post failed: {article.get('title', 'No title')[:100]} ({exc})")
                print(f"[ERROR] Failed to post article: {exc}")
            time.sleep(1)

        save_posted_ids(posted_ids)
    except Exception as exc:
        stats["error"] += 1
        details.append(str(exc))
        print(f"[ERROR] Run failed: {exc}")
    finally:
        status = "OK" if stats["error"] == 0 else "ERROR"
        send_dev_log(stats, status=status, detail="\n".join(details))
        print(
            "[SUMMARY] "
            f"fetch={stats['fetch']} post={stats['post']} "
            f"skip={stats['skip']} error={stats['error']}"
        )


def request_shutdown(signum, _frame):
    global STOP_REQUESTED
    if not STOP_REQUESTED:
        STOP_REQUESTED = True
        print(f"\n[INFO] Shutdown requested (signal {signum}). Finishing safely...")


def validate_config():
    errors = []
    if not ENV_FILE.exists():
        errors.append(".env file is missing")
    if not NEWS_WEBHOOK_URL:
        errors.append("NEWS_WEBHOOK_URL is not set in .env")
    if MAX_ARTICLES_PER_RUN < 1:
        errors.append("MAX_ARTICLES_PER_RUN must be at least 1")
    if FETCH_INTERVAL_MINUTES < 1:
        errors.append("FETCH_INTERVAL_MINUTES must be at least 1")
    if errors:
        raise RuntimeError("; ".join(errors))


def main():
    signal.signal(signal.SIGINT, request_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_shutdown)

    validate_config()
    print("=== Discord Market News Feed Bot ===")
    print(f"Query: {GOOGLE_NEWS_QUERY}")
    print(f"RSS feeds: {len(RSS_FEEDS)}")
    print(f"Interval: Every {FETCH_INTERVAL_MINUTES} minute(s)")
    print("Starting...\n")

    fetch_and_post()
    schedule.every(FETCH_INTERVAL_MINUTES).minutes.do(fetch_and_post)

    while not STOP_REQUESTED:
        schedule.run_pending()
        time.sleep(1)

    print("[INFO] Bot stopped safely.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[FATAL] {exc}")
        send_dev_log(
            {"fetch": 0, "post": 0, "skip": 0, "error": 1},
            status="FATAL",
            detail=str(exc),
        )
        raise SystemExit(1)
