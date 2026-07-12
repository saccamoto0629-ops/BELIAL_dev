# Operations
## 方針

`.env` はGit管理しない。Webhook URL、API key、token などの秘密情報の実値は設計書やREADMEに書かない。

dry-run は以下の副作用を一切発生させない確認モードとして扱う。

- Discord送信
- state ファイル更新
- DB作成・更新
- S3アップロード

新機能を追加する場合も、`--dry-run`(または `dry_run=True`)でこれらの副作用が発生しないことをテストで確認する。
## 環境変数

現行実装の主な環境変数は以下。コンポーネントをまたぐ横断的な変数(DBパスなど)を含む。各コンポーネント固有の全変数は、そのコンポーネントの `README.md` / `.env.example` を正とする(`news-feed-bot/README.md`、`market-watch/README.md`、`paper-signal/README.md`)。

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
| `AWS_DEFAULT_REGION` | `ap-northeast-1` | Chart Snapshot画像保存先S3 region |
| `S3_BUCKET_NAME` | `belial-chart-snapshots` | Chart Snapshot画像保存先S3 bucket |
| `S3_CHART_PREFIX` | `charts` | S3 key prefix |
| `MARKET_SNAPSHOT_ENABLED` | `true` | Market Snapshot定期投稿を有効化 |
| `MARKET_SNAPSHOT_TIME` | `07:00` | Market Snapshot投稿時刻 |
| `ENABLE_FEDWATCH` | `false` | FedWatch optional Providerを有効化 |
| `ENABLE_ECONOMIC_CALENDAR` | `false` | Economic Calendar optional Providerを有効化 |
| `CHART_DB_PATH` | `market-watch/data/chart_data.sqlite3` | Chart DBのパス上書き(`market-watch/chart_collector.py`) |
| `MARKET_BIAS_DB_PATH` | `news-feed-bot/data/market_bias.sqlite3` | Market Bias DBのパス上書き。Backtestから別プロセスで参照する場合に使う(`news-feed-bot/market_bias_store.py`) |

注意: `news-feed-bot/` のニュース投稿では、現行コードで使用している実変数名は `NEWS_WEBHOOK_URL` である。`DISCORD_WEBHOOK_URL` はニュース投稿用の設定名ではない。一方、`market-watch/` のイベント通知では旧設定との互換性のため、`BOT_ALERTS_WEBHOOK_URL` が未設定の場合に限り `DISCORD_WEBHOOK_URL` をlegacy fallbackとして扱う。

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
python market-watch/rsi_alert_bot.py --once --dry-run
python news-feed-bot/market_snapshot.py
```

常駐対象:

```text
news-feed-bot/news_feed_bot.py
market-watch/macd_alert_bot.py
market-watch/ma_alert_bot.py
market-watch/bollinger_alert_bot.py
market-watch/rsi_alert_bot.py
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
python market-watch/rsi_alert_bot.py --once --dry-run
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

Paper Signal の正式な実行エントリーポイントは `paper-signal/paper_signal_bot.py`(常駐ループのロジック本体は `signal_generator.py`。`paper_signal_bot.py` はそれを呼ぶ薄いエントリーポイント。`python signal_generator.py ...` も後方互換で同等に動く)。

```bash
cd /opt/berial/paper-signal

# 確認のみ(Discord送信・SQLite保存・logfile作成なし=永続副作用なし)
python paper_signal_bot.py --once --dry-run

# 1回実行(Discord送信・SQLite保存あり、sleepなし)
python paper_signal_bot.py --once

# 常駐(ExecStartと同じ。--onceなしで PAPER_SIGNAL_CHECK_INTERVAL_SECONDS 既定900秒ごとにループ)
python paper_signal_bot.py
```

1周期の処理(`run_once`)と常駐ループ(`run_service`)は分離されており、`--once` は sleep せず1周期だけ実行する。`--dry-run` は SQLite保存・Discord送信・state更新・logfile作成など永続的副作用を一切行わず(console-only logger)、使用予定の入力・target candle timestamp・生成結果・no-signal・skip理由・重複判定のみをログ出力する。

### Paper Signal 日次集計レポート(Issue #47)

`paper_signal_daily_report.py` は保存済み Paper Signal を指定日1日分だけ集計し、symbol / timeframe / direction / market state / strategy 別件数と confidence 統計を出力する。**読み取り専用**で signal DB・Discord・broker/Execution API に副作用を出さない(`--output` 指定時のファイル書き込みのみ)。日付境界は設定タイムゾーン(`$PAPER_SIGNAL_REPORT_TIMEZONE` → `.env` → 既定 `Asia/Tokyo`)の現地0時〜翌0時を UTC 半開区間で判定する。

