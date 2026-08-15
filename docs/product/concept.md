# Concept

## 目的

BELIAL は、ニュース、マーケットデータ、チャート指標を観測し、市場状態を分類し、売買仮説の生成・検証・仮想執行までを一貫して扱うローカル検証基盤である。単なる通知システムではなく、次の5つの活動を担う。

1. **市場観測(Observation)**: RSSニュース監視、Market Snapshot、ローソク足・指標の収集
2. **分類(Classification)**: Market Bias(Bull / Bear / Neutral)と Market State(trend_up / trend_down / range / squeeze / volatile / unknown)の判定
3. **戦略評価(Strategy / Method)**: 観測・分類結果をどの条件でエントリー候補とするかのルールを評価
4. **仮説記録(Hypothesis / Paper Signal)**: Strategy / Method の評価結果を paper_only 仮説として生成・保存・通知
5. **検証(Backtest)**: 過去のローソク足上で Strategy(シグナル生成ルール)と Execution ルール(Exit Policy / 仮想約定モデル)を再生・評価する検証基盤
6. **仮想執行(Paper Execution)**: Paper Signal を入力に、時間進行に沿って仮想Entry、仮想ポジション管理、SL / TP / 最大保有時間による Exit 判定、仮想損益の確定を行う

Broker API による実注文・実約定(**Live Execution**)は、v1.5 で基盤(Broker 境界・Adapter・Safety Gate・送信境界・reconciliation・Kill Switch・Live 接続の二重以上のロック)まで、v1.6 の Issue #131 で **runner core が Fake / Shadow に限って**実装済みだが、**実 Broker(OANDA Demo / Live)への runner 統合・自動売買は未実装**であり、**実口座での Live 運用は未解禁**である(下記「非スコープ」)。

## 基本方針

- 観測、分類、Alert、仮説生成、執行、永続化の責務を層として分ける(下記「レイヤー分離」)。
- 執行(Execution)は **Paper Execution**(仮想約定・仮想ポジション管理による損益評価。**実装済み** = `paper-execution/`)と **Live Execution**(Broker API による実注文・実約定。**基盤実装済み・runner core は Fake / Shadow のみ・運用未解禁** = `broker/` + `execution/`)に分ける。Paper Execution は仮想レコードのみを生成し、Broker API・Live Execution 基盤のコードを一切使わない。
- 過去データでの検証は、リアルタイムの執行フローとは独立した **Backtest**(検証基盤)が担う。Backtest と Paper Execution は約定規則の語彙・保守的方針(同一足での SL 優先、不利方向コスト)を共有し、結果を突合可能に保つ。
- Webhook、API key、token などの秘密情報は `.env` に置き、Git管理しない。
- dry-run は Discord 送信、state 更新、DB作成・migration などの副作用を発生させない。
- 通知は「何が起きたか」を伝える。売買判断そのものは通知層に置かない。
- 既存の運用導線を壊さず、機能追加は最小差分で行う。

## 現行スコープ

