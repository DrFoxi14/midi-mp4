"""Local-only FastAPI application for MIDI-MP4."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import html
import json, os, re, shutil, subprocess, tempfile, uuid, zipfile
from fastapi import BackgroundTasks, Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from typing import Any, Literal
from datetime import datetime, timezone
from music21 import chord as m21chord, converter as m21converter, note as m21note, pitch as m21pitch
from app.core import ScoreData, make_midi_bytes, parse_score, render_preview_wav, find_musescore, piano_backend_info, set_runtime_soundfont
from app.video import VideoExportError, render_score_video, video_dependencies
from app.conductor import analyze_score, measure_ranges, validate_annotations
from app.omr import omr_status, run_omr_to_musicxml
from app.intonation import IntonationError, MAX_RECORDING_BYTES, analyze_voice_recording
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
DIRECT_EXTENSIONS = {".musicxml", ".xml", ".mxl", ".mscz"}
OMR_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
ALLOWED_EXTENSIONS = DIRECT_EXTENSIONS | OMR_EXTENSIONS

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

def _save_score_record(score_id: str, record: ScoreRecord) -> None:
    """Persist only local relative paths and score metadata so setlists survive restarts."""
    folder = record.directory.resolve()
    try:
        musicxml_relative = Path(record.score.musicxml_path).resolve().relative_to(folder).as_posix()
    except ValueError as exc:
        raise ValueError("Calea MusicXML trebuie să rămână în directorul local al partiturii.") from exc
    source = next((p for p in folder.glob("source.*") if p.is_file()), None)
    source_relative = source.relative_to(folder).as_posix() if source else None
    payload = {
        "score_id": score_id,
        "title": record.score.title,
        "source_name": record.original_name,
        "musicxml_path": musicxml_relative,
        "source_path": source_relative,
    }
    path = folder / "score-record.json"
    temporary = folder / ("score-record-" + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def get_score(score_id: str) -> ScoreRecord:
    record = scores.get(score_id)
    if record:
        return record
    # Score IDs are generated as 32-character UUID hex strings. Validate before
    # using the ID as a path component to prevent traversal outside DATA_ROOT.
    if not re.fullmatch(r"[a-f0-9]{32}", score_id):
        raise HTTPException(status_code=404, detail="ID de partitură invalid.")
    folder = (DATA_ROOT / score_id).resolve()
    try:
        folder.relative_to(DATA_ROOT.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Partitură inexistentă.") from exc
    metadata_path = folder / "score-record.json"
    if not metadata_path.is_file():
        raise HTTPException(status_code=404, detail="Partitura nu este salvată local. Import-o din nou.")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        relative = metadata.get("musicxml_path")
        if not isinstance(relative, str):
            raise ValueError("Lipsește calea MusicXML din metadate.")
        musicxml_path = (folder / relative).resolve()
        musicxml_path.relative_to(folder)
        if not musicxml_path.is_file():
            raise ValueError("Fișierul MusicXML salvat nu mai există.")
        score = parse_score(musicxml_path, folder)
        # The saved MusicXML is already normalized; keep using it instead of
        # creating another "-display" copy every time the score is restored.
        score.musicxml_path = str(musicxml_path)
        score.title = str(metadata.get("title") or score.title)
        original_name = str(metadata.get("source_name") or musicxml_path.name)
        record = ScoreRecord(score, folder, original_name)
        scores[score_id] = record
        return record
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=410,
            detail="Nu am putut restaura partitura salvată. Reimportă originalul. " + str(exc),
        ) from exc


def _validate_voice_volumes(value: object, available_voices: set[str]) -> dict[str, int]:
    """Validate the optional conductor mix before launching a render."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="Mixerul trebuie să fie un obiect cu volum pentru fiecare voce.")
    result: dict[str, int] = {}
    for voice_name, level in value.items():
        if not isinstance(voice_name, str) or voice_name not in available_voices:
            raise HTTPException(status_code=422, detail="Voce necunoscută în mixer: " + str(voice_name))
        if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 100:
            raise HTTPException(
                status_code=422,
                detail=f"Volumul pentru {voice_name} trebuie să fie un număr întreg între 0 și 100.",
            )
        result[voice_name] = level
    return result

