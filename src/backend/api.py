"""Маршруты /api/v1: API жюри и ботов (по ключу) и методы интерфейса (открыты). Контракт — docs/API.md."""

import asyncio
import base64
import json
import shutil
import tempfile
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import catalog, config, media, progress, storage, temporal_client
from backend.auth import hash_token, new_token, require_key
from backend.db import (
    ApiToken, Box, Event, EventSample, EventType, Job, PipelineRun, SessionLocal, Video, get_db, now,
)
from backend.schema import (
    AnalyzeResponse, BoxIn, BoxOut, CalendarDay, ErrorOut, EventOut, EventTypeIn, EventTypeOut, EvidenceFrame,
    HealthOut, JobOut, JobProgress, JobResponse, JobSummary, ManualEventIn, PipelineOut, QueueOut, QueueWorker,
    ReviewIn, Rule, SampleIn, SampleOut, SampleReviewIn, TokenCreated, TokenIn, TokenOut, UploadOut, VideoOut,
    Violation,
)

router = APIRouter(prefix="/api/v1")
Db = Annotated[Session, Depends(get_db)]
VIOLATION_MERGE_SEC = 5.0  # события одного типа ближе 5 с — один эпизод нарушения


# --- справочники ---------------------------------------------------------------------------------------------------

def load_meta() -> dict:
    # классы боксов — только рамки; тег event (система событий в CVAT) — не класс
    labels = [label for label in json.loads((config.META_DIR / "cvat" / "labels.json").read_text())
              if label.get("type", "rectangle") != "tag"]
    return {"labels": labels, "norms": catalog.load_norms()}


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
    """Задача дольше лимита. Пересчёт архива запускает задачи по одной (RerunArchive): ожидание своей очереди
    в лимит не входит, отсчёт — от старта обработки."""
    since = job.started_at or (None if job.requested_by.startswith("rerun:") else job.created_at)
    if since is None or job.status not in ("queued", "processing"):
        return False
    since = since if since.tzinfo else since.replace(tzinfo=UTC)
    return now() - since > timedelta(seconds=config.JOB_TIMEOUT_SEC + 30)


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
        type_id=catalog.event_type_id(e.event, e.status, e.title, e.rules, e.source or "model"),
        source=e.source or "model",
    )


def norm_rule(norm_id: str | None) -> tuple[Rule | None, str]:
    norm = meta()["norms"].get(norm_id) if norm_id else None
    if not norm:
        return None, ""
    return (Rule(document=norm.get("act") or norm.get("document", ""), clause=norm.get("clause"), url=norm.get("url")),
            f" Норматив: {norm.get('short', norm.get('clause', ''))} ({norm.get('url', '')}).")


def violation_groups(db: Session, job: Job) -> list[list[Event]]:
    """События-нарушения одного типа ближе VIOLATION_MERGE_SEC — один эпизод; без кадра-доказательства эпизод
    в ответ жюри не попадает."""
    events = db.scalars(select(Event).where(Event.job_id == job.id, Event.status == "violation")
                        .order_by(Event.t_start)).all()
    groups: list[list[Event]] = []
    for e in events:
        if groups and groups[-1][0].title == e.title and e.t_start - groups[-1][-1].t_start <= VIOLATION_MERGE_SEC:
            groups[-1].append(e)
        else:
            groups.append([e])
    return [g for g in groups if any(e.evidence for e in g)]


def violations_for(db: Session, job: Job) -> list[Violation]:
    result = []
    for group in violation_groups(db, job):
        first = group[0]
        frames = [ev for e in group for ev in (e.evidence or [])][:3]
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


