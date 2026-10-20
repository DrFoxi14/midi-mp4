from fastapi.testclient import TestClient

from app.main import app


def test_local_app_entrypoint_and_health_endpoint():
    client = TestClient(app)

    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "MIDI-MP4" in page.text

    health = client.get("/api/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["mode"] == "local-only"
    assert isinstance(body["video_export_available"], bool)
    assert set(body["video_dependencies"]) == {
        "ffmpeg", "verovio", "cairosvg", "pillow", "playwright", "chromium"
    }