def _build_rehearsal_pack(record: ScoreRecord, reference_bpm: float, resolution: str,
                          include_videos: bool, volume_by_voice: dict[str, int]) -> Path:
    """Build an offline ZIP with per-voice audio, MIDI, score, annotations and QA report."""
    if not 30 <= float(reference_bpm) <= 240:
        raise ValueError("Tempo-ul trebuie să fie între 30 și 240 BPM.")
    if resolution not in {"1080p", "4k"}:
        raise ValueError("Rezoluție invalidă.")
    score = record.score
    voices = list(score.voice_order or score.part_names.keys())
    if not voices or set(voices) - set(score.part_names):
        raise ValueError("Maparea vocilor din partitură este inconsistentă.")
    score_path = Path(score.musicxml_path)
    if not score_path.is_file():
        raise ValueError("MusicXML-ul normalizat nu mai există. Reimportă partitura.")
    exports_dir = record.directory / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    safe_title = "".join(ch for ch in score.title if ch.isalnum() or ch in " -_").strip()[:60] or "partitura"
    package_path = exports_dir / f"{safe_title}-rehearsal-pack.zip"
    build_dir = record.directory / ("pack-work-" + uuid.uuid4().hex)
    build_dir.mkdir(parents=False, exist_ok=False)
    manifest = {
        "title": score.title, "source_name": record.original_name,
        "reference_bpm": float(reference_bpm), "voices": voices,
        "include_videos": include_videos, "files": [],
        "privacy": "Generated locally; source scores are not uploaded to a cloud service.",
    }
    try:
        analysis = analyze_score(score)
        annotations_path = record.directory / "annotations.json"
        corrections_path = record.directory / "manual-corrections.json"
        try:
            annotations = json.loads(annotations_path.read_text(encoding="utf-8")) if annotations_path.is_file() else []
            if not isinstance(annotations, list): annotations = []
        except (OSError, json.JSONDecodeError):
            annotations = []
        try:
            corrections = json.loads(corrections_path.read_text(encoding="utf-8")) if corrections_path.is_file() else []
            if not isinstance(corrections, list): corrections = []
        except (OSError, json.JSONDecodeError):
            corrections = []
        analysis["manual_corrections"] = corrections
        analysis["omr_report_stale"] = bool(corrections)
        with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.write(score_path, "score.musicxml")
            manifest["files"].append("score.musicxml")
            for voice in ["all", *voices]:
                key = "SATB" if voice == "all" else "".join(ch for ch in voice if ch.isalnum() or ch in "-_") or "part"
                midi_name = f"midi/{key}.mid"
                archive.writestr(midi_name, make_midi_bytes(score, reference_bpm, voice))
                manifest["files"].append(midi_name)
                selected = voices if voice == "all" else [voice]
                wav_name = f"audio/{key}.wav"
                vocal_audio = mix_vocal_audio(
                    score, record.directory / "vocal-stems", selected, reference_bpm,
                    focus_voice=(voice if voice != "all" else None),
                    volume_by_voice=volume_by_voice,
                )
                if vocal_audio is None:
                    render_kwargs = {"volume_by_voice": volume_by_voice} if volume_by_voice else {}
                    vocal_audio = render_preview_wav(score, reference_bpm, selected, **render_kwargs)
                archive.writestr(wav_name, vocal_audio)
                manifest["files"].append(wav_name)
            for voice in voices:
                slug = "".join(ch for ch in voice.lower().replace(" ", "-") if ch.isalnum() or ch in "-_") or "part"
                key = "".join(ch for ch in voice if ch.isalnum() or ch in "-_") or "part"
                title_text = html.escape(score.title)
                voice_text = html.escape(voice)
                voice_annotations = [
                    annotation for annotation in annotations
                    if annotation.get("voice", "all") in {"all", voice}
                ]
                status_labels = {"needs_work": "De lucrat", "practiced": "Repetată", "ready": "Pregătită"}
                annotation_rows = "".join(
                    "<li><b>M. " + html.escape(str(annotation.get("measure", "—"))) + "</b> · "
                    + html.escape(str(annotation.get("category", "general"))) + " · "
                    + html.escape(status_labels.get(str(annotation.get("status", "needs_work")), "De lucrat"))
                    + "<p>" + html.escape(str(annotation.get("text", ""))) + "</p></li>"
                    for annotation in sorted(voice_annotations, key=lambda item: (str(item.get("measure", "")), str(item.get("category", ""))))
                )
                annotation_html = (
                    "<ul>" + annotation_rows + "</ul>"
                    if annotation_rows else "<p>Nu există încă adnotări pentru această voce.</p>"
                )
                page = f"""<!doctype html>
<html lang="ro"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title_text} — {voice_text}</title>
<style>body{{font:16px system-ui;max-width:760px;margin:36px auto;padding:0 16px;background:#11141a;color:#f2f4f7}}a{{color:#88e0b0}}audio{{width:100%}}.card{{padding:18px;border:1px solid #363b47;border-radius:12px;margin:16px 0}}small{{color:#adb4c2}}</style>
<h1>{title_text}</h1><p>Material de repetiție · <strong>{voice_text}</strong></p>
<section class="card"><h2>Ascultă vocea</h2><audio controls preload="metadata" src="../audio/{key}.wav"></audio>
<p><a href="../audio/{key}.wav">Descarcă audio WAV</a></p></section>
<section class="card"><h2>Partitură și fișiere</h2><p><a href="../score.musicxml">Deschide partitura MusicXML</a></p>
<p><a href="../midi/{key}.mid">Descarcă MIDI {voice_text}</a></p>
<small>Tempo: {float(reference_bpm):.0f} BPM. Deschide indexul offline după dezarhivare.</small></section>
<section class="card"><h2>Puncte de lucru</h2>{annotation_html}</section></html>"""
                page_name = f"choir/{slug}.html"
                archive.writestr(page_name, page)
                manifest["files"].append(page_name)
            archive.writestr("analysis/preflight.json", json.dumps(analysis, ensure_ascii=False, indent=2))
            archive.writestr("notes/annotations.json", json.dumps(annotations, ensure_ascii=False, indent=2))
            archive.writestr("notes/manual-corrections.json", json.dumps(corrections, ensure_ascii=False, indent=2))
            manifest["files"].extend(["analysis/preflight.json", "notes/annotations.json", "notes/manual-corrections.json"])
            omr_report_dir = record.directory / "omr-work" / "omr-analysis"
            if corrections:
                archive.writestr(
                    "analysis/omr-report-status.txt",
                    "Raportul OMR original este anterior corecturilor manuale; nu este inclus ca analiză actuală. "
                    "Reexecută OMR-SATB dacă ai nevoie de un raport armonic actualizat. Consultă notes/manual-corrections.json."
                )
                manifest["files"].append("analysis/omr-report-status.txt")
            else:
                for name in ("analiza.json", "analiza.html", "analiza.txt"):
                    report_path = omr_report_dir / name
                    if report_path.is_file():
                        archive.write(report_path, "analysis/omr-" + name)
                        manifest["files"].append("analysis/omr-" + name)
            if include_videos:
                for voice in ["all", *voices]:
                    key = "SATB" if voice == "all" else "".join(ch.lower() for ch in voice if ch.isalnum() or ch in "-_") or "part"
                    video_path = build_dir / f"{key}.mp4"
                    render_score_video(
                        score, score_path, video_path, reference_bpm=reference_bpm,
                        resolution=resolution, voice=voice, audio_mode="accompaniment",
                        volume_by_voice=volume_by_voice,
                        vocal_stems_dir=record.directory / "vocal-stems",
                    )
                    archive.write(video_path, f"video/{key}.mp4")
                    manifest["files"].append(f"video/{key}.mp4")
            guide = [
                f"PAKET DE REPETIȚIE: {score.title}",
                f"Tempo de referință: {reference_bpm:g} BPM", "",
                "Conținut: MusicXML, MIDI și WAV SATB/pe voci, pagini HTML offline pentru cântăreți,",
                "raport preflight și adnotări. MP4-urile sunt incluse numai dacă au fost cerute.",
                "Redarea audio este sintetizată, nu o înregistrare vocală umană.",
                "Verifică analysis/preflight.json; alertele sunt orientative și nu modifică partitura.",
                "Generat local.",
            ]
            archive.writestr("README.txt", "\n".join(guide))
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return package_path
    except Exception:
        package_path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)

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
        "omr": omr_status(ROOT),
        "scan_import_available": bool(omr_status(ROOT).get("available")),
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
    volume_by_voice: dict[str, int] = Field(default_factory=dict)


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
            volume_by_voice=options.get("volume_by_voice"),
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
    validated_volumes = _validate_voice_volumes(
        request.volume_by_voice, set(record.score.part_names)
    )
    active_jobs = sum(1 for j in video_jobs.values() if j.get("status") in {"queued", "running"})
    if active_jobs >= 2:
        raise HTTPException(status_code=429, detail="Sunt deja două exporturi video în lucru. Așteaptă să se termine unul.")
    job_id = uuid.uuid4().hex
    options = request.model_dump()
    options["volume_by_voice"] = validated_volumes
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
        raise HTTPException(status_code=415, detail="Format nesuportat. Folosește MusicXML/MXL/MSCZ sau PDF/imagine dacă OMR-SATB este configurat local.")
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
        score_source = source_path
        if ext in OMR_EXTENSIONS:
            omr_capability = omr_status(ROOT)
            if not omr_capability.get("available"):
                raise HTTPException(
                    status_code=503,
                    detail=omr_capability.get("message", "OMR-SATB nu este configurat pentru import de scanuri."),
                )
            omr_result = await run_in_threadpool(
                run_omr_to_musicxml, source_path, folder / "omr-work", ROOT, 100.0
            )
            score_source = Path(omr_result["musicxml_path"])
            (folder / "omr-import.json").write_text(
                json.dumps({
                    "original_name": original_name,
                    "pages": omr_result["pages"],
                    "analysis_json": omr_result.get("analysis_json"),
                    "omr_directory": omr_result.get("omr_directory"),
                }, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        score = await run_in_threadpool(parse_score, score_source, folder)
        if ext in OMR_EXTENSIONS or score.title.strip().lower() in {"source", "score"}:
            score.title = Path(original_name).stem
    except HTTPException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    except ValueError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(status_code=422, detail="Partitura nu a putut fi procesată: " + str(exc)) from exc
    record = ScoreRecord(score, folder, original_name)
    scores[score_id] = record
    try:
        _save_score_record(score_id, record)
    except OSError:
        # Processing should remain usable even on a read-only storage location;
        # the UI can still use this session, though the saved setlist won't restore it.
        pass
    return {"score_id": score_id, "source_name": original_name,
            "score_url": f"/api/scores/{score_id}/musicxml", **score.public_dict()}

@app.get("/api/library/scores")
def list_saved_scores() -> dict:
    """List score records persisted under the local data directory without loading every score."""
    root = DATA_ROOT.expanduser().resolve()
    if not root.is_dir():
        return {"count": 0, "scores": []}

    items: list[dict[str, Any]] = []
    try:
        candidates = list(root.iterdir())
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Nu pot citi biblioteca locală: " + str(exc)) from exc

    for candidate in candidates:
        if not candidate.is_dir() or not re.fullmatch(r"[a-f0-9]{32}", candidate.name):
            continue
        try:
            folder = candidate.resolve()
            folder.relative_to(root)
            metadata_path = folder / "score-record.json"
            if not metadata_path.is_file():
                continue
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                continue

            relative = metadata.get("musicxml_path")
            restorable = False
            if isinstance(relative, str) and relative:
                try:
                    musicxml = (folder / relative).resolve()
                    musicxml.relative_to(folder)
                    restorable = musicxml.is_file() and musicxml.stat().st_size > 0
                except (OSError, ValueError):
                    restorable = False

            modified = datetime.fromtimestamp(metadata_path.stat().st_mtime, timezone.utc).isoformat()
            items.append({
                "score_id": candidate.name,
                "title": str(metadata.get("title") or "Partitură fără titlu"),
                "source_name": str(metadata.get("source_name") or ""),
                "updated_at": modified,
                "restorable": restorable,
            })
        except (OSError, json.JSONDecodeError, ValueError):
            # Preserve visibility of a damaged record so the user can remove it.
            try:
                modified = datetime.fromtimestamp((candidate / "score-record.json").stat().st_mtime, timezone.utc).isoformat()
            except OSError:
                modified = None
            items.append({
                "score_id": candidate.name,
                "title": "Partitură inaccesibilă",
                "source_name": "",
                "updated_at": modified,
                "restorable": False,
            })

    items.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item["title"]).casefold()), reverse=True)
    return {"count": len(items), "scores": items}


