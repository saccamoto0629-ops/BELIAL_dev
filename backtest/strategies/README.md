# Strategy 定義ディレクトリ(Issue #53 / #54)

`python -m backtest run --strategy <ID>` が走査する **外部 Strategy 定義(YAML)の既定配置**。
環境変数 `STRATEGY_DEFINITIONS_DIR` で上書きできる。

- 1 ファイル = 1 Strategy(`*.yaml` / `*.yml`。このREADME等、他の拡張子は走査対象外)
- スキーマ・エラー方針は [docs/architecture/strategy_definition.md](../../docs/architecture/strategy_definition.md)
- ロードは all-or-nothing:1 ファイルでも invalid / id 重複があると `--strategy` 実行全体が
  実行開始前エラーになる(`--strategy-file` / `--strategy` を使わない実行には一切影響しない)

## 配置済みの定義

| ファイル | id | 内容 |
|---|---|---|
| [xauusd_1h_squeeze_breakout_v1.yaml](xauusd_1h_squeeze_breakout_v1.yaml) | `xauusd_1h_squeeze_breakout_v1` | Issue #54: 既存 `squeeze_breakout` の外部定義。旧 default(`RISK_REWARD=2.0` / `TIME_EXIT_BARS=24`)と同値で、`--strategy-profile` の旧経路と結果が完全一致することをテストで固定(`backtest/tests/test_squeeze_breakout_migration.py`)。 |

実行例(リポジトリルートから):

```bash
python -m backtest run \
  --symbol XAU_USD --timeframe 1h \
  --from 2026-06-01T00:00:00 --to 2026-06-30T23:59:59 \
  --strategy xauusd_1h_squeeze_breakout_v1
```

## 新しい Strategy 定義を追加する基本手順

1. 既存の [xauusd_1h_squeeze_breakout_v1.yaml](xauusd_1h_squeeze_breakout_v1.yaml) をコピーし、
   `id`(`^[a-z0-9_]+$`、ディレクトリ内で一意)/ `name` / 可変設定
   (`risk.take_profit.r`、`exit.max_holding_bars` 等)を書き換える。
   スキーマの全項目・型・default・許容範囲は
   [docs/architecture/strategy_definition.md §2](../../docs/architecture/strategy_definition.md) を参照。
2. `entry.handler` は登録済み handler(現行は `squeeze_breakout` のみ)から選ぶ。
   handler ごとに許容される値(capability)が異なり、squeeze_breakout では
   `reentry: new_setup` / `cooldown_bars: 0` / `stop_loss.type: trigger_bar_extreme` /
   `same_candle_priority: stop_loss` / `allowed_market_states: null` 以外は validation error になる。
3. `--strategy-file <path>` で単体検証してから、このディレクトリへ配置して `--strategy <id>` で選択する
   (配置しただけでは既存実行に影響しない。外部定義は完全な opt-in)。
4. 条件ロジック(条件式・約定判定・状態遷移)は定義ファイルへ書けない。新しい挙動が必要な場合は
   Python 側で handler を実装・登録してから、その handler を選ぶ定義を書く。

定義ファイルへ入れられるのは**宣言的な設定値のみ**。Python コード・条件式文字列・eval 対象・
import 名は入れられない(Loader が構造的に拒否する)。
