# EDCB の録画プラグイン設定の保持

`future/recording-default-profile` は、Linux EDCB の録画プラグインを Windows 用の
`.dll` に置き換えてしまう問題を修正するブランチです。

## 動作

- 予約と EpgTimerSrv.ini のプリセットから書き込み・ファイル名変更プラグインを取得し、
  `.so`・`.dll`・独自名をそのまま予約作成・更新へ渡します。
- 未指定のプラグイン、`?` を含むマクロ、明示的な空オプションも保持します。
- 録画フォルダの `write_plugin` と `recording_file_name_plugin` は必須です。
  プラグイン名を送らない旧クライアントの要求は HTTP 422 で拒否します。
  更新後はブラウザー・PWA を再読み込みしてください。
  `recording_folders: []` による EDCB デフォルト保存先の利用は引き続き可能です。
- 画面では最初の通常録画フォルダを編集し、追加保存先とワンセグ保存先を保持します。
  フォルダ新規追加時は ID=0 のプリセットから通常録画のプラグインを引き継ぎます。
- 既存フォルダもデフォルトプリセットのフォルダもない場合、保存先・マクロ欄は無効になります。
  EDCB 側のデフォルトプリセットに録画フォルダを設定し、画面を開き直してください。
  ファイル名変更プラグインが未指定の場合は、その設定を保持してマクロ入力を無効にします。
- デフォルトプリセットの取得失敗時は新規予約を中止します。
  既存予約に含まれるプラグイン設定を使った編集は引き続き可能です。

## 検証

サーバーの Python 3.11 / Poetry 環境で実行します。

```sh
cd server
poetry run python -m unittest discover -s tests -p 'test_recording_plugins.py' -v
poetry run task lint
```

クライアントの依存関係をインストールした Node.js 環境で実行します。

```sh
cd client
node --test tests/recording-folder-settings.test.cjs
yarn lint
yarn typecheck
```

テストでは Linux / Windows / 独自プラグインの往復変換、通常・ワンセグ用プリセット、
旧形式 API 要求の拒否、設定取得失敗、画面での追加・変更・空欄化を確認します。
EDCB への通信はモック化しており、本番予約を変更しません。
本番での実際の予約追加・更新とデプロイは別途実施する工程です。
