# Shadow Mode(Issue #78)

実際の Signal 由来の ExecutionRequest・実際の設定(SafetyPolicy)・実際の Safety Gate という**本番相当の判定経路**を使用しながら、**Broker へは一切送信せず**、生成された内容と判定結果を検証記録として保存する観察モード。実装は `execution/shadow_execution.py`(サービス)、`execution/shadow_store.py`(記録モデル + 永続化)、`execution/shadow_report.py`(読み取り専用 CLI)。

> **重要な位置づけ**
>
> - Shadow Mode は **Paper Execution でも Backtest でもない**。仮想約定を行わず、仮想ポジションを管理せず、仮想 PnL を計算しない。**判定経路(ExecutionRequest 生成 → Safety Gate)の観察と記録のみ**を行う。
> - Shadow Mode を追加しても、**OANDA Adapter・Demo / Live 発注・Live Execution runner・自動売買は引き続き未実装**である([Concept](../product/concept.md) の非スコープ)。Shadow Mode は実発注**前**の判定経路を観察する検証機能である。
> - Broker 注文状態(ACCEPTED / FILLED / REJECTED 等)は**記録しない**。何も送信していないので、Broker 応答は存在しない。Safety Gate の拒否は Gate の事実であり、Broker 拒否と混同しない(shadow テーブルには注文状態列自体が存在しない)。

## execution mode の契約

