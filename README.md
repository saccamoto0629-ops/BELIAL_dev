# BELIAL

BELIAL は、ニュース・マーケットデータ・チャート指標を観測し、市場状態を分類し、`paper_only` の仮説を記録するローカル監視システムです。**自動エントリー、自動Exit、損切り、利確、Broker API 連携、実発注、売買推奨文言の生成は行いません。**

現行の実装状況(v1.3系)と、v1.4で計画している Backtest 群の位置づけは [docs/product/roadmap.md](docs/product/roadmap.md) を参照してください。

## ドキュメント

すべての設計・運用ドキュメントは [`docs/`](docs/README.md) 配下にあります。

| ディレクトリ | 内容 |
|---|---|
| [docs/product/](docs/product/README.md) | 目的、現行スコープ、非スコープ、責務レイヤー、Roadmap |
| [docs/architecture/](docs/architecture/README.md) | 全体構成、コンポーネント責務、DB設計、各モジュールの現行仕様 |
| [docs/operations/](docs/operations/README.md) | 環境変数、起動手順、dry-run方針、systemd運用 |
| [docs/development/](docs/development/README.md) | Issue / PR 運用ルール |

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

各コンポーネントは独立した `.env` / `requirements.txt`(共通は repo ルートの `requirements.txt`)/ `tests/` を持ちます。

## 責務分離(Observation → Classification → Alert → Hypothesis → Execution)

| Layer | Responsibility | 現行モジュール |
|---|---|---|
| Observation | 価格、ニュース、指標を取得する | `news-feed-bot/market_data.py` など各Provider, `market-watch/chart_collector.py` |
| Classification | 市場状態を分類する | `market-watch/indicators.py`, `news-feed-bot/market_bias.py` |
| Alert | 事実を検知して通知する(売買判断はしない) | `market-watch/macd_alert_bot.py` / `ma_alert_bot.py` / `bollinger_alert_bot.py` / `rsi_alert_bot.py` |
| Hypothesis (Paper Signal) | `paper_only` の仮説を生成・保存する | `paper-signal/signal_generator.py` |
| Execution | 実注文、Exit、損切り、利確を担当する | **現行スコープ外**(未実装) |

詳細は [Concept のレイヤー分離](docs/product/concept.md#レイヤー分離)、Alert層の記述粒度は [Architecture の責務分離](docs/architecture/architecture.md#alert--strategy--execution-の責務分離) を参照してください。

## DB構成

BELIAL はコンポーネントごとに独立した SQLite ファイルを持ちます。共有DBはありません。

| DB | パス(既定) | 所有コンポーネント | 用途 |
|---|---|---|---|
| Chart DB | `market-watch/data/chart_data.sqlite3` | `market-watch/` | ローソク足(UTC保存)、Chart Snapshot、Alert のメタデータ |
| Paper Signal DB | `paper-signal/data/paper_signals.sqlite3` | `paper-signal/` | `paper_only` シグナル記録 |
| Market Bias DB | `news-feed-bot/data/market_bias.sqlite3` | `news-feed-bot/` | Market Bias(trend/risk/volatility)の評価履歴。Backtest からの時点参照用 |

いずれも `data/` はGit管理しません。詳細スキーマは [Architecture](docs/architecture/architecture.md)、[Market Watch](docs/architecture/market_watch.md)、[Market Bias](docs/architecture/market_bias.md)、[Paper Signal](docs/architecture/paper_signal.md) を参照してください。

## dry-run 方針

すべてのBotは `--dry-run` で以下の副作用を発生させません。

- Discord送信
- state ファイル更新
- DB作成・更新
- S3アップロード

`--dry-run` はコンソール出力のみで確認モードとして動作します。詳細は [Operations](docs/operations/operations.md) を参照してください。

## v1.4 Backtest(準備中)

v1.4 では、v1.3までに蓄積した Chart Snapshot / Market Bias の永続データをもとに、過去の市場状態を再生する Backtest 層の追加を計画しています。Issue #17〜#28 が対象範囲です。詳細と非スコープは [Roadmap](docs/product/roadmap.md) を参照してください。

Backtest Engine を手元やEC2から実行するための Runner / CLI (`python -m backtest`) を追加済みです。実行方法・引数・終了コードは [docs/backtest.md](docs/backtest.md) を参照してください。

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
```

実際のWebhook URL、API key、tokenは `.env` にのみ置き、Gitにコミットしません。起動コマンドの詳細は各コンポーネントのREADME、または [Operations](docs/operations/operations.md) を参照してください。

## セキュリティ

- `.env`、`data/`、`state/`、`logs/`、`__pycache__/`、`.pytest_cache/` はGit管理しません(`.gitignore` 参照)。
- Webhook URL、API key、tokenの実値はドキュメント・READMEに記載しません。
