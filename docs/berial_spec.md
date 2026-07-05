---

## 25. 現行 Market Snapshot 仕様

この章は、Berial が RSSニュース監視Botから、マーケットデータ監視機能を持つBotへ拡張された現行実装を反映する。古い構想章と差異がある場合、現行の `news-feed-bot/` 配下の実装については本章を優先する。

### 25.1 システム概要

現行の Berial は、以下の3つの監視レイヤーを持つ。

- RSSニュース監視
  - Google News / Reuters と Yahoo!ニュース系RSSを取得する
  - 金融・市場関連ニュースをフィルタし、重要度順にDiscordへ通知する
- Market Snapshot
  - Yahoo Finance / FRED / TradingView TA から市場データを取得する
  - FREDのマクロ指標、TradingViewのテクニカル指標、Yahoo Financeの市場価格をまとめる
  - 簡易 `Risk Label` を算出し、Discord投稿本文として整形する
- Market Watch
  - `market-watch/` 配下でローソク足を取得し、SMA / EMA / Bollinger Bands / MACDを計算する
  - Chart Snapshotを生成し、Market Snapshotの `Chart Snapshot Summary` と Market Bias に連携する
  - MACDクロス発生時は `macd_alert_bot.py` がイベント通知する

現時点では、AI分析・売買判断・自動発注は行わない。Market Snapshot と Market Watch は、BELIALの将来のAI判断層へ渡す前段の「市場状態の可視化」機能である。

### 25.2 ディレクトリ構成

現行の主要ファイルは以下の通り。

```text
news-feed-bot/
├─ news_feed_bot.py          # RSSニュース監視、Discord投稿、定期実行
├─ market_snapshot.py        # Market Snapshot本文生成、Provider統合、Risk Label、Chart Summary統合
├─ market_bias.py            # Bull / Bear / Neutral のMarket Bias Engine
├─ market_data.py            # Yahoo Finance / yfinance Provider
├─ fred_data.py              # FRED API Provider
├─ tradingview_data.py       # TradingView TA Provider
├─ economic_calendar.py      # Economic Calendar Provider枠
├─ fedwatch.py               # FedWatch Provider枠
└─ tests/                    # pytest
```

`market-watch/` 配下の構成は第26章に記載する。

### 25.3 データ取得Provider

| Provider | 実装ファイル | 役割 | 失敗時 |
|---|---|---|---|
| Yahoo Finance | `market_data.py` | 市場価格・指数・商品価格を取得 | 個別シンボルを `None` |
| FRED | `fred_data.py` | 金利・マクロ指標を公式APIから取得 | APIキー未設定時はFREDセクションをスキップ |
| TradingView | `tradingview_data.py` | テクニカル指標を取得 | 個別シンボルを `None` |
| Economic Calendar | `economic_calendar.py` | 経済イベント取得のProvider枠 | `[]`、本文は unavailable |
| FedWatch | `fedwatch.py` | 金利確率取得のProvider枠 | `None`、本文は unavailable |
| Chart Snapshot | `../market-watch/chart_snapshot.py` | ローソク足由来のTrend / BB / MACD状態を取得 | セクションは unavailable、またはBiasから除外 |

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
- `Chart Snapshot Summary`
- `Today's Events`
- `FedWatch`
- `Risk Label`
- `Market Bias`

`Chart Snapshot Summary` は `market-watch/chart_snapshot.py` 由来の1h代表値を表示する。表示項目は `Trend`、`State`、`Freshness`、`Bias Weight`、`BB Position`、`MACD State` である。

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
- Chart Snapshot Summary連携
- Market Bias本文生成
- Economic Calendar Providerの正常系・異常系
- FedWatch Providerの正常系・異常系
- optional Providerが既定falseで呼ばれないこと

外部APIアクセスはテストではMock化し、実Webhookや本物のAPIキーは使わない。

### 25.8 今後の拡張予定

- `MIN_IMPORTANCE_SCORE` による低スコアニュースの足切り
- RSSソース追加、Google News query調整
- Market Snapshotのsystemd化またはLambda化
- `market_bias.py` による Bull / Bear / Neutral 判定の改善
- AI要約、市場インパクト判定
- Fear & Greed
- AI Risk Score
- Polymarket
- COT Report

