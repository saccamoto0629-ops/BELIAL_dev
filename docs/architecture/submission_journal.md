# Submission Journal(Issue #93)

Broker 送信前後の送信試行を、Execution Order DB(Issue #74)とは**独立した耐久媒体**へ記録する Submission Journal の仕様。実装は `execution/submission_journal.py`(store)と `execution/live_submission.py`(唯一の公認送信経路への統合)。

> **重要な位置づけ**
>
> - Journal は**記録のみ**を行う。Broker 呼び出し・外部通信・実発注は一切行わない。OANDA Demo / Live Adapter(Issue #79 / #82)と reconciliation(Issue #103〜#105)は実装済みだが、**実 Broker へ実発注を駆動する runner・reconciliation の定期実行は未実装**であり、実口座での Live 運用は未解禁である(Issue #131 の runner core は `fake` / `shadow` のみを駆動し、`fake` では本 Journal を正規経路として通る)。
> - Paper Execution / Paper Signal / Strategy とは無関係・独立である(これらは Broker 境界に触れない)。
> - 注文状態の唯一の正は既存 `broker.OrderState` のまま。Journal は注文状態機械を新設しない — 記録するのは**送信試行のライフサイクル**(Broker 呼び出しの前後関係と Execution DB 保存の成否)である。

## 目的(なぜ Execution DB と別ファイルか)

`LiveOrderSubmissionService` は Broker 応答を Execution Order DB へ記録し、保存失敗時は state-only UNKNOWN へフォールバックする(Issue #75 review r3)。しかし Execution Order DB **自体**が schema 異常・read-only・破損・ファイル消失で利用不能な場合、結果保存・UNKNOWN 記録・`list_unresolved_submissions()` が同時に失敗し、Broker が保持しうる注文を耐久的に追跡できなくなる。Submission Journal は専用 SQLite ファイル(既定 `execution/data/submission_journal.sqlite3`、`SUBMISSION_JOURNAL_DB_PATH` で上書き)にこの追跡を分離し、Execution DB 全損時にも未解決送信を列挙・restart 後復元できるようにする。

## 記録内容(1 送信試行 = 1 行、`execution_request_id` が PK)

| 列 | 内容 |
|---|---|
| `execution_request_id` | PK。送信試行の identity(Issue #74 の intent と 1:1) |
| `client_order_id` | Broker べき等キー。**テーブル全体で UNIQUE** |
| `symbol` / `side` / `order_type` / `quantity` / `requested_price` | **broker へ渡る `OrderRequest` の全内容フィールド**(数量・価格は canonical Decimal 文字列。`requested_price` は LIMIT のときのみ非 NULL — `OrderRequest.limit_price` と同一契約を CHECK で強制) |
| `execution_mode` / `connection_target` | 型付き接続 identity(`BrokerConnection` から導出された値) |
| `submission_started_at_utc` | 送信試行の開始時刻(reclaim で更新) |
| `broker_call_state` | `not_started` → `started` → `responded` \| `unknown` |
| `broker_response_state` / `broker_order_id` | Broker 応答(`OrderState` 値。`responded` のときのみ非 NULL) |
| `execution_db_save_state` | `not_attempted` → `saved` \| `failed`(OrderResult の Execution DB 保存) |
| `outcome` | 機械可読サマリ: `open` → `completed` \| `reconciliation_required` \| **`superseded`**(schema v2 / Issue #116。下記「superseded への終端化」) |
| `last_error` | 失敗理由(unknown / save-failed で必須) |
| `updated_at_utc` | 最終更新時刻(単調。過去時刻の更新は拒否) |
| `superseded_by_execution_request_id` | schema v2 / Issue #116。`outcome = superseded` のときのみ非 NULL(相互制約を CHECK + モデルで強制): この intent を退役させた再発行子の ID(監査用) |

`started` は **`Broker.submit_order()` 呼び出しの直前**に書かれる。したがって `not_started` は「この intent で Broker が呼ばれたことは一度もない」ことの耐久的な証明であり、`started` 以降は「呼ばれた可能性がある」と保守的に扱う。

ライフサイクルの整合(`completed` ⇔ `saved`、`unknown` / `failed` → `reconciliation_required` + `last_error` 必須、`responded` 以前は応答値を持てない等)は、モデル(`SubmissionJournalEntry.__post_init__`)と DB CHECK 制約の二重で強制される。

## 送信順序と書き込み境界

```text
service.submit(execution_request_id, market=...)
    1. 意図復元・phase 検査・Gate 評価・判定の耐久保存 + read-back(Issue #75。従来どおり)
    2. Journal へ送信試行を記録(record_submission_intent。not_started)
         失敗 → submission_journal_failed で拒否(Broker は呼ばれない)
         重複 → duplicate_submission で拒否(下記「二重送信防止」)
    3. execution store の created → submitting 遷移を獲得(従来どおり)
    4. Journal を started へ更新(mark_broker_call_started。Broker 呼び出し直前の最後の書き込み)
         失敗 → submission_journal_failed で拒否(Broker は呼ばれない。entry は not_started のまま = 未送信の証明)
    5. Broker.submit_order()
         例外 → Journal へ unknown を記録(best-effort)→ execution store へ UNKNOWN 記録 → re-raise
    6. Broker 応答を Journal へ記録(record_broker_response。best-effort)
    7. OrderResult を execution store へ保存(record_order_result)
         失敗 → Journal へ応答 + 保存失敗を記録(record_execution_db_save_failed =
                reconciliation_required。best-effort だが Journal が耐久記録の正)
                → execution store へ UNKNOWN フォールバック(従来どおり)→ re-raise
    8. Journal を完了へ更新(mark_completed = responded + saved + completed。best-effort)
```

- **Broker 呼び出し前の Journal 失敗はすべて fail-closed**: `submission_journal_failed`(または `duplicate_submission`)として拒否・監査保存され、`Broker.submit_order()` は呼ばれない。Journal DB の欠損(未初期化)・schema 不一致・書き込み失敗はすべてこの経路に入る。
- **Broker 呼び出し後の Journal 失敗は best-effort**: critical ログを残して処理を続行する(注文は存在しうるので、Journal 障害を拒否・例外へ変換して結果記録を妨げない)。entry は前の状態に留まり `list_unresolved()` から見え続ける。
- 手順 6 の応答記録が失われても、手順 7 失敗時の `record_execution_db_save_failed` / 手順 8 の `mark_completed` は `started` からの遷移も受理して応答内容ごと記録する(lost write の自己修復)。

## 二重送信防止(Execution store と独立の第二ガード)

- 主ガードは従来どおり execution store(phase ≠ created → `duplicate_submission`、`created → submitting` は一度きり)。
- Journal は**独立の**第二ガードを追加する: `record_submission_intent` は、同一 `execution_request_id` の entry が `not_started` 以外(= Broker が呼ばれた可能性がある)なら `SubmissionJournalConflictError` を返し、サービスはこれを `duplicate_submission` で拒否する。payload 不一致、他 request が持つ `client_order_id` も conflict。**比較する payload は broker へ渡る `OrderRequest` の全フィールド**(client_order_id / symbol / side / order_type / quantity / requested_price)+ 接続 identity(mode / target)であり(PR #113 レビュー P1)、MARKET → LIMIT の変更や指値の移動など**注文内容が 1 項目でも変わった再登録は reclaim されない**。
- これにより **Execution Order DB が消失・再作成され同じ intent が再 register された場合でも**、Journal が 2 回目の Broker 呼び出しを構造的に阻止する(受け入れ条件「同一 execution_request_id を再実行しても二重送信されない」の Execution DB 非依存の保証)。
- `not_started` の entry を持つ意図が**訂正差し替え**された場合、その entry は**監査可能な終端状態へ収束する**(Issue #95 の契約 → **Issue #116 で実装済み**。下記「superseded への終端化」)。

## superseded への終端化(schema v2 / Issue #116)

差し替え(supersession)によって「送信されないことが確定した」intent の終端 outcome は **`superseded`** である。entry のライフサイクル・reclaim 規則・二重送信ガードそのものは不変で、以下の契約で終端化する。

- **遷移可能なのは `not_started` + `open` の entry のみ**(`mark_superseded(execution_request_id, superseded_by_execution_request_id=..., at_utc=...)`)。`started` / `responded` / `unknown` は Broker 到達の可能性があるため**構造的に終端化できず**(typed error + DB CHECK)、`completed` は既に終端である。**終端化が送信可能性を増やすことはない**: superseded な entry は reclaim されず(`record_submission_intent` は conflict)、以後のライフサイクル更新(`mark_broker_call_started` を含む)もすべて typed に拒否される。
- **監査**: `superseded_by_execution_request_id` が退役の根拠(再発行子)を named provenance として保持する(`outcome = superseded` ⇔ 非 NULL の相互制約)。同一の superseding ID による `mark_superseded` の再実行は冪等 no-op、異なる ID は conflict。
- **遷移タイミングと再試行**(Issue #116 で確定):
  1. **losing submit path** — `mark_submitting()` が `ExecutionOrderSupersededError` で敗れた submit(`intent_superseded` 拒否)は、直前に自分が記録した `not_started` entry を best-effort で終端化する(失敗は WARN のみ。拒否自体は揺らがない)。
  2. **訂正差し替えの登録直後** — `ReissueService.register_gate_correction()` が子の commit 後に親の `not_started` entry を best-effort で終端化する。
  3. **耐久 recovery sweep** — `ReissueService.terminalize_superseded_intents()` が execution store の耐久 supersession marker(`execution_intent_supersessions`)全件と Journal を突き合わせ、`not_started` + `open` の entry だけを終端化する(冪等・再起動後も可)。**競合で遅れて記録された未送信 intent**(親の submit が intent を記録した直後に superseded となる経路)もこの sweep が取りこぼさない。他の状態の entry は `not_terminalizable` として報告のみ行い、`list_unresolved()` に残す(Broker 到達可能性が常に勝つ)。
- **`list_unresolved()` は terminal な outcome(`completed` / `superseded`)を除外**する: 送信されないことが確定した intent が未解決として恒久残存することはない。
- 例外は **reclaim** のみ: entry が `not_started`(Broker 未呼び出しの証明)かつ payload 完全一致の場合に限り、同じ行を再利用して attempt 時刻を更新する。Broker に到達しなかった試行(手順 3 のインフラ失敗など)の正当な再実行を、二重送信ハザードなしで許すための規則である。reclaim も他の更新と同じ**時刻単調性**に従う: `started_at_utc` が保存済み `updated_at_utc` より過去なら拒否し、耐久 timestamp を巻き戻さない(PR #113 レビュー P2)。

## クラッシュ境界ごとの扱い(再送 / 照会 / reconciliation)

どの境界でも**自動即時再送は行わない**。「再送可」とは、通常の `submit()` 再実行が既存ガードを通過して安全に送信へ到達しうる、の意味である。

| # | クラッシュ / 障害点 | Journal | Execution DB | 扱い |
|---|---|---|---|---|
| 1 | intent 記録(手順 2)前 | entry なし | `created` | Broker 未送信。再 submit 可(通常経路そのもの) |
| 2 | intent 記録後〜遷移獲得(手順 3)前 | `not_started` / `open` | `created` | Broker 未送信(Journal が証明)。再 submit 可(reclaim) |
| 3 | 遷移獲得後〜started(手順 4)前 | `not_started` / `open` | `submitting` / `pending` | Broker 未送信(Journal が証明)。ただし execution store の再送禁止(phase ≠ created)が優先され自動再送はない。`list_unresolved_submissions()` と `list_unresolved()` の両方から見え、reconciliation が「Broker に存在しない」ことを確認して閉じる |
| 4 | started 後〜Broker 応答前(呼び出し中含む) | `started` / `open` | `submitting` / `pending` | **送信結果不明**。両ガードが再送を拒否。`client_order_id` による Broker 照会で解決(reconciliation) |
| 5 | Broker 応答後〜応答記録(手順 6)前 | `started` / `open` | `submitting` / `pending` | 同上(照会で解決)。プロセスが生存していれば手順 7/8 が応答内容ごと自己修復する |
| 6 | 応答記録後〜Execution DB 保存(手順 7)前 | `responded` / `open` | `submitting` / `pending` | Broker 応答は Journal に耐久保存済み。reconciliation が Broker 照会・Execution DB への反映(Issue #104 の修復経路)で解決 |
| 7 | Execution DB 保存失敗(プロセス生存) | `responded` + `failed` / **`reconciliation_required`** | UNKNOWN 記録(それも失敗なら stuck `submitting`/`pending`) | Issue #93 の中核。Journal が応答内容込みで reconciliation required を耐久保持。再送は両ガードが拒否 |
| 8 | Execution DB 保存後〜完了更新(手順 8)前 | `responded` / `open` | `submitted` + 確定状態 | 実害なし。Journal 上は未解決として残るが、Execution DB との突合(journal `open` × exec `submitted`)で保存済みと機械判定できる。閉じるのは reconciliation(自動 close は非スコープ) |
| 9 | Journal 自体が利用不能 | — | — | **Broker 呼び出し前**: fail-closed(送信しない)。**Broker 呼び出し後**: critical ログ + Execution DB への記録は継続(注文の事実を失わない)。従来の `list_unresolved_submissions()` が引き続き機能する |

`UNKNOWN`(手順 5 の例外経路)も同様: Journal は `unknown` / `reconciliation_required`、Execution DB は UNKNOWN 記録。解決は後続照会の記録のみで、再送はどの経路でも拒否される。

## Cancellation Journal(schema v3 / Issue #99)

公認キャンセル経路(`LiveOrderSubmissionService.cancel()`、[Safety Gate / Live Order Submission](safety_gate.md) の「公認キャンセル経路」)の**耐久 cancel claim**。同一 SQLite ファイル内の専用テーブル `cancellation_journal`(1 注文 = 1 claim 行、`execution_request_id` PK・`client_order_id` UNIQUE)であり、submit 側と同じライフサイクル(`not_started` → `started` → `responded` | `unknown`、`started` は `Broker.cancel_order` 直前の最後の書き込み)を持つ。

- **なぜ必要か(PR #124 CodeX レビュー P1)**: 注文状態だけでは「Broker cancel を送った後・outcome を書く前にプロセスが停止した」クラッシュ窓を塞げない(row は ACCEPTED のままで、再実行が事前検査を通過して二重 cancel を送ってしまう)。並行 2 呼び出しが同時に事前検査を通過する窓も同様。claim の一意獲得(`BEGIN IMMEDIATE` 直列化)と `not_started → started` の一度きり遷移が、この両方を構造的に阻止する。
- **outcome**: `open` → `completed`(post-cancel 結果を Execution DB へ耐久保存)/ `reconciliation_required`(cancel outcome 消失、または応答受領後の Execution DB 保存失敗 = 応答内容込みで耐久記録)/ **`broker_refused`**(確定拒否 `BrokerCancellationError` の terminal。応答 snapshot なし・拒否理由を記録)。`superseded` は cancel には存在しない。
- **reclaim**: `not_started`(Broker cancel が送られていないことの耐久的証明)は同一 claim として、terminal `broker_refused`(Broker に係属中の cancel なし)は **新規試行**(`attempt_count` + 1)として reclaim できる。それ以外(`started` / `unknown` / `reconciliation_required` / `completed`)は conflict = 公認経路が `cancel_attempt_unresolved` で拒否し、Broker は呼ばれない。時刻後退 reclaim・別 `client_order_id` への束縛替えは拒否。
- **read API**: `get_cancellation_entry()` / `list_unresolved_cancellations()`(terminal = `completed` / `broker_refused` 以外を旧い順に列挙)。Execution Order DB が全損していても未解決 cancel を列挙できる。突合の自動実行・自動修復は submit 側と同じく非スコープ(reconciliation 側 Issue)。

## 不整合の機械可読な識別

- Journal 単独: `outcome`(`open` / `completed` / `reconciliation_required` / `superseded`)+ `broker_call_state` × `execution_db_save_state` が試行の到達点を機械可読に示す。`list_unresolved()`(`outcome` が terminal = `completed` / `superseded` 以外)と `list_reconciliation_required()` が read API。
- Journal × Execution DB の突合: `execution_request_id` で対応づけ、`outcome = reconciliation_required`(Broker 応答が Execution DB に無い)、`open` × exec `submitted`(完了更新のみ喪失 = 境界 8)、`not_started` × exec `submitting`(未送信のまま遷移だけ獲得 = 境界 3)を判別できる。突合の自動実行・自動修復は非スコープ(reconciliation 側 Issue)。

## Journal 自身の schema validation と障害時挙動

他 store(execution / safety / shadow / reconciliation store)と同一方針:

- `initialize()` は **pristine な DB のみ**へ schema を新規作成(`sqlite_master` 0 件 + `user_version` 0)。それ以外は完全検証し、不一致なら **DB 無変更で** `SubmissionJournalSchemaError`。
- **schema version は v3**(v1 = Issue #93 初版、**v2 = Issue #116 で `superseded` outcome + `superseded_by_execution_request_id` 列 + ライフサイクル CHECK の置換**、**v3 = Issue #99 / PR #124 レビュー P1 で cancellation journal テーブルの追加**)。**v1 → v2 migration**: `initialize()` が「完全な構造検証で v1 と一致した DB のみ」を単一 `BEGIN IMMEDIATE` トランザクションで migration する。v2 はテーブル本体の inline CHECK を書き換えるため(SQLite の `ALTER TABLE` では不可能)、journal テーブルを **rebuild** する(v2 テーブル新設 → 全行を無変更コピー(新列 NULL。v2 制約は v1 の緩和なので全 v1 行が満たす)→ 行数検証 → 旧テーブル DROP → index 再作成 → version bump → v2 完全再検証 → 全行のモデル再検証 → COMMIT)。**v2 → v3 migration は purely additive**(空の `cancellation_journal` テーブル + index の追加のみ。既存 submission 行は無変更・v1 からは連鎖 migration)。途中失敗は全 rollback で旧バージョンのファイル無変更。読み取り経路は migration しない。
- 全 read / write が「open → **closed-world schema 検証**(sqlite_master 全 object 集合 + token 正規化 DDL + PRAGMA 構造 signature + version)→ トランザクション」で動作。trigger / view / 制約欠損 lookalike / 他コンポーネント DB は拒否。
- 書き込みは同一トランザクション内で **commit 前 read-back 検証**(更新行がモデル不変条件を満たして読み戻せること)を行い、失敗は全 rollback。
- 書き込みは missing DB を暗黙作成しない(未初期化 = 失敗 = 送信されない)。読み取りは **true read-only 接続**(`execution/sqlite_ro.py`)で missing DB を作成せず(missing → 空)、保存行を厳格に再検証して帯域外編集を `SubmissionJournalIntegrityError` で拒否する。
- Journal store は送信サービス内部に**封印**される: `LiveOrderSubmissionService` のコンストラクタは `journal_db_path`(filesystem パス)しか受け取らず、journal オブジェクトの注入点は存在しない(監査 store と同じ方針。Issue #75 review r3)。

## テスト

`python -m pytest execution/tests -q`(`tmp_path` の SQLite と FakeBroker のみ。外部通信・実発注なし)。

- `test_submission_journal.py` — store のライフサイクル round-trip と reopen(プロセス再起動相当)復元、遷移前提条件・時刻単調性(**reclaim の時刻後退拒否を含む**)、reclaim と conflict(進行後 / payload 不一致(**order_type 変更・指値移動を含む broker-bound 全フィールド**)/ client_order_id 競合)、未解決列挙、missing DB(write 失敗 + ファイル非作成 / read 空 + ファイル非作成)、pristine 判定・foreign schema・制約欠損 lookalike・trigger・version 不一致の拒否(DB 無変更)、帯域外改竄の復元時拒否、DB CHECK 発火、モデル不変条件。サービス統合: 正常送信の completed 記録、**Journal 欠損 / schema 破損 / started 書き込み失敗の fail-closed(Broker 呼び出し 0 件)**、**Broker 受理後の Execution DB 障害(ファイル消失 / バイト破損 / schema 改変 / read-only 接続)で Journal に reconciliation_required が残ること**、**Execution DB を消去・再作成しても Journal が再送を阻止すること**、timeout(UNKNOWN)の記録と再送拒否、Broker 呼び出し後の Journal 障害が結果記録を妨げないこと(critical ログ + 未解決列挙)、lost response write の自己修復、遷移インフラ失敗後の reclaim 再実行、プロセス再起動後の未解決復元。
- `test_submission_journal.py`(Issue #116 追加分)— `mark_superseded` の終端化(not_started 限定・冪等 no-op・別 provenance の conflict・possibly-sent 全状態の拒否・entry 不在 / 時刻後退 / 自己参照の拒否)、superseded 後の reclaim / 遷移拒否、`list_unresolved` からの terminal 除外、v2 の DB CHECK 発火(started × superseded / superseded × 参照 NULL / open × 参照あり)、**v1 → v2 migration**(rebuild 後の全行保持・migration 後の通常ライフサイクル・冪等性・検証失敗時の完全 rollback と retry 成功)。
- `test_submission_journal.py`(Issue #99 追加分)— cancellation journal: claim の round-trip と reopen 復元、`started` 後の再取得拒否(クラッシュ窓)、unknown / save-failed 後の conflict と `list_unresolved_cancellations()` への出現、`not_started` reclaim と時刻後退拒否、`broker_refused` 終端化 + 新規試行 reclaim(`attempt_count` 増分)、started の一度きり遷移、client_order_id 束縛、`CancellationJournalEntry` 不変条件、**v2 → v3 migration**(additive・既存行無変更・冪等・検証失敗時の完全 rollback と retry 成功)。サービス統合は `test_cancel_submission.py`([Safety Gate / Live Order Submission](safety_gate.md) の「テスト」)。
- 既存 `test_safety_gate.py` / 固定シナリオ(Issue #77)は新しいコンストラクタ引数(`journal_db_path`)へ追随し、従来契約(二重送信防止・UNKNOWN 再送禁止・全拒否経路で Broker 送信 0)を回帰として維持する。

## 非スコープ

- reconciliation runner の定期実行、Journal と Execution DB の突合の自動実行、Broker 照会結果による自動修復・entry の自動 close(reconciliation の検出・修復・手動 CLI は Issue #103〜#105 で実装済み。定期実行は未実装)
- OANDA Adapter・Demo / Live API 接続・実口座への注文(Adapter は Issue #79 / #82 で実装済み。Live 接続は全ロック成立時のみ・運用は未解禁)
- 動的な資金管理、Strategy / Paper Signal の変更、Paper Execution への Broker 接続
