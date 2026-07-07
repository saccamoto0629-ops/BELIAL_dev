# MACD Cross Alert Bot (v1.2.2)

`market-watch/macd_alert_bot.py` monitors USD/JPY M15 candles and sends a Discord alert when MACD(12,26,9) makes a confirmed-candle bullish or bearish cross.
`market-watch/bollinger_alert_bot.py` monitors confirmed candles for Bollinger Bands market-state signals and sends observation-only Discord alerts.

v1.2.2 is an operations-hardening release. It does not add market-judgement logic. The bot only detects MACD crosses and notifies Discord.

## Files

```text
market-watch/
├─ .env.example          # secret-only sample
├─ config.toml           # public monitoring settings
├─ README.md
├─ macd_alert_bot.py
└─ tests/
   └─ test_macd_alert_bot.py
```

Runtime files are created outside Git:

```text
logs/macd_alert_bot.log
logs/bollinger_alert_bot.log
market-watch/state/macd_alert_state.json
market-watch/state/bollinger_alert_state.json
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

[runtime]
check_interval_seconds = 60

[logging]
log_level = "INFO"
```

`.env` is reserved for secrets such as Discord webhook URLs and future API keys.

For backward compatibility, old `.env` keys such as `SYMBOL`, `TIMEFRAME`, `MACD_FAST`, `MACD_SLOW`, `MACD_SIGNAL`, `CHECK_INTERVAL_SECONDS`, and `LOG_LEVEL` can still override config values, but new deployments should use `config.toml`.

## Run

One dry-run check:

```bash
cd /opt/berial
python market-watch/macd_alert_bot.py --once --dry-run
python market-watch/bollinger_alert_bot.py --once --dry-run
```

One real check:

```bash
cd /opt/berial
python market-watch/macd_alert_bot.py --once
python market-watch/bollinger_alert_bot.py --once
```

Continuous loop:

```bash
cd /opt/berial
python market-watch/macd_alert_bot.py
python market-watch/bollinger_alert_bot.py
```

`--dry-run` does not send to Discord and does not update `market-watch/state/macd_alert_state.json`.
For Bollinger alerts, `--dry-run` also leaves `market-watch/state/bollinger_alert_state.json` unchanged.

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

## Discord alert content

Alerts include:

- symbol
- timeframe
- signal type and golden/dead cross label
- Bollinger signal type when emitted by `bollinger_alert_bot.py`
- confirmed candle time
- current close price
- Bollinger upper, middle, lower, and bandwidth
- MACD value
- signal-line value
- detection time
- short explanation

The tone is Berial-style market watch, but the content stays practical.

## Logs

Logs are written to:

```text
logs/macd_alert_bot.log
```

The log directory is created automatically. Logs include:

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

## Future

v1.3 is reserved for market-judgement logic. v1.2.2 intentionally does not add judgement, scoring, or trade decisions.

systemd service files and production daemonization are postponed to v1.4 or later.
