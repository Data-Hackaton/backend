"""Маршруты /api/v1: API жюри и ботов (по ключу) и методы интерфейса (открыты). Контракт — docs/API.md."""

import base64
import json
import shutil
import tempfile
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import yaml
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import config, media, progress, storage, temporal_client
from backend.auth import hash_token, new_token, require_key
from backend.db import ApiToken, Box, Event, Job, Video, get_db, now
from backend.schema import (
    AnalyzeResponse, BoxIn, BoxOut, CalendarDay, EventOut, EvidenceFrame, JobOut, JobResponse, QueueOut, QueueWorker,
    ReviewIn, Rule, TokenCreated, TokenIn, TokenOut, UploadOut, VideoOut, Violation,
)

router = APIRouter(prefix="/api/v1")
Db = Annotated[Session, Depends(get_db)]
VIOLATION_MERGE_SEC = 5.0  # события одного типа ближе 5 с — один эпизод нарушения


# --- справочники ---------------------------------------------------------------------------------------------------

def load_meta() -> dict:
    labels = json.loads((config.META_DIR / "cvat" / "labels.json").read_text())
    norms_path = config.META_DIR / "reference" / "norms.yaml"
    norms = yaml.safe_load(norms_path.read_text()) if norms_path.exists() else {}
    return {"labels": labels, "norms": norms}


META: dict | None = None


def meta() -> dict:
    global META
    if META is None:
        META = load_meta()
    return META


# --- приём видео ---------------------------------------------------------------------------------------------------

def _save_upload(file: UploadFile, dst: Path) -> None:
    size = 0
    with dst.open("wb") as out:
        while chunk := file.file.read(1 << 20):
            size += len(chunk)
            if size > config.MAX_UPLOAD_MB * 1024 * 1024:
                raise HTTPException(status_code=413, detail=f"file larger than {config.MAX_UPLOAD_MB} MB")
            out.write(chunk)


def _store_video(src: Path, video_id: str) -> tuple[dict, str, str]:
    """Метаданные, sha256, оригинал и mp4 для плеера в S3."""
    try:
        info = media.probe(src)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail="not a video file") from e
    sha = media.sha256_file(src)
    source_key = f"videos/{video_id}/source{src.suffix}"
    storage.put_file(source_key, str(src), "video/quicktime" if src.suffix == ".mov" else "video/mp4")
    play = src.with_name("play.mp4")
    media.remux_mp4(src, play)
    storage.put_file(f"videos/{video_id}/play.mp4", str(play), "video/mp4")
    return {**info, "sha256": sha, "size": src.stat().st_size}, source_key, f"videos/{video_id}/play.mp4"


async def ingest(file: UploadFile, db: Session, *, source: str, requested_by: str,
                 recorded_at: datetime | None = None) -> tuple[Video, Job]:
    ext = Path(file.filename or "").suffix.lower()
    allowed = {".mov"} if source == "api" else {".mov", ".mp4"}
    if ext not in allowed:
        raise HTTPException(status_code=415, detail=f"expected {'/'.join(sorted(allowed))} file in field 'file'")
    video_id, job_id = uuid.uuid4().hex[:12], uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"source{ext}"
        await run_in_threadpool(_save_upload, file, src)
        info, source_key, play_key = await run_in_threadpool(_store_video, src, video_id)
    video = Video(id=video_id, filename=file.filename or src.name, source_key=source_key, play_key=play_key,
                  sha256=info["sha256"], size=info["size"], duration=info["duration"], fps=info["fps"],
                  width=info["width"], height=info["height"], source=source, recorded_at=recorded_at or now())
    job = Job(id=job_id, video_id=video_id, requested_by=requested_by, stage="queued")
    db.add(video)
    db.flush()  # связей в моделях нет: без flush задача может уйти в БД раньше видео
    db.add(job)
    db.commit()
    progress.set_stage(job_id, "queued", 0.0)
    await temporal_client.start_analysis(job_id)
    return video, job


# --- преобразования ------------------------------------------------------------------------------------------------

def _stale(job: Job) -> bool:
    created = job.created_at if job.created_at.tzinfo else job.created_at.replace(tzinfo=UTC)
    return job.status in ("queued", "processing") and now() - created > timedelta(seconds=config.JOB_TIMEOUT_SEC + 30)


def _expire(db: Session, job: Job) -> None:
    """Workflow убит по таймауту, не успев записать failed, — фиксируем сами."""
    if _stale(job):
        job.status, job.error, job.finished_at = "failed", "timeout: обработка не уложилась в лимит", now()
        db.commit()


