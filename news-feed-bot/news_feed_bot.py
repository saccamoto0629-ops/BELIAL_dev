import argparse
import html
import json
import os
import re
import shutil
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import requests
import schedule

try:
    from market_snapshot import get_market_snapshot_message
except Exception as exc:  # optional dependency; do not crash the news bot
    get_market_snapshot_message = None
    print(f"[WARN] Market snapshot feature is unavailable: {exc}")


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

FINANCE_KEYWORDS = [
    "株価",
    "日経平均",
    "topix",
    "株式",
    "株式市場",
    "市場",
    "急騰",
    "急落",
    "上場",
    "ipo",
    "決算",
    "業績",
    "増益",
    "減益",
    "配当",
    "為替",
    "円安",
    "円高",
    "ドル円",
    "fx",
    "金利",
    "利上げ",
    "利下げ",
    "日銀",
    "日本銀行",
    "frb",
    "中央銀行",
    "政策金利",
    "インフレ",
    "デフレ",
    "gdp",
    "消費者物価",
    "景気",
    "貿易収支",
    "失業率",
    "原油",
    "金価格",
    "商品先物",
    "仮想通貨",
    "ビットコイン",
    "暗号資産",
    "stocks",
    "markets",
    "bonds",
    "forex",
    "commodities",
    "oil",
    "gold",
    "central bank",
    "inflation",
    "interest rates",
    "earnings",
]

STOP_REQUESTED = False


def load_env_file(path=ENV_FILE):
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


def env_bool(value, default=False):
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


ENV_VALUES = load_env_file()

NEWS_WEBHOOK_URL = ENV_VALUES.get("NEWS_WEBHOOK_URL", "").strip()
DEV_LOG_WEBHOOK_URL = ENV_VALUES.get("DEV_LOG_WEBHOOK_URL", "").strip()
GOOGLE_NEWS_QUERY = ENV_VALUES.get("GOOGLE_NEWS_QUERY", DEFAULT_NEWS_QUERY).strip()
MAX_ARTICLES_PER_RUN = int(ENV_VALUES.get("MAX_ARTICLES_PER_RUN", "5"))
MAX_TOTAL_ARTICLES_PER_RUN = int(ENV_VALUES.get("MAX_TOTAL_ARTICLES_PER_RUN", "5"))
FETCH_INTERVAL_MINUTES = int(ENV_VALUES.get("FETCH_INTERVAL_MINUTES", "60"))
DRY_RUN = env_bool(ENV_VALUES.get("DRY_RUN", os.getenv("DRY_RUN")), default=False)
DEBUG_SELECTION = env_bool(
    ENV_VALUES.get("DEBUG_SELECTION", os.getenv("DEBUG_SELECTION")),
    default=False,
)
MARKET_SNAPSHOT_ENABLED = env_bool(
    ENV_VALUES.get("MARKET_SNAPSHOT_ENABLED", os.getenv("MARKET_SNAPSHOT_ENABLED")),
    default=True,
)
MARKET_SNAPSHOT_TIME = ENV_VALUES.get("MARKET_SNAPSHOT_TIME", "07:00").strip()

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
    return article.get("id") or article.get("guid") or article.get("link", "")


def is_finance_related(article):
    text = strip_html(f"{article.get('title', '')} {article.get('summary', '')}").lower()
    return any(keyword.lower() in text for keyword in FINANCE_KEYWORDS)


IMPORTANCE_KEYWORD_GROUPS = [
    (6, [
        "breaking", "urgent", "emergency", "crisis", "shock", "war", "attack",
        "sanctions", "default", "recession", "緊急", "速報", "危機", "戦争",
        "攻撃", "制裁", "デフォルト", "景気後退",
    ]),
    (5, [
        "frb", "federal reserve", "boj", "日銀", "日本銀行", "central bank",
        "policy rate", "interest rates", "rate hike", "rate cut", "金利",
        "政策金利", "利上げ", "利下げ", "中央銀行",
    ]),
    (5, [
        "cpi", "inflation", "jobs report", "unemployment", "gdp", "消費者物価",
        "インフレ", "雇用統計", "失業率",
    ]),
    (4, [
        "forex", "dollar", "yen", "usdjpy", "ドル円", "円安", "円高", "為替",
    ]),
    (4, [
        "oil", "crude", "gold", "commodity", "原油", "金", "金価格", "商品先物",
    ]),
    (4, [
        "stocks", "markets", "bonds", "yields", "株価", "日経平均", "topix",
        "株式市場", "債券", "利回り",
    ]),
    (3, [
        "earnings", "guidance", "profit warning", "決算", "業績", "増益",
        "減益", "下方修正",
    ]),
    (2, [
        "crypto", "bitcoin", "暗号資産", "仮想通貨", "ビットコイン",
    ]),
    (-4, [
        "pr", "sponsored", "advertisement", "promotion", "キャンペーン", "広告",
    ]),
    (-3, [
        "個人投資家", "投資術", "ランキング", "おすすめ", "初心者向け",
    ]),
    (-2, [
        "解説", "コラム", "opinion", "column",
    ]),
]


