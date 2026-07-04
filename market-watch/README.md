# MACD Cross Alert Bot (v1.2.2)

`market-watch/macd_alert_bot.py` monitors USD/JPY M15 candles and sends a Discord alert when MACD(12,26,9) makes a confirmed-candle bullish or bearish cross.

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
market-watch/state/macd_alert_state.json
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
DISCORD_WEBHOOK_URL=
```

Do not commit `.env` or real webhook URLs.

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
```

One real check:

```bash
cd /opt/berial
python market-watch/macd_alert_bot.py --once
```

Continuous loop:

```bash
cd /opt/berial
python market-watch/macd_alert_bot.py
```

`--dry-run` does not send to Discord and does not update `market-watch/state/macd_alert_state.json`.

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

## Discord alert content

Alerts include:

- symbol
- timeframe
- signal type and golden/dead cross label
- confirmed candle time
- current close price
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
