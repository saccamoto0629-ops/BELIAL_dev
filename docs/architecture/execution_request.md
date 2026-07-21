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
- `broker_order_id` TEXT NULL(未確定時 NULL)
- `last_state_reason` TEXT NULL
- `created_at_utc` / `updated_at_utc` TEXT NOT NULL

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
- **current 更新と history 追加を同一 `BEGIN IMMEDIATE` トランザクション**で確定する(両方 or どちらも無し)。
- `broker_order_id` は NULL → 値の一度きり。以後**別 ID への差し替えを拒否**する。観測が ID を含まない場合(`None`)は保存済みの値を維持する(NULL へ戻さない)。accepted / partially_filled / filled は更新後に broker_order_id が既知であることを要求する(`OrderResult` の構築不変条件の鏡像)。
- `updated_at_utc` を**逆行させない**(過去時刻の観測は `ValueError`)。
- `record_order_result(execution_request_id, result, reason=..., previous_result=...)` は `OrderResult` スナップショットから state / broker_order_id / 時刻を導出する。スナップショットの `client_order_id` が保存済みのものと一致しない場合は拒否する。呼び出し側が直前スナップショットを保持している場合は `previous_result` を渡すことで `validate_order_result_update()` の内容レベル契約(PR #87)も強制される。store は state レベルの情報のみ永続化するため、再起動をまたぐ内容レベル検証は両スナップショットを持つ呼び出し側の責務である。

**同一状態の再観測(PR #87 契約の維持)**: `validate_order_state_transition` は全状態(terminal 含む)で same-state 更新を許可する。**設計判断として、再観測は履歴へ残す** — `updated_at_utc` / `last_state_reason` を更新し history 行を追加する(照会・reconciliation の観測が監査可能になる)。polling の no-op を履歴に残したくない呼び出し側は store を呼ばなければよい。

### UNKNOWN の扱い

- UNKNOWN は**そのまま保存**され、phase は `submitting` に留まる。
- `list_unknown_orders()` が **照会対象**(`order_state = 'unknown'`)を抽出する。
- UNKNOWN の解決は、後続照会の結果を通常の検証済み更新経路(`record_order_state` / `record_order_result`)で記録することによってのみ行われる。許可される解決先は既存遷移表のとおり(accepted / partially_filled / filled / cancelled / rejected)。
- store / API は UNKNOWN を「未発注」「拒否」「再送可」へ**自動変換しない**。即時再送可能状態として扱わない。
- 自動照会 runner・定期 reconciliation は**非スコープ**(未実装)。

### schema 管理・読み取り経路

- `init_db()` は冪等(`PRAGMA user_version` = 1 を刻印)。**新規コンポーネント・新規 DB のため legacy migration 経路は存在しない**: 既存テーブルが期待スキーマと一致しない場合は `ExecutionSchemaError` で明示的に失敗する(黙って rebuild・再作成しない)。
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
- `test_execution_store.py` — 初期レコード保存・復元、created → submitting、ACCEPTED 保存、ACCEPTED → PARTIALLY_FILLED → FILLED / REJECTED / CANCELLED、UNKNOWN 保存・抽出・許可解決、terminal からの不正遷移拒否、broker_order_id 差し替え拒否、updated_at 逆行拒否、current + history の原子性(履歴側の失敗で current 更新も rollback)、DB を開き直した後の完全復元、init 冪等性、missing DB を作成しないこと。
- `test_package_imports.py` — リポジトリルートからの package import(subprocess)、Paper / Observation レイヤーが execution を import しないこと(AST 走査)、execution が stdlib + broker 以外へ依存しないこと。
