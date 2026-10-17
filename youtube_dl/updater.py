"""GitHub Release からの自動アップデート。

最新の正式リリース（タグ v*）を gh CLI で確認し、配布 ZIP を取得して
実行中のアプリを差し替える。差し替えはアプリ終了後に別プロセスで行う。
リポジトリは非公開のため、認証は gh CLI のログイン情報を利用する。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from version import __version__

REPO = 'yesmenjoker3104/youtube-audio-downloader'
APP_NAME = 'YoutubeAudioDownloader'
_GH_TIMEOUT = 900

# Windows: 終了を待ってから robocopy で上書きし、再起動する。
# download_list.json と models\ はユーザーデータなので上書き・削除しない。
_WIN_SCRIPT = r'''@echo off
rem Wait for the running app to exit, copy the new files, then restart the app
:wait
tasklist /FI "PID eq %YAD_PID%" 2>nul | find "%YAD_PID%" >nul
if errorlevel 1 goto copy
ping -n 2 127.0.0.1 >nul
goto wait
:copy
robocopy "%YAD_SRC%" "%YAD_DST%" /E /R:5 /W:1 /NFL /NDL /NJH /NJS /NP /XF download_list.json /XD models >nul
if errorlevel 8 goto fail
start "" "%YAD_EXE%"
goto done
:fail
echo %date% %time% update failed >> "%TEMP%\YoutubeAudioDownloader-update.log"
start "" "%YAD_EXE%"
:done
(goto) 2>nul & rmdir /S /Q "%YAD_BASE%"
'''

# macOS: 旧 .app を退避してから ditto で差し替える。失敗時は旧 .app を元に戻す。
_MAC_SCRIPT = '''#!/bin/sh
pid="$1"; new_app="$2"; target="$3"; base="$4"
while kill -0 "$pid" 2>/dev/null; do sleep 1; done
old="$target.old"
rm -rf "$old"
if mv "$target" "$old"; then
  if ditto "$new_app" "$target"; then
    rm -rf "$old"
  else
    rm -rf "$target"
    mv "$old" "$target"
  fi
fi
open "$target"
rm -rf "$base"
'''


class UpdateError(Exception):
    """ユーザーに表示するアップデートエラー"""


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str        # 例: v1.2.0
    version: str    # 例: 1.2.0
    asset: str      # 取得する ZIP 名


@dataclass(frozen=True)
class UpdatePackage:
    base: Path      # 作業フォルダ（差し替え後に削除される）
    app: Path       # 展開済みアプリ（exe を含むフォルダ / .app バンドル）


def is_frozen() -> bool:
    return bool(getattr(sys, 'frozen', False))


def parse_version(text: str) -> tuple[int, ...]:
    m = re.fullmatch(r'v?(\d+(?:\.\d+)*)', text.strip())
    if not m:
        raise ValueError(f'バージョンの形式が不正です: {text}')
    return tuple(int(x) for x in m.group(1).split('.'))


def is_newer(remote: str, local: str) -> bool:
    a, b = parse_version(remote), parse_version(local)
    n = max(len(a), len(b))
    # 1.2 と 1.2.0 は同じ扱いにする
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def asset_name() -> str:
    if sys.platform == 'win32':
        return f'{APP_NAME}-Windows.zip'
    if sys.platform == 'darwin':
        return f'{APP_NAME}-macOS.zip'
    raise UpdateError('このOSでは自動アップデートに対応していません。')


def _run_gh(args: list[str], timeout: int = _GH_TIMEOUT) -> str:
    gh = shutil.which('gh')
    if gh is None:
        raise UpdateError(
            'GitHub CLI (gh) が見つかりません。\n'
            'https://cli.github.com/ からインストールし、`gh auth login` を実行してください。'
        )
    try:
        proc = subprocess.run(
            [gh, *args],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            stdin=subprocess.DEVNULL, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise UpdateError('gh の実行がタイムアウトしました。ネットワーク接続を確認してください。') from e
    if proc.returncode != 0:
        raise UpdateError(f'gh の実行に失敗しました:\n{(proc.stderr or proc.stdout).strip()}')
    return proc.stdout


def check_for_update() -> 'ReleaseInfo | None':
    """最新の正式リリースが現在のバージョンより新しければ ReleaseInfo を返す。なければ None。"""
    try:
        data = json.loads(_run_gh(['api', f'repos/{REPO}/releases/latest'], timeout=60))
    except UpdateError as e:
        if 'HTTP 404' in str(e):
            raise UpdateError('正式リリースがまだありません。') from e
        raise
    tag = data.get('tag_name', '')
    try:
        parse_version(tag)
    except ValueError as e:
        raise UpdateError(f'最新リリースのタグが不正です: {tag}') from e
    if not is_newer(tag, __version__):
        return None
    asset = asset_name()
    if asset not in {a.get('name') for a in data.get('assets', [])}:
        raise UpdateError(f'{tag} に {asset} が含まれていません。')
    return ReleaseInfo(tag=tag, version=tag.lstrip('vV'), asset=asset)


def _extract(zip_path: Path, dest: Path) -> None:
    if sys.platform == 'darwin':
        # zipfile は実行権限とシンボリックリンクを復元できないため ditto を使う
        subprocess.run(['ditto', '-x', '-k', str(zip_path), str(dest)], check=True)
    else:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest)


def _is_app_root(p: Path) -> bool:
    if sys.platform == 'darwin':
        return (p / 'Contents' / 'MacOS').is_dir()
    return (p / '_internal').is_dir()


def _find_app_root(extract_dir: Path) -> Path:
    if sys.platform == 'darwin':
        candidates = [p for p in extract_dir.rglob('*.app') if p.is_dir()]
    else:
        candidates = [p.parent for p in extract_dir.rglob(f'{APP_NAME}.exe')]
    # ZIP の階層（フォルダ内にまとめた形式 / 直下に置いた形式）のどちらにも対応する
    for c in sorted(candidates, key=lambda p: len(p.parts)):
        if _is_app_root(c):
            return c
    raise UpdateError('ダウンロードしたパッケージにアプリ本体が見つかりません。')


def download_update(info: ReleaseInfo) -> UpdatePackage:
    base = Path(tempfile.mkdtemp(prefix='yad-update-'))
    try:
        pkg_dir = base / 'download'
        pkg_dir.mkdir()
        _run_gh(['release', 'download', info.tag, '--repo', REPO,
                 '--pattern', info.asset, '--dir', str(pkg_dir), '--clobber'])
        extract_dir = base / 'app'
        _extract(pkg_dir / info.asset, extract_dir)
        return UpdatePackage(base=base, app=_find_app_root(extract_dir))
    except Exception:
        shutil.rmtree(base, ignore_errors=True)
        raise


def discard_update(pkg: UpdatePackage) -> None:
    shutil.rmtree(pkg.base, ignore_errors=True)


def install_target() -> Path:
    """差し替え先（Windows: exe のあるフォルダ / macOS: .app バンドル）"""
    exe = Path(sys.executable).resolve()
    if sys.platform == 'darwin':
        for parent in exe.parents:
            if parent.suffix == '.app':
                return parent
        raise UpdateError('.app バンドルとして実行されていないため更新できません。')
    return exe.parent


def start_update(pkg: UpdatePackage, target: Path, pid: int) -> None:
    """アプリ終了後に差し替えを行うスクリプトを別プロセスで起動する。呼び出し側はこのあとアプリを終了すること。"""
    if sys.platform == 'darwin':
        script = pkg.base / 'update.sh'
        script.write_text(_MAC_SCRIPT, encoding='utf-8')
        subprocess.Popen(
            ['/bin/sh', str(script), str(pid), str(pkg.app), str(target), str(pkg.base)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return

    # パスは環境変数で渡す（バッチファイルを非 ASCII パスから切り離すため）
    script = pkg.base / 'update.cmd'
    script.write_bytes(_WIN_SCRIPT.replace('\n', '\r\n').encode('ascii'))
    env = {
        **os.environ,
        'YAD_PID': str(pid),
        'YAD_SRC': str(pkg.app),
        'YAD_DST': str(target),
        'YAD_EXE': str(target / f'{APP_NAME}.exe'),
        'YAD_BASE': str(pkg.base),
    }
    subprocess.Popen(
        ['cmd.exe', '/c', str(script)],
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True,
    )
