"""Temporal-воркер бэкенда: workflow AnalyzeVideo и активности prepare / publish / fail (очередь analyze)."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from backend import config, storage
from backend.activities import fail, prepare, publish
from backend.db import init_db
from backend.workflows import AnalyzeVideo


async def main() -> None:
    init_db()
    storage.ensure_bucket()
    client = await Client.connect(config.TEMPORAL_ADDRESS, namespace=config.TEMPORAL_NAMESPACE)
    worker = Worker(client, task_queue=config.ANALYZE_QUEUE, workflows=[AnalyzeVideo],
                    activities=[prepare, publish, fail], activity_executor=ThreadPoolExecutor(8))
    print(f"worker: очередь {config.ANALYZE_QUEUE}, Temporal {config.TEMPORAL_ADDRESS}", flush=True)
    await worker.run()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
