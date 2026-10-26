"""Local score-video export using Verovio, SVG geometry, CairoSVG and FFmpeg.

The exporter makes its limits explicit: the engraving is Verovio's layout of the
normalized MusicXML, not a byte-for-byte reproduction of MuseScore's page layout.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Callable, Any
import json
from math import ceil, isfinite
import shutil
import subprocess
import tempfile
import wave
from io import BytesIO
import xml.etree.ElementTree as ET

from app.core import ScoreData, render_preview_wav, scaled_tempos, seconds_at_q
from app.vocals import VocalSynthesisError, mix_vocal_audio


SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)


class VideoExportError(RuntimeError):
    """A user-correctable video export error."""


def video_dependencies() -> dict[str, bool]:
    import importlib.util
    result = {
        "ffmpeg": False,
        "verovio": importlib.util.find_spec("verovio") is not None,
        "cairosvg": importlib.util.find_spec("cairosvg") is not None,
        "pillow": importlib.util.find_spec("PIL") is not None,
        "playwright": importlib.util.find_spec("playwright") is not None,
        "chromium": False,
    }
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            encoders = subprocess.run(
                [ffmpeg, "-hide_banner", "-encoders"],
                capture_output=True, text=True, check=False, timeout=10,
            )
            encoder_text = (encoders.stdout or "") + (encoders.stderr or "")
            result["ffmpeg"] = encoders.returncode == 0 and "libx264" in encoder_text and " aac " in encoder_text
        except (OSError, subprocess.TimeoutExpired):
            result["ffmpeg"] = False
    if result["playwright"]:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                result["chromium"] = Path(playwright.chromium.executable_path).exists()
        except Exception:
            result["chromium"] = False
    return result


def _source_bpm(score: ScoreData) -> float:
    return float(next((t.bpm for t in score.tempos if t.start_q <= 0), 100.0))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _write_voice_filtered_musicxml(
    source_path: Path,
    voice_order: list[str],
    selected_voice: str,
    output_path: Path,
) -> Path:
    """Create a valid MusicXML copy containing only the selected part.

    Filter before engraving so noteheads, stems, beams, ties, accidentals and
    lyrics are laid out together by Verovio. Hiding individual SVG note groups
    after engraving leaves orphaned beams and slurs and damages the notation.
    """
    if selected_voice not in voice_order:
        raise VideoExportError(f"Vocea {selected_voice} nu există în ordinea părților MusicXML.")
    try:
        tree = ET.parse(source_path)
        root = tree.getroot()
    except (ET.ParseError, OSError) as exc:
        raise VideoExportError("Nu pot pregăti MusicXML pentru vocea selectată: " + str(exc)) from exc

    parts = [child for child in list(root) if _local_name(child.tag) == "part"]
    selected_index = voice_order.index(selected_voice)
    if selected_index >= len(parts):
        raise VideoExportError(
            "Ordinea vocilor nu corespunde părților din MusicXML; exportul individual a fost oprit."
        )
    selected_part = parts[selected_index]
    selected_part_id = selected_part.get("id")
    if not selected_part_id:
        raise VideoExportError("Partea MusicXML selectată nu are un identificator valid.")

    part_list = next(
        (child for child in list(root) if _local_name(child.tag) == "part-list"),
        None,
    )
    if part_list is None:
        raise VideoExportError("Partitura MusicXML nu conține lista părților.")
    score_part_ids = {
        child.get("id") for child in list(part_list)
        if _local_name(child.tag) == "score-part"
    }
    if selected_part_id not in score_part_ids:
        raise VideoExportError(
            "Partitura MusicXML are un part-list care nu corespunde părții selectate."
        )

    # Keep exactly the selected part definition and its data. A single-voice video
    # should be re-engraved as a clean, single-staff score, not have other parts'
    # note glyphs selectively hidden after layout.
    for child in list(part_list):
        kind = _local_name(child.tag)
        if kind == "score-part" and child.get("id") != selected_part_id:
            part_list.remove(child)
        elif kind == "part-group":
            part_list.remove(child)
    for part in parts:
        if part is not selected_part:
            root.remove(part)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    return output_path

def _svg_geometry(svg_text: str, voice_order: list[str]) -> dict[str, Any]:
    """Use the locally installed Chromium to obtain transformed SVG bounding boxes."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise VideoExportError(
            "Lipsește Playwright. Instalează dependențele cu pip install -r requirements.txt."
        ) from exc

    html = "<!doctype html><html><head><meta charset='utf-8'></head><body style='margin:0'>" + svg_text + "</body></html>"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1200, "height": 900})
                page.set_content(html, wait_until="load")
                data = page.evaluate("""(expectedStaffCount) => {
                  const outerSvg = document.querySelector('svg');
                  // Verovio wraps notation in an inner <svg class="definition-scale">.
                  // Measure/crop in that inner SVG's viewBox coordinates, not the outer
                  // page viewport; changing the outer viewBox alone leaves notation uncropped.
                  const svg = document.querySelector('svg.definition-scale') || outerSvg;
                  if (!svg) throw new Error('Verovio did not produce an SVG root');
                  const vb = svg.viewBox && svg.viewBox.baseVal;
                  const rootBox = vb && vb.width > 0 && vb.height > 0
                    ? {x: vb.x, y: vb.y, width: vb.width, height: vb.height}
                    : {x: 0, y: 0, width: parseFloat(svg.getAttribute('width')) || 2100,
                       height: parseFloat(svg.getAttribute('height')) || 60000};
                  // The root SVG transform is stable for this layout query.
                  // Cache its inverse: every note and staff box otherwise recalculates it.
                  const rootScreenCTM = svg.getScreenCTM();
                  const rootScreenCTMInverse = rootScreenCTM ? rootScreenCTM.inverse() : null;
                  function box(el) {
                    const b = el.getBBox();
                    const c = el.getScreenCTM();
                    if (!c || !rootScreenCTMInverse) return null;
                    const m = rootScreenCTMInverse.multiply(c);
                    const pts = [
                      new DOMPoint(b.x,b.y).matrixTransform(m),
                      new DOMPoint(b.x+b.width,b.y).matrixTransform(m),
                      new DOMPoint(b.x,b.y+b.height).matrixTransform(m),
                      new DOMPoint(b.x+b.width,b.y+b.height).matrixTransform(m)
                    ];
                    const xs = pts.map(p=>p.x), ys=pts.map(p=>p.y);
                    const x=Math.min(...xs), y=Math.min(...ys);
                    return {x, y, width:Math.max(...xs)-x, height:Math.max(...ys)-y};
                  }
                  let systems = Array.from(svg.querySelectorAll('g.system'));
                  if (!systems.length) systems = Array.from(svg.querySelectorAll('.system'));
                  // Some Verovio versions nest every real system inside a page-wide
                  // wrapper that also has class "system". Keep only leaf system groups
                  // so one oversized wrapper cannot make the full score look like one system.
                  const systemCandidates = new Set(systems);
                  systems = systems.filter(el =>
                    !Array.from(el.querySelectorAll('.system')).some(child => systemCandidates.has(child))
                  );
                  const staffCenterCache = new Map();
                  const staffRowsCache = new Map();
                  function staffCenterY(staff) {
                    if (staffCenterCache.has(staff)) return staffCenterCache.get(staff);
                    // Verovio creates one staff group per measure, not one group per
                    // continuous staff. Use its staff-line path for a stable row
                    // coordinate; the full staff group's bounding box also includes
                    // notes/ledger lines and changes from measure to measure.
                    const line = staff.querySelector('path.staff, line.staff');
                    const b = box(line || staff);
                    const y = b ? b.y + b.height / 2 : NaN;
                    staffCenterCache.set(staff, y);
                    return y;
                  }
                  function staffRows(system) {
                    if (staffRowsCache.has(system)) return staffRowsCache.get(system);
                    const centers = [];
                    for (const staff of Array.from(system.querySelectorAll('g.staff'))) {
                      const y = staffCenterY(staff);
                      if (Number.isFinite(y)) centers.push(y);
                    }
                    centers.sort((a,b)=>a-b);

                    // Verovio may emit several staff groups per bar/measure. Their
                    // vertical centers vary a little even when they belong to the
                    // same musical staff. Cluster close centers relative to this
                    // system's height; real SATB rows are spaced much farther apart.
                    const systemBounds = box(system);
                    const tolerance = Math.max(
                      100,
                      (systemBounds ? systemBounds.height : 1000) * 0.12,
                    );
                    const rows = [];
                    for (const y of centers) {
                      let closestIndex = -1;
                      let closestDistance = Infinity;
                      for (let i = 0; i < rows.length; i++) {
                        const distance = Math.abs(rows[i].center - y);
                        if (distance < closestDistance) {
                          closestDistance = distance;
                          closestIndex = i;
                        }
                      }
                      if (closestIndex < 0 || closestDistance >= tolerance) {
                        rows.push({center: y, count: 1});
                      } else {
                        const row = rows[closestIndex];
                        row.center = (row.center * row.count + y) / (row.count + 1);
                        row.count += 1;
                      }
                    }
                    const result = rows.map(row => row.center).sort((a,b)=>a-b);
                    staffRowsCache.set(system, result);
                    return result;
                  }
                  const systemRecords = systems.map((el,index)=>({
                    index, id:el.id||'',
                    staff_count: staffRows(el).length,
                    staff_centers: staffRows(el),
                    ...box(el)
                  }));
                  const noteEls = Array.from(svg.querySelectorAll('[id]')).filter(el => {
                    const c = Array.from(el.classList || []);
                    return c.includes('note') || c.includes('rest') || c.includes('chord');
                  });
                  const elements = [];
                  for (const el of noteEls) {
                    const bb = box(el);
                    if (!bb || !Number.isFinite(bb.x) || bb.width <= 0 || bb.height <= 0) continue;
                    let sys = el.closest('g.system') || el.closest('.system');
                    let sysIndex = systems.indexOf(sys);
                    if (sysIndex < 0) {
                      // Recover the actual containing leaf system if closest('.system')
                      // returns a page-wide wrapper that was filtered out above.
                      sysIndex = systems.findIndex(candidate => candidate.contains(el));
                      if (sysIndex >= 0) sys = systems[sysIndex];
                    }
                    const staff = el.closest('g.staff') || el.closest('.staff');
                    let staffRank = -1;
                    if (sys && sysIndex >= 0 && staff) {
                      const rows = staffRows(sys);
                      const center = staffCenterY(staff);
                      if (Number.isFinite(center) && rows.length) {
                        let bestDistance = Infinity;
                        for (let rowIndex = 0; rowIndex < rows.length; rowIndex++) {
                          const distance = Math.abs(rows[rowIndex] - center);
                          if (distance < bestDistance) {
                            bestDistance = distance;
                            staffRank = rowIndex;
                          }
                        }
                      }
                    }
                    const classes = Array.from(el.classList || []);
                    const kind = classes.includes('rest') ? 'rest' : (classes.includes('chord') ? 'chord' : 'note');
                    elements.push({id:el.id, ...bb, system_index:sysIndex, staff_rank:staffRank, kind});
                  }
                  return {viewBox:rootBox, systems:systemRecords, elements};
                }""", len(voice_order))
                return data
            finally:
                browser.close()
    except VideoExportError:
        raise
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" in text or "browserType.launch" in text or "Failed to launch" in text:
            raise VideoExportError(
                "Chromium-ul local pentru randarea video lipsește. Rulează: python -m playwright install chromium"
            ) from exc
        raise VideoExportError("Nu am putut calcula pozițiile notelor pentru video: " + text[:500]) from exc


