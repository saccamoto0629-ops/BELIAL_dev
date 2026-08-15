# Architecture

## 全体構成

BELIAL は以下の主要レイヤーで構成する。リアルタイムのフローは Paper Signal で止まらず、Paper Execution による仮想執行(Virtual Position → Exit Evaluation → Execution Result)まで進む。

```text
RSS News Monitor
  -> Market Snapshot

Market Watch
  -> Chart Snapshot
  -> Market Bias / Market State
  -> Event Alerts

Market Bias + Chart Snapshot
  -> Strategy Evaluation (Paper Signal Generator)
  -> Paper Signal (paper_only records / Discord notification)

Paper Signal (保存済みレコード)
  -> Paper Execution
      -> Virtual Position (pending / open / closed / expired / cancelled)
      -> Exit Evaluation (SL / TP / 最大保有時間)
      -> Execution Result (仮想損益を Position Store へ記録)
```

- **Live Execution(Broker API による実注文)は上記のリアルタイムフローに存在しない**。基盤(Broker 境界・OANDA Demo / Live Adapter・Safety Gate・送信境界・reconciliation・Kill Switch・Live 接続ロック)は `broker/` / `execution/` に実装済みで、Issue #131 で **runner core も Fake / Shadow に限って実装済み**だが、**実 Broker(OANDA Demo / Live)への runner 統合は未実装**のためリアルタイムフローとしては駆動されず、**実口座での Live 運用も未解禁**である(下記「Alert / Strategy / Signal / Execution の責務分離」)。
- 過去のローソク足上で Strategy / Execution ルールを再生・評価する **Backtest** は、このリアルタイムフローとは独立した検証基盤である(下記「Backtest と Paper Execution の役割差」)。

## コンポーネント関係

| Component | Role | Output |
|---|---|---|
| RSSニュース監視 | Google News / Reuters / Yahoo系RSSを取得し重要ニュースを通知 | Discord news alerts |
| Market Snapshot | Provider出力を統合し定時レポートを生成 | Market Snapshot message |
| Market Watch | ローソク足、SMA / EMA / Bollinger Bands / MACD / RSI14を扱う | Chart Snapshot / Event alerts |
| Market Bias | Market Snapshot と Chart Snapshot から Bull / Bear / Neutral を分類 | market_bias, confidence, reasons |
| Paper Signal | Market Bias と Chart Snapshot から paper_only 仮説を生成 | SQLite record / Discord notification |
| Paper Execution | 保存済み Paper Signal から仮想ポジションを生成し、確定足の時間進行に沿って Entry / SL / TP / 最大保有時間を判定 | Paper Position DB の仮想ポジション・仮想損益レコード |
| Backtest | 過去のローソク足上で Strategy / Exit ルールを再生・評価 | Backtest DB(run / signal レコード)、summary |

## Alert / Strategy / Signal / Execution の責務分離

