# Market Bias

v1.3 は、Berial が取得している市場データ(Yahoo Finance / FRED / TradingView / Chart Snapshot)をもとに、**Bull / Bear / Neutral の3分類だけ**を返す初期ロジックを追加するリリースである。

## 目的

- 既存Providerが取得済みのデータから、市場の大まかな方向感をルールベースで判定する
- Chart Snapshot由来のTrend / BB / MACD状態をBias計算へ反映する
- Chart Snapshot由来のRSI14 / RSI Stateは表示・観測専用であり、Bias計算へは反映しない
- 判定結果、confidence、scores、reasons、warnings、note をDiscordへ出せる形にする
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
    "market_bias": "Bull",
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
}
```

## 仕様: market_bias / confidence / scores / reasons / warnings

#### market_bias

返却値は `Bull` / `Bear` / `Neutral` のいずれかのみとする。`Risk Off`、`USD Strength`、`Gold Strength` などの細かい市場状態ラベルは現行スコープ外であり、将来検討に回す。

- 入力データが不足している場合 → `Neutral`
- Bull / Bear のスコア差が小さい場合 → `Neutral`
- BullスコアがBearスコアを十分上回る場合 → `Bull`
- BearスコアがBullスコアを十分上回る場合 → `Bear`

#### confidence

`confidence` は 0.0～1.0 の範囲に収める。入力不足の場合は `0.0` とし、スコア差と評価できた材料数をもとに算出する。

#### scores

`scores` は `{"Bull": float, "Bear": float}` のdictで返す。Market Snapshot由来のVIX、VVIX、株価指数、米金利、Dollar Index、TradingView technical recommendation、Chart Snapshot由来のTrend / MACD Stateなどをルールベースで加点する。RSI14 / RSI Stateは現行版ではスコア入力にしない。

Chart Snapshotは `bias_weight` を掛けて反映する。

- `Fresh`: 原則 `1.0`
- `Weekend Closed` / `Market Closed`: 原則 `0.25`
- `Stale`: `0.0`、Bias計算から除外

MACD StateはTrendより軽い補助指標として扱う。例: `0.5 * bias_weight`。

#### reasons

`reasons` は判定に使った根拠のlistで返す。Discord表示では長くなりすぎないよう最大数を制限する。

Chart Snapshot由来の例:

```text
Chart XAUUSD 1h trend is Bull (weight 0.25)
Chart USDJPY 1h MACD is Bullish (weight 0.12)
```

#### warnings

TradingView recommendation と Chart Snapshot のTrend / MACD Stateが大きく矛盾する場合、必要に応じて `warnings` へ入れる。短期のMACDや1hの状態は補助シグナルであり、全体判断を支配しすぎないように扱う。

#### note

常に固定文字列 `"Observation only. No trade recommendation."` を含める。分岐によって売買推奨文に変化させない。

## 売買判断・自動発注は行わないこと(制約)

Market Bias Engine は、以下を**実装しない**。

- 自動売買・自動発注
- エントリー推奨
- 利確・損切り判断
- 直接的な売買指示
- 細かい `Risk Off`、`USD Strength`、`Gold Strength` などの複数ラベル分類
- バックテスト

TradingView由来の `BUY` / `SELL` は内部データとして扱うことは許容するが、ユーザー向けMarkdownや `note` に売買指示として出さない。この制約は `tests/test_market_bias.py` で検証する。

## Market Snapshotとの連携

`market_snapshot.build_market_snapshot_message()` は、本文内に `[Chart Snapshot Summary]` と `## Market Bias` セクションを追加する。生成時の例外は吸収し、Market Snapshot全体の投稿が壊れないようにする。

`Chart Snapshot Summary` では、各Symbolの1hを代表値として以下を表示する。

```text
- USDJPY 1h: Trend Neutral / State Inside band / Freshness Weekend Closed / Bias Weight 0.25 / BB Position 0.76 / MACD State Bullish / RSI14 58.40 / RSI State Neutral
```

Market BiasはこのSummary由来の `chart_data` を受け取り、TrendとMACD Stateをスコアへ反映する。RSI14 / RSI Stateは表示・観測専用であり、Market Biasスコアには反映しない。
