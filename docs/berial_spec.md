# Berial / Belial 仕様書 v0.1

## 0. 文書の位置づけ

本書は、ChatGPTエクスポート会話ログから抽出した Berial / Belial / FX / ニュース監視 / Discord bot 関連の内容をもとに作成した初期仕様書である。

現時点では、Berial は「自動売買システム」ではなく、ニュース・市況データ・チャート特徴量を収集し、Discord上で市場状態を可視化する市場監視システムとして定義する。

将来的には、以下の段階的拡張を想定する。

```text
ニュース監視
↓
市場状態分類
↓
手法選択
↓
ペーパートレード
↓
半自動売買
↓
小額自動売買
```

---

## 1. プロジェクト目的

### 1.1 主目的

Berial は、ニュース・市況データ・チャート特徴量を監視し、FX / CFD / 株式判断を補助する Discord 常駐型の市場監視システムである。

初期段階では、以下を目的とする。

- 市場関連ニュースを自動取得する
- 取得したニュースを Discord に投稿する
- 重要ニュースや市場変化を見逃しにくくする
- 人間が夜間にチャートやニュースへ張り付く負荷を下げる
- 将来的な市場状態判定・手法選択・売買補助の土台を作る

### 1.2 裏目的

本プロジェクトは、単なる投資 bot ではない。以下の目的も含む。

- ニュースを追う負荷を減らす
- 睡眠を壊さずに市場を監視する
- 感覚的な Entry / Exit をルール化する
- AI / GitHub / Codex / Claude Code の実践環境にする
- 自分で設計・実装・運用まで作り切る個人開発プロジェクトにする

---

## 2. システム概要

### 2.1 初期構成

v0.1 では、以下の構成を目指す。

```text
RSS / News Source
↓
news_feed_bot.py
↓
Discord Webhook
↓
#news-feed / #dev-log
```

### 2.2 将来構成

将来的には、以下の構成を想定する。

```text
News Sources / Market Data / Chart Features
↓
Market Analyst
↓
Market State Classifier
↓
Strategy Selector
↓
Risk Manager
↓
Paper Trading / Trade Executor
↓
Discord / Logs / Reports
```

---

## 3. v0.1 スコープ

### 3.1 v0.1 で作るもの

v0.1 では、以下を対象とする。

- RSS からニュースを取得する
- Discord `#news-feed` にニュースを投稿する
- 投稿済みニュースの重複排除を行う
- 最大投稿件数を制御する
- 実行ログを `#dev-log` に投稿する
- Webhook URL を `.env` で管理する
- `.env.example` を用意する
- README に起動方法・設定方法を書く
- 最低限のエラーハンドリングを実装する

### 3.2 v0.1 で作らないもの

v0.1 では、以下は対象外とする。

- 自動発注
- 自動売買
- Reuters 本文の自動取得
- Paywall 回避
- ログイン Cookie を使った記事取得
- X 自動投稿
- 高頻度スキャルピング
- 秒単位の価格監視
- AI による未来予測

---

## 4. 名称・世界観

### 4.1 名称

- `Belial`: システム全体の名称候補
- `Berial`: Discord bot / 人格 / 看板キャラ候補
- `Berialちゃん`: Discord 常駐キャラとしての呼称

### 4.2 Berial の役割

Berial は、市場の欲望・恐怖・ニュース・価格変動を観測する悪魔的な市場監視 bot である。

人格の方向性は以下。

- 市場監視担当
- ニュースを拾う
- 重要な変化を知らせる
- たまに核心を突く
- 辛口だが面倒見がいい
- 欲望を満たすのではなく、欲望の形を見せる

キャラクター設定は `docs/character.md` に分離する。

---

## 5. Discord チャンネル設計

### 5.1 チャンネル一覧

```text
#news-feed
#market-state
#strategy-lab
#bot-alerts
#dev-log
```

### 5.2 各チャンネルの役割

#### #news-feed

生ニュースを投稿するチャンネル。

投稿内容の例。

- 記事タイトル
- 配信元
- 投稿日時
- URL
- カテゴリ
- 重要度スコア

