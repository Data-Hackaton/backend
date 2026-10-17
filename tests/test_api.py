import base64
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend import activities, storage, temporal_client
from backend.db import Base, engine
from backend.main import app

ADMIN = {"X-API-Key": "test-admin"}


@pytest.fixture
def s3(monkeypatch):
    objects: dict[str, bytes] = {}
    monkeypatch.setattr(storage, "ensure_bucket", lambda: None)
    monkeypatch.setattr(storage, "put_file", lambda key, path, ct: objects.__setitem__(key, open(path, "rb").read()))
    monkeypatch.setattr(storage, "put_bytes", lambda key, data, ct: objects.__setitem__(key, data))
    monkeypatch.setattr(storage, "get_bytes", lambda key: objects[key])
    return objects


@pytest.fixture
def started(monkeypatch):
    jobs: list[str] = []

    async def start(job_id):
        jobs.append(job_id)

    async def workers():
        return [{"queue": "cv-gpu", "identity": "cv-worker@test", "last_seen": None}]

    monkeypatch.setattr(temporal_client, "start_analysis", start)
    monkeypatch.setattr(temporal_client, "workers", workers)
    return jobs


@pytest.fixture
def client(s3, started):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def mov(tmp_path):
    path = tmp_path / "sample.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=160x120:rate=10",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def upload_api(client, mov, headers=ADMIN, name="sample.mov"):
    with mov.open("rb") as f:
        return client.post("/api/v1/analyze", headers=headers, files={"file": (name, f)})


def finish_job(s3, job_id, events, boxes=(), verdict="violation"):
    """То, что делает cv-worker + publish: результат в S3 и в БД."""
    s3[f"jobs/{job_id}/events.json"] = json.dumps(events).encode()
    s3[f"jobs/{job_id}/boxes.json"] = json.dumps(list(boxes)).encode()
    s3[f"jobs/{job_id}/evidence/0.jpg"] = b"\xff\xd8jpeg"
    activities.prepare(job_id)
    activities.publish({"job_id": job_id, "verdict": verdict, "detector": "stub",
                        "events_key": f"jobs/{job_id}/events.json", "boxes_key": f"jobs/{job_id}/boxes.json"})


def violation_event(t, title="Деньги предыдущего клиента на кассовом столе", job_id="x"):
    return {"t_start": t, "t_end": t, "event": "person_enter", "actor": "client", "status": "violation",
            "title": title, "rules": ["desk_carryover:banknotes"], "norm": "n231_p20",
            "message": "Входит клиент, на столе деньги предыдущего.", "evidence": [{"t": t, "key": f"jobs/{job_id}/evidence/0.jpg"}]}


