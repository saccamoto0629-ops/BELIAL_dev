# Paper Execution

`paper-execution/` は、[Paper Signal](paper_signal.md) の `paper_only` 仮説を入力に、ローソク足の時間進行に沿って **仮想ポジション**を Open / Close し、仮想損益を記録する執行(Execution)層である(Issue #50 / #51)。

**実発注・自動売買・Broker API 連携は一切行わない。** Paper Execution は観測・シミュレーションのみであり、[Live Execution](../product/concept.md) とは独立した別レイヤーである。過去データを遡及検証する [Backtest](../testing/test_specification.md) とも別レイヤーだが、約定価格・gap・優先順位のルールは Backtest Engine と一致させている。

## 位置づけと責務分離

```text
Market Bias / Chart Snapshot ─ Paper Signal(paper-signal/, 仮説の記録)
                                        │
                                        └─ Paper Execution(paper-execution/, 本章)
                                               ├─ 仮想ポジションの Open / Close 判定
                                               ├─ 仮想損益(gross / net / R multiple)
                                               └─ 永続化(paper_positions.sqlite3)
```

責務は次の3ファイルへ厳密に分離している(Market Watch / Alert / Paper Signal のロジックは持ち込まず、Execution ロジックもそれらへ逆流させない):

| ファイル | 責務 | 依存 |
|---|---|---|
| `position_models.py` | 仮想ポジションのデータモデル(TypedDict)と status / side / close_reason の語彙(定数) | なし |
| `position_store.py` | SQLite 保存層(Repository)。migration・CRUD・Close・履歴・冪等制約・transaction境界 | `position_models` |
| `execution_engine.py` | **純粋な** Open / Close 判定と損益計算(DB・Discord 非依存) | `backtest/execution_model`, `position_models` |
| `execution_runner.py` | runner / CLI。Paper Signal 読取(READ-ONLY)→ candle 読取(READ-ONLY)→ engine → store | 上記＋`chart_collector`, `paper_signal_store` |

## データモデル(#50)

`virtual_positions` テーブル(独立DB `paper-execution/data/paper_positions.sqlite3`。既存 `paper_signals.sqlite3` には一切触れないため後方互換を自動的に保証)。

- 関連: `paper_signal_id`(Paper Signal への TEXT 参照。別DBファイルのため cross-file FK は張らない)
- 分類: `strategy_profile` / `symbol` / `timeframe` / `side`
- entry: `entry_price` / `size` / `stop_loss` / `take_profit` / `opened_at`
- exit: `status` / `exit_price` / `closed_at` / `close_reason`
- 損益: `gross_pnl` / `net_pnl` / `spread_cost` / `slippage_cost` / `fee_cost` / `r_multiple`
- bookkeeping: `entry_signal_time` / `created_at` / `updated_at`

`position_events` テーブルは Open / Close の状態変更履歴を追記専用で保持し、ポジション行と同一 transaction で書かれる。

status / side / close_reason は文字列直書きせず、`position_models` の定数(`STATUS_*` / `SIDE_*` / `CLOSE_REASON_*`)で制約する。close_reason は Backtest の `exit_policy.py` と同一語彙(`stop_loss` / `take_profit` / `time_expired`)。

## 冪等性(DB制約 + Repository の二層)

- **Open**: `paper_signal_id` の UNIQUE index が「1 Paper Signal = 1 仮想ポジション」をDBレベルで保証。runner は `get_position_by_signal` で事前確認(Repositoryレベル)し、再実行を静かにスキップする。
- **Close**: `close_position` が `WHERE status='open'` でUPDATEするため二重Closeは0行更新となり `PositionAlreadyClosedError`。履歴も `UNIQUE(position_id, event_type)` で二重 `closed` を拒否。
- 作成・Close はポジション行と履歴イベントを1 transaction で書き、途中失敗時は全ロールバック。

## Open / Close 判定(#51)

Open: Paper Signal の側(long/short)と確定足(`target_candle_utc`)の終値から、Execution Model 補正後の entry を約定し、そこを基準に `stop_loss` / `take_profit`(固定%×R)を決める。side が long/short 以外なら Open しない。

Close(bars を entry の次足から walk。順序は Backtest `squeeze_breakout.evaluate_squeeze_exit` と一致):

1. **gap Stop** — long `open<=stop` / short `open>=stop`: 生の `open` で約定
2. **gap TP** — long `open>=target` / short `open<=target`: **固定 target** で約定(利益側へ改善しない)
3. intrabar SL / TP(high / low)
4. **同一足で SL と TP の両到達 → Stop 優先**(保守的・決定論的)
5. time exit — `time_exit_bars` 目の足まで未決着なら終値でClose(ただし 1〜4 を評価した後)

## spread / slippage / fee の二重適用防止

- entry: Execution Model で **1回**補正。stop / target は補正後 entry を基準に算出。
- exit: SL / time exit / gap-stop は close 側で **1回**補正。**TP は固定 target のまま補正しない**(保存直前に共通の再補正処理を挟まない = 「target固定・stop固定が再補正されない」)。
- `gross_pnl` は補正済み約定価格から算出(spread/slippage は価格に1回だけ内包)。`spread_cost` / `slippage_cost` は透明性のための記録で **net から再控除しない**。`net_pnl = gross_pnl - fee_cost`(fee のみ価格非内包の1回控除)。

## dry-run

`--dry-run` 時は DB schema作成・record作成/更新・state更新・log ファイル作成・Discord 通知を一切行わない。判定内容はログ出力のみ(読取専用パスはDBを作成しない)。

## 実行

```bash
python paper_execution_bot.py --once            # 1 サイクル
python paper_execution_bot.py --once --dry-run  # 副作用なし
```