#### #market-state

Berial の市場状態判定を投稿するチャンネル。

将来的に以下を投稿する。

- Bull / Bear / Range / RiskOff / Event
- confidence
- trend_strength
- volatility
- event_risk
- reasons
- counter_reasons

#### #strategy-lab

手法候補・プロンプト・判定ロジックの試作場。

#### #bot-alerts

重要アラート専用。

例。

- FOMC 前
- BOJ 前
- CPI 前
- 雇用統計前
- 急変検知
- VIX 急上昇
- USDJPY 急変

#### #dev-log

bot の裏側のログを投稿する。

投稿内容の例。

- fetch 件数
- post 件数
- skip 件数
- error 件数
- RSS 取得失敗
- Discord 投稿失敗
- 重複排除ファイル破損

---

## 6. ニュース取得仕様

### 6.1 初期ニュースソース

v0.1 では、以下を候補とする。

```text
Google News RSS:
https://news.google.com/rss/search?q=site%3Areuters.com&hl=en-US&gl=US&ceid=US%3Aen

Yahooニュース RSS:
https://news.yahoo.co.jp/rss/categories/business.xml
https://news.yahoo.co.jp/rss/categories/it.xml
https://news.yahoo.co.jp/rss/categories/domestic.xml
https://news.yahoo.co.jp/rss/categories/world.xml
```

### 6.2 Reuters の扱い

Reuters は、公式本文の自動取得対象とはしない。

v0.1 では、Google News RSS 経由で Reuters の見出し・URLを監視するに留める。

以下は禁止する。

- Paywall 回避
- ログイン Cookie を使った本文取得
- 本文の無断スクレイピング
- 記事全文の Discord 再配信

### 6.3 重複排除

同一 URL は再投稿しない。

以下の情報をローカルまたはストレージに保存する。

- posted URL
- posted timestamp
- source
- title hash

重複排除ファイルが破損した場合は、bot を停止させずに復旧する。

---

## 7. 投稿仕様

### 7.1 #news-feed 投稿形式

```text
[カテゴリ] タイトル
Source: Reuters via Google News
Published: 2026-06-27 10:00 JST
URL: https://example.com
```

### 7.2 #dev-log 投稿形式

```text
Berial run completed
fetched: 20
posted: 5
skipped: 15
errors: 0
```

### 7.3 投稿件数制御

現行の `news-feed-bot` では、投稿件数制御を2段階で行う。

| 設定 | 役割 |
|---|---|
| `MAX_ARTICLES_PER_RUN` | RSS feedごとの候補取得上限 |
| `MAX_TOTAL_ARTICLES_PER_RUN` | 1回の実行全体の投稿上限 |

つまり、各RSS feedから一定数まで候補を集めたあと、全候補を `importance_score(article)` で並べ、最終的に `MAX_TOTAL_ARTICLES_PER_RUN` 件まで投稿する。

詳細な現行仕様は「24. 現行 news-feed-bot 仕様」を正とする。

---

## 8. 設定管理

### 8.1 config.json

`config.json` は将来候補である。

現行の `news-feed-bot` は、主に `.env` / 環境変数で設定を管理する。

将来的にRSS feed一覧やスコア重み、投稿先チャンネルなどが増えた場合、変更頻度の高い設定を `config.json` に分離する案がある。

将来候補の例。

```json
{
  "max_articles_per_run": 5,
  "max_total_articles_per_run": 5,
  "fetch_interval_minutes": 60,
  "feeds": [
    "google_news_reuters",
    "https://news.yahoo.co.jp/rss/categories/business.xml",
    "https://news.yahoo.co.jp/rss/categories/it.xml",
    "https://news.yahoo.co.jp/rss/categories/domestic.xml",
    "https://news.yahoo.co.jp/rss/categories/world.xml"
  ]
}
```

### 8.2 .env

現行実装では、秘密情報と運用設定は `.env` または環境変数から読む。

`.env` はGit管理しない。

例。実値は書かない。

