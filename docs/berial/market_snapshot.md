# Market Snapshot

この章は、Berial が RSSニュース監視Botから、マーケットデータ監視機能を持つBotへ拡張された現行実装を反映する。古い構想章と差異がある場合、現行の `news-feed-bot/` 配下の実装については本章を優先する。

## システム概要

現行の Berial は、以下の3つの監視レイヤーを持つ。

- RSSニュース監視
  - Google News / Reuters と Yahoo!ニュース系RSSを取得する
  - 金融・市場関連ニュースをフィルタし、重要度順にDiscordへ通知する
- Market Snapshot
  - Yahoo Finance / FRED / TradingView TA から市場データを取得する
  - FREDのマクロ指標、TradingViewのテクニカル指標、Yahoo Financeの市場価格をまとめる
  - 簡易 `Risk Label` を算出し、Discord投稿本文として整形する
- Market Watch
  - `market-watch/` 配下でローソク足を取得し、SMA / EMA / Bollinger Bands / MACD / RSI14を計算する
  - Chart Snapshotを生成し、Market Snapshotの `Chart Snapshot Summary` と Market Bias に連携する
  - MACDクロス発生時は `macd_alert_bot.py` がイベント通知する
  - Bollinger Bands の break / squeeze / expansion_start は `bollinger_alert_bot.py` がイベント通知する

現時点では、AI分析・売買判断・自動発注は行わない。Market Snapshot と Market Watch は、BELIALの将来のAI判断層へ渡す前段の「市場状態の可視化」機能である。

## ディレクトリ構成

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

`market-watch/` 配下の構成は [Market Watch](market_watch.md) に記載する。

## データ取得Provider

| Provider | 実装ファイル | 役割 | 失敗時 |
|---|---|---|---|
| Yahoo Finance | `market_data.py` | 市場価格・指数・商品価格を取得 | 個別シンボルを `None` |
| FRED | `fred_data.py` | 金利・マクロ指標を公式APIから取得 | APIキー未設定時はFREDセクションをスキップ |
| TradingView | `tradingview_data.py` | テクニカル指標を取得 | 個別シンボルを `None` |
| Economic Calendar | `economic_calendar.py` | 経済イベント取得のProvider枠 | `[]`、本文は unavailable |
| FedWatch | `fedwatch.py` | 金利確率取得のProvider枠 | `None`、本文は unavailable |
| Chart Snapshot | `../market-watch/chart_snapshot.py` | ローソク足由来のTrend / BB / MACD / RSI状態を取得 | セクションは unavailable、またはBiasから除外 |

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

## Market Snapshot仕様

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

`Chart Snapshot Summary` は `market-watch/chart_snapshot.py` 由来の1h代表値を表示する。表示項目は `Trend`、`State`、`Freshness`、`Bias Weight`、`BB Position`、`MACD State`、`RSI14`、`RSI State` である。

`news_feed_bot.py` からは `MARKET_SNAPSHOT_ENABLED=true` のとき、`MARKET_SNAPSHOT_TIME` に `market_snapshot.get_market_snapshot_message()` を呼び出してDiscordへ投稿する。

## Risk Label仕様

`compute_risk_label()` は以下の簡易ルールで `Risk Off` / `Risk On` / `Watch` / `Neutral` を返す。

| Label | 条件 |
|---|---|
| Risk Off | VIX > 25、VVIX > 100、またはVIX前日比 +15%以上 |
| Risk On | VIX < 18 かつ S&P500 / Nasdaq が上昇 |
| Watch | US10Y上昇、Dollar Index上昇、または株指数下落 |
| Neutral | 上記以外 |

これはルールベースの補助ラベルであり、AI判定や売買判断ではない。
