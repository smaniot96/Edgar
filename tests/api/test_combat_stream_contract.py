"""Combat narration streams from finalised mechanics (EDG-079).

The real graph, the real combat round and the real SSE runner run here, with a scripted fake
narrator and `persist_turn` replaced by a recorder, so these tests need no database, Redis server
or provider. Only the last test (the retry replay) needs Postgres and skips without Docker.

Timing: `test_fixture_timing_before_and_after` runs a scripted fake provider with fixed sleeps.
It shows ordering and the size of the change on this fixture. It is NOT a production latency
benchmark: real time-to-first-token depends on the provider, the network and the model.
"""

from __future__ import annotations

import asyncio
import copy
import json
import random
import time
from contextlib import aclosing, suppress
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

from agent.combat_telemetry import COMBAT_ROUND_EVENT
from agent.graph import app
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.turn_diagnostics import TURN_DIAGNOSTICS_EVENT
from apps.api.services import turn_runner
from tests.agent.test_combat import _enemy, _MaxDice, _player, _state
from tests.api.test_turn_pipeline import (
    SECRET,
    _count_events,
    _disconnect_turn_stream,
    _fake_chat,
    _seed,
    _sse_events,
)
from tools.dice import use_rng

TOKEN = "event: token"


class ScriptedNarrator:
    """A fake narrator that serves `ainvoke` and `astream` from the same scripted chunks.

    Each chunk costs `delay` seconds of provider time: `ainvoke` waits for all of them before it
    returns, while `astream` yields each chunk after its own delay. `gate` (optional) holds the
    stream open after the first chunk. `fail_at` raises a SECRET-bearing error at that chunk index.
    `closed` is True once the provider stream has been finalised; `finished` only when it completed.
    """

    def __init__(
        self,
        chunks: list[str],
        *,
        delay: float = 0.0,
        gate: asyncio.Event | None = None,
        fail_at: int | None = None,
    ) -> None:
        self.chunks = list(chunks)
        self.delay = delay
        self.gate = gate
        self.fail_at = fail_at
        self.calls: list[str] = []
        self.closed = False
        self.finished = False

    async def ainvoke(self, *_a: Any, **_kw: Any) -> AIMessage:
        self.calls.append("ainvoke")
        await asyncio.sleep(self.delay * len(self.chunks))
        return AIMessage(content="".join(self.chunks))

    async def astream(self, *_a: Any, **_kw: Any):
        self.calls.append("astream")
        try:
            for index, chunk in enumerate(self.chunks):
                if self.fail_at == index:
                    raise RuntimeError(SECRET)
                if index >= 1 and self.gate is not None:
                    await self.gate.wait()
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield AIMessage(content=chunk)
            self.finished = True
        finally:
            self.closed = True


@pytest.fixture
def narrator(monkeypatch, fake_llm_schema, fake_retrieval):
    """Install a scripted narrator as the combat round's free-text model; the parser says attack."""
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="goblin"
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)

    def install(scripted: ScriptedNarrator) -> ScriptedNarrator:
        monkeypatch.setattr(fake_llm_schema, "astream", scripted.astream)
        monkeypatch.setattr(fake_llm_schema, "ainvoke", scripted.ainvoke)
        return scripted

    return install


