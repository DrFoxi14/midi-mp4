import json
import pytest

from app.video import VideoExportError, _read_timeline, _system_pair, video_dependencies


def test_video_timeline_is_sorted_by_source_timestamp():
    data = json.dumps([
        {"tstamp": 1000, "on": ["n2"]},
        {"tstamp": 0, "on": ["n1"]},
        {"tstamp": 500, "off": ["n1"], "on": ["n2"]},
    ])
    result = _read_timeline(data)
    assert [event["tstamp"] for event in result] == [0, 500, 1000]


def test_invalid_video_timeline_is_reported():
    with pytest.raises(VideoExportError):
        _read_timeline("this is not json")


def test_system_window_contains_active_system_and_neighbor():
    systems = [
        {"index": 0, "y": 10, "height": 100},
        {"index": 1, "y": 130, "height": 100},
        {"index": 2, "y": 250, "height": 100},
    ]
    assert _system_pair(0, systems) == (0, 1)
    assert _system_pair(1, systems) == (1, 2)
    assert _system_pair(2, systems) == (1, 2)


def test_short_score_uses_its_only_system():
    assert _system_pair(0, [{"index": 0, "y": 10, "height": 100}]) == (0, 0)


def test_video_cursor_can_move_before_camera_pans():
    from app.video import _camera_crop_x

    crop_start = _camera_crop_x(
        cursor_x=100, system_left=0, current_crop_x=None,
        crop_width=1000, view_x=0, view_width=2000,
    )
    crop_middle = _camera_crop_x(
        cursor_x=500, system_left=0, current_crop_x=crop_start,
        crop_width=1000, view_x=0, view_width=2000,
    )
    crop_near_edge = _camera_crop_x(
        cursor_x=950, system_left=0, current_crop_x=crop_middle,
        crop_width=1000, view_x=0, view_width=2000,
    )

    assert crop_middle == crop_start  # camera stays still while cursor crosses notes
    assert crop_near_edge > crop_middle  # camera only pans when cursor approaches edge


def test_video_cursor_spans_only_the_active_system():
    from app.video import _cursor_span_for_system

    y1, y2 = _cursor_span_for_system(
        {"y": 120, "height": 360},
        crop_y=50,
        crop_height=900,
    )

    assert y1 == 120
    assert y2 == 480
    assert y2 - y1 < 900  # do not draw through the adjacent system


def test_voice_filtered_musicxml_contains_only_complete_selected_part(tmp_path):
    import xml.etree.ElementTree as ET
    from pathlib import Path
    from app.video import _write_voice_filtered_musicxml

    source = Path(__file__).parent / "fixtures" / "satb-tiny.musicxml"
    target = _write_voice_filtered_musicxml(
        source,
        ["Soprano", "Alto", "Tenor", "Bass"],
        "Bass",
        tmp_path / "bass-only.musicxml",
    )
    root = ET.parse(target).getroot()
    parts = [node for node in list(root) if node.tag.split("}")[-1] == "part"]
    part_list = next(node for node in list(root) if node.tag.split("}")[-1] == "part-list")
    part_defs = [node for node in list(part_list) if node.tag.split("}")[-1] == "score-part"]

    assert len(parts) == 1
    assert parts[0].get("id") == "P4"
    assert len(part_defs) == 1
    assert part_defs[0].get("id") == "P4"
    notes = [node for node in parts[0].iter() if node.tag.split("}")[-1] == "note"]
    assert len(notes) == 1
    assert any(node.tag.split("}")[-1] == "pitch" for node in notes[0])

def test_video_dependency_report_is_boolean_and_complete():
    report = video_dependencies()
    assert set(report) == {"ffmpeg", "verovio", "cairosvg", "pillow", "playwright", "chromium"}
    assert all(isinstance(value, bool) for value in report.values())


def test_piano_backend_defaults_to_local_synthesized_preview(monkeypatch):
    import app.core as core
    monkeypatch.delenv("MIDI_MP4_SOUNDFONT", raising=False)
    status = core.piano_backend_info()
    assert status["backend"] == "synthesized"
    assert status["sample_piano_available"] is False


