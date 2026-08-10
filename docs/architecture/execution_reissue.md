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

| 分類 | 拒否コード | 解決経路 |
|---|---|---|
| **environment-bound**(市場・口座・時刻・運用状態) | `reference_price_missing` / `reference_price_stale` / `reference_timestamp_future` / `spread_missing` / `spread_limit_exceeded` / `market_closed` / `symbol_not_tradable` / `symbol_position_limit_exceeded` / `account_position_limit_exceeded` / `kill_switch_enabled` / `broker_query_failed` / `state_query_failed` / `decision_persist_failed` / `submission_journal_failed` / `gate_internal_error` | 条件解消後に**同一 Request を再 submit** |
| **caller-bound**(呼び出し側の入力・構成) | `market_snapshot_mismatch`(誤った snapshot を渡した)/ `execution_mode_mismatch`(接続先・policy 構成) | 正しい snapshot / 構成で**同一 Request を再 submit** |
| **policy-bound**(注文内容は signal に忠実。禁止しているのは policy) | `symbol_not_allowed` / `side_not_allowed` | **訂正不可・再発行の対象外**。明示的な policy 変更(監査対象の安全判断)後に同一 Request を再 submit するか、その signal を執行しない |
| **request-bound(訂正可能)**(注文内容の不備。identity を変えずに直せる) | `quantity_limit_exceeded` / `stop_loss_required` / `stop_loss_invalid` / `take_profit_invalid` | **訂正差し替え**(`gate_correction`。下記)。policy 側の緩和でも解消しうるが、それは安全ポリシーの変更であって注文内容の訂正ではない |

