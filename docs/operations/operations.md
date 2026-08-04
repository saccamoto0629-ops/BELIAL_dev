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
| `PAPER_POSITION_DB_PATH` | `paper-execution/data/paper_positions.sqlite3` | Paper Execution の仮想ポジションDBのパス上書き(`paper-execution/position_store.py`)。相対パスは `paper-execution/` 基準。本番は永続ボリューム上の絶対パス推奨(例 `/var/lib/berial/paper_positions.sqlite3`)。通常(非dry-run)起動時に legacy DB を現行スキーマへ自動 migration してからポジションを読む(v3 で通知 outbox テーブル `paper_execution_notifications` を追加)。dry-run では作成・migration せず、本体+WAL/SHM の snapshot コピーに対する読み取り専用スキーマ確認のみ行う(実 DB へ接続しない) |
| `EXECUTION_ORDER_DB_PATH` | `execution/data/execution_orders.sqlite3` | 将来の Live Execution 用 Execution Order DB のパス上書き(`execution/execution_store.py`、Issue #74)。相対パスは `execution/` 基準。**運用導線からこの DB を作成・更新するプロセスは現時点で存在しない**(Live Execution runner 未実装。テストは `tmp_path` を使用) |
| `SAFETY_GATE_DECISION_DB_PATH` | `execution/data/safety_gate_decisions.sqlite3` | 将来の Live Execution 用 Safety Gate 判定監査 DB のパス上書き(`execution/safety_store.py`、Issue #75)。相対パスは `execution/` 基準。**運用導線からこの DB を作成・更新するプロセスは現時点で存在しない**(Live Execution runner 未実装。テストは `tmp_path` を使用) |
| `BROKER_RECONCILIATION_DB_PATH` | `execution/data/broker_reconciliation.sqlite3` | Broker reconciliation の照合結果 DB のパス上書き(`execution/reconciliation_store.py`、Issue #103)。相対パスは `execution/` 基準。照合は**検出専用**で注文状態を変更しない(この DB に注文テーブルは存在しない)。**運用導線からこの DB を作成・更新するプロセスは現時点で存在しない**(scheduler / 常駐 Bot への組み込みは未実装。テストは `tmp_path` を使用) |
| `OANDA_API_TOKEN` | empty | OANDA Demo Adapter(`broker/oanda_demo_adapter.py`、Issue #79)の API token。**Demo(fxpractice)口座のトークンのみ**。環境変数でのみ渡し、ログ・例外・docs へ実値を書かない。**運用導線からこの Adapter を起動するプロセスは現時点で存在しない**(Execution 統合は未実装。opt-in の実 API テストのみが使用) |
| `OANDA_ACCOUNT_ID` | empty | OANDA Demo Adapter の Demo account id。環境変数でのみ渡し、実値を出力しない |
| `OANDA_API_URL` | `https://api-fxpractice.oanda.com` | OANDA Demo Adapter の接続先。**Demo endpoint のみ許可(allowlist)**。Live endpoint(`fxtrade` を含む host)やその他の URL を指定すると構築時に fail-closed で起動拒否。HTTP redirect は追従しない(3xx は共通エラー化)ため、応答によって Demo endpoint 外へ接続が逸れることもない |
| `OANDA_HTTP_TIMEOUT_SECONDS` | `10` | OANDA Demo Adapter の HTTP timeout 秒数。timeout 時は `BrokerTimeoutError`(注文状態 UNKNOWN)となり、**自動再送はしない** |
| `BELIAL_RUN_OANDA_DEMO_API_TESTS` | unset | `1` のときだけ OANDA Demo 実 API テスト(`broker/tests/test_oanda_demo_adapter_demo_api.py`)を実行する opt-in フラグ。通常 CI では未設定のため全件 skip され、7 スイートは外部通信しないまま |
| `PAPER_EXECUTION_DISCORD_WEBHOOK_URL` | empty | Paper Execution 状態遷移通知(仮想約定・仮想損益。Issue #83)の Discord Webhook。`paper-execution/.env` またはプロセス環境変数。Paper Signal の `DISCORD_WEBHOOK_URL` とは別変数(責務分離)。Transactional Outbox 方式: 通知 outbox 行は Position 遷移と同一トランザクションで確定するため、Webhook 未設定でも記録され消えない。未設定時は claim せず attempt を消費せず滞留し、URL 設定後に `DELIVERY_BATCH_LIMIT`=20 件/cycle で順次配送する。配送は at-least-once(HTTP 成功後 success 記録前に停止すると同一 Event ID の重複があり得る)。配送失敗は position 状態・`--once` 終了コードへ影響せず、後続 cycle で自動再送(上限5回、以後 `dead_letter`) |

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
## Paper Execution 通知 outbox の運用(Issue #83)

Paper Execution の Discord 通知は Position DB(`paper_positions.sqlite3`)内の
`paper_execution_notifications` テーブルを **Transactional Outbox** として使う。
状態遷移(open/closed/expired/cancelled)行はポジション遷移と同一トランザクションで
確定し、配送は commit 後に claim/lease 付きで行う(at-least-once)。

### 状態確認

```bash
DB=/opt/berial/paper-execution/data/paper_positions.sqlite3

# delivery_status ごとの件数(dead_letter / processing の滞留を監視)
sqlite3 "$DB" \
  "SELECT delivery_status, COUNT(*) FROM paper_execution_notifications GROUP BY delivery_status;"

# 未解消 dead-letter(上限到達で配送を諦めた通知)の一覧
sqlite3 "$DB" \
  "SELECT event_key, attempt_count, last_error, updated_at_utc
     FROM paper_execution_notifications
    WHERE delivery_status = 'dead_letter' ORDER BY updated_at_utc;"

# lease が生きたまま長時間 processing の行(worker 停止の疑い。lease 期限後は自動再claim)
sqlite3 "$DB" \
  "SELECT event_key, claimed_by, claimed_until_utc
     FROM paper_execution_notifications WHERE delivery_status = 'processing';"
```

各 cycle のログ末尾 `Cycle done: ... dead_letter=N` と、`N>0` 時の
`WARNING ... dead-letter notification(s)` で継続監視できる(dead-letter は SELECT
対象外にして静かに消さない)。`dead_letter=N` は**同一 cycle 内で新たに上限到達した分も
即時反映**する(配送後に再集計)。

上限到達済みの `failed` 行(`attempt_count >= max_attempts`。旧 v2 prototype から
移行した行や `max_attempts` を後から下げた場合に生じ得る)は、次の配送 cycle の claim
開始時に自動で `dead_letter` へ正規化され、監視対象に入る。手元で先に確認したい場合:

```bash
# 上限到達済みなのに failed のまま残っている行(次 cycle で dead_letter へ正規化される)
sqlite3 "$DB" \
  "SELECT event_key, attempt_count FROM paper_execution_notifications
    WHERE delivery_status = 'failed' AND attempt_count >= 5;"
```

### 復旧(手動再送 / attempt リセット)

`dead_letter` や再送したい行は、Python から `requeue_notification` で
`pending`(attempt_count=0)へ戻すと次 cycle で再配送される。

```bash
cd /opt/berial/paper-execution
python -c "import position_store as s; print(s.requeue_notification('POSITION_ID:closed'))"
```

不要になった通知行は運用判断で手動削除してよい(outbox は無期限保持が既定)。
Webhook 未設定のあいだ滞留した行も、`PAPER_EXECUTION_DISCORD_WEBHOOK_URL` 設定後に
`DELIVERY_BATCH_LIMIT`=20 件/cycle で順次配送される(未設定中は attempt を消費しない)。

### 重複通知について

配送は **at-least-once**。HTTP POST 成功後・DB の success 記録前に停止すると、
lease 期限後に同一通知が再配送され Discord 上で重複し得る。各本文の
`Event ID: <position_id:event_type>` 行で同一イベントと識別できる。厳密な
exactly-once は保証しない(「outbox 成功済みは再送しない」ことと「Discord で重複しない」
ことは別)。

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

# 前日分(--timezone 基準)を Markdown で reports/paper_signal_YYYY-MM-DD.md へ保存
# (日次 systemd timer と同じ形。前日計算もファイル名の日付も CLI 内部で行うため、
#  shell の date 展開に依存しない)
python paper_signal_daily_report.py --previous-day --timezone Asia/Tokyo \
    --format markdown --output-dir reports

# 特定日を明示する場合(従来どおり)
python paper_signal_daily_report.py --date 2026-07-13 --timezone Asia/Tokyo \
    --format markdown --output reports/paper_signal_2026-07-13.md
```

Issue #71 でのCLI仕様:

- `--previous-day`: タイムゾーン解決後の「現在日付の1日前」を対象にする。`--date` と同時指定不可(usage error)。指定なしは従来どおり「当日」。
- `--output-dir DIR`: 実際の集計対象日から `paper_signal_YYYY-MM-DD.md`(`--format text` なら `.txt`)をディレクトリ内へ自動命名で保存する。ディレクトリは自動作成、同日再実行は一時ファイル経由の atomic replace で安全に上書き。`--output` と同時指定不可(`--output` は従来どおり維持)。
- DB検証: DB不存在 / パスがディレクトリ / SQLiteでないファイル / `paper_signals` テーブル無し / `created_at_utc` カラム無し / その他読込エラーは、**0件レポートに化けず**エラーをstderrへ出して非0終了する(`paper_signal_store.list_signals_in_range_strict`)。正常なDBで対象日0件のときだけ正常終了して0件レポートを生成する。
- DBへの保証範囲: read-only URI接続(`mode=ro`)で読むため、**DB本体の内容・schemaを変更せず、DBを新規作成せず、`init_db()`・migration・backfill・INSERT/UPDATE/DELETE を実行しない**。ただし SQLite の WAL mode では、read-only 接続でも SQLite 自身が `-shm` sidecar を作成・利用する場合があるため、DBディレクトリにはWAL運用上の書込み権限が必要になることがある。DBディレクトリが書込み不可の場合、DB本体が読めてもWAL/SHM関連で読み込みに失敗し、runtime error として非0終了し得る(原因は journal のエラーメッセージで確認できる)。
- 終了コード: `0`=正常(0件含む)/ `2`=引数・日付・タイムゾーン等のusage error / `1`=DB読込失敗・schema異常・出力失敗等のruntime error。systemdからはruntime errorがそのまま service failure になる。

日次定期実行は下記の `berial-paper-signal-report.timer` を使う。レポート生成は読み取り専用なので、常駐サービスとは独立に任意タイミングで再実行してよい(冪等)。

### Paper Signal 日次レポート timer(Issue #71)

テンプレートは `scripts/systemd/berial-paper-signal-report.service` / `berial-paper-signal-report.timer`(リポジトリにコミット済み)。毎日 **00:10 Asia/Tokyo** に oneshot service を起動し、前日分(Asia/Tokyo基準)を `/opt/berial/paper-signal/reports/paper_signal_YYYY-MM-DD.md` へ保存する。timer の `OnCalendar=*-*-* 00:10:00 Asia/Tokyo` と CLI の `--timezone Asia/Tokyo` を一致させているため、タイムゾーンの食い違いは起きない。`Persistent=true` により、00:10 に停止していた場合も次回起動時に1回だけ追い付き実行される。

`OnCalendar` のタイムゾーン付き構文は systemd >= 235(Ubuntu 18.04+)が必要。対象サーバーで `systemd-analyze calendar '*-*-* 00:10:00 Asia/Tokyo'` が通ることを確認する。通らない場合は `OnCalendar=*-*-* 15:10:00 UTC` へ書き換える(JSTはDSTが無いため恒等)。

配備手順:

```bash
# 1. テンプレートを配置(User / パスは環境に合わせて調整)
sudo cp /opt/berial/scripts/systemd/berial-paper-signal-report.service /etc/systemd/system/
sudo cp /opt/berial/scripts/systemd/berial-paper-signal-report.timer /etc/systemd/system/

# 2. 反映して timer を有効化(enable するのは timer。service を enable しない)
sudo systemctl daemon-reload
sudo systemctl enable --now berial-paper-signal-report.timer
```

確認:

```bash
# timer 一覧と次回実行時刻(毎日 00:10 JST = 15:10 UTC)
systemctl list-timers --all | grep berial-paper-signal-report
systemctl status berial-paper-signal-report.timer

# 手動で1回実行(前日分を生成。timer を待たずに動作確認できる)
sudo systemctl start berial-paper-signal-report.service

# service の結果と journal(DB異常時は Result: exit-code / failed になり、
# stderr の "error: paper signal DB ..." で原因が分かる)
systemctl status berial-paper-signal-report.service
journalctl -u berial-paper-signal-report.service        # または ./scripts/logs.sh report

# 生成された Markdown の確認
ls -l /opt/berial/paper-signal/reports/
cat /opt/berial/paper-signal/reports/paper_signal_$(TZ=Asia/Tokyo date -d yesterday +%F).md

# 再起動後も timer が有効なことの確認(enable 済みなら enabled のまま)
systemctl is-enabled berial-paper-signal-report.timer
systemctl list-timers --all | grep berial-paper-signal-report

# 停止・無効化
sudo systemctl disable --now berial-paper-signal-report.timer
```

DB異常時の切り分け: service が failed の場合、`journalctl -u berial-paper-signal-report.service -n 20` に「DB not found / not a regular file / no paper_signals table / missing required column / could not read」のいずれかが出る。正常なDBで対象日0件の場合は failed にならず、`Total Signals: 0` のレポートが生成される。

`Persistent=true` と停止期間中の欠損日: `Persistent=true` は「停止中に逃した日数分の service をすべて順番に実行する」ものでは**ない**。複数日停止していた場合でも、起動時の追い付き実行は通常1回だけであり、CLI の `--previous-day` により**起動時点の前日分だけ**が生成される。停止期間中の古い欠損日は自動 backfill されないため、`--date YYYY-MM-DD` で手動生成する:

```bash
cd /opt/berial/paper-signal

/opt/berial/.venv/bin/python paper_signal_daily_report.py \
  --date 2026-07-15 \
  --timezone Asia/Tokyo \
  --format markdown \
  --output-dir /opt/berial/paper-signal/reports
```

レポートファイルの permission: 生成される `paper_signal_YYYY-MM-DD.md` は **`0600`(所有者のみ読み書き)** を仕様とする。閲覧は service の実行ユーザー(`ubuntu`)で行う(他ユーザーで読む場合は `sudo cat` など)。既存ファイルが `0644` 等だった場合も、同日再生成後は `0600` に統一される。

DBディレクトリの権限(WAL): レポート読み込みは read-only 接続で、DB本体の内容・schemaは変更しないが、SQLite の WAL mode では read-only 接続でも `-shm` sidecar が作成・利用される場合がある。実行ユーザーには DB ファイルの読み取りに加えて、WAL運用上 DBディレクトリへの権限が必要になることがある。ディレクトリが書込み不可だと読み込みが runtime error になり service は failed となる(journal で原因確認)。

実機のみで確認する項目(pytestには含めない): `systemd-analyze verify` の通過、timer 発火時刻、`Persistent=true` による追い付き実行、再起動後の enable 状態、実DB・実 reports ディレクトリへの権限(WAL sidecar 含む)。

`scripts/restart_all.sh` には**組み込んでいない**(timer も service も対象外)。理由: `Persistent=true` の timer は、停止中に予定時刻を逃した状態で再activateされると oneshot service を即時起動し得るため、常駐プロセス再起動用の `restart_all.sh` に含めると、実行のたびに意図しない日次レポート生成が起き得る。timer の配備・更新は次の明示的な運用で行う:

```bash
# unit ファイル変更を反映
sudo systemctl daemon-reload

# 初回のみ
sudo systemctl enable --now berial-paper-signal-report.timer

# unit 変更後に必要な場合のみ、管理者が明示的に
sudo systemctl restart berial-paper-signal-report.timer
```

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

## PNG cleanup timer(Issue #67)

EC2 ローカルに残る Chart Snapshot PNG(`/opt/berial/market-watch/data/chart_images` 直下)を毎日 03:20 Asia/Tokyo に削除する oneshot timer。テンプレートは `scripts/systemd/berial-png-cleanup.{service,timer}`。対象・保持期間・dry-run・手動/緊急 cleanup・導入/停止・ディスク使用量確認の手順は [png_cleanup.md](png_cleanup.md) を正とする。日次レポート timer と同じ理由(`Persistent=true` の追い付き実行)で `restart_all.sh` には**組み込まない**。

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

`berial-paper-signal-report.timer` は `restart_all.sh` の対象に**含めない**(下記「Paper Signal 日次レポート timer」の節を参照)。

`logs.sh` は `journalctl` の短縮ラッパーとして使う。

```bash
./scripts/logs.sh ma
./scripts/logs.sh macd
./scripts/logs.sh bollinger
./scripts/logs.sh signal
./scripts/logs.sh snapshot
./scripts/logs.sh timer
./scripts/logs.sh report
./scripts/logs.sh news
./scripts/logs.sh all --since today
```

追加引数なしの場合、`logs.sh` は選択したunitの直近200行を表示し、そのままfollowする。追加引数は `journalctl` に渡すため、`./scripts/logs.sh snapshot -n 100 --no-pager` のような指定も使える。

各スクリプトは通常 `systemctl` と `journalctl` を使う。実際のsystemd unitに触れずにスクリプトだけを検証したい場合のみ、`SYSTEMCTL` または `JOURNALCTL` 環境変数で差し替える。