@app.delete("/api/library/scores/{score_id}")
def delete_saved_library_score(score_id: str) -> dict:
    """Remove a saved local score, including a damaged record that cannot be parsed."""
    if not re.fullmatch(r"[a-f0-9]{32}", score_id):
        raise HTTPException(status_code=404, detail="ID de partitură invalid.")
    root = DATA_ROOT.expanduser().resolve()
    folder = (root / score_id).resolve()
    try:
        folder.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Partitură inexistentă.") from exc
    metadata_path = folder / "score-record.json"
    if not folder.is_dir() or not metadata_path.is_file():
        raise HTTPException(status_code=404, detail="Partitura nu există în biblioteca locală.")
    if any(
        job.get("score_id") == score_id and job.get("status") in {"queued", "running"}
        for job in video_jobs.values()
    ):
        raise HTTPException(status_code=409, detail="Partitura are un export video în curs. Așteaptă să se termine.")
    scores.pop(score_id, None)
    try:
        shutil.rmtree(folder)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Nu am putut șterge fișierele locale: " + str(exc)) from exc
    return {"deleted": True, "score_id": score_id}


@app.get("/api/scores/{score_id}")
def score_info(score_id: str) -> dict:
    record = get_score(score_id)
    return {"score_id": score_id, "source_name": record.original_name, **record.score.public_dict()}

