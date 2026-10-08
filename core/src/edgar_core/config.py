"""Single source of configuration for every Edgar package.

Reads component env vars (host, port, user, password, db) and builds the URLs the rest of
the code consumes. The same `.env` works on the host (DB_HOST=localhost) and inside Docker
Compose (DB_HOST=db, etc., overridden in `docker-compose.yml`); nothing duplicates URLs.

Importing this module loads `.env` from the Edgar repo root once, regardless of cwd.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# core/src/edgar_core/config.py -> parents[3] is the Edgar repo root.
EDGAR_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(EDGAR_ROOT / ".env")

# `local` for laptop dev, `cloud` when DB_HOST/REDIS_HOST/QDRANT_HOST point at managed services.
ENV = os.getenv("ENV", "local")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
POSTGRES_USER = os.getenv("POSTGRES_USER", "user")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "password")
POSTGRES_DB = os.getenv("POSTGRES_DB", "dnd")

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")

QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = os.getenv("QDRANT_PORT", "6333")

# `postgresql+asyncpg://` is required so SQLAlchemy uses the async driver.
DATABASE_URL = (
    f"postgresql+asyncpg://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{DB_HOST}:{DB_PORT}/{POSTGRES_DB}"
)
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}"
VECTOR_DB_URL = f"http://{QDRANT_HOST}:{QDRANT_PORT}"

# Secrets live only in .env; never duplicate them into docker-compose or source.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
VECTOR_DB_API_KEY = os.getenv("VECTOR_DB_API_KEY")

API_TITLE = "Edgar API"
API_VERSION = "0.1.0"

# Override via env. gpt-6-luna is a reasoning model: it only accepts the default temperature,
# so agent.llm omits `temperature` for reasoning families, sends `reasoning_effort` instead and
# uses the Responses API (Chat Completions rejects function tools + reasoning_effort on it).
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-6-luna")
# none | low | medium | high | xhigh. "low" keeps turn latency close to a non-reasoning model
# (narration measured ~2s with zero reasoning tokens); ignored for non-reasoning models.
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "low")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
