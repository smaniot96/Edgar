"""
Single source of config: reads component env vars and builds URLs.
No duplication: .env has hosts/ports/credentials; Docker only overrides host names.
Works for ENV=local (localhost) and ENV=cloud (set hosts to your cloud endpoints).
"""
from pathlib import Path
import os

from dotenv import load_dotenv

# Edgar repo root (directory containing core/, db/, apps/, .env)
EDGAR_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(EDGAR_ROOT / ".env")

# -----------------------------------------------------------------------------
# Runtime: local (default) | cloud
# For cloud, set DB_HOST / REDIS_HOST / QDRANT_HOST to your cloud service URLs.
# -----------------------------------------------------------------------------
ENV = os.getenv("ENV", "local")

# -----------------------------------------------------------------------------
# PostgreSQL – components only; URL is built below
# -----------------------------------------------------------------------------
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
POSTGRES_USER = os.getenv("POSTGRES_USER", "user")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "password")
POSTGRES_DB = os.getenv("POSTGRES_DB", "dnd")

# -----------------------------------------------------------------------------
# Redis
# -----------------------------------------------------------------------------
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")

# -----------------------------------------------------------------------------
# Qdrant
# -----------------------------------------------------------------------------
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = os.getenv("QDRANT_PORT", "6333")

# -----------------------------------------------------------------------------
# Built URLs (single place – no duplication in docker-compose)
# SQLAlchemy async: postgresql+asyncpg://
# -----------------------------------------------------------------------------
DATABASE_URL = (
    f"postgresql+asyncpg://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{DB_HOST}:{DB_PORT}/{POSTGRES_DB}"
)
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}"
VECTOR_DB_URL = f"http://{QDRANT_HOST}:{QDRANT_PORT}"

# -----------------------------------------------------------------------------
# Secrets (only in .env, never in compose)
# -----------------------------------------------------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
VECTOR_DB_API_KEY = os.getenv("VECTOR_DB_API_KEY")

# -----------------------------------------------------------------------------
# API metadata
# -----------------------------------------------------------------------------
API_TITLE = "Edgar API"
API_VERSION = "0.1.0"

# -----------------------------------------------------------------------------
# Models (override via env)
# -----------------------------------------------------------------------------
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
