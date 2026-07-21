# Execution Request / Order Persistence(Issue #74)

将来の **Live Execution** に向けた、Strategy / Signal から Execution ユースケースへ渡す共通モデル **ExecutionRequest** と、注文状態・状態履歴を SQLite へ永続化する基盤の仕様。実装は `execution/`(明示的な Python package。`__init__.py` あり)にあり、リポジトリルートから通常の package import で利用する。

```python
from execution.execution_models import ExecutionRequest
from execution import execution_store
```

> **重要な位置づけ**
>
> - 本コンポーネントは **将来の Live Execution 用のモデル・状態永続化基盤のみ** を実装する。
> - **Live Execution 本体・Broker API Adapter・実発注・Safety Gate・reconciliation runner は未実装**である。このコンポーネントが存在しても Live Execution が実装済みになったわけではない。
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

store は両者を **1:1 で固定**する(`client_order_id` は NOT NULL + UNIQUE、保存後は不変)。UNKNOWN(結果不明)後の照会は**保存済みの `client_order_id`** で行う。同じ意図を**新しい `client_order_id` で再送することが二重発注の正体**であるため、モデル・store は client order ID を暗黙に生成・更新・ローテーションしない。再送(意図の再発行)が必要なら、それは新しい `execution_request_id` + 新しい `client_order_id` を持つ別レコードであり、UNKNOWN の解決とは区別される(再送フロー自体は非スコープ)。

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
- `client_order_id` TEXT NOT NULL UNIQUE(1:1・不変)
- `submission_phase` TEXT NOT NULL(CHECK: 上記 3 値)
- `order_state` TEXT NULL(CHECK: `OrderState` の値集合。`created` ⇔ NULL を CHECK で強制)
- `broker_order_id` TEXT NULL **UNIQUE(テーブル全体で一意。PR #91 レビュー Major 3)**: 1 つの Broker 注文が複数の execution request に関連付くことを DB レベルで拒否する
- `last_state_reason` TEXT NULL
- `last_order_result_json` TEXT NULL(**最新の完全な `OrderResult` snapshot**。PR #91 レビュー Major 1。Decimal は正準文字列・日時は UTC ISO・fills 含めて損失なく JSON 化し、current 行・履歴行と同一トランザクションで保存)
- `created_at_utc` / `updated_at_utc` TEXT NOT NULL

アプリケーション層の検証に加え、主要な不変条件(ID / reason の非空、UTC サフィックス、MARKET / LIMIT × `requested_price`、state × `broker_order_id`、phase × `order_state`)は **DB CHECK 制約でも二重に強制**する(PR #91 レビュー Major 2)。

`execution_order_state_history`(状態履歴。変更理由の追跡):

- `history_id` INTEGER PRIMARY KEY AUTOINCREMENT
- `execution_request_id` TEXT NOT NULL(FK → `execution_orders`。`PRAGMA foreign_keys=ON`)
- `previous_state` TEXT NULL(最初の履歴行 = created → pending は NULL)
- `new_state` TEXT NOT NULL
- `reason` TEXT NOT NULL
- `broker_order_id` TEXT NULL(変更時点の値)
- `changed_at_utc` TEXT NOT NULL

### 状態更新 API の契約

すべての状態更新(`mark_submitting` / `record_order_state` / `record_order_result`)は:

- 遷移を既存 `validate_order_state_transition()` で検証する(不正遷移は `ValueError`。terminal からの離脱・UNKNOWN の不許可解決はここで拒否される)。
- **current 更新・snapshot 更新・history 追加を同一 `BEGIN IMMEDIATE` トランザクション**で確定する(すべて or どれも無し)。`broker_order_id` の一意性競合時は current・history とも rollback し `ExecutionOrderConflictError` を送出する。
- `broker_order_id` は NULL → 値の一度きり。以後**別 ID への差し替えを拒否**する。state-only 観測が ID を含まない場合(`None`)は保存済みの値を維持するが、**完全 snapshot が既知の ID を欠落させること(値 → None)も拒否**する。accepted / partially_filled / filled は更新後に broker_order_id が既知であることを要求する(`OrderResult` の構築不変条件の鏡像)。
- `updated_at_utc` を**逆行させない**(過去時刻の観測は `ValueError`)。

**`record_order_result`(正の更新経路。PR #91 レビュー Major 1)**:

- snapshot はこの注文に属していなければならない(`client_order_id` と identity / request フィールド全部が保存済みの intent と一致)。
- **DB に保存された直前 snapshot を必ず `validate_order_result_update()` の old 側として使用**する。stale なメモリ上の snapshot を根拠に、既知の fill・`filled_quantity` / `remaining_quantity`・`broker_order_id`・`updated_at_utc` を巻き戻すことは、**プロセス再起動直後であっても**不可能である。
- 呼び出し側が `previous_result` を渡す場合、**DB snapshot と完全一致するときのみ受理**する(呼び出し側の stale 前提を検出する)。
- 遷移は DB snapshot との内容検証に加えて、**現在の追跡状態**(snapshot の後に state-only UNKNOWN が記録されていることがある)に対しても検証する。

**`record_order_state`(state-only 経路)は UNKNOWN の記録専用**である。UNKNOWN は「完全な `OrderResult` を構造上取得できなかった」(submit / query の結果喪失、例: `BrokerTimeoutError`)ことを表す唯一の状態であるため、snapshot 無しで記録できるのはこれだけとする。他のすべての観測は Broker snapshot を伴うため `record_order_result` を通る。state-only UNKNOWN は保存済み snapshot に**触れない**ため、結果不明になる前に判明していた fill が後続の解決を検証し続ける。

**同一状態の再観測(PR #87 契約の維持)**: `validate_order_state_transition` は全状態(terminal 含む)で same-state 更新を許可し、terminal の same-state 再観測は `validate_order_result_update` により `updated_at_utc` 以外の全フィールド不変が強制される。**設計判断として、再観測は履歴へ残す** — `updated_at_utc` / `last_state_reason` を更新し history 行を追加する(照会・reconciliation の観測が監査可能になる)。polling の no-op を履歴に残したくない呼び出し側は store を呼ばなければよい。

**同一状態の再観測(PR #87 契約の維持)**: `validate_order_state_transition` は全状態(terminal 含む)で same-state 更新を許可する。**設計判断として、再観測は履歴へ残す** — `updated_at_utc` / `last_state_reason` を更新し history 行を追加する(照会・reconciliation の観測が監査可能になる)。polling の no-op を履歴に残したくない呼び出し側は store を呼ばなければよい。

### UNKNOWN の扱い

- UNKNOWN は**そのまま保存**され、phase は `submitting` に留まる。
- `list_unknown_orders()` が **照会対象**(`order_state = 'unknown'`)を抽出する。
- UNKNOWN の解決は、後続照会の結果(完全な `OrderResult`)を `record_order_result` で記録することによってのみ行われる。許可される解決先は既存遷移表のとおり(accepted / partially_filled / filled / cancelled / rejected)で、**保存済み snapshot の fill を失う解決(例: 部分約定既知のまま fill 無しの CANCELLED / REJECTED へ確定)は `validate_order_result_update()` が拒否**する。
- store / API は UNKNOWN を「未発注」「拒否」「再送可」へ**自動変換しない**。即時再送可能状態として扱わない。
- 自動照会 runner・定期 reconciliation は**非スコープ**(未実装)。

### schema 管理・読み取り経路

- `init_db()` は冪等。ただし「current」の判定は**列名ではなく完全な構造検証**で行う(PR #91 レビュー Major 2): `PRAGMA user_version`、両テーブルの**正規化 CREATE SQL 全体**(NOT NULL / UNIQUE / CHECK / FK をすべて含む)、必須 index、**予期しないテーブル・index が存在しないこと**を検証する。
- **新規コンポーネント・新規 DB のため legacy migration 経路は存在しない**: partial schema(片側テーブル欠損)・制約欠損の同名テーブル・version 不一致・他コンポーネントの DB は、いずれも **DB を一切変更せず** `ExecutionSchemaError` で明示的に失敗する(自動補完・黙った rebuild をしない)。
- **復元時の整合検証**(PR #91 レビュー Major 2): 注文を返すすべての読み取りは、enum 値・UTC・非空 ID / reason・**current / history chain**(先頭は created → pending、リンク欠落なし、各ペアが許可遷移、末尾が current 行と一致)・phase 整合・snapshot JSON(`OrderResult` として再構築可能でこの注文に属する)を再検証し、帯域外編集で不整合になった DB は `ExecutionIntegrityError` で拒否する。
- 読み取り API は存在しない DB ファイルを**作成しない**(missing → None / 空)。存在するファイルの読み取りエラーは握り潰さず伝播する。更新 API も存在しないファイルを作成せず `ExecutionOrderNotFoundError` にする。

## 非スコープ(Issue #74 時点で未実装)

- Live Execution runner / ユースケース本体
- Broker API 実装(OANDA 等の Adapter)・実注文送信・外部通信
- Safety Gate
- 自動照会・定期 reconciliation
- 再送フロー(UNKNOWN の解決は照会結果の記録のみ)
- Paper Signal / Paper Execution からの接続(両レイヤーは本パッケージを使わない)

## テスト

`python -m pytest execution/tests -q`(`tmp_path` 配下の SQLite のみ。外部通信・Discord・S3・sleep・実注文なし)。

- `test_execution_models.py` — 正常生成、必須 ID 空文字拒否、quantity / price 不正拒否、naive datetime 拒否、MARKET / LIMIT 価格整合、SL / TP 方向整合(一意に保証できる関係のみ)、`to_order_request` 変換(SL / TP が境界を越えないこと)。
- `test_execution_store.py` — 初期レコード保存・復元、created → submitting(2 接続競合で 1 更新のみ成功を含む)、ACCEPTED 保存、ACCEPTED → PARTIALLY_FILLED → FILLED / REJECTED / CANCELLED、state-only API の UNKNOWN 限定、stale snapshot による fill 消失拒否・`previous_result` と DB snapshot の一致要求・再起動後も更新検証が機能すること、UNKNOWN 保存・抽出・許可解決・fill を失う解決の拒否、terminal からの不正遷移拒否と terminal same-state 再観測、broker_order_id 差し替え / 欠落拒否と **DB 全体一意性(競合時の全 rollback)**、updated_at 逆行拒否、current + snapshot + history の原子性、制約欠損 / partial / 他コンポーネント DB / version 不一致 schema の拒否(DB 無変更)、DB CHECK 制約の発火、不正 current / history chain・改竄 snapshot の復元拒否、DB を開き直した後の完全復元(fills / Decimal / UTC 含む)、init 冪等性、missing DB を作成しないこと。
- `test_package_imports.py` — リポジトリルートからの package import(subprocess)、Paper / Observation レイヤーが execution を import しないこと(AST 走査)、execution が stdlib + broker 以外へ依存しないこと。
