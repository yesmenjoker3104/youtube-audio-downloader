# YouTube Audio Downloader

YouTube動画の音声ダウンロードと、Demucsによる音源分離を行うWindows/macOSアプリです。

## Windows配布版の準備

Windows版はPython環境からビルドする必要はありません。GitHub CLI (`gh`) でGitHub ReleaseのZIPを取得し、解凍したフォルダ一式をそのまま使用します。

```powershell
gh auth login
.\setup.ps1
```

`setup.ps1` は次のリリースアセットを取得します。

`https://github.com/yesmenjoker3104/youtube-audio-downloader/releases/download/dev-latest/YoutubeAudioDownloader-Windows.zip`

既定の展開先は `release_runtime\YoutubeAudioDownloader` です。ここには少なくとも次が含まれます。

- `YoutubeAudioDownloader.exe`
- `YoutubeAudioDownloaderCli.exe`
- `_internal\` 配下のPythonランタイム、ライブラリ、その他の同梱ファイル

### 注意

- `YoutubeAudioDownloaderCli.exe` だけをコピーして実行してはいけません。
- `_internal` を含む配布フォルダ内の全ファイルが必要です。
- 配布版を使う場合、PySide6やDemucsなどを別途インストールする必要はありません。
- ソースから実行・再ビルドする場合だけ、`youtube_dl\requirements.txt` と `youtube_dl\build.ps1` を使用します。

## 起動

GUI版:

```powershell
.\release_runtime\YoutubeAudioDownloader\YoutubeAudioDownloader.exe
```

CLI版:

```powershell
.\release_runtime\YoutubeAudioDownloader\YoutubeAudioDownloaderCli.exe --help
```

詳しい操作方法は [youtube_dl/MANUAL.md](youtube_dl/MANUAL.md) を参照してください。

## アップデート

配布版（EXE / .app）は、メニューの **ヘルプ → アップデートを確認...** で最新の正式リリースを確認できます。新しいバージョンがあれば、ダウンロード後にアプリを再起動して自動的に差し替えます。

- 確認とダウンロードには GitHub CLI (`gh`) とログイン（`gh auth login`）が必要です（リポジトリが非公開のため）。
- 差し替え時もユーザーデータ（`download_list.json`、`models\`）は残ります。
- 処理（ダウンロード・音源分離）中はアップデートを適用しません。
- ソースから実行した場合は自動アップデートを行いません。
- 自動アップデートは本機能を含むバージョンから有効です。それ以前の配布版は、新しい ZIP を手動で入れ替えてください。

リリース手順（開発者向け）は [AGENTS.md](AGENTS.md) を参照してください。