class NoteCorrectionRequest(BaseModel):
    pitch: int = Field(ge=0, le=127)


@app.put("/api/scores/{score_id}/notes/{note_index}")
def correct_score_note(score_id: str, note_index: int, request: NoteCorrectionRequest) -> dict:
    """Apply a manual pitch correction to a note and persist the updated MusicXML."""
    record = get_score(score_id)
    if note_index < 0 or note_index >= len(record.score.notes):
        raise HTTPException(status_code=404, detail="Indicele notei nu există în partitura curentă.")
    target_event = record.score.notes[note_index]
    if target_event.is_rest or target_event.pitch is None:
        raise HTTPException(status_code=422, detail="Poți corecta doar o notă cu înălțime definită, nu o pauză.")
    old_pitch = int(target_event.pitch)
    new_pitch = int(request.pitch)
    if old_pitch == new_pitch:
        return {"saved": False, "unchanged": True, "score_id": score_id, **record.score.public_dict()}

    xml_path = Path(record.score.musicxml_path)
    if not xml_path.is_file():
        raise HTTPException(status_code=410, detail="MusicXML-ul editabil nu mai există. Reimportă partitura.")
    voice_order = list(record.score.voice_order or record.score.part_names.keys())
    try:
        part_index = voice_order.index(target_event.voice)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Nu pot mapa vocea notei la portativul original.") from exc

    candidate_path = record.directory / ("manual-correction-" + uuid.uuid4().hex + ".musicxml")
    try:
        parsed = m21converter.parse(str(xml_path), forceSource=True)
        if part_index >= len(parsed.parts):
            raise ValueError("Partitura importată are mai puține portative decât maparea vocilor.")
        part = parsed.parts[part_index]
        candidates = list(part.flatten().notesAndRests)
        changed = False
        for obj in candidates:
            try:
                start_q = max(0.0, float(obj.offset))
                duration_q = max(0.0, float(obj.duration.quarterLength))
                measure = getattr(obj, "measureNumber", None)
            except Exception:
                continue
            if abs(start_q - float(target_event.start_q)) > 1e-4:
                continue
            if abs(duration_q - float(target_event.duration_q)) > 1e-4:
                continue
            if target_event.measure is not None and str(measure) != str(target_event.measure):
                continue
            if isinstance(obj, m21note.Note):
                try:
                    current_pitch = int(obj.pitch.midi)
                except Exception:
                    continue
                if current_pitch != old_pitch:
                    continue
                obj.pitch = m21pitch.Pitch(midi=new_pitch)
                changed = True
                break
            if isinstance(obj, m21chord.Chord):
                pitches = list(obj.pitches)
                matching = next((i for i, pitch_obj in enumerate(pitches)
                                 if int(pitch_obj.midi) == old_pitch), None)
                if matching is None:
                    continue
                pitches[matching] = m21pitch.Pitch(midi=new_pitch)
                obj.pitches = pitches
                changed = True
                break
        if not changed:
            raise ValueError(
                "Nota nu a putut fi găsită fără ambiguitate în MusicXML. "
                "Reimportă fișierul sau corectează partitura în MuseScore."
            )
        parsed.write("musicxml", fp=str(candidate_path))
        # Re-parse before touching the active file. A bad export must not corrupt
        # the current score or its already-generated rehearsal assets.
        corrected_score = parse_score(candidate_path, record.directory)
        corrected_score.musicxml_path = str(xml_path)
        corrected_score.title = record.score.title
        history_path = record.directory / "manual-corrections.json"
        try:
            history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.is_file() else []
            if not isinstance(history, list):
                history = []
        except (OSError, json.JSONDecodeError):
            history = []
        correction = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "voice": target_event.voice,
            "measure": str(target_event.measure) if target_event.measure is not None else None,
            "start_q": round(float(target_event.start_q), 4),
            "duration_q": round(float(target_event.duration_q), 4),
            "old_pitch": old_pitch,
            "old_note": f"{m21pitch.Pitch(midi=old_pitch).nameWithOctave}",
            "new_pitch": new_pitch,
            "new_note": f"{m21pitch.Pitch(midi=new_pitch).nameWithOctave}",
        }
        if len(history) >= 1000:
            history = history[-999:]
        history.append(correction)
        history_temp = record.directory / ("manual-corrections-" + uuid.uuid4().hex + ".tmp")
        history_temp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        history_temp.replace(history_path)
        candidate_path.replace(xml_path)
        record.score = corrected_score
        try:
            _save_score_record(score_id, record)
        except OSError:
            # The active session is corrected; the MusicXML itself is saved locally.
            pass
    except HTTPException:
        raise
    except (ValueError, OSError, m21converter.ConverterException) as exc:
        raise HTTPException(status_code=422, detail="Nu am putut salva corectura: " + str(exc)[:500]) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Corectarea notei a eșuat: " + str(exc)[:500]) from exc
    finally:
        candidate_path.unlink(missing_ok=True)

    # A pitch edit invalidates the import-time OMR report. The current-score
    # preflight remains authoritative until the OMR is run again.
    return {"saved": True, "correction": correction, "score_id": score_id, **record.score.public_dict()}


