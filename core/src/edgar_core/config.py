"""Single source of configuration for every Edgar package.

Reads component env vars (host, port, user, password, db) and builds the URLs the rest of
the code consumes. The same `.env` works on the host (DB_HOST=localhost) and inside Docker
Compose (DB_HOST=db, etc., overridden in `docker-compose.yml`); nothing duplicates URLs.

Importing this module loads `.env` from the Edgar repo root once, regardless of cwd.
"""

import os
from pathlib import Path
from urllib.parse import quote

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
# Encode credentials as URL components so delimiters and spaces reach asyncpg unchanged.
DATABASE_URL = (
    f"postgresql+asyncpg://{quote(POSTGRES_USER, safe='')}:{quote(POSTGRES_PASSWORD, safe='')}"
    f"@{DB_HOST}:{DB_PORT}/{POSTGRES_DB}"
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

# Request, upload, concurrency and spend limits (EDG-066). Non-secret integers only; defaults are
# documented in docs/request-limits.md. A malformed value fails at import, naming the setting but
# never echoing its value.


def _int_setting(name: str, default: int, *, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


# Per client (IP until EDG-065 supplies a user id): play turns, intro and sync/stream turns.
LIMIT_TURNS_PER_MINUTE = _int_setting("LIMIT_TURNS_PER_MINUTE", 30, minimum=1)
# Per client: starts of paid or heavy background work (upload, recovery, AI generation, preparation).
LIMIT_JOB_STARTS_PER_HOUR = _int_setting("LIMIT_JOB_STARTS_PER_HOUR", 20, minimum=1)
# Concurrent turn leases across all replicas, and per client.
LIMIT_MAX_CONCURRENT_TURNS = _int_setting("LIMIT_MAX_CONCURRENT_TURNS", 8, minimum=1)
LIMIT_MAX_CONCURRENT_TURNS_PER_CLIENT = _int_setting(
    "LIMIT_MAX_CONCURRENT_TURNS_PER_CLIENT", 4, minimum=1
)
# Concurrent background jobs (ingestion, generation, preparation) across all replicas.
LIMIT_MAX_CONCURRENT_JOBS = _int_setting("LIMIT_MAX_CONCURRENT_JOBS", 4, minimum=1)
# Largest accepted PDF upload, in bytes (64 MiB).
LIMIT_MAX_UPLOAD_BYTES = _int_setting("LIMIT_MAX_UPLOAD_BYTES", 64 * 1024 * 1024, minimum=1024 * 1024)
# UTC-day ceiling for provider spend, in USD micro-units (50 USD).
LIMIT_DAILY_SPEND_MICROUSD = _int_setting("LIMIT_DAILY_SPEND_MICROUSD", 50_000_000, minimum=1)
# Charged for a provider call whose cost is unknown, and for an intro generation.
LIMIT_ASSUMED_CALL_COST_MICROUSD = _int_setting(
    "LIMIT_ASSUMED_CALL_COST_MICROUSD", 100_000, minimum=1
)
# Reserved against the daily ceiling for a job that declares no preparation budget.
LIMIT_UNBUDGETED_JOB_COST_MICROUSD = _int_setting(
    "LIMIT_UNBUDGETED_JOB_COST_MICROUSD", 500_000, minimum=1
)
# Lease lifetimes. A lease whose holder dies is freed after its lifetime; a live holder is never
# cut short by it, but a slot can be counted free for a moment after a very long job.
LIMIT_TURN_LEASE_SECONDS = _int_setting("LIMIT_TURN_LEASE_SECONDS", 300, minimum=30)
LIMIT_JOB_LEASE_SECONDS = _int_setting("LIMIT_JOB_LEASE_SECONDS", 7200, minimum=60)
