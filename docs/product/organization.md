# Organization

Berial には伝承上で広く定着した名前付き眷属が存在しないため、BELIAL DEV では独自の眷属組織を定義する。

BELIAL軍団の眷属は、炎・辛味・スパイスをモチーフとしたコードネームで統一する。各眷属は市場における専門役割を持ち、Berial の判断や命令を受けて個別業務を担当する。キャラクター設定は技術的責務([Architecture](../architecture/architecture.md))と矛盾させない。責務の正は architecture 側とする。

## 確定済みの役割

現行実装・運用に対応する確定済みメンバーは以下の4名である。

| 名前 | 役割 | 対応する実装レイヤー |
|---|---|---|
| Berial | 軍団長 / 市場監視、全体判断、戦略立案 | Observation / Classification / Strategy(Market Watch, Market Bias, Strategy 評価) |
| Pepperoni | Entry / Position / Execution 系 | Paper Execution(`paper-execution/`: 仮想Entry、仮想ポジション管理、SL / TP / Exit 判定、Position Store)と、Live Execution 基盤(`broker/` + `execution/`: Broker 境界・Adapter・Safety Gate・送信境界・journal・reconciliation・Kill Switch・Live 接続ロック。**実装済みだが常駐 runner 未実装・実口座運用は未解禁**) |
| 花椒 | Paper Signal / Test 結果・データ集約、外部データ窓口 | Paper Signal(`paper-signal/`: paper_only 仮説の生成・保存・通知イベント・日次集計レポート)、Backtest 結果の集約、外部データ取得(RSS / マーケットデータ Provider) |
| Tabasco | Chart Alert / Discord 通知の象徴 | Alert(`market-watch/*_alert_bot.py`)と各コンポーネント内の Discord 配送経路を象徴する。独立した共通通知モジュールではない |

```text
Berial(軍団長 / 市場監視・全体判断・戦略立案)
  ├─ Pepperoni  # Entry / Position / Execution(Paper Execution は実装済み・運用中、
  │             #   Live Execution は基盤実装済み・runner 未実装・実口座運用は未解禁)
  ├─ 花椒       # Paper Signal / Test 結果・データ集約、外部データ窓口
  └─ Tabasco    # Chart Alert / Discord 通知
```

## 候補キャラクター(未採用・未定)

以下は構想段階のキャラクターであり、確定済みの役割と同列には扱わない。現行実装に対応する責務を持たず、採用・役割とも未定である。

| 名前 | 構想上の役割 | ステータス |
|---|---|---|
| Jalapeño | ブレイクアウト・モメンタム担当 | 未定 |
| Habanero | 高リスク戦略・危険相場担当 | 未定 |
| Paprika | レポート・可視化担当 | 未定 |
| Carolina | 最終兵器・特殊判断担当 | 未定 |

## Pepperoni の役割

Pepperoni は、Berial から渡された市場判断をもとに、Entry / Position / Execution 系を受け持つ悪魔である。約定・ポジション管理・執行ログを受け持つ。

裁量判断は苦手だが、命令を忠実かつ高速に実行することを得意とする。市場判断そのものは Berial が担当し、Pepperoni は「いつ、どの条件で、どの注文を出すか」を実装上の責務として受け持つ。

Execution 層は **Paper Execution**(仮想約定・仮想ポジション管理による損益評価。**実装済み** = `paper-execution/`)と **Live Execution**(Broker API による実注文・実約定。**基盤実装済み・常駐 runner 未実装・実口座運用は未解禁** = `broker/` + `execution/`)に分かれる。現行実装で Pepperoni の担当領域のうち日常運用されているのは Paper Execution であり、仮想position の pending / open / closed / expired / cancelled 管理、仮想 Open / Close 判定、SL / TP / 最大保有時間の判定、Paper Signal からの仮想執行がすでに実装されている。いずれも仮想レコードのみで、実発注は行わない。過去データで仮説を検証する Backtest(検証基盤)とは別レイヤーである。

## Pepperoni の責務

現行(Paper Execution・実装済み):

- 仮想エントリー執行(Entry 価格決定・open 遷移)
- 仮想ポジションの状態管理と保存(Position Store)
- SL / TP / 最大保有時間による仮想 Exit 判定
- 仮想損益(pnl)の算出・記録
- 実行ログ出力

現行(Live Execution 基盤・実装済みだが運用未解禁):

