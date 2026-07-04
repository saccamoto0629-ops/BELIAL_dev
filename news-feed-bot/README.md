# Discord Market News Feed Bot

RSSから金融・市場関連ニュースを取得し、Discord Webhookへ投稿するPythonボットです。

## Local setup

```powershell
cd C:\Users\kinniki\ClaudeLocalSession\opt\BELIAL_dev
pip install -r requirements.txt
```

## Dry run

実Webhookを使わずに確認する場合は、環境変数またはCLI引数でdry-runを使います。

```powershell
cd C:\Users\kinniki\ClaudeLocalSession\opt\BELIAL_dev\news-feed-bot
$env:DRY_RUN = "true"
python .\news_feed_bot.py
```

または:

```powershell
python .\news_feed_bot.py --dry-run
```

Dry-runでは以下を行いません。

- Discord WebhookへのHTTP送信
- `posted_ids.json` の更新

送信予定のpayloadだけをコンソールへ出力します。

## Production run

本番実行では、従来通り `.env` の `NEWS_WEBHOOK_URL` を使ってDiscordへ送信します。

```powershell
cd C:\Users\kinniki\ClaudeLocalSession\opt\BELIAL_dev\news-feed-bot
Copy-Item .env.example .env
notepad .env
python .\news_feed_bot.py
```

`.env` の例:

```env
NEWS_WEBHOOK_URL=https://discord.com/api/webhooks/...
DRY_RUN=false
```

注意:

- 本物のWebhook URLやAPIキーは、コード・README・テスト・`.env.example` へ書かないでください。
- `.env` はGit管理対象外です。
- `posted_ids.json` は投稿済み記事の重複投稿防止に使います。

## Tests

```powershell
cd C:\Users\kinniki\ClaudeLocalSession\opt\BELIAL_dev
pytest news-feed-bot
```

pytestでは以下を検証します。

- RSS取得結果から投稿メッセージを作れること
- `posted_ids` に存在する記事は投稿しないこと
- `posted_ids` にない記事だけ送信対象になること
- `DRY_RUN=true` / `--dry-run` 相当ではDiscord送信しないこと
- dry-runでは `posted_ids.json` が変更されないこと
- 実Webhookなしでも送信関数を差し替えて本番送信経路を検証できること

## Main settings

| Setting | Default | Description |
|---|---:|---|
| `NEWS_WEBHOOK_URL` | empty | Discord news webhook URL. Required for production posting. |
| `DEV_LOG_WEBHOOK_URL` | empty | Optional webhook for run summaries. |
| `DRY_RUN` | `false` | When `true`, prints only and does not send or update `posted_ids.json`. |
| `GOOGLE_NEWS_QUERY` | market query | Google News search query. |
| `MAX_ARTICLES_PER_RUN` | `5` | Max selected articles per RSS feed per run. |
| `FETCH_INTERVAL_MINUTES` | `60` | RSS polling interval. |
