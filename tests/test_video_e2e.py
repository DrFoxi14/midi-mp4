"""End-to-end rendering test with enough measures to force multiple systems.

The test verifies not just that an MP4 exists, but that the score is cropped into
the 16:9 canvas rather than spilling outside a nested Verovio SVG viewport.
"""
import copy
import shutil
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image
import pytest

from app.core import parse_score
from app.video import render_score_video, video_dependencies


def _make_multi_system_fixture(tmp_path: Path) -> Path:
    source = Path(__file__).parent / "fixtures" / "satb-tiny.musicxml"
    root = ET.parse(source).getroot()

    # Repeat the four-part whole-note bar enough times to force Verovio to engrave
    # several systems while keeping the audio/rendering workload small.
    for part in root.findall("./part"):
        original = part.find("measure")
        if original is None:
            raise AssertionError("Fixture is missing its first measure.")
        for existing in list(part.findall("measure")):
            part.remove(existing)
        for number in range(1, 25):
            measure = copy.deepcopy(original)
            measure.set("number", str(number))
            part.append(measure)

    destination = tmp_path / "satb-multiple-systems.musicxml"
    destination.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    return destination


def test_selected_voice_is_engraved_intact_and_cursor_stays_in_one_system(tmp_path: Path):
    deps = video_dependencies()
    missing = [name for name in ("verovio", "playwright", "chromium") if not deps[name]]
    if missing:
        pytest.skip("Selected-voice engraving dependencies missing: " + ", ".join(missing))

    import verovio
    from app.video import (
        _cursor_span_for_system, _read_timeline, _svg_geometry,
        _write_voice_filtered_musicxml,
    )

    fixture = _make_multi_system_fixture(tmp_path)
    score = parse_score(fixture, tmp_path / "selected-parsed")
    voice_order = list(score.voice_order or score.part_names.keys())
    selected_xml = _write_voice_filtered_musicxml(
        Path(score.musicxml_path),
        voice_order,
        "Bass",
        tmp_path / "bass-filtered.musicxml",
    )

    toolkit = verovio.toolkit()
    assert toolkit.setOptions({
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
    })
    assert toolkit.loadFile(str(selected_xml)), toolkit.getLog()
    timemap = _read_timeline(toolkit.renderToTimemap({"includeRests": True}))
    selected_note_ids = {
        str(note_id)
        for event in timemap
        for note_id in event.get("on", []) or []
    }
    # The fixture has one Bass note in each of its 24 measures. No other part's
    # note IDs should survive; this tests filtering before notation is engraved.
    assert len(selected_note_ids) == 24

    svg_text = toolkit.renderToSVG(1)
    svg_root = ET.fromstring(svg_text.encode("utf-8"))
    nodes_by_id = {node.get("id"): node for node in svg_root.iter() if node.get("id")}
    for note_id in selected_note_ids:
        note_node = nodes_by_id.get(note_id)
        assert note_node is not None, f"Missing rendered note node {note_id}"
        descendants = list(note_node.iter())
        assert any(node.tag.split("}")[-1] in {"path", "ellipse", "use", "polygon"} for node in descendants), (
            f"Rendered note {note_id} has no notehead/glyph descendants"
        )

    geometry = _svg_geometry(svg_text, voice_order)
    systems = geometry["systems"]
    assert len(systems) >= 2, "Fixture did not produce multiple systems"
    assert all(system["staff_count"] == 1 for system in systems), (
        "An individual-voice video should engrave one staff per system; "
        f"got {[system['staff_count'] for system in systems]}"
    )
    note_geometry = [item for item in geometry["elements"] if item["kind"] != "rest"]
    assert len(note_geometry) == 24
    assert all(item["staff_rank"] == 0 for item in note_geometry), (
        "All rendered note events in the Bass-only score should map to its single staff"
    )

    first = systems[0]
    second = systems[1]
    y1, y2 = _cursor_span_for_system(
        first,
        crop_y=first["y"],
        crop_height=(second["y"] + second["height"] - first["y"]),
    )
    assert y1 == pytest.approx(first["y"])
    assert y2 == pytest.approx(first["y"] + first["height"])
    assert y2 < second["y"], "Cursor should not span into the next four-staff system"


