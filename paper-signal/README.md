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

実行エントリーポイントは `paper_signal_bot.py`(常駐ロジック本体は `signal_generator.py`。`python signal_generator.py ...` も後方互換で同等)。

```powershell
cd C:\Users\kinniki\ClaudeLocalSession\opt\BELIAL_dev\paper-signal

# 一度だけ確認(永続副作用なし: SQLite保存・Discord送信・chart DB・logfile を作らない)
python paper_signal_bot.py --once --dry-run

# 一度だけ実行(Discord送信・SQLite保存あり、sleepなし)
python paper_signal_bot.py --once

# 常駐(systemd ExecStart と同じ)
python paper_signal_bot.py
```

`--once` を付けない場合は `PAPER_SIGNAL_CHECK_INTERVAL_SECONDS`(既定900秒)ごとにループします。1周期(`run_once`)と常駐ループ(`run_service`)は分離され、SIGTERM / SIGINT で安全に停止します。systemd 配備は [docs/operations/operations.md](../docs/operations/operations.md) と `scripts/systemd/berial-paper-signal.service` を参照。

## 2.1 Daily report(日次集計, Issue #47)

`paper_signal_daily_report.py` は、保存済み Paper Signal を **指定日の1日分**だけ集計し、symbol / timeframe / direction(long/short)/ market state / strategy 別の件数と confidence の平均・最小・最大を text または Markdown で出力します。**読み取り専用で永続副作用なし**: signal DB を書き換えず(存在しなければ作成もせず)、Discord 送信も broker/Execution API 呼び出しも一切しません(唯一の書き込みは `--output` / `--output-dir` 指定時のレポートファイルのみ)。仮想約定・PnL・勝率などの Execution 系指標はスコープ外です。

```powershell
cd C:\Users\kinniki\ClaudeLocalSession\opt\BELIAL_dev\paper-signal

# 今日(設定タイムゾーン基準)のレポートを標準出力へ
python paper_signal_daily_report.py

# 特定日を Markdown でファイル出力
python paper_signal_daily_report.py --date 2026-07-13 --format markdown --output reports/paper_signal_2026-07-13.md

# 日付境界を別タイムゾーンで判定
python paper_signal_daily_report.py --date 2026-07-13 --timezone UTC

# 前日分(--timezone 基準)を reports/paper_signal_YYYY-MM-DD.md へ自動命名で保存
# (Issue #71 の systemd timer が毎日 00:10 JST に実行する形)
python paper_signal_daily_report.py --previous-day --timezone Asia/Tokyo --format markdown --output-dir reports
```