```env
NEWS_WEBHOOK_URL=
DEV_LOG_WEBHOOK_URL=
DRY_RUN=false
DEBUG_SELECTION=false
GOOGLE_NEWS_QUERY=
MAX_ARTICLES_PER_RUN=5
MAX_TOTAL_ARTICLES_PER_RUN=5
FETCH_INTERVAL_MINUTES=60
```

### 8.3 .env.example

`.env.example` には安全なサンプル値のみ記載する。

本物のWebhook URL、API key、tokenは書かない。

現行の `.env.example` は、誤送信防止のため `DRY_RUN=true` を既定のサンプルにする。本番投稿する場合は `.env` 側で `NEWS_WEBHOOK_URL` を設定し、`DRY_RUN=false` に変更する。

```env
NEWS_WEBHOOK_URL=
DEV_LOG_WEBHOOK_URL=
DRY_RUN=true
DEBUG_SELECTION=false
GOOGLE_NEWS_QUERY=site:reuters.com (markets OR stocks OR bonds OR forex OR commodities OR oil OR gold OR "central bank" OR inflation OR "interest rates" OR earnings) when:1d
MAX_ARTICLES_PER_RUN=5
MAX_TOTAL_ARTICLES_PER_RUN=5
FETCH_INTERVAL_MINUTES=60
```

---

## 9. セキュリティ要件

- Discord Webhook URL をソースコードに直書きしない
- APIキー、Webhook、トークンは `.env` または Secrets Manager / SSM Parameter Store で管理する
- `.env` は Git 管理しない
- `.gitignore` に `.env` を含める
- `.env.example` にはダミー値のみ記載する
- 漏洩した Webhook は削除・再生成する
- GitHub に秘密情報を push しない

---

## 10. AI 判定方針

### 10.1 基本方針

AI に未来予測をさせない。
AI には、現在の市場状態を分類させる。

NG 例。

```text
明日ドル円は上がりますか？
```

OK 例。

```text
現在の USDJPY は Bull / Bear / Range / Event / RiskOff のどれですか？
```

### 10.2 市場状態分類

分類候補。

- Bull
- Bear
- Range
- RiskOff
- Event
- NoTrade

### 10.3 AI 出力形式

AI の出力は JSON 固定とする。

```json
{
  "pair": "USDJPY",
  "market_state": "range",
  "usd_score": 62,
  "jpy_score": 48,
  "trend_strength": 35,
  "volatility": 70,
  "event_risk": 80,
  "recommended_strategy": "no_trade",
  "confidence": 58,
  "reasons": [
    "重要指標前で方向感が出にくい",
    "直近レンジ上限付近",
    "ボラティリティ上昇中"
  ],
  "counter_reasons": [
    "米金利上昇によりドル買い圧力は残る"
  ]
}
```

---

## 11. 将来の market_data.py 仕様

将来的に `market_data.py` を作成し、以下を取得する。

### 11.1 取得候補

- VIX
- 米10年金利
- S&P500
- Nasdaq
- Nikkei
- TOPIX
- USDJPY
- Gold
- FedWatch
- 経済指標カレンダー

### 11.2 取得元候補

- yfinance
- FRED API
- OANDA API
- CME FedWatch
- Investing.com
- Yahoo Finance

### 11.3 注意

取得元の規約を確認し、無理なスクレイピングは行わない。

---

## 12. チャート特徴量仕様

AI にチャート画像そのものを渡すのではなく、特徴量を渡す方針とする。

### 12.1 特徴量候補

- RSI
- MACD
- ATR
- 移動平均
- 高値更新
- 安値更新
- 出来高
- ボラティリティ
- MA の向き
- 価格と MA の位置関係

### 12.2 基本思想

画像ではなく数値特徴量を渡す。

例。

```text
悪い例:
  チャート画像を渡して雰囲気で判断させる

良い例:
  RSI=72, MACD=positive, ATR=high, price_above_ma=true を渡す
```

---

## 13. 手法選択仕様

将来的には、市場状態に応じて手法候補を選択する。

```text
Market State
↓
Strategy Candidate
↓
Strategy Selection
```

