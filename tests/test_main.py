from fastapi.testclient import TestClient

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
        target.writeframes(b"\\x00\\x00" * 5512)

    encoded = main_module._encode_mp3(pcm.getvalue())
    assert len(encoded) > 128
