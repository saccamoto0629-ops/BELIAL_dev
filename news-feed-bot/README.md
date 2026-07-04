# Discord Market News Feed Bot

RSSから金融・市場関連ニュースを取得し、重要度スコア順に並べてDiscord Webhookへ投稿するPythonボットです。

The bot fetches finance/market-related RSS articles, ranks them by importance score, and posts the highest-priority articles to Discord.

## 1. Local setup

```powershell
cd C:\Users\kinniki\ClaudeLocalSession\opt\BELIAL_dev
python -m pip install -r requirements.txt
```

## 2. Dry run

実Webhookを使わずに確認する場合は、`--dry-run` を使います。

```powershell
cd C:\Users\kinniki\ClaudeLocalSession\opt\BELIAL_dev
python .\news-feed-bot\news_feed_bot.py --dry-run
```

Dry-runでは以下を行いません。

- Discord WebhookへのHTTP送信
- `posted_ids.json` の更新

送信予定のpayloadだけをコンソールへ出力します。

## 3. Production run

本番実行では `.env` の `NEWS_WEBHOOK_URL` を使ってDiscordへ送信します。

```powershell
cd C:\Users\kinniki\ClaudeLocalSession\opt\BELIAL_dev
python .\news-feed-bot\news_feed_bot.py
```

`.env.example` を `.env` にコピーし、少なくとも以下を設定してください。

```env
NEWS_WEBHOOK_URL=<your Discord webhook URL>
DRY_RUN=false
MAX_ARTICLES_PER_RUN=5
MAX_TOTAL_ARTICLES_PER_RUN=5
```

本物の `.env`、Webhook URL、APIキー、tokenはGitにコミットしないでください。

## 4. Article selection

記事選択は以下の流れで行います。

1. 設定されたRSS feedを取得する
2. `posted_ids.json` に存在する記事を除外する
3. 同一実行内で重複する記事IDを除外する
4. 金融・市場関連キーワードに一致しない記事を除外する
5. 各RSS feedから最大 `MAX_ARTICLES_PER_RUN` 件を候補にする
6. 候補記事に `importance_score(article)` を付ける
7. scoreが高い順に並べる
8. scoreが同じ場合は公開日時が新しい順にする
9. 全体で最大 `MAX_TOTAL_ARTICLES_PER_RUN` 件だけ投稿する

### Finance filter

記事タイトルとsummaryをHTML除去後にチェックし、金融・市場関連キーワードに一致しない記事は除外します。

このfilterは本番でも有効です。

### Per-feed candidate limit

`MAX_ARTICLES_PER_RUN` は、RSS feedごとの候補記事数上限です。

例: RSS feedが5個あり、`MAX_ARTICLES_PER_RUN=5` の場合、最大25件が候補になります。

### Global post limit

`MAX_TOTAL_ARTICLES_PER_RUN` は、1回の実行全体で実際に投稿する最大件数です。

例: `MAX_TOTAL_ARTICLES_PER_RUN=5` の場合、候補が25件あっても投稿されるのは上位5件だけです。

Discord embedのfooterには `Score: N` と、最大5個のhit keywordsが表示されます。

### Selection debug logs

`DEBUG_SELECTION=true` にすると、記事が選ばれなかった理由をfeedごと・全体で表示します。

Example:

```text
[DEBUG] Google News / Reuters: posted=20 duplicate=1 non_finance=70 low_score=0 per_feed_limit=5 total_limit=0 selected=0
[DEBUG] Selection stats: posted=80 duplicate=3 non_finance=210 low_score=0 per_feed_limit=5 total_limit=10 selected=5
```

Reasons:

- `posted`: already exists in `posted_ids.json`
- `duplicate`: already selected as a candidate in the same run
- `non_finance`: did not match the finance filter
- `low_score`: below score threshold, currently always `0` because no score threshold is implemented
- `per_feed_limit`: dropped by `MAX_ARTICLES_PER_RUN`
- `total_limit`: dropped by `MAX_TOTAL_ARTICLES_PER_RUN`
- `selected`: feed candidate count in feed logs, final post target count in the overall log

Default is `false`. Keeping this enabled in production can produce a lot of logs.

## 5. Importance score overview

`importance_score(article)` は、記事タイトルとsummaryをHTML除去したテキストをもとに計算します。

### Positive signals

主な加点対象:

- breaking, urgent, emergency, crisis, shock, war, attack, sanctions, default, recession
- 緊急, 速報, 危機, 戦争, 攻撃, 制裁, デフォルト, 景気後退
- FRB, Federal Reserve, BOJ, central bank, policy rate, interest rates, rate hike, rate cut
- 日銀, 日本銀行, 金利, 政策金利, 利上げ, 利下げ, 中央銀行
- CPI, inflation, jobs report, unemployment, GDP
- 消費者物価, インフレ, 雇用統計, 失業率, GDP
- forex, dollar, yen, USDJPY, ドル円, 円安, 円高, 為替
- oil, crude, gold, commodity, 原油, 金, 金価格, 商品先物
- stocks, markets, bonds, yields, 株価, 日経平均, TOPIX, 株式市場, 債券, 利回り
- earnings, guidance, profit warning, 決算, 業績, 増益, 減益, 下方修正
- crypto, bitcoin, 暗号資産, 仮想通貨, ビットコイン

