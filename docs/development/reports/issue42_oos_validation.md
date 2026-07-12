# Issue #42 — XAUUSD Market State フィルタの頑健性検証（時間分割 retrospective）

[P1][Backtest] XAUUSD の Market State フィルタをアウトオブサンプル検証する（#42）の実行手順書兼結果記録。

本番 Linux (`/opt/berial`) 上で実行する。`.env` / AWS / Discord / S3 には一切触れない。新規コードは追加せず、既存 CLI（`python -m backtest run` / `summary` と #40 で追加済みの `backtest.backfill_symbol_bias`）のみを使う。

---

## 0. 位置づけ（重要）

- **これは「真のアウトオブサンプル」ではない。** Live のデータ収集開始が約1年前（~2025-07）のため、PF 1.24 を導いた in-sample 期間（2025-07-20〜2026-07-10 のフル1年）の外側に、実データのある未使用期間が存在しない。
- したがって本検証は**既存1年データの時間分割による頑健性確認（retrospective validation）**として実施する。**検証前に固定した** Candidate 条件を、検証側（held-out）で再最適化せずに評価するのが主目的。
- **条件選定バイアスは残る。** Candidate 条件（trend_up,squeeze,volatile）は**フル1年（in-sample 全体）の結果を見て選ばれている**ため、検証側もその選定の影響を完全には免れない。学習側の Run T は「条件の根拠（学習側の独立した証拠）」ではなく、**既に固定された条件が学習側でも同様の傾向を示していたかを確認する参考結果**にすぎない。
- **真の OOS は別途、2026-07-11 以降の未見データを蓄積してから後日評価する**（本手順書の対象外・フォローアップ）。

---

## 1. 検証前に固定した条件（実行前ロック — 検証中に変更・再最適化しない）

| 項目 | 値 |
|---|---|
| Symbol | XAUUSD（CLI 指定は `XAU_USD`） |
| Timeframe | `1h` |
| Market Bias | 銘柄別（`--time-series-bias` + #40 backfill 済み per-symbol bias） |
| Candidate の許可 Market State | `trend_up,squeeze,volatile`（固定） |
| no-reentry | 有効（`--no-reentry`） |
| cooldown | 60分（`--cooldown-minutes 60`） |
| Exit Policy | `fixed_rr`（明示ピン） |
| Execution Model | Baseline と Candidate で**同一**（本手順は既定 = spread 0 / slippage 0。PF1.24 参照値と比較可能にするため） |
| warmup-bars | 既定 75 |
| confidence-threshold | 既定 0.40 |

Baseline と Candidate の**唯一の差分は `--allowed-market-states` の有無**。他の全フラグは完全に一致させる。

---

## 2. 期間定義（明示）

| 区分 | 期間 | 用途 |
|---|---|---|
| 学習側（in-sample） | 2025-07-20 〜 2026-03-30 | 参考: 既に固定済みの Candidate 条件が学習側でも同傾向（by_market_state で squeeze/volatile/trend_up が優位）を示したかの確認。条件選定はここでは行わない（既にフル1年の結果を見て選定済み） |
| 検証側（held-out） | 2026-03-31 〜 2026-07-10 | Baseline vs Candidate の本比較。ここでは一切の再最適化をしない |

> warmup 注記: 学習側 run は最初期候補に 75 本の warmup が要る。実データ先頭が ~2025-07-20 の場合、`--from 2025-08-01` から開始し先頭数本の欠落を許容する（by_market_state 傾向の確認が目的なので影響なし）。検証側 run は前段に数ヶ月分あり warmup は自動的に充足。

---

## 3. 前提: 銘柄別 Bias の backfill（#40 の既存ツール、3 run 開始前に1回だけ）

`--time-series-bias` が GLOBAL フォールバックせず XAUUSD/1h を解決するには、Market Bias DB に per-symbol レコードが必要（#40 の検証時に実行済みだが、再現性のため再実行して最新化する）。**以下は 3 run（T / V-B / V-C）を始める前に1回だけ実行する**（各 run ごとに再実行する必要はない）。

```bash
cd /opt/berial

# XAUUSD の銘柄別 bias 履歴を Market Bias DB へ backfill（既定で対象銘柄の既存行を消して再構築。GLOBAL は不可侵）
python -m backtest.backfill_symbol_bias --symbols XAU_USD --timeframe 1h
```

backfill 自体は1回でよいが、**実際に効いているかの確認は各 run の出力で行う**: 各 run の summary の **`Bias scope:` 行が `XAU_USD/1h -> XAUUSD/1h`（`(GLOBAL fallback)` が付かない）** ことを毎回確認する。付いていたら backfill が効いておらず #40（銘柄別 bias）が no-op になっている＝やり直し。

---

## 4. 実行コマンド

### Run T — 学習側 Baseline（固定条件の学習側傾向の参考確認）

```bash
python -m backtest run \
  --symbol XAU_USD --timeframe 1h \
  --from 2025-08-01T00:00:00 --to 2026-03-30T23:59:59 \
  --time-series-bias \
  --no-reentry --cooldown-minutes 60 \
  --exit-policy fixed_rr \
  --run-name XAU_train_baseline_2025H2_2026Q1
```

目的（参考のみ）: `by_market_state` で squeeze / volatile / trend_up が range / trend_down より優位（PF>1 or PnL>0）という傾向が学習側でも見られたかを確認する。**注意: Candidate 条件（trend_up,squeeze,volatile）は既にフル1年（in-sample 全体）の結果を見て選定済みであり、Run T はその選定の裏付け（学習側の独立した証拠）ではない。条件選定バイアスは残る**ため、Run T は参考結果として扱い、採否判定の主根拠にはしない（判定は §7 の検証側比較で行う）。

