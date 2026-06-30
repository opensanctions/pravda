# syntax=docker/dockerfile:1.9
# Pravda service image — the FastAPI/uvicorn app only.
# It talks to Chrome over WebSocket (BROWSER_WS_URL) and Postgres over the
# network (DATABASE_URL). No browsers or databases live in this image.
FROM ghcr.io/astral-sh/uv:0.7-python3.13-bookworm-slim

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first for layer caching.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY pravda ./pravda
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Run as a non-root user.
RUN useradd --create-home appuser
USER appuser

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
CMD ["uvicorn", "pravda.api:app", "--host", "0.0.0.0", "--port", "8000"]
