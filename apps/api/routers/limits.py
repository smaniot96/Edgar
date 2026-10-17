"""Developer-only view of current limit usage against configured limits (EDG-066)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from ..dependencies import get_redis
from ..limits import usage_snapshot
from ..schemas.turn import debug_turns_enabled
from ..services.safe_errors import classify_error

router = APIRouter(tags=["limits"])


@router.get("/limits/usage")
async def limits_usage(redis: Redis = Depends(get_redis)):
    """Global concurrency, spend and configured limits. Developer only (EDGAR_DEBUG).

    Per-client counters are never returned, so the response carries no client identifiers.
    """
    if not debug_turns_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    try:
        return await usage_snapshot(redis)
    except RedisError:
        return JSONResponse(
            status_code=503, content=classify_error("limits_unavailable").to_payload()
        )