@app.get("/api/scores/{score_id}/analysis")
def score_analysis(score_id: str) -> dict:
    """Run non-destructive score preflight checks and merge optional OMR confidence findings."""
    record = get_score(score_id)
    analysis = analyze_score(record.score)
    corrections_path = record.directory / "manual-corrections.json"
    try:
        corrections = json.loads(corrections_path.read_text(encoding="utf-8")) if corrections_path.is_file() else []
        if not isinstance(corrections, list):
            corrections = []
    except (OSError, json.JSONDecodeError):
        corrections = []
    analysis["manual_corrections"] = corrections[-100:]
    omr_json = record.directory / "omr-work" / "omr-analysis" / "analiza.json"
    analysis["omr_report_stale"] = bool(corrections and omr_json.is_file())
    if omr_json.is_file() and not corrections:
        try:
            raw = json.loads(omr_json.read_text(encoding="utf-8"))
            analysis["omr_music_intelligence"] = {
                "key": raw.get("key"),
                "keys": raw.get("keys", []),
                "meters": raw.get("meters", []),
                "measures": raw.get("measures"),
                "voices": raw.get("voices", []),
                "chord_functions": raw.get("chord_functions", []),
                "cadences": raw.get("cadences", []),
                "form": raw.get("form", []),
                "structure": raw.get("structure", {}),
                "texture": raw.get("texture", {}),
                "notes_total": raw.get("notes_total", 0),
            }
            for item in raw.get("doubtful", [])[:500]:
                level = str(item.get("level", "")).lower()
                measure = item.get("m")
                analysis["issues"].append({
                    "severity": "warning" if level == "rosu" else "info",
                    "code": "omr-low-confidence",
                    "message": "OMR-SATB marchează nota " + str(item.get("note", "?")) +
                        " ca " + ("nesigură" if level == "rosu" else "de verificat") +
                        " la timpul " + str(item.get("beat", "?")) + ".",
                    "recommendation": "Compară nota cu fotografia/scanul original înainte de distribuirea materialelor.",
                    "voice": item.get("voice"),
                    "measure": str(measure) if measure is not None else None,
                    "start_q": None,
                })
            for item in raw.get("suspicions", [])[:500]:
                measure = item.get("m")
                analysis["issues"].append({
                    "severity": "warning" if float(item.get("weight", 0) or 0) >= 3 else "info",
                    "code": "omr-musical-suspicion",
                    "message": "Citire de verificat: " + str(item.get("note", "?")) + " — " + str(item.get("why", "")),
                    "recommendation": "Verifică această notă în scan și corectează OMR/MusicXML dacă este necesar.",
                    "voice": item.get("voice"),
                    "measure": str(measure) if measure is not None else None,
                    "start_q": None,
                })
            for level in ("error", "warning", "info"):
                analysis["summary"][level] = sum(1 for issue in analysis["issues"] if issue["severity"] == level)
            analysis["omr_source"] = True
            analysis["disclaimer"] += " Sunt incluse și suspiciunile Music Intelligence din OMR-SATB."
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            analysis["omr_source"] = False
    return analysis


