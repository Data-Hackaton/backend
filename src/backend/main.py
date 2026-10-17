import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend import config, storage
from backend.api import router
from backend.db import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    try:
        storage.ensure_bucket()
    except Exception as e:  # noqa: BLE001 — без S3 API поднимается, загрузка видео упадёт с понятной ошибкой
        print(f"S3 недоступен: {e}", flush=True)
    yield


app = FastAPI(title="Forte Video Audit — Datathon 26", version="1.0", lifespan=lifespan)
app.include_router(router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


if config.FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=config.FRONTEND_DIST, html=True), name="frontend")


def run() -> None:
    uvicorn.run("backend.main:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
