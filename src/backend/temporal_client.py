"""Клиент Temporal для API: запуск обработки и список воркеров очередей."""

from datetime import timedelta

from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client

from backend import config

_client: Client | None = None


async def client() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(config.TEMPORAL_ADDRESS, namespace=config.TEMPORAL_NAMESPACE)
    return _client


async def start_analysis(job_id: str) -> None:
    c = await client()
    await c.start_workflow("AnalyzeVideo", job_id, id=f"job-{job_id}", task_queue=config.ANALYZE_QUEUE,
                           execution_timeout=timedelta(seconds=config.JOB_TIMEOUT_SEC))


async def start_rerun(run_id: int, job_ids: list[str]) -> None:
    """Пересчёт архива одним workflow: записи по одной, а не все разом в очередь cv-gpu."""
    c = await client()
    await c.start_workflow("RerunArchive", {"job_ids": job_ids, "timeout_sec": config.JOB_TIMEOUT_SEC},
                           id=f"rerun-{run_id}", task_queue=config.ANALYZE_QUEUE)


async def workers() -> list[dict]:
    """Кто сейчас слушает очереди: analyze (бэкенд) и cv-gpu (cv-worker)."""
    try:
        c = await client()
    except Exception:  # noqa: BLE001 — Temporal недоступен: воркеров просто нет
        return []
    found = []
    for queue in (config.ANALYZE_QUEUE, config.CV_QUEUE):
        try:
            resp = await c.workflow_service.describe_task_queue(DescribeTaskQueueRequest(
                namespace=config.TEMPORAL_NAMESPACE, task_queue=TaskQueue(name=queue),
                task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY))
        except Exception:  # noqa: BLE001
            continue
        for p in resp.pollers:
            found.append({"queue": queue, "identity": p.identity,
                          "last_seen": p.last_access_time.ToDatetime() if p.HasField("last_access_time") else None})
    return found
