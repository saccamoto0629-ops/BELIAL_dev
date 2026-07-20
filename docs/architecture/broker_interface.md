# Broker Interface / FakeBroker(Issue #73)

将来の **Live Execution** に向けた Broker 境界の定義と、外部通信を一切行わないテストダブル **FakeBroker** の仕様。実装は `broker/` にある。

> **重要な位置づけ**
>
> - Broker interface / FakeBroker は**将来の Live Execution 用の基盤(境界の定義)**である。
> - FakeBroker は**外部通信しないテストダブル**である。ネットワーク・実DB・Discord・S3・sleep・乱数を使わない。
> - **Paper Execution(`paper-execution/`)は従来どおり Broker 非依存の仮想執行**であり、本境界を使わない・使わせない。
> - **実発注・OANDA Adapter・Safety Gate・Live Execution runner は未実装**である。FakeBroker が存在しても Live Execution が実装済みになったわけではない。

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
(将来の) OANDA Adapter          ← 未実装
```

- Strategy / Paper Signal / Paper Execution から FakeBroker・将来の OANDA 実装を**直接 import しない**(`broker/tests/test_broker_interface.py` が回帰として固定)。
- FakeBroker と将来の Adapter は Broker interface に依存する。逆方向の依存はない。

## Broker interface(`broker_interface.py`)

`typing.Protocol`(`runtime_checkable`、構造的型付け)。実装側に継承関係を要求しない。

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
- `OrderResult` — 注文スナップショット。`requested_quantity` / `filled_quantity` / `remaining_quantity`(= requested − filled を検証)、`requested_price` と `average_fill_price` / `fills`(約定明細)を**別々に保持**し、約定価格の乖離(`price_deviation`)をテストで比較できる。
- `Fill` — 約定明細(fill_id / price / quantity / filled_at_utc)。
- `PositionInfo` / `AccountState` — Broker 側スナップショット。
- Broker 固有フィールドは共通モデルへ**漏らさない**。Broker 固有レスポンス → 共通モデルの変換は将来の Adapter 内部で完結させる。

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
- 遷移の閉集合は `ORDER_STATE_TRANSITIONS` / `validate_order_state_transition()` が固定する。

### エラー方針(例外 + Result の組み合わせ)

- Broker の**確定的な業務回答**(受理 / 約定 / 部分約定 / 拒否)は `OrderResult` の状態として**返す**。拒否は `OrderState.REJECTED` + `reject_reason`。
- **インフラ障害・操作失敗**は `BrokerErrorCode` を持つ型付き例外として**送出する**(文字列判定をさせない): `BrokerTimeoutError`(timeout。注文状態は `unknown` に)/ `BrokerCommunicationError`(communication failure。リクエスト未到達)/ `BrokerCancellationError`(cancellation failure)/ `BrokerInvalidRequestError`(invalid request)/ `BrokerOrderNotFoundError`(not found)。
- **注文拒否(rejected)とタイムアウト、通信障害はそれぞれ別に区別される**(拒否=確定状態、タイムアウト=`unknown`、通信失敗=未到達)。

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

- 未スクリプト注文は `default_script`(既定: 指値価格で即時全量約定)。
- タイムアウトは `BrokerTimeoutError` を**即時に**送出し、注文を `unknown` として記録する(後続の `get_order` で「拒否ではない」ことを検証できる)。sleep・実時間待機・乱数・ネットワークは使わない。時刻は決定的な内部クロック(固定基準時刻 + 連番秒)または注入 `clock` から得る。
- 単体テストは一時ファイルすら不要のインメモリ動作(`broker/tests/` 参照。socket を無効化した検証あり)。

## Paper Execution との境界

- Paper Execution は保存済み Paper Signal を入力とする独立した仮想執行レイヤーであり、**本境界の追加による変更は一切ない**(FakeBroker への置き換え、Position Store の Broker 照会化、Signal→Broker 直接呼び出しはすべて禁止)。
- Paper Execution の仮想約定モデル(`position_models.py`: long/short・pending/open/closed/...)と本境界の注文モデル(buy/sell・accepted/filled/...)は**別ドメインの語彙**であり統合しない。

## 将来の OANDA Adapter の拡張点

1. `broker/` 配下(または独立コンポーネント)に Adapter モジュールを追加し、`Broker` Protocol を構造的に実装する。
2. OANDA 固有のレスポンス→ `broker_models` 共通モデルへの変換は Adapter 内部で完結させ、OANDA 固有フィールドを境界の外へ出さない。
3. HTTP タイムアウト・接続断は `BrokerTimeoutError` / `BrokerCommunicationError` へマッピングし、タイムアウト後の注文状態は `unknown` として扱う(後続照会・照合で解決)。
4. 実発注に進む場合も Safety Gate・認証情報管理・再接続・定期照合は本 Issue の非スコープであり、別 Issue で設計する。

## 非スコープ(Issue #73 時点で未実装)

OANDA Demo/Live API 連携、実 HTTP クライアント、実発注、ロット計算、レバレッジ計算、資金管理、Safety Gate、Broker との定期照合、再接続ループ、認証情報管理、Discord 通知、Live Execution runner。
