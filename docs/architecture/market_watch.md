# Market Watch

v1.3 では、`market-watch/` を単なるMACDクロス通知Botではなく、チャート取得・指標計算・状態表示・イベント通知を担当する Market Watch Engine として拡張した。

Market Watch Engine は、ローソク足を取得し、SQLiteへ保存し、SMA / EMA / Bollinger Bands / MACD / RSI14を計算する。計算結果は `Chart Snapshot` として単独表示できるほか、`news-feed-bot/market_snapshot.py` の `Chart Snapshot Summary` と `news-feed-bot/market_bias.py` の判定材料として利用する。RSI14は現時点では表示・観測専用であり、Market Biasのスコアには入れない。

現時点では、売買判断、自動発注、エントリー推奨、ロット計算は行わない。

## 目的

- 複数シンボル・複数時間足のローソク足を取得する
- 取得データをSQLiteへ保存する
- timestampはUTCで保存し、Discord表示時のみJSTへ変換する
- SMA20 / SMA75 / EMA20 / Bollinger Bands / MACD / RSI14を共通関数で計算する
- Chart Snapshotとして現在のチャート状態を表示する
- Market Snapshotへ1h代表値の要約を渡す
- Market BiasへTrend / BB / MACD状態を渡す。RSI14 / RSI StateはSummary表示用の観測値として保持し、Biasスコアには反映しない
- MACDクロスは `macd_alert_bot.py` でイベント通知として扱う
- Bollinger Bands Alert は `bollinger_alert_bot.py` で市場状態監視のイベント通知として扱う
- ニュース監視Botとは責務を分離する

## ディレクトリ構成

```text
market-watch/
├─ .env.example
├─ config.toml
├─ chart_collector.py        # ローソク足取得、SQLite保存、UTC正規化
├─ chart_models.py           # ChartTarget / Candle / IndicatorSnapshot
├─ indicators.py             # SMA / EMA / BB / MACD / RSI / クロス判定 / 状態分類
├─ chart_snapshot.py         # Chart Snapshot生成、Discord投稿、Summary生成
├─ macd_alert_bot.py         # MACDクロスのイベント通知Bot
├─ bollinger_alert_bot.py    # Bollinger Bandsの市場状態イベント通知Bot
├─ data/
│  └─ chart_data.sqlite3     # runtime DB。Git管理しない
├─ state/
│  ├─ macd_alert_state.json  # MACDクロス通知の重複防止state。Git管理しない
│  └─ bollinger_alert_state.json  # Bollinger通知の重複防止state。Git管理しない
└─ tests/
   ├─ test_chart_collector.py
   ├─ test_chart_snapshot.py
   ├─ test_indicators.py
   ├─ test_macd_alert_bot.py
   └─ test_bollinger_alert_bot.py
```

`data/`、`state/`、`.env`、`__pycache__/`、`.pytest_cache/`、`logs/` はGit管理しない。

## 処理パイプライン

```text
chart_collector.py
  ↓
SQLite chart_data.sqlite3 (timestampはUTC保存)
  ↓
indicators.py
  ├─ SMA / EMA
  ├─ Bollinger Bands
  ├─ MACD
  └─ RSI14
  ↓
chart_snapshot.py
  ├─ Chart Snapshot詳細表示
  └─ Market Snapshot向けSummary
  ↓
market_snapshot.py / market_bias.py
```

## Chart Snapshot画像生成

Chart Snapshot画像は、定時Snapshotとイベント通知で同じ描画関数を使う。

```text
chart_snapshot.py
  -> render_chart_snapshot()
  -> S3 upload
  -> Discord chart-snapshot channel

macd_alert_bot.py / ma_alert_bot.py / bollinger_alert_bot.py
  -> chart_alert_context.prepare_alert_image()
  -> render_chart_snapshot()
  -> S3 upload
  -> presigned URL
  -> Discord embed.image.url
```