@pytest.fixture
def committed(monkeypatch) -> list[dict[str, Any]]:
    """`persist_turn` stand-in: records what one commit would have written. Never touches a DB."""
    rows: list[dict[str, Any]] = []

    async def record(session_id, body_message, result):
        rows.append(result)
        return {
            "event_id": len(rows),
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

    monkeypatch.setattr(turn_runner, "persist_turn", record)
    return rows


def _combat_state(*, hp: int = 40, player_init: int = 20, enemies=None, message: str = "I attack the goblin"):
    if enemies is None:
        enemies = [_enemy("Goblin", init=5)]
    state = _state([_player(init=player_init, ac=13), *enemies], message)
    state["turn_id"] = "turn-1"
    state["character"] = {**state["character"], "hp_current": hp, "hp_max": hp}
    return state


async def stream_events(state: dict[str, Any]) -> list[tuple[str, Any]]:
    frames: list[str] = []
    async with aclosing(
        turn_runner.stream_session_turn(
            session_id=1, body_message="I attack the goblin", initial_state=state, debug=False
        )
    ) as gen:
        async for frame in gen:
            frames.append(frame)
    return _sse_events("".join(frames))


def tokens_of(events: list[tuple[str, Any]]) -> str:
    return "".join(data["text"] for name, data in events if name == "token")


def done_of(events: list[tuple[str, Any]]) -> dict[str, Any]:
    assert events and events[-1][0] == "done", events[-1:]
    return events[-1][1]


def _round_payload(events: list[dict[str, Any]]) -> dict[str, Any]:
    (round_event,) = [e for e in events if e["event_type"] == COMBAT_ROUND_EVENT]
    payload = dict(round_event["payload"])
    # Wall-clock stage timings differ between runs; everything else must match exactly.
    payload.pop("timings_ms")
    payload.pop("timings_unavailable")
    return payload


@pytest.mark.asyncio
async def test_combat_stream_finalises_mechanics_before_one_streamed_narration(narrator, committed, fake_llm_schema):
    scripted = narrator(ScriptedNarrator(["The goblin ", "lunges ", "at you."]))
    with use_rng(random.Random(3)):
        events = await stream_events(_combat_state())

    names = [name for name, _ in events]

    def status_at(stage: str) -> int:
        return next(i for i, (n, d) in enumerate(events) if n == "status" and d == {"stage": stage})

    adjudicating = status_at("adjudicating")
    adjudication = names.index("adjudication")
    narrating = status_at("narrating")
    first_token = names.index("token")
    # Status feedback arrives while mechanics run, and mechanics are final before any token.
    assert adjudicating < adjudication < narrating < first_token
    assert scripted.calls == ["astream"], "exactly one narrator provider call"
    assert any(kw.get("profile") == "play.combat_narrator" for kw in fake_llm_schema.factory_kwargs)
    done = done_of(events)
    assert done["narration"] == tokens_of(events) == "The goblin lunges at you."
    assert len(committed) == 1


@pytest.mark.asyncio
async def test_declared_unsupported_mechanic_is_bounded_before_any_token(
    narrator, committed, monkeypatch
):
    """Like the normal path, text for a declared unsupported mechanic is withheld until bounded."""
    scripted = narrator(ScriptedNarrator(["The goblin ", "lunges ", "at you."]))
    monkeypatch.setattr(turn_runner, "actions_for_state", lambda _state: ("declared",))
    with use_rng(random.Random(3)):
        events = await stream_events(_combat_state())

    assert scripted.calls == ["ainvoke"], "one provider call, not streamed to the player raw"
    done = done_of(events)
    assert tokens_of(events) == done["narration"]
    assert len(committed) == 1


@pytest.mark.asyncio
async def test_sync_and_stream_finalise_the_same_round(narrator, committed):
    text = ["The goblin ", "lunges at you."]
    # A foe that survives the round keeps the scripted text unchanged, so both paths are compared
    # on an ordinary round (no outcome suffix, no roster fallback).
    sync_model = narrator(ScriptedNarrator(text))
    with use_rng(random.Random(11)):
        sync = await app.ainvoke(copy.deepcopy(_combat_state(enemies=[_enemy("Goblin", hp=500, init=5)])))
    stream_model = narrator(ScriptedNarrator(text))
    with use_rng(random.Random(11)):
        events = await stream_events(_combat_state(enemies=[_enemy("Goblin", hp=500, init=5)]))
    streamed = committed[-1]

    assert sync_model.calls == ["ainvoke"] and stream_model.calls == ["astream"]
    assert done_of(events)["narration"] == sync["narration"] == streamed["narration"]
    assert sync["narration"] == "The goblin lunges at you."
    assert sync["combat_state"] == streamed["combat_state"]
    assert _round_payload(sync["telemetry_events"]) == _round_payload(streamed["telemetry_events"])
    # The history keeps the bounded narration (no outcome suffix), on both paths.
    assert [m.content for m in sync["messages"]] == [m.content for m in streamed["messages"]]
    assert sync["messages"][-1].content == sync["narration"]


@pytest.mark.asyncio
async def test_roster_violation_makes_done_authoritative_over_tokens(narrator, committed):
    enemies = [_enemy("Goblin", init=5), _enemy("Bugbear", init=3, alive=False)]
    narrator(ScriptedNarrator(["The Bugbear rises ", "and strikes you!"]))
    with use_rng(random.Random(4)):
        events = await stream_events(_combat_state(enemies=enemies))

    adjudication = next(d for n, d in events if n == "adjudication")
    done = done_of(events)
    assert tokens_of(events) == "The Bugbear rises and strikes you!"
    # The engine's own summary replaces the revived foe's prose; the client must show `done`.
    assert done["narration"] == adjudication["mechanical_summary"] != tokens_of(events)
    assert "rises" not in done["narration"]
    assert committed[-1]["messages"][-1].content == done["narration"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "state_kwargs", "dice", "text", "suffix", "outcome"),
    [
        (
            "victory",
            {"enemies": [_enemy("Goblin", hp=1, init=5)]},
            _MaxDice(),
            ["You strike the goblin down."],
            "**Victory!** The way forward is clear.",
            "victory",
        ),
        (
            "defeat",
            {"hp": 1, "enemies": [_enemy("Goblin", hp=500, init=5)]},
            _MaxDice(),
            ["The goblin strikes you down."],
            "**Subdued.**",
            "defeat",
        ),
        (
            "fled",
            {"message": "I flee the fight"},
            None,
            ["You break away."],
            "*(You have left the fight.)*",
            "fled",
        ),
    ],
)
async def test_outcome_suffix_reaches_done_and_not_the_provisional_tokens(
    narrator, committed, label, state_kwargs, dice, text, suffix, outcome
):
    narrator(ScriptedNarrator(text))
    state = _combat_state(**state_kwargs)
    if dice is None:
        events = await stream_events(state)
    else:
        with use_rng(dice):
            events = await stream_events(state)
    done = done_of(events)
    assert suffix in done["narration"], label
    assert suffix not in tokens_of(events)
    assert committed[-1]["combat_state"]["outcome"] == outcome


