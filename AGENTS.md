# AGENTS.md

BELIAL_dev で作業する AI エージェント(Codex、Claude Code、その他)向けの入口。ここには手順の本文を書かない。

## GitHub Issue の実装・修正を行う場合

必ず先に BELIAL Issue 実装 Skill を読み、その手順に従うこと。

- Skill 本体: [`.claude/skills/belial-issue-implementation/SKILL.md`](.claude/skills/belial-issue-implementation/SKILL.md)
- 参照資料: 同ディレクトリの `references/`(SKILL.md から必要時に辿る)

Claude Code は上記パスの Skill を自動発見する。Codex など他のエージェントは、Issue 実装・修正・レビュー対応のタスクを開始する前に上記 SKILL.md を読み込むこと。

## PR をコード変更なしでレビューする場合

コード変更を伴わない純粋な PR レビュー(Issue 受け入れ条件との突き合わせ、再レビュー、mergeable 判定)は、必ず先に BELIAL PR レビュー Skill を読み、その手順に従うこと。

- Skill 本体: [`.claude/skills/belial-pr-review/SKILL.md`](.claude/skills/belial-pr-review/SKILL.md)

レビュー中はコード修正・commit・push・PR 更新を行わない。ユーザーが修正を依頼した時点で上記の Issue 実装 Skill へ引き継ぐ。

## 仕様の正

責務・仕様・運用の正は Skill ではなく `docs/` 配下の既存ドキュメントである(入口: [docs/README.md](docs/README.md)、最初に読むのは `docs/product/concept.md` / `docs/architecture/architecture.md` / `docs/operations/operations.md`)。Skill と docs が矛盾した場合は docs に従う。

## Skill の配置と運用

配置理由・更新条件・docs との責務差は [docs/development/agent-skills.md](docs/development/agent-skills.md) を参照。
