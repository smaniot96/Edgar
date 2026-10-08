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
async def test_turn_writes_telemetry_and_narration_together(fake_llm_turn, fake_retrieval, api_client) -> None:
    sid = (await _seed(api_client))["session_id"]
    r = await api_client.post(f"/api/sessions/{sid}/turn", json={"message": "I look around."})
    assert r.status_code == 200
    assert await _count_events(sid, "narration") == 1
    assert await _count_events(sid, "adjudication") == 1


@pytest.mark.asyncio
async def test_stream_persist_failure_emits_error_and_leaves_no_partial_writes(
    monkeypatch, fake_llm_turn, fake_retrieval, api_client
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
    assert names[-1] == "error"
    assert "done" not in names
    err = events[-1][1]
    assert err["code"] == "turn_save_failed"
    assert err["detail"] == err["message"]
    assert "SECRET" not in r.text

    assert await _count_events(sid, "narration") == 0
    assert await _count_events(sid, "adjudication") == 0
    assert await _assignment_hp(sid) == 12

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
    # the narrator is the LLM call whose failure must surface as a sanitised 503.
    monkeypatch.setattr(fake, "ainvoke", boom)
    monkeypatch.setattr(fake, "astream", boom_stream)

    sid = (await _seed(api_client))["session_id"]
    sync = await api_client.post(f"/api/sessions/{sid}/turn", json={"message": "I look around."})
    assert sync.status_code == 503
    assert sync.json()["code"] == "llm_unavailable"
    assert "SECRET" not in sync.text

    stream = await api_client.post(f"/api/sessions/{sid}/turn/stream", json={"message": "I look around."})
    events = _sse_events(stream.text)
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "llm_unavailable"
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
    assert events[-1][1]["code"] == "llm_unavailable"
    assert "SECRET" not in r.text
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
