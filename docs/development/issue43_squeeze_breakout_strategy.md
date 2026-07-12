# Issue #43 — XAUUSD / 1h squeeze breakout 戦略仕様

## 現行実装の制約

- 対象は CLI symbol `XAU_USD`、DB / internal canonical symbol `XAUUSD` / `1h` の paper-only Backtest とする。入力時に既存normalizeを通してcanonicalへ統一する。Live、Broker、注文執行は対象外。
- Bollinger Bands は終値の20本単純移動平均、pandas標本標準偏差（`Series.std()` の既定 `ddof=1`）、±2標準偏差である。bandwidth は `(upper - lower) / middle`。
- Market State の `squeeze` は、当該確定足を含む直近20本の有効bandwidthに対する下位10%以下である。20本未満なら成立しない。価格がband中央30〜70%にあるかはconfidenceだけに影響し、state成立の必須条件ではない。squeezeは他stateより先に判定される。
- Bollinger alert の squeeze は直近120本・下位10%であり、Market Stateの20本定義とは異なる。本戦略はIssue #42と同じ **Market Stateの20本定義** を準備状態に使い、alert定義を混在させない。
- alert側には `expansion_start`（前足squeeze、現足非squeeze、bandwidthが前足の1.30倍以上）があるが、現行BacktestのEntry生成には使われていない。
- ATRは現行の共通indicator / Backtestに実装されていない。Bollinger幅が一部でATR代替として扱われるだけなので、ATR stopを採ると新指標・warm-up・テスト契約が増える。
- MACDは EMA12−EMA26、signalはMACDのEMA9、histogramは差分。`Bullish` / `Bearish` はクロスイベントではなく現在状態である。
- 現行Paper Signalは Market Bias（Bull/Bear、confidence閾値）と同方向のMACD状態、Fresh、正のbias weightで候補を作る。`signal_type=trend_follow`、`trigger_type=macd_state_alignment` であり、squeeze breakoutを表していない。
- Backtestは各時点までの足だけでindicator / Market Stateを計算し、歴史再生では各足のclose時点を評価時刻とする。Entry raw priceはシグナル確定足のclose、仮想約定はlongで `close + spread + slippage`、shortで `close - spread - slippage`。Exit判定は必ず次足から始まる。
- 現行Exit Policyは `fixed_rr`、`time_based_close`、`ma_break_invalidation` の択一。`fixed_rr`はsignalに明示されたstop/TPを利用できるが、runnerは現在それらを渡さず既定0.3% stop / 2Rへfallbackする。fixed RRとtime exitの「先に来た方」は未実装。
- fixed RRで同一足中にSL/TP双方へ到達した場合は設定依存。比較では保守的な `stop_loss` 優先に固定する。
- `--no-reentry` は全銘柄・全方向で同時1ポジション、`--cooldown-minutes` もglobal。抑制順は Market State filter → no-reentry → cooldown。既存機能は「同一squeeze setupを消費したか」を管理しない。
- Issue #42は真のOOSではなく、1年全体を見た候補選定後の時間分割 retrospective validationである。squeezeは学習側 PF 1.7533 / PnL +209.64、検証側 PF 1.2160〜1.3132 / PnL +47.48〜+82.52で一貫してプラスだった。一方、trend_up / trend_down / Bias方向は期間で反転し、rangeは検証側最大損失（-170.02）、volatileは検証側0件で評価不能だった。したがって方向stateやBias/MACDを本仕様へ追加しない。

## 戦略候補比較

