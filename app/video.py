"""Local score-video export using Verovio, SVG geometry, CairoSVG and FFmpeg.

The exporter makes its limits explicit: the engraving is Verovio's layout of the
normalized MusicXML, not a byte-for-byte reproduction of MuseScore's page layout.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Callable, Any
import json
import shutil
import subprocess
import wave
from io import BytesIO
import xml.etree.ElementTree as ET

from app.core import ScoreData, render_preview_wav, scaled_tempos, seconds_at_q


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
                data = page.evaluate("""() => {
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
                  function box(el) {
                    const b = el.getBBox();
                    const c = el.getScreenCTM();
                    const r = svg.getScreenCTM();
                    if (!c || !r) return null;
                    const m = r.inverse().multiply(c);
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
                  function staffCenterY(staff) {
                    // Verovio creates one staff group per measure, not one group per
                    // continuous staff. Use its staff-line path for a stable row
                    // coordinate; the full staff group's bounding box also includes
                    // notes/ledger lines and changes from measure to measure.
                    const line = staff.querySelector('path.staff, line.staff');
                    const b = box(line || staff);
                    return b ? b.y + b.height / 2 : NaN;
                  }
                  function staffRows(system) {
                    const rows = [];
                    for (const staff of Array.from(system.querySelectorAll('g.staff, .staff'))) {
                      const y = staffCenterY(staff);
                      if (!Number.isFinite(y)) continue;
                      const existing = rows.find(row => Math.abs(row - y) < 5);
                      if (existing === undefined) rows.push(y);
                    }
                    return rows.sort((a,b)=>a-b);
                  }
                  const systemRecords = systems.map((el,index)=>({
                    index, id:el.id||'',
                    staff_count: staffRows(el).length,
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
                    const sys = el.closest('g.system') || el.closest('.system');
                    const sysIndex = systems.indexOf(sys);
                    const staff = el.closest('g.staff') || el.closest('.staff');
                    let staffRank = -1;
                    if (sys && staff) {
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
                }""")
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


def _system_pair(index: int, systems: list[dict[str, Any]]) -> tuple[int, int]:
    if not systems:
        raise VideoExportError("Verovio nu a putut identifica sistemele muzicale din partitură.")
    index = min(max(0, index), len(systems) - 1)
    if len(systems) == 1:
        return 0, 0
    if index == len(systems) - 1:
        return index - 1, index
    return index, index + 1


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
) -> dict[str, Any]:
    """Render a local H.264/AAC video with a two-system moving view and note highlighting."""
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
    if not 30 <= float(reference_bpm) <= 240:
        raise VideoExportError("Tempo-ul de referință trebuie să fie între 30 și 240 BPM.")
    if resolution not in {"1080p", "4k"}:
        raise VideoExportError("Rezoluție video invalidă. Alege 1080p sau 4k.")
    available_voices = list(score.voice_order or score.part_names.keys())
    if voice != "all" and voice not in score.part_names:
        raise VideoExportError("Voce necunoscută: " + voice)
    if audio_mode not in {"isolated", "accompaniment"}:
        raise VideoExportError("Mod audio invalid. Alege isolated sau accompaniment.")
    if voice == "all":
        audio_mode = "accompaniment"

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
            "breaks": "auto",
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
    _validate_timeline_ids(timemap, element_info, node_by_id)
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
    source_bpm = _source_bpm(score)
    scale_time = source_bpm / float(reference_bpm)
    offset_seconds = int(offset_ms) / 1000.0
    scaled = scaled_tempos(score.tempos, reference_bpm)
    video_duration = seconds_at_q(score.total_quarters, scaled) + 1.25 + max(0, offset_ms) / 1000.0
    if video_duration <= 0 or video_duration > 20 * 60:
        raise VideoExportError("Durata exportabilă trebuie să fie între 0 și 20 de minute.")
    if progress:
        progress(0.10, "Generez pista audio locală")

    if voice == "all" or audio_mode == "accompaniment":
        audio_voices = available_voices
        focus_voice = voice if voice != "all" else None
    else:
        audio_voices = [voice]
        focus_voice = None
    # The sync offset moves the notation/highlights against fixed audio, not the audio itself.
    wav_bytes = render_preview_wav(
        score, reference_bpm, audio_voices, offset_ms=0, focus_voice=focus_voice
    )
    wav_path = output_path.parent / "video-audio.wav"
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

    frames_dir = output_path.parent / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for item in frames_dir.glob("frame_*.png"):
        item.unlink(missing_ok=True)

    # Add style and a few reusable vector overlays once; alter only their coordinates per frame.
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
    # Paint the frame background in the outer SVG's pixel coordinate system.
    # The nested Verovio SVG uses notation coordinates and can letterbox within
    # the 16:9 viewport; placing the backdrop inside it leaves transparent/black
    # corners when the nested SVG's aspect ratio differs from the video frame.
    node_root.insert(1, background)
    line = ET.SubElement(canvas_svg, _svg_tag("line"), {
        "id": "mp4-cursor", "stroke": "#e11d48", "stroke-width": "7",
        "stroke-linecap": "round", "opacity": "1", "pointer-events": "none",
    })

    previous_active: set[str] = set()
    frames_meta: list[tuple[str, float]] = []
    frame_index = 0
    last_cursor_xy: tuple[float, float, int] | None = None
    last_crop_system: int | None = None
    last_crop_x: float | None = None
    for i, state in enumerate(states):
        if progress and i % 10 == 0:
            progress(0.12 + 0.74 * i / max(1, len(states)), f"Randare cadru {i + 1}/{len(states)}")
        state_cursor_ids = [n for n in state.get("cursor_ids", set()) if n in element_info]
        cursor_id = state_cursor_ids[0] if state_cursor_ids else None
        cursor_info = element_info.get(cursor_id) if cursor_id else None
        cursor_index = int(cursor_info.get("system_index", -1)) if cursor_info else -1
        if cursor_index < 0:
            cursor_index = last_cursor_xy[2] if last_cursor_xy else 0
        cursor_index = min(max(0, cursor_index), len(boxes) - 1)
        first_sys, second_sys = _system_pair(cursor_index, boxes)
        content_top = boxes[first_sys]["y"] - boxes[first_sys]["height"] * margin_factor
        content_bottom = boxes[second_sys]["y"] + boxes[second_sys]["height"] * (1.0 + margin_factor)
        natural_height = max(10.0, content_bottom - content_top)
        # Fit the active pair vertically. The score is wider than a 16:9 frame,
        # so the viewport pans horizontally with the music instead of shrinking
        # the notation until four or more systems fit on screen.
        crop_height = natural_height
        crop_width = min(view_w, crop_height * aspect)
        if crop_width < 10.0:
            crop_width = min(view_w, 10.0)
            crop_height = crop_width / aspect
        cursor_anchor = _safe_float(cursor_info.get("x")) + _safe_float(cursor_info.get("width")) / 2.0 if cursor_info else view_x
        # Do not chase the cursor on every note: keep the viewport fixed for a
        # system so the line visibly travels left-to-right. Pan only at the edges.
        current_x = None if cursor_index != last_crop_system else last_crop_x
        crop_x = _camera_crop_x(
            cursor_anchor, boxes[cursor_index]["x"], current_x,
            crop_width, view_x, view_w,
        )
        last_crop_x = crop_x
        last_crop_system = cursor_index
        crop_y = (content_top + content_bottom - crop_height) / 2.0
        crop_y = max(view_y, min(crop_y, max(view_y, view_y + view_h - crop_height)))

        relevant_positions = [
            (element_info[n]["x"] + element_info[n]["width"] / 2.0,
             element_info[n]["y"] + element_info[n]["height"] / 2.0,
             int(element_info[n].get("system_index", cursor_index)))
            for n in state_cursor_ids if n in element_info
        ]
        if relevant_positions:
            xs = sorted(p[0] for p in relevant_positions)
            cursor_x = xs[len(xs) // 2]
            last_cursor_xy = (cursor_x, relevant_positions[0][1], relevant_positions[0][2])
        elif last_cursor_xy:
            cursor_x = last_cursor_xy[0]
        else:
            cursor_x = view_x + view_w / 2

        # Remove any active classes introduced by earlier frames, then apply this state.
        current_active = {
            n for n in state["active"]
            if n in node_by_id
            and element_info.get(n, {}).get("kind") != "rest"
            and (voice == "all" or element_info.get(n, {}).get("voice") == voice)
        }
        cursor_y1, cursor_y2 = _cursor_span_for_system(
            boxes[cursor_index], crop_y, crop_height
        )
        target, previous_active = _render_frame(
            node_root, canvas_svg, node_by_id, element_info, current_active,
            cursor_x, cursor_y1, cursor_y2, crop_x, crop_y, crop_width, crop_height,
            video_width, video_height, frames_dir, frame_index,
            previous_active, line, background, style
        )
        frame_index += 1
        next_time = states[i + 1]["time"] if i + 1 < len(states) else video_duration
        duration = max(0.001, min(video_duration, next_time) - state["time"])
        frames_meta.append((target.name, duration))

    # FFmpeg concat needs the final still repeated to honour the duration of the last event.
    concat_path = frames_dir / "concat.txt"
    lines = []
    for name, duration in frames_meta:
        lines.append("file '" + name + "'")
        lines.append(f"duration {duration:.6f}")
    if frames_meta:
        lines.append("file '" + frames_meta[-1][0] + "'")
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if progress:
        progress(0.90, "Codare H.264 și AAC cu FFmpeg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_path),
        "-i", str(wav_path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", f"scale={video_width}:{video_height}:flags=lanczos,format=yuv420p",
        "-r", "30", "-fps_mode", "cfr",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-af", f"apad=pad_dur={max(0.0, video_duration - wav_duration):.3f}",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{video_duration:.3f}",
    ]
    if output_path.suffix.lower() == ".mp4":
        command += ["-movflags", "+faststart"]
    command.append(str(output_path))
    try:
        result = subprocess.run(command, cwd=frames_dir, check=False,
                                capture_output=True, text=True, timeout=60 * 60)
    except subprocess.TimeoutExpired as exc:
        raise VideoExportError("Codarea video a depășit o oră și a fost oprită.") from exc
    if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        detail = (result.stderr or result.stdout or "FFmpeg nu a returnat un fișier video.").strip()[-1200:]
        raise VideoExportError("Exportul FFmpeg a eșuat: " + detail)

    # Do not leave thousands of temporary PNG frames after a successful export.
    shutil.rmtree(frames_dir, ignore_errors=True)
    try:
        wav_path.unlink(missing_ok=True)
    except OSError:
        pass
    if progress:
        progress(1.0, "Export video gata")
    return {
        "path": str(output_path),
        "bytes": output_path.stat().st_size,
        "duration_seconds": round(video_duration, 2),
        "frames": frame_index,
        "resolution": resolution,
        "format": output_path.suffix.lstrip(".").lower(),
        "voice": voice,
        "audio_mode": audio_mode,
        "renderer": "Verovio",
    }
