# broker/

将来の **Live Execution** に向けた Broker 境界(Issue #73)。**実発注は行わない。外部通信も行わない。**

明示的な Python package。リポジトリルートから import する:

```python
from broker.broker_interface import Broker
from broker.broker_models import OrderRequest, OrderResult
from broker.fake_broker import FakeBroker
```

| ファイル | 内容 |
|---|---|
| `broker_interface.py` | Broker 共通インターフェース(`Protocol`): 注文送信 / キャンセル / 注文照会 / ポジション照会 / 口座状態照会 |
| `broker_models.py` | Broker 非依存の共通モデル(`dataclass` + `Enum` + `Decimal` + UTC): 注文要求 / 注文結果 / 注文状態 / 約定情報 / ポジション / 口座状態 / Broker エラー |
| `fake_broker.py` | 外部通信・DB・sleep・乱数を使わない決定的なテストダブル。client order ID 単位で Scripted Scenario を注入する。呼び出し記録(`submitted_requests` / `cancel_requests`)を持ち、受付系 script(`FillImmediately` / `StayPending` / `PartiallyFill`)は `broker_order_id` を固定値に指定できる(Issue #77: Execution 固定シナリオ試験が期待値比較に使用。未指定なら従来どおり連番 `FAKE-<n>`)。**broker_order_id は注文間で一意**(PR #96 レビュー): 固定 ID と連番 ID は同じ割当済み集合で管理され、割当済み ID の固定指定は `BrokerInvalidRequestError` で拒否(不完全な注文は残らない)、連番は固定 ID と衝突する番号を安全にスキップする。submit / cancel は単一 lock 下で実行され、並行 submit でも ID は一意 |
| `tests/` | `python -m pytest broker/tests -q`(インメモリのみ。一時ファイルも不要) |

## 位置づけ(誤解しないこと)

- Broker interface / FakeBroker は**将来の Live Execution 用の基盤**であり、FakeBroker が存在しても **Live Execution が実装済みになったわけではない**。
- **Paper Execution(`paper-execution/`)は従来どおり Broker 非依存の仮想執行**であり、このパッケージを使わない。
- 実発注・OANDA Adapter は**未実装**。発注前の Safety Gate は Issue #75 で `execution/` に実装済みだが、Broker 実装から独立した検証部品である([Safety Gate 設計](../docs/architecture/safety_gate.md))。
- Issue #75 で本パッケージに追加されたのは**接続 identity の契約のみ**: `broker_models.BrokerConnectionIdentity` と、各 Broker 実装が自身の構築時接続 config から不変の identity を自己申告する `connection_identity()`(FakeBroker は mode `"fake"` 固定 + 構築時 `connection_target`)。これは Issue #73 の 5 操作 Protocol とは別の契約であり、execution 層の型付き `BrokerConnection` が mode / 接続先を **Broker 実体から導出**するために用いる。将来の Adapter も自身の実接続設定から identity を返す義務を負う。
- `runtime_checkable` Protocol の `isinstance()` は属性の存在確認のみ(シグネチャ・型の適合は保証しない)。本リポジトリは静的型検査(mypy / pyright)を実行しておらず、シグネチャ適合を保証する仕組みはない。`tests/` の振る舞いテストが確認するのは FakeBroker の挙動のみで、将来の Adapter は自前のテストで適合を確認する。

設計の詳細は [docs/architecture/broker_interface.md](../docs/architecture/broker_interface.md) を参照。
