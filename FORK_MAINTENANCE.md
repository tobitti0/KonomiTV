# KonomiTV フォーク運用手順

この文書は `tobitti0/KonomiTV` フォーク固有の運用手順です。
本流の完全なミラーとして維持する `master` には追加せず、`dev-tobitti` だけで管理します。

## ブランチとリモートの役割

| 名前 | 役割 | 運用ルール |
| --- | --- | --- |
| `upstream/master` | `tsukumijima/KonomiTV` の本流 | 読み取り元。直接変更しない |
| `origin/master` | フォーク側の本流ミラー | 常に `upstream/master` と同じコミットを指す。独自コミットを置かない |
| `origin/dev-tobitti` | 恒久的な独自機能を統合する開発ブランチ | 本流更新と、採用が確定した独自機能だけを取り込む |
| `integrate/upstream-YYYY-MM-DD` | 本流更新の競合解消・検証用ブランチ | `dev-tobitti` から作り、検証後に `dev-tobitti` を fast-forward する |
| `future/*` | 機能単位の開発・保管ブランチ | 実装・レビュー・テストが完了するまで `dev-tobitti` へ統合しない |

`master` を本流と同一に保つことで、本流の更新量と独自差分を明確に分離できます。
フォーク固有のコード・設定・文書を `master` へ直接コミットしてはいけません。

## 本流へ追従する標準手順

### 1. 作業前の確認

作業ツリーが clean であることと、リモート設定を確認します。

```bash
git status --short --branch
git remote -v
```

`upstream` が未登録の場合だけ、次のように追加します。

```bash
git remote add upstream git@github.com:tsukumijima/KonomiTV.git
```

リモートを取得し、ローカルとフォーク側の `master` に独自コミットがないことを確認します。

```bash
git fetch --prune origin
git fetch --prune upstream
git rev-list --left-right --count master...upstream/master
git rev-list --left-right --count origin/master...upstream/master
```

最後の2コマンドは、更新前であれば `0 N` となるのが正常です。左側が 1 以上の場合はローカルまたはフォークの `master` に独自コミットがあるため、同期作業を停止します。

### 2. フォークの `master` を先に更新する

必ず `master` を本流へ fast-forward してから、`dev-tobitti` の統合作業を始めます。

```bash
git switch master
git merge --ff-only upstream/master
git rev-list --left-right --count master...upstream/master
git push origin master
```

push の直前に、ローカル `master` と本流の左右の独自コミット数がともに 0、つまり `0 0` であることを必ず確認します。これにより、ローカル `master` の独自コミットを誤ってフォークへ push する事故を防ぎます。

push 後は、フォーク側も `0 0` であることを確認します。

```bash
git rev-list --left-right --count origin/master...upstream/master
```

期待値は `0 0` です。

事前確認または `--ff-only` が失敗した場合、その場で通常マージや force push を行わず、次のコマンドでローカルとフォーク側の差分を確認してから、独自コミットの退避方法を決めます。

```bash
git log --oneline --left-right master...upstream/master
git log --oneline --left-right origin/master...upstream/master
```

### 3. 統合ブランチで `dev-tobitti` に取り込む

日付は作業日の `YYYY-MM-DD` に置き換えます。

```bash
git switch dev-tobitti
git pull --ff-only origin dev-tobitti
git switch -c integrate/upstream-YYYY-MM-DD
git merge --no-ff --no-commit master
```

`--no-commit` でマージコミット作成前に止め、競合解消・レビュー・テストを行います。

競合解消時は `ours` または `theirs` をファイル全体へ機械的に適用せず、本流の変更意図と独自機能を箇所ごとに統合します。特に次を確認します。

- Box 録画ストリーミング、CM 解析、シリーズ管理、コメント強度グラフ、Cloudflare Access 再認証、Akebi 無効化などの恒久機能が残っている
- 本流で削除・置換された処理を、競合解消によって不用意に復活させていない
- DB migration の番号衝突、モデル・設定項目・lockfile の不整合がない
- 非同期関数内に同期ファイル I/O を持ち込んでいない
- Dockerfile の thirdparty と、本流コードが要求するエンコーダー機能・バージョンが一致している
- `client/dist` を競合解消後のソースから再生成している

### 4. 検証する

まず未解決競合と競合マーカーを確認します。

```bash
git diff --name-only --diff-filter=U
rg -n --hidden --glob '!client/dist/**' --glob '!.git/**' '^(<<<<<<<|=======|>>>>>>>)' .
git diff --check -- . ':(exclude)client/dist/**'
git diff --cached --check -- . ':(exclude)client/dist/**'
```

クライアントは `client/` で検証します。

```bash
cd client
yarn install --frozen-lockfile
yarn eslint . --ignore-path .gitignore
yarn lint
yarn typecheck
yarn build
cd ..
git add client/dist
```