| 項目 | A. BB終値ブレイク | B. squeeze解除＋bandwidth拡大 | C. 直近N本高安ブレイク |
|---|---|---|---|
| 準備状態 | 連続するMarket State `squeeze`を1 episodeとする | 同左 | 同左 |
| Trigger | episode終了足（最初の非squeeze確定足）のcloseが現足BB upper以上 / lower以下 | episode終了足でbandwidthが前足の1.30倍以上、かつBB終値ブレイク | episode終了足closeが直前N本（現足除外）の最高値超 / 最安値割れ |
| Entry条件 | Trigger成立。Bias / MACD不使用 | 全Triggerの同時成立 | Trigger成立。Bias / MACD不使用 |
| Entry価格 | Trigger足closeに既存spread/slippageを不利約定 | 同左 | 同左 |
| Stop | long=Trigger足low、short=Trigger足high | 同左 | 同左 |
| Take Profit | 2R | 2R | 2R |
| Time Exit | Entry後24本目のclose（SL/TP未成立時） | 同左 | 同左 |
| 無効化条件 | Entry前: Trigger足確定時に条件不成立。Entry後: SL/TP/time exitのみ | 拡大率またはbreak不成立 | N本高安break不成立 |
| 再エントリー | 同一episodeから最大1Entry。episode終了足だけを評価し、次episode開始まで不可 | 同左 | 同左 |
| 必要指標 | 既存BB / Market State | 既存BB / Market State / bandwidth | 既存OHLC / Market State、N本rolling高安 |
| 現行コードで実装可能か | 最小差分。strategy候補生成、stop/TP引渡し、複合time exitが必要 | 可能だがalertの120本定義を流用せずBacktest側で整合が必要 | 可能だが新しいNとrolling境界を追加 |
| future leakageリスク | 低。BBは現足まで、判定は確定後 | 低。ただし前足/現足参照を固定する必要 | 中。rolling窓から現足を必ず除外（`shift(1)`相当） |
| 想定される弱点 | bandを僅かに終値で越すfakeout、Trigger足が大きいとstop幅が過大、小さいと過小 | 条件過多で遅れ、急なbreakを取り逃す | N依存、BB squeezeと価格rangeの二重定義、gap的breakに弱い |
| 過学習リスク | 低。追加閾値は2R / 24本のみ | 中。1.30拡大率が追加され組合せ最適化を誘発 | 中〜高。Nの探索が主要な自由度になる |

## 推奨戦略

**A. squeeze episode解除足のBollinger終値ブレイク**を唯一の初回実装対象として推奨する。

理由は、Issue #42で確認したsqueezeを準備状態としてのみ使い、方向Bias / MACD / ATR / N本lookback / bandwidth拡大率を追加せず、既存BBと確定足終値だけでTriggerを再現できるためである。squeeze単独ではEntryせず、episode解除と同時の終値breakを必須とする。

旧案は `state[t-1]=squeeze` だけを要求し、`state[t]` が引き続きsqueezeでもBB終値breakがあればEntryし得た。確定案は `state[t-1]=squeeze`、`state[t]!=squeeze`、BB終値breakの3条件を同時に要求する。これにより連続squeeze中の複数候補を排除し、episodeの終了足をbreakout評価対象として一意にできる。Bとの差は、Aが解除幅の1.30倍条件を要求しない点である。

### squeeze episode定義

- 開始: 直前確定足が非squeeze（または履歴先頭）で、現在の確定足が初めて `squeeze` になった時点。
- 継続: 以後、確定足のMarket Stateが連続して `squeeze` である間。同じepisode内の各squeeze足ではEntry判定しない。
- 終了: squeeze連続列の直後に現れる、最初の `state != squeeze` の確定足 `t`。この足を **episode終了足 / breakout評価対象足** とする。
- 1つのepisodeにつき評価対象は終了足 `t` の1本だけ。ここでEntryが成立してもしなくてもepisodeは終了・消費済みとなる。
- 終了足の次以降にbreakしても旧episodeからはEntryしない。新しいepisodeは、再び非squeezeからsqueezeへ遷移した時だけ開始する。

事前固定値:

- BB: 20本、±2標準偏差（現行値）
- squeeze: Market State既定20本、下位10%（現行値）
- TP: 2R
- time exit: Entry後24本（XAUUSD 1hの24時間相当。市場休場による欠損は「実在する次の24本」で数える）
- same-candle priority: stop loss
- no-reentry: enabled
- cooldown: 0分（新しいsqueeze必須規則がより強く、60分cooldownを重ねない）

これらはIssue #42の損益を見て最適化した値ではない。最初の比較では固定し、感度確認を採用判断と混ぜない。

擬似コード:

```text
episode_active = false

for each confirmed 1h bar t in chronological order:
    indicators_t = calculate_indicators(candles through t only)
    state_t = classify_market_state(indicators_t)

    if position_is_open:
        manage_exit_on_bar(t)  # entry bar自身は対象外
        continue               # 保有中のsqueezeは次setupとしてarmしない

    if state_t == squeeze:
        if not episode_active:
            episode_active = true  # 新episode開始
        continue

    if not episode_active:
        continue

    # state_t != squeeze かつ episode_active:
    # このtは連続squeeze直後の最初の非squeeze足であり、唯一の評価対象
    assert state_at(t-1) == squeeze
    episode_active = false  # 成否にかかわらずepisode終了・消費

    long_trigger  = close[t] >= bb_upper[t]
    short_trigger = close[t] <= bb_lower[t]

    if long_trigger == short_trigger:  # 両方false、または異常データで両方true
        continue

    direction = long if long_trigger else short
    raw_entry = close[t]
    entry = execution_model.adjust_entry_price(raw_entry, direction)
    stop = low[t] if direction == long else high[t]
    risk = entry - stop if long else stop - entry

    stop_is_valid = (stop < entry) if long else (stop > entry)
    if not stop_is_valid or risk <= 0 or any required value is NaN/non-finite:
        continue

    target = entry + 2 * risk if long else entry - 2 * risk
    open paper position at entry with stop and target
    # t+1以降を時系列走査
    for each subsequent confirmed bar u:
        # 1. gap判定（不利なstop gapを先に処理）
        if long and open[u] <= stop: close at raw open[u]; break
        if short and open[u] >= stop: close at raw open[u]; break
        if long and open[u] >= target: close at raw target; break
        if short and open[u] <= target: close at raw target; break

        # 2. intrabar SL/TP判定
        stop_hit = low[u] <= stop if long else high[u] >= stop
        tp_hit   = high[u] >= target if long else low[u] <= target

        # 3. 同一足双方hitはSL優先
        if stop_hit and tp_hit: close at stop (conservative priority); break
        if stop_hit: close at stop; break
        if tp_hit: close at target; break

        # 4. 最後にtime exit（24本目でも上記を先に評価済み）
        if bars_elapsed_since_entry == 24: close at close[u]; break
```

## Entry仕様

1. 必要履歴（SMA75等を含む現行readiness契約）を満たした確定足だけを対象とする。
2. 連続squeeze episodeの終了足 `t`、すなわち `state[t-1]=squeeze` かつ `state[t]!=squeeze` となる最初の確定足だけを評価する。
3. 終了足 `t` の確定終値が、足 `t` までの終値で算出した `bb_upper[t]` 以上ならlong、`bb_lower[t]` 以下ならshort。ヒゲ到達はTriggerにしない。
4. Bias、trend_up/down、MACD状態・crossは条件にしない。
5. Entryは足 `t` のcloseをraw priceとし、既存Execution Modelで不利約定を加える。
6. 同一episodeの評価は終了足1本だけ。breakしなければepisodeは消費済みとして失効し、次のepisode開始を待つ。
7. `signal_type=squeeze_breakout`、`trigger_type=bollinger_close_break`、`strategy_profile=xauusd_1h_squeeze_breakout_v1` を新規定義する。既存 `trend_follow` / `macd_state_alignment` を流用しない。

## Exit仕様

- 初期Stop: longはTrigger足low、shortはTrigger足high。Entry確定時点で一意に算出する。
- Stop妥当性: longは必ず `stop < entry`、shortは必ず `stop > entry`。満たさない場合はEntryしない。
- risk: longは `entry - stop`、shortは `stop - entry`。`risk <= 0`、NaN、非有限はinvalidとする。
- TP: spread/slippageを含む **Execution Model適用後のEntry価格** から有利方向へ `2 * risk`。
- 判定開始: Entry足の次の確定足。
- 各Exit足の判定順は固定する: (1) open gap、(2) high/lowによるSL/TP、(3) 同一足SL/TP双方hitならSL優先、(4) 最後にtime exit。
- gap stopはlongで `open <= stop`、shortで `open >= stop` の場合にraw openで決済する。gap TPはlongで `open >= target`、shortで `open <= target` の場合に、保守的にraw targetで決済し有利な価格改善を加えない。stop gapをTP gapより先に評価する。
- SL/TPが未成立なら24本目の確定足closeでtime exit。time exit足でもgapとSL/TPを先に評価し、いずれも不成立の場合だけtime exitを適用する。
- close priceにも既存Execution Modelの不利なspread/slippage補正を適用する。

## 無効化・再エントリー

- Entry前の無効化: episode終了足で終値breakが成立しない、必要値不足、Stop妥当性違反、`risk <= 0`、既存no-reentryでポジション保有中。
- episodeはEntry成否にかかわらず終了足の評価1回で消費する。
- 同一episodeから最大1Entry。失敗後の反対方向entryも認めない。
- exit後は時間だけのcooldownを置かず、非squeeze→squeeze遷移で新episodeが開始するまで再Entry不可。
- globalな既存 `--no-reentry` を有効化する。将来複数銘柄を同時検証する場合、global制約による銘柄間干渉は別Issueで分離する。

