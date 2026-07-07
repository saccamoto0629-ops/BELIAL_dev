# Concept

## 目的

BELIAL は、ニュース、マーケットデータ、チャート指標を観測し、市場状態を整理するためのローカル監視システムである。現行実装は、RSSニュース監視、Market Snapshot、Market Watch、Market Bias、Paper Signal を組み合わせ、市場状態の可視化と仮説生成までを扱う。

## 基本方針

- 観測、分類、仮説生成、執行を分ける。
- Webhook、API key、token などの秘密情報は `.env` に置き、Git管理しない。
- dry-run は Discord 送信、state 更新、DB作成などの副作用を避ける。
- 通知は「何が起きたか」を伝える。売買判断そのものは通知層に置かない。
- 既存の運用導線を壊さず、機能追加は最小差分で行う。

## 現行スコープ

- RSSニュース監視とDiscord通知
- Market Snapshot本文生成
- Chart Snapshot / Market Watch 指標表示
- MA / MACD / Bollinger Bands のイベント通知
- Market Bias の Bull / Bear / Neutral 分類
- Paper Signal の paper_only 仮説生成、保存、通知

## 非スコープ

- 自動エントリー
- 自動Exit
- 損切り判断
- 利確判断
- ロット計算
- Broker API 連携
- 実発注
- 売買推奨文言の生成

## 設計思想

BELIAL は、いきなり執行判断へ進まない。まず市場を観測し、状態を分類し、仮説を残す。実際の注文、Exit、損切り、利確は Execution 層の責務であり、Market Watch や Paper Signal では扱わない。

## レイヤー分離

| Layer | Responsibility | Current modules |
|---|---|---|
| Observation | 価格、ニュース、指標を取得する | `news-feed-bot/`, `market-watch/chart_collector.py` |
| Classification | 市場状態を分類する | `market-watch/indicators.py`, `market_bias.py` |
| Alert | 事実を通知する | `macd_alert_bot.py`, `ma_alert_bot.py`, `bollinger_alert_bot.py` |
| Hypothesis | paper_only の仮説を生成・保存する | `paper-signal/` |
| Execution | 実注文、Exit、損切り、利確を扱う | 現行スコープ外 |
