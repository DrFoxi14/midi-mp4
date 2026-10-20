from io import BytesIO
from pathlib import Path
import wave

import mido
import pytest

from app.core import (
    NoteEvent, ScoreData, TempoEvent, _collapse_ties, canonical_voice,
    make_midi_bytes, parse_score, scaled_tempos, seconds_at_q,
    render_preview_wav, piano_backend_info,
)

def test_mscz_export_file_is_used_even_if_musescore_exits_nonzero(tmp_path, monkeypatch):
    import subprocess
    import app.core as core

    source = tmp_path / "score.mscz"
    source.write_bytes(b"dummy archive")
    work_dir = tmp_path / "work"
    monkeypatch.setattr(core, "find_musescore", lambda: "/fake/MuseScore4")
    
    def fake_run(args, **kwargs):
        output = Path(args[2])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("<score-partwise/>", encoding="utf-8")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="crash reporter warning")

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    result = core._prepare_musicxml(source, work_dir)
    assert result.is_file()
    assert result.stat().st_size > 0


def tiny_score():
    voices = ["Soprano", "Alto", "Tenor", "Bass"]
    notes = [
        NoteEvent("Soprano", "Soprano", 72, 0.0, 4.0),
        NoteEvent("Alto", "Alto", 67, 0.0, 4.0),
        NoteEvent("Tenor", "Tenor", 60, 0.0, 4.0),
        NoteEvent("Bass", "Bass", 48, 0.0, 4.0),
    ]
    return ScoreData(
        "Test SATB",
        {voice: voice for voice in voices},
        notes,
        [TempoEvent(0, 60), TempoEvent(4, 90)],
        8.0,
        "",
    )


def test_reference_tempo_preserves_tempo_ratios():
    result = scaled_tempos([TempoEvent(0, 60), TempoEvent(8, 90)], 80)
    assert [round(t.bpm, 4) for t in result] == [80, 120]


def test_seconds_at_q_handles_tempo_changes():
    tempos = [TempoEvent(0, 60), TempoEvent(4, 90)]
    assert seconds_at_q(4, tempos) == pytest.approx(4.0)
    assert seconds_at_q(8, tempos) == pytest.approx(4.0 + 4 * 60 / 90)


def test_invalid_reference_tempo_is_rejected():
    with pytest.raises(ValueError):
        scaled_tempos([TempoEvent(0, 100)], 250)


def test_satb_midi_contains_separate_tracks_and_scaled_tempo_events():
    data = make_midi_bytes(tiny_score(), reference_bpm=80, voice="all")
    midi = mido.MidiFile(file=BytesIO(data))
    names = [m.name for track in midi.tracks for m in track if m.type == "track_name"]
    assert names == ["Tempo map", "Soprano", "Alto", "Tenor", "Bass"]
    tempo_values = [
        round(mido.tempo2bpm(msg.tempo))
        for msg in midi.tracks[0]
        if msg.type == "set_tempo"
    ]
    assert tempo_values == [80, 120]
    for track in midi.tracks[1:]:
        assert any(m.type == "note_on" and m.velocity > 0 for m in track)


def test_single_voice_midi_isolated_to_one_named_track():
    data = make_midi_bytes(tiny_score(), reference_bpm=100, voice="Alto")
    midi = mido.MidiFile(file=BytesIO(data))
    names = [m.name for track in midi.tracks for m in track if m.type == "track_name"]
    assert names == ["Tempo map", "Alto"]
    assert sum(1 for track in midi.tracks for m in track if m.type == "note_on" and m.velocity) == 1


def test_tied_notes_are_merged_in_preview_and_midi_pipeline():
    events = [
        NoteEvent("Soprano", "Soprano", 72, 0, 1, tie="start"),
        NoteEvent("Soprano", "Soprano", 72, 1, 2, tie="stop"),
    ]
    merged = _collapse_ties(events)
    assert len(merged) == 1
    assert merged[0].duration_q == 3


@pytest.mark.parametrize(("text", "expected"), [
    ("Soprano", "Soprano"), ("Alto 1", "Alto"), ("Tenor", "Tenor"),
    ("Bass section", "Bass"),
])
def test_satb_name_mapping(text, expected):
    assert canonical_voice(text) == expected


