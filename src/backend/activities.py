"""Активности бэкенда в очереди analyze: пишут в Postgres, тяжёлое делает cv-worker."""

import json

from sqlalchemy import func, select, update
from temporalio import activity
from temporalio.exceptions import ApplicationError

from backend import deadlines, evidence, jobs, progress, storage
from backend.db import Box, Event, Job, SessionLocal, Video, now


@activity.defn(name="prepare")
def prepare(job_id: str) -> dict:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None or job.status not in jobs.ACTIVE:
            raise ApplicationError("job is missing or already finished", non_retryable=True)
        if jobs.expired(job):
            jobs.mark_failed(db, job_id, jobs.TIMEOUT_ERROR)
            raise ApplicationError(jobs.TIMEOUT_ERROR, non_retryable=True)
        video = db.get(Video, job.video_id)
        changed = db.execute(update(Job).where(Job.id == job_id, Job.status.in_(jobs.ACTIVE)).values(
            status="processing", stage="downloading", started_at=func.coalesce(Job.started_at, now())))
        if not changed.rowcount:
            db.rollback()
            raise ApplicationError("job already finished", non_retryable=True)
        db.commit()
        progress.set_stage(job_id, "downloading", 0.0)
        return {"job_id": job_id, "video_id": video.id, "source_key": video.source_key, "sha256": video.sha256,
                "duration": video.duration, "fps": video.fps, "width": video.width, "height": video.height}


def review_state(e: dict) -> str:
    """В очередь «Требует внимания» — только сработавшее названное правило L3. Остаются на таймлайне, но вопросом не
    считаются: шаг процедуры и отклонение от шаблона без названия (out_of_template, order) — по шумному журналу
    детектора это чаще нормальное поведение; SOP-чекер («Пропущен шаг: …») — детектор плохо видит, как купюры
    ложатся в машинку, и пропуск чаще ложный."""
    named = e["status"] != "expected" and e.get("title") and e.get("event") != "sop_step"
    return "pending" if named else "none"


@activity.defn(name="publish")
def publish(result: dict) -> str:
    """result от run_cv: {job_id, verdict, detector, events_key, boxes_key}; события и боксы лежат в S3."""
    job_id = result["job_id"]
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None:
            return "failed"
        if job.status not in jobs.ACTIVE:
            return job.verdict or job.status
        if jobs.expired(job):
            jobs.mark_failed(db, job_id, jobs.TIMEOUT_ERROR)
            return "failed"
        duration = db.get(Video, job.video_id).duration
        remaining = jobs.seconds_left(job)
    try:
        with deadlines.budget(remaining):
            progress.set_stage(job_id, "publishing", 0.95)
            events = json.loads(storage.get_bytes(result["events_key"]))
            boxes = json.loads(storage.get_bytes(result["boxes_key"]))
            evidence.validate_result(result, events, duration)
            with SessionLocal() as db:
                job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
                if job.status not in jobs.ACTIVE:
                    return job.verdict or job.status
                if jobs.expired(job):
                    jobs.mark_failed(db, job_id, jobs.TIMEOUT_ERROR)
                    return "failed"
                # Claim the transition before editing events. A concurrent expiry wins safely.
                changed = db.execute(update(Job).where(Job.id == job_id, Job.status.in_(jobs.ACTIVE)).values(
                    status="done", stage="done", verdict=result["verdict"], error=None,
                    detector=result.get("detector"), model=result.get("model"), finished_at=now()))
                if not changed.rowcount:
                    db.rollback()
                    return "failed"
                db.query(Event).filter(Event.job_id == job_id).delete()
                for e in events:
                    db.add(Event(
                        job_id=job_id, video_id=job.video_id, t_start=e["t_start"], t_end=e["t_end"], event=e["event"],
                        actor=e.get("actor"), object=e.get("object"), target=e.get("target"), scenario=e.get("scenario"),
                        instance=e.get("instance"), step=e.get("step"), status=e["status"], title=e.get("title"),
                        rules=e.get("rules", []), norm=e.get("norm"), message=e.get("message"), evidence=e.get("evidence", [])[:3],
                        review=review_state(e),
                    ))
                # Replace model boxes, preserving human corrections.
                db.query(Box).filter(Box.video_id == job.video_id, Box.source != "human").delete()
                for b in boxes:
                    db.add(Box(video_id=job.video_id, job_id=job_id, t=b["t"], cls=b["cls"], x1=b["bbox"][0], y1=b["bbox"][1],
                               x2=b["bbox"][2], y2=b["bbox"][3], conf=b.get("conf"), track_id=b.get("track_id"),
                               attributes=b.get("attributes", {}),
                               source="stub" if result.get("detector") == "stub" else "model"))
                db.flush()
                deadlines.remaining()
                if jobs.expired(job):
                    raise TimeoutError(jobs.TIMEOUT_ERROR)
                job.finished_at = now()
                db.commit()
    except Exception as exc:
        fail({"job_id": job_id, "error": f"invalid or unavailable analysis result: {exc}"})
        return "failed"
    progress.set_stage(job_id, "done", 1.0)
    return result["verdict"]


@activity.defn(name="fail")
def fail(payload: dict) -> None:
    with SessionLocal() as db:
        changed = jobs.mark_failed(db, payload["job_id"], payload["error"])
    if changed:
        progress.set_stage(payload["job_id"], "failed", None)
