import pytest


@pytest.mark.asyncio
async def test_create_list_get_campaign(api_client) -> None:
    seed = await api_client.post("/api/seed")
    assert seed.status_code == 200
    uid = seed.json()["user_id"]

    created = await api_client.post(
        "/api/campaigns",
        json={"title": "West Marches", "system": "D&D 5e", "created_by": uid},
    )
    assert created.status_code == 200
    body = created.json()
    cid = body["id"]
    assert body["title"] == "West Marches"

    listed = await api_client.get("/api/campaigns")
    assert listed.status_code == 200
    ids = {c["id"] for c in listed.json()}
    assert cid in ids

    one = await api_client.get(f"/api/campaigns/{cid}")
    assert one.status_code == 200
    assert one.json()["id"] == cid


@pytest.mark.asyncio
async def test_patch_delete_campaign(api_client) -> None:
    seed = await api_client.post("/api/seed")
    uid = seed.json()["user_id"]
    created = await api_client.post(
        "/api/campaigns",
        json={"title": "Tmp", "system": "5e", "created_by": uid},
    )
    cid = created.json()["id"]

    patched = await api_client.patch(f"/api/campaigns/{cid}", json={"title": "Renamed"})
    assert patched.status_code == 200
    assert patched.json()["title"] == "Renamed"

    deleted = await api_client.delete(f"/api/campaigns/{cid}")
    assert deleted.status_code == 204
    missing = await api_client.get(f"/api/campaigns/{cid}")
    assert missing.status_code == 404
