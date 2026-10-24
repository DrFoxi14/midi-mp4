"""Local-only FastAPI application for MIDI-MP4."""
from __future__ import annotations
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import json, os, shutil, subprocess, tempfile, uuid, wave, zipfile
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from typing import Literal
from app.core import ScoreData, make_midi_bytes, parse_score, render_preview_wav, find_musescore, piano_backend_info, set_runtime_soundfont
from app.video import VideoExportError, render_score_video, video_dependencies
from app.vocals import (
    MAX_VOCAL_STEM_BYTES, VocalSynthesisError, list_vocal_stems,
    mix_vocal_audio, validate_vocal_wav, vocal_engine_status,
    vocal_stem_metadata_path, vocal_stem_path, voice_slug,
)

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DATA_ROOT = Path(os.environ.get("MIDI_MP4_DATA_DIR", str(Path.home() / ".local" / "share" / "midi-mp4"))).expanduser()
DATA_ROOT.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_SOUNDFONT_BYTES = 512 * 1024 * 1024
ALLOWED_EXTENSIONS = {".musicxml", ".xml", ".mxl", ".mscz"}

class AudioExportError(RuntimeError):
    """A user-correctable local audio-export error."""


def _encode_mp3(wav_data: bytes) -> bytes:
    """Encode generated WAV bytes to MP3 with the machine's local FFmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioExportError(
            "Exportul MP3 necesită FFmpeg instalat local. Pentru audio fără această "
            "dependință suplimentară, alege WAV."
        )

    with tempfile.TemporaryDirectory(prefix="midi-mp4-audio-") as temporary:
        directory = Path(temporary)
        wav_path = directory / "source.wav"
        mp3_path = directory / "export.mp3"
        try:
            wav_path.write_bytes(wav_data)
            result = subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(wav_path), "-vn",
                    "-codec:a", "libmp3lame", "-b:a", "192k",
                    str(mp3_path),
                ],
                capture_output=True, text=True, check=False, timeout=1800,
            )
        except subprocess.TimeoutExpired as exc:
            raise AudioExportError("Codarea MP3 a depășit 30 de minute și a fost oprită.") from exc
        except OSError as exc:
            raise AudioExportError("Nu am putut porni FFmpeg pentru exportul MP3: " + str(exc)) from exc

        if result.returncode != 0 or not mp3_path.exists() or mp3_path.stat().st_size == 0:
            detail = (result.stderr or result.stdout or "FFmpeg nu a produs un fișier MP3.").strip()[-900:]
            raise AudioExportError("Exportul MP3 a eșuat: " + detail)
        try:
            return mp3_path.read_bytes()
        except OSError as exc:
            raise AudioExportError("Nu am putut citi MP3-ul generat: " + str(exc)) from exc


@dataclass
class ScoreRecord:
    score: ScoreData
    directory: Path
    original_name: str

scores: dict[str, ScoreRecord] = {}
video_jobs: dict[str, dict] = {}
app = FastAPI(title="MIDI-MP4 Local", version="0.1.0",
              description="Local-only SATB score analysis and MIDI generation.",
              docs_url="/api/docs", redoc_url=None)

def get_score(score_id: str) -> ScoreRecord:
    record = scores.get(score_id)
    if not record:
        raise HTTPException(status_code=404, detail="Partitura nu există în sesiunea curentă. Import-o din nou.")
    return record

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")

@app.get("/vendor/osmd.js", include_in_schema=False)
def osmd_vendor() -> FileResponse:
    candidates = [
        ROOT / "node_modules" / "opensheetmusicdisplay" / "build" / "opensheetmusicdisplay.min.js",
        ROOT / "node_modules" / "opensheetmusicdisplay" / "build" / "opensheetmusicdisplay.js",
    ]
    found = next((p for p in candidates if p.exists()), None)
    if not found:
        raise HTTPException(status_code=503, detail="Lipsește pachetul local OSMD. Rulează npm install în folderul proiectului și reîncarcă pagina.")
    return FileResponse(found, media_type="text/javascript")

@app.get("/api/health")
def health() -> dict:
    dependencies = video_dependencies()
    return {
        "status": "ok",
        "mode": "local-only",
        "musescore_cli": find_musescore() is not None,
        "piano_backend": piano_backend_info(),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "osmd_installed": any((ROOT / "node_modules" / "opensheetmusicdisplay" / "build" / f).exists()
                              for f in ("opensheetmusicdisplay.min.js", "opensheetmusicdisplay.js")),
        "video_dependencies": dependencies,
        "video_export_available": all(dependencies.values()),
        "video_export": "available" if all(dependencies.values()) else "missing-local-dependencies",
        "vocal_synthesis": vocal_engine_status(),
    }


@app.post("/api/settings/soundfont")
async def upload_soundfont(file: UploadFile = File(...)) -> dict:
    """Save a sampled instrument file to this machine; no external service is involved."""
    original_name = Path(file.filename or "piano.sf2").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in {".sf2", ".sf3"}:
        raise HTTPException(status_code=415, detail="Alege un SoundFont .sf2 sau .sf3.")
    settings_dir = DATA_ROOT / "settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    temp_path = settings_dir / ("upload-" + uuid.uuid4().hex + suffix)
    size = 0
    try:
        with temp_path.open("wb") as destination:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_SOUNDFONT_BYTES:
                    raise HTTPException(status_code=413, detail="SoundFont-ul depășește limita de 512 MB.")
                destination.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="Fișierul SoundFont este gol.")
        final_path = settings_dir / ("active-piano" + suffix)
        temp_path.replace(final_path)
        # Keep only the currently selected, uploaded SoundFont. Never delete any
        # SoundFont supplied by the user through MIDI_MP4_SOUNDFONT outside this folder.
        for old_path in settings_dir.glob("active-piano.sf?"):
            if old_path != final_path:
                old_path.unlink(missing_ok=True)
        set_runtime_soundfont(str(final_path))
        status = piano_backend_info()
        return {"saved": True, "filename": original_name, "bytes": size, "piano": status}
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Nu am putut salva SoundFont-ul local: " + str(exc)) from exc
    finally:
        await file.close()
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


@app.delete("/api/settings/soundfont")
def clear_uploaded_soundfont() -> dict:
    settings_dir = DATA_ROOT / "settings"
    removed = False
    for path in settings_dir.glob("active-piano.sf?"):
        try:
            path.unlink(missing_ok=True)
            removed = True
        except OSError as exc:
            raise HTTPException(status_code=500, detail="Nu am putut elimina SoundFont-ul selectat: " + str(exc)) from exc
    set_runtime_soundfont(None)
    return {"removed": removed, "piano": piano_backend_info() }


class VideoExportRequest(BaseModel):
    reference_bpm: float = Field(default=100, ge=30, le=240)
    resolution: Literal["1080p", "4k"] = "1080p"
    voice: str = "all"
    audio_mode: Literal["isolated", "accompaniment"] = "isolated"
    offset_ms: int = Field(default=0, ge=-5000, le=5000)
    format: Literal["mp4", "mov"] = "mp4"
    accept_reflowed_engraving: bool = False


def _run_video_job(job_id: str, score_id: str, options: dict) -> None:
    job = video_jobs.get(job_id)
    if not job:
        return
    job["status"] = "running"
    job["progress"] = 0.01
    job["message"] = "Pornesc exportul local."
    work_dir: Path | None = None
    try:
        record = get_score(score_id)
        exports_dir = record.directory / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)
        safe_title = "".join(ch for ch in record.score.title if ch.isalnum() or ch in "-_ ").strip()[:60] or "score"
        voice_slug = "satb" if options["voice"] == "all" else "".join(
            ch for ch in options["voice"].lower() if ch.isalnum() or ch in "-_"
        )
        filename = f"{safe_title}-{voice_slug}-{job_id[:8]}.{options['format']}"
        # Frames and the temporary WAV live in a job-unique directory. Two valid
        # concurrent exports must never delete or overwrite one another's files.
        work_dir = exports_dir / (".work-" + job_id)
        work_dir.mkdir(parents=False, exist_ok=False)
        output_path = work_dir / filename
        final_path = exports_dir / filename

        def update_progress(progress: float, message: str) -> None:
            job["progress"] = max(0.0, min(1.0, float(progress)))
            job["message"] = message

        result = render_score_video(
            record.score,
            Path(record.score.musicxml_path),
            output_path,
            reference_bpm=options["reference_bpm"],
            resolution=options["resolution"],
            voice=options["voice"],
            audio_mode=options["audio_mode"],
            offset_ms=options["offset_ms"],
            progress=update_progress,
            vocal_stems_dir=record.directory / "vocal-stems",
        )
        output_path.replace(final_path)
        job.update({
            "status": "completed", "progress": 1.0, "message": "Export video gata.",
            "path": str(final_path), "filename": final_path.name,
            "bytes": result["bytes"], "duration_seconds": result["duration_seconds"],
            "frames": result["frames"], "resolution": result["resolution"],
            "format": result["format"], "renderer": result["renderer"],
        })
    except (VideoExportError, ValueError) as exc:
        job.update({"status": "failed", "progress": 1.0, "message": str(exc), "error": str(exc)})
    except Exception as exc:
        job.update({
            "status": "failed", "progress": 1.0,
            "message": "Exportul video a eșuat din cauza unei erori interne.",
            "error": str(exc)[:1000],
        })
    finally:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)


@app.post("/api/scores/{score_id}/video-jobs", status_code=202)
def start_video_job(score_id: str, request: VideoExportRequest, background_tasks: BackgroundTasks) -> dict:
    record = get_score(score_id)
    if not request.accept_reflowed_engraving:
        raise HTTPException(
            status_code=422,
            detail="Exportul folosește gravură Verovio, care poate modifica aspectul față de MuseScore. "
                   "Confirmă explicit în interfață că accepți o versiune rearanjată."
        )
    if request.voice != "all" and request.voice not in record.score.part_names:
        raise HTTPException(status_code=422, detail="Voce necunoscută: " + request.voice)
    active_jobs = sum(1 for j in video_jobs.values() if j.get("status") in {"queued", "running"})
    if active_jobs >= 2:
        raise HTTPException(status_code=429, detail="Sunt deja două exporturi video în lucru. Așteaptă să se termine unul.")
    job_id = uuid.uuid4().hex
    options = request.model_dump()
    if options["voice"] == "all":
        options["audio_mode"] = "accompaniment"
    video_jobs[job_id] = {
        "job_id": job_id, "score_id": score_id, "status": "queued",
        "progress": 0.0, "message": "În coada de export.", "filename": None,
    }
    background_tasks.add_task(_run_video_job, job_id, score_id, options)
    return {"job_id": job_id, "status": "queued", "status_url": f"/api/video-jobs/{job_id}"}


@app.get("/api/video-jobs/{job_id}")
def video_job_status(job_id: str) -> dict:
    job = video_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job video inexistent în sesiunea curentă.")
    result = {key: value for key, value in job.items() if key not in {"path", "error"}}
    if job.get("status") == "completed":
        result["download_url"] = f"/api/video-jobs/{job_id}/download"
    if job.get("status") == "failed":
        result["error"] = job.get("error", job.get("message", "Export eșuat."))
    return result


@app.get("/api/video-jobs/{job_id}/download")
def download_video_job(job_id: str) -> FileResponse:
    job = video_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job video inexistent în sesiunea curentă.")
    if job.get("status") != "completed" or not job.get("path"):
        raise HTTPException(status_code=409, detail="Exportul video nu este gata.")
    path = Path(job["path"])
    if not path.exists():
        raise HTTPException(status_code=410, detail="Fișierul video temporar nu mai există.")
    return FileResponse(path, filename=job.get("filename", path.name),
                        media_type="video/mp4" if path.suffix.lower() == ".mp4" else "video/quicktime")

@app.post("/api/scores")
async def upload_score(file: UploadFile = File(...)) -> dict:
    original_name = Path(file.filename or "score.musicxml").name
    ext = Path(original_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Format nesuportat. Folosește MusicXML (.musicxml/.xml), .mxl sau MuseScore (.mscz).")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Fișierul încărcat este gol.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Fișierul depășește limita de 50 MB.")
    score_id = uuid.uuid4().hex
    folder = DATA_ROOT / score_id
    folder.mkdir(parents=True, exist_ok=False)
    source_path = folder / ("source" + ext)
    source_path.write_bytes(data)
    try:
        score = await run_in_threadpool(parse_score, source_path, folder)
        if score.title.strip().lower() in {"source", "score"}:
            score.title = Path(original_name).stem
    except ValueError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(status_code=422, detail="Partitura nu a putut fi procesată: " + str(exc)) from exc
    scores[score_id] = ScoreRecord(score, folder, original_name)
    return {"score_id": score_id, "source_name": original_name,
            "score_url": f"/api/scores/{score_id}/musicxml", **score.public_dict()}

@app.get("/api/scores/{score_id}")
def score_info(score_id: str) -> dict:
    record = get_score(score_id)
    return {"score_id": score_id, "source_name": record.original_name, **record.score.public_dict()}

@app.get("/api/scores/{score_id}/musicxml")
def score_musicxml(score_id: str) -> FileResponse:
    record = get_score(score_id)
    path = Path(record.score.musicxml_path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="MusicXML-ul temporar nu mai există. Importă partitura din nou.")
    safe_title = "".join(ch for ch in record.score.title if ch.isalnum() or ch in " -_").strip()[:80] or "score"
    return FileResponse(path, media_type="application/vnd.recordare.musicxml+xml",
                        filename=f"{safe_title}.musicxml")

@app.get("/api/scores/{score_id}/midi")
def download_midi(score_id: str, reference_bpm: float = Query(100.0, ge=30, le=240),
                  voice: str = Query("all")) -> Response:
    record = get_score(score_id)
    if voice not in (set(record.score.part_names) | {"all"}):
        raise HTTPException(status_code=422, detail=f"Voce necunoscută: {voice}")
    try:
        midi = make_midi_bytes(record.score, reference_bpm, voice)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    safe_name = "".join(ch for ch in record.score.title if ch.isalnum() or ch in " -_").strip()[:80] or "score"
    suffix = "-SATB" if voice == "all" else "-" + "".join(ch for ch in voice if ch.isalnum() or ch in "-_")
    return Response(content=midi, media_type="audio/midi",
                    headers={"Content-Disposition": f'attachment; filename="{safe_name}{suffix}.mid"'})

@app.get("/api/scores/{score_id}/preview.wav")
async def preview_wav(score_id: str, reference_bpm: float = Query(100.0, ge=30, le=240),
                      voices: str = Query("all")) -> Response:
    record = get_score(score_id)
    available = set(record.score.part_names)
    selected = list(available) if voices == "all" else [v for v in voices.split(",") if v]
    unknown = sorted(set(selected) - available)
    if unknown:
        raise HTTPException(status_code=422, detail="Voce necunoscută: " + ", ".join(unknown))
    if not selected:
        raise HTTPException(status_code=422, detail="Selectează cel puțin o voce pentru preview.")
    try:
        # Timing correction belongs to the visual cursor, not the audio track.
        # Keep audio events anchored at t=0 so the frontend can shift the score against it.
        audio = await run_in_threadpool(render_preview_wav, record.score, reference_bpm, selected, 0)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(content=audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@app.get("/api/scores/{score_id}/audio")
async def download_audio(
    score_id: str,
    audio_format: Literal["wav", "mp3"] = Query(default="wav", alias="format"),
    reference_bpm: float = Query(100.0, ge=30, le=240),
    voice: str = Query("all"),
    mode: Literal["isolated", "accompaniment"] = Query("isolated"),
) -> Response:
    """Download a local WAV/MP3 render of the score, a solo part, or a lead-plus-accompaniment mix."""
    record = get_score(score_id)
    available = [
        part for part in (record.score.voice_order or list(record.score.part_names))
        if part in record.score.part_names
    ]
    if voice != "all" and voice not in record.score.part_names:
        raise HTTPException(status_code=422, detail="Voce necunoscută: " + voice)

    if voice == "all":
        selected_voices = available
        focus_voice = None
        voice_slug = "SATB"
    elif mode == "isolated":
        selected_voices = [voice]
        focus_voice = None
        voice_slug = "".join(ch for ch in voice.lower() if ch.isascii() and (ch.isalnum() or ch in "-_"))
    else:
        selected_voices = available
        focus_voice = voice
        voice_slug = "".join(ch for ch in voice.lower() if ch.isascii() and (ch.isalnum() or ch in "-_")) + "-accompaniment"

    if not selected_voices:
        raise HTTPException(status_code=422, detail="Partitura nu conține voci exportabile.")

    try:
        vocal_wav = await run_in_threadpool(
            mix_vocal_audio, record.score, record.directory / "vocal-stems",
            selected_voices, reference_bpm, focus_voice,
        )
        wav_data = vocal_wav if vocal_wav is not None else await run_in_threadpool(
            render_preview_wav, record.score, reference_bpm, selected_voices, 0, focus_voice
        )
        audio_data = (
            wav_data if audio_format == "wav"
            else await run_in_threadpool(_encode_mp3, wav_data)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AudioExportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except VocalSynthesisError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    safe_title = "".join(
        ch for ch in record.score.title if ch.isascii() and (ch.isalnum() or ch in " -_")
    ).strip().replace(" ", "_")[:80] or "score"
    filename = f"{safe_title}-{voice_slug}.{audio_format}"
    return Response(
        content=audio_data,
        media_type="audio/wav" if audio_format == "wav" else "audio/mpeg",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )




def _write_audio_pack_zip(
    score: ScoreData,
    archive_path: Path,
    reference_bpm: float,
    audio_format: str,
    safe_title: str,
    stems_dir: Path,
) -> None:
    """Render ZIP members off the event loop, one audio file at a time."""
    voices = [
        part for part in (score.voice_order or list(score.part_names))
        if part in score.part_names
    ]
    manifest = [
        "MIDI-MP4 — Pachet de repetiție",
        f"Partitură: {score.title}",
        f"Tempo de referință: {float(reference_bpm):g} BPM",
        f"Format audio: {audio_format.upper()}",
        "",
        "SATB-fullmix conține toate vocile simultan.",
        "Fiecare fișier cu numele unei voci conține numai acea voce.",
        "Fișierele sunt randate local din partitura importată.",
    ]
    with zipfile.ZipFile(
        archive_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=3
    ) as archive:
        mix_wav = mix_vocal_audio(score, stems_dir, voices, reference_bpm)
        if mix_wav is None:
            mix_wav = render_preview_wav(score, reference_bpm, voices, 0)
        mix_data = mix_wav if audio_format == "wav" else _encode_mp3(mix_wav)
        archive.writestr(f"{safe_title}-SATB-fullmix.{audio_format}", mix_data)
        del mix_wav, mix_data

        for voice_name in voices:
            voice_wav = mix_vocal_audio(score, stems_dir, [voice_name], reference_bpm)
            if voice_wav is None:
                voice_wav = render_preview_wav(score, reference_bpm, [voice_name], 0)
            voice_data = voice_wav if audio_format == "wav" else _encode_mp3(voice_wav)
            voice_slug = "".join(
                ch for ch in voice_name.lower()
                if ch.isascii() and (ch.isalnum() or ch in "-_")
            ) or "part"
            archive.writestr(f"{safe_title}-{voice_slug}.{audio_format}", voice_data)
            manifest.append(f"{voice_name}: {safe_title}-{voice_slug}.{audio_format}")
            del voice_wav, voice_data

        archive.writestr("README.txt", "\\n".join(manifest) + "\\n")



def _write_openutau_project_zip(
    record: ScoreRecord,
    archive_path: Path,
    reference_bpm: float,
) -> None:
    """Package a lyric-bearing MusicXML score and per-part MIDI for OpenUtau."""
    xml_path = Path(record.score.musicxml_path)
    if not xml_path.is_file() or xml_path.stat().st_size == 0:
        raise ValueError("MusicXML-ul normalizat nu mai există. Reimportă partitura.")
    voices = [
        voice for voice in (record.score.voice_order or list(record.score.part_names))
        if voice in record.score.part_names
    ]
    safe_title = "".join(
        ch for ch in record.score.title if ch.isascii() and (ch.isalnum() or ch in " -_")
    ).strip().replace(" ", "_")[:80] or "score"
    readme = f"""# Proiect vocal pentru OpenUtau / DiffSinger

