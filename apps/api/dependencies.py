"""FastAPI dependencies: DB session, Redis client, per-session turn lock, current user.

The `get_session` dependency is re-exported from `db.postgres` so routers can `from
.dependencies import get_session` without reaching across packages.
"""

from fastapi import Depends, HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres import get_session
from db.postgres.models import User
from edgar_core.config import REDIS_URL

# Process-wide Redis connection. We do not pool per-request because the lock workload is
# trivial and reusing one client lets us close it cleanly in lifespan().
_redis: Redis | None = None

# A turn never legitimately runs longer than this; if it does, the lock auto-expires so a
# crashed worker cannot freeze a session forever.
TURN_LOCK_TTL = 60


async def current_user_id(db: AsyncSession = Depends(get_session)) -> int:
    """Single-user seam: returns the first user's id.

    Replace this with a real auth dependency when more than one user exists. Routers should
    depend on this instead of hard-coding `1` so the swap is local.
    """
    result = await db.execute(select(User).limit(1))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=400,
            detail="No user found. Open /ui to complete first-run setup.",
        )
    return user.id


async def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def acquire_turn_lock(session_id: int, redis: Redis) -> bool:
    """SET turn:{session_id} 1 NX EX TURN_LOCK_TTL. Returns True if we acquired the lock.

    A False return means another turn for this session is in flight; the caller returns 409.
    """
    return await redis.set(f"turn:{session_id}", "1", nx=True, ex=TURN_LOCK_TTL)


async def release_turn_lock(session_id: int, redis: Redis) -> None:
    """Always call from a `finally` so a crashed turn does not pin the lock for TURN_LOCK_TTL."""
    await redis.delete(f"turn:{session_id}")


__all__ = [
    "acquire_turn_lock",
    "close_redis",
    "current_user_id",
    "get_redis",
    "get_session",
    "release_turn_lock",
]
