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

- `Risk Off` / `USD Strength` / `Gold Strength` など、より細かい市場状態分類
- Market Bias判定履歴の保存と状態遷移の追跡
- 判定ロジックのバックテスト・閾値チューニング
- Chart Snapshotの定期投稿頻度調整
- ATR / 出来高 / Fear & Greed / COT Report などの補助指標追加
- デモ口座でのPaper Signal記録
- Pepperoniによる将来のエントリー執行レイヤー

これらは v1.3.1 以降の検討事項であり、v1.3.0では実装しない。

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
