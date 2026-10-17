# API Forte Video Audit — контракт для интерфейса и ботов

Базовый путь — `/api/v1`. Полная схема — [`openapi.json`](openapi.json), живая документация — `/docs` на запущенном бэкенде. Время — секунды от начала видео; даты — ISO 8601 в UTC; боксы — `[x1, y1, x2, y2]` в пикселях исходного кадра.

## Доступ

- Рабочие методы, поток видео, кадры и документация требуют общий `ADMIN_KEY` либо браузерную сессию. Без доступа — `401`; неверный заголовок не заменяется существующей cookie.
- API принимает `X-API-Key: <ключ>` или `Authorization: Bearer <ключ>`. Ключ задан в локальном `infra/docker-compose.override.yml`; токены ботов больше не принимаются.
- `POST /auth/session` с JSON `{"token":"…"}` устанавливает подписанную HttpOnly cookie на 12 часов. Требуется `Origin` текущего сайта. Токен не сохраняется в браузерном хранилище.
- `GET /auth/session` → `{"authenticated":true,"expires_at":<Unix seconds>}`; без сессии — `false`, `null`. Для доступа по API-заголовку срок отсутствует (`null`).
- `DELETE /auth/session` удаляет cookie (`204`). Изменяющие запросы с cookie требуют совпадающий `Origin`. Смена `ADMIN_KEY` аннулирует все сессии.
- `SESSION_COOKIE_SECURE=true` обязателен на HTTPS-развёртывании; `false` предусмотрен для локального HTTP. Ответы API и медиа — `Cache-Control: no-store`.
- Без ключа доступны `/healthz`, `/api/v1/health`, методы сессии и статическая оболочка входа. Презентация и её подготовленные материалы публичны на gateway.

## Экраны → методы

### Загрузка и записи по календарю (режим «просмотр»)

| Метод | Что |
|---|---|
| `POST /videos` | multipart: `file` (`.mov` или `.mp4`), необязательно `recorded_at` (дата записи для календаря). Сохраняет в S3, ставит анализ в очередь. Ответ `{video, job}` |
| `GET /videos/calendar?month=2026-10` | `[{date, videos, violations}]` — дни месяца, где есть записи; `violations` — записи с вердиктом «нарушение» |
| `GET /videos?date_from=2026-10-15&date_to=2026-10-15&limit=100` | записи за период, новые сверху; у каждой `last_job` — статус последнего анализа |
| `GET /videos/{id}` | запись: длительность, fps, размер кадра, `stream_url`, `last_job` |
| `GET /videos/{id}/stream` | видео для `<video src>`: mp4, поддерживает `Range` (перемотка) |
| `GET /videos/{id}/frame?t=12.5` | JPEG кадра на момент `t` — подложка редактора боксов |
| `POST /videos/{id}/analyze` | перезапустить анализ (например, после дообучения) → `job` |

### Очередь и статус анализа

| Метод | Что |
|---|---|
| `GET /queue` | сводка: `queued`, `processing`, `done_24h`, `failed_24h`, `avg_duration_sec`, `workers` (кто слушает очереди `analyze` и `cv-gpu`), `jobs` — активные задачи по порядку: `stage`, `progress` 0–1, `position` и `eta_sec` для ждущих |
| `GET /queue/jobs?status=failed&limit=50` | история задач |
| `GET /queue/jobs/{job_id}` | одна задача — для опроса после загрузки (раз в 1–2 с) |

Статусы задачи: `queued` → `processing` → `done` | `failed` (`error` — текст). Этапы `stage`: `queued`, `downloading`, `detecting`, `events`, `evidence`, `publishing`, `done` (или `failed`); `sampling` и `actions` добавятся с обученным детектором, полный список — `GET /meta` → `stages`. `detector` — чем анализировали (`stub` — заглушка до обученного детектора).

### События и нарушения (режим «аудит нарушений»)

