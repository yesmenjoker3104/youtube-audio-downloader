# YouTube Audio Downloader Windows exe build script
# venv を有効化した状態で実行:
#   cd youtube_dl
#   ..\.game_python\Scripts\Activate.ps1
#   .\build.ps1

$ScriptDir = $PSScriptRoot

# 1. PyInstaller
Write-Host ""
Write-Host "[1/4] PyInstaller を確認..." -ForegroundColor Cyan
pip show pyinstaller 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  PyInstaller をインストールします..." -ForegroundColor Yellow
    pip install pyinstaller
} else {
    Write-Host "  PyInstaller: OK" -ForegroundColor Green
}

# 2. ffmpeg バイナリ取得
Write-Host ""
Write-Host "[2/4] ffmpeg バイナリを確認..." -ForegroundColor Cyan
$FfmpegDir  = Join-Path $ScriptDir 'ffmpeg'
$FfmpegExe  = Join-Path $FfmpegDir 'ffmpeg.exe'
$FfprobeExe = Join-Path $FfmpegDir 'ffprobe.exe'

if ((Test-Path $FfmpegExe) -and (Test-Path $FfprobeExe)) {
    Write-Host "  ffmpeg: 既に存在します。スキップします。" -ForegroundColor Green
} else {
    New-Item -ItemType Directory -Force -Path $FfmpegDir | Out-Null
    $ZipUrl  = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip'
    $ZipPath = Join-Path $env:TEMP 'ffmpeg_dl.zip'
    Write-Host "  ffmpeg をダウンロード中..." -ForegroundColor Yellow
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $ZipUrl -OutFile $ZipPath -UseBasicParsing
    Write-Host "  展開中..." -ForegroundColor Yellow
    $ExtractDir = Join-Path $env:TEMP 'ffmpeg_extract'
    if (Test-Path $ExtractDir) { Remove-Item $ExtractDir -Recurse -Force }
    Expand-Archive -Path $ZipPath -DestinationPath $ExtractDir
    $BinDir = Get-ChildItem -Path $ExtractDir -Recurse -Filter 'ffmpeg.exe' |
              Select-Object -First 1 |
              ForEach-Object { $_.DirectoryName }
    if (-not $BinDir) {
        Write-Error "ffmpeg.exe が見つかりませんでした。手動で ffmpeg/ フォルダに配置してください。"
        exit 1
    }
    Copy-Item (Join-Path $BinDir 'ffmpeg.exe')  $FfmpegDir
    Copy-Item (Join-Path $BinDir 'ffprobe.exe') $FfmpegDir
    Remove-Item $ZipPath    -Force -ErrorAction SilentlyContinue
    Remove-Item $ExtractDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "  ffmpeg: ダウンロード完了" -ForegroundColor Green
}

# 3. 依存パッケージ
Write-Host ""
Write-Host "[3/4] 依存パッケージを確認..." -ForegroundColor Cyan
pip install --quiet pyinstaller Pillow yt-dlp PySide6 pydub demucs
pip install --quiet torch torchaudio --index-url https://download.pytorch.org/whl/cpu
Write-Host "  依存パッケージ: OK" -ForegroundColor Green

# 4. icon.ico を生成（Windows タスクバー用）
Write-Host ""
Write-Host "[4/5] icon.ico を生成中..." -ForegroundColor Cyan
Set-Location $ScriptDir
python -c @"
from PIL import Image
import pathlib
img = Image.open('icon.png').convert('RGBA')
sizes = [(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)]
imgs = [img.resize(s, Image.LANCZOS) for s in sizes]
imgs[0].save('icon.ico', format='ICO', sizes=sizes, append_images=imgs[1:])
print('icon.ico: OK')
"@
if ($LASTEXITCODE -ne 0) {
    Write-Error "icon.ico の生成に失敗しました。"
    exit 1
}
Write-Host "  icon.ico: 生成完了" -ForegroundColor Green

# 5. PyInstaller ビルド
Write-Host ""
Write-Host "[5/5] PyInstaller ビルドを開始..." -ForegroundColor Cyan
Set-Location $ScriptDir
if (Test-Path 'dist')  {
    # 実行中の EXE がファイルをロックしている場合は先に終了させる
    Get-Process -Name 'YoutubeAudioDownloader' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    Remove-Item 'dist'  -Recurse -Force
}
if (Test-Path 'build') { Remove-Item 'build' -Recurse -Force }
# 古い .pyc キャッシュを削除してビルドに反映されるようにする
Get-ChildItem -Path (Join-Path $ScriptDir '..\demucs') -Filter '__pycache__' -Recurse -Directory -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

pyinstaller youtube_dl.spec --noconfirm --clean

if ($LASTEXITCODE -ne 0) {
    Write-Error "ビルドに失敗しました。"
    exit 1
}

Write-Host ""
Write-Host "========================================"  -ForegroundColor Green
Write-Host "  ビルド完了!"                             -ForegroundColor Green
Write-Host "  出力先: $ScriptDir\dist\YoutubeAudioDownloader\" -ForegroundColor Green
Write-Host "  YoutubeAudioDownloader.exe を実行してください。" -ForegroundColor Green
Write-Host ""
Write-Host "  ※ 音源分離モデル (htdemucs_6s) は初回使用時に"    -ForegroundColor Yellow
Write-Host "     インターネットから自動ダウンロードされます (~400MB)。" -ForegroundColor Yellow
Write-Host "========================================"  -ForegroundColor Green
Write-Host ""
