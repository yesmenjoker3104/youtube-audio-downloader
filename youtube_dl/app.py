#!/usr/bin/env python3
"""YouTube Audio Downloader

YouTube 動画から音声を抽出して MP3 ファイルに保存するアプリ。

依存:
  pip install PySide6 yt-dlp
  ffmpeg を PATH に追加 (https://ffmpeg.org/)
"""

import sys
import os
import re
import json
import time
import threading
import shutil
import traceback
import logging
import urllib.request
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel, QTableWidget, QTableWidgetItem,
    QFileDialog, QMessageBox, QHeaderView, QAbstractItemView,
    QProgressBar, QCheckBox, QGroupBox, QSpinBox, QTabWidget, QMenu,
)
from PySide6.QtCore import Qt, Signal, QObject, QRunnable, QThreadPool, Slot
from PySide6.QtGui import QPixmap, QColor, QKeySequence, QIcon

# yt_dlp は起動時ではなく各ワーカーの run() 内で遅延インポートする（起動時間短縮）

# ── frozen (PyInstaller) / 開発時 共通パス解決 ──────────────────────────────
_IS_FROZEN  = getattr(sys, 'frozen', False)
_BASE_DIR   = Path(sys.executable).parent if _IS_FROZEN else Path(__file__).parent

# macOS PyInstaller バンドルではシステムの SSL 証明書が参照できないため
# certifi の証明書を明示的に設定する（torch.hub のモデルダウンロードに必要）
if _IS_FROZEN:
    try:
        import certifi as _certifi
        os.environ.setdefault('SSL_CERT_FILE',      _certifi.where())
        os.environ.setdefault('REQUESTS_CA_BUNDLE', _certifi.where())
    except ImportError:
        pass

# MPS (Apple Silicon) は一部の演算（出力チャンネル > 65536 の Conv 等）が未対応のため
# 未対応の演算のみ CPU にフォールバックさせる
os.environ.setdefault('PYTORCH_ENABLE_MPS_FALLBACK', '1')

# frozen EXE (console=False) では子プロセス（ffmpeg 等）の DOS ウィンドウを抑制する
if _IS_FROZEN and sys.platform == 'win32':
    import subprocess as _sp
    _orig_Popen = _sp.Popen
    class _NoWindowPopen(_orig_Popen):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault('creationflags', 0)
            kwargs['creationflags'] |= 0x08000000  # CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)
    _sp.Popen = _NoWindowPopen
_BUNDLE_DIR = Path(sys._MEIPASS) if _IS_FROZEN else Path(__file__).parent

_log = logging.getLogger(__name__)
_log.addHandler(logging.NullHandler())

# macOS frozen: App Translocation で .app バンドルが読み取り専用になるため
# 書き込みが必要なデータは ~/Library/Application Support/YoutubeAudioDownloader/ に保存する
if _IS_FROZEN and sys.platform == 'darwin':
    _USER_DATA_DIR = Path.home() / 'Library' / 'Application Support' / 'YoutubeAudioDownloader'
else:
    _USER_DATA_DIR = _BASE_DIR
_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

_models_dir = str(_USER_DATA_DIR / 'models')
os.makedirs(_models_dir, exist_ok=True)
os.environ.setdefault('TORCH_HOME', _models_dir)

# ── demucs 連携 ───────────────────────────────────────────────────────────────
if not _IS_FROZEN:
    _DEMUCS_DIR = Path(__file__).parent.parent / 'demucs'
    if _DEMUCS_DIR.is_dir() and str(_DEMUCS_DIR) not in sys.path:
        sys.path.insert(0, str(_DEMUCS_DIR))

_DEMUCS_AVAILABLE = False
_DEMUCS_IMPORT_ERROR = ''
try:
    if _IS_FROZEN:
        # frozen EXE: utils/__init__.py の連鎖 import を完全回避するため
        # _MEIPASS/utils/separator.py を importlib で直接ロードする
        import importlib.util as _ilu
        _sep_path = _BUNDLE_DIR / 'utils' / 'separator.py'
        _sep_spec = _ilu.spec_from_file_location('utils.separator', str(_sep_path))
        _sep_mod  = _ilu.module_from_spec(_sep_spec)
        sys.modules['utils.separator'] = _sep_mod
        _sep_spec.loader.exec_module(_sep_mod)
        _AudioSeparator = _sep_mod.AudioSeparator
    else:
        from utils.separator import AudioSeparator as _AudioSeparator
    _DEMUCS_AVAILABLE = True
except Exception as _e:
    import traceback as _tb
    _DEMUCS_IMPORT_ERROR = _tb.format_exc()

# ──────────────────────────────────────────────────────────────────────────────
MAX_ENTRIES = 1000
ROW_H       = 80
THUMB_W     = 120
THUMB_H     = 68
DATA_FILE   = _USER_DATA_DIR / "download_list.json"
# ──────────────────────────────────────────────────────────────────────────────


def _find_bundled_ffmpeg() -> 'Path | None':
    """バンドルされた ffmpeg バイナリのパスを返す。Windows/macOS 両対応。
    見つからなければ None を返し、yt-dlp が PATH から探す。"""
    for name in ('ffmpeg.exe', 'ffmpeg'):
        p = _BUNDLE_DIR / name
        if p.exists():
            return p
    return None


def _open_path_in_explorer(path: str) -> None:
    """OS に応じてファイルマネージャーでパスを開く。"""
    import subprocess as _sp
    if sys.platform == 'win32':
        os.startfile(path)
    elif sys.platform == 'darwin':
        _sp.Popen(['open', path])
    else:
        _sp.Popen(['xdg-open', path])


class AppSignals(QObject):
    """スレッド間通信用シグナル (全ワーカーで共有)"""
    info_ready   = Signal(str, str, str)         # url, title, thumb_url
    thumb_data   = Signal(str, bytes)            # url, 画像バイト列
    dl_progress  = Signal(str, str, float)       # url, テキスト, 進捗%(0-100, -1=不定)
    dl_done      = Signal(str, str)              # url, mp3_path
    dl_error     = Signal(str, str)              # url, エラーメッセージ
    sep_progress = Signal(str, str)              # url, テキスト
    sep_done     = Signal(str)                   # url
    sep_error    = Signal(str, str)              # url, エラーメッセージ


# ── Workers ───────────────────────────────────────────────────────────────────

class InfoWorker(QRunnable):
    """動画タイトル・サムネイル URL を非同期取得"""
    def __init__(self, url: str, sig: AppSignals):
        super().__init__()
        self.url, self.sig = url, sig
        self.setAutoDelete(True)

    def run(self):
        try:
            import yt_dlp  # noqa: PLC0415 – 遅延インポート（起動時間短縮）
            opts = {
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
                'noplaylist': True,          # プレイリスト URL でも単体動画を取得
                'extract_flat': False,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=False)

            # プレイリスト情報が返った場合は先頭エントリを使う
            if info.get('_type') == 'playlist':
                entries = info.get('entries') or []
                info = entries[0] if entries else info

            title = info.get('title', 'Unknown')

            # サムネイルは jpg/png 優先で選ぶ（webp は Qt 環境依存のため後回し）
            thumb_url = ''
            thumbnails = info.get('thumbnails') or []
            # preference 付きのものを降順ソートして最良を選ぶ
            sorted_thumbs = sorted(
                thumbnails,
                key=lambda t: (t.get('preference') or 0, t.get('width') or 0),
                reverse=True,
            )
            for t in sorted_thumbs:
                u = t.get('url', '')
                if any(u.lower().endswith(ext) for ext in ('.jpg', '.jpeg', '.png')):
                    thumb_url = u
                    break
            # jpg/png が見つからなければ webp でも使う
            if not thumb_url:
                for t in sorted_thumbs:
                    u = t.get('url', '')
                    if u:
                        thumb_url = u
                        break
            if not thumb_url:
                thumb_url = info.get('thumbnail', '')

            self.sig.info_ready.emit(self.url, title, thumb_url)
        except Exception as e:
            self.sig.info_ready.emit(self.url, f'[取得エラー: {str(e)[:80]}]', '')


