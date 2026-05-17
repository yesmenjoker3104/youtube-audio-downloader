"""
utils/drum_score.py
ドラム MIDI → MusicXML（パーカッション譜）変換モジュール。

GM ドラムマップ → 五線上の表示位置（displayStep / displayOctave）に変換し、
music21 の Unpitched ノートで MusicXML を生成する。
"""
from __future__ import annotations

import math
import os
import tempfile
import logging
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GM ドラムマップ → (displayStep, displayOctave, noteHead, stemDirection)
# ---------------------------------------------------------------------------
# 表示位置は Standard Drum Notation 準拠の5線上レイアウト:
#
#   シンバル系（×音符頭）
#     Crash   : A5 (x, up)      Ride    : D5 (x, up)
#     Open HH : G5 (x, up)      Closed HH / Pedal HH : F5 (x, up)
#     Chinese : F5 (x, up)      Splash  : A5 (x, up)
#
#   タム系（通常音符頭）
#     High Tom     : A4 (normal, up)     Hi-Mid Tom   : G4 (normal, up)
#     Mid Tom      : F4 (normal, up)     Low-Mid Tom  : E4 → スネアと吐離のため D4
#     Hi Floor Tom : B3 (normal, down)   Low Floor Tom: A3 (normal, down)
#
#   スネア／キック
#     Snare   : C4 (normal, up)     Side Stick : C4 (x, up)
#     Kick    : F3 (normal, down)
# ---------------------------------------------------------------------------
_GM: Dict[int, Tuple[str, int, str, str]] = {
    # --- キック ---
    35: ("F", 3, "normal", "down"),   # Acoustic Bass Drum
    36: ("F", 3, "normal", "down"),   # Bass Drum 1
    # --- スネア ---
    37: ("C", 4, "x",      "up"),     # Side Stick
    38: ("C", 4, "normal", "up"),     # Acoustic Snare
    39: ("C", 4, "x",      "up"),     # Hand Clap
    40: ("C", 4, "normal", "up"),     # Electric Snare
    # --- タム類 ---
    41: ("A", 3, "normal", "down"),   # Low Floor Tom   (最下段)
    43: ("B", 3, "normal", "down"),   # High Floor Tom
    45: ("D", 4, "normal", "up"),     # Low Tom
    47: ("E", 4, "normal", "up"),     # Low-Mid Tom     (スネア上の隙間)
    48: ("G", 4, "normal", "up"),     # Hi-Mid Tom
    50: ("A", 4, "normal", "up"),     # High Tom        (最上段)
    # --- ハイハット ---
    42: ("F", 5, "x",      "up"),     # Closed Hi-Hat
    44: ("F", 5, "x",      "up"),     # Pedal Hi-Hat
    46: ("G", 5, "x",      "up"),     # Open Hi-Hat
    # --- ライド／クラッシュ ---
    49: ("A", 5, "x",      "up"),     # Crash Cymbal 1
    51: ("D", 5, "x",      "up"),     # Ride Cymbal 1
    52: ("F", 5, "x",      "up"),     # Chinese Cymbal
    53: ("D", 5, "x",      "up"),     # Ride Bell
    55: ("A", 5, "x",      "up"),     # Splash Cymbal
    57: ("A", 5, "x",      "up"),     # Crash Cymbal 2
    59: ("D", 5, "x",      "up"),     # Ride Cymbal 2
}
_DEFAULT_DRUM = ("C", 4, "normal", "up")  # 未定義 pitch は Snare 相当


class DrumScoreExporter:
    """ドラム MIDI ファイルを MusicXML のパーカッション譜に変換するクラス。

    Args:
        bpm: テンポ（BPM）
        time_signature: 拍子記号（例: "4/4"）
        quantize: 量子化単位（16 = 16分音符、8 = 8分音符）
    """

    def __init__(
        self,
        bpm: float = 120.0,
        time_signature: str = "4/4",
        quantize: int = 32,
    ) -> None:
        self.bpm = bpm
        self.time_signature = time_signature
        self.quantize = quantize

        ts = time_signature.split("/")
        self.beats_per_measure = int(ts[0])
        self.beat_dur_sec = 60.0 / bpm
        self.measure_dur_sec = self.beat_dur_sec * self.beats_per_measure
        self.quant_ql = 4.0 / quantize  # 量子化単位（四分音符単位）

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def export(self, midi_path: str, output_path: str) -> str:
        score = self._build_score(midi_path)
        score.write("musicxml", fp=output_path)
        return output_path

    def to_string(self, midi_path: str) -> str:
        """MusicXML を文字列として返す。"""
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False, mode="w") as f:
            tmp_path = f.name
        try:
            self.export(midi_path, tmp_path)
            with open(tmp_path, "r", encoding="utf-8") as f:
                return f.read()
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

    def _build_score(self, midi_path: str):
        from music21 import stream, note, meter, tempo as m21tempo, clef, instrument  # type: ignore

        drum_notes = self._load_midi(midi_path)
        total_end = max((t for t, _, _ in drum_notes), default=self.measure_dur_sec) + self.beat_dur_sec
        total_measures = max(1, math.ceil(total_end / self.measure_dur_sec))
        measure_ql = float(self.beats_per_measure)

        score = stream.Score()
        part = stream.Part()
        part.id = "P1"
        part.partName = "Drums"
        part.insert(0, instrument.Percussion())

        measures: List = []
        for i in range(total_measures):
            m = stream.Measure(number=i + 1)
            if i == 0:
                m.append(clef.PercussionClef())
                m.append(meter.TimeSignature(self.time_signature))
                m.append(m21tempo.MetronomeMark(number=self.bpm))
            # フル小節 Rest（音符が必ず存在するよう保証）
            r = note.Rest()
            r.quarterLength = measure_ql
            m.append(r)
            measures.append(m)

        for t_sec, pitch, _velocity in drum_notes:
            mi = min(int(t_sec / self.measure_dur_sec), total_measures - 1)
            offset_raw = (t_sec - mi * self.measure_dur_sec) / self.beat_dur_sec
            # 量子化（最近傍スナップ）
            offset_ql = round(offset_raw / self.quant_ql) * self.quant_ql
            offset_ql = max(0.0, min(offset_ql, measure_ql - self.quant_ql))

            step, octave, head, stem_dir = _GM.get(pitch, _DEFAULT_DRUM)

            un = note.Unpitched()
            un.displayStep = step
            un.displayOctave = octave
            un.quarterLength = self.quant_ql
            un.notehead = head
            un.stemDirection = stem_dir

            measures[mi].insert(offset_ql, un)

        for m in measures:
            part.append(m)

        score.append(part)
        return score

    def _load_midi(self, midi_path: str) -> List[Tuple[float, int, int]]:
        """pretty_midi でドラムトラックのノートを読み込む。"""
        try:
            import pretty_midi  # type: ignore
            pm = pretty_midi.PrettyMIDI(midi_path)
            notes = []
            for inst in pm.instruments:
                if inst.is_drum:
                    for n in inst.notes:
                        notes.append((n.start, n.pitch, n.velocity))
            return sorted(notes, key=lambda x: x[0])
        except Exception as exc:
            logger.warning("ドラム MIDI 読み込み失敗: %s", exc)
            return []
