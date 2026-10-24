from io import BytesIO
import math
import wave

import numpy as np
import pytest

from app.core import NoteEvent, ScoreData, TempoEvent
from app.intonation import IntonationError, _estimate_f0, analyze_voice_recording


def make_wav(frequency=440.0, duration=3.0, sample_rate=22050):
    t = np.arange(int(duration * sample_rate), dtype=np.float32) / sample_rate
    samples = (0.45 * np.sin(2 * math.pi * frequency * t) * 32767).astype("<i2")
    buffer = BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(samples.tobytes())
    return buffer.getvalue()


def one_note_score(pitch=69):
    return ScoreData(
        "A4 pitch test",
        {"Soprano": "Soprano"},
        [NoteEvent("Soprano", "Soprano", pitch, 0.0, 4.0, measure=1)],
        [TempoEvent(0.0, 100.0)],
        4.0,
        "",
        ["Soprano"],
    )


def test_estimate_f0_recovers_a4():
    audio = make_wav()
    with wave.open(BytesIO(audio), "rb") as reader:
        frame = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2").astype(np.float32)
        sr = reader.getframerate()
    frequency, confidence = _estimate_f0(frame[int(0.5 * sr):int(0.56 * sr)], sr)
    assert frequency == pytest.approx(440.0, abs=2.0)
    assert confidence > 0.7


def test_recording_check_marks_correct_pitch_as_matched():
    report = analyze_voice_recording(make_wav(440.0), one_note_score(69), "Soprano", 100)
    assert report["summary"]["notes_checked"] == 1
    assert report["summary"]["matched"] == 1
    assert report["notes"][0]["status"] == "matched"
    assert abs(report["notes"][0]["cents"]) < 10


def test_recording_check_reports_sharp_pitch_in_cents():
    report = analyze_voice_recording(make_wav(466.1638), one_note_score(69), "Soprano", 100, tolerance_cents=20)
    assert report["summary"]["sharp"] == 1
    assert report["notes"][0]["cents"] > 80


def test_recording_check_rejects_missing_voice_and_bad_wav():
    with pytest.raises(IntonationError, match="nu există"):
        analyze_voice_recording(make_wav(), one_note_score(), "Alto")
    with pytest.raises(IntonationError, match="WAV"):
        analyze_voice_recording(b"not a wav", one_note_score(), "Soprano")
