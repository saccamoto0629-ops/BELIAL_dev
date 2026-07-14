# Backtest Runner / CLI

Issue #33 (v1.4 Backtest)。既存の `backtest/backtest_models.py` / `backtest/backtest_store.py` /
`backtest/warmup.py` を再利用し、`symbol / timeframe / from / to` を受け取って過去のローソク足を
再生する Runner / CLI (`backtest/runner.py`) を追加した。

Issue #23 で、run単位のSignal明細を集計するSummary report(`backtest/summary.py` / `backtest/report.py`)
と、それを再取得する `summary` サブコマンドを追加した(後述)。

Issue #24(Backtest CLI)は`symbol`/`timeframe`/`from`/`to`/`strategy_name`/`exit_policy`の指定、
run_id・summaryの標準出力、Discord非送信をAcceptance Criteriaとしており、`--strategy-name`・
run_idのstdout表示・summary表示・Discord非送信はIssue #33/#23の実装で既に満たされていた。
このセッションで唯一未実装だった `--exit-policy` フラグを追加し、Issue #24を完了させた
(詳細は下記「`run` の引数」を参照)。

**Paper-only。** Discord通知、実発注、Broker API連携は一切行わない。読み取るのは
`market-watch/data/chart_data.sqlite3`(ローソク足)と `news-feed-bot/data/market_bias.sqlite3`
(Market Bias)のみで、書き込むのは既存の Backtest DB(`backtest_runs` / `backtest_signals`)のみ
(`summary` サブコマンドで `--output` を指定した場合のみ、指定したファイルにも書き込む)。