@pytest.mark.asyncio
async def test_provider_failure_before_first_token_is_redacted_and_commits_nothing(narrator, committed):
    scripted = narrator(ScriptedNarrator(["never"], fail_at=0))
    with use_rng(random.Random(5)):
        events = await stream_events(_combat_state())

    assert [n for n, _ in events][-1] == "error"
    error = events[-1][1]
    assert error["category"] == "internal"
    assert error["outcome"] == "not_committed"
    assert SECRET not in json.dumps(events)
    assert all(n != "done" for n, _ in events)
    assert scripted.calls == ["astream"] and scripted.closed
    assert committed == []


@pytest.mark.asyncio
async def test_provider_failure_mid_stream_keeps_tokens_provisional_and_uncommitted(narrator, committed):
    scripted = narrator(ScriptedNarrator(["The goblin ", "lunges."], fail_at=1))
    with use_rng(random.Random(5)):
        events = await stream_events(_combat_state())

    names = [n for n, _ in events]
    assert "token" in names and names[-1] == "error"
    assert "done" not in names
    assert SECRET not in json.dumps(events)
    assert scripted.closed
    assert committed == []


@pytest.mark.asyncio
async def test_client_disconnect_closes_provider_and_commits_nothing(narrator, committed):
    gate = asyncio.Event()  # never set: the provider stalls after its first chunk
    scripted = narrator(ScriptedNarrator(["The goblin ", "lunges."], gate=gate))
    frames = turn_runner.stream_session_turn(
        session_id=1, body_message="I attack the goblin", initial_state=_combat_state(), debug=False
    )
    with use_rng(random.Random(5)):
        async for frame in frames:
            if frame.startswith(TOKEN):
                break
        await frames.aclose()  # the client went away while the provider was still streaming

    assert scripted.calls == ["astream"]
    assert scripted.closed and not scripted.finished
    assert committed == []


