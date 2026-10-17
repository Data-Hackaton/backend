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

    async def start_rerun(run_id, job_ids):  # пересчёт архива: задачи одной пачкой (RerunArchive)
        jobs.extend(job_ids)

    monkeypatch.setattr(temporal_client, "start_analysis", start)
    monkeypatch.setattr(temporal_client, "start_rerun", start_rerun)
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


def test_model_boxes_are_source_model(client, mov, s3):
    job_id = upload_api(client, mov).json()["job_id"]
    video_id = client.get(f"/api/v1/queue/jobs/{job_id}").json()["video_id"]
    s3[f"jobs/{job_id}/events.json"] = b"[]"
    s3[f"jobs/{job_id}/boxes.json"] = json.dumps([{"t": 1.0, "cls": "person", "bbox": [1, 2, 30, 60], "conf": 0.8,
                                                   "track_id": 1, "attributes": {"role": "cashier"}}]).encode()
    activities.prepare(job_id)
    activities.publish({"job_id": job_id, "verdict": "no_violation", "detector": "rfdetr",
                        "model": "model-rfdetr-det_l1-20261017",
                        "events_key": f"jobs/{job_id}/events.json", "boxes_key": f"jobs/{job_id}/boxes.json"})
    boxes = client.get(f"/api/v1/videos/{video_id}/boxes").json()
    assert [(b["source"], b["track_id"], b["attributes"]) for b in boxes] == [("model", 1, {"role": "cashier"})]
    assert client.get(f"/api/v1/queue/jobs/{job_id}").json()["detector"] == "rfdetr"
    assert client.get("/api/v1/pipeline").json()["model_version"] == "model-rfdetr-det_l1-20261017"


def test_events_list_type_ids_and_manual_events(client, mov, s3):
    job_id = upload_api(client, mov).json()["job_id"]
    video_id = client.get(f"/api/v1/queue/jobs/{job_id}").json()["video_id"]
    finish_job(s3, job_id, [violation_event(1.0, job_id=job_id),
                            {"t_start": 0.5, "t_end": 0.5, "event": "person_enter", "status": "expected"},
                            {"t_start": 0.7, "t_end": 0.7, "event": "put", "status": "warning", "rules": ["out_of_template"]},
                            {"t_start": 0.0, "t_end": 0.9, "event": "sop_step", "status": "warning",
                             "title": "Пропущен шаг: кассир кладёт купюры в счётную машинку", "rules": ["nvidia_sop_checker"]}])
    types = {t["id"]: t for t in client.get("/api/v1/event-types").json()}
    events = client.get("/api/v1/events").json()
    # в очередь — только названное правило; шаг и отклонение от шаблона без названия — не вопрос
    assert {(e["status"], e["event"]): e["review"] for e in events} == {
        ("violation", "person_enter"): "pending", ("expected", "person_enter"): "none", ("warning", "put"): "none",
        ("warning", "sop_step"): "none"}
    assert [e["status"] for e in client.get("/api/v1/review").json()] == ["violation"]
    assert {e["type_id"] for e in events} <= set(types)  # у каждого события тип из каталога
    carry = next(e for e in events if e["status"] == "violation")
    assert types[carry["type_id"]]["title"] == "Деньги предыдущего клиента на кассовом столе"
    assert types[carry["type_id"]]["kind"] == "violation" and types["step:person_enter"]["kind"] == "expected"
    procedure = types[carry["type_id"]]["procedures"][0]  # процедура проверки темы из нормативного корпуса
    assert procedure["id"] == "previous_cash_storage" and procedure["check"] and procedure["steps"][0]["observability"]

    personal = next(t["id"] for t in types.values() if t["title"] == "Личные вещи на кассовом столе")
    body = {"video_id": video_id, "type_id": personal, "start": 1.0, "end": 2.0, "description": "сумка на столе"}
    manual = client.post("/api/v1/events", json=body).json()
    assert (manual["source"], manual["review"], manual["type_id"], manual["status"]) == ("human", "approved", personal, "violation")
    assert client.put(f"/api/v1/events/{manual['id']}", json={**body, "end": 3.0}).json()["t_end"] == 3.0
    assert client.delete(f"/api/v1/events/{carry['id']}").status_code == 409  # событие модели не удаляется
    assert client.delete(f"/api/v1/events/{manual['id']}").status_code == 204
    assert all(e["source"] == "model" for e in client.get("/api/v1/events").json())


