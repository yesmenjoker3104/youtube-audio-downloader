# utils package
# 各モジュールは環境によっては存在しない依存を持つため try/except でガードする。
# (例: youtube_dl EXE 環境では librosa / music21 等が未インストール)

from .separator import AudioSeparator

try:
    from .chord_analyzer import ChordAnalyzer
except Exception:
    ChordAnalyzer = None  # type: ignore

try:
    from .drum_transcriber import DrumTranscriber
except Exception:
    DrumTranscriber = None  # type: ignore

try:
    from .musicxml_exporter import MusicXMLExporter
except Exception:
    MusicXMLExporter = None  # type: ignore

try:
    from .drum_score import DrumScoreExporter
except Exception:
    DrumScoreExporter = None  # type: ignore

try:
    from .chord_midi_exporter import ChordMIDIExporter
except Exception:
    ChordMIDIExporter = None  # type: ignore

try:
    from .musescore_exporter import xml_to_pdf_bytes, is_available as musescore_available
except Exception:
    xml_to_pdf_bytes = None   # type: ignore
    musescore_available = None  # type: ignore

__all__ = [
    "AudioSeparator",
    "ChordAnalyzer",
    "DrumTranscriber",
    "MusicXMLExporter",
    "DrumScoreExporter",
    "ChordMIDIExporter",
    "xml_to_pdf_bytes",
    "musescore_available",
]
