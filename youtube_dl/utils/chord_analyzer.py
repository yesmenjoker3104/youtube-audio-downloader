"""
utils/chord_analyzer.py
コード進行解析モジュール。

優先順位:
  1. Omnizart（高精度・딥러닝ベース）
  2. librosa chroma特徴量（フォールバック・軽量）

返り値は共通フォーマット:
  List[{"start": float, "end": float, "chord": str}]
  （start/endは秒単位）
"""
from __future__ import annotations

import importlib
import logging
from typing import List, Dict

import numpy as np
import librosa

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 公開クラス
# ---------------------------------------------------------------------------

class ChordAnalyzer:
    """コード進行を解析するクラス。

    Args:
        backend: "omnizart" | "librosa" | "auto"
            "auto" の場合は Omnizart が利用可能ならそちらを使い、
            インストールされていなければ librosa にフォールバックする。
        segment_duration: librosa モード時の解析セグメント長（秒）。
    """

    def __init__(
        self,
        backend: str = "auto",
        segment_duration: float = 1.0,
    ) -> None:
        self.backend = self._resolve_backend(backend)
        self.segment_duration = segment_duration
        logger.info("ChordAnalyzer backend: %s", self.backend)

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def analyze(self, audio_path: str) -> List[Dict]:
        """コード進行を解析する。

        Args:
            audio_path: 解析対象の音声ファイルパス（WAV/MP3 等）

        Returns:
            [{"start": float, "end": float, "chord": str}, ...] のリスト
        """
        if self.backend == "omnizart":
            return self._analyze_omnizart(audio_path)
        return self._analyze_librosa(audio_path)

    def estimate_bpm(self, audio_path: str) -> float:
        """librosa でテンポ（BPM）を推定する。"""
        y, sr = librosa.load(audio_path, sr=22050, mono=True)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        # tempo は numpy array の場合があるので float に変換
        return float(np.atleast_1d(tempo)[0])

    # ------------------------------------------------------------------
    # Omnizart backend
    # ------------------------------------------------------------------

    def _analyze_omnizart(self, audio_path: str) -> List[Dict]:
        """Omnizart のコード認識モジュールを呼び出す。"""
        try:
            from omnizart.chord import app as chord_app  # type: ignore
        except ImportError:
            logger.warning("Omnizart が見つかりません。librosa にフォールバックします。")
            return self._analyze_librosa(audio_path)

        try:
            # Omnizart は内部で最終的に MIDI や JSON を返す実装が多いため
            # transcribe を呼び出してから結果を正規化する
            midi_path = chord_app.transcribe(audio_path)
            return self._parse_omnizart_midi(midi_path)
        except Exception as exc:
            logger.warning(
                "Omnizart の実行に失敗しました (%s)。librosa にフォールバックします。", exc
            )
            return self._analyze_librosa(audio_path)

    def _parse_omnizart_midi(self, midi_path: str) -> List[Dict]:
        """Omnizart が出力した MIDI からコード進行リストを生成する。"""
        import pretty_midi  # type: ignore

        pm = pretty_midi.PrettyMIDI(midi_path)
        results: List[Dict] = []

        for instrument in pm.instruments:
            for note in instrument.notes:
                # Omnizart のコード出力は pitch_name がコード名になっている場合がある
                chord_name = pretty_midi.note_number_to_name(note.pitch)
                results.append(
                    {
                        "start": note.start,
                        "end": note.end,
                        "chord": chord_name,
                    }
                )

        return sorted(results, key=lambda x: x["start"])

    # ------------------------------------------------------------------
    # librosa backend
    # ------------------------------------------------------------------

    def _analyze_librosa(self, audio_path: str) -> List[Dict]:
        """chroma CQT 特徴量からコード進行を推定する（軽量フォールバック）。

        アルゴリズム概要:
          1. CQT chroma を計算する。
          2. segment_duration 秒ごとにフレームを集約し、
             12 次元の平均 chroma ベクトルを得る。
          3. メジャー／マイナーのテンプレートとの内積でコード種別を判定する。
        """
        y, sr = librosa.load(audio_path, sr=22050, mono=True)

        hop_length = 2048
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop_length)
        frame_times = librosa.frames_to_time(
            np.arange(chroma.shape[1]), sr=sr, hop_length=hop_length
        )
        total_duration = float(frame_times[-1]) if len(frame_times) > 0 else 0.0

        results: List[Dict] = []
        seg = self.segment_duration
        num_segments = max(1, int(np.ceil(total_duration / seg)))

        for i in range(num_segments):
            t_start = i * seg
            t_end = min((i + 1) * seg, total_duration)

            f_start = int(np.searchsorted(frame_times, t_start))
            f_end = int(np.searchsorted(frame_times, t_end))
            f_end = min(f_end, chroma.shape[1])

            if f_start >= f_end:
                continue

            seg_chroma = chroma[:, f_start:f_end].mean(axis=1)
            chord_name = self._chroma_to_chord(seg_chroma)

            results.append({"start": t_start, "end": t_end, "chord": chord_name})

        return results

    # ------------------------------------------------------------------
    # 内部ユーティリティ
    # ------------------------------------------------------------------

    # メジャー・マイナー chroma テンプレート（ルート = 0 基準）
    _MAJOR = np.array([1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0], dtype=float)
    _MINOR = np.array([1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0], dtype=float)
    _NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

    def _chroma_to_chord(self, chroma_vec: np.ndarray) -> str:
        """12次元のchromaベクトルからコード名を推定する。"""
        best_score = -np.inf
        best_chord = "C"

        for root in range(12):
            rotated = np.roll(chroma_vec, -root)
            major_score = float(np.dot(rotated, self._MAJOR))
            minor_score = float(np.dot(rotated, self._MINOR))

            if major_score >= minor_score and major_score > best_score:
                best_score = major_score
                best_chord = self._NOTE_NAMES[root]
            elif minor_score > major_score and minor_score > best_score:
                best_score = minor_score
                best_chord = f"{self._NOTE_NAMES[root]}m"

        return best_chord

    @staticmethod
    def _resolve_backend(backend: str) -> str:
        if backend == "auto":
            omnizart_available = importlib.util.find_spec("omnizart") is not None
            return "omnizart" if omnizart_available else "librosa"
        return backend