def test_full_satb_cursor_span_is_exactly_one_four_staff_system(tmp_path: Path):
    deps = video_dependencies()
    missing = [name for name in ("verovio", "playwright", "chromium") if not deps[name]]
    if missing:
        pytest.skip("Full SATB cursor geometry dependencies missing: " + ", ".join(missing))

    import verovio
    from app.video import _cursor_span_for_system, _svg_geometry

    fixture = _make_multi_system_fixture(tmp_path)
    score = parse_score(fixture, tmp_path / "full-satb-parsed")
    voice_order = list(score.voice_order or score.part_names.keys())
    toolkit = verovio.toolkit()
    assert toolkit.setOptions({
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
    })
    assert toolkit.loadFile(score.musicxml_path), toolkit.getLog()

    geometry = _svg_geometry(toolkit.renderToSVG(1), voice_order)
    systems = geometry["systems"]
    assert len(systems) >= 2
    assert all(system["staff_count"] == 4 for system in systems), (
        "A full SATB system should map to four unique staff rows; "
        f"counts/centers={[ (system['staff_count'], system.get('staff_centers'), system['y'], system['height']) for system in systems ]}"
    )
    note_ranks = {
        item["staff_rank"] for item in geometry["elements"]
        if item["kind"] != "rest"
    }
    assert note_ranks == {0, 1, 2, 3}, f"Expected all four staff ranks, got {note_ranks}"

    first, second = systems[0], systems[1]
    y1, y2 = _cursor_span_for_system(
        first,
        crop_y=first["y"],
        crop_height=second["y"] + second["height"] - first["y"],
    )
    assert y1 == pytest.approx(first["y"])
    assert y2 == pytest.approx(first["y"] + first["height"])
    assert y2 < second["y"], (
        "The full-score cursor should cross the active four-staff system only, "
        "not the next system too"
    )


def test_multi_system_satb_score_renders_cropped_mp4(tmp_path: Path, monkeypatch):
    deps = video_dependencies()
    missing = [name for name, available in deps.items() if not available]
    if missing:
        pytest.skip("Video toolchain not installed: " + ", ".join(missing))

    import io
    import json
    import math
    import struct
    import wave
    import app.video as video_module
    import app.vocals as vocals

    fixture = _make_multi_system_fixture(tmp_path)
    score = parse_score(fixture, tmp_path / "parsed")
    output = tmp_path / "multi-system.mp4"

    # Provide one externally rendered voice stem and leave the other three parts
    # on the local synth fallback. This verifies the *real* MusicXML -> WAV mix ->
    # moving-score MP4 pipeline, not just the synthetic-only video path.
    stems_dir = tmp_path / "vocal-stems"
    stems_dir.mkdir()
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as singer_wav:
        singer_wav.setnchannels(1)
        singer_wav.setsampwidth(2)
        singer_wav.setframerate(22050)
        sample_count = int(0.8 * 22050)
        samples = b"".join(
            struct.pack("<h", int(5000 * math.sin(2 * math.pi * 440 * index / 22050)))
            for index in range(sample_count)
        )
        singer_wav.writeframes(samples)
    stem_path = vocals.vocal_stem_path(stems_dir, "Soprano")
    stem_path.write_bytes(wav_buffer.getvalue())
    vocals.vocal_stem_metadata_path(stems_dir, "Soprano").write_text(
        json.dumps({"voice": "Soprano", "reference_bpm": 240, "duration_seconds": 0.8}),
        encoding="utf-8",
    )

    original_mix = video_module.mix_vocal_audio
    observed = {}

    def capture_vocal_mix(*args, **kwargs):
        observed["stems_dir"] = args[1]
        observed["voices"] = list(args[2])
        observed["normalize_duration"] = kwargs.get("normalize_duration", False)
        mixed = original_mix(*args, **kwargs)
        observed["imported_stem_used"] = mixed is not None
        return mixed

    monkeypatch.setattr(video_module, "mix_vocal_audio", capture_vocal_mix)
    result = render_score_video(
        score,
        Path(score.musicxml_path),
        output,
        reference_bpm=240,
        resolution="1080p",
        voice="all",
        audio_mode="accompaniment",
        offset_ms=0,
        vocal_stems_dir=stems_dir,
    )
    assert observed["stems_dir"] == stems_dir
    assert observed["voices"] == list(score.voice_order or score.part_names.keys())
    assert observed["normalize_duration"] is True
    assert observed["imported_stem_used"] is True

    assert output.is_file()
    assert output.stat().st_size > 10_000
    assert result["bytes"] == output.stat().st_size
    assert result["format"] == "mp4"
    assert result["resolution"] == "1080p"
    assert result["frames"] >= int(result["duration_seconds"] * 29), (
        "Cursor animation should export at approximately 30 FPS, not only one frame per note event."
    )

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    first_frame = tmp_path / "first-frame.png"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(output),
         "-frames:v", "1", str(first_frame)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    with Image.open(first_frame) as image:
        rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
        assert image.size == (1920, 1080)
        # The parchment background must fill the canvas. Sample just inside the
        # four corners: the exact outermost pixel row/column can be antialiased by
        # SVG rasterization, even when the score and opaque background are correctly
        # cropped. A real black/letterboxed corner region would extend into this inset.
        expected = np.array([248, 245, 236], dtype=np.int16)
        margin = 8
        corner_samples = (
            (margin, margin),
            (image.width - 1 - margin, margin),
            (margin, image.height - 1 - margin),
            (image.width - 1 - margin, image.height - 1 - margin),
        )
        for x, y in corner_samples:
            assert np.max(np.abs(rgb[y, x] - expected)) <= 18, (
                f"Video corner area {(x, y)} is not the score background: {rgb[y, x].tolist()}"
            )
        difference = np.max(np.abs(rgb - expected), axis=2)
        ys, xs = np.where(difference > 28)
        assert len(xs) > 500, "The video frame contains no visible score notation."
        assert int(xs.max() - xs.min()) > image.width * 0.35, (
            "Notation occupies only a narrow strip of the video frame."
        )

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        probe = subprocess.run(
            [
                ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(output),
            ],
            capture_output=True, text=True, check=True, timeout=30,
        )
        assert float(probe.stdout.strip()) > 0


