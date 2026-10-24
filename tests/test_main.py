from fastapi.testclient import TestClient
from pathlib import Path
import json
import pytest

import app.main as main_module
from app.core import NoteEvent, ScoreData, TempoEvent, parse_score
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

    def fake_render(score, reference_bpm, voices, offset_ms=0, focus_voice=None, volume_by_voice=None):
        observed["reference_bpm"] = reference_bpm
        observed["voices"] = voices
        observed["offset_ms"] = offset_ms
        observed["volume_by_voice"] = volume_by_voice
        return b"fake-wav-data"

    monkeypatch.setattr(main_module, "render_preview_wav", fake_render)
    client = TestClient(main_module.app)
    response = client.get(
        f"/api/scores/{key}/preview.wav?reference_bpm=110&voices=Soprano&offset_ms=250&volumes=%7B%22Soprano%22%3A50%7D"
    )
    assert response.status_code == 200
    assert response.content == b"fake-wav-data"
    assert observed == {
        "reference_bpm": 110,
        "voices": ["Soprano"],
        "offset_ms": 0,
        "volume_by_voice": {"Soprano": 50},
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

def test_preview_rejects_invalid_mixer_volume(tmp_path, monkeypatch):
    key = "invalid-mixer-volume-test"
    monkeypatch.setitem(main_module.scores, key,
                        ScoreRecord(_score(tmp_path), tmp_path, "test.musicxml"))
    client = TestClient(main_module.app)
    response = client.get(
        f"/api/scores/{key}/preview.wav",
        params={"voices": "Soprano", "volumes": '{"Soprano":101}'},
    )
    assert response.status_code == 422
    assert "între 0 și 100" in response.json()["detail"]
    monkeypatch.delitem(main_module.scores, key, raising=False)


def test_video_export_rejects_invalid_mixer_volume(tmp_path, monkeypatch):
    key = "invalid-video-mixer-test"
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
        "accept_reflowed_engraving": True,
        "volume_by_voice": {"Soprano": 101},
    })
    assert response.status_code == 422
    assert "între 0 și 100" in response.json()["detail"]
    assert not main_module.video_jobs
    monkeypatch.delitem(main_module.scores, key, raising=False)


def _measured_score(tmp_path):
    score = _score(tmp_path)
    for event in score.notes:
        event.measure = 1
    return score


def test_score_analysis_endpoint_returns_measure_ranges(tmp_path, monkeypatch):
    key = "analysis-endpoint-test"
    score = _measured_score(tmp_path)
    monkeypatch.setitem(main_module.scores, key, ScoreRecord(score, tmp_path, "test.musicxml"))
    client = TestClient(main_module.app)
    response = client.get(f"/api/scores/{key}/analysis")
    assert response.status_code == 200
    payload = response.json()
    assert payload["measure_count"] == 1
    assert payload["measures"][0]["number"] == "1"
    assert "issues" in payload and "summary" in payload
    monkeypatch.delitem(main_module.scores, key, raising=False)


def test_annotations_are_persisted_and_validated(tmp_path, monkeypatch):
    key = "annotations-endpoint-test"
    score = _measured_score(tmp_path)
    monkeypatch.setitem(main_module.scores, key, ScoreRecord(score, tmp_path, "test.musicxml"))
    client = TestClient(main_module.app)
    valid = [{
        "id": "ann-one", "measure": "1", "voice": "Soprano",
        "category": "respiration", "text": "Respirație înainte de intrare",
    }]
    saved = client.put(f"/api/scores/{key}/annotations", json=valid)
    assert saved.status_code == 200
    assert saved.json()["saved"] is True
    loaded = client.get(f"/api/scores/{key}/annotations")
    assert loaded.status_code == 200
    assert loaded.json()["annotations"][0]["text"] == valid[0]["text"]

    invalid = client.put(f"/api/scores/{key}/annotations", json=[{
        "measure": "99", "voice": "Soprano", "category": "general", "text": "Invalid",
    }])
    assert invalid.status_code == 422
    monkeypatch.delitem(main_module.scores, key, raising=False)


