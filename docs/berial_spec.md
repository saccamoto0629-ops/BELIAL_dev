
---

## 25. 現行 Market Snapshot 仕様

この章は、Berial が RSSニュース監視Botから、マーケットデータ監視機能を持つBotへ拡張された現行実装を反映する。古い構想章と差異がある場合、現行の `news-feed-bot/` 配下の実装については本章を優先する。

### 25.1 システム概要

現行の Berial は、以下の2つの監視レイヤーを持つ。

- RSSニュース監視
  - Google News / Reuters と Yahoo!ニュース系RSSを取得する
  - 金融・市場関連ニュースをフィルタし、重要度順にDiscordへ通知する
- Market Snapshot
  - Yahoo Finance / FRED / TradingView TA から市場データを取得する
  - FREDのマクロ指標、TradingViewのテクニカル指標、Yahoo Financeの市場価格をまとめる
  - 簡易 `Risk Label` を算出し、Discord投稿本文として整形する

現時点では、AI分析・売買判断・自動発注は行わない。Market Snapshot は、BELIALの将来のAI判断層へ渡す前段の「市場状態の可視化」機能である。

### 25.2 ディレクトリ構成

現行の主要ファイルは以下の通り。

```text
news-feed-bot/
├─ news_feed_bot.py          # RSSニュース監視、Discord投稿、定期実行
├─ market_snapshot.py        # Market Snapshot本文生成、Provider統合、Risk Label
├─ market_data.py            # Yahoo Finance / yfinance Provider
├─ fred_data.py              # FRED API Provider
├─ tradingview_data.py       # TradingView TA Provider
├─ economic_calendar.py      # Economic Calendar Provider枠
├─ fedwatch.py               # FedWatch Provider枠
└─ tests/                    # pytest
```

### 25.3 データ取得Provider

| Provider | 実装ファイル | 役割 | 失敗時 |
|---|---|---|---|
| Yahoo Finance | `market_data.py` | 市場価格・指数・商品価格を取得 | 個別シンボルを `None` |
| FRED | `fred_data.py` | 金利・マクロ指標を公式APIから取得 | APIキー未設定時はFREDセクションをスキップ |
| TradingView | `tradingview_data.py` | テクニカル指標を取得 | 個別シンボルを `None` |
| Economic Calendar | `economic_calendar.py` | 経済イベント取得のProvider枠 | `[]`、本文は unavailable |
| FedWatch | `fedwatch.py` | 金利確率取得のProvider枠 | `None`、本文は unavailable |

#### Yahoo Finance

`yfinance` を利用する。対象は以下。

- VIX
- S&P500
- Nasdaq
- USDJPY
- Dollar Index
- Gold
- Oil
- VVIX

`^TNX` はUS10Yの簡易補助として保持しているが、金利情報はFREDを優先する。

#### FRED

FREDは公式APIであり、現行Market Snapshotでは信頼Providerとして扱う。対象は以下。

- US10Y
- US2Y
- 10Y-2Y Spread
- EFFR / DFF
- CPI
- PCE
- Unemployment

`FEDFUNDS` は追加しない。Market Snapshot用途では日次の `DFF` を優先する。各FRED系列は最新値だけでなく、FRED観測値の日付も保持し、Discord表示では `as of YYYY-MM-DD` を付ける。

例:

```text
- US10Y: 4.48% as of 2026-07-03
- US2Y: 4.17% as of 2026-07-03
- 10Y-2Y Spread: 0.35% as of 2026-07-03
- EFFR / DFF: 3.63% as of 2026-07-01
```

#### TradingView

`tradingview_ta` を利用する。対象は以下。

- USDJPY
- XAUUSD
- SPX
- VIX

取得・表示対象は以下。

- Recommendation
- RSI
- MACD
- EMA20 / EMA50 / EMA200
- ADX
- STOCH K / STOCH D

#### Economic Calendar

現状はProvider枠として実装している。外部API・スクレイピング元が不安定なため、必須機能にはしない。`ENABLE_ECONOMIC_CALENDAR=false` が既定であり、有効化された場合だけ取得を試みる。失敗時は `unavailable` として継続する。

