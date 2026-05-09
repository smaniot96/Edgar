"""FastAPI dependencies: DB session, Redis, agent graph."""

from fastapi import Depends, HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from edgar_core.config import REDIS_URL
from db.postgres import get_session
from db.postgres.models import User


_redis: Redis | None = None


async def current_user_id(db: AsyncSession = Depends(get_session)) -> int:
    """Solo-dev: authoritative user id from seeded `dm@edgar.local` (call POST /api/seed first)."""
    result = await db.execute(select(User).where(User.email == "dm@edgar.local").limit(1))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=400,
            detail="No user found. Call POST /api/seed first.",
        )
    return user.id


async def get_redis() -> Redis:
    """Return Redis client. Reused across requests."""
    global _redis
    if _redis is None:
        _redis = Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


async def close_redis() -> None:
    """Close Redis connection on shutdown."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


TURN_LOCK_TTL = 60


async def acquire_turn_lock(session_id: int, redis: Redis) -> bool:
    """Acquire lock for session turn. Key: turn:{session_id}. TTL 60s. Returns True if acquired."""
    key = f"turn:{session_id}"
    return await redis.set(key, "1", nx=True, ex=TURN_LOCK_TTL)


async def release_turn_lock(session_id: int, redis: Redis) -> None:
    """Release turn lock for session."""
    key = f"turn:{session_id}"
    await redis.delete(key)


__all__ = [
    "get_session",
    "get_redis",
    "close_redis",
    "acquire_turn_lock",
    "release_turn_lock",
    "current_user_id",
]
