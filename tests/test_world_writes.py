"""apply_adjudication (plan 03)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from agent.models.adjudication import AdjudicationResult, CharacterUpdate, FlagUpdate
from apps.api.services.world_writes import apply_adjudication
from db.postgres.models import (
    Campaign,
    Character,
    CharacterAssignment,
    Session as SessionModel,
    User,
    WorldFlag,
)


@pytest.mark.asyncio
async def test_apply_adjudication_updates_hp_flags_scene(db_session) -> None:
    u = User(email="w@example.com")
    db_session.add(u)
    await db_session.flush()
    camp = Campaign(title="C", system="dnd5e", created_by=u.id)
    db_session.add(camp)
    await db_session.flush()
    char = Character(
        owner_user_id=u.id,
        name="Hero",
        character_class="Fighter",
        level=1,
        hp_max=20,
        base_stats={"conditions": []},
        base_inventory={"items": []},
    )
    db_session.add(char)
    await db_session.flush()
    ass = CharacterAssignment(
        character_id=char.id,
        campaign_id=camp.id,
        hp_current=20,
        hp_max=20,
        stats={"conditions": []},
        inventory={"items": []},
    )
    db_session.add(ass)
    await db_session.flush()
    sess = SessionModel(
        campaign_id=camp.id,
        active_character_id=char.id,
        current_scene_id="a",
        started_at=datetime.now(UTC),
    )
    db_session.add(sess)
    await db_session.flush()

    adj = AdjudicationResult(
        success=True,
        character_update=CharacterUpdate(hp_delta=-7),
        flags_set=[FlagUpdate(key="torch", value="lit")],
        scene_id="cave_entrance",
    )
    await apply_adjudication(db_session, sess, adj)
    await db_session.commit()

    await db_session.refresh(ass)
    await db_session.refresh(sess)
    assert ass.hp_current == 13
    assert sess.current_scene_id == "cave_entrance"

    r = await db_session.execute(
        select(WorldFlag).where(WorldFlag.campaign_id == camp.id, WorldFlag.key == "torch")
    )
    flag = r.scalar_one()
    assert flag.value == "lit"

    await db_session.refresh(char)
    assert char.hp_max == 20


async def _setup(db_session, *, inventory=None, scene="cave_entrance"):
    u = User(email="v@example.com")
    db_session.add(u)
    await db_session.flush()
    camp = Campaign(title="C", system="dnd5e", created_by=u.id)
    db_session.add(camp)
    await db_session.flush()
    char = Character(
        owner_user_id=u.id,
        name="Hero",
        character_class="Fighter",
        level=1,
        hp_max=20,
        base_stats={"conditions": []},
        base_inventory={},
    )
    db_session.add(char)
    await db_session.flush()
    ass = CharacterAssignment(
        character_id=char.id,
        campaign_id=camp.id,
        hp_current=20,
        hp_max=20,
        stats={"conditions": []},
        inventory=inventory if inventory is not None else {"items": []},
    )
    db_session.add(ass)
    sess = SessionModel(
        campaign_id=camp.id,
        active_character_id=char.id,
        current_scene_id=scene,
        started_at=datetime.now(UTC),
    )
    db_session.add(sess)
    db_session.add(WorldFlag(campaign_id=camp.id, key="door_locked", value="true"))
    await db_session.flush()
    return camp, ass, sess


@pytest.mark.asyncio
async def test_apply_adjudication_rejects_junk_scene_and_unknown_flag_clears(db_session) -> None:
    camp, _ass, sess = await _setup(db_session)
    adj = AdjudicationResult(
        success=True,
        scene_id="You are now in the throne room <b>",
        flags_set=[FlagUpdate(key="Door Unlocked", value="yes")],
        flags_cleared=["door_locked", "does_not_exist"],
    )
    await apply_adjudication(db_session, sess, adj)
    await db_session.commit()
    await db_session.refresh(sess)
    assert sess.current_scene_id == "cave_entrance"

    keys = {
        f.key: f.value
        for f in (await db_session.execute(select(WorldFlag).where(WorldFlag.campaign_id == camp.id))).scalars()
    }
    assert keys == {"door_unlocked": "yes"}


@pytest.mark.asyncio
async def test_apply_adjudication_validates_conditions_and_inventory(db_session) -> None:
    _camp, ass, sess = await _setup(
        db_session, inventory={"items": ["Potion of Healing"], "weapons": ["longsword", "dagger"]}
    )
    adj = AdjudicationResult(
        success=True,
        character_update=CharacterUpdate(
            add_conditions=["Poisoned", "doomed"],
            inventory_add=["torch", "rope", "lantern", "chalk", "<script>"],
            inventory_remove=["potion of healing", "dagger", "crown jewels"],
        ),
    )
    await apply_adjudication(db_session, sess, adj)
    await db_session.commit()
    await db_session.refresh(ass)
    assert ass.stats["conditions"] == ["poisoned"]
    assert ass.inventory["items"] == ["torch", "rope", "lantern"]
    assert ass.inventory["weapons"] == ["longsword"]


@pytest.mark.asyncio
async def test_completion_flag_only_written_when_engine_accepts(db_session) -> None:
    camp, _ass, sess = await _setup(db_session)
    adj = AdjudicationResult(
        success=True,
        flags_set=[FlagUpdate(key="campaign_complete", value="true")],
        completion_status="pending",
    )
    await apply_adjudication(db_session, sess, adj)
    await db_session.commit()
    r = await db_session.execute(
        select(WorldFlag).where(WorldFlag.campaign_id == camp.id, WorldFlag.key == "campaign_complete")
    )
    assert r.scalar_one_or_none() is None
