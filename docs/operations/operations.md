# Operations
## 方針

`.env` はGit管理しない。Webhook URL、API key、token などの秘密情報の実値は設計書やREADMEに書かない。dry-run は Discord 送信と state 更新を行わない確認モードとして扱う。
## 環境変数

現行実装の主な環境変数は以下。

| 変数 | 既定 | 用途 |
|---|---:|---|
| `NEWS_WEBHOOK_URL` | empty | Discordニュース投稿先Webhook。現行実装で使用する変数 |
| `DEV_LOG_WEBHOOK_URL` | empty | 実行サマリ投稿先Webhook |
| `DRY_RUN` | `true` in `.env.example` | Discord送信と `posted_ids.json` 更新を止める安全モード |
| `DEBUG_SELECTION` | `false` | RSS記事選別理由のdebugログ |
| `GOOGLE_NEWS_QUERY` | market query | Google News RSS検索条件 |
| `MAX_ARTICLES_PER_RUN` | `5` | RSS feedごとの候補取得上限 |
| `MAX_TOTAL_ARTICLES_PER_RUN` | `5` | 1回の実行全体の投稿上限 |
| `FETCH_INTERVAL_MINUTES` | `60` | RSS取得間隔 |
| `FRED_API_KEY` | empty | FRED API用キー。未設定ならFREDセクションをスキップ |
| `MARKET_SNAPSHOT_ENABLED` | `true` | Market Snapshot定期投稿を有効化 |
| `MARKET_SNAPSHOT_TIME` | `07:00` | Market Snapshot投稿時刻 |
| `ENABLE_FEDWATCH` | `false` | FedWatch optional Providerを有効化 |
| `ENABLE_ECONOMIC_CALENDAR` | `false` | Economic Calendar optional Providerを有効化 |

注意: ユーザー向けに `DISCORD_WEBHOOK_URL` と呼ぶ場合があるが、現行コードで使用している実変数名は `NEWS_WEBHOOK_URL` である。実装と `.env.example` を変更しない限り、`DISCORD_WEBHOOK_URL` は有効な設定名ではない。

`.env` はGit管理しない。本物のWebhook URL、API key、tokenは `.env.example` や設計書に書かない。
## テスト方針

pytestで以下を検証している。

- RSS取得結果からDiscord payloadを作れること
- posted_idsによる既投稿除外
- 金融ニュースフィルタ
- importance scoreと全体投稿上限
- dry-run時にDiscord送信・posted_ids更新をしないこと
- Market Snapshot本文生成
- Yahoo Finance Provider
- FRED Provider
- TradingView Provider
- Risk Label
- Chart Snapshot Summary連携
- Market Bias本文生成
- Economic Calendar Providerの正常系・異常系
- FedWatch Providerの正常系・異常系
- optional Providerが既定falseで呼ばれないこと

外部APIアクセスはテストではMock化し、実Webhookや本物のAPIキーは使わない。
## 起動方式 / 本番運用

Linux運用パスは `/opt/berial` を想定する。

初期確認:

```bash
cd /opt/berial
source .venv/bin/activate
python -m pip install -r requirements.txt
python market-watch/chart_snapshot.py --once --dry-run
python market-watch/macd_alert_bot.py --once --dry-run
python market-watch/ma_alert_bot.py --once --dry-run
python market-watch/bollinger_alert_bot.py --once --dry-run
python news-feed-bot/market_snapshot.py
```

常駐対象:

```text
news-feed-bot/news_feed_bot.py
market-watch/macd_alert_bot.py
market-watch/ma_alert_bot.py
market-watch/bollinger_alert_bot.py
```

定期実行対象:

```text
market-watch/chart_snapshot.py --once
news-feed-bot/market_snapshot.py
```

Market Watch 手動確認:

```bash
python market-watch/macd_alert_bot.py --once --dry-run
python market-watch/ma_alert_bot.py --once --dry-run
python market-watch/bollinger_alert_bot.py --once --dry-run
python market-watch/bollinger_alert_bot.py --once
python market-watch/bollinger_alert_bot.py
```

Windowsローカル確認:

```powershell
cd C:\Users\kinniki\ClaudeLocalSession\opt\BELIAL_dev
C:\Users\kinniki\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe market-watch\bollinger_alert_bot.py --once --dry-run
```

実運用確認は `.env` に `BOT_ALERTS_WEBHOOK_URL` を設定したうえで行う。

```powershell
cd C:\Users\kinniki\ClaudeLocalSession\opt\BELIAL_dev
C:\Users\kinniki\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe market-watch\bollinger_alert_bot.py --once
```

継続監視:

```powershell
C:\Users\kinniki\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe market-watch\bollinger_alert_bot.py
```

`macd_alert_bot.py` は systemd で常駐させる。手動起動プロセスとsystemd起動プロセスが二重起動しないよう、`ps -aux | grep .py` で確認する。

systemd例:

```ini
[Unit]
Description=Berial MACD Alert Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/berial/market-watch
Environment="PYTHONUNBUFFERED=1"
ExecStart=/opt/berial/.venv/bin/python /opt/berial/market-watch/macd_alert_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

確認コマンド:

```bash
systemctl status berial-macd-alert --no-pager
journalctl -u berial-macd-alert -f
ps -aux | grep .py
```

`--dry-run` ではDiscord送信もstate更新も行わない。

---
## CLI

```bash
cd /opt/berial/paper-signal

# 確認のみ(Discord送信・SQLite保存なし)
python signal_generator.py --once --dry-run

# 1回実行(Discord送信・SQLite保存あり)
python signal_generator.py --once
```

`--once` を付けない場合は `PAPER_SIGNAL_CHECK_INTERVAL_SECONDS`(既定900秒)ごとにループする常駐プロセスとして動作する。
## Paper Signal systemd timer案

Paper Signal の将来の systemd timer 案は [Roadmap](../product/roadmap.md) に置く。
