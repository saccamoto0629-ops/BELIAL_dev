# BELIAL

BELIAL は、ニュース・マーケットデータ・チャート指標を観測し、市場状態を分類し、`paper_only` の仮説を記録するローカル監視システムです。**自動エントリー、自動Exit、損切り、利確、Broker API 連携、実発注、売買推奨文言の生成は行いません。**

本ドキュメントは **v1.4.1 リリース候補**(タグ未作成)の状態を反映しています。v1.4.1 は、既存の `v1.4` タグ以降に入った修正・整理をまとめたパッチリリースです。v1.3系までの Observation / Classification / Alert / Paper Signal に加えて、過去データで仮説を遡及検証する Backtest(検証基盤)、Market State 局面別エントリー制御、銘柄別 Market Bias、XAUUSD squeeze breakout 戦略、Paper Signal の常駐サービス化と日次集計レポートを提供します。各機能の位置づけと非スコープは [docs/product/roadmap.md](docs/product/roadmap.md) を参照してください。

## ドキュメント

すべての設計・運用ドキュメントは [`docs/`](docs/README.md) 配下にあります。

| ディレクトリ | 内容 |
|---|---|
| [docs/product/](docs/product/README.md) | 目的、現行スコープ、非スコープ、責務レイヤー、Roadmap |
| [docs/architecture/](docs/architecture/README.md) | 全体構成、コンポーネント責務、DB設計、各モジュールの現行仕様 |
| [docs/operations/](docs/operations/README.md) | 環境変数、起動手順、dry-run方針、systemd運用 |
| [docs/development/](docs/development/README.md) | Issue / PR 運用ルール |
| [v1.4.1 リリースノート](docs/development/reports/v1.4.1_release_notes.md) | リリース候補の変更内容、既知の制約、CI確認・タグ作成手順 |
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

各コンポーネントは独立した `.env` / `requirements.txt`(共通は repo ルートの `requirements.txt`)/ `tests/` を持ちます。

## 責務分離(Observation → Classification → Alert → Hypothesis → Execution)

| Layer | Responsibility | 現行モジュール |
|---|---|---|
| Observation | 価格、ニュース、指標を取得する | `news-feed-bot/market_data.py` など各Provider, `market-watch/chart_collector.py` |
| Classification | 市場状態を分類する | `market-watch/indicators.py`, `news-feed-bot/market_bias.py` |
| Alert | 事実を検知して通知する(売買判断はしない) | `market-watch/macd_alert_bot.py` / `ma_alert_bot.py` / `bollinger_alert_bot.py` / `rsi_alert_bot.py` |
| Hypothesis (Paper Signal) | `paper_only` の仮説を生成・保存する | `paper-signal/signal_generator.py` |
| Execution | 執行層の総称。**Paper Execution**(仮想約定・仮想ポジション管理・仮想Exit・仮想損益)と **Live Execution**(実注文・約定確認・ポジション管理・Exit)に分かれる | **Paper Execution は実装済み**(`paper-execution/`。仮想約定のみ)。**Live Execution**(Broker API・実注文・自動売買)は**未実装** |

詳細は [Concept のレイヤー分離](docs/product/concept.md#レイヤー分離)、Alert層の記述粒度は [Architecture の責務分離](docs/architecture/architecture.md#alert--strategy--execution-の責務分離) を参照してください。

## DB構成

BELIAL はコンポーネントごとに独立した SQLite ファイルを持ちます。共有DBはありません。

| DB | パス(既定) | 所有コンポーネント | 用途 |
|---|---|---|---|
| Chart DB | `market-watch/data/chart_data.sqlite3` | `market-watch/` | ローソク足(UTC保存)、Chart Snapshot、Alert のメタデータ |
| Paper Signal DB | `paper-signal/data/paper_signals.sqlite3` | `paper-signal/` | `paper_only` シグナル記録 |
| Market Bias DB | `news-feed-bot/data/market_bias.sqlite3` | `news-feed-bot/` | Market Bias(trend/risk/volatility)の評価履歴。Backtest からの時点参照用 |
| Paper Position DB | `paper-execution/data/paper_positions.sqlite3` | `paper-execution/` | Paper Execution の仮想ポジション(状態・仮想損益)。上書きは `PAPER_POSITION_DB_PATH` |

いずれも `data/` はGit管理しません。詳細スキーマは [Architecture](docs/architecture/architecture.md)、[Market Watch](docs/architecture/market_watch.md)、[Market Bias](docs/architecture/market_bias.md)、[Paper Signal](docs/architecture/paper_signal.md) を参照してください。

## dry-run 方針

すべてのBotは `--dry-run` で以下の副作用を発生させません。

- Discord送信
- state ファイル更新
- DB作成・更新
- S3アップロード

`--dry-run` はコンソール出力のみで確認モードとして動作します。詳細は [Operations](docs/operations/operations.md) を参照してください。

## v1.4 Backtest / Paper Signal(実装済み / v1.4.1 リリース候補)

v1.4系では、v1.3までに蓄積した Chart Snapshot / Market Bias の永続データをもとに、過去の市場状態を再生する Backtest 層(検証基盤)を追加しました。v1.4.1 は、`v1.4` タグ以降に入った以下の機能の修正・整理をまとめたパッチリリースです。Backtest は Paper Signal を過去データで遡及検証する **独立した Validation レイヤー**であり、発注・約定を行う Execution 層ではありません(Execution は現行スコープ外)。

- **Backtest Engine / Runner / CLI**(`python -m backtest`): 手元やEC2から実行可能。実行方法・引数・終了コードは [docs/backtest.md](docs/backtest.md) を参照。
- **Market State 局面別エントリー制御**(`--allowed-market-states`)と**銘柄別 Market Bias**、時系列 Bias 参照・再エントリー抑制・cooldown。
- **XAUUSD squeeze breakout 戦略**(`--strategy-profile xauusd_1h_squeeze_breakout_v1`)を paper-only Backtest に追加。
- **Paper Signal の常駐サービス化**(`paper_signal_bot.py`)と**日次集計レポート**(`paper_signal_daily_report.py`, 読み取り専用)。

詳細と非スコープは [Roadmap](docs/product/roadmap.md) を参照してください。

## 非スコープ(全レイヤー共通)

- 自動エントリー
- 自動Exit
- 損切り判断
- 利確判断
- ロット計算
- Broker API 連携(OANDA / XM / MT5 など)
- 実発注
- 売買推奨文言の生成

詳細は [Concept の非スコープ](docs/product/concept.md#非スコープ) を参照してください。

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
