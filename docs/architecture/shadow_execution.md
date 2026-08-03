# Shadow Mode / Shadow Execution(Issue #78)

実際の Signal・Execution Request 生成・設定・Safety Gate を**通常の Production 経路**で実行しつつ、**Broker へは一切送信せず**、生成された注文候補と Safety Gate 結果を検証記録として保存する Shadow Mode の仕様。実装は `execution/{shadow_models,shadow_store,shadow_execution,shadow_runner,shadow_report}.py`。

> **重要な位置づけ**
>
> - Shadow Mode は **Live 発注ではない**。Live Execution(実発注・Broker API 連携)は引き続き**未実装・非スコープ**であり、Shadow Mode はその**準備層の検証機能**である: Demo / Live 接続前に、実市場データと実 Signal から生成される Execution Request と Safety Gate の判定経路を継続観察・監査するための記録装置。
> - **Paper Execution とは別物**。Paper Execution(`paper-execution/`)は仮想約定・仮想 Position・仮想 PnL を扱う独立した仮想執行レイヤーであり、Broker 境界にも Safety Gate にも触れない。Shadow Mode は**仮想約定・仮想 Position・仮想 PnL を一切計算しない** — 記録するのは「送信予定だった注文内容」と「Gate を通過した / 拒否された」という監査事実だけである。
> - Shadow 結果には Broker 応答を意味する値(`ACCEPTED` / `FILLED` / `PARTIALLY_FILLED` / `CANCELLED`、Broker reference id、fill price / quantity)が**構造的に存在しない**(schema にその列がない)。Shadow が Broker 状態を捏造することはない。

## execution mode(統一語彙)

mode の唯一の正は `execution.safety_gate.ExecutionMode`(Enum)。文字列比較を各所へ散らさず、設定文字列は `resolve_execution_mode()` が設定ロード時に解決し、**未知の mode・大文字小文字違い・前後空白は起動時に明示拒否**する(黙った既定値へのフォールバックなし)。

