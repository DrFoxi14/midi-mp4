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


def test_concert_mode_auto_advances_to_next_setlist_item(tmp_path: Path):
    """Exercise the actual audio-ended event and setlist transition in Chromium."""
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

        fixture = ROOT / "tests" / "fixtures" / "satb-tiny.musicxml"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(base_url, wait_until="domcontentloaded")
            page.wait_for_function(
                "() => document.querySelector('#library-count')?.textContent === '0 partituri'",
                timeout=15_000,
            )
            page.locator("#file-input").set_input_files(str(fixture))
            page.wait_for_function(
                "() => document.querySelector('#score-render svg') !== null",
                timeout=60_000,
            )
            page.locator("#tempo").evaluate(
                "(el) => { el.value = '240'; el.dispatchEvent(new Event('input', {bubbles:true})); }"
            )
            page.locator("#concert-add").click()

            page.locator("#file-input").set_input_files(str(fixture))
            page.wait_for_function(
                "() => document.querySelector('#library-count')?.textContent === '2 partituri'",
                timeout=15_000,
            )
            page.wait_for_function(
                "() => document.querySelector('#score-render svg') !== null",
                timeout=60_000,
            )
            page.locator("#tempo").evaluate(
                "(el) => { el.value = '240'; el.dispatchEvent(new Event('input', {bubbles:true})); }"
            )
            page.locator("#concert-add").click()

            auto_next = page.locator("#concert-auto-next")
            auto_next.check()
            page.reload(wait_until="domcontentloaded")
            assert page.locator("#concert-auto-next").is_checked()
            page.wait_for_function(
                "() => document.querySelectorAll('#concert-setlist .setlist-item').length === 2",
                timeout=15_000,
            )
            items = page.locator("#concert-setlist .setlist-item")
            first_class = items.nth(0).get_attribute("class") or ""
            second_class = items.nth(1).get_attribute("class") or ""
            assert "active" in first_class, "The current setlist item should persist after reload."
            assert "active" not in second_class

            items.nth(0).locator(".setlist-actions button").nth(3).click()
            page.wait_for_function(
                "() => document.querySelectorAll('#concert-setlist .setlist-item.active').length === 1"
                " && document.querySelectorAll('#concert-setlist .setlist-item')[1].classList.contains('active')",
                timeout=20_000,
            )
            page.wait_for_function(
                "() => document.querySelector('#concert-status')?.textContent.startsWith('Trecere automată:')",
                timeout=20_000,
            )
            assert page.locator("#concert-status").inner_text().startswith("Trecere automată:")
            browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
