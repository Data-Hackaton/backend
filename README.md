# backend

FastAPI: API для жюри (раздел 10 ТЗ) и раздача сборки `frontend`. Сейчас обработка — мок: через 3 секунды задача отдаёт одно «нарушение» с кадром из середины видео. Следующий шаг — workflow `AnalyzeVideo` в Temporal (схема в `infra/README.md`).

## Запуск

```sh
uv sync
API_KEY=$(openssl rand -hex 24) uv run backend      # http://127.0.0.1:8000, docs: /docs
uv run pytest
```

Нужны `ffmpeg` и `ffprobe` в PATH.

| Переменная | По умолчанию | Что |
|---|---|---|
| `API_KEY` | пусто → все запросы 401 | ключ для заголовка `X-API-Key` |
| `MAX_UPLOAD_MB` | 200 | лимит размера видео |
| `FRONTEND_DIST` | `../frontend/dist` | если каталог есть, отдаётся на `/` |
| `HOST`, `PORT` | 127.0.0.1, 8000 | |

## API

```sh
curl -X POST http://127.0.0.1:8000/api/v1/analyze -H "X-API-Key: $API_KEY" -F "file=@video.mov"
# {"job_id":"3f2a9c1e0b7d"}
curl http://127.0.0.1:8000/api/v1/jobs/3f2a9c1e0b7d -H "X-API-Key: $API_KEY"
```

- `status`: `processing` → `done` | `failed`. При `done` есть `verdict` (`violation` / `no_violation`) и `violations`.
- В `violations[]` поля строго по ТЗ: `type`, `timecode_sec`, `description`, `evidence_frames` (1–3 JPEG в base64 без префикса `data:`).
- Сверх ТЗ: `rule` {`document`, `clause`, `url`} для интерфейса и `error` при `failed`. В схеме ТЗ места для норматива нет, поэтому ссылка на него дублируется в конце `description`: жюри проверяет автоматически по этой схеме.
- Не `.mov` → 415, без ключа или с неверным ключом → 401, неизвестный `job_id` → 404.
- Загруженное видео удаляется после обработки: тестовые видео жюри хранить нельзя.

Модели ответа — `src/backend/schema.py`; frontend держит такие же типы.
