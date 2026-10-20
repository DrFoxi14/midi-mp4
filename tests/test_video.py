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
