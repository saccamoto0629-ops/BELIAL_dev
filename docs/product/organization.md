# Organization / Pepperoni

Berial には伝承上で広く定着した名前付き眷属が存在しないため、BELIAL DEV では独自の眷属組織を定義する。

BELIAL軍団の眷属は、炎・辛味・スパイスをモチーフとしたコードネームで統一する。各眷属は市場における専門役割を持ち、Berial の判断や命令を受けて個別業務を担当する。

## 組織方針

| 名前 | 役割 | ステータス |
|---|---|---|
| Berial | 軍団長 / 市場監視・戦略立案 | 実装中 |
| Pepperoni | 将来の Live Execution(Broker API による実発注・エントリー執行)担当 | 構想追加(Live Execution は未実装。仮想約定の Paper Execution は `paper-execution/` に実装済み) |
| Jalapeño | ブレイクアウト・モメンタム担当 | 未定 |
| Habanero | 高リスク戦略・危険相場担当 | 未定 |
| Tabasco | Discord通知・アラート担当 | 未定 |
| Paprika | レポート・可視化担当 | 未定 |
| Carolina | 最終兵器・特殊判断担当 | 未定 |

```text
Berial
  ├─ Pepperoni  # Execution (Entry / Order) — 将来・未実装
  ├─ Jalapeño   # Momentum / Breakout
  ├─ Habanero   # High Risk Strategy
  ├─ Tabasco    # Alert / Notification
  ├─ Paprika    # Report / Dashboard
  └─ Carolina   # Special / Emergency Strategy
```

## Pepperoni の役割

Pepperoni は、Berial から渡された市場判断をもとに、エントリーを実行する将来の **Execution** 担当の悪魔である。約定・ポジション管理・執行ログを受け持つ。

裁量判断は苦手だが、命令を忠実かつ高速に実行することを得意とする。市場判断そのものは Berial が担当し、Pepperoni は「いつ、どの条件で、どの注文を出すか」を実装上の責務として受け持つ。

Execution 層は **Paper Execution**(仮想約定・仮想ポジション管理による損益評価。**実装済み** = `paper-execution/`)と **Live Execution**(Broker API による実注文・実約定。**未実装**)に分かれる。仮想position の pending / open / closed / expired / cancelled 管理、仮想 Open / Close 判定、Paper Signal からの仮想執行はすでに実装されている。Pepperoni が象徴するのは Broker API による実発注(Live Execution)であり、これは未実装である。いずれの場合も、過去データで仮説を検証する Backtest(検証基盤)とは別レイヤーである。

## Pepperoni の責務

- エントリー執行
- ポジションオープン
- 発注管理
- エントリー履歴保存
- 約定確認
- 実行ログ出力

将来的な実装イメージ:

```text
Berial
  └─ Pepperoni  # Execution(将来・未実装)
      ├─ Entry Execution
      ├─ Order Management
      ├─ Position Open
      └─ Execution Log
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

Pepperoni 自身は炎のエフェクトを出さないが、このペンだけが炎を帯びる。これは「市場に入る瞬間だけ火が灯る」ことを示す演出であり、Berial の判断が実際のポジションへ変換される境界を表す。

## Representative Lines

> 「えっと……エントリーします……。」

> 「……失敗しました。すみません……。」

> 「もう一度だけ……やらせてください。」

> 「Berial様の判断なら……信じます。」

> 「だ、大丈夫です……最悪、私が市場で身体を張りますから……。」

## 実装上の注意

Pepperoni は将来的な **Live Execution** レイヤー(Broker API による実発注・実約定・実ポジション管理)を象徴するキャラクターである。Execution は Paper Execution(仮想約定・仮想ポジション管理。**実装済み** = `paper-execution/`)と Live Execution(Broker API による実注文。**未実装**)に分かれる。仮想執行(Paper Execution)はすでに実装され、Pepperoni が象徴する実発注(Live Execution)は現行スコープ(v1.4系時点)では実装しない。

なお、過去データで仮説を遡及検証する Backtest は独立した検証基盤であり、Execution レイヤーそのものではない。
