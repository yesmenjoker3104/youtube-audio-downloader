"""
utils/drum_transcriber.py
ドラム採譜モジュール。

優先順位:
  1. Omnizart drum モジュール（딥러닝ベース高精度）
  2. librosa オンセット検出ベースの簡易採譜（フォールバック）

出力: MIDI ファイルパス
"""
from __future__ import annotations

import importlib
import logging
import os
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import librosa

logger = logging.getLogger(__name__)


class DrumTranscriber:
    """ドラム音源をMIDIに変換するクラス。

    Args:
        backend: "omnizart" | "librosa" | "auto"
    """

    # GM ドラムマップ
    _GM_KICK = 36
    _GM_SNARE = 38
    _GM_HIHAT_CLOSED = 42
    _GM_HIHAT_OPEN = 46
    _GM_TOM_LOW = 45
    _GM_TOM_MID = 47
    _GM_TOM_HIGH = 50
    _GM_CRASH = 49
    _GM_RIDE = 51

    def __init__(
        self,
        backend: str = "auto",
        quantize: int = 32,
        quantize_mode: str = "straight",
    ) -> None:
        self.backend = self._resolve_backend(backend)
        self.quantize_mode = (
            quantize_mode.lower() if quantize_mode.lower() in ("straight", "triplet") else "straight"
        )
        valid = (8, 16, 32, 64) if self.quantize_mode == "straight" else (12, 24, 48)
        self.quantize = quantize if quantize in valid else valid[1]
        logger.info("DrumTranscriber backend: %s", self.backend)

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def transcribe(self, drums_audio_path: str, output_dir: str) -> str:
        """ドラム音源を MIDI ファイルに変換する。

        Args:
            drums_audio_path: Demucsが出力したドラム音源ファイルのパス
            output_dir: MIDI ファイルの出力先ディレクトリ

        Returns:
            生成した MIDI ファイルのパス
        """
        os.makedirs(output_dir, exist_ok=True)
        out_name = Path(drums_audio_path).stem + "_drums.mid"
        out_path = str(Path(output_dir) / out_name)

        if self.backend == "omnizart":
            result = self._transcribe_omnizart(drums_audio_path, out_path)
            if result:
                return result

        return self._transcribe_librosa(drums_audio_path, out_path)

    # ------------------------------------------------------------------
    # Omnizart backend
    # ------------------------------------------------------------------

    def _transcribe_omnizart(self, audio_path: str, out_path: str) -> str | None:
        """Omnizart の drum 認識を呼び出す。"""
        try:
            from omnizart.drum import app as drum_app  # type: ignore
        except ImportError:
            logger.warning("Omnizart が見つかりません。librosa にフォールバックします。")
            return None

        try:
            midi_path = drum_app.transcribe(audio_path, output=out_path)
            return str(midi_path)
        except Exception as exc:
            logger.warning("Omnizart drum 失敗 (%s)。librosa にフォールバックします。", exc)
            return None

    # ------------------------------------------------------------------
    # librosa backend（簡易オンセット検出）
    # ------------------------------------------------------------------

    def _transcribe_librosa(self, audio_path: str, out_path: str) -> str:
        """HPSS (調波/打楽器音分離) とスペクトル特徴量による高精度採譜。
        
        1. HPSSでドラム成分（Percussive）を強調し、他の楽器の残響をカット。
        2. 全体のオンセットを検出し、その瞬間の周波数バランス（重心・帯域エネルギー）で楽器を判定。
        3. キック＋シンバルのような同時発音も考慮。
        """
        import pretty_midi  # type: ignore

        # 1. 音声をロード & HPSS分離
        y, sr = librosa.load(audio_path, sr=44100, mono=True)
        # marginを高めにして、アタック成分を鋭く残す
        _, y_percussive = librosa.effects.hpss(y, margin=3.0)
        
        # 2. オンセット検出 (Percussive成分使用)
        # hop_length=256 (約5.8ms精度) で細かく見る
        hop_length = 256
        onset_env = librosa.onset.onset_strength(y=y_percussive, sr=sr, hop_length=hop_length, aggregate=np.median)
        
        # ピーク検出: wait=3 (約17ms) で極端な連打連打誤検知を防ぐ
        detection_frames = librosa.util.peak_pick(
            onset_env,
            pre_max=3, post_max=3,
            pre_avg=5, post_avg=5,
            delta=0.02, # 感度
            wait=3
        )
        onset_times = librosa.frames_to_time(detection_frames, sr=sr, hop_length=hop_length)

        # 3. 特徴量抽出 (分析用)
        n_fft = 2048
        S = np.abs(librosa.stft(y_percussive, n_fft=n_fft, hop_length=hop_length))
        freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
        
        # スペクトル重心 (Brightness)
        centroid = librosa.feature.spectral_centroid(S=S, sr=sr, n_fft=n_fft)[0]
        
        # 帯域ごとのエネルギー (Kick, Snare, HH)
        # Kick: 20-120Hz
        # Snare(Body): 200-500Hz
        # HH/Cymbal: 6000Hz+
        bin_kick = (freqs >= 20) & (freqs < 120)
        bin_snare = (freqs >= 200) & (freqs < 600)
        bin_hh = (freqs >= 6000)

        detected_events = []
        
        # 4. テンポ & グリッド
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(np.atleast_1d(tempo)[0]) if tempo is not None else 120.0
        if bpm <= 50: bpm = 120.0
        grid = 60.0 / bpm * (4.0 / float(self.quantize))

        for frame, t in zip(detection_frames, onset_times):
            # 前後フレーム含めた平均特徴量
            w_left = max(0, frame - 1)
            w_right = min(S.shape[1], frame + 2)
            
            # 各帯域のエネルギー比率計算
            # 局所的なスペクトルの合計
            s_local = np.mean(S[:, w_left:w_right], axis=1)
            total_E = np.sum(s_local) + 1e-9
            
            E_kick = np.sum(s_local[bin_kick]) / total_E
            E_snare = np.sum(s_local[bin_snare]) / total_E
            E_hh = np.sum(s_local[bin_hh]) / total_E
            
            c_local = np.mean(centroid[w_left:w_right])
            
            # Velocity: オンセット強度の相対値 (0.0~1.0 -> 0~127)
            # ピーク周辺の最大値を取る
            peak_val = np.max(onset_env[max(0,frame-1):min(len(onset_env),frame+2)])
            # 正規化係数 (適当なゲイン)
            vel = int(np.clip(peak_val * 30 + 40, 40, 127))

            # --- 分類ロジック (Voting) ---
            
            # [Kick] 低域エネルギーが支配的 or 重心が極端に低い
            is_kick = (E_kick > 0.25) or (c_local < 800)
            
            # [HH] 高域エネルギーが高い or 重心が高い
            is_hh = (E_hh > 0.15) or (c_local > 4500)
            
            # [Snare] 中域があり、かつ重心がある程度高いがHHほどではない
            # スネアはHHと混ざることが多い
            is_snare = (E_snare > 0.15) and (c_local > 800) and (c_local < 5000)

            # 判定結果の登録 (同時発音OK)
            
            if is_kick:
                qt = round(float(t) / grid) * grid
                detected_events.append((qt, self._GM_KICK, vel))
            
            if is_snare:
                # Kickと同時に鳴る場合、重心が下がるのでSnare判定が漏れやすい。
                # Snare判定漏れを防ぐための補正: Kick判定済みでも中域が強ければSnareも足す
                qt = round(float(t) / grid) * grid
                detected_events.append((qt, self._GM_SNARE, vel))
                
            if is_hh:
                # Kick/Snareがあっても金物は鳴る
                qt = round(float(t) / grid) * grid
                # 音量は少し控えめに
                detected_events.append((qt, self._GM_HIHAT_CLOSED, int(vel * 0.9)))
                
            # もし何も判定されなかった場合 -> 安全策で重心で一つ選ぶ
            if not (is_kick or is_snare or is_hh):
                qt = round(float(t) / grid) * grid
                if c_local < 1000:
                    detected_events.append((qt, self._GM_KICK, vel))
                elif c_local < 4000:
                    detected_events.append((qt, self._GM_SNARE, vel))
                else:
                    detected_events.append((qt, self._GM_HIHAT_CLOSED, vel))

        # 5. 重複除去 (同じ楽器が同じタイミングに重なったら統合)
        # ベロシティは大きい方を採用
        unique_notes = {}
        for t, p, v in detected_events:
            key = (t, p)
            if key in unique_notes:
                unique_notes[key] = max(unique_notes[key], v)
            else:
                unique_notes[key] = v
        
        final_events = sorted([(t, p, v) for (t,p), v in unique_notes.items()], key=lambda x: x[0])

        # MIDI書き出し
        pm = pretty_midi.PrettyMIDI()
        drums = pretty_midi.Instrument(program=0, is_drum=True, name="Drums")
        
        note_duration = 0.05
        for t, pitch, vel in final_events:
            if t < 0: continue
            n = pretty_midi.Note(
                velocity=vel,
                pitch=int(pitch),
                start=t,
                end=t + note_duration,
            )
            drums.notes.append(n)

        pm.instruments.append(drums)
        pm.write(out_path)
        return out_path
    
    # Old classification method (unused but kept for reference if needed, 
    # though here we are fully replacing _transcribe_librosa)
    def _classify_event_legacy(
        self,
        S: np.ndarray,
        freqs: np.ndarray,
        onset_env: np.ndarray,
        frame: int,
    ) -> Tuple[int, int] | None:
        """1つのオンセットをドラム種別に分類して (GM pitch, velocity) を返す。"""
        if frame < 0 or frame >= S.shape[1]:
            return None

        left = max(0, frame - 2)
        right = min(S.shape[1], frame + 3)
        spec = S[:, left:right].mean(axis=1)
        total = float(spec.sum())
        if total <= 1e-8:
            return None

        low = float(spec[(freqs >= 20) & (freqs < 180)].sum())
        mid = float(spec[(freqs >= 180) & (freqs < 2500)].sum())
        high = float(spec[(freqs >= 2500)].sum())
        centroid = float((spec * freqs).sum() / total)

        low_r = low / total
        mid_r = mid / total
        high_r = high / total

        strength = float(onset_env[min(frame, len(onset_env) - 1)])
        velocity = int(np.clip(35 + strength * 25.0, 35, 120))

        # Cymbal / hi-hat
        if high_r > 0.55 or centroid > 4200:
            if high_r > 0.70 and centroid > 6000:
                return self._GM_CRASH, max(velocity, 88)
            if centroid > 5200:
                return self._GM_HIHAT_OPEN, max(velocity, 72)
            return self._GM_HIHAT_CLOSED, max(velocity, 65)

        # Kick
        if low_r > 0.45 and centroid < 1700:
            return self._GM_KICK, max(velocity, 80)

        # Tom/Snare 領域
        if mid_r > 0.45 and low_r > 0.20 and centroid < 2400:
            if centroid < 900:
                return self._GM_TOM_LOW, max(velocity, 78)
            if centroid < 1400:
                return self._GM_TOM_MID, max(velocity, 78)
            return self._GM_TOM_HIGH, max(velocity, 78)

        # 既定は snare
        return self._GM_SNARE, max(velocity, 75)

    def _dedupe_events(
        self,
        events: List[Tuple[float, int, int]],
        min_gap: float,
    ) -> List[Tuple[float, int, int]]:
        """同一ピッチで近接したイベントの重複を除去する。"""
        events = sorted(events, key=lambda x: (x[0], x[1]))
        out: List[Tuple[float, int, int]] = []
        last_by_pitch: Dict[int, float] = {}
        for t, p, v in events:
            last_t = last_by_pitch.get(p)
            if last_t is not None and abs(t - last_t) < min_gap:
                continue
            out.append((t, p, v))
            last_by_pitch[p] = t
        return out

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_backend(backend: str) -> str:
        if backend == "auto":
            return "omnizart" if importlib.util.find_spec("omnizart") else "librosa"
        return backend