> Issue #52(設計): Strategy ごとの可変設定を外部ファイルへ切り出すためのスキーマ・エラー方針は
> [docs/architecture/strategy_definition.md](architecture/strategy_definition.md) を参照。Loader /
> Validation は未実装(後続 Issue #53)で、現状の `--strategy-profile` の挙動は変わらない。

## 事前準備(データの前提)

Runnerは何もデータを取得しない。実行前に、別コンポーネントが以下を**既に永続化済み**である必要がある。

### 1. Chart DB(必須)

- パス: 既定 `market-watch/data/chart_data.sqlite3`(`CHART_DB_PATH` / `--db-path` で上書き可)。
- 必要テーブル: `candles`(`symbol, timeframe, timestamp, open, high, low, close, volume`)。
  スキーマは `market-watch/chart_collector.py:init_db()` が定義する。
- 用意する方法: `market-watch/chart_collector.py`(または `chart_snapshot.py`)を実行し、
  対象 symbol/timeframe のローソク足を事前に収集しておく。
- Runnerが検索する symbol は `--symbol` からアンダースコアを除去・大文字化した値(例:
  `USD_JPY` → `USDJPY`)。`candles` テーブルにこのキーで該当行が無ければデータ不足として扱う。
- `--timeframe` は `candles.timeframe` の値と完全一致が必要(例: `15m` / `1h` / `4h`)。

### 2. Market Bias DB(任意、ただし無いとシグナルは生成されない)

- パス: 既定 `news-feed-bot/data/market_bias.sqlite3`(`MARKET_BIAS_DB_PATH` / `--market-bias-db-path` で上書き可)。
- 必要テーブル: `market_bias`(`news-feed-bot/market_bias_store.py:init_db()` が定義)。
- 用意する方法: `news-feed-bot/market_snapshot.py` の通常運用で `symbol="GLOBAL", timeframe="1h"`
  として自動的に保存される。Backtestは `--from` 時点以前の直近レコードを1件だけ参照する
  (詳細は「Market Bias の参照」を参照)。
- DBが無い、または該当レコードが無い場合はエラーにはならず、Neutralプレースホルダにフォールバック
  して警告ログを出す(=その run はシグナルを1件も生成しない)。データ不足で終了コード3にはならない
  (Chart DB側のデータ不足のみが終了コード3の対象)。

### 3. 実行前の最小チェック

本番DBを使う前に、以下で「データが実際にあるか」を素早く確認できる。

```bash
python - <<'EOF'
import sqlite3
conn = sqlite3.connect("market-watch/data/chart_data.sqlite3")
print(conn.execute(
    "SELECT symbol, timeframe, MIN(timestamp), MAX(timestamp), COUNT(*) "
    "FROM candles WHERE symbol='USDJPY' AND timeframe='1h' GROUP BY symbol, timeframe"
).fetchall())
EOF
```

何も出力されなければ、その symbol/timeframe の候補データが無い(=Runnerは終了コード3を返す)。
`--from` より前に `--warmup-bars`(既定75)本以上のローソク足があるかも、この`MIN(timestamp)`と
`--from`の差から事前に見積もっておくとよい(不足していても実行自体は失敗せず、
`warmup_insufficient`として全バーがスキップされるだけ)。

## 実行方法

リポジトリルートから実行する。`run` サブコマンドは省略できる(後方互換のため、
サブコマンド名を省略した場合は暗黙に `run` として扱われる -- 既存の呼び出し方は変更不要)。

```bash
python -m backtest \
  --symbol USD_JPY \
  --timeframe 1h \
  --from 2026-06-01T00:00:00 \
  --to 2026-06-30T23:59:59

# 上と完全に同じ意味
python -m backtest run \
  --symbol USD_JPY \
  --timeframe 1h \
  --from 2026-06-01T00:00:00 \
  --to 2026-06-30T23:59:59

# strategy_name / exit_policy を明示する場合(Issue #24)
python -m backtest run \
  --symbol USD_JPY \
  --timeframe 1h \
  --from 2026-06-01T00:00:00 \
  --to 2026-06-30T23:59:59 \
  --strategy-name trend_pullback_v1 \
  --exit-policy fixed_rr
```

`python -m backtest.runner` も同じ意味(`backtest` の `__main__.py` はこちらへの薄いエイリアス)。

```bash
python -m backtest.runner \
  --symbol USD_JPY \
  --timeframe 1h \
  --from 2026-06-01T00:00:00 \
  --to 2026-06-30T23:59:59
```

過去の `run_id` からsummaryだけを再取得する場合は `summary` サブコマンドを使う(サブコマンド名は省略不可)。

```bash
# stdoutにテキストで表示(既定)
python -m backtest summary --run-id <RUN_ID>

# JSON形式でstdoutに表示
python -m backtest summary --run-id <RUN_ID> --format json

# Markdown形式でファイルへ保存(親ディレクトリは自動作成、既存ファイルは上書き)
python -m backtest summary --run-id <RUN_ID> --format markdown --output reports/run_<RUN_ID>.md
```

### `run` の引数

| 引数 | 必須 | 説明 |
|---|---|---|
| `--symbol` | ✅ | 例: `USD_JPY`。Chart DB 側のキー(`USDJPY` のようにアンダースコアなし)へ内部で正規化して検索する。 |
| `--timeframe` | ✅ | Chart DB に保存されている値と一致させる(例: `15m` / `1h` / `4h`)。issue本文の `M15` のような表記はそのままでは一致しないので注意。 |
| `--from` | ✅ | validation期間の開始(inclusive)。ISO-8601。タイムゾーンなしはUTCとして扱う。 |
| `--to` | ✅ | validation期間の終了(inclusive)。ISO-8601。 |
| `--db-path` | | Chart DB のパス。既定は `CHART_DB_PATH` 環境変数、なければ `market-watch/data/chart_data.sqlite3`。 |
| `--backtest-db-path` | | Backtest結果DBのパス。既定は `BACKTEST_DB_PATH`、なければ `backtest/data/backtest_results.sqlite3`。 |
| `--market-bias-db-path` | | Market Bias DBのパス。既定は `MARKET_BIAS_DB_PATH`、なければ `news-feed-bot/data/market_bias.sqlite3`。 |
| `--warmup-bars` | | indicator計算に必要なwarm-up本数。既定は `warmup.DEFAULT_WARMUP_BARS`(=75、SMA75由来)。**1以上の整数のみ**(0以下は終了コード2)。 |
| `--confidence-threshold` | | Paper Signal Generatorの信頼度しきい値を上書きする。既定は `signal_generator.DEFAULT_CONFIDENCE_THRESHOLD`(0.40)。**0.0以上1.0以下のみ**(範囲外は終了コード2)。 |
| `--exit-config` | | Exit Policy JSON設定ファイルへのパス(`paper-signal/exit_policy.py` 参照)。既定は `BACKTEST_EXIT_CONFIG_PATH`、なければ組み込みのデフォルト設定。 |
| `--exit-policy` | | どのExit Policyを使うかを明示的に選択する(`--exit-config` が指定する `exit_policy` より優先)。選択肢は `fixed_rr` / `time_based_close` / `ma_break_invalidation` / `bias_reversal_close` / `recent_high_low_break`(不正な値はargparseレベルで終了コード2)。既定は `--exit-config` の解決結果(未指定なら `fixed_rr`)。選択したポリシーが解決後のconfigで `enabled: true` になっていない場合(例: 既定では無効な `ma_break_invalidation` をconfigファイルなしで指定した場合)は、分かりやすいエラーメッセージとともに終了コード2を返す。保存される `backtest_runs.exit_policy` / 各 `backtest_signals.exit_policy` に、実際に使われたポリシー名がそのまま反映される。 |
| `--spread` | | Issue #26: 各約定に対して不利方向へ常時かけるスプレッド(pipsではなく絶対価格差)。**0.0以上のみ**(既定0.0=コストなし)。詳細は下記「コスト込みrun」を参照。 |
| `--slippage` | | Issue #26: 各約定に対して不利方向へ常時かけるスリッページ(pipsではなく絶対価格差)。**0.0以上のみ**(既定0.0=コストなし)。 |
| `--no-reentry` | | Issue #38: 保有中の再エントリーを禁止する(グローバルに常時1ポジション)。有効時、保有ポジションの `entry <= T <= close_time` の区間に発生した新規候補を抑制する。`close_time=None`(loaded window内で決済しなかったopenポジション)は `--to` まで保有扱い。既定は無効(候補の重なりを許容=Issue #38以前の挙動)。 |
| `--cooldown-minutes` | | Issue #38: 任意のポジションが時刻 `C` で決済した後、`C + N分` 未満に発生した新規候補を抑制する。long/shortやsignal type単位ではなくグローバル。**0.0以上のみ**(既定0.0=無効=Issue #38以前の挙動)。`--no-reentry` とは独立したフラグで、両方指定も可能。 |
| `--time-series-bias` | | Issue #38: Market Bias を run全体に固定適用せず、各バーのclose時刻ごとに `evaluated_at <= close` の最新biasをpoint-in-timeで参照する(未来のbiasは混入しない。対象バーより前にbiasが無ければNeutral)。既定は無効(static bias=Issue #38以前の挙動、下記「Market Bias の参照」を参照)。詳細は下記「時系列 Market Bias」を参照。 |
| `--allowed-market-states` | | Issue #39: 各候補を、そのバーの Market State がこの許可リストに含まれる場合のみ採用する(カンマ区切り。有効値: `trend_up` / `trend_down` / `range` / `volatile` / `squeeze` / `unknown`)。全バーの Market State は指定有無に関わらず常に判定・記録され、このフラグは「どの候補を保存シグナルにするか」だけをゲートする。未指定(既定)=state フィルタなし(Issue #39以前の挙動、全候補が対象)。不正・空・未知の state 名は終了コード2。詳細は下記「Market State によるエントリー制御」を参照。 |
| `--run-name` | | 既定は `<symbol>_<timeframe>_<from>_<to>`。 |
| `--strategy-name` | | 既定は `paper_signal_generator`(=Paper Signal Generatorの既存ルールをそのまま再生していることを示す)。`backtest_runs.strategy_name` に保存されるラベルのみで、指定してもシグナル生成ロジック自体は変わらない。 |

### `summary` の引数

| 引数 | 必須 | 説明 |
|---|---|---|
| `--run-id` | ✅ | `backtest_runs.id`。存在しない run_id を指定した場合は終了コード3。 |
| `--backtest-db-path` | | Backtest結果DBのパス。既定は `run` と同じ(`BACKTEST_DB_PATH` / `backtest/data/backtest_results.sqlite3`)。 |
| `--format` | | `text`(既定)/ `json` / `markdown`。 |
| `--output` | | 指定するとレンダリング結果をこのパスへ書き込む(親ディレクトリは自動作成、既存ファイルは無条件に上書き)。指定した場合、stdoutにはファイルパスを含む短い確認メッセージのみ表示し、内容は二重に出力しない。省略した場合はレンダリング結果をそのままstdoutへ出力する。書き込みに失敗した場合(既存ディレクトリを指定した、権限がない等)はtracebackを出さず、`error: could not write summary to <path>: <reason>` をstderrへ出力して終了コード2(`EXIT_USAGE_ERROR`)を返す。 |

## 終了コード

| コード | 意味 |
|---|---|
| `0` | 実行成功(シグナル0件でも成功)。 |
| `2` | 引数エラー(必須引数の欠落、`--from`/`--to` のパース失敗、`--to` が `--from` より前、`--warmup-bars` が1未満、`--confidence-threshold` が0.0〜1.0の範囲外、`--spread`/`--slippage`/`--cooldown-minutes` が負、`--allowed-market-states` が空/未知の state 名など)。argparse自体の使用エラーも同じ2を返す。 |
| `3` | データ不足(`run`: 該当 symbol/timeframe のローソク足が1件もない、またはvalidation期間内に該当する足が1件もない。`summary`: 指定した `--run-id` が `backtest_runs` に存在しない)。 |

## warm-up期間とvalidation期間の分離

- Runnerは `--to` 以前の全ローソク足を読み込み、`warmup.split_warmup_and_validation()` で
  「`--from` より前(warm-up)」と「`--from`〜`--to`(validation)」に分割する。
- Backtestの結果集計(`backtest_signals` への保存、標準出力の集計)に含まれるのは
  常にvalidation期間の足だけ。warm-up期間の足はindicator計算のシードにのみ使われ、
  結果には一切現れない(`warmup.evaluate_backtest_bars()` の既存の保証をそのまま利用)。
- warm-up本数が `--warmup-bars` に届かない場合、その旨を警告ログに出し、
  validation期間の全バーが `warmup_insufficient` 理由でスキップされる(既存の`warmup.py`の挙動)。

## Market Bias の参照

Paper Signal候補の生成には Market Bias が必要(`paper-signal/signal_generator.build_candidates()`)。
Runnerは外部APIを一切呼ばず、`news-feed-bot/market_bias_store.get_market_bias_as_of("GLOBAL", "1h", <validation開始時刻>)`
で、validation期間開始時点までに永続化済みのMarket Biasを1件だけ読み取り、run全体に適用する。

該当するMarket Biasが見つからない場合(DB未作成、対象期間より前の記録が1件もない等)は
`market_bias="Neutral", confidence=0.0` のプレースホルダにフォールバックする。Neutral bias
は `build_candidates()` のルール上シグナルを一切生成しないため、安全側に倒れる(警告ログのみ)。

## 現実的な検証条件(Issue #38)

現状の Runner は validation 期間の各バーを独立に評価するため、条件成立バーごとに重なり合う
シグナルが密に生成される。Issue #38 は、**既存runの再現性を壊さないオプトイン方式**で、より
現実的な検証条件を追加する。以下のフラグはすべて既定でOFFであり、未指定なら Issue #38 以前と
完全に同一の結果になる(フラグ無しの既存1年runは変更前と同じシグナル数を再現する)。

### 保有中の再エントリー禁止(`--no-reentry`)

- 有効時はグローバルに常時1ポジションとして扱う。
- 保有ポジションの `entry <= T <= close_time` の区間に発生した新規候補を抑制する
  (close時刻と同一timestampの再entryも抑止する)。
- `close_time=None`(loaded window内で決済しなかったopenポジション)は `--to`(validation_end)まで
  保有扱いとし、以降の候補をすべて抑制する。
- 抑止件数は summary の `Suppressed: re-entry=...` と、`backtest_runs.config_json` の
  `no_reentry` に記録される。

### 決済後グローバルcooldown(`--cooldown-minutes N`)

- 単位は分、既定は `0.0`(無効)。
- 任意のポジションが時刻 `C` で決済した後、`C + N分` 未満に発生した全候補を抑制する。
- long/shortやsignal type単位ではなくグローバルなcooldownとして扱う。
- `--no-reentry` とは独立で、両方を同時に指定できる(その場合、`entry <= C` は re-entry、
  `C < entry < C + N分` は cooldown として、二重計上せずどちらか一方でカウントする)。
- 抑止件数は summary の `Suppressed: cooldown=...` と `config_json` の `cooldown_minutes` に記録される。

`--no-reentry --cooldown-minutes 60` を併用すると、密なシグナルが大幅に間引かれ、実運用の
Paper Signal に近い発生頻度になる。

### コスト込みrun(`--spread` / `--slippage`)

既存の Issue #26 の仮想約定モデル(`backtest/execution_model.py`)をそのまま使う。両者とも
「不利方向へ常時かける絶対価格差(pipsではない)」で、既定は `0.0`(コストなし)。エントリーと
決済の双方でコストがかかるため、決済済みシグナルのpnlは概ね `2 * (spread + slippage)` 分だけ
悪化する。

```bash
# スプレッド0.02・スリッページ0.01込みで1年run
python -m backtest run \
  --symbol USD_JPY \
  --timeframe 1h \
  --from 2025-07-25T00:00:00 \
  --to 2026-07-11T00:00:00 \
  --exit-policy fixed_rr \
  --spread 0.02 \
  --slippage 0.01
```

コストは `backtest_runs.config_json` の `execution_model`(`type` / `spread` / `slippage`)と
summary の `Execution model:` 行に記録される。

### Bull固定 / Bear固定の別run手順

`--time-series-bias` を使わず、Bull だけ・Bear だけを含む Market Bias DB をそれぞれ用意して
2回runすることで、特定biasに固定した挙動を検証できる(コード変更は不要)。

```bash
# 1) Bull固定のbias DBを用意(GLOBAL/1h に Bull レコードを1件入れておく)
#    -> そのbias DBを指す --market-bias-db-path で run
python -m backtest run \
  --symbol USD_JPY --timeframe 1h \
  --from 2025-07-25T00:00:00 --to 2026-07-11T00:00:00 \
  --exit-policy fixed_rr \
  --market-bias-db-path /path/to/bias_bull.sqlite3

# 2) Bear固定のbias DBで別run
python -m backtest run \
  --symbol USD_JPY --timeframe 1h \
  --from 2025-07-25T00:00:00 --to 2026-07-11T00:00:00 \
  --exit-policy fixed_rr \
  --market-bias-db-path /path/to/bias_bear.sqlite3
```

static bias モード(`--time-series-bias` 未指定)は `--from` 時点の直近1件を全期間へ固定適用する
ため、上記のように「Bull だけ」「Bear だけ」の bias DB を用意すれば、その方向に固定した run に
なる。

### 時系列 Market Bias(`--time-series-bias`)

static bias は `--from` 時点の1件を全期間へ固定適用するため、Bull/Bear が時間変化する実際の
状態を再現できない。`--time-series-bias` を指定すると、Runner は
`news-feed-bot/market_bias_store.load_market_bias_series("GLOBAL", "1h", ...)` で対象の全bias履歴を
**runの最初に1回だけ**(=毎バーSQLiteを開閉しない、単一コネクション)ロードし、各バーの
**close時刻**(`candle open + timeframe`)ごとに `evaluated_at <= close` の最新biasを二分探索で
point-in-time参照する。

- 未来のbias(close時刻より後に評価されたbias)は決して混入しない。
- 対象バーより前にbiasが1件も無ければ Neutral(=そのバーは候補を生成しない)。
- 内部実装は「bias事前ロード + 昇順ソート済み系列への二分探索」なので、将来の時系列ポインタ探索
  への差し替えも容易(`backtest/runner.py:TimeSeriesBiasLookup`)。

これにより、Bull期間のバーからは long、Bear期間のバーからは short が同一run内で生成され、
summary の `by_bias` に Bull/Bear の双方が現れる。使用したモードは summary の `Bias mode:` 行と
`config_json` の `bias_mode`(`static` / `time_series`)に記録される。

> **Follow-up(既知の制約):** バーclose時点の指標で判定し、同じバーのclose価格でentryしている
> ため、原理的にはlookaheadの可能性が残る(次バーopen entry対応は本Issueの範囲外)。次バーopen
> entryへの切り替えは別Issue/TODOとして扱う。

## Market State によるエントリー制御(Issue #39)

#38 の realistic backtest で「USDJPY では PF>1 だが XAUUSD/SPX では PF<1」というトレンド依存性が
確認された。根因候補は、GLOBAL bias が銘柄自身のトレンドと無関係に適用され、下落中の銘柄を
Bull 判定で買い続けること。Issue #39 は **相場局面(Market State)を各バーで判定し、局面ごとに
エントリーを制御できるようにする**。

### Market State の定義(判定ロジックは新規実装しない)

Backtest は Live と同じ分類器 `market-watch/market_state.py:classify_market_state()`(Live の
Chart Snapshot が使っているものと同一)を再利用する。新しい語彙(Bull/Bear 等)は作らず、既存の
ラベルをそのまま採用する:

| Issue 本文の表現 | 既存ラベル | 判定内容(要約) |
|---|---|---|
| Bull | `trend_up` | EMA20 > SMA20 > SMA75 かつ MACD Bullish かつ close > SMA20 |
| Bear | `trend_down` | 上記の逆(下降スタック + MACD Bearish + close < SMA20) |
| Range | `range` | MA収束・MACD Neutral・BB内側(フォールバック) |
| (等) | `squeeze` | BB幅が直近レンジの下位パーセンタイル(最初に判定) |
| (等) | `volatile` | BB幅が直近ベースラインから拡大 |
| (等) | `unknown` | 指標欠損等で判定不能 |

### 各バーでの判定(point-in-time)

- validation 期間の **ready なバーごと**に、そのバーまで(自身を含む)の指標だけで Market State を
  判定する(`warmup.evaluate_backtest_bars()`)。スライスは当該バーを最終行に含み、**未来のバーは
  一切参照しない**(off-by-one 回帰テストあり: 未来バーを変更しても過去 state は不変、当該バーを
  変更するとその state は変化しうる)。
- 判定結果(`market_state` とその confidence)は、採用された各シグナルの `backtest_signals` 行に
  保存される。**全バーの state 履歴を別途保存することは本 Issue のスコープ外**で、永続化するのは
  採用された Backtest Signal のみ(フィルタ未指定でも記録される)。
- 性能注記: `classify_market_state()` は実際には直近約21本(BB の lookback)しか参照しないが、Live と
  byte-identical にするため毎バー履歴全体スライスを渡している。現状規模(1年1h ≒ 6千本)では許容。
  run が大きく育つ場合は、ready バーでは等価な bounded tail スライスへ差し替え可能
  (`backtest/warmup.py` のコメント参照)。

### エントリー制御(`--allowed-market-states`)

```bash
# 対象銘柄自身が trend_up のバーでのみ long を採用(下落局面での買いを止める)
python -m backtest run \
  --symbol XAU_USD --timeframe 1h \
  --from 2025-07-20T00:00:00 --to 2026-07-10T23:59:59 \
  --no-reentry --cooldown-minutes 60 --time-series-bias \
  --allowed-market-states trend_up
```

- カンマ区切りの許可リスト。指定した state のバーの候補のみ採用し、それ以外は抑制する。
- **未指定(既定)= フィルタ無効 = Issue #39以前の挙動**(全候補が対象)。空文字・未知の state 名は
  終了コード2。
- 抑制は #38 の re-entry / cooldown と同じ「採用ループでの suppression + カウンタ」方式。3つの
  suppression カウンタ(`market_state` / `re-entry` / `cooldown`)は **相互排他**で、1つの候補は
  **market_state → re-entry → cooldown の順で最初に該当した理由でのみ**1回カウントされる(複数条件
  への該当を二重計上しない)。market_state はバー自体の性質でポジション管理より上流のため先頭に置く。
- 許可リストは summary の `Allowed states:` 行(未指定時は `all`)と `backtest_runs.config_json` の
  `allowed_market_states`(ソート済みリスト、未指定時は null)に記録される。抑制件数は summary の
  `Suppressed: market-state=...` に出る。

### 局面別の成績確認(`by_market_state`)

集計に `by_market_state` breakdown が追加され、Bull/Bear/Range 別の勝率・PF が text/JSON/Markdown
全形式で確認できる(下記「標準出力の集計例」参照)。state を持たない古い行(NULL)は `unknown`
バケットに入る。まず **フィルタなしで1本回して `by_market_state` の分布を見てから**許可リストを
決める進め方を推奨する(フィルタ未指定でも全シグナルに state は記録されるため)。

## 銘柄別 Market Bias の履歴backfill(Issue #40 の検証用)

Issue #40 で Backtest は銘柄別 Market Bias(`symbol/1h`)を GLOBAL より優先して参照できるように
なったが、**実際に効くのは Market Bias DB に銘柄別レコードが存在する場合だけ**である。Live の
`market_snapshot` は「今」の bias しか記録せず過去を backfill しないため、過去1年を time-series で
回すと銘柄別レコードが無く、`--time-series-bias` は GLOBAL にフォールバックする
(summary の `Bias scope:` 行が `global_fallback=True` になる)。

`backtest/backfill_symbol_bias.py` は、各銘柄自身のローソク足を再生して**銘柄別 bias の履歴系列**を
再構築し、Market Bias DB へ書き込むオフラインツール(Paper-only、Discord/発注は一切なし)。

```bash
# 対象銘柄の銘柄別 bias 系列を Market Bias DB に backfill(既定で対象銘柄の既存行を消してから再構築)
python -m backtest.backfill_symbol_bias \
  --symbols USD_JPY,XAU_USD,SPX --timeframe 1h

# まず分布だけ確認(DB非書き込み)。Bull/Bear/Neutral の内訳と書き込み予定件数を表示
python -m backtest.backfill_symbol_bias \
  --symbols USD_JPY,XAU_USD,SPX --timeframe 1h --dry-run
```

設計上の要点(結果解釈のため):

- **方向性スコアは chart のみ。** Live が `classify_market_bias` に渡す cross-asset core signals
  (VIX/S&P500/Nasdaq/US10Y/Dollar/Oil/TV 推奨...)は履歴値が保存されていないため再生不能。近似の
  マクロ履歴を捏造せず `market_data=fred_data=tv_data=None` とし、各銘柄の方向性を**その銘柄自身の
  chart trend + MACD だけ**から決める(これは #40 が分離する銘柄別軸そのもの。
  `classify_market_bias_for_symbol` は元々 chart_data を対象銘柄に絞る)。分類器の evidence ルール上、
  方向性 Bull は chart trend `Bull` かつ MACD `Bullish`(Bear は両方 bearish)が必要で、それ以外は
  `Neutral`(=シグナル非生成の安全側)。
- **point-in-time / 先読みなし。** 各バーは自身までの直近200本(Live の `load_candles(limit=200)` と
  一致、指標が byte-identical)だけで、そのバーの close 時刻を `evaluation_time` として評価する。
- **変化時のみ記録。** 連続する同一 bias は先頭1件に集約(time-series lookup は前値を持ち越すので
  等価、DB を小さく保ち `evaluated_at` の区切りを意味のあるものにする)。
- **冪等な再実行。** 決定論的 id を使い、書き込み前に対象 canonical symbol の既存行を削除する
  (GLOBAL は絶対に触らない)。`--keep-existing` で削除を抑止できる。

backfill 後に `--time-series-bias` で回すと、summary の `Bias scope:` に `XXXUSD/1h` が現れ
(`global_fallback` は、GLOBAL しか無い期間が一部でもあれば True のまま)、`by_bias` に GLOBAL 固定
では出なかった Bear(=下落局面での short)が現れる。これで #40 の「下落中の銘柄を売り、上昇中を買う」
挙動を検証できる。

## Backtest結果の保存とEntry/Exitの扱い

- validation期間の各バーで `warmup.evaluate_backtest_bars()` がPaper Signal候補を生成する
  (bias + そのバーのindicator状態から `paper-signal/signal_generator.generate_signal_candidates()`
  を呼ぶだけで、DB・Discord・HTTPには一切触れない)。
- 候補が出たバーについて、そのバーの close 価格を仮の entry_price
  として扱う(この「バーの終値でエントリーする」のはBacktestの評価タイミングであって、
  シグナルの生成契機ではない。`trigger_type` にはPaper Signal候補の実際の生成契機
  `macd_state_alignment` がそのまま入り、`bar_close` で上書きすることはしない)。
  Exit評価(`paper-signal/exit_policy.evaluate_exit()`、既定は `fixed_rr`)に渡す
  候補足は**signalが発生したバーの次の足以降のみ**であり、signal発生バー自身のhigh/lowはTP/SL判定に
  一切使わない(同一足のhigh/lowで即座にexitが決まってしまう先読みを防ぐため)。読み込み範囲は
  `--to` までなので、`--to` 付近で生成されたシグナルは将来の足が無く `open` のまま残ることがある
  (想定内の挙動)。
- run自体(`backtest_runs`)とシグナル(`backtest_signals`)は既存の `backtest_store.py` の
  `save_backtest_run()` / `save_backtest_signals()` でそのまま保存する。

## 標準出力の集計例

`run` サブコマンドは、完了後に既存のBacktest Summary(バー数・シグナル数・result_status別件数)に
続けて、新しいSignal Summary(win/loss/pnl系)も自動的に表示する。

```
=== Backtest Summary ===
Symbol:              USD_JPY
Timeframe:           1h
Validation window:   2026-06-01T00:00:00+00:00 .. 2026-06-30T23:59:59+00:00
Warm-up candles:     120 (required >= 75)
Bars processed:      2880
Signals generated:   14
Bias mode:           static
Allowed states:      all
Suppressed:          market-state=0, re-entry=0, cooldown=0
Execution model:     fixed_spread_slippage (spread=0.0, slippage=0.0)
Result breakdown:
  open         3
  win          6
  loss         4
  expired      1
  invalidated  0
  closed       0
Run ID:              6f1a...
Backtest DB:         backtest/data/backtest_results.sqlite3

=== Backtest Signal Summary ===
Run ID:              6f1a...
Total signals:       14
Win / Loss:          6 / 4
Win rate:            0.6000
Total PnL:           3.2000
Average PnL:         0.3200
Profit factor:       1.8500
Max drawdown:        0.9000
Avg holding bars:    n/a
by_bias:
  Bull: signals=9 win=4 loss=2 win_rate=0.6667 total_pnl=2.5000
  Bear: signals=5 win=2 loss=2 win_rate=0.5000 total_pnl=0.7000
by_signal_type:
  trend_follow: signals=14 win=6 loss=4 win_rate=0.6000 total_pnl=3.2000
by_timeframe:
  1h: signals=14 win=6 loss=4 win_rate=0.6000 total_pnl=3.2000
by_market_state:
  trend_up: signals=9 win=5 loss=1 win_rate=0.8333 total_pnl=2.9000
  range: signals=5 win=1 loss=3 win_rate=0.2500 total_pnl=0.3000
```

## Signal Summary report (Issue #23)

`backtest/summary.py`(集計ロジック、DB・stdout・argparseに一切依存しない純関数)と
`backtest/report.py`(text/JSON/Markdownへのレンダリングとファイル保存)に責務を分離している。
どちらもCLIから独立して単体テスト可能。

`summary.build_summary(run_id, signals)` は、各signalが `run_id` フィールドを持つ場合、
それが引数の `run_id` と一致するか検証する。1件でも不一致があれば黙って除外・混入させず
`ValueError` を送出する(別runのSignalを誤って渡した際の誤集計を防ぐため)。`run_id` フィールド
自体を持たない内部/テスト用データはこの検証の対象外(比較対象が無いため)。

### Metrics定義

既存コード・schemaに定義があるものはそれを優先し(win/loss/pnlは `backtest_signals.result_status` /
`pnl` 列)、無いものはIssue #23の指示に従う。

| Metric | 定義 |
|---|---|
| `total_signals` | そのrun_idの `backtest_signals` 件数(open/unresolvedも含む)。 |
| `win_count` | `pnl > 0` の件数(`pnl is None` の未決済Signalは含まない)。 |
| `loss_count` | `pnl < 0` の件数。`pnl == 0` はwin/lossどちらにも含めない。 |
| `win_rate` | `win_count / (win_count + loss_count)`。分母(勝敗対象)が0件なら `0.0`。 |
| `total_pnl` | 決済済み(`pnl is not None`)Signalのpnl合計。未決済分は0扱いで捏造せず単純に除外。 |
| `average_pnl` | `total_pnl / 決済済み件数`。決済済みが0件なら `0.0`。 |
| `profit_factor` | `gross_profit / abs(gross_loss)`。ゼロ除算時の扱いは下記参照。 |
| `max_drawdown` | 決済済みSignalを`signal_time`昇順に並べた累積pnl equity curveの、過去最高値からの最大下落額(金額ベース、0以上の非負値)。0件/未決済のみなら `0.0`。 |
| `average_holding_bars` | 常に `null`。理由は下記「未実装の軸・指標」を参照。 |

`profit_factor` のゼロ除算方針(コード・テストで統一済み):

- 決済済みSignalが0件、または利益も損失も無い(相殺してゼロ) → `0.0`
- 利益はあるが損失が無い → 数学的に無限大になるため `null`(JSONにInfinityは出さない)
- それ以外 → 常に有限な `gross_profit / abs(gross_loss)`

`max_drawdown` は `signal_time` で明示的に時系列ソートしてから計算する(DBが返す行順に依存しない)。
未決済Signalはequity curveに含めない(未来情報の混入や0扱いの捏造を防ぐため)。

### 未決済Signalの扱い

`pnl` が `None` のSignal(=まだ `exit_policy.evaluate_exit()` がclose条件を検出していない)は、
`total_signals` には数えるが、win/loss判定・`total_pnl`・`average_pnl`・`profit_factor`・
`max_drawdown` からは完全に除外する。0扱いにはしない。

### `by_bias` / `by_signal_type` / `by_timeframe`(将来拡張用の軸)

`backtest_signals` に実在する列(`bias` / `signal_type` / `timeframe`)をそのままグルーピングキーに
使っており、推測値・ダミー値は一切含まない。値が無い(例: `bias` が `NULL`)Signalは
`"unknown"` バケットに入る(欠損の事実をそのまま表現するためで、値の捏造ではない)。
各バケットの中身は最上位と同じMetricsサブセット(`total_signals` 〜 `max_drawdown`)。

Issue #27以降、Backtest RunnerはPaper Signal候補の4分類フィールド(`signal_type` / `trigger_type` / `alert_source` / `strategy_profile`)をそのまま `backtest_signals` へ引き継ぐ。したがって `signal_type` には戦略的分類(現行は `trend_follow`)が入り、`by_signal_type` は戦略種別ごとの集計になる。`backtest_signals` テーブルにも `alert_source` / `strategy_profile` 列を持ち、既存DBには冪等migration(不足列を `ALTER TABLE ADD COLUMN` し、NULL・空文字を安全なデフォルト値へ補完)で追加する。候補が当該フィールドを持たない稀なケースのみ、`backtest_models.CLASSIFICATION_FIELD_DEFAULTS` の既定値にフォールバックする。

Issue #39 は同様に `market_state` 列を追加し、`by_market_state` 軸(相場局面別の勝率・PF)を提供する。ただし分類フィールドと違い **NULL のバックフィルはしない**: 古い行の当時の Market State は事後的に復元不能なため、捏造した既定値ではなく NULL(=`by_market_state` の `unknown` バケット)が正しい値となる。既存DBへの列追加は同じ冪等 `ALTER TABLE ADD COLUMN` migration による。

### 未実装の軸・指標: `average_holding_bars`

`backtest_signals` には保有バー数に相当する列が存在しない(`signal_time` / `closed_at` という
壁時計時刻のみ)。`closed_at - signal_time` から経過バー数を逆算するには、`timeframe` 文字列
(`15m` / `1h` / `4h` ...)ごとに1本あたりの時間幅を仮定する必要があり、これはIssue #23が
明示的に禁止する「推測」にあたる。そのため `average_holding_bars` は常に `null` とし、
DBに保有バー数相当の列が追加されない限り実装しない(Signalが0件でも決済済みSignalがあっても、
現行schemaでは変わらず `null`)。DB migrationや時刻差からの逆算は今回のIssue #23では行わない。
`backtest_signals` へ保有バー数(またはbar単位の経過本数)を保存する仕組みは、schema変更を伴う
別Issueの候補として切り出す。

### 0件時の出力例(`--format json`)

```json
{
  "run_id": "...",
  "total_signals": 0,
  "win_count": 0,
  "loss_count": 0,
  "win_rate": 0.0,
  "total_pnl": 0.0,
  "average_pnl": 0.0,
  "profit_factor": 0.0,
  "max_drawdown": 0.0,
  "average_holding_bars": null,
  "breakdowns": {
    "by_bias": {},
    "by_signal_type": {},
    "by_timeframe": {},
    "by_market_state": {}
  }
}
```

## テスト

```bash
python -m pytest backtest/tests -q
```

- `backtest/tests/test_runner.py`(Issue #33で新規追加、Issue #23でCLI統合テストを、
  Issue #24で `--exit-policy` / `--strategy-name` のテストを追記)
- `backtest/tests/test_backtest_engine.py`(Issue #25で新規追加。手書きOHLCV fixtureで
  Signal生成/Signal 0件/Exit到達(win・loss)/Invalidation到達のエンジン主要経路を固定。
  warm-up不足skipと「validation_endより後の足がwarm-upへ混入しない」回帰は
  `test_warmup.py` / `test_runner.py` の既存テストが担当しており、重複追加していない)
- `backtest/tests/candle_fixtures.py`(Issue #25で新規追加。テスト間で共有する
  小さなOHLCV fixtureビルダー。`test_runner.py` も同じビルダーを使うよう整理済み)
- `backtest/tests/test_summary.py`(Issue #23で新規追加、集計ロジックの単体テスト)
- `backtest/tests/test_report.py`(Issue #23で新規追加、レンダリング/ファイル保存の単体テスト)
- `backtest/tests/test_backtest_models.py`(Issue #23で `apply_exit_policy_result()` の
  pnl計算テストを追記。既存テストの内容は変更していない)
- Issue #39で `test_warmup.py`(各バーの Market State 判定と point-in-time / off-by-one 回帰)・
  `test_runner.py`(`--allowed-market-states` のパース/フィルタ/相互排他カウンタ/config・summary記録)・
  `test_backtest_store.py`(market_state 列の in-place migration と round-trip)・
  `test_summary.py`(`by_market_state` breakdown)へテストを追記
