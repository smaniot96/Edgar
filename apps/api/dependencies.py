"""FastAPI dependencies: DB session, Redis client, per-session turn lock, current user.

The `get_session` dependency is re-exported from `db.postgres` so routers can `from
.dependencies import get_session` without reaching across packages.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import structlog
from fastapi import Depends, HTTPException
from redis.asyncio import Redis
from redis.exceptions import WatchError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres import get_session
from db.postgres.models import User
from edgar_core.config import REDIS_URL

log = structlog.get_logger()

# Process-wide Redis connection. We do not pool per-request because the lock workload is
# trivial and reusing one client lets us close it cleanly in lifespan().
_redis: Redis | None = None

# The lock key expires after TURN_LOCK_TTL seconds unless its holder renews it. A running turn
# renews every TURN_LOCK_RENEW_INTERVAL seconds, so a slow turn (several LLM calls) keeps the
# lock for as long as it runs, while a crashed worker frees the session within one TTL.
TURN_LOCK_TTL = 30
TURN_LOCK_RENEW_INTERVAL = 10
# Renewal stops after this long, so a lock whose holder never releases it (e.g. a stream the
# client abandoned before it started) still expires instead of being renewed forever.
TURN_LOCK_MAX_HOLD = 300


async def current_user_id(db: AsyncSession = Depends(get_session)) -> int:
    """Single-user seam: returns the first user's id (lowest id, so it is deterministic).

    Replace this with a real auth dependency when more than one user exists. Routers should
    depend on this instead of hard-coding `1` so the swap is local.
    """
    result = await db.execute(select(User).order_by(User.id).limit(1))
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


class TurnLock:
    """Owner-token lock on `turn:{session_id}`, renewed in the background while held.

    The key's value is a random token, and release/renew only act when the key still holds
    *our* token (compare-and-delete / compare-and-expire under WATCH/MULTI). So a turn whose
    lock already expired can never delete or extend the lock a newer turn now holds. WATCH is
    used instead of a Lua script so the same code runs against fakeredis in tests.
    """

    def __init__(
        self,
        redis: Redis,
        session_id: int,
        *,
        ttl: float = TURN_LOCK_TTL,
        renew_interval: float = TURN_LOCK_RENEW_INTERVAL,
        max_hold: float = TURN_LOCK_MAX_HOLD,
    ) -> None:
        self.redis = redis
        self.session_id = session_id
        self.key = f"turn:{session_id}"
        self.token = uuid.uuid4().hex
        self.ttl = ttl
        self.renew_interval = renew_interval
        self.max_hold = max_hold
        self._renew_task: asyncio.Task | None = None

    def _ttl_ms(self) -> int:
        return max(1, int(self.ttl * 1000))

    async def acquire(self) -> bool:
        """SET key token NX PX ttl. On success, start the background renewal task."""
        acquired = await self.redis.set(self.key, self.token, nx=True, px=self._ttl_ms())
        if acquired:
            self._renew_task = asyncio.create_task(self._renew_loop())
        return bool(acquired)

    def _owns(self, current: str | bytes | None) -> bool:
        if isinstance(current, bytes):
            current = current.decode()
        return current == self.token

    async def _compare_and(self, action: str) -> bool:
        """Atomically delete ("delete") or re-arm ("extend") the key iff it still holds our token."""
        async with self.redis.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(self.key)
                    if not self._owns(await pipe.get(self.key)):
                        await pipe.unwatch()
                        return False
                    pipe.multi()
                    if action == "delete":
                        pipe.delete(self.key)
                    else:
                        pipe.pexpire(self.key, self._ttl_ms())
                    await pipe.execute()
                    return True
                except WatchError:
                    # The key changed between WATCH and EXEC; re-check ownership.
                    continue

    async def extend(self) -> bool:
        return await self._compare_and("extend")

    async def _renew_loop(self) -> None:
        deadline = asyncio.get_running_loop().time() + self.max_hold
        while True:
            await asyncio.sleep(self.renew_interval)
            if asyncio.get_running_loop().time() >= deadline:
                log.warning("turn_lock_max_hold_reached", session_id=self.session_id)
                return
            try:
                if not await self.extend():
                    log.warning("turn_lock_lost", session_id=self.session_id)
                    return
            except Exception:
                # A transient Redis error should not kill the turn; the next tick retries.
                log.warning("turn_lock_renew_failed", session_id=self.session_id, exc_info=True)

    async def release(self) -> None:
        """Stop renewing and delete the key only if we still own it. Safe to call twice."""
        if self._renew_task is not None:
            self._renew_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._renew_task
            self._renew_task = None
        try:
            await self._compare_and("delete")
        except Exception:
            # The key still expires on its own within one TTL.
            log.warning("turn_lock_release_failed", session_id=self.session_id, exc_info=True)


async def acquire_turn_lock(session_id: int, redis: Redis) -> TurnLock | None:
    """Take the per-session turn lock. Returns the held lock, or None if another turn (or
    intro) for this session is in flight — the caller returns 409.

    Always `await lock.release()` from a `finally`.
    """
    lock = TurnLock(redis, session_id)
    if await lock.acquire():
        return lock
    return None


__all__ = [
    "TurnLock",
    "acquire_turn_lock",
    "close_redis",
    "current_user_id",
    "get_redis",
    "get_session",
]
