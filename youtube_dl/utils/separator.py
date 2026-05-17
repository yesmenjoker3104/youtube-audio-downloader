"""
utils/separator.py
DemucsによるAI音源分離モジュール。
Vocal / Drums / Bass / Other の4ステムに分離する。
"""
from __future__ import annotations

import os
import subprocess
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class AudioSeparator:
    """Demucsを使って音楽ファイルをステムに分離するクラス。

    Args:
        model: Demucsモデル名。デフォルトは "htdemucs"（高品質・汎用）。
               他の選択肢: "htdemucs_ft"（ファインチューニング版）,
               "htdemucs_6s"（6ステム: guitar/piano を個別分離）,
               "mdx_extra" など。
        device: "cuda" または "cpu"。GPU が使えれば自動的に cuda を選択。
    """

    STEMS_4S = ("vocals", "drums", "bass", "other")
    STEMS_6S = ("vocals", "drums", "bass", "guitar", "piano", "other")
    STEMS = STEMS_4S  # 後方互換

    MODEL_STEMS = {
        "htdemucs":    ("vocals", "drums", "bass", "other"),
        "htdemucs_ft": ("vocals", "drums", "bass", "other"),
        "htdemucs_6s": ("vocals", "drums", "bass", "guitar", "piano", "other"),
        "mdx_extra":   ("vocals", "drums", "bass", "other"),
        "mdx":         ("vocals", "drums", "bass", "other"),
    }
    # 4ステムモデルでギター/ピアノ分解する際に2段階目で使用するモデル
    GUITAR_PIANO_MODEL = "htdemucs_6s"

    def __init__(self, model: str = "htdemucs", device: str = "auto") -> None:
        self.model = model
        self.device = self._resolve_device(device)
        self._model_stems: tuple = self.MODEL_STEMS.get(model, self.STEMS_4S)

    # ------------------------------------------------------------------
    # public methods
    # ------------------------------------------------------------------

    def separate(self, audio_path: str, output_dir: str, requested_stems: Optional[List[str]] = None) -> Dict[str, str]:
        """音源分離を実行する。

        Args:
            audio_path: 入力音声ファイルのパス（MP3 / WAV / FLAC など）
            output_dir: 分離結果の出力先ルートディレクトリ
            requested_stems: 取得したいステム名のリスト（None の場合は全ステムを返す）

        Returns:
            ステム名 -> ファイルパス の辞書
            例: {"vocals": "/tmp/out/vocals.wav", "drums": "/tmp/out/drums.wav", ...}

        Raises:
            RuntimeError: Demucsの実行に失敗した場合
            FileNotFoundError: demucsコマンドが見つからない場合
        """
        if shutil.which("demucs") is None:
            raise FileNotFoundError(
                "demucs コマンドが見つかりません。"
                " `pip install demucs` でインストールしてください。"
            )

        audio_path = str(Path(audio_path).resolve())
        output_dir = str(Path(output_dir).resolve())
        os.makedirs(output_dir, exist_ok=True)

        # torchaudio 2.5+ は MP3 の直接読み込みに torchcodec を要求するため、
        # pydub で WAV に変換してから demucs に渡す
        audio_path = self._ensure_wav(audio_path, output_dir)

        cmd = [
            "demucs",
            "--name", self.model,
            "--out", output_dir,
            "--device", self.device,
            audio_path,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=self._build_demucs_env(),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Demucs の実行に失敗しました。\n"
                f"stdout: {result.stdout}\n"
                f"stderr: {result.stderr}"
            )

        stems = self._collect_output_paths(audio_path, output_dir)
        if requested_stems:
            stems = {k: v for k, v in stems.items() if k in requested_stems}
        return stems

    def separate_with_guitar_piano(
        self, audio_path: str, output_dir: str, requested_stems: Optional[List[str]] = None
    ) -> Dict[str, str]:
        """2段階分離：指定モデルで基本ステムを分離したあと、
        htdemucs_6s で "other" ステムをギター/ピアノにさらに分割する。

        すでに 6s モデルを選択中の場合は通常の separate() と等価。

        Returns:
            {"vocals": ..., "drums": ..., "bass": ...,
             "guitar": ..., "piano": ..., "other": ...} 相当の辞書
        """
        if self.GUITAR_PIANO_MODEL in self.model or self.model == self.GUITAR_PIANO_MODEL:
            # 既に6sモデルなのでそのまま
            return self.separate(audio_path, output_dir, requested_stems)

        # --- 1段階目: ユーザー指定モデルで基本分離 ---
        first_stems = self.separate(audio_path, output_dir)

        other_path = first_stems.get("other")
        if not other_path or not Path(other_path).exists():
            # "other" が無ければそのまま返す
            if requested_stems:
                first_stems = {k: v for k, v in first_stems.items() if k in requested_stems}
            return first_stems

        # --- 2段階目: htdemucs_6s で "other" をギター/ピアノに分割 ---
        second_dir = str(Path(output_dir) / "_2nd_pass")
        os.makedirs(second_dir, exist_ok=True)

        second_sep = AudioSeparator(model=self.GUITAR_PIANO_MODEL, device=self.device)
        second_stems = second_sep.separate(other_path, second_dir)

        # "other" を2段階目の結果で置き換え / 追加
        merged: Dict[str, str] = dict(first_stems)
        for key in ("guitar", "piano", "other"):
            if key in second_stems:
                merged[key] = second_stems[key]

        if requested_stems:
            merged = {k: v for k, v in merged.items() if k in requested_stems}
        return merged

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _build_demucs_env(self) -> Dict[str, str]:
        """Demucs 実行用の環境変数を構築する。

        torchaudio + torchcodec は FFmpeg の shared DLL (avcodec*.dll など) が
        PATH 上に必要なため、winget の shared 版が見つかれば優先的に先頭へ追加する。
        """
        env = os.environ.copy()

        # frozen EXE の場合、モデルキャッシュを exe 隣の models/ フォルダに集約する
        if getattr(sys, 'frozen', False):
            models_dir = str(Path(sys.executable).parent / 'models')
            env.setdefault('TORCH_HOME', models_dir)
        path_parts = []

        shared_candidates = [
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Microsoft"
            / "WinGet"
            / "Packages"
            / "Gyan.FFmpeg.Shared_Microsoft.Winget.Source_8wekyb3d8bbwe"
            / "ffmpeg-8.0.1-full_build-shared"
            / "bin",
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Microsoft"
            / "WinGet"
            / "Packages"
            / "Gyan.FFmpeg.Shared_Microsoft.Winget.Source_8wekyb3d8bbwe"
            / "ffmpeg-8.0.1-essentials_build-shared"
            / "bin",
        ]

        for p in shared_candidates:
            if (p / "avcodec-62.dll").exists() or (p / "avcodec-61.dll").exists():
                path_parts.append(str(p))

        ffmpeg_path, _ = self._find_ffmpeg_tools()
        if ffmpeg_path:
            path_parts.append(str(Path(ffmpeg_path).parent))

        current_path = env.get("PATH", "")
        if path_parts:
            unique_parts = list(dict.fromkeys(path_parts))
            env["PATH"] = os.pathsep.join(unique_parts + [current_path])

        return env

    def _ensure_wav(self, audio_path: str, output_dir: str) -> str:
        """WAV 以外のフォーマットを WAV に変換して返す。
        すでに WAV であればそのまま返す。
        """
        if not Path(audio_path).exists():
            raise RuntimeError(f"入力音声ファイルが見つかりません: {audio_path}")

        if Path(audio_path).suffix.lower() == ".wav":
            return audio_path

        wav_path = str(Path(output_dir) / (Path(audio_path).stem + "_input.wav"))
        try:
            from pydub import AudioSegment  # type: ignore

            ffmpeg_path, ffprobe_path = self._find_ffmpeg_tools()
            if ffmpeg_path:
                # PATH に無くても pydub が実行できるように実ファイルを指定する
                AudioSegment.converter = ffmpeg_path
                AudioSegment.ffmpeg = ffmpeg_path
                ffmpeg_bin = str(Path(ffmpeg_path).parent)
                os.environ["PATH"] = os.pathsep.join([ffmpeg_bin, os.environ.get("PATH", "")])
            if ffprobe_path:
                AudioSegment.ffprobe = ffprobe_path

            audio = AudioSegment.from_file(audio_path)
            audio.export(wav_path, format="wav")
            return wav_path
        except Exception as exc:
            ffmpeg_path, _ = self._find_ffmpeg_tools()
            ffmpeg_hint = (
                f"検出済み ffmpeg: {ffmpeg_path}" if ffmpeg_path
                else "ffmpeg.exe が見つかりませんでした。"
            )
            raise RuntimeError(
                f"音声ファイルの WAV 変換に失敗しました: {exc}\n"
                "pydub と FFmpeg が正しくインストールされているか確認してください。\n"
                f"{ffmpeg_hint}"
            ) from exc

    def _find_ffmpeg_tools(self) -> Tuple[Optional[str], Optional[str]]:
        """ffmpeg / ffprobe 実行ファイルを探索して返す。"""
        # PyInstaller frozen exe の場合、exe と同じフォルダに同梱した ffmpeg を最優先
        if getattr(sys, 'frozen', False):
            _exe_dir = Path(sys.executable).parent
            _ff  = _exe_dir / 'ffmpeg.exe'
            _ffp = _exe_dir / 'ffprobe.exe'
            if _ff.exists() and _ffp.exists():
                return str(_ff), str(_ffp)

        ffmpeg_path = shutil.which("ffmpeg")
        ffprobe_path = shutil.which("ffprobe")
        if ffmpeg_path and ffprobe_path:
            return ffmpeg_path, ffprobe_path

        candidate_bins = []

        # winget 版 ffmpeg（full/essentials/shared いずれも対象）
        localappdata = os.environ.get("LOCALAPPDATA", "")
        if localappdata:
            winget_root = Path(localappdata) / "Microsoft" / "WinGet" / "Packages"
            if winget_root.exists():
                for pkg in winget_root.glob("Gyan.FFmpeg*"):
                    for ffmpeg_exe in pkg.glob("ffmpeg-*/bin/ffmpeg.exe"):
                        candidate_bins.append(ffmpeg_exe.parent)

        # よく使われる配置先
        program_files = os.environ.get("ProgramFiles", "")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", "")
        user_profile = os.environ.get("USERPROFILE", "")
        common_bins = [
            Path("C:/ffmpeg/bin"),
            Path(program_files) / "ffmpeg" / "bin" if program_files else None,
            Path(program_files_x86) / "ffmpeg" / "bin" if program_files_x86 else None,
            Path(user_profile) / "scoop" / "shims" if user_profile else None,
            Path("C:/ProgramData/chocolatey/bin"),
        ]
        for p in common_bins:
            if p:
                candidate_bins.append(p)

        seen = set()
        ordered_bins = []
        for p in candidate_bins:
            p_str = str(p)
            if p_str in seen:
                continue
            seen.add(p_str)
            ordered_bins.append(p)

        for bin_dir in ordered_bins:
            ffmpeg_exe = bin_dir / "ffmpeg.exe"
            ffprobe_exe = bin_dir / "ffprobe.exe"
            if ffmpeg_exe.exists() and ffprobe_exe.exists():
                return str(ffmpeg_exe), str(ffprobe_exe)

        if ffmpeg_path and not ffprobe_path:
            ffprobe_candidate = Path(ffmpeg_path).with_name("ffprobe.exe")
            if ffprobe_candidate.exists():
                ffprobe_path = str(ffprobe_candidate)

        return ffmpeg_path, ffprobe_path

    def _resolve_device(self, device: str) -> str:
        if device != "auto":
            return device
        try:
            import torch
            return "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            return "cpu"

    def _collect_output_paths(self, audio_path: str, output_dir: str) -> Dict[str, str]:
        """Demucsが出力したステムファイルのパスを収集する。

        Demucsの出力ディレクトリ構造:
            {output_dir}/{model}/{track_name}/{stem}.wav
        """
        track_name = Path(audio_path).stem
        sep_dir = Path(output_dir) / self.model / track_name

        if not sep_dir.exists():
            raise RuntimeError(
                f"分離結果のディレクトリが見つかりません: {sep_dir}\n"
                "Demucsの出力形式が変わった可能性があります。"
            )

        stems: Dict[str, str] = {}
        for stem in self._model_stems:
            stem_path = sep_dir / f"{stem}.wav"
            if stem_path.exists():
                stems[stem] = str(stem_path)

        if not stems:
            raise RuntimeError(f"分離された音源ファイルが見つかりません: {sep_dir}")

        return stems
