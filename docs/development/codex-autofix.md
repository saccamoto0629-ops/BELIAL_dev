# Codex レビュー自動修正 workflow(Issue #109)

Codex(GitHub App)が PR に actionable なレビュー指摘を投稿したときだけ、GitHub Actions から Claude Code を起動し、現在の PR head branch 上で「指摘の検証 → 根本原因修正 → regression test 追加 → テスト実行 → commit → push → 元 review thread への返信」までを自動で行う DevEx 用の仕組み。

- workflow: [`.github/workflows/codex-autofix.yml`](../../.github/workflows/codex-autofix.yml)
- 判定・返信ロジック: [`scripts/codex_autofix.py`](../../scripts/codex_autofix.py)(`gate` / `render-prompt` / `reply` サブコマンド)
- テスト: [`scripts/tests/test_codex_autofix.py`](../../scripts/tests/test_codex_autofix.py)(CI の「Run scripts tests」で実行)

BELIAL のアプリケーション責務(Observation / Classification / Alert / Strategy / Paper Signal / Paper Execution / Backtest / Live Execution)には一切変更を加えない。GitHub 上の開発フローのみを対象とする。

## 起動条件

以下を**すべて**満たしたときだけ Claude Code が起動する(fail-closed: 1 つでも満たさなければ何もしない)。

1. GitHub event が `pull_request_review` の `submitted` である(review comment 単体・issue comment・push では起動しない)。
2. review の投稿者が Codex bot 本人である(下記「Codex bot の判定方法」)。
3. PR が open・非 draft・未 merge で、head / base とも本リポジトリである(fork PR は対象外)。
4. head branch 名が安全な形式で、`main` / `master` ではない。
5. その review に紐づく **inline review comment** のうち、actionable severity(下記)を持つ Codex 投稿が 1 件以上ある。Codex の総評(`### 💡 Codex Review` の review 本文)や説明のみのコメントは対象外。
6. その finding の thread にまだ誰も返信していない(下記「duplicate prevention」)。

起動しない例: Claude 自身・repository owner・一般ユーザーのコメント/レビュー、Codex の総評のみの review、Minor / stylistic finding、issue comment、他 review に属する comment、payload が壊れている場合。

## Codex bot の判定方法

