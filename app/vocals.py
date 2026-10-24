"""Local singing-voice workflow helpers for OpenUtau/DiffSinger rendered WAV stems."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import tempfile
import wave
from typing import Any

from app.core import ScoreData, render_preview_wav, scaled_tempos, seconds_at_q

MAX_VOCAL_STEM_BYTES = 256 * 1024 * 1024


class VocalSynthesisError(ValueError):
    """A clear, user-correctable error while mixing imported vocal stems."""


def voice_slug(voice: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", voice.casefold()).strip("-_")
    return slug or "part"


def vocal_stem_path(stems_dir: Path, voice: str) -> Path:
    return stems_dir / f"{voice_slug(voice)}.wav"


def vocal_stem_metadata_path(stems_dir: Path, voice: str) -> Path:
    return stems_dir / f"{voice_slug(voice)}.json"


def validate_vocal_wav(path: Path) -> dict[str, Any]:
    """Accept uncompressed PCM WAVs that are safe to align and mix locally."""
    if not path.is_file() or path.stat().st_size < 44:
        raise ValueError("Fișierul WAV este gol sau incomplet.")
    if path.stat().st_size > MAX_VOCAL_STEM_BYTES:
        raise ValueError("Fișierul vocal WAV depășește limita de 256 MB.")
    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frame_count = source.getnframes()
            compression = source.getcomptype()
    except (wave.Error, EOFError, OSError) as exc:
        raise ValueError("Fișierul nu este un WAV PCM valid: " + str(exc)) from exc
    if compression != "NONE":
        raise ValueError("WAV-ul trebuie să conțină PCM necomprimat.")
    if channels not in {1, 2}:
        raise ValueError("WAV-ul trebuie să fie mono sau stereo.")
    if sample_width not in {2, 3, 4}:
        raise ValueError("WAV-ul trebuie să folosească PCM pe 16, 24 sau 32 de biți.")
    if not 8000 <= sample_rate <= 192000:
        raise ValueError("Rata de eșantionare trebuie să fie între 8 kHz și 192 kHz.")
    duration = frame_count / max(1, sample_rate)
    if duration <= 0.05:
        raise ValueError("WAV-ul vocal este prea scurt.")
    if duration > 20 * 60:
        raise ValueError("Durata maximă a unui stem vocal este de 20 de minute.")
    return {
        "channels": channels,
        "sample_width_bytes": sample_width,
        "sample_rate": sample_rate,
        "frames": frame_count,
        "duration_seconds": round(duration, 3),
        "bytes": path.stat().st_size,
        "format": "wav-pcm",
    }


def stem_metadata(stems_dir: Path, voice: str) -> dict[str, Any] | None:
    path = vocal_stem_path(stems_dir, voice)
    meta_path = vocal_stem_metadata_path(stems_dir, voice)
    if not path.is_file() or not meta_path.is_file():
        return None
    try:
        result = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(result, dict):
        return None
    return result


def list_vocal_stems(stems_dir: Path, voices: list[str]) -> dict[str, Any]:
    items = []
    for voice in voices:
        meta = stem_metadata(stems_dir, voice) if stems_dir.exists() else None
        items.append({
            "voice": voice,
            "uploaded": bool(meta),
            "reference_bpm": meta.get("reference_bpm") if meta else None,
            "duration_seconds": meta.get("duration_seconds") if meta else None,
            "filename": vocal_stem_path(stems_dir, voice).name if meta else None,
        })
    count = sum(1 for item in items if item["uploaded"])
    return {
        "voices": items,
        "uploaded_count": count,
        "total_voices": len(items),
        "all_uploaded": bool(items) and count == len(items),
        "message": (
            f"{count}/{len(items)} piste vocale importate. "
            "Vocile fără WAV folosesc în continuare sintetizatorul local."
        ),
    }


def vocal_engine_status() -> dict[str, Any]:
    """Detect OpenUtau if installed; never imply a neural singer is bundled."""
    candidates: list[Path] = []
    configured = os.environ.get("MIDI_MP4_OPENUTAU", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    for command in ("OpenUtau", "openutau"):
        found = shutil.which(command)
        if found:
            candidates.append(Path(found))
    home = Path.home()
    candidates.extend([
        Path("/Applications/OpenUtau.app"),
        home / "Applications" / "OpenUtau.app",
        home / ".local" / "bin" / "OpenUtau",
    ])
    openutau = next((path for path in candidates if path.exists()), None)

    singer_dirs = []
    configured_singers = os.environ.get("MIDI_MP4_OPENUTAU_SINGERS", "").strip()
    if configured_singers:
        singer_dirs.append(Path(configured_singers).expanduser())
    singer_dirs.extend([
        home / "Library" / "Application Support" / "OpenUtau" / "Singers",
        home / ".local" / "share" / "OpenUtau" / "Singers",
        home / ".config" / "OpenUtau" / "Singers",
        home / ".local" / "share" / "openutau" / "Singers",
    ])
    singer_root = next((path for path in singer_dirs if path.is_dir()), None)
    singer_count = 0
    if singer_root:
        try:
            singer_count = sum(
                1 for child in singer_root.iterdir()
                if child.is_dir() and (
                    (child / "character.txt").is_file()
                    or (child / "character.yaml").is_file()
                )
            )
        except OSError:
            singer_count = 0

    return {
        "engine": "OpenUtau + compatible singing voicebank",
        "openutau_available": openutau is not None,
        "openutau_path": str(openutau) if openutau else None,
        "singer_bank_count": singer_count,
        "singer_bank_directory": str(singer_root) if singer_root else None,
        "headless_render_available": False,
        "message": (
            "OpenUtau a fost detectat. Randarea vocală se face în interfața OpenUtau "
            "cu un singer/voicebank compatibil; MIDI-MP4 importă apoi WAV-urile rezultate."
            if openutau else
            "OpenUtau nu a fost detectat automat. Poți exporta proiectul oricum; "
            "pentru voci neuronale ai nevoie de OpenUtau și de voicebank-uri compatibile."
        ),
    }


def mix_vocal_audio(
    score: ScoreData,
    stems_dir: Path | None,
    voices: list[str],
    reference_bpm: float,
    focus_voice: str | None = None,
    volume_by_voice: dict[str, int] | None = None,
    normalize_duration: bool = False,
) -> bytes | None:
    """Use imported singing WAVs where available and synthesize only missing parts.

    Returns None when there are no imported singing stems, allowing the caller to
    retain the established offline preview path without changing its sound.
    Stems must have been rendered at the requested reference BPM to preserve sync.
    """
    if not stems_dir or not stems_dir.is_dir() or not voices:
        return None
    available: dict[str, Path] = {}
    for voice in voices:
        path = vocal_stem_path(stems_dir, voice)
        if not path.is_file():
            continue
        meta = stem_metadata(stems_dir, voice)
        if not meta:
            raise VocalSynthesisError(
                f"Stemul pentru {voice} nu are metadate valide. Reimportă fișierul WAV."
            )
        stem_bpm = float(meta.get("reference_bpm", 0))
        if abs(stem_bpm - float(reference_bpm)) > 0.01:
            raise VocalSynthesisError(
                f"Stemul pentru {voice} a fost randat la {stem_bpm:g} BPM, "
                f"dar exportul cere {float(reference_bpm):g} BPM. "
                "Redă din nou vocea în OpenUtau la tempo-ul ales și reimportă WAV-ul."
            )
        available[voice] = path
    if not available:
        return None

    # A solo uploaded WAV can bypass FFmpeg only if no processing is needed.
    def voice_level(name: str) -> float:
        try:
            value = float((volume_by_voice or {}).get(name, 100))
        except (TypeError, ValueError):
            value = 100.0
        return max(0.0, min(100.0, value)) / 100.0

    if (len(voices) == 1 and voices[0] in available and not focus_voice
            and voice_level(voices[0]) >= 0.9999 and not normalize_duration):
        return available[voices[0]].read_bytes()

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VocalSynthesisError(
            "Pentru a combina mai multe piste vocale importate este necesar FFmpeg local."
        )

    duration = seconds_at_q(score.total_quarters, scaled_tempos(score.tempos, reference_bpm)) + 1.25
    if duration <= 0 or duration > 20 * 60:
        raise VocalSynthesisError("Durata mixului vocal trebuie să fie între 0 și 20 de minute.")

    with tempfile.TemporaryDirectory(prefix="midi-mp4-vocal-mix-") as temp_name:
        temp_dir = Path(temp_name)
        input_paths: list[Path] = []
        for index, voice in enumerate(voices):
            path = available.get(voice)
            if path is not None:
                input_paths.append(path)
            else:
                synth = render_preview_wav(score, reference_bpm, [voice], 0)
                synth_path = temp_dir / f"synth-{index}.wav"
                synth_path.write_bytes(synth)
                input_paths.append(synth_path)

        filters: list[str] = []
        labels: list[str] = []
        for index, voice in enumerate(voices):
            gain = 1.0 if focus_voice and voice == focus_voice else (
                0.25 if focus_voice else 0.62 / max(1.0, len(voices) ** 0.5)
            )
            gain *= voice_level(voice)
            filters.append(
                f"[{index}:a]aresample=22050,"
                f"aformat=sample_fmts=fltp:channel_layouts=mono,"
                f"volume={gain:.4f},apad,atrim=duration={duration:.3f}[a{index}]"
            )
            labels.append(f"[a{index}]")
        filters.append(
            "".join(labels)
            + f"amix=inputs={len(voices)}:duration=longest:dropout_transition=0:normalize=0,"
              f"alimiter=limit=0.95[out]"
        )
        output_path = temp_dir / "vocal-mix.wav"
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-nostdin"]
        for input_path in input_paths:
            command += ["-i", str(input_path)]
        command += [
            "-filter_complex", ";".join(filters), "-map", "[out]",
            "-ar", "22050", "-ac", "1", "-c:a", "pcm_s16le",
            "-t", f"{duration:.3f}", str(output_path),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, check=False,
                timeout=max(120, min(1800, int(duration * 3 + 60))),
            )
        except subprocess.TimeoutExpired as exc:
            raise VocalSynthesisError("Mixarea vocală a depășit limita de timp.") from exc
        except OSError as exc:
            raise VocalSynthesisError("Nu am putut porni FFmpeg pentru mix vocal: " + str(exc)) from exc
        if completed.returncode != 0 or not output_path.is_file() or output_path.stat().st_size < 44:
            detail = (completed.stderr or completed.stdout or "FFmpeg nu a produs un WAV valid.").strip()[-800:]
            raise VocalSynthesisError("Mixarea vocilor a eșuat: " + detail)
        try:
            return output_path.read_bytes()
        except OSError as exc:
            raise VocalSynthesisError("Nu am putut citi mixul vocal generat.") from exc
