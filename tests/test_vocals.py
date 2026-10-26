from io import BytesIO
from pathlib import Path
import json
import math
import struct
import wave

import pytest

from app.core import NoteEvent, ScoreData, TempoEvent
import app.vocals as vocals


def _score():
    voices = ["Soprano", "Alto", "Tenor", "Bass"]
    notes = [
        NoteEvent("Soprano", "Soprano", 72, 0.0, 4.0),
        NoteEvent("Alto", "Alto", 67, 0.0, 4.0),
        NoteEvent("Tenor", "Tenor", 60, 0.0, 4.0),
        NoteEvent("Bass", "Bass", 48, 0.0, 4.0),
    ]
    return ScoreData(
        "Focused mix test",
        {voice: voice for voice in voices},
        notes,
        [TempoEvent(0, 100)],
        4.0,
        "",
        voices,
    )


def _bass_wav() -> bytes:
    output = BytesIO()
    rate = 22050
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        samples = b"".join(
            struct.pack("<h", int(10000 * math.sin(2 * math.pi * 110 * i / rate)))
            for i in range(int(0.4 * rate))
        )
        wav_file.writeframes(samples)
    return output.getvalue()


def test_focused_mix_keeps_50_percent_accompaniment_after_imported_lead(tmp_path, monkeypatch):
    stems = tmp_path / "vocal-stems"
    stems.mkdir()
    vocals.vocal_stem_path(stems, "Bass").write_bytes(_bass_wav())
    vocals.vocal_stem_metadata_path(stems, "Bass").write_text(
        json.dumps({"voice": "Bass", "reference_bpm": 100, "duration_seconds": 0.4}),
        encoding="utf-8",
    )

    monkeypatch.setattr(vocals.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(vocals, "render_preview_wav", lambda *args, **kwargs: b"fake synth WAV")
    captured = {}

    def fake_ffmpeg(args, **kwargs):
        filter_text = args[args.index("-filter_complex") + 1]
        captured["filter"] = filter_text
        Path(args[-1]).write_bytes(b"RIFF" + b"\x00" * 96)
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(vocals.subprocess, "run", fake_ffmpeg)
    levels = {"Soprano": 50, "Alto": 50, "Tenor": 50, "Bass": 100}
    result = vocals.mix_vocal_audio(
        _score(), stems, ["Soprano", "Alto", "Tenor", "Bass"], 100,
        focus_voice="Bass",
        volume_by_voice=levels,
        normalize_duration=True,
    )

    assert result and len(result) > 44
    assert "loudnorm=I=-20:TP=-2:LRA=11" in captured["filter"]
    assert captured["filter"].count("volume=0.2750") == 3
    assert "volume=1.0000" in captured["filter"]
    assert "amix=inputs=4" in captured["filter"]
