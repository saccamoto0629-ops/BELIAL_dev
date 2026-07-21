# BELIAL テスト仕様書

Issue #34。本書は品質の百科事典ではなく、**Claude Code・Codex・pytest・CI(#32)が同じ地図を見るための作戦盤**である。全単体テストを転記せず、重要な品質特性・代表テストケース・既存pytestとの対応のみを記載する。

- 記載内容はコードまたは既存ドキュメントから確認できる事実に基づく。推測で仕様を補わない。
- 個々のテストの網羅は各 `*/tests/*.py` が持つ。本書はその「観点」と「代表例」を固定する。
- コードと矛盾する点・未確認の点は [§9 矛盾・不明点](#9-矛盾不明点) に明示する。

関連: [#32 Add minimal GitHub Actions CI](https://github.com/saccamoto0629-ops/BELIAL_dev/issues/32)(CI実装は#32の範囲。本書はそこで何を継続実行すべきかの判断材料を提供する)。

---

## 1. テスト対象・目的・範囲・非対象

### 対象
BELIAL の観測〜分類〜Alert〜仮説(Paper Signal)〜検証(Backtest)〜仮想執行(Paper Execution)の各レイヤーと、その副作用境界(DB / Discord / S3 / state ファイル)。

### 目的
- レイヤー責務ごとの**壊れてはいけない性質**を明文化する。
- Backtest の**時系列整合性・境界値**を回帰として固定する。
- `--dry-run` / `dry_run=True` の**無副作用**を保証する。
- 重要ケースと既存pytestの**対応関係**を追跡可能にする。

### 範囲
- `backtest/` / `market-watch/` / `news-feed-bot/` / `paper-signal/` / `paper-execution/` の単体・軽量結合テスト。
- 副作用抑止(DB作成/更新・Discord送信・S3アップロード・presigned URL・state更新)の検証観点。

### 非対象(Non-Scope)
- 全単体テストの人間向け転記、手動試験の証跡保存。
- CI workflow の実装(#32)。
- integration test / heavy backtest の全面整備。
- 自動売買・Broker API・Live Execution(現行スコープ外・未実装。仮想執行の Paper Execution は対象に含む)。

---

## 2. テスト実行方法

ルートに `pytest.ini` / `conftest.py` / `pyproject.toml` は**存在しない**。各コンポーネントのテストは**ディレクトリ単位で個別に**実行する(各テストファイルが冒頭で自分の `sys.path` を設定する)。

```bash
python -m pytest backtest/tests -q
python -m pytest market-watch/tests -q
python -m pytest news-feed-bot/tests -q
python -m pytest paper-signal/tests -q
python -m pytest paper-execution/tests -q
python -m pytest broker/tests -q
python -m pytest execution/tests -q
```

CI(#32)では最低限この7スイートを継続実行することを想定する(現行の `.github/workflows/ci.yml` は7スイートすべてを実行している)。テストは外部ネットワーク・実DB・実S3・実Discordに到達しない(§4–§5)。

現状のテスト件数(`def test_` 実数、目安):backtest 128 / market-watch 127 / news-feed-bot 117 / paper-signal 89 / paper-execution 91 / broker 97(Issue #73 / PR #87)/ execution 52(Issue #74)。

---

## 3. レイヤー別テスト観点

| Layer | モジュール(代表) | 主なテスト観点 | 代表テストファイル |
|---|---|---|---|
| Observation / data collection | `market-watch/chart_collector.py`, `news-feed-bot/market_data.py` ほか | 取得結果の正規化、SQLite保存/期間読み込み、取得失敗時の挙動 | `test_chart_collector.py`, `test_market_data.py` |
| Indicator / classification | `market-watch/indicators.py`, `news-feed-bot/market_bias.py` | 指標計算の正しさ、warm-up未達はNaN、trend/risk/volatility分類 | `test_indicators.py`, `test_market_bias.py` |
| Alert | `market-watch/{macd,ma,rsi,bollinger}_alert_bot.py` | 検知条件、重複抑止、dry-run無副作用、webhook必須検証 | `test_*_alert_bot.py` |
| Paper Signal / hypothesis | `paper-signal/signal_generator.py`, `signal_models.py` | Bull/Bear/Neutral別候補生成、confidence閾値、`status=paper_only` 固定、重複窓 | `test_signal_generator.py`, `test_paper_signal_store.py` |
| Exit / Invalidation | `paper-signal/exit_policy.py` | fixed_rr / time_based_close / ma_break、同足優先、open(no_close)、空/None candles | `test_exit_policy.py` |
| Backtest | `backtest/{runner,warmup,summary,report,backtest_store,backtest_models}.py` | 境界分離・future leak禁止・skip・DB保存・集計 | `test_warmup.py`, `test_runner.py`, `test_backtest_engine.py` ほか |
| Paper Execution | `paper-execution/{execution_engine,execution_runner,position_store,position_models,execution_profile,paper_execution_notifier}.py` | Entry / Close判定の決定論、`state × terminal_reason` 閉集合、`signal_id` 冪等、schema migration(positions table rebuild / v1→v3 additive / v2 prototype→v3 outbox rebuild / 実制約検証 + behavioral probe / migration rollback)、strict read、dry-run無副作用(WAL/rollback-journal DB のファイル完全不変を hash/mtime で検証)、Discord通知 **Transactional Outbox**(遷移+enqueue の atomicity、enqueue失敗時の position rollback、commit後・HTTP前クラッシュからの再送、claim/lease による並行runner排他=HTTP 1回、lease期限切れ回収、at-least-once=success記録失敗時の同一Event ID重複、本文生成・None/Unicode、timeout/429/5xx/connection error 分類、dead-letter継続検知、Webhook未設定滞留、batch limit、outbox永続化failureは`failures`・配送failureは`notification_failures`) | `paper-execution/tests/test_*.py` |
| Execution Request / 注文状態永続化(将来のLive Execution用。Issue #74) | `execution/{__init__,execution_models,execution_store}.py` | `ExecutionRequest` 構築不変条件(Decimal・UTC・必須ID非空・quantity/price正の有限値・MARKET/LIMITとrequested_priceの整合・SL/TPとdirectionの一意に保証できる関係のみ・naive datetime拒否)、`to_order_request()` 変換(SL/TP・provenanceがBroker境界を越えない)、SQLite永続化(保存・復元・`client_order_id` 1:1一意・重複conflict)、送信フェーズ(created→submitting一度きり・submittedは確定観測でのみ導出)、既存 `validate_order_state_transition`/`validate_order_result_update` による遷移・内容検証(terminal離脱拒否・UNKNOWN許可解決先のみ)、broker_order_id差し替え拒否、updated_at逆行拒否、same-state再観測の履歴記録、current更新+履歴追加の原子性(失敗時rollback)、UNKNOWN保存・照会対象抽出・自動変換なし、DB再オープン後の完全復元、`init_db` 冪等性・不一致schemaの明示的失敗、missing DBを作成しないread/update、ルートからのpackage import(subprocess)、Paper/Observationレイヤーがexecutionをimportしないこと(AST走査)。`tmp_path` のSQLiteのみ使用 | `execution/tests/test_*.py` |
| 副作用境界 | 各store / uploader | 一時DB利用、dry-run抑止、run/signalの分離DB、FK/重複ID | `test_*_store.py`, `test_chart_alert_context.py` |
| Broker境界(将来のLive Execution用。Issue #73 / PR #87) | `broker/{__init__,broker_models,broker_interface,fake_broker}.py` | 共通モデル不変条件(Decimal・UTC・数量整合・フィールド型不正は`BrokerInvalidRequestError`/整合性違反は`ValueError`)、注文状態遷移の閉集合(timeout後の`unknown`は`rejected`と別状態。`unknown`は既知fill保持可。同一状態更新は全状態で許可)、スナップショット更新不変条件(`validate_order_result_update`: 識別/要求フィールド不変・fill単調性・`fill_id`一意性(snapshot内・追加fill)・fills prefix保持・`broker_order_id`はNone→値のみ・`updated_at_utc`単調非減少・terminal same-state再観測は`updated_at_utc`以外全フィールド不変・UNKNOWN解決先の二重検証)、構築時クロスフィールド検証(LIMITは`requested_price`必須/MARKETはNone必須/`pending`は`broker_order_id`禁止)、型付きBrokerエラー分類、FakeBrokerのScripted Scenario(即時/未/部分約定・拒否・timeout・通信失敗・キャンセル成功/失敗・約定価格乖離・各照会)とScenario/設定APIの構築時検証、外部通信ゼロ(AST import走査+socket無効化)、Paper Signal / Paper Execution / Backtest(Strategy関連)がBroker層をimportしないこと(rglob+AST)、リポジトリルートからのpackage import(subprocess検証)。インメモリのみ(tmp_path不要) | `broker/tests/test_*.py` |

補足:
- Market Bias は**時点参照(point-in-time)**が肝。`get_market_bias_as_of()` は評価時刻より**後**のレコードを返してはならない(§6 SE系)。
- Alert層は「事実検知のみ・売買判断をしない」責務であり、テストも通知条件と副作用抑止に限定される。

---

## 4. バックテスト重要観点(時系列整合性・境界値)

`backtest/warmup.py::split_warmup_and_validation()` は `validation_end` 指定時に3区間を明確に分離する。

- **warm-up**: `index < validation_start`(indicator計算のseed専用。結果集計に出現しない)
- **validation**: `validation_start <= index <= validation_end`(結果集計対象)
- **future(未来足)**: `index > validation_end`(warm-up にも validation にも**含めない**)

固定すべき性質:

| 観点 | 期待 | 対応pytest |
|---|---|---|
| warm-up / validation境界 | validation_start を境に厳密分離、warm-up足は結果に出ない | `test_warmup.py::test_split_warmup_and_validation_splits_at_the_given_start`, `::test_sufficient_warmup_only_validation_bars_appear_in_results` |
| validation_end境界 | end を含む(inclusive)。end超の足は両区間に入らない | `test_warmup.py::test_split_warmup_and_validation_respects_validation_end`, `::test_split_warmup_and_validation_excludes_future_rows_from_both_frames` |
| future leak禁止(warm-up水増し) | end超の足数が warm-up 本数に加算されない → 本来のwarm-up不足を隠さない | `test_warmup.py::test_split_warmup_and_validation_future_rows_do_not_inflate_warmup_frame_size`, `::test_evaluate_backtest_bars_future_rows_do_not_hide_warmup_insufficient`, `test_runner.py::test_run_backtest_excludes_candles_after_to` |
| future leak禁止(同足Exit) | signal発生足自身の high/low で自分のExitを判定しない(次足以降のみ) | `test_runner.py::test_signal_own_bar_high_low_never_decides_its_own_exit` |
| future leak禁止(Market Bias) | 評価時刻より後のbiasを参照しない | `test_runner.py::test_run_backtest_never_leaks_a_future_market_bias_record` |
| データ不足時のskip | warm-up不足はエラーではなく期待されたskip(全validation足がskip、signal 0件、exit 0) | `test_warmup.py::test_warmup_insufficient_skips_signal_generation_for_every_validation_bar`, `test_runner.py::test_run_backtest_flags_insufficient_warmup_and_generates_no_signals` |
| 空データ / 期間該当なし | candlesなし・validation該当足なしは exit code 3(EXIT_NO_DATA) | `test_runner.py::test_run_backtest_returns_no_data_when_symbol_has_no_candles`, `::test_run_backtest_returns_no_data_when_validation_window_has_no_candles` |
| 不正期間 / 不正引数 | `--to < --from`・不正 `--warmup-bars`/`--confidence-threshold`/`--exit-policy` は exit code 2(EXIT_USAGE_ERROR) | `test_runner.py::test_run_backtest_returns_usage_error_when_to_before_from`, `::test_parse_args_rejects_non_positive_warmup_bars`, `::test_parse_args_rejects_out_of_range_confidence_threshold` |
| 欠損指標 | RSI/MACD/BB/MA いずれか未準備の足はreason付きでskip | `test_warmup.py::test_readiness_*` |
| Signal 0件は正常系 | biasがNeutral / confidence閾値未満でも例外を出さず完走 | `test_backtest_engine.py::test_bull_bias_below_the_confidence_threshold_generates_no_signals`, `test_warmup.py::test_neutral_bias_produces_no_candidates_even_when_fully_ready` |

エンジン主要経路(手書きOHLCV fixture)は `test_backtest_engine.py` が固定:Signalあり / Signalなし / Exit到達(win・loss) / Invalidation到達。

---

## 5. dry-run 無副作用

方針は [ルートREADME](../../README.md#dry-run-方針) / [Operations](../operations/operations.md) に準拠。`--dry-run`(または `dry_run=True`)は**以下を一切発生させない**確認モード:

1. Discord送信
2. state ファイル更新(例: posted_ids、touch zone state)
3. DB作成・更新
4. S3アップロード / presigned URL生成

新機能追加時も、これらが発生しないことをテストで確認する(既存方針の維持)。

| 観点 | 期待 | 対応pytest |
|---|---|---|
| Discord送信なし | dry-runでHTTP/セッションを呼ばない | `market-watch/tests/test_macd_alert_bot.py::test_dry_run_does_not_call_discord_http`, `news-feed-bot/tests/test_news_feed_bot.py::test_dry_run_prints_without_sending_to_discord` |
| DB作成・更新なし | dry-runでSQLiteを作らない/更新しない | `paper-signal/tests/test_signal_generator.py::test_run_once_dry_run_does_not_create_db_or_call_discord_http`, `market-watch/tests/test_chart_snapshot.py::test_main_dry_run_skip_fetch_does_not_create_missing_sqlite` |
| state更新なし | dry-runでstateファイルを書き換えない | `market-watch/tests/test_{ma,rsi,bollinger}_alert_bot.py::test_run_once_dry_run_does_not_save_state`, `news-feed-bot/tests/test_news_feed_bot.py::test_dry_run_does_not_update_posted_ids_json` |
| S3/presigned生成なし | dry-runでupload/presignedを呼ばない、snapshot_id/URLはNone | `market-watch/tests/test_chart_alert_context.py::test_prepare_alert_image_dry_run_has_no_side_effects` |
| Market Bias永続化なし | dry-runでbiasを保存しない | `news-feed-bot/tests/test_market_snapshot.py::test_persist_market_bias_is_a_noop_in_dry_run` |

---

## 6. DB・Discord・S3 の副作用観点

| 観点 | 期待 | 対応pytest |
|---|---|---|
| 一時DBのみ使用 | テストは `tmp_path` を使い、実 `data/*.sqlite3` を作らない/触らない | Backtest系全般(`test_backtest_store.py`, `test_runner.py`, `test_backtest_engine.py`) |
| run/signal DB分離 | Backtest DB は paper_signals とは別ファイル | `test_backtest_store.py::test_backtest_db_is_a_separate_file_from_paper_signals` |
| 冪等な初期化 | `init_db()` 反復呼び出しが安全、不完全schemaは明確なエラー | `test_backtest_store.py::test_init_db_is_idempotent`, `::test_init_db_raises_clear_error_for_incomplete_existing_schema` |
| 重複ID防止 | run/signalの重複idは IntegrityError、上書きしない | `test_backtest_store.py::test_save_backtest_run_with_duplicate_id_raises_integrity_error` |
| FK整合 | 未知run_idのsignal保存を拒否 | `test_backtest_store.py::test_foreign_key_constraint_rejects_signal_for_unknown_run` |
| 読み取り時の非作成 | 存在しないDBの読み取りは None/[] を返しファイルを作らない | `test_backtest_store.py::test_get_backtest_run_returns_none_when_db_file_missing` |
| Market Bias時点参照 | 評価時刻以前の直近のみ返す(未来リーク禁止・DB無ければNone) | `news-feed-bot/tests/test_market_bias_store.py::test_get_market_bias_as_of_never_leaks_a_future_record`, `::test_get_market_bias_as_of_returns_none_when_db_file_missing` |
| Paper Signal重複窓 | 同一symbol/timeframe/side の一定時間内重複を抑止 | `paper-signal/tests/test_paper_signal_store.py::test_has_recent_signal_true_within_window` |
| Discordは注入セッション | 実webhook無しでも注入セッションで検証、webhook未設定はスキップ/検証 | `news-feed-bot/tests/test_news_feed_bot.py::test_non_dry_run_uses_injected_session_without_real_webhook`, `market-watch/tests/test_macd_alert_bot.py::test_validate_config_requires_webhook_unless_dry_run` |
| S3 private key運用 | 直リンクを持たずprivate keyでupload→presignedする | `market-watch/tests/test_chart_renderer_storage.py::test_upload_and_presign_use_private_s3_key_without_direct_url` |
| Backtestは非通知 | Backtest Runnerは発注/Discord/外部APIを呼ばない | `backtest/tests/test_runner.py::test_run_backtest_never_places_an_order_or_calls_discord`, `test_backtest_engine.py`(autouseネットワーク遮断) |

---

## 7. 重要テストケース一覧

ID = 分類プレフィックス + 連番。**代表ケースのみ**(全網羅ではない)。「対応pytest」は既存テスト、空欄は§8参照。

分類: `BT`=Backtest時系列/境界、`EX`=Exit/Invalidation、`DR`=dry-run無副作用、`SE`=副作用/DB/通知、`CLI`=CLI引数/終了コード、`SUM`=Summary集計。

| ID | 前提条件 | 入力 / 操作 | 期待結果 | 対応pytest |
|---|---|---|---|---|
| BT-01 | 十分なwarm-up + validation足 | `split_warmup_and_validation(start)` | warm-up足は結果に出ず、validation足のみ集計 | `test_warmup.py::test_sufficient_warmup_only_validation_bars_appear_in_results` |
| BT-02 | validation_end指定、end超の足あり | `split_warmup_and_validation(start,end)` | end超足は warm-up/validation どちらにも入らない | `test_warmup.py::test_split_warmup_and_validation_excludes_future_rows_from_both_frames` |
| BT-03 | warm-up不足 + 大量のend超足 | `evaluate_backtest_bars(...)` | 未来足がwarm-up不足を隠さず、全validation足がskip | `test_warmup.py::test_evaluate_backtest_bars_future_rows_do_not_hide_warmup_insufficient` |
| BT-04 | warm-up本数 < 要求本数 | `run_backtest(...)` | エラーでなくskip、signal 0件、exit code 0 | `test_runner.py::test_run_backtest_flags_insufficient_warmup_and_generates_no_signals` |
| BT-05 | validation期間にcandleあり、end超candleも保存済 | `run_backtest(--to=end)` | end超candleを読み込み対象から除外 | `test_runner.py::test_run_backtest_excludes_candles_after_to` |
| BT-06 | signal発生足のみ(次足なし) | `run_backtest(...)` | 自足high/lowで自分をExit判定しない → open/no_close | `test_runner.py::test_signal_own_bar_high_low_never_decides_its_own_exit` |
| BT-07 | 評価時刻より後にのみbiasレコード | `run_backtest(...)` | 未来biasを参照しない(Neutralフォールバック) | `test_runner.py::test_run_backtest_never_leaks_a_future_market_bias_record` |
| EX-01 | long signal、次足highがTP到達 | fixed_rr で評価 | `win` / `take_profit`、pnl>0、closed_at=次足 | `test_backtest_engine.py::test_exit_reached_at_the_take_profit_records_a_win` |
| EX-02 | long signal、次足lowがstop到達 | fixed_rr で評価 | `loss` / `stop_loss`、pnl<0 | `test_backtest_engine.py::test_exit_reached_at_the_stop_records_a_loss` |
| EX-03 | long signal、MAを下抜けるclose | ma_break_invalidation で評価 | `invalidated` / `ma_break` | `test_backtest_engine.py::test_invalidation_reached_when_price_closes_below_the_ma` |
| EX-04 | candlesが空/None | `evaluate_exit(...)` | 例外を出さず open / no_close | `paper-signal/tests/test_exit_policy.py::test_empty_candles_returns_open_no_close_without_raising` |
| EX-05 | TPとstopが同一足で両方到達 | `same_candle_priority` 設定 | 設定通りの優先(stop_loss / take_profit)で決着 | `test_exit_policy.py::test_same_candle_priority_stop_loss_wins_long` ほか |
| CLI-01 | — | 必須引数欠落で `run` | usageエラー(exit code 2) | `test_runner.py::test_parse_args_requires_symbol_timeframe_from_to` |
| CLI-02 | — | `--to < --from` | exit code 2 | `test_runner.py::test_run_backtest_returns_usage_error_when_to_before_from` |
| CLI-03 | candle DBに該当symbolなし | `run` | exit code 3(EXIT_NO_DATA) | `test_runner.py::test_run_backtest_returns_no_data_when_symbol_has_no_candles` |
| CLI-04 | — | `--exit-policy` に無効/無効化済みポリシー | 明確なエラーで拒否(exit code 2) | `test_runner.py::test_run_backtest_rejects_a_valid_but_disabled_exit_policy_with_a_clean_usage_error` |
| SUM-01 | win/loss/pnl=0 混在のsignals | `build_summary(run_id, signals)` | win/loss/win_rate/total_pnl/profit_factor/max_drawdown が定義通り | `test_summary.py::test_normal_case_wins_losses_and_zero_pnl` |
| SUM-02 | signal 0件 | `build_summary(run_id, [])` | 例外なし、各指標が0/None、profit_factor/NaN/Infを出さない | `test_summary.py::test_zero_signals_matches_issue_23_example`, `::test_json_never_contains_nan_or_infinity_for_wins_only` |
| SUM-03 | 別run_idのsignalが混入 | `build_summary(run_id, signals)` | ValueErrorで拒否(誤集計しない) | `test_summary.py::test_build_summary_raises_value_error_when_a_different_run_id_is_mixed_in` |
| DR-01 | Alert Bot、dry-run | `run_once(dry_run=True)` | Discord HTTPを呼ばない | `test_macd_alert_bot.py::test_dry_run_does_not_call_discord_http` |
| DR-02 | Alert Bot、dry-run | `run_once(dry_run=True)` | stateファイルを更新しない | `test_ma_alert_bot.py::test_run_once_dry_run_does_not_save_state` |
| DR-03 | Chart Snapshot、dry-run + skip-fetch、DB無し | `main(--once --dry-run --skip-fetch)` | 欠損SQLiteを作らない | `test_chart_snapshot.py::test_main_dry_run_skip_fetch_does_not_create_missing_sqlite` |
| DR-04 | Alert画像、dry-run | `prepare_alert_image(dry_run=True)` | S3 upload/presignedを呼ばず、id/URLはNone、DB無し | `test_chart_alert_context.py::test_prepare_alert_image_dry_run_has_no_side_effects` |
| DR-05 | Paper Signal、dry-run | `run_once(dry_run=True)` | DB作成もDiscord HTTPもしない | `test_signal_generator.py::test_run_once_dry_run_does_not_create_db_or_call_discord_http` |
| SE-01 | 既存run_id | signal保存 | Backtest DBは paper_signals と別ファイル | `test_backtest_store.py::test_backtest_db_is_a_separate_file_from_paper_signals` |
| SE-02 | 未知run_id | signal保存 | FK違反で拒否 | `test_backtest_store.py::test_foreign_key_constraint_rejects_signal_for_unknown_run` |
| SE-03 | 評価時刻とその前後にbias | `get_market_bias_as_of(t)` | t以前の直近のみ、未来リークなし | `test_market_bias_store.py::test_get_market_bias_as_of_never_leaks_a_future_record` |
| SE-04 | 同一symbol/timeframe/sideの直近signal | `has_recent_signal(...)` | 窓内は重複と判定 | `test_paper_signal_store.py::test_has_recent_signal_true_within_window` |

---

## 8. 未実装 / 不足している重要テスト

現時点でコード上に**確認できなかった**、または**明示的なテストが無い**観点。CI(#32)導入前に別Issue化を推奨。

| # | 不足観点 | 現状 | 推奨対応 |
|---|---|---|---|
| G-1 | 4スイート横断の一括実行 | ルートに pytest 設定/`conftest.py` が無く、スイートは個別実行のみ | #32 で4スイート実行をCIに固定(またはルート設定追加を別Issue化) |
| G-2 | candleに時間ギャップ/欠損行がある場合のBacktest挙動 | 欠損足を含む入力の明示テストは未確認 | Backtestに時間ギャップfixtureの回帰を追加(別Issue候補) |
| G-3 | 重複timestamp / 非単調index candle | 明示テスト未確認 | 入力健全性チェックの有無を確認し、必要ならテスト追加 |
| G-4 | Backtest全体でのDiscord/S3非到達の集約保証 | `test_backtest_engine.py` はautouseでネットワーク遮断するが、他Backtestテストは個別 | 必要なら共通 `conftest.py` でネットワーク遮断を全Backtestに拡張(別Issue) |
| G-5 | S3 presigned URLの失敗系(認証エラー等) | 正常系中心。失敗時の副作用抑止は未確認 | uploader失敗時にDB/stateを汚さないことのテスト検討 |

上記は本Issueでコード追加せず、観点として記録するに留める(Issue #34 の指示に従い、実装コードは必要以上に変更しない)。

---

## 9. 矛盾・不明点

- **docs配置ガイドラインとの差異**: [docs/README.md](../README.md) は「`docs/` 直下には原則としてこの `README.md` だけを置く」としており、`docs/backtest.md` は既にこの原則の例外になっている。本書は原則に沿って新カテゴリ `docs/testing/` に配置し、`docs/README.md` の Structure/Guidelines を更新した。
- **テスト件数**は `def test_` の実数カウントであり、parametrize による実行ケース数とは一致しない(実行ケースはこれより多い)。
- **G-2/G-3(欠損・非単調candle)**はコードを確認した範囲で明示的な入力健全性チェック・テストを発見できなかった。「テストが無い」ことは「保証されていない」を意味し、「壊れる」を意味しない。実挙動は未検証のため断定しない。
- 本書のテスト名・件数は作成時点のもの。テスト追加/リネーム時は本書の対応表も更新する(維持コストを抑えるため、代表ケースのみを追う)。
