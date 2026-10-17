"""Shared deadlines and conditional state transitions for API requests and workers."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from backend import config, deadlines
from backend.db import Job, now

ACTIVE = ("queued", "processing")
TIMEOUT_ERROR = "timeout: обработка не уложилась в лимит"


def deadline(job: Job) -> datetime | None:
    # An archive batch queues its children serially. Jury uploads always count queue time.
    since = job.started_at if job.requested_by.startswith("rerun:") else job.created_at
    if since is None:
        return None
    return (since if since.tzinfo else since.replace(tzinfo=UTC)) + timedelta(seconds=config.JOB_TIMEOUT_SEC)


def seconds_left(job: Job, at: datetime | None = None) -> float:
    end = deadline(job)
    return (end - (at or now())).total_seconds() if end is not None else config.JOB_TIMEOUT_SEC


def expired(job: Job, at: datetime | None = None) -> bool:
    return seconds_left(job, at) <= 0


def mark_failed(db: Session, job_id: str, error: str, *, statuses: tuple[str, ...] = ACTIVE) -> bool:
    """Compare-and-set prevents timeout/late retries from overwriting a terminal result."""
    deadlines.remaining()
    result = db.execute(update(Job).where(Job.id == job_id, Job.status.in_(statuses)).values(
        status="failed", stage="failed", error=error[:2000], verdict=None, finished_at=now()))
    try:
        deadlines.remaining()
    except TimeoutError:
        db.rollback()
        raise
    db.commit()
    return bool(result.rowcount)
