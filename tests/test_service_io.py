"""Exercise concrete SDK objects that the API tests otherwise replace with dictionaries."""
import asyncio
import time
from contextvars import copy_context
from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace

import pytest
from botocore.response import StreamingBody
from PIL import Image

from backend import config, deadlines, jobs, media, progress, storage, temporal_client
from backend.db import Job, SessionLocal, now
from backend.temporal_client import start_analysis as submit_analysis
from test_api import client, mov, s3, started, upload_api  # noqa: F401


def test_s3_get_bytes_reads_and_closes_real_streaming_body(monkeypatch):
    raw = BytesIO(b"JPEG or JSON data")
    body = StreamingBody(raw, len(raw.getvalue()))
    monkeypatch.setattr(storage, "client", lambda: SimpleNamespace(get_object=lambda **kwargs: {"Body": body}))
    assert storage.get_bytes("example") == b"JPEG or JSON data"
    assert raw.closed


def test_s3_read_closes_body_when_budget_expires(monkeypatch):
    class SlowStream(BytesIO):
        def read(self, *args):
            time.sleep(0.03)
            return super().read(*args)

    raw = SlowStream(b"x" * 100_000)
    body = StreamingBody(raw, len(raw.getvalue()))
    monkeypatch.setattr(storage, "client", lambda: SimpleNamespace(get_object=lambda **kwargs: {"Body": body}))
    with deadlines.budget(0.01), pytest.raises(TimeoutError):
        storage.get_bytes("example")
    assert raw.closed


def test_request_cancellation_reaches_copied_worker_context():
    # Cancellation can arrive before a monotonic clock boundary on Windows.
    with pytest.raises(asyncio.CancelledError):
        with deadlines.budget(60):
            worker_context = copy_context()
            raise asyncio.CancelledError
    with pytest.raises(TimeoutError, match="cancelled"):
        worker_context.run(deadlines.remaining)


def test_workflow_timeout_uses_remaining_budget_after_connect(client, mov, monkeypatch):
    job_id = upload_api(client, mov).json()["job_id"]
    current = now()
    with SessionLocal() as db:
        db.get(Job, job_id).created_at = current - timedelta(seconds=120)
        db.commit()
    calls = []

    async def start_workflow(*args, **kwargs):
        calls.append(kwargs)

    async def connect():
        monkeypatch.setattr(jobs, "now", lambda: current + timedelta(seconds=5))
        return SimpleNamespace(start_workflow=start_workflow)

    monkeypatch.setattr(temporal_client, "client", connect)
    asyncio.run(submit_analysis(job_id))
    assert calls[0]["execution_timeout"] == timedelta(seconds=config.JOB_TIMEOUT_SEC - 125)


def test_optional_progress_outage_returns_promptly(monkeypatch):
    monkeypatch.setattr(progress, "VALKEY_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(progress, "_client", None)
    before = time.monotonic()
    assert progress.get("missing") is None
    assert time.monotonic() - before < 1.5


def test_valid_png_is_not_accepted_as_jpeg():
    stream = BytesIO()
    Image.new("RGB", (8, 8), "red").save(stream, format="PNG")
    with pytest.raises(ValueError, match="JPEG"):
        media.validate_jpeg(stream.getvalue())


def test_storage_outage_is_a_controlled_error(client, mov, monkeypatch):
    def unavailable(*args):
        raise ConnectionError("S3 unavailable")

    monkeypatch.setattr(storage, "put_file", unavailable)
    response = upload_api(client, mov)
    assert response.status_code == 503 and response.json()["detail"]