FedWatch / Economic Calendar は外部取得元が不安定なため、今後も必須Providerではなく optional Provider として扱う。

---

## 26. Berial v1.3 Market Watch Engine

v1.3 では、`market-watch/` を単なるMACDクロス通知Botではなく、チャート取得・指標計算・状態表示・イベント通知を担当する Market Watch Engine として拡張した。

Market Watch Engine は、ローソク足を取得し、SQLiteへ保存し、SMA / EMA / Bollinger Bands / MACDを計算する。計算結果は `Chart Snapshot` として単独表示できるほか、`news-feed-bot/market_snapshot.py` の `Chart Snapshot Summary` と `news-feed-bot/market_bias.py` の判定材料として利用する。

現時点では、売買判断、自動発注、エントリー推奨、ロット計算は行わない。

### 26.1 目的

- 複数シンボル・複数時間足のローソク足を取得する
- 取得データをSQLiteへ保存する
- timestampはUTCで保存し、Discord表示時のみJSTへ変換する
- SMA20 / SMA75 / EMA20 / Bollinger Bands / MACDを共通関数で計算する
- Chart Snapshotとして現在のチャート状態を表示する
- Market Snapshotへ1h代表値の要約を渡す
- Market BiasへTrend / BB / MACD状態を渡す
- MACDクロスは `macd_alert_bot.py` でイベント通知として扱う
- ニュース監視Botとは責務を分離する

### 26.2 ディレクトリ構成

```text
market-watch/
├─ .env.example
├─ config.toml
├─ chart_collector.py        # ローソク足取得、SQLite保存、UTC正規化
├─ chart_models.py           # ChartTarget / Candle / IndicatorSnapshot
├─ indicators.py             # SMA / EMA / BB / MACD / クロス判定 / 状態分類
├─ chart_snapshot.py         # Chart Snapshot生成、Discord投稿、Summary生成
├─ macd_alert_bot.py         # MACDクロスのイベント通知Bot
├─ data/
│  └─ chart_data.sqlite3     # runtime DB。Git管理しない
├─ state/
│  └─ macd_alert_state.json  # MACDクロス通知の重複防止state。Git管理しない
└─ tests/
   ├─ test_chart_collector.py
   ├─ test_chart_snapshot.py
   ├─ test_indicators.py
   └─ test_macd_alert_bot.py
```

`data/`、`state/`、`.env`、`__pycache__/`、`.pytest_cache/`、`logs/` はGit管理しない。

### 26.3 処理パイプライン

```text
chart_collector.py
  ↓
SQLite chart_data.sqlite3 (timestampはUTC保存)
  ↓
indicators.py
  ├─ SMA / EMA
  ├─ Bollinger Bands
  └─ MACD
  ↓
chart_snapshot.py
  ├─ Chart Snapshot詳細表示
  └─ Market Snapshot向けSummary
  ↓
market_snapshot.py / market_bias.py
```

MACDクロス通知は、上記のSnapshot系とは役割を分ける。

```text
indicators.py
  ↓
macd_alert_bot.py
  ↓
Discord Event Alert
```

`chart_snapshot.py` は「状態表示」、`macd_alert_bot.py` は「イベント通知」である。状態表示とイベント通知を混ぜないことで、Snapshot本文の肥大化や二重通知を避ける。

### 26.4 監視対象と時間足

現行のChart Snapshot対象は以下。

| Symbol | 用途 |
|---|---|
| `USDJPY` | 為替、ドル円 |
| `XAUUSD` | Gold |
| `SPX` | S&P500 |
| `VIX` | Volatility |

現行の時間足は以下。

- `15m`
- `1h`
- `4h`

Market Snapshotに統合する `Chart Snapshot Summary` では、本文を短く保つため、各Symbolの `1h` を代表値として表示する。

### 26.5 SQLite保存と時刻仕様

ローソク足は `market-watch/data/chart_data.sqlite3` へ保存する。保存時のtimestampはUTCへ正規化する。