### 13.1 手法候補

#### Bull

- Trend Follow
- Breakout
- MA 押し目

#### Bear

- Trend Follow Short
- 戻り売り
- Breakout Short

#### Range

- Range Trade
- Mean Reversion

#### Event Risk High

- NoTrade

---

## 14. Exit / Risk 管理仕様

### 14.1 背景

過去トレードでは、含み益を大きく伸ばした後に最終利益を大きく減らした経験がある。

そのため、Berial では Entry よりも Exit / Risk / 状態遷移を重視する。

### 14.2 Exit 候補

- Bull 終了で決済
- Bull → Range で半分利確
- Bull → Bear で全決済
- 時間決済
- ATR 損切り
- 最大損失率で損切り
- トレーリングストップ

### 14.3 Risk 管理候補

- ATR 損切り
- 1トレード損失率制限
- 最大 DD 制限
- Tradeable / Not Tradeable 判定
- イベントリスク過大時は取引しない
- 流動性不足時は取引しない

---

## 15. 運用要件

### 15.1 実行間隔

初期は以下を目安とする。

- ニュース取得: 15〜30分
- 市場データ取得: 5〜15分
- AI 判定: 30分〜1時間
- 経済指標予定: 1日1回 + 直前チェック
- アラート: 状態変化時のみ

### 15.2 深夜運用

睡眠を守ることを重要要件とする。

- 深夜通知は抑制する
- 緊急度の高いものだけ通知する
- 朝にまとめる設定を検討する
- 夜間の手動監視を前提にしない

---

## 16. 開発・実行環境

### 16.1 開発環境

- GitHub でコード管理する
- EC2 を開発環境として利用する
- Claude Code / Codex が作業できる構成にする
- README に作業手順を書く

### 16.2 本番実行候補

将来的には以下を目指す。

```text
Lambda
EventBridge Scheduler
CloudWatch Logs
Secrets Manager / SSM Parameter Store
```

### 16.3 役割分担

```text
EC2      = 工房
Lambda   = 店番
GitHub   = 保管庫
Discord  = 司令室
```

---

## 17. AI ツールの役割分担

### 17.1 ChatGPT

- 仕様整理
- 設計壁打ち
- プロンプト作成
- ドキュメント作成
- レビュー

### 17.2 Claude Code

- 初期実装
- 叩き台生成
- 構成作成

### 17.3 Codex

- 実行
- 検証
- 修正
- テスト追加
- 例外処理強化

---

## 18. リポジトリ構成案

```text
BELIAL_dev/
├─ README.md
├─ requirements.txt
├─ .gitignore
├─ .env.example
├─ config.json
├─ news-feed-bot/
│  └─ news_feed_bot.py
├─ src/
│  ├─ market_data.py
│  ├─ market_state.py
│  ├─ strategy_selector.py
│  └─ risk_manager.py
└─ docs/
   ├─ spec.md
   ├─ backlog.md
   ├─ architecture.md
   ├─ trading_policy.md
   ├─ character.md
   ├─ glossary.md
   └─ decisions.md
```

---

## 19. Backlog

### 19.1 完了済み

現行 `news-feed-bot` で完了済みの項目。

- `.gitignore` に `.env` を含める
- `.env.example` を用意する
- Webhook URL のソースコード直書きを避ける
- `NEWS_WEBHOOK_URL` と `DEV_LOG_WEBHOOK_URL` を分離する
- `.env` / 環境変数から設定を読む
- `DRY_RUN=true` / `--dry-run` を実装する
- dry-runではDiscord送信も `posted_ids.json` 更新もしない
- 金融・市場関連キーワードフィルタを追加する
- `importance_score(article)` を追加する
- `MAX_ARTICLES_PER_RUN` と `MAX_TOTAL_ARTICLES_PER_RUN` による2段階上限を追加する
- Discord embed footer にscoreとhit keywordsを表示する
- `DEBUG_SELECTION=true` による選別理由debugログを追加する
- fetch / post / skip / error 件数をdev-logに送る
- `posted_ids.json` による重複排除を行う
- 重複排除ファイル破損時に退避して継続する
- READMEを整備する
- pytestで主要機能を検証する

