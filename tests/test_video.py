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
    assert set(report) == {"ffmpeg", "verovio", "cairosvg", "pillow", "playwright"}
    assert all(isinstance(value, bool) for value in report.values())
