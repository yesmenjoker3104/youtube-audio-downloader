param(
    [string]$Destination = (Join-Path $PSScriptRoot 'release_runtime\YoutubeAudioDownloader')
)

$ErrorActionPreference = 'Stop'
$Repository = 'yesmenjoker3104/youtube-audio-downloader'
$Release = 'dev-latest'
$Asset = 'YoutubeAudioDownloader-Windows.zip'
$ZipPath = Join-Path $env:TEMP $Asset

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw 'GitHub CLI (gh) was not found. Install it from https://cli.github.com/.'
}

Write-Host 'Downloading the Windows release ZIP from GitHub...' -ForegroundColor Cyan
gh release download $Release --repo $Repository --pattern $Asset --clobber --output $ZipPath
if ($LASTEXITCODE -ne 0) {
    throw 'Release ZIP download failed. Check gh auth status and the release asset.'
}

$Parent = Split-Path -Parent $Destination
New-Item -ItemType Directory -Force -Path $Parent | Out-Null
$ExtractPath = Join-Path $env:TEMP 'YoutubeAudioDownloader-Windows-extract'
if (Test-Path $ExtractPath) {
    Remove-Item $ExtractPath -Recurse -Force
}
Expand-Archive -Path $ZipPath -DestinationPath $ExtractPath -Force

if (Test-Path $Destination) {
    Remove-Item $Destination -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
Copy-Item (Join-Path $ExtractPath '*') $Destination -Recurse -Force

$CliPath = Join-Path $Destination 'YoutubeAudioDownloaderCli.exe'
$RuntimePath = Join-Path $Destination '_internal\python311.dll'
if (-not (Test-Path $CliPath) -or -not (Test-Path $RuntimePath)) {
    throw "Release folder validation failed: $Destination"
}

Remove-Item $ZipPath -Force -ErrorAction SilentlyContinue
Remove-Item $ExtractPath -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Release folder is ready: $Destination" -ForegroundColor Green
Write-Host 'No build is required. Keep every file in the release folder.' -ForegroundColor Yellow