### 19.2 news-feed-bot の未完了改善候補

- `MIN_IMPORTANCE_SCORE` を導入し、低スコア記事を足切りする
- RSSソースを追加する
- Google News queryを調整する
- RSS feed一覧やスコア重みを将来的に `config.json` 化する
- ソースごとの信頼度・優先度重みを導入する
- Webikeの新車記事など、市場影響が薄い記事をより強く除外する
- Discord投稿を「速報」「通常」「低優先度」に分ける

### 19.3 次段階システム機能

- `market_data.py` を作成する
- `market_state.py` を作成する
- AI 判定 JSON スキーマを固定する
- AI要約を追加する
- ブル/ベア判定を追加する
- 市場インパクト判定を追加する
- `#market-state` 投稿を実装する
- systemd化またはLambda化を検討する

---

## 20. 未決定事項

### 20.1 データソース

- Reuters をどこまで使うか
- Yahoo Finance をどう使うか
- Google News RSS だけで足りるか
- Investing.com を使うか
- CME FedWatch をどう取得するか
- FRED API を使うか
- OANDA API をいつ使うか
- XM / MT5 連携をいつ検討するか

### 20.2 自動売買の範囲

- 市場判定のみで止めるか
- ペーパートレードまで進めるか
- 半自動売買まで進めるか
- 小額自動売買まで進めるか
- 完全自動売買を目指すか

### 20.3 保有期間

候補。

- デイトレ、当日決済
- 1〜3日スイング
- 数週間保有は除外寄り

### 20.4 Entry / Exit

- Entry 条件
- Exit 条件
- StopLoss 条件
- TakeProfit 条件
- 状態遷移時の決済条件

---

## 21. 設計原則

### 21.1 いきなり自動売買しない

最初はニュース監視・市場状態分類・通知に限定する。

### 21.2 AI に未来予測をさせない

AI は現在の市場状態を分類する。

### 21.3 Entry より Exit を重視する

過去の課題から、利益を伸ばすよりも、利益を失わない Exit 設計を重視する。

### 21.4 睡眠を守る

夜間に人間が張り付く運用は失敗条件とする。

### 21.5 秘密情報を守る

Webhook / API key / token は絶対にソースコードに直書きしない。

---

## 22. 次にやること

次に実施する候補は以下。

```text
1. MIN_IMPORTANCE_SCORE 導入を検討する
2. RSSソースを追加する
3. Google News query を調整する
4. systemd化またはLambda化を検討する
5. market_data.py を作成する
6. market_state.py を作成する
7. AI要約・市場インパクト判定を検討する
```

---

## 23. Codex / Claude Code への作業指示テンプレート

```text
このリポジトリでは docs/berial_spec.md を正とします。
実装前に必ず docs/berial_spec.md と関連ドキュメントを確認してください。
勝手に仕様外の機能を追加しないでください。

今回の作業範囲:
- 24章の現行仕様を確認する
- 変更対象ファイルを明示する
- 既存の news-feed-bot 本番挙動を壊さない
- 実Webhook URL、API key、token を書かない
- dry-run、pytest、debugログ仕様を必要に応じて維持・更新する
- 既存機能を壊さない

作業後に以下を報告してください。
- 変更ファイル
- 変更内容
- 動作確認方法
- 残った課題
```

---

## 24. 現行 news-feed-bot 仕様

この章は、現在実装済みの `news-feed-bot/news_feed_bot.py` の仕様を反映する。

1〜23章には初期構想や将来構想も含まれる。現行の `news-feed-bot` の実装詳細については、この24章を正とする。

将来構想と現行仕様が異なる場合は、以下のように扱う。

- 現行挙動の確認・修正では24章を優先する
- 将来拡張の検討では19章、20章、22章、24.14を参照する
- 秘密情報の扱いは9章と24.12を優先する

### 24.1 現在の役割

`news-feed-bot` は、市場監視システム BELIAL におけるニュース収集・一次選別レイヤーである。