def queue_snapshot(db: Session) -> dict:
    """Активные задачи по порядку, место и ожидание для queued (по средней длительности за сутки), счётчики."""
    for job in db.scalars(select(Job).where(Job.status.in_(["queued", "processing"]))).all():
        _expire(db, job)
    day_ago = now() - timedelta(days=1)
    active = db.scalars(select(Job).where(Job.status.in_(["queued", "processing"])).order_by(Job.created_at)).all()
    done = db.scalars(select(Job).where(Job.status == "done", Job.finished_at >= day_ago)).all()
    durations = [(j.finished_at - j.started_at).total_seconds() for j in done if j.started_at and j.finished_at]
    avg = sum(durations) / len(durations) if durations else None
    places, waiting = {}, 0
    for job in active:
        if job.status == "queued":
            waiting += 1
            places[job.id] = (waiting, round(waiting * (avg or 60), 1))
    failed = db.scalar(select(func.count()).select_from(Job).where(Job.status == "failed", Job.finished_at >= day_ago))
    return {"active": active, "places": places, "avg": round(avg, 1) if avg else None, "done_24h": len(done),
            "failed_24h": failed or 0, "queued": sum(j.status == "queued" for j in active),
            "processing": sum(j.status == "processing" for j in active)}


def jury_result(job_id: str) -> JobResponse:
    """Ответ жюри по задаче — своя сессия: длинный опрос читает БД заново на каждом шаге."""
    with SessionLocal() as db:
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


FRAME_EXAMPLE = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcp…"
JOB_EXAMPLES = {
    "processing": {"summary": "В очереди или в работе", "value": {"job_id": "3f2a9c1e0b7d", "status": "processing"}},
    "violation": {"summary": "Готово: нарушение", "value": {
        "job_id": "3f2a9c1e0b7d", "status": "done", "verdict": "violation", "violations": [{
            "type": "Деньги предыдущего клиента на кассовом столе", "timecode_sec": 32.27,
            "description": "32.3 с: Клиент входит в помещение. На рабочем месте кассира только наличные текущего "
                           "клиента, ранее принятые хранятся в сейфе. Норматив: Правила №231, п.20 "
                           "(https://adilet.zan.kz/rus/docs/V1900019680#z111).",
            "evidence_frames": [{"timestamp_sec": 32.27, "image_base64": FRAME_EXAMPLE}],
            "rule": {"document": "Постановление Правления НБРК от 29 ноября 2019 года № 231", "clause": "пункт 20",
                     "url": "https://adilet.zan.kz/rus/docs/V1900019680#z111"}}]}},
    "clean": {"summary": "Готово: нарушений нет",
              "value": {"job_id": "3f2a9c1e0b7d", "status": "done", "verdict": "no_violation", "violations": []}},
    "failed": {"summary": "Ошибка обработки", "value": {"job_id": "3f2a9c1e0b7d", "status": "failed",
                                                        "error": "timeout: обработка не уложилась в лимит"}},
}
ERRORS = {401: {"model": ErrorOut, "description": "Нет ключа или ключ неверный"},
          404: {"model": ErrorOut, "description": "Задача не найдена"}}
UPLOAD_ERRORS = {401: ERRORS[401], 413: {"model": ErrorOut, "description": "Файл больше 200 МБ"},
                 415: {"model": ErrorOut, "description": "Не .mov в поле file"},
                 422: {"model": ErrorOut, "description": "Файл не читается как видео"}}


# --- API жюри и ботов (ключ) ---------------------------------------------------------------------------------------

@router.post("/analyze", response_model=AnalyzeResponse, tags=["jury"], responses=UPLOAD_ERRORS,
             summary="Загрузить видео на проверку")
async def analyze(file: Annotated[UploadFile, File(description="Видео .mov, до 200 МБ")], db: Db,
                  who: Annotated[str, Depends(require_key)]) -> AnalyzeResponse:
    """Шаг 1: видео уходит в очередь, в ответ — `job_id`. Результат — `GET /api/v1/jobs/{job_id}`."""
    _, job = await ingest(file, db, source="api", requested_by=who)
    return AnalyzeResponse(job_id=job.id)


@router.get("/jobs/{job_id}", response_model=JobResponse, response_model_exclude_none=True, tags=["jury"],
            summary="Статус и результат", responses={
                200: {"content": {"application/json": {"examples": JOB_EXAMPLES}},
                      "headers": {"Retry-After": {"description": "При processing: через сколько секунд спросить снова",
                                                  "schema": {"type": "integer"}}}}, **ERRORS})
