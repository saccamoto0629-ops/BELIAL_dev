# Paper Signal

v1.4.0 は、[Market Bias](market_bias.md) と Chart Snapshot(`market-watch/chart_snapshot.py`)をもとに、**paper_only のシグナル仮説**を生成・保存・Discord通知する `paper-signal/` を追加するリリースである。

## 目的

- Market Bias と Chart Snapshot(1h)から、ルールベースでシグナル候補を作る
- 生成したシグナルを SQLite に記録し、Discord へ通知できるようにする
- **実発注・自動売買は一切行わない。** 生成物はあくまで「仮説の記録」である

## 位置づけ

```text
Market Snapshot(可視化)─ Market Bias(市場状態の分類)
                                         │
                                         ├─ Chart Snapshot(1h, market-watch/chart_snapshot.py)
                                         │
                                         └─ Paper Signal Generator(paper-signal/, 本章)
                                                  │
                                          (まだ存在しない) Entry Bot
```

Paper Signal は、Market Bias が示す「市場の大まかな方向」と、Chart Snapshot が示す「チャート状態(1h)」を組み合わせて、シグナル仮説を記録するだけの層である。エントリー実行を担当する層(Entry Bot / Pepperoni)はv1.4.0では実装しない。

## 入力データ

`paper-signal/signal_generator.py` は、既存Providerが取得済みのデータのみを使う。自前でAPIを呼び出すのは `news-feed-bot/market_snapshot.fetch_snapshot_sources()` 経由のみである。

| 入力 | 由来 | 説明 |
|---|---|---|
| `market_bias` / `confidence` | `news-feed-bot/market_bias.classify_market_bias()` | `Bull` / `Bear` / `Neutral` と 0.0〜1.0 の confidence |
| Chart Snapshot(1h) | `fetch_snapshot_sources()["chart"]`(内部で `market-watch/chart_snapshot.py` を呼ぶ) | symbol / timeframe / trend / state / freshness / bias_weight / macd_state / rsi14 / rsi_state |

Paper Signal v1.4.0では、`rsi14` / `rsi_state` は記録・観測用の補助フィールドであり、候補生成ルールには使わない。

## 出力データ(Paper Signal)

`paper-signal/signal_models.py` の `create_paper_signal()` が返す。

```python
{
    "signal_id": "b3f1...",
    "created_at_utc": "2026-07-05T12:00:00Z",
    "symbol": "USDJPY",
    "timeframe": "1h",
    "side": "long",
    "market_bias": "Bull",
    "confidence": 0.72,
    "macd_state": "Bullish",
    "trend": "Bull",
    "state": "Inside band",
    "freshness": "Fresh",
    "reason": "Market Bias=Bull (72%); USDJPY 1h MACD=Bullish, Trend=Bull, State=Inside band, Freshness=Fresh.",
    "status": "paper_only",
}
```

`status` は常に `"paper_only"` であり、他の値を取ることはない。

## シグナル生成ルール

`paper-signal/signal_generator.build_candidates()` が判定する。

1. `market_bias` が `Bull` または `Bear` のときだけ候補にする(`Neutral` では生成しない)
2. `confidence` が `PAPER_SIGNAL_CONFIDENCE_THRESHOLD`(既定 `0.40`)以上のときだけ候補にする
3. Chart Snapshot の `1h` を代表時間足として使う(他の時間足は見ない)
4. Bull の場合: `macd_state == "Bullish"` かつ `freshness == "Fresh"` かつ `bias_weight > 0` → `side = "long"`
5. Bear の場合: `macd_state == "Bearish"` かつ `freshness == "Fresh"` かつ `bias_weight > 0` → `side = "short"`
6. `freshness` が `"Weekend Closed"` / `"Market Closed"` / `"Stale"` のいずれかの場合はシグナルを生成しない(`"Fresh"` のみ候補にする)

## 重複防止

`paper-signal/paper_signal_store.has_recent_signal()` により、同一 `symbol` / `timeframe` / `side` の組み合わせで `PAPER_SIGNAL_DUPLICATE_WINDOW_MINUTES`(既定60分)以内に生成済みの場合はスキップする。

## ストレージ

SQLiteに保存する。パスは `paper-signal/data/paper_signals.sqlite3`(`paper-signal/paper_signal_store.py`)。テーブルは29.4の全フィールドを1行1シグナルとして保持する。`paper-signal/data/` はGit管理しない(`.gitignore`)。

## Discord通知

本文の末尾に必ず固定文字列 `"Paper signal only. No trade execution."` を含める(`format_discord_message()`)。この制約は `tests/test_signal_generator.py` で検証する。

## 売買判断・自動発注は行わないこと(制約)

Paper Signal Generator は、以下を**実装しない**。

- 自動売買・自動発注
- 発注API接続(OANDA / XM / MT5 などbroker APIは使わない)
- ロット計算・レバレッジ計算
- 利確・損切り(stop-loss / take-profit)判断
- エントリー実行

`paper-signal/signal_generator.py` のソースコードは、broker APIのimportや発注関数呼び出しが存在しないことをテスト(`test_module_source_has_no_broker_api_integration`)で検証する。生成されるレコードの各フィールド値にも、order / lot / leverage / stop-loss / take-profit 等の語が含まれないことを検証する。