サーバーは `server/` で、Python を必ず Poetry 経由で実行します。

```bash
cd server
poetry check --lock
poetry install --with dev --no-root
poetry run ruff check --no-cache .
poetry run pyright
poetry run task lint
PYTHONDONTWRITEBYTECODE=1 poetry run python -m unittest discover -s tests -v
cd ..
```

インストーラーに本流差分がある場合は `installer/` も検証します。

```bash
cd installer
poetry check --lock
poetry install --no-root
poetry run ruff check --no-cache .
poetry run pyright
poetry run task lint
cd ..
```

`yarn lint` と `poetry run task lint` は自動修正を行うため、先に非書き換えの ESLint・Ruff・Pyright を通し、実行後の差分をレビューします。

2026-08-27 時点の本流には、`installer/pyproject.toml` の Linux 用 PyInstaller 依存が未定義の `pypi` source を参照するため、`poetry check --lock` だけが失敗する既知の設定問題があります。依存インストール・Ruff・Pyright の結果と本流との差分を確認し、本流由来の問題と統合起因の問題を区別します。

ローカルに指定バージョンの Node.js・Python・Poetry がない場合は、対応バージョンを固定した隔離 Docker 環境で同じ検証を行います。ホスト側の作業ツリーへ `.venv`、`node_modules`、キャッシュなどの意図しない差分を作らないようにします。

### 5. 統合コミットを作り、先に統合ブランチを push する

検証で生じた差分を含めて最終確認し、マージコミットを作成します。

```bash
git add -A
git diff --cached --check -- . ':(exclude)client/dist/**'
git status --short --branch
git commit -m 'Merge upstream/master into dev-tobitti'
git push -u origin integrate/upstream-YYYY-MM-DD
```

統合ブランチを先に push しておくと、`dev-tobitti` 更新後に問題が見つかった場合も、検証時点の履歴を明確に参照できます。

### 6. `dev-tobitti` を fast-forward して push する

```bash
git switch dev-tobitti
git merge --ff-only integrate/upstream-YYYY-MM-DD
git push origin dev-tobitti
```

最後に、`master` が本流と一致し、`dev-tobitti` が本流を祖先として含むことを確認します。

```bash
git fetch --prune origin
git fetch --prune upstream
git rev-list --left-right --count origin/master...upstream/master
git merge-base --is-ancestor upstream/master origin/dev-tobitti
git status --short --branch
```

- 1つ目の期待値: `0 0`
- 2つ目の期待値: 終了コード `0`
- 3つ目の期待値: clean かつ `origin/dev-tobitti` と同期済み

## 禁止事項と復旧

- `dev-tobitti` を `master` へマージ・rebase・cherry-pick しない
- `master` にフォーク固有のコミットを作らない
- `master` の同期に通常マージ・squash・force push を使わない
- 競合ファイルを一括して `ours` / `theirs` だけで解消しない
- 本流追従と無関係なWIPを同じマージコミットへ混ぜない
- Git の同期を理由に、実環境のデプロイや再起動まで自動的に行わない

マージコミット作成前に統合を取り消す場合は、統合ブランチ上で次を実行します。

```bash
git merge --abort
```

公開済みの `dev-tobitti` に問題が見つかった場合は、原則として履歴を書き換えず、原因修正または `git revert` を新しいコミットとして追加します。

## 一時作業と恒久機能を分ける

データ移行・一括補正のためだけに作ったコードは、実行が完了しても原則として `dev-tobitti` へ統合しません。保管が必要なものは専用ブランチでレビュー・コミットし、そのブランチだけをリモートへ push します。未コミットの作業を保管済みとして扱ってはいけません。

2026-08-27 時点の分類は次のとおりです。

- `future/box-history-import`: Box 履歴移行の一回限りのツール。統合しない
- `future/normalize-recorded-program-titles`: 移行後のタイトル一括補正用。統合しない。現時点では専用コミット・リモートブランチ未作成
- `future/recording-default-profile`: 恒久機能候補だが、レビューとテストが完了するまで統合しない

恒久機能を `future/*` から統合する場合も、本流追従とは別のコミットまたはマージとして扱います。本流更新のマージコミットへ無関係なWIPを混ぜません。

## デプロイは別工程として扱う

Git の同期・統合完了は、実環境へのデプロイ許可や動作確認完了を意味しません。デプロイ前には少なくとも次を別途確認します。

- 本番DBをバックアップし、コピーDBで Aerich migration を検証する
- Dockerfile が取得する thirdparty と、コードが要求する HWEncC オプションの互換性を確認する
- 使用中の QSVEncC・NVEncC・VCEEncC・rkmppenc で実際に配信を開始する
- Box-only 録画の再生・Rangeシーク・ダウンロード・切断時cleanupを確認する
- PWA更新、オフライン保存・再生、CMチャプター、シリーズ次話再生を確認する
- ユーザー管理の KonomiTV・PM2・Docker プロセスを、明示的な許可なく再起動しない

