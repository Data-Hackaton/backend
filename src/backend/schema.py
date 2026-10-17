"""Ответ API по разделу 10 ТЗ. Поля из ТЗ не переименовывать: жюри проверяет автоматически."""

from typing import Literal

from pydantic import BaseModel, Field

JobStatus = Literal["processing", "done", "failed"]
Verdict = Literal["violation", "no_violation"]


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
    job_id: str
    status: JobStatus
    verdict: Verdict | None = None
    violations: list[Violation] | None = None
    error: str | None = None