#### FedWatch

現状はProvider枠として実装している。CME / Investing.com は timeout や 403 Forbidden になる可能性があるため、必須機能にはしない。`ENABLE_FEDWATCH=false` が既定であり、有効化された場合だけ取得を試みる。失敗時は `unavailable` として継続する。

### 25.4 Market Snapshot仕様

`market_snapshot.py` は各Providerを並列に取得し、Discord投稿用のプレーンMarkdown本文を生成する。1つのProviderが失敗してもBot全体は停止しない。

主な表示セクション:

- `Market Snapshot`
- `VIX`
- `US Index`
- `FX / Commodity`
- `Rates / FRED`
- `Technical / TradingView`
- `Today's Events`
- `FedWatch`
- `Risk Label`

`news_feed_bot.py` からは `MARKET_SNAPSHOT_ENABLED=true` のとき、`MARKET_SNAPSHOT_TIME` に `market_snapshot.get_market_snapshot_message()` を呼び出してDiscordへ投稿する。

### 25.5 Risk Label仕様

`compute_risk_label()` は以下の簡易ルールで `Risk Off` / `Risk On` / `Watch` / `Neutral` を返す。

| Label | 条件 |
|---|---|
| Risk Off | VIX > 25、VVIX > 100、またはVIX前日比 +15%以上 |
| Risk On | VIX < 18 かつ S&P500 / Nasdaq が上昇 |
| Watch | US10Y上昇、Dollar Index上昇、または株指数下落 |
| Neutral | 上記以外 |

これはルールベースの補助ラベルであり、AI判定や売買判断ではない。

### 25.6 環境変数

現行実装の主な環境変数は以下。

| 変数 | 既定 | 用途 |
|---|---:|---|
| `NEWS_WEBHOOK_URL` | empty | Discordニュース投稿先Webhook。現行実装で使用する変数 |
| `DEV_LOG_WEBHOOK_URL` | empty | 実行サマリ投稿先Webhook |
| `DRY_RUN` | `true` in `.env.example` | Discord送信と `posted_ids.json` 更新を止める安全モード |
| `DEBUG_SELECTION` | `false` | RSS記事選別理由のdebugログ |
| `GOOGLE_NEWS_QUERY` | market query | Google News RSS検索条件 |
| `MAX_ARTICLES_PER_RUN` | `5` | RSS feedごとの候補取得上限 |
| `MAX_TOTAL_ARTICLES_PER_RUN` | `5` | 1回の実行全体の投稿上限 |
| `FETCH_INTERVAL_MINUTES` | `60` | RSS取得間隔 |
| `FRED_API_KEY` | empty | FRED API用キー。未設定ならFREDセクションをスキップ |
| `MARKET_SNAPSHOT_ENABLED` | `true` | Market Snapshot定期投稿を有効化 |
| `MARKET_SNAPSHOT_TIME` | `07:00` | Market Snapshot投稿時刻 |
| `ENABLE_FEDWATCH` | `false` | FedWatch optional Providerを有効化 |
| `ENABLE_ECONOMIC_CALENDAR` | `false` | Economic Calendar optional Providerを有効化 |

注意: ユーザー向けに `DISCORD_WEBHOOK_URL` と呼ぶ場合があるが、現行コードで使用している実変数名は `NEWS_WEBHOOK_URL` である。実装と `.env.example` を変更しない限り、`DISCORD_WEBHOOK_URL` は有効な設定名ではない。

`.env` はGit管理しない。本物のWebhook URL、API key、tokenは `.env.example` や設計書に書かない。

### 25.7 テスト方針

pytestで以下を検証している。

- RSS取得結果からDiscord payloadを作れること
- posted_idsによる既投稿除外
- 金融ニュースフィルタ
- importance scoreと全体投稿上限
- dry-run時にDiscord送信・posted_ids更新をしないこと
- Market Snapshot本文生成
- Yahoo Finance Provider
- FRED Provider
- TradingView Provider
- Risk Label
- Economic Calendar Providerの正常系・異常系
- FedWatch Providerの正常系・異常系
- optional Providerが既定falseで呼ばれないこと

