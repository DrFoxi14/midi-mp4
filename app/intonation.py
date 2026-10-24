"""Offline, monophonic voice-practice intonation check against a score part.

This intentionally analyses one isolated singer/part at a time. It is not a
polyphonic choir source-separation or concert-recording evaluator.
"""
from __future__ import annotations

from collections import Counter
from io import BytesIO
import math
import wave
from typing import Any

import numpy as np

from app.core import ScoreData, NoteEvent, seconds_at_q, scaled_tempos


MAX_RECORDING_BYTES = 32 * 1024 * 1024
MAX_RECORDING_SECONDS = 15 * 60
MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 192000


class IntonationError(ValueError):
    pass


def _decode_pcm_wav(raw: bytes) -> tuple[np.ndarray, int]:
    if not raw or len(raw) > MAX_RECORDING_BYTES:
        raise IntonationError("Înregistrarea trebuie să fie un WAV valid de maximum 32 MB.")
    try:
        with wave.open(BytesIO(raw), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frame_count = source.getnframes()
            compression = source.getcomptype()
            if compression != "NONE":
                raise IntonationError("Folosește un WAV PCM necomprimat.")
            if channels not in (1, 2):
                raise IntonationError("WAV-ul trebuie să aibă una sau două canale.")
            if sample_width not in (1, 2, 3, 4):
                raise IntonationError("Adâncime PCM nesuportată; folosește WAV PCM pe 8/16/24/32 biți.")
            if not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE:
                raise IntonationError("Frecvența WAV trebuie să fie între 8 kHz și 192 kHz.")
            duration = frame_count / max(1, sample_rate)
            if duration <= 0 or duration > MAX_RECORDING_SECONDS:
                raise IntonationError("Înregistrarea trebuie să aibă între 0 și 15 minute.")
            data = source.readframes(frame_count)
    except (wave.Error, EOFError) as exc:
        raise IntonationError("Fișierul nu poate fi citit ca WAV PCM valid.") from exc

    if sample_width == 1:
        samples = np.frombuffer(data, dtype=np.uint8).astype(np.float32)
        samples = (samples - 128.0) / 128.0
    elif sample_width == 2:
        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 3:
        octets = np.frombuffer(data, dtype=np.uint8)
        if len(octets) % 3:
            raise IntonationError("Datele PCM pe 24 de biți sunt incomplete.")
        triplets = octets.reshape(-1, 3).astype(np.int32)
        integers = triplets[:, 0] | (triplets[:, 1] << 8) | (triplets[:, 2] << 16)
        integers = (integers ^ 0x800000) - 0x800000
        samples = integers.astype(np.float32) / 8388608.0
    else:
        samples = np.frombuffer(data, dtype="<i4").astype(np.float32) / 2147483648.0

    if samples.size % channels:
        raise IntonationError("Datele audio nu se aliniază cu numărul de canale.")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak < 1e-5:
        raise IntonationError("Înregistrarea este tăcută sau are un nivel prea mic.")
    return np.ascontiguousarray(samples, dtype=np.float32), sample_rate


def _estimate_f0(frame: np.ndarray, sample_rate: int) -> tuple[float | None, float]:
    """Estimate a voiced fundamental with FFT autocorrelation; no cloud or model required."""
    if frame.size < 256:
        return None, 0.0
    centered = frame.astype(np.float32, copy=False)
    centered = centered - float(np.mean(centered))
    rms = float(np.sqrt(np.mean(centered * centered)))
    if rms < 1e-4:
        return None, 0.0
    centered = centered * np.hanning(len(centered)).astype(np.float32)
    fft_size = 1 << (2 * len(centered) - 1).bit_length()
    spectrum = np.fft.rfft(centered, n=fft_size)
    autocorr = np.fft.irfft(spectrum * np.conjugate(spectrum), n=fft_size)[:len(centered)]
    if not autocorr.size or autocorr[0] <= 1e-10:
        return None, 0.0
    min_lag = max(2, int(sample_rate / 1000.0))
    max_lag = min(len(centered) - 2, int(sample_rate / 55.0))
    if max_lag <= min_lag:
        return None, 0.0
    normalized = autocorr[min_lag:max_lag + 1] / autocorr[0]
    index = int(np.argmax(normalized))
    lag = min_lag + index
    confidence = float(normalized[index])
    if confidence < 0.22:
        return None, confidence
    # Small parabolic correction improves cents accuracy between integer lags.
    if 0 < index < len(normalized) - 1:
        left, middle, right = map(float, normalized[index - 1:index + 2])
        denom = left - 2.0 * middle + right
        if abs(denom) > 1e-10:
            lag += 0.5 * (left - right) / denom
    if lag <= 0:
        return None, confidence
    frequency = sample_rate / lag
    if not 45.0 <= frequency <= 1100.0:
        return None, confidence
    return frequency, max(0.0, min(1.0, confidence))


def midi_note_name(midi_pitch: int) -> str:
    names = ("C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B")
    return f"{names[int(midi_pitch) % 12]}{int(midi_pitch) // 12 - 1}"


def analyze_voice_recording(
    raw_wav: bytes,
    score: ScoreData,
    voice: str,
    reference_bpm: float = 100.0,
    offset_ms: int = 0,
    tolerance_cents: float = 40.0,
) -> dict[str, Any]:
    """Compare one recorded singer's pitch to score note events at their expected times."""
    if voice not in score.part_names and voice not in {event.voice for event in score.notes}:
        raise IntonationError("Vocea selectată nu există în partitură.")
    if not 30 <= float(reference_bpm) <= 240:
        raise IntonationError("Tempo-ul de referință trebuie să fie între 30 și 240 BPM.")
    if not -10000 <= int(offset_ms) <= 10000:
        raise IntonationError("Corecția de sincronizare trebuie să fie între -10 000 și +10 000 ms.")
    if not 10.0 <= float(tolerance_cents) <= 100.0:
        raise IntonationError("Toleranța de intonație trebuie să fie între 10 și 100 de cenți.")

    audio, sample_rate = _decode_pcm_wav(raw_wav)
    audio_duration = len(audio) / sample_rate
    tempos = scaled_tempos(score.tempos, reference_bpm)
    events = sorted(
        (event for event in score.notes
         if event.voice == voice and not event.is_rest and event.pitch is not None
         and float(event.duration_q) > 0),
        key=lambda event: (event.start_q, event.pitch or 0),
    )
    if not events:
        raise IntonationError("Nu am găsit note de verificat pentru vocea selectată.")

    # A polyphonic chord on one voice cannot be compared to one monophonic singer
    # with this method. Exclude overlapping distinct pitches instead of guessing.
    event_windows = []
    for event in events:
        start = seconds_at_q(event.start_q, tempos) + offset_ms / 1000.0
        end = seconds_at_q(event.start_q + event.duration_q, tempos) + offset_ms / 1000.0
        if end <= 0 or start >= audio_duration:
            continue
        event_windows.append((event, max(0.0, start), min(audio_duration, end)))
    if not event_windows:
        raise IntonationError("Înregistrarea nu se suprapune cu notele din partitură. Verifică tempo-ul și offset-ul.")

    # 60 ms allows several periods in lower vocal notes. 30 ms hop balances precision
    # with practical CPU cost; pitch estimates are cached across event sampling points.
    frame_size = max(480, int(round(sample_rate * 0.060)))
    half = frame_size // 2
    pitch_cache: dict[int, tuple[float | None, float]] = {}

    def pitch_at(time_seconds: float) -> tuple[float | None, float]:
        center = int(round(time_seconds * sample_rate))
        if center in pitch_cache:
            return pitch_cache[center]
        begin = center - half
        end = begin + frame_size
        if begin < 0 or end > len(audio):
            pitch_cache[center] = (None, 0.0)
            return pitch_cache[center]
        result = _estimate_f0(audio[begin:end], sample_rate)
        pitch_cache[center] = result
        return result

    results: list[dict[str, Any]] = []
    overlapping_ambiguous = 0
    for event, start, end in event_windows:
        midi_pitch = int(event.pitch)
        start_q = float(event.start_q)
        end_q = start_q + float(event.duration_q)
        overlaps = [
            other for other in events
            if other is not event and other.pitch is not None and int(other.pitch) != midi_pitch
            and float(other.start_q) < end_q - 1e-6
            and start_q < float(other.start_q) + float(other.duration_q) - 1e-6
        ]
        if overlaps:
            overlapping_ambiguous += 1
            continue

        duration = end - start
        fractions = (0.25, 0.50, 0.75) if duration >= 0.36 else (0.50,)
        estimates: list[tuple[float, float]] = []
        for fraction in fractions:
            time = start + duration * fraction
            frequency, confidence = pitch_at(time)
            if frequency is not None:
                estimates.append((69.0 + 12.0 * math.log2(frequency / 440.0), confidence))
        timestamp = round(max(0.0, start), 3)
        common = {
            "voice": voice,
            "measure": str(event.measure) if event.measure is not None else None,
            "time_seconds": timestamp,
            "expected_midi": midi_pitch,
            "expected_note": midi_note_name(midi_pitch),
            "duration_seconds": round(max(0.0, end - start), 3),
        }
        if not estimates:
            results.append({
                **common, "status": "unclear", "detected_midi": None,
                "detected_note": None, "cents": None, "confidence": 0.0,
                "message": "Nu am putut estima o înălțime stabilă; verifică respirația, zgomotul și sincronizarea.",
            })
            continue
        detected = float(np.median([item[0] for item in estimates]))
        confidence = float(np.median([item[1] for item in estimates]))
        cents = (detected - midi_pitch) * 100.0
        rounded_midi = int(round(detected))
        if abs(cents) <= float(tolerance_cents):
            status, message = "matched", "Înălțime apropiată de nota din partitură."
        else:
            status = "sharp" if cents > 0 else "flat"
            message = "Mai sus decât ținta." if cents > 0 else "Mai jos decât ținta."
        results.append({
            **common,
            "status": status,
            "detected_midi": round(detected, 2),
            "detected_note": midi_note_name(rounded_midi),
            "cents": round(cents, 1),
            "confidence": round(confidence, 3),
            "message": message,
        })

    counts = Counter(item["status"] for item in results)
    tested = len(results)
    detected_count = sum(counts[key] for key in ("matched", "sharp", "flat"))
    accurate_count = counts["matched"]
    average_abs_cents = (
        round(float(np.mean([abs(item["cents"]) for item in results if item["cents"] is not None])), 1)
        if detected_count else None
    )
    return {
        "title": score.title,
        "voice": voice,
        "reference_bpm": float(reference_bpm),
        "recording_duration_seconds": round(audio_duration, 2),
        "tolerance_cents": float(tolerance_cents),
        "summary": {
            "notes_checked": tested,
            "pitch_detected": detected_count,
            "matched": accurate_count,
            "sharp": counts["sharp"],
            "flat": counts["flat"],
            "unclear": counts["unclear"],
            "ambiguous_overlaps_skipped": overlapping_ambiguous,
            "match_percent": round(100.0 * accurate_count / detected_count, 1) if detected_count else None,
            "average_absolute_cents": average_abs_cents,
        },
        "notes": results[:3000],
        "truncated": len(results) > 3000,
        "disclaimer": (
            "Verificare orientativă pentru o singură voce înregistrată separat. "
            "Zgomotul, vibrato-ul, consoanele, reverberația, armonicele și diferențele de sincronizare pot afecta estimarea. "
            "Rezultatul nu evaluează timbrul sau calitatea artistică și nu poate separa singur toate vocile dintr-o înregistrare de cor."
        ),
    }
