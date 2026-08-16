# Broker Reconciliation — 差分検出と安全な注文状態修復(Issue #103 / #104 / #105 / #133)

BELIAL 内部の注文状態と Broker 側の注文状態を照合し、不一致を分類・記録する**検出基盤**(Issue #103)と、そのうち安全に確定できる注文状態だけを内部へ冪等に反映する**修復サービス**(Issue #104)、および照合範囲を position / trade / transaction / SL・TP へ広げた **position スコープ検出**(Issue #105)。それらを**定期実行するための独立した oneshot 境界**(Issue #133)。実装は `execution/`(`reconciliation_models.py` / `reconciliation_repair_models.py` / `reconciliation_store.py` / `broker_reconciliation.py` / `broker_reconciliation_repair.py` / `reconciliation_runner.py` / `reconciliation_schedule.py` / `reconciliation_schedule_cli.py`)にある。

> **重要な位置づけ**
>
> - **検出サービスは検出専用(detection only)である。** 内部注文状態を変更しない・Broker へ注文を送信しない・Broker 注文をキャンセルしない。Broker へは**照会系操作のみ**(`get_order` / `list_orders` / `get_positions` / `get_open_trades` / `get_closed_trades`)を発行する。
> - **修復サービスが変更できるのは BELIAL 内部の注文状態だけであり、必ず既存の注文状態機械を経由する。** Broker への write 操作(submit / cancel / replace / close)は一切行わず、Broker へは `get_order` のみを発行する。SQL の直接 UPDATE も行わない(唯一の書き込み経路は `execution_store.record_order_result`。AST 検査で固定)。
> - **position / trade / transaction / SL・TP の照合(Issue #105)は検出 + `manual_review` 分類のみで、修復経路が存在しない。** position 系 discrepancy はモデル不変条件と DB CHECK の両方で `manual_review` 固定であり、修復サービスは order discrepancy(`run.discrepancies`)しか読まないため、position 系記録は構造的に修復へ到達できない。Broker 側のみの position / trade の自動採用・内部のみ position の自動 close・SL / TP の自動再作成・変更は行わない。
> - **定期実行は Execution 送信 runner とは独立した実行単位で行う**(Issue #133)。実行経路はサービス API の明示的な呼び出し、手動 CLI(`python -m execution.reconciliation_runner`。Issue #105)、または scheduled cycle CLI(`python -m execution.reconciliation_schedule_cli`。systemd timer + oneshot)の 3 つのみ。常駐 Bot・送信 runner への組み込みは行っていない。scheduled cycle も既存の `ReconcileOnlyBroker` facade しか受け取れず、submit / cancel 能力を構造的に持たない。
> - 注文状態の唯一の正は既存の `broker.OrderState` と `validate_order_state_transition()` / `validate_order_result_update()`([Broker Interface](broker_interface.md)、Issue #73)。本基盤は**注文状態 Enum・遷移表を新設・緩和しない**(position ドメインにも並行状態機械を導入しない — 観測値は文字列レンダリングで記録される監査情報である)。

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
   ├─ query-only → Broker interface(get_order / list_orders /
   │               get_positions / get_open_trades / get_closed_trades)
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
| `ReconciliationRun` | 1 回の照合結果: `run_id` / `mode` / 開始・終了時刻 / `checked_order_count`(比較した内部注文数)/ `discrepancies` / `broker_listing_error`。Issue #105 で `position_discrepancies` / `position_summary` を追加(`position_summary=None` = その run は position スコープを照合していない。order-only run と v3 以前の保存行) |
| `OrderDiscrepancy` | 1 件の注文差分: 種別 / 対象種別 / 対象 ID / resolution category / 観測時刻 / 判定理由(`detail`)/ 内部観測状態 / Broker 観測状態 / `client_order_id` / `broker_order_id` / `broker_error_code` |
| `PositionDiscrepancy`(Issue #105) | 1 件の position ドメイン差分(position / trade / transaction 由来 close / SL / TP): 種別 / 対象種別 / 対象 ID / **`manual_review` 固定**の resolution category / 観測時刻 / `detail` / `symbol` / 観測値の文字列レンダリング(`internal_value` / `broker_value`)/ 対応 ID 群(`execution_request_id` / `client_order_id` / `broker_trade_id` / `protection_order_id` / `broker_transaction_ids`) |
| `PositionReconciliationSummary`(Issue #105) | position スコープが**何を見たか**: `checked_position_count` / `checked_trade_count` / `checked_closed_trade_count`、および「照合しなかった」ことを明示する列挙エラー(`internal_exposure_error` / `position_listing_error` / `trade_listing_error` / `closed_trade_listing_error`)と fail-safe 除外シンボル(`excluded_symbols`) |
| `ReconciliationMode` | `detection_only` のみ。run 行へ保存し、「その run が何を許されていたか」をコードのバージョンから推測させない |
| `ReconciliationTargetType` | `execution_order`(`target_id` = `execution_request_id`)/ `broker_order`(`target_id` = `client_order_id`。内部 ID が存在しない Broker 側のみの注文)。Issue #105 で `position`(`target_id` = `"<symbol>:<side>"`)/ `broker_trade`(`target_id` = Broker trade id)を追加 |

`checked_order_count` があるため「差分 0 件」と「そもそも 1 件も見ていない」を区別できる。`has_partial_failures` は対象別 `query_failure`・列挙失敗/未対応・position スコープの列挙/導出失敗・除外シンボルの有無から導出し、一部だけ未確認の run を完全成功として表示しないための機械可読フラグである。

**enum の分割(Issue #105)**: `DiscrepancyType` / `ReconciliationTargetType` は 1 本の軸のまま拡張し、`ORDER_DISCREPANCY_TYPES` / `POSITION_DISCREPANCY_TYPES`(および `ORDER_TARGET_TYPES` / `POSITION_TARGET_TYPES`)の凍結サブセットで 2 つのドメインへ分割する。order 系 discrepancy テーブルの CHECK 制約は**フル enum ではなくこのサブセットから生成**されるため、enum が育っても既存テーブルの DDL はリリース済み v2 とバイト同一に保たれる(既存 DB の再構築なし)。`OrderDiscrepancy` は position 系の種別を、`PositionDiscrepancy` は order 系の種別を、それぞれ構築時に拒否する。

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
3. `broker_order_id` が `NULL → 値` の連鎖を壊さないこと。値の変更は**監査された Order Reissue の場合のみ**許容(Issue #102: 観測 snapshot の `superseded_broker_order_ids` が保存済み ID を記録していること。正確な chain-append 規則は次項の snapshot 更新契約が強制する。記録しない ID 変更は `manual_review`)
4. **Broker snapshot が名指す全 broker order ID(current + superseded chain)が他の Execution Request に割当済みでないこと**(broker order ID は execution order テーブル**全体で一意**(chain 含む)。他注文が所有する ID を反映しようとすると `record_order_result` が制約違反 / 重複 scan で全体 rollback する。所有者が照合中の注文自身である場合は競合ではない)
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

## Position / Trade / Transaction / SL・TP 照合(Issue #105)

`detect(run_id=..., include_position_scope=True)` / `run(run_id=..., include_position_scope=True)` で有効化する **position スコープ**。既定は `False` で、Issue #103 の order-only 呼び出しは挙動が一切変わらない。スコープの原則は「**差分検出 → discrepancy 記録 → `manual_review` 分類**」であり、このドメインには自動修復・自動採用・自動 close・SL / TP の自動再作成が**存在しない**(モデル・DB CHECK・修復サービスの入力経路の 3 層で構造的に保証)。

### 内部期待の導出(内部記録のみから)

内部側の position 期待は execution order store の記録**のみ**から導出する: 各注文の約定数量(保存済み `OrderResult` スナップショット)を BUY 正 / SELL 負で符号化し、シンボルごとにネットする。内部の反対売買で相殺された exposure はネット 0 になるため、Broker 側 close は「期待どおり」となり誤検出しない。**fail-safe 除外**: 結果が UNKNOWN の注文・スナップショットを読めない注文・状態だけでは約定量を確定できない注文(スナップショットなしの PARTIALLY_FILLED / CANCELLED)があるシンボルは期待が不定となり、exposure 比較から除外して `excluded_symbols` に記録する(推測しない)。ID インデックス(client_order_id / fill id)は除外注文についても構築する — 同一性照合は正確な ID 証拠であり、数量の不定性と無関係のため。

### ID ベースの対応付け(時刻・価格では推測しない)

Broker trade と内部注文の対応付けは **ID 証拠のみ**: (1) Broker が trade に報告する `client_order_id`、(2) trade id が注文の記録済み fill id(= Broker 約定 transaction id)に一致すること。0 件一致は `broker_only_trade`(そのシンボルに未確定注文があれば `trade_match_ambiguous`)、2 件以上一致は `trade_match_ambiguous` — **曖昧一致は決して推測で解決しない**。時刻・価格の近さによるマッチングは行わない。

### 検出する差分(position ドメインの discrepancy type)

| 種別 | 対象 | 意味 |
|---|---|---|
| `position_missing_on_broker` | `position` | 内部の filled 注文は exposure を期待するが、Broker に該当 (symbol, side) の position がない |
| `broker_only_position` | `position` | Broker にのみ position が存在する(自動採用しない) |
| `broker_only_trade` | `broker_trade` | Broker にのみ open trade が存在する(自動採用しない) |
| `trade_match_ambiguous` | `broker_trade` | ID 証拠で一意に対応付けられない(複数一致、または該当シンボルに未確定注文がある) |
| `broker_closed_internal_open` | `execution_order` | Broker の closed trade / closing transaction 証拠により Broker 側 close 済みだが、内部はまだ exposure を期待している |
| `internal_closed_broker_open` | `execution_order` | 内部は約定なしの終端(REJECTED / fill 0 の CANCELLED)だが、Broker はその注文に紐づく open trade を保持している |
| `stop_loss_missing` / `take_profit_missing` | `execution_order` | 内部 intent(`execution_orders.stop_loss_price` / `take_profit_price`)があるのに Broker trade に protection order がない |
| `stop_loss_price_mismatch` / `take_profit_price_mismatch` | `execution_order` | protection order の価格が intent と不一致(**Decimal の値等価比較**。float 比較・丸め許容は導入しない) |
| `stop_loss_reference_mismatch` / `take_profit_reference_mismatch` | `execution_order` | protection order の trade 参照(back-reference)が対象 trade と不一致 |
| `stop_loss_unexpected` / `take_profit_unexpected` | `execution_order` | 内部 intent がないのに Broker trade に protection order が付いている |

resolution category は**全種別で `manual_review` 固定**(モデル不変条件 + DB CHECK)。SL / TP の内部 intent は `execution_orders` の `stop_loss_price` / `take_profit_price`(= `ExecutionRequest` の intent。Issue #74)であり、SL / TP は broker boundary の submit を越えない(Issue #73 契約は不変)。

### Broker capability(optional Protocol の追加)

`OrderListingBroker` と同じ流儀で `broker/broker_interface.py` に 2 つの optional Protocol を追加した: `TradeListingBroker`(`get_open_trades() -> tuple[TradeInfo, ...]`)/ `ClosedTradeListingBroker`(`get_closed_trades() -> tuple[ClosedTradeInfo, ...]`)。`Broker` Protocol は 5 操作のまま不変。position 照会は既存の `Broker.get_positions()` を使う。capability 非対応・列挙失敗・列挙の重複 ID(曖昧)は、それぞれ summary の `*_listing_error` に「未確認」として記録し、**「差分なし」とは決して読ませない**。closed trade 照合は open trade 列挙が成功した場合のみ実行する(open 列挙なしには「もう open でない」を確立できないため)。closed 列挙は Broker 側で有界(直近分)でありうるため、**closed 一覧に無いことを「close されていない」とは読まない**(証拠としてのみ使う)。

### 照会失敗時の fail-closed

position スコープの Broker 照会はすべて列挙であり、失敗は run-level(summary の `*_listing_error`)に記録して該当ドメインの比較を**まるごとスキップ**する。execution store のスナップショット読み取り失敗は `internal_exposure_error` + シンボル除外。いずれの場合も**内部状態は一切変更されず、discrepancy を捏造しない**(SL 状態が取れない ≠ SL missing)。

### 冪等性

order スコープと同一の run 単位 append 方針。同じ状態で何度照合しても、内部 position / order 状態は変わらず(検出サービスに書き込み経路がない)、run ごとに独立した記録が append され、同一 run 内の重複は `UNIQUE (run_id, discrepancy_type, target_type, target_id)` で排除される(1 注文の複数 trade から同じ SL 所見が出ても 1 行)。

### 手動 reconciliation CLI(`python -m execution.reconciliation_runner`)

Issue #105 の手動照合経路。**reconcile-only**: 渡された Broker を `ReconcileOnlyBroker` facade で包み、`submit_order` / `cancel_order` / `get_account_state` は**無条件に raise** する(検出サービス自体の detection-only 契約に対する defense in depth。口座情報は取得すらしない)。

- `--broker oanda-demo`: `OANDA_API_TOKEN` / `OANDA_ACCOUNT_ID` / `OANDA_API_URL` から OANDA Demo Adapter(Issue #79)を構築する。**Demo 以外の endpoint は Adapter の構築時 allowlist で fail-closed に拒否**され、CLI は usage error(exit 2)で停止する。Safety Gate の `BROKER_TYPE_BY_MODE` には登録しない(submit 経路は開放されない)。token / account id / endpoint はログ・出力へ一切出さない(出力は定数ラベル `demo / oanda-demo` のみ)。
- `--broker fake`: 空の in-memory FakeBroker(配線・dry-run 検証用。ネットワークなし)。
- `--dry-run`: 比較と報告のみ。reconciliation DB を作成・migration・書き込みしない。省略時は `run()`(冪等 init + append)。
- `--orders-only`: Issue #103 スコープに限定。`--run-id` / `--now` / `--json`(pure ASCII)/ `--execution-db` / `--reconciliation-db` / `--data-dir` は他の execution CLI と同じ流儀。
- exit code: 0 = 差分・部分失敗なし / 1 = 差分または部分失敗あり(実行時失敗を含む)/ 2 = usage・設定エラー(非 Demo 拒否を含む)。
- 通常 CI・pytest は実 Demo API へ接続しない(FakeBroker 経路 + 環境変数による拒否テスト + monkeypatch した offline adapter のみ。実 API 試験は従来どおり `BELIAL_RUN_OANDA_DEMO_API_TESTS=1` の opt-in スイートに分離)。

### 定期実行 cycle(`python -m execution.reconciliation_schedule_cli`、Issue #133)

上記の照合を**継続運転中に定期実行する**ための境界。第一候補どおり **systemd timer + oneshot service** で駆動し(`scripts/systemd/berial-execution-reconciliation.{service,timer}`)、**Execution 送信 runner とは独立した実行単位**とする。

**なぜ送信 runner に統合しないか**: 送信 runner のライフサイクルは「intent を選ぶ → 送信境界へ渡す → 結果を観測する」であり、reconciliation のライフサイクルは「両側を観測し分類し、必要なら Broker の事実を内部状態機械へ反映する」である。失敗の性質が違い、触ってよい対象が違い、**互いに独立して停止・再起動できること自体が要件**である(reconciliation の停止が送信を止めず、送信の停止が監査を止めない)。`execution/live_runner.py` は Issue #131 の非スコープ宣言どおり reconciliation を呼ばない。

1 cycle の構成(`execution/reconciliation_schedule.py` の `run_reconciliation_cycle()`):

1. **排他入場** — cycle 全体を advisory lock で保護する(既定 `<reconciliation DB のディレクトリ>/reconciliation_cycle.lock`)。
2. **takeover recovery**(`--repair` 時のみ)— crash した前回 cycle が残した pending attempt を、既存の `recover_pending_attempts()` で**新しい観測の前に**引き継ぐ。後に回すと、回収された更新が直前の観測を stale にするだけになるため。監査 DB がまだ存在しない初回 cycle では pending が存在し得ないので、DB を作ってまで問い合わせない。**recovery が reservation を拒否した場合、または recovery 後に pending attempt が 1 件でも残っている場合(`BLOCKED`、あるいは単に lease が有効なだけの場合を含む)は、この cycle の 4 を丸ごと skip する**(下記「未解決の claim が残っている cycle は修復 pass を走らせない」)。
3. **検出** — `BrokerOrderReconciliationService.run()`(既定で position スコープ込み)。run が監査 DB へ記録されることが restart をまたいだ追跡可能性の実体である。
4. **修復**(`--repair` 時のみ)— 直前に記録した run に対する `repair(mode=APPLY)`。

**構造的に持たない能力**:

- cycle が受け取れる Broker は `ReconcileOnlyBroker` **だけ**である(型で拒否する。`get_order` / `get_positions` を持つだけの duck-typed オブジェクトも拒否される)。broker 実装を import せず、`BrokerConnection` を組み立てず、adapter を構築しない — 4 点とも `execution/tests/test_submission_boundary.py` の AST 検査で固定。OANDA Demo Adapter の構築サイトは `demo_lock.py` と `reconciliation_runner.py` のままで、**Issue #133 は新しい構築サイトを増やしていない**(cycle CLI は `reconciliation_runner.build_reconcile_only_broker()` を経由する)。
- cycle 自身は分類も状態更新もしない。`auto_repair_candidate` / `manual_review` の判定は検出サービスの、修復可否は修復サービスの責務であり、claim / lease / generation / takeover / fencing 契約はそのまま使われる(緩めない・再実装しない)。execution store の書き込み API を cycle が直接呼ばないことも AST 検査で固定。

**未解決の claim が残っている cycle は修復 pass を走らせない**(PR #137 レビュー r2 / r3 / r5): recovery の後、cycle は **(1) audit DB に pending が残っているか**、**(2) recovery が `BLOCKED` を返したか** の**両方**を見て、どちらかが成立すれば新しい修復 pass を走らせない。

**両方を見る理由**: 片方はもう片方を含意しない。recovery が何も返さなくても pending は残り得るし、逆に **`BLOCKED` が必ず pending を伴うとは限らない** — 既に finalize 済みの attempt に紐づく **orphaned な active intent** を protocol が park した場合(`_report_orphaned_active_intents`)、recovery は `BLOCKED` を返すのに pending 行は 1 件も無い。片方だけを見ると、もう片方のケースがそのまま通過する。

**recovery が何も返さないことは「何も保護されていない」ことを意味しない**。recovery は (1) lease がまだ有効な reservation を意図的に放置し(誰かが今まさに駆動しているかもしれず、kill されたプロセスの claim も期限までは leased のまま残る)、(2) `BLOCKED` を意図的に pending のまま残す(Issue #104 は「**blocked からの自動復旧**」を明示的に非スコープとしており、人手の判断を要求する境界である)。

新しい pass はこの両方を素通りしてしまう。attempt id が新規であり、`execution_store.register_repair_fence` は **別 attempt が保持する fence を設計上 REPLACE する**(本当に新しい attempt は注文を引き継いでよい、という契約)ためである。結果として、他者が claim しているまさにその注文が再観測され、generation 1 で再 claim され、commit され、元の claim は pending のまま残る。timer 運用ではこれが 1 回きりではなく**毎 tick 起きる**。

**entry lock はこれを防げない**。lock が排他するのは cycle 同士であって、claim は cycle より長生きするため、kill された cycle(や人手の API 呼び出し)が残した claim は次の tick が lock を取った時点でまだそこにある。

この延期は**自己修復的で有界**である: lease 満了後、次の cycle の recovery が **元の identity のまま** takeover し、以降の pass は通常どおり走る。無期限に残るのは `BLOCKED` だけであり、それは意図どおりで、cycle が clean を名乗らない理由でもある。検出はどちらの場合も実行・記録される — 状況を診断するために運用者が必要とするのは、まさにその read-only な観測だからである。

**永続化された `blocked` intent は「可視化」であって「gate」ではない**(PR #137 レビュー r6): 一度 park された blocked intent は **active ではなくなる**ため、以降の cycle では recovery が何も返さず、pending 行も無い — gate の生きた信号が両方とも沈黙する。それでも人手対応が必要な事実は audit DB に残っている。

そこで cycle は、**開始時点で audit DB にある `blocked` intent を毎回読み出して報告し、その cycle を clean と名乗らせない**(`blocked_intent_ids` / CLI の `unresolved_blocked_intents` / exit 1)。blocked な takeover が journal から流れて忘れられることを防ぐ。

**gate にはしない**理由: `BLOCKED` は**終端 status** であり(`ALLOWED_INTENT_TRANSITIONS[BLOCKED]` は空)、intent は監査証跡として**永久に保持される**。したがって「解決されるまで止める」を実装する手段が存在せず、gate にすると「**歴史的な監査行 1 行で永久に止まる**」= 無関係な注文の修復まで無期限に停止し、戻す手段が無い、という状態になる。gate は生きた保護(pending claim・その cycle の `BLOCKED` 結果)だけが担う。

> **既知の制約**: 上記のとおり blocked intent を解消する in-band な手段が現状存在しないため、**一度発生すると以降の cycle は恒久的に exit 1 になる**。「blocked takeover を運用者が解決済みとして確定する」操作は Issue #104 の intent 状態機械に属し、Issue #133 のスコープ外である(後続 Issue 候補)。

**この gate は mutex ではない**(PR #137 レビュー r4)。判定は可能な限り**遅く** — 検出の後、pass の直前 — に行うため、「見る」と「動く」の間隔はこのモジュールが縮められる限り小さい(検出 pass 1 回分の窓は除去済み)。それでも、**修復サービスを直接呼ぶ運用者は cycle lock を取らない**ため、gate の読み取り直後に claim する可能性は残る。

これは本モジュールが解くべき問題ではなく、かつ**すでに解かれている**: 「1 注文に 2 つの repair worker」という、Issue #104 の fencing 契約がまさに想定している状況である。`register_repair_fence` は新しい attempt に注文を引き継がせ、古い worker の commit / finalize は transaction 内で typed に失敗する(`repair_fence_conflict` → outcome `conflict`)。**誤った書き込みは起こらず、provenance 行が 2 本になることも、更新が失われることもない。** `execution/tests/test_reconciliation_schedule.py` が実際にスレッドを停止させてこれを固定している。

呼び出し経路をまたいで check と claim を atomic にするには、修復サービス内部に**呼び出し側横断の admission プロトコル**を追加する必要がある。それは Issue #104 の claim / lease / generation / fencing 契約そのものの変更であり、Issue #133 が触れてはならない領域である — しかもその契約がすでに安全にしている結果を防ぐために、である。

**overlap / concurrent run**: timer 駆動の `Type=oneshot` unit は systemd 自身が二重起動しないが、**手動実行と timer 実行の重なり**はそれでは防げないため、cycle 全体を advisory lock で保護する。lock は「空の SQLite ファイルを `BEGIN IMMEDIATE` で保持する」形式で、POSIX / Windows で同一に動作し、プロセス消滅時に OS が解放する(crash 後に手で消す stale lock が残らない)。同一プロセス内の 2 つ目の取得も拒否されるため、排他そのものをテストで固定できる。busy な lock は**失敗ではなく抑止**(`CycleAlreadyRunningError` → CLI は WARNING と `outcome: "suppressed_locked"` を出して exit 0)。

この lock が保証するのは **入場の排他**だけである。「2 つの worker が同一 discrepancy / repair claim を掴まない」ことは、従来どおり修復サービスの durable な claim / lease / generation / fencing が保証する(lock の有無に依存しない)。

**dry-run**: 完全に無副作用。`detect()` 経路のみを使い、監査 DB を作成・migration・書き込みせず、execution DB も作成せず、**lock file も開かない・作らない**。`--repair` との併用は usage error(記録済み run が存在しないため)。

**restart 継続性**: cycle が生む情報はすべてプロセス終了前に永続化される(run と discrepancy = 監査 DB、repair attempt = claim 行 → finalized 行 + takeover intent)。戻り値の `ReconciliationCycleReport` はその**ビュー**であって記録ではない。再起動後は次の cycle が lease 期限切れの pending を引き継ぎ、運用者は `run_id` から `get_reconciliation_run()` / `list_repair_attempts()` / `list_pending_repair_attempts()` で追跡できる。BLOCKED な takeover が 1 件でもあれば、差分ゼロでも cycle は clean を名乗らない(exit 1)。

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

lease 期限切れ後は recovery worker が新しい generation で takeover できる。takeover は **durable takeover intent** に駆動される再入可能な cross-DB protocol である(再レビュー r4)。

```text
owner A / generation 1
    ↓ lease expiry(それ以前は誰も — 同じ owner_id を使う別実行であっても — 触れない)
takeover intent(takeover_id / target owner / target claim execution id / generation 2)を
reconciliation DB へ prepared で durable commit  ← ここで identity が確定する
    ↓
owner B / generation 2 が intent の identity のまま fence install → claim promotion
    ↓
owner A / generation 1 の遅延 commit は Execution DB の fence 照合で拒否される
owner A / generation 1 の遅延 finalize は claim CAS で拒否される
```

**claim 契約**

- 新規 reservation: `owner_id` = 実行中の worker、`generation` = 1、`takeover_id` = NULL(原初の claim は takeover 由来ではない)、`lease_expires_at_utc` = `reserved_at + lease_duration`、`status` = pending。
- lease 有効中の pending は**再取得できない**。判定に使うのは owner 名ではなく **lease の期限だけ**なので、同一 service instance・同一 `owner_id` からの並行呼び出しでも二重実行にはならない。
- takeover は lease 期限切れ後のみ。**identity を作れるのは intent 作成の 1 箇所だけ**で、claim の昇格(`promote_repair_claim()`)は intent の `target_*` を CAS で焼き付けるだけである。generation ≥ 2 の claim は必ず `takeover_id` を持つ(DB CHECK)。
- lease 期限判定は呼び出し側 clock の契約、原子性は CAS の責務、と役割を分離している(store は clock を持たない)。

### durable takeover intent(再レビュー r4)

2 つのストアは同時に commit できない。したがって「先に identity を durable にしてから両 DB を順に動かす」以外に、**restart 後も同じ identity で続行できる**方法はない。`broker_reconciliation_repair_takeover_intents` はそのための表であり、**takeover の source of truth** である。

| 列 | 意味 |
|---|---|
| `takeover_id`(PK) | この takeover の identity。fence・claim・finalize のすべてが提示する |
| `repair_attempt_id` | 対象 attempt(FK) |
| `source_generation` / `target_generation` | 何世代から何世代へ(`target = source + 1` を DB CHECK で固定) |
| `source_owner_id` / `source_claim_execution_id` | 置換される旧 claim(CAS の前提条件) |
| `target_owner_id` / `target_claim_execution_id` | 正当な新 owner / 実行単位 |
| `lease_expires_at_utc` | この takeover を完了させる期限。切れたら**同じ intent を**別 worker が再開できる |
| `status` | `prepared` / `fence_installed` / `claim_promoted` / `completed` / `cancelled` / `blocked` |
| `created_at_utc` / `fence_installed_at_utc` / `claim_promoted_at_utc` / `completed_at_utc` / `cancelled_at_utc` / `blocked_at_utc` | 各段階の時刻(status と整合を DB CHECK で固定) |
| `blocked_reason` / `observed_claim_generation` / `observed_fence_generation` / `observed_fence_identity` | blocked の診断(closed set + 観測値) |
| `cancelled_reason` | cancel の理由(closed set。`cancelled` ⇔ NOT NULL を DB CHECK) |

**一意性**(すべて DB 制約): active intent(`prepared` / `fence_installed` / `claim_promoted`)は **attempt ごとに高々 1 件**(partial UNIQUE index)/ **同じ generation を 2 度 takeover できない**(`(repair_attempt_id, source_generation)` の partial UNIQUE index。`blocked` 行は診断であって takeover ではないので対象外)/ **blocked 診断も同 pair につき 1 件だけ**(もう 1 つの partial UNIQUE index)/ `takeover_id` は PK / `target_generation = source_generation + 1` / status・blocked reason・cancel reason は closed set。作成時にはさらに transaction 内で「attempt は pending か」「lease は本当に切れているか」「source identity は現在の claim と一致するか」を検証する。複数 worker が同時に作成すると **1 件だけが勝ち**、敗者は何も書かずに離脱する(勝者の intent が新しい lease を持つため、その attempt は勝者のものになる)。

**state machine**(前進のみ・同一遷移は冪等・別 identity は拒否・**実体を伴わない前進は不可**):

```text
prepared → fence_installed → claim_promoted → completed
   ↓             ↓                 ↓
 cancelled     cancelled         blocked
   ↓             ↓
 blocked       blocked
```

`cancelled` は「takeover が不要になった」= 置換対象の claim 自身が attempt を finalize した場合(遅いだけで生きていた worker が勝った。`TakeoverCancelReason` を必須とする)。`completed` / `cancelled` / `blocked` は終端で、二度と動かない。unknown status / blocked reason / cancel reason は model / DB CHECK / restore のすべてで fail-closed。

### intent status は進捗フラグではない — 遷移ごとの実体検証(再レビュー r5)

**status は「その実体が本当に起きた」という主張**であり、任意に前進させられる進捗フラグではない。汎用の `advance_takeover_intent(status=...)` は廃止し、遷移ごとの専用 API に分割した。各 API は遷移先が主張する実体を**同一 transaction 内で**検証し、不一致なら status を動かさず rollback して typed 例外を返す(silent no-op にしない)。

| API | 遷移先 | 同一 transaction 内で検証する実体 |
|---|---|---|
| `mark_takeover_fence_installed(execution_db_path=...)` | `fence_installed` | **Execution DB の commit 済み fence 行を、この API 自身が読む**(`execution_store.get_repair_fence()`)。読んだ行の `execution_request_id` / `repair_attempt_id` / `generation` / `takeover_id` / `owner_id` / `claim_execution_id` が、intent の target identity **かつ attempt 行自身の execution order** と完全一致すること。呼び出し側から evidence を受け取る引数は存在しない(r6 レビュー) |
| `mark_takeover_claim_promoted()` | `claim_promoted` | repair attempt が **pending** で、`generation` / `takeover_id` / `owner_id` / `claim_execution_id` がすべて intent の target identity と一致すること |
| `complete_takeover_intent()` | `completed` | repair attempt が **finalized** で、`outcome` と `finalized_at_utc` を持ち、outcome が `COMPLETABLE_TAKEOVER_OUTCOMES`(`would_apply` と `blocked` 以外の全 outcome)に属し、identity 4 要素が一致すること |
| `cancel_takeover_intent(reason=...)` | `cancelled` | repair attempt が **置換対象の source claim** で finalize されていること(この intent の identity で finalize されていたら cancel ではない) |
| `block_takeover_intent(reason=...)` | `blocked` | 実体要求なし(fail-closed の受け皿。reason と観測値が必須) |

したがって「fence 未設置・claim 未 promotion・attempt 未 finalize」のまま `prepared → fence_installed → claim_promoted → completed` と進めることは**公開 store API から直接呼んでも不可能**である(r5 レビューが再現した Blocker)。identity は 4 箇所で一致していなければならず、`takeover_id` / `owner_id` / `claim_execution_id` / `generation` のどれか 1 つが違うだけで遷移は拒否される。lease だけは identity ではなく liveness として扱う(再開時に intent の lease が前へ動くため)。

typed 例外: `TakeoverProtocolInvariantError`(基底)/ `TakeoverClaimNotPromotedError` / `TakeoverIdentityMismatchError`。

### 呼び出し側が渡す値は「権限」ではない(再レビュー r6)

r5 まで `fence_installed` への遷移は、呼び出し側が組み立てた `TakeoverFenceProof`(ただの frozen dataclass)を「fence 設置の証明」として信用していた。intent を持っている者なら intent と完全一致する proof を作るのは自明であり、これは

- `prepared → fence_installed` を通し、
- そのまま claim を generation 2 へ promotion させ、
- しかし Execution fence は generation 1 のまま

という **identity 分裂**(2 つの store が「誰が order を持っているか」で食い違う)を許した。fence はまさにこれを防ぐために存在するので、Blocker である。

**修正の方針は「検証の強化」ではなく「trust boundary からの削除」**である。proof を渡す引数そのものを廃止し(`TakeoverFenceProof` は削除)、遷移 API が **source of truth である Execution DB の永続 fence 行を自分で read** する。`execution_db_path` は「どの order store を見るか」という **configuration** であって evidence ではない(別の store を指せる呼び出し側は、そもそも本物の store に書ける)。

API 内部の順序:

1. Reconciliation DB の transaction を開始する
2. intent 行を read し、caller が渡した intent と identity が一致することを確認する(`_verified_intent_row`)
3. attempt 行を read し、**その行の** `execution_request_id` を得る(caller が指定した order ではない)
4. `execution_store.get_repair_fence()` で **commit 済み fence 行**を read する(SQL を再実装しない)
5. fence 行と intent target identity を 6 項目で完全比較する
6. 一致したときだけ `fence_installed` へ遷移する
7. 不一致・行なし・store が読めない場合は status を動かさず rollback し、typed 例外を返す

| 観測した状態 | 結果 |
|---|---|
| fence 行なし(未設置 / 別 order にしかない) | `TakeoverProtocolInvariantError` → service は `takeover_protocol_corrupt` で BLOCKED |
| identity のいずれか 1 項目が不一致(古い generation・別 takeover・別 owner・別 claim・別 attempt を含む) | `TakeoverIdentityMismatchError`(差分列名を列挙)→ `fence_intent_mismatch` で BLOCKED |
| fence が intent より先の generation | 同上 → `fence_ahead` で BLOCKED |
| order store が壊れている / 読めない(`sqlite3.Error` を含む) | `TakeoverProtocolInvariantError` → `takeover_protocol_corrupt` で BLOCKED |
| attempt が execution order を持たない(broker-only) | fence は存在し得ないので fence なしで遷移可 |
| すでに `fence_installed` | 実体を再確認したうえで冪等に return(crash 後の再試行) |

TOCTOU について: fence contract は単調である(generation は巻き戻らない / 同一 generation が別 identity へ張り替えられることはない / finalize 済み fence は再武装されない)。したがって「この identity の fence を観測した」あとに起こり得るのは **そのまま維持されるか、別の attempt が order を奪うか**のどちらかだけで、この intent の identity を後から偽造することはできない。さらに最終的な `record_order_result()` が **自分の transaction 内で** fence を再検証するため、新しい attempt と競合した takeover は commit で敗北する(誤った書き込みにはならない)。read と遷移が同一 reconciliation transaction 内にあることで、read 後に intent 側の status だけが横取りされることもない。

同じ観点で protocol 全体を横断確認した結果:

| 呼び出し側が渡す値 | 扱い |
|---|---|
| `TakeoverFenceProof` | **削除**(唯一「durable write の証明」として信用されていた値) |
| `RepairFenceToken`(`record_order_result` / `register_repair_fence`) | evidence ではなく **assertion**。同一 transaction 内で commit 済み fence 行と突き合わせ、書ける範囲を狭めるだけ |
| `InstalledRepairFence`(`register_repair_fence` の戻り値) | commit 済み行の **観測値**。log / test 用であり、権限として持ち回さない |
| `RepairTakeoverIntent`(全 intent API の第 1 引数) | lookup key + identity 主張。必ず stored 行と突き合わせる |
| `OrderRepairAttempt`(`finalize_repair_attempt`) | reservation 行の identity 列 + claim 4 要素と突き合わせる |
| `block_*` の `observed_*` | 人間が読む **診断値**のみ。blocked は fail-closed 方向なので claim / fence / order を動かさない |
| `observed_at_utc` / `at_utc`(clock) | 入力であって durable fact の証明ではない。早すぎる takeover は fence と CAS で敗北するだけで、誤った書き込みにはならない |


### claim promotion の critical section(再レビュー r7)

`fence_installed` は「**過去のある時点で** fence がこの identity を指していた」という歴史的事実にすぎず、claim promotion の許可そのものではない。r6 までは

```text
fence を read して検証 → (窓) → promote_repair_claim() の CAS
```

の間に別 attempt が同じ注文の fence を置換でき、その結果

```text
Reconciliation claim : generation 2 / identity X
takeover intent      : CLAIM_PROMOTED / X
Execution fence      : generation 1 / identity Y
```

という **identity 分裂**を、reconciliation store の API 自身が作れてしまった(Blocker)。

**「CAS の直前にもう一度 read する」は修正にならない。** read と CAS の間に同じ窓が残るだけである。したがって検証と CAS の全体を、fence を置換できない排他区間の中に入れる。

```text
execution_store.repair_fence_promotion_guard()   # Execution DB: BEGIN IMMEDIATE
  → commit 済み fence 行を lock 下で read
  → intent target identity と 6 項目完全比較
  ├ reconciliation DB: BEGIN IMMEDIATE → claim CAS → COMMIT
  └ guard 解放
```

guard は Execution DB の **write lock(RESERVED)** を保持する。保持中:

- 他 connection は Execution DB の write lock を取得できない = `register_repair_fence()` / `record_order_result()` による fence 置換は成功しない(busy timeout まで待ち、その後 typed に失敗する)
- 他 connection の **read は一切ブロックされない**。guard は `PRAGMA query_only` を立てて**自分では書けない**ため、RESERVED を EXCLUSIVE へ昇格させることが決してないからである

同じ guard は `mark_takeover_fence_installed()` でも使う。これにより **cross-DB を触る全経路の lock order が一方向に統一**される。

#### lock order(全経路で統一)

```text
Execution order store の guard(write lock)
        ↓
Reconciliation DB の transaction
```

逆順(Reconciliation の lock を保持したまま Execution の lock を取る)経路は存在しない。guard を取る前に必要な `execution_request_id` は **true read-only 接続**で読み、その read は guard 取得前に完全に終了している(lock を跨いで保持しない)。`execution_store` から `reconciliation_store` を呼ぶ経路は 1 つも無い。

対象経路の確認: repair claim promotion / repair finalize / recovery / live worker commit / fence register・replace / blocked handling / old worker finalize — いずれも「Execution の write lock を保持したまま Reconciliation の lock を待つ」か「どちらか一方だけ」であり、逆順は無い。

#### promotion 時の identity 検証

guard を取得したあと、**Execution DB の実 row** について次の 6 項目を比較し、1 つでも違えば claim CAS を行わない。

```text
execution_request_id / repair_attempt_id / generation /
takeover_id / owner_id / claim_execution_id
```

| guard 下で観測した状態 | 例外 | service の扱い |
|---|---|---|
| fence 行なし / order store なし | `TakeoverProtocolInvariantError` | BLOCKED `takeover_protocol_corrupt` |
| 別 attempt・別 takeover・別 owner・別 claim・別 order | `TakeoverIdentityMismatchError`(差分列名付き) | BLOCKED `fence_intent_mismatch` |
| 先の generation | 同上 | BLOCKED `fence_ahead` |
| 古い generation(置換対象の旧 claim の fence) | 同上 | BLOCKED `fence_intent_mismatch` |
| store が壊れて読めない | `TakeoverProtocolInvariantError` | BLOCKED `takeover_protocol_corrupt` |
| store が write lock されたまま busy timeout | `TakeoverFenceUnavailableError` | **BLOCKED にしない**。sanitize 済みログを出して back off し、次の pass が同じ intent を再開する |

#### promotion 成功時に保証される invariant

```text
claim(generation N+1 / identity X)が commit された瞬間、
Execution fence は identity X を指していた
```

guard 保持中に fence を置換できないので、これは「検証時に真だった」ではなく「CAS commit 時点でも真だった」である。

#### process death / timeout / deadlock

- guard は **SQLite のファイルロック**であり、耐久化された lock 行ではない。プロセスが死ねば OS がロックを解放する。**永久ロックも掃除対象も存在しない**(lease や cleanup を持つ durable reservation を新設していないのはこのためで、schema も増えていない)。
- ロック待ちは必ず `BUSY_TIMEOUT_SECONDS`(30 秒)で打ち切られ、`ExecutionStoreBusyError` / `TakeoverFenceUnavailableError` という **typed failure** になる。無限待機する経路は無い。
- guard が read-only であることにより、「Reconciliation transaction の中から Execution を read する」経路(`mark_takeover_fence_installed` の substance check)と互いにブロックしない。これが deadlock 不在の根拠である。

#### guard を抜けたあと

guard は claim CAS までを守る。その後に別 attempt が fence を置換する契約は従来どおり維持される(attempt ID は reservation ごとに一意なので、新しい attempt が正当に注文を取る)。置換された場合、先の worker は `record_order_result()` の transaction 内 fence 検証で拒否され、recovery は typed BLOCKED へ収束する(永久 pending にはならない)。

### cross-DB protocol(8 段階・各段階から restart 可能)

```text
1. reconciliation DB : intent を prepared で durable commit
2. execution DB      : その identity を fence として install(同一 token は冪等)
3. reconciliation DB : intent → fence_installed(このとき Execution DB の fence 行を read して検証)
4. reconciliation DB : claim を intent の identity へ promotion(CAS・冪等)
5. reconciliation DB : intent → claim_promoted
6. 処理              : Broker 再照会 + eligibility + fencing された Execution 更新
7. reconciliation DB : attempt finalize
8. reconciliation DB : intent → completed
```

どの段階の間でプロセスが落ちても、次の worker は **active intent を読み出し**、`takeover_id` / `target_owner_id` / `target_claim_execution_id` / `target_generation` を**そのまま**使って続きから再開する(新しい identity を作らない)。intent の lease が切れている場合は、まずその lease を原子的 CAS で取り直すため、**再開者も 1 人だけ**になる。

段階 2 と 3 の間で落ちた場合、再開した worker には前の worker が持っていた in-memory の値は何も残っていない。それでよい: 段階 3 は commit 済み fence 行を read して判断するので、**死んだ worker が commit した事実だけ**から同じ takeover を再開できる(r6 レビュー)。

### identity の完全一致(fencing)

`record_order_result()` の transaction から reconciliation DB の claim を読むことはできない(SQLite ファイルを跨ぐ単一トランザクションは存在しない)ため、**案 A: Execution DB 内の fence mirror**(`execution_repair_fences`)を採用する。fence は attempt ID と generation だけでなく **takeover ID / owner / claim execution id** も保持し、次の 4 箇所が**同一 identity** を共有する。

```text
takeover intent  ==  Execution fence  ==  Reconciliation claim  ==  attempt finalize
```

- `register_repair_fence(token=...)`: **完全一致した token のみ冪等成功**。同一 attempt・同一 generation でも takeover ID / claim execution id / owner が違えば `ExecutionRepairFenceConflictError`(= 別 identity は**決して adopt しない**)。generation の巻き戻し・飛び越しも同様に拒否(`stored + 1` だけが前進)。別 attempt の fence は置換される(attempt ID は reservation ごとに一意なので、新しい attempt が正当にその注文を取る)。
- `record_order_result(..., repair_fence=token)`: 同一 `BEGIN IMMEDIATE` 内で **fence の 5 要素 identity + expected revision + expected current state** を検証し、1 つでも外れれば全 rollback。したがって takeover された旧 worker は**状態も revision も一致していても** commit できない。
- `finalize_repair_attempt()`: `attempt_id` + `owner_id` + `claim_execution_id` + `generation` + **`takeover_id`** + `status = pending` の CAS。旧 identity は APPLIED を上書きできない。

**旧 worker が先に commit した場合**(intent prepared 後〜fence install 前の窓): 旧 generation の fence はまだ有効なので commit は成功しうる。その後 takeover 側は state history の構造化 provenance でそれを検出し、**再適用せずに** `applied` へ収束する。逆に、fence を追い越された worker が自分の commit を拒否された場合は、provenance を見て `already_applied` を返す(**自分がやっていない適用を `applied` と偽らない**)。どちらの経路でも history は 1 行、audit は APPLIED、attempt ID は同一である。

### recovery 契約(`recover_pending_attempts()`)

pending reservation を列挙し、次のように処理する。

| 観測 | 動作 |
|---|---|
| active intent あり・intent の lease 有効 | **触らない**(他 worker が駆動中) |
| active intent あり・lease 期限切れ | **同じ identity で再開**(lease を CAS で取得した 1 人だけ) |
| active intent なし・claim の lease 有効 | **触らない**(照会も finalize も takeover もしない) |
| active intent なし・claim の lease 期限切れ | 新しい intent を 1 件だけ作成して protocol を開始 |
| blocked intent が同 generation に存在 | **自動処理しない**。typed `BLOCKED` を返すだけ(新しい行も generation も増えない) |
| **terminal intent(completed / cancelled)があるのに attempt が pending** | typed `BLOCKED`(`terminal_intent_with_pending_attempt`)。下記 |
| 安全に収束できない状態 | 下記のとおり durable に `blocked` へ |

### intent 作成競合の分類(一律 `None` にしない)

`create_takeover_intent()` が競合した場合、**原因を確認せずに「別 worker が勝った」と扱わない**(r5 レビュー)。DB を再読して分類する。

| 再読の結果 | 判定 |
|---|---|
| attempt が既に finalized | 正常(pending の snapshot が古かっただけ)。何もせず離脱 |
| 同 generation の **active** intent があり、その source identity が現在 claim と一致 | 正常競合。winner が所有しているので離脱 |
| active intent の source identity が現在 claim と不一致 | 異常 → `BLOCKED`(`intent_source_mismatch`) |
| 同 generation の **terminal** intent(completed / cancelled)があり attempt は **pending** | 異常 → `BLOCKED`(`terminal_intent_with_pending_attempt`) |
| 説明のつく intent が無い / 再読自体が失敗 | 異常 → `BLOCKED`(`protocol_corrupt`) |

**terminal intent + pending attempt** は「takeover は終わったと主張しているのに仕事が終わっていない」状態である。generation の一意制約により新しい intent も作れないため、放置すると recovery は永久に空 tuple を返し続ける(r5 レビューが 10 回再起動で実測した silent stop)。この経路は廃止し、**typed BLOCKED を返して blocked intent 行へ永続化**する。保存されるのは attempt ID / intent ID / intent status / attempt status / claim generation / claim takeover ID / claim owner / claim execution ID / 観測時刻 / blocked reason(detail 文字列と observed 列に分けて記録)。BLOCKED の間は **Broker 照会 0 回・Execution 更新 0 回・generation 増加なし・intent 追加なし**で、何度再起動しても同じ BLOCKED が安定して返る。

blocked 行は `(attempt, source_generation)` ごとに **1 件だけ**存在できる(専用の partial UNIQUE index)。takeover 用の一意制約は「blocked 以外」に限定した partial UNIQUE index なので、診断行は terminal intent と共存でき、かつ量産されない。blocked 行が存在する generation は `create_takeover_intent()` 自身が拒否する(**人手の介入待ちを自動で再試行しない**)。

**「説明できない fence」の判定**は、`fence.takeover_id` が **その attempt の intent 履歴(active / terminal を問わず)に存在するか**で行う。「今 active な intent があるか」では判定しない — 並行 worker が intent を commit して fence を install する瞬間を跨いで読むと、健全な takeover を誤って blocked にしてしまうため(10 worker の CI レースで顕在化)。説明できる fence の場合はそのまま原子的な各段階(intent 作成 / fence install / claim promotion)へ進み、それらの CAS が結果を決める。

**blocked にする状態**(黙って `continue` しない): Execution fence が intent の target generation より先行 / fence が別 attempt・別 takeover・別 claim・別 owner / active intent が無いのに fence だけ先行 / active intent が複数 / claim が intent の target を追い越している / intent の source が現在 claim と不一致 / **terminal intent があるのに attempt が pending** / finalized attempt に別 identity の active intent が残っている / intent status が unknown。いずれも **intent 行へ reason code と observed 値(claim generation・fence generation・fence identity)と `blocked_at_utc` を保存**し、呼び出し側へ `RepairOutcome.BLOCKED` + `RepairFailureCode`(`fence_ahead` / `fence_intent_mismatch` / `takeover_intent_missing` / `claim_intent_mismatch` / `multiple_active_intents` / `takeover_identity_mismatch` / `takeover_protocol_corrupt` / `terminal_intent_with_pending_attempt`)を返す。**blocked の間は Broker 照会も Execution 更新も行わない**(attempt は pending のまま人手の判断を待つ)。

処理を継続できる場合は、state history の**構造化 provenance 列 `repair_attempt_id` / `repair_generation`**(reason 文字列の解析はしない)を確認し、既に適用済みなら**元の attempt ID のまま** `applied` へ finalize、未適用なら**新 owner として fresh Broker 再照会 + eligibility 再検証を最初からやり直す**。**「history に無い」だけを根拠に `failed` へ確定することはしない。**

元の run / discrepancy が削除・改竄されていた場合は fail-closed で復旧を拒否する(`ReconciliationRepairError`)。broker-only target のように execution order を持たない reservation は fence を使わず claim promotion だけで takeover する。

### intent の保持方針(retention)

intent 行は**物理削除しない**。`completed` / `cancelled` / `blocked` のすべてが残るため、「どの実行がどの generation を所有し、何を適用したか」という identity chain を attempt finalize 後も監査できる。増加量は **1 attempt あたり takeover 回数分**(通常 0、crash 後で 1)であり、attempt 自体が run ごとの append であることを考えると無視できる。active intent の検索は partial UNIQUE index、attempt 単位の履歴は `(repair_attempt_id, source_generation, takeover_id)` の index で引く。将来 cleanup が必要になった場合も、**terminal な intent だけ**を対象にできる(active intent を消すと fencing の根拠が失われる)。

### repair eligibility の再検証(保存結果を無条件に信用しない)

適用直前に毎回、以下を再検証する。いずれかが崩れていれば内部状態は変更されず、typed な結果(`RepairOutcome` + 安定 `failure_code`)として finalize される。

1. 現在の内部行を再読取し、`client_order_id` の対応が保存時の観測と一致すること(不一致 = `rejected`)。
2. **Broker へ再照会**(`get_order`)。**`already_applied` の判定を含むすべての結論の前に必ず実行**する。照会失敗(`BrokerError` / timeout / not found / Broker 境界契約違反)は `query_failed`(型付き `BrokerErrorCode`。#103 と同じ boundary 方針)であり、`already_applied` を照会なしで確定することはない。
3. **revision gate**: discrepancy は観測時点の注文 **revision**(execution store の履歴行数。same-state 更新でも必ず増える)を保持しており、現在 revision と一致しない = **観測後に何らかの変更があった**。UNKNOWN → UNKNOWN の履歴追加・snapshot refresh・部分約定の増加・timestamp のみの更新も state 文字列が同じまま検出される。変化していた場合: 既に観測どおりの Broker 状態へ到達済みで、かつ **3-way 検証**(保存済み discrepancy × 現在行 / anchored snapshot × fresh snapshot が state・client/broker ID・数量・価格・fills・reject reason の全フィールドで一致。fresh の timestamp 逆行は不可)を通ったときのみ `already_applied`(no-op)。それ以外は `stale` / `conflict`(古い観測で新しい状態を巻き戻さない)。revision を持たない v1 時代の観測は常に `stale`(新しい照合を実行する)。
4. fresh snapshot が保存済み観測と一致すること。状態が異なる(UNKNOWN へ戻った場合を含む)・broker 参照 ID が変わった場合は `conflict`(新しい reconciliation run での再分類が必要)。
5. **既存状態機械・store 契約の再検証**: intent identity 一致、`validate_order_state_transition`、anchored snapshot に対する `validate_order_result_update`、時刻の非逆行。拒否は `rejected`(遷移表は緩和しない)。
6. **Broker 参照 ID を失わない**: 既知の `broker_order_id` の NULL 化・別値への置換は `conflict`。fresh snapshot の ID が別 Execution Request に所有されている場合(DB 全体 UNIQUE)も `conflict`。判定に必要な store 読み取りの失敗は `failed`(fail-closed。修復可能性を推測しない)。

### 適用・冪等性・並行実行(post-commit 安全性)

- 適用は `execution_store.record_order_result(..., expected_current_state=..., expected_revision=観測 revision, repair_fence=RepairFenceToken(...))` の 1 呼び出しのみ。store は `BEGIN IMMEDIATE` トランザクション内で **fence の 5 要素 identity(attempt id / generation / takeover id / owner / claim execution id)+ expected order revision + expected current state** を検証する。いずれかが不一致なら全 rollback。**attempt ID と generation だけでは通らない**(同一 generation の別実行を区別できないため)。
- **takeover 後の旧 worker は、状態も revision もまだ同じであっても commit できない**(fence が別 identity を指しているため)。この拒否は typed な `ExecutionRepairFenceConflictError` で、呼び出し側は**決して `applied` にせず**、`conflict` + `failure_code = repair_fence_conflict` / `claim_lost` として結果化する。ただし state history の provenance が「この attempt は(別実行によって)適用済み」と示す場合だけは `already_applied` を返す — 事実に反する `conflict` を audit に残さないため。
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
- claim / fence / takeover 由来のコードもこの集合に含まれる: `claim_lost`(takeover により claim を失った)/ `repair_fence_conflict`(fence が別 identity を指していた)/ **`BLOCKED` 専用の 8 種**(`takeover_intent_missing` / `takeover_identity_mismatch` / `fence_ahead` / `multiple_active_intents` / `claim_intent_mismatch` / `fence_intent_mismatch` / `takeover_protocol_corrupt` / `terminal_intent_with_pending_attempt`)。`BLOCKED` は**監査行として finalize されない**(attempt は pending のまま)ため、attempts テーブルの outcome CHECK には含めず、durable な記録は intent 行の `status = 'blocked'` + `blocked_reason`(こちらも Enum 生成の CHECK)である。

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
  - **takeover の途中(8 段階のどの commit 境界)** = durable intent が残り、次の worker が**同じ identity で**続きから再開する(fault injection で 8 境界すべてを実測)。
  - **偽の terminal intent(status だけ進んで実体が無い)** = **silent stop にしない**。typed `BLOCKED` として検出・永続化し、人手の介入対象として報告する(status は実体検証つきの専用 API 経由でしか進まないので、この状態は本来 in-band では作れない)。
  - commit 後〜finalize 前 = pending + history の構造化 provenance(`repair_attempt_id` / `repair_generation`)が残り、recovery が**元 attempt ID のまま `applied` 確定**(元の attempt ID・outcome・Broker 時刻は失われない)。
- finalize 失敗時は attempt に `audit_error` を付けて返す + ERROR ログ(`ReconciliationRepairResult.has_audit_failures` で機械検出)。中途半端な内部状態は残らない(store 側は全 rollback)。
- 更新途中の DB 障害は store のトランザクションが全 rollback し、attempt は `failed`(= 注文状態が無変更だと確定した場合のみの分類)として finalize される。

### 照合のみモード(dry-run)

`mode=RepairMode.DRY_RUN` は上記の評価(store 読み取り + Broker 照会)をすべて実行するが、**一切書き込まない**: 注文更新なし・履歴追加なし・**claim / lease / generation なし**・**takeover intent なし**・**fence 行なし**(既存 fence の更新もしない)・監査行なし・DB 作成 / migration なし(v1 監査 DB は migration せず明示的に拒否)・recovery を起動しない・Broker write 呼び出しなし・state file / ファイルシステム副作用なし(**両 DB** のバイト列・mtime・ディレクトリエントリ・journal / WAL / SHM・`user_version` の不変をテストで固定)。dry-run の attempt は `owner_id` / `claim_execution_id` / `generation` / `takeover_id` を**持たない**(モデル不変条件で強制)。dry-run は **BLOCKED 検出も含めて何も永続化しない**(intent status / blocked 行 / blocked reason / claim / fence / history / schema / migration / DB バイト・mtime / WAL・SHM・journal / directory entry / Broker write のいずれも不変)。適用可能な対象は `would_apply` として返る(`applied` とは区別され、永続化もされない)。破損 DB は無変更で拒否する。

## 永続化(`reconciliation_store.py`)

独立 DB `execution/data/broker_reconciliation.sqlite3`(既定。`BROKER_RECONCILIATION_DB_PATH` で上書き。相対パスは `execution/` 基準)。所有コンポーネントは `execution/` のみ。他 store と同居しない理由は既存 store と同じ(各 store が closed-world で schema を検証するため)。加えて、reconciliation 専用 DB に**注文テーブルが存在しない**こと自体が「この store が注文状態を書き換えられない」構造的保証になる。repair 監査行(Issue #104)を第 5 の DB に分けず**この同一 DB に置く**のは、観測と修復が同じ broker-reconciliation コンポーネントの 2 つの監査系列であり、attempt 行から run 行への実 FK を張れるため。

### schema(version 3)

| テーブル | 列 |
|---|---|
| `broker_reconciliation_runs`(WITHOUT ROWID) | `run_id`(PK)/ `mode` / `started_at_utc` / `finished_at_utc` / `checked_order_count` / `broker_listing_error`。v3(Issue #105)で position スコープ summary 列を追加: `checked_position_count` / `checked_trade_count` / `checked_closed_trade_count` / `internal_exposure_error` / `position_listing_error` / `trade_listing_error` / `closed_trade_listing_error` / `excluded_symbols_json`(8 列とも nullable。**全列 NULL ⇔ その run は position スコープ未照合** — v3 以前の行と order-only run。部分 NULL は復元時に `ReconciliationIntegrityError`) |
| `broker_reconciliation_position_discrepancies`(Issue #105 / v3) | `discrepancy_id`(PK AUTOINCREMENT)/ `run_id`(FK → runs)/ `discrepancy_type`(position 系サブセットのみ)/ `target_type`(`execution_order` / `position` / `broker_trade`)/ `target_id` / **`resolution_category`(CHECK で `manual_review` のみ許可)** / `symbol` / `internal_value` / `broker_value` / `execution_request_id`(`execution_order` 対象と 1:1)/ `client_order_id` / `broker_trade_id` / `protection_order_id` / `broker_transaction_ids_json`(close 証拠の transaction id 群。ASCII JSON 配列)/ `detail` / `observed_at_utc`。`UNIQUE (run_id, discrepancy_type, target_type, target_id)`(1 対象が SL と TP の両所見を持てるため、order 系と異なり種別込みの一意性) |
| `broker_reconciliation_discrepancies` | `discrepancy_id`(PK AUTOINCREMENT)/ `run_id`(FK → runs)/ `discrepancy_type` / `target_type` / `target_id` / `resolution_category` / `internal_state` / `broker_state` / `client_order_id` / `broker_order_id` / `broker_error_code` / `detail` / `observed_at_utc` / `internal_revision`(v2 / Issue #104: 観測時点の注文 revision。v1 由来の行は NULL = 自動修復対象外)。同一 run の `(target_type, target_id)` は UNIQUE |
| `broker_reconciliation_repair_attempts`(WITHOUT ROWID、Issue #104 / v2) | `attempt_id`(PK)/ `run_id`(FK → runs)/ `mode` / **`status`(pending / finalized)** / `target_type` / `target_id`(**複合 FK → discrepancies の UNIQUE key** = attempt は必ず実在する観測へ紐づく)/ **claim: `owner_id` / `claim_execution_id` / `generation`(≥ 1)/ `lease_expires_at_utc` / `last_claimed_at_utc`** / `execution_request_id` / `client_order_id` / `reconciliation_internal_state` / `reconciliation_broker_state` / `internal_revision` / `internal_state_before` / `internal_state_after` / `outcome`(pending 中は NULL)/ `broker_order_id` / `broker_error_code` / `failure_code` / `detail` / `observed_at_utc` / `reserved_at_utc` / **`broker_state_updated_at_utc`**(旧 `applied_at_utc`)/ **`finalized_at_utc`** |

| `broker_reconciliation_repair_takeover_intents`(WITHOUT ROWID、Issue #104 / v2) | `takeover_id`(PK)/ `repair_attempt_id`(FK → repairs)/ `source_generation` / `target_generation` / `source_owner_id` / `source_claim_execution_id` / `target_owner_id` / `target_claim_execution_id` / `lease_expires_at_utc` / `status` / `created_at_utc` / `fence_installed_at_utc` / `claim_promoted_at_utc` / `completed_at_utc` / `cancelled_at_utc` / `blocked_at_utc` / `blocked_reason` / `observed_claim_generation` / `observed_fence_generation` / `observed_fence_identity`。`UNIQUE (repair_attempt_id, source_generation)` |

index: `idx_broker_reconciliation_discrepancies_run (run_id, discrepancy_id)`、`idx_broker_reconciliation_position_discrepancies_run (run_id, discrepancy_id)`(v3)、`idx_broker_reconciliation_repair_attempts_run (run_id, status, reserved_at_utc, attempt_id)`(pending 列挙 = recovery の work list がこの index で引ける)、`idx_..._takeover_intents_active (repair_attempt_id) WHERE status IN ('prepared','fence_installed','claim_promoted')`(**active intent は attempt ごとに高々 1 件**を DB が保証)、`idx_..._takeover_intents_attempt (repair_attempt_id, source_generation, takeover_id)`。CHECK 制約がモデル不変条件を二重化する(既知 enum 値のみ、UTC 接尾辞、非負カウント / revision、`query_failure` ⇔ `query_failed` ⇔ `broker_error_code IS NOT NULL`、attempt 側は `mode = 'apply'` のみ・`would_apply` 非永続・`(status='pending') ⇔ (outcome IS NULL)`・pending 行は outcome 系列 NULL・finalized 行は `applied` ⇔ `broker_state_updated_at_utc`・`applied`/`already_applied` ⇔ `internal_state_after` ⇔ `failure_code IS NULL`・`query_failed` ⇔ `broker_error_code`)。

claim 列の CHECK: `generation >= 1`、`owner_id` / `claim_execution_id` / `lease_expires_at_utc` / `last_claimed_at_utc` は **NOT NULL**(APPLY モードの行しか存在しないため、pending / finalized を問わず claim は必須 = 「どの実行がこの結果を確定させたか」が監査に残る)、UTC 接尾辞必須、`(status='finalized') ⇔ (finalized_at_utc IS NOT NULL)`、`failure_code` は **`RepairFailureCode` から生成した値一覧**の `IN (...)`(手書きの重複なし)。malformed な lease / generation・非 UTC・unknown failure code は書き込み時に CHECK で、読み取り時にモデル不変条件で **fail-closed** に落ちる。

attempts 側の `takeover_id` は generation ≥ 2 でのみ NOT NULL(`CHECK ((generation = 1) = (takeover_id IS NULL))`)で、intent 行への FK を張る。intents 側の CHECK は `target_generation = source_generation + 1`、`source_generation >= 1`、`target_claim_execution_id <> source_claim_execution_id`、status × 各 timestamp の整合(`prepared` は fence/claim 時刻を持てない、`claim_promoted` は両方必須、など)、`blocked` ⇔ (`blocked_at_utc` かつ `blocked_reason`)、`cancelled` ⇔ `cancelled_at_utc`、`completed` ⇔ `completed_at_utc`、observed 列は blocked 専用、status / blocked_reason は **Enum から生成した closed set**。

**claim 列の保持方針**: finalize 後も claim 列(および `takeover_id`)は保持する(消さない)。どの owner / claim execution / takeover / generation が確定させたかは監査事実そのものであり、finalized 行はもう takeover 対象にならない(CAS が `status='pending'` を要求する)ため保持による危険はない。intent 行も同様に保持する(上記 retention)。

### 重複方針(明示)

**run 単位の append**。1 run = runs 行 1 件 + discrepancies 行 N 件。**同一内容の観測でも run ごとに独立した行として残す**(dedup・上書き・UPSERT はしない)。これは既存の監査設計(`safety_gate_decisions` は判定を毎回 append、`execution_order_state_history` は観測を毎回 append)と同じ立場であり、「いつ何を観測したか」を再構成可能に保つためである。

一方、**同一 run 内の同一 target は最大1件**であり、モデル不変条件と DB の `UNIQUE (run_id, target_type, target_id)` の両方で固定する。Broker 列挙が client/broker ID を重複して返した場合は broker-only 差分を二重記録せず、列挙全体を曖昧として `broker_listing_error` に残す。

`run_id` は呼び出し側が与える PRIMARY KEY。**既存 run_id での再記録は `ReconciliationRunConflictError` で拒否**し、保存済みの run を書き換えない。したがって同じ照合を何度繰り返しても、内部注文状態も既存の記録も壊れない。

repair attempt(Issue #104)も同じ append の立場で、**2 段階ライフサイクル(pending claim → finalized)**を持つ: すべての試行(適用しなかったものを含む)が独立した監査イベントであり、repair の再実行は新しい attempt 行を claim → finalize するだけで、既存の run / discrepancy / attempt 行を更新しない。既存 `attempt_id` の再利用は **reservation 時点で** `RepairAttemptConflictError`(状態更新の前に拒否される)。

pending 行だけが例外的に **claim 列のみ** in-place 更新される(`claim_repair_attempt()` の takeover CAS = `generation + 1`)。observation / identity / outcome 系列は決して書き換わらない。finalize は **claim 込みの CAS 保護**(pending のみ・ちょうど 1 回・現 claim のみ。二重 finalize は `RepairAttemptConflictError`、claim 不一致は `RepairClaimLostError`、identity 不一致は `ReconciliationIntegrityError`)。pending のまま残った行は crash の証跡として `list_pending_repair_attempts()` で列挙でき、**lease 期限切れのものだけ**が recovery の回収対象になる。dry-run は claim を含め**記録自体を拒否**する(無副作用契約)。

### migration / 初期化

`init_reconciliation_db()` は**冪等**で、pristine な DB(`sqlite_master` 0 件かつ `user_version` 0)にのみ schema を作成し、現行 schema の既存 DB は closed-world 検証のみを行う(既存行は無変更)。migration は**リリース済みの前バージョンと完全一致した DB のみ**を対象に、単一トランザクション内で**段階的に**適用する(v1 → v2 → v3。各段階は additive、最後に現行 schema の完全再検証、途中失敗は全 rollback で元ファイル無変更)。**v2 → v3 migration(Issue #105)**: runs テーブルへの position summary 8 列の `ALTER TABLE ... ADD COLUMN`(共有 DDL 定数により migration 後の DDL は v3 参照 CREATE と token 正規化で一致)+ position discrepancies テーブル + index + version bump。既存 run 行は summary 全列 NULL(= position スコープ未照合)で読み戻され、既存の order discrepancy / repair attempt / takeover intent 行は無変更。order 系テーブルの DDL は v2 と同一のまま(CHECK は凍結サブセットから生成)なので、v2 → v3 でテーブル再構築は発生しない。**v1 → v2 migration(Issue #104)**: **リリース済み v1 と完全一致した DB のみ**を単一トランザクションで additive に migration する(`ALTER TABLE discrepancies ADD COLUMN internal_revision` — SQLite は列をテーブル制約の前・列リスト末尾へ挿入するため migration 後の DDL は v2 参照 CREATE と token 正規化で一致 — → repair テーブル + index → **takeover intents テーブル + 2 index** → version bump → v2 完全再検証 → COMMIT。途中失敗は全 rollback で v1 ファイル無変更。既存 discrepancy 行は `internal_revision = NULL` で読み戻され自動修復対象外)。**repair 監査テーブルと takeover intents テーブルは同じ未リリース変更(v2)で導入されるため、両者の間に中間バージョンは存在しない**(v1 → v2 の 1 段階で両方が作られる)。v1 由来の DB に pending attempt は存在しえないので、**既存行から intent を捏造することはない**(intent は常に本物の takeover からのみ生まれる)。**読み取り経路は migration しない**(v1 DB は `init_reconciliation_db()` を指すエラーで拒否 = dry-run / read-only の無副作用契約を維持)。それ以外(partial schema・制約欠損 lookalike・trigger / view・他コンポーネントの DB)は **DB を変更せず**明示的に失敗する。読み取りは true read-only 接続(`execution/sqlite_ro.py`)で行い、存在しない DB を作成しない。復元時はモデル不変条件で厳格に再検証する(帯域外改竄・malformed pending 行は `ReconciliationIntegrityError` で fail-closed)。

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
run = service.run(run_id="...", include_position_scope=True)   # Issue #105: position / trade / SL・TP スコープを含める
run = service.detect(run_id="...", include_position_scope=True)

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

```python
from execution.reconciliation_runner import build_reconcile_only_broker
from execution.reconciliation_schedule import run_reconciliation_cycle, cycle_lock_path_for

broker, label, query_timeout = build_reconcile_only_broker("oanda-demo")  # 既に facade で包まれている
report = run_reconciliation_cycle(                       # Issue #133: 1 cycle(oneshot)
    broker=broker,                                       # ReconcileOnlyBroker 以外は拒否
    execution_db_path="execution/data/demo/execution_orders.sqlite3",
    reconciliation_db_path="execution/data/demo/broker_reconciliation.sqlite3",
    run_id="recon-cycle-...",
    cycle_lock_path=cycle_lock_path_for("execution/data/demo/broker_reconciliation.sqlite3"),
    repair_enabled=True,                                 # 既定 False(検出のみ)
    repair_owner_id="demo-recon-1",
    broker_timeout_seconds=query_timeout,                # lease 未満であることを構築時に検証
)
report.is_clean            # 差分・部分失敗・blocked takeover・修復エラーが無い
report.recovered_attempts  # crash した前回 cycle から引き継いだ attempt
report.repair_attempts     # この cycle の attempt(auto_repair_candidate のみが進む)
```

`detect()` は reconciliation DB を作成しない(dry-run 的な確認に使える)。`run()` は `init_reconciliation_db()`(冪等)の後に append する。`repair()` は**保存済み run のみ**を入力とし、APPLY モードでは既存 DB を検証(必要なら v1 → v2 migration)してから処理する(DB が存在しなければエラー。新規作成はしない)。run-level 停止時は `ReconciliationRepairRunError.attempts` に完了済み attempt が添付される(監査 DB からも取得可能)。`recover_pending_attempts()` は **lease が切れた pending だけ**を対象にするので、live な repair pass と同時に走らせても安全である(何度呼んでも、回収済み = finalized の attempt には二度と触れない)。

### reissue clearance からの利用(Issue #116)

intent reissue の clearance 観測(`execution/reissue_clearance.py` の `ReissueClearanceService.observe_signal()`)は、本サービスの `run(include_position_scope=True)` を**そのまま**呼んで観測を行い(run_id = `reissue-clearance-<signal>-g<世代>`)、その結果を内部状態 × 観測の組合せで判定して execution DB の clearance evidence(`record_reissue_clearance()`)へ結論を追記する。本サービス側に変更はない — 照合は従来どおり read-only であり、clearance が加えるのは「run の結果をどう解釈するか」というサービス層の判定と、チェーン末尾が未送信の場合の `Broker.get_order()` による明示的な不在照会(照会系のみ)だけである。判定の分類・claim / lease・失効の単調性は [Execution Intent Reissue](execution_reissue.md) が正。

## 非スコープ(Issue #105 時点)

Broker への注文送信・キャンセル・replace・close(修復は内部 DB のみ)、Broker 側のみの注文・position・trade の自動採用、Broker に存在しない内部 position の自動 close、SL / TP 注文の自動再作成・自動変更、Broker 状態の推測(照会失敗・曖昧一致からの状態確定を含む)、無条件の自動決済、position / trade / SL・TP ドメインへの修復拡張(検出 + `manual_review` のみ)、常駐 Bot / Execution 送信 runner への組み込み(定期実行は Issue #133 の独立した oneshot cycle で行う)、reconciliation 経路への submit / cancel 能力追加、Live 接続解禁、position 数量レベルの不一致検出(presence 比較のみ。per-trade 検査が細粒度を担う)。

claim / takeover まわりの非スコープ: **処理中の lease heartbeat**(固定 lease + `broker_timeout < lease_duration` の構築時検証で代替。intent の lease は「再開の排他」としてのみ CAS で取り直す)、hung worker の強制中断(lease 期限切れによる takeover のみ)、worker 間の分散ロックサービス、blocked からの自動復旧(人手の判断が要る)、intent / fence 行の GC(監査証跡として保持)。

## テスト

`execution/tests/test_reconciliation_models.py` / `test_reconciliation_repair_models.py` / `test_reconciliation_store.py` / `test_broker_reconciliation.py` / `test_broker_reconciliation_repair.py` / `test_position_reconciliation.py`(Issue #105)/ `test_reconciliation_runner_cli.py`(Issue #105 CLI)/ `test_reconciliation_schedule.py`(Issue #133 定期実行 cycle と oneshot CLI)/ `scripts/tests/test_reconciliation_units.py`(Issue #133 systemd テンプレートの静的検査)、および `broker/tests/test_fake_broker.py` / `test_broker_interface.py` / `test_broker_models.py` / `test_oanda_demo_adapter.py`。観点は [テスト仕様](../testing/test_specification.md) を参照。すべて `tmp_path` の SQLite と in-memory FakeBroker(OANDA は fake transport / offline stub)のみで、外部通信・Discord・実 DB 更新は発生しない。
