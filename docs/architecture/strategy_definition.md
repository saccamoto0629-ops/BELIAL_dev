# Strategy 定義の外部化仕様(Issue #52)

## この文書の位置づけ

Issue #52 の **設計成果物**。Strategy ごとの可変設定を外部ファイルへ切り出すための
**ファイル形式・スキーマ・エラー方針・後方互換性方針**を確定する。後続の Issue #53
(Loader / Validation 実装)が判断を再発生させずに実装できる粒度まで明文化することを目的とする。

**この Issue の非スコープ(本文書では実装しない):**

- Loader / Validation / CLI 読込処理の実装(Issue #53)
- 既存 Strategy(`squeeze_breakout`)の外部定義への移行(後続 Issue)
- 条件式 DSL、指標計算、entry/exit の約定判定ロジックの外部化(恒久的に Python 側へ残す)
- 実注文 / Paper Execution / Live Execution

サンプル定義: [`examples/strategy_squeeze_breakout_v1.yaml`](examples/strategy_squeeze_breakout_v1.yaml)
(**Loader 未実装のため、どのコードからも読み込まれない参照用サンプル**)。

---

## 0. 責務境界:外部化するもの / Python に残すもの

外部ファイルに置くのは **「Strategy ごとに変更可能な宣言的設定」だけ**とする。
以下は **恒久的に Python 側へ残す**(外部化しない)。

| Python 側に残す責務 | 現行の所在 | 残す理由 |
|---|---|---|
| 指標計算(BB / MACD / SMA 等) | `market-watch/indicators.py` | Live と byte-identical を保つ共通実装。Strategy 固有ではない。 |
| Market State 分類 | `market-watch/market_state.py` | 同上。squeeze episode の定義は分類器が所有する。 |
| 条件式・Trigger 判定(episode state machine、BB 終値 break 判定) | `backtest/squeeze_breakout.py` | 条件式を文字列 DSL 化すると複雑性が急増する(Issue #52 背景)。ロジック本体は Python。 |
| entry/exit の約定判定(gap → SL/TP → 同一足 SL 優先 → time exit の順序、excursion 追跡) | `backtest/squeeze_breakout.py:evaluate_squeeze_exit()` | 判定順序は future-leakage 契約と一体。宣言化しない。 |
| stop / target の価格計算(Trigger 足 low/high、2R 計算) | `backtest/squeeze_breakout.py` | 「どの方法か」は宣言、実際の価格算出は Python。 |
| 仮想約定コスト適用(spread / slippage) | `backtest/execution_model.py` | **実行環境の設定**。後述 §9 の責務境界により Strategy には置かない。 |

外部化するのは、これらの Python ロジックが **読み取るパラメータ**(2R の `2.0`、time exit の `24`、
対象 symbol/timeframe、allowed market states 等)であって、ロジックそのものではない。

---

## 1. ファイル形式:YAML

**採用: YAML(1 Strategy = 1 ファイル)。**

| 観点 | 採用理由(YAML) | 不採用理由(JSON) |
|---|---|---|
| 可読性 | 人手で編集・レビューする宣言的設定に適する。ネストが浅く punctuation ノイズが少ない | 波括弧・カンマ・クォートが多く、人手編集で壊しやすい |
| コメント | `#` で各設定に採用理由・出典 Issue を併記できる(この種の設定では重要) | コメント不可。設定の意図を残せない |
| Issue 整合 | Issue #52 の「想定イメージ」が YAML | — |
| 既存実装との整合 | 内部変換後は JSON 相当の dict になり、`backtest_runs.config_json`(既に JSON)へそのまま入る | 設定ファイルとしては noisy |

**トレードオフ(明記):**

- `PyYAML` は現状 `requirements.txt` に無い。**Issue #53 で `pyyaml` を追加し、`yaml.safe_load()`
  (`load()` ではなく)で読む**こと。任意コード実行を避けるため `safe_load` 固定。
- YAML はファイル外周(人間が書く境界)でのみ使う。**読み込んだ直後に plain dict へ落とし、
  以降の内部表現・永続化(config_json)は JSON 相当**とする(§10)。YAML 固有機能
  (アンカー・タグ・複数ドキュメント)は使わない/受け付けない。

---

## 2. トップレベル・スキーマ

```yaml
schema_version: 1            # 必須 int
id: xauusd_1h_squeeze_breakout_v1   # 必須 str, 一意
name: "XAUUSD 1h Squeeze Breakout v1"   # 必須 str
description: "..."          # 任意 str

symbols: [XAU_USD]          # 必須 非空リスト
timeframes: [1h]            # 必須 非空リスト

allowed_market_states: null # 任意(handler 依存, §6)

entry:                      # 必須 object(§7)
  handler: squeeze_breakout
  params: { ... }
  reentry: new_setup
  cooldown_bars: 0

risk:                       # 必須 object(§8)
  stop_loss: { type: trigger_bar_extreme }
  take_profit: { type: r_multiple, r: 2.0 }

exit:                       # 必須 object(§8)
  max_holding_bars: 24
  same_candle_priority: stop_loss
```

### 必須項目 / default 一覧

| キー | 型 | 必須 | default | 説明 |
|---|---|---|---|---|
| `schema_version` | int | ✅ | — | サポート集合に含まれること(§3)。 |
| `id` | str | ✅ | — | `^[a-z0-9_]+$`。ロード集合内で一意(§3)。config 上の Strategy 同一性。 |
| `name` | str | ✅ | — | 人間可読ラベル。 |
| `description` | str | | `""` | 自由記述。 |
| `symbols` | list[str] | ✅ | — | 非空。CLI 表記(例 `XAU_USD`)。canonical 化は Python(§5)。 |
| `timeframes` | list[str] | ✅ | — | 非空。Chart DB の値と一致(例 `1h`)。 |
| `allowed_market_states` | list[str] \| null | | `null` | `null`=フィルタ無し。各要素 ∈ `KNOWN_MARKET_STATES`(§6)。 |
| `entry.handler` | str | ✅ | — | 登録済み handler キー(§7)。 |
| `entry.params` | object | | `{}` | handler 固有。値はスカラ / スカラ平坦リストのみ(ネスト・NaN 不可, §7)。意味は handler が検証。 |
| `entry.reentry` | enum | | `overlap` | `{overlap, single_position, new_setup}`(§7)。 |
| `entry.cooldown_bars` | int ≥ 0 | | `0` | 決済後 N 本抑制。0=無効(§7)。 |
| `risk.stop_loss.type` | enum | ✅ | — | `{trigger_bar_extreme, fixed_pct}`(§8)。 |
| `risk.stop_loss.pct` | float > 0 | 条件付 | — | `type=fixed_pct` のとき必須。 |
| `risk.take_profit.type` | enum | ✅ | — | `{r_multiple}`(§8)。 |
| `risk.take_profit.r` | float > 0 | 条件付 | — | `type=r_multiple` のとき必須。 |
| `exit.max_holding_bars` | int > 0 | ✅ | — | time exit の本数(§8)。 |
| `exit.same_candle_priority` | enum | | `stop_loss` | スキーマ enum は `{stop_loss, take_profit}` だが handler 契約で制限。squeeze は `stop_loss` のみ(§4.1, §8)。 |

> **`entry.params` を除き、既知オブジェクト配下の未知キーは拒否する**(fail-closed, §3)。
> フィールドの **値**が選択 handler の capability に反する場合も validation error(§4.1)。

---

## 3. schema_version・一意性・後方互換性

### schema_version

- 型は **int**(`1`, `2`, ...)。semver ではなく単調増加の整数のみ。
- Loader は **サポートする version の集合**(初期は `{1}`)を持つ。
- 集合外の値 → `StrategyDefinitionError`(§ エラー方針)。**未対応 version は黙って読まない**。

### 後方互換性方針

- **同一 schema_version 内は「追加のみ後方互換」**。新キーは **必ず optional + default 付き**で足す。
  既存ファイルは新フィールドを書かなくても従来どおり読める。
- **破壊的変更(必須キー追加 / 既存キーの意味変更 / 型変更 / default 変更)は schema_version を上げる。**
- Loader は複数 version を同時サポートしてよい。version を上げても、旧 version をサポート集合に
  残す限り旧ファイルは動く。サポートを外す場合はサポート集合から除外し、明示エラーにする。
- default は **Strategy 定義側で解決してから内部型へ渡す**(§10)。Python 側のロジックが
  「未指定 = ハードコード定数」に暗黙依存しない。

### id 一意性・重複

- `id` は **ロード対象の全 Strategy 定義集合内で一意**でなければならない。
- **重複 id を検出したらロード全体を失敗**させる(`StrategyDefinitionError`)。先勝ち/後勝ちの
  暗黙上書きはしない(どちらが有効か不定になり再現性を損なうため)。エラーメッセージには
  重複 id と、衝突した両ファイルパスを含める。

---

## 4. handler レジストリ(未登録 handler の扱い)

- `entry.handler` は Python 側の **handler レジストリ**(`{名前: 呼び出し可能}` の dict、Issue #53 で実装)の
  キーを指す。
- **未登録の handler 名 → `StrategyDefinitionError`**。エラーには要求 handler 名と登録済み一覧を含める。
- レジストリは **Python コードが所有**する。外部ファイルは「どの handler を使うか」を選ぶだけで、
  handler を定義・注入できない(コード実行の外部化を防ぐ)。
- `id` と `handler` は **別概念**:`id` は定義ファイルの同一性、`handler` はコードとの結合点。
  複数の定義(別 `id`)が同一 `handler` を params 違いで指してよい。

### 4.1 handler 対応フィールド契約(capability descriptor)

各 handler は Python 側に **capability descriptor**(対応フィールドと許容値の宣言)を持つ(Issue #53 で実装)。
Validator は定義を descriptor と突合し、**非対応フィールド、または対応するが許容しない値を検出したら
`StrategyDefinitionError`** とする。**「無視して読み飛ばす」ことはしない**(Loader が受理したのに実行時に
無視される「死に設定」を構造的に禁止する)。handler が「対応するが単一固定値のみ」のフィールドは、
**その固定値または省略のみ許容**し、他値はエラー。

現時点で唯一の実装済み handler は `squeeze_breakout`(現 `backtest/squeeze_breakout.py`)。
その capability を **確定契約**とする。`trend_follow` は将来 handler 化する際に確定する **参考列**
(現状は paper-signal 候補経路であり handler ではない)。

| フィールド | `squeeze_breakout`(確定) | `trend_follow`(参考・将来確定) |
|---|---|---|
| `entry.params.squeeze_state` | ✅ 対応(既定 `squeeze`) | ✕ 非対応 |
| `entry.params.require_episode_release` | ✅ 対応 | ✕ 非対応 |
| `entry.params.breakout` | ✅ 対応(`bollinger_close` のみ) | ✕ 非対応 |
| `allowed_market_states` | ✕ **非対応 → `null` 必須**(自前 episode state machine) | ✅ 対応 |
| `entry.reentry` | `new_setup` のみ(構造的に単一ポジション) | `overlap` / `single_position` |
| `entry.cooldown_bars` | `0` のみ | 任意(≥ 0) |
| `risk.stop_loss.type` | `trigger_bar_extreme` のみ | `fixed_pct` |
| `risk.take_profit.type` | `r_multiple` のみ | `r_multiple` |
| `exit.max_holding_bars` | ✅ 対応(int > 0) | ✅ 対応 |
| `exit.same_candle_priority` | **`stop_loss` のみ**(§8 参照) | `stop_loss` / `take_profit` |

> 例:`handler: squeeze_breakout` に対し `allowed_market_states: [trend_up]`、
> `same_candle_priority: take_profit`、`cooldown_bars: 60`、`reentry: overlap` はいずれも
> **validation error**。squeeze の descriptor が許容しないため(死に設定にならない)。

**受理段階の validation と実行挙動の一致**:descriptor が単一固定値のみ許容するフィールドは、
その許容値を **handler の実際の実行挙動と一致**させる(§5.2 カテゴリ③)。これにより「受理されたが
実行時に無視される」死に設定を、受理段階(§4.1)と実行段階(§5.2)の両方で排除する。

---

## 5. symbols / timeframes

- ともに **非空リスト**。`symbols` は CLI 表記(`XAU_USD`)で記述し、**DB / internal canonical へ正規化する
  責務は Python 側**(`runner._chart_symbol()` / `normalize_bias_symbol()` 相当)に残す。定義ファイルには
  canonical(`XAUUSD`)を書かない(表記ゆれの単一情報源を Python に一本化)。
- `timeframes` は Chart DB の `candles.timeframe` と完全一致する値(`15m` / `1h` / `4h`)。
- 意味論:**この定義が「適用可能」な symbol/timeframe の宣言**であり、runner を多重ループさせる指示ではない。
  runner は従来どおり 1 回の `--symbol`/`--timeframe` を取り、Loader/runner は
  「要求された symbol/timeframe がリストに含まれるか」を検証に使う(Issue #53)。含まれない組合せは
  使用時エラー。**複数 symbol の同時 run は別 Issue**(本 Issue のスコープ外)。

---

## 5.1 ロード単位 / runtime 配置 / CLI 接続 / 後方互換(#53 実装契約)

Loader / Validation の実装方式を左右するため、以下を **本 Issue で確定**する(§12 に残さない)。

### ロード単位

- **1 ファイル = 1 Strategy 定義**。1 ファイルに複数 Strategy を書く multi-document YAML は不可
  (§1 の「複数ドキュメント不可」と一致)。
- ディレクトリ指定時は **配下の `*.yaml` を全て読み、`id` を key とする registry** に載せる。
  ロードは登録段階で全ファイルを検証し、**1 件でも invalid、または `id` 重複があれば registry 構築
  全体を失敗**させる(部分ロードしない=再現性のため)。

### runtime 配置(既定)

- **既定ディレクトリ: `backtest/strategies/`**(リポジトリルート相対)。環境変数
  `STRATEGY_DEFINITIONS_DIR` で上書き可(既存 `*_DB_PATH` 群と同じ上書き idiom)。
- 拡張子は `.yaml`(`.yml` も許容)。`data/` のように `.gitignore` はしない(定義は成果物)。
- 本 Issue のサンプルは **参照用として `docs/architecture/examples/` に置く**(Loader 未実装で非読込)。
  #53 で実際にロードされる定義ファイルを `backtest/strategies/` へ置く(移行 Issue で squeeze を配置)。

### CLI 接続

- **単一ファイル指定とディレクトリ+id 選択の両方を用意**する(排他)。
  - `--strategy-file <PATH>`:1 ファイルを直接ロードして使う(アドホック検証用。ディレクトリ走査なし)。
  - `--strategy <ID>`:既定ディレクトリ(または `STRATEGY_DEFINITIONS_DIR`)を registry 化し、
    `id == <ID>` を選ぶ。未存在 id はエラー。
- 両者は排他。両方指定はエラー。

### ファイル未指定時の後方互換

- `--strategy-file` / `--strategy` の **いずれも未指定なら、外部定義機構は一切起動しない**。
  現行の `--strategy-profile`(enum、既定 `trend_follow`)の挙動を **完全維持**する。
  ディレクトリの走査も PyYAML の import も行わない。
- したがって外部 Strategy 定義は **完全な opt-in**。既存の全呼び出し・全テストは変更なしで従来どおり動く。
- `--strategy(-file)` と旧 `--strategy-profile` の同時指定はエラー(どちらが有効か曖昧にしない)。

---

## 5.2 外部定義の実行接続と実行契約(#53, 案A)

**#53 は「ロード + validation」で終わらず、選択された外部定義を実際の Backtest 実行へ接続する**
ところまでを責務に含む(案A)。これにより「Loader が受理したが実行結果に反映されない死に設定」を
**別レイヤー(実行時)でも**排除する。

> 前提:実行接続は **外部定義を明示選択したとき(`--strategy-file` / `--strategy`)だけ**起動する。
> 未指定時は現行 `--strategy-profile` 経路を完全維持(§5.1)。既存 module 定数
> (`squeeze_breakout.RISK_REWARD` 等)は **非選択パスの既定値**として残り、選択パスの固定値契約
> (下記カテゴリ③)の **参照実装**にもなる。既存 Strategy の default 移行(module 定数の撤去)は
> 引き続き後続 移行 Issue の範囲。

### 受理フィールドの実行反映(4 カテゴリ)

受理された全フィールドは必ず次の①〜④のいずれかに属す。**「受理されるが実行のどこにも反映されない」
フィールドは存在しない**。

**① 実行パラメータ(可変。handler へ値を渡す)**

現行 `evaluate_squeeze_breakout_signals()` は既に `risk_reward` / `time_exit_bars` を引数に取る
(`_run_squeeze_breakout` が現状は渡さず module 定数へ fallback しているだけ)。#53 は選択時にこれらへ
外部定義の値を渡す。

| 外部フィールド | 実行接続先(squeeze_breakout) |
|---|---|
| `risk.take_profit.r` | `evaluate_squeeze_breakout_signals(risk_reward=<r>)` |
| `exit.max_holding_bars` | `evaluate_squeeze_breakout_signals(time_exit_bars=<max_holding_bars>)` |

**② 適合検証(実行対象の妥当性)**

| 外部フィールド | 実行時の使われ方 |
|---|---|
| `symbols` | 要求 `--symbol` がリストに含まれること。不一致は使用エラー(`EXIT_USAGE_ERROR`) |
| `timeframes` | 要求 `--timeframe` がリストに含まれること。不一致は使用エラー |

**③ 固定値契約(handler 実装挙動と一致必須。許容値=実装値のみ)**

descriptor が単一固定値のみ許容し(§4.1)、**その許容値は handler が実際に実装している挙動と必ず一致**
させる。よって実装と異なる値は validation error になり、死に設定にならない。値を runtime へ「渡す」の
ではなく、**定義値と実装挙動の一致を保証**することで反映を担保する。

| 外部フィールド(固定値) | 一致対象の実装挙動 |
|---|---|
| `entry.handler = squeeze_breakout` | registry 解決 → `squeeze_breakout` 経路を選択 |
| `risk.stop_loss.type = trigger_bar_extreme` | Trigger 足 low/high から stop 算出(`squeeze_breakout.py`) |
| `risk.take_profit.type = r_multiple` | Execution Model 適用後 Entry から `r × risk` |
| `entry.reentry = new_setup` | 新 episode まで再 arm しない state machine |
| `exit.same_candle_priority = stop_loss` | `evaluate_squeeze_exit()` の無条件 SL 優先 |
| `entry.cooldown_bars = 0` | squeeze は時間 cooldown を適用しない(実装挙動と一致。0=無適用のため runner へ別途渡す必要がない) |
| `entry.params.squeeze_state = squeeze` | `SQUEEZE_STATE` |
| `entry.params.require_episode_release = true` | episode 解除足のみ評価 |
| `entry.params.breakout = bollinger_close` | BB 終値 break(ヒゲ到達不可) |
| `allowed_market_states = null` | squeeze は market-state ゲート非対応(§6) |

**④ メタデータ(記録。strategy ロジックには影響しないが config_json / summary へ反映)**

| 外部フィールド | 反映先 |
|---|---|
| `schema_version` / `id` / `name` / `description` | `backtest_runs.config_json`(strategy 識別)/ summary 出力 |

### handler 実行契約の一致保証(テスト)

カテゴリ③の「descriptor 許容値 == handler 実装挙動」は **テストで固定**する(#53)。例:
descriptor の `same_candle_priority` 許容集合が `{stop_loss}` であることと、`evaluate_squeeze_exit()` が
同一足 SL/TP 双方到達時に stop_loss を返すことを同一テストで突合する。descriptor と実装が乖離したら
handler 側のバグとして検出される。これにより capability validation(受理段階)と実行挙動が恒久的に一致する。

### cooldown_bars(補足)

`cooldown_bars` は squeeze では固定値契約(0 のみ、カテゴリ③)。0 は「cooldown 無適用」で、squeeze
実行経路は元々 cooldown を適用しないため、**runner へ値を渡さなくても定義と実行が一致**する。非ゼロ
cooldown を許容する handler を追加する時点で、その handler の実行接続(bars→runner 表現)を併せて実装する
(その handler の descriptor が非ゼロを許容し、かつ実行接続を持つ、という③の一致保証で縛る)。

---

## 6. allowed_market_states

- 各要素は `market-watch/market_state.py:KNOWN_MARKET_STATES`
  (`trend_up` / `trend_down` / `range` / `volatile` / `squeeze` / `unknown`)のいずれか。範囲外はエラー。
- `null`(既定)= フィルタ無し。既存 CLI `--allowed-market-states` と同じ意味論を宣言化したもの。
- **handler 対応契約(§4.1)で判定**:squeeze_breakout handler は **自前の squeeze episode state
  machine** を持ち、market-state ゲートを使わない(`--allowed-market-states` は trend_follow 経路専用)。
  よって squeeze の capability descriptor では `allowed_market_states` は **非対応 = `null` 必須**とし、
  **非 null を指定したら validation error**(死に設定にしない)。サンプルは `null`。
  スキーマ上は全 handler 共通に持てるフィールドだが、実際に尊重するかは §4.1 の contract で決まり、
  非対応 handler との組合せはエラーになる(黙って無視しない)。

---

## 7. entry(handler / params / reentry / cooldown)

```yaml
entry:
  handler: squeeze_breakout       # 登録済みキー(§4)
  params:                         # handler 固有・宣言値のみ(式・コード不可)
    squeeze_state: squeeze        # episode の準備状態ラベル
    require_episode_release: true # 連続 squeeze 解除足だけを評価
    breakout: bollinger_close     # BB 終値 break(ヒゲ到達は不可)
  reentry: new_setup              # {overlap, single_position, new_setup}
  cooldown_bars: 0
```

- **`handler`**:§4。
- **`params`**:handler 固有の **宣言的値のみ**。generic loader は構造(下記の型規則)だけ検証し、
  **各 key の意味・範囲・必須は handler の capability descriptor(§4.1)が検証**する。これが唯一の
  未知 key 許容ゾーン(§3)。**式・条件文字列・コード片は入れない**(過剰な DSL 化の回避)。
  - squeeze_breakout の場合、BB 期間(20)・±2σ・squeeze lookback(20)・下位 10% は
    **indicators / market_state が所有する共通定義**であり、Strategy 固有の自由度ではない。
    よって params には「どの state を準備に使うか」「解除足だけ評価するか」「break の種類」といった
    **handler が明示的に読む宣言フラグ**のみを置き、指標定数そのものは持たせない
    (持たせると指標計算の外部化=責務越境になる)。
  - **許可する値型(schema v1、generic loader が強制):**
    - スカラ: `str` / `int` / `float` / `bool` / `null`。
    - **スカラの平坦リスト** `list[scalar]`。
    - **ネスト不可**:dict(mapping)、list of dict、多重 list は schema v1 では **拒否**
      (`StrategyDefinitionError`)。構造化 params が必要になれば schema_version を上げて拡張する。
    - key は **文字列のみ**。空 params(`{}`)または省略は許容(default `{}`)。
    - `null` は明示的に許容(値の欠如)。ただし handler が必須とする key が `null`/欠落なら
      handler validation error。
    - **`NaN` / `Infinity` / `-Infinity`(YAML の `.nan` / `.inf` 含む)は禁止**=`StrategyDefinitionError`。
      config_json は NaN を出さない方針(既存 `report.render_json` は `allow_nan=False`)と整合。
- **`reentry`**:
  - `overlap`(既定)= 重なり許容(現行 runner 既定)。
  - `single_position` = グローバル単一ポジション(現行 `--no-reentry` 相当)。
  - `new_setup` = handler の setup 消費規則に従う(squeeze:新しい episode まで再 arm しない)。
    squeeze はこれを採用。構造的に単一ポジションが保証される。
- **`cooldown_bars`**:決済足から N 本は新規 entry を抑制。**単位は「分」ではなく「本(bars)」**。
  採用理由:Strategy 固有量は timeframe 相対で表すのが自然で、`max_holding_bars` と単位が揃う。
  不採用(分):現行 runner は `--cooldown-minutes`(壁時計分)だが、これは実行時フラグの都合であり、
  Strategy の宣言としては bars が適切。squeeze は capability 契約上 `0` 固定で、0=cooldown 無適用は
  squeeze の実行挙動と一致するため runner へ値を渡す必要がない(§5.2 カテゴリ③・cooldown_bars 補足)。

---

## 8. risk / exit(SL / TP / max holding / time exit / same-candle priority)

```yaml
risk:
  stop_loss:
    type: trigger_bar_extreme   # long=Trigger足low, short=Trigger足high(価格算出はPython)
  take_profit:
    type: r_multiple
    r: 2.0
exit:
  max_holding_bars: 24          # time exit:Entry後この本数目のcloseで決済
  same_candle_priority: stop_loss
```

- **stop_loss.type**(実装済みのみ enum に載せる。将来 type は先行実装しない):
  - `trigger_bar_extreme`:long=Trigger 足 low、short=Trigger 足 high。**squeeze が採用**。
    R(risk)はこの stop 距離で定義される(=固定 R を stop に持たせない)。
  - `fixed_pct` + `pct`:Entry から `pct` の逆方向(exit_policy の fallback 0.003 相当)。
    trend_follow 系 handler 向けの実在型として定義。
- **take_profit.type**:
  - `r_multiple` + `r`:Execution Model 適用後 Entry から有利方向へ `r × risk`。squeeze は `r=2.0`。
- **`stop_loss_r` / `take_profit_r` を採らない理由**:Issue #52 の想定イメージは `stop_loss_r`/`take_profit_r`
  だが、squeeze の stop は **構造的(Trigger 足 low/high)** で R 倍数ではない。R 倍数だけの表現では
  squeeze を忠実に表せないため、`{type, ...}` の型付き表現を採り、その中で `r_multiple`/`fixed_pct` を
  選べるようにした。数値だけの平坦表現(`stop_loss_r`)は不採用。
- **`max_holding_bars`**:int > 0。time exit は「Entry 後この本数目の確定足 close」。
  その足でも **gap → SL/TP を先に評価**し、いずれも不成立のときだけ time exit(判定順序は Python)。
  squeeze は `24`(XAUUSD 1h の 24 時間相当、欠損は実在の次 24 本で数える)。
- **`same_candle_priority`**:同一足で SL/TP 双方到達時どちらを優先するか。スキーマ上の enum は
  `{stop_loss, take_profit}`(既定 `stop_loss`, 保守的)。語彙は `exit_policy.py` /
  `paper-execution/execution_profile.py` と同一文字列(層をまたいで突合可能にするため)。
  - **squeeze_breakout は `stop_loss` のみ対応**(§4.1)。現行 `evaluate_squeeze_exit()` は同一足到達時に
    **無条件で stop_loss 優先**で、priority を受け取る引数を持たない。よって
    `handler: squeeze_breakout` に対し **`same_candle_priority: take_profit` は validation error**
    とし、`stop_loss` または省略(default `stop_loss`)のみ許容する。これにより「Loader は受理したが
    実行時に無視される」死に設定を防ぐ。`take_profit` を実際に選べるのは、それを読む handler が
    追加された時だけ(その handler の descriptor が対応値に含める)。

---

## 9. spread / slippage / fee の責務境界(重点調査)

**結論:spread / slippage / fee(commission)は Strategy 定義に置かない。実行環境側の設定とする。**

### 現状の所有者(2 か所、いずれも Strategy とは独立に解決)

| 層 | モジュール | 持つコスト | 解決キー | 記録先 |
|---|---|---|---|---|
| Backtest | `backtest/execution_model.py`(`fixed_spread_slippage`) | spread / slippage(**fee 無し**) | **run 単位の CLI `--spread`/`--slippage`** | `backtest_runs.config_json.execution_model` |
| Paper Execution | `paper-execution/execution_profile.py`(`ExecutionProfile`, Issue #49) | spread / slippage / **commission(fee)** | **`(symbol, timeframe)`** | position 行へ snapshot |

### なぜ Strategy に置かないか(採用理由)

1. **Issue #43 が明示**:「spread/slippage 値は戦略仕様では固定せず、各 Backtest の**実行条件**として
   必ず明示し、run config へ保存する」。コストは Strategy の同一性ではなく **run の実行条件**。
2. **Issue #49 の分離原則**:`execution_profile.py` は「`strategy_profile` は RECORD-ONLY で、
   SL/TP・deadline・priority・**costs** の解決に一切使わない」と明記。コストは execution 側が
   `(symbol, timeframe)` で解決する。Strategy にコストを持たせると、この確立済みの分離に反する。
3. **二重管理の回避**:同じ spread を Strategy と run(または execution_profile)の両方に書けると、
   どちらが有効か不定になり再現性を損なう。単一情報源を実行環境側に固定する。
4. **fee は Backtest に存在しない**:`execution_model` は spread+slippage のみ。fee は Paper Execution の
   commission だけに存在する。Backtest 用 Strategy 定義に fee を足すのは、実在しないコストの先行実装。

### 残る重複の明示(risk_reward / max_hold_bars)

`ExecutionProfile` は `risk_reward` / `max_hold_bars` / `same_candle_priority` も持ち、本 Strategy 定義の
`risk.take_profit.r` / `exit.max_holding_bars` / `exit.same_candle_priority` と **値域が重なる**。

- **切り分け**:Strategy 定義が持つのは「**その Strategy が何であるか**」を定義する intrinsic な値
  (2R・24 本は Issue #43 が Strategy の仮説として pin したもの)。`ExecutionProfile` の同名項目は
  Paper Execution が `(symbol, timeframe)` で解決する **別レイヤーの実行ルール**。
- 本 Issue のスコープは **Backtest の Strategy 外部化**であり、Paper Execution の `ExecutionProfile` との
  最終的な統合(どちらを source of truth にするか)は移行 Issue の判断とする(§11 の残課題)。
  現時点の境界は「**Strategy 定義=Strategy intrinsic な sizing 意図の宣言、コストは常に実行環境側**」で確定。

---

## 10. 内部型への変換(想定構造)

Loader(Issue #53)は、YAML を **既存 idiom に倣った frozen dataclass** へ落とす
(`ExecutionModel` / `ExecutionProfile` / `SqueezeBreakoutSignal` と同じ不変・検証済み型)。

```python
@dataclass(frozen=True)
class StrategyDefinition:
    schema_version: int
    id: str
    name: str
    description: str                       # 未指定は ""
    symbols: tuple[str, ...]               # 非空
    timeframes: tuple[str, ...]            # 非空
    allowed_market_states: frozenset[str] | None
    entry: "EntrySpec"
    risk: "RiskSpec"
    exit: "ExitSpec"
    source_path: str                       # 由来ファイル(エラー・重複検出用)

@dataclass(frozen=True)
class EntrySpec:
    handler: str
    params: dict                           # handler が検証; 変換後も dict のまま
    reentry: str                           # {overlap, single_position, new_setup}
    cooldown_bars: int

@dataclass(frozen=True)
class RiskSpec:
    stop_loss: dict                        # {"type": ..., ...}
    take_profit: dict                      # {"type": "r_multiple", "r": 2.0}

@dataclass(frozen=True)
class ExitSpec:
    max_holding_bars: int
    same_candle_priority: str
```

変換の要点:

- **default はここで解決**して埋める(§3)。以降のロジックは「未指定=ハードコード」に依存しない。
- `symbols`/`timeframes` は tuple、`allowed_market_states` は frozenset|None(既存 runner が
  `frozenset` を使うため整合)。`params` は dict のまま handler へ渡す。
- **`to_config()` は JSON-serializable な plain dict** を返し、`backtest_runs.config_json` の既存
  `strategy_profile` 近傍へそのまま格納できる(現行 `_run_squeeze_breakout` が `strategy_profile` /
  `risk_reward` / `time_exit_bars` / `same_candle_priority` を config へ書いているのと同型)。
- `cooldown_bars`(本)は `EntrySpec` に載せる。squeeze では固定値 `0`(=cooldown 無適用)で実行挙動と
  一致するため runner へ値を渡さない(§5.2)。非ゼロ cooldown を許容する handler 追加時に実行接続する。

---

## 11. エラー方針(まとめ)

Loader は **専用例外 `StrategyDefinitionError(ValueError)`** を送出する(既存の
`exit_policy.validate_exit_policy_config` / `execution_model` が `ValueError` を使う idiom に合わせる)。
メッセージには **ファイルパス + 問題キーの JSON パス** を含める。

| ケース | 挙動 |
|---|---|
| YAML パース失敗 | `StrategyDefinitionError`(パス付き)。 |
| 必須キー欠落 | エラー。どのキーが無いかを明示。 |
| 未知キー(既知オブジェクト配下) | **拒否(fail-closed)**。typo 検出のため。**例外は `entry.params`**(handler 所有)。 |
| 型不正(例:`schema_version` が str) | エラー(期待型と実型を表示)。 |
| 範囲外値(例:`max_holding_bars <= 0`、未知 market state、負の cooldown) | エラー。 |
| 未対応 schema_version | エラー(サポート集合を表示)。 |
| id 重複(集合内) | **ロード全体を失敗**。両ファイルパスを表示。 |
| 未登録 handler | エラー(登録済み一覧を表示)。 |
| 条件付き必須の欠落(`r_multiple` に `r` 無し 等) | エラー。 |
| handler 非対応フィールド / 非許容値(例:squeeze に `same_candle_priority: take_profit`、非 null `allowed_market_states`) | **validation error**(§4.1)。無視しない。 |
| `entry.params` の型違反(ネスト dict、多重 list、NaN/Inf) | エラー(§7)。 |
| `--strategy-file` と `--strategy` の同時指定 / 旧 `--strategy-profile` との併用 | 使用エラー(§5.1)。 |

- **fail-fast**:最初のエラーで停止し 1 件を報告する(既存バリデータと同挙動)。複数エラーの集約は
  任意改善であり必須ではない。
- generic loader と handler の責務分割:**構造・型・enum・必須は generic loader**、
  **`entry.params` の中身は handler** が検証する。

---

## 12. 本 Issue で確定した事項 / 意図的に後続へ残す事項

### 本 Issue(#52)で確定済み(#53 で再判断しない)

- ファイル形式(YAML / `safe_load`)、schema_version・互換性方針、全スキーマ項目と必須/default、
  未知キー・型不正・範囲外・未対応 version・id 重複・未登録 handler のエラー方針(§1–§11)。
- **ロード単位 = 1 ファイル 1 Strategy、ディレクトリ走査で id registry 化、部分ロード禁止**(§5.1)。
- **runtime 既定配置 = `backtest/strategies/`(`STRATEGY_DEFINITIONS_DIR` で上書き)**(§5.1)。
- **CLI 接続 = `--strategy-file <path>`(単一) / `--strategy <id>`(ディレクトリ+選択)、排他**(§5.1)。
- **ファイル未指定時の後方互換 = 外部機構を起動せず現行 `--strategy-profile` を完全維持(opt-in)**(§5.1)。
- **#53 は実行接続まで含む(案A)**:選択された外部定義の全受理フィールドが ①実行パラメータ /
  ②適合検証 / ③固定値契約 / ④メタデータ のいずれかで実行へ反映され、**死に設定ゼロ**(§5.2)。
  squeeze の可変フィールドは `risk.take_profit.r`→`risk_reward`、`exit.max_holding_bars`→`time_exit_bars`。
- **handler 別対応フィールド一覧(capability descriptor)+ 非対応/非許容値は validation error、
  かつ固定値許容値は handler 実行挙動と一致(テストで固定)**(§4.1, §5.2)。
- **`entry.params` の許可型(スカラ/平坦リスト、ネスト・NaN 不可、null 可)**(§7)。

### 意図的に後続 Issue(移行 Issue)へ残す事項 — #53 の Loader/Validation 実装には不要

以下は Loader / Validation の実装方式を左右しないため本 Issue では確定しない。

1. **Paper Execution `ExecutionProfile` との統合**:`risk_reward` / `max_hold_bars` /
   `same_candle_priority` の最終 source of truth をどちらへ寄せるか(§9)。本 Issue のスコープは
   Backtest の Strategy 外部化であり、Paper Execution 層の再編は別 Issue。現時点の境界(Strategy 定義=
   intrinsic な宣言、コストは常に実行環境側)は確定済みで、#53 の実装はこれに依存しない。
2. **既存 `squeeze_breakout` の default パスの移行**(非選択時の module 定数 `RISK_REWARD` /
   `TIME_EXIT_BARS` を撤去し、既定でも外部定義から解決するようにする)。#53 は **選択時のみ**外部定義を
   実行接続し、非選択パスは module 定数のまま完全維持する(§5.2 前提)。default パスの撤去・置換は
   移行 Issue で行う(本 Issue の明示的非スコープ)。
3. **cooldown を実際に使う handler を追加する際の runner 接続**(bars→runner 表現の橋渡し)。
   その handler を足す Issue で行う(§5.2)。