Partitură: {record.score.title}
Tempo MIDI de referință: {float(reference_bpm):g} BPM

## Ce conține pachetul

- `{safe_title}.musicxml`: partitura MusicXML normalizată. Dacă sursa conține versuri, acestea sunt păstrate pe cât permite conversia MusicXML.
- `MIDI/SATB.mid`: toate partidele într-un fișier MIDI multitrack.
- `MIDI/<voce>.mid`: MIDI separat pentru fiecare voce detectată.
- `manifest.json`: maparea vocilor și setările exportului.

## Cum obții voci cântate realiste

1. Instalează OpenUtau din https://www.openutau.com/ și adaugă voicebank-uri/singers compatibile. Proiectul nu include modele vocale și nu descarcă automat voicebank-uri.
2. Deschide fișierul MusicXML în OpenUtau. OpenUtau suportă importul MusicXML; MIDI-urile separate sunt alternative dacă importul partiturii nu mapează corect o voce.
3. Alege un singer pentru fiecare track și un phonemizer potrivit limbii voicebank-ului. DiffSinger necesită voicebank și vocoder compatibile. Versurile trebuie să existe în partitură; dacă lipsesc, adaugă silabe/vocale în OpenUtau înainte de randare.
4. Randează/exportă câte un WAV pentru fiecare voce, la exact {float(reference_bpm):g} BPM.
5. În MIDI-MP4, selectează vocea și importă WAV-ul în secțiunea „Voci realiste”. După ce ai importat stem-urile dorite, exporturile audio/video le vor folosi; vocile fără stem rămân pe sintetizatorul local.

