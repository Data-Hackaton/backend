"""Regression cases from the jury API audit, including failed dependencies and deadlines."""
import asyncio
import json
import subprocess
import time
from threading import Event as ThreadEvent
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from backend import activities, api, config, media, progress, storage, temporal_client, workflows
from backend.db import Event, Job, SessionLocal, Video, now
from backend.main import app
from test_api import ADMIN, client, mov, s3, started, upload_api, violation_event  # noqa: F401


@pytest.fixture(autouse=True)
def no_progress_service(monkeypatch):
    # Progress is optional, and these cases exercise the database/API contract.
    monkeypatch.setattr(progress, "set_stage", lambda *args: None)
    monkeypatch.setattr(progress, "get", lambda *args: None)


@pytest.fixture(scope="module")
def jpeg():
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=1",
         "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg", "-"],
        capture_output=True, check=True,
    ).stdout


def stage_result(s3, job_id, events, jpeg, verdict="violation"):
    events_key, boxes_key = f"jobs/{job_id}/events.json", f"jobs/{job_id}/boxes.json"
    s3[events_key] = json.dumps(events).encode()
    s3[boxes_key] = b"[]"
    s3[f"jobs/{job_id}/evidence/0.jpg"] = jpeg
    return {"job_id": job_id, "verdict": verdict, "detector": "model",
            "events_key": events_key, "boxes_key": boxes_key}


@pytest.mark.parametrize("mode", ["bearer", "cookie"])
@pytest.mark.parametrize("method", ["get", "post"])
def test_jury_requires_header_even_with_other_credentials(client, mov, mode, method):
    job_id = upload_api(client, mov).json()["job_id"]
    client.headers.clear()
    if mode == "bearer":
        client.headers["Authorization"] = "Bearer test-admin"
    else:
        assert client.post("/api/v1/auth/session", headers={"Origin": "http://testserver"},
                           json={"token": "test-admin"}).status_code == 204
        client.headers["Origin"] = "http://testserver"
    if method == "get":
        response = client.get(f"/api/v1/jobs/{job_id}")
    else:
        response = client.post("/api/v1/analyze", files={"file": ("sample.mov", mov.read_bytes())})
    assert response.status_code == 401
    # Existing UI authentication continues to work.
    assert client.get("/api/v1/videos").status_code == 200


def test_jury_openapi_requires_only_api_key(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path, method in [("/api/v1/analyze", "post"), ("/api/v1/jobs/{job_id}", "get")]:
        assert paths[path][method]["security"] == [{"X-API-Key": []}]


@pytest.mark.parametrize("fault", ["missing", "corrupt", "missing_object", "negative_time", "beyond_video", "blank_description"])
def test_incomplete_violation_fails_instead_of_becoming_clean(client, mov, s3, jpeg, fault):
    job_id = upload_api(client, mov).json()["job_id"]
    event = violation_event(1, job_id=job_id)
    if fault == "missing":
        event["evidence"] = []
    elif fault == "negative_time":
        event["t_start"] = -1
    elif fault == "beyond_video":
        event["evidence"][0]["t"] = 100
    elif fault == "blank_description":
        event["message"] = " "
    payload = stage_result(s3, job_id, [event], jpeg)
    if fault == "corrupt":
        s3[f"jobs/{job_id}/evidence/0.jpg"] = b"\xff\xd8jpeg"
    elif fault == "missing_object":
        del s3[f"jobs/{job_id}/evidence/0.jpg"]
    activities.prepare(job_id)
    activities.publish(payload)
    body = client.get(f"/api/v1/jobs/{job_id}").json()
    assert body["status"] == "failed" and body["error"]
    assert "verdict" not in body and "violations" not in body


def test_violation_verdict_without_events_is_not_clean(client, mov, s3, jpeg):
    job_id = upload_api(client, mov).json()["job_id"]
    activities.prepare(job_id)
    activities.publish(stage_result(s3, job_id, [], jpeg, verdict="violation"))
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "failed"


def test_legacy_done_with_missing_evidence_fails_at_read(client, mov):
    job_id = upload_api(client, mov).json()["job_id"]
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        job.status, job.verdict, job.finished_at = "done", "violation", now()
        db.add(Event(job_id=job_id, video_id=job.video_id, t_start=1, t_end=1, event="person_enter",
                     status="violation", title="Visible violation", message="Observed action", evidence=[]))
        db.commit()
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "failed"


def test_queue_time_counts_toward_deadline(client, mov):
    job_id = upload_api(client, mov).json()["job_id"]
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        job.status = "processing"
        job.created_at, job.started_at = now() - timedelta(seconds=301), now() - timedelta(seconds=100)
        db.commit()
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "failed"


@pytest.mark.parametrize("expire_first", [False, True])
def test_late_publication_never_succeeds(client, mov, s3, jpeg, expire_first):
    job_id = upload_api(client, mov).json()["job_id"]
    activities.prepare(job_id)
    with SessionLocal() as db:
        db.get(Job, job_id).created_at = now() - timedelta(seconds=301)
        db.commit()
    if expire_first:
        assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "failed"
    activities.publish(stage_result(s3, job_id, [], jpeg, verdict="no_violation"))
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "failed"


def test_prepare_retry_preserves_first_start(client, mov):
    job_id = upload_api(client, mov).json()["job_id"]
    activities.prepare(job_id)
    first_start = now() - timedelta(seconds=100)
    with SessionLocal() as db:
        db.get(Job, job_id).started_at = first_start
        db.commit()
    activities.prepare(job_id)
    with SessionLocal() as db:
        assert db.get(Job, job_id).started_at.replace(tzinfo=first_start.tzinfo) == first_start


def test_prepare_cannot_restart_failed_job(client, mov):
    job_id = upload_api(client, mov).json()["job_id"]
    activities.fail({"job_id": job_id, "error": "already failed"})
    try:
        activities.prepare(job_id)
    except Exception:
        pass
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "failed"


def test_late_fail_does_not_destroy_success(client, mov, s3, jpeg):
    job_id = upload_api(client, mov).json()["job_id"]
    activities.prepare(job_id)
    activities.publish(stage_result(s3, job_id, [], jpeg, verdict="no_violation"))
    activities.fail({"job_id": job_id, "error": "late retry"})
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "done"


def test_prepare_error_runs_failure_activity(monkeypatch):
    calls = []

    async def execute(name, *args, **kwargs):
        calls.append(name)
        if name == "prepare":
            raise RuntimeError("prepare unavailable")

    monkeypatch.setattr(workflows.workflow, "execute_activity", execute)
    with pytest.raises(RuntimeError, match="prepare unavailable"):
        asyncio.run(workflows.AnalyzeVideo().run("missing"))
    assert calls == ["prepare", "fail"]


def test_valid_mov_not_limited_to_mp4_codecs(client, tmp_path, started):
    path = tmp_path / "qtrle.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=2",
                    "-c:v", "qtrle", str(path)], check=True)
    assert media.probe(path)["duration"] == 1
    with TestClient(app, headers=ADMIN, raise_server_exceptions=False) as http:
        response = upload_api(http, path)
    assert response.status_code == 200
    assert started == [response.json()["job_id"]]


