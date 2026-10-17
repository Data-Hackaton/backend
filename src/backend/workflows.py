"""Workflow обработки видео. Код workflow детерминированный: только вызовы активностей по имени.

prepare (очередь analyze) → run_cv (очередь cv-gpu, cv-worker, одна активность за раз) → publish (analyze).
Любая ошибка → fail: задача уходит в failed, а не висит. Общий таймаут задаёт API при старте (JOB_TIMEOUT_SEC).
RerunArchive — пересчёт архива по одной записи: держит один cv-worker, остальные свободны для жюри.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

ANALYZE_QUEUE = "analyze"
CV_QUEUE = "cv-gpu"


@workflow.defn(name="AnalyzeVideo")
class AnalyzeVideo:
    @workflow.run
    async def run(self, job_id: str) -> str:
        task = await workflow.execute_activity(
            "prepare", job_id, task_queue=ANALYZE_QUEUE,
            start_to_close_timeout=timedelta(seconds=30), retry_policy=RetryPolicy(maximum_attempts=3),
        )
        try:
            result = await workflow.execute_activity(
                "run_cv", task, task_queue=CV_QUEUE,
                schedule_to_close_timeout=timedelta(seconds=250), retry_policy=RetryPolicy(maximum_attempts=1),
            )
            return await workflow.execute_activity(
                "publish", result, task_queue=ANALYZE_QUEUE,
                start_to_close_timeout=timedelta(seconds=60), retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except Exception as e:
            await workflow.execute_activity(
                "fail", {"job_id": job_id, "error": str(e)}, task_queue=ANALYZE_QUEUE,
                start_to_close_timeout=timedelta(seconds=30),
            )
            raise


@workflow.defn(name="RerunArchive")
class RerunArchive:
    @workflow.run
    async def run(self, batch: dict) -> int:
        """batch: {job_ids, timeout_sec}; дочерние AnalyzeVideo по одной, сбой одной не останавливает остальные."""
        done = 0
        for job_id in batch["job_ids"]:
            try:
                await workflow.execute_child_workflow("AnalyzeVideo", job_id, id=f"job-{job_id}",
                                                      task_queue=ANALYZE_QUEUE,
                                                      execution_timeout=timedelta(seconds=batch["timeout_sec"]))
                done += 1
            except Exception:  # noqa: BLE001 — задача уже записана как failed активностью fail
                continue
        return done
