# Paper Execution(最小設計)

Issue #49 の成果物。Paper Signal から仮想ポジションを生成し、決済まで追跡する **Paper Execution 層**の最小責務と状態遷移を定義する設計文書である。

**本書の本文は Issue #49 時点の設計を記述する。** DBテーブル・Repository・Open / Close判定コードは、後続の Issue #50 / #51 で `paper-execution/` に**実装済み**である(末尾の「実装状況」を参照)。ただし、設計にある `(symbol, timeframe)` 別profileマッピングは内部APIとしてのみ利用可能で、設定ファイルやCLIからマッピングをロードする通常運用経路は未実装である。状態遷移の Discord通知は Issue #83 で**実装済み**(末尾の「Discord通知(Issue #83)」を参照)、同一 symbol / timeframe / side の同時ポジション制御は Issue #85 で**実装済み**(「同時ポジション制御(Issue #85)」を参照)、Broker API連携・実注文は恒久的に対象外。

## 目的と位置づけ

Paper Execution は、[Paper Signal](paper_signal.md) が記録した paper_only の仮説を入力に、時間進行に沿って **仮想Entry → 仮想ポジション保持 → 仮想Close → 仮想損益確定** を行う独立レイヤーである。[Concept のレイヤー分離](../product/concept.md#レイヤー分離) における Execution の paper 側(Paper Execution)にあたる。

```text
Market Bias + Chart Snapshot
        ↓
Paper Signal Generator(実装済み・仮説の記録のみ)
        ↓  paper_signals レコード
Paper Execution(本書・設計のみ)
        ↓  仮想ポジション / 仮想損益レコード
Live Execution ── Broker API による実注文。Paper Execution とは完全に別レイヤー
                  (基盤は broker/ + execution/ に実装済み。runner 未実装・運用未解禁)
```

- **実発注・自動売買は一切行わない。** Paper Execution が生成するのは「もしこの仮説どおりに執行していたら」という仮想レコードだけである。
- **Broker API(OANDA / XM / MT5 等)には接続しない。** Paper Signal の `test_module_source_has_no_broker_api_integration` と同様に、`paper-execution/` が `broker` / `execution` パッケージを import しないことは `execution/tests/test_package_imports.py`(`FORBIDDEN_IMPORTERS`)が回帰として固定済みである。
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
| 実注文・実約定・実ポジション管理・Broker API 連携 | Live Execution(`broker/` + `execution/` に基盤実装済み。Paper Execution はこれを一切使わない) |
| ロット計算・レバレッジ計算・資金管理 | 非スコープ(将来検討) |
| Discord通知 | 本設計(Issue #49)では非スコープとしたが、後続 Issue #83 で状態遷移通知として実装済み(`paper_execution_notifier.py`。末尾の「Discord通知(Issue #83)」) |

## 入力(Paper Signal から受け取る項目)

入力は `paper_signals` テーブルの保存済みレコード(スキーマは [Paper Signal](paper_signal.md#出力データpaper-signal))である。Paper Execution は Paper Signal Generator を呼び出さず、**保存済みレコードの読み取りのみ**で連携する(疎結合)。

| 項目 | 用途 |
|---|---|
| `signal_id` | 仮想ポジションとの1:1対応キー。冪等性の基準(下記) |
| `symbol` / `timeframe` | 判定に使うローソク足系列の特定 |
| `side`(`long` / `short`) | 仮想ポジションの方向 |
| `target_candle_utc` | Signal根拠となった確定足の open timestamp。Entry足の決定基準 |
| `status` | `"paper_only"` であることの確認(それ以外は受理しない) |
| `market_bias` / `confidence` / `signal_type` / `trigger_type` / `alert_source` / `strategy_profile` ほか | 記録用の引き継ぎのみ。執行判断には使わない |

Paper Signal は entry / exit を一切決めない(既存設計の不変条件)ため、**Entry価格・SL・TP・保有期限はレコードに含まれない。これらの導出はすべて Paper Execution の責務**である。

Paper Signal の分類4フィールド(`signal_type` / `trigger_type` / `alert_source` / `strategy_profile`)は、[Paper Signal の既存定義](paper_signal.md#分類フィールドissue-27)どおり **記録用であり、entry / exit 判断には一切用いない**。この不変条件は Paper Execution でも維持する: `strategy_profile` は Signal生成戦略の識別子として記録用に引き継ぐだけで、**執行規則(SL / TP・期限・優先順位・コスト)の解決には使わない**。執行規則は Paper Execution 自身が所有する `execution_profile`(次節)で解決する。**Paper Signal 側の既存フィールドの変更・追加は不要**である。

## execution_profile(実行規則プロファイル)

Signal生成戦略を示す `strategy_profile`(Paper Signal 所有・記録用)と、執行規則を示す **`execution_profile`(Paper Execution 所有)** を明確に分離する。

- **定義するパラメータ**: SL / TP導出規則(`fallback_stop_pct` × `risk_reward`)、Entry有効期限(`max_entry_delay`)、最大保有時間(`max_holding_time`)、曖昧足の優先順位(`same_candle_priority`)、コスト(`spread` / `slippage` / `commission`)。すなわち本書の Entry / Close / 約定規則の可変パラメータはすべて execution_profile に属する。
- **解決規則(現行実装)**: `resolve_execution_profile()` は、呼び出し側から `(symbol, timeframe)` → execution_profile の `overrides` 辞書が渡された場合にマッピングを解決し、該当がなければ既定プロファイルへフォールバックする。通常CLIは各profile値をCLI引数から単一のdefault profileとして構築し、全symbol/timeframeへ適用する。設定ファイルまたはCLIからsymbol/timeframe別マッピングをロードする通常運用経路は未実装である。いずれの経路でも **Signal の分類フィールド(`strategy_profile` を含む)は解決キーに使わない。**
- **記録**: 適用した execution_profile 名と、その時点のパラメータ実額(コスト等)を仮想ポジションレコードへ記録し、後から設定を変えても既存レコードの再現性が壊れないようにする。
- **将来拡張(参考)**: Signal生成戦略ごとに執行規則を変えたくなった場合、マッピングキーへ `strategy_profile` を加える拡張が考えられる。ただしその場合は「分類フィールドは判断に用いない」という Paper Signal 側の責務定義の更新が必要になるため、本設計では採用せず、採否は後続Issueで改めて判断する。

このほかに、判定用のローソク足を Chart DB(`market-watch/data/chart_data.sqlite3` の `candles`)から **読み取り専用** で参照する。使用するのは Issue #41 と同じ規律に従う **確定足のみ**(`T + timeframe <= 評価時刻` を満たす足。`market-watch/freshness.py` の共有ルールを再利用)。未確定足で Entry / Close を判定することはない。

## 保持・生成する情報(仮想ポジション)

Paper Execution が保持する中心データは **仮想ポジション(paper position)** である。概念モデルは以下(物理スキーマ・列型は後続のデータモデルIssueで確定する)。

| フィールド | 説明 |
|---|---|
| `position_id` | 仮想ポジションの一意ID |
| `signal_id` | 元 Paper Signal への参照(UNIQUE。1 Signal = 高々1ポジション) |
| `symbol` / `timeframe` / `side` | Signal から引き継ぐ |
| `strategy_profile` | Signal から**記録用**に引き継ぐ(Signal生成戦略の識別子。執行判断には使わない) |
| `execution_profile` | 適用した実行規則プロファイル名(Paper Execution 所有。前節) |
| `state` | `pending` / `open` / `closed` / `expired` / `cancelled`(下記) |
| `entry_price` | 仮想Entry約定価格(コスト調整後)。`pending` 以前は NULL |
| `entry_time_utc` | Entry足の open timestamp(UTC ISO-8601 `...Z`。時刻規約は Paper Signal と共通) |
| `stop_loss_price` / `take_profit_price` | Entry時に確定する仮想SL / TP水準。実注文ではなく判定用のマーカー |
| `max_hold_deadline_utc` | 最大保有時間による強制Close期限(Entry時に確定) |
| `close_price` / `close_time_utc` | 仮想Close約定価格(コスト調整後)と時刻。未Closeは NULL |
| `terminal_reason` | 終端状態に至った理由。**終端状態では必ず値を持ち、非終端状態では NULL**。取り得る値は state ごとに固定する(下記) |
| `pnl` | 決済済みのみ。`side` に応じた符号付き価格差(コスト込み)。未決済は NULL(0扱いで捏造しない — Backtest summary と同方針) |
| `spread` / `slippage` / `commission` | この約定に適用したコスト実額(再現性のため適用値を記録) |
| `created_at_utc` / `updated_at_utc` | 監査用 |

状態(`state`)はライフサイクル、`terminal_reason` は終端に至った理由であり、両者を混同しない。「Close」という語は仮想決済(`closed`)にのみ使い、Entryせず終了した `expired` / `cancelled` の理由を「close_reason」と呼ぶ名称の不整合を避けるため、終端理由は **`terminal_reason` に統一**する。state と terminal_reason の許容組み合わせは以下に固定する。

| state | 許容する `terminal_reason` |
|---|---|
| `closed` | `take_profit` / `stop_loss` / `max_holding_time` |
| `expired` | `entry_timeout` |
| `cancelled` | `invalid_input`(入力不整合。`status` が `"paper_only"` でない、必須項目の欠損等)/ `manual`(運用上の取消)/ `position_limit`(同時ポジション制御による取り込み拒否。Issue #85 で追加。下記「同時ポジション制御」) |
| `pending` / `open`(非終端) | NULL |

`terminal_reason` の語彙は上表で閉じた集合とする。値を追加する場合は本設計文書の更新を伴う(実装側で自由な文字列を書き込まない)。

Backtest の `result_status` 語彙との対応は「closed + `take_profit` ≒ win」「closed + `stop_loss` ≒ loss」「closed + `max_holding_time` ≒ expired(Backtest語彙)」となるが、Paper Execution 側では **勝敗ラベルではなく事実(理由)を保存**し、win/loss は集計時に導出する。

## 状態一覧と状態遷移

### 状態候補の整理

| 状態 | 意味 | 終端 |
|---|---|---|
| `pending` | Signal を受理済みで、仮想Entryの約定待ち | |
| `open` | 仮想Entry約定済み。Close判定の対象 | |
| `closed` | SL / TP / 最大保有時間のいずれかで仮想決済済み | ✅ |
| `expired` | Entryできないまま有効期限切れ(ポジションを持たずに終了。pnl なし) | ✅ |
| `cancelled` | Entry前に取消(入力不整合の検出、Signal の status 異常等。pnl なし) | ✅ |

終端状態はいずれも、その状態に至った理由を `terminal_reason` に保持する(前節の state × terminal_reason 対応表)。

- `closed` / `expired` / `cancelled` は終端状態であり、**終端状態からの遷移は存在しない**(再openしない)。
- 「openのまま期限切れ」は `expired` ではなく **`closed` + `terminal_reason = max_holding_time`** とする。`expired` は「一度もEntryしなかった」場合に限定し、ポジションを持ったか否かを state だけで判別できるようにする。
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
       (終端)                 (終端)           (終端)
        いずれの終端状態も terminal_reason を保持する
```

## Entry価格の決定規則

1. **Entry足**: `target_candle_utc + timeframe`(= Signal根拠足のclose時刻)以降で **最初に存在する確定足** を Entry足とする。Signal根拠足自身では Entry しない(根拠足のclose価格で入る Backtest の既知の lookahead 制約を、Paper Execution では最初から採らない)。
2. **Entry価格(raw)**: Entry足の **open 価格**。成行相当であり、指値・逆指値のEntryは本設計では扱わない。
3. **Entry価格(有効)**: raw open に対し、不利方向へ spread + slippage を加算した値(long: `open + spread + slippage`、short: `open - spread - slippage`。`backtest/execution_model.py` と同じ固定・不利方向モデル)。
4. **Entry有効期限**: Entry足の open 時刻が `target_candle_utc + timeframe` から `max_entry_delay`(既定: **timeframe × 3本分**。execution_profile 単位で調整可能なパラメータとする)を超える場合、Entryせず `expired`(`terminal_reason = entry_timeout`)とする。週末・休場明けの持ち越しEntry(金曜のSignalで月曜openに入る等)は既定では行わない。
5. **SL / TP水準の確定**: Entry約定と同時に、有効Entry価格を基準として execution_profile が定義するルール(初期実装は `exit_policy.py` の `fixed_rr` と同じ `fallback_stop_pct` × `risk_reward` 方式)で `stop_loss_price` / `take_profit_price` を確定し、以後変更しない(トレーリングは非スコープ)。`max_hold_deadline_utc` も同時に確定する。

## Close規則(SL / TP / 最大保有時間)

Close判定は **Entry足以降の確定足を時系列順に1本ずつ** 評価する(Entryは open 時点の約定なので、Entry足自身の high / low も判定対象に含む。Signal根拠足は含まない)。

各確定足について、以下の順で判定する。**いずれかのステップでCloseが成立した時点で `closed` へ遷移し、当該足の評価をそこで終了する(後続ステップへは進まない)。**

1. **gap判定(足の open)**: 足の open が既に SL / TP を超えて始まっていれば、下記「gap open時の約定規則」が定める価格と `terminal_reason` で `closed` へ遷移し、**その時点で当該足の評価を終了する**(同一足の intrabar 判定・最大保有時間判定は行わない)
2. **intrabar判定(足の high / low)**: **gap判定でCloseしなかった場合のみ**行う
   - long: `low <= stop_loss_price` → SL到達、`high >= take_profit_price` → TP到達
   - short: `high >= stop_loss_price` → SL到達、`low <= take_profit_price` → TP到達
3. **最大保有時間判定**: **intrabar判定でもCloseしなかった場合のみ**行う。その足のclose時刻が `max_hold_deadline_utc` に達していれば、その足の **close価格** で `terminal_reason = max_holding_time` としてClose

Close価格にも Entry と同じ不利方向コスト(long のClose: `価格 - spread - slippage`、short のClose: `価格 + spread + slippage`)を適用する。pnl は「有効Close価格 − 有効Entry価格」(short は符号反転)から commission を差し引いた値とする。

同一足で SL / TP と最大保有時間の両方が該当する場合は **価格到達(SL / TP)を優先** する(保有時間はその足のclose時刻で初めて満了するのに対し、価格到達は足の途中で発生しているため)。

### 同一足でSLとTPの両方へ到達した場合の優先順位

1本の確定足の high / low が SL と TP の両方に届いた場合、足内の到達順序は OHLC からは復元できない。この曖昧足の扱いは:

- 設定 `same_candle_priority`(`stop_loss` / `take_profit`)で明示的に決める。**既定は `stop_loss`(保守側 = 成績を過大評価しない)**。
- ハードコードせず execution_profile の設定として持ち、記録に残す。`paper-signal/exit_policy.py` の `same_candle_priority`(既定 `stop_loss`)と同じ語彙・同じ既定値を用い、Backtest との突合を可能にする。

### gap open時のEntry / SL / TP約定規則

窓開け(前足closeと当足openの乖離、週明け・休場明けを含む)に対する規則。方針は一貫して **「不利なgapは実額どおり受け、有利なgapは計上しない(保守側)」**。

| ケース | 約定規則 |
|---|---|
| Entry(成行相当) | gapの有無にかかわらず Entry足の open で約定する(gap自体による補正はしない。コストは通常どおり不利方向へ加算)。ただし Entry有効期限を超えた場合は `expired`(前述) |
| 足の open が既に SL を超えて始まった | SL価格ではなく **その足の open 価格** で約定する(gapによる不利スリッページを隠さない)。`terminal_reason = stop_loss` |
| 足の open が既に TP を超えて始まった | **TP価格** で約定する(gapによる有利な上振れは計上しない)。`terminal_reason = take_profit` |

open は単一価格のため「openがSLとTPを同時に超える」ことは起こり得ない(SL < Entry < TP の関係が保たれる限り)。gapでSLまたはTPが成立した場合は、上表の価格と `terminal_reason` で `closed` へ遷移し、**その時点で当該足の評価を終了する**(同一足の high / low による intrabar 判定は行わない)。**gap非該当の場合のみ** intrabar 判定へ進み、intrabar でもCloseしなかった場合のみ最大保有時間判定へ進む(前節の判定順)。

## spread / slippage / 手数料の責務境界

| コスト | 責務 | 規則 |
|---|---|---|
| spread | Paper Execution | 約定ごとに不利方向へ加算する固定の絶対価格差(pips換算しない)。`backtest/execution_model.py` の `fixed_spread_slippage` と同一の意味論 |
| slippage | Paper Execution | 同上。spread と独立に設定 |
| 手数料(commission) | Paper Execution | v1 では **既定 0.0 の固定額** としてフィールドのみ確保する(pnl から減算)。ロット・約定金額比例の手数料はロット計算が非スコープのため導入しない |

- **Paper Signal はコストに一切関与しない**(Signal レコードにコスト情報を持たせない)。コスト反映後の約定価格・pnl を持つのは Paper Execution だけである。
- 設定値は execution_profile 側に置き、適用した実額を仮想ポジションレコードへ記録する(後から設定を変えても既存レコードの再現性が壊れないようにする)。
- Backtest 側の `--spread` / `--slippage` は Backtest run のシミュレーション用パラメータであり、Paper Execution の設定とは独立に管理する(同じ値を与えれば同じ調整式で比較できる、という関係に留める)。

## 冪等性(再実行・重複Signal)

Paper Signal 側は既に `(strategy_profile, symbol, timeframe, target_candle_utc)` の UNIQUE 制約で確定足単位の冪等性を持つ(Issue #41)。Paper Execution はその上に **`signal_id` 単位の冪等性** を重ねる。

- 仮想ポジションは `signal_id` に対して UNIQUE とする(1 Signal = 高々1ポジション)。取り込み時に既存ポジションがあれば、その state にかかわらず新規生成せず skip する(`INSERT ... ON CONFLICT DO NOTHING` 相当)。
- 判定エンジンの再実行は **決定論的** であること: 入力(Signalレコード + 確定足)が同じなら、何度実行しても同じ状態遷移・同じ約定価格・同じ pnl に到達する。乱数・現在時刻依存の判定を持たない(現在時刻は「どこまでの足が確定しているか」の判定にのみ使う)。
- 各ポジションは「どの確定足まで評価済みか」の watermark を持ち、再実行・プロセス再起動時は未評価の確定足からのみ評価を再開する。評価済みの足を再評価しても結果が変わらないこと(冪等)を実装時のテスト観点とする。
- 終端状態(`closed` / `expired` / `cancelled`)のポジションは再実行で変更されない。

## 同時ポジション制御(Issue #85)

### 背景と現行挙動の明文化

Issue #85 時点までの Paper Execution は **Signal 単位の完全独立ポジション**であった: 重複防止は `signal_id` UNIQUE のみで、同一 `symbol` / `timeframe` / `side` の Signal が連続すると、それぞれ独立の pending / open position が際限なく積み上がる(実運用確認では USDJPY 1h short が 8 件同時 open)。これは「Signal 1件あたりの素の成績」の検証としては成立するが、Paper Execution を**現在進行の仮想執行・仮想口座評価**として使う運用では、集計結果を単一口座の成績として解釈できず、将来の Live Execution 設計へそのまま転用すると過剰な集中ポジションにつながる。

### 採用方針

**`(symbol, timeframe, side)` スコープごとの同時 active 上限(既定 1 件)** を Paper Execution の Signal 取り込み時ゲートとして実装する。

| 項目 | 決定 | 理由 |
|---|---|---|
| position scope | `(symbol, timeframe, side)` | Signal 生成と足評価の単位が `(symbol, timeframe)` であり、方向は独立の仮説であるため side まで含めてスコープとする |
| 計上対象 | **pending + open**(非終端 = active) | pending も約定待ちの建玉予約であり、放置すると同時 open に転化するため双方を対象にする |
| terminal の扱い | `closed` / `expired` / `cancelled` は**一切計上しない** | 終端 position が新規 Signal を阻害しない(受け入れ条件) |
| 同一方向 Signal | active 数が上限未満なら受理、上限到達なら拒否 | 積み上がり問題(Issue #85 の主訴)への直接の対処 |
| 反対方向 Signal | **ブロックしない**(自 side のスコープで独立に判定) | 反対方向は独立の仮説であり、抑制すると Signal 検証情報を失う。既存 close(netting)は実口座のネッティング仕様への踏み込みになるため非採用 |
| 上限到達 Signal の扱い | terminal な `cancelled` position(`terminal_reason = position_limit`)として**記録**し、cancelled 通知 outbox 行も同一トランザクションで作成 | 「invalid は黙って skip せず cancelled で記録する」既存原則(Issue #49)と同型。記録された `signal_id` 行が拒否自体の冪等性も担保する |
| 設定 | execution_profile の `max_active_positions_per_side`(既定 `1`、`None` = 無制限)。CLI は `--max-active-positions-per-side`(`0` = 無制限) | 執行規則の可変パラメータは execution_profile に置く既存方針に従い、新しい設定体系を作らない |

**既定挙動の変更(意図的)**: 既定を無制限(従来挙動)ではなく 1 件とする。Issue #85 の運用上の問題(同方向の際限ない積み上がり)は既定のままで解消すべきであり、「Signal 単位の独立検証」はフィルタなしの Backtest run と、`max_active_positions_per_side` の引き上げ / `None`(CLI `0`)でいつでも再現できるため。

### 検討した代替案と非採用理由

1. **Signal 単位で完全独立(現状維持)**: 積み上がり問題が解消せず、仮想口座評価として集計を解釈できないため非採用。無制限設定(`None`)として挙動自体は残す。
2. **`(symbol, timeframe)` 単位で side 無差別に 1 件**: 反対方向 Signal をブロックまたは既存 close する必要が生じる。前者は独立仮説の検証情報を失い、後者は実口座の netting / hedging 仕様(非スコープ)へ踏み込むため非採用。
3. **上限到達 Signal を記録なしで skip**: 「受理しなかった事実」が残らず、後から容量が空いたときに同一 Signal が cycle タイミング依存で pending になり得て決定論・冪等性が壊れるため非採用。terminal `cancelled` / `position_limit` として即時記録する(entry deadline のある Signal を遅延受理しても意味論が壊れる点も同様)。

### 判定・並行性・冪等性の保証

- 取り込み判定は store の `create_position_capacity_guarded` が行う: **`signal_id` 既存チェック → スコープ内 active 件数チェック → INSERT(pending または cancelled/position_limit + cancelled outbox 行)を単一の `BEGIN IMMEDIATE` トランザクション**で実行する。SQLite の single-writer により並行 runner は直列化され、「チェックしてから INSERT」の間に別 runner が INSERT する TOCTOU 競合で上限を突破することはできない。`signal_id` UNIQUE と outbox の `event_key` PRIMARY KEY が第二の防壁として残る。
- 上限到達で記録された `cancelled` / `position_limit` は terminal であり、後で容量が空いても遡って再受理しない(`signal_id` 行が存在するため duplicate skip)。同一 Signal 再取込み・同一 cycle 再実行・多重起動のいずれでも、position 追加ゼロ・通知追加ゼロで結果は不変。
- `max_active_positions_per_side` は **snapshot しない唯一の profile パラメータ**である: これは「他 position の現在状態を踏まえて新規 Signal を受理するか」という取り込み時ゲートであり、個々の position の判定規則ではない。既存 position の Entry / Close 評価には一切影響しない。
- dry-run は snapshot DB の active 件数 + 同一 cycle 内で受理予定の would-be pending を加算して同じ判定を投影し、書き込み・通知・migration を一切行わない(完全無副作用)。

### migration(schema v4)

`cancelled` の `terminal_reason` CHECK 閉集合に `position_limit` が加わるため、`virtual_positions` テーブルの CHECK 制約が変わる = **schema v4**。v3 以前の DB は既存の transactional table rebuild(CREATE → 検証コピー → DROP → RENAME、1 トランザクション・失敗時全 rollback)で移行する。v3 の全行は v4 の CHECK(上位集合)を満たすため行はそのまま保持される。dry-run は v3 DB を `legacy`(migration required)として読み込みを拒否し、DB へは触れない(既存契約どおり)。

## cooldown / re-entry / Market State との責務境界

Backtest の `--no-reentry` / `--cooldown-minutes` / `--allowed-market-states`(Issue #38 / #39)に相当する**戦略レベルの採用抑制**は、引き続き **Paper Execution の責務としない**。

- どの Signal を生成するか(Market State / freshness / confidence によるゲート)は Paper Signal Generator の責務、cooldown・re-entry 抑制・ポートフォリオ横断のリスク制約は将来の Portfolio / Risk 層(または Signal 採用層)の責務として切り出す。
- Issue #85 で Paper Execution が持つことになったのは、**自レイヤーの評価単位(仮想口座としての解釈可能性)を守るための取り込み時の同時建玉上限のみ**である(前節)。これは Signal の品質・戦略の優劣に基づく採用判断ではなく、Signal 内容を一切評価しない容量ゲートであるため、上記の責務分離とは矛盾しない。
- 「Signal 1件あたりの素の成績」を評価したい場合は、フィルタなしの Backtest run、または `max_active_positions_per_side` を `None`(CLI `0`)にした Paper Execution run を用いる。
- Market State は Signal 生成側の入力であり、Paper Execution はEntry / Close判定に参照しない(記録用の引き継ぎのみ将来検討)。

## 実発注・Broker API との分離(明記)

- Paper Execution は **実発注・自動売買・Broker API 連携(OANDA / XM / MT5 等)・ロット計算・レバレッジ計算を一切含まない**。生成するのは paper_only の仮想レコードのみである。
- `stop_loss_price` / `take_profit_price` は判定用のマーカーであり、broker への stop / limit 注文ではない(`exit_policy.py` と同じ「observation, not execution」の境界)。
- Live Execution は完全に別のレイヤーであり、Paper Execution のコード・データ・設定を実注文経路に転用しない。Live Execution の基盤は `broker/` + `execution/` に実装済み(定期実行 runner は未実装・実口座での運用は未解禁)だが、この分離不変条件は変わらない: `paper-execution/` のソースコード上に broker API の import・発注呼び出しが存在しないことは `execution/tests/test_package_imports.py` が回帰として固定済みである。

## 他レイヤーとの入出力境界(まとめ)

| 相手 | 方向 | 内容 |
|---|---|---|
| Paper Signal | 入力 | 保存済み `paper_signals` レコードの読み取りのみ(Generator は呼ばない、Signal DB へは書かない) |
| Chart DB(`candles`) | 入力 | 確定足の読み取りのみ(収集・書き込みは `market-watch/` の責務) |
| 自レイヤーのDB | 出力 | 仮想ポジションレコード(独立した SQLite。コンポーネントごとに独立DBという既存方針に従う。物理設計は後続Issue) |
| Market Bias / Market State | なし | 執行判定に参照しない(Signal 生成側の入力) |
| Backtest | なし(語彙共有のみ) | 直接のデータ連携はしない。約定規則・状態語彙を揃えて結果を突合可能に保つ |
| Discord | 出力(Issue #83) | 確定済み状態遷移(open / closed / expired / cancelled)の通知のみ。Position 遷移と outbox enqueue を同一トランザクションで確定し、配送は commit 後(Transactional Outbox)。`paper_execution_notifier.py` が担当(下記「Discord通知(Issue #83: Transactional Outbox)」) |
| Broker API | なし | 恒久的に対象外 |

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

## 実装状況(後続 Issue #50 / #51)

本設計は `paper-execution/` に実装済み(Issue #50: データモデル・保存層、Issue #51: Open / Close 判定エンジン)。本設計との対応:

- `position_models.py` — 状態(`pending` / `open` / `closed` / `expired` / `cancelled`)と `state × terminal_reason` の閉集合をアプリ層で検証(Issue #85 で `cancelled` に `position_limit` を追加)。
- `position_store.py` — 独立SQLite。`state` 集合・`state × terminal_reason` 閉集合・`same_candle_priority` 語彙・`signal_id` UNIQUE をDBの CHECK / UNIQUE 制約で担保。終端遷移は全て CAS(`WHERE state=...`)で、終端からの再遷移を拒否。close と watermark 前進は同一 UPDATE(watermark CAS で競合防止)。legacy schema からの移行は `PRAGMA user_version` + **table rebuild**(CREATE→検証コピー→DROP→RENAME、1トランザクション・失敗時 rollback)で CHECK/UNIQUE/NOT NULL を実際に付与し、安全に変換できない legacy row は明示的に migration 失敗させる。
- `execution_profile.py` — 執行規則プロファイル。内部APIは呼び出し側から渡された `(symbol, timeframe)` overridesを解決し、`strategy_profile` は解決キーにしない。通常CLIは単一default profileを使用し、symbol/timeframe別マッピングの設定ファイル/CLIロードは未実装。`size` 含む適用値を position へスナップショットし、後からの設定変更が既存 position の判定を変えないことを保証。`max_active_positions_per_side`(Issue #85)のみ取り込み時ゲートとして live profile で評価し、スナップショットしない(上記「同時ポジション制御」)。
- `execution_engine.py` — 純粋判定。Entry = 根拠足の次の確定足の open、Entry期限超過 / 週末持ち越しは `expired`(`entry_timeout`)、Close は gap → intrabar → `max_hold_deadline_utc` の順、曖昧足は position 保存の `same_candle_priority`。損益は `gross_pnl`(コスト前, raw価格)、`net_pnl`(entry/exit コスト各1回 + commission を控除)、`r_multiple`(net / (初期リスク×size)、初期リスク≤0 は None)を算出。`pnl` は `net_pnl` の後方互換エイリアス。
- `execution_runner.py` / `paper_execution_bot.py` — runner / 常駐ループ。Chart DB は **strict read**(`load_candles_range_strict`)で読み、DB破損・candles table欠損・OperationalError を空データ扱いせず cycle failure として計上し、該当 position は無変更のままにする(期限切れへ誤遷移しない)。失敗を握り潰さず件数を数え、`--once` は失敗時に非0終了。dry-run は完全無副作用(読取エラーも成功扱いしない)。

## Discord通知(Issue #83: Transactional Outbox)

Paper Execution の状態遷移を Discord へ通知する。**Paper Signal 通知は「仮説の発生」、Paper Execution 通知は「仮説が時間進行の中で仮想約定・仮想決済された結果」**を伝える。両者は責務・Webhook設定とも分離する。

方式は **Transactional Outbox**。Position 状態遷移と通知イベントの enqueue を **同一 SQLite connection・同一トランザクション**で確定し、Discord への HTTP 配送はその commit 後に別工程で行う。これにより「Position 遷移が commit されたのに通知 outbox 行が存在しない」状態を構造的に作らない。

### 責務分離

`execution_runner.py` は Discord Webhook を直接呼ばない。

```text
execution_runner
  -> position_store の *_with_notification API
       [ BEGIN IMMEDIATE
           UPDATE/INSERT virtual_positions
           INSERT paper_execution_notifications   ← 同一COMMIT
         COMMIT ]
  -> paper_execution_notifier.deliver_outbox   (commit後)
       - claim/lease(position_store.claim_notifications: atomic)
       - 本文生成(format_event_message: 純関数・None安全・Event ID付き)
       - payload生成(build_discord_payload)
       - HTTP配送(deliver_payload: timeout必須・1呼び出し1試行)
       - settle(finish_notification_delivery: success/failed/dead_letter)
  -> Discord Webhook
```

### 対象イベント(閉集合)

| event_type | 遷移 | atomic API | 本文の最低要素 |
|---|---|---|---|
| `open` | `pending -> open` | `transition_to_open_with_notification` | Paper Execution / 仮想約定であること、Event ID、symbol / timeframe / side / signal_id / position_id / entry_time_utc / entry_price / stop_loss_price / take_profit_price / max_hold_deadline_utc / execution_profile |
| `closed` | `open -> closed`(`stop_loss` / `take_profit` / `max_holding_time`) | `close_open_position_with_notification` | Paper Execution / 仮想損益であること、Event ID、symbol / timeframe / side / signal_id / position_id / terminal_reason / entry_time_utc / close_time_utc / entry_price / close_price / gross_pnl / net_pnl / r_multiple |
| `expired` | `pending -> expired` | `transition_pending_terminal_with_notification` | Event ID、symbol / timeframe / side / signal_id / position_id / state / terminal_reason / 仮想処理であること |
| `cancelled` | invalid signal による `cancelled` | `create_cancelled_position_with_notification` | 同上 |

`pending` 作成時点の通知はスコープ外(outbox 行を作らない)。表示できない値(NULL等)は例外にせず `-` 表記で安全にレンダリングする。

### Position遷移とenqueueのatomicity

各 atomic API は1つの `BEGIN IMMEDIATE` トランザクション内で position の UPDATE/INSERT と outbox 行の INSERT を実行し、`COMMIT`(失敗時 `ROLLBACK`)する。

- Position 変更だけ commit され通知行が無い状態を作らない
- 通知行 INSERT に失敗した場合は Position 変更も rollback する
- CAS 競合(`WHERE state=...` が0行)時は例外を送出し、通知行を作らずトランザクション全体を rollback する
- outbox INSERT は `ON CONFLICT(event_key) DO NOTHING`。同一遷移の再実行(CAS が本来防ぐ)や旧スキーマの残存行があっても重複 outbox 行や配送状態の破壊を起こさない
- `SIGKILL 相当(commit後・HTTP前)`でプロセスが落ちても、outbox 行は既にディスク上にあるため、次 cycle の `deliver_outbox` が検出・配送する

### 配送claim / lease(並行runner対策)

`deliver_outbox` は `list_undelivered_notifications` の SELECT-then-send を使わず、`claim_notifications` で **atomic claim** する。

- 1つの `BEGIN IMMEDIATE` トランザクション(SQLite は単一writer)で候補 SELECT と各行の `processing` への UPDATE を一括 commit する。2つの runner が同じ行を同時に claim できない = 同時配送しない
- claim 対象: `pending` / `failed`(attempt cap 未満)+ lease 期限切れの `processing`(claimed_until_utc <= now)。`success` / `dead_letter` は claim しない
- claim 時に `claimed_by`(worker id = host:pid)と `claimed_until_utc`(now + `DEFAULT_LEASE_SECONDS` = 600秒)を書く
- claim 中の worker が停止しても、lease 期限を過ぎれば別 worker が再 claim できる(crash 回収)。stale worker が後から settle しようとしても、`(delivery_status='processing', claimed_by=worker)` guard により `NotificationClaimError` で拒否され、新 owner の状態を壊さない
- 1 cycle の配送件数は `DELIVERY_BATCH_LIMIT` = 20 件(oldest-first)。滞留は cycle をまたいで順次 drain する

### at-least-once配送(Event ID・重複の可能性)

Discord への HTTP POST と DB の `success` 記録は **atomic にできない**。したがって外部 Discord への配送は **exactly-once ではなく at-least-once** である。

- **outbox 上の成功済み(`success`)イベントは再送しない**(runner 再起動・再実行後も)
- ただし **HTTP POST 成功後・`success` 記録前**にプロセスが落ちる/DB 書き込みが失敗すると、その行は `processing` のまま残り lease 期限後に再配送される → **同一 Event ID の Discord メッセージが重複し得る**
- 「outbox 成功済みは再送しない」ことと「Discord で重複しない」ことは**別物**である。後者は保証しない
- 各通知本文に `Event ID: <position_id:event_type>`(outbox の `event_key` と一致)を必ず含め、重複が起きても利用者が同一イベントと識別できるようにする
- `success` 記録失敗は明示的に ERROR ログへ出し(「DUPLICATE ... at-least-once」)、`notification_failures` に計上する

### 通知履歴テーブル(outbox schema)

Position DB(schema v3)の `paper_execution_notifications` テーブル。

- `event_key` **PRIMARY KEY**(= UNIQUE + NOT NULL)= `position_id:event_type`(決定論的)
- 列: `event_key` / `position_id` / `event_type`(CHECK)/ `delivery_status`(CHECK)/ `attempt_count` / `last_error` / `claimed_by` / `claimed_until_utc` / `notified_at_utc` / `created_at_utc` / `updated_at_utc`
- `delivery_status` ∈ `pending` / `processing` / `success` / `failed` / `dead_letter`
- index: `(delivery_status, created_at_utc)`

### failure分類

| 種別 | 例 | 計上先 | `--once` |
|---|---|---|---|
| data-plane / outbox永続化 | Position+outbox atomic tx 失敗、outbox schema 不正、claim DB 操作失敗、outbox 行作成失敗、settle の DB 失敗、position 再読込失敗 | `CycleResult.failures` | 非0終了 |
| delivery | timeout / connection error / HTTP 429 / HTTP 5xx / success記録失敗 / lease期限切れ回収 | `CycleResult.notification_failures` | 0終了(自動再送) |

outbox 永続化障害は「通知保証そのものが成立していない」ため通常の `failures` に含め `--once` を非0にする。純粋な配送障害は position が整合したままなので分離し、自動再送で回復させる。`success` 記録失敗は重複リスクがあるため必ずログに出す。

### dead-letter(継続監視)

- 失敗が `MAX_DELIVERY_ATTEMPTS` = 5 回に達した行は `dead_letter` へ遷移する(SELECT 対象外にして静かに消さない)
- **上限到達済み `failed` 行の正規化**: claim トランザクションの冒頭で `delivery_status='failed' AND attempt_count >= max_attempts` の行を `dead_letter` へ UPDATE する。claim 条件は `attempt_count < max_attempts` のため、これが無いと「claim 不能・dead-letter 監視対象外・自動再送不能」で永久に取り残される行が生じる。**LIVE の `max_attempts` 設定に追従**するため、(a) v2 prototype から `failed` のまま移行した行(v2 に `dead_letter` は無い)、(b) 既存 v3 の取り残し、(c) `max_attempts` を後から下げた場合、いずれも次 cycle の claim で reconcile される。migration 時に固定値で焼き込まないのは設定値依存を避けるため
- `CycleResult.dead_letter_notifications` に未解消 dead-letter 件数を gauge として載せる。**同一 cycle 内で新たに dead_letter 化した分も即時反映**する(配送後に再集計してから返す)。0でなければ warning ログを出す
- 復旧は `position_store.requeue_notification(event_key)`(attempt_count=0, `pending` へリセット)。確認 SQL・手順は [Operations](../operations/operations.md) を参照

### Webhook未設定時

- outbox 行は atomic transition により**通常どおり作成**する
- URL 未設定時は **claim しない = attempt_count を消費しない**。滞留件数を warning ログに出す
- URL 設定後、滞留分を `DELIVERY_BATCH_LIMIT` 件/cycle で順次配送する
- 現行方針は**滞留通知を無期限保持**する(自動削除・保持期限なし)。運用上不要になった行は手動削除する

### 設定・dry-run

- Webhook URL は `paper-execution/.env` の `PAPER_EXECUTION_DISCORD_WEBHOOK_URL`(プロセス環境変数優先)。Paper Signal の `DISCORD_WEBHOOK_URL` とは意図的に別変数(責務分離)
- Paper Signal DB / Chart DB は読み取り専用のまま。Broker API・実注文・売買推奨は一切実装しない
- `--dry-run` は完全無副作用: Discord送信なし・Position DB 更新なし・outbox 作成/migration/更新なし・ファイル作成なし。送信予定本文は `[DRY_RUN] would notify ...` ログで確認できる
- **WAL 対応**: dry-run の schema probe と position 読み取りは、実 DB へ SQLite 接続を一切張らない。DB 本体と `-wal` / `-shm` sidecar を一時ディレクトリへコピーし、**snapshot 側**を読む。通常の `sqlite3.connect(path)` は WAL DB を checkpoint して本体を書き換え `-wal` を消し得るため、read-only URI ではなく snapshot 方式を採る。dry-run 前後で DB 本体・`-wal`・`-shm` の内容・size・mtime・hash・schema・user_version が完全不変であることをテストで実ファイルの hash/mtime まで検証する
- **接続の明示 close(Windows)**: read helper(`_read_one` / `_read_many`)は `contextlib.closing()` で SQLite connection を必ず close する。sqlite3 の connection context manager は transaction を終えるだけで connection を close しないため、Windows では snapshot DB のファイルハンドルが残り `TemporaryDirectory.cleanup()` が `WinError 32` で失敗する。close を保証することで dry-run 後の snapshot 削除が全 OS で成功する(Windows CI job で検証)

### migration経路(v3 / v4)

`position_store.py` の既存契約(`PRAGMA user_version` + 実スキーマ検証)に統合する。

- **schema 検証は列名だけでなく実制約を検証**する: `PRAGMA table_info` の `pk`/`notnull`、`sqlite_master.sql` の CHECK 文字列、`PRAGMA index_list`/`index_info` を確認し、加えて write 経路では SAVEPOINT 内の **behavioral probe**(不正 event_type / 不正 status / NULL position_id / 重複 event_key の INSERT が実際に拒否されること)を rollback 付きで実行する。制約欠損テーブルは `user_version` を偽装しても `current` として扱わない
- `v1`(outbox テーブル無し)→ `v3`: additive(既存 `virtual_positions` を rebuild せず、1トランザクションで outbox テーブル作成 + index + `user_version=3`)
- `v2 prototype`(claim 列無し・旧 status 集合の outbox テーブル)→ `v3`: outbox テーブルの **transactional rebuild**(CREATE→検証コピー→DROP→RENAME)。既存 notification 行(delivery_status / attempt_count / last_error 等)を保持し、新列は NULL 埋め。変換不能な行(不正 event_type 等)は明示的に migration 失敗
- legacy positions rebuild 経路でも同一トランザクション内で outbox テーブルを整える
- `v3` → `v4`(Issue #85): `cancelled` の terminal_reason CHECK に `position_limit` が加わるため、`virtual_positions` の **transactional rebuild** で移行する(v3 の全行は v4 CHECK の上位集合を満たすため行は保持)。v3 以前の DB は positions rebuild 経路に乗る(v1→v3 の「positions を rebuild しない additive」経路は、v4 で positions の CHECK 自体が変わったため pre-v4 DB には適用されない)
- いずれも失敗時は全 rollback(user_version・既存行とも不変)
