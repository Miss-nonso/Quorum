# Backend image: every process type, selected by the command.

# ---- build: resolve and install locked dependencies into /app/.venv -------
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
# Dependencies first, so source edits don't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---- runtime: no build tooling, non-root ----------------------------------
FROM python:3.12-slim-bookworm AS runtime

RUN groupadd --system conductor && useradd --system --gid conductor --no-create-home conductor

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini ./
COPY alembic ./alembic

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    CONDUCTOR_API_HOST=0.0.0.0

USER conductor
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, sys, urllib.request; port = os.environ.get('PORT') or os.environ.get('CONDUCTOR_API_PORT', '8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/api/v1/health', timeout=2).status == 200 else 1)"]

# One image, many roles: `conductor api | worker | scheduler | migrate | seed | all`.
# (`all` runs everything in one container, for free hosting tiers.)
CMD ["conductor", "api"]
