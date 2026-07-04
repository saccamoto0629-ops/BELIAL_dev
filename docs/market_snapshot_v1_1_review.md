# Berial Market Data v1.1 Review Notes

## 今回の改善内容

- Yahoo Finance / FRED / TradingView の取得を `ThreadPoolExecutor` で並列化した。
- provider単位で例外を握り、1サービスが失敗しても他サービスの結果でSnapshotを継続するようにした。
- Yahoo Financeに以下を追加した。
  - Dollar Index: `DX-Y.NYB`
  - WTI Crude Oil: `CL=F`
  - VVIX: `^VVIX`
- FREDに以下を追加した。
  - CPI: `CPIAUCSL`
  - PCE: `PCEPI`
  - Unemployment Rate: `UNRATE`
- TradingViewに以下を追加した。
  - ADX
  - Stoch.K
  - Stoch.D
- Risk Label判定を関数内に整理し、VIX / VVIX / 株指数 / US10Y / Dollar Indexを考慮するようにした。
- Market Snapshot表示にDollar Index、Oil、VVIX、CPI、PCE、Unemployment、ADX、STOCHを追加した。
- 既存のDiscord向けプレーンMarkdown形式は維持した。
- 主要関数へ型ヒントとTypedDictを追加した。
- APIアクセス部分をmock/fake化したpytestを追加・更新した。

## 既存互換性

- `get_market_data()`, `get_fred_data()`, `get_tradingview_data()`, `get_market_snapshot_message()` の既存呼び出し形式は維持した。
- 各データ取得関数は失敗時に例外を外へ投げず、`None` または空dict相当で継続する方針を維持した。
- FRED API keyが未設定の場合は、Bot全体を止めずにFREDセクションをunavailableとして扱う。

## 今回追加しなかったもの

- Economic Calendar、FedWatch、Fear & Greed、AI Risk Scoreは、外部API選定・認証・利用制限の確認が必要なため今回は実装していない。
- Snapshotがさらに長くなる可能性はあるが、Discordの既存投稿形式維持を優先し、embed分割や折りたたみ風の大幅な投稿形式変更は行っていない。

## 今後の改善候補

- Economic Calendar providerの追加。
- FedWatch providerの追加。
- Fear & Greed providerの追加。
- AI Risk Scoreの追加。
- providerごとのタイムアウト値・retry回数の設定化。
- Snapshot本文が長くなった場合のDiscord embed分割、または重要セクション優先表示。
- Risk Labelの理由文を併記する `risk_reasons` の追加。
