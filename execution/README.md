# execution/

将来の **Live Execution** に向けた ExecutionRequest、注文状態永続化、および Broker 送信直前の fail-closed Safety Gate(Issue #74 / #75)。**実Broker Adapter・Live接続・実発注runnerは未実装。**

明示的な Python package。リポジトリルートから import する:

```python
from execution.execution_models import ExecutionRequest
from execution.safety_gate import SafetyGate, submit_after_safety_gate
from execution.safety_store import SQLiteSafetyDecisionStore
```

| ファイル | 内容 |
|---|---|
| `execution_models.py` | `ExecutionRequest`(immutable dataclass + `Decimal` + UTC): BELIAL 内部の発注意図。`to_order_request()` で `broker.OrderRequest` へ変換する唯一の経路 |
| `execution_store.py` | SQLite 永続化(`execution_orders` + `execution_order_state_history`): 送信フェーズ(created / submitting / submitted)、Broker 注文状態、最新の完全な OrderResult snapshot、状態変更理由、broker_order_id、UNKNOWN 照会対象を管理 |
| `safety_gate.py` | 許可銘柄・方向・数量・ポジション数・SL/TP・価格鮮度・spread・market open/tradable・kill switch・execution mode/接続先を独立検査。Broker/状態照会失敗は拒否し、通過前は `submit_order()` を呼ばない |
| `safety_store.py` | Safety Gate の許可/拒否、機械可読な拒否コード、理由、照会時の account/position情報を SQLite に監査保存 |
| `tests/` | `python -m pytest execution/tests -q`(`tmp_path` SQLiteとFakeBrokerのみ。外部通信・Discord・S3・sleep・実注文なし) |

## Safety Gate の境界

- Strategy は Safety Gate の判定を持たない。
- Broker Adapter は Safety Gate のポリシーを持たない。
- `submit_after_safety_gate()` は判定を保存した後、許可時だけ `ExecutionRequest.to_order_request()` と `Broker.submit_order()` を呼ぶ。
- 1件でも違反がある場合、kill switch有効時、Broker/状態照会失敗時、判定保存失敗時は fail-closed で送信しない。
- FakeBroker の `submitted_requests` により、拒否経路で Broker 送信がゼロ件であることをテストする。

## 位置づけ(誤解しないこと)

- 注文状態の唯一の正は既存の `broker.OrderState` / `validate_order_state_transition()` / `validate_order_result_update()`(Issue #73 / PR #87)。本パッケージは注文状態 Enum・遷移表を新設しない。
- Safety Gate は将来の Live Execution 境界を安全にする部品であり、OANDA Adapter、Live接続、実注文runner、reconciliation runnerは未実装。
- **Paper Signal / Paper Execution は本パッケージを使わない**。
- UNKNOWN は即時再送可能状態ではない。照会対象として扱い、後続照会結果でのみ解決する。

Execution Request の詳細は [docs/architecture/execution_request.md](../docs/architecture/execution_request.md) を参照。