Execution は **Paper Execution**(仮想約定・仮想ポジション管理・Exit・損益計算を時間進行に沿って行う。**実装済み** = `paper-execution/`)と **Live Execution**(Broker API による実注文・実約定。**基盤 + runner core 実装済み(Fake / Shadow / Demo)・実口座運用は未解禁**)に分かれる。Paper Execution は仮想レコードのみを生成し、Broker API 連携・実注文・自動売買は含まない(それらは Live Execution の責務)。Live Execution が使う **Broker 境界**(共通インターフェース・共通モデル・外部通信しないテストダブル FakeBroker)は Issue #73 で `broker/` に定義済みで、Issue #79 で **OANDA Demo Adapter**(Demo 口座専用。Live endpoint は fail-closed で拒否)が同境界の実装として追加され、Issue #132 で Execution 層へ正式統合された(`BROKER_TYPE_BY_MODE` への `demo` 登録。[Broker Interface](broker_interface.md))。Paper Execution はこの境界を使わない。加えて Issue #74 で、Strategy / Signal から将来の Live Execution ユースケースへ渡す **ExecutionRequest** と注文状態・状態履歴の SQLite 永続化基盤を、Issue #75 で Broker 送信直前の fail-closed **Safety Gate**(検証・判定監査部品。Strategy / Broker Adapter から独立)を `execution/` に実装した([Execution Request / Order Persistence](execution_request.md) / [Safety Gate](safety_gate.md))。さらに Issue #103 で、内部注文状態と Broker 注文状態を照合して不一致を分類・記録する **Broker reconciliation の差分検出基盤**(`execution/broker_reconciliation.py` ほか)を、Issue #104 で、その結果のうち `auto_repair_candidate` に分類された注文状態だけを既存の注文状態機械経由で内部へ冪等に反映する**安全な修復サービス**(`execution/broker_reconciliation_repair.py` ほか)を追加した([Broker Reconciliation](broker_reconciliation.md))。検出は**検出専用**、修復は**内部 DB のみ**を対象とする(Broker への write 操作なし・要確認対象は修復しない・dry-run は無副作用)。Issue #105 で照合範囲を **position / trade / transaction / SL・TP** へ拡張した(検出 + `manual_review` 分類のみ。自動採用・自動 close・SL / TP の自動再作成は行わず、このドメインに修復経路は存在しない)。あわせて **OANDA Demo 専用の手動 reconciliation CLI**(`python -m execution.reconciliation_runner`。reconcile-only: submit / cancel / 口座照会を構造的に封じた facade 経由・Demo 以外の endpoint は fail-closed 拒否)を追加した。scheduler への組み込みは引き続き行わない。これらも**モデル・永続化・検証基盤と手動照合経路のみ**であり、定期実行の reconciliation は引き続き未実装である。Issue #82 で **Live 接続の二重以上のロック**(`broker/oanda_live_adapter.py` + `execution/live_lock.py` + 起動前 preflight CLI `execution/live_startup_cli.py`)を追加し、`BROKER_TYPE_BY_MODE` へ `live` を登録したが、Live 接続は全ロック成立時のみ構築できる。Issue #131 で **Live Execution runner core**(`execution/live_runner.py` + `execution/runner_composition.py`)を追加し、ExecutionRequest の取得 / 対象選択・1 cycle の API・常駐ループ境界・graceful shutdown・fail-closed を実装した。続く Issue #132 で **OANDA Demo mode を Execution 層へ正式統合**し(`demo` を `BROKER_TYPE_BY_MODE` へ**具象型完全一致**で登録、唯一の接続 composer として `execution/demo_lock.py` + 起動前 preflight CLI `execution/demo_startup_cli.py` を追加、Live / Demo 共通の設定検証を `execution/safety_config.py` へ集約)、**runner が駆動できる mode は `fake` / `shadow` / `demo`** となった(`live` / `dry-run` / 未知 mode は明示拒否)。送信は従来どおり唯一の境界 `LiveOrderSubmissionService` のみを通り、`demo` でも Safety Gate・Submission Journal・注文状態機械・**永続 Kill Switch**(`REAL_BROKER_MODES` は demo / live の双方に永続 reader を要求する)を省略しない。**reconciliation の定期実行・systemd 等の継続運転導線は未実装**であり、**実口座での Live 運用は未解禁**である([運用ドキュメント](../operations/operations.md) の「Live 運用開始の事前条件」)。

