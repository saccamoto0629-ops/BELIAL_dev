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

v0.1 では、1回の実行につき最大投稿件数を設定可能にする。

初期値。

```json
{
  "max_articles_per_run": 5
}
```

---

## 8. 設定管理

### 8.1 config.json

変更頻度の高い設定は `config.json` に分離する。

候補。

```json
{
  "max_articles_per_run": 5,
  "fetch_interval_minutes": 15,
  "feeds": [
    "https://news.yahoo.co.jp/rss/categories/business.xml",
    "https://news.yahoo.co.jp/rss/categories/it.xml",
    "https://news.yahoo.co.jp/rss/categories/domestic.xml",
    "https://news.yahoo.co.jp/rss/categories/world.xml"
  ]
}
```

### 8.2 .env

秘密情報は `.env` に置く。

```env
NEWS_WEBHOOK_URL=
DEV_LOG_WEBHOOK_URL=
OPENAI_API_KEY=
OANDA_API_KEY=
```

### 8.3 .env.example

`.env.example` にはダミー値のみ記載する。

```env
NEWS_WEBHOOK_URL=https://discord.com/api/webhooks/xxxxx/yyyyy
DEV_LOG_WEBHOOK_URL=https://discord.com/api/webhooks/xxxxx/yyyyy
OPENAI_API_KEY=sk-xxxxx
OANDA_API_KEY=xxxxx
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

### 19.1 最優先

- `docs/spec.md` を作成する
- `docs/backlog.md` を作成する
- `.gitignore` に `.env` を追加する
- `.env.example` を作成する
- Webhook URL をソースコードから除去する
- 漏洩済み Webhook を削除・再生成する

### 19.2 news_feed_bot 改善

- RSS URL を config 化する
- 最大投稿件数を config 化する
- `NEWS_WEBHOOK_URL` と `DEV_LOG_WEBHOOK_URL` を分離する
- Ctrl+C 時に安全終了する
- fetch / post / skip / error 件数を dev-log に送る
- 重複排除ファイル破損時に復旧する
- README を追加する
- 最低限のテストを追加する

### 19.3 次段階

- 市場関連キーワードフィルタを追加する
- ニュース重要度スコアを追加する
- `market_data.py` を作成する
- `market_state.py` を作成する
- AI 判定 JSON スキーマを固定する
- `#market-state` 投稿を実装する

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

次に実施する作業は以下。

```text
1. この仕様書を docs/spec.md として GitHub に配置する
2. docs/backlog.md を作成する
3. .env / .env.example / .gitignore を整備する
4. news_feed_bot.py の Webhook 直書きを除去する
5. dev-log 通知を追加する
6. config.json 対応を追加する
7. Codex / Claude Code に仕様書を読ませてから改修させる
```

---

## 23. Codex / Claude Code への作業指示テンプレート

```text
このリポジトリでは docs/spec.md を正とします。
実装前に必ず docs/spec.md と docs/backlog.md を確認してください。
勝手に仕様外の機能を追加しないでください。

今回の作業範囲:
- news_feed_bot.py の Webhook 直書きを廃止する
- NEWS_WEBHOOK_URL / DEV_LOG_WEBHOOK_URL を .env から読む
- .env.example を追加する
- .gitignore に .env を追加する
- fetch/post/skip/error 件数を #dev-log に投稿する
- 既存機能を壊さない

作業後に以下を報告してください。
- 変更ファイル
- 変更内容
- 動作確認方法
- 残った課題
```
