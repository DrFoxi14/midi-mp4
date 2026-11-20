"""Best-effort local launcher for OpenUtau project files.

This module opens a generated .ustx in the user's installed desktop application.
It does not claim to render audio or provide a headless DiffSinger backend.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Callable


def _configured_app_path() -> Path | None:
    configured = os.environ.get("MIDI_MP4_OPENUTAU_APP", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        return candidate if candidate.exists() else None

    if sys.platform == "darwin":
        candidates = [
            Path("/Applications/OpenUtau.app"),
            Path.home() / "Applications" / "OpenUtau.app",
        ]
        return next((candidate for candidate in candidates if candidate.exists()), None)
    return None


def openutau_status() -> dict:
    """Report launcher availability separately from confirmed app discovery."""
    app_path = _configured_app_path()
    if sys.platform == "darwin":
        available = bool(shutil.which("open"))
        detected = bool(app_path or shutil.which("OpenUtau"))
    elif sys.platform.startswith("win"):
        available = hasattr(os, "startfile")
        detected = bool(shutil.which("OpenUtau.exe") or shutil.which("OpenUtau"))
    else:
        available = bool(shutil.which("xdg-open"))
        detected = bool(shutil.which("OpenUtau"))
    if detected:
        message = (
            "OpenUtau a fost detectat local. MIDI-MP4 poate trimite proiectul USTX "
            "către aplicație; randarea WAV rămâne în OpenUtau."
        )
    elif available:
        message = (
            "Sistemul poate deschide fișiere USTX prin asocierea desktop, dar instalarea "
            "OpenUtau nu a putut fi confirmată. Proiectul rămâne și descărcabil."
        )
    else:
        message = (
            "Nu există un launcher desktop disponibil. Proiectele USTX pot fi "
            "descărcate și deschise manual în OpenUtau."
        )
    return {
        "available": available,
        "application_detected": detected,
        "app_path": str(app_path) if app_path else None,
        "platform": sys.platform,
        "launch_mode": "desktop-file-open",
        "headless_rendering": False,
        "message": message,
    }


def launch_project(
    project_path: Path,
    *,
    system: str | None = None,
    app_path: Path | None = None,
    which: Callable[[str], str | None] | None = None,
    popen: Callable[..., object] | None = None,
    startfile: Callable[[str], object] | None = None,
) -> dict:
    """Open an existing USTX via the desktop OS; never starts a shell command."""
    path = Path(project_path).expanduser().resolve()
    if path.suffix.casefold() != ".ustx" or not path.is_file() or path.stat().st_size == 0:
        raise ValueError("Fișierul proiectului OpenUtau trebuie să fie un USTX existent și nevid.")

    platform_name = system or sys.platform
    which_fn = which or shutil.which
    popen_fn = popen or subprocess.Popen

    if platform_name == "darwin":
        opener = which_fn("open")
        if not opener:
            return {"launched": False, "reason": "macOS nu oferă comanda «open» în PATH."}
        selected_app = app_path or _configured_app_path()
        command = [opener]
        if selected_app:
            command.extend(["-a", str(selected_app)])
        command.append(str(path))
        launch_mode = "application" if selected_app else "association"
    elif platform_name.startswith("win"):
        startfile_fn = startfile or getattr(os, "startfile", None)
        if startfile_fn is None:
            return {"launched": False, "reason": "Deschiderea fișierelor Windows nu este disponibilă."}
        try:
            startfile_fn(str(path))
        except OSError as exc:
            return {"launched": False, "reason": f"Windows nu a putut deschide proiectul: {exc}"}
        return {"launched": True, "mode": "association"}
    else:
        opener = which_fn("xdg-open")
        if not opener:
            return {"launched": False, "reason": "Nu am găsit xdg-open pentru a deschide aplicația."}
        command = [opener, str(path)]
        launch_mode = "association"

    try:
        popen_fn(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return {"launched": False, "reason": f"Nu am putut porni OpenUtau: {exc}"}
    return {
        "launched": True,
        "mode": launch_mode,
        "application": str(app_path or _configured_app_path() or ""),
    }
