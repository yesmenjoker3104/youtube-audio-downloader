# YouTube Audio Downloader — エージェント向けプロジェクト説明書

## クイックスタート

**プロジェクト:** YouTubeから動画の音声をダウンロードし、AI（Demucs）を使ってミックスされた音声を個別の楽器トラックに分離するデスクトップGUIアプリケーション。

**技術スタック:** Python 3.11+、PySide6（Qt）、yt-dlp、PyTorch、Demucs

**ローカル実行:**
```bash
cd youtube_dl
python -m venv venv
.\venv\Scripts\activate    # Windows
source venv/bin/activate   # macOS
pip install -r requirements.txt
python app.py
```

**ビルド・リリース:** [build.ps1](youtube_dl/build.ps1)（Windows）と [build_mac.sh](youtube_dl/build_mac.sh)（macOS）を参照。Gitタグ推送時に [.github/workflows/build.yml](.github/workflows/build.yml) で自動ビルド。

---

## アーキテクチャ概要

### コアコンポーネント

| コンポーネント | ファイル | 責務 |
|-----------|------|-----------------|
| **メインウィンドウ** | [youtube_dl/app.py](youtube_dl/app.py) | タブベースGUI: YouTubeタブ（ダウンロード＆分離）、ローカルファイルタブ、設定 |
| **InfoWorker** | [youtube_dl/app.py#L175](youtube_dl/app.py#L175) | 非同期スレッド: YouTubeのURLから動画タイトルと縮小画像を取得 |
| **DownloadWorker** | [youtube_dl/app.py#L226](youtube_dl/app.py#L226) | 非同期スレッド: `yt-dlp` + ffmpeg経由で音声抽出（MP3またはMP4モード） |
| **SeparateWorker** | [youtube_dl/app.py#L379](youtube_dl/app.py#L379) | 非同期スレッド: Demucs AIモデルを実行して音声をステム（ボーカル、ドラム、ベースなど）に分割 |
| **AudioSeparator** | [youtube_dl/utils/separator.py](youtube_dl/utils/separator.py) | Core Demucsラッパー: モデル読込、ステム抽出、メモリ管理 |
| **StatusWidget** | [youtube_dl/app.py#L538](youtube_dl/app.py#L538) | カスタムテーブルセル: 各ダウンロード/分離タスクのリアルタイムプログレス表示 |

### 設計パターン

- **Signal-Slot アーキテクチャ:** ワーカースレッドがQtシグナル（info_ready、dl_progress、dl_done、sep_error）を発行 → メインスレッドUI更新。これでUIの応答性を維持。
- **スレッドプール:** メタデータ取得用（6スレッド）、ダウンロード用（デフォルト3）、分離用（デフォルト3）の独立したプール。設定タブで変更可能。
- **遅延インポート:** 重い依存関係（yt-dlp、Demucs、torch）はワーカースレッド内でのみインポート → 起動が高速。
- **永続化:** ダウンロード履歴は [youtube_dl/download_list.json](youtube_dl/download_list.json)（JSON形式）に保存。

### データフロー

```
ユーザー入力（URLまたはローカルファイル）
  ↓
InfoWorker（非同期） → タイトル/サムネイル取得
  ↓
DownloadWorker（非同期） → yt-dlp + ffmpeg → audio.mp3
  ↓
SeparateWorker（非同期） → Demucs → {vocals, drums, bass, ...}.mp3
  ↓
出力フォルダ（~/Downloads/YoutubeAudioDownloader/ またはユーザー選択）
```

---

## 開発環境セットアップ

### Pythonバージョン＆仮想環境

**要件:** Python 3.11以上（3.11でテスト済み）。

**⚠️ Windows App Control ポリシーの問題:**
プロジェクトがOneDriveまたは制限されたパス下にある場合、Windows App Controlポリシーがvenvのpython.exeをブロックする可能性があります。**解決策:**
1. OneDrive外に仮想環境を作成: `python -m venv %LocalAppData%/venvs/youtube_dl`
2. またはシステムPythonを使用: `py -3.11`（winget/microsoft.comからインストールが必要）

詳細は [ユーザーメモリ: debugging.md] を参照。

### 依存関係

```bash
pip install -r youtube_dl/requirements.txt
```

主要パッケージ:
- **PySide6** (≥6.5.0): Qt GUIフレームワーク
- **yt-dlp[default]** (≥2026.8.19): YouTubeダウンロード（YouTube の JS チャレンジ解決用 yt-dlp-ejs を含む）
- **Demucs** (≥4.0.0): 音声ソース分離
- **torch** & **torchaudio** (CPU版、PyPIまたはpytorch.org)

**PyTorch CPU インストール** （`pip install -r` が動作しない場合）:
```bash
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
```

### 初回実行

1. クローン＆仮想環境に入る（上記参照）
2. `cd youtube_dl && python app.py`
3. 初回Demucs実行時に自動的にモデルをダウンロード（~400MB を `TORCH_HOME/hub/checkpoints/` に）— 2～5分かかる場合あり
4. モデルがキャッシュされた後、分離は高速（曲あたり~30～60秒）

---

## コード規約＆パターン

### スレッド処理＆ Qt シグナル

すべてのネットワーク/重い入出力処理はワーカースレッドで実行する必要があります。パターン例:

```python
class MyWorker(QObject):
    finished = pyqtSignal(result_type)
    error = pyqtSignal(str)
    
    def run(self):
        try:
            result = expensive_operation()
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))

# メインスレッド内:
worker = MyWorker()
worker.moveToThread(self.thread_pool_threadname)
worker.finished.connect(self.on_finished)
worker.error.connect(self.on_error)
self.thread_pool_threadname.start(worker)
```

すべてのUI更新はシグナルスロット接続経由でメインスレッドで行う必要があります（ワーカーからUIにアクセスしないでください）。

### Demucs 統合

`AudioSeparator` を呼び出してステムを分離します。例:

```python
from utils.separator import AudioSeparator

sep = AudioSeparator(model_name="htdemucs")
stems = sep.separate(audio_file_path)  # dict: {"vocals": path, "drums": path, ...}
```

[youtube_dl/utils/separator.py](youtube_dl/utils/separator.py) でAPI とモデルオプション（htdemucs、htdemucs_ft、hdemucs_mmi）を確認。

### yt-dlp 統合

`DownloadWorker` は yt-dlp オプションを管理します。[app.py#L226](youtube_dl/app.py#L226) のオプション辞書とプログレスフックを確認して、フォーマット選択と出力パス処理を理解してください。

### ファイルパス＆出力

- 出力フォルダ: `~/Downloads/YoutubeAudioDownloader/` (Windows/macOS)
- ステムファイル命名規則: `{title}_{stem}.mp3` (例: `Song_vocals.mp3`)
- 永続化されるメタデータ: [youtube_dl/download_list.json](youtube_dl/download_list.json)

---

## ビルド＆リリース

### ローカルビルド

**Windows:**
```powershell
cd youtube_dl
.\build.ps1
# 出力: dist/YoutubeAudioDownloader/YoutubeAudioDownloader.exe
```

**macOS:**
```bash
cd youtube_dl
chmod +x build_mac.sh
./build_mac.sh
# 出力: dist/YoutubeAudioDownloader.app
```

### GitHubへのリリース

バージョンの正は [youtube_dl/version.py](youtube_dl/version.py) の `__version__` です（`.spec` ではなくこちらを更新）。アプリの「ヘルプ → アップデートを確認」はこのバージョンと GitHub の最新正式リリース（タグ `vX.Y.Z`）を比較します。

1. [youtube_dl/version.py](youtube_dl/version.py) の `__version__` を更新（例: `1.2.3`）
2. ユーザー向けの変更がある場合、[MANUAL.md](youtube_dl/MANUAL.md) のチェンジログを更新
3. コミット: `git add . && git commit -m "v1.2.3 release"`
4. タグ付け: `git tag v1.2.3`（タグは `version.py` と一致させること。CI が不一致だと失敗します）
5. プッシュ: `git push origin main --tags`
6. CI が自動実行 → ビルド＆ GitHub Releases へ正式リリースとしてアップロード（`dev-latest` は更新されません）

---

## エージェント向けの一般的なタスク

### 新しいUI機能の追加（新しいタブなど）

1. [app.py](youtube_dl/app.py) のメインウィンドウクラスにQtシグナルとウィジェットを追加
2. 入出力が必要な場合は非同期ワーカーを作成（上記のスレッドパターンに従う）
3. シグナル → スロットをメインスレッドで配線
4. `python app.py` でテスト
5. ユーザー向けの場合、[MANUAL.md](MANUAL.md) を更新

### 音声分離の問題を修正する

1. [youtube_dl/utils/separator.py](youtube_dl/utils/separator.py) でモデル読込と Demucs API を確認
2. 音声フォーマット（MP3/MP4はDemucsのためffmpegでWAVに変換可能である必要がある）を確認
3. `SeparateWorker.run()` のログで詳細なエラーを確認
4. まず小さなローカルファイルでテスト

### 依存関係の更新

1. [youtube_dl/requirements.txt](youtube_dl/requirements.txt) を編集
2. 必要に応じて PyInstaller spec [youtube_dl.spec](youtube_dl/youtube_dl.spec) と [youtube_dl_mac.spec](youtube_dl/youtube_dl_mac.spec) を更新（例: 新しい隠しインポート）
3. ローカルでビルドをテスト
4. コミット＆タグ付けしてリリース

### リリース前のテスト

1. 手動テスト: Windows & macOS で `python app.py`
2. 各ワークフローをテスト: MP3ダウンロード、MP4ダウンロード、ローカルファイル分離、スレッドプールサイズ変更
3. `.exe` と `.app` をビルド＆テスト（ソース実行だけでなく）
4. ターミナルまたはUIにエラーログがないことを確認

---

## ドキュメント＆リソース

- **[MANUAL.md](MANUAL.md)**: ユーザーガイド（機能、設定、トラブルシューティング）
- **[youtube_dl/requirements.txt](youtube_dl/requirements.txt)**: ピン留めされた依存関係
- **PyInstaller Specs:** [youtube_dl.spec](youtube_dl/youtube_dl.spec)、[youtube_dl_mac.spec](youtube_dl/youtube_dl_mac.spec) — バンドルされたバイナリと隠しインポート
- **ビルドスクリプト:** [build.ps1](youtube_dl/build.ps1)、[build_mac.sh](youtube_dl/build_mac.sh) — ステップバイステップのビルドプロセス
- **CI/CD:** [.github/workflows/build.yml](.github/workflows/build.yml) — リリース自動化

---

## 注意点＆既知の問題

- **Demucs モデルダウンロード:** 初回実行時に~400MBモデルをダウンロード。2～5分の遅延が発生
- **Windows App Control:** OneDrive下の仮想環境はブロックされる可能性（環境セットアップを参照）
- **macOS Gatekeeper:** ビルド済み `.app` は `xattr -cr dist/YoutubeAudioDownloader.app` で実行可能に
- **PyTorch MPS フォールバック:** Apple Silicon は非サポート演算で自動的にフォールバックを有効化（正常な動作）
- **メモリ使用量:** 並行分離は~1～2GB/スレッド使用。2台以上の並列分離で最小4GB RAM必要
- **FFmpeg:** Windows ビルドは ffmpeg をバンドル。macOS は Homebrew または静的ビルドからダウンロード（build_mac.sh を参照）
- **YouTube の 403 エラー:** 配布版は CI ビルド時点の yt-dlp を同梱する。古い配布版は `HTTP Error 403: Forbidden` で失敗するため、`setup.ps1` の再実行（最新の `dev-latest` 取得）か再ビルドで更新する。YouTube の JS チャレンジ解決には Node.js / Deno が有効（`app.py` の `JS_RUNTIMES`）

---

## ファイル構成参考

```
youtube-audio-downloader/
├── youtube_dl/
│   ├── app.py              ← メインGUI＆ワーカー（ここから開始）
│   ├── updater.py           ← 自動アップデート（GitHub Release 取得・差し替え）
│   ├── version.py           ← アプリのバージョン（__version__）
│   ├── requirements.txt     ← Python依存関係
│   ├── youtube_dl.spec      ← PyInstaller spec（Windows）
│   ├── youtube_dl_mac.spec  ← PyInstaller spec（macOS）
│   ├── build.ps1            ← Windowsビルドスクリプト
│   ├── build_mac.sh         ← macOSビルドスクリプト
│   ├── download_list.json   ← ダウンロード履歴の永続化
│   ├── MANUAL.md            ← ユーザードキュメント
│   └── utils/
│       ├── separator.py     ← Demucsラッパー
│       ├── chord_analyzer.py, drum_transcriber.py, など
│       └── ...
├── .github/workflows/
│   └── build.yml            ← CI/CDパイプライン（リリース自動化）
└── AGENTS.md                ← このファイル
```

---

## 質問や問題がある場合

- ユーザー向けの問題については [MANUAL.md](MANUAL.md) のトラブルシューティングセクションを確認
- 開発上の問題については [app.py](youtube_dl/app.py) のワーカースレッドを確認
- リリースが失敗した場合、CI のビルドログを確認: [.github/workflows/build.yml](.github/workflows/build.yml)
