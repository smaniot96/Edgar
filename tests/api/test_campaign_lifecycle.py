"""Campaign lifecycle: active vs ended (plan 02)."""

import pytest
from sqlalchemy import select

from db.postgres.models import CharacterAssignment


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
    assert before.ended_at is not None


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
    assert created.status_code == 200
    new_id = created.json()["id"]

    assign = await api_client.post(
        f"/api/campaigns/{cid}/characters",
        json={"character_id": new_id},
    )
    assert assign.status_code == 409
    assert "ended" in assign.json()["detail"].lower()

