# execution/

将来の **Live Execution** に向けた ExecutionRequest モデルと注文状態・状態履歴の SQLite 永続化基盤(Issue #74)。**実発注は行わない。外部通信も行わない。**

明示的な Python package。リポジトリルートから import する:

```python
from execution.execution_models import ExecutionRequest
from execution import execution_store
```

| ファイル | 内容 |
|---|---|
| `execution_models.py` | `ExecutionRequest`(immutable dataclass + `Decimal` + UTC): BELIAL 内部の発注意図。`to_order_request()` で `broker.OrderRequest` へ変換する唯一の経路 |
| `execution_store.py` | SQLite 永続化(`execution_orders` + `execution_order_state_history`): 送信フェーズ(created / submitting / submitted)、Broker 注文状態、**最新の完全な OrderResult snapshot(JSON)**、状態変更理由、broker_order_id(**DB 全体で UNIQUE**)、UNKNOWN 照会対象の抽出。current・snapshot・履歴は同一トランザクションで更新し、更新は DB 上の snapshot を old とした `validate_order_result_update()` で常時検証する。schema は完全構造検証(不一致は DB 無変更で失敗)、復元時も chain / enum / UTC / snapshot 整合を再検証 |
| `tests/` | `python -m pytest execution/tests -q`(`tmp_path` 配下の SQLite のみ。外部通信・Discord・S3・sleep・実注文なし) |

## 位置づけ(誤解しないこと)

- 注文状態の唯一の正は既存の `broker.OrderState` / `validate_order_state_transition()` / `validate_order_result_update()`(Issue #73 / PR #87)。**本パッケージは注文状態 Enum・遷移表を新設しない。** CREATED / SUBMITTING は注文状態ではなく Execution Request 側の**送信フェーズ**(別フィールド)である。
- 本パッケージは**将来の Live Execution 用のモデル・状態永続化基盤のみ**であり、Live Execution 本体・Broker API Adapter・実注文・Safety Gate・reconciliation runner は**未実装**。
- **Paper Signal / Paper Execution は本パッケージを使わない**(`tests/test_package_imports.py` が回帰として固定)。
- UNKNOWN は即時再送可能状態ではない。照会対象として抽出し、後続照会結果の記録によってのみ確定状態へ解決する。state-only で記録できるのは UNKNOWN のみで、他のすべての観測は完全な `OrderResult` を `record_order_result` で記録する(stale snapshot による fill 消失・巻き戻しを DB 側 snapshot 検証で拒否)。

設計の詳細は [docs/architecture/execution_request.md](../docs/architecture/execution_request.md) を参照。
