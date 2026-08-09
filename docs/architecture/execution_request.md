# Execution Request / Order Persistence(Issue #74)

将来の **Live Execution** に向けた、Strategy / Signal から Execution ユースケースへ渡す共通モデル **ExecutionRequest** と、注文状態・状態履歴を SQLite へ永続化する基盤の仕様。実装は `execution/`(明示的な Python package。`__init__.py` あり)にあり、リポジトリルートから通常の package import で利用する。

```python
from execution.execution_models import ExecutionRequest
from execution import execution_store
```

> **重要な位置づけ**
>
> - 本コンポーネントは **将来の Live Execution 用のモデル・状態永続化基盤のみ** を実装する。
> - **Live Execution 本体・Broker API Adapter・実発注・reconciliation runner は未実装**である。このコンポーネントが存在しても Live Execution が実装済みになったわけではない。発注前の Safety Gate は Issue #75 で `execution/safety_gate.py` に実装済み([Safety Gate](safety_gate.md))だが、これも検証部品のみで実発注経路は存在しない。
> - 外部通信・Discord・S3・実注文はこのパッケージのどこにも存在しない。
> - **Paper Signal / Paper Execution は従来どおり Broker 非依存**であり、本パッケージを import しない・させない(`execution/tests/test_package_imports.py` が回帰として固定)。
> - 注文状態の唯一の正は既存の `broker.broker_models.OrderState` / `ORDER_STATE_TRANSITIONS` / `validate_order_state_transition()` / `validate_order_result_update()`(Issue #73 / PR #87)である。**本パッケージは注文状態 Enum・遷移表を一切新設しない。**

## 依存方向

```text
Strategy / Paper Signal(語彙のマッピングは呼び出し側の責務)
    ↓
ExecutionRequest                ← execution/execution_models.py(BELIAL 内部の発注意図)
    ↓ to_order_request()
broker.OrderRequest             ← broker/broker_models.py(Broker 境界へ渡す要求)
    ↓
(将来の) Broker Adapter          ← 未実装

execution_store                 ← execution/execution_store.py(SQLite 永続化)
    → broker.OrderState / validate_* を検証に使用(状態機械の正は broker 側)
```

依存は `execution → broker` の一方向のみ。broker パッケージは execution を知らない。

## ExecutionRequest と broker.OrderRequest の違い

| | `ExecutionRequest`(execution/) | `broker.OrderRequest`(broker/) |
|---|---|---|
| 意味 | BELIAL 内部で「何を発注しようとしたか」を表す**永続化可能な発注意図** | Broker 境界へ実際に渡す **Broker 非依存リクエスト** |
| 由来情報 | `signal_id` / `strategy_id`(provenance)を保持 | 持たない |
| SL / TP | `stop_loss_price` / `take_profit_price` を保持(BELIAL 内部情報) | 持たない(Issue #73 の注文モデルは素の market / limit のみ) |
| キー | `execution_request_id`(内部の意図 ID) | `client_order_id`(Broker 境界の呼び出し側キー) |
| 永続化 | execution store が保存・監査する | しない(境界の値オブジェクト) |

変換責務は **ExecutionRequest 側**にある: `ExecutionRequest.to_order_request(client_order_id=..., created_at_utc=...)` が唯一のサポートされた変換経路であり、SL / TP と provenance は Broker 境界を越えない。`requested_price` は LIMIT のとき `limit_price` へ写像される。

## ID 設計(execution_request_id と client_order_id)

意味の異なる 2 つの ID を**別フィールド**として保持し、混在させない。

- `execution_request_id` — BELIAL 内部の発注**意図**の ID。execution store の主キーで、状態履歴のキー。
- `client_order_id` — Broker 境界(submit / cancel / query, Issue #73)の呼び出し側キー。この意図に対する Broker 側注文 1 件を指す。

store は両者を **1:1 で固定**する(`client_order_id` は NOT NULL + UNIQUE、保存後は不変)。UNKNOWN(結果不明)後の照会は**保存済みの `client_order_id`** で行う。同じ意図を**新しい `client_order_id` で再送することが二重発注の正体**であるため、モデル・store は client order ID を暗黙に生成・更新・ローテーションしない。再送(意図の再発行)は新しい `execution_request_id` + 新しい `client_order_id` を持つ別レコードとして UNKNOWN の解決と区別されるが、**Issue #76 以降は同一 source signal からの新レコード作成自体が `signal_id` の UNIQUE 制約で拒否される**: 暗黙の再発行経路は存在せず、将来再発行フローを作る場合(非スコープ)は明示的な運用判断・migration を伴う設計にしなければならない。

## ExecutionRequest(`execution_models.py`)

`dataclass(frozen=True)` + `Decimal`(価格・数量)+ timezone-aware UTC `datetime`。検証違反は `ValueError`(BELIAL 内部モデルのため、リポジトリ既存のモデル検証様式に従う。Broker 境界の `BrokerInvalidRequestError` は使わない)。

| フィールド | 型 | 検証 |
|---|---|---|
| `execution_request_id` / `signal_id` / `strategy_id` / `symbol` | `str` | 非空 |
| `side` | `broker.OrderSide` | enum メンバー必須(direction は broker と同一語彙。long/short 等 Signal 側語彙の変換は呼び出し側の責務) |
| `order_type` | `broker.OrderType` | enum メンバー必須 |
| `quantity` | `Decimal` | 正の有限値 |
| `requested_price` | `Decimal \| None` | LIMIT は必須、MARKET は禁止(`OrderRequest.limit_price` と同じ契約)。指定時は正の有限値 |
| `stop_loss_price` / `take_profit_price` | `Decimal \| None` | 指定時は正の有限値 + 下記の方向整合 |
| `requested_at_utc` | `datetime` | timezone-aware UTC。naive は拒否 |

**SL / TP と direction の検証は「現段階で一意に保証できる関係」のみ**:

- SL と TP の両方が指定された場合: BUY は `SL < TP`、SELL は `TP < SL`(fill 価格に依存しない不変関係)。
- `requested_price` が既知(= LIMIT)の場合: BUY は `SL < requested_price` かつ `TP > requested_price`、SELL はその鏡像。
- **意図的に検証しないもの**: MARKET で SL / TP の片方だけが指定されたケース(参照価格が存在しない)、SL / TP の最小距離・リスクリワード等の業務ルール。曖昧なルールを勝手に追加しない。

## 注文状態の永続化(`execution_store.py`)

独立 DB `execution/data/execution_orders.sqlite3`(既定。`EXECUTION_ORDER_DB_PATH` で上書き)。所有コンポーネントは `execution/` のみで、他コンポーネントの DB には触れない。テストは `tmp_path` 配下の SQLite のみを使う。

### CREATED / SUBMITTING の設計(broker.OrderState と混同しない)

Issue #74 の選択肢のうち **「CREATED / SUBMITTING を注文状態ではなく Execution Request 側の送信フェーズとして別フィールドで管理する」** 方式を採用した。既存 `broker.OrderState` には**何も追加しない**。

`execution_orders.submission_phase`(BELIAL 内部の送信ライフサイクル):

| phase | 意味 | `order_state` 列 |
|---|---|---|
| `created` | 意図を永続化済み。送信試行なし | NULL(Broker 注文は存在しない。CHECK 制約で強制) |
| `submitting` | 送信試行が開始された | 既存 `OrderState.PENDING`(「送信済み・Broker ACK なし」という broker の既存語彙そのまま)から追跡開始 |
| `submitted` | Broker が確定的に応答した観測(accepted / partially_filled / filled / cancelled / rejected)が 1 件以上存在する | broker 観測値 |

- `submitted` は導出フェーズであり、**後退しない**。`PENDING` / `UNKNOWN` の観測は Broker 側の保持を証明しないため phase を進めない(UNKNOWN で `submitting` に留まる)。
- `mark_submitting()` は **created から一度だけ**入れる。送信結果を失った場合は UNKNOWN として記録し照会で解決する(送信フェーズへ静かに再入させない)。

### テーブル

`execution_orders`(current 状態):

- `execution_request_id` TEXT PRIMARY KEY
- request 本体: `signal_id` / `strategy_id` / `symbol` / `side` / `order_type` / `quantity` / `requested_price` / `stop_loss_price` / `take_profit_price` / `requested_at_utc`(Decimal は正準 TEXT で保存し float 誤差を持ち込まない)
- `signal_id` は**専用 UNIQUE index(`idx_execution_orders_signal`)でテーブル全体一意**(Issue #76): 1 つの source signal からは高々 1 件の Execution Request しか作成できない。inline 列制約でなく独立 index にしたのは **v2 → v3 migration を purely additive にするため**(テーブル DDL は v2 / v3 で同一)
- `client_order_id` TEXT NOT NULL UNIQUE(1:1・不変)
- `submission_phase` TEXT NOT NULL(CHECK: 上記 3 値)
- `order_state` TEXT NULL(CHECK: `OrderState` の値集合。`created` ⇔ NULL を CHECK で強制)
- `broker_order_id` TEXT NULL **UNIQUE(テーブル全体で一意。PR #91 レビュー Major 3)**: 1 つの Broker 注文が複数の execution request に関連付くことを DB レベルで拒否する。**Order Reissue(Issue #102)後は常に「現在操作対象(current actionable)の ID」**を保持し、過去 ID は history 行と snapshot の `superseded_broker_order_ids` chain に監査可能なまま残る(一意性は復元時 scan が過去 chain ID まで含めて検証する。下記)
- `last_state_reason` TEXT NULL
- `last_order_result_json` TEXT NULL(**最新の完全な `OrderResult` snapshot**。Decimal は正準文字列・日時は UTC ISO・fills 含めて損失なく・キーソート済みで JSON 化)
- `created_at_utc` / `updated_at_utc` TEXT NOT NULL

`execution_request_id` は **`NOT NULL` を明示**し(rowid テーブルの TEXT PRIMARY KEY は NULL を許すため)、orders テーブルは **`WITHOUT ROWID`**(PR #91 レビュー r2 Major 2)。アプリケーション層の検証に加え、主要な不変条件(ID / reason の非空、UTC サフィックス、MARKET / LIMIT × `requested_price`、state × `broker_order_id`、phase × `order_state`)は **DB CHECK 制約でも二重に強制**する。

**snapshot の history anchor(PR #91 レビュー r2 Major 1)**: 完全な `OrderResult` snapshot は current 行だけでなく、**その観測を記録した history 行の `order_result_json` にも同一トランザクションで保存**される。history 側が anchor であり、復元・更新のたびに以下を検証する。

- current 行の snapshot は「**最後に snapshot を保存した history 行**」と**バイト一致**(canonical JSON)。**追跡状態が UNKNOWN の間も例外にしない**(state-only UNKNOWN は snapshot に触れないため、一致が維持されるはずである)。current 行の snapshot だけを古いものへ差し替える改竄は必ず検出される。
- **連続する snapshot 同士は `validate_order_result_update()` で pairwise 検証**される(履歴上でも fill・数量・ID・時刻の巻き戻しは存在できない)。
- 各 snapshot はそれ自身の history 行(state / `broker_order_id` は **NULL 含む対称比較** / 時刻)および保存済み intent と一致しなければならない。
- `broker_order_id` は **history 全行にわたる chain** としても検証される: NULL → 値は一度きり、以後の**値の変更は監査された Order Reissue の場合のみ**(Issue #102: 変更する行は完全 snapshot を持ち、その snapshot の `superseded_broker_order_ids` が直前の ID を記録していること。正確な chain-append 規則は pairwise の `validate_order_result_update()` が強制する。state-only 行は ID を変更できない)。
- snapshot を持たない history 行は「最初の created → pending 行」と「state-only UNKNOWN 行」だけに限られる(確定観測が snapshot を欠くことは破損)。

`execution_order_state_history`(状態履歴。変更理由の追跡):

- `history_id` INTEGER PRIMARY KEY AUTOINCREMENT
- `execution_request_id` TEXT NOT NULL(FK → `execution_orders`。`PRAGMA foreign_keys=ON`)
- `previous_state` TEXT NULL(最初の履歴行 = created → pending は NULL)
- `new_state` TEXT NOT NULL
- `reason` TEXT NOT NULL
- `broker_order_id` TEXT NULL(変更時点の値)
- `order_result_json` TEXT NULL(**snapshot anchor**。この観測を生んだ完全 `OrderResult`。state-only UNKNOWN と最初の pending 行のみ NULL)
- `changed_at_utc` TEXT NOT NULL
- `repair_attempt_id` TEXT NULL(schema v4 / Issue #104: **構造化 repair provenance**。reconciliation repair が適用した観測にのみ設定され、partial UNIQUE index `idx_execution_order_state_history_repair_attempt` により 1 attempt = 高々 1 行。provenance 付き行は snapshot 必須(復元時検証)。crash recovery はこの列で適用済み事実を判定する — reason 文字列は人間向け補助であり解析しない)
- `repair_generation` INTEGER NULL(schema v4 / Issue #104: その適用を commit した **claim generation**(≥ 1)。`repair_attempt_id` と**必ず同時に設定 / 同時に NULL**。UNIQUE は attempt ID 単位のままでよい — 同じ attempt は generation を跨いでも高々 1 回しか適用されないため、旧 generation が commit 済みなら新 generation は再 commit せず provenance を読んで `applied` を確定する)

`execution_repair_fences`(WITHOUT ROWID。schema v4 / Issue #104: **repair fencing mirror**):

- `execution_request_id` TEXT PRIMARY KEY(FK → `execution_orders`)— 注文ごとに高々 1 行
- `repair_attempt_id` TEXT NOT NULL / `generation` INTEGER NOT NULL CHECK(`>= 1`)
- `takeover_id` TEXT NULL(generation ≥ 2 でのみ NOT NULL。`CHECK ((generation = 1) = (takeover_id IS NULL))`)
- `owner_id` TEXT NOT NULL / `claim_execution_id` TEXT NOT NULL(**どの実行**が現在この注文の修復を所有しているか)
- `updated_at_utc` TEXT NOT NULL(UTC)

reconciliation DB の claim を `record_order_result()` のトランザクション内から読むことは(SQLite ファイルを跨げないため)できない。そこで **Execution DB 側に claim の mirror を置き、状態更新と同じトランザクションで照合する**(案 A)。責務は「現在どの (attempt, generation) が更新を許可されているか」だけで、注文行や通常の submission 経路には一切影響しない。

fencing token は `RepairFenceToken`(attempt id / generation / takeover id / owner / claim execution id)という**1 つの identity オブジェクト**として扱う。reconciliation DB の durable takeover intent がこれを生成し、fence 登録と状態更新の両方が同じオブジェクトを提示する。

`register_repair_fence()` は成功時に **commit した fence 行を読み戻した** `InstalledRepairFence`(order ID + token + 更新時刻)を返す。呼び出し側の引数をそのまま echo するのではなく**この store が実際に保持している値**なので、生成された瞬間においては真である。ただしこれは **観測値であって権限ではない**: reconciliation 側へ「fence を設置した証明」として持ち回すことはしない(r6 レビュー)。in-memory のオブジェクトを持てる者はそれを捏造することもできるからである。

#### promotion guard(claim 昇格中は fence を止める。再レビュー r7)

`repair_fence_promotion_guard(execution_request_id, expected=RepairFenceToken)` は、この store に `BEGIN IMMEDIATE`(RESERVED lock)を張ったまま **commit 済み fence 行を lock 下で read し、6 項目 identity を照合してから** 呼び出し側の critical section を実行する context manager である。reconciliation 側の claim CAS をこの中で commit することで、「fence を確認してから claim を昇格するまでの間に別 attempt が fence を置換する」窓(r7 Blocker)を無くす。

性質:

- **write 排他 / read 非排他**: guard は `PRAGMA query_only` を立てるので自分では書けず、RESERVED を EXCLUSIVE へ昇格させない。したがって他 connection の read は一切ブロックしない一方、`register_repair_fence()` / `record_order_result()` などの writer は guard 解放まで進めない。
- **process 間で有効**: SQLite のファイルロックなので、thread だけでなく別プロセスにも効く。
- **lock leak なし**: プロセス死で OS がロックを解放する。耐久 lock 行を持たないので掃除も migration も不要。
- **typed timeout**: 取得できなければ `ExecutionRepairFenceBusyError`(`ExecutionStoreBusyError` のサブクラス)。無限待機はしない。通常の write 経路も同様に、lock 競合は生の `sqlite3.OperationalError` ではなく `ExecutionStoreBusyError` になる。
- **fail-closed**: fence 行なし / store なしは `ExecutionRepairFenceMissingError`、identity 不一致は差分列名を持つ `ExecutionRepairFenceConflictError`。どちらも critical section に入らない。

lock order は **この store の guard が先、reconciliation DB の transaction が後**で固定されている。

reconciliation 側が `fence_installed` を記録するときは、`mark_takeover_fence_installed(execution_db_path=...)` が **この store の `get_repair_fence()` を自分で呼び、commit 済み fence 行を read して** intent の identity 6 項目と突き合わせる。行がない・identity が違う・store が読めない場合は遷移せず typed 例外になる。この read も上記 guard の中で行われるため、確認と reconciliation 側の書き込みの間に fence が動くことはない。したがって fence に関する信頼の起点は、常にこの store に commit された行そのものである。

`register_repair_fence(token=...)` の規則(すべて単一 `BEGIN IMMEDIATE` 内):

| 現在の fence | 要求 | 結果 |
|---|---|---|
| 無し | 任意 | INSERT |
| 別 attempt | 任意 | REPLACE(新しい attempt が所有権を取る。旧 attempt の後続 commit / finalize は typed に失敗する) |
| 同一 attempt / **5 要素すべて一致** | 再登録(restart 後の retry) | 冪等 no-op |
| 同一 attempt / stored generation + 1 = 要求 generation | takeover | UPDATE |
| 同一 attempt / 同一 generation・**別 takeover id / 別 claim / 別 owner** | 別実行の割り込み | `ExecutionRepairFenceConflictError`(DB 無変更) |
| 同一 attempt / stored `>` 要求 generation | 巻き戻し | `ExecutionRepairFenceConflictError` |
| 同一 attempt / stored + 1 `<` 要求 generation | 飛び越し | `ExecutionRepairFenceConflictError` |

**「同じ attempt かつ同じ generation」でも identity が違えば adopt しない**のが要点である(誰が正当かを決めるのは durable intent だけで、intent は restart 後も同じ token を提示する)。

fence 行は repair 完了後も**保持**する(cleanup 不要): 新しい attempt が来れば無条件に置換されるため蓄積せず、「最後にどの実行がこの注文を修復したか」の手がかりとしても読める(`get_repair_fence()`。読み取り経路は read-only 接続で、存在しない DB を作成しない)。dry-run は fence を作成も更新もしない。**旧 schema(v3 以前)から migrate した DB では fence テーブルは空**であり、fence が無い注文への repair commit は typed に拒否されるので、migration 直後に不明な identity が滑り込む窓は無い。

### 状態更新 API の契約

すべての状態更新(`mark_submitting` / `record_order_state` / `record_order_result`)は:

- 遷移を既存 `validate_order_state_transition()` で検証する(不正遷移は `ValueError`。terminal からの離脱・UNKNOWN の不許可解決はここで拒否される)。
- **current 更新・snapshot 更新・history 追加を同一 `BEGIN IMMEDIATE` トランザクション**で確定する(すべて or どれも無し)。`broker_order_id` の一意性競合時は current・history とも rollback し `ExecutionOrderConflictError` を送出する。
- `broker_order_id` は NULL → 値の一度きり。以後の**値の変更は監査された Order Reissue(Issue #102)としてのみ許可**する: 新 snapshot が保存済み ID を `superseded_broker_order_ids` chain に記録し、anchored snapshot に対する `validate_order_result_update()` の chain-append 規則(旧 chain + 旧 current ID が新 chain の prefix)を満たす場合に限り、current 行の ID を新 ID へ更新する(旧 ID は history と snapshot chain に監査可能なまま残る)。**無断の差し替え(chain に保存済み ID を記録しない ID 変更)は拒否**する。さらに **commit 前の所有権検証(PR #112 レビュー)**: snapshot が名指す**全 broker order ID(current + superseded chain)**が、他の execution order の current ID **または** superseded chain ID と衝突しないことを同一トランザクション内・書き込み前に検証し、衝突は `ExecutionOrderConflictError` で拒否して DB を無変更のまま rollback する(column UNIQUE 制約は current 列しか守れず、chain ID の衝突が commit されると以後の全 read/write が DB-wide scan で停止してしまうため)。state-only 観測が ID を含まない場合(`None`)は保存済みの値を維持するが、**完全 snapshot が既知の ID を欠落させること(値 → None)も拒否**する。accepted / partially_filled / filled は更新後に broker_order_id が既知であることを要求する(`OrderResult` の構築不変条件の鏡像)。
- `updated_at_utc` を**逆行させない**(過去時刻の観測は `ValueError`)。

すべての公開 API(読み取り含む)は **「open → schema 検証 → トランザクション → row restore / update」の一連の流れ**で動作する(PR #91 レビュー r2 Major 3)。schema 検証に失敗した DB からは読み取りも書き込みも行われない。さらに書き込みは、更新前に対象注文の **bundle 全体(current 行 + history + anchor)を検証**するため、改竄された DB が更新判断に影響することはない。

**commit 前の write-back 再検証(PR #91 レビュー r3 Major 1)**: schema 検証は書き込みの**前**に走るため、それだけには依存しない。すべての書き込み経路は、current 更新・history 追加を行った**同じトランザクション内で**対象 bundle を再読・再検証してから COMMIT する。

- bundle 全体の整合(current 行・history chain・anchored snapshot・broker_order_id chain・state transition・reason・updated_at・snapshot / current / history 整合)
- **既存 history 行が 1 行も変更・削除されていないこと**(行単位の完全一致)
- current 行と追記された history 行が**意図した値と完全一致**していること

いずれかが不一致なら(例: schema 検証をすり抜けた副作用がトランザクション中に発火した場合)`ExecutionIntegrityError` を送出し、**同じトランザクションを rollback** する。破損状態が commit されることはなく、更新 API が失敗を返した場合、DB の current / history / snapshot は呼び出し前と完全一致する。commit 後に初めて検証する経路は存在しない(更新 API の戻り値もトランザクション内で検証済みの行である)。

**`record_order_result`(正の更新経路。PR #91 レビュー Major 1)**:

- snapshot はこの注文に属していなければならない(`client_order_id` と identity / request フィールド全部が保存済みの intent と一致)。
- **history に anchor された直前 snapshot を必ず `validate_order_result_update()` の old 側として使用**する(メモリ上のコピーは使わない)。stale な snapshot を根拠に、既知の fill・`filled_quantity` / `remaining_quantity`・`broker_order_id`・`updated_at_utc` を巻き戻すことは、**プロセス再起動直後であっても**不可能である。
- 呼び出し側が `previous_result` を渡す場合、**anchored snapshot と完全一致するときのみ受理**する(呼び出し側の stale 前提を検出する)。
- 遷移は snapshot との内容検証に加えて、**現在の追跡状態**(snapshot の後に state-only UNKNOWN が記録されていることがある)に対しても検証する。
- `expected_current_state` / `expected_revision`(Issue #104、optional)を渡すと**原子的 compare-and-set** になる: 同一 `BEGIN IMMEDIATE` トランザクション内で読み直した現在状態 / **revision**(= 履歴行数。すべての書き込み — same-state 再観測・snapshot / broker_order_id / timestamp のみの更新を含む — で必ず 1 増える導出値。schema 列ではない)が一致しなければ `ExecutionOrderStateConflictError` で拒否し、DB は無変更のまま rollback する。**state だけの比較では same-state 更新(UNKNOWN → UNKNOWN 履歴追加・部分約定の増加・snapshot refresh)を検出できないため、repair は revision CAS を用いる**。省略時は従来どおり(same-state 再観測は許可・記録)。read API が返す行 dict には同一トランザクションで導出した `revision` キーが含まれる。
- `repair_attempt_id` / `repair_generation`(Issue #104、optional。**両方 or どちらも無し**)は追加される history 行に**構造化された repair provenance**として記録される(schema v4 列 + partial UNIQUE index)。1 つの repair attempt は DB 全体で高々 1 行しか commit できず、recovery は reason 文字列の解析ではなくこの列で「この attempt が適用済みか」を判定する。provenance 付き history 行は必ず完全 snapshot を伴う(復元時検証)。
- **fencing 検証(Issue #104 再レビュー r4)**: `repair_fence` を伴う更新は、**同じトランザクション内で** `execution_repair_fences` の行が**この token と 5 要素すべて一致**(attempt id / generation / takeover id / owner / claim execution id)することを確認する。fence が無い / 別 attempt / 別 generation / **同一 generation の別実行**のいずれでも `ExecutionRepairFenceConflictError` で**全 rollback**する。したがって claim を takeover された旧 worker は、**現在状態も revision もまだ一致していても** commit できない。1 回の更新で検証されるのは `expected_current_state` / `expected_revision` / fence identity の全要素であり、どれか 1 つでも外れれば DB は無変更のままになる。呼び出し側はこの typed 例外を `applied` に分類してはならない(repair サービスは `conflict` + `claim_lost` / `repair_fence_conflict`、あるいは provenance が示す場合のみ `already_applied` として結果化する)。

**`record_order_state`(state-only 経路)は UNKNOWN の記録専用**である。UNKNOWN は「完全な `OrderResult` を構造上取得できなかった」(submit / query の結果喪失、例: `BrokerTimeoutError`)ことを表す唯一の状態であるため、snapshot 無しで記録できるのはこれだけとする。他のすべての観測は Broker snapshot を伴うため `record_order_result` を通る。state-only UNKNOWN は保存済み snapshot に**触れない**ため、結果不明になる前に判明していた fill が後続の解決を検証し続ける。また state-only 更新は保存済み `broker_order_id` を**確認できるだけ**であり、新規導入・変更はできない(Broker ID は完全な `OrderResult` からのみ得られる)。

**同一状態の再観測(PR #87 契約の維持)**: `validate_order_state_transition` は全状態(terminal 含む)で same-state 更新を許可し、terminal の same-state 再観測は `validate_order_result_update` により `updated_at_utc` 以外の全フィールド不変が強制される。**設計判断として、再観測は履歴へ残す** — `updated_at_utc` / `last_state_reason` を更新し history 行を追加する(照会・reconciliation の観測が監査可能になる)。polling の no-op を履歴に残したくない呼び出し側は store を呼ばなければよい。

**同一状態の再観測(PR #87 契約の維持)**: `validate_order_state_transition` は全状態(terminal 含む)で same-state 更新を許可する。**設計判断として、再観測は履歴へ残す** — `updated_at_utc` / `last_state_reason` を更新し history 行を追加する(照会・reconciliation の観測が監査可能になる)。polling の no-op を履歴に残したくない呼び出し側は store を呼ばなければよい。

### UNKNOWN の扱い

- UNKNOWN は**そのまま保存**され、phase は `submitting` に留まる。
- `list_unknown_orders()` が **照会対象**(`order_state = 'unknown'`)を抽出する。
- `list_unresolved_submissions()`(Issue #75)は、UNKNOWN に加えて **`submitting` phase のまま `pending` に留まる注文**(送信試行後に Broker 観測が一度も記録されないまま停止したケース)も照会対象として返す read-only API。Broker が注文を保持しうる submission が reconciliation の視界から消えないことを保証する([Safety Gate](safety_gate.md))。
- `list_in_flight_orders()`(Issue #103)は、**終端でない全注文**(`order_state` が pending / accepted / partially_filled / unknown)だけを返す read-only APIであり、終端状態を in-flight と誤分類しない。`list_reconciliation_orders()`(同)は Broker state を持つ全注文(in-flight + terminal、`created` は除外)を返し、Broker 照合の完全な比較対象抽出に使う。終端状態は BELIAL 内では不変でも、Broker の外部事実と矛盾していないか監査する必要があるためである。`list_client_order_ids()`(同)は Broker 側にのみ存在する注文を判定するための `client_order_id` 全件(終端・created 含む)を返す。`get_execution_order_by_broker_order_id()`(同)は **`broker_order_id` を所有している注文**を返す(現在 ID の column に加え、Issue #102 の reissue で過去になった ID も current snapshot の `superseded_broker_order_ids` chain から解決する。重複 scan により高々 1 件。未割当は None)— Broker 観測を反映可能と判断する前に、その ID が別 Execution Request に割当済みでないことを確認するために使う。いずれも missing DB を作成せず、状態行を返す 3 APIは行ごとに完全な bundle 検証を通す([Broker Reconciliation](broker_reconciliation.md))。
- UNKNOWN の解決は、後続照会の結果(完全な `OrderResult`)を `record_order_result` で記録することによってのみ行われる。許可される解決先は既存遷移表のとおり(accepted / partially_filled / filled / cancelled / rejected)で、**保存済み snapshot の fill を失う解決(例: 部分約定既知のまま fill 無しの CANCELLED / REJECTED へ確定)は `validate_order_result_update()` が拒否**する。
- store / API は UNKNOWN を「未発注」「拒否」「再送可」へ**自動変換しない**。即時再送可能状態として扱わない。
- 自動照会 runner・**定期** reconciliation は**非スコープ**(未実装)。Broker 照合の**差分検出**(内部状態を変更しない照合・分類・記録)は Issue #103 で、`auto_repair_candidate` に分類された注文状態の**安全な修復**(既存状態機械経由・冪等・`expected_current_state` による compare-and-set。stale / conflict / manual review は変更しない)は Issue #104 で実装済み([Broker Reconciliation](broker_reconciliation.md))。いずれもサービス API の明示呼び出しのみで、scheduler への組み込みはない。

### 二重発注防止と idempotency(Issue #76)

同一 Execution Request の再処理・同一 Signal からの重複生成・多重起動時の競合が Broker への重複送信を生まないことは、**永続化層の契約**として保証する(インメモリ集合・プロセス内ロック・FakeBroker 内部の判定に依存しない)。

- **一意性の最終防衛線は DB 制約 + トランザクション**: `execution_request_id`(PRIMARY KEY)・`signal_id`(UNIQUE index)・`client_order_id`(UNIQUE)。事前 SELECT は失敗後の分類にのみ使い、重複判定そのものは `BEGIN IMMEDIATE` トランザクション内の INSERT / UPDATE が制約に衝突するかどうかで確定する。既存行とのキー一致を確認できた並行 creator の敗者だけを `ExecutionOrderConflictError`(正常系の抑止)へ分類し、CHECK / NOT NULL や未知制約の `sqlite3.IntegrityError` は重複として扱わず障害として伝播する。複数キーが異なる既存行と同時に衝突した場合は全キーを報告する。
- **送信権の獲得は短いトランザクションで完了してから Broker を呼ぶ**: `mark_submitting()`(created → submitting、`order_state = pending` の永続化)が `BEGIN IMMEDIATE` で直列化され一度しか成功しないため、Broker を呼べる実行主体は最大 1 つ。外部 Broker 呼び出し中に SQLite の書き込みトランザクションを保持しない。
- **再処理の抑止**: phase が `created` 以外(submitting / submitted。order_state が pending / accepted / partially_filled / filled / unknown / terminal のいずれでも)の Request は再送信されない([Safety Gate](safety_gate.md) の `duplicate_submission`)。**REJECTED も同一意図での再送は不可**(判断根拠: `client_order_id` は 1:1 不変で `mark_submitting` は一度きり、かつ再発行レコードは `signal_id` UNIQUE が拒否する。暗黙の再送可能状態は存在しない)。
- **タイムアウト・結果不明は UNKNOWN として保存**され、即時再送しない(上記「UNKNOWN の扱い」)。
- **重複抑止ログ**: 抑止時は WARN で `execution_request_id`・`signal_id`・現在の `submission_phase` / `order_state`・抑止理由を必ず出力する(store の作成時 conflict と送信サービスの `duplicate_submission` の両方)。
- `get_execution_order_by_signal_id()`: signal からの逆引き read helper(高々 1 件)。missing DB はファイルを作らず None。
- **Shadow Mode(Issue #78)は本 store の idempotency namespace に参加しない**: Shadow 検証記録は専用の shadow store(`shadow_results.sqlite3`)にのみ保存され、`execution_orders` へは登録されない。したがって Shadow を実行しても同一 signal の将来の fake / demo / live 送信が「既に送信済み」と判定されることはなく、逆に live 側の register が Shadow 実行をブロックすることもない([Shadow Mode](shadow_execution.md))。

### schema 管理・読み取り経路

- schema version は **v4**(v1 = PR #91 初版、v2 = r2 レビューで history anchor 列・PK の NOT NULL / WITHOUT ROWID を追加、v3 = Issue #76 で `signal_id` の UNIQUE index を追加、**v4 = Issue #104 で history の repair provenance 2 列 + `execution_repair_fences` テーブル**)。
- **v2 → v3 migration**(Issue #76): `init_db()` が「**完全な構造検証で v2 と一致した DB のみ**」を additive に migration する — 重複 `signal_id` の事前 scan → `CREATE UNIQUE INDEX` → version bump → **完全再検証** → COMMIT を**単一の `BEGIN IMMEDIATE` トランザクション**で行う。途中失敗は全体 rollback となり中途半端な schema は残らない。既存行は削除・書き換えされない。**既存データに重複 signal がある場合は明示的に失敗**し(重複の解消は人間の判断)、DB は無変更。
- **v3 → v4 migration**(Issue #104): 完全一致した v3 DB のみを `init_db()` が additive に migration する — `ALTER TABLE ... ADD COLUMN repair_attempt_id` / `ADD COLUMN repair_generation`(SQLite は列を**テーブル制約の前・列リスト末尾**へ挿入するため、migration 後の DDL は v4 参照 CREATE と token 正規化で一致する)+ `CREATE TABLE execution_repair_fences`(`takeover_id` 列を含む)+ partial UNIQUE index + version bump + **完全再検証**を単一トランザクションで行う。既存 history 行は両列 NULL として読み戻され、fence テーブルは空で始まる(fence の無い注文への repair commit は typed に拒否されるため、migration 直後に不明な identity が滑り込む窓は無い)。fence テーブルは v4 で新設されるため、**旧 schema 由来の fence 行や NULL identity は存在しない**。途中失敗は全 rollback で v3 ファイル無変更。v2 DB は v3 を経由して連鎖 migration される。v1 および migratable でない schema は従来どおり DB 無変更で失敗する。**読み取り経路は migration しない**(v2 / v3 DB の読み取りは `init_db()` への誘導を含む `ExecutionSchemaError`)。
- `init_db()` が schema を**新規作成するのは pristine な DB のみ**(PR #91 レビュー r2 Major 2): `sqlite_master` のオブジェクトが **0 件**かつ `user_version = 0`。**version だけが設定された DB・metadata だけが残った DB(`sqlite_sequence` 等)は新規 DB として扱わず**、DB を変更せずに失敗する。
- 既存 schema の判定は**列名ではなく完全な構造検証**で行う(PR #91 レビュー r2 Minor 2 / r3 Major 1 / r3 Minor 1)。
  - **sqlite_master 全 object 集合の closed-world 検証(r3 Major 1)**: `type IN ('table','index')` の抽出ではなく、**全 type の object 集合**を pristine な参照 schema(in-memory)の集合と完全一致で比較する。許可される SQLite 内部 object は本 schema 自身が作るもの(AUTOINCREMENT の `sqlite_sequence`、UNIQUE 制約の `sqlite_autoindex_*`)だけを**明示的に**参照集合へ含める。**trigger・view・予期しないテーブル・予期しない index は type を問わずすべて失敗**である(trigger / view は store の背後で行を書き換え・削除できるため、存在自体を拒否する)。
  - **構造(列・NOT NULL / PK / UNIQUE / FK / index)は PRAGMA signature が正**: `PRAGMA user_version` に加え、`PRAGMA table_info` / `index_list` / `index_info` / `foreign_key_list` から構築した構造 signature を参照 schema と比較する。
  - **DDL は token 正規化比較(r3 Minor 1)**: `sqlite_master.sql` のバイト一致は要求しない。quoted identifier(`"execution_orders"` 等)・空白 / 改行・keyword の大文字小文字・コメント・冗長な括弧(単一 atom を包む括弧と二重括弧のみ、意味を保存できる collapse に限定)は SQLite 上同値なため吸収する。**設計判断**: CHECK 式のみの抽出比較ではなく DDL 全体の token 比較を選んだ。PRAGMA から見えない意味論は CHECK 式だけでなく `WITHOUT ROWID` / `AUTOINCREMENT` / `COLLATE` 等にも存在し、token 比較はそれらを一括で防御できる。CHECK 式内の**文字列 literal は大文字小文字を保存**して比較し(`'created'` → `'CREATED'` は拒否)、正規化で同値と証明できない差分はすべて不一致として扱う(fail closed。同値だが表記が大きく異なる式の誤拒否は残るが、安全側)。
- **migration 経路はリリース済み v2 からの additive 移行(上記)のみ**: partial schema(片側テーブル欠損)・制約欠損の同名テーブル・未知 version・他コンポーネントの DB は、いずれも **DB を一切変更せず** `ExecutionSchemaError` で明示的に失敗する(自動補完・黙った rebuild をしない)。v1 は未リリース schema のため migration を提供しない。
- **復元時の整合検証**(PR #91 レビュー Major 2 / r2 Major 1): 注文を返すすべての読み取りは schema 検証の後、enum 値・UTC・非空 ID / reason・**current / history chain**(先頭は created → pending、リンク欠落なし、各ペアが許可遷移、末尾が current 行と一致)・phase 整合・broker_order_id の chain・**snapshot の history anchor**(上記)を再検証し、帯域外編集で不整合になった DB は `ExecutionIntegrityError` で拒否する。**DB 全体の `broker_order_id` 重複 scan** も読み取り・書き込みの両方で実施し、重複を保持する DB からは一切復元しない(PR #91 レビュー r2 Major 3)。scan は **current ID column に加えて current snapshot の `superseded_broker_order_ids` chain の ID も対象**とし、「現在 ID か過去 ID かに関係なく、1 つの Broker 注文 ID は 1 つの execution request にしか属せない」ことを検証する(Issue #102)。
- snapshot JSON は**厳格に**解析する(PR #91 レビュー r2 Minor 1): top-level と fills の**キー集合が期待集合と完全一致**しなければならず、未知 field・欠落 field は拒否する。**唯一の例外(Issue #102 の後方互換)**: reissue 対応以前に書かれた snapshot は `superseded_broker_order_ids` キーを持たないため、**このキーの欠落だけは「空 chain」として受理**する(当時 reissue は表現不能だったので空 chain が当時の意味そのもの。書き込みは常にキーを含む。schema DDL は不変で migration は不要)。
- 読み取り API は存在しない DB ファイルを**作成しない**(missing → None / 空)。存在するファイルの読み取りエラーは握り潰さず伝播する。更新 API も存在しないファイルを作成せず `ExecutionOrderNotFoundError` にする。

## 非スコープ(Issue #74 時点で未実装)

- Live Execution runner / ユースケース本体
- Broker API 実装(OANDA 等の Adapter)・実注文送信・外部通信
- Safety Gate(その後 Issue #75 で実装済み。[Safety Gate](safety_gate.md) を参照)
- 自動照会・定期 reconciliation
- 再送フロー(UNKNOWN の解決は照会結果の記録のみ)
- Paper Signal / Paper Execution からの接続(両レイヤーは本パッケージを使わない)

## テスト

`python -m pytest execution/tests -q`(`tmp_path` 配下の SQLite のみ。外部通信・Discord・S3・sleep・実注文なし)。

- `test_execution_models.py` — 正常生成、必須 ID 空文字拒否、quantity / price 不正拒否、naive datetime 拒否、MARKET / LIMIT 価格整合、SL / TP 方向整合(一意に保証できる関係のみ)、`to_order_request` 変換(SL / TP が境界を越えないこと)。
- `test_execution_store.py` — 初期レコード保存・復元、created → submitting(2 接続競合で 1 更新のみ成功を含む)、ACCEPTED 保存、ACCEPTED → PARTIALLY_FILLED → FILLED / REJECTED / CANCELLED、state-only API の UNKNOWN 限定と broker_order_id 新規導入拒否、stale snapshot による fill 消失拒否・`previous_result` と anchored snapshot の一致要求・再起動後も更新検証が機能すること、UNKNOWN 保存・抽出・許可解決・fill を失う解決の拒否、**UNKNOWN 中の current 行 snapshot 差し替え検出(history anchor)**・current 行 broker_order_id 改竄検出・history snapshot 改竄 / 剥離検出、terminal からの不正遷移拒否と terminal same-state 再観測、broker_order_id 差し替え / 欠落拒否と **DB 全体一意性(競合時の全 rollback・復元時重複 scan)**、updated_at 逆行拒否、current + snapshot + history の原子性、**pristine 判定**(version-only DB / metadata-only DB の拒否・無変更)、制約欠損 / partial / 他コンポーネント DB / version 不一致 / index 差し替え / index 欠落 schema の拒否(DB 無変更)、**trigger / view の検出**(AFTER UPDATE で history 削除・BEFORE UPDATE で値書き換え・AFTER INSERT で history 追加・view。全公開 API が DB 無変更 = バイト一致で `ExecutionSchemaError`、r3 Major 再現経路の回帰、trigger 削除後は同じ更新が成功)、**schema 検証後にトランザクション内で発火する副作用の commit 前検出と rollback**(history 削除 / 既存 history 改竄 / current 行書き換えの 3 系統、失敗後の current / history / snapshot 完全一致、副作用が無ければ同じ更新が成功)、**同値 DDL の受理**(quoted identifier・空白 / 改行・keyword case・冗長括弧・コメント)と **CHECK 欠損 / CHECK 式変更 / literal 変更・NOT NULL / UNIQUE / FK / WITHOUT ROWID 欠損の拒否**、**read API の schema 検証**、DB CHECK 制約の発火(NULL PK 含む)、snapshot JSON の未知 field 拒否、DB を開き直した後の完全復元(fills / Decimal / UTC 含む)、init 冪等性、missing DB を作成しないこと。
- `test_execution_store.py`(Issue #76 追加分)— 同一 `signal_id` の 2 件目作成が conflict になり行が増えないこと、3種の一意キー競合の個別・複合分類と CHECK / NOT NULL 系 `IntegrityError` の非誤分類、重複抑止の WARN ログ(execution_request_id / signal_id / 既存行の phase・state / 理由)、**独立 DB 接続 2 本の同時作成で 1 行のみ**(敗者は正常な conflict でプロセス継続)、schema レベルの UNIQUE 発火(帯域外 INSERT も拒否)、`get_execution_order_by_signal_id` の round-trip と missing DB 非作成、**v2 → v3 migration**(既存行の完全保持・migration 後の通常読み取り・制約の有効化・冪等性・post-migration検証失敗時のindex/version完全rollback・WAL DBへの2接続同時migration)、**重複 signal を含む v2 の migration 明示失敗(DB 無変更)**、読み取り・更新経路が v2 DB を migration しないこと。
- `test_package_imports.py` — リポジトリルートからの package import(subprocess)、Paper / Observation レイヤーが execution を import しないこと(AST 走査)、execution が stdlib + broker 以外へ依存しないこと。
- `test_scenario_models.py` / `test_scenario_runner.py` / `test_fixed_scenarios.py`(Issue #77)— 固定シナリオ試験基盤。本 store と `LiveOrderSubmissionService` を本番経路のまま固定データで駆動し、正常約定・重複・UNKNOWN・DB保存失敗(初期保存 / SUBMITTING 遷移 / 受付後結果保存 / 状態履歴の各境界)・多重起動競合などの分岐を expected/actual 比較で固定する。実行方法・schema は `execution/README.md` の「固定シナリオ試験」節を参照。
