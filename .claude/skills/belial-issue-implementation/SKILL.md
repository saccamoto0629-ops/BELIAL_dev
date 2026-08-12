---
name: belial-issue-implementation
description: BELIAL_dev リポジトリの GitHub Issue の実装・修正・レビュー対応を行うときに使用する。Issue調査、関連ドキュメント選定、影響範囲調査、最小差分実装、テスト実行方針、docs更新判断、PR本文の記載形式を定める。Use when implementing, fixing, or addressing review feedback for a GitHub Issue in the BELIAL_dev repository.
---

# BELIAL Issue Implementation

BELIAL_dev の GitHub Issue を実装・修正するときの共通手順。Claude Code / Codex どちらのエージェントからも同じ内容を参照する。

## 発動条件

- **対象**: BELIAL_dev の GitHub Issue の実装・修正。レビュー指摘(review submissions / inline threads / conversation comments)を受けて実装を修正する作業を含む。
- **非発動(原則対象外)**:
  - 単なる質問・説明への回答
  - コード変更を伴わない純粋なレビュー(PR レビューは [`belial-pr-review`](../belial-pr-review/SKILL.md) を使う。ユーザーが修正を依頼した時点で本 Skill へ引き継がれる)
  - 実装を伴わない独立した CI 原因調査
  - PR 作成・push だけの作業
  - Issue 実装と無関係なリポジトリ案内
  - ただし、これらから実装・修正作業へ移行した時点で本 Skill の対象になる。また PR を作る場合は本 Skill の PR 記載形式に従う。

## 大原則

1. **仕様の正は既存 docs であり、本 Skill ではない。** 責務・仕様の判断は必ず以下を正とする。本 Skill と docs が矛盾したら docs に従い、本 Skill の修正 Issue を立てる。
   - 目的・責務レイヤー・非スコープ: `docs/product/concept.md`
   - 全体構成・コンポーネント責務・DB構成・schema validation / migration・dry-run 方針: `docs/architecture/architecture.md`
   - 環境変数・起動・運用・テスト方針: `docs/operations/operations.md`
   - テスト観点と pytest の対応: `docs/testing/test_specification.md`
   - Issue / PR 運用ルール: `docs/development/issues-guideline.md`
2. **最小差分。** 既存運用を壊さない最小の変更で Issue の受け入れ条件を満たす。大きくなるなら分割を提案する。
3. **レイヤー責務を壊さない。** Observation → Classification → Alert → Strategy → Hypothesis → Execution の責務分離(`docs/product/concept.md` の「レイヤー分離」)を超える変更をしない。特に Live Execution の**運用領域**(実口座での Live 運用開始・実発注を駆動する定期実行 runner・自動売買)は非スコープであり、実装済みの Live Execution 基盤(Broker 境界・Adapter・Safety Gate・Live 接続ロック等)の安全契約を弱める変更をしない(現行の区別は `docs/product/concept.md` の「非スコープ」が正)。
4. **不明点・未完了・既知制約を隠さない。** 確認できなかったこと、動かしていないこと、割り切った制約は、省略せず PR 本文と報告に明記する。推測で仕様を補わない。

## 実装フロー

### 0. 実装前ゲート: 対象 Issue・PR・レビュー履歴の解決(必須)

このゲートを完了するまで実装・変更へ着手しない。

**対象 Issue の解決**(上から順に試す):

1. ユーザーが Issue 番号 / URL を提示していればそれを使う。
2. 現在の branch に紐づく PR があれば、その linked issue / closing issue / PR 本文から対象 Issue を解決する。
3. コミットメッセージ・branch 名・PR 本文・GitHub 上の関連付けから対象 Issue を探索する。
4. それでも一意に特定できない場合は、**実装・変更を禁止する**。対話可能かどうかに関係なく(非対話環境でも)同じ契約で停止する。
   - **許可**: 読み取り専用の調査、候補 Issue の列挙、各候補を支持・否定する根拠の整理、影響範囲の予備調査、実装計画案の作成、未解決事項と必要な確認事項の報告。
   - **禁止**: ファイル変更、コード実装、docs 変更、テスト修正、commit、push、PR 更新、候補 Issue の 1 件を推測で採用しての作業続行。
   - 候補と調査結果を報告した時点で停止し、明示的な Issue 識別子または一意な GitHub 上の関係が得られるまで変更を行わない。

Issue 番号が提示されていなくても機械的に停止はせず、2〜3 で合理的に解決できる場合は探索して特定する。実装へ進めるのは一意に解決できたときだけである。

