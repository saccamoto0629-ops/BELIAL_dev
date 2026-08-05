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

### attempt ライフサイクル(claim → 評価 → fenced commit → finalize)

APPLY モードの各 attempt は監査 DB 上で **2 段階のレコード**になる(pending → finalized。`RepairAttemptConflictError` により finalize はちょうど 1 回)。attempt ID は**全対象分を処理開始前に生成・検証**し(空・重複・factory 例外はどの副作用よりも前に pass 全体を失敗させる)、既に永続化済みの ID の再利用は **reservation 時点**(= 状態更新の前)で拒否される。

pending 行は単なる「予約」ではなく **claim(所有権)** である。claim は `owner_id`(worker 名)/ `claim_execution_id`(**実行単位**の ID。同一 worker の別実行を区別する)/ `generation`(≥ 1、takeover ごとに increment)/ `lease_expires_at_utc` / `last_claimed_at_utc` から成る。

```text
claim(reservation: owner A / generation 1 / lease = now + lease_duration、pending 行を commit)
    ↓ owner A が lease を保持したまま評価(eligibility 再検証 + Broker 再照会)
    ↓ generation 付き Execution commit(fence 登録 → record_order_result が transaction 内で fence 照合)
    ↓ finalize(attempt_id + owner + claim_execution_id + generation + status=pending の CAS)
finalized
```

lease 期限切れ後は recovery worker が新しい generation で takeover できる。

```text
owner A / generation 1
    ↓ lease expiry(それ以前は誰も — 同じ owner_id を使う別実行であっても — 触れない)
owner B / generation 2 が takeover(fence bump → claim CAS の順で原子的に)
    ↓
owner A / generation 1 の遅延 commit は Execution DB の fence 照合で拒否される
owner A / generation 1 の遅延 finalize は claim CAS で拒否される
```

**claim 契約**

- 新規 reservation: `owner_id` = 実行中の worker、`generation` = 1、`lease_expires_at_utc` = `reserved_at + lease_duration`、`status` = pending。
- lease 有効中の pending は**再取得できない**。判定に使うのは owner 名ではなく **lease の期限だけ**なので、同一 service instance・同一 `owner_id` からの並行呼び出しでも二重実行にはならない。
- takeover は lease 期限切れ後のみ。`claim_repair_attempt()` が `UPDATE ... SET generation = generation + 1, owner_id = ?, claim_execution_id = ?, lease_expires_at_utc = ? WHERE attempt_id = ? AND status = 'pending' AND generation = ?` という**原子的 CAS** を発行する。N 個の recovery worker が同時に走っても勝者はちょうど 1 つで、敗者は typed な `RepairClaimLostError`(既に finalized なら `RepairAttemptConflictError`)を受け取り**何も変更しない**。
- lease 期限判定は呼び出し側 clock の契約、原子性は CAS の責務、と役割を分離している(store は clock を持たない)。

**fencing(Execution DB 側)** — `record_order_result()` の transaction から reconciliation DB の claim を読むことはできない(SQLite ファイルを跨ぐ単一トランザクションは存在しない)ため、**案 A: Execution DB 内の fence mirror**(`execution_repair_fences`)を採用する。

```text
1. reconciliation DB で claim 取得(新規 = generation 1 / takeover = generation N+1)
2. Execution DB で同じ (attempt_id, generation) の fence を原子的に登録・takeover
3. record_order_result() が同じ transaction 内で fence 一致を検証
4. 一致する generation だけが状態更新できる
5. takeover 後、旧 generation の commit は ExecutionRepairFenceConflictError で全 rollback
```

takeover の順序は **fence を先、claim CAS を後**にする(旧 generation が commit できなくなってから初めて再評価を始めるため)。ただし **mutex は claim CAS の方**であり、fence は「どの generation なら commit してよいか」の barrier にすぎない。

