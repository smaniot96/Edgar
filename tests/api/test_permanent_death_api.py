"""EDG-045 over the API: permanent death is recorded in the story ledger, a dead hero takes no turns,
and the explicit replacement path (add a hero, switch the session, release the dead one) lets play
continue. Each step uses existing endpoints; nothing is a dead end.

Needs Postgres (testcontainers), so these run in hosted CI. The death comes from a real turn: the
engine rolls the third failed death save with scripted dice, and the turn writer records it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import db.postgres.session as sm
from agent.models.adjudication import AdjudicationResult
from db.postgres.models import (
    CharacterAssignment,
    Session as SessionModel,
    StoryFact as StoryFactRow,
)
from tests.agent.test_spells import ScriptedDice
from tests.api.test_turn_pipeline import _fake_chat, _seed
from tools.dice import use_rng


async def _set_hero(session_id: int, *, hp_current: int, stats: dict | None = None) -> None:
    async with sm.async_session_factory() as s:
        sess = (
            await s.execute(select(SessionModel).where(SessionModel.id == session_id))
        ).scalar_one()
        ass = (
            await s.execute(
                select(CharacterAssignment).where(
                    CharacterAssignment.character_id == sess.active_character_id,
                    CharacterAssignment.campaign_id == sess.campaign_id,
                )
            )
        ).scalar_one()
        ass.hp_current = hp_current
        if stats is not None:
            ass.stats = {**(ass.stats or {}), **stats}
        await s.commit()


async def _turn(api_client, session_id: int, message: str, faces: list[int] | None = None):
    _fake_chat()._adj = AdjudicationResult(success=True, mechanical_summary="You look around.")
    if faces is None:
        return await api_client.post(f"/api/sessions/{session_id}/turn", json={"message": message})
    with use_rng(ScriptedDice(list(faces))):
        return await api_client.post(f"/api/sessions/{session_id}/turn", json={"message": message})


async def _ledger_dead_facts(session_id: int) -> list[dict]:
    async with sm.async_session_factory() as s:
        rows = (
            (await s.execute(select(StoryFactRow).where(StoryFactRow.session_id == session_id)))
            .scalars()
            .all()
        )
        return [dict(r.payload) for r in rows if r.payload.get("status") == "dead"]


@pytest.mark.asyncio
async def test_permanent_death_is_recorded_refused_and_replaced(
    fake_llm_turn, fake_retrieval, api_client
) -> None:
    seeded = await _seed(api_client)
    cid, sid = seeded["campaign_id"], seeded["session_id"]
    dead_id = (await api_client.get("/api/characters")).json()[0]["id"]
    assert (
        await api_client.patch(f"/api/campaigns/{cid}", json={"death_mode": "death_saves"})
    ).status_code == 200
    # Two failed saves already: the next failed one is the third, so the hero dies on this turn.
    await _set_hero(
        sid, hp_current=0, stats={"death_saves": {"state": "dying", "successes": 0, "failures": 2}}
    )

    done = await _turn(api_client, sid, "I look around the room", faces=[4])
    assert done.status_code == 200, done.text
    assert done.json()["character"]["hp_current"] == 0

    # The sheet records the death, and the story ledger records it once.
    async with sm.async_session_factory() as s:
        ass = (
            await s.execute(
                select(CharacterAssignment).where(
                    CharacterAssignment.character_id == dead_id,
                    CharacterAssignment.campaign_id == cid,
                )
            )
        ).scalar_one()
    assert ass.stats["death_saves"]["state"] == "dead"
    facts = await _ledger_dead_facts(sid)
    assert len(facts) == 1 and facts[0]["target"]["collection"] == "hero"

    # The roster shows the state, and the dead hero takes no further turns.
    roster = (await api_client.get(f"/api/campaigns/{cid}/characters")).json()
    assert next(r for r in roster if r["character_id"] == dead_id)["life_state"] == "dead"
    refused = await _turn(api_client, sid, "I look around the room")
    assert refused.status_code == 409
    assert refused.json()["code"] == "hero_dead"
    assert refused.json()["recovery_action"] == "assign_character"

    # Replacement: add a new hero, switch the session to them, and release the dead one.
    created = await api_client.post(
        "/api/characters",
        json={
            "name": "Replacement",
            "character_class": "Fighter",
            "level": 1,
            "hp_max": 12,
            "base_stats": {"STR": 16, "DEX": 12, "CON": 14, "INT": 10, "WIS": 12, "CHA": 8},
            "base_inventory": {"weapons": ["longsword", "shield"], "armor": "chain mail"},
        },
    )
    assert created.status_code == 201, created.text
    new_id = created.json()["id"]
    assigned = await api_client.post(
        f"/api/campaigns/{cid}/characters", json={"character_id": new_id}
    )
    assert assigned.status_code == 201, assigned.text
    switched = await api_client.patch(f"/api/sessions/{sid}", json={"active_character_id": new_id})
    assert switched.status_code == 200, switched.text
    released = await api_client.delete(f"/api/campaigns/{cid}/characters/{dead_id}")
    assert released.status_code == 204, released.text

    # Play continues with the replacement, and the death stays on record.
    resumed = await _turn(api_client, sid, "I look around the room")
    assert resumed.status_code == 200, resumed.text
    assert len(await _ledger_dead_facts(sid)) == 1
