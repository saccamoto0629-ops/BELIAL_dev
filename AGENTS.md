# AGENTS.md

BELIAL_dev で作業する AI エージェント(Codex、Claude Code、その他)向けの入口。ここには手順の本文を書かない。

## GitHub Issue の実装・修正を行う場合

必ず先に BELIAL Issue 実装 Skill を読み、その手順に従うこと。

- Skill 本体: [`.claude/skills/belial-issue-implementation/SKILL.md`](.claude/skills/belial-issue-implementation/SKILL.md)
- 参照資料: 同ディレクトリの `references/`(SKILL.md から必要時に辿る)

Claude Code は上記パスの Skill を自動発見する。Codex など他のエージェントは、Issue 実装・修正・レビュー対応のタスクを開始する前に上記 SKILL.md を読み込むこと。

## 仕様の正

責務・仕様・運用の正は Skill ではなく `docs/` 配下の既存ドキュメントである(入口: [docs/README.md](docs/README.md)、最初に読むのは `docs/product/concept.md` / `docs/architecture/architecture.md` / `docs/operations/operations.md`)。Skill と docs が矛盾した場合は docs に従う。

## Skill の配置と運用

配置理由・更新条件・docs との責務差は [docs/development/agent-skills.md](docs/development/agent-skills.md) を参照。
