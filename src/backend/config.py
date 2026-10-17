"""Настройки из окружения (compose: infra/docker-compose.yml, профиль app)."""

import os
from pathlib import Path

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")  # общий ключ: локальный Compose override или окружение
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./dev.db")

S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://localhost:48333")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "datathon")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "change-me-please")
S3_BUCKET = os.environ.get("S3_BUCKET", "datathon")

TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:47233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
ANALYZE_QUEUE = "analyze"  # workflow и активности бэкенда
CV_QUEUE = "cv-gpu"  # активность run_cv в cv-worker, одна за раз
JOB_TIMEOUT_SEC = 270  # лимит жюри — 5 минут на видео

VALKEY_URL = os.environ.get("VALKEY_URL", "redis://localhost:46379/0")

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))
FRONTEND_DIST = Path(os.environ.get("FRONTEND_DIST", "../frontend/dist"))
META_DIR = Path(os.environ.get("META_DIR", "../data_prepare"))  # cvat/labels.json, reference/norms.yaml
