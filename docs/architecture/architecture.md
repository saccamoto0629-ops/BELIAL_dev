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

| Layer | Responsibility | Example |
|---|---|---|
| Alert | 事実を検知して通知する | MACD cross, MA touch, Bollinger upper_break, RSI14 overbought/oversold |
| Strategy / Method | Alert の情報をどう解釈するか判断する | トレンド相場では順張り候補、レンジ相場では過熱警戒 |
| Execution | 実際の注文、Exit、損切り、利確を担当する | 現行スコープ外 |

Alert は売買判断をしない。Strategy / Method は複数の観測情報を文脈化する。Execution は実際の発注とポジション管理を担当するが、現行実装には含めない。

この3層は、[Concept](../product/concept.md) の Observation / Classification / Alert / Hypothesis / Execution の5層分離のうち、Alert 以降を実装観点で細分化したものである。Market Bias / `indicators.py` が Classification、`paper-signal/` が Hypothesis(paper_only 仮説)にあたる。層の対応は以下の通り。

| Concept (5層) | Architecture (3層) | 現行モジュール |
|---|---|---|
| Observation | (Alert以前の入力) | 各Provider, `chart_collector.py` |
| Classification | (Alert以前の入力) | `indicators.py`, `market_bias.py` |
| Alert | Alert | `macd_alert_bot.py` / `ma_alert_bot.py` / `bollinger_alert_bot.py` / `rsi_alert_bot.py` |
| Hypothesis | Strategy / Method に相当する仮説生成の実装 | `paper-signal/signal_generator.py` |
| Execution | Execution | 現行スコープ外 |

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
- [Operations](../operations/operations.md)
- [Roadmap(v1.4 Backtest)](../product/roadmap.md)
