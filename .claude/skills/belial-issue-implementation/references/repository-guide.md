# BELIAL Repository Guide(Skill 参照資料)

BELIAL Issue 実装時に参照するコンポーネント・ドキュメント・テストの対応表。**仕様の本文はここに書かない。** 各項目の正は右列の docs / README を参照する。

## コンポーネント対応表

| コンポーネント | コード | README | 設計書(docs/architecture/) | テスト | 所有 DB |
|---|---|---|---|---|---|
| news-feed-bot | `news-feed-bot/` | `news-feed-bot/README.md` | `market_snapshot.md`, `market_bias.md` | `news-feed-bot/tests/` | `market_bias.sqlite3` |
| market-watch | `market-watch/` | `market-watch/README.md` | `market_watch.md`, `market_snapshot.md` | `market-watch/tests/` | `chart_data.sqlite3` |
| paper-signal | `paper-signal/` | `paper-signal/README.md` | `paper_signal.md` | `paper-signal/tests/` | `paper_signals.sqlite3` |
| paper-execution | `paper-execution/` | (なし。設計書を参照) | `paper_execution.md` | `paper-execution/tests/` | `paper_positions.sqlite3` |
| backtest | `backtest/` | `backtest/strategies/README.md`(Strategy 定義) | `strategy_definition.md`、実行方法は `docs/backtest.md` | `backtest/tests/` | (backtest 結果 DB。`backtest_store.py` 参照) |
| broker | `broker/` | `broker/README.md` | `broker_interface.md` | `broker/tests/` | なし(インメモリのみ) |

全体像・DB 構成・データ連携は `docs/architecture/architecture.md`、責務レイヤーと非スコープは `docs/product/concept.md` が正。

## ドキュメント選定の目安

| 変更内容 | 必ず読む docs |
|---|---|
| どの変更でも | `docs/product/concept.md`(レイヤー分離・非スコープ)、`docs/architecture/architecture.md`(該当節) |
| 環境変数・起動・systemd・CLI | `docs/operations/operations.md` |
| DB スキーマ・store 層 | `docs/architecture/architecture.md` の「SQLite schema validation / migration」+ 該当コンポーネント設計書 |
| dry-run に関わる経路 | `docs/architecture/architecture.md` の「dry-run 方針」、`docs/operations/operations.md` |
| テスト追加・変更 | `docs/testing/test_specification.md` |
| Backtest CLI・Strategy YAML | `docs/backtest.md`, `docs/architecture/strategy_definition.md` |
| Issue / PR の立て方・粒度 | `docs/development/issues-guideline.md` |
| リリース済み機能の位置づけ | `docs/product/roadmap.md` |

## テストスイート実行コマンド

ルートに `pytest.ini` / `conftest.py` / `pyproject.toml` は存在しない。**ディレクトリ単位で個別に**実行する(CI `.github/workflows/ci.yml` と同じ 6 スイート)。

```bash
python -m pytest backtest/tests -q
python -m pytest market-watch/tests -q
python -m pytest news-feed-bot/tests -q
python -m pytest paper-signal/tests -q
python -m pytest paper-execution/tests -q
python -m pytest broker/tests -q
```

- 依存は repo ルートの `requirements.txt`(`python -m pip install -r requirements.txt`)。
- テストは外部ネットワーク・実 DB・実 S3・実 Discord に到達してはならない。
- paper-execution は CI に Windows ジョブがある(SQLite ファイルハンドル / snapshot cleanup)。Windows 固有の副作用に触れる変更では Windows での挙動を考慮する。
- CI はこの 6 スイートに加えて、リポジトリルートからの `broker` パッケージ import smoke test を実行する。

## 影響範囲調査のコマンド例

```bash
# 関数・クラス・定数の参照元(コード・テスト・docs・CI・scripts を含める)
grep -rn "対象シンボル" --include="*.py" --include="*.md" --include="*.yml" .

# 環境変数の参照元(.env.example も対象)
grep -rn "ENV_VAR_NAME" --include="*.py" --include="*.md" --include="*.example" .

# DB テーブル・列の参照元
grep -rn "table_or_column_name" --include="*.py" --include="*.md" .
```

- 動的 import(`importlib` 等)や文字列組み立てによる参照は grep で漏れることがある。境界となるモジュールでは AST ベースの検査テスト(例: `broker/tests` の依存禁止テスト)の有無も確認する。
- 他コンポーネントから import される共有境界を追加・変更したときは、リポジトリルートからの import 可否(`python -c "from broker.broker_interface import Broker"` のような smoke test)を CI に含める。

## 過去のレビューで繰り返された指摘(先回りチェック)

実装前に、同種の問題がないか確認する。仕様の正は各 docs。

- パッケージ import 構成: テストだけ `sys.path` を細工して素の import 失敗を隠していないか(PR #87 r1)。
- 共通モデルの型検証: dataclass の enum / tuple / クロスフィールド不変条件を `__post_init__` で検証し、不正値拒否テストがあるか(PR #87 r1)。
- 状態契約の網羅性: docstring で宣言した不変条件(例: terminal 状態の content 不変)が、対象フィールド全部に対して実装・テストされているか(PR #87 r3)。
- 一意性制約: ID 類(fill_id 等)の一意性を宣言するなら検証もするか(PR #87 r4)。
- dry-run / snapshot の Windows 挙動: SQLite 接続の close 漏れによる cleanup 失敗(Issue #83 r2)。
- 通知等の副作用は transactional outbox など再送安全な形か(Issue #83 r1)。
