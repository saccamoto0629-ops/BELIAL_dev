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
| Alert | 事実を検知して通知する | MACD cross, MA touch, Bollinger upper_break |
| Strategy / Method | Alert の情報をどう解釈するか判断する | トレンド相場では順張り候補、レンジ相場では過熱警戒 |
| Execution | 実際の注文、Exit、損切り、利確を担当する | 現行スコープ外 |

Alert は売買判断をしない。Strategy / Method は複数の観測情報を文脈化する。Execution は実際の発注とポジション管理を担当するが、現行実装には含めない。

## データ連携概要

- `market-watch/chart_collector.py` はローソク足を取得し、SQLiteへ保存する。
- `market-watch/indicators.py` は共通指標計算を担当する。
- `market-watch/chart_snapshot.py` は状態表示と Market Snapshot 向け summary を作る。
- `market-watch/*_alert_bot.py` はイベント通知を担当する。
- `news-feed-bot/market_bias.py` はMarket Biasを計算する。
- `paper-signal/signal_generator.py` は paper_only 仮説を生成する。

## 関連ドキュメント

- [Market Snapshot](market_snapshot.md)
- [Market Watch](market_watch.md)
- [Market Bias](market_bias.md)
- [Paper Signal](paper_signal.md)
- [Operations](operations.md)
