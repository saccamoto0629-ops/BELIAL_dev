# Agent Skills(Issue #86)

BELIAL の Issue 実装・修正手順と PR レビュー手順を、AI エージェント(Claude Code / Codex 等)が共通に参照できる Agent Skill として管理する。

## 配置

```text
.claude/skills/belial-issue-implementation/
├── SKILL.md                      # 発動条件・実装フロー・禁止事項(常時読む本体)
└── references/
    ├── repository-guide.md       # コンポーネント/docs/テスト対応表、調査コマンド(必要時に読む)
    └── pr-template.md            # PR 本文テンプレート(PR 作成時に読む)
.claude/skills/belial-pr-review/
└── SKILL.md                      # コード変更を伴わない純粋 PR レビューの手順(Issue #90)。
                                  # references/ は持たず、repository-guide.md を相対パスで再利用する
AGENTS.md                         # リポジトリルート。全エージェント共通の入口(Skill へのポインタのみ)
```

### Skill 間のルーティング

- **Issue 実装・修正・レビュー指摘対応(実変更あり)** → `belial-issue-implementation`
- **コード変更を伴わない純粋な PR レビュー**(受け入れ条件との突き合わせ・再レビュー・mergeable 判定) → `belial-pr-review`
- レビュー中に修正が必要になっても、ユーザーが修正を依頼した時点で `belial-pr-review` から `belial-issue-implementation` へ引き継ぐ(勝手に修正へ移行しない)。

### この配置を採用した理由

- **単一ソース**: Skill 本体は `.claude/skills/` 配下の 1 箇所のみ。Codex 等は複製ではなくルートの `AGENTS.md` から同じファイルを参照するため、二重管理が発生しない。
- **Claude Code**: `.claude/skills/<name>/SKILL.md` はプロジェクト Skill の標準配置であり、自動発見・自動発動の対象になる。
- **Codex 等**: `AGENTS.md` はエージェント向け指示ファイルの事実上の標準であり、Codex はリポジトリルートの `AGENTS.md` を自動で読む。そこから SKILL.md を読み込む指示を置いている。SKILL.md 本文は特定ツールに依存しない Markdown である。
- **シンボリックリンクは不使用**: 検討したが採用しない。Windows では git の既定(`core.symlinks=false`)や権限(Developer Mode / 管理者権限が必要)によりリンクがプレーンテキストファイルとして checkout され、リンク切れと同じ状態になる。GitHub 上でもリンク先パスの文字列として表示され内容を追いにくい。ポインタ文書(`AGENTS.md`)による参照の方が単純で移植性が高い。

## 利用方法

- **Claude Code**: 追加設定は不要。BELIAL_dev の Issue 実装・修正タスクで Skill が自動発動する。明示的に使う場合はプロンプトで Skill 名 `belial-issue-implementation` を指定する。
- **Codex**: リポジトリルートで起動すれば `AGENTS.md` が自動で読み込まれ、Issue 実装タスクの開始時に `.claude/skills/belial-issue-implementation/SKILL.md` を読むよう誘導される。読み込まれない場合はプロンプトで同パスの読み込みを指示する。なお、Codex のネイティブ Skill 探索場所は `.agents/skills/` であり、本 Skill はそこへ複製していないため **Codex の Skill selector には自動登録されない**(`AGENTS.md` 経由の明示参照方式)。これは重複コピーによる二重管理を避けるための意図的な構成であり、ネイティブ登録が必要になった場合の対応は別 Issue で判断する。
- **その他のエージェント / 人間**: SKILL.md は通常の Markdown なので、そのままチェックリストとして読める。

## docs と Skill の責務差

| | 既存 docs(`docs/`) | Skill |
|---|---|---|
| 内容 | 仕様・責務・運用の**正** | Issue 実装の**手順・確認観点・成果物形式** |
| 矛盾時 | docs が優先 | docs に従い、Skill 側の修正 Issue を立てる |
| 更新契機 | 仕様・運用の変更 | 開発フローの変更、レビューで繰り返し指摘が出たとき |

Skill には docs の本文を複製しない。Skill はどの docs をいつ読むかと、省略してはならない確認・報告項目だけを定める。

## Skill を更新すべき条件

- Issue / PR / テストの運用ルールが変わったとき(例: CI のスイート構成変更、PR 記載項目の追加)。
- 同種のレビュー指摘が複数 PR で繰り返されたとき(`references/repository-guide.md` の「過去のレビューで繰り返された指摘」へ追記)。
- コンポーネント・docs の追加/移動で `references/repository-guide.md` の対応表が古くなったとき。
- Skill と docs の矛盾が見つかったとき(docs を正として Skill を直す)。

## 導入時の試行記録(Issue #86)

作成した Skill を、**open の [Issue #73](https://github.com/saccamoto0629-ops/BELIAL_dev/issues/73) に対する merged の [PR #87](https://github.com/saccamoto0629-ops/BELIAL_dev/pull/87)**(Broker interface と FakeBroker の追加)へ後追い適用して確認した。試行時点の GitHub 上の状態は Issue #73 = open、PR #87 = merged であり、Skill 試行は PR #87 の実装内容とレビュー履歴(4 ラウンドの review submissions)を対象にした。Issue #73 を close するかどうかは運用判断であり、この試行の対象外とする(試行が勝手に Issue を close することはない)。

| 確認項目 | 結果 |
|---|---|
| Issue・関連 Issue・PR・docs の選定 | Issue #73([Execution] / broker 境界)に対し、Skill §2 + 対応表から `concept.md`(非スコープ・レイヤー)、`architecture.md`、`test_specification.md`、新規 `broker_interface.md` が選定される。PR #87 の実際の参照 docs と一致 |
| 影響範囲調査の不足 | 初稿の Skill には「共有境界のリポジトリルート import 確認」がなく、PR #87 レビュー r1 Major 1(`sys.path` 細工による import 問題の隠蔽)を予防できなかった → Skill §3 と repository-guide へ追記 |
| テスト種別の列挙 | 対象(`broker/tests`)→ 関連(paper-signal / paper-execution の依存禁止)→ 全体 6 スイート + import smoke test が列挙される |
| docs 更新要否の判定 | 新コンポーネント追加のため architecture 設計書・README・concept・roadmap・test_specification の更新が必要と判定される(PR #87 の実際の更新と一致) |
| PR 記載項目の欠落 | PR #87 本文には変更内容・非スコープはあったが、スイート別のテスト実行結果と受け入れ条件ごとの充足状況が明示されていなかった → `references/pr-template.md` の必須見出しでカバー |
| レビュー指摘の予防 | r1 Major 2(共通モデルの enum 型未検証)、r3(terminal 状態の content 不変条件の網羅漏れ)、r4(fill_id 一意性)は、試行を受けて Skill §4 の横断ルールチェックリストと repository-guide の「過去のレビューで繰り返された指摘」へ反映した |

試行のための実行コード変更は行っていない(Skill / docs のみ)。