@pytest.mark.asyncio
async def test_cancelled_stream_closes_provider_and_leaks_no_task(narrator, committed):
    gate = asyncio.Event()
    scripted = narrator(ScriptedNarrator(["The goblin ", "lunges."], gate=gate))
    seen_token = asyncio.Event()

    async def consume() -> None:
        async with aclosing(
            turn_runner.stream_session_turn(
                session_id=1, body_message="I attack the goblin", initial_state=_combat_state(), debug=False
            )
        ) as gen:
            async for frame in gen:
                if frame.startswith(TOKEN):
                    seen_token.set()

    before = len(asyncio.all_tasks())
    with use_rng(random.Random(5)):
        task = asyncio.create_task(consume())
        await asyncio.wait_for(seen_token.wait(), 5)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await asyncio.sleep(0)

    assert scripted.closed and not scripted.finished
    assert committed == []
    assert len(asyncio.all_tasks()) <= before


@pytest.mark.asyncio
async def test_engine_only_ruling_streams_without_a_provider_call(narrator, committed, fake_llm_schema):
    scripted = narrator(ScriptedNarrator(["never used"]))
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="combat", check="attack", target="dragon")
    state = _combat_state(enemies=[_enemy("Goblin"), _enemy("Wolf")], message="I attack the dragon")
    events = await stream_events(state)

    assert scripted.calls == []
    assert "Who do you attack?" in done_of(events)["narration"]
    assert tokens_of(events) == done_of(events)["narration"]


@pytest.mark.asyncio
async def test_chunk_boundaries_are_forwarded_and_empty_chunks_skipped(narrator, committed):
    narrator(ScriptedNarrator(["Ta", "", "vern — ", "é", "ok."]))
    with use_rng(random.Random(6)):
        events = await stream_events(_combat_state())

    assert [d["text"] for n, d in events if n == "token"] == ["Ta", "vern — ", "é", "ok."]
    assert done_of(events)["narration"] == "Tavern — éok."


@pytest.mark.asyncio
async def test_diagnostics_row_records_first_text_and_the_narrating_stage(narrator, committed):
    narrator(ScriptedNarrator(["The goblin ", "lunges."]))
    with use_rng(random.Random(7)):
        await stream_events(_combat_state())

    (row,) = [e for e in committed[-1]["telemetry_events"] if e["event_type"] == TURN_DIAGNOSTICS_EVENT]
    payload = row["payload"]
    assert payload["path"] == "combat" and payload["delivery"] == "stream"
    assert payload["turn_id"] == "turn-1"
    assert isinstance(payload["time_to_first_text_ms"], int)
    assert "narrating_ms" in payload["stages_ms"]
    # Combat setup also makes a traced builder call when a new encounter starts; the narrator
    # call is the one streamed narrate call.
    (call,) = [c for c in payload["provider_calls"] if c["kind"] == "narrate"]
    assert call["streamed"] is True
    assert call["profile"] == "play.combat_narrator"
    assert isinstance(call["first_text_ms"], int)
    # Fake usage is absent, so token counts are unknown, never zero.
    assert call["usage"] == {"input_tokens": None, "output_tokens": None, "total_tokens": None}


@pytest.mark.asyncio
async def test_fixture_timing_before_and_after(narrator, committed):
    """Old pseudo-stream (wait for the whole narration, then send it) vs the streamed path.

    Fixture timing: each fake provider chunk costs DELAY seconds. Not a production benchmark.
    """
    delay = 0.2
    chunks = ["The goblin ", "lunges ", "at you."]

    survivor = [_enemy("Goblin", hp=500, init=5)]
    narrator(ScriptedNarrator(chunks, delay=delay))
    started = time.monotonic()
    with use_rng(random.Random(8)):
        sync = await app.ainvoke(_combat_state(enemies=survivor))
    old_first_text = time.monotonic() - started  # the sync path has nothing before the whole text
    assert sync["narration"] == "".join(chunks)

    scripted = narrator(ScriptedNarrator(chunks, delay=delay))
    started = time.monotonic()
    first_text = None
    finished_at_first_text = None
    with use_rng(random.Random(8)):
        async with aclosing(
            turn_runner.stream_session_turn(
                session_id=1,
                body_message="I attack the goblin",
                initial_state=_combat_state(enemies=survivor),
                debug=False,
            )
        ) as gen:
            async for frame in gen:
                if frame.startswith(TOKEN) and first_text is None:
                    first_text = time.monotonic() - started
                    finished_at_first_text = scripted.finished
    total = time.monotonic() - started

    print(f"\nfixture timing: old first text {old_first_text:.3f}s, new first text {first_text:.3f}s, "
          f"new total {total:.3f}s (fake provider, {delay}s per chunk)")
    assert finished_at_first_text is False, "the first token arrived before the provider finished"
    assert first_text < old_first_text / 2
    assert total > first_text