def test_invalid_soundfont_path_is_reported_in_health(monkeypatch):
    import app.core as core
    monkeypatch.setenv("MIDI_MP4_SOUNDFONT", "/definitely/missing/piano.sf2")
    status = core.piano_backend_info()
    assert status["backend"] == "configuration-error"
    assert status["sample_piano_available"] is False


def test_verovio_layout_options_use_supported_footer_and_header_values():
    # The renderer must use toolkit options, not CLI-only noFooter/noHeader flags.
    source = __import__("inspect").getsource(__import__("app.video", fromlist=["render_score_video"]).render_score_video)
    assert '"footer": "none"' in source
    assert '"header": "none"' in source
    assert '"noFooter"' not in source
    assert '"noHeader"' not in source
    # One continuous horizontal strip keeps the measures in order while the
    # camera pans, instead of laying the video out as short rows.
    assert '"breaks": "none"' in source
    assert '"adjustPageWidth": True' in source


def test_svg_geometry_caches_staff_rows_and_recovers_nested_system_wrappers():
    import inspect
    from app import video

    source = inspect.getsource(video._svg_geometry)
    assert "const staffCenterCache = new Map();" in source
    assert "const staffRowsCache = new Map();" in source
    assert "const rootScreenCTMInverse = rootScreenCTM ? rootScreenCTM.inverse() : null;" in source
    assert "systems.findIndex(candidate => candidate.contains(el))" in source


def test_continuous_strip_crop_shows_more_context_than_staff_height():
    from app.video import _continuous_strip_crop_height

    assert _continuous_strip_crop_height(200) == pytest.approx(440)
    assert _continuous_strip_crop_height(500) == pytest.approx(1100)
    assert _continuous_strip_crop_height(0) == pytest.approx(22)


@pytest.mark.parametrize("height", [-100, float("nan"), float("inf"), -float("inf")])
def test_continuous_strip_crop_handles_invalid_svg_heights(height):
    from app.video import _continuous_strip_crop_height

    assert _continuous_strip_crop_height(height) == pytest.approx(22)


@pytest.mark.parametrize(
    "cursor_x, current_x",
    [
        (-100, None), (0, None), (100, 0), (500, 0), (880, 0),
        (950, 0), (1500, 300), (1999, 1000), (5000, 1600),
    ],
)
def test_camera_crop_remains_inside_continuous_strip(cursor_x, current_x):
    from app.video import _camera_crop_x

    result = _camera_crop_x(cursor_x, 0, current_x, 1000, 0, 2000)
    assert 0 <= result <= 1000


def test_staff_layout_validator_accepts_expected_voice_and_satb_staff_counts():
    from app.video import _validate_staff_mapping

    _validate_staff_mapping([{"staff_count": 4}, {"staff_count": 4}], 4, "all")
    _validate_staff_mapping([{"staff_count": 1}], 1, "Bass")


@pytest.mark.parametrize(
    "systems, expected, voice, match",
    [
        ([{"staff_count": 3}], 4, "all", "Maparea SATB"),
        ([{"staff_count": 2}], 1, "Bass", "un singur portativ"),
        ([{"staff_count": 0}], 1, "Alto", "un singur portativ"),
    ],
)
def test_staff_layout_validator_rejects_ambiguous_notation(systems, expected, voice, match):
    from app.video import _validate_staff_mapping

    with pytest.raises(VideoExportError, match=match):
        _validate_staff_mapping(systems, expected, voice)


def test_camera_crop_stays_bounded_for_hundreds_of_cursor_positions():
    from app.video import _camera_crop_x

    current_x = None
    previous_x = 0
    for cursor_x in range(-250, 12001, 25):
        current_x = _camera_crop_x(
            cursor_x, system_left=0, current_crop_x=current_x,
            crop_width=1000, view_x=0, view_width=1800,
        )
        assert 0 <= current_x <= 800
        assert current_x >= previous_x
        previous_x = current_x


def test_cursor_interpolation_matches_precomputed_keyframe_times():
    from app.video import _cursor_position_at

    keyframes = [
        {"time": 0.0, "x": 100.0, "system_index": 0},
        {"time": 0.75, "x": 250.0, "system_index": 0},
        {"time": 2.0, "x": 900.0, "system_index": 0},
        {"time": 3.5, "x": 1200.0, "system_index": 0},
    ]
    times = [point["time"] for point in keyframes]
    for tick in range(-5, 400):
        at = tick / 100
        assert _cursor_position_at(keyframes, at, times) == _cursor_position_at(keyframes, at)

