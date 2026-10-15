"""The story ledger preserves exact earned knowledge and caller-owned transactions."""

import pytest
from sqlalchemy import func, select

from apps.api.services.npcs import load_npcs
from apps.api.services.story_state import (
    StoryStateConflict,
    load_story_state,
    persist_story_effects,
    player_known_story,
    session_story_flags,
)
from db.postgres.models import NPC, Campaign, EventLog, Session, StoryFact as StoryFactRow, User
from edgar_core.story import StoryEffect, StoryRef, accept_story_effect


def test_journal_person_link_format_stays_stable():
    from apps.api.services.journal_identity import journal_entry_id

    assert journal_entry_id(42, "person", "a" * 64) == "entry_myHG5XICxbfVX_iqRPRzHA"


async def setup(db):
    user = User(email="story@example.test")
    db.add(user)
    await db.flush()
    campaign = Campaign(title="Story", system="5e", created_by=user.id)
    db.add(campaign)
    await db.flush()
    sessions = [Session(campaign_id=campaign.id), Session(campaign_id=campaign.id)]
    db.add_all(sessions)
    await db.flush()
    events = [EventLog(session_id=s.id, event_type="narration", payload={}) for s in sessions]
    db.add_all(events)
    await db.flush()
    return campaign, sessions, events


def effect(kind="discover", canonical_id="explorer_note", status="discovered", **kwargs):
    target_kind = {
        "visit": "scene",
        "objective": "objective",
        "clock": "clock",
        "promise": "promise",
    }.get(kind, "entity")
    target = StoryRef(
        collection="adventure",
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        kind=target_kind,
        canonical_id=canonical_id,
    )
    return accept_story_effect(
        StoryEffect(kind=kind, target=target, status=status, **kwargs),
        turn_id="turn-one",
        display_name="Known person" if kind == "encounter" else canonical_id,
        source_refs=[
            {
                "reference_id": "c" * 64,
                "collection": "adventure",
                "source_sha256": "a" * 64,
                "extraction_sha256": "e" * 64,
                "manifest_sha256": "b" * 64,
                "audience": "player",
                "page": 2,
                "passage_id": canonical_id,
                "private_extra": "never public",
            }
        ],
    )


async def test_reload_idempotency_exact_quotes_and_session_isolation(db_session):
    campaign, sessions, events = await setup(db_session)
    note = effect(player_text="The lens tracks reflections, not the sky.")
    moon = effect(canonical_id="moon_clue", player_text="Don’t let the lens see the moon twice.")
    npc = effect("encounter", "mara_quill", "encountered", player_text="Known person greets you.")
    location = effect("visit", "star_pool", "visited")
    facts = [note, moon, npc, location]
    state = await persist_story_effects(
        db_session, sessions[0], facts, event_id=events[0].id, expected_state={"facts": []}
    )
    await db_session.commit()
    assert await load_story_state(db_session, sessions[0].id, campaign.id) == state
    assert await load_story_state(db_session, sessions[1].id, campaign.id) == {"facts": []}
    assert (
        await persist_story_effects(db_session, sessions[0], facts, event_id=events[0].id) == state
    )
    assert await db_session.scalar(select(func.count()).select_from(StoryFactRow)) == 4
    encountered = await load_npcs(db_session, campaign.id, sessions[0].id)
    assert len(encountered) == 1 and encountered[0].name == "Known person"
    npc_id = encountered[0].id
    assert await load_npcs(db_session, campaign.id, sessions[1].id) == []
    assert (await load_npcs(db_session, campaign.id, sessions[0].id))[0].id == npc_id
    journal = player_known_story(state)
    assert [f["text"] for f in journal["facts"][:2]] == [note.player_text, moon.player_text]
    assert "private_extra" not in str(journal)
    assert "source_refs" not in str(journal)
    manifest = {
        "adventure": {
            "source_sha256": "a" * 64,
            "reviewed_sha256": "b" * 64,
            "entities": [
                {"entity_id": "explorer_note", "discovery_flag": "note_found"},
                {"entity_id": "secret", "discovery_flag": "secret_found"},
            ],
        }
    }
    legacy = {"manual": "yes", "note_found": "true", "secret_found": "true"}
    assert session_story_flags(state, manifest, legacy) == {
        "manual": "yes",
        "note_found": "discovered",
    }
    assert session_story_flags({"facts": []}, manifest, legacy) == {"manual": "yes"}


