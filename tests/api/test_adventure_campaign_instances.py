"""Reusable adventure sources create isolated, retry-safe campaign playthroughs."""

import asyncio
import hashlib
from uuid import uuid4

from sqlalchemy import func, select

from apps.api.schemas.campaign import AdventureCampaignOpen
from apps.api.services import campaign_library
from db.postgres.models import (
    Campaign,
    Character,
    CharacterAssignment,
    EventLog,
    Session,
    WorldFlag,
)


async def test_start_again_preserves_prior_play_and_character_library(api_client, db_session):
    seed = (await api_client.post("/api/seed")).json()
    old_campaign = await db_session.get(Campaign, seed["campaign_id"])
    slug = old_campaign.adventure_collections[0]
    original_title = old_campaign.title
    old_event = EventLog(
        session_id=seed["session_id"], event_type="narration", payload={"text": "Earlier outcome."}
    )
    old_flag = WorldFlag(campaign_id=old_campaign.id, key="villain_defeated", value="true")
    db_session.add_all([old_event, old_flag])
    await db_session.commit()
    campaign_library.set_preferences(slug, title="Reusable Adventure")
    before_characters = await db_session.scalar(select(func.count()).select_from(Character))
    before_assignments = await db_session.scalar(
        select(func.count()).select_from(CharacterAssignment)
    )

    continued = await api_client.post(f"/api/adventures/{slug}/campaign")
    assert continued.status_code == 200 and continued.json()["id"] == old_campaign.id
    first = await api_client.post(
        f"/api/adventures/{slug}/campaign",
        json={"new_campaign": True, "request_id": str(uuid4())},
    )
    second = await api_client.post(
        f"/api/adventures/{slug}/campaign",
        json={"new_campaign": True, "request_id": str(uuid4())},
    )
    assert first.status_code == second.status_code == 200
    fresh_ids = {first.json()["id"], second.json()["id"]}
    assert len(fresh_ids) == 2 and old_campaign.id not in fresh_ids
    assert first.json()["adventure_collections"] == second.json()["adventure_collections"] == [slug]
    assert first.json()["title"] == second.json()["title"] == "Reusable Adventure"
    assert (await api_client.get(f"/api/campaigns/{old_campaign.id}")).json()[
        "title"
    ] == original_title
    for model in (Session, WorldFlag, CharacterAssignment):
        assert (
            await db_session.scalar(
                select(func.count()).select_from(model).where(model.campaign_id.in_(fresh_ids))
            )
            == 0
        )
    assert (await db_session.get(EventLog, old_event.id)).payload == {"text": "Earlier outcome."}
    assert (await db_session.get(WorldFlag, old_flag.id)).value == "true"
    assert (await db_session.get(Session, seed["session_id"])).campaign_id == old_campaign.id
    assert await db_session.scalar(select(func.count()).select_from(Character)) == before_characters
    assert (
        await db_session.scalar(select(func.count()).select_from(CharacterAssignment))
        == before_assignments
    )
    assert old_campaign.creation_request_id is None


async def test_new_campaign_replay_keeps_edited_instance_and_rejects_another_template(api_client):
    seed = (await api_client.post("/api/seed")).json()
    original = (await api_client.get(f"/api/campaigns/{seed['campaign_id']}")).json()
    slug = original["adventure_collections"][0]
    body = {"new_campaign": True, "request_id": str(uuid4())}
    created = await api_client.post(f"/api/adventures/{slug}/campaign", json=body)
    campaign_id = created.json()["id"]
    edited = await api_client.patch(
        f"/api/campaigns/{campaign_id}", json={"title": "My first playthrough"}
    )
    assert edited.status_code == 200
    assert (await api_client.post(f"/api/campaigns/{campaign_id}/end")).status_code == 200
    replay = await api_client.post(f"/api/adventures/{slug}/campaign", json=body)
    assert replay.status_code == 200
    assert replay.json()["id"] == campaign_id
    assert replay.json()["title"] == "My first playthrough"
    assert replay.json()["status"] == "ended"
    conflict = await api_client.post("/api/adventures/other_adventure/campaign", json=body)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "validation_failed"
    changed_intent = await api_client.post(
        f"/api/adventures/{slug}/campaign", json={**body, "new_campaign": False}
    )
    assert changed_intent.status_code == 409
    assert changed_intent.json()["code"] == "validation_failed"


async def test_concurrent_start_retry_creates_one_independent_campaign(api_client):
    from apps.api.routers.campaign import open_campaign_for_adventure
    from db.postgres import session as sessions

    seed = (await api_client.post("/api/seed")).json()
    original = (await api_client.get(f"/api/campaigns/{seed['campaign_id']}")).json()
    slug = original["adventure_collections"][0]
    body = AdventureCampaignOpen(new_campaign=True, request_id=uuid4())

    async def create():
        async with sessions.async_session_factory() as db:
            return await open_campaign_for_adventure(
                slug, body=body, db=db, owner_id=seed["user_id"]
            )

    first, second = await asyncio.gather(create(), create())
    assert first.id == second.id and first.id != seed["campaign_id"]
    async with sessions.async_session_factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Campaign)
                .where(
                    Campaign.created_by == seed["user_id"],
                    Campaign.creation_request_id == str(body.request_id),
                )
            )
            == 1
        )


