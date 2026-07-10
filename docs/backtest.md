# Backtest Runner / CLI

Issue #33 (v1.4 Backtest)。既存の `backtest/backtest_models.py` / `backtest/backtest_store.py` /
`backtest/warmup.py` を再利用し、`symbol / timeframe / from / to` を受け取って過去のローソク足を
再生する Runner / CLI (`backtest/runner.py`) を追加した。

Issue #23 で、run単位のSignal明細を集計するSummary report(`backtest/summary.py` / `backtest/report.py`)
と、それを再取得する `summary` サブコマンドを追加した(後述)。

**Paper-only。** Discord通知、実発注、Broker API連携は一切行わない。読み取るのは
`market-watch/data/chart_data.sqlite3`(ローソク足)と `news-feed-bot/data/market_bias.sqlite3`
(Market Bias)のみで、書き込むのは既存の Backtest DB(`backtest_runs` / `backtest_signals`)のみ
(`summary` サブコマンドで `--output` を指定した場合のみ、指定したファイルにも書き込む)。

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
| `--run-name` | | 既定は `<symbol>_<timeframe>_<from>_<to>`。 |
| `--strategy-name` | | 既定は `paper_signal_generator`(=Paper Signal Generatorの既存ルールをそのまま再生していることを示す)。 |

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
| `2` | 引数エラー(必須引数の欠落、`--from`/`--to` のパース失敗、`--to` が `--from` より前、`--warmup-bars` が1未満、`--confidence-threshold` が0.0〜1.0の範囲外など)。argparse自体の使用エラーも同じ2を返す。 |
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

## Backtest結果の保存とEntry/Exitの扱い

- validation期間の各バーで `warmup.evaluate_backtest_bars()` がPaper Signal候補を生成する
  (bias + そのバーのindicator状態から `paper-signal/signal_generator.generate_signal_candidates()`
  を呼ぶだけで、DB・Discord・HTTPには一切触れない)。
- 候補が出たバー(`trigger_type="bar_close"`)について、そのバーの close 価格を仮の entry_price
  として扱う。Exit評価(`paper-signal/exit_policy.evaluate_exit()`、既定は `fixed_rr`)に渡す
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
  paper_signal: signals=14 win=6 loss=4 win_rate=0.6000 total_pnl=3.2000
by_timeframe:
  1h: signals=14 win=6 loss=4 win_rate=0.6000 total_pnl=3.2000
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
    "by_timeframe": {}
  }
}
```

## テスト

```bash
python -m pytest backtest/tests -q
```

- `backtest/tests/test_runner.py`(Issue #33で新規追加、Issue #23でCLI統合テストを追記)
- `backtest/tests/test_summary.py`(Issue #23で新規追加、集計ロジックの単体テスト)
- `backtest/tests/test_report.py`(Issue #23で新規追加、レンダリング/ファイル保存の単体テスト)
- `backtest/tests/test_backtest_models.py`(Issue #23で `apply_exit_policy_result()` の
  pnl計算テストを追記。既存テストの内容は変更していない)
- `test_backtest_store.py` / `test_warmup.py` は変更していない