def test_dispatch_outage_returns_trackable_failed_job(client, mov, monkeypatch):
    async def unavailable(job_id):
        raise ConnectionError("Temporal unavailable")

    monkeypatch.setattr(temporal_client, "start_analysis", unavailable)
    with TestClient(app, headers=ADMIN, raise_server_exceptions=False) as http:
        response = upload_api(http, mov)
    assert response.status_code == 200
    body = client.get(f"/api/v1/jobs/{response.json()['job_id']}").json()
    assert body["status"] == "failed"


def test_preprocessing_time_is_part_of_job_age(client, mov, monkeypatch):
    original = api._store_video
    entered = []

    def store(*args, **kwargs):
        entered.append(now())
        return original(*args, **kwargs)

    monkeypatch.setattr(api, "_store_video", store)
    job_id = upload_api(client, mov).json()["job_id"]
    with SessionLocal() as db:
        assert db.get(Job, job_id).created_at.replace(tzinfo=entered[0].tzinfo) <= entered[0]


def test_slow_request_body_has_finite_deadline(client, monkeypatch):
    monkeypatch.setattr(config, "JOB_TIMEOUT_SEC", 0.02)
    messages = []

    async def receive():
        await asyncio.sleep(0.15)
        return {"type": "http.request", "body": b"--slow--\r\n", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/api/v1/analyze",
             "raw_path": b"/api/v1/analyze", "query_string": b"", "root_path": "",
             "headers": [(b"host", b"testserver"), (b"x-api-key", b"test-admin"),
                         (b"content-type", b"multipart/form-data; boundary=slow")],
             "server": ("testserver", 80), "client": ("test", 1)}
    asyncio.run(app(scope, receive, send))
    assert next(message["status"] for message in messages if message["type"] == "http.response.start") == 504


def test_blocked_preprocessing_has_finite_deadline(client, mov, monkeypatch):
    monkeypatch.setattr(config, "JOB_TIMEOUT_SEC", 0.03)
    original = api._store_video

    def slow_store(*args, **kwargs):
        time.sleep(0.15)
        return original(*args, **kwargs)

    monkeypatch.setattr(api, "_store_video", slow_store)
    with TestClient(app, headers=ADMIN, raise_server_exceptions=False) as http:
        response = upload_api(http, mov)
    assert response.status_code == 504


def test_database_write_cannot_block_upload_deadline(client, mov, monkeypatch):
    from sqlalchemy.orm import Session

    release, entered, finished = ThreadEvent(), ThreadEvent(), ThreadEvent()
    original = Session.flush

    def blocked_flush(session, *args, **kwargs):
        if any(isinstance(item, Video) for item in session.new):
            entered.set()
            try:
                release.wait(0.8)
                return original(session, *args, **kwargs)
            finally:
                finished.set()
        return original(session, *args, **kwargs)

    monkeypatch.setattr(Session, "flush", blocked_flush)
    monkeypatch.setattr(config, "JOB_TIMEOUT_SEC", 0.1)
    monkeypatch.setattr(api, "_process_upload", lambda *args: (
        {"sha256": "test", "size": 100, "duration": 3, "fps": 10, "width": 160, "height": 120},
        "source.mov", None))
    before = time.monotonic()
    try:
        response = upload_api(client, mov)
        elapsed = time.monotonic() - before
        assert entered.is_set(), "test must reach the blocked database write"
        assert response.status_code == 504
        assert elapsed < 0.5, f"event loop blocked for {elapsed:.3f}s"
    finally:
        release.set()
        assert finished.wait(2)