### 本番 Docker 環境の更新手順

本番環境では次の配置を前提とします。

- checkout: `/home/tobitti/Dockers/TVSystems/KonomiTV`
- Compose file: `/home/tobitti/Dockers/TVSystems/docker-compose.yml`
- Compose project / service: `tvsystems` / `konomitv`
- container: `tvsystem.konomitv`
- image: `tvsystems-konomitv:latest`

ソースコードは bind mount ではなく image にコピーされます。したがって、`git pull` 後の `docker compose restart konomitv` だけでは更新されません。新 image を build し、`konomitv` だけを `--no-deps --force-recreate` で再作成します。`mirakurun`、`edcb`、`mysql`、`epgstation`、`nginx`、`tvdashboard`、`cloudflared` は再作成しません。

まず checkout、Compose の解決結果、稼働中の container と image を確認します。`server/data` 配下には正規の未追跡ランタイムデータがあるため、tracked file の差分だけを確認します。

```bash
checkout_path=/home/tobitti/Dockers/TVSystems/KonomiTV
compose_path=/home/tobitti/Dockers/TVSystems/docker-compose.yml

test "$(git -C "$checkout_path" branch --show-current)" = dev-tobitti
git -C "$checkout_path" diff --quiet
git -C "$checkout_path" diff --cached --quiet
docker compose -f "$compose_path" config --quiet
docker compose -f "$compose_path" ps konomitv
```

切替前に、旧 Git SHA、旧 image、設定、Compose 定義、SQLite DB を退避します。DB は稼働中の旧 image に含まれる Python の SQLite backup API を使い、WAL の内容を含む一貫したコピーを作成します。バックアップ先は checkout の外に置き、権限を `0700` にします。

```bash
deploy_stamp=$(date +%Y%m%d-%H%M%S)
backup_dir="/home/tobitti/Dockers/TVSystems/KonomiTV-deploy-backups/$deploy_stamp"
old_git_sha=$(git -C "$checkout_path" rev-parse HEAD)
old_container_id=$(docker compose -f "$compose_path" ps -q konomitv)
old_image_id=$(docker inspect --format '{{.Image}}' "$old_container_id")
rollback_tag="tvsystems-konomitv:rollback-$deploy_stamp-$old_git_sha"

install -d -m 0700 "$backup_dir"
docker image tag "$old_image_id" "$rollback_tag"
printf '%s\n' "$old_git_sha" > "$backup_dir/old-git-sha.txt"
printf '%s\n' "$old_image_id" > "$backup_dir/old-image-id.txt"
printf '%s\n' "$rollback_tag" > "$backup_dir/rollback-tag.txt"
cp -a "$checkout_path/config.yaml" "$compose_path" "$backup_dir/"

docker run --rm --network none --read-only --tmpfs /tmp \
    --entrypoint /code/server/.venv/bin/python \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$checkout_path/server/data:/source:ro" \
    -v "$backup_dir:/backup" \
    "$old_image_id" \
    -c "import sqlite3; s=sqlite3.connect('file:/source/database.sqlite?mode=ro', uri=True); d=sqlite3.connect('/backup/database.sqlite'); s.backup(d); d.close(); s.close()"

db_check=$(docker run --rm --network none --read-only --tmpfs /tmp \
    --entrypoint /code/server/.venv/bin/python \
    -v "$backup_dir:/backup:ro" \
    "$old_image_id" \
    -c "import sqlite3; c=sqlite3.connect('file:/backup/database.sqlite?mode=ro&immutable=1', uri=True); print(c.execute('PRAGMA quick_check').fetchone()[0]); c.close()")
test "$db_check" = ok
```

本番 checkout の `origin` は pull 専用の HTTPS URL にしておくと、通常は SSH host key や秘密鍵の状態に依存しません。ただし、グローバルな `url.*.insteadOf` 設定により HTTPS URL が SSH URL へ書き換えられる場合があります。設定上の URL と Git が実際に使う URL を両方確認します。

```bash
git -C "$checkout_path" remote set-url origin https://github.com/tobitti0/KonomiTV.git
git -C "$checkout_path" config --get remote.origin.url
git -C "$checkout_path" remote get-url origin
git config --show-origin --get-regexp '^url\..*\.insteadOf$' || true
git -C "$checkout_path" fetch --prune origin
git -C "$checkout_path" merge --ff-only origin/dev-tobitti
new_git_sha=$(git -C "$checkout_path" rev-parse HEAD)

git -C "$checkout_path" diff --name-status "$old_git_sha..$new_git_sha" -- \
    server/app/migrations config.example.yaml docker-compose.example.yaml Dockerfile
```

