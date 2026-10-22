"""EDG-045 over the API: milestones are granted once per won encounter, spent by an explicit level-up,
and the level-up is atomic and idempotent across save and reload.

Needs Postgres (testcontainers), so these run in hosted CI. Dice are never assumed beyond the
engine's own bounds: the assertions read the stored record and compare it with the response.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import db.postgres.session as sm
from db.postgres.models import Campaign, CharacterAssignment, Session as SessionModel
from tests.api.test_turn_pipeline import _seed


async def _set_pending(session_id: int, pending: int, *, level: int | None = None) -> None:
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
        ass.milestones_pending = pending
        if level is not None:
            ass.level = level
        await s.commit()


async def _assignment(session_id: int) -> CharacterAssignment:
    async with sm.async_session_factory() as s:
        sess = (
            await s.execute(select(SessionModel).where(SessionModel.id == session_id))
        ).scalar_one()
        return (
            await s.execute(
                select(CharacterAssignment).where(
                    CharacterAssignment.character_id == sess.active_character_id,
                    CharacterAssignment.campaign_id == sess.campaign_id,
                )
            )
        ).scalar_one()


async def _set_mode(api_client, campaign_id: int, progression: str) -> None:
    patched = await api_client.patch(
        f"/api/campaigns/{campaign_id}", json={"progression_mode": progression}
    )
    assert patched.status_code == 200, patched.text


async def _level_up(api_client, campaign_id: int, character_id: int, body: dict):
    return await api_client.post(
        f"/api/campaigns/{campaign_id}/characters/{character_id}/level-up", json=body
    )


@pytest.mark.asyncio
async def test_level_up_is_refused_while_progression_is_fixed(api_client) -> None:
    seeded = await _seed(api_client)
    cid = seeded["campaign_id"]
    char_id = (await api_client.get("/api/characters")).json()[0]["id"]
    await _set_pending(seeded["session_id"], 1)

    refused = await _level_up(api_client, cid, char_id, {"from_level": 1, "request_id": "fixed-1"})
    assert refused.status_code == 409, refused.text
    assert "fixed" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_level_up_spends_one_milestone_and_survives_reload(api_client) -> None:
    seeded = await _seed(api_client)
    cid, sid = seeded["campaign_id"], seeded["session_id"]
    char_id = (await api_client.get("/api/characters")).json()[0]["id"]
    await _set_mode(api_client, cid, "milestone")
    await _set_pending(sid, 1)
    before = await _assignment(sid)
    hp_max_before, hp_current_before = before.hp_max, before.hp_current

    done = await _level_up(
        api_client,
        cid,
        char_id,
        {"from_level": 1, "request_id": "lvl-2", "hp_method": "average"},
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["applied"] is True
    assert (body["from_level"], body["to_level"]) == (1, 2)
    hp = body["hit_points"]
    assert hp["method"] == "average" and hp["dice"] is None
    # The gain is the class table's fixed value plus CON: the engine's number, read back here.
    assert hp["hp_max_before"] == hp_max_before
    assert hp["hp_max_after"] == hp_max_before + hp["amount"]
    assert body["character"]["level"] == 2 and body["character"]["hp_max"] == hp["hp_max_after"]
    assert body["character"]["hp_current"] == min(
        hp["hp_max_after"], hp_current_before + hp["amount"]
    )
    assert body["milestones_pending"] == 0

    # Survives reload: the database row, and the assignment view a client reads back.
    after = await _assignment(sid)
    assert after.level == 2 and after.milestones_pending == 0
    assert after.hp_max == hp["hp_max_after"]
    assert "advancement" in after.stats and after.stats["advancement"]["request_id"] == "lvl-2"
    assignments = (await api_client.get(f"/api/characters/{char_id}/assignments")).json()
    active = next(a for a in assignments if a["status"] == "active")
    assert active["hit_dice"]["total"] == 2  # one Hit Die per level: the level, not the library row

    # A retry of the same request replays the stored record and changes nothing.
    replay = await _level_up(
        api_client, cid, char_id, {"from_level": 1, "request_id": "lvl-2", "hp_method": "average"}
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["applied"] is False
    assert replay.json()["hit_points"] == hp
    again = await _assignment(sid)
    assert again.level == 2 and again.hp_max == after.hp_max

    # A new request from the old level is refused: the milestone is already spent.
    stale = await _level_up(api_client, cid, char_id, {"from_level": 1, "request_id": "lvl-2b"})
    assert stale.status_code == 409
    # Without a milestone the next level is refused too.
    none_left = await _level_up(api_client, cid, char_id, {"from_level": 2, "request_id": "lvl-3"})
    assert none_left.status_code == 409
    assert (await _assignment(sid)).level == 2


@pytest.mark.asyncio
async def test_level_up_roll_method_and_asi_at_level_four(api_client) -> None:
    seeded = await _seed(api_client)
    cid, sid = seeded["campaign_id"], seeded["session_id"]
    char_id = (await api_client.get("/api/characters")).json()[0]["id"]
    await _set_mode(api_client, cid, "milestone")
    await _set_pending(sid, 3, level=3)
    before = await _assignment(sid)

    # The Ability Score Improvement is taken at level 4, two points, and is refused without a choice.
    no_choice = await _level_up(api_client, cid, char_id, {"from_level": 3, "request_id": "a4-0"})
    assert no_choice.status_code == 409
    rolled = await _level_up(
        api_client,
        cid,
        char_id,
        {
            "from_level": 3,
            "request_id": "a4-1",
            "hp_method": "roll",
            "ability_increases": {"STR": 1, "CON": 1},
        },
    )
    assert rolled.status_code == 200, rolled.text
    body = rolled.json()
    assert body["to_level"] == 4 and body["hit_points"]["method"] == "roll"
    assert body["hit_points"]["dice"]["expression"].startswith("1d")
    assert body["ability_increases"] == {"STR": 1, "CON": 1}
    assert any(f["name"] == "Ability Score Improvement" and f["executed"] for f in body["features"])
    after = await _assignment(sid)
    assert after.level == 4
    # The +1 to STR and +1 to CON land on the sheet's own keys (the seeded sheet's spelling).
    str_key = next(k for k in after.stats if str(k).lower() in ("str", "strength"))
    con_key = next(k for k in after.stats if str(k).lower() in ("con", "constitution"))
    assert after.stats[str_key] == before.stats[str_key] + 1
    assert after.stats[con_key] == before.stats[con_key] + 1


@pytest.mark.asyncio
async def test_a_won_encounter_grants_one_milestone_once(api_client) -> None:
    """The turn writer's grant: once on the open-to-victory transition, never for a re-persist."""
    from apps.api.services.advancement import grant_victory_milestone, victory_just_ended
    from apps.api.services.combat import persist_combat

    seeded = await _seed(api_client)
    cid, sid = seeded["campaign_id"], seeded["session_id"]
    await _set_mode(api_client, cid, "milestone")
    async with sm.async_session_factory() as s:
        sess = (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()
        char_id = sess.active_character_id
        open_state = {
            "initiative_order": ["Hero", "Goblin"],
            "round": 1,
            "ended": False,
            "combatants": [],
            "outcome": None,
        }
        row_id = await persist_combat(s, sid, open_state)
        await s.commit()

    victory = {**open_state, "id": row_id, "ended": True, "outcome": "victory"}
    async with sm.async_session_factory() as s:
        assert await victory_just_ended(s, sid, victory) is True
        campaign = (await s.execute(select(Campaign).where(Campaign.id == cid))).scalar_one()
        assert await grant_victory_milestone(s, campaign, char_id) is True
        await persist_combat(s, sid, victory)
        await s.commit()

    # The row is now ended: a second persist of the same outcome is not a new victory.
    async with sm.async_session_factory() as s:
        assert await victory_just_ended(s, sid, victory) is False
    assert (await _assignment(sid)).milestones_pending == 1


@pytest.mark.asyncio
async def test_ordered_round_events_survive_combat_reload(api_client) -> None:
    from apps.api.services.combat import load_active_combat, persist_combat
    from apps.api.services.turn_runner import combat_state_public

    seeded = await _seed(api_client)
    events = [
        {"kind": "second_wind", "text": "Hero regains 5 HP.", "actor_id": "Hero"},
        {"kind": "attack", "text": "Hero attacks Goblin.", "actor_id": "Hero", "target_id": "Goblin"},
    ]
    participants = [{"id": "watcher", "name": "A watcher", "participation": "visible_nonparticipant", "reason": "Remains outside this exchange."}]
    state = {
        "initiative_order": ["Hero", "Goblin"], "round": 2, "ended": False,
        "current_turn_index": 0, "outcome": None,
        "combatants": [
            {"name": "Hero", "is_player": True, "init": 20, "round_events": events, "encounter_participants": participants},
            {"name": "Goblin", "is_player": False, "init": 5},
        ],
    }
    async with sm.async_session_factory() as s:
        await persist_combat(s, seeded["session_id"], state)
        await s.commit()
    async with sm.async_session_factory() as s:
        reloaded = await load_active_combat(s, seeded["session_id"])
    assert reloaded["round_events"] == events
    assert combat_state_public(reloaded)["round_events"] == events
    assert combat_state_public(reloaded)["participants"] == participants


@pytest.mark.asyncio
async def test_milestones_are_not_granted_past_the_cap(api_client) -> None:
    seeded = await _seed(api_client)
    cid, sid = seeded["campaign_id"], seeded["session_id"]
    await _set_mode(api_client, cid, "milestone")
    # Level 4 with none pending is one short of the cap: one more milestone can be granted.
    await _set_pending(sid, 0, level=4)
    from apps.api.services.advancement import grant_victory_milestone

    async with sm.async_session_factory() as s:
        campaign = (await s.execute(select(Campaign).where(Campaign.id == cid))).scalar_one()
        char_id = (
            await s.execute(select(SessionModel.active_character_id).where(SessionModel.id == sid))
        ).scalar_one()
        assert await grant_victory_milestone(s, campaign, char_id) is True
        await s.commit()
        # Level 4 plus one pending already reaches the cap: no further grant.
        assert await grant_victory_milestone(s, campaign, char_id) is False
        await s.commit()
    assert (await _assignment(sid)).milestones_pending == 1
