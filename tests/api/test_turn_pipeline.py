"""Turn pipeline guarantees: owner-token lock, one transaction per turn, sanitised errors,
off-critical-path memory summary, debug gating, and session endpoint validation."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from sqlalchemy import func, select, update

from db.postgres.models import (
    Character,
    CharacterAssignment,
    EventLog,
    Session as SessionModel,
    WorldFlag,
)

SECRET = "sk-live-SECRET-token connection refused at 10.0.0.7"


def _sse_events(text: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    for frame in text.strip().split("\n\n"):
        name, data = "message", None
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        events.append((name, data))
    return events


async def _seed(api_client) -> dict[str, int]:
    seed = await api_client.post("/api/seed")
    assert seed.status_code == 200
    return seed.json()


def _fake_chat():
    """The shared fake chat model installed by `fake_llm_turn`."""
    from agent.nodes import input_parser

    return input_parser.make_chat_model()


async def _count_events(session_id: int, event_type: str) -> int:
    import db.postgres.session as sm

    async with sm.async_session_factory() as s:
        return (
            await s.execute(
                select(func.count())
                .select_from(EventLog)
                .where(EventLog.session_id == session_id, EventLog.event_type == event_type)
            )
        ).scalar_one()


async def _assignment_hp(session_id: int) -> int:
    import db.postgres.session as sm

    async with sm.async_session_factory() as s:
        sess = (
            await s.execute(select(SessionModel).where(SessionModel.id == session_id))
        ).scalar_one()
        return (
            await s.execute(
                select(CharacterAssignment.hp_current).where(
                    CharacterAssignment.character_id == sess.active_character_id,
                    CharacterAssignment.campaign_id == sess.campaign_id,
                )
            )
        ).scalar_one()


async def _disconnect_turn_stream(session_id: int, *, on_token: bool) -> bytes:
    """POST the SSE turn over raw ASGI and drop the client at one point in the stream.

    `on_token` disconnects once the first token frame is sent. Otherwise the client is already
    gone when the response starts, and the start write suspends so the disconnect can cancel
    the stream before its body has begun. Returns the bytes the client received.
    """
    from apps.api.main import app

    payload = json.dumps({"message": "I look around."}).encode()
    path = f"/api/sessions/{session_id}/turn/stream"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    disconnected = asyncio.Event()
    if not on_token:
        disconnected.set()
    body_sent = False
    received = bytearray()

    async def receive() -> dict:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            await asyncio.sleep(0)  # a real socket write can suspend here
        body = message.get("body", b"")
        received.extend(body)
        if on_token and b"event: token" in body:
            disconnected.set()

    await asyncio.wait_for(app(scope, receive, send), 20)
    return bytes(received)


# --- Turn lock -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_contention_returns_409(fake_llm_turn, fake_retrieval, api_client, redis_client) -> None:
    sid = (await _seed(api_client))["session_id"]
    await redis_client.set(f"turn:{sid}", "someone-else", ex=30)

    sync = await api_client.post(f"/api/sessions/{sid}/turn", json={"message": "I wait."})
    assert sync.status_code == 409
    stream = await api_client.post(f"/api/sessions/{sid}/turn/stream", json={"message": "I wait."})
    assert stream.status_code == 409
    # The other holder's lock is untouched.
    assert await redis_client.get(f"turn:{sid}") == "someone-else"
    assert await _count_events(sid, "narration") == 0


@pytest.mark.asyncio
async def test_stale_lock_holder_cannot_delete_or_extend_newer_lock(redis_client) -> None:
    from apps.api.dependencies import TurnLock

    stale = TurnLock(redis_client, 42, ttl=30, renew_interval=60)
    assert await stale.acquire()
    await redis_client.delete("turn:42")  # simulate the stale holder's key expiring

    fresh = TurnLock(redis_client, 42, ttl=30, renew_interval=60)
    assert await fresh.acquire()

    assert await stale.extend() is False
    await stale.release()
    assert await redis_client.get("turn:42") == fresh.token

    await fresh.release()
    assert await redis_client.get("turn:42") is None


@pytest.mark.asyncio
async def test_lock_is_renewed_while_held(redis_client) -> None:
    from apps.api.dependencies import TurnLock

    lock = TurnLock(redis_client, 7, ttl=0.3, renew_interval=0.1)
    assert await lock.acquire()
    await asyncio.sleep(0.6)  # two TTLs: the key only survives because it is being renewed
    assert await redis_client.get("turn:7") == lock.token
    await lock.release()
    assert await redis_client.get("turn:7") is None


@pytest.mark.asyncio
async def test_missing_session_turn_releases_lock(api_client, redis_client) -> None:
    r = await api_client.post("/api/sessions/9999/turn", json={"message": "hello"})
    assert r.status_code == 404
    r = await api_client.post("/api/sessions/9999/turn/stream", json={"message": "hello"})
    assert r.status_code == 404
    assert await redis_client.get("turn:9999") is None


# --- One transaction per turn --------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_writes_telemetry_and_narration_together(fake_llm_turn, fake_retrieval, api_client, db_session) -> None:
    sid = (await _seed(api_client))["session_id"]
    r = await api_client.post(f"/api/sessions/{sid}/turn", json={"message": "I look around."})
    assert r.status_code == 200
    assert await _count_events(sid, "narration") == 1
    assert await _count_events(sid, "adjudication") == 1
    rows = (await db_session.execute(select(EventLog).where(
        EventLog.session_id == sid, EventLog.event_type.in_(["adjudication", "narration"]),
    ))).scalars().all()
    assert rows[0].payload["turn_id"]
    assert rows[0].payload["turn_id"] == rows[1].payload["turn_id"]


@pytest.mark.asyncio
async def test_stream_persist_failure_emits_error_and_leaves_no_partial_writes(
    monkeypatch, fake_llm_turn, fake_retrieval, api_client, redis_client
) -> None:
    from apps.api.services import turn_runner

    async def boom(*_a, **_kw):
        raise RuntimeError(SECRET)

    # Fails after combat/world writes and event rows have been staged in the transaction.
    monkeypatch.setattr(turn_runner, "maybe_complete_campaign", boom)

    sid = (await _seed(api_client))["session_id"]
    r = await api_client.post(f"/api/sessions/{sid}/turn/stream", json={"message": "I look around."})
    assert r.status_code == 200
    events = _sse_events(r.text)
    names = [n for n, _ in events]
    # Narration was already streamed as uncommitted tokens before the commit failed.
    assert "token" in names
    assert names[-1] == "error"
    assert "done" not in names
    err = events[-1][1]
    assert err["code"] == "turn_save_failed"
    assert err["detail"] == err["message"]
    assert "SECRET" not in r.text

    assert await _count_events(sid, "narration") == 0
    assert await _count_events(sid, "adjudication") == 0
    assert await _assignment_hp(sid) == 12
    assert await redis_client.get(f"turn:{sid}") is None

    import db.postgres.session as sm

    async with sm.async_session_factory() as s:
        flag = (
            await s.execute(select(WorldFlag).where(WorldFlag.key == "goblin_ambush"))
        ).scalar_one_or_none()
        assert flag is None


@pytest.mark.asyncio
async def test_final_turn_still_returns_character(fake_llm_turn, fake_retrieval, api_client) -> None:
    from agent.models.adjudication import AdjudicationResult, FlagUpdate

    _fake_chat()._adj = AdjudicationResult(
        success=True,
        mechanical_summary="The lich falls.",
        flags_set=[FlagUpdate(key="campaign_complete", value="true")],
    )
    sid = (await _seed(api_client))["session_id"]
    first = await api_client.post(f"/api/sessions/{sid}/turn", json={"message": "I strike the lich."})
    assert first.status_code == 200
    assert first.json()["campaign_complete"] is False  # a single proposal only asks to confirm
    r = await api_client.post(f"/api/sessions/{sid}/turn", json={"message": "Yes, end the adventure."})
    assert r.status_code == 200
    body = r.json()
    assert body["campaign_complete"] is True
    assert body["character"] is not None
    assert body["character"]["name"] == "Eda"


@pytest.mark.asyncio
async def test_agent_errors_contain_no_exception_text(monkeypatch, fake_llm_turn, fake_retrieval, api_client) -> None:
    fake = _fake_chat()

    async def boom(*_a, **_kw):
        raise RuntimeError(SECRET)

    async def boom_stream(*_a, **_kw):
        raise RuntimeError(SECRET)
        yield  # pragma: no cover

    # Structured-output failures now degrade (parser -> exploration, adjudicator -> neutral);
    # An arbitrary programming fault must remain internal, even at a model-call boundary.
    monkeypatch.setattr(fake, "ainvoke", boom)
    monkeypatch.setattr(fake, "astream", boom_stream)

    sid = (await _seed(api_client))["session_id"]
    sync = await api_client.post(f"/api/sessions/{sid}/turn", json={"message": "I look around."})
    assert sync.status_code == 500
    assert sync.json()["category"] == "internal"
    assert sync.json()["outcome"] == "not_committed"
    assert "SECRET" not in sync.text

    stream = await api_client.post(f"/api/sessions/{sid}/turn/stream", json={"message": "I look around."})
    events = _sse_events(stream.text)
    assert events[-1][0] == "error"
    assert events[-1][1]["category"] == "internal"
    assert events[-1][1]["outcome"] == "not_committed"
    assert "SECRET" not in stream.text


@pytest.mark.asyncio
async def test_narrator_stream_failure_is_sanitised(monkeypatch, fake_llm_turn, fake_retrieval, api_client) -> None:
    fake = _fake_chat()

    async def exploding_astream(*_a, **_kw):
        raise RuntimeError(SECRET)
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(fake, "astream", exploding_astream)
    sid = (await _seed(api_client))["session_id"]
    r = await api_client.post(f"/api/sessions/{sid}/turn/stream", json={"message": "I look around."})
    events = _sse_events(r.text)
    assert events[-1] == ("error", events[-1][1])
    assert events[-1][1]["category"] == "internal"
    assert events[-1][1]["outcome"] == "not_committed"
    assert "SECRET" not in r.text
    assert await _count_events(sid, "narration") == 0


@pytest.mark.asyncio
async def test_stream_provider_error_after_tokens_is_an_uncommitted_error(
    monkeypatch, fake_llm_turn, fake_retrieval, api_client, redis_client
) -> None:
    from langchain_core.messages import AIMessage

    async def failing_astream(*_a, **_kw):
        yield AIMessage(content="The arrow grazes ")
        raise RuntimeError(SECRET)

    monkeypatch.setattr(_fake_chat(), "astream", failing_astream)
    sid = (await _seed(api_client))["session_id"]
    r = await api_client.post(f"/api/sessions/{sid}/turn/stream", json={"message": "I look around."})
    names = [n for n, _ in _sse_events(r.text)]
    assert "token" in names
    assert names[-1] == "error"
    assert "done" not in names
    assert "SECRET" not in r.text

    assert await _count_events(sid, "narration") == 0
    assert await _count_events(sid, "adjudication") == 0
    assert await _assignment_hp(sid) == 12
    assert await redis_client.get(f"turn:{sid}") is None


@pytest.mark.asyncio
async def test_stream_disconnect_mid_narration_releases_lock_and_commits_nothing(
    monkeypatch, fake_llm_turn, fake_retrieval, api_client, redis_client
) -> None:
    from langchain_core.messages import AIMessage

    stalled = asyncio.Event()

    async def stalled_astream(*_a, **_kw):
        yield AIMessage(content="The arrow grazes ")
        await stalled.wait()  # narration stalls until the client disconnects

    monkeypatch.setattr(_fake_chat(), "astream", stalled_astream)
    sid = (await _seed(api_client))["session_id"]
    sent = await _disconnect_turn_stream(sid, on_token=True)
    assert b"event: token" in sent
    assert b"event: done" not in sent

    assert await redis_client.get(f"turn:{sid}") is None
    assert await _count_events(sid, "narration") == 0
    assert await _count_events(sid, "adjudication") == 0
    assert await _assignment_hp(sid) == 12


@pytest.mark.asyncio
async def test_stream_disconnect_before_body_releases_lock(
    fake_llm_turn, fake_retrieval, api_client, redis_client
) -> None:
    sid = (await _seed(api_client))["session_id"]
    sent = await _disconnect_turn_stream(sid, on_token=False)
    assert b"event: token" not in sent

    assert await redis_client.get(f"turn:{sid}") is None
    assert await _count_events(sid, "narration") == 0


@pytest.mark.asyncio
async def test_missing_assignment_on_sync_turn_is_409(fake_llm_turn, fake_retrieval, api_client) -> None:
    seed = await _seed(api_client)
    import db.postgres.session as sm

    async with sm.async_session_factory() as s:
        await s.execute(
            update(CharacterAssignment)
            .where(CharacterAssignment.campaign_id == seed["campaign_id"])
            .values(ended_at=func.now())
        )
        await s.commit()

    r = await api_client.post(f"/api/sessions/{seed['session_id']}/turn", json={"message": "I rest."})
    assert r.status_code == 409
    assert r.json()["code"] == "character_not_assigned"
    assert "character_id=" not in r.text


# --- Orchestration ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_combat_stream_parses_input_once(monkeypatch, fake_llm_turn, fake_retrieval, api_client) -> None:
    from agent.models.parsed_input import ParsedInput

    fake = _fake_chat()
    fake._parsed = ParsedInput(intent="combat", entities={"target": "goblin"}, dice_expression=None)
    calls: list[type] = []
    real = fake.with_structured_output

    def counting(schema, **kw):
        calls.append(schema)
        return real(schema, **kw)

    monkeypatch.setattr(fake, "with_structured_output", counting)

    sid = (await _seed(api_client))["session_id"]
    r = await api_client.post(f"/api/sessions/{sid}/turn/stream", json={"message": "I attack the goblin!"})
    names = [n for n, _ in _sse_events(r.text)]
    assert names[-1] == "done", r.text
    assert calls.count(ParsedInput) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_unknown_creature_returns_recovery_error_without_saving_turn(
    fake_llm_turn, fake_retrieval, api_client, stream
) -> None:
    from agent.models.parsed_input import ParsedInput

    _fake_chat()._parsed = ParsedInput(intent="combat", target="Captain Vellum")
    sid = (await _seed(api_client))["session_id"]
    hp_before = await _assignment_hp(sid)
    suffix = "/stream" if stream else ""
    response = await api_client.post(
        f"/api/sessions/{sid}/turn{suffix}", json={"message": "I attack Captain Vellum"}
    )
    if stream:
        assert response.status_code == 200
        events = _sse_events(response.text)
        assert events[-1][0] == "error"
        error = events[-1][1]
        assert "done" not in [event for event, _ in events]
    else:
        assert response.status_code == 409
        error = response.json()
    assert error["code"] == "creature_identity_unresolved"
    assert "exact verified creature name" in error["message"]
    assert await _count_events(sid, "narration") == 0
    assert await _assignment_hp(sid) == hp_before


# --- Spell slots (EDG-027) -----------------------------------------------------------------


async def _assignment_stats(session_id: int) -> dict:
    import db.postgres.session as sm

    async with sm.async_session_factory() as s:
        sess = (
            await s.execute(select(SessionModel).where(SessionModel.id == session_id))
        ).scalar_one()
        return (
            await s.execute(
                select(CharacterAssignment.stats).where(
                    CharacterAssignment.character_id == sess.active_character_id,
                    CharacterAssignment.campaign_id == sess.campaign_id,
                )
            )
        ).scalar_one()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_declared_magic_missile_spends_a_slot_and_model_damage_is_ignored(
    fake_llm_turn, fake_retrieval, api_client, stream
) -> None:
    import db.postgres.session as sm
    from agent.models.adjudication import AdjudicationResult, CharacterUpdate
    from agent.models.parsed_input import ParsedInput

    sid = (await _seed(api_client))["session_id"]
    async with sm.async_session_factory() as s:
        sess = (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()
        await s.execute(
            update(Character)
            .where(Character.id == sess.active_character_id)
            .values(character_class="Wizard")
        )
        await s.commit()
    fake = _fake_chat()
    fake._parsed = ParsedInput(
        intent="exploration", entities={"spell": "Magic Missile"}, dice_expression=None
    )
    fake._adj = AdjudicationResult(
        success=True,
        mechanical_summary="A bolt of force hits the crate.",
        character_update=CharacterUpdate(hp_delta=-5),
    )
    hp_before = await _assignment_hp(sid)
    suffix = "/stream" if stream else ""
    response = await api_client.post(
        f"/api/sessions/{sid}/turn{suffix}", json={"message": "I cast Magic Missile at the crate"}
    )
    assert response.status_code == 200, response.text
    if stream:
        assert [name for name, _ in _sse_events(response.text)][-1] == "done"
    assert (await _assignment_stats(sid))["spell_slots_expended"] == {"1": 1}
    assert await _assignment_hp(sid) == hp_before


# --- Memory summary ------------------------------------------------------------------------


async def _add_narration_rows(session_id: int, n: int) -> None:
    import db.postgres.session as sm

    async with sm.async_session_factory() as s:
        for i in range(n):
            s.add(
                EventLog(
                    session_id=session_id,
                    event_type="narration",
                    payload={"player_input": f"action {i}", "narration": f"outcome {i}"},
                )
            )
        await s.commit()


@pytest.mark.asyncio
async def test_memory_summary_failure_does_not_fail_turn(
    monkeypatch, fake_llm_turn, fake_retrieval, api_client
) -> None:
    from apps.api.services import session_messages

    async def boom(*_a, **_kw):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(session_messages, "summarize_story", boom)
    sid = (await _seed(api_client))["session_id"]
    await _add_narration_rows(sid, 20)

    r = await api_client.post(f"/api/sessions/{sid}/turn", json={"message": "I look around."})
    assert r.status_code == 200
    stream = await api_client.post(f"/api/sessions/{sid}/turn/stream", json={"message": "Again."})
    assert _sse_events(stream.text)[-1][0] == "done"
    assert await _count_events(sid, "narration") == 22
    assert await _count_events(sid, "memory_summary") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("with_summary", [False, True])
async def test_turn_prompts_cover_full_uncovered_history_after_summary_or_failure(
    monkeypatch, fake_llm_turn, fake_retrieval, api_client, stream, with_summary
) -> None:
    from agent import graph
    from agent.nodes import input_parser, narrator, rules_adjudicator
    from apps.api.services import session_messages, turn_runner

    async def fail_summary(*_args):
        raise RuntimeError("deterministic summary failure")

    monkeypatch.setattr(session_messages, "summarize_story", fail_summary)
    seen: dict[str, list[str]] = {"parser": [], "planner": [], "narrator": []}
    original_parser = input_parser._parser_context
    original_planner = rules_adjudicator.build_adjudicator_prompt
    original_narrator = narrator.build_narrator_prompt

    def capture_parser(state):
        text = original_parser(state)
        seen["parser"].append(text)
        return text

    def capture_planner(state, *args, **kwargs):
        prompt = original_planner(state, *args, **kwargs)
        seen["planner"].append(prompt[1]["content"])
        return prompt

    def capture_narrator(state, *args):
        prompt = original_narrator(state, *args)
        seen["narrator"].append(prompt[-1].content)
        return prompt

    monkeypatch.setattr(input_parser, "_parser_context", capture_parser)
    monkeypatch.setattr(rules_adjudicator, "build_adjudicator_prompt", capture_planner)
    monkeypatch.setattr(narrator, "build_narrator_prompt", capture_narrator)
    monkeypatch.setattr(turn_runner, "build_narrator_prompt", capture_narrator)
    monkeypatch.setattr(turn_runner, "app", graph.build_graph())
    monkeypatch.setattr(turn_runner, "prepare_app", graph.build_graph(include_narrator=False))

    sid = (await _seed(api_client))["session_id"]
    import db.postgres.session as sm

    expected_turns = range(1, 24)
    async with sm.async_session_factory() as db, db.begin():
        rows = []
        for turn in expected_turns:
            narration = f"EDG009-N{turn:03d} exact narration"
            if turn == 23:
                narration += " " + ("LONG-TAIL-EXACT " * 240)
            rows.append(
                EventLog(
                    session_id=sid,
                    event_type="narration",
                    payload={"player_input": f"EDG009-P{turn:03d} exact action", "narration": narration},
                )
            )
        db.add_all(rows)
        await db.flush()
        if with_summary:
            db.add(
                EventLog(
                    session_id=sid,
                    event_type="memory_summary",
                    payload={
                        "summary": "EDG009-SUMMARY-COVERS-T001-THROUGH-T004",
                        "through_event_id": rows[3].id,
                    },
                )
            )

    endpoint = "/stream" if stream else ""
    response = await api_client.post(
        f"/api/sessions/{sid}/turn{endpoint}", json={"message": "I ask about the old note."}
    )
    if stream:
        assert _sse_events(response.text)[-1][0] == "done", response.text
    else:
        assert response.status_code == 200, response.text

    assert set(seen) == {"parser", "planner", "narrator"}
    for prompt_list in seen.values():
        assert len(prompt_list) == 1
        prompt_text = prompt_list[0]
        if with_summary:
            assert "EDG009-SUMMARY-COVERS-T001-THROUGH-T004" in prompt_text
            uncovered = range(5, 24)
        else:
            uncovered = expected_turns
        for turn in uncovered:
            assert f"EDG009-P{turn:03d} exact action" in prompt_text
            assert f"EDG009-N{turn:03d} exact narration" in prompt_text
        assert "LONG-TAIL-EXACT " * 240 in prompt_text


@pytest.mark.asyncio
async def test_memory_summary_is_persisted_every_n_turns_and_fed_to_narrator(
    monkeypatch, fake_llm_turn, fake_retrieval, api_client
) -> None:
    from apps.api.services import session_messages, turn_runner

    summarized: list[int] = []

    async def fake_summary(previous, messages):
        summarized.append(len(messages))
        return "Eda crossed the swamp and spared the goblin chief."

    monkeypatch.setattr(session_messages, "summarize_story", fake_summary)
    sid = (await _seed(api_client))["session_id"]

    # Fewer aged-out turns than SUMMARY_EVERY_N_TURNS: no LLM call.
    await _add_narration_rows(sid, session_messages.RECENT_TURNS + session_messages.SUMMARY_EVERY_N_TURNS - 2)
    assert (await api_client.post(f"/api/sessions/{sid}/turn", json={"message": "a"})).status_code == 200
    assert summarized == []
    # This turn ages the Nth turn out of the verbatim window: summarise exactly N turns.
    assert (await api_client.post(f"/api/sessions/{sid}/turn", json={"message": "b"})).status_code == 200
    assert summarized == [2 * session_messages.SUMMARY_EVERY_N_TURNS]  # human + AI per turn
    assert await _count_events(sid, "memory_summary") == 1

    seen: list[Any] = []
    real_prompt = turn_runner.build_narrator_prompt

    def spy(state):
        seen.append(state.get("memory_summary"))
        return real_prompt(state)

    monkeypatch.setattr(turn_runner, "build_narrator_prompt", spy)
    stream = await api_client.post(f"/api/sessions/{sid}/turn/stream", json={"message": "c"})
    assert _sse_events(stream.text)[-1][0] == "done"
    assert seen == ["Eda crossed the swamp and spared the goblin chief."]


# --- Debug gating --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debug_frame_requires_server_flag(monkeypatch, fake_llm_turn, fake_retrieval, api_client) -> None:
    sid = (await _seed(api_client))["session_id"]
    monkeypatch.delenv("EDGAR_DEBUG", raising=False)
    r = await api_client.post(f"/api/sessions/{sid}/turn/stream", json={"message": "a", "debug": True})
    assert "debug" not in [n for n, _ in _sse_events(r.text)]

    monkeypatch.setenv("EDGAR_DEBUG", "1")
    r = await api_client.post(f"/api/sessions/{sid}/turn/stream", json={"message": "b", "debug": True})
    debug = dict(_sse_events(r.text))["debug"]
    timings = debug["timings_ms"]
    assert {"parsing_ms", "retrieving_ms", "adjudicating_ms", "narrating_ms", "saving_ms", "total_ms"} <= set(timings)


# --- Session endpoint validation -----------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_for_missing_campaign_is_404(api_client) -> None:
    r = await api_client.post("/api/sessions", json={"campaign_id": 9999})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_session_enforces_character_assignment(api_client) -> None:
    seed = await _seed(api_client)
    sid = seed["session_id"]
    import db.postgres.session as sm

    async with sm.async_session_factory() as s:
        loose = Character(
            owner_user_id=seed["user_id"],
            name="Unassigned",
            character_class="Rogue",
            level=1,
            hp_max=9,
            base_stats={},
            base_inventory={},
        )
        s.add(loose)
        await s.commit()
        loose_id = loose.id
        current = (
            await s.execute(select(SessionModel.active_character_id).where(SessionModel.id == sid))
        ).scalar_one()

    bad = await api_client.patch(f"/api/sessions/{sid}", json={"active_character_id": loose_id})
    assert bad.status_code == 422
    ok = await api_client.patch(f"/api/sessions/{sid}", json={"active_character_id": current})
    assert ok.status_code == 200
    cleared = await api_client.patch(f"/api/sessions/{sid}", json={"active_character_id": None})
    assert cleared.status_code == 200


# --- Combat HUD restore --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_combat_returns_public_shape_or_null(api_client) -> None:
    import db.postgres.session as sm
    from db.postgres.models import CombatState

    sid = (await _seed(api_client))["session_id"]
    assert (await api_client.get(f"/api/sessions/{sid}/combat")).json() is None
    assert (await api_client.get("/api/sessions/999999/combat")).status_code == 404

    async with sm.async_session_factory() as s:
        s.add(
            CombatState(
                session_id=sid,
                initiative_order=["Eda", "Goblin"],
                round=2,
                current_turn_index=0,
                combatants=[
                    {"name": "Eda", "is_player": True, "hp_current": 9, "hp_max": 12, "ac": 16},
                    {"name": "Goblin", "hp_current": 3, "hp_max": 7, "ac": 15, "init": 9},
                ],
            )
        )
        await s.commit()

    body = (await api_client.get(f"/api/sessions/{sid}/combat")).json()
    assert body["round"] == 2
    assert {c["name"] for c in body["combatants"]} == {"Eda", "Goblin"}
    assert next(c for c in body["combatants"] if c["name"] == "Goblin")["ac"] == 15
    assert isinstance(body["initiative"], list)
