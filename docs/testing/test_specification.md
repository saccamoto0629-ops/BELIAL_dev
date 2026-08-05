# BELIAL テスト仕様書

Issue #34。本書は品質の百科事典ではなく、**Claude Code・Codex・pytest・CI(#32)が同じ地図を見るための作戦盤**である。全単体テストを転記せず、重要な品質特性・代表テストケース・既存pytestとの対応のみを記載する。

- 記載内容はコードまたは既存ドキュメントから確認できる事実に基づく。推測で仕様を補わない。
- 個々のテストの網羅は各 `*/tests/*.py` が持つ。本書はその「観点」と「代表例」を固定する。
- コードと矛盾する点・未確認の点は [§9 矛盾・不明点](#9-矛盾不明点) に明示する。

関連: [#32 Add minimal GitHub Actions CI](https://github.com/saccamoto0629-ops/BELIAL_dev/issues/32)(CI実装は#32の範囲。本書はそこで何を継続実行すべきかの判断材料を提供する)。

---

## 1. テスト対象・目的・範囲・非対象

### 対象
BELIAL の観測〜分類〜Alert〜仮説(Paper Signal)〜検証(Backtest)〜仮想執行(Paper Execution)の各レイヤーと、その副作用境界(DB / Discord / S3 / state ファイル)。

### 目的
- レイヤー責務ごとの**壊れてはいけない性質**を明文化する。
- Backtest の**時系列整合性・境界値**を回帰として固定する。
- `--dry-run` / `dry_run=True` の**無副作用**を保証する。
- 重要ケースと既存pytestの**対応関係**を追跡可能にする。

### 範囲
- `backtest/` / `market-watch/` / `news-feed-bot/` / `paper-signal/` / `paper-execution/` の単体・軽量結合テスト。
- 副作用抑止(DB作成/更新・Discord送信・S3アップロード・presigned URL・state更新)の検証観点。

### 非対象(Non-Scope)
- 全単体テストの人間向け転記、手動試験の証跡保存。
- CI workflow の実装(#32)。
- integration test / heavy backtest の全面整備。
- 自動売買・Broker API・Live Execution(現行スコープ外・未実装。仮想執行の Paper Execution は対象に含む)。

---

## 2. テスト実行方法

ルートに `pytest.ini` / `conftest.py` / `pyproject.toml` は**存在しない**。各コンポーネントのテストは**ディレクトリ単位で個別に**実行する(各テストファイルが冒頭で自分の `sys.path` を設定する)。

```bash
python -m pytest backtest/tests -q
python -m pytest market-watch/tests -q
python -m pytest news-feed-bot/tests -q
python -m pytest paper-signal/tests -q
python -m pytest paper-execution/tests -q
python -m pytest broker/tests -q
python -m pytest execution/tests -q
```

CI(#32)では最低限この7スイートを継続実行することを想定する(現行の `.github/workflows/ci.yml` は7スイートすべてを実行している)。テストは外部ネットワーク・実DB・実S3・実Discordに到達しない(§4–§5)。唯一の例外は OANDA Demo 実 API テスト(`broker/tests/test_oanda_demo_adapter_demo_api.py`、Issue #79)で、**通常 CI からは分離**されている: `BELIAL_RUN_OANDA_DEMO_API_TESTS=1` と Demo credentials(`OANDA_API_TOKEN` / `OANDA_ACCOUNT_ID`)を明示設定したときのみ実行され、未設定なら全件 skip されるため、7 スイートの通常実行は引き続き外部通信ゼロである。

加えて、Execution 層には**固定シナリオ試験**(Issue #77。Issue #78 / PR #100 の Shadow Mode シナリオを含め現在 34 シナリオ)がある。`execution/scenarios/*.json` の固定データを共通 runner が本番の Execution 経路(`LiveOrderSubmissionService` / `SafetyGate` / `execution_store` / FakeBroker)へ流し、expected と actual を比較する。通常の pytest がモジュール単位の観点を固定するのに対し、固定シナリオは「正常約定・拒否・部分約定・timeout(UNKNOWN)・DB保存失敗・多重起動競合」等の**分岐シナリオ全体を1件ずつデータで再現**する。pytest(`execution/tests/test_fixed_scenarios.py` がシナリオ id 単位で parametrize)からも、CLI からも実行できる:

```bash
python -m execution.scenario_runner                # 全シナリオ。1件でも不一致なら終了コード1
python -m execution.scenario_runner --list         # シナリオ一覧
python -m execution.scenario_runner --scenario-id timeout_unknown_no_resend
python -m execution.scenario_runner --json         # machine-readable な結果
```

CLI の終了コードは 0=全 PASS / 1=FAIL(expected不一致)または ERROR(未処理例外)あり / 2=usage・シナリオ定義エラー。この契約はコンソール encoding に依存しない(Windows 既定の cp932 でも `PYTHONUTF8` なしで動作。区切りは ASCII、`--json` は pure ASCII。cp932 再現 pytest と CI の Windows ジョブで固定)。一時ディレクトリの SQLite と FakeBroker のみを使い、外部通信・既定パスのDB・`.env`・stateファイルへは到達しない。キャンセルは Production キャンセルユースケース未実装のため固定シナリオの対象外(通常 pytest が担当。PR #96 レビュー)。schema・実行方法・シナリオ追加手順の正は `execution/README.md` の「固定シナリオ試験」節。

Execution 層には Shadow Mode 用 CLI(Issue #78 / PR #100)もある。いずれも secrets・ネットワーク不要で、`--json` は pure ASCII・ログ非混入:

```bash
python -m execution.shadow_runner --mode shadow --signal-id <id> --signal-db ... --execution-config ... --market-snapshot ... --account-snapshot ...   # Production Signal 経路
python -m execution.shadow_runner --mode shadow --cycle <cycle.json>   # CLI fixture 経路(cli_fixture として記録。0=完了 / 1=conflict・入力未解決・store障害 / 2=usage・設定不正)
python -m execution.shadow_runner --mode dry-run ...                   # 同じ評価で完全無副作用(true read-only probe・DB未作成)
python -m execution.shadow_report [--limit N | --execution-request-id ... | --source-signal-id ... | --safety-decision-id N | --claims | --orphan-decisions | --decision rejected] [--json]
```

現状のテスト件数(`def test_` 実数、目安):backtest 128 / market-watch 127 / news-feed-bot 117 / paper-signal 89 / paper-execution 91 / broker 228(Issue #73 / PR #87、Issue #75 接続identity追加、Issue #77 / PR #96 FakeBroker最小拡張 + broker_order_id一意性・拒否原子性、Issue #79 / PR #101 OANDA Demo Adapter、Issue #103 注文列挙 `OrderListingBroker`)/ execution 673(Issue #74 / PR #91、Issue #75 Safety Gate / Live Order Submission、Issue #76 二重発注防止 / idempotency、Issue #77 / PR #96 固定シナリオ試験、Issue #78 / PR #100 Shadow Mode = Production Signal経路・claim-first・provenance・true read-only・strict JSON、Issue #103 Broker reconciliation 差分検出、Issue #104 Broker reconciliation 修復 追加)。

---

## 3. レイヤー別テスト観点

| Layer | モジュール(代表) | 主なテスト観点 | 代表テストファイル |
|---|---|---|---|
| Observation / data collection | `market-watch/chart_collector.py`, `news-feed-bot/market_data.py` ほか | 取得結果の正規化、SQLite保存/期間読み込み、取得失敗時の挙動 | `test_chart_collector.py`, `test_market_data.py` |
| Indicator / classification | `market-watch/indicators.py`, `news-feed-bot/market_bias.py` | 指標計算の正しさ、warm-up未達はNaN、trend/risk/volatility分類 | `test_indicators.py`, `test_market_bias.py` |
| Alert | `market-watch/{macd,ma,rsi,bollinger}_alert_bot.py` | 検知条件、重複抑止、dry-run無副作用、webhook必須検証 | `test_*_alert_bot.py` |
| Paper Signal / hypothesis | `paper-signal/signal_generator.py`, `signal_models.py` | Bull/Bear/Neutral別候補生成、confidence閾値、`status=paper_only` 固定、重複窓 | `test_signal_generator.py`, `test_paper_signal_store.py` |
| Exit / Invalidation | `paper-signal/exit_policy.py` | fixed_rr / time_based_close / ma_break、同足優先、open(no_close)、空/None candles | `test_exit_policy.py` |
| Backtest | `backtest/{runner,warmup,summary,report,backtest_store,backtest_models}.py` | 境界分離・future leak禁止・skip・DB保存・集計 | `test_warmup.py`, `test_runner.py`, `test_backtest_engine.py` ほか |
| Paper Execution | `paper-execution/{execution_engine,execution_runner,position_store,position_models,execution_profile,paper_execution_notifier}.py` | Entry / Close判定の決定論、`state × terminal_reason` 閉集合、`signal_id` 冪等、schema migration(positions table rebuild / v1→v3 additive / v2 prototype→v3 outbox rebuild / 実制約検証 + behavioral probe / migration rollback)、strict read、dry-run無副作用(WAL/rollback-journal DB のファイル完全不変を hash/mtime で検証)、Discord通知 **Transactional Outbox**(遷移+enqueue の atomicity、enqueue失敗時の position rollback、commit後・HTTP前クラッシュからの再送、claim/lease による並行runner排他=HTTP 1回、lease期限切れ回収、at-least-once=success記録失敗時の同一Event ID重複、本文生成・None/Unicode、timeout/429/5xx/connection error 分類、dead-letter継続検知、Webhook未設定滞留、batch limit、outbox永続化failureは`failures`・配送failureは`notification_failures`) | `paper-execution/tests/test_*.py` |
| Execution Request / 注文状態永続化(将来のLive Execution用。Issue #74 / PR #91、Issue #76) | `execution/{__init__,execution_models,execution_store}.py` | `ExecutionRequest` 構築不変条件(Decimal・UTC・必須ID非空・quantity/price正の有限値・MARKET/LIMITとrequested_priceの整合・SL/TPとdirectionの一意に保証できる関係のみ・naive datetime拒否)、`to_order_request()` 変換(SL/TP・provenanceがBroker境界を越えない)、SQLite永続化(保存・復元・`client_order_id` 1:1一意・重複conflict)、送信フェーズ(created→submitting一度きり・2接続競合で1更新のみ成功・submittedは確定観測でのみ導出)、**完全OrderResult snapshotのhistory行へのanchor永続化とanchored snapshotをoldとする `validate_order_result_update` の常時適用**(stale snapshotによるfill消失・数量/ID/時刻巻き戻しの拒否、`previous_result` とanchored snapshotの一致要求、再起動後も検証が機能、UNKNOWN中のcurrent行snapshot差し替え検出、連続snapshotのpairwise検証、state-only APIはUNKNOWN限定でbroker_order_id新規導入不可)、既存 `validate_order_state_transition` による遷移検証(terminal離脱拒否・UNKNOWN許可解決先のみ・fillを失う解決の拒否)、broker_order_id差し替え/欠落拒否と**DB全体UNIQUE(競合時current/history全rollback・復元時の重複scan)**、updated_at逆行拒否、same-state再観測の履歴記録(terminalは内容不変)、current+snapshot+履歴の原子性、**schema完全検証を全read/write経路で実施**(pristine判定=オブジェクト0件かつuser_version 0のみ新規作成、version-only/metadata-only DBの拒否、正規化CREATE SQL・PRAGMA構造signature・制約・index・予期しないテーブル。partial/制約欠損/他DBはDB無変更で拒否)、DB CHECK制約の発火(NULL PK含む)、復元時のcurrent/history chain・enum・UTC・reason・snapshot整合検証(改竄拒否・未知JSON field拒否)、UNKNOWN保存・照会対象抽出・自動変換なし、DB再オープン後の完全復元(fills/Decimal/UTC含む)、missing DBを作成しないread/update、**broker_order_id 所有者の read-only 照会**(`get_execution_order_by_broker_order_id`: 所有 request の特定・未割当は None・空 ID 拒否・missing DB を作らない・読み取りで rows/history/バイト列不変・再取得で同一・schema 不一致は既存契約どおり失敗)、**Issue #76 idempotency**(`signal_id` のDB全体UNIQUE=同一source signalからのExecution Request重複生成拒否、重複抑止のWARNログ(execution_request_id/signal_id/既存行のphase・state/理由)、独立DB接続2本の同時作成で1行のみ、帯域外INSERTも拒否、`get_execution_order_by_signal_id`、**schema v2→v3のadditive migration**=単一トランザクション・既存行完全保持・migration後の通常読み取り・冪等・重複signalを含むv2の明示失敗と無変更・read/write経路はmigrationしない)、ルートからのpackage import(subprocess)、Paper/Observationレイヤーがexecutionをimportしないこと(AST走査)。`tmp_path` のSQLiteのみ使用 | `execution/tests/test_*.py` |
| Safety Gate / Live Order Submission(発注前検証・将来のLive Execution用。Issue #75、Issue #76) | `execution/{safety_gate,safety_store,live_submission}.py` | 正常許可(FakeBrokerへ1件送信+監査記録+execution storeのphase/state記録)、**固定client_order_id**(register時に1:1束縛、submitは受け取らない)、**二重送信防止**(2回目は`duplicate_submission`・Broker送信1件のまま)、**UNKNOWN再送禁止**(timeout/通信失敗→UNKNOWN記録→再submit拒否)、**created→submitting勝者のみ送信**(先行遷移・遷移インフラ失敗のfail-closed)、Broker拒否は結果として記録、**受領済み結果の保存失敗→state-only UNKNOWNフォールバック+`list_unresolved_submissions()`での可視性**(二重失敗時のsubmitting/pending取得・送信前クラッシュ相当のstuck pending取得・元例外の伝播)、未登録意図拒否、破損order storeのfail-closed、**封印された監査store+read-back検証**(コンストラクタは`decision_db_path`パスのみ受理・recorderオブジェクト注入は構築時拒否・record消失のread-back検知・未初期化storeの送信阻止・元コード保持・**read-backはmembership判定=並行する他callerの判定が record と read-back の間に割り込んでも `decision_persist_failed` を誤付与しない(PR #101。旧「最新行一致」要求はLinux CIのflake原因)**)、LIMITのSL/TPは`requested_price`基準・MARKETは想定約定サイド(BUY=ask / SELL=bid)基準、各安全条件の独立拒否(許可銘柄/方向/数量上限/SL必須/SL・TP不正(BUY・SELL両方向)/snapshot銘柄不一致/参照価格欠損・stale・**未来時刻**/spread欠損・上限超過/market closed/not tradable/接続先不一致・未知・大文字小文字)、**Broker実体から導出する型付き接続identity**(`BrokerConnection`: mode/targetは`connection_identity()`から導出されcaller申告は`TypeError`・FakeBrokerはlive/demoを名乗れない・非FakeBrokerはfakeを名乗れない・未知mode・identity欠損/型不正の構築時拒否・**組み立て後のidentityドリフトをevaluateの再導出が拒否**)、kill switch(全条件正常でも拒否・照会例外・**bool以外の応答**のfail-closed)、ポジション上限(件数ベース・long+short合算・口座全体・新規注文+1計上・stale/未来snapshotのfail-closed)、Broker照会失敗(positions/account/型不正)、**AccountStateのstale/未来/型不正のfail-closed**、想定外例外・clock破損の`gate_internal_error`、複数違反の同時収集と**順序保持dedup**(コード一意性不変条件含む)、**全拒否経路で`submitted_requests`が0件(または増加0)**、**Issue #76 idempotency**(同一signalの2重register拒否・ACCEPTED/SUBMITTING/UNKNOWNの再処理非送信・サービス再作成=プロセス再起動相当後の重複抑止・独立store接続の2サービス同時submitでBroker送信1件・重複抑止ログのexecution_request_id/signal_id/状態/理由)、ログへの拒否コード出力、`SafetyPolicy`/`MarketSafetySnapshot`/`SafetyDecision`構築時検証(NaN/Infinity/0/負値/naive datetime/spread網羅/接続先のmode重複)と**policyのdeep immutability**(呼び出し元dictの事後変更が判定へ不影響・read-only viewへの書き込み拒否)、監査store(再オープン後のround-trip・記録順・missing DB書き込み拒否とファイル非作成・読み取り空・pristine判定・foreign schema/制約欠損/trigger/version不一致のDB無変更拒否・帯域外改竄の復元時拒否・DB CHECK制約発火)、`submit_order`/`to_order_request`呼び出し箇所のリポジトリ全体AST走査(サービス境界迂回の構造的禁止)。`tmp_path`のSQLiteとFakeBrokerのみ使用 | `execution/tests/test_safety_gate.py`, `test_safety_store.py`, `test_submission_boundary.py` |
| Execution 固定シナリオ試験(Issue #77 / PR #96) | `execution/{scenario_models,scenario_runner}.py`, `execution/scenarios/*.json` | シナリオ定義の closed-world schema validation(必須フィールド欠落・未知キー・未知 behavior/状態/拒否コード・型不一致・重複 scenario id・不正 expected 形式の明示的拒否、all-or-nothing ロード、不正定義の silent skip 禁止)、runner の PASS/FAIL/ERROR 分類(expected 一致=PASS・不一致=FAIL+フィールド単位差分・未処理例外=ERROR+traceback、actual を expected へ書き換えない)、CLI 契約(終了コード 0/1/2、`--scenario-id`/`--scenario-file`/`--list`/`--json`)、**コンソール encoding 非依存**(ASCII 区切り・pure-ASCII JSON・自由テキストのエスケープ退避。cp932 strict stream を再現した pytest で通常/`--list`/`--json`/FAIL 終了コードを固定。PR #96)、**stdout/stderr 設定の非漏洩**(r2: `main()` の encoding 退避は正常終了・usage error・schema error・mismatch・未処理例外の全経路で元設定へ復元、import 時は無変更 — 全経路の復元を pytest で固定)、**状態履歴のオブジェクト比較**(件数・順序の厳密比較+`previous_state`/`new_state`/`reason`/`broker_order_id` の部分比較、キー省略と明示 null の区別、`changed_at_utc` の存在・単調性の常時検証、動的 timestamp の完全一致比較なし)、シナリオ間の一時DB分離と実行順序非依存(同一 id を再利用する2シナリオが両方 PASS)、外部通信ゼロ(socket 無効化下で PASS)、fault injection の関数置換が終了後に必ず復元されること、runner が判定ロジックを再実装しないこと(遷移表・validator・拒否コード enum を import しない AST 検査)、**全固定シナリオの統合実行**(必須シナリオ id の存在 pin + シナリオ id 単位 parametrize で本番経路を通して全件 PASS: 正常成行 / 同一Signal重複 / Broker送信前失敗 / 通信失敗UNKNOWN / timeout UNKNOWN+再送禁止 / Broker拒否 / 部分約定 / 約定価格乖離 / DB保存失敗3境界(初期保存・受付後結果保存・状態履歴二重失敗) / stale price / kill switch / execution mode不整合 / barrier同期の多重submit競合=Broker呼び出し1回 / 同一signal同時register=1行 / ACCEPTED・SUBMITTING再実行 / 複数違反同時収集 / 不正状態遷移拒否 / 同一IDでの異payload抑止)。**キャンセルは固定シナリオ対象外**(PR #96: Production キャンセルユースケース未実装のため。FakeBroker のキャンセル挙動は broker/tests、store の CANCELLED 遷移は test_execution_store.py の通常 pytest が担う) | `execution/tests/test_scenario_models.py`, `test_scenario_runner.py`, `test_fixed_scenarios.py` |
| Shadow Mode(Broker送信なしの検証記録。Issue #78 / PR #100) | `execution/{shadow_models,shadow_store,shadow_execution,shadow_providers,shadow_config,shadow_composition,shadow_runner,shadow_report,sqlite_ro,strict_json}.py` | **Production Signal 入力経路**(保存済み Signal の provider 解決・Production 設定の完全明示 parse・Signal→ExecutionRequest の決定的 identity 導出・composition root・欠損/未設定の fail-closed・retry は duplicate)、**input provenance**(production_signal / cli_fixture / scenario_fixture の分離、signal / config / snapshot source と config version の保存・表示)、**Broker API 呼び出しゼロの構造保証**(broker/connection 引数が存在しない・全 FakeBroker API 毒化で全 cycle 完走)、**claim-first**(Gate 評価前の identity claim、loser は Gate 非評価・decision 非保存 = **completed 1 件 : Safety Decision 1 件**、**同一 service instance を 20 thread で共有する race を含め** 8 worker×20 反復 + 20 worker 単発で recorded 1 / duplicate n-1 / error 0 / decision 1 / Gate 評価 1 回、lock エラーは duplicate に分類しない)、**crash / stale claim 回復**(claim は attempt id + fencing generation で所有され有効 lease 中は同一 owner でも再取得不可・lease 切れ takeover が generation を +1・takeover 後の旧 attempt complete は fencing 不一致で拒否・orphan decision の report 検索)、**duplicate vs conflict**(canonical payload: Decimal 数値正準化 `1`≡`1.0`・キー順序非依存・evaluation-time 値の除外、同一 ID 異 payload / 異 client_order_id / identity 混用は `ShadowIdentityConflictError` + 差分フィールド、conflict は decision を増やさない、並行異 payload で winner 1)、**実行可能 status の closed set**(`paper_only` 完全一致のみ実行可・case/空白違い・NULL・空・archived・未知 status は Signal id 付きで fail-closed・拒否時は request/claim/decision/記録を一切作らない)、**account snapshot 監査保存**(全 position 含む round-trip・position/account 上限拒否の事後再現・投影列整合(account_id 列 ⇔ snapshot ⇔ 参照 decision の 3-way・position 件数含む)・policy snapshot の closed-world 再構築 + round-trip 検証(数値は有限値のみ — `Infinity`/`NaN` 注入は `OverflowError` でなく安定した integrity error)・改ざん検出・secret 非保存)、**winner 限定の snapshot provider I/O**(market/account supplier は claim 獲得後にのみ解決 — duplicate loser / in-progress duplicate は provider を呼ばない(counting provider で固定)、supplier 失敗は claim を保持し lease 回収へ接続)、**true read-only**(`sqlite_ro`: missing/parent/WAL/SHM 非作成・mtime/size 不変・read-only FS・write 必敗・corrupted 明示失敗・**authorizer による ATTACH/DETACH/DDL/状態変更 PRAGMA の deny — 欠損/既存/日本語/空白/`#`/`%` パスへの ATTACH や VACUUM/journal_mode/user_version/INSERT 等でファイルシステム完全不変**)、**strict JSON**(重複キー(ネスト含む)・不正 UTF-8・BOM・深さ/サイズ/文字列/配列/キー数上限・NaN/Infinity・bool-as-number・巨大指数の安定拒否)、dry-run 無副作用(ファイル一覧・バイト・mtime 不変)、execution mode 語彙、CLI(Production/fixture 両経路・provenance 表示・conflict exit 1 + reason code・cp932・stream 復元)、report(`--limit`・`--safety-decision-id`・`--claims`・`--orphan-decisions`・新しい順・**参照 Safety Decision row との埋め込み判定フィールド一致検証 — 改ざん/欠損記録は text/JSON とも黙って表示せず exit 1、decision 参照がある限り Decision store ファイル欠落も fail-closed(参照ゼロの standalone copy のみ閲覧可)。**`--orphan-decisions` 経路も同一検証**(行削除 / account_id / position_count / request_id 不一致 / NULL 参照破損の 5 種 × text・JSON で exit 1・stdout 空)**、**使用不能な Decision store の正規化**(空ファイル / 非 SQLite / table 欠損 / column 欠損 / `user_version` 不一致 / malformed row / 破損 JSON / 物理破損 の 8 種 × text・JSON で `ShadowReportIntegrityError` → 安定 exit 1・traceback なし・stdout 空・ファイル一覧/サイズ/バイト不変。`--orphan-decisions` 経路も同様)、**`safety_decision_id IS NULL` ⇔ `decision_persist_failed` の双方向不変条件**(参照を失った通常記録・参照を持つ persist-failure 記録はいずれも model 構築時/復元時に拒否され report も exit 1、正当な persist-failure 記録は正常表示、dry-run 投影は両 id NULL))、shadow store 書き込み境界と runner の ShadowResult 非構築の AST 走査 | `execution/tests/test_shadow_*.py`, `test_sqlite_ro.py`, `test_strict_json.py`, `test_submission_boundary.py` |
| Broker Reconciliation 差分検出(将来の Live Execution 用。Issue #103) | `execution/{reconciliation_models,reconciliation_store,broker_reconciliation}.py` | **検出専用契約**(Broker submit/cancel と execution order DB write は production 経路で 0、`detect()` は reconciliation DB にも無副作用)、**差分検出**(SUBMITTING / ACCEPTED / PARTIALLY_FILLED / UNKNOWN / terminal を照合し、FILLED・CANCELLED・REJECTED、終端矛盾、Brokerのみ、Broker missing、UNKNOWN継続、状態一致でも identity / broker_order_id / snapshot 契約不一致を分類)、**分類**(既存 state machine / snapshot validator を再利用し `auto_repair_candidate` と `manual_review` を区別するが修復しない)、**broker_order_id 一意性整合**(`get_execution_order_by_broker_order_id` の read-only 所有者照会 = 所有 request 特定・未割当 None・空 ID 拒否・missing DB 非作成・読み取りで rows/history/バイト列不変・再取得で同一・schema 不一致は既存契約どおり失敗。他 Execution Request が所有する `broker_order_id` の観測は PENDING / UNKNOWN とも `manual_review`(detail に競合 ID・所有者 ID・照合中 ID、両注文の状態と履歴が不変)、未割当 / 自己所有 / broker id 無し観測は従来分類、**所有者照会の store 読み取り失敗は fail-closed で `manual_review`**)、**照会失敗**(timeout / communication / auth / rate-limit / malformed return / adapter想定外例外を対象ID・型付きcode・detail付き `query_failure` として記録し他注文を継続、列挙非対応・失敗・malformed・重複は `broker_listing_error`、`has_partial_failures` で部分失敗を機械判定)、**Broker列挙**(Broker-only、重複ID拒否、未対応と0件を区別)、**冪等性・永続化**(run単位append、同一run ID拒否、複数回・並行run、transaction rollback、process restart、closed-world schema、corrupt / 非SQLite拒否、missing DB非作成)、ログと動的副作用検査。`tmp_path` SQLite と in-memory FakeBroker のみ使用 | `execution/tests/test_reconciliation_models.py`, `test_reconciliation_store.py`, `test_broker_reconciliation.py`, `test_execution_store.py`, `test_submission_boundary.py` |
| Broker Reconciliation 修復(将来の Live Execution 用。Issue #104) | `execution/{reconciliation_repair_models,reconciliation_store,broker_reconciliation_repair}.py`, `execution/execution_store.py` | **安全な修復**(保存済み run の `auto_repair_candidate` のみを入力に、UNKNOWN → FILLED / CANCELLED / REJECTED、SUBMITTING(PENDING)/ ACCEPTED / PARTIALLY_FILLED → Broker 確認済み許可状態を既存状態機械 API 経由で確定。fill prefix 保全)、**attempt ライフサイクル / claim**(全 attempt ID の事前生成・空/重複/factory 例外は無副作用で pass 拒否、claim = pending を状態更新前に永続化(owner / claim execution id / generation 1 / lease)・attempt ID 再利用は reservation 時点で拒否、**takeover は generation の原子的 CAS**(stale generation / finalized 済み / 未知 attempt は typed 拒否、**4 thread barrier で 1 勝者のみ・generation はちょうど 2**)、**finalize は claim 込み CAS**(旧 generation の finalize は `RepairClaimLostError` で pending 維持、現 claim だけが確定できる、`finalized_at_utc` 必須)、二重 finalize / identity 不一致の拒否)、**eligibility 再検証**(保存結果を信用しない: `client_order_id` 対応・**revision gate** = UNKNOWN→UNKNOWN 履歴追加 / same-state snapshot・timestamp 更新 / 部分約定増加を state 同一のまま stale 検出・revision 無し v1 観測は stale・identity / 遷移 / snapshot 契約 / 時刻の validator 再通過・遷移表非緩和 = 捏造 candidate の拒否)、**already_applied の 3-way 検証**(Broker 再照会なしに確定しない: 照会失敗は query_failed、fresh の state / 数量 / 平均価格 / 参照 ID / timestamp 逆行の差異は conflict、完全一致時のみ already_applied)、**変更しないケース**(manual_review / query_failed 入力・broker-only は skipped、stale、再照会不一致・Broker UNKNOWN・参照 ID 変化・他 request 所有 ID・lookup 後の所有権競合は conflict、遷移拒否は rejected、repair 時 timeout / 通信失敗 / not found / Broker 境界契約違反は typed query_failed — いずれも内部 row / 履歴 / バイト列不変)、**Broker 参照 ID 保全**(既知 ID の NULL 化・置換の拒否、DB 全体 UNIQUE 所有者照会、store 読み取り失敗は fail-closed)、**冪等性・並行実行**(`record_order_result(expected_current_state=..., expected_revision=..., repair_attempt_id=...)` の原子的 revision CAS + history 側 partial UNIQUE。再実行は already_applied で履歴不変、並行 2 worker で applied は 1 件のみ・履歴 +1 行のみ、CAS 不一致時の DB 無変更 rollback、normal submission との競合は stale)、**post-commit 安全性**(APPLIED attempt は commit 前に完全構築: commit 後の clock 失敗 / result 構築失敗 / store 戻り値破損でも committed repair が failed にならず、監査と実 DB が一致し、run-level error に完了済み attempt が添付される)、**run-level integrity failure**(typed 例外のみ outcome 化。KeyError 等のプログラマエラーは pass 停止 + 部分結果を error と監査 DB の両方から取得可能 + 処理途中対象は pending 残置)、**recovery / lease**(lease 有効な pending は**完全に不可侵** = Broker 照会 0 回・両 DB バイト不変・finalize なし、lease 期限切れのみ fence bump → claim takeover → history の構造化 `repair_attempt_id` / `repair_generation`(reason 文字列を解析しない)で元 attempt ID のまま applied 確定、provenance が無ければ **Broker 再照会 + eligibility 再実行**(単なる failed 認定をしない)、finalize 後の再 recovery は no-op、run / discrepancy 改竄時は復旧拒否、`broker_timeout < lease_duration` の構築時検証)、**genuinely concurrent fault injection**(threading の Event / Barrier で実際に重ねる: live worker を Broker 照会中 / fence 登録前 / finalize 直前で停止させたまま recovery を走らせ、lease 有効なら不可侵・lease 切れなら新 generation のみ有効で旧 worker の commit / finalize が typed に拒否されること、Execution DB と監査 DB の最終 outcome 一致・provenance history はちょうど 1 行・attempt ID 同一・generation 遷移が正しいこと。**2 worker / 10 worker の同時 takeover** で勝者 1 つ・generation ちょうど 2・Broker 照会は合計 1 回・敗者は fence を変えないこと。live 2 worker 同時 repair で applied 1 件・履歴 +1 行)、**fence**(generation 1 で claim → generation 2 で takeover → generation 1 の `record_order_result` 拒否 + rollback → generation 1 の再武装拒否 → generation 2 だけ applied → history provenance は generation 2 → current row は 1 回だけ更新、fence 無しの repair commit 拒否、別 attempt による置換で旧 attempt を締め出す、同一 claim の再登録は冪等、未知注文への登録拒否、read-only 契約、**4 thread barrier で takeover 勝者 1 つ**)、**logging 隔離 / sanitize**(handler / formatter 故障を注入しても outcome・監査・pass 継続が不変。**実際の secret 文字列 12 種**(token / API key / password / authorization header / account ID / endpoint URL query / raw HTTP response / SQL / DB パス / Windows パス / Unix パス / 大文字小文字違い / URL encode 済み / cause chain)を非 `BrokerError` 例外へ注入し、監査 detail・全ログレコード(message / args / exc_info)から不在であることを確認)、**dry-run**(全評価は実行するが注文更新・履歴・**claim / lease / generation**・**fence 行**・監査行・DB 作成 / migration・ファイル生成の全副作用 0。両 DB のバイト列 / mtime / ディレクトリエントリ不変、破損 DB は無変更で拒否、would_apply は非永続で claim 列を持たない)、**監査 / failure code closed set**(サニタイズ済み detail + `RepairFailureCode` Enum。生例外文字列を保存しない。**outcome × failure code の許可組合せ**(applied / already_applied は code なし、query_failed は query 系必須、failed / rejected / stale / conflict / skipped は各許可集合)・**unknown code の行は復元時 fail-closed**・DB CHECK は Enum から生成。attempt ID 重複拒否・未記録 run / discrepancy 拒否・dry-run 記録拒否・改竄行 / malformed pending の復元時 fail-closed)、**timestamp 契約**(`broker_state_updated_at_utc` は Broker clock 域で内部 commit 時刻ではない・`finalized_at_utc` は finalize 時に取得し失敗しても outcome を変えない・clock 呼び出しは pass 開始 / claim / finalize / pass 終了の 4 点のみ)、**schema migration**(reconciliation v1 → v2: discrepancies へ ALTER + claim 列を持つ repair テーブル追加・完全一致のみ・単一トランザクション・失敗 rollback・読み取り経路は migration せず init を指すエラー。execution v3 → v4: history provenance 2 列 + partial UNIQUE + `execution_repair_fences` テーブル・v2 は連鎖 migration・既存行 NULL 読み戻し・fence テーブルは空で開始)、**Broker write 禁止**(毒化 facade + submit / cancel 0 記録 + モジュール AST + repo 全体 AST + `record_order_result` 呼び出しちょうど 1 箇所)。`tmp_path` SQLite と in-memory FakeBroker のみ使用 | `execution/tests/test_reconciliation_repair_models.py`, `test_broker_reconciliation_repair.py`, `test_reconciliation_store.py`, `test_execution_store.py`, `test_submission_boundary.py` |
| 副作用境界 | 各store / uploader | 一時DB利用、dry-run抑止、run/signalの分離DB、FK/重複ID | `test_*_store.py`, `test_chart_alert_context.py` |
| Broker境界(将来のLive Execution用。Issue #73 / PR #87、Issue #79 OANDA Demo Adapter) | `broker/{__init__,broker_models,broker_interface,fake_broker,oanda_demo_adapter}.py` | 共通モデル不変条件(Decimal・UTC・数量整合・フィールド型不正は`BrokerInvalidRequestError`/整合性違反は`ValueError`)、注文状態遷移の閉集合(timeout後の`unknown`は`rejected`と別状態。`unknown`は既知fill保持可。同一状態更新は全状態で許可)、スナップショット更新不変条件(`validate_order_result_update`: 識別/要求フィールド不変・fill単調性・`fill_id`一意性(snapshot内・追加fill)・fills prefix保持・`broker_order_id`はNone→値のみ・`updated_at_utc`単調非減少・terminal same-state再観測は`updated_at_utc`以外全フィールド不変・UNKNOWN解決先の二重検証)、構築時クロスフィールド検証(LIMITは`requested_price`必須/MARKETはNone必須/`pending`は`broker_order_id`禁止)、型付きBrokerエラー分類、FakeBrokerのScripted Scenario(即時/未/部分約定・拒否・timeout・通信失敗・キャンセル成功/失敗・約定価格乖離・各照会)とScenario/設定APIの構築時検証、**broker_order_idの注文間一意性と拒否の原子性**(PR #96: 固定IDと連番IDの共通割当集合・割当済みIDの固定指定拒否と不完全注文の非残留・連番の衝突スキップ・並行submitでの一意性・単一lock境界。r2: 重複拒否がfill sequence・決定的clock・割当集合を一切消費しないこと=FillImmediately/PartiallyFill両系統、拒否後の次注文のfill ID・timestamp連続性。`submitted_requests`は送信試行の記録契約)、**注文列挙(Issue #103)**: optional Protocol `OrderListingBroker` が `Broker` の 5 操作と分離されていること(`Broker` は 5 操作のまま・5 操作のみの実装は `Broker` を満たし `OrderListingBroker` を満たさない・FakeBroker は両方を満たす)、`list_orders()` の決定的順序・空・現在 snapshot 反映(cancel 後)・通信失敗注文の非出現・timeout の unknown 出現・呼び出し記録を増やさないこと、外部通信ゼロ(AST import走査+socket無効化。走査対象は境界モジュール+FakeBroker)、Paper Signal / Paper Execution / Backtest(Strategy関連)がBroker層をimportしないこと(rglob+AST)、リポジトリルートからのpackage import(subprocess検証)。**OANDA Demo Adapter(Issue #79 / PR #101。`oanda_demo_adapter.py`)**: Demo endpoint allowlist の fail-closed(Live host・未知 URL・大文字小文字違いの構築時拒否)、**redirect 非追従**(30x で 2 回目の接続が発生しない・既定 transport の `allow_redirects=False`・submit 中 3xx→TIMEOUT / 読み取り 3xx→COMMUNICATION_FAILURE)、環境変数のみの設定(`from_env` の欠損変数名指摘・実値非echo・timeout の有限正数限定)、secrets 非漏洩(`repr` からの token/account/**endpoint** 除外・全エラーメッセージに token/account/URL を含まない・**Broker echo する errorMessage/rejectReason 内の token/account/endpoint scrub**=**redact してから 200 字へ切り詰める順序**(切り詰め境界をまたぐ secret の断片非残留を token/account/endpoint 各々で検証)+**一致区間結合による一括 redaction**(包含でない部分重複 `abcdef`×`defghi`・3 区間連鎖・同一 secret 複数出現/重複登録・空 secret 無視・隣接区間結合でも断片非残留)・**endpoint 拒否例外の固定文言=入力値も許可 URL も非echo**・transport 例外の cause chain 遮断(`from None`)・`AccountState.account_id` は**実 ID 保持**(識別子契約)+応答 account 照合+表示境界の共通ヘルパー `safe_account_reference`(`display_account_id`・`SafetyDecision` repr 除外・shadow report text/JSON・shadow 整合性例外)・connection identity target が URL でなく定数ラベル)、注文 payload 変換(MARKET=FOK / LIMIT=GTC・符号付き units・`clientExtensions.id`=client_order_id)、**client ID の URL-safe charset 検証**(予約文字・空白・Unicode・129 字超の transport 到達前拒否・128 字上限の受理)、**応答 correlation 検証**(fill/create/cancel transaction・order object の client ID echo 欠損/不一致→COMMUNICATION_FAILURE。**submit 内 cancel transaction は空 object・非 Mapping・id 欠損・他注文の cancel 混在でも terminal CANCELLED を返さず fail-closed**、**fill の orderID 欠損時の create fallback は create transaction の client ID 相関を必須化**=他注文の create から broker_order_id を採用しない)、レスポンス変換(fill→FILLED・**部分約定→PARTIALLY_FILLED / 残量 cancel→CANCELLED・zero/over-fill→COMMUNICATION_FAILURE・reissue **field の key 存在**で明示非対応の fail-closed(TIMEOUT=outcome unknown・送信1回・Issue #102 へ分離。**値が文字列・配列・空 object・null の malformed でも正常継続せず、HTTP 2xx/400/404 とも business 解釈より先に検査=reject transaction と同居する 400/404 でも REJECTED へ確定しない**)**・FOK cancel→CANCELLED・create→ACCEPTED・`orderRejectTransaction`→REJECTED 結果返却・**duplicate `CLIENT_ORDER_ID_ALREADY_EXISTS`→INVALID_REQUEST(FakeBroker と同一契約)**)、エラー taxonomy 変換(timeout→TIMEOUT+**送信 1 回のみ=自動再送なし**、接続失敗→COMMUNICATION_FAILURE、401/403→AUTHENTICATION_FAILURE、429→RATE_LIMITED、submit 後 5xx/不正 body→outcome unknown として TIMEOUT、読み取り 5xx→COMMUNICATION_FAILURE、**モデル不変条件違反の応答→COMMUNICATION_FAILURE で裸の ValueError 非漏洩**)、照会・キャンセル(`@<client id>` 指定子・404 の NOT_FOUND / terminal キャンセル不能の判別・**cancel PUT 応答自体の相関検証=`orderCancelTransaction` 欠損 / `clientOrderID` 欠損・不一致 / malformed body / post-cancel snapshot が CANCELLED でない不整合の fail-closed**・post-cancel snapshot)、position/account/instruments 変換、**open trades 変換(`TradeInfo`・空・malformed・401/404/429/5xx)**、**応答由来の zero/負価格(fill・filling transaction・open trade)→COMMUNICATION_FAILURE(caller 起因 INVALID_REQUEST と区別)**、nanosecond timestamp の UTC parse、fake transport による socket 無効化下の全経路 PASS。インメモリのみ(tmp_path不要)。実 Demo API テストは opt-in の別ファイルで通常 CI では skip(§1) | `broker/tests/test_*.py` |

補足:
- Market Bias は**時点参照(point-in-time)**が肝。`get_market_bias_as_of()` は評価時刻より**後**のレコードを返してはならない(§6 SE系)。
- Alert層は「事実検知のみ・売買判断をしない」責務であり、テストも通知条件と副作用抑止に限定される。

---

## 4. バックテスト重要観点(時系列整合性・境界値)

`backtest/warmup.py::split_warmup_and_validation()` は `validation_end` 指定時に3区間を明確に分離する。

- **warm-up**: `index < validation_start`(indicator計算のseed専用。結果集計に出現しない)
- **validation**: `validation_start <= index <= validation_end`(結果集計対象)
- **future(未来足)**: `index > validation_end`(warm-up にも validation にも**含めない**)

固定すべき性質:

| 観点 | 期待 | 対応pytest |
|---|---|---|
| warm-up / validation境界 | validation_start を境に厳密分離、warm-up足は結果に出ない | `test_warmup.py::test_split_warmup_and_validation_splits_at_the_given_start`, `::test_sufficient_warmup_only_validation_bars_appear_in_results` |
| validation_end境界 | end を含む(inclusive)。end超の足は両区間に入らない | `test_warmup.py::test_split_warmup_and_validation_respects_validation_end`, `::test_split_warmup_and_validation_excludes_future_rows_from_both_frames` |
| future leak禁止(warm-up水増し) | end超の足数が warm-up 本数に加算されない → 本来のwarm-up不足を隠さない | `test_warmup.py::test_split_warmup_and_validation_future_rows_do_not_inflate_warmup_frame_size`, `::test_evaluate_backtest_bars_future_rows_do_not_hide_warmup_insufficient`, `test_runner.py::test_run_backtest_excludes_candles_after_to` |
| future leak禁止(同足Exit) | signal発生足自身の high/low で自分のExitを判定しない(次足以降のみ) | `test_runner.py::test_signal_own_bar_high_low_never_decides_its_own_exit` |
| future leak禁止(Market Bias) | 評価時刻より後のbiasを参照しない | `test_runner.py::test_run_backtest_never_leaks_a_future_market_bias_record` |
| データ不足時のskip | warm-up不足はエラーではなく期待されたskip(全validation足がskip、signal 0件、exit 0) | `test_warmup.py::test_warmup_insufficient_skips_signal_generation_for_every_validation_bar`, `test_runner.py::test_run_backtest_flags_insufficient_warmup_and_generates_no_signals` |
| 空データ / 期間該当なし | candlesなし・validation該当足なしは exit code 3(EXIT_NO_DATA) | `test_runner.py::test_run_backtest_returns_no_data_when_symbol_has_no_candles`, `::test_run_backtest_returns_no_data_when_validation_window_has_no_candles` |
| 不正期間 / 不正引数 | `--to < --from`・不正 `--warmup-bars`/`--confidence-threshold`/`--exit-policy` は exit code 2(EXIT_USAGE_ERROR) | `test_runner.py::test_run_backtest_returns_usage_error_when_to_before_from`, `::test_parse_args_rejects_non_positive_warmup_bars`, `::test_parse_args_rejects_out_of_range_confidence_threshold` |
| 欠損指標 | RSI/MACD/BB/MA いずれか未準備の足はreason付きでskip | `test_warmup.py::test_readiness_*` |
| Signal 0件は正常系 | biasがNeutral / confidence閾値未満でも例外を出さず完走 | `test_backtest_engine.py::test_bull_bias_below_the_confidence_threshold_generates_no_signals`, `test_warmup.py::test_neutral_bias_produces_no_candidates_even_when_fully_ready` |

エンジン主要経路(手書きOHLCV fixture)は `test_backtest_engine.py` が固定:Signalあり / Signalなし / Exit到達(win・loss) / Invalidation到達。

---

## 5. dry-run 無副作用

方針は [ルートREADME](../../README.md#dry-run-方針) / [Operations](../operations/operations.md) に準拠。`--dry-run`(または `dry_run=True`)は**以下を一切発生させない**確認モード:

1. Discord送信
2. state ファイル更新(例: posted_ids、touch zone state)
3. DB作成・更新
4. S3アップロード / presigned URL生成

新機能追加時も、これらが発生しないことをテストで確認する(既存方針の維持)。

| 観点 | 期待 | 対応pytest |
|---|---|---|
| Discord送信なし | dry-runでHTTP/セッションを呼ばない | `market-watch/tests/test_macd_alert_bot.py::test_dry_run_does_not_call_discord_http`, `news-feed-bot/tests/test_news_feed_bot.py::test_dry_run_prints_without_sending_to_discord` |
| DB作成・更新なし | dry-runでSQLiteを作らない/更新しない | `paper-signal/tests/test_signal_generator.py::test_run_once_dry_run_does_not_create_db_or_call_discord_http`, `market-watch/tests/test_chart_snapshot.py::test_main_dry_run_skip_fetch_does_not_create_missing_sqlite` |
| state更新なし | dry-runでstateファイルを書き換えない | `market-watch/tests/test_{ma,rsi,bollinger}_alert_bot.py::test_run_once_dry_run_does_not_save_state`, `news-feed-bot/tests/test_news_feed_bot.py::test_dry_run_does_not_update_posted_ids_json` |
| S3/presigned生成なし | dry-runでupload/presignedを呼ばない、snapshot_id/URLはNone | `market-watch/tests/test_chart_alert_context.py::test_prepare_alert_image_dry_run_has_no_side_effects` |
| Market Bias永続化なし | dry-runでbiasを保存しない | `news-feed-bot/tests/test_market_snapshot.py::test_persist_market_bias_is_a_noop_in_dry_run` |

---

## 6. DB・Discord・S3 の副作用観点

| 観点 | 期待 | 対応pytest |
|---|---|---|
| 一時DBのみ使用 | テストは `tmp_path` を使い、実 `data/*.sqlite3` を作らない/触らない | Backtest系全般(`test_backtest_store.py`, `test_runner.py`, `test_backtest_engine.py`) |
| run/signal DB分離 | Backtest DB は paper_signals とは別ファイル | `test_backtest_store.py::test_backtest_db_is_a_separate_file_from_paper_signals` |
| 冪等な初期化 | `init_db()` 反復呼び出しが安全、不完全schemaは明確なエラー | `test_backtest_store.py::test_init_db_is_idempotent`, `::test_init_db_raises_clear_error_for_incomplete_existing_schema` |
| 重複ID防止 | run/signalの重複idは IntegrityError、上書きしない | `test_backtest_store.py::test_save_backtest_run_with_duplicate_id_raises_integrity_error` |
| FK整合 | 未知run_idのsignal保存を拒否 | `test_backtest_store.py::test_foreign_key_constraint_rejects_signal_for_unknown_run` |
| 読み取り時の非作成 | 存在しないDBの読み取りは None/[] を返しファイルを作らない | `test_backtest_store.py::test_get_backtest_run_returns_none_when_db_file_missing` |
| Market Bias時点参照 | 評価時刻以前の直近のみ返す(未来リーク禁止・DB無ければNone) | `news-feed-bot/tests/test_market_bias_store.py::test_get_market_bias_as_of_never_leaks_a_future_record`, `::test_get_market_bias_as_of_returns_none_when_db_file_missing` |
| Paper Signal重複窓 | 同一symbol/timeframe/side の一定時間内重複を抑止 | `paper-signal/tests/test_paper_signal_store.py::test_has_recent_signal_true_within_window` |
| Discordは注入セッション | 実webhook無しでも注入セッションで検証、webhook未設定はスキップ/検証 | `news-feed-bot/tests/test_news_feed_bot.py::test_non_dry_run_uses_injected_session_without_real_webhook`, `market-watch/tests/test_macd_alert_bot.py::test_validate_config_requires_webhook_unless_dry_run` |
| S3 private key運用 | 直リンクを持たずprivate keyでupload→presignedする | `market-watch/tests/test_chart_renderer_storage.py::test_upload_and_presign_use_private_s3_key_without_direct_url` |
| Backtestは非通知 | Backtest Runnerは発注/Discord/外部APIを呼ばない | `backtest/tests/test_runner.py::test_run_backtest_never_places_an_order_or_calls_discord`, `test_backtest_engine.py`(autouseネットワーク遮断) |

---

## 7. 重要テストケース一覧

ID = 分類プレフィックス + 連番。**代表ケースのみ**(全網羅ではない)。「対応pytest」は既存テスト、空欄は§8参照。

分類: `BT`=Backtest時系列/境界、`EX`=Exit/Invalidation、`DR`=dry-run無副作用、`SE`=副作用/DB/通知、`CLI`=CLI引数/終了コード、`SUM`=Summary集計。

| ID | 前提条件 | 入力 / 操作 | 期待結果 | 対応pytest |
|---|---|---|---|---|
| BT-01 | 十分なwarm-up + validation足 | `split_warmup_and_validation(start)` | warm-up足は結果に出ず、validation足のみ集計 | `test_warmup.py::test_sufficient_warmup_only_validation_bars_appear_in_results` |
| BT-02 | validation_end指定、end超の足あり | `split_warmup_and_validation(start,end)` | end超足は warm-up/validation どちらにも入らない | `test_warmup.py::test_split_warmup_and_validation_excludes_future_rows_from_both_frames` |
| BT-03 | warm-up不足 + 大量のend超足 | `evaluate_backtest_bars(...)` | 未来足がwarm-up不足を隠さず、全validation足がskip | `test_warmup.py::test_evaluate_backtest_bars_future_rows_do_not_hide_warmup_insufficient` |
| BT-04 | warm-up本数 < 要求本数 | `run_backtest(...)` | エラーでなくskip、signal 0件、exit code 0 | `test_runner.py::test_run_backtest_flags_insufficient_warmup_and_generates_no_signals` |
| BT-05 | validation期間にcandleあり、end超candleも保存済 | `run_backtest(--to=end)` | end超candleを読み込み対象から除外 | `test_runner.py::test_run_backtest_excludes_candles_after_to` |
| BT-06 | signal発生足のみ(次足なし) | `run_backtest(...)` | 自足high/lowで自分をExit判定しない → open/no_close | `test_runner.py::test_signal_own_bar_high_low_never_decides_its_own_exit` |
| BT-07 | 評価時刻より後にのみbiasレコード | `run_backtest(...)` | 未来biasを参照しない(Neutralフォールバック) | `test_runner.py::test_run_backtest_never_leaks_a_future_market_bias_record` |
| EX-01 | long signal、次足highがTP到達 | fixed_rr で評価 | `win` / `take_profit`、pnl>0、closed_at=次足 | `test_backtest_engine.py::test_exit_reached_at_the_take_profit_records_a_win` |
| EX-02 | long signal、次足lowがstop到達 | fixed_rr で評価 | `loss` / `stop_loss`、pnl<0 | `test_backtest_engine.py::test_exit_reached_at_the_stop_records_a_loss` |
| EX-03 | long signal、MAを下抜けるclose | ma_break_invalidation で評価 | `invalidated` / `ma_break` | `test_backtest_engine.py::test_invalidation_reached_when_price_closes_below_the_ma` |
| EX-04 | candlesが空/None | `evaluate_exit(...)` | 例外を出さず open / no_close | `paper-signal/tests/test_exit_policy.py::test_empty_candles_returns_open_no_close_without_raising` |
| EX-05 | TPとstopが同一足で両方到達 | `same_candle_priority` 設定 | 設定通りの優先(stop_loss / take_profit)で決着 | `test_exit_policy.py::test_same_candle_priority_stop_loss_wins_long` ほか |
| CLI-01 | — | 必須引数欠落で `run` | usageエラー(exit code 2) | `test_runner.py::test_parse_args_requires_symbol_timeframe_from_to` |
| CLI-02 | — | `--to < --from` | exit code 2 | `test_runner.py::test_run_backtest_returns_usage_error_when_to_before_from` |
| CLI-03 | candle DBに該当symbolなし | `run` | exit code 3(EXIT_NO_DATA) | `test_runner.py::test_run_backtest_returns_no_data_when_symbol_has_no_candles` |
| CLI-04 | — | `--exit-policy` に無効/無効化済みポリシー | 明確なエラーで拒否(exit code 2) | `test_runner.py::test_run_backtest_rejects_a_valid_but_disabled_exit_policy_with_a_clean_usage_error` |
| SUM-01 | win/loss/pnl=0 混在のsignals | `build_summary(run_id, signals)` | win/loss/win_rate/total_pnl/profit_factor/max_drawdown が定義通り | `test_summary.py::test_normal_case_wins_losses_and_zero_pnl` |
| SUM-02 | signal 0件 | `build_summary(run_id, [])` | 例外なし、各指標が0/None、profit_factor/NaN/Infを出さない | `test_summary.py::test_zero_signals_matches_issue_23_example`, `::test_json_never_contains_nan_or_infinity_for_wins_only` |
| SUM-03 | 別run_idのsignalが混入 | `build_summary(run_id, signals)` | ValueErrorで拒否(誤集計しない) | `test_summary.py::test_build_summary_raises_value_error_when_a_different_run_id_is_mixed_in` |
| DR-01 | Alert Bot、dry-run | `run_once(dry_run=True)` | Discord HTTPを呼ばない | `test_macd_alert_bot.py::test_dry_run_does_not_call_discord_http` |
| DR-02 | Alert Bot、dry-run | `run_once(dry_run=True)` | stateファイルを更新しない | `test_ma_alert_bot.py::test_run_once_dry_run_does_not_save_state` |
| DR-03 | Chart Snapshot、dry-run + skip-fetch、DB無し | `main(--once --dry-run --skip-fetch)` | 欠損SQLiteを作らない | `test_chart_snapshot.py::test_main_dry_run_skip_fetch_does_not_create_missing_sqlite` |
| DR-04 | Alert画像、dry-run | `prepare_alert_image(dry_run=True)` | S3 upload/presignedを呼ばず、id/URLはNone、DB無し | `test_chart_alert_context.py::test_prepare_alert_image_dry_run_has_no_side_effects` |
| DR-05 | Paper Signal、dry-run | `run_once(dry_run=True)` | DB作成もDiscord HTTPもしない | `test_signal_generator.py::test_run_once_dry_run_does_not_create_db_or_call_discord_http` |
| SE-01 | 既存run_id | signal保存 | Backtest DBは paper_signals と別ファイル | `test_backtest_store.py::test_backtest_db_is_a_separate_file_from_paper_signals` |
| SE-02 | 未知run_id | signal保存 | FK違反で拒否 | `test_backtest_store.py::test_foreign_key_constraint_rejects_signal_for_unknown_run` |
| SE-03 | 評価時刻とその前後にbias | `get_market_bias_as_of(t)` | t以前の直近のみ、未来リークなし | `test_market_bias_store.py::test_get_market_bias_as_of_never_leaks_a_future_record` |
| SE-04 | 同一symbol/timeframe/sideの直近signal | `has_recent_signal(...)` | 窓内は重複と判定 | `test_paper_signal_store.py::test_has_recent_signal_true_within_window` |

---

## 8. 未実装 / 不足している重要テスト

現時点でコード上に**確認できなかった**、または**明示的なテストが無い**観点。CI(#32)導入前に別Issue化を推奨。

| # | 不足観点 | 現状 | 推奨対応 |
|---|---|---|---|
| G-1 | 4スイート横断の一括実行 | ルートに pytest 設定/`conftest.py` が無く、スイートは個別実行のみ | #32 で4スイート実行をCIに固定(またはルート設定追加を別Issue化) |
| G-2 | candleに時間ギャップ/欠損行がある場合のBacktest挙動 | 欠損足を含む入力の明示テストは未確認 | Backtestに時間ギャップfixtureの回帰を追加(別Issue候補) |
| G-3 | 重複timestamp / 非単調index candle | 明示テスト未確認 | 入力健全性チェックの有無を確認し、必要ならテスト追加 |
| G-4 | Backtest全体でのDiscord/S3非到達の集約保証 | `test_backtest_engine.py` はautouseでネットワーク遮断するが、他Backtestテストは個別 | 必要なら共通 `conftest.py` でネットワーク遮断を全Backtestに拡張(別Issue) |
| G-5 | S3 presigned URLの失敗系(認証エラー等) | 正常系中心。失敗時の副作用抑止は未確認 | uploader失敗時にDB/stateを汚さないことのテスト検討 |

上記は本Issueでコード追加せず、観点として記録するに留める(Issue #34 の指示に従い、実装コードは必要以上に変更しない)。

---

## 9. 矛盾・不明点

- **docs配置ガイドラインとの差異**: [docs/README.md](../README.md) は「`docs/` 直下には原則としてこの `README.md` だけを置く」としており、`docs/backtest.md` は既にこの原則の例外になっている。本書は原則に沿って新カテゴリ `docs/testing/` に配置し、`docs/README.md` の Structure/Guidelines を更新した。
- **テスト件数**は `def test_` の実数カウントであり、parametrize による実行ケース数とは一致しない(実行ケースはこれより多い)。
- **G-2/G-3(欠損・非単調candle)**はコードを確認した範囲で明示的な入力健全性チェック・テストを発見できなかった。「テストが無い」ことは「保証されていない」を意味し、「壊れる」を意味しない。実挙動は未検証のため断定しない。
- 本書のテスト名・件数は作成時点のもの。テスト追加/リネーム時は本書の対応表も更新する(維持コストを抑えるため、代表ケースのみを追う)。
