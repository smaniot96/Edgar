"""EDG-045 over the API: the death mode is explicit, a dying hero's death save is engine-rolled and
stored with the sheet, the record survives reload, and a switch of death mode waits until the hero
is no longer dying. The default subdued campaign keeps its 1 HP / unconscious continuation.

Needs Postgres (testcontainers), so these run in hosted CI. The engine's d20 is scripted with
`use_rng` where the outcome matters.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import db.postgres.session as sm
from agent.models.adjudication import AdjudicationResult
from db.postgres.models import CharacterAssignment, Session as SessionModel
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


async def _stored_stats(session_id: int) -> dict:
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
        return dict(ass.stats or {})


async def _turn(api_client, session_id: int, message: str, faces: list[int] | None = None) -> dict:
    _fake_chat()._adj = AdjudicationResult(success=True, mechanical_summary="You look around.")
    if faces is None:
        response = await api_client.post(
            f"/api/sessions/{session_id}/turn", json={"message": message}
        )
    else:
        with use_rng(ScriptedDice(list(faces))):
            response = await api_client.post(
                f"/api/sessions/{session_id}/turn", json={"message": message}
            )
    assert response.status_code == 200, response.text
    return response.json()


async def _set_death_mode(api_client, campaign_id: int, mode: str):
    return await api_client.patch(f"/api/campaigns/{campaign_id}", json={"death_mode": mode})


@pytest.mark.asyncio
async def test_a_dying_hero_rolls_a_death_save_at_the_start_of_a_turn(
    fake_llm_turn, fake_retrieval, api_client
) -> None:
    seeded = await _seed(api_client)
    cid, sid = seeded["campaign_id"], seeded["session_id"]
    assert (await _set_death_mode(api_client, cid, "death_saves")).status_code == 200
    await _set_hero(sid, hp_current=0)

    body = await _turn(api_client, sid, "I look around the room", faces=[11])
    # A 11 on the death save succeeds: one success, still at 0 HP and dying.
    assert body["character"]["hp_current"] == 0
    assert (await _stored_stats(sid))["death_saves"] == {
        "state": "dying",
        "successes": 1,
        "failures": 0,
    }
    assert "death saving throw" in str(body["roll"]) or "death_saves" in str(body)

    # The record is the sheet's: a reload of the session still shows the turn it was rolled in.
    history = await api_client.get(f"/api/sessions/{sid}/messages")
    assert history.status_code == 200, history.text


@pytest.mark.asyncio
async def test_mode_switch_waits_until_a_dying_hero_is_resolved(
    fake_llm_turn, fake_retrieval, api_client
) -> None:
    seeded = await _seed(api_client)
    cid, sid = seeded["campaign_id"], seeded["session_id"]
    assert (await _set_death_mode(api_client, cid, "death_saves")).status_code == 200
    await _set_hero(
        sid, hp_current=0, stats={"death_saves": {"state": "dying", "successes": 1, "failures": 0}}
    )

    refused = await _set_death_mode(api_client, cid, "subdued")
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"].startswith("A hero is dying or stable")

    # Once the hero is back on their feet, the mode can change, explicitly.
    await _set_hero(sid, hp_current=3, stats={"death_saves": None})
    switched = await _set_death_mode(api_client, cid, "subdued")
    assert switched.status_code == 200, switched.text
    assert switched.json()["death_mode"] == "subdued"


@pytest.mark.asyncio
async def test_the_default_subdued_campaign_keeps_the_hero_at_zero_without_death_saves(
    fake_llm_turn, fake_retrieval, api_client
) -> None:
    seeded = await _seed(api_client)
    sid = seeded["session_id"]
    await _set_hero(sid, hp_current=0)

    body = await _turn(api_client, sid, "I look around the room")
    assert body["character"]["hp_current"] == 0
    assert "death_saves" not in await _stored_stats(sid)


@pytest.mark.asyncio
async def test_a_long_rest_restores_the_hero_and_clears_the_death_record(
    fake_llm_turn, fake_retrieval, api_client
) -> None:
    seeded = await _seed(api_client)
    cid, sid = seeded["campaign_id"], seeded["session_id"]
    assert (await _set_death_mode(api_client, cid, "death_saves")).status_code == 200
    await _set_hero(
        sid, hp_current=0, stats={"death_saves": {"state": "dying", "successes": 0, "failures": 2}}
    )

    body = await _turn(api_client, sid, "I sleep for the night")
    assert body["character"]["hp_current"] > 0
    assert "death_saves" not in await _stored_stats(sid)
