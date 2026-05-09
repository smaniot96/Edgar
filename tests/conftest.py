"""Shared fixtures: Postgres (testcontainers), DB session, API client, Redis fake."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from docker.errors import DockerException
from fakeredis import FakeAsyncRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

EDGAR_ROOT = Path(__file__).resolve().parents[1]


def _db_components_from_container(pg: PostgresContainer) -> dict[str, str]:
    raw = pg.get_connection_url()
    parsed = urlparse(raw)
    path = (parsed.path or "").lstrip("/") or "postgres"
    user = parsed.username or "test"
    password = parsed.password or ""
    host = parsed.hostname or "localhost"
    port = str(parsed.port or 5432)
    return {
        "DB_HOST": host,
        "DB_PORT": port,
        "POSTGRES_USER": user,
        "POSTGRES_PASSWORD": password,
        "POSTGRES_DB": path,
    }


def _asyncpg_url(components: dict[str, str]) -> str:
    user = quote_plus(components["POSTGRES_USER"])
    password = quote_plus(components["POSTGRES_PASSWORD"])
    return (
        f"postgresql+asyncpg://{user}:{password}"
        f"@{components['DB_HOST']}:{components['DB_PORT']}/{components['POSTGRES_DB']}"
    )


@pytest.fixture(scope="session")
def postgres_container() -> PostgresContainer:
    try:
        with PostgresContainer("postgres:16-alpine") as pg:
            yield pg
    except DockerException as exc:
        pytest.skip(f"Docker not available for Postgres testcontainers: {exc}")


@pytest.fixture(scope="session")
def postgres_env(postgres_container: PostgresContainer) -> dict[str, str]:
    env = _db_components_from_container(postgres_container)
    proc_env = {**os.environ, **env, "OPENAI_API_KEY": "test-key-for-ci"}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(EDGAR_ROOT / "db"),
        env=proc_env,
        check=True,
    )
    return env


@pytest.fixture(scope="session")
def _patch_db_engine(postgres_env: dict[str, str]) -> None:
    import db.postgres.session as sm

    async_url = _asyncpg_url(postgres_env)

    async def _swap() -> None:
        await sm.engine.dispose()
        sm.engine = create_async_engine(async_url, echo=False, pool_pre_ping=True)
        sm.async_session_factory = async_sessionmaker(
            sm.engine, class_=AsyncSession, expire_on_commit=False
        )

    asyncio.run(_swap())
    try:
        yield
    finally:
        asyncio.run(sm.engine.dispose())


@pytest_asyncio.fixture
async def db_session(_patch_db_engine: None) -> AsyncIterator[AsyncSession]:
    from sqlalchemy import text

    import db.postgres.session as sm

    async with sm.async_session_factory() as session:
        yield session
    async with sm.engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE event_log, combat_state, sessions, characters, npcs, "
                "world_flags, campaigns, users RESTART IDENTITY CASCADE"
            )
        )


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def api_client(
    db_session: AsyncSession,
    redis_client: FakeAsyncRedis,
) -> AsyncIterator[AsyncClient]:
    from apps.api.dependencies import get_redis, get_session
    from apps.api.main import app

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def override_get_redis() -> AsyncIterator[FakeAsyncRedis]:
        yield redis_client

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_redis] = override_get_redis
    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    app.dependency_overrides.clear()


@pytest.fixture
def fake_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub Qdrant-backed retrieval (no embeddings, no vector DB)."""

    def _rules(*_a: Any, **_kw: Any) -> list[dict[str, Any]]:
        return [{"text": "Stub rule text.", "source": "PHB", "kind": "rules", "page": 1}]

    def _adv(*_a: Any, **_kw: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr("agent.nodes.world_retriever.search_rules_context", _rules)
    monkeypatch.setattr("agent.nodes.world_retriever.search_adventure_context", _adv)


@pytest.fixture
def fake_llm_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic LLM outputs for a single non-combat graph run."""

    from langchain_core.messages import AIMessage

    from agent.models.adjudication import AdjudicationResult, CharacterUpdate, FlagUpdate
    from agent.models.parsed_input import ParsedInput

    class _Structured:
        def __init__(self, value: Any) -> None:
            self._value = value

        async def ainvoke(self, *_a: Any, **_kw: Any) -> Any:
            return self._value

    class _FakeChat:
        def __init__(self) -> None:
            self._parsed = ParsedInput(
                intent="exploration",
                entities={},
                dice_expression=None,
            )
            self._adj = AdjudicationResult(
                success=True,
                mechanical_summary="A goblin arrow finds you.",
                character_update=CharacterUpdate(hp_delta=-5),
                flags_set=[FlagUpdate(key="goblin_ambush", value="resolved")],
                scene_id="forest_trail",
            )
            self._narration = "The arrow grazes your shoulder; you mark a wound."

        def with_structured_output(self, schema: type, **_kw: Any) -> _Structured:
            if schema is ParsedInput:
                return _Structured(self._parsed)
            if schema is AdjudicationResult:
                return _Structured(self._adj)
            raise AssertionError(f"unexpected structured schema {schema}")

        async def ainvoke(self, *_a: Any, **_kw: Any) -> AIMessage:
            return AIMessage(content=self._narration)

        async def astream(self, *_a: Any, **_kw: Any) -> AsyncIterator[Any]:
            yield AIMessage(content=self._narration)

    fake = _FakeChat()

    def _factory(temperature: float = 0) -> _FakeChat:  # noqa: ARG001
        return fake

    monkeypatch.setattr("agent.llm.make_chat_model", _factory)
    for mod in (
        "agent.nodes.input_parser",
        "agent.nodes.rules_adjudicator",
        "agent.nodes.narrator",
        "agent.nodes.memory_summarizer",
        "agent.graph_combat",
        "apps.api.services.turn_runner",
    ):
        monkeypatch.setattr(f"{mod}.make_chat_model", _factory)