def keyword_matches(text, keyword):
    keyword_text = keyword.lower()
    if keyword_text == "pr":
        return re.search(r"\bpr\b", text) is not None
    return keyword_text in text


def importance_details(article):
    text = strip_html(f"{article.get('title', '')} {article.get('summary', '')}").lower()
    score = 0
    hits = []
    seen = set()

    for points, keywords in IMPORTANCE_KEYWORD_GROUPS:
        for keyword in keywords:
            if keyword_matches(text, keyword):
                score += points
                hit_key = keyword.lower()
                if hit_key not in seen:
                    hits.append(keyword)
                    seen.add(hit_key)

    return score, hits


def importance_score(article):
    score, _hits = importance_details(article)
    return score


def load_posted_ids(path=None):
    path = path or POSTED_IDS_FILE
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8") as posted_file:
            data = json.load(posted_file)
        if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
            raise ValueError("posted_ids.json must contain a list of strings")
        return data
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = path.with_name(f"{path.stem}.corrupt-{timestamp}{path.suffix}")
        try:
            shutil.move(str(path), str(backup_path))
            print(f"[WARN] Invalid posted ID file moved to: {backup_path.name}")
        except OSError as backup_exc:
            print(f"[WARN] Could not back up invalid posted ID file: {backup_exc}")
        print(f"[WARN] Starting with an empty posted ID list: {exc}")
        return []


def save_posted_ids(ids, path=None):
    path = path or POSTED_IDS_FILE
    unique_ids = list(dict.fromkeys(ids))[-MAX_SAVED_IDS:]
    temp_path = path.with_suffix(".json.tmp")
    with temp_path.open("w", encoding="utf-8") as posted_file:
        json.dump(unique_ids, posted_file, ensure_ascii=False, indent=2)
        posted_file.flush()
        os.fsync(posted_file.fileno())
    os.replace(temp_path, path)