def test_custom_event_type_needs_ten_videos(client, mov, s3):
    videos = []
    for _ in range(10):
        job_id = upload_api(client, mov).json()["job_id"]
        videos.append(client.get(f"/api/v1/queue/jobs/{job_id}").json()["video_id"])
    t = client.post("/api/v1/event-types", json={"title": "Кассир ест", "kind": "warning", "description": "еда",
                                                 "observable": "еда на столе",
                                                 "first_sample": {"video_id": videos[0], "start": 1, "end": 2}}).json()
    assert (t["status"], t["custom"], len(t["samples"])) == ("draft", True, 1)
    again = client.post(f"/api/v1/event-types/{t['id']}/samples", json={"video_id": videos[0], "start": 3, "end": 4})
    assert again.status_code == 409  # одна запись — один действующий пример
    for v in videos[1:]:
        t = client.post(f"/api/v1/event-types/{t['id']}/samples", json={"video_id": v, "start": 1, "end": 2}).json()
    for sample in t["samples"][:-1]:
        t = client.post(f"/api/v1/event-types/{t['id']}/samples/{sample['id']}/review", json={"decision": "approved"}).json()
    assert client.post(f"/api/v1/event-types/{t['id']}/publish").status_code == 409  # 9 из 10
    last = t["samples"][-1]["id"]
    client.post(f"/api/v1/event-types/{t['id']}/samples/{last}/review", json={"decision": "approved"})
    assert client.post(f"/api/v1/event-types/{t['id']}/publish").json()["status"] == "published"


def test_pipeline_dataset_rerun_train(client, mov, s3, started):
    job_id = upload_api(client, mov).json()["job_id"]
    finish_job(s3, job_id, [], verdict="no_violation")
    assert client.get("/api/v1/pipeline").json()["batch_stage"] == "idle"
    ds = client.post("/api/v1/pipeline/dataset").json()
    assert ds["dataset_stage"] == "ready" and any(k.startswith("datasets/") for k in s3)
    rerun = client.post("/api/v1/pipeline/rerun").json()
    assert (rerun["batch_stage"], rerun["total_videos"], rerun["processed_videos"]) == ("running", 1, 0)
    finish_job(s3, started[-1], [], verdict="no_violation")
    assert client.get("/api/v1/pipeline").json()["batch_stage"] == "ready"
    assert client.post("/api/v1/pipeline/train").status_code == 409


def test_tag_label_is_not_a_box_class(client, mov, s3):
    classes = [c["name"] for c in client.get("/api/v1/meta").json()["classes"]]
    assert "event" not in classes and "person" in classes  # тег системы событий CVAT — не класс бокса
    job_id = upload_api(client, mov).json()["job_id"]
    video_id = client.get(f"/api/v1/queue/jobs/{job_id}").json()["video_id"]
    finish_job(s3, job_id, [], verdict="no_violation")
    bad = client.put(f"/api/v1/videos/{video_id}/boxes", params={"t": 1.0}, json=[{"cls": "event", "bbox": [0, 0, 5, 5]}])
    assert bad.status_code == 422


def test_meta_and_cameras(client):
    assert client.get("/api/v1/cameras").json() == {"items": [], "coming_soon": True}
    meta = client.get("/api/v1/meta").json()
    assert [c["name"] for c in meta["classes"]] == ["person", "banknotes", "table"]
    assert meta["modes"] == ["view", "audit", "training"]


def test_openapi_security_and_docs(client):
    spec = client.get("/openapi.json").json()
    schemes = spec["components"]["securitySchemes"]
    assert schemes["X-API-Key"] == {"type": "apiKey", "in": "header", "name": "X-API-Key",
                                    "description": schemes["X-API-Key"]["description"]}
    assert schemes["Bearer"]["scheme"] == "bearer"
    for path, method in (("/api/v1/analyze", "post"), ("/api/v1/jobs/{job_id}", "get"), ("/api/v1/jobs", "get")):
        assert {"X-API-Key": []} in spec["paths"][path][method]["security"]
    assert "security" not in spec["paths"]["/api/v1/health"]["get"]  # готовность — без ключа
    assert spec["tags"][0]["name"] == "jury" and client.get("/docs").status_code == 200


