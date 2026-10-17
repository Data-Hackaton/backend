"""Модель данных: токены, видео, задачи, события с ревью, боксы (модель и правки человека)."""

from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from backend.config import DATABASE_URL


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(12))  # начало токена для узнавания в списке
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Video(Base):
    __tablename__ = "videos"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    source_key: Mapped[str] = mapped_column(String(255))  # оригинал в S3 — на анализ
    play_key: Mapped[str | None] = mapped_column(String(255))  # mp4 для плеера
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column(Integer)
    duration: Mapped[float | None] = mapped_column(Float)
    fps: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(16))  # upload / api / camera
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued / processing / done / failed
    stage: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    verdict: Mapped[str | None] = mapped_column(String(16))  # violation / no_violation
    detector: Mapped[str | None] = mapped_column(String(32))  # чем анализировали: stub, yolo, rfdetr
    requested_by: Mapped[str] = mapped_column(String(100))  # ui / admin / имя токена
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)
    t_start: Mapped[float] = mapped_column(Float)
    t_end: Mapped[float] = mapped_column(Float)
    event: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str | None] = mapped_column(String(32))
    object: Mapped[str | None] = mapped_column(String(32))
    target: Mapped[str | None] = mapped_column(String(32))
    scenario: Mapped[str | None] = mapped_column(String(32))
    instance: Mapped[int | None] = mapped_column(Integer)
    step: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), index=True)  # expected / warning / violation
    title: Mapped[str | None] = mapped_column(String(200))
    rules: Mapped[list] = mapped_column(JSON, default=list)
    norm: Mapped[str | None] = mapped_column(String(32))
    message: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list] = mapped_column(JSON, default=list)  # [{t, key}] — кадры в S3
    review: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending / approved / rejected / none
    review_comment: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16), default="model", server_default="model")  # model / human


class EventType(Base):
    """Собственный тип события из интерфейса: черновик, пока нет 10 подтверждённых примеров из разных записей."""

    __tablename__ = "event_types"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    observable: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(16))  # violation / warning / quality / expected
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft / published
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EventSample(Base):
    __tablename__ = "event_samples"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type_id: Mapped[str] = mapped_column(ForeignKey("event_types.id"), index=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)
    start: Mapped[float] = mapped_column(Float)
    end: Mapped[float] = mapped_column(Float)
    review: Mapped[str] = mapped_column(String(16), default="pending")  # pending / approved / rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PipelineRun(Base):
    """Запуски конвейера из интерфейса: сборка датасета и пересчёт архива."""

    __tablename__ = "pipeline_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # dataset / rerun
    status: Mapped[str] = mapped_column(String(16))  # running / ready / failed
    version: Mapped[str | None] = mapped_column(String(32))
    total: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Box(Base):
    __tablename__ = "boxes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(String(12))
    t: Mapped[float] = mapped_column(Float, index=True)
    cls: Mapped[str] = mapped_column(String(32))
    x1: Mapped[float] = mapped_column(Float)
    y1: Mapped[float] = mapped_column(Float)
    x2: Mapped[float] = mapped_column(Float)
    y2: Mapped[float] = mapped_column(Float)
    conf: Mapped[float | None] = mapped_column(Float)
    track_id: Mapped[int | None] = mapped_column(Integer)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(16))  # stub / model / human
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


engine = create_engine(DATABASE_URL, pool_pre_ping=True)
if engine.dialect.name == "sqlite":  # тесты: внешние ключи проверяются, как в Postgres
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
    # колонки, добавленные после первого запуска: create_all их в существующие таблицы не добавляет
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE events ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'model'")


def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
