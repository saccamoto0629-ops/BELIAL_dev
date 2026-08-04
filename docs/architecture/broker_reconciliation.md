# Broker Reconciliation — 差分検出基盤(Issue #103)と安全な注文状態修復(Issue #104)

BELIAL 内部の注文状態と Broker 側の注文状態を照合し、不一致を分類・記録する**検出専用**基盤(#103)と、その結果のうち安全に確定できる注文状態だけを既存の注文状態機械経由で反映する**修復サービス**(#104)。実装は `execution/`(`reconciliation_models.py` / `reconciliation_store.py` / `broker_reconciliation.py` / `reconciliation_repair.py`)にある。

> **重要な位置づけ**
>
> - **検出は detection only である。** `BrokerOrderReconciliationService` は内部注文状態を変更しない・Broker へ注文を送信しない・Broker 注文をキャンセルしない。Broker へは**照会系操作のみ**(`get_order` / `list_orders`)を発行する。
> - **修復(#104)は検出結果を入力とする別サービス**であり、`auto_repair_candidate` に分類された注文状態のみを対象に、書き込みは `execution_store.record_order_result`(Issue #74 の状態機械・store 契約)の 1 経路だけで行う。Broker へは修復時も照会のみ。
> - position / trade / SL・TP の照合と OANDA Demo による手動照合は **Issue #105** の責務であり、実装していない。
> - scheduler・常駐 Bot への組み込みは行っていない。実行はサービス API の明示的な呼び出しのみ。
> - 注文状態の唯一の正は既存の `broker.OrderState` と `validate_order_state_transition()` / `validate_order_result_update()`([Broker Interface](broker_interface.md)、Issue #73)。本基盤は**注文状態 Enum・遷移表を新設しない**。

```python
from execution.broker_reconciliation import BrokerOrderReconciliationService
from execution.reconciliation_models import DiscrepancyType, ResolutionCategory
from execution.reconciliation_repair import BrokerOrderRepairService, RepairMode
from execution import reconciliation_store
```

## 依存方向

```text
BrokerOrderReconciliationService   ← execution/broker_reconciliation.py
   ├─ read-only → execution_store(execution_orders.sqlite3)
   ├─ query-only → Broker interface(get_order / list_orders)
   └─ append   → reconciliation_store(broker_reconciliation.sqlite3)

BrokerOrderRepairService           ← execution/reconciliation_repair.py(Issue #104)
   ├─ 入力     → ReconciliationRun(#103 の検出結果)
   ├─ query-only → Broker interface(get_order のみ)
   └─ 書き込み → execution_store.record_order_result のみ(#74 状態機械経由)
```

- 検出サービスは execution order DB へは**読み取りのみ**。書き込み API(`create_execution_order` / `mark_submitting` / `record_order_state` / `record_order_result` / `init_db`)は呼ばない(AST 検査で固定)。
- 修復サービスの書き込みは `record_order_result` **のみ**(注文作成・phase 遷移・state-only 記録・DB 初期化・reconciliation store への書き込み・SQLite 直接接続はすべて AST 検査で禁止)。
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

## 永続化(`reconciliation_store.py`)

独立 DB `execution/data/broker_reconciliation.sqlite3`(既定。`BROKER_RECONCILIATION_DB_PATH` で上書き。相対パスは `execution/` 基準)。所有コンポーネントは `execution/` のみ。他 store と同居しない理由は既存 store と同じ(各 store が closed-world で schema を検証するため)。加えて、reconciliation 専用 DB に**注文テーブルが存在しない**こと自体が「照合が注文状態を書き換えられない」構造的保証になる。

### schema(version 1)

| テーブル | 列 |
|---|---|
| `broker_reconciliation_runs`(WITHOUT ROWID) | `run_id`(PK)/ `mode` / `started_at_utc` / `finished_at_utc` / `checked_order_count` / `broker_listing_error` |
| `broker_reconciliation_discrepancies` | `discrepancy_id`(PK AUTOINCREMENT)/ `run_id`(FK → runs)/ `discrepancy_type` / `target_type` / `target_id` / `resolution_category` / `internal_state` / `broker_state` / `client_order_id` / `broker_order_id` / `broker_error_code` / `detail` / `observed_at_utc`。同一 run の `(target_type, target_id)` は UNIQUE |

index: `idx_broker_reconciliation_discrepancies_run (run_id, discrepancy_id)`。CHECK 制約がモデル不変条件を二重化する(既知 enum 値のみ、UTC 接尾辞、非負カウント、`query_failure` ⇔ `query_failed` ⇔ `broker_error_code IS NOT NULL`)。

### 重複方針(明示)

**run 単位の append**。1 run = runs 行 1 件 + discrepancies 行 N 件。**同一内容の観測でも run ごとに独立した行として残す**(dedup・上書き・UPSERT はしない)。これは既存の監査設計(`safety_gate_decisions` は判定を毎回 append、`execution_order_state_history` は観測を毎回 append)と同じ立場であり、「いつ何を観測したか」を再構成可能に保つためである。

一方、**同一 run 内の同一 target は最大1件**であり、モデル不変条件と DB の `UNIQUE (run_id, target_type, target_id)` の両方で固定する。Broker 列挙が client/broker ID を重複して返した場合は broker-only 差分を二重記録せず、列挙全体を曖昧として `broker_listing_error` に残す。

`run_id` は呼び出し側が与える PRIMARY KEY。**既存 run_id での再記録は `ReconciliationRunConflictError` で拒否**し、保存済みの run を書き換えない。したがって同じ照合を何度繰り返しても、内部注文状態も既存の記録も壊れない。

### migration / 初期化

新規 schema(v1)であり migration 経路は存在しない。`init_reconciliation_db()` は**冪等**で、pristine な DB(`sqlite_master` 0 件かつ `user_version` 0)にのみ schema を作成し、既存 DB は closed-world 検証のみを行う(既存行は無変更)。それ以外(partial schema・制約欠損 lookalike・trigger / view・他コンポーネントの DB)は **DB を変更せず**明示的に失敗する。読み取りは true read-only 接続(`execution/sqlite_ro.py`)で行い、存在しない DB を作成しない。復元時はモデル不変条件で厳格に再検証する(帯域外改竄は `ReconciliationIntegrityError`)。

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
```

`detect()` は reconciliation DB を作成しない(dry-run 的な確認に使える)。`run()` は `init_reconciliation_db()`(冪等)の後に append する。

## 安全な注文状態修復(Issue #104。`reconciliation_repair.py`)

`BrokerOrderRepairService.repair(run, mode=...)` は #103 の `ReconciliationRun` を入力に、discrepancy 1 件につき 1 件の `OrderRepairAction` を返す(対象外もスキップ理由付きで返し、黙って落とさない)。

```python
repair = BrokerOrderRepairService(broker=broker, execution_db_path=...)
result = repair.repair(run, mode=RepairMode.DRY_RUN)  # 照合のみモード: 完全無副作用
result = repair.repair(run, mode=RepairMode.APPLY)    # 安全な修復のみ適用
```

### 修復の対象と手順

1. **対象は `auto_repair_candidate` かつ `execution_order` の discrepancy のみ。** `manual_review`(要確認対象)・`query_failed`・`broker_order`(Broker のみの注文)は Broker へ照会すらせず `not_eligible`。
2. **修復時に Broker へ再照会する**(`get_order`)。検出時の観測を信用して適用することはない。照会失敗(型付き `BrokerError`・contract 違反の想定外例外)は `query_failed`、応答が確定状態でない場合(UNKNOWN・PENDING・not found)は `broker_state_unconfirmed` として**内部状態を一切変更しない**。
3. **現在の内部状態を再読込する。** 既に観測状態と一致していれば `already_consistent`(書き込みゼロ)。検出時の状態から動いていれば `internal_state_changed`(stale な結果は適用せず、再検出を促す)。
4. **許可される修復元は UNKNOWN / PENDING(= SUBMITTING)/ ACCEPTED / PARTIALLY_FILLED のみ**、反映できるのは Broker が確定回答した状態(accepted / partially_filled / filled / cancelled / rejected)のみ。terminal 状態からの離脱は拒否。
5. **repairability を修復時に再証明する**: 検出と同じ共有判定 `classify_order_repairability()`(遷移検証・intent 一致・broker_order_id 不変性と DB 全体 UNIQUE の所有者確認・anchored snapshot 更新契約・時刻の非逆行。store 読み取り失敗は fail-closed)。阻害があれば `refused`。
6. **apply モードのみ** `execution_store.record_order_result()` で反映する。状態機械・store 契約の全検証が単一トランザクション内で再実行され、拒否は全体 rollback + `refused`。**状態を直接 DB 更新する経路は存在しない**(AST 検査で固定)。

### 記録内容

- execution store の履歴行に **Broker snapshot 全体**(fills・`broker_order_id`)が anchor され、`changed_at_utc` = Broker snapshot の `updated_at_utc`、`reason` に**修復元 run ID・Broker 確定状態・修復時観測時刻・`client_order_id` / `broker_order_id`** が入る。
- 戻り値 `OrderRepairRun` は action ごとに**対象 ID・修復前状態・修復後状態・観測 Broker 状態・理由・観測時刻**を持つ(モデル不変条件で強制)。
- 修復結果自体は**永続化しない**(戻り値 + ログのみ)。注文に何が起きたかの監査は従来どおり execution store の状態履歴が担い、reconciliation DB(#103)は検出専用のまま。

### Dry Run(照合のみモード)

`RepairMode.DRY_RUN` は Broker 照会・store 読み取り・判定まで同一に行い、**DB 更新・履歴追加・状態変更を一切行わない**(execution DB はバイト単位で不変。テストで固定)。全チェックを通過した対象は `would_repair` として報告される。

### Idempotency

同じ `ReconciliationRun` を何度 apply しても壊れない: 1 回目で修復された注文は 2 回目以降 `already_consistent` となり**書き込みゼロ**(履歴も増えない)。二重更新は発生せず、修復後の再検出は discrepancy 0 件になる。

### 禁止事項(#104 で実装しないこと)

Broker のみの注文の自動採用、Broker が知らない内部注文の close 扱い、position / trade / transaction の照合、SL・TP の再作成、Live 注文、Broker 照会失敗・UNKNOWN 応答での状態更新、要確認対象の自動修復。

## 非スコープ(Issue #103 / #104 時点)

position / trade / transaction / SL・TP の照合、OANDA Demo Adapter を使った手動照合(Issue #105)、Broker への注文送信・キャンセル、Broker 側のみの注文の自動採用、無条件の自動決済、scheduler / 常駐 Bot への組み込み、Live 接続解禁。

## テスト

`execution/tests/test_reconciliation_models.py` / `test_reconciliation_store.py` / `test_broker_reconciliation.py` / `test_reconciliation_repair.py`、および `broker/tests/test_fake_broker.py` / `test_broker_interface.py`。観点は [テスト仕様](../testing/test_specification.md) を参照。すべて `tmp_path` の SQLite と in-memory FakeBroker のみで、外部通信・Discord・実 DB 更新は発生しない。
