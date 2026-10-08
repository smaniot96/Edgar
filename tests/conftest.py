"""Shared fixtures: Postgres (testcontainers), DB session, API client, Redis fake."""

from __future__ import annotations

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
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer

EDGAR_ROOT = Path(__file__).resolve().parents[1]

# Must happen before anything imports `edgar_core.config` (which reads the key once, and whose
# `load_dotenv` never overrides an existing variable): nodes short-circuit when the key is
# unset, so CI (no .env) would otherwise fail every LLM-path test, and local runs must never
# reach the real OpenAI API with the developer's key.
os.environ.setdefault("OPENAI_API_KEY", "test-key-for-ci")

# Locally a missing Docker daemon skips the DB-backed tests; under CI it is a hard failure so
# the suite can never go green by silently skipping them.
_REQUIRE_DOCKER = bool(os.environ.get("CI"))


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
        if _REQUIRE_DOCKER:
            pytest.fail(f"Docker is required in CI (CI is set) for Postgres testcontainers: {exc}")
        pytest.skip(f"Docker not available for Postgres testcontainers: {exc}")


@pytest.fixture(scope="session")
def postgres_env(postgres_container: PostgresContainer) -> dict[str, str]:
    env = _db_components_from_container(postgres_container)
    proc_env = {**os.environ, **env}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(EDGAR_ROOT / "db"),
        env=proc_env,
        check=True,
    )
    return env


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _test_async_engine(postgres_env: dict[str, str]) -> AsyncIterator[None]:
    import db.postgres.session as sm

    async_url = _asyncpg_url(postgres_env)
    await sm.engine.dispose()
    sm.engine = create_async_engine(
        async_url,
        echo=False,
        poolclass=NullPool,
    )
    sm.async_session_factory = async_sessionmaker(
        sm.engine, class_=AsyncSession, expire_on_commit=False
    )
    yield
    await sm.engine.dispose()


@pytest_asyncio.fixture
async def db_session(_test_async_engine: None) -> AsyncIterator[AsyncSession]:
    from sqlalchemy import text

    import db.postgres.session as sm

    async with sm.async_session_factory() as session:
        yield session
    async with sm.engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE event_log, combat_state, sessions, character_assignments, "
                "characters, npcs, world_flags, campaigns, users RESTART IDENTITY CASCADE"
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
    ):
        monkeypatch.setattr(f"{mod}.make_chat_model", _factory)


class SchemaKeyedFakeLLM:
    """Fake chat model whose structured outputs are keyed by schema class.

    `responses[Schema]` may be: a model instance (returned), an Exception (raised), a list
    (one item consumed per call; the last one sticks) or a callable `(messages) -> value`.
    A schema with no entry raises, which exercises the nodes' fallback paths. Every structured
    call is recorded in `calls` as `(schema, messages)`; free-text calls return `narration`.
    """

    def __init__(self) -> None:
        self.responses: dict[type, Any] = {}
        self.narration = "The DM describes what happens."
        self.calls: list[tuple[type | None, Any]] = []
        self.factory_kwargs: list[dict[str, Any]] = []

    def _next(self, schema: type, messages: Any) -> Any:
        self.calls.append((schema, messages))
        if schema not in self.responses:
            raise RuntimeError(f"no fake response for {schema.__name__}")
        value = self.responses[schema]
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        if isinstance(value, BaseException):
            raise value
        if callable(value) and not isinstance(value, type) and not hasattr(value, "model_dump"):
            value = value(messages)
        return value.model_copy(deep=True) if hasattr(value, "model_copy") else value

    def with_structured_output(self, schema: type, **_kw: Any) -> Any:
        fake = self

        class _Structured:
            async def ainvoke(self, messages: Any, *_a: Any, **_kw: Any) -> Any:
                return fake._next(schema, messages)

        return _Structured()

    async def ainvoke(self, messages: Any, *_a: Any, **_kw: Any) -> Any:
        from langchain_core.messages import AIMessage

        self.calls.append((None, messages))
        return AIMessage(content=self.narration)

    async def astream(self, messages: Any, *_a: Any, **_kw: Any) -> AsyncIterator[Any]:
        from langchain_core.messages import AIMessage

        self.calls.append((None, messages))
        yield AIMessage(content=self.narration)


@pytest.fixture
def fake_llm_schema(monkeypatch: pytest.MonkeyPatch) -> SchemaKeyedFakeLLM:
    """Install a `SchemaKeyedFakeLLM` everywhere `make_chat_model` is imported; return it."""
    fake = SchemaKeyedFakeLLM()

    def _factory(temperature: float = 0, **kwargs: Any) -> SchemaKeyedFakeLLM:
        fake.factory_kwargs.append({"temperature": temperature, **kwargs})
        return fake

    monkeypatch.setattr("agent.llm.make_chat_model", _factory)
    for mod in (
        "agent.nodes.input_parser",
        "agent.nodes.rules_adjudicator",
        "agent.nodes.narrator",
        "agent.nodes.memory_summarizer",
        "agent.graph_combat",
    ):
        monkeypatch.setattr(f"{mod}.make_chat_model", _factory, raising=False)
    # Monster Manual lookups would hit Qdrant/OpenAI; default to "no stat block found".
    monkeypatch.setattr("agent.graph_combat.retrieve_monster", lambda *_a, **_kw: [], raising=False)
    return fake
