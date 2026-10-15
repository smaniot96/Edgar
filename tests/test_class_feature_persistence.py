"""EDG-145: class feature uses persist on the character assignment, reach the sync/SSE payloads, and
survive encounters and reload.

The writer, the public views and the combat HUD payload are exercised without a database. The
DB-backed reload test follows tests/test_second_wind_persistence.py and runs only where the Postgres
testcontainer is available (CI).
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agent.class_features import ACTION_SURGE, RAGE
from agent.models.adjudication import AdjudicationResult, CharacterUpdate
from agent.resolution import CLASS_FEATURES_KEY
from apps.api.services import character_assignments as assignments, world_writes
from apps.api.services.turn_runner import combat_state_public
from apps.api.services.world_writes import apply_adjudication
from db.postgres.models import (
    Campaign,
    Character,
    CharacterAssignment,
    Session as SessionModel,
    User,
)


def _assignment(
    stats: dict | None = None, *, cls: str = "Barbarian", level: int = 1
) -> SimpleNamespace:
    return SimpleNamespace(
        hp_current=5,
        hp_max=12,
        stats={"conditions": []} if stats is None else stats,
        inventory={"items": []},
        character=SimpleNamespace(id=1, name="Brann", character_class=cls, level=level),
    )


@pytest.mark.asyncio
async def test_class_feature_writes_touch_only_the_written_features(monkeypatch) -> None:
    ass = _assignment({"conditions": [], "second_wind": {"uses_remaining": 0}})

    async def load(_db, _character_id, _campaign_id):
        return ass

    monkeypatch.setattr(world_writes, "load_active_assignment", load)
    await world_writes._apply_character_update(
        None, 1, 1, CharacterUpdate(class_feature_uses={RAGE: 0}, hp_delta=2)
    )
    assert ass.stats[CLASS_FEATURES_KEY] == {RAGE: {"uses_remaining": 0}}
    assert ass.stats["second_wind"] == {"uses_remaining": 0}  # the other resource is kept
    assert ass.stats["conditions"] == []

    # A later write for another feature keeps the rage count already on the sheet.
    await world_writes._apply_character_update(
        None, 1, 1, CharacterUpdate(class_feature_uses={ACTION_SURGE: 1})
    )
    assert ass.stats[CLASS_FEATURES_KEY] == {
        RAGE: {"uses_remaining": 0},
        ACTION_SURGE: {"uses_remaining": 1},
    }
    # An update that touches no class feature leaves the sheet's uses alone.
    await world_writes._apply_character_update(None, 1, 1, CharacterUpdate(hp_delta=1))
    assert ass.stats[CLASS_FEATURES_KEY][RAGE] == {"uses_remaining": 0}


@pytest.mark.asyncio
async def test_character_payload_exposes_class_feature_uses_additively(monkeypatch) -> None:
    async def load_barbarian(_db, _character_id, _campaign_id):
        return _assignment({"conditions": [], CLASS_FEATURES_KEY: {RAGE: {"uses_remaining": 1}}})

    async def load_legacy(_db, _character_id, _campaign_id):
        return _assignment({"conditions": []})

    async def load_wizard(_db, _character_id, _campaign_id):
        return _assignment({"conditions": []}, cls="Wizard")

    monkeypatch.setattr(assignments, "load_active_assignment", load_barbarian)
    payload = await assignments.character_play_state(None, 1, 1)
    assert payload["class_features"] == {RAGE: {"uses_remaining": 1, "uses_max": 2}}
    assert payload["stats"] == {"conditions": [], CLASS_FEATURES_KEY: {RAGE: {"uses_remaining": 1}}}

    # A legacy sheet shows its full uses, and no row is rewritten.
    monkeypatch.setattr(assignments, "load_active_assignment", load_legacy)
    legacy = await assignments.character_play_state(None, 1, 1)
    assert legacy["class_features"] == {RAGE: {"uses_remaining": 2, "uses_max": 2}}
    assert legacy["stats"] == {"conditions": []}

    monkeypatch.setattr(assignments, "load_active_assignment", load_wizard)
    wizard = await assignments.character_play_state(None, 1, 1)
    assert "class_features" not in wizard


def test_combat_hud_payload_carries_the_rage_flag() -> None:
    public = combat_state_public(
        {"round": 2, "ended": False, "combatants": [], "raging": True, "current_turn": "Brann"}
    )
    assert public["raging"] is True
    assert combat_state_public({"round": 1, "combatants": []})["raging"] is False


@pytest.mark.asyncio
async def test_class_feature_uses_persist_across_encounters_and_reload(db_session) -> None:
    import db.postgres.session as sm
    from apps.api.services.character_assignments import character_play_state

    u = User(email="rage@example.com")
    db_session.add(u)
    await db_session.flush()
    camp = Campaign(title="Rage", system="dnd5e", created_by=u.id)
    db_session.add(camp)
    await db_session.flush()
    char = Character(
        owner_user_id=u.id,
        name="Brann",
        character_class="Barbarian",
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
        stats={"conditions": []},  # a legacy row: no class feature key yet
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

    # An encounter spends a rage use; the write lands in the same transaction as the turn.
    await apply_adjudication(
        db_session,
        sess,
        AdjudicationResult(
            success=True, character_update=CharacterUpdate(class_feature_uses={RAGE: 1})
        ),
    )
    await db_session.commit()

    # Reload from a fresh session: the spent use is what a later encounter reads.
    async with sm.async_session_factory() as fresh:
        reloaded = (
            await fresh.execute(select(CharacterAssignment).where(CharacterAssignment.id == ass.id))
        ).scalar_one()
        assert reloaded.stats[CLASS_FEATURES_KEY] == {RAGE: {"uses_remaining": 1}}
        payload = await character_play_state(fresh, char.id, camp.id)
        assert payload["class_features"] == {RAGE: {"uses_remaining": 1, "uses_max": 2}}

    # A long rest restores the uses; the next reload shows the recovered count.
    await apply_adjudication(
        db_session,
        sess,
        AdjudicationResult(
            success=True, character_update=CharacterUpdate(class_feature_uses={RAGE: 2})
        ),
    )
    await db_session.commit()
    async with sm.async_session_factory() as fresh:
        payload = await character_play_state(fresh, char.id, camp.id)
        assert payload["class_features"] == {RAGE: {"uses_remaining": 2, "uses_max": 2}}