`render_chart_snapshot()` は `market-watch/chart_renderer.py` の共通描画関数である。入力は `open` / `high` / `low` / `close` を持つOHLC DataFrameを必須とし、close-only Seriesやclose-only DataFrameを正常扱いするfallbackは持たない。これはローソク足が横線化したり、OHLC欠損に気づかないまま通知画像を生成したりすることを防ぐためである。

アラート画像生成入口の `prepare_alert_image()` もOHLC DataFrameを必須とする。アラート判定ロジックは引き続き `close` を主に使うが、画像生成には同じDataFrameの `open` / `high` / `low` / `close` を渡す。テストやモックもこの契約に合わせ、`open = close`、`high = close + 0.01`、`low = close - 0.01` のような最小OHLCを明示的に渡す。

描画内容は通知種別に応じて変わる。

| 通知種別 | 画像に表示する主な要素 |
|---|---|
| 定時Chart Snapshot | ローソク足、SMA20、SMA75、EMA20、Bollinger upper / middle / lower、MACD |
| MA Alert | ローソク足、SMA20 / SMA75 / EMA20、Bollinger upper / middle / lower |
| MACD Alert | ローソク足、MACD、Signal、Histogram |
| Bollinger Alert | ローソク足、Bollinger upper / middle / lower。`upper_break` / `lower_break` では突破対象ラインを強調する |

ローソク足はOpen/Closeからbodyを描画する。bodyが極端に小さい場合も、画面上で確認できる最小表示サイズを持たせる。ただし、欠損したOHLCをcloseから暗黙補完して正常扱いすることはしない。

MACDクロス通知は、上記のSnapshot系とは役割を分ける。

```text
indicators.py
  ↓
macd_alert_bot.py
  ↓
Discord Event Alert
```

Bollinger Bands Alert は同じイベント通知層に属する。

```text
indicators.py
  →
bollinger_alert_bot.py
  →
Discord Event Alert
```

`chart_snapshot.py` は「状態表示」、`macd_alert_bot.py` / `ma_alert_bot.py` / `bollinger_alert_bot.py` は「イベント通知」である。状態表示とイベント通知を混ぜないことで、Snapshot本文の肥大化や二重通知を避ける。

Discord投稿先も役割ごとに分離する。

| 変数 | 投稿先 | 役割 |
|---|---|---|
| `CHART_SNAPSHOT_WEBHOOK_URL` | `chart-snapshot` | 定時のチャート状態レポート。`chart_snapshot.py` が使用する |
| `BOT_ALERTS_WEBHOOK_URL` | `bot-alerts` | MAタッチ、MACDクロス、Bollinger Bands Alert、急変などのイベント通知。`macd_alert_bot.py` / `ma_alert_bot.py` / `bollinger_alert_bot.py` が使用する |

Chart Snapshot画像は private S3 bucket に保存し、`chart_snapshots.s3_bucket` / `chart_snapshots.s3_key` に永続メタデータを保存する。S3 bucket は public access block 済みの前提であり、Discord通知ではS3直URLを本文に載せず、短時間だけ有効な presigned URL を `embed.image.url` に設定する。presigned URL は期限切れするためDBへ保存しない。

旧設定との互換性のため、イベント通知側は `BOT_ALERTS_WEBHOOK_URL` が未設定の場合に限り旧 `DISCORD_WEBHOOK_URL` をfallbackとして扱う。新規運用では `CHART_SNAPSHOT_WEBHOOK_URL` と `BOT_ALERTS_WEBHOOK_URL` を明示する。

## 監視対象と時間足

現行のChart Snapshot対象は以下。

| Symbol | 用途 |
|---|---|
| `USDJPY` | 為替、ドル円 |
| `XAUUSD` | Gold |
| `SPX` | S&P500. Chart data may use `ES` / `ES=F` as an index futures proxy with `SPX` / `^GSPC` fallback. |
| `VIX` | Volatility |

`SPX` は論理Symbolとして残し、表示名とチャート取得用Symbolを分離する。SQLite、Discord本文、S3 keyでは `SPX` を保存・表示の基準にする。一方、ローソク足取得では `CHART_SYMBOL_SPX` / `CHART_TICKER_SPX` を使い、現物指数の休場・時間外欠損を避けるため、既定では E-mini S&P 500 Futures をプロキシにできる。