async def get_job(job_id: str, response: Response, _: Annotated[str, Depends(require_key)],
                  wait: Annotated[float, Query(ge=0, le=60, description="Длинный опрос: подождать до N секунд, пока "
                                                                       "задача не выйдет из processing")] = 0
                  ) -> JobResponse:
    """Шаг 2: `processing`, пока видео в очереди или в работе; затем `done` с `verdict` и `violations` или
    `failed` с `error`. С `wait` ответ приходит сразу по готовности — меньше запросов при опросе."""
    deadline = time.monotonic() + wait
    while True:
        result = await run_in_threadpool(jury_result, job_id)
        if result.status != "processing" or time.monotonic() >= deadline:
            break
        await asyncio.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    response.headers["Cache-Control"] = "no-store"
    if result.status == "processing":
        response.headers["Retry-After"] = "2"
    return result


@router.get("/jobs/{job_id}/progress", response_model=JobProgress, tags=["jury"], responses=ERRORS,
            summary="Ход обработки")
def job_progress(job_id: str, db: Db, _: Annotated[str, Depends(require_key)]) -> JobProgress:
    """Где задача: место в очереди и ожидание (для queued), этап и доля выполнения (для processing)."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    place = queue_snapshot(db)["places"].get(job.id, (None, None))
    p = progress.get(job.id) or {}
    done = job.status == "done"
    return JobProgress(job_id=job.id, status=job.status, stage="done" if done else p.get("stage", job.stage),
                       progress=1.0 if done else p.get("progress"), queue_position=place[0],
                       eta_sec=place[1], created_at=job.created_at, started_at=job.started_at,
                       finished_at=job.finished_at, error=job.error)


@router.get("/jobs", response_model=list[JobSummary], tags=["jury"], responses={401: ERRORS[401]},
            summary="Мои задачи")
def my_jobs(db: Db, who: Annotated[str, Depends(require_key)],
            limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[JobSummary]:
    """Задачи, отправленные этим ключом, новые сверху."""
    out = []
    for job in db.scalars(select(Job).where(Job.requested_by == who).order_by(Job.created_at.desc()).limit(limit)):
        _expire(db, job)
        video = db.get(Video, job.video_id)
        status = "processing" if job.status in ("queued", "processing") else job.status
        out.append(JobSummary(job_id=job.id, filename=video.filename if video else "", status=status,
                              verdict=job.verdict if status == "done" else None,
                              violations=len(violation_groups(db, job)) if status == "done" else None,
                              created_at=job.created_at, finished_at=job.finished_at))
    return out


@router.get("/health", response_model=HealthOut, tags=["jury"], summary="Готовность сервиса")
async def health(db: Db) -> HealthOut:
    """Без ключа: слушают ли очереди обработчики и сколько задач ждёт. `degraded` — видео сейчас не обработается."""
    snap = await run_in_threadpool(queue_snapshot, db)
    workers = await temporal_client.workers()
    counts = {q: sum(w["queue"] == q for w in workers) for q in (config.ANALYZE_QUEUE, config.CV_QUEUE)}
    return HealthOut(status="ok" if counts[config.CV_QUEUE] else "degraded", workers=counts, queued=snap["queued"],
                     processing=snap["processing"], avg_duration_sec=snap["avg"])


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
    snap = await run_in_threadpool(queue_snapshot, db)
    jobs = [job_out(j, *snap["places"].get(j.id, (None, None))) for j in snap["active"]]
    return QueueOut(queued=snap["queued"], processing=snap["processing"], done_24h=snap["done_24h"],
                    failed_24h=snap["failed_24h"], avg_duration_sec=snap["avg"],
                    workers=[QueueWorker(**w) for w in await temporal_client.workers()], jobs=jobs)


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


def latest_done_jobs(db: Session) -> list[str]:
    """id последнего готового анализа каждого видео."""
    rows = db.execute(select(Job.video_id, Job.id, Job.finished_at).where(Job.status == "done")).all()
    best: dict[str, tuple] = {}
    for video_id, job_id, finished in rows:
        if video_id not in best or (finished and (best[video_id][1] is None or finished > best[video_id][1])):
            best[video_id] = (job_id, finished)
    return [job_id for job_id, _ in best.values()]


@router.get("/events", response_model=list[EventOut], tags=["events"])
def all_events(db: Db, video_id: str | None = None, status: str | None = None) -> list[EventOut]:
    """События последнего готового анализа по всем видео (или одному) и ручные события — для интерфейса одним запросом."""
    q = select(Event).where((Event.job_id.in_(latest_done_jobs(db))) | (Event.source == "human"))
    if video_id:
        q = q.where(Event.video_id == video_id)
    if status:
        q = q.where(Event.status == status)
    return [event_out(e) for e in db.scalars(q.order_by(Event.video_id, Event.t_start)).all()]


def type_by_id(db: Session, type_id: str) -> dict:
    found = next((t for t in catalog.standard_types() if t["id"] == type_id), None)
    if found:
        return found
    custom = db.get(EventType, type_id.removeprefix("custom:"))
    if custom is None:
        raise HTTPException(status_code=404, detail="тип события не найден")
    return event_type_out(db, custom).model_dump()


def apply_manual(e: Event, body: ManualEventIn, db: Session) -> None:
    t = type_by_id(db, body.type_id)
    if t["status"] != "published":
        raise HTTPException(status_code=409, detail="выберите опубликованный тип события")
    if body.end < body.start:
        raise HTTPException(status_code=422, detail="конец раньше начала")
    e.video_id, e.t_start, e.t_end = body.video_id, body.start, body.end
    e.event, e.status, e.title = "manual", ("expected" if t["kind"] == "quality" else t["kind"]), t["title"]
    e.rules, e.norm, e.message = [body.type_id], t.get("norm"), body.description or t["description"]


@router.post("/events", response_model=EventOut, tags=["events"])
def create_manual_event(body: ManualEventIn, db: Db) -> EventOut:
    """Событие, отмеченное человеком: тип из каталога, отрезок записи, описание. Сразу подтверждено."""
    _video(db, body.video_id)
    job = last_job(db, body.video_id)
    if job is None:
        raise HTTPException(status_code=409, detail="у записи нет анализа")
    e = Event(job_id=job.id, source="human", review="approved", evidence=[], reviewed_at=now())
    apply_manual(e, body, db)
    db.add(e)
    db.commit()
    return event_out(e)


def _manual(db: Session, event_id: int) -> Event:
    e = db.get(Event, event_id)
    if e is None:
        raise HTTPException(status_code=404, detail="event not found")
    if e.source != "human":
        raise HTTPException(status_code=409, detail="менять и удалять можно только события, добавленные человеком")
    return e


@router.put("/events/{event_id}", response_model=EventOut, tags=["events"])
def update_manual_event(event_id: int, body: ManualEventIn, db: Db) -> EventOut:
    e = _manual(db, event_id)
    _video(db, body.video_id)
    apply_manual(e, body, db)
    db.commit()
    return event_out(e)


@router.delete("/events/{event_id}", status_code=204, tags=["events"])
def delete_manual_event(event_id: int, db: Db) -> Response:
    db.delete(_manual(db, event_id))
    db.commit()
    return Response(status_code=204)


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
def video_boxes(video_id: str, db: Db, t_from: float = 0.0, t_to: float = 1e9,
                step: Annotated[float, Query(ge=0)] = 0.0) -> list[BoxOut]:
    """Боксы на отрезке; если на кадре есть правка человека, боксы модели этого кадра не отдаются.
    step > 0 — боксы модели только с кадров не чаще чем раз в step секунд (интерфейс опрашивает часто);
    правки человека отдаются все."""
    _video(db, video_id)
    boxes = db.scalars(select(Box).where(Box.video_id == video_id, Box.deleted.is_(False), Box.t >= t_from,
                                         Box.t <= t_to).order_by(Box.t, Box.id)).all()
    human_frames = {round(b.t, 3) for b in boxes if b.source == "human"}
    keep_frames: set[float] = set()
    if step:
        last = None
        for t in sorted({round(b.t, 3) for b in boxes if b.source != "human"}):
            if last is None or t - last >= step - 1e-6:
                keep_frames.add(t)
                last = t
    return [box_out(b) for b in boxes if b.source == "human" or (
        round(b.t, 3) not in human_frames and (not step or round(b.t, 3) in keep_frames))]


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


# --- типы событий: стандартные и собственные ------------------------------------------------------------------------

PUBLISH_MIN_VIDEOS = 10  # собственный тип публикуется после подтверждённых примеров из 10 разных записей


def event_type_out(db: Session, t: EventType) -> EventTypeOut:
    samples = db.scalars(select(EventSample).where(EventSample.type_id == t.id).order_by(EventSample.id)).all()
    return EventTypeOut(id=f"custom:{t.id}", title=t.title, description=t.description, observable=t.observable,
                        kind=t.kind, custom=True, status=t.status,
                        samples=[SampleOut(id=x.id, video_id=x.video_id, start=x.start, end=x.end, review=x.review)
                                 for x in samples])


def _custom(db: Session, type_id: str) -> EventType:
    t = db.get(EventType, type_id.removeprefix("custom:"))
    if t is None:
        raise HTTPException(status_code=404, detail="тип события не найден")
    return t


def _add_sample(db: Session, t: EventType, body: SampleIn) -> None:
    """Одна запись — не больше одного действующего примера типа; отклонённый можно заменить."""
    _video(db, body.video_id)
    if body.end < body.start:
        raise HTTPException(status_code=422, detail="конец раньше начала")
    active = db.scalar(select(EventSample).where(EventSample.type_id == t.id, EventSample.video_id == body.video_id,
                                                 EventSample.review != "rejected"))
    if active is not None:
        raise HTTPException(status_code=409, detail="у этой записи уже есть пример этого типа")
    db.add(EventSample(type_id=t.id, video_id=body.video_id, start=body.start, end=body.end))


@router.get("/event-types", response_model=list[EventTypeOut], tags=["event-types"])
def event_types(db: Db) -> list[EventTypeOut]:
    custom = db.scalars(select(EventType).order_by(EventType.created_at)).all()
    return [EventTypeOut(**t) for t in catalog.standard_types()] + [event_type_out(db, t) for t in custom]


@router.post("/event-types", response_model=EventTypeOut, tags=["event-types"])
def create_event_type(body: EventTypeIn, db: Db) -> EventTypeOut:
    t = EventType(id=uuid.uuid4().hex[:12], title=body.title, description=body.description,
                  observable=body.observable, kind=body.kind, status="draft")
    db.add(t)
    db.flush()
    _add_sample(db, t, body.first_sample)
    db.commit()
    return event_type_out(db, t)


@router.post("/event-types/{type_id}/samples", response_model=EventTypeOut, tags=["event-types"])
def add_sample(type_id: str, body: SampleIn, db: Db) -> EventTypeOut:
    t = _custom(db, type_id)
    _add_sample(db, t, body)
    db.commit()
    return event_type_out(db, t)


@router.post("/event-types/{type_id}/samples/{sample_id}/review", response_model=EventTypeOut, tags=["event-types"])
def review_sample(type_id: str, sample_id: int, body: SampleReviewIn, db: Db) -> EventTypeOut:
    t = _custom(db, type_id)
    sample = db.get(EventSample, sample_id)
    if sample is None or sample.type_id != t.id:
        raise HTTPException(status_code=404, detail="пример не найден")
    sample.review = body.decision
    db.commit()
    return event_type_out(db, t)


@router.post("/event-types/{type_id}/publish", response_model=EventTypeOut, tags=["event-types"])
def publish_event_type(type_id: str, db: Db) -> EventTypeOut:
    t = _custom(db, type_id)
    videos = set(db.scalars(select(EventSample.video_id).where(EventSample.type_id == t.id,
                                                               EventSample.review == "approved")).all())
    if len(videos) < PUBLISH_MIN_VIDEOS:
        raise HTTPException(status_code=409, detail=f"нужны подтверждённые примеры из {PUBLISH_MIN_VIDEOS} разных "
                                                    f"записей, сейчас {len(videos)}")
    t.status = "published"
    db.commit()
    return event_type_out(db, t)


# --- конвейер: датасет, обучение, пересчёт архива ----------------------------------------------------------------

def _last_run(db: Session, kind: str) -> PipelineRun | None:
    return db.scalar(select(PipelineRun).where(PipelineRun.kind == kind).order_by(PipelineRun.id.desc()).limit(1))


@router.get("/pipeline", response_model=PipelineOut, tags=["pipeline"])
def pipeline(db: Db) -> PipelineOut:
    dataset, rerun = _last_run(db, "dataset"), _last_run(db, "rerun")
    last_done = db.scalar(select(Job).where(Job.status == "done").order_by(Job.finished_at.desc()).limit(1))
    out = PipelineOut(dataset_version=dataset.version if dataset else None,
                      dataset_stage=dataset.status if dataset else "idle",
                      model_version=(last_done.model or last_done.detector) if last_done else None,
                      total_videos=db.scalar(select(func.count()).select_from(Video)) or 0)
    if rerun:
        jobs = db.scalars(select(Job).where(Job.requested_by == f"rerun:{rerun.id}")).all()
        for job in jobs:
            _expire(db, job)
        out.total_videos = rerun.total
        out.processed_videos = sum(j.status in ("done", "failed") for j in jobs)
        failed = [j for j in jobs if j.status == "failed"]
        out.batch_stage = "running" if out.processed_videos < rerun.total else ("failed" if failed else "ready")
        out.error = failed[0].error if failed else None
    return out


@router.post("/pipeline/dataset", response_model=PipelineOut, tags=["pipeline"])
def build_dataset(db: Db) -> PipelineOut:
    """Версия набора для дообучения: COCO из правок боксов человеком + примеры опубликованных собственных типов."""
    version = "ds-" + now().strftime("%Y%m%d-%H%M%S")
    published = db.scalars(select(EventType).where(EventType.status == "published")).all()
    data = {"version": version, "boxes": training_export(db),
            "event_types": [event_type_out(db, t).model_dump() for t in published]}
    run = PipelineRun(kind="dataset", status="ready", version=version, total=len(data["boxes"]["images"]))
    try:
        storage.put_bytes(f"datasets/{version}.json", json.dumps(data, ensure_ascii=False, default=str).encode(),
                          "application/json")
    except Exception as e:  # S3 недоступен — версия не собрана
        run.status, run.error = "failed", str(e)[:500]
    db.add(run)
    db.commit()
    return pipeline(db)


@router.post("/pipeline/train", tags=["pipeline"])
def train_model() -> None:
    raise HTTPException(status_code=409, detail="Обучение детектора запускается на GPU вручную, чтобы не останавливать "
                                                "анализ: docker compose --profile tools run --rm cv python -m "
                                                "cv.train_yolo --dataset det_l1 (см. computer-vision/README.md)")


@router.post("/pipeline/rerun", response_model=PipelineOut, tags=["pipeline"])
async def rerun_archive(db: Db) -> PipelineOut:
    """Повторный анализ всех записей текущей моделью — по одной записи, чтобы не задерживать видео жюри."""
    videos = db.scalars(select(Video).order_by(Video.uploaded_at)).all()
    run = PipelineRun(kind="rerun", status="running", total=len(videos))
    db.add(run)
    db.commit()
    job_ids = []
    for video in videos:
        job = Job(id=uuid.uuid4().hex[:12], video_id=video.id, requested_by=f"rerun:{run.id}", stage="queued")
        db.add(job)
        db.commit()
        progress.set_stage(job.id, "queued", 0.0)
        job_ids.append(job.id)
    # по одной записи (RerunArchive): пересчёт держит один обработчик, остальные свободны для жюри
    await temporal_client.start_rerun(run.id, job_ids)
    return pipeline(db)


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
