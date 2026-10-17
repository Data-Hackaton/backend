"""Прогресс задач по этапам в Valkey: cv-worker пишет, API читает для статуса очереди."""

import json

import redis

from backend.config import VALKEY_URL

STAGES = ["queued", "downloading", "sampling", "detecting", "actions", "events", "evidence", "publishing", "done"]

_client = None


def client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(VALKEY_URL, decode_responses=True)
    return _client


def get(job_id: str) -> dict | None:
    try:
        raw = client().get(f"job:{job_id}")
    except redis.RedisError:
        return None
    return json.loads(raw) if raw else None


def set_stage(job_id: str, stage: str, progress: float | None = None) -> None:
    try:
        client().set(f"job:{job_id}", json.dumps({"stage": stage, "progress": progress}), ex=24 * 3600)
    except redis.RedisError:
        pass
