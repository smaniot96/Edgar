"""Campaign lifecycle: active vs ended (plan 02)."""

import asyncio
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import select

import db.postgres.session as db_session_module
from agent.models.adjudication import AdjudicationResult
from apps.api.routers.campaign import reopen_campaign
from apps.api.routers.campaign_characters import (
    assign_character_to_campaign,
    release_character_from_campaign,
)
from apps.api.schemas.character import CampaignCharacterAssignBody
from apps.api.services.campaign_completion import close_campaign, maybe_complete_campaign
from db.postgres.models import Campaign, Character, CharacterAssignment, Session as SessionModel


@pytest.mark.asyncio
async def test_end_campaign_sets_status_and_ended_at(api_client) -> None:
    seed = await api_client.post("/api/seed")
    assert seed.status_code == 200
    cid = seed.json()["campaign_id"]

    ended = await api_client.post(f"/api/campaigns/{cid}/end")
    assert ended.status_code == 200
    body = ended.json()
    assert body["status"] == "ended"
    assert body["ended_at"] is not None

    again = await api_client.post(f"/api/campaigns/{cid}/end")
    assert again.status_code == 200
    assert again.json()["status"] == "ended"
    assert again.json()["ended_at"] == body["ended_at"]


@pytest.mark.asyncio
async def test_turn_rejected_on_ended_campaign(fake_llm_turn, fake_retrieval, api_client) -> None:
    seed = await api_client.post("/api/seed")
    assert seed.status_code == 200
    cid = seed.json()["campaign_id"]
    session_id = seed.json()["session_id"]

    end = await api_client.post(f"/api/campaigns/{cid}/end")
    assert end.status_code == 200

    turn = await api_client.post(
        f"/api/sessions/{session_id}/turn",
        json={"message": "I attack."},
    )
    assert turn.status_code == 409
    assert "ended" in turn.json()["detail"].lower()


@pytest.mark.asyncio
async def test_reopen_campaign_restores_active(fake_llm_turn, fake_retrieval, api_client) -> None:
    seed = await api_client.post("/api/seed")
    cid = seed.json()["campaign_id"]
    session_id = seed.json()["session_id"]

    await api_client.post(f"/api/campaigns/{cid}/end")

    reopened = await api_client.post(f"/api/campaigns/{cid}/reopen")
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "active"
    assert reopened.json()["ended_at"] is None

    chars = await api_client.get("/api/characters")
    assert chars.status_code == 200
    char_id = chars.json()[0]["id"]
    reassign = await api_client.post(
        f"/api/campaigns/{cid}/characters",
        json={"character_id": char_id},
    )
    assert reassign.status_code in (200, 201)

    turn = await api_client.post(
        f"/api/sessions/{session_id}/turn",
        json={"message": "I continue."},
    )
    assert turn.status_code == 200


@pytest.mark.asyncio
async def test_list_campaigns_filters_by_status(api_client) -> None:
    seed = await api_client.post("/api/seed")
    cid = seed.json()["campaign_id"]

    active_only = await api_client.get("/api/campaigns", params={"status": "active"})
    assert active_only.status_code == 200
    assert any(c["id"] == cid for c in active_only.json())

    await api_client.post(f"/api/campaigns/{cid}/end")

    ended_only = await api_client.get("/api/campaigns", params={"status": "ended"})
    assert ended_only.status_code == 200
    assert any(c["id"] == cid for c in ended_only.json())

    active_after = await api_client.get("/api/campaigns", params={"status": "active"})
    assert not any(c["id"] == cid for c in active_after.json())


@pytest.mark.asyncio
async def test_end_campaign_closes_active_assignments(api_client, db_session) -> None:
    """POST /campaigns/{id}/end sets ended_at on active character_assignments (plan 03)."""
    seed = await api_client.post("/api/seed")
    assert seed.status_code == 200
    cid = seed.json()["campaign_id"]

    result = await db_session.execute(
        select(CharacterAssignment).where(
            CharacterAssignment.campaign_id == cid,
            CharacterAssignment.ended_at.is_(None),
        )
    )
    before = result.scalar_one_or_none()
    assert before is not None
    assert before.ended_at is None

    end = await api_client.post(f"/api/campaigns/{cid}/end")
    assert end.status_code == 200

    await db_session.refresh(before)
    assert before.ended_at == datetime.fromisoformat(end.json()["ended_at"])