```text
2026-07-03T20:45:00Z
```

Discord表示時のみJSTへ変換する。

```text
2026-07-04 05:45 JST
```

内部保存をUTCへ統一する理由は、USDJPY / SPX / VIX / XAUUSD のように市場・取得元ごとにタイムゾーンが異なるデータを同一基準で比較するためである。

### 26.6 指標仕様

`indicators.py` はチャート指標の共通ライブラリである。`chart_snapshot.py` と `macd_alert_bot.py` は、可能な限りここにある共通関数を利用する。

計算対象:

| 指標 | 内容 |
|---|---|
| `sma20` | Closeの20本単純移動平均 |
| `sma75` | Closeの75本単純移動平均 |
| `ema20` | Closeの20本指数移動平均 |
| `bb_middle` | SMA20 |
| `bb_upper` | SMA20 + 2σ |
| `bb_lower` | SMA20 - 2σ |
| `bb_width_pct` | `(bb_upper - bb_lower) / bb_middle * 100` |
| `bb_position` | CloseがBB内のどこにいるか。0.0=下限、0.5=中央、1.0=上限 |
| `macd` | EMA12 - EMA26 |
| `macd_signal` | MACDのEMA9 |
| `macd_hist` | `macd - macd_signal` |
| `macd_state` | `Bullish` / `Bearish` / `Neutral` |

MACD State:

| State | 条件 |
|---|---|
| `Bullish` | `macd > macd_signal` かつ `macd_hist > 0` |
| `Bearish` | `macd < macd_signal` かつ `macd_hist < 0` |
| `Neutral` | 上記以外、または値欠損 |

Trend:

| Trend | 条件 |
|---|---|
| `Bull` | `close > sma20 > sma75` |
| `Bear` | `close < sma20 < sma75` |
| `Neutral` | 上記以外 |
| `Stale` | データ鮮度がStaleの場合 |

Bollinger Band State:

| State | 条件 |
|---|---|
| `Upper breakout` | `bb_position > 1.0` |
| `Near upper band` | `0.8 <= bb_position <= 1.0` |
| `Inside band` | `0.2 <= bb_position < 0.8` |
| `Near lower band` | `0.0 <= bb_position < 0.2` |
| `Lower breakout` | `bb_position < 0.0` |
| `Unavailable` | 値欠損 |

### 26.7 Freshness / Bias Weight

Chart Snapshotは、最新ローソク足のtimestampと現在時刻を比較して鮮度を判定する。

| Freshness | 意味 | Bias Weight |
|---|---|---:|
| `Fresh` | 通常の新鮮なデータ | `1.0` |
| `Weekend Closed` | 土日など市場休場により更新が止まっている | `0.25` |
| `Market Closed` | 市場再開前など、休場時間帯 | `0.25` |
| `Stale` | 平常時に24時間超古いデータ | `0.0` |

`Stale` はMarket Bias計算から除外する。`Weekend Closed` / `Market Closed` は、休場中の最終有効値として表示は維持しつつ、Market Biasへの影響を弱める。

### 26.8 Chart Snapshot表示

`chart_snapshot.py --once --dry-run` は、Discordへ送信せずにChart Snapshot本文を標準出力する。

表示例:

```text
📈 Chart Snapshot

USDJPY 1h
Time: 2026-07-04 05:00 JST
Freshness: Weekend Closed (1d 9h old)
Bias Weight: 0.25
Close: 161.34
SMA20: 161.18
SMA75: 161.85
EMA20: 161.25
BB: 160.87 / 161.18 / 161.49
BB Width: 0.38%
BB Position: 0.76
MACD: -0.00872 / Signal: -0.06150 / Hist: 0.05278
MACD State: Bullish
Trend: Neutral
State: Inside band
```

### 26.9 MACD Alert Bot

`macd_alert_bot.py` は、MACDクロスが発生したときだけDiscordへイベント通知する常駐Botである。

初期監視対象:

| 項目 | 値 |
|---|---|
| symbol | `USD_JPY` |
| ticker | `JPY=X` |
| timeframe | `M15` |
| interval | `15m` |
| period | `5d` |
| MACD | `12,26,9` |
| check interval | `60s` |

クロス判定は、未確定足を除外し、前回の確定足と今回の確定足で行う。

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

同一足・同一signalの重複通知を防ぐため、stateを以下に保存する。

```text
market-watch/state/macd_alert_state.json
```

保存内容:

- `candle_time`
- `signal`
- `price`
- `macd`
- `signal_line`
- `notified_at`

旧v1.2系の `market-watch/macd_alert_state.json` が残っている場合は初回読み込み互換を持たせる。ただし新規保存先は `market-watch/state/` である。

### 26.10 起動方式 / 本番運用

Linux運用パスは `/opt/berial` を想定する。

初期確認:

```bash
cd /opt/berial
source .venv/bin/activate
python -m pip install -r requirements.txt
python market-watch/chart_snapshot.py --once --dry-run
python market-watch/macd_alert_bot.py --once --dry-run
python news-feed-bot/market_snapshot.py
```

常駐対象:

```text
news-feed-bot/news_feed_bot.py
market-watch/macd_alert_bot.py
```

定期実行対象:

```text
market-watch/chart_snapshot.py --once
news-feed-bot/market_snapshot.py
```

`macd_alert_bot.py` は systemd で常駐させる。手動起動プロセスとsystemd起動プロセスが二重起動しないよう、`ps -aux | grep .py` で確認する。

systemd例:

```ini
[Unit]
Description=Berial MACD Alert Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/berial/market-watch
Environment="PYTHONUNBUFFERED=1"
ExecStart=/opt/berial/.venv/bin/python /opt/berial/market-watch/macd_alert_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

確認コマンド:

```bash
systemctl status berial-macd-alert --no-pager
journalctl -u berial-macd-alert -f
ps -aux | grep .py
```

`--dry-run` ではDiscord送信もstate更新も行わない。

---

## 27. Berial v1.3 Market Bias Engine

v1.3 は、Berial が取得している市場データ(Yahoo Finance / FRED / TradingView / Chart Snapshot)をもとに、**Bull / Bear / Neutral の3分類だけ**を返す初期ロジックを追加するリリースである。

### 27.1 目的

- 既存Providerが取得済みのデータから、市場の大まかな方向感をルールベースで判定する
- Chart Snapshot由来のTrend / BB / MACD状態をBias計算へ反映する
- 判定結果、confidence、scores、reasons、warnings、note をDiscordへ出せる形にする
- 「観測結果の可視化」を一段進めるものであり、売買判断や自動発注を行う機能ではない

### 27.2 Market Bias Engine の位置づけ

`news-feed-bot/market_bias.py` は、Market Snapshotが集約したデータを読む補助レイヤーである。

```text
Market Snapshot(市場データの可視化)
        ├─ TradingView Summary
        ├─ Chart Snapshot Summary
        └─ Market Bias Engine(Bull / Bear / Neutral の3分類)
