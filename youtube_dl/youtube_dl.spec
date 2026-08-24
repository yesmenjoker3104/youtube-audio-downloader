# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for YouTube Audio Downloader
生成方法: build.ps1 を実行してください。
"""

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules
import sys
from pathlib import Path

# yt-dlp
yt_dlp_datas, yt_dlp_binaries, yt_dlp_hiddenimports = collect_all('yt_dlp')

# certifi (SSL証明書)
certifi_datas, _, _ = collect_all('certifi')

# demucs
demucs_datas, demucs_binaries, demucs_hiddenimports = collect_all('demucs')

# torch / torchaudio
torch_datas, torch_binaries, torch_hiddenimports = collect_all('torch')
torchaudio_datas, torchaudio_binaries, torchaudio_hiddenimports = collect_all('torchaudio')

# soundfile (torchcodec の代替保存バックエンド)
soundfile_datas, soundfile_binaries, soundfile_hiddenimports = collect_all('soundfile')

# プロジェクト内 utils (demucs/utils からコピー済み)
_local_utils = str(Path(SPECPATH) / 'utils')

a = Analysis(
    ['app.py', 'cli.py'],
    pathex=[str(Path(SPECPATH))],
    binaries=[
        # ffmpeg / ffprobe バイナリ (build.ps1 が ffmpeg/ へ配置)
        ('ffmpeg/ffmpeg.exe',  '.'),
        ('ffmpeg/ffprobe.exe', '.'),
        *yt_dlp_binaries,
        *torch_binaries,
        *torchaudio_binaries,
        *demucs_binaries,
        *soundfile_binaries,
    ],
    datas=[
        ('icon.png', '.'),
        # プロジェクト内 utils をバンドル (separator.py 等)
        (_local_utils, 'utils'),
        *yt_dlp_datas,
        *certifi_datas,
        *demucs_datas,
        *torch_datas,
        *torchaudio_datas,
        *soundfile_datas,
    ],
    hiddenimports=[
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        'PIL._tkinter_finder',
        'utils.separator',
        'demucs',
        'demucs.separate',
        'demucs.pretrained',
        'demucs.apply',
        'demucs.htdemucs',
        'demucs.hdemucs',
        'demucs.states',
        'demucs.repo',
        'torch',
        'torchaudio',
        'soundfile',
        *soundfile_hiddenimports,
        *yt_dlp_hiddenimports,
        *demucs_hiddenimports,
        *torch_hiddenimports,
        *torchaudio_hiddenimports,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'notebook', 'IPython',
        'tkinter', '_tkinter',
        'torchcodec',  # FFmpeg 共有 DLL が必要だが同效しないため除外。soundfile で代替。
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    [script for script in a.scripts if script[0] == 'app'],
    [],
    exclude_binaries=True,
    name='YoutubeAudioDownloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,          # CPU 版 torch は UPX 圧縮可
    console=False,       # GUI アプリ — コンソール非表示
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',
)

cli_exe = EXE(
    pyz,
    [script for script in a.scripts if script[0] == 'cli'],
    [],
    exclude_binaries=True,
    name='YoutubeAudioDownloaderCli',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',
)

coll = COLLECT(
    exe,
    cli_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='YoutubeAudioDownloader',
)
