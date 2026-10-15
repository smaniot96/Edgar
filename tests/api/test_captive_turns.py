"""EDG-146 (DB-backed): a captive release or attack turn reads and writes the same state on sync and SSE.

Runs against disposable PostgreSQL in hosted CI; skipped locally without Docker.
"""

import json

import pytest
from sqlalchemy import select

from db.postgres.models import NPC, CombatState, StoryFact as StoryFactRow


def _sse_done(text: str) -> dict:
    for frame in text.strip().split("\n\n"):
        name, data = "message", None
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if name == "done":
            return data
    raise AssertionError(f"no done event in stream: {text[:300]}")


async def _seed_captive(session_id: int) -> None:
    import db.postgres.session as sm
    from apps.api.services.combat import persist_combat, persist_combat_npcs
    from apps.api.services.story_state import persist_combat_outcome_facts
    from db.postgres.models import Session as SessionModel

    ended = {
        "initiative_order": ["player", "Bandit"],
        "round": 2,
        "current_turn_index": 0,
        "ended": True,
        "outcome": "victory",
        "combatants": [
            {
                "name": "player",
                "display_name": "Eda",
                "is_player": True,
                "alive": True,
                "disposition": "active",
                "hp_current": 9,
            },
            {
                "name": "Bandit",
                "display_name": "Bandit",
                "is_player": False,
                "alive": True,
                "disposition": "captured",
                "hp_current": 0,
            },
        ],
    }
    async with sm.async_session_factory() as s:
        game_session = await s.get(SessionModel, session_id)
        combat_id = await persist_combat(s, session_id, ended)
        await persist_combat_npcs(s, session_id, game_session.campaign_id, combat_id, ended)
        from db.postgres.models import EventLog

        event = EventLog(session_id=session_id, event_type="seed", payload={})
        s.add(event)
        await s.flush()
        await persist_combat_outcome_facts(
            s, game_session, combat_id, ended, event_id=event.id, turn_id="seed-turn"
        )
        await s.commit()


async def _state(session_id: int) -> tuple[str, list[str]]:
    import db.postgres.session as sm

    async with sm.async_session_factory() as s:
        npc = await s.scalar(select(NPC).where(NPC.session_id == session_id, NPC.name == "Bandit"))
        statuses = [
            f.payload["status"]
            for f in (
                await s.scalars(
                    select(StoryFactRow)
                    .where(StoryFactRow.session_id == session_id)
                    .order_by(StoryFactRow.id)
                )
            ).all()
        ]
        open_fights = await s.scalar(
            select(CombatState.id).where(
                CombatState.session_id == session_id, CombatState.ended.is_(False)
            )
        )
        assert open_fights is None
        return npc.disposition, statuses


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_release_turn_updates_npc_and_ledger_on_sync_and_sse(
    stream, fake_llm_turn, fake_retrieval, api_client
) -> None:
    from agent.models.parsed_input import ParsedInput
    from agent.nodes import input_parser

    sid = (await api_client.post("/api/seed")).json()["session_id"]
    await _seed_captive(sid)
    input_parser.make_chat_model()._parsed = ParsedInput(intent="normal", target="Bandit")

    if stream:
        r = await api_client.post(
            f"/api/sessions/{sid}/turn/stream", json={"message": "I release the bandit"}
        )
        narration = _sse_done(r.text)["narration"]
    else:
        r = await api_client.post(
            f"/api/sessions/{sid}/turn", json={"message": "I release the bandit"}
        )
        assert r.status_code == 200, r.text
        narration = r.json()["narration"]
    assert narration.startswith("You release Bandit.")

    disposition, statuses = await _state(sid)
    assert disposition == "released"
    assert statuses == ["captive", "released"]


@pytest.mark.asyncio
async def test_attack_on_captive_is_a_ruling_with_no_fight_on_sync_turn(
    fake_llm_turn, fake_retrieval, api_client
) -> None:
    from agent.models.parsed_input import ParsedInput
    from agent.nodes import input_parser

    sid = (await api_client.post("/api/seed")).json()["session_id"]
    await _seed_captive(sid)
    input_parser.make_chat_model()._parsed = ParsedInput(
        intent="combat", check="attack", target="Bandit"
    )
    r = await api_client.post(f"/api/sessions/{sid}/turn", json={"message": "I attack the bandit"})
    assert r.status_code == 200, r.text
    assert "do you release, question, or kill them" in r.json()["narration"]
    assert r.json()["combat_state"] is None

    disposition, statuses = await _state(sid)
    assert disposition == "captive"
    assert statuses == ["captive"]
