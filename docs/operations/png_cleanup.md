# PNG cleanup operation

EC2 のローカルディスクに残る Chart Snapshot PNG を定期削除する。S3 オブジェクト、SQLite、ログ、`.env`、state ファイルは対象外。

## 対象と保持期間

既定の systemd unit は次だけを対象にする。

```text
/opt/berial/market-watch/data/chart_images
```

このディレクトリは `market-watch/chart_snapshot.py` が S3 upload 前に生成するローカル PNG の保存先である。Alert画像は `TemporaryDirectory` を使うため通常は処理終了時に消える。

既定保持期間は **7日**。最終更新時刻が cutoff と同時刻のファイルは保持し、cutoff より古い通常ファイルだけを削除する。サブディレクトリは再帰走査せず、シンボリックリンクは追跡しない。

対象を追加する場合は、service の `PNG_CLEANUP_TARGETS` を `:` 区切りの絶対パスで変更し、同じパスを `ReadWritePaths` にも追加する。リポジトリルート、`/`、`/tmp`、`/opt` など広すぎるパス、相対パス、`..` を含むパスは拒否される。**対象は実体パスで指定する**: パスの途中に symlink が含まれる場合も fail-closed で拒否される(symlink 経由の対象外削除防止)。保持期間は有限の 0 以上の数値のみ受理される(`nan` / `inf` / 負数は設定エラーとして終了コード2)。

## dry-run

ファイル削除、ディレクトリ作成、lock file 作成を行わず、対象ファイルと予定容量だけを出力する。

```bash
cd /opt/berial

/opt/berial/.venv/bin/python scripts/cleanup_png.py \
  --target /opt/berial/market-watch/data/chart_images \
  --retention-days 7 \
  --dry-run
```

複数ディレクトリ:

```bash
/opt/berial/.venv/bin/python scripts/cleanup_png.py \
  --target /opt/berial/market-watch/data/chart_images \
  --target /opt/berial/another/component/chart_images \
  --retention-days 7 \
  --dry-run
```

正常時は `SUMMARY` に `candidates` / `candidate_bytes` / `warnings` / `errors` が表示される。対象ディレクトリが存在しない場合は warning 扱いで正常終了する。危険なパス、scan失敗、削除失敗は `errors` に計上して終了コード1となる。

## 手動 cleanup

まず dry-run を確認してから実行する。

```bash
cd /opt/berial

/opt/berial/.venv/bin/python scripts/cleanup_png.py \
  --target /opt/berial/market-watch/data/chart_images \
  --retention-days 7
```

環境変数でも指定できる。

```bash
export PNG_CLEANUP_TARGETS=/opt/berial/market-watch/data/chart_images
export PNG_CLEANUP_RETENTION_DAYS=7
/opt/berial/.venv/bin/python /opt/berial/scripts/cleanup_png.py
```

通常実行は `flock` で多重起動を抑止する。別プロセスが実行中なら warning を出して削除せず正常終了する。一部のファイル削除に失敗しても残りは継続し、最後に非0終了する。cleanup失敗は Market Watch 常駐サービスへ伝播しない。

**lock の共有**: lock は既定 `/tmp/berial-png-cleanup.lock` を service・手動実行の双方が使うため、追加指定なしで排他になる。service に `PrivateTmp=` を設定しないのは意図的で、private `/tmp` では timer 実行と手動実行の flock が別ファイルになり排他にならないためである。lock は `O_NOFOLLOW`・mode 0600 で開き、symlink だった場合や親ディレクトリ不存在時はエラー終了する。既存 lock は実行ユーザー所有・link 数 1 の regular file のみ受理し(他ユーザー所有・hardlink は拒否)、permission が広い場合は 0600 へ矯正する。cleanup は lock を含めディレクトリを一切作成しない。dry-run は削除しないため lock を取得しない。

## systemd timer の導入

実行ユーザーは `ubuntu`。毎日 03:20 Asia/Tokyo を基準に、最大15分のランダム遅延を付けて実行する。`restart_all.sh` とは分離している。

```bash
sudo cp /opt/berial/scripts/systemd/berial-png-cleanup.service /etc/systemd/system/
sudo cp /opt/berial/scripts/systemd/berial-png-cleanup.timer /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now berial-png-cleanup.timer
```

設定確認:

```bash
systemd-analyze verify /etc/systemd/system/berial-png-cleanup.service \
  /etc/systemd/system/berial-png-cleanup.timer
systemd-analyze calendar '*-*-* 03:20:00 Asia/Tokyo'
systemctl list-timers --all | grep berial-png-cleanup
systemctl status berial-png-cleanup.timer --no-pager
```

手動で service を1回実行してログを確認:

```bash
sudo systemctl start berial-png-cleanup.service
systemctl status berial-png-cleanup.service --no-pager
journalctl -u berial-png-cleanup.service -n 100 --no-pager
```

停止・無効化:

```bash
sudo systemctl disable --now berial-png-cleanup.timer
```

unit を変更した場合:

```bash
sudo systemctl daemon-reload
sudo systemctl restart berial-png-cleanup.timer
```

`Persistent=true` のため停止中に予定時刻を逃した場合、次回起動後に1回追い付き実行される。service は oneshot であり、常駐プロセスではない。

## ディスク使用量の確認

```bash
df -h /
du -sh /opt/berial/market-watch/data/chart_images
find /opt/berial/market-watch/data/chart_images \
  -maxdepth 1 -type f -name '*.png' -printf '%s\n' \
  | awk '{sum += $1; count += 1} END {printf "files=%d bytes=%d\n", count, sum}'
```

大きいファイルを確認:

```bash
find /opt/berial/market-watch/data/chart_images \
  -maxdepth 1 -type f -name '*.png' -printf '%s %TY-%Tm-%Td %TH:%TM %p\n' \
  | sort -nr | head -20
```

## 緊急時

空き容量が危険な場合も、いきなり広い `find -delete` は使わない。

```bash
cd /opt/berial

# 1. 対象確認
/opt/berial/.venv/bin/python scripts/cleanup_png.py \
  --target /opt/berial/market-watch/data/chart_images \
  --retention-days 1 \
  --dry-run

# 2. 1日より古いPNGだけ削除(lock は既定パスで timer 実行と排他になる)
/opt/berial/.venv/bin/python scripts/cleanup_png.py \
  --target /opt/berial/market-watch/data/chart_images \
  --retention-days 1

# 3. 空き容量と結果を確認
df -h /
du -sh /opt/berial/market-watch/data/chart_images
```

`--retention-days 0` は現在時刻より古いPNGをほぼすべて対象にするため、緊急時でも必ず dry-run を先に確認する。生成中・upload中のファイルを巻き込む可能性が上がるため、通常運用では使わない。