def job_out(job: Job, position: int | None = None, eta: float | None = None) -> JobOut:
    p = progress.get(job.id) or {}
    return JobOut(id=job.id, video_id=job.video_id, status=job.status, stage=p.get("stage", job.stage),
                  progress=p.get("progress"), verdict=job.verdict, detector=job.detector, error=job.error,
                  requested_by=job.requested_by, created_at=job.created_at, started_at=job.started_at,
                  finished_at=job.finished_at, position=position, eta_sec=eta)


def last_job(db: Session, video_id: str) -> Job | None:
    return db.scalar(select(Job).where(Job.video_id == video_id).order_by(Job.created_at.desc()).limit(1))


def video_out(db: Session, video: Video) -> VideoOut:
    job = last_job(db, video.id)
    return VideoOut(id=video.id, filename=video.filename, duration=video.duration, fps=video.fps, width=video.width,
                    height=video.height, source=video.source, recorded_at=video.recorded_at,
                    uploaded_at=video.uploaded_at, stream_url=f"/api/v1/videos/{video.id}/stream",
                    last_job=job_out(job) if job else None)


def event_out(e: Event) -> EventOut:
    return EventOut(
        id=e.id, job_id=e.job_id, video_id=e.video_id, t_start=e.t_start, t_end=e.t_end, event=e.event, actor=e.actor,
        object=e.object, target=e.target, scenario=e.scenario, instance=e.instance, step=e.step, status=e.status,
        title=e.title, rules=e.rules or [], norm=e.norm, message=e.message,
        evidence=[{"t": ev["t"], "url": f"/api/v1/events/{e.id}/evidence/{i}"} for i, ev in enumerate(e.evidence or [])],
        review=e.review, review_comment=e.review_comment, reviewed_at=e.reviewed_at,
    )


def norm_rule(norm_id: str | None) -> tuple[Rule | None, str]:
    norm = meta()["norms"].get(norm_id) if norm_id else None
    if not norm:
        return None, ""
    return (Rule(document=norm.get("act") or norm.get("document", ""), clause=norm.get("clause"), url=norm.get("url")),
            f" Норматив: {norm.get('short', norm.get('clause', ''))} ({norm.get('url', '')}).")


def violations_for(db: Session, job: Job) -> list[Violation]:
    events = db.scalars(select(Event).where(Event.job_id == job.id, Event.status == "violation")
                        .order_by(Event.t_start)).all()
    groups: list[list[Event]] = []
    for e in events:
        if groups and groups[-1][0].title == e.title and e.t_start - groups[-1][-1].t_start <= VIOLATION_MERGE_SEC:
            groups[-1].append(e)
        else:
            groups.append([e])
    result = []
    for group in groups:
        first = group[0]
        frames = [ev for e in group for ev in (e.evidence or [])][:3]
        if not frames:
            continue  # без кадра-доказательства нарушение в ответ жюри не попадает
        rule, norm_text = norm_rule(next((e.norm for e in group if e.norm), None))
        description = (first.message or first.title or first.event) + norm_text
        result.append(Violation(
            type=first.title or first.event, timecode_sec=round(first.t_start, 2), description=description,
            evidence_frames=[EvidenceFrame(timestamp_sec=round(f["t"], 2),
                                           image_base64=base64.b64encode(storage.get_bytes(f["key"])).decode())
                             for f in frames],
            rule=rule,
        ))
    return result


# --- API жюри и ботов (ключ) ---------------------------------------------------------------------------------------

@router.post("/analyze", response_model=AnalyzeResponse, tags=["jury"])
async def analyze(file: UploadFile, db: Db, who: Annotated[str, Depends(require_key)]) -> AnalyzeResponse:
    _, job = await ingest(file, db, source="api", requested_by=who)
    return AnalyzeResponse(job_id=job.id)


@router.get("/jobs/{job_id}", response_model=JobResponse, response_model_exclude_none=True, tags=["jury"])
def get_job(job_id: str, db: Db, _: Annotated[str, Depends(require_key)]) -> JobResponse:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    _expire(db, job)
    if job.status in ("queued", "processing"):
        return JobResponse(job_id=job.id, status="processing")
    if job.status == "failed":
        return JobResponse(job_id=job.id, status="failed", error=job.error)
    violations = violations_for(db, job)
    return JobResponse(job_id=job.id, status="done", verdict="violation" if violations else "no_violation",
                       violations=violations)