| mode | Broker 呼び出し | 永続化 | 状態 |
|---|---|---|---|
| `dry-run` | なし | **一切なし**(DB 作成・migration・INSERT/UPDATE・state 更新・Discord いずれもなし。既存 DB は read-only probe のみ) | 実装済み |
| `shadow` | **なし(構造的に不可)** | Shadow 検証結果 + Safety Decision を保存 | 実装済み(本書) |
| `fake` | FakeBroker へ送信 | 既存の注文状態機械・execution store(Issue #74) | 実装済み(`LiveOrderSubmissionService`) |
| `demo` | — | — | **mode 定義と validation のみ。未実装**(Adapter 型未登録のため `BrokerConnection` を構築できない) |
| `live` | — | — | 同上(未実装) |

mode dispatch はこの構造自体が担う: `shadow` は `ShadowExecutionService`(Broker 依存なし)、`fake` は `LiveOrderSubmissionService`(`BrokerConnection` 必須・FakeBroker のみ構築可能)、`demo` / `live` は `BROKER_TYPE_BY_MODE` に Adapter 型が登録されるまで**接続オブジェクト自体が構築不能**(fail-closed by unrepresentability)で、runner はそれらを明示的に拒否する(`NON_SUBMITTING_MODES` / `UNIMPLEMENTED_BROKER_MODES`)。

## Production Shadow サービス境界

正式な Shadow 経路は **`ShadowExecutionService`(`execution/shadow_execution.py`)ただ 1 つ**。scenario runner / CLI は wiring と結果比較のみを行い、Shadow 処理そのものを持たない(runner が `ShadowResult` を直接構築・保存しないことは AST 検査で固定。`record_shadow_result` の呼び出し箇所も repository-wide AST で `shadow_execution.py` に限定される)。

```text
Signal(実 Signal。語彙→OrderSide の変換は呼び出し側の責務 — live submission と同一契約)
  -> ExecutionRequest 生成(既存モデル。Issue #74)
  -> 重複 pre-check(shadow store。既存記録があれば明示 DUPLICATE)
  -> SafetyGate.evaluate_shadow()   … live と同一の共有チェック実装
  -> SafetyDecision の耐久保存 + read-back 検証(live と同じ封印監査 store)
  -> ShadowResult を単一のアトミック INSERT で保存(UNIQUE 制約が勝者を決定)
```

守られる責務分離(Issue #78):

- runner は Production API を呼ぶだけ。runner 専用の Shadow 判定・Shadow 専用のロット計算は存在しない。
- Safety Gate は複製しない: `evaluate()`(live)と `evaluate_shadow()` は同一の内部チェックヘルパーを共有する。
- Execution Request 生成は複製しない: 既存 `ExecutionRequest` モデルをそのまま使う。
- Broker Adapter へ Shadow 判定を持たせない。Paper Execution へ Shadow 処理を入れない。

## Broker 呼び出し防止(構造保証)

- `ShadowExecutionService` は **Broker / BrokerConnection を受け取る引数を持たない**(コンストラクタにも全メソッドにも存在しない)。呼びようがない、が保証の本体。
- `SafetyGate.evaluate_shadow()` も connection を受け取らない。Gate のポジション / 口座上限チェックが必要とする Broker 状態は、呼び出し側が注入する **`AccountSafetySnapshot`**(`positions` + `AccountState`)から評価する。snapshot の信頼性(型・未来時刻・stale)は live の Broker 照会結果と**同一のルール**で検証され、不正なら `broker_query_failed` で fail-closed に拒否する。
- shadow モジュール群は `broker.broker_models`(モデル)のみ import し、`broker.broker_interface` / `broker.fake_broker` を import しない(AST 検査で固定)。
- テストは FakeBroker の **submit / cancel / order query / position query / account query 全 API を「呼ばれた瞬間に失敗する」double に差し替えた状態**で、Gate 通過・拒否・duplicate・dry-run の全 cycle が完走することを固定する。固定シナリオも全 Shadow シナリオで `broker_submit_calls: 0` を要求する。
- Production の Demo / Live Broker への接続は不可能(上記のとおり接続オブジェクトが構築不能)。

## Shadow 保存モデル(`execution/shadow_store.py`)

独立 DB `execution/data/shadow_results.sqlite3`(既定。`SHADOW_RESULT_DB_PATH` で上書き)。`execution_orders.sqlite3` / `safety_gate_decisions.sqlite3` とは**別ファイル**(いずれも closed-world schema 検証のため同居不能)であり、**通常注文状態のレコードとは混同されない**(Shadow は execution order store に一切書かない)。

`shadow_results` テーブル(schema version 1)。検索・集計に必要な主要項目は JSON とは別に**列として保持**する:

- `shadow_result_id` INTEGER PRIMARY KEY AUTOINCREMENT
- `execution_request_id` TEXT NOT NULL **UNIQUE** / `signal_id` TEXT NOT NULL **UNIQUE**(source signal)/ `client_order_id` TEXT NOT NULL **UNIQUE**
- `strategy_id` / `symbol` / `timeframe`(NULL 可。ExecutionRequest は timeframe を持たないため、呼び出し側が Signal 由来のメタ情報として任意で渡す)/ `side` / `order_type` / `quantity` / `requested_price` / `stop_loss_price` / `take_profit_price` / `requested_at_utc`(request 本体の列投影。Decimal は正準 TEXT)
- `execution_mode` TEXT NOT NULL **CHECK (= 'shadow')**
- `request_json`(ExecutionRequest 全体の canonical JSON)/ `intended_order_json`(想定注文内容 = Broker へ送る予定だった内容の監査投影。`broker.OrderRequest` と同じフィールド集合だが **OrderRequest は構築しない** — `to_order_request()` は公認送信経路専用のまま)
- `allowed` INTEGER CHECK(0,1)/ `rejections_json`(機械可読コード + 人間向け理由。**CHECK: `allowed=1` ⇔ `rejections_json='[]'`**)/ `policy_id` / `policy_json`(適用 policy の完全 snapshot)/ `checked_at_utc` / `connection_target`
- `market_json`(判定に使った market snapshot 全体)/ `market_observed_at_utc`(price timestamp)/ `spread`
- `account_id` / `position_count`(判定時に注入 snapshot から得た口座識別・ポジション数。取得不能なら NULL)
- `safety_decision_id`(Safety decision store の該当行への参照。decision 保存が失敗した場合は NULL = 明示的な回復可能状態)
- `created_at_utc`

schema 方針は execution store / safety store と同一: **pristine な DB のみ新規作成**(冪等・単一トランザクション・失敗時全 rollback)、全 read / write が closed-world schema 検証(sqlite_master 全 object 集合 + token 正規化 DDL + PRAGMA 構造 signature + version)を通り、不正 schema は **DB 無変更で拒否**(黙った上書きなし)。読み取りは missing DB を作成せず、復元行は厳格に再検証(JSON キー集合の完全一致・既知拒否コードのみ・UTC・列と JSON の相互整合)し、帯域外改竄は `ShadowIntegrityError` で拒否する。NOT NULL / UNIQUE / CHECK は実 schema として検証され、JSON の必須構造は application validation(strict restore)が確認する。**dry-run では初期化・migration を行わない。**

## Safety Gate 連携

Shadow でも Safety Gate は**通常どおり・同一ロジックで**実行される(`evaluate_shadow` は live の `evaluate` と内部ヘルパーを共有): 許可銘柄 / 許可方向 / 数量上限 / 銘柄別・口座全体ポジション上限(注入 snapshot 起点)/ SL 必須・SL / TP 妥当性 / price freshness(未来時刻含む)/ spread 上限 / market open / tradable / kill switch / 外部状態(注入 snapshot)取得失敗の fail-closed / execution mode 整合(shadow 接続先が `SafetyPolicy.allowed_connection_targets[SHADOW]` にあること。不一致は `execution_mode_mismatch`)。

判定は live と同じ**封印監査 store**(`SQLiteSafetyDecisionStore`。コンストラクタはパスのみ受理)へ耐久保存 + read-back 検証される。**Gate 拒否でも Shadow 結果は保存される**(rejected・全 reason code・人間向け理由・Execution Request・実行時刻・適用 policy)。decision 保存 / read-back が失敗した場合は live と同じく `decision_persist_failed` が追記され allow は拒否へ転じ、Shadow 行は `safety_decision_id = NULL` で保存される — Shadow 結果と Safety Decision の間に「参照先が存在しない」不整合は残らず、欠落は明示的に検出可能である。

## idempotency 契約(Issue #78)

- **namespace 分離が最重要契約**: Shadow は shadow store にのみ書き、execution order store(`execution_orders.sqlite3`)へは登録しない。したがって **Shadow を実行しても、同じ Signal の将来の fake / demo / live 送信が「既に送信済み」と判定されることはない**(逆方向も同様: live 側の register が Shadow をブロックしない)。Execution Request の identity(注文 intent、execution store 側)と Shadow の evaluation 記録は store 単位で分離されている。
- 同一 source signal からの Execution Request 重複生成は、送信経路では従来どおり execution store の `signal_id` UNIQUE(Issue #76)が拒否する。Shadow 側は shadow store の `signal_id` UNIQUE が同じ不変条件を独立に守る。
- **同一 execution_request_id / signal_id / client_order_id の Shadow 結果は重複保存されない**: 勝者は `BEGIN IMMEDIATE` トランザクション内の単一 INSERT と UNIQUE 制約が決定する(事前 SELECT は分類のみ)。再実行・多重起動の敗者は**既存の保存済み結果を持つ明示的な DUPLICATE outcome** を受け取る。DB lock 等の store 障害は duplicate に化けず、cycle 失敗として伝播する。
- 保存は単一 INSERT なので、初期保存失敗時に「不完全な Shadow 行」や「idempotency 登録だけが残る」中間状態は構造的に存在しない。
- Safety Gate や設定が変化した場合の**再評価(同一 request の 2 件目の Shadow 記録)は意図的に非対応**(最初の記録が勝ち、適用 policy snapshot が一緒に保存される)。将来必要になれば evaluation id / policy version による別記録として明示する設計拡張になる(非スコープ)。

## dry-run との分離

`dry-run` は従来契約(architecture.md「dry-run 方針」)のまま: Shadow table 作成・migration・Shadow result 保存・Execution Request 保存・Safety Decision 保存・idempotency 登録・state 更新・Broker 呼び出し・Discord・S3 の**すべてが発生しない**。`ShadowExecutionService.dry_run_cycle()` は同じ評価(Gate・intended order 投影)を実行するが何も永続化せず、既存 DB があっても read-only probe に留める(存在しない DB パスはファイルを作らない。不正 DB・schema 欠損は成功扱いせず明示エラー = cycle failure)。

## CLI

```bash
python -m execution.shadow_runner --mode shadow --cycle <cycle.json>    # 1 cycle 実行・保存
python -m execution.shadow_runner --mode dry-run --cycle <cycle.json>   # 同じ評価・完全無副作用
python -m execution.shadow_runner ... --json                            # machine-readable(pure ASCII・ログ非混入)
python -m execution.shadow_report                                       # 保存済み Shadow 結果の一覧
python -m execution.shadow_report --execution-request-id <id>           # 詳細(通過/拒否・拒否理由)
python -m execution.shadow_report --source-signal-id <id>
python -m execution.shadow_report --decision rejected
python -m execution.shadow_report --json
```

終了コード: shadow_runner は 0 = cycle 完了(RECORDED / 明示 DUPLICATE / dry-run。**Gate 拒否も記録された正常結果**)/ 1 = cycle 失敗(store・schema 障害)/ 2 = usage・設定エラー(不正 mode、`fake` の誤用、`demo` / `live` の未実装拒否を含む)。shadow_report は 0 = レポート生成(空 store 含む)/ 1 = missing DB・unknown id・schema / integrity 障害 / 2 = usage エラー。両 CLI とも Issue #77 の encoding-safe 出力(`execution/cli_streams.py`)を共有し、Windows cp932 コンソールでも終了コード契約が保たれる。cycle 入力ファイルの schema は closed-world(未知キー拒否・Decimal 文字列・ISO-8601 UTC)。`--now` で決定的な検証実行ができる。

## 固定シナリオ(Issue #77 基盤の再利用)

Shadow シナリオも共通の scenario loader / runner / reporting(closed-world validation・未知フィールド拒否・一時 DB 隔離・実行順序非依存)で走る。runner は `ShadowExecutionService` の公開 API を呼ぶだけである。追加シナリオ:

`shadow_gate_pass` / `shadow_stale_price_rejected` / `shadow_kill_switch_rejected` / `shadow_mode_mismatch_rejected` / `shadow_duplicate_request` / `shadow_concurrent_single_record`(barrier 同期・DB lock エラーを成功扱いしない)/ `shadow_db_failure_initial_save`(ERROR・不完全行なし)/ `shadow_decision_save_failure`(transaction 境界と明示回復可能状態)/ `shadow_dry_run_no_side_effects`(DB 未作成)/ `fake_mode_no_shadow_record`(fake 回帰: 従来どおり送信・Shadow 記録なし)。

いずれも `broker_submit_calls: 0`(fake 回帰を除く)・Shadow 行数・拒否コード・intended order を expected として宣言する。fault injection は Issue #77 の依存差し替え機構を `shadow_result_save` / `safety_decision_record` 境界へ拡張したもの。

## 非スコープ(Issue #78 時点)

- OANDA Adapter・Demo 注文・Live 注文・Broker reconciliation
- 仮想約定・仮想 Position・仮想 PnL(Paper Execution の責務)
- 自動キャンセル・Production キャンセルユースケース(Issue #99)
- Strategy 変更・Paper Signal 生成ルール変更・Paper Execution 変更
- Signal → ExecutionRequest の自動 wiring・ロット計算(Live Execution 領域。ExecutionRequest 構築は従来どおり呼び出し側の責務)
- 同一 request の policy 変更後再評価(上記 idempotency 契約)

## テスト

`python -m pytest execution/tests -q`(`tmp_path` の SQLite のみ。外部通信・Discord・S3・実注文なし)。観点の一覧は [テスト仕様書](../testing/test_specification.md) の Shadow Mode 行を参照。固定シナリオは `python -m execution.scenario_runner`(pytest からは `test_fixed_scenarios.py`)で実行する。
