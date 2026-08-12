# BELIAL

BELIAL は、ニュース・マーケットデータ・チャート指標を観測し、市場状態を分類し、`paper_only` の仮説を記録し、その仮説を Backtest(過去データでの検証)と Paper Execution(仮想約定・仮想ポジション管理)で検証するローカル検証基盤です。加えて v1.5 で、将来の Live Execution に向けた実装(Broker 境界 / OANDA Demo・Live Adapter / Safety Gate / Shadow Mode / reconciliation / Kill Switch / Live 接続の二重以上のロック)が `broker/` / `execution/` に入りました。ただし次の3つを混同しないでください。

1. **実装済み機能**: Broker 境界・Safety Gate・OANDA Demo / Live Adapter・reconciliation・Kill Switch・submission / cancellation journal・再発行(reissue)系の安全機構(下記「v1.5(現行 main)の収録範囲」)
2. **Live 接続コードとして存在するもの**: Live 用の接続・発注経路(`LiveOrderSubmissionService`)と安全ロック(Issue #82)。全ロック成立時のみ構築できる
3. **Live 運用開始**: **未解禁**。実口座での自動売買・定期実行する Live Execution runner は未実装であり、Live 運用は [Operations の「Live 運用開始の事前条件」](docs/operations/operations.md#live-運用開始の事前条件issue-82-の-merge-だけでは開始しない) をすべて満たしたうえで別途運用判断する(Issue #82 の merge だけでは開始しない)

本ドキュメントは、**v1.5 完了時点の main 開発状態**を反映しています。`v1.4.1` までの収録機能と v1.5 の差分は [docs/product/roadmap.md](docs/product/roadmap.md) および [v1.5 リリースノート](docs/development/reports/v1.5_release_notes.md) を参照してください。

## ドキュメント

すべての設計・運用ドキュメントは [`docs/`](docs/README.md) 配下にあります。

| ディレクトリ | 内容 |
|---|---|
| [docs/product/](docs/product/README.md) | 目的、現行スコープ、非スコープ、責務レイヤー、Roadmap |
| [docs/architecture/](docs/architecture/README.md) | 全体構成、コンポーネント責務、DB設計、各モジュールの現行仕様 |
| [docs/operations/](docs/operations/README.md) | 環境変数、起動手順、dry-run方針、systemd運用 |
| [Strategy 定義仕様](docs/architecture/strategy_definition.md) | Strategy ごとの可変設定を外部ファイルへ切り出すためのスキーマ・エラー方針(Issue #52 設計) |
| [docs/development/](docs/development/README.md) | Issue / PR 運用ルール |
| [Agent Skills](docs/development/agent-skills.md) | AIエージェント(Claude Code / Codex)向け Issue 実装 Skill の配置・利用方法(Issue #86) |
| [v1.5 リリースノート](docs/development/reports/v1.5_release_notes.md) | v1.4.1 との差分、v1.5 の収録範囲、既知制約、Live 運用未解禁の明記 |
| [v1.4.1 リリースノート](docs/development/reports/v1.4.1_release_notes.md) | タグ作成前に作成された候補スナップショット。`v1.4.1` タグは後に作成済み |
| [docs/testing/](docs/testing/test_specification.md) | テスト方針、レイヤー別テスト観点、重要テストケースとpytestの対応 |

最初に読むべきドキュメント:

1. [Concept](docs/product/concept.md) — 目的、基本方針、責務レイヤー、非スコープ
2. [Architecture](docs/architecture/architecture.md) — 全体構成、コンポーネント関係、DB構成
3. [Operations](docs/operations/operations.md) — 環境変数、dry-run方針、起動手順

## コンポーネント

| コンポーネント | 役割 | README |
|---|---|---|
| `news-feed-bot/` | RSSニュース監視、Market Snapshot、Market Bias分類 | [news-feed-bot/README.md](news-feed-bot/README.md) |
| `market-watch/` | ローソク足取得、指標計算、Chart Snapshot、イベントAlert | [market-watch/README.md](market-watch/README.md) |
| `paper-signal/` | Market Bias + Chart Snapshot から `paper_only` 仮説を生成 | [paper-signal/README.md](paper-signal/README.md) |
| `paper-execution/` | Paper Signal を入力に仮想ポジションを Open / Close し仮想損益を記録(仮想約定のみ。Broker API・実注文なし) | [Paper Execution 設計](docs/architecture/paper_execution.md) |
| `broker/` | Live Execution に向けた Broker 境界(共通インターフェース・共通モデル)と外部通信しないテストダブル FakeBroker(Issue #73)、OANDA Demo Adapter(Issue #79。fxpractice endpoint のみ許可)、OANDA Live Adapter(Issue #82。fxtrade endpoint のみ許可・全ロック成立時のみ構築可能)。定期実行する Live Execution runner は未実装で、Live 運用は未解禁 | [broker/README.md](broker/README.md) / [設計](docs/architecture/broker_interface.md) |
| `execution/` | Live Execution に向けた ExecutionRequest モデル・注文状態永続化(Issue #74。再発行チェーン込みの schema v5)、発注前の fail-closed Safety Gate + 判定監査保存(Issue #75)、唯一の送信・キャンセル境界 `LiveOrderSubmissionService`(Issue #75 / #99)、Shadow Mode(Issue #78)、Kill Switch(Issue #81)、Submission / Cancellation Journal(Issue #93 / #99)、Broker reconciliation(検出 #103 / 修復 #104 / position・SL・TP スコープ + 手動 CLI #105)、再発行系安全機構(#95 / #102 / #114〜#117)、Live 接続の二重以上のロック + 起動前 preflight CLI(Issue #82)。execution mode は `dry-run` / `shadow` / `fake` / `demo` / `live` の統一 validation(`demo` は mode 定義のみで Execution 層への組み込み未実装、`live` は全ロック成立時のみ)。定期実行する Live Execution runner は未実装 | [execution/README.md](execution/README.md) / [設計](docs/architecture/execution_request.md) / [Safety Gate](docs/architecture/safety_gate.md) / [Shadow Mode](docs/architecture/shadow_execution.md) / [Reconciliation](docs/architecture/broker_reconciliation.md) |

各コンポーネントは独立した `.env` / `requirements.txt`(共通は repo ルートの `requirements.txt`)/ `tests/` を持ちます。

## 責務分離(Observation → Classification → Alert → Strategy → Hypothesis → Execution)

| Layer | Responsibility | 現行モジュール |
|---|---|---|
| Observation | 価格、ニュース、指標を取得する | `news-feed-bot/market_data.py` など各Provider, `market-watch/chart_collector.py` |
| Classification | 市場状態を分類する | `market-watch/indicators.py`, `news-feed-bot/market_bias.py` |
| Alert | 事実を検知して通知する(売買判断はしない) | `market-watch/macd_alert_bot.py` / `ma_alert_bot.py` / `bollinger_alert_bot.py` / `rsi_alert_bot.py` |
| Strategy / Method | 観測・分類情報から候補を生成・評価するルール | `paper-signal/signal_generator.py`。外部YAML定義は現在Backtest経路のみ |
| Hypothesis / Paper Signal | Strategy / Method の評価結果を `paper_only` 仮説レコードとして生成・保存する | `paper-signal/signal_generator.py`, `paper_signal_store.py` |
| Execution | 執行層の総称。**Paper Execution**(仮想約定・仮想ポジション管理・仮想Exit・仮想損益)と **Live Execution**(実注文・約定確認・ポジション管理・Exit)に分かれる | **Paper Execution は実装済み**(`paper-execution/`。仮想約定のみ)。**Live Execution は基盤実装済み**(`broker/` + `execution/`: Broker 境界・Adapter・Safety Gate・送信境界・reconciliation・Kill Switch・Live 接続ロック)だが、**定期実行する runner・自動売買は未実装で、実口座での Live 運用は未解禁** |

詳細は [Concept のレイヤー分離](docs/product/concept.md#レイヤー分離)、Alert層の記述粒度は [Architecture の責務分離](docs/architecture/architecture.md#alert--strategy--signal--execution-の責務分離) を参照してください。

## DB構成

BELIAL はコンポーネントごとに独立した SQLite ファイルを持ちます。共有DBはありません。

| DB | パス(既定) | 所有コンポーネント | 用途 |
|---|---|---|---|
| Chart DB | `market-watch/data/chart_data.sqlite3` | `market-watch/` | ローソク足(UTC保存)、Chart Snapshot、Alert のメタデータ |
| Paper Signal DB | `paper-signal/data/paper_signals.sqlite3` | `paper-signal/` | `paper_only` シグナル記録 |
| Market Bias DB | `news-feed-bot/data/market_bias.sqlite3` | `news-feed-bot/` | Market Bias(trend/risk/volatility)の評価履歴。Backtest からの時点参照用 |
| Paper Position DB | `paper-execution/data/paper_positions.sqlite3` | `paper-execution/` | Paper Execution の仮想ポジション(状態・仮想損益)。上書きは `PAPER_POSITION_DB_PATH` |
| Execution Order DB | `execution/data/execution_orders.sqlite3` | `execution/` | Live Execution 用の execution request・注文状態・状態履歴・再発行チェーン(Issue #74 / #95 / #104 / #115。schema v5)。上書きは `EXECUTION_ORDER_DB_PATH` |
| Safety Gate Decision DB | `execution/data/safety_gate_decisions.sqlite3` | `execution/` | Safety Gate の allow / reject 判定の監査保存(Issue #75 / #116)。上書きは `SAFETY_GATE_DECISION_DB_PATH` |
| Submission Journal DB | `execution/data/submission_journal.sqlite3` | `execution/` | Broker 送信・キャンセル試行の耐久記録(Issue #93 / #99。Execution Order DB と独立。送信前の保存失敗は fail-closed)。上書きは `SUBMISSION_JOURNAL_DB_PATH` |
| Kill Switch DB | `execution/data/kill_switch.sqlite3` | `execution/` | Execution 層の永続 Kill Switch 状態(Issue #81。append-only 遷移履歴。読めない間は fail-closed で全新規送信拒否)。上書きは `KILL_SWITCH_DB_PATH` |
| Broker Reconciliation DB | `execution/data/broker_reconciliation.sqlite3` | `execution/` | 内部注文状態と Broker 状態の照合結果 + 修復監査(Issue #103〜#105)。上書きは `BROKER_RECONCILIATION_DB_PATH` |
| Shadow Result DB | `execution/data/shadow_results.sqlite3` | `execution/` | Shadow Mode の検証記録(Issue #78。Broker 送信なし・仮想約定なし。想定注文内容と Safety Gate 判定の監査保存)。上書きは `SHADOW_RESULT_DB_PATH` |

いずれも `data/` はGit管理しません。詳細スキーマは [Architecture](docs/architecture/architecture.md)、[Market Watch](docs/architecture/market_watch.md)、[Market Bias](docs/architecture/market_bias.md)、[Paper Signal](docs/architecture/paper_signal.md) を参照してください。

## dry-run 方針

すべてのBotは `--dry-run` で以下の副作用を発生させません。

- Discord送信
- state ファイル更新
- DB作成・更新
- S3アップロード

`--dry-run` はコンソール出力のみで確認モードとして動作します。詳細は [Operations](docs/operations/operations.md) を参照してください。

## リリース履歴と v1.5(現行 main)の収録範囲

v1.4系では、v1.3までに蓄積した Chart Snapshot / Market Bias の永続データをもとに、過去の市場状態を再生する Backtest 層(検証基盤)を追加しました。`v1.4.1` は、`v1.4` タグ以降に入ったBacktest / Paper Signal関連の修正・整理をまとめたリリース済みのパッチリリースです(Backtest Engine / Runner / CLI、Market State 局面別エントリー制御、銘柄別 Market Bias、XAUUSD squeeze breakout 戦略、Paper Signal の常駐サービス化・日次集計レポート)。Backtest は Paper Signal を過去データで遡及検証する **独立した Validation レイヤー**であり、Execution 層ではありません。

**v1.5(`v1.4.1` タグ後の開発)に含まれる系統:**

- **Paper Execution**(Issue #49〜#51 / #83 / #85): 保存済み Paper Signal からの仮想Entry / 仮想ポジション管理 / SL・TP・Exit 判定・仮想損益、Discord 通知(Transactional Outbox)、同時ポジション上限。仮想レコードのみで実発注なし。
- **外部 Strategy 定義(YAML)**(Issue #52〜#54): Backtest 経路のみで使用(`--strategy-file` / `--strategy`)。Paper Signal / Paper Execution との定義共有は未実装。
- **Broker interface / FakeBroker**(Issue #73)と接続 identity 契約(Issue #75)。
- **Execution Request / 注文状態永続化**(Issue #74。schema v5 = 再発行チェーン込み)+ 二重発注防止(Issue #76)。
- **発注前 Safety Gate + 判定監査保存 + 唯一の送信境界 `LiveOrderSubmissionService`**(Issue #75)。
- **固定シナリオ試験**(Issue #77。現在 48 シナリオ。`python -m execution.scenario_runner`)。
- **Shadow Mode**(Issue #78: Broker へ送信せず実判定経路を監査保存。`python -m execution.shadow_runner` / `shadow_report`)。
- **OANDA Demo Adapter**(Issue #79。Demo endpoint allowlist・fail-closed。Execution 層への組み込みは未実装)。
- **Kill Switch**(Issue #81。永続・fail-closed。`python -m execution.kill_switch_cli`)。
- **Submission / Cancellation Journal**(Issue #93 / #99。送信・キャンセル試行の耐久記録)と**公認キャンセル経路**(Issue #99)。
- **Order Reissue 系**(Issue #102 の Broker 側状態機械 / #114 の正規化拒否分類 / #95・#115 の再発行チェーン永続化 / #116 の clearance / #117 の自動再試行=既定無効)。
- **Broker reconciliation**(Issue #103 検出 / #104 修復 / #105 position・trade・SL・TP スコープ + 手動 CLI `python -m execution.reconciliation_runner`)。
- **Live 接続の二重以上のロック**(Issue #82。OANDA Live Adapter + `execution/live_lock.py` + 起動前 preflight `python -m execution.live_startup_cli`)。

**v1.5 に含まれないもの(未実装のまま):** 定期実行する Live Execution runner(実発注を駆動する常駐プロセス・自動売買)、`demo` mode の Execution 層統合、reconciliation の scheduler 組み込み、外部 Strategy 定義の Paper Signal / Paper Execution 共有。

**v1.5 完了 = Live 実口座運用開始ではありません。** Live 運用開始は [Operations の事前条件](docs/operations/operations.md#live-運用開始の事前条件issue-82-の-merge-だけでは開始しない)(FakeBroker 固定シナリオ試験の安定 / Shadow Mode での想定注文確認 / Demo 口座での継続運転 / reconciliation 稼働 / Kill Switch 動作確認 / 発注数量・許可銘柄・リスク上限の設定)をすべて満たしたうえでの別途の運用判断です。

詳細と非スコープは [Roadmap](docs/product/roadmap.md)、差分の一覧は [v1.5 リリースノート](docs/development/reports/v1.5_release_notes.md) を参照してください。

## 非スコープ(全レイヤー共通)

以下は実売買(Live Execution)の**運用**に踏み込む領域であり、行いません。

- 実口座での Live 運用開始(Issue #82 の merge だけでは開始しない。[Operations の事前条件](docs/operations/operations.md#live-運用開始の事前条件issue-82-の-merge-だけでは開始しない)を満たしたうえで別途判断)
- 自動売買(実口座での自動エントリー・自動Exit・損切り・利確)と、それを駆動する定期実行 runner
- ロット計算・レバレッジ計算・資金管理
- 売買推奨文言の生成

Broker API 連携(OANDA Demo / Live Adapter)と実発注経路(`LiveOrderSubmissionService`)は**コードとして実装済み**ですが、endpoint allowlist・Live 接続の二重以上のロック・Kill Switch により fail-closed で保護されており、これらを駆動する常駐プロセスは存在しません。Paper Execution が行う仮想 Entry / Exit / SL / TP 判定は仮想レコード上のシミュレーションであり、実売買には該当しません。詳細は [Concept の非スコープ](docs/product/concept.md#非スコープ) を参照してください。

## Setup

```bash
python -m pip install -r requirements.txt
```

各コンポーネントの `.env.example` をコピーして使います。

```bash
cp news-feed-bot/.env.example news-feed-bot/.env
cp market-watch/.env.example market-watch/.env
cp paper-signal/.env.example paper-signal/.env
cp paper-execution/.env.example paper-execution/.env   # PAPER_POSITION_DB_PATH (任意)
```

実際のWebhook URL、API key、tokenは `.env` にのみ置き、Gitにコミットしません。起動コマンドの詳細は各コンポーネントのREADME、または [Operations](docs/operations/operations.md) を参照してください。

## セキュリティ

- `.env`、`data/`、`state/`、`logs/`、`__pycache__/`、`.pytest_cache/` はGit管理しません(`.gitignore` 参照)。
- Webhook URL、API key、tokenの実値はドキュメント・READMEに記載しません。
