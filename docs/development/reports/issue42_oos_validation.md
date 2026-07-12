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

Run ID: `13fd4cf5506147a781e76089096e0196` / window 2025-08-01〜2026-03-30 / signals=420, WR=0.3643, PF=**1.1218**, PnL=+434.68, MaxDD=551.69

by_market_state:

| Market State | signals | win_rate | total_pnl | PF |
|---|---|---|---|---|
| trend_up | 172 | 0.4012 | +400.07 | 1.2859 |
| squeeze | 39 | 0.4872 | +209.64 | **1.7533** |
| volatile | 5 | 0.4000 | +12.92 | 1.4084 |
| range | 66 | 0.3485 | +54.37 | 1.0987 |
| trend_down | 138 | 0.2899 | -242.32 | 0.8149 |

参考傾向: squeeze/volatile/trend_up が優位か → **是**（3 state とも PF>1、trend_down のみ明確に負）。ただし range も学習側では PF 1.10 と僅かにプラス、volatile は 5 件と極小サンプル。※参考のみ。条件選定バイアスが残るため採否判定の主根拠にはしない。

### 6.2 検証側 Baseline vs Candidate（本比較）

検証窓 2026-03-31〜2026-07-10。V-B Run ID `2890e6eb5bf24155b905447e2bc38b9e` / V-C Run ID `90867f178089487e80f7887abc56ae69`。

| 指標 | Baseline (V-B) | Candidate (V-C) |
|---|---|---|
| Total Signals | 202 | 83 |
| Win / Loss | 67 / 135 | 29 / 54 |
| Win Rate | 0.3317 | 0.3494 |
| Total PnL | -14.98 | +40.80 |
| Average PnL | -0.0742 | +0.4915 |
| **Profit Factor** | 0.9917 | **1.0549** |
| **Max Drawdown** | 304.67 | **199.08** |
| Suppressed (market-state) | 0 | 387 |

by_bias（Bull / Bear 各行）:

| | Baseline Bull | Baseline Bear | Candidate Bull | Candidate Bear |
|---|---|---|---|---|
| signals | 70 | 132 | 62 | 21 |
| win_rate | 0.2857 | 0.3561 | 0.3226 | 0.4286 |
| total_pnl | -132.34 | +117.36 | -30.72 | +71.52 |
| PF | 0.8062 | 1.1040 | 0.9468 | 1.4326 |

by_market_state:

| Market State | Baseline signals / PF / PnL | Candidate signals / PF / PnL |
|---|---|---|
| trend_up | 49 / 0.9687 / -14.13 | 51 / 0.9129 / **-41.73** |
| squeeze | 26 / 1.2160 / +47.48 | 32 / 1.3132 / **+82.52** |
| volatile | 0（検証窓に該当バーなし） | 0（検証窓に該当バーなし） |
| range | 28 / 0.4490 / **-170.02** | 除外（フィルタで 0） |
| trend_down | 99 / 1.1463 / **+121.69** | 除外（フィルタで 0） |

> 注: no-reentry は「常時1ポジション」制約のため、Candidate はフィルタで range/trend_down 候補を先に落とす（suppression 順: market_state→re-entry→cooldown）ことで、残る trend_up/squeeze により多くの約定枠が回る。よって Candidate の trend_up(51)/squeeze(32) は Baseline(49/26)より**増える**（単純な部分集合ではない）。

---

## 7. 判定基準（**結果を見る前に固定**）

PF だけでなく取引数と Drawdown を必ず併せて見る（検証側は約3.3ヶ月・no-reentry・cooldown60 のため取引数が少なくノイズになりやすい）。

- **採用**: 検証側で `Candidate PF > Baseline PF` かつ `Candidate PF > 1.0` かつ Max DD が Baseline に対し著しく悪化しない かつ Candidate の決済取引数が意味のある水準（目安 ≥ 30）。→ 銘柄別コンフィグ反映の次 Issue を定義。
- **保留**: 方向性は良い（PF 改善 or PF>1）が、取引数が少なすぎる／DD が悪化／改善が僅少で誤差範囲。→ 真の OOS（2026-07-11 以降）でのデータ蓄積後に再評価。
- **棄却**: 検証側で `Candidate PF ≤ 1.0` または `Candidate PF < Baseline PF`（フィルタが held-out 期間ではエッジを毀損）。→ XAUUSD の Market State フィルタは頑健でないと結論。

### 評価

**(A) 事前固定ルールの機械的判定 → 形式上は「採用」ライン到達。**
検証側で `Candidate PF 1.0549 > Baseline PF 0.9917` かつ `PF > 1.0` かつ Max DD が悪化しない（304.67 → 199.08 と**改善**）かつ決済取引数 83 ≥ 30。§7 の採用条件を4つとも満たす。

**(B) しかし by_market_state / by_bias を見ると、エッジは state レベルで汎化していない（規制レジームの反転）。**

