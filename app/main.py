"""Local-only FastAPI application for MIDI-MP4."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import os, shutil, subprocess, tempfile, uuid
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from typing import Literal
from app.core import ScoreData, make_midi_bytes, parse_score, render_preview_wav, find_musescore, piano_backend_info, set_runtime_soundfont
from app.video import VideoExportError, render_score_video, video_dependencies

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
        wav_data = await run_in_threadpool(
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
