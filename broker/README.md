# broker/

将来の **Live Execution** に向けた Broker 境界(Issue #73)。境界定義(`broker_interface` / `broker_models`)と FakeBroker は**実発注も外部通信も行わない**。Issue #79 で追加された OANDA Demo Adapter(`oanda_demo_adapter.py`)のみが HTTP を話すが、**接続先は OANDA Demo(fxpractice)endpoint に allowlist で固定**され、Live endpoint は構築時に fail-closed で拒否される。

明示的な Python package。リポジトリルートから import する:

```python
from broker.broker_interface import Broker
from broker.broker_models import OrderRequest, OrderResult
from broker.fake_broker import FakeBroker
from broker.oanda_demo_adapter import OandaDemoAdapter, OandaDemoConfig
```

| ファイル | 内容 |
|---|---|
| `broker_interface.py` | Broker 共通インターフェース(`Protocol`): 注文送信 / キャンセル / 注文照会 / ポジション照会 / 口座状態照会 |
| `broker_models.py` | Broker 非依存の共通モデル(`dataclass` + `Enum` + `Decimal` + UTC): 注文要求 / 注文結果 / 注文状態 / 約定情報 / ポジション / 口座状態 / Broker エラー |
| `fake_broker.py` | 外部通信・DB・sleep・乱数を使わない決定的なテストダブル。client order ID 単位で Scripted Scenario を注入する。呼び出し記録(`submitted_requests` / `cancel_requests`)を持ち、受付系 script(`FillImmediately` / `StayPending` / `PartiallyFill`)は `broker_order_id` を固定値に指定できる(Issue #77: Execution 固定シナリオ試験が期待値比較に使用。未指定なら従来どおり連番 `FAKE-<n>`)。**broker_order_id は注文間で一意**(PR #96 レビュー): 固定 ID と連番 ID は同じ割当済み集合で管理され、割当済み ID の固定指定は `BrokerInvalidRequestError` で拒否、連番は固定 ID と衝突する番号を安全にスキップする。**拒否は原子的**(PR #96 r2): 重複 ID 等で拒否された注文は fill ID・決定的 clock の tick・注文レコード・割当済み ID 集合を一切消費せず、`submitted_requests`(すべての送信**試行**を記録する契約)にのみ残る。submit / cancel は単一 lock 下で実行され、並行 submit でも ID は一意 |
| `oanda_demo_adapter.py` | **OANDA Demo 専用** Broker Adapter(Issue #79 / PR #101)。OANDA v20 REST(fxpractice)と共通モデルの**変換のみ**を担い、Strategy 判定・Execution 判断・Safety Gate・idempotency・Shadow Mode の各責務は一切持たない。設定は環境変数のみ(`OANDA_API_TOKEN` / `OANDA_ACCOUNT_ID` / `OANDA_API_URL` / `OANDA_HTTP_TIMEOUT_SECONDS`)。endpoint は Demo に allowlist 固定(Live は fail-closed)で **redirect は追従しない**(`allow_redirects=False`、3xx は共通エラー化)。**Secrets 非漏洩**: token / account id / endpoint を `repr` から除外、実 account id は `AccountState` へ渡す前にマスク、`connection_identity()` の target は URL でなく定数ラベル `"oanda-demo"`、Broker が echo する `errorMessage` / `rejectReason` からも token / account id を scrub。エラーは共通 taxonomy へ変換(timeout→`BrokerTimeoutError`=UNKNOWN・**自動再送なし**、接続失敗→`BrokerCommunicationError`、401/403→`BrokerAuthenticationError`、429→`BrokerRateLimitError`、送信後 5xx / redirect / 不正 body→outcome unknown として `BrokerTimeoutError`、Broker 拒否→`OrderState.REJECTED` の結果返却、**duplicate client ID 拒否(`CLIENT_ORDER_ID_ALREADY_EXISTS`)→ FakeBroker と同じ `BrokerInvalidRequestError`**、応答がモデル不変条件に反する場合→`BrokerCommunicationError`(裸の `ValueError` を漏らさない))。`client_order_id` は URL-safe charset(`[A-Za-z0-9._-]`、最大128字)を構築前に検証し、OANDA `clientExtensions.id` として送信、**応答側の echo(fill/create/cancel transaction・order object)と要求 ID の一致を検証**してから結果へ束縛する(execution_request_id との 1:1 関連は Issue #74 の execution store が担保)。部分約定は `PARTIALLY_FILLED`(残量継続)/ `CANCELLED`(残量 cancel・reissue 拒否)へ変換。Protocol 外の追加操作として `get_tradable_instruments()` / `get_open_trades()`(共通モデル `TradeInfo`)を持つ |
| `tests/` | `python -m pytest broker/tests -q`(既定はインメモリ・fake transport のみで外部通信なし。`test_oanda_demo_adapter_demo_api.py` の実 Demo API テストは `BELIAL_RUN_OANDA_DEMO_API_TESTS=1` + Demo credentials を設定したときだけ実行される opt-in で、通常 CI では skip) |

## 位置づけ(誤解しないこと)

- Broker interface / FakeBroker は**将来の Live Execution 用の基盤**であり、FakeBroker が存在しても **Live Execution が実装済みになったわけではない**。
- **Paper Execution(`paper-execution/`)は従来どおり Broker 非依存の仮想執行**であり、このパッケージを使わない。
- OANDA **Demo** Adapter は Issue #79 で実装済み(Demo 口座での注文ライフサイクル検証用)。ただし **Live 口座接続・Live Execution runner・Execution 層への組み込みは未実装**: `execution.safety_gate.BROKER_TYPE_BY_MODE` に `demo` は未登録のままであり、`BrokerConnection` / `LiveOrderSubmissionService` から Demo Adapter を使うことはまだできない(表現不能による fail-closed を維持。登録は Execution 統合の別 Issue で行う)。発注前の Safety Gate は Issue #75 で `execution/` に実装済みだが、Broker 実装から独立した検証部品である([Safety Gate 設計](../docs/architecture/safety_gate.md))。
- Issue #75 で本パッケージに追加されたのは**接続 identity の契約のみ**: `broker_models.BrokerConnectionIdentity` と、各 Broker 実装が自身の構築時接続 config から不変の identity を自己申告する `connection_identity()`(FakeBroker は mode `"fake"` 固定 + 構築時 `connection_target`)。これは Issue #73 の 5 操作 Protocol とは別の契約であり、execution 層の型付き `BrokerConnection` が mode / 接続先を **Broker 実体から導出**するために用いる。将来の Adapter も自身の実接続設定から identity を返す義務を負う。
- `runtime_checkable` Protocol の `isinstance()` は属性の存在確認のみ(シグネチャ・型の適合は保証しない)。本リポジトリは静的型検査(mypy / pyright)を実行しておらず、シグネチャ適合を保証する仕組みはない。`tests/` の振る舞いテストが確認するのは FakeBroker の挙動のみで、将来の Adapter は自前のテストで適合を確認する。

設計の詳細は [docs/architecture/broker_interface.md](../docs/architecture/broker_interface.md) を参照。
