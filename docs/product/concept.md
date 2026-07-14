# Concept

## 目的

BELIAL は、ニュース、マーケットデータ、チャート指標を観測し、市場状態を整理するためのローカル監視システムである。現行実装は、RSSニュース監視、Market Snapshot、Market Watch、Market Bias、Paper Signal を組み合わせ、市場状態の可視化と仮説生成(Paper Signal)までを扱う。生成した仮説を過去データで遡及検証する Backtest(検証基盤)も実装済みだが、これは後述の Execution(執行)フローとは独立した Validation レイヤーである。

## 基本方針

- 観測、分類、仮説生成、執行を分ける。執行(Execution)はさらに **Paper Execution**(仮想約定・仮想ポジション管理による損益評価。**実装済み** = `paper-execution/`)と **Live Execution**(Broker API による実注文・実約定。**未実装**)に分ける。Paper Execution は仮想レコードのみを生成し、**Broker API 連携・実注文・自動売買は含まない**(それらは Live Execution の責務で未実装)。過去データでの検証は、この執行フローとは独立した Backtest(検証基盤)が担う。
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
- Backtest(検証基盤): 過去のローソク足を使って Paper Signal を遡及検証する。Execution フローには含まれない独立した Validation レイヤー

## 非スコープ

- 自動エントリー
- 自動Exit
- 損切り判断
- 利確判断
- ロット計算
- Broker API 連携
- 実発注
- 売買推奨文言の生成

上記は実売買(自動売買)に踏み込む領域であり、**Live Execution**(Broker API による実注文・実約定。**未実装**)の担当として現行スコープ外である。なお、仮想約定・仮想ポジション管理・仮想Exit・仮想損益(「もしこの仮説どおり執行していたら」のシミュレーション)は **Paper Execution**(`paper-execution/`)として**実装済み**だが、これは仮想レコードのみを生成し、**実発注・Broker API 連携・ロット計算・売買推奨文言の生成は含まない**。Backtest は過去データで仮説を遡及検証する独立した検証基盤であり、`backtest/execution_model.py` の spread / slippage / fill は Backtest 内部のシミュレーションモデルにすぎない(Paper Execution レイヤーの実装ではない)。

## 設計思想

BELIAL は、いきなり執行判断へ進まない。まず市場を観測し、状態を分類し、仮説を残す。仮説を仮想約定・仮想ポジション・仮想損益へ変換する **Paper Execution** は実装済み(`paper-execution/`)だが、Broker API による実注文・実約定を行う **Live Execution** は未実装である。いずれの執行も Market Watch や Paper Signal では扱わない。仮説の妥当性を過去データで確かめる **Backtest** は、これらの執行層とは独立した検証基盤(Validation)である。

## レイヤー分離

処理は次の直列フローで進む。現時点では Paper Signal と **Paper Execution**(仮想約定・仮想ポジション管理・仮想Exit・仮想損益)までが実装済みで、Broker API による **Live Execution** は未実装である。

```text
Observation
    ↓
Classification
    ↓
Alert
    ↓
Hypothesis / Paper Signal
    ↓
Execution variants (Future)
    ├─ Paper Execution
    └─ Live Execution
```

| Layer | Responsibility | Status / Current modules |
|---|---|---|
| Observation | 価格、ニュース、指標を取得する | 実装済み: `news-feed-bot/`, `market-watch/chart_collector.py` |
| Classification | 市場状態を分類する | 実装済み: `market-watch/indicators.py`, `market_bias.py` |
| Alert | 事実を通知する | 実装済み: `macd_alert_bot.py`, `ma_alert_bot.py`, `bollinger_alert_bot.py`, `rsi_alert_bot.py` |
| Hypothesis (Paper Signal) | paper_only の仮説を生成・保存する。entry / exit は判断しない | 実装済み: `paper-signal/signal_generator.py` |
| Paper Execution | Paper Signal を入力に、時間進行に沿って仮想約定・仮想ポジション管理・Exit・損益計算を行う | **実装済み**(`paper-execution/`)。設計は [Paper Execution](../architecture/paper_execution.md)(Issue #49)、実装は Issue #50 / #51。Broker API・実注文は含まない |
| Live Execution | Broker API による実注文・実約定・実ポジション管理 | 未実装(将来) |

Backtest はこの直列フローには含まれない独立した **Validation レイヤー(検証基盤)** であり、過去のローソク足で Paper Signal や将来の Execution 設計を遡及検証する。`backtest/execution_model.py` の spread / slippage / fill は Backtest 内部のシミュレーションモデルであって、Paper Execution レイヤーの実装ではない。