def test_cursor_precomputed_times_mismatch_is_reported():
    from app.video import _cursor_position_at

    keyframes = [{"time": 0.0, "x": 10.0, "system_index": 0}]
    with pytest.raises(VideoExportError, match="nu corespunde"):
        _cursor_position_at(keyframes, 0.0, [])


@pytest.mark.parametrize(
    "bpm, resolution, suffix, offset, expected_match",
    [
        (29.9, "1080p", ".mp4", 0, "30 și 240 BPM"),
        (240.1, "1080p", ".mp4", 0, "30 și 240 BPM"),
        (float("nan"), "1080p", ".mp4", 0, "30 și 240 BPM"),
        (float("inf"), "1080p", ".mp4", 0, "30 și 240 BPM"),
        ("not-a-number", "1080p", ".mp4", 0, "30 și 240 BPM"),
        (100, "720p", ".mp4", 0, "Rezoluție video invalidă"),
        (100, "1080p", ".webm", 0, "Format video invalid"),
        (100, "1080p", ".mp4", -5001, "-5000 și 5000 ms"),
        (100, "1080p", ".mp4", 5001, "-5000 și 5000 ms"),
        (100, "1080p", ".mp4", float("nan"), "-5000 și 5000 ms"),
        (100, "1080p", ".mp4", 1.5, "număr întreg"),
    ],
)
def test_video_timing_preflight_handles_tempo_changes_and_positive_offset():
    from app.core import ScoreData, NoteEvent, TempoEvent
    from app.video import _prepare_video_timing

    score = ScoreData(
        "tempo-change",
        {"Soprano": "Soprano"},
        [NoteEvent("Soprano", "Soprano", 72, 0.0, 8.0)],
        [TempoEvent(0, 60), TempoEvent(4, 120)],
        8.0,
        "",
    )
    source_bpm, scaled, duration = _prepare_video_timing(score, 100, 1000)
    assert source_bpm == pytest.approx(60)
    assert [tempo.bpm for tempo in scaled] == pytest.approx([100, 200])
    assert duration == pytest.approx(5.85)


@pytest.mark.parametrize(
    "total_quarters, tempos, message",
    [
        (0, [], "durată muzicală"),
        (-1, [], "durată muzicală"),
        (float("nan"), [], "durată muzicală"),
        (float("inf"), [], "durată muzicală"),
        (8, [__import__("app.core", fromlist=["TempoEvent"]).TempoEvent(0, 0)], "BPM invalid"),
        (8, [__import__("app.core", fromlist=["TempoEvent"]).TempoEvent(0, -50)], "BPM invalid"),
        (8, [__import__("app.core", fromlist=["TempoEvent"]).TempoEvent(0, float("nan"))], "BPM invalid"),
        (8, [__import__("app.core", fromlist=["TempoEvent"]).TempoEvent(float("nan"), 100)], "poziție nenumerică"),
    ],
)
def test_video_timing_preflight_rejects_malformed_score_metadata(
    total_quarters, tempos, message,
):
    from app.core import ScoreData
    from app.video import _prepare_video_timing

    score = ScoreData("invalid", {"Soprano": "Soprano"}, [], tempos, total_quarters, "")
    with pytest.raises(VideoExportError, match=message):
        _prepare_video_timing(score, 100, 0)


def test_video_timing_preflight_rejects_duration_above_twenty_minutes():
    from app.core import ScoreData, TempoEvent
    from app.video import _prepare_video_timing

    score = ScoreData(
        "too-long",
        {"Soprano": "Soprano"},
        [],
        [TempoEvent(0, 30)],
        1000.0,
        "",
    )
    with pytest.raises(VideoExportError, match="maximum 20 de minute"):
        _prepare_video_timing(score, 30, 0)


def test_video_export_options_reject_invalid_edge_cases(
    bpm, resolution, suffix, offset, expected_match,
):
    from app.video import _validate_video_export_options

    with pytest.raises(VideoExportError, match=expected_match):
        _validate_video_export_options(bpm, resolution, suffix, offset)