```bash
cd /opt/berial/paper-signal

# 設定タイムゾーン基準の前日分を Markdown で保存(日次 cron/timer 想定)
report_tz="${PAPER_SIGNAL_REPORT_TIMEZONE:-Asia/Tokyo}"
report_date="$(TZ="$report_tz" date -d 'yesterday' +%F)"
python paper_signal_daily_report.py --date "$report_date" --timezone "$report_tz" \
    --format markdown --output "reports/paper_signal_${report_date}.md"
```

日次定期実行は、既存の `berial-chart-snapshot.timer` と同様に systemd の `OnCalendar=` timer(例: 毎日 JST 00:10 = `OnCalendar=*-*-* 15:10:00 UTC` を前日 `--date` 付きで起動)へ組み込める。レポート生成は読み取り専用なので、常駐サービスとは独立に任意タイミングで再実行してよい(冪等)。

## Paper Signal systemd(常駐サービス, Issue #41)

`paper_signal_bot.py` を `macd_alert_bot.py` などと同様に systemd で常駐させる。SIGTERM / SIGINT を受けると現在の周期を終えて安全に停止し、`TimeoutStopSec` 内に終了しなければ systemd が SIGKILL する。多重起動・restart・競合が起きても、同一 `strategy_profile` / `symbol` / `timeframe` / target candle timestamp の Paper Signal は SQLite の UNIQUE 制約で重複保存・重複通知されない(`ps -aux | grep .py` での二重起動確認は従来どおり推奨)。

unit定義は **リポジトリにコミット済み**のテンプレート `scripts/systemd/berial-paper-signal.service` を正とする(全ディレクティブ入り)。配備手順:

```bash
# 1. テンプレートを配置(User/パスは環境に合わせて調整)
sudo cp /opt/berial/scripts/systemd/berial-paper-signal.service /etc/systemd/system/
sudo nano /etc/systemd/system/berial-paper-signal.service   # 必要なら User / WorkingDirectory / venv path を調整

# 2. 反映して有効化・起動
sudo systemctl daemon-reload
sudo systemctl enable --now berial-paper-signal

# 3. 確認
systemctl status berial-paper-signal --no-pager
journalctl -u berial-paper-signal -f        # または ./scripts/logs.sh signal
```

主要ディレクティブ(テンプレート参照): `ExecStart=<venv>/python .../paper_signal_bot.py` / `EnvironmentFile=-.../paper-signal/.env`(`.env` 欠如でも起動、Discordはスキップ)/ `Restart=always` / `RestartSec=10` / `KillSignal=SIGTERM` / `TimeoutStopSec=30` / `StandardOutput=journal` / `StandardError=journal`。

> `scripts/restart_all.sh` は上記 unit が **未配備でも** 後続 unit の restart を止めず、失敗を報告して非0で終了する(POSIX sh)。この unit を配備するまで `berial-paper-signal.service` の restart は失敗する点に注意。

---
## Berial systemd helper scripts

開発環境および本番相当のLinux環境では、コード変更後に古いプロセスを動かし続けないように、`scripts/` 配下の補助スクリプトで systemd の再起動、状態確認、ログ確認を行う。

リポジトリルートから実行する。

```bash
cd /opt/berial

# Berial の常駐サービスをまとめて再起動し、Chart Snapshot timer を更新する。
./scripts/restart_all.sh

# Market Watch alert 系だけを再起動する。
./scripts/restart_market_watch.sh

# 主要な service / timer の状態をまとめて確認する。
./scripts/status.sh
```

`restart_market_watch.sh` は以下を再起動する。

```text
berial-ma-alert.service
berial-macd-alert.service
berial-bollinger-alert.service
```

`restart_all.sh` は以下を再起動する。

```text
berial-news.service
berial-ma-alert.service
berial-macd-alert.service
berial-bollinger-alert.service
berial-paper-signal.service
berial-chart-snapshot.timer
```

`logs.sh` は `journalctl` の短縮ラッパーとして使う。

```bash
./scripts/logs.sh ma
./scripts/logs.sh macd
./scripts/logs.sh bollinger
./scripts/logs.sh signal
./scripts/logs.sh snapshot
./scripts/logs.sh timer
./scripts/logs.sh news
./scripts/logs.sh all --since today
```

追加引数なしの場合、`logs.sh` は選択したunitの直近200行を表示し、そのままfollowする。追加引数は `journalctl` に渡すため、`./scripts/logs.sh snapshot -n 100 --no-pager` のような指定も使える。

各スクリプトは通常 `systemctl` と `journalctl` を使う。実際のsystemd unitに触れずにスクリプトだけを検証したい場合のみ、`SYSTEMCTL` または `JOURNALCTL` 環境変数で差し替える。
