# Paper Signal

v1.4系 は、[Market Bias](market_bias.md) と Chart Snapshot(`market-watch/chart_snapshot.py`)をもとに、**paper_only のシグナル仮説**を生成・保存・Discord通知する `paper-signal/` を追加したリリースラインである。

## 目的

- Market Bias と Chart Snapshot(1h)から、ルールベースでシグナル候補を作る
- 生成したシグナルを SQLite に記録し、Discord へ通知できるようにする
- **実発注・自動売買は一切行わない。** 生成物はあくまで「仮説の記録」である

## 位置づけ

```text
Market Snapshot(可視化)─ Market Bias(市場状態の分類)
                                         │
                                         ├─ Chart Snapshot(1h, market-watch/chart_snapshot.py)
                                         │
                                         └─ Paper Signal Generator(paper-signal/, 本章)
                                                  │
                                                  ├─ Paper Execution(次フェーズ・未実装)
                                                  └─ Live Execution(将来・未実装)
```

Paper Signal は、Market Bias が示す「市場の大まかな方向」と、Chart Snapshot が示す「チャート状態(1h)」を組み合わせて、シグナル仮説を記録するだけの層である。Paper Signal 自身は entry / exit を一切決めない。

- **Paper Execution**: Paper Signal を入力として、時間進行に沿って仮想約定・仮想ポジション管理・Exit・損益計算を行う将来レイヤー。v1.4 では**未実装**。
- **Live Execution**: Broker API を利用した実注文・実約定・実ポジション管理の将来レイヤー。**未実装**。

Paper Execution も Live Execution も Paper Signal の外側の別レイヤーであり、いずれも未実装。本章(Paper Signal)では扱わない。

なお、生成済みの Paper Signal を過去のローソク足で遡及検証する **Backtest** は、この執行フローとは独立した検証基盤(Validation)である。Backtest 内部の Exit 判定(`paper-signal/exit_policy.py`)や仮想約定モデル(`backtest/execution_model.py` の spread / slippage / fill)は検証用のシミュレーションであって、Paper Execution レイヤーの実装ではない(詳細は [Backtest](../backtest.md))。

## 入力データ

`paper-signal/signal_generator.py` は、既存Providerが取得済みのデータのみを使う。自前でAPIを呼び出すのは `news-feed-bot/market_snapshot.fetch_snapshot_sources()` 経由のみである。

| 入力 | 由来 | 説明 |
|---|---|---|
| `market_bias` / `confidence` | `news-feed-bot/market_bias.classify_market_bias()` | `Bull` / `Bear` / `Neutral` と 0.0〜1.0 の confidence |
| Chart Snapshot(1h) | `fetch_snapshot_sources()["chart"]`(内部で `market-watch/chart_snapshot.py` を呼ぶ) | symbol / timeframe / trend / state / market_state / freshness / bias_weight / macd_state / rsi14 / rsi_state |

Paper Signal v1.4系では、`rsi14` / `rsi_state` は記録・観測用の補助フィールドであり、候補生成ルールには使わない。

## 出力データ(Paper Signal)

`paper-signal/signal_models.py` の `create_paper_signal()` が返す。

```python
{
    "signal_id": "b3f1...",
    "created_at_utc": "2026-07-05T12:00:00Z",
    "symbol": "USDJPY",
    "timeframe": "1h",
    "side": "long",
    "market_bias": "Bull",
    "confidence": 0.72,
    "macd_state": "Bullish",
    "trend": "Bull",
    "state": "Inside band",
    "freshness": "Fresh",
    "reason": "Market Bias=Bull (72%); USDJPY 1h MACD=Bullish, Trend=Bull, State=Inside band, Freshness=Fresh.",
    "signal_type": "trend_follow",
    "trigger_type": "macd_state_alignment",
    "alert_source": "composite",
    "strategy_profile": "market_bias_macd_alignment_v1",
    "status": "paper_only",
}
```

`status` は常に `"paper_only"` であり、他の値を取ることはない。

