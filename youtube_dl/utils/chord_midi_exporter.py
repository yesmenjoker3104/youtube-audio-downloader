"""
utils/chord_midi_exporter.py
コード進行（start/end/chord）から MIDI を生成するモジュール。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List


class ChordMIDIExporter:
    """コード進行を MIDI に変換するクラス。"""

    def __init__(self, bpm: float = 120.0) -> None:
        self.bpm = bpm

    def export(self, chord_sequence: List[Dict], output_path: str) -> str:
        """コード進行を MIDI ファイルとして書き出す。"""
        import pretty_midi  # type: ignore
        from music21 import harmony  # type: ignore

        pm = pretty_midi.PrettyMIDI(initial_tempo=self.bpm)
        inst = pretty_midi.Instrument(program=0, is_drum=False, name="Chord Progression")

        for entry in chord_sequence:
            start = float(entry.get("start", 0.0))
            end = float(entry.get("end", start + 0.5))
            chord_name = str(entry.get("chord", "")).strip()

            if end <= start or not chord_name:
                continue

            try:
                cs = harmony.ChordSymbol(chord_name)
                pitches = cs.pitches
            except Exception:
                continue

            # 右手: C4-B5 の近接ボイシング
            for p in pitches:
                midi_num = p.midi
                while midi_num < 60:
                    midi_num += 12
                while midi_num > 83:
                    midi_num -= 12
                inst.notes.append(
                    pretty_midi.Note(
                        velocity=85,
                        pitch=int(midi_num),
                        start=start,
                        end=end,
                    )
                )

            # 左手: ルート音を2オクターブ下
            try:
                root = cs.root()
                if root is not None:
                    bass_midi = int(root.midi)
                    while bass_midi > 48:
                        bass_midi -= 12
                    while bass_midi < 36:
                        bass_midi += 12
                    inst.notes.append(
                        pretty_midi.Note(
                            velocity=75,
                            pitch=bass_midi,
                            start=start,
                            end=end,
                        )
                    )
            except Exception:
                pass

        if not inst.notes:
            # 最低限無音MIDIを避けるため C メジャーを1拍入れる
            inst.notes.append(pretty_midi.Note(velocity=70, pitch=60, start=0.0, end=0.5))

        pm.instruments.append(inst)
        os.makedirs(str(Path(output_path).parent), exist_ok=True)
        pm.write(output_path)
        return output_path
