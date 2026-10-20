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


def test_multi_system_satb_score_renders_cropped_mp4(tmp_path: Path):
    deps = video_dependencies()
    missing = [name for name, available in deps.items() if not available]
    if missing:
        pytest.skip("Video toolchain not installed: " + ", ".join(missing))

    fixture = _make_multi_system_fixture(tmp_path)
    score = parse_score(fixture, tmp_path / "parsed")
    output = tmp_path / "multi-system.mp4"

    result = render_score_video(
        score,
        Path(score.musicxml_path),
        output,
        reference_bpm=240,
        resolution="1080p",
        voice="all",
        audio_mode="accompaniment",
        offset_ms=0,
    )

    assert output.is_file()
    assert output.stat().st_size > 10_000
    assert result["bytes"] == output.stat().st_size
    assert result["format"] == "mp4"
    assert result["resolution"] == "1080p"
    assert result["frames"] >= 2

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    first_frame = tmp_path / "first-frame.png"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(output),
         "-frames:v", "1", str(first_frame)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    # Temporary CI diagnostic: preserve the generated frame for visual inspection
    # when this integration test fails on an actual renderer.
    shutil.copy2(first_frame, Path.cwd() / "debug-first-frame.png")
    with Image.open(first_frame) as image:
        rgb = np.asarray(image.convert("RGB"), dtype=np.int16)
        assert image.size == (1920, 1080)
        # The parchment background must fill the whole 16:9 canvas. Black corners
        # indicate the crop was applied to the outer page SVG instead of the inner
        # definition-scale notation SVG (the broken behavior reported by the user).
        expected = np.array([248, 245, 236], dtype=np.int16)
        for x, y in ((0, 0), (1919, 0), (0, 1079), (1919, 1079)):
            assert np.max(np.abs(rgb[y, x] - expected)) <= 18, (
                f"Video corner {(x, y)} is not the score background: {rgb[y, x].tolist()}"
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
