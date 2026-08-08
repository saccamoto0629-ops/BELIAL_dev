---
name: belial-pr-review
description: BELIAL_dev リポジトリの Pull Request を、コードを変更せずにレビューするときに使用する。Issue 受け入れ条件との突き合わせ、初回レビュー、修正後の再レビュー、mergeable 判定、セキュリティ・運用安全性・回帰影響の確認、レビュー結果の PR Conversation への記録を定める。Use when performing a pure (no code change) review of a pull request in the BELIAL_dev repository, including re-review after fixes and mergeability judgement.
---

# BELIAL PR Review

BELIAL_dev の PR を**コードを変更せずに**レビューするときの共通手順。Claude Code / Codex どちらのエージェントからも同じ内容を参照する。

## 発動条件

- **対象**: BELIAL_dev の PR に対する、コード変更を伴わない純粋なレビュー。
  - Issue 受け入れ条件との突き合わせ
  - 初回レビュー
  - 修正後 head に対する再レビュー
  - mergeable 判定
  - セキュリティ・運用安全性・回帰影響の確認
- **非発動**(実装 Skill [`belial-issue-implementation`](../belial-issue-implementation/SKILL.md) の対象):
  - Issue 実装・バグ修正
  - レビュー指摘(review feedback)を受けたコード修正
  - docs / テストを含む実変更、commit / push / PR 更新
  - CI 失敗の修正

## 大原則

1. **レビュー中はリポジトリを一切変更しない。** 問題を発見しても、コード修正・branch 作成・commit・push・PR 更新(タイトル・本文・base の変更、merge を含む)を行わない。許可されるのは読み取り・テスト実行(作業ツリーを汚さない範囲)・PR Conversation へのレビューコメント投稿のみ。
2. **修正への移行はユーザーの明示的な依頼があったときだけ。** その時点で `belial-issue-implementation` へ引き継ぎ、その手順(実装前ゲートを含む)に従う。レビュー依頼を修正依頼と推測して勝手に修正しない。修正用 branch も勝手に作らない。
3. **仕様の正は Issue 本文と既存 docs。** 責務・契約の判断は `docs/` 配下(入口: `docs/README.md`)を正とする。本 Skill と docs が矛盾したら docs に従う。
4. **PR diff だけを眺めてレビュー完了としない。** 関連 docs・実装・tests・CI・scripts・運用導線まで、変更対象に応じて確認する。
5. **確認できなかったことを確認済みと報告しない。** 実行できなかったテスト・未確認事項は必ずその旨を明記する。

## レビューフロー

### 1. レビュー開始時(必須)

以下をすべて確認してからレビュー判断に入る。

- 対象 PR を特定し、**head SHA を取得する**(レビュー結果コメントに記載するため必須)。
- PR 本文を読む。
- linked / closing Issue を解決する。
  - PR 本文の `Closes #N` 等、GitHub の linked issue、branch 名・コミットメッセージから解決する。
  - **一意に決まらない場合**: 推測で 1 件を採用しない。候補と根拠を列挙し、ユーザーに確認できる場合は確認する。確認できない場合は、Issue 照合を除いた範囲(差分の正しさ・既存契約との整合・回帰影響)でレビューを進め、Issue 受け入れ条件との突き合わせが未実施であることを「未確認事項」とレビューコメントに明記する。
- 全差分を確認する(ファイル単位の見落としがないよう変更ファイル一覧と突き合わせる)。
- コミット履歴を確認する。
- PR Conversation(comments)を確認する。
- review submissions を確認する。
- unresolved / resolved review threads を確認する(resolved も今回差分に関係するものは読む)。
- CI 結果を確認する(実行不能な場合はその旨を未確認事項へ)。
- 同一 PR の過去レビューコメント(本 Skill 形式のものを含む)を確認する。
- **同一 head SHA への既存レビューが既にある場合**、同じ内容の完全レビューを重複投稿しない。既存レビューの確認済み事項・未確認事項を引き継ぎ、差分(未確認事項の解消、追加観点)に絞る。ユーザーが明示的に再実施を依頼した場合はこの限りではない。

### 2. 仕様との突き合わせ

対象 Issue 本文の以下を、それぞれ差分と照合する。

- 目的
- 対応内容
- 受け入れ条件(1 項目ずつ充足 / 未充足 / 未確認を判定する)
- テスト観点
- 非スコープ(スコープ外の変更が混入していないか)

さらに変更対象に応じて、関連 docs・実装・tests・CI(`.github/workflows/`)・`scripts/`・運用導線(CLI・systemd・常駐 Bot)を確認する。docs の選定は [repository-guide.md](../belial-issue-implementation/references/repository-guide.md) の「ドキュメント選定の目安」を使う。

### 3. 影響範囲の確認

変更箇所ごとに、caller / callee・他コンポーネント・docs・tests を含む影響を確認する。評価観点は [repository-guide.md](../belial-issue-implementation/references/repository-guide.md) の「影響マトリクス評価項目」(dry-run、DB / migration、read-only 経路、副作用、Discord / S3 / ネットワーク、CLI、systemd、後方互換、secrets、レイヤー責務など全 20 項目)と「横断契約の要点」を再利用する。レビューでは実装時のような全項目の記録は必須としないが、**該当し得る項目を根拠なく飛ばさない**。特に以下は毎回意識する。

