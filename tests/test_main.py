from fastapi.testclient import TestClient
import json

import app.main as main_module
from app.core import NoteEvent, ScoreData, TempoEvent
from app.main import ScoreRecord


def _score(tmp_path):
    voices = ["Soprano", "Alto", "Tenor", "Bass"]
    notes = [
        NoteEvent("Soprano", "Soprano", 72, 0.0, 4.0),
        NoteEvent("Alto", "Alto", 67, 0.0, 4.0),
        NoteEvent("Tenor", "Tenor", 60, 0.0, 4.0),
        NoteEvent("Bass", "Bass", 48, 0.0, 4.0),
    ]
    return ScoreData("Consent test", {v: v for v in voices}, notes,
                     [TempoEvent(0, 60)], 4.0,
                     str(tmp_path / "unused.musicxml"), voices)


def test_video_export_requires_explicit_score_relayout_consent(tmp_path, monkeypatch):
    key = "consent-test"
    monkeypatch.setitem(main_module.scores, key,
                        ScoreRecord(_score(tmp_path), tmp_path, "test.musicxml"))
    client = TestClient(main_module.app)
    response = client.post(f"/api/scores/{key}/video-jobs", json={
        "reference_bpm": 100,
        "resolution": "1080p",
        "voice": "all",
        "audio_mode": "accompaniment",
        "offset_ms": 0,
        "format": "mp4",
        "accept_reflowed_engraving": False,
    })
    assert response.status_code == 422
    assert "Confirmă explicit" in response.json()["detail"]
    assert not main_module.video_jobs
    monkeypatch.delitem(main_module.scores, key, raising=False)


def test_video_export_rejects_unknown_voice_after_consent(tmp_path, monkeypatch):
    key = "voice-test"
    monkeypatch.setitem(main_module.scores, key,
                        ScoreRecord(_score(tmp_path), tmp_path, "test.musicxml"))
    client = TestClient(main_module.app)
    response = client.post(f"/api/scores/{key}/video-jobs", json={
        "reference_bpm": 100,
        "resolution": "1080p",
        "voice": "Soprano 2",
        "audio_mode": "isolated",
        "offset_ms": 0,
        "format": "mp4",
        "accept_reflowed_engraving": True,
    })
    assert response.status_code == 422
    assert "Voce necunoscută" in response.json()["detail"]
    assert not main_module.video_jobs
    monkeypatch.delitem(main_module.scores, key, raising=False)



def test_preview_offset_does_not_shift_audio_timeline(tmp_path, monkeypatch):
    key = "preview-offset-test"
    monkeypatch.setitem(main_module.scores, key,
                        ScoreRecord(_score(tmp_path), tmp_path, "test.musicxml"))
    observed = {}

    def fake_render(score, reference_bpm, voices, offset_ms=0, focus_voice=None):
        observed["reference_bpm"] = reference_bpm
        observed["voices"] = voices
        observed["offset_ms"] = offset_ms
        return b"fake-wav-data"

    monkeypatch.setattr(main_module, "render_preview_wav", fake_render)
    client = TestClient(main_module.app)
    response = client.get(
        f"/api/scores/{key}/preview.wav?reference_bpm=110&voices=Soprano&offset_ms=250"
    )
    assert response.status_code == 200
    assert response.content == b"fake-wav-data"
    assert observed == {
        "reference_bpm": 110,
        "voices": ["Soprano"],
        "offset_ms": 0,
    }
    monkeypatch.delitem(main_module.scores, key, raising=False)


def test_soundfont_upload_accepts_only_sf2_or_sf3_and_sets_runtime_path(tmp_path, monkeypatch):
    import app.main as main_module
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main_module, "DATA_ROOT", tmp_path)
    client = TestClient(main_module.app)
    response = client.post("/api/settings/soundfont", files={"file": ("local.sf2", b"small-sf-data", "application/octet-stream")})
    assert response.status_code == 200
    assert response.json()["saved"] is True
    assert response.json()["filename"] == "local.sf2"
    path = tmp_path / "settings" / "active-piano.sf2"
    assert path.exists()
    assert path.read_bytes() == b"small-sf-data"
    # Restore module-level process setting so tests remain isolated.
    from app.core import set_runtime_soundfont
    set_runtime_soundfont(None)
    response = client.post("/api/settings/soundfont", files={"file": ("unsupported.txt", b"bad", "text/plain")})
    assert response.status_code == 415
    path.unlink(missing_ok=True)


def test_soundfont_upload_rejects_empty_payload(tmp_path, monkeypatch):
    import app.main as main_module
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main_module, "DATA_ROOT", tmp_path)
    client = TestClient(main_module.app)
    response = client.post("/api/settings/soundfont", files={"file": ("empty.sf2", b"", "application/octet-stream")})
    assert response.status_code == 400



