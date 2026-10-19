"""EDG-074: the single-replica guard against a real Postgres (testcontainers)."""

import asyncio

import pytest
import structlog
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from apps.api import replica_lock
from apps.api.replica_lock import ReplicaLockError, start_replica_guard, stop_replica_guard
from tests.conftest import _asyncpg_url


@pytest.fixture
def engine(postgres_env):
    eng = create_async_engine(_asyncpg_url(postgres_env))
    yield eng


@pytest.fixture(autouse=True)
async def _clean():
    yield
    await stop_replica_guard()


async def _second(engine, wait="0"):
    """Try to take the lock as another process would; releases at once on success."""
    other = replica_lock.ReplicaLock(replica_lock.connect_kwargs_from_engine(engine))
    await other.acquire(float(wait))
    await other.release()


async def test_second_replica_refused_first_keeps_running(engine):
    first = await start_replica_guard(engine, {})
    assert first is not None and first.state == "held"
    with pytest.raises(ReplicaLockError) as exc:
        await _second(engine)
    message = str(exc.value)
    assert "exactly one replica" in message
    url = make_url(str(engine.url.render_as_string(hide_password=False)))
    for secret in (url.password, url.host, str(url.port), url.username):
        assert secret and secret not in message
    assert first.state == "held"
    assert replica_lock.replica_lock_state() == "ok"
    assert await first._conn.fetchval("SELECT 1") == 1


async def test_release_lets_successor_start(engine):
    await start_replica_guard(engine, {})
    await stop_replica_guard()
    assert replica_lock.replica_lock_state() is None
    successor = await start_replica_guard(engine, {})
    assert successor is not None and successor.state == "held"


async def test_successor_waits_for_a_releasing_predecessor(engine):
    await start_replica_guard(engine, {})
    waiter = asyncio.create_task(_second(engine, wait="5"))
    await asyncio.sleep(0.7)
    assert not waiter.done()
    await stop_replica_guard()
    await asyncio.wait_for(waiter, 5)


async def test_dead_connection_frees_lock(engine):
    first = await start_replica_guard(engine, {})
    first._conn.terminate()  # what a crashed process looks like to the server
    first._task.cancel()
    await _second(engine, wait="5")


async def test_opt_out_allows_and_warns(engine, monkeypatch):
    monkeypatch.setattr(replica_lock, "log", structlog.get_logger())
    await start_replica_guard(engine, {})
    with structlog.testing.capture_logs() as logs:
        assert await start_replica_guard(engine, {"EDGAR_ALLOW_MULTIPLE_REPLICAS": "1"}) is None
    assert any(
        e["event"] == "replica_guard_disabled_unsupported" and e["log_level"] == "warning"
        for e in logs
    )


async def test_default_is_enforced_and_bad_wait_rejected(engine):
    assert not replica_lock.multiple_replicas_allowed({})
    with pytest.raises(ReplicaLockError):
        await start_replica_guard(engine, {"EDGAR_REPLICA_LOCK_WAIT_SEC": "soon"})


async def test_unreachable_database_fails_safely():
    eng = create_async_engine("postgresql+asyncpg://u:hunter2@127.0.0.1:1/db")
    with pytest.raises(ReplicaLockError) as exc:
        await start_replica_guard(eng, {})
    assert "hunter2" not in str(exc.value) and "127.0.0.1" not in str(exc.value)


async def test_app_lifespan_enforces_guard(engine, monkeypatch, tmp_path):
    from asgi_lifespan import LifespanManager

    import db.postgres.session as sm
    from apps.api.main import app
    from apps.api.services import adventures_meta

    monkeypatch.delenv("EDGAR_ALLOW_MULTIPLE_REPLICAS", raising=False)
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    monkeypatch.setattr(adventures_meta, "PDF_DIR", tmp_path / "pdfs")
    monkeypatch.setattr(sm, "engine", engine)
    async with LifespanManager(app):
        assert replica_lock.replica_lock_state() == "ok"
        with pytest.raises(ReplicaLockError):
            await _second(engine)
    assert replica_lock.replica_lock_state() is None
    await _second(engine)  # released on shutdown