@app.post("/api/scores/{score_id}/intonation-check")
async def check_voice_intonation(
    score_id: str,
    file: UploadFile = File(...),
    voice: str = Query(...),
    reference_bpm: float = Query(100.0, ge=30, le=240),
    offset_ms: int = Query(0, ge=-10000, le=10000),
    tolerance_cents: float = Query(40.0, ge=10, le=100),
) -> dict:
    """Compare a single isolated singer's PCM WAV recording to one score part."""
    record = get_score(score_id)
    if voice not in record.score.part_names:
        raise HTTPException(status_code=422, detail="Vocea selectată nu există în partitură.")
    filename = Path(file.filename or "").name
    if Path(filename).suffix.lower() != ".wav":
        await file.close()
        raise HTTPException(status_code=415, detail="Folosește o înregistrare WAV PCM necomprimată.")
    try:
        audio_bytes = await file.read(MAX_RECORDING_BYTES + 1)
    finally:
        await file.close()
    if len(audio_bytes) > MAX_RECORDING_BYTES:
        raise HTTPException(status_code=413, detail="Înregistrarea depășește limita de 32 MB.")
    try:
        return await run_in_threadpool(
            analyze_voice_recording, audio_bytes, record.score, voice,
            reference_bpm, offset_ms, tolerance_cents,
        )
    except IntonationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/scores/{score_id}/omr-report")