@pytest.mark.parametrize(
    "bpm, resolution, suffix, offset",
    [
        (30, "1080p", ".mp4", -5000),
        (100, "1080p", ".MP4", 0),
        (240, "4k", ".mov", 5000),
    ],
)
def test_video_export_options_accept_supported_boundaries(bpm, resolution, suffix, offset):
    from app.video import _validate_video_export_options

    result_bpm, result_offset = _validate_video_export_options(
        bpm, resolution, suffix, offset,
    )
    assert result_bpm == float(bpm)
    assert result_offset == offset


def test_video_workdir_keeps_output_suffix_for_atomic_staging(tmp_path):
    from app.video import _create_video_workdir

    destination = tmp_path / "final-cut.mov"
    handle, workdir = _create_video_workdir(destination)
    try:
        staged = workdir / destination.name
        assert staged.suffix == destination.suffix
        assert staged != destination
        assert staged.parent == workdir
        assert destination.parent == workdir.parent
    finally:
        handle.cleanup()


def test_video_exports_get_isolated_temp_directories(tmp_path):
    from app.video import _create_video_workdir

    first_handle, first = _create_video_workdir(tmp_path / "first.mp4")
    second_handle, second = _create_video_workdir(tmp_path / "second.mp4")
    try:
        assert first != second
        assert first.parent == tmp_path
        assert second.parent == tmp_path
        assert (first / "frames") != (second / "frames")
        assert (first / "video-audio.wav") != (second / "video-audio.wav")
        assert first.exists() and second.exists()
    finally:
        first_handle.cleanup()
        second_handle.cleanup()


def test_background_signature_reuses_visually_identical_states():
    from app.video import _background_signature

    base = {
        "active_ids": {"n2", "n1"},
        "crop_x": 100,
        "crop_y": -20,
        "crop_width": 900,
        "crop_height": 500,
        "time": 1.0,
    }
    same_picture = {**base, "time": 2.0, "active_ids": {"n1", "n2"}}
    changed_note = {**base, "active_ids": {"n1"}}
    changed_camera = {**base, "crop_x": 101}

    assert _background_signature(base) == _background_signature(same_picture)
    assert _background_signature(base) != _background_signature(changed_note)
    assert _background_signature(base) != _background_signature(changed_camera)


def test_mov_stream_command_uses_container_specific_flags(tmp_path):
    from app.video import _build_ffmpeg_stream_command

    command = _build_ffmpeg_stream_command(
        "/usr/bin/ffmpeg", tmp_path / "audio.wav", tmp_path / "video.mov",
        3840, 2160, 30, 600.0, 620.0,
    )
    assert command[command.index("-video_size") + 1] == "3840x2160"
    assert command[command.index("-framerate") + 1] == "30"
    assert "-movflags" not in command
    assert "pad_dur=0.000" in command[command.index("-af") + 1]


def test_ffmpeg_command_streams_raw_frames_without_concat_png_manifest(tmp_path):
    from app.video import _build_ffmpeg_stream_command

    command = _build_ffmpeg_stream_command(
        "/usr/bin/ffmpeg",
        tmp_path / "audio.wav",
        tmp_path / "video.mp4",
        1920, 1080, 30, 12.5, 11.0,
    )
    assert command[command.index("-f") + 1] == "rawvideo"
    assert "pipe:0" in command
    assert "rgb24" in command
    assert "concat" not in command
    assert "frame_000001.png" not in command
    assert command[-1].endswith("video.mp4")
    assert command[command.index("-movflags") + 1] == "+faststart"


def test_timemap_quarter_position_maps_using_scaled_tempo_map():
    from app.video import _timemap_event_seconds
    from app.core import TempoEvent
    tempos = [TempoEvent(0, 80), TempoEvent(4, 120)]
    assert _timemap_event_seconds({"qstamp": 4, "tstamp": 3000}, tempos, 0, 100, 80) == pytest.approx(3.0)
    assert _timemap_event_seconds({"qstamp": 8, "tstamp": 6000}, tempos, 0, 100, 80) == pytest.approx(5.0)


