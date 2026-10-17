import os
import secrets
import shutil
import tempfile
import uuid
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Security, UploadFile
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles

from backend import jobs
from backend.schema import AnalyzeResponse, JobResponse

API_KEY = os.environ.get("API_KEY", "")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "200")) * 1024 * 1024
FRONTEND_DIST = Path(os.environ.get("FRONTEND_DIST", "../frontend/dist"))

app = FastAPI(title="Datathon 26 — кассовая дисциплина")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: str | None = Security(api_key_header)) -> None:
    if not API_KEY or not key or not secrets.compare_digest(key, API_KEY):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/analyze", response_model=AnalyzeResponse, dependencies=[Depends(require_api_key)])
def analyze(file: UploadFile) -> AnalyzeResponse:
    if not (file.filename or "").lower().endswith(".mov"):
        raise HTTPException(status_code=415, detail="expected .mov file in field 'file'")
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large")

    job_id = uuid.uuid4().hex[:12]
    fd, tmp = tempfile.mkstemp(prefix=f"{job_id}-", suffix=".mov")
    with os.fdopen(fd, "wb") as out:
        shutil.copyfileobj(file.file, out)
    jobs.submit(job_id, Path(tmp))
    return AnalyzeResponse(job_id=job_id)


@app.get(
    "/api/v1/jobs/{job_id}",
    response_model=JobResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_api_key)],
)
def get_job(job_id: str) -> JobResponse:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")


def run() -> None:
    uvicorn.run("backend.main:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
