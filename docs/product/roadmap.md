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
- ~~Pepperoniによる将来のエントリー執行レイヤー~~ → 仮想執行(Paper Execution)は Issue #49〜#51 で `paper-execution/` に実装済み。実発注(Live Execution)は引き続き未実装・非スコープ

これらは v1.3.1 以降の検討事項であり、v1.3.0では実装しない。

---
## v1.4 Backtest / Paper Signal(v1.4.1 リリース済み)

v1.4系は、v1.3までに蓄積したChart Snapshot / Market Biasの永続データをもとに、過去の市場状態を再生するBacktest層(検証基盤)を追加したリリースラインである。**v1.4.1 は、既存の `v1.4` タグ以降に入った修正・整理をまとめたリリース済みのパッチリリース**であり、Market State 局面制御、銘柄別 Market Bias、戦略プロファイル、Paper Signal の常駐サービス化・日次集計レポートを含む。Paper Executionと外部Strategy定義(YAML)は `v1.4.1` タグ後の開発で追加されたため、v1.4.1の収録機能には含めない。

| Issue | 内容 | 状態 |
|---|---|---|
| #3 | Market BiasにRisk-On / Risk-Off軸を追加 | 実装済み(v1.3系) |
| #15 | Chart Snapshotのx軸表示をUTCからJSTへ変換 | 実装済み(v1.3系) |
| #16 | MA touch alertの同一接触ゾーン連続通知を抑制 | 実装済み(v1.3系) |
| #17 | Market BiasをSnapshot/DBへ永続化し、Backtestから参照可能にする | 実装済み。Backtest本体の土台 |
| #18〜#28, #33 | Backtest Engine / results DB / Exit・Invalidation / warm-up / Runner / CLI / summary / fixture / 仮想約定価格モデル / signal_type・trigger_type / Live-Backtest 生成一致 | 実装済み(`v1.4` タグ時点) |
| #38 | 再エントリー抑制・cooldown・時系列Market Bias対応 | 実装済み(v1.4.1) |
| #39 | Market State判定を導入し、局面ごとにエントリーを制御(`--allowed-market-states`) | 実装済み(v1.4.1) |
| #40 | GLOBAL Biasから銘柄別Market Biasへ対応 + 履歴backfillツール | 実装済み(v1.4.1) |
| #42 | XAUUSD Market StateフィルタのOOS検証 | 検証完了(判定=保留。Liveへは未反映, v1.4.1) |
| #44 | XAUUSD squeeze breakout戦略A(`--strategy-profile xauusd_1h_squeeze_breakout_v1`) | 実装済み(v1.4.1, paper-only) |
| #41 | Paper Signal を常駐サービスとして実装 | 実装済み(v1.4.1) |
| #47 | Paper Signal 日次集計レポート(読み取り専用) | 実装済み(v1.4.1) |