### Run V-B — 検証側 Baseline（Market State フィルタなし）

```bash
python -m backtest run \
  --symbol XAU_USD --timeframe 1h \
  --from 2026-03-31T00:00:00 --to 2026-07-10T23:59:59 \
  --time-series-bias \
  --no-reentry --cooldown-minutes 60 \
  --exit-policy fixed_rr \
  --run-name XAU_OOS_baseline_2026Q2Q3
```

### Run V-C — 検証側 Candidate（trend_up,squeeze,volatile のみ許可）

```bash
python -m backtest run \
  --symbol XAU_USD --timeframe 1h \
  --from 2026-03-31T00:00:00 --to 2026-07-10T23:59:59 \
  --time-series-bias \
  --no-reentry --cooldown-minutes 60 \
  --exit-policy fixed_rr \
  --allowed-market-states trend_up,squeeze,volatile \
  --run-name XAU_OOS_candidate_2026Q2Q3
```

> V-B と V-C の差分は `--allowed-market-states trend_up,squeeze,volatile` の1行のみ。期間・Execution Model・その他フラグは同一。

### （任意）構造化アーカイブ

各 run の末尾に出る `Run ID` を使い、指標を JSON/Markdown で保存できる（stdout だけでも下表は埋められる）。

```bash
python -m backtest summary --run-id <RUN_ID> --format json --output reports/issue42/<run_name>.json
```

---

## 5. 実行時の確認ポイント（各 run の stdout）

- `Bias mode:  time_series`
- `Bias scope: XAU_USD/1h -> XAUUSD/1h`（**`(GLOBAL fallback)` が付いていないこと**）
- `Allowed states:` — V-B は `all`、V-C は `trend_up, squeeze, volatile`
- `Execution model:` — 3 run すべてで同一（`spread=0.0, slippage=0.0`）
- `Suppressed: market-state=N` — V-C でのみ N>0（フィルタが効いている証拠）、V-B は 0
- `=== Backtest Signal Summary ===` の `by_bias` / `by_market_state` を丸ごと控える

---

## 6. 結果記録（実行後に記入）

### 6.1 学習側 Baseline（Run T）— 固定条件の学習側傾向の参考確認

by_market_state（PnL / PF の符号だけでも可）:

| Market State | signals | win_rate | total_pnl | PF |
|---|---|---|---|---|
| trend_up | | | | |
| squeeze | | | | |
| volatile | | | | |
| range | | | | |
| trend_down | | | | |

参考傾向: squeeze/volatile/trend_up が優位か → ( 是 / 否 )　※参考のみ。条件選定バイアスが残るため採否判定の主根拠にはしない

### 6.2 検証側 Baseline vs Candidate（本比較）

| 指標 | Baseline (V-B) | Candidate (V-C) |
|---|---|---|
| Total Signals | | |
| Win / Loss | | |
| Win Rate | | |
| Total PnL | | |
| Average PnL | | |
| **Profit Factor** | | |
| **Max Drawdown** | | |
| Suppressed (market-state) | 0 | |

by_bias（Bull / Bear 各行）:

| | Baseline Bull | Baseline Bear | Candidate Bull | Candidate Bear |
|---|---|---|---|---|
| signals | | | | |
| win_rate | | | | |
| total_pnl | | | | |

by_market_state（Candidate は許可 3 state のみ残る想定）:

| Market State | Baseline signals / PF | Candidate signals / PF |
|---|---|---|
| trend_up | | |
| squeeze | | |
| volatile | | |
| range | | （フィルタで除外＝0 想定） |
| trend_down | | （フィルタで除外＝0 想定） |

---

## 7. 判定基準（**結果を見る前に固定**）

PF だけでなく取引数と Drawdown を必ず併せて見る（検証側は約3.3ヶ月・no-reentry・cooldown60 のため取引数が少なくノイズになりやすい）。

- **採用**: 検証側で `Candidate PF > Baseline PF` かつ `Candidate PF > 1.0` かつ Max DD が Baseline に対し著しく悪化しない かつ Candidate の決済取引数が意味のある水準（目安 ≥ 30）。→ 銘柄別コンフィグ反映の次 Issue を定義。
- **保留**: 方向性は良い（PF 改善 or PF>1）が、取引数が少なすぎる／DD が悪化／改善が僅少で誤差範囲。→ 真の OOS（2026-07-11 以降）でのデータ蓄積後に再評価。
- **棄却**: 検証側で `Candidate PF ≤ 1.0` または `Candidate PF < Baseline PF`（フィルタが held-out 期間ではエッジを毀損）。→ XAUUSD の Market State フィルタは頑健でないと結論。

### 最終判定: （採用 / 保留 / 棄却）を記入

理由:

> **結論の前提（再掲）:** 本検証は**真の OOS ではなく、既存1年データの時間分割による retrospective validation** である。Candidate 条件（trend_up,squeeze,volatile）はフル1年の結果を見て選定されているため、**条件選定バイアスは残る**。検証側で良好な結果が出ても「真に汎化した」とは断定できず、あくまで頑健性の一材料。確定的な結論には 2026-07-11 以降の forward データによる真の OOS 検証（§8）が必要。

---

## 8. 採用時のフォローアップ Issue 候補（採用の場合のみ定義）

- 銘柄別コンフィグ（XAUUSD → 銘柄別 bias + `allowed_market_states=trend_up,squeeze,volatile`）を Live / paper-signal のエントリー制御へ反映する Issue。
- 真の OOS 検証: 2026-07-11 以降の forward データを蓄積し、本手順書と同一固定条件で再評価する Issue。

## スコープ外（本 Issue では実施しない）

- 検証結果を見た後の条件再調整、Entry/Exit ルール変更、spread/slippage/cooldown の再最適化。
- USDJPY / SPX の追加検証。
