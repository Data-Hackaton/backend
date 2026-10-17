"""Модели ответов API. Ответ жюри (раздел 10 ТЗ) — поля не переименовывать: проверка автоматическая."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

JobStatus = Literal["queued", "processing", "done", "failed"]
Verdict = Literal["violation", "no_violation"]
EventStatus = Literal["expected", "warning", "violation"]
Review = Literal["pending", "approved", "rejected", "none"]


# --- API жюри ---------------------------------------------------------------------------------------------------

class EvidenceFrame(BaseModel):
    timestamp_sec: float = Field(ge=0, allow_inf_nan=False, description="Секунда кадра от начала видео")
    image_base64: str = Field(description="Кадр-доказательство: JPEG в base64, без префикса data:")


class Rule(BaseModel):
    """Нормативный источник. В схеме ТЗ такого поля нет, поэтому ссылка дублируется в конце description."""

    document: str = Field(description="Документ: акт НБРК или иной акт РК")
    clause: str | None = Field(None, description="Пункт")
    url: str | None = Field(None, description="Ссылка на adilet.zan.kz")


class Violation(BaseModel):
    type: str = Field(min_length=1, description="Тип нарушения")
    timecode_sec: float = Field(ge=0, allow_inf_nan=False, description="Таймкод события в секундах от начала видео")
    description: str = Field(min_length=1, description="Что видно на видео и почему это может быть нарушением; в конце — норматив")
    evidence_frames: list[EvidenceFrame] = Field(min_length=1, max_length=3,
                                                 description="1–3 кадра-доказательства; первый — на таймкоде")
    rule: Rule | None = Field(None, description="Сверх схемы ТЗ: прямой норматив, если он есть")


class AnalyzeResponse(BaseModel):
    job_id: str = Field(description="Идентификатор задачи для GET /api/v1/jobs/{job_id}")


class JobResponse(BaseModel):
    """Жюри видит processing и для queued: статусов в ТЗ три."""

    job_id: str
    status: Literal["processing", "done", "failed"] = Field(description="processing — в очереди или в работе")
    verdict: Verdict | None = Field(None, description="Только при done: violation или no_violation")
    violations: list[Violation] | None = Field(None, description="Только при done: список нарушений, пустой — если "
                                                                   "нарушений нет")
    error: str | None = Field(None, description="Только при failed: причина")

    @model_validator(mode="after")
    def complete_result(self):
        if self.status == "done":
            if self.verdict is None or self.violations is None:
                raise ValueError("done requires verdict and violations")
            expected = "violation" if self.violations else "no_violation"
            if self.verdict != expected:
                raise ValueError("verdict must match violations")
        elif self.verdict is not None or self.violations is not None:
            raise ValueError("only done may contain a result")
        return self


class ErrorOut(BaseModel):
    detail: str = Field(description="Причина ошибки")


class JobProgress(BaseModel):
    """Ход обработки для опроса: где задача в очереди и на каком этапе."""

    job_id: str
    status: JobStatus = Field(description="queued — ждёт свободного обработчика, processing — в работе")
    stage: str | None = Field(None, description="queued, downloading, detecting, events, evidence, publishing, done")
    progress: float | None = Field(None, description="Доля выполнения 0–1 текущей задачи")
    queue_position: int | None = Field(None, description="Место в очереди (для queued)")
    eta_sec: float | None = Field(None, description="Оценка ожидания до начала обработки, с")
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None = None


class JobSummary(BaseModel):
    job_id: str
    filename: str
    status: Literal["processing", "done", "failed"]
    verdict: Verdict | None
    violations: int | None = Field(description="Число нарушений в ответе (при done)")
    created_at: datetime
    finished_at: datetime | None


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"] = Field(description="degraded — нет обработчика видео или Temporal недоступен")
    workers: dict[str, int] = Field(description="Сколько воркеров слушают очереди analyze и cv-gpu")
    queued: int
    processing: int
    avg_duration_sec: float | None = Field(description="Средняя обработка за сутки, с")


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


class ProcedureStep(BaseModel):
    action: str
    observability: str | None = None  # partly_observable / context_required / not_visually_verifiable


class Procedure(BaseModel):
    """Процедура проверки темы из нормативного корпуса (reference/topics.yaml)."""
    id: str
    title: str
    check: list[str] = []  # что установить человеку
    limits: str | None = None  # исключения и границы вывода
    steps: list[ProcedureStep] = []


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
    procedures: list[Procedure] = []


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