def _svg_tag(name: str) -> str:
    return f"{{{SVG_NS}}}{name}"


def _safe_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _validate_staff_mapping(
    systems: list[dict[str, Any]],
    expected_staff_count: int,
    selected_voice: str,
) -> None:
    """Fail clearly instead of silently assigning notes to the wrong SATB voice."""
    if expected_staff_count < 1:
        raise VideoExportError("Nu există portative așteptate pentru această randare.")
    for index, system in enumerate(systems):
        actual = int(_safe_float(system.get("staff_count"), 0))
        if actual != expected_staff_count:
            if selected_voice != "all":
                raise VideoExportError(
                    f"Exportul pentru {selected_voice} cere un singur portativ, dar "
                    f"am detectat {actual} în sistemul {index + 1}. Exportă acea voce "
                    "ca MusicXML separat, cu un singur portativ."
                )
            raise VideoExportError(
                f"Maparea SATB nu este sigură: sistemul {index + 1} are {actual} "
                f"portative, dar sunt așteptate {expected_staff_count}. Verifică dacă "
                "partitura folosește câte un portativ pentru fiecare voce."
            )


def _system_pair(index: int, systems: list[dict[str, Any]]) -> tuple[int, int]:
    if not systems:
        raise VideoExportError("Verovio nu a putut identifica sistemele muzicale din partitură.")
    index = min(max(0, index), len(systems) - 1)
    if len(systems) == 1:
        return 0, 0
    if index == len(systems) - 1:
        return index - 1, index
    return index, index + 1


def _continuous_strip_crop_height(natural_height: float) -> float:
    """Give a continuous horizontal score enough context to show multiple measures.

    Cropping exactly to the staff/system height makes the 16:9 window too narrow
    and can leave only a small fragment of one measure visible.
    """
    height = _safe_float(natural_height, 10.0)
    if not isfinite(height) or height <= 0:
        height = 10.0
    return max(10.0, height) * 2.2


