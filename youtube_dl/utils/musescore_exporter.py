"""
utils/musescore_exporter.py
MuseScore CLI を使って MusicXML を PDF / MIDI に変換するユーティリティ。

MuseScore 3/4 がインストールされていれば自動検出して使用します。
未インストールの場合は ImportError-like な MuseScoreNotFoundError を投げます。

インストール先 (Windows):
  C:\\Program Files\\MuseScore 4\\bin\\MuseScore4.exe
  C:\\Program Files\\MuseScore 3\\bin\\MuseScore3.exe
  %LOCALAPPDATA%\\Programs\\MuseScore 4\\bin\\MuseScore4.exe
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class MuseScoreNotFoundError(RuntimeError):
    """MuseScore 実行ファイルが見つからない場合の例外。"""
    pass


def find_musescore() -> str | None:
    """MuseScore の実行ファイルパスを返す。見つからなければ None。"""
    # 1) PATH に含まれているか
    import shutil
    for name in ("mscore4", "mscore3", "mscore", "MuseScore4", "MuseScore3"):
        found = shutil.which(name)
        if found:
            return found

    # 2) Windows の代表的インストールパスを検索
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
        / "MuseScore 4" / "bin" / "MuseScore4.exe",
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
        / "MuseScore 3" / "bin" / "MuseScore3.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Programs" / "MuseScore 4" / "bin" / "MuseScore4.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Programs" / "MuseScore 3" / "bin" / "MuseScore3.exe",
        # MuseScore 4 ポータブル版など
        Path("C:/MuseScore 4/bin/MuseScore4.exe"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)

    return None


def is_available() -> bool:
    """MuseScore が使用可能かどうかを返す。"""
    return find_musescore() is not None


def _run_musescore(input_path: str, output_path: str) -> None:
    """MuseScore CLI を呼び出してファイル変換を実行する。"""
    exe = find_musescore()
    if exe is None:
        raise MuseScoreNotFoundError(
            "MuseScore が見つかりません。\n"
            "https://musescore.org から MuseScore 4 をインストールして再試行してください。"
        )

    cmd = [exe, "-o", output_path, input_path]
    logger.info("MuseScore: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"MuseScore の変換に失敗しました。\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def xml_to_pdf(xml_str: str, output_path: str) -> str:
    """MusicXML 文字列を PDF ファイルに変換して output_path に保存する。

    Args:
        xml_str: MusicXML の文字列
        output_path: 出力 PDF のパス（例: /tmp/score.pdf）

    Returns:
        output_path をそのまま返す

    Raises:
        MuseScoreNotFoundError: MuseScore が未インストール
        RuntimeError: 変換失敗
    """
    with tempfile.NamedTemporaryFile(
        suffix=".musicxml", delete=False, mode="w", encoding="utf-8"
    ) as f:
        tmp_xml = f.name
        f.write(xml_str)
    try:
        _run_musescore(tmp_xml, output_path)
    finally:
        if os.path.exists(tmp_xml):
            os.unlink(tmp_xml)
    return output_path


def xml_to_midi(xml_str: str, output_path: str) -> str:
    """MusicXML 文字列を MIDI ファイルに変換して output_path に保存する。

    Args:
        xml_str: MusicXML の文字列
        output_path: 出力 MIDI のパス（例: /tmp/score.mid）

    Returns:
        output_path をそのまま返す

    Raises:
        MuseScoreNotFoundError: MuseScore が未インストール
        RuntimeError: 変換失敗
    """
    with tempfile.NamedTemporaryFile(
        suffix=".musicxml", delete=False, mode="w", encoding="utf-8"
    ) as f:
        tmp_xml = f.name
        f.write(xml_str)
    try:
        _run_musescore(tmp_xml, output_path)
    finally:
        if os.path.exists(tmp_xml):
            os.unlink(tmp_xml)
    return output_path


def xml_to_pdf_bytes(xml_str: str) -> bytes:
    """MusicXML 文字列を PDF のバイト列として返す（Streamlit ダウンロード用）。"""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        tmp_pdf = f.name
    try:
        xml_to_pdf(xml_str, tmp_pdf)
        with open(tmp_pdf, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_pdf):
            os.unlink(tmp_pdf)