def test_timemap_falls_back_to_real_timestamp_when_qstamp_missing():
    from app.video import _timemap_event_seconds
    from app.core import TempoEvent
    assert _timemap_event_seconds({"tstamp": 2000}, [TempoEvent(0, 80)], 0.25, 100, 80) == pytest.approx(2.75)


@pytest.mark.parametrize(
    "voice, extension, offset_ms, audio_mode",
    [
        ("all", "mp4", 0, "accompaniment"),
        ("Bass", "mov", 500, "accompaniment"),
        ("Soprano", "mp4", -500, "isolated"),
    ],
)
def test_real_video_export_encodes_synchronized_video(
    tmp_path, voice, extension, offset_ms, audio_mode,
):
    import math
    import shutil
    import subprocess
    from pathlib import Path

    from app.core import parse_score
    from app.video import render_score_video

    dependencies = video_dependencies()
    if not all(dependencies.values()):
        pytest.skip("Local video toolchain is incomplete; CI installs Chromium and FFmpeg.")

    fixture = Path(__file__).parent / "fixtures" / "satb-tiny.musicxml"
    score = parse_score(fixture, tmp_path / "parse-work")
    output = tmp_path / f"tiny-{voice}.{extension}"

    result = render_score_video(
        score,
        fixture,
        output,
        reference_bpm=100,
        resolution="1080p",
        voice=voice,
        audio_mode=audio_mode,
        offset_ms=offset_ms,
    )

    assert output.is_file()
    assert output.stat().st_size > 10_000
    assert result["path"] == str(output)
    assert result["frames"] == math.ceil(result["duration_seconds"] * 30)
    assert result["resolution"] == "1080p"
    assert result["format"] == extension

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        probe = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(output),
            ],
            check=True, capture_output=True, text=True, timeout=15,
        )
        encoded_duration = float(probe.stdout.strip())
        assert encoded_duration == pytest.approx(result["duration_seconds"], abs=0.15)


def test_long_continuous_satb_score_keeps_all_measures_on_one_canvas(tmp_path):
    import copy
    import xml.etree.ElementTree as ET
    from pathlib import Path

    import verovio
    from app.video import _read_timeline, _svg_geometry, _validate_staff_mapping, _validate_timeline_ids

    fixture = Path(__file__).parent / "fixtures" / "satb-tiny.musicxml"
    tree = ET.parse(fixture)
    root = tree.getroot()
    parts = root.findall("part")
    assert len(parts) == 4

    # Expand the real SATB fixture to 100 measures (400 notes). Repeated whole
    # notes deliberately keep the fixture simple while stressing SVG width/layout.
    for part in parts:
        original = part.find("measure")
        assert original is not None
        for number in range(2, 101):
            measure = copy.deepcopy(original)
            measure.set("number", str(number))
            part.append(measure)

    long_score = tmp_path / "satb-100-measures.musicxml"
    tree.write(long_score, encoding="utf-8", xml_declaration=True)

    toolkit = verovio.toolkit()
    options = {
        "inputFrom": "xml",
        "pageWidth": 2100,
        "pageHeight": 60000,
        "adjustPageHeight": True,
        "breaks": "none",
        "adjustPageWidth": True,
        "scale": 50,
        "footer": "none",
        "header": "none",
        "spacingLinear": 0.25,
        "spacingNonLinear": 0.6,
    }
    assert toolkit.setOptions(options)
    assert toolkit.loadFile(str(long_score)), toolkit.getLog()
    assert toolkit.getPageCount() == 1, toolkit.getLog()

    svg_text = toolkit.renderToSVG(1)
    assert svg_text and "<svg" in svg_text
    geometry = _svg_geometry(svg_text, ["Soprano", "Alto", "Tenor", "Bass"])
    systems = geometry.get("systems") or []
    _validate_staff_mapping(systems, 4, "all")
    assert systems
    assert all(system["staff_count"] == 4 for system in systems)
    assert len(geometry.get("elements") or []) >= 390

    timemap = _read_timeline(toolkit.renderToTimemap({"includeRests": True}))
    root_svg = ET.fromstring(svg_text.encode("utf-8"))
    svg_nodes = {node.get("id"): node for node in root_svg.iter() if node.get("id")}
    element_info = {item["id"]: item for item in geometry["elements"] if item.get("id")}
    timed_ids = _validate_timeline_ids(timemap, element_info, svg_nodes)
    assert len(timed_ids) >= 390