def test_parses_small_musicxml_satb_fixture(tmp_path: Path):
    fixture = Path(__file__).parent / "fixtures" / "satb-tiny.musicxml"
    parsed = parse_score(fixture, tmp_path / "work")
    assert parsed.title == "Mini SATB"
    assert set(parsed.part_names) == {"Soprano", "Alto", "Tenor", "Bass"}
    assert parsed.total_quarters >= 4
    assert len([n for n in parsed.notes if not n.is_rest]) == 4
    assert parsed.tempos[0].bpm == pytest.approx(60)

def test_parsed_score_retains_original_satb_voice_order(tmp_path: Path):
    fixture = Path(__file__).parent / "fixtures" / "satb-tiny.musicxml"
    parsed = parse_score(fixture, tmp_path / "voice-order")
    assert parsed.voice_order == ["Soprano", "Alto", "Tenor", "Bass"]
    assert parsed.public_dict()["voice_order"] == parsed.voice_order


def test_negative_offset_discards_notes_fully_before_zero_without_clicks(monkeypatch):
    monkeypatch.delenv("MIDI_MP4_SOUNDFONT", raising=False)
    score = ScoreData(
        "Clipped note",
        {"Soprano": "Soprano"},
        [NoteEvent("Soprano", "Soprano", 60, 0.0, 1.0)],
        [TempoEvent(0.0, 60.0)],
        1.0,
        "",
        ["Soprano"],
    )
    rendered = render_preview_wav(score, 60, ["Soprano"], offset_ms=-2000)
    with wave.open(BytesIO(rendered), "rb") as wav_file:
        pcm = wav_file.readframes(wav_file.getnframes())
    assert pcm
    assert not any(pcm)


def test_audio_backend_defaults_to_offline_synth_without_soundfont(monkeypatch):
    monkeypatch.delenv("MIDI_MP4_SOUNDFONT", raising=False)
    result = piano_backend_info()
    assert result["backend"] == "synthesized"
    assert result["sample_piano_available"] is False


def test_invalid_soundfont_path_is_reported(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MIDI_MP4_SOUNDFONT", str(tmp_path / "missing.sf2"))
    result = piano_backend_info()
    assert result["backend"] == "configuration-error"
    assert result["sample_piano_available"] is False


def test_uploaded_runtime_soundfont_is_used_by_preview(monkeypatch, tmp_path: Path):
    import app.core as core

    monkeypatch.delenv("MIDI_MP4_SOUNDFONT", raising=False)
    soundfont = tmp_path / "uploaded.sf2"
    soundfont.write_bytes(b"test-soundfont")
    observed = {}
    core.set_runtime_soundfont(str(soundfont))

    def fake_render(score, reference_bpm, voices, offset_ms, focus_voice):
        observed["voices"] = voices
        observed["reference_bpm"] = reference_bpm
        return b"runtime-soundfont-preview"

    monkeypatch.setattr(core, "_render_soundfont_wav", fake_render)
    try:
        result = core.render_preview_wav(tiny_score(), reference_bpm=60, voices=["Soprano"])
    finally:
        core.set_runtime_soundfont(None)

    assert result == b"runtime-soundfont-preview"
    assert observed == {"voices": {"Soprano"}, "reference_bpm": 60}


def test_soundfont_positive_offset_prepends_exact_silence(monkeypatch, tmp_path: Path):
    import subprocess
    import app.core as core

    soundfont = tmp_path / "test.sf2"
    soundfont.write_bytes(b"test-soundfont")
    monkeypatch.delenv("MIDI_MP4_SOUNDFONT", raising=False)
    monkeypatch.setattr(core.shutil, "which", lambda command: "/usr/bin/fluidsynth")
    core.set_runtime_soundfont(str(soundfont))

    def fake_run(args, **kwargs):
        wav_path = Path(args[4])
        with wave.open(str(wav_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(22050)
            output.writeframes(b"\x01\x00" * 22050)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    try:
        rendered = core._render_soundfont_wav(
            tiny_score(), reference_bpm=60, voices={"Soprano"},
            offset_ms=1000, focus_voice=None,
        )
    finally:
        core.set_runtime_soundfont(None)

    with wave.open(BytesIO(rendered), "rb") as output:
        assert output.getframerate() == 22050
        assert output.getnframes() == 44100
        leading_silence = output.readframes(22050)
        remaining_audio = output.readframes(22050)
    assert leading_silence == b"\x00" * (22050 * 2)
    assert remaining_audio == b"\x01\x00" * 22050