@pytest.mark.asyncio
async def test_stream_route_releases_turn_lock_on_completion_and_disconnect(
    monkeypatch, narrator, committed
):
    """The route's lock lifecycle with no database: overrides supply Redis and the initial state."""
    from fakeredis import FakeAsyncRedis

    from apps.api.dependencies import get_redis, get_session
    from apps.api.main import app as api_app
    from apps.api.routers import session as session_router

    redis = FakeAsyncRedis(decode_responses=True)

    async def no_db():
        yield None

    async def fake_redis():
        yield redis

    async def initial(session_id, message, db):
        return {**_combat_state(), "campaign_id": 1}

    async def no_summary(*_a, **_kw):
        return None

    monkeypatch.setattr(session_router, "_load_turn_initial_state", initial)
    monkeypatch.setattr(session_router, "maybe_update_memory_summary", no_summary)
    api_app.dependency_overrides[get_session] = no_db
    api_app.dependency_overrides[get_redis] = fake_redis
    try:
        scripted = narrator(ScriptedNarrator(["The goblin ", "lunges."]))
        with use_rng(random.Random(9)):
            async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
                done_response = await client.post(
                    "/api/sessions/1/turn/stream", json={"message": "I attack the goblin"}
                )
            assert "event: done" in done_response.text
            assert await redis.get("turn:1") is None

            scripted.gate = asyncio.Event()  # stall the second chunk, then disconnect
            sent = await _disconnect_turn_stream(1, on_token=True)
        assert b"event: token" in sent and b"event: done" not in sent
        assert await redis.get("turn:1") is None
        assert scripted.closed
        assert len(committed) == 1  # only the completed turn committed
    finally:
        api_app.dependency_overrides.clear()
        await redis.aclose()


@pytest.mark.asyncio
async def test_stream_route_releases_turn_lock_after_provider_failure(monkeypatch, narrator, committed):
    """A provider failure ends the stream with an `error` frame and frees the turn lock."""
    from fakeredis import FakeAsyncRedis

    from apps.api.dependencies import get_redis, get_session
    from apps.api.main import app as api_app
    from apps.api.routers import session as session_router

    redis = FakeAsyncRedis(decode_responses=True)

    async def no_db():
        yield None

    async def fake_redis():
        yield redis

    async def initial(session_id, message, db):
        return {**_combat_state(), "campaign_id": 1}

    async def no_summary(*_a, **_kw):
        return None

    monkeypatch.setattr(session_router, "_load_turn_initial_state", initial)
    monkeypatch.setattr(session_router, "maybe_update_memory_summary", no_summary)
    api_app.dependency_overrides[get_session] = no_db
    api_app.dependency_overrides[get_redis] = fake_redis
    try:
        narrator(ScriptedNarrator(["The goblin ", "lunges."], fail_at=1))
        with use_rng(random.Random(9)):
            async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
                response = await client.post("/api/sessions/1/turn/stream", json={"message": "I attack the goblin"})
        names = [n for n, _ in _sse_events(response.text)]
        assert names[-1] == "error" and "done" not in names
        assert SECRET not in response.text
        assert await redis.get("turn:1") is None
        assert committed == []
    finally:
        api_app.dependency_overrides.clear()
        await redis.aclose()


