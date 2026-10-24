"""Optional local bridge to the user's OMR-SATB project for scans and rough manuscript pages.

Set OMR_SATB_DIR to the project root or its `omr/` directory. No cloud service is used.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any


def find_omr_project(application_root: Path) -> Path | None:
    configured = os.environ.get("OMR_SATB_DIR", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend([
        application_root.parent / "omr-satb" / "omr",
        application_root.parent / "OMR-SATB" / "omr",
        application_root.parent / "omr-satb",
        Path.home() / "omr-satb" / "omr",
    ])
    for candidate in candidates:
        candidate = candidate.resolve()
        omr_dir = candidate if (candidate / "piesa.py").is_file() else candidate / "omr"
        if (omr_dir / "piesa.py").is_file() and (omr_dir / "step21_musicxml.py").is_file():
            return omr_dir
    return None


def _omr_python(omr_dir: Path) -> str:
    configured = os.environ.get("OMR_SATB_PYTHON", "").strip()
    if configured:
        return configured
    candidates = [
        omr_dir / ".venv" / "bin" / "python",
        omr_dir.parent / ".venv" / "bin" / "python",
        omr_dir / ".venv" / "Scripts" / "python.exe",
        omr_dir.parent / ".venv" / "Scripts" / "python.exe",
    ]
    return str(next((p for p in candidates if p.is_file()), Path(sys.executable)))


def omr_status(application_root: Path) -> dict[str, Any]:
    omr_dir = find_omr_project(application_root)
    if not omr_dir:
        return {
            "available": False,
            "directory": None,
            "message": "OMR-SATB nu a fost găsit. Setează OMR_SATB_DIR către proiectul local pentru import PDF/foto.",
        }
    model_files = ["pixnet.pt", "symbols.pkl", "music_prior.pkl"]
    missing = [name for name in model_files if not (omr_dir / name).is_file()]
    return {
        "available": not missing,
        "directory": str(omr_dir),
        "python": _omr_python(omr_dir),
        "missing_assets": missing,
        "message": "OMR-SATB local este pregătit." if not missing else
            "OMR-SATB există, dar îi lipsesc activele: " + ", ".join(missing),
    }


def run_omr_to_musicxml(source_path: Path, work_root: Path, application_root: Path,
                          tempo_bpm: float = 100.0) -> dict[str, Any]:
    """Run piesa.py on a local PDF/photo and return an absolute MusicXML output path."""
    import json
    from PIL import Image

    if not 30 <= float(tempo_bpm) <= 240:
        raise ValueError("Tempo-ul OMR trebuie să fie între 30 și 240 BPM.")
    omr_dir = find_omr_project(application_root)
    if not omr_dir:
        raise RuntimeError(
            "Importul de scan/ciornă necesită OMR-SATB instalat local. "
            "Setează variabila OMR_SATB_DIR către folderul omr-satb/omr."
        )
    status = omr_status(application_root)
    if not status.get("available"):
        raise RuntimeError(status.get("message", "Activele modelului OMR lipsesc."))
    source_suffix = source_path.suffix.lower()
    if source_suffix not in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
        raise ValueError("Pentru recunoaștere OMR folosește PDF, PNG, JPG, TIFF, BMP sau WebP.")
    python_executable = _omr_python(omr_dir)
    if source_suffix == ".pdf":
        try:
            count = subprocess.run(
                [python_executable, "-c", "import fitz,sys; print(len(fitz.open(sys.argv[1])))", str(source_path)],
                capture_output=True, text=True, check=False, timeout=60,
            )
            if count.returncode != 0:
                raise ValueError((count.stderr or "PDF-ul nu a putut fi deschis.").strip()[-500:])
            page_count = int(count.stdout.strip())
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise ValueError("Nu pot citi numărul de pagini din PDF folosind Python-ul OMR: " + str(exc)) from exc
    else:
        page_count = 1
    if page_count < 1 or page_count > 40:
        raise ValueError("Importul OMR acceptă între 1 și 40 de pagini per lucrare.")
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="omr-import-", dir=str(work_root)) as tmp:
        run_dir = Path(tmp)
        input_pdf = run_dir / "submitted-score.pdf"
        if source_suffix == ".pdf":
            shutil.copy2(source_path, input_pdf)
        else:
            try:
                with Image.open(source_path) as image:
                    image.convert("RGB").save(input_pdf, "PDF", resolution=200.0)
            except Exception as exc:
                raise ValueError("Imaginea nu poate fi convertită într-o pagină PDF pentru OMR.") from exc
        env = dict(os.environ)
        env["OMR_LYRICS"] = "0"
        env.setdefault("OMR_CA_ORIGINALUL", "0")
        command = [python_executable, str(omr_dir / "piesa.py"), str(input_pdf), str(float(tempo_bpm)),
                   "--fara-versuri", "--fara-audio"]
        try:
            completed = subprocess.run(
                command, cwd=run_dir, env=env, text=True,
                capture_output=True, check=False, timeout=max(900, min(2400, page_count * 240)),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("OMR-SATB a depășit limita de timp. Încearcă mai puține pagini per import.") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "OMR-SATB nu a finalizat toate etapele.").strip()
            raise RuntimeError("OMR-SATB nu a produs o lucrare completă (cod " + str(completed.returncode) + "). " + detail[-1800:])
        output_dirs = sorted(run_dir.glob("piesa_*"))
        xml_candidates: list[Path] = []
        analysis_candidates: list[Path] = []
        for output_dir in output_dirs:
            xml_candidates.extend(p for p in output_dir.glob("*.musicxml") if "_ca_originalul" not in p.name)
            analysis_candidates.extend(p for p in output_dir.glob("analiza.json"))
        if not xml_candidates:
            raise RuntimeError("OMR-SATB s-a încheiat, dar nu a produs un MusicXML utilizabil.")
        xml_candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
        output_xml = work_root / "omr-converted.musicxml"
        shutil.copy2(xml_candidates[0], output_xml)
        analysis_target = work_root / "omr-analysis"
        analysis_target.mkdir(parents=True, exist_ok=True)
        analysis_json = None
        for candidate in analysis_candidates:
            try:
                json.loads(candidate.read_text(encoding="utf-8"))
                target = analysis_target / "analiza.json"
                shutil.copy2(candidate, target)
                analysis_json = str(target)
                for sibling_name in ("analiza.html", "analiza.txt", "status.json"):
                    sibling = candidate.with_name(sibling_name)
                    if sibling.is_file():
                        shutil.copy2(sibling, analysis_target / sibling_name)
                break
            except (OSError, json.JSONDecodeError):
                continue
        return {
            "musicxml_path": str(output_xml),
            "analysis_json": analysis_json,
            "omr_directory": str(omr_dir),
            "pages": page_count,
            "stdout_tail": (completed.stdout or "")[-1800:],
        }
