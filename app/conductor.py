"""Local conductor tools: score preflight, measure ranges and annotations validation."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from typing import Any

from app.core import NoteEvent, ScoreData, seconds_at_q, scaled_tempos


VOICE_RANGES = {
    "Soprano": (60, 84),  # C4–C6, practical warning range rather than a hard rule
    "Alto": (55, 79),     # G3–G5
    "Tenor": (48, 72),    # C3–C5
    "Bass": (40, 64),     # E2–E4
}
VOICE_ORDER = {"Soprano": 0, "Alto": 1, "Tenor": 2, "Bass": 3}


def measure_ranges(score: ScoreData) -> list[dict[str, Any]]:
    """Infer usable measure start/end positions from MusicXML measure-number metadata."""
    grouped: dict[str, list[NoteEvent]] = defaultdict(list)
    for event in score.notes:
        if event.measure is None:
            continue
        # Keep the source label intact; XML may use non-integer measure labels like 12a.
        label = str(event.measure).strip()
        if label:
            grouped[label].append(event)
    result = []
    for label, events in grouped.items():
        starts = [float(e.start_q) for e in events]
        ends = [float(e.start_q) + float(e.duration_q) for e in events]
        result.append({
            "number": label,
            "start_q": min(starts),
            "end_q": max(ends),
        })
    def sort_key(item: dict[str, Any]) -> tuple[int, float, str]:
        label = str(item["number"])
        try:
            return (0, float(label), label)
        except ValueError:
            return (1, float(item["start_q"]), label)
    return sorted(result, key=sort_key)


def _issue(severity: str, code: str, message: str, recommendation: str,
           voice: str | None = None, measure: Any = None,
           start_q: float | None = None) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "recommendation": recommendation,
        "voice": voice,
        "measure": str(measure) if measure is not None else None,
        "start_q": round(float(start_q), 4) if start_q is not None else None,
    }


def analyze_score(score: ScoreData) -> dict[str, Any]:
    """Return explanatory composition/preflight warnings without changing the score."""
    issues: list[dict[str, Any]] = []
    measures = measure_ranges(score)
    named_voices = list(score.voice_order or score.part_names.keys())

    satb_present = set(named_voices) & set(VOICE_ORDER)
    if satb_present and satb_present != set(VOICE_ORDER):
        missing_parts = [voice for voice in ("Soprano", "Alto", "Tenor", "Bass") if voice not in satb_present]
        issues.append(_issue(
            "warning", "missing-satb-part",
            "Lipsesc cel puțin o parte SATB așteptată: " + ", ".join(missing_parts) + ".",
            "Verifică dacă partea lipsește din sursă sau a fost numită diferit la export.",
        ))

    for voice in named_voices:
        events = sorted(
            (e for e in score.notes if e.voice == voice and not e.is_rest and e.pitch is not None),
            key=lambda e: (e.start_q, e.pitch or 0),
        )
        if not events:
            issues.append(_issue(
                "warning", "empty-voice", f"{voice}: nu am găsit note cântabile.",
                "Verifică dacă partea este goală sau dacă importul a pierdut note.", voice,
            ))
            continue
        if voice in VOICE_RANGES:
            low, high = VOICE_RANGES[voice]
            for event in events:
                if int(event.pitch) < low or int(event.pitch) > high:
                    issues.append(_issue(
                        "warning", "outside-practical-range",
                        f"{voice}: nota MIDI {event.pitch} este în afara intervalului practic orientativ ({low}–{high}).",
                        "Confirmă octava și registrul cu un cântăreț; intervalele sunt orientative, nu reguli absolute.",
                        voice, event.measure, event.start_q,
                    ))
        previous: NoteEvent | None = None
        for event in events:
            if previous is not None and abs(previous.start_q - event.start_q) > 1e-5:
                leap = abs(int(event.pitch) - int(previous.pitch))
                if leap >= 12:
                    issues.append(_issue(
                        "info" if leap < 19 else "warning", "large-leap",
                        f"{voice}: salt melodic de {leap} semitonuri între două note succesive.",
                        "Verifică dacă saltul este intenționat și dacă este comod pentru intonație.",
                        voice, event.measure, event.start_q,
                    ))
            if (previous is None or event.start_q >= previous.start_q + previous.duration_q - 1e-5):
                previous = event

    # Voice crossing is evaluated only where both parts have an active note.
    # It is a review hint: crossings can be stylistically intentional.
    per_voice = {
        voice: sorted((e for e in score.notes if e.voice == voice and not e.is_rest and e.pitch is not None),
                      key=lambda e: e.start_q)
        for voice in named_voices
    }
    ordered_satb = sorted((voice for voice in named_voices if voice in VOICE_ORDER),
                          key=lambda voice: VOICE_ORDER[voice])
    for i, upper in enumerate(ordered_satb):
        for lower in ordered_satb[i + 1:]:
            seen: set[tuple[float, int, int]] = set()
            for a in per_voice[upper]:
                if a.pitch is None:
                    continue
                for b in per_voice[lower]:
                    if b.pitch is None:
                        continue
                    if b.start_q >= a.start_q + a.duration_q - 1e-5:
                        break
                    if a.start_q >= b.start_q + b.duration_q - 1e-5:
                        continue
                    if int(a.pitch) < int(b.pitch):
                        overlap = max(a.start_q, b.start_q)
                        key = (round(overlap, 4), int(a.pitch), int(b.pitch))
                        if key in seen:
                            continue
                        seen.add(key)
                        issues.append(_issue(
                            "info", "voice-crossing",
                            f"{upper} coboară sub {lower} într-o zonă suprapusă.",
                            "Ascultă pasajul și verifică dacă încrucișarea este intenționată.",
                            upper, a.measure if a.measure is not None else b.measure, overlap,
                        ))
                        # Avoid flooding the conductor with repeated warnings.
                        if len(seen) >= 8:
                            break
                if len(seen) >= 8:
                    break

    tempo_marks = [asdict(t) for t in score.tempos]
    bad_tempos = [t for t in tempo_marks if not 20 <= float(t["bpm"]) <= 400]
    for event in score.notes:
        if event.duration_q < 0:
            issues.append(_issue("error", "negative-duration", "Există o durată negativă în datele importate.",
                                 "Reexportă sau corectează partitura sursă.", event.voice, event.measure, event.start_q))
    count_by_severity = {
        level: sum(1 for item in issues if item["severity"] == level)
        for level in ("error", "warning", "info")
    }
    return {
        "title": score.title,
        "note_count": sum(1 for e in score.notes if not e.is_rest and e.pitch is not None),
        "voice_count": len(named_voices),
        "measure_count": len(measures),
        "measures": measures,
        "tempo_marks": tempo_marks,
        "summary": count_by_severity,
        "issues": issues,
        "disclaimer": (
            "Acestea sunt verificări euristice de repetiție/compoziție, nu o certificare muzicologică. "
            "Registrul vocal este orientativ; încrucișările și salturile pot fi intenționate. "
            "Nu se modifică automat nicio notă."
        ),
    }


def validate_annotations(payload: object, score: ScoreData) -> list[dict[str, Any]]:
    """Validate saved measure annotations before writing the local JSON file."""
    if not isinstance(payload, list):
        raise ValueError("Adnotările trebuie trimise ca listă.")
    if len(payload) > 1000:
        raise ValueError("Sunt permise maximum 1000 de adnotări pentru o partitură.")
    available_voices = set(score.part_names)
    valid_measures = {str(m["number"]) for m in measure_ranges(score)}
    result: list[dict[str, Any]] = []
    categories = {"general", "respiration", "entrance", "dynamics", "diction", "intonation", "cutoff", "other"}
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"Adnotarea {index + 1} trebuie să fie un obiect.")
        measure = str(item.get("measure", "")).strip()
        voice = str(item.get("voice", "all")).strip()
        category = str(item.get("category", "general")).strip().lower()
        status = str(item.get("status", "needs_work")).strip().lower()
        note = str(item.get("text", "")).strip()
        if not measure or measure not in valid_measures:
            raise ValueError(f"Măsura '{measure or '—'}' nu există în datele importate.")
        if voice != "all" and voice not in available_voices:
            raise ValueError(f"Voce necunoscută în adnotare: {voice}.")
        if category not in categories:
            raise ValueError(f"Categoria '{category}' nu este recunoscută.")
        if status not in {"needs_work", "practiced", "ready"}:
            raise ValueError("Starea adnotării trebuie să fie needs_work, practiced sau ready.")
        if not note or len(note) > 2000:
            raise ValueError("Textul fiecărei adnotări trebuie să aibă între 1 și 2000 de caractere.")
        result.append({
            "id": str(item.get("id") or f"annotation-{index + 1}"),
            "measure": measure,
            "voice": voice,
            "category": category,
            "status": status,
            "text": note,
        })
    return result