| Layer | Responsibility | Status |
|---|---|---|
| Alert | 事実を検知して通知する。売買判断はしない | 実装済み(MACD cross, MA touch, Bollinger upper_break, RSI14 overbought/oversold) |
| Strategy / Method | 観測・分類情報をどう解釈し、どの条件でエントリー候補とするかのルール | 実装済み(Paper Signal のルールベース候補生成、Backtest の外部 Strategy 定義(YAML)) |
| Signal (Paper Signal) | Strategy 評価の結果を paper_only 仮説として記録・通知する。entry / exit は決めない | 実装済み(`paper-signal/`) |
| Paper Execution | Paper Signal を入力に、仮想Entry・仮想ポジション管理・SL / TP / Exit 判定・仮想損益計算を行う | **実装済み**(`paper-execution/`)。設計 [Paper Execution](paper_execution.md)(Issue #49)、実装 Issue #50 / #51。Broker API・実注文は含まない |
| Live Execution | Broker API による実際の注文・約定・ポジション管理 | **基盤 + runner core 実装済み(Fake / Shadow / Demo)・実口座運用は未解禁**。Broker 境界と OANDA Demo / Live Adapter は `broker/`([Broker Interface](broker_interface.md)、Issue #73 / #79 / #82)、ExecutionRequest と注文状態・再発行チェーンの永続化([Execution Request / Order Persistence](execution_request.md)、Issue #74 / #95 / #115)、発注前 Safety Gate + 判定監査保存 + 唯一の送信・キャンセル境界([Safety Gate](safety_gate.md)、Issue #75 / #99)、Shadow Mode([Shadow Mode](shadow_execution.md)、Issue #78)、Kill Switch(Issue #81)、Submission / Cancellation Journal([Submission Journal](submission_journal.md)、Issue #93 / #99)、Broker reconciliation([Broker Reconciliation](broker_reconciliation.md)、Issue #103〜#105)、再発行系安全機構([Execution Intent Reissue](execution_reissue.md)、Issue #95 / #102 / #114〜#117)、Live 接続の二重以上のロック(Issue #82)、Live Execution runner core(Issue #131)、OANDA Demo mode の Execution 統合と demo 活性化ロック(Issue #132。runner は `fake` / `shadow` / `demo` を駆動でき、`live` は fail-closed で拒否する)は `execution/` に実装済み。**継続運転する常駐プロセスは存在せず**(reconciliation の定期実行・systemd 等の運用導線は未実装)、Live 接続は全ロック成立時のみ構築できる |

この分離は、[Concept](../product/concept.md) のレイヤー分離(Observation / Classification / Alert / Strategy / Execution / Persistence)のうち、Alert 以降を実装観点で細分化したものである。層の対応は以下の通り。

| Concept のレイヤー | Architecture | 状態 / 現行モジュール |
|---|---|---|
| Observation | (Alert以前の入力) | 各Provider, `chart_collector.py` |
| Classification | (Alert以前の入力) | `indicators.py`, `market_state.py`, `market_bias.py` |
| Alert | Alert | `macd_alert_bot.py` / `ma_alert_bot.py` / `bollinger_alert_bot.py` / `rsi_alert_bot.py` |
| Strategy / Method | 候補生成・評価ルール | `paper-signal/signal_generator.py`、Backtest の外部定義は `backtest/strategy_definition.py`(実装済み) |
| Hypothesis / Paper Signal | Strategy / Method の評価結果を paper_only 仮説として生成・保存 | `paper-signal/signal_generator.py`, `paper_signal_store.py`(実装済み) |
| Execution | Paper Execution | 実装済み(`paper-execution/`。仮想約定のみ) |
| Execution | Live Execution | 基盤 + runner core 実装済み(`broker/` + `execution/`)。runner が駆動できるのは Fake / Shadow(Issue #131)と Demo(Issue #132)・継続運転の運用導線は未実装・実口座運用は未解禁 |
| Persistence | 各コンポーネントの store 層(下記「DB構成」「schema validation / migration」) | 実装済み |

## Paper Signal と Paper Execution の境界

- **Paper Signal は仮説の記録のみ**を行う。Entry価格・SL・TP・保有期限はレコードに含まれない。分類フィールド(`signal_type` / `trigger_type` / `alert_source` / `strategy_profile`)は記録用であり、判断には使わない。
- **Paper Execution は保存済み `paper_signals` レコードの読み取りのみ**で連携する(Generator は呼ばない、Signal DB へは書かない)。Entry価格の決定、SL / TP / 最大保有時間の導出と判定、仮想損益計算はすべて Paper Execution の責務である。
- 執行規則(SL / TP 導出、Entry有効期限、最大保有時間、曖昧足の優先順位、コスト)は Paper Execution 所有の **execution_profile** で解決し、Signal 側の `strategy_profile` は解決キーに使わない。通常CLIはCLI引数から単一のdefault profileを作り、全symbol/timeframeへ適用する。内部APIは呼び出し側が `(symbol, timeframe)` ごとの `overrides` 辞書を渡した場合の解決にも対応するが、このマッピングを設定ファイルやCLIからロードする通常運用経路は未実装である。
- 詳細な入出力境界・状態遷移・約定規則は [Paper Execution](paper_execution.md) を参照。

## Backtest と Paper Execution の役割差

どちらも「実発注しない仮想execution」を扱うが、役割と時間の向きが異なる。

| 観点 | Backtest (`backtest/`) | Paper Execution (`paper-execution/`) |
|---|---|---|
| 目的 | 過去データ上で Strategy / Execution ルールを遡及再生・評価する検証基盤(Validation) | 保存済み Paper Signal を現在進行の確定足で仮想執行する常駐処理 |
| 時間の向き | `--from`〜`--to` の過去区間を一括再生 | 現在時刻に向かって前進し、確定足を1本ずつ消化 |
| シグナルの由来 | run 内で候補を生成(`signal_generator.py` / `squeeze_breakout.py` を再生) | Live の Paper Signal サービスが保存したレコードを取り込む |
| Entry タイミング | trend_follow 経路はバー close 価格(既知の lookahead 制約として文書化済み)。squeeze 経路は trigger 足基準 | Signal 根拠足の**次の確定足の open**(lookahead を最初から採らない) |
| 入力 | Chart DB / Market Bias DB(読み取り専用) | Paper Signal DB / Chart DB(読み取り専用) |
| 出力 | Backtest DB(`backtest_runs` / `backtest_signals`)、summary | Paper Position DB(`virtual_positions`) |
| 実行形態 | CLI(`python -m backtest`)による単発実行 | runner / 常駐ループ(`execution_runner.py` / `paper_execution_bot.py`) |

両者は直接のデータ連携をしない。代わりに約定規則の語彙・保守的方針を共有し(下記)、結果を突合可能に保つ。

## Strategy / 売買ルールの共有状況

実装済みの共有関係と、将来構想を区別して記載する。

**実装済みの共有:**

- **Signal 生成ルール**: `paper-signal/signal_generator.py` を、Live の常駐 Paper Signal と Backtest(trend_follow 経路)が共用する。生成一致は `backtest/tests/test_live_backtest_parity.py` で固定している。
- **Market State 分類器**: `market-watch/market_state.py` の `classify_market_state()` を Live の Chart Snapshot と Backtest が共用する(byte-identical)。
- **Exit の語彙・保守的方針**: 同一足で SL / TP の両方へ到達した場合の `same_candle_priority`(既定 `stop_loss` = 保守側)と、不利方向へのコスト適用(spread / slippage)は、Backtest 側(`paper-signal/exit_policy.py` / `backtest/execution_model.py`)と Paper Execution 側(`execution_engine.py` / `execution_profile.py`)で同じ語彙・同じ既定値を用いる。
- **執行規則パラメータの記録**: 両者とも、適用した規則・コストの実額をレコードへ記録し、後から設定を変えても既存レコードの再現性が壊れないようにする。

**独立している部分(混同しない):**

- Paper Execution の執行規則は Paper Execution 所有の **execution_profile** で解決する。通常CLIでは単一profile、内部APIでは任意に渡された `(symbol, timeframe)` overridesを扱う。symbol/timeframe別設定のファイル/CLIロードは未実装である。Backtest の `--spread` / `--slippage` は run 単位のシミュレーションパラメータであり、両者は独立に管理する(同じ値を与えれば同じ調整式で比較できる、という関係に留める)。
- 外部 Strategy 定義(YAML, `backtest/strategies/`)は現在 **Backtest 経路のみ**が読み込む([Strategy 定義仕様](strategy_definition.md))。

**将来構想(未実装):**

- 外部 Strategy 定義を Live の Paper Signal / Paper Execution へ接続し、同一定義で Live / Backtest を駆動する共有化は将来構想であり、未実装である。現時点で「同一の Strategy 定義ファイルが全経路を駆動している」わけではない。

## データ連携概要

- `market-watch/chart_collector.py` はローソク足を取得し、SQLiteへ保存する。
- `market-watch/indicators.py` は共通指標計算を担当する。
- `market-watch/chart_snapshot.py` は状態表示と Market Snapshot 向け summary を作る。
- `market-watch/*_alert_bot.py`(macd / ma / bollinger / rsi)はイベント通知を担当する。
- `news-feed-bot/market_bias.py` はMarket Bias(trend / risk / volatility)を計算する。
- `news-feed-bot/market_bias_store.py` はMarket Biasの評価結果をSQLiteへ永続化し、時点参照APIを提供する。
- `paper-signal/signal_generator.py` は paper_only 仮説を生成する(`paper_signal_bot.py` が常駐サービスとして保存・通知する)。
- `paper-execution/execution_runner.py` / `paper_execution_bot.py` は、保存済み Paper Signal と確定足(Chart DB)から仮想ポジションを生成・更新する。確定した状態遷移(open / closed / expired / cancelled)は `paper_execution_notifier.py` が Discord へ通知する(Issue #83。**Transactional Outbox**: Position 遷移と通知 enqueue を同一トランザクションで確定し、配送は commit 後・claim/lease 付き・at-least-once。失敗はポジションへ影響しない)。
- `backtest/runner.py` は過去データを再生し、結果を Backtest DB へ保存する([Backtest](../backtest.md))。

## DB構成

BELIAL はコンポーネントごとに独立したSQLiteファイルを持つ。共有DBはない。

| DB | パス(既定) | 所有コンポーネント | 主なテーブル |
|---|---|---|---|
| Chart DB | `market-watch/data/chart_data.sqlite3` | `market-watch/` | `candles`, `chart_snapshots`, `alerts`, `alert_chart_snapshots` |
| Paper Signal DB | `paper-signal/data/paper_signals.sqlite3` | `paper-signal/` | `paper_signals` |
| Market Bias DB | `news-feed-bot/data/market_bias.sqlite3` | `news-feed-bot/` | `market_bias` |
| Paper Position DB | `paper-execution/data/paper_positions.sqlite3` | `paper-execution/` | `virtual_positions`, `paper_execution_notifications` |
| Backtest DB | `backtest/data/backtest_results.sqlite3` | `backtest/` | `backtest_runs`, `backtest_signals` |
| Execution Order DB | `execution/data/execution_orders.sqlite3` | `execution/` | `execution_orders`, `execution_order_state_history`, `execution_repair_fences`, `execution_reissue_clearances`(Issue #74。Live Execution 用の永続化基盤。これを書き込む常駐プロセスは未実装。schema v4 = history に構造化 repair provenance 列 `repair_attempt_id` + `repair_generation`(partial UNIQUE = 1 attempt 高々 1 行)と、注文ごとに現在有効な repair 実行の **完全な identity**(attempt / generation / **takeover id** / owner / claim execution id)を映す **fence テーブル**を追加。`record_order_result` はこの 5 要素を transaction 内で照合する。Issue #104。schema v5 = 再発行チェーンの永続化(Issue #95 / #115): orders へ親参照 / `signal_attempt` / kind / initiated_by / reason / 検証可能な根拠参照 2 列、history へ送信時 clearance provenance 列、**append-only の reissue clearance evidence テーブル**(signal × 世代。空で開始 = fail-closed)を追加し、signal 単独 UNIQUE を `(signal_id, signal_attempt)` UNIQUE + `reissued_from` UNIQUE(1 親 1 子)へ置換) |
| Safety Gate Decision DB | `execution/data/safety_gate_decisions.sqlite3` | `execution/` | `safety_gate_decisions`(Issue #75。Safety Gate の allow / reject 判定・拒否コード・policy_id の監査保存。execution store の closed-world schema 検証と同居できないため別ファイル。実注文なし) |
| Submission Journal DB | `execution/data/submission_journal.sqlite3` | `execution/` | `submission_journal`(Issue #93。Broker 送信前後の送信試行の耐久記録: 送信内容の要約・接続 identity・`broker_call_state`(not_started / started / responded / unknown)・Execution DB 保存状態・機械可読 `outcome`(open / completed / reconciliation_required)。**Execution Order DB と独立**で、Execution DB が schema 異常 / read-only / 破損 / 消失でも未解決送信を列挙・restart 後復元できる。Broker 送信前の Journal 保存失敗は fail-closed = 送信されない。実注文なし) |
| Kill Switch DB | `execution/data/kill_switch.sqlite3` | `execution/` | `kill_switch_transitions`(Issue #81。Execution 層の永続 Kill Switch: 明示操作 1 回 = 1 行の **append-only** 遷移履歴(有効/無効・UTC 時刻・operator・reason。UPDATE / DELETE は trigger が拒否)。現在状態 = 最大 `transition_id` の行、初期化済み 0 行 = disabled。Safety Gate は `PersistentKillSwitchStateProvider` 経由で毎評価この DB を再読し、**DB が読めない場合は fail-closed(`state_query_failed`)で全新規送信を拒否**する。他の execution store と同方針(closed-world schema 検証・pristine init・true read-only 読み取り)・別ファイル。実注文なし) |
| Broker Reconciliation DB | `execution/data/broker_reconciliation.sqlite3` | `execution/` | `broker_reconciliation_runs`, `broker_reconciliation_discrepancies`(Issue #103。内部注文状態と Broker 注文状態の照合結果: run 単位の append・差分種別 / 対象 ID / 両側の観測状態 / resolution category / 判定理由。**検出は注文状態を変更しない**)+ `broker_reconciliation_repair_attempts` + `broker_reconciliation_repair_takeover_intents`(Issue #104 / schema v2。修復試行の監査: **pending claim → finalized の 2 段階 append**・**claim(owner / claim execution id / generation / takeover id / lease)**・outcome + closed set の failure code・前後状態・観測 revision・Broker 参照 ID・観測 / 予約 / **Broker 状態時刻** / finalize 時刻。観測行と修復行は別の監査イベントで、run / discrepancy 行は書き換えない。pending 行のうち **lease が切れたものだけ**を recovery が takeover するが、その takeover は **durable な intent 行**(identity・状態機械・blocked 理由・観測値)に駆動され、cross-DB の各段階で落ちても**同じ identity で再開**される)。Issue #105 / schema v3 で `broker_reconciliation_position_discrepancies`(position / trade / transaction 由来 close / SL・TP の検出記録。**CHECK で `manual_review` のみ許可** = 自動修復候補として保存不能)と runs の position summary 列(checked counts / 列挙エラー / 除外シンボル。全 NULL = position スコープ未照合)を追加。注文テーブルを持たないため、この DB 経由で注文状態は書き換えられない(修復の書き込みは execution store の状態機械 API のみ) |
| Shadow Result DB | `execution/data/shadow_results.sqlite3` | `execution/` | `shadow_results`(Issue #78。Shadow Mode の検証記録: Execution Request 全体・想定注文内容・Safety Gate 判定と拒否理由・適用 policy / market snapshot。Broker 送信なし・Broker 応答語彙なし。通常注文状態とは別 namespace で、Shadow 実行が後の fake / demo / live 送信をブロックしない) |

いずれも `data/` はGit管理しない。関連付けは時刻のみに依存せず、可能な限りIDで行う(例: `alerts.snapshot_id` → `chart_snapshots.id`、`virtual_positions.signal_id` → `paper_signals.signal_id`)。時刻は補助情報として扱う。詳細スキーマは [Market Watch](market_watch.md)、[Market Bias](market_bias.md)、[Paper Signal](paper_signal.md)、[Paper Execution](paper_execution.md)、[Backtest](../backtest.md) を参照。

## SQLite schema validation / migration

Persistence の責務は各コンポーネントの store 層が持つ。他コンポーネントのDBを migration することはない。ただし、schema validation / migration の保証範囲はstoreごとに異なり、全store共通の完全検証を提供するものではない。

- `paper-execution/position_store.py`: `PRAGMA user_version` を現行スキーマの目印としつつ、実スキーマ(`PRAGMA table_info` の pk/notnull、`sqlite_master.sql` の CHECK、`PRAGMA index_list`/`index_info`、write 経路では SAVEPOINT 内の behavioral probe)を検証する。列名だけの検証ではなく実制約を検証し、制約欠損テーブルは user_version を偽装しても `current` としない。legacy schema からの migration は **table rebuild**(CREATE → 検証コピー → DROP → RENAME、1トランザクション・失敗時 rollback)で CHECK / UNIQUE / NOT NULL 制約を実際に付与し、安全に変換できない legacy row は明示的に migration を失敗させる(黙って捨てない)。schema v3(Issue #83)は通知 outbox テーブル `paper_execution_notifications`(claim/lease 列付き)を追加する: v1 からの upgrade は additive(`virtual_positions` を rebuild せず)、v2 prototype(claim 列無し)からは outbox テーブルの transactional rebuild(既存行を保持)。schema v4(Issue #85)は `cancelled` の terminal_reason CHECK へ `position_limit` を追加する: CHECK 自体が変わるため pre-v4 DB は `virtual_positions` の transactional rebuild で移行する(v3 全行は v4 CHECK の上位集合を満たし行は保持。失敗時は全 rollback)。同時ポジション上限の判定は capacity チェックと INSERT を単一 `BEGIN IMMEDIATE` トランザクションで行い、並行 runner の check-then-insert(TOCTOU)による上限突破を構造的に防ぐ。
- `backtest/backtest_store.py`: 不足列を冪等な `ALTER TABLE ADD COLUMN` で追加する。分類フィールドは安全な既定値で補完し、`market_state` のような事後復元不能な列は NULL のまま残す(値を捏造しない)。
- `paper-signal/paper_signal_store.py`: classification / `target_candle_utc` / `market_state` など既知の不足列を冪等に追加し、既知値をbackfillする。基本列の型・NOT NULL・PRIMARY KEYを含む既存schema全体の完全性検証ではない。
- `market-watch/chart_collector.py`: テーブルを `CREATE TABLE IF NOT EXISTS` で初期化し、`chart_snapshots` などの一部既知列を追加する。全テーブル・全制約の完全性検証ではない。
- `news-feed-bot/market_bias_store.py`: `CREATE TABLE/INDEX IF NOT EXISTS` による初期化が中心であり、既存schemaの型・制約を完全検証またはmigrationする仕組みではない。
- `execution/execution_store.py`(Issue #74 / PR #91): schema の新規作成は **pristine な DB のみ**(`sqlite_master` 0 件かつ `user_version` 0。version だけ・metadata だけの DB は拒否)。既存 schema の判定は列名ではなく**完全な構造検証**(`PRAGMA user_version` + 両テーブル・両 index の正規化 CREATE SQL 全体 = NOT NULL / UNIQUE / CHECK / FK + `PRAGMA table_info`/`index_list`/`index_info`/`foreign_key_list` による構造 signature + 予期しないテーブル・index の不在)で行い、**読み取り経路も同じ検証を通ってから復元する**。schema v3(Issue #76)は `signal_id` の UNIQUE index を、schema v4(Issue #104)は history の構造化 repair provenance 列(`ALTER TABLE ADD COLUMN repair_attempt_id` / `repair_generation` + partial UNIQUE index)と repair fencing テーブル `execution_repair_fences`(注文ごとに現在有効な attempt / generation / takeover id / owner / claim execution id を保持し、`record_order_result` が**同一トランザクション内で 5 要素すべてを照合**して、旧 generation はもちろん**同一 generation の別実行**の commit も拒否する)を、schema v5(Issue #95 / #115)は**再発行チェーンの永続化**(orders への `ALTER TABLE ADD COLUMN` × 7 = 親参照 / `signal_attempt NOT NULL DEFAULT 1`(既存行は attempt 1 として backfill)/ kind / initiated_by / reason / 検証可能な根拠参照 2 列、history への送信時 clearance provenance 列、append-only の `execution_reissue_clearances` テーブル(**空で開始** = 既存 signal の再発行には新しい観測が必要 = fail-closed)、signal 単独 UNIQUE index の DROP と `(signal_id, signal_attempt)` UNIQUE + `reissued_from` UNIQUE への置換 — **テーブル rebuild・行の書き換えなし**)を追加する: **リリース済み v2 / v3 / v4 と完全一致した DB のみ** `init_db()` が migration する(旧版は連鎖。各段階とも version bump → 完全再検証を単一トランザクションで実施。途中失敗は全体 rollback、既存行は削除・書き換えなし、重複 signal を含む v2 DB は明示的に失敗、読み取り経路は migration しない)。それ以外(v1・partial schema・制約欠損テーブル・他コンポーネントのDB)は **DB を変更せず**明示的に失敗する(自動補完・黙った rebuild をしない)。存在しないDBファイルは作成せず、復元時に current / history chain・enum・UTC・reason・history に anchor した OrderResult snapshot・broker_order_id の DB 全体一意性を再検証する。
- `execution/safety_store.py`(Issue #75): execution_store と同方針。新規作成は pristine な DB のみ、全 read / write が closed-world schema 検証(sqlite_master 全 object 集合 + token 正規化 DDL + PRAGMA 構造 signature + `user_version`)を通り、trigger / view / 制約欠損 lookalike / 他コンポーネント DB は **DB 無変更で**拒否する。読み取りは missing DB を作成せず、保存行(拒否コード JSON・UTC・allow/拒否整合)を厳格に再検証する。**書き込み失敗は例外 = Safety Gate が注文を送信しない**(fail-closed)。 schema v2(Issue #116)は判定行へ `policy_snapshot_json`(policy 内容の canonical JSON snapshot)を additive に追加する: **完全検証で v1 と一致した DB のみ**を `initialize()` が単一トランザクションで migration(`ALTER TABLE ADD COLUMN` → version bump → v2 完全再検証。途中失敗は全 rollback・既存行は snapshot NULL で読み戻し・読み取り経路は migration しない)。
- `execution/submission_journal.py`(Issue #93): execution_store / safety_store と同方針。新規作成は pristine な DB のみ、全 read / write が closed-world schema 検証を通り、不正 schema は **DB 無変更で**拒否する。書き込みは同一トランザクション内の commit 前 read-back 検証つきで、missing DB を暗黙作成しない(**未初期化 = 失敗 = Broker へ送信されない**)。読み取りは true read-only 接続(`execution/sqlite_ro.py`)で missing DB を作成せず、保存行(enum・UTC・数量・ライフサイクル整合)を厳格に再検証する。 schema v2(Issue #116)は終端 outcome `superseded` + `superseded_by_execution_request_id` 列 + ライフサイクル CHECK の置換を導入する: inline CHECK の書き換えは `ALTER TABLE` では不可能なため、**完全検証で v1 と一致した DB のみ**を `initialize()` が単一トランザクションで **table rebuild**(v2 テーブル新設 → 全行無変更コピー(新列 NULL。v2 制約は v1 の緩和)→ 行数検証 → 旧テーブル DROP → index 再作成 → version bump → v2 完全再検証 → 全行モデル再検証)により migration する。途中失敗は全 rollback で v1 無変更・読み取り経路は migration しない。
- `execution/shadow_store.py`(Issue #78 / PR #100): execution_store / safety_store と同方針。新規作成は pristine な DB のみ(冪等・単一トランザクション・失敗時全 rollback)、全 read / write が closed-world schema 検証を通り、不正 schema は **DB 無変更で**拒否する(黙った上書きなし)。行は **claim-first ライフサイクル**(`phase` = claimed / completed。Gate 評価前に identity を claim し、lease 切れの stale claim は同一 payload の次回実行が回収)を持ち、`execution_request_id` / `signal_id` / `client_order_id` の UNIQUE + canonical payload 比較で「同一 payload = 明示 duplicate / 異 payload = 明示 conflict」を区別する。読み取りは **true read-only 接続**(`execution/sqlite_ro.py`: missing DB・WAL/SHM・journal を作らず mtime 不変)で行い、保存行(request / intended order / rejections / market / policy / **account snapshot** の各 JSON キー集合完全一致・列との相互整合・canonical payload 再計算・UTC・allow/拒否整合)を厳格に再検証する。
- `execution/reconciliation_store.py`(Issue #103 / #104): execution_store / safety_store / shadow_store と同方針。`init_reconciliation_db()` は冪等で、新規作成は pristine な DB のみ(単一トランザクション・作成後に完全検証・失敗時は全 rollback)、現行 schema の既存 DB は closed-world 検証のみで**既存行を変更しない**。schema v2(Issue #104)は repair 監査テーブル `broker_reconciliation_repair_attempts` を追加する: **リリース済み v1 と完全一致した DB のみ** `init_reconciliation_db()` が additive に migration する(v1 のテーブル DDL は v2 と同一。テーブル + index 作成 → version bump → v2 完全再検証を単一トランザクションで実施。途中失敗は全体 rollback、既存行は削除・書き換えなし、**読み取り経路は migration しない** = dry-run の無副作用を維持)。全 read / write が同じ検証を通り、trigger / view / 制約欠損 lookalike / 他コンポーネント DB は **DB 無変更で**拒否する。読み取りは **true read-only 接続**(`execution/sqlite_ro.py`)で missing DB を作成せず、復元行はモデル不変条件で厳格に再検証する。記録は **run / attempt 単位の append**(同一内容の再照合・再修復も別行として残し、既存行は上書きしない。既存 `run_id` / `attempt_id` の再利用は明示的に拒否 — attempt は reservation 時点 = 状態更新前に拒否される。attempt は pending claim → finalized の 2 段階で、claim の takeover は **durable takeover intent**(active intent は attempt ごとに高々 1 件・同じ generation は 2 度 takeover 不可・closed な state machine。**status は遷移ごとに専用 API が同一 transaction 内で実体(claim promotion / finalize / fence proof)を検証**するため、実体なしに進めることはできない)に駆動され、finalize は **owner / claim execution id / generation / takeover id 込みの CAS** 保護・ちょうど 1 回。自動収束できない不整合は intent 行へ `blocked` として永続化され typed に報告される。dry-run は claim・intent を含め記録自体を拒否)。v2 migration の `ALTER TABLE ADD COLUMN` は discrepancies の `internal_revision` を追加する(既存行は NULL で読み戻し)。schema v3(Issue #105)は position discrepancies テーブルと runs の position summary 8 列を additive に追加する: migration は**リリース済み前バージョンと完全一致した DB のみ**を対象に単一トランザクション内で段階適用され(v1 → v2 → v3)、order 系テーブルの DDL は変更しない(CHECK は enum の凍結サブセットから生成されるため、enum 拡張後も既存テーブルの DDL はリリース済み定義とトークン一致し、再構築は発生しない)。既存 run 行は summary 全列 NULL(= position スコープ未照合)で読み戻される。
- **dry-run では DB作成・migration を行わない**(Paper Execution は読み取り専用のスキーマ確認のみ行う。Shadow の dry-run も同様に read-only probe のみ)。

## dry-run 方針

すべてのBot/生成処理は `--dry-run`(または `dry_run=True`)で以下の副作用を発生させない。

- Discord送信
- state ファイル更新
- DB作成・更新・migration
- S3アップロード

Paper Execution の dry-run は完全無副作用であり、読み取りエラーも成功扱いしない(Chart DB は strict read で読み、破損・テーブル欠損を cycle failure として計上する)。WAL DB でも副作用ゼロを守るため、dry-run の schema probe / position 読み取りは実 DB へ接続せず、本体 + `-wal` / `-shm` を一時ディレクトリへコピーした snapshot を読む(通常の read-write 接続は WAL を checkpoint して本体を書き換えるため)。詳細と起動コマンドは [Operations](../operations/operations.md) を参照。

## 関連ドキュメント

- [Market Snapshot](market_snapshot.md)
- [Market Watch](market_watch.md)
- [Market Bias](market_bias.md)
- [Paper Signal](paper_signal.md)
- [Paper Execution](paper_execution.md)
- [Strategy 定義仕様](strategy_definition.md)
- [Broker Interface](broker_interface.md)
- [Execution Request / Order Persistence](execution_request.md)
- [Safety Gate / Live Order Submission](safety_gate.md)
- [Submission Journal](submission_journal.md)
- [Shadow Mode / Shadow Execution](shadow_execution.md)
- [Execution Intent Reissue](execution_reissue.md)(Issue #95 設計。#114〜#117 で実装済み)
- [Broker Reconciliation](broker_reconciliation.md)
- [Backtest](../backtest.md)
- [Operations](../operations/operations.md)
- [Roadmap](../product/roadmap.md)