現在の役割は以下に限定する。

- Google News / Reuters と Yahoo!ニュース系RSSからニュースを取得する
- 金融・市場関連キーワードで記事を一次選別する
- 重要そうな市場ニュースをDiscordへ通知する
- 既投稿記事や同一実行内の重複記事を除外する
- 記事ごとにルールベースの `importance_score(article)` を計算し、優先順位を付ける
- 実行結果を必要に応じてdev-logへ送る

現時点では、以下は行わない。

- AIによる記事分析
- AIによるブル/ベア判定
- 売買判断
- 自動発注
- 市場インパクトの最終判定

つまり `news-feed-bot` は、BELIAL本体の判断層ではなく、その前段にある「ニュース収集・通知・一次フィルタ」の役割を持つ。

### 24.2 RSS取得元

現行実装では、主に以下のRSSを取得対象にする。

| 種別 | 用途 |
|---|---|
| Google News / Reuters | Reuters由来の市場関連ニュース監視 |
| Yahoo!ニュース / 経済 | 国内経済ニュース |
| Yahoo!ニュース / IT | IT・テック関連ニュース |
| Yahoo!ニュース / 国内 | 国内ニュースのうち市場影響があり得るもの |
| Yahoo!ニュース / 国際 | 国際ニュースのうち市場影響があり得るもの |

Google News queryは `GOOGLE_NEWS_QUERY` で変更可能である。

### 24.3 記事選別フロー

記事選別は以下の順番で行う。

1. RSS取得
2. `posted_ids.json` による既投稿記事の除外
3. 同一実行内の重複記事の除外
4. 金融・市場関連キーワードによるフィルタ
5. RSS feedごとの候補上限 `MAX_ARTICLES_PER_RUN` の適用
6. `importance_score(article)` によるスコアリング
7. `importance_score` の降順に並べる
8. scoreが同点の場合は `published_sort_key` により公開日時が新しい順に並べる
9. 1回の実行全体の投稿上限 `MAX_TOTAL_ARTICLES_PER_RUN` を適用する
10. Discordへ投稿する
11. 本番実行時のみ `posted_ids.json` を更新する

`MAX_ARTICLES_PER_RUN` は「feedごとの候補上限」であり、最終投稿数ではない。

`MAX_TOTAL_ARTICLES_PER_RUN` は「1回の実行全体で実際に投稿する最大件数」である。

たとえば、5つのRSS feedがあり `MAX_ARTICLES_PER_RUN=5` の場合、最大25件が候補になる。その後、score順に並べたうえで `MAX_TOTAL_ARTICLES_PER_RUN=5` なら最大5件だけが投稿される。

### 24.4 金融・市場関連フィルタ

金融・市場関連フィルタは、記事タイトルとsummaryを対象にする。

判定時にはHTMLを除去し、キーワードに一致するかを確認する。

対象例:

- 株価
- 日経平均
- TOPIX
- 株式市場
- 為替
- 円安
- 円高
- ドル円
- 金利
- 日銀
- 日本銀行
- FRB
- 中央銀行
- インフレ
- 消費者物価
- GDP
- 原油
- 金価格
- 暗号資産
- 仮想通貨
- stocks
- markets
- bonds
- forex
- commodities
- oil
- gold
- central bank
- inflation
- interest rates
- earnings

このフィルタは本番でも有効である。

### 24.5 importance_score(article)

`importance_score(article)` は、記事の重要度をルールベースで計算する関数である。

対象:

- title
- summary

判定前処理:

- HTMLタグを除去する
- HTML entityを復元する
- 大文字小文字を無視する

加点対象の例:

| 種別 | 例 |
|---|---|
| 危機・速報 | breaking, urgent, emergency, crisis, shock, war, attack, sanctions, default, recession, 緊急, 速報, 危機, 戦争, 攻撃, 制裁, デフォルト, 景気後退 |
| 中央銀行・金利 | FRB, Federal Reserve, BOJ, 日銀, 日本銀行, central bank, policy rate, interest rates, rate hike, rate cut, 金利, 政策金利, 利上げ, 利下げ, 中央銀行 |
| 経済指標 | CPI, inflation, jobs report, unemployment, GDP, 消費者物価, インフレ, 雇用統計, 失業率 |
| 為替 | forex, dollar, yen, USDJPY, ドル円, 円安, 円高, 為替 |
| 商品 | oil, crude, gold, commodity, 原油, 金, 金価格, 商品先物 |
| 株式・債券 | stocks, markets, bonds, yields, 株価, 日経平均, TOPIX, 株式市場, 債券, 利回り |
| 決算 | earnings, guidance, profit warning, 決算, 業績, 増益, 減益, 下方修正 |
| 暗号資産 | crypto, bitcoin, 暗号資産, 仮想通貨, ビットコイン |

減点対象の例:

| 種別 | 例 |
|---|---|
| 広告・PR | PR, sponsored, advertisement, promotion, キャンペーン, 広告 |
| 個人向け投資記事 | 個人投資家, 投資術, ランキング, おすすめ, 初心者向け |
| コラム・解説系 | 解説, コラム, opinion, column |

現時点の `importance_score(article)` はルールベースであり、AI判定ではない。

将来的には、AI要約や市場インパクト判定の前段スコアとして使うことができる。

### 24.6 Discord投稿仕様

ニュース投稿はDiscord Webhookを通じて行う。

投稿payloadはDiscord embed形式である。

主な内容:

- title
- URL
- summary由来のdescription
- source feed名
- `Score: N`
- hit keywords 最大5個
- 実行時刻

footerには、scoreとhit keywordsを表示する。

例:

```text
Google News / Reuters • Score: 12 • 2026-07-04 14:30 • Hits: breaking, FRB, inflation
```

### 24.7 posted_ids.json と重複除外

`posted_ids.json` は、既に投稿した記事IDを記録するruntime fileである。

目的:

- 同じ記事を繰り返し投稿しない
- bot再起動後も既投稿記事を除外する

同一実行内では、複数RSS feedに同じ記事IDが現れた場合もduplicateとして除外する。

`posted_ids.json` が破損している場合は、破損ファイルを退避し、空のIDリストから継続する。

### 24.8 Dry run仕様

`DRY_RUN=true` または `--dry-run` の場合、botは安全な検証モードで動作する。

dry-runでは以下を行わない。

- Discord Webhookへの実送信
- `posted_ids.json` の更新

dry-runでは、送信予定payloadをコンソールに出力する。

このため、dry-runを繰り返すと同じ記事が候補に見え続ける。これは正常挙動である。

本番実行では、投稿後に `posted_ids.json` が更新されるため、次回以降は既投稿記事として除外される。

### 24.9 DEBUG_SELECTION仕様

`DEBUG_SELECTION=true` のときだけ、記事選別理由のdebugログを出す。

通常運用では `false` を推奨する。調査時のみ `true` にする。

feedごと、および全体で以下のカウントを出す。

| 項目 | 意味 |
|---|---|
| `posted` | `posted_ids.json` に既に存在する |
| `duplicate` | 同一実行内で既に候補化済み |
| `non_finance` | finance filterに一致しない |
| `low_score` | score閾値未満。現時点では閾値未実装のため通常0 |
| `per_feed_limit` | feedごとの候補上限 `MAX_ARTICLES_PER_RUN` で落ちた |
| `total_limit` | 全体投稿上限 `MAX_TOTAL_ARTICLES_PER_RUN` で落ちた |
| `selected` | feedログではfeed内候補数、全体ログでは最終投稿対象数 |

ログ例:

```text
[DEBUG] Google News / Reuters: posted=20 duplicate=1 non_finance=70 low_score=0 per_feed_limit=5 total_limit=0 selected=0
[DEBUG] Yahoo!ニュース / 経済: posted=10 duplicate=0 non_finance=32 low_score=0 per_feed_limit=3 total_limit=0 selected=3
[DEBUG] Selection stats: posted=28 duplicate=0 non_finance=269 low_score=0 per_feed_limit=0 total_limit=0 selected=3
```