```

Market Bias Engine は、発注・執行・売買推奨を行うレイヤーには接続しない。

### 27.3 入力データ

`market_bias.classify_market_bias()` は以下を入力として受け取る。いずれも既存Providerがすでに取得済みのデータであり、Market Bias Engine自身は外部APIを呼び出さない。

| 入力 | 由来 | 必須 |
|---|---|---|
| `market_data` | `market_data.get_market_data()` (Yahoo Finance) | 任意(欠損可) |
| `fred_data` | `fred_data.get_fred_data()` (FRED) | 任意(`None`可) |
| `tv_data` | `tradingview_data.get_tradingview_data()` (TradingView TA) | 任意(欠損可) |
| `chart_data` | `market-watch/chart_snapshot.py` の `IndicatorSnapshot` summary | 任意(欠損可) |
| `macd_signal` | `macd_alert_bot.py` が検知した直近MACDクロス方向 | 任意。現在は主入力ではなく、イベント通知用の補助扱い |

`chart_data` は以下の値を含む。

- `symbol`
- `timeframe`
- `trend`
- `state`
- `freshness`
- `bias_weight`
- `bb_position`
- `macd_state`

すべての入力は欠損・部分欠損があっても例外を投げず、判定可能な範囲で分類を行う。

### 27.4 出力データ

```python
{
    "market_bias": "Bull",
    "confidence": 0.40,
    "scores": {"Bull": 3.5, "Bear": 1.5},
    "reasons": [
        "VIX is below 18",
        "S&P500 is rising",
        "SPX technical recommendation is positive",
        "Chart USDJPY 1h MACD is Bullish (weight 0.12)",
    ],
    "warnings": [],
    "note": "Observation only. No trade recommendation.",
}
```

### 27.5 仕様: market_bias / confidence / scores / reasons / warnings

#### market_bias

返却値は `Bull` / `Bear` / `Neutral` のいずれかのみとする。`Risk Off`、`USD Strength`、`Gold Strength` などの細かい市場状態ラベルは現行スコープ外であり、将来検討に回す。

- 入力データが不足している場合 → `Neutral`
- Bull / Bear のスコア差が小さい場合 → `Neutral`
- BullスコアがBearスコアを十分上回る場合 → `Bull`
- BearスコアがBullスコアを十分上回る場合 → `Bear`

#### confidence

`confidence` は 0.0～1.0 の範囲に収める。入力不足の場合は `0.0` とし、スコア差と評価できた材料数をもとに算出する。

#### scores

`scores` は `{"Bull": float, "Bear": float}` のdictで返す。Market Snapshot由来のVIX、VVIX、株価指数、米金利、Dollar Index、TradingView technical recommendation、Chart Snapshot由来のTrend / MACD Stateなどをルールベースで加点する。

Chart Snapshotは `bias_weight` を掛けて反映する。

- `Fresh`: 原則 `1.0`
- `Weekend Closed` / `Market Closed`: 原則 `0.25`
- `Stale`: `0.0`、Bias計算から除外

MACD StateはTrendより軽い補助指標として扱う。例: `0.5 * bias_weight`。

#### reasons

`reasons` は判定に使った根拠のlistで返す。Discord表示では長くなりすぎないよう最大数を制限する。

Chart Snapshot由来の例:

```text
Chart XAUUSD 1h trend is Bull (weight 0.25)
Chart USDJPY 1h MACD is Bullish (weight 0.12)
```

#### warnings

TradingView recommendation と Chart Snapshot のTrend / MACD Stateが大きく矛盾する場合、必要に応じて `warnings` へ入れる。短期のMACDや1hの状態は補助シグナルであり、全体判断を支配しすぎないように扱う。

#### note

常に固定文字列 `"Observation only. No trade recommendation."` を含める。分岐によって売買推奨文に変化させない。

### 27.6 売買判断・自動発注は行わないこと(制約)

Market Bias Engine は、以下を**実装しない**。

- 自動売買・自動発注
- エントリー推奨
- 利確・損切り判断
- 直接的な売買指示
- 細かい `Risk Off`、`USD Strength`、`Gold Strength` などの複数ラベル分類
- バックテスト

TradingView由来の `BUY` / `SELL` は内部データとして扱うことは許容するが、ユーザー向けMarkdownや `note` に売買指示として出さない。この制約は `tests/test_market_bias.py` で検証する。

### 27.7 Market Snapshotとの連携

`market_snapshot.build_market_snapshot_message()` は、本文内に `[Chart Snapshot Summary]` と `## Market Bias` セクションを追加する。生成時の例外は吸収し、Market Snapshot全体の投稿が壊れないようにする。

`Chart Snapshot Summary` では、各Symbolの1hを代表値として以下を表示する。

```text
- USDJPY 1h: Trend Neutral / State Inside band / Freshness Weekend Closed / Bias Weight 0.25 / BB Position 0.76 / MACD State Bullish
```

Market BiasはこのSummary由来の `chart_data` を受け取り、TrendとMACD Stateをスコアへ反映する。

### 27.8 v1.3.1以降の検討