- Broker 境界・共通モデル・FakeBroker(Issue #73)と OANDA Demo / Live Adapter(Issue #79 / #82)
- ExecutionRequest・注文状態・状態履歴・再発行チェーンの永続化(Issue #74 / #95 / #115)
- 発注前 Safety Gate と唯一の送信・キャンセル境界 `LiveOrderSubmissionService`(Issue #75 / #99)
- Submission / Cancellation Journal による送信試行の耐久記録(Issue #93 / #99)
- Broker reconciliation(検出・修復・position スコープ・手動 CLI。Issue #103〜#105)
- Kill Switch による緊急発注停止(Issue #81)
- Live 接続の二重以上のロックと起動前 preflight(Issue #82)

将来(未実装・未解禁):

```text
Berial
  └─ Pepperoni  # Live Execution の運用(未解禁)
      ├─ 定期実行 runner(実発注の駆動。未実装)
      ├─ 自動売買(未実装)
      └─ 実口座での Live 運用開始(Issue #82 の merge だけでは開始しない。
          運用ドキュメントの事前条件を満たしたうえで別途判断)
```

## Pepperoni の性格

- 気弱
- 真面目
- 自己評価が低い
- 命令には忠実
- 褒められると頑張る
- 失敗すると極端に落ち込む
- 損失や失敗を自分で背負い込もうとする

## Pepperoni のデザイン

- 年上感のある長身のお姉さん
- ピンク色のロングヘア
- 大きな垂れ目
- 黒い悪魔角
- Berial軍団の紋章を模した髪留め
- 黒×ピンクを基調とした衣装
- 炎を宿したエントリーペンを所持
- キャラクター本体から炎のエフェクトは出さない
- 気弱で不安げだが、命令を受けると行動する

## Weapon: Entry Flame Pen

`Entry Flame Pen` は、市場へエントリー命令を刻むための悪魔のペンである。

Pepperoni 自身は炎のエフェクトを出さないが、このペンだけが炎を帯びる。これは「市場に入る瞬間だけ火が灯る」ことを示す演出であり、Berial の判断がポジションへ変換される境界を表す。現行実装で灯るのは仮想ポジション(Paper Execution)である。実際の市場に火を入れる Live Execution は、ペン先(接続・発注経路と安全ロック)まで実装済みだが、**実口座で火を灯すこと(Live 運用)は未解禁**である。

## Representative Lines

> 「えっと……エントリーします……。」

> 「……失敗しました。すみません……。」

> 「もう一度だけ……やらせてください。」

> 「Berial様の判断なら……信じます。」

> 「だ、大丈夫です……最悪、私が市場で身体を張りますから……。」

## 花椒の役割

花椒(ホアジャオ)は、Paper Signal と Test / Backtest 結果のデータ集約、および外部データの窓口を受け持つ。

- Paper Signal(paper_only 仮説)の生成・保存、通知イベントの所有と日次集計レポート(`paper-signal/`)
- Backtest / テスト結果のデータ集約(Backtest summary、検証レポート)
- 外部データの窓口(RSS ニュース、マーケットデータ Provider からの取得)

花椒が扱うのは観測・仮説・集計であり、執行判断(Entry / Exit / SL / TP)は持たない。執行系は Pepperoni の領域である。キャラクターとしての性格・デザイン詳細は未整備(本書では役割のみ確定)。

## Tabasco の役割

Tabasco は、Chart Alert と Discord 通知・配送経路を象徴するキャラクターである。ただし、これはキャラクター上の責務整理であり、現行実装に独立した共通の「Tabasco通知モジュール」が存在するわけではない。

- MA / MACD / Bollinger Bands / RSI のイベント検知と通知(`market-watch/*_alert_bot.py`)
- Chart Snapshot / 各種通知の Discord 投稿経路(現行は各コンポーネント内に個別実装)

Alert は「何が起きたか」という事実の通知であり、売買判断を含まない([Concept の基本方針](concept.md#基本方針))。Paper Signal の通知は `paper-signal/signal_generator.py`、Chart Alert の通知は各 `market-watch/*_alert_bot.py` から直接送信される。花椒はPaper Signalの生成結果・通知イベントを所有し、Tabascoは横断的な配送責務を象徴するが、両者を実装モジュールと一対一対応させない。キャラクターとしての性格・デザイン詳細は未整備(本書では役割のみ確定)。

## 実装上の注意

- Pepperoni の現行の運用担当は **Paper Execution**(仮想約定のみ。`paper-execution/`)である。**Live Execution は基盤(Broker 境界・Adapter・Safety Gate・送信境界・reconciliation・Kill Switch・Live 接続ロック)まで実装済み**だが、実発注を駆動する常駐 runner・自動売買は未実装であり、**実口座での Live 運用は未解禁**である(責務の正は [Architecture](../architecture/architecture.md)、運用条件の正は [Operations](../operations/operations.md))。
- 過去データで仮説を遡及検証する **Backtest** は独立した検証基盤であり、Execution レイヤーそのものではない。結果の集約は花椒の担当領域として扱う。
- 候補キャラクター(Jalapeño / Habanero / Paprika / Carolina)は未採用であり、実装上の責務を割り当てない。
