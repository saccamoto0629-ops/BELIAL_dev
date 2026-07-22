# Safety Gate(Issue #75)

将来の **Live Execution** に向けた、Broker 送信直前の fail-closed 検証層の仕様。実装は `execution/safety_gate.py`(判定)と `execution/safety_store.py`(監査保存)。

> **重要な位置づけ**
>
> - Safety Gate は**将来の Live Execution 送信経路を安全にする部品のみ**を実装する。
> - **OANDA Adapter・Live 接続・実発注・Live Execution runner は引き続き未実装**である。Safety Gate が存在しても実発注経路は存在しない。
> - **Paper Execution とは無関係・独立**である。Paper Execution(`paper-execution/`)は Broker 境界に触れない仮想執行レイヤーであり、Safety Gate を通らないし通る必要もない。
> - Safety Gate は **Strategy からも Broker Adapter からも独立**する: Strategy は安全ポリシーを持たず、Broker 実装は Gate のロジックを持たない。依存は `execution → broker`(境界モデルのみ)の一方向。

## 依存方向と送信順序

```text
ExecutionRequest(execution/execution_models.py)
    ↓
SafetyGate.evaluate()            … 全条件を検査し SafetyDecision を返す(送信しない)
    ↓
SafetyDecisionRecorder.record()  … 判定を監査保存(失敗したら送信しない)
    ↓ (allowed のときのみ)
ExecutionRequest.to_order_request() → Broker.submit_order()
```

この一連の順序を実装する **`submit_after_safety_gate()` が唯一の公認送信経路**である。「呼び出し側が気をつける」設計にはしない: リポジトリ全体の AST 走査テスト(`execution/tests/test_submission_boundary.py`)が、`submit_order` / `to_order_request` の呼び出しを公認箇所(broker 自身のテストと `safety_gate.py` の 1 箇所)以外に存在させないことを回帰として固定する。Gate を通過し判定が**保存された後**でなければ、`Broker.submit_order()` に到達する経路は存在しない。

## 入力