- **方向性 state が符号反転:** 学習側で trend_up は最大の稼ぎ頭（PF 1.29 / +400）、trend_down は最大の負け（PF 0.81 / -242）。検証側では**逆転**し、trend_up が負け（PF 0.91〜0.97 / -14〜-42）、除外したはずの trend_down が勝ち（PF 1.15 / **+121.69**）。
- **バイアス方向も反転:** 学習側は Bull 優位（PF 1.30）・Bear 劣位（0.91）。検証側は Bear 優位（Candidate Bear PF 1.43 / +71.5）・Bull 劣位（0.95 / -30.7）。
- **Candidate のプラスは squeeze 単独が牽引:** 検証側 Candidate の内訳は squeeze +82.52 に対し trend_up は **-41.73（足を引っ張っている）**、volatile は検証窓に0件（検証不能）。つまり「trend_up,squeeze,volatile」の3点セットのうち、検証側で機能したのは squeeze だけ。
- **集計 PF が勝った主因は range 除外:** Baseline 最大の損失は range（-170.02）。Candidate はこれを除外して集計 PF を押し上げた。だが同時に検証側で勝っていた trend_down（+121.69）も捨てている。差し引き +40.80 は「squeeze の利益 ＋ range 回避」が「trend_down 放棄 ＋ trend_up の損」を上回った結果で、仮説（trend_up 系が効く）が当たったわけではない。

**(C) 頑健と言えるのは squeeze と「range を避ける」ことのみ。** squeeze は学習側 PF 1.75 / 検証側 PF 1.22〜1.31 と両期間で一貫してプラス。range は両期間で最弱〜負け。一方 trend_up / trend_down / bias 方向は非定常（期間で反転）で、直近3.3ヶ月・squeeze 26〜32件という小サンプルも相まって、3点セット条件を「汎化した」とは言えない。

### 推奨判定: **保留**（要ユーザー確定）

事前ルールの集計指標は「採用」に触れるが、それは range 除外による集計効果と squeeze 単独の寄与によるもので、**固定条件「trend_up,squeeze,volatile」そのものの汎化は by_market_state/by_bias の反転により否定された**。よって「この3点セットを Live コンフィグへそのまま採用」は時期尚早と判断し、**保留**を推奨する。

次アクション（保留時）:
1. 頑健な核（**squeeze 中心、range 除外**）へ条件を絞った再検証を別 Issue で行う（例: `allowed_market_states=squeeze` 単独、あるいは range/trend_down 除外の効果分離）。※本 Issue のスコープ外＝条件再調整のため、必ず別 Issue として切る。
2. **真の OOS**（2026-07-11 以降の forward データ蓄積後）で本手順書と同一固定条件で再評価する（§8）。
3. 現時点で trend_up,squeeze,volatile を Live へロールアウトしない。

### 最終判定: **保留**（trend_up,squeeze,volatile の3状態セットは採用保留）

理由:
- 今回は真の OOS ではなく retrospective validation であり、条件選定バイアスが残る。
- 集計指標（PF・PnL・DD・取引数）は事前固定ルールの採用基準を満たしたが、by_market_state / by_bias で成績が学習側と検証側で反転しており、3状態セットの頑健性は確認できなかった（検証側の黒字は squeeze 単独＋range 除外が牽引で、trend_up はむしろ損、volatile は0件で検証不能）。
- 現時点では Live ルールや銘柄別コンフィグへ反映しない。

> **結論の前提（再掲）:** 本検証は**真の OOS ではなく、既存1年データの時間分割による retrospective validation** である。Candidate 条件（trend_up,squeeze,volatile）はフル1年の結果を見て選定されているため、**条件選定バイアスは残る**。検証側で良好な結果が出ても「真に汎化した」とは断定できず、あくまで頑健性の一材料。確定的な結論には 2026-07-11 以降の forward データによる真の OOS 検証（§8）が必要。

---

## 8. フォローアップ Issue 候補（判定＝保留を受けて）

保留のため、trend_up,squeeze,volatile の Live / 銘柄別コンフィグ反映は**行わない**。代わりに以下を別 Issue として切る:

1. **squeeze 中心の最小構成の再検証**: 頑健と確認できた核（squeeze・range 除外）へ条件を絞って retrospective で再評価する。候補: `allowed_market_states=squeeze` 単独、および range/trend_down 除外の効果分離。※条件再調整のため本 Issue のスコープ外＝必ず別 Issue。
2. **真の OOS 検証**: 2026-07-11 以降の forward データが十分蓄積した時点で、本手順書と同一の固定条件（および 1. の絞り込み条件）で再評価する。
3. （参考）方向性 state / bias 方向が学習・検証で反転した非定常性は XAUUSD 固有の可能性があり、必要なら USDJPY / SPX でも同様の反転が起きるかは別途（本 Issue スコープ外）。

## スコープ外（本 Issue では実施しない）

- 検証結果を見た後の条件再調整、Entry/Exit ルール変更、spread/slippage/cooldown の再最適化。
- USDJPY / SPX の追加検証。
