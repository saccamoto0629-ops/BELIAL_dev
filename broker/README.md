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
| `fake_broker.py` | 外部通信・DB・sleep・乱数を使わない決定的なテストダブル。client order ID 単位で Scripted Scenario を注入する |
| `tests/` | `python -m pytest broker/tests -q`(インメモリのみ。一時ファイルも不要) |

## 位置づけ(誤解しないこと)

- Broker interface / FakeBroker は**将来の Live Execution 用の基盤**であり、FakeBroker が存在しても **Live Execution が実装済みになったわけではない**。
- **Paper Execution(`paper-execution/`)は従来どおり Broker 非依存の仮想執行**であり、このパッケージを使わない。
- 実発注・OANDA Adapter は**未実装**。発注前の Safety Gate は Issue #75 で `execution/` に実装済みだが、Broker 実装から独立した検証部品であり、本パッケージには何も追加しない([Safety Gate 設計](../docs/architecture/safety_gate.md))。
- `runtime_checkable` Protocol の `isinstance()` は属性の存在確認のみ(シグネチャ・型の適合は保証しない)。本リポジトリは静的型検査(mypy / pyright)を実行しておらず、シグネチャ適合を保証する仕組みはない。`tests/` の振る舞いテストが確認するのは FakeBroker の挙動のみで、将来の Adapter は自前のテストで適合を確認する。

設計の詳細は [docs/architecture/broker_interface.md](../docs/architecture/broker_interface.md) を参照。