- `--date`(既定: 設定タイムゾーンの今日, `YYYY-MM-DD`)/ `--previous-day`(タイムゾーン解決後の前日。`--date` と排他)/ `--timezone`(既定: `$PAPER_SIGNAL_REPORT_TIMEZONE` → `.env` → `Asia/Tokyo`)/ `--format text|markdown` / `--output` / `--output-dir`(対象日から自動命名・atomic上書き。`--output` と排他)/ `--db-path`。
- DB異常(不存在 / ディレクトリ / 非SQLite / `paper_signals` テーブル・`created_at_utc` カラム欠如 / 読込エラー)は0件レポートに化けず、stderrへ原因を出して非0終了する(exit 1)。usage/設定エラーは exit 2。正常なDBで対象日0件は exit 0 で0件レポートを生成する(Issue #71)。
- 日付境界は**設定タイムゾーンの現地0時〜翌0時**を UTC に変換した半開区間 `[start, end)` で一貫して扱うため、`2026-07-13T15:00:00Z` の Signal は JST では翌日、UTC ではその日、と取り違えなく集計されます。
- market state / strategy が NULL・空文字・`unknown` のものは欠落させず明示的に `unknown` バケットへ集計するので、各軸の合計は常に `Total Signals` と一致します。0件の日も例外終了せずレポートを生成します。
- 日次定期実行への組み込みは [docs/operations/operations.md](../docs/operations/operations.md) を参照。

## 3. Signal generation rules

1. Market Bias が `Bull` または `Bear` のときだけ候補にする(`Neutral` では生成しない)
2. confidence が `PAPER_SIGNAL_CONFIDENCE_THRESHOLD`(既定 `0.40`)以上のときだけ候補にする
3. Chart Snapshot の `1h` を代表時間足として使う
4. Bull: `macd_state == Bullish` かつ `freshness == Fresh` かつ `bias_weight > 0` → `side = long`
5. Bear: `macd_state == Bearish` かつ `freshness == Fresh` かつ `bias_weight > 0` → `side = short`
6. `Weekend Closed` / `Market Closed` / `Stale` の場合はシグナルを生成しない
7. **確定足のみ**を使う。symbol/timeframe ごとに対象確定足(closed candle)の open timestamp を Chart Snapshot から明示的に決定し、未確定足は使わない(Issue #41、共有ルール `market-watch/freshness.py`)
8. Market Bias と Chart Snapshot は `symbol` / `timeframe` / target candle timestamp が一致する場合のみ組み合わせる。欠損・不一致は正常系skip(理由をログ出力)
9. 重複防止は **確定足単位の冪等性**。`(strategy_profile, symbol, timeframe, target_candle_utc)` の UNIQUE 制約で、多重起動・競合・再周期でも同一 target candle を重複保存・重複通知しない

## 4. Storage

`paper-signal/data/paper_signals.sqlite3` にSQLiteで保存します(`paper_signal_store.py`)。レコードは以下を含みます。

`signal_id` / `created_at_utc` / `symbol` / `timeframe` / `side` / `market_bias` / `confidence` / `macd_state` / `trend` / `state`(Bollinger状態) / `market_state`(Issue #47、旧行はNULL=`unknown`) / `freshness` / `reason` / `signal_type` / `trigger_type` / `alert_source` / `strategy_profile` / `status`(常に `paper_only`)/ `target_candle_utc`(Issue #41: 対象確定足のopen timestamp, UTC `...Z`)

保存順は「候補生成 → SQLite保存 → 保存成功後にDiscord通知」で、通知失敗時も保存済みSignalは残します(`save_signal_for_candle()` は新規挿入時のみ通知)。既存DBには `init_db()` の冪等マイグレーションで `target_candle_utc` 列とUNIQUEインデックスを追加します(旧データは非破壊)。

常駐サービスとしての起動・終了・冪等性・restart は [operations.md](../docs/operations/operations.md) と [paper_signal.md](../docs/architecture/paper_signal.md) を参照。

## 5. Discord notification

本文の末尾に必ず `Paper signal only. No trade execution.` を含めます。テスト(`tests/test_signal_generator.py`)で発注・ロット・レバレッジ等の語が含まれないことも検証しています。

## 6. Tests

```powershell
cd C:\Users\kinniki\ClaudeLocalSession\opt\BELIAL_dev
python -m pytest paper-signal -q
```

## 7. Backtest Exit Policy evaluation (paper-only)

`exit_policy.py` は、確定済みのpaper signal(`entry_price` / `direction` など)と過去ローソク足から、**このsignalが過去データ上でどう終わったか**を判定するBacktest専用モジュールです。

**これはExecution層の自動Exitではありません。** 実ポジションの決済、損切り注文、利確注文、Broker API連携は一切実装していません。`evaluate_exit()` はSQLite・Discord・HTTPのいずれにも触れない純粋関数で、過去足に対する事後的なマークを返すだけです。詳細は `exit_policy.py` のモジュールdocstringを参照してください。

用語:

- `entry_price` / `invalidation_price` / `take_profit_price`: いずれもpaper-only。実注文の価格ではありません。
- `close_price` / `close_time` / `close_reason`: Exit Policyが「終了」と判定した時点の、過去足に対するシミュレーション結果です。

### 7.1 設定はJSON config、`.env` にはパスだけ

Exit Policyの判断値(どのpolicyを使うか、risk:reward、保有本数上限、MA期間、同一足TP/SL優先度など)は **`.env` に直接置きません**。`.env` に置くのは設定ファイルへのパスだけです。

`paper-signal/.env`:

```env
BACKTEST_EXIT_CONFIG_PATH=config/backtest_exit_policy.json
```

`paper-signal/config/backtest_exit_policy.json`(コミット済みの実例。秘密情報を含まないため直接編集して使えます):

```json
{
  "exit_policy": "fixed_rr",
  "same_candle_priority": "stop_loss",
  "policies": {
    "fixed_rr": {
      "enabled": true,
      "risk_reward": 2.0,
      "stop_loss_mode": "fixed_pct",
      "fallback_stop_pct": 0.003
    },
    "time_based_close": {
      "enabled": true,
      "max_hold_bars": 48,
      "close_as": "expired"
    },
    "ma_break_invalidation": {
      "enabled": false,
      "ma_type": "sma",
      "period": 20
    }
  }
}
```

`BACKTEST_EXIT_CONFIG_PATH` が未設定の場合は、安全な組み込みdefault(`default_exit_policy_config()`、上記とほぼ同内容)を使います。値の読み込み・default補完・検証は `load_exit_policy_config()` / `resolve_exit_policy_config()` / `validate_exit_policy_config()` が担当します。JSONの一部だけ書けば残りはdefaultで補完されます。

### 7.2 Exit Policy一覧

| policy | 判定 | status | close_reason |
|---|---|---|---|
| `fixed_rr`(既定) | entry/direction と `fallback_stop_pct`(entry比%のstop幅)・`risk_reward`(またはsignal自身のinvalidation_price/take_profit_price)から、ローソク足のhigh/lowが先にどちらへ到達したかで判定 | `win` / `loss` | `take_profit` / `stop_loss` |
| `time_based_close` | `max_hold_bars` 本を超えたら強制終了。価格水準は見ない。statusは `close_as`(既定 `expired`)に従う | `expired`(既定) | `time_expired` |
| `ma_break_invalidation` | 終値が方向に対してMAを割った/上抜けたら無効化。`ma_type` は現時点で `sma` のみ対応(`ema` などは `ValueError`)。`period` はJSONの値をそのまま使用 | `invalidated` | `ma_break` |

いずれの policy も、供給したローソク足の範囲内で条件に到達しなければ `status="open"` / `close_reason="no_close"` を返します(エラーではありません)。

**同一ローソク足でTP/SLの両方に到達した場合**、どちらを優先するかは `same_candle_priority`(`"stop_loss"` または `"take_profit"`)で決まります。ハードコードではなく設定値なので、同じconfigで実行すれば同じ判定を再現できます。

不正な設定(未定義の `exit_policy`、`enabled: false` のpolicyを選択、不正な `same_candle_priority` / `ma_type`、`risk_reward` / `fallback_stop_pct` / `max_hold_bars` / `period` が0以下など)は、いずれも `ValueError` で明確に弾かれます。

### 7.3 DBへの保存(Backtest)

`backtest/backtest_store.py` の `backtest_signals` テーブルは signal 単位で `result_status` / `close_price` / `closed_at` / `close_reason` / `exit_policy` / `mfe` / `mae` を保存できます。`ExitPolicyResult` をそのまま渡せないため、`backtest/backtest_models.apply_exit_policy_result(signal_record, exit_result)` がフィールド変換を行います(`status`→`result_status`、`close_time`→`closed_at`、他は同名)。

```powershell
cd C:\Users\kinniki\ClaudeLocalSession\opt\BELIAL_dev\paper-signal
python -c "
import exit_policy as ep, pandas as pd
candles = pd.DataFrame({'open':[150.5],'high':[151.0],'low':[150.2],'close':[150.8]}, index=pd.date_range('2026-01-01', periods=1, freq='1h', tz='UTC'))
signal = {'direction': 'long', 'entry_price': 150.0}
config = ep.load_exit_policy_config('config/backtest_exit_policy.json')
print(ep.evaluate_exit(signal, candles, config))
"
```

## 8. Security notes

- `paper-signal/.env` をコミットしない
- `paper-signal/data/`(SQLite)はruntime生成物のためコミットしない
- `logs/paper_signal_generator.log` はruntime生成物のためコミットしない
