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
