"""MOV metadata is not proof that the video contains decodable frames."""

import subprocess

import pytest

from backend import deadlines, media, progress
from backend.db import Job, SessionLocal, Video
from test_api import client, s3, started  # noqa: F401


@pytest.fixture(autouse=True)
def no_progress_service(monkeypatch):
    monkeypatch.setattr(progress, "set_stage", lambda *args: None)


@pytest.fixture
def qtrle(tmp_path):
    path = tmp_path / "sample.mov"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
         "testsrc=duration=1:size=64x64:rate=2", "-c:v", "qtrle", str(path)],
        capture_output=True, check=True,
    )
    return path


@pytest.mark.parametrize("endpoint", ["/api/v1/analyze", "/api/v1/videos"])
@pytest.mark.parametrize("fault", ["unknown_codec", "corrupt_packets"])
def test_undecodable_mov_is_rejected_before_storage_or_dispatch(client, qtrle, s3, started, endpoint, fault):
    data = bytearray(qtrle.read_bytes())
    if fault == "unknown_codec":
        # Keep the valid MOV container and dimensions, but remove the decoder.
        codec = data.index(b"stsd") + 16
        assert data[codec:codec + 4] == b"rle "
        data[codec:codec + 4] = b"zzzz"
    else:
        # Preserve codec and sample tables; corrupt only the compressed frames.
        mdat = data.index(b"mdat")
        size = int.from_bytes(data[mdat - 4:mdat], "big")
        data[mdat + 4:mdat - 4 + size] = b"\xff" * (size - 8)
    qtrle.write_bytes(data)
    assert media.probe(qtrle)["duration"] == 1

    response = client.post(endpoint, files={"file": ("undecodable.mov", bytes(data), "video/quicktime")})

    assert response.status_code == 422, response.text
    assert "job_id" not in response.json()
    assert s3 == {} and started == []
    with SessionLocal() as db:
        assert db.query(Video).count() == 0
        assert db.query(Job).count() == 0


@pytest.mark.parametrize("fault,expected_status", [("empty_output", 422), ("timeout", 504)])
def test_decoder_without_a_frame_cannot_create_a_job(client, qtrle, s3, started, monkeypatch, fault, expected_status):
    run = subprocess.run
    decoder_timeouts = []

    def decoder(command, **kwargs):
        if command[0] != "ffmpeg":
            return run(command, **kwargs)
        decoder_timeouts.append(kwargs["timeout"])
        if fault == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, stdout=b"")

    monkeypatch.setattr(subprocess, "run", decoder)
    with deadlines.budget(5):
        response = client.post("/api/v1/analyze", files={"file": ("sample.mov", qtrle.read_bytes())})

    assert response.status_code == expected_status, response.text
    assert len(decoder_timeouts) == 1 and 0 < decoder_timeouts[0] <= 5
    assert s3 == {} and started == []
    with SessionLocal() as db:
        assert db.query(Video).count() == 0
        assert db.query(Job).count() == 0
