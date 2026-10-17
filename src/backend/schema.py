"""Модели ответов API. Ответ жюри (раздел 10 ТЗ) — поля не переименовывать: проверка автоматическая."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

JobStatus = Literal["queued", "processing", "done", "failed"]
Verdict = Literal["violation", "no_violation"]
EventStatus = Literal["expected", "warning", "violation"]
Review = Literal["pending", "approved", "rejected", "none"]


# --- API жюри ---------------------------------------------------------------------------------------------------

class EvidenceFrame(BaseModel):
    timestamp_sec: float
    image_base64: str  # JPEG без префикса data:


class Rule(BaseModel):
    """Нормативный источник. В схеме ТЗ такого поля нет, поэтому ссылка дублируется в конце description."""

    document: str
    clause: str | None = None
    url: str | None = None


class Violation(BaseModel):
    type: str
    timecode_sec: float
    description: str
    evidence_frames: list[EvidenceFrame] = Field(min_length=1, max_length=3)
    rule: Rule | None = None


class AnalyzeResponse(BaseModel):
    job_id: str


class JobResponse(BaseModel):
    """Жюри видит processing и для queued: статусов в ТЗ три."""

    job_id: str
    status: Literal["processing", "done", "failed"]
    verdict: Verdict | None = None
    violations: list[Violation] | None = None
    error: str | None = None


# --- интерфейс ---------------------------------------------------------------------------------------------------

class VideoOut(BaseModel):
    id: str
    filename: str
    duration: float | None
    fps: float | None
    width: int | None
    height: int | None
    source: str
    recorded_at: datetime
    uploaded_at: datetime
    stream_url: str  # /api/v1/videos/{id}/stream
    last_job: "JobOut | None" = None


class JobOut(BaseModel):
    id: str
    video_id: str
    status: JobStatus
    stage: str | None
    progress: float | None
    verdict: Verdict | None
    detector: str | None
    error: str | None
    requested_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    position: int | None = None  # место в очереди, для queued
    eta_sec: float | None = None


class UploadOut(BaseModel):
    video: VideoOut
    job: JobOut


class CalendarDay(BaseModel):
    date: str  # YYYY-MM-DD
    videos: int
    violations: int  # видео с вердиктом violation


class QueueWorker(BaseModel):
    queue: str
    identity: str
    last_seen: datetime | None


class QueueOut(BaseModel):
    queued: int
    processing: int
    done_24h: int
    failed_24h: int
    avg_duration_sec: float | None
    workers: list[QueueWorker]
    jobs: list[JobOut]  # queued + processing, по порядку очереди


class EventOut(BaseModel):
    id: int
    job_id: str
    video_id: str
    t_start: float
    t_end: float
    event: str
    actor: str | None
    object: str | None
    target: str | None
    scenario: str | None
    instance: int | None
    step: int | None
    status: EventStatus
    title: str | None
    rules: list[str]
    norm: str | None
    message: str | None
    evidence: list[dict]  # [{t, url}]
    review: Review
    review_comment: str | None
    reviewed_at: datetime | None
    type_id: str  # id типа из GET /event-types
    source: Literal["model", "human"] = "model"


class ManualEventIn(BaseModel):
    video_id: str
    type_id: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    description: str = ""


EventKind = Literal["violation", "warning", "quality", "expected"]


class SampleIn(BaseModel):
    video_id: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)


class SampleOut(SampleIn):
    id: int
    review: Literal["pending", "approved", "rejected"]


class EventTypeIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    observable: str = ""
    kind: EventKind
    first_sample: SampleIn


class EventTypeOut(BaseModel):
    id: str
    title: str
    description: str
    observable: str
    kind: EventKind
    custom: bool
    status: Literal["published", "draft"]
    samples: list[SampleOut] = []
    norm: str | None = None


class SampleReviewIn(BaseModel):
    decision: Literal["approved", "rejected"]


PipelineStage = Literal["idle", "running", "ready", "failed"]


class PipelineOut(BaseModel):
    dataset_version: str | None = None
    dataset_stage: PipelineStage = "idle"
    model_version: str | None = None
    training_stage: PipelineStage = "idle"
    batch_stage: PipelineStage = "idle"
    processed_videos: int = 0
    total_videos: int = 0
    error: str | None = None


class ReviewIn(BaseModel):
    decision: Literal["approved", "rejected"]
    comment: str | None = None


class BoxIn(BaseModel):
    cls: str
    bbox: list[float] = Field(min_length=4, max_length=4)  # x1, y1, x2, y2 в пикселях кадра
    attributes: dict = {}
    track_id: int | None = None


class BoxOut(BoxIn):
    id: int
    t: float
    conf: float | None
    source: Literal["stub", "model", "human"]


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class TokenOut(BaseModel):
    id: int
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class TokenCreated(TokenOut):
    token: str  # показывается один раз


VideoOut.model_rebuild()
