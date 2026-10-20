"""End-to-end smoke test for the real score-to-MP4 pipeline.

When the full video toolchain is installed, render a tiny real MusicXML score and
verify that FFmpeg produces a non-empty video. On a developer machine without
the optional video dependencies, the test skips with a clear reason.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core import parse_score
from app.video import render_score_video, video_dependencies


def test_tiny_satb_score_renders_mp4_end_to_end(tmp_path: Path):
    deps = video_dependencies()
    missing = [name for name, available in deps.items() if not available]
    if missing:
        pytest.skip("Video toolchain not installed: " + ", ".join(missing))

    fixture = Path(__file__).parent / "fixtures" / "satb-tiny.musicxml"
    score = parse_score(fixture, tmp_path / "parsed")
    output = tmp_path / "tiny-satb.mp4"

    result = render_score_video(
        score,
        Path(score.musicxml_path),
        output,
        reference_bpm=60,
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
    assert result["frames"] >= 1

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