@pytest.mark.asyncio
async def test_campaign_roster_excludes_characters_after_campaign_end(api_client) -> None:
    seed = await api_client.post("/api/seed")
    cid = seed.json()["campaign_id"]

    roster = await api_client.get(f"/api/campaigns/{cid}/characters")
    assert roster.status_code == 200
    assert roster.json()

    await api_client.post(f"/api/campaigns/{cid}/end")

    empty = await api_client.get(f"/api/campaigns/{cid}/characters")
    assert empty.status_code == 200
    assert empty.json() == []


@pytest.mark.asyncio
async def test_assign_character_to_ended_campaign_returns_409(api_client) -> None:
    seed = await api_client.post("/api/seed")
    assert seed.status_code == 200
    cid = seed.json()["campaign_id"]

    await api_client.post(f"/api/campaigns/{cid}/end")

    created = await api_client.post(
        "/api/characters",
        json={
            "name": "Straggler",
            "character_class": "Rogue",
            "level": 1,
            "hp_max": 10,
            "base_stats": {},
            "base_inventory": {},
        },
    )
    assert created.status_code == 201
    new_id = created.json()["id"]

    assign = await api_client.post(
        f"/api/campaigns/{cid}/characters",
        json={"character_id": new_id},
    )
    assert assign.status_code == 409
    assert "ended" in assign.json()["detail"].lower()



async def _create_character(api_client, name: str) -> int:
    created = await api_client.post(
        "/api/characters",
        json={"name": name, "character_class": "Rogue", "level": 1, "hp_max": 10},
    )
    assert created.status_code == 201
    return created.json()["id"]


@pytest.mark.asyncio
async def test_reopen_restores_only_assignments_closed_by_the_end(api_client) -> None:
    """Reopen restores exactly the assignments `end` closed, not ones released before it."""
    seed = await api_client.post("/api/seed")
    cid = seed.json()["campaign_id"]

    released_id = await _create_character(api_client, "Released")
    assert (
        await api_client.post(f"/api/campaigns/{cid}/characters", json={"character_id": released_id})
    ).status_code == 201
    assert (
        await api_client.delete(f"/api/campaigns/{cid}/characters/{released_id}")
    ).status_code == 204

    roster_before = {r["character_id"] for r in (await api_client.get(f"/api/campaigns/{cid}/characters")).json()}
    assert released_id not in roster_before and roster_before

    await api_client.post(f"/api/campaigns/{cid}/end")
    reopened = await api_client.post(f"/api/campaigns/{cid}/reopen")
    assert reopened.status_code == 200

    roster_after = {r["character_id"] for r in (await api_client.get(f"/api/campaigns/{cid}/characters")).json()}
    assert roster_after == roster_before


async def _owner_id(character_id: int) -> int:
    async with db_session_module.async_session_factory() as db:
        return await db.scalar(select(Character.owner_user_id).where(Character.id == character_id))


async def _assert_one_closure(campaign_id: int, *, status: str) -> None:
    """Campaign and every assignment of it agree: one closure timestamp, or none when active."""
    async with db_session_module.async_session_factory() as db:
        campaign = await db.get(Campaign, campaign_id)
        stamps = (
            await db.scalars(
                select(CharacterAssignment.ended_at).where(
                    CharacterAssignment.campaign_id == campaign_id
                )
            )
        ).all()
    assert campaign.status == status
    assert stamps and all(stamp == campaign.ended_at for stamp in stamps)
    assert (campaign.ended_at is None) == (status == "active")


