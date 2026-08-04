# Broker Interface / FakeBroker(Issue #73)

将来の **Live Execution** に向けた Broker 境界の定義と、外部通信を一切行わないテストダブル **FakeBroker** の仕様。実装は `broker/`(明示的な Python package。`__init__.py` あり)にあり、リポジトリルートから通常の package import で利用する(PR #87)。

```python
from broker.broker_interface import Broker
from broker.broker_models import OrderRequest, OrderResult
from broker.fake_broker import FakeBroker
```

> **重要な位置づけ**
>
> - Broker interface / FakeBroker は**将来の Live Execution 用の基盤(境界の定義)**である。
> - FakeBroker は**外部通信しないテストダブル**である。ネットワーク・実DB・Discord・S3・sleep・乱数を使わない。
> - **Paper Execution(`paper-execution/`)は従来どおり Broker 非依存の仮想執行**であり、本境界を使わない・使わせない。
> - **OANDA Demo Adapter は Issue #79 で実装済み**(下記「OANDA Demo Adapter」節)。ただし **Live 口座接続・Live Execution runner・Execution 層への組み込み(`BROKER_TYPE_BY_MODE` への `demo` 登録)は未実装**であり、FakeBroker / Demo Adapter が存在しても Live Execution が実装済みになったわけではない。発注前の Safety Gate は Issue #75 で `execution/` に実装済み([Safety Gate](safety_gate.md))だが、Broker 実装からは独立した検証部品であり、本境界には何も追加しない。

## 依存方向

```text
Strategy / Paper Signal
    ↓
(将来の) Execution use case
    ↓
Broker interface / 共通モデル   ← broker/broker_interface.py, broker/broker_models.py
    ↑
FakeBroker                      ← broker/fake_broker.py(テストダブル)
    ↑
OANDA Demo Adapter              ← broker/oanda_demo_adapter.py(Issue #79。Demo 専用)
```

- Strategy / Paper Signal / Paper Execution から FakeBroker・OANDA Demo Adapter を**直接 import しない**(`broker/tests/test_broker_interface.py` が回帰として固定)。
- FakeBroker と OANDA Demo Adapter は Broker interface / 共通モデルに依存する。逆方向の依存はない。
- 「(将来の) Execution use case」のうち、ExecutionRequest(BELIAL 内部の発注意図)と注文状態の永続化基盤は Issue #74 で `execution/` に実装済み([Execution Request / Order Persistence](execution_request.md))。依存は `execution → broker` の一方向のみで、本パッケージは `execution/` を知らない。

## Broker interface(`broker_interface.py`)

`typing.Protocol`(`runtime_checkable`、構造的型付け)。実装側に継承関係を要求しない。

> **runtime check の限界(PR #87)**: `runtime_checkable` な Protocol の `isinstance()` は**必要な属性(メソッド)が存在すること**しか確認せず、引数型・戻り値型の適合は保証しない。本リポジトリは mypy / pyright 等の静的型検査を実行していないため、**シグネチャ適合を保証する仕組みはない**。`broker/tests/` の振る舞いテストが確認するのは FakeBroker の具体的な挙動のみであり、将来の Adapter 実装は自前のテスト(および必要なら型検査)で適合を確認する必要がある。

| 操作 | メソッド | 戻り値 |
|---|---|---|
| 注文送信 | `submit_order(request: OrderRequest)` | `OrderResult` |
| 注文キャンセル | `cancel_order(client_order_id: str)` | `OrderResult` |
| 注文照会 | `get_order(client_order_id: str)` | `OrderResult` |
| ポジション照会 | `get_positions()` | `tuple[PositionInfo, ...]` |
| 口座状態照会 | `get_account_state()` | `AccountState` |

境界のキーは**呼び出し側が生成する `client_order_id`**。Broker が採番する `broker_order_id` は結果側にのみ現れる(意味の異なる ID を同一フィールドに混在させない)。Adapter は内部で自 Broker の注文 ID へマッピングする。

## 共通モデル(`broker_models.py`)

すべて `dataclass(frozen=True)` + `Enum` + `Decimal`(価格・数量・金額)+ timezone-aware UTC `datetime`。Paper Execution の float / TypedDict 規約は既存レイヤーのものであり、本境界は新規設計として Decimal を採用する(Issue #73)。

- `OrderRequest` — client_order_id / symbol / side / order_type(MARKET, LIMIT)/ quantity / limit_price / created_at_utc。生成時に検証(`BrokerInvalidRequestError`)。
- `OrderResult` — 注文スナップショット。`requested_quantity` / `filled_quantity` / `remaining_quantity`(= requested − filled を検証)、`requested_price` と `average_fill_price` / `fills`(約定明細)を**別々に保持**し、約定価格の乖離(`price_deviation`)をテストで比較できる。構築時のクロスフィールド検証(`ValueError`): LIMIT は `requested_price` 必須・MARKET は `None` 必須、`pending`(Broker 未確認)は `broker_order_id` 禁止、`fill_id` は snapshot 内で一意(重複 fill による同一約定の二重計上を構築時に拒否。更新時も追加 fill の ID 重複を検証)。
- `Fill` — 約定明細(fill_id / price / quantity / filled_at_utc)。
- `PositionInfo` / `AccountState` — Broker 側スナップショット。
- `TradeInfo` — Broker 側 open trade スナップショット(Issue #79)。trade は Broker が管理する個別建玉、`PositionInfo` は symbol×side の集約であり、両者は別語彙のまま統合しない。
- Broker 固有フィールドは共通モデルへ**漏らさない**。Broker 固有レスポンス → 共通モデルの変換は Adapter 内部で完結させる(OANDA Demo Adapter は Issue #79 でこの契約に従って実装済み)。
- エラー分類には Issue #79 で実 endpoint 由来の 2 分類が追加された: `BrokerAuthenticationError`(`authentication_failure`)/ `BrokerRateLimitError`(`rate_limited`)。いずれも Broker 非依存の分類であり、HTTP status やベンダー固有 payload は Adapter 内で変換してから境界を越える。

### 注文状態(`OrderState`)

```text
pending → accepted → partially_filled → filled
   |          |            |
   |          |            +→ cancelled / unknown
   |          +→ cancelled / rejected / unknown
   +→ (accepted / partially_filled / filled / cancelled / rejected / unknown)

unknown → (accepted / partially_filled / filled / cancelled / rejected)  ※後続照会で解決
terminal = filled / cancelled / rejected(再遷移なし)
```

- `unknown` は**通信タイムアウト等で最終状態が確認できない**ケース専用。Broker 側で受理された可能性を否定できないため、「未発注」「拒否」へ安易に確定しない。`rejected`(Broker の確定的拒否)とは別状態。
- **`unknown` の契約(PR #87)**: `unknown` が限定するのは「Broker 側の確定が取れていない」ことであり、約定量ではない。`unknown` スナップショットは**既知の fill を保持できる**(0 件・部分・要求全量のいずれも可。`0 <= filled_quantity <= requested_quantity`、fills 合計との一致は他状態と同じく検証される)。`broker_order_id` は有無どちらも許容。`unknown` が禁じるのは最終結果の断定のみで、後続照会で確定状態へ解決するまで `filled` / `rejected` / 「未発注」へ確定してはならない。
- 遷移の閉集合は `ORDER_STATE_TRANSITIONS` / `validate_order_state_transition()` が固定する。
- **同一状態の更新(PR #87)**: Broker polling は同じスナップショットを繰り返し観測するため、`validate_order_state_transition(old, new)` は **`old == new` を全状態で許可**する(terminal 含む。`filled → filled` は許可、`filled → cancelled` は引き続き拒否)。`ORDER_STATE_TRANSITIONS` は「異なる状態への変更」のみを列挙する。
- **スナップショット更新の不変条件(PR #87)**: 状態ペアの検証に加え、`validate_order_result_update(old, new)` が更新の**内容**を検証する。(1) `client_order_id` / `symbol` / `side` / `order_type` / `requested_quantity` / `requested_price` は更新前後で不変、(2) `filled_quantity` は単調非減少・`remaining_quantity` は単調非増加、(3) `old.fills` は `new.fills` の prefix としてすべて保持(削除・並べ替え・同一 `fill_id` の内容変更を禁止。追加 fill が既存 `fill_id` を再利用することも禁止)、(4) `broker_order_id` は `None → 値` のみ許可(`値 → None`、`値A → 値B` は禁止)、(5) `updated_at_utc` は全更新で単調非減少、(6) **terminal 状態の same-state 再観測は `updated_at_utc` 以外の全フィールドが不変**(確定済みの `average_fill_price` や `reject_reason` を後から書き換えることはできない)。**UNKNOWN の解決先は new 自体の構築 invariant と更新不変条件の両方**を満たす必要がある — 例: 部分約定を保持した `unknown` は `partially_filled` / `filled` / `cancelled` へは解決できるが `accepted` / `rejected` へは戻れず、全量 fill を保持した `unknown` は `filled` のみへ解決できる。型不正は `BrokerInvalidRequestError`、不変条件違反は `ValueError`。FakeBroker のキャンセル経路もこの検証を通る。

### エラー方針(例外 + Result の組み合わせ)

- Broker の**確定的な業務回答**(受理 / 約定 / 部分約定 / 拒否)は `OrderResult` の状態として**返す**。拒否は `OrderState.REJECTED` + `reject_reason`。
- **インフラ障害・操作失敗**は `BrokerErrorCode` を持つ型付き例外として**送出する**(文字列判定をさせない): `BrokerTimeoutError`(timeout。注文状態は `unknown` に)/ `BrokerCommunicationError`(communication failure。リクエスト未到達)/ `BrokerCancellationError`(cancellation failure)/ `BrokerInvalidRequestError`(invalid request)/ `BrokerOrderNotFoundError`(not found)。
- **注文拒否(rejected)とタイムアウト、通信障害はそれぞれ別に区別される**(拒否=確定状態、タイムアウト=`unknown`、通信失敗=未到達)。
- **検証の境界(PR #87)**: caller / adapter から渡された**フィールド・型の不正**(enum の代わりの生文字列、tuple でない fills、空の reject_reason 等)は `BrokerInvalidRequestError`、**モデル内部の整合性違反**(数量の算術、状態×fill の制約、reject_reason と状態の対応)は `ValueError`。

## FakeBroker(`fake_broker.py`)

インメモリ・決定的なテストダブル。`client_order_id` 単位で Scripted Scenario を注入する(汎用シナリオエンジンではない)。

```python
broker = FakeBroker()
broker.script_order("c-1", FillImmediately(price=Decimal("2400.5")))   # 即時約定(価格乖離の注入も可)
broker.script_order("c-2", StayPending())                               # 未約定(accepted のまま)
broker.script_order("c-3", PartiallyFill(fills=((Decimal("2400"), Decimal("3")),)))  # 部分約定
broker.script_order("c-4", RejectOrder(reason="insufficient margin"))   # 拒否
broker.script_order("c-5", TimeoutOrder())                              # タイムアウト(即時再現・実待機なし)
broker.script_order("c-6", FailCommunication())                         # 通信失敗(注文は記録されない)
broker.script_cancel("c-2", FailCancellation())                         # キャンセル失敗
broker.set_positions([...]); broker.set_account_state(...)              # 照会結果の注入
```

- 未スクリプト注文は `default_script`(既定: 指値価格で即時全量約定)。約定価格の選択は truthiness ではなく明示的な `is not None` 判定(`script.price` → `request.limit_price` → `default_fill_price` の順)。
- **設定ミスは即時に失敗する(PR #87)**: Scenario dataclass は構築時に値を検証し(価格は正の有限 Decimal、reason は非空文字列、`PartiallyFill.fills` は非空 tuple の (Decimal, Decimal) ペア)、設定 API(`script_order` / `script_cancel` / `set_positions` / `set_account_state`)も引数の型を検証する。いずれも `BrokerInvalidRequestError` を送出し、submit / cancel 実行まで設定ミスを遅延させない(注文数量との比較のみ submit 時)。
- タイムアウトは `BrokerTimeoutError` を**即時に**送出し、注文を `unknown` として記録する(後続の `get_order` で「拒否ではない」ことを検証できる)。sleep・実時間待機・乱数・ネットワークは使わない。時刻は決定的な内部クロック(固定基準時刻 + 連番秒)または注入 `clock` から得る。
- 単体テストは一時ファイルすら不要のインメモリ動作(`broker/tests/` 参照。socket を無効化した検証あり)。

## Paper Execution との境界

- Paper Execution は保存済み Paper Signal を入力とする独立した仮想執行レイヤーであり、**本境界の追加による変更は一切ない**(FakeBroker への置き換え、Position Store の Broker 照会化、Signal→Broker 直接呼び出しはすべて禁止)。
- Paper Execution の仮想約定モデル(`position_models.py`: long/short・pending/open/closed/...)と本境界の注文モデル(buy/sell・accepted/filled/...)は**別ドメインの語彙**であり統合しない。

## OANDA Demo Adapter(`oanda_demo_adapter.py`、Issue #79)

**Demo 専用**の `Broker` Protocol 実装。OANDA v20 REST(fxpractice)と `broker_models` 共通モデルの**変換のみ**を担う translation layer であり、Strategy 判定・Execution 判断・Safety Gate 判定・idempotency 判断・Shadow Mode 判定は一切持ち込まない(それらは既存の execution 層の責務)。

- **Demo 限定(fail-closed)**: 接続先は `OANDA_API_URL`(既定 `https://api-fxpractice.oanda.com`)を **allowlist で検証**し、Live host(`fxtrade` を含む)もその他の未知 URL も `OandaDemoConfig` 構築時に `ValueError` で起動拒否する。Live へ到達できる設定は表現不能。さらに **redirect は追従しない**(PR #101): 既定 transport は `allow_redirects=False` で、3xx 応答は接続を demo endpoint 外へ引き出せず共通エラーへ変換される(submit 中は outcome unknown として `BrokerTimeoutError`、読み取りは `BrokerCommunicationError`)。
- **Secrets**(PR #101 で強化): `OANDA_API_TOKEN` / `OANDA_ACCOUNT_ID` / `OANDA_API_URL` は環境変数のみ(`OandaDemoConfig.from_env()`)。token / account id / **endpoint** を `repr` から除外。Adapter はログ出力を一切持たず、例外メッセージは静的文言+Broker の短い reason 文字列のみで構成し、**Broker が echo した reason 内の token / account id も scrub** する(transport 例外は `from None` で再送出し、URL を含むライブラリ例外を traceback へ漏らさない)。境界を越える値も秘匿する: `AccountState.account_id` には応答の account id を構成値と照合したうえで**マスクした識別子**を渡し、`connection_identity().target` は URL ではなく**定数ラベル `"oanda-demo"`**。
- **操作**: 口座情報(`get_account_state`)/ tradable instrument 確認(`get_tradable_instruments()`)/ **open trade 照会(`get_open_trades()` → 共通モデル `TradeInfo`)**/ 成行注文(FOK)・指値注文(GTC)の送信(`submit_order`)/ 注文照会(`get_order`)/ キャンセル(`cancel_order`)/ open position 照会(`get_positions`)/ fill・transaction 結果の取得(FILLED 注文は filling transaction を照会して `Fill` へ変換)。`get_tradable_instruments` / `get_open_trades` は Issue #73 の 5 操作 Protocol 外の Adapter 追加操作。STOP 等の高度な注文種別は非対応。**部分約定**(PR #101): submit 応答の fill 数量が要求未満の場合、残量が継続していれば `PARTIALLY_FILLED`、残量が cancel された・reissue が拒否された場合は部分 fill を保持した `CANCELLED` へ変換する。fill 数量 0 または要求超過は不正応答として `BrokerCommunicationError`。
- **client ID 関連付け**(PR #101 で双方向化): `client_order_id` は URL path に埋め込まれるため **URL-safe charset(`[A-Za-z0-9._-]`、先頭は英数字、最大 128 字)を送信・照会・取消の前に検証**する。送信時に OANDA `clientExtensions.id` として渡し、`get_order` / `cancel_order` は `@<client id>` 指定子で照会する。**応答側の echo も検証**: fill / create / cancel transaction の `clientOrderID` / `clientExtensions.id`、および order object の `clientExtensions.id` が要求 ID と一致しない・欠損している応答は `BrokerCommunicationError` で拒否し、他人の注文を `execution_request_id`(Issue #74 の 1:1 束縛)へ誤って結び付けない。
- **エラー変換**(共通 taxonomy 以外は境界を越えない): transport timeout → `BrokerTimeoutError`(注文状態 UNKNOWN・**自動再送なし、1 回で送信を打ち切る**)/ 接続確立失敗 → `BrokerCommunicationError` / HTTP 401・403 → `BrokerAuthenticationError` / HTTP 429 → `BrokerRateLimitError`(Adapter は retry しない)/ **submit 後の 5xx・redirect・不正 body・分類不能な transport 失敗 → `BrokerTimeoutError`**(サーバは要求を受領し得たため「未発注」へ降格させない。読み取り系の同種失敗は `BrokerCommunicationError`)/ OANDA の注文拒否(`orderRejectTransaction`)→ 例外ではなく `OrderState.REJECTED` + `reject_reason` の `OrderResult`。ただし **`CLIENT_ORDER_ID_ALREADY_EXISTS` は Broker 境界の duplicate 契約(Issue #73 / FakeBroker と同一)に合わせ `BrokerInvalidRequestError`** / 不正要求 → `BrokerInvalidRequestError` / 未知の注文指定子 → `BrokerOrderNotFoundError` / キャンセル拒否 → `BrokerCancellationError`(404 は `get_order` で「注文が存在しない」と「terminal でキャンセル不能」を判別)/ **応答が共通モデルの不変条件に反する場合 → `BrokerCommunicationError`(裸の `ValueError` を境界外へ漏らさない)**。
- **connection identity**(Issue #75): `connection_identity()` は構築時 config に由来する不変の `BrokerConnectionIdentity(mode="demo", target="oanda-demo")` を自己申告する(target は非秘密の定数ラベル。PR #101)。
- **HTTP transport は注入可能**: 単体テストは fake transport で全変換経路を検証し、ネットワークへ一切出ない(socket 無効化下でも PASS)。実 Demo API を叩くテストは `broker/tests/test_oanda_demo_adapter_demo_api.py` に分離され、`BELIAL_RUN_OANDA_DEMO_API_TESTS=1` + Demo credentials の明示 opt-in でのみ実行される(通常 CI では skip)。
- **Execution 層への組み込みは未実装**: `execution.safety_gate.BROKER_TYPE_BY_MODE` に `demo` は登録されておらず、`BrokerConnection(broker=OandaDemoAdapter(...))` は構築できない(fail-closed 維持)。登録・runner 対応・定期照合(reconciliation)は Live/統合の別 Issue で設計する。Adapter は Gate のロジックを持たず、送信経路は将来も `LiveOrderSubmissionService` 経由のみ([Safety Gate](safety_gate.md))。

## 非スコープ(Issue #79 時点で未実装)

Live 口座接続・Live 用認証情報、`BROKER_TYPE_BY_MODE` への `demo` 登録(Execution 統合)、Live Execution runner、ロット計算、レバレッジ計算、資金管理、Broker との定期照合、再接続ループ、pricing/stream API、Discord 通知。(Safety Gate は Issue #75 で `execution/` に実装済み。)