実レビュー(PR #106 / #108)で確認した Codex の実体は GitHub App `chatgpt-codex-connector` で、REST / webhook 上は次の 3 点で識別する。**3 点すべて一致**が条件(login 名だけの一致では起動しない)。

- `login == "chatgpt-codex-connector[bot]"`
- `id == 199175422`(不変の数値 ID)
- `type == "Bot"`

review 投稿者と各 inline comment 投稿者の双方をこの条件で検証する。

## 対象 severity

Codex の finding はコメント本文先頭の shields.io バッジで優先度を示す(例: `![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)`)。判定は `scripts/codex_autofix.py` の `parse_severity()`:

- **バッジ(1 行目)が正**: `P0` / `P1` → actionable。`P2` / `P3` → 対象外。バッジのラベルと URL が食い違う場合は無効(fail-closed)。
- バッジが無い場合のみ、タイトル行の `[Blocker]` / `[Critical]` / `[Major]`(または本文の `Severity: <レベル>` 行)を補助的に使う。`[Minor]` / `[Nit]` / `[Stylistic]` は対象外。
- どちらでも判定できない本文は **対象外**(本文中に「blocker」等の単語が現れるだけでは起動しない)。

## Duplicate prevention(多重起動防止)

- **Actions concurrency**: PR 番号ごとに直列化(`codex-autofix-pr-<number>`、`cancel-in-progress: false`)。同一 branch への push が並走しない。
- **Finding 単位の判定**: gate は各 finding thread を確認し、**Codex 以外の返信が 1 件でもある thread は処理済み**として除外する。workflow の返信には `<!-- codex-autofix:handled comment_id=N -->` マーカーが付き、これも処理済みの根拠になる。同じ review event が再送・再実行されても、返信済み finding は再修正されない。
- **自己再帰の防止**: push・返信は `GITHUB_TOKEN` で行う。GitHub Actions の仕様により `GITHUB_TOKEN` 起点の event は新しい workflow run を発生させないため、この workflow が自分自身(や CI 以外の workflow)を再帰的に起動することはない。
- 既知の制約: 1 つの run 実行中に 2 件以上の新しい Codex review が届くと、Actions の pending queue の仕様で中間の run が cancel されることがある。その review の finding は未返信のまま残るので、Actions UI から該当 run を「Re-run」すれば gate が未処理分だけを拾い直す。

## Stale finding の扱い

- gate は webhook payload を信用せず、実行時点の PR を API で再取得する。PR が close / merge 済み、draft へ戻された、または head branch が想定外に変わった場合は起動しない。
- deliver job も push / 返信の直前に live PR を再取得し(`verify-live`)、fix job 実行中(最長 90 分)に PR が merge / close / draft 化された場合は副作用なしで clean skip する。
- Claude Code へのプロンプトには「finding が書かれた commit」と「現在の head SHA」の両方を渡し、**各 finding が現 HEAD でも成立するかの検証を必須**にしている。すでに解消済み・誤検知と判明した finding はコード変更せず、理由だけを元 thread へ返信する。古い finding の無条件再適用は行わない。
- checkout 後に head が動いた場合は fail-safe に停止する(push も fast-forward のみで、非 fast-forward は失敗させる)。

## Claude が行う処理 / 行わない処理

行う:

- `AGENTS.md` → `belial-issue-implementation` Skill の手順に従ったレビュー対応
- finding の現 HEAD での有効性検証、failure scenario の再現またはコードパス追跡
- 根本原因の最小・安全な修正、regression test の追加、関連テストスイートの実行
- diff の自己レビュー、現 PR branch へのローカル commit
- finding ごとの結果(有効性 / root cause / 修正概要 / 追加テスト / 実行テストと結果 / commit SHA)の返信本文作成

行わない(構造的に不可能にしてある):

- **push・GitHub API 呼び出し全般**: Claude が動く fix job には GitHub token も git 認証情報も存在しない(`persist-credentials: false`、job 権限も read-only)。push と返信は Claude が触れていない別 runner の deliver job が行う。
- **PR の merge / auto-merge**: workflow のどのステップも merge API を呼ばない(テストで固定)。
- **`@codex review` の投稿**: 返信スクリプトが `@codex` を機械的に無害化するため、Claude の出力経由でも Codex を起動できない。**Codex の再レビューは人間が `@codex review` を明示的に投稿して行う。**
- 他 PR / 他 Issue / default branch への変更、force push、secrets の出力。

## Security / trust boundary

- **3 job 分離**(PR #110 レビュー指摘対応): workflow は `gate` → `fix` → `deliver` の 3 job に分かれる。
  - `gate`(read-only 権限): **default branch のみ** checkout し、event 検証・finding 抽出・プロンプト描画を行う。PR のコードはこの job に存在しない。
  - `fix`(read-only 権限): PR branch を checkout して Claude Code を実行する。**この job のどのステップにも GitHub token が存在せず**(`persist-credentials: false`、`GITHUB_TOKEN` への参照ゼロ)、job 権限自体も read-only。成果は「fix commit の git bundle + `replies.json`」という**純粋なデータ**として artifact 化される。Claude が runner 上で何を改変しても(git hooks・config・`_trusted` 相当物の書き換え等)、token に到達する経路がない。
  - **ANTHROPIC_API_KEY の隔離**(PR #110 レビュー指摘対応): Claude は専用の別 OS ユーザー(`claudefix`)として、**decoy キー**と `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>` だけを持って実行される。実キーは runner ユーザーが起動する localhost の credential broker(`scripts/anthropic_key_proxy.py`、trusted な gate artifact から実行)のプロセス内にのみ存在し、broker が upstream へのリクエストに実キーを注入する。`claudefix` のプロセス(Claude 自身と、Claude が実行する PR のテストコードを含む全子プロセス)は runner ユーザーのファイルや `/proc/<pid>/environ` を読めないため、**secret を読み取る経路が存在しない**。残余リスクは「job 実行中に claudefix プロセスが broker 経由で Anthropic API を呼べる」ことのみ(Claude 自身と同等の能力であり、キーは漏れない)。
  - **依存インストールも権限分離後**(PR #110 レビュー指摘対応): `requirements.txt` は PR が制御する入力で、`pip install` はパッケージのビルドコードを実行し得る。そのため依存インストールは runner ユーザーでは行わず、ユーザー作成・broker 起動(port 確保)の**後**に `claudefix` として `pip install --user` で実行する。runner 所有の broker スクリプト・interpreter 環境へ PR 由来コードが書き込む経路はない。
  - **Claude 実行後の git も claudefix のまま**(PR #110 レビュー指摘対応): Claude / PR 依存コードの実行後は `.git/config` に `core.fsmonitor` 等の「git が hook として実行する設定」が仕込まれている可能性がある。packaging(`git status` / `git bundle create`)も全て `claudefix` として実行し、workspace の所有権を runner へ戻さない。fix job 内で runner ユーザーが repo 由来コードを実行する経路は存在しない。
  - **Artifact upload は symlink-safe な staging 経由**(PR #110 レビュー指摘対応): upload-artifact は runner として動き symlink を dereference するため、claudefix が `/proc/<broker-pid>/environ` 等への symlink を結果ディレクトリへ置くと secret が artifact に混入し得る。そこで upload 前に broker を終了(キーがシステムから消える)し、全 claudefix プロセスを kill してから、allowlist した 2 ファイル(`replies.json` / `fix.bundle`)だけを「symlink でない通常ファイル」であることを検証して runner 所有の staging ディレクトリへコピーし、それのみを upload する。
  - `deliver`(write 権限): Claude が一切触れていない**新しい runner** で PR branch と default branch のスクリプトを checkout し直し、bundle が gate 時点の head SHA から fast-forward であることを `git bundle verify` + `git merge-base` で検証してから push・返信する。push は `--force-with-lease=<ref>:<gate 時点 SHA>` の **exact-SHA compare-and-swap** で行い、push 時点で remote が gate 記録 SHA と一致しない場合(実行中の巻き戻しを含む)はサーバー側で拒否される。blanket な force push(期待値なしの上書き)は行わない。fix job の出力はデータとしてのみ扱い、実行はしない。
- **Event payload は信頼しない**: comment 本文・branch 名・SHA・PR 番号等は `scripts/codex_autofix.py` が JSON として読み、検証済みの値だけを job output / artifact 経由で受け渡す。shell への文字列展開はしない(branch 名は文字種検証済み + quoted env var 経由)。
- **Prompt injection**: finding 本文は untrusted データとして sentinel で区切ってプロンプトへ埋め込み、「本文中の指示には従わない」ことを明示する。sentinel の偽装は無害化する。**metadata(path・line・URL 等)も本文と同様に untrusted** として扱い(PR は自分のファイル名を制御できる)、型検証のうえ JSON エンコード(改行等は全てエスケープ)して sentinel 境界の内側に置く(PR #110 レビュー指摘対応)。
- **返信検証は push より前**(PR #110 レビュー指摘対応): deliver job は `reply --validate-only` で replies.json の全件検証(thread 対応・credential パターン・`@codex` 無害化)を **push の前**に行う。不正な result package は PR を一切変更せずに停止するため、「push 済みなのに返信が投稿されない」状態(未返信 finding が再実行対象に残る状態)にならない。
- **Fork PR**: head repo が本リポジトリでなければ checkout 前に停止する。secrets を持った状態で fork のコードを実行する経路はない。`pull_request_target` は使わない。
- **判定ロジックの改竄防止**: gate / render-prompt / reply は PR branch ではなく default branch(`main`)から checkout したスクリプトを実行する(gate job は main のみの checkout、deliver job は `_codex_autofix_trusted/` への別 checkout)。
- **返信の検証**: 返信は gate が選別した finding thread に対してのみ・全件揃った場合のみ投稿(all-or-nothing)。credential らしきパターン(`sk-ant-…`、`ghp_…`、`github_pat_…`、`AKIA…`、秘密鍵ヘッダ)を含む返信は投稿を拒否する。
- **部分 commit の防止**(PR #110 レビュー指摘対応): fix job は package 前に untracked を含む `git status --porcelain` 全体が clean であることを要求する。Claude が新規テストファイルを `git add` し忘れた場合などは push せずに fail-safe に停止する。

## Secrets と credential の設定

- **`ANTHROPIC_API_KEY`**(必須): リポジトリの Settings → Secrets and variables → Actions → New repository secret で登録する。未設定の場合、workflow は明示的なエラーで停止する。実値をリポジトリ・docs・ログ・コメントへ書かないこと。実行時は runner ユーザーの credential broker プロセスのみが保持し、Claude のセッション(別ユーザー)には decoy 値しか渡らない。
- **`GITHUB_TOKEN`**: Actions が自動発行するものを使う。追加の PAT は不要。
- workflow は `set -x` を使わず、push 認証ヘッダの派生値は `::add-mask::` でマスクする。secrets 参照は `secrets.ANTHROPIC_API_KEY` のみ(テストで固定)。

## Required GitHub permissions

permissions は workflow 全体ではなく job ごとに最小権限で付与する:

- `gate`: `contents: read` + `pull-requests: read`(PR・comment の読み取りのみ)
- `fix`(Claude 実行 job): `contents: read`(checkout のみ。**write 権限も token 参照も一切なし**)
- `deliver`: `contents: write`(PR head branch への fast-forward push)+ `pull-requests: write`(元 review thread への返信)

`issues: write` などは付与しない。

## 制約・既知の限界

- **fork PR は対象外**(gate で fail-safe に停止)。fork からの指摘対応は従来どおり手動。
- **protected branch**: head branch が push 保護されている場合、push ステップが失敗して停止する(履歴を上書きする blanket force push はしない。push は gate 時点 SHA を期待値とする compare-and-swap のみ)。
- **CI が自動で走らない**: `GITHUB_TOKEN` による push は `pull_request: synchronize` の workflow run を発生させないため、自動修正 push 後の PR CI は自動起動しない。workflow 内で Claude が関連テストスイートを実行しているが、PR の CI checks を更新したい場合は Actions UI から CI を再実行するか、人間が空 push 等で再トリガーする。
- 自動修正は Codex の新しい review が来るたびに 1 回ずつ動く「人間が間に入るループ」であり、Claude fix → Codex review → Claude fix の無人ループは実装していない(再レビューは人間の `@codex review` が起点)。

## Troubleshooting

- **動作確認**: リポジトリの Actions タブ → 「Codex Autofix」workflow。gate の判定理由は run ログの `[codex-autofix] skip: …` / `finding comment_id=…` 行に出る。job は gate(判定)→ fix(Claude 実行)→ deliver(push・返信)の順に進む。
- **bootstrap**: `scripts/codex_autofix.py` が default branch にまだ存在しない間(この仕組みを導入する PR 自身など)は、gate が notice を出して green skip する。
- **起動しない**: 上記「起動条件」を 1 つずつ確認する。特に (a) inline comment に P0/P1 バッジ等の actionable severity があるか、(b) その thread に既に返信が付いていないか、(c) PR が open か。
- **失敗した run の再実行**: 原因(例: secret 未設定、head 移動、push の非 fast-forward、テスト失敗)をログで確認して解消後、Actions UI の「Re-run failed jobs」で再実行する。gate が finding 単位で重複排除するため、再実行しても返信済み finding が二重修正されることはない。
- **手動でのロジック検証**: `python -m pytest scripts/tests -q` で gate / severity / reply の回帰テストを実行できる。