現時点でpytestは55件以上が通過している。外部APIアクセスはテストではMock化し、実Webhookや本物のAPIキーは使わない。

### 25.8 今後の拡張予定

- `MIN_IMPORTANCE_SCORE` による低スコアニュースの足切り
- RSSソース追加、Google News query調整
- Market Snapshotのsystemd化またはLambda化
- `market_state.py` による市場状態分類
- AI要約、ブル/ベア判定、市場インパクト判定
- Fear & Greed
- AI Risk Score
- Polymarket
- COT Report

FedWatch / Economic Calendar は外部取得元が不安定なため、今後も必須Providerではなく optional Provider として扱う。

---

## 26. Berial v1.2.2 Market Watch / MACD Alert Bot

v1.2.2 は、`market-watch/macd_alert_bot.py` を継続運用しやすい監視モジュールとして整備するリリースである。v1.2系は MACD クロス通知Botの安定運用フェーズであり、市場判断ロジックは追加しない。

### 26.1 目的

- USD/JPY M15 の MACD(12,26,9) を監視する
- 確定足ベースで bullish / bearish cross を検知する
- Discord Webhookへ通知する
- 再起動後も同じ足・同じsignalを重複通知しない
- 運用ログと設定ファイルを整備する

このBotは「シグナル検知・通知」までを担当する。売買判断、ブル/ベア判定、市場状態分類、エントリー判断は行わない。

### 26.2 追加・更新された機能

| 項目 | 内容 |
|---|---|
| 設定外部化 | 公開可能な監視設定を `market-watch/config.toml` に分離 |
| 秘匿情報管理 | `DISCORD_WEBHOOK_URL` などのsecretは `.env` に残す |
| state管理 | `market-watch/state/macd_alert_state.json` に最後に通知した candle time と signal を保存 |
| 通知改善 | symbol、timeframe、signal種別、golden/dead cross、価格、MACD、signal line、検知時刻、説明文を含める |
| ログ強化 | `logs/macd_alert_bot.log` に起動、監視対象、取得結果、cross検知、Discord通知結果、例外を記録 |
| エラー処理 | データ取得失敗やDiscord通知失敗はログに残し、継続運用できる形にする |

### 26.3 起動方式

Linux運用パスは `/opt/berial` を想定する。

```bash
cd /opt/berial
python -m pip install -r requirements.txt
cp market-watch/.env.example market-watch/.env
python market-watch/macd_alert_bot.py --once --dry-run
```

本番で継続監視する場合:

```bash
cd /opt/berial
python market-watch/macd_alert_bot.py
```

`--dry-run` ではDiscord送信もstate更新も行わない。

systemd化は v1.4 以降に回す。v1.2.2 ではPythonプロセスとして起動できる状態に留める。

### 26.4 監視対象

現行の既定値は以下。

| 項目 | 値 |
|---|---|
| symbol | `USD_JPY` |
| ticker | `JPY=X` |
| timeframe | `M15` |
| interval | `15m` |
| period | `5d` |
| MACD | `12,26,9` |
| check interval | `60s` |

設定は `market-watch/config.toml` で変更する。

### 26.5 state管理

重複通知防止のstateは以下に保存する。

```text
market-watch/state/macd_alert_state.json
```

保存内容は `symbol + timeframe` 単位で、以下を含む。

- `candle_time`
- `signal`
- `price`
- `macd`
- `signal_line`
- `notified_at`

旧v1.2.1の `market-watch/macd_alert_state.json` が残っている場合も、初回読み込み時の互換性を持たせる。ただし新規保存先は `market-watch/state/` である。

`state/`、`.env`、`__pycache__/`、`.pytest_cache/`、`logs/` はGit管理しない。

### 26.6 v1.3 / v1.4 以降

v1.3では、市場判断ロジックの追加を検討する。

候補:

- Market Snapshotとの連携
- Risk Labelとの連携
- AI要約・市場インパクト判定
- MACD以外の補助指標

ただし v1.2.2 ではこれらを実装しない。

v1.4以降で検討するもの:

- systemd unit
- restart policy
- logrotate
- production deploy手順
---
