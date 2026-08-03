# BELIAL

BELIAL は、ニュース・マーケットデータ・チャート指標を観測し、市場状態を分類し、`paper_only` の仮説を記録し、その仮説を Backtest(過去データでの検証)と Paper Execution(仮想約定・仮想ポジション管理)で検証するローカル検証基盤です。**Broker API 連携、実発注、実約定、ロット計算、売買推奨文言の生成(Live Execution 系)は行いません。**

本ドキュメントは、**リリース済みの `v1.4.1` タグ以降の main 開発状態**を反映しています。`v1.4.1` には、過去データで仮説を遡及検証する Backtest(検証基盤)、Market State 局面別エントリー制御、銘柄別 Market Bias、XAUUSD squeeze breakout 戦略、Paper Signal の常駐サービス化と日次集計レポートが含まれます。その後の開発で、Paper Signal を仮想執行する Paper Execution(`paper-execution/`: 仮想Entry / 仮想ポジション管理 / SL・TP・Exit 判定)と、Backtest 経路で利用する外部 Strategy 定義(YAML)が追加されています。各機能のリリース時点・位置づけ・非スコープは [docs/product/roadmap.md](docs/product/roadmap.md) を参照してください。

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
| `broker/` | 将来の Live Execution 用の Broker 境界(共通インターフェース・共通モデル)と、外部通信しないテストダブル FakeBroker(Issue #73)。実発注・OANDA Adapter・Live Execution は未実装のまま | [broker/README.md](broker/README.md) / [設計](docs/architecture/broker_interface.md) |
| `execution/` | 将来の Live Execution 用の ExecutionRequest モデル・注文状態永続化基盤(Issue #74)と、発注前の fail-closed Safety Gate + 判定監査保存(Issue #75)、Broker へ送信せず実判定経路を検証記録する Shadow Mode(Issue #78。execution mode `dry-run` / `shadow` / `fake` / `demo` / `live` の統一 validation 含む)。注文状態は既存 `broker.OrderState` を再利用。Live Execution 本体・Broker API Adapter・実注文・reconciliation runner は未実装のまま(`demo` / `live` は mode 定義のみ) | [execution/README.md](execution/README.md) / [設計](docs/architecture/execution_request.md) / [Safety Gate](docs/architecture/safety_gate.md) / [Shadow Mode](docs/architecture/shadow_execution.md) |

各コンポーネントは独立した `.env` / `requirements.txt`(共通は repo ルートの `requirements.txt`)/ `tests/` を持ちます。

## 責務分離(Observation → Classification → Alert → Strategy → Hypothesis → Execution)

| Layer | Responsibility | 現行モジュール |
|---|---|---|
| Observation | 価格、ニュース、指標を取得する | `news-feed-bot/market_data.py` など各Provider, `market-watch/chart_collector.py` |
| Classification | 市場状態を分類する | `market-watch/indicators.py`, `news-feed-bot/market_bias.py` |
| Alert | 事実を検知して通知する(売買判断はしない) | `market-watch/macd_alert_bot.py` / `ma_alert_bot.py` / `bollinger_alert_bot.py` / `rsi_alert_bot.py` |
| Strategy / Method | 観測・分類情報から候補を生成・評価するルール | `paper-signal/signal_generator.py`。外部YAML定義は現在Backtest経路のみ |
| Hypothesis / Paper Signal | Strategy / Method の評価結果を `paper_only` 仮説レコードとして生成・保存する | `paper-signal/signal_generator.py`, `paper_signal_store.py` |
| Execution | 執行層の総称。**Paper Execution**(仮想約定・仮想ポジション管理・仮想Exit・仮想損益)と **Live Execution**(実注文・約定確認・ポジション管理・Exit)に分かれる | **Paper Execution は実装済み**(`paper-execution/`。仮想約定のみ)。**Live Execution**(Broker API・実注文・自動売買)は**未実装** |

詳細は [Concept のレイヤー分離](docs/product/concept.md#レイヤー分離)、Alert層の記述粒度は [Architecture の責務分離](docs/architecture/architecture.md#alert--strategy--signal--execution-の責務分離) を参照してください。

## DB構成

BELIAL はコンポーネントごとに独立した SQLite ファイルを持ちます。共有DBはありません。

| DB | パス(既定) | 所有コンポーネント | 用途 |
|---|---|---|---|
| Chart DB | `market-watch/data/chart_data.sqlite3` | `market-watch/` | ローソク足(UTC保存)、Chart Snapshot、Alert のメタデータ |
| Paper Signal DB | `paper-signal/data/paper_signals.sqlite3` | `paper-signal/` | `paper_only` シグナル記録 |
| Market Bias DB | `news-feed-bot/data/market_bias.sqlite3` | `news-feed-bot/` | Market Bias(trend/risk/volatility)の評価履歴。Backtest からの時点参照用 |
| Paper Position DB | `paper-execution/data/paper_positions.sqlite3` | `paper-execution/` | Paper Execution の仮想ポジション(状態・仮想損益)。上書きは `PAPER_POSITION_DB_PATH` |
| Execution Order DB | `execution/data/execution_orders.sqlite3` | `execution/` | 将来の Live Execution 用の execution request・注文状態・状態履歴(Issue #74。永続化基盤のみ・実注文なし)。上書きは `EXECUTION_ORDER_DB_PATH` |
| Shadow Result DB | `execution/data/shadow_results.sqlite3` | `execution/` | Shadow Mode の検証記録(Issue #78。Broker 送信なし・仮想約定なし。想定注文内容と Safety Gate 判定の監査保存)。上書きは `SHADOW_RESULT_DB_PATH` |

いずれも `data/` はGit管理しません。詳細スキーマは [Architecture](docs/architecture/architecture.md)、[Market Watch](docs/architecture/market_watch.md)、[Market Bias](docs/architecture/market_bias.md)、[Paper Signal](docs/architecture/paper_signal.md) を参照してください。

## dry-run 方針

すべてのBotは `--dry-run` で以下の副作用を発生させません。

- Discord送信
- state ファイル更新
- DB作成・更新
- S3アップロード

`--dry-run` はコンソール出力のみで確認モードとして動作します。詳細は [Operations](docs/operations/operations.md) を参照してください。

## v1.4.1 とタグ以降の開発状態

v1.4系では、v1.3までに蓄積した Chart Snapshot / Market Bias の永続データをもとに、過去の市場状態を再生する Backtest 層(検証基盤)を追加しました。`v1.4.1` は、`v1.4` タグ以降に入ったBacktest / Paper Signal関連の修正・整理をまとめたリリース済みのパッチリリースです。Backtest は Paper Signal を過去データで遡及検証する **独立した Validation レイヤー**であり、Execution 層(Paper Execution / Live Execution)ではありません。

**`v1.4.1` に含まれる主な機能:**

- **Backtest Engine / Runner / CLI**(`python -m backtest`): 手元やEC2から実行可能。実行方法・引数・終了コードは [docs/backtest.md](docs/backtest.md) を参照。
- **Market State 局面別エントリー制御**(`--allowed-market-states`)と**銘柄別 Market Bias**、時系列 Bias 参照・再エントリー抑制・cooldown。
- **XAUUSD squeeze breakout 戦略**(`--strategy-profile xauusd_1h_squeeze_breakout_v1`)を paper-only Backtest に追加。
- **Paper Signal の常駐サービス化**(`paper_signal_bot.py`)と**日次集計レポート**(`paper_signal_daily_report.py`, 読み取り専用)。

**`v1.4.1` タグ後に追加された主な機能:**

- **Paper Execution**(`paper-execution/`): 保存済みPaper Signalから仮想Entry / Position / SL / TP / Exitを判定する別レイヤー。実発注を行うLive Executionは未実装・非スコープ。
- **外部 Strategy 定義(YAML)の Loader / Validation**(Issue #53): Strategy ごとの可変設定を YAML で外部化し、`--strategy-file <path>`(単一ファイル)または `--strategy <id>`(`backtest/strategies/` ディレクトリ、`STRATEGY_DEFINITIONS_DIR` で上書き可)で opt-in 実行する。現在利用するのはBacktest経路のみであり、Paper Signal / Paper Executionとの定義共有は未実装。スキーマは [Strategy 定義仕様](docs/architecture/strategy_definition.md)、使用方法は [docs/backtest.md](docs/backtest.md) を参照。
- **Execution 固定シナリオ試験**(Issue #77): 将来の Live Execution 用に実装済みの Execution 基盤(Issue #73–#76)の主要分岐(正常約定 / 拒否 / 部分約定 / timeout=UNKNOWN / DB保存失敗 / Safety Gate 拒否 / 多重起動競合など)を、FakeBroker と一時 DB のみで固定データから再現・検証するシナリオ runner(`python -m execution.scenario_runner`)。外部通信・実発注は一切行わない。詳細は [execution/README.md](execution/README.md) の「固定シナリオ試験」節を参照。
- **Shadow Mode**(Issue #78): 実 Signal・実設定・Safety Gate を通常の Production 経路で実行しつつ、**Broker へは一切送信せず**、生成された注文候補(想定注文内容)と Gate の通過 / 拒否結果を専用 DB へ監査保存する検証モード(`python -m execution.shadow_runner` / `python -m execution.shadow_report`)。仮想約定・仮想 Position・仮想 PnL は計算しない(Paper Execution とは別物)。dry-run は従来どおり完全無副作用のまま分離。Demo / Live 接続は未実装。詳細は [docs/architecture/shadow_execution.md](docs/architecture/shadow_execution.md)。

詳細と非スコープは [Roadmap](docs/product/roadmap.md) を参照してください。

## 非スコープ(全レイヤー共通)

以下は実売買(Live Execution)に踏み込む領域であり、行いません。

- Broker API 連携(OANDA / XM / MT5 など)
- 実発注・実約定・実ポジション管理
- 自動売買(実口座での自動エントリー・自動Exit・損切り・利確)
- ロット計算・レバレッジ計算・資金管理
- 売買推奨文言の生成

Paper Execution が行う仮想 Entry / Exit / SL / TP 判定は仮想レコード上のシミュレーションであり、上記の実売買には該当しません。詳細は [Concept の非スコープ](docs/product/concept.md#非スコープ) を参照してください。

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