### Negative signals

主な減点対象:

- PR, sponsored, advertisement, promotion
- キャンペーン, 広告
- 個人投資家, 投資術, ランキング, おすすめ, 初心者向け
- 解説, コラム, opinion, column

## 6. Settings

| Setting | Default | Description |
|---|---:|---|
| `NEWS_WEBHOOK_URL` | empty | Discord news webhook URL. Required for production posting. |
| `DEV_LOG_WEBHOOK_URL` | empty | Optional webhook for run summaries. |
| `DRY_RUN` | `false` | When `true`, prints only and does not send or update `posted_ids.json`. |
| `DEBUG_SELECTION` | `false` | When `true`, prints selection reason counts for each feed and the whole run. |
| `GOOGLE_NEWS_QUERY` | market query | Google News search query. |
| `MAX_ARTICLES_PER_RUN` | `5` | Per-feed candidate limit before global scoring. |
| `MAX_TOTAL_ARTICLES_PER_RUN` | `5` | Overall post limit for one run after scoring all candidates. |
| `FETCH_INTERVAL_MINUTES` | `60` | RSS polling interval. |

## 7. Tests

```powershell
cd C:\Users\kinniki\ClaudeLocalSession\opt\BELIAL_dev
python -m pytest news-feed-bot --basetemp=.tmp_pytest -p no:cacheprovider
```

Tests use fixtures and fake sessions, so they do not require a real Discord Webhook.

テストでは主に以下を検証します。

- RSS取得結果からDiscord payloadを作れること
- `posted_ids` に存在する記事を投稿しないこと
- `posted_ids` にない記事だけ送信対象になること
- dry-runではDiscord送信しないこと
- dry-runでは `posted_ids.json` を更新しないこと
- importance scoreで重要記事が優先されること
- `MAX_TOTAL_ARTICLES_PER_RUN` で全体投稿数が制限されること

## 8. Market snapshot

`market_snapshot.py` は、ニュースとは別に市場スナップショットをDiscordへ投稿するための機能です。

```powershell
cd C:\Users\kinniki\ClaudeLocalSession\opt\BELIAL_dev\news-feed-bot
python market_snapshot.py
```

コマンドラインで直接実行すると、Discordへは投稿せずスナップショットの内容をコンソールへ表示します。

### Data sources

| File | Source | Content |
|---|---|---|
| `market_data.py` | Yahoo Finance (`yfinance`) | VIX, S&P500, Nasdaq, USDJPY, Gold の直近終値・前日比 |
| `fred_data.py` | FRED API | US10Y, US2Y, 10Y-2Y spread, Effective Fed Funds Rate |
| `tradingview_data.py` | `tradingview_ta` | USDJPY, XAUUSD, SPX, VIX のRECOMMENDATION・RSI・MACD・EMA |
| `market_snapshot.py` | 上記3つを統合 | Discord投稿用Markdownメッセージと `[Risk Label]` を生成 |

各データ取得は個別に例外を処理するため、いずれかが失敗・タイムアウトしてもそのセクションだけ `unavailable` になり、bot全体は継続します。`FRED_API_KEY` が未設定の場合は `[Rates / FRED]` セクション全体がスキップされます。

### Risk Label rules

- VIX > 25、またはVIX前日比 +15%以上 → `Risk Off`
- VIX < 18 かつ S&P500・Nasdaqがともにプラス → `Risk On`
- US10Yが前日比 +0.05(pt)以上、かつS&P500かNasdaqのいずれかがマイナス → `Watch`
- それ以外 → `Neutral`

### Scheduling

`news_feed_bot.py` から `market_snapshot.get_market_snapshot_message()` を呼び出し、`MARKET_SNAPSHOT_TIME`(デフォルト `07:00`)に毎日1回Discordへ投稿します。`MARKET_SNAPSHOT_ENABLED=false` にすると無効化できます。市場データ用のライブラリ(`yfinance` / `tradingview-ta`)が読み込めない環境でも、警告を出すだけでニュース配信自体は継続します。

| Setting | Default | Description |
|---|---:|---|
| `FRED_API_KEY` | empty | FREDの金利・マクロ指標取得用APIキー。未設定なら該当セクションのみスキップ。 |
| `MARKET_SNAPSHOT_ENABLED` | `true` | 市場スナップショットの定期投稿を有効にするか。 |
| `MARKET_SNAPSHOT_TIME` | `07:00` | 市場スナップショットを投稿する時刻(ローカル時刻、`HH:MM`)。 |

## 9. Security notes

- `.env` をコミットしない
- real webhook URL、API key、tokenをREADMEや`.env.example`に書かない
- real secretsはEC2など実行環境の `.env` だけに置く
- `posted_ids.json` は重複投稿防止用のruntime file
- `posted_ids.json` はコミットしない