- `Risk Off` / `USD Strength` / `Gold Strength` など、より細かい市場状態分類
- Market Bias判定履歴の保存と状態遷移の追跡
- 判定ロジックのバックテスト・閾値チューニング
- Chart Snapshotの定期投稿頻度調整
- ATR / 出来高 / Fear & Greed / COT Report などの補助指標追加
- デモ口座でのPaper Signal記録
- Pepperoniによる将来のエントリー執行レイヤー

これらは v1.3.1 以降の検討事項であり、現行v1.3では実装しない。

---

## 28. BELIAL Demon Organization / Pepperoni

Berial には伝承上で広く定着した名前付き眷属が存在しないため、BELIAL DEV では独自の眷属組織を定義する。

BELIAL軍団の眷属は、炎・辛味・スパイスをモチーフとしたコードネームで統一する。各眷属は市場における専門役割を持ち、Berial の判断や命令を受けて個別業務を担当する。

### 28.1 組織方針

| 名前 | 役割 | ステータス |
|---|---|---|
| Berial | 軍団長 / 市場監視・戦略立案 | 実装中 |
| Pepperoni | エントリー執行 | 構想追加 |
| Jalapeño | ブレイクアウト・モメンタム担当 | 未定 |
| Habanero | 高リスク戦略・危険相場担当 | 未定 |
| Tabasco | Discord通知・アラート担当 | 未定 |
| Paprika | レポート・可視化担当 | 未定 |
| Carolina | 最終兵器・特殊判断担当 | 未定 |

```text
Berial
  ├─ Pepperoni  # Entry Execution
  ├─ Jalapeño   # Momentum / Breakout
  ├─ Habanero   # High Risk Strategy
  ├─ Tabasco    # Alert / Notification
  ├─ Paprika    # Report / Dashboard
  └─ Carolina   # Special / Emergency Strategy
```

### 28.2 Pepperoni の役割

Pepperoni は、Berial から渡された市場判断をもとに、実際のエントリーを実行する執行担当の悪魔である。

裁量判断は苦手だが、命令を忠実かつ高速に実行することを得意とする。市場判断そのものは Berial が担当し、Pepperoni は「いつ、どの条件で、どの注文を出すか」を実装上の責務として受け持つ。

### 28.3 Pepperoni の責務

- エントリー執行
- ポジションオープン
- 発注管理
- エントリー履歴保存
- 約定確認
- 実行ログ出力

将来的な実装イメージ:

```text
Berial
  └─ Pepperoni
      ├─ Entry Execution
      ├─ Order Management
      ├─ Position Open
      └─ Execution Log
```

### 28.4 Pepperoni の性格

- 気弱
- 真面目
- 自己評価が低い
- 命令には忠実
- 褒められると頑張る
- 失敗すると極端に落ち込む
- 損失や失敗を自分で背負い込もうとする

### 28.5 Pepperoni のデザイン

- 年上感のある長身のお姉さん
- ピンク色のロングヘア
- 大きな垂れ目
- 黒い悪魔角
- Berial軍団の紋章を模した髪留め
- 黒×ピンクを基調とした衣装
- 炎を宿したエントリーペンを所持
- キャラクター本体から炎のエフェクトは出さない
- 気弱で不安げだが、命令を受けると行動する

### 28.6 Weapon: Entry Flame Pen

`Entry Flame Pen` は、市場へエントリー命令を刻むための悪魔のペンである。

Pepperoni 自身は炎のエフェクトを出さないが、このペンだけが炎を帯びる。これは「市場に入る瞬間だけ火が灯る」ことを示す演出であり、Berial の判断が実際のポジションへ変換される境界を表す。

### 28.7 Representative Lines

> 「えっと……エントリーします……。」

> 「……失敗しました。すみません……。」

> 「もう一度だけ……やらせてください。」

> 「Berial様の判断なら……信じます。」

> 「だ、大丈夫です……最悪、私が市場で身体を張りますから……。」

### 28.8 実装上の注意

Pepperoni は将来的な自動発注レイヤーを象徴するキャラクターである。ただし、現行の v1.3 では自動売買・自動発注・エントリー推奨は実装しない。
