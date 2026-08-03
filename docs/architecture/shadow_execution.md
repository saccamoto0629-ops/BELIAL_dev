# Shadow Mode / Shadow Execution(Issue #78 / PR #100)

実際の Signal・Execution Request 生成・設定・ロット設定・Safety Gate を**通常の Production 経路**で実行しつつ、**Broker へは一切送信せず**、生成された注文候補と Safety Gate 結果を検証記録として保存する Shadow Mode の仕様。実装は `execution/{shadow_models,shadow_store,shadow_execution,shadow_providers,shadow_config,shadow_composition,shadow_runner,shadow_report,sqlite_ro,strict_json}.py`。

> **重要な位置づけ**
>
> - Shadow Mode は **Live 発注ではない**。Live Execution(実発注・Broker API 連携)は引き続き**未実装・非スコープ**であり、Shadow Mode はその**準備層の検証機能**である。
> - **Paper Execution とは別物**。Shadow Mode は**仮想約定・仮想 Position・仮想 PnL を一切計算しない** — 記録するのは「送信予定だった注文内容」と「Gate を通過した / 拒否された」という監査事実だけである。
> - Shadow 結果には Broker 応答を意味する値(`ACCEPTED` / `FILLED` / `PARTIALLY_FILLED` / `CANCELLED`、Broker reference id、fill price / quantity)が**構造的に存在しない**(schema にその列がない)。

## execution mode(統一語彙)

mode の唯一の正は `execution.safety_gate.ExecutionMode`(`dry-run` / `shadow` / `fake` / `demo` / `live`)。設定文字列は `resolve_execution_mode()` が設定ロード時に解決し、**未知の mode は起動時に明示拒否**する。`demo` / `live` は mode 定義と validation のみ(Adapter 型未登録のため `BrokerConnection` を構築できず、runner は未実装として明示拒否)。`fake` の送信経路は従来どおり `LiveOrderSubmissionService` ただ 1 つ。

## Production Signal 入力経路(本 Issue の中核)

**「実際の Signal・設定を利用する」経路は、保存済み Signal を読む経路に限定される。**正式な composition root は `execution/shadow_composition.py`:

```text
signal_id
  -> StoredSignalProvider.get_signal()            … 保存済み Signal の解決(欠損は明示エラー)
  -> ShadowExecutionConfig.order_quantity_for()   … Production 設定の銘柄別数量(未設定銘柄は明示エラー)
  -> build_execution_request_from_signal()        … Signal → ExecutionRequest の正式サービス(決定的 identity)
  -> MarketSnapshotProvider / AccountSnapshotProvider … snapshot 解決(欠損は明示エラー)
  -> ShadowExecutionService.run_cycle()           … claim → Gate → 記録
```

- **provider 境界**(`execution/shadow_providers.py`): `StoredSignalProvider` / `MarketSnapshotProvider` / `AccountSnapshotProvider` は Protocol。Production adapter は
  - `PaperSignalDbSignalProvider` — Paper Signal SQLite store(`paper-signal/` が所有・書き込み)を **true read-only**(`execution/sqlite_ro.py`)で読む。`paper-signal` ディレクトリは import 可能な package 名ではないため、adapter は DB ファイル境界で接続する(Paper Execution が Chart DB を読むのと同じ cross-component read-only パターン)。必要列の欠損・DB 欠損・Signal 欠損はすべて明示 `ShadowInputError`。
  - `MarketSnapshotFileProvider` / `AccountSnapshotFileProvider` — 運用が維持する snapshot JSON(実 feed が存在するまでの injected context)。**全フィールド必須**。
- **Production 設定**(`execution/shadow_config.py`): `config_id`(= configuration version)・完全明示の `SafetyPolicy`・銘柄別 `order_quantities`・`kill_switch`・`connection_target` を 1 ドキュメントで宣言する。**Production 経路に permissive default は存在しない**: 設定不足・未知キー・未設定銘柄の数量・snapshot 欠損・Signal 未取得・strategy 未解決はすべて明示エラー(fail-closed)。
- **Signal → ExecutionRequest**(`execution/shadow_composition.py::build_execution_request_from_signal`): `long`/`short` → `OrderSide`、MARKET 注文、数量は設定から。identity は **Signal id から決定的に導出**される(`execution_request_id = "sigreq-<signal_id>"`、`client_order_id = "sigord-<signal_id>"`、`requested_at_utc` = Signal 自身の時刻)ため、同一 Signal の再実行は常に同一 canonical payload を claim し、crash-retry は duplicate / recovery に分類される(新規 cycle や conflict にならない)。
- 保存済み Paper Signal は**価格レベル(entry / SL / TP)を持たない**ため、Production Shadow の request は SL / TP なしの MARKET 注文になる。それが Gate を通るかは設定の `require_stop_loss` の明示判断であり、保護価格の導出・数量計算(ロット計算)は Live Execution 領域として**実装しない**。