**孤立 fence の adoption(再入可能な takeover)**: 2 つの cross-DB commit(fence 更新 → claim CAS)は原子化できないので、fence を更新した直後にプロセスが停止すると「fence = generation N+1 / 監査 claim = generation N」という不一致が残る。この fence は**どの claim にも裏付けられていない孤立 fence**であり、次の worker が(自分の attempt ID と目標 generation に**完全一致する場合に限り**)そのまま **adopt** する — 何も書き換えずに claim CAS へ進み、勝者だけが commit する。adopt を認めないと、その attempt は「fence は N+1 なので登録できない / claim は N のままなので誰も進めない」という状態で**永久に pending のまま回収不能**になる(PR #107 再レビュー r3 の P1 指摘)。adopt の条件は「同一 attempt ID かつ同一 generation」だけで、より新しい generation・別 attempt・fence 不在・Execution DB 読み取り失敗はいずれも adopt せず(fail-closed)、fence を追い越された worker が barrier をすり抜ける余地はない。

**recovery 契約**(`recover_pending_attempts()`): pending reservation を列挙し、**lease が有効なものは一切触らない**(finalize しない・takeover しない・Broker 照会しない・Execution DB を変更しない)。lease 期限切れのものだけを次の順で処理する。

1. Execution DB の fence を `generation + 1` へ bump(敗者は typed conflict を受け、**孤立 fence だった場合のみ** adopt して次へ進む。それ以外は何も書かずに離脱する)。
2. reconciliation DB の claim を同じ generation へ CAS で takeover(**ここが単一勝者を決める**)。
3. state history の**構造化 provenance 列 `repair_attempt_id` / `repair_generation`**(reason 文字列の解析はしない)を確認。
4. 既に適用済みなら**元の attempt ID のまま** `applied` へ finalize(commit 済みの状態・broker ID・broker 時刻で確定)。
5. 未適用なら、新 generation の正当な owner として **fresh Broker 再照会 + eligibility 再検証を最初からやり直す**(適用できるなら applied、できないならその理由の typed outcome)。
6. **「history に無い」だけを根拠に `failed` へ確定することはしない。** Broker 再照会なしに失敗認定してよい根拠はない。

元の run / discrepancy が削除・改竄されていた場合は fail-closed で復旧を拒否する(`ReconciliationRepairError`)。broker-only target のように execution order を持たない reservation は fence を使わず claim CAS だけで takeover する。

### repair eligibility の再検証(保存結果を無条件に信用しない)

適用直前に毎回、以下を再検証する。いずれかが崩れていれば内部状態は変更されず、typed な結果(`RepairOutcome` + 安定 `failure_code`)として finalize される。

1. 現在の内部行を再読取し、`client_order_id` の対応が保存時の観測と一致すること(不一致 = `rejected`)。
2. **Broker へ再照会**(`get_order`)。**`already_applied` の判定を含むすべての結論の前に必ず実行**する。照会失敗(`BrokerError` / timeout / not found / Broker 境界契約違反)は `query_failed`(型付き `BrokerErrorCode`。#103 と同じ boundary 方針)であり、`already_applied` を照会なしで確定することはない。
3. **revision gate**: discrepancy は観測時点の注文 **revision**(execution store の履歴行数。same-state 更新でも必ず増える)を保持しており、現在 revision と一致しない = **観測後に何らかの変更があった**。UNKNOWN → UNKNOWN の履歴追加・snapshot refresh・部分約定の増加・timestamp のみの更新も state 文字列が同じまま検出される。変化していた場合: 既に観測どおりの Broker 状態へ到達済みで、かつ **3-way 検証**(保存済み discrepancy × 現在行 / anchored snapshot × fresh snapshot が state・client/broker ID・数量・価格・fills・reject reason の全フィールドで一致。fresh の timestamp 逆行は不可)を通ったときのみ `already_applied`(no-op)。それ以外は `stale` / `conflict`(古い観測で新しい状態を巻き戻さない)。revision を持たない v1 時代の観測は常に `stale`(新しい照合を実行する)。
4. fresh snapshot が保存済み観測と一致すること。状態が異なる(UNKNOWN へ戻った場合を含む)・broker 参照 ID が変わった場合は `conflict`(新しい reconciliation run での再分類が必要)。
5. **既存状態機械・store 契約の再検証**: intent identity 一致、`validate_order_state_transition`、anchored snapshot に対する `validate_order_result_update`、時刻の非逆行。拒否は `rejected`(遷移表は緩和しない)。
6. **Broker 参照 ID を失わない**: 既知の `broker_order_id` の NULL 化・別値への置換は `conflict`。fresh snapshot の ID が別 Execution Request に所有されている場合(DB 全体 UNIQUE)も `conflict`。判定に必要な store 読み取りの失敗は `failed`(fail-closed。修復可能性を推測しない)。

### 適用・冪等性・並行実行(post-commit 安全性)

- 適用は `execution_store.record_order_result(..., expected_current_state=..., expected_revision=観測 revision, repair_attempt_id=..., repair_generation=...)` の 1 呼び出しのみ。store は `BEGIN IMMEDIATE` トランザクション内で **4 つ**を検証する: **expected `repair_attempt_id`** / **expected `generation`**(fence 行との一致)/ **expected order revision** / **expected current state**。いずれかが不一致なら全 rollback。
- **takeover 後の旧 worker は、状態も revision もまだ同じであっても commit できない**(fence が generation 2 を指しているため)。この拒否は typed な `ExecutionRepairFenceConflictError` で、呼び出し側は**決して `applied` にせず**、`conflict` + `failure_code = repair_fence_conflict` / `claim_lost` として結果化する。
- CAS に敗れた worker は `ExecutionOrderStateConflictError` を受けて再読取し、3-way 検証つきで `already_applied` / `stale` / `conflict` として結果化する。
- **APPLIED attempt は commit 前に完全構築・検証される**: `broker_state_updated_at_utc` は Broker snapshot の `updated_at_utc`、broker 参照 ID は store と同じ導出式、監査 detail も事前確定。**commit 後は一切の計算をしない**(store の戻り値も clock も参照しない)ため、commit 後の例外が「実際には適用済みなのに `failed`」を生む経路は存在しない。`failed` は **execution order DB の注文状態が無変更であることが store の rollback で証明されている場合のみ**使われる。
- 同一 repair を複数回・複数 worker で実行しても、**状態履歴は高々 1 行しか増えず**(history 側も `repair_attempt_id` の partial UNIQUE で 1 attempt = 高々 1 行を DB レベルで強制。generation を跨いでも「1 attempt = 高々 1 回の適用」は変わらないので UNIQUE の単位は attempt ID のままでよい)、最終状態は Broker が確認した状態のまま壊れない(`already_applied` は成功と区別される明示的な no-op)。current row も同じ transaction で 1 回だけ動く。

### lease duration と Broker timeout の設定契約

lease は「1 attempt の処理時間より十分長い固定値」を採る(heartbeat / lease renewal は実装しない — Issue #104 の規模に対して回収不能な複雑さを持ち込まないため)。既定は `DEFAULT_LEASE_DURATION_SECONDS = 600` 秒。

将来 Adapter がこの契約を破らないよう、**コンストラクタが `broker_timeout < lease_duration` を検証する**: 明示引数 `broker_timeout_seconds` と、broker が公開する `timeout_seconds` 属性の**両方**を見て、どちらかが lease 以上なら `ReconciliationRepairError` で構築を拒否する(Broker 照会中に lease が切れて自分の claim を奪われる構成を、実行前に落とす)。

### `RepairFailureCode`(closed set)

`failure_code` は自由文字列ではなく `RepairFailureCode` Enum(`reconciliation_repair_models.py`)であり、**model validation / store serialization / store restoration / SQLite CHECK / result API / tests / docs が同じ closed set を共有**する。SQLite の CHECK 句は値一覧を手書きせず **Enum から生成**するため、二重管理が起きない。

- 改竄などで **unknown な failure code を持つ行は読み取り時に fail-closed** で拒否される(`ReconciliationIntegrityError`)。
- **outcome × failure_code の組合せも検証**する: `applied` / `already_applied` / `would_apply` は failure code なし、`skipped` は不適格理由コード、`query_failed` は query 系コード必須、`stale` / `conflict` / `rejected` / `failed` はそれぞれの許可集合から必須。集合は `_ALLOWED_FAILURE_CODES` に 1 箇所だけ定義する。
- claim / fence 由来のコードもこの集合に含まれる: `claim_lost`(takeover により claim を失った)/ `repair_fence_conflict`(fence が別 generation を指していた)。

### run-level integrity failure と部分結果

運用上想定される typed 例外(`BrokerError` / `ExecutionStoreError` / `ReconciliationStoreError` / store の refusal channel / CAS conflict)だけが attempt outcome に変換される。**プログラマエラー相当**(`AssertionError` / `KeyError` / 想定外 `TypeError` / モデル不変条件違反 / clock 契約違反)は catch-all で `failed` に変換せず、pass を `ReconciliationRepairRunError` で停止する。このとき **完了済み attempt はエラーオブジェクトに添付され、監査 DB からも取得できる**(commit 済み結果は決して失われない)。処理途中の対象は pending のまま残り recovery で回収される。pass 終了時の clock 失敗・result 構築失敗も同じ扱いで、commit 済み attempt を隠さない。

監査 detail は**サニタイズ**される: 安定 `failure_code` + 自前の説明文(登場する例外クラス名は自 typed クラスのみ)であり、生の例外文字列・パス・SQL・adapter 自由文は監査 DB へ入れない。

### logging の位置づけ(sanitize 契約)

**監査 DB が正であり、logging は補助**。すべてのログ呼び出しは隔離されており、handler / formatter / encoding の失敗が修復の outcome を変えたり、commit 済み attempt を失わせたり、次の attempt の処理を止めたりすることはない(テストで handler 故障を注入して固定)。

Broker adapter 境界から漏れた非 `BrokerError` の例外は、**文字列も repr も traceback もログへ出さない**。ログに出るのは次の 5 つだけである。

```text
stable failure code
exception class name
attempt ID
run ID
target ID
```

実装上は、例外を扱うすべての log 呼び出しが `_safe_log_sanitized_exception()` を通り、`str(exc)` / `repr(exc)` / `exc_info` / `logger.exception()` をどこでも使わない(「詳細は typed な監査行を見よ」とだけ書く)。この結果、token / API key / password / authorization header / account ID / endpoint URL の query / raw HTTP response / SQL / DB パス / Windows パス / Unix パス、それらの大文字小文字違い・URL encode 済み・複数回登場・`raise ... from ...` の cause / context chain のいずれも、**監査 DB・通常ログ・fallback ログのすべてに現れない**(実際の secret 文字列を注入するテストで固定)。

### timestamp 契約(Broker 時刻と内部時刻の分離)

| timestamp | 意味 | clock |
|---|---|---|
| pass `started_at_utc` / `finished_at_utc` | repair pass の開始・終了 | repair 側 clock |
| `reserved_at_utc` | attempt の claim 取得時刻(その attempt のどの副作用よりも前に取得) | repair 側 clock |
| `last_claimed_at_utc` / `lease_expires_at_utc` | 現 claim の取得時刻とその lease 期限(takeover ごとに更新) | repair 側 clock |
| `observed_at_utc` | reconciliation 観測時刻(#103 の記録の複写) | detection 側 clock |
| `broker_state_updated_at_utc` | **Broker が報告した状態の時刻**(= snapshot の `updated_at_utc`。state history 行の `changed_at_utc` と同じ値。commit 前に確定) | **Broker clock 域** |
| `finalized_at_utc` | 監査行を finalize した時刻 | repair 側 clock |

**`broker_state_updated_at_utc` は「BELIAL が commit した時刻」ではない**。旧名 `applied_at_utc` は Broker clock 域の値を内部適用時刻に見せていたため改名した。ドキュメント・ログ・detail のいずれでもこの値を「commit 時刻」と呼ばない。

内部の commit 時刻を厳密に取るには commit 後に clock を呼ぶ必要があり、それは「commit 済みなのに `failed`」を再導入するため**採らない**(前回 Blocker の再発防止)。代わりに監査上必要な事実を `broker_state_updated_at_utc`(Broker 時刻)/ `reserved_at_utc`(適用開始側の時刻)/ `finalized_at_utc`(監査確定時刻)の 3 点で表現する。

clock の呼び出しは **pass 開始 / attempt ごとの claim / attempt ごとの finalize / pass 終了**の 4 点のみ。finalize の clock は finalize の `try` の中で呼ぶため、失敗しても outcome は変わらず `audit_error` が付き pending として回収可能に留まる。Broker clock 域の値は repair 側 clock と順序付けない(明示契約)。

### 監査記録と DB 障害時の整合性

- すべての attempt(applied / already_applied / skipped / stale / conflict / rejected / query_failed / failed)は claim(reservation)→ finalize の 2 段階で `broker_reconciliation_repair_attempts` に記録される(観測 = run / discrepancy 行は書き換えない。「修復済みフラグ」は存在しない)。
- **finalize は fencing された CAS**である: `attempt_id` + `owner_id` + `claim_execution_id` + `generation` + `status = 'pending'` がすべて一致した行だけを更新する。takeover 後に復活した旧 generation の worker は `RepairClaimLostError` を受け、**audit DB を `failed` で上書きできない**(APPLIED という事実と矛盾する outcome を保存しない)。旧 worker は `audit_error` 付きの結果を返すだけで、行の確定は新 generation の recovery が history provenance を見て `applied` として行う。
- 状態更新(execution orders DB)と repair 監査(reconciliation DB)は別ファイルであり単一トランザクションにできない。分散トランザクション風の実装は行わず、**claim(監査)→ fenced commit(orders)→ finalize(監査)** という outbox 型の明示リカバリ設計とする。全 crash 窓が回収可能:
  - claim 前 = 痕跡なし・無変更。
  - claim 後〜commit 前 = pending が残り、lease 期限後に recovery が takeover して**評価をやり直す**(Broker 再照会 + eligibility 再検証)。
  - **takeover の途中(fence 更新後〜claim CAS 前)** = 孤立 fence が残るが、次の recovery が adopt して同じ generation で継続する(pending が回収不能になる窓は無い)。
  - commit 後〜finalize 前 = pending + history の構造化 provenance(`repair_attempt_id` / `repair_generation`)が残り、recovery が**元 attempt ID のまま `applied` 確定**(元の attempt ID・outcome・Broker 時刻は失われない)。
- finalize 失敗時は attempt に `audit_error` を付けて返す + ERROR ログ(`ReconciliationRepairResult.has_audit_failures` で機械検出)。中途半端な内部状態は残らない(store 側は全 rollback)。
- 更新途中の DB 障害は store のトランザクションが全 rollback し、attempt は `failed`(= 注文状態が無変更だと確定した場合のみの分類)として finalize される。

### 照合のみモード(dry-run)

`mode=RepairMode.DRY_RUN` は上記の評価(store 読み取り + Broker 照会)をすべて実行するが、**一切書き込まない**: 注文更新なし・履歴追加なし・**claim / lease / generation なし**・**fence 行なし**・監査行なし・DB 作成 / migration なし(v1 監査 DB は migration せず明示的に拒否)・recovery を起動しない・Broker write 呼び出しなし・state file / ファイルシステム副作用なし(**両 DB** のバイト列・mtime・ディレクトリエントリ・journal / WAL / SHM・`user_version` の不変をテストで固定)。dry-run の attempt は `owner_id` / `claim_execution_id` / `generation` を**持たない**(モデル不変条件で強制)。適用可能な対象は `would_apply` として返る(`applied` とは区別され、永続化もされない)。破損 DB は無変更で拒否する。

## 永続化(`reconciliation_store.py`)

独立 DB `execution/data/broker_reconciliation.sqlite3`(既定。`BROKER_RECONCILIATION_DB_PATH` で上書き。相対パスは `execution/` 基準)。所有コンポーネントは `execution/` のみ。他 store と同居しない理由は既存 store と同じ(各 store が closed-world で schema を検証するため)。加えて、reconciliation 専用 DB に**注文テーブルが存在しない**こと自体が「この store が注文状態を書き換えられない」構造的保証になる。repair 監査行(Issue #104)を第 5 の DB に分けず**この同一 DB に置く**のは、観測と修復が同じ broker-reconciliation コンポーネントの 2 つの監査系列であり、attempt 行から run 行への実 FK を張れるため。

### schema(version 2)

| テーブル | 列 |
|---|---|
| `broker_reconciliation_runs`(WITHOUT ROWID) | `run_id`(PK)/ `mode` / `started_at_utc` / `finished_at_utc` / `checked_order_count` / `broker_listing_error` |
| `broker_reconciliation_discrepancies` | `discrepancy_id`(PK AUTOINCREMENT)/ `run_id`(FK → runs)/ `discrepancy_type` / `target_type` / `target_id` / `resolution_category` / `internal_state` / `broker_state` / `client_order_id` / `broker_order_id` / `broker_error_code` / `detail` / `observed_at_utc` / `internal_revision`(v2 / Issue #104: 観測時点の注文 revision。v1 由来の行は NULL = 自動修復対象外)。同一 run の `(target_type, target_id)` は UNIQUE |
| `broker_reconciliation_repair_attempts`(WITHOUT ROWID、Issue #104 / v2) | `attempt_id`(PK)/ `run_id`(FK → runs)/ `mode` / **`status`(pending / finalized)** / `target_type` / `target_id`(**複合 FK → discrepancies の UNIQUE key** = attempt は必ず実在する観測へ紐づく)/ **claim: `owner_id` / `claim_execution_id` / `generation`(≥ 1)/ `lease_expires_at_utc` / `last_claimed_at_utc`** / `execution_request_id` / `client_order_id` / `reconciliation_internal_state` / `reconciliation_broker_state` / `internal_revision` / `internal_state_before` / `internal_state_after` / `outcome`(pending 中は NULL)/ `broker_order_id` / `broker_error_code` / `failure_code` / `detail` / `observed_at_utc` / `reserved_at_utc` / **`broker_state_updated_at_utc`**(旧 `applied_at_utc`)/ **`finalized_at_utc`** |

index: `idx_broker_reconciliation_discrepancies_run (run_id, discrepancy_id)`、`idx_broker_reconciliation_repair_attempts_run (run_id, status, reserved_at_utc, attempt_id)`(pending 列挙 = recovery の work list がこの index で引ける)。CHECK 制約がモデル不変条件を二重化する(既知 enum 値のみ、UTC 接尾辞、非負カウント / revision、`query_failure` ⇔ `query_failed` ⇔ `broker_error_code IS NOT NULL`、attempt 側は `mode = 'apply'` のみ・`would_apply` 非永続・`(status='pending') ⇔ (outcome IS NULL)`・pending 行は outcome 系列 NULL・finalized 行は `applied` ⇔ `broker_state_updated_at_utc`・`applied`/`already_applied` ⇔ `internal_state_after` ⇔ `failure_code IS NULL`・`query_failed` ⇔ `broker_error_code`)。

claim 列の CHECK: `generation >= 1`、`owner_id` / `claim_execution_id` / `lease_expires_at_utc` / `last_claimed_at_utc` は **NOT NULL**(APPLY モードの行しか存在しないため、pending / finalized を問わず claim は必須 = 「どの実行がこの結果を確定させたか」が監査に残る)、UTC 接尾辞必須、`(status='finalized') ⇔ (finalized_at_utc IS NOT NULL)`、`failure_code` は **`RepairFailureCode` から生成した値一覧**の `IN (...)`(手書きの重複なし)。malformed な lease / generation・非 UTC・unknown failure code は書き込み時に CHECK で、読み取り時にモデル不変条件で **fail-closed** に落ちる。

**claim 列の保持方針**: finalize 後も claim 列は保持する(消さない)。どの owner / claim execution / generation が確定させたかは監査事実そのものであり、finalized 行はもう takeover 対象にならない(CAS が `status='pending'` を要求する)ため保持による危険はない。

### 重複方針(明示)

**run 単位の append**。1 run = runs 行 1 件 + discrepancies 行 N 件。**同一内容の観測でも run ごとに独立した行として残す**(dedup・上書き・UPSERT はしない)。これは既存の監査設計(`safety_gate_decisions` は判定を毎回 append、`execution_order_state_history` は観測を毎回 append)と同じ立場であり、「いつ何を観測したか」を再構成可能に保つためである。

一方、**同一 run 内の同一 target は最大1件**であり、モデル不変条件と DB の `UNIQUE (run_id, target_type, target_id)` の両方で固定する。Broker 列挙が client/broker ID を重複して返した場合は broker-only 差分を二重記録せず、列挙全体を曖昧として `broker_listing_error` に残す。

`run_id` は呼び出し側が与える PRIMARY KEY。**既存 run_id での再記録は `ReconciliationRunConflictError` で拒否**し、保存済みの run を書き換えない。したがって同じ照合を何度繰り返しても、内部注文状態も既存の記録も壊れない。

repair attempt(Issue #104)も同じ append の立場で、**2 段階ライフサイクル(pending claim → finalized)**を持つ: すべての試行(適用しなかったものを含む)が独立した監査イベントであり、repair の再実行は新しい attempt 行を claim → finalize するだけで、既存の run / discrepancy / attempt 行を更新しない。既存 `attempt_id` の再利用は **reservation 時点で** `RepairAttemptConflictError`(状態更新の前に拒否される)。

pending 行だけが例外的に **claim 列のみ** in-place 更新される(`claim_repair_attempt()` の takeover CAS = `generation + 1`)。observation / identity / outcome 系列は決して書き換わらない。finalize は **claim 込みの CAS 保護**(pending のみ・ちょうど 1 回・現 claim のみ。二重 finalize は `RepairAttemptConflictError`、claim 不一致は `RepairClaimLostError`、identity 不一致は `ReconciliationIntegrityError`)。pending のまま残った行は crash の証跡として `list_pending_repair_attempts()` で列挙でき、**lease 期限切れのものだけ**が recovery の回収対象になる。dry-run は claim を含め**記録自体を拒否**する(無副作用契約)。

### migration / 初期化

`init_reconciliation_db()` は**冪等**で、pristine な DB(`sqlite_master` 0 件かつ `user_version` 0)にのみ schema を作成し、現行 schema の既存 DB は closed-world 検証のみを行う(既存行は無変更)。**v1 → v2 migration(Issue #104)**: **リリース済み v1 と完全一致した DB のみ**を単一トランザクションで additive に migration する(`ALTER TABLE discrepancies ADD COLUMN internal_revision` — SQLite は列をテーブル制約の前・列リスト末尾へ挿入するため migration 後の DDL は v2 参照 CREATE と token 正規化で一致 — → repair テーブル + index 作成 → version bump → v2 完全再検証 → COMMIT。途中失敗は全 rollback で v1 ファイル無変更。既存 discrepancy 行は `internal_revision = NULL` で読み戻され自動修復対象外)。**読み取り経路は migration しない**(v1 DB は `init_reconciliation_db()` を指すエラーで拒否 = dry-run / read-only の無副作用契約を維持)。それ以外(partial schema・制約欠損 lookalike・trigger / view・他コンポーネントの DB)は **DB を変更せず**明示的に失敗する。読み取りは true read-only 接続(`execution/sqlite_ro.py`)で行い、存在しない DB を作成しない。復元時はモデル不変条件で厳格に再検証する(帯域外改竄・malformed pending 行は `ReconciliationIntegrityError` で fail-closed)。

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
    owner_id="repair-worker-1",       # 省略時はインスタンスごとのランダム ID
    lease_duration_seconds=600,       # claim lease(既定 600 秒)
    broker_timeout_seconds=30,        # 省略可。lease 未満であることを構築時に検証
)
result = repairer.repair(run_id="recon-...")                            # claim → fenced 適用 → finalize
result = repairer.repair(run_id="recon-...", mode=RepairMode.DRY_RUN)   # 評価のみ(無副作用)
recovered = repairer.recover_pending_attempts()                         # lease 切れ pending の takeover
```

`detect()` は reconciliation DB を作成しない(dry-run 的な確認に使える)。`run()` は `init_reconciliation_db()`(冪等)の後に append する。`repair()` は**保存済み run のみ**を入力とし、APPLY モードでは既存 DB を検証(必要なら v1 → v2 migration)してから処理する(DB が存在しなければエラー。新規作成はしない)。run-level 停止時は `ReconciliationRepairRunError.attempts` に完了済み attempt が添付される(監査 DB からも取得可能)。`recover_pending_attempts()` は **lease が切れた pending だけ**を対象にするので、live な repair pass と同時に走らせても安全である(何度呼んでも、回収済み = finalized の attempt には二度と触れない)。

## 非スコープ(Issue #104 時点)

position / trade / transaction / SL・TP の照合、OANDA Demo Adapter を使った手動照合(Issue #105)、Broker への注文送信・キャンセル・replace・close(修復は内部 DB のみ)、Broker 側のみの注文の自動採用、Broker に存在しない内部 position の自動 close、SL / TP 注文の再作成、Broker 状態の推測、無条件の自動決済、scheduler / 常駐 Bot への組み込み、Live 接続解禁。

claim まわりの非スコープ: **lease の heartbeat / renewal**(固定 lease + `broker_timeout < lease_duration` の構築時検証で代替)、hung worker の強制中断(lease 期限切れによる takeover のみ)、worker 間の分散ロックサービス、fence 行の GC(新しい attempt が来れば置換されるため蓄積しない)。

## テスト

`execution/tests/test_reconciliation_models.py` / `test_reconciliation_repair_models.py` / `test_reconciliation_store.py` / `test_broker_reconciliation.py` / `test_broker_reconciliation_repair.py`、および `broker/tests/test_fake_broker.py` / `test_broker_interface.py`。観点は [テスト仕様](../testing/test_specification.md) を参照。すべて `tmp_path` の SQLite と in-memory FakeBroker のみで、外部通信・Discord・実 DB 更新は発生しない。