**現在 PR の確認**: 現在の branch に紐づく既存 PR がある場合、実装前に以下を必ず読む。**レビュー指摘対応では、対象 PR と未解決 review thread を読まずに変更へ着手しない。**

- PR 本文、変更差分、全コミット
- review submissions、未解決 inline review thread、解決済み thread のうち今回変更に関係するもの
- conversation comments
- CI 結果または既知の失敗状況
- linked issue / closing issue

**関連 Issue・前提 Issue の探索**: Issue 本文中の明示リンクに限定せず、以下から探索する。ただし無制限に過去 Issue を読むのではなく、**今回の仕様・互換性・前提条件へ影響するものに絞る**。

- GitHub の linked issue / development relation
- PR 本文・コミットの Issue 参照
- 同一コンポーネントの変更履歴(直近のマージ済み PR のレビュー指摘を含む。繰り返し指摘されている観点は今回へ先回りで適用する)
- 対象コード・docs 内の Issue 参照、直前の前提実装、変更対象の設計経緯

### 1. Issue の把握

- Issue 本文・受け入れ条件・非スコープを全部読む。
- 受け入れ条件が曖昧な場合は、勝手に解釈を固定せず、解釈を PR 本文に明記する。

### 2. 関連ドキュメントの選定

- 必読 3 点: `docs/product/concept.md` / `docs/architecture/architecture.md` / `docs/operations/operations.md`(変更に関係する節だけでよい)。
- 変更対象コンポーネントの README と `docs/architecture/` 配下の個別設計書を読む。コンポーネントとドキュメントの対応表は [references/repository-guide.md](references/repository-guide.md) を参照。
- テストへ影響する場合は `docs/testing/test_specification.md` の該当レイヤーの観点を確認する。

### 3. 影響範囲調査(影響マトリクス必須)

- 変更するモジュール・関数・定数・環境変数・DB テーブルごとに、リポジトリ全体を grep して**呼び出し元と呼び出し先の双方向**で参照を列挙する(コード、テスト、docs、`.github/workflows/`、`scripts/` を含む)。
- **影響マトリクス**: 変更ごとに、[references/repository-guide.md](references/repository-guide.md) の「影響マトリクス評価項目」の**全項目**を次の 3 値で評価し、記録する。
  - `Relevant`: 確認した内容と影響
  - `Not relevant`: 非該当と判断した**根拠**
  - `Unknown`: 未確認事項と残るリスク
  - 「関係なさそうなので省略」は不可。全項目を深掘りする必要はないが、非該当にも根拠を残す。結果は PR 本文の Changed files / affected components と Known limitations の根拠にする。
- DB スキーマに触れる場合は、`docs/architecture/architecture.md` の「SQLite schema validation / migration」を読み、該当 store の migration 方式(rebuild / additive / 冪等 ALTER)に合わせる。
- 共有される境界(他コンポーネントから import されるモジュール)を変更・追加する場合は、リポジトリルートからの import 可否をテストで固定する。テスト専用の `sys.path` 操作で import 問題を隠さない。

### 4. 実装

- 最小差分で実装し、周辺コードのスタイル・命名・コメント密度に合わせる。
- **横断ルールチェックリスト**(該当するものを必ず確認。詳細・参照先は [references/repository-guide.md](references/repository-guide.md) の「横断契約の要点」、正は各 docs):
  - [ ] dry-run(`--dry-run` / `dry_run=True`)で Discord 送信・DB 作成/更新/migration・S3 アップロード・state 更新が発生しない
  - [ ] DB は所有コンポーネントの store 層だけが書く。他コンポーネントの DB を migration しない
  - [ ] read-only 経路(dry-run の schema probe、レポート、照会系)が存在しない DB・ファイルを新規作成しない
  - [ ] 異常を握り潰さず、所有コンポーネントの正本 docs が定める failure channel へ必ず記録する。data-plane / 永続化 / outbox の失敗と delivery(通知配送)の失敗など、失敗種別を区別し同一視しない。終了コード・状態維持・再送・リトライ・failure 集計は所有 docs と既存回帰テストの契約に従う(例: Paper Execution の失敗分類は `docs/architecture/paper_execution.md` と `docs/operations/operations.md` が正)。「0 終了 = 異常を無視した」と機械的に判断せず、契約上 0 終了となる失敗は記録され再送・監視可能であることを確認する。既存の再送・outbox・通知失敗記録の契約を壊さない。docs と実装・テストが食い違う場合は推測で修正せず不整合として報告する
  - [ ] 既存 DB・既存データとの後方互換(migration 方式・既存行の扱い)と、既存の運用コマンド・起動導線(CLI 引数・終了コード・systemd・常駐 Bot)を壊さない
  - [ ] DB の関連付けは ID を主とし、時刻は補助情報(時刻のみで関連付けない)。時刻は tz-aware UTC で保存・比較する
  - [ ] 共通モデル・状態機械を追加/変更する場合、型・enum・クロスフィールドの不変条件を `__post_init__` 等で検証し、不正値の拒否をテストで固定する
  - [ ] 状態遷移契約(terminal 状態の不変性など)は docstring の宣言と実装・テストを一致させる。宣言した不変条件はフィールド単位で網羅的に検証する
  - [ ] Webhook URL・API key・token の実値をコード・docs・`.env.example` に書かない
  - [ ] Paper Signal(仮説)/ Paper Execution(仮想執行)/ Backtest(検証基盤)の責務を混同せず、Live Execution 領域(実発注・ロット計算・売買推奨文言)へ踏み込まない

