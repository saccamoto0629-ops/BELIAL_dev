# BELIAL Design Documents

`docs/architecture/berial_spec.md` は BELIAL 設計ドキュメント群への入口です。実装に追随して更新される仕様は `docs/product/` / `docs/architecture/` / `docs/operations/` 配下の機能別Markdownに分割します。

## Documents

| Document | Purpose |
|---|---|
| [Concept](../product/concept.md) | BELIAL の目的、基本方針、現行スコープ、非スコープ、設計思想 |
| [Organization](../product/organization.md) | BELIAL Demon Organization / Pepperoni の設定と責務 |
| [Architecture](architecture.md) | 全体構成、データ連携、Alert / Strategy / Execution の責務分離 |
| [Market Snapshot](market_snapshot.md) | Market Snapshot の仕様とProvider概要 |
| [Market Watch](market_watch.md) | Chart Snapshot、MACD Alert、MA Alert、Bollinger Bands Alert の仕様 |
| [Market Bias](market_bias.md) | Bull / Bear / Neutral 判定仕様と制約 |
| [Paper Signal](paper_signal.md) | paper_only の仮説生成・保存・通知仕様 |
| [Operations](../operations/operations.md) | 環境変数、dry-run、起動例、本番運用、手動確認コマンド |
| [Roadmap](../product/roadmap.md) | v1.3.1以降、v1.4系(v1.4.1リリース候補)、本番常駐化(systemd)、v1.5以降、将来検討 |

## Documentation Rules

- 全体連番の章番号は使わない。各ファイル内で閉じた見出しにする。
- コンセプトやキャラクター設定など、あまり動かない情報は `docs/product/concept.md` / `docs/product/organization.md` に置く。
- 実装に追随して変わる仕様は機能別ドキュメントに置く。
- 起動方法、dry-run、systemd、環境変数は `docs/operations/operations.md` に置く。
- 将来予定や検討事項は `docs/product/roadmap.md` に置く。
- Webhook URL、API key、token などの秘密情報の実値は書かない。
- Alert は事実通知、Strategy / Method は解釈、Execution は執行として責務を分けて記述する。

## Current Scope

現行実装は、市場観測、分類、paper_only の仮説生成までを扱う。自動エントリー、自動Exit、損切り、利確、Broker API 接続、実発注は現行スコープ外です。