# --- видео (интерфейс) ---------------------------------------------------------------------------------------------

@router.post("/videos", response_model=UploadOut, tags=["videos"])
async def upload_video(db: Db, file: Annotated[UploadFile, File()],
                       recorded_at: Annotated[datetime | None, Form()] = None) -> UploadOut:
    video, job = await ingest(file, db, source="upload", requested_by="ui", recorded_at=recorded_at)
    return UploadOut(video=video_out(db, video), job=job_out(job))


@router.get("/videos", response_model=list[VideoOut], tags=["videos"])
def list_videos(db: Db, date_from: date | None = None, date_to: date | None = None,
                limit: Annotated[int, Query(le=500)] = 100) -> list[VideoOut]:
    q = select(Video).order_by(Video.recorded_at.desc()).limit(limit)
    if date_from:
        q = q.where(Video.recorded_at >= datetime.combine(date_from, datetime.min.time(), UTC))
    if date_to:
        q = q.where(Video.recorded_at < datetime.combine(date_to + timedelta(days=1), datetime.min.time(), UTC))
    return [video_out(db, v) for v in db.scalars(q).all()]


@router.get("/videos/calendar", response_model=list[CalendarDay], tags=["videos"])
def calendar(db: Db, month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")]) -> list[CalendarDay]:
    start = datetime.strptime(month + "-01", "%Y-%m-%d").replace(tzinfo=UTC)
    end = (start + timedelta(days=32)).replace(day=1)
    days: dict[str, CalendarDay] = {}
    for v in db.scalars(select(Video).where(Video.recorded_at >= start, Video.recorded_at < end)).all():
        key = v.recorded_at.date().isoformat()
        day = days.setdefault(key, CalendarDay(date=key, videos=0, violations=0))
        day.videos += 1
        job = last_job(db, v.id)
        if job and job.verdict == "violation":
            day.violations += 1
    return sorted(days.values(), key=lambda d: d.date)


def _video(db: Session, video_id: str) -> Video:
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail="video not found")
    return video


@router.get("/videos/{video_id}", response_model=VideoOut, tags=["videos"])
def get_video(video_id: str, db: Db) -> VideoOut:
    return video_out(db, _video(db, video_id))


@router.get("/videos/{video_id}/stream", tags=["videos"])
def stream_video(video_id: str, request: Request, db: Db) -> StreamingResponse:
    video = _video(db, video_id)
    body, headers, status = storage.stream(video.play_key or video.source_key, request.headers.get("range"))
    return StreamingResponse(body, status_code=status, headers=headers, media_type="video/mp4")


@router.get("/videos/{video_id}/frame", tags=["videos"], response_class=Response)
def video_frame(video_id: str, db: Db, t: Annotated[float, Query(ge=0)] = 0.0) -> Response:
    """JPEG кадра на момент t — подложка редактора боксов."""
    video = _video(db, video_id)
    data = media.frame_jpeg(storage.presign(video.play_key or video.source_key), t)
    if not data:
        raise HTTPException(status_code=416, detail="t outside the video")
    return Response(content=data, media_type="image/jpeg")


@router.post("/videos/{video_id}/analyze", response_model=JobOut, tags=["videos"])
async def reanalyze(video_id: str, db: Db) -> JobOut:
    _video(db, video_id)
    job = Job(id=uuid.uuid4().hex[:12], video_id=video_id, requested_by="ui", stage="queued")
    db.add(job)
    db.commit()
    progress.set_stage(job.id, "queued", 0.0)
    await temporal_client.start_analysis(job.id)
    return job_out(job)


# --- очередь -------------------------------------------------------------------------------------------------------

@router.get("/queue", response_model=QueueOut, tags=["queue"])
async def queue(db: Db) -> QueueOut:
    for job in db.scalars(select(Job).where(Job.status.in_(["queued", "processing"]))).all():
        _expire(db, job)
    day_ago = now() - timedelta(days=1)
    active = db.scalars(select(Job).where(Job.status.in_(["queued", "processing"])).order_by(Job.created_at)).all()
    done = db.scalars(select(Job).where(Job.status == "done", Job.finished_at >= day_ago)).all()
    durations = [(j.finished_at - j.started_at).total_seconds() for j in done if j.started_at and j.finished_at]
    avg = sum(durations) / len(durations) if durations else None
    jobs, waiting = [], 0
    for job in active:
        if job.status == "queued":
            waiting += 1
            jobs.append(job_out(job, position=waiting, eta=round(waiting * (avg or 60), 1)))
        else:
            jobs.append(job_out(job))
    return QueueOut(
        queued=sum(j.status == "queued" for j in active), processing=sum(j.status == "processing" for j in active),
        done_24h=len(done),
        failed_24h=db.scalar(select(func.count()).select_from(Job).where(Job.status == "failed",
                                                                          Job.finished_at >= day_ago)) or 0,
        avg_duration_sec=round(avg, 1) if avg else None,
        workers=[QueueWorker(**w) for w in await temporal_client.workers()], jobs=jobs,
    )


