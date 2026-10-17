"""`/ready` releases its database connection before the Redis and Qdrant probes.

Pure unit tests: a counting fake session stands in for the pooled connection (checked out by the
first statement, returned by `close()`), and fake Redis/Qdrant probes record whether that
connection was still held when they ran. No Docker, database or network access.
"""

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.api.dependencies import get_redis, get_session
from apps.api.routers import health

OK_CHECKS = {"postgres": "ok", "schema": "ok", "redis": "ok", "qdrant": "ok"}


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class CountingSession:
    """Mimics AsyncSession's connection lifetime: checked out on first use, returned on close."""

    def __init__(self, revision: str):
        self.revision = revision
        self.checked_out = False
        self.checkouts = 0

    async def execute(self, *_args, **_kwargs):
        if not self.checked_out:
            self.checked_out = True
            self.checkouts += 1
        return _Result(self.revision)

    async def close(self) -> None:
        self.checked_out = False


class ProbeRecorder:
    """Records whether the connection was held at the moment an external probe started."""

    def __init__(self, session: CountingSession):
        self.session = session
        self.held_at_probe: dict[str, bool] = {}


def _app(session: CountingSession, redis_probe, monkeypatch, qdrant_probe) -> FastAPI:
    app = FastAPI()
    app.include_router(health.router)

    async def override_session():
        try:
            yield session
        finally:
            await session.close()

    class _Redis:
        async def ping(self):
            await redis_probe()

    async def override_redis():
        return _Redis()

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_redis] = override_redis
    monkeypatch.setattr(health, "_check_qdrant", qdrant_probe)
    return app


def _probe(recorder: ProbeRecorder, name: str, delay: float, fail: bool):
    async def probe(*_args):
        recorder.held_at_probe[name] = recorder.session.checked_out
        await asyncio.sleep(delay)
        if fail:
            raise ConnectionError("provider said: connection refused at 10.0.0.7")

    return probe


async def _get_ready(app: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/ready")


@pytest.fixture
def revision() -> str:
    return health._code_head()


@pytest.mark.asyncio
async def test_connection_is_released_before_slow_redis_and_qdrant(monkeypatch, revision):
    session = CountingSession(revision)
    recorder = ProbeRecorder(session)
    redis_probe = _probe(recorder, "redis", delay=0.05, fail=False)
    qdrant_probe = _probe(recorder, "qdrant", delay=0.05, fail=False)

    app = _app(session, redis_probe, monkeypatch, qdrant_probe)
    resp = await _get_ready(app)

    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "checks": OK_CHECKS}
    assert recorder.held_at_probe == {"redis": False, "qdrant": False}
    assert session.checkouts == 1  # the database probes shared one connection


@pytest.mark.asyncio
async def test_failed_redis_keeps_the_same_body_and_releases_connection(monkeypatch, revision):
    session = CountingSession(revision)
    recorder = ProbeRecorder(session)
    redis_probe = _probe(recorder, "redis", delay=0.05, fail=True)

    async def qdrant(*_args):
        recorder.held_at_probe["qdrant"] = session.checked_out

    app = _app(session, redis_probe, monkeypatch, qdrant)
    resp = await _get_ready(app)

    assert resp.status_code == 503
    assert resp.json() == {
        "status": "unavailable",
        "checks": {"postgres": "ok", "schema": "ok", "redis": "unavailable", "qdrant": "ok"},
    }
    assert recorder.held_at_probe == {"redis": False, "qdrant": False}


@pytest.mark.asyncio
async def test_failed_qdrant_keeps_the_same_body_and_releases_connection(monkeypatch, revision):
    session = CountingSession(revision)
    held: list[bool] = []

    async def redis_probe():
        held.append(session.checked_out)
        await asyncio.sleep(0.05)

    async def qdrant(*_args):
        held.append(session.checked_out)
        await asyncio.sleep(0.05)
        raise ConnectionError("provider said: connection refused at 10.0.0.7")

    app = _app(session, redis_probe, monkeypatch, qdrant)
    resp = await _get_ready(app)

    assert resp.status_code == 503
    assert resp.json() == {
        "status": "unavailable",
        "checks": {"postgres": "ok", "schema": "ok", "redis": "ok", "qdrant": "unavailable"},
    }
    assert held == [False, False]


@pytest.mark.asyncio
async def test_schema_behind_head_keeps_the_same_body(monkeypatch, revision):
    session = CountingSession("14_session_story_ledger")
    held: list[bool] = []

    async def redis_probe():
        held.append(session.checked_out)

    async def qdrant(*_args):
        return None

    app = _app(session, redis_probe, monkeypatch, qdrant)
    resp = await _get_ready(app)

    assert resp.status_code == 503
    assert resp.json() == {
        "status": "unavailable",
        "checks": {"postgres": "ok", "schema": "unavailable", "redis": "ok", "qdrant": "ok"},
    }
    assert held == [False]
