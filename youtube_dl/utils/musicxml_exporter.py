"""
utils/musicxml_exporter.py
コード進行 → MusicXML 変換モジュール。

【小節（Bar/Measure）の概念と実装方針】
─────────────────────────────────────────────────────────────
  入力:
    chord_sequence = [{"start": 0.0, "end": 2.0, "chord": "C"}, ...]
    bpm            = 120.0（テンポ、Beat Per Minute）
    time_signature = "4/4"（拍子記号）

  計算ステップ:
    1. beat_dur [sec]   = 60.0 / bpm
    2. measure_dur [sec] = beat_dur × beats_per_measure

  music21 の構造（OSMD 互換）:
    Score
      └── Part (id="P1")
            ├── Measure 1: [Clef, KeySig, TimeSig, Tempo, Rest(whole)]
            │             + ChordSymbol アノテーション（insert で offset 指定）
            ├── Measure 2: [Rest(whole)] + ChordSymbol ...
            └── ...

  重要: OSMD は <note> 要素（Rest を含む）が無い Measure を invalid と判断する。
  ChordSymbol は <harmony> 要素として <note> の前に出力されるアノテーションであり、
  単独では音符として機能しない。そのため各小節には必ず Rest を入れる。
─────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import math
import os
import tempfile
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)


class MusicXMLExporter:
    """コード進行リストを Music21 経由で MusicXML に変換するクラス。

    Args:
        bpm: テンポ（BPM）
        time_signature: 拍子記号（例: "4/4", "3/4", "6/8"）
        key_signature: 調号（例: "C", "G", "F"）
    """

    def __init__(
        self,
        bpm: float = 120.0,
        time_signature: str = "4/4",
        key_signature: str = "C",
        quantize: int = 16,
        quantize_mode: str = "straight",
    ) -> None:
        self.bpm = bpm
        self.time_signature = time_signature
        self.key_signature = key_signature

        ts_parts = time_signature.split("/")
        self.beats_per_measure: int = int(ts_parts[0])
        self.beat_type: int = int(ts_parts[1])

        # 1拍の秒数（四分音符基準）
        self.beat_dur_sec: float = 60.0 / bpm
        # 1小節の秒数
        self.measure_dur_sec: float = self.beat_dur_sec * self.beats_per_measure

        # ピアノ譜の量子化グリッド（四分音符単位）
        # straight: 4/8=0.5, 4/16=0.25, 4/32=0.125 など
        # triplet : 4/12≈0.333, 4/24≈0.167, 4/48≈0.083 など
        self.quant: float = 4.0 / max(1, quantize)

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def export(self, chord_sequence: List[Dict], output_path: str) -> str:
        """コード進行を MusicXML ファイルとして書き出す。

        Args:
            chord_sequence: [{"start": float, "end": float, "chord": str}, ...]
            output_path: 保存先ファイルパス（.xml 推奨）

        Returns:
            書き出したファイルパスを返す
        """
        score = self._build_score(chord_sequence)
        score.write("musicxml", fp=output_path)
        return output_path

    def to_string(self, chord_sequence: List[Dict]) -> str:
        """MusicXML を文字列として返す（Streamlit ダウンロード用）。"""
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False, mode="w") as f:
            tmp_path = f.name
        try:
            self.export(chord_sequence, tmp_path)
            with open(tmp_path, "r", encoding="utf-8") as f:
                return f.read()
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    # ------------------------------------------------------------------
    # ピアノ譜 public API
    # ------------------------------------------------------------------

    def piano_str(self, chord_sequence: List[Dict]) -> str:
        """コード進行をピアノ大譜表（グランドスタッフ）の MusicXML 文字列として返す。

        各コードを実音（音符）で表記する:
          - 高音部譜表: コードの構成音を Oct4–5 でクローズドボイシング
          - 低音部譜表: コードのルート音を Oct2
        """
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False, mode="w") as f:
            tmp_path = f.name
        try:
            score = self._build_piano_score(chord_sequence)
            score.write("musicxml", fp=tmp_path)
            with open(tmp_path, "r", encoding="utf-8") as f:
                return f.read()
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def export_pdf(self, chord_sequence: List[Dict], output_path: str) -> str:
        """コード進行を MusicXML から PDF（楽譜）に書き出す。

        このメソッドは music21 に MuseScore か LilyPond が
        設定されている環境で動作します。

        output_path: 出力PDFパス（拡張子 .pdf 推奨）
        """
        from music21 import converter  # type: ignore

        # Score を構築
        score = self._build_piano_score(chord_sequence)

        # 一時 XML を介して、music21 の PDF 出力を行う
        with tempfile.NamedTemporaryFile(suffix=".musicxml", delete=False, mode="w") as f:
            tmp_xml = f.name
        try:
            score.write("musicxml", fp=tmp_xml)
            # music21 の書き出し先として PDF を指定
            score = converter.parse(tmp_xml)
            score.write("musicxml.pdf", fp=output_path)
            return output_path
        finally:
            if os.path.exists(tmp_xml):
                os.unlink(tmp_xml)

    def _build_piano_score(self, chord_sequence: List[Dict]):
        """ピアノ大譜表の Score を構築する。

        方針（inexpressible duration 回避）:
          - 全休符を先置きして insert() する方式を廃止。
          - 各小節を cursor で管理し、コードと休符を順番に append() する。
          - 全オフセット・長さを 16 分音符グリッド (QUANT=0.25) に丸める。
          - 1 小節内で音符が収まらない場合は次の小節へ持ち越す（タイなし）。
        """
        from music21 import (  # type: ignore
            stream, harmony, chord as m21chord, note, meter,
            tempo as m21tempo, key, clef, instrument, layout, duration
        )

        QUANT = self.quant  # 量子化グリッド（四分音符単位）

        def q(val: float) -> float:
            """値を QUANT 刻みに丸めて最低 QUANT を保証する。"""
            return max(QUANT, round(val / QUANT) * QUANT)

        def make_rest(ql: float) -> "note.Rest":
            r = note.Rest()
            r.quarterLength = q(ql)
            return r

        total_sec = chord_sequence[-1]["end"] if chord_sequence else self.measure_dur_sec
        total_measures = max(1, math.ceil(total_sec / self.measure_dur_sec))
        measure_ql = float(self.beats_per_measure)

        # Step 1: コード進行を (score_offset_ql, dur_ql, chord_name) に変換
        events: List = []
        for entry in chord_sequence:
            chord_name = str(entry.get("chord", "")).strip()
            if not chord_name:
                continue
            off_ql = round(float(entry["start"]) / self.beat_dur_sec / QUANT) * QUANT
            end_ql = round(float(entry["end"]) / self.beat_dur_sec / QUANT) * QUANT
            dur_ql = max(QUANT, end_ql - off_ql)
            events.append((off_ql, dur_ql, chord_name))
        events.sort(key=lambda x: x[0])

        # Step 2: 各小節ごとにコンテンツをリスト化
        #   [(is_chord: bool, ql: float, chord_name: str | None), ...]
        measure_contents: List[List] = [[] for _ in range(total_measures)]

        for ev_off, ev_dur, chord_name in events:
            placed = 0.0
            while placed < ev_dur:
                mi = min(int((ev_off + placed) / measure_ql), total_measures - 1)
                m_start  = mi * measure_ql
                off_in_m = round((ev_off + placed - m_start) / QUANT) * QUANT
                remaining_in_m = round((measure_ql - off_in_m) / QUANT) * QUANT
                chunk = min(q(ev_dur - placed), remaining_in_m)
                if chunk < QUANT:
                    break
                measure_contents[mi].append((off_in_m, chunk, chord_name))
                placed += chunk

        # Step 3: 各小節を cursor / append で構築
        treble_part = stream.Part()
        treble_part.id = "P1"
        treble_part.partName = "Piano"
        treble_part.insert(0, instrument.Piano())

        bass_part = stream.Part()
        bass_part.id = "P2"
        bass_part.partName = "Piano"
        bass_part.insert(0, instrument.Piano())

        for mi in range(total_measures):
            tm = stream.Measure(number=mi + 1)
            bm = stream.Measure(number=mi + 1)
            if mi == 0:
                tm.append(clef.TrebleClef())
                tm.append(key.Key(self.key_signature))
                tm.append(meter.TimeSignature(self.time_signature))
                tm.append(m21tempo.MetronomeMark(number=self.bpm))
                bm.append(clef.BassClef())
                bm.append(key.Key(self.key_signature))
                bm.append(meter.TimeSignature(self.time_signature))

            items = sorted(measure_contents[mi], key=lambda x: x[0])
            cursor = 0.0

            for off_in_m, dur, chord_name in items:
                gap = round((off_in_m - cursor) / QUANT) * QUANT
                if gap >= QUANT:
                    tm.append(make_rest(gap))
                    bm.append(make_rest(gap))
                    cursor += gap

                try:
                    cs = harmony.ChordSymbol(chord_name)
                    t_pits = self._voice_treble(cs)
                    tc = m21chord.Chord(t_pits)
                    tc.quarterLength = q(dur)
                    tm.append(tc)

                    root = cs.root()
                    bn = note.Note(f"{root.name}2") if root else make_rest(dur)
                    bn.quarterLength = q(dur)
                    bm.append(bn)
                except Exception:
                    tm.append(make_rest(dur))
                    bm.append(make_rest(dur))

                cursor += q(dur)

            # 末尾の残りを休符で埋める
            tail = round((measure_ql - cursor) / QUANT) * QUANT
            if tail >= QUANT:
                tm.append(make_rest(tail))
                bm.append(make_rest(tail))

            treble_part.append(tm)
            bass_part.append(bm)

        score = stream.Score()
        score.insert(0, treble_part)
        score.insert(0, bass_part)

        try:
            sg = layout.StaffGroup(
                [treble_part, bass_part],
                name="Piano", abbreviation="Pno.",
                symbol="brace",
            )
            sg.barTogether = True
            score.insert(0, sg)
        except Exception:
            pass

        return score


    def _voice_treble(self, cs) -> List:
        """ChordSymbol の構成音を高音部譜表用 Oct4–5 にボイシングして返す。"""
        from music21 import pitch as m21pitch  # type: ignore
        try:
            result = []
            seen = set()
            for p in cs.pitches:
                p2 = m21pitch.Pitch(p.name)
                # 音域 C4–B5 に収める
                p2.octave = 4
                if p2.ps < m21pitch.Pitch("C4").ps:
                    p2.octave = 5
                key = p2.nameWithOctave
                if key not in seen:
                    seen.add(key)
                    result.append(p2)
            return result if result else [m21pitch.Pitch("C4")]
        except Exception:
            from music21 import pitch as m21pitch  # type: ignore
            return [m21pitch.Pitch("C4")]

    # ------------------------------------------------------------------
    # コード譜 Score 構築
    # ------------------------------------------------------------------

    def _build_score(self, chord_sequence: List[Dict]):
        """music21 の Score オブジェクトを構築する。

        方針:
          1. 各小節にフル小節 Rest を置く（OSMD が <note> 要素を必要とするため）
          2. ChordSymbol を <harmony> アノテーションとして insert（quarterLength は設定しない）
          3. 複数小節にまたがるコードは各小節の先頭に繰り返し配置する
        """
        from music21 import stream, harmony, meter, tempo as m21tempo, key, note, clef  # type: ignore

        score = stream.Score()
        part = stream.Part()
        part.id = "P1"
        part.partName = "Chord Progression"

        total_sec = chord_sequence[-1]["end"] if chord_sequence else self.measure_dur_sec
        total_measures = max(1, math.ceil(total_sec / self.measure_dur_sec))
        measure_ql = float(self.beats_per_measure)

        # 全小節を生成し、フル小節 Rest を置く
        measures: list = []
        for i in range(total_measures):
            m = stream.Measure(number=i + 1)
            if i == 0:
                m.append(clef.TrebleClef())
                m.append(key.Key(self.key_signature))
                m.append(meter.TimeSignature(self.time_signature))
                m.append(m21tempo.MetronomeMark(number=self.bpm))
            r = note.Rest()
            r.quarterLength = measure_ql
            m.append(r)
            measures.append(m)

        # ChordSymbol アノテーションを挿入
        for entry in chord_sequence:
            t_start = float(entry["start"])
            t_end = float(entry["end"])
            chord_name = str(entry.get("chord", "")).strip()
            if not chord_name:
                continue

            # コードが複数小節にまたがる場合は各小節の頭に配置
            m_start = min(int(t_start / self.measure_dur_sec), total_measures - 1)
            m_end = min(int(t_end / self.measure_dur_sec), total_measures - 1)

            for mi in range(m_start, m_end + 1):
                if mi == m_start:
                    offset_ql = (t_start - mi * self.measure_dur_sec) / self.beat_dur_sec
                else:
                    offset_ql = 0.0
                offset_ql = max(0.0, min(float(offset_ql), measure_ql - 0.01))

                try:
                    cs = harmony.ChordSymbol(chord_name)
                    # quarterLength は設定しない（アノテーションとして扱う）
                    measures[mi].insert(offset_ql, cs)
                except Exception:
                    pass  # パース不能なコード名はスキップ

        for m in measures:
            part.append(m)

        score.append(part)
        return score