async def test_immutable_clue_and_terminal_progress_and_clock(db_session):
    _, sessions, events = await setup(db_session)
    await persist_story_effects(
        db_session, sessions[0], [effect(player_text="Exact original")], event_id=events[0].id
    )
    with pytest.raises(StoryStateConflict, match="quotation"):
        await persist_story_effects(
            db_session, sessions[0], [effect(player_text="Wrong moon quote")], event_id=events[0].id
        )
    for kind, terminal in [
        ("objective", "completed"),
        ("promise", "fulfilled"),
    ]:
        await persist_story_effects(
            db_session, sessions[0], [effect(kind, kind, terminal)], event_id=events[0].id
        )
        with pytest.raises(StoryStateConflict, match="terminal"):
            await persist_story_effects(
                db_session, sessions[0], [effect(kind, kind, "active")], event_id=events[0].id
            )
    await persist_story_effects(
        db_session,
        sessions[0],
        [effect("clock", "flood", "advanced", quantity=3)],
        event_id=events[0].id,
    )
    with pytest.raises(StoryStateConflict, match="backwards"):
        await persist_story_effects(
            db_session,
            sessions[0],
            [effect("clock", "flood", "advanced", quantity=2)],
            event_id=events[0].id,
        )


async def test_provenance_staleness_identity_and_atomic_rollback(db_session):
    campaign, sessions, events = await setup(db_session)
    await db_session.commit()
    with pytest.raises(StoryStateConflict, match="another session"):
        await persist_story_effects(db_session, sessions[0], [effect()], event_id=events[1].id)
    forged = effect().model_copy(update={"effect_id": "d" * 64})
    with pytest.raises(StoryStateConflict, match="identity"):
        await persist_story_effects(db_session, sessions[0], [forged], event_id=events[0].id)
    await persist_story_effects(db_session, sessions[0], [effect()], event_id=events[0].id)
    with pytest.raises(StoryStateConflict, match="changed"):
        await persist_story_effects(
            db_session, sessions[0], [], event_id=events[0].id, expected_state={"facts": []}
        )
    sid, cid = sessions[0].id, campaign.id
    await db_session.rollback()
    assert await load_story_state(db_session, sid) == {"facts": []}
    with pytest.raises(StoryStateConflict, match="campaign"):
        await load_story_state(db_session, sid, cid + 1)


async def test_legacy_npc_rows_stay_campaign_wide_and_new_rows_are_scoped(api_client, db_session):
    from apps.api.services.journal_identity import journal_entry_id

    campaign, sessions, events = await setup(db_session)
    legacy = NPC(
        campaign_id=campaign.id, name="Manual", disposition="friendly", stat_block={"old": True}
    )
    db_session.add(legacy)
    encounter = effect(
        "encounter",
        "odo",
        "encountered",
        player_text="Private source prose that must never appear in the people list.",
    )
    await persist_story_effects(db_session, sessions[0], [encounter], event_id=events[0].id)
    await db_session.commit()
    response = await api_client.get(f"/api/campaigns/{campaign.id}/npcs")
    assert [n["name"] for n in response.json()] == ["Manual"]
    assert response.json()[0]["session_id"] is None
    assert response.json()[0]["journal_entry_id"] is None
    response = await api_client.get(
        f"/api/campaigns/{campaign.id}/npcs?session_id={sessions[0].id}"
    )
    assert len(response.json()) == 2
    assert response.headers["x-npc-tracking"] == "available"
    npc_id = next(n["id"] for n in response.json() if n["name"] != "Manual")
    learned = next(n for n in response.json() if n["name"] != "Manual")
    assert learned["session_id"] == sessions[0].id
    assert learned["stat_block"] == {}
    assert "Private source prose" not in response.text
    assert "never public" not in response.text
    from edgar_core.story import story_target_id

    assert learned["journal_entry_id"] == journal_entry_id(
        sessions[0].id, "person", story_target_id(encounter.target)
    )
    assert (await api_client.get(f"/api/npcs/{npc_id}")).status_code == 404
    assert (
        await api_client.get(f"/api/npcs/{npc_id}?session_id={sessions[0].id}")
    ).status_code == 200
    assert (
        await api_client.get(f"/api/npcs/{npc_id}?session_id={sessions[1].id}")
    ).status_code == 404
    other = Campaign(title="Other campaign", system="5e", created_by=campaign.created_by)
    db_session.add(other)
    await db_session.commit()
    assert (
        await api_client.get(f"/api/campaigns/{other.id}/npcs?session_id={sessions[0].id}")
    ).status_code == 404


