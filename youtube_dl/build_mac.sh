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

ARCH=$(uname -m)
if [ -f "$FFMPEG_DIR/ffmpeg" ] && [ -f "$FFMPEG_DIR/ffprobe" ]; then
    echo "  ffmpeg: 既に存在します。スキップします。"
else
    if [ "$ARCH" = "arm64" ]; then
        echo "  Apple Silicon 検出 — Homebrew の arm64 バイナリを使用します..."
        brew install ffmpeg 2>/dev/null || true
        cp "$(brew --prefix)/bin/ffmpeg"  "$FFMPEG_DIR/ffmpeg"
        cp "$(brew --prefix)/bin/ffprobe" "$FFMPEG_DIR/ffprobe"
        echo "  ffmpeg: Homebrew バイナリをコピー完了"
    else
        echo "  Intel Mac 検出 — evermeet.cx から静的ビルドをダウンロード中..."
        curl -L "https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip"  -o /tmp/ffmpeg.zip
        curl -L "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip" -o /tmp/ffprobe.zip
        unzip -o /tmp/ffmpeg.zip  -d "$FFMPEG_DIR/"
        unzip -o /tmp/ffprobe.zip -d "$FFMPEG_DIR/"
        echo "  ffmpeg: ダウンロード完了 (静的ビルド)"
    fi
    chmod +x "$FFMPEG_DIR/ffmpeg" "$FFMPEG_DIR/ffprobe"
fi

echo ""
echo "=== [3/4] 依存パッケージを確認 ==="
pip install --quiet pyinstaller Pillow "yt-dlp[default]" PySide6 pydub demucs

# Apple Silicon (M1/M2/M3) は MPS バックエンドの torch を使用可能
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