def _background_signature(state: dict[str, Any]) -> tuple[Any, ...]:
    """Identify the visual notation state so duplicate note-off events reuse a background."""
    active = tuple(sorted(str(note_id) for note_id in state.get("active_ids", set())))
    return (
        active,
        round(_safe_float(state.get("crop_x")), 3),
        round(_safe_float(state.get("crop_y")), 3),
        round(_safe_float(state.get("crop_width")), 3),
        round(_safe_float(state.get("crop_height")), 3),
    )


def _validate_video_export_options(
    reference_bpm: Any,
    resolution: str,
    output_suffix: str,
    offset_ms: Any,
) -> tuple[float, int]:
    """Validate scalar export options before checking tools or creating temporary files."""
    try:
        bpm = float(reference_bpm)
    except (TypeError, ValueError, OverflowError) as exc:
        raise VideoExportError("Tempo-ul de referință trebuie să fie între 30 și 240 BPM.") from exc
    if not isfinite(bpm) or not 30 <= bpm <= 240:
        raise VideoExportError("Tempo-ul de referință trebuie să fie între 30 și 240 BPM.")
    if resolution not in {"1080p", "4k"}:
        raise VideoExportError("Rezoluție video invalidă. Alege 1080p sau 4k.")
    if str(output_suffix).lower() not in {".mp4", ".mov"}:
        raise VideoExportError("Format video invalid. Exportă ca MP4 sau MOV.")
    try:
        offset_value = float(offset_ms)
    except (TypeError, ValueError, OverflowError) as exc:
        raise VideoExportError("Decalajul de sincronizare trebuie să fie între -5000 și 5000 ms.") from exc
    if not isfinite(offset_value) or not -5000 <= offset_value <= 5000 or not offset_value.is_integer():
        raise VideoExportError("Decalajul de sincronizare trebuie să fie un număr întreg între -5000 și 5000 ms.")
    return bpm, int(offset_value)