async def test_legacy_session_tracking_state_and_earned_name_refresh(api_client, db_session):
    campaign, sessions, events = await setup(db_session)
    legacy = await api_client.get(f"/api/campaigns/{campaign.id}/npcs?session_id={sessions[0].id}")
    assert legacy.headers["x-npc-tracking"] == "legacy_untracked"

    private = effect("discover", "secret", "discovered", player_text="A private ending.")
    private = accept_story_effect(
        StoryEffect.model_validate(private.model_dump(include=set(StoryEffect.model_fields))),
        turn_id="turn-one",
        display_name="secret ending",
        source_refs=[{**private.source_refs[0], "audience": "planner"}],
    )
    await persist_story_effects(db_session, sessions[0], [private], event_id=events[0].id)
    await db_session.commit()
    still_legacy = await api_client.get(
        f"/api/campaigns/{campaign.id}/npcs?session_id={sessions[0].id}"
    )
    assert still_legacy.headers["x-npc-tracking"] == "legacy_untracked"

    unnamed = effect(
        "encounter",
        "odo",
        "encountered",
        player_text="A hooded stranger greets you.",
    )
    await persist_story_effects(db_session, sessions[0], [unnamed], event_id=events[0].id)
    await db_session.commit()
    assert (
        await api_client.get(f"/api/campaigns/{campaign.id}/npcs?session_id={sessions[0].id}")
    ).json()[0]["name"] == "Encountered person"

    named = effect(
        "encounter", "odo", "encountered", player_text="Known person greets you by name."
    )
    await persist_story_effects(db_session, sessions[0], [named], event_id=events[0].id)
    await db_session.commit()
    refreshed = await api_client.get(
        f"/api/campaigns/{campaign.id}/npcs?session_id={sessions[0].id}"
    )
    assert refreshed.headers["x-npc-tracking"] == "available"
    assert [npc["name"] for npc in refreshed.json()] == ["Known person"]


async def test_selected_session_people_are_not_crowded_out_by_campaign_notes(
    api_client, db_session
):
    from apps.api.routers._common import DEFAULT_PAGE_SIZE

    campaign, sessions, events = await setup(db_session)
    db_session.add_all(
        [
            NPC(
                campaign_id=campaign.id,
                name=f"Manual note {index}",
                disposition="friendly",
                stat_block={},
            )
            for index in range(DEFAULT_PAGE_SIZE)
        ]
    )
    encounter = effect("encounter", "odo", "encountered", player_text="Known person is here.")
    await persist_story_effects(db_session, sessions[0], [encounter], event_id=events[0].id)
    await db_session.commit()

    response = await api_client.get(
        f"/api/campaigns/{campaign.id}/npcs?session_id={sessions[0].id}"
    )

    assert response.status_code == 200
    assert len(response.json()) == DEFAULT_PAGE_SIZE
    assert any(npc["session_id"] == sessions[0].id for npc in response.json())
    assert response.json()[0]["session_id"] == sessions[0].id


def test_projection_hides_unearned_name_ids_and_private_metadata():
    from edgar_core.story import StoryFact, story_target_id

    accepted = effect(
        "encounter", "duke_the_murderer", "encountered", player_text="A hooded stranger greets you."
    )
    accepted.display_name = "Duke the Murderer"
    fact = StoryFact(**accepted.model_dump(), fact_id=story_target_id(accepted.target), event_id=1)
    public = player_known_story({"facts": [fact.model_dump()]})
    assert "Duke" not in str(public) and "duke_the_murderer" not in str(public)
    assert public["facts"][0]["name"] == ""
    fact.source_refs = [{"audience": "planner", "name": "Secret ending"}]
    assert player_known_story({"facts": [fact.model_dump()]}) == {"facts": []}


