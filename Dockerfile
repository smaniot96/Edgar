# Build from Edgar/: docker build -f Dockerfile --target api .
FROM node:20-alpine AS frontend-build
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

FROM python:3.12-slim-bookworm AS base
COPY --from=ghcr.io/astral-sh/uv:0.6.6 /uv /uvx /bin/

WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev

FROM base AS migrate
CMD ["uv", "run", "alembic", "-c", "db/alembic.ini", "upgrade", "head"]

FROM base AS api
COPY --from=frontend-build /frontend/dist /app/frontend/dist
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS ingestion
ENTRYPOINT ["uv", "run", "python", "-m", "ingestion.main"]
CMD ["--help"]

FROM base AS rules_engine
CMD ["uv", "run", "python", "-m", "rules_engine.main"]