def _prepare_video_timing(
    score: ScoreData,
    reference_bpm: float,
    offset_ms: int,
) -> tuple[float, list, float]:
    """Validate tempo metadata and duration before engraving a potentially huge score."""
    try:
        total_quarters = float(score.total_quarters)
    except (TypeError, ValueError, OverflowError) as exc:
        raise VideoExportError("Partitura fără durată muzicală validă.") from exc
    if not isfinite(total_quarters) or total_quarters <= 0:
        raise VideoExportError("Partitura trebuie să aibă o durată muzicală finită și pozitivă.")

    for tempo in score.tempos or []:
        start_q = _safe_float(getattr(tempo, "start_q", None), float("nan"))
        bpm = _safe_float(getattr(tempo, "bpm", None), float("nan"))
        if not isfinite(start_q):
            raise VideoExportError("Harta de tempo conține o poziție nenumerică sau infinită.")
        if not isfinite(bpm) or bpm <= 0:
            raise VideoExportError("Harta de tempo conține un BPM invalid; fiecare tempo trebuie să fie finit și pozitiv.")

    source_bpm = _source_bpm(score)
    if not isfinite(source_bpm) or source_bpm <= 0:
        raise VideoExportError("Tempo-ul inițial al partiturii nu este valid.")
    try:
        scaled = scaled_tempos(score.tempos, reference_bpm)
    except (ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
        raise VideoExportError("Nu pot calcula harta de tempo pentru exportul video.") from exc

    for tempo in scaled:
        if (
            not isfinite(_safe_float(getattr(tempo, "start_q", None), float("nan")))
            or not isfinite(_safe_float(getattr(tempo, "bpm", None), float("nan")))
            or _safe_float(getattr(tempo, "bpm", 0.0)) <= 0
        ):
            raise VideoExportError("Harta de tempo rezultată este în afara limitelor numerice.")

    try:
        video_duration = (
            seconds_at_q(total_quarters, scaled)
            + 1.25
            + max(0, offset_ms) / 1000.0
        )
    except (ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
        raise VideoExportError("Nu pot calcula durata video din harta de tempo.") from exc
    if not isfinite(video_duration) or video_duration <= 0 or video_duration > 20 * 60:
        raise VideoExportError("Durata exportabilă trebuie să fie finită, pozitivă și de maximum 20 de minute.")
    return source_bpm, scaled, video_duration


def _create_video_workdir(output_path: Path) -> tuple[Any, Path]:
    """Create isolated temporary storage for one export, safe for parallel jobs."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    safe_stem = "".join(
        character for character in output_path.stem
        if character.isalnum() or character in "-_"
    )[:40] or "video"
    temporary_directory = tempfile.TemporaryDirectory(
        prefix=f".{safe_stem}-render-",
        dir=str(output_path.parent),
    )
    return temporary_directory, Path(temporary_directory.name)


def _build_ffmpeg_stream_command(
    ffmpeg: str,
    wav_path: Path,
    output_path: Path,
    width: int,
    height: int,
    fps: int,
    video_duration: float,
    wav_duration: float,
) -> list[str]:
    """Encode raw RGB frames from stdin, avoiding a PNG file per output frame."""
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo",
        "-pixel_format", "rgb24",
        "-video_size", f"{width}x{height}",
        "-framerate", str(fps),
        "-i", "pipe:0",
        "-i", str(wav_path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", "format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-af", f"apad=pad_dur={max(0.0, video_duration - wav_duration):.3f}",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{video_duration:.3f}",
    ]
    if output_path.suffix.lower() == ".mp4":
        command += ["-movflags", "+faststart"]
    command.append(str(output_path))
    return command


def _camera_crop_x(
    cursor_x: float,
    system_left: float,
    current_crop_x: float | None,
    crop_width: float,
    view_x: float,
    view_width: float,
) -> float:
    """Keep the camera still while the cursor travels; pan only near viewport edges."""
    max_x = max(view_x, view_x + view_width - crop_width)
    start_x = system_left - crop_width * 0.01 if current_crop_x is None else current_crop_x
    crop_x = min(max(view_x, start_x), max_x)
    left_limit = crop_x + crop_width * 0.12
    right_limit = crop_x + crop_width * 0.88
    if cursor_x < left_limit:
        crop_x = min(max(view_x, cursor_x - crop_width * 0.20), max_x)
    elif cursor_x > right_limit:
        crop_x = min(max(view_x, cursor_x - crop_width * 0.80), max_x)
    return crop_x


def _cursor_span_for_system(
    system: dict[str, Any],
    crop_y: float,
    crop_height: float,
) -> tuple[float, float]:
    """Keep the cursor vertically inside the active system, not a two-system crop."""
    y_top = max(crop_y, _safe_float(system.get("y")))
    y_bottom = min(
        crop_y + crop_height,
        _safe_float(system.get("y")) + _safe_float(system.get("height")),
    )
    if y_bottom <= y_top:
        raise VideoExportError("Sistemul activ se află în afara cadrului video.")
    return y_top, y_bottom


def _build_cursor_keyframes(
    grouped: dict[float, dict[str, set[str]]],
    element_info: dict[str, dict[str, Any]],
    voice: str,
) -> list[dict[str, Any]]:
    """Build cursor keyframes from note-on events, independently of note-off events."""
    keyframes: list[dict[str, Any]] = []
    for time_s, kinds in sorted(grouped.items()):
        note_ids = [
            str(note_id) for note_id in (kinds.get("on", set()) or set())
            if str(note_id) in element_info
            and (voice == "all" or element_info[str(note_id)].get("voice") == voice)
        ]
        if not note_ids:
            continue
        notes = [element_info[note_id] for note_id in note_ids]
        system_counts: dict[int, int] = defaultdict(int)
        for note in notes:
            system_counts[int(note.get("system_index", -1))] += 1
        system_index = max(system_counts, key=lambda idx: (system_counts[idx], -idx))
        notes = [note for note in notes if int(note.get("system_index", -1)) == system_index]
        centers = sorted(
            _safe_float(note.get("x")) + _safe_float(note.get("width")) / 2.0
            for note in notes
        )
        if centers:
            keyframes.append({
                "time": float(time_s),
                "x": float(centers[len(centers) // 2]),
                "system_index": system_index,
            })
    return keyframes


def _cursor_position_at(
    keyframes: list[dict[str, Any]],
    time_s: float,
    keyframe_times: list[float] | None = None,
) -> dict[str, Any]:
    """Interpolate cursor position; reusable sorted times keep long exports efficient."""
    if not keyframes:
        raise VideoExportError("Nu există poziții temporizate pentru cursorul video.")
    times = keyframe_times if keyframe_times is not None else [
        float(point["time"]) for point in keyframes
    ]
    if len(times) != len(keyframes):
        raise VideoExportError("Lista de timpi a cursorului nu corespunde notelor temporizate.")
    index = bisect_right(times, float(time_s)) - 1
    if index < 0:
        first = keyframes[0]
        return {"x": float(first["x"]), "system_index": int(first["system_index"])}
    left = keyframes[index]
    if index >= len(keyframes) - 1:
        return {"x": float(left["x"]), "system_index": int(left["system_index"])}
    right = keyframes[index + 1]
    x0, x1 = float(left["x"]), float(right["x"])
    t0, t1 = float(left["time"]), float(right["time"])
    h = t1 - t0
    system_index = int(left["system_index"])
    if h <= 0 or int(right["system_index"]) != system_index:
        return {"x": x0, "system_index": system_index}
    u = max(0.0, min(1.0, (float(time_s) - t0) / h))
    secant = (x1 - x0) / h
    m0 = secant
    m1 = secant
    if index > 0:
        previous = keyframes[index - 1]
        hp = t0 - float(previous["time"])
        if hp > 0 and int(previous["system_index"]) == system_index:
            previous_slope = (x0 - float(previous["x"])) / hp
            if previous_slope * secant > 0:
                w1, w2 = 2.0 * h + hp, h + 2.0 * hp
                m0 = (w1 + w2) / (w1 / previous_slope + w2 / secant)
            else:
                m0 = 0.0
    if index + 2 < len(keyframes):
        following = keyframes[index + 2]
        hn = float(following["time"]) - t1
        if hn > 0 and int(following["system_index"]) == system_index:
            following_slope = (float(following["x"]) - x1) / hn
            if secant * following_slope > 0:
                w1, w2 = 2.0 * hn + h, hn + 2.0 * h
                m1 = (w1 + w2) / (w1 / secant + w2 / following_slope)
            else:
                m1 = 0.0
    u2, u3 = u * u, u * u * u
    x = (
        (2.0 * u3 - 3.0 * u2 + 1.0) * x0
        + (u3 - 2.0 * u2 + u) * h * m0
        + (-2.0 * u3 + 3.0 * u2) * x1
        + (u3 - u2) * h * m1
    )
    x = max(min(x0, x1), min(max(x0, x1), x))
    return {"x": x, "system_index": system_index}

def _timemap_event_seconds(event: dict[str, Any], scaled_tempos_map: list, offset_seconds: float,
                             source_bpm: float, reference_bpm: float) -> float:
    if event.get("qstamp") is not None:
        qstamp = max(0.0, _safe_float(event.get("qstamp")))
        return seconds_at_q(qstamp, scaled_tempos_map) + offset_seconds
    fallback_scale = source_bpm / float(reference_bpm)
    return _safe_float(event.get("tstamp")) / 1000.0 * fallback_scale + offset_seconds


def _read_timeline(raw: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Verovio Python bindings may return JSON text or an already-decoded list.
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except Exception as exc:
            raise VideoExportError("Verovio nu a returnat o hartă de timp validă.") from exc
    else:
        payload = raw
    if not isinstance(payload, list):
        raise VideoExportError("Verovio a returnat un format de hartă de timp neașteptat.")
    return sorted((item for item in payload if isinstance(item, dict)),
                  key=lambda item: _safe_float(item.get("tstamp")))


def _validate_timeline_ids(
    timemap: list[dict[str, Any]],
    element_info: dict[str, dict[str, Any]],
    svg_nodes: dict[str, ET.Element],
) -> set[str]:
    """Reject a render if timing events cannot be tied to actual rendered glyph IDs."""
    timed_ids = {
        str(note_id)
        for event in timemap
        for kind in ("on", "off")
        for note_id in (event.get(kind, []) or [])
    }
    if not timed_ids:
        raise VideoExportError("Harta temporală Verovio nu conține nicio notă animabilă.")
    missing_geometry = sorted(timed_ids - set(element_info))
    missing_svg = sorted(timed_ids - set(svg_nodes))
    if missing_geometry or missing_svg:
        samples = sorted(set(missing_geometry + missing_svg))[:6]
        raise VideoExportError(
            "Nu pot sincroniza în siguranță toate notele cu gravura SVG "
            f"(fără geometrie: {len(missing_geometry)}, fără element SVG: {len(missing_svg)}). "
            "Exportul a fost oprit pentru a evita un video cu cursor/evidențieri incorecte. "
            "Încearcă să exporți partitura din nou ca MusicXML simplu. "
            "ID-uri exemplu: " + ", ".join(samples)
        )
    return timed_ids


def _render_frame(
    root: ET.Element,
    canvas_svg: ET.Element,
    node_by_id: dict[str, ET.Element],
    all_element_info: dict[str, dict[str, Any]],
    active_ids: set[str],
    cursor_x: float,
    cursor_y1: float,
    cursor_y2: float,
    crop_x: float,
    crop_y: float,
    crop_w: float,
    crop_h: float,
    width: int,
    height: int,
    frames_dir: Path,
    frame_index: int,
    previous_active: set[str],
    line: ET.Element,
    background: ET.Element,
    style: ET.Element,
) -> tuple[Path, set[str]]:
    import cairosvg

    for note_id in previous_active - active_ids:
        node = node_by_id.get(note_id)
        if node is None:
            continue
        classes = (node.get("class") or "").split()
        classes = [c for c in classes if not c.startswith("mp4-active-")]
        node.set("class", " ".join(classes))
    for note_id in active_ids:
        node = node_by_id.get(note_id)
        info = all_element_info.get(note_id)
        if node is None or info is None:
            continue
        classes = [c for c in (node.get("class") or "").split() if not c.startswith("mp4-active-")]
        voice_name = (info.get("voice") or "all").lower().replace(" ", "-")
        classes.append("mp4-active-" + voice_name)
        node.set("class", " ".join(classes))

    # The outer Verovio SVG is a page viewport; the notation lives in a nested
    # definition-scale SVG. Resize the outer viewport to the video frame and crop
    # the inner notation SVG. Cropping the outer viewBox alone lets the inner SVG
    # overflow with the entire score, which creates a narrow score strip and stray glyphs.
    root.set("viewBox", f"0 0 {width} {height}")
    root.set("width", f"{width}px")
    root.set("height", f"{height}px")
    root.set("preserveAspectRatio", "xMidYMid meet")
    root.set("overflow", "hidden")
    canvas_svg.set("x", "0")
    canvas_svg.set("y", "0")
    canvas_svg.set("width", f"{width}px")
    canvas_svg.set("height", f"{height}px")
    canvas_svg.set("viewBox", f"{crop_x:.4f} {crop_y:.4f} {crop_w:.4f} {crop_h:.4f}")
    canvas_svg.set("preserveAspectRatio", "xMidYMid meet")
    # The Verovio score is a nested SVG and may otherwise paint beyond its
    # viewport into the video corners even after its viewBox is cropped.
    canvas_svg.set("overflow", "hidden")
    inline_style = canvas_svg.get("style", "").strip().rstrip(";")
    canvas_svg.set("style", (inline_style + ";" if inline_style else "") + "overflow:hidden")
    # The background is a child of the outer SVG, whose viewBox is the output
    # frame in pixel coordinates. Keep it independent of the notation crop.
    background.set("x", "0")
    background.set("y", "0")
    background.set("width", str(width))
    background.set("height", str(height))
    line.set("x1", f"{cursor_x:.4f}")
    line.set("x2", f"{cursor_x:.4f}")
    line.set("y1", f"{max(crop_y, cursor_y1):.4f}")
    line.set("y2", f"{min(crop_y + crop_h, cursor_y2):.4f}")

    target = frames_dir / f"frame_{frame_index:06d}.png"
    xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    try:
        # Set an opaque raster background as well as the SVG rect. This prevents
        # half-transparent antialiased pixels at the image boundary becoming dark
        # after FFmpeg converts the PNG frames to YUV420 video.
        cairosvg.svg2png(bytestring=xml, write_to=str(target),
                         output_width=width, output_height=height,
                         background_color="#f8f5ec")
    except Exception as exc:
        raise VideoExportError("Nu am putut rasteriza un cadru video SVG: " + str(exc)[:400]) from exc
    return target, set(active_ids)


def render_score_video(
    score: ScoreData,
    musicxml_path: Path,
    output_path: Path,
    reference_bpm: float = 100,
    resolution: str = "1080p",
    voice: str = "all",
    audio_mode: str = "isolated",
    offset_ms: int = 0,
    progress: Callable[[float, str], None] | None = None,
    volume_by_voice: dict[str, int] | None = None,
    vocal_stems_dir: Path | None = None,
) -> dict[str, Any]:
    """Render a local H.264/AAC video from one continuous horizontal score strip."""
    reference_bpm, offset_ms = _validate_video_export_options(
        reference_bpm, resolution, output_path.suffix, offset_ms,
    )
    deps = video_dependencies()
    missing = [name for name, available in deps.items() if not available]
    if missing:
        raise VideoExportError(
            "Lipsesc dependențele video: " + ", ".join(missing) +
            ". Instalează dependințele Python și FFmpeg; pentru Chromium rulează "
            "python -m playwright install chromium."
        )
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VideoExportError("FFmpeg nu este instalat local.")
    available_voices = list(score.voice_order or score.part_names.keys())
    if voice != "all" and voice not in score.part_names:
        raise VideoExportError("Voce necunoscută: " + voice)
    if audio_mode not in {"isolated", "accompaniment"}:
        raise VideoExportError("Mod audio invalid. Alege isolated sau accompaniment.")
    if voice == "all":
        audio_mode = "accompaniment"

    # Fail fast on malformed tempo maps or oversized scores before Verovio lays
    # out a potentially enormous one-line SVG.
    source_bpm, scaled, video_duration = _prepare_video_timing(
        score, reference_bpm, offset_ms,
    )

    import verovio

    render_musicxml_path = musicxml_path
    if voice != "all":
        filtered_path = output_path.parent / (".voice-" + "".join(
            ch for ch in voice.lower() if ch.isalnum() or ch in "-_"
        ) + ".musicxml")
        render_musicxml_path = _write_voice_filtered_musicxml(
            musicxml_path, available_voices, voice, filtered_path
        )

    if progress:
        progress(0.03, "Pregătesc partitura și harta de timp")
    try:
        toolkit = verovio.toolkit()
        options = {
            "inputFrom": "xml",
            "pageWidth": 2100,
            "pageHeight": 60000,
            "adjustPageHeight": True,
            # Keep all measures on one horizontal strip; the camera pans along it.
            "breaks": "none",
            "adjustPageWidth": True,
            "scale": 50,
            "footer": "none",
            "header": "none",
            "spacingLinear": 0.25,
            "spacingNonLinear": 0.6,
        }
        if not toolkit.setOptions(options):
            raise VideoExportError("Verovio a respins opțiunile de randare.")
        if not toolkit.loadFile(str(render_musicxml_path)):
            detail = (toolkit.getLog() or "format MusicXML necunoscut").strip()[-600:]
            raise VideoExportError("Verovio nu a putut încărca MusicXML-ul: " + detail)
        page_count = int(toolkit.getPageCount())
        if page_count != 1:
            raise VideoExportError(
                "Partitura este prea lungă pentru randarea continuă într-o singură pânză "
                f"({page_count} pagini). Exportă o copie MusicXML fără layout-uri anormale sau "
                "împarte lucrarea în secțiuni pentru export video."
            )
        svg_text = toolkit.renderToSVG(1)
        timemap = _read_timeline(toolkit.renderToTimemap({"includeRests": True}))
    except VideoExportError:
        raise
    except Exception as exc:
        raise VideoExportError("Verovio nu a putut pregăti partitura: " + str(exc)[:600]) from exc
    if not svg_text or "<svg" not in svg_text:
        raise VideoExportError("Verovio nu a produs o imagine SVG pentru partitură.")

    # A selected-part MusicXML renders as a one-voice score. The full source
    # order only applies when the user exports all SATB parts.
    render_voice_order = [voice] if voice != "all" else available_voices
    geometry = _svg_geometry(svg_text, render_voice_order)
    systems = geometry.get("systems") or []
    if not systems:
        raise VideoExportError("Nu am identificat sistemele muzicale în imaginea SVG.")
    _validate_staff_mapping(systems, len(render_voice_order), voice)
    elements_list = geometry.get("elements") or []
    element_info: dict[str, dict[str, Any]] = {}
    for element in elements_list:
        if not element.get("id"):
            continue
        rank = int(element.get("staff_rank", -1))
        voice_name = render_voice_order[rank] if 0 <= rank < len(render_voice_order) else None
        element_info[element["id"]] = {**element, "voice": voice_name}
    node_root = ET.fromstring(svg_text.encode("utf-8"))
    nested_svgs = [
        node for node in node_root.iter()
        if node.tag == _svg_tag("svg") and node is not node_root
    ]
    canvas_svg = next(
        (node for node in nested_svgs if "definition-scale" in (node.get("class") or "").split()),
        nested_svgs[0] if nested_svgs else None,
    )
    if canvas_svg is None:
        raise VideoExportError(
            "Structura SVG Verovio nu conține pânza internă necesară decupării video."
        )
    node_by_id = {node.get("id"): node for node in node_root.iter() if node.get("id")}
    timed_ids = _validate_timeline_ids(timemap, element_info, node_by_id)
    unmapped_timed_ids = [
        note_id for note_id in timed_ids
        if int(element_info[note_id].get("system_index", -1)) < 0
        or int(element_info[note_id].get("staff_rank", -1)) < 0
    ]
    if unmapped_timed_ids:
        examples = ", ".join(sorted(unmapped_timed_ids)[:5])
        raise VideoExportError(
            f"Nu pot mapa poziția sau portativul pentru {len(unmapped_timed_ids)} note temporizate. "
            "Exportul a fost oprit pentru a evita un cursor incorect. ID-uri exemplu: " + examples
        )
    if voice != "all":
        mapped = [i for i in element_info.values() if i.get("voice") == voice]
        if not mapped:
            raise VideoExportError(
                "Nu pot mapa fără ambiguitate notele pe vocea " + voice +
                ". Exportul video individual cere un sistem cu câte un portativ pentru fiecare parte SATB."
            )

    video_width, video_height = (1920, 1080) if resolution == "1080p" else (3840, 2160)
    viewbox = geometry.get("viewBox") or {}
    view_x = _safe_float(viewbox.get("x"))
    view_y = _safe_float(viewbox.get("y"))
    view_w = max(1.0, _safe_float(viewbox.get("width"), 2100))
    view_h = max(1.0, _safe_float(viewbox.get("height"), 60000))
    aspect = video_width / video_height
    margin_factor = 0.035
    boxes = []
    for system in systems:
        x, y = _safe_float(system.get("x")), _safe_float(system.get("y"))
        w, h = _safe_float(system.get("width")), _safe_float(system.get("height"))
        boxes.append({"x": x, "y": y, "width": w, "height": h})
    if any(b["height"] <= 0 for b in boxes):
        raise VideoExportError("Verovio a returnat dimensiuni invalide pentru sistemele muzicale.")

    # One continuous SVG lets the view window follow a pair of adjacent systems.
    # Use Verovio's qstamp (quarter-note position) to rebuild times from our exact
    # reference-tempo map. Raw tstamp is only a fallback for older timemap variants.
    offset_seconds = int(offset_ms) / 1000.0
    if progress:
        progress(0.10, "Generez pista audio locală")

    if voice == "all" or audio_mode == "accompaniment":
        audio_voices = available_voices
        focus_voice = voice if voice != "all" else None
    else:
        audio_voices = [voice]
        focus_voice = None
    # The sync offset moves the notation/highlights against fixed audio, not the audio itself.
    try:
        wav_bytes = mix_vocal_audio(
            score, vocal_stems_dir, audio_voices, reference_bpm,
            focus_voice=focus_voice, volume_by_voice=volume_by_voice,
            normalize_duration=True,
        )
    except VocalSynthesisError as exc:
        raise VideoExportError(str(exc)) from exc
    if wav_bytes is None:
        wav_bytes = render_preview_wav(
            score, reference_bpm, audio_voices, offset_ms=0, focus_voice=focus_voice,
            volume_by_voice=volume_by_voice,
        )
    workdir_handle, workdir_path = _create_video_workdir(output_path)
    # Keep a good previous export intact until the new output is fully encoded.
    staged_output_path = workdir_path / output_path.name
    wav_path = workdir_path / "video-audio.wav"
    wav_path.write_bytes(wav_bytes)
    with wave.open(BytesIO(wav_bytes), "rb") as wav:
        wav_duration = wav.getnframes() / max(1, wav.getframerate())
    if wav_duration <= 0:
        raise VideoExportError("Pista audio generată este goală.")

    # Keep only time-map events relevant to the selected part when rendering a part video.
    grouped: dict[float, dict[str, set[str]]] = defaultdict(lambda: {"on": set(), "off": set()})
    for event in timemap:
        output_time = _timemap_event_seconds(event, scaled, offset_seconds, source_bpm, reference_bpm)
        key = round(output_time, 4)
        for kind in ("on", "off"):
            for note_id in event.get(kind, []) or []:
                info = element_info.get(str(note_id))
                if voice != "all" and (not info or info.get("voice") != voice):
                    continue
                grouped[key][kind].add(str(note_id))
    if not grouped:
        raise VideoExportError("Verovio nu a găsit note temporizate pentru această partitură.")

    events = sorted(grouped.items())
    active: set[str] = set()
    previous_cursor_ids: set[str] = set()
    states: list[dict[str, Any]] = []
    # Events before time zero are applied so a negative offset starts mid-phrase correctly.
    for time_s, kinds in events:
        if time_s < 0:
            active.difference_update(kinds["off"])
            active.update(kinds["on"])
            if kinds["on"]:
                previous_cursor_ids = set(kinds["on"])
    states.append({"time": 0.0, "active": set(active), "cursor_ids": set(previous_cursor_ids)})
    for time_s, kinds in events:
        if time_s < 0 or time_s >= video_duration:
            continue
        active.difference_update(kinds["off"])
        active.update(kinds["on"])
        if kinds["on"]:
            previous_cursor_ids = set(kinds["on"])
        cursor_ids = set(previous_cursor_ids)
        if not cursor_ids and active:
            cursor_ids = set(active)
        if cursor_ids and states and abs(states[-1]["time"] - time_s) < 0.0001:
            states[-1] = {"time": time_s, "active": set(active), "cursor_ids": cursor_ids}
        else:
            states.append({"time": time_s, "active": set(active), "cursor_ids": cursor_ids})

    # If the first visible note is later than t=0 (positive offset), keep the score at its opening.
    if not states[0]["cursor_ids"]:
        first_on = next((item for _, kinds in events for item in sorted(kinds["on"])
                         if item in element_info and (voice == "all" or element_info[item].get("voice") == voice)), None)
        if first_on:
            states[0]["cursor_ids"] = {first_on}

    cursor_keyframes = _build_cursor_keyframes(grouped, element_info, voice)
    if not cursor_keyframes:
        raise VideoExportError("Nu pot calcula traiectoria cursorului din notele temporizate.")
    # If a positive sync offset delays the first note, show the opening note and
    # hold there until the shifted playback reaches it.
    if cursor_keyframes[0]["time"] > 0:
        cursor_keyframes.insert(0, {**cursor_keyframes[0], "time": 0.0})
    # Compute once after inserting the optional opening keyframe, so the timestamp
    # array always matches the keyframes (especially with positive sync offsets).
    cursor_keyframe_times = [float(point["time"]) for point in cursor_keyframes]

    frames_dir = workdir_path / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    # Render notation/highlight backgrounds only at musical state changes. Reuse
    # them for the 30 FPS cursor animation rather than rasterizing the entire SVG
    # thirty times per second.
    style = ET.Element(_svg_tag("style"))
    style.text = """
      .mp4-active-soprano path,.mp4-active-soprano polygon,.mp4-active-soprano ellipse,.mp4-active-soprano use {fill:#e11d48 !important;stroke:#9f1239 !important}
      .mp4-active-alto path,.mp4-active-alto polygon,.mp4-active-alto ellipse,.mp4-active-alto use {fill:#7c3aed !important;stroke:#5b21b6 !important}
      .mp4-active-tenor path,.mp4-active-tenor polygon,.mp4-active-tenor ellipse,.mp4-active-tenor use {fill:#2563eb !important;stroke:#1d4ed8 !important}
      .mp4-active-bass path,.mp4-active-bass polygon,.mp4-active-bass ellipse,.mp4-active-bass use {fill:#059669 !important;stroke:#047857 !important}
      .mp4-active-part-1 path,.mp4-active-part-1 polygon,.mp4-active-part-1 ellipse,.mp4-active-part-1 use {fill:#059669 !important;stroke:#047857 !important}
      .mp4-active-part-2 path,.mp4-active-part-2 polygon,.mp4-active-part-2 ellipse,.mp4-active-part-2 use {fill:#059669 !important;stroke:#047857 !important}
      #mp4-cursor {stroke:#e11d48;stroke-width:7;stroke-linecap:round;opacity:1}
      #mp4-background {fill:#f8f5ec}
    """
    node_root.insert(0, style)
    background = ET.Element(_svg_tag("rect"), {"id":"mp4-background"})
    node_root.insert(1, background)
    line = ET.SubElement(canvas_svg, _svg_tag("line"), {
        "id": "mp4-cursor", "stroke": "#e11d48", "stroke-width": "7",
        "stroke-linecap": "round", "opacity": "1", "pointer-events": "none",
    })
    line.set("display", "none")

    # Prepare camera geometry only. Rasterize backgrounds lazily during playback
    # so long scores do not leave thousands of full-resolution PNGs on disk.
    state_frames: list[dict[str, Any]] = []
    last_crop_system: int | None = None
    last_crop_x: float | None = None
    for i, state in enumerate(states):
        if progress and i % max(1, len(states) // 30) == 0:
            progress(
                0.12 + 0.26 * i / max(1, len(states)),
                f"Pregătesc încadrarea muzicală {i + 1}/{len(states)}",
            )
        position = _cursor_position_at(cursor_keyframes, float(state["time"]), cursor_keyframe_times)
        cursor_index = min(max(0, int(position["system_index"])), len(boxes) - 1)
        first_sys, second_sys = _system_pair(cursor_index, boxes)
        content_top = boxes[first_sys]["y"] - boxes[first_sys]["height"] * margin_factor
        content_bottom = boxes[second_sys]["y"] + boxes[second_sys]["height"] * (1.0 + margin_factor)
        natural_height = max(10.0, content_bottom - content_top)
        crop_height = _continuous_strip_crop_height(natural_height)
        crop_width = min(view_w, crop_height * aspect)
        if crop_width < 10.0:
            crop_width = min(view_w, 10.0)
            crop_height = crop_width / aspect
        current_x = None if cursor_index != last_crop_system else last_crop_x
        crop_x = _camera_crop_x(
            float(position["x"]), boxes[cursor_index]["x"], current_x,
            crop_width, view_x, view_w,
        )
        last_crop_x = crop_x
        last_crop_system = cursor_index
        crop_y = (content_top + content_bottom - crop_height) / 2.0
        # If the crop exceeds the one-line SVG height, keep the notation centered;
        # the extra area becomes paper background. Clamp only crops that fit.
        if crop_height <= view_h:
            crop_y = max(view_y, min(crop_y, view_y + view_h - crop_height))

        current_active = {
            n for n in state["active"]
            if n in node_by_id
            and element_info.get(n, {}).get("kind") != "rest"
            and (voice == "all" or element_info.get(n, {}).get("voice") == voice)
        }
        cursor_y1, cursor_y2 = _cursor_span_for_system(
            boxes[cursor_index], crop_y, crop_height
        )
        state_frames.append({
            "time": float(state["time"]),
            "active_ids": current_active,
            "system_index": cursor_index,
            "crop_x": crop_x,
            "crop_y": crop_y,
            "crop_width": crop_width,
            "crop_height": crop_height,
            "cursor_y1": cursor_y1,
            "cursor_y2": cursor_y2,
        })

    # Stream RGB frames directly to FFmpeg. The maximum 20-minute export is
    # 36,000 frames; encoding raw frames avoids writing that many PNGs to disk.
    from PIL import Image, ImageDraw

    fps = 30
    frame_count = max(1, int(ceil(video_duration * fps)))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = _build_ffmpeg_stream_command(
        ffmpeg, wav_path, staged_output_path, video_width, video_height, fps,
        video_duration, wav_duration,
    )
    state_index = 0
    cached_background_signature: tuple[Any, ...] | None = None
    cached_background = None
    rendered_active_ids: set[str] = set()
    ffmpeg_process = None
    ffmpeg_detail = ""
    return_code = -1
    try:
        with tempfile.TemporaryFile() as ffmpeg_log:
            ffmpeg_process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=ffmpeg_log,
            )
            if ffmpeg_process.stdin is None:
                raise VideoExportError("Nu am putut deschide fluxul de cadre către FFmpeg.")

            for frame_index in range(frame_count):
                frame_time = frame_index / fps
                while (
                    state_index + 1 < len(state_frames)
                    and state_frames[state_index + 1]["time"] <= frame_time + 1e-8
                ):
                    state_index += 1
                state = state_frames[state_index]
                position = _cursor_position_at(cursor_keyframes, frame_time, cursor_keyframe_times)

                background_signature = _background_signature(state)
                if cached_background_signature != background_signature:
                    if cached_background is not None:
                        cached_background.close()
                    target, rendered_active_ids = _render_frame(
                        node_root, canvas_svg, node_by_id, element_info, state["active_ids"],
                        float(_cursor_position_at(cursor_keyframes, float(state["time"]))["x"]),
                        state["cursor_y1"], state["cursor_y2"],
                        state["crop_x"], state["crop_y"], state["crop_width"], state["crop_height"],
                        video_width, video_height, frames_dir, -1, rendered_active_ids,
                        line, background, style,
                    )
                    try:
                        with Image.open(target) as source_image:
                            cached_background = source_image.convert("RGB")
                    finally:
                        target.unlink(missing_ok=True)
                    cached_background_signature = background_signature

                frame = cached_background.copy()
                try:
                    draw = ImageDraw.Draw(frame)
                    # Match SVG preserveAspectRatio="xMidYMid meet", including letterboxing.
                    viewport_scale = min(
                        video_width / max(1.0, state["crop_width"]),
                        video_height / max(1.0, state["crop_height"]),
                    )
                    viewport_offset_x = (video_width - state["crop_width"] * viewport_scale) / 2.0
                    viewport_offset_y = (video_height - state["crop_height"] * viewport_scale) / 2.0
                    cursor_x_px = round(
                        viewport_offset_x + (float(position["x"]) - state["crop_x"]) * viewport_scale
                    )
                    cursor_y1_px = round(
                        viewport_offset_y + (state["cursor_y1"] - state["crop_y"]) * viewport_scale
                    )
                    cursor_y2_px = round(
                        viewport_offset_y + (state["cursor_y2"] - state["crop_y"]) * viewport_scale
                    )
                    cursor_y1_px = max(0, min(video_height - 1, cursor_y1_px))
                    cursor_y2_px = max(0, min(video_height - 1, cursor_y2_px))
                    cursor_width = 7 if resolution == "1080p" else 14
                    draw.line(
                        (cursor_x_px, cursor_y1_px, cursor_x_px, cursor_y2_px),
                        fill=(225, 29, 72),
                        width=cursor_width,
                    )
                    if ffmpeg_process.stdin is None:
                        raise VideoExportError("Fluxul video către FFmpeg s-a închis prematur.")
                    try:
                        ffmpeg_process.stdin.write(frame.tobytes())
                    except (BrokenPipeError, OSError) as exc:
                        raise VideoExportError("FFmpeg a închis fluxul de cadre înainte de final.") from exc
                finally:
                    frame.close()

                if progress and frame_index % fps == 0:
                    progress(
                        0.40 + 0.48 * frame_index / max(1, frame_count),
                        f"Animez cursorul la {fps} FPS: cadrul {frame_index + 1}/{frame_count}",
                    )

            if cached_background is not None:
                cached_background.close()
                cached_background = None
            if ffmpeg_process.stdin is not None:
                ffmpeg_process.stdin.close()
            return_code = ffmpeg_process.wait(timeout=60 * 60)
            ffmpeg_log.seek(0)
            ffmpeg_detail = ffmpeg_log.read().decode("utf-8", errors="replace").strip()[-1200:]
    except subprocess.TimeoutExpired as exc:
        if ffmpeg_process is not None and ffmpeg_process.poll() is None:
            ffmpeg_process.kill()
            ffmpeg_process.wait()
        if ffmpeg_process is not None:
            staged_output_path.unlink(missing_ok=True)
        raise VideoExportError("Codarea video a depășit o oră și a fost oprită.") from exc
    except VideoExportError:
        if ffmpeg_process is not None:
            staged_output_path.unlink(missing_ok=True)
        if cached_background is not None:
            cached_background.close()
        if ffmpeg_process is not None and ffmpeg_process.poll() is None:
            ffmpeg_process.kill()
            ffmpeg_process.wait()
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        if ffmpeg_process is not None:
            staged_output_path.unlink(missing_ok=True)
        if cached_background is not None:
            cached_background.close()
        if ffmpeg_process is not None and ffmpeg_process.poll() is None:
            ffmpeg_process.kill()
            ffmpeg_process.wait()
        raise VideoExportError("Fluxul video a eșuat: " + str(exc)[:500]) from exc
    except Exception as exc:
        if ffmpeg_process is not None:
            staged_output_path.unlink(missing_ok=True)
        if cached_background is not None:
            cached_background.close()
        if ffmpeg_process is not None and ffmpeg_process.poll() is None:
            ffmpeg_process.kill()
            ffmpeg_process.wait()
        raise VideoExportError("Nu am putut genera cadrele video: " + str(exc)[:500]) from exc

    if return_code != 0 or not staged_output_path.exists() or staged_output_path.stat().st_size == 0:
        staged_output_path.unlink(missing_ok=True)
        detail = ffmpeg_detail or "FFmpeg nu a returnat un fișier video."
        raise VideoExportError("Exportul FFmpeg a eșuat: " + detail)
    try:
        staged_output_path.replace(output_path)
    except OSError as exc:
        raise VideoExportError("Nu am putut finaliza fișierul video: " + str(exc)[:400]) from exc
    if progress:
        progress(0.90, "Codare H.264 și AAC completată")

    # Every job owns a unique working directory; remove it after successful encode.
    workdir_handle.cleanup()
    if progress:
        progress(1.0, "Export video gata")
    return {
        "path": str(output_path),
        "bytes": output_path.stat().st_size,
        "duration_seconds": round(video_duration, 2),
        "frames": frame_count,
        "resolution": resolution,
        "format": output_path.suffix.lstrip(".").lower(),
        "voice": voice,
        "audio_mode": audio_mode,
        "renderer": "Verovio",
    }
