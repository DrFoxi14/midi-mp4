from pathlib import Path

import pytest

from app.openutau import launch_project


def _project(tmp_path: Path) -> Path:
    path = tmp_path / "test-project.ustx"
    path.write_text("{}", encoding="utf-8")
    return path


def test_launch_project_uses_macos_open_and_selected_application(tmp_path):
    path = _project(tmp_path)
    calls = []
    result = launch_project(
        path,
        system="darwin",
        app_path=Path("/Applications/OpenUtau.app"),
        which=lambda name: "/usr/bin/open" if name == "open" else None,
        popen=lambda command, **kwargs: calls.append((command, kwargs)),
    )

    assert result["launched"] is True
    assert calls[0][0] == [
        "/usr/bin/open", "-a", "/Applications/OpenUtau.app", str(path.resolve())
    ]
    assert calls[0][1]["start_new_session"] is True


def test_launch_project_uses_macos_file_association_when_app_path_is_unknown(tmp_path):
    path = _project(tmp_path)
    calls = []
    result = launch_project(
        path,
        system="darwin",
        which=lambda name: "/usr/bin/open" if name == "open" else None,
        popen=lambda command, **kwargs: calls.append(command),
    )

    assert result["launched"] is True
    assert calls == [["/usr/bin/open", str(path.resolve())]]


def test_launch_project_reports_missing_macos_opener(tmp_path):
    result = launch_project(
        _project(tmp_path),
        system="darwin",
        which=lambda _name: None,
    )

    assert result["launched"] is False
    assert "open" in result["reason"]


def test_launch_project_uses_xdg_open_on_linux(tmp_path):
    path = _project(tmp_path)
    calls = []
    result = launch_project(
        path,
        system="linux",
        which=lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None,
        popen=lambda command, **kwargs: calls.append(command),
    )

    assert result["launched"] is True
    assert calls == [["/usr/bin/xdg-open", str(path.resolve())]]


def test_launch_project_uses_startfile_on_windows(tmp_path):
    path = _project(tmp_path)
    calls = []
    result = launch_project(
        path,
        system="win32",
        startfile=lambda value: calls.append(value),
    )

    assert result["launched"] is True
    assert calls == [str(path.resolve())]


def test_launch_project_catches_os_launch_failure(tmp_path):
    def fail(*_args, **_kwargs):
        raise OSError("app launch failed")

    result = launch_project(
        _project(tmp_path),
        system="darwin",
        which=lambda name: "/usr/bin/open" if name == "open" else None,
        popen=fail,
    )

    assert result["launched"] is False
    assert "app launch failed" in result["reason"]


@pytest.mark.parametrize(
    "filename, contents",
    [("not-project.txt", "{}"), ("empty.ustx", "")],
)
def test_launch_project_rejects_non_ustx_or_empty_file(tmp_path, filename, contents):
    path = tmp_path / filename
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError, match="USTX existent și nevid"):
        launch_project(path, system="darwin", which=lambda _name: "/usr/bin/open")
