FROM python:3.12-slim-trixie

# ffmpeg — метаданные видео, mp4 для плеера, кадр для редактора боксов
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project
COPY src ./src
COPY tests ./tests
RUN uv sync --frozen

EXPOSE 8000
CMD ["backend"]
