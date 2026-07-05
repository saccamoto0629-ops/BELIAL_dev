
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
- `market_bias.py` による Bull / Bear / Neutral 判定の改善
- AI要約、市場インパクト判定
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

## 27. Berial v1.3.0 Market Bias Engine

v1.3.0 は、Berial が既に取得している市場データ(Yahoo Finance / FRED / TradingView / MACD Alert Bot)をもとに、**Bull / Bear / Neutral の3分類だけ**を返す初期ロジックを追加するリリースである。

### 27.1 目的

- 既存Providerが取得済みのデータから、市場の大まかな方向感をルールベースで判定する
- 判定結果、confidence、scores、reasons、warnings、note をDiscordへ出せる形にする
- 「観測結果の可視化」を一段進めるものであり、売買判断や自動発注を行う機能ではない

### 27.2 Market Bias Engine の位置づけ

第25章で述べた通り、現行のBerialはRSSニュース監視とMarket Snapshotを持つ。v1.3.0で追加する `news-feed-bot/market_bias.py` は、Market Snapshot が集約したデータを読むだけの補助レイヤーである。

```text
Market Snapshot(市場データの可視化)
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
| `macd_signal` | `market-watch/macd_alert_bot.py` が検知した直近MACDクロス方向(`"bullish"` / `"bearish"` / `None`) | 任意、v1.3.0では呼び出し側が明示的に渡す場合のみ使用 |

すべての入力は欠損・部分欠損があっても例外を投げず、判定可能な範囲で分類を行う。

### 27.4 出力データ

```python
{
    "market_bias": "Bear",
    "confidence": 0.67,
    "scores": {"Bull": 1, "Bear": 5},
    "reasons": [
        "VIX is above 25",
        "S&P500 is falling",
        "Nasdaq is falling",
    ],
    "warnings": [
        "M15 MACD bullish cross conflicts with broader Bear conditions",
    ],
    "note": "Observation only. No trade recommendation.",
}
```

### 27.5 仕様: market_bias / confidence / scores / reasons / warnings

#### market_bias

返却値は `Bull` / `Bear` / `Neutral` のいずれかのみとする。`Risk Off`、`USD Strength`、`Gold Strength` などの細かい市場状態ラベルは v1.3.0 のスコープ外であり、将来検討に回す。

- 入力データが不足している場合 → `Neutral`
- Bull / Bear のスコア差が小さい場合 → `Neutral`
- BullスコアがBearスコアを十分上回る場合 → `Bull`
- BearスコアがBullスコアを十分上回る場合 → `Bear`

#### confidence

`confidence` は 0.0～1.0 の範囲に収める。入力不足の場合は `0.0` とし、スコア差が大きいほど高くなる。

#### scores

`scores` は `{"Bull": int, "Bear": int}` のdictで返す。Market Snapshot由来のVIX、VVIX、株価指数、米金利、Dollar Index、TradingView technical recommendationなどをルールベースで加点する。

#### reasons

`reasons` は判定に使った根拠のlistで返す。Discord表示では長くなりすぎないよう最大数を制限する。

#### warnings

M15 MACDの直近クロス方向と、より広いMarket Snapshot由来の方向感が矛盾する場合に `warnings` へ入れる。MACD Alert は短期補助シグナルであり、全体判断を支配しすぎないように扱う。

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

`market_snapshot.build_market_snapshot_message()` は、末尾に `## Market Bias` セクションを追加する。生成時の例外は吸収し、Market Snapshot全体の投稿が壊れないようにする。

`macd_signal` は `build_market_snapshot_message()` の任意キーワード引数として渡せるが、v1.3.0時点では `market-watch/macd_alert_bot.py` のstateを自動的に読み取る連携は行わない。呼び出し側が明示的に渡さない限り `None` として扱われる。

### 27.8 v1.3.1以降の検討

- `Risk Off` / `USD Strength` / `Gold Strength` など、より細かい市場状態分類
- Market Bias判定履歴の保存と状態遷移の追跡
- 判定ロジックのバックテスト・閾値チューニング
- `market-watch/macd_alert_bot.py` のstateを読み取る自動連携
- 複数時間足(M15以外)や他通貨ペアへの対応

これらは v1.3.1 以降の検討事項であり、v1.3.0では実装しない。

---

## 29. Berial v1.4.0 Paper Signal Generator

v1.4.0 は、Market Bias(第27章)と Chart Snapshot(`market-watch/chart_snapshot.py`)をもとに、**paper_only のシグナル仮説**を生成・保存・Discord通知する `paper-signal/` を追加するリリースである。

### 29.1 目的

- Market Bias と Chart Snapshot(1h)から、ルールベースでシグナル候補を作る
- 生成したシグナルを SQLite に記録し、Discord へ通知できるようにする
- **実発注・自動売買は一切行わない。** 生成物はあくまで「仮説の記録」である

### 29.2 位置づけ

```text
Market Snapshot(可視化)─ Market Bias(第27章、市場状態の分類)
                                         │
                                         ├─ Chart Snapshot(1h, market-watch/chart_snapshot.py)
                                         │
                                         └─ Paper Signal Generator(paper-signal/, 本章)
                                                  │
                                          (まだ存在しない) Entry Bot
```

Paper Signal は、Market Bias が示す「市場の大まかな方向」と、Chart Snapshot が示す「チャート状態(1h)」を組み合わせて、シグナル仮説を記録するだけの層である。エントリー実行を担当する層(Entry Bot / Pepperoni)はv1.4.0では実装しない。

### 29.3 入力データ

