# Architecture

BELIAL の設計資料を置く。

- 全体構成
- コンポーネント責務
- データベース設計
- Market Watch / Market Bias / Paper Signal などの現行仕様

## 設計文書

- [Strategy 定義の外部化仕様](strategy_definition.md)(Issue #52): Strategy ごとの可変設定を外部
  ファイルへ切り出すためのファイル形式・スキーマ・エラー方針・後方互換性方針。サンプル定義は
  [examples/strategy_squeeze_breakout_v1.yaml](examples/strategy_squeeze_breakout_v1.yaml)。