async def test_new_campaign_request_id_must_be_uuid(api_client):
    await api_client.post("/api/seed")
    response = await api_client.post(
        "/api/adventures/example/campaign",
        json={"new_campaign": True, "request_id": "invalid"},
    )
    assert response.status_code == 422


async def _available_party(db, owner_id):
    party = [
        Character(
            owner_user_id=owner_id,
            name=name,
            character_class="Fighter",
            level=1,
            hp_max=12,
            base_stats={"STR": 16},
            base_inventory={"items": ["Longsword"]},
        )
        for name in ("Arden", "Briar")
    ]
    db.add_all(party)
    await db.commit()
    return party


async def test_named_party_creation_is_atomic_and_replayable(api_client, db_session):
    seed = (await api_client.post("/api/seed")).json()
    original = await db_session.get(Campaign, seed["campaign_id"])
    slug = original.adventure_collections[0]
    party = await _available_party(db_session, seed["user_id"])
    body = {
        "new_campaign": True,
        "request_id": str(uuid4()),
        "title": "Friday party",
        "character_ids": [character.id for character in party],
    }
    response = await api_client.post(f"/api/adventures/{slug}/campaign", json=body)
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["title"] == "Friday party"
    assert created["adventure_collections"] == [slug]
    assert {row["name"] for row in created["characters"]} == {"Arden", "Briar"}
    assert len(created["characters"]) == 2
    replay = await api_client.post(f"/api/adventures/{slug}/campaign", json=body)
    assert replay.json() == created
    listed = (await api_client.get("/api/campaigns")).json()
    assert (
        next(row for row in listed if row["id"] == created["id"])["characters"]
        == created["characters"]
    )
    detail = (await api_client.get(f"/api/campaigns/{created['id']}")).json()
    assert detail["characters"] == created["characters"]
    changed = await api_client.post(
        f"/api/adventures/{slug}/campaign", json={**body, "character_ids": [party[0].id]}
    )
    assert changed.status_code == 409
    for character in party:
        assert character.base_stats == {"STR": 16}
        assert character.base_inventory == {"items": ["Longsword"]}
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Session).where(Session.campaign_id == created["id"])
        )
        == 0
    )


async def test_party_conflict_creates_neither_campaign_nor_partial_roster(api_client, db_session):
    seed = (await api_client.post("/api/seed")).json()
    original = await db_session.get(Campaign, seed["campaign_id"])
    party = await _available_party(db_session, seed["user_id"])
    busy = await db_session.scalar(
        select(CharacterAssignment.character_id).where(
            CharacterAssignment.campaign_id == original.id
        )
    )
    before = await db_session.scalar(select(func.count()).select_from(Campaign))
    response = await api_client.post(
        f"/api/adventures/{original.adventure_collections[0]}/campaign",
        json={
            "new_campaign": True,
            "request_id": str(uuid4()),
            "title": "Rejected party",
            "character_ids": [party[0].id, busy],
        },
    )
    assert response.status_code == 409
    assert await db_session.scalar(select(func.count()).select_from(Campaign)) == before
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(CharacterAssignment)
            .where(CharacterAssignment.character_id == party[0].id)
        )
        == 0
    )


async def test_party_selection_validation_and_missing_character(api_client, db_session):
    seed = (await api_client.post("/api/seed")).json()
    original = await db_session.get(Campaign, seed["campaign_id"])
    slug = original.adventure_collections[0]
    for ids in ([], [1, 1], [-1]):
        response = await api_client.post(
            f"/api/adventures/{slug}/campaign",
            json={
                "new_campaign": True,
                "character_ids": ids,
            },
        )
        assert response.status_code == 422
    before = await db_session.scalar(select(func.count()).select_from(Campaign))
    response = await api_client.post(
        f"/api/adventures/{slug}/campaign",
        json={
            "new_campaign": True,
            "character_ids": [999999],
        },
    )
    assert response.status_code == 404
    assert await db_session.scalar(select(func.count()).select_from(Campaign)) == before
    response = await api_client.post(
        "/api/campaigns",
        json={
            "title": "Two adventures",
            "system": "5e",
            "adventure_collections": [slug, "other"],
        },
    )
    assert response.status_code == 422
    response = await api_client.patch(
        f"/api/campaigns/{original.id}", json={"adventure_collections": [slug, "other"]}
    )
    assert response.status_code == 422


def test_legacy_campaign_creation_digest_is_unchanged():
    from apps.api.services.setup_requests import creation_identity

    request = AdventureCampaignOpen(new_campaign=True, request_id=uuid4())
    context = {"adventure_slug": "sheep"}
    expected = hashlib.sha256(
        b'{"context":{"adventure_slug":"sheep"},"request":{"new_campaign":true}}'
    ).hexdigest()
    assert creation_identity(request, context=context)["creation_request_hash"] == expected