```env
CHART_DISPLAY_NAME_SPX="S&P 500"
CHART_SYMBOL_SPX=ES
CHART_TICKER_SPX=ES=F
CHART_FALLBACK_CHART_SYMBOL_SPX=SPX
CHART_FALLBACK_TICKER_SPX=^GSPC
CHART_ASSET_TYPE_SPX=index_futures_proxy
```

Micro E-mini を使う場合は `CHART_SYMBOL_SPX=MES` / `CHART_TICKER_SPX=MES=F` に切り替える。取得プロキシが空データを返す場合は fallback の `SPX` / `^GSPC` を使う。これはS&P500の地合い監視を目的とした観測用プロキシであり、売買判断や発注対象を意味しない。

現行の時間足は以下。

- `15m`
- `1h`
- `4h`

Market Snapshotに統合する `Chart Snapshot Summary` では、本文を短く保つため、各Symbolの `1h` を代表値として表示する。

## SQLite保存と時刻仕様

ローソク足は `market-watch/data/chart_data.sqlite3` へ保存する。保存時のtimestampはUTCへ正規化する。

`chart_snapshots` は `id` / `symbol` / `timeframe` / `snapshot_at` / `s3_bucket` / `s3_key` / `created_at` を保持する。互換用の `image_url` / `image_s3_key` が存在するDBでも、S3保存では `s3_bucket` / `s3_key` を正とする。

`alerts` は `snapshot_id` と `alert_chart_snapshots.alert_id` / `chart_snapshot_id` で `chart_snapshots.id` を参照する。`occurred_at` / `snapshot_at` / `candle_time` は補助情報として保持し、主関連を時刻JOINにしない。

```text
2026-07-03T20:45:00Z
```

Discord表示時のみJSTへ変換する。

```text
2026-07-04 05:45 JST
```

内部保存をUTCへ統一する理由は、USDJPY / SPX / VIX / XAUUSD のように市場・取得元ごとにタイムゾーンが異なるデータを同一基準で比較するためである。

## 指標仕様

`indicators.py` はチャート指標の共通ライブラリである。`chart_snapshot.py`、`macd_alert_bot.py`、`ma_alert_bot.py`、`bollinger_alert_bot.py` は、可能な限りここにある共通関数を利用する。

計算対象:

| 指標 | 内容 |
|---|---|
| `sma20` | Closeの20本単純移動平均 |
| `sma75` | Closeの75本単純移動平均 |
| `ema20` | Closeの20本指数移動平均 |
| `bb_middle` | SMA20 |
| `bb_upper` | SMA20 + 2σ |
| `bb_lower` | SMA20 - 2σ |
| `bb_bandwidth` | `(bb_upper - bb_lower) / bb_middle` |
| `bb_width_pct` | `(bb_upper - bb_lower) / bb_middle * 100` |
| `bb_position` | CloseがBB内のどこにいるか。0.0=下限、0.5=中央、1.0=上限 |
| `macd` | EMA12 - EMA26 |
| `macd_signal` | MACDのEMA9 |
| `macd_hist` | `macd - macd_signal` |
| `macd_state` | `Bullish` / `Bearish` / `Neutral` |
| `rsi14` | Wilder-style RSI, period=14 |
| `rsi_state` | `Overbought` / `Oversold` / `Neutral` / `Unavailable` |

MACD State:

| State | 条件 |
|---|---|
| `Bullish` | `macd > macd_signal` かつ `macd_hist > 0` |
| `Bearish` | `macd < macd_signal` かつ `macd_hist < 0` |
| `Neutral` | 上記以外、または値欠損 |

RSI State:

| State | Condition |
|---|---|
| `Overbought` | `rsi14 >= 70` |
| `Oversold` | `rsi14 <= 30` |
| `Neutral` | `30 < rsi14 < 70` |
| `Unavailable` | `rsi14` is `None` / `NaN`, or candles are insufficient |

