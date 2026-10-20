"""Local-only FastAPI application for MIDI-MP4."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import os, shutil, uuid
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from app.core import ScoreData, make_midi_bytes, parse_score, render_preview_wav, find_musescore

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DATA_ROOT = Path(os.environ.get("MIDI_MP4_DATA_DIR", str(Path.home() / ".local" / "share" / "midi-mp4"))).expanduser()
DATA_ROOT.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_EXTENSIONS = {".musicxml", ".xml", ".mxl", ".mscz"}

@dataclass
class ScoreRecord:
    score: ScoreData
    directory: Path
    original_name: str

scores: dict[str, ScoreRecord] = {}
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
    return {
        "status": "ok",
        "mode": "local-only",
        "musescore_cli": find_musescore() is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "osmd_installed": any((ROOT / "node_modules" / "opensheetmusicdisplay" / "build" / f).exists()
                              for f in ("opensheetmusicdisplay.min.js", "opensheetmusicdisplay.js")),
        "video_export": "not-yet-implemented",
    }

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
    return FileResponse(path, media_type="application/vnd.recordare.musicxml+xml",
                        filename=f"{record.score.title}.musicxml")

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
                      voices: str = Query("all"), offset_ms: int = Query(0, ge=-5000, le=5000)) -> Response:
    record = get_score(score_id)
    available = set(record.score.part_names)
    selected = list(available) if voices == "all" else [v for v in voices.split(",") if v]
    unknown = sorted(set(selected) - available)
    if unknown:
        raise HTTPException(status_code=422, detail="Voce necunoscută: " + ", ".join(unknown))
    try:
        audio = await run_in_threadpool(render_preview_wav, record.score, reference_bpm, selected, offset_ms)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(content=audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})

@app.delete("/api/scores/{score_id}")
def delete_score(score_id: str) -> dict:
    record = get_score(score_id)
    scores.pop(score_id, None)
    shutil.rmtree(record.directory, ignore_errors=True)
    return {"deleted": True, "score_id": score_id}

app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8765, reload=False)
