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