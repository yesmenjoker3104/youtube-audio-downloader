#!/usr/bin/env python3
"""YoutubeAudioDownloader command-line interface."""

import argparse
import json
import sys
from pathlib import Path

from app import AppSignals, DownloadWorker, SeparateWorker


DEFAULT_OUTPUT_DIR = Path.home() / "Downloads" / "YoutubeAudioDownloader"
STEM_CHOICES = ("vocals", "drums", "bass", "guitar", "piano", "other")


def _print_download_progress(_url: str, text: str, percent: float) -> None:
    if percent >= 0:
        print(f"[download] {percent:5.1f}% {text}", flush=True)
    else:
        print(f"[download] {text}", flush=True)


def _print_separation_progress(_source: str, text: str) -> None:
    print(f"[separate] {text}", flush=True)


def _configure_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="YouTubeの音声ダウンロードとDemucsによる音源分離を実行します。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    download = subparsers.add_parser("download", help="YouTube URLをダウンロードする")
    download.add_argument("urls", nargs="+", help="YouTube URL（複数指定可）")
    download.add_argument(
        "--output-dir", "-o", type=Path, default=DEFAULT_OUTPUT_DIR,
        help=f"保存先フォルダ（既定: {DEFAULT_OUTPUT_DIR}）",
    )
    download.add_argument("--mp4", action="store_true", help="MP4とMP3を保存する")
    download.add_argument("--playlist", action="store_true", help="プレイリスト内の動画をすべて処理する")
    download.add_argument("--flat", action="store_true", help="曲ごとのサブフォルダを作らない")
    download.add_argument(
        "--stems", nargs="+", choices=STEM_CHOICES,
        help="ダウンロード完了後に指定パートを音源分離する",
    )

    subparsers.add_parser("history", help="ダウンロード履歴を表示")

    separate = subparsers.add_parser("separate", help="ローカル音声ファイルを音源分離する")
    separate.add_argument("files", nargs="+", type=Path, help="入力音声ファイル（複数指定可）")
    separate.add_argument(
        "--stems", nargs="+", choices=STEM_CHOICES, default=list(STEM_CHOICES),
        help="抽出するパート（既定: 全パート）",
    )
    return parser


def _history() -> bool:
    if getattr(sys, "frozen", False):
        data_file = Path(sys.executable).parent / "download_list.json"
    else:
        data_file = Path(__file__).parent / "download_list.json"
    if not data_file.exists():
        print(json.dumps({"ok": True, "entries": [], "count": 0}, ensure_ascii=True))
        return True
    try:
        with open(data_file, encoding="utf-8") as f:
            data = json.load(f)
        entries = [
            {
                "title": e.get("title", ""),
                "url": e.get("url", ""),
                "downloaded": e.get("downloaded", False),
                "mp3_path": e.get("mp3_path", ""),
            }
            for e in data.get("entries", [])
            if isinstance(e, dict)
        ]
        print(json.dumps({"ok": True, "entries": entries, "count": len(entries)}, ensure_ascii=True))
        return True
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return False


def _download(url: str, output_dir: Path, keep_video: bool, use_subdir: bool, stems: list[str] | None, playlist: bool) -> bool:
    signals = AppSignals()
    result: dict[str, str] = {"path": "", "error": ""}
    signals.dl_progress.connect(_print_download_progress)
    signals.dl_done.connect(lambda _url, path: result.__setitem__("path", path))
    signals.dl_error.connect(lambda _url, error: result.__setitem__("error", error))

    print(f"[download] 開始: {url}", flush=True)
    DownloadWorker(
        url, str(output_dir), use_subdir, signals,
        keep_video=keep_video, allow_playlist=playlist,
    ).run()
    if result["error"]:
        print(f"[error] ダウンロード失敗: {url}\n{result['error']}", file=sys.stderr)
        return False
    if not result["path"]:
        print(f"[error] 出力されたMP3を特定できませんでした: {url}", file=sys.stderr)
        return False

    print(f"[download] 完了: {result['path']}", flush=True)
    return _separate(result["path"], stems) if stems else True


def _separate(audio_path: str, stems: list[str]) -> bool:
    signals = AppSignals()
    result = {"error": "", "done": False}
    signals.sep_progress.connect(_print_separation_progress)
    signals.sep_done.connect(lambda _source: result.__setitem__("done", True))
    signals.sep_error.connect(lambda _source, error: result.__setitem__("error", error))

    print(f"[separate] 開始: {audio_path}", flush=True)
    SeparateWorker(audio_path, audio_path, stems, signals).run()
    if result["error"]:
        print(f"[error] 音源分離失敗: {audio_path}\n{result['error']}", file=sys.stderr)
        return False
    if not result["done"]:
        print(f"[error] 音源分離が完了しませんでした: {audio_path}", file=sys.stderr)
        return False
    print(f"[separate] 完了: {Path(audio_path).parent}", flush=True)
    return True


def main() -> int:
    args = _configure_parser().parse_args()
    if args.command == "download":
        args.output_dir.mkdir(parents=True, exist_ok=True)
        succeeded = all(
            _download(url, args.output_dir, args.mp4, not args.flat, args.stems, args.playlist)
            for url in args.urls
        )
    elif args.command == "history":
        succeeded = _history()
    else:
        succeeded = all(_separate(str(path), args.stems) for path in args.files)
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())