---
name: belial-issue-implementation
description: BELIAL_dev リポジトリの GitHub Issue の実装・修正・レビュー対応を行うときに使用する。Issue調査、関連ドキュメント選定、影響範囲調査、最小差分実装、テスト実行方針、docs更新判断、PR本文の記載形式を定める。Use when implementing, fixing, or addressing review feedback for a GitHub Issue in the BELIAL_dev repository.
---

# BELIAL Issue Implementation

BELIAL_dev の GitHub Issue を実装・修正するときの共通手順。Claude Code / Codex どちらのエージェントからも同じ内容を参照する。

## 発動条件

- **対象**: BELIAL_dev の GitHub Issue の実装・修正、および対応する PR のレビュー指摘対応。
- **対象外**: Issue に紐づかない質問応答、docs の誤字修正などの自明な単発変更(ただし PR を作る場合は本 Skill の PR 記載形式に従う)。

## 大原則

1. **仕様の正は既存 docs であり、本 Skill ではない。** 責務・仕様の判断は必ず以下を正とする。本 Skill と docs が矛盾したら docs に従い、本 Skill の修正 Issue を立てる。
   - 目的・責務レイヤー・非スコープ: `docs/product/concept.md`
   - 全体構成・コンポーネント責務・DB構成・schema validation / migration・dry-run 方針: `docs/architecture/architecture.md`
   - 環境変数・起動・運用・テスト方針: `docs/operations/operations.md`
   - テスト観点と pytest の対応: `docs/testing/test_specification.md`
   - Issue / PR 運用ルール: `docs/development/issues-guideline.md`
2. **最小差分。** 既存運用を壊さない最小の変更で Issue の受け入れ条件を満たす。大きくなるなら分割を提案する。
3. **レイヤー責務を壊さない。** Observation → Classification → Alert → Strategy → Hypothesis → Execution の責務分離(`docs/product/concept.md` の「レイヤー分離」)を超える変更をしない。特に Live Execution(実発注・Broker API 連携・自動売買)は非スコープ。
4. **不明点・未完了・既知制約を隠さない。** 確認できなかったこと、動かしていないこと、割り切った制約は、省略せず PR 本文と報告に明記する。推測で仕様を補わない。

## 実装フロー

### 1. Issue の把握

- Issue 本文・受け入れ条件・非スコープを全部読む。
- Issue 本文からリンクされた関連 Issue / PR を辿る。同じコンポーネントの直近のマージ済み PR のレビューコメントを確認し、繰り返し指摘されている観点(型検証、import 構成、副作用境界など)を今回の実装に先回りで適用する。
- 受け入れ条件が曖昧な場合は、勝手に解釈を固定せず、解釈を PR 本文に明記する。

### 2. 関連ドキュメントの選定

- 必読 3 点: `docs/product/concept.md` / `docs/architecture/architecture.md` / `docs/operations/operations.md`(変更に関係する節だけでよい)。
- 変更対象コンポーネントの README と `docs/architecture/` 配下の個別設計書を読む。コンポーネントとドキュメントの対応表は [references/repository-guide.md](references/repository-guide.md) を参照。
- テストへ影響する場合は `docs/testing/test_specification.md` の該当レイヤーの観点を確認する。

### 3. 影響範囲調査

- 変更するモジュール・関数・定数・環境変数・DB テーブルごとに、リポジトリ全体を grep して呼び出し元・参照元を列挙する(コード、テスト、docs、`.github/workflows/`、`scripts/` を含む)。
- DB スキーマに触れる場合は、`docs/architecture/architecture.md` の「SQLite schema validation / migration」を読み、該当 store の migration 方式(rebuild / additive / 冪等 ALTER)に合わせる。
- 共有される境界(他コンポーネントから import されるモジュール)を変更・追加する場合は、リポジトリルートからの import 可否をテストで固定する。テスト専用の `sys.path` 操作で import 問題を隠さない。
- 調査結果(影響ファイル一覧)は PR 本文の Changed files / affected components の根拠にする。

### 4. 実装

- 最小差分で実装し、周辺コードのスタイル・命名・コメント密度に合わせる。
- **横断ルールチェックリスト**(該当するものを必ず確認。詳細は各 docs が正):
  - [ ] dry-run(`--dry-run` / `dry_run=True`)で Discord 送信・DB 作成/更新/migration・S3 アップロード・state 更新が発生しない
  - [ ] DB は所有コンポーネントの store 層だけが書く。他コンポーネントの DB を migration しない
  - [ ] DB の関連付けは ID を主とし、時刻は補助情報(時刻のみで関連付けない)
  - [ ] 時刻は tz-aware UTC で保存・比較する
  - [ ] 共通モデル・状態機械を追加/変更する場合、型・enum・クロスフィールドの不変条件を `__post_init__` 等で検証し、不正値の拒否をテストで固定する
  - [ ] 状態遷移契約(terminal 状態の不変性など)は docstring の宣言と実装・テストを一致させる。宣言した不変条件はフィールド単位で網羅的に検証する
  - [ ] Webhook URL・API key・token の実値をコード・docs・`.env.example` に書かない
  - [ ] Live Execution 領域(実発注・ロット計算・売買推奨文言)へ踏み込まない

### 5. テスト

実行方針: **対象テスト → 関連テスト → 全体テスト** の順で段階的に広げる。

1. **対象**: 変更したコンポーネントのスイート(例: `python -m pytest paper-execution/tests -q`)。
2. **関連**: 影響範囲調査で参照元が見つかった他コンポーネントのスイート。
3. **全体**: 6 スイートすべて。ルートに pytest 設定はなく、**ディレクトリ単位で個別に**実行する(コマンド一覧は [references/repository-guide.md](references/repository-guide.md))。

- 新規動作は正常系・異常系・境界値のテストを追加する。外部 API / Discord / S3 / 実 DB へは到達させない(Mock / tmp_path / monkeypatch)。
- Windows 固有の副作用(SQLite ファイルハンドル等)に触れる場合は CI の Windows ジョブ対象かを確認する。
- **実行しなかったスイートがある場合は、その理由を PR 本文に明記する。**「全部通った」とだけ書いて省略しない。失敗したテストは失敗のまま報告し、成功と偽らない。

### 6. docs 更新要否の確認

- 仕様・責務・環境変数・起動方法・DB スキーマ・テスト観点のいずれかが変わったら、対応する docs(§2 で選定したもの)と README を同じ PR で更新する。
- 変更が docs に影響しない場合も、PR 本文の Documentation impact に「影響なし」と根拠を書く。

### 7. PR 作成

PR 本文には最低限、以下の項目をこの順で記載する。テンプレートは [references/pr-template.md](references/pr-template.md) を使う。

1. Summary
2. Changed files / affected components
3. Tests executed and results
4. Documentation impact
5. Known limitations / remaining work
6. Issue との対応関係(受け入れ条件ごとの充足状況)

## 禁止事項

- 本 Skill へ docs の本文(仕様)を複製すること。
- Issue のスコープ外の機能追加・リファクタリング(気づいた問題は Issue 提案として報告する)。
- dry-run の無副作用保証を破る変更。
- テスト結果・未確認事項・既知制約の省略や虚偽報告。
- `.env` / `data/` / `state/` / `logs/` など Git 管理外ファイルのコミット。