def build_discord_message(article):
    """Build a Discord webhook payload for one RSS article."""
    summary = strip_html(article.get("summary", ""))
    description = summary[:300] + "..." if len(summary) > 300 else summary
    score, hits = importance_details(article)
    footer_text = (
        f"{article.get('_feed_name', 'RSS')} • "
        f"Score: {score} • "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    if hits:
        footer_text += f" • Hits: {', '.join(hits[:5])}"
    embed = {
        "title": article.get("title", "No title")[:256],
        "url": article.get("link", ""),
        "description": description,
        "color": 0xE8312A,
        "footer": {
            "text": footer_text[:2048]
        },
    }
    return {"embeds": [embed]}


def send_discord_webhook(webhook_url, payload, *, dry_run=DRY_RUN, session=requests):
    """Send a Discord webhook payload, or print it when dry-run is enabled."""
    if dry_run:
        title = payload.get("embeds", [{}])[0].get("title", "No title")
        print(f"[DRY_RUN] Would send Discord message: {title}")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return None

    if not webhook_url:
        raise RuntimeError("Discord webhook URL is not configured")

    response = session.post(
        webhook_url,
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response


def post_article(article, *, webhook_url=None, dry_run=DRY_RUN):
    webhook_url = NEWS_WEBHOOK_URL if webhook_url is None else webhook_url
    payload = build_discord_message(article)
    return send_discord_webhook(webhook_url, payload, dry_run=dry_run)


def parse_rss_entries(feed_name, rss_content):
    feed = feedparser.parse(rss_content)
    if getattr(feed, "bozo", False) and not feed.entries:
        raise RuntimeError(f"RSS parse failed: {feed.bozo_exception}")

    entries = list(feed.entries)
    for entry in entries:
        entry["_feed_name"] = feed_name
    return entries


def fetch_feed(feed_name, rss_url, *, session=requests):
    response = session.get(
        rss_url,
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": "DiscordMarketNewsFeedBot/1.0"},
    )
    response.raise_for_status()
    return parse_rss_entries(feed_name, response.content)


def published_sort_key(article):
    published = article.get("published_parsed") or article.get("updated_parsed")
    return time.mktime(published) if published else 0


SELECTION_STAT_KEYS = [
    "posted",
    "duplicate",
    "non_finance",
    "low_score",
    "per_feed_limit",
    "total_limit",
    "selected",
]


def empty_selection_stats():
    return {key: 0 for key in SELECTION_STAT_KEYS}


def add_selection_stats(total, partial):
    for key in SELECTION_STAT_KEYS:
        total[key] += partial.get(key, 0)


def format_selection_stats(stats):
    return " ".join(f"{key}={stats.get(key, 0)}" for key in SELECTION_STAT_KEYS)


def debug_selection_log(message):
    if DEBUG_SELECTION:
        print(f"[DEBUG] {message}")


def select_new_articles(
    entries,
    posted_ids,
    *,
    queued_ids=None,
    max_articles=MAX_ARTICLES_PER_RUN,
    return_stats=False,
):
    posted_id_set = set(posted_ids)
    queued_id_set = set(queued_ids or [])
    selected = []
    skipped = 0
    reason_stats = empty_selection_stats()

    for entry in entries:
        entry_id = article_id(entry)
        if not entry_id or entry_id in posted_id_set:
            reason_stats["posted"] += 1
            skipped += 1
            continue
        if entry_id in queued_id_set:
            reason_stats["duplicate"] += 1
            skipped += 1
            continue
        if not is_finance_related(entry):
            reason_stats["non_finance"] += 1
            skipped += 1
            continue
        if len(selected) >= max_articles:
            reason_stats["per_feed_limit"] += 1
            skipped += 1
            continue

        selected.append(entry)
        queued_id_set.add(entry_id)
        reason_stats["selected"] += 1

    if return_stats:
        return selected, skipped, reason_stats
    return selected, skipped


def rank_articles(articles, *, max_articles=MAX_TOTAL_ARTICLES_PER_RUN):
    ranked = sorted(
        articles,
        key=lambda article: (importance_score(article), published_sort_key(article)),
        reverse=True,
    )
    return ranked[:max_articles]


def build_messages_from_rss(feed_name, rss_content, posted_ids, *, max_articles=MAX_ARTICLES_PER_RUN):
    entries = parse_rss_entries(feed_name, rss_content)
    entries.sort(key=published_sort_key, reverse=True)
    selected, skipped = select_new_articles(entries, posted_ids, max_articles=max_articles)
    ranked = rank_articles(selected, max_articles=len(selected))
    messages = [build_discord_message(article) for article in ranked]
    return messages, ranked, skipped


def send_dev_log(stats, status="OK", detail="", *, dry_run=DRY_RUN):
    if not DEV_LOG_WEBHOOK_URL and not dry_run:
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
        send_discord_webhook(DEV_LOG_WEBHOOK_URL, {"embeds": [embed]}, dry_run=dry_run)
    except requests.RequestException as exc:
        print(f"[ERROR] Failed to post dev log: {exc}")


def post_market_snapshot(*, dry_run=DRY_RUN):
    if get_market_snapshot_message is None:
        print("[WARN] Skipping market snapshot: feature unavailable.")
        return

    try:
        message = get_market_snapshot_message(dry_run=dry_run)
    except Exception as exc:
        print(f"[ERROR] Failed to build market snapshot: {exc}")
        return

    try:
        send_discord_webhook(NEWS_WEBHOOK_URL, {"content": message[:2000]}, dry_run=dry_run)
        print("[OK] Posted market snapshot.")
    except requests.RequestException as exc:
        print(f"[ERROR] Failed to post market snapshot: {exc}")


def fetch_and_post(*, dry_run=DRY_RUN):
    stats = {"fetch": 0, "post": 0, "skip": 0, "error": 0}
    details = []
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Fetching RSS feed...")

    try:
        posted_ids = load_posted_ids()
        selected = []
        queued_ids = []
        selection_stats = empty_selection_stats()

        for feed_name, rss_url in RSS_FEEDS:
            if STOP_REQUESTED:
                details.append("Stop requested; remaining RSS feeds were skipped.")
                break
            try:
                feed_entries = fetch_feed(feed_name, rss_url)
                feed_entries.sort(key=published_sort_key, reverse=True)
                stats["fetch"] += len(feed_entries)
                print(f"[OK] Fetched {len(feed_entries)} article(s): {feed_name}")

                feed_selected, skipped, feed_selection_stats = select_new_articles(
                    feed_entries,
                    posted_ids,
                    queued_ids=queued_ids,
                    max_articles=MAX_ARTICLES_PER_RUN,
                    return_stats=True,
                )
                add_selection_stats(selection_stats, feed_selection_stats)
                stats["skip"] += skipped
                selected.extend(feed_selected)
                queued_ids.extend(article_id(article) for article in feed_selected)
                print(f"[INFO] Selected {len(feed_selected)} article(s): {feed_name}")
                debug_selection_log(
                    f"{feed_name}: {format_selection_stats(feed_selection_stats)}"
                )
            except Exception as exc:
                stats["error"] += 1
                details.append(f"Fetch failed: {feed_name} ({exc})")
                print(f"[ERROR] Failed to fetch {feed_name}: {exc}")

        ranked_selected = rank_articles(selected, max_articles=MAX_TOTAL_ARTICLES_PER_RUN)
        total_limit_count = len(selected) - len(ranked_selected)
        selection_stats["total_limit"] += total_limit_count
        selection_stats["selected"] = len(ranked_selected)
        stats["skip"] += total_limit_count
        selected = ranked_selected
        debug_selection_log(f"Selection stats: {format_selection_stats(selection_stats)}")

        for index, article in enumerate(selected):
            if STOP_REQUESTED:
                stats["skip"] += len(selected) - index
                details.append("Stop requested; remaining articles were skipped.")
                break

            try:
                post_article(article, dry_run=dry_run)
                entry_id = article_id(article)
                if not dry_run:
                    posted_ids.append(entry_id)
                stats["post"] += 1
                print(f"[OK] Posted: {article.get('title', '')[:60]}")
            except requests.RequestException as exc:
                stats["error"] += 1
                details.append(f"Post failed: {article.get('title', 'No title')[:100]} ({exc})")
                print(f"[ERROR] Failed to post article: {exc}")
            time.sleep(1)

        if dry_run:
            print("[DRY_RUN] posted_ids.json was not updated.")
        else:
            save_posted_ids(posted_ids)
    except Exception as exc:
        stats["error"] += 1
        details.append(str(exc))
        print(f"[ERROR] Run failed: {exc}")
    finally:
        status = "OK" if stats["error"] == 0 else "ERROR"
        send_dev_log(stats, status=status, detail="\n".join(details), dry_run=dry_run)
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


def validate_config(*, dry_run=DRY_RUN):
    errors = []
    if not dry_run and not ENV_FILE.exists():
        errors.append(".env file is missing")
    if not dry_run and not NEWS_WEBHOOK_URL:
        errors.append("NEWS_WEBHOOK_URL is not set in .env")
    if MAX_ARTICLES_PER_RUN < 1:
        errors.append("MAX_ARTICLES_PER_RUN must be at least 1")
    if MAX_TOTAL_ARTICLES_PER_RUN < 1:
        errors.append("MAX_TOTAL_ARTICLES_PER_RUN must be at least 1")
    if FETCH_INTERVAL_MINUTES < 1:
        errors.append("FETCH_INTERVAL_MINUTES must be at least 1")
    if errors:
        raise RuntimeError("; ".join(errors))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Post market news RSS entries to Discord.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print Discord payloads without sending webhooks or updating posted_ids.json.",
    )
    parser.add_argument(
        "--send-market-snapshot-now",
        action="store_true",
        help="Send one Market Snapshot immediately and exit without running the news feed loop.",
    )
    return parser.parse_args(argv)


def is_dry_run_requested(argv=None):
    return DRY_RUN or parse_args(argv).dry_run


def main(argv=None):
    args = parse_args(argv)
    dry_run = DRY_RUN or args.dry_run

    if args.send_market_snapshot_now:
        validate_config(dry_run=dry_run)
        print("=== Discord Market Snapshot Manual Send ===")
        print(f"Dry run: {dry_run}")
        post_market_snapshot(dry_run=dry_run)
        return

    signal.signal(signal.SIGINT, request_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_shutdown)

    validate_config(dry_run=dry_run)
    print("=== Discord Market News Feed Bot ===")
    print(f"Query: {GOOGLE_NEWS_QUERY}")
    print(f"RSS feeds: {len(RSS_FEEDS)}")
    print(f"Max total articles: {MAX_TOTAL_ARTICLES_PER_RUN}")
    print(f"Interval: Every {FETCH_INTERVAL_MINUTES} minute(s)")
    print(f"Dry run: {dry_run}")
    print("Starting...\n")

    fetch_and_post(dry_run=dry_run)
    schedule.every(FETCH_INTERVAL_MINUTES).minutes.do(fetch_and_post, dry_run=dry_run)

    if MARKET_SNAPSHOT_ENABLED and get_market_snapshot_message is not None:
        schedule.every().day.at(MARKET_SNAPSHOT_TIME).do(post_market_snapshot, dry_run=dry_run)
        print(f"Market snapshot: daily at {MARKET_SNAPSHOT_TIME}")

    while not STOP_REQUESTED:
        schedule.run_pending()
        time.sleep(1)

    print("[INFO] Bot stopped safely.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        dry_run = is_dry_run_requested(sys.argv[1:])
        print(f"[FATAL] {exc}")
        send_dev_log(
            {"fetch": 0, "post": 0, "skip": 0, "error": 1},
            status="FATAL",
            detail=str(exc),
            dry_run=dry_run,
        )
        raise SystemExit(1)
