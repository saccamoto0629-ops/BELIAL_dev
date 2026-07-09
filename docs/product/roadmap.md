# Roadmap
## 今後の拡張予定

- `MIN_IMPORTANCE_SCORE` による低スコアニュースの足切り
- RSSソース追加、Google News query調整
- Market Snapshotのsystemd化またはLambda化
- `market_bias.py` による Bull / Bear / Neutral 判定の改善
- AI要約、市場インパクト判定
- Fear & Greed
- AI Risk Score
- Polymarket
- COT Report

FedWatch / Economic Calendar は外部取得元が不安定なため、今後も必須Providerではなく optional Provider として扱う。

---
## v1.3.1以降の検討

- `Risk Off` / `USD Strength` / `Gold Strength` など、より細かい市場状態分類(Risk-On / Risk-OffはIssue #3で実装済み。`USD Strength` / `Gold Strength` などのラベルは引き続き検討事項)
- ~~Market Bias判定履歴の保存と状態遷移の追跡~~ → Issue #17でDB永続化と時点参照APIを実装済み(v1.4 Backtestの土台)。状態遷移の追跡そのものはBacktest実装側の検討事項として残る。
- 判定ロジックのバックテスト・閾値チューニング → v1.4 Backtest(下記)で継続検討
- Chart Snapshotの定期投稿頻度調整
- ATR / 出来高 / Fear & Greed / COT Report などの補助指標追加
- デモ口座でのPaper Signal記録
- Pepperoniによる将来のエントリー執行レイヤー

これらは v1.3.1 以降の検討事項であり、v1.3.0では実装しない。

---
## v1.4 Backtest(Issue #17〜#28)

v1.4は、v1.3までに蓄積したChart Snapshot / Market Biasの永続データをもとに、過去の市場状態を再生するBacktest層を追加するリリースとして計画している。対象IssueはGitHub上の #17〜#28。

現時点でこのリポジトリに実装が確認できるものは以下のみである。

| Issue | 内容 | 状態 |
|---|---|---|
| #3 | Market BiasにRisk-On / Risk-Off軸を追加 | 実装済み(v1.3系) |
| #15 | Chart Snapshotのx軸表示をUTCからJSTへ変換 | 実装済み(v1.3系) |
| #16 | MA touch alertの同一接触ゾーン連続通知を抑制 | 実装済み(v1.3系) |
| #17 | Market BiasをSnapshot/DBへ永続化し、Backtestから参照可能にする | 実装済み。Backtest本体の土台 |
| #18〜#28 | Backtest本体の実装(範囲・詳細はGitHub Issueを参照) | 未着手。本ドキュメントの時点ではリポジトリ上に実装なし |

v1.4 Backtest群に共通する非スコープ(各Issueで個別に明記されているものを集約):

- 自動エントリー
- 自動Exit
- 損切り判断
- 利確判断
- Broker API 連携
- 売買推奨文言の生成

Backtestは「過去の観測・分類データを読み取って検証する」レイヤーであり、Execution層(発注・ポジション管理)ではない。[Concept](concept.md#レイヤー分離) のレイヤー分離を維持したまま追加する。

`#18`以降の詳細が確定し次第、本セクションと [Market Bias](../architecture/market_bias.md#永続化とbacktest参照issue-17) を更新する。

---
## 本番常駐化(v1.5以降の検討: systemd timer案)

v1.4.0時点ではsystemd化を行わない。将来検討する場合は、`macd_alert_bot.py`のような常駐daemon(systemd service)ではなく、**systemd timer + oneshot service** による定期実行を推奨する。理由は、Paper Signalが「一定間隔での市場状態スナップショット判定」であり、常駐プロセスを維持する必要性が薄いため。

```ini
# /etc/systemd/system/berial-paper-signal.service (案、oneshot)
[Unit]
Description=Berial Paper Signal Generator (oneshot)

[Service]
Type=oneshot
WorkingDirectory=/opt/berial/paper-signal
ExecStart=/usr/bin/python3 /opt/berial/paper-signal/signal_generator.py --once
```

```ini
# /etc/systemd/system/berial-paper-signal.timer (案)
[Unit]
Description=Run Berial Paper Signal Generator every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
```

これはv1.4.0では実装しない案であり、実際のunit配置・有効化はv1.5以降の検討事項とする。
## v1.5以降の検討

- systemd timer化(Roadmapの案を実装する)
- Paper Signalの結果検証(仮説が実際の値動きとどう対応したかの事後集計。発注は行わない)
- 複数時間足・複数通貨ペアへの対応
- Entry Bot / Pepperoni(まだ設計しない)

これらはv1.5以降の検討事項であり、v1.4.0では実装しない。