def test_jury_long_poll_and_retry_after(client, mov, s3):
    import time

    job_id = upload_api(client, mov).json()["job_id"]
    t0 = time.monotonic()
    r = client.get(f"/api/v1/jobs/{job_id}", params={"wait": 0.6}, headers=ADMIN)
    assert r.json() == {"job_id": job_id, "status": "processing"} and time.monotonic() - t0 >= 0.5
    assert r.headers["Retry-After"] == "2" and r.headers["Cache-Control"] == "no-store"
    finish_job(s3, job_id, [], verdict="no_violation")
    t0 = time.monotonic()
    r = client.get(f"/api/v1/jobs/{job_id}", params={"wait": 30}, headers=ADMIN)
    assert r.json()["status"] == "done" and time.monotonic() - t0 < 5 and "Retry-After" not in r.headers
    assert client.get(f"/api/v1/jobs/{job_id}", params={"wait": 61}, headers=ADMIN).status_code == 422


def test_progress_and_jobs_of_key(client, mov, s3):
    job_id = upload_api(client, mov).json()["job_id"]
    p = client.get(f"/api/v1/jobs/{job_id}/progress", headers=ADMIN).json()
    assert (p["status"], p["stage"], p["queue_position"]) == ("queued", "queued", 1) and p["eta_sec"] > 0
    assert client.get(f"/api/v1/jobs/{job_id}/progress").status_code == 401
    assert client.get("/api/v1/jobs/nope/progress", headers=ADMIN).status_code == 404
    finish_job(s3, job_id, [violation_event(1.0, job_id=job_id)])
    p = client.get(f"/api/v1/jobs/{job_id}/progress", headers=ADMIN).json()
    assert (p["status"], p["stage"], p["progress"], p["queue_position"]) == ("done", "done", 1.0, None)

    token = client.post("/api/v1/tokens", json={"name": "bot"}).json()["token"]
    bot_job = upload_api(client, mov, headers={"X-API-Key": token}).json()["job_id"]
    mine = client.get("/api/v1/jobs", headers=ADMIN).json()
    assert [(j["job_id"], j["status"], j["verdict"], j["violations"]) for j in mine] == [(job_id, "done", "violation", 1)]
    assert [j["job_id"] for j in client.get("/api/v1/jobs", headers={"X-API-Key": token}).json()] == [bot_job]


def test_health(client, monkeypatch):
    h = client.get("/api/v1/health").json()
    assert h["status"] == "ok" and h["workers"] == {"analyze": 0, "cv-gpu": 1}

    async def no_workers():
        return []

    monkeypatch.setattr(temporal_client, "workers", no_workers)
    assert client.get("/api/v1/health").json()["status"] == "degraded"


def test_rerun_is_one_batch_and_waiting_jobs_do_not_expire(client, mov, s3, monkeypatch):
    from datetime import timedelta

    from backend.db import Job, SessionLocal

    for _ in range(2):
        with mov.open("rb") as f:
            client.post("/api/v1/videos", files={"file": ("cam.mov", f)})
    batches = []

    async def start_rerun(run_id, job_ids):
        batches.append(job_ids)

    monkeypatch.setattr(temporal_client, "start_rerun", start_rerun)
    client.post("/api/v1/pipeline/rerun")
    assert len(batches) == 1 and len(batches[0]) == 2
    with SessionLocal() as db:  # ждёт своей очереди дольше лимита — это не зависание
        for job in db.query(Job).filter(Job.id.in_(batches[0])):
            job.created_at = job.created_at - timedelta(minutes=30)
        db.commit()
    client.get("/api/v1/queue")
    with SessionLocal() as db:
        assert {j.status for j in db.query(Job).filter(Job.id.in_(batches[0]))} == {"queued"}