class ThumbWorker(QRunnable):
    """サムネイル画像を非同期ダウンロード (バイト列でメインスレッドへ送る)"""
    def __init__(self, url: str, thumb_url: str, sig: AppSignals):
        super().__init__()
        self.url, self.thumb_url, self.sig = url, thumb_url, sig
        self.setAutoDelete(True)

    def run(self):
        try:
            req = urllib.request.Request(
                self.thumb_url,
                headers={'User-Agent': 'Mozilla/5.0'},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
                content_type = resp.headers.get('Content-Type', '')

            # webp は Qt の loadFromData が失敗する場合があるので
            # Pillow で PNG 変換を試みる（インストール済みの場合）
            if 'webp' in content_type or self.thumb_url.lower().endswith('.webp'):
                try:
                    import io
                    from PIL import Image
                    img = Image.open(io.BytesIO(data)).convert('RGB')
                    buf = io.BytesIO()
                    img.save(buf, format='PNG')
                    data = buf.getvalue()
                except Exception:
                    pass  # Pillow 未導入なら変換せずそのまま試す

            self.sig.thumb_data.emit(self.url, data)
        except Exception:
            pass  # サムネイル取得失敗は無視


class DownloadWorker(QRunnable):
    """yt-dlp で音声抽出 → MP3 保存"""
    def __init__(self, url: str, out_dir: str, use_subdir: bool, sig: AppSignals,
                 keep_video: bool = False, allow_playlist: bool = False):
        super().__init__()
        self.url, self.out_dir, self.use_subdir, self.sig = url, out_dir, use_subdir, sig
        self.keep_video = keep_video
        self.allow_playlist = allow_playlist
        self.setAutoDelete(True)

    def run(self):
        url = self.url

        def hook(d):
            if d['status'] == 'downloading':
                pct_str = (d.get('_percent_str') or '').strip()
                try:
                    dl  = float(d.get('downloaded_bytes') or 0)
                    tot = float(
                        d.get('total_bytes') or
                        d.get('total_bytes_estimate') or 0
                    )
                    pct_val = (dl / tot * 100) if tot > 0 else 0.0
                except (TypeError, ValueError):
                    pct_val = 0.0
                self.sig.dl_progress.emit(url, pct_str or '...', pct_val)
            elif d['status'] == 'finished':
                self.sig.dl_progress.emit(url, '変換中...', -1.0)

        final_path: list = ['']

        import yt_dlp  # noqa: PLC0415 – 遅延インポート（起動時間短縮）

        if self.keep_video:
            # ── 動画ダウンロードモード (MP4) ────────────────────────────────────
            # keepvideo を使わず MP4 をダウンロードし、完了後に ffmpeg で MP3 を抽出。
            # keepvideo: True だとマージ前の中間ファイル (.m4a / 映像のみ .mp4) も
            # すべて残ってしまうため、この方式で MP4 + MP3 のみを生成する。
            mp4_path: list = ['']

            def pp_hook(d):
                if d.get('status') == 'finished':
                    fp = (d.get('info_dict') or {}).get('filepath', '')
                    if fp and Path(fp).suffix.lower() == '.mp4':
                        mp4_path[0] = fp

            opts = {
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',
                'merge_output_format': 'mp4',
                'postprocessors': [],
                # 常にタイトル名のサブフォルダ内に保存
                'outtmpl': str(Path(self.out_dir) / '%(title)s' / '%(title)s.%(ext)s'),
                'progress_hooks': [hook],
                'postprocessor_hooks': [pp_hook],
                'quiet': True,
                'no_warnings': True,
                'noplaylist': not self.allow_playlist,
                'sleep_interval_requests': 2,
                'sleep_interval': 1,
                'max_sleep_interval': 5,
            }
            _ffmpeg = _find_bundled_ffmpeg()
            if _ffmpeg:
                opts['ffmpeg_location'] = str(_BUNDLE_DIR)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
            except Exception:
                self.sig.dl_error.emit(url, traceback.format_exc())
                return

            # MP4 から MP3 を ffmpeg で抽出
            mp4 = Path(mp4_path[0]) if mp4_path[0] else None
            if mp4 and mp4.exists():
                self.sig.dl_progress.emit(url, 'MP3 抽出中...', -1.0)
                mp3 = mp4.with_suffix('.mp3')
                ffmpeg_exe = str(_ffmpeg) if _ffmpeg else 'ffmpeg'
                try:
                    import subprocess as _sp
                    _sp.run(
                        [ffmpeg_exe, '-y', '-i', str(mp4),
                         '-vn', '-ab', '192k', '-ar', '44100', str(mp3)],
                        check=True,
                        stdout=_sp.DEVNULL,
                        stderr=_sp.DEVNULL,
                    )
                    final_path[0] = str(mp3)
                except Exception:
                    # MP3 抽出失敗は非致命的。MP4 だけでも完了扱いにする
                    _log.warning('MP3 extraction failed: %s', traceback.format_exc())
            self.sig.dl_done.emit(url, final_path[0])
            return
        else:
            # ── 音声ダウンロードモード (MP3) ──────────────────────────────────
            def pp_hook(d):  # noqa: F811
                if d.get('status') == 'finished':
                    fp = (d.get('info_dict') or {}).get('filepath', '')
                    if fp and Path(fp).suffix.lower() == '.mp3':
                        final_path[0] = fp

            opts = {
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'outtmpl': str(
                    Path(self.out_dir) / '%(title)s' / '%(title)s.%(ext)s'
                    if self.use_subdir else
                    Path(self.out_dir) / '%(title)s.%(ext)s'
                ),
                'progress_hooks': [hook],
                'postprocessor_hooks': [pp_hook],
                'quiet': True,
                'no_warnings': True,
                'noplaylist': not self.allow_playlist,
                'sleep_interval_requests': 2,
                'sleep_interval': 1,
                'max_sleep_interval': 5,
            }

        try:
            # PyInstaller バンドル時は同梱 ffmpeg を使用
            if _find_bundled_ffmpeg():
                opts['ffmpeg_location'] = str(_BUNDLE_DIR)
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            # 動画モードでも MP3 パスを渡す（音源分離が選択されていれば自動実行される）
            self.sig.dl_done.emit(url, final_path[0])
        except Exception as e:
            self.sig.dl_error.emit(url, traceback.format_exc())


# ── demucs 進捗テキストパーサー ─────────────────────────────────────────────────
# 例: "3.81M/381M" "56.9MB/380MB" "0.00B/381M" など
_RE_DL_SIZE = re.compile(
    r'(\d+(?:\.\d+)?)\s*([KkMmGg])[iI]?[Bb]?/(\d+(?:\.\d+)?)\s*([KkMmGg])[iI]?[Bb]?'
)
_RE_PCT = re.compile(r'(\d+)%\|')


def _parse_demucs_line(line: str):
    """demucs/torch stderr 1行からUI用進捗テキストを返す。表示不要なら None。"""
    if 'Downloading' in line and ('http' in line or 'fbaipublicfiles' in line):
        return 'モデルをDL中... (~400MB) 接続中...'
    if 'Connecting' in line or 'connecting' in line:
        return 'モデルサーバーに接続中...'
    if 'Separating' in line or 'applying model' in line.lower():
        return 'AI分離処理中 (時間がかかります...)'
    m_size = _RE_DL_SIZE.search(line)
    if m_size:
        cur, cu = float(m_size.group(1)), m_size.group(2).upper()
        tot, tu = float(m_size.group(3)), m_size.group(4).upper()
        m_pct = _RE_PCT.search(line)
        pct = int(m_pct.group(1)) if m_pct else min(int(cur / tot * 100), 100)
        return f'モデルDL中... {cur:.0f}{cu}B/{tot:.0f}{tu}B ({pct}%)'
    m_pct = _RE_PCT.search(line)
    if m_pct and ('it/s' in line or 's/it' in line or 'sep' in line.lower()):
        return f'分離処理中... {m_pct.group(1)}%'
    return None


# ── Separate Worker ──────────────────────────────────────────────────────────

class SeparateWorker(QRunnable):
    """demucs で音源分離 → MP3 と同じフォルダに各パートを MP3 として出力"""
    def __init__(self, url: str, mp3_path: str, stems: list, sig: AppSignals):
        super().__init__()
        self.url       = url
        self.mp3_path  = mp3_path
        self.stems     = stems
        self.sig       = sig
        self.setAutoDelete(True)

    def run(self):
        url = self.url
        sep_tmp_dir = str(Path(self.mp3_path).parent / '_sep_tmp')
        try:
            if not _DEMUCS_AVAILABLE:
                raise RuntimeError(
                    f'demucs ライブラリが読み込めませんでした。\n{_DEMUCS_IMPORT_ERROR}'
                )
            if not Path(self.mp3_path).exists():
                raise RuntimeError(
                    f'MP3 ファイルが見つかりません:\n{self.mp3_path}'
                )

            self.sig.sep_progress.emit(url, '音源分離中...')

            # MP3 と同じフォルダへ出力する
            final_dir   = Path(self.mp3_path).parent
            sep_tmp_dir = str(final_dir / '_sep_tmp')

            # モデルキャッシュの有無を確認してメッセージを切り替え
            _ckpt_dir = Path(os.environ.get(
                'TORCH_HOME', str(Path.home() / '.cache' / 'torch')
            )) / 'hub' / 'checkpoints'
            if not (_ckpt_dir.exists() and any(_ckpt_dir.glob('*.th'))):
                self.sig.sep_progress.emit(url, 'モデルをDL中... (初回のみ ~400MB、回線によっては数十分かかります)')

            sep = _AudioSeparator(model='htdemucs_6s')

            def _cb(line: str, _url=url):
                txt = _parse_demucs_line(line)
                if txt:
                    self.sig.sep_progress.emit(_url, txt)

            # ハートビート: tqdm 更新がない長時間処理中でもUIに経過時間を表示
            _hb_stop = threading.Event()
            _hb_start = time.monotonic()

            def _heartbeat(_u=url, _e=_hb_stop):
                while not _e.wait(20):
                    elapsed = int(time.monotonic() - _hb_start)
                    self.sig.sep_progress.emit(
                        _u, f'AI分離処理中... ({elapsed // 60}分{elapsed % 60}秒経過)')

            _hb_thread = threading.Thread(target=_heartbeat, daemon=True)
            _hb_thread.start()
            try:
                wav_stems = sep.separate(self.mp3_path, sep_tmp_dir, self.stems, progress_callback=_cb)
            finally:
                _hb_stop.set()
                _hb_thread.join(timeout=2)

            try:
                from pydub import AudioSegment  # type: ignore
            except ImportError:
                raise RuntimeError(
                    'pydub が見つかりません。pip install pydub を実行してください。'
                )

            for stem_name, wav_path in wav_stems.items():
                if not wav_path or not Path(wav_path).exists():
                    continue
                self.sig.sep_progress.emit(url, f'MP3 変換中: {stem_name}')
                original_stem = Path(self.mp3_path).stem
                mp3_out = str(final_dir / f'【{stem_name}】{original_stem}.mp3')
                audio = AudioSegment.from_file(wav_path)
                audio.export(mp3_out, format='mp3', bitrate='192k')

            self.sig.sep_done.emit(url)
        except Exception:
            tb = traceback.format_exc()
            _log.error('SeparateWorker\n%s', tb)
            self.sig.sep_error.emit(url, tb)
        finally:
            shutil.rmtree(sep_tmp_dir, ignore_errors=True)


# ── Status Cell Widget ───────────────────────────────────────────────────────

class StatusWidget(QWidget):
    """テーブルの状態列に埋め込むカスタムセルウィジェット"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self.lbl = QLabel("—")
        self.lbl.setAlignment(Qt.AlignCenter)
        self.lbl.setStyleSheet("color: #585b70; font-size: 12px;")

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(True)
        self.bar.setFixedHeight(14)
        self.bar.setStyleSheet("""
            QProgressBar {
                background: #313244;
                border: 1px solid #45475a;
                border-radius: 3px;
                color: #1e1e2e;
                font-size: 10px;
            }
            QProgressBar::chunk { background: #89b4fa; border-radius: 2px; }
        """)
        self.bar.hide()

        layout.addStretch()
        layout.addWidget(self.lbl)
        layout.addWidget(self.bar)
        layout.addStretch()

    def set_idle(self):
        self.lbl.setText("—")
        self.lbl.setStyleSheet("color: #585b70; font-size: 12px;")
        self.lbl.setToolTip("")
        self.bar.hide()

    def set_waiting(self):
        self.lbl.setText("⏳  待機中")
        self.lbl.setStyleSheet("color: #cba6f7; font-size: 12px;")
        self.bar.hide()

    def set_downloading(self, pct: float, text: str):
        self.lbl.setText(f"⬇  {text}")
        self.lbl.setStyleSheet("color: #f9e2af; font-size: 12px;")
        self.bar.setRange(0, 100)
        self.bar.setValue(max(0, min(100, int(pct))))
        self.bar.show()

    def set_converting(self):
        self.lbl.setText("🔄  変換中...")
        self.lbl.setStyleSheet("color: #89dceb; font-size: 12px;")
        self.bar.setRange(0, 0)   # indeterminate
        self.bar.show()

    def set_done(self):
        self.lbl.setText("〇  完了")
        self.lbl.setStyleSheet(
            "color: #a6e3a1; font-size: 13px; font-weight: bold;"
        )
        self.lbl.setToolTip("")
        self.bar.hide()

    def set_error(self, msg: str):
        self.lbl.setText("✕  エラー")
        self.lbl.setStyleSheet("color: #f38ba8; font-size: 12px;")
        self.lbl.setToolTip(msg)
        self.bar.hide()

    def set_separating(self, text: str):
        self.lbl.setText(f"🔬  {text}")
        self.lbl.setStyleSheet("color: #cba6f7; font-size: 12px;")
        self.lbl.setToolTip("")
        self.bar.setRange(0, 0)   # indeterminate
        self.bar.show()

    def set_sep_done(self):
        self.lbl.setText("🎵  分離完了")
        self.lbl.setStyleSheet(
            "color: #89dceb; font-size: 13px; font-weight: bold;"
        )
        self.lbl.setToolTip("")
        self.bar.hide()


# ── Local Separation ───────────────────────────────────────────────────────

# ローカル分離タブで受け付ける音声ファイルの拡張子
_AUDIO_EXTENSIONS = {'.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac', '.opus', '.wma'}


class LocalSepSignals(QObject):
    sep_progress = Signal(str, str)   # path, テキスト
    sep_done     = Signal(str)        # path
    sep_error    = Signal(str, str)   # path, エラーメッセージ


class LocalSepWorker(QRunnable):
    """ローカル MP3 ファイルに対して demucs で音源分離"""
    def __init__(self, mp3_path: str, stems: list, out_dir: str, sig: 'LocalSepSignals'):
        super().__init__()
        self.mp3_path = mp3_path
        self.stems    = stems
        self.out_dir  = out_dir
        self.sig      = sig
        self.setAutoDelete(True)

    def run(self):
        path        = self.mp3_path
        stem_folder = Path(self.out_dir) / Path(path).stem
        sep_tmp_dir = str(stem_folder / '_sep_tmp')
        try:
            if not _DEMUCS_AVAILABLE:
                raise RuntimeError(
                    f'demucs ライブラリが読み込めませんでした。\n{_DEMUCS_IMPORT_ERROR}'
                )
            if not Path(path).exists():
                raise RuntimeError(f'ファイルが見つかりません:\n{path}')

            self.sig.sep_progress.emit(path, '音源分離中...')
            stem_folder.mkdir(parents=True, exist_ok=True)

            # モデルキャッシュの有無を確認してメッセージを切り替え
            _ckpt_dir = Path(os.environ.get(
                'TORCH_HOME', str(Path.home() / '.cache' / 'torch')
            )) / 'hub' / 'checkpoints'
            if _ckpt_dir.exists() and any(_ckpt_dir.glob('*.th')):
                self.sig.sep_progress.emit(path, '音源分離中...')
            else:
                self.sig.sep_progress.emit(path, 'モデルをDL中... (初回のみ ~400MB、回線によっては数十分かかります)')

            sep = _AudioSeparator(model='htdemucs_6s')

            def _cb(line: str, _path=path):
                txt = _parse_demucs_line(line)
                if txt:
                    self.sig.sep_progress.emit(_path, txt)

            # ハートビート: tqdm 更新がない長時間処理中でもUIに経過時間を表示
            _hb_stop = threading.Event()
            _hb_start = time.monotonic()

            def _heartbeat(_p=path, _e=_hb_stop):
                while not _e.wait(20):
                    elapsed = int(time.monotonic() - _hb_start)
                    self.sig.sep_progress.emit(
                        _p, f'AI分離処理中... ({elapsed // 60}分{elapsed % 60}秒経過)')

            _hb_thread = threading.Thread(target=_heartbeat, daemon=True)
            _hb_thread.start()
            try:
                wav_stems = sep.separate(path, sep_tmp_dir, self.stems, progress_callback=_cb)
            finally:
                _hb_stop.set()
                _hb_thread.join(timeout=2)

            try:
                from pydub import AudioSegment  # type: ignore
            except ImportError:
                raise RuntimeError(
                    'pydub が見つかりません。pip install pydub を実行してください。'
                )

            for stem_name, wav_path in wav_stems.items():
                if not wav_path or not Path(wav_path).exists():
                    continue
                self.sig.sep_progress.emit(path, f'MP3 変換中: {stem_name}')
                original_stem = Path(path).stem
                mp3_out = str(stem_folder / f'【{stem_name}】{original_stem}.mp3')
                audio = AudioSegment.from_file(wav_path)
                audio.export(mp3_out, format='mp3', bitrate='192k')
                try:
                    Path(wav_path).unlink()
                except Exception:
                    pass

            self.sig.sep_done.emit(path)
        except Exception:
            tb = traceback.format_exc()
            _log.error('LocalSepWorker\n%s', tb)
            self.sig.sep_error.emit(path, tb)
        finally:
            shutil.rmtree(sep_tmp_dir, ignore_errors=True)


class LocalSepWidget(QWidget):
    """ローカル MP3 → 音源分離 タブ"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.files:  list = []   # 絶対パスのリスト
        self.states: dict = {}   # path → {done, error, separating, progress, queued}
        self.output_dir   = str(Path.home() / 'Downloads')
        self.sig          = LocalSepSignals()
        self._pool        = QThreadPool()
        self._pool.setMaxThreadCount(3)
        self._pending: set = set()
        self._total        = 0

        self.sig.sep_progress.connect(self._on_progress)
        self.sig.sep_done    .connect(self._on_done)
        self.sig.sep_error   .connect(self._on_error)
        self._build_local_ui()

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build_local_ui(self):
        v = QVBoxLayout(self)
        v.setSpacing(8)
        v.setContentsMargins(12, 12, 12, 12)

        # ファイル追加ボタン行
        h1 = QHBoxLayout()
        btn_files = QPushButton('📄  ファイルを追加...')
        btn_files.setMinimumHeight(36)
        btn_files.clicked.connect(self._add_files_dialog)
        btn_folder = QPushButton('📁  フォルダを追加...')
        btn_folder.setMinimumHeight(36)
        btn_folder.clicked.connect(self._add_folder_dialog)
        h1.addWidget(btn_files)
        h1.addWidget(btn_folder)
        h1.addStretch()
        v.addLayout(h1)

        lbl_dd = QLabel('または、MP3 ファイル ・ フォルダをこの画面にドラッグ＆ドロップでも追加できます')
        lbl_dd.setAlignment(Qt.AlignCenter)
        lbl_dd.setStyleSheet('color: #585b70; font-style: italic; padding: 4px;')
        v.addWidget(lbl_dd)

        # テーブル
        self.tbl = QTableWidget(0, 3)
        self.tbl.setHorizontalHeaderLabels(['ファイル名', 'パス', '状態'])
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.tbl.setColumnWidth(2, 130)
        self.tbl.verticalHeader().setDefaultSectionSize(48)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.tbl.setAcceptDrops(False)
        v.addWidget(self.tbl, 1)

        self.lbl_count = QLabel('登録数: 0')
        v.addWidget(self.lbl_count)

        # 出力先フォルダ行
        h2 = QHBoxLayout()
        self.folder_edit = QLineEdit(self.output_dir)
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setMinimumHeight(32)
        btn_fol = QPushButton('出力先変更...')
        btn_fol.setMinimumHeight(32)
        btn_fol.clicked.connect(self._change_folder)
        btn_open = QPushButton('📁 フォルダを開く')
        btn_open.setMinimumHeight(32)
        btn_open.clicked.connect(self._open_folder)
        h2.addWidget(QLabel('出力先:'))
        h2.addWidget(self.folder_edit, 1)
        h2.addWidget(btn_fol)
        h2.addWidget(btn_open)
        v.addLayout(h2)

        # パーツ選択 + 並列数
        grp = QGroupBox('抽出するパートを選択')
        grp_layout = QHBoxLayout(grp)
        grp_layout.setSpacing(20)
        self._stem_checks: dict = {}
        for key, label in [
            ('vocals', 'Vocal'), ('guitar', 'Guitar'), ('bass', 'Bass'),
            ('drums', 'Drums'), ('piano', 'Piano'), ('other', 'その他'),
        ]:
            cb = QCheckBox(label)
            cb.setChecked(True)
            grp_layout.addWidget(cb)
            self._stem_checks[key] = cb
        grp_layout.addStretch()
        grp_layout.addWidget(QLabel('並列数:'))
        spin = QSpinBox()
        spin.setRange(1, 10)
        spin.setValue(3)
        spin.setFixedWidth(80)
        spin.setMinimumHeight(28)
        spin.setStyleSheet(
            'QSpinBox { background: #313244; color: #cdd6f4; border: 1px solid #45475a;'
            ' border-radius: 4px; padding: 2px 6px; font-size: 14px; }'
            'QSpinBox::up-button, QSpinBox::down-button { width: 18px; }'
        )
        spin.valueChanged.connect(lambda val: self._pool.setMaxThreadCount(val))
        grp_layout.addWidget(spin)
        v.addWidget(grp)

        # ボタン行
        h3 = QHBoxLayout()
        self.btn_start = QPushButton('▶  分離スタート')
        self.btn_start.setMinimumHeight(44)
        self.btn_start.clicked.connect(self._start)
        btn_clear = QPushButton('リストクリア')
        btn_clear.setMinimumHeight(44)
        btn_clear.clicked.connect(self._clear)
        h3.addWidget(self.btn_start)
        h3.addWidget(btn_clear)
        v.addLayout(h3)

    # ── Drag & Drop ───────────────────────────────────────────────────────────
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        added = 0
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if not local:
                continue
            p = Path(local)
            if p.is_dir():
                for af in sorted(p.rglob('*')):
                    if af.suffix.lower() in _AUDIO_EXTENSIONS:
                        self._add_file(str(af))
                        added += 1
            elif p.suffix.lower() in _AUDIO_EXTENSIONS:
                self._add_file(str(p))
                added += 1
        event.acceptProposedAction()

    # ── File management ──────────────────────────────────────────────────────
    def _add_file(self, path: str):
        if path in self.files:
            return
        self.files.append(path)
        self.states[path] = {}
        row = len(self.files) - 1
        self.tbl.insertRow(row)
        self._paint_row(row)
        self.lbl_count.setText(f'登録数: {len(self.files)}')

    def _add_files_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, '音声ファイルを選択', '',
            'Audio Files (*.mp3 *.wav *.flac *.ogg *.m4a *.aac *.opus *.wma);;All Files (*)'
        )
        for p in paths:
            self._add_file(str(Path(p)))

    def _add_folder_dialog(self):
        folder = QFileDialog.getExistingDirectory(self, 'フォルダを選択')
        if folder:
            for af in sorted(Path(folder).rglob('*')):
                if af.suffix.lower() in _AUDIO_EXTENSIONS:
                    self._add_file(str(af))

    def _find_row(self, path: str) -> int:
        try:
            return self.files.index(path)
        except ValueError:
            return -1

    def _paint_row(self, row: int):
        p         = self.files[row]
        name_item = QTableWidgetItem(Path(p).name)
        name_item.setToolTip(p)
        self.tbl.setItem(row, 0, name_item)
        path_item = QTableWidgetItem(p)
        path_item.setToolTip(p)
        self.tbl.setItem(row, 1, path_item)
        self.tbl.setCellWidget(row, 2, StatusWidget())
        self._refresh_status(row)

    def _refresh_status(self, row: int):
        p = self.files[row]
        s = self.states.get(p, {})
        w = self.tbl.cellWidget(row, 2)
        if not isinstance(w, StatusWidget):
            return
        if s.get('done'):
            w.set_sep_done()
        elif s.get('error'):
            w.set_error(s['error'][:80])
        elif s.get('separating'):
            w.set_separating(s.get('progress', '音源分離中...'))
        elif s.get('queued'):
            w.set_waiting()
        else:
            w.set_idle()

    # ── Actions ───────────────────────────────────────────────────────────────
    def _start(self):
        selected = [k for k, cb in self._stem_checks.items() if cb.isChecked()]
        if not selected:
            QMessageBox.warning(self, '選択エラー', '抽出するパートを1つ以上選択してください。')
            return
        if not _DEMUCS_AVAILABLE:
            QMessageBox.critical(self, 'demucs 未インストール',
                'demucs ライブラリが見つかりません。\npip install demucs pydub を実行してください。')
            return
        to_sep = [p for p in self.files if not self.states.get(p, {}).get('done')]
        if not to_sep:
            QMessageBox.information(self, '完了', '処理待ちのアイテムがありません。')
            return
        if not Path(self.output_dir).is_dir():
            QMessageBox.warning(self, 'エラー', f'出力先フォルダが存在しません。\n{self.output_dir}')
            return
        for p in to_sep:
            s = self.states.setdefault(p, {})
            s.pop('error', None)
            s['queued']     = True
            s['separating'] = False
        self._pending = set(to_sep)
        self._total   = len(self._pending)
        self.btn_start.setEnabled(False)
        self.btn_start.setText(f'分離中... (0 / {self._total})')
        for p in to_sep:
            row = self._find_row(p)
            if row >= 0:
                self._refresh_status(row)
            self._pool.start(LocalSepWorker(p, selected, self.output_dir, self.sig))

    def _clear(self):
        if not self.files:
            QMessageBox.information(self, '情報', 'リストは既に空です。')
            return
        reply = QMessageBox.question(
            self, 'リストクリアの確認',
            f'登録されている {len(self.files)} 件を全て削除しますか？',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.files.clear()
            self.states.clear()
            self.tbl.setRowCount(0)
            self.lbl_count.setText('登録数: 0')

    def _change_folder(self):
        folder = QFileDialog.getExistingDirectory(self, '出力先フォルダを選択', self.output_dir)
        if folder:
            self.output_dir = folder
            self.folder_edit.setText(folder)

    def _open_folder(self):
        if Path(self.output_dir).is_dir():
            _open_path_in_explorer(self.output_dir)
        else:
            QMessageBox.warning(self, 'エラー', f'フォルダが存在しません:\n{self.output_dir}')

    # ── Slots ────────────────────────────────────────────────────────────────
    @Slot(str, str)
    def _on_progress(self, path: str, text: str):
        row = self._find_row(path)
        if row < 0:
            return
        s = self.states.setdefault(path, {})
        s['separating'] = True
        s['queued']     = False
        s['progress']   = text
        self._refresh_status(row)

    @Slot(str)
    def _on_done(self, path: str):
        row = self._find_row(path)
        if row >= 0:
            s = self.states.setdefault(path, {})
            s['done']       = True
            s['separating'] = False
            s.pop('error',    None)
            s.pop('progress', None)
            s.pop('queued',   None)
            self._refresh_status(row)
        self._pending.discard(path)
        self._refresh_btn()

    @Slot(str, str)
    def _on_error(self, path: str, msg: str):
        row = self._find_row(path)
        if row >= 0:
            s = self.states.setdefault(path, {})
            s['error']      = msg
            s['separating'] = False
            s.pop('progress', None)
            s.pop('queued',   None)
            self._refresh_status(row)
        self._pending.discard(path)
        self._refresh_btn()

    def _refresh_btn(self):
        if self._pending:
            done = self._total - len(self._pending)
            self.btn_start.setText(f'分離中... ({done} / {self._total})')
        else:
            self.btn_start.setEnabled(True)
            self.btn_start.setText('▶  分離スタート')
            self._total = 0


# ── Main Window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YouTube Audio Downloader")
        self.resize(1100, 720)

        self.entries: list[dict] = []
        self.output_dir: str = str(Path.home() / "Downloads")
        self.sig = AppSignals()

        # スレッドプール — 情報取得用とダウンロード用を分離
        self._pool_misc = QThreadPool()
        self._pool_misc.setMaxThreadCount(6)
        self._pool_dl = QThreadPool()
        self._pool_dl.setMaxThreadCount(3)   # デフォルト並列数

        # ダウンロード進捗管理
        self._dl_pending: set[str] = set()
        self._dl_total = 0

        # 音源分離進捗管理
        self._sep_pending: set[str] = set()
        self._sep_total = 0
        self._pool_sep = QThreadPool()
        self._pool_sep.setMaxThreadCount(3)   # デフォルト並列数

        self.sig.info_ready  .connect(self._on_info_ready)
        self.sig.thumb_data  .connect(self._on_thumb_data)
        self.sig.dl_progress .connect(self._on_dl_progress)
        self.sig.dl_done     .connect(self._on_dl_done)
        self.sig.dl_error    .connect(self._on_dl_error)
        self.sig.sep_progress.connect(self._on_sep_progress)
        self.sig.sep_done    .connect(self._on_sep_done)
        self.sig.sep_error   .connect(self._on_sep_error)

        self._build_ui()
        self._load_data()

    # ── Build UI ───────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._tabs = QTabWidget()
        yt_tab = self._build_yt_tab()
        self._tabs.addTab(yt_tab, "🎬  YouTube ダウンロード")
        self.local_sep_tab = LocalSepWidget()
        self._tabs.addTab(self.local_sep_tab, "🎸  ローカル MP3 音源分離")
        self.setCentralWidget(self._tabs)
        self._apply_style()

    def _build_yt_tab(self) -> QWidget:
        root = QWidget()
        v = QVBoxLayout(root)
        v.setSpacing(8)
        v.setContentsMargins(12, 12, 12, 12)

        # URL 入力行
        h1 = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(
            "YouTube URL を入力して Enter または「追加」ボタンを押してください"
        )
        self.url_edit.setMinimumHeight(36)
        self.url_edit.returnPressed.connect(self._add_url)
        btn_add = QPushButton("追加")
        btn_add.setMinimumHeight(36)
        btn_add.setFixedWidth(80)
        btn_add.clicked.connect(self._add_url)
        h1.addWidget(QLabel("URL:"))
        h1.addWidget(self.url_edit, 1)
        h1.addWidget(btn_add)
        v.addLayout(h1)

        # 出力先フォルダ行
        h2 = QHBoxLayout()
        self.folder_edit = QLineEdit(self.output_dir)
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setMinimumHeight(32)
        btn_fol = QPushButton("出力先変更...")
        btn_fol.setMinimumHeight(32)
        btn_fol.clicked.connect(self._change_folder)
        btn_open = QPushButton("📁 フォルダを開く")
        btn_open.setMinimumHeight(32)
        btn_open.clicked.connect(self._open_folder)
        h2.addWidget(QLabel("出力先:"))
        h2.addWidget(self.folder_edit, 1)
        h2.addWidget(btn_fol)
        h2.addWidget(btn_open)
        v.addLayout(h2)

        # テーブル
        self.tbl = QTableWidget(0, 4)
        self.tbl.setHorizontalHeaderLabels(["サムネイル", "タイトル", "URL", "状態"])
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Fixed)
        self.tbl.setColumnWidth(0, 135)
        self.tbl.setColumnWidth(3, 130)
        self.tbl.verticalHeader().setDefaultSectionSize(ROW_H)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tbl.customContextMenuRequested.connect(self._show_context_menu)
        v.addWidget(self.tbl, 1)

        # 件数表示
        self.lbl_count = QLabel("登録数: 0 / 1000")
        v.addWidget(self.lbl_count)

        # 音源分離パーツ選択
        grp = QGroupBox("音源分離 — 抽出するパートを選択（何も選択しなければ分離スキップ・ダウンロード後に自動実行）")
        grp_layout = QHBoxLayout(grp)
        grp_layout.setSpacing(20)
        self._stem_checks: dict = {}
        for key, label in [
            ('vocals', 'Vocal'),
            ('guitar', 'Guitar'),
            ('bass',   'Bass'),
            ('drums',  'Drums'),
            ('piano',  'Piano'),
            ('other',  'その他'),
        ]:
            cb = QCheckBox(label)
            cb.setChecked(False)
            grp_layout.addWidget(cb)
            self._stem_checks[key] = cb
        grp_layout.addStretch()

        # 動画ダウンロードチェックボックス
        self.chk_keep_video = QCheckBox("動画をダウンロード (MP4)")
        self.chk_keep_video.setChecked(False)
        self.chk_keep_video.setToolTip(
            "チェックすると映像と音声をまとめた MP4 ファイルをダウンロードします（音声分離は行われません）。"
        )
        grp_layout.addWidget(self.chk_keep_video)

        # 並列数スピンボックスを右端に追加
        grp_layout.addWidget(QLabel("並列数:"))
        self.spin_concurrency = QSpinBox()
        self.spin_concurrency.setRange(1, 10)
        self.spin_concurrency.setValue(3)
        self.spin_concurrency.setFixedWidth(80)
        self.spin_concurrency.setMinimumHeight(28)
        self.spin_concurrency.setStyleSheet(
            "QSpinBox { background: #313244; color: #cdd6f4; border: 1px solid #45475a;"
            " border-radius: 4px; padding: 2px 6px; font-size: 14px; }"
            "QSpinBox::up-button, QSpinBox::down-button { width: 18px; }"
        )
        self.spin_concurrency.setToolTip(
            "ダウンロード・音源分離の同時処理件数 (1〜10)"
        )
        self.spin_concurrency.valueChanged.connect(self._update_concurrency)
        grp_layout.addWidget(self.spin_concurrency)
        v.addWidget(grp)

        # アクションボタン行
        h3 = QHBoxLayout()
        self.btn_start = QPushButton("▶  スタート")
        self.btn_start.setMinimumHeight(44)
        self.btn_start.clicked.connect(self._start_download)
        btn_clear = QPushButton("リストクリア")
        btn_clear.setMinimumHeight(44)
        btn_clear.clicked.connect(self._clear_list)
        h3.addWidget(self.btn_start)
        h3.addWidget(btn_clear)
        v.addLayout(h3)

        return root

    def _apply_style(self):
        self.setStyleSheet("""
            QWidget {
                background: #1e1e2e;
                color: #cdd6f4;
                font-size: 13px;
                font-family: "Segoe UI", "Yu Gothic UI", sans-serif;
            }
            QLineEdit {
                background: #313244;
                border: 1px solid #45475a;
                border-radius: 4px;
                padding: 4px 8px;
                color: #cdd6f4;
            }
            QLineEdit[readOnly="true"] { background: #181825; }
            QPushButton {
                background: #89b4fa;
                color: #1e1e2e;
                border-radius: 4px;
                font-weight: bold;
                padding: 0 12px;
                border: none;
            }
            QPushButton:hover    { background: #b4d0fa; }
            QPushButton:pressed  { background: #6e9ef0; }
            QPushButton:disabled { background: #45475a; color: #6c7086; }
            QTableWidget {
                background: #181825;
                border: 1px solid #45475a;
                gridline-color: #313244;
            }
            QHeaderView::section {
                background: #313244;
                color: #cdd6f4;
                border: none;
                padding: 6px;
                font-weight: bold;
            }
            QTableWidget::item          { padding: 4px; }
            QTableWidget::item:selected { background: #45475a; }
            QLabel { color: #cdd6f4; }
            QScrollBar:vertical              { background: #181825; width: 10px; }
            QScrollBar::handle:vertical      { background: #45475a; border-radius: 5px; }
            QScrollBar:horizontal            { background: #181825; height: 10px; }
            QScrollBar::handle:horizontal    { background: #45475a; border-radius: 5px; }
            QScrollBar::add-line, QScrollBar::sub-line { background: none; border: none; }
            QMessageBox { background: #1e1e2e; }
            QGroupBox {
                border: 1px solid #45475a;
                border-radius: 4px;
                margin-top: 10px;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
            }
            QCheckBox { font-weight: normal; spacing: 6px; }
            QCheckBox::indicator {
                width: 15px; height: 15px;
                border: 1px solid #89b4fa;
                border-radius: 3px;
                background: #313244;
            }
            QCheckBox::indicator:checked { background: #89b4fa; }
            QTabWidget::pane {
                border: 1px solid #45475a;
                border-top: none;
            }
            QTabBar::tab {
                background: #313244;
                color: #6c7086;
                padding: 8px 22px;
                border: 1px solid #45475a;
                border-bottom: none;
                border-radius: 4px 4px 0 0;
                margin-right: 2px;
            }
            QTabBar::tab:selected { background: #1e1e2e; color: #89b4fa; font-weight: bold; }
            QTabBar::tab:hover    { background: #45475a; color: #cdd6f4; }
        """)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _update_concurrency(self, value: int):
        self._pool_dl.setMaxThreadCount(value)
        self._pool_sep.setMaxThreadCount(value)
        self._save_data()

    def _find_row(self, url: str) -> int:
        for i, e in enumerate(self.entries):
            if e['url'] == url:
                return i
        return -1

    def _full_refresh(self):
        self.tbl.setRowCount(len(self.entries))
        for r in range(len(self.entries)):
            self._paint_row(r)
        self.lbl_count.setText(f"登録数: {len(self.entries)} / {MAX_ENTRIES}")

    def _paint_row(self, row: int):
        e = self.entries[row]

        # サムネイル
        lbl = QLabel()
        lbl.setAlignment(Qt.AlignCenter)
        if e.get('_pixmap'):
            lbl.setPixmap(e['_pixmap'])
        self.tbl.setCellWidget(row, 0, lbl)

        # タイトル
        ti = QTableWidgetItem(e.get('title', ''))
        ti.setToolTip(e.get('title', ''))
        self.tbl.setItem(row, 1, ti)

        # URL
        ui = QTableWidgetItem(e['url'])
        ui.setToolTip(e['url'])
        self.tbl.setItem(row, 2, ui)

        # 状態
        self._paint_status(row)

    def _paint_status(self, row: int):
        e = self.entries[row]
        w = self.tbl.cellWidget(row, 3)
        if not isinstance(w, StatusWidget):
            w = StatusWidget()
            self.tbl.setCellWidget(row, 3, w)

        if e.get('_sep_done'):
            w.set_done()
        elif e.get('_sep_error'):
            full_msg = e['_sep_error']
            w.set_error(f"[分離エラー] クリックで詳細表示")
            w.lbl.setToolTip(full_msg[:500])
            w.lbl.setCursor(Qt.PointingHandCursor)
            # クリックで全文ダイアログを表示
            w.lbl.mousePressEvent = lambda _, msg=full_msg: QMessageBox.critical(
                None, "分離エラー詳細", msg
            )
        elif e.get('_separating'):
            w.set_separating(e.get('_sep_progress', '音源分離中...'))
        elif e.get('downloaded'):
            w.set_done()
        elif e.get('_error'):
            full_msg = e['_error']
            w.set_error("[DLエラー] クリックで詳細表示")
            w.lbl.setToolTip(full_msg[:500])
            w.lbl.setCursor(Qt.PointingHandCursor)
            w.lbl.mousePressEvent = lambda _, msg=full_msg: QMessageBox.critical(
                None, "ダウンロードエラー詳細", msg
            )
        elif e.get('_progress') == '変換中...':
            w.set_converting()
        elif e.get('_progress'):
            w.set_downloading(e.get('_pct', 0.0), e['_progress'])
        elif e.get('_queued'):
            w.set_waiting()
        else:
            w.set_idle()

    # ── User actions ───────────────────────────────────────────────────────────

    def _add_url(self):
        url = self.url_edit.text().strip()
        if not url:
            return
        if not url.startswith(('http://', 'https://')):
            QMessageBox.warning(self, "入力エラー", "有効な URL を入力してください。")
            return
        if self._find_row(url) >= 0:
            QMessageBox.information(self, "重複", "この URL はすでに登録されています。")
            return

        entry = {
            'url':        url,
            'title':      '情報取得中...',
            'thumb_url':  '',
            'downloaded': False,
        }
        self.entries.insert(0, entry)

        # 上限超過時は古いものを削除
        if len(self.entries) > MAX_ENTRIES:
            self.entries = self.entries[:MAX_ENTRIES]

        self.url_edit.clear()
        self._full_refresh()
        self._save_data()

        # 情報取得を非同期で開始
        self._pool_misc.start(InfoWorker(url, self.sig))

    def _show_context_menu(self, pos):
        """右クリックで選択行を再ダウンロード / 削除するメニューを表示"""
        rows = sorted({idx.row() for idx in self.tbl.selectedIndexes()})
        if not rows:
            return
        menu = QMenu(self)
        act_redl = menu.addAction("🔄  再ダウンロード")
        act_redl.setEnabled(any(self.entries[r].get('downloaded') for r in rows if r < len(self.entries)))
        menu.addSeparator()
        act_del = menu.addAction("🗑  選択行を削除")
        action = menu.exec(self.tbl.viewport().mapToGlobal(pos))
        if action == act_redl:
            self._redownload_rows(rows)
        elif action == act_del:
            self._remove_rows(rows)

    def _remove_rows(self, rows: list):
        """指定行をリストから削除する（ダウンロード中の行はスキップ）"""
        # 逆順で削除するとインデックスがずれない
        for r in sorted(rows, reverse=True):
            if r >= len(self.entries):
                continue
            url = self.entries[r]['url']
            if url in self._dl_pending or url in self._sep_pending:
                continue  # 処理中はスキップ
            self.entries.pop(r)
            self.tbl.removeRow(r)
        self._save_data()

    def _redownload_rows(self, rows: list):
        """指定行の downloaded フラグをリセットして再ダウンロードキューに投入"""
        selected_stems = [k for k, cb in self._stem_checks.items() if cb.isChecked()]
        if not Path(self.output_dir).is_dir():
            QMessageBox.warning(self, "エラー", f"出力先フォルダが存在しません。\n{self.output_dir}")
            return
        to_dl = []
        for r in rows:
            if r >= len(self.entries):
                continue
            e = self.entries[r]
            e.pop('downloaded',    None)
            e.pop('mp3_path',      None)
            e.pop('sep_done',      None)
            e.pop('_error',        None)
            e.pop('_progress',     None)
            e.pop('_pct',          None)
            e.pop('_sep_done',     None)
            e.pop('_sep_error',    None)
            e.pop('_sep_progress', None)
            e.pop('_separating',   None)
            e['_queued'] = True
            to_dl.append(e)
        if not to_dl:
            return
        self._dl_pending |= {e['url'] for e in to_dl}
        if self._dl_total == 0:
            self._dl_total = len(to_dl)
        self.btn_start.setEnabled(False)
        self.btn_start.setText(f"ダウンロード中... (0 / {len(self._dl_pending)})")
        for e in to_dl:
            row = self._find_row(e['url'])
            self._paint_status(row)
            use_subdir = len(selected_stems) > 0
            keep_video = self.chk_keep_video.isChecked()
            self._pool_dl.start(DownloadWorker(e['url'], self.output_dir, use_subdir, self.sig,
                                               keep_video=keep_video))
        self._save_data()

    def _start_download(self):
        selected_stems = [k for k, cb in self._stem_checks.items() if cb.isChecked()]
        to_dl = [e for e in self.entries if not e.get('downloaded')]
        if not to_dl:
            QMessageBox.information(self, "完了", "ダウンロード待ちのアイテムがありません。")
            return
        if not Path(self.output_dir).is_dir():
            QMessageBox.warning(
                self, "エラー",
                f"出力先フォルダが存在しません。\n{self.output_dir}",
            )
            return

        # エラー・進捗フラグをリセットし待機状態へ
        for e in to_dl:
            e.pop('_error',    None)
            e.pop('_progress', None)
            e.pop('_pct',      None)
            e['_queued'] = True

        self._dl_pending = {e['url'] for e in to_dl}
        self._dl_total   = len(self._dl_pending)
        self.btn_start.setEnabled(False)
        self.btn_start.setText(f"ダウンロード中... (0 / {self._dl_total})")

        for e in to_dl:
            row = self._find_row(e['url'])
            self._paint_status(row)
            use_subdir = len(selected_stems) > 0
            keep_video = self.chk_keep_video.isChecked()
            self._pool_dl.start(DownloadWorker(e['url'], self.output_dir, use_subdir, self.sig,
                                               keep_video=keep_video))

    def _clear_list(self):
        if not self.entries:
            QMessageBox.information(self, "情報", "リストは既に空です。")
            return
        reply = QMessageBox.question(
            self,
            "リストクリアの確認",
            f"登録されている {len(self.entries)} 件を全て削除しますか？\n"
            "この操作は元に戻せません。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.entries.clear()
            self.tbl.setRowCount(0)
            self.lbl_count.setText("登録数: 0 / 1000")
            self._save_data()

    def _change_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "出力先フォルダを選択", self.output_dir
        )
        if folder:
            self.output_dir = folder
            self.folder_edit.setText(folder)
            self._save_data()

    def _open_folder(self):
        path = self.output_dir
        if Path(path).is_dir():
            _open_path_in_explorer(path)
        else:
            QMessageBox.warning(self, "エラー", f"フォルダが存在しません:\n{path}")

    # ── Signal slots ──────────────────────────────────────────────────────────

    @Slot(str, str, str)
    def _on_info_ready(self, url: str, title: str, thumb_url: str):
        row = self._find_row(url)
        if row < 0:
            return
        self.entries[row]['title']     = title
        self.entries[row]['thumb_url'] = thumb_url
        item = self.tbl.item(row, 1)
        if item:
            item.setText(title)
            item.setToolTip(title)
        self._save_data()
        if thumb_url:
            self._pool_misc.start(ThumbWorker(url, thumb_url, self.sig))

    @Slot(str, bytes)
    def _on_thumb_data(self, url: str, data: bytes):
        """サムネイル画像バイト列を受け取り、メインスレッドで QPixmap を生成"""
        row = self._find_row(url)
        if row < 0:
            return
        pix = QPixmap()
        pix.loadFromData(data)
        if pix.isNull():
            return
        pix = pix.scaled(THUMB_W, THUMB_H, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.entries[row]['_pixmap'] = pix
        lbl = self.tbl.cellWidget(row, 0)
        if isinstance(lbl, QLabel):
            lbl.setPixmap(pix)

    @Slot(str, str, float)
    def _on_dl_progress(self, url: str, text: str, pct: float):
        row = self._find_row(url)
        if row < 0:
            return
        e = self.entries[row]
        e['_queued']   = False
        e['_progress'] = text
        e['_pct']      = pct
        self._paint_status(row)

    @Slot(str, str)
    def _on_dl_done(self, url: str, mp3_path: str):
        row = self._find_row(url)
        if row >= 0:
            e = self.entries[row]
            e['downloaded'] = True
            e['mp3_path']   = mp3_path
            e.pop('_progress',     None)
            e.pop('_pct',          None)
            e.pop('_error',        None)
            e.pop('_queued',       None)
            # 再ダウンロード時に古い分離状態が残っている場合もリセットする
            e.pop('_sep_done',     None)
            e.pop('_sep_error',    None)
            e.pop('_sep_progress', None)
            e.pop('_separating',   None)
            self._paint_status(row)
            self._save_data()
            # パーツが選択されていれば自動で音源分離を開始
            self._auto_separate(e)
        self._dl_pending.discard(url)
        self._refresh_dl_button()

    @Slot(str, str)
    def _on_dl_error(self, url: str, msg: str):
        row = self._find_row(url)
        if row >= 0:
            self.entries[row]['_error'] = msg
            self.entries[row].pop('_progress', None)
            self.entries[row].pop('_pct',      None)
            self.entries[row].pop('_queued',   None)
            self._paint_status(row)
        self._dl_pending.discard(url)
        self._refresh_dl_button()

    def _refresh_dl_button(self):
        if self._dl_pending:
            done = self._dl_total - len(self._dl_pending)
            self.btn_start.setText(f"ダウンロード中... ({done} / {self._dl_total})")
        else:
            self.btn_start.setEnabled(True)
            self.btn_start.setText("▶  スタート")
            self._dl_total = 0

    def _auto_separate(self, e: dict):
        """ダウンロード完了後に選択パーツがあれば自動で音源分離を開始"""
        selected_stems = [k for k, cb in self._stem_checks.items() if cb.isChecked()]
        if not selected_stems:
            return
        if not _DEMUCS_AVAILABLE:
            return
        mp3_path = e.get('mp3_path', '')
        if not mp3_path or not Path(mp3_path).exists():
            return
        if e.get('_sep_done') or e.get('_separating'):
            return

        e.pop('_sep_error', None)
        e['_separating']   = True
        e['_sep_progress'] = '待機中'

        url = e['url']
        self._sep_pending.add(url)
        self._sep_total = len(self._sep_pending)

        row = self._find_row(url)
        if row >= 0:
            self._paint_status(row)

        self._pool_sep.start(
            SeparateWorker(url, mp3_path, selected_stems, self.sig)
        )

    @Slot(str, str)
    def _on_sep_progress(self, url: str, text: str):
        row = self._find_row(url)
        if row < 0:
            return
        self.entries[row]['_sep_progress'] = text
        self._paint_status(row)

    @Slot(str)
    def _on_sep_done(self, url: str):
        # 再ダウンロード中に古い分離ワーカーのステイルシグナルが届いた場合は無視
        if url in self._dl_pending:
            self._sep_pending.discard(url)
            return
        row = self._find_row(url)
        if row >= 0:
            e = self.entries[row]
            # downloaded がリセットされている場合（再ダウンロード中）は無視
            if not e.get('downloaded'):
                self._sep_pending.discard(url)
                return
            e['_sep_done']  = True
            e['_separating'] = False
            e.pop('_sep_error',    None)
            e.pop('_sep_progress', None)
            self._paint_status(row)
            self._save_data()
        self._sep_pending.discard(url)

    @Slot(str, str)
    def _on_sep_error(self, url: str, msg: str):
        # 再ダウンロード中は無視
        if url in self._dl_pending:
            self._sep_pending.discard(url)
            return
        row = self._find_row(url)
        if row >= 0:
            e = self.entries[row]
            if not e.get('downloaded'):
                self._sep_pending.discard(url)
                return
            self.entries[row]['_sep_error']  = msg
            self.entries[row]['_separating'] = False
            self.entries[row].pop('_sep_progress', None)
            self._paint_status(row)
        self._sep_pending.discard(url)

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_data(self):
        try:
            serial = [
                {
                    'url':        e['url'],
                    'title':      e.get('title', ''),
                    'thumb_url':  e.get('thumb_url', ''),
                    'downloaded': e.get('downloaded', False),
                    'mp3_path':   e.get('mp3_path', ''),
                    'sep_done':   e.get('_sep_done', False),
                }
                for e in self.entries
            ]
            payload = {'output_dir': self.output_dir, 'concurrency': self.spin_concurrency.value(), 'entries': serial}
            with open(DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_data(self):
        if not DATA_FILE.exists():
            return
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.output_dir = data.get('output_dir', self.output_dir)
            self.folder_edit.setText(self.output_dir)
            concurrency = int(data.get('concurrency', 3))
            concurrency = max(1, min(10, concurrency))
            self.spin_concurrency.setValue(concurrency)
            self._pool_dl.setMaxThreadCount(concurrency)
            self._pool_sep.setMaxThreadCount(concurrency)
            raw = data.get('entries', [])
            loaded = [e for e in raw if isinstance(e, dict) and e.get('url')]
            for e in loaded:
                if e.pop('sep_done', False):
                    e['_sep_done'] = True
            self.entries = loaded
            self._full_refresh()
            # 全エントリのサムネイルを再読み込み
            for e in self.entries:
                if e.get('thumb_url'):
                    self._pool_misc.start(ThumbWorker(e['url'], e['thumb_url'], self.sig))
        except Exception:
            pass

    def closeEvent(self, event):
        """処理中に閉じようとした場合は確認ダイアログを表示し、中間ファイルを削除する"""
        yt_busy    = bool(self._dl_pending or self._sep_pending)
        local_busy = bool(self.local_sep_tab._pending)

        if yt_busy or local_busy:
            parts = []
            if self._dl_pending:
                parts.append(f"・ダウンロード中: {len(self._dl_pending)} 件")
            if self._sep_pending:
                parts.append(f"・音源分離中 (YouTube): {len(self._sep_pending)} 件")
            if local_busy:
                parts.append(f"・音源分離中 (ローカル): {len(self.local_sep_tab._pending)} 件")
            reply = QMessageBox.question(
                self,
                "処理中です — 終了しますか？",
                "以下の処理が実行中です:\n" + "\n".join(parts) +
                "\n\n終了すると処理が中断され、中間ファイルを削除します。\n本当に終了しますか？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return

        # 全プールを即時停止（新規タスクのみ。実行中スレッドは完了を待たない）
        self._pool_dl  .clear()
        self._pool_sep .clear()
        self._pool_misc.clear()
        self.local_sep_tab._pool.clear()

        # 中間ファイル削除: YouTube 分離中エントリの _sep_tmp
        for e in self.entries:
            if e.get('_separating') and e.get('mp3_path'):
                tmp = Path(e['mp3_path']).parent / '_sep_tmp'
                shutil.rmtree(tmp, ignore_errors=True)

        # 中間ファイル削除: ローカル分離中の _sep_tmp
        out_dir = Path(self.local_sep_tab.output_dir)
        for path, state in self.local_sep_tab.states.items():
            if state.get('separating'):
                tmp = out_dir / Path(path).stem / '_sep_tmp'
                shutil.rmtree(tmp, ignore_errors=True)

        event.accept()

    def keyPressEvent(self, event):
        """Ctrl+V でクリップボードの URL をリストに追加 / Delete で選択行削除 (YouTube タブのみ)"""
        if self._tabs.currentIndex() == 0:
            if event.matches(QKeySequence.StandardKey.Paste):
                clipboard = QApplication.clipboard()
                text = clipboard.text().strip()
                if text.startswith(('http://', 'https://')):
                    self.url_edit.setText(text)
                    self._add_url()
                    return
            if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                rows = sorted({idx.row() for idx in self.tbl.selectedIndexes()})
                if rows:
                    self._remove_rows(rows)
                    return
        super().keyPressEvent(event)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("YouTube Audio Downloader")
    _icon_path = _BUNDLE_DIR / 'icon.png'
    if _icon_path.exists():
        try:
            from PIL import Image as _PilImage
            import io as _io
            _pil = _PilImage.open(str(_icon_path)).convert('RGBA')
            _bg = _PilImage.new('RGBA', _pil.size, (255, 255, 255, 255))
            _bg.paste(_pil, mask=_pil.split()[3])
            _buf = _io.BytesIO()
            _bg.convert('RGB').save(_buf, format='PNG')
            _px = QPixmap()
            _px.loadFromData(_buf.getvalue())
            _icon = QIcon(_px)
        except Exception:
            _icon = QIcon(str(_icon_path))
        app.setWindowIcon(_icon)
    window = MainWindow()
    if _icon_path.exists():
        window.setWindowIcon(_icon)
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
