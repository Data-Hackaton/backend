import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from backend import jobs, main

KEY = "test-key"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", KEY)
    monkeypatch.setattr(jobs, "MOCK_DELAY_SEC", 0)
    return TestClient(main.app)


@pytest.fixture
def mov(tmp_path):
    path = tmp_path / "sample.mov"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10", str(path)],
        check=True,
    )
    return path


def test_rejects_missing_or_wrong_key(client, mov):
    with mov.open("rb") as f:
        assert client.post("/api/v1/analyze", files={"file": ("a.mov", f)}).status_code == 401
    assert client.get("/api/v1/jobs/x", headers={"X-API-Key": "wrong"}).status_code == 401


def test_rejects_non_mov(client):
    r = client.post("/api/v1/analyze", headers={"X-API-Key": KEY}, files={"file": ("a.mp4", b"x")})
    assert r.status_code == 415


def test_analyze_then_poll_until_done(client, mov):
    with mov.open("rb") as f:
        r = client.post("/api/v1/analyze", headers={"X-API-Key": KEY}, files={"file": ("sample.mov", f)})
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    for _ in range(50):
        job = client.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": KEY}).json()
        if job["status"] != "processing":
            break
        time.sleep(0.1)

    assert job["status"] == "done", job
    assert job["verdict"] == "violation"
    v = job["violations"][0]
    assert 1 <= len(v["evidence_frames"]) <= 3
    assert v["evidence_frames"][0]["image_base64"].startswith("/9j/")  # JPEG
