
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

## 26. 現行 MACDクロス通知Bot 仕様 v1.2.1

この章は、`market-watch/` 配下に追加した MACDクロス通知Bot の現行仕様を反映する。

v1.2.1 では、Berial はニュース監視・Market Snapshot に加えて、テクニカル指標の変化を監視する `market-watch` レイヤーを持つ。  
現時点では、MACDクロスを検知してDiscordへ通知するのみであり、自動売買・自動発注・売買推奨は行わない。

### 26.1 目的

`market-watch` は、チャート特徴量監視の最初の実装である。

v1.2.1 の目的は以下。

- USD/JPY の15分足を監視する
- MACD(12,26,9) を計算する
- 確定足ベースでMACDクロスを検知する
- Bullish cross / Bearish cross をDiscordへ通知する
- 同一足での重複通知を防止する
- ニュース監視Botとは責務を分離する
- 将来の市場状態分類・エントリーBot連携の前段にする

### 26.2 ディレクトリ構成

v1.2.1 時点の主要ファイルは以下。

```text
market-watch/
├─ .env.example
├─ README.md
├─ macd_alert_bot.py
└─ tests/
   └─ test_macd_alert_bot.py
```

実行時には、以下のruntime fileが生成される場合がある。

```text
market-watch/macd_alert_state.json
```

`macd_alert_state.json` は重複通知防止用の状態ファイルであり、Git管理しない。

### 26.3 news-feed-bot との責務分離

`news-feed-bot` と `market-watch` は責務を分ける。

| モジュール | 役割 |
|---|---|
| `news-feed-bot` | RSSニュース監視、Market Snapshot投稿、ニュース重要度スコアリング |
| `market-watch` | チャート特徴量監視、MACDクロス検知、テクニカルアラート通知 |

v1.2.1 では、`market-watch/macd_alert_bot.py` は既存の `news-feed-bot/news_feed_bot.py` を変更せずに追加する。

### 26.4 データ取得

v1.2.1 の初期実装では、ローソク足データ取得に `yfinance` を使用する。

初期監視対象:

| 項目 | 値 |
|---|---|
| 通貨ペア | USD/JPY |
| Symbol | `USDJPY=X` |
| 時間足 | `15m` |
| 監視間隔 | 60秒 |

将来的には、OANDA API / MT5 / XM 連携への置き換えを検討する。  
ただし、現時点では発注連携は行わない。

### 26.5 MACD設定

MACD設定は以下。

| パラメータ | 値 |
|---|---:|
| Fast EMA | 12 |
| Slow EMA | 26 |
| Signal EMA | 9 |

MACDラインは Fast EMA と Slow EMA の差分である。  
Signalラインは MACDラインのEMAである。

### 26.6 クロス判定

クロス判定は、前回の確定足と今回の確定足のMACD / Signalの位置関係で行う。

Bullish cross:

```text
prev_macd <= prev_signal
and
curr_macd > curr_signal
```

Bearish cross:

```text
prev_macd >= prev_signal
and
curr_macd < curr_signal
```

未確定足は判定対象にしない。  
これにより、足確定前に一時的に発生して消える「幻のクロス」を通知しない。

### 26.7 重複通知防止

同一足で同じクロスを複数回通知しないよう、状態ファイルを使用する。

```text
market-watch/macd_alert_state.json
```

状態ファイルには、最後に通知した足の時刻やクロス種別を保存する。

dry-run時は、状態ファイルを更新しない。  
これにより、検証実行を繰り返しても本番通知状態に影響しない。

### 26.8 Discord通知仕様

MACDクロスを検知した場合、Discord Webhookへ通知する。

通知内容の例。

```text
Berial MACD Alert

Symbol: USDJPY=X
Timeframe: 15m
Signal: Bullish Cross
Candle Time: 2026-07-04 12:45 JST
Close: 160.12
MACD: 0.0123
Signal: 0.0087
```

通知は市場状態の変化候補を知らせるものであり、売買推奨ではない。

### 26.9 環境変数

`market-watch/.env` に設定する。  
`.env` はGit管理しない。

```env
DISCORD_WEBHOOK_URL=
SYMBOL=USDJPY=X
TIMEFRAME=15m
MACD_FAST=12
MACD_SLOW=26
MACD_SIGNAL=9
CHECK_INTERVAL_SECONDS=60
```

`.env.example` には本物のWebhook URLを書かない。

本番用 `.env` はEC2上にのみ配置する。  
Windowsローカル、GitHub、設計書には実値を書かない。

### 26.10 実行方法

単発dry-run:

```bash
cd /opt/berial
python3 market-watch/macd_alert_bot.py --once --dry-run
```

単発本番実行:

```bash
cd /opt/berial
python3 market-watch/macd_alert_bot.py --once
```

通常実行:

```bash
cd /opt/berial
python3 market-watch/macd_alert_bot.py
```

nohupによる常駐実行:

```bash
cd /opt/berial
nohup python3 market-watch/macd_alert_bot.py > logs/macd_alert_bot.log 2>&1 &
```

プロセス確認:

```bash
ps aux | grep macd_alert_bot
```

ログ確認:

```bash
tail -n 50 logs/macd_alert_bot.log
```

停止:

```bash
pkill -f macd_alert_bot.py
```

### 26.11 テスト

pytestで `market-watch` のテストを実行する。

```bash
cd /opt/berial
python3 -m pytest market-watch/tests -q
```

主な検証観点:

- MACD計算が実行できること
- Bullish crossを検知できること
- Bearish crossを検知できること
- クロスしていない場合は通知対象にならないこと
- 同一足で重複通知しないこと
- dry-run時にDiscord送信しないこと
- dry-run時に状態ファイルを更新しないこと
- `.env.example` と実装の設定項目が一致していること

### 26.12 依存関係

v1.2.1 で追加・利用する主な依存関係は以下。

```text
pandas
yfinance
requests
pytest
```

EC2では、仮想環境を有効化した状態で以下を実行する。

```bash
cd /opt/berial
python3 -m pip install -r requirements.txt
```

`ModuleNotFoundError: No module named 'pandas'` が出た場合は、仮想環境に依存関係が入っていない可能性が高い。

### 26.13 運用状態

v1.2.1 時点では、EC2上で以下の2プロセスを起動する運用とする。

```text
python3 market-watch/macd_alert_bot.py
python3 news-feed-bot/news_feed_bot.py
```

`nohup` で起動した場合、TeraTermを閉じてもプロセスは継続する。

確認例:

```bash
ps aux | grep -E 'macd_alert_bot|news_feed_bot'
```

### 26.14 現在の制約

- `yfinance` のデータ取得に依存する
- 15分足の取得可用性は外部サービス側に依存する
- MACDクロス単体ではダマシが多い
- クロス通知は売買判断ではない
- 経済指標・ニュース・上位足トレンドはまだ統合していない
- 複数通貨ペア監視は未対応
- systemdによる常駐管理は未実装

### 26.15 今後の拡張候補

- OANDA APIによるローソク足取得
- 複数通貨ペア監視
- 複数時間足監視
- 上位足トレンドフィルタ
- RSI / ADX / 移動平均線との複合判定
- Market Snapshotとの統合
- ニュース重要度とテクニカルアラートの統合
- `#market-alerts` チャンネルの新設
- systemd化
- Discord通知文のBerial人格化
- エントリーBotの前段シグナルとして利用
- クロス後の成績記録とバックテスト
