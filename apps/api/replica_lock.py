"""Single-replica guard (EDG-074, option A).

Edgar runs generation and ingestion in-process, keeps source status in metadata files and keeps
per-process stream/drain state, so exactly one API replica may use a database. At startup the
process takes a Postgres *session-level advisory lock* on a dedicated, unpooled connection and
holds it for its lifetime. A second API process against the same database fails fast.

Why Postgres and not Redis: Postgres is the authoritative store every replica would share. The
lock is bound to the connection, so it disappears when the process exits or its connection
dies, with no TTL to tune. The pooled engine is unsuitable (a session lock belongs to one
backend connection, and the pool recycles them), hence the dedicated connection. A heartbeat
detects a lost connection, re-takes the lock if it is still free, and otherwise stops the
process rather than keep serving beside another replica.

``EDGAR_ALLOW_MULTIPLE_REPLICAS=1`` disables the guard for tests and development only.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Mapping
from typing import Any

import asyncpg
import structlog

log = structlog.get_logger()

ALLOW_MULTIPLE_ENV = "EDGAR_ALLOW_MULTIPLE_REPLICAS"
WAIT_ENV = "EDGAR_REPLICA_LOCK_WAIT_SEC"
# Fixed 64-bit key of the lock inside the database (advisory locks are per database).
LOCK_KEY = 7_407_400_074_000_001
DEFAULT_WAIT_SEC = 10.0
HEARTBEAT_SEC = 15.0
CONNECT_TIMEOUT_SEC = 5.0
DOC_HINT = "docs/hosting-hardening.md (Supported topology: one API replica)"

DISABLED = "disabled"
HELD = "held"
LOST = "lost"
NOT_STARTED = "not_started"


class ReplicaLockError(RuntimeError):
    """Startup refused. The message never carries hosts, users or credentials."""


class ReplicaLock:
    def __init__(self, connect_kwargs: Mapping[str, Any]):
        self._kwargs = dict(connect_kwargs)
        self._conn: asyncpg.Connection | None = None
        self._task: asyncio.Task | None = None
        self.state = NOT_STARTED

    async def _open_and_try_lock(self) -> bool:
        conn = await asyncpg.connect(
            **self._kwargs,
            timeout=CONNECT_TIMEOUT_SEC,
            command_timeout=CONNECT_TIMEOUT_SEC,
            server_settings={
                "application_name": "edgar-api-replica-lock",
                # A dead peer is noticed by the server within about a minute, which frees the lock.
                "tcp_keepalives_idle": "30",
                "tcp_keepalives_interval": "10",
                "tcp_keepalives_count": "3",
            },
        )
        try:
            got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY)
        except BaseException:
            await conn.close()
            raise
        if not got:
            await conn.close()
            return False
        self._conn = conn
        return True

    async def acquire(self, wait_sec: float) -> None:
        deadline = time.monotonic() + max(wait_sec, 0.0)
        while True:
            try:
                if await self._open_and_try_lock():
                    break
            except (TimeoutError, OSError, asyncpg.PostgresError) as exc:
                log.error("replica_lock_database_unreachable", error_class=type(exc).__name__)
                raise ReplicaLockError(
                    "Cannot reach the database to take the API replica lock."
                ) from None
            if time.monotonic() >= deadline:
                log.error("replica_lock_refused")
                raise ReplicaLockError(
                    "Another API replica is running against this database; Edgar supports "
                    f"exactly one replica. See {DOC_HINT}."
                )
            await asyncio.sleep(0.5)  # a restarting predecessor may still be releasing it
        self.state = HELD
        self._task = asyncio.create_task(self._heartbeat(), name="replica-lock-heartbeat")
        log.info("replica_lock_acquired")

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SEC)
            try:
                assert self._conn is not None
                await self._conn.fetchval("SELECT 1")
                continue
            except Exception as exc:  # noqa: BLE001 - any failure means the lock may be gone
                log.warning("replica_lock_connection_lost", error_class=type(exc).__name__)
            self.state = LOST
            with contextlib.suppress(Exception):
                if self._conn is not None:
                    await self._conn.close(timeout=1)
            self._conn = None
            try:
                if await self._open_and_try_lock():
                    self.state = HELD
                    log.info("replica_lock_reacquired")
                    continue
            except Exception as exc:  # noqa: BLE001
                log.warning("replica_lock_reacquire_failed", error_class=type(exc).__name__)
                continue  # database unreachable: stay LOST (not ready) and keep trying
            log.critical("replica_lock_lost_to_another_replica")
            os.kill(os.getpid(), signal.SIGTERM)  # graceful shutdown rather than split-brain
            return

    async def release(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)
            with contextlib.suppress(Exception):
                await conn.close(timeout=2)
        self.state = NOT_STARTED


_active: ReplicaLock | None = None


def multiple_replicas_allowed(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get(ALLOW_MULTIPLE_ENV) or "").strip() == "1"


def connect_kwargs_from_engine(engine: Any) -> dict[str, Any]:
    url = engine.url
    return {
        "host": url.host,
        "port": url.port,
        "user": url.username,
        "password": url.password,
        "database": url.database,
    }


async def start_replica_guard(
    engine: Any, environ: Mapping[str, str] | None = None
) -> ReplicaLock | None:
    """Take the lock (or log the opt-out). Raises ``ReplicaLockError`` when another replica holds it."""
    global _active
    env = os.environ if environ is None else environ
    if multiple_replicas_allowed(env):
        log.warning(
            "replica_guard_disabled_unsupported",
            detail=f"{ALLOW_MULTIPLE_ENV}=1: running several API replicas is unsupported and unsafe",
        )
        return None
    try:
        wait = float(env.get(WAIT_ENV) or DEFAULT_WAIT_SEC)
    except ValueError:
        raise ReplicaLockError(f"{WAIT_ENV} must be a number of seconds.") from None
    lock = ReplicaLock(connect_kwargs_from_engine(engine))
    await lock.acquire(wait)
    _active = lock
    return lock


async def stop_replica_guard() -> None:
    global _active
    lock, _active = _active, None
    if lock is not None:
        await lock.release()


def replica_lock_state() -> str | None:
    """``ok``/``unavailable`` while the guard is enforced, ``None`` when it is not."""
    if _active is None:
        return None
    return "ok" if _active.state == HELD else "unavailable"
