# Paper Execution(最小設計)

Issue #49 の成果物。Paper Signal から仮想ポジションを生成し、決済まで追跡する **Paper Execution 層**の最小責務と状態遷移を定義する設計文書である。

**本書は設計のみを対象とする。** DBテーブル・Repository・Open / Close判定コード・Discord通知・Broker API連携・実注文は本Issueのスコープ外であり、後続Issue(仮想ポジションデータモデルと保存層 / Open・Close判定エンジン)で実装する。

## 目的と位置づけ

Paper Execution は、[Paper Signal](paper_signal.md) が記録した paper_only の仮説を入力に、時間進行に沿って **仮想Entry → 仮想ポジション保持 → 仮想Close → 仮想損益確定** を行う独立レイヤーである。[Concept のレイヤー分離](../product/concept.md#レイヤー分離) における Execution variants の paper 側にあたる。

```text
Market Bias + Chart Snapshot
        ↓
Paper Signal Generator(実装済み・仮説の記録のみ)
        ↓  paper_signals レコード
Paper Execution(本書・設計のみ)
        ↓  仮想ポジション / 仮想損益レコード
(将来)Live Execution ── Broker API による実注文。Paper Execution とは完全に別レイヤー
```

- **実発注・自動売買は一切行わない。** Paper Execution が生成するのは「もしこの仮説どおりに執行していたら」という仮想レコードだけである。
- **Broker API(OANDA / XM / MT5 等)には接続しない。** 実装時には Paper Signal と同様に、broker API の import・発注関数呼び出しが存在しないことをテストで検証する(`test_module_source_has_no_broker_api_integration` と同型)。
- **Backtest とは別物である。** Backtest は過去データを遡及再生する Validation 基盤(`backtest/`)であり、Paper Execution は **現在時刻に向かって前進する時間進行の中で** 確定足を1本ずつ消化する常駐処理である。ただし約定規則の語彙・保守的方針(`paper-signal/exit_policy.py` の same_candle_priority、`backtest/execution_model.py` の不利方向コスト)は共有し、両者の結果を比較可能に保つ。

## 責務(やること / やらないこと)

### Paper Execution がやること

1. 保存済み Paper Signal を取り込み、仮想ポジションを **1 Signal につき高々1つ** 生成する
2. Entry価格の決定と仮想Entry(open 遷移)
3. SL / TP / 最大保有時間による仮想Close判定と Close価格の決定
4. spread / slippage / 手数料を約定価格へ反映した仮想損益(pnl)の算出
5. 仮想ポジションの状態遷移(下記)の管理と記録

### Paper Execution がやらないこと

| やらないこと | 責務を持つレイヤー |
|---|---|
| シグナル仮説の生成・候補判定(Market Bias / MACD / freshness 等の評価) | Paper Signal Generator |
| Signal の採用抑制(cooldown / re-entry / Market State フィルタ) | 上流(Signal生成・採用側)または将来の Portfolio / Risk 層(下記「責務境界」) |
| 過去データの遡及検証 | Backtest(Validation) |
| 実注文・実約定・実ポジション管理・Broker API 連携 | Live Execution(将来・未実装) |
| ロット計算・レバレッジ計算・資金管理 | 非スコープ(将来検討) |
| Discord通知 | 本設計では非スコープ(後続Issueで別途検討) |

## 入力(Paper Signal から受け取る項目)

入力は `paper_signals` テーブルの保存済みレコード(スキーマは [Paper Signal](paper_signal.md#出力データpaper-signal))である。Paper Execution は Paper Signal Generator を呼び出さず、**保存済みレコードの読み取りのみ**で連携する(疎結合)。

| 項目 | 用途 |
|---|---|
| `signal_id` | 仮想ポジションとの1:1対応キー。冪等性の基準(下記) |
| `symbol` / `timeframe` | 判定に使うローソク足系列の特定 |
| `side`(`long` / `short`) | 仮想ポジションの方向 |
| `target_candle_utc` | Signal根拠となった確定足の open timestamp。Entry足の決定基準 |
| `strategy_profile` | 適用する Entry / Close パラメータプロファイルの解決キー |
| `status` | `"paper_only"` であることの確認(それ以外は受理しない) |
| `market_bias` / `confidence` / `signal_type` / `trigger_type` / `alert_source` ほか | 記録用の引き継ぎのみ。執行判断には使わない |

Paper Signal は entry / exit を一切決めない(既存設計の不変条件)ため、**Entry価格・SL・TP・保有期限はレコードに含まれない。これらの導出はすべて Paper Execution の責務**である。

このほかに、判定用のローソク足を Chart DB(`market-watch/data/chart_data.sqlite3` の `candles`)から **読み取り専用** で参照する。使用するのは Issue #41 と同じ規律に従う **確定足のみ**(`T + timeframe <= 評価時刻` を満たす足。`market-watch/freshness.py` の共有ルールを再利用)。未確定足で Entry / Close を判定することはない。

## 保持・生成する情報(仮想ポジション)

Paper Execution が保持する中心データは **仮想ポジション(paper position)** である。概念モデルは以下(物理スキーマ・列型は後続のデータモデルIssueで確定する)。

| フィールド | 説明 |
|---|---|
| `position_id` | 仮想ポジションの一意ID |
| `signal_id` | 元 Paper Signal への参照(UNIQUE。1 Signal = 高々1ポジション) |
| `symbol` / `timeframe` / `side` / `strategy_profile` | Signal から引き継ぐ |
| `state` | `pending` / `open` / `closed` / `expired` / `cancelled`(下記) |
| `entry_price` | 仮想Entry約定価格(コスト調整後)。`pending` 以前は NULL |
| `entry_time_utc` | Entry足の open timestamp(UTC ISO-8601 `...Z`。時刻規約は Paper Signal と共通) |
| `stop_loss_price` / `take_profit_price` | Entry時に確定する仮想SL / TP水準。実注文ではなく判定用のマーカー |
| `max_hold_deadline_utc` | 最大保有時間による強制Close期限(Entry時に確定) |
| `close_price` / `close_time_utc` | 仮想Close約定価格(コスト調整後)と時刻。未Closeは NULL |
| `close_reason` | `take_profit` / `stop_loss` / `max_holding_time`(+ 終端理由 `entry_timeout` / `cancelled_*`) |
| `pnl` | 決済済みのみ。`side` に応じた符号付き価格差(コスト込み)。未決済は NULL(0扱いで捏造しない — Backtest summary と同方針) |
| `spread` / `slippage` / `commission` | この約定に適用したコスト実額(再現性のため適用値を記録) |
| `created_at_utc` / `updated_at_utc` | 監査用 |

状態(`state`)はライフサイクル、`close_reason` は Close の内訳であり、両者を混同しない。Backtest の `result_status` 語彙との対応は「closed + `take_profit` ≒ win」「closed + `stop_loss` ≒ loss」「closed + `max_holding_time` ≒ expired(Backtest語彙)」となるが、Paper Execution 側では **勝敗ラベルではなく事実(理由)を保存**し、win/loss は集計時に導出する。

## 状態一覧と状態遷移

### 状態候補の整理

| 状態 | 意味 | 終端 |
|---|---|---|
| `pending` | Signal を受理済みで、仮想Entryの約定待ち | |
| `open` | 仮想Entry約定済み。Close判定の対象 | |
| `closed` | SL / TP / 最大保有時間のいずれかで仮想決済済み(`close_reason` に理由を保持) | ✅ |
| `expired` | Entryできないまま有効期限切れ(ポジションを持たずに終了。pnl なし) | ✅ |
| `cancelled` | Entry前に取消(入力不整合の検出、Signal の status 異常等。pnl なし) | ✅ |

- `closed` / `expired` / `cancelled` は終端状態であり、**終端状態からの遷移は存在しない**(再openしない)。
- 「openのまま期限切れ」は `expired` ではなく **`closed` + `close_reason = max_holding_time`** とする。`expired` は「一度もEntryしなかった」場合に限定し、ポジションを持ったか否かを state だけで判別できるようにする。
- `rejected` 相当(受理前の重複Signal skip)は状態を作らない。冪等性チェックで弾かれた Signal はポジション自体を生成しない(下記「冪等性」)。

### 状態遷移表

| From | To | トリガー |
|---|---|---|
| (なし) | `pending` | 新規 Paper Signal の受理(冪等性チェック通過、`status == "paper_only"` 確認済み) |
| `pending` | `open` | Entry足(下記)の確定を検知し、仮想Entry約定 |
| `pending` | `expired` | Entry有効期限(`max_entry_delay`)内に Entry足が確定しなかった |
| `pending` | `cancelled` | Entry前の無効化(入力不整合・運用上の取消) |
| `open` | `closed` | SL到達 / TP到達 / 最大保有時間到達(優先順位は下記) |

```text
                 ┌──────────┐
 Paper Signal ──▶│ pending  │──── Entry約定 ────▶┌────────┐
 (受理)          │          │                    │  open  │
                 └────┬─────┘                    └───┬────┘
                      │                              │ SL / TP / 最大保有時間
        期限切れ      │  取消                        ▼
       ┌─────────┐    └──────▶┌───────────┐    ┌──────────┐
       │ expired │            │ cancelled │    │  closed  │
       └─────────┘            └───────────┘    └──────────┘
       (終端)                 (終端)           (終端, close_reason保持)
```

## Entry価格の決定規則

1. **Entry足**: `target_candle_utc + timeframe`(= Signal根拠足のclose時刻)以降で **最初に存在する確定足** を Entry足とする。Signal根拠足自身では Entry しない(根拠足のclose価格で入る Backtest の既知の lookahead 制約を、Paper Execution では最初から採らない)。
2. **Entry価格(raw)**: Entry足の **open 価格**。成行相当であり、指値・逆指値のEntryは本設計では扱わない。
3. **Entry価格(有効)**: raw open に対し、不利方向へ spread + slippage を加算した値(long: `open + spread + slippage`、short: `open - spread - slippage`。`backtest/execution_model.py` と同じ固定・不利方向モデル)。
4. **Entry有効期限**: Entry足の open 時刻が `target_candle_utc + timeframe` から `max_entry_delay`(既定: **timeframe × 3本分**。strategy_profile 単位で調整可能なパラメータとする)を超える場合、Entryせず `expired` とする。週末・休場明けの持ち越しEntry(金曜のSignalで月曜openに入る等)は既定では行わない。
5. **SL / TP水準の確定**: Entry約定と同時に、有効Entry価格を基準として strategy_profile が解決するルール(初期実装は `exit_policy.py` の `fixed_rr` と同じ `fallback_stop_pct` × `risk_reward` 方式)で `stop_loss_price` / `take_profit_price` を確定し、以後変更しない(トレーリングは非スコープ)。`max_hold_deadline_utc` も同時に確定する。

## Close規則(SL / TP / 最大保有時間)

Close判定は **Entry足以降の確定足を時系列順に1本ずつ** 評価する(Entryは open 時点の約定なので、Entry足自身の high / low も判定対象に含む。Signal根拠足は含まない)。

各確定足について、以下の順で判定する。

1. **gap判定(足の open)**: 足の open が既に SL / TP を超えて始まっていないか(下記「gap open時の約定規則」)
2. **intrabar判定(足の high / low)**:
   - long: `low <= stop_loss_price` → SL到達、`high >= take_profit_price` → TP到達
   - short: `high >= stop_loss_price` → SL到達、`low <= take_profit_price` → TP到達
3. **最大保有時間判定**: その足のclose時刻が `max_hold_deadline_utc` に達していれば、その足の **close価格** で `close_reason = max_holding_time` としてClose

Close価格にも Entry と同じ不利方向コスト(long のClose: `価格 - spread - slippage`、short のClose: `価格 + spread + slippage`)を適用する。pnl は「有効Close価格 − 有効Entry価格」(short は符号反転)から commission を差し引いた値とする。

同一足で SL / TP と最大保有時間の両方が該当する場合は **価格到達(SL / TP)を優先** する(保有時間はその足のclose時刻で初めて満了するのに対し、価格到達は足の途中で発生しているため)。

### 同一足でSLとTPの両方へ到達した場合の優先順位

1本の確定足の high / low が SL と TP の両方に届いた場合、足内の到達順序は OHLC からは復元できない。この曖昧足の扱いは:

- 設定 `same_candle_priority`(`stop_loss` / `take_profit`)で明示的に決める。**既定は `stop_loss`(保守側 = 成績を過大評価しない)**。
- ハードコードせず strategy_profile / 設定として持ち、記録に残す。`paper-signal/exit_policy.py` の `same_candle_priority`(既定 `stop_loss`)と同じ語彙・同じ既定値を用い、Backtest との突合を可能にする。

### gap open時のEntry / SL / TP約定規則

窓開け(前足closeと当足openの乖離、週明け・休場明けを含む)に対する規則。方針は一貫して **「不利なgapは実額どおり受け、有利なgapは計上しない(保守側)」**。

| ケース | 約定規則 |
|---|---|
| Entry(成行相当) | gapの有無にかかわらず Entry足の open で約定する(gap自体による補正はしない。コストは通常どおり不利方向へ加算)。ただし Entry有効期限を超えた場合は `expired`(前述) |
| 足の open が既に SL を超えて始まった | SL価格ではなく **その足の open 価格** で約定する(gapによる不利スリッページを隠さない)。`close_reason = stop_loss` |
| 足の open が既に TP を超えて始まった | **TP価格** で約定する(gapによる有利な上振れは計上しない)。`close_reason = take_profit` |

open は単一価格のため「openがSLとTPを同時に超える」ことは起こり得ない(SL < Entry < TP の関係が保たれる限り)。openによるgap判定の後、同一足の high / low による intrabar 判定を続けて行う。

## spread / slippage / 手数料の責務境界

| コスト | 責務 | 規則 |
|---|---|---|
| spread | Paper Execution | 約定ごとに不利方向へ加算する固定の絶対価格差(pips換算しない)。`backtest/execution_model.py` の `fixed_spread_slippage` と同一の意味論 |
| slippage | Paper Execution | 同上。spread と独立に設定 |
| 手数料(commission) | Paper Execution | v1 では **既定 0.0 の固定額** としてフィールドのみ確保する(pnl から減算)。ロット・約定金額比例の手数料はロット計算が非スコープのため導入しない |

- **Paper Signal はコストに一切関与しない**(Signal レコードにコスト情報を持たせない)。コスト反映後の約定価格・pnl を持つのは Paper Execution だけである。
- 設定値は strategy_profile / 設定ファイル側に置き、適用した実額を仮想ポジションレコードへ記録する(後から設定を変えても既存レコードの再現性が壊れないようにする)。
- Backtest 側の `--spread` / `--slippage` は Backtest run のシミュレーション用パラメータであり、Paper Execution の設定とは独立に管理する(同じ値を与えれば同じ調整式で比較できる、という関係に留める)。

## 冪等性(再実行・重複Signal)

Paper Signal 側は既に `(strategy_profile, symbol, timeframe, target_candle_utc)` の UNIQUE 制約で確定足単位の冪等性を持つ(Issue #41)。Paper Execution はその上に **`signal_id` 単位の冪等性** を重ねる。

- 仮想ポジションは `signal_id` に対して UNIQUE とする(1 Signal = 高々1ポジション)。取り込み時に既存ポジションがあれば、その state にかかわらず新規生成せず skip する(`INSERT ... ON CONFLICT DO NOTHING` 相当)。
- 判定エンジンの再実行は **決定論的** であること: 入力(Signalレコード + 確定足)が同じなら、何度実行しても同じ状態遷移・同じ約定価格・同じ pnl に到達する。乱数・現在時刻依存の判定を持たない(現在時刻は「どこまでの足が確定しているか」の判定にのみ使う)。
- 各ポジションは「どの確定足まで評価済みか」の watermark を持ち、再実行・プロセス再起動時は未評価の確定足からのみ評価を再開する。評価済みの足を再評価しても結果が変わらないこと(冪等)を実装時のテスト観点とする。
- 終端状態(`closed` / `expired` / `cancelled`)のポジションは再実行で変更されない。

## cooldown / re-entry / Market State との責務境界

Backtest の `--no-reentry` / `--cooldown-minutes` / `--allowed-market-states`(Issue #38 / #39)に相当する採用抑制は、**Paper Execution の責務としない**。

- Paper Execution v1 は「保存された Paper Signal を1件ずつ、独立に、無条件で執行対象にする」層である。どの Signal を生成するか(Market State / freshness / confidence によるゲート)は Paper Signal Generator の責務、複数ポジションの同時保有制限・cooldown・ポートフォリオ制約は将来の Portfolio / Risk 層(または Signal 採用層)の責務として切り出す。
- この分離により、Paper Execution の結果は「Signal 1件あたりの素の成績」となり、Backtest のフィルタなし run と直接比較できる。抑制ロジックを Paper Execution に混ぜると、Signal品質の評価と執行規則の評価が分離できなくなるため採らない。
- Market State は Signal 生成側の入力であり、Paper Execution はEntry / Close判定に参照しない(記録用の引き継ぎのみ将来検討)。

## 実発注・Broker API との分離(明記)

- Paper Execution は **実発注・自動売買・Broker API 連携(OANDA / XM / MT5 等)・ロット計算・レバレッジ計算を一切含まない**。生成するのは paper_only の仮想レコードのみである。
- `stop_loss_price` / `take_profit_price` は判定用のマーカーであり、broker への stop / limit 注文ではない(`exit_policy.py` と同じ「observation, not execution」の境界)。
- Live Execution は将来の完全に別のレイヤーであり、Paper Execution のコード・データ・設定を実注文経路に転用しない。実装時は Paper Signal と同様、ソースコード上に broker API の import・発注呼び出しが存在しないことをテストで固定する。

## 他レイヤーとの入出力境界(まとめ)

| 相手 | 方向 | 内容 |
|---|---|---|
| Paper Signal | 入力 | 保存済み `paper_signals` レコードの読み取りのみ(Generator は呼ばない、Signal DB へは書かない) |
| Chart DB(`candles`) | 入力 | 確定足の読み取りのみ(収集・書き込みは `market-watch/` の責務) |
| 自レイヤーのDB | 出力 | 仮想ポジションレコード(独立した SQLite。コンポーネントごとに独立DBという既存方針に従う。物理設計は後続Issue) |
| Market Bias / Market State | なし | 執行判定に参照しない(Signal 生成側の入力) |
| Backtest | なし(語彙共有のみ) | 直接のデータ連携はしない。約定規則・状態語彙を揃えて結果を突合可能に保つ |
| Discord / Broker API | なし | 本設計では出力しない(通知は後続検討、Broker API は恒久的に対象外) |

## 後続Issue(実装の分割)

1. **仮想ポジションデータモデルと保存層**: 本書の概念モデルを物理スキーマ化し、`signal_id` UNIQUE・冪等 insert・watermark 更新を持つ保存層を実装する
2. **Open / Close判定エンジン**: 本書の Entry / Close / gap / 曖昧足規則を確定足の時間進行に沿って適用するエンジン(dry-run 対応、常駐化は Paper Signal と同じ規律)を実装する

## 非スコープ(本Issue)

- DBテーブル・Repository の実装
- Open / Close 判定コードの実装
- Discord通知
- Broker API 連携・実注文
- ロット計算・資金管理・複数ポジションのポートフォリオ制御
- 指値 / 逆指値Entry、トレーリングストップ、部分決済
