"""EDG-026: spent Hit Dice persist on the character assignment and survive rests and reload.

The writer and the public payload are exercised without a database. The DB-backed test follows
tests/test_second_wind_persistence.py and runs only where the Postgres testcontainer is available.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agent.models.adjudication import AdjudicationResult, CharacterUpdate
from agent.resolution import ENGINE_STATS_KEYS, HIT_DICE_KEY
from apps.api.services import character_assignments as assignments, world_writes
from apps.api.services.world_writes import apply_adjudication
from db.postgres.models import (
    Campaign,
    Character,
    CharacterAssignment,
    Session as SessionModel,
    User,
)


def _assignment(stats: dict | None = None, cls: str = "Fighter", level: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
        level=level,  # EDG-045: the assignment owns the level the engine reads
        milestones_pending=0,
        hp_current=5,
        hp_max=12,
        stats={"conditions": []} if stats is None else stats,
        inventory={"items": []},
        character=SimpleNamespace(id=1, name="Eda", character_class=cls, level=level),
    )


@pytest.mark.asyncio
async def test_supported_hit_dice_ledger_persists_and_is_protected(monkeypatch) -> None:
    ass = _assignment({"conditions": [], "second_wind": {"uses_remaining": 0}})

    async def load(_db, _character_id, _campaign_id):
        return ass

    monkeypatch.setattr(world_writes, "load_active_assignment", load)
    monkeypatch.setattr(assignments, "load_active_assignment", load)

    # The engine's spent count is written; other sheet state is kept.
    await world_writes._apply_character_update(
        None, 1, 1, CharacterUpdate(hit_dice_spent=2, hp_delta=3)
    )
    assert ass.stats[HIT_DICE_KEY] == {"spent": 2}
    assert ass.stats["second_wind"] == {"uses_remaining": 0}
    assert ass.hp_current == 8

    # An update that does not touch Hit Dice leaves the ledger alone.
    await world_writes._apply_character_update(None, 1, 1, CharacterUpdate(hp_delta=1))
    assert ass.stats[HIT_DICE_KEY] == {"spent": 2}

    # The play state reads the ledger for every class: remaining, total and the die.
    payload = await assignments.character_play_state(None, 1, 1)
    assert payload["hit_dice"] == {"remaining": 1, "total": 3, "die": "d10"}

    # Manual sheet edits cannot wipe the ledger (campaign characters PATCH keeps engine keys).
    assert HIT_DICE_KEY in ENGINE_STATS_KEYS


@pytest.mark.parametrize(
    ("cls", "level", "stats", "expected"),
    [
        ("Wizard", 4, {"conditions": []}, {"remaining": 4, "total": 4, "die": "d6"}),
        ("Barbarian", 2, {"hit_dice": {"spent": 1}}, {"remaining": 1, "total": 2, "die": "d12"}),
        ("Fighter", 1, {"hit_dice": {"spent": 9}}, {"remaining": 0, "total": 1, "die": "d10"}),
    ],
)
@pytest.mark.asyncio
async def test_play_state_shows_hit_dice_for_every_class(
    monkeypatch, cls, level, stats, expected
) -> None:
    ass = _assignment(stats, cls=cls, level=level)

    async def load(_db, _character_id, _campaign_id):
        return ass

    monkeypatch.setattr(assignments, "load_active_assignment", load)
    payload = await assignments.character_play_state(None, 1, 1)
    assert payload["hit_dice"] == expected


@pytest.mark.asyncio
async def test_hit_dice_persist_across_rests_and_reload(db_session) -> None:
    import db.postgres.session as sm
    from apps.api.services.character_assignments import character_play_state

    u = User(email="hd@example.com")
    db_session.add(u)
    await db_session.flush()
    camp = Campaign(title="HD", system="dnd5e", created_by=u.id)
    db_session.add(camp)
    await db_session.flush()
    char = Character(
        owner_user_id=u.id,
        name="Eda",
        character_class="Fighter",
        level=2,
        hp_max=20,
        base_stats={"conditions": []},
        base_inventory={"items": []},
    )
    db_session.add(char)
    await db_session.flush()
    ass = CharacterAssignment(
        character_id=char.id,
        campaign_id=camp.id,
        level=2,  # EDG-045: this campaign's level, as seeding from the character sets it
        hp_current=5,
        hp_max=20,
        stats={"conditions": []},  # a legacy row: no Hit Dice key yet
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

    # A short rest spends one die; the write lands in the same transaction as the turn.
    await apply_adjudication(
        db_session,
        sess,
        AdjudicationResult(
            success=True, character_update=CharacterUpdate(hit_dice_spent=1, hp_delta=4)
        ),
    )
    await db_session.commit()

    async with sm.async_session_factory() as fresh:
        reloaded = (
            await fresh.execute(select(CharacterAssignment).where(CharacterAssignment.id == ass.id))
        ).scalar_one()
        assert reloaded.stats[HIT_DICE_KEY] == {"spent": 1}
        payload = await character_play_state(fresh, char.id, camp.id)
        assert payload["hit_dice"] == {"remaining": 1, "total": 2, "die": "d10"}

    # A long rest recovers up to half the total, rounded down (PHB p.187): both dice show again.
    await apply_adjudication(
        db_session,
        sess,
        AdjudicationResult(success=True, character_update=CharacterUpdate(hit_dice_spent=0)),
    )
    await db_session.commit()
    async with sm.async_session_factory() as fresh:
        payload = await character_play_state(fresh, char.id, camp.id)
        assert payload["hit_dice"] == {"remaining": 2, "total": 2, "die": "d10"}