## future leakage対策

- 足 `t` の判定には `candles[:t+1]` だけを渡し、形成中の足を含めない。
- `state[t-1]` は `t-1` までで算出済みの値を使い、全期間一括計算後の将来情報で再分類しない。
- BBのrolling窓は当該足まで。候補Cを比較実装する場合のみ、最高値/最安値窓を `t-N ... t-1` として現足を除外する。
- EntryはTrigger足closeなので、Entry足high/lowでexit判定しない。Exit walkは `t+1` から。
- Market Biasを使わない。比較上参照する場合も `evaluated_at <= bar close` の既存point-in-time lookup以外は禁止。
- warm-upはvalidation開始より前の足だけ。validation終了後の足をwarm-upへ混入させない。
- time exitはローソク足の本数で数え、未来の時刻や最終結果から欠損足を補間しない。

## Backtest比較案

初回実装・検証対象はAだけに固定する。Issue #42の結果を使った閾値再調整はしない。

1. Control: 現行Paper Signal baseline（Issue #42と同条件）を再現。
2. A: episode解除＋BB終値break（唯一の実装対象）。

B（1.30倍bandwidth拡大追加）とC（直近20本高安break）は将来の比較候補として仕様表に残すが、初回実装Issueには含めない。

共通条件は CLI `XAU_USD` / internal `XAUUSD` / 1h、同一期間、2R、24-bar time exit、SL優先、no-reentry、cooldown 0。spread/slippage値は戦略仕様では固定せず、各Backtestの **実行条件** として必ず明示し、run configへ保存する。比較指標は取引数、勝率、PF、total PnL、max drawdown、平均R、中央値R、MFE/MAE、time-exit率、long/short別、月別とする。

minimum trade countは次の値で事前固定する。

- 全期間の決済取引数: **50件以上**
- held-out期間の決済取引数: **30件以上**

いずれか一方でも未満なら、成績の採用・棄却判定を行わず **「サンプル不足で保留」** とする。PF、PnL、勝率その他の結果を閲覧した後に、この閾値を引き下げ、引き上げ、または別定義へ変更してはならない。run config / 検証報告には両期間の決済取引数と閾値判定を記録する。

評価は (a) Issue #42と同じ時間分割を設計確認用retrospective、(b) 2026-07-11以降の未使用forward期間を真のOOSとして分離する。採否はforward OOSを主根拠とし、1年データ上の小さな差は採用理由にしない。

## 未決事項

- 複合Exit（SL/TPと24本time exitの先着）は現行Exit Policyの択一構造にない。既存APIを壊さずfixed_rr evaluatorへ `max_hold_bars` を加えるか、composite policyを追加するかは実装設計で決める。

## 実装Issue案

**題名:** `[P1][Backtest] XAU_USD 1h squeeze episode breakout v1（案A）をpaper-onlyで実装する`

完了条件:

- 新しいpure strategy候補生成器が連続squeeze episodeを管理し、唯一の終了足で `state[t-1]=squeeze`、`state[t]!=squeeze`、BB終値breakを確定足だけで判定する。
- 新しい `signal_type` / `trigger_type` / `strategy_profile` をmodel・保存schema・summaryへ通し、MACD alignmentと混同しない。
- Trigger足low/highから妥当なstop、Execution Model適用後Entry基準の2R targetをEntry時に確定し、Exit Policyへ明示的に渡す。
- Entry足をExit walkから除外し、gap→SL/TP→同一足SL優先→24-bar time exitの順序を実装する。
- 同一episode最大1Entry、新episodeまで再armしないstate machineと既存no-reentryを実装する。
- 初回実装はAだけとし、B/Cの選択実装やパラメータ探索を含めない。
- point-in-time、形成中足除外、rolling境界、setup消費、反対方向再entry禁止、gap、同一足SL/TP、time exit境界のfixtureテストを追加する。
- Backtest run configへ全パラメータとExecution Modelを保存し、結果を再現可能にする。
- CLIは `XAU_USD`、normalize後のDB / internal canonicalは `XAUUSD` とする。spread/slippageを実行条件として保存する。
- minimum trade countを全期間の決済取引50件以上、held-out期間の決済取引30件以上に固定する。いずれか未満は「サンプル不足で保留」とし、結果閲覧後の閾値変更を禁止する。
- `.env`、AWS、Discord、S3、既存DB、Live/Execution層を変更しない。テストは一時DBのみを使う。
