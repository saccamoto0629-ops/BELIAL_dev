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
                                                  ├─ Paper Execution(次フェーズ・未実装)
                                                  └─ Live Execution(将来・未実装)
```

Paper Signal は、Market Bias が示す「市場の大まかな方向」と、Chart Snapshot が示す「チャート状態(1h)」を組み合わせて、シグナル仮説を記録するだけの層である。Paper Signal 自身は entry / exit を一切決めない。

- **Paper Execution**: Paper Signal を入力として、時間進行に沿って仮想約定・仮想ポジション管理・Exit・損益計算を行う将来レイヤー。v1.4 では**未実装**。
- **Live Execution**: Broker API を利用した実注文・実約定・実ポジション管理の将来レイヤー。**未実装**。

Paper Execution も Live Execution も Paper Signal の外側の別レイヤーであり、いずれも未実装。本章(Paper Signal)では扱わない。

なお、生成済みの Paper Signal を過去のローソク足で遡及検証する **Backtest** は、この執行フローとは独立した検証基盤(Validation)である。Backtest 内部の Exit 判定(`paper-signal/exit_policy.py`)や仮想約定モデル(`backtest/execution_model.py` の spread / slippage / fill)は検証用のシミュレーションであって、Paper Execution レイヤーの実装ではない(詳細は [Backtest](../backtest.md))。

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
    "signal_type": "trend_follow",
    "trigger_type": "macd_state_alignment",
    "alert_source": "composite",
    "strategy_profile": "market_bias_macd_alignment_v1",
    "status": "paper_only",
}
```

`status` は常に `"paper_only"` であり、他の値を取ることはない。

### 分類フィールド(Issue #27)

Signalの戦略的分類(Strategy / Method層)と、生成契機・入力元(Alert層)を区別して追跡するための4フィールド。いずれも記録用であり、entry / exit / サイズ / 発注等の判断には一切用いない。定数は `paper-signal/signal_models.py` に一元定義する(`CLASSIFICATION_FIELD_DEFAULTS`)。

| フィールド | 現行値 | 意味 |
| --- | --- | --- |
| `signal_type` | `trend_follow` | Signalの戦略的分類(トレンドフォロー) |
| `trigger_type` | `macd_state_alignment` | 生成契機。Market BiasとMACD**状態**の整合で候補化していることを表す |
| `alert_source` | `composite` | 入力元。単一イベント検知ではなく複数入力の合成であること |
| `strategy_profile` | `market_bias_macd_alignment_v1` | 適用ロジックのプロファイル名 |

現行ロジックは**MACDクロスイベントを直接検知していない**。Market Biasと現在のMACD状態の整合によって候補を生成しているため、`trigger_type` は `macd_cross` ではなく `macd_state_alignment` とする(クロス検知を入力としていないため、`macd_cross` と記録すると事実と異なる)。

古い形式のcandidate(4フィールドを持たない)を `create_paper_signal()` に渡した場合も、`.get()` により上表の既定値で補完されるため後方互換を保つ。`paper_signals` テーブルはこの4列をTEXTで持ち、既存DBには `init_db()` の冪等マイグレーション(`PRAGMA table_info` で不足列だけ `ALTER TABLE ... ADD COLUMN`、`NOT NULL DEFAULT` で既存行を補完)で追加する。

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

SQLiteに保存する。パスは `paper-signal/data/paper_signals.sqlite3`(`paper-signal/paper_signal_store.py`)。テーブルは上記「出力データ(Paper Signal)」の全フィールドを1行1シグナルとして保持する。`paper-signal/data/` はGit管理しない(`.gitignore`)。

Market Bias自体は `news-feed-bot/data/market_bias.sqlite3` に別途保存される(Issue #17、詳細は [Market Bias](market_bias.md#永続化とbacktest参照issue-17))。Paper Signalは現状 `classify_market_bias()` をその場で呼び出しており、このDBを読み取らない。`market_bias_store.get_market_bias_as_of()` はDB不在・レコード不在時に例外を投げず `None` を返す設計のため、将来 Paper Signal や Backtest がこのAPIを使うように変わっても、Market Biasが存在しない期間で異常終了しない。

## Discord通知

本文の末尾に必ず固定文字列 `"Paper signal only. No trade execution."` を含める(`format_discord_message()`)。この制約は `tests/test_signal_generator.py` で検証する。

## 売買判断・自動発注は行わないこと(制約)

Paper Signal Generator は、以下を**実装しない**。

- 自動売買・自動発注
- 発注API接続(OANDA / XM / MT5 などbroker APIは使わない)
- ロット計算・レバレッジ計算
- 利確・損切り(stop-loss / take-profit)判断
- エントリー実行

これらの entry / exit / 発注は Paper Signal の外側の執行層(**Paper Execution** / **Live Execution**)の責務であり、いずれも未実装である。Paper Signal Generator 自身は entry / exit をいっさい決めない。なお TP / SL の評価は Backtest(検証基盤)が過去データ上で遡及的に行うが、これは検証用シミュレーションであって Paper Execution レイヤーの実装ではない。

`paper-signal/signal_generator.py` のソースコードは、broker APIのimportや発注関数呼び出しが存在しないことをテスト(`test_module_source_has_no_broker_api_integration`)で検証する。生成されるレコードの各フィールド値にも、order / lot / leverage / stop-loss / take-profit 等の語が含まれないことを検証する。
