"""World flag updates keep campaign scope and the caller's transaction boundaries."""

import asyncio

from sqlalchemy import select

from apps.api.services.world_flags import upsert_world_flag
from db.postgres.models import Campaign, WorldFlag


async def test_world_flag_crud_keeps_campaigns_isolated(api_client) -> None:
    first = (await api_client.post("/api/seed")).json()["campaign_id"]
    created_campaign = await api_client.post(
        "/api/campaigns", json={"title": "Second", "system": "D&D 5e"}
    )
    assert created_campaign.status_code == 201
    second = created_campaign.json()["id"]

    def flag_url(campaign_id: int) -> str:
        return f"/api/campaigns/{campaign_id}/world-flags"

    created = await api_client.post(flag_url(first), json={"key": "door", "value": "closed"})
    assert created.status_code == 201
    flag_id = created.json()["id"]
    await api_client.post(flag_url(second), json={"key": "door", "value": "locked"})

    updated = await api_client.post(flag_url(first), json={"key": "door", "value": "open"})
    assert updated.status_code == 201
    assert updated.json()["id"] == flag_id
    assert updated.json()["value"] == "open"

    patched = await api_client.patch(f"{flag_url(first)}/door", json={"value": "broken"})
    assert patched.status_code == 200
    assert patched.json()["value"] == "broken"
    assert (await api_client.get(flag_url(second))).json()[0]["value"] == "locked"

    assert (await api_client.delete(f"{flag_url(first)}/door")).status_code == 204
    assert (await api_client.get(flag_url(first))).json() == []
    assert (await api_client.get(flag_url(second))).json()[0]["value"] == "locked"


async def test_world_flag_missing_rows_keep_404_responses(api_client) -> None:
    campaign_id = (await api_client.post("/api/seed")).json()["campaign_id"]
    missing_flag = f"/api/campaigns/{campaign_id}/world-flags/missing"
    for response in (
        await api_client.patch(missing_flag, json={"value": "x"}),
        await api_client.delete(missing_flag),
    ):
        assert response.status_code == 404
        assert response.json()["detail"] == "World flag not found"

    response = await api_client.post(
        "/api/campaigns/999999/world-flags", json={"key": "door", "value": "open"}
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Campaign not found"


async def test_world_flag_upsert_can_be_rolled_back(api_client, db_session) -> None:
    campaign_id = (await api_client.post("/api/seed")).json()["campaign_id"]
    db_session.add(WorldFlag(campaign_id=campaign_id, key="door", value="closed"))
    await db_session.commit()

    await upsert_world_flag(db_session, campaign_id, "door", "open")
    await upsert_world_flag(db_session, campaign_id, "torch", "lit")
    await db_session.flush()
    await db_session.rollback()

    rows = await db_session.execute(select(WorldFlag).where(WorldFlag.campaign_id == campaign_id))
    assert {flag.key: flag.value for flag in rows.scalars()} == {"door": "closed"}


async def test_concurrent_world_flag_inserts_share_one_row_and_both_transactions_commit(
    api_client,
    db_session,
) -> None:
    from db.postgres import session as session_module

    campaign_id = (await api_client.post("/api/seed")).json()["campaign_id"]
    barrier = asyncio.Barrier(8)

    async def update(value: str):
        async with session_module.async_session_factory() as db:
            # Establish independent connections before releasing simultaneous writes.
            await db.execute(select(Campaign.id).where(Campaign.id == campaign_id))
            await barrier.wait()
            flag = await upsert_world_flag(db, campaign_id, "shared_door", value)
            await db.commit()
            return flag.id

    values = [f"value-{index}" for index in range(8)]
    ids = await asyncio.wait_for(asyncio.gather(*(update(value) for value in values)), timeout=10)
    assert len(set(ids)) == 1
    rows = (
        (
            await db_session.execute(
                select(WorldFlag).where(
                    WorldFlag.campaign_id == campaign_id, WorldFlag.key == "shared_door"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1 and rows[0].value in values


async def test_world_flag_upsert_refreshes_existing_identity(api_client, db_session) -> None:
    campaign_id = (await api_client.post("/api/seed")).json()["campaign_id"]
    original = await upsert_world_flag(db_session, campaign_id, "door", "closed")
    await db_session.commit()
    updated = await upsert_world_flag(db_session, campaign_id, "door", "open")
    assert updated is original
    assert original.value == "open"
