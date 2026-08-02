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
| `execution_store.py` | SQLite 永続化(`execution_orders` + `execution_order_state_history`): 送信フェーズ(created / submitting / submitted)、Broker 注文状態、**最新の完全な OrderResult snapshot(JSON。history 行へ anchor)**、状態変更理由、broker_order_id(**DB 全体で UNIQUE**)、UNKNOWN 照会対象の抽出。current・snapshot・履歴は同一トランザクションで更新し、更新は **history に anchor された snapshot** を old とした `validate_order_result_update()` で常時検証する。**`signal_id` は UNIQUE index でテーブル全体一意**(Issue #76: source signal 1 件 → Execution Request 高々 1 件。重複作成は conflict + WARN ログで抑止され、schema v2 → v3 は `init_db()` が単一トランザクションで additive に migration する。既存重複 signal がある DB は明示的に失敗・無変更)。全 read / write 経路が「open → schema 完全構造検証 → トランザクション → restore / update → **commit 前の bundle 再検証**」で動作し(schema は sqlite_master **全 object 集合**の closed-world 比較 — **trigger / view / 予期しない object は存在自体を拒否**、DDL は quoted identifier 等の無害差を吸収する token 正規化比較。不一致は DB 無変更で失敗、commit 前検証の不一致は同一トランザクションで rollback)、復元時も chain / enum / UTC / snapshot anchor / broker_order_id 重複を再検証 |
| `safety_gate.py` | 発注前 Safety Gate(Issue #75): 許可銘柄・方向・数量上限・銘柄別 / 口座全体ポジション上限(件数ベース)・SL 必須 / SL・TP 妥当性・参照価格の鮮度(未来時刻含む)・spread 上限・market open / tradable・kill switch・**Broker 実体から導出する型付き接続 identity**(`BrokerConnection` — mode / target は `connection_identity()` から導出され caller は申告不能。評価のたびに再導出しドリフトを拒否)× 許可接続先の整合を独立検査し、**全違反を機械可読コードで収集・順序保持 dedup** する。Broker 照会(positions / account)の失敗・型不正・stale / 未来 snapshot、bool 以外の kill switch 応答はすべて fail-closed で拒否。`SafetyPolicy` は deep immutable(防御的コピー + read-only view) |
| `safety_store.py` | Safety Gate 判定の SQLite 監査保存(`safety_gate_decisions.sqlite3`。`execution_orders.sqlite3` とは別ファイル): allow / reject・機械可読な拒否コード・詳細・判定時刻・`policy_id`・mode / 接続先・account / position 情報。execution_store と同方針の closed-world schema 検証・pristine 判定・厳格復元。**保存失敗は例外 = 送信されない** |
| `live_submission.py` | **唯一の公認送信経路** `LiveOrderSubmissionService`(Issue #75): `register()` が意図と固定 `client_order_id` を execution store へ 1:1 で永続化し、`submit()` が「意図復元 → Gate 評価 → 判定の耐久保存 + read-back 検証 → created→submitting 遷移の獲得(一度きり)→ 送信 → 結果記録」を実施。二重送信・UNKNOWN 後の再送は `duplicate_submission` で拒否(抑止時は execution_request_id / signal_id / 現在の phase・state / 理由を WARN ログへ出力。Issue #76)。**監査 store はサービス内部に封印**(コンストラクタは `decision_db_path` パスのみ受理。recorder オブジェクトの注入点なし)。受領済み結果の保存失敗は UNKNOWN へフォールバック記録し、停止・二重失敗で残る submitting/pending も `execution_store.list_unresolved_submissions()` で照会可能 |
| `scenario_models.py` | 固定シナリオ試験(Issue #77)のシナリオ定義ロード + closed-world schema validation(未知キー・未知 behavior/状態/拒否コード・型不一致・重複 scenario id を明示的に拒否。all-or-nothing ロード) |
| `scenario_runner.py` | 固定シナリオ実行器 + CLI(`python -m execution.scenario_runner`)。シナリオデータを**本番の Execution 経路そのまま**(`LiveOrderSubmissionService` / `SafetyGate` / `execution_store` / FakeBroker)へ流し、actual を収集して expected と比較する。判定ロジックの再実装なし(AST 検査で固定) |
| `scenarios/` | 固定シナリオ定義(1ファイル = 1 シナリオの JSON)。正常約定・重複・Broker拒否・部分約定・timeout(UNKNOWN)・DB保存失敗・Safety Gate拒否・多重起動競合など([下記](#固定シナリオ試験issue-77)。キャンセルは Production ユースケース未実装のため対象外) |
| `tests/` | `python -m pytest execution/tests -q`(`tmp_path` 配下の SQLite と FakeBroker のみ。外部通信・Discord・S3・sleep・実注文なし) |

## Safety Gate / 送信サービスの境界

- Strategy は Safety Gate の判定を持たない。Broker Adapter は Safety Gate のポリシーを持たない。raw Broker は型付き `BrokerConnection` としてサービスへ注入され、外へ公開されない。
- `LiveOrderSubmissionService.submit()` は「意図復元 → 評価 → 判定の耐久保存 + read-back → created→submitting 遷移獲得」に**すべて成功した呼び出しだけ**が `ExecutionRequest.to_order_request()` と `Broker.submit_order()` に到達する。
- 1 件でも違反・kill switch 有効・Broker / store 照会失敗・判定保存 / read-back 失敗・二重送信・UNKNOWN 後の再送・想定外例外は fail-closed で送信しない。
- `BrokerConnection` の mode / target は Broker 自身の `connection_identity()`(構築時 config 由来・不変)から**導出**され、mode は具象型と相互検証される: `fake` = FakeBroker のみ、`demo` / `live` は Adapter 型が登録されるまで**構築不能**。
- 迂回は「呼び出し側の注意」ではなく **AST 走査テスト**(`tests/test_submission_boundary.py`)で構造的に禁止する: `submit_order` / `to_order_request` の呼び出し箇所は公認箇所以外に存在できない。この境界は**誤用防止**であり、reflection 等による**故意回避の in-process 防止は言語的に不可能**(実 Adapter 導入時の運用境界で扱う。[設計書の該当節](../docs/architecture/safety_gate.md)参照)。
- FakeBroker の `submitted_requests` により、全拒否経路で Broker 送信がゼロ件であることをテストする。

## 固定シナリオ試験(Issue #77)

実発注処理は実相場や Broker 障害を待って検証できないため、正常約定だけでなく拒否・部分約定・timeout(結果不明)・DB保存失敗・多重起動競合などを**固定データから決定的に再現**する試験基盤(固定シナリオは現在 **21 件**)。通常の pytest がモジュール単位の観点を固定するのに対し、固定シナリオは「入力 → Safety Gate → Broker → 永続化」の分岐 1 本を 1 シナリオとしてデータで表現する。**試験対象の判定はすべて本番コード**(`LiveOrderSubmissionService` / `SafetyGate` / `execution_store` / broker の状態機械)であり、runner は wiring・actual 収集・比較のみを行う(`tests/test_scenario_runner.py` の AST 検査が遷移表・validator の import を禁止)。`mark_submitting` / `record_unknown` ステップは store の公開 API による前提状態・観測の注入であり、ユースケースの合成ではない。

**キャンセルは固定シナリオの対象外**(PR #96 レビュー): Production のキャンセル Execution ユースケース(サービス境界経由のキャンセル)は未実装であり、runner がキャンセル処理を合成すると「本番経路の再現」にならないため。FakeBroker のキャンセル振る舞い(成功・失敗・terminal 拒否・呼び出し記録)は `broker/tests/test_fake_broker.py`、Execution Store の CANCELLED 遷移は `tests/test_execution_store.py` の通常 pytest が検証する。キャンセルユースケース実装(別 Issue 候補)後に、その公開 API を呼ぶ形で固定シナリオを追加する。

### 実行方法

```bash
python -m execution.scenario_runner                       # 全シナリオ
python -m execution.scenario_runner --list                # シナリオ一覧
python -m execution.scenario_runner --scenario-id <id>    # id 指定(複数可)
python -m execution.scenario_runner --scenario-file <path>  # ファイル指定
python -m execution.scenario_runner -v                    # expected/actual と本番ログを表示
python -m execution.scenario_runner --json                # machine-readable JSON 結果
```

- 出力: シナリオごとに `PASS / FAIL / ERROR` + scenario id + description、FAIL 時は不一致フィールドの expected/actual 差分、末尾に総数・PASS/FAIL/ERROR 数・実行時間・失敗シナリオ一覧。
- 終了コード: **0 = 全シナリオ PASS のみ**。FAIL(expected 不一致)または ERROR(未処理例外)が 1 件でもあれば 1、usage・シナリオ定義エラーは 2。
- コンソール encoding 非依存(PR #96 レビュー): 区切り文字は ASCII、`--json` は pure ASCII(`ensure_ascii`)、自由テキストは encode 不能文字をエスケープへ退避する。Windows 既定の cp932 コンソールでも(`PYTHONUTF8` なしで)クラッシュせず、終了コード契約が保たれる(cp932 を再現する pytest と Windows CI ジョブで固定)。この encoding 退避は `main()` 実行中のみで、stdout / stderr の設定は正常終了・usage error・schema error・未処理例外のすべての経路で元へ復元される(import 時には一切変更しない。pytest で固定)。
- CI では pytest スイート(`tests/test_fixed_scenarios.py` がシナリオ id 単位で parametrize)と CLI ステップ(Ubuntu / Windows)の両方で実行される。

### シナリオ定義(schema)

`scenarios/*.json`(1 ファイル = 1 シナリオ、ファイル名順にロード)。schema は `scenario_models.py` が closed-world で検証し、必須フィールド欠落・未知キー・未知 behavior/注文状態/拒否コード・型不一致・重複 scenario id は**ロード全体を失敗させる**(silent skip なし)。価格・数量は Decimal 文字列(float 禁止)。

| キー | 内容 |
|---|---|
| `scenario_id` / `description` | `^[a-z0-9_]+$` の一意 id と説明 |
| `gate` | Safety Gate 側の状態: `kill_switch`、policy 上書き(`max_order_quantity` / `allowed_symbols` / `allowed_fake_targets` / `max_reference_age_seconds` など。既定: USD_JPY・数量上限10・鮮度30s・接続先 `fake`) |
| `market` | 市場 snapshot: `reference_price` / `bid` / `ask` / `age_seconds`(固定 clock からの経過秒。stale 再現用)/ `market_open` / `tradable` |
| `broker` | FakeBroker 設定: `default_fill_price` / `connection_target` / `scripts`(client_order_id → behavior: `fill_immediately`(`price`・`broker_order_id` 指定可)/ `stay_pending` / `partially_fill` / `reject` / `timeout` / `fail_communication`) |
| `faults` | fault injection: `boundary` = `initial_save` / `mark_submitting` / `record_order_result` / `record_order_state` / `state_history` + `after_calls`(最初の N 回は成功させる) |
| `steps` | 宣言的ステップ列。`register` / `submit` / `record_unknown` / `mark_submitting`(別 worker が遷移を獲得済みの状態を作る)/ `submit_concurrent`(`workers` 本の thread を barrier 同期で同時 submit)/ `register_concurrent`。各ステップは `expect`(outcome・状態・拒否コード・エラー分類)を持つ |
| `expected` | 最終結果の比較: `final_phase` / `final_state` / `state_history`(履歴エントリのオブジェクト列。件数・順序は常に厳密比較。各エントリは `previous_state` / `new_state` 必須 + `reason` / `broker_order_id` の部分比較 — **キー省略 = 比較しない、明示 `null` = NULL であること**の区別あり。`changed_at_utc` の存在と単調性は宣言に関係なく常に検証、動的 timestamp の完全一致比較はしない)/ `broker_submit_calls` / `submitted_request`(Broker へ渡った request の部分一致)/ `order_rows` / `history_rows` / `safety_decisions` / `last_decision_allowed` / `last_decision_codes` / `broker_order_id` / `filled_quantity` / `average_fill_price` / `fill_prices` / `requested_price` / `reject_reason` / `listed_unknown` / `listed_unresolved` |

実行ごとに変動する値は比較しない設計: runner は固定 clock(`NOW` = FakeBroker の決定的 clock base)を注入し、`broker_order_id` は比較したいシナリオが script で固定値を指定する。DB 採番値(history_id)は比較対象外。

### fault injection

DB 保存失敗は、シナリオ実行中だけ `execution_store` の対象関数を例外を送出するスタブへ差し替えて再現する(終了時に必ず復元。テスト専用の依存差し替えであり、**本番コードに環境変数ベースの隠し故障モードは存在しない**)。境界は「Execution Request 初期保存」「SUBMITTING 遷移保存」「Broker 受付後の結果保存」「状態履歴保存」を区別し、Broker 受付後の保存失敗が「未送信」や即時再送可能状態に化けないこと(UNKNOWN 記録 + `list_unresolved_submissions()` での可視性 + 再送拒否)をシナリオが固定する。

### 副作用の隔離

各シナリオは専用の一時ディレクトリ内に `orders.sqlite3` / `safety.sqlite3` を作り(パスは明示注入。`EXECUTION_ORDER_DB_PATH` 等の既定パスは参照しない)、Broker は in-memory の FakeBroker のみ。外部 Broker API・Discord・S3・`.env`・state ファイル・ネットワークへの副作用はない(socket 無効化下でも PASS することを pytest が固定)。シナリオは相互に独立で、実行順序に依存しない。

### シナリオの追加方法

1. `scenarios/` に `NN_<scenario_id>.json` を追加する(`scenario_id` はファイル内で宣言。全ファイルで一意)。
2. 上記 schema に従い `steps` と `expected` を書く。分岐の判定を試験側へ複製しない — 期待する状態・回数・コードだけを宣言する。
3. `python -m execution.scenario_runner --scenario-id <id>` で確認する。`tests/test_fixed_scenarios.py` は `scenarios/` を自動で拾う(Issue #77 必須シナリオは id を pin しているため、削除・改名は fail する)。

## 位置づけ(誤解しないこと)

- 注文状態の唯一の正は既存の `broker.OrderState` / `validate_order_state_transition()` / `validate_order_result_update()`(Issue #73 / PR #87)。**本パッケージは注文状態 Enum・遷移表を新設しない。** CREATED / SUBMITTING は注文状態ではなく Execution Request 側の**送信フェーズ**(別フィールド)である。
- Safety Gate は将来の Live Execution 送信経路を安全にする**部品のみ**であり、OANDA Adapter・Live 接続・実注文 runner・reconciliation runner は**未実装**。`ExecutionMode` に `paper` は存在しない(Paper Execution は Broker 境界を使わず、Safety Gate を通らない)。
- **Paper Signal / Paper Execution は本パッケージを使わない**(`tests/test_package_imports.py` が回帰として固定)。
- UNKNOWN は即時再送可能状態ではない。照会対象として抽出し、後続照会結果の記録によってのみ確定状態へ解決する。state-only で記録できるのは UNKNOWN のみで、他のすべての観測は完全な `OrderResult` を `record_order_result` で記録する(stale snapshot による fill 消失・巻き戻しを DB 側 snapshot 検証で拒否)。

設計の詳細は [docs/architecture/execution_request.md](../docs/architecture/execution_request.md)(ExecutionRequest / 永続化)と [docs/architecture/safety_gate.md](../docs/architecture/safety_gate.md)(Safety Gate)を参照。