v1.4 Backtest群に共通する非スコープ(各Issueで個別に明記されているものを集約。いずれも実売買=Live Execution としての意味であり、仮想執行はのちに Issue #49〜#51 で Paper Execution として実装された):

- 自動エントリー
- 自動Exit
- 損切り判断
- 利確判断
- Broker API 連携
- 売買推奨文言の生成

Backtestは「過去の観測・分類データを読み取って検証する」レイヤーであり、Execution層(発注・ポジション管理)ではない。[Concept](concept.md#レイヤー分離) のレイヤー分離を維持したまま追加している。`backtest/execution_model.py` の spread / slippage / fill は Backtest 内部のシミュレーションモデルであり、Paper Execution レイヤーの実装ではない。

### v1.4.1 の既知の制約

- Market State / 戦略フィルタのエッジは銘柄固有で、汎用的な優位性は確認されていない(#40: XAUのみ改善・USDJPYは逆効果、#42: XAUは判定=保留)。Live への反映は行っていない。
- squeeze breakout 戦略(#44)は paper-only の Backtest 検証用であり、実発注・自動Exitは行わない。
- Paper Signal 蓄積結果の長期評価(仮説が実際の値動きとどう対応したか)は v1.5 以降の検討事項。

---
## 本番常駐化(systemd)

**現行(v1.4系): 常駐daemon用の systemd service は実装済み。** Paper Signal を常駐サービスとして動かす service テンプレート `scripts/systemd/berial-paper-signal.service`(`Type=simple` / `Restart=always`、内部の周期ループで一定間隔ごとに判定)を提供している(Issue #41)。配備・有効化・停止の手順は [Operations](../operations/operations.md) を参照。

**将来候補: systemd timer + oneshot。** 常駐プロセスを維持せず、`--once` を **systemd timer + oneshot service** で定期起動する方式も選択肢として残す。理由は、Paper Signalが「一定間隔での市場状態スナップショット判定」であり、常駐プロセスの維持が必須ではないため。ただし現行の常駐 service で運用要件を満たしているため、timer + oneshot への移行は v1.5 以降の検討事項とする。

```ini
# /etc/systemd/system/berial-paper-signal.service (将来候補、oneshot)
[Unit]
Description=Berial Paper Signal Generator (oneshot)

[Service]
Type=oneshot
WorkingDirectory=/opt/berial/paper-signal
ExecStart=/usr/bin/python3 /opt/berial/paper-signal/paper_signal_bot.py --once
```

```ini
# /etc/systemd/system/berial-paper-signal.timer (将来候補)
[Unit]
Description=Run Berial Paper Signal Generator every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
```

timer + oneshot 方式の実際のunit配置・有効化はv1.5以降の検討事項とする。
## v1.4.1以降の実装と今後の検討

**v1.4.1タグ後に実装済み:**

- **Paper Execution**: Paper Signal を入力に、時間進行に沿って仮想約定・仮想ポジション管理・仮想Exit・仮想損益を行う執行層(Live Execution とは独立)。実注文・Broker API 連携・自動売買・ロット計算は含めない。Issue #49 で最小責務・状態遷移・約定規則を設計([Paper Execution](../architecture/paper_execution.md))、Issue #50 / #51 でデータモデル・保存層および Open / Close 判定エンジンを `paper-execution/` に実装した。
- **外部Strategy定義(Backtest経路)**: Issue #52〜#54でYAML schema、loader / validator、`--strategy-file` / `--strategy` CLI、および `backtest/strategies/xauusd_1h_squeeze_breakout_v1.yaml` を実装した。外部定義を読み込むのは現在Backtest経路のみであり、Paper Signal / Paper Executionが同じ定義を共有する経路は未実装。
- **Broker interface / FakeBroker(Issue #73)**: 将来の Live Execution に向けた Broker 境界(共通インターフェース・Broker 非依存の共通モデル)と、外部通信を一切行わないテストダブル FakeBroker を `broker/` に実装した([Broker Interface](../architecture/broker_interface.md))。これは**境界の定義のみ**であり、実発注・OANDA Adapter・Safety Gate・Live Execution runner は引き続き未実装・非スコープ。Paper Execution は従来どおり Broker 非依存の仮想執行のままで、この境界を使わない。
- **Execution Request / 注文状態永続化基盤(Issue #74)**: Strategy / Signal から将来の Live Execution ユースケースへ渡す `ExecutionRequest` モデルと、注文状態・状態履歴(状態変更理由・broker_order_id・UNKNOWN 照会対象の抽出を含む)を SQLite へ永続化する基盤を `execution/` に実装した([Execution Request / Order Persistence](../architecture/execution_request.md))。注文状態は既存 `broker.OrderState` を唯一の正として再利用する。これも**将来の Live Execution 用のモデル・状態永続化基盤のみ**であり、Live Execution 本体・Broker API Adapter・実注文・Safety Gate・reconciliation runner は引き続き未実装・非スコープ。

**今後の検討:**

- systemd timer + oneshot 化(上記「本番常駐化」の将来候補を実装する)
- Paper Signalの結果検証(仮説が実際の値動きとどう対応したかの事後集計。発注は行わない)
- 複数時間足・複数通貨ペアへの対応
- Live Execution / Broker API(Pepperoni の実発注担当分。Broker 境界は Issue #73、ExecutionRequest / 注文状態永続化基盤は Issue #74 で定義済み。実発注・OANDA Adapter・runner・Safety Gate・reconciliation はまだ設計しない)
- 外部Strategy定義をPaper Signalへ接続し、Live相当のSignal生成とBacktestを同一定義で駆動する共有化

Broker API による実注文・自動売買(Live Execution)は引き続き未実装・非スコープである。実装済みの外部Strategy定義をPaper Signal / Paper Executionへ接続する共有化は、現在のアーキテクチャの一部ではなく将来構想として扱う。
