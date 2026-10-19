"""Offline, deterministic capacity harness (EDG-071): connection demand, provider concurrency, latency.

What it drives, all in one process with no network, no Docker and no live provider:

- `runner`: N concurrent player turns through the real SSE turn runner (`stream_session_turn`), the
  real LangGraph combat path and fake delayed chat models. `persist_turn` is a stand-in with the same
  transaction shape (one session, several statements, one commit).
- `route`: N concurrent `POST /api/sessions/{id}/turn/stream` requests through the real FastAPI route
  and its real `get_session` dependency lifecycle. The initial-state loader is a stand-in with the
  same statement count and a commit at its end, as in `_load_turn_initial_state`.
- `mixed`: `route` plus the real background memory-summary job after every turn (worst case: every
  turn publishes a summary, while the real cadence is every 5 turns) plus a readiness-probe loop.
- `summary`: N concurrent real `maybe_update_memory_summary` jobs. The two DB helpers are stand-ins
  with the same `async with session, begin()` shape; `summarize_story` runs for real.
- `generation`: N concurrent `medium` campaign generations (6 chapters) through the real
  `_expand_chapter` with the per-generation semaphore of six, as in `campaign_generator`.

What is measured: every DB session is a counting fake. A session holds a pooled connection from its
first statement until commit, rollback or close (SQLAlchemy autobegin semantics). A pool cap (default
15 = SQLAlchemy `pool_size` 5 + `max_overflow` 10) models queueing; "connection demand" counts sessions
waiting for or holding a connection and is the sizing input. Each unit of work (turn, summary job,
generation) is attributed through a contextvar, so the harness can check whether any unit holds a
connection while one of its own provider calls is pending.

What it does NOT establish: production latency or throughput; real database latency (statement delays
are modelled constants); real provider timing (provider delays are fixed fixture sleeps); container
memory or CPU (no container runs here). Counts of connections and in-flight calls are structural.
Timings are relative fixture behaviour only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import random
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, aclosing, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from unittest import mock

if __package__ in (None, ""):  # `python tools/capacity_harness.py` from any directory
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCHEMA = "edgar.capacity_harness/1"
ALL_SCENARIOS = ("runner", "route", "mixed", "summary", "generation")
DEFAULT_LEVELS = (1, 2, 4, 8, 16, 32)
# SQLAlchemy QueuePool defaults for create_async_engine without pool arguments: 5 + 10.
DEFAULT_POOL_CAP = 15
LOADER_STATEMENTS = 10  # _load_turn_initial_state issues about this many reads before its commit
PERSIST_STATEMENTS = 4  # persist_turn stand-in: writes inside one transaction
PROBE_STATEMENTS = 2  # /ready: postgres + schema checks on one session
GENERATION_CHAPTERS = 6  # "medium" size tier
GENERATION_CONCURRENCY = 6  # campaign_generator._MAX_CONCURRENT
MESSAGE = "I attack the goblin"
CHAT_MODULES = (
    "agent.llm",
    "agent.nodes.input_parser",
    "agent.nodes.rules_adjudicator",
    "agent.nodes.narrator",
    "agent.nodes.memory_summarizer",
    "agent.graph_combat",
    "apps.api.services.campaign_generator",
)
NARRATION_CHUNKS = ["The goblin ", "lunges ", "at you, ", "and Eda steadies her shield ", "against the wall."]
CHAPTER_TEXT = (
    "The party crosses the flooded cellar while torches gutter and an old bell tolls once more "
    "across the valley below the ruined chapel tonight."
)


@dataclass(frozen=True)
class Timing:
    """Fixture delays in milliseconds. Fixed sleeps; none is a measurement of a real system."""

    provider_ms: float = 40.0  # per provider call, before its output
    chunk_ms: float = 8.0  # per streamed narration chunk
    statement_ms: float = 2.0  # per database statement (modelled)
    probe_external_ms: float = 20.0  # modelled Redis/Qdrant wait inside /ready
    probe_interval_ms: float = 25.0  # probe pressure in `mixed`, far above the 15 s healthcheck


@dataclass
class Unit:
    """One unit of work (turn, summary job, generation). Attribution for connection and provider checks."""

    name: str
    held: int = 0
    max_held: int = 0
    inflight: int = 0
    max_inflight: int = 0
    violations: int = 0
    ok: bool = False
    latency_ms: float | None = None
    ttft_ms: float | None = None


_CURRENT: ContextVar[Unit | None] = ContextVar("capacity_unit", default=None)


class Ledger:
    """Counts sessions, connection demand and provider calls for one concurrency level."""

    def __init__(self, pool_cap: int | None) -> None:
        self.pool_cap = pool_cap or None
        self._slots = asyncio.Semaphore(self.pool_cap) if self.pool_cap else None
        self.demand = self.peak_demand = 0
        self.held = self.peak_held = 0
        self.open_sessions = self.peak_open_sessions = 0
        self.provider_inflight = self.peak_provider_inflight = 0
        self.provider_calls = 0
        self.statements = 0
        self.violations = 0
        self.waits_ms: list[float] = []
        self.queued = 0  # acquisitions that found every pool slot taken and had to wait

    async def acquire(self, unit: Unit | None) -> None:
        self.demand += 1
        self.peak_demand = max(self.peak_demand, self.demand)
        started = time.perf_counter()
        if self._slots is not None:
            if self._slots.locked():
                self.queued += 1
            await self._slots.acquire()
        self.waits_ms.append((time.perf_counter() - started) * 1000)
        self.held += 1
        self.peak_held = max(self.peak_held, self.held)
        if unit is not None:
            unit.held += 1
            unit.max_held = max(unit.max_held, unit.held)
            if unit.inflight:  # a connection taken while this unit's provider call is pending
                self.violations += 1
                unit.violations += 1

    def release(self, unit: Unit | None) -> None:
        self.held -= 1
        self.demand -= 1
        if unit is not None:
            unit.held -= 1
        if self._slots is not None:
            self._slots.release()

    def provider_begin(self) -> Unit | None:
        unit = _CURRENT.get()
        self.provider_calls += 1
        self.provider_inflight += 1
        self.peak_provider_inflight = max(self.peak_provider_inflight, self.provider_inflight)
        if unit is not None:
            unit.inflight += 1
            unit.max_inflight = max(unit.max_inflight, unit.inflight)
            if unit.held:  # a connection held while this unit's provider call starts
                self.violations += 1
                unit.violations += 1
        return unit

    def provider_end(self, unit: Unit | None) -> None:
        self.provider_inflight -= 1
        if unit is not None:
            unit.inflight -= 1


class _Txn:
    def __init__(self, session: CountingSession) -> None:
        self._session = session

    async def __aenter__(self) -> CountingSession:
        return self._session

    async def __aexit__(self, exc_type: Any, *_exc: Any) -> None:
        if exc_type is None:
            await self._session.commit()
        else:
            await self._session.rollback()


class _Result:
    def scalar_one_or_none(self) -> None:
        return None

    def scalar(self) -> None:
        return None

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return []

    def first(self) -> None:
        return None


class CountingSession:
    """Stands in for AsyncSession. A connection is held from the first statement until commit/rollback/close."""

    def __init__(self, ledger: Ledger, statement_s: float) -> None:
        self._ledger = ledger
        self._statement_s = statement_s
        self._unit: Unit | None = None
        self._connected = False

    async def __aenter__(self) -> CountingSession:
        self._ledger.open_sessions += 1
        self._ledger.peak_open_sessions = max(self._ledger.peak_open_sessions, self._ledger.open_sessions)
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def _connect(self) -> None:
        if not self._connected:
            self._unit = _CURRENT.get()
            await self._ledger.acquire(self._unit)
            self._connected = True

    async def _statement(self) -> _Result:
        await self._connect()
        await asyncio.sleep(self._statement_s)
        self._ledger.statements += 1
        return _Result()

    async def execute(self, *_a: Any, **_kw: Any) -> _Result:
        return await self._statement()

    async def scalar(self, *_a: Any, **_kw: Any) -> None:
        await self._statement()
        return None

    async def get(self, *_a: Any, **_kw: Any) -> None:
        await self._statement()
        return None

    async def flush(self) -> None:
        await self._connect()

    def begin(self) -> _Txn:
        return _Txn(self)

    async def commit(self) -> None:
        self._release()

    async def rollback(self) -> None:
        self._release()

    async def close(self) -> None:
        self._release()
        self._ledger.open_sessions -= 1

    def _release(self) -> None:
        if self._connected:
            self._connected = False
            self._ledger.release(self._unit)


class CountingFactory:
    def __init__(self, ledger: Ledger, statement_s: float) -> None:
        self.ledger = ledger
        self.statement_s = statement_s

    def __call__(self) -> CountingSession:
        return CountingSession(self.ledger, self.statement_s)


class DelayedChat:
    """Fake chat model: every call sleeps `provider_ms` before its output, and streams chunk by chunk."""

    def __init__(self, ledger: Ledger, timing: Timing) -> None:
        self._ledger = ledger
        self._timing = timing

    def with_structured_output(self, schema: type, **_kw: Any) -> _Structured:
        return _Structured(self, schema)

    def _text(self, messages: Any) -> str:
        return CHAPTER_TEXT if _is_chapter_request(messages) else "".join(NARRATION_CHUNKS)

    async def ainvoke(self, messages: Any, *_a: Any, **_kw: Any) -> Any:
        from langchain_core.messages import AIMessage

        unit = self._ledger.provider_begin()
        try:
            await asyncio.sleep(self._timing.provider_ms / 1000)
            return AIMessage(content=self._text(messages))
        finally:
            self._ledger.provider_end(unit)

    async def astream(self, messages: Any, *_a: Any, **_kw: Any) -> AsyncIterator[Any]:
        from langchain_core.messages import AIMessage

        unit = self._ledger.provider_begin()
        try:
            await asyncio.sleep(self._timing.provider_ms / 1000)
            for chunk in NARRATION_CHUNKS:
                await asyncio.sleep(self._timing.chunk_ms / 1000)
                yield AIMessage(content=chunk)
        finally:
            self._ledger.provider_end(unit)


def _is_chapter_request(messages: Any) -> bool:
    return any("Write Chapter" in str(getattr(m, "content", "")) for m in messages)


class _Structured:
    def __init__(self, chat: DelayedChat, schema: type) -> None:
        self._chat = chat
        self._schema = schema

    async def ainvoke(self, *_a: Any, **_kw: Any) -> Any:
        from agent.models.adjudication import AdjudicationResult
        from agent.models.parsed_input import ParsedInput

        answers = {
            ParsedInput: ParsedInput(intent="combat", check="attack", target="goblin"),
            AdjudicationResult: AdjudicationResult(success=True),
        }
        if self._schema not in answers:
            raise RuntimeError(f"no fake response for {self._schema.__name__}")
        unit = self._chat._ledger.provider_begin()
        try:
            await asyncio.sleep(self._chat._timing.provider_ms / 1000)
            return answers[self._schema]
        finally:
            self._chat._ledger.provider_end(unit)


def _turn_state(session_id: int) -> dict[str, Any]:
    from tests.agent.test_combat import _enemy, _player, _state

    state = _state([_player(init=20, ac=13), _enemy("Goblin", hp=500, init=5)], MESSAGE)
    state["session_id"] = session_id
    state["turn_id"] = f"turn-{session_id}"
    state["campaign_id"] = 1
    state["character"] = {**state["character"], "hp_current": 40, "hp_max": 40}
    return state


def _done_payload(result: dict[str, Any]) -> dict[str, Any]:
    """The `done` payload shape the route and runner read back after persist_turn (as the tests build it)."""
    from apps.api.services import turn_runner

    return {
        "event_id": 1,
        "narration": result.get("narration", ""),
        "citations": [],
        "ruling_citation_ids": [],
        "rules_edition": "2014",
        "character": None,
        "current_scene_id": None,
        "combat_state": turn_runner.combat_state_public(result.get("combat_state")),
        "campaign_complete": False,
        "retrieval_degraded": False,
        "source_status": [],
        "source_mode": "module",
        "roll_status": None,
        "roll": None,
    }


class Run:
    """Per-level wiring: one ledger and the stand-ins that count against it."""

    def __init__(self, timing: Timing, pool_cap: int | None, statement_s: float) -> None:
        self.timing = timing
        self.ledger = Ledger(pool_cap)
        self.factory = CountingFactory(self.ledger, statement_s)
        self.chat = DelayedChat(self.ledger, timing)

    def chat_factory(self, *_a: Any, **_kw: Any) -> DelayedChat:
        return self.chat

    async def persist(
        self, session_id: int, body_message: str, result: dict[str, Any], *, spotlight: Any = None
    ) -> dict[str, Any]:
        async with self.factory() as db, db.begin():
            for _ in range(PERSIST_STATEMENTS):
                await db.execute(None)
        return _done_payload(result)

    async def loader(self, session_id: int, message: str, db: CountingSession) -> dict[str, Any]:
        for _ in range(LOADER_STATEMENTS):
            await db.execute(None)
        await db.commit()
        return _turn_state(session_id)

    async def get_session_dependency(self) -> AsyncIterator[CountingSession]:
        async with self.factory() as session:
            yield session

    async def read_snapshot(self, session_id: int, *, min_turns: int) -> Any:
        from apps.api.services.session_messages import _MemorySnapshot

        async with self.factory() as db, db.begin():
            await db.execute(None)
        return _MemorySnapshot(
            base_summary_id=None,
            previous_summary=None,
            through_event_id=session_id,
            turns_summarized=5,
            messages=tuple(("human" if i % 2 == 0 else "ai", f"turn text {i}") for i in range(5)),
        )

    async def publish(self, session_id: int, snapshot: Any, summary: str) -> str:
        async with self.factory() as db, db.begin():
            await db.execute(None)
        return "published"


async def _noop_summary(*_a: Any, **_kw: Any) -> None:
    return None


def _rules_stub(*_a: Any, **_kw: Any) -> list[dict[str, Any]]:  # synchronous, as the real search is
    return [{"text": "Stub rule text.", "source": "PHB", "kind": "rules", "page": 1}]


@contextmanager
def _install(run: Run, *, summary_on_turn: bool) -> Iterator[None]:
    """Patch every seam the real code reaches for a provider, the DB or retrieval. Restored on exit."""
    import db.postgres.session as db_session_module
    from apps.api.dependencies import get_redis, get_session
    from apps.api.main import app as api_app
    from apps.api.routers import session as session_router
    from apps.api.services import session_messages, turn_runner

    with ExitStack() as stack:
        for module in CHAT_MODULES:
            stack.enter_context(mock.patch(f"{module}.make_chat_model", run.chat_factory))
        # The parser refuses to run without a key configured. The placeholder is never sent anywhere:
        # every chat model is the fake above.
        stack.enter_context(mock.patch("agent.nodes.input_parser.OPENAI_API_KEY", "offline-fixture"))
        stack.enter_context(mock.patch("agent.nodes.world_retriever.search_rules_context", _rules_stub))
        stack.enter_context(mock.patch.object(db_session_module, "async_session_factory", run.factory))
        stack.enter_context(mock.patch.object(turn_runner, "persist_turn", run.persist))
        stack.enter_context(mock.patch.object(session_router, "_load_turn_initial_state", run.loader))
        stack.enter_context(
            mock.patch.object(
                session_router,
                "maybe_update_memory_summary",
                session_messages.maybe_update_memory_summary if summary_on_turn else _noop_summary,
            )
        )
        stack.enter_context(mock.patch.object(session_messages, "_read_memory_snapshot", run.read_snapshot))
        stack.enter_context(mock.patch.object(session_messages, "_publish_memory_summary", run.publish))
        api_app.dependency_overrides[get_session] = run.get_session_dependency
        from fakeredis import FakeAsyncRedis

        redis = FakeAsyncRedis(decode_responses=True)

        async def fake_redis() -> AsyncIterator[Any]:
            yield redis

        api_app.dependency_overrides[get_redis] = fake_redis
        try:
            yield
        finally:
            api_app.dependency_overrides.pop(get_session, None)
            api_app.dependency_overrides.pop(get_redis, None)


async def _run_runner_unit(run: Run, index: int, seed: int) -> Unit:
    from apps.api.services import turn_runner
    from tools.dice import use_rng

    unit = Unit(name=f"turn-{index}")
    token = _CURRENT.set(unit)
    started = time.perf_counter()
    try:
        with use_rng(random.Random(seed + index)):
            async with aclosing(
                turn_runner.stream_session_turn(
                    session_id=index + 1, body_message=MESSAGE, initial_state=_turn_state(index + 1)
                )
            ) as frames:
                async for frame in frames:
                    if unit.ttft_ms is None and frame.startswith("event: token"):
                        unit.ttft_ms = (time.perf_counter() - started) * 1000
                    if frame.startswith("event: done"):
                        unit.ok = True
    finally:
        unit.latency_ms = (time.perf_counter() - started) * 1000
        _CURRENT.reset(token)
    return unit


async def _run_route_unit(client: Any, run: Run, index: int, seed: int) -> Unit:
    from tools.dice import use_rng

    unit = Unit(name=f"request-{index}")
    token = _CURRENT.set(unit)
    started = time.perf_counter()
    try:
        with use_rng(random.Random(seed + index)):
            response = await client.post(
                f"/api/sessions/{index + 1}/turn/stream", json={"message": MESSAGE}
            )
        body = response.text
        unit.ok = response.status_code == 200 and "event: done" in body and "event: error" not in body
    finally:
        unit.latency_ms = (time.perf_counter() - started) * 1000
        _CURRENT.reset(token)
    return unit


async def _run_summary_unit(index: int) -> Unit:
    from apps.api.services.session_messages import maybe_update_memory_summary

    unit = Unit(name=f"summary-{index}")
    token = _CURRENT.set(unit)
    started = time.perf_counter()
    try:
        await maybe_update_memory_summary(index + 1)
        unit.ok = True
    finally:
        unit.latency_ms = (time.perf_counter() - started) * 1000
        _CURRENT.reset(token)
    return unit


def _outline() -> Any:
    from apps.api.services.campaign_generator import CampaignOutline, ChapterOutline

    return CampaignOutline(
        title="Cellar of Bells",
        premise="A bell rings beneath the chapel.",
        setting="A flooded cellar under a ruined chapel.",
        hook="The bell has not rung in forty years.",
        factions=["the chapel wardens"],
        key_npcs=["Brother Oren"],
        chapters=[
            ChapterOutline(name=f"Chapter name {k}", summary="The party moves deeper into the cellar.")
            for k in range(GENERATION_CHAPTERS)
        ],
    )


async def _run_generation_unit(index: int) -> Unit:
    from apps.api.services.campaign_generator import _expand_chapter

    unit = Unit(name=f"generation-{index}")
    token = _CURRENT.set(unit)
    started = time.perf_counter()
    try:
        outline = _outline()
        # One semaphore per generation, as generate_campaign creates it (bound of six per run).
        sem = asyncio.Semaphore(GENERATION_CONCURRENCY)
        results = await asyncio.gather(
            *(_expand_chapter(outline, k, GENERATION_CHAPTERS, sem, "2014") for k in range(GENERATION_CHAPTERS))
        )
        unit.ok = len(results) == GENERATION_CHAPTERS
    finally:
        unit.latency_ms = (time.perf_counter() - started) * 1000
        _CURRENT.reset(token)
    return unit


async def _lag_sampler(samples: list[float], stop: asyncio.Event, interval_s: float = 0.005) -> None:
    while not stop.is_set():
        started = time.perf_counter()
        await asyncio.sleep(interval_s)
        samples.append(max(0.0, (time.perf_counter() - started - interval_s) * 1000))


async def _probe_loop(run: Run, stop: asyncio.Event, probes: list[int]) -> None:
    """A /ready-shaped probe: DB checks on one session, which stays open across the Redis/Qdrant waits."""
    unit = Unit(name="probe")
    token = _CURRENT.set(unit)
    try:
        while not stop.is_set():
            async with run.factory() as db:
                for _ in range(PROBE_STATEMENTS):
                    await db.execute(None)
                await asyncio.sleep(run.timing.probe_external_ms / 1000)
            probes.append(1)
            await asyncio.sleep(run.timing.probe_interval_ms / 1000)
    finally:
        _CURRENT.reset(token)


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def _dist(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {"p50": round(_pct(values, 50), 2), "p95": round(_pct(values, 95), 2), "max": round(max(values), 2)}


async def _run_level(scenario: str, n: int, run: Run, seed: int) -> dict[str, Any]:
    from apps.api.main import app as api_app

    lags: list[float] = []
    stop_lag = asyncio.Event()
    probes: list[int] = []
    stop_probe = asyncio.Event()
    summary_on_turn = scenario == "mixed"
    with _install(run, summary_on_turn=summary_on_turn):
        sampler = asyncio.create_task(_lag_sampler(lags, stop_lag))
        probe_task = asyncio.create_task(_probe_loop(run, stop_probe, probes)) if scenario == "mixed" else None
        started = time.perf_counter()
        try:
            if scenario == "runner":
                units = await asyncio.gather(*(_run_runner_unit(run, i, seed) for i in range(n)))
            elif scenario in ("route", "mixed"):
                from httpx import ASGITransport, AsyncClient

                async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
                    units = await asyncio.gather(*(_run_route_unit(client, run, i, seed) for i in range(n)))
            elif scenario == "summary":
                units = await asyncio.gather(*(_run_summary_unit(i) for i in range(n)))
            elif scenario == "generation":
                units = await asyncio.gather(*(_run_generation_unit(i) for i in range(n)))
            else:
                raise ValueError(f"unknown scenario {scenario!r}")
        finally:
            wall = time.perf_counter() - started
            stop_probe.set()
            if probe_task is not None:
                await probe_task
            stop_lag.set()
            await sampler
    ledger = run.ledger
    unit_list = list(units)
    ttfts = [u.ttft_ms for u in unit_list if u.ttft_ms is not None]
    return {
        "concurrency": n,
        "units": len(unit_list),
        "completed": sum(u.ok for u in unit_list),
        "failed": sum(not u.ok for u in unit_list),
        "wall_s": round(wall, 4),
        "latency_ms": _dist([u.latency_ms for u in unit_list if u.latency_ms is not None]),
        "ttft_ms": _dist(ttfts) if ttfts else None,
        "provider_calls": ledger.provider_calls,
        "max_inflight_provider_calls": ledger.peak_provider_inflight,
        "max_inflight_provider_calls_per_unit": max((u.max_inflight for u in unit_list), default=0),
        "peak_connection_demand": ledger.peak_demand,
        "peak_connections_held": ledger.peak_held,
        "max_connections_held_per_unit": max((u.max_held for u in unit_list), default=0),
        "connection_wait_ms": _dist(ledger.waits_ms),
        "connection_acquires": len(ledger.waits_ms),
        "connection_acquires_queued": ledger.queued,
        "pool_cap": ledger.pool_cap,
        "held_across_provider_violations": ledger.violations,
        "peak_open_sessions": ledger.peak_open_sessions,
        "statements": ledger.statements,
        "probes_completed": len(probes),
        "event_loop_lag_ms": _dist(lags),
    }


async def _run_all(
    *, levels: tuple[int, ...], scenarios: tuple[str, ...], timing: Timing, pool_cap: int | None, seed: int
) -> dict[str, list[dict[str, Any]]]:
    results: dict[str, list[dict[str, Any]]] = {}
    # Unrecorded warm-up: first use pays one-off imports and graph compilation, which would
    # otherwise inflate the first level's latency.
    for warm in ("runner", "route"):
        if warm in scenarios:
            await _run_level(warm, 1, Run(timing, pool_cap, timing.statement_ms / 1000), seed)
    for scenario in scenarios:
        rows = []
        for n in levels:
            run = Run(timing, pool_cap, timing.statement_ms / 1000)
            rows.append(await _run_level(scenario, n, run, seed))
        results[scenario] = rows
    return results


def run_harness(
    *,
    levels: tuple[int, ...] = DEFAULT_LEVELS,
    scenarios: tuple[str, ...] = ALL_SCENARIOS,
    timing: Timing | None = None,
    pool_cap: int | None = DEFAULT_POOL_CAP,
    seed: int = 7,
    out: Path | None = None,
) -> dict[str, Any]:
    """Run every requested scenario at every level. Writes JSON to `out` when given and returns it."""
    timing = timing or Timing()
    unknown = set(scenarios) - set(ALL_SCENARIOS)
    if unknown:
        raise ValueError(f"unknown scenarios: {sorted(unknown)}")
    scenario_rows = asyncio.run(
        _run_all(levels=levels, scenarios=scenarios, timing=timing, pool_cap=pool_cap, seed=seed)
    )
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "measures": {
            "offline": True,
            "production_benchmark": False,
            "container_resources": False,
            "note": (
                "Fixture timing with fake delayed providers and modelled DB statement delays. Connection and "
                "in-flight counts are structural; latencies are relative fixture behaviour only."
            ),
        },
        "config": {
            "levels": list(levels),
            "scenarios": list(scenarios),
            "seed": seed,
            "pool_cap": pool_cap,
            "timing_ms": asdict(timing),
            "loader_statements": LOADER_STATEMENTS,
            "persist_statements": PERSIST_STATEMENTS,
            "probe_statements": PROBE_STATEMENTS,
            "generation_chapters": GENERATION_CHAPTERS,
            "generation_concurrency": GENERATION_CONCURRENCY,
        },
        "environment": {"python": platform.python_version(), "platform": sys.platform},
        "scenarios": scenario_rows,
        "sizing_inputs": {
            name: max(row["peak_connection_demand"] for row in rows) for name, rows in scenario_rows.items()
        },
    }
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def format_table(result: dict[str, Any]) -> str:
    """A compact per-scenario table, one row per concurrency level."""
    header = (
        f"{'scenario':<10} {'N':>3} {'wall_s':>7} {'lat_p95':>8} {'ttft_p95':>9} {'prov_max':>8} "
        f"{'demand':>6} {'held':>4} {'per_unit':>8} {'queued':>6} {'wait_max':>8} {'viol':>4} {'fail':>4} "
        f"{'lag_p95':>7}"
    )
    lines = [header, "-" * len(header)]

    def value(dist: dict[str, float] | None, key: str = "p95") -> float:
        return dist[key] if dist else 0.0

    for scenario, rows in result["scenarios"].items():
        for row in rows:
            lines.append(
                f"{scenario:<10} {row['concurrency']:>3} {row['wall_s']:>7.3f} "
                f"{value(row['latency_ms']):>8.1f} {value(row['ttft_ms']):>9.1f} "
                f"{row['max_inflight_provider_calls']:>8} {row['peak_connection_demand']:>6} "
                f"{row['peak_connections_held']:>4} {row['max_connections_held_per_unit']:>8} "
                f"{row['connection_acquires_queued']:>6} {value(row['connection_wait_ms'], 'max'):>8.1f} "
                f"{row['held_across_provider_violations']:>4} {row['failed']:>4} "
                f"{value(row['event_loop_lag_ms']):>7.2f}"
            )
    return "\n".join(lines)


def _parse_csv_ints(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(",") if part.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--levels", default=",".join(map(str, DEFAULT_LEVELS)))
    parser.add_argument("--scenarios", default=",".join(ALL_SCENARIOS))
    parser.add_argument("--pool-cap", type=int, default=DEFAULT_POOL_CAP, help="0 = unlimited (demand only)")
    parser.add_argument("--provider-ms", type=float, default=Timing.provider_ms)
    parser.add_argument("--chunk-ms", type=float, default=Timing.chunk_ms)
    parser.add_argument("--statement-ms", type=float, default=Timing.statement_ms)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(tempfile.gettempdir()) / "edgar-capacity" / "capacity-result.json",
        help="JSON result path (default is under the system temp dir, never the repository)",
    )
    args = parser.parse_args(argv)
    timing = Timing(provider_ms=args.provider_ms, chunk_ms=args.chunk_ms, statement_ms=args.statement_ms)
    result = run_harness(
        levels=_parse_csv_ints(args.levels),
        scenarios=tuple(s for s in args.scenarios.split(",") if s),
        timing=timing,
        pool_cap=args.pool_cap or None,
        seed=args.seed,
        out=args.out,
    )
    print(format_table(result))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
