# Paper Signal Generator

Market Bias(`news-feed-bot/market_bias.py`)と Chart Snapshot(`market-watch/chart_snapshot.py` の1h足)を組み合わせて、**paper_only** のシグナル仮説を生成・保存・Discord通知するモジュールです。

**発注APIには接続しません。** OANDA / XM / MT5 などのbroker APIは使わず、order / position / lot / leverage / stop-loss / take-profit のいずれも実装しません。生成される全レコードの `status` は常に `"paper_only"` です。

## 1. Setup

```powershell
cd C:\Users\kinniki\ClaudeLocalSession\opt\BELIAL_dev
python -m pip install -r requirements.txt
copy paper-signal\.env.example paper-signal\.env
```

`paper-signal/.env` に `DISCORD_WEBHOOK_URL` を設定してください(未設定でも動作しますが、Discord通知はスキップされます)。

このモジュールは `news-feed-bot/market_snapshot.py` の `fetch_snapshot_sources()`(Yahoo Finance / FRED / TradingView / Chart Snapshot 1h)と `news-feed-bot/market_bias.py` の `classify_market_bias()` を再利用します。追加のAPIキーは不要です(FREDのみ `news-feed-bot/.env` の `FRED_API_KEY` が未設定だとFREDセクションのみスキップされます)。

## 2. Run

```powershell
cd C:\Users\kinniki\ClaudeLocalSession\opt\BELIAL_dev\paper-signal

# 一度だけ確認(Discord送信・SQLite保存なし)
python signal_generator.py --once --dry-run

# 一度だけ実行(Discord送信・SQLite保存あり)
python signal_generator.py --once
```

`--once` を付けない場合は `PAPER_SIGNAL_CHECK_INTERVAL_SECONDS`(既定900秒)ごとにループします。

## 3. Signal generation rules

1. Market Bias が `Bull` または `Bear` のときだけ候補にする(`Neutral` では生成しない)
2. confidence が `PAPER_SIGNAL_CONFIDENCE_THRESHOLD`(既定 `0.40`)以上のときだけ候補にする
3. Chart Snapshot の `1h` を代表時間足として使う
4. Bull: `macd_state == Bullish` かつ `freshness == Fresh` かつ `bias_weight > 0` → `side = long`
5. Bear: `macd_state == Bearish` かつ `freshness == Fresh` かつ `bias_weight > 0` → `side = short`
6. `Weekend Closed` / `Market Closed` / `Stale` の場合はシグナルを生成しない
7. 同一 `symbol` / `timeframe` / `side` で `PAPER_SIGNAL_DUPLICATE_WINDOW_MINUTES`(既定60分)以内に重複生成しない

## 4. Storage

`paper-signal/data/paper_signals.sqlite3` にSQLiteで保存します(`paper_signal_store.py`)。レコードは以下を含みます。

`signal_id` / `created_at_utc` / `symbol` / `timeframe` / `side` / `market_bias` / `confidence` / `macd_state` / `trend` / `state` / `freshness` / `reason` / `status`(常に `paper_only`)

## 5. Discord notification

本文の末尾に必ず `Paper signal only. No trade execution.` を含めます。テスト(`tests/test_signal_generator.py`)で発注・ロット・レバレッジ等の語が含まれないことも検証しています。

## 6. Tests

```powershell
cd C:\Users\kinniki\ClaudeLocalSession\opt\BELIAL_dev
python -m pytest paper-signal -q
```

## 7. Security notes

- `paper-signal/.env` をコミットしない
- `paper-signal/data/`(SQLite)はruntime生成物のためコミットしない
- `logs/paper_signal_generator.log` はruntime生成物のためコミットしない
