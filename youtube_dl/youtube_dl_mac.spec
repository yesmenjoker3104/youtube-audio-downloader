# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for YouTube Audio Downloader — macOS 版
ビルド方法: build_mac.sh を実行してください。
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

# soundfile
soundfile_datas, soundfile_binaries, soundfile_hiddenimports = collect_all('soundfile')

# プロジェクト内 utils (demucs/utils からコピー済み)
_local_utils = str(Path(SPECPATH) / 'utils')

# macOS 用 ffmpeg バイナリ (build_mac.sh が ffmpeg_mac/ へ配置)
_ffmpeg_dir = str(Path(SPECPATH) / 'ffmpeg_mac')

a = Analysis(
    ['app.py'],
    pathex=[str(Path(SPECPATH))],
    binaries=[
        # macOS 用 ffmpeg バイナリ
        (str(Path(_ffmpeg_dir) / 'ffmpeg'),  '.'),
        (str(Path(_ffmpeg_dir) / 'ffprobe'), '.'),
        *yt_dlp_binaries,
        *torch_binaries,
        *torchaudio_binaries,
        *demucs_binaries,
        *soundfile_binaries,
    ],
    datas=[
        ('icon.png', '.'),
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
        'torchcodec',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='YoutubeAudioDownloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # macOS では UPX が動作しないため無効
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,    # None = ビルド環境に合わせる (x86_64 or arm64)
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.png',     # macOS は .icns または .png
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='YoutubeAudioDownloader',
)

# macOS .app バンドルを生成
app = BUNDLE(
    coll,
    name='YoutubeAudioDownloader.app',
    icon='icon.png',
    bundle_identifier='com.local.youtubeaudiodownloader',
    info_plist={
        'CFBundleDisplayName': 'YouTube Audio Downloader',
        'CFBundleShortVersionString': '1.0.0',
        'NSHighResolutionCapable': True,
        'NSMicrophoneUsageDescription': 'This app does not use the microphone.',
    },
)