- **`symbol_not_allowed` / `side_not_allowed` を訂正対象にしない理由**(PR #118 Codex レビュー r2 P1): Execution Request の `symbol` / `side` は source signal(仮説)そのものに由来する。これらを書き換えた注文は、**どの signal も支持していない別の取引**になる。「XAUUSD の買いは許可されていない」への正しい応答は、EURUSD や売りへの差し替えではなく、**policy を変える(明示的な安全判断)か、その signal を執行しないか**の二択である。したがって identity フィールド(`symbol` / `side`)は子でも不変のまま維持し、この 2 コードは訂正差し替えの適格拒否コードから除外する。policy 変更後は Request が不変のまま phase `created` で残っているため、**同一 Request の再 submit がそのまま解決経路になる**(Gate は毎回全条件を再評価する)。
- **設計上の閉包条件**: 訂正差し替えが受け付ける拒否コードの集合は、**「子 Request のフィールド不変条件で訂正可能なフィールドだけで解消できる拒否コード」と厳密に一致**していなければならない。不変フィールドでしか解消できないコードを訂正対象に含めると、訂正しても同じ拒否が再生産される(この閉包条件が破れていたのが r2 P1 の指摘である)。後続 Issue でコード集合を変更する場合は、この一致を必ず維持する。
- `stop_loss_invalid` / `take_profit_invalid` は MARKET のとき基準価格が市場 snapshot(ask / bid)であるため市場変動でも解消しうるが、LIMIT のときは基準が `requested_price` = request 内容のみで決まるため恒久的である。どちらも SL / TP フィールドの訂正で解消できるため訂正可能に分類する。

#### 訂正差し替え(`gate_correction`)

request-bound(訂正可能)拒否のために、**Broker へ一度も到達していない意図に限った差し替え経路**を定義する。これはチェーンモデルの中に `reissue_kind = gate_correction` として収容し、`broker_rejected` と同じ親子関係・一意性・監査契約に従わせる(別モデルを作らない)。

**適格条件(すべて満たす場合のみ。ひとつでも欠ければ fail-closed で拒否)**:

1. **親の最新 Gate 判定が、現行 policy の下での訂正可能な拒否である**(PR #118 Codex レビュー r2 P2 / r5 P2): `safety_gate_decisions` に記録された親の**最新の判定**(最大 `decision_id`)が (a) `allowed = false` であり、(b) **訂正可能な request-bound コードを 1 件以上含み**、(c) その判定の **`policy_id` が現在適用中の `policy_id` と一致する**こと。参照した `decision_id` は子行に永続化する(下記データモデル)。判定の読み取りは登録の critical section 内で**最後に再読**する(下記「判定読み取りの競合」)。
   - **なぜ「過去のどこかに訂正可能な拒否があればよい」では不十分か**(r5 P2): request-bound の判定は request 単体ではなく **request × policy** の関数である。request が不変でも、policy A で `quantity_limit_exceeded` が出た後に policy B が上限を緩和すれば、最新の拒否は `market_closed` だけ、という状態がありうる。この場合その注文内容に訂正すべき不備は無く、差し替え(= 親の退役)を正当化できない。したがって**最新判定そのものが訂正可能であること**を要求し、さらに `policy_id` を突き合わせて**古い policy 下の証拠を根拠にしない**。
2. 親の `submission_phase = created` かつ `order_state IS NULL` — `Broker.submit_order()` は `created → submitting` 遷移の勝者しか呼べないため、これは「この意図で Broker が呼ばれていない」ことの**構造的証明**である。
3. 親の Submission Journal entry が**存在しないか `not_started`** — Execution DB が消去・再作成されて同じ意図が再 register された場合、store 上の phase は `created` でも Journal 側は `started` / `responded` / `unknown` でありうる(Broker が注文を保持しうる。[Submission Journal](submission_journal.md) の「二重送信防止」)。**この場合の差し替えは禁止**であり、Journal 検査は冗長な二重確認ではなく、この経路の唯一のガードである。
4. `initiated_by = manual` のみ(自動には訂正すべき「判断」が存在しない)。

最新が許可(送信直前で別要因が失敗した等)なら訂正の対象ではなく、同一 Request の再 submit が正しい経路である。最新が拒否でも訂正可能コードを含まない・古い policy 下の判定であれば、訂正すべき不備が現在も存在するとは言えないため同様に拒否する。`safety_gate_decisions` は execution store と別ファイルであり同一トランザクション内から読めないため、条件 1 と 3 は**サービス層の事前条件**、条件 2 は**登録トランザクション内の判定**として実施する(下記フロー)。

##### 判定読み取りの競合(PR #118 Codex レビュー r3 P2)

親が `created` のまま並行 `submit()` が走ると、条件 1 の読み取りと子行の commit の間に新しい判定が追記されうる。**厳密な「判定を進ませない fence」は採用しない**: それには submit() が Gate 評価(内部で Broker の positions / account 照会を行う)を跨いで execution store の書き込みロックを保持する必要があり、「外部 Broker 呼び出し中に SQLite の書き込みトランザクションを保持しない」という既存契約([Execution Request](execution_request.md))に反する。代わりに以下で閉じる。

- **窓の縮小**: 条件 1 の判定読み取りは、登録の `BEGIN IMMEDIATE` critical section に入った**最後**に再実行する。
- **安全性(二重発注が起きないこと)**: 条件 1 を無効化しうる判定(許可・非訂正可能な拒否・policy 変更後の判定)のうち、**送信に至りうるのは許可判定だけ**である。許可判定が出た submit は Journal 記録 → `created → submitting` 遷移へ進むため、その遷移獲得と子行 INSERT は execution store の同一直列化で衝突し、**「親が送信される」か「子が登録される」かのどちらか一方しか成立しない**(子が先なら submit は `intent_superseded`、submit が先なら登録は phase 検査で失敗)。どの interleaving でも二重発注は生じない。
- **残余(監査メタデータのみ)**: 並行して新しい判定が記録され、それが条件 1 を満たさない種類だった場合、子行の `reissue_source_decision_id` は最新判定ではなくなる。これは**送信結果に影響しない監査上のズレ**であり(子はいずれにせよ通常の Gate を通り、現在の policy で再評価される)、記録から機械的に検出できる(親は `created` のまま子を持ち、当該判定に対応する Journal entry や phase 変化が存在しない)。登録時の再読で検出した場合は WARN を残す。
- **検討して見送った案**: 最新 `decision_id` を `execution_orders` 側へ mirror し(repair fence mirror と同じ方式)、登録トランザクション内で CAS する案。採用すると**すべての Gate 拒否が execution store への耐久書き込みを伴う**ことになり、純粋な評価だった拒否経路に新たな失敗モード(execution DB 不調で拒否が記録できない)が持ち込まれる。証拠の絶対的な鮮度が必要になった場合に #116 で再検討する。

**親の退役(二重の live intent を作らない)**: 差し替え後に親が送信されてはならない。親行を書き換えるのではなく、`UNIQUE(reissued_from_execution_request_id)` index を使って **`mark_submitting()` が同一トランザクション内で「自分を親とする子行が存在しないこと」を確認**し、存在すれば送信権を与えない(Safety Gate 側の拒否コードは新設の `intent_superseded`)。競合は既存の `BEGIN IMMEDIATE` 直列化で解決する — 差し替えが先に commit すれば `mark_submitting()` が拒否され、`mark_submitting()` が先に commit すれば親の phase が `created` でなくなるので差し替えが拒否される。**どちらの順序でも「送信された親」と「その子」が同時に存在することはない。**

- **検討した代替案**: `submission_phase` に `superseded` 値を追加して親を退役させる案は採らない。SQLite は `ALTER TABLE` で CHECK 制約を変更できず、phase の CHECK 更新には**注文状態を保持する本番テーブルの rebuild** が必要になる(現行の execution store は additive migration のみを採用している)。加えて親行を書き換えないほうが「元 Request の行・履歴・snapshot は不変」という監査上の性質を保てる。
- **Journal 側の後始末**: 差し替えられた親の Journal entry(`not_started`)は、終端 outcome `abandoned` へ遷移させる(**`not_started` からのみ許可**。`started` 以降は Broker 到達の可能性があるため不可)。これは store の commit 後の best-effort であり、失敗しても entry は `open` のまま `list_unresolved()` に残るだけで、二重発注ハザードにはならない。
- 差し替えは Broker へ到達しなかった意図に対する操作なので `RejectionCategory` の対象外である(親に `OrderResult` が存在しない)。適格性の根拠は Gate 判定の耐久記録(`safety_gate_decisions`)であり、**その `decision_id` を子行に保存**して機械的に追跡可能にする(自由文の `reissue_reason` に運用者判断を書くだけにしない。[Issue 運用ルール](../development/issues-guideline.md)の「ID による関連付け」に従う)。`safety_store.list_for_request_with_ids()` が既にこの ID を返すため、参照経路は実装済みである。
- **crash boundary 3(phase `submitting` / `pending` かつ Journal `not_started`)は対象外**とする。Journal 上は未送信の証明があるが、送信権を獲得済みの意図を差し替えるのは reconciliation の責務であり([Submission Journal](submission_journal.md) の境界表 3)、本経路では fail-closed に拒否する。

### 子 Request のフィールド不変条件

`parameter` 拒否の訂正・`gate_correction` はいずれも「内容を修正した新 intent」を作るため、**どのフィールドが不変でどれが訂正可能かを契約として固定する**(PR #118 Codex レビュー P2)。これが無ければ、strategy / symbol / side を差し替えた別の取引が「同一 signal の次 attempt」として記録され、チェーンの監査性と signal 単位の再試行上限が意味を失う。

| フィールド | 契約 |
|---|---|
| `signal_id` | **不変**(チェーンのキー。定義上同一) |
| `strategy_id` | **不変**(仮説の所有者は変わらない) |
| `symbol` | **不変**(別銘柄は別の取引であり訂正ではない。したがって `symbol_not_allowed` は訂正差し替えの対象外 = policy-bound) |
| `side` | **不変**(方向の反転は新しい判断であり訂正ではない。したがって `side_not_allowed` は訂正差し替えの対象外 = policy-bound) |
| `quantity` | 訂正可。既定は**親以下(減少または同値)のみ** — 増量は訂正ではなく新しい判断。**例外**: 親が `broker_rejected` かつ `rejection_category = parameter` の場合に限り、policy 上限内での**増量を許可**する(下記) |
| `order_type` / `requested_price` | 訂正可(MARKET ⇔ LIMIT を含む)。両者の整合は従来どおり `ExecutionRequest` の構築時契約が強制する |
| `stop_loss_price` / `take_profit_price` | 訂正可(方向整合は構築時契約が強制) |
| `requested_at_utc` | 子は新規作成時刻。**親より過去は拒否**(時刻の逆行禁止) |
| `execution_request_id` / `client_order_id` | **常に新規発行**(旧値の再利用禁止) |

- 検証は**登録トランザクション内で親行と突き合わせて**行う(親子は同一テーブルにあるため同一 `BEGIN IMMEDIATE` 内で比較できる)。違反は typed error で DB 無変更。
- **`initiated_by = auto` は全注文パラメータが親と同一**でなければならない。自動再試行が注文内容を書き換えることを構造的に禁止する(`transient` は「同じ注文をもう一度」であり、内容の訂正判断は人間の責務)。

#### 数量増加が必要な `parameter` 拒否(PR #118 Codex レビュー r3 P2)

Broker は「最小取引数量を下回る」「数量の刻み(increment)が不正」といった拒否も `parameter` に分類しうる。これらの訂正には**数量を増やす**必要があり、一律の非増加ルールでは訂正不能になる(カテゴリ表は `parameter` を手動訂正可能と定めているため、この 2 つが矛盾していた)。

- 親が `broker_rejected` かつ `rejection_category = parameter` の場合に限り、**policy 上限内での増量を許可**する(手動のみ。理由の記録必須)。
- 増量の安全性は「非増加ルール」ではなく **Safety Gate が担保**する: 子は通常どおり `submit()` で Gate を通り、`max_order_quantity` 超過は `quantity_limit_exceeded` として拒否される。非増加ルールの本来の目的は、**再試行に見せかけた裁量的な増し玉**の防止であり、それは「`parameter` 拒否に限る + policy 上限 + 監査必須」で維持できる。
- 上記以外(`gate_correction`・`transient` / `account` / `instrument` / `broker_internal` / `unclassified` の再発行・自動再試行)は**非増加のまま**である。特に `gate_correction` の `quantity_limit_exceeded` は「大きすぎる」ことが不備なので、増量は定義上あり得ない。
- policy 上限の具体値(親数量に対する比率上限か絶対上限か)は #116 で確定する。

## データモデル(schema v5 案・後続 Issue で実装)

`execution_orders` へ追加する列:

| 列 | 型 | 意味 |
|---|---|---|
| `reissued_from_execution_request_id` | TEXT NULL(FK → `execution_orders`) | 親 Request(直前 attempt)。初回 attempt は NULL |
| `signal_attempt` | INTEGER NOT NULL(≥ 1。既存行は 1 として読み戻し) | 同一 signal 内の attempt 番号(1 始まり・連番) |
| `reissue_kind` | TEXT NULL(CHECK: `broker_rejected` / `gate_correction`) | attempt ≥ 2 で必須。親のどの終わり方に対する子か |
| `reissue_initiated_by` | TEXT NULL(CHECK: `manual` / `auto`) | attempt ≥ 2 で必須。誰の判断による再発行か |
| `reissue_reason` | TEXT NULL | attempt ≥ 2 で必須・非空。手動: 運用者の識別子と判断理由。自動: 適用 policy の識別子と根拠カテゴリ |
| `reissue_source_decision_id` | INTEGER NULL | **`gate_correction` で必須**(それ以外では NULL)。適格性の根拠とした `safety_gate_decisions.decision_id`。別 DB ファイルのため FK は張れず**参照値**として保持する(repair fence mirror と同じ割り切り) |
| `reissue_clearance_run_id` | TEXT NULL | attempt ≥ 2 で必須。登録時に根拠とした reconciliation run の `run_id`(**参照値**)。送信可否の判定は下記 mirror が正で、この列は監査用の provenance |

`execution_reissue_clearances`(WITHOUT ROWID。**reconciliation clearance mirror**):

| 列 | 意味 |
|---|---|
| `signal_id` TEXT PRIMARY KEY | チェーン単位で 1 行 |
| `sequence` INTEGER NOT NULL(≥ 1) | **単調増加の観測世代**。suspend が `保存値 + 1` を採番し、publish はこの値と `run_id` の一致を CAS 条件とする(古い run による上書きを防ぐ) |
| `run_id` TEXT NOT NULL | この世代を保持している reconciliation run |
| `state` TEXT NOT NULL(CHECK: `suspended` / `clear` / `revoked`) | `suspended` = 観測中(送信不可)、`clear` = 要件を満たす無差分観測、`revoked` = 差分あり。**送信を許可するのは `clear` のみ** |
| `position_scope_verified` INTEGER NOT NULL(0 / 1) | その run が position スコープを**列挙エラー・対象シンボルの除外なしで**実施したか(`clear` で 1 必須) |
| `observed_at_utc` / `updated_at_utc` TEXT NOT NULL | 鮮度窓の判定に使う観測時刻と更新時刻(UTC) |

reconciliation DB を execution store のトランザクション内から読むことはできない(ファイルを跨げない)ため、**repair fence と同じく execution DB 側へ mirror を置き、送信権の付与と同じトランザクションで照合する**。書き込むのは reconciliation 側だが、対象はこの mirror テーブルのみであり、注文行・履歴には触れない。

一意性制約(すべて DB 制約が最終防衛線):

- `execution_request_id` PRIMARY KEY — **不変**(再発行は常に新 ID)
- `client_order_id` NOT NULL UNIQUE — **不変**(再発行は常に新 ID。旧 ID は Broker 側に記録されている可能性があるため**再利用禁止**。Journal 側の UNIQUE も従来どおり独立に強制)
- `UNIQUE(signal_id, signal_attempt)` — 現行の `idx_execution_orders_signal`(signal_id 単独 UNIQUE)を**置換**。「1 signal = 高々 1 件」から「1 signal × 1 attempt = 高々 1 件」へ
- `UNIQUE(reissued_from_execution_request_id)` — **1 つの親は高々 1 回しか再発行できない**(チェーンは線形であって木ではない。SQLite の UNIQUE index は NULL を重複可能として扱うため、初回 attempt 多数と両立する)

アプリケーション層 + 復元時 bundle 検証で強制する不変条件(既存パターンどおり二重防御。SQLite の `ALTER TABLE ADD COLUMN` に載せられない相互制約はこちらが正):

- `signal_attempt = 1` ⇔ `reissued_from_execution_request_id IS NULL` ⇔ reissue 監査列(`reissue_kind` / `reissue_initiated_by` / `reissue_reason`)がすべて NULL
- `reissue_source_decision_id IS NOT NULL` ⇔ `reissue_kind = gate_correction`(訂正の根拠は必ず記録され、訂正以外は根拠を持たない)
- `reissue_clearance_run_id IS NOT NULL` ⇔ `signal_attempt ≥ 2`(再発行は必ず無差分観測を根拠に持つ)
- attempt n(≥ 2)の親は、**同一 `signal_id` の attempt n−1 の行**でなければならない(ID による親子関係が正。時刻では関連付けない)
- **子の request 内容は「子 Request のフィールド不変条件」を満たす**(identity 4 フィールドの一致・数量の非増加・時刻の非逆行)。検証は登録と同一トランザクション内で親行と突き合わせる
- **`reissue_kind` ごとに親の状態要件が異なる**。どちらの要件も再発行登録と同一の `BEGIN IMMEDIATE` トランザクション内で検証する:

| `reissue_kind` | 親の要件 | 根拠 |
|---|---|---|
| `broker_rejected` | `order_state = rejected`(完全 `OrderResult` snapshot と `reject_reason` を保持した確定拒否) | Broker が確定的に拒否した = 注文は存在しない |
| `gate_correction` | `submission_phase = created` かつ `order_state IS NULL`(トランザクション内)、かつ **最新 Gate 判定が訂正可能コードを含む拒否**・Journal entry が非存在 / `not_started`(サービス層の事前条件) | Broker が呼ばれていないことの構造的証明 + Journal の独立証明 + 訂正であることの根拠 |

- 上記以外の親状態(`pending` / `accepted` / `partially_filled` / `unknown`)は**どちらの kind でも登録拒否** — **UNKNOWN / SUBMITTING / ACCEPTED など Broker 受理の可能性が残る状態から意図を再発行する経路は存在しない**(Issue #95 の維持必須契約)
- 親が `filled` / `cancelled` の場合も登録拒否(fill を得た意図・取り消した意図の「再発行」は別の業務判断であり、本モデルの対象外。キャンセル後の扱いは Issue #99 の進行を待って別途判断する)
- **`gate_correction` の子を持つ親は送信権を得られない**: `mark_submitting()` は同一トランザクション内で「自分を親とする子行が存在しないこと」を確認し、存在すれば送信権を与えない(Safety Gate 側は `intent_superseded`)。これにより 1 signal に live な intent が 2 つ存在することはない
- **再発行の子は有効な clearance がなければ送信権を得られない**: `mark_submitting()` は同一トランザクション内で clearance mirror(`clear` / position スコープ検証済み / 鮮度窓内)を照合する(上記「登録から送信までの fencing」)

### チェーンのライフサイクル(状態遷移図)

Request 単体の状態機械は不変(`broker.OrderState` + `submission_phase`)。再発行はその**上位**に「signal ごとの意図チェーン」という線形構造を足すだけである。

```text
signal S(signal_id は仮説の識別子。再発行で変わらない)
  │
  ├─ attempt 1: ExecutionRequest R1(reissued_from = NULL)
  │     created ─┬─ [Safety Gate が request-bound(訂正可能)拒否] → created のまま(Broker 未到達)
  │              │        │   拒否判定は safety_gate_decisions へ耐久保存(訂正の根拠)
  │              │        ▼  kind = gate_correction(手動のみ。最新拒否判定 + Journal not_started が条件)
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

0. **事前条件(サービス層)**: (i) **全 kind 共通** — 有効な clearance(上記 5 要件)を reconciliation DB で確認し、その `run_id` を子行へ渡す。**無ければ拒否**(記録の不在を無差分と読まない)。送信可否の最終判定は `mark_submitting()` が mirror に対して行う。(ii) `gate_correction` のみ — 親の**最新** Gate 判定が現行 `policy_id` の下での訂正可能な拒否であることを確認し、`decision_id` を子行へ渡す(判定は critical section 内で最後に再読)。(iii) `gate_correction` のみ — 親の Submission Journal entry が非存在または `not_started` であることを確認する。`started` / `responded` / `unknown` なら差し替えを拒否する(Broker が注文を保持しうる)。Journal は別 SQLite ファイルであり execution store のトランザクション内から読めない(repair fence と同じ制約。[Execution Request](execution_request.md))ため、この確認はトランザクション外の事前条件として行い、**トランザクション内の判定は親の `submission_phase = created` が担う**(送信権を獲得していない = Broker 未呼び出しの構造的証明)。両者の間に親が送信された場合は、下記の登録トランザクションが phase 変化を検出して拒否する。
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
| 注文パラメータの訂正 | 可(不変条件の範囲内。数量は非増加。ただし `parameter` 拒否のみ policy 上限内の増量可) | 可(不変条件の範囲内。数量は非増加) | **不可(親と完全同一)** |
| 起点 | 運用者の明示操作(CLI / 運用手順) | 運用者の明示操作 | Live Execution runner の policy 判断 |
| 記録 | `initiated_by = manual` + 運用者識別子・理由(必須) | `initiated_by = manual` + 運用者識別子・理由(必須) | `initiated_by = auto` + policy 識別子・根拠(必須) |
| 回数上限 | policy `max_manual_reissues_per_signal`(既定は小さい有限値。後続 Issue で確定) | policy `max_gate_corrections_per_signal`(同上) | policy `max_auto_reissues_per_signal`(**既定 0 = 無効**) |
| 追加条件 | — | Gate 判定 / Journal 事前条件(上記)。crash boundary 3 は対象外 | cooldown(連打防止)・kill switch 有効時は不可 |

**全経路に共通する前提**: 有効な **reissue clearance**(position スコープを含む run による無差分の積極的証拠・鮮度窓内)が必要であり、登録時だけでなく**送信時にも mirror で再検証**される(下記「Reconciliation との整合」。自動だけでなく手動・訂正も対象)。

- 回数の判定は**常に永続化された行から導出**する(該当 signal の MAX(`signal_attempt`) と `reissue_kind` / `initiated_by` の集計)。in-memory カウンタ・プロセス状態には依存しないため、**プロセス再起動・多重起動後も上限と一意性が維持される**。
- 3 つの上限は独立に数える(自動が上限に達しても、運用者の明示判断は別枠。Broker へ到達しなかった訂正は Broker 再試行の予算を消費しない)。**合計 attempt 数にも policy 上限**を置き、無限チェーンを構造的に禁止する。

## 監査・再起動耐性・並行実行

- **監査**: 元 Request の行・状態履歴・anchored snapshot は再発行で一切変更されない(`broker_rejected` は terminal 不変により、`gate_correction` は親行を書き換えない設計により)。親子関係は子側の provenance 列(`reissued_from_…` / `signal_attempt` / `reissue_kind` / `initiated_by` / `reason` / `source_decision_id`)で追跡し、チェーン全体は `signal_id` で列挙できる。「どの意図が・誰の判断で・何回目として・どの終わり方に対して再発行されたか」はすべて DB から復元可能。訂正差し替えの場合、根拠となった Gate 拒否判定は `reissue_source_decision_id` から `safety_gate_decisions` の該当行(拒否コード・policy_id・判定時刻)へ**ID で辿れる**(適格条件 1 によりその判定の存在が保証されている)。
- **再起動耐性**: すべての不変条件は DB 制約 + トランザクション内検証で強制され、復元時 bundle 検証(既存パターン)にチェーン整合(親子 state・attempt 連番・監査列の相互制約・identity フィールドの一致)を加える。帯域外編集で壊されたチェーンは `ExecutionIntegrityError` で拒否する。
- **並行実行**: 同一 signal への並行再発行・多重起動は、`UNIQUE(reissued_from_…)` と `UNIQUE(signal_id, signal_attempt)` への `BEGIN IMMEDIATE` 内 INSERT 衝突で直列化され、勝者は高々 1 つ。検証(親の状態要件・フィールド不変条件・上限)と INSERT が同一トランザクションなので、TOCTOU 窓はない。**訂正差し替えと親の送信の競合**も同じ直列化で解決する(先に commit した側が勝ち、他方は phase 変化 / 子の存在を見て拒否される。両方が成立することはない)。
- **読み取り API への影響**(後続 Issue): `get_execution_order_by_signal_id()` の「高々 1 件」契約は成立しなくなるため、「チェーン(全 attempt)の列挙」と「最新 attempt の取得」へ契約を再定義する。reconciliation の対象抽出(`list_in_flight_orders()` 等)は attempt ごとに独立の行として自然に動作する(変更不要の見込みだが、実装 Issue で影響を確認する)。

## Reconciliation との整合

親の REJECTED が誤りだった場合(内部は REJECTED だが Broker 側に open trade が存在する `internal_closed_broker_open`)、再発行は**二重建玉**を生みうる。この差分種別は resolution category が **`manual_review` 固定で自動修復の対象外**である([Broker Reconciliation](broker_reconciliation.md))ため、放置したまま再発行すると本モデルが防ごうとしているハザードそのものが発生する。運用者への警告表示だけで手動再発行を許可する設計は採らない(警告は助言であって二重建玉を防がない)。

### 無差分の積極的証拠(reissue clearance)

**「未解決 discrepancy が記録されていないこと」を条件にはしない**(PR #118 Codex レビュー r4 P1 × 2)。記録の不在は無差分の証明ではないためである。

- reconciliation は**連続監視ではなく on-demand の観測**であり、一度も走っていなければ差分は当然記録されていない。
- 後続 run が差分を報告しなくても、その run が **order スコープのみ**(`include_position_scope` の既定は **False**)であれば position 側は照合されていない。
- 列挙が失敗した run・fail-safe で除外されたシンボルも「見ていない」のであって「差分なし」ではない(`PositionReconciliationSummary` は両者を区別するために存在する)。

したがって条件を反転し、**全 kind 共通の前提として「有効な clearance(無差分の積極的証拠)」を要求**する(fail-closed)。clearance の要件はすべて満たされなければならない:

1. 対象 signal のチェーンに属する**全 `execution_request_id` を照合対象に含む** reconciliation run であること。
2. その run が **order スコープと position スコープの両方**を実施していること(`include_position_scope = True`。既定の order-only run は clearance にならない)。
3. その run の position summary に**列挙エラーが無く**(`internal_exposure_error` / `position_listing_error` / `trade_listing_error` / `closed_trade_listing_error` がすべて NULL)、対象 signal の symbol が `excluded_symbols` に**含まれない**こと。
4. その run がチェーンについて **discrepancy を 1 件も報告していない**こと(order 系・position 系のいずれも)。
5. policy の**鮮度窓内**であること(`max_reconciliation_clearance_age`。具体値は #116 で確定)。

`gate_correction` の親は Broker へ到達していないため通常は自明に clean だが、規則は同一に適用する(fail-closed)。

### 登録から送信までの fencing

clearance を事前条件として読むだけでは、**読み取り後に discrepancy が commit され、その後に子が登録・送信される**窓が残る(reconciliation DB は別ファイルであり、execution DB の `BEGIN IMMEDIATE` では守れない。r4 P1)。repair fence と同じ **mirror 方式(案 A)** を採るが、**観測を commit してから mirror を更新する「後追い」方式は採らない**(r5 P1): その方式では、差分を記録した run の commit と mirror 更新の間に `mark_submitting()` が古い `clear` を受理でき、mirror 更新に失敗すればそのまま送信可能な状態が残ってしまう。

**書き込み先行(write-ahead)の invalidation** で閉じる。reconciliation 側の順序は次で固定する。

1. **観測の前に mirror を `suspended` にする**(Broker 照会を発行する前)。対象は、その run が照合するチェーンの `signal_id`。これは execution DB への durable な書き込みであり、以後 `mark_submitting()` は再発行の子へ送信権を与えない。
2. **この suspend に失敗した run は、そのチェーンの観測を行わない**(fail-closed)。したがって **「commit 済みの観測が存在するのに mirror がそれを知らない」状態は原理的に発生しない** — mirror が `clear` を示すことは「未反映の観測が存在しない」ことを含意する。
3. 観測を reconciliation DB へ commit する。
4. **commit 後に mirror を publish する**: 差分ゼロかつ clearance 要件(スコープ・列挙エラー・除外シンボル)を満たすなら `clear`、差分を 1 件でも記録したなら `revoked`。**publish に失敗した場合、mirror は `suspended` のまま残る = ブロック継続**(失敗が送信可能状態へ化けない)。

**単調な観測順序による直列化**(r5 P1): mirror は `sequence`(単調増加の観測世代)を持ち、更新は CAS で守る。

- suspend は execution DB の `BEGIN IMMEDIATE` 内で `sequence = 保存値 + 1` を採番し、その値と run identity を mirror へ書く(採番も照合も同一トランザクション)。
- publish は **自分が採番した `sequence` と run identity が mirror の保存値と一致するときだけ**成功する。より新しい run が既に suspend していれば保存値は進んでいるため、**古い run(や遅延した再試行)は publish に失敗し、`revoked` を `clear` で上書きできない**。
- これは repair fence の generation / takeover identity の CAS と同じ規則であり、新しい規律を導入していない。

**`mark_submitting()` の照合**: 対象が再発行の子である場合、同一トランザクション内で mirror が `clear`・position スコープ検証済み・鮮度窓内であることを確認し、満たさなければ**送信権を与えない**(`suspended` / `revoked` / 行なし / 鮮度切れはいずれも拒否。Safety Gate 側の拒否コードは `intent_superseded` と同様に #116 で定義)。これにより clearance は登録時点だけでなく**送信の瞬間まで**有効でなければならない。

reconciliation が書き込むのは mirror テーブルのみで、注文行・履歴には触れない(既存の「修復の書き込みは `record_order_result` だけ」という契約は不変)。登録時に参照した run は `reissue_clearance_run_id` として子行に永続化し、監査で「どの観測を根拠に発行したか」を ID で辿れるようにする。送信時の権限判定は mirror(より新しい clean run による refresh を含む)が正である。

**残余(明示的な境界)**: 上記の write-ahead + CAS により、「**BELIAL が観測した事実**が mirror へ反映されないまま送信される」経路は無くなる。一方、最後の clean 観測から送信までの間に **Broker 側で新たに状態が変わる**可能性は、Broker 側のロックを持たない以上どんな fence でも排除できない。これは Safety Gate が市場データに対して `max_reference_age` で受け入れているのと**同じ種類の境界**であり、鮮度窓(要件 5)で bound する。この残余は「観測の反映漏れ」ではなく「観測後の外部変化」であり、性質が異なる。

**既知の制約**: reconciliation には現在、`manual_review` の差分を「確認済みとして明示的に閉じる」監査付き API が存在しない。ただし clearance モデルでは**差分を閉じる操作は不要**である — 必要なのは、原因を是正したうえで **position スコープを含む run を再実行し、無差分を積極的に観測する**ことだからである。再発行側に bypass は設けない。

REJECTED の確定は常に完全 `OrderResult` snapshot を伴う(`record_order_result` 経由。reconciliation repair が確定させた場合も同じ)。snapshot を持たない「REJECTED らしき状態」から再発行が始まる経路は存在しない。

## 影響しないもの

- `broker.OrderState` / `ORDER_STATE_TRANSITIONS` / `validate_order_state_transition()` / `validate_order_result_update()` — 変更なし(`RejectionCategory` の追加は `OrderResult` のフィールド追加であり、状態機械には触れない)
- UNKNOWN の契約 — 保存・非再送・照会による解決のみ。UNKNOWN の自動再送は引き続き非スコープであり、本モデルは UNKNOWN に**一切適用されない**
- `submission_phase` の値集合(`created` / `submitting` / `submitted`)と phase × `order_state` の CHECK 制約 — 変更なし(親の退役は子行の存在で表現し、phase 値を追加しない。上記「検討した代替案」)
- Safety Gate の**判定基準**(policy 評価・fail-closed 条件・順序)— 変更なし。再発行 Request も同一の Gate・同一の順序を通る
- Shadow Mode / Paper Signal / Paper Execution / Backtest — namespace 独立のまま(Paper / Observation レイヤーは execution を import しない回帰を維持)

### 本設計が変更を要求するもの(後続 Issue の対象)

- `SafetyRejectionCode` へ **`intent_superseded` を追加**(訂正差し替え済みの親を送信しようとした場合の fail-closed 拒否)+ **clearance 不足に対する拒否コード**(#116 で命名)。既存コードの意味は変えない
- `mark_submitting()` に「自分を親とする子行が存在しないこと」および「再発行の子は有効な clearance mirror を持つこと」の同一トランザクション内検証を追加(既存の一度きり契約は不変)
- reconciliation 側に **clearance mirror の write-ahead suspend(観測前)と publish(commit 後)** を追加。suspend に失敗したチェーンは観測しない(fail-closed)。注文行・履歴には触れない
- Submission Journal に終端 outcome **`abandoned` を追加**(`not_started` からのみ遷移可)。entry のライフサイクル・reclaim 規則・二重送信ガードそのものは不変
- `OrderResult` への `rejection_category` 追加(#114)、`execution_orders` への 7 列追加・一意性制約の置換・clearance mirror テーブルの新設(#115)

## 移行方針(v4 → v5・後続 Issue)

- **リリース済み v4 と完全一致した DB のみ**を `init_db()` が migration する(既存方針)。単一トランザクションで: 新 7 列の `ALTER TABLE ADD COLUMN`(`signal_attempt` は `NOT NULL DEFAULT 1` で既存行を attempt 1 として backfill、他 6 列は NULL)→ `idx_execution_orders_signal`(signal 単独 UNIQUE)の DROP → `UNIQUE(signal_id, signal_attempt)` / `UNIQUE(reissued_from_…)` index の CREATE → **`execution_reissue_clearances` テーブルの CREATE**(空で開始 = 既存 signal は clearance を持たず、再発行するには新しい観測が必要 = fail-closed)→ version bump → 完全再検証 → COMMIT。途中失敗は全 rollback。既存行の削除・書き換えなし(旧 UNIQUE が保証していた既存データに attempt 重複はあり得ないため、index 置換で失敗する既存 DB は存在しない)。
- index の置換を含むため purely additive ではないが、**テーブル rebuild・行の書き換えは行わない**。単一列の CHECK(`signal_attempt >= 1`・`reissue_kind` / `reissue_initiated_by` の値集合)は `ADD COLUMN` に載せられるが、**複数列にまたがる相互制約(attempt ⇔ 親 NULL ⇔ 監査列、identity フィールドの親子一致)はテーブル制約であり `ALTER TABLE` では追加できない**ため、アプリケーション層 + 復元時 bundle 検証が正となる(既存の phase × `order_state` のような DB CHECK による二重防御は、これらの相互制約については得られないことを明示的な割り切りとして受け入れる)。参照 DDL との token 一致検証は v5 参照 schema で更新する。
- **読み取り経路は migration しない**(既存方針。v4 DB の読み取りは `init_db()` への誘導を含む `ExecutionSchemaError`)。dry-run の無副作用保証も不変。
- 旧 snapshot の `rejection_category` 欠落は `unclassified` として読み戻す(broker モデル側の後方互換。上記)。

## 後続 Issue への分割(本 Issue では実装しない)

| Issue | 内容 | 依存 |
|---|---|---|
| #114 | [Broker] `RejectionCategory` の定義と `OrderResult.rejection_category`(REJECTED ⇔ 必須の相互制約・旧 snapshot の `unclassified` 読み戻し・FakeBroker / OANDA Demo Adapter の写像) | なし |
| #115 | [Execution] schema v5(親子関係・attempt / kind 列・一意性制約の置換・**clearance mirror テーブル**・v4 → v5 migration・チェーン整合とフィールド不変条件の検証・`mark_submitting()` の子存在検証と **clearance 照合**・読み取り API のチェーン対応) | #114 は不要(独立) |
| #116 | [Execution] 手動再発行 + 訂正差し替えユースケース(登録 API・eligibility 検証・Gate 判定 / Journal 事前条件と `abandoned`・`intent_superseded` 拒否コード・**clearance の要件判定と reconciliation 側の mirror 発行 / 失効**・監査記録・通常 `submit()` 経路への接続・固定シナリオ) | #114 #115 |
| #117 | [Execution] `transient` 限定の自動再試行 policy(cooldown・上限・kill switch / discrepancy 前提条件。**既定無効**) | #114 #115 #116 + Live Execution runner(未着手) |

## 関連ドキュメント

- [Execution Request / Order Persistence](execution_request.md) — 現行の ID 設計・idempotency 契約(本書が拡張する対象)
- [Safety Gate / Live Order Submission](safety_gate.md) — 再発行 Request が通る唯一の送信経路
- [Submission Journal](submission_journal.md) — 独立した第二の二重送信ガード
- [Broker Interface / FakeBroker](broker_interface.md) — `OrderState` 状態機械と Broker Order Reissue(Issue #102)
- [Broker Reconciliation](broker_reconciliation.md) — 差分検出・修復と再発行前提条件の関係
