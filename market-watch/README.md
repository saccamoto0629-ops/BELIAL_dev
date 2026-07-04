# MACD Cross Alert Bot (v1.2.1)

`market-watch/macd_alert_bot.py` monitors USD/JPY M15 candles and sends a Discord alert when MACD(12,26,9) makes a confirmed-candle bullish or bearish cross.

The current data source is Yahoo Finance via `yfinance` (`JPY=X`). No broker account or market-data API key is required.

## Local setup on Linux

The production path is assumed to be `/opt/berial`.

```bash
cd /opt/berial
python -m pip install -r requirements.txt
cp market-watch/.env.example market-watch/.env
```

Edit `market-watch/.env` and set a real Discord webhook only on the production host:

```env
DISCORD_WEBHOOK_URL=
SYMBOL=USD_JPY
TIMEFRAME=M15
MACD_FAST=12
MACD_SLOW=26
MACD_SIGNAL=9
CHECK_INTERVAL_SECONDS=60
```

Do not commit `.env` or a real webhook URL.

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

`--dry-run` does not send to Discord and does not update `market-watch/macd_alert_state.json`.

## Cross detection logic

1. Fetch recent USD/JPY M15 close prices from Yahoo Finance.
2. Calculate:
   - MACD line = EMA(12) - EMA(26)
   - Signal line = EMA(MACD, 9)
3. Exclude the most recent bar because it may still be forming.
4. Compare the two latest confirmed candles:
   - previous confirmed candle: index `-3`
   - current confirmed candle: index `-2`
5. Detect:
   - bullish cross: previous `MACD - Signal <= 0`, current `MACD - Signal > 0`
   - bearish cross: previous `MACD - Signal >= 0`, current `MACD - Signal < 0`

## Duplicate prevention

The bot stores the notified candle timestamp in:

```text
market-watch/macd_alert_state.json
```

If the same `SYMBOL_TIMEFRAME` candle has already been notified, the bot skips sending again.

This file is runtime state and must not be committed.

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

## Files

```text
market-watch/
├─ .env.example
├─ README.md
├─ macd_alert_bot.py
└─ tests/
   └─ test_macd_alert_bot.py
```

## Git ignore / security notes

Do not commit:

- `market-watch/.env`
- real Discord webhook URLs
- `market-watch/macd_alert_state.json`
- `__pycache__/`
- `.pytest_cache/`
- `logs/`

The repository `.gitignore` excludes these runtime files.

## Future: OANDA data source

The current version uses Yahoo Finance. If more precise FX candles are needed later, replace `fetch_candles()` with an OANDA candle API implementation and enable the commented OANDA settings in `.env.example`.