@pytest.mark.asyncio
async def test_combat_stream_retry_replays_receipt_without_second_narration(
    fake_llm_turn, fake_retrieval, api_client, monkeypatch
) -> None:
    """Needs Postgres (skips without Docker): a lost-ack retry returns the committed round."""
    fake = _fake_chat()
    fake._parsed = ParsedInput(intent="combat", entities={"target": "goblin"}, dice_expression=None)
    scripted = ScriptedNarrator(["The goblin ", "lunges."])
    monkeypatch.setattr(fake, "astream", scripted.astream)
    sid = (await _seed(api_client))["session_id"]
    body = {"message": "I attack the goblin!", "request_id": "retry-combat-1"}

    first = await api_client.post(f"/api/sessions/{sid}/turn/stream", json=body)
    assert first.text.count("event: done") == 1
    replay = await api_client.post(f"/api/sessions/{sid}/turn/stream", json=body)
    assert replay.text.count("event: done") == 1
    assert scripted.calls == ["astream"]
    assert await _count_events(sid, "narration") == 1
    assert await _count_events(sid, TURN_DIAGNOSTICS_EVENT) == 1


def _round_payload_or_none(result: dict[str, Any]) -> None:
    """A refused proposal runs no combat round, so it emits no round telemetry."""
    assert not [e for e in result.get("telemetry_events") or [] if e.get("event_type") == COMBAT_ROUND_EVENT]
    return None


@pytest.mark.asyncio
async def test_sync_and_stream_ask_the_same_question_for_an_unsupported_proposal(
    narrator, committed, fake_llm_schema
):
    """EDG-182: a proposed action the player did not declare is one question, identical on both paths.

    Neither path rolls a die, spends the round or calls the narrator.
    """
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="goblin", actions=[{"kind": "dodge", "text": "dodge"}]
    )
    enemies = [_enemy("Goblin", hp=500, init=5)]
    sync_model = narrator(ScriptedNarrator(["unused"]))
    with use_rng(random.Random(11)):
        sync = await app.ainvoke(copy.deepcopy(_combat_state(enemies=enemies)))
    stream_model = narrator(ScriptedNarrator(["unused"]))
    with use_rng(random.Random(11)):
        events = await stream_events(_combat_state(enemies=[_enemy("Goblin", hp=500, init=5)]))
    streamed = committed[-1]

    assert sync_model.calls == [] and stream_model.calls == []
    assert "No roll is made" in sync["narration"]
    assert done_of(events)["narration"] == sync["narration"] == streamed["narration"]
    assert sync["adjudication_result"].dice_log == []
    assert sync["combat_state"]["round"] == 1
    assert streamed["combat_state"]["round"] == 1
    assert _round_payload_or_none(sync) is None and _round_payload_or_none(streamed) is None


@pytest.mark.asyncio
async def test_sync_and_stream_hold_a_multi_part_turn_for_confirmation_identically(
    narrator, committed, fake_llm_schema
):
    """EDG-182: a multi-part typed turn is repeated back on both paths, before any die or narrator call."""
    proposal = ParsedInput.model_validate(
        {
            "intent": "combat",
            "check": "attack",
            "target": "goblin",
            "actions": [
                {"kind": "move", "text": "close in on the goblin"},
                {"kind": "attack", "text": "attack it", "target": "goblin"},
            ],
        }
    )
    fake_llm_schema.responses[ParsedInput] = proposal
    text = "I close in on the goblin and attack it"
    sync_model = narrator(ScriptedNarrator(["unused"]))
    sync_state = _combat_state(enemies=[_enemy("Goblin", hp=500, init=5)], message=text)
    with use_rng(random.Random(11)):
        sync = await app.ainvoke(copy.deepcopy(sync_state))
    stream_model = narrator(ScriptedNarrator(["unused"]))
    with use_rng(random.Random(11)):
        events = await stream_events(_combat_state(enemies=[_enemy("Goblin", hp=500, init=5)], message=text))
    streamed = committed[-1]

    assert sync_model.calls == [] and stream_model.calls == []
    assert "Confirm before any dice" in sync["narration"]
    assert done_of(events)["narration"] == sync["narration"] == streamed["narration"]
    assert sync["adjudication_result"].dice_log == []
    assert sync["combat_state"]["round"] == 1
    assert turn_runner.combat_state_public(streamed["combat_state"])["pending_declaration"] == {
        "summary": sync["narration"]
    }