def test_rehearsal_pack_contains_score_audio_midi_analysis_and_choir_pages(tmp_path, monkeypatch):
    key = "rehearsal-pack-test"
    score = _score(tmp_path)
    score.musicxml_path = str(tmp_path / "unused.musicxml")
    Path(score.musicxml_path).write_text("<score-partwise/>", encoding="utf-8")
    monkeypatch.setitem(main_module.scores, key, ScoreRecord(score, tmp_path, "test.musicxml"))
    monkeypatch.setattr(main_module, "make_midi_bytes", lambda *args, **kwargs: b"MThd-test-midi")
    monkeypatch.setattr(main_module, "render_preview_wav", lambda *args, **kwargs: b"RIFF-test-wav")
    client = TestClient(main_module.app)
    response = client.post(f"/api/scores/{key}/rehearsal-pack", json={
        "reference_bpm": 100, "resolution": "1080p", "include_videos": False,
        "volume_by_voice": {"Soprano": 60},
    })
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    from io import BytesIO
    from zipfile import ZipFile
    with ZipFile(BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert "score.musicxml" in names
        assert "midi/SATB.mid" in names
        assert "audio/SATB.wav" in names
        assert "analysis/preflight.json" in names
        assert "notes/annotations.json" in names
        assert "choir/soprano.html" in names
        assert "README.txt" in names
    monkeypatch.delitem(main_module.scores, key, raising=False)


def test_scan_upload_explains_missing_local_omr_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(main_module, "omr_status", lambda root: {
        "available": False, "message": "Setează OMR_SATB_DIR pentru test."
    })
    client = TestClient(main_module.app)
    response = client.post("/api/scores", files={"file": ("scan.pdf", b"fake-pdf-bytes", "application/pdf")})
    assert response.status_code == 503
    assert "OMR_SATB_DIR" in response.json()["detail"]


def test_saved_score_can_be_restored_after_session_restart(tmp_path, monkeypatch):
    import shutil
    monkeypatch.setattr(main_module, "DATA_ROOT", tmp_path)
    score_id = "a" * 32
    folder = tmp_path / score_id
    folder.mkdir()
    fixture = Path(__file__).parent / "fixtures" / "satb-tiny.musicxml"
    xml = folder / "saved.musicxml"
    shutil.copy2(fixture, xml)
    score = _score(folder)
    score.title = "Persistent rehearsal score"
    score.musicxml_path = str(xml)
    record = ScoreRecord(score, folder, "persistent.musicxml")
    main_module._save_score_record(score_id, record)
    monkeypatch.delitem(main_module.scores, score_id, raising=False)

    client = TestClient(main_module.app)
    response = client.get(f"/api/scores/{score_id}")
    assert response.status_code == 200
    assert response.json()["title"] == "Persistent rehearsal score"
    assert response.json()["note_count"] > 0
    assert response.json()["score_url"] == f"/api/scores/{score_id}/musicxml"
    assert score_id in main_module.scores
    monkeypatch.delitem(main_module.scores, score_id, raising=False)


def test_pdf_upload_invokes_configured_local_omr_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(main_module, "omr_status", lambda root: {"available": True, "message": "ready"})

    def fake_omr(source_path, work_root, application_root, tempo_bpm=100):
        output = work_root / "omr-converted.musicxml"
        output.parent.mkdir(parents=True, exist_ok=True)
        fixture = Path(__file__).parent / "fixtures" / "satb-tiny.musicxml"
        output.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
        return {"musicxml_path": str(output), "analysis_json": None, "pages": 1, "omr_directory": "/fake/omr"}

    monkeypatch.setattr(main_module, "run_omr_to_musicxml", fake_omr)
    client = TestClient(main_module.app)
    response = client.post(
        "/api/scores",
        files={"file": ("choral-draft.pdf", b"fake input handled by mocked OMR", "application/pdf")},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["title"] == "choral-draft"
    assert payload["note_count"] > 0
    score_id = payload["score_id"]
    assert (tmp_path / score_id / "omr-import.json").is_file()
    monkeypatch.delitem(main_module.scores, score_id, raising=False)


def test_intonation_api_returns_single_voice_pitch_report(tmp_path, monkeypatch):
    key = "intonation-api-test"
    monkeypatch.setitem(main_module.scores, key,
                        ScoreRecord(_score(tmp_path), tmp_path, "test.musicxml"))
    observed = {}

    def fake_analysis(audio_bytes, score, voice, reference_bpm, offset_ms, tolerance_cents):
        observed.update({
            "audio_bytes": audio_bytes,
            "voice": voice,
            "reference_bpm": reference_bpm,
            "offset_ms": offset_ms,
            "tolerance_cents": tolerance_cents,
        })
        return {"voice": voice, "summary": {"notes_checked": 1, "matched": 1}, "notes": []}

    monkeypatch.setattr(main_module, "analyze_voice_recording", fake_analysis)
    client = TestClient(main_module.app)
    response = client.post(
        f"/api/scores/{key}/intonation-check",
        params={"voice": "Soprano", "reference_bpm": 110, "offset_ms": 150, "tolerance_cents": 35},
        files={"file": ("practice.wav", b"mocked-wav-payload", "audio/wav")},
    )
    assert response.status_code == 200
    assert response.json()["summary"]["matched"] == 1
    assert observed == {
        "audio_bytes": b"mocked-wav-payload",
        "voice": "Soprano",
        "reference_bpm": 110.0,
        "offset_ms": 150,
        "tolerance_cents": 35.0,
    }
    monkeypatch.delitem(main_module.scores, key, raising=False)


def test_intonation_api_rejects_non_wav_and_unknown_voice(tmp_path, monkeypatch):
    key = "intonation-api-validation-test"
    monkeypatch.setitem(main_module.scores, key,
                        ScoreRecord(_score(tmp_path), tmp_path, "test.musicxml"))
    client = TestClient(main_module.app)
    non_wav = client.post(
        f"/api/scores/{key}/intonation-check",
        params={"voice": "Soprano"},
        files={"file": ("practice.mp3", b"not wav", "audio/mpeg")},
    )
    assert non_wav.status_code == 415
    unknown = client.post(
        f"/api/scores/{key}/intonation-check",
        params={"voice": "Baritone"},
        files={"file": ("practice.wav", b"mocked-wav-payload", "audio/wav")},
    )
    assert unknown.status_code == 422
    monkeypatch.delitem(main_module.scores, key, raising=False)


def test_manual_note_correction_updates_musicxml_and_persisted_score(tmp_path, monkeypatch):
    key = "note-correction-integration-test"
    fixture = Path(__file__).parent / "fixtures" / "satb-tiny.musicxml"
    score = parse_score(fixture, tmp_path)
    target_index = next(
        i for i, event in enumerate(score.notes)
        if event.voice == "Soprano" and not event.is_rest and event.pitch is not None
    )
    old_pitch = int(score.notes[target_index].pitch)
    new_pitch = old_pitch + 2 if old_pitch <= 125 else old_pitch - 2
    monkeypatch.setitem(
        main_module.scores, key,
        ScoreRecord(score, tmp_path, "satb-tiny.musicxml"),
    )
    client = TestClient(main_module.app)
    response = client.put(
        f"/api/scores/{key}/notes/{target_index}",
        json={"pitch": new_pitch},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["saved"] is True
    assert payload["correction"]["old_pitch"] == old_pitch
    assert payload["correction"]["new_pitch"] == new_pitch
    assert any(
        event.voice == "Soprano" and int(event.pitch) == new_pitch
        for event in main_module.scores[key].score.notes
        if not event.is_rest and event.pitch is not None
    )
    history = tmp_path / "manual-corrections.json"
    assert history.is_file()
    assert json.loads(history.read_text(encoding="utf-8"))[-1]["new_pitch"] == new_pitch
    analysis = client.get(f"/api/scores/{key}/analysis")
    assert analysis.status_code == 200
    assert analysis.json()["manual_corrections"][-1]["new_pitch"] == new_pitch
    monkeypatch.delitem(main_module.scores, key, raising=False)


# Audio export and imported singing-stem integration tests.

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
        readme_text = archive.read("README.txt").decode("utf-8")
        assert "Tempo de referință: 120 BPM" in readme_text
        assert "\n" in readme_text
        assert "\\n" not in readme_text
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


def test_local_library_lists_saved_scores_and_reports_restore_status(tmp_path, monkeypatch):
    import shutil

    monkeypatch.setattr(main_module, "DATA_ROOT", tmp_path)
    score_id = "c" * 32
    folder = tmp_path / score_id
    folder.mkdir()
    fixture = Path(__file__).parent / "fixtures" / "satb-tiny.musicxml"
    xml = folder / "saved.musicxml"
    shutil.copy2(fixture, xml)
    score = _score(folder)
    score.title = "Choir rehearsal library item"
    score.musicxml_path = str(xml)
    record = ScoreRecord(score, folder, "choir-rehearsal.musicxml")
    main_module._save_score_record(score_id, record)

    # Listing uses metadata only; the score is restored lazily when opened.
    monkeypatch.delitem(main_module.scores, score_id, raising=False)
    client = TestClient(main_module.app)
    response = client.get("/api/library/scores")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["scores"][0]["score_id"] == score_id
    assert payload["scores"][0]["title"] == "Choir rehearsal library item"
    assert payload["scores"][0]["source_name"] == "choir-rehearsal.musicxml"
    assert payload["scores"][0]["restorable"] is True
    assert score_id not in main_module.scores

    restored = client.get(f"/api/scores/{score_id}")
    assert restored.status_code == 200
    assert restored.json()["title"] == "Choir rehearsal library item"


def test_local_library_lists_broken_record_and_deletes_it_without_touching_neighbor_files(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DATA_ROOT", tmp_path)
    score_id = "d" * 32
    folder = tmp_path / score_id
    folder.mkdir()
    outside = tmp_path / "outside.musicxml"
    outside.write_text("<score-partwise/>", encoding="utf-8")
    (folder / "score-record.json").write_text(json.dumps({
        "score_id": score_id,
        "title": "Broken path record",
        "source_name": "broken.musicxml",
        "musicxml_path": "../outside.musicxml",
    }), encoding="utf-8")

    client = TestClient(main_module.app)
    listed = client.get("/api/library/scores")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["scores"][0]["restorable"] is False

    deleted = client.delete(f"/api/library/scores/{score_id}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert not folder.exists()
    assert outside.is_file()


def test_local_library_rejects_invalid_delete_identifier(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DATA_ROOT", tmp_path)
    client = TestClient(main_module.app)

    response = client.delete("/api/library/scores/../../outside")

    assert response.status_code == 404
    assert list(tmp_path.iterdir()) == []

def test_full_rehearsal_pack_uses_imported_vocal_stems(tmp_path, monkeypatch):
    import io
    import zipfile
    import app.vocals as vocals

    key = "rehearsal-pack-imported-vocal-test"
    score = _score(tmp_path)
    xml = tmp_path / "rehearsal.musicxml"
    xml.write_text("<score-partwise/>", encoding="utf-8")
    score.musicxml_path = str(xml)
    stems_dir = tmp_path / "vocal-stems"
    stems_dir.mkdir()
    vocals.vocal_stem_path(stems_dir, "Soprano").write_bytes(b"placeholder-wav")
    vocals.vocal_stem_metadata_path(stems_dir, "Soprano").write_text(
        json.dumps({"voice": "Soprano", "reference_bpm": 100, "duration_seconds": 1.0}),
        encoding="utf-8",
    )
    monkeypatch.setitem(main_module.scores, key, ScoreRecord(score, tmp_path, "rehearsal.musicxml"))
    monkeypatch.setattr(main_module, "make_midi_bytes", lambda *args, **kwargs: b"MThd-test-midi")
    calls = []

    def fake_mix(render_score, directory, voices, reference_bpm, focus_voice=None,
                 volume_by_voice=None, normalize_duration=False):
        calls.append((tuple(voices), reference_bpm, focus_voice, normalize_duration))
        return ("RIFF-imported:" + ",".join(voices)).encode("utf-8")

    monkeypatch.setattr(main_module, "mix_vocal_audio", fake_mix)
    monkeypatch.setattr(
        main_module, "render_preview_wav",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("synth fallback should not run")),
    )
    client = TestClient(main_module.app)
    response = client.post(f"/api/scores/{key}/rehearsal-pack", json={
        "reference_bpm": 100, "resolution": "1080p", "include_videos": False,
        "volume_by_voice": {"Soprano": 70},
    })
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read("audio/SATB.wav") == b"RIFF-imported:Soprano,Alto,Tenor,Bass"
        assert archive.read("audio/Soprano.wav") == b"RIFF-imported:Soprano"
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        assert manifest["imported_vocal_stems"] == ["Soprano"]
        readme = archive.read("README.txt").decode("utf-8")
        assert "Stem-uri vocale WAV importate: Soprano" in readme
        assert "partidele fără stem folosesc sintetizatorul local" in readme
    assert len(calls) == 5
    assert all(call[3] is True for call in calls)
    monkeypatch.delitem(main_module.scores, key, raising=False)

def test_normalized_single_vocal_stem_is_padded_to_score_duration(tmp_path):
    import io
    import wave
    import shutil
    import app.vocals as vocals

    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is required to verify normalized vocal duration.")

    score = _score(tmp_path)
    stems_dir = tmp_path / "vocal-stems"
    stems_dir.mkdir()
    stem_path = vocals.vocal_stem_path(stems_dir, "Soprano")
    stem_path.write_bytes(_pcm_wav_bytes(seconds=0.30, frequency=440))
    vocals.vocal_stem_metadata_path(stems_dir, "Soprano").write_text(
        json.dumps({"voice": "Soprano", "reference_bpm": 60, "duration_seconds": 0.3}),
        encoding="utf-8",
    )

    raw = vocals.mix_vocal_audio(score, stems_dir, ["Soprano"], 60)
    assert raw == stem_path.read_bytes(), "Solo isolated export should preserve the uploaded WAV by default."

    aligned = vocals.mix_vocal_audio(
        score, stems_dir, ["Soprano"], 60, normalize_duration=True
    )
    with wave.open(io.BytesIO(aligned), "rb") as mixed:
        seconds = mixed.getnframes() / mixed.getframerate()
    assert seconds == pytest.approx(5.25, abs=0.06), (
        "Synchronized playback must pad/trim the imported stem to the score timeline."
    )

def test_isolated_vocal_fader_applies_exact_requested_gain(tmp_path):
    import io
    import struct
    import wave
    import shutil
    import app.vocals as vocals

    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is required to test vocal gain.")

    score = _score(tmp_path)
    stems_dir = tmp_path / "vocal-stems"
    stems_dir.mkdir()
    source_bytes = _pcm_wav_bytes(seconds=0.30, frequency=440)
    stem_path = vocals.vocal_stem_path(stems_dir, "Soprano")
    stem_path.write_bytes(source_bytes)
    vocals.vocal_stem_metadata_path(stems_dir, "Soprano").write_text(
        json.dumps({"voice": "Soprano", "reference_bpm": 60, "duration_seconds": 0.3}),
        encoding="utf-8",
    )

    mixed_bytes = vocals.mix_vocal_audio(
        score, stems_dir, ["Soprano"], 60,
        volume_by_voice={"Soprano": 50},
        normalize_duration=True,
    )
    with wave.open(io.BytesIO(source_bytes), "rb") as source:
        source_data = source.readframes(source.getnframes())
    source_samples = struct.unpack("<" + "h" * (len(source_data) // 2), source_data)
    source_peak = max(abs(sample) for sample in source_samples)
    with wave.open(io.BytesIO(mixed_bytes), "rb") as mixed:
        assert mixed.getnchannels() == 1
        assert mixed.getsampwidth() == 2
        frame_count = mixed.getnframes()
        mixed_data = mixed.readframes(frame_count)
    mixed_samples = struct.unpack("<" + "h" * (len(mixed_data) // 2), mixed_data)
    mixed_peak = max(abs(sample) for sample in mixed_samples)
    assert mixed_peak == pytest.approx(source_peak * 0.5, rel=0.08), (
        "A 50% isolated fader should apply approximately half amplitude, not an extra mix attenuation."
    )

