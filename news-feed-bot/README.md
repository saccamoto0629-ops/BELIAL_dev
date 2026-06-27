# Discord Market News Feed Bot

市場関連ニュースを複数のRSSから取得し、Discordへ投稿するPythonボットです。

## 取得元

- Google News（Reuters市場監視クエリ）
- Yahoo!ニュース「経済」
- Yahoo!ニュース「IT」
- Yahoo!ニュース「国内」
- Yahoo!ニュース「国際」

各RSSから新着を最大5件ずつ選び、公開日時順にまとめて投稿します。既定設定では、
1サイクルにつき最大25件です。RSS間で同じ記事IDが見つかった場合は重複を除外します。
1つのRSS取得に失敗しても、残りのRSS処理は継続します。

## セットアップ

```powershell
pip install feedparser requests schedule
Copy-Item .env.example .env
notepad .env
```

`.env.example` のWebhook値はダミーです。実際のWebhook URLは `.env` にだけ記載してください。

実行:

```powershell
python .\news_feed_bot.py
```

停止は `Ctrl+C` です。

## 主な設定

| 設定 | 既定値 | 説明 |
|---|---:|---|
| `NEWS_WEBHOOK_URL` | なし | ニュース投稿先。必須 |
| `DEV_LOG_WEBHOOK_URL` | なし | 実行集計の投稿先 |
| `GOOGLE_NEWS_QUERY` | 市場監視用検索 | Google News検索条件 |
| `MAX_ARTICLES_PER_RUN` | `5` | 各RSSから1サイクルに投稿する最大件数 |
| `FETCH_INTERVAL_MINUTES` | `60` | RSS確認間隔 |

## セキュリティ

- Webhook設定はスクリプトと同じフォルダの `.env` からのみ読み込みます。
- `.env` は `.gitignore` の対象です。Gitへ追加しないでください。
- `.env.example` には実URL、トークン、秘密情報を記載しないでください。

`posted_ids.json` は投稿済み記事の重複排除に使用します。破損時には退避して自動復旧します。
