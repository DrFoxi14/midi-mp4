"""Local-only FastAPI application for MIDI-MP4."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import html
import json, os, re, shutil, uuid, zipfile
from fastapi import BackgroundTasks, Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from typing import Any, Literal
from app.core import ScoreData, make_midi_bytes, parse_score, render_preview_wav, find_musescore, piano_backend_info, set_runtime_soundfont
from app.video import VideoExportError, render_score_video, video_dependencies
from app.conductor import analyze_score, measure_ranges, validate_annotations
from app.omr import omr_status, run_omr_to_musicxml

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
        try:
            annotations = json.loads(annotations_path.read_text(encoding="utf-8")) if annotations_path.is_file() else []
        except (OSError, json.JSONDecodeError):
            annotations = []
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
                archive.writestr(
                    wav_name,
                    render_preview_wav(score, reference_bpm, selected, volume_by_voice=volume_by_voice),
                )
                manifest["files"].append(wav_name)
            for voice in voices:
                slug = "".join(ch for ch in voice.lower().replace(" ", "-") if ch.isalnum() or ch in "-_") or "part"
                key = "".join(ch for ch in voice if ch.isalnum() or ch in "-_") or "part"
                title_text = html.escape(score.title)
                voice_text = html.escape(voice)
                page = f"""<!doctype html>
<html lang="ro"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title_text} — {voice_text}</title>
<style>body{{font:16px system-ui;max-width:760px;margin:36px auto;padding:0 16px;background:#11141a;color:#f2f4f7}}a{{color:#88e0b0}}audio{{width:100%}}.card{{padding:18px;border:1px solid #363b47;border-radius:12px;margin:16px 0}}small{{color:#adb4c2}}</style>
<h1>{title_text}</h1><p>Material de repetiție · <strong>{voice_text}</strong></p>
<section class="card"><h2>Ascultă vocea</h2><audio controls preload="metadata" src="../audio/{key}.wav"></audio>
<p><a href="../audio/{key}.wav">Descarcă audio WAV</a></p></section>
<section class="card"><h2>Partitură și fișiere</h2><p><a href="../score.musicxml">Deschide partitura MusicXML</a></p>
<p><a href="../midi/{key}.mid">Descarcă MIDI {voice_text}</a></p>
<small>Tempo: {float(reference_bpm):.0f} BPM. Deschide indexul offline după dezarhivare.</small></section></html>"""
                page_name = f"choir/{slug}.html"
                archive.writestr(page_name, page)
                manifest["files"].append(page_name)
            archive.writestr("analysis/preflight.json", json.dumps(analysis, ensure_ascii=False, indent=2))
            archive.writestr("notes/annotations.json", json.dumps(annotations, ensure_ascii=False, indent=2))
            manifest["files"].extend(["analysis/preflight.json", "notes/annotations.json"])
            if include_videos:
                for voice in ["all", *voices]:
                    key = "SATB" if voice == "all" else "".join(ch.lower() for ch in voice if ch.isalnum() or ch in "-_") or "part"
                    video_path = build_dir / f"{key}.mp4"
                    render_score_video(
                        score, score_path, video_path, reference_bpm=reference_bpm,
                        resolution=resolution, voice=voice, audio_mode="accompaniment",
                        volume_by_voice=volume_by_voice,
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

@app.get("/api/scores/{score_id}")
def score_info(score_id: str) -> dict:
    record = get_score(score_id)
    return {"score_id": score_id, "source_name": record.original_name, **record.score.public_dict()}

@app.get("/api/scores/{score_id}/analysis")
def score_analysis(score_id: str) -> dict:
    """Run non-destructive score preflight checks and merge optional OMR confidence findings."""
    record = get_score(score_id)
    analysis = analyze_score(record.score)
    omr_json = record.directory / "omr-work" / "omr-analysis" / "analiza.json"
    if omr_json.is_file():
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
            render_preview_wav, record.score, reference_bpm, selected, 0,
            volume_by_voice=validated_volumes,
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
