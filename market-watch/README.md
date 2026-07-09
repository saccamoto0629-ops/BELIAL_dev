# Market Watch

`market-watch/` fetches candles, computes SMA/EMA/Bollinger Bands/MACD/RSI14, renders Chart Snapshots, and runs observation-only Discord event alerts. It never places orders, sizes lots, or generates entry/exit recommendations.

Full architecture and per-bot specs live in [docs/architecture/market_watch.md](../docs/architecture/market_watch.md). This README covers setup, run, and test commands.

Bots in this directory:

- `chart_snapshot.py` — scheduled Chart Snapshot report (candlestick + SMA/EMA/Bollinger/MACD PNG) posted to Discord, and the summary feed consumed by `news-feed-bot/market_snapshot.py` and `news-feed-bot/market_bias.py`.
- `macd_alert_bot.py` — event alert when MACD(12,26,9) makes a confirmed-candle bullish/bearish cross.
- `ma_alert_bot.py` — event alert when a confirmed candle's close touches a monitored moving average. Suppresses repeat notifications while price stays in the same touch zone (see [Duplicate prevention](#duplicate-prevention)).
- `bollinger_alert_bot.py` — event alert for Bollinger Bands breaks, squeeze, and expansion_start.
- `rsi_alert_bot.py` — event alert for RSI14 overbought/oversold zone entry and exit.

## Files

```text
market-watch/
├─ .env.example          # secret-only sample
├─ config.toml           # public monitoring settings
├─ README.md
├─ chart_collector.py    # candle fetch, UTC normalization, SQLite persistence
├─ chart_models.py        # ChartTarget / Candle / IndicatorSnapshot
├─ indicators.py          # SMA / EMA / Bollinger / MACD / RSI / cross detection
├─ market_state.py         # market_state classification (trend_up/trend_down/range/volatile/squeeze/unknown)
├─ chart_renderer.py       # shared PNG rendering (JST x-axis on display)
├─ chart_snapshot.py       # Chart Snapshot generation, Discord post, summary
├─ chart_alert_context.py  # shared alert-image + S3 + snapshot linking helper
├─ chart_image_uploader.py # private S3 upload + presigned URL generation
├─ macd_alert_bot.py
├─ ma_alert_bot.py
├─ bollinger_alert_bot.py
├─ rsi_alert_bot.py
└─ tests/
   └─ test_*.py
```

Runtime files are created outside Git:

```text
logs/chart_snapshot.log
logs/macd_alert_bot.log
logs/ma_alert_bot.log
logs/bollinger_alert_bot.log
logs/rsi_alert_bot.log
market-watch/data/chart_data.sqlite3
market-watch/state/macd_alert_state.json
market-watch/state/ma_alert_state.json
market-watch/state/bollinger_alert_state.json
market-watch/state/rsi_alert_state.json
```

## Setup on Linux

Production path is assumed to be `/opt/berial`.

```bash
cd /opt/berial
python -m pip install -r requirements.txt
cp market-watch/.env.example market-watch/.env
```

Edit `market-watch/.env` and set:

```env
CHART_SNAPSHOT_WEBHOOK_URL=
BOT_ALERTS_WEBHOOK_URL=
AWS_DEFAULT_REGION=ap-northeast-1
S3_BUCKET_NAME=belial-chart-snapshots
S3_CHART_PREFIX=charts
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
```

Do not commit `.env` or real webhook URLs.

Webhook roles:

- `CHART_SNAPSHOT_WEBHOOK_URL`: posts scheduled Chart Snapshot reports to the dedicated `chart-snapshot` channel.
- `BOT_ALERTS_WEBHOOK_URL`: posts event alerts such as MA touches, MACD crosses, and sharp moves to `bot-alerts`.
- `DISCORD_WEBHOOK_URL`: legacy fallback for older MACD alert deployments. New deployments should prefer `BOT_ALERTS_WEBHOOK_URL`.

## Configuration

Public monitoring settings live in `market-watch/config.toml`.

```toml
[monitor]
symbol = "USD_JPY"
ticker = "JPY=X"
timeframe = "M15"
interval = "15m"
period = "5d"

[macd]
macd_fast = 12
macd_slow = 26
macd_signal = 9

[bollinger]
enabled = true
window = 20
std_multiplier = 2.0
squeeze_lookback = 120
squeeze_percentile = 0.10
expansion_ratio = 1.30

[bollinger.notify]
upper_break = true
lower_break = true
expansion_start = true
upper_touch = false
lower_touch = false
squeeze = false

[rsi_alert]
enabled = true
overbought_threshold = 70.0
oversold_threshold = 30.0

[runtime]
check_interval_seconds = 60

[logging]
log_level = "INFO"
```

