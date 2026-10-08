# Build from Edgar/: docker build --target api .   (targets: migrate, api, ingestion, rules_engine)
#
# Layering: workspace manifests + uv.lock are copied first and third-party deps are installed
# with --no-install-workspace, so editing source only invalidates the final, cheap layers.
# Each target installs only its own workspace package (`uv sync --package ...`): `migrate` and
# `rules_engine` stay small; `api` includes `ingestion` (the upload/authoring endpoints embed
# PDFs in-process, which pulls pymupdf/onnxruntime). Containers run as the non-root `edgar`
# user straight from the venv (no `uv run` at runtime).

FROM node:24-alpine AS frontend-build
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

FROM python:3.12-slim-bookworm AS python-base
COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin edgar
WORKDIR /app
# Workspace manifests only (uv needs every member's pyproject to resolve the workspace).
COPY pyproject.toml uv.lock ./
COPY core/pyproject.toml core/
COPY db/pyproject.toml db/
COPY tools/pyproject.toml tools/
COPY apps/api/pyproject.toml apps/api/
COPY apps/agent/pyproject.toml apps/agent/
COPY apps/ingestion/pyproject.toml apps/ingestion/
COPY apps/rules_engine/pyproject.toml apps/rules_engine/

FROM python-base AS migrate
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package db
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package db
USER edgar
CMD ["alembic", "-c", "db/alembic.ini", "upgrade", "head"]

FROM python-base AS api
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package api
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package api
COPY --from=frontend-build /frontend/dist /app/frontend/dist
# Uploaded PDFs / adventure metadata live under /data (bind-mounted in compose).
RUN mkdir -p /data && chown edgar:edgar /data
USER edgar
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python-base AS ingestion
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package ingestion
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package ingestion
USER edgar
ENTRYPOINT ["python", "-m", "ingestion.main"]
CMD ["--help"]

FROM python-base AS rules_engine
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package rules-engine
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package rules-engine
USER edgar
CMD ["python", "-m", "rules_engine.main"]
