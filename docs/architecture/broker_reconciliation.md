# Broker Reconciliation — 差分検出と安全な注文状態修復(Issue #103 / #104)

BELIAL 内部の注文状態と Broker 側の注文状態を照合し、不一致を分類・記録する**検出基盤**(Issue #103)と、そのうち安全に確定できる注文状態だけを内部へ冪等に反映する**修復サービス**(Issue #104)。実装は `execution/`(`reconciliation_models.py` / `reconciliation_repair_models.py` / `reconciliation_store.py` / `broker_reconciliation.py` / `broker_reconciliation_repair.py`)にある。

> **重要な位置づけ**
>
> - **検出サービスは検出専用(detection only)である。** 内部注文状態を変更しない・Broker へ注文を送信しない・Broker 注文をキャンセルしない。Broker へは**照会系操作のみ**(`get_order` / `list_orders`)を発行する。
> - **修復サービスが変更できるのは BELIAL 内部の注文状態だけであり、必ず既存の注文状態機械を経由する。** Broker への write 操作(submit / cancel / replace / close)は一切行わず、Broker へは `get_order` のみを発行する。SQL の直接 UPDATE も行わない(唯一の書き込み経路は `execution_store.record_order_result`。AST 検査で固定)。
> - position / trade / SL・TP の照合と OANDA Demo による手動照合は **Issue #105** の責務であり、未実装。
> - scheduler・常駐 Bot への組み込みは行っていない。実行はサービス API の明示的な呼び出しのみ。
> - 注文状態の唯一の正は既存の `broker.OrderState` と `validate_order_state_transition()` / `validate_order_result_update()`([Broker Interface](broker_interface.md)、Issue #73)。本基盤は**注文状態 Enum・遷移表を新設・緩和しない**。

```python
from execution.broker_reconciliation import BrokerOrderReconciliationService
from execution.broker_reconciliation_repair import BrokerOrderReconciliationRepairService
from execution.reconciliation_models import DiscrepancyType, ResolutionCategory
from execution.reconciliation_repair_models import RepairMode, RepairOutcome
from execution import reconciliation_store
```

## 依存方向

```text
BrokerOrderReconciliationService         ← execution/broker_reconciliation.py(検出)
   ├─ read-only → execution_store(execution_orders.sqlite3)
   ├─ query-only → Broker interface(get_order / list_orders)
   └─ append   → reconciliation_store(broker_reconciliation.sqlite3)

BrokerOrderReconciliationRepairService   ← execution/broker_reconciliation_repair.py(修復)
   ├─ read     → reconciliation_store(保存済み run の読込。dry-run は true read-only)
   ├─ query-only → Broker interface(get_order のみ)
   ├─ write    → execution_store.record_order_result(既存状態機械経由の唯一の書き込み)
   └─ append   → reconciliation_store(repair attempt 監査行)
```

- 検出サービスは execution order DB へ**読み取りのみ**。書き込み API(`create_execution_order` / `mark_submitting` / `record_order_state` / `record_order_result` / `init_db`)は呼ばない(AST 検査で固定)。
- 修復サービスの execution order DB への書き込みは **`record_order_result` の 1 呼び出しのみ**(AST 検査で「ちょうど 1 箇所」を固定。`execute` 等の raw SQL 呼び出しも 0)。
- Broker へは両サービスとも照会のみ。`submit_order` / `cancel_order` の呼び出し箇所はリポジトリ全体の AST 検査で禁止されている(`execution/tests/test_submission_boundary.py`)。

## 共通モデル(`reconciliation_models.py`)

すべて `dataclass(frozen=True)` + `Enum` + timezone-aware UTC。分類軸は文字列ではなく Enum で管理する。

| モデル | 内容 |
|---|---|
| `ReconciliationRun` | 1 回の照合結果: `run_id` / `mode` / 開始・終了時刻 / `checked_order_count`(比較した内部注文数)/ `discrepancies` / `broker_listing_error` |
| `OrderDiscrepancy` | 1 件の差分: 種別 / 対象種別 / 対象 ID / resolution category / 観測時刻 / 判定理由(`detail`)/ 内部観測状態 / Broker 観測状態 / `client_order_id` / `broker_order_id` / `broker_error_code` |
| `ReconciliationMode` | `detection_only` のみ。run 行へ保存し、「その run が何を許されていたか」をコードのバージョンから推測させない |
| `ReconciliationTargetType` | `execution_order`(`target_id` = `execution_request_id`)/ `broker_order`(`target_id` = `client_order_id`。内部 ID が存在しない Broker 側のみの注文) |

`checked_order_count` があるため「差分 0 件」と「そもそも 1 件も見ていない」を区別できる。`has_partial_failures` は対象別 `query_failure` または列挙失敗/未対応の有無から導出し、一部だけ未確認の run を完全成功として表示しないための機械可読フラグである。

### discrepancy type

| 種別 | 意味 |
|---|---|
| `in_flight_broker_filled` | 内部は発注中(pending / accepted / partially_filled)、Broker は FILLED |
| `in_flight_broker_cancelled` | 内部は発注中、Broker は CANCELLED |
| `in_flight_broker_rejected` | 内部は発注中、Broker は REJECTED |
| `unknown_broker_settled` | 内部は UNKNOWN、Broker 側は状態が確定している(accepted / partially_filled / filled / cancelled / rejected) |
| `broker_only_order` | Broker にのみ注文が存在する(対応する Execution Request が無い) |
| `broker_order_missing` | 内部は送信済みだが Broker が当該 `client_order_id` を知らない(`BrokerOrderNotFoundError`)。**「未発注」の証明ではない** |
| `state_mismatch` | 上記のいずれにも当てはまらない不一致(終端状態同士の矛盾、Broker が別注文について回答した場合など) |
| `query_failure` | Broker 照会自体が失敗した。状態は一切推測しない |

**両側の状態に加え、注文 identity・broker_order_id・history anchor を含む snapshot 更新契約が一致している注文だけを discrepancy として記録しない。** 終端状態(filled / cancelled / rejected)も照合対象に含める。BELIAL 内では終端状態を変更できないが、Broker が異なる終端事実を返す場合は `state_mismatch` + `manual_review` として監査し、黙って除外しない。送信試行のない `created` intent のみ対象外。

内部・Broker の双方が `unknown` の場合も「確定状態を観測できた」とは扱わず、`state_mismatch` + `manual_review` として残す。状態の推測・修復は行わない。

### resolution category

| category | 意味 |
|---|---|
| `auto_repair_candidate` | 既存の注文状態機械・execution store の契約で**情報を失わずに**反映できる観測。Issue #103 は分類のみで**修復しない** |
| `manual_review` | 人手確認が必要。単純反映できない差分・終端状態の矛盾・片側にしか存在しない注文 |
| `query_failed` | 照会失敗により判定不能。`query_failure` と 1:1(モデル不変条件および DB CHECK で相互に固定) |

`auto_repair_candidate` の判定は、既存 validator と `record_order_result` の前提条件を**再利用**して行う(判定ロジックを再実装しない)。

1. `validate_order_state_transition(内部状態, Broker 状態)` が通ること
2. Broker snapshot が保存済み intent と一致すること(symbol / side / order_type / quantity / requested_price)
3. `broker_order_id` が `NULL → 値 → 同値` の連鎖を壊さないこと
4. **Broker snapshot の `broker_order_id` が他の Execution Request に割当済みでないこと**(`broker_order_id` は execution order テーブル**全体で UNIQUE**。他注文が所有する ID を反映しようとすると `record_order_result` が制約違反で全体 rollback する。所有者が照合中の注文自身である場合は競合ではない)
5. history に anchor された snapshot があれば `validate_order_result_update(anchored, observed)` が通ること
6. `updated_at_utc` が逆行しないこと

いずれかを満たさない場合は `manual_review` とし、`detail` に阻害理由を残す。所有者衝突の場合は **競合した `broker_order_id`・現所有者の `execution_request_id`・照合中の `execution_request_id`・候補にできない理由**を `detail` に記録する(認証情報や Broker レスポンス全体は記録しない)。

**fail-closed**: 上記判定に必要な execution order store の読み取り(anchored snapshot の取得・`broker_order_id` 所有者の照会)が失敗した場合、推測で `auto_repair_candidate` にせず `manual_review` とし、失敗内容を `detail` に残す。`query_failure` を使わないのは、それが **Broker 照会**失敗専用の分類であり型付き `BrokerErrorCode` を必須とするため(内部 DB の読み取り失敗は Broker エラーではない)。run 全体を失敗させないのは、既存設計と同じく対象単位で追跡可能にするためである。

## 照会失敗の扱い

- **対象 ID を特定できる失敗**(`get_order` の timeout / 通信失敗 / 認証失敗 / rate limit)は `query_failure` discrepancy として記録する。対象 ID・理由・型付き `BrokerErrorCode` が残り、run は他の注文の照合を続行する(1 件の失敗が成功した観測を隠さない)。
- **対象 ID に帰属しない失敗**(Broker 側注文の**列挙**失敗、または列挙非対応の Broker)は run 行の `broker_listing_error` に記録する。この間、`broker_only_order` が 0 件であることは「存在しない」ではなく「**確認していない**」を意味する(`ReconciliationRun.broker_orders_enumerated` が機械可読な区別)。
- いずれの場合も、内部状態を FILLED / CANCELLED / REJECTED や「未発注」へ推測で確定しない。

## Broker 側注文の列挙(`OrderListingBroker`)

Broker 側にのみ存在する注文は、照会だけでは観測できない(存在を知らない ID は照会できない)。そのため `broker/broker_interface.py` に**追加の optional Protocol** `OrderListingBroker`(`list_orders() -> tuple[OrderResult, ...]`)を定義した。

- Issue #73 の `Broker` Protocol は**5 操作のまま変更していない**(テストで固定)。既存実装(FakeBroker / OANDA Demo Adapter / 将来の Adapter)は無変更で `Broker` を満たし続ける = **後方互換**。
- FakeBroker は既に保持している注文レコードを決定的順序(`client_order_id` 昇順)で返す `list_orders()` を実装する。新しい scripting API は増やしていない。
- 列挙非対応の Broker(現時点の OANDA Demo Adapter を含む)では、上記のとおり `broker_listing_error` に「未確認」を記録する。

## 自動修復(Issue #104、`broker_reconciliation_repair.py`)

`BrokerOrderReconciliationRepairService.repair(run_id=..., mode=...)` は、**保存済みの** reconciliation run を入力として、各 discrepancy を観測順に処理する。未保存の run(`detect()` のみの結果や捏造されたオブジェクト)は入力にできない — repair は必ず記録済みの観測に紐づく。

### 修復できる状態 / できない状態

- **修復対象**: `target_type = execution_order` かつ `resolution_category = auto_repair_candidate` の discrepancy のみ。既存遷移表が許可する範囲で、UNKNOWN → FILLED / CANCELLED / REJECTED / ACCEPTED / PARTIALLY_FILLED、および SUBMITTING(PENDING)/ ACCEPTED / PARTIALLY_FILLED → Broker で確認された許可状態を確定できる。
- **修復しない**: `manual_review`(終端矛盾・identity 不一致・broker_order_id 喪失/衝突・Broker のみの注文)と `query_failed`(何も観測していない)は `skipped` として監査に残すのみ。Broker にのみ存在する注文の自動採用、Broker に存在しない position の自動 close、SL / TP の再作成は行わない(非スコープ)。

### repair eligibility の再検証(保存結果を無条件に信用しない)

適用直前に毎回、以下を再検証する。いずれかが崩れていれば内部状態は変更されず、typed な結果 (`RepairOutcome`) として記録される。

1. 現在の内部行を再読取し、`client_order_id` の対応が保存時の観測と一致すること(不一致 = `rejected`)。
2. **現在の内部状態が reconciliation 時に観測した内部状態と一致すること。** 別処理で進んでいた場合: 既に観測どおりの Broker 状態へ到達済み(broker 参照も整合)なら `already_applied`(no-op)、それ以外は `stale`(古い観測で新しい状態を巻き戻さない)。
3. **Broker へ再照会**(`get_order`)し、fresh snapshot が保存済み観測と一致すること。状態が異なる(UNKNOWN へ戻った場合を含む)・broker 参照 ID が変わった場合は `conflict`(新しい reconciliation run での再分類が必要)。照会失敗(`BrokerError` / timeout / not found / 返却契約違反)は `query_failed`(型付き `BrokerErrorCode`。#103 と同じく想定外例外も scoped fail-closed)。
4. **既存状態機械・store 契約の再検証**: intent identity 一致、`validate_order_state_transition`、anchored snapshot に対する `validate_order_result_update`、時刻の非逆行。拒否は `rejected`(遷移表は緩和しない)。
5. **Broker 参照 ID を失わない**: 既知の `broker_order_id` の NULL 化・別値への置換は `conflict`。fresh snapshot の ID が別 Execution Request に所有されている場合(DB 全体 UNIQUE)も `conflict`。判定に必要な store 読み取りの失敗は `failed`(fail-closed。修復可能性を推測しない)。

### 適用・冪等性・並行実行

- 適用は `execution_store.record_order_result(..., expected_current_state=観測した内部状態)` の 1 呼び出しのみ。`expected_current_state` は store の `BEGIN IMMEDIATE` トランザクション内で現在状態と比較される**原子的 compare-and-set** であり(Issue #104 で追加)、読み取り〜適用間の TOCTOU を閉じる。競合に敗れた worker は `ExecutionOrderStateConflictError` を受けて再読取し、`already_applied` / `stale` として結果化する。
- したがって同一 repair を複数回・複数 worker で実行しても、**状態履歴は高々 1 行しか増えず**、最終状態は Broker が確認した状態のまま壊れない(`already_applied` は成功と区別される明示的な no-op)。
- 適用時の状態履歴 reason には `run_id` / `attempt_id` を埋め込む。**状態履歴が「repair が適用された」ことの source of truth** である。

### 監査記録と DB 障害時の整合性

- すべての attempt(applied / already_applied / skipped / stale / conflict / rejected / query_failed / failed)は、対象 ID・実行前後の内部状態・観測した Broker 状態・Broker 参照 ID・観測時刻(`observed_at_utc`)・試行時刻・適用時刻・理由とともに `broker_reconciliation_repair_attempts` へ **append** される(観測 = run / discrepancy 行は書き換えない。「修復済みフラグ」は存在しない)。
- 状態更新(execution orders DB)と repair 監査行(reconciliation DB)は別ファイルであり単一トランザクションにできない。分散トランザクション風の実装は行わず、**更新を先に commit → 監査 append を後**とし、監査 append が失敗した場合は attempt に `audit_error` を付けて返す + ERROR ログ(`ReconciliationRepairResult.has_audit_failures` で機械検出)。状態履歴に provenance が残っているため、**repair を再実行すれば `already_applied` として監査行が復元**される — 「更新済みだが監査なし」は検出可能かつ再実行で回復可能であり、中途半端な内部状態は残らない(store 側は全 rollback)。
- 更新途中の DB 障害は store のトランザクションが全 rollback し、attempt は `failed` として監査される。

### 照合のみモード(dry-run)

`mode=RepairMode.DRY_RUN` は上記の評価(store 読み取り + Broker 照会)をすべて実行するが、**一切書き込まない**: 注文更新なし・履歴追加なし・監査行なし・DB 作成 / migration なし(v1 監査 DB は migration せず明示的に拒否)・state file / ファイルシステム副作用なし。適用可能な対象は `would_apply` として返る(`applied` とは区別され、永続化もされない)。

## 永続化(`reconciliation_store.py`)

独立 DB `execution/data/broker_reconciliation.sqlite3`(既定。`BROKER_RECONCILIATION_DB_PATH` で上書き。相対パスは `execution/` 基準)。所有コンポーネントは `execution/` のみ。他 store と同居しない理由は既存 store と同じ(各 store が closed-world で schema を検証するため)。加えて、reconciliation 専用 DB に**注文テーブルが存在しない**こと自体が「この store が注文状態を書き換えられない」構造的保証になる。repair 監査行(Issue #104)を第 5 の DB に分けず**この同一 DB に置く**のは、観測と修復が同じ broker-reconciliation コンポーネントの 2 つの監査系列であり、attempt 行から run 行への実 FK を張れるため。

### schema(version 2)

| テーブル | 列 |
|---|---|
| `broker_reconciliation_runs`(WITHOUT ROWID) | `run_id`(PK)/ `mode` / `started_at_utc` / `finished_at_utc` / `checked_order_count` / `broker_listing_error` |
| `broker_reconciliation_discrepancies` | `discrepancy_id`(PK AUTOINCREMENT)/ `run_id`(FK → runs)/ `discrepancy_type` / `target_type` / `target_id` / `resolution_category` / `internal_state` / `broker_state` / `client_order_id` / `broker_order_id` / `broker_error_code` / `detail` / `observed_at_utc`。同一 run の `(target_type, target_id)` は UNIQUE |
| `broker_reconciliation_repair_attempts`(WITHOUT ROWID、Issue #104 / v2) | `attempt_id`(PK)/ `run_id`(FK → runs)/ `mode` / `target_type` / `target_id` / `execution_request_id` / `client_order_id` / `outcome` / `reconciliation_internal_state` / `reconciliation_broker_state` / `internal_state_before` / `internal_state_after` / `broker_order_id` / `broker_error_code` / `detail` / `observed_at_utc` / `attempted_at_utc` / `applied_at_utc` |

index: `idx_broker_reconciliation_discrepancies_run (run_id, discrepancy_id)`、`idx_broker_reconciliation_repair_attempts_run (run_id, attempted_at_utc, attempt_id)`。CHECK 制約がモデル不変条件を二重化する(既知 enum 値のみ、UTC 接尾辞、非負カウント、`query_failure` ⇔ `query_failed` ⇔ `broker_error_code IS NOT NULL`、attempt 側は `mode = 'apply'` のみ・`would_apply` 非永続・`applied` ⇔ `applied_at_utc`・`applied`/`already_applied` ⇔ `internal_state_after`・`query_failed` ⇔ `broker_error_code`)。

### 重複方針(明示)

**run 単位の append**。1 run = runs 行 1 件 + discrepancies 行 N 件。**同一内容の観測でも run ごとに独立した行として残す**(dedup・上書き・UPSERT はしない)。これは既存の監査設計(`safety_gate_decisions` は判定を毎回 append、`execution_order_state_history` は観測を毎回 append)と同じ立場であり、「いつ何を観測したか」を再構成可能に保つためである。

一方、**同一 run 内の同一 target は最大1件**であり、モデル不変条件と DB の `UNIQUE (run_id, target_type, target_id)` の両方で固定する。Broker 列挙が client/broker ID を重複して返した場合は broker-only 差分を二重記録せず、列挙全体を曖昧として `broker_listing_error` に残す。

`run_id` は呼び出し側が与える PRIMARY KEY。**既存 run_id での再記録は `ReconciliationRunConflictError` で拒否**し、保存済みの run を書き換えない。したがって同じ照合を何度繰り返しても、内部注文状態も既存の記録も壊れない。

repair attempt(Issue #104)も同じ立場の **attempt 単位の append**: すべての試行(適用しなかったものを含む)が独立した監査イベントであり、repair の再実行は新しい attempt 行(通常 `already_applied`)を append するだけで、既存の run / discrepancy / attempt 行を更新しない。既存 `attempt_id` の再記録は `RepairAttemptConflictError` で拒否。dry-run の attempt は**記録自体を拒否**する(無副作用契約)。

### migration / 初期化

`init_reconciliation_db()` は**冪等**で、pristine な DB(`sqlite_master` 0 件かつ `user_version` 0)にのみ schema を作成し、現行 schema の既存 DB は closed-world 検証のみを行う(既存行は無変更)。**v1 → v2 migration(Issue #104)**: v1 のテーブル DDL は v2 と完全同一のため純粋 additive であり、**リリース済み v1 と完全一致した DB のみ**を単一トランザクションで migration する(repair テーブル + index 作成 → version bump → v2 完全再検証 → COMMIT。途中失敗は全 rollback で v1 ファイル無変更)。**読み取り経路は migration しない**(v1 DB は `init_reconciliation_db()` を指すエラーで拒否 = dry-run / read-only の無副作用契約を維持)。それ以外(partial schema・制約欠損 lookalike・trigger / view・他コンポーネントの DB)は **DB を変更せず**明示的に失敗する。読み取りは true read-only 接続(`execution/sqlite_ro.py`)で行い、存在しない DB を作成しない。復元時はモデル不変条件で厳格に再検証する(帯域外改竄は `ReconciliationIntegrityError`)。

## 記録先(3 系統)

| 系統 | 内容 |
|---|---|
| 戻り値 | `ReconciliationRun`(discrepancy 全件・分類・件数・列挙可否) |
| ログ | discrepancy 1 件ごとに WARN(run_id / 種別 / 対象種別 / 対象 ID / client_order_id / 両側状態 / category / error code / 理由)、run 単位で INFO サマリ。**token / account ID / 認証情報は出力しない**(照合は口座情報を参照しない) |
| DB | 上記 schema(`run()` 実行時。`detect()` は保存しない) |

## API

```python
service = BrokerOrderReconciliationService(
    broker=broker,                                   # Broker(照会のみ使用)
    execution_db_path="execution/data/execution_orders.sqlite3",
    reconciliation_db_path="execution/data/broker_reconciliation.sqlite3",
)
run = service.run(run_id="recon-2026-08-04T00:00Z")   # 検出 + ログ + DB 記録
run = service.detect(run_id="...")                    # 検出 + ログのみ(DB 無副作用)

repairer = BrokerOrderReconciliationRepairService(
    broker=broker,                                   # Broker(get_order のみ使用)
    execution_db_path="execution/data/execution_orders.sqlite3",
    reconciliation_db_path="execution/data/broker_reconciliation.sqlite3",
)
result = repairer.repair(run_id="recon-...")                            # 適用 + 監査 append
result = repairer.repair(run_id="recon-...", mode=RepairMode.DRY_RUN)   # 評価のみ(無副作用)
```

`detect()` は reconciliation DB を作成しない(dry-run 的な確認に使える)。`run()` は `init_reconciliation_db()`(冪等)の後に append する。`repair()` は**保存済み run のみ**を入力とし、APPLY モードでは既存 DB を検証(必要なら v1 → v2 migration)してから処理する(DB が存在しなければエラー。新規作成はしない)。

## 非スコープ(Issue #104 時点)

position / trade / transaction / SL・TP の照合、OANDA Demo Adapter を使った手動照合(Issue #105)、Broker への注文送信・キャンセル・replace・close(修復は内部 DB のみ)、Broker 側のみの注文の自動採用、Broker に存在しない内部 position の自動 close、SL / TP 注文の再作成、Broker 状態の推測、無条件の自動決済、scheduler / 常駐 Bot への組み込み、Live 接続解禁。

## テスト

`execution/tests/test_reconciliation_models.py` / `test_reconciliation_repair_models.py` / `test_reconciliation_store.py` / `test_broker_reconciliation.py` / `test_broker_reconciliation_repair.py`、および `broker/tests/test_fake_broker.py` / `test_broker_interface.py`。観点は [テスト仕様](../testing/test_specification.md) を参照。すべて `tmp_path` の SQLite と in-memory FakeBroker のみで、外部通信・Discord・実 DB 更新は発生しない。