def test_audio_download_wav_exports_selected_voice_and_sets_attachment_headers(tmp_path, monkeypatch):
    key = "audio-wav-test"
    score = _score(tmp_path)
    monkeypatch.setitem(main_module.scores, key, ScoreRecord(score, tmp_path, "test.musicxml"))
    observed = {}

    def fake_render(render_score, reference_bpm, voices, offset_ms=0, focus_voice=None):
        observed.update(reference_bpm=reference_bpm, voices=voices,
                        offset_ms=offset_ms, focus_voice=focus_voice)
        return b"RIFF-test-wav"

    monkeypatch.setattr(main_module, "render_preview_wav", fake_render)
    client = TestClient(main_module.app)
    response = client.get(
        f"/api/scores/{key}/audio",
        params={"format": "wav", "reference_bpm": 110, "voice": "Soprano", "mode": "isolated"},
    )

    assert response.status_code == 200
    assert response.content == b"RIFF-test-wav"
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.headers["content-disposition"] == 'attachment; filename="Consent_test-soprano.wav"'
    assert observed == {
        "reference_bpm": 110,
        "voices": ["Soprano"],
        "offset_ms": 0,
        "focus_voice": None,
    }
    monkeypatch.delitem(main_module.scores, key, raising=False)


def test_audio_download_can_emphasize_a_voice_over_accompaniment(tmp_path, monkeypatch):
    key = "audio-accompaniment-test"
    score = _score(tmp_path)
    monkeypatch.setitem(main_module.scores, key, ScoreRecord(score, tmp_path, "test.musicxml"))
    observed = {}

    def fake_render(render_score, reference_bpm, voices, offset_ms=0, focus_voice=None):
        observed.update(voices=voices, focus_voice=focus_voice)
        return b"RIFF-test-wav"

    monkeypatch.setattr(main_module, "render_preview_wav", fake_render)
    client = TestClient(main_module.app)
    response = client.get(
        f"/api/scores/{key}/audio",
        params={"format": "wav", "voice": "Alto", "mode": "accompaniment"},
    )

    assert response.status_code == 200
    assert observed == {
        "voices": ["Soprano", "Alto", "Tenor", "Bass"],
        "focus_voice": "Alto",
    }
    assert response.headers["content-disposition"] == 'attachment; filename="Consent_test-alto-accompaniment.wav"'
    monkeypatch.delitem(main_module.scores, key, raising=False)


def test_audio_download_mp3_uses_local_encoder(tmp_path, monkeypatch):
    key = "audio-mp3-test"
    score = _score(tmp_path)
    monkeypatch.setitem(main_module.scores, key, ScoreRecord(score, tmp_path, "test.musicxml"))
    monkeypatch.setattr(main_module, "render_preview_wav", lambda *args, **kwargs: b"RIFF-test-wav")
    monkeypatch.setattr(main_module, "_encode_mp3", lambda wav_data: b"ID3-test-mp3")
    client = TestClient(main_module.app)

    response = client.get(f"/api/scores/{key}/audio", params={"format": "mp3", "voice": "all"})

    assert response.status_code == 200
    assert response.content == b"ID3-test-mp3"
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.headers["content-disposition"] == 'attachment; filename="Consent_test-SATB.mp3"'
    monkeypatch.delitem(main_module.scores, key, raising=False)


def test_audio_download_rejects_unknown_voice(tmp_path, monkeypatch):
    key = "audio-invalid-voice-test"
    monkeypatch.setitem(main_module.scores, key, ScoreRecord(_score(tmp_path), tmp_path, "test.musicxml"))
    client = TestClient(main_module.app)

    response = client.get(f"/api/scores/{key}/audio", params={"voice": "Voice 99"})

    assert response.status_code == 422
    assert "Voce necunoscută" in response.json()["detail"]
    monkeypatch.delitem(main_module.scores, key, raising=False)



