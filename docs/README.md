# BELIAL Docs

BELIAL のドキュメントは、目的別に以下のディレクトリへ分ける。リポジトリ全体の入口は [ルートREADME](../README.md) を参照。

## Structure

```text
docs/
├── product/       # 何を作るか、目的、世界観
├── architecture/  # どう作るか、責務分離、DB、各モジュール
├── operations/    # どう動かすか、環境変数、デプロイ、障害対応
├── development/   # どう開発するか、Issue、PR、コミット、タグ
├── testing/       # 何をテストするか、テスト観点、重要ケースとpytestの対応
└── archive/       # 過去レビュー、旧仕様、検証メモ
```

## Guidelines

- 現行仕様は `architecture/` に置く。
- 開発ルールは `development/` に置く。
- 検証レポートは `development/reports/` に置く。
- テスト方針・重要テスト項目は `testing/` に置く([test_specification.md](testing/test_specification.md))。
- 旧仕様やレビュー途中の資料は `archive/` に退避する。
- `docs/` 直下には原則として、この `README.md` だけを置く。