def test_selected_voice_export_has_complete_visible_notation_cursor_and_lead_color(tmp_path: Path):
    deps = video_dependencies()
    missing = [name for name, available in deps.items() if not available]
    if missing:
        pytest.skip("Video toolchain not installed: " + ", ".join(missing))

    fixture = _make_multi_system_fixture(tmp_path)
    score = parse_score(fixture, tmp_path / "bass-parsed")
    output = tmp_path / "bass-only.mp4"

    result = render_score_video(
        score,
        Path(score.musicxml_path),
        output,
        reference_bpm=240,
        resolution="1080p",
        voice="Bass",
        audio_mode="accompaniment",
        offset_ms=0,
    )

    assert output.is_file() and output.stat().st_size > 10_000
    assert result["voice"] == "Bass"
    assert result["audio_mode"] == "accompaniment"
    assert result["frames"] >= 2

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    first_frame = tmp_path / "bass-first-frame.png"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", "0.1",
         "-i", str(output), "-frames:v", "1", str(first_frame)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    with Image.open(first_frame) as image:
        rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
        assert image.size == (1920, 1080)
        expected = np.array([248, 245, 236], dtype=np.int16)
        difference = np.max(np.abs(rgb - expected), axis=2)
        ys, xs = np.where(difference > 28)
        assert len(xs) > 300, "Bass-only export has no readable notation in its frame."
        assert int(xs.max() - xs.min()) > image.width * 0.08, (
            "Bass-only notation collapsed into a narrow strip."
        )

        # The first active Bass note is green and the moving cursor is red.
        green_note_pixels = (
            (rgb[:, :, 1] > rgb[:, :, 0] * 1.15)
            & (rgb[:, :, 1] > rgb[:, :, 2] * 1.10)
            & (rgb[:, :, 0] < 180)
            & (rgb[:, :, 1] < 235)
            & (rgb[:, :, 2] < 180)
        )
        red_cursor_pixels = (
            (rgb[:, :, 0] > 150)
            & (rgb[:, :, 1] < 100)
            & (rgb[:, :, 2] < 150)
        )
        assert int(green_note_pixels.sum()) > 20, (
            "The active Bass note is not visibly highlighted in the encoded video."
        )
        assert int(red_cursor_pixels.sum()) > 20, (
            "The moving cursor is not visible in the encoded video."
        )