CLI fixture 経路(`shadow_runner --cycle`)は手書き JSON の fixture 用途として残るが、**`input_origin=cli_fixture` として明確に区別**され(fixture 用の default 許容も fixture 契約のみ)、`production_signal` を名乗ることはできない。

## input provenance

すべての Shadow 記録は入力元を保存する(`ShadowProvenance`):

- `input_origin` — `production_signal` / `cli_fixture` / `scenario_fixture`(closed set)
- `signal_source`(provider / store 識別子)・`signal_observed_at_utc`(Signal 自身の時刻。production では必須)
- `config_source`・`config_version`(適用設定の識別。policy snapshot 全体も併存保存)
- `market_source`・`account_source`

report(通常出力・`--json` とも)は provenance を表示する。

## claim-first ライフサイクルと idempotency

**Gate 評価の前に** DB 所有の identity claim を取得する(`shadow_store.claim_shadow_cycle`)。row は `phase`(`claimed` / `completed`)を持ち、Broker 注文状態とは混同しない:

```text
claim shadow identity(BEGIN IMMEDIATE + UNIQUE)
  ├─ winner:  Gate 評価 → Safety Decision 保存 + read-back → complete(phase=completed)
  ├─ loser :  duplicate として既存記録(completed 結果 or 進行中 claim)を返す。
  │           Gate 評価しない。Safety Decision を保存しない。
  └─ conflict: ShadowIdentityConflictError(下記)。何も評価・保存しない。
```

claim 契約:

- claim は `claim_owner_id` / `claimed_at_utc` / `lease_expires_at_utc`(既定 lease 600 秒、サービス構築時に指定可)を持つ。
- **同一 owner は自分の claim を即時再取得できる**(in-process retry。claim 取得後〜完了前のあらゆる失敗を同一プロセスが即座にやり直せる)。
- **他 owner は lease 期限切れ後のみ takeover できる**(process crash 回復)。takeover は conflict 判定と同一トランザクション内の UPDATE なので、2 者が同時に stale claim を奪うことはない。stale claim の回収者は「次に同一 payload を実行した者」であり、専用 reconciliation runner は不要(非スコープ)。**永久に処理不能になる claim は存在しない**: 完了するか、lease 切れ後の次回実行が回収する。
- crash 時点別の挙動: (a) claim 後・Gate 評価前に死亡 → claim が stale 化し回収される(decision なし)。(b) decision 保存後・complete 前に死亡 → claim 回収時に再評価され新 decision で完了。先行 decision は **orphan** として `shadow_report --orphan-decisions` で検索可能(増加は retry 回数で有界)。(c) complete 後 → 以後は duplicate。
- takeover 後に旧 owner が complete しようとすると「claim lost」で明示失敗する。
- **SQLite lock / store 障害は `ShadowStoreError` であり、duplicate に分類されない。**

namespace 分離は従来どおり: Shadow は `execution_orders` に登録しないため、Shadow 実行が同一 Signal の将来の fake / demo / live 送信をブロックすることはない(逆も同様)。

## duplicate と conflict(canonical payload)

identity キー(`execution_request_id` / `signal_id` / `client_order_id`。各 UNIQUE)の一致だけでは duplicate にならない。**canonical payload**(`shadow_models.canonical_cycle_payload`: execution_request_id / signal_id / client_order_id / strategy_id / symbol / side / order_type / quantity / requested_price / SL / TP / timeframe / execution_mode / input_origin)が**完全一致した場合のみ duplicate** となり、既存記録を明示的に返す。