`.env` is reserved for secrets such as Discord webhook URLs and future API keys. Chart images are uploaded to private S3 keys and exposed to Discord only through short-lived presigned URLs. Do not store presigned URLs or depend on public S3 URLs.

Chart Snapshot symbols keep display/storage names separate from fetch symbols. For S&P 500 monitoring, `SPX` remains the logical symbol in SQLite, Discord text, and S3 keys, while `CHART_SYMBOL_SPX=ES` / `CHART_TICKER_SPX=ES=F` fetches E-mini S&P 500 futures candles for denser off-hours charts. `CHART_FALLBACK_CHART_SYMBOL_SPX=SPX` / `CHART_FALLBACK_TICKER_SPX=^GSPC` keeps the old index source as fallback when the futures proxy returns no candles. Use `MES` / `MES=F` instead if the Micro E-mini feed is preferred.

For backward compatibility, old `.env` keys such as `SYMBOL`, `TIMEFRAME`, `MACD_FAST`, `MACD_SLOW`, `MACD_SIGNAL`, `CHECK_INTERVAL_SECONDS`, and `LOG_LEVEL` can still override config values, but new deployments should use `config.toml`.

## Run

One dry-run check:

```bash
cd /opt/berial
python market-watch/chart_snapshot.py --once --dry-run
python market-watch/macd_alert_bot.py --once --dry-run
python market-watch/ma_alert_bot.py --once --dry-run
python market-watch/bollinger_alert_bot.py --once --dry-run
python market-watch/rsi_alert_bot.py --once --dry-run
```

One real check:

```bash
cd /opt/berial
python market-watch/chart_snapshot.py --once
python market-watch/macd_alert_bot.py --once
python market-watch/ma_alert_bot.py --once
python market-watch/bollinger_alert_bot.py --once
python market-watch/rsi_alert_bot.py --once
```

Continuous loop (event alert bots only; `chart_snapshot.py` is intended for scheduled/oneshot runs):

```bash
cd /opt/berial
python market-watch/macd_alert_bot.py
python market-watch/ma_alert_bot.py
python market-watch/bollinger_alert_bot.py
python market-watch/rsi_alert_bot.py
```

`--dry-run` does not send to Discord, upload to S3, write to `chart_data.sqlite3`, or update any `market-watch/state/*.json` file, for every bot in this directory.

## Cross detection

1. Fetch recent close prices with `yfinance`.
2. Calculate:
   - MACD line = EMA(12) - EMA(26)
   - Signal line = EMA(MACD, 9)
3. Exclude the latest bar because it may still be forming.
4. Compare confirmed candles:
   - previous confirmed candle: index `-3`
   - current confirmed candle: index `-2`
5. Detect:
   - golden cross / bullish: previous `MACD - Signal <= 0`, current `MACD - Signal > 0`
   - dead cross / bearish: previous `MACD - Signal >= 0`, current `MACD - Signal < 0`

## Duplicate prevention

State is saved in:

```text
market-watch/state/macd_alert_state.json
```

The key is `symbol + timeframe`. The value stores:

- last notified candle time
- signal type
- price
- MACD value
- signal-line value
- notification time

This prevents duplicate notifications for the same candle after a bot restart.

For Bollinger alerts, state is saved in:

```text
market-watch/state/bollinger_alert_state.json
```

The dedupe key is `symbol + timeframe + signal_type + candle_time`, so the same confirmed candle cannot notify the same Bollinger signal twice.

For RSI alerts, state is saved in:

```text
market-watch/state/rsi_alert_state.json
```

The key is `symbol + timeframe`. The value stores the latest recorded confirmed candle, RSI14 state, and last notified event. This prevents duplicate notifications for the same confirmed candle and keeps repeated candles in the same RSI zone quiet.

For MA touch alerts, state is saved in:

```text
market-watch/state/ma_alert_state.json
```

`ma_alert_bot.py` separates the raw touch *detection* from the notification *decision*, so the raw fact is always recorded even when Discord notification is suppressed. State holds two things:

- per-candle dedupe entries (`symbol + timeframe + ma_name + candle_time`), same shape as before, now also including `suppressed` and `suppress_reason`.
- a `touch_zones` map keyed by `symbol + timeframe + ma_type + ma_period + zone_key`, holding `outside` / `touching`. The first touch after `outside` notifies; repeated touches while still `touching` are suppressed with `suppress_reason = "same_touch_zone"`; leaving the touch band resets the zone to `outside` so the next touch notifies again.