`paper-signal/signal_generator.py` は、既存Providerが取得済みのデータのみを使う。自前でAPIを呼び出すのは `news-feed-bot/market_snapshot.fetch_snapshot_sources()` 経由のみである。

| 入力 | 由来 | 説明 |
|---|---|---|
| `market_bias` / `confidence` | `news-feed-bot/market_bias.classify_market_bias()` | `Bull` / `Bear` / `Neutral` と 0.0〜1.0 の confidence |
| Chart Snapshot(1h) | `fetch_snapshot_sources()["chart"]`(内部で `market-watch/chart_snapshot.py` を呼ぶ) | symbol / timeframe / trend / state / freshness / bias_weight / macd_state |

### 29.4 出力データ(Paper Signal)

`paper-signal/signal_models.py` の `create_paper_signal()` が返す。

```python
{
    "signal_id": "b3f1...",
    "created_at_utc": "2026-07-05T12:00:00Z",
    "symbol": "USDJPY",
    "timeframe": "1h",
    "side": "long",
    "market_bias": "Bull",
    "confidence": 0.72,
    "macd_state": "Bullish",
    "trend": "Bull",
    "state": "Inside band",
    "freshness": "Fresh",
    "reason": "Market Bias=Bull (72%); USDJPY 1h MACD=Bullish, Trend=Bull, State=Inside band, Freshness=Fresh.",
    "status": "paper_only",
}
```

`status` は常に `"paper_only"` であり、他の値を取ることはない。

### 29.5 シグナル生成ルール

`paper-signal/signal_generator.build_candidates()` が判定する。

1. `market_bias` が `Bull` または `Bear` のときだけ候補にする(`Neutral` では生成しない)
2. `confidence` が `PAPER_SIGNAL_CONFIDENCE_THRESHOLD`(既定 `0.40`)以上のときだけ候補にする
3. Chart Snapshot の `1h` を代表時間足として使う(他の時間足は見ない)
4. Bull の場合: `macd_state == "Bullish"` かつ `freshness == "Fresh"` かつ `bias_weight > 0` → `side = "long"`
5. Bear の場合: `macd_state == "Bearish"` かつ `freshness == "Fresh"` かつ `bias_weight > 0` → `side = "short"`
6. `freshness` が `"Weekend Closed"` / `"Market Closed"` / `"Stale"` のいずれかの場合はシグナルを生成しない(`"Fresh"` のみ候補にする)

### 29.6 重複防止

`paper-signal/paper_signal_store.has_recent_signal()` により、同一 `symbol` / `timeframe` / `side` の組み合わせで `PAPER_SIGNAL_DUPLICATE_WINDOW_MINUTES`(既定60分)以内に生成済みの場合はスキップする。

### 29.7 ストレージ

SQLiteに保存する。パスは `paper-signal/data/paper_signals.sqlite3`(`paper-signal/paper_signal_store.py`)。テーブルは29.4の全フィールドを1行1シグナルとして保持する。`paper-signal/data/` はGit管理しない(`.gitignore`)。

### 29.8 Discord通知

本文の末尾に必ず固定文字列 `"Paper signal only. No trade execution."` を含める(`format_discord_message()`)。この制約は `tests/test_signal_generator.py` で検証する。

### 29.9 売買判断・自動発注は行わないこと(制約)

Paper Signal Generator は、以下を**実装しない**。

- 自動売買・自動発注
- 発注API接続(OANDA / XM / MT5 などbroker APIは使わない)
- ロット計算・レバレッジ計算
- 利確・損切り(stop-loss / take-profit)判断
- エントリー実行

`paper-signal/signal_generator.py` のソースコードは、broker APIのimportや発注関数呼び出しが存在しないことをテスト(`test_module_source_has_no_broker_api_integration`)で検証する。生成されるレコードの各フィールド値にも、order / lot / leverage / stop-loss / take-profit 等の語が含まれないことを検証する。

### 29.10 CLI

```bash
cd /opt/berial/paper-signal

# 確認のみ(Discord送信・SQLite保存なし)
python signal_generator.py --once --dry-run

# 1回実行(Discord送信・SQLite保存あり)
python signal_generator.py --once
```

`--once` を付けない場合は `PAPER_SIGNAL_CHECK_INTERVAL_SECONDS`(既定900秒)ごとにループする常駐プロセスとして動作する。

### 29.11 本番常駐化(v1.5以降の検討: systemd timer案)

v1.4.0時点ではsystemd化を行わない。将来検討する場合は、`macd_alert_bot.py`のような常駐daemon(systemd service)ではなく、**systemd timer + oneshot service** による定期実行を推奨する。理由は、Paper Signalが「一定間隔での市場状態スナップショット判定」であり、常駐プロセスを維持する必要性が薄いため。

```ini
# /etc/systemd/system/berial-paper-signal.service (案、oneshot)
[Unit]
Description=Berial Paper Signal Generator (oneshot)

[Service]
Type=oneshot
WorkingDirectory=/opt/berial/paper-signal
ExecStart=/usr/bin/python3 /opt/berial/paper-signal/signal_generator.py --once
```

```ini
# /etc/systemd/system/berial-paper-signal.timer (案)
[Unit]
Description=Run Berial Paper Signal Generator every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
```

これはv1.4.0では実装しない案であり、実際のunit配置・有効化はv1.5以降の検討事項とする。

### 29.12 v1.5以降の検討

- systemd timer化(29.11の案を実装する)
- Paper Signalの結果検証(仮説が実際の値動きとどう対応したかの事後集計。発注は行わない)
- 複数時間足・複数通貨ペアへの対応
- Entry Bot / Pepperoni(まだ設計しない)

これらはv1.5以降の検討事項であり、v1.4.0では実装しない。
