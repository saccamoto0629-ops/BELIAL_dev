# Market Bias

v1.3 は、Berial が取得している市場データ(Yahoo Finance / FRED / TradingView / Chart Snapshot)をもとに、Bull / Bear / Neutral の3分類(Trend軸)を返す初期ロジックを追加するリリースである。その後、Trendとは独立した Risk(Risk-On / Risk-Off / Neutral)軸と Volatility(Low / Normal / High)軸を追加し(Issue #3)、判定結果をSQLiteへ永続化してBacktestから時点参照できるようにした(Issue #17)。

## 目的

- 既存Providerが取得済みのデータから、市場の大まかな方向感をルールベースで判定する
- Chart Snapshot由来のTrend / BB / MACD状態をBias計算へ反映する
- Chart Snapshot由来のRSI14 / RSI Stateは表示・観測専用であり、Bias計算へは反映しない
- Trend(方向感)とは独立に、Risk-On / Risk-Off(相場環境フィルタ)と Volatility(値動きの大きさ)を判定する
- 判定結果、confidence、scores、reasons、warnings、note をDiscordへ出せる形にする
- 判定結果をSQLiteへ保存し、Backtestや他コンポーネントから過去時点の状態を参照できるようにする
- 「観測結果の可視化」を一段進めるものであり、売買判断や自動発注を行う機能ではない

## Market Bias Engine の位置づけ

`news-feed-bot/market_bias.py` は、Market Snapshotが集約したデータを読む補助レイヤーである。

```text
Market Snapshot(市場データの可視化)
        ├─ TradingView Summary
        ├─ Chart Snapshot Summary
        └─ Market Bias Engine(Bull / Bear / Neutral の3分類)
```

Market Bias Engine は、発注・執行・売買推奨を行うレイヤーには接続しない。

## 入力データ

`market_bias.classify_market_bias()` は以下を入力として受け取る。いずれも既存Providerがすでに取得済みのデータであり、Market Bias Engine自身は外部APIを呼び出さない。

| 入力 | 由来 | 必須 |
|---|---|---|
| `market_data` | `market_data.get_market_data()` (Yahoo Finance) | 任意(欠損可) |
| `fred_data` | `fred_data.get_fred_data()` (FRED) | 任意(`None`可) |
| `tv_data` | `tradingview_data.get_tradingview_data()` (TradingView TA) | 任意(欠損可) |
| `chart_data` | `market-watch/chart_snapshot.py` の `IndicatorSnapshot` summary | 任意(欠損可) |
| `macd_signal` | `macd_alert_bot.py` が検知した直近MACDクロス方向 | 任意。現在は主入力ではなく、イベント通知用の補助扱い |

`chart_data` は以下の値を含む。

- `symbol`
- `timeframe`
- `trend`
- `state`
- `freshness`
- `bias_weight`
- `bb_position`
- `macd_state`
- `rsi14`
- `rsi_state`

`rsi14` / `rsi_state` are accepted as observation fields only. They may appear in summaries and downstream records, but Market Bias scoring ignores them in the current version.

すべての入力は欠損・部分欠損があっても例外を投げず、判定可能な範囲で分類を行う。

## 出力データ

```python
{
    "market_bias": "Bull",        # trend と同値。後方互換のため維持するフィールド名
    "confidence": 0.40,
    "scores": {"Bull": 3.5, "Bear": 1.5},
    "reasons": [
        "VIX is below 18",
        "S&P500 is rising",
        "SPX technical recommendation is positive",
        "Chart USDJPY 1h MACD is Bullish (weight 0.12)",
    ],
    "warnings": [],
    "note": "Observation only. No trade recommendation.",

    # Trend / Risk / Volatility を独立した軸として表示するための追加フィールド(Issue #3)
    "trend": "Bull",
    "risk": "Risk-On",
    "risk_confidence": 0.50,
    "risk_scores": {"Risk-On": 2.0, "Risk-Off": 0.0},
    "risk_reasons": ["VIX is below 18 (risk-on)", "S&P500 is rising (risk-on)"],
    "volatility": "Normal",
    "volatility_confidence": 0.30,
    "volatility_scores": {"High": 0.0, "Normal": 1.0, "Low": 0.0},
    "volatility_reasons": [],
}
```

## 仕様: market_bias / confidence / scores / reasons / warnings(Trend軸)

#### market_bias / trend

`market_bias` は `Bull` / `Bear` / `Neutral` のいずれかのみを返す。`trend` は `market_bias` と同じ値を持つエイリアスであり、Trend / Risk / Volatility を軸として明示するために追加した(Issue #3)。既存の呼び出し側(Paper Signal など)は引き続き `market_bias` を参照できる。

- 入力データが不足している場合 → `Neutral`
- Bull / Bear のスコア差が小さい場合 → `Neutral`
- BullスコアがBearスコアを十分上回る場合 → `Bull`
- BearスコアがBullスコアを十分上回る場合 → `Bear`

#### confidence

`confidence` は 0.0～1.0 の範囲に収める。入力不足の場合は `0.0` とし、スコア差と評価できた材料数をもとに算出する。Trend軸のconfidenceであり、Risk / Volatility軸はそれぞれ独立した `risk_confidence` / `volatility_confidence` を持つ。

#### scores

`scores` は `{"Bull": float, "Bear": float}` のdictで返す。Market Snapshot由来のVIX、VVIX、株価指数、米金利、Dollar Index、TradingView technical recommendation、Chart Snapshot由来のTrend / MACD Stateなどをルールベースで加点する。RSI14 / RSI Stateは現行版ではスコア入力にしない。

Chart Snapshotは `bias_weight` を掛けて反映する。

- `Fresh`: 原則 `1.0`
- `Weekend Closed` / `Market Closed`: 原則 `0.25`
- `Stale`: `0.0`、Bias計算から除外

MACD StateはTrendより軽い補助指標として扱う。例: `0.5 * bias_weight`。

#### reasons

`reasons` は判定に使った根拠のlistで返す。Discord表示では長くなりすぎないよう最大数を制限する。Trend軸の根拠のみを含み、Risk / Volatilityの根拠はそれぞれ `risk_reasons` / `volatility_reasons` に分離する(Issue #3)。

Chart Snapshot由来の例:

```text
Chart XAUUSD 1h trend is Bull (weight 0.25)
Chart USDJPY 1h MACD is Bullish (weight 0.12)
```

#### warnings

TradingView recommendation と Chart Snapshot のTrend / MACD Stateが大きく矛盾する場合、必要に応じて `warnings` へ入れる。短期のMACDや1hの状態は補助シグナルであり、全体判断を支配しすぎないように扱う。

#### note

常に固定文字列 `"Observation only. No trade recommendation."` を含める。分岐によって売買推奨文に変化させない。

## 仕様: risk / risk_confidence / risk_reasons(Risk軸)

Trendとは独立に、相場のリスク選好(Risk-On / Risk-Off)を判定する。売買方向の判断ではなく、相場環境フィルタとして扱う。

- 返却値は `Risk-On` / `Risk-Off` / `Neutral` のいずれか。
- Risk-Off寄りの材料: VIX上昇、S&P500 / Nasdaq下落、原油急騰、Gold上昇(安全資産買い)。
- Risk-On寄りの材料: VIX低下、S&P500 / Nasdaq上昇、Gold軟調。
- `risk_reasons` にRisk軸の根拠だけを分離して記録する。
- 地政学ニュースなど、現行Providerが未取得のシグナルは将来のProvider拡張時に追加する。

## 仕様: volatility / volatility_confidence / volatility_reasons(Volatility軸)

Trend / Riskとは独立に、値動きの大きさ(Low / Normal / High)を判定する最小実装。

- 返却値は `Low` / `Normal` / `High` のいずれか。
- 判定材料: Chart Snapshot由来のBollinger Band幅(`bb_width_pct`)、直近の値幅・急変率。
- `volatility_reasons` にVolatility軸の根拠だけを分離して記録する。

Trend / Risk / Volatility は互いに独立した軸のため、USD/JPYのように複数要因がぶつかる相場でも、Trendが `Neutral` になる一方でRiskやVolatilityは明確な値を返せる。

## 売買判断・自動発注は行わないこと(制約)

Market Bias Engine は、以下を**実装しない**。

- 自動売買・自動発注
- エントリー推奨
- 利確・損切り判断
- 直接的な売買指示
- Backtestロジック本体(判定エンジン自身はバックテストを実行しない。Issue #17 により判定結果をDBへ保存し、**別レイヤーのBacktestが読み取れる**ようにしただけであり、Market Bias Engine自体が過去検証や最適化を行うわけではない)

TradingView由来の `BUY` / `SELL` は内部データとして扱うことは許容するが、ユーザー向けMarkdownや `note` に売買指示として出さない。この制約は `tests/test_market_bias.py` で検証する。

## 永続化とBacktest参照(Issue #17)

Market Biasの評価結果は `news-feed-bot/market_bias_store.py` を通じてSQLiteへ保存する。

- DB: `news-feed-bot/data/market_bias.sqlite3`(`MARKET_BIAS_DB_PATH` で上書き可能。相対パスは `news-feed-bot/` 基準)
- テーブル: `market_bias`(`market_bias_id`, `evaluated_at`, `symbol`, `timeframe`, `market_bias`, `confidence`, `trend`, `risk`, `risk_confidence`, `volatility`, `volatility_confidence`, `reasons_json`, `risk_reasons_json`, `volatility_reasons_json`, `warnings_json`, `source_snapshot_id`)
- レコードは `market_bias_models.create_market_bias_record()` が組み立てる。`symbol` は既定で `GLOBAL`(Market Biasは市場全体を見るクロスアセット判定のため)、`timeframe` は既定で `1h`(Chart Snapshot入力の代表時間足)。Issue #40 以降は銘柄別のMarket Biasも保存され、その場合は正規化した銘柄名(例 `USDJPY`)で上書きされる(下記「銘柄別Market BiasとGLOBALフォールバック」参照)。
- 保存タイミング: `market_snapshot.build_market_snapshot_message()` がMarket Bias分類に成功した直後。`dry_run=True` のときはDBに一切書き込まない。
- 参照: `market_bias_store.get_market_bias_as_of(symbol, timeframe, at)` は、指定時刻 `at` **以前**で最新のレコードを返す(未来のレコードを漏らさない、Backtestのlook-ahead防止のための設計)。DBやテーブルが存在しない場合、または該当レコードがない場合は例外を投げず `None` を返す。この関数は読み取り専用で、DBファイルを新規作成しない。
- Paper Signal / Backtest からこのAPIを呼ぶ場合、`None` を「その期間はMarket Bias不明」として扱い、処理を継続する想定である。

## 銘柄別Market BiasとGLOBALフォールバック(Issue #40)

Issue #40 で、市場全体を見る `GLOBAL` Biasに加えて、**銘柄別**のMarket Biasを保存・参照できるようにした。`USDJPY` が Bull でも `XAUUSD` / `SPX` が同じ方向とは限らないため、対象銘柄のBiasがあればそれを優先し、なければ従来どおり `GLOBAL` にフォールバックする。既存の `GLOBAL` のみのDBはそのまま従来動作となる(後方互換)。

### 銘柄表記の正規化

`USDJPY`(Chart Snapshot / Chart DB)と `USD_JPY`(Backtest `--symbol`)、`XAU/USD` などの表記揺れは `market_bias_models.normalize_bias_symbol()` で1つの正規形(アンダースコア/スラッシュを除去した大文字、例 `USD_JPY` → `USDJPY`)へ統一する。銘柄別Biasはこの正規形で保存・参照する。`GLOBAL` は正規化しても `GLOBAL` のまま。

### 参照優先順位(point-in-time)

`market_bias_store.get_effective_market_bias_as_of(symbol, timeframe, at)` は、指定時刻 `at` 以前で以下の優先順位で最初に見つかったレコードを返す(`market_bias_models.effective_bias_scopes()` が生成、重複スコープは1回だけ検索)。

1. `symbol` + `timeframe`
2. `symbol` + `1h`(代表時間足)
3. `GLOBAL` + `timeframe`
4. `GLOBAL` + `1h`

各スコープは `get_market_bias_as_of()` に委譲するため、`evaluated_at <= at`(未来を見ない)と同時刻の `market_bias_id DESC` タイブレークをそのまま継承する。戻り値はレコード自身の `symbol` / `timeframe` を保持するので、呼び出し側はどのスコープ(GLOBALフォールバックか否か)が採用されたか判別できる。DB/テーブル不在時は副作用なく `None`。

### 時系列参照(GLOBALが銘柄別を上書きしない)

`load_effective_market_bias_series(symbol, timeframe)` は、時系列Backtest向けに上記フォールバックを**各時点で**適用したマージ系列を返す。単純に銘柄別系列とGLOBAL系列を連結して `evaluated_at` で並べると、銘柄別Biasが有効になった後に保存された**新しいGLOBALレコードが銘柄別を上書き**してしまう。これを避けるため、各スコープの系列を一度だけ事前ロードし、全スコープの `evaluated_at` を区切り点として各時点の有効Biasを解決する。結果は「有効Biasが変化する点だけ」を含む最小系列で、`evaluated_at` が厳密増加するため、`runner.TimeSeriesBiasLookup` がそのまま二分探索で正しい有効Biasを引ける。

- 銘柄別Biasがまだ無い期間だけ `GLOBAL` を使う
- 一度銘柄別Biasが有効になったら、以降に保存された新しい `GLOBAL` では上書きされない
- 銘柄別レコード間では直近の銘柄別Biasを継続利用
- 未来情報は使わない

### 銘柄別Biasの保存

`market_snapshot.build_market_snapshot_message()` は、従来の `GLOBAL` 保存(`persist_market_bias()`、先に実行)に続けて `persist_symbol_market_biases()` を呼び、Chart Snapshotに含まれる各銘柄について銘柄別Biasを保存する。銘柄別Biasは `market_bias.classify_market_bias_for_symbol()` で計算し、**マクロ/クロスアセットのcore・Risk・Volatilityは共有しつつ、chart_dataを対象銘柄のみに絞る**ことで、他銘柄のchart trend / MACDが対象銘柄の方向性スコアへ混入しないようにする。`dry_run=True` では `GLOBAL`・銘柄別ともにDBへ書き込まない。保存は観測ログのみで、売買推奨やExecutionは行わない。

### Backtest / Paper Signal からの利用

- Backtest(`backtest/runner.py`)は、対象runの `--symbol` / `--timeframe` を使って有効Biasを解決する(以前の `GLOBAL` / `1h` 固定参照から変更)。
  - static mode: `get_effective_market_bias_as_of(symbol, timeframe, --from)` を全バーへ適用。
  - time-series mode(`--time-series-bias`): `load_effective_market_bias_series(symbol, timeframe)` を `TimeSeriesBiasLookup` に渡し、各バーの close 時点で有効Biasを解決。
  - どちらも銘柄別があれば銘柄別、なければ `GLOBAL` フォールバック。Biasが一件も無ければ Neutral placeholder(候補生成なし)。
  - `backtest_runs.config_json.bias_resolution` に、要求した symbol/timeframe・正規化名・スコープ優先順位・採用スコープ(または利用スコープ集合)・GLOBALフォールバック有無を記録し、summary/ログ(`Bias scope:` 行)でも確認できる。
- Paper Signal(`paper-signal/signal_generator.py`)は live 実行時にBiasをin-memoryで計算する(DBは読まない)。詳細は [Paper Signal](paper_signal.md#銘柄別market-biasの適用issue-40)。

## Market Snapshotとの連携

`market_snapshot.build_market_snapshot_message()` は、本文内に `[Chart Snapshot Summary]` と `## Market Bias` セクションを追加し、Trend / Risk / Volatilityをそれぞれ表示する。生成時の例外は吸収し、Market Snapshot全体の投稿が壊れないようにする。Market Bias分類が成功した場合は、続けて上記の永続化も行う(dry-run時を除く)。

`Chart Snapshot Summary` では、各Symbolの1hを代表値として以下を表示する。

```text
- USDJPY 1h: Trend Neutral / State Inside band / Freshness Weekend Closed / Bias Weight 0.25 / BB Position 0.76 / MACD State Bullish / RSI14 58.40 / RSI State Neutral
```

Market BiasはこのSummary由来の `chart_data` を受け取り、TrendとMACD Stateをスコアへ反映する。RSI14 / RSI Stateは表示・観測専用であり、Market Biasスコアには反映しない。