## Limite

OpenUtau este un editor/render de voci extern, nu un serviciu headless integrat în MIDI-MP4. Aplicația nu pretinde că generează automat o voce neurală fără un model/singer instalat. Calitatea, limba și licența audio depind de voicebank-ul ales. Verifică licența fiecărui voicebank înainte de distribuirea rezultatului.
"""
    manifest = {
        "title": record.score.title,
        "reference_bpm": float(reference_bpm),
        "source_musicxml": f"{safe_title}.musicxml",
        "voices": voices,
        "midi_files": {voice: f"MIDI/{voice_slug(voice)}.mid" for voice in voices},
        "notes": "MIDI timing and pitch are included. Lyrics belong to the MusicXML when present; MIDI does not carry lyrics.",
    }
    with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
        archive.write(xml_path, f"{safe_title}.musicxml")
        archive.writestr("MIDI/SATB.mid", make_midi_bytes(record.score, reference_bpm, "all"))
        for voice in voices:
            archive.writestr(
                f"MIDI/{voice_slug(voice)}.mid",
                make_midi_bytes(record.score, reference_bpm, voice),
            )
        archive.writestr("README-OpenUtau.md", readme)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))


@app.get("/api/vocals/status")
def vocals_status() -> dict:
    return vocal_engine_status()


@app.get("/api/scores/{score_id}/vocal-stems")
def get_vocal_stems(score_id: str) -> dict:
    record = get_score(score_id)
    voices = list(record.score.voice_order or record.score.part_names.keys())
    result = list_vocal_stems(record.directory / "vocal-stems", voices)
    result["engine"] = vocal_engine_status()
    return result


@app.post("/api/scores/{score_id}/vocal-stems/{voice}")
async def upload_vocal_stem(
    score_id: str,
    voice: str,
    file: UploadFile = File(...),
    reference_bpm: float = Query(100.0, ge=30, le=240),
) -> dict:
    """Import a real/AI-synthesized voice WAV exported by OpenUtau or another local singer."""
    record = get_score(score_id)
    if voice not in record.score.part_names:
        await file.close()
        raise HTTPException(status_code=422, detail="Voce necunoscută: " + voice)
    original_name = Path(file.filename or "voice.wav").name
    if Path(original_name).suffix.lower() != ".wav":
        await file.close()
        raise HTTPException(status_code=415, detail="Importă un WAV PCM exportat din motorul vocal.")
    stems_dir = record.directory / "vocal-stems"
    stems_dir.mkdir(parents=True, exist_ok=True)
    temp_path = stems_dir / (".upload-" + uuid.uuid4().hex + ".tmp")
    try:
        size = 0
        with temp_path.open("wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_VOCAL_STEM_BYTES:
                    raise HTTPException(status_code=413, detail="WAV-ul vocal depășește limita de 256 MB.")
                output.write(chunk)
        info = validate_vocal_wav(temp_path)
        final_path = vocal_stem_path(stems_dir, voice)
        meta_path = vocal_stem_metadata_path(stems_dir, voice)
        meta_temp = stems_dir / (".metadata-" + uuid.uuid4().hex + ".tmp")
        metadata = {
            "voice": voice,
            "source_filename": original_name,
            "reference_bpm": float(reference_bpm),
            **info,
        }
        meta_temp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(final_path)
        meta_temp.replace(meta_path)
        return {
            "uploaded": True,
            "voice": voice,
            "reference_bpm": float(reference_bpm),
            "duration_seconds": info["duration_seconds"],
            "stems": list_vocal_stems(stems_dir, list(record.score.voice_order or record.score.part_names.keys())),
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Nu am putut salva stemul vocal local: " + str(exc)) from exc
    finally:
        await file.close()
        temp_path.unlink(missing_ok=True)


@app.delete("/api/scores/{score_id}/vocal-stems/{voice}")
def delete_vocal_stem(score_id: str, voice: str) -> dict:
    record = get_score(score_id)
    if voice not in record.score.part_names:
        raise HTTPException(status_code=422, detail="Voce necunoscută: " + voice)
    stems_dir = record.directory / "vocal-stems"
    vocal_stem_path(stems_dir, voice).unlink(missing_ok=True)
    vocal_stem_metadata_path(stems_dir, voice).unlink(missing_ok=True)
    return {
        "deleted": True,
        "stems": list_vocal_stems(stems_dir, list(record.score.voice_order or record.score.part_names.keys())),
    }


@app.get("/api/scores/{score_id}/vocal-project.zip")
async def download_openutau_project(
    score_id: str,
    reference_bpm: float = Query(100.0, ge=30, le=240),
) -> FileResponse:
    record = get_score(score_id)
    temp_dir = Path(tempfile.mkdtemp(prefix="midi-mp4-openutau-"))
    archive_path = temp_dir / "openutau-vocal-project.zip"
    try:
        await run_in_threadpool(_write_openutau_project_zip, record, archive_path, reference_bpm)
    except ValueError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Nu am putut pregăti proiectul OpenUtau: " + str(exc)[:500]) from exc
    safe_title = "".join(
        ch for ch in record.score.title if ch.isascii() and (ch.isalnum() or ch in " -_")
    ).strip().replace(" ", "_")[:80] or "score"
    return FileResponse(
        archive_path,
        filename=f"{safe_title}-OpenUtau.zip",
        media_type="application/zip",
        background=BackgroundTask(shutil.rmtree, str(temp_dir), ignore_errors=True),
    )


@app.get("/api/scores/{score_id}/audio-pack.zip")
async def download_audio_pack(
    score_id: str,
    audio_format: Literal["wav", "mp3"] = Query(default="wav", alias="format"),
    reference_bpm: float = Query(100.0, ge=30, le=240),
) -> FileResponse:
    """Build a local rehearsal ZIP with the full SATB mix and one isolated file per part."""
    record = get_score(score_id)
    voices = [
        part for part in (record.score.voice_order or list(record.score.part_names))
        if part in record.score.part_names
    ]
    if not voices:
        raise HTTPException(status_code=422, detail="Partitura nu conține voci exportabile.")

    temporary_dir = Path(tempfile.mkdtemp(prefix="midi-mp4-audio-pack-"))
    archive_path = temporary_dir / "rehearsal-pack.zip"
    safe_title = "".join(
        ch for ch in record.score.title if ch.isascii() and (ch.isalnum() or ch in " -_")
    ).strip().replace(" ", "_")[:80] or "score"

    try:
        # Keep waveform generation and ZIP compression off the ASGI event loop.
        await run_in_threadpool(
            _write_audio_pack_zip,
            record.score,
            archive_path,
            reference_bpm,
            audio_format,
            safe_title,
            record.directory / "vocal-stems",
        )
    except ValueError as exc:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AudioExportError as exc:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500,
            detail="Nu am putut genera pachetul audio local: " + str(exc)[:500],
        ) from exc

    return FileResponse(
        archive_path,
        filename=f"{safe_title}-rehearsal-pack.zip",
        media_type="application/zip",
        background=BackgroundTask(shutil.rmtree, str(temporary_dir), ignore_errors=True),
    )


@app.delete("/api/scores/{score_id}")
def delete_score(score_id: str) -> dict:
    record = get_score(score_id)
    if any(j.get("score_id") == score_id and j.get("status") in {"queued", "running"} for j in video_jobs.values()):
        raise HTTPException(status_code=409, detail="Partitura are un export video în curs. Așteaptă să se termine.")
    scores.pop(score_id, None)
    shutil.rmtree(record.directory, ignore_errors=True)
    return {"deleted": True, "score_id": score_id}

app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8765, reload=False)