RSI14 / RSI State are observation-only values. They are displayed in Chart Snapshot and Chart Snapshot Summary, but are not used for Market Bias score, Paper Signal candidate rules, entries, exits, lot sizing, or order handling.

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

## Freshness / Bias Weight

Chart Snapshotは、最新ローソク足のtimestampと現在時刻を比較して鮮度を判定する。

| Freshness | 意味 | Bias Weight |
|---|---|---:|
| `Fresh` | 通常の新鮮なデータ | `1.0` |
| `Weekend Closed` | 土日など市場休場により更新が止まっている | `0.25` |
| `Market Closed` | 市場再開前など、休場時間帯 | `0.25` |
| `Stale` | 平常時に24時間超古いデータ | `0.0` |

`Stale` はMarket Bias計算から除外する。`Weekend Closed` / `Market Closed` は、休場中の最終有効値として表示は維持しつつ、Market Biasへの影響を弱める。

## Chart Snapshot表示

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
RSI14: 58.40
RSI State: Neutral
Trend: Neutral
State: Inside band
```

## MACD Alert Bot

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

## MA Alert Bot

`ma_alert_bot.py` は、USDJPY M15の確定足で終値が監視対象の移動平均線に接近したときだけDiscordへイベント通知するBotである。これはChart Snapshotではなくイベント通知であり、売買判断、エントリー推奨、ロット計算、自動発注は行わない。

初期監視対象:

| 項目 | 値 |
|---|---|
| symbol | `USD_JPY` |
| ticker | `JPY=X` |
| timeframe | `M15` |
| interval | `15m` |
| period | `5d` |
| MA | `sma20` / `sma75` / `ema20` |
| threshold_pct | `0.05` |

判定は確定足の `close` を基準に行う。Yahoo Financeの最新足は未確定の可能性があるため、最新足 `-1` は使わず、直近の確定足 `-2` を使う。

```text
distance_pct = abs(close - ma) / ma * 100
distance_pct <= threshold_pct
```

`side` は `close >= ma` のとき `above`、`close < ma` のとき `below` とする。

同一足・同一MAの重複通知を避けるため、stateを以下へ保存する。

```text
market-watch/state/ma_alert_state.json
```

state keyは `symbol`、`timeframe`、`ma_name`、`candle_time` を区別できる形式とし、保存内容には `candle_time`、`ma_name`、`close`、`ma_value`、`distance_pct`、`side`、`notified_at` を含める。

通知先は `BOT_ALERTS_WEBHOOK_URL` を使う。既存互換として、未設定の場合のみ `DISCORD_WEBHOOK_URL` へfallbackしてよい。Embed titleは `Berial Market Watch: MA Touch` とし、注意文として `This is a watch alert only, not a trade decision.` を含める。

設定は `market-watch/config.toml` の `[ma_alert]` に置く。

```toml
[ma_alert]
enabled = true
threshold_pct = 0.05
ma_names = ["sma20", "sma75", "ema20"]
```

ログは `logs/ma_alert_bot.log` に `RotatingFileHandler` で出力する。

起動例と dry-run の確認方法は [Operations](../operations/operations.md) に置く。

## Bollinger Bands Alert Bot

`bollinger_alert_bot.py` は、BELIAL の `market-watch` における市場状態監視アラートである。Bollinger Bands の ±2σ 到達、ボラティリティ圧縮、圧縮解除を検知し、既存のイベント通知導線へ通知する。

この機能の目的は、価格やボラティリティの状態変化を「何が起きたか」として記録・通知することである。売買判断、エントリー指示、Exit指示、損切り判断、利確判断、売買推奨文言の生成は行わない。Alert 層は事実検知だけを担当し、その情報をどう解釈するかは Strategy / Method 側の責務とする。

責務分離:

| Layer | Responsibility | Example |
|---|---|---|
| Alert | 事実を検知して通知する | 終値が +2σ を突破した、squeeze から expansion_start に移行した |
| Strategy / Method | Alert の情報を文脈に応じて解釈する | トレンド相場では順張り候補、レンジ相場では過熱警戒、他指標と組み合わせた条件判定 |
| Execution | 実際の注文、Exit、損切り、利確を担当する | Bollinger Alert では扱わない |

計算仕様:

| Item | Value |
|---|---|
| `window` | `20` |
| `std_multiplier` | `2.0` |
| `middle` | close の20期間移動平均 |
| `upper` | `middle + 2σ` |
| `lower` | `middle - 2σ` |
| `bandwidth` | `(upper - lower) / middle` |

シグナル仕様:

| signal_type | Condition | Meaning | Default notify | Note |
|---|---|---|---|---|
| `upper_break` | `close >= upper` | 終値が +2σ 外側 | ON | 過熱またはブレイク候補の観測 |
| `lower_break` | `close <= lower` | 終値が -2σ 外側 | ON | 過熱またはブレイク候補の観測 |
| `upper_touch` | `high >= upper` | 高値が +2σ に接触 | OFF | 通知頻度が高くなりやすいため初期OFF |
| `lower_touch` | `low <= lower` | 安値が -2σ に接触 | OFF | 通知頻度が高くなりやすいため初期OFF |
| `squeeze` | `bandwidth` が過去120本の下位10%以下 | ボラティリティ圧縮 | OFF | 状態継続中に通知が増えやすいため初期OFF |
| `expansion_start` | 直前足が squeeze、現在足で squeeze を抜ける、かつ `bandwidth` が直前足比 `1.3x` 以上 | ボラティリティ拡張開始候補 | ON | レンジ圧縮後の変化検知 |

通知仕様:

```text
[BOLLINGER] USD_JPY M15 upper_break
close=161.234 upper=161.180 middle=160.820 lower=160.459
bandwidth=0.45%
note=終値が+2σ外側。過熱/ブレイク候補。エントリー判断は手法側で確認。
```

通知には `symbol`、`timeframe`、`signal_type`、`timestamp`、`close`、`upper`、`middle`、`lower`、`bandwidth`、`note` を含める。

Webhook仕様:

- Bollinger Alert は既存のイベント通知導線に乗せる。
- `BOT_ALERTS_WEBHOOK_URL` を優先する。
- 未設定時のみ legacy `DISCORD_WEBHOOK_URL` へ fallback してよい。
- `CHART_SNAPSHOT_WEBHOOK_URL` は定時レポート専用であり、Bollinger Alert では使わない。

State / Dedupe 方針:

- Yahoo Finance の最新足は未確定の可能性があるため、判定は確定足 `-2` ベースで行う。
- 同一足・同一銘柄・同一時間足・同一シグナルの重複通知を防ぐ。
- state ファイルは `market-watch/state/bollinger_alert_state.json` を使う。
- dedupe の概念は `bollinger:{symbol}:{timeframe}:{signal_type}:{timestamp}` である。
- 実装上の state key は `symbol + timeframe + signal_type + candle_time` を識別できる形式とする。
- `--dry-run` では Discord 送信も state 更新も行わない。

設定は `market-watch/config.toml` の `[bollinger]` と `[bollinger.notify]` に置く。

```toml
[bollinger]
enabled = true
window = 20
std_multiplier = 2.0
squeeze_lookback = 120
squeeze_percentile = 0.10
expansion_ratio = 1.30

[bollinger.notify]
upper_break = true
lower_break = true
expansion_start = true
upper_touch = false
lower_touch = false
squeeze = false
```

初期ON:

- `upper_break`
- `lower_break`
- `expansion_start`

初期OFF:

- `upper_touch`
- `lower_touch`
- `squeeze`

非スコープ:

- 自動エントリー
- 自動Exit
- 損切り判断
- 利確判断
- 売買推奨文言の通知
- バックテスト最適化
- Strategy / Method の実装

手動確認、実運用確認、継続監視のコマンドは [Operations](../operations/operations.md) に置く。