`DEBUG_SELECTION=true` はログ量が増えるため、常時有効化は推奨しない。

### 24.10 Dev log仕様

`DEV_LOG_WEBHOOK_URL` が設定されている場合、実行サマリをdev-log用Webhookへ送る。

サマリ項目:

- fetch
- post
- skip
- error
- status
- detail

`DEV_LOG_WEBHOOK_URL` が未設定の場合、dev-log投稿はスキップされる。

### 24.11 現在の運用上の観察

dry-runでは `posted_ids.json` を更新しないため、候補が多く見えることがある。

本番では `posted_ids.json` により既投稿記事が除外されるため、`MAX_TOTAL_ARTICLES_PER_RUN=5` でも候補が3件なら3件しか投稿されない。

これは正常挙動である。

重要なのは、`MAX_TOTAL_ARTICLES_PER_RUN` は「最大5件」であって「必ず5件」ではない、という点である。

候補が少ない場合は、投稿数が5件未満になる。

### 24.12 設定項目

| 設定 | 既定値 | 説明 |
|---|---:|---|
| `NEWS_WEBHOOK_URL` | empty | ニュース投稿先Discord Webhook URL。本番投稿時に必要 |
| `DEV_LOG_WEBHOOK_URL` | empty | 実行サマリ投稿先Discord Webhook URL。任意 |
| `DRY_RUN` | `false` | `true` のときDiscord送信も `posted_ids.json` 更新もしない |
| `DEBUG_SELECTION` | `false` | `true` のとき記事選別理由のdebugログを出す |
| `GOOGLE_NEWS_QUERY` | market query | Google News RSS検索条件 |
| `MAX_ARTICLES_PER_RUN` | `5` | RSS feedごとの候補上限 |
| `MAX_TOTAL_ARTICLES_PER_RUN` | `5` | 1回の実行全体の投稿上限 |
| `FETCH_INTERVAL_MINUTES` | `60` | RSS取得間隔 |

`.env.example` には実値を記載しない。

本物のWebhook URL、API key、tokenは `.env` または実行環境のSecret管理に置く。

### 24.13 pytestによる検証範囲

現行実装では、pytestにより主要機能を検証している。

検証対象の例:

- RSS取得結果からDiscord payloadを作れること
- `posted_ids` に存在する記事を除外すること
- `posted_ids` に存在しない記事だけ送信対象になること
- 日本語金融キーワードで記事が選別されること
- 金融キーワードを含まない一般記事が除外されること
- `importance_score(article)` が重要語で加点されること
- PR、ランキング、個人投資家などで減点されること
- scoreが高い記事が低い記事より優先されること
- score同点時に公開日時が新しい記事が優先されること
- `MAX_TOTAL_ARTICLES_PER_RUN` により全体投稿数が制限されること
- dry-runではDiscord送信しないこと
- dry-runでは `posted_ids.json` が更新されないこと
- `DEBUG_SELECTION=true` のときdebugログが出ること
- `DEBUG_SELECTION=false` のときdebugログが出ないこと

### 24.14 現在の課題・次の改善候補

現在の課題:

- 候補が少ない場合、投稿数が5件未満になる
- Webikeの新車記事など、市場影響が薄い記事が混ざる可能性がある
- 現在の `importance_score(article)` はルールベースであり、文脈理解は行わない
- `low_score` はdebug項目として存在するが、現時点では最低score閾値が未実装のため通常0である

改善候補:

- `MIN_IMPORTANCE_SCORE` を導入し、低スコア記事を足切りする
- Google News queryを調整して候補数を増やす
- RSSソースを追加する
- ソースごとの重み付けを導入する
- hit keywordsをより説明的に整形する
- AI要約を追加する
- ブル/ベア判定を追加する
- 市場インパクト判定を追加する
- Discord投稿を「速報」「通常」「低優先度」に分ける

将来的には、`news-feed-bot` が収集した記事をBELIALのAI分析層へ渡し、市場状態分類や戦略判断の材料にする。
