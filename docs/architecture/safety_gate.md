# Safety Gate / Live Order Submission(Issue #75)

将来の **Live Execution** に向けた、Broker 送信直前の fail-closed 検証層と単一送信サービス境界の仕様。実装は `execution/safety_gate.py`(判定)、`execution/safety_store.py`(判定監査保存)、`execution/live_submission.py`(唯一の公認送信経路)。

> **重要な位置づけ**
>
> - Safety Gate / 送信サービスは**将来の Live Execution 送信経路を安全にする部品のみ**を実装する。
> - **Live 接続・実発注・Live Execution runner は引き続き未実装**である。OANDA **Demo** Adapter は Issue #79 で `broker/` に実装済みだが `BROKER_TYPE_BY_MODE` に `demo` は未登録のため、現在構築可能な Broker **接続**は FakeBroker(`fake` mode)だけであり(下記「型付き接続 identity」)、実発注経路は存在しない。
> - **Paper Execution とは無関係・独立**である。Paper Execution(`paper-execution/`)は Broker 境界に触れない仮想執行レイヤーであり、Safety Gate を通らないし通る必要もない。
> - Safety Gate は **Strategy からも Broker Adapter からも独立**する: Strategy は安全ポリシーを持たず、Broker 実装は Gate のロジックを持たない。依存は `execution → broker`(境界モデルのみ)の一方向。

## 単一サービス境界と送信順序

正式な Live Execution 送信経路は **`LiveOrderSubmissionService`(`execution/live_submission.py`)ただ 1 つ**に閉じる。raw Broker Adapter は型付き `BrokerConnection` としてサービスへ**組み立て時に注入**され、サービスの外へは公開されない。

```text
service.register(request, client_order_id=...)   … 意図を execution store へ永続化(phase=created)。
    |                                              client_order_id はここで 1:1 に固定(Issue #74)
    v
service.submit(execution_request_id, market=...)
    1. execution store から意図・固定 client_order_id・phase を復元
       (未登録 → intent_not_registered / store 失敗 → state_query_failed / phase≠created → duplicate_submission)
    2. SafetyGate.evaluate()    … 全条件を検査し SafetyDecision(全違反コード)を返す
    3. 判定を SQLite へ耐久保存し、read-back で保存内容を検証
       (保存・read-back の失敗 → decision_persist_failed。allow は拒否へ転じる)
    4. Submission Journal へ送信試行を耐久記録(Issue #93。失敗 → submission_journal_failed /
       既存 entry との衝突 → duplicate_submission。いずれも Broker は呼ばれない)
    5. execution store の created → submitting 遷移を獲得
       (BEGIN IMMEDIATE で直列化。敗者 → duplicate_submission)
    6. Journal を started へ更新(Broker 呼び出し直前の最後の書き込み。
       失敗 → submission_journal_failed で拒否 = Broker は呼ばれない)
    7. 遷移に成功した呼び出しだけが to_order_request() → Broker.submit_order() を実行
    8. 結果を Journal と execution store へ記録
       (OrderResult → Journal 応答記録(best-effort)+ record_order_result / submit の例外 →
        Journal unknown 記録(best-effort)+ UNKNOWN を記録して re-raise / 受領済み OrderResult の
        保存失敗 → Journal へ reconciliation_required を耐久記録 + state-only UNKNOWN へ
        フォールバック記録して re-raise / 保存成功 → Journal 完了更新(best-effort))
```

- **「呼び出し側が気をつける」設計にはしない**: リポジトリ全体の AST 走査テスト(`execution/tests/test_submission_boundary.py`)が、`submit_order` / `to_order_request` の呼び出しを公認箇所(broker 自身のテストと `live_submission.py` の各 1 箇所)以外に存在させないことを回帰として固定する。

### 迂回防止境界の意味(誤用防止であり、故意回避の防止ではない)

