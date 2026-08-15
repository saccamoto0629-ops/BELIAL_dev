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
- [Broker Interface / FakeBroker](broker_interface.md)(Issue #73、#79、#82): Live Execution に向けた
  Broker 境界(共通インターフェース・共通モデル)と、外部通信を一切行わないテストダブル FakeBroker、
  Demo 専用の OANDA Demo Adapter(Issue #79。Live endpoint は fail-closed で拒否)、および
  Live 専用の OANDA Live Adapter(Issue #82。全ロック成立時のみ構築可能)。実装は `broker/`。
  Demo Adapter は Issue #132 で Execution 層へ統合され(`BROKER_TYPE_BY_MODE` への `demo` 登録 +
  唯一の composer `execution/demo_lock.py`)、runner から Demo 口座へ実送信できる。
  継続運転の運用導線は未整備で、実口座での Live 運用は未解禁。
- [Execution Request / Order Persistence](execution_request.md)(Issue #74。schema v5 = #95 / #104 / #115):
  Strategy / Signal から Live Execution ユースケースへ渡す ExecutionRequest モデルと、注文状態・状態履歴・
  再発行チェーンの SQLite 永続化基盤。注文状態は既存 `broker.OrderState` を唯一の正として再利用する。
  実装は `execution/`。これを駆動する runner core は Issue #131 / #132 で `fake` / `shadow` / `demo` まで実装済み。
- [Safety Gate / Live Order Submission](safety_gate.md)(Issue #75、#81、#82、#99): Broker 送信直前の
  fail-closed 検証層、判定の SQLite 監査保存、唯一の公認送信・キャンセル経路 `LiveOrderSubmissionService`、
  永続 Kill Switch(Issue #81)、および Live 接続の二重以上のロック + 起動前 preflight(Issue #82)。
  Strategy / Broker Adapter から独立し、判定の耐久保存 + read-back と execution store の
  created→submitting 遷移(一度きり)に成功した呼び出しだけが `Broker.submit_order()` へ到達する。
  実装は `execution/{safety_gate,safety_store,live_submission,kill_switch_store,kill_switch_cli,live_lock,live_startup_cli}.py`。
- [Submission Journal](submission_journal.md)(Issue #93、#99): Broker 送信・キャンセル前後の試行を
  Execution Order DB と**独立した耐久媒体**へ記録する Submission / Cancellation Journal。Broker 送信前の
  Journal 保存失敗は fail-closed(送信されない)、Broker 応答後の Execution DB 保存失敗は
  `reconciliation_required` として Journal に残り、Execution DB 全損時・プロセス再起動後も未解決送信を
  列挙できる。クラッシュ境界ごとの再送 / 照会 / reconciliation の扱いを定義する。
  実装は `execution/submission_journal.py`。
- [Execution Intent Reissue](execution_reissue.md)(Issue #95 設計。#114〜#117 で実装済み): `REJECTED` で
  終端した Execution Request の注文意図を、明示的な新 intent(新 `execution_request_id` / 新
  `client_order_id` + 親子関係の永続化)として再発行するモデルと、その clearance(観測にもとづく
  再発行許可)・手動再発行 / 訂正差し替え / 自動再試行(既定無効)の登録ユースケース。状態機械は不変
  (REJECTED は terminal のまま)で、再発行可否は Broker 中立の正規化拒否分類(Issue #114)で決める。
  実装は `execution/{reissue_service,reissue_clearance,reissue_composition,reissue_evidence}.py` + execution store schema v5。
- [Broker Reconciliation](broker_reconciliation.md)(Issue #103〜#105): 内部注文状態と Broker 状態の
  差分検出(#103)、`auto_repair_candidate` に限定した注文状態の安全な修復(#104)、position / trade /
  SL・TP スコープの検出(#105。`manual_review` のみ・修復経路なし)、および OANDA Demo 専用の
  手動 reconciliation CLI(`python -m execution.reconciliation_runner`)。scheduler への組み込みは未実装。
- [Shadow Mode / Shadow Execution](shadow_execution.md)(Issue #78): 実 Signal・実設定・Safety Gate を
  通常の Production 経路で実行しつつ Broker へは一切送信せず、生成された注文候補と Gate 判定を
  検証記録として保存する Shadow Mode。execution mode(`dry-run` / `shadow` / `fake` / `demo` / `live`)の
  統一語彙と validation、Shadow 専用 store、CLI(`shadow_runner` / `shadow_report`)を含む。
  仮想約定・仮想 PnL は行わない(Paper Execution とは別物)。
