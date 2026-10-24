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