@router.get("/queue/jobs", response_model=list[JobOut], tags=["queue"])
def list_jobs(db: Db, status: str | None = None, limit: Annotated[int, Query(le=500)] = 100) -> list[JobOut]:
    q = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if status:
        q = q.where(Job.status == status)
    return [job_out(j) for j in db.scalars(q).all()]


@router.get("/queue/jobs/{job_id}", response_model=JobOut, tags=["queue"])
def queue_job(job_id: str, db: Db) -> JobOut:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    _expire(db, job)
    return job_out(job)


# --- события и аудит -----------------------------------------------------------------------------------------------

@router.get("/videos/{video_id}/events", response_model=list[EventOut], tags=["events"])
def video_events(video_id: str, db: Db, status: str | None = None, job_id: str | None = None) -> list[EventOut]:
    _video(db, video_id)
    if job_id is None:
        job = db.scalar(select(Job).where(Job.video_id == video_id, Job.status == "done")
                        .order_by(Job.finished_at.desc()).limit(1))
        if job is None:
            return []
        job_id = job.id
    q = select(Event).where(Event.job_id == job_id).order_by(Event.t_start)
    if status:
        q = q.where(Event.status == status)
    return [event_out(e) for e in db.scalars(q).all()]


@router.get("/events/{event_id}/evidence/{n}", tags=["events"], response_class=Response)
def evidence(event_id: int, n: int, db: Db) -> Response:
    e = db.get(Event, event_id)
    if e is None or n >= len(e.evidence or []):
        raise HTTPException(status_code=404, detail="evidence not found")
    return Response(content=storage.get_bytes(e.evidence[n]["key"]), media_type="image/jpeg")


@router.get("/review", response_model=list[EventOut], tags=["events"])
def review_queue(db: Db, state: str = "pending", status: str | None = None,
                 limit: Annotated[int, Query(le=500)] = 200) -> list[EventOut]:
    q = select(Event).where(Event.review == state).order_by(Event.id.desc()).limit(limit)
    if status:
        q = q.where(Event.status == status)
    return [event_out(e) for e in db.scalars(q).all()]


@router.post("/events/{event_id}/review", response_model=EventOut, tags=["events"])
def review_event(event_id: int, body: ReviewIn, db: Db) -> EventOut:
    e = db.get(Event, event_id)
    if e is None:
        raise HTTPException(status_code=404, detail="event not found")
    e.review, e.review_comment, e.reviewed_at = body.decision, body.comment, now()
    db.commit()
    return event_out(e)


# --- боксы и дообучение --------------------------------------------------------------------------------------------

def box_out(b: Box) -> BoxOut:
    return BoxOut(id=b.id, t=b.t, cls=b.cls, bbox=[b.x1, b.y1, b.x2, b.y2], attributes=b.attributes or {},
                  track_id=b.track_id, conf=b.conf, source=b.source)


@router.get("/videos/{video_id}/boxes", response_model=list[BoxOut], tags=["boxes"])
def video_boxes(video_id: str, db: Db, t_from: float = 0.0, t_to: float = 1e9) -> list[BoxOut]:
    """Боксы на отрезке; если на кадре есть правка человека, боксы модели этого кадра не отдаются."""
    _video(db, video_id)
    boxes = db.scalars(select(Box).where(Box.video_id == video_id, Box.deleted.is_(False), Box.t >= t_from,
                                         Box.t <= t_to).order_by(Box.t, Box.id)).all()
    human_frames = {round(b.t, 3) for b in boxes if b.source == "human"}
    return [box_out(b) for b in boxes if b.source == "human" or round(b.t, 3) not in human_frames]


