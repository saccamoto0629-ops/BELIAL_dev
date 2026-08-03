# BELIAL Repository Guide(Skill 参照資料)

BELIAL Issue 実装時に参照するコンポーネント・ドキュメント・テストの対応表。**仕様の本文はここに書かない。** 各項目の正は右列の docs / README を参照する。

## コンポーネント対応表

| コンポーネント | コード | README | 設計書(docs/architecture/) | テスト | 所有 DB |
|---|---|---|---|---|---|
| news-feed-bot | `news-feed-bot/` | `news-feed-bot/README.md` | `market_snapshot.md`, `market_bias.md` | `news-feed-bot/tests/` | `market_bias.sqlite3` |
| market-watch | `market-watch/` | `market-watch/README.md` | `market_watch.md`, `market_snapshot.md` | `market-watch/tests/` | `chart_data.sqlite3` |
| paper-signal | `paper-signal/` | `paper-signal/README.md` | `paper_signal.md` | `paper-signal/tests/` | `paper_signals.sqlite3` |
| paper-execution | `paper-execution/` | (なし。設計書を参照) | `paper_execution.md` | `paper-execution/tests/` | `paper_positions.sqlite3` |
| backtest | `backtest/` | `backtest/strategies/README.md`(Strategy 定義) | `strategy_definition.md`、実行方法は `docs/backtest.md` | `backtest/tests/` | (backtest 結果 DB。`backtest_store.py` 参照) |
| broker | `broker/` | `broker/README.md` | `broker_interface.md` | `broker/tests/` | なし(インメモリのみ) |
| execution | `execution/` | `execution/README.md` | `execution_request.md`, `safety_gate.md`, `shadow_execution.md` | `execution/tests/` | `execution_orders.sqlite3`(Issue #74)+ `safety_gate_decisions.sqlite3`(Safety Gate 判定監査。Issue #75)+ `shadow_results.sqlite3`(Shadow Mode 検証記録。Issue #78)。いずれも将来のLive Execution用 |

全体像・DB 構成・データ連携は `docs/architecture/architecture.md`、責務レイヤーと非スコープは `docs/product/concept.md` が正。

## ドキュメント選定の目安

| 変更内容 | 必ず読む docs |
|---|---|
| どの変更でも | `docs/product/concept.md`(レイヤー分離・非スコープ)、`docs/architecture/architecture.md`(該当節) |
| 環境変数・起動・systemd・CLI | `docs/operations/operations.md` |
| DB スキーマ・store 層 | `docs/architecture/architecture.md` の「SQLite schema validation / migration」+ 該当コンポーネント設計書 |
| dry-run に関わる経路 | `docs/architecture/architecture.md` の「dry-run 方針」、`docs/operations/operations.md` |
| テスト追加・変更 | `docs/testing/test_specification.md` |
| Backtest CLI・Strategy YAML | `docs/backtest.md`, `docs/architecture/strategy_definition.md` |
| Issue / PR の立て方・粒度 | `docs/development/issues-guideline.md` |
| リリース済み機能の位置づけ | `docs/product/roadmap.md` |

## テストスイート実行コマンド

ルートに `pytest.ini` / `conftest.py` / `pyproject.toml` は存在しない。**ディレクトリ単位で個別に**実行する(CI `.github/workflows/ci.yml` と同じ 7 スイート)。

```bash
python -m pytest backtest/tests -q
python -m pytest market-watch/tests -q
python -m pytest news-feed-bot/tests -q
python -m pytest paper-signal/tests -q
python -m pytest paper-execution/tests -q
python -m pytest broker/tests -q
python -m pytest execution/tests -q
```

- 依存は repo ルートの `requirements.txt`(`python -m pip install -r requirements.txt`)。
- テストは外部ネットワーク・実 DB・実 S3・実 Discord に到達してはならない。
- paper-execution は CI に Windows ジョブがある(SQLite ファイルハンドル / snapshot cleanup)。Windows 固有の副作用に触れる変更では Windows での挙動を考慮する。
- CI はこの 7 スイートに加えて、リポジトリルートからの `broker` / `execution` パッケージ import smoke test を実行する。

## 影響マトリクス評価項目

SKILL.md §3 が要求する評価項目。変更ごとに全項目を `Relevant`(確認内容と影響)/ `Not relevant`(非該当の根拠)/ `Unknown`(未確認事項と残るリスク)のいずれかで記録する。根拠なしの省略は不可。

| # | 評価項目 |
|---|---|
| 1 | 所有コンポーネント(どのコンポーネントの責務か) |
| 2 | Concept / Architecture 上の所有レイヤー |
| 3 | caller / 呼び出し元(コード・テスト・docs・CI・scripts) |
| 4 | callee / 呼び出し先(変更対象が依存する側) |
| 5 | 外部 I/O 境界(Discord / S3 / 外部 API / ネットワーク) |
| 6 | SQLite またはその他の永続化 |
| 7 | DB 作成・更新・migration・schema validation |
| 8 | read-only 経路(存在しない DB・ファイルを作成しないか) |
| 9 | Discord 通知 |
| 10 | state ファイル |
| 11 | S3 |
| 12 | 環境変数・設定ファイル(`.env.example` / `config.toml` 等) |
| 13 | CLI 引数と終了コード |
| 14 | systemd・常駐 Bot・定期実行(timer) |
| 15 | 既存 DB・既存データとの後方互換性 |
| 16 | 既存運用コマンド・起動導線 |
| 17 | エラー処理(異常を握り潰していないか。failure channel への記録・終了コード・再送は所有 docs の失敗種別ごとの契約に従っているか) |
| 18 | Paper Signal / Paper Execution / Backtest / Live Execution の責務境界 |
| 19 | 秘密情報の扱い(Webhook URL / API key / token) |
| 20 | dry-run の無副作用保証 |

## 横断契約の要点(正は docs。本文をここへ複製しない)

| 契約 | 参照先 |
|---|---|
| store は所有コンポーネント以外の DB を migration しない | `docs/architecture/architecture.md`「SQLite schema validation / migration」 |
| read-only 処理(dry-run の schema probe・レポート等)は存在しない DB・ファイルを作成しない | 同上 + 「dry-run 方針」 |
| dry-run で DB 作成・migration・Discord 送信・state 更新・S3 アップロードを行わない | `docs/architecture/architecture.md`「dry-run 方針」、`docs/operations/operations.md` |
| dry-run・strict read の読み取りエラー・schema 不正を成功扱いしない(cycle failure として計上) | `docs/architecture/architecture.md`「dry-run 方針」 |
| 異常系の failure channel・終了コード・再送・状態維持は失敗種別ごとに所有 docs と回帰テストの契約に従う(delivery 失敗と data-plane / outbox 失敗を同一視しない) | `docs/architecture/paper_execution.md`(失敗分類)、`docs/operations/operations.md`「Paper Execution 通知 outbox の運用」 |
| Paper Signal は仮説、Paper Execution は仮想執行、Backtest は検証基盤 | `docs/product/concept.md`「設計思想」「レイヤー分離」 |
| Live Execution は未実装・非スコープ | `docs/product/concept.md`「非スコープ」 |
| 既存の運用導線(起動コマンド・systemd・CLI)を壊さない | `docs/operations/operations.md`「起動方式 / 本番運用」 |

## docs / Skill 変更の直接検証チェックリスト

docs / Skill のみの変更では、pytest ではなく以下を第 1 段階の直接検証として全項目確認し、結果を PR へ記録する。

- [ ] YAML frontmatter の構文が正しい(Skill の場合)
- [ ] 必須 frontmatter 項目(`name`, `description`)が存在し、`name` がディレクトリ名と一致する
- [ ] 相対リンクがすべて解決できる
- [ ] 参照先ファイル・ディレクトリが実在する(文中の backtick パス含む)
- [ ] 相対パスの起点(ファイル位置基準か repo ルート基準か)が正しい
- [ ] Markdown code fence が開閉対で整合している
- [ ] 見出し構造(レベルの飛び・重複)が壊れていない
- [ ] 既存 docs・Skill 内との重複記述を増やしていない
- [ ] 既存 docs 本文の過剰な複製がない(要点と参照先に留まっている)
- [ ] Claude Code 側の配置(`.claude/skills/<name>/SKILL.md`)・利用方法の記述が正確
- [ ] Codex 側の配置・利用方法の記述が正確(`AGENTS.md` 経由の参照であり、ネイティブ Skill selector 登録ではない)
- [ ] シンボリックリンクを使う場合は実体と Windows / Linux / GitHub 互換性を確認(不使用ならファイル mode が通常ファイルであること)
- [ ] 変更ファイルが Markdown / Skill / developer docs に限定されている
- [ ] 実行コード・Bot・DB・workflow(`.github/workflows/`)に差分がない(`git diff` で確認)

リポジトリに Markdown lint / link checker は**現在存在しない**(`.markdownlint*`・pre-commit 設定なし、CI は pytest のみ)。存在しない間は、上記を手動またはスクリプトで検査し、実施内容を PR に記録する。lint が導入されたらそれを実行する。

## 影響範囲調査のコマンド例

```bash
# 関数・クラス・定数の参照元(コード・テスト・docs・CI・scripts を含める)
grep -rn "対象シンボル" --include="*.py" --include="*.md" --include="*.yml" .

# 環境変数の参照元(.env.example も対象)
grep -rn "ENV_VAR_NAME" --include="*.py" --include="*.md" --include="*.example" .

# DB テーブル・列の参照元
grep -rn "table_or_column_name" --include="*.py" --include="*.md" .
```

- 動的 import(`importlib` 等)や文字列組み立てによる参照は grep で漏れることがある。境界となるモジュールでは AST ベースの検査テスト(例: `broker/tests` の依存禁止テスト)の有無も確認する。
- 他コンポーネントから import される共有境界を追加・変更したときは、リポジトリルートからの import 可否(`python -c "from broker.broker_interface import Broker"` のような smoke test)を CI に含める。

## 過去のレビューで繰り返された指摘(先回りチェック)

実装前に、同種の問題がないか確認する。仕様の正は各 docs。

- パッケージ import 構成: テストだけ `sys.path` を細工して素の import 失敗を隠していないか(PR #87 r1)。
- 共通モデルの型検証: dataclass の enum / tuple / クロスフィールド不変条件を `__post_init__` で検証し、不正値拒否テストがあるか(PR #87 r1)。
- 状態契約の網羅性: docstring で宣言した不変条件(例: terminal 状態の content 不変)が、対象フィールド全部に対して実装・テストされているか(PR #87 r3)。
- 一意性制約: ID 類(fill_id 等)の一意性を宣言するなら検証もするか(PR #87 r4)。
- dry-run / snapshot の Windows 挙動: SQLite 接続の close 漏れによる cleanup 失敗(Issue #83 r2)。
- 通知等の副作用は transactional outbox など再送安全な形か(Issue #83 r1)。
