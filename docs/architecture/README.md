# Architecture

BELIAL の設計資料を置く。

- 全体構成
- コンポーネント責務
- データベース設計
- Market Watch / Market Bias / Paper Signal / Paper Execution などの現行仕様

## 設計文書

- [Strategy 定義の外部化仕様](strategy_definition.md)(Issue #52): Strategy ごとの可変設定を外部
  ファイルへ切り出すためのファイル形式・スキーマ・エラー方針・後方互換性方針。サンプル定義は
  [examples/strategy_squeeze_breakout_v1.yaml](examples/strategy_squeeze_breakout_v1.yaml)。
- [Paper Execution](paper_execution.md)(Issue #49 設計 / #50・#51 実装): 仮想ポジションの状態遷移、
  Entry / Close / SL / TP 約定規則、Position Store と schema migration。実装は `paper-execution/`。
- [Broker Interface / FakeBroker](broker_interface.md)(Issue #73): 将来の Live Execution に向けた
  Broker 境界(共通インターフェース・共通モデル)と、外部通信を一切行わないテストダブル FakeBroker。
  実装は `broker/`。実発注・OANDA Adapter・Live Execution は未実装のまま。
- [Execution Request / Order Persistence](execution_request.md)(Issue #74): Strategy / Signal から
  将来の Live Execution ユースケースへ渡す ExecutionRequest モデルと、注文状態・状態履歴の SQLite
  永続化基盤。注文状態は既存 `broker.OrderState` を唯一の正として再利用する。実装は `execution/`。
  Live Execution 本体・Broker API Adapter・実注文・reconciliation runner は未実装のまま。
- [Safety Gate / Live Order Submission](safety_gate.md)(Issue #75): Broker 送信直前の fail-closed
  検証層、判定の SQLite 監査保存、および唯一の公認送信経路 `LiveOrderSubmissionService`。
  Strategy / Broker Adapter から独立し、判定の耐久保存 + read-back と execution store の
  created→submitting 遷移(一度きり)に成功した呼び出しだけが `Broker.submit_order()` へ到達する。
  実装は `execution/{safety_gate,safety_store,live_submission}.py`。実発注・Live 接続は引き続き未実装。