@router.put("/videos/{video_id}/boxes", response_model=list[BoxOut], tags=["boxes"])
def save_boxes(video_id: str, body: list[BoxIn], db: Db, t: Annotated[float, Query(ge=0)]) -> list[BoxOut]:
    """Правка человека: полный список боксов кадра t заменяет прошлую правку этого кадра."""
    _video(db, video_id)
    classes = {label["name"] for label in meta()["labels"]}
    unknown = {b.cls for b in body} - classes
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown classes: {sorted(unknown)}")
    for old in db.scalars(select(Box).where(Box.video_id == video_id, Box.source == "human",
                                            Box.t.between(t - 5e-4, t + 5e-4))).all():
        old.deleted = True
    saved = [Box(video_id=video_id, t=t, cls=b.cls, x1=b.bbox[0], y1=b.bbox[1], x2=b.bbox[2], y2=b.bbox[3],
                 attributes=b.attributes, track_id=b.track_id, source="human") for b in body]
    db.add_all(saved)
    db.commit()
    return [box_out(b) for b in saved]


@router.get("/training/export", tags=["boxes"])
def training_export(db: Db) -> dict:
    """COCO из кадров с правками человека — для дообучения детектора и импорта в CVAT.

    Картинка кадра: GET /api/v1/videos/{video_id}/frame?t=...; имя файла — <video_id>_<мс>.jpg.
    """
    names = [label["name"] for label in meta()["labels"]]
    boxes = db.scalars(select(Box).where(Box.source == "human", Box.deleted.is_(False)).order_by(Box.video_id, Box.t)).all()
    images: dict[tuple[str, float], dict] = {}
    annotations = []
    for b in boxes:
        key = (b.video_id, round(b.t, 3))
        if key not in images:
            video = db.get(Video, b.video_id)
            images[key] = {"id": len(images) + 1, "file_name": f"{b.video_id}_{round(b.t * 1000):06d}.jpg",
                           "width": video.width, "height": video.height, "video_id": b.video_id, "t": b.t,
                           "frame_url": f"/api/v1/videos/{b.video_id}/frame?t={b.t}"}
        annotations.append({"id": len(annotations) + 1, "image_id": images[key]["id"],
                            "category_id": names.index(b.cls) + 1, "bbox": [b.x1, b.y1, b.x2 - b.x1, b.y2 - b.y1],
                            "area": (b.x2 - b.x1) * (b.y2 - b.y1), "iscrowd": 0, "attributes": b.attributes or {}})
    return {"images": list(images.values()), "annotations": annotations,
            "categories": [{"id": i + 1, "name": n} for i, n in enumerate(names)]}


# --- токены ботов --------------------------------------------------------------------------------------------------

def token_out(t: ApiToken) -> TokenOut:
    return TokenOut(id=t.id, name=t.name, prefix=t.prefix, created_at=t.created_at, last_used_at=t.last_used_at,
                    revoked_at=t.revoked_at)


@router.get("/tokens", response_model=list[TokenOut], tags=["tokens"])
def list_tokens(db: Db) -> list[TokenOut]:
    return [token_out(t) for t in db.scalars(select(ApiToken).order_by(ApiToken.created_at.desc())).all()]


@router.post("/tokens", response_model=TokenCreated, tags=["tokens"])
def create_token(body: TokenIn, db: Db) -> TokenCreated:
    secret = new_token()
    token = ApiToken(name=body.name, prefix=secret[:12], token_hash=hash_token(secret))
    db.add(token)
    db.commit()
    return TokenCreated(**token_out(token).model_dump(), token=secret)


@router.delete("/tokens/{token_id}", response_model=TokenOut, tags=["tokens"])
def revoke_token(token_id: int, db: Db) -> TokenOut:
    token = db.get(ApiToken, token_id)
    if token is None:
        raise HTTPException(status_code=404, detail="token not found")
    token.revoked_at = token.revoked_at or now()
    db.commit()
    return token_out(token)


# --- камеры и справочники ------------------------------------------------------------------------------------------

@router.get("/cameras", tags=["meta"])
def cameras() -> dict:
    return {"items": [], "coming_soon": True}


@router.get("/meta", tags=["meta"])
def get_meta() -> dict:
    m = meta()
    return {
        "classes": m["labels"],
        "event_statuses": ["expected", "warning", "violation"],
        "review_states": ["pending", "approved", "rejected", "none"],
        "job_statuses": ["queued", "processing", "done", "failed"],
        "stages": progress.STAGES,
        "norms": m["norms"],
        "modes": ["view", "audit", "training"],
    }