def get_omr_report(score_id: str) -> FileResponse:
    record = get_score(score_id)
    corrections_path = record.directory / "manual-corrections.json"
    if corrections_path.is_file():
        try:
            corrections = json.loads(corrections_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            corrections = []
        if isinstance(corrections, list) and corrections:
            raise HTTPException(
                status_code=409,
                detail="Raportul OMR este dinaintea corecturilor manuale. Rulează din nou analiza OMR pentru un raport actualizat.",
            )
    report = record.directory / "omr-work" / "omr-analysis" / "analiza.html"
    if not report.is_file():
        raise HTTPException(status_code=404, detail="Raportul Music Intelligence nu există pentru această partitură.")
    return FileResponse(report, media_type="text/html; charset=utf-8", filename="analiza-omr.html")


@app.get("/api/scores/{score_id}/annotations")
def get_score_annotations(score_id: str) -> dict:
    record = get_score(score_id)
    path = record.directory / "annotations.json"
    if not path.is_file():
        return {"annotations": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {"annotations": payload if isinstance(payload, list) else []}
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Nu am putut citi adnotările locale.") from exc


@app.put("/api/scores/{score_id}/annotations")
def save_score_annotations(score_id: str, payload: list[dict[str, Any]] = Body(...)) -> dict:
    record = get_score(score_id)
    try:
        annotations = validate_annotations(payload, record.score)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    path = record.directory / "annotations.json"
    temporary = record.directory / ("annotations-" + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(annotations, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Nu am putut salva adnotările pe acest dispozitiv.") from exc
    return {"saved": True, "count": len(annotations), "annotations": annotations}


class RehearsalPackRequest(BaseModel):
    reference_bpm: float = Field(default=100, ge=30, le=240)
    resolution: Literal["1080p", "4k"] = "1080p"
    include_videos: bool = False
    volume_by_voice: dict[str, int] = Field(default_factory=dict)


@app.post("/api/scores/{score_id}/rehearsal-pack")
async def create_rehearsal_pack(score_id: str, request: RehearsalPackRequest) -> FileResponse:
    record = get_score(score_id)
    volumes = _validate_voice_volumes(request.volume_by_voice, set(record.score.part_names))
    try:
        output = await run_in_threadpool(
            _build_rehearsal_pack, record, request.reference_bpm,
            request.resolution, request.include_videos, volumes,
        )
    except (ValueError, VideoExportError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Pachetul de repetiție nu a putut fi generat: " + str(exc)[:500],
        ) from exc
    return FileResponse(output, media_type="application/zip", filename=output.name)


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


def _write_audio_pack_zip(
    score: ScoreData,
    archive_path: Path,
    reference_bpm: float,
    audio_format: str,
    safe_title: str,
    stems_dir: Path,
    volume_by_voice: dict[str, int] | None = None,
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
        mix_wav = mix_vocal_audio(
            score, stems_dir, voices, reference_bpm, volume_by_voice=volume_by_voice
        )
        if mix_wav is None:
            render_kwargs = {"volume_by_voice": volume_by_voice} if volume_by_voice else {}
            mix_wav = render_preview_wav(score, reference_bpm, voices, 0, **render_kwargs)
        mix_data = mix_wav if audio_format == "wav" else _encode_mp3(mix_wav)
        archive.writestr(f"{safe_title}-SATB-fullmix.{audio_format}", mix_data)
        del mix_wav, mix_data

        for voice_name in voices:
            voice_wav = mix_vocal_audio(
                score, stems_dir, [voice_name], reference_bpm, volume_by_voice=volume_by_voice
            )
            if voice_wav is None:
                render_kwargs = {"volume_by_voice": volume_by_voice} if volume_by_voice else {}
                voice_wav = render_preview_wav(score, reference_bpm, [voice_name], 0, **render_kwargs)
            voice_data = voice_wav if audio_format == "wav" else _encode_mp3(voice_wav)
            voice_slug = "".join(
                ch for ch in voice_name.lower()
                if ch.isascii() and (ch.isalnum() or ch in "-_")
            ) or "part"
            archive.writestr(f"{safe_title}-{voice_slug}.{audio_format}", voice_data)
            manifest.append(f"{voice_name}: {safe_title}-{voice_slug}.{audio_format}")
            del voice_wav, voice_data

        archive.writestr("README.txt", "\n".join(manifest) + "\n")


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


@app.get("/api/scores/{score_id}/audio")
async def download_audio(
    score_id: str,
    audio_format: Literal["wav", "mp3"] = Query(default="wav", alias="format"),
    reference_bpm: float = Query(100.0, ge=30, le=240),
    voice: str = Query("all"),
    mode: Literal["isolated", "accompaniment"] = Query("isolated"),
    volumes: str = Query("{}"),
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
        volume_payload = json.loads(volumes)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="Setările mixerului nu sunt JSON valid.") from exc
    validated_volumes = _validate_voice_volumes(volume_payload, set(available))

    try:
        vocal_wav = await run_in_threadpool(
            mix_vocal_audio, record.score, record.directory / "vocal-stems",
            selected_voices, reference_bpm, focus_voice,
            volume_by_voice=validated_volumes,
        )
        if vocal_wav is not None:
            wav_data = vocal_wav
        else:
            render_kwargs = {"volume_by_voice": validated_volumes} if validated_volumes else {}
            wav_data = await run_in_threadpool(
                render_preview_wav, record.score, reference_bpm, selected_voices, 0, focus_voice,
                **render_kwargs,
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
    volumes: str = Query("{}"),
) -> FileResponse:
    """Build a local rehearsal ZIP with the full SATB mix and one isolated file per part."""
    record = get_score(score_id)
    voices = [
        part for part in (record.score.voice_order or list(record.score.part_names))
        if part in record.score.part_names
    ]
    if not voices:
        raise HTTPException(status_code=422, detail="Partitura nu conține voci exportabile.")
    try:
        volume_payload = json.loads(volumes)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="Setările mixerului nu sunt JSON valid.") from exc
    validated_volumes = _validate_voice_volumes(volume_payload, set(voices))

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
            validated_volumes,
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
                      voices: str = Query("all"), volumes: str = Query("{}")) -> Response:
    record = get_score(score_id)
    available = set(record.score.part_names)
    selected = list(available) if voices == "all" else [v for v in voices.split(",") if v]
    unknown = sorted(set(selected) - available)
    if unknown:
        raise HTTPException(status_code=422, detail="Voce necunoscută: " + ", ".join(unknown))
    if not selected:
        raise HTTPException(status_code=422, detail="Selectează cel puțin o voce pentru preview.")
    try:
        volume_payload = json.loads(volumes)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="Setările mixerului nu sunt JSON valid.") from exc
    validated_volumes = _validate_voice_volumes(volume_payload, available)
    try:
        # Timing correction belongs to the visual cursor, not the audio track.
        # Keep audio events anchored at t=0 so the frontend can shift the score against it.
        audio = await run_in_threadpool(
            mix_vocal_audio, record.score, record.directory / "vocal-stems",
            selected, reference_bpm, volume_by_voice=validated_volumes,
        )
        if audio is None:
            render_kwargs = {"volume_by_voice": validated_volumes} if validated_volumes else {}
            audio = await run_in_threadpool(
                render_preview_wav, record.score, reference_bpm, selected, 0, **render_kwargs
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(content=audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})

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