def test_video_export_rejects_unmapped_timed_notes():
    from app.video import _validate_timeline_ids
    events = [{"tstamp": 0, "on": ["note-1"], "off": ["note-1"]}]
    geometry = {"note-1": {"x": 5, "y": 8}}
    svg_nodes = {"note-1": object()}
    assert _validate_timeline_ids(events, geometry, svg_nodes) == {"note-1"}

    with pytest.raises(VideoExportError, match="sincroniza în siguranță"):
        _validate_timeline_ids(events, {}, {})


def test_video_export_rejects_empty_note_timeline():
    from app.video import _validate_timeline_ids
    with pytest.raises(VideoExportError, match="nicio notă animabilă"):
        _validate_timeline_ids([{"tstamp": 0}], {}, {})


def test_runtime_soundfont_configuration_precedes_shell_environment(tmp_path, monkeypatch):
    import app.core as core
    soundfont = tmp_path / "local-piano.sf2"
    soundfont.write_bytes(b"test-local-soundfont")
    monkeypatch.delenv("MIDI_MP4_SOUNDFONT", raising=False)
    monkeypatch.setattr(core.shutil, "which", lambda command: "/usr/bin/fluidsynth")
    core.set_runtime_soundfont(str(soundfont))
    try:
        status = core.piano_backend_info()
        assert status["backend"] == "soundfont"
        assert status["sample_piano_available"] is True
        assert status["soundfont_name"] == "local-piano.sf2"
    finally:
        core.set_runtime_soundfont(None)


def test_runtime_soundfont_can_be_cleared(tmp_path, monkeypatch):
    import app.core as core
    soundfont = tmp_path / "local-piano.sf2"
    soundfont.write_bytes(b"test-local-soundfont")
    monkeypatch.delenv("MIDI_MP4_SOUNDFONT", raising=False)
    core.set_runtime_soundfont(str(soundfont))
    core.set_runtime_soundfont(None)
    assert core.piano_backend_info()["backend"] == "synthesized"

def test_cursor_keyframes_use_note_onsets_not_note_offs():
    from app.video import _build_cursor_keyframes

    grouped = {
        0.0: {"on": {"n1"}, "off": set()},
        0.4: {"on": set(), "off": {"n1"}},
        1.0: {"on": {"n2"}, "off": set()},
    }
    geometry = {
        "n1": {"x": 10, "width": 10, "system_index": 0, "voice": "Bass"},
        "n2": {"x": 90, "width": 10, "system_index": 0, "voice": "Bass"},
    }

    result = _build_cursor_keyframes(grouped, geometry, "Bass")

    assert [frame["time"] for frame in result] == [0.0, 1.0]
    assert [frame["x"] for frame in result] == [15.0, 95.0]


def test_cursor_interpolation_is_continuous_and_does_not_overshoot():
    from app.video import _cursor_position_at

    keyframes = [
        {"time": 0.0, "x": 100.0, "system_index": 0},
        {"time": 1.0, "x": 300.0, "system_index": 0},
        {"time": 2.0, "x": 420.0, "system_index": 0},
        {"time": 3.0, "x": 600.0, "system_index": 0},
    ]
    positions = [_cursor_position_at(keyframes, i / 30)["x"] for i in range(91)]

    assert positions[0] == pytest.approx(100.0)
    assert positions[30] == pytest.approx(300.0)
    assert positions[-1] == pytest.approx(600.0)
    assert all(a <= b for a, b in zip(positions, positions[1:])), (
        "Cursor should move forward continuously through a system"
    )
    assert min(positions) >= 100.0
    assert max(positions) <= 600.0


def test_cursor_does_not_sweep_backwards_across_a_system_break():
    from app.video import _cursor_position_at

    keyframes = [
        {"time": 0.0, "x": 1800.0, "system_index": 0},
        {"time": 1.0, "x": 100.0, "system_index": 1},
    ]

    before_break = _cursor_position_at(keyframes, 0.99)
    at_break = _cursor_position_at(keyframes, 1.0)

    assert before_break == {"x": 1800.0, "system_index": 0}
    assert at_break == {"x": 100.0, "system_index": 1}