| 入力 | 型 | 供給者 |
|---|---|---|
| 発注意図 | `ExecutionRequest` | 呼び出し側(Strategy 語彙の変換は呼び出し側の責務。Issue #74) |
| 静的ポリシー | `SafetyPolicy` | 構築時に注入(`policy_id` で監査から識別可能) |
| 市場データ | `MarketSafetySnapshot` | 呼び出し側(Gate 自身は市場データ I/O を行わない) |
| Broker 照会 | `Broker.get_positions()` / `get_account_state()` | evaluate 内で同期照会 |
| BELIAL 側状態 | `SafetyStateProvider`(kill switch) | 構築時に注入 |
| 現在時刻 | 注入 clock(UTC 必須) | 構築時に注入 |
| execution mode / 接続先 | `ExecutionMode` / `str` | 呼び出し側 |

## SafetyPolicy(構築時検証つき)

| フィールド | 意味 |
|---|---|
| `policy_id` | このポリシー構成の識別子。全 `SafetyDecision` に記録され、監査時に適用ポリシーを特定できる |
| `allowed_symbols` | 許可銘柄(非空・前後空白禁止) |
| `allowed_sides` | 許可方向(`broker.OrderSide`) |
| `max_order_quantity` | 1 注文の数量上限(正の有限 `Decimal`) |
| `max_positions_per_symbol` / `max_positions_account` | **件数ベース**のポジション上限(下記) |
| `max_reference_age` | 参照価格・ポジション snapshot の鮮度上限(正の `timedelta`) |
| `max_spread_by_symbol` | 銘柄別 spread 上限。**全許可銘柄をちょうど網羅**しなければ構築エラー(上限のない許可銘柄という設定ミスを排除) |
| `allowed_connection_targets` | mode → 接続先集合。**1 つの接続先は 1 つの mode にのみ**属せる |
| `require_stop_loss` | SL 必須(既定 True) |

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
- **部分約定中(working)の注文は数えない**: Broker がポジションとして報告するものだけが対象(Issue #75 では非スコープと明記)。

### execution mode と接続先

`ExecutionMode` は `fake` / `demo` / `live`。**`paper` という mode は意図的に存在しない**(Paper Execution は Broker 境界を使わないため)。判定は **exact・大文字小文字区別**の一致のみ:

- 未知の mode(enum 外の値)→ 拒否
- 接続先が欠損・空・空白のみ → 拒否
- mode の許可集合にない接続先(`fake` mode に `oanda-live`、`live` mode に `fake`、`demo` mode に `oanda-live` など)→ 拒否
- 大文字小文字違い(`FAKE`)・前後空白(` fake `)→ 正規化せず拒否

いずれも拒否コードは `execution_mode_mismatch`。

### kill switch

`SafetyStateProvider.is_kill_switch_enabled()` が True なら、**他条件に関係なく**すべての新規注文を拒否する(`kill_switch_enabled`)。照会が例外を出した場合、または **bool 以外を返した場合**(falsy なゴミを「無効」と読まない)は `state_query_failed` で拒否する。

## fail-closed 条件(すべて拒否になる)

| 異常 | 拒否コード |
|---|---|
| Broker positions / account 照会の例外・型不正・snapshot 鮮度異常 | `broker_query_failed` |
| kill switch 照会の例外・bool 以外の戻り値 | `state_query_failed` |
| 判定の監査保存失敗(store 未初期化・schema 不正・書き込み失敗) | `decision_persist_failed`(元の拒否コードに追記。allow は拒否へ転じる) |
| evaluate 内の想定外例外(引数型不正・clock 破損を含む) | `gate_internal_error`(`submit_after_safety_gate` が変換・記録) |
| 参照価格の欠損・stale・未来時刻 | `reference_price_missing` / `reference_price_stale` / `reference_timestamp_future` |
| mode / 接続先の欠損・未知・不一致 | `execution_mode_mismatch` |

例外の型・失敗原因を問わず、**送信へ進む経路は存在しない**。違反が 1 件でもあれば `Broker.submit_order()` は呼ばれない(全違反を収集してから単一の `SafetyDecision` で返す)。

## 拒否コード一覧(機械可読が正)

`symbol_not_allowed` / `side_not_allowed` / `quantity_limit_exceeded` / `symbol_position_limit_exceeded` / `account_position_limit_exceeded` / `stop_loss_required` / `stop_loss_invalid` / `take_profit_invalid` / `market_snapshot_mismatch` / `reference_price_missing` / `reference_price_stale` / `reference_timestamp_future` / `spread_missing` / `spread_limit_exceeded` / `market_closed` / `symbol_not_tradable` / `kill_switch_enabled` / `broker_query_failed` / `state_query_failed` / `decision_persist_failed` / `execution_mode_mismatch` / `gate_internal_error`

拒否理由の正は `SafetyRejectionCode`(enum 値)であり、文字列ログは補助である。ログには拒否コード・`execution_request_id`・`policy_id` のみを出し、secret・token・口座残高等は出さない。

## SafetyDecision(判定結果)と監査保存

`SafetyDecision` は構築時検証つき(allow ⇔ 拒否 0 件、UTC、非空 ID)で、以下を保持する:

- `execution_request_id` / `allowed` / `checked_at_utc`(判定時刻 = Gate clock)
- `policy_id`(適用ポリシーの識別)
- `rejections`(コード + 詳細メッセージのタプル)
- `execution_mode` / `connection_target`(呼び出し側の主張。主張自体が不正なら None)
- `account_id` / `position_count`(Broker 照会結果。照会失敗なら None)

`SQLiteSafetyDecisionStore`(`execution/data/safety_gate_decisions.sqlite3` 既定、`SAFETY_GATE_DECISION_DB_PATH` で上書き)が監査保存を担う。**`execution_orders.sqlite3` とは別ファイル**である(execution store の closed-world schema 検証は他テーブルの同居を拒否するため)。方針は execution store と同一:

- `initialize()` は **pristine な DB のみ**へ schema を新規作成(オブジェクト 0 件 + `user_version` 0)。それ以外は完全検証し、不一致なら **DB 無変更で** `SafetySchemaError`。
- 全 read / write が「open → **closed-world schema 検証**(sqlite_master 全 object 集合 + token 正規化 DDL + PRAGMA 構造 signature + version)→ トランザクション」で動作。trigger / view / 制約欠損 lookalike / 他コンポーネント DB は黙って受け入れない。
- `record()` は**あらゆる失敗で例外**を送出する(DB ファイル欠損 = 未初期化も失敗であり、暗黙にファイルを作らない)。`submit_after_safety_gate` はこれを `decision_persist_failed` へ変換し送信しない。
- 読み取りは missing DB を**作成しない**(空タプル)。保存行は厳格に再検証(拒否 JSON のキー集合完全一致・既知コードのみ・UTC・allow/拒否整合)し、帯域外編集は `SafetyIntegrityError` で拒否する。DB CHECK 制約(allow ⇔ `rejections_json = '[]'`、UTC サフィックス等)を二重の防御とする。
- 接続は常に明示 close(Windows のファイルハンドル衛生。Issue #83 r2)。

保存順序は **record → submit**: 許可判定が保存されてから Broker へ送信する。送信自体の結果(`OrderResult` / `BrokerError`)の追跡は execution store(Issue #74)の責務であり、Gate の監査対象外。

## テスト

`python -m pytest execution/tests -q`(`tmp_path` の SQLite と FakeBroker のみ。外部通信・Discord・S3・実注文なし)。

- `test_safety_gate.py` — 正常許可(FakeBroker へ 1 件送信・監査記録)、LIMIT の SL / TP が現在価格でなく `requested_price` 基準であること、各条件の独立拒否(銘柄 / 方向 / 数量 / SL 無し / SL・TP 不正(BUY / SELL)/ snapshot 銘柄不一致 / 参照価格欠損・stale・未来 / spread 欠損・超過 / market closed / not tradable / mode × 接続先の全誤接続パターン・未知 mode・大文字小文字・空白・空)、kill switch(全条件正常でも拒否・照会失敗・bool 以外)、ポジション上限(long + short 合算・口座全体・上限未満の許可・stale / 未来 snapshot の fail-closed)、Broker 照会失敗(positions / account / 型不正)、判定保存失敗(allow が拒否へ転じ元コードを保持)、想定外例外・clock 破損の `gate_internal_error`、複数違反の同時収集、**全拒否経路で `submitted_requests == []`**、ログへの機械可読コード出力、`SafetyPolicy` / `MarketSafetySnapshot` / `SafetyDecision` の構築時検証(NaN / Infinity / 0 / 負値 / naive datetime / spread 網羅 / 接続先の mode 重複)。
- `test_safety_store.py` — 再オープン(プロセス再起動相当)後の round-trip、記録順の復元、missing DB への書き込み拒否(ファイル非作成)・読み取り空(ファイル非作成)、pristine 判定、foreign schema / 制約欠損 lookalike / trigger / version 不一致の拒否(DB 無変更)、帯域外改竄(未知コード・未知キー・壊れた時刻)の復元時拒否、DB CHECK 制約の発火、公認送信経路との結合(拒否・許可の監査が再起動後も取得可能、未初期化 store の fail-closed)。
- `test_submission_boundary.py` — `submit_order` / `to_order_request` 呼び出し箇所のリポジトリ全体 AST 走査(Gate 迂回の構造的禁止)。

## 非スコープ(Issue #75 時点で未実装)

- OANDA Adapter・OANDA Demo / Live API・実口座への注文・Live 接続
- Production Live Execution runner(Gate を呼ぶ本体側ユースケース)
- 動的ロット計算・証拠金最適化・リスク量からの数量計算
- Strategy / Paper Signal の売買ロジック変更、Paper Execution への Broker 接続
- reconciliation の定期実行
- 数量ベース・証拠金ベースのポジション上限、部分約定中注文のカウント
