"""Real-browser smoke test for import, local library, audio export and restore."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest

from app.video import video_dependencies


ROOT = Path(__file__).resolve().parents[1]


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_browser_can_import_export_restore_and_delete_local_score(tmp_path: Path):
    deps = video_dependencies()
    if not deps.get("playwright") or not deps.get("chromium"):
        pytest.skip("Playwright Chromium is not installed locally.")

    from playwright.sync_api import sync_playwright

    port = _free_local_port()
    data_root = tmp_path / "local-score-data"
    env = os.environ.copy()
    env["MIDI_MP4_DATA_DIR"] = str(data_root)
    server = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if server.poll() is not None:
                pytest.fail("The local app server exited before becoming ready.")
            try:
                with urlopen(base_url + "/api/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except (OSError, URLError):
                time.sleep(0.25)
        else:
            pytest.fail("The local app server did not become ready in 30 seconds.")

        browser_errors: list[str] = []
        fixture = ROOT / "tests" / "fixtures" / "satb-tiny.musicxml"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(accept_downloads=True)
            page.on("pageerror", lambda error: browser_errors.append(str(error)))
            page.goto(base_url, wait_until="domcontentloaded")
            page.locator("#library-count").wait_for(state="visible")
            page.wait_for_function(
                "() => document.querySelector('#library-count')?.textContent === '0 partituri'",
                timeout=15_000,
            )

            page.locator("#file-input").set_input_files(str(fixture))
            page.wait_for_function(
                "() => document.querySelector('#score-title')?.textContent.includes('Mini SATB')",
                timeout=60_000,
            )
            page.wait_for_function(
                "() => document.querySelector('#score-render svg') !== null",
                timeout=60_000,
            )
            page.wait_for_function(
                "() => document.querySelector('#library-count')?.textContent === '1 partitură'",
                timeout=15_000,
            )
            for button_id in ("export-audio", "export-audio-pack", "download-vocal-project"):
                assert page.locator("#" + button_id).is_enabled(), f"{button_id} should be enabled after import"

            with page.expect_download(timeout=60_000) as pending_download:
                page.locator("#export-audio").click()
            download = pending_download.value
            assert download.suggested_filename.endswith(".wav")
            downloaded_audio = tmp_path / download.suggested_filename
            download.save_as(downloaded_audio)
            assert downloaded_audio.stat().st_size > 44
            assert downloaded_audio.read_bytes()[:4] == b"RIFF"

            page.locator("#new-score").click()
            assert page.locator("#score-panel").is_hidden()
            page.locator("#saved-score-list .saved-score-actions button").first.click()
            page.wait_for_function(
                "() => document.querySelector('#score-title')?.textContent.includes('Mini SATB')",
                timeout=60_000,
            )
            assert page.locator("#export-audio").is_enabled()
            assert page.locator("#upload-vocal-stem").is_enabled()

            page.once("dialog", lambda dialog: dialog.accept())
            page.locator("#saved-score-list .saved-score-actions button").nth(1).click()
            page.wait_for_function(
                "() => document.querySelector('#library-count')?.textContent === '0 partituri'",
                timeout=15_000,
            )
            assert list(data_root.iterdir()) == []
            assert browser_errors == [], "Unexpected JavaScript runtime errors: " + "; ".join(browser_errors)
            browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