@pytest.mark.parametrize("late_bytes", [None, b"invalid JPEG"])
def test_slow_evidence_read_has_hard_deadline_without_late_mutation(client, mov, s3, jpeg, monkeypatch, late_bytes):
    job_id = upload_api(client, mov).json()["job_id"]
    activities.prepare(job_id)
    activities.publish(stage_result(s3, job_id, [violation_event(1, job_id=job_id)], jpeg))
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "done"
    release, entered, finished = ThreadEvent(), ThreadEvent(), ThreadEvent()
    original_read, original_result = storage.get_bytes, api.jury_result

    def slow_read(key):
        entered.set()
        release.wait(0.8)
        return original_read(key) if late_bytes is None else late_bytes

    def read_result(*args):
        try:
            return original_result(*args)
        finally:
            finished.set()

    monkeypatch.setattr(config, "RESULT_READ_TIMEOUT_SEC", 0.1, raising=False)
    monkeypatch.setattr(storage, "get_bytes", slow_read)
    monkeypatch.setattr(api, "jury_result", read_result)
    before = time.monotonic()
    try:
        response = client.get(f"/api/v1/jobs/{job_id}")
        elapsed = time.monotonic() - before
        assert entered.is_set(), "test must reach the blocked S3 read"
        assert response.status_code == 503
        assert elapsed < 0.5, f"S3 held the request for {elapsed:.3f}s"
    finally:
        release.set()
        assert finished.wait(2)
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.status == "done", job.error


def test_distinct_types_without_titles_remain_separate(client, mov, s3, jpeg):
    job_id = upload_api(client, mov).json()["job_id"]
    events = [violation_event(1, title=None, job_id=job_id), violation_event(2, title=None, job_id=job_id)]
    events[1]["event"] = "cash_transfer"
    activities.prepare(job_id)
    activities.publish(stage_result(s3, job_id, events, jpeg))
    body = client.get(f"/api/v1/jobs/{job_id}").json()
    assert body["status"] == "done"
    assert [event["type"] for event in body["violations"]] == ["person_enter", "cash_transfer"]


def test_manual_annotation_does_not_change_published_analysis(client, mov, s3, jpeg):
    job_id = upload_api(client, mov).json()["job_id"]
    activities.prepare(job_id)
    activities.publish(stage_result(s3, job_id, [], jpeg, verdict="no_violation"))
    before = client.get(f"/api/v1/jobs/{job_id}").json()
    with SessionLocal() as db:
        video_id = db.get(Job, job_id).video_id
    types = client.get("/api/v1/event-types").json()
    violation_type = next(item["id"] for item in types if item["kind"] == "violation" and item["status"] == "published")
    response = client.post("/api/v1/events", json={"video_id": video_id, "type_id": violation_type,
                                                 "start": 1, "end": 2, "description": "Human annotation"})
    assert response.status_code == 200 and response.json()["source"] == "human"
    assert client.get(f"/api/v1/jobs/{job_id}").json() == before


def test_timed_out_failure_write_is_rolled_back(client, mov, s3, jpeg, monkeypatch):
    from sqlalchemy.orm import Session
    from sqlalchemy.sql.dml import Update

    job_id = upload_api(client, mov).json()["job_id"]
    activities.prepare(job_id)
    activities.publish(stage_result(s3, job_id, [violation_event(1, job_id=job_id)], jpeg))
    s3[f"jobs/{job_id}/evidence/0.jpg"] = b"invalid JPEG"
    # Initialize Pillow's format plugins before measuring the blocked database operation.
    with pytest.raises(OSError):
        media.validate_jpeg(b"invalid JPEG")
    release, entered, finished = ThreadEvent(), ThreadEvent(), ThreadEvent()
    original_execute, original_result = Session.execute, api.jury_result

    def blocked_update(session, statement, *args, **kwargs):
        if isinstance(statement, Update) and statement.table.name == "jobs":
            entered.set()
            release.wait(0.8)
        return original_execute(session, statement, *args, **kwargs)

    def read_result(*args):
        try:
            return original_result(*args)
        finally:
            finished.set()

    monkeypatch.setattr(Session, "execute", blocked_update)
    monkeypatch.setattr(api, "jury_result", read_result)
    monkeypatch.setattr(config, "RESULT_READ_TIMEOUT_SEC", 0.1)
    before = time.monotonic()
    try:
        response = client.get(f"/api/v1/jobs/{job_id}")
        assert entered.is_set()
        assert response.status_code == 503
        assert time.monotonic() - before < 0.5
    finally:
        release.set()
        assert finished.wait(2)
    with SessionLocal() as db:
        assert db.get(Job, job_id).status == "done"