| Метод | Что |
|---|---|
| `GET /videos/{id}/events?status=violation` | события последнего готового анализа (или `job_id=`), по времени |
| `GET /review?state=pending&status=violation` | очередь аудита: предупреждения и нарушения без решения, со всех записей |
| `POST /events/{event_id}/review` | `{"decision": "approved" \| "rejected", "comment": "..."}` — подтвердить или отклонить замечание; отклонённые идут в дообучение |
| `GET /events/{event_id}/evidence/{n}` | JPEG кадра-доказательства с боксами |

Событие:

```json
{
  "id": 17, "job_id": "3f2a9c1e0b7d", "video_id": "a1b2c3d4e5f6",
  "t_start": 56.5, "t_end": 56.5,
  "event": "person_enter", "actor": "client", "object": null, "target": null,
  "scenario": "client_service", "instance": 2, "step": 1,
  "status": "violation",
  "title": "Деньги предыдущего клиента на кассовом столе",
  "rules": ["desk_carryover:banknotes"], "norm": "n231_p20",
  "message": "56.5 с: Клиент входит в помещение. На рабочем месте кассира только наличные текущего клиента, ранее принятые хранятся в сейфе.",
  "evidence": [{"t": 56.5, "url": "/api/v1/events/17/evidence/0"}],
  "review": "pending", "review_comment": null, "reviewed_at": null
}
```

- `status`: `expected` — шаг процедуры на своём месте; `warning` — отклонение без подтверждённого норматива; `violation` — нарушение.
- `title` нарушений (тип для жюри): «Деньги предыдущего клиента на кассовом столе», «Ценности на столе при уходе кассира», «Пересчёт наличных после ухода клиента», «Личные вещи на кассовом столе», «Присвоение части наличных», «Компьютер с активной сессией без присмотра», «Сейф оставлен открытым», «Постороннее лицо у кассы», «Кассир ушёл до окончания обслуживания». Полный список — `data_prepare/reference/scenarios.yaml`.
- `event: "sop_step"` — предупреждение SOP-чекера (по схеме NVIDIA): «Пропущен шаг процедуры» или «Шаг процедуры не по порядку» в цикле обслуживания; `actor`/`object`/`target` — какой шаг.
- `source` у боксов: `model` — обученный детектор (какой — `detector` задачи: `yolo` или `rfdetr`), `stub`, `human`. У людей `track_id` и `attributes.role` (`cashier` / `client`).
- `review`: `pending` (ждёт решения), `approved`, `rejected`; у ожидаемых событий — `none`.
- `norm` — id из `GET /meta` → `norms` (документ, пункт, URL, цитата).
- Таймлайн видео = события; маркеры на шкале плеера — `t_start`, цвет по `status`.

### Bounding boxes и дообучение (режим «дообучение»)

| Метод | Что |
|---|---|
| `GET /videos/{id}/boxes?t_from=50&t_to=60` | боксы на отрезке: `{id, t, cls, bbox, attributes, track_id, conf, source}`; `source` — `stub` / `model` / `human`. Если на кадре есть правка человека, боксы модели этого кадра не отдаются |
| `PUT /videos/{id}/boxes?t=56.5` | правка человека: тело — **полный** список боксов кадра `[{cls, bbox, attributes, track_id?}]`, заменяет прошлую правку этого кадра. Добавить объект = добавить бокс в список, удалить = не передать. Неизвестный класс → `422` |
| `GET /training/export` | COCO по всем кадрам с правками человека; картинка кадра — `frame_url` у каждого `image` |

Боксы модели приходят на кадрах с шагом ~0,5 с; для плавного показа поверх видео брать ближайший кадр по времени. Классы и атрибуты (цвета, `state`, `kind`, `screen`) — `GET /meta` → `classes`. Служебные классы: `ignore_zone` — стекло, всё внутри игнорируется; `reflection` — человек в отражении.

### Токены ботов — Coming soon

| Метод | Ответ после авторизации |
|---|---|
| `GET /tokens` | `{"items":[],"coming_soon":true}` |
| `POST /tokens`, `DELETE /tokens/{id}` | `501`, сообщение Coming soon; без изменений БД |

Ранее созданные токены остаются в БД, но не дают доступа.