Full spec: [docs/architecture/market_watch.md](../docs/architecture/market_watch.md#同一接触ゾーンの連続通知抑制issue-16).

## Bollinger alert detection

`bollinger_alert_bot.py` excludes the latest bar because it may still be forming, then evaluates the confirmed candle at index `-2`.

It calculates:

- `middle = SMA20(close)`
- `upper = middle + 2σ`
- `lower = middle - 2σ`
- `bandwidth = (upper - lower) / middle`

Enabled by default:

- `upper_break`: close is at or above upper band
- `lower_break`: close is at or below lower band
- `expansion_start`: previous confirmed candle was squeeze, current candle exits squeeze, and bandwidth is at least `1.30x` the previous bandwidth

Disabled by default:

- `upper_touch`: high is at or above upper band
- `lower_touch`: low is at or below lower band
- `squeeze`: bandwidth is at or below the lower 10% of the past 120 candles

These are market-state alerts only. They are not entry, exit, order, lot-size, stop-loss, or take-profit instructions.

## RSI alert detection

`rsi_alert_bot.py` excludes the latest bar because it may still be forming, then compares the previous confirmed candle at index `-3` with the current confirmed candle at index `-2`.

It uses the `rsi14` value from `indicators.calculate_indicators()` and detects:

- `overbought_enter`: RSI14 moves from below 70 to 70 or above
- `overbought_exit`: RSI14 moves from 70 or above to below 70
- `oversold_enter`: RSI14 moves from above 30 to 30 or below
- `oversold_exit`: RSI14 moves from 30 or below to above 30

These are observation alerts only. They are not entry, exit, order, lot-size, stop-loss, or take-profit instructions.

## Discord alert content

Alerts include:

- symbol
- timeframe
- signal type and golden/dead cross label
- Bollinger signal type when emitted by `bollinger_alert_bot.py`
- RSI event type when emitted by `rsi_alert_bot.py`
- confirmed candle time
- current close price
- Bollinger upper, middle, lower, and bandwidth
- RSI14 value and state
- MACD value
- signal-line value
- detection time
- short explanation
- chart image in `embed.image.url` through a short-lived presigned URL when a linked `chart_snapshots.s3_key` is available

The tone is Berial-style market watch, but the content stays practical.

## Logs

Each bot writes to its own log file under `logs/` (see [Files](#files)), created automatically on first run. Logs include:

- bot startup
- monitored target and MACD settings
- candle fetch success/failure
- MACD cross detection
- Discord notification success/failure
- exceptions

## Tests

Recommended command:

```bash
cd /opt/berial
python -m pytest market-watch/tests -q --basetemp .tmp_pytest_market_watch -p no:cacheprovider
```

Manual confirmation for chart images:

- Run `python market-watch/chart_snapshot.py --once --dry-run --skip-fetch` first and confirm no S3 upload, DB write, or Discord send is attempted.
- In a configured environment, run one non-dry-run alert or snapshot check and confirm `chart_snapshots.s3_bucket` / `chart_snapshots.s3_key` are saved while no presigned URL is stored in SQLite.
- Open the generated presigned URL before expiry and confirm the PNG renders; Discord should display the same URL through `embed.image.url`.
- Keep the S3 bucket private. Do not switch to public S3 URLs.

The explicit `--basetemp` and `-p no:cacheprovider` options avoid problems on hosts where an old `.pytest_cache/` or default temp directory is owned by another user.

If `.pytest_cache/` causes `Permission denied`, fix ownership or remove it:

```bash
sudo chown -R "$USER:$USER" /opt/berial/.pytest_cache /opt/berial/news-feed-bot/.pytest_cache
```

or:

```bash
sudo rm -rf /opt/berial/.pytest_cache /opt/berial/news-feed-bot/.pytest_cache
```

## Security notes

Do not commit:

- `market-watch/.env`
- real Discord webhook URLs
- `market-watch/state/`
- `market-watch/macd_alert_state.json` from older versions
- `__pycache__/`
- `.pytest_cache/`
- `logs/`

## Scope

`market-watch/` is an Alert-layer implementation: it detects and reports facts (crosses, touches, band breaks, RSI zone transitions). It does not implement trade judgement, scoring, entries, exits, lot sizing, stop-loss/take-profit, or broker API integration. Interpreting these alerts (Strategy/Method) and executing trades (Execution) are separate, currently out-of-scope layers — see [docs/product/concept.md](../docs/product/concept.md#レイヤー分離).

For the full current specification (indicator formulas, Freshness/Bias Weight rules, SQLite schema, systemd examples), see [docs/architecture/market_watch.md](../docs/architecture/market_watch.md). For planned v1.4 Backtest work, see [docs/product/roadmap.md](../docs/product/roadmap.md).