@pytest.mark.asyncio
async def test_concurrent_manual_ends_close_once(api_client) -> None:
    """Both ends read the campaign as active; the one that waits must no-op, not re-stamp."""
    cid = (await api_client.post("/api/seed")).json()["campaign_id"]
    factory = db_session_module.async_session_factory
    async with factory() as first, factory() as second:
        assert await close_campaign(first, await first.get(Campaign, cid))
        loser = asyncio.create_task(close_campaign(second, await second.get(Campaign, cid)))
        await asyncio.sleep(0.2)
        assert not loser.done()
        await first.commit()
        assert await asyncio.wait_for(loser, timeout=5) is False
    await _assert_one_closure(cid, status="ended")


@pytest.mark.asyncio
async def test_automatic_completion_and_manual_end_close_once(api_client) -> None:
    """Turn persistence (session row, then campaign) and a manual end serialize on the campaign."""
    seed = (await api_client.post("/api/seed")).json()
    cid = seed["campaign_id"]
    accepted = AdjudicationResult(success=True, completion_status="accepted")
    factory = db_session_module.async_session_factory
    async with factory() as turn, factory() as manual:
        await turn.execute(
            select(SessionModel.id)
            .where(SessionModel.id == seed["session_id"])
            .with_for_update()
        )
        assert await maybe_complete_campaign(turn, cid, accepted)
        loser = asyncio.create_task(close_campaign(manual, await manual.get(Campaign, cid)))
        await asyncio.sleep(0.2)
        assert not loser.done()
        await turn.commit()
        assert await asyncio.wait_for(loser, timeout=5) is False
    await _assert_one_closure(cid, status="ended")


@pytest.mark.asyncio
async def test_reopen_waits_for_in_flight_end(api_client) -> None:
    cid = (await api_client.post("/api/seed")).json()["campaign_id"]
    factory = db_session_module.async_session_factory
    async with factory() as ending, factory() as reopening:
        assert await close_campaign(ending, await ending.get(Campaign, cid))
        reopen = asyncio.create_task(reopen_campaign(cid, None, db=reopening))
        await asyncio.sleep(0.2)
        await ending.commit()
        await asyncio.wait_for(reopen, timeout=5)
    await _assert_one_closure(cid, status="active")


@pytest.mark.asyncio
async def test_release_waits_for_in_flight_end_and_keeps_closure_stamp(api_client) -> None:
    cid = (await api_client.post("/api/seed")).json()["campaign_id"]
    char_id = (await api_client.get(f"/api/campaigns/{cid}/characters")).json()[0]["character_id"]
    owner = await _owner_id(char_id)
    factory = db_session_module.async_session_factory
    async with factory() as ending, factory() as releasing:
        assert await close_campaign(ending, await ending.get(Campaign, cid))
        release = asyncio.create_task(
            release_character_from_campaign(cid, char_id, None, db=releasing, owner_id=owner)
        )
        await asyncio.sleep(0.2)
        await ending.commit()
        with pytest.raises(HTTPException) as exc:
            await asyncio.wait_for(release, timeout=5)
    assert exc.value.status_code == 404
    await _assert_one_closure(cid, status="ended")


@pytest.mark.asyncio
async def test_assign_waits_for_in_flight_end_and_is_rejected(api_client) -> None:
    cid = (await api_client.post("/api/seed")).json()["campaign_id"]
    new_id = await _create_character(api_client, "Latecomer")
    owner = await _owner_id(new_id)
    factory = db_session_module.async_session_factory
    async with factory() as ending, factory() as assigning:
        assert await close_campaign(ending, await ending.get(Campaign, cid))
        assign = asyncio.create_task(
            assign_character_to_campaign(
                cid,
                CampaignCharacterAssignBody(character_id=new_id),
                auth=None,
                db=assigning,
                owner_id=owner,
            )
        )
        await asyncio.sleep(0.2)
        await ending.commit()
        with pytest.raises(HTTPException) as exc:
            await asyncio.wait_for(assign, timeout=5)
    assert exc.value.status_code == 409
    await _assert_one_closure(cid, status="ended")
