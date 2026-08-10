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
- [Broker Interface / FakeBroker](broker_interface.md)(Issue #73、Issue #79): 将来の Live Execution に向けた
  Broker 境界(共通インターフェース・共通モデル)と、外部通信を一切行わないテストダブル FakeBroker、
  および Demo 専用の OANDA Demo Adapter(Issue #79。Live endpoint は fail-closed で拒否)。
  実装は `broker/`。Live 接続・Execution 層への組み込み・Live Execution 本体は未実装のまま。
- [Execution Request / Order Persistence](execution_request.md)(Issue #74): Strategy / Signal から
  将来の Live Execution ユースケースへ渡す ExecutionRequest モデルと、注文状態・状態履歴の SQLite
  永続化基盤。注文状態は既存 `broker.OrderState` を唯一の正として再利用する。実装は `execution/`。
  Live Execution 本体・Broker API Adapter・実注文・reconciliation runner は未実装のまま。
- [Safety Gate / Live Order Submission](safety_gate.md)(Issue #75): Broker 送信直前の fail-closed
  検証層、判定の SQLite 監査保存、および唯一の公認送信経路 `LiveOrderSubmissionService`。
  Strategy / Broker Adapter から独立し、判定の耐久保存 + read-back と execution store の
  created→submitting 遷移(一度きり)に成功した呼び出しだけが `Broker.submit_order()` へ到達する。
  実装は `execution/{safety_gate,safety_store,live_submission}.py`。実発注・Live 接続は引き続き未実装。
- [Submission Journal](submission_journal.md)(Issue #93): Broker 送信前後の送信試行を Execution Order DB と
  **独立した耐久媒体**へ記録する Submission Journal。Broker 送信前の Journal 保存失敗は fail-closed
  (送信されない)、Broker 応答後の Execution DB 保存失敗は `reconciliation_required` として Journal に残り、
  Execution DB 全損時・プロセス再起動後も未解決送信を列挙できる。クラッシュ境界ごとの再送 / 照会 /
  reconciliation の扱いを定義する。実装は `execution/submission_journal.py`。
- [Execution Intent Reissue](execution_reissue.md)(Issue #95 設計・未実装): `REJECTED` で終端した
  Execution Request の注文意図を、明示的な新 intent(新 `execution_request_id` / 新 `client_order_id` +
  親子関係の永続化)として再発行するモデル。状態機械は不変(REJECTED は terminal のまま)で、
  再発行可否は Broker 中立の正規化拒否分類で決める。schema v5・API の実装は後続 Issue。
- [Shadow Mode / Shadow Execution](shadow_execution.md)(Issue #78): 実 Signal・実設定・Safety Gate を
  通常の Production 経路で実行しつつ Broker へは一切送信せず、生成された注文候補と Gate 判定を
  検証記録として保存する Shadow Mode。execution mode(`dry-run` / `shadow` / `fake` / `demo` / `live`)の
  統一語彙と validation、Shadow 専用 store、CLI(`shadow_runner` / `shadow_report`)を含む。
  仮想約定・仮想 PnL は行わない(Paper Execution とは別物)。Demo / Live は未実装のまま。
