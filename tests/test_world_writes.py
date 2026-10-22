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
from tests.agent.test_combat import play_defeat, play_flee, play_round_limit, play_victory


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
async def test_exact_accepted_similar_scene_persists_and_drives_next_context(db_session, monkeypatch):
    from agent.scene_guidance import preload_scene_context
    from edgar_core.story import StoryEffect, StoryRef, accept_story_effect

    _, _, sess = await _setup(db_session, scene="ch1_island_approach")
    move = accept_story_effect(
        StoryEffect(kind="move", status="visited", target=StoryRef(
            collection="generated", source_sha256="a" * 64, manifest_sha256="b" * 64,
            kind="scene", canonical_id="ch2_island_approach",
        )), turn_id="entry", source_refs=[], display_name="Island landing",
    )
    await apply_adjudication(db_session, sess, AdjudicationResult(
        success=True, scene_id="ch2_island_approach", accepted_story_effects=[move],
    ))
    await db_session.commit()
    await db_session.refresh(sess)
    assert sess.current_scene_id == "ch2_island_approach"
    seen = []

    def retrieve(_manifests, scene, _flags, **_kw):
        seen.append(scene)
        return {"allowed_scene_ids": [scene], "adventure_context": [{"text": "Interior entry is available."}]}

    monkeypatch.setattr("db.vector.retrieval.retrieve_active_scene", retrieve)
    cache = await preload_scene_context({
        "source_mode": "module", "current_scene_id": sess.current_scene_id,
        "adventure_manifests": {"generated": {"reviewed_sha256": "b" * 64}},
    })
    assert seen == ["ch2_island_approach"]
    assert cache["context"]["adventure_context"][0]["text"] == "Interior entry is available."


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
        for f in (
            await db_session.execute(select(WorldFlag).where(WorldFlag.campaign_id == camp.id))
        ).scalars()
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
        select(WorldFlag).where(
            WorldFlag.campaign_id == camp.id, WorldFlag.key == "campaign_complete"
        )
    )
    assert r.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_typed_object_consume_does_not_remove_second_same_name_item(db_session):
    from edgar_core.story import StoryEffect, StoryRef, accept_story_effect

    target = StoryRef(
        collection="fixture",
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        kind="object",
        canonical_id="cord",
    )
    accepted = accept_story_effect(
        StoryEffect(kind="consume", target=target, status="consumed", quantity=1),
        turn_id="consume-turn",
        source_refs=[],
        display_name="Spare cord",
    )
    # The stable object projection already removed the consumed cord. A second same-name
    # item must survive the legacy resource update generated for that same operation.
    camp, ass, sess = await _setup(
        db_session,
        inventory={
            "items": [{"name": "Spare cord", "story_object_id": "another-object"}, "Spare cord"]
        },
    )
    ass.hp_current = 10
    adj = AdjudicationResult(
        success=True,
        accepted_story_effects=[accepted],
        character_update=CharacterUpdate(hp_delta=2, inventory_remove=["Spare cord"]),
    )
    await apply_adjudication(db_session, sess, adj)
    assert len(ass.inventory["items"]) == 2
    assert ass.hp_current == 12


@pytest.mark.asyncio
async def test_resolved_combat_outcome_cannot_regress(db_session):
    from apps.api.services.combat import persist_combat
    from db.postgres.models import CombatState

    _, _, sess = await _setup(db_session)
    resolved = CombatState(
        session_id=sess.id,
        initiative_order=["Hero"],
        round=3,
        current_turn_index=0,
        combatants=[],
        ended=True,
        outcome="victory",
    )
    db_session.add(resolved)
    await db_session.flush()
    unchanged = {"id": resolved.id, "ended": True, "outcome": "victory", "round": 3}
    await persist_combat(db_session, sess.id, unchanged)
    with pytest.raises(ValueError, match="resolved combat"):
        await persist_combat(db_session, sess.id, {**unchanged, "ended": False, "outcome": None})
    assert resolved.ended is True and resolved.outcome == "victory"


@pytest.mark.parametrize("play", [play_victory, play_defeat, play_flee, play_round_limit])
@pytest.mark.asyncio
async def test_engine_round_survives_persist_and_reload(
    db_session, fake_llm_schema, fake_retrieval, play
) -> None:
    """EDG-127: each turn's round is written and read back as the engine resolved it."""
    from apps.api.services.combat import (
        load_active_combat,
        load_resolved_combat_outcomes,
        persist_combat,
    )
    from apps.api.services.turn_runner import combat_state_public

    _, _, sess = await _setup(db_session)
    sid = sess.id
    combat_id = None
    for out in await play(fake_llm_schema):
        combat_id = await persist_combat(db_session, sid, {**out["combat_state"], "id": combat_id})
        await db_session.commit()
        db_session.expunge_all()
        resolved = out["telemetry_events"][0]["payload"]["round"]
        shown = combat_state_public(out["combat_state"])["round"]
        if out["combat_state"]["ended"]:
            (row,) = await load_resolved_combat_outcomes(db_session, sid)
            assert row["id"] == combat_id and row["round"] == shown == resolved
        else:
            loaded = await load_active_combat(db_session, sid)
            assert loaded["id"] == combat_id and loaded["round"] == shown