### Камеры и справочники

| Метод | Что |
|---|---|
| `GET /cameras` | `{"items": [], "coming_soon": true}` — заглушка экрана камер |
| `GET /meta` | `classes`, `event_statuses`, `review_states`, `job_statuses`, `stages`, `norms`, `modes` (`view`, `audit`, `training`) |
| `GET /healthz` | `{"status": "ok"}` |

## API для жюри и ботов (раздел 10 ТЗ)

После входа на `/` доступны Swagger с примерами и кнопкой **Authorize** (ключ один раз на всю страницу) — http://localhost:48000/docs, то же в ReDoc — `/redoc`.

```sh
curl -X POST http://localhost:48000/api/v1/analyze -H "X-API-Key: $ADMIN_KEY" -F "file=@X-1.mov"
# {"job_id":"3f2a9c1e0b7d"}
curl "http://localhost:48000/api/v1/jobs/3f2a9c1e0b7d?wait=30" -H "X-API-Key: $ADMIN_KEY"
```

| Метод | Ключ | Что |
|---|---|---|
| `POST /api/v1/analyze` | да | видео `.mov` в поле `file` (до 200 МБ) → `{"job_id"}` |
| `GET /api/v1/jobs/{job_id}` | да | статус и результат по схеме ТЗ; `?wait=0..60` — длинный опрос: ответ сразу по готовности; при `processing` — заголовок `Retry-After: 2` |
| `GET /api/v1/jobs/{job_id}/progress` | да | `status` (`queued` / `processing` / `done` / `failed`), `stage`, `progress` 0–1, `queue_position`, `eta_sec`, время |
| `GET /api/v1/jobs` | да | задачи этого ключа, новые сверху: `status`, `verdict`, число нарушений |
| `GET /api/v1/health` | нет | `ok` / `degraded` (нет обработчика видео), воркеры по очередям, `queued`, `processing`, средняя длительность |

Ошибки — `{"detail": "…"}`: `401` нет или неверный ключ, `404` нет задачи, `413` файл больше 200 МБ, `415` не `.mov` в поле `file`, `422` файл не читается как видео (и неверные параметры).

Видео жюри и ботов хранятся, как и загрузки из интерфейса, и видны в `GET /videos` с `source: "api"` (из интерфейса — `upload`).

`status`: `processing` (в очереди или в работе) → `done` | `failed`. При `done`: `verdict` (`violation` / `no_violation`) и `violations[]` — `type`, `timecode_sec`, `description` (что видно, почему это нарушение, косвенные и условные основания — «основание (косвенно): Правила №231, п.80», прямой норматив — «Норматив: … (url)»), `evidence_frames` (1–3 JPEG base64: первый — на таймкоде, затем за 1,5 с до и через 1,5 с после), сверх ТЗ — `rule` {`document`, `clause`, `url`} — только прямой норматив. Нормы — `data_prepare/reference/norms.yaml` поверх `norms_review.yaml` (аудит «Полной проверки ForteBank»). События одного типа ближе 5 с объединяются в одно нарушение. Задача, не уложившаяся в 4,5 мин, отдаётся как `failed`. Пересчёт архива из интерфейса идёт по одной записи (workflow `RerunArchive`) и держит не больше одного обработчика — видео жюри не ждут за ним в очереди.

## Запуск для разработки интерфейса

```sh
cd infra
./init-access.py                                  # один раз; --https для публичного HTTPS
docker compose --profile tools build cv               # образ cv-worker (torch, ~10 ГБ)
docker compose --profile app up -d                    # postgres, backend, worker, cv-worker + Temporal, S3, Valkey
```

API — http://localhost:48000/api/v1, документация — http://localhost:48000/docs. Сборка фронта (`frontend/dist`) отдаётся на `/`; при разработке Vite проксирует `/api` на бэкенд.

Детектор задаётся `DETECTOR` в `infra/.env`: `yolo` или `rfdetr` — обученные модели, любое видео; `stub` — заглушка: видео из датасета дают события из эталона, другие — пусто.
