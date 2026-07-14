# Architecture

## 全体構成

BELIAL は以下の主要レイヤーで構成する。

```text
RSS News Monitor
  -> Market Snapshot

Market Watch
  -> Chart Snapshot
  -> Market Bias
  -> Event Alerts

Market Bias + Chart Snapshot
  -> Paper Signal Generator

Paper Signal
  -> paper_only records / Discord notification
```

## コンポーネント関係

| Component | Role | Output |
|---|---|---|
| RSSニュース監視 | Google News / Reuters / Yahoo系RSSを取得し重要ニュースを通知 | Discord news alerts |
| Market Snapshot | Provider出力を統合し定時レポートを生成 | Market Snapshot message |
| Market Watch | ローソク足、SMA / EMA / Bollinger Bands / MACD / RSI14を扱う | Chart Snapshot / Event alerts |
| Market Bias | Market Snapshot と Chart Snapshot から Bull / Bear / Neutral を分類 | market_bias, confidence, reasons |
| Paper Signal | Market Bias と Chart Snapshot から paper_only 仮説を生成 | SQLite record / Discord notification |

## Alert / Strategy / Execution の責務分離

Execution は **Paper Execution**(仮想約定・仮想ポジション管理・Exit・損益計算を時間進行に沿って行う。**実装済み** = `paper-execution/`)と **Live Execution**(Broker API による実注文・実約定の**未実装**レイヤー)に分かれる。Paper Execution は仮想レコードのみを生成し、Broker API 連携・実注文・自動売買は含まない(それらは Live Execution の責務で未実装)。

| Layer | Responsibility | Status |
|---|---|---|
| Alert | 事実を検知して通知する | 実装済み(MACD cross, MA touch, Bollinger upper_break, RSI14 overbought/oversold) |
| Strategy / Method | Alert の情報をどう解釈するか判断する | 実装済み(Paper Signal のルールベース候補生成) |
| Paper Execution | Paper Signal を入力に、仮想約定・仮想ポジション管理・Exit・損益計算を行う | **実装済み**(`paper-execution/`)。設計 [Paper Execution](paper_execution.md)(Issue #49)、実装 Issue #50 / #51。Broker API・実注文は含まない |
| Live Execution | Broker API による実際の注文・約定・ポジション管理 | 未実装(将来) |

Alert は売買判断をしない。Strategy / Method は複数の観測情報を文脈化する。Paper Execution は実装済み(`paper-execution/`、仮想約定のみ)だが、Live Execution(Broker API 実注文)は現行実装に含めない。

Backtest はこの責務分離の直列フローには含まれない独立した **Validation レイヤー(検証基盤)** であり、過去データで Paper Signal や将来の Execution 設計を遡及検証する。`backtest/execution_model.py` の spread / slippage / fill は Backtest 内部のシミュレーションモデルであって、Paper Execution レイヤーの実装ではない(詳細は [Backtest](../backtest.md))。

この分離は、[Concept](../product/concept.md) の Observation / Classification / Alert / Hypothesis / Execution の5層分離のうち、Alert 以降を実装観点で細分化したものである(Execution は Paper Execution / Live Execution に分かれる)。Market Bias / `indicators.py` が Classification、`paper-signal/signal_generator.py` が Hypothesis(Paper Signal, paper_only 仮説)にあたる。層の対応は以下の通り。

| Concept (5層) | Architecture | 状態 / 現行モジュール |
|---|---|---|
| Observation | (Alert以前の入力) | 各Provider, `chart_collector.py` |
| Classification | (Alert以前の入力) | `indicators.py`, `market_bias.py` |
| Alert | Alert | `macd_alert_bot.py` / `ma_alert_bot.py` / `bollinger_alert_bot.py` / `rsi_alert_bot.py` |
| Hypothesis | Strategy / Method に相当する仮説生成(Paper Signal)の実装 | `paper-signal/signal_generator.py`(実装済み) |
| Execution | Paper Execution | 実装済み(`paper-execution/`。仮想約定のみ、Broker API・実注文は未実装) |
| Execution | Live Execution | 未実装(将来) |

## データ連携概要

- `market-watch/chart_collector.py` はローソク足を取得し、SQLiteへ保存する。
- `market-watch/indicators.py` は共通指標計算を担当する。
- `market-watch/chart_snapshot.py` は状態表示と Market Snapshot 向け summary を作る。
- `market-watch/*_alert_bot.py`(macd / ma / bollinger / rsi)はイベント通知を担当する。
- `news-feed-bot/market_bias.py` はMarket Bias(trend / risk / volatility)を計算する。
- `news-feed-bot/market_bias_store.py` はMarket Biasの評価結果をSQLiteへ永続化し、時点参照APIを提供する。
- `paper-signal/signal_generator.py` は paper_only 仮説を生成する。

## DB構成

BELIAL はコンポーネントごとに独立したSQLiteファイルを持つ。共有DBはない。

| DB | パス(既定) | 所有コンポーネント | 主なテーブル |
|---|---|---|---|
| Chart DB | `market-watch/data/chart_data.sqlite3` | `market-watch/` | `candles`, `chart_snapshots`, `alerts`, `alert_chart_snapshots` |
| Paper Signal DB | `paper-signal/data/paper_signals.sqlite3` | `paper-signal/` | `paper_signals` |
| Market Bias DB | `news-feed-bot/data/market_bias.sqlite3` | `news-feed-bot/` | `market_bias` |

いずれも `data/` はGit管理しない。関連付けは時刻のみに依存せず、可能な限りIDで行う(例: `alerts.snapshot_id` → `chart_snapshots.id`)。時刻は補助情報として扱う。詳細スキーマは [Market Watch](market_watch.md)、[Market Bias](market_bias.md)、[Paper Signal](paper_signal.md) を参照。

## dry-run 方針

すべてのBot/生成処理は `--dry-run`(または `dry_run=True`)で以下の副作用を発生させない。

- Discord送信
- state ファイル更新
- DB作成・更新
- S3アップロード

詳細と起動コマンドは [Operations](../operations/operations.md) を参照。

## 関連ドキュメント

- [Market Snapshot](market_snapshot.md)
- [Market Watch](market_watch.md)
- [Market Bias](market_bias.md)
- [Paper Signal](paper_signal.md)
- [Paper Execution(最小設計)](paper_execution.md)
- [Operations](../operations/operations.md)
- [Roadmap(v1.4 Backtest)](../product/roadmap.md)