- Decimal は**数値として正準化**して比較する(`1` と `1.0` は同値)。canonical JSON は key-sorted で、キー順序は等価性に影響しない。評価時刻・market snapshot・requested_at 等の evaluation-time 値は payload に含めない(crash-retry を conflict にしないため)。
- 1 項目でも異なれば **`ShadowIdentityConflictError`**(machine-readable reason code `shadow_payload_mismatch` + 差分フィールド一覧)。既存結果を黙って返さず、CLI は非 0(exit 1)で reason code を表示し、scenario では `conflict` outcome として監査される。conflict は Gate を評価せず Safety Decision も増やさない。
- 複数の identity キーが**異なる既存 row** に属する場合(identity の混用)も conflict。

## Safety Decision との監査整合(1:1 契約)

claim-first により **completed Shadow 記録 1 件 : Safety Decision 1 件**が通常形(`safety_decision_id` で参照)。duplicate / conflict は decision を作らない。例外は明示的:

- decision 保存 / read-back 失敗 → allow は `decision_persist_failed` の拒否へ転じ、Shadow 記録は `safety_decision_id = NULL` で完了(明示的な回復可能状態)。
- complete 失敗後の retry → 新 decision で完了し、先行 decision が orphan になる(`--orphan-decisions` で追跡可能・retry 回数で有界)。
- Shadow 保存(complete)失敗時に残る decision の契約は上記 (b) のとおり: claim が残り、回収時の新 decision が正となる。

Safety Decision store 自体への schema 変更はない(additive の read API `list_for_request_with_ids()` のみ追加。既存利用者は不変)。orphan 検索は「Shadow row の execution_request_id に対する decision のうち、参照されていないもの」という結合で行う。

## Shadow 保存モデル(`execution/shadow_store.py`)

独立 DB `execution/data/shadow_results.sqlite3`(既定。`SHADOW_RESULT_DB_PATH` で上書き)。`shadow_results` テーブル(schema version 1)は claim 列(phase / owner / lease / canonical payload)+ request 投影列 + provenance 列 + 完了列(判定・policy snapshot・market snapshot・**account snapshot 全体**)を持ち、`phase='claimed'` ⇔ 完了列全 NULL / `phase='completed'` ⇔ 完了列全 NOT NULL を CHECK で強制する。

**account / position snapshot の監査保存**: Gate が判定に使った注入 `AccountSafetySnapshot` **全体**を `account_snapshot_json` に保存する(account_id / currency / balance / equity / margin_available / retrieved_at + 各 position の symbol / side / quantity / average_entry_price / retrieved_at)。Decimal は文字列・時刻は UTC tz-aware・復元は closed-world(未知フィールド拒否・投影列 `position_count` との整合検証・改ざんは `ShadowIntegrityError`)。ポジション上限・口座上限の拒否は保存 snapshot から**事後再現可能**(pytest で固定)。モデルに secret(API token / password / credential)は存在せず、保存もされない。なお `AccountState` に used margin / position opened_at のフィールドは存在しないため保存対象外(モデル追加は Broker 境界 Issue の範囲)。

schema 方針は従来どおり(pristine 初期化・closed-world 検証・strict restore・canonical payload 列の再計算検証)。

## true read-only probe(`execution/sqlite_ro.py`)

dry-run・report・全 store 読み取り・Signal adapter は `connect_read_only()`(`file:...?mode=ro` URI + `PRAGMA query_only=ON`)を使う。Safety Decision store の読み取りも同契約。保証:

- missing DB / parent directory を作らない。journal / WAL / SHM を作らない。migration せず `user_version` を書き換えない。mtime / size を変えない。read-only ディレクトリでも読める。write 文は必ず失敗する。corrupted DB・schema 欠損は明示エラー(成功扱いしない)。
- WAL header を持つ**外部** DB は `immutable=1` で開く(side file を作らないため。point-in-time 読みであり並行 writer とは競合させない前提 — Paper Execution の snapshot copy 読みと同じ割り切り。execution 所有 store は常に rollback journal なので通常経路はロック付き `mode=ro`)。

dry-run(`--mode dry-run` / `dry_run_cycle()`)は同じ評価を実行して**何も永続化せず**、既存 DB は上記 read-only probe のみ(前後でファイル一覧・バイト列・mtime 不変。pytest / CI で固定)。

## JSON 入力の制限(`execution/strict_json.py`)

CLI cycle fixture・Production 設定・snapshot ファイル・scenario 定義はすべて strict loader を通る:

