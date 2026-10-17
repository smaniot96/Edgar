"""Liveness (`/health`) and readiness (`/ready`) probes.

`/ready` checks every backing service the API needs to serve a turn — Postgres, Redis and
Qdrant — each with a short timeout, and returns 503 with a per-component breakdown if any
of them is unreachable. It also reports `schema` as unavailable while the database revision is
not this code's Alembic head, so a replica started against an older schema stays out of rotation.
"""

import asyncio
import time
from functools import lru_cache
from pathlib import Path

import httpx
import structlog
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import db as db_package
from edgar_core.config import VECTOR_DB_API_KEY, VECTOR_DB_URL

from ..dependencies import get_redis, get_session
from ..services.operational_metrics import observe_readiness, observe_readiness_check

log = structlog.get_logger()
router = APIRouter(tags=["health"])

READY_TIMEOUT_SEC = 2.0
ALEMBIC_DIR = Path(db_package.__file__).resolve().parent / "alembic"


@router.get("/health")
def health():
    return {"status": "ok"}


async def _check_postgres(db: AsyncSession) -> None:
    await db.execute(text("SELECT 1"))


@lru_cache(maxsize=1)
def _code_head() -> str:
    return ScriptDirectory(str(ALEMBIC_DIR)).get_current_head()


async def _check_schema(db: AsyncSession) -> None:
    revision = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    if revision != _code_head():
        raise RuntimeError(f"database is at {revision}, this code expects {_code_head()}")


async def _check_redis(redis: Redis) -> None:
    await redis.ping()


async def _check_qdrant() -> None:
    headers = {"api-key": VECTOR_DB_API_KEY} if VECTOR_DB_API_KEY else {}
    async with httpx.AsyncClient(timeout=READY_TIMEOUT_SEC) as client:
        resp = await client.get(f"{VECTOR_DB_URL}/readyz", headers=headers)
        resp.raise_for_status()


async def _probe(name: str, coro) -> tuple[str, str]:
    started = time.monotonic()
    try:
        await asyncio.wait_for(coro, timeout=READY_TIMEOUT_SEC)
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        observe_readiness_check(name, ok=False, seconds=time.monotonic() - started)
        log.warning("readiness_check_failed", component=name, error=repr(exc))
        return name, "unavailable"
    observe_readiness_check(name, ok=True, seconds=time.monotonic() - started)
    return name, "ok"


@router.get("/ready")
async def ready(
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    # Postgres and schema share the AsyncSession, so they run one after the other.
    results = dict(
        [
            await _probe("postgres", _check_postgres(db)),
            await _probe("schema", _check_schema(db)),
        ]
    )
    # The session's first statement checked out a pooled connection and holds it until the
    # transaction ends. Release it now, so a slow Redis or Qdrant cannot pin it for the probe.
    await db.close()
    results.update(
        await asyncio.gather(
            _probe("redis", _check_redis(redis)),
            _probe("qdrant", _check_qdrant()),
        )
    )
    if all(v == "ok" for v in results.values()):
        observe_readiness(ready=True)
        return {"status": "ready", "checks": results}
    observe_readiness(ready=False)
    return JSONResponse(status_code=503, content={"status": "unavailable", "checks": results})