| mode | 意味 | Broker 送信 | 検証記録の保存 | 実装状態 |
|---|---|---|---|---|
| dry-run | 従来どおり副作用ゼロ(DB 作成・migration・INSERT・state 更新・Broker 呼び出し・Discord 送信なし)。mode というより各 Bot CLI の `--dry-run` フラグ | しない | **しない** | 実装済み(各 Bot) |
| `shadow` | 通常の判定経路(ExecutionRequest + Safety Gate)を実行し、結果を shadow store へ保存する。送信境界には到達しない | **しない** | する | 実装済み(Issue #78) |
| `fake` | FakeBroker への送信(既存契約。`LiveOrderSubmissionService` 経由) | FakeBroker のみ | Safety Gate 判定監査(`safety_gate_decisions.sqlite3`) | 実装済み(Issue #75) |
| `demo` | デモ口座接続。Adapter 型が登録されるまで接続を**構築できない** | (未実装) | — | 未実装 |
| `live` | 本番口座接続。同上 | (未実装) | — | 未実装 |

- mode の正は `execution.safety_gate.ExecutionMode`(enum)。未知の mode 文字列は enum 変換で fail-fast する。
- **`shadow` は接続種別ではない**: `BROKER_TYPE_BY_MODE` に entry を持たないため、`shadow` を名乗る Broker から `BrokerConnection` を構築することはできず(表現不能による fail-closed)、`SafetyPolicy.allowed_connection_targets` に `shadow` の接続先を設定することも構築時エラーになる。組み立て後に identity が `shadow` へドリフトした場合は Gate の再導出が `execution_mode_mismatch` で拒否する。
- dry-run と shadow は**排他の関係にある別概念**である: dry-run は「記録を残さない」契約、shadow は「送信せず記録を残す」契約。execution レイヤーには両者を同時指定できる CLI 経路自体が存在しない(shadow の読み取り CLI は常に読み取り専用)。

## 通常経路の再利用と送信境界

Shadow 専用の判定ロジックは存在しない。共有するもの:

- `ExecutionRequest`(モデル・構築時検証・provenance)— 実登録と同じものを同じ形で受け取る
- `SafetyGate.evaluate()` — **同一クラス・同一 policy・同一 fail-closed 契約**をそのまま実行する(kill switch・stale / 未来 snapshot・Broker 照会失敗・接続 identity 再導出を含む)。Gate の read-only Broker 照会(positions / account)は通常どおり行われる
- `SafetyRejection` / `SafetyRejectionCode` / 順序保持 dedup — 拒否理由の生成は Gate 所有のまま

分岐点は**送信境界ただ一点**である: `ShadowExecutionService.observe()` は評価結果を記録して終了し、`to_order_request()` にも `Broker.submit_order()` にも**呼び出し箇所自体が存在しない**。`execution/tests/test_submission_boundary.py` のリポジトリ全体 AST 走査が、shadow 3 モジュールに送信境界呼び出しが 0 箇所であることを回帰として固定する(本番 Adapter が connection に設定されていても、shadow 経路から submit へ到達するコードは存在できない)。shadow store はサービス内部に封印され、コンストラクタは filesystem パス(`shadow_db_path`)のみ受け取る(Issue #75 の監査 store と同じ方針)。

## idempotency(mode 間干渉の禁止)

- shadow 記録は **`execution/data/shadow_executions.sqlite3`(専用 DB)** に保存し、実登録用の `execution_orders.sqlite3` には**一切書かない**(`register()` / `created → submitting` 遷移は実送信専用)。
- 重複判定の単位は **store = mode 系列ごと**である:
  - shadow 内: `execution_request_id` / `signal_id` とも shadow store 内で UNIQUE。同一 Signal への shadow 再実行・多重起動は `BEGIN IMMEDIATE` 内の UNIQUE 制約で 1 件に抑止され(敗者は `ShadowExecutionConflictError`)、Issue #76 と同じ形式で WARN ログ(両 ID・既存記録の情報・理由)を残す。
  - mode 跨ぎ: shadow 観察済みの Signal はその後 fake/demo/live の実登録・送信へ**通常どおり進める**。逆に実送信済みの Signal も shadow 観察できる。意図しない mode 跨ぎの重複抑止は起こらない(回帰テストで固定)。

## 保存内容(`shadow_executions` テーブル)

1 行 = 1 shadow 観察。ExecutionRequest 全項目(`execution_request_id` / `signal_id` / `strategy_id` / symbol / side / order type / quantity / requested price / SL / TP / `requested_at_utc`)、固定の `execution_mode = 'shadow'`(DB CHECK で強制)、shadow 実行時刻(`observed_at_utc`)、Safety Gate 判定(allow / reject・判定時刻・**`policy_id`(適用設定の追跡)**・Gate が評価した接続 identity(`connection_mode` / `connection_target`)・account / position 情報)、機械可読な拒否コード + 人間可読な詳細(`rejections_json`。厳格キー検証)、想定注文内容(`intended_order_json`: Broker 境界を越えるはずだった symbol / side / order_type / quantity / limit_price。**`client_order_id` は含まない** — Broker キーの採番は実登録時のみ)。

- Decimal は正規文字列、時刻は tz-aware UTC ISO 文字列で保存し、読み戻し時に `ExecutionRequest` / `SafetyDecision` の構築時検証を再実行する(改竄・未知コード・キー集合不一致・intended order と意図の不一致は `ShadowIntegrityError`)。
- schema 方針は execution store / safety store と同一: pristine DB のみ新規作成、全 read / write が closed-world schema 検証を通過、読み取りは missing DB を**作成しない**、書き込み失敗は同一トランザクションで全 rollback(中途半端な行は残らない)。保存は書き込みトランザクション内で read-back 検証してから commit する。
- 既定パスは `SHADOW_EXECUTION_DB_PATH` で上書き可能(相対パスは `execution/` 基準)。

## 起動方法と結果確認

Shadow 実行はライブラリ API(将来の runner の composition root から呼ぶ形。Live Execution runner が未実装のため、常駐の起動導線はまだ存在しない):

```python
from execution.shadow_execution import ShadowExecutionService

service = ShadowExecutionService(
    connection=connection,          # BrokerConnection(現状構築可能なのは fake のみ)
    gate=gate,                      # 通常の SafetyGate(SafetyPolicy / kill switch / clock)
    shadow_db_path=shadow_db_path,  # パスのみ(store はサービス内部に封印)
    clock=clock,
)
service.initialize_store()          # 唯一の明示的 DB 作成経路(pristine のみ)
record = service.observe(request, market=market_snapshot)
```

結果確認(読み取り専用 CLI。DB 作成・migration・書き込みを行わない):

```bash
python -m execution.shadow_report list [--limit N] [--db PATH]
python -m execution.shadow_report show EXECUTION_REQUEST_ID [--db PATH]
```

- `list` は決定的順序(`observed_at_utc`, 挿入順)で全記録を一覧する(実行時刻・signal / request ID・symbol・side・quantity・allow / reject・拒否コード)。空・DB 不存在は「no shadow execution records」で終了コード 0。
- `show` は 1 件の全詳細(Safety Gate 拒否理由の全文・想定注文内容・policy_id・接続 identity)を表示する。未知 ID は終了コード 1。壊れた記録・schema 不一致は信用せずエラー(終了コード 1)。

## Paper Execution / Backtest / Shadow Mode の役割差

| 観点 | Paper Execution | Backtest | Shadow Mode |
|---|---|---|---|
| 目的 | 仮想約定・仮想ポジション・仮想損益の時間進行評価 | 過去データ上の Strategy / Execution ルール再生・評価 | 実発注前の判定経路(Request 生成 + Safety Gate)の観察・記録 |
| 約定 / PnL | 仮想的に行う | 仮想的に行う | **行わない** |
| Broker 境界 | 使わない | 使わない | Gate の read-only 照会のみ。**submit 系は構造的に到達不能** |
| 出力 | Paper Position DB | Backtest DB | Shadow Execution DB(検証記録のみ) |

## テスト

`python -m pytest execution/tests -q`(`tmp_path` の SQLite と FakeBroker のみ。外部通信・実注文なし)。

- `test_shadow_execution.py` — 許可 / 拒否 / kill switch / stale・未来 snapshot / Broker 照会失敗 / gate 内部エラーの各経路で**記録が保存され、submit 系呼び出しが 0 回**であること(submit を呼んだ瞬間に失敗する spy broker で検証)、shadow 専用判定ロジックが存在しないこと(実 Gate と同一結果)、shadow store 未初期化時の fail-closed(ファイル非作成)、shadow 内 idempotency(同一 intent / 同一 signal / 多重起動の抑止と WARN ログ)、**mode 跨ぎの非干渉**(shadow 観察後の fake 送信成功・fake 送信後の shadow 観察成功・execution order store 非作成)、mode validation(`shadow` enum・未知 mode 拒否・`shadow` を名乗る Broker の構築時拒否・identity ドリフトの Gate 拒否・policy の shadow 接続先拒否)、サービス構築時検証と store の封印。
- `test_shadow_store.py` — round-trip(Decimal / UTC / enum / 拒否理由 / 想定注文)、決定的順序、UNIQUE 制約(DB レベル含む)、rollback(失敗時に中途半端な行が残らない)、missing DB を作成しない read、closed-world schema 検証(foreign schema / trigger / version 不一致)、帯域外改竄の復元時拒否、DB CHECK 制約(`execution_mode = 'shadow'` 固定・allow ⇔ 拒否 0 件)。
- `test_shadow_report.py` — allow / reject / 空 / missing DB(非作成・終了コード 0)/ 未知 ID(終了コード 1)/ 改竄記録(終了コード 1)/ 決定的出力 / 既存 DB を 1 byte も変更しないこと(hash 比較)。
- `test_submission_boundary.py` — shadow 3 モジュールに `submit_order` / `to_order_request` の呼び出しが 0 箇所であることの AST 固定。

## 非スコープ(Issue #78 時点で未実装)

- Signal → ExecutionRequest の自動生成 runner(Signal 読取・数量決定を含む常駐導線。Live Execution runner と同様に未実装であり、ロット計算は Concept の非スコープ)
- OANDA Demo / Live 発注、Broker reconciliation
- 仮想約定・仮想ポジション・仮想 PnL(Paper Execution / Backtest の責務)
- 固定シナリオ試験基盤(Issue #77。本 Issue には含めない)