@pytest.mark.asyncio
async def test_combat_dispositions_persist_to_npc_rows_and_survive_reload(db_session):
    """EDG-022: captive/dead rows are session-owned, idempotent, and never touch manual NPCs."""
    from apps.api.services.combat import (
        load_resolved_combat_outcomes,
        persist_combat,
        persist_combat_npcs,
    )
    from db.postgres.models import NPC

    camp, _, sess = await _setup(db_session)
    manual = NPC(
        campaign_id=camp.id, session_id=None, name="Raider", disposition="hostile", stat_block={}
    )
    db_session.add(manual)
    await db_session.flush()
    ended = {
        "initiative_order": ["Hero", "Raider", "Raider Captain", "Cultist"],
        "round": 2,
        "current_turn_index": 0,
        "ended": True,
        "outcome": "victory",
        "combatants": [
            {"name": "Hero", "is_player": True, "alive": True, "disposition": "active"},
            {"name": "Raider", "is_player": False, "alive": True, "disposition": "captured"},
            {"name": "Raider Captain", "is_player": False, "alive": False, "disposition": "dead"},
            {"name": "Cultist", "is_player": False, "alive": False},  # legacy: cause unknown
        ],
    }
    combat_id = await persist_combat(db_session, sess.id, ended)
    for _ in range(2):  # idempotent
        await persist_combat_npcs(db_session, sess.id, camp.id, combat_id, ended)
    await db_session.commit()
    db_session.expunge_all()

    rows = {
        n.name: n
        for n in (await db_session.execute(select(NPC).where(NPC.session_id == sess.id)))
        .scalars()
        .all()
    }
    assert set(rows) == {"Raider", "Raider Captain"}  # the legacy Cultist claims neither fate
    assert rows["Raider"].disposition == "captive"
    assert rows["Raider"].stat_block["life_state"] == "living"
    assert rows["Raider Captain"].disposition == "dead"
    assert rows["Raider Captain"].stat_block["life_state"] == "dead"
    untouched = await db_session.get(NPC, manual.id)
    assert untouched.disposition == "hostile" and untouched.session_id is None

    outcome = (await load_resolved_combat_outcomes(db_session, sess.id))[0]
    by_name = {c["name"]: c for c in outcome["combatants"]}
    assert by_name["Raider"]["disposition"] == "captured"
    assert by_name["Raider Captain"]["disposition"] == "dead"
    assert "disposition" not in by_name["Cultist"]


@pytest.mark.asyncio
async def test_spell_slot_ledger_persists_and_reloads(db_session) -> None:
    """EDG-027: the expended-slot count is written to the assignment and survives a reload."""
    u = User(email="slots@example.com")
    db_session.add(u)
    await db_session.flush()
    camp = Campaign(title="Slots", system="dnd5e", created_by=u.id)
    db_session.add(camp)
    await db_session.flush()
    char = Character(
        owner_user_id=u.id,
        name="Ilse",
        character_class="Wizard",
        level=3,
        hp_max=12,
        base_stats={"conditions": []},
        base_inventory={"items": []},
    )
    db_session.add(char)
    await db_session.flush()
    ass = CharacterAssignment(
        character_id=char.id,
        campaign_id=camp.id,
        hp_current=12,
        hp_max=12,
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

    await apply_adjudication(
        db_session,
        sess,
        AdjudicationResult(
            success=True,
            character_update=CharacterUpdate(spell_slots_expended={"1": 1}),
        ),
    )
    await db_session.commit()
    await db_session.refresh(ass)
    assert ass.stats["spell_slots_expended"] == {"1": 1}
    assert ass.stats["conditions"] == [] and ass.hp_current == 12

    # A second cast spends the next slot; a long rest writes an empty ledger.
    await apply_adjudication(
        db_session,
        sess,
        AdjudicationResult(
            success=True,
            character_update=CharacterUpdate(spell_slots_expended={"1": 2, "2": 1}),
        ),
    )
    await db_session.commit()
    await db_session.refresh(ass)
    assert ass.stats["spell_slots_expended"] == {"1": 2, "2": 1}

    await apply_adjudication(
        db_session,
        sess,
        AdjudicationResult(success=True, character_update=CharacterUpdate(spell_slots_expended={})),
    )
    await db_session.commit()
    await db_session.refresh(ass)
    assert ass.stats["spell_slots_expended"] == {}
