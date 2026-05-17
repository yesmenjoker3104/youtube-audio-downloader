#!/usr/bin/env bash
# ============================================================
# YouTube Audio Downloader — macOS ビルドスクリプト
# 使い方:
#   cd youtube_dl
#   chmod +x build_mac.sh
#   ./build_mac.sh
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "=== [1/4] PyInstaller を確認 ==="
if ! python -m pyinstaller --version &>/dev/null; then
    echo "  PyInstaller をインストールします..."
    pip install pyinstaller
else
    echo "  PyInstaller: OK"
fi

echo ""
echo "=== [2/4] ffmpeg バイナリを確認 ==="
FFMPEG_DIR="$SCRIPT_DIR/ffmpeg_mac"
mkdir -p "$FFMPEG_DIR"

if [ -f "$FFMPEG_DIR/ffmpeg" ] && [ -f "$FFMPEG_DIR/ffprobe" ]; then
    echo "  ffmpeg: 既に存在します。スキップします。"
else
    # Homebrew 経由でインストールされている場合はそちらを使用
    if command -v ffmpeg &>/dev/null && command -v ffprobe &>/dev/null; then
        echo "  Homebrew の ffmpeg を使用します..."
        cp "$(command -v ffmpeg)"  "$FFMPEG_DIR/ffmpeg"
        cp "$(command -v ffprobe)" "$FFMPEG_DIR/ffprobe"
        echo "  ffmpeg: コピー完了"
    else
        echo "  ffmpeg が見つかりません。Homebrew でインストールします..."
        echo "  ※ Homebrew 未インストールの場合は https://brew.sh を参照してください"
        brew install ffmpeg
        cp "$(command -v ffmpeg)"  "$FFMPEG_DIR/ffmpeg"
        cp "$(command -v ffprobe)" "$FFMPEG_DIR/ffprobe"
        echo "  ffmpeg: インストール・コピー完了"
    fi
fi

echo ""
echo "=== [3/4] 依存パッケージを確認 ==="
pip install --quiet pyinstaller Pillow yt-dlp PySide6 pydub demucs

# Apple Silicon (M1/M2/M3) は MPS バックエンドの torch を使用可能
ARCH=$(uname -m)
if [ "$ARCH" = "arm64" ]; then
    echo "  Apple Silicon 検出 — MPS 対応 torch をインストールします..."
    pip install --quiet torch torchaudio
else
    echo "  Intel Mac 検出 — CPU 版 torch をインストールします..."
    pip install --quiet torch torchaudio
fi
echo "  依存パッケージ: OK"

echo ""
echo "=== [4/4] PyInstaller でビルド ==="
python -m pyinstaller youtube_dl_mac.spec --noconfirm

echo ""
echo "=== ビルド完了 ==="
echo "  出力先: dist/YoutubeAudioDownloader/"
echo "  アプリ: dist/YoutubeAudioDownloader.app  (またはフォルダ内の実行ファイル)"
echo ""
echo "  実行方法:"
echo "    open dist/YoutubeAudioDownloader.app"
echo "    または: ./dist/YoutubeAudioDownloader/YoutubeAudioDownloader"
echo ""
echo "  ※ 初回起動時に「開発元を確認できない」ダイアログが出る場合:"
echo "    システム設定 > プライバシーとセキュリティ > 「このまま開く」をクリック"
echo "    または: xattr -cr dist/YoutubeAudioDownloader.app"
