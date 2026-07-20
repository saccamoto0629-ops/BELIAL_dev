# broker/

将来の **Live Execution** に向けた Broker 境界(Issue #73)。**実発注は行わない。外部通信も行わない。**

| ファイル | 内容 |
|---|---|
| `broker_interface.py` | Broker 共通インターフェース(`Protocol`): 注文送信 / キャンセル / 注文照会 / ポジション照会 / 口座状態照会 |
| `broker_models.py` | Broker 非依存の共通モデル(`dataclass` + `Enum` + `Decimal` + UTC): 注文要求 / 注文結果 / 注文状態 / 約定情報 / ポジション / 口座状態 / Broker エラー |
| `fake_broker.py` | 外部通信・DB・sleep・乱数を使わない決定的なテストダブル。client order ID 単位で Scripted Scenario を注入する |
| `tests/` | `python -m pytest broker/tests -q`(インメモリのみ。一時ファイルも不要) |

## 位置づけ(誤解しないこと)

- Broker interface / FakeBroker は**将来の Live Execution 用の基盤**であり、FakeBroker が存在しても **Live Execution が実装済みになったわけではない**。
- **Paper Execution(`paper-execution/`)は従来どおり Broker 非依存の仮想執行**であり、このパッケージを使わない。
- 実発注・OANDA Adapter・Safety Gate は**未実装**。

設計の詳細は [docs/architecture/broker_interface.md](../docs/architecture/broker_interface.md) を参照。