- **重複キー拒否**(ネスト含む)・不正 UTF-8 / surrogate・**UTF-8 BOM 拒否**は安定した入力エラー(traceback 非表示)
- 上限(定数): ファイル 256 KiB / ネスト深さ 32 / 文字列長 4096 / 配列 1024 要素 / object 256 キー
- `NaN` / `Infinity` 拒否。数値は Decimal **文字列**のみ(bool / number は拒否)、正の有限値、指数は ±28 に制限

## CLI

```bash
# Production Signal 経路(保存済み Signal + Production 設定。default なし)
python -m execution.shadow_runner --mode shadow --signal-id <id> \
  --signal-db <paper_signals.sqlite3> --execution-config <config.json> \
  --market-snapshot <market.json> --account-snapshot <account.json>

# CLI fixture 経路(input_origin=cli_fixture として記録)
python -m execution.shadow_runner --mode shadow --cycle <cycle.json>
python -m execution.shadow_runner --mode dry-run ...          # 完全無副作用

# report(read-only。一覧は shadow_result_id DESC = 新しい順)
python -m execution.shadow_report [--limit N(1..1000)] [--decision allowed|rejected]
python -m execution.shadow_report --execution-request-id <id> | --source-signal-id <id> | --safety-decision-id <n>
python -m execution.shadow_report --claims                    # 進行中 / stale claim
python -m execution.shadow_report --orphan-decisions [--decision-db <path>]
python -m execution.shadow_report --json                      # pure ASCII・ログ非混入
```

終了コード: shadow_runner は 0 = cycle 完了(RECORDED / 明示 DUPLICATE / dry-run。Gate 拒否は記録された正常結果)/ **1 = cycle 失敗(identity conflict[reason code 付き]・Production 入力未解決・store 障害)**/ 2 = usage・設定エラー(不正 mode・不正 cycle 入力・不完全な Production 設定)。shadow_report は 0 = レポート生成 / 1 = missing DB・unknown id・schema 障害 / 2 = usage エラー。両 CLI とも cp932 コンソールで契約を維持する(`execution/cli_streams.py`)。

## 固定シナリオ(Issue #77 基盤の再利用)

runner は Production API(`ShadowExecutionService` / `run_production_shadow_cycle`)を呼ぶだけ(AST 検査で固定)。Shadow 系シナリオ:

`shadow_production_signal_path`(**Production Signal provider 経路**: 保存済み Signal fixture を production adapter で解決)/ `shadow_gate_pass`(scenario_fixture provenance・account snapshot 保存・decision 1:1)/ `shadow_stale_price_rejected` / `shadow_kill_switch_rejected` / `shadow_mode_mismatch_rejected` / `shadow_duplicate_request`(同一 payload duplicate・decision 増加なし)/ `shadow_payload_conflict`(**同一 ID 異 payload → conflict**)/ `shadow_concurrent_single_record`(**Shadow 1 件 + Safety Decision 1 件**・DB lock を成功扱いしない)/ `shadow_stale_claim_recovery`(**claim 残骸の lease 回収**)/ `shadow_db_failure_initial_save`(claim 失敗 → decision 0 件・残骸なし)/ `shadow_decision_save_failure` / `shadow_dry_run_no_side_effects` / `fake_mode_no_shadow_record`(fake 回帰)。

fault 境界: `shadow_claim` / `shadow_result_save`(completion)/ `safety_decision_record`。`fail_calls`(最初の N 回だけ失敗)が stale-claim 回収シナリオを支える。

## 非スコープ(Issue #78 時点)

- OANDA Adapter・Demo / Live 注文・Broker reconciliation・専用 reconciliation runner
- 仮想約定・仮想 Position・仮想 PnL(Paper Execution の責務)・Production キャンセル(Issue #99)
- Strategy 変更・Paper Signal 生成ルール変更・Paper Execution 変更
- 保護価格(SL/TP)の導出・数量計算(ロット計算)— Live Execution 領域
- policy 変更後の同一 request 再評価(最初の記録が勝ち、適用 policy snapshot を併存保存)

## テスト

`python -m pytest execution/tests -q`(`tmp_path` の SQLite のみ。外部通信・Discord・S3・実注文なし)。観点の一覧は [テスト仕様書](../testing/test_specification.md)、固定シナリオは `python -m execution.scenario_runner`。