def test_session_discovery_unlocks_only_its_reviewed_passage():
    from edgar_core.manifests import AdventureManifest, manifest_digest, select_scene_context
    from edgar_core.story import StoryFact, story_target_id

    manifest = AdventureManifest(
        source="adventure",
        source_sha256="a" * 64,
        extraction_sha256="e" * 64,
        opening_scene_id="room",
        scenes=[{"scene_id": "room", "title": "Room"}],
        entities=[
            {
                "entity_id": "explorer_note",
                "kind": "clue",
                "title": "Note",
                "discovery_flag": "note_found",
            }
        ],
        passages=[
            {
                "passage_id": "opening",
                "page": 1,
                "start": 0,
                "end": 10,
                "text_sha256": "c" * 64,
                "scene_ids": ["room"],
                "audience": "player",
            },
            {
                "passage_id": "earned",
                "page": 2,
                "start": 0,
                "end": 10,
                "text_sha256": "d" * 64,
                "scene_ids": ["room"],
                "audience": "player",
                "requires": {"note_found": "discovered"},
            },
        ],
        reviewed_by="test",
    )
    manifest.reviewed_sha256 = manifest_digest(manifest)
    accepted = effect(player_text="Exact note")
    accepted.target.manifest_sha256 = manifest.reviewed_sha256
    fact = StoryFact(**accepted.model_dump(), fact_id=story_target_id(accepted.target), event_id=1)
    manifests = {"adventure": manifest.model_dump()}
    earned = session_story_flags({"facts": [fact.model_dump()]}, manifests, {})
    unrelated = session_story_flags({"facts": []}, manifests, {"note_found": "discovered"})
    assert "earned" in select_scene_context(manifest, "room", earned)["player_passage_ids"]
    assert "earned" not in select_scene_context(manifest, "room", unrelated)["player_passage_ids"]


async def test_resolved_engine_victory_survives_reload_without_session_leak(db_session):
    from apps.api.services.combat import (
        load_active_combat,
        load_resolved_combat_outcomes,
        resolved_combat_outcomes_public,
    )
    from db.postgres.models import CombatState

    campaign, sessions, _ = await setup(db_session)
    campaign.source_mode = "homebrew"
    victory = CombatState(
        session_id=sessions[0].id,
        initiative_order=["Hero", "Guard"],
        round=4,
        current_turn_index=0,
        ended=True,
        outcome="victory",
        combatants=[
            {"name": "Hero", "is_player": True, "alive": True},
            {
                "name": "Guard",
                "creature_id": "actual-creature-id",
                "is_player": False,
                "alive": False,
                "hp_current": 0,
                "source_reference": {
                    "actor_id": "guard_eli",
                    "actor_manifest_sha256": "a" * 64,
                    "private_extension": "Hidden motive",
                },
            },
        ],
    )
    active = CombatState(
        session_id=sessions[0].id,
        initiative_order=["Hero"],
        round=1,
        current_turn_index=0,
        ended=False,
        combatants=[],
    )
    db_session.add_all([victory, active])
    await db_session.commit()
    sid, other_sid, victory_id = sessions[0].id, sessions[1].id, victory.id
    db_session.expunge_all()
    outcomes = await load_resolved_combat_outcomes(db_session, sid)
    assert len(outcomes) == 1
    assert outcomes[0]["id"] == victory_id and outcomes[0]["outcome"] == "victory"
    assert outcomes[0]["round"] == 4
    assert outcomes[0]["combatants"][1]["source_reference"]["actor_id"] == "guard_eli"
    assert "hp_current" not in outcomes[0]["combatants"][1]
    assert await load_resolved_combat_outcomes(db_session, other_sid) == []
    assert (await load_active_combat(db_session, sid))["ended"] is False
    public = resolved_combat_outcomes_public(outcomes)
    assert public[0]["combatants"][1]["name"] == "Guard"
    assert "source_reference" not in str(public) and "guard_eli" not in str(public)
    assert "Hidden motive" not in str(public)
    assert await db_session.scalar(select(func.count()).select_from(StoryFactRow)) == 0

    from apps.api.routers.session import _load_turn_initial_state

    initial = await _load_turn_initial_state(sid, "Remember the victory", db_session)
    assert initial["resolved_combat_outcomes"] == outcomes
    other_initial = await _load_turn_initial_state(other_sid, "Begin", db_session)
    assert other_initial["resolved_combat_outcomes"] == []