### 分類フィールド(Issue #27)

Signalの戦略的分類(Strategy / Method層)と、生成契機・入力元(Alert層)を区別して追跡するための4フィールド。いずれも記録用であり、entry / exit / サイズ / 発注等の判断には一切用いない。定数は `paper-signal/signal_models.py` に一元定義する(`CLASSIFICATION_FIELD_DEFAULTS`)。

| フィールド | 現行値 | 意味 |
| --- | --- | --- |
| `signal_type` | `trend_follow` | Signalの戦略的分類(トレンドフォロー) |
| `trigger_type` | `macd_state_alignment` | 生成契機。Market BiasとMACD**状態**の整合で候補化していることを表す |
| `alert_source` | `composite` | 入力元。単一イベント検知ではなく複数入力の合成であること |
| `strategy_profile` | `market_bias_macd_alignment_v1` | 適用ロジックのプロファイル名 |

現行ロジックは**MACDクロスイベントを直接検知していない**。Market Biasと現在のMACD状態の整合によって候補を生成しているため、`trigger_type` は `macd_cross` ではなく `macd_state_alignment` とする(クロス検知を入力としていないため、`macd_cross` と記録すると事実と異なる)。

古い形式のcandidate(4フィールドを持たない)を `create_paper_signal()` に渡した場合も、`.get()` により上表の既定値で補完されるため後方互換を保つ。`paper_signals` テーブルはこの4列をTEXTで持ち、既存DBには `init_db()` の冪等マイグレーション(`PRAGMA table_info` で不足列だけ `ALTER TABLE ... ADD COLUMN`、`NOT NULL DEFAULT` で既存行を補完)で追加する。

## シグナル生成ルール

`paper-signal/signal_generator.build_candidates()` が判定する。

1. `market_bias` が `Bull` または `Bear` のときだけ候補にする(`Neutral` では生成しない)
2. `confidence` が `PAPER_SIGNAL_CONFIDENCE_THRESHOLD`(既定 `0.40`)以上のときだけ候補にする
3. Chart Snapshot の `1h` を代表時間足として使う(他の時間足は見ない)
4. Bull の場合: `macd_state == "Bullish"` かつ `freshness == "Fresh"` かつ `bias_weight > 0` → `side = "long"`
5. Bear の場合: `macd_state == "Bearish"` かつ `freshness == "Fresh"` かつ `bias_weight > 0` → `side = "short"`
6. `freshness` が `"Weekend Closed"` / `"Market Closed"` / `"Stale"` のいずれかの場合はシグナルを生成しない(`"Fresh"` のみ候補にする)

### 銘柄別Market Biasの適用(Issue #40)

以前は1つの `GLOBAL` Market Bias を全 snapshot に一律適用していたため、`USDJPY` の Bull Bias が `XAUUSD` / `SPX` へも無条件に適用されていた。Issue #40 では **各 snapshot に、その `symbol` / `timeframe` に対応する Bias** を使う。