def test_mp3_encoder_produces_audio_with_local_ffmpeg():
    import io
    import wave
    import pytest

    if not main_module.shutil.which("ffmpeg"):
        pytest.skip("Local FFmpeg is not installed.")
    pcm = io.BytesIO()
    with wave.open(pcm, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(22050)
        target.writeframes(bytes(5512 * 2))

    encoded = main_module._encode_mp3(pcm.getvalue())
    assert len(encoded) > 128



def test_audio_pack_zip_contains_full_mix_and_each_isolated_voice(tmp_path, monkeypatch):
    import io
    import zipfile

    key = "audio-pack-test"
    score = _score(tmp_path)
    monkeypatch.setitem(main_module.scores, key, ScoreRecord(score, tmp_path, "test.musicxml"))
    rendered = []

    def fake_render(render_score, reference_bpm, voices, offset_ms=0, focus_voice=None):
        rendered.append((tuple(voices), reference_bpm, offset_ms, focus_voice))
        return ("WAV:" + ",".join(voices)).encode("utf-8")

    monkeypatch.setattr(main_module, "render_preview_wav", fake_render)
    client = TestClient(main_module.app)
    response = client.get(
        f"/api/scores/{key}/audio-pack.zip",
        params={"format": "wav", "reference_bpm": 120},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert response.headers["content-disposition"] == 'attachment; filename="Consent_test-rehearsal-pack.zip"'
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert names == {
            "Consent_test-SATB-fullmix.wav",
            "Consent_test-soprano.wav",
            "Consent_test-alto.wav",
            "Consent_test-tenor.wav",
            "Consent_test-bass.wav",
            "README.txt",
        }
        assert archive.read("Consent_test-SATB-fullmix.wav") == b"WAV:Soprano,Alto,Tenor,Bass"
        assert archive.read("Consent_test-soprano.wav") == b"WAV:Soprano"
        assert "Tempo de referință: 120 BPM" in archive.read("README.txt").decode("utf-8")
    assert rendered[0] == (("Soprano", "Alto", "Tenor", "Bass"), 120, 0, None)
    assert len(rendered) == 5
    monkeypatch.delitem(main_module.scores, key, raising=False)


def test_audio_pack_rejects_invalid_audio_format(tmp_path, monkeypatch):
    key = "audio-pack-invalid-format"
    monkeypatch.setitem(main_module.scores, key, ScoreRecord(_score(tmp_path), tmp_path, "test.musicxml"))
    client = TestClient(main_module.app)

    response = client.get(f"/api/scores/{key}/audio-pack.zip", params={"format": "flac"})

    assert response.status_code == 422
    monkeypatch.delitem(main_module.scores, key, raising=False)



def test_audio_pack_mp3_encodes_mix_and_each_voice(tmp_path, monkeypatch):
    import io
    import zipfile

    key = "audio-pack-mp3-test"
    monkeypatch.setitem(main_module.scores, key, ScoreRecord(_score(tmp_path), tmp_path, "test.musicxml"))
    encodes = []

    def fake_render(render_score, reference_bpm, voices, offset_ms=0, focus_voice=None):
        return ("WAV:" + ",".join(voices)).encode("utf-8")

    def fake_encode(wav_data):
        encodes.append(wav_data)
        return b"ID3:" + wav_data

    monkeypatch.setattr(main_module, "render_preview_wav", fake_render)
    monkeypatch.setattr(main_module, "_encode_mp3", fake_encode)
    client = TestClient(main_module.app)

    response = client.get(f"/api/scores/{key}/audio-pack.zip", params={"format": "mp3"})

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert names == {
            "Consent_test-SATB-fullmix.mp3",
            "Consent_test-soprano.mp3",
            "Consent_test-alto.mp3",
            "Consent_test-tenor.mp3",
            "Consent_test-bass.mp3",
            "README.txt",
        }
        assert archive.read("Consent_test-bass.mp3") == b"ID3:WAV:Bass"
    assert len(encodes) == 5
    monkeypatch.delitem(main_module.scores, key, raising=False)



def _pcm_wav_bytes(seconds=1.0, rate=22050, frequency=440.0):
    import io
    import math
    import struct
    import wave

    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        samples = bytearray()
        for i in range(int(seconds * rate)):
            value = int(9000 * math.sin(2 * math.pi * frequency * i / rate))
            samples.extend(struct.pack("<h", value))
        target.writeframes(bytes(samples))
    return output.getvalue()


def test_openutau_vocal_project_zip_contains_musicxml_midi_and_instructions(tmp_path, monkeypatch):
    import io
    import zipfile

    key = "openutau-project-test"
    score = _score(tmp_path)
    xml_path = tmp_path / "Mini SATB.musicxml"
    xml_path.write_text(
        '<?xml version="1.0"?><score-partwise><work><work-title>Mini SATB</work-title></work></score-partwise>',
        encoding="utf-8",
    )
    score.musicxml_path = str(xml_path)
    monkeypatch.setitem(main_module.scores, key, ScoreRecord(score, tmp_path, "mini.musicxml"))
    client = TestClient(main_module.app)

    response = client.get(f"/api/scores/{key}/vocal-project.zip", params={"reference_bpm": 100})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert "Consent_test.musicxml" in names
        assert "MIDI/SATB.mid" in names
        assert "MIDI/soprano.mid" in names
        assert "MIDI/alto.mid" in names
        assert "MIDI/tenor.mid" in names
        assert "MIDI/bass.mid" in names
        guide = archive.read("README-OpenUtau.md").decode("utf-8")
        assert "OpenUtau" in guide
        assert "voicebank" in guide
        assert "100 BPM" in guide
    monkeypatch.delitem(main_module.scores, key, raising=False)


def test_upload_vocal_stem_and_download_selected_voice_uses_uploaded_wav(tmp_path, monkeypatch):
    key = "vocal-stem-upload-test"
    score = _score(tmp_path)
    monkeypatch.setitem(main_module.scores, key, ScoreRecord(score, tmp_path, "test.musicxml"))
    wav_data = _pcm_wav_bytes(seconds=0.5)
    monkeypatch.setattr(
        main_module, "render_preview_wav",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("synthetic audio should not run")),
    )
    client = TestClient(main_module.app)

    response = client.post(
        f"/api/scores/{key}/vocal-stems/Soprano?reference_bpm=100",
        files={"file": ("soprano.wav", wav_data, "audio/wav")},
    )
    assert response.status_code == 200
    assert response.json()["uploaded"] is True
    assert response.json()["stems"]["uploaded_count"] == 1

    download = client.get(
        f"/api/scores/{key}/audio",
        params={"format": "wav", "reference_bpm": 100, "voice": "Soprano", "mode": "isolated"},
    )
    assert download.status_code == 200
    assert download.content == wav_data
    assert download.headers["content-type"].startswith("audio/wav")
    monkeypatch.delitem(main_module.scores, key, raising=False)


def test_uploaded_vocal_stem_rejects_mismatched_tempo(tmp_path, monkeypatch):
    key = "vocal-stem-tempo-test"
    monkeypatch.setitem(main_module.scores, key, ScoreRecord(_score(tmp_path), tmp_path, "test.musicxml"))
    client = TestClient(main_module.app)
    uploaded = client.post(
        f"/api/scores/{key}/vocal-stems/Alto?reference_bpm=100",
        files={"file": ("alto.wav", _pcm_wav_bytes(), "audio/wav")},
    )
    assert uploaded.status_code == 200
    response = client.get(
        f"/api/scores/{key}/audio",
        params={"format": "wav", "reference_bpm": 110, "voice": "Alto", "mode": "isolated"},
    )
    assert response.status_code == 422
    assert "110 BPM" in response.json()["detail"]
    monkeypatch.delitem(main_module.scores, key, raising=False)


def test_vocal_stem_upload_rejects_non_wav_and_invalid_wav(tmp_path, monkeypatch):
    key = "vocal-stem-invalid-test"
    monkeypatch.setitem(main_module.scores, key, ScoreRecord(_score(tmp_path), tmp_path, "test.musicxml"))
    client = TestClient(main_module.app)
    unsupported = client.post(
        f"/api/scores/{key}/vocal-stems/Bass",
        files={"file": ("bass.mp3", b"not wav", "audio/mpeg")},
    )
    assert unsupported.status_code == 415
    invalid = client.post(
        f"/api/scores/{key}/vocal-stems/Bass",
        files={"file": ("bass.wav", b"not wav", "audio/wav")},
    )
    assert invalid.status_code == 422
    monkeypatch.delitem(main_module.scores, key, raising=False)


def test_vocal_stems_can_be_removed(tmp_path, monkeypatch):
    key = "vocal-stem-delete-test"
    monkeypatch.setitem(main_module.scores, key, ScoreRecord(_score(tmp_path), tmp_path, "test.musicxml"))
    client = TestClient(main_module.app)
    uploaded = client.post(
        f"/api/scores/{key}/vocal-stems/Tenor",
        files={"file": ("tenor.wav", _pcm_wav_bytes(), "audio/wav")},
    )
    assert uploaded.status_code == 200
    response = client.delete(f"/api/scores/{key}/vocal-stems/Tenor")
    assert response.status_code == 200
    assert response.json()["stems"]["uploaded_count"] == 0
    monkeypatch.delitem(main_module.scores, key, raising=False)


def test_imported_vocal_stems_are_mixed_with_ffmpeg_for_full_chorus(tmp_path, monkeypatch):
    import io
    import wave
    import app.vocals as vocals

    score = _score(tmp_path)
    stems_dir = tmp_path / "vocal-stems"
    stems_dir.mkdir()
    for voice in ("Soprano", "Alto"):
        path = vocals.vocal_stem_path(stems_dir, voice)
        path.write_bytes(_pcm_wav_bytes(seconds=0.3, frequency=440 if voice == "Soprano" else 330))
        vocals.vocal_stem_metadata_path(stems_dir, voice).write_text(
            json.dumps({"voice": voice, "reference_bpm": 60}), encoding="utf-8"
        )
    result = vocals.mix_vocal_audio(score, stems_dir, ["Soprano", "Alto"], 60, focus_voice="Soprano")
    assert result and result[:4] == b"RIFF"
    with wave.open(io.BytesIO(result), "rb") as mixed:
        assert mixed.getnchannels() == 1
        assert mixed.getframerate() == 22050
        assert mixed.getnframes() > 0