- セキュリティ(secrets の実値混入、権限、不正入力)
- 運用安全性(dry-run の無副作用、既存運用コマンド・起動導線・DB 後方互換)
- 回帰影響(既存テスト・既存契約・並行実行・境界値・障害時挙動)
- 「過去のレビューで繰り返された指摘」(同 repository-guide.md)の再発

### 4. テスト

- 変更に対応する対象テスト・関連テストを実行する(スイート一覧・コマンドは [repository-guide.md](../belial-issue-implementation/references/repository-guide.md) の「テストスイート実行コマンド」)。
- レビュー結果には以下を区別して明示する。
  - 実行したテスト
  - 成功したテスト
  - 失敗したテスト(失敗のまま報告する)
  - 実行不能だったテスト(環境・依存の理由を添える)
  - 未実行テスト(実行しなかった理由を添える)
- **「実行できなかった」を「成功」と扱わない。** checkout やコマンドが利用できない環境では、実行不能である旨と静的確認で代替した範囲を明記する。
- docs / Skill のみの変更では、pytest ではなく [repository-guide.md](../belial-issue-implementation/references/repository-guide.md) の「docs / Skill 変更の直接検証チェックリスト」を検証の基準にする(frontmatter、`name` とディレクトリ名の一致、相対リンク・参照パス、code fence、見出し構造、Claude Code / Codex 配置整合、実行コード・workflow に差分がないこと)。

### 5. レビュー結果形式

レビュー結果は以下を基本形とする。

```markdown
## 結論

Mergeable / Not mergeable

## Blocker
...

## Major
...

## Minor
...

## 解消を確認した項目
...

## テスト結果
...

## 未確認事項 / Known limitations
...
```

- 各指摘には最低限以下を含める。
  - severity(Blocker / Major / Minor)
  - ファイル
  - 行番号または対象箇所
  - 問題となる条件
  - Issue 受け入れ条件または既存契約(docs)との矛盾
  - 再現方法または具体的な failure path
  - 修正方針(提示のみ。実装はしない)
- Blocker / Major / Minor が 0 件の場合も見出しを省略せず「なし」と明記する。
- **mergeable 判定基準**(曖昧にしない):
  - Blocker または Major が 1 件以上 → **Not mergeable**
  - 受け入れ条件の充足が確認できない項目が残る、または必要テストが失敗 / 未確認で充足判定ができない → **Not mergeable**(理由を「未確認事項」に明記)
  - 上記に該当せず、指摘が Minor のみ、または 0 件 → **Mergeable**(Minor は残っていてよいが列挙する)

### 6. PR Conversation への記録(必須)

- **最終レビュー結果は、チャット回答だけで終わらせず、必ず対象 PR の Conversation へコメントとして残す。** チャットへの最終回答よりも先に投稿する。
- コメントには**レビュー対象の head SHA を明記**する(後続レビューが陳腐化を判定できるようにするため)。
- レビューが途中で終了・失敗しそうな場合、または失敗した場合も、可能な限り以下を PR Conversation へ残す。
  - `REVIEW INCOMPLETE` 等、レビューが未完了であること
  - head SHA
  - 確認済み範囲
  - 発見済みの Blocker / Major / Minor
  - 実行済みテスト / 未実行テスト
  - 未確認事項
  - 次セッションで確認すべき内容
- 後続セッションはまずこのコメントを読み、head SHA が同一なら確認済み内容を引き継ぎ、head が変わっていれば前回結果が陳腐化していると認識した上で再レビューする。
- 既存コメントと内容が重複する場合はコメントを乱立させず、可能であれば既存レビューコメントを更新するか、前回レビューとの関係(どの head SHA のレビューを引き継いだか)が分かる形で追記する。
- 無意味に細かい進捗コメントを大量投稿しない。原則は 1 レビュー(1 head SHA)につき 1 コメント。中断リスクに備えた途中記録は、上記の未完了フォーマットで必要時のみ行う。

### 7. 再レビュー(修正後 head)

修正後 head の再レビューでは、以下をすべて確認する。

- 前回指摘(Blocker / Major / Minor)が解消されているか
- unresolved review threads
- 前回の未確認事項
- 前回 head 以降の新しい diff 全体
- 修正による回帰(修正差分が新しい問題を持ち込んでいないか)

前回指摘が解消した場合は「解消を確認した項目」へ記録する。**前回の Blocker / Major の確認だけで終了せず**、新規差分に対して §2〜§4 を適用する。

## 利用例(最小プロンプト)

Issue を明示する場合:

```text
PR #123 を Issue #100 に照らしてレビューしてください。
```

PR から linked / closing Issue を一意に解決できる場合は Issue 番号を省略できる:

```text
PR #123 をレビューしてください。
```

## 禁止事項

- レビュー中のファイル変更・コード修正・branch 作成・commit・push・PR 更新・merge。
- ユーザーの明示的な修正依頼なしに実装 Skill へ移行すること(レビュー依頼を修正依頼と推測しない)。
- linked Issue を推測で一意に採用してレビュー判定へ使うこと。
- 実行できなかったテスト・検証を成功扱いにすること。
- 最終レビュー結果を PR Conversation へ残さずに終了すること。
- head SHA を記載しないレビューコメント。
- 同一 head SHA への同内容レビューコメントの重複投稿・進捗コメントの乱立。
- 自動 merge、CI workflow の追加・変更、自動レビュー bot 化、一般リポジトリ向け汎用化(Issue #90 非スコープ)。
