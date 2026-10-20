"""Score ingestion, SATB mapping, tempo math and MIDI generation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any

import mido
from music21 import chord, converter, note, stream, tempo


SATB = ("Soprano", "Alto", "Tenor", "Bass")

# Runtime UI selection takes precedence over an optional shell environment setting.
_runtime_soundfont_path: str | None = None


def set_runtime_soundfont(path: str | None) -> None:
    """Select or clear the SoundFont for this local application process."""
    global _runtime_soundfont_path
    _runtime_soundfont_path = str(Path(path).expanduser()) if path else None


def configured_soundfont_path() -> str:
    if _runtime_soundfont_path is not None:
        return _runtime_soundfont_path
    return os.environ.get("MIDI_MP4_SOUNDFONT", "").strip()
VOICE_TERMS = {
    "Soprano": ("soprano", "sop", "s."),
    "Alto": ("alto", "contralto", "alt", "a."),
    "Tenor": ("tenor", "ten", "t."),
    "Bass": ("bass", "basso", "baritone", "b."),
}


@dataclass
class NoteEvent:
    voice: str
    part_name: str
    pitch: int | None
    start_q: float
    duration_q: float
    is_rest: bool = False
    tie: str | None = None
    measure: int | None = None


@dataclass
class TempoEvent:
    start_q: float
    bpm: float


@dataclass
class ScoreData:
    title: str
    part_names: dict[str, str]
    notes: list[NoteEvent]
    tempos: list[TempoEvent]
    total_quarters: float
    musicxml_path: str
    voice_order: list[str] | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "part_names": self.part_names,
            "voice_order": self.voice_order or list(self.part_names.keys()),
            "notes": [asdict(n) for n in self.notes],
            "tempos": [asdict(t) for t in self.tempos],
            "total_quarters": self.total_quarters,
            "note_count": sum(1 for n in self.notes if not n.is_rest),
            "voices_found": sorted({n.voice for n in self.notes}),
        }


def find_musescore() -> str | None:
    candidates = [
        shutil.which("mscore"),
        shutil.which("musescore"),
        shutil.which("MuseScore4"),
        "/Applications/MuseScore 4.app/Contents/MacOS/mscore",
        "/Applications/MuseScore 4.app/Contents/MacOS/MuseScore4",
    ]
    return next((p for p in candidates if p and Path(p).exists()), None)


def canonical_voice(name: str) -> str | None:
    low = re.sub(r"[^a-z.]", "", name.lower())
    # Match longer tokens first to avoid e.g. baritone being read as bass only
    for voice, terms in VOICE_TERMS.items():
        if any(term.replace(" ", "") in low for term in terms):
            return voice
    return None


def _prepare_musicxml(source: Path, work_dir: Path) -> Path:
    """Convert .mscz locally with MuseScore; preserve MusicXML as the browser source."""
    suffix = source.suffix.lower()
    if suffix == ".mscz":
        binary = find_musescore()
        if not binary:
            raise ValueError(
                "Importul .mscz necesită MuseScore 4 instalat local. "
                "Instalează-l sau exportă partitura ca MusicXML (.musicxml)."
            )
        target = work_dir / f"{source.stem}-converted.musicxml"
        try:
            completed = subprocess.run(
                [binary, "-o", str(target), str(source)],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Conversia MuseScore a depășit 120 de secunde.") from exc
        # MuseScore on macOS can emit crash-handler warnings or exit non-zero
        # after writing the export. Judge conversion by the actual output file;
        # parse_score() will validate the MusicXML contents immediately afterward.
        if not target.is_file() or target.stat().st_size == 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-1200:]
            status = f" (cod de ieșire {completed.returncode})"
            raise ValueError(
                "MuseScore nu a produs un fișier MusicXML utilizabil" + status + ". "
                + (detail or "Verifică versiunea MuseScore și încearcă exportul MusicXML din aplicația grafică.")
            )
        return target
    if suffix not in {".musicxml", ".xml", ".mxl"}:
        raise ValueError("Format nesuportat. Folosește .musicxml, .xml, .mxl sau .mscz.")
    return source


def _tempo_map(parsed: stream.Score) -> list[TempoEvent]:
    marks: list[TempoEvent] = []
    try:
        flat = parsed.flatten()
        for item in flat.getElementsByClass(tempo.MetronomeMark):
            bpm = item.number
            if bpm is None:
                continue
            value = float(bpm)
            if 20 <= value <= 400:
                marks.append(TempoEvent(max(0.0, float(item.offset)), value))
    except Exception:
        marks = []
    marks.sort(key=lambda t: t.start_q)
    # Last tempo marking at a given offset takes precedence.
    unique: dict[float, TempoEvent] = {}
    for item in marks:
        unique[round(item.start_q, 6)] = item
    marks = sorted(unique.values(), key=lambda t: t.start_q)
    if not marks or marks[0].start_q > 0:
        marks.insert(0, TempoEvent(0.0, marks[0].bpm if marks else 100.0))
    return marks


def parse_score(source: Path, work_dir: Path) -> ScoreData:
    if not source.exists() or source.stat().st_size == 0:
        raise ValueError("Fișierul este gol sau nu există.")
    if source.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("Fișierul depășește limita locală de 50 MB.")
    work_dir.mkdir(parents=True, exist_ok=True)
    xml_path = _prepare_musicxml(source, work_dir)
    try:
        parsed = converter.parse(str(xml_path), forceSource=True)
    except Exception as exc:
        raise ValueError(f"Partitura nu poate fi citită ca MusicXML: {exc}") from exc
    if not isinstance(parsed, stream.Score):
        parsed = stream.Score(parsed)

    raw_parts = list(parsed.parts)
    if not raw_parts:
        raise ValueError("Partitura nu conține părți muzicale.")
    names: dict[str, str] = {}
    assigned: dict[int, str] = {}
    used: set[str] = set()
    for idx, part in enumerate(raw_parts):
        name = (getattr(part, "partName", None) or str(part.id) or f"Part {idx + 1}").strip()
        voice = canonical_voice(name)
        if voice and voice not in used:
            assigned[idx] = voice
            used.add(voice)
            names[voice] = name
    for idx, part in enumerate(raw_parts):
        if idx in assigned:
            continue
        available = [v for v in SATB if v not in used]
        # Standard SATB exports commonly order parts S, A, T, B.
        if available and len(raw_parts) >= 3:
            voice = available[0]
            assigned[idx] = voice
            used.add(voice)
            names[voice] = (getattr(part, "partName", None) or str(part.id) or voice).strip()
        else:
            voice = f"Part {idx + 1}"
            assigned[idx] = voice
            names[voice] = (getattr(part, "partName", None) or str(part.id) or voice).strip()

    events: list[NoteEvent] = []
    total_q = 0.0
    for idx, part in enumerate(raw_parts):
        voice = assigned[idx]
        try:
            flattened = part.flatten()
            objects = flattened.notesAndRests
        except Exception as exc:
            raise ValueError(f"Nu pot citi notele din partea {names.get(voice, voice)}: {exc}") from exc
        for obj in objects:
            try:
                start_q = max(0.0, float(obj.offset))
                duration_q = max(0.0, float(obj.duration.quarterLength))
            except Exception:
                continue
            total_q = max(total_q, start_q + duration_q)
            if duration_q <= 0:
                continue
            measure_number = getattr(obj, "measureNumber", None)
            if isinstance(obj, note.Rest):
                events.append(NoteEvent(voice, names[voice], None, start_q, duration_q,
                                        True, None, measure_number))
                continue
            pitches = obj.pitches if isinstance(obj, chord.Chord) else [obj.pitch]
            tie_obj = getattr(obj, "tie", None)
            tie_type = getattr(tie_obj, "type", None)
            for pitch_obj in pitches:
                try:
                    pitch_num = int(pitch_obj.midi)
                except Exception:
                    continue
                events.append(NoteEvent(voice, names[voice], pitch_num, start_q,
                                        duration_q, False, tie_type, measure_number))

    events.sort(key=lambda n: (n.start_q, n.voice, n.pitch if n.pitch is not None else -1))
    tempos = _tempo_map(parsed)
    title = ""
    try:
        title = str(parsed.metadata.title or "").strip()
    except Exception:
        pass
    if not title:
        title = source.stem
    # Export a normalized MusicXML file for the local browser score renderer.
    normalized_xml = work_dir / f"{source.stem}-display.musicxml"
    try:
        parsed.write("musicxml", fp=str(normalized_xml))
    except Exception as exc:
        # An original XML/MXL file may still be directly readable by OSMD.
        if xml_path.suffix.lower() not in {".xml", ".musicxml", ".mxl"}:
            raise ValueError(f"Partitura a fost citită, dar nu s-a putut pregăti afișarea: {exc}") from exc
        normalized_xml = xml_path
    voice_order = [assigned[i] for i in range(len(raw_parts))]
    return ScoreData(title, names, events, tempos, total_q, str(normalized_xml), voice_order)


def scaled_tempos(tempos: list[TempoEvent], reference_bpm: float) -> list[TempoEvent]:
    if not 30 <= float(reference_bpm) <= 240:
        raise ValueError("Tempo-ul de referință trebuie să fie între 30 și 240 BPM.")
    source_bpm = next((t.bpm for t in tempos if t.start_q <= 0), 100.0)
    factor = float(reference_bpm) / source_bpm
    return [TempoEvent(t.start_q, t.bpm * factor) for t in tempos]


def seconds_at_q(position_q: float, tempos: list[TempoEvent]) -> float:
    """Convert a quarter-note position to seconds using a piecewise tempo map."""
    if position_q <= 0:
        return 0.0
    ordered = sorted(tempos, key=lambda t: t.start_q)
    if not ordered:
        ordered = [TempoEvent(0.0, 100.0)]
    if ordered[0].start_q > 0:
        ordered.insert(0, TempoEvent(0.0, ordered[0].bpm))
    seconds = 0.0
    for idx, item in enumerate(ordered):
        start = max(0.0, item.start_q)
        end = ordered[idx + 1].start_q if idx + 1 < len(ordered) else position_q
        if position_q <= start:
            break
        segment_end = min(position_q, end)
        if segment_end > start:
            seconds += (segment_end - start) * 60.0 / item.bpm
        if position_q <= end:
            break
    return seconds


def _collapse_ties(events: list[NoteEvent]) -> list[NoteEvent]:
    """Merge adjacent tied note events of the same pitch and voice."""
    ordered = sorted((e for e in events if not e.is_rest and e.pitch is not None),
                     key=lambda e: (e.voice, e.pitch, e.start_q))
    out: list[NoteEvent] = []
    open_ties: dict[tuple[str, int], NoteEvent] = {}
    for event in ordered:
        key = (event.voice, int(event.pitch))
        previous = open_ties.get(key)
        if previous and abs((previous.start_q + previous.duration_q) - event.start_q) < 1e-5 and event.tie in {"stop", "continue"}:
            previous.duration_q += event.duration_q
            previous.tie = event.tie
            if event.tie == "stop":
                open_ties.pop(key, None)
            else:
                open_ties[key] = previous
            continue
        copy = NoteEvent(**asdict(event))
        out.append(copy)
        if event.tie in {"start", "continue"}:
            open_ties[key] = copy
        elif event.tie == "stop":
            open_ties.pop(key, None)
    return sorted(out, key=lambda e: (e.start_q, e.voice, e.pitch or 0))


def make_midi_bytes(score: ScoreData, reference_bpm: float = 100.0,
                    voice: str = "all",
                    velocity_by_voice: dict[str, int] | None = None) -> bytes:
    tempo_events = scaled_tempos(score.tempos, reference_bpm)
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    tempo_track = mido.MidiTrack()
    midi.tracks.append(tempo_track)
    tempo_track.append(mido.MetaMessage("track_name", name="Tempo map", time=0))
    last_tick = 0
    for item in tempo_events:
        tick = round(item.start_q * midi.ticks_per_beat)
        tempo_track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(item.bpm),
                                            time=max(0, tick - last_tick)))
        last_tick = tick
    tempo_track.append(mido.MetaMessage("end_of_track", time=0))

    known_voices = list(SATB) + sorted({n.voice for n in score.notes if n.voice not in SATB})
    chosen = [voice] if voice != "all" else known_voices
    for voice_name in chosen:
        track = mido.MidiTrack()
        midi.tracks.append(track)
        track.append(mido.MetaMessage("track_name", name=voice_name, time=0))
        track.append(mido.Message("program_change", channel=len(midi.tracks) - 2,
                                  program=0, time=0))
        note_events = [e for e in _collapse_ties(score.notes)
                       if e.voice == voice_name and not e.is_rest and e.pitch is not None]
        timeline: list[tuple[int, int, int, int]] = []
        channel = (len(midi.tracks) - 2) % 16
        for ev in note_events:
            start_tick = max(0, round(ev.start_q * midi.ticks_per_beat))
            end_tick = max(start_tick + 1, round((ev.start_q + ev.duration_q) * midi.ticks_per_beat))
            timeline.append((start_tick, 1, int(ev.pitch), channel))
            timeline.append((end_tick, 0, int(ev.pitch), channel))
        timeline.sort(key=lambda item: (item[0], item[1]))  # note-off before note-on at same tick
        last_tick = 0
        for tick, is_on, pitch_num, ch in timeline:
            track.append(mido.Message("note_on" if is_on else "note_off",
                                      channel=ch, note=pitch_num,
                                      velocity=max(1, min(127, int((velocity_by_voice or {}).get(voice_name, 82)))) if is_on else 0,
                                      time=max(0, tick - last_tick)))
            last_tick = tick
        track.append(mido.MetaMessage("end_of_track", time=0))
    bio = BytesIO()
    midi.save(file=bio)
    return bio.getvalue()



def piano_backend_info() -> dict[str, Any]:
    """Report whether an optional locally configured sampled piano is usable."""
    configured = configured_soundfont_path()
    if not configured:
        return {
            "backend": "synthesized",
            "sample_piano_available": False,
            "message": "Nu este configurat un SoundFont local; se folosește sintetizarea offline.",
        }
    soundfont = Path(configured).expanduser()
    if not soundfont.is_file() or soundfont.suffix.lower() not in {".sf2", ".sf3"}:
        return {
            "backend": "configuration-error",
            "sample_piano_available": False,
            "message": "MIDI_MP4_SOUNDFONT trebuie să indice un fișier .sf2 sau .sf3 existent.",
        }
    configured_binary = os.environ.get("MIDI_MP4_FLUIDSYNTH", "fluidsynth").strip() or "fluidsynth"
    binary = shutil.which(configured_binary)
    if not binary and Path(configured_binary).is_file():
        binary = configured_binary
    if not binary:
        return {
            "backend": "missing-fluidsynth",
            "sample_piano_available": False,
            "message": "SoundFont configurat, dar FluidSynth nu este găsit în PATH.",
        }
    return {
        "backend": "soundfont",
        "sample_piano_available": True,
        "soundfont_name": soundfont.name,
        "message": "Preview cu pian bazat pe un SoundFont local prin FluidSynth.",
    }


def _render_soundfont_wav(
    score: ScoreData,
    reference_bpm: float,
    voices: set[str],
    offset_ms: int,
    focus_voice: str | None,
) -> bytes:
    """Render selected tracks through a locally installed FluidSynth and local SF2/SF3 file."""
    import wave

    status = piano_backend_info()
    if status["backend"] != "soundfont":
        raise ValueError(status["message"])
    soundfont = Path(configured_soundfont_path()).expanduser()
    configured_binary = os.environ.get("MIDI_MP4_FLUIDSYNTH", "fluidsynth").strip() or "fluidsynth"
    binary = shutil.which(configured_binary) or configured_binary
    selected_order = [v for v in (score.voice_order or list(score.part_names)) if v in voices]
    selected_names = {v: label for v, label in score.part_names.items() if v in voices}
    filtered = ScoreData(
        score.title,
        selected_names,
        [event for event in score.notes if event.voice in voices],
        score.tempos,
        score.total_quarters,
        score.musicxml_path,
        selected_order,
    )
    velocities = None
    if focus_voice and focus_voice in voices:
        velocities = {v: (104 if v == focus_voice else 56) for v in voices}
    midi_data = make_midi_bytes(filtered, reference_bpm, "all", velocity_by_voice=velocities)
    rate = 22050
    duration = seconds_at_q(score.total_quarters, scaled_tempos(score.tempos, reference_bpm))
    if duration > 20 * 60:
        raise ValueError("Preview-ul local este limitat la 20 de minute.")
    with tempfile.TemporaryDirectory(prefix="midi-mp4-sf-preview-") as tmp:
        midi_path = Path(tmp) / "preview.mid"
        wav_path = Path(tmp) / "preview.wav"
        midi_path.write_bytes(midi_data)
        timeout_seconds = max(90, min(3600, int(duration * 4 + 60)))
        try:
            completed = subprocess.run(
                [binary, "-n", "-i", "-F", str(wav_path), "-r", str(rate),
                 str(soundfont), str(midi_path)],
                capture_output=True, text=True, check=False, timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("FluidSynth a depășit limita de timp la randarea preview-ului.") from exc
        except OSError as exc:
            raise ValueError("Nu am putut porni FluidSynth: " + str(exc)) from exc
        if completed.returncode != 0 or not wav_path.is_file() or wav_path.stat().st_size < 44:
            detail = (completed.stderr or completed.stdout or "FluidSynth nu a produs WAV.").strip()[-700:]
            raise ValueError("FluidSynth nu a putut reda SoundFont-ul local: " + detail)
        try:
            with wave.open(str(wav_path), "rb") as source:
                channels = source.getnchannels()
                sample_width = source.getsampwidth()
                sample_rate = source.getframerate()
                frames = source.readframes(source.getnframes())
        except (wave.Error, OSError) as exc:
            raise ValueError("FluidSynth a produs un WAV invalid: " + str(exc)) from exc
    frame_width = max(1, channels * sample_width)
    shift = int(abs(offset_ms) * sample_rate / 1000) * frame_width
    if offset_ms > 0:
        frames = (b"\\x00" * shift) + frames
    elif offset_ms < 0:
        frames = frames[min(shift, len(frames)):]
    max_bytes = int(20 * 60 * sample_rate) * frame_width
    frames = frames[:max_bytes]
    output = BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(sample_width)
        target.setframerate(sample_rate)
        target.writeframes(frames)
    return output.getvalue()


def render_preview_wav(score: ScoreData, reference_bpm: float = 100.0,
                       voices: list[str] | None = None, offset_ms: int = 0,
                       focus_voice: str | None = None) -> bytes:
    """Render with a configured local SoundFont, otherwise use the offline piano-like synth."""
    import math
    import wave
    import numpy as np

    tempo_events = scaled_tempos(score.tempos, reference_bpm)
    selected = set(SATB if voices is None else voices)
    if not selected:
        raise ValueError("Selectează cel puțin o voce pentru preview.")
    duration = seconds_at_q(score.total_quarters, tempo_events) + 1.25 + max(0, offset_ms) / 1000
    if duration > 20 * 60:
        raise ValueError("Preview-ul local este limitat la 20 de minute.")
    if os.environ.get("MIDI_MP4_SOUNDFONT", "").strip():
        return _render_soundfont_wav(score, reference_bpm, selected, offset_ms, focus_voice)
    rate = 22050
    count = int(duration * rate)
    mix = np.zeros(count, dtype=np.float32)
    for ev in _collapse_ties(score.notes):
        if ev.voice not in selected or ev.pitch is None or ev.is_rest:
            continue
        shift_seconds = offset_ms / 1000.0
        raw_start = seconds_at_q(ev.start_q, tempo_events) + shift_seconds
        raw_end = seconds_at_q(ev.start_q + ev.duration_q, tempo_events) + shift_seconds
        if raw_end <= 0 or raw_start >= duration:
            continue
        start = max(0.0, raw_start)
        end = min(duration, raw_end)
        if end <= start:
            continue
        start_idx = max(0, int(start * rate))
        end_idx = min(count, max(start_idx + 1, int(end * rate)))
        if start_idx >= count or end_idx <= start_idx:
            continue
        size = end_idx - start_idx
        # Preserve phase/envelope if a negative offset trims the attack before t=0.
        t = np.arange(size, dtype=np.float32) / rate + max(0.0, -raw_start)
        frequency = 440.0 * (2.0 ** ((int(ev.pitch) - 69) / 12.0))
        # Three decaying partials give a simple piano-like local preview without external assets.
        envelope = (1.0 - np.exp(-t * 70.0)) * np.exp(-t * (2.2 + max(0.0, 0.8 - ev.duration_q) * 2))
        sample = (np.sin(2 * math.pi * frequency * t)
                  + 0.42 * np.sin(2 * math.pi * frequency * 2.01 * t)
                  + 0.18 * np.sin(2 * math.pi * frequency * 3.9 * t))
        if focus_voice is None:
            gain = 0.10
        else:
            gain = 0.14 if ev.voice == focus_voice else 0.045
        mix[start_idx:end_idx] += (gain * envelope * sample).astype(np.float32)
    peak = float(np.max(np.abs(mix))) if count else 0.0
    if peak > 0.95:
        mix *= 0.95 / peak
    pcm = (np.clip(mix, -1, 1) * 32767).astype(np.int16).tobytes()
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return output.getvalue()
