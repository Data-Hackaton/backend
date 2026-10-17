"""Мок-обработка до подключения Temporal: через несколько секунд отдаёт одно «нарушение» с кадром из середины видео."""

import base64
import subprocess
import threading
import time
from pathlib import Path

from backend.schema import EvidenceFrame, JobResponse, Rule, Violation

MOCK_DELAY_SEC = 3.0

_jobs: dict[str, JobResponse] = {}
_lock = threading.Lock()


def get(job_id: str) -> JobResponse | None:
    with _lock:
        return _jobs.get(job_id)


def _set(job: JobResponse) -> None:
    with _lock:
        _jobs[job.job_id] = job


def submit(job_id: str, video: Path) -> None:
    _set(JobResponse(job_id=job_id, status="processing"))
    threading.Thread(target=_run_mock, args=(job_id, video), daemon=True).start()


def _probe_duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _frame_jpeg(video: Path, t: float) -> bytes:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video),
         "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "3", "-"],
        capture_output=True, check=True,
    )
    return out.stdout


def _run_mock(job_id: str, video: Path) -> None:
    try:
        time.sleep(MOCK_DELAY_SEC)
        t = round(_probe_duration(video) / 2, 1)
        frame = base64.b64encode(_frame_jpeg(video, t)).decode()
        violation = Violation(
            type="Мок: тип нарушения",
            timecode_sec=t,
            description="Мок-результат для проверки интерфейса и схемы ответа. Норматив: —",
            evidence_frames=[EvidenceFrame(timestamp_sec=t, image_base64=frame)],
            rule=Rule(document="—"),
        )
        _set(JobResponse(job_id=job_id, status="done", verdict="violation", violations=[violation]))
    except Exception as e:  # noqa: BLE001 — любая ошибка обработки = failed, а не зависшая задача
        _set(JobResponse(job_id=job_id, status="failed", error=str(e)))
    finally:
        video.unlink(missing_ok=True)  # тестовые видео жюри не храним