この境界(単一サービス+raw Broker の非公開+AST 回帰テスト)が保証するのは **accidental misuse(誤用)の構造的防止**である: リポジトリ内に Gate を通らない送信コードが追加されれば CI が失敗し、公認経路の外に `submit_order` / `to_order_request` の呼び出し箇所は存在できない。

一方、**deliberate circumvention(故意の回避)— 例えば `getattr` による動的呼び出し・monkeypatch・reflection — を in-process の Python で防止することは言語的に不可能**である。これは本実装固有の限界ではなく、いかなる実行時 capability 方式・封印方式を導入しても、同じ reflection でその機構自体を偽装できる(無限後退)。また、Adapter 側に承認状態の検査を持たせる方式は「Broker Adapter は Safety Gate のロジックを持たない」(Issue #75)および Broker 境界の責務分離(Issue #73)と矛盾するため採らない。

故意回避に対する実効的な境界は、実 Adapter 導入時に**プロセス・運用境界**として設計する(Broker 認証情報を Live Execution runner プロセス以外へ渡さない、Adapter の構築を runner の composition root に限定する、コードレビュー + CI)。これは Live Adapter / runner 設計 Issue(非スコープ)の一部として後続 Issue で扱う。
- **二重送信防止**: `submit()` は `client_order_id` を受け取らない。使えるのは register 時に固定された保存済みの値だけであり(NOT NULL + UNIQUE + 不変、Issue #74)、同じ意図が新しい broker キーで再送されることは構造的に起きない。送信試行自体も `created → submitting` 遷移(一度きり・`BEGIN IMMEDIATE` 直列化)を獲得した呼び出しに限られる。
- **UNKNOWN の再送禁止**: timeout・通信失敗を含む submit の例外は UNKNOWN として記録され(「未発注」へ降格しない)、以後の `submit()` は phase≠created により `duplicate_submission` で拒否される。UNKNOWN の解決は後続照会の記録のみ(Issue #74)。
- **Broker 呼び出し後の失敗は拒否へ変換しない**: 注文が存在しうるため、例外はそのまま伝播する。**受領済み `OrderResult` の保存(`record_order_result`)が失敗した場合も、state-only UNKNOWN へのフォールバック記録を試みてから元例外を re-raise する** — Broker に存在する注文が永続化エラーの陰に消えることはない。フォールバックまで失敗した場合(order DB 全体の障害)は critical ログを残し、注文は phase `submitting` / state `pending` のまま **`execution_store.list_unresolved_submissions()`** で照会対象として取得できる(下記)。
- **未解決 submission の照会**: `execution_store.list_unresolved_submissions()` は、`state = 'unknown'`(従来の照会対象)に加え、**`phase = 'submitting'` かつ `state = 'pending'`**(遷移獲得後に Broker 観測が一度も記録されないまま停止したケース — 送信前クラッシュ、または結果保存とUNKNOWNフォールバックの二重失敗)を返す read-only API。実注文が存在しうる submission は必ず reconciliation から見える。`created`(送信試行なし)と確定観測済み(phase `submitted`)は含まない。
- **Execution DB と独立の耐久 Submission Journal(Issue #93)**: Gate を通過した送信試行は、Broker 呼び出しの**前に**専用 SQLite ファイルの Submission Journal へ耐久記録される。Journal への保存失敗は `submission_journal_failed` で fail-closed(Broker は呼ばれない)。Broker 応答後に Execution DB 保存が失敗した場合は Journal 上に `reconciliation_required` が応答内容込みで残り、**Execution DB が schema 異常・read-only・破損・消失で全損していても** `SubmissionJournal.list_unresolved()` から未解決送信を列挙・プロセス再起動後も復元できる。Journal は execution store と独立の二重送信ガードでもある(Execution DB を再作成しても同一 intent の再送信を阻止)。詳細・クラッシュ境界表は [Submission Journal](submission_journal.md) を参照。

## execution mode と Shadow 評価(Issue #78)

execution mode の統一語彙は `ExecutionMode`(`dry-run` / `shadow` / `fake` / `demo` / `live`)であり、設定文字列は `resolve_execution_mode()` がロード時に解決する(未知 mode は明示拒否)。`demo` / `live` は引き続き**定義と validation のみ**で、Adapter 型が登録されるまで接続を構築できない。

Shadow Mode(Issue #78)では Gate を `SafetyGate.evaluate_shadow(request, market=..., account=..., connection_target=...)` で評価する。live の `evaluate()` と**同一の共有チェック実装**を通るが、Broker connection を一切受け取らない:

- ポジション / 口座上限が必要とする Broker 状態は、呼び出し側が注入する `AccountSafetySnapshot`(positions + `AccountState`)から評価する。信頼性検証(型・未来時刻・stale)は live の Broker 照会結果と同一ルールで、不正なら `broker_query_failed`(fail-closed)。
- 接続 identity 検査は「shadow 接続先が `allowed_connection_targets[SHADOW]` にあるか」に縮退し、不一致は `execution_mode_mismatch`。
- 判定は `execution_mode = "shadow"` として同じ監査 store へ保存される。

詳細は [Shadow Mode / Shadow Execution](shadow_execution.md) を参照。

## 型付き接続 identity(`BrokerConnection` — Broker 実体から導出)

execution mode / 接続先は**呼び出し側が申告する文字列ではない**。すべての Broker 実装は、自身の**構築時接続 config に由来する不変の identity**(`broker_models.BrokerConnectionIdentity`)を `connection_identity()` で自己申告する義務を負い、`BrokerConnection` は mode・接続先を **Broker インスタンスから導出**する(申告用の引数は存在せず、渡そうとすると `TypeError`):

- FakeBroker の identity は mode `"fake"` 固定 + 構築時 `connection_target`(実 Adapter の endpoint URL に相当する構築時 config)。将来の Adapter は自身の実接続設定から identity を返す(mode / target を接続 config と別に申告する経路は存在しない)。
- 導出された mode は Broker の具象型と相互検証される(`BROKER_TYPE_BY_MODE` レジストリ): `fake` は `FakeBroker`(サブクラス含む)のみ、`FakeBroker` は `fake` 以外を名乗れない。`demo` / `live` は**実 Adapter 型が登録されるまで接続オブジェクト自体を構築できない**(表現不能による fail-closed)。
- mode ごとの許可接続先(`SafetyPolicy.allowed_connection_targets`。exact・大文字小文字区別・1 接続先 1 mode)は Gate が判定し、未知の接続先・大文字小文字違い(`FAKE`)は `execution_mode_mismatch` で拒否する。
- Gate は**評価のたびに identity を Broker 実体から再導出**し、組み立て後に identity がドリフトした場合・identity メソッドが欠損 / 破損した場合も `execution_mode_mismatch` で fail-closed に拒否する。

## 入力

| 入力 | 型 | 供給者 |
|---|---|---|
| 発注意図 | `ExecutionRequest`(execution store から復元) | `register()` で永続化(Issue #74) |
| 静的ポリシー | `SafetyPolicy` | Gate 構築時に注入(`policy_id` で監査から識別可能) |
| 市場データ | `MarketSafetySnapshot` | 呼び出し側(Gate 自身は市場データ I/O を行わない) |
| Broker 照会 | `Broker.get_positions()` / `get_account_state()` | evaluate 内で同期照会 |
| BELIAL 側状態 | `SafetyStateProvider`(kill switch) | Gate 構築時に注入 |
| 現在時刻 | 注入 clock(UTC 必須) | Gate / サービス各々に注入 |
| 接続 identity | `BrokerConnection` | サービス構築時に注入(mode / target は Broker 自身の `connection_identity()` から導出。評価時にも再導出) |

## SafetyPolicy(構築時検証つき)

| フィールド | 意味 |
|---|---|
| `policy_id` | このポリシー構成の識別子。全 `SafetyDecision` に記録され、監査時に適用ポリシーを特定できる |
| `allowed_symbols` | 許可銘柄(非空・前後空白禁止) |
| `allowed_sides` | 許可方向(`broker.OrderSide`) |
| `max_order_quantity` | 1 注文の数量上限(正の有限 `Decimal`) |
| `max_positions_per_symbol` / `max_positions_account` | **件数ベース**のポジション上限(下記) |
| `max_reference_age` | 参照価格・ポジション/口座 snapshot の鮮度上限(正の `timedelta`) |
| `max_spread_by_symbol` | 銘柄別 spread 上限。**全許可銘柄をちょうど網羅**しなければ構築エラー(上限のない許可銘柄という設定ミスを排除) |
| `allowed_connection_targets` | mode → 接続先集合。**1 つの接続先は 1 つの mode にのみ**属せる |
| `require_stop_loss` | SL 必須(既定 True) |

`SafetyPolicy` は **deep immutable** である: mapping フィールドは構築時検証の後に**防御的コピーされ read-only view(`MappingProxyType`)として保持**されるため、呼び出し元が構築に使った辞書を後から変更しても、また policy 属性を直接変更しようとしても、検証済みのポリシー内容が同一 `policy_id` のまま変化することはない。

## 判定基準の明確化

### 参照価格と SL / TP の基準価格

- **LIMIT**: 基準は `requested_price`。SL / TP の方向整合は `ExecutionRequest` のコンストラクタが既に保証しており(Issue #74)、Gate は防御的再検査のみ行う。**LIMIT の SL / TP を現在価格で棄却しない**(市場から離れた指値の TP が現在価格を下回っていても正当)。
- **MARKET**: 基準は想定約定サイド — **BUY は ask、SELL は bid**(欠損時は `reference_price` へフォールバック)。
- spread は `ask - bid`(`MarketSafetySnapshot.spread`)。bid / ask いずれか欠損なら `spread_missing` で拒否。
- **鮮度の基準時刻は Gate の注入 clock**。`observed_at_utc` が clock より**未来**なら `reference_timestamp_future` で拒否(時計ずれ・破損データを「新鮮」扱いしない)。`max_reference_age` 超過は `reference_price_stale`。
- snapshot の銘柄が request と不一致なら `market_snapshot_mismatch`。
- 価格は全て正の有限 `Decimal`(NaN / Infinity / 0 / 負値は `MarketSafetySnapshot` 構築時に拒否)。時刻は tz-aware UTC のみ。

### ポジション上限(保守的・単純ルール)

- **件数上限**である(数量集計・証拠金ベースの上限は Issue #75 の非スコープ)。
- Broker が報告した全ポジションを数える: **同一 symbol の long / short は両方 1 件ずつ**数え、重複エントリの名寄せはしない。
- **新規注文は +1 として数える**: 既存件数が上限に達していれば拒否(`既存件数 >= 上限` で拒否)。上限 0 は「新規ポジション全面禁止」。
- ポジション情報は evaluate 内で同期照会する。エントリが `PositionInfo` でない・snapshot 時刻(`retrieved_at_utc`)が未来・`max_reference_age` より古い場合は、その回答全体を信頼せず `broker_query_failed` で拒否する(fail-closed)。
- **AccountState も同じ鮮度基準**で検証する: 型不正・`retrieved_at_utc` が未来・stale は `broker_query_failed`。
- **部分約定中(working)の注文は数えない**: Broker がポジションとして報告するものだけが対象(Issue #75 では非スコープと明記)。

### kill switch

`SafetyStateProvider.is_kill_switch_enabled()` が True なら、**他条件に関係なく**すべての新規注文を拒否する(`kill_switch_enabled`)。照会が例外を出した場合、または **bool 以外を返した場合**(falsy なゴミを「無効」と読まない)は `state_query_failed` で拒否する。

## fail-closed 条件(すべて拒否になる)

| 異常 | 拒否コード |
|---|---|
| execution store の意図照会失敗・`created → submitting` 遷移のインフラ失敗 | `state_query_failed` |
| 未登録の意図(`register()` 未実行) | `intent_not_registered` |
| 送信試行が既に存在(二重送信・UNKNOWN 後・送信済み) | `duplicate_submission` |
| Broker positions / account 照会の例外・型不正・snapshot 鮮度異常(未来・stale) | `broker_query_failed` |
| kill switch 照会の例外・bool 以外の戻り値 | `state_query_failed` |
| 判定の耐久保存失敗・**read-back 検証失敗**(store 未初期化・schema 不正・書き込み消失) | `decision_persist_failed`(元の拒否コードに追記。allow は拒否へ転じる) |
| Submission Journal への送信試行の耐久保存失敗(Journal 未初期化・schema 不正・書き込み失敗。Issue #93) | `submission_journal_failed`(Journal 上の既存 entry との衝突は `duplicate_submission`) |
| 準備段階の想定外例外(引数型不正・clock 破損を含む) | `gate_internal_error`(サービスが変換・記録) |
| 参照価格の欠損・stale・未来時刻 | `reference_price_missing` / `reference_price_stale` / `reference_timestamp_future` |
| 接続先が mode の許可集合にない・identity 不整合 | `execution_mode_mismatch` |

例外の型・失敗原因を問わず、**Broker 呼び出し前の失敗から送信へ進む経路は存在しない**。違反が 1 件でもあれば `Broker.submit_order()` は呼ばれない(全違反を収集してから単一の `SafetyDecision` で返す)。

## 拒否コード一覧(機械可読が正)

`symbol_not_allowed` / `side_not_allowed` / `quantity_limit_exceeded` / `symbol_position_limit_exceeded` / `account_position_limit_exceeded` / `stop_loss_required` / `stop_loss_invalid` / `take_profit_invalid` / `market_snapshot_mismatch` / `reference_price_missing` / `reference_price_stale` / `reference_timestamp_future` / `spread_missing` / `spread_limit_exceeded` / `market_closed` / `symbol_not_tradable` / `kill_switch_enabled` / `broker_query_failed` / `state_query_failed` / `decision_persist_failed` / `execution_mode_mismatch` / `gate_internal_error` / `intent_not_registered` / `duplicate_submission` / `submission_journal_failed`

- 拒否理由の正は `SafetyRejectionCode`(enum 値)であり、文字列ログは補助である。ログには拒否コード・`execution_request_id`・`policy_id` を出す。**重複抑止(`duplicate_submission`)時はこれに加えて source `signal_id`・現在の `submission_phase` / `order_state`・抑止理由を WARN で必ず出力する**(Issue #76。store 側の作成時 conflict も同じ項目をログする)。secret・token・口座残高等は出さない。
- 1 判定内の拒否コードは**順序保持 dedup**される(最初の出現が勝つ)。`SafetyDecision` はコードの一意性を構築時不変条件として強制する。

## SafetyDecision(判定結果)と監査保存

`SafetyDecision` は構築時検証つき(allow ⇔ 拒否 0 件、コード一意、UTC、非空 ID)で、以下を保持する:

- `execution_request_id` / `allowed` / `checked_at_utc`(判定時刻 = Gate clock)
- `policy_id`(適用ポリシーの識別)
- `rejections`(コード + 詳細メッセージのタプル。コードは一意)
- `execution_mode` / `connection_target`(型付き接続 identity の値)
- `account_id` / `position_count`(Broker 照会結果。照会失敗なら None)

`SQLiteSafetyDecisionStore`(`execution/data/safety_gate_decisions.sqlite3` 既定、`SAFETY_GATE_DECISION_DB_PATH` で上書き)が監査保存を担う。**`execution_orders.sqlite3` とは別ファイル**である(execution store の closed-world schema 検証は他テーブルの同居を拒否するため)。方針は execution store と同一:

- `initialize()` は **pristine な DB のみ**へ schema を新規作成(オブジェクト 0 件 + `user_version` 0)。それ以外は完全検証し、不一致なら **DB 無変更で** `SafetySchemaError`。
- 全 read / write が「open → **closed-world schema 検証**(sqlite_master 全 object 集合 + token 正規化 DDL + PRAGMA 構造 signature + version)→ トランザクション」で動作。trigger / view / 制約欠損 lookalike / 他コンポーネント DB は黙って受け入れない。
- `record()` は**あらゆる失敗で例外**を送出する(DB ファイル欠損 = 未初期化も失敗であり、暗黙にファイルを作らない)。
- **監査 store は送信サービス内部に封印される**: `LiveOrderSubmissionService` のコンストラクタは **filesystem パス(`decision_db_path`)しか受け取らず**、具体実装 `SQLiteSafetyDecisionStore` をサービス内部で生成する。recorder オブジェクトの注入点自体が存在しないため、`record()` / read-back を override して永続化を偽装できる注入経路はない(InMemory / NoOp recorder は構造的に排除)。さらに record 後に **read-back で保存内容を検証**し、保存が黙って失われた場合も送信は行われない(`decision_persist_failed`)。read-back は**自分の判定が再読で見つかること(membership)**を検証する(PR #101): 同一 intent の並行敗者はそれぞれ自分の拒否判定を記録するため、record と read-back の間に他の呼び出しの判定が割り込み得る。「最新行であること」は契約に含まれない(旧実装はこれを要求しており、並行時に誤って `decision_persist_failed` が付く flake の原因だった)。
- 読み取りは missing DB を**作成しない**(空タプル)。保存行は厳格に再検証(拒否 JSON のキー集合完全一致・既知コードのみ・UTC・allow/拒否整合)し、帯域外編集は `SafetyIntegrityError` で拒否する。DB CHECK 制約(allow ⇔ `rejections_json = '[]'`、UTC サフィックス等)を二重の防御とする。
- 接続は常に明示 close(Windows のファイルハンドル衛生。Issue #83 r2)。

保存順序は **record + read-back → created→submitting 遷移 → submit**: 許可判定の保存が検証されてから遷移を獲得し、遷移に勝った呼び出しだけが Broker へ送信する。送信自体の結果(`OrderResult` / UNKNOWN)は execution store(Issue #74)へ記録される。

## テスト

`python -m pytest execution/tests -q`(`tmp_path` の SQLite と FakeBroker のみ。外部通信・Discord・S3・実注文なし)。

- `test_safety_gate.py` — 正常許可(FakeBroker へ 1 件送信・監査記録・execution store の phase/state 記録)、固定 client_order_id の使用、LIMIT の SL / TP が現在価格でなく `requested_price` 基準であること、**二重送信防止**(2 回目は `duplicate_submission`・Broker 送信 1 件のまま)、**UNKNOWN 再送禁止**(timeout / 通信失敗後の再 submit 拒否・UNKNOWN 記録)、**created→submitting 勝者のみ送信**(先行遷移・遷移インフラ失敗の fail-closed)、Broker 拒否は結果として記録、**受領済み結果の保存失敗 → UNKNOWN フォールバック + `list_unresolved_submissions()` での可視性**(二重失敗時の submitting/pending 取得・送信前クラッシュ相当の stuck pending 取得を含む)、未登録意図の拒否、破損 order store の fail-closed、**封印された監査 store**(オブジェクト注入の構築時拒否・record 消失の read-back 検知・未初期化 store の送信阻止・元コード保持)、各条件の独立拒否(銘柄 / 方向 / 数量 / SL 無し / SL・TP 不正(BUY / SELL)/ snapshot 銘柄不一致 / 参照価格欠損・stale・未来 / spread 欠損・超過 / market closed / not tradable / 接続先不一致・未知・大文字小文字)、**Broker 実体から導出する型付き接続 identity**(caller 申告の構造的不能(`TypeError`)・FakeBroker は live / demo を名乗れない・非 FakeBroker は fake を名乗れない・未知 mode・identity 欠損 / 型不正の構築時拒否・**組み立て後の identity ドリフトを evaluate の再導出が拒否**)、kill switch(全条件正常でも拒否・照会例外・bool 以外)、ポジション上限(long + short 合算・口座全体・上限未満の許可・stale / 未来 snapshot の fail-closed)、Broker 照会失敗(positions / account / 型不正)、**AccountState の stale / 未来 / 型不正の fail-closed**、想定外例外・clock 破損の `gate_internal_error`、複数違反の同時収集と**順序保持 dedup**(dedup 関数・`SafetyDecision` の一意性不変条件・evaluate 経由の重複排除)、**全拒否経路で `submitted_requests` が 0 件(または増加 0)**、ログへの機械可読コード出力、`SafetyPolicy` / `MarketSafetySnapshot` / `SafetyDecision` の構築時検証と **policy の deep immutability**(呼び出し元 dict の事後変更が判定に影響しない・read-only view への書き込み拒否)、**Issue #76 idempotency**(同一 signal の 2 重 register 拒否・ACCEPTED / SUBMITTING / UNKNOWN の再処理非送信・**サービス再作成(プロセス再起動相当)後の重複抑止**・**独立 store 接続を持つ 2 サービスの同時 submit で Broker 送信 1 件**・重複抑止ログの execution_request_id / signal_id / 状態 / 理由)。
- `test_safety_store.py` — 再オープン(プロセス再起動相当)後の round-trip、記録順の復元、missing DB への書き込み拒否(ファイル非作成)・読み取り空(ファイル非作成)、pristine 判定、foreign schema / 制約欠損 lookalike / trigger / version 不一致の拒否(DB 無変更)、帯域外改竄(未知コード・未知キー・壊れた時刻)の復元時拒否、DB CHECK 制約の発火、公認送信サービスとの結合(拒否・許可の監査が再起動後も取得可能、未初期化 store の fail-closed)。
- `test_submission_boundary.py` — `submit_order` / `to_order_request` 呼び出し箇所のリポジトリ全体 AST 走査(サービス境界迂回の構造的禁止、`live_submission.py` 内の送信呼び出しが 1 箇所であること)。
- `test_fixed_scenarios.py` ほか(Issue #77)— 固定シナリオ試験。Safety Gate 拒否系(stale price / kill switch / execution mode 不整合 / 複数違反同時収集)を含む分岐を固定データから本番経路(本サービス)経由で再現し、全拒否シナリオで Broker 呼び出しが 0 件であること・機械可読な拒否コード・監査保存件数を expected/actual 比較で固定する。実行方法は `execution/README.md` の「固定シナリオ試験」節を参照。

## 非スコープ(Issue #75 時点で未実装)

- OANDA Adapter・OANDA Demo / Live API・実口座への注文・Live 接続(`demo` / `live` の `BrokerConnection` は Adapter 型が登録されるまで構築不能)
- Production Live Execution runner(サービスを呼ぶ本体側ユースケース・スケジューラ)
- 動的ロット計算・証拠金最適化・リスク量からの数量計算
- Strategy / Paper Signal の売買ロジック変更、Paper Execution への Broker 接続
- reconciliation の定期実行(UNKNOWN / 未解決 submission の解決は後続照会記録のみ。照会**対象の取得**は `list_unresolved_submissions()` で可能)
- 数量ベース・証拠金ベースのポジション上限、部分約定中注文のカウント
- in-process での故意回避(reflection / monkeypatch)の完全防止(言語的に不可能。上記「迂回防止境界の意味」参照。実 Adapter 導入時の認証情報分離等の運用境界として後続 Issue で設計)
- ~~order DB 全体が利用不能な場合の耐久フォールバック~~ → **Issue #93 で実装済み**: 独立した耐久 [Submission Journal](submission_journal.md) が Broker 送信前後を記録し、order DB 全損時にも未解決送信を列挙できる(Journal と Execution DB の突合の自動実行・自動修復は reconciliation 側の後続 Issue)
