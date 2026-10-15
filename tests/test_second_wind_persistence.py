"""EDG-025: Second Wind uses persist on the character assignment and survive encounters and reload.

The writer and the public payload are exercised without a database. The DB-backed test follows
tests/test_world_writes.py and runs only where the Postgres testcontainer is available (CI).
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agent.models.adjudication import AdjudicationResult, CharacterUpdate
from apps.api.services import character_assignments as assignments, world_writes
from apps.api.services.world_writes import apply_adjudication
from db.postgres.models import (
    Campaign,
    Character,
    CharacterAssignment,
    Session as SessionModel,
    User,
)


def _assignment(stats: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        hp_current=5,
        hp_max=12,
        stats={"conditions": []} if stats is None else stats,
        inventory={"items": []},
        character=SimpleNamespace(id=1, name="Eda", character_class="Fighter", level=1),
    )


@pytest.mark.asyncio
async def test_second_wind_writes_persist_on_the_assignment_only(monkeypatch) -> None:
    ass = _assignment()

    async def load(_db, _character_id, _campaign_id):
        return ass

    monkeypatch.setattr(world_writes, "load_active_assignment", load)
    await world_writes._apply_character_update(
        None, 1, 1, CharacterUpdate(second_wind_uses_remaining=0, hp_delta=4)
    )
    assert ass.stats["second_wind"] == {"uses_remaining": 0}
    assert ass.stats["conditions"] == []  # other sheet state is kept
    assert ass.hp_current == 9

    # An update that does not touch Second Wind leaves the spent resource alone.
    await world_writes._apply_character_update(None, 1, 1, CharacterUpdate(hp_delta=1))
    assert ass.stats["second_wind"] == {"uses_remaining": 0}


@pytest.mark.asyncio
async def test_character_payload_exposes_remaining_uses_for_fighters_only(monkeypatch) -> None:
    async def load_spent(_db, _character_id, _campaign_id):
        return _assignment({"conditions": [], "second_wind": {"uses_remaining": 0}})

    async def load_legacy(_db, _character_id, _campaign_id):
        return _assignment({"conditions": []})

    monkeypatch.setattr(assignments, "load_active_assignment", load_spent)
    spent = await assignments.character_play_state(None, 1, 1)
    assert spent["second_wind"] == {"uses_remaining": 0, "uses_max": 1}

    # A legacy assignment with no key shows its full use without any row being rewritten.
    monkeypatch.setattr(assignments, "load_active_assignment", load_legacy)
    legacy = await assignments.character_play_state(None, 1, 1)
    assert legacy["second_wind"] == {"uses_remaining": 1, "uses_max": 1}
    assert legacy["stats"] == {"conditions": []}

    wizard = _assignment()
    wizard.character = SimpleNamespace(id=2, name="Ilse", character_class="Wizard", level=1)

    async def load_wizard(_db, _character_id, _campaign_id):
        return wizard

    monkeypatch.setattr(assignments, "load_active_assignment", load_wizard)
    payload = await assignments.character_play_state(None, 2, 1)
    assert "second_wind" not in payload


@pytest.mark.asyncio
async def test_second_wind_persists_across_encounters_and_reload(db_session) -> None:
    import db.postgres.session as sm
    from apps.api.services.character_assignments import character_play_state

    u = User(email="sw@example.com")
    db_session.add(u)
    await db_session.flush()
    camp = Campaign(title="SW", system="dnd5e", created_by=u.id)
    db_session.add(camp)
    await db_session.flush()
    char = Character(
        owner_user_id=u.id,
        name="Eda",
        character_class="Fighter",
        level=1,
        hp_max=12,
        base_stats={"conditions": []},
        base_inventory={"items": []},
    )
    db_session.add(char)
    await db_session.flush()
    ass = CharacterAssignment(
        character_id=char.id,
        campaign_id=camp.id,
        hp_current=5,
        hp_max=12,
        stats={"conditions": []},  # a legacy row: no Second Wind key yet
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

    # Encounter one spends the use; the write lands in the same transaction as the turn.
    await apply_adjudication(
        db_session,
        sess,
        AdjudicationResult(
            success=True, character_update=CharacterUpdate(second_wind_uses_remaining=0, hp_delta=6)
        ),
    )
    await db_session.commit()

    # Reload from a fresh session: the spent state is what a later encounter reads.
    async with sm.async_session_factory() as fresh:
        reloaded = (
            await fresh.execute(select(CharacterAssignment).where(CharacterAssignment.id == ass.id))
        ).scalar_one()
        assert reloaded.stats["second_wind"] == {"uses_remaining": 0}
        payload = await character_play_state(fresh, char.id, camp.id)
        assert payload["second_wind"] == {"uses_remaining": 0, "uses_max": 1}

    # A short rest restores it; the next reload shows the recovered use.
    await apply_adjudication(
        db_session,
        sess,
        AdjudicationResult(
            success=True, character_update=CharacterUpdate(second_wind_uses_remaining=1)
        ),
    )
    await db_session.commit()
    async with sm.async_session_factory() as fresh:
        payload = await character_play_state(fresh, char.id, camp.id)
        assert payload["second_wind"] == {"uses_remaining": 1, "uses_max": 1}
