# syntax=docker/dockerfile:1.7
# Ein Image für API, Worker und Migrationen – der Befehl entscheidet die Rolle.

ARG PYTHON_VERSION=3.13

# --- Abhängigkeiten und Projekt bauen ------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app

# Erst nur Abhängigkeiten (gut cachebar), dann der Code
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev
COPY README.md alembic.ini ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --- Entwicklung: inkl. Dev-Tools, Quellcode wird per Volume eingebunden ---------------
FROM builder AS dev
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1
CMD ["uvicorn", "musichub.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app/src"]

# --- Laufzeit ------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime
ARG BUILD_SHA=dev
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MUSICHUB_BUILD_SHA=${BUILD_SHA} \
    MUSICHUB_APP_ENV=production \
    MUSICHUB_LOG_FORMAT=json

RUN groupadd --system --gid 10001 musichub \
 && useradd --system --uid 10001 --gid musichub --no-create-home --shell /usr/sbin/nologin musichub
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/alembic.ini /app/alembic.ini
USER musichub

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=4).status == 200 else 1)"]

# Standardrolle: API (migriert vor dem Start, siehe musichub/serve.py).
# Worker: command ["musichub-worker"]
CMD ["musichub-serve"]

LABEL org.opencontainers.image.source="https://github.com/ex0th/MusicHub" \
      org.opencontainers.image.title="MusicHub Backend" \
      org.opencontainers.image.revision="${BUILD_SHA}"
