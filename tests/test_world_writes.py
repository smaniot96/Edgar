"""apply_adjudication (plan 03)."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from apps.api.services.world_writes import apply_adjudication
from agent.models.adjudication import AdjudicationResult, CharacterUpdate, FlagUpdate
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
        started_at=datetime.now(timezone.utc),
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
