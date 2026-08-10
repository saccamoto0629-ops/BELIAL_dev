# Execution Intent Reissue — REJECTED 後の再発行モデル(Issue #95 設計)

`REJECTED` で終端した Execution Request の注文意図を、idempotency と二重発注防止を壊さずに再発行するためのモデル定義。**本書は設計判断のみであり、実装は含まない**。schema・store API・Broker モデルの変更はすべて後続 Issue(下記「後続 Issue への分割」)で行い、実装が入るまで現行契約(schema v4、`signal_id` の DB 全体 UNIQUE、再発行経路の不存在)は不変である。

> **重要な位置づけ**
>
> - Live Execution 本体・実 Broker 接続・自動売買は引き続き未実装・非スコープ([Concept](../product/concept.md) の「非スコープ」)。本書は将来の Live Execution 運用で必要になる契約を先に確定するもの。
> - 注文状態の唯一の正は既存 `broker.OrderState` / `ORDER_STATE_TRANSITIONS` のまま。**本設計は状態機械へ一切遷移を追加しない**(REJECTED は terminal のまま)。
> - **Broker Order Reissue(Issue #102)とは別概念**である(下記「用語」)。
> - OANDA 固有の拒否コード設計・Broker reconciliation 全体・UNKNOWN の自動再送は非スコープ(Issue #95 の非スコープをそのまま維持)。

## 設計判断の要約

1. **REJECTED は同一 Execution Request にとって terminal のまま維持する。** 同一 `execution_request_id` / 同一 `client_order_id` での再送信経路は今後も作らない(`ORDER_STATE_TRANSITIONS` 不変・`mark_submitting()` 一度きり・Journal 第二ガード不変)。
2. **再発行は「暗黙のリトライ」ではなく「明示的な新しい注文意図」**として扱う: 新しい `execution_request_id` + 新しい `client_order_id` を持つ**別の ExecutionRequest レコード**を、元 Request との親子関係(`reissued_from_execution_request_id` + `signal_attempt`)を永続化して作成する。元 Request の行・履歴・snapshot には一切触れない。親子リンクは `reissue_kind` で 2 種類に区別する — **`broker_rejected`**(Broker が確定拒否した意図の再発行)と **`gate_correction`**(Broker へ一度も到達していない意図の訂正差し替え。下記「Safety Gate 拒否の扱い」)。
3. **子 Request が親から変更してよいフィールドを限定する**: `signal_id` / `strategy_id` / `symbol` / `side` は identity として不変、注文パラメータ(数量・価格・SL / TP・order type)のみ訂正可能。数量は減少方向のみ。自動再試行は全パラメータ同一を必須とする(下記「子 Request のフィールド不変条件」)。
4. **再発行可否は Broker 中立の正規化拒否分類(`RejectionCategory`)で決める。** Broker 固有の拒否コードは Adapter 内で分類へ写像され、共通モデルへ直接漏れない。分類できない拒否は fail-closed(自動再発行不可)。
5. **手動再発行を先行し、自動再試行は定義のみ**: 初期実装は手動再発行(と `gate_correction`)のみ。自動再試行は `transient` カテゴリ限定・policy 上限つきの契約として本書で定義するが、Live Execution runner が存在するまで実装せず、policy 既定でも無効(上限 0)とする。
6. **`signal_id` の一意性契約は「signal_id 全体一意」から「(signal_id, signal_attempt) 一意 + 線形チェーン制約」へ拡張する**(schema v5・後続 Issue)。同一 signal からの意図しない重複生成の防止は、従来どおり DB 制約 + `BEGIN IMMEDIATE` を最終防衛線として維持する。
7. **再発行 Request は通常の `submit()` 経路をそのまま通る**: Safety Gate の再評価・判定の耐久保存・Submission Journal・`created → submitting` 遷移を含め、再発行専用の送信経路や Gate バイパスは作らない。

## 用語(Broker Order Reissue との区別)

| | Broker Order Reissue(Issue #102) | **Intent Reissue(本書・Issue #95)** |
|---|---|---|
| 何が差し替わるか | 同一 Execution Request 内の **Broker 側注文 ID**(部分約定後の残数量再発行) | **Execution Request そのもの**(新しい注文意図) |
| レコード | 同一行。`superseded_broker_order_ids` chain と history で監査 | **新しい行**。親子関係列で監査 |
| `execution_request_id` | 不変 | **新規発行** |
| `client_order_id` | 不変 | **新規発行**(旧 ID の再利用禁止) |
| 起点 | Broker が応答内で報告する(観測の反映) | BELIAL 側の明示的な判断(運用者または将来の auto policy) |

## 再発行可否の設計判断(検討した代替案)

| 案 | 内容 | 評価 |
|---|---|---|
| A: 一律再発行禁止(現行) | REJECTED を含む全送信試行済み状態を恒久に再発行不可のままにする | 安全側だが、一時的な市場状態・入力不備の修正後に「同じ仮説(signal)」を再発注する正当な運用が構造的に不可能。signal を複製して別 ID を振る回避運用を誘発し、かえって監査性を壊す |
| B: REJECTED → 再送可能状態への遷移追加 | 同一 Request を再送する(terminal 緩和) | **却下**。`client_order_id` 1:1 不変・`mark_submitting()` 一度きり・Journal の entry 不変性という二重発注防止の根幹をすべて壊す。REJECTED の terminal 性は broker 状態機械の公理であり、緩和は Issue #74 以来の全回帰テストと矛盾する |
| C: 明示的な新 intent としての再発行(**採用**) | 新 `execution_request_id` + 新 `client_order_id` の別レコード + 親子関係の永続化 | 既存の不変条件(request 単位の idempotency・terminal 不変・Journal 契約)を**ひとつも緩めない**。重複防止は「signal 単位の一意性」の定義を(signal 全体で 1 件)から(signal × attempt で 1 件 + 線形チェーン)へ精緻化することで維持する |

案 C の採用により、「REJECTED を再発行可能にするか」という問いは「**REJECTED の後に、同じ signal から次の attempt を作成することを、どの条件で許可するか**」に置き換わる。状態機械・既存レコードは一切変化しない。

## 正規化拒否分類(`RejectionCategory`)

Broker の確定拒否(`OrderState.REJECTED`)には現在、監査用の自由文字列 `reject_reason`(REJECTED のとき必須・非空)だけが付く。再発行可否の判断を Broker 固有文字列の解析に依存させないため、**Broker 境界に正規化分類を追加する**(後続 Issue)。

- `RejectionCategory` は `broker/broker_models.py` に定義する **Broker 中立の閉じた enum** とする。各 Adapter(FakeBroker / OANDA Demo Adapter / 将来の実 Adapter)は、自身の native 拒否コードからこの分類への写像を **Adapter 内部に**持つ。native コード・文字列は従来どおり `reject_reason` に verbatim で残る(監査用)が、execution 層の**判断**には使わない。
- `OrderResult` に `rejection_category: RejectionCategory | None` を追加する(REJECTED のとき必須、他状態では None — `reject_reason` と同じ相互制約)。既存の保存済み snapshot(分類フィールドを持たない)は **`unclassified` として読み戻す**(Issue #102 の `superseded_broker_order_ids` 後方互換と同じ「欠落 = 当時の意味」方式)。
- **写像できない・不明な拒否はすべて `unclassified`**(fail-closed): 自動再発行の対象にならない。

| category | 意味(例) | 自動再試行 | 手動再発行 |
|---|---|---|---|
| `transient` | 一時的な市場状態(瞬間的な price bound / 流動性・一時的な取引制限) | **可**(policy 上限内のみ) | 可 |
| `parameter` | 数量・価格・精度など入力内容の不備 | 不可(同一内容の再送は同一結果になるだけ) | 可(**内容を修正した新 intent** として) |
| `account` | 証拠金不足・口座状態による拒否 | 不可 | 可(口座状態の解消後) |
| `instrument` | 銘柄の取引停止・取引不可 | 不可 | 可(解消後) |
| `broker_internal` | Broker 側障害による**確定**拒否 | 不可(障害中の自動連打を禁止) | 可 |
| `unclassified` | 写像不能・旧 snapshot・不明 | **不可**(fail-closed) | 可 |

- 手動再発行が全カテゴリで「可」なのは、**手動の根拠は分類ではなく運用者の明示的判断**だからである(判断の理由は監査列に必須記録。下記)。自動はカテゴリが唯一の根拠なので `transient` に限定する。
- Broker 側障害は多くの場合、確定応答ではなく例外(timeout / 通信失敗)として現れ、その場合は従来どおり **UNKNOWN** になる(再発行の対象外。下記)。`broker_internal` は「Broker が明示的に拒否応答を返した」場合のみ。

### Safety Gate 拒否の扱い(environment-bound と request-bound の区別)

Safety Gate の拒否は Broker 送信**前**に発生し、Request は phase `created` のまま残る(Journal 記録・`mark_submitting()` へ到達しない)。したがって Gate 拒否は `OrderState.REJECTED` にはならず、**同一 Request の `submit()` 再実行が現行仕様で可能**である(再実行時に Gate は全条件を再評価する)。ただしこれで足りるのは、**拒否原因が Request の外にある場合だけ**である。

永続化された `ExecutionRequest` は不変(`frozen=True` で保存され、`submit()` は毎回 `load_execution_request()` で同じ内容を読み直す)であり、**request 内容を更新する store API は存在しない**。したがって拒否原因が request 自身の内容にある場合、再 submit は必ず同じ拒否を再生産する。さらに `signal_id` の一意性が差し替え登録を拒むため、**訂正経路が無ければその signal は恒久的に使用不能になる**(PR #118 Codex レビュー P1)。

| 分類 | 拒否コード | 再 submit で解決するか |
|---|---|---|
| **environment-bound**(市場・口座・時刻・運用状態) | `reference_price_missing` / `reference_price_stale` / `reference_timestamp_future` / `spread_missing` / `spread_limit_exceeded` / `market_closed` / `symbol_not_tradable` / `symbol_position_limit_exceeded` / `account_position_limit_exceeded` / `kill_switch_enabled` / `broker_query_failed` / `state_query_failed` / `decision_persist_failed` / `submission_journal_failed` / `gate_internal_error` | **する**(条件解消後に同一 Request を再 submit) |
| **caller-bound**(呼び出し側の入力・構成) | `market_snapshot_mismatch`(誤った snapshot を渡した)/ `execution_mode_mismatch`(接続先・policy 構成) | **する**(正しい snapshot / 構成で再 submit) |
| **request-bound**(不変の request 内容 × 静的 policy) | `symbol_not_allowed` / `side_not_allowed` / `quantity_limit_exceeded` / `stop_loss_required` / `stop_loss_invalid` / `take_profit_invalid` | **しない**(request を訂正しない限り恒久的に同一結果) |

- `stop_loss_invalid` / `take_profit_invalid` は MARKET のとき基準価格が市場 snapshot(ask / bid)であるため、市場変動で解消しうる。LIMIT のときは基準が `requested_price` = request 内容のみで決まるため恒久的である。**判定は「同一 policy・同一市場条件で再現するか」ではなく「request を変えずに解消しうるか」で行い、曖昧なものは request-bound(訂正経路が必要)として扱う。**
- request-bound 拒否は policy 側の変更(許可銘柄の追加・数量上限の緩和)でも解消しうるが、それは**安全ポリシーの緩和**であり、注文内容の誤りに対する正しい訂正手段ではない。

#### 訂正差し替え(`gate_correction`)

request-bound 拒否のために、**Broker へ一度も到達していない意図に限った差し替え経路**を定義する。これはチェーンモデルの中に `reissue_kind = gate_correction` として収容し、`broker_rejected` と同じ親子関係・一意性・監査契約に従わせる(別モデルを作らない)。

**適格条件(すべて満たす場合のみ。ひとつでも欠ければ fail-closed で拒否)**:

1. 親の `submission_phase = created` かつ `order_state IS NULL` — `Broker.submit_order()` は `created → submitting` 遷移の勝者しか呼べないため、これは「この意図で Broker が呼ばれていない」ことの**構造的証明**である。
2. 親の Submission Journal entry が**存在しないか `not_started`** — Execution DB が消去・再作成されて同じ意図が再 register された場合、store 上の phase は `created` でも Journal 側は `started` / `responded` / `unknown` でありうる(Broker が注文を保持しうる。[Submission Journal](submission_journal.md) の「二重送信防止」)。**この場合の差し替えは禁止**であり、Journal 検査は冗長な二重確認ではなく、この経路の唯一のガードである。
3. `initiated_by = manual` のみ(自動には訂正すべき「判断」が存在しない)。

**親の退役(二重の live intent を作らない)**: 差し替え後に親が送信されてはならない。親行を書き換えるのではなく、`UNIQUE(reissued_from_execution_request_id)` index を使って **`mark_submitting()` が同一トランザクション内で「自分を親とする子行が存在しないこと」を確認**し、存在すれば送信権を与えない(Safety Gate 側の拒否コードは新設の `intent_superseded`)。競合は既存の `BEGIN IMMEDIATE` 直列化で解決する — 差し替えが先に commit すれば `mark_submitting()` が拒否され、`mark_submitting()` が先に commit すれば親の phase が `created` でなくなるので差し替えが拒否される。**どちらの順序でも「送信された親」と「その子」が同時に存在することはない。**

- **検討した代替案**: `submission_phase` に `superseded` 値を追加して親を退役させる案は採らない。SQLite は `ALTER TABLE` で CHECK 制約を変更できず、phase の CHECK 更新には**注文状態を保持する本番テーブルの rebuild** が必要になる(現行の execution store は additive migration のみを採用している)。加えて親行を書き換えないほうが「元 Request の行・履歴・snapshot は不変」という監査上の性質を保てる。
- **Journal 側の後始末**: 差し替えられた親の Journal entry(`not_started`)は、終端 outcome `abandoned` へ遷移させる(**`not_started` からのみ許可**。`started` 以降は Broker 到達の可能性があるため不可)。これは store の commit 後の best-effort であり、失敗しても entry は `open` のまま `list_unresolved()` に残るだけで、二重発注ハザードにはならない。
- 差し替えは Broker へ到達しなかった意図に対する操作なので `RejectionCategory` の対象外である(親に `OrderResult` が存在しない)。適格性の根拠は Gate 判定の耐久記録(`safety_gate_decisions`)であり、`reissue_reason` に運用者判断とともに記録する。
- **crash boundary 3(phase `submitting` / `pending` かつ Journal `not_started`)は対象外**とする。Journal 上は未送信の証明があるが、送信権を獲得済みの意図を差し替えるのは reconciliation の責務であり([Submission Journal](submission_journal.md) の境界表 3)、本経路では fail-closed に拒否する。

### 子 Request のフィールド不変条件

`parameter` 拒否の訂正・`gate_correction` はいずれも「内容を修正した新 intent」を作るため、**どのフィールドが不変でどれが訂正可能かを契約として固定する**(PR #118 Codex レビュー P2)。これが無ければ、strategy / symbol / side を差し替えた別の取引が「同一 signal の次 attempt」として記録され、チェーンの監査性と signal 単位の再試行上限が意味を失う。

| フィールド | 契約 |
|---|---|
| `signal_id` | **不変**(チェーンのキー。定義上同一) |
| `strategy_id` | **不変**(仮説の所有者は変わらない) |
| `symbol` | **不変**(別銘柄は別の取引であり訂正ではない) |
| `side` | **不変**(方向の反転は新しい判断であり訂正ではない) |
| `quantity` | 訂正可。ただし**親以下(減少または同値)のみ** — 増量は訂正ではなく新しい判断 |
| `order_type` / `requested_price` | 訂正可(MARKET ⇔ LIMIT を含む)。両者の整合は従来どおり `ExecutionRequest` の構築時契約が強制する |
| `stop_loss_price` / `take_profit_price` | 訂正可(方向整合は構築時契約が強制) |
| `requested_at_utc` | 子は新規作成時刻。**親より過去は拒否**(時刻の逆行禁止) |
| `execution_request_id` / `client_order_id` | **常に新規発行**(旧値の再利用禁止) |

- 検証は**登録トランザクション内で親行と突き合わせて**行う(親子は同一テーブルにあるため同一 `BEGIN IMMEDIATE` 内で比較できる)。違反は typed error で DB 無変更。
- **`initiated_by = auto` は全注文パラメータが親と同一**でなければならない。自動再試行が注文内容を書き換えることを構造的に禁止する(`transient` は「同じ注文をもう一度」であり、内容の訂正判断は人間の責務)。

## データモデル(schema v5 案・後続 Issue で実装)

`execution_orders` へ追加する列:

| 列 | 型 | 意味 |
|---|---|---|
| `reissued_from_execution_request_id` | TEXT NULL(FK → `execution_orders`) | 親 Request(直前 attempt)。初回 attempt は NULL |
| `signal_attempt` | INTEGER NOT NULL(≥ 1。既存行は 1 として読み戻し) | 同一 signal 内の attempt 番号(1 始まり・連番) |
| `reissue_kind` | TEXT NULL(CHECK: `broker_rejected` / `gate_correction`) | attempt ≥ 2 で必須。親のどの終わり方に対する子か |
| `reissue_initiated_by` | TEXT NULL(CHECK: `manual` / `auto`) | attempt ≥ 2 で必須。誰の判断による再発行か |
| `reissue_reason` | TEXT NULL | attempt ≥ 2 で必須・非空。手動: 運用者の識別子と判断理由。自動: 適用 policy の識別子と根拠カテゴリ |

一意性制約(すべて DB 制約が最終防衛線):

- `execution_request_id` PRIMARY KEY — **不変**(再発行は常に新 ID)
- `client_order_id` NOT NULL UNIQUE — **不変**(再発行は常に新 ID。旧 ID は Broker 側に記録されている可能性があるため**再利用禁止**。Journal 側の UNIQUE も従来どおり独立に強制)
- `UNIQUE(signal_id, signal_attempt)` — 現行の `idx_execution_orders_signal`(signal_id 単独 UNIQUE)を**置換**。「1 signal = 高々 1 件」から「1 signal × 1 attempt = 高々 1 件」へ
- `UNIQUE(reissued_from_execution_request_id)` — **1 つの親は高々 1 回しか再発行できない**(チェーンは線形であって木ではない。SQLite の UNIQUE index は NULL を重複可能として扱うため、初回 attempt 多数と両立する)

アプリケーション層 + 復元時 bundle 検証で強制する不変条件(既存パターンどおり二重防御。SQLite の `ALTER TABLE ADD COLUMN` に載せられない相互制約はこちらが正):

- `signal_attempt = 1` ⇔ `reissued_from_execution_request_id IS NULL` ⇔ reissue 監査列(`reissue_kind` / `reissue_initiated_by` / `reissue_reason`)がすべて NULL
- attempt n(≥ 2)の親は、**同一 `signal_id` の attempt n−1 の行**でなければならない(ID による親子関係が正。時刻では関連付けない)
- **子の request 内容は「子 Request のフィールド不変条件」を満たす**(identity 4 フィールドの一致・数量の非増加・時刻の非逆行)。検証は登録と同一トランザクション内で親行と突き合わせる
- **`reissue_kind` ごとに親の状態要件が異なる**。どちらの要件も再発行登録と同一の `BEGIN IMMEDIATE` トランザクション内で検証する:

| `reissue_kind` | 親の要件 | 根拠 |
|---|---|---|
| `broker_rejected` | `order_state = rejected`(完全 `OrderResult` snapshot と `reject_reason` を保持した確定拒否) | Broker が確定的に拒否した = 注文は存在しない |
| `gate_correction` | `submission_phase = created` かつ `order_state IS NULL`、かつ Journal entry が非存在 / `not_started`(サービス層の事前条件) | Broker が呼ばれていないことの構造的証明 + Journal の独立証明 |

- 上記以外の親状態(`pending` / `accepted` / `partially_filled` / `unknown`)は**どちらの kind でも登録拒否** — **UNKNOWN / SUBMITTING / ACCEPTED など Broker 受理の可能性が残る状態から意図を再発行する経路は存在しない**(Issue #95 の維持必須契約)
- 親が `filled` / `cancelled` の場合も登録拒否(fill を得た意図・取り消した意図の「再発行」は別の業務判断であり、本モデルの対象外。キャンセル後の扱いは Issue #99 の進行を待って別途判断する)
- **`gate_correction` の子を持つ親は送信権を得られない**: `mark_submitting()` は同一トランザクション内で「自分を親とする子行が存在しないこと」を確認し、存在すれば送信権を与えない(Safety Gate 側は `intent_superseded`)。これにより 1 signal に live な intent が 2 つ存在することはない

### チェーンのライフサイクル(状態遷移図)

Request 単体の状態機械は不変(`broker.OrderState` + `submission_phase`)。再発行はその**上位**に「signal ごとの意図チェーン」という線形構造を足すだけである。

```text
signal S(signal_id は仮説の識別子。再発行で変わらない)
  │
  ├─ attempt 1: ExecutionRequest R1(reissued_from = NULL)
  │     created ─┬─ [Safety Gate が request-bound 拒否] → created のまま(Broker 未到達)
  │              │        │
  │              │        ▼  kind = gate_correction(手動のみ。Journal not_started が条件)
  │              │   〔訂正差し替え〕R1 は以後 mark_submitting() を獲得できない
  │              │
  │              └─ submitting → … → REJECTED(terminal・行/履歴/snapshot は以後不変)
  │                                    │  rejection_category = transient / parameter / …
  │                                    ▼  kind = broker_rejected(手動 or 自動 policy)
  │            〔明示的再発行。いずれの kind も 新 execution_request_id / 新 client_order_id〕
  ├─ attempt 2: ExecutionRequest R2(reissued_from = R1, kind / initiated_by / reason 記録)
  │     created → submitting → … → REJECTED
  │                                    ▼
  ├─ attempt 3: …(policy 上限まで)
  │
  └─ チェーン終端条件:
       - いずれかの attempt が FILLED / CANCELLED / (未解決の) UNKNOWN・ACCEPTED 等で終端・継続中
         → その間、次の attempt は作成できない(親の状態要件を満たさないため)
       - attempt 上限到達 → 以後は登録拒否(手動・自動・訂正それぞれ policy で定義)
```

チェーン不変条件: 任意の時点で、1 つの signal に対して **送信可能な(live な)行は高々 1 件**(= チェーン末尾)しか存在しない。これは「親の状態要件を満たすときだけ子を作れる」+「1 親 1 子」+「(signal, attempt) 一意」+「子を持つ親は `mark_submitting()` を獲得できない」の帰結であり、**二重発注防止の signal 単位の保証が再発行導入後も維持される**ことを意味する。

## 再発行の登録・送信フロー

0. **事前条件(`gate_correction` のみ・サービス層)**: 親の Submission Journal entry を読み、非存在または `not_started` であることを確認する。`started` / `responded` / `unknown` なら差し替えを拒否する(Broker が注文を保持しうる)。Journal は別 SQLite ファイルであり execution store のトランザクション内から読めない(repair fence と同じ制約。[Execution Request](execution_request.md))ため、この確認はトランザクション外の事前条件として行い、**トランザクション内の判定は親の `submission_phase = created` が担う**(送信権を獲得していない = Broker 未呼び出しの構造的証明)。両者の間に親が送信された場合は、下記の登録トランザクションが phase 変化を検出して拒否する。
1. **登録**(後続 Issue の store API `create_reissue_execution_order()`(仮称)/ service API): 単一 `BEGIN IMMEDIATE` トランザクション内で、(a) 親行の存在と **`reissue_kind` に応じた状態要件**(`broker_rejected` → `order_state = rejected` / `gate_correction` → phase `created` かつ `order_state IS NULL`)の確認、(b) attempt 連番・チェーン整合・**フィールド不変条件(親行との突き合わせ)**の検証、(c) policy 上限(手動 / 自動 / 訂正それぞれ)の検証、(d) 新行 INSERT を行う。並行する再発行登録の競合は `UNIQUE(reissued_from_…)` / `UNIQUE(signal_id, signal_attempt)` への衝突で敗者が `ExecutionOrderConflictError`(正常系の抑止・行は増えない)になる。既存の作成時 conflict 分類・WARN ログ契約(Issue #76)をそのまま踏襲する。
   - **`gate_correction` commit 後(best-effort)**: 親の Journal entry(`not_started`)を終端 outcome `abandoned` へ遷移させる。失敗しても entry が `open` のまま残るだけで、二重発注ハザードにはならない。
2. **送信**: 登録された再発行 Request は phase `created` の通常の Request であり、**既存の `submit()` をそのまま**通る — Safety Gate の全条件再評価(市場状態・ポジション上限・kill switch を含む)、判定の耐久保存 + read-back、Submission Journal への耐久記録(新 entry)、`created → submitting` 一度きりの遷移獲得。**再発行だから緩和・省略される検査は 1 つもない**。
3. **Journal との関係**: 再発行は新しい `execution_request_id` / `client_order_id` を持つため、Journal 上も**新しい entry**になる(entry の再利用・reclaim は発生しない)。親 attempt の entry は不変のまま残り、`client_order_id` UNIQUE により旧 Broker キーの再利用は Journal 側でも独立に拒否される。

## 自動再試行と手動再発行の責務分離

| | 手動再発行(`broker_rejected`) | 訂正差し替え(`gate_correction`) | 自動再試行(`broker_rejected`) |
|---|---|---|---|
| 初期実装 | **対象**(後続 Issue) | **対象**(後続 Issue) | **対象外**(定義のみ。Live Execution runner 導入後の別 Issue) |
| 親の終わり方 | Broker が確定拒否(REJECTED) | Broker 未到達(phase `created` + Journal `not_started`) | Broker が確定拒否(REJECTED) |
| 許可カテゴリ | 全カテゴリ(運用者判断が根拠) | 該当なし(`OrderResult` が存在しない) | `transient` のみ |
| 注文パラメータの訂正 | 可(不変条件の範囲内) | 可(不変条件の範囲内) | **不可(親と完全同一)** |
| 起点 | 運用者の明示操作(CLI / 運用手順) | 運用者の明示操作 | Live Execution runner の policy 判断 |
| 記録 | `initiated_by = manual` + 運用者識別子・理由(必須) | `initiated_by = manual` + 運用者識別子・理由(必須) | `initiated_by = auto` + policy 識別子・根拠(必須) |
| 回数上限 | policy `max_manual_reissues_per_signal`(既定は小さい有限値。後続 Issue で確定) | policy `max_gate_corrections_per_signal`(同上) | policy `max_auto_reissues_per_signal`(**既定 0 = 無効**) |
| 追加条件 | — | Journal 事前条件(上記)。crash boundary 3 は対象外 | cooldown(連打防止)・kill switch 有効時は不可・対象 signal に未解決の reconciliation discrepancy がないこと |

- 回数の判定は**常に永続化された行から導出**する(該当 signal の MAX(`signal_attempt`) と `reissue_kind` / `initiated_by` の集計)。in-memory カウンタ・プロセス状態には依存しないため、**プロセス再起動・多重起動後も上限と一意性が維持される**。
- 3 つの上限は独立に数える(自動が上限に達しても、運用者の明示判断は別枠。Broker へ到達しなかった訂正は Broker 再試行の予算を消費しない)。**合計 attempt 数にも policy 上限**を置き、無限チェーンを構造的に禁止する。

## 監査・再起動耐性・並行実行

- **監査**: 元 Request の行・状態履歴・anchored snapshot は再発行で一切変更されない(`broker_rejected` は terminal 不変により、`gate_correction` は親行を書き換えない設計により)。親子関係は子側の provenance 列(`reissued_from_…` / `signal_attempt` / `reissue_kind` / `initiated_by` / `reason`)で追跡し、チェーン全体は `signal_id` で列挙できる。「どの意図が・誰の判断で・何回目として・どの終わり方に対して再発行されたか」はすべて DB から復元可能。訂正差し替えの場合、親が受けた Gate 拒否の内容は `safety_gate_decisions` に耐久保存されている。
- **再起動耐性**: すべての不変条件は DB 制約 + トランザクション内検証で強制され、復元時 bundle 検証(既存パターン)にチェーン整合(親子 state・attempt 連番・監査列の相互制約・identity フィールドの一致)を加える。帯域外編集で壊されたチェーンは `ExecutionIntegrityError` で拒否する。
- **並行実行**: 同一 signal への並行再発行・多重起動は、`UNIQUE(reissued_from_…)` と `UNIQUE(signal_id, signal_attempt)` への `BEGIN IMMEDIATE` 内 INSERT 衝突で直列化され、勝者は高々 1 つ。検証(親の状態要件・フィールド不変条件・上限)と INSERT が同一トランザクションなので、TOCTOU 窓はない。**訂正差し替えと親の送信の競合**も同じ直列化で解決する(先に commit した側が勝ち、他方は phase 変化 / 子の存在を見て拒否される。両方が成立することはない)。
- **読み取り API への影響**(後続 Issue): `get_execution_order_by_signal_id()` の「高々 1 件」契約は成立しなくなるため、「チェーン(全 attempt)の列挙」と「最新 attempt の取得」へ契約を再定義する。reconciliation の対象抽出(`list_in_flight_orders()` 等)は attempt ごとに独立の行として自然に動作する(変更不要の見込みだが、実装 Issue で影響を確認する)。

## Reconciliation との整合

- 親の REJECTED が誤りだった場合(内部は REJECTED だが Broker 側に open trade が存在する `internal_closed_broker_open` 型の差分)、再発行は二重建玉を生みうる。このため**自動再試行は「対象 signal のチェーンに未解決 discrepancy がないこと」を前提条件に含める**(fail-closed)。手動再発行は運用者判断だが、実装 Issue で未解決 discrepancy の警告表示を要件に含める。
- REJECTED の確定は常に完全 `OrderResult` snapshot を伴う(`record_order_result` 経由。reconciliation repair が確定させた場合も同じ)。snapshot を持たない「REJECTED らしき状態」から再発行が始まる経路は存在しない。

## 影響しないもの

- `broker.OrderState` / `ORDER_STATE_TRANSITIONS` / `validate_order_state_transition()` / `validate_order_result_update()` — 変更なし(`RejectionCategory` の追加は `OrderResult` のフィールド追加であり、状態機械には触れない)
- UNKNOWN の契約 — 保存・非再送・照会による解決のみ。UNKNOWN の自動再送は引き続き非スコープであり、本モデルは UNKNOWN に**一切適用されない**
- `submission_phase` の値集合(`created` / `submitting` / `submitted`)と phase × `order_state` の CHECK 制約 — 変更なし(親の退役は子行の存在で表現し、phase 値を追加しない。上記「検討した代替案」)
- Safety Gate の**判定基準**(policy 評価・fail-closed 条件・順序)— 変更なし。再発行 Request も同一の Gate・同一の順序を通る
- Shadow Mode / Paper Signal / Paper Execution / Backtest — namespace 独立のまま(Paper / Observation レイヤーは execution を import しない回帰を維持)

### 本設計が変更を要求するもの(後続 Issue の対象)

- `SafetyRejectionCode` へ **`intent_superseded` を追加**(訂正差し替え済みの親を送信しようとした場合の fail-closed 拒否)。既存コードの意味は変えない
- `mark_submitting()` に「自分を親とする子行が存在しないこと」の同一トランザクション内検証を追加(既存の一度きり契約は不変)
- Submission Journal に終端 outcome **`abandoned` を追加**(`not_started` からのみ遷移可)。entry のライフサイクル・reclaim 規則・二重送信ガードそのものは不変
- `OrderResult` への `rejection_category` 追加(#114)、`execution_orders` への 5 列追加と一意性制約の置換(#115)

## 移行方針(v4 → v5・後続 Issue)

- **リリース済み v4 と完全一致した DB のみ**を `init_db()` が migration する(既存方針)。単一トランザクションで: 新 5 列の `ALTER TABLE ADD COLUMN`(`signal_attempt` は `NOT NULL DEFAULT 1` で既存行を attempt 1 として backfill、他 4 列は NULL)→ `idx_execution_orders_signal`(signal 単独 UNIQUE)の DROP → `UNIQUE(signal_id, signal_attempt)` / `UNIQUE(reissued_from_…)` index の CREATE → version bump → 完全再検証 → COMMIT。途中失敗は全 rollback。既存行の削除・書き換えなし(旧 UNIQUE が保証していた既存データに attempt 重複はあり得ないため、index 置換で失敗する既存 DB は存在しない)。
- index の置換を含むため purely additive ではないが、**テーブル rebuild・行の書き換えは行わない**。単一列の CHECK(`signal_attempt >= 1`・`reissue_kind` / `reissue_initiated_by` の値集合)は `ADD COLUMN` に載せられるが、**複数列にまたがる相互制約(attempt ⇔ 親 NULL ⇔ 監査列、identity フィールドの親子一致)はテーブル制約であり `ALTER TABLE` では追加できない**ため、アプリケーション層 + 復元時 bundle 検証が正となる(既存の phase × `order_state` のような DB CHECK による二重防御は、これらの相互制約については得られないことを明示的な割り切りとして受け入れる)。参照 DDL との token 一致検証は v5 参照 schema で更新する。
- **読み取り経路は migration しない**(既存方針。v4 DB の読み取りは `init_db()` への誘導を含む `ExecutionSchemaError`)。dry-run の無副作用保証も不変。
- 旧 snapshot の `rejection_category` 欠落は `unclassified` として読み戻す(broker モデル側の後方互換。上記)。

## 後続 Issue への分割(本 Issue では実装しない)

| Issue | 内容 | 依存 |
|---|---|---|
| #114 | [Broker] `RejectionCategory` の定義と `OrderResult.rejection_category`(REJECTED ⇔ 必須の相互制約・旧 snapshot の `unclassified` 読み戻し・FakeBroker / OANDA Demo Adapter の写像) | なし |
| #115 | [Execution] schema v5(親子関係・attempt / kind 列・一意性制約の置換・v4 → v5 migration・チェーン整合とフィールド不変条件の検証・`mark_submitting()` の子存在検証・読み取り API のチェーン対応) | #114 は不要(独立) |
| #116 | [Execution] 手動再発行 + 訂正差し替えユースケース(登録 API・eligibility 検証・Journal 事前条件と `abandoned`・`intent_superseded` 拒否コード・監査記録・通常 `submit()` 経路への接続・固定シナリオ・未解決 discrepancy の警告) | #114 #115 |
| #117 | [Execution] `transient` 限定の自動再試行 policy(cooldown・上限・kill switch / discrepancy 前提条件。**既定無効**) | #114 #115 #116 + Live Execution runner(未着手) |

## 関連ドキュメント

- [Execution Request / Order Persistence](execution_request.md) — 現行の ID 設計・idempotency 契約(本書が拡張する対象)
- [Safety Gate / Live Order Submission](safety_gate.md) — 再発行 Request が通る唯一の送信経路
- [Submission Journal](submission_journal.md) — 独立した第二の二重送信ガード
- [Broker Interface / FakeBroker](broker_interface.md) — `OrderState` 状態機械と Broker Order Reissue(Issue #102)
- [Broker Reconciliation](broker_reconciliation.md) — 差分検出・修復と再発行前提条件の関係
