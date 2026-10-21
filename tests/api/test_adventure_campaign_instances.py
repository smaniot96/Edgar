"""Reusable adventure sources create isolated, retry-safe campaign playthroughs."""

import asyncio
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
    before_assignments = await db_session.scalar(select(func.count()).select_from(CharacterAssignment))

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
    assert (await api_client.get(f"/api/campaigns/{old_campaign.id}")).json()["title"] == original_title
    for model in (Session, WorldFlag, CharacterAssignment):
        assert await db_session.scalar(
            select(func.count()).select_from(model).where(model.campaign_id.in_(fresh_ids))
        ) == 0
    assert (await db_session.get(EventLog, old_event.id)).payload == {"text": "Earlier outcome."}
    assert (await db_session.get(WorldFlag, old_flag.id)).value == "true"
    assert (await db_session.get(Session, seed["session_id"])).campaign_id == old_campaign.id
    assert await db_session.scalar(select(func.count()).select_from(Character)) == before_characters
    assert await db_session.scalar(select(func.count()).select_from(CharacterAssignment)) == before_assignments
    assert old_campaign.creation_request_id is None


async def test_new_campaign_replay_keeps_edited_instance_and_rejects_another_template(api_client):
    seed = (await api_client.post("/api/seed")).json()
    original = (await api_client.get(f"/api/campaigns/{seed['campaign_id']}")).json()
    slug = original["adventure_collections"][0]
    body = {"new_campaign": True, "request_id": str(uuid4())}
    created = await api_client.post(f"/api/adventures/{slug}/campaign", json=body)
    campaign_id = created.json()["id"]
    edited = await api_client.patch(f"/api/campaigns/{campaign_id}", json={"title": "My first playthrough"})
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
            return await open_campaign_for_adventure(slug, body=body, db=db, owner_id=seed["user_id"])

    first, second = await asyncio.gather(create(), create())
    assert first.id == second.id and first.id != seed["campaign_id"]
    async with sessions.async_session_factory() as db:
        assert await db.scalar(
            select(func.count()).select_from(Campaign).where(
                Campaign.created_by == seed["user_id"],
                Campaign.creation_request_id == str(body.request_id),
            )
        ) == 1


async def test_new_campaign_request_id_must_be_uuid(api_client):
    await api_client.post("/api/seed")
    response = await api_client.post(
        "/api/adventures/example/campaign",
        json={"new_campaign": True, "request_id": "invalid"},
    )
    assert response.status_code == 422