- `run_once()` が `build_bias_by_scope()` で銘柄別Bias(`(正規化symbol, 1h)` ごと)+ `GLOBAL` フォールバックのマップ `bias_by_scope` を作り、`SignalContext` に載せる。
- 各 snapshot について `signal_generator.resolve_scope_bias()` が [Market Bias の参照優先順位](market_bias.md#参照優先順位point-in-time)(symbol+timeframe → symbol+1h → GLOBAL+timeframe → GLOBAL+1h)で有効Biasを解決する。銘柄別があればそれ、なければ `GLOBAL`。
- 銘柄別Biasは `GLOBAL` と同じ `classify_market_bias()` を、その銘柄のchart_dataだけに絞って呼ぶ(マクロ/Risk/Volatilityは共有、他銘柄のchart trend / MACDは混入させない)。`GLOBAL` のみの既存環境でも従来どおり候補生成できる。
- `build_candidates()` / `generate_signal_candidates()` は**pure関数のまま**(SQLite / HTTP / Discord へアクセスしない)。BiasのDB lookupやin-memory計算は `run_once()` 側で完結し、pure関数には解決済みBiasを渡す。Backtestは1バーごとに解決済みの単一Biasを渡すため、pure・DB非依存のまま変更なし(既存の単一 `bias_result` を受け取るAPIも後方互換で残す)。

## 常駐サービス化(Issue #41)

Paper Signal Generator は、既存入力(Market Bias / Chart Snapshot)を使って定期的にPaper Signalを生成・保存・通知する常駐サービスである。実行エントリーポイントは `paper-signal/paper_signal_bot.py`(薄いエントリーポイント)で、ロジック本体は `signal_generator.py`。戦略判定(`build_candidates`)・Market Bias分類・Chart Snapshot生成のロジックは一切変更せず、以下の運用規律を追加している。

- **確定足のみを使用**: `signal_generator.run_once()` は周期開始時刻(`evaluation_time`)を1つ固定し、symbol / timeframe ごとに対象となる **確定足(closed candle)の open timestamp** を Chart Snapshot 自身が持つ candle timestamp から明示的に決定する。共有ルール `market-watch/freshness.py`(`candle_close_time` / `select_confirmed_candles`, Issue #28)を再利用し、`T + timeframe <= evaluation_time` を満たさない未確定足は使わない。timeframeが未知で確定時刻を計算できない場合は **fail closed** として未確定扱いで skip する。「現在時刻に最も近いレコードを無条件に使う」ことはしない。
- **入力整合(provenance照合)**: Market Bias の provenance(その Bias が実際に分類に使った入力の `symbol` / `timeframe` / target candle / source snapshot id)を **Bias生成時点で固定して記録**する(`build_bias_provenance_by_scope()`。各銘柄の Bias は `build_bias_by_scope()` と同じく自銘柄の snapshot だけから分類され、その入力の candle を provenance として持つ)。`build_confirmed_candidate()` はこの provenance と Chart Snapshot 側の実 timestamp / source id を明示的に比較する。**Chart 側の timestamp を Bias へ後付けコピーして一致させることはしない**ため、symbol / timeframe / target candle / source dataset のいずれかが食い違えば skip となり(欠損も skip)、不一致の入力同士から Signal は生成されない。
- **処理順**: Signal候補生成 → SQLite保存 → 保存成功後にDiscord通知(`_process_candidate()`)。DB保存に失敗した場合は Discord を呼ばずにログして次symbolへ継続。Discord通知が失敗しても保存済みSignalは削除・ロールバックせず、失敗はログに記録して次周期・他symbolの処理を継続する。
- **dry-run(永続副作用なし)**: `--dry-run` では (1) Paper Signal の SQLite保存、(2) Discord送信、(3) chart SQLite の作成・更新、(4) logfile / `logs/` の作成 のいずれも行わない。Chart Snapshot は `fetch_snapshot_sources(persist=False)` 経由の **メモリ上のみの非永続経路**(`chart_snapshot.build_snapshots_in_memory()`)で生成し、ログは **console-only logger**(ファイルハンドラを付けない)を使う。使用予定の入力・target candle・生成結果・no-signal・skip理由・重複判定はログで確認できる。
- **CLI/構造**: 常駐ループ(`run_service()`)と1周期(`run_once()`)を分離。`--once` は sleep せず1周期だけ実行する。
- **起動/終了**: SIGTERM / SIGINT で現在の周期を終えて安全に停止する(`run_service` は sleep ではなく停止イベントを待つので即座に中断される)。systemd unit は `scripts/systemd/berial-paper-signal.service`(コミット済みテンプレート、`KillSignal=SIGTERM` / `TimeoutStopSec=30`)。`scripts/restart_all.sh`(POSIX sh)の再起動対象に `berial-paper-signal.service` を追加済みで、この unit が未配備でも他 unit の restart は止めず失敗を非0で報告する。配備手順は [operations.md](../operations/operations.md#paper-signal-systemd常駐サービス-issue-41)。

## 重複防止

Issue #41 以降、重複防止は時間ウィンドウではなく **確定足単位の冪等性** で行う。`paper_signals` テーブルに `target_candle_utc` 列を追加し、`(strategy_profile, symbol, timeframe, target_candle_utc)` に SQLite の UNIQUE インデックスを張る。`paper_signal_store.save_signal_for_candle()` は `INSERT ... ON CONFLICT DO NOTHING` で挿入し、新規挿入時のみ `True` を返す。呼び出し側は **新規挿入時だけDiscord通知** するため、多重起動・競合・再周期でも同一 target candle のSignalは重複保存も重複通知もされない。

既存DB(Issue #27時点)には `init_db()` の冪等マイグレーションで `target_candle_utc` 列(nullable)を追加する。旧データは `target_candle_utc = NULL` のままだが、SQLite は UNIQUE インデックス上の NULL を相互に distinct として扱うため、旧行が新制約で拒否されることはない。

**通知状態の管理(仕様判断)**: Paper Signal は通知成否を専用列で持たない。上記の確定足冪等性により「一度保存済みの target candle は次周期以降 duplicate として通知前に skip される」ため、Discord通知は各Signalにつき生成時に一度だけ試行され、失敗しても自動再送しない。これが「無制限再送を避ける最小方針」である。

なお `has_recent_signal()`(旧・時間ウィンドウ判定)と `PAPER_SIGNAL_DUPLICATE_WINDOW_MINUTES` は後方互換のため残しているが、常駐サービスの重複防止経路では使用しない。

### 時刻の扱い

DB保存時刻(`created_at_utc`)と target candle timestamp(`target_candle_utc`)はいずれも UTC の ISO-8601(`...Z`)で保存する。Discord表示では target candle を `<UTC-Z> (<JST> JST)` の形で併記し、JSTは常にUTC値から導出する表示専用値とする(UTCとJSTを暗黙に混在させない)。

## ストレージ

SQLiteに保存する。パスは `paper-signal/data/paper_signals.sqlite3`(`paper-signal/paper_signal_store.py`)。テーブルは上記「出力データ(Paper Signal)」の全フィールドを1行1シグナルとして保持する。`paper-signal/data/` はGit管理しない(`.gitignore`)。

Market Bias自体は `news-feed-bot/data/market_bias.sqlite3` に別途保存される(Issue #17、詳細は [Market Bias](market_bias.md#永続化とbacktest参照issue-17))。Paper Signalは現状 `classify_market_bias()` をその場で呼び出しており、このDBを読み取らない。`market_bias_store.get_market_bias_as_of()` はDB不在・レコード不在時に例外を投げず `None` を返す設計のため、将来 Paper Signal や Backtest がこのAPIを使うように変わっても、Market Biasが存在しない期間で異常終了しない。

## Discord通知

本文の末尾に必ず固定文字列 `"Paper signal only. No trade execution."` を含める(`format_discord_message()`)。この制約は `tests/test_signal_generator.py` で検証する。

## 売買判断・自動発注は行わないこと(制約)

Paper Signal Generator は、以下を**実装しない**。

- 自動売買・自動発注
- 発注API接続(OANDA / XM / MT5 などbroker APIは使わない)
- ロット計算・レバレッジ計算
- 利確・損切り(stop-loss / take-profit)判断
- エントリー実行

これらの entry / exit / 発注は Paper Signal の外側の執行層(**Paper Execution** / **Live Execution**)の責務であり、いずれも未実装である。Paper Signal Generator 自身は entry / exit をいっさい決めない。なお TP / SL の評価は Backtest(検証基盤)が過去データ上で遡及的に行うが、これは検証用シミュレーションであって Paper Execution レイヤーの実装ではない。

`paper-signal/signal_generator.py` のソースコードは、broker APIのimportや発注関数呼び出しが存在しないことをテスト(`test_module_source_has_no_broker_api_integration`)で検証する。生成されるレコードの各フィールド値にも、order / lot / leverage / stop-loss / take-profit 等の語が含まれないことを検証する。
