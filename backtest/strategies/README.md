# Strategy 定義ディレクトリ(Issue #53)

`python -m backtest run --strategy <ID>` が走査する **外部 Strategy 定義(YAML)の既定配置**。
環境変数 `STRATEGY_DEFINITIONS_DIR` で上書きできる。

- 1 ファイル = 1 Strategy(`*.yaml` / `*.yml`。このREADME等、他の拡張子は走査対象外)
- スキーマ・エラー方針は [docs/architecture/strategy_definition.md](../../docs/architecture/strategy_definition.md)
- 参照用サンプルは [docs/architecture/examples/strategy_squeeze_breakout_v1.yaml](../../docs/architecture/examples/strategy_squeeze_breakout_v1.yaml)
- ロードは all-or-nothing:1 ファイルでも invalid / id 重複があると `--strategy` 実行全体が
  実行開始前エラーになる(`--strategy-file` / `--strategy` を使わない実行には一切影響しない)

既存 Strategy(`squeeze_breakout`)の定義ファイル配置(外部定義への移行)は後続 Issue で行うため、
現時点でこのディレクトリに定義ファイルは無い。