- RSSニュース監視とDiscord通知
- Market Snapshot本文生成
- Chart Snapshot / Market Watch 指標表示
- MA / MACD / Bollinger Bands / RSI のイベント通知
- Market Bias の Bull / Bear / Neutral 分類と永続化
- Paper Signal の paper_only 仮説生成、保存、通知(常駐サービス・日次集計レポート含む)
- Paper Execution: 保存済み Paper Signal を入力とした仮想Entry / 仮想ポジション管理(Position Store)/ SL・TP・最大保有時間による Exit 判定 / 仮想損益の記録
- Backtest: 過去のローソク足上で Strategy / Execution ルールを再生・評価する検証基盤(Runner / CLI / summary)
- Strategy 定義の外部化(YAML)と validation(現在は Backtest 経路のみが使用)
- Live Execution に向けた基盤(`broker/` + `execution/`): Broker 境界 / FakeBroker(#73)、ExecutionRequest・注文状態永続化(#74 / #95 / #115)、Safety Gate と唯一の送信・キャンセル境界(#75 / #99)、Shadow Mode(#78)、OANDA Demo Adapter(#79)、Kill Switch(#81)、Submission / Cancellation Journal(#93 / #99)、Broker reconciliation の検出・修復・position スコープと手動 CLI(#103〜#105)、再発行系安全機構(#102 / #114 / #116 / #117)、OANDA Live Adapter と Live 接続の二重以上のロック(#82)。**いずれも fail-closed の検証・監査・安全機構であり、実発注を駆動する定期実行 runner は含まない**
- SQLite の初期化・既知migration・schema検証(保証範囲はコンポーネントごとに異なる。`position_store` は実schema検証とtable rebuild、`backtest_store` は列追加とschema検証を行う一方、他storeは既知列の追加や `CREATE ... IF NOT EXISTS` が中心)
- dry-run による副作用抑制(全Bot・生成処理共通)

## 非スコープ

以下は実売買(**Live Execution**)の運用に踏み込む領域であり、現行スコープ外である。

- **実口座での Live 運用開始** — OANDA Live Adapter と Live 接続の二重以上のロック(Issue #82)が merge 済みであっても、それだけでは開始しない。[運用ドキュメント](../operations/operations.md) の「Live 運用開始の事前条件」(FakeBroker 固定シナリオ試験の安定 / Shadow Mode での想定注文確認 / Demo 口座での継続運転 / Broker reconciliation 稼働 / Kill Switch 動作確認 / 発注数量・許可銘柄・リスク上限の設定)をすべて満たしたうえで、別途運用判断する
- **実発注を駆動する定期実行 runner・常駐プロセス** — Broker API Adapter(Demo: Issue #79 / Live: Issue #82。いずれも endpoint allowlist で 1 URL に固定)と実発注経路(`LiveOrderSubmissionService` の単一境界)に加え、Issue #131 で runner core(1 cycle API・常駐ループ境界・graceful shutdown・fail-closed)も実装済みだが、**駆動できるのは `fake` / `shadow` のみ**で `demo` / `live` は明示拒否される。実 Broker へ実発注を駆動するプロセス(OANDA Demo 統合・reconciliation の定期実行・CLI / systemd 等の運用導線)は未実装
- 自動売買(実口座での自動エントリー・自動Exit・損切り・利確)
- ロット計算・レバレッジ計算・資金管理
- 売買推奨文言の生成

Paper Execution が行う仮想Entry / 仮想Exit / SL / TP 判定は、仮想レコード上のシミュレーションであり、上記の実売買には該当しない。`stop_loss_price` / `take_profit_price` は判定用のマーカーであって broker への注文ではない。また、`backtest/execution_model.py` の spread / slippage / fill は Backtest 内部のシミュレーションモデルであり、実注文の実装ではない。

## 設計思想

BELIAL は、いきなり実売買へ進まない。まず市場を観測し、状態を分類し、仮説を残す。そのうえで、仮説と執行ルールを過去データ(Backtest)と時間進行(Paper Execution)の両面から検証する。

- **Paper Signal は entry / exit を決めない。** 仮説の記録・通知だけを行う。
- **Paper Execution は仮説を仮想約定・仮想ポジション・仮想損益へ変換する。** 実装済み(`paper-execution/`)だが、生成するのは仮想レコードのみである。
- **Backtest は過去データ上の再生・評価に徹する。** リアルタイムの執行フローには介在しない独立した Validation レイヤーである。
- **Live Execution は設計上も完全に別レイヤーとして扱う。** Paper Execution のコード・データ・設定を実注文経路に転用しない。Live Execution の基盤 — Broker 境界と FakeBroker(Issue #73)、ExecutionRequest と注文状態永続化(Issue #74)、発注前の fail-closed Safety Gate と唯一の送信境界(Issue #75)、OANDA Demo / Live Adapter(Issue #79 / #82)、Kill Switch(Issue #81)、Submission Journal(Issue #93)、Broker reconciliation(Issue #103〜#105)、Live 接続の二重以上のロック(Issue #82)— は `broker/` / `execution/` に実装済みである([Broker Interface](../architecture/broker_interface.md) / [Execution Request / Order Persistence](../architecture/execution_request.md) / [Safety Gate](../architecture/safety_gate.md) / [Broker Reconciliation](../architecture/broker_reconciliation.md))。ただし**実発注を駆動する定期実行 runner・自動売買は未実装**であり、Live 接続は全ロック成立時のみ構築できる。**Live 運用開始は別途の運用判断**である。

## レイヤー分離

処理は次の直列フローで進む。Paper Execution までが実装済み・運用中である。Live Execution は基盤(Broker 境界・Adapter・Safety Gate・Live 接続ロック)まで実装済みだが、常駐 runner が未実装のためリアルタイムフローとしては駆動されず、実口座での運用も未解禁である。

```text
Observation
    ↓
Classification (Market Bias / Market State)
    ↓
Alert                       … 事実の通知(売買判断なし)
    ↓
Strategy / Method           … 候補生成・評価ルール
  ↓
Hypothesis / Paper Signal   … Strategy / Method の評価結果を記録した paper_only 仮説
    ↓
Execution
    ├─ Paper Execution      … 実装済み(仮想約定のみ)
    │      ↓
    │  Virtual Position → Exit Evaluation → Execution Result(仮想損益)
    └─ Live Execution       … 基盤実装済み(Broker 境界・Adapter・Safety Gate・journal・
                              reconciliation・Kill Switch・Live 接続ロック =
                              Issue #73〜#82/#93/#99/#103〜#105)。常駐 runner・自動売買は
                              未実装で、実口座での Live 運用は未解禁
```

| Layer | Responsibility | Status / Current modules |
|---|---|---|
| Observation | 価格、ニュース、指標を取得する | 実装済み: `news-feed-bot/`, `market-watch/chart_collector.py` |
| Classification | 市場状態を分類する(Market Bias / Market State) | 実装済み: `market-watch/indicators.py`, `market-watch/market_state.py`, `news-feed-bot/market_bias.py` |
| Alert | 事実を通知する。売買判断はしない | 実装済み: `macd_alert_bot.py`, `ma_alert_bot.py`, `bollinger_alert_bot.py`, `rsi_alert_bot.py` |
| Strategy / Method | 観測・分類情報から候補を生成・評価するルール。Paper Signal の現行ルールはコード実装、外部 Strategy 定義(YAML)は現在 Backtest 経路のみで使用 | 実装済み: `paper-signal/signal_generator.py`, `backtest/strategy_definition.py` |
| Hypothesis / Paper Signal | Strategy / Method の評価結果を paper_only の仮説レコードとして生成・保存する。entry / exit は判断しない | 実装済み: `paper-signal/signal_generator.py`, `paper_signal_store.py` |
| Execution (Paper Execution) | Paper Signal を入力に、時間進行に沿って仮想Entry・仮想ポジション管理・SL / TP / Exit 判定・仮想損益計算を行う | **実装済み**: `paper-execution/`(設計は [Paper Execution](../architecture/paper_execution.md)。Issue #49 設計、#50 / #51 実装)。Broker API・実注文は含まない |
| Execution (Live Execution) | Broker API による実注文・実約定・実ポジション管理 | **常駐 runner・自動売買は未実装**。基盤は実装済み: Broker 境界(Issue #73)/ ExecutionRequest・注文状態永続化(Issue #74 / #95 / #115)/ Safety Gate・単一送信境界(Issue #75)/ Shadow Mode(Issue #78)/ OANDA Demo Adapter(Issue #79)/ Kill Switch(Issue #81)/ Submission・Cancellation Journal と公認キャンセル(Issue #93 / #99)/ Broker reconciliation(Issue #103〜#105)/ 再発行系安全機構(Issue #102 / #114 / #116 / #117)/ **OANDA Live Adapter と Live 接続の二重以上のロック(Issue #82)**。Live 接続は全ロック成立時のみ構築でき、**実口座での Live 運用は未解禁**([運用ドキュメント](../operations/operations.md) の事前条件による) |
| Persistence | 各コンポーネントが独立した SQLite を所有する。初期化・既知migration・schema検証の保証範囲はstoreごとに異なる | 実装済み: `chart_collector.py`, `market_bias_store.py`, `paper_signal_store.py`, `backtest_store.py`, `position_store.py`。詳細は [Architecture](../architecture/architecture.md#sqlite-schema-validation--migration) |

Backtest はこの直列フローには含まれない独立した **Validation レイヤー(検証基盤)** であり、過去のローソク足で Strategy / Execution ルールを遡及再生・評価する。Backtest と Paper Execution の役割差、および両者が共有するルールは [Architecture](../architecture/architecture.md#backtest-と-paper-execution-の役割差) を参照。
