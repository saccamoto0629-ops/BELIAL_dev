# execution/

将来の **Live Execution** に向けた ExecutionRequest モデル、注文状態・状態履歴の SQLite 永続化基盤(Issue #74)、および Broker 送信直前の fail-closed **Safety Gate**(Issue #75)。**実発注は行わない。外部通信も行わない。OANDA Adapter・Live 接続・Live Execution runner は未実装。**

明示的な Python package。リポジトリルートから import する:

```python
from execution.execution_models import ExecutionRequest
from execution import execution_store
from execution.safety_gate import BrokerConnection, SafetyGate, SafetyPolicy
from execution.safety_store import SQLiteSafetyDecisionStore
from execution.live_submission import LiveOrderSubmissionService
```

| ファイル | 内容 |
|---|---|
| `execution_models.py` | `ExecutionRequest`(immutable dataclass + `Decimal` + UTC): BELIAL 内部の発注意図。`to_order_request()` で `broker.OrderRequest` へ変換する唯一の経路 |
| `execution_store.py` | SQLite 永続化(`execution_orders` + `execution_order_state_history`): 送信フェーズ(created / submitting / submitted)、Broker 注文状態、**最新の完全な OrderResult snapshot(JSON。history 行へ anchor)**、状態変更理由、broker_order_id(**DB 全体で UNIQUE**)、UNKNOWN 照会対象の抽出。current・snapshot・履歴は同一トランザクションで更新し、更新は **history に anchor された snapshot** を old とした `validate_order_result_update()` で常時検証する。全 read / write 経路が「open → schema 完全構造検証 → トランザクション → restore / update → **commit 前の bundle 再検証**」で動作し(schema は sqlite_master **全 object 集合**の closed-world 比較 — **trigger / view / 予期しない object は存在自体を拒否**、DDL は quoted identifier 等の無害差を吸収する token 正規化比較。不一致は DB 無変更で失敗、commit 前検証の不一致は同一トランザクションで rollback)、復元時も chain / enum / UTC / snapshot anchor / broker_order_id 重複を再検証 |
| `safety_gate.py` | 発注前 Safety Gate(Issue #75): 許可銘柄・方向・数量上限・銘柄別 / 口座全体ポジション上限(件数ベース)・SL 必須 / SL・TP 妥当性・参照価格の鮮度(未来時刻含む)・spread 上限・market open / tradable・kill switch・**Broker 実体から導出する型付き接続 identity**(`BrokerConnection` — mode / target は `connection_identity()` から導出され caller は申告不能。評価のたびに再導出しドリフトを拒否)× 許可接続先の整合を独立検査し、**全違反を機械可読コードで収集・順序保持 dedup** する。Broker 照会(positions / account)の失敗・型不正・stale / 未来 snapshot、bool 以外の kill switch 応答はすべて fail-closed で拒否。`SafetyPolicy` は deep immutable(防御的コピー + read-only view) |
| `safety_store.py` | Safety Gate 判定の SQLite 監査保存(`safety_gate_decisions.sqlite3`。`execution_orders.sqlite3` とは別ファイル): allow / reject・機械可読な拒否コード・詳細・判定時刻・`policy_id`・mode / 接続先・account / position 情報。execution_store と同方針の closed-world schema 検証・pristine 判定・厳格復元。**保存失敗は例外 = 送信されない** |
| `live_submission.py` | **唯一の公認送信経路** `LiveOrderSubmissionService`(Issue #75): `register()` が意図と固定 `client_order_id` を execution store へ 1:1 で永続化し、`submit()` が「意図復元 → Gate 評価 → 判定の耐久保存 + read-back 検証 → created→submitting 遷移の獲得(一度きり)→ 送信 → 結果記録」を実施。二重送信・UNKNOWN 後の再送は `duplicate_submission` で拒否。**監査 store はサービス内部に封印**(コンストラクタは `decision_db_path` パスのみ受理。recorder オブジェクトの注入点なし)。受領済み結果の保存失敗は UNKNOWN へフォールバック記録し、停止・二重失敗で残る submitting/pending も `execution_store.list_unresolved_submissions()` で照会可能 |
| `tests/` | `python -m pytest execution/tests -q`(`tmp_path` 配下の SQLite と FakeBroker のみ。外部通信・Discord・S3・sleep・実注文なし) |

## Safety Gate / 送信サービスの境界

- Strategy は Safety Gate の判定を持たない。Broker Adapter は Safety Gate のポリシーを持たない。raw Broker は型付き `BrokerConnection` としてサービスへ注入され、外へ公開されない。
- `LiveOrderSubmissionService.submit()` は「意図復元 → 評価 → 判定の耐久保存 + read-back → created→submitting 遷移獲得」に**すべて成功した呼び出しだけ**が `ExecutionRequest.to_order_request()` と `Broker.submit_order()` に到達する。
- 1 件でも違反・kill switch 有効・Broker / store 照会失敗・判定保存 / read-back 失敗・二重送信・UNKNOWN 後の再送・想定外例外は fail-closed で送信しない。
- `BrokerConnection` の mode / target は Broker 自身の `connection_identity()`(構築時 config 由来・不変)から**導出**され、mode は具象型と相互検証される: `fake` = FakeBroker のみ、`demo` / `live` は Adapter 型が登録されるまで**構築不能**。
- 迂回は「呼び出し側の注意」ではなく **AST 走査テスト**(`tests/test_submission_boundary.py`)で構造的に禁止する: `submit_order` / `to_order_request` の呼び出し箇所は公認箇所以外に存在できない。この境界は**誤用防止**であり、reflection 等による**故意回避の in-process 防止は言語的に不可能**(実 Adapter 導入時の運用境界で扱う。[設計書の該当節](../docs/architecture/safety_gate.md)参照)。
- FakeBroker の `submitted_requests` により、全拒否経路で Broker 送信がゼロ件であることをテストする。

## 位置づけ(誤解しないこと)

- 注文状態の唯一の正は既存の `broker.OrderState` / `validate_order_state_transition()` / `validate_order_result_update()`(Issue #73 / PR #87)。**本パッケージは注文状態 Enum・遷移表を新設しない。** CREATED / SUBMITTING は注文状態ではなく Execution Request 側の**送信フェーズ**(別フィールド)である。
- Safety Gate は将来の Live Execution 送信経路を安全にする**部品のみ**であり、OANDA Adapter・Live 接続・実注文 runner・reconciliation runner は**未実装**。`ExecutionMode` に `paper` は存在しない(Paper Execution は Broker 境界を使わず、Safety Gate を通らない)。
- **Paper Signal / Paper Execution は本パッケージを使わない**(`tests/test_package_imports.py` が回帰として固定)。
- UNKNOWN は即時再送可能状態ではない。照会対象として抽出し、後続照会結果の記録によってのみ確定状態へ解決する。state-only で記録できるのは UNKNOWN のみで、他のすべての観測は完全な `OrderResult` を `record_order_result` で記録する(stale snapshot による fill 消失・巻き戻しを DB 側 snapshot 検証で拒否)。

設計の詳細は [docs/architecture/execution_request.md](../docs/architecture/execution_request.md)(ExecutionRequest / 永続化)と [docs/architecture/safety_gate.md](../docs/architecture/safety_gate.md)(Safety Gate)を参照。
