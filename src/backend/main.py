import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend import config, storage
from backend.api import jury_router, public_router, router
from backend.auth import AccessMiddleware
from backend.db import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not config.ADMIN_KEY.strip():
        raise RuntimeError("ADMIN_KEY is required: configure the local Compose override or environment")
    init_db()
    try:
        storage.ensure_bucket()
    except Exception as e:  # noqa: BLE001 — без S3 API поднимается, загрузка видео упадёт с понятной ошибкой
        print(f"S3 недоступен: {e}", flush=True)
    yield


DESCRIPTION = """
Локальный сервис проверки кассовой дисциплины по видео. Все модели работают внутри сервиса, без облачных API.

## API жюри — два шага

1. `POST /api/v1/analyze` — видео `.mov` в поле `file`, в ответ `job_id`.
2. `GET /api/v1/jobs/{job_id}` — `processing`, пока видео в очереди или в работе; затем `done` с `verdict` и
   `violations` или `failed` с `error`.

Для этих двух методов ключ обязателен в заголовке `X-API-Key`; Bearer и cookie его не заменяют. Без него — `401`.
В этой странице ключ задаётся один раз кнопкой **Authorize**.

```
curl -X POST http://localhost:48000/api/v1/analyze -H "X-API-Key: <ключ>" -F "file=@video_017.mov"
curl "http://localhost:48000/api/v1/jobs/<job_id>?wait=30" -H "X-API-Key: <ключ>"
```

**Опрос.** При `processing` ответ содержит `Retry-After: 2`. Параметр `wait` (до 60 с) держит запрос, пока задача
не будет готова, — один запрос вместо десятка. Где задача в очереди и на каком этапе — `/api/v1/jobs/{job_id}/progress`;
готов ли сервис принимать видео — `/api/v1/health` (без ключа).

**Лимиты.** Видео до 200 МиБ. Бюджет задачи — 270 секунд от начала приёма POST, включая подготовку и очередь;
просроченная задача отдаётся как `failed`, оставляя резерв до лимита 5 минут на получение ответа. Отсутствующие
или повреждённые кадры также означают `failed`. MOV принимается и при невозможности создать MP4 для плеера.
Сборка ответа GET из БД и S3 ограничена 20 секундами; превышение возвращает `503`.

**Коды ошибок:** `401` — нет ключа или ключ неверный, `404` — нет задачи, `413` — файл больше 200 МБ,
`415` — не `.mov` в поле `file`, `422` — файл не читается как видео, `503` — хранилище, БД или результат недоступны,
`504` — срок приёма/подготовки истёк. Тело ошибки — `{"detail": "…"}`.

Остальные методы, видео и кадры также требуют ключ или браузерную сессию. Токены ботов — Coming soon.
Описание контракта — также в `/redoc` (после входа).
"""
TAGS = [
    {"name": "jury", "description": "API жюри и ботов: загрузка видео, статус и результат, ход обработки. По ключу."},
    {"name": "queue", "description": "Очередь обработки для интерфейса: задачи, места, воркеры."},
    {"name": "videos", "description": "Архив записей: загрузка из интерфейса, плеер, кадры, повторный анализ."},
    {"name": "events", "description": "События записи, очередь «Требует внимания», решения проверяющего."},
    {"name": "boxes", "description": "Рамки объектов: вывод модели и правки человека."},
    {"name": "event-types", "description": "Каталог типов событий и собственные типы с примерами."},
    {"name": "tokens", "description": "Coming soon — токены ботов пока недоступны."},
    {"name": "access", "description": "Вход по общему токену доступа."},
    {"name": "pipeline", "description": "Сборка набора, дообучение, пересчёт архива."},
    {"name": "meta", "description": "Справочники интерфейса."},
]
app = FastAPI(title="Forte Видеоаудит — API", version="1.1", description=DESCRIPTION, openapi_tags=TAGS,
              lifespan=lifespan,
              swagger_ui_parameters={"persistAuthorization": False, "displayRequestDuration": True,
                                     "tryItOutEnabled": True, "defaultModelsExpandDepth": 0})
app.include_router(public_router)
app.include_router(jury_router)
app.include_router(router)
app.add_middleware(AccessMiddleware)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


if config.FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=config.FRONTEND_DIST, html=True), name="frontend")


def run() -> None:
    uvicorn.run("backend.main:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