def test_jury_requires_key(client, mov):
    assert upload_api(client, mov, headers={}).status_code == 401
    assert upload_api(client, mov, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/v1/jobs/nope").status_code == 401


def test_jury_only_mov(client, tmp_path):
    r = client.post("/api/v1/analyze", headers=ADMIN, files={"file": ("a.mp4", b"x")})
    assert r.status_code == 415


def test_tokens_issue_use_revoke(client, mov):
    created = client.post("/api/v1/tokens", json={"name": "bot-1"}).json()
    secret = created["token"]
    assert secret.startswith("fva_") and created["prefix"] == secret[:12]
    assert "token" not in client.get("/api/v1/tokens").json()[0]
    assert upload_api(client, mov, headers={"Authorization": f"Bearer {secret}"}).status_code == 200
    assert client.get("/api/v1/tokens").json()[0]["last_used_at"] is not None
    client.delete(f"/api/v1/tokens/{created['id']}")
    assert upload_api(client, mov, headers={"X-API-Key": secret}).status_code == 401


def test_analyze_then_done_in_jury_schema(client, mov, s3, started):
    job_id = upload_api(client, mov).json()["job_id"]
    assert started == [job_id]
    assert client.get(f"/api/v1/jobs/{job_id}", headers=ADMIN).json() == {"job_id": job_id, "status": "processing"}

    # два события одного типа в 3 с — один эпизод; третье через 8 с — второй
    finish_job(s3, job_id, [violation_event(56.5, job_id=job_id), violation_event(59.0, job_id=job_id),
                            violation_event(67.0, job_id=job_id),
                            {"t_start": 1.0, "t_end": 1.0, "event": "person_enter", "status": "expected"}])
    body = client.get(f"/api/v1/jobs/{job_id}", headers=ADMIN).json()
    assert body["status"] == "done" and body["verdict"] == "violation"
    assert [v["timecode_sec"] for v in body["violations"]] == [56.5, 67.0]
    v = body["violations"][0]
    assert v["type"] == "Деньги предыдущего клиента на кассовом столе"
    assert "Правила №231, п.20" in v["description"]
    assert v["rule"]["clause"] == "пункт 20"
    assert base64.b64decode(v["evidence_frames"][0]["image_base64"]).startswith(b"\xff\xd8")


def test_done_without_violations(client, mov, s3):
    job_id = upload_api(client, mov).json()["job_id"]
    finish_job(s3, job_id, [], verdict="no_violation")
    assert client.get(f"/api/v1/jobs/{job_id}", headers=ADMIN).json() == {
        "job_id": job_id, "status": "done", "verdict": "no_violation", "violations": []}


def test_ui_upload_video_queue_and_calendar(client, mov, s3):
    with mov.open("rb") as f:
        r = client.post("/api/v1/videos", files={"file": ("cam.mov", f)}, data={"recorded_at": "2026-10-15T10:00:00Z"})
    assert r.status_code == 200
    video, job = r.json()["video"], r.json()["job"]
    assert video["duration"] == pytest.approx(3.0, abs=0.2) and video["stream_url"].endswith("/stream")
    assert f"videos/{video['id']}/play.mp4" in s3

    q = client.get("/api/v1/queue").json()
    assert q["queued"] == 1 and q["jobs"][0]["position"] == 1 and q["workers"][0]["queue"] == "cv-gpu"

    finish_job(s3, job["id"], [violation_event(1.0, job_id=job["id"])])
    days = client.get("/api/v1/videos/calendar", params={"month": "2026-10"}).json()
    assert days == [{"date": "2026-10-15", "videos": 1, "violations": 1}]
    assert client.get("/api/v1/videos", params={"date_from": "2026-10-15", "date_to": "2026-10-15"}).json()[0]["id"] == video["id"]


def test_review_flow(client, mov, s3):
    job_id = upload_api(client, mov).json()["job_id"]
    finish_job(s3, job_id, [violation_event(5.0, job_id=job_id),
                            {"t_start": 1.0, "t_end": 1.0, "event": "person_enter", "status": "expected"}])
    pending = client.get("/api/v1/review").json()
    assert len(pending) == 1 and pending[0]["status"] == "violation"  # ожидаемые события в ревью не попадают
    event_id = pending[0]["id"]
    r = client.post(f"/api/v1/events/{event_id}/review", json={"decision": "rejected", "comment": "это отражение"})
    assert r.json()["review"] == "rejected"
    assert client.get("/api/v1/review").json() == []
    assert client.get(f"/api/v1/events/{event_id}/evidence/0").content.startswith(b"\xff\xd8")


def test_boxes_human_override_and_export(client, mov, s3):
    job_id = upload_api(client, mov).json()["job_id"]
    video_id = client.get(f"/api/v1/queue/jobs/{job_id}").json()["video_id"]
    finish_job(s3, job_id, [], boxes=[{"t": 1.0, "cls": "person", "bbox": [1, 2, 30, 60], "conf": 0.8},
                                      {"t": 2.0, "cls": "table", "bbox": [0, 50, 160, 120], "conf": 0.9}])
    assert len(client.get(f"/api/v1/videos/{video_id}/boxes").json()) == 2

    fixed = [{"cls": "banknotes", "bbox": [10, 10, 20, 20]}]
    assert client.put(f"/api/v1/videos/{video_id}/boxes", params={"t": 1.0}, json=fixed).status_code == 200
    boxes = client.get(f"/api/v1/videos/{video_id}/boxes").json()
    assert [(b["t"], b["cls"], b["source"]) for b in boxes] == [(1.0, "banknotes", "human"), (2.0, "table", "stub")]

    bad = client.put(f"/api/v1/videos/{video_id}/boxes", params={"t": 1.0}, json=[{"cls": "ufo", "bbox": [0, 0, 1, 1]}])
    assert bad.status_code == 422

    coco = client.get("/api/v1/training/export").json()
    assert len(coco["images"]) == 1 and coco["annotations"][0]["bbox"] == [10, 10, 10, 10]
    assert coco["categories"][coco["annotations"][0]["category_id"] - 1]["name"] == "banknotes"


def test_meta_and_cameras(client):
    assert client.get("/api/v1/cameras").json() == {"items": [], "coming_soon": True}
    meta = client.get("/api/v1/meta").json()
    assert [c["name"] for c in meta["classes"]] == ["person", "banknotes", "table"]
    assert meta["modes"] == ["view", "audit", "training"]