SSH に書き換えられていて host key 検証に失敗した場合は、`StrictHostKeyChecking=no` で回避しません。GitHub 公式の [SSH key fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints) と照合し、古い host または解決先 IP のエントリーだけを `ssh-keygen -R <host-or-ip>` で削除してから再接続します。`known_hosts` 全体を消してはいけません。

取得後は必ず fast-forward のみで更新し、実際の旧 SHA から migration・設定・Compose 定義の差分を確認します。

旧 container を稼働させたまま `konomitv` image だけを build します。切替前に新 image の ID、NVEncC のバージョン、コードが要求するオプションを確認します。本流の `build_thirdparty.yaml` が指定する HWEncC バージョンと Dockerfile の固定値も、同期のたびに照合します。

```bash
docker compose -f "$compose_path" build konomitv
new_image_id=$(docker image inspect --format '{{.Id}}' tvsystems-konomitv:latest)
test "$new_image_id" != "$old_image_id"
docker image tag "$new_image_id" "tvsystems-konomitv:$new_git_sha"

docker run --rm --gpus all \
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility,video \
    --entrypoint /code/server/thirdparty/NVEncC/NVEncC.elf \
    "$new_image_id" --version
docker run --rm --gpus all \
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility,video \
    --entrypoint /code/server/thirdparty/NVEncC/NVEncC.elf \
    "$new_image_id" --check-hw
docker run --rm --gpus all \
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility,video \
    --entrypoint /code/server/thirdparty/NVEncC/NVEncC.elf \
    "$new_image_id" --help 2>&1 | grep -- '--adapt-resolution'
```

ライブ視聴中のセッションがないことを再確認してから切り替えます。`--no-build` により、直前に検証した image だけを使います。

```bash
curl -fsS http://127.0.0.1:7000/api/streams/live
docker compose -f "$compose_path" up -d --no-deps --force-recreate --no-build konomitv
```

切替後は container が新 image ID を使っていること、API が応答すること、再起動ループや startup error がないことを確認します。Compose に healthcheck はないため、`up -d` の成功だけで完了扱いにしません。

```bash
new_container_id=$(docker compose -f "$compose_path" ps -q konomitv)
test "$(docker inspect --format '{{.Image}}' "$new_container_id")" = "$new_image_id"
curl -fsS http://127.0.0.1:7000/api/version
docker compose -f "$compose_path" logs --tail=250 konomitv
docker inspect --format '{{.RestartCount}}' "$new_container_id"
```

最後に、チャンネル一覧・録画一覧・録画ファイルの Range 応答を確認します。空きチューナーがあり録画や視聴を妨げない場合だけ、短時間のライブ配信も開始し、`Standby` → `ONAir` → `Idling` → `Offline` と遷移すること、ログ上の NVEncC コマンドに `--adapt-resolution` が含まれることを確認します。テスト接続を切断した後に `Idling` やクライアントが残った状態で完了扱いにしてはいけません。

異常がある場合は、DB migration の有無を確認したうえで旧 image へ戻します。migration がない更新では、通常 DB の復元は不要です。

```bash
docker image tag "$rollback_tag" tvsystems-konomitv:latest
docker compose -f "$compose_path" up -d --no-deps --force-recreate --no-build konomitv
rollback_container_id=$(docker compose -f "$compose_path" ps -q konomitv)
test "$(docker inspect --format '{{.Image}}' "$rollback_container_id")" = "$old_image_id"
```

DB 自体を戻す必要がある場合だけ、KonomiTV container を停止して現行の DB・WAL・SHM を別名で退避してから、検証済みの `database.sqlite` を復元します。稼働中の DB ファイルを直接上書きしてはいけません。

## 運用記録

### 2026-08-27

- `origin/master` を `ba7a6f04` から本流 `5cbbd348` へ fast-forward
- 本流 `5cbbd348` を `dev-tobitti` へ統合したマージコミット: `42615681`
- `box-history-import` と `recording-default-profile` は統合対象外として維持
- 本流コードが要求する `--adapt-resolution` に合わせ、Docker image 内の QSVEncC / NVEncC / VCEEncC を 8.26 / 9.31 / 9.12 へ固定更新
- 本番 checkout を `33bc6f2c` から `db0e1254` へ fast-forward し、KonomiTV container だけを再作成
- 本番 image: `sha256:6cabb787f45c18159fcaaeda6906d8898f59532515fc4bfa78039337403fa0b0`
- バックアップ: `/home/tobitti/Dockers/TVSystems/KonomiTV-deploy-backups/20260827-131354.ILATQ1`
- NVEncC 9.31 / GTX 1050 Ti の H.264・H.265 対応、録画 Range 応答、`gr011-720p` の実配信と切断後 cleanup を確認