### 5. テスト・検証(変更種別ごと)

**コード変更時** — 対象 → 関連 → 全体の順で段階的に広げる:

1. **対象**: 変更したコンポーネントのスイート(例: `python -m pytest paper-execution/tests -q`)。
2. **関連**: 影響マトリクスで参照が見つかった他コンポーネントのスイート。
3. **全体**: 8 スイート(コマンド一覧は [references/repository-guide.md](references/repository-guide.md)。ルートに pytest 設定はなく、ディレクトリ単位で個別に実行)。実行要否は下の「全体 pytest の要否」で判断する。

- 新規動作は正常系・異常系・境界値のテストを追加する。外部 API / Discord / S3 / 実 DB へは到達させない(Mock / tmp_path / monkeypatch)。
- Windows 固有の副作用(SQLite ファイルハンドル等)に触れる場合は CI の Windows ジョブ対象かを確認する。

**docs / Skill のみの変更時** — pytest は変更成果物を検証しない。第 1 段階の直接検証として、[references/repository-guide.md](references/repository-guide.md) の「docs / Skill 変更の直接検証チェックリスト」(frontmatter 構文・必須項目、相対リンクと参照パスの実在・起点、code fence・見出し構造、重複・docs 本文の複製、Claude Code / Codex の配置整合、シンボリックリンク、差分が Markdown / Skill / developer docs に限定されコード・Bot・DB・workflow に差分がないこと)を全項目実施する。リポジトリに Markdown lint / link checker が存在しない場合は、存在しないことと代替として実施した検査内容を PR へ記録する。

**全体 pytest の要否** — 常に固定で必須にはしない。以下で判断し、**実行・非実行のどちらでも理由を PR 本文に記載する**:

- 実行する: 実行コードに変更がある / import・package 構造に影響する / 設定・テスト収集・共通 fixture に影響する / CI や既存方針として要求される / 変更リスクに対して追加確認が有効
- 省略可: docs / Skill のみで runtime へ影響しない(理由を明記)
- 全体 pytest を実行した場合も、それだけで docs / Skill 固有の直接検証を代替しない。

**共通**: 実行しなかったスイート・検査がある場合は、その理由を PR 本文に明記する。「全部通った」とだけ書いて省略しない。失敗したテストは失敗のまま報告し、成功と偽らない。

### 6. docs 更新要否の確認

- 仕様・責務・環境変数・起動方法・DB スキーマ・テスト観点のいずれかが変わったら、対応する docs(§2 で選定したもの)と README を同じ PR で更新する。
- 変更が docs に影響しない場合も、PR 本文の Documentation impact に「影響なし」と根拠を書く。

### 7. PR 作成

PR 本文には最低限、以下の項目をこの順で記載する。テンプレートは [references/pr-template.md](references/pr-template.md) を使う。

1. Summary
2. Changed files / affected components(影響マトリクスの結果を反映)
3. Tests executed and results(docs / Skill 変更時は直接検証の内容、全体 pytest の実行/非実行の理由を含む)
4. Documentation impact
5. Known limitations / remaining work
6. Issue との対応関係(受け入れ条件ごとの充足状況)

## 禁止事項

- §0 の実装前ゲート(対象 Issue の解決、現在 PR・未解決 thread の確認)を完了せずに実装へ着手すること。
- 影響マトリクスの項目を根拠の記録なしに省略すること。
- 本 Skill へ docs の本文(仕様)を複製すること。
- Issue のスコープ外の機能追加・リファクタリング(気づいた問題は Issue 提案として報告する)。
- dry-run の無副作用保証を破る変更。
- テスト結果・未確認事項・既知制約の省略や虚偽報告。
- `.env` / `data/` / `state/` / `logs/` など Git 管理外ファイルのコミット。
