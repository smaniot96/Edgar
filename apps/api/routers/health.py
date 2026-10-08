"""Liveness (`/health`) and readiness (`/ready`) probes.

`/ready` checks every backing service the API needs to serve a turn — Postgres, Redis and
Qdrant — each with a short timeout, and returns 503 with a per-component breakdown if any
of them is unreachable.
"""

import asyncio

import httpx
import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from edgar_core.config import VECTOR_DB_API_KEY, VECTOR_DB_URL

from ..dependencies import get_redis, get_session

log = structlog.get_logger()
router = APIRouter(tags=["health"])

READY_TIMEOUT_SEC = 2.0


@router.get("/health")
def health():
    return {"status": "ok"}


async def _check_postgres(db: AsyncSession) -> None:
    await db.execute(text("SELECT 1"))


async def _check_redis(redis: Redis) -> None:
    await redis.ping()


async def _check_qdrant() -> None:
    headers = {"api-key": VECTOR_DB_API_KEY} if VECTOR_DB_API_KEY else {}
    async with httpx.AsyncClient(timeout=READY_TIMEOUT_SEC) as client:
        resp = await client.get(f"{VECTOR_DB_URL}/readyz", headers=headers)
        resp.raise_for_status()


async def _probe(name: str, coro) -> tuple[str, str]:
    try:
        await asyncio.wait_for(coro, timeout=READY_TIMEOUT_SEC)
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        log.warning("readiness_check_failed", component=name, error=repr(exc))
        return name, "unavailable"
    return name, "ok"


@router.get("/ready")
async def ready(
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    # Postgres runs on its own: the AsyncSession must not be used concurrently.
    results = dict([await _probe("postgres", _check_postgres(db))])
    results.update(
        await asyncio.gather(
            _probe("redis", _check_redis(redis)),
            _probe("qdrant", _check_qdrant()),
        )
    )
    if all(v == "ok" for v in results.values()):
        return {"status": "ready", "checks": results}
    return JSONResponse(status_code=503, content={"status": "unavailable", "checks": results})
